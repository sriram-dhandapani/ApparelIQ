"""Idempotent Azure Databricks setup for the ApparelIQ demo (Unity Catalog + workspace files + jobs).

All data creation and resets run inside Databricks notebooks; this script only uploads them and starts job runs.

  python scripts/setup_databricks.py --config config/deployment.local.json setup     # UC objects + upload
  python scripts/setup_databricks.py --config config/deployment.local.json run-batch  # notebook 01 as a job run
  python scripts/setup_databricks.py --config config/deployment.local.json run-erp    # notebook 02: ERP/WMS tables + decision history
  python scripts/setup_databricks.py --config config/deployment.local.json run-sql-grants  # notebook 03: Fabric mirroring user
  python scripts/setup_databricks.py --config config/deployment.local.json run-sql-agent  # notebook 03: step 9 agent schema, personas, grants
  python scripts/setup_databricks.py --config config/deployment.local.json test-sql-agent # notebook 04: gateway contract tests (self-cleaning)
  python scripts/setup_databricks.py --config config/deployment.local.json close-case --case-id C-...  # notebook 05: close an agent case
    python scripts/setup_databricks.py --config config/deployment.local.json reset-store-transfers --style-id HM9443 --market-id US
        # notebook 05: cancel unshipped agent STOs, release reserved store/DC units, close open cases (store options return)
        # add --mode preview to either reset to see what would change without committing
  python scripts/setup_databricks.py --config config/deployment.local.json run-erp    # NOTE: run `upload` first after generator changes
  python scripts/setup_databricks.py --config config/deployment.local.json warehouse  # classic Pro SQL warehouse (in VNet)

Auth: Microsoft Entra token from the Azure CLI (resource 2ff814a6-3304-4ab8-85cb-cd0e6f879c1d). No PATs or secrets.
Environment-specific values (workspace URL, access connector id, storage account, SQL FQDN) come from the
git-ignored config file; see config/deployment.example.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATABRICKS_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
WS_ROOT = "/Workspace/Shared/appareliq"


class Dbx:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url = "https://" + cfg["databricks"]["workspace_url"].removeprefix("https://").rstrip("/")
        self.s = requests.Session()
        self._token_at = 0.0
        self._refresh_token()

    def _refresh_token(self):
        # Entra tokens last ~60-90 min; long job polls must refresh them.
        tok = subprocess.run(["az", "account", "get-access-token", "--tenant", self.cfg["tenant_id"], "--resource",
                              DATABRICKS_RESOURCE, "--query", "accessToken", "-o", "tsv"],
                             capture_output=True, text=True, check=True, shell=sys.platform == "win32").stdout.strip()
        self.s.headers["Authorization"] = f"Bearer {tok}"
        self._token_at = time.time()

    def call(self, method: str, path: str, ok=(200,), **kw):
        if time.time() - self._token_at > 40 * 60:
            self._refresh_token()
        r = self.s.request(method, self.url + path, timeout=120, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.text else {}

    def get_or_none(self, path: str):
        r = self.s.get(self.url + path, timeout=60)
        return r.json() if r.status_code == 200 else None


# ------------------------------------------------------------------ Unity Catalog
def setup_uc(d: Dbx):
    c = d.cfg["databricks"]
    st = d.cfg["storage"]["account_name"]
    ac = d.cfg["databricks"]["access_connector_id"]
    cat, root = c["catalog"], f"abfss://unity@{st}.dfs.core.windows.net/"
    msid = d.call("GET", "/api/2.1/unity-catalog/current-metastore-assignment")["metastore_id"]
    print("metastore", msid)

    cred = c["storage_credential"]
    if not d.get_or_none(f"/api/2.1/unity-catalog/storage-credentials/{cred}"):
        d.call("POST", "/api/2.1/unity-catalog/storage-credentials",
               json={"name": cred, "azure_managed_identity": {"access_connector_id": ac},
                     "comment": "ApparelIQ: access connector MI for the private ADLS account"})
    print("storage credential", cred)

    loc = c["external_location"]
    if not d.get_or_none(f"/api/2.1/unity-catalog/external-locations/{loc}"):
        d.call("POST", "/api/2.1/unity-catalog/external-locations",
               json={"name": loc, "url": root, "credential_name": cred,
                     "comment": "ApparelIQ: private ADLS container 'unity' (no public network access)"})
    print("external location", loc, root)

    if not d.get_or_none(f"/api/2.1/unity-catalog/catalogs/{cat}"):
        d.call("POST", "/api/2.1/unity-catalog/catalogs",
               json={"name": cat, "storage_root": root + cat,
                     "comment": "ApparelIQ SYNTHETIC demo data (Kinetiq is a fictional brand)"})
    print("catalog", cat)
    sch = c["schema"]
    if not d.get_or_none(f"/api/2.1/unity-catalog/schemas/{cat}.{sch}"):
        d.call("POST", "/api/2.1/unity-catalog/schemas",
               json={"name": sch, "catalog_name": cat, "comment": "SYNTHETIC batch sources DS-01, DS-02"})
    print("schema", f"{cat}.{sch}")

    svc = c["service_credential"]
    if not d.get_or_none(f"/api/2.1/unity-catalog/credentials/{svc}"):
        d.call("POST", "/api/2.1/unity-catalog/credentials",
               json={"name": svc, "purpose": "SERVICE", "azure_managed_identity": {"access_connector_id": ac},
                     "comment": "ApparelIQ: access connector MI used to reach the private Azure SQL ERP/WMS DB"})
    print("service credential", svc)

    # Fabric mirroring reads Delta files directly -> metastore external data access + EXTERNAL USE SCHEMA
    try:
        d.call("PATCH", f"/api/2.1/unity-catalog/metastores/{msid}", json={"external_access_enabled": True})
        print("metastore external data access: enabled")
    except RuntimeError as e:
        print("WARNING could not enable external data access (needs metastore/account admin):", e)
    me = d.call("GET", "/api/2.0/preview/scim/v2/Me")["userName"]
    d.call("PATCH", f"/api/2.1/unity-catalog/permissions/catalog/{cat}",
           json={"changes": [{"principal": me, "add": ["ALL_PRIVILEGES", "EXTERNAL_USE_SCHEMA"]}]})
    print("grants on", cat, "->", me)


# ------------------------------------------------------------------ SQL warehouse
WAREHOUSE_NAME = "appareliq-classic-pro"


def ensure_warehouse(d: Dbx) -> str:
    """Classic (non-serverless) Pro warehouse: runs in the injected VNet, so it reaches the private ADLS account
    through its private endpoints. Serverless warehouses run outside the VNet and fail on data-file reads."""
    wh = next((w for w in d.call("GET", "/api/2.0/sql/warehouses").get("warehouses", [])
               if w["name"] == WAREHOUSE_NAME), None)
    if not wh:
        wh = d.call("POST", "/api/2.0/sql/warehouses", json={
            "name": WAREHOUSE_NAME, "warehouse_type": "PRO", "enable_serverless_compute": False,
            "cluster_size": "2X-Small", "min_num_clusters": 1, "max_num_clusters": 1, "auto_stop_mins": 15,
            "spot_instance_policy": "COST_OPTIMIZED",
            "tags": {"custom_tags": [{"key": "project", "value": "ApparelIQ"}, {"key": "data", "value": "SYNTHETIC"}]}})
    wid = wh["id"]
    state = d.call("GET", f"/api/2.0/sql/warehouses/{wid}")["state"]
    if state in ("STOPPED", "STOPPING"):
        d.call("POST", f"/api/2.0/sql/warehouses/{wid}/start")
    for _ in range(80):
        w = d.call("GET", f"/api/2.0/sql/warehouses/{wid}")
        if w["state"] == "RUNNING":
            break
        if w["state"] in ("DELETED", "DELETING"):
            raise RuntimeError(f"warehouse {wid} is {w['state']}: {w.get('health')}")
        time.sleep(15)
    else:
        raise RuntimeError(f"warehouse {wid} not RUNNING: {w.get('health')}")
    c = d.cfg["databricks"]
    q = (f"SELECT location_id, SUM(units_sold) AS units FROM {c['catalog']}.{c['schema']}.sell_through_daily "
         "GROUP BY location_id ORDER BY units DESC LIMIT 3")
    r = d.call("POST", "/api/2.0/sql/statements", json={"warehouse_id": wid, "statement": q, "wait_timeout": "50s"})
    print("warehouse", WAREHOUSE_NAME, wid, "| test query:", r["status"]["state"],
          r.get("result", {}).get("data_array") or r["status"].get("error", {}).get("message", "")[:300])
    return wid


# ------------------------------------------------------------------ workspace files
def _import(d: Dbx, path: str, content: bytes, fmt: str, language: str | None = None):
    body = {"path": path, "content": base64.b64encode(content).decode(), "format": fmt, "overwrite": True}
    if language:
        body["language"] = language
    d.call("POST", "/api/2.0/workspace/import", json=body)


def upload(d: Dbx):
    for sub in ("sql", "notebooks"):
        # Recreate the SQL folder so scripts removed from the repo don't linger in the workspace.
        if sub == "sql":
            d.call("POST", "/api/2.0/workspace/delete", json={"path": f"{WS_ROOT}/sql", "recursive": True}, ok=(200, 404))
        d.call("POST", "/api/2.0/workspace/mkdirs", json={"path": f"{WS_ROOT}/{sub}"})
    # The generators are self-contained notebooks; remove the package folder left by older uploads.
    d.call("POST", "/api/2.0/workspace/delete", json={"path": f"{WS_ROOT}/src", "recursive": True}, ok=(200, 404))
    for f in sorted((ROOT / "sql" / "azure_sql").glob("*.sql")):
        _import(d, f"{WS_ROOT}/sql/{f.name}", f.read_bytes(), "AUTO")
    for f in sorted((ROOT / "databricks" / "notebooks").glob("*.ipynb")):
        _import(d, f"{WS_ROOT}/notebooks/{f.stem}", f.read_bytes(), "JUPYTER")
    print("uploaded SQL scripts and notebooks to", WS_ROOT)


# ------------------------------------------------------------------ job runs
FALLBACK_NODE_TYPES = ["Standard_D8ds_v5", "Standard_D8as_v5", "Standard_E8ds_v5", "Standard_D8s_v3", "Standard_DS4_v2"]


def run_notebook(d: Dbx, notebook: str, params: dict, name: str) -> dict:
    """Submit a one-time run; on regional VM stockout retry with the next node type."""
    c = d.cfg["databricks"]
    preferred = c.get("node_type_id", FALLBACK_NODE_TYPES[0])
    node_types = [preferred] + [n for n in FALLBACK_NODE_TYPES if n != preferred]
    for i, node_type in enumerate(node_types):
        try:
            return _run_once(d, notebook, params, name, node_type)
        except _Stockout as e:
            print(f"stockout on {node_type}: {e}"[:300])
            if i == len(node_types) - 1:
                raise SystemExit(1)


class _Stockout(Exception):
    pass


def _run_once(d: Dbx, notebook: str, params: dict, name: str, node_type: str) -> dict:
    c = d.cfg["databricks"]
    print("node type:", node_type)
    cluster = {
        "spark_version": c.get("spark_version", "16.4.x-scala2.12"),
        "node_type_id": node_type,
        "num_workers": 0,
        "data_security_mode": "SINGLE_USER",
        "single_user_name": d.call("GET", "/api/2.0/preview/scim/v2/Me")["userName"],
        "spark_conf": {"spark.databricks.cluster.profile": "singleNode", "spark.master": "local[*]"},
        "custom_tags": {"ResourceClass": "SingleNode", "project": "appareliq"},
    }
    run = d.call("POST", "/api/2.1/jobs/runs/submit", json={
        "run_name": name, "timeout_seconds": 7200,
        "tasks": [{"task_key": "main", "new_cluster": cluster,
                   "notebook_task": {"notebook_path": f"{WS_ROOT}/notebooks/{notebook}", "base_parameters": params}}]})
    rid = run["run_id"]
    print(f"run {rid} submitted: {d.url}/#job/runs/{rid}")
    while True:
        try:
            r = d.call("GET", f"/api/2.1/jobs/runs/get?run_id={rid}")
        except RuntimeError as e:  # transient control-plane errors right after workspace creation
            print("poll retry:", str(e)[:160])
            time.sleep(30)
            continue
        state = r["state"]
        if state["life_cycle_state"] in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
            break
        # Databricks keeps retrying a stocked-out VM size for ~25 min; fail fast instead.
        cid = (r["tasks"][0].get("cluster_instance") or {}).get("cluster_id")
        if cid and state["life_cycle_state"] == "PENDING":
            ev = d.call("POST", "/api/2.0/clusters/events", json={"cluster_id": cid, "limit": 5}).get("events", [])
            fail = next((e for e in ev if e["type"] == "ADD_NODES_FAILED"), None)
            if fail and "STOCKOUT" in fail.get("details", {}).get("reason", {}).get("code", ""):
                d.call("POST", "/api/2.1/jobs/runs/cancel", json={"run_id": rid})
                raise _Stockout(fail["details"]["reason"]["parameters"].get("azure_error_message", "")[:200])
        print(time.strftime("%H:%M:%S"), state["life_cycle_state"], state.get("state_message", "")[:100])
        time.sleep(30)
    task_run = r["tasks"][0]["run_id"]
    out = d.call("GET", f"/api/2.1/jobs/runs/get-output?run_id={task_run}")
    print("result:", state.get("result_state"), state.get("state_message", "")[:300])
    if "STOCKOUT" in state.get("state_message", "") or "SkuNotAvailable" in state.get("state_message", ""):
        raise _Stockout(state.get("state_message", "")[:200])
    if out.get("error"):
        print("error:", out["error"], "\n", (out.get("error_trace") or "")[-3000:])
    if out.get("notebook_output"):
        print("notebook output:", out["notebook_output"].get("result"))
    if state.get("result_state") != "SUCCESS":
        raise SystemExit(1)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "deployment.local.json"))
    ap.add_argument("--profile", default="demo")
    ap.add_argument("action", choices=["setup", "upload", "run-batch", "run-erp", "run-sql-grants", "run-sql-diag",
                                       "run-sql-agent", "test-sql-agent", "close-case",
                                       "reset-store-transfers", "warehouse"])
    ap.add_argument("--case-id", help="close-case: agent case to close (admin demo reset, no ERP write-back)")
    ap.add_argument("--style-id", default="HM9443", help="reset-store-transfers: style (default HM9443)")
    ap.add_argument("--market-id", default="US", help="reset-store-transfers: destination market (default US)")
    ap.add_argument("--mode", default="write", choices=["write", "verify", "preview"],
                    help="run-batch: write | verify (compare with the UC tables, no writes); run-erp: write | preview; "
                         "close-case / reset-store-transfers: preview rolls back and only shows what would change")
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    d = Dbx(cfg)
    c = cfg["databricks"]
    if a.action == "setup":
        setup_uc(d)
        upload(d)
    elif a.action == "upload":
        upload(d)
    elif a.action == "warehouse":
        ensure_warehouse(d)
    elif a.action == "run-batch":
        if a.mode == "preview":
            raise SystemExit("run-batch supports --mode write or verify")
        run_notebook(d, "01_generate_batch_sources", {"mode": a.mode, "profile": a.profile, "catalog": c["catalog"],
                                                      "schema": c["schema"]}, f"appareliq-generate-batch-sources-{a.mode}")
    elif a.action == "run-erp":
        if a.mode == "verify":
            raise SystemExit("run-erp supports --mode write or preview")
        run_notebook(d, "02_load_erp_wms_sql", {"mode": a.mode, "profile": a.profile,
                                                "sql_server": cfg["sql"]["server_fqdn"],
                                                "sql_database": cfg["sql"]["database"],
                                                "service_credential": c["service_credential"]},
                     f"appareliq-load-erp-wms-sql-{a.mode}")
    elif a.action == "run-sql-grants":
        fc = cfg["fabric"]
        assert fc.get("sql_sp_app_id"), "run scripts/setup_fabric.py sql-principal first"
        run_notebook(d, "03_apply_sql_script", {
            "sql_server": cfg["sql"]["server_fqdn"], "sql_database": cfg["sql"]["database"],
            "service_credential": c["service_credential"], "script": "03_fabric_mirroring.sql",
            "substitutions": json.dumps({"FABRIC_PRINCIPAL_NAME": fc["sql_sp_name"],
                                         "FABRIC_PRINCIPAL_APP_ID": fc["sql_sp_app_id"]})},
            "appareliq-sql-fabric-mirroring-grants")
    elif a.action == "run-sql-diag":
        run_notebook(d, "03_apply_sql_script", {
            "sql_server": cfg["sql"]["server_fqdn"], "sql_database": cfg["sql"]["database"],
            "service_credential": c["service_credential"], "script": "90_mirroring_diagnostics.sql",
            "substitutions": "{}"}, "appareliq-sql-mirroring-diagnostics")
    elif a.action == "run-sql-agent":
        # Step 9: agent schema + gateway (05), persona map (06) and the managed identity's grants (07) in one cluster run.
        g, ag = cfg["agent_users_group"], cfg.get("agent", {})
        subs = {}
        safe = re.compile(r"[A-Za-z0-9 _.@\-]+")

        def q(v):  # same allow-list the 03 notebook applies to substitutions
            assert safe.fullmatch(str(v)), f"unsafe persona_map value: {v!r}"
            return str(v)
        rows = []
        for pid, u in g["persona_map"].items():
            uuid.UUID(u["object_id"])  # fail fast on a malformed object id
            rows.append(f"    (N'{q(pid)}', N'{q(u['upn'])}', '{u['object_id']}', N'{q(u.get('display_name', pid))}')")
        persona_rows = ",\n".join(rows)  # multi-line, so rendered here rather than as a notebook substitution
        fb = g["fallback_user"]
        subs.update({"UPN_FALLBACK": fb["upn"], "OID_FALLBACK": fb["object_id"],
                     "NAME_FALLBACK": fb.get("display_name", "Escalation delegate")})
        op = g.get("demo_operator") or {}
        subs.update({"UPN_OPERATOR": op.get("upn", ""), "OID_OPERATOR": op.get("object_id", ""),
                     "NAME_OPERATOR": op.get("display_name", "Demo operator")})
        scripts = ["05_agent.sql", "06_agent_personas.sql"]
        if ag.get("identity_client_id"):
            scripts.append("07_agent_grants.sql")
            subs.update({"AGENT_PRINCIPAL_NAME": ag["identity_name"], "AGENT_CLIENT_ID": ag["identity_client_id"]})
        else:
            print("agent.identity_client_id not set yet: skipping 07_agent_grants.sql (deploy infra/agent.bicep first)")
        upload(d)
        bundle = "\nGO\n".join((ROOT / "sql" / "azure_sql" / s).read_text(encoding="utf-8") for s in scripts)
        bundle = bundle.replace("{{PERSONA_MAP_ROWS}}", persona_rows)
        _import(d, f"{WS_ROOT}/sql/_agent_bundle.sql", bundle.encode("utf-8"), "AUTO")
        run_notebook(d, "03_apply_sql_script", {
            "sql_server": cfg["sql"]["server_fqdn"], "sql_database": cfg["sql"]["database"],
            "service_credential": c["service_credential"], "script": "_agent_bundle.sql",
            "substitutions": json.dumps(subs)}, "appareliq-sql-agent")
    elif a.action in ("close-case", "reset-store-transfers"):
        # Demo resets live in notebook 05_demo_reset; --mode preview rolls back and only shows what would change.
        if a.action == "close-case":
            assert a.case_id and re.fullmatch(r"[A-Za-z0-9\-]+", a.case_id), "--case-id is required (e.g. C-261110-HM9443-US-01)"
            args = {"action": "close_case", "case_id": a.case_id}
        else:
            assert re.fullmatch(r"[A-Z0-9]{2,16}", a.style_id) and re.fullmatch(r"[A-Z]{2}", a.market_id), "bad --style-id/--market-id"
            args = {"action": "reset_store_transfers", "style_id": a.style_id, "market_id": a.market_id}
        mode = "preview" if a.mode == "preview" else "apply"
        upload(d)
        run_notebook(d, "05_demo_reset", {**args, "mode": mode, "sql_server": cfg["sql"]["server_fqdn"],
                                          "sql_database": cfg["sql"]["database"],
                                          "service_credential": c["service_credential"]},
                     f"appareliq-demo-reset-{args['action'].replace('_', '-')}-{mode}")
    elif a.action == "test-sql-agent":
        # Step 9: gateway contract tests against the private SQL database; the notebook always cleans up.
        upload(d)
        out = run_notebook(d, "04_test_agent_gateway", {
            "sql_server": cfg["sql"]["server_fqdn"], "sql_database": cfg["sql"]["database"],
            "service_credential": c["service_credential"],
            "agent_principal": cfg.get("agent", {}).get("identity_name", "id-appareliq-agent")}, "appareliq-sql-agent-test")
        res = json.loads((out or {}).get("notebook_output", {}).get("result") or "{}")
        if res.get("failed") or not res.get("total"):
            raise SystemExit(f"gateway tests failed: {json.dumps(res.get('failed'), indent=1)}")


if __name__ == "__main__":
    main()
