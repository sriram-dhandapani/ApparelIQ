"""Read-only KQL against the Fabric Eventhouse (Gold external tables + real-time trigger functions)."""
from __future__ import annotations

import json
from typing import Any

import requests

from . import auth, settings


def query(csl: str, **params: str | float | int) -> list[dict[str, Any]]:
    """Run KQL with typed query parameters (declare query_parameters), so event values are never spliced into text."""
    s = settings.get()
    if params:
        decl = ", ".join(f"{k}:{_kql_type(v)}" for k, v in params.items())
        csl = f"declare query_parameters({decl});\n{csl}"
    body = {"db": s.kql_database, "csl": csl,
            "properties": json.dumps({"Parameters": {k: str(v) for k, v in params.items()},
                                      "Options": {"servertimeout": "00:02:00"}})}
    r = requests.post(f"{s.kql_query_uri}/v1/rest/query", json=body, timeout=150,
                      headers={"Authorization": "Bearer " + auth.token(auth.KUSTO)})
    if r.status_code != 200:
        raise RuntimeError(f"KQL {r.status_code}: {r.text[:500]}")
    t = r.json()["Tables"][0]
    cols = [c["ColumnName"] for c in t["Columns"]]
    rows = [dict(zip(cols, row)) for row in t["Rows"]]
    # v1 reports some failures (e.g. external-table access) inline as an exception row instead of an HTTP error
    for row in t["Rows"]:
        if isinstance(row, dict) and row.get("Exceptions"):
            raise RuntimeError(f"KQL error: {str(row['Exceptions'])[:500]}")
    if rows and "Exception" in cols and len(cols) == 1:
        raise RuntimeError(f"KQL error: {str(rows[0]['Exception'])[:500]}")
    return rows


def _kql_type(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "long"
    if isinstance(v, float):
        return "real"
    return "string"


def health() -> dict:
    rows = query("ext_gold_rebalancing_candidates | summarize n = count(), as_of = max(as_of_date)")
    if not rows or "n" not in rows[0]:
        return {"ok": False, "detail": f"unexpected result {str(rows)[:300]}"}
    return {"ok": bool(rows[0]["n"]), "detail": f"{rows[0]['n']} gold candidates as of {rows[0].get('as_of')}"}
