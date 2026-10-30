"""Observability aggregations — Logs Insights over AgentCore telemetry + metrics.

Design contract: design/mockup-observability.html. Every view is served from a
60s TTL cache so the (slow, billed-per-scan) Logs Insights queries run at most
once a minute per (view, range). Tokens-by-model comes from the
`bedrock-agentcore` metrics namespace (gen_ai.client.token.usage, per the
mockup); top tools and all trace/session rollups cover both the legacy shared
aws/spans destination and unified per-runtime log groups.

Token counts are reported as-is; no USD cost is derived from them (R13 — cost
reporting returns with the budgets family, using admin-maintained prices).
"""

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import String, cast
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.services import memory_backend, memory_ownership, runtime_sessions, visibility
from app.services.strands_eval.redaction import redact_attributes
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext

logger = logging.getLogger("launchpad.observability")
RecordsGuard = Callable[[str, list[dict[str, Any]]], None]

SPANS_LOG_GROUP = "aws/spans"
RUNTIME_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"
SPANS_SOURCE = (
    "SOURCE logGroups(namePrefix: "
    f"['{SPANS_LOG_GROUP}', '{RUNTIME_LOG_GROUP_PREFIX}'])"
)


def _spans_source() -> str:
    """SOURCE clause for every Logs Insights query, narrowed by
    `Settings.observability_spans_source` (spec N5). Under the default "both" this
    returns the exact same string as the module constant `SPANS_SOURCE`, so every
    pre-existing test asserting against that constant stays true unchanged."""
    mode = get_settings().observability_spans_source
    if mode == "spans":
        prefixes = [SPANS_LOG_GROUP]
    elif mode == "runtime_logs":
        prefixes = [RUNTIME_LOG_GROUP_PREFIX]
    else:
        return SPANS_SOURCE
    quoted = ", ".join(f"'{p}'" for p in prefixes)
    return f"SOURCE logGroups(namePrefix: [{quoted}])"


RANGE_HOURS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
# SCORE NOW (N12 E1-5): one session, at most this many evaluators, in-process (CA-8).
MAX_ON_DEMAND_EVALUATORS = 5
BIN_BY_RANGE = {"1h": "5m", "6h": "15m", "24h": "1h", "7d": "6h"}
# List caps: the UI paginates client-side (50/100/200 per page), so fetch a
# deeper window per Logs Insights query; row counts this size are cheap.
TRACE_LIMIT = 500
ROOT_TRACE_CHUNK = 100  # trace ids per roots query (~3.5k chars of filter)
SESSION_LIMIT = 500
SPANS_PER_TRACE = 500
# On-demand scoring sends a whole session to Evaluate (model cap 20k spans);
# a bounded Logs Insights fetch keeps one call's payload and scan predictable.
SESSION_SPANS_LIMIT = 2000
CACHE_TTL_SECONDS = 60.0
QUERY_DEADLINE_SECONDS = 55
NANOS_PER_MS = 1_000_000

