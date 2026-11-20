"""Idempotent setup for the step 9 coworker agent (Kinetiq UC1 rebalancing, SYNTHETIC demo data).

  python scripts/setup_agent.py deploy        # az deployment of infra/agent.bicep (app settings from config) + config
  python scripts/setup_agent.py config        # copy the Bicep outputs into config/deployment.local.json (agent.*)
  python scripts/setup_agent.py grants        # Graph app roles + Fabric workspace Viewer for the agent identity
  python scripts/setup_databricks.py run-sql-agent   # agent schema, personas, SQL contained user + grants
  python scripts/setup_agent.py publish       # zip-deploy agent/ to the Function App (remote build)
  python scripts/setup_agent.py activator     # UDF udf_agent_relay + Activator act_uc1_t1 + outbox role/app settings
  python scripts/setup_agent.py trigger-notebook [--run]  # Fabric notebook 07_trigger_agent_case (manual case trigger)
  python scripts/setup_agent.py teams-app     # build dist/teams-app.zip and publish it to the org app catalog
  python scripts/setup_agent.py validate      # health + infra/permission checks
  python scripts/setup_agent.py foundry-agent # project connections + Foundry prompt agent (Foundry IQ KB + Fabric IQ)
  python scripts/setup_agent.py foundry-smoke # grounded policy + data questions; non-zero exit on wrong answers
  python scripts/setup_agent.py foundry-publish [--scope Tenant|Shared]  # publish the agent to Microsoft 365 / Teams

Auth is keyless: one user-assigned managed identity is the Function identity AND the Azure Bot identity
(msaAppType=UserAssignedMSI). Activator reaches the agent without any secret: the Fabric UDF relay_t1 writes the T1
event to lh_medallion Files/agent_outbox (native Lakehouse connection) and the Function polls it with its identity.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import uuid
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "agent"
TEAMS_DIR = ROOT / "teams"
DIST = ROOT / "dist"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
FABRIC = "https://api.fabric.microsoft.com/v1"
DEPLOYMENT = "appareliq-agent"
KV_SECRET = "agent-events-function-key"
EVENT_FUNCTION = "t1_event"
# Least privilege for the proactive group-chat pattern: create chats, install *this* app into them (consenting its
# chat-scoped RSC Chat.Manage.Chat, which lets it add Finance / wholesale people only to chats it is installed in),
# resolve users. The *Self* install permission cannot install apps that declare RSC, hence ReadWriteAndConsentSelf.
GRAPH_APP_ROLES = ["Chat.Create", "TeamsAppInstallation.ReadWriteAndConsentSelfForChat.All", "User.Read.All"]
GRAPH_APP_ROLES_RETIRED = ["TeamsAppInstallation.ReadWriteSelfForChat.All"]
# Teams RSC requires a real Entra application as webApplicationInfo.id (a managed identity has no application object;
# RSC installs 404). This app registration has no secret: its federated credential trusts the agent managed identity.
GRAPH_APP_NAME = "appareliq-agent-graph"


def az(*args: str, check: bool = True) -> str:
    r = subprocess.run(["az", *args], capture_output=True, text=True, shell=sys.platform == "win32")
    if check and r.returncode:
        raise RuntimeError(f"az {' '.join(args[:4])} failed: {r.stderr.strip()[:2000]}")
    return r.stdout.strip()


class Ctx:
    def __init__(self, cfg_path: Path):
        self.cfg_path = cfg_path
        self.cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        self.tenant = self.cfg["tenant_id"]
        self.rg = self.cfg["resource_group"]
        self._tok: dict[str, tuple[float, str]] = {}

    @property
    def ac(self) -> dict:
        return self.cfg.setdefault("agent", {})

    def save(self):
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2) + "\n", encoding="utf-8")

    def token(self, resource: str) -> str:
        t = self._tok.get(resource)
        if not t or time.time() - t[0] > 40 * 60:
            t = (time.time(), az("account", "get-access-token", "--tenant", self.tenant, "--resource", resource,
                                 "--query", "accessToken", "-o", "tsv"))
            self._tok[resource] = t
        return t[1]

    def call(self, method: str, url: str, resource: str, ok=(200, 201, 202, 204), **kw):
        for _ in range(8):
            r = requests.request(method, url, headers={"Authorization": "Bearer " + self.token(resource)},
                                 timeout=120, **kw)
            if r.status_code in (429, 503):
                time.sleep(int(r.headers.get("Retry-After", "15")))
                continue
            break
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:1500]}")
        return r.json() if r.content and "json" in r.headers.get("Content-Type", "") else {}

    def graph(self, method: str, path: str, **kw):
        return self.call(method, GRAPH + path, "https://graph.microsoft.com", **kw)

    def fabric(self, method: str, path: str, **kw):
        return self.call(method, FABRIC + path, "https://api.fabric.microsoft.com", **kw)


def app_settings(c: Ctx) -> dict[str, str]:
    """Environment-specific settings; only ids/endpoints, never secrets."""
    cfg, fc, f = c.cfg, c.cfg["foundry"], c.cfg["fabric"]
    rt, grp = f["realtime"], cfg["agent_users_group"]
    s = {
        "SQL_SERVER": cfg["sql"]["server_fqdn"],
        "SQL_DATABASE": cfg["sql"]["database"],
        "FABRIC_WORKSPACE_ID": f["workspace_id"],
        "KQL_QUERY_URI": rt["query_uri"],
        "KQL_DATABASE": "kdb_appareliq",
        "DATA_AGENT_MCP_URL": f["data_agent"]["mcp_url"],
        "FOUNDRY_PROJECT_ENDPOINT": fc["project_endpoint"],
        "FOUNDRY_OPENAI_ENDPOINT": fc["openai_endpoint"],
        "FOUNDRY_CHAT_DEPLOYMENT": fc["chat_deployment"],
        "SEARCH_ENDPOINT": fc["search_endpoint"],
        "KNOWLEDGE_BASE": fc["knowledge_base"],
        "KNOWLEDGE_SOURCE": fc["knowledge_source"],
        "AGENT_USERS_GROUP_ID": grp["object_id"],
        "FALLBACK_UPN": grp["fallback_user"]["upn"],
        "MATERIALITY_GM_USD": "50000",
        "APPROVAL_SLA_HOURS": "4",
        "DATA_LABEL": "SYNTHETIC",
    }
    if c.ac.get("teams_app_id"):
        s["TEAMS_APP_ID"] = c.ac["teams_app_id"]
    if c.ac.get("graph_app_client_id"):
        s["GRAPH_APP_CLIENT_ID"] = c.ac["graph_app_client_id"]
    if c.ac.get("foundry_coworker_agent"):
        s["FOUNDRY_COWORKER_AGENT"] = c.ac["foundry_coworker_agent"]
    if c.ac.get("outbox_lakehouse_id"):
        s["OUTBOX_LAKEHOUSE_ID"] = c.ac["outbox_lakehouse_id"]
        s["OUTBOX_DIR"] = c.ac.get("outbox_dir", OUTBOX_DIR)
    return s


# ---------------------------------------------------------------- deploy / config
def setup_deploy(c: Ctx):
    fc = c.cfg["foundry"]
    params = {
        "location": {"value": c.cfg.get("location", "centralus")},
        "operatorObjectId": {"value": c.cfg["agent_users_group"]["fallback_user"]["object_id"]},
        "foundryName": {"value": fc["account_name"]},
        "searchName": {"value": fc["search_name"]},
        "appSettings": {"value": app_settings(c)},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                   "contentVersion": "1.0.0.0", "parameters": params}, fh)
        pfile = fh.name
    try:
        print(f"deploying infra/agent.bicep to {c.rg} ...")
        az("deployment", "group", "create", "-g", c.rg, "-n", DEPLOYMENT, "-f", str(ROOT / "infra" / "agent.bicep"),
           "-p", f"@{pfile}", "-o", "none")
    finally:
        Path(pfile).unlink(missing_ok=True)
    setup_config(c)


def setup_config(c: Ctx):
    out = json.loads(az("deployment", "group", "show", "-g", c.rg, "-n", DEPLOYMENT,
                        "--query", "properties.outputs", "-o", "json"))
    v = {k: o["value"] for k, o in out.items()}
    c.ac.update({
        "identity_name": v["identityName"], "identity_client_id": v["identityClientId"],
        "identity_principal_id": v["identityPrincipalId"], "identity_resource_id": v["identityResourceId"],
        "function_app": v["functionAppName"], "function_host": v["functionHost"],
        "events_url": f"https://{v['functionHost']}/api/events/t1",
        "bot_name": v["botName"], "key_vault": v["keyVaultName"], "key_vault_uri": v["keyVaultUri"],
        "key_vault_secret": KV_SECRET, "storage": v["storageName"],
    })
    c.save()
    print(json.dumps(c.ac, indent=2))


# ---------------------------------------------------------------- grants
def setup_graph_app(c: Ctx) -> str:
    """Secretless Graph app registration: federated identity credential (FIC) trusting the agent managed identity."""
    apps = c.graph("GET", f"/applications?$filter=displayName eq '{GRAPH_APP_NAME}'&$select=id,appId")["value"]
    if apps:
        app = apps[0]
        print(f"graph app {GRAPH_APP_NAME}: exists ({app['appId']})")
    else:
        app = c.graph("POST", "/applications", json={
            "displayName": GRAPH_APP_NAME, "signInAudience": "AzureADMyOrg",
            "notes": "UC1 rebalancing coworker (SYNTHETIC demo). Graph identity for Teams chat RSC; no secrets - "
                     "federated credential trusts the agent user-assigned managed identity."})
        print(f"graph app {GRAPH_APP_NAME}: created ({app['appId']})")
    sps = c.graph("GET", f"/servicePrincipals?$filter=appId eq '{app['appId']}'&$select=id")["value"]
    sp = sps[0] if sps else c.graph("POST", "/servicePrincipals", json={"appId": app["appId"]})
    fic = {"name": "agent-managed-identity", "issuer": f"https://login.microsoftonline.com/{c.tenant}/v2.0",
           "subject": c.ac["identity_principal_id"], "audiences": ["api://AzureADTokenExchange"],
           "description": "Function App user-assigned managed identity"}
    fics = c.graph("GET", f"/applications/{app['id']}/federatedIdentityCredentials")["value"]
    if any(f["subject"] == fic["subject"] and f["issuer"] == fic["issuer"] for f in fics):
        print("graph app federated credential: present")
    else:
        c.graph("POST", f"/applications/{app['id']}/federatedIdentityCredentials", json=fic)
        print("graph app federated credential: trusts the agent managed identity")
    c.ac.update({"graph_app_client_id": app["appId"], "graph_app_object_id": app["id"], "graph_app_sp_id": sp["id"]})
    c.save()
    return sp["id"]


def _grant_graph_roles(c: Ctx, gsp: dict, sp_id: str, grant: list[str], revoke: list[str], who: str):
    have = {g["appRoleId"]: g["id"] for g in c.graph("GET", f"/servicePrincipals/{sp_id}/appRoleAssignments")["value"]
            if g["resourceId"] == gsp["id"]}
    for name in grant:
        rid = next(r["id"] for r in gsp["appRoles"] if r["value"] == name)
        if rid in have:
            print(f"graph {name} -> {who}: already granted")
            continue
        c.graph("POST", f"/servicePrincipals/{gsp['id']}/appRoleAssignedTo",
                json={"principalId": sp_id, "resourceId": gsp["id"], "appRoleId": rid})
        print(f"graph {name} -> {who}: granted")
    for name in revoke:
        rid = next(r["id"] for r in gsp["appRoles"] if r["value"] == name)
        if rid in have:
            c.graph("DELETE", f"/servicePrincipals/{sp_id}/appRoleAssignments/{have[rid]}")
            print(f"graph {name} -> {who}: revoked")


def setup_grants(c: Ctx):
    sp_id = c.ac["identity_principal_id"]
    gsp = c.graph("GET", f"/servicePrincipals?$filter=appId eq '{GRAPH_APP_ID}'&$select=id,appRoles")["value"][0]
    graph_sp = setup_graph_app(c)
    _grant_graph_roles(c, gsp, graph_sp, GRAPH_APP_ROLES, GRAPH_APP_ROLES_RETIRED, GRAPH_APP_NAME)
    # The managed identity no longer calls Graph directly: move its roles to the app registration.
    _grant_graph_roles(c, gsp, sp_id, [], GRAPH_APP_ROLES + GRAPH_APP_ROLES_RETIRED, "managed identity")
    az("functionapp", "config", "appsettings", "set", "-g", c.rg, "-n", c.ac["function_app"],
       "--settings", f"GRAPH_APP_CLIENT_ID={c.ac['graph_app_client_id']}", "-o", "none")
    print("GRAPH_APP_CLIENT_ID app setting updated")
    ws = c.cfg["fabric"]["workspace_id"]
    ras = c.fabric("GET", f"/workspaces/{ws}/roleAssignments")["value"]
    if any(r["principal"]["id"] == sp_id for r in ras):
        print("fabric workspace role: already assigned")
    else:
        c.fabric("POST", f"/workspaces/{ws}/roleAssignments",
                 json={"principal": {"id": sp_id, "type": "ServicePrincipal"}, "role": "Viewer"})
        print("fabric workspace role: Viewer assigned (data agent MCP + KQL reads)")
    setup_onelake_role(c)


ONELAKE_ROLES = {
    # Workspace Viewer has no OneLake ReadAll, and lh_medallion uses OneLake security, so the KQL external tables
    # (impersonate) are denied without this. Gold only, never Bronze/Silver.
    "CoworkerAgentGoldReader": ["/Tables/gold"],
    # T1 outbox written by the Fabric UDF relay_t1 (D-26); the agent only reads it.
    "CoworkerAgentOutboxReader": ["/Files/agent_outbox"],
}


def lakehouse_id(c: Ctx, name: str = "lh_medallion") -> str:
    ws = c.cfg["fabric"]["workspace_id"]
    return next(x["id"] for x in c.fabric("GET", f"/workspaces/{ws}/lakehouses")["value"] if x["displayName"] == name)


def setup_onelake_role(c: Ctx):
    """Read-only OneLake data access roles for the agent identity on lh_medallion (see ONELAKE_ROLES)."""
    ws, sp_id = c.cfg["fabric"]["workspace_id"], c.ac["identity_principal_id"]
    lh = lakehouse_id(c)
    roles = c.fabric("GET", f"/workspaces/{ws}/items/{lh}/dataAccessRoles")["value"]
    by_name = {r["name"]: r for r in roles}
    missing = [n for n in ONELAKE_ROLES if not any(m.get("objectId") == sp_id for m in
               ((by_name.get(n) or {}).get("members") or {}).get("microsoftEntraMembers") or [])]
    if not missing:
        print(f"onelake roles {', '.join(ONELAKE_ROLES)}: already present")
        return
    keep = [{k: r[k] for k in ("name", "decisionRules", "members") if k in r} for r in roles if r["name"] not in ONELAKE_ROLES]
    want = [{"name": n, "decisionRules": [{"effect": "Permit", "permission": [
                {"attributeName": "Path", "attributeValueIncludedIn": paths},
                {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]}]}],
             "members": {"microsoftEntraMembers": [{"tenantId": c.tenant, "objectId": sp_id}]}}
            for n, paths in ONELAKE_ROLES.items()]
    c.fabric("PUT", f"/workspaces/{ws}/items/{lh}/dataAccessRoles", json={"value": keep + want})
    print(f"onelake roles on lh_medallion for the agent identity: {', '.join(f'{n} {p}' for n, p in ONELAKE_ROLES.items())}")


# ---------------------------------------------------------------- code deploy
def setup_publish(c: Ctx):
    DIST.mkdir(exist_ok=True)
    pkg = DIST / "agent.zip"
    skip = {"__pycache__", ".venv", "tests", ".pytest_cache", "local.settings.json"}
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
        for p in AGENT_DIR.rglob("*"):
            rel = p.relative_to(AGENT_DIR)
            if p.is_file() and not any(part in skip for part in rel.parts):
                z.write(p, rel.as_posix())
    print(f"package {pkg} ({pkg.stat().st_size // 1024} KB); deploying with remote build ...")
    az("functionapp", "deployment", "source", "config-zip", "-g", c.rg, "-n", c.ac["function_app"],
       "--src", str(pkg), "--build-remote", "true", "-o", "none")
    print("deployed; functions:", az("functionapp", "function", "list", "-g", c.rg, "-n", c.ac["function_app"],
                                     "--query", "[].name", "-o", "tsv").replace("\n", ", "))


def setup_activator(c: Ctx):
    """Activator act_uc1_t1 (KQL source over rt_inventory_signal) -> UDF udf_agent_relay.relay_t1 -> OneLake outbox.

    The Key Vault + function-key relay was dropped (D-26): tenant policy forces Key Vault public access off and Fabric
    UDFs cannot reach a private vault, so the UDF writes to the outbox with its native Lakehouse connection instead."""
    ws = c.cfg["fabric"]["workspace_id"]
    lh = lakehouse_id(c)
    c.ac["outbox_lakehouse_id"], c.ac["outbox_dir"] = lh, OUTBOX_DIR
    setup_onelake_role(c)
    az("functionapp", "config", "appsettings", "set", "-g", c.rg, "-n", c.ac["function_app"], "--settings",
       f"OUTBOX_LAKEHOUSE_ID={lh}", f"OUTBOX_DIR={OUTBOX_DIR}", "-o", "none")
    print(f"app settings OUTBOX_LAKEHOUSE_ID/OUTBOX_DIR set ({OUTBOX_DIR} on lh_medallion)")

    udf = _ensure_item(c, ws, "UserDataFunction", UDF_NAME,
                       "Activator relay: writes T1 events to the coworker agent outbox on OneLake (SYNTHETIC demo)")
    _update_definition(c, ws, udf, udf_parts(ws, lh))
    exposed = _udf_functions(c, ws, udf)
    if UDF_FUNCTION not in exposed:
        raise RuntimeError(f"{UDF_NAME} does not expose {UDF_FUNCTION} after publish: {exposed}")
    print(f"udf {UDF_NAME} ({udf}) publishes {UDF_FUNCTION}")

    reflex = _ensure_item(c, ws, "Reflex", ACTIVATOR_NAME,
                          "UC1 T1 projected stock-out -> coworker agent outbox (SYNTHETIC demo)")
    entities = activator_entities(ws, c.cfg["fabric"]["realtime"]["kql_database_id"], udf)
    _update_definition(c, ws, reflex, [_part("ReflexEntities.json", json.dumps(entities))], kind="reflexes")
    back = _get_definition(c, ws, reflex, kind="reflexes")
    got = json.loads(back.get("ReflexEntities.json", "[]"))
    rules = [e for e in got if e["type"] == "timeSeriesView-v1" and e["payload"].get("definition", {}).get("type") == "Rule"]
    if not rules or not any(e["type"] == "fabricItemAction-v1" for e in got):
        raise RuntimeError(f"Activator readback incomplete: {[e['type'] for e in got]}")
    c.ac.update({"udf_relay_id": udf, "udf_relay_function": UDF_FUNCTION, "activator_id": reflex})
    c.save()
    print(f"activator {ACTIVATOR_NAME} ({reflex}): {len(got)} entities, rule '{rules[0]['payload']['name']}' "
          f"running={json.loads(json.dumps(rules[0]['payload']['definition'].get('settings', {}))).get('shouldRun')}")


UDF_NAME, UDF_FUNCTION, ACTIVATOR_NAME = "udf_agent_relay", "relay_t1", "act_uc1_t1"
OUTBOX_DIR = "Files/agent_outbox"
UDF_DIR = ROOT / "fabric" / "udf" / UDF_NAME
UDF_ALIAS = "lhmedallion"  # connection aliases must be alphanumeric
UDF_LIBRARY = ("fabric-user-data-functions", "1.0.142")
# One row per style x market per signal_ts: the case grain. No condition pre-filtering; the rule detects the T1 flip.
T1_KQL = """declare query_parameters(startTime:datetime, endTime:datetime);
rt_inventory_signal
| where signal_ts between (startTime .. endTime)
| summarize t1_sizes = countif(t1_intraday), sizes = count(), min_weeks_of_cover = min(intraday_weeks_of_cover)
    by signal_ts, style_id, market_id
