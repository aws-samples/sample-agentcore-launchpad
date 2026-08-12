"""Session trace queries — spans read through the shared Logs Insights transport.

This module used to have its own reader: `filter_log_events` against `aws/spans` alone.
That API scans forward from `startTime` with bounded pagination, so it returned **nothing
at all** once the lookback was more than a few hours — measured on a real session whose
32 spans were sitting in `aws/spans` the whole time, `?lookback_hours=72` gave 0. That is
the defect this replaces.

It now reuses `observability.SPANS_SOURCE`, the same multi-group source every
Observability query already uses, which also makes the rail forward-compatible with
AgentCore's per-agent span destination (documented as the default for newer agents).
Note that in this account per-agent runtime groups currently hold **no spans** — only
application logs and OTEL log records — which is exactly why the query must filter for
actual spans; see `session_span_query`.

Span *shaping* (`normalize_spans`, `categorize`, `_span_times`) stays here; only the
fetch is shared.
"""

import json
from typing import Any

from app.core.errors import AppError
from app.services.observability import SPANS_SOURCE, run_insights_queries
from app.services.workspace import WorkspaceContext

# Only used for the fallback console deep link when nothing was found.
SPANS_LOG_GROUP = "aws/spans"
SPAN_LIMIT = 200

CATEGORY_RULES = [
    ("policy", ("policy", "authorize")),
    ("memory", ("memory", "createevent", "listevents", "retrieve", "listactors",
                "listsessions")),
    ("tool", ("tool", "hr-database", "office-facts", "gateway", "mcp")),
    ("model", ("chat", "invoke_model", "converse", "anthropic", "bedrock-runtime", "gen_ai")),
    ("runtime", ("invoke_harness", "invokeharness", "runtime", "invocations", "harness",
                 "event_loop")),
]


def session_span_query(session_id: str) -> str:
    """Substring match on the raw message, deliberately — not
    ``attributes.session.id`` equality. The reader this replaced matched any span whose
    text contained the id, and the rail's value is the *whole* call tree; tightening
    that would quietly show fewer spans than before.

    The ``name`` clause is the other half: a per-agent runtime log group holds spans
    **mixed with** structured application logs and OTEL log records (``body`` /
    ``severityText`` / ``spanId``, no ``name``). Those also mention the session id, so
    without this they would land in the rail as nameless "span" rows.
    """
    return (
        f"{SPANS_SOURCE}"
        " | fields @message, @log"
        f' | filter @message like "{session_id}"'
        " | filter ispresent(name) or ispresent(spanName)"
        " | sort @timestamp desc"
        f" | limit {SPAN_LIMIT}"
    )


def _is_span(obj: Any) -> bool:
    """Client-side guard mirroring the query's ``name`` clause, so a future query edit
    cannot silently readmit log records into the rail."""
    return isinstance(obj, dict) and bool(obj.get("name") or obj.get("spanName"))


def find_session_spans(
    session_id: str,
    workspace: WorkspaceContext,
    lookback_hours: int = 3,
    logs: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``(spans, spans-per-contributing-log-group)``.

    Raises nothing on query failure — that is handled by ``session_trace``, which needs
    to degrade rather than error out a side panel on the Chat page.
    """
    rows = run_insights_queries(
        {"spans": session_span_query(session_id)},
        lookback_hours,
        logs=logs,
        workspace=workspace,
    ).get("spans") or []
    spans: list[dict[str, Any]] = []
    per_group: dict[str, int] = {}
    for row in rows:
        raw = row.get("@message")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            # Plain (non-JSON) application log lines live in the same group and also
            # mention the session id.
            continue
        if not _is_span(parsed):
            continue
        spans.append(parsed)
        # `@log` is an ARN-ish identifier; the group name is the trailing segment.
        group = (row.get("@log") or "").split(":")[-1]
        if group:
            per_group[group] = per_group.get(group, 0) + 1
    return spans, per_group


def _span_times(span: dict[str, Any]) -> tuple[float | None, float | None]:
    """(start_ms, end_ms) tolerant of OTEL export shape variations."""
    for start_key, end_key, scale in (
        ("startTimeUnixNano", "endTimeUnixNano", 1e6),
        ("startTime", "endTime", 1.0),
    ):
        start, end = span.get(start_key), span.get(end_key)
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return float(start) / scale, float(end) / scale
        if isinstance(start, str) and isinstance(end, str):
            try:
                return float(start) / scale, float(end) / scale
            except ValueError:
                continue
    return None, None


def categorize(name: str) -> str:
    lowered = name.lower()
    for category, needles in CATEGORY_RULES:
        if any(needle in lowered for needle in needles):
            return category
    return "other"


def normalize_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for span in spans:
        name = str(span.get("name") or span.get("spanName") or "span")
        start_ms, end_ms = _span_times(span)
        rows.append(
            {
                "name": name[:80],
                "category": categorize(name),
                "start_ms": start_ms,
                "duration_ms": (end_ms - start_ms) if start_ms and end_ms else None,
                "trace_id": span.get("traceId") or span.get("trace_id"),
            }
        )
    timed = [r for r in rows if r["start_ms"] is not None]
    if timed:
        origin = min(r["start_ms"] for r in timed)
        for r in rows:
            r["start_ms"] = round(r["start_ms"] - origin, 1) if r["start_ms"] else 0.0
            if r["duration_ms"] is not None:
                r["duration_ms"] = round(r["duration_ms"], 1)
    rows.sort(key=lambda r: r["start_ms"] or 0)
    return rows


def session_trace(
    session_id: str,
    workspace: WorkspaceContext,
    lookback_hours: int = 3,
    logs: Any = None,
) -> dict[str, Any]:
    region = workspace.region
    unavailable_reason = None
    try:
        raw, per_group = find_session_spans(
            session_id, workspace, lookback_hours, logs=logs
        )
    except AppError as exc:
        # The rail is a side panel on the Chat page: a Logs Insights hiccup must not
        # turn chatting into an error state.
        raw, per_group, unavailable_reason = [], {}, exc.code
    spans = normalize_spans(raw)
    # `log_group` now means "the group that contributed the most spans" rather than
    # always aws/spans, which is strictly more truthful once several contribute.
    primary = max(per_group, key=lambda g: per_group[g]) if per_group else SPANS_LOG_GROUP
    return {
        "session_id": session_id,
        "span_count": len(spans),
        "spans": spans,
        "log_groups": sorted(per_group, key=lambda g: -per_group[g]),
        "log_group": primary,
        "cloudwatch_url": _log_group_url(region, primary),
        "unavailable_reason": unavailable_reason,
    }


def _log_group_url(region: str, log_group: str) -> str:
    escaped = log_group.replace("/", "$252F")
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#logsV2:log-groups/log-group/{escaped}"
    )
