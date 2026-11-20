"""Adaptive Card role views, idempotency keys and the AG-04 invented-number check."""
from coworker import advisor, cards

OWNERS = [
    {"case_role": "ACCOUNTABLE", "person_id": "dortiz", "aad_object_id": "AAA", "persona_name": "D. Ortiz"},
    {"case_role": "FINANCE", "person_id": "fbrandt", "aad_object_id": "fff", "persona_name": "F. Brandt"},
    {"case_role": "PLANNER", "person_id": "gkim", "aad_object_id": "ggg", "persona_name": "G. Kim"},
]
CASE = {"case_id": "C-1", "style_id": "HM9443", "market_id": "US", "case_status": "AWAITING_APPROVAL",
        "recommended_option_id": "S2",
        "recommendation": {"options": [{"option_id": "S0", "option_label": "Do nothing"},
                                       {"option_id": "S2", "option_label": "Fast", "units": 260}]},
        "drafts": [{"option_id": "S2", "draft_status": "PROPOSED", "units": 260}]}


def verbs(card):
    return {a.get("verb") or a["card"]["actions"][0]["verb"] for a in card["actions"]}


def test_owner_sees_decision_actions():
    card = cards.case_card(dict(CASE), OWNERS, roles=cards.viewer_roles(OWNERS, "aaa"))
    assert {"approve", "revise", "override", "defer", "reject"} <= verbs(card)


def test_planner_and_unknown_viewer_cannot_decide():
    for roles in (cards.viewer_roles(OWNERS, "ggg"), set()):
        assert not {"approve", "revise", "override"} & verbs(cards.case_card(dict(CASE), OWNERS, roles=roles))


def test_finance_only_co_approves_when_case_needs_finance():
    case = dict(CASE, case_status="NEEDS_FINANCE", pending_option_id="S2", pending_units=260)
    assert {"approve", "reject"} <= verbs(cards.case_card(case, OWNERS, roles={"FINANCE"}))
    assert "approve" not in verbs(cards.case_card(dict(CASE), OWNERS, roles={"FINANCE"}))


def test_demo_operator_can_decide_and_co_approve():
    owners = OWNERS + [{"case_role": "OPERATOR", "person_id": "operator", "aad_object_id": "ooo"}]
    roles = cards.viewer_roles(owners, "OOO")
    assert {"approve", "revise", "override", "defer", "reject"} <= verbs(cards.case_card(dict(CASE), owners, roles=roles))
    case = dict(CASE, case_status="NEEDS_FINANCE", pending_option_id="S2", pending_units=260)
    assert "approve" in verbs(cards.case_card(case, owners, roles=roles))
    assert "8:orgid:ooo" in cards.case_card(dict(CASE), owners)["refresh"]["userIds"]


def test_refresh_targets_deciders_by_mri():
    ids = cards.case_card(dict(CASE), OWNERS)["refresh"]["userIds"]
    assert "8:orgid:AAA" in ids and "8:orgid:fff" in ids and all(i.startswith("8:orgid:") for i in ids)


def test_action_id_stable_and_input_sensitive():
    a = cards.action_id("C-1", "approve", "u", {"option_id": "S2"})
    assert a == cards.action_id("C-1", "approve", "u", {"option_id": "S2"})
    assert a != cards.action_id("C-1", "approve", "u", {"option_id": "S3"})
    assert len(a) <= 64


def test_invented_numbers_flags_only_new_figures():
    facts = {"units": 260, "gm": 12345.67, "pct": 0.062}
    assert advisor.invented_numbers("Move 260 units, 6.2% of price", facts) == set()
    assert advisor.invented_numbers("Move 999 units", facts)

RICH = {**CASE, "recommended_option_id": "S1", "evidence": {
            "style_name": "Volt Runner", "signals": {"intent_intraday": 1.6, "persistence": 0.72, "persistence_ok": True},
            "markets": [{"market_id": "US", "weeks_of_cover": 0.9}, {"market_id": "DE", "weeks_of_cover": 6.2, "transferable": 400}],
            "guardrail": {"max_transfer_cost_pct": 0.06}},
        "recommendation": {"horizon_days": 28,
            "inaction": {"lost_units": 991, "lost_full_price_sales_usd": 109000, "lost_gm_usd": 52000,
                         "stockout_sku_sizes": 9, "earliest_stockout_days": 6.8},
            "needs": [{"sku_id": "HM9443-105", "size": "10.5", "short_units": 120, "available": 14, "daily_rate": 4.8,
                       "days_to_stockout": 2.9}],
            "options": [{"option_id": "S0", "kind": "MAINTAIN"},
                        {"option_id": "S1", "kind": "REBALANCE", "units": 991, "source_markets": ["DE"], "modes": ["AIR"],
                         "max_lead_days": 8, "transfer_cost_usd": 3581, "transfer_cost_pct_of_price": 0.033,
                         "within_guardrail": True, "projected_gm_usd": 48000, "late_units": 180, "lost_gap_units": 40},
                        {"option_id": "S2", "kind": "EXPEDITE", "units": 991, "source_markets": ["DE"], "max_lead_days": 3,
                         "transfer_cost_usd": 5200, "within_guardrail": True, "projected_gm_usd": 47000}]},
        "drafts": [{"option_id": "S1", "draft_status": "PROPOSED", "from_location_id": "DC-DE-01", "lane_mode": "AIR",
                    "lead_days": 8, "units": 991, "cost_per_unit_usd": 3.61}]}


