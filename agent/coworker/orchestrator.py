"""UC1 coworker orchestration: proactive case intake, collaboration, decisions, conversation and follow-ups.

Tiering (requirements §8): Tier 1 actions are reversible and need no approval (open case, gather evidence, stage
PROPOSED drafts, create the chat, pull people in, post cards). Tier 2 actions (ERP/WMS write-back) only happen through
agent.usp_record_decision after a named human clicks Approve/Revise/Override/Defer/Reject; the gateway re-checks
role, scope, stock, guardrail and materiality itself. The LLM never produces a number shown on a card (AG-04).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any

from . import advisor, botconn, cards, evidence, graph, options, settings, sql

log = logging.getLogger(__name__)
AGENT = "agent:uc1-coworker"
ACTIVE = ("OPEN", "AWAITING_APPROVAL", "NEEDS_FINANCE", "DEFERRED")
CHAT_ROLES = ("ACCOUNTABLE", "ESCALATION", "OPERATOR", "PLANNER", "SOURCE_ACCOUNTABLE", "SOURCE_PLANNER",
              "STORE_MANAGER_DEST", "STORE_MANAGER_SOURCE")
ROLE_ASK = {
    "ACCOUNTABLE": "you own this decision (Approve / Revise / Override / Defer / Reject on the card)",
    "ESCALATION": "there is no accountable owner for this category and market, so the decision escalates to you (DQ-12)",
    "OPERATOR": "demo operator: you can take any decision on the card, including Finance co-approval",
    "PLANNER": "please sanity-check the demand signal and the size curve",
    "SOURCE_ACCOUNTABLE": "the stock would come from your market; flag anything that should keep it there",
    "SOURCE_PLANNER": "please confirm the source DC quantities are really spare",
    "STORE_MANAGER_DEST": "your store is about to run out of these sizes; confirm you can receive and sell the extra units",
    "STORE_MANAGER_SOURCE": "your store has more of these sizes than it will sell in 4 weeks; confirm the spare units can be picked and sent",
    "FINANCE": "the GM impact is above materiality, so Finance co-approval is needed (HL-05)",
    "WHOLESALE_LEAD": "open wholesale lines exist in a source market; they are protected, but please confirm",
}


# ---------------------------------------------------------------- helpers
def _j(x: Any) -> str:
    return json.dumps(x, default=str, separators=(",", ":"))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def log_action(action_id: str, case_id: str | None, action_type: str, outcome: str = "DONE", detail: Any = None,
               actor: str = AGENT, tier: int = 1) -> bool:
    """Idempotent on action_id; True the first time (used as a once-only marker for reminders and updates)."""
    row = sql.one("usp_log_action", action_id=action_id[:80], case_id=case_id, tier=tier, action_type=action_type[:32],
                  actor=actor, outcome=outcome, detail_json=_j(detail) if detail is not None else None)
    return bool(row.get("inserted"))


def get_case(case_id: str) -> dict:
    row = sql.one("usp_get_case", case_id=case_id)
    return json.loads(row["case_json"]) if row.get("case_json") else {}


def get_owners(case_id: str) -> list[dict]:
    rows = sql.exec_proc("usp_get_case_owners", case_id=case_id)
    return [{k: (str(v) if k == "aad_object_id" and v is not None else v) for k, v in r.items()} for r in rows]


def personas_for(aad_object_id: str | None) -> list[dict]:
    if not aad_object_id:
        return []
    return sql.query("SELECT person_id, upn, display_name, is_escalation FROM agent.persona_user WHERE aad_object_id = ?",
                     aad_object_id)


def case_for_chat(chat_id: str) -> str | None:
    rows = sql.query("SELECT TOP 1 case_id FROM agent.[case] WHERE chat_id = ? ORDER BY updated_at_utc DESC", chat_id)
    return rows[0]["case_id"] if rows else None


def owner_limit(category: str | None, market_id: str) -> int | None:
    rows = sql.query("SELECT MAX(approval_limit_units) AS lim FROM ref.decision_owner "
                     "WHERE role = N'ACCOUNTABLE' AND category = ? AND market_id = ?", category, market_id)
    lim = rows[0]["lim"] if rows else None
    return int(lim) if lim is not None else 5000  # escalation delegate limit when nobody is accountable (DQ-12)


def strip_mentions(text: str) -> str:
    return re.sub(r"<at>.*?</at>", "", text or "", flags=re.S).replace("&nbsp;", " ").strip()


def card_for(case_id: str, viewer_aad: str | None = None, banner: str | None = None) -> dict:
    case = get_case(case_id)
    owners = get_owners(case_id)
    roles = cards.viewer_roles(owners, viewer_aad) if viewer_aad else set()
    return cards.case_card(case, owners, roles=roles, banner=banner, data_label=settings.get().data_label)


# ---------------------------------------------------------------- simulation (CW-04 / CW-05 / CW-07)
def simulate(ev: dict, constraints: options.Constraints, *, policy_check: dict | None = None) -> dict:
    s = settings.get()
    style = ev["style"]
    ver = ev["verification"]
    alert = set(ver["intraday_t1_skus"]) | set(ver["batch_t1_skus"])
    res = options.build_options(
        market_id=ev["market_id"], candidates=ev["candidates"], dc_positions=ev["dc_positions"], lanes=ev["lanes"],
        guardrail=ev["guardrail"], commitments=ev["commitments_open"], signals=ev["signals"], alert_skus=alert or None,
        constraints=constraints, materiality_gm_usd=s.materiality_gm_usd,
        approval_limit_units=owner_limit(style.get("category"), ev["market_id"]),
        stores=ev.get("stores"), store_lanes=ev.get("store_lanes"))
    rec = options.recommendation_json(res)
    best = next(o for o in res["options"] if o["option_id"] == res["recommended_option_id"])
    commit_markets = {c["market_id"] for c in ev["commitments_open"]}
    rec["wholesale_markets"] = sorted(commit_markets & set(best.get("source_markets") or []))
    rec["signals"] = ev["signals"]
    facts = {"style": {k: style.get(k) for k in ("style_id", "style_name", "category", "lifecycle")},
             "destination_market": ev["market_id"], "inaction": res["inaction"], "signals": ev["signals"],
             "guardrail_max_transfer_cost_pct": (ev["guardrail"] or {}).get("max_transfer_cost_pct"),
             "recommended": {k: best.get(k) for k in ("option_id", "option_label", "units", "projected_gm_usd",
                                                     "transfer_cost_usd", "transfer_cost_pct_of_price", "max_lead_days",
                                                     "late_units", "source_markets", "confidence", "needs_finance",
                                                     "exceeds_owner_limit", "tradeoff")},
             "events": [{k: e.get(k) for k in ("market_id", "event_name", "start_date", "end_date", "timing")}
                        for e in ev["events"][:4]],
             "precedent": [{k: p.get(k) for k in ("decision", "reason_code", "approved_units", "decided_at_utc")}
                           for p in ev["precedent"][:3]],
             "constraints": res["constraints"]}
    ina = res["inaction"]
    fallback = (f"- **Why now:** {ina['stockout_sku_sizes']} SKU-sizes in {ev['market_id']} stock out in about "
                f"{ina['earliest_stockout_days']} days; {ina['lost_units']} units of full-price demand at risk.\n"
                f"- **My recommendation:** {best['option_id']}, {best.get('option_label')}.\n"
                f"- **Watch out:** {best.get('tradeoff')}")
    rec["brief"] = advisor.narrative(facts, fallback)
    if policy_check is None and best["option_id"] != "S0":
        try:
            q = (f"For a {style.get('category')} {style.get('lifecycle')} style, which transfer cost cap, approval routing and "
                 f"materiality rules apply to an inter-market stock transfer into {ev['market_id']}? The proposal moves "
                 f"{best['units']} units at {best.get('transfer_cost_pct_of_price', 0):.1%} of full price with "
                 f"${best.get('projected_gm_usd', 0):,.0f} GM impact.")
            p = advisor.policy(q)
            policy_check = {"text": p["answer"], "citations": p["citations"]}
        except Exception as e:  # noqa: BLE001 - the card still works without the policy check
            log.warning("policy check failed: %s", e)
            policy_check = {"text": "Policy knowledge base unavailable; the gateway still enforces the guardrail.",
                            "citations": []}
    rec["policy_check"] = policy_check or {}
    rec["facts"] = facts
    drafts = [{"option_id": o["option_id"], "lines": options.staged_lines(o)} for o in res["options"] if o["lines"]]
    return {"result": res, "recommendation": rec, "drafts": drafts, "best": best}


# ---------------------------------------------------------------- AA-01: proactive intake from Activator
def handle_t1_event(evt: dict) -> dict:
    style_id, market_id = evt["style_id"], evt["market_id"]
    signal_ts = evt.get("signal_ts")
    existing = sql.query("SELECT case_id, case_status, chat_id, defer_until_utc FROM agent.[case] "
                         "WHERE style_id = ? AND market_id = ? AND case_status IN (N'OPEN', N'AWAITING_APPROVAL', "
                         "N'NEEDS_FINANCE', N'DEFERRED')", style_id, market_id)
    ev = evidence.gather(style_id, market_id, signal_ts)
    if not ev["verified"]:
        log_action(f"nv:{style_id}:{market_id}:{signal_ts or _now():%Y%m%d%H}", None, "AA-00_NOT_VERIFIED", "SKIPPED",
                   {"event": evt, "verification": ev["verification"]})
        return {"status": "not_verified", "verification": ev["verification"]}

    reopen = bool(existing) and existing[0]["case_status"] == "DEFERRED" and existing[0]["defer_until_utc"] is not None \
        and existing[0]["defer_until_utc"].replace(tzinfo=dt.timezone.utc) <= _now()
    if existing and not reopen:
        return _signal_update(existing[0], ev)

    cons = options.Constraints()
    if reopen:
        cons = options.Constraints.from_dict((get_case(existing[0]["case_id"]).get("recommendation") or {}).get("constraints"))
    sim = simulate(ev, cons)
    res, rec, best = sim["result"], sim["recommendation"], sim["best"]
    ina = res["inaction"]
    severity = "HIGH" if ev["verification"]["intraday_t1_skus"] or ina["earliest_stockout_days"] <= 7 else "MEDIUM"
    summary = (f"T1 stock-out risk: {style_id} {ev['style'].get('style_name', '')} in {market_id}; "
               f"{ina['stockout_sku_sizes']} SKU-sizes, {ina['lost_units']} units at risk; recommend {best['option_id']}")
    ev_json = evidence.compact(ev)
    opened = sql.one("usp_open_case", trigger_id="T1", style_id=style_id, market_id=market_id, severity=severity,
                     summary=summary[:1000], evidence_json=_j(ev_json), recommendation_json=_j(rec),
                     recommended_option_id=res["recommended_option_id"], actor=AGENT)
    case_id = opened["case_id"]
    if opened.get("is_new"):
        for d in sim["drafts"]:
            sql.one("usp_stage_transfer_draft", case_id=case_id, option_id=d["option_id"], lines_json=_j(d["lines"]), actor=AGENT)
        open_collaboration(case_id)
        return {"status": "opened", "case_id": case_id, "recommended": best["option_id"]}
    if opened.get("reopened"):
        sql.one("usp_update_recommendation", case_id=case_id, recommendation_json=_j(rec),
                recommended_option_id=res["recommended_option_id"], drafts_json=_j(sim["drafts"]), actor=AGENT,
                action_type="HL-04_REOPEN_RESIM", detail_json=_j({"trigger": "T1 re-fired after deferral"}),
                evidence_json=_j(ev_json))
        _repost_card(case_id, opened.get("chat_id"), "Deferral expired and the T1 trigger fired again: re-opened with fresh "
                     "evidence and re-simulated options.", mention_roles={"ACCOUNTABLE", "ESCALATION"})
        return {"status": "reopened", "case_id": case_id, "recommended": best["option_id"]}
    return _signal_update(sql.query("SELECT case_id, case_status, chat_id FROM agent.[case] WHERE case_id = ?", case_id)[0], ev)


def _signal_update(case_row: dict, ev: dict) -> dict:
    """CW-03: a repeat signal does not open a second case; the chat gets at most one update per hour."""
    case_id = case_row["case_id"]
    ver = ev["verification"]
    dest = next((m for m in ev["markets"] if m["market_id"] == ev["market_id"]), {})
    marker = f"sig:{case_id}:{_now():%Y%m%d%H}"
    first = log_action(marker, case_id, "CW-03_SIGNAL_UPDATE", "DONE",
                       {"signal_ts": ver["signal_ts"], "intraday_t1_skus": len(ver["intraday_t1_skus"]),
                        "weeks_of_cover": dest.get("weeks_of_cover")})
    posted = False
    if first and case_row.get("chat_id"):
        try:
            botconn.send(case_row["chat_id"], text=(
                f"New signal for this case [E1]: {len(ver['intraday_t1_skus'])} SKU-sizes still meet T1 at {ver['signal_ts']}; "
                f"{ev['market_id']} weeks of cover {dest.get('weeks_of_cover', 'n/a')}. Status: "
                f"{case_row['case_status'].replace('_', ' ').lower()}. The options on the card are still current."))
            posted = True
            refresh_shared_card(case_id)
        except botconn.NotInChat:
            log_action(f"{marker}:pending", case_id, "CW-03_SIGNAL_UPDATE", "SKIPPED",
                       {"reason": "agent not yet installed in the case chat; the intro and card are posted on install"})
    return {"status": "dedup", "case_id": case_id, "posted": posted}


# ---------------------------------------------------------------- AA-02 / CW-02: pull the right people in
def _wholesale_leads(case: dict) -> list[dict]:
    mkts = (case.get("recommendation") or {}).get("wholesale_markets") or []
    if not mkts:
        return []
    rows = sql.query("SELECT o.person_id, o.market_id, p.upn, CONVERT(NVARCHAR(36), p.aad_object_id) AS aad_object_id, "
                     "COALESCE(rp.display_name, p.display_name) AS persona_name, rp.title, p.display_name AS user_display_name "
                     "FROM ref.decision_owner o JOIN agent.persona_user p ON p.person_id = o.person_id "
                     "LEFT JOIN ref.person rp ON rp.person_id = o.person_id "
                     "WHERE o.role = N'WHOLESALE_LEAD' AND o.category = ?", case.get("category"))
    return [{**r, "case_role": "WHOLESALE_LEAD"} for r in rows if r["market_id"] in mkts]


def wanted_members(case: dict, owners: list[dict]) -> list[dict]:
    rec = case.get("recommendation") or {}
    best = next((o for o in rec.get("options") or [] if o["option_id"] == case.get("recommended_option_id")), {})
    roles = set(CHAT_ROLES)
    if best.get("needs_finance") or case.get("case_status") == "NEEDS_FINANCE":
        roles.add("FINANCE")
    return [o for o in owners if o["case_role"] in roles and o.get("aad_object_id")] + _wholesale_leads(case)


def _store_ask(p: dict, case: dict | None) -> str | None:
    """Exact pick / receive instruction for a store manager, naming both stores, sizes and units."""
    loc = p.get("location_id")
    if not (case and loc and p["case_role"] in ("STORE_MANAGER_SOURCE", "STORE_MANAGER_DEST")):
        return None
    rec_id = case.get("recommended_option_id") or (case.get("recommendation") or {}).get("recommended_option_id")
    moves = cards.store_moves(case, rec_id)
    if p["case_role"] == "STORE_MANAGER_SOURCE":
        mine = [m for m in moves if m["from_id"] == loc]
        if mine:
            return (f"your store {mine[0]['from']} sells these sizes slowly. Please confirm you can pick and send "
                    + "; ".join(f"{m['size_text']} to {m['to']}" for m in mine)
                    + " (truck collects; you keep 4 weeks of sales plus display stock)")
    else:
        mine = [m for m in moves if m["to_id"] == loc]
        if mine:
            return (f"your store {mine[0]['to']} is about to run out. Please confirm you can receive "
                    + "; ".join(f"{m['size_text']} from {m['from']} (arrives in {m['lead_days']} days)" for m in mine)
                    + " and put them on the floor the same day")
    return None


def _asks(people: list[dict], case: dict | None = None) -> tuple[str, list[dict]]:
    """One line per real user (several demo personas may map to one account), each with an @mention and the ask."""
    by_aad: dict[str, list[dict]] = {}
    for p in people:
        by_aad.setdefault(p["aad_object_id"].lower(), []).append(p)
    lines, ents = [], []
    for aad, ps in by_aad.items():
        txt, ent = botconn.mention(aad, ps[0].get("user_display_name") or ps[0].get("persona_name") or "teammate")
        ents.append(ent)
        asks = "; ".join(f"as {p.get('persona_name') or p['person_id']} ({p['case_role'].replace('_', ' ').lower()}): "
                         f"{_store_ask(p, case) or ROLE_ASK.get(p['case_role'], 'FYI')}" for p in ps)
        lines.append(f"- {txt} {asks}")
    return "\n".join(lines), ents


def _intro(people: list[dict], case: dict) -> tuple[str, list[dict]]:
    """Plain-language opening message: the problem, the suggestion, and an @mention with a clear ask per person."""
    lines, ents = _asks(people, case)
    rec = case.get("recommendation") or {}
    ina = rec.get("inaction") or {}
    rec_id = case.get("recommended_option_id") or rec.get("recommended_option_id")
    best = next((o for o in rec.get("options") or [] if o["option_id"] == rec_id), {})
    style = f"{(case.get('evidence') or {}).get('style_name') or ''} ({case['style_id']})".strip()
    head = f"👋 I opened case **{case['case_id']}** and need your help with a stock-out risk."
    if ina:
        days = ina.get("earliest_stockout_days")
        head += (f"\n\n**The problem:** {style} is selling faster than {case['market_id']} can keep in stock. "
                 + (f"The first size sells out in about {days:.0f} days and " if isinstance(days, (int, float)) and days < 900 else "")
                 + f"{ina.get('stockout_sku_sizes')} sizes are short. Doing nothing loses about {ina.get('lost_units')} units "
                 f"of full-price sales (${float(ina.get('lost_full_price_sales_usd') or 0):,.0f}).")
        if best and rec_id != "S0":
            s_units = int(best.get("store_units") or 0)
            far = [m for m in best.get("source_markets") or [] if m != case["market_id"]]
            if s_units and far:
                head += (f"\n\n**My suggestion ({rec_id}):** right away, move {s_units} spare units by truck from "
                         f"{len(best.get('donor_stores') or [])} slow-selling {case['market_id']} store(s) to "
                         f"{len(best.get('receiving_stores') or [])} store(s) about to run out "
                         f"({best.get('store_lead_days')} days), and refill the market with "
                         f"{best.get('units', 0) - s_units} units from {', '.join(far)} (within "
                         f"{best.get('max_lead_days')} days). Shipping costs ${float(best.get('transfer_cost_usd') or 0):,.0f}.")
            elif s_units:
                head += (f"\n\n**My suggestion ({rec_id}):** move {s_units} spare units by truck between "
                         f"{case['market_id']} stores, arriving within {best.get('max_lead_days')} days, for "
                         f"${float(best.get('transfer_cost_usd') or 0):,.0f}.")
            else:
                head += (f"\n\n**My suggestion ({rec_id}):** ship {best.get('units')} spare units from "
                         f"{', '.join(best.get('source_markets') or [])} to {case['market_id']} for "
                         f"${float(best.get('transfer_cost_usd') or 0):,.0f} in shipping, arriving within "
                         f"{best.get('max_lead_days')} days.")
            moves = cards.store_moves(case, rec_id)
            if moves:
                head += "\n\n**Store-to-store moves (from → to):**\n" + "\n".join(
                    f"{i}. {m['from']} → {m['to']}: {m['size_text']} ({m['units']} units), arrives in {m['lead_days']} days"
                    for i, m in enumerate(moves[:8], 1))
        elif best:
            head += "\n\n**My suggestion:** hold for now (S0); no transfer is worth the cost."
    head += ("\n\nI checked this against the live signal and the Gold data and prepared the options as drafts only. "
             "**Nothing ships until the accountable owner approves.** The card below explains the details and the choices. "
             "What I need from each of you:")
    return head + "\n\n" + lines + "\n\nAsk me anything here, or tell me a constraint and I'll re-simulate.", ents


def open_collaboration(case_id: str) -> dict:
    s = settings.get()
    case = get_case(case_id)
    owners = get_owners(case_id)
    people = wanted_members(case, owners)
    aads = list(dict.fromkeys(p["aad_object_id"].lower() for p in people))
    if len(aads) < 3 and s.fallback_upn:
        fb = graph.resolve_user(s.fallback_upn)
        if fb["id"].lower() not in aads:
            aads.append(fb["id"].lower())
    style_name = (case.get("evidence") or {}).get("style_name") or ""
    srcs = ",".join(next((o.get("source_markets") or [] for o in (case.get("recommendation") or {}).get("options") or []
                          if o["option_id"] == case.get("recommended_option_id")), []))
    topic = f"UC1 · {case['style_id']} {style_name} · {case['market_id']}" + (f" ← {srcs}" if srcs else "") + " (SYNTHETIC)"
    chat_id = case.get("chat_id")
    if not chat_id:
        chat_id = graph.create_group_chat(aads, topic)
        sql.one("usp_set_case_chat", case_id=case_id, chat_id=chat_id, card_activity_id=None)
    install = graph.install_app(chat_id, s.teams_app_id) if s.teams_app_id else "skipped"
    for p in people:
        sql.one("usp_add_case_member", case_id=case_id, person_id=p["person_id"], upn=p["upn"], case_role=p["case_role"])
    text, ents = _intro(people, case)
    botconn.send(chat_id, text=text, mentions=ents)
    card = card_for(case_id)
    activity_id = botconn.send(chat_id, card=card)
    sql.one("usp_set_case_chat", case_id=case_id, chat_id=chat_id, card_activity_id=activity_id)
    post_charts(case_id, chat_id, card)
    log_action(f"collab:{case_id}", case_id, "AA-02_COLLABORATE", "DONE",
               {"chat_id": chat_id, "install": install, "members": [(p["person_id"], p["case_role"]) for p in people]})
    return {"chat_id": chat_id, "card_activity_id": activity_id, "members": len(aads)}


def sync_members(case_id: str, reason: str) -> list[str]:
    """CW-06: after a re-simulation or routing change, pull in anyone newly needed (new source market, Finance)."""
    case = get_case(case_id)
    chat_id = case.get("chat_id")
    if not chat_id:
        return []
    have = {(m["person_id"], m["case_role"]) for m in case.get("members") or []}
    new = [p for p in wanted_members(case, get_owners(case_id)) if (p["person_id"], p["case_role"]) not in have]
    if not new:
        return []
    added = []
    for p in new:
        if graph.add_member(chat_id, p["aad_object_id"]):
            sql.one("usp_add_case_member", case_id=case_id, person_id=p["person_id"], upn=p["upn"], case_role=p["case_role"])
            added.append(p)
    if added:
        text, ents = _asks(added, case)
        botconn.send(chat_id, text=f"{reason} I've added:\n\n" + text, mentions=ents)
        log_action(f"sync:{case_id}:{_now():%Y%m%d%H%M%S}", case_id, "CW-06_ADD_MEMBER", "DONE",
                   {"reason": reason, "added": [(p["person_id"], p["case_role"]) for p in added]})
    return [p["person_id"] for p in added]


def _repost_card(case_id: str, chat_id: str | None, banner: str, mention_roles: set[str] | None = None) -> None:
    chat_id = chat_id or get_case(case_id).get("chat_id")
    if not chat_id:
        return
    if mention_roles:
        ps = [o for o in get_owners(case_id) if o["case_role"] in mention_roles and o.get("aad_object_id")]
        if ps:
            text, ents = _asks(ps)
            botconn.send(chat_id, text=banner + "\n\n" + text, mentions=ents)
    card = card_for(case_id, banner=banner)
    activity_id = botconn.send(chat_id, card=card)
    sql.one("usp_set_case_chat", case_id=case_id, chat_id=chat_id, card_activity_id=activity_id)
    post_charts(case_id, chat_id, card)


def post_charts(case_id: str, chat_id: str | None, sent_card: dict) -> None:
    """Charts normally sit inside the case card; only a case too big to carry them gets a separate charts card."""
    if cards.has_charts(sent_card):
        return
    try:
        case = get_case(case_id)
        chat_id = chat_id or case.get("chat_id")
        card = cards.charts_card(case, settings.get().data_label)
        if chat_id and card:
            botconn.send(chat_id, card=card)
    except Exception as e:  # noqa: BLE001 - charts are a convenience; the case card carries the same numbers
        log.warning("charts card failed for %s: %s", case_id, e)


def refresh_shared_card(case_id: str, banner: str | None = None) -> None:
    case = get_case(case_id)
    if case.get("chat_id") and case.get("card_activity_id"):
        try:
            card = cards.case_card(case, get_owners(case_id), banner=banner, data_label=settings.get().data_label)
            botconn.update_card(case["chat_id"], case["card_activity_id"], card)
        except Exception as e:  # noqa: BLE001 - a stale card is refreshed by the next action
            log.warning("card update failed for %s: %s", case_id, e)


# ---------------------------------------------------------------- Tier 2: human decisions through the gateway
def _decision_args(verb: str, data: dict) -> dict:
    if verb == "approve":
        return {"decision": "APPROVE", "option_id": data.get("option_id")}
    if verb == "revise":
        return {"decision": "REVISE", "option_id": data.get("option_id"), "units": int(float(data.get("rev_units") or 0)),
                "rationale": (data.get("rev_rationale") or "").strip()}
    if verb == "override":
        return {"decision": "OVERRIDE", "option_id": data.get("ovr_option"), "reason_code": data.get("ovr_reason"),
                "rationale": (data.get("ovr_rationale") or "").strip()}
    if verb == "defer":
        d = (data.get("def_date") or "").strip()
        return {"decision": "DEFER", "defer_until_utc": f"{d}T12:00:00" if d else None}
    if verb == "reject":
        return {"decision": "REJECT", "rationale": (data.get("rej_rationale") or "").strip() or None}
    raise ValueError(verb)


def _plan_text(case: dict, who: str, args: dict) -> list[str]:
    status = case.get("case_status")
    staged: dict[str, int] = {}
    for d in case.get("drafts") or []:
        if d.get("draft_status") == "PROPOSED":
            staged[d["option_id"]] = staged.get(d["option_id"], 0) + int(d["units"])
    opt = args.get("option_id") or case.get("pending_option_id") or case.get("recommended_option_id")
    dec = args["decision"]
    if status == "NEEDS_FINANCE" and dec == "APPROVE":
        return [f"{who} is co-approving {case.get('pending_option_id')} ({case.get('pending_units')} units) as Finance.",
                "I'll ask the gateway to re-check stock and the guardrail, then create the stock transfer order in ERP/WMS."]
    if dec in ("APPROVE", "REVISE", "OVERRIDE"):
        units = args.get("units") or staged.get(opt, 0)
        return [f"{who} chose {dec.lower()} → option {opt}, {units} units" +
                (f", reason {args['reason_code']}" if args.get("reason_code") else "") + ".",
                "Gateway checks: your approval role and limit, staged scope, source DC stock, margin guardrail, materiality.",
                "If all pass I write the decision and the STO to ERP/WMS and reserve the stock; above materiality I route to Finance."]
    if dec == "DEFER":
        return [f"{who} is deferring until {args.get('defer_until_utc')} UTC. I'll re-open and re-simulate then."]
    return [f"{who} is rejecting the recommendation. I'll record it in ERP so the next case can learn from it."]


def handle_decision(job: dict) -> dict:
    s = settings.get()
    verb, data = job["verb"], job.get("data") or {}
    case_id = data.get("case_id")
    who_aad = (job.get("from") or {}).get("aadObjectId")
    who_name = (job.get("from") or {}).get("name") or "Someone"
    chat_id = job.get("conversation_id")
    ps = personas_for(who_aad)
    case = get_case(case_id)
    if not case:
        return {"outcome": "BLOCKED", "message": "case not found"}
    chat_id = chat_id or case.get("chat_id")
    if not ps:
        botconn.send(chat_id, card=cards.status_card(
            "Not recorded", [f"{who_name} is not mapped to a Kinetiq persona, so the gateway cannot record a decision. "
                             "You can still ask questions or add constraints."], "attention", s.data_label))
        return {"outcome": "BLOCKED", "message": "unmapped user"}
    try:
        args = _decision_args(verb, data)
    except (ValueError, TypeError) as e:
        botconn.send(chat_id, text=f"I couldn't read that input ({e}); nothing was recorded.")
        return {"outcome": "BLOCKED", "message": str(e)}
    botconn.send(chat_id, card=cards.status_card("Recording decision (Tier 2, gateway-checked)",
                                                 _plan_text(case, who_name, args), "accent", s.data_label))
    aid = cards.action_id(case_id, verb, who_aad, {k: v for k, v in data.items() if k != "case_id"})
    out = sql.one("usp_record_decision", action_id=aid, case_id=case_id, approver_upn=ps[0]["upn"],
                  materiality_gm_usd=s.materiality_gm_usd, **{k: v for k, v in args.items() if v is not None})
    outcome = out.get("outcome", "BLOCKED")
    lines = [out.get("message") or ""]
    if out.get("decision_id"):
        lines.append(f"Decision {out['decision_id']} in erp.allocation_decision [E4].")
    if out.get("sto_id"):
        lines.append(f"Stock transfer order {out['sto_id']}: {out.get('approved_units')} units, GM impact "
                     f"${float(out.get('gm_usd') or 0):,.0f}, transfer cost ${float(out.get('transfer_cost_usd') or 0):,.0f}.")
        lines.append("I'll check sell-through and cover at T+7 and T+14 and report back here.")
    style = {"EXECUTED": "good", "REJECTED": "attention", "BLOCKED": "attention", "PENDING_FINANCE": "warning"}.get(outcome, "default")
    title = {"EXECUTED": "Execution complete", "PENDING_FINANCE": "Waiting for Finance co-approval",
             "DEFERRED": "Deferred", "REJECTED": "Rejected and recorded", "BLOCKED": "Blocked by the gateway",
             "DUPLICATE": "Already recorded"}.get(outcome, outcome)
    botconn.send(chat_id, card=cards.status_card(title, lines, style, s.data_label))
    if outcome == "PENDING_FINANCE":
        added = sync_members(case_id, "Finance co-approval is needed (HL-05).")
        if not added:
            fin = [o for o in get_owners(case_id) if o["case_role"] == "FINANCE" and o.get("aad_object_id")]
            if fin:
                text, ents = _asks(fin)
                botconn.send(chat_id, text="Finance, over to you:\n\n" + text,
                             mentions=ents)
    banner = {"EXECUTED": f"Approved and executed by {who_name}" + (f" · {out['sto_id']}" if out.get("sto_id") else ""),
              "PENDING_FINANCE": f"Owner approval by {who_name}; waiting for Finance",
              "DEFERRED": f"Deferred by {who_name}", "REJECTED": f"Rejected by {who_name}"}.get(outcome)
    if outcome != "BLOCKED":
        refresh_shared_card(case_id, banner)
    return {k: (str(v) if v is not None else None) for k, v in out.items()}


# ---------------------------------------------------------------- CW-07 / CW-08: conversation
def _case_facts(case: dict) -> dict:
    rec = case.get("recommendation") or {}
    ev = case.get("evidence") or {}
    return {"case_id": case.get("case_id"), "status": case.get("case_status"), "style": case.get("style_id"),
            "style_name": ev.get("style_name"), "market": case.get("market_id"), "recommended": case.get("recommended_option_id"),
            "options": rec.get("options"), "inaction": rec.get("inaction"), "constraints": rec.get("constraints"),
            "markets": ev.get("markets"), "signals": ev.get("signals"), "events": ev.get("events"),
            "inbound_dest": ev.get("inbound_dest"), "precedent": ev.get("precedent"),
            "resend_duplicates": ev.get("resend_duplicates")}


def apply_constraint(case_id: str, new: dict, who_name: str, who_upn: str | None) -> str:
    case = get_case(case_id)
    if case.get("case_status") not in ("OPEN", "AWAITING_APPROVAL", "DEFERRED"):
        return (f"The case is {case.get('case_status', '').replace('_', ' ').lower()}, so I can't re-simulate now. "
                "If the owner withdraws (Reject) I'll start fresh on the next signal.")
    rec_old = case.get("recommendation") or {}
    note = new.pop("note", None) or ", ".join(f"{k}={v}" for k, v in new.items())
    cons = options.Constraints.from_dict(rec_old.get("constraints")).merge(
        options.Constraints.from_dict({**new, "notes": [f"{who_name}: {note}"]}))
    at = (case.get("evidence") or {}).get("at")
    ev = evidence.gather(case["style_id"], case["market_id"], at)
    sim = simulate(ev, cons, policy_check=rec_old.get("policy_check"))
    best = sim["best"]
    sql.one("usp_update_recommendation", case_id=case_id, recommendation_json=_j(sim["recommendation"]),
            recommended_option_id=best["option_id"], drafts_json=_j(sim["drafts"]), actor=who_upn or who_name,
            action_type="CW-07_RESIMULATE",
            detail_json=_j({"requested_by": who_name, "constraint": new, "note": note,
                            "previous_option": case.get("recommended_option_id"), "new_option": best["option_id"]}),
            evidence_json=_j(evidence.compact(ev)))
    changed = best["option_id"] != case.get("recommended_option_id")
    old = next((o for o in rec_old.get("options") or [] if o["option_id"] == case.get("recommended_option_id")), {})
    msg = (f"Re-simulated with **{note}** (from {who_name}). "
           + (f"Recommendation changes {case.get('recommended_option_id')} → **{best['option_id']}**: "
              if changed else f"Still **{best['option_id']}**: ")
           + f"{best['units']} units ({old.get('units', 'n/a')} before), GM impact ${best.get('projected_gm_usd', 0):,.0f}, "
             f"transfer cost {best.get('transfer_cost_pct_of_price', 0):.1%} of price. {best.get('tradeoff', '')}")
    if best["option_id"] == "S0":
        msg += " No feasible transfer remains under these constraints, so I recommend maintaining allocation."
    refresh_shared_card(case_id, f"Updated after a constraint from {who_name}")
    sync_members(case_id, "The re-simulated plan involves new people.")
    return msg


def _policy_reply(question: str) -> str:
    p = advisor.policy(question)
    cites = sorted({c["doc"] for c in p["citations"] if c.get("doc")})
    return p["answer"] + (f"\n\n_Sources: {', '.join(cites)}_" if cites else "")


def _data_reply(question: str, case: dict | None) -> str:
    q = question
    if case:
        q = f"{question} (context: style {case['style_id']}, destination market {case['market_id']})"
    return advisor.data_agent(q)["answer"] + "\n\n_Source: Fabric data agent over the Kinetiq ontology (SYNTHETIC)._"


def handle_message(job: dict) -> dict:
    text = strip_mentions(job.get("text") or "")
    chat_id = job["conversation_id"]
    who = job.get("from") or {}
    who_name = who.get("name") or "teammate"
    if not text:
        return {"status": "empty"}
    case_id = job.get("case_id") or case_for_chat(chat_id)
    case = get_case(case_id) if case_id else None
    facts = _case_facts(case) if case else {}
    intent = advisor.classify(text, facts)
    kind = intent.get("intent", "case")
    question = intent.get("question") or text
    botconn.typing(chat_id)
    try:
        if kind == "constraint" and case:
            ps = personas_for(who.get("aadObjectId"))
            reply = apply_constraint(case_id, dict(intent.get("constraints") or {}), who_name, ps[0]["upn"] if ps else None)
        elif kind == "policy":
            reply = _policy_reply(question)
        elif kind == "data":
            botconn.send(chat_id, text="Checking the Fabric data agent; this takes up to a minute…")
            reply = _data_reply(question, case)
        elif case:
            reply = advisor.answer_case(question, facts)
        else:
            reply = ("I'm the Kinetiq rebalancing coworker (SYNTHETIC demo). I open a case chat when a style is about to "
                     "stock out in one market while another has spare stock. Ask me about policies (e.g. 'what is the "
                     "transfer cost cap for launch styles?') or data (e.g. 'weeks of cover for HM9443 by market').")
    except Exception as e:  # noqa: BLE001
        log.exception("message handling failed")
        reply = f"Sorry, I couldn't complete that ({type(e).__name__}). Nothing was changed."
    botconn.send(chat_id, text=reply, reply_to=job.get("reply_to_id"))
    if case_id:
        log_action(f"msg:{job.get('activity_id') or _now().timestamp()}", case_id, f"CW-08_{kind.upper()}"[:32], "DONE",
                   {"from": who_name, "intent": kind})
    return {"status": "answered", "intent": kind}


def handle_card_action(job: dict) -> dict:
    """Queued Action.Execute verbs other than refresh."""
    verb, data = job["verb"], job.get("data") or {}
    who = job.get("from") or {}
    if verb == "ask":
        return handle_message({**job, "text": data.get("ask_text", ""), "case_id": data.get("case_id")})
    if verb == "comment":
        case_id = data.get("case_id")
        text = data.get("cmt_text", "")
        intent = advisor.classify(text, _case_facts(get_case(case_id)))
        cons = dict(intent.get("constraints") or {})
        if not cons:
            return handle_message({**job, "text": text, "case_id": case_id})
        ps = personas_for(who.get("aadObjectId"))
        cons.setdefault("note", text[:200])
        msg = apply_constraint(case_id, cons, who.get("name") or "teammate", ps[0]["upn"] if ps else None)
        botconn.send(job["conversation_id"], text=msg)
        return {"status": "resimulated"}
    return handle_decision(job)


def refresh_response(case_id: str, viewer_aad: str | None) -> dict:
    """Synchronous answer to the card's refresh invoke: the role-specific view for this viewer."""
    return card_for(case_id, viewer_aad)


