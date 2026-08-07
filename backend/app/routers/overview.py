"""Mission-control overview: live tile metrics + control-plane service health."""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.evaluation.models import EvalRun
from app.models.ledger import ChatSession
from app.services.registry_console import console_list

router = APIRouter(prefix="/api", tags=["overview"])

_TTL_SECONDS = 30.0
_cache: dict[str, Any] = {"assets_at": 0.0, "assets": None, "traces_at": 0.0, "traces": None}


def _registry_assets() -> dict[str, int]:
    """Non-deprecated record counts per asset type (30s cache — AWS-backed)."""
    if _cache["assets"] is not None and time.monotonic() - _cache["assets_at"] < _TTL_SECONDS:
        return _cache["assets"]
    counts = {"agents": 0, "tools": 0, "skills": 0}
    by_type = {"A2A": "agents", "MCP": "tools", "AGENT_SKILLS": "skills"}
    try:
        for record in console_list():
            if record.get("status") == "DEPRECATED":
                continue
            key = by_type.get(record.get("descriptorType", ""))
            if key:
                counts[key] += 1
    except Exception:
        # keep the last good value warm; on a cold cache return the default
        # WITHOUT caching it, so the next request retries immediately
        return _cache["assets"] if _cache["assets"] is not None else counts
    _cache.update(assets_at=time.monotonic(), assets=counts)
    return counts


def _traces_active() -> bool:
    """Transaction Search destination check (30s cache — AWS-backed)."""
    if _cache["traces"] is not None and time.monotonic() - _cache["traces_at"] < _TTL_SECONDS:
        return _cache["traces"]
    try:
        dest = boto3.client("xray", region_name=get_settings().region)
        response = dest.get_trace_segment_destination()
        active = response.get("Destination") == "CloudWatchLogs" and response.get(
            "Status"
        ) in ("ACTIVE", "PENDING")
    except Exception:
        # cold cache: report False but don't cache it — retry next request
        return _cache["traces"] if _cache["traces"] is not None else False
    _cache.update(traces_at=time.monotonic(), traces=active)
    return active


@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    resources = get_settings().resources
    assets = _registry_assets()

    day_ago = datetime.now(UTC) - timedelta(hours=24)
    active_sessions = (
        db.query(ChatSession).filter(ChatSession.last_at >= day_ago).count()
    )

    runs = db.query(EvalRun).filter(EvalRun.status == "completed").all()
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
        "policy": bool(resources.get("policy_engine_id")),
        "evaluation": len(runs) > 0,
        "observability": _traces_active(),
    }
    detail = {
        "gateway": resources.get("gateway_id", ""),
        "memory": resources.get("memory_id", ""),
        "registry": (
            resources.get("registry_id", "")
            or resources.get("registry_unavailable_reason", "")
        ),
        "policy": resources.get("policy_engine_id", ""),
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
