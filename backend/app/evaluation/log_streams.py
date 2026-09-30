"""Console V2 评估任务 · 日志 data source — the log streams of an agent's runtime
log group, optionally filtered by a keyword.

A code runtime writes one stream per session, named
``YYYY/MM/DD/[runtime-logs-<sessionId>]<uuid>``, plus the shared ``otel-rt-logs``
stream that carries every session's OTel content logs (each event names its
session in ``attributes["session.id"]``). A managed Harness runtime names its
streams per microVM (``[runtime-logs]<uuid>``, no session id), so there the
sessions exist only inside ``otel-rt-logs``: each is listed as that stream's
per-session slice (``kind="otel_session"``). Any other log group (an agent that
is not on AgentCore Runtime, evaluated from its CloudWatch telemetry alone) is
treated the same way: with no stream named after a session, sessions are found
by the ``session.id`` their OTel events carry, in whichever stream. Either way a
selectable row maps to a session a batch evaluation can score; the chosen rows
are submitted as an ordinary ``session_ids`` run scope.

A keyword matches a stream by name (case-insensitive, e.g. a session id) or by
content: ``FilterLogEvents`` with the keyword as a quoted term (CloudWatch term
matching is case-sensitive), where an ``otel-rt-logs`` hit is credited to the
session its event names. Both scans are capped so one request stays bounded on
a busy log group; ``truncated`` says a cap was hit.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

SESSION_STREAM_RE = re.compile(r"\[runtime-logs-(?P<sid>[^\]]+)\]")
MAX_STREAMS = 500
_DESCRIBE_PAGE = 50
MAX_FILTER_PAGES = 10
OTEL_STREAM = "otel-rt-logs"
_SNIPPET_CHARS = 160


def session_of_stream(name: str) -> str | None:
    match = SESSION_STREAM_RE.search(name)
    return match.group("sid") if match else None


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms else None


_ESCAPED_BREAKS = re.compile(r"(?:\\+[nrt]|\s)+")
_ESCAPED_QUOTE = re.compile(r'\\+"')


def _snippet(message: str, keyword: str) -> str:
    """An excerpt around the keyword; OTel events are JSON (often JSON inside
    JSON), so escaped line breaks and quotes are flattened for display."""
    at = message.find(keyword)
    if at < 0:
        at = message.lower().find(keyword.lower())
    start = max(0, at - _SNIPPET_CHARS // 3) if at >= 0 else 0
    text = message[start : start + _SNIPPET_CHARS]
    text = _ESCAPED_QUOTE.sub('"', _ESCAPED_BREAKS.sub(" ", text))
    return ("…" if start else "") + text.strip()


def _event_session(message: str) -> str | None:
    """``session.id`` of an OTel content-log event (``otel-rt-logs``), if any."""
    try:
        record = json.loads(message)
    except ValueError:
        return None
    attrs = record.get("attributes") if isinstance(record, dict) else None
    sid = attrs.get("session.id") if isinstance(attrs, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _recent_streams(logs: Any, log_group: str, since_ms: int) -> tuple[list[dict], bool]:
    """Streams with an event since ``since_ms``, newest first (capped)."""
    streams: list[dict] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {
            "logGroupName": log_group,
            "orderBy": "LastEventTime",
            "descending": True,
            "limit": _DESCRIBE_PAGE,
        }
        if token:
            kwargs["nextToken"] = token
        page = logs.describe_log_streams(**kwargs)
        for stream in page.get("logStreams", []):
            last = stream.get("lastEventTimestamp") or stream.get("creationTime") or 0
            if last < since_ms:
                return streams, False
            streams.append(stream)
            if len(streams) >= MAX_STREAMS:
                return streams, True
        token = page.get("nextToken")
        if not token:
            return streams, False


def _term(keyword: str) -> str:
    """A quoted filter-pattern term: the keyword is matched literally, spaces and all."""
    return '"' + keyword.replace('"', " ").strip() + '"'


def _scan(
    logs: Any,
    log_group: str,
    since_ms: int,
    keyword: str,
    streams: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Events containing ``keyword``, keyed by session id (or by stream name for
    an event no session claims): ``{"matches", "snippet", "otel", "first", "last"}``
    where ``otel`` says the session was seen through ``otel-rt-logs``."""
    hits: dict[str, dict[str, Any]] = {}
    token = None
    for _ in range(MAX_FILTER_PAGES):
        kwargs: dict[str, Any] = {
            "logGroupName": log_group,
            "startTime": since_ms,
            "filterPattern": _term(keyword),
        }
        if streams:
            kwargs["logStreamNames"] = streams
        if token:
            kwargs["nextToken"] = token
        page = logs.filter_log_events(**kwargs)
        for event in page.get("events", []):
            stream = event.get("logStreamName") or ""
            message = event.get("message") or ""
            sid = session_of_stream(stream)
            otel_sid = None if sid else _event_session(message)
            key = sid or otel_sid or stream
            ts = event.get("timestamp") or 0
            hit = hits.setdefault(key, {
                "matches": 0, "snippet": _snippet(message, keyword), "otel": False,
                "first": ts, "last": ts, "stream": stream,
            })
            hit["matches"] += 1
            hit["otel"] = hit["otel"] or bool(otel_sid)
            hit["first"], hit["last"] = min(hit["first"], ts), max(hit["last"], ts)
        token = page.get("nextToken")
        if not token:
            return hits, False
    return hits, True


