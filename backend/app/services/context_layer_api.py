"""COA REST client — the backend's reads of a member's COA context, as that member.

One API Gateway REST API fronts COA's control plane and data layer (``ApiEndpoint``
of ``<prefix>-<env>-api``). Every request carries the member's own COA access token
(``context_layer_tokens``), so COA's grants decide what comes back. Redirects are
never followed (a redirect would re-send the bearer elsewhere), and an error names
the HTTP status only — never the token or the response body.

``call_as_member`` owns the error contract (parent design §3): no consent ⇒
``context_layer.authorization_required`` with the URL; COA 401 ⇒ the broker drops
its token and the next fetch forces consent (``context_layer.reauthorize``); COA 403
⇒ ``context_layer.forbidden`` (no grant — do not retry); 5xx or timeout ⇒
``context_layer.upstream``.
"""

import logging
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.services import context_layer_tokens as tokens
from app.services.context_layer import ContextLayer
from app.services.workspace import WorkspaceContext

logger = logging.getLogger("launchpad.context_layer.api")

# COA's REST data-layer Lambda caps at 29 s; the control plane answers far sooner.
TIMEOUT_S = 30.0
MAX_PAGES = 20
PAGE_SIZE = 100


class CoaUnauthorized(Exception):
    """COA answered 401: the token is no longer accepted."""


def _transport() -> httpx.BaseTransport | None:
    """Seam for tests (an ``httpx.MockTransport``); None = the real network."""
    return None


def _get(layer: ContextLayer, token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    base = layer.api_endpoint.rstrip("/")
    if not base:
        raise AppError(
            "context_layer.upstream", "the connected COA deployment has no REST endpoint",
            status_code=502,
        )
    url = f"{base}/{path.lstrip('/')}"
    try:
        with httpx.Client(
            timeout=TIMEOUT_S, follow_redirects=False, transport=_transport()
        ) as client:
            response = client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise AppError(
            "context_layer.upstream", "COA did not answer in time", {"status": None},
            status_code=504,
        ) from exc
    except httpx.HTTPError as exc:
        raise AppError(
            "context_layer.upstream", f"COA is unreachable ({type(exc).__name__})",
            {"status": None}, status_code=502,
        ) from exc
    status = response.status_code
    if status == 401:
        raise CoaUnauthorized()
    if status == 403:
        raise AppError(
            "context_layer.forbidden",
            "COA refused this read: you have no grant for it in COA",
            {"status": 403},
            status_code=403,
        )
    if status >= 300:
        logger.warning("COA GET %s answered HTTP %s", path, status)
        raise AppError(
            "context_layer.upstream",
            f"COA answered HTTP {status}",
            {"status": status},
            status_code=502,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AppError(
            "context_layer.upstream", "COA answered with a body that is not JSON",
            {"status": status}, status_code=502,
        ) from exc


def call_as_member[T](
    db: Session,
    workspace: WorkspaceContext,
    username: str,
    call: Callable[[ContextLayer, str], T],
) -> T:
    """Run ``call(layer, token)`` with the member's COA token (see module docstring)."""
    grant = tokens.user_token(db, workspace, username)
    if isinstance(grant, tokens.AuthNeeded):
        raise AppError(
            "context_layer.authorization_required",
            "authorize Launchpad to read COA as you first",
            {"authorization_url": grant.url, "connection": grant.layer.connection},
            status_code=409,
        )
    try:
        return call(grant.layer, grant.value)
    except CoaUnauthorized:
        tokens.invalidate(workspace.id, username)
        # 409, not 401: the console treats any 401 as "your Launchpad session ended"
        raise AppError(
            "context_layer.reauthorize",
            "COA no longer accepts your authorization — authorize again",
            {"connection": grant.layer.connection},
            status_code=409,
        ) from None


def _namespace(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "namespace_id": str(item.get("namespaceId") or ""),
        "name": str(item.get("name") or ""),
        "display_name": item.get("displayName") or None,
        "status": item.get("status"),
        "source_count": item.get("sourceCount"),
        "created_at": item.get("createdAt"),
        "updated_at": item.get("updatedAt"),
    }


def list_namespaces(layer: ContextLayer, token: str) -> list[dict[str, Any]]:
    """``GET /namespaces`` across pages: the namespaces COA lets this member see."""
    items: list[dict[str, Any]] = []
    next_token: str | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"maxResults": PAGE_SIZE}
        if next_token:
            params["nextToken"] = next_token
        body = _get(layer, token, "namespaces", params) or {}
        items.extend(_namespace(ns) for ns in body.get("namespaces") or [] if isinstance(ns, dict))
        next_token = body.get("nextToken") or None
        if not next_token:
            break
    return [ns for ns in items if ns["namespace_id"]]
