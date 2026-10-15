"""Budget reconciliation: measured span cost and AgentCore fees per person (child 3).

Children 1–2 charge what the platform sees at call time (exact tokens where a call
reports usage, a flagged estimate where it does not). This module, run every ~10
minutes by ``services/budget_scheduler.py``, corrects and completes that from
AWS telemetry — **only for the sessions in this box's own ``usage_sessions``**, so
prod and prod2 sharing one AWS account never charge each other's sessions:

(a) **Measured tokens.** Span tokens per session (the cost report's
    ``_IS_LLM_FIELDS`` + the Strands-on-Mantle wrapper fallback) for a session the
    meter could not see in-band (Strands zip, BYOC, Studio, A2A, gateway traffic).
    The measured ``tokens`` row supersedes the session's ``turn_estimate`` /
    ``job_estimate`` rows. A session with in-band exact rows (Harness, Claude SDK)
    is never re-measured, so nothing is counted twice; an unpriced measured row
    supersedes nothing (the estimate keeps the payer charged).
(b) **Exact per-session fees** counted on the session's spans × the AgentCore
    rates (``services/rates.py``): memory events (``CreateEvent``) and long-term
    retrievals (``RetrieveMemoryRecords``), Gateway calls (the Gateway's own SERVER
    spans, which carry no ``session.id``, joined to the session by ``traceId``) and
    Policy authorizations, Web Search (``…___WebSearch``), KB queries
    (``…___Retrieve`` / ``…___AgenticRetrieveStream``) and agent-started Code
    Interpreter sessions (estimated per tool session).
(c) **Runtime compute** (estimated, decisions 7–8): per microVM lifetime, from the
    first span to the last plus the runtime's idle timeout (``GetAgentRuntime``
    ``lifecycleConfiguration``; V2 reclaims idle memory after 120 s) capped at its
    ``maxLifetime``, × the runtime's calibrated GB and vCPU factors × the
    platform-version rates. **One ``runtime_compute`` row per microVM lifetime of a
    session, updated in place** while the session stays alive — the single
    in-place update the append-only ledger allows (a correction is otherwise a new
    row plus ``superseded_by``). Activity after the idle tail expired, past the
    max lifetime or in a new month starts a new row.
(d) **Evaluations.** A finished batch run's evaluations are counted from its row
    (``scores[].count`` = ``totalEvaluated`` = the result records, live probe), or
    from its result records when the row has no counts; online configs' result
    records are counted per minute. Built-in = count × the calibrated tokens per
    evaluation × the Tier2 (batch) or Tier1 (online) rate; custom = count × the
    per-evaluation fee + those tokens at the platform judge model — both flagged
    estimated. A run's rows supersede child 2's judge estimate; online rows are
    charged to the config's creator (an experiment / canary config to the caller of
    the action that created it).

**Windows.** A span is attributed to the minute it *ended* (``bin(1m)`` of
``@timestamp``, which is the span end time; ingestion lags it by ~1–5 s). Each
session has its own window ``[reconciled_until, E)`` with ``E = now − 2 min``
floored to the minute, so consecutive windows never overlap; a session's rows and
its new ``reconciled_until`` commit in one transaction, and a failed window
(a query error, a write error) leaves the watermark alone so the next tick reads
it again.

The tick never raises; failures are logged and kept in the status
(``platform_settings`` key ``budget_reconcile_state``), which the admin route reads
through ``budget_scheduler.status()``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.billing_policy import SOURCE_CANARY, SOURCE_EVAL, SOURCE_EXPERIMENT
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun, OnlineEvalConfig
from app.models.budgets import UsageEvent, UsageSession
from app.models.ledger import Agent, Workspace
from app.models.platform_settings import PlatformSetting
from app.optimization.models import Experiment, RuntimeCanary
from app.services import budgets, rates
from app.services import usage_meter as um
from app.services.costs import _TOKEN_SUMS, _WRAPPER_FIELDS
from app.services.observability import (
    _IS_LLM_FIELDS,
    SESSION_ID_RE,
    SPANS_SOURCE,
    TRACE_ID_RE,
    logs_client,
    run_insights_queries,
)
from app.services.workspace import WorkspaceContext, workspace_context

logger = logging.getLogger("launchpad.usage")

STATE_KEY = "budget_reconcile_state"
# per-workspace online-results watermarks, committed with the rows they cover
ONLINE_KEY = "budget_reconcile_online"
# A window ends this long before now (spans land 1–5 s after they end) …
LAG = timedelta(minutes=2)
# … and a session first seen starts this far before its first settled turn (the
# turn's own spans began before the meter settled it; a Runtime turn runs ≤ 15 min).
NEW_SESSION_LOOKBACK = timedelta(minutes=15)
# A session is re-read for this long after its last settled turn, so spans that
# end a moment after the turn settled are still counted.
RECENT_ACTIVITY = timedelta(minutes=15)
# Never read further back than this in one window (a box down for longer keeps the
# estimates it charged for the missing span).
MAX_WINDOW = timedelta(hours=24)
# Session ids per spans query: the ids are interpolated into an `in [...]` filter
# and a Logs Insights query is at most 10,000 characters.
ID_CHARS_PER_QUERY = 5000
RESULT_LIMIT = 10000
# Sessions read per workspace per tick (oldest activity first): a backlog — the
# first tick after a long switch-off — drains over several ticks instead of
# starting more concurrent Logs Insights queries than the account allows.
MAX_SESSIONS_PER_TICK = 300
# Runtime V2 reclaims idle memory after 120 s (pricing page), so its tail is short.
V2_IDLE_RECLAIM_SECONDS = 120
DEFAULT_MAX_LIFETIME_SECONDS = 28800
RUNTIME_INFO_TTL_SECONDS = 3600.0
# A batch run is counted once it has been terminal this long (its rows settle).
EVAL_SETTLE = timedelta(minutes=2)
# … and is looked at for this long after it finished (each tick re-reads them)
EVAL_LOOKBACK = timedelta(days=7)
EVAL_TERMINAL = ("completed", "stopped", "failed")
MAX_FAILURES_KEPT = 20

REF_EVAL_RUN = "eval_run"
REF_ONLINE_CONFIG = "online_eval_config"
BATCH_RESULTS_SOURCE = (
    "SOURCE logGroups(namePrefix: "
    "['/aws/bedrock-agentcore/evaluations/batch-evaluations/results/'])"
)
ONLINE_RESULTS_SOURCE = (
    "SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])"
)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


# ── time helpers ─────────────────────────────────────────────────────────────


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; every one stored here is UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def window_end(now: datetime | None = None) -> datetime:
    """The exclusive end of this tick's windows: ``now − 2 min``, floored to the minute."""
    return _floor_minute((now or datetime.now(UTC)) - LAG)


def _minute_of(text: Any) -> datetime | None:
    """A ``bin(1m)`` bucket (``"2026-10-15 05:09:00.000"``, UTC) as a datetime."""
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _num(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _parse_iso(value: Any) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(str(value))) if value else None
    except ValueError:
        return None


# ── the persisted state (watermarks + the status the admin route reads) ──────


