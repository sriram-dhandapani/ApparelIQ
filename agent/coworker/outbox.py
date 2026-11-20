"""T1 event outbox on OneLake (D-26): Activator -> Fabric UDF relay_t1 -> JSON file -> this poller -> agent queue.

Files land at <lakehouse>/<OUTBOX_DIR>/<yyyy-mm-dd>/<signal>_<style>_<market>.json (UTC write date). The agent identity
has a read-only OneLake role on that folder; nothing is deleted. Each file is claimed once through agent.action_log
(idempotent on action_id), so replays and overlapping timer runs never enqueue the same event twice.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import logging
from collections.abc import Callable, Iterator

import requests

from . import auth, settings

log = logging.getLogger(__name__)
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
_HDR = {"x-ms-version": "2023-11-03"}
_seen: set[str] = set()


def _get(url: str, params: dict | None = None) -> requests.Response:
    return requests.get(url, params=params, timeout=30,
                        headers={**_HDR, "Authorization": f"Bearer {auth.token(auth.STORAGE)}"})


def list_files(day: dt.date) -> list[dict]:
    s = settings.get()
    r = _get(f"{ONELAKE}/{s.workspace_id}", {"resource": "filesystem", "recursive": "false",
                                             "directory": f"{s.outbox_lakehouse_id}/{s.outbox_dir}/{day.isoformat()}"})
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [p for p in r.json().get("paths", []) if str(p.get("isDirectory", "false")).lower() != "true"
            and p["name"].endswith(".json")]


def read_file(name: str) -> dict:
    r = _get(f"{ONELAKE}/{settings.get().workspace_id}/{name}")
    r.raise_for_status()
    return json.loads(r.content.decode("utf-8"))


def new_events(claim: Callable[[str, dict], bool], now: dt.datetime | None = None,
               max_age: dt.timedelta = dt.timedelta(hours=6)) -> Iterator[tuple[str, dict]]:
    """Yield (marker, payload) for outbox files not yet claimed. `claim` returns True only the first time."""
    s = settings.get()
    if not s.outbox_lakehouse_id:
        return
    now = now or dt.datetime.now(dt.timezone.utc)
    for day in sorted({(now - dt.timedelta(days=1)).date(), now.date()}):
        for p in list_files(day):
            marker = "outbox:" + p["name"].split(f"/{s.outbox_dir}/", 1)[-1]
            if marker in _seen:
                continue
            modified = email.utils.parsedate_to_datetime(p["lastModified"]) if p.get("lastModified") else now
            if now - modified > max_age:
                _seen.add(marker)
                continue
            try:
                payload = read_file(p["name"])
            except (requests.RequestException, ValueError):
                log.exception("outbox read failed for %s", p["name"])
                continue
            first = claim(marker, payload)
            _seen.add(marker)
            if len(_seen) > 5000:
                _seen.clear()
            if first:
                yield marker, payload
