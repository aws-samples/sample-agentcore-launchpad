"""CloudWatch readiness gate for fresh AgentCore evaluation sessions."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ROOT_SPAN_NAME = "invoke_agent Strands Agents"


@dataclass(frozen=True)
class TelemetryProbe:
    root_span_id: str | None
    content_ingestion_ms: int | None

    @property
    def paired(self) -> bool:
        return self.root_span_id is not None and self.content_ingestion_ms is not None


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


def probe_latest_session(
    client: Any,
    *,
    session_id: str,
    content_log_group: str,
    start_time_ms: int | None = None,
) -> TelemetryProbe:
    """Pair the newest root span with its structured input/output log event."""
    roots: list[tuple[int, str]] = []
    for event in _filter_events(
        client,
        log_group="aws/spans",
        session_id=session_id,
        start_time_ms=start_time_ms,
    ):
        message = _json_message(event)
        if not message or message.get("name") != ROOT_SPAN_NAME:
            continue
        attributes = message.get("attributes") or {}
        span_id = message.get("spanId")
        if attributes.get("session.id") == session_id and isinstance(span_id, str):
            roots.append((int(event.get("timestamp") or 0), span_id))
    if not roots:
        return TelemetryProbe(root_span_id=None, content_ingestion_ms=None)

    _, latest_span_id = max(roots)
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
        if (
            attributes.get("session.id") == session_id
            and isinstance(body, dict)
            and "input" in body
            and "output" in body
        ):
            matching_ingestion.append(
                int(event.get("ingestionTime") or event.get("timestamp") or 0)
            )
    return TelemetryProbe(
        root_span_id=latest_span_id,
        content_ingestion_ms=max(matching_ingestion, default=None),
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
    """Wait until the newest session is paired and old enough for Evaluation.

    CloudWatch can expose a span and content event before AgentCore Evaluation's
    internal index can consume them. Since Evaluation has no readiness API, the
    matching content event's ingestion age is the conservative index watermark.
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
        if last_probe.paired:
            age_seconds = max(0.0, now - last_probe.content_ingestion_ms / 1000)
            if age_seconds >= stability_seconds:
                return last_probe
        if now >= deadline:
            if last_probe.root_span_id is None:
                detail = "root span is missing"
            elif last_probe.content_ingestion_ms is None:
                detail = f"content log for span {last_probe.root_span_id} is missing"
            else:
                age = max(0.0, now - last_probe.content_ingestion_ms / 1000)
                detail = (
                    f"content log age is {age:.1f}s, below the required "
                    f"{stability_seconds}s"
                )
            raise TelemetryReadinessTimeout(
                f"evaluation telemetry did not become ready for session {session_id}: {detail}"
            )
        sleep(min(poll_seconds, max(0.0, deadline - now)))