def test_briefing_explains_problem_recommendation_and_choices():
    import json
    card = cards.case_card(RICH, OWNERS, roles={"ACCOUNTABLE"})
    text = json.dumps(card)
    for phrase in ("The problem", "about 7 days", "991 units", "Sizes running short", "10.5",
                   "Why I believe this is real", "1.6x normal", "My recommendation: S1", "DC-DE-01", "within policy",
                   "Heads-up", "S2 uses faster shipping", "How to decide", "Who needs to do what", "glossary"):
        assert phrase in text, phrase
    todo = [b["text"] for b in cards.briefing(RICH, OWNERS) if b.get("type") == "TextBlock" and "· " in b.get("text", "")]
    assert any("D. Ortiz" in t for t in todo) and not any("F. Brandt" in t for t in todo)  # Finance only when needed
    assert len(text.encode()) < 28000


def test_briefing_falls_back_to_brief_for_old_cases():
    old = {**CASE, "recommendation": {**CASE["recommendation"], "brief": "Old terse brief."}}
    assert cards.briefing(old, OWNERS) == [cards._tb("Old terse brief.", spacing="Medium")]


STORE_CASE = {**RICH, "recommended_option_id": "S5",
              "recommendation": {**RICH["recommendation"],
                  "store_needs": [{"location_id": "ST-US-1001", "location_name": "Kinetiq NY Flagship", "sku_id": "HM9443-105",
                                   "size": "10.5", "short_units": 9, "available": 2, "daily_rate": 0.8, "days_to_stockout": 2.5}],
                  "donors": [{"location_id": "ST-US-1003", "location_name": "Kinetiq Chicago", "sku_id": "HM9443-105",
                              "size": "10.5", "spare_units": 11, "available": 15, "weeks_of_cover": 6.1}],
                  "options": RICH["recommendation"]["options"] + [
                      {"option_id": "S5", "kind": "STORE_BRIDGE_REBALANCE", "units": 1002, "store_units": 11,
                       "store_lead_days": 2, "donor_stores": ["ST-US-1003"], "receiving_stores": ["ST-US-1001"],
                       "source_markets": ["DE", "US"], "modes": ["AIR", "TRUCK"], "max_lead_days": 8,
                       "transfer_cost_usd": 3591, "transfer_cost_pct_of_price": 0.033, "within_guardrail": True,
                       "projected_gm_usd": 48500}]},
              "drafts": RICH["drafts"] + [
                  {"option_id": "S5", "draft_status": "PROPOSED", "from_location_id": "ST-US-1003", "to_location_id": "ST-US-1001",
                   "lane_mode": "TRUCK", "lead_days": 2, "units": 11, "cost_per_unit_usd": 0.9},
                  {"option_id": "S5", "draft_status": "PROPOSED", "from_location_id": "DC-DE-01", "to_location_id": "DC-US-MEM",
                   "lane_mode": "AIR", "lead_days": 8, "units": 991, "cost_per_unit_usd": 3.61}]}
STORE_OWNERS = OWNERS + [{"case_role": "STORE_MANAGER_SOURCE", "person_id": "sm-us-1003", "persona_name": "R. Okafor",
                          "location_id": "ST-US-1003"},
                         {"case_role": "STORE_MANAGER_DEST", "person_id": "sm-us-1001", "persona_name": "L. Brooks",
                          "location_id": "ST-US-1001"}]


def test_store_moves_name_both_stores_sizes_and_managers():
    case = {**STORE_CASE, "drafts": STORE_CASE["drafts"] + [
        {"option_id": "S5", "draft_status": "PROPOSED", "sku_id": "HM9443-001-9", "from_location_id": "ST-US-1003",
         "to_location_id": "ST-US-1001", "lane_mode": "TRUCK", "lead_days": 2, "units": 5, "cost_per_unit_usd": 1.0}]}
    case["drafts"][1] = dict(case["drafts"][1], sku_id="HM9443-001-10.5")
    [m] = cards.store_moves(case, "S5", cards.store_managers(STORE_OWNERS))
    assert m["from"] == "Kinetiq Chicago (ST-US-1003)" and m["to"] == "Kinetiq NY Flagship (ST-US-1001)"
    assert m["units"] == 16 and m["size_text"] == "5 × size 9, 11 × size 10.5"
    assert (m["from_manager"], m["to_manager"]) == ("R. Okafor", "L. Brooks")
    import json
    text = json.dumps(cards.briefing(case, STORE_OWNERS), ensure_ascii=False)
    for phrase in ("Step 1, now", "**Kinetiq Chicago (ST-US-1003)** → **Kinetiq NY Flagship (ST-US-1001)**",
                   "Step 2, refill", "R. Okafor / L. Brooks", "pick and send 5 × size 9, 11 × size 10.5 to Kinetiq NY",
                   "receive 5 × size 9, 11 × size 10.5 from Kinetiq Chicago"):
        assert phrase in text, phrase


