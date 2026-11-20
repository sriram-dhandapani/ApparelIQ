"""udf_agent_relay: Activator -> coworker agent relay (Kinetiq UC1, SYNTHETIC demo data).

Activator rule "UC1 T1 projected stock-out" (act_uc1_t1) calls relay_t1 when a style x market's intraday T1 flag
becomes true. The function writes one JSON event into the agent outbox on lh_medallion (Files/agent_outbox/<date>/),
which the coworker Function polls every minute with its managed identity (read-only OneLake role).

No secret and no inbound endpoint are involved: the Lakehouse connection runs with the UDF connection owner's identity.
The file name is deterministic per event, so Activator retries overwrite the same file, and the agent claims each file
once (agent.action_log).
"""
import datetime as dt
import json
import logging
import re

import fabric.functions as fn

udf = fn.UserDataFunctions()

OUTBOX_DIR = "agent_outbox"
_STYLE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,31}$")
_MARKET = re.compile(r"^[A-Z]{2,8}$")


def _utc(ts: str) -> dt.datetime:
    if not ts:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    v = dt.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    return (v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc).replace(microsecond=0)


@udf.connection(argName="outboxLakehouse", alias="lhmedallion")
@udf.function()
def relay_t1(outboxLakehouse: fn.FabricLakehouseClient, styleId: str, marketId: str, signalTs: str) -> str:
    """Queue a T1 projected stock-out event for the coworker agent. Returns the outbox file path."""
    style, market = (styleId or "").strip().upper(), (marketId or "").strip().upper()
    if not _STYLE.match(style) or not _MARKET.match(market):
        raise ValueError("styleId and marketId must be catalogue codes, e.g. HM9443 / US")
    ts, now = _utc(signalTs), dt.datetime.now(dt.timezone.utc)
    path = f"{OUTBOX_DIR}/{now:%Y-%m-%d}/{ts:%Y%m%dT%H%M%SZ}_{style}_{market}.json"
    event = {"style_id": style, "market_id": market, "signal_ts": f"{ts:%Y-%m-%dT%H:%M:%SZ}", "source": "activator",
             "rule": "UC1 T1 projected stock-out", "written_at": f"{now:%Y-%m-%dT%H:%M:%SZ}", "record_label": "SYNTHETIC"}
    files = outboxLakehouse.connectToFiles()
    try:
        files.get_file_client(path).upload_data(json.dumps(event).encode("utf-8"), overwrite=True)
    finally:
        files.close()
    logging.info("relay_t1 wrote %s", path)
    return path
