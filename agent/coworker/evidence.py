"""CW-01/CW-04: verify the trigger and assemble cited evidence from Fabric (Eventhouse + Gold over OneLake) and ERP/WMS.

Every number shown to people comes from these queries; each block carries a `source` string used as a citation.
"""
from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import kql, sql

log = logging.getLogger(__name__)

KQL_CANDIDATES = """
external_table('ext_gold_rebalancing_candidates')
| where style_id == p_style
| project sku_id, market_id, style_id, size, category, lifecycle, is_launch, total_available_units, dc_available_units,
    committed_wholesale_units, in_transit_units, daily_rate, daily_rate_7d, weeks_of_cover, days_to_stockout,
    unit_cost_usd, full_price_usd, markdown_pct, intent_index_7d, demand_class, spike_reason, demand_persistence_score,
    dq_stale_feed, recent_spike_7d, deficit_units, surplus_units, transferable_surplus_units, t1_stockout_risk,
    t2_aging_risk, stockout_date_est, rebalancing_role, as_of_date
"""

KQL_SIGNAL = """
rt_inventory_signal
| where style_id == p_style and market_id == p_market and signal_ts <= todatetime(p_at)
| summarize arg_max(signal_ts, *) by sku_id
| project signal_ts, sku_id, size, total_available_units, rt_units, rt_receipts, intraday_available, daily_rate_7d,
    intraday_rate, batch_weeks_of_cover, intraday_weeks_of_cover, intent_index_7d, intent_index_intraday, intent_used,
    demand_class, batch_t1, t1_intraday, last_sale
| order by intraday_weeks_of_cover asc
"""

SQL_STYLE = """
SELECT s.style_id, s.style_name, s.model, s.category, s.division, s.lifecycle, s.launch_date, s.msrp_usd, s.unit_cost_usd
FROM erp.product_style s WHERE s.style_id = ?"""

SQL_DC_POSITIONS = """
SELECT i.location_id, l.market_id, i.sku_id, k.size, i.on_hand_units, i.allocated_units, i.reserved_units, i.in_transit_units,
       ISNULL(i.on_hand_units,0) - ISNULL(i.allocated_units,0) - ISNULL(i.reserved_units,0) AS available_units, i.snapshot_ts
FROM wms.inventory_position i
JOIN erp.location l ON l.location_id = i.location_id AND l.location_type = N'DC'
JOIN erp.product_sku k ON k.sku_id = i.sku_id
WHERE k.style_id = ? AND i.inventory_status = N'AVAILABLE'"""

SQL_LANES = """
SELECT t.from_location_id, f.market_id AS from_market_id, t.to_location_id, d.market_id AS to_market_id,
       t.mode, t.lead_days, t.cost_per_unit_usd
FROM wms.transfer_lane t
JOIN erp.location f ON f.location_id = t.from_location_id AND f.location_type = N'DC'
JOIN erp.location d ON d.location_id = t.to_location_id AND d.location_type = N'DC'
WHERE d.market_id = ?"""

# Store extension: per-store rate, cover and SHORT / DONOR role from Gold; live store stock and store-to-store lanes from WMS.
KQL_STORES = """
external_table('ext_gold_store_rebalancing')
| where style_id == p_style and market_id == p_market
| project location_id, location_name, city, format, market_id, sku_id, size, demand_rate_daily, available_units,
    weeks_of_cover, days_to_stockout, store_role, deficit_units, surplus_units, full_price_usd, unit_cost_usd, as_of_date
"""

SQL_STORE_POSITIONS = """
SELECT i.location_id, l.location_name, l.city, l.format, l.market_id, i.sku_id, k.size,
       ISNULL(i.on_hand_units,0) - ISNULL(i.allocated_units,0) - ISNULL(i.reserved_units,0) AS available_units, i.snapshot_ts
FROM wms.inventory_position i
JOIN erp.location l ON l.location_id = i.location_id AND l.location_type = N'STORE'
JOIN erp.product_sku k ON k.sku_id = i.sku_id
WHERE k.style_id = ? AND l.market_id = ? AND i.inventory_status = N'AVAILABLE'"""

