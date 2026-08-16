"""Readiness gate between fresh runtime traffic and StartBatchEvaluation."""

import json

import pytest

from app.evaluation.telemetry import (
    TelemetryReadinessTimeout,
    probe_latest_session,
    wait_for_evaluation_telemetry,
)

SESSION_ID = "session-" + "a" * 32
CONTENT_GROUP = "/aws/bedrock-agentcore/runtimes/test-runtime-DEFAULT"


def _event(message, *, timestamp=1_000_000, ingestion=1_001_000):
    return {
        "message": json.dumps(message),
        "timestamp": timestamp,
        "ingestionTime": ingestion,
    }


def _root(span_id, *, timestamp):
    return _event(
        {
            "name": "invoke_agent Strands Agents",
            "spanId": span_id,
            "attributes": {"session.id": SESSION_ID},
        },
        timestamp=timestamp,
        ingestion=timestamp + 100,
    )


def _content(span_id, *, timestamp, ingestion):
    return _event(
        {
            "spanId": span_id,
            "attributes": {"session.id": SESSION_ID},
            "body": {"input": {"messages": []}, "output": {"messages": []}},
        },
        timestamp=timestamp,
        ingestion=ingestion,
    )


def _native_root(
    span_id,
    *,
    timestamp,
    ingestion,
    completed=True,
):
    attributes = {
        "session.id": SESSION_ID,
        "openinference.span.kind": "AGENT",
    }
    message = {
        "name": "ClaudeAgentSDK.query",
        "spanId": span_id,
        "scope": {"name": "openinference.instrumentation.claude_agent_sdk"},
        "attributes": attributes,
    }
    if completed:
        message["endTimeUnixNano"] = timestamp * 1_000_000
    return _event(message, timestamp=timestamp, ingestion=ingestion)


def _native_content(span_id, *, timestamp, ingestion):
    return _event(
        {
            "spanId": span_id,
            "scope": {"name": "openinference.instrumentation.claude_agent_sdk"},
            "attributes": {"session.id": SESSION_ID},
            "body": {
                "input": {"messages": [{"role": "user", "content": "hello"}]},
                "output": {"messages": [{"role": "assistant", "content": "hi"}]},
            },
        },
        timestamp=timestamp,
        ingestion=ingestion,
    )


class StubLogs:
    def __init__(self, spans=None, content=None):
        self.spans = spans or []
        self.content = content or []
        self.calls = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        events = self.spans if kwargs["logGroupName"] == "aws/spans" else self.content
        return {"events": events}


def test_probe_requires_content_for_latest_root_span():
    logs = StubLogs(
        spans=[_root("old-span", timestamp=1_000), _root("new-span", timestamp=2_000)],
        content=[_content("old-span", timestamp=1_000, ingestion=1_100)],
    )

    probe = probe_latest_session(
        logs,
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
        start_time_ms=500,
    )

    assert probe.root_span_id == "new-span"
    assert probe.content_ingestion_ms is None
    assert probe.paired is False
    assert all(call["startTime"] == 500 for call in logs.calls)


def test_probe_pairs_completed_native_claude_root_with_native_content_log():
    logs = StubLogs(
        spans=[_native_root("native-span", timestamp=2_000, ingestion=2_100)],
        content=[_native_content("native-span", timestamp=2_000, ingestion=2_200)],
    )

    probe = probe_latest_session(
        logs,
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
        start_time_ms=500,
    )

    assert probe.root_span_id == "native-span"
    assert probe.framework == "claude_agent_sdk"
    assert probe.content_ingestion_ms == 2_200
    assert probe.readiness_ingestion_ms == 2_200
    assert probe.paired is True
    assert [call["logGroupName"] for call in logs.calls] == [
        "aws/spans",
        CONTENT_GROUP,
    ]


def test_probe_requires_native_content_log():
    logs = StubLogs(
        spans=[_native_root("native-span", timestamp=2_000, ingestion=2_100)],
    )

    probe = probe_latest_session(
        logs,
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
    )

    assert probe.root_span_id == "native-span"
    assert probe.framework == "claude_agent_sdk"
    assert probe.paired is False
    assert probe.detail is not None
    assert "content log for claude_agent_sdk span native-span is missing" in probe.detail


def test_probe_rejects_incomplete_native_claude_root():
    logs = StubLogs(
        spans=[
            _native_root(
                "native-span",
                timestamp=2_000,
                ingestion=2_100,
                completed=False,
            )
        ],
    )

    probe = probe_latest_session(
        logs,
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
    )

    assert probe.root_span_id == "native-span"
    assert probe.framework == "claude_agent_sdk"
    assert probe.paired is False
    assert probe.detail is not None
    assert "completion timestamp" in probe.detail


def test_wait_reports_incomplete_native_claude_root():
    logs = StubLogs(
        spans=[
            _native_root(
                "native-span",
                timestamp=2_000,
                ingestion=2_100,
                completed=False,
            )
        ],
    )

    with pytest.raises(
        TelemetryReadinessTimeout,
        match="native Claude root span is incomplete: missing completion timestamp",
    ):
        wait_for_evaluation_telemetry(
            logs,
            session_id=SESSION_ID,
            content_log_group=CONTENT_GROUP,
            stability_seconds=0,
            timeout_seconds=0,
            clock=lambda: 3.0,
        )


