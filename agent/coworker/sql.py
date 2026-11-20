"""Azure SQL (ERP/WMS + agent case store) over the private endpoint with an Entra token. Read-only SELECTs plus
EXEC of agent.usp_* procedures; the database user is DENIED direct writes (07_agent_grants.sql)."""
from __future__ import annotations

import logging
import struct
import time
from typing import Any

from . import auth, settings

log = logging.getLogger(__name__)
SQL_COPT_SS_ACCESS_TOKEN = 1256
_RETRYABLE = ("40613", "40197", "40501", "49918", "10928", "10929", "08S01", "HYT00")


def _connect_pyodbc():
    import pyodbc  # preinstalled ODBC Driver 18/17 on the Functions Linux image

    s = settings.get()
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    driver = next((d for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server") if d in drivers), None)
    if not driver:
        raise RuntimeError(f"no SQL Server ODBC driver (found {pyodbc.drivers()})")
    raw = auth.token(auth.SQL).encode("utf-16-le")
    conn = pyodbc.connect(
        f"Driver={{{driver}}};Server=tcp:{s.sql_server},1433;Database={s.sql_database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;",
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: struct.pack(f"<I{len(raw)}s", len(raw), raw)},
        autocommit=True)
    return conn, "pyodbc/" + driver


def _connect_mssql_python():
    import mssql_python

    s = settings.get()
    return mssql_python.connect(
        f"Server=tcp:{s.sql_server},1433;Database={s.sql_database};Encrypt=yes;"
        f"Authentication=ActiveDirectoryMSI;UID={s.client_id};", autocommit=True), "mssql-python"


def connect():
    try:
        return _connect_pyodbc()
    except ImportError:
        return _connect_mssql_python()


def _rows(cur) -> list[dict[str, Any]]:
    while cur.description is None:
        if not cur.nextset():
            return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def query(sql: str, *params: Any) -> list[dict[str, Any]]:
    for attempt in range(4):
        try:
            conn, _ = connect()
            try:
                cur = conn.cursor()
                cur.execute(sql, *params) if params else cur.execute(sql)
                return _rows(cur)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            if attempt < 3 and any(code in str(e) for code in _RETRYABLE):
                log.warning("sql transient error, retrying: %s", str(e)[:200])
                time.sleep(5 * (attempt + 1))
                continue
            raise
    return []


def exec_proc(proc: str, **params: Any) -> list[dict[str, Any]]:
    """EXEC agent.<proc> @name = ?, ... with bound parameters (never string-built values)."""
    if not proc.replace("_", "").isalnum():
        raise ValueError(proc)
    names = ", ".join(f"@{k} = ?" for k in params)
    return query(f"SET NOCOUNT ON; EXEC [agent].[{proc}] {names}", *params.values())


def one(proc: str, **params: Any) -> dict[str, Any]:
    rows = exec_proc(proc, **params)
    return rows[0] if rows else {}


def health() -> dict:
    t = time.time()
    conn, how = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT USER_NAME() AS u, (SELECT COUNT(*) FROM agent.persona_user) AS personas")
        u, n = cur.fetchone()
    finally:
        conn.close()
    return {"ok": True, "detail": f"{how} as {u}; {n} personas; {time.time() - t:.1f}s"}
