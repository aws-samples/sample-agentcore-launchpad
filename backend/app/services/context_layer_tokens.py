"""The COA token broker: a member's COA access token, from the token vault only.

**The custody exception (D1, docs/identity.md §7.7).** Every other ``as_user``
token is fetched inside the agent's own runtime and Launchpad never sees it. For
the context layer the *backend* holds one: the console workload identity
``launchpad-console-<workspace>`` fetches the member's COA token so the V2 module
can call COA's REST API and so a Harness agent's turn can carry it in an
``InvokeHarness`` tools override. The rules this module enforces:

1. **Source.** Only ``GetResourceOauth2Token(oauth2Flow=USER_FEDERATION)``,
   presenting ``GetWorkloadAccessTokenForUserId(console workload, userId=<console
   username>)`` — the same user id Chat sends as ``runtimeUserId``.
2. **Sinks.** Process memory and outbound requests to the COA deployment (its REST
   API, Serve runtime, and MCP runtime via an InvokeHarness override). Never the
   ledger, a log line, an SSE event, an API response, an exception or a trace:
   ``Token`` hides its value from ``repr`` and errors name status codes only.
3. **Cache.** In-process, keyed (workspace id, username), held until ``exp - 120 s``.
   A hit is honoured only while no revocation is in force.
4. **Revocation.** ``user_token_revocations`` (user, Connection): the next fetch
   sends ``forceAuthentication=true`` until the console grant (``agent_id=""``) is
   re-authorized — the existing semantics, shared with every agent on the
   Connection.
5. **Consent.** No token ⇒ ``record_pending(agent_id="", tool="context_layer")``
   and the authorization URL; ``/auth/return`` → ``POST /api/identity/oauth/complete``
   binds it with ``CompleteResourceTokenAuth(userId)``.
"""

import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, aws_error_code
from app.services import context_layer, oauth_sessions
from app.services.agentcore.client import data_client
from app.services.workspace import WorkspaceContext
from app.templates import context_layer_support as support

logger = logging.getLogger("launchpad.context_layer.tokens")

# A cached token is used until this many seconds before it expires.
EXPIRY_MARGIN_S = 120


@dataclass(frozen=True)
class Token:
    """A member's COA access token. ``repr`` never shows the value."""

    value: str = field(repr=False)
    expires_at: float
    layer: context_layer.ContextLayer = field(repr=False, compare=False)


@dataclass(frozen=True)
class AuthNeeded:
    """The member must consent at COA first. ``url`` is the single-use
    authorization URL (sent to that member only); the session uri stays server-side."""

    url: str = field(repr=False)
    layer: context_layer.ContextLayer = field(repr=False, compare=False)


_CACHE: dict[tuple[str, str], Token] = {}
# (workspace, user) → when COA last rejected that member's token. Every fetch forces
# consent until the console grant is authorized after that moment: the vault still
# holds the rejected token, and a plain fetch would hand it straight back.
_FORCE: dict[tuple[str, str], datetime] = {}
_LOCK = threading.Lock()


def forget(workspace_id: str, username: str | None = None) -> None:
    """Drop cached tokens: one member's, or the whole workspace's (connect/disconnect)."""
    with _LOCK:
        for key in [k for k in _CACHE if k[0] == workspace_id and username in (None, k[1])]:
            _CACHE.pop(key, None)
        if username is None:
            for key in [k for k in _FORCE if k[0] == workspace_id]:
                _FORCE.pop(key, None)


def invalidate(workspace_id: str, username: str) -> None:
    """COA rejected the member's token (401): drop it and force re-auth until the
    member consents again."""
    with _LOCK:
        _CACHE.pop((workspace_id, username), None)
        _FORCE[(workspace_id, username)] = datetime.now(UTC)