def test_probe_selects_newest_supported_framework_root():
    logs = StubLogs(
        spans=[
            _root("strands-span", timestamp=1_000),
            _native_root("native-span", timestamp=2_000, ingestion=2_100),
        ],
        content=[
            _content("strands-span", timestamp=1_000, ingestion=3_000),
            _native_content("native-span", timestamp=2_000, ingestion=2_200),
        ],
    )

    probe = probe_latest_session(
        logs,
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
    )

    assert probe.root_span_id == "native-span"
    assert probe.framework == "claude_agent_sdk"
    assert probe.readiness_ingestion_ms == 2_200
    assert [call["logGroupName"] for call in logs.calls] == [
        "aws/spans",
        CONTENT_GROUP,
    ]


def test_probe_does_not_fall_back_past_newer_incomplete_native_root():
    logs = StubLogs(
        spans=[
            _root("strands-span", timestamp=1_000),
            _native_root(
                "native-span",
                timestamp=2_000,
                ingestion=2_100,
                completed=False,
            ),
        ],
        content=[_content("strands-span", timestamp=1_000, ingestion=3_000)],
    )

    probe = probe_latest_session(
        logs,
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
    )

    assert probe.root_span_id == "native-span"
    assert probe.framework == "claude_agent_sdk"
    assert probe.paired is False
    assert probe.detail is not None and "completion timestamp" in probe.detail
    assert [call["logGroupName"] for call in logs.calls] == ["aws/spans"]


def test_wait_polls_until_matching_log_reaches_stability_age():
    logs = StubLogs(
        spans=[_root("root-span", timestamp=990_000)],
        content=[_content("root-span", timestamp=990_000, ingestion=995_000)],
    )
    now = {"value": 1_000.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now["value"] += seconds

    probe = wait_for_evaluation_telemetry(
        logs,
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
        stability_seconds=10,
        timeout_seconds=30,
        poll_seconds=2,
        clock=lambda: now["value"],
        sleep=sleep,
    )

    assert probe.root_span_id == "root-span"
    assert now["value"] == 1_006.0
    assert sleeps == [2, 2, 2]
    assert len(logs.calls) == 8  # two log groups per probe


def test_wait_uses_native_content_ingestion_for_stability_age():
    logs = StubLogs(
        spans=[_native_root("native-span", timestamp=990_000, ingestion=995_000)],
        content=[
            _native_content("native-span", timestamp=990_000, ingestion=995_000)
        ],
    )
    now = {"value": 1_000.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now["value"] += seconds

    probe = wait_for_evaluation_telemetry(
        logs,
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
        stability_seconds=10,
        timeout_seconds=30,
        poll_seconds=2,
        clock=lambda: now["value"],
        sleep=sleep,
    )

    assert probe.framework == "claude_agent_sdk"
    assert now["value"] == 1_006.0
    assert sleeps == [2, 2, 2]
    assert len(logs.calls) == 8  # two log groups per probe


def test_wait_times_out_before_batch_when_root_span_never_arrives():
    logs = StubLogs()
    now = {"value": 1_000.0}

    def sleep(seconds):
        now["value"] += seconds

    with pytest.raises(TelemetryReadinessTimeout, match="root span is missing"):
        wait_for_evaluation_telemetry(
            logs,
            session_id=SESSION_ID,
            content_log_group=CONTENT_GROUP,
            stability_seconds=0,
            timeout_seconds=5,
            poll_seconds=2,
            clock=lambda: now["value"],
            sleep=sleep,
        )


def test_probe_uses_cloudwatch_pagination():
    root = _root("root-span", timestamp=1_000)
    content = _content("root-span", timestamp=1_000, ingestion=1_100)

    class PagedLogs:
        def filter_log_events(self, **kwargs):
            token = kwargs.get("nextToken")
            if kwargs["logGroupName"] == "aws/spans":
                return {"events": [] if token is None else [root], "nextToken": "spans-next"}
            return {
                "events": [] if token is None else [content],
                "nextToken": "content-next",
            }

    probe = probe_latest_session(
        PagedLogs(),
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
    )

    assert probe.root_span_id == "root-span"
    assert probe.content_ingestion_ms == 1_100


def test_probe_pages_to_native_claude_root():
    native = _native_root("native-span", timestamp=1_000, ingestion=1_100)
    content = _native_content("native-span", timestamp=1_000, ingestion=1_200)

    class PagedLogs:
        def filter_log_events(self, **kwargs):
            token = kwargs.get("nextToken")
            if kwargs["logGroupName"] == "aws/spans":
                return {
                    "events": [] if token is None else [native],
                    "nextToken": "spans-next",
                }
            return {
                "events": [] if token is None else [content],
                "nextToken": "content-next",
            }

    probe = probe_latest_session(
        PagedLogs(),
        session_id=SESSION_ID,
        content_log_group=CONTENT_GROUP,
    )

    assert probe.root_span_id == "native-span"
    assert probe.framework == "claude_agent_sdk"
    assert probe.content_ingestion_ms == 1_200
