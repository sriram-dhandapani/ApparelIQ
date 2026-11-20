"""Adaptive Cards 1.5 with Universal Actions (Action.Execute) for the case chat. Pure functions, no I/O.

Everyone in the chat sees the shared view (evidence, options, policy check, people, Tier 1 actions, sources) with
Ask / Comment actions. `refresh.userIds` makes Teams fetch a user-specific view for the case owners, so the accountable
owner sees Approve / Revise / Override / Defer / Reject and Finance sees Co-approve / Reject. "Show my actions" is the
manual fallback for anyone whose client did not auto-refresh.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

VERSION = "1.5"
SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
DECIDER_ROLES = {"ACCOUNTABLE", "ESCALATION", "OPERATOR"}
FINANCE_ROLES = {"FINANCE", "OPERATOR"}
OPEN_STATUSES = {"OPEN", "AWAITING_APPROVAL", "DEFERRED"}
REASON_CODES = [("SUSTAINED_DEMAND", "Sustained demand"), ("DEMAND_NOT_SUSTAINED", "Demand not sustained"),
                ("PARTNER_COMMITMENT", "Partner / wholesale commitment"), ("PRICE_PROTECTION", "Price protection"),
                ("INBOUND_PO_SOONER", "Inbound PO lands sooner"), ("LOCAL_EVENT", "Local event / promo at source"),
                ("OTHER", "Other (explain in rationale)")]
STATUS_STYLE = {"OPEN": "accent", "AWAITING_APPROVAL": "warning", "NEEDS_FINANCE": "warning", "DEFERRED": "default",
                "APPROVED": "good", "REJECTED": "attention", "CLOSED": "default"}


def mri(aad_object_id: str) -> str:
    return f"8:orgid:{aad_object_id}"


def action_id(case_id: str, verb: str, user: str, inputs: dict | None = None) -> str:
    """Idempotency key for the gateway (<= 64 chars): the same click with the same inputs maps to the same key."""
    blob = json.dumps({"c": case_id, "v": verb, "u": user, "i": inputs or {}}, sort_keys=True, default=str)
    return f"ui:{verb[:8]}:{hashlib.sha256(blob.encode()).hexdigest()[:40]}"


def viewer_roles(owners: list[dict], aad_object_id: str | None) -> set[str]:
    if not aad_object_id:
        return set()
    a = aad_object_id.lower()
    return {o["case_role"] for o in owners if str(o.get("aad_object_id") or "").lower() == a}


def _usd(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(x) -> str:
    try:
        return f"{float(x):.1%}"
    except (TypeError, ValueError):
        return "n/a"


def _tb(text: str, **kw) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, **kw}


def _section(title: str) -> dict:
    return _tb(title, weight="Bolder", size="Medium", spacing="Medium", separator=True)


def _facts(pairs: list[tuple[str, Any]]) -> dict:
    return {"type": "FactSet", "facts": [{"title": k, "value": str(v)} for k, v in pairs if v not in (None, "")]}


def _table(headers: list[str], rows: list[list[Any]]) -> dict:
    def cell(v, bold=False):
        # Short values never wrap; leaving "wrap" off them keeps big cases under the Teams message limit.
        tb = {"type": "TextBlock", "text": str(v), "size": "Small"}
        if len(tb["text"]) > 10:
            tb["wrap"] = True
        if bold:
            tb["weight"] = "Bolder"
        return {"type": "TableCell", "items": [tb]}
    return {"type": "Table", "firstRowAsHeaders": True, "showGridLines": False,
            "columns": [{"width": 1} for _ in headers],
            "rows": [{"type": "TableRow", "cells": [cell(h, True) for h in headers]}] +
                    [{"type": "TableRow", "cells": [cell(v) for v in r]} for r in rows]}


def _option_block(o: dict, staged_units: dict[str, int]) -> dict:
    rec = bool(o.get("recommended"))
    title = f"{'⭐ ' if rec else ''}{o['option_id']} · {o.get('option_label', '')}"
    guard = "n/a" if o["option_id"] == "S0" else ("✅ within guardrail" if o.get("within_guardrail") else "⛔ breaches guardrail")
    pairs = [("Units", o.get("units", 0)), ("Transfer cost", _usd(o.get("transfer_cost_usd"))),
             ("GM impact", _usd(o.get("projected_gm_usd"))), ("Net benefit", _usd(o.get("net_benefit_usd")))
             if o.get("net_benefit_usd") is not None else ("Net benefit", None),
             ("Lead time", f"up to {o['max_lead_days']} days" if o.get("max_lead_days") else None),
             ("Confidence", _pct(o.get("confidence")) if o.get("confidence") is not None else None),
             ("Policy", guard)]
    if o["option_id"] != "S0" and o["option_id"] not in staged_units:
        pairs.append(("Draft", "not staged (infeasible)"))
    if o.get("needs_finance"):
        pairs.append(("Approval", "Owner + Finance co-approval (above materiality)"))
    if o.get("exceeds_owner_limit"):
        pairs.append(("Approval", "Above the owner's unit limit; Revise to fewer units"))
    items = [_tb(title, weight="Bolder", color="Good" if rec else "Default"), _facts(pairs)]
    if o.get("tradeoff"):
        items.append(_tb(o["tradeoff"], size="Small", isSubtle=True))
    return {"type": "Container", "style": "emphasis" if rec else "default", "spacing": "Small", "items": items}


MODE_NAMES = {"AIR": "air freight", "OCEAN": "ocean freight", "OCEAN_AIR": "sea-air freight", "TRUCK": "truck",
              "ROAD": "road freight", "RAIL": "rail"}
ROLE_TODO = {
    "ACCOUNTABLE": "Decide. Read the briefing, then use the buttons at the bottom (Approve, Revise units, Choose another "
                   "option, Defer or Reject).",
    "ESCALATION": "Decide in place of the missing accountable owner, using the buttons at the bottom.",
    "OPERATOR": "Demo operator: can take any decision with the buttons at the bottom.",
    "FINANCE": "Co-approve or reject once the owner approves, because the margin impact is above the Finance threshold.",
    "PLANNER": "Check the demand is real and the sizes listed are the right ones. Reply in the chat if anything looks off.",
    "SOURCE_ACCOUNTABLE": "Stock would leave your market. Reply in the chat if you need it (promo, partner order) and I'll "
                          "re-plan without it.",
    "SOURCE_PLANNER": "Confirm the units listed from your distribution centre really are spare.",
    "WHOLESALE_LEAD": "Confirm your open wholesale orders are unaffected (they are protected in every option).",
    "STORE_MANAGER_DEST": "Your store is about to run out. Confirm you can receive the units and put them on the floor "
                          "the day they arrive.",
    "STORE_MANAGER_SOURCE": "Your store has more than it will sell in 4 weeks. Confirm the spare units can be picked and "
                            "sent; you always keep 4 weeks of sales plus display stock.",
}
GLOSSARY = [
    ("SKU-size", "One product in one size, e.g. HM9443 in size 10.5. Stock-outs happen size by size."),
    ("Weeks of cover", "How many weeks the stock on hand lasts at the current sales pace. Below 1.5 weeks is a risk."),
    ("Intent index", "Shopper interest (searches, wish-lists, app views) compared with normal. 1.0 = normal; "
                     "1.3 or higher plus low cover triggers a case."),
    ("Demand persistence", "0 to 1 score of whether the extra demand has lasted. Below 0.5 means it may be a short spike."),
    ("Transfer cost cap", "Policy limit: moving a unit may not cost more than this share of its full selling price."),
    ("Gross margin (GM)", "Selling price minus product cost minus transfer cost: the money the move protects."),
    ("Lands after stock-out", "Units that arrive after their size has already sold out, so some sales in that gap are lost."),
    ("Finance threshold", "Decisions with a margin impact above this amount also need Finance co-approval."),
    ("DC", "Distribution centre (warehouse) that ships stock to stores and online orders."),
    ("Store-to-store move", "Sending spare units from a slow-selling store to a nearby store in the same market that is "
                            "about to sell out. Arrives by truck in 1 to 3 days."),
    ("Donor store", "A store with at least 4.5 weeks of stock for a size. It keeps 4 weeks of its own sales plus 2 display "
                    "units and gives only the rest."),
]


def _f(x, default: float | None = 0.0) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _size(sku_id) -> str:
    return str(sku_id).rsplit("-", 1)[-1]


def store_label(case: dict, loc: str) -> str:
    """'Kinetiq NY Flagship, New York (ST-US-1002)' from the store directory, falling back to the store needs/donors."""
    rec = case.get("recommendation") or {}
    s = (rec.get("store_directory") or {}).get(loc)
    if not s:
        s = next(({"name": x.get("location_name"), "city": x.get("city")}
                  for x in (rec.get("store_needs") or []) + (rec.get("donors") or []) if x.get("location_id") == loc), {})
    name, city = s.get("name"), s.get("city")
    if not name:
        return loc
    return f"{name}{', ' + city if city and city not in name else ''} ({loc})"


def _is_store(case: dict, loc: str) -> bool:
    return loc in ((case.get("recommendation") or {}).get("store_directory") or {}) or str(loc).startswith("ST-")


def store_moves(case: dict, option_id: str | None, managers: dict[str, str] | None = None) -> list[dict]:
    """Store-to-store lines of one option, one entry per (from store, to store), with sizes, units, cost and lead time."""
    moves: dict[tuple, dict] = {}
    for d in case.get("drafts") or []:
        if d.get("option_id") != option_id or d.get("draft_status") == "CANCELLED":
            continue
        f, t = d.get("from_location_id"), d.get("to_location_id")
        if not (f and t and _is_store(case, f) and _is_store(case, t)):
            continue
        units = int(d.get("released_units") or d.get("units") or 0)
        g = moves.setdefault((f, t), {"from_id": f, "to_id": t, "from": store_label(case, f), "to": store_label(case, t),
                                      "from_manager": (managers or {}).get(f), "to_manager": (managers or {}).get(t),
                                      "sizes": [], "units": 0, "cost": 0.0, "lead_days": 0, "mode": d.get("lane_mode")})
        g["sizes"].append((_size(d.get("sku_id")), units))
        g["units"] += units
        g["cost"] += units * (_f(d.get("cost_per_unit_usd")) or 0)
        g["lead_days"] = max(g["lead_days"], int(d.get("lead_days") or 0))
    for g in moves.values():
        g["sizes"].sort(key=lambda x: (_f(x[0], 999), x[0]))
        g["size_text"] = ", ".join(f"{u} × size {s}" for s, u in g["sizes"])
    return sorted(moves.values(), key=lambda g: (-g["units"], g["from_id"], g["to_id"]))


def store_managers(owners: list[dict]) -> dict[str, str]:
    """store location_id -> 'Persona (user)' for the store managers on the case."""
    out: dict[str, str] = {}
    for o in owners:
        loc = o.get("location_id")
        if loc and str(o.get("case_role", "")).startswith("STORE_MANAGER"):
            who, user = o.get("persona_name") or o.get("person_id"), o.get("user_display_name")
            out[loc] = who + (f" ({user})" if user and user != who else "")
    return out


def _move_sentence(m: dict) -> str:
    return (f"**{m['from']}** → **{m['to']}**: {m['size_text']} ({m['units']} units), by truck, arrives in "
            f"{m['lead_days']} days, costs {_usd(m['cost'])}")


def _plain_option(o: dict, market: str, case: dict | None = None) -> str:
    oid = o["option_id"]
    if oid == "S0":
        return f"**S0, do nothing:** no cost, but the stock-out losses above happen."
    srcs = ", ".join(m for m in (o.get("source_markets") or []) if m != market)
    gm, cost = _usd(o.get("projected_gm_usd")), _usd(o.get("transfer_cost_usd"))
    extra = "" if o.get("within_guardrail", True) else " ⛔ breaks the transfer cost cap, so it cannot be approved."
    late = f", {o['late_units']} land late" if o.get("late_units") else ", all before stock-out"
    moves = store_moves(case, oid) if case else []
    route = ("; moves: " + "; ".join(f"{m['from']} → {m['to']} ({m['units']})" for m in moves)) if moves else ""
    if o.get("kind") == "STORE_TRANSFER":
        return (f"**{oid}, store-to-store only:** {o.get('units', 0)} spare units from "
                f"{len(o.get('donor_stores') or [])} slow-selling {market} store(s) to "
                f"{len(o.get('receiving_stores') or [])} store(s) about to run out, by truck in up to "
                f"{o.get('max_lead_days')} days{late}; costs {cost}{route}. Fast and cheap, but the market stays short "
                f"overall.{extra}")
    if o.get("kind") == "STORE_BRIDGE_REBALANCE":
        return (f"**{oid}, store-to-store now plus refill from abroad:** {o.get('store_units', 0)} units between {market} "
                f"stores in {o.get('store_lead_days')} days{route}, plus {o.get('units', 0) - (o.get('store_units') or 0)} "
                f"units from {srcs} in up to {o.get('max_lead_days')} days{late}; costs {cost}, protects {gm} margin.{extra}")
    what = {"REBALANCE": "cheapest shipping", "EXPEDITE": "fastest shipping", "PARTIAL": "move only 2 weeks of need"}.get(
        o.get("kind"), o.get("option_label", ""))
    return (f"**{oid}, {what}:** {o.get('units', 0)} units from {srcs} to {market} in up to {o.get('max_lead_days')} days"
            f"{late}; costs {cost}, protects {gm} margin.{extra}")


def _chart(kind: str, title: str, **kw) -> dict:
    # Teams clients without chart support (e.g. some mobile versions) drop the chart; the tables below carry the same data.
    return {"type": f"Chart.{kind}", "title": title, "fallback": "drop", **kw}


def _short_label(text: str, n: int = 22) -> str:
    text = str(text).replace("Kinetiq ", "")
    return text if len(text) <= n else text[: n - 1] + "…"


def charts(case: dict) -> list[dict]:
    """'At a glance' charts built only from case data (Teams Adaptive Card charts, no images, no model numbers)."""
    ev = case.get("evidence") or {}
    rec = case.get("recommendation") or {}
    ina = rec.get("inaction") or {}
    market = case.get("market_id")
    horizon = int(rec.get("horizon_days") or 28)
    rec_id = case.get("recommended_option_id") or rec.get("recommended_option_id")
    out: list[dict] = []

    days = _f(ina.get("earliest_stockout_days"), None)
    if days is not None and days < 900:
        week = round(horizon / 4)
        # The docs table names the segment width "value" and their sample uses "size"; send both.
        seg = lambda legend, n, color: {"legend": legend, "value": n, "size": n, "color": color}  # noqa: E731
        out.append(_chart("Gauge", "Days until the first size sells out", value=round(max(days, 0)), min=0,
                          max=horizon, valueFormat="Fraction", subLabel=f"out of the next {horizon} days",
                          segments=[seg("Urgent (1 week)", week, "attention"), seg("Soon (2 weeks)", week, "warning"),
                                    seg("Time to plan", horizon - 2 * week, "good")]))
    mk = [m for m in ev.get("markets") or [] if m.get("weeks_of_cover") is not None][:8]
    if mk:
        out.append(_chart("HorizontalBar", "Weeks of stock left, by market", xAxisTitle="Market",
                          yAxisTitle="Weeks of stock",
                          data=[{"x": m["market_id"] + (" (this case)" if m["market_id"] == market else ""),
                                 "y": round(_f(m["weeks_of_cover"]) or 0, 1),
                                 "color": "attention" if m["market_id"] == market else "good"} for m in mk]))

    opts = [o for o in rec.get("options") or [] if o.get("option_id") != "S0" and o.get("transfer_cost_usd") is not None]
    if opts:
        lbl = lambda o: o["option_id"] + (" ⭐" if o["option_id"] == rec_id else "")  # noqa: E731
        out.append(_chart("VerticalBar.Grouped", "Margin protected vs shipping cost, by option",
                          xAxisTitle="Option (⭐ recommended)", yAxisTitle="US dollars",
                          data=[{"legend": "Margin protected", "color": "good",
                                 "values": [{"x": lbl(o), "y": round(_f(o.get("projected_gm_usd")) or 0)} for o in opts]},
                                {"legend": "Shipping cost", "color": "attention",
                                 "values": [{"x": lbl(o), "y": round(_f(o.get("transfer_cost_usd")) or 0)} for o in opts]}]))

    needs = [n for n in rec.get("needs") or [] if n.get("short_units")][:9]
    if needs:
        out.append(_chart("HorizontalBar", f"Units short by size, next {horizon} days", xAxisTitle="Size",
                          yAxisTitle="Units short", color="attention",
                          data=[{"x": f"Size {n.get('size') or _size(n['sku_id'])}", "y": int(n["short_units"])}
                                for n in needs]))
    stores = ([(f"{_short_label(n.get('location_name') or n['location_id'], 16)} · {n.get('size') or _size(n['sku_id'])}"
                f" needs", int(n.get("short_units") or 0), "attention") for n in (rec.get("store_needs") or [])[:5]] +
              [(f"{_short_label(d.get('location_name') or d['location_id'], 16)} · {d.get('size') or _size(d['sku_id'])}"
                f" spare", int(d.get("spare_units") or 0), "good") for d in (rec.get("donors") or [])[:5]])
    if stores:
        out.append(_chart("HorizontalBar", f"{market} stores: needed (red) vs spare (green)",
                          xAxisTitle="Store · size", yAxisTitle="Units",
                          data=[{"x": x, "y": y, "color": c} for x, y, c in stores]))
    return out


GLANCE_ID = "glance"


def _chart_rows(blocks: list[dict]) -> list[dict]:
    """Two charts per row; a lone last chart keeps half width so every chart is the same size."""
    rows = []
    for i in range(0, len(blocks), 2):
        pair = blocks[i:i + 2]
        cols = [{"type": "Column", "width": 1, "items": [c]} for c in pair]
        if len(cols) == 1:
            cols.append({"type": "Column", "width": 1, "items": []})
        rows.append({"type": "ColumnSet", "spacing": "Medium", "separator": i > 0, "columns": cols})
    return rows


def glance(case: dict) -> list[dict]:
    """'At a glance' header with a Show/hide charts button, then the charts two per row (visible by default)."""
    blocks = charts(case)
    if not blocks:
        return []
    head = {"type": "ColumnSet", "id": f"{GLANCE_ID}_head", "spacing": "Medium", "columns": [
        {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center",
         "items": [_tb("At a glance", weight="Bolder", size="Medium")]},
        {"type": "Column", "width": "auto", "items": [
            {"type": "ActionSet", "actions": [{"type": "Action.ToggleVisibility", "title": "Show / hide charts",
                                               "targetElements": [GLANCE_ID]}]}]}]}
    return [head, {"type": "Container", "id": GLANCE_ID, "spacing": "Small", "items": _chart_rows(blocks)}]


def charts_card(case: dict, data_label: str = "SYNTHETIC") -> dict | None:
    """Fallback only: when a very large case card has no room for the charts, they are posted as their own card."""
    blocks = charts(case) if (case.get("recommendation") or {}).get("inaction") else []
    if not blocks:
        return None
    ev = case.get("evidence") or {}
    head = (f"At a glance · {case.get('style_id')} {ev.get('style_name') or ''} · {case.get('market_id')}").replace("  ", " ")
    return {"$schema": SCHEMA, "type": "AdaptiveCard", "version": VERSION, "msteams": {"width": "Full"},
            "body": [_tb(head, weight="Bolder", size="Medium"),
                     _tb(f"Charts for case {case.get('case_id')}; the case card above has the full detail and the "
                         "decision buttons.", size="Small", isSubtle=True, spacing="None")] + _chart_rows(blocks) +
                    [_tb(f"{data_label} demo data", size="Small", isSubtle=True, separator=True)]}


def briefing(case: dict, owners: list[dict]) -> list[dict]:
    """Plain-language explanation for any reader: the problem, why it is real, the recommendation and what it means,
    how to decide, and who needs to do what. Built only from case data (no model text, so no invented numbers)."""
    ev = case.get("evidence") or {}
    rec = case.get("recommendation") or {}
    ina = rec.get("inaction") or {}
    if not ina:
        return [_tb(rec["brief"], spacing="Medium")] if rec.get("brief") else []
    market = case.get("market_id")
    style = f"{ev.get('style_name') or ''} ({case.get('style_id')})".strip()
    kind = " ".join(x for x in (str(case.get("lifecycle") or "").lower(), str(case.get("category") or "").lower()) if x)
    rec_id = case.get("recommended_option_id") or rec.get("recommended_option_id")
    opts = rec.get("options") or []
    best = next((o for o in opts if o["option_id"] == rec_id), {})
    sig = ev.get("signals") or {}
    guard = ev.get("guardrail") or {}
    cap = _f(guard.get("max_transfer_cost_pct"), None)
    horizon = rec.get("horizon_days") or 28
    out: list[dict] = []

    # 1. The problem
    days = _f(ina.get("earliest_stockout_days"), None)
    prob = (f"**{style}**, a {kind} style, is selling faster than **{market}** can keep in stock. At the current sales "
            f"pace the first size sells out in **about {days:.0f} days**" if days is not None and days < 900 else
            f"**{style}**, a {kind} style, is short of stock in **{market}**")
    prob += (f", and {ina.get('stockout_sku_sizes')} sizes will not have enough stock for the next {horizon} days. "
             f"If nothing is done, about **{ina.get('lost_units')} units** of full-price sales are lost: "
             f"**{_usd(ina.get('lost_full_price_sales_usd'))} in revenue** and {_usd(ina.get('lost_gm_usd'))} in gross margin.")
    inbound = [p for p in ev.get("inbound_dest") or [] if p.get("market_id") == market]
    if inbound:
        po = inbound[0]
        slip = f", already {po['slip_days']} days late" if (po.get("slip_days") or 0) > 0 else ""
        prob += (f" A replenishment order is on the way ({po.get('po_id')}, {po.get('units')} units, promised "
                 f"{str(po.get('current_promise_date'))[:10]}{slip}), but it does not arrive in time to prevent this.")
    else:
        prob += f" No replenishment order arrives in {market} in time."
    out.append({"type": "Container", "style": "warning", "spacing": "Medium",
                "items": [_tb("The problem", weight="Bolder", size="Medium"), _tb(prob)]})
    out += glance(case)
    needs = rec.get("needs") or []
    if needs:
        out.append(_tb(f"Sizes running short in {market}", weight="Bolder", size="Small"))
        out.append(_table(["Size", "Sells out in", "In stock now", "Selling per day", f"Short over {horizon} days"],
                          [[n.get("size") or n["sku_id"],
                            "already out" if (n.get("days_to_stockout") or 0) <= 0 else
                            ("beyond horizon" if n["days_to_stockout"] >= 900 else f"{n['days_to_stockout']:.0f} days"),
                            n.get("available"), n.get("daily_rate"), n.get("short_units")] for n in needs[:9]]))
    sneeds = rec.get("store_needs") or []
    if sneeds:
        out.append(_tb(f"{market} stores that run out first (store view)", weight="Bolder", size="Small"))
        out.append(_table(["Store", "Size", "Sells out in", "In stock", "Selling per day", "Needs (2 weeks)"],
                          [[n.get("location_name") or n["location_id"], n.get("size") or n["sku_id"],
                            "already out" if (n.get("days_to_stockout") or 0) <= 0 else f"{n['days_to_stockout']:.0f} days",
                            n.get("available"), n.get("daily_rate"), n.get("short_units")] for n in sneeds[:8]]))
    sdonors = rec.get("donors") or []
    if sneeds and not any(o.get("kind") in ("STORE_TRANSFER", "STORE_BRIDGE_REBALANCE") for o in opts):
        note = (f"**Why there is no store-to-store move this time:** no {market} store has spare stock of these sizes. "
                "Every store is either short itself or needs all it has (each store keeps at least 4 weeks of its own "
                "sales plus 2 display units).")
        sent = ev.get("store_transfers_open") or []
        if sent:
            stos = sorted({t["sto_id"] for t in sent if t.get("sto_id")})
            sdir = rec.get("store_directory") or {}
            nm = lambda i: (sdir.get(i) or {}).get("name") or i  # noqa: E731
            note += (f" The spare store stock was already moved by an earlier approved transfer "
                     f"({', '.join(stos)}: {sum(int(t.get('units') or 0) for t in sent)} units, "
                     + "; ".join(f"{nm(t['from_location_id'])} → {nm(t['to_location_id'])}" for t in sent[:4])
                     + "). Those units are counted as on their way, so they are not planned twice. "
                     "Only a refill from distribution centres can close the remaining gap.")
        out.append({"type": "Container", "style": "emphasis", "items": [_tb(note, size="Small")]})
    if sdonors:
        out.append(_tb(f"{market} stores with spare stock (selling slowly)", weight="Bolder", size="Small"))
        out.append(_table(["Store", "Size", "In stock", "Weeks of stock", "Can spare"],
                          [[d.get("location_name") or d["location_id"], d.get("size") or d["sku_id"], d.get("available"),
                            d.get("weeks_of_cover"), d.get("spare_units")] for d in sdonors[:8]]))

    # 2. Why I think it is real
    why = []
    ii = _f(sig.get("intent_intraday"), None)
    if ii:
        why.append(f"Shopper interest is **{ii:.1f}x normal** (1.0 = normal; a case opens at 1.3x with low stock).")
    pers = _f(sig.get("persistence"), None)
    if pers is not None:
        why.append(f"The extra demand has held up (persistence {pers:.2f} out of 1), so it is unlikely to be a one-day blip."
                   if sig.get("persistence_ok") else
                   f"The extra demand may be a short spike (persistence {pers:.2f} out of 1"
                   + (f"; reason: {', '.join(sig.get('spike_reasons') or [])}" if sig.get("spike_reasons") else "")
                   + "). Moving fewer units is the safer choice.")
    mk = ev.get("markets") or []
    dest = next((m for m in mk if m["market_id"] == market), None)
    donors = sorted([m for m in mk if m["market_id"] in (best.get("source_markets") or [])],
                    key=lambda m: -(m.get("transferable") or 0))
    if dest and dest.get("weeks_of_cover") is not None:
        txt = f"{market} has only **{dest['weeks_of_cover']:.1f} weeks** of stock left for this style"
        if donors:
            txt += "; " + ", ".join(f"{m['market_id']} has {m['weeks_of_cover'] if m.get('weeks_of_cover') is not None else 'n/a'}"
                                    f" weeks and {m.get('transferable', 0)} spare units" for m in donors[:3])
        why.append(txt + ".")
    why.append("⚠ One data feed for this market is stale, so treat the numbers with some caution." if sig.get("stale_feed")
               else "All data feeds are current.")
    today = str(case.get("opened_at_utc") or dt.datetime.now(dt.timezone.utc).isoformat())[:10]
    for e in (ev.get("events") or [])[:2]:
        start, end = str(e.get("start_date"))[:10], str(e.get("end_date"))[:10]
        when = e.get("timing") or ("upcoming" if start > today else "recent" if end < today else "ongoing")
        span = start if start == end else f"{start} to {end}"
        if when == "upcoming":
            why.append(f"Upcoming event in {e.get('market_id')}: {e.get('event_name')} ({span}).")
        elif when == "ongoing":
            why.append(f"Event happening now in {e.get('market_id')}: {e.get('event_name')} ({span}).")
        else:
            why.append(f"Recent event in {e.get('market_id')}: {e.get('event_name')} ended {end}; "
                       "its extra demand may already be fading.")
    prec = (ev.get("precedent") or [])[:1]
    if prec:
        p = prec[0]
        why.append(f"Last comparable decision: {str(p.get('decision', '')).lower()}"
                   + (f", {p['approved_units']} units" if p.get("approved_units") else "")
                   + (f" ({str(p.get('reason_code')).replace('_', ' ').lower()})" if p.get("reason_code") else "")
                   + f" on {str(p.get('decided_at_utc'))[:10]}.")
    out.append(_tb("Why I believe this is real", weight="Bolder", size="Medium", spacing="Medium"))
    out += [_tb("• " + w, spacing="None") for w in why]

    # 3. The recommendation in plain words
    if best and rec_id != "S0":
        srcs = ", ".join(m for m in (best.get("source_markets") or []) if m != market)
        modes = " / ".join(MODE_NAMES.get(m, m.lower()) for m in best.get("modes") or [])
        kind_ = best.get("kind")
        s_units = int(best.get("store_units") or 0)
        nd, nr = len(best.get("donor_stores") or []), len(best.get("receiving_stores") or [])
        mgrs = store_managers(owners)
        moves = store_moves(case, rec_id, mgrs)
        if kind_ == "STORE_TRANSFER":
            what = (f"Send **{s_units} spare units** by truck from **{nd} slow-selling {market} store(s)** to **{nr} "
                    f"store(s)** that are about to run out (each move is listed below). Everything arrives within "
                    f"**{best.get('max_lead_days')} days**. This only moves stock inside {market}, so the market as a whole "
                    f"stays short.")
        elif kind_ == "STORE_BRIDGE_REBALANCE":
            what = (f"Two steps. **Step 1, now:** send **{s_units} spare units** by truck from **{nd} slow-selling store(s)** "
                    f"to **{nr} store(s)** that are about to run out (arrives in **{best.get('store_lead_days')} days**), so "
                    f"they keep selling. **Step 2, refill:** ship **{best.get('units', 0) - s_units} units** from "
                    f"distribution centres in **{srcs}** to the {market} distribution centre by {modes}, arriving within "
                    f"**{best.get('max_lead_days')} days**. Both steps are listed below.")
        else:
            what = (f"Ship **{best.get('units')} units** from distribution centres in **{srcs}** to the {market} "
                    f"distribution centre by {modes}. Everything arrives within **{best.get('max_lead_days')} days**.")
        items = [_tb(f"My recommendation: {rec_id}", weight="Bolder", size="Medium"), _tb(what)]
        if moves:
            items.append(_tb(("Step 1, now: " if kind_ == "STORE_BRIDGE_REBALANCE" else "")
                             + f"store-to-store moves in {market} (from → to)", weight="Bolder", spacing="Medium"))
            items += [_tb(f"{i}. " + _move_sentence(m), spacing="None") for i, m in enumerate(moves[:8], 1)]
            items.append(_table(["From store (sends)", "To store (receives)", "Sizes", "Units", "Arrives in", "Cost",
                                 "Sender / receiver manager"],
                                [[m["from"], m["to"], m["size_text"], m["units"], f"{m['lead_days']} days", _usd(m["cost"]),
                                  f"{m.get('from_manager') or 'n/a'} / {m.get('to_manager') or 'n/a'}"] for m in moves[:8]]))
        moved = {(m["from_id"], m["to_id"]) for m in moves}
        lines: dict[tuple, dict] = {}
        for d in case.get("drafts") or []:
            if (d.get("option_id") == rec_id and d.get("draft_status") == "PROPOSED"
                    and (d["from_location_id"], d.get("to_location_id")) not in moved):
                to = d.get("to_location_id") or f"{market} DC"
                k = (d["from_location_id"], to, d.get("lane_mode"), d.get("lead_days"))
                g = lines.setdefault(k, {"units": 0, "cost": 0.0, "sizes": 0})
                g["units"] += int(d["units"])
                g["cost"] += int(d["units"]) * (_f(d.get("cost_per_unit_usd")) or 0)
                g["sizes"] += 1
        if lines:
            if moves:
                items.append(_tb(f"Step 2, refill: distribution centre shipments into {market}", weight="Bolder",
                                 spacing="Medium"))
            rows = sorted(lines.items(), key=lambda kv: -kv[1]["units"])
            items.append(_table(["Ship from", "To", "Units", "Sizes", "How", "Arrives in", "Cost"],
                                [[k[0], k[1], g["units"], g["sizes"], MODE_NAMES.get(k[2], k[2]), f"{k[3]} days",
                                  _usd(g["cost"])] for k, g in rows[:12]]))
        pct = _f(best.get("transfer_cost_pct_of_price"), None)
        cost = f"**What it costs:** {_usd(best.get('transfer_cost_usd'))} in shipping"
        if pct is not None and cap is not None:
            cost += (f", which is {pct:.1%} of the selling price. Policy allows up to {cap:.1%}, so this is "
                     + ("✅ within policy." if best.get("within_guardrail") else "⛔ over the limit."))
        else:
            cost += "."
        items.append(_tb(cost))
        items.append(_tb(f"**What it protects:** about {_usd(best.get('projected_gm_usd'))} of gross margin after shipping "
                         f"costs, compared with losing {_usd(ina.get('lost_gm_usd'))} if we do nothing."))
        if best.get("late_units"):
            gap = _f(best.get("lost_gap_units"), None)
            faster = next((o for o in opts if o.get("kind") == "EXPEDITE" and o["option_id"] != rec_id), None)
            items.append(_tb(f"**Heads-up:** {best['late_units']} of these units arrive after their size has already sold out"
                             + (f", so about {gap:.0f} sales are still lost while waiting" if gap else "")
                             + (f". {faster['option_id']} uses faster shipping to shrink that gap for "
                                f"{_usd(faster.get('transfer_cost_usd'))}." if faster else ".")))
        safe = (f"**Safe for the source markets:** I only move stock above what {srcs} need for their own "
                "sales, and open wholesale orders are never touched." if srcs else "")
        if s_units:
            safe += (" **Safe for donor stores:** each keeps at least 4 weeks of its own sales plus 2 display units.")
        if safe:
            items.append(_tb(safe.strip(), size="Small"))
        appr = "**Who approves:** the accountable owner"
        if best.get("needs_finance"):
            appr += ", plus Finance, because the margin impact is above the Finance threshold"
        if best.get("exceeds_owner_limit"):
            appr += ". The quantity is above the owner's limit, so use Revise units to approve fewer"
        items.append(_tb(appr + ". Nothing ships until then.", size="Small"))
        out.append({"type": "Container", "style": "good", "spacing": "Medium", "items": items})
    elif best:
        out.append(_tb(f"**My recommendation: S0, do nothing for now.** No transfer is worth it: "
                       f"{best.get('tradeoff') or ''}", spacing="Medium"))

    # 4. How to decide
    others = [o for o in opts if o["option_id"] != rec_id]
    out.append(_tb("How to decide (accountable owner)", weight="Bolder", size="Medium", spacing="Medium"))
    out += [_tb(t, spacing="None") for t in [
        f"✅ **Approve {rec_id}** if you agree the demand is real and the source markets can spare the stock.",
        "✏️ **Revise units** to move fewer, for example if the demand may fade. Give a short reason.",
        "🔀 **Choose another option** if a different trade-off suits you better (reason code required):",
    ]]
    out += [_tb("    " + _plain_option(o, market, case), size="Small", spacing="None") for o in others]
    out += [_tb(t, spacing="None") for t in [
        "⏸ **Defer** if you are waiting for information (for example promo dates). The case re-opens on the date you pick.",
        "❌ **Reject** if you think the signal is wrong. Nothing moves and your reason is recorded in ERP.",
        "💬 **Not the owner?** Use *Add a constraint / challenge* (for example \"exclude GB\" or \"max 400 units\") and I "
        "re-run the options, or *Ask the agent* any question about the data or policy.",
    ]]

    # 5. Who needs to do what
    todo: dict[str, list[str]] = {}
    for o in owners:
        if o["case_role"] in ROLE_TODO:
            who = o.get("persona_name") or o.get("person_id")
            user = o.get("user_display_name")
            label = who + (f" ({user})" if user and user != who else "")
            todo.setdefault(o["case_role"], [])
            if label not in todo[o["case_role"]]:
                todo[o["case_role"]].append(label)
    if todo:
        out.append(_tb("Who needs to do what", weight="Bolder", size="Medium", spacing="Medium"))
        for role in ROLE_TODO:
            if role in todo and not (role == "FINANCE" and not best.get("needs_finance")
                                     and case.get("case_status") != "NEEDS_FINANCE"):
                out.append(_tb(f"• **{', '.join(todo[role][:4])}** · {role.replace('_', ' ').title()}: {ROLE_TODO[role]}",
                               spacing="None"))
        # Store managers get the exact pick / receive list for their own store.
        for m in (store_moves(case, rec_id, store_managers(owners)) if rec_id else []):
            if m.get("from_manager"):
                out.append(_tb(f"• **{m['from_manager']}** at {m['from']}: pick and send {m['size_text']} to {m['to']}.",
                               spacing="None"))
            if m.get("to_manager"):
                out.append(_tb(f"• **{m['to_manager']}** at {m['to']}: receive {m['size_text']} from {m['from']} "
                               f"in {m['lead_days']} days and put them on the floor.", spacing="None"))

    # 6. Glossary (collapsed)
    out.append({"type": "ActionSet", "actions": [{"type": "Action.ToggleVisibility", "title": "What do these terms mean?",
                                                  "targetElements": ["glossary"]}]})
    out.append({"type": "Container", "id": "glossary", "isVisible": False,
                "items": [_facts([(k, v) for k, v in GLOSSARY])]})
    return out


def case_card(case: dict, owners: list[dict], *, roles: set[str] | None = None, banner: str | None = None,
              data_label: str = "SYNTHETIC") -> dict:
    """case = agent.usp_get_case JSON (evidence, recommendation, drafts, members, actions)."""
    ev = case.get("evidence") or {}
    rec = case.get("recommendation") or {}
    status = case.get("case_status", "OPEN")
    opts = rec.get("options") or []
    staged: dict[str, int] = {}
    for d in case.get("drafts") or []:
        if d.get("draft_status") == "PROPOSED":
            staged[d["option_id"]] = staged.get(d["option_id"], 0) + int(d["units"])
    rec_id = case.get("recommended_option_id") or rec.get("recommended_option_id")
    for o in opts:
        o["recommended"] = o["option_id"] == rec_id
    ver = ev.get("verification") or {}
    sig = ev.get("signals") or {}
    style_name = (ev.get("style_name") or "")

    body: list[dict] = [
        {"type": "ColumnSet", "columns": [
            {"type": "Column", "width": "stretch", "items": [
                _tb(f"UC1 Inventory rebalancing · {case.get('style_id')} {style_name} · {case.get('market_id')}",
                    weight="Bolder", size="Large"),
                _tb(f"Case {case.get('case_id')} · {case.get('category', '')} / {case.get('lifecycle', '')} · "
                    f"severity {case.get('severity', '')}", size="Small", isSubtle=True, spacing="None")]},
            {"type": "Column", "width": "auto", "items": [
                {"type": "Container", "style": STATUS_STYLE.get(status, "default"),
                 "items": [_tb(status.replace("_", " "), weight="Bolder", size="Small")]},
                _tb(f"{data_label} DATA", size="Small", color="Attention", weight="Bolder", horizontalAlignment="Right")]}]}]
    if banner:
        body.append({"type": "Container", "style": "accent", "items": [_tb(banner, weight="Bolder")]})
    body += briefing(case, owners)

    body += [_section("Details: verified evidence"),
             _facts([("Trigger", "T1 stock-out risk: weeks of cover < 1.5 and intent index ≥ 1.3"),
                     ("Intraday signal [E1]", f"{len(ver.get('intraday_t1_skus') or [])} SKU-sizes at {ver.get('signal_ts') or 'n/a'}"),
                     ("Batch check [E2]", f"{len(ver.get('batch_t1_skus') or [])} SKU-sizes, gold as of {ver.get('gold_as_of')}"),
                     ("Intent index (intraday)", sig.get("intent_intraday")),
                     ("Demand persistence", f"{sig.get('persistence')} ({'sustained' if sig.get('persistence_ok') else 'may fade'})"),
                     ("Data quality", "⚠ stale feed in this market" if sig.get("stale_feed") else "feeds current")])]
    markets = ev.get("markets") or []
    if markets:
        body.append(_tb("Weeks of cover by market (style level)", size="Small", weight="Bolder"))
        body.append(_table(["Market", "Weeks of cover", "Transferable", "T1 SKUs"],
                           [[m["market_id"] + (" ◀" if m["market_id"] == case.get("market_id") else ""),
                             m.get("weeks_of_cover") if m.get("weeks_of_cover") is not None else "n/a",
                             m.get("transferable", 0), m.get("t1_skus", 0)] for m in markets[:8]]))
    notes = []
    if ev.get("resend_duplicates"):
        notes.append(f"Ignored {len(ev['resend_duplicates'])} duplicate wholesale re-send line(s) (DQ-11).")
    if (rec.get("constraints") or {}).get("notes"):
        notes += [f"Constraint: {n}" for n in rec["constraints"]["notes"]]
    if rec.get("inaction"):
        ina = rec["inaction"]
        notes.insert(0, f"If we do nothing: {ina.get('lost_units')} units / {_usd(ina.get('lost_full_price_sales_usd'))} "
                        f"full-price sales at risk; first stock-out in {ina.get('earliest_stockout_days')} days.")
    for n in notes:
        body.append(_tb("• " + n, size="Small"))

    body.append(_section(f"All options in detail ({len(opts)}) · recommended {rec_id}"))
    body += [_option_block(o, staged) for o in opts]

    pc = rec.get("policy_check") or {}
    if pc:
        body.append(_section("Policy check (Foundry IQ)"))
        body.append(_tb(pc.get("text", ""), size="Small"))
        cites = sorted({c.get("doc") for c in pc.get("citations") or [] if c.get("doc")})
        if cites:
            body.append(_tb("Sources: " + ", ".join(cites), size="Small", isSubtle=True))

    members = case.get("members") or []
    if (members or owners) and not (rec.get("inaction") or {}):
        body.append(_section("People"))
        seen = []
        for o in owners:
            key = (o.get("persona_name"), o["case_role"])
            if key in seen:
                continue
            seen.append(key)
            body.append(_tb(f"• {o['case_role'].replace('_', ' ').title()}: {o.get('persona_name') or o.get('person_id')}"
                            f"{' (' + o['title'] + ')' if o.get('title') else ''}", size="Small", spacing="None"))
    acts = [a for a in (case.get("actions") or []) if a.get("tier") in (1, "1")][:5]
    if acts:
        body.append(_section("What I already did (Tier 1, reversible)"))
        for a in acts:
            body.append(_tb(f"• {a['action_type']} · {a['outcome']} · {str(a.get('created_at_utc', ''))[:16]}",
                            size="Small", spacing="None"))
    srcs = ev.get("sources") or []
    if srcs:
        body.append(_section("Sources"))
        for s in srcs:
            body.append(_tb(f"[{s['id']}] {s['system']} · {s['object']} · as of {s.get('as_of') or 'n/a'}",
                            size="Small", isSubtle=True, spacing="None"))
    body.append(_tb(f"{data_label} demo data. Nothing is written to ERP/WMS until the accountable owner approves "
                    "(and Finance co-approves above materiality).", size="Small", isSubtle=True, separator=True))

    cid = case.get("case_id")
    card = {"$schema": SCHEMA, "type": "AdaptiveCard", "version": VERSION, "body": body,
            "msteams": {"width": "Full"},
            "actions": _actions(case, opts, staged, roles or set(), status)}
    user_ids = []
    for o in owners:
        if o.get("aad_object_id") and o["case_role"] in DECIDER_ROLES | FINANCE_ROLES | {"PLANNER"}:
            m = mri(str(o["aad_object_id"]))
            if m not in user_ids:
                user_ids.append(m)
    card["refresh"] = {"action": {"type": "Action.Execute", "title": "Refresh", "verb": "refresh", "data": {"case_id": cid}},
                       "userIds": user_ids[:60]}
    if card_bytes(card) > CARD_LIMIT:  # very large case: leave the charts off; the orchestrator posts them separately
        card["body"] = [b for b in body if b.get("id") not in (GLANCE_ID, f"{GLANCE_ID}_head")]
    return card


CARD_LIMIT = 92_000  # Teams bot messages are capped at 100 KB (UTF-16); keep room for the activity envelope


def has_charts(card: dict) -> bool:
    return any(b.get("id") == GLANCE_ID for b in card.get("body") or [])


def card_bytes(card: dict) -> int:
    """Size as Teams counts it for the bot message limit (UTF-16)."""
    return len(json.dumps(card, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-16-le"))


def _show(title: str, verb: str, case_id: str, inputs: list[dict], submit: str, style: str | None = None) -> dict:
    act = {"type": "Action.Execute", "title": submit, "verb": verb, "data": {"case_id": case_id}}
    if style:
        act["style"] = style
    return {"type": "Action.ShowCard", "title": title,
            "card": {"type": "AdaptiveCard", "version": VERSION, "body": inputs, "actions": [act]}}


def _actions(case: dict, opts: list[dict], staged: dict[str, int], roles: set[str], status: str) -> list[dict]:
    cid = case.get("case_id")
    rec_id = case.get("recommended_option_id")
    acts: list[dict] = []
    if roles & DECIDER_ROLES and status in OPEN_STATUSES:
        rec_units = staged.get(rec_id, 0)
        label = f"Approve {rec_id}" + (f" ({rec_units} units)" if rec_units else "")
        acts.append({"type": "Action.Execute", "title": label, "verb": "approve", "style": "positive",
                     "data": {"case_id": cid, "option_id": rec_id}})
        if rec_units:
            acts.append(_show("Revise units", "revise", cid, [
                {"type": "Input.Number", "id": "rev_units", "label": f"Units (1 to {rec_units})", "min": 1, "max": rec_units,
                 "value": rec_units, "isRequired": True, "errorMessage": f"Enter 1 to {rec_units}"},
                {"type": "Input.Text", "id": "rev_rationale", "label": "Business rationale", "isMultiline": True,
                 "isRequired": True, "errorMessage": "A rationale is required (HL-03)"}], "Submit revision"))
        choices = [{"title": f"{o['option_id']} · {o.get('option_label', '')[:60]}", "value": o["option_id"]}
                   for o in opts if o["option_id"] != rec_id and (o["option_id"] == "S0" or o["option_id"] in staged)]
        if choices:
            acts.append(_show("Choose another option", "override", cid, [
                {"type": "Input.ChoiceSet", "id": "ovr_option", "label": "Option", "choices": choices,
                 "value": choices[0]["value"], "isRequired": True},
                {"type": "Input.ChoiceSet", "id": "ovr_reason", "label": "Reason code", "isRequired": True,
                 "choices": [{"title": t, "value": v} for v, t in REASON_CODES], "errorMessage": "Pick a reason code (HL-06)"},
                {"type": "Input.Text", "id": "ovr_rationale", "label": "Rationale", "isMultiline": True, "isRequired": True,
                 "errorMessage": "A rationale is required (HL-06)"}], "Submit override"))
        acts.append(_show("Defer", "defer", cid, [
            {"type": "Input.Date", "id": "def_date", "label": "Re-open on (UTC)", "isRequired": True,
             "errorMessage": "Pick a future date (HL-04)"}], "Defer case"))
        acts.append(_show("Reject", "reject", cid, [
            {"type": "Input.Text", "id": "rej_rationale", "label": "Why? (recorded in ERP)", "isMultiline": True}],
            "Reject recommendation", "destructive"))
    elif status == "NEEDS_FINANCE" and roles & FINANCE_ROLES:
        acts.append({"type": "Action.Execute", "title": f"Co-approve {case.get('pending_option_id') or rec_id}"
                     f" ({case.get('pending_units') or ''} units)", "verb": "approve", "style": "positive",
                     "data": {"case_id": cid}})
        acts.append(_show("Reject", "reject", cid, [
            {"type": "Input.Text", "id": "rej_rationale", "label": "Why? (recorded in ERP)", "isMultiline": True}],
            "Reject", "destructive"))
    elif status == "NEEDS_FINANCE" and roles & DECIDER_ROLES:
        acts.append(_show("Withdraw (reject)", "reject", cid, [
            {"type": "Input.Text", "id": "rej_rationale", "label": "Why?", "isMultiline": True}], "Reject", "destructive"))
    acts.append(_show("Ask the agent", "ask", cid, [
        {"type": "Input.Text", "id": "ask_text", "label": "Question (policy, data or this case)", "isMultiline": True,
         "isRequired": True, "placeholder": "e.g. Can GB spare stock before its promo? What does KQ-FIN-102 say?"}], "Ask"))
    if status in OPEN_STATUSES | {"NEEDS_FINANCE"}:
        acts.append(_show("Add a constraint / challenge", "comment", cid, [
            {"type": "Input.Text", "id": "cmt_text", "label": "Constraint or challenge (I re-simulate the options)",
             "isMultiline": True, "isRequired": True,
             "placeholder": "e.g. Exclude GB, it has a promo next week. Keep it under 400 units."}], "Send"))
    acts.append({"type": "Action.Execute", "title": "Refresh" if roles else "Refresh / show my actions",
                 "verb": "refresh", "data": {"case_id": cid}})
    return acts


def status_card(title: str, lines: list[str], style: str = "default", data_label: str = "SYNTHETIC") -> dict:
    """Small card for 'working on it', plan restatement and execution receipts."""
    return {"$schema": SCHEMA, "type": "AdaptiveCard", "version": VERSION,
            "body": [{"type": "Container", "style": style, "items": [_tb(title, weight="Bolder")]}] +
                    [_tb(x, size="Small", spacing="Small") for x in lines] +
                    [_tb(f"{data_label} demo data", size="Small", isSubtle=True)]}
