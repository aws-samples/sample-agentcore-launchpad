"""Mission-control overview: live tile metrics + control-plane service health."""

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.evaluation import online as online_eval
from app.evaluation.models import EvalRun
from app.models.ledger import ChatSession
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import policy_backend, policy_store, registry_backend, visibility
from app.services.agent_access import HiddenAgents, hidden_agent_ids
from app.services.visibility import Viewer, require_viewer
from app.services.workspace import WorkspaceContext

router = APIRouter(prefix="/api", tags=["overview"])

_TTL_SECONDS = 30.0
# The online-quality tile polls with the page; the aggregation is a ledger scan, cached
# per workspace so many tabs cost one scan per window (N12 E2-3 — same TTL the Global
# tile used for its Logs Insights query).
_QUALITY_TTL_SECONDS = 120.0
# Keyed by workspace: every value below is a fact about one account/region.
_cache: dict[str, dict[str, Any]] = {}
# Single-flight per (workspace, viewer): a slow aggregation for one workspace must not
# hold up another workspace's tile. Under workspace isolation a member's tile covers
# only their visible configs, so the quality value is cached per
# `Viewer.principal_key()` ("all" for every viewer who sees everything).
_quality_locks: dict[tuple[str, str], threading.Lock] = {}
_quality_locks_guard = threading.Lock()


def _slot(workspace_id: str) -> dict[str, Any]:
    return _cache.setdefault(
        workspace_id,
        {"assets_at": 0.0, "assets": None, "traces_at": 0.0, "traces": None,
         "quality": {}},
    )


def _registry_assets(workspace: WorkspaceContext) -> dict[str, int]:
    """Non-deprecated record counts per asset type (30s cache — backend-backed).
    Zero across the board while no registry backend is registered (N4)."""
    slot = _slot(workspace.id)
    if slot["assets"] is not None and time.monotonic() - slot["assets_at"] < _TTL_SECONDS:
        return slot["assets"]
    counts = {"agents": 0, "tools": 0, "skills": 0}
    backend = registry_backend.backend_or_none()
    if backend is None:
        return counts
    by_type = {"A2A": "agents", "MCP": "tools", "AGENT_SKILLS": "skills"}
    a2a_ids: list[str] = []
    try:
        for record in backend.list_records(workspace):
            if record.get("status") == "DEPRECATED":
                continue
            key = by_type.get(record.get("descriptorType", ""))
            if key:
                counts[key] += 1
            if key == "agents" and record.get("recordId"):
                a2a_ids.append(str(record["recordId"]))
    except Exception:
        # keep the last good value warm; on a cold cache return the default
        # WITHOUT caching it, so the next request retries immediately
        return slot["assets"] if slot["assets"] is not None else counts
    # the A2A record ids ride along so a member's count can drop hidden agents'
    slot.update(assets_at=time.monotonic(), assets=counts, a2a_ids=a2a_ids)
    return counts


def _viewer_assets(
    db: Session, workspace: WorkspaceContext, viewer: Viewer, assets: dict[str, int]
) -> dict[str, int]:
    """The cached counts as `viewer` sees them: under workspace isolation the A2A
    records of agents hidden from a member are not counted."""
    if not viewer.filters:
        return assets
    hidden = HiddenAgents(db, viewer, workspace.id).record_ids
    listed = set(_slot(workspace.id).get("a2a_ids") or [])
    drop = len(hidden & listed)
    return {**assets, "agents": max(0, assets.get("agents", 0) - drop)} if drop else assets


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


def _eval_summary(
    db: Session, workspace_id: str, viewer: Viewer = Viewer.ALL
) -> tuple[float | None, int]:
    """(mean score over every completed run's evaluator means, completed-run count) —
    the EVAL PASS RATE tile (N12 E2-3, baseline semantics). The pass rate and the run
    count are the viewer's (workspace isolation)."""
    runs = (
        db.query(EvalRun)
        .filter(
            EvalRun.workspace_id == workspace_id,
            EvalRun.status == "completed",
            visibility.clause(viewer, EvalRun.created_by),
        )
        .all()
    )
    scores = [
        s["score"]
        for run in runs
        for s in (run.scores or [])
        if isinstance(s.get("score"), int | float)
    ]
    return (round(sum(scores) / len(scores), 3) if scores else None), len(runs)


