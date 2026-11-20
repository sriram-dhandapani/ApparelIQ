"""Idempotent Microsoft Fabric setup for the ApparelIQ demo (Phase 2 tasks 0 and 2).

  python scripts/setup_fabric.py workspace        # workspace on the F capacity + workspace identity + lakehouses
  python scripts/setup_fabric.py dbx-mirror       # Databricks (SP) + ADLS (workspace identity) connections, mirrored UC catalog, shortcuts
  python scripts/setup_fabric.py sql-principal    # Entra SP for SQL mirroring; then: setup_databricks.py run-sql-grants
  python scripts/setup_fabric.py sql-mirror       # VNet data gateway + SQL connection + mirrored Azure SQL database + shortcuts
  python scripts/setup_fabric.py medallion --run  # lh_medallion (schemas) + code upload + notebooks 01-04 + pl_medallion; run 01..04
  python scripts/setup_fabric.py medallion --via-pipeline   # same deployment, executed through the pl_medallion pipeline
  python scripts/setup_fabric.py verify           # sample T-SQL queries over the lakehouse SQL endpoints
  python scripts/setup_fabric.py dataagent        # data agent (ontology + semantic model), publish, MCP benchmark
  python scripts/setup_fabric.py dataagent --benchmark-only [--cases B07 B14]
Auth: Entra tokens from the Azure CLI. The ADLS connection uses the Fabric workspace identity. Databricks mirroring and
VNet-gateway SQL connections do not accept workspace identity, so dedicated single-tenant service principals are used;
their 90-day secrets are minted at connection-creation time and passed only to Fabric (never written to disk).
Environment-specific values come from the git-ignored config/deployment.local.json; generated ids are written back to it.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
F = "https://api.fabric.microsoft.com/v1"
DBX_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
LAKEHOUSES = {"lh_databricks": "Shortcuts to the mirrored Databricks UC catalog (DS-01, DS-02) - SYNTHETIC",
              "lh_erp_wms": "Shortcuts to the mirrored ERP/WMS Azure SQL database - SYNTHETIC"}


def az_token(tenant: str, resource: str) -> str:
    return subprocess.run(["az", "account", "get-access-token", "--tenant", tenant, "--resource", resource,
                           "--query", "accessToken", "-o", "tsv"], capture_output=True, text=True, check=True,
                          shell=sys.platform == "win32").stdout.strip()


class Fabric:
    def __init__(self, cfg: dict, cfg_path: Path):
        self.cfg, self.cfg_path = cfg, cfg_path
        self.s = requests.Session()
        self._t = 0.0

    def _auth(self):
        if time.time() - self._t > 40 * 60:
            self.s.headers["Authorization"] = "Bearer " + az_token(self.cfg["tenant_id"], "https://api.fabric.microsoft.com")
            self._t = time.time()

    def call(self, method: str, path: str, ok=(200, 201, 202), **kw):
        self._auth()
        url = path if path.startswith("http") else F + path
        for attempt in range(6):
            r = self.s.request(method, url, timeout=120, **kw)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "30")))
                continue
            break
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
        if r.status_code == 202 and r.headers.get("Location"):
            return self._lro(r)
        return r.json() if r.text else {}

    def _lro(self, r):
        loc = r.headers["Location"]
        while True:
            time.sleep(int(r.headers.get("Retry-After", "5")))
            r = self.s.get(loc, timeout=60)
            body = r.json() if r.text else {}
            status = body.get("status")
            if status in ("Succeeded", "Failed", "Undefined") or r.status_code == 200 and not status:
                if status == "Failed":
                    raise RuntimeError(f"LRO failed: {body}")
                res = self.s.get(loc.rstrip("/") + "/result", timeout=60) if status == "Succeeded" else None
                return res.json() if res is not None and res.ok and res.text else body

    def save(self):
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- helpers
    def items(self, ws: str, item_type: str | None = None):
        q = f"?type={item_type}" if item_type else ""
        return self.call("GET", f"/workspaces/{ws}/items{q}").get("value", [])

    def ensure_item(self, ws: str, item_type: str, name: str, description: str = "", definition: dict | None = None,
                    creation_payload: dict | None = None):
        for it in self.items(ws, item_type):
            if it["displayName"] == name:
                print(f"{item_type} {name} exists: {it['id']}")
                return it
        body = {"displayName": name, "type": item_type, "description": description}
        if definition:
            body["definition"] = definition
        if creation_payload:
            body["creationPayload"] = creation_payload
        it = self.call("POST", f"/workspaces/{ws}/items", json=body)
        if not it.get("id"):
            it = next(i for i in self.items(ws, item_type) if i["displayName"] == name)
        print(f"{item_type} {name} created: {it['id']}")
        return it

    def find_connection(self, name: str):
        return next((c for c in self.call("GET", "/connections").get("value", []) if c["displayName"] == name), None)

    def ensure_connection(self, name: str, conn_type: str, method: str, params: list[dict], credential_type: str,
                          gateway_id: str | None = None, connectivity: str = "ShareableCloud",
                          credentials: dict | None = None, encryption: str = "NotEncrypted"):
        existing = self.find_connection(name)
        if existing:
            print(f"connection {name} exists: {existing['id']}")
            return existing
        body = {
            "connectivityType": connectivity,
            "displayName": name,
            "connectionDetails": {"type": conn_type, "creationMethod": method, "parameters": params},
            "privacyLevel": "Organizational",
            "credentialDetails": {"singleSignOnType": "None", "connectionEncryption": encryption,
                                  "skipTestConnection": False,
                                  "credentials": {"credentialType": credential_type, **(credentials or {})}},
        }
        if gateway_id:
            body["gatewayId"] = gateway_id
        c = self.call("POST", "/connections", json=body)
        print(f"connection {name} created: {c['id']}")
        return c


def b64(obj) -> str:
    return base64.b64encode((json.dumps(obj) if not isinstance(obj, str) else obj).encode()).decode()


# -------------------------------------------------------------------- workspace
def setup_workspace(fb: Fabric):
    fc = fb.cfg["fabric"]
    name = fc.get("workspace_name", "ApparelIQ")
    ws = next((w for w in fb.call("GET", "/workspaces").get("value", []) if w["displayName"] == name), None)
    if not ws:
        ws = fb.call("POST", "/workspaces", json={"displayName": name, "capacityId": fc["capacity_id"],
                                                  "description": "ApparelIQ UC1 inventory rebalancing demo - SYNTHETIC data"})
        print("workspace created:", ws["id"])
    else:
        print("workspace exists:", ws["id"])
    wsid = ws["id"]
    info = fb.call("GET", f"/workspaces/{wsid}")
    if info.get("capacityId", "").lower() != fc["capacity_id"].lower():
        fb.call("POST", f"/workspaces/{wsid}/assignToCapacity", json={"capacityId": fc["capacity_id"]})
        print("assigned to capacity", fc["capacity_id"])
    ident = info.get("workspaceIdentity")
    if not ident:
        fb.call("POST", f"/workspaces/{wsid}/provisionIdentity")
        for _ in range(30):
            ident = fb.call("GET", f"/workspaces/{wsid}").get("workspaceIdentity")
            if ident:
                break
            time.sleep(10)
    print("workspace identity:", ident)
    fc["workspace_id"] = wsid
    fc["workspace_identity_app_id"] = ident["applicationId"]
    fc["workspace_identity_sp_id"] = ident["servicePrincipalId"]
    fc.setdefault("lakehouses", {})
    for lh, desc in LAKEHOUSES.items():
        it = fb.ensure_item(wsid, "Lakehouse", lh, desc)
        fc["lakehouses"][lh] = it["id"]
    fb.save()


# -------------------------------------------------------------------- Databricks mirror
DBX_SP_NAME = "sp-appareliq-fabric-dbx"
UC_GRANTS = [("catalog", "{catalog}", ["USE_CATALOG"]),
             ("schema", "{catalog}.{schema}", ["USE_SCHEMA", "SELECT", "EXTERNAL_USE_SCHEMA"])]


def az_json(*args: str):
    out = subprocess.run(["az", *args, "-o", "json"], capture_output=True, text=True, check=True,
                         shell=sys.platform == "win32").stdout
    return json.loads(out) if out.strip() else None


def grant_databricks_principal(cfg: dict, application_id: str, display_name: str):
    """Register an Entra service principal in the Databricks workspace and grant it read access to the UC schema."""
    dc = cfg["databricks"]
    w = "https://" + dc["workspace_url"].removeprefix("https://")
    h = {"Authorization": "Bearer " + az_token(cfg["tenant_id"], DBX_RESOURCE)}
    found = requests.get(f"{w}/api/2.0/preview/scim/v2/ServicePrincipals", headers=h, timeout=60,
                         params={"filter": f'applicationId eq "{application_id}"'}).json()
    if not found.get("Resources"):
        r = requests.post(f"{w}/api/2.0/preview/scim/v2/ServicePrincipals", headers=h, timeout=60, json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal"], "applicationId": application_id,
            "displayName": display_name,
            "entitlements": [{"value": "workspace-access"}, {"value": "databricks-sql-access"}]})
        r.raise_for_status()
        print("databricks principal registered:", display_name)
    for securable, full, privs in UC_GRANTS:
        full = full.format(**dc)
        r = requests.patch(f"{w}/api/2.1/unity-catalog/permissions/{securable}/{full}", headers=h, timeout=60,
                           json={"changes": [{"principal": application_id, "add": privs}]})
        r.raise_for_status()
        print(f"granted {privs} on {securable} {full} to {display_name}")


def ensure_service_principal(fb: Fabric, name: str, cfg_key: str, mint_secret: bool = True) -> tuple[str, str | None]:
    """Single-tenant Entra app for a Fabric connection that cannot use workspace identity.

    Returns (appId, secret). A new 90-day secret is minted only when requested and handed straight to the
    Fabric connection; it is never written to disk."""
    apps = az_json("ad", "app", "list", "--display-name", name, "--query", "[].appId")
    app_id = apps[0] if apps else az_json("ad", "app", "create", "--display-name", name,
                                          "--sign-in-audience", "AzureADMyOrg", "--query", "appId")
    if not az_json("ad", "sp", "list", "--filter", f"appId eq '{app_id}'", "--query", "[].id"):
        az_json("ad", "sp", "create", "--id", app_id, "--query", "id")
    fb.cfg["fabric"][cfg_key] = app_id
    fb.save()
    if not mint_secret:
        return app_id, None
    secret = az_json("ad", "app", "credential", "reset", "--id", app_id, "--append",
                     "--display-name", "fabric-connection", "--end-date",
                     time.strftime("%Y-%m-%d", time.gmtime(time.time() + 90 * 86400)), "--query", "password")
    return app_id, secret


def ensure_dbx_service_principal(fb: Fabric) -> tuple[str, str]:
    app_id, secret = ensure_service_principal(fb, DBX_SP_NAME, "databricks_sp_app_id")
    grant_databricks_principal(fb.cfg, app_id, DBX_SP_NAME)
    return app_id, secret


def setup_dbx_mirror(fb: Fabric, recreate: bool = False):
    cfg, fc, dc = fb.cfg, fb.cfg["fabric"], fb.cfg["databricks"]
    wsid = fc["workspace_id"]
    host = "https://" + dc["workspace_url"].removeprefix("https://")
    dbx_name = "appareliq-databricks-sp"
    # A failed SQL endpoint provisioning leaves an undeletable orphan SQLEndpoint that blocks the item name,
    # so the mirrored catalog name is configurable.
    mc_name = fc.setdefault("mirrored_catalog_name", "mc_appareliq")
    dbx_conn = fb.find_connection(dbx_name)
    if not dbx_conn:
        app_id, secret = ensure_dbx_service_principal(fb)
        dbx_conn = fb.ensure_connection(
            dbx_name, "AzureDatabricksWorkspace", "AzureDatabricksWorkspace.Actions",
            [{"dataType": "Text", "name": "url", "value": host}], "ServicePrincipal",
            credentials={"tenantId": cfg["tenant_id"], "servicePrincipalClientId": app_id,
                         "servicePrincipalSecret": secret})
    st = cfg["storage"]["account_name"]
    adls_conn = fb.ensure_connection(
        "appareliq-adls-unity-wi", "AzureDataLakeStorage", "AzureDataLakeStorage",
        [{"dataType": "Text", "name": "server", "value": f"https://{st}.dfs.core.windows.net"},
         {"dataType": "Text", "name": "path", "value": f"/unity/{dc['catalog']}"}], "WorkspaceIdentity")
    fc["connections"] = {**fc.get("connections", {}), "databricks": dbx_conn["id"], "adls": adls_conn["id"]}
    fb.save()
    if recreate:
        old = next((i for i in fb.items(wsid, "MirroredAzureDatabricksCatalog")
                    if i["id"] == fc.get("mirrored_catalog_id") or i["displayName"] == mc_name), None)
        if old:
            fb.call("DELETE", f"/workspaces/{wsid}/items/{old['id']}", ok=(200,))
            print("deleted MirroredAzureDatabricksCatalog", old["displayName"], old["id"])
            time.sleep(20)
    definition = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/mirroredAzureDatabricksCatalog/definition/mirroredAzureDatabricksCatalogDefinition/1.0.0/schema.json",
        "catalogName": dc["catalog"],
        "databricksWorkspaceConnectionId": dbx_conn["id"],
        "storageConnectionId": adls_conn["id"],
        "autoSync": "Enabled",
        "mirroringMode": "Partial",
        "mirrorConfiguration": {"schemas": [{"name": dc["schema"], "mirroringMode": "Full"}]},
    }
    item = fb.ensure_item(wsid, "MirroredAzureDatabricksCatalog", mc_name,
                          "Mirrored Unity Catalog appareliq.kinetiq_batch (SYNTHETIC)",
                          definition={"parts": [{"path": "definition.json", "payload": b64(definition),
                                                 "payloadType": "InlineBase64"}]})
    fc["mirrored_catalog_id"] = item["id"]
    fb.save()
    check_mirrored_catalog(fb, wsid, item["id"])
    tables = ["sell_through_daily", "digital_intent_daily"]
    shortcut_tables(fb, wsid, fc["lakehouses"]["lh_databricks"], item["id"], [f"Tables/{dc['schema']}/{t}" for t in tables])


def check_mirrored_catalog(fb: Fabric, wsid: str, item_id: str):
    """The item's SQL analytics endpoint is provisioned asynchronously; if that fails the GET returns
    SQLEndpointCreationFailed and the endpoint is orphaned. Fix with: dbx-mirror --recreate."""
    for _ in range(24):
        try:
            props = fb.call("GET", f"/workspaces/{wsid}/mirroredAzureDatabricksCatalogs/{item_id}").get("properties", {})
        except RuntimeError as e:
            if "SQLEndpointCreationFailed" in str(e):
                raise SystemExit("mirrored catalog SQL endpoint creation failed; re-run: dbx-mirror --recreate")
            raise
        ep = props.get("sqlEndpointProperties", {})
        if ep.get("id") and ep.get("provisioningStatus", "Success") == "Success":
            print("mirrored catalog SQL endpoint:", ep["id"], ep.get("provisioningStatus"))
            return
        time.sleep(10)
    raise SystemExit(f"mirrored catalog SQL endpoint not ready: {props}")


def shortcut_tables(fb: Fabric, wsid: str, lakehouse_id: str, target_item: str, target_paths: list[str]):
    existing = {s["name"]: s for s in fb.call("GET", f"/workspaces/{wsid}/items/{lakehouse_id}/shortcuts").get("value", [])}
    for p in target_paths:
        name = p.rsplit("/", 1)[-1]
        cur = existing.get(name, {}).get("target", {}).get("oneLake", {})
        if cur.get("itemId") == target_item and cur.get("path") == p:
            print("shortcut exists:", name)
            continue
        # CreateOrOverwrite retargets shortcuts left pointing at a deleted/recreated source item.
        fb.call("POST", f"/workspaces/{wsid}/items/{lakehouse_id}/shortcuts?shortcutConflictPolicy=CreateOrOverwrite",
                json={"path": "Tables", "name": name,
                      "target": {"oneLake": {"workspaceId": wsid, "itemId": target_item, "path": p}}})
        print("shortcut created:", name, "->", p)


VERIFY_QUERIES = {
    "lh_databricks": [
        ("sell_through_daily", "SELECT COUNT_BIG(*) AS row_count, COUNT(DISTINCT location_id) AS locations, MIN(business_date) AS min_date, MAX(business_date) AS max_date FROM dbo.sell_through_daily"),
        ("digital_intent_daily", "SELECT COUNT_BIG(*) AS row_count FROM dbo.digital_intent_daily"),
    ],
    "lh_erp_wms": [
        ("inventory_position", "SELECT COUNT_BIG(*) AS row_count, SUM(CAST(on_hand_units AS BIGINT)) AS on_hand_units FROM dbo.inventory_position"),
        ("on_hand_by_location_type", "SELECT l.location_type, COUNT(DISTINCT l.location_id) AS locations, SUM(CAST(p.on_hand_units AS BIGINT)) AS on_hand_units "
                                     "FROM dbo.inventory_position p JOIN dbo.location l ON l.location_id = p.location_id GROUP BY l.location_type ORDER BY 1"),
    ],
    "lh_medallion": [
        ("table_counts", "SELECT s.name AS layer, COUNT(*) AS tables FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
                         "WHERE s.name IN ('bronze','silver','gold') GROUP BY s.name ORDER BY 1"),
        ("bronze_zero_copy", "SELECT 'bronze.dbx_sell_through_daily' AS shortcut, COUNT_BIG(*) AS row_count FROM bronze.dbx_sell_through_daily "
                             "UNION ALL SELECT 'bronze.wms_inventory_position', COUNT_BIG(*) FROM bronze.wms_inventory_position"),
        ("silver_dq_rules", "SELECT rule_id, SUM(rows_affected) AS rows_affected FROM silver.dq_rule_summary GROUP BY rule_id ORDER BY rule_id"),
        ("gold_triggers", "SELECT trigger_id, market_id, COUNT(*) AS events FROM gold.trigger_event GROUP BY trigger_id, market_id ORDER BY 1, 2"),
        ("gold_t3_pairs", "SELECT sku_id, source_market_id, destination_market_id, proposed_units, lane_mode, arrives_before_stockout, "
                          "within_margin_guardrail FROM gold.mart_rebalancing_pairs ORDER BY sku_id"),
        ("gold_checks", "SELECT layer, SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) AS passed, COUNT(*) AS checks FROM gold.dq_check_result GROUP BY layer"),
    ],
}


def sql_endpoint(fb: Fabric, wsid: str, lakehouse_id: str) -> str:
    for _ in range(30):
        props = fb.call("GET", f"/workspaces/{wsid}/lakehouses/{lakehouse_id}").get("properties", {})
        ep = props.get("sqlEndpointProperties") or {}
        if ep.get("provisioningStatus") == "Success" and ep.get("connectionString"):
            return ep["connectionString"]
        time.sleep(10)
    raise RuntimeError("SQL analytics endpoint not ready")


def run_tsql(cfg: dict, server: str, database: str, query: str):
    import struct
    import pyodbc
    tok = az_token(cfg["tenant_id"], "https://database.windows.net/").encode("utf-16-le")
    attrs = {1256: struct.pack("<i", len(tok)) + tok}  # SQL_COPT_SS_ACCESS_TOKEN
    cn = pyodbc.connect(f"Driver={{ODBC Driver 18 for SQL Server}};Server={server},1433;Database={database};"
                        "Encrypt=yes;TrustServerCertificate=no", attrs_before=attrs, timeout=60)
    try:
        cur = cn.cursor().execute(query)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cn.close()


def verify(fb: Fabric, only: str | None = None):
    fc = fb.cfg["fabric"]
    wsid = fc["workspace_id"]
    for lh, queries in VERIFY_QUERIES.items():
        if only and lh != only or lh not in fc.get("lakehouses", {}):
            continue
        server = sql_endpoint(fb, wsid, fc["lakehouses"][lh])
        for label, q in queries:
            for attempt in range(12):
                try:
                    print(f"[{lh}] {label}: {run_tsql(fb.cfg, server, lh, q)}")
                    break
                except Exception as e:  # shortcut metadata sync into the SQL endpoint can lag a few minutes
                    if attempt == 11:
                        raise
                    print(f"[{lh}] {label}: waiting for SQL endpoint sync ({str(e)[:120]})")
                    time.sleep(30)


ERP_WMS_TABLES = ["ref.market", "ref.person", "ref.decision_owner", "ref.synthetic_run", "erp.location",
                  "erp.product_style", "erp.product_sku", "erp.price", "erp.vendor", "erp.wholesale_partner",
                  "erp.wholesale_order", "erp.wholesale_order_line", "erp.purchase_order", "erp.purchase_order_line",
                  "erp.margin_guardrail", "erp.regional_event", "wms.inventory_position", "wms.transfer_lane",
                  "wms.inventory_movement", "erp.allocation_decision", "erp.stock_transfer_order"]


def ensure_vnet_gateway(fb: Fabric) -> str:
    cfg, fc = fb.cfg, fb.cfg["fabric"]
    name = fc.get("vnet_gateway_name", "vngw-appareliq")
    gw = next((g for g in fb.call("GET", "/gateways").get("value", []) if g.get("displayName") == name), None)
    if not gw:
        gw = fb.call("POST", "/gateways", json={
            "type": "VirtualNetwork", "displayName": name, "capacityId": fc["capacity_id"],
            "virtualNetworkAzureResource": {"subscriptionId": cfg["subscription_id"],
                                            "resourceGroupName": cfg["resource_group"],
                                            "virtualNetworkName": fc.get("vnet_name", "vnet-appareliq"),
                                            "subnetName": fc.get("gateway_subnet", "snet-fabric-gw")},
            "inactivityMinutesBeforeSleep": 30, "numberOfMemberGateways": 1})
        print("vnet gateway created:", gw["id"])
    else:
        print("vnet gateway exists:", gw["id"])
    fc["vnet_gateway_id"] = gw["id"]
    fb.save()
    return gw["id"]


SQL_SP_NAME = "sp-appareliq-fabric-sql"


def setup_sql_principal(fb: Fabric):
    """Create the SQL mirroring service principal (no secret). Then run
    `setup_databricks.py run-sql-grants` to create its least-privilege database user inside the VNet."""
    app_id, _ = ensure_service_principal(fb, SQL_SP_NAME, "sql_sp_app_id", mint_secret=False)
    fb.cfg["fabric"]["sql_sp_name"] = SQL_SP_NAME
    fb.save()
    print(f"{SQL_SP_NAME}: appId {app_id}. Next: python scripts/setup_databricks.py run-sql-grants")


def grant_sql_identity(fb: Fabric, wsid: str):
    """The Azure SQL change feed writes to the mirror's OneLake landing zone as the server's system-assigned identity.
    Without a workspace role, sys.dm_change_feed_errors shows 22736 "Error occurred while interacting with landing
    zone" and tables stay 'Initialized' forever. Contributor is the least role that can write to OneLake items."""
    cfg = fb.cfg
    pid = az_json("sql", "server", "show", "--subscription", cfg["subscription_id"], "-g", cfg["resource_group"],
                  "-n", cfg["sql"]["server_fqdn"].split(".")[0], "--query", "identity.principalId")
    roles = fb.call("GET", f"/workspaces/{wsid}/roleAssignments").get("value", [])
    if any(r["principal"]["id"] == pid for r in roles):
        print("SQL server identity already has a workspace role")
        return
    fb.call("POST", f"/workspaces/{wsid}/roleAssignments",
            json={"principal": {"id": pid, "type": "ServicePrincipal"}, "role": "Contributor"})
    print("granted workspace Contributor to SQL server identity", pid)