def _expiry(token: str) -> float | None:
    """``exp`` of a JWT access token, decoded WITHOUT verification — it only bounds
    the cache; COA verifies the token on every call."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = claims.get("exp")
        return float(exp) if isinstance(exp, int | float) else None
    except (IndexError, ValueError, TypeError):
        return None


def _custom_state(agent_id: str, session_id: str) -> str:
    """What ``/auth/return`` reads back to send the member home: the Chat
    conversation that asked, else the Context Layer page."""
    if agent_id:
        state = {"agent_id": agent_id, "tool": support.GRANT_TOOL, "session_id": session_id}
    else:
        state = {"kind": support.GRANT_TOOL, "tool": support.GRANT_TOOL}
    return json.dumps(state)


def _identity_failure(step: str, exc: Exception) -> AppError:
    code = aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
    logger.warning("context layer token: %s failed (%s)", step, code)
    return AppError(
        "context_layer.identity_failed",
        f"AgentCore Identity could not provide your COA token ({step}: {code})",
        {"step": step, "code": code},
        status_code=502,
    )


def user_token(
    db: Session,
    workspace: WorkspaceContext,
    username: str | None,
    *,
    console_user: bool = True,
    force: bool = False,
    agent_id: str = "",
    session_id: str = "",
    now: Callable[[], float] = time.time,
) -> Token | AuthNeeded:
    """The member's COA token, or the consent they owe first.

    ``console_user=False`` (the public /v1 API) and a call with no username are
    refused before any vault call: such a caller names nobody who could consent
    (``identity.as_user_requires_console``). ``agent_id`` / ``session_id`` only
    shape where ``/auth/return`` sends the member after a Chat-started consent.
    """
    layer = context_layer.require(db, workspace.id)
    if not console_user or not username:
        raise oauth_sessions.as_user_requires_console(
            provider=layer.connection, tool=support.GRANT_TOOL, agent_id=agent_id
        )
    key = (workspace.id, username)
    revoked = bool(oauth_sessions.pending_revocations(
        db, workspace.id, user_id=username, agent_id="", providers=[layer.connection],
    ))
    with _LOCK:
        forced_at = _FORCE.get(key)
        hit = _CACHE.get(key)
    forced = forced_at is not None and not oauth_sessions.authorized_since(
        db, workspace.id, user_id=username, provider=layer.connection, agent_id="",
        since=forced_at,
    )
    if forced_at is not None and not forced:
        with _LOCK:
            _FORCE.pop(key, None)
    if (
        hit is not None
        and not (revoked or forced or force)
        and hit.layer.id == layer.id
        and hit.expires_at - EXPIRY_MARGIN_S > now()
    ):
        return hit
    data = data_client(workspace)
    try:
        workload_token = data.get_workload_access_token_for_user_id(
            workloadName=layer.console_workload, userId=username
        )["workloadAccessToken"]
    except (ClientError, BotoCoreError, KeyError) as exc:
        raise _identity_failure("workload token", exc) from exc
    request: dict[str, Any] = {
        "workloadIdentityToken": workload_token,
        "resourceCredentialProviderName": layer.connection,
        "scopes": list(support.SCOPES),
        "oauth2Flow": "USER_FEDERATION",
        "resourceOauth2ReturnUrl": get_settings().resolved_oauth_return_url(),
        "customState": _custom_state(agent_id, session_id),
    }
    if revoked or forced or force:
        request["forceAuthentication"] = True
    try:
        response = data.get_resource_oauth2_token(**request)
    except (ClientError, BotoCoreError) as exc:
        raise _identity_failure("resource token", exc) from exc
    access = response.get("accessToken")
    if access:
        expires = _expiry(access)
        token = Token(value=access, expires_at=expires or 0.0, layer=layer)
        with _LOCK:
            if expires is not None:
                _CACHE[key] = token
            else:
                _CACHE.pop(key, None)
        return token
    url = str(response.get("authorizationUrl") or "")
    session_uri = str(response.get("sessionUri") or "")
    if not url:
        raise AppError(
            "context_layer.identity_failed",
            "AgentCore Identity returned neither a COA token nor an authorization URL",
            {"step": "resource token"},
            status_code=502,
        )
    with _LOCK:
        _CACHE.pop(key, None)
    oauth_sessions.record_pending(
        db,
        workspace.id,
        session_uri=session_uri,
        provider=layer.connection,
        user_id=username,
        agent_id="",
        tool=support.GRANT_TOOL,
        scopes=support.SCOPES,
    )
    return AuthNeeded(url=url, layer=layer)


def turn_token(
    workspace: WorkspaceContext,
    username: str | None,
    *,
    console_user: bool = True,
    agent_id: str = "",
    session_id: str = "",
) -> Token | AuthNeeded:
    """``user_token`` through its own short session — the invoke hot path."""
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        return user_token(
            db, workspace, username, console_user=console_user,
            agent_id=agent_id, session_id=session_id,
        )
    finally:
        db.close()


def auth_notice(grant: AuthNeeded, agent_id: str) -> dict[str, Any]:
    """The ``auth_required`` event for a Harness turn, in the shape every other
    as_user ask has, so the Chat auth card and ``/auth/return`` work unchanged."""
    return {
        "provider": grant.layer.connection,
        "tool": support.GRANT_TOOL,
        "url": grant.url,
        "scopes": list(support.SCOPES),
        "agent_id": agent_id,
    }