# ---------------------------------------------------------------- AA-05 / HL-04 / SLA: timer
def _follow_up(row: dict) -> None:
    case_id, kind = row["case_id"], row["task_type"]
    case = get_case(case_id)
    if kind == "REOPEN_DEFERRED":
        rec_old = case.get("recommendation") or {}
        ev = evidence.gather(case["style_id"], case["market_id"], (case.get("evidence") or {}).get("at"))
        sim = simulate(ev, options.Constraints.from_dict(rec_old.get("constraints")))
        sql.one("usp_update_recommendation", case_id=case_id, recommendation_json=_j(sim["recommendation"]),
                recommended_option_id=sim["best"]["option_id"], drafts_json=_j(sim["drafts"]), actor=AGENT,
                action_type="HL-04_REOPEN_RESIM", detail_json=_j({"follow_up_id": row["follow_up_id"]}),
                evidence_json=_j(evidence.compact(ev)))
        _repost_card(case_id, case.get("chat_id"), "The deferral expired: I re-checked the evidence and re-simulated.",
                     mention_roles={"ACCOUNTABLE", "ESCALATION"})
        return
    ev = evidence.gather(case["style_id"], case["market_id"], None)
    dest = next((m for m in ev["markets"] if m["market_id"] == case["market_id"]), {})
    before = next((m for m in (case.get("evidence") or {}).get("markets") or [] if m["market_id"] == case["market_id"]), {})
    sto = sql.query("SELECT sto_status, SUM(units) AS units FROM erp.stock_transfer_order WHERE decision_id = ? GROUP BY sto_status",
                    case.get("decision_id")) if case.get("decision_id") else []
    sto_txt = ", ".join(f"{r['units']} units {r['sto_status'].lower()}" for r in sto) or "no transfer (maintain/reject)"
    label = "T+7" if kind == "CHECK_T7" else "T+14"
    msg = (f"**{label} outcome check** for {case_id}: {case['market_id']} weeks of cover now "
           f"{dest.get('weeks_of_cover', 'n/a')} (was {before.get('weeks_of_cover', 'n/a')} when the case opened) [E2]; "
           f"transfer: {sto_txt} [E4]. "
           + ("Cover is back above the 1.5-week trigger." if (dest.get("weeks_of_cover") or 0) >= 1.5
              else "Cover is still below the trigger; I'll open a new case if T1 fires again."))
    log_action(f"fu:{row['follow_up_id']}", case_id, f"AA-05_{kind}", "DONE",
               {"weeks_of_cover_now": dest.get("weeks_of_cover"), "weeks_of_cover_before": before.get("weeks_of_cover"),
                "sto": sto})
    if case.get("chat_id"):
        botconn.send(case["chat_id"], text=msg)