def setup_sql_mirror(fb: Fabric):
    cfg, fc, sc = fb.cfg, fb.cfg["fabric"], fb.cfg["sql"]
    wsid = fc["workspace_id"]
    gw = ensure_vnet_gateway(fb)
    # VNet data gateway connections reject WorkspaceIdentity (DMTS_InvalidCredentialTypeError) -> service principal.
    conn_name = "appareliq-erpwms-sql-sp"
    conn = fb.find_connection(conn_name)
    if not conn:
        assert fc.get("sql_sp_app_id"), "run `setup_fabric.py sql-principal` and `setup_databricks.py run-sql-grants` first"
        app_id, secret = ensure_service_principal(fb, SQL_SP_NAME, "sql_sp_app_id")
        conn = fb.ensure_connection(
            conn_name, "SQL", "Sql",
            [{"dataType": "Text", "name": "server", "value": sc["server_fqdn"]},
             {"dataType": "Text", "name": "database", "value": sc["database"]}],
            "ServicePrincipal", gateway_id=gw, connectivity="VirtualNetworkGateway", encryption="Encrypted",
            credentials={"tenantId": cfg["tenant_id"], "servicePrincipalClientId": app_id,
                         "servicePrincipalSecret": secret})
    fc.setdefault("connections", {})["sql"] = conn["id"]
    fb.save()
    mirroring = {"properties": {
        "source": {"type": "AzureSqlDatabase", "typeProperties": {"connection": conn["id"], "database": sc["database"]}},
        "target": {"type": "MountedRelationalDatabase", "typeProperties": {"defaultSchema": "dbo", "format": "Delta"}}}}
    item = fb.ensure_item(wsid, "MirroredDatabase", "md_erp_wms", "Mirrored ERP/WMS Azure SQL database (SYNTHETIC)",
                          definition={"parts": [{"path": "mirroring.json", "payload": b64(mirroring),
                                                 "payloadType": "InlineBase64"}]})
    mid = item["id"]
    fc["mirrored_database_id"] = mid
    fb.save()
    grant_sql_identity(fb, wsid)
    for _ in range(20):  # startMirroring is a no-op until the new item finishes provisioning
        status = fb.call("POST", f"/workspaces/{wsid}/mirroredDatabases/{mid}/getMirroringStatus").get("status")
        if status in ("Running", "Starting"):
            break
        fb.call("POST", f"/workspaces/{wsid}/mirroredDatabases/{mid}/startMirroring")
        print("mirroring start requested; status was", status)
        time.sleep(30)
    for _ in range(60):
        st = fb.call("POST", f"/workspaces/{wsid}/mirroredDatabases/{mid}/getTablesMirroringStatus").get("data", [])
        done = [t for t in st if t.get("status") == "Replicating"]  # write-back tables are legitimately empty
        failed = [t for t in st if t.get("status") == "Failed"]
        print(f"{time.strftime('%H:%M:%S')} mirroring: {len(done)}/{len(st)} tables replicating, {len(failed)} failed")
        if failed:
            raise RuntimeError(json.dumps(failed, indent=1)[:2000])
        if st and len(done) == len(st):
            break
        time.sleep(30)
    shortcut_tables(fb, wsid, fc["lakehouses"]["lh_erp_wms"], mid,
                    [f"Tables/{t.split('.')[0]}/{t.split('.')[1]}" for t in ERP_WMS_TABLES])


