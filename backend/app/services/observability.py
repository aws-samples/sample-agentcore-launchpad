"""Observability aggregations — Logs Insights over AgentCore telemetry + metrics.

Design contract: design/mockup-observability.html. Every view is served from a
60s TTL cache so the (slow, billed-per-scan) Logs Insights queries run at most
once a minute per (view, range). Tokens-by-model comes from the
`bedrock-agentcore` metrics namespace (gen_ai.client.token.usage, per the
mockup); top tools and all trace/session rollups cover both the legacy shared
aws/spans destination and unified per-runtime log groups.

Cost figures are advisory estimates: token counts × config `model_prices`
(USD per 1M tokens, substring-matched on the model id; unknown model → None).
"""

import json
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.evaluation.models import EvalRun
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.optimization.models import Experiment
from app.services import memory
from app.services.workspace import WorkspaceContext

SPANS_LOG_GROUP = "aws/spans"
RUNTIME_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"
SPANS_SOURCE = (
    "SOURCE logGroups(namePrefix: "
    f"['{SPANS_LOG_GROUP}', '{RUNTIME_LOG_GROUP_PREFIX}'])"
)
RANGE_HOURS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
BIN_BY_RANGE = {"1h": "5m", "6h": "15m", "24h": "1h", "7d": "6h"}
# List caps: the UI paginates client-side (50/100/200 per page), so fetch a
# deeper window per Logs Insights query; row counts this size are cheap.
TRACE_LIMIT = 500
SESSION_LIMIT = 500
SPANS_PER_TRACE = 500
CACHE_TTL_SECONDS = 60.0
QUERY_DEADLINE_SECONDS = 55
NANOS_PER_MS = 1_000_000

# Anthropic-convention multipliers applied when a price entry has no explicit
# cache_read / cache_write rate — advisory, like every cost figure here.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25

# The router enforces these shapes too; re-checked here (defense in depth)
# because the ids are interpolated into Logs Insights query strings.
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: dict[str, threading.Lock] = {}