def _sla() -> int:
    s = settings.get()
    rows = sql.query("SELECT case_id, case_status, chat_id, DATEDIFF(MINUTE, updated_at_utc, SYSUTCDATETIME()) AS waited_min "
                     "FROM agent.[case] WHERE case_status IN (N'AWAITING_APPROVAL', N'NEEDS_FINANCE') AND chat_id IS NOT NULL "
                     "AND card_activity_id IS NOT NULL")
    n = 0
    for r in rows:
        hours = r["waited_min"] / 60
        roles = {"FINANCE"} if r["case_status"] == "NEEDS_FINANCE" else {"ACCOUNTABLE", "ESCALATION"}
        if hours >= 2 * s.approval_sla_hours and log_action(f"sla2:{r['case_id']}:{r['case_status']}", r["case_id"],
                                                            "AA-06_SLA_ESCALATE", "DONE", {"waited_min": r["waited_min"]}):
            esc = sql.query("SELECT TOP 1 person_id, upn, CONVERT(NVARCHAR(36), aad_object_id) AS aad_object_id, display_name "
                            "AS user_display_name, display_name AS persona_name FROM agent.persona_user WHERE is_escalation = 1")
            if esc:
                e = {**esc[0], "case_role": "ESCALATION"}
                graph.add_member(r["chat_id"], e["aad_object_id"])
                sql.one("usp_add_case_member", case_id=r["case_id"], person_id=e["person_id"], upn=e["upn"], case_role="ESCALATION")
                text, ents = botconn.mention(e["aad_object_id"], e["user_display_name"] or "escalation")
                botconn.send(r["chat_id"], mentions=[ents], text=(
                    f"{text} this case has waited {hours:.0f} h for a decision (SLA {s.approval_sla_hours:.0f} h, escalation at "
                    f"{2 * s.approval_sla_hours:.0f} h). Stock-out risk keeps growing; could you help unblock it?"))
                n += 1
        elif hours >= s.approval_sla_hours and log_action(f"sla1:{r['case_id']}:{r['case_status']}", r["case_id"],
                                                          "AA-06_SLA_REMIND", "DONE", {"waited_min": r["waited_min"]}):
            ps = [o for o in get_owners(r["case_id"]) if o["case_role"] in roles and o.get("aad_object_id")]
            if ps:
                text, ents = _asks(ps)
                botconn.send(r["chat_id"], mentions=ents, text=(
                    f"Reminder: waiting {hours:.1f} h for a decision (SLA {s.approval_sla_hours:.0f} h).\n\n"
                    + text))
                n += 1
    return n


