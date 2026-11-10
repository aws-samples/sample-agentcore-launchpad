"""LangGraph sessions → strands ``Session`` (r17-bf7), by Global AgentCore Evaluations'
LangGraph rules (``supported-frameworks-langgraph``) — both instrumentation libraries
(OpenTelemetry / traceloop ``opentelemetry.instrumentation.langchain`` and OpenInference
``openinference.instrumentation.langchain``), both delivery modes:

- span types — invoke agent: ``traceloop.span.kind=workflow`` (also ``gen_ai.operation.name
  =invoke_agent``) / ``openinference.span.kind`` ``CHAIN`` or ``AGENT``; execute tool:
  ``traceloop.span.kind=tool`` (``execute_tool``) / ``TOOL``. One trace is one turn; its
  agent span is the last-ending one with content (the graph root: node spans end first).
- content — split: the span's event record (``body.input`` / ``body.output``); unified:
  ``gen_ai.task.input`` / ``gen_ai.task.output`` (agent) and ``gen_ai.tool.call.arguments``
  / ``gen_ai.tool.call.result`` (tool) — OpenInference ``input.value`` / ``output.value``.
- the agent span's content is the serialised graph STATE: user prompt = the last human
  message of the input state, agent response = the last message of the output state (roles
  ``human``/``ai``/``tool`` or ``HumanMessage``/``AIMessage``/``ToolMessage``). A turn whose
  state ends on a tool result (``return_direct``) showed the user that ``ToolMessage``: it
  is the answer (N17-obs412, user ruling — Global takes the previous turn's AI message from
  the replayed history instead). ``""`` only when the turn produced no message after the
  prompt — never the user prompt (the library fell back to it) nor an earlier turn's answer.
- with no span records (``runtime_logs``) the event records alone are classified by shape:
  a tool result (``{"output": ToolMessage}``, ``{"type": "tool", "data"}``) is a tool
  call, a graph state is an agent record — the library found no convertible span.

TypeScript (r17-bm, "TypeScript agent support"): LangGraph.js emits the same span types under
three scopes — ADOT Node's own ``@aws/aws-distro-opentelemetry-instrumentation-langchain``
(``gen_ai.operation.name``; the invoke agent span is a structural container and the
conversation lives on the ``chat`` spans' parts-format ``gen_ai.input.messages`` /
``gen_ai.output.messages`` — the user prompt is the last user message of the turn's first
model call, the answer the text parts of its last-ending one; ADOT 0.13 also puts the turn's
own messages on the agent span, read first), Traceloop's ``@traceloop/instrumentation-langchain``
(``traceloop.span.kind`` ``workflow`` / ``task``; content in ``traceloop.entity.input|output``;
a tool's ``traceloop.entity.name`` is the LangChain class — ``DynamicStructuredTool`` — so the
tool is named by its ``ToolMessage``, and its input is ``{"args": [<arguments JSON>]}``) and
OpenInference's ``@arizeai/openinference-instrumentation-langchain`` (as in Python).

A turn with an ERROR agent span is failed (``metadata["span_status"]``, as in
:mod:`spans_provider`); without span records, an agent record with no output state is.

Deep Agents (``deepagents``) load a skill with ``read_file(file_path=".../SKILL.md")``:
the tool call carries its parsed arguments (``inputs``, else ``input_str``), so the
strands-evals skills extractor sees the read.

Pure library (shipped into the worker zip): imports nothing from ``app.*`` but sibling
engine modules.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from typing import Any

from strands_evals.mappers import SessionMapper
from strands_evals.mappers.utils import get_body
from strands_evals.types.trace import (
    AgentInvocationSpan,
    Session,
    SpanInfo,
    ToolCall,
    ToolConfig,
    ToolExecutionSpan,
    ToolResult,
    Trace,
)

from app.services.strands_eval import tool_catalog

TRACELOOP_SCOPE = "opentelemetry.instrumentation.langchain"
OPENINFERENCE_SCOPE = "openinference.instrumentation.langchain"
# TypeScript (LangGraph.js) scopes — Global's "TypeScript agent support" (r17-bm)
ADOT_TS_SCOPE = "@aws/aws-distro-opentelemetry-instrumentation-langchain"
TRACELOOP_TS_SCOPE = "@traceloop/instrumentation-langchain"
OPENINFERENCE_TS_SCOPE = "@arizeai/openinference-instrumentation-langchain"
LANGGRAPH_SCOPES = frozenset({TRACELOOP_SCOPE, OPENINFERENCE_SCOPE, ADOT_TS_SCOPE,
                              TRACELOOP_TS_SCOPE, OPENINFERENCE_TS_SCOPE})

_ROLES = {
    "human": "user", "user": "user", "humanmessage": "user", "humanmessagechunk": "user",
    "ai": "ai", "assistant": "ai", "aimessage": "ai", "aimessagechunk": "ai",
    "tool": "tool", "toolmessage": "tool", "toolmessagechunk": "tool",
    "system": "system", "systemmessage": "system",
}
_AGENT_INPUT_KEYS = ("gen_ai.task.input", "traceloop.entity.input", "input.value",
                     "gen_ai.input.messages")
_AGENT_OUTPUT_KEYS = ("gen_ai.task.output", "traceloop.entity.output", "output.value",
                      "gen_ai.output.messages")
_TOOL_INPUT_KEYS = ("gen_ai.tool.call.arguments", "traceloop.entity.input", "input.value")
_TOOL_OUTPUT_KEYS = ("gen_ai.tool.call.result", "traceloop.entity.output", "output.value")


def is_langgraph(spans: list[dict[str, Any]]) -> bool:
    """The session's records come from a LangGraph (LangChain) instrumentation — and from
    no Strands agent: ADOT instruments LangChain in any process that imports it, so a
    Strands agent calling a LangChain tool writes both scopes and stays a Strands session."""
    scopes = {str((s.get("scope") or {}).get("name") or "") for s in spans}
    return bool(scopes & LANGGRAPH_SCOPES) and not any(
        scope.startswith("strands") for scope in scopes)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _text(content: Any) -> str:
    """Text of a message content: a string, or the text blocks of a block list (reasoning
    and tool-call blocks are not text)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") in (None, "text") and isinstance(content.get("text"), str):
            return content["text"]
        if content.get("type") == "text" and isinstance(content.get("content"), str):
            return content["content"]
        return ""
    if isinstance(content, list):
        return "\n".join(t for t in (_text(c) for c in content) if t)
    return ""


