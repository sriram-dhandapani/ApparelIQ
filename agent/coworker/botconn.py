"""Bot Framework channel <-> agent. Inbound: validate the channel JWT ourselves (no Bot SDK). Outbound: Bot Connector REST
with a managed-identity token (the bot is MSI-typed: msaAppId = the user-assigned identity's client id)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import jwt
import requests

from . import auth, settings

log = logging.getLogger(__name__)
BF_ISSUER = "https://api.botframework.com"
BF_OPENID = "https://login.botframework.com/v1/.well-known/openidconfiguration"
_jwks: dict[str, tuple[float, jwt.PyJWKClient]] = {}


class AuthError(Exception):
    pass


def _jwks_client(openid_url: str) -> jwt.PyJWKClient:
    hit = _jwks.get(openid_url)
    if hit and time.time() - hit[0] < 24 * 3600:
        return hit[1]
    meta = requests.get(openid_url, timeout=15).json()
    client = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True)
    _jwks[openid_url] = (time.time(), client)
    return client


def validate_inbound(auth_header: str | None, activity: dict, *, app_id: str | None = None, tenant_id: str | None = None,
                     key_resolver=None) -> dict:
    """Validate the Authorization header of a channel -> bot request. Returns claims or raises AuthError."""
    s = settings.get()
    app_id = app_id or s.bot_app_id
    tenant_id = tenant_id or s.tenant_id
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    tok = auth_header.split(" ", 1)[1]
    try:
        unverified = jwt.decode(tok, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise AuthError(f"malformed token: {e}") from e
    iss = unverified.get("iss", "")
    entra = {f"https://login.microsoftonline.com/{tenant_id}/v2.0": f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration",
             f"https://sts.windows.net/{tenant_id}/": f"https://login.microsoftonline.com/{tenant_id}/.well-known/openid-configuration"}
    if iss == BF_ISSUER:
        openid = BF_OPENID
    elif iss in entra:
        openid = entra[iss]
    else:
        raise AuthError(f"untrusted issuer {iss}")
    try:
        key = key_resolver(tok) if key_resolver else _jwks_client(openid).get_signing_key_from_jwt(tok).key
        claims = jwt.decode(tok, key, algorithms=["RS256"], audience=app_id, issuer=iss, leeway=300,
                            options={"require": ["exp", "iss", "aud"]})
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid token: {e}") from e
    svc = claims.get("serviceurl") or claims.get("serviceUrl")
    if iss == BF_ISSUER and svc and activity.get("serviceUrl") and svc.rstrip("/") != activity["serviceUrl"].rstrip("/"):
        raise AuthError("serviceUrl claim mismatch")
    return claims


# ---------------------------------------------------------------- outbound
def _url(service_url: str | None, path: str) -> str:
    base = (service_url or settings.get().service_url).rstrip("/") + "/"
    return base + path.lstrip("/")


def _call(method: str, url: str, body: dict | None = None, retries: int = 6) -> dict:
    for attempt in range(retries):
        # Compact JSON: Teams limits a message (card included) to ~28 KB.
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode() if body is not None else None
        r = requests.request(method, url, data=data, timeout=30,
                             headers={"Authorization": "Bearer " + auth.token(auth.BOT),
                                      "Content-Type": "application/json; charset=utf-8"})
        if r.status_code in (200, 201, 202):
            return r.json() if r.content else {}
        # 403/404 right after a Graph install: the bot is not yet a chat member; retry with backoff.
        if r.status_code in (403, 404, 429, 500, 502, 503) and attempt < retries - 1:
            time.sleep(min(30, 3 * (attempt + 1)))
            continue
        if r.status_code == 403 and "BotNotInConversationRoster" in r.text:
            raise NotInChat(f"Bot Connector {method} {url} -> 403 BotNotInConversationRoster")
        raise RuntimeError(f"Bot Connector {method} {url} -> {r.status_code}: {r.text[:500]}")
    return {}


class NotInChat(RuntimeError):
    """The app is not (yet) installed in the chat; the case's install retry (tick/_resume) catches up later."""


def mention(aad_id: str, name: str) -> tuple[str, dict]:
    text = f"<at>{name}</at>"
    return text, {"type": "mention", "text": text, "mentioned": {"id": aad_id, "name": name}}


def send(conversation_id: str, *, text: str | None = None, card: dict | None = None, mentions: list[dict] | None = None,
         service_url: str | None = None, reply_to: str | None = None) -> str:
    act: dict[str, Any] = {"type": "message", "textFormat": "markdown"}
    if text:
        act["text"] = text
    if card:
        act["attachments"] = [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
    if mentions:
        act["entities"] = mentions
    path = f"v3/conversations/{conversation_id}/activities" + (f"/{reply_to}" if reply_to else "")
    return _call("POST", _url(service_url, path), act).get("id", "")


def update_card(conversation_id: str, activity_id: str, card: dict, service_url: str | None = None) -> None:
    act = {"type": "message", "id": activity_id,
           "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
    _call("PUT", _url(service_url, f"v3/conversations/{conversation_id}/activities/{activity_id}"), act, retries=3)


def typing(conversation_id: str, service_url: str | None = None) -> None:
    try:
        _call("POST", _url(service_url, f"v3/conversations/{conversation_id}/activities"), {"type": "typing"}, retries=1)
    except Exception:  # noqa: BLE001 - cosmetic
        pass


def health() -> dict:
    t = auth.token(auth.BOT)
    return {"ok": bool(t), "detail": f"bot token acquired for {settings.get().bot_app_id}"}
