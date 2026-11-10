"""The architect turn against the self-built Harness runtime (plan R12).

The installed ``aws-agent-solution-architect`` preset is an ordinary
``method="harness"`` Runtime row (Task 5). One assistant turn is one
``InvokeAgentRuntime`` call whose payload carries the bounded ``messages`` replay
(the rendered entrypoint's ``_extract_prompt`` hands ``payload["messages"]`` verbatim
to ``agent.stream_async``) plus the actor; the response is the Strands/Converse event
stream the entrypoint yields, which this module maps to the assistant consumer's three
events — ``tool`` / ``tool_input`` / ``delta`` — and to the ``harness.execution_*``
errors the console already renders. No AWS client is constructed here: the caller
hands in ``agentcore.client.data_client(workspace)`` (tests inject a double).
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

from app.core.errors import AppError
from app.services.agentcore import runtime as rt

# Converse stop reasons that end a turn normally; anything else is the Harness's
# own bound (``ExecutionLimitsHook`` → ``timeout_exceeded`` / ``max_tokens`` /
# ``str(ExecutionLimitExceeded)``) and surfaces as an error the member can act on.
_CONVERSE_STOPS = frozenset(
    {"end_turn", "tool_use", "stop_sequence", "guardrail_intervened", "content_filtered"}
)
RUNTIME_USER_ID_MAX = 1024  # the API's bound on ``runtimeUserId``


class RuntimeStreamError(rt.SdkStreamError):
    """The stream carried a ``runtimeClientError`` / ``internalServerException``
    event, or the AgentCore SDK's terminal error frame
    (``{"error", "error_type", "message"}``, see ``rt.SdkStreamError``): the Runtime's
    side of the call failed mid-turn (transient — the turn is replayed once in a fresh
    session, see ``service._replayable_turn_error``). ``error_type`` is the frame's
    exception class name (None for the two Runtime-level events); ``detail`` is what the
    member-facing ``error`` event and the turn event log carry."""


STREAM_ERROR_TEXT_MAX = rt.STREAM_ERROR_TEXT_MAX


TOOL_INPUT_MAX_CHARS = 20_000  # what the transcript row keeps of a tool call's arguments


def stream_architect_turn(
    client: Any,
    runtime_arn: str,
    messages: list[dict[str, Any]],
    *,
    session_id: str,
    runtime_user_id: str,
    on_stream: Callable[[Any], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield ``{"event": "tool"|"tool_input"|"delta", "data": …}`` for one turn.

    ``on_stream`` receives the upstream body as soon as it exists so an owner that
    cancels the turn can close it and unblock a pending read. Raises
    ``AppError("harness.execution_timeout" | "harness.execution_limit",
    detail={"stop_reason"})`` on a non-Converse stop reason,
    ``AppError("harness.incomplete_response")`` when the stream ends without any reply
    text (cut short after a tool step, or empty), and ``RuntimeStreamError`` when the
    stream carries a runtime error event (``runtimeClientError`` /
    ``internalServerException``) or the SDK's terminal ``{"error", "error_type",
    "message"}`` frame — the latter used to be skipped, so a turn that died after a
    tool step on a model timeout / throttle / validation error was masked as
    ``harness.incomplete_response`` (N-ARCH-obs18). The body is always closed.
    """
    actor = (runtime_user_id or "default")[:RUNTIME_USER_ID_MAX]
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        runtimeUserId=actor,
        payload=json.dumps({"messages": messages, "actor_id": actor}).encode("utf-8"),
    )
    body = response["response"]
    if on_stream is not None:
        on_stream(body)
    try:
        yield from _map_events(rt.iter_sse_payloads(body, str(response.get("contentType", ""))))
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


def _bounded(raw: str) -> str:
    """The recorded form of a tool call's arguments: whitespace collapsed, cut with an
    explicit marker. It lands in the ledger and on the wire and is never parsed."""
    text = " ".join(raw.split())
    if len(text) <= TOOL_INPUT_MAX_CHARS:
        return text
    return text[:TOOL_INPUT_MAX_CHARS] + f"… [+{len(text) - TOOL_INPUT_MAX_CHARS} chars]"


def _map_events(payloads: Iterator[Any]) -> Iterator[dict[str, Any]]:
    current_tool: str | None = None
    pending: list[str] = []
    has_text = False
    last_stop: str | None = None
    for payload in payloads:
        if isinstance(payload, str):
            payload = rt.repr_event(payload) or {}
        if not isinstance(payload, dict):
            continue
        envelope = payload.get("event")
        inner: dict[str, Any] = envelope if isinstance(envelope, dict) else payload
        if "runtimeClientError" in inner or "internalServerException" in inner:
            detail = inner.get("runtimeClientError") or inner.get("internalServerException")
            raise RuntimeStreamError(f"runtime returned error: {detail}")
        for frame in (payload, inner):
            if rt.is_sdk_error_frame(frame):
                text, error_type, detail = rt.sdk_error_parts(frame)
                raise RuntimeStreamError(text, error_type=error_type, detail=detail)
        start = ((inner.get("contentBlockStart") or {}).get("start") or {}).get("toolUse")
        if isinstance(start, dict):
            current_tool, pending = start.get("toolUseId"), []
            yield {"event": "tool", "data": {"name": str(start.get("name") or ""),
                                            "id": current_tool}}
        delta = (inner.get("contentBlockDelta") or {}).get("delta") or {}
        if isinstance(delta, dict):
            tool_delta = delta.get("toolUse")
            if isinstance(tool_delta, dict) and tool_delta.get("input"):
                pending.append(str(tool_delta["input"]))
            if delta.get("text"):
                has_text = has_text or bool(str(delta["text"]).strip())
                yield {"event": "delta", "data": {"text": str(delta["text"])}}
        if "contentBlockStop" in inner and current_tool is not None:
            if pending:
                yield {"event": "tool_input",
                       "data": {"id": current_tool, "input": _bounded("".join(pending))}}
            current_tool, pending = None, []
        stop = (inner.get("messageStop") or {}).get("stopReason")
        if stop:
            last_stop = str(stop)
        if stop and stop not in _CONVERSE_STOPS:
            code = ("harness.execution_timeout" if stop == "timeout_exceeded"
                    else "harness.execution_limit")
            raise AppError(code, f"the agent run stopped: {stop}", {"stop_reason": str(stop)},
                           status_code=502)
    # an empty stream or one cut off after a tool step is never a reply
    if not has_text or last_stop == "tool_use":
        raise AppError(
            "harness.incomplete_response",
            "the agent run ended without a complete text response",
            {"stop_reason": last_stop}, status_code=502,
        )
