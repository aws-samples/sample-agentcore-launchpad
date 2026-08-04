"""CloudWatch-backed trace readiness for configuration experiments."""

import json
import threading
import time
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from sqlalchemy.orm import Session

from app.evaluation.models import EvalRun
from app.evaluation.service import resolve_telemetry
from app.optimization.service import discover_agent_tools
from app.services.observability import SPANS_SOURCE, run_insights_queries

DEFAULT_LOOKBACK_HOURS = 24
MIN_LOOKBACK_HOURS = 24
MAX_LOOKBACK_HOURS = 24 * 30
SPARSE_SESSION_THRESHOLD = 3
CACHE_TTL_SECONDS = 30.0

ReadinessState = Literal["missing", "sparse", "ready", "unavailable"]


class LatestRun(TypedDict):
    id: str
    status: str
    session_count: int
    created_at: str | None


class ReadinessResult(TypedDict):
    agent_id: str
    lookback_hours: int
    state: ReadinessState
    trace_count: int
    session_count: int
    latest_trace_at: str | None
    observed_tools: list[str]
    expected_tools: list[str]
    missing_tools: list[str]
    latest_run: LatestRun | None
    message: str | None


class _TelemetrySnapshot(TypedDict):
    state: ReadinessState
    trace_count: int
    session_count: int
    latest_trace_at: str | None
    observed_tools: list[str]
    expected_tools: list[str]
    missing_tools: list[str]
    message: str | None


_CACHE: dict[tuple[str, int], tuple[float, _TelemetrySnapshot]] = {}
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: dict[tuple[str, int], threading.Lock] = {}


def reset_cache() -> None:
    """Clear readiness cache state (used by tests and operational refreshes)."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _KEY_LOCKS.clear()


def _number(row: dict[str, str], key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _latest_trace_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1_000_000_000, UTC).isoformat()
    except (OverflowError, TypeError, ValueError):
        return None


def _service_filter(service_name: str) -> str:
    # JSON string encoding is also a valid Logs Insights string literal and
    # prevents a server-derived service name from changing the query shape.
    return f"resource.attributes.service.name = {json.dumps(service_name)}"


def _query_snapshot(agent: Any, lookback_hours: int) -> _TelemetrySnapshot:
    expected_tools = sorted(discover_agent_tools(agent.spec or {}))
    try:
        service_name, _ = resolve_telemetry(agent)
        service_filter = _service_filter(service_name)
        rows = run_insights_queries(
            {
                "summary": f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and ispresent(traceId)
| filter {service_filter}
| stats count_distinct(traceId) as trace_count,
        count_distinct(attributes.session.id) as session_count,
        max(startTimeUnixNano) as latest_trace_ns
""",
                "tools": f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano)
    and ispresent(attributes.gen_ai.tool.name)
| filter {service_filter}
| stats count(*) as calls by attributes.gen_ai.tool.name as tool
| sort calls desc
| limit 200
""",
            },
            lookback_hours,
        )
    except Exception:
        return {
            "state": "unavailable",
            "trace_count": 0,
            "session_count": 0,
            "latest_trace_at": None,
            "observed_tools": [],
            "expected_tools": expected_tools,
            "missing_tools": expected_tools,
            "message": "CloudWatch trace readiness could not be verified.",
        }

    summary = (rows.get("summary") or [{}])[0]
    trace_count = _number(summary, "trace_count")
    session_count = _number(summary, "session_count")
    observed_tools = sorted(
        {
            str(row["tool"])
            for row in rows.get("tools", [])
            if row.get("tool")
        }
    )
    missing_tools = sorted(set(expected_tools) - set(observed_tools))
    if trace_count == 0:
        state: ReadinessState = "missing"
    elif session_count < SPARSE_SESSION_THRESHOLD or missing_tools:
        state = "sparse"
    else:
        state = "ready"
    return {
        "state": state,
        "trace_count": trace_count,
        "session_count": session_count,
        "latest_trace_at": _latest_trace_at(summary.get("latest_trace_ns")),
        "observed_tools": observed_tools,
        "expected_tools": expected_tools,
        "missing_tools": missing_tools,
        "message": None,
    }


def _snapshot(agent: Any, lookback_hours: int, *, force: bool) -> _TelemetrySnapshot:
    cache_key = (agent.id, lookback_hours)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and not force and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        key_lock = _KEY_LOCKS.setdefault(cache_key, threading.Lock())

    with key_lock:
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and not force and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]
        value = _query_snapshot(agent, lookback_hours)
        with _CACHE_LOCK:
            _CACHE[cache_key] = (time.monotonic(), value)
        return value


def _latest_run(db: Session, agent_id: str) -> LatestRun | None:
    run = (
        db.query(EvalRun)
        .filter(EvalRun.agent_id == agent_id)
        .order_by(EvalRun.created_at.desc())
        .first()
    )
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "session_count": len(run.session_ids or []),
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def project_readiness(
    agent: Any,
    db: Session,
    *,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    force: bool = False,
) -> ReadinessResult:
    """Project telemetry evidence plus the newest local run for one Agent."""
    snapshot = _snapshot(agent, lookback_hours, force=force)
    return {
        "agent_id": agent.id,
        "lookback_hours": lookback_hours,
        **snapshot,
        "latest_run": _latest_run(db, agent.id),
    }
