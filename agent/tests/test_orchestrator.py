"""Orchestrator pure helpers and Function HTTP routing (no Azure calls)."""
import pytest

from coworker import orchestrator as orch


def test_strip_mentions():
    assert orch.strip_mentions("<at>Coworker</at>&nbsp;exclude GB") == "exclude GB"


@pytest.mark.parametrize("verb,data,expect", [
    ("approve", {"option_id": "S2"}, {"decision": "APPROVE", "option_id": "S2"}),
    ("revise", {"option_id": "S2", "rev_units": "120", "rev_rationale": " promo "},
     {"decision": "REVISE", "option_id": "S2", "units": 120, "rationale": "promo"}),
    ("override", {"ovr_option": "S0", "ovr_reason": "LOCAL_EVENT", "ovr_rationale": "x"},
     {"decision": "OVERRIDE", "option_id": "S0", "reason_code": "LOCAL_EVENT", "rationale": "x"}),
    ("defer", {"def_date": "2026-11-20"}, {"decision": "DEFER", "defer_until_utc": "2026-11-20T12:00:00"}),
    ("reject", {"rej_rationale": "  "}, {"decision": "REJECT", "rationale": None}),
])
def test_decision_args(verb, data, expect):
    assert orch._decision_args(verb, data) == expect


def test_unknown_verb_rejected():
    with pytest.raises(ValueError):
        orch._decision_args("delete", {})


def test_intro_groups_personas_per_user():
    people = [{"aad_object_id": "AAA", "person_id": "dortiz", "persona_name": "D. Ortiz", "case_role": "ACCOUNTABLE",
               "user_display_name": "Wonda"},
              {"aad_object_id": "aaa", "person_id": "x", "persona_name": "X", "case_role": "PLANNER"},
              {"aad_object_id": "bbb", "person_id": "fb", "persona_name": "F", "case_role": "FINANCE"}]
    text, ents = orch._intro(people, {"case_id": "C-1", "style_id": "HM9443", "market_id": "US"})
    assert len(ents) == 2 and text.count("<at>") == 2
    assert "you own this decision" in text and "sanity-check" in text and "Finance co-approval" in text


def test_store_manager_ask_names_their_store_and_partner_store():
    from test_cards import STORE_CASE
    case = {**STORE_CASE, "case_id": "C-2"}
    people = [{"aad_object_id": "s1", "person_id": "sm-us-1003", "persona_name": "R. Okafor",
               "case_role": "STORE_MANAGER_SOURCE", "location_id": "ST-US-1003"},
              {"aad_object_id": "s2", "person_id": "sm-us-1001", "persona_name": "L. Brooks",
               "case_role": "STORE_MANAGER_DEST", "location_id": "ST-US-1001"}]
    text, _ = orch._intro(people, case)
    assert "Store-to-store moves (from → to)" in text
    assert "Kinetiq Chicago (ST-US-1003) → Kinetiq NY Flagship (ST-US-1001)" in text
    assert "pick and send 11 × size" in text and "to Kinetiq NY Flagship (ST-US-1001)" in text
    assert "receive 11 × size" in text and "from Kinetiq Chicago (ST-US-1003) (arrives in 2 days)" in text


def test_wanted_members_adds_finance_only_when_material():
    owners = [{"case_role": r, "person_id": r, "aad_object_id": r.lower()} for r in ("ACCOUNTABLE", "FINANCE", "PLANNER")]
    case = {"recommended_option_id": "S2", "recommendation": {"options": [{"option_id": "S2", "needs_finance": False}]}}
    assert {p["case_role"] for p in orch.wanted_members(case, owners)} == {"ACCOUNTABLE", "PLANNER"}
    case["recommendation"]["options"][0]["needs_finance"] = True
    assert "FINANCE" in {p["case_role"] for p in orch.wanted_members(case, owners)}


fa = pytest.importorskip("function_app")


def test_validate_t1_normalises():
    e = fa.validate_t1({"style_id": "hm9443", "market_id": "us", "signal_ts": "2026-11-08T16:00:00+00:00"})
    assert e == {"style_id": "HM9443", "market_id": "US", "signal_ts": "2026-11-08T16:00:00Z", "source": "activator"}
    with pytest.raises(ValueError):
        fa.validate_t1({"style_id": "x; DROP", "market_id": "US"})


def test_route_message_queues_job():
    inv, job = fa.route_activity({"type": "message", "text": "<at>bot</at> hi", "id": "1",
                                  "conversation": {"id": "19:c@thread.v2"}, "from": {"aadObjectId": "a"}})
    assert inv is None and job["kind"] == "message" and job["conversation_id"] == "19:c@thread.v2"