def _eval_detail(pass_rate: float | None, run_count: int) -> dict[str, Any] | None:
    """Detail of the evaluation health row (WB-001): ``{"runs": 15, "pass_rate": 0.85}``
    — structured, so the console renders it in the viewer's language with its own plural
    rules ("15 runs · 85 % pass" / "15 次运行 · 通过率 85 %"; r7-e dropped the English
    string the backend used to format). ``pass_rate`` is ``None`` while the runs carry no
    scores; the whole detail is ``None`` while nothing has completed, so the row keeps its
    "run your first evaluation" hint."""
    if run_count == 0:
        return None
    return {"runs": run_count, "pass_rate": pass_rate}


def _policy_status(db: Session, workspace_id: str) -> dict[str, Any]:
    """`services.policy` / `service_detail.policy` (plan §2.6). Off-feature builds keep
    the keys (the console reads them) with `false` / `""`."""
    if "policy" not in get_settings().features:
        return {"enabled": False, "detail": ""}
    return policy_store.overview_status(
        db, workspace_id, engine_registered=policy_backend.backend_or_none() is not None,
    )


@router.get("/overview")
def overview(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    resources = ws.context.resources
    assets = _viewer_assets(db, ws.context, viewer, _registry_assets(ws.context))

    day_ago = datetime.now(UTC) - timedelta(hours=24)
    sessions = db.query(ChatSession).filter(
        ChatSession.workspace_id == ws.id, ChatSession.last_at >= day_ago
    )
    hidden_agents = hidden_agent_ids(db, viewer, ws.id)
    if hidden_agents:  # the viewer's agents only (workspace isolation)
        sessions = sessions.filter(ChatSession.agent_id.not_in(hidden_agents))
    active_sessions = sessions.count()
    pass_rate, run_count = _eval_summary(db, ws.id, viewer)

    # Bootstrap rows (gateway/memory/registry/observability) come from `make bootstrap`;
    # the USAGE row `evaluation` (WB-001) counts what the operator has done — it lights up
    # with the first completed run (same ledger scan as the EVAL PASS RATE tile, so the
    # card can never contradict it). `policy` (D20, plan §2.6) is ledger-derived the same
    # way: lit iff the Cedar engine is registered, the workspace mode is not `off` and ≥ 1
    # policy is ACTIVE; its detail reads "<mode> · <n> active". An installed-but-empty
    # engine is a capability (`/api/capabilities.features`), not a service in use.
    policy = _policy_status(db, ws.id)
    services = {
        "gateway": bool(resources.get("gateway_id")),
        "memory": bool(resources.get("memory_id")),
        "registry": bool(resources.get("registry_id")),
        "policy": policy["enabled"],
        "evaluation": run_count > 0,
        "observability": _traces_active(ws.context),
    }
    detail = {
        "gateway": resources.get("gateway_id", ""),
        "memory": resources.get("memory_id", ""),
        "registry": (
            resources.get("registry_id", "")
            or resources.get("registry_unavailable_reason", "")
        ),
        "policy": policy["detail"],
        "evaluation": _eval_detail(pass_rate, run_count),
        "observability": "aws/spans" if services["observability"] else "",
    }
    return {
        "registry_assets": {**assets, "total": sum(assets.values())},
        "active_sessions": active_sessions,
        "eval_pass_rate": pass_rate,
        "eval_runs": run_count,
        "services": services,
        "service_detail": detail,
    }


@router.get("/overview/online-quality")
def overview_online_quality(
    force: bool = False,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """ONLINE QUALITY · 24h tile: polarity-normalised mean of every online evaluation
    score the workspace's agent-owned configs produced in the last 24 h. Served from a
    120 s per-workspace cache with single-flight (one ledger aggregation per window,
    however many tabs poll); a workspace with no agent config short-circuits. Under
    workspace isolation a member sees only their visible configs, cached under their
    own key."""
    key = viewer.principal_key()
    by_viewer = _slot(ws.id).setdefault("quality", {})

    def fresh() -> dict[str, Any] | None:
        hit = by_viewer.get(key)
        if hit is not None and not force and (
            time.monotonic() - hit[0] < _QUALITY_TTL_SECONDS
        ):
            return hit[1]
        return None

    if (hit := fresh()) is not None:
        return {**hit, "cached": True}
    with _quality_locks_guard:
        lock = _quality_locks.setdefault((ws.id, key), threading.Lock())
    with lock:
        if (hit := fresh()) is not None:
            return {**hit, "cached": True}
        value = online_eval.online_quality(db, ws.context, viewer=viewer)
        by_viewer[key] = (time.monotonic(), value)
    return {**value, "cached": False}
