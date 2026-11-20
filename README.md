# ApparelIQ — UC1 Athletic Marketplace Inventory Rebalancing (prototype)

A Microsoft Fabric + Azure AI Foundry prototype of a **coworker agent** that helps a marketplace team rebalance
inventory for the fictional athletic brand **Kinetiq**. **All data is SYNTHETIC.**

| Doc | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | As-built architecture, identities, network, medallion, real time, semantic model, ontology, verification results |
| [docs/architecture.md §8](docs/architecture.md#8-end-to-end-flow-as-built) | End-to-end flow: which Fabric, Azure, Foundry, and Microsoft 365 items run at each step |
| [docs/deployment-guide.md](docs/deployment-guide.md) | Step-by-step deployment in your own tenant, including every admin action |

## Repository layout

```
infra/                      Bicep: main.bicep (hub: network, ADLS, SQL, access connector)
                            databricks-compute-region.bicep (VNet-injected Databricks in a region with VM capacity)
databricks/notebooks/       All data creation and resets, self-contained (no custom package): 01 batch sources -> UC,
                            02 ERP/WMS -> Azure SQL (table DDL, one cell per table, row-count widgets, prior-decision
                            history), 03 apply admin/security SQL inside the VNet, 04 agent gateway tests,
                            05 demo resets (close a case, free store stock; preview rolls back)
fabric/notebooks/           Self-contained (no custom library): 00 lh_medallion setup (lakehouse + zero-copy Bronze
                            shortcuts), 01 Bronze feed generator, 02 validate mirror shortcuts, 03 Silver, 04 Gold;
                            one cell per table, validation cells fail the run
fabric/pipelines/           pl_medallion: (01 ∥ 02) -> 03 -> 04 (ids injected at deploy time)
fabric/notebooks/05_*       Real-time event simulator (Python kernel, DuckDB over Gold) -> Eventstream custom endpoint
fabric/notebooks/06_*       Intraday signal snapshots -> rt_inventory_signal (Python kernel, scheduled every 15 min)
fabric/eventhouse/          kdb_appareliq.kql: RT tables, update policies, dedup views, Gold external tables, T1 functions
fabric/eventstream/         es-appareliq-rt topology (custom endpoint -> Eventhouse)
fabric/semantic_model/      sm_appareliq TMDL (Direct Lake on lh_medallion)
fabric/ontology/            ApparelIQ_Ontology spec (entities, keys, bindings, relationships, time series)
fabric/data_agent/          Data agent spec (instructions, source config, benchmark) + last benchmark results
sql/azure_sql/              Setup and security only (no data): Fabric mirroring user (03), agent schema and gateway (05),
                            persona-to-user map from local config (06), agent identity grants (07), diagnostics (90)
scripts/setup_databricks.py Idempotent UC setup, workspace upload, notebook job runs
scripts/setup_fabric.py     Idempotent Fabric workspace, connections, mirroring, shortcuts, medallion, real time,
                            semantic model, ontology and data agent deployment, plus verification
infra/foundry.bicep         Foundry resource + project, gpt-4.1 / text-embedding-3-small, AI Search, RBAC (keyless)
scripts/build_policy_pdfs.py  Deterministic SYNTHETIC policy PDFs (DS-18) -> foundry/knowledge/ (+ manifest.json)
scripts/setup_foundry.py    Idempotent Foundry IQ: ingest app, SharePoint upload, knowledge source/base, retrieval benchmark
config/deployment.example.json  Template -> copy to config/deployment.local.json (git-ignored)
```

> **Deploying in a new tenant?** Follow [docs/deployment-guide.md](docs/deployment-guide.md): it starts from
> `git clone`, lists every admin action (Azure, Entra, Databricks, Fabric, SharePoint, Teams, Microsoft 365), and
> covers the coworker agent steps that the execution order table below does not.

## Prerequisites

* Azure CLI ≥ 2.60 with Bicep, logged in: `az login --tenant <tenant-id>`
* Python 3.11 with `pip install requests pyodbc numpy pandas`
  (`pyodbc` needs **ODBC Driver 18 for SQL Server**; only used by `setup_fabric.py verify`);
  Foundry IQ steps also need `pip install reportlab pypdf msal`
* Roles
  * Owner (or Contributor + User Access Administrator) on the resource group
  * Entra: can create app registrations (two service principals are created for Fabric connections)
  * Databricks **account admin** / metastore admin for the region (Unity Catalog setup)
  * Fabric: capacity admin on a paid **F-SKU** capacity; tenant settings *service principals can use Fabric APIs*,
    *OneLake third-party access*, mirroring enabled, *Users can create Ontology (preview) items* (task 6; takes
    ~15 min to propagate)
* An Entra group for SQL admins (object id goes into `sqlAdminObjectId`)
* Foundry IQ (steps 20–26): Entra role that can grant Graph application consent (Privileged Role Administrator or
  Global Administrator) and a SharePoint admin/site owner for the device-code upload and `Sites.Selected` site grant;
  a SharePoint folder for the policies; the tenant must not apply Conditional Access that blocks the SharePoint indexer

> Tenant policy in the reference environment forces Storage and SQL public network access to **Disabled**; the design
> assumes that. Nothing is reachable from the internet; administration of SQL runs from Databricks inside the VNet.

## Configuration

```powershell
Copy-Item config\deployment.example.json config\deployment.local.json
# Fill tenant/subscription/resource group/location and, after step 1, the Bicep outputs.
```

No secrets are stored in the config. Service-principal secrets for Fabric connections are minted at run time
(90-day lifetime) and passed only to Fabric.

## Execution order

| # | Step | Command |
|---|---|---|
| 1 | Hub infrastructure | `az deployment group create -g <rg> -f infra/main.bicep -p sqlAdminObjectId=<group-oid> sqlAdminLogin=<group-name> sqlAdminPrincipalType=Group` |
| 2 | Databricks in a region with VM capacity (preflight first, see Troubleshooting) | `az deployment group create -g <rg> -f infra/databricks-compute-region.bicep -p location=<region> addressPrefix=<non-overlapping /16>` |
| 3 | Fill `config/deployment.local.json` from the outputs of 1 and 2 | — |
| 4 | Unity Catalog objects + upload notebooks/package | `python scripts/setup_databricks.py --config config/deployment.local.json setup` |
| 5 | Generate batch sources (DS-01, DS-02) into UC | `python scripts/setup_databricks.py --config config/deployment.local.json run-batch` |
| 6 | Generate and load ERP/WMS into Azure SQL with notebook `02_load_erp_wms_sql`: creates the tables, loads them (incl. `ref.store_manager` and store↔store lanes) and seeds the prior-decision history (DS-10: 6 decisions, 3 stock transfer orders; idempotent). Run `upload` first after notebook changes | `python scripts/setup_databricks.py --config config/deployment.local.json upload` then `... run-erp` |
| 6a | *(optional)* Classic Pro SQL warehouse for the Databricks SQL editor | `python scripts/setup_databricks.py --config config/deployment.local.json warehouse` |
| 7 | Fabric workspace, workspace identity, lakehouses | `python scripts/setup_fabric.py workspace` |
| 8 | Allow the Fabric workspace through the storage firewall + RBAC (re-run step 1 with the new ids) | add `-p fabricWorkspaceId=<fabric.workspace_id> fabricWorkspaceIdentityObjectId=<fabric.workspace_identity_sp_id>` to step 1 |
| 9 | Mirror the Databricks UC catalog + lakehouse shortcuts | `python scripts/setup_fabric.py dbx-mirror` |
| 10 | Service principal for SQL mirroring | `python scripts/setup_fabric.py sql-principal` |
| 11 | Create its database user + least-privilege grants (runs inside the VNet) | `python scripts/setup_databricks.py --config config/deployment.local.json upload` then `... run-sql-grants` |
| 12 | VNet data gateway, SQL connection, mirrored database, shortcuts, workspace role for the SQL server identity | `python scripts/setup_fabric.py sql-mirror` |
| 13 | Verify both lakehouses with sample T-SQL | `python scripts/setup_fabric.py verify` |
| 14 | Medallion: deploy notebooks 00–04 and `pl_medallion`, run them. The script only deploys items; notebook `00_lh_medallion_setup` owns everything in `lh_medallion` (creates the lakehouse, the zero-copy `bronze.*` shortcuts to both mirrors, cleanup). On a first deployment the script runs 00 once so 01–04 can be bound to the new lakehouse | `python scripts/setup_fabric.py medallion --run` (or `--run 03 04` for a subset, `--via-pipeline` to run the pipeline instead) |
| 15 | Verify the medallion (layer counts, DQ rules, triggers, T3 pairs, checks) | `python scripts/setup_fabric.py verify --lakehouse lh_medallion` |
| 16 | Real time: Eventhouse + KQL schema, Eventstream, notebooks 05 and 06, Real-Time Dashboard; runs notebook 06 (signal snapshots + 24 h backfill) and schedules it every 15 min for 7 days (`realtime.signal_refresh_minutes` / `signal_schedule_days`); `--run` first streams the synthetic events. Checks the tables, the signal, the T1 alerts and every dashboard tile | `python scripts/setup_fabric.py realtime --run` |
| 17 | Semantic model `sm_appareliq`: deploy TMDL, refresh, 48 DAX checks against Gold | `python scripts/setup_fabric.py semantic` |
| 18 | Ontology `ApparelIQ_Ontology`: deploy, refresh the graph, GQL checks against the lakehouse and KQL | `python scripts/setup_fabric.py ontology` |
| 19 | Data agent `ApparelIQ_Rebalancing_Agent` from `fabric/data_agent/appareliq_data_agent.json`: rebuild the draft (instructions, ontology, semantic model with all tables selected, preview runtime), publish, then run the 14-question benchmark through the published MCP endpoint (results in `fabric/data_agent/benchmark_results.json`; exits non-zero on any failure) | `python scripts/setup_fabric.py dataagent` (`--benchmark-only [--cases B07]` to re-test without republishing) |
| 20 | Generate the SYNTHETIC policy corpus (DS-18): 5 PDFs + `manifest.json` in `foundry/knowledge/` (deterministic) | `python scripts/build_policy_pdfs.py` |
| 21 | Foundry resource + project, gpt-4.1 and text-embedding-3-small, AI Search Standard, RBAC | `az deployment group create -g <rg> -n appareliq-foundry -f infra/foundry.bicep -p operatorObjectId=$(az ad signed-in-user show --query id -o tsv)` |
| 22 | Copy the Foundry outputs into the local config (`foundry.*`) | `python scripts/setup_foundry.py config` |
| 23 | Entra ingest app (Graph `Sites.Selected`, admin consent, federated credential for the search MI) | `python scripts/setup_foundry.py app` |
| 24 | Upload the PDFs to the SharePoint folder and grant the ingest app read on that site (device-code sign-in as a SharePoint/Global admin; token cached in `~/.appareliq/`) | `python scripts/setup_foundry.py upload --share-url "<folder sharing link>"` |
| 25 | Indexed SharePoint knowledge source scoped to the folder; waits for ingestion | `python scripts/setup_foundry.py ks` (`--recreate` to rebuild) |
| 26 | Knowledge base + retrieval benchmark (7 cases; exits non-zero on failure) | `python scripts/setup_foundry.py kb` then `python scripts/setup_foundry.py validate` |

All scripts are idempotent; re-running a step reuses existing items and writes generated ids back to the local config.

> **Always pass `fabricWorkspaceId` and `fabricWorkspaceIdentityObjectId` when re-deploying `main.bicep`** after step 8,
> otherwise the storage resource-instance rule for Fabric is removed and the Databricks mirror stops.

### Synthetic data volume

All generation code lives inside the Databricks notebooks; there is no separate Python package and nothing extra to
install. Each notebook starts with shared "world" cells (markets, stores, styles, SKUs, demand), then has one cell per
table. The same seed always produces the same data.

Notebook `01_generate_batch_sources` widgets:

| Widget | Default | Meaning |
|---|---|---|
| `mode` | `write` | `write` (re)creates the UC tables; `verify` regenerates and compares checksums with the existing tables, writing nothing |
| `profile` | `demo` | `tiny` / `small` / `demo` size preset; the widgets below override it when not blank |
| `seed`, `as_of`, `history_days` | from profile | Random seed, last business day, days of history |
| `n_markets`, `stores_per_market`, `n_styles` | from profile | World size |
| `demand_scale`, `dq_defect_rate` | from profile | Demand multiplier and share of deliberately defective rows |
| `max_rows_sell_through_daily`, `max_rows_digital_intent_daily` | `0` (all) | Row cap per table; a fair, repeatable sample per market |
| `catalog`, `schema` | `appareliq`, `kinetiq_batch` | Unity Catalog target |

Notebook `02_load_erp_wms_sql` uses the same world widgets plus `mode` (`write` or `preview`, which only builds the
tables and shows row counts), `orders_per_key_partner` (3), `orders_per_partner` (2), `po_style_share` (0.35),
`movement_days` (14) and `movements_per_day` (60). Its cells must run in order, because wholesale orders reserve stock
and donor stores move stock in `wms.inventory_position`.

From a workstation: `python scripts/setup_databricks.py run-batch --mode verify` (or `run-erp --mode preview`).
Reference run (`demo`, defaults): `sell_through_daily` 1,981,279 rows, `digital_intent_daily` 296,685 rows,
`wms.inventory_position` 144,396 rows.

Fabric notebook `01_bronze_generate_sources` has one cell per Bronze feed table (weather, regional events, member
engagement, returns) and writes straight to `bronze.file_*`, with no files and no custom library. Its *parameters*
cell has `seed`/`dq_defect_rate` (None = reuse `bronze.ref_synthetic_run`), `engagement_scale`,
`members_per_location`, `hero_small_return_share`, weather gap rates and the scenario markets. The feeds are derived
from the mirrored sales, masters and events, so they always match the mirrored sources.

## Tests

Testing runs in Fabric only; there is no local test suite. Each notebook ends with a validation cell, and 03/04 write
their layer only after it passes:

* **01**: tables present, SYNTHETIC labels, foreign keys, planted defects present (DQ-01/02/05/07/08), weather coverage.
* **02**: every mirrored table reachable, non-empty and labelled.
* **03**: Bronze lineage, Silver row counts, keys (not null, unique), FKs, null handling, DQ-04/07/08/11 invariants.
* **04**: Gold keys and FKs, business rules T1–T5, sustained vs spike, and the planted demo scenario.

Results are stored in `silver.dq_check_result` and `gold.dq_check_result`. Run
`python scripts/setup_fabric.py medallion --via-pipeline` and then `verify --lakehouse lh_medallion`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Databricks cluster fails with `SkuNotAvailable` / quota stockout | Subscription VM restrictions are region-specific and only visible through ARM preflight. Run `az deployment group validate` with a VM SKU in candidate regions; deploy `databricks-compute-region.bicep` where it passes. |
| Mirrored catalog connection: *Untrusted token is not supported* | Databricks connections do not accept workspace identity; `dbx-mirror` uses service principal `sp-appareliq-fabric-dbx`. |
| SQL connection: `DMTS_InvalidCredentialTypeError` | Connections through a VNet gateway do not accept workspace identity; `sql-principal` creates `sp-appareliq-fabric-sql`. |
| `CREATE USER ... FROM EXTERNAL PROVIDER` fails | SQL server identity lacks Directory Readers; `03_fabric_mirroring.sql` uses `WITH SID = <appId>, TYPE = E` instead. |
| Mirrored tables stay *Initialized*, diagnostics show error **22736** (landing zone) | The SQL server's system managed identity needs a role on the Fabric workspace; `sql-mirror` grants Contributor. Inspect with `setup_databricks.py run-sql-diag`. |
| `verify` shows missing tables | Shortcut sync to the SQL endpoint lags a minute; re-run. |
| Mirrored catalog and its SQL analytics endpoint are disconnected; API returns `SQLEndpointCreationFailed` | Endpoint provisioning failed and left an orphaned SQL endpoint that cannot be deleted and blocks the name. Set a new `fabric.mirrored_catalog_name` and run `setup_fabric.py dbx-mirror --recreate` (also retargets the lakehouse shortcuts). |
| Databricks SQL editor on **Serverless Starter Warehouse** fails with `Error while reading file abfss://...` | Serverless compute runs outside the VNet and cannot reach the private storage account. Use the classic warehouse `appareliq-classic-pro` (`setup_databricks.py warehouse`) and fully qualify names: `appareliq.kinetiq_batch.<table>`. |
| Fabric Spark reading `lh_databricks` tables fails with `DELTA_PATH_BASED_ACCESS_TO_CATALOG_MANAGED_TABLE_BLOCKED` or *Couldn't locate commit coordinator* | Mirrored UC tables are catalog-managed. Read through the SQL endpoint: `spark.read.synapsesql("lh_databricks.dbo.<table>")` (notebooks 02 and 03 do this; the `bronze.dbx_*` shortcuts have the same restriction). |
| `ontology` fails with `403 FeatureNotAvailable` | Tenant setting *Users can create Ontology (preview) items* is off, or was enabled less than ~15 minutes ago. |
| `verify --lakehouse lh_medallion` waits on `Invalid object name 'gold...'` | SQL endpoint metadata sync lags new tables by a few minutes; the script retries. |
| Adding the ontology to the data agent fails with `OntologyAddAsDataSourceBlocked` | The staging datasources API does not accept ontologies; `dataagent` attaches it through `updateDefinition` instead. |
| Data agent answers *"There's content here I can't work with"* | Platform content filter on imperative prompts (e.g. "Approve X now"); rephrase as a question. |
| `infra/foundry.bicep` fails with `RequestConflict … Another operation is in progress` on the Foundry account | Children of the account must deploy sequentially; the template chains project → deployments → connection. Re-run. |
| Knowledge base answers with **0 references** and invented numbers | Preview planner skipped retrieval. Send `knowledgeSourceParams[].alwaysQuerySource: true` and a leading assistant instruction message (as `setup_foundry.py validate` does). |
| `upload` gets `authorization_pending` | The device code expired before sign-in; re-run and complete the sign-in within ~15 minutes. |
| Silver/Gold checks fail on data you just reloaded into Azure SQL (e.g. new tables missing) | The mirror `md_erp_wms` is **Paused**, so Silver reads stale data. Check `getMirroringStatus` / `getTablesMirroringStatus` on the mirrored database and resume with `POST …/mirroredDatabases/{id}/startMirroring`. |
| `run-sql-agent` fails with *unsafe value for PERSONA_MAP_ROWS* | Notebook `03_apply_sql_script` only accepts `[A-Za-z0-9 _.@-]` substitutions; `setup_databricks.py` renders the persona rows locally into the bundle. Update the script, don't pass SQL as a substitution. |

## Operations

* **Secret rotation**: re-running `dbx-mirror` / `sql-mirror` after deleting the connection mints a new 90-day secret.
* **Start a demo case manually:** in the Fabric workspace run notebook `07_trigger_agent_case` (deploy or update it with `python scripts/setup_agent.py trigger-notebook`). It queues the same OneLake outbox event as Activator; close any open case for that style × market first.
* **Demo resets** (admin only, synthetic data) run in Databricks notebook `05_demo_reset`. Open it in the workspace
  (`/Workspace/Shared/appareliq/notebooks`), pick `action`, set `mode` to `preview` (shows what would change, then rolls
  back) or `apply`, and fill the SQL widgets. Or start it from a workstation (add `--mode preview` to only look):
  * **Close a demo case:** `action = close_case`, `case_id = C-...` · `python scripts/setup_databricks.py --config config/deployment.local.json close-case --case-id <C-...>`.
    Closes the case, cancels proposed drafts and pending follow-ups, and logs `DEMO_RESET_CLOSE`. No ERP write-back.
  * **Free store stock again for store-to-store options:** `action = reset_store_transfers`, `style_id = HM9443`, `market_id = US` ·
    `python scripts/setup_databricks.py --config config/deployment.local.json reset-store-transfers --style-id HM9443 --market-id US`.
    Cancels unshipped agent transfer orders, releases their reservations and closes open cases for that style and market.
  A new T1 event for the same style and market then opens a fresh case.
* **Cost**: pause the Fabric capacity when idle; Azure SQL is serverless with 60-minute auto-pause; Databricks job
  clusters terminate after each run; the VNet data gateway sleeps after 30 minutes of inactivity.
* **Clean-up**: `az group delete -g <rg>` (the compute-region Databricks workspace lives in the same group; its managed group is
  removed with it), then delete the Fabric workspace, the VNet data gateway, the three app registrations
  (incl. `appareliq-sharepoint-ingest`), the `Policies` SharePoint files, and `~/.appareliq/msal_graph_cache.json`.

## Not yet deployed (requires approval)

Activator, the Foundry agent (step 9), Fabric IQ tool connection, Functions, App Insights and Teams publishing —
see the deployment guide.