def _require(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise AppError("observability.bad_id", f"invalid {what}", status_code=422)
    return value


def _now() -> float:
    return time.time()


def reset_cache() -> None:
    _CACHE.clear()
    _KEY_LOCKS.clear()


def cached(key: str, force: bool, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """60s TTL cache with per-key single-flight (Logs Insights is billed per
    scan — concurrent misses must not stampede) and expired-entry eviction
    (detail keys are per trace/session id and would otherwise accumulate)."""

    def lookup() -> dict[str, Any] | None:
        hit = _CACHE.get(key)
        if hit and not force and _now() - hit[0] < CACHE_TTL_SECONDS:
            return {**hit[1], "cache": {"hit": True, "age_seconds": round(_now() - hit[0], 1)}}
        return None

    if (fresh := lookup()) is not None:
        return fresh
    with _CACHE_LOCK:
        key_lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        if (fresh := lookup()) is not None:  # a concurrent request built it
            return fresh
        value = build()
        now = _now()
        with _CACHE_LOCK:
            for stale in [k for k, (ts, _) in _CACHE.items() if now - ts >= CACHE_TTL_SECONDS]:
                _CACHE.pop(stale, None)
                _KEY_LOCKS.pop(stale, None)
            _CACHE[key] = (now, value)
    return {**value, "cache": {"hit": False, "age_seconds": 0.0}}


def logs_client(workspace: WorkspaceContext) -> Any:
    return workspace.client("logs")


def cw_client(workspace: WorkspaceContext) -> Any:
    return workspace.client("cloudwatch")


# ── Logs Insights runner ────────────────────────────────────────────────────


def _start_query(
    logs: Any, query: str, start: int, end: int,
    log_groups: list[str] | None = None,
) -> str | None:
    """Returns the query id, or None when the target log group doesn't exist
    (fresh account before any agent traffic) — callers degrade to empty rows."""
    if log_groups:
        target: dict[str, Any] = {"logGroupNames": log_groups}
    elif query.lstrip().startswith("SOURCE "):
        target = {}
    else:
        target = {"logGroupName": SPANS_LOG_GROUP}
    for attempt in (0, 1):
        try:
            return logs.start_query(
                **target,
                startTime=start,
                endTime=end,
                queryString=query,
            )["queryId"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                return None
            if attempt == 0 and code in ("ThrottlingException", "LimitExceededException"):
                # One bounded backoff prevents an immediate repeat throttle.
                time.sleep(1.5)  # nosemgrep: arbitrary-sleep
                continue
            raise AppError(
                "observability.query_failed",
                f"Logs Insights start_query failed: {code}",
                status_code=502,
            ) from exc
    raise AppError("observability.query_failed", "unreachable", status_code=502)


def run_insights_queries(
    queries: dict[str, str], hours: int, logs: Any = None,
    log_groups: list[str] | None = None,
    workspace: WorkspaceContext | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Start all queries concurrently, poll each to completion, flatten rows.

    Queries run in the workspace's region — CloudWatch Logs is regional, so a
    caller must supply either a ``logs`` client or the ``workspace`` to build one
    from.
    """
    if logs is None:
        if workspace is None:
            raise ValueError("run_insights_queries needs either logs or workspace")
        logs = logs_client(workspace)
    end = int(_now())
    start = end - hours * 3600
    query_ids = {
        name: _start_query(logs, q, start, end, log_groups=log_groups)
        for name, q in queries.items()
    }
    results: dict[str, list[dict[str, str]]] = {}
    deadline = time.time() + QUERY_DEADLINE_SECONDS
    for name, qid in query_ids.items():
        if qid is None:  # log group missing — empty view, not an error
            results[name] = []
            continue
        while True:
            try:
                res = logs.get_query_results(queryId=qid)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                raise AppError(
                    "observability.query_failed",
                    f"Logs Insights polling failed: {code}",
                    status_code=502,
                ) from exc
            status = res["status"]
            if status == "Complete":
                results[name] = [
                    {f["field"]: f["value"] for f in row if f["field"] != "@ptr"}
                    for row in res["results"]
                ]
                break
            if status in ("Failed", "Cancelled", "Timeout"):
                raise AppError(
                    "observability.query_failed",
                    f"Logs Insights query '{name}' ended with status {status}",
                    status_code=502,
                )
            if time.time() > deadline:
                try:
                    logs.stop_query(queryId=qid)  # don't keep billing a lost query
                except ClientError:
                    pass
                raise AppError(
                    "observability.query_failed",
                    f"Logs Insights query '{name}' timed out after "
                    f"{QUERY_DEADLINE_SECONDS}s",
                    status_code=502,
                )
            # Deliberate query polling interval; max attempts bound the wait.
            time.sleep(0.8)  # nosemgrep: arbitrary-sleep
    return results


# ── Query builders (shapes validated against live aws/spans data) ──────────


# Aggregations restrict token sums/LLM counts to one token-bearing span:
# (a) agent-level spans (operation.name=invoke_agent) repeat their children's
# gen_ai.usage.* values, and (b) the Strands SDK emits each LLM call twice —
# a framework wrapper span (gen_ai.system=strands-agents) plus the terminal
# provider span (gen_ai.system=aws.bedrock) with identical token counts (both
# verified against live data; naive sums count 2-3x). Native Claude SDK spans
# instead put the aggregate usage on their OpenInference AGENT root.
_MODEL_FIELD = "coalesce(attributes.gen_ai.request.model, attributes.llm.model_name)"
_INPUT_TOKENS_FIELD = (
    "coalesce(attributes.gen_ai.usage.input_tokens, attributes.llm.token_count.prompt)"
)
_OUTPUT_TOKENS_FIELD = (
    "coalesce(attributes.gen_ai.usage.output_tokens, attributes.llm.token_count.completion)"
)
_CACHE_READ_FIELD = (
    "coalesce(attributes.gen_ai.usage.cache_read_input_tokens, "
    "attributes.llm.token_count.prompt_details.cache_read)"
)
_CACHE_WRITE_FIELD = (
    "coalesce(attributes.gen_ai.usage.cache_write_input_tokens, "
    "attributes.llm.token_count.prompt_details.cache_write)"
)
_NATIVE_AGENT_FIELD = (
    'strcontains(coalesce(attributes.openinference.span.kind, ""), "AGENT")'
)
_OPERATION_FIELD = 'coalesce(attributes.gen_ai.operation.name, "")'

# The `x * is_llm` multiplication is Logs Insights' conditional sum.
_IS_LLM_FIELDS = f"""fields {_MODEL_FIELD} as telemetry_model,
       ((strcontains({_OPERATION_FIELD}, "chat")
        + strcontains({_OPERATION_FIELD}, "text_completion")
        + strcontains({_OPERATION_FIELD}, "generate_content"))
        * (1 - strcontains(coalesce(attributes.gen_ai.system, ""), "strands-agents"))
        * (1 - {_NATIVE_AGENT_FIELD})
        + {_NATIVE_AGENT_FIELD}) as is_llm,
       strcontains(status.code, "ERROR") as is_error
| fields {_INPUT_TOKENS_FIELD} * is_llm as llm_in,
         {_OUTPUT_TOKENS_FIELD} * is_llm as llm_out,
         {_CACHE_READ_FIELD} * is_llm as llm_cache_read,
         {_CACHE_WRITE_FIELD} * is_llm as llm_cache_write"""


def q_trace_aggregates(session_id: str | None = None, limit: int = TRACE_LIMIT) -> str:
    if session_id is not None:
        _require(SESSION_ID_RE, session_id, "session id")
    filters = ["ispresent(startTimeUnixNano)"]
    if session_id:
        filters.append(f'attributes.session.id = "{session_id}"')
    return f"""
{SPANS_SOURCE}
| filter {" and ".join(filters)}
| {_IS_LLM_FIELDS}
| stats count(*) as span_count, sum(is_llm) as llm_count,
        sum(llm_in) as tokens_in,
        sum(llm_out) as tokens_out,
        sum(llm_cache_read) as cache_read,
        sum(llm_cache_write) as cache_write,
        sum(is_error) as error_count,
        min(startTimeUnixNano) as start_ns, max(endTimeUnixNano) as end_ns,
        latest(attributes.session.id) as session_id,
        latest(resource.attributes.service.name) as service,
        latest(telemetry_model) as model,
        count_distinct(telemetry_model) as model_count
  by traceId
| sort start_ns desc
| limit {limit}
"""


def q_root_spans(limit: int = 3 * TRACE_LIMIT) -> str:
    # No session variant: root spans don't reliably carry session.id, so session
    # views join roots by traceId against the session-filtered aggregates.
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and not ispresent(parentSpanId)
| fields name, traceId, resource.attributes.service.name as service,
         durationNano, startTimeUnixNano, status.code as status_code
| sort startTimeUnixNano desc
| limit {limit}
"""


def q_session_aggregates(limit: int = SESSION_LIMIT) -> str:
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| {_IS_LLM_FIELDS}
| stats count_distinct(traceId) as traces, sum(is_llm) as llm_calls,
        sum(llm_in) as tokens_in,
        sum(llm_out) as tokens_out,
        sum(is_error) as errors,
        min(startTimeUnixNano) as first_ns, max(endTimeUnixNano) as last_ns,
        latest(resource.attributes.service.name) as service,
        latest(telemetry_model) as model
  by attributes.session.id as session_id
| sort last_ns desc
| limit {limit}
"""


def q_dashboard_series(range_key: str) -> str:
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and not ispresent(parentSpanId)
| fields strcontains(status.code, "ERROR") as is_error
| stats count(*) as traces, sum(is_error) as errors,
        pct(durationNano, 50) as p50_nano, pct(durationNano, 95) as p95_nano
  by bin({BIN_BY_RANGE[range_key]}) as bucket
| sort bucket asc
| limit 200
"""


def q_dashboard_totals() -> str:
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and not ispresent(parentSpanId)
| fields strcontains(status.code, "ERROR") as is_error
| stats count(*) as traces, sum(is_error) as errors,
        pct(durationNano, 50) as p50_nano, pct(durationNano, 95) as p95_nano
"""


def q_dashboard_distincts() -> str:
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| stats count_distinct(attributes.session.id) as sessions,
        count_distinct(resource.attributes.service.name) as agents
"""


def q_top_tools(limit: int = 10) -> str:
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano)
    and (ispresent(attributes.gen_ai.tool.name) or ispresent(attributes.tool.name))
| fields coalesce(attributes.gen_ai.tool.name, attributes.tool.name) as tool,
         strcontains(status.code, "ERROR") as is_error
| stats count(*) as calls, sum(is_error) as errors
  by tool
| sort calls desc
| limit {limit}
"""


def q_tokens_by_model(limit: int = 25) -> str:
    """Per-model token sums from spans — the one source every agent kind feeds.

    Sums provider (non-wrapper) LLM spans, but also carries the Strands wrapper
    sums per model: non-Bedrock providers (e.g. Mantle ``openai.*`` models) emit
    the wrapper span ONLY, so a model whose provider sums are zero falls back to
    its wrapper sums in ``_tokens_by_model_rows`` — the group-by mirror of the
    per-trace "prefer non-wrapper, fall back to wrapper" rollup rule.
    """
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano)
    and (ispresent(attributes.gen_ai.request.model) or ispresent(attributes.llm.model_name))
| fields {_MODEL_FIELD} as telemetry_model,
       ((strcontains({_OPERATION_FIELD}, "chat")
        + strcontains({_OPERATION_FIELD}, "text_completion")
        + strcontains({_OPERATION_FIELD}, "generate_content")
        ) * (1 - {_NATIVE_AGENT_FIELD})
        + {_NATIVE_AGENT_FIELD}) as is_call,
       strcontains(coalesce(attributes.gen_ai.system, ""), "strands-agents") as is_wrapper
| fields {_INPUT_TOKENS_FIELD} * is_call * (1 - is_wrapper) as llm_in,
         {_OUTPUT_TOKENS_FIELD} * is_call * (1 - is_wrapper) as llm_out,
         {_INPUT_TOKENS_FIELD} * is_call * is_wrapper as wrap_in,
         {_OUTPUT_TOKENS_FIELD} * is_call * is_wrapper as wrap_out
| stats sum(llm_in) as tokens_in, sum(llm_out) as tokens_out,
        sum(wrap_in) as wrapper_in, sum(wrap_out) as wrapper_out
  by telemetry_model as model
| limit {limit}
"""


def _tokens_by_model_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Project q_tokens_by_model rows into the tokens_by_model response shape."""
    prices = get_settings().model_prices
    out: list[dict[str, Any]] = []
    for row in rows:
        tokens_in, tokens_out = _num(row, "tokens_in"), _num(row, "tokens_out")
        if not (tokens_in or tokens_out):  # wrapper-only provider (see builder)
            tokens_in, tokens_out = _num(row, "wrapper_in"), _num(row, "wrapper_out")
        if not (tokens_in or tokens_out):
            continue
        model = row.get("model") or "unknown"
        out.append(
            {
                "model": model,
                "input": round(tokens_in),
                "output": round(tokens_out),
                "total": round(tokens_in + tokens_out),
                "est_cost_usd": estimate_cost(model, tokens_in, tokens_out, prices=prices),
            }
        )
    out.sort(key=lambda r: -r["total"])
    return out


def q_trace_spans(trace_id: str) -> str:
    _require(TRACE_ID_RE, trace_id, "trace id")
    return f"""
{SPANS_SOURCE}
| filter traceId = "{trace_id}" and ispresent(startTimeUnixNano)
| fields @message
| limit {SPANS_PER_TRACE}
"""


def q_trace_message_events(trace_id: str) -> str:
    # Runs against the agents' runtime log groups (otel-rt-logs), where the
    # SDKs emit gen_ai message events (prompts/completions) as OTel log
    # records correlated to spans via traceId/spanId.
    _require(TRACE_ID_RE, trace_id, "trace id")
    return f"""
filter traceId = "{trace_id}"
| fields @message, spanId
| limit 200
"""


# ── Row helpers ─────────────────────────────────────────────────────────────


def _num(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def _ns_to_iso(ns: float | None) -> str | None:
    if not ns:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat(timespec="seconds")


# ── Cost estimator ──────────────────────────────────────────────────────────


def match_price(model: str | None, prices: dict[str, Any]) -> dict[str, float] | None:
    """Longest price-map key that is a substring of the model id."""
    if not model:
        return None
    best: tuple[int, dict[str, float]] | None = None
    for key, entry in prices.items():
        if key in model and isinstance(entry, dict):
            if best is None or len(key) > best[0]:
                best = (len(key), entry)
    return best[1] if best else None


def estimate_cost(
    model: str | None,
    tokens_in: float,
    tokens_out: float,
    cache_read: float = 0.0,
    cache_write: float = 0.0,
    prices: dict[str, Any] | None = None,
) -> float | None:
    prices = get_settings().model_prices if prices is None else prices
    entry = match_price(model, prices)
    if not entry:
        return None
    rate_in = float(entry.get("input", 0.0))
    rate_out = float(entry.get("output", 0.0))
    rate_cache_read = float(entry.get("cache_read", rate_in * CACHE_READ_FACTOR))
    rate_cache_write = float(entry.get("cache_write", rate_in * CACHE_WRITE_FACTOR))
    cost = (
        tokens_in * rate_in
        + tokens_out * rate_out
        + cache_read * rate_cache_read
        + cache_write * rate_cache_write
    ) / 1e6
    return round(cost, 6)


# ── Category mapper (mockup contract: llm/tool/memory/gateway/http/agent) ──

_MEMORY_NEEDLES = (
    "createevent", "listevents", "retrievememoryrecords", "listmemoryrecords",
    "listactors", "listsessions", "memory",
)
_GATEWAY_NEEDLES = ("tools/call", "getresourceoauth2token", "mcp", "gateway",
                    "oauth", "authorizeaction")
_AGENT_NEEDLES = ("invoke_agent", "event_loop", "invoke_harness", "agent")
_LLM_OPS = ("chat", "text_completion", "generate_content")


def _attr(attrs: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = attrs.get(name)
        if value is not None and value != "":
            return value
    return None


def _openinference_kind(attrs: dict[str, Any]) -> str:
    return str(attrs.get("openinference.span.kind") or "").upper()


def categorize_span(name: str, attributes: dict[str, Any] | None = None,
                    kind: str | None = None) -> str:
    """Order matters: strong signals (execute_tool prefix, operation.name)
    before substring needles, so e.g. `execute_tool search_memory` is a tool
    and an LLM chat span whose name mentions "agent" still counts as llm."""
    attrs = attributes or {}
    lowered = name.lower()
    operation = str(attrs.get("gen_ai.operation.name", ""))
    openinference_kind = _openinference_kind(attrs)
    if (
        lowered.startswith("execute_tool")
        or operation == "execute_tool"
        or openinference_kind == "TOOL"
    ):
        return "tool"
    if openinference_kind == "AGENT":
        return "agent"
    if operation in _LLM_OPS:
        return "llm"
    if operation == "invoke_agent":
        return "agent"
    if any(n in lowered for n in _GATEWAY_NEEDLES):
        return "gateway"
    if any(n in lowered for n in _MEMORY_NEEDLES):
        return "memory"
    if "gen_ai.tool.name" in attrs or "tool.name" in attrs:
        return "tool"
    if any(n in lowered for n in _AGENT_NEEDLES):
        return "agent"
    if lowered.startswith("chat") or "converse" in lowered or "invoke_model" in lowered:
        return "llm"
    if kind == "SERVER" or "http.method" in attrs or "http.request.method" in attrs:
        return "http"
    return "other"


# ── Agent-name mapper (service.name → platform agent display name) ─────────


def build_agent_resolver(
    db: Session, workspace_id: str
) -> Callable[[str | None], Agent | None]:
    """service.name (from a span) → the Agent row that owns that runtime.

    The transcript fallback needs the agent *id* (to build scoped memory actor
    candidates), so the matching lives here and `build_agent_mapper` is a thin
    display wrapper over it — one set of rules for both.

    Only this workspace's agents are candidates: the spans came from its region,
    and a resource-id match against another environment's row would label a trace
    with an agent that never produced it.
    """
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace_id, Agent.resource_id.isnot(None))
        .order_by(Agent.updated_at.asc())
        .all()
    )
    # Later (fresher) rows win; active rows win over deleted ones.
    candidates: list[tuple[str, Agent]] = []
    for agent in sorted(rows, key=lambda a: a.status == "active"):
        base = (agent.resource_id or "").rsplit("-", 1)[0]
        if base:
            candidates.append((base, agent))

    def resolve_service(service: str | None) -> Agent | None:
        if not service:
            return None
        service_base = service.split(".")[0]
        matched = None
        for base, agent in candidates:
            if service_base in (base, f"harness_{base}") or service_base.endswith(base):
                matched = agent
        return matched

    return resolve_service


def build_agent_mapper(db: Session, workspace_id: str) -> Callable[[str | None], str]:
    resolve = build_agent_resolver(db, workspace_id)

    def map_service(service: str | None) -> str:
        if not service:
            return "unknown"
        agent = resolve(service)
        return agent.name if agent is not None else service

    return map_service


# ── Span tree builder ───────────────────────────────────────────────────────


def _span_ns(span: dict[str, Any], key: str) -> float | None:
    value = span.get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _token_usage(attrs: dict[str, Any]) -> dict[str, float] | None:
    values = {
        "input": _attr(
            attrs,
            "gen_ai.usage.input_tokens",
            "llm.token_count.prompt",
        ),
        "output": _attr(
            attrs,
            "gen_ai.usage.output_tokens",
            "llm.token_count.completion",
        ),
        "cache_read": _attr(
            attrs,
            "gen_ai.usage.cache_read_input_tokens",
            "llm.token_count.prompt_details.cache_read",
        ),
        "cache_write": _attr(
            attrs,
            "gen_ai.usage.cache_write_input_tokens",
            "llm.token_count.prompt_details.cache_write",
        ),
    }
    if all(value is None for value in values.values()):
        return None
    return {key: float(value or 0) for key, value in values.items()}


def build_span_tree(raw_spans: list[dict[str, Any]],
                    prices: dict[str, Any] | None = None) -> dict[str, Any]:
    """Nest spans by parentSpanId; annotate offsets/durations as ms + percentages."""
    starts = [s for s in (_span_ns(sp, "startTimeUnixNano") for sp in raw_spans) if s]
    ends = [e for e in (_span_ns(sp, "endTimeUnixNano") for sp in raw_spans) if e]
    trace_start = min(starts) if starts else 0.0
    trace_end = max(ends) if ends else trace_start
    total_ns = max(trace_end - trace_start, 1.0)

    flat: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    for span in raw_spans:
        attrs = span.get("attributes") or {}
        name = str(span.get("name") or "span")
        kind = span.get("kind")
        start = _span_ns(span, "startTimeUnixNano")
        end = _span_ns(span, "endTimeUnixNano")
        duration_ns = (end - start) if start and end else _span_ns(span, "durationNano") or 0.0
        offset_ns = (start - trace_start) if start else 0.0
        tokens = _token_usage(attrs)
        model = _attr(attrs, "gen_ai.request.model", "llm.model_name")
        finish = attrs.get("gen_ai.response.finish_reasons") or attrs.get(
            "gen_ai.response.finish_reason"
        )
        row = {
            "span_id": span.get("spanId"),
            "parent_span_id": span.get("parentSpanId"),
            "name": name[:120],
            "category": categorize_span(name, attrs, kind),
            "kind": kind,
            "status": (span.get("status") or {}).get("code", "UNSET"),
            "start_offset_ms": round(offset_ns / NANOS_PER_MS, 1),
            "duration_ms": round(duration_ns / NANOS_PER_MS, 1),
            "offset_pct": round(offset_ns / total_ns * 100, 2),
            "width_pct": round(duration_ns / total_ns * 100, 2),
            "model": model,
            "finish_reason": finish,
            "tool_name": _attr(attrs, "gen_ai.tool.name", "tool.name"),
            "tokens": tokens,
            "est_cost_usd": (
                estimate_cost(model, tokens["input"], tokens["output"],
                              tokens["cache_read"], tokens["cache_write"], prices=prices)
                if tokens and model else None
            ),
            "attributes": attrs,
        }
        flat.append(row)
        if row["span_id"]:
            nodes[row["span_id"]] = {**row, "children": [], "depth": 0}
            nodes[row["span_id"]].pop("attributes")

    roots: list[dict[str, Any]] = []
    for node in nodes.values():
        parent = nodes.get(node["parent_span_id"] or "")
        if parent is not None and parent is not node:
            parent["children"].append(node)
        else:
            roots.append(node)

    def annotate(node: dict[str, Any], depth: int) -> None:
        node["depth"] = depth
        node["children"].sort(key=lambda c: c["start_offset_ms"])
        for child in node["children"]:
            annotate(child, depth + 1)

    roots.sort(key=lambda r: r["start_offset_ms"])
    for root in roots:
        annotate(root, 0)
    flat.sort(key=lambda r: r["start_offset_ms"])
    return {
        "start": _ns_to_iso(trace_start),
        "duration_ms": round(total_ns / NANOS_PER_MS, 1),
        "tree": roots,
        "spans": flat,
    }


# ── gen_ai message events (prompts / completions per span) ─────────────────

MESSAGE_TEXT_CAP = 2000
MESSAGES_PER_SIDE = 20
BLOCKS_PER_MESSAGE = 20


def _parse_content_blocks(raw: Any) -> list[dict[str, Any]]:
    """Message content arrives as a JSON string of content blocks
    ([{"text"}, {"toolUse"}, {"toolResult"}]); normalize + truncate."""
    if not isinstance(raw, str):
        return []
    try:
        blocks = json.loads(raw)
    except ValueError:
        return [{"type": "text", "text": raw[:MESSAGE_TEXT_CAP]}]
    if not isinstance(blocks, list):
        blocks = [blocks]
    out: list[dict[str, Any]] = []
    for block in blocks[:BLOCKS_PER_MESSAGE]:
        if not isinstance(block, dict):
            out.append({"type": "text", "text": str(block)[:MESSAGE_TEXT_CAP]})
        elif "text" in block:
            out.append({"type": "text", "text": str(block["text"])[:MESSAGE_TEXT_CAP]})
        elif "toolUse" in block:
            use = block["toolUse"] or {}
            out.append({
                "type": "tool_use",
                "name": use.get("name"),
                "input": json.dumps(use.get("input"), ensure_ascii=False)[:500],
            })
        elif "toolResult" in block:
            result = block["toolResult"] or {}
            out.append({
                "type": "tool_result",
                "status": result.get("status"),
                "text": json.dumps(result.get("content"), ensure_ascii=False)[:500],
            })
        else:
            out.append({"type": "other",
                        "text": json.dumps(block, ensure_ascii=False)[:300]})
    return out


def _normalize_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    out = []
    for message in messages[:MESSAGES_PER_SIDE]:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        finish = None
        if isinstance(content, str):  # some scopes emit the payload directly
            blocks = _parse_content_blocks(content)
        elif isinstance(content, dict):
            blocks = _parse_content_blocks(
                content.get("content") or content.get("message")
            )
            finish = content.get("finish_reason")
        else:
            continue
        if not blocks:
            continue
        entry: dict[str, Any] = {"role": message.get("role"), "blocks": blocks}
        if finish:
            entry["finish_reason"] = finish
        out.append(entry)
    return out


def parse_message_events(rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """spanId → {input: [...], output: [...]} from runtime OTel log records."""
    by_span: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            record = json.loads(row.get("@message", ""))
            span_id = record.get("spanId")
            body = record.get("body")
            if not span_id or not isinstance(body, dict):
                continue
            entry = by_span.setdefault(span_id, {})
            for side in ("input", "output"):
                payload = body.get(side)
                if not entry.get(side) and isinstance(payload, dict):
                    normalized = _normalize_messages(payload.get("messages"))
                    if normalized:
                        entry[side] = normalized
        except (TypeError, ValueError, AttributeError):
            continue  # malformed record — message events are best-effort
        if len(by_span) >= 50:
            break
    return {k: v for k, v in by_span.items() if v}


def _span_log_groups(raw_spans: list[dict[str, Any]]) -> list[str]:
    groups: set[str] = set()
    for span in raw_spans:
        names = ((span.get("resource") or {}).get("attributes") or {}).get(
            "aws.log.group.names"
        )
        if isinstance(names, str):
            groups.update(g.strip() for g in names.split(",") if g.strip())
    return sorted(groups)[:5]


# ── Metrics (bedrock-agentcore namespace) ───────────────────────────────────


def query_token_usage_metrics(
    hours: int, workspace: WorkspaceContext, cw: Any = None
) -> list[dict[str, Any]]:
    """gen_ai.client.token.usage summed over the range, grouped by model.

    Fallback source only: verified live (us-east-1 + us-west-2, 2026-08-11)
    that just managed-Harness runtimes publish this CloudWatch metric — zip/
    Strands runtimes emit spans but no metric, so span aggregation
    (q_tokens_by_model) is the primary tokens-by-model source.
    """
    cw = cw or cw_client(workspace)
    metrics: list[dict[str, Any]] = []
    for page in cw.get_paginator("list_metrics").paginate(
        Namespace="bedrock-agentcore", MetricName="gen_ai.client.token.usage"
    ):
        metrics.extend(page["Metrics"])
    if not metrics:
        return []
    period = max(hours * 3600, 60)
    queries, keys = [], []
    for i, metric in enumerate(metrics[:100]):
        dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        queries.append(
            {
                "Id": f"m{i}",
                "MetricStat": {"Metric": metric, "Period": period, "Stat": "Sum"},
                "ReturnData": True,
            }
        )
        keys.append((dims.get("gen_ai.request.model", "unknown"),
                     dims.get("gen_ai.token.type", "input")))
    end = datetime.fromtimestamp(_now(), tz=UTC)
    start = datetime.fromtimestamp(_now() - hours * 3600, tz=UTC)
    response = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
    per_model: dict[str, dict[str, float]] = {}
    for result in response.get("MetricDataResults", []):
        index = int(result["Id"][1:])
        model, token_type = keys[index]
        total = sum(result.get("Values") or [])
        if total:
            per_model.setdefault(model, {})[token_type] = (
                per_model.get(model, {}).get(token_type, 0.0) + total
            )
    prices = get_settings().model_prices
    rows = []
    for model, usage in per_model.items():
        tokens_in, tokens_out = usage.get("input", 0.0), usage.get("output", 0.0)
        rows.append(
            {
                "model": model,
                "input": round(tokens_in),
                "output": round(tokens_out),
                "total": round(tokens_in + tokens_out),
                "est_cost_usd": estimate_cost(model, tokens_in, tokens_out, prices=prices),
            }
        )
    rows.sort(key=lambda r: -r["total"])
    return rows


# ── Row composers ───────────────────────────────────────────────────────────


def _trace_row(
    agg: dict[str, str],
    root: dict[str, str] | None,
    map_agent: Callable[[str | None], str],
) -> dict[str, Any]:
    start_ns, end_ns = _num(agg, "start_ns"), _num(agg, "end_ns")
    tokens = {
        "input": round(_num(agg, "tokens_in")),
        "output": round(_num(agg, "tokens_out")),
        "cache_read": round(_num(agg, "cache_read")),
        "cache_write": round(_num(agg, "cache_write")),
    }
    tokens["total"] = tokens["input"] + tokens["output"]
    model = agg.get("model") or None
    service = agg.get("service") or (root or {}).get("service")
    duration_ns = _num(root or {}, "durationNano") or (end_ns - start_ns)
    return {
        "trace_id": agg.get("traceId"),
        "time": _ns_to_iso(start_ns),
        "root_operation": (root or {}).get("name") or "trace",
        "service": service,
        "agent": map_agent(service),
        "session_id": agg.get("session_id"),
        "duration_ms": round(duration_ns / NANOS_PER_MS, 1),
        "span_count": int(_num(agg, "span_count")),
        "llm_count": int(_num(agg, "llm_count")),
        "error_count": int(_num(agg, "error_count")),
        "status": "error" if _num(agg, "error_count") > 0 else "ok",
        "model": model,
        "multi_model": _num(agg, "model_count") > 1,
        "tokens": tokens,
        "est_cost_usd": estimate_cost(
            model, tokens["input"], tokens["output"],
            tokens["cache_read"], tokens["cache_write"],
        ),
    }


def _session_row(row: dict[str, str], map_agent: Callable[[str | None], str],
                 platform_ids: set[str]) -> dict[str, Any]:
    tokens_in, tokens_out = _num(row, "tokens_in"), _num(row, "tokens_out")
    model = row.get("model") or None
    service = row.get("service")
    session_id = row.get("session_id") or ""
    return {
        "session_id": session_id,
        "service": service,
        "agent": map_agent(service),
        "traces": int(_num(row, "traces")),
        "llm_calls": int(_num(row, "llm_calls")),
        "errors": int(_num(row, "errors")),
        "tokens": {
            "input": round(tokens_in),
            "output": round(tokens_out),
            "total": round(tokens_in + tokens_out),
        },
        "est_cost_usd": estimate_cost(model, tokens_in, tokens_out),
        "first": _ns_to_iso(_num(row, "first_ns")),
        "last": _ns_to_iso(_num(row, "last_ns")),
        "platform": session_id in platform_ids,
    }


# ── Public views ────────────────────────────────────────────────────────────


def get_dashboard(range_key: str, workspace: WorkspaceContext, force: bool = False,
                  logs: Any = None, cw: Any = None) -> dict[str, Any]:
    hours = RANGE_HOURS[range_key]

    def build() -> dict[str, Any]:
        results = run_insights_queries(
            {
                "series": q_dashboard_series(range_key),
                "totals": q_dashboard_totals(),
                "distincts": q_dashboard_distincts(),
                "tools": q_top_tools(),
                "tokens_by_model": q_tokens_by_model(),
            },
            hours,
            logs=logs,
            workspace=workspace,
        )
        totals = results["totals"][0] if results["totals"] else {}
        distincts = results["distincts"][0] if results["distincts"] else {}
        trace_total = int(_num(totals, "traces"))
        errors = int(_num(totals, "errors"))
        # Spans are the primary source (every agent kind emits them); the
        # CloudWatch metric only covers managed-Harness runtimes and remains a
        # fallback for windows where the span aggregation comes back empty.
        tokens_by_model = _tokens_by_model_rows(results.get("tokens_by_model") or [])
        if not tokens_by_model:
            try:
                tokens_by_model = query_token_usage_metrics(hours, workspace, cw=cw)
            except ClientError:
                # Metrics are one tile/chart — a CloudWatch failure must not
                # take down the dashboard when the Logs Insights data succeeded.
                tokens_by_model = []
        tokens_in = sum(r["input"] for r in tokens_by_model)
        tokens_out = sum(r["output"] for r in tokens_by_model)
        costs = [r["est_cost_usd"] for r in tokens_by_model if r["est_cost_usd"] is not None]
        series = [
            {
                "bucket": row.get("bucket"),
                "traces": int(_num(row, "traces")),
                "errors": int(_num(row, "errors")),
                "p50_ms": round(_num(row, "p50_nano") / NANOS_PER_MS, 1),
                "p95_ms": round(_num(row, "p95_nano") / NANOS_PER_MS, 1),
            }
            for row in results["series"]
        ]
        tools = [
            {
                "tool": row.get("tool"),
                "calls": int(_num(row, "calls")),
                "errors": int(_num(row, "errors")),
                "success_rate": round(
                    (1 - _num(row, "errors") / _num(row, "calls")) * 100, 1
                ) if _num(row, "calls") else None,
            }
            for row in results["tools"]
        ]
        return {
            "range": range_key,
            "prices_meta": get_settings().model_prices_meta or None,
            "tiles": {
                "traces": {"total": trace_total, "ok": trace_total - errors, "error": errors},
                "sessions": {
                    "total": int(_num(distincts, "sessions")),
                    "agents": int(_num(distincts, "agents")),
                },
                "error_rate": round(errors / trace_total, 4) if trace_total else 0.0,
                "latency": {
                    "p50_ms": round(_num(totals, "p50_nano") / NANOS_PER_MS, 1),
                    "p95_ms": round(_num(totals, "p95_nano") / NANOS_PER_MS, 1),
                },
                "tokens": {
                    "input": tokens_in,
                    "output": tokens_out,
                    "total": tokens_in + tokens_out,
                    "est_cost_usd": round(sum(costs), 4) if costs else None,
                },
            },
            "series": series,
            "tokens_by_model": tokens_by_model,
            "top_tools": tools,
        }

    # Every cache key carries the workspace: the same range in two environments
    # is two different answers.
    return cached(f"dashboard:{workspace.id}:{range_key}", force, build)


def list_traces(range_key: str, db: Session, workspace: WorkspaceContext,
                force: bool = False, logs: Any = None) -> dict[str, Any]:
    hours = RANGE_HOURS[range_key]

    def build() -> dict[str, Any]:
        results = run_insights_queries(
            {"aggregates": q_trace_aggregates(), "roots": q_root_spans()},
            hours,
            logs=logs,
            workspace=workspace,
        )
        roots = {row.get("traceId"): row for row in results["roots"]}
        map_agent = build_agent_mapper(db, workspace.id)
        rows = [
            _trace_row(agg, roots.get(agg.get("traceId")), map_agent)
            for agg in results["aggregates"]
        ]
        return {"range": range_key, "traces": rows, "count": len(rows), "limit": TRACE_LIMIT}

    return cached(f"traces:{workspace.id}:{range_key}", force, build)


def get_trace(trace_id: str, range_key: str, db: Session, workspace: WorkspaceContext,
              force: bool = False, logs: Any = None) -> dict[str, Any]:
    hours = RANGE_HOURS[range_key]

    def build() -> dict[str, Any]:
        results = run_insights_queries(
            {"spans": q_trace_spans(trace_id)}, hours, logs=logs, workspace=workspace
        )
        raw_spans = []
        for row in results["spans"]:
            try:
                raw_spans.append(json.loads(row.get("@message", "")))
            except (TypeError, ValueError):
                continue
        tree = build_span_tree(raw_spans)
        spans = tree["spans"]
        # gen_ai message events (prompts/completions) live in the agents' own
        # runtime log groups, referenced by the spans themselves; best-effort.
        messages_by_span: dict[str, dict[str, Any]] = {}
        log_groups = _span_log_groups(raw_spans)
        if log_groups:
            try:
                event_rows = run_insights_queries(
                    {"events": q_trace_message_events(trace_id)},
                    hours, logs=logs, log_groups=log_groups, workspace=workspace,
                )["events"]
                messages_by_span = parse_message_events(event_rows)
            except Exception:  # best-effort enrichment — never fail the trace
                messages_by_span = {}
        for span in spans:
            span["messages"] = messages_by_span.get(span["span_id"] or "")
        # Strands emits each LLM call as a wrapper span (system=strands-agents)
        # plus a terminal provider span with identical tokens; native Claude
        # instead carries aggregate usage on its AGENT root. Sum those call
        # spans, preferring Strands terminal providers over wrappers.
        with_tokens = [
            s
            for s in spans
            if s["tokens"]
            and (
                s["category"] == "llm"
                or _openinference_kind(s["attributes"]) == "AGENT"
            )
        ]
        llm_spans = [
            s for s in with_tokens
            if s["attributes"].get("gen_ai.system") != "strands-agents"
        ] or with_tokens
        tokens = {
            "input": round(sum(s["tokens"]["input"] for s in llm_spans)),
            "output": round(sum(s["tokens"]["output"] for s in llm_spans)),
            "cache_read": round(sum(s["tokens"]["cache_read"] for s in llm_spans)),
            "cache_write": round(sum(s["tokens"]["cache_write"] for s in llm_spans)),
        }
        tokens["total"] = tokens["input"] + tokens["output"]
        costs = [s["est_cost_usd"] for s in llm_spans if s["est_cost_usd"] is not None]
        service = next(
            (
                (sp.get("resource") or {}).get("attributes", {}).get("service.name")
                for sp in raw_spans
                if (sp.get("resource") or {}).get("attributes", {}).get("service.name")
            ),
            None,
        )
        session_id = next(
            (s["attributes"].get("session.id") for s in spans
             if s["attributes"].get("session.id")),
            None,
        )
        map_agent = build_agent_mapper(db, workspace.id)
        root = tree["tree"][0] if tree["tree"] else None
        return {
            "trace_id": trace_id,
            "range": range_key,
            "meta": {
                "root_operation": root["name"] if root else None,
                "service": service,
                "agent": map_agent(service),
                "session_id": session_id,
                "start": tree["start"],
                "duration_ms": tree["duration_ms"],
                "span_count": len(spans),
                "llm_count": len(llm_spans),
                "status": "error" if any(s["status"] == "ERROR" for s in spans) else "ok",
                "tokens": tokens,
                "est_cost_usd": round(sum(costs), 6) if costs else None,
            },
            "tree": tree["tree"],
            "spans": spans,
        }

    return cached(f"trace:{workspace.id}:{trace_id}:{range_key}", force, build)


def list_sessions(range_key: str, db: Session, workspace: WorkspaceContext,
                  force: bool = False, logs: Any = None) -> dict[str, Any]:
    hours = RANGE_HOURS[range_key]
    workspace_id = workspace.id

    def build() -> dict[str, Any]:
        results = run_insights_queries(
            {"sessions": q_session_aggregates()}, hours, logs=logs, workspace=workspace
        )
        map_agent = build_agent_mapper(db, workspace_id)
        # "Is this one of our chat sessions" is answered per workspace: a bare
        # session_id match would claim another environment's session as ours.
        platform_ids = {
            row.session_id
            for row in db.query(ChatSession.session_id)
            .filter(ChatSession.workspace_id == workspace_id)
            .all()
        }
        rows = [_session_row(r, map_agent, platform_ids) for r in results["sessions"]]
        return {"range": range_key, "sessions": rows, "count": len(rows),
                "limit": SESSION_LIMIT}

    return cached(f"sessions:{workspace_id}:{range_key}", force, build)


def _agent_from_traces(
    db: Session, workspace_id: str, traces: list[dict[str, Any]]
) -> Agent | None:
    """The agent behind a session's spans — the only agent signal a session with
    no platform ledger row has."""
    resolve = build_agent_resolver(db, workspace_id)
    for trace in traces:
        agent = resolve(trace.get("service"))
        if agent is not None:
            return agent
    return None


def get_session(session_id: str, range_key: str, db: Session,
                workspace: WorkspaceContext, force: bool = False,
                logs: Any = None) -> dict[str, Any]:
    hours = RANGE_HOURS[range_key]

    def build() -> dict[str, Any]:
        results = run_insights_queries(
            {"aggregates": q_trace_aggregates(session_id=session_id), "roots": q_root_spans()},
            hours,
            logs=logs,
            workspace=workspace,
        )
        roots = {row.get("traceId"): row for row in results["roots"]}
        map_agent = build_agent_mapper(db, workspace.id)
        rows = [
            _trace_row(agg, roots.get(agg.get("traceId")), map_agent)
            for agg in results["aggregates"]
        ]
        tokens = {
            "input": sum(r["tokens"]["input"] for r in rows),
            "output": sum(r["tokens"]["output"] for r in rows),
        }
        tokens["total"] = tokens["input"] + tokens["output"]
        costs = [r["est_cost_usd"] for r in rows if r["est_cost_usd"] is not None]
        times = [r["time"] for r in rows if r["time"]]
        return {
            "session_id": session_id,
            "range": range_key,
            "summary": {
                "agent": rows[0]["agent"] if rows else None,
                "traces": len(rows),
                "llm_calls": sum(r["llm_count"] for r in rows),
                "errors": sum(r["error_count"] for r in rows),
                "tokens": tokens,
                "est_cost_usd": round(sum(costs), 6) if costs else None,
                "first": min(times) if times else None,
                "last": max(times) if times else None,
            },
            "traces": rows,
        }

    payload = cached(f"session:{workspace.id}:{session_id}:{range_key}", force, build)
    # Transcript is attached outside the cache: memory errors must degrade to
    # {available: false} on every request, never poison the cached span data.
    # It runs AFTER the trace query because sessions with no ledger row carry
    # their only agent signal in the spans (service.name → Agent).
    transcript = session_transcript(
        db,
        session_id,
        workspace,
        agent=_agent_from_traces(db, workspace.id, payload["traces"]),
    )
    return {**payload, "transcript": transcript}


# ── Memory transcript (platform sessions only) ──────────────────────────────


def _turn_text(raw: str) -> str | None:
    """Extract display text from a memory event.

    Harness agents persist whole message envelopes as the event text
    ({"message": {"role", "content": [{"text"|"toolUse"|"toolResult"...}]}});
    platform-written events are already plain text. Tool-only turns (no text
    parts) return None and are dropped from the transcript.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return raw
    try:
        envelope = json.loads(text)
    except ValueError:
        return raw
    content = (envelope.get("message") or {}).get("content")
    if not isinstance(content, list):
        return raw
    parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")]
    return "\n".join(parts) if parts else None


def _event_iso(value: Any) -> str:
    """boto3 event timestamps are tz-aware datetimes in the SERVER's local tz;
    normalize to UTC ISO so the frontend can render in the browser's tz."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return str(value or "")


def _chat_ledger_turns(
    db: Session, workspace_id: str, agent_id: str, session_id: str
) -> list[dict[str, Any]]:
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.workspace_id == workspace_id,
            ChatMessage.agent_id == agent_id,
            ChatMessage.session_id == session_id,
            ChatMessage.role.in_(("user", "agent")),
        )
        .order_by(ChatMessage.id.asc())
        .limit(500)
        .all()
    )
    return [
        {
            "role": "USER" if message.role == "user" else "ASSISTANT",
            "text": message.text[:4000],
            "at": _event_iso(message.created_at),
        }
        for message in rows
    ]


def _eval_run_for_session(
    db: Session, workspace_id: str, session_id: str
) -> EvalRun | None:
    """The eval run that PRODUCED this session, if any. Insights re-runs reuse
    an earlier run's session_ids, so the oldest match is the creator (its
    created_at anchors the content-log time window). session_ids is a JSON
    column, so membership is checked in Python over recent runs (small table)."""
    rows = (
        db.query(EvalRun)
        .filter(EvalRun.workspace_id == workspace_id)
        .order_by(EvalRun.created_at.desc())
        .limit(500)
        .all()
    )
    matches = [r for r in rows if session_id in (r.session_ids or [])]
    return matches[-1] if matches else None  # desc scan → last match is the oldest


# ── Eval transcript from OTEL content logs (runtime-backed agents) ───────────
# Runtime methods write no memory events during eval runs, but their ADOT
# sidecar streams per-span gen_ai content records into the runtime log group
# (stream otel-rt-logs) — the same content StartBatchEvaluation reads.
RUNTIME_LOG_METHODS = {"zip_runtime", "studio", "container"}


def _part_list_text(raw: Any) -> str | None:
    """Content strings in otel-rt-logs records are polymorphic: plain text or
    a JSON-encoded list of parts ([{"text"}|{"toolUse"}|{"toolResult"}]).
    Returns the joined text parts — None for tool-only content."""
    if not isinstance(raw, str) or not raw:
        return None
    s = raw.strip()
    if s.startswith("["):
        try:
            parts = json.loads(s)
        except ValueError:
            return raw
        if isinstance(parts, list):
            texts = [
                p.get("text") for p in parts if isinstance(p, dict) and p.get("text")
            ]
            return "\n".join(texts) if texts else None
    return raw


def _iter_body_messages(body: dict[str, Any]):
    """(kind, role, text, finish_reason) for each message of a content record.

    A message's ``content`` is either the raw string or a wrapper dict whose
    text sits under ``message`` (outputs) / ``content`` (inputs)."""
    for kind in ("input", "output"):
        for msg in (body.get(kind) or {}).get("messages") or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            finish = None
            if isinstance(content, dict):
                finish = content.get("finish_reason")
                raw = content.get("message") if "message" in content else content.get("content")
            else:
                raw = content
            yield kind, msg.get("role"), _part_list_text(raw), finish


def _ns_iso(ns: int) -> str:
    return datetime.fromtimestamp((ns or 0) / 1e9, UTC).isoformat(timespec="seconds")


def eval_turns_from_content_logs(
    log_group: str,
    session_id: str,
    started_at: datetime | None,
    workspace: WorkspaceContext,
    logs: Any = None,
) -> list[dict[str, Any]]:
    """USER/ASSISTANT turns for an eval session, rebuilt from content logs.

    One invocation = one traceId. Its USER turn is the trace's latest input
    user message (later records carry the full history — last one is the
    current turn); its ASSISTANT turn is the ``end_turn`` output, falling back
    to the last assistant text. filter_log_events scans oldest-first, so
    startTime (the run's creation time) is load-bearing — without it the scan
    exhausts its page budget on old log data and returns nothing.
    """
    logs = logs or logs_client(workspace)
    started = started_at or datetime.now(UTC)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    start_ms = int((started - timedelta(minutes=10)).timestamp() * 1000)
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "filterPattern": f'"{session_id}"',
        "startTime": start_ms,
        "endTime": start_ms + 26 * 3600 * 1000,  # runs settle well within a day
        "limit": 500,
    }
    records: list[dict[str, Any]] = []
    for _ in range(5):  # page cap — a session yields tens of records
        try:
            resp = logs.filter_log_events(**kwargs)
        except ClientError:
            return []
        for event in resp.get("events", []):
            try:
                rec = json.loads(event["message"])
            except ValueError:
                continue
            if (rec.get("attributes") or {}).get("session.id") == session_id:
                records.append(rec)
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token

    invocations: dict[str, dict[str, Any]] = {}
    for rec in sorted(records, key=lambda r: r.get("timeUnixNano") or 0):
        ts = rec.get("timeUnixNano") or 0
        inv = invocations.setdefault(rec.get("traceId") or "?", {"ts": ts})
        for kind, role, text, finish in _iter_body_messages(rec.get("body") or {}):
            if text is None:
                continue
            if kind == "input" and role == "user":
                inv["user"] = text
            elif kind == "output" and role == "assistant":
                if finish == "end_turn":
                    inv["final"], inv["final_ts"] = text, ts
                else:
                    inv.setdefault("final_ts", ts)
                    inv["last_assistant"] = text

    turns: list[dict[str, Any]] = []
    for inv in sorted(invocations.values(), key=lambda i: i["ts"]):
        if inv.get("user"):
            turns.append({"role": "USER", "text": inv["user"][:4000], "at": _ns_iso(inv["ts"])})
        answer = inv.get("final") or inv.get("last_assistant")
        if answer:
            turns.append({
                "role": "ASSISTANT",
                "text": answer[:4000],
                "at": _ns_iso(inv.get("final_ts") or inv["ts"]),
            })
    return turns


def _turns_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Conversational turns from memory events, oldest first.

    Tool-only turns (no text part) are dropped — see `_turn_text`.
    """
    turns = []
    for event in sorted(events, key=lambda e: str(e.get("eventTimestamp", ""))):
        for part in event.get("payload", []):
            conv = part.get("conversational")
            if not conv:
                continue
            text = _turn_text(conv.get("content", {}).get("text", ""))
            if text is None:
                continue  # tool-use/tool-result turn — not conversational display
            turns.append(
                {
                    "role": conv.get("role"),
                    "text": text[:4000],
                    "at": _event_iso(event.get("eventTimestamp")),
                }
            )
    return turns


NOT_PLATFORM_SESSION = {"available": False, "reason": "not_platform_session"}


def _experiment_for_session(
    db: Session, workspace_id: str, session_id: str
) -> Experiment | None:
    """The experiment whose gateway traffic minted this session id, if still
    recorded. Stepwise re-runs OVERWRITE artifacts["traffic"], so a miss means
    "not attributable", never "not experiment traffic" — labeling only."""
    rows = (
        db.query(Experiment)
        .filter(Experiment.workspace_id == workspace_id)
        .order_by(Experiment.created_at.desc())
        .limit(200)
        .all()
    )
    for experiment in rows:
        traffic = (experiment.artifacts or {}).get("traffic")
        if isinstance(traffic, dict) and session_id in (traffic.get("session_ids") or []):
            return experiment
    return None


def _external_transcript(
    db: Session, session_id: str, workspace: WorkspaceContext, agent: Agent | None
) -> dict[str, Any]:
    """Transcript for a session that is in NO platform ledger — experiment
    gateway traffic, a `/v1` caller, or any other direct runtime invoke.

    Memory has no "which actor owns this session" lookup, so probe a bounded
    candidate set: the traced agent's scoped actors first (unambiguous), then
    the bare "default" actor that the gateway→runtime hop leaves in place.
    """
    try:
        candidates = (
            memory.list_actor_ids(workspace, prefix=f"{agent.id}{memory.SCOPE_SEP}")
            if agent is not None
            else []
        )
        candidates.append("default")
        for actor_id in candidates:
            turns = _turns_from_events(
                memory.list_events(workspace, actor_id, session_id, 100)
            )
            if not turns:
                continue
            experiment = _experiment_for_session(db, workspace.id, session_id)
            return {
                "available": True,
                "actor_id": actor_id,
                "agent_id": agent.id if agent else None,
                "agent_name": agent.name if agent else None,
                "source": "experiment" if experiment else "external",
                "origin": "memory",
                "run_id": None,
                "experiment_id": experiment.id if experiment else None,
                "experiment_name": experiment.name if experiment else None,
                "turns": turns,
                # Long-term namespaces are keyed on the actor alone, and these
                # actors are shared across agents/runs — the count would say
                # nothing about this session (same reason eval skips it).
                "long_term_records": None,
            }
    except Exception:
        # Memory unreachable / not bootstrapped: this session was already
        # unattributable, so degrade to the plain empty state.
        return dict(NOT_PLATFORM_SESSION)
    return dict(NOT_PLATFORM_SESSION)


def session_transcript(
    db: Session,
    session_id: str,
    workspace: WorkspaceContext,
    agent: Agent | None = None,
) -> dict[str, Any]:
    row = (
        db.query(ChatSession)
        .filter(
            ChatSession.workspace_id == workspace.id,
            ChatSession.session_id == session_id,
        )
        .first()
    )
    run = None if row else _eval_run_for_session(db, workspace.id, session_id)
    if row is None and run is None:
        # Not chat, not eval — try memory directly (`agent` comes from the
        # session's traces, which is the only agent signal such sessions carry).
        return _external_transcript(db, session_id, workspace, agent)
    if row is not None:
        agent = db.get(Agent, row.agent_id)
        # Memory is written under an agent-scoped actor (memory.scoped_actor);
        # the ledger stores the bare human actor, so re-scope for the read.
        mem_actor = memory.scoped_actor(row.agent_id, row.actor_id)
        agent_id, actor_display = row.agent_id, row.actor_id
    else:
        # Eval-run sessions: invokes pass the BARE "default" actor straight to
        # the runtime — harness runtimes persist the conversation under it
        # (runtime-backed agents write no memory events, so turns come back
        # empty for them).
        agent = db.get(Agent, run.agent_id)
        mem_actor = "default"
        agent_id, actor_display = run.agent_id, "default"
    memory_error = None
    try:
        events = memory.list_events(workspace, mem_actor, session_id, max_results=100)
    except Exception as exc:
        memory_error = exc
        events = []  # chat may fall back to its ledger; eval may use content logs
    turns = _turns_from_events(events)
    origin = "memory"
    if row is not None:
        ledger_turns = _chat_ledger_turns(db, workspace.id, row.agent_id, session_id)
        memory_signature = [(turn["role"], turn["text"]) for turn in turns]
        ledger_signature = [(turn["role"], turn["text"]) for turn in ledger_turns]
        if ledger_turns and ledger_signature != memory_signature:
            # ChatMessage is the exact rendered conversation. Reconcile from it
            # when eventual consistency or actor drift leaves Memory incomplete.
            turns = ledger_turns
            origin = "ledger"
        elif memory_error is not None:
            return {
                "available": False,
                "reason": "memory_unavailable",
                "detail": f"{type(memory_error).__name__}: {memory_error}"[:200],
                "actor_id": actor_display,
            }

    # Runtime-backed agents write no memory events during eval runs — rebuild
    # the conversation from the runtime's OTEL content logs instead.
    if (
        not turns
        and run is not None
        and agent is not None
        and agent.method in RUNTIME_LOG_METHODS
        and agent.resource_id
    ):
        log_group = f"/aws/bedrock-agentcore/runtimes/{agent.resource_id}-DEFAULT"
        turns = eval_turns_from_content_logs(
            log_group, session_id, run.created_at, workspace
        )
        if turns:
            origin = "logs"
    # Long-term records only make sense for chat sessions — eval traffic all
    # shares the bare "default" actor, so its namespaces aggregate across
    # every agent's runs and say nothing about this session.
    long_term = None
    if row is not None:
        try:
            long_term = sum(
                len(memory.list_records(workspace, f"{ns}/{mem_actor}", max_results=20))
                for ns in ("/preferences", "/facts")
            )
        except Exception:
            pass
    return {
        "available": True,
        "actor_id": actor_display,
        "agent_id": agent_id,
        "agent_name": agent.name if agent else (run.agent_name if run else None),
        "source": "chat" if row else "eval",
        "origin": origin,
        "run_id": run.id if run else None,
        "turns": turns,
        "long_term_records": long_term,
    }
