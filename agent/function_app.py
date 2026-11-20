"""Azure Functions (Python v2) host for the Kinetiq rebalancing coworker (SYNTHETIC demo data only).

POST /api/events/t1   function key  manual / replay -> queue a T1 event
timer every 1 min     T1 outbox on OneLake (Activator -> Fabric UDF relay_t1 writes JSON; no secret) -> queue
POST /api/messages    Bot Framework Teams channel -> JWT validated in botconn; work is queued, card refresh is synchronous
queue agent-events    orchestrator.dispatch (one job at a time, 3 attempts then poison queue)
timer every 15 min    follow-ups (T+7/T+14, deferred reopen), approval SLA reminders/escalation, resume
GET  /api/health      dependency probe (function key)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

import azure.functions as func

from coworker import botconn, cards, health, orchestrator, outbox, settings

app = func.FunctionApp()
log = logging.getLogger("coworker")
QUEUE = "%AGENT_QUEUE_NAME%"
STORAGE = "AzureWebJobsStorage"
_STYLE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,31}$")
_MARKET = re.compile(r"^[A-Z]{2,8}$")


def _json(body: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body, default=str), status_code=status, mimetype="application/json")


def validate_t1(body: dict) -> dict:
    """Normalise and validate the Activator payload; raises ValueError on bad input."""
    style = str(body.get("style_id") or "").strip().upper()
    market = str(body.get("market_id") or "").strip().upper()
    if not _STYLE.match(style) or not _MARKET.match(market):
        raise ValueError("style_id and market_id are required")
    ts = body.get("signal_ts")
    if ts:
        ts = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        ts = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"style_id": style, "market_id": market, "signal_ts": ts, "source": str(body.get("source") or "activator")[:40]}


@app.route(route="events/t1", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
@app.queue_output(arg_name="out", queue_name=QUEUE, connection=STORAGE)
def t1_event(req: func.HttpRequest, out: func.Out[str]) -> func.HttpResponse:
    try:
        body = req.get_json()
        events = body if isinstance(body, list) else [body]
        jobs = [{"kind": "t1_event", "event": validate_t1(e)} for e in events[:50]]
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    out.set([json.dumps(j) for j in jobs])
    return _json({"queued": len(jobs), "events": [j["event"] for j in jobs]}, 202)


def route_activity(activity: dict) -> tuple[dict | None, dict | None]:
    """Map a Teams activity to (synchronous invoke response body, queued job). Pure: no I/O except refresh."""
    a_type = activity.get("type")
    frm = activity.get("from") or {}
    conv = (activity.get("conversation") or {}).get("id")
    common = {"from": {"id": frm.get("id"), "name": frm.get("name"), "aadObjectId": frm.get("aadObjectId")},
              "conversation_id": conv, "service_url": activity.get("serviceUrl")}
    if a_type == "invoke" and activity.get("name") == "adaptiveCard/action":
        action = (activity.get("value") or {}).get("action") or {}
        verb, data = action.get("verb"), action.get("data") or {}
        if verb == "refresh":
            card = orchestrator.refresh_response(data.get("case_id"), frm.get("aadObjectId"))
            return _invoke(card), None
        job = {"kind": "card_action", "verb": verb, "data": data, "activity_id": activity.get("id"), **common}
        # The invoke response replaces the card for this user, so send back their case view (buttons intact), not a bare ack.
        banner = f"{frm.get('name') or 'You'} chose {verb}; working on it. The outcome will be posted in this chat."
        try:
            card = orchestrator.card_for(data.get("case_id"), frm.get("aadObjectId"), banner=banner)
        except Exception:  # noqa: BLE001
            log.warning("viewer card unavailable for %s; sending a plain ack", data.get("case_id"), exc_info=True)
            card = cards.status_card("Working on it…", [banner], data_label=settings.get().data_label)
        return _invoke(card), job
    if a_type == "message" and (activity.get("text") or "").strip():
        return None, {"kind": "message", "text": activity.get("text"), "activity_id": activity.get("id"),
                      "reply_to_id": activity.get("replyToId"), **common}
    return None, None


def _invoke(card: dict) -> dict:
    return {"statusCode": 200, "type": "application/vnd.microsoft.card.adaptive", "value": card}


@app.route(route="messages", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.queue_output(arg_name="out", queue_name=QUEUE, connection=STORAGE)
def messages(req: func.HttpRequest, out: func.Out[str]) -> func.HttpResponse:
    try:
        activity = req.get_json()
    except ValueError:
        return func.HttpResponse(status_code=400)
    try:
        botconn.validate_inbound(req.headers.get("Authorization"), activity)
    except botconn.AuthError as e:
        log.warning("rejected channel request: %s", e)
        return func.HttpResponse(status_code=401)
    try:
        invoke, job = route_activity(activity)
    except Exception:  # noqa: BLE001
        log.exception("activity routing failed")
        invoke = _invoke(cards.status_card("Something went wrong", ["Nothing was changed. Try again shortly."]))
        job = None
    if job:
        out.set(json.dumps(job))
    if invoke is not None:
        return _json(invoke)
    return func.HttpResponse(status_code=200)


@app.queue_trigger(arg_name="msg", queue_name=QUEUE, connection=STORAGE)
def worker(msg: func.QueueMessage) -> None:
    job = json.loads(msg.get_body().decode("utf-8"))
    log.info("job %s (dequeue %s)", job.get("kind"), msg.dequeue_count)
    result = orchestrator.dispatch(job)
    log.info("job %s -> %s", job.get("kind"), json.dumps(result, default=str)[:500])


@app.timer_trigger(arg_name="timer", schedule="0 */15 * * * *", run_on_startup=False)
def tick(timer: func.TimerRequest) -> None:
    log.info("tick %s", json.dumps(orchestrator.tick()))


def outbox_jobs(claim, now: dt.datetime | None = None) -> list[dict]:
    jobs = []
    for marker, payload in outbox.new_events(claim, now):
        try:
            jobs.append({"kind": "t1_event", "event": validate_t1({**payload, "source": "activator-outbox"})})
        except ValueError as e:
            log.warning("outbox %s rejected: %s", marker, e)
    return jobs


@app.timer_trigger(arg_name="timer", schedule="0 * * * * *", run_on_startup=False)
@app.queue_output(arg_name="out", queue_name=QUEUE, connection=STORAGE)
def outbox_poll(timer: func.TimerRequest, out: func.Out[str]) -> None:
    jobs = outbox_jobs(lambda marker, payload: orchestrator.log_action(marker, None, "AA-00_OUTBOX_EVENT", "DONE", payload))
    if jobs:
        out.set([json.dumps(j) for j in jobs])
        log.info("outbox queued %s", json.dumps([j["event"] for j in jobs]))


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    only = [x for x in (req.params.get("only") or "").split(",") if x] or None
    res = health.run(only)
    return _json(res, 200 if res["ok"] else 503)