# -------------------------------------------------------------------- medallion (Phase 2 step 3)
MEDALLION_LH = "lh_medallion"
SETUP_NOTEBOOK = "00_lh_medallion_setup"   # owns everything in lh_medallion: lakehouse, Bronze shortcuts, cleanup
NOTEBOOKS = ["01_bronze_generate_sources", "02_bronze_validate_mirrors", "03_silver_transform", "04_gold_build"]
RETIRED_NOTEBOOKS = ["02_bronze_ingest_mirrors",   # copied the mirrors into Bronze; replaced by zero-copy shortcuts
                     "01_bronze_generate_files"]   # wrote landing files first; 01 now generates straight into Bronze


def deploy_notebook(fb: Fabric, wsid: str, lh_id: str | None, name: str) -> str:
    nb = json.loads((ROOT / "fabric" / "notebooks" / f"{name}.ipynb").read_text(encoding="utf-8"))
    if lh_id:
        nb["metadata"]["dependencies"] = {"lakehouse": {"default_lakehouse": lh_id, "default_lakehouse_name": MEDALLION_LH,
                                                        "default_lakehouse_workspace_id": wsid}}
    definition = {"format": "ipynb", "parts": [{"path": "notebook-content.ipynb", "payload": b64(nb),
                                                "payloadType": "InlineBase64"}]}
    existing = next((i for i in fb.items(wsid, "Notebook") if i["displayName"] == name), None)
    if existing:
        fb.call("POST", f"/workspaces/{wsid}/notebooks/{existing['id']}/updateDefinition", json={"definition": definition})
        print(f"notebook {name} updated: {existing['id']}")
        return existing["id"]
    return fb.ensure_item(wsid, "Notebook", name, "ApparelIQ medallion - SYNTHETIC data", definition=definition)["id"]


def deploy_pipeline(fb: Fabric, wsid: str, nb_ids: dict) -> str:
    text = (ROOT / "fabric" / "pipelines" / "pl_medallion.json").read_text(encoding="utf-8")
    text = text.replace("{{workspace_id}}", wsid)
    for n, i in nb_ids.items():
        text = text.replace("{{notebook:%s}}" % n, i)
    definition = {"parts": [{"path": "pipeline-content.json", "payload": b64(text), "payloadType": "InlineBase64"}]}
    existing = next((i for i in fb.items(wsid, "DataPipeline") if i["displayName"] == "pl_medallion"), None)
    if existing:
        fb.call("POST", f"/workspaces/{wsid}/items/{existing['id']}/updateDefinition", json={"definition": definition})
        print("pipeline pl_medallion updated:", existing["id"])
        return existing["id"]
    return fb.ensure_item(wsid, "DataPipeline", "pl_medallion", "Bronze -> Silver -> Gold (SYNTHETIC)", definition)["id"]


def run_job(fb: Fabric, wsid: str, item_id: str, job_type: str, label: str, timeout_min: int = 90):
    fb._auth()
    r = fb.s.post(f"{F}/workspaces/{wsid}/items/{item_id}/jobs/instances?jobType={job_type}", timeout=60)
    if r.status_code != 202:
        raise RuntimeError(f"start {label} -> {r.status_code}: {r.text[:500]}")
    loc, t0 = r.headers["Location"], time.time()
    print(f"[{label}] started")
    while time.time() - t0 < timeout_min * 60:
        time.sleep(20)
        fb._auth()
        st = fb.s.get(loc, timeout=60).json()
        status = st.get("status")
        if status in ("Completed", "Failed", "Cancelled", "Deduped"):
            print(f"[{label}] {status} after {time.time() - t0:.0f}s")
            if status != "Completed":
                raise SystemExit(f"[{label}] {status}: {json.dumps(st.get('failureReason'), indent=1)}")
            return st
    raise SystemExit(f"[{label}] timed out")


