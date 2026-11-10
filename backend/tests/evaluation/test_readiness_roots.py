"""N17-obs336 — the batch readiness gate finds the agent's root span by what it IS, not by
the literal ``invoke_agent Strands Agents``.

Before this fix a dataset replay against a named Strands agent (Global's own example span is
``invoke_agent TravelAgent``), a TypeScript Strands agent (``invoke_agent Strands Agent``) or
any other framework never paired and failed with ``root span is missing``. The root is now an
``invoke_agent`` span (``gen_ai.operation.name`` or the ``invoke_agent`` name prefix) with no
agent ancestor; without one, the framework root kinds the Global framework pages list
(``openinference.span.kind`` AGENT / CHAIN, ``traceloop.span.kind == workflow``) at the top of
their tree. Content: the root's own content record (Strands split mode, unchanged), else
content records of the root's trace, else content inline on the root span itself.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.evaluation.telemetry import (
    TelemetryReadinessTimeout,
    probe_latest_session,
    wait_for_evaluation_telemetry,
)

SID = "session-" + "b" * 32
GROUP = "/aws/bedrock-agentcore/runtimes/agent-x-DEFAULT"
TRACE = "t" * 32


def _event(message: dict[str, Any], ts: int, ingestion: int | None = None) -> dict[str, Any]:
    return {"message": json.dumps(message), "timestamp": ts,
            "ingestionTime": ingestion if ingestion is not None else ts + 100}


def _span(span_id: str, name: str, ts: int, *, parent: str | None = None,
          scope: str = "strands.telemetry.tracer", trace: str = TRACE,
          service: str = "agent_x.DEFAULT", **attributes: Any) -> dict[str, Any]:
    message = {"name": name, "traceId": trace, "spanId": span_id, "scope": {"name": scope},
               "resource": {"attributes": {"service.name": service}},
               "attributes": {"session.id": SID, **attributes}}
    if parent:
        message["parentSpanId"] = parent
    return _event(message, ts)


def _content(span_id: str, ts: int, ingestion: int, trace: str = TRACE) -> dict[str, Any]:
    return _event({"traceId": trace, "spanId": span_id, "attributes": {"session.id": SID},
                   "body": {"input": {"messages": []}, "output": {"messages": []}}},
                  ts, ingestion)


class Logs:
    def __init__(self, spans: list[dict], content: list[dict]) -> None:
        self.spans, self.content = spans, content

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        return {"events": self.spans if kwargs["logGroupName"] == "aws/spans"
                else self.content}


def _probe(spans: list[dict], content: list[dict], **kw: Any):
    return probe_latest_session(Logs(spans, content), session_id=SID, content_log_group=GROUP,
                                **kw)


@pytest.mark.parametrize("name", ["invoke_agent TravelAgent", "invoke_agent Strands Agent",
                                  "invoke_agent Strands Agents"])
def test_any_strands_agent_name_is_a_root(name):
    probe = _probe([_span("root1", name, 2_000)],
                   [_content("root1", 2_000, 2_500)])
    assert (probe.root_span_id, probe.paired, probe.framework) == ("root1", True, "strands")
    assert probe.content_ingestion_ms == 2_500


def test_the_operation_attribute_alone_marks_a_root():
    span = _span("adk1", "agent_run [assistant]", 2_000, scope="gcp.vertex.agent",
                 **{"gen_ai.operation.name": "invoke_agent"})
    probe = _probe([span], [_content("adk1", 2_000, 2_400)])
    assert (probe.root_span_id, probe.paired) == ("adk1", True)


def test_an_a2a_peer_agent_under_the_orchestrators_tool_call_is_not_the_root():
    spans = [_span("orch", "invoke_agent Strands Agents", 1_000),
             _span("tool", "execute_tool call_peer", 1_100, parent="orch"),
             _span("peer", "invoke_agent Strands Agents", 1_500, parent="tool",
                   service="peer.DEFAULT")]
    probe = _probe(spans, [_content("orch", 1_000, 1_900), _content("peer", 1_500, 1_600)])
    assert (probe.root_span_id, probe.content_ingestion_ms) == ("orch", 1_900)


def test_a_langgraph_openinference_root_pairs_on_the_content_of_its_trace():
    spans = [_span("lg", "LangGraph", 3_000, scope="openinference.instrumentation.langchain",
                   **{"openinference.span.kind": "CHAIN"}),
             _span("node", "agent", 2_900, parent="lg",
                   scope="openinference.instrumentation.langchain",
                   **{"openinference.span.kind": "CHAIN"}),
             _span("llm", "ChatBedrock", 2_800, parent="node",
                   scope="openinference.instrumentation.langchain",
                   **{"openinference.span.kind": "LLM"})]
    probe = _probe(spans, [_content("llm", 2_800, 3_300), _content("node", 2_900, 3_400)])
    assert (probe.root_span_id, probe.paired, probe.framework) == ("lg", True, "openinference")
    assert probe.content_ingestion_ms == 3_400


def test_a_traceloop_workflow_root_with_content_inline_on_the_spans():
    root = _span("wf", "LangGraph.workflow", 4_000, scope="opentelemetry.instrumentation.langchain",
                 **{"traceloop.span.kind": "workflow",
                    "traceloop.entity.output": "{\"messages\": []}"})
    probe = _probe([root], [])
    assert (probe.root_span_id, probe.paired, probe.framework) == ("wf", True, "traceloop")
    assert probe.content_ingestion_ms == 4_100  # the root span record's own ingestion


def test_unified_strands_content_inline_on_the_root_span_pairs():
    root = json.loads(_span("u1", "invoke_agent Strands Agents", 5_000)["message"])
    root["events"] = [{"name": "gen_ai.user.message", "attributes": {"content": "[]"}},
                      {"name": "gen_ai.choice", "attributes": {"message": "hi"}}]
    probe = _probe([_event(root, 5_000, 5_050)], [])
    assert (probe.root_span_id, probe.paired, probe.content_ingestion_ms) == ("u1", True, 5_050)


def test_split_strands_still_waits_for_the_roots_own_content_record():
    spans = [_span("r1", "invoke_agent TravelAgent", 2_000),
             _span("c1", "chat", 1_900, parent="r1")]
    probe = _probe(spans, [_content("c1", 1_900, 2_100)])
    assert probe.root_span_id == "r1" and not probe.paired


def test_non_agent_spans_alone_are_still_no_root():
    spans = [_span("c1", "chat", 1_000), _span("x", "ClaudeAgentSDK.query", 1_100, scope="x")]
    assert _probe(spans, []).root_span_id is None


def _ticking_clock(start: float = 1_000.0):
    now = {"t": start}

    def clock() -> float:
        now["t"] += 1.0
        return now["t"]

    return clock


def test_a_named_agent_replay_passes_the_wait():
    logs = Logs([_span("root1", "invoke_agent TravelAgent", 990_000)],
                [_content("root1", 990_000, 995_000)])
    probe = wait_for_evaluation_telemetry(
        logs, session_id=SID, content_log_group=GROUP, stability_seconds=1,
        timeout_seconds=5, clock=_ticking_clock(), sleep=lambda _s: None)
    assert probe.paired and probe.root_span_id == "root1"
    with pytest.raises(TelemetryReadinessTimeout, match="root span is missing"):
        wait_for_evaluation_telemetry(
            Logs([_span("c1", "chat", 990_000)], []), session_id=SID, content_log_group=GROUP,
            stability_seconds=1, timeout_seconds=0, clock=_ticking_clock(),
            sleep=lambda _s: None)