def test_briefing_explains_store_to_store_bridge():
    import json
    text = json.dumps(cards.briefing(STORE_CASE, STORE_OWNERS))
    for phrase in ("stores that run out first", "Kinetiq NY Flagship", "stores with spare stock", "Kinetiq Chicago",
                   "Two steps", "11 spare units", "991 units", "ST-US-1001", "Safe for donor stores",
                   "R. Okafor", "L. Brooks", "Donor store"):
        assert phrase in text, phrase
    assert "from DE, US" not in text  # the home market is never listed as a source market


def test_event_labels_follow_case_date_not_gold_date():
    import json
    ev = {**RICH["evidence"], "events": [
        {"market_id": "US", "event_name": "City Marathon Expo", "start_date": "2026-11-01", "end_date": "2026-11-03"},
        {"market_id": "US", "event_name": "Black Friday Weekend", "start_date": "2026-11-27", "end_date": "2026-11-29"}]}
    text = json.dumps(cards.briefing({**RICH, "opened_at_utc": "2026-11-11T10:00:00", "evidence": ev}, OWNERS))
    assert "Recent event in US: City Marathon Expo ended 2026-11-03" in text
    assert "Upcoming event in US: City Marathon Expo" not in text
    assert "Upcoming event in US: Black Friday Weekend" in text


def test_briefing_explains_missing_store_move_and_earlier_transfer():
    import json
    ev = {**RICH["evidence"], "store_transfers_open": [
        {"sto_id": "STO-1", "sku_id": "HM9443-001-11", "from_location_id": "ST-US-1003", "to_location_id": "ST-US-1004",
         "units": 7}]}
    rec = {**RICH["recommendation"], "store_needs": [{"location_id": "ST-US-1007", "sku_id": "HM9443-001-11",
                                                      "size": "11", "short_units": 5, "available": 1,
                                                      "daily_rate": 0.4, "days_to_stockout": 2}]}
    text = json.dumps(cards.briefing({**RICH, "evidence": ev, "recommendation": rec}, OWNERS))
    assert "no store-to-store move this time" in text and "STO-1" in text and "ST-US-1003 \\u2192 ST-US-1004" in text

def test_charts_sit_after_the_problem_two_per_row_with_toggle():
    import json
    card = cards.case_card(STORE_CASE, STORE_OWNERS, roles={"ACCOUNTABLE"})
    body = card["body"]
    prob = next(i for i, b in enumerate(body) if b.get("style") == "warning")
    head, box = body[prob + 1], body[prob + 2]
    assert head["id"] == "glance_head" and box["id"] == "glance" and cards.has_charts(card)
    toggle = head["columns"][1]["items"][0]["actions"][0]
    assert toggle["type"] == "Action.ToggleVisibility" and toggle["targetElements"] == ["glance"]
    assert all(len(r["columns"]) == 2 for r in box["items"])
    ch = [c for r in box["items"] for col in r["columns"] for c in col["items"]]
    by = {c["type"]: c for c in ch}
    assert by["Chart.Gauge"]["value"] == 7 and by["Chart.Gauge"]["max"] == 28
    assert sum(s["value"] for s in by["Chart.Gauge"]["segments"]) == 28
    margin, cost = by["Chart.VerticalBar.Grouped"]["data"]
    assert [v["x"] for v in cost["values"]] == ["S1", "S2", "S5 ⭐"]
    assert [v["y"] for v in cost["values"]] == [3581, 5200, 3591] and margin["values"][2]["y"] == 48500
    bars = [c for c in ch if c["type"] == "Chart.HorizontalBar"]
    assert {"x": "US (this case)", "y": 0.9, "color": "attention"} in bars[0]["data"]
    assert bars[1]["data"] == [{"x": "Size 10.5", "y": 120}]
    assert [(d["y"], d["color"]) for d in bars[2]["data"]] == [(9, "attention"), (11, "good")]
    assert all(c.get("fallback") == "drop" for c in ch)
    assert cards.card_bytes(card) < cards.CARD_LIMIT
    assert cards.glance(CASE) == [] and cards.charts_card(CASE) is None


def test_oversized_case_card_leaves_charts_for_a_separate_card(monkeypatch):
    import json
    monkeypatch.setattr(cards, "CARD_LIMIT", 1000)
    card = cards.case_card(STORE_CASE, STORE_OWNERS, roles={"ACCOUNTABLE"})
    assert not cards.has_charts(card) and "Chart." not in json.dumps(card) and "The problem" in json.dumps(card)
    assert "Chart.Gauge" in json.dumps(cards.charts_card(STORE_CASE))