def setup_medallion(fb: Fabric, run: list[str] | None, via_pipeline: bool):
    """Deploys the medallion notebooks and pipeline only. Everything inside lh_medallion (the lakehouse itself, the
    zero-copy Bronze shortcuts, cleanup) is done by notebook 00_lh_medallion_setup, the first pl_medallion activity."""
    fc = fb.cfg["fabric"]
    wsid = fc["workspace_id"]
    setup_id = deploy_notebook(fb, wsid, None, SETUP_NOTEBOOK)   # no default lakehouse: it may not exist yet
    lh = next((i for i in fb.items(wsid, "Lakehouse") if i["displayName"] == MEDALLION_LH), None)
    ran_setup = False
    if lh is None:   # first deployment: 01-04 must be bound to the lakehouse, so let notebook 00 create it now
        run_job(fb, wsid, setup_id, "RunNotebook", SETUP_NOTEBOOK)
        ran_setup = True
        lh = next(i for i in fb.items(wsid, "Lakehouse") if i["displayName"] == MEDALLION_LH)
    fc.setdefault("lakehouses", {})[MEDALLION_LH] = lh["id"]
    nb_ids = {SETUP_NOTEBOOK: setup_id} | {n: deploy_notebook(fb, wsid, lh["id"], n) for n in NOTEBOOKS}
    fc["notebooks"] = nb_ids
    fc["pipelines"] = {"pl_medallion": deploy_pipeline(fb, wsid, nb_ids)}
    for old in (i for i in fb.items(wsid, "Notebook") if i["displayName"] in RETIRED_NOTEBOOKS):
        fb.call("DELETE", f"/workspaces/{wsid}/items/{old['id']}")
        print("deleted retired notebook", old["displayName"])
    fb.save()
    if via_pipeline:
        run_job(fb, wsid, fc["pipelines"]["pl_medallion"], "Pipeline", "pl_medallion", timeout_min=180)
    elif run is not None:
        for n in [n for n in nb_ids if not run or n in run or n[:2] in run]:
            if not (n == SETUP_NOTEBOOK and ran_setup):
                run_job(fb, wsid, nb_ids[n], "RunNotebook", n)


# -------------------------------------------------------------------- real-time intelligence (step 4)
EVENTHOUSE = "eh_appareliq"
KQL_DB = "kdb_appareliq"
EVENTSTREAM = "es-appareliq-rt"            # no underscores: they break the Eventstream SQL operator
RT_NOTEBOOK = "05_rt_event_simulator"
SIGNAL_NOTEBOOK = "06_rt_inventory_signal"  # appends rt_inventory_signal snapshots (ontology time series), scheduled
KUSTO = "https://kusto.kusto.windows.net"


def kusto(fb: Fabric, uri: str, csl: str, mgmt: bool = False):
    r = requests.post(f"{uri}/v1/rest/{'mgmt' if mgmt else 'query'}", json={"db": KQL_DB, "csl": csl}, timeout=300,
                      headers={"Authorization": "Bearer " + az_token(fb.cfg["tenant_id"], KUSTO)})
    if not r.ok:
        raise RuntimeError(f"KQL {csl[:80]!r} -> {r.status_code}: {r.text[:1500]}")
    t = r.json()["Tables"][0]
    return [dict(zip([c["ColumnName"] for c in t["Columns"]], row)) for row in t["Rows"]]


def deploy_kql_schema(fb: Fabric, uri: str, wsid: str, lh_id: str):
    text = (ROOT / "fabric" / "eventhouse" / f"{KQL_DB}.kql").read_text(encoding="utf-8")
    text = text.replace("{{workspace_id}}", wsid).replace("{{lh_medallion_id}}", lh_id)
    script = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
    rows = kusto(fb, uri, ".execute database script with (ContinueOnErrors=true) <|\n" + script, mgmt=True)
    failed = [r for r in rows if r.get("Result") != "Completed"]
    print(f"KQL schema: {len(rows) - len(failed)}/{len(rows)} commands completed")
    if failed:
        raise RuntimeError("KQL schema failures:\n" + "\n".join(f"- {r['CommandText'][:90]}: {r['Reason'][:400]}" for r in failed))


def setup_realtime(fb: Fabric, run: bool):
    fc = fb.cfg["fabric"]
    wsid = fc["workspace_id"]
    lh_id = fc["lakehouses"][MEDALLION_LH]
    eh = fb.ensure_item(wsid, "Eventhouse", EVENTHOUSE, "UC1 real-time signals DS-15..17 (SYNTHETIC)")
    db = fb.ensure_item(wsid, "KQLDatabase", KQL_DB, "POS, e-commerce intent and inventory movement events (SYNTHETIC)",
                        creation_payload={"databaseType": "ReadWrite", "parentEventhouseItemId": eh["id"]})
    for _ in range(30):
        uri = fb.call("GET", f"/workspaces/{wsid}/kqlDatabases/{db['id']}").get("properties", {}).get("queryServiceUri")
        if uri:
            break
        time.sleep(10)
    deploy_kql_schema(fb, uri, wsid, lh_id)

    topo = (ROOT / "fabric" / "eventstream" / f"{EVENTSTREAM}.json").read_text(encoding="utf-8")
    topo = topo.replace("{{workspace_id}}", wsid).replace("{{kql_db_id}}", db["id"]).replace("{{kql_db_name}}", KQL_DB)
    definition = {"parts": [
        {"path": "eventstream.json", "payload": b64(topo), "payloadType": "InlineBase64"},
        {"path": "eventstreamProperties.json", "payloadType": "InlineBase64",
         "payload": b64({"retentionTimeInDays": 1, "eventThroughputLevel": "Low", "schemaMode": "Inferred"})}]}
    es = next((i for i in fb.items(wsid, "Eventstream") if i["displayName"] == EVENTSTREAM), None)
    if es:
        fb.call("POST", f"/workspaces/{wsid}/eventstreams/{es['id']}/updateDefinition", json={"definition": definition})
        print(f"Eventstream {EVENTSTREAM} updated: {es['id']}")
    else:
        es = fb.ensure_item(wsid, "Eventstream", EVENTSTREAM, "Custom endpoint -> Eventhouse rt_events_raw (SYNTHETIC)",
                            definition=definition)
    nb_id = deploy_notebook(fb, wsid, lh_id, RT_NOTEBOOK)
    sig_id = deploy_notebook(fb, wsid, None, SIGNAL_NOTEBOOK)
    dash = deploy_rt_dashboard(fb, wsid, db["id"], uri)
    rc = fc.get("realtime", {})
    fc["realtime"] = {"eventhouse_id": eh["id"], "kql_database_id": db["id"], "query_uri": uri,
                      "eventstream_id": es["id"], "simulator_notebook_id": nb_id, "signal_notebook_id": sig_id,
                      "signal_refresh_minutes": rc.get("signal_refresh_minutes", 15),
                      "signal_schedule_days": rc.get("signal_schedule_days", 7), "dashboard_id": dash}
    fb.save()
    if run:
        run_job(fb, wsid, nb_id, "RunNotebook", RT_NOTEBOOK, timeout_min=60)
    run_job(fb, wsid, sig_id, "RunNotebook", SIGNAL_NOTEBOOK, timeout_min=30)
    schedule_notebook(fb, wsid, sig_id, fc["realtime"]["signal_refresh_minutes"], fc["realtime"]["signal_schedule_days"])
    for label, q in [("tables", "union withsource=t rt_events_raw, rt_pos_transaction, rt_digital_event, rt_inventory_movement "
                                "| summarize rows = count(), latest = max(event_ts) by t"),
                     ("signal", "rt_inventory_signal | summarize rows = count(), snapshots = dcount(signal_ts), "
                                "t1_rows = countif(t1_intraday), latest = max(signal_ts)"),
                     ("t1_alerts", "fn_t1_alerts() | summarize sku_sizes = count() by style_id, market_id | top 10 by sku_sizes")]:
        print(f"[{KQL_DB}] {label}: {kusto(fb, uri, q)}")


def schedule_notebook(fb: Fabric, wsid: str, item_id: str, minutes: int, days: int):
    """Idempotent Fabric job-scheduler entry: run the notebook every `minutes` for `days` from now (bounded for cost)."""
    import datetime as dt
    start = dt.datetime.utcnow().replace(second=0, microsecond=0)
    body = {"enabled": True, "configuration": {"type": "Cron", "interval": minutes, "localTimeZoneId": "UTC",
                                               "startDateTime": start.isoformat(),
                                               "endDateTime": (start + dt.timedelta(days=days)).isoformat()}}
    base = f"/workspaces/{wsid}/items/{item_id}/jobs/RunNotebook/schedules"
    existing = fb.call("GET", base).get("value", [])
    if existing:
        fb.call("PATCH", f"{base}/{existing[0]['id']}", json=body)
        for extra in existing[1:]:
            fb.call("DELETE", f"{base}/{extra['id']}")
    else:
        fb.call("POST", base, json=body)
    print(f"schedule {SIGNAL_NOTEBOOK}: every {minutes} min until {body['configuration']['endDateTime']} UTC")


RT_DASHBOARD = "rtd-appareliq-intraday"
WINDOW = "event_ts between (_startTime .. _endTime)"
# (title, visual, x, y, w, h, KQL) - every query runs against kdb_appareliq; _startTime/_endTime come from the time-range parameter
RT_TILES = [
    ("T1 alerts firing now (SKU-size x market)", "card", 0, 0, 6, 5, "fn_t1_alerts() | count"),
    ("T1 projected stock-out - intraday (requirements 6.3)", "table", 6, 0, 18, 9,
     "fn_t1_alerts() | project style_id, size, market_id, intraday_available, rt_units, rt_receipts, "
     "intraday_weeks_of_cover, intent_index_intraday, intent_index_7d, batch_weeks_of_cover, last_sale"),
    ("Real-time data quality (raw stream)", "table", 0, 5, 6, 8,
     "rt_events_raw | where " + WINDOW + " | summarize events = count(), "
     "invalid = countif(isempty(event_id) or isempty(sku_id)), duplicates = count() - dcount(event_id) by event_category"),
    ("POS units per hour by market (DS-15)", "timechart", 0, 13, 12, 8,
     "pos_events() | where " + WINDOW + " and event_type == 'sale' | summarize units = sum(qty) by bin(event_ts, 1h), market_id"),
    ("Hero style HM9443 - digital intent events per hour (DS-16)", "timechart", 12, 9, 12, 8,
     "digital_events() | where " + WINDOW + " and style_id == 'HM9443' | summarize events = count() by bin(event_ts, 1h), market_id"),
    ("Intent index - top style x market, last 2h vs prior 22h", "bar", 12, 17, 12, 8,
     "fn_intent_index() | where isnotnull(intent_index_intraday) | extend style_market = strcat(style_id, ' ', market_id) "
     "| top 15 by intent_index_intraday | project style_market, intent_index_intraday"),
    ("Inventory net change by market and movement type (DS-17)", "column", 0, 21, 12, 8,
     "inventory_events() | where " + WINDOW + " | summarize units = sum(signed_qty) by market_id, event_type"),
    ("T1 firing SKU-sizes per snapshot by market (rt_inventory_signal)", "timechart", 12, 25, 12, 8,
     "rt_inventory_signal | where signal_ts between (_startTime .. _endTime) "
     "| summarize sku_sizes = countif(t1_intraday) by signal_ts, market_id"),
]