# The router enforces these shapes too; re-checked here (defense in depth)
# because the ids are interpolated into Logs Insights query strings.
# AgentCore only bounds runtimeSessionId by length (33..256, no alphabet), and
# external integrations compose ids such as `<ulid>#feishu#<chat_id>` — so the
# allowlist admits `#` `:` `.` `@` while still excluding the characters that
# would matter inside a double-quoted Logs Insights literal (`"`, `\`, `|`).
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-#:.@]{8,256}$")

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
# The UNCACHED prompt tokens in both conventions (N17-obs439, see `_token_usage`):
# gen_ai.usage.input_tokens already excludes the cache counts; OpenInference's prompt count
# includes them, so they are taken off it.
_INPUT_TOKENS_FIELD = (
    "coalesce(attributes.gen_ai.usage.input_tokens, attributes.llm.token_count.prompt"
    " - coalesce(attributes.llm.token_count.prompt_details.cache_read, 0)"
    " - coalesce(attributes.llm.token_count.prompt_details.cache_write, 0))"
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
# N17-obs439: the Strands terminal provider span (gen_ai.system=aws.bedrock) carries input /
# output only — the call's cache reads / writes (and total_tokens) are on its Strands wrapper
# chat span. So the cache counts are ALSO summed over the wrapper chat spans
# (`wrap_cache_*`, `is_wrap_call`) and `_cache_tokens` prefers the provider sums, falling back
# to the wrapper's — the same rule `q_tokens_by_model` applies to input / output.
_CALL_OPERATION = f"""(strcontains({_OPERATION_FIELD}, "chat")
        + strcontains({_OPERATION_FIELD}, "text_completion")
        + strcontains({_OPERATION_FIELD}, "generate_content"))"""
_IS_WRAPPER = 'strcontains(coalesce(attributes.gen_ai.system, ""), "strands-agents")'
_IS_LLM_FIELDS = f"""fields {_MODEL_FIELD} as telemetry_model,
       ({_CALL_OPERATION}
        * (1 - {_IS_WRAPPER})
        * (1 - {_NATIVE_AGENT_FIELD})
        + {_NATIVE_AGENT_FIELD}) as is_llm,
       {_CALL_OPERATION} * {_IS_WRAPPER} * (1 - {_NATIVE_AGENT_FIELD}) as is_wrap_call,
       strcontains(status.code, "ERROR") as is_error
| fields {_INPUT_TOKENS_FIELD} * is_llm as llm_in,
         {_OUTPUT_TOKENS_FIELD} * is_llm as llm_out,
         {_CACHE_READ_FIELD} * is_llm as llm_cache_read,
         {_CACHE_WRITE_FIELD} * is_llm as llm_cache_write,
         {_CACHE_READ_FIELD} * is_wrap_call as wrap_cache_read,
         {_CACHE_WRITE_FIELD} * is_wrap_call as wrap_cache_write"""
_CACHE_STATS = """sum(llm_cache_read) as cache_read,
        sum(llm_cache_write) as cache_write,
        sum(wrap_cache_read) as wrapper_cache_read,
        sum(wrap_cache_write) as wrapper_cache_write"""


def q_trace_aggregates(session_id: str | None = None, limit: int = TRACE_LIMIT) -> str:
    if session_id is not None:
        _require(SESSION_ID_RE, session_id, "session id")
    filters = ["ispresent(startTimeUnixNano)"]
    if session_id:
        filters.append(f'attributes.session.id = "{session_id}"')
    return f"""
{_spans_source()}
| filter {" and ".join(filters)}
| {_IS_LLM_FIELDS}
| stats count(*) as span_count, sum(is_llm) as llm_count,
        sum(llm_in) as tokens_in,
        sum(llm_out) as tokens_out,
        {_CACHE_STATS},
        sum(is_error) as error_count,
        min(startTimeUnixNano) as start_ns, max(endTimeUnixNano) as end_ns,
        latest(attributes.session.id) as session_id,
        latest(resource.attributes.service.name) as service,
        count_distinct(resource.attributes.service.name) as service_count,
        latest(telemetry_model) as model,
        count_distinct(telemetry_model) as model_count
  by traceId
| sort start_ns desc
| limit {limit}
"""


def q_root_spans(trace_ids: list[str] | None = None, limit: int = 3 * TRACE_LIMIT) -> str:
    # No session variant: root spans don't reliably carry session.id, so session
    # views join roots by traceId against the session-filtered aggregates —
    # `trace_ids` narrows the scan to those traces (an unfiltered roots query
    # over 7d is the slow half of a session view, and its newest-N cap can miss
    # an older session's roots entirely).
    filters = ["ispresent(startTimeUnixNano)", "not ispresent(parentSpanId)"]
    if trace_ids is not None:
        for trace_id in trace_ids:
            _require(TRACE_ID_RE, trace_id, "trace id")
        quoted = ", ".join(f'"{t}"' for t in trace_ids)
        filters.insert(0, f"traceId in [{quoted}]")
    return f"""
{_spans_source()}
| filter {" and ".join(filters)}
| fields name, traceId, resource.attributes.service.name as service,
         attributes.session.id as session_id,
         resource.attributes.session.id as resource_session_id,
         durationNano, startTimeUnixNano, status.code as status_code
| sort startTimeUnixNano desc
| limit {limit}
"""


def _q_provenance(predicate: str) -> str:
    """One complete-window source summary, never a newest-N trace sample.

    Include both supported sources and content records: even a spans-only view
    discovers runtime content groups, and actual readers may enrich from them.
    """
    return f"""
{SPANS_SOURCE}
| filter {predicate}
| stats count(*) as record_count,
        count(resource.attributes.service.name) as attributed_count,
        count_distinct(resource.attributes.service.name) as service_count,
        latest(resource.attributes.service.name) as service
"""


def q_session_provenance(session_id: str) -> str:
    _require(SESSION_ID_RE, session_id, "session id")
    return _q_provenance(f'attributes.session.id = "{session_id}"')


def q_trace_provenance(trace_id: str) -> str:
    _require(TRACE_ID_RE, trace_id, "trace id")
    return _q_provenance(f'traceId = "{trace_id}"')


def q_session_aggregates(limit: int = SESSION_LIMIT) -> str:
    return f"""
{_spans_source()}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| {_IS_LLM_FIELDS}
| stats count_distinct(traceId) as traces, sum(is_llm) as llm_calls,
        sum(llm_in) as tokens_in,
        sum(llm_out) as tokens_out,
        {_CACHE_STATS},
        sum(is_error) as errors,
        min(startTimeUnixNano) as first_ns, max(endTimeUnixNano) as last_ns,
        latest(resource.attributes.service.name) as service,
        count_distinct(resource.attributes.service.name) as service_count,
        latest(telemetry_model) as model
  by attributes.session.id as session_id
| sort last_ns desc
| limit {limit}
"""


def q_dashboard_series(range_key: str) -> str:
    return f"""
{_spans_source()}
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
{_spans_source()}
| filter ispresent(startTimeUnixNano) and not ispresent(parentSpanId)
| fields strcontains(status.code, "ERROR") as is_error
| stats count(*) as traces, sum(is_error) as errors,
        pct(durationNano, 50) as p50_nano, pct(durationNano, 95) as p95_nano
"""


def q_dashboard_distincts() -> str:
    return f"""
{_spans_source()}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| stats count_distinct(attributes.session.id) as sessions,
        count_distinct(resource.attributes.service.name) as agents
"""


def q_top_tools(limit: int = 10) -> str:
    return f"""
{_spans_source()}
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
    sums per model: a non-Bedrock provider may emit the wrapper span ONLY, so a
    model whose provider sums are zero falls back to its wrapper sums in
    ``_tokens_by_model_rows`` — the group-by mirror of the per-trace "prefer
    non-wrapper, fall back to wrapper" rollup rule.
    """
    return f"""
{_spans_source()}
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
         {_CACHE_READ_FIELD} * is_call * (1 - is_wrapper) as llm_cache_read,
         {_CACHE_WRITE_FIELD} * is_call * (1 - is_wrapper) as llm_cache_write,
         {_INPUT_TOKENS_FIELD} * is_call * is_wrapper as wrap_in,
         {_OUTPUT_TOKENS_FIELD} * is_call * is_wrapper as wrap_out,
         {_CACHE_READ_FIELD} * is_call * is_wrapper as wrap_cache_read,
         {_CACHE_WRITE_FIELD} * is_call * is_wrapper as wrap_cache_write
| stats sum(llm_in) as tokens_in, sum(llm_out) as tokens_out,
        sum(wrap_in) as wrapper_in, sum(wrap_out) as wrapper_out,
        {_CACHE_STATS}
  by telemetry_model as model
| limit {limit}
"""


def _cache_tokens(row: dict[str, str]) -> tuple[float, float]:
    """N17-obs439: ``(cache_read, cache_write)`` of an aggregate row — the provider spans'
    sums, else the Strands wrapper chat spans' (a bedrock provider span carries none)."""
    read = _num(row, "cache_read") or _num(row, "wrapper_cache_read")
    write = _num(row, "cache_write") or _num(row, "wrapper_cache_write")
    return read, write


def _tokens(tokens_in: float, tokens_out: float, cache_read: float = 0.0,
            cache_write: float = 0.0) -> dict[str, int]:
    """The console's token shape. ``input`` is the providers' ``inputTokens``, which leaves out
    prompt-cache reads AND writes; ``total`` = input + output + cache reads + cache writes —
    what Bedrock's own ``totalTokens`` adds up to on every real Kimi / GLM span record checked
    (N17-obs439: 55 research17 records, 6 with cache writes, and live spans 2026-10-25)."""
    out = {"input": round(tokens_in), "output": round(tokens_out),
           "cache_read": round(cache_read), "cache_write": round(cache_write)}
    out["total"] = out["input"] + out["output"] + out["cache_read"] + out["cache_write"]
    return out


def _tokens_by_model_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Project q_tokens_by_model rows into the tokens_by_model response shape."""
    out: list[dict[str, Any]] = []
    for row in rows:
        tokens_in, tokens_out = _num(row, "tokens_in"), _num(row, "tokens_out")
        if not (tokens_in or tokens_out):  # wrapper-only provider (see builder)
            tokens_in, tokens_out = _num(row, "wrapper_in"), _num(row, "wrapper_out")
        if not (tokens_in or tokens_out):
            continue
        model = row.get("model") or "unknown"
        out.append({"model": model, **_tokens(tokens_in, tokens_out, *_cache_tokens(row))})
    out.sort(key=lambda r: -r["total"])
    return out


def q_trace_spans(trace_id: str) -> str:
    _require(TRACE_ID_RE, trace_id, "trace id")
    return f"""
{_spans_source()}
| filter traceId = "{trace_id}" and ispresent(startTimeUnixNano)
| fields @message
| limit {SPANS_PER_TRACE}
"""


def q_session_spans(session_id: str) -> str:
    """Raw span records of one session, oldest first — the exact per-session
    query the AgentCore on-demand evaluation guide prescribes, over both
    telemetry layouts. Records without ``scope.name`` are correlated logs, not
    spans, and Evaluate rejects them."""
    _require(SESSION_ID_RE, session_id, "session id")
    return f"""
{_spans_source()}
| filter ispresent(scope.name) and ispresent(attributes.session.id)
    and attributes.session.id = "{session_id}"
| fields @message
| sort @timestamp asc
| limit {SESSION_SPANS_LIMIT}
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
    """A span's tokens with ``input`` = the UNCACHED prompt tokens, in both conventions
    (N17-obs439):

    - ``gen_ai.usage.*`` (Strands / Bedrock): ``input_tokens`` already excludes the cache
      reads and writes, which are reported beside it (``total_tokens`` = input + output +
      cache read + cache write on every real record checked);
    - OpenInference ``llm.token_count.*``: ``prompt`` counts the WHOLE prompt and the
      ``prompt_details.cache_*`` are its breakdown (OpenInference semantic conventions), so the
      uncached input is prompt − cache. Spec-based: no OpenInference span with cache counts
      has been recorded on this platform yet.
    """
    cache_read = _attr(attrs, "gen_ai.usage.cache_read_input_tokens",
                       "llm.token_count.prompt_details.cache_read")
    cache_write = _attr(attrs, "gen_ai.usage.cache_write_input_tokens",
                        "llm.token_count.prompt_details.cache_write")
    tokens_in = _attr(attrs, "gen_ai.usage.input_tokens")
    if tokens_in is None and _attr(attrs, "llm.token_count.prompt") is not None:
        prompt = float(_attr(attrs, "llm.token_count.prompt") or 0)
        tokens_in = max(prompt - float(cache_read or 0) - float(cache_write or 0), 0.0)
    values = {
        "input": tokens_in,
        "output": _attr(attrs, "gen_ai.usage.output_tokens", "llm.token_count.completion"),
        "cache_read": cache_read,
        "cache_write": cache_write,
    }
    if all(value is None for value in values.values()):
        return None
    return {key: float(value or 0) for key, value in values.items()}


def _resource_attrs(span: dict[str, Any]) -> dict[str, Any]:
    """`span.resource.attributes` when both levels are objects, else `{}` (N16-obs53)."""
    resource = span.get("resource")
    attrs = resource.get("attributes") if isinstance(resource, dict) else None
    return attrs if isinstance(attrs, dict) else {}


def build_span_tree(raw_spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Nest spans by parentSpanId; annotate offsets/durations as ms + percentages."""
    starts = [s for s in (_span_ns(sp, "startTimeUnixNano") for sp in raw_spans) if s]
    ends = [e for e in (_span_ns(sp, "endTimeUnixNano") for sp in raw_spans) if e]
    trace_start = min(starts) if starts else 0.0
    trace_end = max(ends) if ends else trace_start
    total_ns = max(trace_end - trace_start, 1.0)

    flat: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    # N16-obs53: a row whose `attributes` / `status` / `resource` is not an object (a
    # correlated log record, a truncated export) is rendered with empty ones, never a 500 —
    # and COUNTED, so a shorter-looking trace never passes for a clean one (rev-r16)
    malformed = {"attrs": 0, "status": 0, "resource": 0}
    for span in raw_spans:
        attrs = span.get("attributes")
        if attrs is not None and not isinstance(attrs, dict):
            malformed["attrs"] += 1
        attrs = attrs if isinstance(attrs, dict) else {}
        status = span.get("status")
        if status is not None and not isinstance(status, dict):
            malformed["status"] += 1
        status_code = status.get("code", "UNSET") if isinstance(status, dict) else "UNSET"
        resource = span.get("resource")
        if resource is not None and not (
            isinstance(resource, dict)
            and (resource.get("attributes") is None or isinstance(resource.get("attributes"), dict))
        ):
            malformed["resource"] += 1
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
            "status": status_code,
            "start_offset_ms": round(offset_ns / NANOS_PER_MS, 1),
            "duration_ms": round(duration_ns / NANOS_PER_MS, 1),
            "offset_pct": round(offset_ns / total_ns * 100, 2),
            "width_pct": round(duration_ns / total_ns * 100, 2),
            "model": model,
            "finish_reason": finish,
            "tool_name": _attr(attrs, "gen_ai.tool.name", "tool.name"),
            "tokens": tokens,
            # N17-obs307: credential-like attributes (ADOT's botocore spans carry
            # `aws.auth.account.access_key`) are masked before they reach the response
            # or the trace cache
            "attributes": redact_attributes(attrs),
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
    # N16-obs52: spans whose parent pointers form a cycle reach no root; promote the
    # first-arrived member of each unreachable group to a root so the waterfall keeps every
    # span (its back-pointing child is detached from it to break the cycle)
    reachable: set[int] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if id(node) not in reachable:
            reachable.add(id(node))
            stack.extend(node["children"])
    for node in nodes.values():
        if id(node) not in reachable:
            parent = nodes[node["parent_span_id"]]
            parent["children"] = [c for c in parent["children"] if c is not node]
            roots.append(node)
            stack = [node]
            while stack:
                cur = stack.pop()
                if id(cur) not in reachable:
                    reachable.add(id(cur))
                    stack.extend(cur["children"])

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
        "malformed": malformed,
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
            # Provenance survives normalization: the session id a content event was
            # recorded under (a correlated span may carry none) is what the privacy
            # boundary filters on, before AND after the cache.
            resource = record.get("resource") if isinstance(record.get("resource"), dict) else {}
            candidates = [
                (record.get("attributes") or {}).get("session.id")
                if isinstance(record.get("attributes"), dict) else None,
                (resource.get("attributes") or {}).get("session.id")
                if isinstance(resource.get("attributes"), dict) else None,
                resource.get("session.id"),
            ]
            sids = entry.setdefault("session_ids", [])
            for sid in candidates:
                if isinstance(sid, str) and sid and sid not in sids:
                    sids.append(sid)  # EVERY contributing event's id, not only the first
            if sids and not entry.get("session_id"):
                entry["session_id"] = sids[0]
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
    return {k: v for k, v in by_span.items() if v.get("input") or v.get("output")}


def record_session_ids(record: dict[str, Any]) -> set[str]:
    """All explicit session claims on an actual OTel document."""
    resource = record.get("resource")
    resource = resource if isinstance(resource, dict) else {}
    sessions = set()
    for attrs in (record.get("attributes"), resource.get("attributes"), resource):
        if not isinstance(attrs, dict):
            continue
        nested = attrs.get("session")
        for sid in (attrs.get("session.id"),
                    nested.get("id") if isinstance(nested, dict) else None):
            if isinstance(sid, str) and sid:
                sessions.add(sid)
    return sessions


def _span_log_groups(raw_spans: list[dict[str, Any]]) -> list[str]:
    groups: set[str] = set()
    for span in raw_spans:
        names = _resource_attrs(span).get("aws.log.group.names")
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
    rows = []
    for model, usage in per_model.items():
        tokens_in, tokens_out = usage.get("input", 0.0), usage.get("output", 0.0)
        # the metric carries no cache token type: cache counts read 0 here
        rows.append({"model": model, **_tokens(tokens_in, tokens_out)})
    rows.sort(key=lambda r: -r["total"])
    return rows


# ── Row composers ───────────────────────────────────────────────────────────


def _trace_row(
    agg: dict[str, str],
    root: dict[str, str] | None,
    map_agent: Callable[[str | None], str],
) -> dict[str, Any]:
    start_ns, end_ns = _num(agg, "start_ns"), _num(agg, "end_ns")
    tokens = _tokens(_num(agg, "tokens_in"), _num(agg, "tokens_out"), *_cache_tokens(agg))
    model = agg.get("model") or None
    service = agg.get("service") or (root or {}).get("service")
    duration_ns = _num(root or {}, "durationNano") or (end_ns - start_ns)
    return {
        "trace_id": agg.get("traceId"),
        "time": _ns_to_iso(start_ns),
        "root_operation": (root or {}).get("name") or "trace",
        # A trace-id join may enrich a local aggregate with an external A2A root.
        # Keep its own provenance in the cache; the member response authorizes it
        # separately and can fall back to the aggregate without losing local data.
        "_root_enrichment": {
            "service": root.get("service"),
            "session_ids": [root[key] for key in ("session_id", "resource_session_id")
                            if root.get(key)],
            "aggregate_duration_ms": round((end_ns - start_ns) / NANOS_PER_MS, 1),
        } if root else None,
        "service": service,
        "service_count": int(_num(agg, "service_count")),
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
    }


def _session_row(row: dict[str, str], map_agent: Callable[[str | None], str],
                 platform_ids: set[str]) -> dict[str, Any]:
    service = row.get("service")
    session_id = row.get("session_id") or ""
    return {
        "session_id": session_id,
        "service": service,
        "service_count": int(_num(row, "service_count")),
        "agent": map_agent(service),
        "traces": int(_num(row, "traces")),
        "llm_calls": int(_num(row, "llm_calls")),
        "errors": int(_num(row, "errors")),
        "tokens": _tokens(_num(row, "tokens_in"), _num(row, "tokens_out"), *_cache_tokens(row)),
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
        tokens_tile = _tokens(*(sum(r.get(k, 0) for r in tokens_by_model)
                                for k in ("input", "output", "cache_read", "cache_write")))
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
                "tokens": tokens_tile,
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
        # per-user policy: a trace of a derived per-principal runtime session (`lpp-…`) is
        # listed under its conversation, the derived id kept beside it (additive key)
        derived = runtime_sessions.conversation_of(
            db, workspace.id, [r.get("session_id") or "" for r in rows])
        for row in rows:
            caller = derived.get(row.get("session_id") or "")
            if caller:
                row["runtime_session_id"], row["session_id"] = row["session_id"], caller
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
        skipped_rows = 0
        for row in results["spans"]:
            try:
                parsed = json.loads(row.get("@message", ""))
            except (TypeError, ValueError):
                skipped_rows += 1  # a plain-text log line in the span query result
                continue
            if isinstance(parsed, dict):
                raw_spans.append(parsed)
            else:  # N16-obs53: a non-object JSON row is not a span
                skipped_rows += 1
        tree = build_span_tree(raw_spans)
        spans = tree["spans"]
        # rev-r16: every row skipped or field coerced is counted into `meta.malformed` and
        # logged once per trace (shapes only — never row content)
        malformed = {"rows": skipped_rows, **tree["malformed"]}
        if any(malformed.values()):
            logger.warning(
                "trace %s: malformed span rows in the Logs Insights result — %s",
                trace_id, ", ".join(f"{k}={v}" for k, v in malformed.items() if v),
            )
        # gen_ai message events (prompts/completions) live in the agents' own
        # runtime log groups, referenced by the spans themselves; best-effort.
        messages_by_span: dict[str, dict[str, Any]] = {}
        raw_events: list[dict[str, Any]] = []
        log_groups = _span_log_groups(raw_spans)
        if log_groups:
            try:
                event_rows = run_insights_queries(
                    {"events": q_trace_message_events(trace_id)},
                    hours, logs=logs, log_groups=log_groups, workspace=workspace,
                )["events"]
                for event_row in event_rows:
                    try:
                        record = json.loads(event_row.get("@message") or "")
                    except (ValueError, TypeError):
                        continue
                    if isinstance(record, dict):
                        raw_events.append(record)
                messages_by_span = parse_message_events(event_rows)
            except Exception:  # best-effort enrichment — never fail the trace
                messages_by_span = {}
        for span in spans:
            span["messages"] = messages_by_span.get(span["span_id"] or "")
        # Every session id this trace's spans OR content events were recorded under.
        source_records = raw_spans + raw_events
        session_ids = sorted({
            sid for sid in (
                [s["attributes"].get("session.id") for s in spans]
                + [sid for m in messages_by_span.values() for sid in (m.get("session_ids") or [])]
                + [sid for record in source_records for sid in record_session_ids(record)]
            ) if isinstance(sid, str) and sid
        })
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
        # N17-obs439: the bedrock provider span carries no cache counts — they are on its
        # Strands wrapper; a component the providers report none of falls back to the wrappers
        wrappers = [s for s in with_tokens
                    if s["attributes"].get("gen_ai.system") == "strands-agents"
                    and s["category"] == "llm"]

        def component(key: str) -> float:
            return (sum(s["tokens"][key] for s in llm_spans)
                    or sum(s["tokens"][key] for s in wrappers))

        tokens = _tokens(component("input"), component("output"),
                         component("cache_read"), component("cache_write"))
        service = next(
            (
                _resource_attrs(sp).get("service.name")
                for sp in raw_spans
                if _resource_attrs(sp).get("service.name")
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
        source_services = [_resource_attrs(record).get("service.name")
                           for record in source_records]
        return {
            "trace_id": trace_id,
            "range": range_key,
            "meta": {
                "root_operation": root["name"] if root else None,
                "service": service,
                "services": sorted({name for name in source_services if isinstance(name, str)}),
                # Validate every actual source after a cache hit too. A successful
                # earlier metadata probe cannot authorize newly arrived content.
                "provenance_complete": bool(source_records) and all(
                    isinstance(name, str) and bool(name) for name in source_services
                ) and all(record.get("traceId") == trace_id for record in source_records),
                "agent": map_agent(service),
                "session_id": session_id,
                "session_ids": session_ids,
                "start": tree["start"],
                "duration_ms": tree["duration_ms"],
                "span_count": len(spans),
                "llm_count": len(llm_spans),
                "status": "error" if any(s["status"] == "ERROR" for s in spans) else "ok",
                "tokens": tokens,
                # additive (N16-obs53 / rev-r16): rows skipped and fields coerced while
                # building this trace; all zero for a clean export
                "malformed": malformed,
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
        # per-user policy: derived per-principal runtime sessions (`lpp-…`) are shown under
        # the conversation they belong to, never as separate sessions
        rows = runtime_sessions.fold_session_rows(rows, runtime_sessions.conversation_of(
            db, workspace_id, [r["session_id"] for r in rows]))
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
                logs: Any = None, viewer: Viewer = Viewer.ALL,
                records_guard: RecordsGuard | None = None,
                source_session_ids: list[str] | None = None) -> dict[str, Any]:
    hours = RANGE_HOURS[range_key]

    # per-user policy: the conversation's derived per-principal runtime sessions carry its
    # spans too (none while the carrier never ran a turn in it — one ledger read)
    sources = source_session_ids if source_session_ids is not None else (
        runtime_sessions.source_session_ids(db, workspace.id, session_id, viewer))
    derived = sources[1:]

    def build() -> dict[str, Any]:
        queries = {"aggregates": q_trace_aggregates(session_id=session_id)}
        for index, extra in enumerate(derived):
            queries[f"aggregates{index}"] = q_trace_aggregates(session_id=extra)
        found = run_insights_queries(queries, hours, logs=logs, workspace=workspace)
        aggregates = [row for key in queries for row in found.get(key) or []]
        trace_ids = [row["traceId"] for row in aggregates if row.get("traceId")]
        # Second pass, roots of THESE traces only; chunked so each query string
        # stays well under the Logs Insights length cap, chunks run concurrently.
        chunks = {
            f"roots{i}": q_root_spans(trace_ids[i:i + ROOT_TRACE_CHUNK])
            for i in range(0, len(trace_ids), ROOT_TRACE_CHUNK)
        }
        root_rows = run_insights_queries(
            chunks, hours, logs=logs, workspace=workspace
        ) if chunks else {}
        roots = {
            row.get("traceId"): row for rows in root_rows.values() for row in rows
        }
        map_agent = build_agent_mapper(db, workspace.id)
        rows = [
            _trace_row(agg, roots.get(agg.get("traceId")), map_agent)
            for agg in aggregates
        ]
        tokens = _tokens(*(sum(r["tokens"][k] for r in rows)
                           for k in ("input", "output", "cache_read", "cache_write")))
        times = [r["time"] for r in rows if r["time"]]
        # Online evaluation scores ride along in the same cache entry but are their own
        # ledger read (N12 E2-3): a failure there degrades to {unavailable: true}
        # instead of taking the traces down. Local import — the online module imports
        # this one for the range table.
        from app.evaluation import online as online_eval

        online_scores = online_eval.session_online_scores(
            db, workspace, session_id, hours, logs=logs
        )
        return {
            "session_id": session_id,
            "range": range_key,
            "summary": {
                "agent": rows[0]["agent"] if rows else None,
                "traces": len(rows),
                "llm_calls": sum(r["llm_count"] for r in rows),
                "errors": sum(r["error_count"] for r in rows),
                "tokens": tokens,
                "first": min(times) if times else None,
                "last": max(times) if times else None,
            },
            "traces": rows,
            "online_scores": online_scores,
        }

    cache_key = f"session:{workspace.id}:{session_id}:{range_key}:{json.dumps(derived)}"
    payload = cached(cache_key, force, build)
    # Transcript is attached outside the cache: memory errors must degrade to
    # {available: false} on every request, never poison the cached span data.
    # It runs AFTER the trace query because sessions with no ledger row carry
    # their only agent signal in the spans (service.name → Agent).
    transcript = session_transcript(
        db,
        session_id,
        workspace,
        agent=_agent_from_traces(db, workspace.id, payload["traces"]),
        started_at=_iso_datetime((payload.get("summary") or {}).get("first")),
        viewer=viewer,
        records_guard=records_guard,
    )
    return {**payload, "transcript": transcript}


def _iso_datetime(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def get_session_transcript(
    session_id: str,
    db: Session,
    workspace: WorkspaceContext,
    agent_id: str | None = None,
    started_at: datetime | None = None,
    viewer: Viewer = Viewer.ALL,
    records_guard: RecordsGuard | None = None,
) -> dict[str, Any]:
    """The session's conversation alone — no Logs Insights pass.

    For views that already know the session's agent (an evaluation result row):
    `get_session` would spend seconds on span aggregates just to recover the
    agent that `session_transcript` falls back on for unattributed sessions.
    `started_at` (its first span) lets a session no ledger row claims fall back to
    that agent's content logs (upstream #287).
    """
    agent = None
    if agent_id:
        agent = db.get(Agent, agent_id)
        if agent is not None and agent.workspace_id != workspace.id:
            agent = None
    return {
        "session_id": session_id,
        "transcript": session_transcript(
            db, session_id, workspace, agent=agent, started_at=started_at, viewer=viewer,
            records_guard=records_guard,
        ),
    }


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
            # N-SCN-bug4: additive; the answer the runtime closed at the model's token limit
            **({"truncated": True} if getattr(message, "truncated", False) else {}),
        }
        for message in rows
    ]


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


# ── eval-session content logs (N12 E2 — restored from the Global baseline) ──────
# The recommendation evidence (`optimization/providers/evidence.py`) rebuilds an eval
# session's conversation and tool calls from the runtime's otel-rt-logs content records
# when Memory does not hold them.


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


def _content_records(
    log_group: str,
    session_id: str,
    started_at: datetime | None,
    workspace: WorkspaceContext,
    logs: Any = None,
    records_guard: RecordsGuard | None = None,
) -> list[dict[str, Any]]:
    """The session's gen_ai content records (otel-rt-logs), oldest first.

    Shared by the text-turn and the tool-turn extractors. filter_log_events
    scans oldest-first, so startTime (the run's creation time) is load-bearing —
    without it the scan exhausts its page budget on old log data and returns
    nothing. A missing group / permission error degrades to ``[]``.
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
            # N17-obs520: the substring filter also matches the runtime's plain
            # application log lines (`bedrock_agentcore.app`, a bare `sessionId`, no
            # resource attributes); they are not telemetry and are skipped, so only
            # actual content records reach the member guard — which stays fail-closed.
            attrs = rec.get("attributes") if isinstance(rec, dict) else None
            if not isinstance(attrs, dict) or attrs.get("session.id") != session_id:
                continue
            if records_guard:
                records_guard(session_id, [rec])
            records.append(rec)
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return sorted(records, key=lambda r: r.get("timeUnixNano") or 0)


def _iter_body_parts(body: dict[str, Any]):
    """(kind, role, parts) for each message of a content record whose content is
    a JSON-encoded part list — the shape tool calls arrive in. Plain-string
    content (text-only messages) yields nothing here; `_iter_body_messages`
    covers those."""
    for kind in ("input", "output"):
        for msg in (body.get(kind) or {}).get("messages") or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, dict):
                raw = content.get("message") if "message" in content else content.get("content")
            else:
                raw = content
            if not isinstance(raw, str) or not raw.lstrip().startswith("["):
                continue
            try:
                parts = json.loads(raw)
            except ValueError:
                continue
            if isinstance(parts, list):
                yield kind, msg.get("role"), [p for p in parts if isinstance(p, dict)]


def eval_tool_turns_from_content_logs(
    log_group: str,
    session_id: str,
    started_at: datetime | None,
    workspace: WorkspaceContext,
    logs: Any = None,
) -> list[dict[str, Any]]:
    """TOOL_CALL / TOOL_RESULT turns for an eval session, from content logs.

    Strands runtimes record the model's ``toolUse`` parts on output/assistant
    messages and the ``toolResult`` parts on the next input/tool message (and
    echo the result on the output too). Every record carries the FULL history
    so far, so the same call appears in many records — deduped by
    ``toolUseId``, first sighting wins for time, the input/tool copy wins for
    the result body. Ordered by first sighting; a call precedes its result.
    Companion of `eval_turns_from_content_logs` (which keeps text turns only).
    """
    records = _content_records(log_group, session_id, started_at, workspace, logs)
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rec in records:
        ts = rec.get("timeUnixNano") or 0
        trace_id = rec.get("traceId")
        for kind, _role, parts in _iter_body_parts(rec.get("body") or {}):
            for part in parts:
                use = part.get("toolUse")
                if isinstance(use, dict) and use.get("toolUseId"):
                    tid = str(use["toolUseId"])
                    if tid not in calls:
                        calls[tid] = {
                            "role": "TOOL_CALL", "id": tid,
                            "name": str(use.get("name") or ""),
                            "input": use.get("input"),
                            "at": _ns_iso(ts), "trace_id": trace_id,
                        }
                        order.append(tid)
                    continue
                res = part.get("toolResult")
                if isinstance(res, dict) and res.get("toolUseId"):
                    tid = str(res["toolUseId"])
                    texts = [
                        c.get("text") for c in (res.get("content") or [])
                        if isinstance(c, dict) and c.get("text")
                    ]
                    entry = {
                        "role": "TOOL_RESULT", "id": tid,
                        "name": calls.get(tid, {}).get("name", ""),
                        "status": str(res.get("status") or ""),
                        "text": "\n".join(str(t) for t in texts),
                        "at": _ns_iso(ts), "trace_id": trace_id,
                    }
                    # the input/tool copy is the one the model actually consumed
                    if tid not in results or kind == "input":
                        results[tid] = entry
                    if tid not in calls:
                        order.append(tid)
    turns: list[dict[str, Any]] = []
    for tid in order:
        if tid in calls:
            turns.append(calls[tid])
        if tid in results:
            res = results[tid]
            if not res["name"] and tid in calls:
                res["name"] = calls[tid]["name"]
            turns.append(res)
    return turns


def eval_turns_from_content_logs(
    log_group: str,
    session_id: str,
    started_at: datetime | None,
    workspace: WorkspaceContext,
    logs: Any = None,
    records_guard: RecordsGuard | None = None,
) -> list[dict[str, Any]]:
    """USER/ASSISTANT turns for an eval session, rebuilt from content logs.

    One invocation = one traceId. Its USER turn is the trace's latest input
    user message (later records carry the full history — last one is the
    current turn); its ASSISTANT turn is the ``end_turn`` output, falling back
    to the last assistant text. filter_log_events scans oldest-first, so
    startTime (the run's creation time) is load-bearing — without it the scan
    exhausts its page budget on old log data and returns nothing.
    """
    records = _content_records(log_group, session_id, started_at, workspace, logs,
                               records_guard=records_guard)

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


NOT_PLATFORM_SESSION = {"available": False, "reason": "not_platform_session"}


def _eval_content_log_group(agent: Agent, workspace: WorkspaceContext) -> str | None:
    """The log group holding an agent's otel-rt-logs content records, or None.

    CN: every evaluable method is a Runtime (the self-built 方式B harness included), so
    the group is the runtime's own; a discovered or undeployed agent degrades to None.
    """
    # local import — the evaluation service sits above this module
    from app.evaluation.service import EVAL_SUPPORTED_METHODS

    if not agent.resource_id or agent.method not in EVAL_SUPPORTED_METHODS:
        return None
    return f"{RUNTIME_LOG_GROUP_PREFIX}{agent.resource_id}-DEFAULT"


def replay_run_of(db: Session, workspace_id: str | None, session_id: str) -> Any:
    """The evaluation run that replayed `session_id` — a dataset scenario's pre-assigned
    session, a key of the run's `scenario_map` — or None (N17-obs432). An id only listed
    in `session_ids` (an explicit-session run scores a session someone else drove) is not
    the run's. The newest run wins should two ever claim one id."""
    # local import — the evaluation package sits above this module
    from app.evaluation.models import EvalRun

    rows = (
        db.query(EvalRun)
        .filter(
            EvalRun.workspace_id == workspace_id,
            cast(EvalRun.scenario_map, String).contains(f'"{session_id}"', autoescape=True),
        )
        .order_by(EvalRun.created_at.desc())
        .all()
    )
    return next((run for run in rows if session_id in (run.scenario_map or {})), None)


def _external_transcript(
    db: Session,
    session_id: str,
    workspace: WorkspaceContext,
    agent: Agent | None,
    started_at: datetime | None = None,
    viewer: Viewer = Viewer.ALL,
    records_guard: RecordsGuard | None = None,
) -> dict[str, Any]:
    """Transcript for a session that is in NO platform ledger — a `/v1` caller, or
    any other direct runtime invoke.

    Memory has no "which actor owns this session" lookup, so probe a bounded
    candidate set: the traced agent's scoped actors first (unambiguous), then
    the bare "default" actor that the gateway→runtime hop leaves in place.
    Nothing in Memory (an agent with memory disabled, no backend registered) and
    the session's start known: the agent's runtime content logs, the same records
    the evaluation worker reads (upstream #287).

    A session an evaluation run replayed (`replay_run_of`, N17-obs432) is the platform's
    own: the run names the agent it drove and, absent the caller's first-span time, the
    content-log start (its creation — the replay follows it); `source` is then `eval`
    with the run's id. Only a run `viewer` may see is used. The turns are always the
    agent's own records — never the dataset's expected answers.
    """
    run = replay_run_of(db, workspace.id, session_id)
    if run is not None and not visibility.sees(viewer, db, run.created_by):
        run = None
    if run is not None:
        run_agent = db.get(Agent, run.agent_id) if run.agent_id else None
        if run_agent is not None and run_agent.workspace_id == workspace.id:
            agent = run_agent
        started_at = started_at or run.created_at
    turns: list[dict[str, Any]] = []
    actor_id: str | None = None
    origin = "memory"
    backend = memory_backend.backend_or_none()
    if backend is not None:
        try:
            candidates: list[str] = []
            if agent is not None:
                prefix = f"{agent.id}{memory_backend.SCOPE_SEP}"
                page = backend.list_actors(workspace)
                candidates = [
                    str(item.get("actor_id") or "")
                    for item in page.get("items") or []
                    if str(item.get("actor_id") or "").startswith(prefix)
                ]
            candidates.append("default")
            for candidate in candidates:
                page = backend.list_events(workspace, candidate, session_id, True, None, 100)
                turns = _turns_from_events(page.get("items") or [])
                if turns:
                    actor_id = candidate
                    break
        except Exception:
            # Memory unreachable / not bootstrapped: the session was already
            # unattributable — only the content logs below can still say something.
            turns = []
    if not turns and agent is not None and started_at is not None:
        try:
            log_group = _eval_content_log_group(agent, workspace)
            if log_group:
                turns = eval_turns_from_content_logs(
                    log_group, session_id, started_at, workspace,
                    **({"records_guard": records_guard} if records_guard is not None else {}),
                )
                origin = "logs"
        except AppError as exc:
            if exc.code == "observability.session_not_found":
                raise
            turns = []
        except Exception:
            turns = []
    if not turns:
        if backend is None:
            return {**NOT_PLATFORM_SESSION, "reason": "memory backend not registered"}
        return dict(NOT_PLATFORM_SESSION)
    return {
        "available": True,
        "actor_id": actor_id,
        "agent_id": agent.id if agent else None,
        "agent_name": agent.name if agent else None,
        "source": "eval" if run is not None else "external",
        "origin": origin,
        "run_id": run.id if run is not None else None,
        "turns": turns,
        # Long-term namespaces are keyed on the actor alone, and these
        # actors are shared across agents/runs — the count would say
        # nothing about this session (same reason eval skips it).
        "long_term_records": None,
    }


def session_transcript(
    db: Session,
    session_id: str,
    workspace: WorkspaceContext,
    agent: Agent | None = None,
    *,
    started_at: datetime | None = None,
    long_term: bool = True,
    viewer: Viewer = Viewer.ALL,
    records_guard: RecordsGuard | None = None,
) -> dict[str, Any]:
    """`started_at` — the session's first span — lets a session no ledger row
    claims fall back to the agent's content logs; `long_term=False` skips the
    chat session's long-term record count (two Memory reads a preview row does
    not show). Upstream #287. `viewer` gates the evaluation run a replayed session
    is read through (N17-obs432)."""
    row = (
        db.query(ChatSession)
        .filter(
            ChatSession.workspace_id == workspace.id,
            ChatSession.session_id == session_id,
        )
        .first()
    )
    if row is None:
        # Not a chat session — try memory directly (`agent` comes from the
        # session's traces, which is the only agent signal such sessions carry).
        return _external_transcript(db, session_id, workspace, agent, started_at, viewer,
                                    records_guard=records_guard)
    agent = db.get(Agent, row.agent_id)
    # Memory is written under an agent-scoped actor (memory_backend.scoped_actor);
    # the ledger stores the bare human actor, so re-scope for the read.
    mem_actor = memory_backend.scoped_actor(row.agent_id, row.actor_id)
    agent_id, actor_display = row.agent_id, row.actor_id
    memory_error: Exception | None = None
    events: list[dict[str, Any]] = []
    # an agent whose spec pins its own memory writes its turns there — read only
    # from a memory this workspace manages (issue #55); a foreign pin refuses the read
    try:
        memory_ownership.readable_spec_memory_id(db, workspace, agent.spec if agent else None)
        readable = True
    except AppError as exc:
        memory_error, readable = exc, False
    backend = memory_backend.backend_or_none()
    if readable and backend is None:
        readable = False
        memory_error = RuntimeError("memory backend not registered")
    if readable:
        try:
            # A spec-pinned store is the backend's concern: the N6 backend resolves
            # the agent's store from the scoped actor it indexes.
            page = backend.list_events(workspace, mem_actor, session_id, True, None, 100)
            events = page.get("items") or []
        except Exception as exc:
            memory_error = exc  # chat may fall back to its ledger
    turns = _turns_from_events(events)
    origin = "memory"
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
            "reason": (
                "memory backend not registered" if backend is None else "memory_unavailable"
            ),
            "detail": f"{type(memory_error).__name__}: {memory_error}"[:200],
            "actor_id": actor_display,
        }
    elif ledger_turns:
        # same conversation in Memory and in the ledger: Memory does not carry the
        # N-SCN-bug4 `truncated` marker, so the ledger's flags ride along positionally
        for turn, ledger_turn in zip(turns, ledger_turns, strict=True):
            if ledger_turn.get("truncated"):
                turn["truncated"] = True
    # Long-term records only make sense when Memory is actually readable here.
    long_term_records = None
    if readable and long_term:
        try:
            long_term_records = sum(
                len(
                    backend.list_records(
                        workspace, f"{ns}/{mem_actor}", None, None, 20
                    ).get("items")
                    or []
                )
                for ns in ("/preferences", "/facts")
            )
        except Exception:
            pass
    return {
        "available": True,
        "actor_id": actor_display,
        "agent_id": agent_id,
        "agent_name": agent.name if agent else None,
        "source": "chat",
        "origin": origin,
        "run_id": None,
        "turns": turns,
        "long_term_records": long_term_records,
    }


# ── Session previews (the task wizard's trajectory picker, upstream #287) ────

MAX_PREVIEW_SESSIONS = 20
PREVIEW_WORKERS = 8
PREVIEW_TEXT_CAP = 600


def session_transcripts(
    sessions: dict[str, datetime | None],
    workspace: WorkspaceContext,
    agent_id: str | None = None,
    viewer: Viewer = Viewer.ALL,
    records_guard: RecordsGuard | None = None,
) -> dict[str, dict[str, Any]]:
    """Each session's transcript (no long-term count), read in parallel.

    `sessions` maps a session id to its first span time (the content-log
    fallback's start, see `session_transcript`). Same sources as the single
    read — chat ledger, Memory, content logs — one worker per session with its
    own DB session (a SQLAlchemy Session is not thread-safe). `agent_id`
    attributes sessions no ledger row claims; another workspace's id is dropped.
    One session's failed read degrades to an unavailable transcript, never
    fails the batch.
    """
    def one(session_id: str) -> dict[str, Any]:
        db = SessionLocal()
        try:
            agent = db.get(Agent, agent_id) if agent_id else None
            if agent is not None and agent.workspace_id != workspace.id:
                agent = None
            return session_transcript(
                db, session_id, workspace, agent=agent,
                started_at=sessions[session_id], long_term=False, viewer=viewer,
                records_guard=records_guard,
            )
        except AppError as exc:
            return {"available": False, "reason": "not_found"
                    if exc.code == "observability.session_not_found" else "read_failed"}
        except Exception:
            return {"available": False, "reason": "read_failed"}
        finally:
            db.close()

    ids = list(sessions)
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(PREVIEW_WORKERS, len(ids))) as pool:
        return dict(zip(ids, pool.map(one, ids), strict=True))