def _resume() -> int:
    """Recover cases whose collaboration step failed half-way (chat not created, or card not posted)."""
    rows = sql.query("SELECT case_id FROM agent.[case] WHERE case_status IN (N'OPEN', N'AWAITING_APPROVAL') "
                     "AND card_activity_id IS NULL AND opened_at_utc < DATEADD(MINUTE, -5, SYSUTCDATETIME())")
    for r in rows:
        try:
            open_collaboration(r["case_id"])
        except Exception:  # noqa: BLE001
            log.exception("resume failed for %s", r["case_id"])
    return len(rows)


def _sync_open_cases() -> int:
    """Owner/persona changes reach chats that are already open: add newly needed people and re-post their card."""
    rows = sql.query("SELECT case_id, chat_id FROM agent.[case] WHERE case_status IN (N'OPEN', N'AWAITING_APPROVAL', "
                     "N'NEEDS_FINANCE', N'DEFERRED') AND chat_id IS NOT NULL AND card_activity_id IS NOT NULL")
    n = 0
    for r in rows:
        if sync_members(r["case_id"], "Decision rights changed."):
            _repost_card(r["case_id"], r["chat_id"], "Updated card for the people just added")
            n += 1
    return n


def tick() -> dict:
    out = {"follow_ups": 0, "sla": 0, "resumed": 0, "synced": 0, "errors": 0}
    for row in sql.exec_proc("usp_claim_due_follow_ups", max_rows=20):
        try:
            _follow_up(row)
            out["follow_ups"] += 1
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            log.exception("follow-up %s failed", row.get("follow_up_id"))
    for name, fn in (("sla", _sla), ("resumed", _resume), ("synced", _sync_open_cases)):
        try:
            out[name] = fn()
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            log.exception("%s failed", name)
    return out


def dispatch(job: dict) -> dict:
    kind = job.get("kind")
    if kind == "t1_event":
        return handle_t1_event(job["event"])
    if kind == "message":
        return handle_message(job)
    if kind == "card_action":
        return handle_card_action(job)
    if kind == "open_collaboration":
        return open_collaboration(job["case_id"])
    if kind == "refresh_card":  # operator: rebuild an open case card in place (e.g. after a card layout change)
        refresh_shared_card(job["case_id"], job.get("banner"))
        return {"refreshed": job["case_id"]}
    raise ValueError(f"unknown job kind {kind}")