def _row(
    stream: str, sid: str | None, kind: str, first: int | None, last: int | None,
    needle: str, hit: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "stream": stream,
        "session_id": sid,
        "kind": kind,
        "first_event": _iso(first),
        "last_event": _iso(last),
        "match": ("content" if hit else "name") if needle else None,
        "matches": hit["matches"] if hit else None,
        "snippet": hit["snippet"] if hit else None,
    }


def list_streams(
    logs: Any,
    log_group: str,
    *,
    since_ms: int,
    keyword: str | None = None,
    visible: Any = None,
) -> dict[str, Any]:
    """Rows for the task wizard, newest first: every recent stream (``kind``
    ``session`` carries its ``session_id``; ``shared`` ones do not), plus the
    ``otel_session`` slices of sessions that own no stream, narrowed to rows
    matching ``keyword`` when given. ``visible(session_id)`` hides a session the
    caller may not read (another principal's private assistant session)."""
    try:
        streams, truncated = _recent_streams(logs, log_group, since_ms)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return {"log_group": log_group, "streams": [], "truncated": False}
        raise
    needle = (keyword or "").strip()
    # No stream is named after a session (a Harness runtime, or a log group
    # outside AgentCore Runtime): sessions are known only by their events'
    # session.id — on a runtime all of them sit in otel-rt-logs.
    sessionless = bool(streams) and not any(session_of_stream(s["logStreamName"]) for s in streams)
    scan_streams = (
        [OTEL_STREAM]
        if sessionless and any(s["logStreamName"] == OTEL_STREAM for s in streams)
        else None
    )
    if sessionless:
        truncated = False  # the stream cap hides no session: the event scan finds them
    hits: dict[str, dict[str, Any]] = {}
    if needle or sessionless:
        hits, filter_truncated = _scan(
            logs, log_group, since_ms, needle or "session.id", scan_streams
        )
        truncated = truncated or filter_truncated
    rows: list[dict[str, Any]] = []
    owned: set[str] = set()
    for stream in streams:
        name = stream["logStreamName"]
        sid = session_of_stream(name)
        if sid:
            owned.add(sid)
        if sid and visible is not None and not visible(sid):
            continue
        hit = hits.get(sid or name)
        if needle and not (needle.lower() in name.lower() or hit):
            continue
        rows.append(_row(
            name, sid, "session" if sid else "shared",
            stream.get("firstEventTimestamp"), stream.get("lastEventTimestamp"), needle, hit,
        ))
    for sid, hit in hits.items():
        if not hit["otel"] or sid in owned or (visible is not None and not visible(sid)):
            continue
        rows.append(_row(hit["stream"], sid, "otel_session", hit["first"], hit["last"],
                         needle, hit if needle else None))
    rows.sort(key=lambda r: r["last_event"] or "", reverse=True)
    return {"log_group": log_group, "streams": rows, "truncated": truncated}


