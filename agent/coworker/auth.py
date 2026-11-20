"""Keyless outbound auth: the user-assigned managed identity in Azure, the developer's az login locally.

Microsoft Graph is the exception: Teams resource-specific consent (RSC) needs a real Entra application as the Teams
app's webApplicationInfo.id, which a managed identity cannot be. So Graph calls use the 'appareliq-agent-graph' app
registration, whose federated identity credential trusts this managed identity (no secret or certificate).
"""
from __future__ import annotations

import os
import threading
import time

from azure.core.credentials import AccessToken
from azure.identity import AzureCliCredential, ClientAssertionCredential, ManagedIdentityCredential

from . import settings

SQL = "https://database.windows.net/.default"
KUSTO = "https://kusto.kusto.windows.net/.default"
FABRIC = "https://api.fabric.microsoft.com/.default"
GRAPH = "https://graph.microsoft.com/.default"
COGNITIVE = "https://cognitiveservices.azure.com/.default"
SEARCH = "https://search.azure.com/.default"
BOT = "https://api.botframework.com/.default"
STORAGE = "https://storage.azure.com/.default"

_lock = threading.Lock()
_cache: dict[str, AccessToken] = {}
TOKEN_EXCHANGE = "api://AzureADTokenExchange/.default"

_cred = None
_graph_cred = None


def _in_azure() -> bool:
    return bool(os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"))


def credential():
    global _cred
    if _cred is None:
        cid = settings.get().client_id
        _cred = ManagedIdentityCredential(client_id=cid) if _in_azure() else AzureCliCredential()
    return _cred


def graph_credential():
    """Graph app registration via federated credential (managed identity token as the client assertion)."""
    global _graph_cred
    if _graph_cred is None:
        s = settings.get()
        if s.graph_client_id and _in_azure():
            _graph_cred = ClientAssertionCredential(s.tenant_id, s.graph_client_id,
                                                    lambda: credential().get_token(TOKEN_EXCHANGE).token)
        else:
            _graph_cred = credential()
    return _graph_cred


def token(scope: str) -> str:
    with _lock:
        t = _cache.get(scope)
        if t is None or t.expires_on - time.time() < 300:
            t = (graph_credential() if scope == GRAPH else credential()).get_token(scope)
            _cache[scope] = t
        return t.token
