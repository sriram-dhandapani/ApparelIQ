"""Microsoft Graph (app-only, via the 'appareliq-agent-graph' app registration whose federated credential trusts the
agent managed identity; RSC needs a real application as the Teams app's webApplicationInfo.id): create the case group
chat, install the agent app into it, add people.

Permissions (least privilege, granted by scripts/setup_agent.py grants):
  Chat.Create                                         POST /chats
  TeamsAppInstallation.ReadWriteAndConsentSelfForChat.All  POST /chats/{id}/installedApps (only this app, with RSC consent)
  User.Read.All                                       resolve UPN -> object id when a persona row lacks one
  RSC Chat.Manage.Chat (manifest, consented per chat) POST /chats/{id}/members, only in chats where the agent is installed
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from . import auth

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
RSC = [{"permissionValue": "Chat.Manage.Chat", "permissionType": "application"}]


class GraphError(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"Graph {status}: {text[:600]}")
        self.status = status


def call(method: str, path: str, ok=(200, 201, 202, 204), **kw) -> requests.Response:
    for attempt in range(6):
        r = requests.request(method, GRAPH + path, timeout=60,
                             headers={"Authorization": "Bearer " + auth.token(auth.GRAPH)}, **kw)
        if r.status_code in (429, 503, 504) and attempt < 5:
            time.sleep(int(r.headers.get("Retry-After", 3 * (attempt + 1))))
            continue
        if r.status_code not in ok:
            raise GraphError(r.status_code, r.text)
        return r
    raise GraphError(429, "throttled")


def _member(aad_id: str) -> dict:
    return {"@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": ["owner"],
            "user@odata.bind": f"{GRAPH}/users('{aad_id}')"}


def create_group_chat(member_ids: list[str], topic: str) -> str:
    ids = list(dict.fromkeys(i.lower() for i in member_ids if i))
    if len(ids) < 2:
        raise ValueError("a group chat needs at least two people")
    body = {"chatType": "group" if len(ids) > 2 else "oneOnOne", "topic": topic[:250] if len(ids) > 2 else None,
            "members": [_member(i) for i in ids]}
    if body["topic"] is None:
        body.pop("topic")
    return call("POST", "/chats", json=body).json()["id"]


def install_app(chat_id: str, teams_app_id: str) -> str:
    """Install the agent app into the chat (consenting its chat RSC). Returns 'rsc' or 'basic'."""
    bind = {"teamsApp@odata.bind": f"{GRAPH}/appCatalogs/teamsApps/{teams_app_id}"}
    try:
        call("POST", f"/chats/{chat_id}/installedApps",
             json={**bind, "consentedPermissionSet": {"resourceSpecificPermissions": RSC}})
        return "rsc"
    except GraphError as e:
        if e.status == 409:
            return "existing"
        log.warning("install with RSC consent failed (%s); installing without RSC", e)
    try:
        call("POST", f"/chats/{chat_id}/installedApps", json=bind)
    except GraphError as e:
        if e.status != 409:
            raise
    return "basic"


def add_member(chat_id: str, aad_id: str) -> bool:
    """HL-05 / CW-06: pull a person into an existing case chat with full history. False if not permitted."""
    body = {**_member(aad_id), "visibleHistoryStartDateTime": "0001-01-01T00:00:00Z"}
    try:
        call("POST", f"/chats/{chat_id}/members", json=body)
        return True
    except GraphError as e:
        if e.status == 409:
            return True
        log.warning("add member %s to %s failed: %s", aad_id, chat_id, e)
        return False


def resolve_user(upn: str) -> dict[str, Any]:
    return call("GET", f"/users/{requests.utils.quote(upn)}?$select=id,displayName,userPrincipalName").json()


def health() -> dict:
    tok = auth.token(auth.GRAPH)
    import base64
    import json

    claims = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "=="))
    roles = sorted(claims.get("roles", []))
    need = {"Chat.Create", "User.Read.All"}
    return {"ok": need <= set(roles) and any(r.startswith("TeamsAppInstallation") for r in roles), "detail": ", ".join(roles)}
