"""Dependency probe for GET /api/health. Each check is isolated so one failure does not hide the others."""
from __future__ import annotations

import time

from . import advisor, botconn, graph, kql, settings, sql

CHECKS = {"sql": sql.health, "kql": kql.health, "graph": graph.health, "advisor": advisor.health, "bot": botconn.health}


def run(only: list[str] | None = None) -> dict:
    out: dict = {"data_label": settings.get().data_label, "checks": {}}
    for name, fn in CHECKS.items():
        if only and name not in only:
            continue
        t0 = time.perf_counter()
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:300]}"}
        res["ms"] = int((time.perf_counter() - t0) * 1000)
        out["checks"][name] = res
    out["ok"] = all(c.get("ok") for c in out["checks"].values())
    return out
