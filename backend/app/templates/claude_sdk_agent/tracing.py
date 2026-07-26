"""Manual Strands-shaped gen_ai telemetry for Claude Agent SDK containers.

The SDK drives the claude CLI as a subprocess, so ADOT auto-instrumentation in
THIS process sees only the /invocations HTTP server span — none of the LLM or
tool work. We therefore emit the gen_ai spans/events ourselves, mirroring the
Strands SDK's telemetry (adapted from the agentxray demo-agent, where this
shape is verified against live AgentCore Evaluations + Transaction Search).

Two hard requirements, each silently breaking evaluation parsing otherwise:

1. **Scope must be a supported instrumentation library.** Spans/events are only
   parsed when ``scope.name`` is ``strands.telemetry.tracer`` (or the langchain
   scopes) — we mirror Strands, so tracer AND event logger use its scope name.
2. **Event bodies must match the scope's shape exactly.** input.messages =
   [{content: <system str>, role: system}, {content: {content: '[{"text":…}]'},
   role: user}]; output.messages = [{content: {message: <str>, finish_reason:
   end_turn}, role: assistant}]. A plain {content: <str>} shape fails parsing.

Content events go out through the **logs** API. They used to use the experimental
events API (``opentelemetry._events``), which upstream deprecated in 1.39.0 and
has since removed — an unpinned ``aws-opentelemetry-distro`` therefore made fresh
builds crash on import while the deploy still reported green (see
docs/issues/2026-07-26-container-otel-events-import.md). ``_emit_event`` now does
by hand the two things the old SDK EventLogger did for us: add the ``event.name``
attribute, and emit through a logger carrying EVAL_SCOPE. The exported record is
byte-identical to the old one, which is why the ``event_name`` LogRecord field
(upstream's canonical replacement for ``Event(name=…)``) is deliberately left
unset — the old path never populated it, and AWS-side evaluation parsing has only
ever been verified against the attribute form.

Spans created here run inside the auto-instrumented request handler, so they
parent under the ADOT ``POST /invocations`` server span — same trace, and the
console waterfall shows invoke_agent → execute_tool/chat children. With no
OTEL SDK configured (local runs, unit tests) everything is a safe no-op.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import baggage, trace
from opentelemetry._logs import LogRecord, SeverityNumber, get_logger
from opentelemetry.context import attach, detach
from opentelemetry.trace import set_span_in_context

PROVIDER = "anthropic"

# MUST be an evaluation-supported instrumentation scope (see module docstring).
EVAL_SCOPE = "strands.telemetry.tracer"

_tracer = trace.get_tracer(EVAL_SCOPE)
_event_logger = get_logger(EVAL_SCOPE)


@contextmanager
def traced_invocation(agent_name: str, session_id: str) -> Iterator[trace.Span]:
    """invoke_agent span + session.id baggage around one invocation."""
    token = attach(baggage.set_baggage("session.id", session_id))
    try:
        with _tracer.start_as_current_span(f"invoke_agent {agent_name}") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.system", PROVIDER)
            span.set_attribute("gen_ai.provider.name", PROVIDER)
            span.set_attribute("gen_ai.agent.name", agent_name)
            span.set_attribute("session.id", session_id)
            yield span
            span.set_status(trace.StatusCode.OK)
    finally:
        detach(token)


def record_tool_call(
    *,
    session_id: str,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    result_text: str,
    is_error: bool = False,
    description: str = "",
    json_schema: dict[str, Any] | None = None,
) -> None:
    """execute_tool span + content event for one tool invocation.

    Mirrors Strands tool telemetry: gen_ai.operation.name=execute_tool /
    gen_ai.tool.* / aws.genai.span_kind=TOOL, with the tool args/result in a
    scope-named content event. Must run INSIDE traced_invocation so the span
    parents under invoke_agent. Claude SDK tools (built-ins, MCP) don't expose
    a schema — description/json_schema stay optional.
    """
    with _tracer.start_as_current_span(f"execute_tool {name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.system", PROVIDER)
        span.set_attribute("gen_ai.provider.name", PROVIDER)
        span.set_attribute("gen_ai.tool.name", name)
        span.set_attribute("gen_ai.tool.call.id", call_id)
        if description:
            span.set_attribute("gen_ai.tool.description", description)
        if json_schema is not None:
            span.set_attribute("gen_ai.tool.json_schema", json.dumps(json_schema))
        span.set_attribute("gen_ai.tool.status", "error" if is_error else "success")
        span.set_attribute("aws.genai.span_kind", "TOOL")
        span.set_attribute("session.id", session_id)
        span.set_status(trace.StatusCode.ERROR if is_error else trace.StatusCode.OK)

        body = {
            "input": {
                "messages": [
                    {
                        "content": {
                            "content": json.dumps(arguments),
                            "role": "tool",
                            "id": call_id,
                        },
                        "role": "tool",
                    }
                ]
            },
            "output": {
                "messages": [
                    {
                        "content": {
                            "message": json.dumps([{"text": result_text}]),
                            "id": call_id,
                        },
                        "role": "assistant",
                    }
                ]
            },
        }
        _emit_event(span, session_id, body)


def record_llm_usage(
    *,
    session_id: str,
    model: str,
    usage: dict[str, Any],
    num_turns: int | None = None,
) -> None:
    """One aggregate ``chat`` span carrying the query's token usage.

    The SDK reports usage once per query (ResultMessage.usage, summed across
    turns), not per LLM call — so this is a single honest aggregate span. The
    attribute names are the aws/spans conventions the console sums
    (gen_ai.usage.input_tokens/output_tokens/cache_read_input_tokens/
    cache_write_input_tokens; the SDK's cache_creation maps to cache_write),
    and gen_ai.operation.name=chat marks it as a terminal LLM client span.
    """
    with _tracer.start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", PROVIDER)
        span.set_attribute("gen_ai.provider.name", PROVIDER)
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("session.id", session_id)
        span.set_attribute("gen_ai.usage.input_tokens", int(usage.get("input_tokens") or 0))
        span.set_attribute("gen_ai.usage.output_tokens", int(usage.get("output_tokens") or 0))
        span.set_attribute(
            "gen_ai.usage.cache_read_input_tokens",
            int(usage.get("cache_read_input_tokens") or 0),
        )
        span.set_attribute(
            "gen_ai.usage.cache_write_input_tokens",
            int(usage.get("cache_creation_input_tokens") or 0),
        )
        if num_turns is not None:
            span.set_attribute("gen_ai.agent.num_turns", int(num_turns))
        span.set_status(trace.StatusCode.OK)


def record_result(
    span: trace.Span,
    *,
    session_id: str,
    system_prompt: str,
    prompt: str,
    output: str,
    model: str,
) -> None:
    """Attach result metadata to the invoke_agent span and emit the
    Strands-shaped content event carrying the actual messages."""
    span.set_attribute("gen_ai.request.model", model)
    body = {
        "input": {
            "messages": [
                {"content": system_prompt, "role": "system"},
                {
                    "content": {"content": json.dumps([{"text": prompt}])},
                    "role": "user",
                },
            ]
        },
        "output": {
            "messages": [
                {
                    "content": {"message": output, "finish_reason": "end_turn"},
                    "role": "assistant",
                }
            ]
        },
    }
    _emit_event(span, session_id, body)


def _emit_event(span: trace.Span, session_id: str, body: dict[str, Any]) -> None:
    """Emit a content event on the span's trace/span id (what the console's
    span drawer and the evaluators read from the runtime log group).

    ``event.name`` is written as an attribute on purpose — that is exactly what
    the removed events API did (``Event.__init__`` merged it into attributes),
    and the record must stay identical for AWS-side parsing (see docstring).

    Correlation is carried by ``context`` rather than the trace_id/span_id/
    trace_flags kwargs: those are deprecated since 1.35.0, and passing the span
    explicitly (instead of leaning on the ambient current span) keeps the record
    pinned to the span the caller handed us."""
    _event_logger.emit(
        LogRecord(
            timestamp=time.time_ns(),
            context=set_span_in_context(span),
            body=body,
            severity_number=SeverityNumber.INFO,
            attributes={"session.id": session_id, "event.name": EVAL_SCOPE},
        )
    )
