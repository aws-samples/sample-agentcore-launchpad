"""CloudWatch readiness gate for fresh AgentCore evaluation sessions."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ROOT_SPAN_NAME = "invoke_agent Strands Agents"
CLAUDE_ROOT_SPAN_NAME = "ClaudeAgentSDK.query"
CLAUDE_SCOPE_NAME = "openinference.instrumentation.claude_agent_sdk"
CLAUDE_SPAN_KIND = "AGENT"


@dataclass(frozen=True)
class TelemetryProbe:
    root_span_id: str | None
    content_ingestion_ms: int | None
    framework: str | None = None
    detail: str | None = None

    @property
    def readiness_ingestion_ms(self) -> int | None:
        return self.content_ingestion_ms

    @property
    def paired(self) -> bool:
        return self.root_span_id is not None and self.readiness_ingestion_ms is not None


class TelemetryReadinessTimeout(RuntimeError):
    """The newest evaluation session never became safe to submit."""


def _filter_events(
    client: Any,
    *,
    log_group: str,
    session_id: str,
    start_time_ms: int | None,
) -> list[dict[str, Any]]:
    pattern = json.dumps(session_id)
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "filterPattern": pattern,
        "limit": 10_000,
    }
    if start_time_ms is not None:
        kwargs["startTime"] = start_time_ms
    events: list[dict[str, Any]] = []
    previous_token: str | None = None
    while True:
        response = client.filter_log_events(**kwargs)
        events.extend(response.get("events", []))
        token = response.get("nextToken")
        if not token or token == previous_token:
            return events
        previous_token = token
        kwargs["nextToken"] = token


def _json_message(event: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(event.get("message") or "")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _event_time_ms(event: dict[str, Any], key: str = "timestamp") -> int:
    try:
        return int(event.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _native_root_issue(message: dict[str, Any], session_id: str) -> str | None:
    attributes = message.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    scope = message.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    missing: list[str] = []
    if scope.get("name") != CLAUDE_SCOPE_NAME:
        missing.append(f"scope {CLAUDE_SCOPE_NAME}")
    if attributes.get("openinference.span.kind") != CLAUDE_SPAN_KIND:
        missing.append("openinference.span.kind=AGENT")
    if attributes.get("session.id") != session_id:
        missing.append("matching session.id")
    if not _positive_int(message.get("endTimeUnixNano")):
        missing.append("completion timestamp")
    if not missing:
        return None
    return "native Claude root span is incomplete: missing " + ", ".join(missing)


def probe_latest_session(
    client: Any,
    *,
    session_id: str,
    content_log_group: str,
    start_time_ms: int | None = None,
) -> TelemetryProbe:
    """Find the newest evaluation-ready root across supported frameworks."""
    strands_roots: list[tuple[int, str]] = []
    native_roots: list[tuple[int, str]] = []
    incomplete_native_roots: list[tuple[int, str, str]] = []
    for event in _filter_events(
        client,
        log_group="aws/spans",
        session_id=session_id,
        start_time_ms=start_time_ms,
    ):
        message = _json_message(event)
        if not message:
            continue
        attributes = message.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        span_id = message.get("spanId")
        if attributes.get("session.id") != session_id or not isinstance(span_id, str):
            continue
        timestamp_ms = _event_time_ms(event)
        if message.get("name") == ROOT_SPAN_NAME:
            strands_roots.append((timestamp_ms, span_id))
        elif message.get("name") == CLAUDE_ROOT_SPAN_NAME:
            issue = _native_root_issue(message, session_id)
            if issue is None:
                native_roots.append((timestamp_ms, span_id))
            else:
                incomplete_native_roots.append((timestamp_ms, span_id, issue))

    latest_strands = max(strands_roots, default=None)
    latest_native = max(native_roots, default=None)
    latest_incomplete_native = max(incomplete_native_roots, default=None)
    latest_native_timestamp = max(
        latest_native[0] if latest_native is not None else -1,
        latest_incomplete_native[0] if latest_incomplete_native is not None else -1,
    )
    framework: str
    latest_span_id: str
    if latest_native_timestamp > (
        latest_strands[0] if latest_strands is not None else -1
    ):
        if (
            latest_incomplete_native is not None
            and latest_incomplete_native[0] == latest_native_timestamp
        ):
            _, span_id, issue = latest_incomplete_native
            return TelemetryProbe(
                root_span_id=span_id,
                content_ingestion_ms=None,
                framework="claude_agent_sdk",
                detail=issue,
            )
        assert latest_native is not None
        _, latest_span_id = latest_native
        framework = "claude_agent_sdk"
    else:
        if latest_strands is None:
            return TelemetryProbe(root_span_id=None, content_ingestion_ms=None)
        _, latest_span_id = latest_strands
        framework = "strands"

    matching_ingestion: list[int] = []
    for event in _filter_events(
        client,
        log_group=content_log_group,
        session_id=session_id,
        start_time_ms=start_time_ms,
    ):
        message = _json_message(event)
        if not message or message.get("spanId") != latest_span_id:
            continue
        attributes = message.get("attributes") or {}
        body = message.get("body")
        scope = message.get("scope") or {}
        if (
            attributes.get("session.id") == session_id
            and isinstance(body, dict)
            and "input" in body
            and "output" in body
            and (
                framework != "claude_agent_sdk"
                or scope.get("name") == CLAUDE_SCOPE_NAME
            )
        ):
            matching_ingestion.append(
                int(event.get("ingestionTime") or event.get("timestamp") or 0)
            )
    return TelemetryProbe(
        root_span_id=latest_span_id,
        content_ingestion_ms=max(matching_ingestion, default=None),
        framework=framework,
        detail=(
            None
            if matching_ingestion
            else f"content log for {framework} span {latest_span_id} is missing"
        ),
    )


def wait_for_evaluation_telemetry(
    client: Any,
    *,
    session_id: str,
    content_log_group: str,
    start_time_ms: int | None = None,
    stability_seconds: int,
    timeout_seconds: int,
    poll_seconds: float = 5.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> TelemetryProbe:
    """Wait until the newest supported root is complete and stable for Evaluation.

    CloudWatch can expose telemetry before AgentCore Evaluation's internal index
    can consume it. Since Evaluation has no readiness API, the selected framework's
    ingestion age is the conservative index watermark.
    """
    deadline = clock() + timeout_seconds
    last_probe = TelemetryProbe(root_span_id=None, content_ingestion_ms=None)
    while True:
        last_probe = probe_latest_session(
            client,
            session_id=session_id,
            content_log_group=content_log_group,
            start_time_ms=start_time_ms,
        )
        now = clock()
        readiness_ingestion_ms = last_probe.readiness_ingestion_ms
        if last_probe.paired and readiness_ingestion_ms is not None:
            age_seconds = max(0.0, now - readiness_ingestion_ms / 1000)
            if age_seconds >= stability_seconds:
                return last_probe
        if now >= deadline:
            if last_probe.detail:
                detail = last_probe.detail
            elif last_probe.root_span_id is None:
                detail = "root span is missing"
            elif readiness_ingestion_ms is None:
                detail = f"content log for span {last_probe.root_span_id} is missing"
            else:
                age = max(0.0, now - readiness_ingestion_ms / 1000)
                detail = (
                    f"content log age is {age:.1f}s, below the required "
                    f"{stability_seconds}s"
                )
            raise TelemetryReadinessTimeout(
                f"evaluation telemetry did not become ready for session {session_id}: {detail}"
            )
        sleep(min(poll_seconds, max(0.0, deadline - now)))