def load_state(db: Session) -> dict[str, Any]:
    row = db.get(PlatformSetting, STATE_KEY)
    if row is None or not row.value:
        return {}
    try:
        value = json.loads(row.value)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _put_state(db: Session, state: dict[str, Any]) -> None:
    """Stage the state in ``db``'s transaction (the caller commits)."""
    row = db.get(PlatformSetting, STATE_KEY)
    if row is None:
        row = PlatformSetting(key=STATE_KEY)
        db.add(row)
    row.value = json.dumps(state, sort_keys=True, default=str)
    row.updated_by = "budget-reconcile"
    row.updated_at = datetime.now(UTC)


def _update_state(change: Any) -> None:
    """Read-modify-write the state in its own short transaction (this module's
    thread is its only writer)."""
    db = SessionLocal()
    try:
        state = load_state(db)
        change(state)
        _put_state(db, state)
        db.commit()
    finally:
        db.close()


def _ws_state(state: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    return state.setdefault("workspaces", {}).setdefault(workspace_id, {})


def _online_marks(db: Session) -> tuple[dict[str, Any], PlatformSetting | None]:
    row = db.get(PlatformSetting, ONLINE_KEY)
    try:
        value = json.loads(row.value) if row is not None and row.value else {}
    except ValueError:
        value = {}
    return (value if isinstance(value, dict) else {}), row


def online_until(db: Session, workspace_id: str) -> datetime | None:
    return _parse_iso(_online_marks(db)[0].get(workspace_id))


def _stage_online_until(db: Session, workspace_id: str, value: datetime) -> None:
    """Stage a workspace's online watermark in ``db``'s transaction."""
    marks, row = _online_marks(db)
    marks[workspace_id] = _iso(value)
    if row is None:
        row = PlatformSetting(key=ONLINE_KEY)
        db.add(row)
    row.value = json.dumps(marks, sort_keys=True)
    row.updated_by = "budget-reconcile"
    row.updated_at = datetime.now(UTC)


# ── runtime lifecycle (idle timeout, max lifetime, platform version) ────────

_RUNTIME_INFO: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_RUNTIME_INFO_LOCK = threading.Lock()


def reset_runtime_cache() -> None:
    with _RUNTIME_INFO_LOCK:
        _RUNTIME_INFO.clear()


def runtime_id_of(arn_or_resource: str | None) -> str | None:
    """``…:runtime/<id>[/runtime-endpoint/…]`` → ``<id>``; None for a Harness ARN
    or anything else."""
    text = str(arn_or_resource or "")
    marker = ":runtime/"
    if marker not in text:
        return None
    return text.split(marker, 1)[1].split("/", 1)[0] or None


def runtime_arn_of(arn_or_resource: str | None) -> str | None:
    """The runtime ARN of a span's ``cloud.resource_id`` (endpoint suffix dropped)."""
    text = str(arn_or_resource or "")
    runtime_id = runtime_id_of(text)
    if runtime_id is None:
        return None
    return text.split(":runtime/", 1)[0] + ":runtime/" + runtime_id


def runtime_name_of(runtime_id: str | None) -> str:
    """``harness_store_assistant-AbC123dEfG`` → ``harness_store_assistant``."""
    return str(runtime_id or "").rsplit("-", 1)[0]


def runtime_info(ctx: WorkspaceContext, workspace_id: str, runtime_id: str | None,
                 default_idle: float) -> dict[str, Any]:
    """``{idle_s, max_lifetime_s, platform_version, known}`` for a runtime, read
    through ``GetAgentRuntime`` and cached for an hour. A runtime that cannot be
    read (deleted, unknown, an AWS error) gets the defaults: V1, the admin's
    idle timeout, 8 h."""
    fallback = {"idle_s": float(default_idle), "max_lifetime_s": DEFAULT_MAX_LIFETIME_SECONDS,
                "platform_version": "V1", "known": False}
    if not runtime_id:
        return fallback
    key = (workspace_id, runtime_id)
    with _RUNTIME_INFO_LOCK:
        hit = _RUNTIME_INFO.get(key)
        if hit is not None and time.monotonic() - hit[0] < RUNTIME_INFO_TTL_SECONDS:
            return dict(hit[1])
    info = dict(fallback)
    try:
        from app.services.agentcore import runtime as rt
        from app.services.agentcore.client import control_client

        got = rt.get_runtime(control_client(ctx), runtime_id)
        lifecycle = got.get("lifecycleConfiguration") or {}
        info = {
            "idle_s": float(lifecycle.get("idleRuntimeSessionTimeout") or default_idle),
            "max_lifetime_s": float(lifecycle.get("maxLifetime") or DEFAULT_MAX_LIFETIME_SECONDS),
            # absent on older runtimes = V1 (live probe)
            "platform_version": str(got.get("platformVersion") or "V1").upper(),
            "known": True,
        }
    except Exception as exc:  # noqa: BLE001 — a missing runtime keeps the defaults
        logger.info("budget reconcile: runtime %s unreadable (%s); defaults apply",
                    runtime_id, type(exc).__name__)
    with _RUNTIME_INFO_LOCK:
        _RUNTIME_INFO[key] = (time.monotonic(), info)
    return dict(info)


def idle_tail_seconds(info: dict[str, Any]) -> float:
    """What a session is billed after its last activity: the idle timeout (V1), or
    at most the 120 s V2 reclaim window."""
    idle = float(info.get("idle_s") or 0.0)
    if str(info.get("platform_version") or "V1").upper() == "V2":
        return min(idle, float(V2_IDLE_RECLAIM_SECONDS))
    return idle


def compute_rates(info: dict[str, Any], fee: dict[str, float]) -> tuple[float, float]:
    """``($ per GB-hour, $ per vCPU-hour)`` for the runtime's platform version."""
    if str(info.get("platform_version") or "V1").upper() == "V2":
        return fee["runtime_v2_gb_hour"], fee["runtime_v2_vcpu_hour"]
    return fee["runtime_v1_gb_hour"], fee["runtime_v1_vcpu_hour"]


def session_factors(
    runtime_id: str | None, *, harness: bool, measured: dict[str, dict[str, Any]],
    cal: dict[str, float],
) -> tuple[float, float, str]:
    """``(GB resident, vCPU average, basis)`` for one runtime: its measured factors
    once they rest on enough sessions, else the admin's seeds for its kind."""
    entry = measured.get(runtime_id or "") if runtime_id else None
    if entry and int(entry.get("sessions") or 0) >= int(cal["calibration_min_sessions"]):
        return float(entry["gb"]), float(entry["vcpu"]), "measured"
    if harness:
        return cal["runtime_harness_gb"], cal["runtime_harness_vcpu"], "default"
    return cal["runtime_default_gb"], cal["runtime_default_vcpu"], "default"


def billable_seconds(start: float, end: float, tail_s: float, max_lifetime_s: float) -> float:
    """One microVM lifetime: first activity → last activity + the idle tail, never
    past the max lifetime."""
    stop = min(end + tail_s, start + max_lifetime_s)
    return max(0.0, stop - start)


# ── the spans queries ────────────────────────────────────────────────────────


def q_session_spans(session_ids: list[str]) -> str:
    """Per (session, trace, end-minute): tokens, the fee counters and the trace's
    span interval. Filtered to this box's sessions."""
    quoted = ", ".join(f'"{sid}"' for sid in session_ids)
    tool = 'coalesce(attributes.gen_ai.tool.name, "")'
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| filter attributes.session.id in [{quoted}]
| {_IS_LLM_FIELDS}
| {_WRAPPER_FIELDS}
| fields strcontains(name, "Bedrock AgentCore.CreateEvent") as is_mem_event,
         strcontains(name, "Bedrock AgentCore.RetrieveMemoryRecords") as is_mem_retrieval,
         strcontains(name, "Bedrock AgentCore.StartCodeInterpreterSession") as is_ci,
         strcontains(name, "mcp tools/call") * strcontains(kind, "CLIENT") as is_tool_call
| fields is_tool_call * strcontains({tool}, "___WebSearch") as is_web,
         is_tool_call * strcontains({tool}, "___AgenticRetrieve") as is_kb_agentic,
         is_tool_call * strcontains({tool}, "___Retrieve") as is_kb
| stats {_TOKEN_SUMS}, sum(is_llm) as llm_calls,
        latest(telemetry_model) as model,
        sum(is_mem_event) as mem_events, sum(is_mem_retrieval) as mem_retrievals,
        sum(is_web) as web_searches, sum(is_kb) as kb_queries,
        sum(is_kb_agentic) as kb_agentic_queries, sum(is_ci) as ci_sessions,
        min(startTimeUnixNano) as start_ns, max(endTimeUnixNano) as end_ns,
        latest(resource.attributes.cloud.resource_id) as resource
  by attributes.session.id as session_id, traceId, bin(1m) as minute
| limit {RESULT_LIMIT}
"""


def q_gateway_spans(trace_ids: list[str]) -> str:
    """Per (trace, end-minute): Gateway calls (its SERVER spans — they carry no
    session id) and Policy authorizations, for the traces this box's sessions ran
    (joined back to their session client-side)."""
    quoted = ", ".join(f'"{trace}"' for trace in trace_ids)
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and traceId in [{quoted}]
| filter name like /^AgentCore\\.(Gateway|Policy)\\./
| fields strcontains(name, "AgentCore.Gateway.") * strcontains(kind, "SERVER") as is_gw,
         strcontains(name, "AgentCore.Policy.") as is_pol
| stats sum(is_gw) as gateway_calls, sum(is_pol) as policy_calls by traceId, bin(1m) as minute
| filter gateway_calls > 0 or policy_calls > 0
| limit {RESULT_LIMIT}
"""


def q_online_results() -> str:
    """Online evaluation result records per (config, evaluator, minute)."""
    return f"""
{ONLINE_RESULTS_SOURCE}
| filter ispresent(onlineEvaluationConfigId)
| stats count(*) as evaluations
  by onlineEvaluationConfigId as config_id, attributes.gen_ai.evaluation.name as evaluator,
     bin(1m) as minute
| limit {RESULT_LIMIT}
"""


def q_batch_results(batch_ids: list[str]) -> str:
    """Batch evaluation result records per (run, evaluator) — for a finished run
    whose row carries no per-evaluator counts."""
    quoted = ", ".join(f'"{bid}"' for bid in batch_ids)
    return f"""
{BATCH_RESULTS_SOURCE}
| filter runId in [{quoted}]
| stats count(*) as evaluations by runId as batch_id,
        attributes.gen_ai.evaluation.name as evaluator
| limit {RESULT_LIMIT}
"""


def chunk_ids(ids: Iterable[str], budget: int = ID_CHARS_PER_QUERY) -> list[list[str]]:
    """Split ids so each chunk's quoted list stays under ``budget`` characters."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for value in ids:
        cost = len(value) + 4
        if current and size + cost > budget:
            chunks.append(current)
            current, size = [], 0
        current.append(value)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def _insights(logs: Any, queries: dict[str, str], start: datetime, end: datetime
              ) -> dict[str, list[dict[str, str]]]:
    return run_insights_queries(queries, hours=1, logs=logs,
                                start_s=int(start.timestamp()), end_s=int(end.timestamp()))


# Concurrent Logs Insights queries per round: well under the account's limit, which
# the console's own dashboards share.
QUERY_BATCH = 4


def chunked_rows(logs: Any, ids: list[str], build: Any, start: datetime, end: datetime
                 ) -> list[dict[str, str]]:
    """Run ``build(chunk)`` over ``ids`` in chunks (a few at a time) and return every
    row. A chunk whose result reached ``RESULT_LIMIT`` was truncated: it is split in
    half and read again, so a busy window never fails the same way on every tick."""
    pending = chunk_ids(sorted(set(ids)))
    rows: list[dict[str, str]] = []
    while pending:
        batch, pending = pending[:QUERY_BATCH], pending[QUERY_BATCH:]
        results = _insights(logs, {f"q{i}": build(chunk) for i, chunk in enumerate(batch)},
                            start, end)
        for index, chunk in enumerate(batch):
            got = results.get(f"q{index}") or []
            if len(got) >= RESULT_LIMIT:
                if len(chunk) == 1:
                    raise RuntimeError(f"one id alone exceeds {RESULT_LIMIT} result rows")
                half = len(chunk) // 2
                pending.extend([chunk[:half], chunk[half:]])
                continue
            rows.extend(got)
    return rows


# ── per-session measurement ──────────────────────────────────────────────────


@dataclass
class Measure:
    """What one session's spans show in its window."""

    tokens_in: float = 0.0
    tokens_out: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    wrapper_in: float = 0.0
    wrapper_out: float = 0.0
    llm_calls: float = 0.0
    model: str | None = None
    model_at: float = -1.0
    mem_events: int = 0
    mem_retrievals: int = 0
    web_searches: int = 0
    kb_queries: int = 0
    kb_agentic_queries: int = 0
    ci_sessions: int = 0
    gateway_calls: int = 0
    policy_calls: int = 0
    resource: str | None = None
    # trace → [first span start, last span end] (epoch seconds) in the window
    traces: dict[str, list[float]] = field(default_factory=dict)
    last_end: float = 0.0

    @property
    def provider_tokens(self) -> float:
        return self.tokens_in + self.tokens_out + self.cache_read + self.cache_write

    @property
    def wrapper_tokens(self) -> float:
        return self.wrapper_in + self.wrapper_out

    @property
    def seen(self) -> bool:
        return bool(self.traces)


def _absorb_session_row(measure: Measure, row: dict[str, Any]) -> None:
    for name in ("tokens_in", "tokens_out", "cache_read", "cache_write",
                 "wrapper_in", "wrapper_out", "llm_calls"):
        setattr(measure, name, getattr(measure, name) + _num(row, name))
    for name in ("mem_events", "mem_retrievals", "web_searches", "kb_queries",
                 "kb_agentic_queries", "ci_sessions"):
        setattr(measure, name, getattr(measure, name) + int(_num(row, name)))
    start = _num(row, "start_ns") / 1e9
    end = _num(row, "end_ns") / 1e9
    if row.get("model") and end >= measure.model_at:
        measure.model, measure.model_at = str(row["model"]), end
    if row.get("resource") and (measure.resource is None or end >= measure.last_end):
        measure.resource = str(row["resource"])
    trace = str(row.get("traceId") or "")
    if trace and end > 0:
        span = measure.traces.setdefault(trace, [start or end, end])
        span[0] = min(span[0], start or end)
        span[1] = max(span[1], end)
    measure.last_end = max(measure.last_end, end)


@dataclass
class SessionWork:
    session: UsageSession
    start: datetime
    measure: Measure = field(default_factory=Measure)


def measure_sessions(
    logs: Any, work: dict[str, SessionWork], end: datetime, now: datetime,
) -> None:
    """Run the spans queries for ``work``'s sessions and fill each one's measure
    with what ended inside its own window ``[start, end)``. Raises on a query error
    (the caller fails the whole workspace window, which the next tick retries)."""
    if not work:
        return
    scan_start = min(item.start for item in work.values())
    trace_session: dict[str, str] = {}
    # The session query reads up to now (not just `end`): a trace still running at
    # `end` must map its earlier Gateway spans to the session (rows past `end` are
    # counted by the next window, never by this one).
    session_rows = chunked_rows(logs, list(work), q_session_spans, scan_start, now)
    for row in session_rows:
        sid = str(row.get("session_id") or "")
        if row.get("traceId") and sid in work:
            trace_session[str(row["traceId"])] = sid
        item = work.get(sid)
        minute = _minute_of(row.get("minute"))
        if item is None or minute is None or not (item.start <= minute < end):
            continue
        _absorb_session_row(item.measure, row)
    traces = [trace for trace in trace_session if TRACE_ID_RE.fullmatch(trace)]
    gateway_rows = (chunked_rows(logs, traces, q_gateway_spans, scan_start, end)
                    if traces else [])
    for row in gateway_rows:
        sid = trace_session.get(str(row.get("traceId") or ""))
        item = work.get(sid or "")
        minute = _minute_of(row.get("minute"))
        if item is None or minute is None or not (item.start <= minute < end):
            continue
        item.measure.gateway_calls += int(_num(row, "gateway_calls"))
        item.measure.policy_calls += int(_num(row, "policy_calls"))


# ── writing one session's rows ───────────────────────────────────────────────


@dataclass
class Context:
    """What every session of one tick needs, read once."""

    now: datetime
    end: datetime
    fee: dict[str, float]
    cal: dict[str, float]
    measured_factors: dict[str, dict[str, Any]]
    ctx: WorkspaceContext
    workspace_id: str


def _event(us: UsageSession, *, kind: str, period: str, usd: float | None,
           estimated: bool, quantity: float | None = None, unit: str | None = None,
           model_id: str | None = None, detail: dict[str, Any] | None = None,
           tokens: tuple[float, float, float, float] = (0, 0, 0, 0),
           ref: tuple[str | None, str | None] = (None, None), now: datetime) -> UsageEvent:
    return UsageEvent(
        user_id=us.user_id, user_name=us.user_name, period=period, source=us.source,
        kind=kind, origin_workspace_id=us.origin_workspace_id, agent_id=us.agent_id,
        session_id=us.session_id, model_id=(model_id or None) and str(model_id)[:160],
        tokens_in=int(tokens[0]), tokens_out=int(tokens[1]), cache_read=int(tokens[2]),
        cache_write=int(tokens[3]), quantity=quantity, unit=unit,
        usd=None if usd is None else round(float(usd), 8), estimated=estimated,
        ref_type=ref[0], ref_id=ref[1], detail=detail, created_at=now,
    )


def _is_reconciled(row: UsageEvent) -> bool:
    return isinstance(row.detail, dict) and bool(row.detail.get("reconciled"))


def _agent_model(db: Session, agent_id: str | None) -> tuple[str | None, bool]:
    """The session agent's model (a fallback when spans name none) and whether it
    is a Harness."""
    agent = db.get(Agent, agent_id) if agent_id else None
    if agent is None:
        return None, False
    from app.services.runtime_discovery import is_discovered_harness

    model, _ = budgets.agent_model(agent)
    return model, agent.method == "harness" or is_discovered_harness(agent)


def _fee_rows(us: UsageSession, m: Measure, c: Context, period: str
              ) -> list[UsageEvent]:
    """The exact per-session fee rows (one per kind with a non-zero count)."""
    fee, cal, out = c.fee, c.cal, []
    base = {"reconciled": True, "source": "spans", "window_end": _iso(c.end)}

    def add(kind: str, qty: float, usd: float, unit: str, extra: dict[str, Any] | None = None,
            estimated: bool = False) -> None:
        out.append(_event(us, kind=kind, period=period, usd=usd, estimated=estimated,
                          quantity=float(qty), unit=unit, detail={**base, **(extra or {})},
                          now=c.now))

    if m.mem_events:
        add(um.KIND_MEMORY_EVENT, m.mem_events, m.mem_events * fee["memory_event"], "event")
    if m.mem_retrievals:
        add(um.KIND_MEMORY_RETRIEVAL, m.mem_retrievals,
            m.mem_retrievals * fee["memory_retrieval"], "request")
    if m.gateway_calls or m.policy_calls:
        add(um.KIND_GATEWAY_CALL, m.gateway_calls,
            m.gateway_calls * fee["gateway_call"]
            + m.policy_calls * fee["policy_authorization"], "call",
            {"gateway_calls": m.gateway_calls, "policy_authorizations": m.policy_calls})
    if m.web_searches:
        add(um.KIND_WEB_SEARCH, m.web_searches, m.web_searches * fee["web_search_query"],
            "query")
    if m.kb_queries or m.kb_agentic_queries:
        add(um.KIND_KB_QUERY, m.kb_queries + m.kb_agentic_queries,
            m.kb_queries * fee["kb_query"] + m.kb_agentic_queries * fee["kb_agentic_query"],
            "query", {"retrieve": m.kb_queries, "agentic_retrieve": m.kb_agentic_queries})
    if m.ci_sessions:
        each = (cal["code_interpreter_gb_hours_per_session"] * fee["code_interpreter_gb_hour"]
                + cal["code_interpreter_vcpu_hours_per_session"]
                * fee["code_interpreter_vcpu_hour"])
        # the tool session's own lifetime is not on any span: a calibrated average
        add(um.KIND_CODE_INTERPRETER, m.ci_sessions, m.ci_sessions * each, "tool_session",
            estimated=True)
    return out


def _tokens(
    db: Session, us: UsageSession, m: Measure, c: Context, period: str,
    prior_tokens: list[UsageEvent], estimates: list[UsageEvent],
) -> tuple[list[UsageEvent], int]:
    """(a): the measured tokens row and the estimates it supersedes. Nothing for a
    session metered in-band (exact rows that reconciliation did not write)."""
    if any(not _is_reconciled(row) for row in prior_tokens):
        return [], 0
    had_provider = any((row.detail or {}).get("provider_spans") for row in prior_tokens)
    tokens: tuple[float, float, float, float] | None = None
    wrapper = False
    if m.provider_tokens > 0:
        tokens = (m.tokens_in, m.tokens_out, m.cache_read, m.cache_write)
    elif m.wrapper_tokens > 0 and not had_provider:
        # a non-Bedrock provider (Mantle) emits only the Strands wrapper span; a
        # session already measured on provider spans never falls back, or a wrapper
        # span that ended a minute after its provider span would count twice
        tokens, wrapper = (m.wrapper_in, m.wrapper_out, 0.0, 0.0), True
    replaced = [row for row in estimates
                if row.kind in um.ESTIMATE_KINDS and _utc(row.created_at) < c.end
                and (row.detail or {}).get("estimate") != "persona_actor"]
    rows: list[UsageEvent] = []
    if tokens is None:
        # No tokens in this window. A session already measured on spans still has
        # its stray estimates (a turn settled just after the window that counted its
        # spans) replaced by its latest priced measurement.
        latest = next((row for row in reversed(prior_tokens) if row.usd is not None), None)
        if latest is not None and replaced:
            return [], um.supersede([row.id for row in replaced], latest.id, db=db)
        return [], 0
    model = m.model
    if not model:
        model, _ = _agent_model(db, us.agent_id)
    usd, priced = rates.price_tokens(model, *tokens)
    ref = next(((row.ref_type, row.ref_id) for row in reversed(estimates) if row.ref_type),
               (None, None))
    detail: dict[str, Any] = {
        "reconciled": True, "source": "spans", "window_end": _iso(c.end),
        "llm_calls": int(m.llm_calls), "provider_spans": not wrapper,
        "replaces": len(replaced) if priced else 0,
    }
    if wrapper:
        detail["wrapper_only"] = True
    if not priced:
        # an unpriced measurement would let the session cost nothing: the estimates
        # stay charged and the model shows up in the admin price editor
        detail["unpriced"] = True
    row = _event(us, kind=um.KIND_TOKENS, period=period, usd=usd, estimated=False,
                 model_id=model, detail=detail, tokens=tokens, ref=ref, now=c.now)
    db.add(row)
    db.flush()
    rows.append(row)
    superseded = um.supersede([r.id for r in replaced], row.id, db=db) if priced else 0
    return rows, superseded


def _runtime(
    db: Session, us: UsageSession, m: Measure, c: Context, window_start: datetime,
    open_rows: list[UsageEvent], harness_hint: bool | None,
) -> tuple[int, int]:
    """(c): extend the session's open ``runtime_compute`` row in place, or open a new
    one per new microVM lifetime. Returns ``(created, updated)``."""
    intervals = sorted(m.traces.values())
    basis = "spans"
    if not intervals:
        spanned = any((row.detail or {}).get("basis") == "spans" for row in open_rows)
        points = []
        last = _utc(us.last_turn_at)
        first = _utc(us.first_turn_at)
        if us.reconciled_until is None and first is not None and window_start <= first < c.end:
            points.append(first.timestamp())
        if last is not None and window_start <= last < c.end:
            points.append(last.timestamp())
        if spanned or not points:
            return 0, 0
        # No spans at all (an uninstrumented agent): the settled turns only — the
        # idle tail is still right; active time is a lower bound.
        intervals, basis = [[p, p] for p in sorted(points)], "ledger"
    runtime_arn = runtime_arn_of(m.resource) or runtime_arn_of(us.resource_id)
    runtime_id = runtime_id_of(runtime_arn)
    harness = (runtime_name_of(runtime_id).startswith("harness_") if runtime_id
               else bool(harness_hint) or ":harness/" in str(us.resource_id or ""))
    info = runtime_info(c.ctx, c.workspace_id, runtime_id,
                        c.cal["runtime_idle_timeout_seconds"])
    tail = idle_tail_seconds(info)
    gb, vcpu, factor = session_factors(runtime_id, harness=harness,
                                       measured=c.measured_factors, cal=c.cal)
    gb_rate, vcpu_rate = compute_rates(info, c.fee)
    current = open_rows[-1] if open_rows else None
    existing = {id(row) for row in open_rows}
    created = 0
    updated: set[int] = set()
    for start, end in intervals:
        start, end = float(start), float(max(start, end))
        period = budgets.period_of(datetime.fromtimestamp(start, UTC))
        detail = dict(current.detail or {}) if current is not None else None
        # the same microVM: this month's row, activity resumed before the idle tail
        # expired, and the max lifetime not yet reached
        alive = (
            detail is not None and current is not None and current.period == period
            and start <= float(detail.get("end", 0)) + float(detail.get("tail_s", tail))
            and start < float(detail.get("start", 0)) + float(
                detail.get("max_lifetime_s", info["max_lifetime_s"]))
        )
        if alive and detail is not None and current is not None:
            detail["start"] = min(float(detail["start"]), start)
            detail["end"] = max(float(detail["end"]), end)
            if id(current) in existing:
                updated.add(id(current))  # THE in-place update (see the module doc)
        else:
            detail = {"start": start, "end": end}
            current = _event(us, kind=um.KIND_RUNTIME_COMPUTE, period=period, usd=0.0,
                             estimated=True, quantity=0.0, unit="session_hour",
                             detail={}, now=c.now)
            db.add(current)
            created += 1
        detail.update({
            "reconciled": True, "basis": basis if detail.get("basis") != "spans" else "spans",
            "tail_s": tail, "max_lifetime_s": info["max_lifetime_s"],
            "platform_version": info["platform_version"], "runtime_known": info["known"],
            "runtime_id": runtime_id, "runtime_arn": runtime_arn, "harness": harness,
            "gb": gb, "vcpu": vcpu, "factor": factor,
        })
        seconds = billable_seconds(float(detail["start"]), float(detail["end"]), tail,
                                   float(info["max_lifetime_s"]))
        hours = seconds / 3600.0
        current.detail = detail  # a new dict: the JSON column only sees reassignment
        current.quantity = round(hours, 6)
        current.usd = round(hours * (gb * gb_rate + vcpu * vcpu_rate), 8)
    return created, len(updated)


def reconcile_session(item: SessionWork, c: Context) -> dict[str, int]:
    """Write one session's rows and advance its watermark, atomically."""
    us_key = (item.session.session_id, item.session.origin_workspace_id)
    db = SessionLocal()
    try:
        us = db.get(UsageSession, us_key)
        if us is None:  # purged since the selection
            return {}
        origin = UsageEvent.origin_workspace_id == us.origin_workspace_id
        if us.origin_workspace_id == DEFAULT_WORKSPACE_ID:
            # a meter with no workspace records the session under the default one
            origin = or_(origin, UsageEvent.origin_workspace_id.is_(None))
        rows = (
            db.query(UsageEvent)
            .filter(UsageEvent.session_id == us.session_id, origin,
                    UsageEvent.superseded_by.is_(None))
            .order_by(UsageEvent.created_at)
            .all()
        )
        prior_tokens = [row for row in rows if row.kind == um.KIND_TOKENS]
        estimates = [row for row in rows if row.kind in um.ESTIMATE_KINDS]
        open_runtime = [row for row in rows if row.kind == um.KIND_RUNTIME_COMPUTE]
        m = item.measure
        period = budgets.period_of(
            datetime.fromtimestamp(m.last_end, UTC) if m.last_end else c.end - timedelta(seconds=1)
        )
        token_rows, superseded = _tokens(db, us, m, c, period, prior_tokens, estimates)
        fees = _fee_rows(us, m, c, period)
        db.add_all(fees)
        _model, harness = _agent_model(db, us.agent_id)
        created, updated = _runtime(db, us, m, c, item.start, open_runtime, harness)
        us.reconciled_until = c.end
        db.commit()
        return {"rows": len(token_rows) + len(fees) + created, "updated": updated,
                "superseded": superseded}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _select_sessions(db: Session, workspace_id: str, now: datetime) -> list[UsageSession]:
    rows = (
        db.query(UsageSession)
        .filter(UsageSession.origin_workspace_id == workspace_id)
        .filter(or_(
            UsageSession.reconciled_until.is_(None),
            UsageSession.last_turn_at > UsageSession.reconciled_until,
            UsageSession.last_turn_at >= now - RECENT_ACTIVITY,
        ))
        .order_by(UsageSession.last_turn_at, UsageSession.session_id)
        .limit(MAX_SESSIONS_PER_TICK)
        .all()
    )
    for row in rows:
        db.expunge(row)
    return rows


def window_start(us: UsageSession, end: datetime, floor: datetime | None) -> datetime:
    """A session's window start: its watermark, or (first seen) its first settled
    turn less the lookback; never more than ``MAX_WINDOW`` back. ``floor`` is the
    workspace's last successful selection less the lookback: a session idle until
    then has nothing to read before it, so an old watermark does not widen the
    scan."""
    if us.reconciled_until is None:
        start = _floor_minute(_utc(us.first_turn_at) - NEW_SESSION_LOOKBACK)
    else:
        start = _utc(us.reconciled_until)
        if floor is not None and start < floor:
            start = floor
    return max(start, end - MAX_WINDOW)


def reconcile_workspace_sessions(
    workspace_id: str, ctx: WorkspaceContext, c: Context, state: dict[str, Any],
    logs: Any = None,
) -> dict[str, Any]:
    """Every selected session of one workspace: one spans read, then one
    transaction per session. Raises when the read fails (no watermark moves)."""
    db = SessionLocal()
    try:
        sessions = _select_sessions(db, workspace_id, c.now)
    finally:
        db.close()
    ws_state = _ws_state(state, workspace_id)
    last_selection = _parse_iso(ws_state.get("last_selection_at"))
    floor = (_floor_minute(last_selection - NEW_SESSION_LOOKBACK)
             if last_selection is not None else None)
    work: dict[str, SessionWork] = {}
    skipped: list[UsageSession] = []
    for us in sessions:
        start = window_start(us, c.end, floor)
        if start >= c.end:
            continue
        if not SESSION_ID_RE.fullmatch(us.session_id or ""):
            skipped.append(us)  # never interpolated into a query
            continue
        work[us.session_id] = SessionWork(session=us, start=start)
    selection_at = c.now
    measure_sessions(logs if logs is not None else logs_client(ctx), work, c.end, c.now)
    out = {"sessions": len(work), "rows": 0, "updated": 0, "superseded": 0, "failed": 0}
    for item in work.values():
        try:
            result = reconcile_session(item, c)
        except Exception as exc:  # noqa: BLE001 — one session must not stop the rest
            out["failed"] += 1
            logger.warning("budget reconcile: session %s failed: %s: %s",
                           item.session.session_id[:16], type(exc).__name__, exc)
            continue
        for key in ("rows", "updated", "superseded"):
            out[key] += int(result.get(key) or 0)
    if skipped:
        db = SessionLocal()
        try:
            for us in skipped:
                row = db.get(UsageSession, (us.session_id, us.origin_workspace_id))
                if row is not None:
                    row.reconciled_until = c.end
            db.commit()
        finally:
            db.close()
    if not out["failed"] and len(sessions) < MAX_SESSIONS_PER_TICK:
        # Only a complete, fully successful pass may narrow later windows (see
        # window_start): a session left for the next tick by the cap, or one whose
        # write failed, still has spans before this selection to read.
        ws_state["last_selection_at"] = _iso(selection_at)
    ws_state["window_end"] = _iso(c.end)
    return out


# ── (d) evaluations ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvalPayer:
    user_id: str | None
    user_name: str | None
    source: str
    agent_id: str | None = None


def _eval_prices(c: Context) -> tuple[dict[str, float], float, str | None]:
    """``({builtin_batch, builtin_online}, custom_each, judge_model)``: what one
    evaluation costs at the calibrated tokens per evaluation (child 2's formula)."""
    from app.core.config import get_settings

    fee, cal = c.fee, c.cal
    tin, tout = cal["builtin_eval_input_tokens"], cal["builtin_eval_output_tokens"]
    builtin = {
        "batch": tin / 1000 * fee["evaluation_batch_input_per_1k"]
        + tout / 1000 * fee["evaluation_batch_output_per_1k"],
        "online": tin / 1000 * fee["evaluation_builtin_input_per_1k"]
        + tout / 1000 * fee["evaluation_builtin_output_per_1k"],
    }
    judge = (get_settings().judge_model_id or "").strip() or None
    judge_usd, _priced = rates.price_tokens(judge, tin, tout)
    return builtin, fee["evaluation_custom"] + (judge_usd or 0.0), judge


def _evaluator_kind(evaluator: str) -> str | None:
    """``builtin`` | ``custom`` | None (an insight: free during the preview)."""
    if evaluator.startswith("Builtin.Insight."):
        return None
    return "builtin" if evaluator.startswith(("Builtin.", "ThirdParty.")) else "custom"


def evaluation_rows(
    counts: dict[str, int], payer: EvalPayer, *, batch: bool, c: Context, period: str,
    ref: tuple[str, str], extra: dict[str, Any] | None = None, zero_row: bool = False,
) -> list[UsageEvent]:
    """One ``evaluation_builtin`` and one ``evaluation_custom`` row for these
    per-evaluator counts (the kinds with a non-zero count); ``zero_row`` keeps an
    empty builtin row so a judge estimate always has something to point at."""
    builtin_each, custom_each, judge = _eval_prices(c)
    tier = "batch" if batch else "online"
    split: dict[str, dict[str, int]] = {"builtin": {}, "custom": {}}
    for evaluator, count in counts.items():
        kind = _evaluator_kind(evaluator)
        if kind and count > 0:
            split[kind][evaluator] = split[kind].get(evaluator, 0) + int(count)
    out: list[UsageEvent] = []
    for kind, per in split.items():
        total = sum(per.values())
        if not total and not (zero_row and kind == "builtin" and not out
                              and not sum(split["custom"].values())):
            continue
        each = builtin_each[tier] if kind == "builtin" else custom_each
        out.append(UsageEvent(
            user_id=payer.user_id, user_name=payer.user_name, period=period,
            source=payer.source,
            kind=um.KIND_EVALUATION_BUILTIN if kind == "builtin" else um.KIND_EVALUATION_CUSTOM,
            origin_workspace_id=c.workspace_id, agent_id=payer.agent_id,
            model_id=judge if kind == "custom" else None,
            quantity=float(total), unit="evaluation", usd=round(total * each, 8),
            estimated=True, ref_type=ref[0], ref_id=ref[1],
            detail={"reconciled": True, "evaluators": per, "tier": tier,
                    "per_evaluation_usd": round(each, 8), **(extra or {})},
            created_at=c.now,
        ))
    return out


def _run_counts(run: EvalRun) -> dict[str, int] | None:
    """Per-evaluator evaluation counts from a finished run's scores, or None when
    the row carries none (``count`` = ``totalEvaluated`` = its result records)."""
    counts: dict[str, int] = {}
    seen = False
    for score in run.scores or []:
        if not isinstance(score, dict) or "count" not in score:
            continue
        try:
            count = int(score.get("count") or 0)
        except (TypeError, ValueError):
            continue
        seen = True
        evaluator = str(score.get("evaluatorId") or score.get("evaluator_id") or "")
        if evaluator:
            counts[evaluator] = counts.get(evaluator, 0) + count
    return counts if seen else None


def reconcile_eval_runs(
    workspace_id: str, ctx: WorkspaceContext, c: Context, state: dict[str, Any],
    logs: Any = None,
) -> dict[str, Any]:
    """Count each finished batch run's evaluations once and charge its creator,
    superseding the judge estimate. Runs that predate the reconciler and carry no
    judge estimate are left alone (nothing is charged retroactively)."""
    first_tick = _parse_iso(state.get("first_tick_at")) or c.now
    db = SessionLocal()
    try:
        candidates = (
            db.query(EvalRun)
            .filter(EvalRun.workspace_id == workspace_id, EvalRun.mode == "evaluators",
                    EvalRun.batch_eval_id.is_not(None), EvalRun.status.in_(EVAL_TERMINAL),
                    EvalRun.updated_at <= c.now - EVAL_SETTLE,
                    EvalRun.updated_at >= c.now - EVAL_LOOKBACK)
            .order_by(EvalRun.updated_at.desc())
            .limit(500)
            .all()
        )
        ids = [run.id for run in candidates]
        if not ids:
            return {"runs": 0, "rows": 0}
        refs = defaultdict(list)
        for row in db.query(UsageEvent).filter(UsageEvent.ref_type == REF_EVAL_RUN,
                                               UsageEvent.ref_id.in_(ids)):
            refs[row.ref_id].append(row)
        pending: list[tuple[EvalRun, list[str]]] = []
        for run in candidates:
            mine = refs.get(run.id, [])
            if any(row.kind in (um.KIND_EVALUATION_BUILTIN, um.KIND_EVALUATION_CUSTOM)
                   and _is_reconciled(row) for row in mine):
                continue  # counted already
            judges = [row.id for row in mine if row.kind == um.KIND_JOB_ESTIMATE
                      and row.superseded_by is None
                      and (row.detail or {}).get("estimate") == "judges"]
            if not judges and _utc(run.created_at) < first_tick:
                continue  # before budgets reconciled anything: never charged back
            pending.append((run, judges))
        for run, _ in pending:
            db.expunge(run)
    finally:
        db.close()
    missing = [run.batch_eval_id for run, _ in pending
               if _run_counts(run) is None and _RUN_ID_RE.fullmatch(run.batch_eval_id or "")]
    from_records: dict[str, dict[str, int]] = {}
    if missing:
        earliest = min(_utc(run.created_at) for run, _ in pending) - timedelta(hours=1)
        rows = _insights(logs if logs is not None else logs_client(ctx),
                         {"batch": q_batch_results(missing)}, earliest, c.now)["batch"]
        for row in rows:
            per = from_records.setdefault(str(row.get("batch_id") or ""), {})
            evaluator = str(row.get("evaluator") or "")
            if evaluator:
                per[evaluator] = per.get(evaluator, 0) + int(_num(row, "evaluations"))
    written = 0
    for run, judges in pending:
        counts = _run_counts(run)
        if counts is None:
            counts = from_records.get(run.batch_eval_id or "", {})
        payer = EvalPayer(run.created_by, run.created_by_name, SOURCE_EVAL, run.agent_id)
        period = budgets.period_of(_utc(run.updated_at))
        db = SessionLocal()
        try:
            rows = evaluation_rows(counts, payer, batch=True, c=c, period=period,
                                   ref=(REF_EVAL_RUN, run.id),
                                   extra={"batch_eval_id": run.batch_eval_id},
                                   zero_row=True)
            db.add_all(rows)
            db.flush()
            if rows and judges:
                um.supersede(judges, rows[0].id, db=db)
            db.commit()
            written += len(rows)
        except Exception as exc:  # noqa: BLE001 — retried next tick
            db.rollback()
            logger.warning("budget reconcile: eval run %s failed: %s", run.id, exc)
        finally:
            db.close()
    return {"runs": len(pending), "rows": written}


def online_payers(db: Session, workspace_id: str) -> dict[str, EvalPayer]:
    """Config id **or name prefix** → who pays for its evaluations: a console config
    its creator; an experiment's ``exp_<id8>_oe1`` the caller of the action that
    created it (``gateway``), a canary's ``can_<id8>_oec|oet`` the caller of
    ``setup`` (each falling back to the row's creator). External configs have no
    payer and are never charged."""
    out: dict[str, EvalPayer] = {}
    for row in db.query(OnlineEvalConfig).filter(OnlineEvalConfig.workspace_id == workspace_id):
        out[row.config_id] = EvalPayer(row.created_by, row.created_by_name, SOURCE_EVAL,
                                       row.agent_id)
    for model, prefix, action, source in (
        (Experiment, "exp_", "gateway", SOURCE_EXPERIMENT),
        (RuntimeCanary, "can_", "setup", SOURCE_CANARY),
    ):
        for row in db.query(model).filter(model.workspace_id == workspace_id):
            caller = ((row.artifacts or {}).get("action_callers") or {}).get(action) or {}
            user_id = caller.get("user_id") if caller else row.created_by
            name = caller.get("name") if caller else row.created_by_name
            agent_id = getattr(row, "agent_id", None) or getattr(row, "champion_agent_id", None)
            out[f"{prefix}{row.id[:8]}_"] = EvalPayer(user_id, name, source, agent_id)
    return out


def payer_of_config(payers: dict[str, EvalPayer], config_id: str) -> EvalPayer | None:
    if config_id in payers:
        return payers[config_id]
    name = config_id.rsplit("-", 1)[0]
    for prefix in ("exp_", "can_"):
        if name.startswith(prefix):
            id8 = name[len(prefix):].split("_", 1)[0]
            return payers.get(f"{prefix}{id8}_")
    return None


def reconcile_online(
    workspace_id: str, ctx: WorkspaceContext, c: Context, state: dict[str, Any],
    logs: Any = None,
) -> dict[str, Any]:
    """Count the online result records judged in ``[online_until, end)`` per config
    this box's ledger knows, and charge each config's payer. The rows and the new
    watermark commit together."""
    del state  # the watermark has its own key, committed with the rows it covers
    db = SessionLocal()
    try:
        payers = online_payers(db, workspace_id)
        start = online_until(db, workspace_id)
        if start is None or not payers:
            # First run (or nothing to charge): start now — evaluations judged
            # before reconciliation existed are never charged back.
            _stage_online_until(db, workspace_id, c.end)
            db.commit()
            return {"configs": 0, "rows": 0}
    finally:
        db.close()
    start = max(start, c.end - MAX_WINDOW)
    if start >= c.end:
        return {"configs": 0, "rows": 0}
    rows = _insights(logs if logs is not None else logs_client(ctx),
                     {"online": q_online_results()}, start, c.end)["online"]
    if len(rows) >= RESULT_LIMIT:
        raise RuntimeError(f"online results query hit the {RESULT_LIMIT}-row limit")
    per_config: dict[str, dict[str, int]] = {}
    last_minute: dict[str, datetime] = {}
    for row in rows:
        minute = _minute_of(row.get("minute"))
        config_id = str(row.get("config_id") or "")
        evaluator = str(row.get("evaluator") or "")
        if minute is None or not (start <= minute < c.end) or not config_id or not evaluator:
            continue
        if payer_of_config(payers, config_id) is None:
            continue  # another box's, or an external config: not ours to charge
        counts = per_config.setdefault(config_id, {})
        counts[evaluator] = counts.get(evaluator, 0) + int(_num(row, "evaluations"))
        last_minute[config_id] = max(last_minute.get(config_id, minute), minute)
    db = SessionLocal()
    written = 0
    try:
        for config_id, counts in per_config.items():
            payer = payer_of_config(payers, config_id)
            if payer is None:
                continue
            new = evaluation_rows(counts, payer, batch=False, c=c,
                                  period=budgets.period_of(last_minute[config_id]),
                                  ref=(REF_ONLINE_CONFIG, config_id),
                                  extra={"window_end": _iso(c.end)})
            db.add_all(new)
            written += len(new)
        _stage_online_until(db, workspace_id, c.end)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"configs": len(per_config), "rows": written}