def _message(obj: Any) -> dict[str, Any] | None:
    """One serialised LangChain message → ``{role, text, tool_calls, tool_call_id, name,
    status}``: a constructor (``{"lc", "id": [..., "AIMessage"], "kwargs"}``),
    OpenInference's ``{"type", "data"}``, or a plain ``{"role"|"type", "content"|"parts"}``."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("kwargs"), dict) and obj.get("lc"):
        ids = obj.get("id") if isinstance(obj.get("id"), list) else []
        fields = obj["kwargs"]
        kind = fields.get("type") or (ids[-1] if ids else "")
    elif isinstance(obj.get("data"), dict) and obj.get("type"):
        fields, kind = obj["data"], obj["type"]
    else:
        fields, kind = obj, obj.get("role") or obj.get("type") or ""
    role = _ROLES.get(str(kind).casefold())
    if role is None:
        return None
    if "parts" in fields and "content" not in fields:
        text = _text(fields.get("parts"))
    else:
        text = _text(fields.get("content"))
    calls = fields.get("tool_calls") if isinstance(fields.get("tool_calls"), list) else []
    return {"role": role, "text": text, "tool_calls": calls,
            "tool_call_id": fields.get("tool_call_id"), "name": fields.get("name"),
            "status": fields.get("status")}


def _state_messages(value: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    """The ``messages`` of a serialised graph state (``{"inputs": {"messages": …}}``,
    ``{"outputs": …}``, OpenInference's bare ``{"messages": …}``); ``None`` when ``value``
    is not a state (a tool payload, a model call's message list, a node update list)."""
    value = _json(value)
    if not isinstance(value, dict) or depth > 3:
        return None
    if isinstance(value.get("messages"), list):
        return [m for m in (_message(x) for x in value["messages"]) if m is not None]
    for key in ("outputs", "inputs", "output", "input", "values"):
        if key in value and (found := _state_messages(value[key], depth + 1)) is not None:
            return found
    return None


def _parts_messages(value: Any) -> list[dict[str, Any]] | None:
    """A parts-format message list (``gen_ai.input.messages`` / ``gen_ai.output.messages``:
    ``[{"role", "parts": [{"type": "text", "content"}, …]}]``); ``None`` when ``value`` is not
    one. Reasoning and tool-call parts are not text."""
    value = _json(value)
    if not isinstance(value, list) or not value or not all(
            isinstance(m, dict) and isinstance(m.get("parts"), list) for m in value):
        return None
    return [m for m in (_message(x) for x in value) if m is not None]


def _tool_message(value: Any) -> dict[str, Any] | None:
    """A tool's result as a ``ToolMessage``: ``{"output": <ToolMessage>}`` (traceloop),
    ``{"type": "tool", "data"}`` (OpenInference), a bare constructor or ``role: tool``."""
    value = _json(value)
    if isinstance(value, dict) and "output" in value and not isinstance(
            value.get("messages"), list):
        inner = _json(value["output"])
        message = _message(inner)
        if message is not None and message["role"] == "tool":
            return message
    message = _message(value)
    return message if message is not None and message["role"] == "tool" else None


def _kind_attribute(span: dict[str, Any]) -> str | None:
    """``agent`` / ``tool`` / ``other`` by the classifying attributes; ``None`` without any
    (an event-only record: ``runtime_logs``)."""
    attributes = span.get("attributes") or {}
    traceloop = attributes.get("traceloop.span.kind")
    if traceloop == "task" and (span.get("scope") or {}).get("name") == TRACELOOP_TS_SCOPE:
        traceloop = "tool"  # Traceloop JS marks a tool ``task`` (Python: a graph node)
    openinference = str(attributes.get("openinference.span.kind") or "").upper()
    operation = attributes.get("gen_ai.operation.name")
    if traceloop == "tool" or openinference == "TOOL" or operation == "execute_tool":
        return "tool"
    if traceloop == "workflow" or openinference in ("CHAIN", "AGENT") \
            or operation == "invoke_agent":
        return "agent"
    if traceloop or openinference or operation:
        return "other"
    return None


def _side(span: dict[str, Any], side: str, keys: tuple[str, ...]) -> Any:
    """One side's content: the event record's (split), else the span attribute's
    (unified). A record message's ``content`` is the serialised payload; a tool result
    filed as the message itself (``{"role": "tool", "content"}``) is returned whole."""
    body = get_body(span)
    messages = ((body or {}).get(side) or {}).get("messages") or []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool" and side == "output":
            return msg
        if msg.get("content") not in (None, ""):
            return msg["content"]
    attributes = span.get("attributes") or {}
    return next((attributes[k] for k in keys if attributes.get(k) not in (None, "")), None)


def _arguments(value: Any) -> dict[str, Any]:
    """Tool arguments: traceloop's ``{"input_str", "inputs"}`` wrapper (``inputs``, else
    ``input_str`` — a Python repr), else the JSON object itself."""
    value = _json(value)
    if isinstance(value, dict) and set(value) == {"args"} and isinstance(value["args"], list) \
            and len(value["args"]) == 1:  # Traceloop JS: {"args": [<arguments JSON>]}
        value = _json(value["args"][0])
    if isinstance(value, dict) and ("input_str" in value or "inputs" in value):
        if isinstance(value.get("inputs"), dict):
            return value["inputs"]
        raw = value.get("input_str")
        for parse in (json.loads, ast.literal_eval):
            try:
                parsed = parse(raw) if isinstance(raw, str) else raw
            except (ValueError, SyntaxError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {"input": raw} if raw not in (None, "") else {}
    if isinstance(value, dict):
        return value
    return {"input": value} if value not in (None, "") else {}


def _wrapper_call_id(value: Any) -> str | None:
    value = _json(value)
    kwargs = value.get("kwargs") if isinstance(value, dict) else None
    call_id = kwargs.get("tool_call_id") if isinstance(kwargs, dict) else None
    return str(call_id) if call_id else None


def _system_prompt(span: dict[str, Any]) -> str | None:
    """A model call's system message: the record's ``role: system`` input (a JSON list of
    parts), else ``gen_ai.system_instructions``."""
    body = get_body(span)
    for msg in ((body or {}).get("input") or {}).get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            text = _text(_json(msg.get("content")))
            if text:
                return text
    text = _text(_json((span.get("attributes") or {}).get("gen_ai.system_instructions")))
    return text or None


def _time(value: Any) -> datetime:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1e9, tz=UTC)
    return datetime.now(tz=UTC)


def _end(span: dict[str, Any]) -> int:
    return int(span.get("end_time") or span.get("start_time") or 0)


class LangGraphSessionMapper(SessionMapper):
    """LangGraph normalised spans (``CloudWatchLogsParser``) → ``Session``; see the module
    docstring for the rules."""

    def __init__(self) -> None:
        self._catalog = tool_catalog.Catalog()

    def map_to_session(self, data: Any, session_id: str) -> Session:
        spans = self._normalize_to_flat_spans(data)
        self._catalog = self._build_catalog(spans)
        by_trace: dict[str, list[dict[str, Any]]] = {}
        for span in spans:
            trace_id = span.get("trace_id") or span.get("traceId")
            if trace_id:
                by_trace.setdefault(str(trace_id), []).append(span)
        traces = [trace for trace_id, trace_spans in by_trace.items()
                  if (trace := self._convert_trace(trace_id, trace_spans, session_id)).spans]
        return Session(session_id=session_id, traces=traces)

    # ── classification ──────────────────────────────────────────────────────
    def _kind(self, span: dict[str, Any]) -> str:
        kind = _kind_attribute(span)
        if kind is not None:
            return kind
        if get_body(span) is None:
            return "other"
        if _tool_message(_side(span, "output", ())) is not None:
            return "tool"
        if _state_messages(_side(span, "input", ())) is not None \
                or _state_messages(_side(span, "output", ())) is not None:
            return "agent"
        return "other"

    def _build_catalog(self, spans: list[dict[str, Any]]) -> tool_catalog.Catalog:
        catalog = tool_catalog.Catalog()
        for span in spans:
            if _kind_attribute(span) != "tool":
                continue
            attributes = span.get("attributes") or {}
            catalog.add(attributes.get("gen_ai.tool.name") or attributes.get("tool.name"),
                        attributes.get("gen_ai.tool.description")
                        or attributes.get("tool.description"),
                        attributes.get("gen_ai.tool.json_schema")
                        or attributes.get("tool.parameters"))
        return catalog

    # ── conversion ──────────────────────────────────────────────────────────
    def _span_info(self, span: dict[str, Any], session_id: str) -> SpanInfo:
        return SpanInfo(
            trace_id=span.get("trace_id") or span.get("traceId") or "",
            span_id=span.get("span_id") or span.get("spanId") or "",
            session_id=session_id,
            parent_span_id=span.get("parent_span_id") or span.get("parentSpanId"),
            start_time=_time(span.get("start_time")),
            end_time=_time(span.get("end_time") or span.get("start_time")))

    def _convert_trace(self, trace_id: str, spans: list[dict[str, Any]],
                       session_id: str) -> Trace:
        by_start = sorted(spans, key=lambda s: int(s.get("start_time") or 0))
        kinds = {id(s): self._kind(s) for s in by_start}
        # the model's own tool calls (AI messages of the trace's states), by call id: the
        # arguments of a tool whose span lost their keys (OpenInference JS records a
        # one-field input as its bare value — ``"NYC"`` for ``{"city": "NYC"}``)
        requested: dict[str, dict[str, Any]] = {}
        for s in by_start:
            for message in _state_messages(_side(s, "output", _AGENT_OUTPUT_KEYS)) or []:
                for call in message["tool_calls"]:
                    if isinstance(call, dict) and call.get("id") \
                            and isinstance(call.get("args"), dict):
                        requested[str(call["id"])] = call["args"]
        tools = [self._tool_span(s, session_id, requested) for s in by_start
                 if kinds[id(s)] == "tool"]
        agents = [s for s in by_start if kinds[id(s)] == "agent"
                  and (_side(s, "input", _AGENT_INPUT_KEYS) is not None
                       or _side(s, "output", _AGENT_OUTPUT_KEYS) is not None)]
        # ADOT Node (TypeScript): an agent span WITHOUT content is a structural container —
        # the turn's conversation is on its chat spans (Global's TypeScript rule)
        containers = [s for s in by_start if kinds[id(s)] == "agent" and s not in agents]
        chats = [s for s in by_start
                 if (s.get("attributes") or {}).get("gen_ai.operation.name") == "chat"
                 and _parts_messages(_side(s, "output", ("gen_ai.output.messages",)))
                 is not None]
        out: list[Any] = list(tools)
        if agents or (containers and chats):
            system = next((p for s in by_start if (p := _system_prompt(s))), None)
            if agents:
                invocation = self._agent_span(max(agents, key=_end), tools, system, session_id)
            else:
                invocation = self._container_span(max(containers, key=_end), chats, tools,
                                                  system, session_id)
            if invocation is not None:
                out.append(invocation)
        return Trace(spans=out, trace_id=trace_id, session_id=session_id)

    def _agent_span(self, span: dict[str, Any], tools: list[ToolExecutionSpan],
                    system: str | None, session_id: str) -> AgentInvocationSpan | None:
        raw_in = _side(span, "input", _AGENT_INPUT_KEYS)
        raw_out = _side(span, "output", _AGENT_OUTPUT_KEYS)
        if _parts_messages(raw_in) is not None or _parts_messages(raw_out) is not None:
            # ADOT Node 0.13: the turn's own messages on the agent span, parts format
            inputs = _parts_messages(raw_in) or []
            outputs = _parts_messages(raw_out) or []
            prompt = next((m["text"] for m in reversed(inputs) if m["role"] == "user"), "")
            response = outputs[-1]["text"] if outputs and outputs[-1]["role"] == "ai" else ""
            return self._invocation(span, prompt, response, tools, system, session_id) \
                if prompt else None
        inputs = _state_messages(raw_in)
        if inputs is None:  # a bare prompt (a custom graph input)
            prompt = _text(_json(raw_in)) if raw_in is not None else ""
        else:
            prompt = next((m["text"] for m in reversed(inputs) if m["role"] == "user"), "")
        outputs = _state_messages(raw_out) or []
        last_user = max((i for i, m in enumerate(outputs) if m["role"] == "user"), default=-1)
        after = outputs[last_user + 1:]
        if not prompt and last_user >= 0:
            prompt = outputs[last_user]["text"]
        if not prompt:
            return None
        # the turn's answer is the state's LAST message: the AI's, or the tool result a
        # return_direct tool ended the graph on — what the user saw (N17-obs412)
        response = after[-1]["text"] if after and after[-1]["role"] in ("ai", "tool") else ""
        metadata: dict[str, Any] | None = None
        if _kind_attribute(span) is None:
            metadata = {}
            if raw_out is None or _state_messages(raw_out) is None:
                # no span records (runtime_logs): the graph never returned its state
                metadata = {"span_status": "ERROR", "span_status_source": "runtime_logs"}
        return self._invocation(span, prompt, response, tools, system, session_id, metadata)

    def _container_span(self, span: dict[str, Any], chats: list[dict[str, Any]],
                        tools: list[ToolExecutionSpan], system: str | None,
                        session_id: str) -> AgentInvocationSpan | None:
        """An ADOT Node agent span without content: the prompt is the last user message of
        the turn's first model call, the answer the text of its last-ending one (empty when
        that call ended on a tool call — the turn produced no answer)."""
        first = min(chats, key=lambda s: int(s.get("start_time") or 0))
        inputs = _parts_messages(_side(first, "input", ("gen_ai.input.messages",))) or []
        prompt = next((m["text"] for m in reversed(inputs) if m["role"] == "user"), "")
        outputs = _parts_messages(
            _side(max(chats, key=_end), "output", ("gen_ai.output.messages",))) or []
        response = outputs[-1]["text"] if outputs and outputs[-1]["role"] == "ai" else ""
        return self._invocation(span, prompt, response, tools, system, session_id) \
            if prompt else None

    def _invocation(self, span: dict[str, Any], prompt: str, response: str,
                    tools: list[ToolExecutionSpan], system: str | None, session_id: str,
                    metadata: dict[str, Any] | None = None) -> AgentInvocationSpan:
        if metadata is None:  # a span record: its status decides
            status = str((span.get("status") or {}).get("code") or "").upper()
            metadata = {"span_status": status} if status else {}
        called = list(dict.fromkeys(t.tool_call.name for t in tools))
        return AgentInvocationSpan(
            span_info=self._span_info(span, session_id), user_prompt=prompt,
            agent_response=response.strip(), system_prompt=system,
            available_tools=(self._catalog.tools(called) if self._catalog
                             else [ToolConfig(name=n) for n in sorted(called)]),
            metadata=metadata)

    def _tool_span(self, span: dict[str, Any], session_id: str,
                   requested: dict[str, dict[str, Any]] | None = None) -> ToolExecutionSpan:
        attributes = span.get("attributes") or {}
        raw_in = _side(span, "input", _TOOL_INPUT_KEYS)
        raw_out = _side(span, "output", _TOOL_OUTPUT_KEYS)
        message = _tool_message(raw_out)
        name = str(attributes.get("gen_ai.tool.name") or attributes.get("tool.name")
                   or (message or {}).get("name") or attributes.get("traceloop.entity.name")
                   or str(span.get("name") or "").removeprefix("execute_tool").strip())
        call_id = (attributes.get("gen_ai.tool.call.id") or _wrapper_call_id(raw_in)
                   or (message or {}).get("tool_call_id"))
        if message is not None:
            result = message["text"]
        else:  # a plain result (ADOT Node: the tool's own return value, often JSON text)
            result = _text(_json(raw_out)) or (raw_out if isinstance(raw_out, str) else "")
        failed = (str((span.get("status") or {}).get("code") or "").upper() == "ERROR"
                  or str((message or {}).get("status") or "").lower() == "error"
                  or str(attributes.get("gen_ai.tool.status") or "").lower() == "error")
        call_id = str(call_id) if call_id else None
        arguments = _arguments(raw_in)
        if call_id and not isinstance(_json(raw_in), dict) and call_id in (requested or {}):
            arguments = (requested or {})[call_id]
        return ToolExecutionSpan(
            span_info=self._span_info(span, session_id),
            tool_call=ToolCall(name=name, arguments=arguments, tool_call_id=call_id),
            tool_result=ToolResult(content=result or "", error="error" if failed else None,
                                   tool_call_id=call_id),
            metadata={})