def deploy_rt_dashboard(fb: Fabric, wsid: str, db_id: str, uri: str) -> str:
    import uuid
    uid = lambda s: str(uuid.uuid5(uuid.NAMESPACE_URL, f"appareliq/{RT_DASHBOARD}/{s}"))
    page, ds = uid("page"), uid("ds")
    queries, tiles = [], []
    for i, (title, visual, x, y, w, h, q) in enumerate(RT_TILES):
        qid = uid(f"q{i}")
        queries.append({"id": qid, "text": q, "dataSource": {"kind": "inline", "dataSourceId": ds},
                        "usedVariables": ["_startTime", "_endTime"] if "_startTime" in q else []})
        tiles.append({"id": uid(f"t{i}"), "title": title, "visualType": visual, "pageId": page,
                      "layout": {"x": x, "y": y, "width": w, "height": h},
                      "queryRef": {"kind": "query", "queryId": qid}, "visualOptions": {}})
    body = {"schema_version": "52", "title": RT_DASHBOARD,
            "autoRefresh": {"enabled": True, "defaultInterval": "1m", "minInterval": "30s"},
            "baseQueries": [], "tiles": tiles, "queries": queries,
            "pages": [{"id": page, "name": "Intraday rebalancing signals"}],
            "dataSources": [{"id": ds, "name": KQL_DB, "kind": "kusto-trident", "scopeId": "kusto-trident",
                             "clusterUri": uri, "database": db_id, "workspace": wsid}],
            "parameters": [{"kind": "duration", "id": uid("p-time"), "displayName": "Time range", "description": "",
                            "beginVariableName": "_startTime", "endVariableName": "_endTime",
                            "defaultValue": {"kind": "dynamic", "count": 24, "unit": "hours"},
                            "showOnPages": {"kind": "all"}}]}
    definition = {"parts": [{"path": "RealTimeDashboard.json", "payload": b64(body), "payloadType": "InlineBase64"}]}
    d = next((i for i in fb.items(wsid, "KQLDashboard") if i["displayName"] == RT_DASHBOARD), None)
    if d:
        fb.call("POST", f"/workspaces/{wsid}/kqlDashboards/{d['id']}/updateDefinition", json={"definition": definition})
        print(f"KQLDashboard {RT_DASHBOARD} updated: {d['id']}")
    else:
        d = fb.ensure_item(wsid, "KQLDashboard", RT_DASHBOARD, "UC1 intraday T1 / intent / inventory signals (SYNTHETIC)",
                           definition=definition)
    for i, (title, *_rest, q) in enumerate(RT_TILES):   # every tile query must run cleanly
        q = q.replace("_startTime", "ago(24h)").replace("_endTime", "now()")
        print(f"  tile {i} ok: {title[:50]} -> {len(kusto(fb, uri, q))} rows")
    return d["id"]


# -------------------------------------------------------------------- semantic model (step 5)
SEMANTIC_MODEL = "sm_appareliq"
PBI = "https://api.powerbi.com/v1.0/myorg"


