"""Mission-control overview: live tile metrics + control-plane service health."""

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.evaluation import online as online_eval
from app.evaluation.models import EvalRun
from app.models.ledger import ChatSession
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services.agentcore.client import control_client
from app.services.governance import attached_policy_engine_id
from app.services.registry_console import console_list
from app.services.workspace import WorkspaceContext

router = APIRouter(prefix="/api", tags=["overview"])

_TTL_SECONDS = 30.0
# Logs Insights is billed per scan; the online-quality tile polls with the page.
_QUALITY_TTL_SECONDS = 120.0
# Keyed by workspace: every value below is a fact about one account/region.
_cache: dict[str, dict[str, Any]] = {}
# Single-flight per workspace: a slow Logs Insights poll for one account must not
# hold up another workspace's tile.
_quality_locks: dict[str, threading.Lock] = {}
_quality_locks_guard = threading.Lock()


def _slot(workspace_id: str) -> dict[str, Any]:
    return _cache.setdefault(
        workspace_id,
        {"assets_at": 0.0, "assets": None, "traces_at": 0.0, "traces": None,
         "quality_at": 0.0, "quality": None},
    )


def _registry_assets(workspace: WorkspaceContext) -> dict[str, int]:
    """Non-deprecated record counts per asset type (30s cache — AWS-backed)."""
    slot = _slot(workspace.id)
    if slot["assets"] is not None and time.monotonic() - slot["assets_at"] < _TTL_SECONDS:
        return slot["assets"]
    counts = {"agents": 0, "tools": 0, "skills": 0}
    by_type = {"A2A": "agents", "MCP": "tools", "AGENT_SKILLS": "skills"}
    try:
        for record in console_list(workspace):
            if record.get("status") == "DEPRECATED":
                continue
            key = by_type.get(record.get("descriptorType", ""))
            if key:
                counts[key] += 1
    except Exception:
        # keep the last good value warm; on a cold cache return the default
        # WITHOUT caching it, so the next request retries immediately
        return slot["assets"] if slot["assets"] is not None else counts
    slot.update(assets_at=time.monotonic(), assets=counts)
    return counts


def _traces_active(workspace: WorkspaceContext) -> bool:
    """Transaction Search destination check (30s cache — AWS-backed)."""
    slot = _slot(workspace.id)
    if slot["traces"] is not None and time.monotonic() - slot["traces_at"] < _TTL_SECONDS:
        return slot["traces"]
    try:
        dest = workspace.client("xray")
        response = dest.get_trace_segment_destination()
        active = response.get("Destination") == "CloudWatchLogs" and response.get(
            "Status"
        ) in ("ACTIVE", "PENDING")
    except Exception:
        # cold cache: report False but don't cache it — retry next request
        return slot["traces"] if slot["traces"] is not None else False
    slot.update(traces_at=time.monotonic(), traces=active)
    return active


@router.get("/overview")
def overview(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    resources = ws.context.resources
    assets = _registry_assets(ws.context)
    policy_engine_id = attached_policy_engine_id(control_client(ws.context), ws.context)

    day_ago = datetime.now(UTC) - timedelta(hours=24)
    active_sessions = (
        db.query(ChatSession)
        .filter(ChatSession.workspace_id == ws.id, ChatSession.last_at >= day_ago)
        .count()
    )

    runs = (
        db.query(EvalRun)
        .filter(EvalRun.workspace_id == ws.id, EvalRun.status == "completed")
        .all()
    )
    scores = [
        s["score"]
        for run in runs
        for s in (run.scores or [])
        if isinstance(s.get("score"), (int, float))
    ]
    pass_rate = round(sum(scores) / len(scores), 3) if scores else None

    services = {
        "gateway": bool(resources.get("gateway_id")),
        "memory": bool(resources.get("memory_id")),
        "registry": bool(resources.get("registry_id")),
        "policy": bool(policy_engine_id),
        "evaluation": len(runs) > 0,
        "observability": _traces_active(ws.context),
    }
    detail = {
        "gateway": resources.get("gateway_id", ""),
        "memory": resources.get("memory_id", ""),
        "registry": (
            resources.get("registry_id", "")
            or resources.get("registry_unavailable_reason", "")
        ),
        "policy": policy_engine_id,
        "evaluation": f"{len(runs)} runs" if runs else "",
        "observability": "aws/spans" if services["observability"] else "",
    }
    return {
        "registry_assets": {**assets, "total": sum(assets.values())},
        "active_sessions": active_sessions,
        "eval_pass_rate": pass_rate,
        "eval_runs": len(runs),
        "services": services,
        "service_detail": detail,
    }


@router.get("/overview/online-quality")
def overview_online_quality(
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """ONLINE QUALITY · 24h tile: polarity-normalised mean of every online
    evaluation score the workspace's agent-owned configs produced in the last
    24 h. Served from a 120 s per-workspace cache with single-flight (one Logs
    Insights scan per window, however many tabs poll); a workspace with no agent
    config short-circuits without any AWS call."""
    slot = _slot(ws.id)

    def fresh() -> dict[str, Any] | None:
        if slot["quality"] is not None and not force and (
            time.monotonic() - slot["quality_at"] < _QUALITY_TTL_SECONDS
        ):
            return slot["quality"]
        return None

    if (hit := fresh()) is not None:
        return {**hit, "cached": True}
    with _quality_locks_guard:
        lock = _quality_locks.setdefault(ws.id, threading.Lock())
    with lock:
        if (hit := fresh()) is not None:
            return {**hit, "cached": True}
        value = online_eval.online_quality(db, ws.context)
        slot.update(quality_at=time.monotonic(), quality=value)
    return {**value, "cached": False}
