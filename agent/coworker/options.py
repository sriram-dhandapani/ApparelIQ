"""Option simulation for a T1 case (AG-04: every number on the card comes from here, never from the LLM).

Mirrors gold.mart_rebalancing_pairs: target cover 4 weeks, deficit = ceil(rate*28 - available), source transferable =
min(surplus, DC available - open wholesale commitments), DC->DC lanes into the destination market, guardrail
lane_cost / destination full price <= erp.margin_guardrail.max_transfer_cost_pct.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

TARGET_DAYS = 28
PARTIAL_DAYS = 14
# Store extension (mirrors gold.mart_store_rebalancing defaults in gold.trigger_config, trigger_id STORE)
STORE_SHORT_MAX_WEEKS = 1.5      # a store is SHORT below this cover (only for SKU-sizes at risk in the market)
STORE_DONOR_MIN_WEEKS = 4.5      # a store can donate at or above this cover
STORE_BRIDGE_DAYS = 14           # store-to-store moves top a short store up to 2 weeks (bridge until the DC refill lands)
STORE_DONOR_KEEP_DAYS = 28       # a donor always keeps 4 weeks of its own demand ...
STORE_DISPLAY_MIN_UNITS = 2      # ... plus display stock


@dataclass
class Constraints:
    """Teammate constraints captured from the chat (CW-07); each one is attributed to who asked for it."""
    max_units: int | None = None
    exclude_source_markets: list[str] = field(default_factory=list)
    exclude_source_locations: list[str] = field(default_factory=list)
    max_lead_days: int | None = None
    min_gm_pct: float | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Constraints":
        d = d or {}
        return cls(max_units=d.get("max_units"), exclude_source_markets=list(d.get("exclude_source_markets") or []),
                   exclude_source_locations=list(d.get("exclude_source_locations") or []),
                   max_lead_days=d.get("max_lead_days"), min_gm_pct=d.get("min_gm_pct"), notes=list(d.get("notes") or []))

    def to_dict(self) -> dict:
        return {"max_units": self.max_units, "exclude_source_markets": self.exclude_source_markets,
                "exclude_source_locations": self.exclude_source_locations,
                "max_lead_days": self.max_lead_days, "min_gm_pct": self.min_gm_pct, "notes": self.notes}

    def merge(self, other: "Constraints") -> "Constraints":
        return Constraints(
            max_units=other.max_units if other.max_units is not None else self.max_units,
            exclude_source_markets=sorted(set(self.exclude_source_markets) | set(other.exclude_source_markets)),
            exclude_source_locations=sorted(set(self.exclude_source_locations) | set(other.exclude_source_locations)),
            max_lead_days=other.max_lead_days if other.max_lead_days is not None else self.max_lead_days,
            min_gm_pct=other.min_gm_pct if other.min_gm_pct is not None else self.min_gm_pct,
            notes=self.notes + other.notes)


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


def destination_needs(candidates: list[dict], market_id: str, alert_skus: set[str] | None = None,
                      horizon_days: int = TARGET_DAYS) -> list[dict]:
    """Destination SKU-sizes at risk: T1 rows (or event SKUs) with a deficit against the cover target."""
    out = []
    for c in candidates:
        if c["market_id"] != market_id:
            continue
        rate = _f(c.get("daily_rate"))
        avail = _f(c.get("total_available_units"))
        at_risk = bool(c.get("t1_stockout_risk")) or (alert_skus and c["sku_id"] in alert_skus)
        need = math.ceil(max(0.0, rate * horizon_days - avail))
        if not at_risk or need <= 0:
            continue
        dts = c.get("days_to_stockout")
        out.append({"sku_id": c["sku_id"], "size": c.get("size"), "need": int(need), "rate": rate,
                    "available": int(avail), "days_to_stockout": _f(dts, 999.0) if dts is not None else 999.0,
                    "price": _f(c.get("full_price_usd")), "ucost": _f(c.get("unit_cost_usd")),
                    "weeks_of_cover": _f(c.get("weeks_of_cover"), 0.0)})
    return sorted(out, key=lambda x: (x["days_to_stockout"], x["sku_id"]))


def source_supply(candidates: list[dict], dc_positions: list[dict], commitments: list[dict], dest_market: str,
                  exclude: list[str]) -> dict[str, list[dict]]:
    """Per SKU, source DCs in other markets with transferable surplus. Open wholesale commitments are protected (AG-03)."""
    surplus = {(c["sku_id"], c["market_id"]): int(_f(c.get("surplus_units"))) for c in candidates}
    protected: dict[tuple[str, str], int] = {}
    for w in commitments:
        k = (w["sku_id"], w["location_id"])
        protected[k] = protected.get(k, 0) + int(_f(w.get("open_units")))
    by_sku: dict[str, list[dict]] = {}
    left_by_market: dict[tuple[str, str], int] = dict(surplus)
    for p in sorted(dc_positions, key=lambda x: -int(_f(x.get("available_units")))):
        m = p["market_id"]
        if m == dest_market or m in exclude:
            continue
        k = (p["sku_id"], m)
        free = int(_f(p.get("available_units"))) - protected.get((p["sku_id"], p["location_id"]), 0)
        take = min(max(free, 0), left_by_market.get(k, 0))
        if take <= 0:
            continue
        left_by_market[k] -= take
        by_sku.setdefault(p["sku_id"], []).append({"location_id": p["location_id"], "market_id": m, "units": take,
                                                   "protected_units": protected.get((p["sku_id"], p["location_id"]), 0)})
    return by_sku


def _store_cover(s: dict) -> tuple[float, int, float | None]:
    rate = _f(s.get("demand_rate_daily"))
    avail = int(_f(s.get("available_units")))
    return rate, avail, (avail / (rate * 7) if rate > 0 else None)


def store_needs(stores: list[dict], at_risk_skus: set[str], prices: dict[str, tuple[float, float]]) -> list[dict]:
    """Stores running short on SKU-sizes that are at risk in the market: top up to STORE_BRIDGE_DAYS of cover."""
    out = []
    for s in stores:
        rate, avail, woc = _store_cover(s)
        if s["sku_id"] not in at_risk_skus or woc is None or woc >= STORE_SHORT_MAX_WEEKS:
            continue
        need = math.ceil(max(0.0, rate * STORE_BRIDGE_DAYS - avail))
        if need <= 0:
            continue
        price, ucost = prices.get(s["sku_id"], (_f(s.get("full_price_usd")), _f(s.get("unit_cost_usd"))))
        out.append({"sku_id": s["sku_id"], "size": s.get("size"), "location_id": s["location_id"],
                    "location_name": s.get("location_name"), "city": s.get("city"), "need": int(need), "rate": rate,
                    "available": avail, "days_to_stockout": round(avail / rate, 1), "price": price, "ucost": ucost,
                    "weeks_of_cover": round(woc, 2)})
    return sorted(out, key=lambda x: (x["days_to_stockout"], -x["rate"], x["location_id"], x["sku_id"]))


def store_supply(stores: list[dict], at_risk_skus: set[str], exclude: list[str]) -> dict[str, list[dict]]:
    """Donor stores: spare = available - 4 weeks of own demand - display stock, only above STORE_DONOR_MIN_WEEKS."""
    by_sku: dict[str, list[dict]] = {}
    for s in stores:
        rate, avail, woc = _store_cover(s)
        if s["sku_id"] not in at_risk_skus or s["location_id"] in exclude or woc is None or woc < STORE_DONOR_MIN_WEEKS:
            continue
        spare = math.floor(avail - rate * STORE_DONOR_KEEP_DAYS) - STORE_DISPLAY_MIN_UNITS
        if spare <= 0:
            continue
        by_sku.setdefault(s["sku_id"], []).append({"location_id": s["location_id"], "market_id": s.get("market_id"),
                                                   "units": int(spare), "location_name": s.get("location_name"),
                                                   "city": s.get("city"), "size": s.get("size"),
                                                   "weeks_of_cover": round(woc, 1), "available": avail})
    return by_sku


def _allocate(needs, supply, lanes, cap, key, cons: Constraints, horizon_scale: float = 1.0) -> list[dict]:
    remaining = {(s, x["location_id"]): x["units"] for s, xs in supply.items() for x in xs}
    lines = []
    for n in needs:
        want = min(n["need"], math.ceil(n["need"] * horizon_scale))
        opts = []
        for src in supply.get(n["sku_id"], []):
            for ln in lanes:
                if ln["from_location_id"] != src["location_id"]:
                    continue
                if n.get("location_id") and ln["to_location_id"] != n["location_id"]:
                    continue
                if cons.max_lead_days is not None and int(ln["lead_days"]) > cons.max_lead_days:
                    continue
                cost = _f(ln["cost_per_unit_usd"])
                if n["price"] <= 0 or (cap is not None and cost / n["price"] > cap + 1e-9):
                    continue
                if cons.min_gm_pct is not None and (n["price"] - n["ucost"] - cost) / n["price"] < cons.min_gm_pct:
                    continue
                opts.append((key(ln), src, ln))
        for _, src, ln in sorted(opts, key=lambda o: o[0]):
            if want <= 0:
                break
            k = (n["sku_id"], src["location_id"])
            take = min(want, remaining.get(k, 0))
            if take <= 0:
                continue
            remaining[k] -= take
            want -= take
            lines.append({"sku_id": n["sku_id"], "from_location_id": src["location_id"],
                          "to_location_id": ln["to_location_id"], "units": int(take), "lane_mode": ln["mode"],
                          "lead_days": int(ln["lead_days"]), "cost_per_unit_usd": round(_f(ln["cost_per_unit_usd"]), 4),
                          "full_price_usd": round(n["price"], 4), "unit_cost_usd": round(n["ucost"], 4),
                          "source_market_id": src["market_id"], "store_move": bool(n.get("location_id"))})
    if cons.max_units is not None:
        lines = scale_lines(lines, cons.max_units)
    return lines


def scale_lines(lines: list[dict], total: int) -> list[dict]:
    """Scale to a total with largest remainder (same rule as the SQL gateway, so the staged curve is preserved)."""
    have = sum(x["units"] for x in lines)
    if have <= total:
        return lines
    exact = [x["units"] * total / have for x in lines]
    base = [math.floor(e) for e in exact]
    order = sorted(range(len(lines)), key=lambda i: (-(exact[i] - base[i]), i))
    for i in order[: total - sum(base)]:
        base[i] += 1
    return [dict(x, units=b) for x, b in zip(lines, base) if b > 0]


def evaluate(lines: list[dict], needs: list[dict]) -> dict[str, Any]:
    # Store needs are keyed by (SKU, store); market needs by (SKU, None) and match any DC in the destination market.
    by_key = {(n["sku_id"], n.get("location_id")): n for n in needs}
    units = sum(x["units"] for x in lines)
    tcost = sum(x["units"] * x["cost_per_unit_usd"] for x in lines)
    gm = 0.0  # same formula as agent.usp_record_decision (materiality / Finance routing uses this)
    net = 0.0  # gm minus margin on demand lost before the stock lands (used to rank options)
    late_units = 0
    worst_pct = 0.0
    lost_gap_units = 0.0
    for x in lines:
        n = by_key.get((x["sku_id"], x["to_location_id"])) or by_key[(x["sku_id"], None)]
        unit_gm = x["full_price_usd"] - x["unit_cost_usd"]
        gap = max(0.0, x["lead_days"] - n["days_to_stockout"])
        lost = min(x["units"], n["rate"] * gap)
        lost_gap_units += lost
        gm += x["units"] * (unit_gm - x["cost_per_unit_usd"])
        net += (x["units"] - lost) * unit_gm - x["units"] * x["cost_per_unit_usd"]
        late_units += x["units"] if gap > 0 else 0
        worst_pct = max(worst_pct, x["cost_per_unit_usd"] / x["full_price_usd"] if x["full_price_usd"] else 1.0)
    covered = {x["sku_id"] for x in lines}
    store_lines = [x for x in lines if x.get("store_move")]
    return {"units": int(units), "transfer_cost_usd": round(tcost, 2), "projected_gm_usd": round(gm, 2),
            "net_benefit_usd": round(net, 2),
            "late_units": int(late_units), "lost_gap_units": round(lost_gap_units, 1),
            "transfer_cost_pct_of_price": round(worst_pct, 4), "skus_covered": len(covered),
            "max_lead_days": max((x["lead_days"] for x in lines), default=0),
            "source_markets": sorted({x["source_market_id"] for x in lines}),
            "modes": sorted({x["lane_mode"] for x in lines}),
            "store_units": int(sum(x["units"] for x in store_lines)),
            "store_lead_days": max((x["lead_days"] for x in store_lines), default=0),
            "donor_stores": sorted({x["from_location_id"] for x in store_lines}),
            "receiving_stores": sorted({x["to_location_id"] for x in store_lines})}


def inaction(needs: list[dict]) -> dict[str, Any]:
    lost_units = sum(n["need"] for n in needs)
    return {"lost_units": int(lost_units),
            "lost_full_price_sales_usd": round(sum(n["need"] * n["price"] for n in needs), 2),
            "lost_gm_usd": round(sum(n["need"] * (n["price"] - n["ucost"]) for n in needs), 2),
            "stockout_sku_sizes": len(needs),
            "earliest_stockout_days": round(min((n["days_to_stockout"] for n in needs), default=999.0), 1)}


def confidence(needs: list[dict], signals: dict, ev: dict) -> float:
    pers = _f(signals.get("persistence"), 0.5)
    c = 0.5 + 0.4 * pers
    if signals.get("stale_feed"):
        c -= 0.15
    if signals.get("spike"):
        c -= 0.1
    if ev["units"]:
        c -= 0.15 * ev["late_units"] / ev["units"]
    return round(max(0.2, min(0.95, c)), 2)


def build_options(*, market_id: str, candidates: list[dict], dc_positions: list[dict], lanes: list[dict],
                  guardrail: dict | None, commitments: list[dict], signals: dict, alert_skus: set[str] | None = None,
                  constraints: Constraints | None = None, materiality_gm_usd: float = 50000,
                  approval_limit_units: int | None = None, stores: list[dict] | None = None,
                  store_lanes: list[dict] | None = None) -> dict[str, Any]:
    cons = constraints or Constraints()
    cap = _f(guardrail.get("max_transfer_cost_pct"), None) if guardrail else None
    needs = destination_needs(candidates, market_id, alert_skus)
    supply = source_supply(candidates, dc_positions, commitments, market_id,
                           cons.exclude_source_markets)
    supply = {s: [x for x in xs if x["location_id"] not in cons.exclude_source_locations] for s, xs in supply.items()}
    at_risk = {n["sku_id"] for n in needs}
    prices = {n["sku_id"]: (n["price"], n["ucost"]) for n in needs}
    stores = [s for s in (stores or []) if s.get("market_id", market_id) == market_id]
    sneeds = store_needs(stores, at_risk, prices)
    ssupply = store_supply(stores, at_risk, cons.exclude_source_locations)
    s_lanes = [ln for ln in (store_lanes or [])
               if ln.get("to_market_id") == market_id and ln.get("from_market_id") == market_id]
    dest_lanes = [ln for ln in lanes if ln.get("to_market_id") == market_id and ln.get("from_market_id") != market_id]
    base = inaction(needs)

    options = [{"option_id": "S0", "option_label": "Maintain allocation (do nothing)", "kind": "MAINTAIN", "lines": [],
                "units": 0, "transfer_cost_usd": 0.0, "projected_gm_usd": 0.0, "feasible": True,
                "within_guardrail": True, "confidence": 0.9 if signals.get("spike") else 0.6,
                "tradeoff": (f"No cost or risk to other markets, but {base['lost_units']} units of full-price demand "
                             f"(${base['lost_full_price_sales_usd']:,.0f}) go unserved; first stock-out in "
                             f"{base['earliest_stockout_days']:.0f} days.")}]
    specs = [
        ("S1", "Rebalance via the cheapest lane", "REBALANCE", lambda ln: (_f(ln["cost_per_unit_usd"]), int(ln["lead_days"])), 1.0),
        ("S2", "Rebalance via the fastest lane", "EXPEDITE", lambda ln: (int(ln["lead_days"]), _f(ln["cost_per_unit_usd"])), 1.0),
        ("S3", "Partial rebalance (2 weeks of cover)", "PARTIAL", lambda ln: (_f(ln["cost_per_unit_usd"]), int(ln["lead_days"])),
         PARTIAL_DAYS / TARGET_DAYS),
    ]
    built: dict[str, list[dict]] = {}
    for oid, label, kind, key, scale in specs:
        built[oid] = _allocate(needs, supply, dest_lanes, cap, key, cons, scale)
    # Store extension: S4 moves spare units from slow stores to stores about to run out (same market, by truck);
    # S5 does that now AND refills the market from abroad with the S1 lines (bridge + refill).
    cheap = lambda ln: (_f(ln["cost_per_unit_usd"]), int(ln["lead_days"]))  # noqa: E731
    s4 = _allocate(sneeds, ssupply, s_lanes, cap, cheap, Constraints.from_dict(dict(cons.to_dict(), max_units=None)))
    if s4:
        specs.append(("S4", f"Move stock between {market_id} stores", "STORE_TRANSFER", None, 1.0))
        built["S4"] = scale_lines(s4, cons.max_units) if cons.max_units is not None else s4
        dc_ids = [k for k in ("S1", "S2", "S3") if built.get(k)]
        if dc_ids:
            base_id = max(dc_ids, key=lambda k: (evaluate(built[k], needs)["net_benefit_usd"], -int(k[1])))
            combo = s4 + [dict(x) for x in built[base_id]]
            specs.append(("S5", f"Move stock between stores now and refill from abroad ({base_id} lanes)",
                          "STORE_BRIDGE_REBALANCE", None, 1.0))
            built["S5"] = scale_lines(combo, cons.max_units) if cons.max_units is not None else combo
    all_needs = needs + sneeds
    seen = []
    for oid, label, kind, _key, _scale in specs:
        lines = built.get(oid) or []
        sig = sorted((x["sku_id"], x["from_location_id"], x["to_location_id"], x["lane_mode"], x["units"]) for x in lines)
        if not lines or sig in seen:
            continue
        seen.append(sig)
        ev = evaluate(lines, all_needs)
        modes = "/".join(ev["modes"])
        if kind == "STORE_TRANSFER":
            olabel = (f"{label}: {len(ev['donor_stores'])} slow-selling store(s) to {len(ev['receiving_stores'])} "
                      f"store(s) running short (TRUCK, up to {ev['max_lead_days']} days)")
        elif kind == "STORE_BRIDGE_REBALANCE":
            srcs = ", ".join(m for m in ev["source_markets"] if m != market_id)
            olabel = (f"{label}: {ev['store_units']} units store-to-store in {ev['store_lead_days']} days, "
                      f"then {srcs} to {market_id} ({modes}, up to {ev['max_lead_days']} days)")
        else:
            srcs = ", ".join(ev["source_markets"])
            olabel = f"{label}: {srcs} to {market_id} ({modes}, up to {ev['max_lead_days']} days)"
        o = {"option_id": oid, "kind": kind, "lines": lines, **ev,
             "option_label": olabel,
             "within_guardrail": cap is not None and ev["transfer_cost_pct_of_price"] <= cap + 1e-9,
             "confidence": confidence(needs, signals, ev)}
        o["feasible"] = o["within_guardrail"] and ev["units"] > 0 and ev["net_benefit_usd"] > 0
        o["needs_finance"] = ev["projected_gm_usd"] > materiality_gm_usd
        o["exceeds_owner_limit"] = approval_limit_units is not None and ev["units"] > approval_limit_units
        late = f"; {ev['late_units']} units land after the projected stock-out" if ev["late_units"] else "; lands before stock-out"
        o["tradeoff"] = (f"Moves {ev['units']} units for ${ev['transfer_cost_usd']:,.0f} transfer cost "
                         f"({ev['transfer_cost_pct_of_price']:.1%} of price, cap {cap:.1%}){late}."
                         if cap is not None else f"Moves {ev['units']} units; no guardrail configured.")
        if kind == "PARTIAL":
            o["tradeoff"] += " Lower exposure if the demand spike fades; may need a second move."
        if kind == "STORE_TRANSFER":
            o["tradeoff"] += (" Fast and cheap, but only reshuffles stock inside the market: the market shortfall "
                              "remains and donor stores keep just 4 weeks plus display stock.")
        if kind == "STORE_BRIDGE_REBALANCE":
            o["tradeoff"] += (f" Store moves cover the gap at the stores selling fastest while the {', '.join(m for m in ev['source_markets'] if m != market_id)} "
                              "stock is in transit; store managers must pick and receive the units.")
        options.append(o)

    feasible = [o for o in options[1:] if o["feasible"]]
    rec = max(feasible, key=lambda o: o["net_benefit_usd"] * o["confidence"], default=options[0])
    if signals.get("spike") and not signals.get("persistence_ok", True):
        partial = next((o for o in feasible if o["kind"] == "PARTIAL"), None)
        rec = partial or rec
    for o in options:
        o["recommended"] = o["option_id"] == rec["option_id"]
    return {"recommended_option_id": rec["option_id"], "options": options, "inaction": base, "needs": needs,
            "guardrail": guardrail or {}, "constraints": cons.to_dict(),
            "supply_markets": sorted({x["market_id"] for xs in supply.values() for x in xs}),
            "store_needs": sneeds,
            "donors": [dict(x, sku_id=s) for s, xs in ssupply.items() for x in xs]}


def recommendation_json(result: dict) -> dict:
    """Shape stored in agent.case.recommendation_json and copied into erp.allocation_decision evidence (HIST-shaped)."""
    return {"recommended_option_id": result["recommended_option_id"], "inaction": result["inaction"],
            "constraints": result["constraints"], "horizon_days": TARGET_DAYS,
            # Sizes at risk in the destination, for the plain-language briefing on the card.
            "needs": [{"sku_id": n["sku_id"], "size": n.get("size"), "short_units": n["need"], "available": n["available"],
                       "daily_rate": round(n["rate"], 1), "days_to_stockout": round(n["days_to_stockout"], 1)}
                      for n in result.get("needs") or []][:12],
            # Store extension: which stores run out first and which slow-selling stores can spare units.
            "store_needs": [{"location_id": n["location_id"], "location_name": n.get("location_name"), "city": n.get("city"),
                             "sku_id": n["sku_id"], "size": n.get("size"), "short_units": n["need"],
                             "available": n["available"], "daily_rate": round(n["rate"], 1),
                             "days_to_stockout": n["days_to_stockout"]}
                            for n in result.get("store_needs") or []][:15],
            "donors": [{"location_id": d["location_id"], "location_name": d.get("location_name"), "city": d.get("city"),
                        "sku_id": d["sku_id"], "size": d.get("size"), "spare_units": d["units"],
                        "available": d.get("available"), "weeks_of_cover": d.get("weeks_of_cover")}
                       for d in result.get("donors") or []][:15],
            # Every store named in a store need or donor (untruncated), so cards can say which store sends to which.
            "store_directory": {s["location_id"]: {"name": s.get("location_name"), "city": s.get("city")}
                                for s in (result.get("store_needs") or []) + (result.get("donors") or [])},
            "options": [{k: o.get(k) for k in ("option_id", "option_label", "kind", "units", "projected_gm_usd", "net_benefit_usd",
                                              "transfer_cost_usd", "transfer_cost_pct_of_price", "confidence", "feasible",
                                              "within_guardrail", "needs_finance", "exceeds_owner_limit", "max_lead_days",
                                              "late_units", "lost_gap_units", "source_markets", "modes", "tradeoff",
                                              "store_units", "store_lead_days", "donor_stores", "receiving_stores")}
                        for o in result["options"]]}


def staged_lines(option: dict) -> list[dict]:
    keep = ("sku_id", "from_location_id", "to_location_id", "units", "lane_mode", "lead_days", "cost_per_unit_usd",
            "full_price_usd", "unit_cost_usd")
    return [{k: x[k] for k in keep} for x in option["lines"]]