def transcript_preview(session_id: str, transcript: dict[str, Any]) -> dict[str, Any]:
    """One picker row: the first user input and the last agent reply (a thread
    list's "first input / last output"), clipped, plus the user-turn count."""
    turns = (transcript.get("turns") or []) if transcript.get("available") else []

    def texts(role: str) -> list[str]:
        return [
            str(turn.get("text") or "").strip()
            for turn in turns
            if str(turn.get("role") or "").lower() == role and str(turn.get("text") or "").strip()
        ]

    inputs, replies = texts("user"), texts("assistant")
    reason = None
    if not inputs and not replies:
        reason = transcript.get("reason") or "no_turns"
    return {
        "session_id": session_id,
        "available": reason is None,
        "reason": reason,
        "input": inputs[0][:PREVIEW_TEXT_CAP] if inputs else None,
        "output": replies[-1][:PREVIEW_TEXT_CAP] if replies else None,
        "user_turns": len(inputs),
    }


# ─── SCORE NOW (N12 E1-5): one session scored synchronously on the evaluation engine ────
def session_log_groups(
    session_id: str, hours: int, *, workspace: WorkspaceContext, logs: Any = None,
) -> tuple[list[str], int]:
    """(log groups the session's spans live in, span record count) — the runtime group(s)
    the records name (``resource.attributes.aws.log.group.names``) plus ``aws/spans`` when
    Transaction Search is on. Empty when no span of the session landed in the range."""
    _require(SESSION_ID_RE, session_id, "session id")
    sid = json.dumps(session_id)
    query = (
        f"{_spans_source()}\n"
        f"| filter attributes.session.id = {sid}\n"
        "| stats count(*) as records by resource.attributes.aws.log.group.names as log_group"
    )
    rows = run_insights_queries({"groups": query}, hours, logs=logs, workspace=workspace)["groups"]
    groups: list[str] = []
    count = 0
    for row in rows:
        try:
            count += int(float(row.get("records") or 0))
        except (TypeError, ValueError):
            pass
        group = str(row.get("log_group") or "").strip()
        if group and group not in groups:
            groups.append(group)
    if not groups:
        return [], count
    runtime_groups = sorted(g for g in groups if g != SPANS_LOG_GROUP)
    if get_settings().observability_spans_source == "runtime_logs":
        return runtime_groups or groups, count
    return runtime_groups + [SPANS_LOG_GROUP], count


