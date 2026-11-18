"""Read-only Agent Insights, scoped to one visible agent's production service."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import current_project, project_context
from app.evaluation.models import OnlineEvalConfig
from app.evaluation.service import resolve_telemetry, telemetry_endpoint
from app.models.ledger import Agent
from app.routers.auth import _signing_key, enabled
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access, visibility
from app.services.agentcore import insights
from app.services.agentcore.client import api_data_client, control_client
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/eval", tags=["evaluation-insights"])

_CURSOR_SECONDS = 900
# A password-less local console cannot safely sign with the known empty-password key.
_LOCAL_CURSOR_KEY = secrets.token_bytes(32)


def _not_found() -> NotFoundError:
    return NotFoundError("insight.not_found", "The finding or its source is not available.")


@router.get("/insight-store")
def get_insight_store(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, str]:
    return {"state": insights.get_store_state(control_client(ws.context))}


def _base(resource_id: str, method: str) -> str:
    # AgentCore IDs are <runtime or harness name>-<generated suffix>.
    name = resource_id.rsplit("-", 1)[0]
    return f"harness_{name}" if method == "harness" else name


def _application(
    db: Session, ws: WorkspaceScope, viewer: Viewer, agent_id: str,
) -> str:
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "Agent not found.")
    # Platform presets can serve several projects under the same service name.
    # They are visible as agents, but we cannot isolate their shared findings.
    if (agent.project_id is None or agent.project_id != current_project()
            or agent.status != "active" or not agent.resource_id):
        raise _not_found()

    service, _ = resolve_telemetry(agent, ws.context)
    endpoint = telemetry_endpoint(agent)
    base = _base(agent.resource_id, agent.method)
    if not base or service != f"{base}.{endpoint}":
        # No fuzzy name matching for account-level aggregates.
        raise _not_found()
    # Each (account, Region) has exactly one workspace (database constraint).
    # Compare across projects and include deleted agents: an insight can survive the
    # old runtime, and recreating the name doesn't convey access to its samples.
    with project_context(None):
        other_sources = db.execute(
            select(Agent.resource_id, Agent.method).where(
                Agent.workspace_id == ws.id, Agent.id != agent_id,
                Agent.resource_id.isnot(None),
                (Agent.resource_id.startswith(f"{base}-", autoescape=True))
                | (Agent.resource_id.startswith(f"{base.removeprefix('harness_')}-",
                                               autoescape=True)),
            )
        ).all()
        configs = db.scalars(
            select(OnlineEvalConfig).where(
                OnlineEvalConfig.workspace_id == ws.id, OnlineEvalConfig.service_name == service,
            )
        ).all()
    if any(_base(resource, method) == base for resource, method in other_sources):
        raise _not_found()
    if any(
        config.agent_id != agent_id
        or config.project_id != agent.project_id
        or not visibility.sees(viewer, db, config.created_by)
        for config in configs
    ):
        # AWS aggregates at service level; an individual configuration cannot be excluded.
        raise _not_found()
    return service


def _cursor_mac(payload: bytes) -> bytes:
    key = _signing_key() if enabled() else _LOCAL_CURSOR_KEY
    return hmac.digest(key, b"agent-insight-cursor/v1:" + payload, "sha256")


def _scope(ws: WorkspaceScope, viewer: Viewer, agent_id: str, service: str) -> list[str | None]:
    return [ws.id, ws.context.account_id, ws.context.region, viewer.project_id, current_project(),
            viewer.user_id, viewer.principal_key(), agent_id, service, "ACTIVE"]


def _encode_cursor(scope: list[str | None], token: str) -> str:
    payload = json.dumps([scope, token, int(time.time()) + _CURSOR_SECONDS],
                         separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload + _cursor_mac(payload)).decode()


def _decode_cursor(scope: list[str | None], cursor: str) -> str:
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        payload, signature = raw[:-32], raw[-32:]
        if hmac.compare_digest(signature, _cursor_mac(payload)):
            signed_scope, token, expires = json.loads(payload)
            if (signed_scope == scope and expires >= time.time()
                    and isinstance(token, str) and token):
                return token
    except (ValueError, TypeError, binascii.Error):
        pass
    raise AppError("insight.invalid_cursor", "Refresh the findings before paging.", status_code=400)


@router.get("/agents/{agent_id}/findings")
def list_agent_findings(
    agent_id: str,
    cursor: str | None = Query(default=None, max_length=8192),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    service = _application(db, ws, viewer, agent_id)
    scope = _scope(ws, viewer, agent_id, service)
    token = _decode_cursor(scope, cursor) if cursor else None
    items, token = insights.list_active_findings(
        api_data_client(ws.context), service, token=token,
    )
    return {"application_name": service, "items": items,
            "next_cursor": _encode_cursor(scope, token) if token else None}


@router.get("/agents/{agent_id}/findings/{insight_id}")
def get_agent_finding(
    agent_id: str,
    insight_id: str = Path(min_length=1, max_length=64, pattern=r"^[a-z0-9]+$"),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    service = _application(db, ws, viewer, agent_id)
    return insights.get_active_finding(api_data_client(ws.context), service, insight_id)