SQL_STORE_LANES = """
SELECT t.from_location_id, f.market_id AS from_market_id, t.to_location_id, d.market_id AS to_market_id,
       t.mode, t.lead_days, t.cost_per_unit_usd
FROM wms.transfer_lane t
JOIN erp.location f ON f.location_id = t.from_location_id AND f.location_type = N'STORE'
JOIN erp.location d ON d.location_id = t.to_location_id AND d.location_type = N'STORE'
WHERE d.market_id = ? AND f.market_id = d.market_id"""

# Approved store-to-store transfers not yet received: the units are already reserved at the sender (WMS), and are
# counted as on the way at the receiver so the next case does not propose the same move twice.
SQL_STORE_TRANSFERS_OPEN = """
SELECT s.sto_id, d.proposal_id AS case_id, s.sku_id, s.from_location_id, s.to_location_id, s.units, s.sto_status
FROM erp.stock_transfer_order s
JOIN erp.allocation_decision d ON d.decision_id = s.decision_id
JOIN erp.location f ON f.location_id = s.from_location_id AND f.location_type = N'STORE'
JOIN erp.location t ON t.location_id = s.to_location_id AND t.location_type = N'STORE'
JOIN erp.product_sku k ON k.sku_id = s.sku_id
WHERE k.style_id = ? AND t.market_id = ? AND s.sto_status IN (N'CREATED', N'RELEASED', N'IN_TRANSIT')"""

SQL_GUARDRAIL = """
SELECT g.market_id, g.category, g.lifecycle, g.min_gross_margin_pct, g.protection_days, g.max_transfer_cost_pct
FROM erp.margin_guardrail g JOIN erp.product_style s ON s.category = g.category AND s.lifecycle = g.lifecycle
WHERE s.style_id = ? AND g.market_id = ?"""

# Open wholesale lines are protected (AG-03). RESERVED lines are already inside wms reserved_units. DQ-11: a cancelled
# line re-sent as OPEN with a -RESEND message id is a duplicate and is excluded (and called out to the team).
SQL_COMMITMENTS = """
SELECT o.order_id, ol.line_no, o.partner_id, p.partner_name, p.tier, o.market_id, o.ship_from_location_id AS location_id,
       ol.sku_id, ol.ordered_units AS open_units, ol.line_status, o.requested_ship_date, ol.source_message_id,
       CASE WHEN ol.source_message_id LIKE N'%-RESEND' AND EXISTS (
                SELECT 1 FROM erp.wholesale_order_line c WHERE c.order_id = ol.order_id AND c.line_status = N'CANCELLED'
                  AND ol.source_message_id = c.source_message_id + N'-RESEND') THEN 1 ELSE 0 END AS is_resend_duplicate
FROM erp.wholesale_order_line ol
JOIN erp.wholesale_order o ON o.order_id = ol.order_id
JOIN erp.product_sku k ON k.sku_id = ol.sku_id
LEFT JOIN erp.wholesale_partner p ON p.partner_id = o.partner_id
WHERE k.style_id = ? AND ol.line_status IN (N'OPEN', N'RESERVED') AND o.order_status <> N'CANCELLED'"""

SQL_INBOUND = """
SELECT po.po_id, po.vendor_id, v.vendor_name, po.ship_to_location_id, l.market_id, po.original_promise_date,
       po.current_promise_date, DATEDIFF(DAY, po.original_promise_date, po.current_promise_date) AS slip_days, po.po_status,
       SUM(pl.ordered_units) AS units
FROM erp.purchase_order po
JOIN erp.purchase_order_line pl ON pl.po_id = po.po_id
JOIN erp.location l ON l.location_id = po.ship_to_location_id
LEFT JOIN erp.vendor v ON v.vendor_id = po.vendor_id
WHERE po.style_id = ? AND po.po_status IN (N'IN_TRANSIT', N'CONFIRMED', N'OPEN')
GROUP BY po.po_id, po.vendor_id, v.vendor_name, po.ship_to_location_id, l.market_id, po.original_promise_date,
         po.current_promise_date, po.po_status"""