def _ondemand_tokens(usage: Any) -> dict[str, int | None] | None:
    if not isinstance(usage, dict):
        return None
    return {"input": usage.get("inputTokens"), "output": usage.get("outputTokens"),
            "total": usage.get("totalTokens")}


def _ondemand_result(record: dict[str, Any], workspace: WorkspaceContext) -> dict[str, Any]:
    """The console's stable SCORE NOW row (the baseline ``normalize_result`` keys)."""
    evaluator_id = str(record.get("evaluator_id"))
    trace_id = record.get("trace_id")
    return {
        "evaluator_id": evaluator_id,
        "evaluator_name": evaluator_id.split(".", 1)[-1] if evaluator_id.startswith("Builtin.")
        else evaluator_id,
        "evaluator_arn": workspace.arn("bedrock-agentcore", f"evaluator/{evaluator_id}"),
        "value": record.get("score"),
        "label": record.get("label"),
        "explanation": record.get("explanation"),
        "span_context": {"traceId": trace_id} if trace_id else None,
        # P8.3: the unit's judge tokens in the console's {input, output, total} shape | null
        "token_usage": _ondemand_tokens(record.get("token_usage")),
        "error_code": record.get("error_type"),
        "error_message": record.get("error_message"),
    }


def evaluate_session(
    session_id: str, range_key: str, evaluator_ids: list[str],
    workspace: WorkspaceContext, *, logs: Any = None, engine: Any = None,
    records_guard: RecordsGuard | None = None,
    source_session_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Score one session synchronously with each evaluator on the evaluation engine.

    Not cached and not persisted (``persist=False``): every call re-reads the spans and
    re-runs the judges. A failed judgement is returned as an error row; an unknown
    evaluator is 422; a judge whose prompt reads ground truth is refused (a live session
    carries none); no spans yet → 409 ``observability.session_spans_missing``.
    """
    from app.evaluation import engine as engine_slot
    from app.evaluation import evaluators_local
    from app.evaluation.managed import managed_reference_gap

    hours = RANGE_HOURS[range_key]
    ids = list(dict.fromkeys(evaluator_ids))  # de-dupe, keep order
    if not ids or len(ids) > MAX_ON_DEMAND_EVALUATORS:
        raise AppError(
            "observability.too_many_evaluators",
            f"choose between 1 and {MAX_ON_DEMAND_EVALUATORS} evaluators",
            status_code=422,
        )
    db = SessionLocal()
    try:
        gap = managed_reference_gap(db, getattr(workspace, "id", None), ids, set())
        definitions = evaluators_local.resolve_definitions(db, workspace, ids)
    finally:
        db.close()
    # a judge whose prompt reads ground truth cannot score a live session (no reference
    # here) — refused before any span read or judge call
    for definition in definitions:
        placeholders = evaluators_local.ground_truth_placeholders(
            str(definition.get("instructions") or ""))
        if placeholders:
            gap.setdefault(str(definition["id"]), []).extend(placeholders)
    if gap:
        raise AppError(
            "observability.evaluator_needs_ground_truth",
            "; ".join(f"{e} reads " + ", ".join(f"{{{p}}}" for p in g) for e, g in gap.items())
            + " — a live session carries no such reference; run it on its Dataset instead",
            {"evaluators": gap}, status_code=422,
        )
    # Internal, server-resolved sources only; never supplied by the HTTP body.
    # Score each real runtime trajectory independently, retaining the logical
    # conversation as owner_id. Never rewrite raw session identities or combine
    # different principals into a synthetic trajectory.
    sources = list(dict.fromkeys(source_session_ids or [session_id]))
    log_groups: list[str] = []
    span_count = 0
    active_sources: list[str] = []
    for source_id in sources:
        groups, count = session_log_groups(source_id, hours, workspace=workspace, logs=logs)
        if groups:
            active_sources.append(source_id)
            log_groups.extend(group for group in groups if group not in log_groups)
            span_count += count
    if not log_groups:
        raise AppError(
            "observability.session_spans_missing",
            f"no span records found for session {session_id} in the last {range_key}",
            detail={
                "session_id": session_id,
                "range": range_key,
                "hint": (
                    "Spans reach CloudWatch a couple of minutes after the invoke "
                    "completes; retry shortly or widen the time range."
                ),
            },
            status_code=409,
        )
    engine = engine if engine is not None else engine_slot.get_engine()
    # eval accuracy item 1: the extraction is reconciled with the chat ledger's answers when
    # the session is a platform chat session (else the check reads "unverifiable")
    from app.evaluation.extraction_refs import references_for

    checks: dict[str, dict[str, Any]] = {}
    records = engine.evaluate_sessions(
        workspace, source="ondemand", owner_id=session_id, session_ids=active_sources,
        log_groups=log_groups, evaluator_ids=ids, ground_truth=None, judge_model_id=None,
        stop=None, progress=None, persist=False,
        references=references_for(getattr(workspace, "id", None), active_sources),
        on_check=checks.__setitem__,
        **({"records_guard": records_guard} if records_guard is not None else {}),
    )
    results = [_ondemand_result(record, workspace) for record in records]
    if source_session_ids is not None:
        for result, record in zip(results, records, strict=True):
            result["span_context"] = {
                **(result["span_context"] or {}), "sessionId": record.get("session_id"),
            }
    return {
        "session_id": session_id,
        "range": range_key,
        "span_count": span_count,
        "results": results,
        # additive: {status: ok|mismatch|unverifiable, source, turns_checked, mismatches[],
        # mismatch_count, anomalies}; null when the session's data could not be extracted
        "extraction_check": (checks.get(active_sources[0]) if len(active_sources) == 1
                             else None),
        **({
            "source_session_ids": active_sources,
            # Preserve individual checks rather than inventing a combined score or
            # applying the complete chat ledger to each partial runtime trajectory.
            "extraction_checks": {sid: checks.get(sid) for sid in active_sources},
        } if source_session_ids is not None else {}),
    }