# ─── CloudWatch-only sources (no platform agent) ────────────────────────────
MAX_LOG_GROUPS = 150
_LOG_GROUP_PAGE = 50
SPANS_LOG_GROUP = "aws/spans"
MAX_SERVICES = 100

# Services whose spans landed in the given log groups: span / session counts,
# last activity and the content log group(s) ADOT names on the resource
# (`aws.log.group.names`), which is what a batch evaluation reads besides spans.
SERVICES_QUERY = f"""
fields resource.attributes.service.name as service,
       resource.attributes.aws.log.group.names as groups
| filter ispresent(startTimeUnixNano) and ispresent(service)
| stats count(*) as spans, count_distinct(attributes.session.id) as sessions,
        max(endTimeUnixNano) as last_ns, latest(groups) as log_groups
  by service
| sort last_ns desc
| limit {MAX_SERVICES}
"""


# Per-service instrumentation scopes: AgentCore Evaluation reads a span only
# by its `scope.name` (the framework scopes of the supported-frameworks table,
# or the generic OpenTelemetry GenAI / OpenInference prefixes) — a custom
# scope fails every session with "No evaluable agent spans found".
SCOPES_QUERY = f"""
fields resource.attributes.service.name as service
| filter ispresent(startTimeUnixNano) and ispresent(service) and ispresent(scope.name)
| stats count(*) as spans by service, scope.name as scope
| limit {MAX_SERVICES * 20}
"""
FRAMEWORK_SCOPES = frozenset({"strands.telemetry.tracer", "strands-agents"})
FRAMEWORK_SCOPE_PREFIXES = (
    "opentelemetry.instrumentation.",
    "openinference.instrumentation.",
    "@aws/aws-distro-opentelemetry-instrumentation-",
    "@traceloop/instrumentation-",
    "@arizeai/openinference-instrumentation-",
)
# generic-prefix scopes that instrument transport, not the agent itself
INFRA_SCOPE_SUFFIXES = (
    "botocore", "boto3", "starlette", "fastapi", "asgi", "wsgi", "httpx", "requests",
    "urllib", "urllib3", "aiohttp", "aiohttp_client", "grpc", "flask", "django",
    "sqlalchemy", "redis", "logging", "threading", "asyncio",
)


def evaluable_scope(scope: str) -> bool:
    """Whether AgentCore Evaluation reads spans of this instrumentation scope as
    agent spans (supported-frameworks table + generic framework support)."""
    if scope in FRAMEWORK_SCOPES:
        return True
    if not scope.startswith(FRAMEWORK_SCOPE_PREFIXES):
        return False
    tail = scope.split("instrumentation", 1)[-1].lstrip(".-_")
    return not any(tail == s or tail.startswith(f"{s}.") for s in INFRA_SCOPE_SUFFIXES)


def list_log_groups(logs: Any, pattern: str | None = None) -> dict[str, Any]:
    """Log groups of the workspace, optionally those whose name contains
    ``pattern`` (DescribeLogGroups' case-insensitive `logGroupNamePattern`)."""
    groups: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {"limit": _LOG_GROUP_PAGE}
        if pattern:
            kwargs["logGroupNamePattern"] = pattern
        if token:
            kwargs["nextToken"] = token
        page = logs.describe_log_groups(**kwargs)
        for group in page.get("logGroups", []):
            groups.append({
                "name": group["logGroupName"],
                "created_at": _iso(group.get("creationTime")),
                "retention_days": group.get("retentionInDays"),
                "stored_bytes": group.get("storedBytes"),
            })
        token = page.get("nextToken")
        if not token or len(groups) >= MAX_LOG_GROUPS:
            return {"log_groups": groups[:MAX_LOG_GROUPS], "truncated": bool(token)}


def _split_groups(value: str | None) -> list[str]:
    return [g for g in re.split(r"[,&]", value or "") if g.strip()] if value else []