SQL_EVENTS = """
SELECT e.event_id, e.market_id, e.event_name, e.start_date, e.end_date, e.categories, e.demand_uplift
FROM erp.regional_event e
WHERE e.end_date >= DATEADD(DAY, -7, CAST(? AS DATE)) AND e.start_date <= DATEADD(DAY, 28, CAST(? AS DATE))"""

# Precedent (CW-05): what owners decided on this style or this market before, and why.
SQL_PRECEDENT = """
SELECT TOP 5 d.decision_id, d.proposal_id, d.style_id, d.market_id, d.decision, d.approved_by_upn, d.rationale,
       JSON_VALUE(d.evidence_json, '$.reason_code') AS reason_code, JSON_VALUE(d.evidence_json, '$.chosen_option_id') AS chosen_option_id,
       TRY_CAST(JSON_VALUE(d.evidence_json, '$.approved_units') AS INT) AS approved_units, d.decided_at_utc
FROM erp.allocation_decision d
WHERE d.style_id = ? OR d.market_id = ?
ORDER BY CASE WHEN d.style_id = ? AND d.market_id = ? THEN 0 WHEN d.style_id = ? THEN 1 ELSE 2 END, d.decided_at_utc DESC"""


def _jsonable(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if hasattr(v, "is_finite") or type(v).__name__ == "Decimal":
        return float(v)
    if type(v).__name__ == "UUID":
        return str(v)
    return v


def _clean(rows: list[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in r.items()} for r in rows]


def _optional(fn) -> list[dict]:
    """Store-level evidence is an extension: if the Gold store mart is not deployed yet the case still works DC-only."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.warning("store evidence unavailable: %s", e)
        return []


def store_view(stores_gold: list[dict], positions: list[dict], transfers_open: list[dict] | None = None) -> list[dict]:
    """Gold store rate and role, with live WMS availability (the gateway re-checks the same WMS number at approval)
    plus units already on the way from approved store-to-store transfers."""
    live = {(p["location_id"], p["sku_id"]): p for p in positions}
    incoming: dict[tuple, int] = {}
    for t in transfers_open or []:
        k = (t["to_location_id"], t["sku_id"])
        incoming[k] = incoming.get(k, 0) + int(t.get("units") or 0)
    out = []
    for g in stores_gold:
        k = (g["location_id"], g["sku_id"])
        p = live.get(k)
        avail = int(p["available_units"]) if p else int(g.get("available_units") or 0)
        out.append(dict(g, available_units=avail + incoming.get(k, 0), incoming_units=incoming.get(k, 0),
                        live=p is not None))
    return out


def event_timing(e: dict, today: str) -> str:
    start, end = str(e.get("start_date") or "")[:10], str(e.get("end_date") or "")[:10]
    if start and start > today:
        return "upcoming"
    if end and end < today:
        return "recent"
    return "ongoing"


def gather(style_id: str, market_id: str, signal_ts: str | None = None) -> dict[str, Any]:
    at = signal_ts or dt.datetime.now(dt.timezone.utc).isoformat()
    jobs = {
        "candidates": lambda: kql.query(KQL_CANDIDATES, p_style=style_id),
        "signal": lambda: kql.query(KQL_SIGNAL, p_style=style_id, p_market=market_id, p_at=at),
        "style": lambda: sql.query(SQL_STYLE, style_id),
        "dc_positions": lambda: sql.query(SQL_DC_POSITIONS, style_id),
        "lanes": lambda: sql.query(SQL_LANES, market_id),
        "guardrail": lambda: sql.query(SQL_GUARDRAIL, style_id, market_id),
        "commitments": lambda: sql.query(SQL_COMMITMENTS, style_id),
        "inbound": lambda: sql.query(SQL_INBOUND, style_id),
        "precedent": lambda: sql.query(SQL_PRECEDENT, style_id, market_id, style_id, market_id, style_id),
        "store_positions": lambda: sql.query(SQL_STORE_POSITIONS, style_id, market_id),
        "store_lanes": lambda: sql.query(SQL_STORE_LANES, market_id),
        "store_transfers_open": lambda: _optional(lambda: sql.query(SQL_STORE_TRANSFERS_OPEN, style_id, market_id)),
        "stores_gold": lambda: _optional(lambda: kql.query(KQL_STORES, p_style=style_id, p_market=market_id)),
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {k: pool.submit(f) for k, f in jobs.items()}
        raw = {k: _clean(f.result()) for k, f in futs.items()}
    # Events are judged against the case date (today), not the Gold as-of date, so past events never read as upcoming.
    today = at[:10]
    raw["events"] = [dict(e, timing=event_timing(e, today)) for e in _clean(sql.query(SQL_EVENTS, today, today))]
    return summarize(style_id, market_id, raw, at)


def summarize(style_id: str, market_id: str, raw: dict, at: str) -> dict[str, Any]:
    cands = raw["candidates"]
    dest = [c for c in cands if c["market_id"] == market_id]
    signal = raw["signal"]
    style = raw["style"][0] if raw["style"] else {"style_id": style_id}
    t1_signal = [s for s in signal if s.get("t1_intraday")]
    t1_batch = [c for c in dest if c.get("t1_stockout_risk")]
    stale = any(c.get("dq_stale_feed") for c in dest)
    spike = any(c.get("recent_spike_7d") for c in dest)
    pers = [float(c["demand_persistence_score"]) for c in dest if c.get("demand_persistence_score") is not None]
    persistence = sum(pers) / len(pers) if pers else 0.5
    commit_all = raw["commitments"]
    resend = [c for c in commit_all if c["is_resend_duplicate"]]
    open_commit = [c for c in commit_all if c["line_status"] == "OPEN" and not c["is_resend_duplicate"]]
    reserved = [c for c in commit_all if c["line_status"] == "RESERVED"]
    dest_inbound = [p for p in raw["inbound"] if p["market_id"] == market_id]
    events = [e for e in raw["events"] if e["market_id"] == market_id or e["market_id"] in
              {c["market_id"] for c in cands if (c.get("transferable_surplus_units") or 0) > 0}]
    order = {"ongoing": 0, "upcoming": 1, "recent": 2}
    events.sort(key=lambda e: (order.get(e.get("timing") or event_timing(e, at[:10]), 3), str(e.get("start_date"))))
    by_market: dict[str, dict] = {}
    for c in cands:
        m = by_market.setdefault(c["market_id"], {"market_id": c["market_id"], "available": 0, "rate": 0.0, "deficit": 0,
                                                  "transferable": 0, "t1_skus": 0, "t2_skus": 0})
        m["available"] += int(c.get("total_available_units") or 0)
        m["rate"] += float(c.get("daily_rate") or 0)
        m["deficit"] += int(c.get("deficit_units") or 0)
        m["transferable"] += int(c.get("transferable_surplus_units") or 0)
        m["t1_skus"] += 1 if c.get("t1_stockout_risk") else 0
        m["t2_skus"] += 1 if c.get("t2_aging_risk") else 0
    for m in by_market.values():
        m["weeks_of_cover"] = round(m["available"] / (m["rate"] * 7), 2) if m["rate"] > 0 else None
    verified = bool(t1_signal or t1_batch)
    sig_ts = max((s["signal_ts"] for s in signal), default=None)
    gold_as_of = cands[0]["as_of_date"][:10] if cands else None
    return {
        "style": style, "market_id": market_id, "at": at, "verified": verified,
        "verification": {
            "intraday_t1_skus": [s["sku_id"] for s in t1_signal], "batch_t1_skus": [c["sku_id"] for c in t1_batch],
            "signal_ts": sig_ts, "gold_as_of": gold_as_of, "stale_feed": stale,
            "rule": "weeks of cover < 1.5 and intent index >= 1.3 (requirements 6.3)"},
        "signals": {"spike": spike, "stale_feed": stale, "persistence": round(persistence, 2),
                    "persistence_ok": persistence >= 0.5,
                    "intent_intraday": max((s.get("intent_used") or 0 for s in signal), default=None),
                    "spike_reasons": sorted({c["spike_reason"] for c in dest if c.get("spike_reason")})},
        "markets": sorted(by_market.values(), key=lambda m: (m["weeks_of_cover"] is None, m["weeks_of_cover"] or 0)),
        "candidates": cands, "signal_rows": signal, "dc_positions": raw["dc_positions"], "lanes": raw["lanes"],
        "stores": store_view(raw.get("stores_gold") or [], raw.get("store_positions") or [],
                             raw.get("store_transfers_open")),
        "store_lanes": raw.get("store_lanes") or [],
        "store_transfers_open": raw.get("store_transfers_open") or [],
        "guardrail": raw["guardrail"][0] if raw["guardrail"] else None,
        "commitments_open": open_commit, "commitments_reserved": reserved, "resend_duplicates": resend,
        "inbound_dest": dest_inbound, "inbound_all": raw["inbound"], "events": events, "precedent": raw["precedent"],
        "sources": [
            {"id": "E1", "system": "Fabric Eventhouse", "object": "kdb_appareliq.rt_inventory_signal", "as_of": sig_ts},
            {"id": "E2", "system": "Fabric Gold (OneLake via Eventhouse shortcut)",
             "object": "gold.mart_rebalancing_candidates, gold.mart_store_rebalancing", "as_of": gold_as_of},
            {"id": "E3", "system": "Azure SQL ERP/WMS (mirrored to Fabric)",
             "object": "wms.inventory_position (DCs and stores), wms.transfer_lane",
             "as_of": max((p["snapshot_ts"] or "" for p in raw["dc_positions"]), default=None)},
            {"id": "E4", "system": "Azure SQL ERP", "object": "erp.wholesale_order_line, erp.purchase_order, erp.margin_guardrail, "
                                                               "erp.regional_event, erp.allocation_decision", "as_of": at[:10]},
        ],
    }


def compact(ev: dict) -> dict:
    """Small JSON for agent.case.evidence_json (the full evidence is re-gathered on demand)."""
    keep = ("market_id", "at", "verified", "verification", "signals", "markets", "guardrail", "sources")
    out = {k: ev[k] for k in keep}
    out["style_id"] = ev["style"].get("style_id")
    out["style_name"] = ev["style"].get("style_name")
    out["sku_id"] = (ev["verification"]["intraday_t1_skus"] or ev["verification"]["batch_t1_skus"] or [None])[0]
    out["resend_duplicates"] = [{k: c[k] for k in ("order_id", "line_no", "sku_id", "open_units", "source_message_id")}
                                for c in ev["resend_duplicates"]]
    out["inbound_dest"] = ev["inbound_dest"][:5]
    out["events"] = ev["events"][:5]
    out["store_transfers_open"] = [{k: t.get(k) for k in ("sto_id", "case_id", "sku_id", "from_location_id",
                                                         "to_location_id", "units")}
                                   for t in ev.get("store_transfers_open") or []][:12]
    out["precedent"] = [{k: p[k] for k in ("proposal_id", "decision", "reason_code", "approved_units", "decided_at_utc")}
                        for p in ev["precedent"][:3]]
    return out
