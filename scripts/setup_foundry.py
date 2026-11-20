"""Idempotent Foundry IQ setup for the ApparelIQ demo (Phase 2 step 8, AI-4 policy retrieval over DS-18).

  python scripts/build_policy_pdfs.py          # (re)generate the SYNTHETIC policy PDFs in foundry/knowledge/
  az deployment group create -g <rg> -n appareliq-foundry -f infra/foundry.bicep -p operatorObjectId=<your oid>
  python scripts/setup_foundry.py config       # copy the Bicep outputs into config/deployment.local.json (foundry.*)
  python scripts/setup_foundry.py app          # Entra ingest app (Graph Sites.Selected) + federated credential for the search MI
  python scripts/setup_foundry.py upload --share-url <SharePoint folder sharing link>
                                               # device-code sign-in: upload PDFs, grant the ingest app read on the site
  python scripts/setup_foundry.py ks           # indexedSharePoint knowledge source scoped to the folder; wait for ingestion
  python scripts/setup_foundry.py kb           # knowledge base (gpt-4.1 query planning + answer synthesis)
  python scripts/setup_foundry.py validate     # retrieve benchmark: policy questions with expected facts + citations

Auth: everything is keyless. Azure CLI tokens are used for ARM, Graph app management and the Search data plane.
SharePoint upload and the Sites.Selected site grant use a one-time MSAL device-code sign-in (Microsoft Graph Command
Line Tools public client) because the Azure CLI Graph token carries no Files/Sites scopes. The indexer authenticates to
SharePoint with a federated credential (search system-assigned MI -> ingest app); no client secret exists.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "foundry" / "knowledge"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_CLI_CLIENT = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft Graph Command Line Tools (public client)
SEARCH_API = "2026-08-01-preview"
INGEST_APP = "appareliq-sharepoint-ingest"
KS_NAME = "ks-kinetiq-policies"
KB_NAME = "kb-kinetiq-policies"
DEPLOYMENT = "appareliq-foundry"

RETRIEVAL_INSTRUCTIONS = (
    "The knowledge source holds SYNTHETIC Kinetiq marketplace policies: KQ-POL-101 inventory allocation and "
    "rebalancing, KQ-FIN-102 markdown pricing and margin guardrails, KQ-WHS-103 wholesale partner terms, KQ-BRD-104 "
    "launch playbook and brand presentation, KQ-GOV-105 decision rights, approval and escalation. Prefer the policy "
    "that owns the topic and follow its cross-references.")
ANSWER_INSTRUCTIONS = (
    "Answer only from the retrieved policy text. Cite the document id and section (for example KQ-FIN-102 §4.1) for "
    "every rule you state. Quote thresholds, limits and dates exactly. If the policies do not cover the question, say "
    "so. All content is synthetic demo data for the fictional brand Kinetiq.")
RETRIEVE_PREAMBLE = (
    "You answer questions about Kinetiq marketplace policies (SYNTHETIC). Sources have a ref_id that must be cited. "
    "If the sources do not contain the answer, respond with 'I do not know'.")

# (id, question, any-of groups that must all appear in the answer, expected source document id)
BENCHMARK = [
    ("F01", "What is the minimum gross margin for a LAUNCH lifecycle SKU and what is the transfer cost cap?",
     [["55%", "55 %"], ["6%", "6 %"]], "KQ-FIN-102"),
    ("F02", "Can we mark down style HM9443 (Men's AeroGlide 5) in October 2026?",
     [["no", "not"], ["2026-11-05", "november 5", "5 november", "11-05"]], "KQ-FIN-102"),
    ("F03", "Can units committed to a wholesale partner be used as a source for a rebalancing transfer?",
     [["never", "not", "no"], ["consent", "commit"]], "KQ-WHS-103"),
    ("F04", "A rebalancing decision has a projected gross margin impact of $65,000. Who must approve it?",
     [["50,000", "50k", "$50"], ["finance", "omar"]], "KQ-GOV-105"),
    ("F05", "What happens if an approver does not respond to a rebalancing approval request?",
     [["4 business hours", "four business hours"], ["delegate"]], "KQ-GOV-105"),
    ("F06", "What minimum depth must a FLAGSHIP store keep for a launch style?",
     [["3 units", "three units", "3 per size", "3 unit"]], "KQ-BRD-104"),
    # Negative: not covered by any policy -> must decline rather than invent a rule.
    ("F07", "What is Kinetiq's employee parental leave entitlement?",
     [["do not know", "don't know", "not cover", "no information", "not contain", "not address", "not include"]], None),
]


def az(*args: str) -> str:
    return subprocess.run(["az", *args], capture_output=True, text=True, check=True,
                          shell=sys.platform == "win32").stdout.strip()


def az_token(tenant: str, resource: str) -> str:
    return az("account", "get-access-token", "--tenant", tenant, "--resource", resource, "--query", "accessToken",
              "-o", "tsv")


class Ctx:
    def __init__(self, cfg_path: Path):
        self.cfg_path = cfg_path
        self.cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        self.tenant = self.cfg["tenant_id"]
        self._tok: dict[str, tuple[float, str]] = {}

    @property
    def fc(self) -> dict:
        return self.cfg.setdefault("foundry", {})

    def save(self):
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2) + "\n", encoding="utf-8")

    def token(self, resource: str) -> str:
        t = self._tok.get(resource)
        if not t or time.time() - t[0] > 40 * 60:
            t = (time.time(), az_token(self.tenant, resource))
            self._tok[resource] = t
        return t[1]

    def call(self, method: str, url: str, resource: str, ok=(200, 201, 202, 204), **kw):
        for _ in range(6):
            r = requests.request(method, url, headers={"Authorization": "Bearer " + self.token(resource)},
                                 timeout=120, **kw)
            if r.status_code in (429, 503):
                time.sleep(int(r.headers.get("Retry-After", "20")))
                continue
            break
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:1500]}")
        return r.json() if r.content and "json" in r.headers.get("Content-Type", "") else {}

    def graph(self, method: str, path: str, **kw):
        return self.call(method, GRAPH + path, "https://graph.microsoft.com", **kw)

    def search(self, method: str, path: str, **kw):
        sep = "&" if "?" in path else "?"
        return self.call(method, f"{self.fc['search_endpoint']}{path}{sep}api-version={SEARCH_API}",
                         "https://search.azure.com", **kw)


# ---------------------------------------------------------------- config
def setup_config(c: Ctx):
    out = json.loads(az("deployment", "group", "show", "-g", c.cfg["resource_group"], "-n", DEPLOYMENT,
                        "--query", "properties.outputs", "-o", "json"))
    v = {k: o["value"] for k, o in out.items()}
    c.fc.update({
        "search_name": v["searchName"], "search_endpoint": v["searchEndpoint"],
        "search_principal_id": v["searchPrincipalId"],
        "account_name": v["foundryName"], "account_endpoint": v["foundryEndpoint"],
        "openai_endpoint": v["openAIEndpoint"],
        "project_name": v["projectName"], "project_endpoint": v["projectEndpoint"],
        "project_principal_id": v["projectPrincipalId"],
        "chat_deployment": v["chatDeployment"], "embedding_deployment": v["embeddingDeployment"],
        "knowledge_source": KS_NAME, "knowledge_base": KB_NAME,
    })
    c.save()
    print(json.dumps(c.fc, indent=2))


# ---------------------------------------------------------------- Entra ingest app
def setup_app(c: Ctx):
    fc = c.fc
    apps = c.graph("GET", f"/applications?$filter=displayName eq '{INGEST_APP}'")["value"]
    body = {"displayName": INGEST_APP, "signInAudience": "AzureADMyOrg",
            "notes": "Azure AI Search SharePoint indexer for the ApparelIQ Foundry IQ knowledge source (SYNTHETIC demo).",
            "isFallbackPublicClient": True,
            "publicClient": {"redirectUris": ["https://login.microsoftonline.com/common/oauth2/nativeclient"]}}
    gsp = c.graph("GET", f"/servicePrincipals?$filter=appId eq '{GRAPH_APP_ID}'&$select=id,appRoles")["value"][0]
    role = next(r["id"] for r in gsp["appRoles"] if r["value"] == "Sites.Selected")
    body["requiredResourceAccess"] = [{"resourceAppId": GRAPH_APP_ID,
                                       "resourceAccess": [{"id": role, "type": "Role"}]}]
    if apps:
        app = apps[0]
        c.graph("PATCH", f"/applications/{app['id']}", json=body)
        print(f"app {INGEST_APP} exists: {app['appId']}")
    else:
        app = c.graph("POST", "/applications", json=body)
        print(f"app {INGEST_APP} created: {app['appId']}")
    sps = c.graph("GET", f"/servicePrincipals?$filter=appId eq '{app['appId']}'")["value"]
    sp = sps[0] if sps else c.graph("POST", "/servicePrincipals", json={"appId": app["appId"]})

    granted = c.graph("GET", f"/servicePrincipals/{sp['id']}/appRoleAssignments")["value"]
    if not any(g["appRoleId"] == role and g["resourceId"] == gsp["id"] for g in granted):
        c.graph("POST", f"/servicePrincipals/{gsp['id']}/appRoleAssignedTo",
                json={"principalId": sp["id"], "resourceId": gsp["id"], "appRoleId": role})
        print("admin consent: Microsoft Graph Sites.Selected (application)")

    mi = c.graph("GET", f"/servicePrincipals/{fc['search_principal_id']}?$select=appId,displayName")
    fic = {"name": "search-system-mi", "issuer": f"https://login.microsoftonline.com/{c.tenant}/v2.0",
           "subject": fc["search_principal_id"], "audiences": ["api://AzureADTokenExchange"],
           "description": f"Azure AI Search {fc['search_name']} system-assigned managed identity"}
    fics = c.graph("GET", f"/applications/{app['id']}/federatedIdentityCredentials")["value"]
    if not any(f["subject"] == fic["subject"] and f["issuer"] == fic["issuer"] for f in fics):
        c.graph("POST", f"/applications/{app['id']}/federatedIdentityCredentials", json=fic)
        print(f"federated credential added for search MI {mi['displayName']}")
    fc.update({"ingest_app_id": app["appId"], "ingest_app_object_id": app["id"], "ingest_sp_id": sp["id"],
               "search_mi_app_id": mi["appId"]})
    c.save()
    print(f"ingest app {app['appId']}; search MI client id {mi['appId']}")


# ---------------------------------------------------------------- SharePoint upload + site grant (delegated)
def delegated_graph_token(tenant: str) -> str:
    import msal
    cache_path = Path.home() / ".appareliq" / "msal_graph_cache.json"
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(GRAPH_CLI_CLIENT, authority=f"https://login.microsoftonline.com/{tenant}",
                                       token_cache=cache)
    scopes = ["https://graph.microsoft.com/Sites.FullControl.All", "https://graph.microsoft.com/Files.ReadWrite.All"]
    accts = app.get_accounts()
    res = app.acquire_token_silent(scopes, account=accts[0]) if accts else None
    if not res:
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"device flow failed: {flow}")
        print(flow["message"], flush=True)
        res = app.acquire_token_by_device_flow(flow)
    if "access_token" not in res:
        raise RuntimeError(f"sign-in failed: {res.get('error')}: {res.get('error_description')}")
    if cache.has_state_changed:
        cache_path.parent.mkdir(exist_ok=True)
        cache_path.write_text(cache.serialize(), encoding="utf-8")
    return res["access_token"]


def setup_upload(c: Ctx, share_url: str | None):
    fc = c.fc
    share_url = share_url or fc.get("sharepoint", {}).get("share_url")
    if not share_url:
        raise SystemExit("--share-url is required the first time")
    tok = delegated_graph_token(c.tenant)
    h = {"Authorization": "Bearer " + tok}

    def g(method, path, ok=(200, 201), **kw):
        r = requests.request(method, GRAPH + path, headers={**h, **kw.pop("headers", {})}, timeout=120, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:1000]}")
        return r.json() if r.content else {}

    enc = "u!" + base64.urlsafe_b64encode(share_url.encode()).decode().rstrip("=")
    folder = g("GET", f"/shares/{enc}/driveItem")
    if "folder" not in folder:
        raise SystemExit("the sharing link must point to a folder")
    drive_id = folder["parentReference"]["driveId"]
    root = g("GET", f"/drives/{drive_id}/root?$select=sharepointIds,webUrl")
    spi = root["sharepointIds"]
    host = urlparse(spi["siteUrl"]).hostname
    site_id = f"{host},{spi['siteId']},{spi['webId']}"
    folder_url = unquote(folder["webUrl"])
    print(f"site {spi['siteUrl']}  library {unquote(root['webUrl'])}  folder {folder_url}")

    manifest = json.loads((KNOWLEDGE / "manifest.json").read_text(encoding="utf-8"))
    for d in manifest["documents"]:
        data = (KNOWLEDGE / d["file"]).read_bytes()
        item = g("PUT", f"/drives/{drive_id}/items/{folder['id']}:/{d['file']}:/content", data=data,
                 headers={"Content-Type": "application/pdf"})
        print(f"  uploaded {d['file']} ({item['size']} bytes)")
    listed = {i["name"] for i in g("GET", f"/drives/{drive_id}/items/{folder['id']}/children?$select=name")["value"]}
    missing = [d["file"] for d in manifest["documents"] if d["file"] not in listed]
    if missing:
        raise RuntimeError(f"not visible after upload: {missing}")

    perms = g("GET", f"/sites/{site_id}/permissions")["value"]
    app_id = fc["ingest_app_id"]
    if not any(i.get("application", {}).get("id") == app_id
               for p in perms for i in p.get("grantedToIdentitiesV2", p.get("grantedToIdentities", []))):
        g("POST", f"/sites/{site_id}/permissions",
          json={"roles": ["read"], "grantedToIdentities": [{"application": {"id": app_id, "displayName": INGEST_APP}}]})
        print(f"site grant: {INGEST_APP} -> read on {spi['siteUrl']}")
    else:
        print(f"site grant exists for {INGEST_APP}")
    fc["sharepoint"] = {"share_url": share_url, "site_url": spi["siteUrl"], "site_id": site_id,
                        "drive_id": drive_id, "folder_id": folder["id"], "folder_url": folder_url}
    c.save()


# ---------------------------------------------------------------- knowledge source
def connection_string(c: Ctx) -> str:
    fc = c.fc
    return (f"SharePointOnlineEndpoint={fc['sharepoint']['site_url']};ApplicationId={fc['ingest_app_id']};"
            f"FederatedCredentialApplicationId={fc['search_mi_app_id']};TenantId={c.tenant}")


def wait_ks(c: Ctx, expected: int, timeout_min: int = 30) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_min * 60:
        st = c.search("GET", f"/knowledgesources/{KS_NAME}/status")
        last, cur = st.get("lastSynchronizationState") or {}, st.get("currentSynchronizationState") or {}
        print(f"  [{int(time.time() - t0)}s] status={st.get('synchronizationStatus')} "
              f"processed={last.get('itemsUpdatesProcessed')} "
              f"failed={last.get('itemsUpdatesFailed')} current={bool(cur)}", flush=True)
        if last.get("endTime") and not cur:
            return st
        time.sleep(20)
    raise TimeoutError("knowledge source ingestion did not finish")


def index_doc_count(c: Ctx) -> int:
    ks = c.search("GET", f"/knowledgesources/{KS_NAME}")
    idx = ks["indexedSharePointParameters"]["createdResources"]["index"]
    r = requests.get(f"{c.fc['search_endpoint']}/indexes/{idx}/docs/$count?api-version={SEARCH_API}",
                     headers={"Authorization": "Bearer " + c.token("https://search.azure.com")}, timeout=60)
    r.raise_for_status()
    return int(r.text.strip().lstrip("\ufeff"))


def setup_ks(c: Ctx, recreate: bool):
    fc = c.fc
    folder = fc["sharepoint"]["folder_url"]
    existing = {k["name"] for k in c.search("GET", "/knowledgesources?$select=name")["value"]}
    if recreate and KS_NAME in existing:
        c.search("DELETE", f"/knowledgesources/{KS_NAME}")
        existing.discard(KS_NAME)
        print(f"deleted {KS_NAME}")
    body = {
        "name": KS_NAME, "kind": "indexedSharePoint",
        "description": "SYNTHETIC Kinetiq marketplace policy corpus (DS-18): allocation/rebalancing, margin "
                       "guardrails, wholesale terms, launch playbook, decision rights. AI-4 grounding.",
        "indexedSharePointParameters": {
            "connectionString": connection_string(c),
            "containerName": "useQuery",
            "query": f"includeFolder={folder}",
            "ingestionParameters": {
                "networkAccessMode": "public", "identity": None,
                "embeddingModel": {"kind": "azureOpenAI", "azureOpenAIParameters": {
                    "resourceUri": fc["openai_endpoint"], "deploymentId": fc["embedding_deployment"],
                    "modelName": fc["embedding_deployment"]}},
                "chatCompletionModel": None, "disableImageVerbalization": True,
                "ingestionSchedule": None, "ingestionPermissionOptions": [], "contentExtractionMode": "minimal"},
        },
    }
    if KS_NAME in existing:
        print(f"knowledge source {KS_NAME} exists (use --recreate to rebuild)")
    else:
        c.search("PUT", f"/knowledgesources/{KS_NAME}", json=body)
        print(f"knowledge source {KS_NAME} created (folder {folder})")
    expected = len(json.loads((KNOWLEDGE / "manifest.json").read_text(encoding="utf-8"))["documents"])
    st = wait_ks(c, expected)
    last = st["lastSynchronizationState"]
    if last.get("itemsUpdatesFailed"):
        raise RuntimeError(f"ingestion errors: {json.dumps(st, indent=2)[:3000]}")
    n = index_doc_count(c)
    print(f"ingestion done: {last.get('itemsUpdatesProcessed')} files processed, {last.get('itemsUpdatesFailed')} failed, {n} chunks indexed")
    if not n:
        raise RuntimeError(f"no chunks indexed; status: {json.dumps(st, indent=2)[:3000]}")


# ---------------------------------------------------------------- knowledge base
def setup_kb(c: Ctx):
    fc = c.fc
    body = {
        "name": KB_NAME,
        "description": "Kinetiq marketplace policy knowledge base (SYNTHETIC) for the UC1 Marketplace Rebalancing "
                       "Agent: guardrails, commitments, launch protection, approvals and escalation.",
        "knowledgeSources": [{"name": KS_NAME}],
        "models": [{"kind": "azureOpenAI", "azureOpenAIParameters": {
            "resourceUri": fc["openai_endpoint"], "deploymentId": fc["chat_deployment"],
            "modelName": fc["chat_deployment"]}}],
        "retrievalReasoningEffort": {"kind": "low"},
        "outputMode": "answerSynthesis",
        "retrievalInstructions": RETRIEVAL_INSTRUCTIONS,
        "answerInstructions": ANSWER_INSTRUCTIONS,
    }
    c.search("PUT", f"/knowledgebases/{KB_NAME}", json=body)
    fc["knowledge_base_mcp_url"] = f"{fc['search_endpoint']}/knowledgebases/{KB_NAME}/mcp?api-version={SEARCH_API}"
    c.save()
    print(f"knowledge base {KB_NAME} created/updated -> {KS_NAME} (gpt-4.1 answer synthesis)")


# ---------------------------------------------------------------- validation
def retrieve(c: Ctx, question: str) -> dict:
    # The 2026-08-01-preview planner can skip retrieval and synthesize an ungrounded answer with 0 references.
    # Mitigations: a leading assistant instruction message (as in the docs) and alwaysQuerySource, which is a
    # per-request knowledgeSourceParams flag only (not settable on the knowledge base).
    return c.search("POST", f"/knowledgebases/{KB_NAME}/retrieve", json={
        "messages": [{"role": "assistant", "content": [{"type": "text", "text": RETRIEVE_PREAMBLE}]},
                     {"role": "user", "content": [{"type": "text", "text": question}]}],
        "knowledgeSourceParams": [{"knowledgeSourceName": KS_NAME, "kind": "indexedSharePoint",
                                   "alwaysQuerySource": True,
                                   "includeReferences": True, "includeReferenceSourceData": True}]})


def validate(c: Ctx, only: list[str] | None):
    passed = 0
    cases = [b for b in BENCHMARK if not only or b[0] in only]
    for cid, q, groups, doc in cases:
        r = retrieve(c, q)
        text = " ".join(x.get("text", "") for m in r.get("response", []) for x in m.get("content", []))
        refs = r.get("references", [])
        blob = text.lower()
        miss = [g for g in groups if not any(s.lower() in blob for s in g)]
        cited = doc is None or doc.lower() in blob or doc.lower() in json.dumps(refs).lower()
        ok = not miss and cited and (refs or doc is None)
        passed += bool(ok)
        print(f"{'PASS' if ok else 'FAIL'} {cid} refs={len(refs)} cites {doc}={cited} {'' if not miss else miss}")
        print("     Q: " + q)
        print("     A: " + text.replace("\n", " ")[:600])
    print(f"\n{passed}/{len(cases)} passed")
    return passed, len(cases)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "deployment.local.json"))
    ap.add_argument("action", choices=["config", "app", "upload", "ks", "kb", "validate"])
    ap.add_argument("--share-url", help="upload: SharePoint sharing link of the target folder")
    ap.add_argument("--recreate", action="store_true", help="ks: delete and recreate the knowledge source")
    ap.add_argument("--cases", nargs="*", metavar="ID", help="validate: run only these case ids")
    a = ap.parse_args()
    c = Ctx(Path(a.config))
    if a.action == "config":
        setup_config(c)
    elif a.action == "app":
        setup_app(c)
    elif a.action == "upload":
        setup_upload(c, a.share_url)
    elif a.action == "ks":
        setup_ks(c, a.recreate)
    elif a.action == "kb":
        setup_kb(c)
    else:
        p, t = validate(c, a.cases)
        if p < t:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
