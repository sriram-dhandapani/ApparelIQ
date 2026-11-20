"""Environment settings (app settings in Azure, local.settings.json locally). Ids and endpoints only, no secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    client_id: str
    tenant_id: str
    sql_server: str
    sql_database: str
    workspace_id: str
    kql_query_uri: str
    kql_database: str
    data_agent_mcp_url: str
    openai_endpoint: str
    chat_deployment: str
    search_endpoint: str
    knowledge_base: str
    knowledge_source: str
    teams_app_id: str
    graph_client_id: str
    users_group_id: str
    fallback_upn: str
    materiality_gm_usd: float
    approval_sla_hours: float
    queue_name: str
    service_url: str
    data_label: str
    outbox_lakehouse_id: str = ""
    outbox_dir: str = "Files/agent_outbox"

    @property
    def bot_app_id(self) -> str:
        # The user-assigned managed identity is also the Azure Bot identity (msaAppType = UserAssignedMSI).
        return _env("MicrosoftAppId") or self.client_id


@lru_cache(maxsize=1)
def get() -> Settings:
    return Settings(
        client_id=_env("AZURE_CLIENT_ID"),
        tenant_id=_env("MicrosoftAppTenantId") or _env("AZURE_TENANT_ID"),
        sql_server=_env("SQL_SERVER"),
        sql_database=_env("SQL_DATABASE"),
        workspace_id=_env("FABRIC_WORKSPACE_ID"),
        kql_query_uri=_env("KQL_QUERY_URI").rstrip("/"),
        kql_database=_env("KQL_DATABASE", "kdb_appareliq"),
        data_agent_mcp_url=_env("DATA_AGENT_MCP_URL"),
        openai_endpoint=_env("FOUNDRY_OPENAI_ENDPOINT").rstrip("/"),
        chat_deployment=_env("FOUNDRY_CHAT_DEPLOYMENT", "gpt-4.1"),
        search_endpoint=_env("SEARCH_ENDPOINT").rstrip("/"),
        knowledge_base=_env("KNOWLEDGE_BASE", "kb-kinetiq-policies"),
        knowledge_source=_env("KNOWLEDGE_SOURCE", "ks-kinetiq-policies"),
        teams_app_id=_env("TEAMS_APP_ID"),
        graph_client_id=_env("GRAPH_APP_CLIENT_ID"),
        users_group_id=_env("AGENT_USERS_GROUP_ID"),
        fallback_upn=_env("FALLBACK_UPN"),
        materiality_gm_usd=float(_env("MATERIALITY_GM_USD", "50000")),
        approval_sla_hours=float(_env("APPROVAL_SLA_HOURS", "4")),
        queue_name=_env("AGENT_QUEUE_NAME", "agent-events"),
        service_url=_env("TEAMS_SERVICE_URL", "https://smba.trafficmanager.net/teams/"),
        data_label=_env("DATA_LABEL", "SYNTHETIC"),
        outbox_lakehouse_id=_env("OUTBOX_LAKEHOUSE_ID"),
        outbox_dir=_env("OUTBOX_DIR", "Files/agent_outbox").strip("/"),
    )