def dax(fb: Fabric, wsid: str, sm_id: str, query: str) -> list[dict]:
    r = requests.post(f"{PBI}/groups/{wsid}/datasets/{sm_id}/executeQueries", timeout=300,
                      headers={"Authorization": "Bearer " + az_token(fb.cfg["tenant_id"], "https://analysis.windows.net/powerbi/api")},
                      json={"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}})
    body = r.json() if r.content else {}
    res = (body.get("results") or [{}])[0]
    if not r.ok or "error" in res:
        raise RuntimeError(f"DAX failed ({r.status_code}): {json.dumps(body)[:1500]}\n{query}")
    return [{k.split("[")[-1].rstrip("]"): v for k, v in row.items()} for row in res["tables"][0]["rows"]]


def deploy_semantic_model(fb: Fabric, wsid: str, lh_id: str) -> str:
    folder = ROOT / "fabric" / "semantic_model" / f"{SEMANTIC_MODEL}.SemanticModel"
    parts = []
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        text = f.read_text(encoding="utf-8").replace("{{workspace_id}}", wsid).replace("{{lh_medallion_id}}", lh_id)
        parts.append({"path": f.relative_to(folder).as_posix(), "payload": b64(text), "payloadType": "InlineBase64"})
    definition = {"parts": parts}
    sm = next((i for i in fb.items(wsid, "SemanticModel") if i["displayName"] == SEMANTIC_MODEL), None)
    if sm:
        fb.call("POST", f"/workspaces/{wsid}/semanticModels/{sm['id']}/updateDefinition", json={"definition": definition})
        print(f"SemanticModel {SEMANTIC_MODEL} updated: {sm['id']}")
    else:
        sm = fb.ensure_item(wsid, "SemanticModel", SEMANTIC_MODEL,
                            "UC1 inventory rebalancing - Direct Lake on lh_medallion gold (SYNTHETIC)", definition=definition)
    return sm["id"]


def refresh_semantic_model(fb: Fabric, wsid: str, sm_id: str):
    h = {"Authorization": "Bearer " + az_token(fb.cfg["tenant_id"], "https://analysis.windows.net/powerbi/api")}
    r = requests.post(f"{PBI}/groups/{wsid}/datasets/{sm_id}/refreshes", headers=h, json={"type": "full"}, timeout=60)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"refresh -> {r.status_code}: {r.text[:800]}")
    for _ in range(60):
        time.sleep(10)
        last = requests.get(f"{PBI}/groups/{wsid}/datasets/{sm_id}/refreshes?$top=1", headers=h, timeout=60).json()["value"][0]
        if last["status"] != "Unknown":
            print(f"refresh (Direct Lake framing): {last['status']}")
            if last["status"] != "Completed":
                raise RuntimeError(json.dumps(last)[:2000])
            return
    raise TimeoutError("semantic model refresh did not finish")


def validate_semantic_model(fb: Fabric, wsid: str, lh_id: str, sm_id: str):
    srv = sql_endpoint(fb, wsid, lh_id)
    tables = {"Product": "dim_product", "Market": "dim_market", "Sell Through": "fact_sell_through_daily",
              "Inventory": "fact_inventory_snapshot", "Rebalancing Candidates": "mart_rebalancing_candidates",
              "Rebalancing Pairs": "mart_rebalancing_pairs", "Trigger Event": "trigger_event"}
    checks = []
    row = dax(fb, wsid, sm_id, "EVALUATE ROW(" + ", ".join(f'"{t}", COUNTROWS(\'{t}\')' for t in tables) + ")")[0]
    for t, g in tables.items():
        n = run_tsql(fb.cfg, srv, MEDALLION_LH, f"SELECT COUNT(*) AS n FROM gold.{g}")[0]["n"]
        checks.append((f"row count {t}", row[t] == n, f"model {row[t]} vs gold {n}"))
    kpis = {"AN-01": "AN-01 In-Stock Rate", "AN-02": "AN-02 Full-Price Sell-Through",
            "AN-03": "AN-03 Markdown Exposure USD", "AN-04": "AN-04 Gross-Margin Protection USD",
            "AN-05": "AN-05 Launch Availability", "AN-07": "AN-07 Decision Cycle Hours (Median)", "AN-08": "AN-08 Approval Rate"}
    q = ("EVALUATE SUMMARIZECOLUMNS(Market[Market ID], "
         + ", ".join(f'"{k}", [{m}]' for k, m in kpis.items()) + ")")
    model = {r["Market ID"]: r for r in dax(fb, wsid, sm_id, q)}
    gold = run_tsql(fb.cfg, srv, MEDALLION_LH, "SELECT kpi_id, market_id, value FROM gold.kpi_snapshot WHERE status = 'COMPUTED'")
    for g in gold:
        mv = model.get(g["market_id"], {}).get(g["kpi_id"])
        ok = mv is not None and abs(mv - g["value"]) <= 1e-6 * max(1.0, abs(g["value"]))
        checks.append((f"{g['kpi_id']} {g['market_id']} reconciles", ok, f"model {mv} vs gold {g['value']}"))
    story = dax(fb, wsid, sm_id, """
EVALUATE
VAR hero = TREATAS({"HM9443"}, Product[Style ID])
RETURN ROW(
    "us_t1", CALCULATE([T1 Stock-out Risk SKU Markets], hero, Market[Market ID] = "US"),
    "gb_t2", CALCULATE([T2 Aging Risk SKU Markets], hero, Market[Market ID] = "GB"),
    "us_inbound_transfer", CALCULATE([Proposed Transfer Units], hero, Market[Market ID] = "US"),
    "gb_outbound_transfer", CALCULATE([Proposed Transfer Units by Source], hero, Market[Market ID] = "GB"),
    "us_t1_max_woc", CALCULATE(MAX('Rebalancing Candidates'[Weeks of Cover]), hero, Market[Market ID] = "US", 'Rebalancing Candidates'[T1 Stockout Risk] = TRUE()),
    "units_sold", [Total Units Sold],
    "units_sold_by_product", SUMX(VALUES(Product[Category]), [Total Units Sold]),
    "triggers", [Triggers Fired])""")[0]
    checks += [
        ("HM9443 US has T1 stock-out risk", (story["us_t1"] or 0) > 0, story["us_t1"]),
        ("HM9443 GB has T2 aging risk", (story["gb_t2"] or 0) > 0, story["gb_t2"]),
        ("HM9443 GB->US transfer visible by destination and by source (inactive relationship)",
         (story["us_inbound_transfer"] or 0) > 0 and story["us_inbound_transfer"] == story["gb_outbound_transfer"],
         f"{story['us_inbound_transfer']} / {story['gb_outbound_transfer']}"),
        ("HM9443 US T1-flagged sizes below 1.5 weeks of cover", (story["us_t1_max_woc"] or 99) < 1.5, story["us_t1_max_woc"]),
        ("Sell Through -> Product relationship complete", story["units_sold"] == story["units_sold_by_product"],
         f"{story['units_sold']} vs {story['units_sold_by_product']}"),
        ("Trigger Event rows visible", (story["triggers"] or 0) > 0, story["triggers"]),
    ]
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    failed = [c for c in checks if not c[1]]
    print(f"semantic model checks: {len(checks) - len(failed)}/{len(checks)} passed")
    if failed:
        raise SystemExit(1)


def setup_semantic(fb: Fabric):
    fc = fb.cfg["fabric"]
    wsid, lh_id = fc["workspace_id"], fc["lakehouses"][MEDALLION_LH]
    sm_id = deploy_semantic_model(fb, wsid, lh_id)
    fc["semantic_model_id"] = sm_id
    fb.save()
    refresh_semantic_model(fb, wsid, sm_id)
    validate_semantic_model(fb, wsid, lh_id, sm_id)


# -------------------------------------------------------------------- ontology (step 6)
ONTOLOGY_SPEC = ROOT / "fabric" / "ontology" / "ApparelIQ_Ontology.json"
SQL_VT = {"varchar": "String", "nvarchar": "String", "char": "String", "int": "BigInt", "bigint": "BigInt",
          "smallint": "BigInt", "float": "Double", "real": "Double", "decimal": "Double", "bit": "Boolean",
          "date": "DateTime", "datetime2": "DateTime", "datetime": "DateTime"}
KQL_VT = {"string": "String", "datetime": "DateTime", "int": "BigInt", "long": "BigInt", "real": "Double",
          "double": "Double", "decimal": "Double", "bool": "Boolean"}


def pascal(col: str) -> str:
    return "".join(w.capitalize() for w in col.split("_"))


def ts_bindings(e: dict) -> list[dict]:
    """An entity's time-series bindings: the spec allows one object or a list (e.g. raw events + a derived signal)."""
    ts = e.get("timeseries")
    return [] if not ts else ts if isinstance(ts, list) else [ts]


def build_ontology_definition(fb: Fabric, wsid: str, lh_id: str, spec: dict) -> tuple[dict, dict]:
    """Old-format (entity-type JSON) ontology definition. Ids are deterministic so updateDefinition is stable."""
    import uuid
    rt = fb.cfg["fabric"]["realtime"]
    srv = sql_endpoint(fb, wsid, lh_id)
    cols: dict[str, list[tuple[str, str]]] = {}
    for r in run_tsql(fb.cfg, srv, MEDALLION_LH, "SELECT TABLE_SCHEMA s, TABLE_NAME t, COLUMN_NAME c, DATA_TYPE d "
                      "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA IN ('gold','silver') ORDER BY ORDINAL_POSITION"):
        cols.setdefault(f"{r['s']}.{r['t']}", []).append((r["c"], r["d"]))
    kql_cols = {}
    for e in spec["entities"]:
        for ts in ts_bindings(e):
            sch = kusto(fb, rt["query_uri"], f"{ts['table']} | getschema")
            kql_cols[ts["table"]] = {r["ColumnName"]: r["ColumnType"] for r in sch}
    ns = uuid.UUID("6f1c3c2e-6d0b-4a53-9a65-1f6a6c0e1a11")
    counters = {"et": 1000000000000, "pr": 2000000000000, "px": 2500000000000, "rt": 3000000000000}

    def nid(kind):
        counters[kind] += 1
        return str(counters[kind])

    def lake(table):
        schema, name = table.split(".")
        return {"sourceType": "LakehouseTable", "workspaceId": wsid, "itemId": lh_id,
                "sourceTableName": name, "sourceSchema": schema}

    def prop(pid, name, vt):
        return {"id": pid, "name": name, "redefines": None, "baseTypeNamespaceType": None, "valueType": vt}

    parts, ents, summary = [], {}, {"entities": {}, "relationships": []}
    for e in spec["entities"]:
        if e["table"] not in cols:
            raise RuntimeError(f"ontology: {e['table']} not found on the lh_medallion SQL endpoint")
        eid = nid("et")
        by_col, props = {}, []
        for c, d in cols[e["table"]]:
            if c in spec["exclude_columns"] and c not in e["key"]:
                continue
            by_col[c] = nid("pr")
            props.append((by_col[c], pascal(c), SQL_VT.get(d, "String"), c))
        ts_groups = []   # (binding spec, [(prop id, name, value type, source column)], timestamp first)
        for i, ts in enumerate(ts_bindings(e)):
            # Property ids are immutable once deployed: the first binding keeps the original allocation order
            # (columns, then timestamp); further bindings use a separate id range so existing ids never shift.
            kind = "pr" if i == 0 else "px"
            kc, prefix = kql_cols[ts["table"]], ts.get("prefix", "Rt")
            cols_ = [(nid(kind), prefix + pascal(c), KQL_VT.get(kc[c], "String"), c) for c in ts["columns"]]
            ts_groups.append((ts, [(nid(kind), ts.get("timestamp_property", "RtEventTs"), "DateTime", ts["timestamp"])] + cols_))
        ts_props = [p for _, grp in ts_groups for p in grp]
        definition = {"id": eid, "namespace": "usertypes", "baseEntityTypeId": None, "name": e["name"],
                      "entityIdParts": [by_col[k] for k in e["key"]], "displayNamePropertyId": by_col[e["display"]],
                      "namespaceType": "Custom", "visibility": "Visible",
                      "properties": [prop(p, n, v) for p, n, v, _ in props],
                      "timeseriesProperties": [prop(p, n, v) for p, n, v, _ in ts_props]}
        parts.append({"path": f"EntityTypes/{eid}/definition.json", "payload": b64(definition), "payloadType": "InlineBase64"})
        bindings = [{"id": str(uuid.uuid5(ns, f"{e['name']}/static")), "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "propertyBindings": [{"sourceColumnName": c, "targetPropertyId": p} for p, _, _, c in props],
            "sourceTableProperties": lake(e["table"])}}]
        for i, (ts, grp) in enumerate(ts_groups):
            bindings.append({"id": str(uuid.uuid5(ns, f"{e['name']}/timeseries" + (f"/{ts['table']}" if i else ""))),
                             "dataBindingConfiguration": {
                "dataBindingType": "TimeSeries", "timestampColumnName": ts["timestamp"],
                "propertyBindings": [{"sourceColumnName": kc_, "targetPropertyId": by_col[k]} for kc_, k in zip(ts["key"], e["key"])]
                                    + [{"sourceColumnName": c, "targetPropertyId": p} for p, _, _, c in grp[1:]],
                "sourceTableProperties": {"sourceType": "KustoTable", "workspaceId": wsid, "itemId": rt["eventhouse_id"],
                                          "clusterUri": rt["query_uri"], "databaseName": KQL_DB, "sourceTableName": ts["table"]}}})
        for bnd in bindings:
            parts.append({"path": f"EntityTypes/{eid}/DataBindings/{bnd['id']}.json", "payload": b64(bnd), "payloadType": "InlineBase64"})
        ents[e["name"]] = {"id": eid, "keys": [by_col[k] for k in e["key"]]}
        summary["entities"][e["name"]] = {"table": e["table"], "properties": [n for _, n, _, _ in props],
                                          "timeseries": [n for _, n, _, _ in ts_props]}
    for r in spec["relationships"]:
        src, tgt = ents[r["from"]], ents[r["to"]]
        if len(r["from_keys"]) != len(src["keys"]) or len(r["to_keys"]) != len(tgt["keys"]):
            raise RuntimeError(f"ontology: key arity mismatch in relationship {r['name']}")
        rid = nid("rt")
        rdef = {"namespace": "usertypes", "id": rid, "name": r["name"], "namespaceType": "Custom",
                "source": {"entityTypeId": src["id"]}, "target": {"entityTypeId": tgt["id"]}}
        ctx = {"id": str(uuid.uuid5(ns, f"rel/{r['name']}")), "dataBindingTable": lake(r["table"]),
               "sourceKeyRefBindings": [{"sourceColumnName": c, "targetPropertyId": p} for c, p in zip(r["from_keys"], src["keys"])],
               "targetKeyRefBindings": [{"sourceColumnName": c, "targetPropertyId": p} for c, p in zip(r["to_keys"], tgt["keys"])]}
        parts.append({"path": f"RelationshipTypes/{rid}/definition.json", "payload": b64(rdef), "payloadType": "InlineBase64"})
        parts.append({"path": f"RelationshipTypes/{rid}/Contextualizations/{ctx['id']}.json", "payload": b64(ctx), "payloadType": "InlineBase64"})
        summary["relationships"].append(f"({r['from']})-[:{r['name']}]->({r['to']})")
    platform = {"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
                "metadata": {"type": "Ontology", "displayName": spec["name"]},
                "config": {"version": "2.0", "logicalId": str(uuid.uuid5(ns, spec["name"]))}}
    parts = [{"path": ".platform", "payload": b64(platform), "payloadType": "InlineBase64"},
             {"path": "definition.json", "payload": b64({}), "payloadType": "InlineBase64"}] + parts
    return {"parts": parts}, summary


def gql(fb: Fabric, wsid: str, gm_id: str, query: str) -> list[dict]:
    body = fb.call("POST", f"/workspaces/{wsid}/GraphModels/{gm_id}/executeQuery?preview=true",
                   json={"query": query, "language": "msftGql"})
    status = body.get("status") or {}
    if (status.get("description") or "").startswith("error"):
        raise RuntimeError(f"GQL error: {json.dumps(status)[:800]}\n{query}")
    res = body.get("result") or {}
    names = [c["name"] for c in res.get("columns", [])]
    return [r if isinstance(r, dict) else dict(zip(names, r)) for r in res.get("data", [])]


def _wait_graph_jobs(fb: Fabric, wsid: str, gm_id: str, t0: float) -> dict:
    """Wait until no refreshGraph job is queued or running; return the most recent job instance."""
    while time.time() - t0 < 60 * 60:
        jobs = fb.call("GET", f"/workspaces/{wsid}/items/{gm_id}/jobs/instances").get("value", [])
        jobs = [j for j in jobs if j["status"] != "Deduped"]
        if jobs and not any(j["status"] in ("NotStarted", "InProgress") for j in jobs):
            return max(jobs, key=lambda j: j.get("startTimeUtc") or "")
        time.sleep(20)
    raise TimeoutError("graph refresh did not finish")