# ── the tick ─────────────────────────────────────────────────────────────────


def _context(db: Session, ctx: WorkspaceContext, workspace_id: str, now: datetime) -> Context:
    return Context(
        now=now, end=window_end(now), fee=rates.agentcore_rates(db), cal=rates.calibration(db),
        measured_factors=rates.runtime_factors(db), ctx=ctx, workspace_id=workspace_id,
    )


def _workspaces(db: Session) -> list[Workspace]:
    rows = db.scalars(select(Workspace).order_by(Workspace.id)).all()
    for row in rows:
        db.expunge(row)
    return list(rows)


def _has_online_owners(db: Session, workspace_id: str) -> bool:
    for model in (OnlineEvalConfig, Experiment, RuntimeCanary):
        if db.query(model.id).filter(model.workspace_id == workspace_id).first() is not None:
            return True
    return False


def _failure(state: dict[str, Any], workspace_id: str, part: str, exc: BaseException,
             now: datetime) -> dict[str, Any]:
    entry = {"workspace_id": workspace_id, "part": part, "at": _iso(now),
             "error": f"{type(exc).__name__}: {exc}"[:300]}
    _ws_state(state, workspace_id)["last_error"] = entry
    return entry


def tick(now: datetime | None = None, *, logs_for: Any = None) -> dict[str, Any]:
    """One reconciliation pass over every workspace. Never raises.

    ``logs_for`` (tests) maps a workspace id to the Logs client to use."""
    now = now or datetime.now(UTC)
    started = time.monotonic()
    outcome: dict[str, Any] = {"window_end": _iso(window_end(now)), "workspaces": {},
                               "failures": []}
    try:
        db = SessionLocal()
        try:
            state = load_state(db)
            workspaces = _workspaces(db)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — the loop must never die
        logger.warning("budget reconcile: could not read the ledger: %s", exc)
        return outcome
    state.setdefault("first_tick_at", _iso(now))
    for ws in workspaces:
        result: dict[str, Any] = {}
        try:
            ctx = workspace_context(ws)
            db = SessionLocal()
            try:
                c = _context(db, ctx, ws.id, now)
                online = _has_online_owners(db, ws.id)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 — a spoke whose role cannot be assumed
            outcome["failures"].append(_failure(state, ws.id, "context", exc, now))
            continue
        logs = logs_for(ws.id) if logs_for is not None else None
        parts: list[tuple[str, Any]] = [
            ("sessions", reconcile_workspace_sessions),
            ("evaluations", reconcile_eval_runs),
        ]
        if online:
            parts.append(("online", reconcile_online))
        for part, fn in parts:
            try:
                result[part] = fn(ws.id, ctx, c, state, logs=logs)
            except Exception as exc:  # noqa: BLE001 — retried next tick
                logger.warning("budget reconcile: %s failed for workspace %s: %s: %s",
                               part, ws.id, type(exc).__name__, exc)
                outcome["failures"].append(_failure(state, ws.id, part, exc, now))
        if not any(f["workspace_id"] == ws.id for f in outcome["failures"]):
            ws_state = _ws_state(state, ws.id)
            ws_state["last_ok_at"] = _iso(now)
            ws_state.pop("last_error", None)
        outcome["workspaces"][ws.id] = result
    try:
        from app.services import online_budget

        outcome["paused"] = online_budget.enforce()
    except Exception as exc:  # noqa: BLE001
        logger.warning("budget reconcile: pausing online configs failed: %s", exc)
        outcome["failures"].append({"workspace_id": None, "part": "pause", "at": _iso(now),
                                    "error": f"{type(exc).__name__}: {exc}"[:300]})
    outcome["duration_s"] = round(time.monotonic() - started, 2)
    try:
        db = SessionLocal()
        try:
            totals = {"sessions": 0, "rows": 0, "updated": 0, "superseded": 0,
                      "runs": 0, "configs": 0}
            for result in outcome["workspaces"].values():
                for part in result.values():
                    if isinstance(part, dict):
                        for key in totals:
                            totals[key] += int(part.get(key) or 0)
            outcome["totals"] = totals
            state["last_tick"] = {
                "started_at": _iso(now), "finished_at": _iso(datetime.now(UTC)),
                "duration_s": outcome["duration_s"], "window_end": outcome["window_end"],
                **totals, "paused": len(outcome.get("paused") or []),
                "failures": outcome["failures"][:MAX_FAILURES_KEPT],
            }
            if outcome["failures"]:
                state["last_failure_at"] = _iso(now)
            _put_state(db, state)
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("budget reconcile: could not save the status: %s", exc)
    return outcome