| extend t1_intraday = t1_sizes > 0, case_key = strcat(style_id, '|', market_id),
    signal_ts_utc = strcat(format_datetime(signal_ts, 'yyyy-MM-dd'), 'T', format_datetime(signal_ts, 'HH:mm:ss'), 'Z')
| project signal_ts, case_key, style_id, market_id, signal_ts_utc, t1_intraday, t1_sizes, sizes, min_weeks_of_cover"""


def _part(path: str, text: str) -> dict:
    return {"path": path, "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"), "payloadType": "InlineBase64"}


def udf_parts(ws: str, lh: str) -> list[dict]:
    definition = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/userDataFunction/definition/1.1.0/schema.json",
        "runtime": "PYTHON",
        "connectedDataSources": [{"alias": UDF_ALIAS, "artifactId": lh, "artifactType": "Lakehouse", "workspaceId": ws}],
        "functions": [{"name": UDF_FUNCTION, "description": "Queue a T1 event for the coworker agent (OneLake outbox)",
                       "isPublicEndpointEnabled": False}],
        "libraries": {"public": [{"name": UDF_LIBRARY[0], "type": "PYPI", "version": UDF_LIBRARY[1]}], "private": []},
    }
    metadata = {"runtime": "PYTHON", "functionsMetadata": [{
        "name": UDF_FUNCTION, "scriptFile": "function_app.py",
        "bindings": [
            {"methods": ["POST"], "route": "", "authLevel": "Anonymous", "name": "req", "direction": "In", "type": "HttpTrigger"},
            {"itemType": None, "subType": "FabricLakehouseClient", "alias": UDF_ALIAS, "name": "outboxLakehouse",
             "direction": "In", "type": "FabricItem"}],
        "fabricProperties": {"fabricMetadataSchemaVersion": "1.1.0", "fabricFunctionReturnType": "str",
                             "fabricFunctionParameters": [{"dataType": "str", "name": n}
                                                          for n in ("styleId", "marketId", "signalTs")]}}]}
    return [_part("definition.json", json.dumps(definition, indent=2)),
            _part("resources/functions.json", json.dumps(metadata, indent=2)),
            _part("function_app.py", (UDF_DIR / "function_app.py").read_text(encoding="utf-8"))]


def activator_entities(ws: str, kql_db: str, udf: str) -> list[dict]:
    """ReflexEntities.json: container, KQL source, SourceEvent, Object(style x market) + SplitEvent + attributes,
    AttributeTrigger rule (t1_intraday BecomesTrue) and the UDF action (activator-authoring-cli schema, v1.2.4)."""
    gid = lambda: str(uuid.uuid4())  # noqa: E731
    V = "1.2.4"
    ids = {k: gid() for k in ("container", "source", "event", "object", "split", "key", "t1", "style", "market", "ts",
                              "woc", "action", "rule")}
    ref = lambda key: [{"name": "entityId", "type": "string", "value": ids[key]}]  # noqa: E731
    parent = {"parentContainer": {"targetUniqueIdentifier": ids["container"]}}
    in_obj = {**parent, "parentObject": {"targetUniqueIdentifier": ids["object"]}}
    view = lambda key, name, kind, tpl, extra: {"uniqueIdentifier": ids[key], "type": "timeSeriesView-v1", "payload": {  # noqa: E731
        "name": name, **extra, "definition": {"type": kind, "instance": json.dumps(tpl, separators=(",", ":"))}}}
    type_assert = lambda op: {"name": "TypeAssertion", "kind": "TypeAssertion", "arguments": [  # noqa: E731
        {"name": "op", "type": "string", "value": op}, {"name": "format", "type": "string", "value": ""}]}

    def attribute(key: str, name: str, field: str, op: str) -> dict:
        return view(key, name, "Attribute", {"templateId": "BasicEventAttribute", "templateVersion": V, "steps": [
            {"name": "EventSelectStep", "id": gid(), "rows": [
                {"name": "EventSelector", "kind": "Event", "arguments": [
                    {"kind": "EventReference", "type": "complex", "name": "event", "arguments": ref("split")}]},
                {"name": "EventFieldSelector", "kind": "EventField", "arguments": [
                    {"name": "fieldName", "type": "string", "value": field}]}]},
            {"name": "EventComputeStep", "id": gid(), "rows": [type_assert(op)]}]}, in_obj)

    def param(name: str, attr_key: str) -> dict:
        return {"kind": "FabricItemParameter", "type": "complex", "arguments": [
            {"name": "parameterName", "type": "string", "value": name},
            {"name": "parameterType", "type": "string", "value": "String"},
            {"name": "parameterValue", "type": "complexArray", "values": [
                {"kind": "AttributeReference", "type": "complexReference", "arguments": ref(attr_key)}]}]}

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    rule = {"templateId": "AttributeTrigger", "templateVersion": V, "steps": [
        {"name": "ScalarSelectStep", "id": gid(), "rows": [{"name": "AttributeSelector", "kind": "Attribute", "arguments": [
            {"kind": "AttributeReference", "type": "complex", "name": "attribute", "arguments": ref("t1")}]}]},
        {"name": "ScalarDetectStep", "id": gid(), "rows": [{"name": "LogicalBecomes", "kind": "LogicalBecomes", "arguments": [
            {"name": "op", "type": "string", "value": "BecomesTrue"}]}]},
        {"name": "ActStep", "id": gid(), "rows": [{"name": "FabricItemBinding", "kind": "FabricItemInvocation", "arguments": [
            {"name": "workspaceId", "type": "string", "value": ws},
            {"name": "itemId", "type": "string", "value": udf},
            {"name": "itemType", "type": "string", "value": "UserDataFunctions"},
            {"name": "jobType", "type": "string", "value": "Execute"},
            {"name": "fabricJobConnectionDocumentId", "type": "string", "value": ids["action"]},
            {"name": "additionalInformation", "type": "array", "values": []},
            {"name": "parameters", "type": "array", "values": [
                param("styleId", "style"), param("marketId", "market"), param("signalTs", "ts")]},
            {"name": "subitemId", "type": "string", "value": UDF_FUNCTION}]}]}]}
    return [
        {"uniqueIdentifier": ids["container"], "type": "container-v1",
         "payload": {"name": "UC1 inventory rebalancing triggers", "type": "kqlQueries"}},
        {"uniqueIdentifier": ids["source"], "type": "kqlSource-v1", "payload": {
            "name": "rt_inventory_signal by style x market", "runSettings": {"executionIntervalInSeconds": 300},
            "query": {"queryString": T1_KQL},
            "eventhouseItem": {"itemId": kql_db, "workspaceId": ws, "itemType": "KustoDatabase"},
            "queryParameters": [
                {"name": "startTime", "type": "DURATION_START", "value": f"{now - dt.timedelta(hours=1):%Y-%m-%dT%H:%M:%SZ}"},
                {"name": "endTime", "type": "DURATION_END", "value": f"{now:%Y-%m-%dT%H:%M:%SZ}"}],
            # notebook 06 appends each signal_ts snapshot a few minutes after the quarter hour
            "eventTimeSettings": {"timeFieldName": "signal_ts", "ingestionDelayInSeconds": 600, "timeZone": "UTC"},
            "metadata": {"workspaceId": ws, "measureName": "", "querySetId": "", "queryId": ""}, **parent}},
        view("event", "Inventory signal events", "Event", {"templateId": "SourceEvent", "templateVersion": V, "steps": [
            {"name": "SourceEventStep", "id": gid(), "rows": [{"name": "SourceSelector", "kind": "SourceReference",
                                                               "arguments": ref("source")}]}]}, parent),
        {"uniqueIdentifier": ids["object"], "type": "timeSeriesView-v1",
         "payload": {"name": "Style x market", **parent, "definition": {"type": "Object"}}},
        view("key", "case_key", "Attribute", {"templateId": "IdentityPartAttribute", "templateVersion": V, "steps": [
            {"name": "IdPartStep", "id": gid(), "rows": [type_assert("Text")]}]}, in_obj),
        view("split", "Style x market events", "Event", {"templateId": "SplitEvent", "templateVersion": V, "steps": [
            {"name": "SplitEventStep", "id": gid(), "rows": [
                {"name": "EventSelector", "kind": "Event", "arguments": [
                    {"kind": "EventReference", "type": "complex", "name": "event", "arguments": ref("event")}]},
                {"name": "FieldIdMapping", "kind": "FieldIdMapping", "arguments": [
                    {"name": "fieldName", "type": "string", "value": "case_key"},
                    {"kind": "AttributeReference", "type": "complex", "name": "idPart", "arguments": ref("key")}]},
                {"name": "SplitEventOptions", "kind": "EventOptions", "arguments": [
                    {"name": "isAuthoritative", "type": "boolean", "value": True}]}]}]}, in_obj),
        attribute("t1", "T1 intraday", "t1_intraday", "Logical"),
        attribute("style", "style_id", "style_id", "Text"),
        attribute("market", "market_id", "market_id", "Text"),
        attribute("ts", "signal_ts_utc", "signal_ts_utc", "Text"),
        attribute("woc", "Min intraday weeks of cover", "min_weeks_of_cover", "Number"),
        {"uniqueIdentifier": ids["action"], "type": "fabricItemAction-v1", "payload": {
            "name": f"{UDF_NAME}.{UDF_FUNCTION}", "fabricItem": {"itemId": udf, "workspaceId": ws, "itemType": "UserDataFunctions"},
            "jobType": "Execute", **parent}},
        {"uniqueIdentifier": ids["rule"], "type": "timeSeriesView-v1", "payload": {
            "name": "UC1 T1 projected stock-out", "description": "Created by: skills-for-fabric", **in_obj,
            "definition": {"type": "Rule", "instance": json.dumps(rule, separators=(",", ":")),
                           "settings": {"shouldRun": True, "shouldApplyRuleOnUpdate": False}}}},
    ]


def _lro(c: Ctx, method: str, path: str, body: dict | None = None) -> dict:
    hdr = lambda: {"Authorization": "Bearer " + c.token("https://api.fabric.microsoft.com")}  # noqa: E731
    r = requests.request(method, FABRIC + path, json=body if body is not None else {}, headers=hdr(), timeout=120)
    if r.status_code == 202 and r.headers.get("Location"):
        loc = r.headers["Location"]
        for _ in range(100):
            time.sleep(int(r.headers.get("Retry-After", "3")))
            s = requests.get(loc, headers=hdr(), timeout=60).json()
            if s.get("status") in ("Succeeded", "Failed", "Cancelled"):
                if s["status"] != "Succeeded":
                    raise RuntimeError(f"{method} {path} LRO {s['status']}: {json.dumps(s.get('error'))[:1500]}")
                res = requests.get(loc + "/result", headers=hdr(), timeout=60)
                return res.json() if res.status_code == 200 and res.content else {}
        raise RuntimeError(f"{method} {path} LRO timed out")
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:1500]}")
    return r.json() if r.content else {}


def _ensure_item(c: Ctx, ws: str, item_type: str, name: str, description: str) -> str:
    def find():
        return next((i["id"] for i in c.fabric("GET", f"/workspaces/{ws}/items?type={item_type}")["value"]
                     if i["displayName"] == name), None)
    item = find()
    if not item:
        item = _lro(c, "POST", f"/workspaces/{ws}/items",
                    {"displayName": name, "type": item_type, "description": description}).get("id") or find()
        print(f"created {item_type} {name} ({item})")
    return item


def _update_definition(c: Ctx, ws: str, item: str, parts: list[dict], kind: str = "items"):
    _lro(c, "POST", f"/workspaces/{ws}/{kind}/{item}/updateDefinition", {"definition": {"parts": parts}})


def _get_definition(c: Ctx, ws: str, item: str, kind: str = "items") -> dict[str, str]:
    d = _lro(c, "POST", f"/workspaces/{ws}/{kind}/{item}/getDefinition")
    return {p["path"]: base64.b64decode(p["payload"]).decode("utf-8") for p in d["definition"]["parts"]}


def _udf_functions(c: Ctx, ws: str, udf: str) -> list[str]:
    d = json.loads(_get_definition(c, ws, udf).get("definition.json", "{}"))
    return [f["name"] for f in d.get("functions", [])]


TRIGGER_NOTEBOOK = "07_trigger_agent_case"


def setup_trigger_notebook(c: Ctx, run: bool = False):
    """Operator notebook that queues a T1 event in the OneLake outbox (same path as Activator), bound to lh_medallion."""
    ws, lh = c.cfg["fabric"]["workspace_id"], lakehouse_id(c)
    nb = json.loads((ROOT / "fabric" / "notebooks" / f"{TRIGGER_NOTEBOOK}.ipynb").read_text(encoding="utf-8"))
    nb["metadata"]["dependencies"] = {"lakehouse": {"default_lakehouse": lh, "default_lakehouse_name": "lh_medallion",
                                                    "default_lakehouse_workspace_id": ws}}
    item = _ensure_item(c, ws, "Notebook", TRIGGER_NOTEBOOK, "Demo operator: start a coworker agent case (SYNTHETIC)")
    _lro(c, "POST", f"/workspaces/{ws}/notebooks/{item}/updateDefinition", {"definition": {"format": "ipynb", "parts": [
        _part("notebook-content.ipynb", json.dumps(nb))]}})
    print(f"notebook {TRIGGER_NOTEBOOK} ({item}) deployed with default lakehouse lh_medallion")
    if run:
        hdr = {"Authorization": "Bearer " + c.token("https://api.fabric.microsoft.com")}
        r = requests.post(f"{FABRIC}/workspaces/{ws}/items/{item}/jobs/instances?jobType=RunNotebook", headers=hdr, timeout=60)
        if r.status_code != 202:
            raise RuntimeError(f"run {TRIGGER_NOTEBOOK} -> {r.status_code}: {r.text[:500]}")
        for _ in range(60):
            time.sleep(10)
            st = requests.get(r.headers["Location"], headers=hdr, timeout=60).json().get("status")
            if st in ("Completed", "Failed", "Cancelled", "Deduped"):
                print(f"run {TRIGGER_NOTEBOOK}: {st}")
                if st != "Completed":
                    raise SystemExit(1)
                return
        raise RuntimeError(f"run {TRIGGER_NOTEBOOK} timed out")


# ---------------------------------------------------------------- Teams app
def build_teams_zip(c: Ctx) -> Path:
    manifest = json.loads((TEAMS_DIR / "manifest.template.json").read_text(encoding="utf-8"))
    text = json.dumps(manifest).replace("{{BOT_APP_ID}}", c.ac["identity_client_id"]) \
                               .replace("{{GRAPH_APP_ID}}", c.ac["graph_app_client_id"]) \
                               .replace("{{FUNCTION_HOST}}", c.ac["function_host"])
    DIST.mkdir(exist_ok=True)
    out = DIST / "teams-app.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(json.loads(text), indent=2))
        for icon in ("color.png", "outline.png"):
            z.write(TEAMS_DIR / icon, icon)
    return out


GRAPH_CLI_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft Graph Command Line Tools (public client)


def catalog_token(c: Ctx) -> str:
    """Org catalog publish needs delegated AppCatalog.ReadWrite.All, which the Azure CLI token does not carry."""
    from azure.identity import AuthenticationRecord, InteractiveBrowserCredential, TokenCachePersistenceOptions
    scope = "https://graph.microsoft.com/AppCatalog.ReadWrite.All"
    rec_path = Path.home() / ".appareliq" / "graph_catalog_auth.json"  # account pointer only, no token (outside repo)
    record = AuthenticationRecord.deserialize(rec_path.read_text()) if rec_path.exists() else None
    cred = InteractiveBrowserCredential(client_id=GRAPH_CLI_CLIENT_ID, tenant_id=c.tenant, authentication_record=record,
                                        cache_persistence_options=TokenCachePersistenceOptions(name="appareliq-catalog"),
                                        timeout=600)
    if record is None:
        print("browser sign-in: use the tenant admin account and accept consent if prompted ...", flush=True)
        rec_path.parent.mkdir(exist_ok=True)
        rec_path.write_text(cred.authenticate(scopes=[scope]).serialize())
    return cred.get_token(scope).token


def setup_teams_app(c: Ctx):
    pkg = build_teams_zip(c)
    ext_id = c.ac["identity_client_id"]
    auth_h = {"Authorization": "Bearer " + catalog_token(c)}
    r = requests.get(f"{GRAPH}/appCatalogs/teamsApps", params={"$filter": f"externalId eq '{ext_id}'"}, headers=auth_h,
                     timeout=60)
    r.raise_for_status()
    existing = r.json()["value"]
    headers = {"Content-Type": "application/zip", **auth_h}
    data = pkg.read_bytes()
    if existing:
        app_id = existing[0]["id"]
        r = requests.post(f"{GRAPH}/appCatalogs/teamsApps/{app_id}/appDefinitions", data=data, headers=headers, timeout=120)
        print(f"teams app {app_id}: new definition -> {r.status_code} {r.text[:300]}")
    else:
        r = requests.post(f"{GRAPH}/appCatalogs/teamsApps", data=data, headers=headers, timeout=120)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"catalog publish failed {r.status_code}: {r.text[:800]}")
        app_id = r.json()["id"]
        print(f"teams app published to org catalog: {app_id}")
    c.ac["teams_app_id"] = app_id
    c.save()
    az("functionapp", "config", "appsettings", "set", "-g", c.rg, "-n", c.ac["function_app"],
       "--settings", f"TEAMS_APP_ID={app_id}", "-o", "none")
    print("TEAMS_APP_ID app setting updated")


# ---------------------------------------------------------------- Foundry advisor agent (Teams / M365 Copilot)
AI = "https://ai.azure.com"
ARM = "https://management.azure.com"
FOUNDRY_AGENT = "marketplace-rebalancing-agent"
FOUNDRY_AGENT_DISPLAY = "Marketplace Rebalancing Agent"
KB_CONN = "kb-kinetiq-policies-mcp"
FABRIC_IQ_CONN = "fabriciq-rebalancing-dataagent"
POWERBI_API = "https://analysis.windows.net/powerbi/api"
# The KB MCP endpoint at 2026-08-01-preview lets the planner skip the knowledge source and synthesize an ungrounded
# answer (0 references; MCP callers cannot send alwaysQuerySource). 2025-11-01-preview always queries the source and
# returns references, so the agent's KB tool pins it (verified 2026-11-09).
KB_MCP_API = "2025-11-01-preview"


def kb_mcp_url(c: Ctx) -> str:
    fc = c.cfg["foundry"]
    return f"{fc['search_endpoint']}/knowledgebases/{fc['knowledge_base']}/mcp?api-version={KB_MCP_API}"
FOUNDRY_AGENT_INSTRUCTIONS = """You are the Kinetiq Marketplace Rebalancing Agent (use case UC1). All data is SYNTHETIC demo data; say so when you show numbers.
You help merchandise planners, marketplace leads, finance and wholesale colleagues decide how to rebalance inventory between marketplaces.