def refresh_graph(fb: Fabric, wsid: str, gm_id: str):
    """Refresh the graph. Creating or updating an ontology starts its own refresh, so a Deduped
    request waits for that in-flight job instead of failing."""
    fb._auth()
    r = fb.s.post(f"{F}/workspaces/{wsid}/graphModels/{gm_id}/jobs/refreshGraph/instances", json={}, timeout=60)
    if r.status_code != 202:
        raise RuntimeError(f"refreshGraph -> {r.status_code}: {r.text[:800]}")
    loc, t0 = r.headers["Location"], time.time()
    while time.time() - t0 < 60 * 60:
        time.sleep(20)
        fb._auth()
        st = fb.s.get(loc, timeout=60).json()
        if st.get("status") == "Deduped":
            st = _wait_graph_jobs(fb, wsid, gm_id, t0)
        if st.get("status") in ("Completed", "Failed", "Cancelled"):
            print(f"graph refresh: {st['status']} after {time.time() - t0:.0f}s")
            if st["status"] != "Completed":
                raise RuntimeError(json.dumps(st.get("failureReason") or st)[:2000])
            return
    raise TimeoutError("graph refresh did not finish")


def validate_ontology(fb: Fabric, wsid: str, lh_id: str, gm_id: str, spec: dict):
    srv = sql_endpoint(fb, wsid, lh_id)
    q = lambda s: run_tsql(fb.cfg, srv, MEDALLION_LH, s)
    checks = []
    for e in spec["entities"]:
        key = " + '|' + ".join(f"CAST({k} AS varchar(200))" for k in e["key"])
        n = q(f"SELECT COUNT(DISTINCT {key}) AS n FROM {e['table']}")[0]["n"]
        g = gql(fb, wsid, gm_id, f"MATCH (n:`{e['name']}`) RETURN count(n) AS c")[0]["c"]
        checks.append((f"node count {e['name']}", g == n, f"graph {g} vs table {n}"))
    tables = {e["name"]: e for e in spec["entities"]}
    for r in spec["relationships"]:
        src, tgt = tables[r["from"]], tables[r["to"]]
        cond = lambda ent, keys: " AND ".join(f"e.{k} = x.{c}" for k, c in zip(ent["key"], keys))
        n = q(f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT {', '.join(dict.fromkeys(r['from_keys'] + r['to_keys']))} FROM {r['table']} x "
              f"WHERE EXISTS (SELECT 1 FROM {src['table']} e WHERE {cond(src, r['from_keys'])}) "
              f"AND EXISTS (SELECT 1 FROM {tgt['table']} e WHERE {cond(tgt, r['to_keys'])})) d")[0]["n"]
        g = gql(fb, wsid, gm_id, f"MATCH (:`{r['from']}`)-[e:{r['name']}]->(:`{r['to']}`) RETURN count(e) AS c")[0]["c"]
        checks.append((f"edge count {r['name']}", g == n, f"graph {g} vs tables {n}"))
    pair = gql(fb, wsid, gm_id, """
MATCH (s:InventoryPosition)<-[:pairSource]-(p:RebalancingPair)-[:pairDestination]->(d:InventoryPosition)-[:positionOf]->(pr:`Product`)
WHERE pr.StyleId = 'HM9443'
RETURN s.MarketId AS source_market, d.MarketId AS destination_market, p.ProposedUnits AS units""")
    checks.append(("HM9443 transfer pair GB -> US", any(r["source_market"] == "GB" and r["destination_market"] == "US" for r in pair), pair))
    t1 = gql(fb, wsid, gm_id, """
MATCH (t:TriggerEvent)-[:firedOn]->(ip:InventoryPosition)-[:positionOf]->(pr:`Product`)
WHERE t.TriggerId = 'T1' AND pr.StyleId = 'HM9443'
RETURN ip.MarketId AS market, count(t) AS triggers GROUP BY market""")
    t1_sql = q("SELECT t.market_id AS market, COUNT(*) AS n FROM gold.trigger_event t JOIN gold.dim_product p ON p.sku_id = t.sku_id "
               "WHERE t.trigger_id = 'T1' AND p.style_id = 'HM9443' GROUP BY t.market_id")
    checks.append(("HM9443 T1 triggers traverse to positions", {r["market"]: r["triggers"] for r in t1} == {r["market"]: r["n"] for r in t1_sql},
                   f"graph {t1} vs gold {t1_sql}"))
    cat = q("SELECT TOP 1 category FROM gold.dim_product WHERE style_id = 'HM9443'")[0]["category"]
    owner = gql(fb, wsid, gm_id, f"""
MATCH (m:Market)<-[:roleForMarket]-(r:DecisionRole)-[:roleHeldBy]->(p:Person)
WHERE m.MarketId = 'US' AND r.Category = '{cat}' AND r.Role = 'ACCOUNTABLE'
RETURN p.DisplayName AS owner, r.ApprovalLimitUnits AS approval_limit""")
    owner_sql = q(f"SELECT display_name, approval_limit_units FROM silver.decision_owner WHERE market_id = 'US' "
                  f"AND category = '{cat}' AND role = 'ACCOUNTABLE'")
    checks.append(("US accountable owner for HM9443 category", [o["owner"] for o in owner] == [o["display_name"] for o in owner_sql],
                   f"graph {owner} vs silver {owner_sql}"))
    dup = gql(fb, wsid, gm_id, """
MATCH (r:DecisionRole)-[:roleForMarket]->(m:Market)
WHERE r.Role = 'ACCOUNTABLE'
RETURN m.MarketId AS market, r.Category AS category, count(r) AS owners GROUP BY market, category""")
    dup_sql = q("SELECT market_id, category, dq_owner_gap FROM silver.decision_owner_coverage")
    checks.append(("DQ-12 single accountable owner per category x market matches silver coverage",
                   {(d["market"], d["category"]) for d in dup if d["owners"] == 1}
                   == {(d["market_id"], d["category"]) for d in dup_sql if not d["dq_owner_gap"]},
                   f"silver gaps {[(d['market_id'], d['category']) for d in dup_sql if d['dq_owner_gap']]}"))
    lane = gql(fb, wsid, gm_id, """
MATCH (p:RebalancingPair)-[:pairUsesLane]->(l:TransferLane)-[:laneTo]->(to:Location)-[:locatedIn]->(m:Market)
WHERE p.StyleId = 'HM9443'
RETURN l.Mode AS mode, l.LeadDays AS lead_days, m.MarketId AS to_market""")
    checks.append(("HM9443 pair uses an open lane into the US", any(r["to_market"] == "US" for r in lane), lane))
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    failed = [c for c in checks if not c[1]]
    print(f"ontology checks: {len(checks) - len(failed)}/{len(checks)} passed")
    if failed:
        raise SystemExit(1)


def validate_ontology_timeseries(fb: Fabric, spec: dict):
    """The graph holds the static entities; time-series properties are read live from the Eventhouse. Check every
    time-series binding resolves: keys present in the KQL table and matching the bound entity keys."""
    rt = fb.cfg["fabric"]["realtime"]
    for e in spec["entities"]:
        for ts in ts_bindings(e):
            key = ", '|', ".join(ts["key"])
            r = kusto(fb, rt["query_uri"], f"{ts['table']} | where isnotempty({ts['key'][0]}) | summarize n = count(), "
                      f"keys = dcount(strcat({key})), last_event = max({ts['timestamp']})")[0]
            ok = r["n"] > 0
            print(f"  [{'PASS' if ok else 'FAIL'}] time series {e['name']} <- {KQL_DB}.{ts['table']}: "
                  f"{r['n']} rows, {r['keys']} entity keys, last {r['last_event']}")
            if not ok:
                raise SystemExit(1)
    # rt_inventory_signal: one row per entity key per snapshot, and every key resolves to an InventoryPosition
    lh = fb.cfg["fabric"]["lakehouses"][MEDALLION_LH]
    pos_keys = {(r["sku_id"], r["market_id"]) for r in run_tsql(fb.cfg, sql_endpoint(fb, fb.cfg["fabric"]["workspace_id"], lh),
                MEDALLION_LH, "SELECT sku_id, market_id FROM gold.mart_rebalancing_candidates")}
    sig = kusto(fb, rt["query_uri"], "rt_inventory_signal | where signal_ts == toscalar(rt_inventory_signal | summarize max(signal_ts)) "
                "| project sku_id, market_id")
    sig_keys = {(r["sku_id"], r["market_id"]) for r in sig}
    hist = kusto(fb, rt["query_uri"], "rt_inventory_signal | summarize rows = count(), keys = dcount(strcat(sku_id, '|', market_id)) "
                 "by signal_ts | summarize snapshots = count(), duplicates = countif(rows != keys)")[0]
    t1 = kusto(fb, rt["query_uri"], "rt_inventory_signal | where t1_intraday | summarize n = dcount(signal_ts)")[0]["n"]
    for name, ok, detail in [
            ("signal snapshot keys unique", hist["duplicates"] == 0, f"{hist['snapshots']} snapshots, {hist['duplicates']} with duplicate keys"),
            ("signal keys resolve to InventoryPosition", bool(sig_keys) and sig_keys <= pos_keys,
             f"{len(sig_keys)} keys in latest snapshot, {len(sig_keys - pos_keys)} unmatched"),
            ("signal history contains T1 firings", t1 > 0, f"{t1} snapshots with t1_intraday")]:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            raise SystemExit(1)