# ── the admin status ─────────────────────────────────────────────────────────


def pending_sessions(db: Session) -> tuple[int, datetime | None]:
    """Sessions with activity the reconciler has not read yet, and the oldest
    such activity."""
    count, oldest = (
        db.query(func.count(UsageSession.session_id), func.min(UsageSession.last_turn_at))
        .filter(or_(UsageSession.reconciled_until.is_(None),
                    UsageSession.last_turn_at > UsageSession.reconciled_until))
        .one()
    )
    return int(count or 0), _utc(oldest)


def kind_totals(db: Session, period: str) -> list[dict[str, Any]]:
    """This period's spend by kind, split into measured and estimated."""
    rows = (
        db.query(UsageEvent.kind, UsageEvent.estimated,
                 func.coalesce(func.sum(UsageEvent.usd), 0.0), func.count(UsageEvent.id))
        .filter(UsageEvent.period == period, UsageEvent.superseded_by.is_(None))
        .group_by(UsageEvent.kind, UsageEvent.estimated)
        .all()
    )
    kinds: dict[str, dict[str, Any]] = {}
    for kind, estimated, usd, count in rows:
        entry = kinds.setdefault(kind, {"kind": kind, "usd": 0.0, "estimated_usd": 0.0,
                                        "events": 0})
        entry["usd"] += float(usd or 0.0)
        entry["events"] += int(count or 0)
        if estimated:
            entry["estimated_usd"] += float(usd or 0.0)
    out = sorted(kinds.values(), key=lambda e: (-e["usd"], e["kind"]))
    for entry in out:
        entry["usd"] = round(entry["usd"], 6)
        entry["estimated_usd"] = round(entry["estimated_usd"], 6)
    return out


def superseded_usd(db: Session, period: str) -> float:
    """What estimates reconciliation replaced this period were charged."""
    total = (
        db.query(func.coalesce(func.sum(UsageEvent.usd), 0.0))
        .filter(UsageEvent.period == period, UsageEvent.superseded_by.is_not(None),
                UsageEvent.estimated.is_(True))
        .scalar()
    )
    return round(float(total or 0.0), 6)