def test_route_card_action_acks_and_queues(monkeypatch):
    act = {"type": "invoke", "name": "adaptiveCard/action", "conversation": {"id": "c"}, "from": {"aadObjectId": "a"},
           "value": {"action": {"verb": "approve", "data": {"case_id": "C-1", "option_id": "S2"}}}}
    monkeypatch.setattr(fa.orchestrator, "card_for", lambda cid, aad, banner=None: {"case": cid, "viewer": aad, "b": banner})
    inv, job = fa.route_activity(act)
    assert inv["statusCode"] == 200 and job["verb"] == "approve" and job["data"]["option_id"] == "S2"
    # The ack keeps the viewer's own case card (with buttons) instead of replacing it with a bare status card.
    assert inv["value"]["case"] == "C-1" and inv["value"]["viewer"] == "a" and "approve" in inv["value"]["b"]


def test_route_refresh_is_synchronous(monkeypatch):
    monkeypatch.setattr(fa.orchestrator, "refresh_response", lambda cid, aad: {"case": cid, "viewer": aad})
    act = {"type": "invoke", "name": "adaptiveCard/action", "conversation": {"id": "c"}, "from": {"aadObjectId": "a"},
           "value": {"action": {"verb": "refresh", "data": {"case_id": "C-1"}}}}
    inv, job = fa.route_activity(act)
    assert job is None and inv["value"] == {"case": "C-1", "viewer": "a"}


def test_outbox_claims_each_file_once_and_rejects_bad_payloads(monkeypatch):
    import datetime as dt
    import types
    from coworker import outbox
    now = dt.datetime(2026, 11, 9, 2, 30, tzinfo=dt.timezone.utc)
    fresh, stale = "Mon, 09 Nov 2026 02:20:00 GMT", "Sun, 08 Nov 2026 12:00:00 GMT"
    files = {dt.date(2026, 11, 9): [{"name": "lh/Files/agent_outbox/2026-11-09/a.json", "lastModified": fresh},
                                    {"name": "lh/Files/agent_outbox/2026-11-09/bad.json", "lastModified": fresh}],
             dt.date(2026, 11, 8): [{"name": "lh/Files/agent_outbox/2026-11-08/old.json", "lastModified": stale}]}
    bodies = {"a.json": {"style_id": "hm9443", "market_id": "us", "signal_ts": "2026-11-08T16:00:00Z"},
              "bad.json": {"style_id": "x; DROP", "market_id": "US"}, "old.json": {"style_id": "HM9443", "market_id": "BR"}}
    monkeypatch.setattr(outbox.settings, "get", lambda: types.SimpleNamespace(outbox_lakehouse_id="lh", outbox_dir="Files/agent_outbox"))
    monkeypatch.setattr(outbox, "list_files", lambda day: files.get(day, []))
    monkeypatch.setattr(outbox, "read_file", lambda name: bodies[name.rsplit("/", 1)[-1]])
    monkeypatch.setattr(outbox, "_seen", set())
    claimed: set[str] = set()

    def claim(marker, payload):
        first = marker not in claimed
        claimed.add(marker)
        return first

    jobs = fa.outbox_jobs(claim, now)
    assert [j["event"] for j in jobs] == [{"style_id": "HM9443", "market_id": "US", "signal_ts": "2026-11-08T16:00:00Z",
                                           "source": "activator-outbox"}]
    assert claimed == {"outbox:2026-11-09/a.json", "outbox:2026-11-09/bad.json"}  # stale file never read or claimed
    outbox._seen.clear()
    assert fa.outbox_jobs(claim, now) == []  # the durable claim (action_log) still dedups after an instance restart

def test_signal_update_tolerates_chat_without_agent(monkeypatch):
    from coworker import botconn
    logged = []
    monkeypatch.setattr(orch, "log_action", lambda *a: logged.append(a[0]) or True)

    def not_in_chat(*a, **k):
        raise botconn.NotInChat("403 BotNotInConversationRoster")
    monkeypatch.setattr(orch.botconn, "send", not_in_chat)
    ev = {"market_id": "US", "markets": [{"market_id": "US", "weeks_of_cover": 0.4}],
          "verification": {"signal_ts": "2026-11-08T16:00:00Z", "intraday_t1_skus": ["a", "b"]}}
    out = orch._signal_update({"case_id": "C-1", "case_status": "AWAITING_APPROVAL", "chat_id": "19:x"}, ev)
    assert out == {"status": "dedup", "case_id": "C-1", "posted": False}
    assert len(logged) == 2 and logged[1].endswith(":pending")