Tools:
- fabric-iq (the Fabric data agent ApparelIQ_Rebalancing_Agent): use it for every data question - inventory positions, days of cover, projected stock-outs, real-time T1 signals, sell-through, transfer options, margins, open rebalancing cases and past decisions. Never estimate or recompute numbers yourself; quote what the tool returns and name the period.
- kinetiq-policies (Foundry IQ knowledge base): use it for every policy, guardrail or approval question (transfer cost caps, margin floors, approval thresholds, launch protections). Quote thresholds exactly as the retrieved policy text states them and cite the policy id and section (for example KQ-FIN-102 §2). If the tool returns no policy text, say the policy could not be retrieved instead of answering.

Behaviour:
1. Recommend, never decide. The named human owner approves. You cannot change data: a transfer is executed only when the owner presses Approve on the case card that the coworker posts in the case group chat.
2. Be concise and practical, like a senior planner: lead with the answer, then 2-4 bullets of evidence, then the trade-off or risk.
3. If the tools do not return the answer, say so and suggest who to ask (planner, finance, wholesale) - do not guess.
4. Refuse requests unrelated to Kinetiq inventory rebalancing."""


def _project_arm_id(c: Ctx) -> str:
    fc = c.cfg["foundry"]
    return (f"/subscriptions/{c.cfg['subscription_id']}/resourceGroups/{c.rg}/providers/Microsoft.CognitiveServices"
            f"/accounts/{fc['account_name']}/projects/{fc['project_name']}")


def _connection(c: Ctx, name: str, props: dict):
    c.call("PUT", f"{ARM}{_project_arm_id(c)}/connections/{name}?api-version=2025-10-01-preview", ARM,
           json={"name": name, "type": "Microsoft.MachineLearningServices/workspaces/connections", "properties": props})
    print(f"project connection {name}: {props['category']}/{props['authType']}")


def foundry_agent_tools(c: Ctx) -> list[dict]:
    return [
        {"type": "mcp", "server_label": "kinetiq-policies", "server_url": kb_mcp_url(c),
         "require_approval": "never", "allowed_tools": ["knowledge_base_retrieve"], "project_connection_id": KB_CONN},
        {"type": "fabric_iq_preview", "server_label": "fabric-iq", "require_approval": "never",
         "server_url": c.cfg["fabric"]["data_agent"]["mcp_url"], "project_connection_id": FABRIC_IQ_CONN},
    ]


def setup_foundry_agent(c: Ctx):
    fc = c.cfg["foundry"]
    ep = fc["project_endpoint"]
    _connection(c, KB_CONN, {"authType": "ProjectManagedIdentity", "category": "RemoteTool", "isSharedToAll": True,
                             "target": kb_mcp_url(c), "audience": "https://search.azure.com/",
                             "metadata": {"ApiType": "Azure"}})
    # Fabric IQ honours Fabric permissions of the signed-in user (on-behalf-of), never an app identity.
    _connection(c, FABRIC_IQ_CONN, {"authType": "UserEntraToken", "category": "RemoteTool", "isSharedToAll": True,
                                    "target": c.cfg["fabric"]["data_agent"]["mcp_url"], "audience": POWERBI_API,
                                    "scopes": [f"{POWERBI_API}/DataAgent.Execute.All"]})
    ver = c.call("POST", f"{ep}/agents/{FOUNDRY_AGENT}/versions?api-version=v1", AI, json={
        "description": "UC1 marketplace rebalancing advisor (SYNTHETIC demo data). Advice only; write-back happens "
                       "only via the coworker case card after human approval.",
        "metadata": {"use_case": "UC1", "data_label": "SYNTHETIC"},
        "definition": {"kind": "prompt", "model": fc["chat_deployment"], "instructions": FOUNDRY_AGENT_INSTRUCTIONS,
                       "temperature": 0.2, "tools": foundry_agent_tools(c)}})
    version = str(ver["version"])
    print(f"agent {FOUNDRY_AGENT} version {version}")
    requests.patch(f"{ep}/agents/{FOUNDRY_AGENT}?api-version=v1", timeout=120,
                   headers={"Authorization": "Bearer " + c.token(AI), "Content-Type": "application/merge-patch+json"},
                   data=json.dumps({"agent_endpoint": {"version_selector": {"version_selection_rules": [
                       {"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100}]}}})
                   ).raise_for_status()
    c.ac.update({"foundry_agent": FOUNDRY_AGENT, "foundry_agent_version": version})
    c.save()
    print(f"active version pinned to {version}")


def foundry_ask(c: Ctx, question: str) -> dict:
    ep = c.cfg["foundry"]["project_endpoint"]
    return c.call("POST", f"{ep}/openai/v1/responses", AI, json={
        "input": question, "agent_reference": {"type": "agent_reference", "name": FOUNDRY_AGENT}})


def _output_text(resp: dict) -> tuple[str, list[str]]:
    text, tools = [], []
    for item in resp.get("output", []):
        if item.get("type", "").endswith("_call"):
            tools.append(f"{item['type']}:{item.get('server_label') or item.get('name', '')}"
                         f"{'!' + str(item['error'])[:200] if item.get('error') else ''}")
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                text.append(part["text"])
    return "\n".join(text), tools


def setup_foundry_smoke(c: Ctx):
    # (question, tool server_label that must be called, substrings that must all appear in the answer)
    cases = [("What is the maximum transfer cost as a share of retail value for a launch running style? Cite the policy.",
              "kinetiq-policies", ["6%", "KQ-FIN-102"]),
             ("Which style and market pairs currently have a projected stock-out (T1) and what is their days of cover?",
              "fabric-iq", ["HM9443"])]
    ok = True
    for q, tool, must in cases:
        text, tools = _output_text(foundry_ask(c, q))
        good = any(t.endswith(":" + tool) for t in tools) and all(m in text for m in must)
        ok &= good
        print(f"[{'PASS' if good else 'FAIL'}] Q: {q}\ntools: {tools}\nA: {text[:900]}\n")
    sys.exit(0 if ok else 1)


def setup_foundry_publish(c: Ctx, scope: str):
    ep = c.cfg["foundry"]["project_endpoint"]
    host = c.ac["function_host"]
    b64 = lambda p: base64.b64encode((TEAMS_DIR / p).read_bytes()).decode()  # noqa: E731
    body = {
        "agentDisplayName": FOUNDRY_AGENT_DISPLAY, "performBotRegistration": True, "publishScope": scope,
        "publishAsAutopilot": False, "appVersion": c.ac.get("foundry_agent_app_version", "1.0.0"),
        "shortDescription": "Kinetiq UC1 inventory rebalancing advisor (SYNTHETIC demo data).",
        "fullDescription": ("Answers questions about marketplace inventory, projected stock-outs, transfer options and "
                            "Kinetiq rebalancing policies using the Fabric data agent and the Foundry IQ policy knowledge "
                            "base. Advice only: transfers are approved by a named owner on the coworker case card. "
                            "All data is SYNTHETIC."),
        "developerName": "Kinetiq demo (SYNTHETIC)", "developerWebsiteUrl": f"https://{host}",
        "privacyUrl": f"https://{host}/privacy", "termsOfUseUrl": f"https://{host}/terms",
        "colorIconBase64": b64("color.png"), "outlineIconBase64": b64("outline.png"),
    }
    r = c.call("POST", f"{ep}/agents/{FOUNDRY_AGENT}/microsoft365/publish?api-version=v1", AI, json=body)
    print(json.dumps(r, indent=2)[:1500])
    c.ac.update({"foundry_agent_publish_scope": scope, "foundry_agent_title_id": r.get("titleId", r.get("title_id"))})
    c.save()
    if scope == "Tenant":
        print("NEXT (M365 admin): approve at https://admin.cloud.microsoft/?#/agents/all/requested and assign "
              f"availability to group {c.cfg['agent_users_group']['name']} ({c.cfg['agent_users_group']['object_id']}).")


# ---------------------------------------------------------------- validate
def validate(c: Ctx):
    ok = True

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal ok
        ok &= bool(cond)
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")

    host = c.ac["function_host"]
    try:
        code = az("functionapp", "function", "keys", "list", "-g", c.rg, "-n", c.ac["function_app"],
                  "--function-name", "health_check", "--query", "default", "-o", "tsv")
        r = requests.get(f"https://{host}/api/health", params={"code": code}, timeout=120)
        body = r.json() if r.ok else {}
        check("function /api/health", r.ok, json.dumps(body)[:400])
        deps = body.get("checks", body)
        for dep, res in deps.items():
            if isinstance(res, dict) and "ok" in res:
                check(f"  dependency {dep}", res.get("ok"), res.get("detail", ""))
    except Exception as e:  # noqa: BLE001
        check("function /api/health", False, str(e)[:200])
    r = requests.post(f"https://{host}/api/events/t1", json={}, timeout=60)
    check("events endpoint rejects anonymous calls", r.status_code == 401, str(r.status_code))
    r = requests.post(f"https://{host}/api/messages", json={"type": "message"}, timeout=60)
    check("bot endpoint rejects unauthenticated activities", r.status_code in (401, 403), str(r.status_code))
    pub = az("storage", "account", "show", "-n", c.ac["storage"], "-g", c.rg, "--query",
             "[publicNetworkAccess, allowSharedKeyAccess]", "-o", "tsv").split()
    check("function storage private + keyless", [p.lower() for p in pub] == ["disabled", "false"], str(pub))
    sp_id = c.ac["graph_app_sp_id"]
    roles = {g["appRoleId"] for g in c.graph("GET", f"/servicePrincipals/{sp_id}/appRoleAssignments")["value"]}
    check(f"graph app roles granted to {GRAPH_APP_NAME}", len(roles) >= len(GRAPH_APP_ROLES), str(len(roles)))
    mi_roles = c.graph("GET", f"/servicePrincipals/{c.ac['identity_principal_id']}/appRoleAssignments")["value"]
    check("managed identity holds no Graph app roles", not mi_roles, str(len(mi_roles)))
    ws = c.cfg["fabric"]["workspace_id"]
    if c.ac.get("udf_relay_id"):
        fns = _udf_functions(c, ws, c.ac["udf_relay_id"])
        check(f"UDF {UDF_NAME} exposes {UDF_FUNCTION}", UDF_FUNCTION in fns, str(fns))
    else:
        check(f"UDF {UDF_NAME} configured", False, "run 'activator'")
    if c.ac.get("activator_id"):
        ents = json.loads(_get_definition(c, ws, c.ac["activator_id"], kind="reflexes").get("ReflexEntities.json", "[]"))
        rules = [e for e in ents if e["payload"].get("definition", {}).get("type") == "Rule"]
        running = [r for r in rules if r["payload"]["definition"].get("settings", {}).get("shouldRun")]
        check(f"Activator {ACTIVATOR_NAME} rule running", bool(running), f"{len(running)}/{len(rules)} rules")
    if c.ac.get("foundry_agent"):
        ag = c.call("GET", f"{c.cfg['foundry']['project_endpoint']}/agents/{FOUNDRY_AGENT}?api-version=v1", AI)
        tools = [t["type"] for t in ag.get("versions", {}).get("latest", {}).get("definition", {}).get("tools", [])]
        check(f"Foundry agent {FOUNDRY_AGENT} tools", {"mcp", "fabric_iq_preview"} <= set(tools), str(tools))
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["deploy", "config", "grants", "publish", "activator", "trigger-notebook", "teams-app",
                                       "foundry-agent", "foundry-smoke", "foundry-publish", "validate"])
    ap.add_argument("--config", default=str(ROOT / "config" / "deployment.local.json"))
    ap.add_argument("--scope", choices=["Shared", "Tenant"], default="Tenant", help="foundry-publish scope")
    ap.add_argument("--run", action="store_true", help="trigger-notebook: also run it once (queues HM9443 / US)")
    a = ap.parse_args()
    c = Ctx(Path(a.config))
    {"deploy": setup_deploy, "config": setup_config, "grants": setup_grants, "publish": setup_publish,
     "activator": setup_activator, "trigger-notebook": lambda x: setup_trigger_notebook(x, a.run),
     "teams-app": setup_teams_app, "foundry-agent": setup_foundry_agent,
     "foundry-smoke": setup_foundry_smoke, "foundry-publish": lambda x: setup_foundry_publish(x, a.scope),
     "validate": validate}[a.action](c)


if __name__ == "__main__":
    main()
