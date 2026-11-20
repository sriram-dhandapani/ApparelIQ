# ApparelIQ — Deploy in your own tenant (step by step)

This guide takes you from an empty Azure subscription and Microsoft 365 tenant to a working demo: synthetic data in
Databricks and Azure SQL, the Fabric medallion, real-time signals, the Fabric data agent, Foundry IQ, and the
coworker agent that opens a Teams case chat and writes the decision back to the ERP after a person approves.

All data is **synthetic** (fictional brand *Kinetiq*). Plan for **one working day**, most of it waiting for
deployments, mirrors and indexing.

> Each step lists **who** must do it. *Deployer* = the person running the scripts. Steps marked **Admin** need a
> tenant-level role; send them to the right admin in advance (see [Step 3](#step-3--admin-checklist-do-this-first)).

---

## Step 1 — Get the latest code

```powershell
# first time
git clone https://github.com/sriram-dhandapani/ApparelIQ.git ApparelIQ
cd ApparelIQ

# later: update to the latest
git checkout main
git pull --ff-only
```

Check you have the expected layout: `infra/`, `scripts/`, `databricks/`, `fabric/`, `agent/`, `teams/`, `sql/`,
`foundry/`, `config/`, `docs/`.

> `config/deployment.local.json` is git-ignored. It holds **your** ids after deployment; never commit or share it.

---

## Step 2 — Install the workstation tools

| Tool | Version | Check |
|---|---|---|
| Azure CLI with Bicep | 2.60 or later | `az version` · `az bicep upgrade` |
| Python | 3.11 | `python --version` |
| Git | any | `git --version` |
| ODBC Driver 18 for SQL Server | 18 | only for `setup_fabric.py verify` |

```powershell
python -m pip install --upgrade pip
pip install requests pyodbc numpy pandas reportlab pypdf msal azure-identity
```

Sign in to the target tenant and subscription:

```powershell
az login --tenant <tenant-id>
az account set --subscription <subscription-id>
az account show --query "{tenant:tenantId, sub:id, user:user.name}" -o table
```

> If you work in more than one tenant, run `az account show` before every step. The scripts use whatever account the
> Azure CLI has active.

---

## Step 3 — Admin checklist (do this first)

Ask your admins to complete these before you start. Several settings take up to 15 minutes to take effect.

### 3.1 Azure (subscription owner)

| # | Action | Why |
|---|---|---|
| A1 | Give the deployer **Owner** (or Contributor + User Access Administrator) on a new resource group, e.g. `Apparels` | Bicep creates resources and role assignments |
| A2 | Make sure the subscription has quota for **Standard_D8ds_v5** (or similar) in at least one region | Databricks job clusters (see [Step 6](#step-6--deploy-databricks-compute)) |
| A3 | Make sure Azure OpenAI **gpt-4.1** and **text-embedding-3-small** are available in your Foundry region | Foundry IQ and the agent |
| A4 | A paid **Microsoft Fabric F-SKU capacity** (F8 or larger recommended); the deployer is a **capacity admin** | Fabric workspace, mirroring, data agent |

### 3.2 Microsoft Entra (Global Administrator or Privileged Role Administrator)

| # | Action | Why |
|---|---|---|
| E1 | Deployer can **create app registrations** | 4 app registrations are created (Databricks connection, SQL connection, SharePoint ingest, agent Graph app) |
| E2 | Create security group **`sg-appareliq-sql-admins`**; add the deployer | Entra-only admin of Azure SQL |
| E3 | Create security group **`sg-appareliq-agent-users`**; add everyone who will use the agent | Agent is published only to this group; also gets Viewer on the Fabric workspace |
| E4 | Create or pick **demo users** for the personas in [Step 4.2](#42-map-the-personas-to-real-users) (at least 1, ideally 3–4) | The agent adds them to Teams case chats; approvals are checked by UPN |
| E5 | Be available to **grant admin consent** for Microsoft Graph application permissions during [Step 12](#step-12--foundry-iq-policy-knowledge) and [Step 13](#step-13--coworker-agent) | `Sites.Selected`, `Chat.Create`, `TeamsAppInstallation.ReadWriteAndConsentSelfForChat.All`, `User.Read.All` |

Create the groups (or use the Entra admin center):

```powershell
az ad group create --display-name sg-appareliq-sql-admins  --mail-nickname sg-appareliq-sql-admins
az ad group create --display-name sg-appareliq-agent-users --mail-nickname sg-appareliq-agent-users
az ad group member add --group sg-appareliq-sql-admins --member-id $(az ad signed-in-user show --query id -o tsv)
az ad group show --group sg-appareliq-sql-admins  --query id -o tsv   # -> sql.admin_group_id
az ad group show --group sg-appareliq-agent-users --query id -o tsv   # -> agent_users_group.object_id
```

### 3.3 Databricks (account admin)

| # | Action | Why |
|---|---|---|
| D1 | Deployer is a **Databricks account admin** (accounts.azuredatabricks.net), or an account admin assigns a **Unity Catalog metastore** to the new workspace's region and makes the deployer metastore admin | Unity Catalog catalog, storage credential, external location |

### 3.4 Fabric (Fabric administrator, in the Fabric admin portal → Tenant settings)

| # | Tenant setting | Scope |
|---|---|---|
| F1 | *Service principals can use Fabric APIs* | the deployer, or a group containing the deployer |
| F2 | *Users can access data stored in OneLake with apps external to Fabric* | enabled |
| F3 | *Mirroring* (Azure SQL Database and Azure Databricks catalog) | enabled |
| F4 | *Users can create Ontology (preview) items* | enabled; **wait ~15 minutes** before [Step 10](#step-10--semantic-model-ontology-data-agent) |
| F5 | *Users can use Copilot, AI Agents and other AI experiences powered by Azure OpenAI* | enabled (data agent) |
| F6 | *Capacities can be designated as Fabric Copilot capacities* | enabled for your F-SKU |
| F7 | *Data agent* / *Users can create and share Data agent item types (preview)* | enabled |
| F8 | *User data functions (preview)* and *Activator* | enabled (Activator rule calls a user data function) |

### 3.5 SharePoint (SharePoint administrator or site owner)

| # | Action | Why |
|---|---|---|
| S1 | Create a SharePoint site and a folder, e.g. `Policies`, and copy its **sharing link** | Foundry IQ indexes the 5 synthetic policy PDFs from it |
| S2 | Be available to sign in once (device code) during [Step 12](#step-12--foundry-iq-policy-knowledge) | Uploads the PDFs and grants the ingest app read on that one site (`Sites.Selected`) |
| S3 | No Conditional Access policy that blocks the Azure AI Search SharePoint indexer | The indexer signs in as the ingest app |

### 3.6 Teams and Microsoft 365 (Teams administrator, Microsoft 365 administrator)

| # | Action | Why |
|---|---|---|
| T1 | Teams admin center → **Teams apps → Setup / Permission policies**: allow **custom (org) apps** for the agent users | The coworker bot is an org app |
| T2 | Be available to sign in once in a browser during `setup_agent.py teams-app` (needs `AppCatalog.ReadWrite.All`) | Publishes the bot to the org app catalog |
| T3 | After `setup_agent.py foundry-publish`: approve the agent at **Microsoft 365 admin center → Agents → Requested** (`https://admin.cloud.microsoft/?#/agents/all/requested`) and make it available to **`sg-appareliq-agent-users`** only | Agent appears in Microsoft 365 Copilot / Teams for that group |

---

## Step 4 — Create your configuration file

```powershell
Copy-Item config\deployment.example.json config\deployment.local.json
```

### 4.1 Fill these values now

| Key | Value |
|---|---|
| `tenant_id`, `subscription_id` | from `az account show` |
| `resource_group`, `location` | e.g. `Apparels`, `centralus` (hub region for SQL, storage, Foundry, Function) |
| `sql.admin_group`, `sql.admin_group_id` | `sg-appareliq-sql-admins` and its object id (Step 3.2) |
| `fabric.capacity_id` | Fabric admin portal → Capacity settings → your F-SKU → copy the capacity id |
| `agent_users_group.object_id` | object id of `sg-appareliq-agent-users` |

Everything marked `<filled by ...>` is written by the scripts. Do not edit those by hand.

### 4.2 Map the personas to real users

The synthetic ERP has invented people (personas). The agent invites the **mapped** Entra user to the Teams chat and
only lets the mapped *accountable* user decide. Any persona you leave out maps to `fallback_user`.

| Persona id | Synthetic role | Needed for the US demo case |
|---|---|---|
| `dortiz` | Regional Marketplace Director, North America (**accountable owner** for US) | **Yes** — approves / rejects |
| `pnair` | Senior Merchandise Planner, Running (US) | Recommended |
| `ohaddad` | Finance Partner (co-approval above the materiality limit) | Recommended |
| `mlee` | Wholesale Account Lead, North America | Optional |
| `sm-us-1001` (and other `sm-us-*`) | Store managers for donor / receiving stores | Optional |
| `fbrandt`, `gkim` | Europe director, APAC analyst (other markets) | Optional |

For each user you map, get the object id:

```powershell
az ad user show --id user@contoso.com --query "{upn:userPrincipalName, object_id:id, display_name:displayName}"
```

Fill `agent_users_group` like this (one user can stand in for several personas):

```json
"agent_users_group": {
  "name": "sg-appareliq-agent-users",
  "object_id": "<group object id>",
  "fabric_workspace_role": "Viewer",
  "persona_map": {
    "dortiz":     { "upn": "director@contoso.com", "object_id": "<oid>", "display_name": "Demo Director" },
    "pnair":      { "upn": "planner@contoso.com",  "object_id": "<oid>", "display_name": "Demo Planner" },
    "ohaddad":    { "upn": "finance@contoso.com",  "object_id": "<oid>", "display_name": "Demo Finance" },
    "sm-us-1001": { "upn": "store@contoso.com",    "object_id": "<oid>", "display_name": "Demo Store Manager" }
  },
  "fallback_user": { "upn": "admin@contoso.com", "object_id": "<oid>", "display_name": "Demo Admin" },
  "demo_operator": { "upn": "", "object_id": "", "display_name": "" }
}
```

* `fallback_user` — receives every unmapped persona and escalations. It is also the operator of the agent's Azure
  resources (`operatorObjectId` in `infra/agent.bicep`). Use the deployer or a demo admin.
* `demo_operator` — optional. A user who may take **every** decision on every case (useful when presenting alone).
  Leave `upn` empty to disable.
* Values may only contain letters, digits, space and `_ . @ -`.
* Add every mapped user to `sg-appareliq-agent-users`.

---

## Step 5 — Deploy the hub infrastructure

Private networking, ADLS Gen2 for Unity Catalog, Azure SQL (ERP/WMS), Databricks access connector. Storage and SQL
have **public network access disabled**.

```powershell
$rg = "Apparels"
az group create -n $rg -l centralus
az deployment group create -g $rg -f infra/main.bicep `
  -p sqlAdminObjectId=<sql admin group oid> sqlAdminLogin=sg-appareliq-sql-admins sqlAdminPrincipalType=Group
az deployment group show -g $rg -n main --query properties.outputs -o json
```

Copy the outputs into `storage.account_name`, `sql.server_fqdn`, `databricks.access_connector_id`.

---

## Step 6 — Deploy Databricks compute

Databricks runs in a region where your subscription has VM capacity. Check first (repeat for candidate regions):

```powershell
az vm list-skus -l swedencentral --size Standard_D8ds_v5 --query "[].restrictions" -o json   # [] or [[]] = available
```

Then deploy with a `/16` that does not overlap the hub (`10.40.0.0/16`):

```powershell
az deployment group create -g $rg -f infra/databricks-compute-region.bicep `
  -p location=swedencentral addressPrefix=10.43.0.0/16
az deployment group show -g $rg -n databricks-compute-region --query properties.outputs -o json
```

Copy `databricks.workspace_url`, `workspace_name` and `region` into the config. If clusters later fail with
`SkuNotAvailable`, see the README troubleshooting table.

---

## Step 7 — Unity Catalog and synthetic source data (Databricks → Azure SQL)

```powershell
$cfg = "config/deployment.local.json"
python scripts/setup_databricks.py --config $cfg setup       # catalog, storage credential, external location, notebooks
python scripts/setup_databricks.py --config $cfg run-batch   # DS-01/02: sell-through + digital intent into Unity Catalog
python scripts/setup_databricks.py --config $cfg run-erp     # ERP/WMS tables + prior-decision history into Azure SQL
```

All synthetic data is created inside Databricks notebooks (`databricks/notebooks/01_*` and `02_*`); same seed, same
data. Use `--profile tiny` or `small` for a smaller, faster world.

**Check:** the jobs finish with `SUCCESS`; the output shows ~1.98 M `sell_through_daily` rows for the `demo` profile.

---

## Step 8 — Fabric workspace and mirroring

```powershell
python scripts/setup_fabric.py workspace        # workspace ApparelIQ on your capacity, workspace identity, lakehouses
```

Allow the Fabric workspace through the storage firewall — **re-run Step 5 with two extra parameters** (keep them on
every future re-run of `main.bicep`):

```powershell
az deployment group create -g $rg -f infra/main.bicep `
  -p sqlAdminObjectId=<oid> sqlAdminLogin=sg-appareliq-sql-admins sqlAdminPrincipalType=Group `
     fabricWorkspaceId=<fabric.workspace_id> fabricWorkspaceIdentityObjectId=<fabric.workspace_identity_sp_id>
```

Then:

```powershell
python scripts/setup_fabric.py dbx-mirror                                 # mirror the Unity Catalog catalog
python scripts/setup_fabric.py sql-principal                              # service principal for SQL mirroring
python scripts/setup_databricks.py --config $cfg upload
python scripts/setup_databricks.py --config $cfg run-sql-grants           # its DB user + least-privilege grants
python scripts/setup_fabric.py sql-mirror                                 # VNet data gateway, connection, mirrored DB
python scripts/setup_fabric.py verify                                     # sample T-SQL on both lakehouses
```

**Check:** `verify` prints row counts for `lh_databricks` and `lh_erp_wms`. If mirrored tables stay *Initialized*,
see the README troubleshooting table (error 22736).

---

## Step 9 — Medallion and real-time intelligence

```powershell
python scripts/setup_fabric.py medallion --run                 # notebooks 00–04: Bronze, Silver, Gold (+ DQ checks)
python scripts/setup_fabric.py verify --lakehouse lh_medallion
python scripts/setup_fabric.py realtime --run                  # Eventhouse, Eventstream, simulator, signal, dashboard
```

**Check:** `verify` shows Bronze/Silver/Gold counts and passing DQ rules; `realtime` reports the T1 alerts and every
dashboard tile as OK.

---

## Step 10 — Semantic model, ontology, data agent

Make sure the Ontology tenant setting (F4) has been on for at least 15 minutes.

```powershell
python scripts/setup_fabric.py semantic     # sm_appareliq + 48 DAX checks
python scripts/setup_fabric.py ontology     # ApparelIQ_Ontology + graph refresh + GQL checks
python scripts/setup_fabric.py dataagent    # publish ApparelIQ_Rebalancing_Agent + 14-question benchmark
```

**Check:** each command ends without failures; `fabric/data_agent/benchmark_results.json` shows all cases passing.
Then share the data agent with `sg-appareliq-agent-users` (Fabric → data agent → Share → *Read*), if not already
covered by the workspace Viewer role.

---

## Step 11 — Foundry resources

```powershell
python scripts/build_policy_pdfs.py                                     # 5 synthetic policy PDFs in foundry/knowledge/
az deployment group create -g $rg -n appareliq-foundry -f infra/foundry.bicep `
  -p operatorObjectId=$(az ad signed-in-user show --query id -o tsv)
python scripts/setup_foundry.py config                                  # copy outputs into foundry.*
```

---

## Step 12 — Foundry IQ (policy knowledge)

**Admin needed** (Entra consent, SharePoint sign-in).

```powershell
python scripts/setup_foundry.py app                                     # ingest app, Sites.Selected, admin consent
python scripts/setup_foundry.py upload --share-url "<SharePoint folder sharing link>"   # device-code sign-in
python scripts/setup_foundry.py ks                                      # SharePoint knowledge source; waits for indexing
python scripts/setup_foundry.py kb
python scripts/setup_foundry.py validate                                # 7 retrieval checks, non-zero exit on failure
```

**Check:** `validate` passes all cases with citations to `KQ-*` documents.

---

## Step 13 — Coworker agent

**Admin needed** (Graph consent, Teams catalog, Microsoft 365 approval). Run in this order:

| # | Command | What it does |
|---|---|---|
| 13.1 | `python scripts/setup_agent.py deploy` | `infra/agent.bicep`: Function App, managed identity, Azure Bot, Key Vault, storage; writes `agent.*` to the config |
| 13.2 | `python scripts/setup_agent.py grants` | Graph app `appareliq-agent-graph` (no secret), Graph application roles, Fabric workspace Viewer, OneLake read roles |
| 13.3 | `python scripts/setup_databricks.py --config $cfg upload` then `... run-sql-agent` | Agent schema + write-back gateway in Azure SQL, persona → user map (Step 4.2), the agent identity's SQL grants |
| 13.4 | `python scripts/setup_agent.py publish` | Deploys the agent code to the Function App |
| 13.5 | `python scripts/setup_agent.py activator` | Fabric user data function `udf_agent_relay` + Activator rule on the T1 signal |
| 13.6 | `python scripts/setup_agent.py trigger-notebook` | Fabric notebook `07_trigger_agent_case` for starting a case on demand |
| 13.7 | `python scripts/setup_agent.py teams-app` | Builds `dist/teams-app.zip` and publishes it to the org app catalog (browser sign-in as Teams admin) |
| 13.8 | `python scripts/setup_agent.py validate` | Health, dependency and permission checks |
| 13.9 | `python scripts/setup_agent.py foundry-agent` | Foundry prompt agent with Foundry IQ and the Fabric data agent as tools |
| 13.10 | `python scripts/setup_agent.py foundry-smoke` | Grounded policy and data questions; non-zero exit on wrong answers |
| 13.11 | `python scripts/setup_agent.py foundry-publish` | Publishes the agent to Microsoft 365 / Teams; prints the admin approval link |

Then the **Microsoft 365 admin** approves the agent and limits it to `sg-appareliq-agent-users` (checklist T3).

If you change a mapped user later, update `persona_map` and re-run 13.3.

---

## Step 14 — Run the demo end to end

1. Make sure the Fabric capacity is **running** (Azure portal → Fabric capacity → *Resume*).
2. Start a case: in the Fabric workspace open notebook **`07_trigger_agent_case`** → *Run all* (defaults: style
   `HM9443`, market `US`), or run `python scripts/setup_agent.py trigger-notebook --run`.
3. Within about 1–2 minutes the mapped users get a Teams case chat with the Adaptive Card (problem, charts, options,
   policy check).
4. The accountable user (`dortiz` mapping or `demo_operator`) chooses **Approve**, **Revise**, **Choose another** or
   **Reject**. The decision is written to Azure SQL through the gateway and the card updates.
5. Ask the agent in Microsoft 365 Copilot / Teams: *"List the decisions taken on past cases."*

See `README.md → Operations` for demo resets
(close a case, free store stock again).

---

## Step 15 — Keep up to date

```powershell
git pull --ff-only
```

All scripts are idempotent. After pulling, re-run only the parts that changed:

| Changed folder | Re-run |
|---|---|
| `agent/` | `setup_agent.py publish` |
| `teams/` | `setup_agent.py teams-app` |
| `fabric/notebooks/0[0-4]*` | `setup_fabric.py medallion --run` |
| `fabric/notebooks/0[5-6]*`, `fabric/eventstream/` | `setup_fabric.py realtime` |
| `fabric/data_agent/` | `setup_fabric.py dataagent` |
| `fabric/ontology/` | `setup_fabric.py ontology` |
| `databricks/notebooks/` | `setup_databricks.py upload` (then the matching `run-*`) |
| `sql/azure_sql/05–07*` | `setup_databricks.py upload` then `run-sql-agent` |
| `infra/*.bicep` | the matching `az deployment group create` (keep the Fabric parameters on `main.bicep`) |

---

## Clean up

```powershell
az group delete -g $rg
```

Then delete the Fabric workspace `ApparelIQ`, the VNet data gateway, the app registrations (`sp-appareliq-fabric-dbx`,
`sp-appareliq-fabric-sql`, `appareliq-sharepoint-ingest`, `appareliq-agent-graph`), the Teams app from the org catalog,
the published agent in the Microsoft 365 admin center, the SharePoint `Policies` files, and `~/.appareliq/`.

---

## Common problems

| Symptom | Fix |
|---|---|
| Script uses the wrong tenant | `az account show`; `az login --tenant <id>`; `az account set --subscription <id>` |
| `ontology` returns `403 FeatureNotAvailable` | Tenant setting F4 is off or was enabled less than 15 minutes ago |
| `grants`, `app` fail with *Authorization_RequestDenied* | The signed-in user cannot grant Graph application permissions; ask a Privileged Role Administrator to run that step |
| `teams-app` fails with 403 | Sign in as a Teams administrator; check custom apps are allowed (T1) |
| No Teams chat appears | Capacity paused; mapped users not in `persona_map`; Teams app not allowed for those users; run `setup_agent.py validate` |
| Agent not visible in Copilot | Microsoft 365 admin has not approved it yet, or the user is not in `sg-appareliq-agent-users` |

More in `README.md → Troubleshooting`.