def setup_ontology(fb: Fabric):
    fc = fb.cfg["fabric"]
    wsid, lh_id = fc["workspace_id"], fc["lakehouses"][MEDALLION_LH]
    spec = json.loads(ONTOLOGY_SPEC.read_text(encoding="utf-8"))
    definition, summary = build_ontology_definition(fb, wsid, lh_id, spec)
    print(f"ontology definition: {len(summary['entities'])} entity types, {len(summary['relationships'])} relationship types, "
          f"{len(definition['parts'])} parts")
    (ROOT / "fabric" / "ontology" / "ontology_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    ont = next((i for i in fb.items(wsid, "Ontology") if i["displayName"] == spec["name"]), None)
    if ont:
        fb.call("POST", f"/workspaces/{wsid}/ontologies/{ont['id']}/updateDefinition?updateMetadata=true", json={"definition": definition})
        print(f"Ontology {spec['name']} updated: {ont['id']}")
    else:
        ont = fb.call("POST", f"/workspaces/{wsid}/ontologies",
                      json={"displayName": spec["name"], "description": spec["description"], "definition": definition})
        ont = ont if ont.get("id") else next(i for i in fb.items(wsid, "Ontology") if i["displayName"] == spec["name"])
        print(f"Ontology {spec['name']} created: {ont['id']}")
    fc.setdefault("ontology", {})["ontology_id"] = ont["id"]
    gm = None
    for _ in range(30):
        gms = fb.items(wsid, "GraphModel")
        gm = next((g for g in gms if g["displayName"].startswith(spec["name"])), None) or (gms[0] if len(gms) == 1 else None)
        if gm:
            break
        time.sleep(10)
    if not gm:
        raise RuntimeError("GraphModel generated by the ontology not found")
    fc["ontology"]["graph_model_id"] = gm["id"]
    fb.save()
    print(f"GraphModel {gm['displayName']}: {gm['id']}")
    refresh_graph(fb, wsid, gm["id"])
    validate_ontology(fb, wsid, lh_id, gm["id"], spec)
    validate_ontology_timeseries(fb, spec)


# ------------------------------------------------------------------- data agent (Phase 2 step 7)
DATA_AGENT_SPEC = ROOT / "fabric" / "data_agent" / "appareliq_data_agent.json"
DA_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/dataAgent/definition/"


def _paged(fb: Fabric, path: str) -> list[dict]:
    out, tok = [], None
    while True:
        sep = "&" if "?" in path else "?"
        r = fb.call("GET", path + (f"{sep}continuationToken={requests.utils.quote(tok)}" if tok else ""))
        out += r.get("value", [])
        tok = r.get("continuationToken")
        if not tok:
            return out


def build_data_agent_draft(fb: Fabric, wsid: str, da_id: str, spec: dict):
    """Rebuild the staging draft: instructions + ontology (definition-only) + semantic model (staging API)."""
    fc = fb.cfg["fabric"]
    base = f"/workspaces/{wsid}/dataAgents/{da_id}"
    instructions = "\n".join(spec["ai_instructions"])
    # The ontology can only be attached through the item definition (staging/datasources rejects it with
    # OntologyAddAsDataSourceBlocked). updateDefinition replaces the whole draft, so it runs first.
    ont_id = fc["ontology"]["ontology_id"]
    ont_name = next(i["displayName"] for i in fb.items(wsid, "Ontology") if i["id"] == ont_id)
    parts = {
        "Files/Config/data_agent.json": {"$schema": DA_SCHEMA + "dataAgent/2.1.0/schema.json"},
        "Files/Config/draft/stage_config.json": {"$schema": DA_SCHEMA + "stageConfiguration/1.0.0/schema.json",
                                                 "aiInstructions": instructions},
        f"Files/Config/draft/ontology-{ont_name}/datasource.json": {
            "$schema": DA_SCHEMA + "dataSource/1.0.0/schema.json", "artifactId": ont_id, "workspaceId": wsid,
            "displayName": ont_name, "type": "ontology", "elements": []},
    }
    fb.call("POST", f"{base}/updateDefinition", json={"definition": {"parts": [
        {"path": k, "payload": b64(v), "payloadType": "InlineBase64"} for k, v in parts.items()]}})
    print(f"  draft definition: instructions ({len(instructions)} chars) + ontology {ont_name}")
    fb.call("PATCH", f"{base}/staging/settings", json={"aiInstructions": instructions,
            "experimental": {"enableExperimentalFeatures": bool(spec.get("enable_preview_runtime"))}})

    sm_id, smc = fc["semantic_model_id"], spec["semantic_model"]
    if not any(d["id"] == sm_id for d in fb.call("GET", f"{base}/staging/datasources").get("value", [])):
        fb.call("POST", f"{base}/staging/datasources", json={"type": "FabricItem", "itemReference": {
            "referenceType": "ById", "itemId": sm_id, "workspaceId": wsid}})
    fb.call("PATCH", f"{base}/staging/datasources/{sm_id}",
            json={"description": smc["description"], "instructions": "\n".join(smc["instructions"])})
    selected = 0
    for el in _paged(fb, f"{base}/staging/datasources/{sm_id}/elements"):
        want = el["type"] == "Table" and el["displayName"] not in smc.get("exclude_tables", [])
        if el.get("isSelected") != want:
            fb.call("PATCH", f"{base}/staging/datasources/{sm_id}/elements?id={requests.utils.quote(el['id'], safe='')}",
                    json={"isSelected": want})
        selected += want
    print(f"  semantic model {sm_id}: {selected} tables selected, description + instructions set")
    return selected


async def _ask_mcp(mcp_url: str, token: str, question: str) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, httpx2, streamable_http_client
    async with streamable_http_client(mcp_url, http_client=create_mcp_http_client(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(300.0))) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            if not tools:
                raise RuntimeError("data agent exposes no MCP tool (is it published?)")
            arg = next(iter(tools[0].input_schema["properties"]))
            res = await session.call_tool(tools[0].name, {arg: question})
            return "\n".join(b.text for b in res.content if getattr(b, "type", None) == "text").strip()


def _score(fb: Fabric, case: dict, answer: str) -> tuple[bool, str]:
    import re
    low = answer.lower()
    if "expect_kql" in case:
        rows = kusto(fb, fb.cfg["fabric"]["realtime"]["query_uri"], case["expect_kql"])
        exp = rows[0][case["expect_field"]] if rows else None
        if exp is None:
            return False, "no ground truth row"
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", answer.replace(",", ""))]
        ok = any(abs(n - exp) <= case["tolerance"] for n in nums) and "signal" in low
        return ok, f"expected ~{exp} (+/-{case['tolerance']})"
    missing = [g for g in case["expect_all"] if not any(s.lower() in low for s in g)]
    return not missing, "missing " + " | ".join("/".join(g) for g in missing) if missing else "all expected facts present"


def benchmark_data_agent(fb: Fabric, spec: dict, mcp_url: str, only: list[str] | None = None):
    import asyncio
    out = ROOT / "fabric" / "data_agent" / "benchmark_results.json"
    token = az_token(fb.cfg["tenant_id"], "https://api.fabric.microsoft.com")
    results = []
    for case in spec["benchmark"]:
        if only and case["id"] not in only:
            continue
        t0 = time.time()
        try:
            answer = asyncio.run(_ask_mcp(mcp_url, token, case["question"]))
            ok, why = _score(fb, case, answer)
        except Exception as e:  # noqa: BLE001 - a failed call is a failed benchmark case
            answer, ok, why = "", False, f"error: {e}"[:400]
        results.append({"id": case["id"], "source": case["source"], "question": case["question"], "pass": ok,
                        "detail": why, "seconds": round(time.time() - t0, 1), "answer": answer})
        print(f"  [{'PASS' if ok else 'FAIL'}] {case['id']} ({case['source']}, {results[-1]['seconds']}s): {why}")
    passed = sum(r["pass"] for r in results)
    print(f"benchmark: {passed}/{len(results)} passed")
    out.write_text(json.dumps({"synthetic": True, "run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "passed": passed, "total": len(results), "results": results}, indent=1), encoding="utf-8")
    return passed, len(results)


def setup_dataagent(fb: Fabric, benchmark_only: bool = False, only: list[str] | None = None):
    fc = fb.cfg["fabric"]
    wsid = fc["workspace_id"]
    spec = json.loads(DATA_AGENT_SPEC.read_text(encoding="utf-8"))
    da = next((i for i in fb.items(wsid, "DataAgent") if i["displayName"] == spec["name"]), None)
    if not da:
        da = fb.call("POST", f"/workspaces/{wsid}/dataAgents",
                     json={"displayName": spec["name"], "description": spec["description"]})
        da = da if da.get("id") else next(i for i in fb.items(wsid, "DataAgent") if i["displayName"] == spec["name"])
        print(f"DataAgent {spec['name']} created: {da['id']}")
    else:
        print(f"DataAgent {spec['name']} exists: {da['id']}")
    base = f"/workspaces/{wsid}/dataAgents/{da['id']}"
    mcp_url = f"{F}/mcp/workspaces/{wsid}/dataagents/{da['id']}/agent"
    fc["data_agent"] = {"id": da["id"], "name": spec["name"], "mcp_url": mcp_url}
    fb.save()
    if not benchmark_only:
        build_data_agent_draft(fb, wsid, da["id"], spec)
        fb.call("POST", f"{base}/staging/publish", json={"publishedDescription": spec["published_description"]})
        pub = fb.call("GET", f"{base}/settings")
        pds = fb.call("GET", f"{base}/datasources").get("value", [])
        print(f"published: preview runtime={pub.get('experimental', {}).get('enableExperimentalFeatures')}, "
              f"datasources={[d['displayName'] for d in pds]} (+ ontology via definition)")
    return benchmark_data_agent(fb, spec, mcp_url, only)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "deployment.local.json"))
    ap.add_argument("action", choices=["workspace", "dbx-mirror", "sql-principal", "sql-mirror", "medallion", "realtime", "semantic", "ontology", "dataagent", "verify"])
    ap.add_argument("--benchmark-only", action="store_true", help="dataagent: skip draft rebuild/publish, only run the benchmark")
    ap.add_argument("--cases", nargs="*", metavar="ID", help="dataagent: run only these benchmark case ids (e.g. B01 B13)")
    ap.add_argument("--lakehouse", help="verify: limit to one lakehouse")
    ap.add_argument("--recreate", action="store_true",
                    help="dbx-mirror: delete and recreate the mirrored catalog (e.g. after SQLEndpointCreationFailed)")
    ap.add_argument("--run", nargs="*", metavar="NOTEBOOK",
                    help="medallion: run notebooks after deploying (no names = all, in order 01..04)")
    ap.add_argument("--via-pipeline", action="store_true", help="medallion: run the pl_medallion pipeline instead")
    a = ap.parse_args()
    p = Path(a.config)
    fb = Fabric(json.loads(p.read_text(encoding="utf-8-sig")), p)
    if a.action == "verify":
        verify(fb, a.lakehouse)
    elif a.action == "dbx-mirror":
        setup_dbx_mirror(fb, a.recreate)
    elif a.action == "medallion":
        setup_medallion(fb, a.run, a.via_pipeline)
    elif a.action == "realtime":
        setup_realtime(fb, a.run is not None)
    elif a.action == "semantic":
        setup_semantic(fb)
    elif a.action == "ontology":
        setup_ontology(fb)
    elif a.action == "dataagent":
        passed, total = setup_dataagent(fb, a.benchmark_only, a.cases)
        if passed < total:
            raise SystemExit(1)
    else:
        {"workspace": setup_workspace, "sql-principal": setup_sql_principal,
         "sql-mirror": setup_sql_mirror}[a.action](fb)


if __name__ == "__main__":
    main()