def services_from_rows(
    rows: list[dict[str, str]],
    resolve: Any = None,
    scope_rows: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Shape SERVICES_QUERY rows; ``resolve(service)`` names the platform agent
    that owns a service, when one does; ``scope_rows`` (SCOPES_QUERY) add each
    service's instrumentation scopes and whether any is evaluable
    (``evaluable`` is ``None`` when the scopes are unknown)."""
    scopes: dict[str, list[str]] = {}
    for row in scope_rows or []:
        if row.get("service") and row.get("scope"):
            scopes.setdefault(row["service"], []).append(row["scope"])
    out = []
    for row in rows:
        service = row.get("service") or ""
        if not service:
            continue
        content = [g.strip() for g in _split_groups(row.get("log_groups"))]
        suggested = list(dict.fromkeys([SPANS_LOG_GROUP, *content]))
        last_ns = row.get("last_ns")
        agent = resolve(service) if resolve else None
        out.append({
            "service_name": service,
            "spans": int(float(row.get("spans") or 0)),
            "sessions": int(float(row.get("sessions") or 0)),
            "last_seen": _iso(int(float(last_ns)) // 1_000_000) if last_ns else None,
            "log_group_names": suggested,
            "agent": {"id": agent.id, "name": agent.name} if agent is not None else None,
            "scopes": sorted(scopes.get(service, [])),
            "evaluable": (
                any(evaluable_scope(sc) for sc in scopes[service]) if service in scopes else None
            ),
        })
    return out


MAX_LOG_SESSIONS = 500  # a batch evaluation's session cap


def sessions_query(service_name: str) -> str:
    """Sessions of one service in the input log groups — from its spans, the
    one record type that always carries both service.name and session.id (an
    agent outside AgentCore Runtime may write content logs without either).
    ``service_name`` is pattern-validated by the route (no quotes)."""
    return f"""
filter resource.attributes.service.name = "{service_name}" and ispresent(attributes.session.id)
| stats count(*) as records, count_distinct(traceId) as traces,
        min(@timestamp) as first, max(@timestamp) as last,
        latest(@log) as log, latest(@logStream) as stream
  by attributes.session.id as session_id
| sort last desc
| limit {MAX_LOG_SESSIONS}
"""


def keyword_query(keyword: str) -> str:
    """Per-session hits of a case-insensitive keyword in any record (span or
    content log) of the input log groups."""
    pattern = re.escape(keyword.strip()).replace("/", "\\/")
    return f"""
filter @message like /(?i){pattern}/ and ispresent(attributes.session.id)
| stats count(*) as hits, latest(@message) as sample by attributes.session.id as session_id
| limit {MAX_LOG_SESSIONS}
"""


def _insights_iso(value: str | None) -> str | None:
    """Logs Insights `@timestamp` ("2026-09-29 06:37:07.936", UTC) → ISO 8601."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


def sessions_from_rows(
    rows: list[dict[str, str]],
    hits: list[dict[str, str]] | None = None,
    *,
    keyword: str | None = None,
    visible: Any = None,
) -> dict[str, Any]:
    """The task wizard's stream-row shape (``kind="session"``) for sessions
    found in spans, narrowed to those with keyword ``hits`` when given."""
    by_session = {h.get("session_id"): h for h in hits or [] if h.get("session_id")}
    out = []
    for row in rows:
        sid = row.get("session_id")
        if not sid or (visible is not None and not visible(sid)):
            continue
        hit = by_session.get(sid)
        if keyword and hit is None:
            continue
        group = (row.get("log") or "").split(":", 1)[-1]  # "<account>:<group>"
        out.append({
            "stream": f"{group} · {row.get('stream') or '—'}",
            "session_id": sid,
            "kind": "session",
            "first_event": _insights_iso(row.get("first")),
            "last_event": _insights_iso(row.get("last")),
            "match": "content" if hit else None,
            "matches": int(float(hit["hits"])) if hit else None,
            "snippet": _snippet(hit.get("sample") or "", keyword or "") if hit else None,
            "traces": int(float(row.get("traces") or 0)),
        })
    return {"streams": out, "truncated": len(rows) >= MAX_LOG_SESSIONS}
