"""``CloudWatchProvider`` with an injected logs client and a log-group list (plan §B.2).

The library's provider builds its own CloudWatch Logs client in ``__init__`` and queries one
``logGroupName`` — a funnel violation and the wrong shape for AgentCore, whose runtime log
groups are per endpoint (``…-DEFAULT``, ``…-stable<id>``). This subclass skips the base
constructor, takes the client from the caller (``ws.client("logs")`` on the backend, the
worker's own client on the Runtime) and passes ``logGroupNames`` (risk R-2). Session ids
are validated before interpolation into the Logs Insights query text.

N17-obs315: a CloudWatch-body session is mapped by :class:`FinalAnswerSessionMapper`, so
``output`` (``actual_output``) and every ``AgentInvocationSpan.agent_response`` (the TRACE and
SESSION judges' response) are each turn's FINAL answer — not the preamble before its first
tool call, and never a tool result.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from strands_evals.mappers import CloudWatchSessionMapper, SessionMapper, detect_otel_mapper
from strands_evals.mappers.cloudwatch_parser import CloudWatchLogsParser
from strands_evals.mappers.utils import get_body
from strands_evals.providers import CloudWatchProvider
from strands_evals.providers.exceptions import ProviderError, SessionNotFoundError
from strands_evals.types.trace import (
    AgentInvocationSpan,
    AssistantMessage,
    Session,
    ToolCall,
    ToolConfig,
    ToolExecutionSpan,
    ToolResult,
    ToolResultContent,
    Trace,
    UserMessage,
)

from app.evaluation.telemetry import is_agent_span, root_kind
from app.services.strands_eval import extraction_check, tool_catalog
from app.services.strands_eval.langgraph_mapper import LangGraphSessionMapper, is_langgraph
from app.services.strands_eval.redaction import redact_value

# Same pattern as app.services.observability.SESSION_ID_RE (pinned by a test) — kept
# literal here because this module is inlined into the worker without ``app.*``.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-#:.@]{8,256}$")
SPANS_LOG_GROUP = "aws/spans"
SpansSource = Literal["spans", "runtime_logs", "both"]


def query_records(rows: list[list[dict[str, str]]], *, strict: bool = False
                  ) -> list[dict[str, Any]]:
    """Decode raw documents; guarded reads preserve malformed rows as unknown records.

    Authorization sees every returned row before parsing, redaction or agent filtering.
    An empty object has no provenance, so an access guard rejects the complete batch.
    The unguarded worker retains the SDK's tolerant parsing behavior.
    """
    if not strict:
        return CloudWatchProvider._parse_query_results(rows)
    records: list[dict[str, Any]] = []
    for row in rows:
        messages = [field.get("value") for field in row if field.get("field") == "@message"]
        try:
            document = json.loads(messages[0]) if len(messages) == 1 else None
        except (TypeError, ValueError):
            document = None
        records.append(document if isinstance(document, dict) else {})
    return records


def _end_time(span: dict) -> int:
    return span.get("end_time") or span.get("start_time") or span.get("timeUnixNano") or 0


# Unified telemetry (gen_ai events inline on the span record) → ADOT's split body record:
# event name → (role, body side), as ADOT's LLO handler groups them.
_INLINE_MESSAGE_EVENTS: dict[str, tuple[str, str]] = {
    "gen_ai.system.message": ("system", "input"),
    "gen_ai.user.message": ("user", "input"),
    "gen_ai.tool.message": ("tool", "input"),
    "gen_ai.assistant.message": ("assistant", "output"),
    "gen_ai.choice": ("assistant", "output"),
}


def split_inline_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unified-telemetry span records also get the split-mode body record ADOT would write
    (``{input,output}.messages`` with the event attributes as ``content``, stamped at span
    end), so one mapper reads both delivery modes. Spans that already have a body are left."""
    with_body = {r.get("spanId") for r in records
                 if isinstance(r.get("body"), dict) and r.get("spanId")}
    extra: list[dict[str, Any]] = []
    for rec in records:
        events = rec.get("events")
        if (not isinstance(events, list) or "startTimeUnixNano" not in rec
                or rec.get("spanId") in with_body):
            continue
        body: dict[str, Any] = {"input": {"messages": []}, "output": {"messages": []}}
        for event in events:
            if not isinstance(event, dict) or event.get("name") not in _INLINE_MESSAGE_EVENTS:
                continue
            role, side = _INLINE_MESSAGE_EVENTS[event["name"]]
            body[side]["messages"].append(
                {"role": role, "content": dict(event.get("attributes") or {})})
        if not (body["input"]["messages"] or body["output"]["messages"]):
            continue
        attributes = rec.get("attributes") or {}
        extra.append({
            "traceId": rec.get("traceId"), "spanId": rec.get("spanId"),
            "timeUnixNano": rec.get("endTimeUnixNano") or rec.get("startTimeUnixNano"),
            "scope": rec.get("scope") or {}, "body": body,
            "attributes": {"session.id": attributes.get("session.id")},
        })
    return records + extra


def _is_tool_span(span: dict) -> bool:
    """A Strands ``execute_tool`` span record (``aws/spans``). ADOT's MCP client span under it
    (``mcp tools/call …``) also says ``execute_tool`` but is the same call seen from the
    transport — scoped ``amazon.opentelemetry.distro.instrumentation.mcp``, so it is skipped."""
    scope = str((span.get("scope") or {}).get("name") or "")
    if scope and not scope.startswith("strands"):
        return False
    attributes = span.get("attributes") or {}
    return (attributes.get("gen_ai.operation.name") == "execute_tool"
            or str(span.get("name") or "").startswith("execute_tool"))


def _service(record: dict) -> str | None:
    value = ((record.get("resource") or {}).get("attributes") or {}).get("service.name")
    return str(value) if value else None


def _has_agent_ancestor(span: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
    seen: set[str] = set()
    parent = span.get("parentSpanId")
    while parent and parent in by_id and parent not in seen:
        seen.add(parent)
        if is_agent_span(by_id[parent]) or root_kind(by_id[parent]):
            return True
        parent = by_id[parent].get("parentSpanId")
    return False


def own_agent_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the evaluated agent's records (N17-obs380). An A2A peer the agent called writes
    its spans with the propagated trace id and ``session.id`` — but its own ``service.name``
    — so without this its ``execute_tool`` / ``chat`` / ``invoke_agent`` spans were read as
    the orchestrator's turn. Per trace, the own agent is the service of the ROOT
    ``invoke_agent`` span (one with no ``invoke_agent`` ancestor; the peer's sits under the
    orchestrator's A2A tool call); else the service of the runtime-group body records.
    Records of another service are dropped; records without a service are kept."""
    body_services = {svc for r in records if isinstance(r.get("body"), dict)
                     and (svc := _service(r))}
    spans_by_trace: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if "startTimeUnixNano" in rec and rec.get("traceId"):
            spans_by_trace.setdefault(str(rec["traceId"]), []).append(rec)
    own_by_trace: dict[str, set[str]] = {}
    for trace_id, spans in spans_by_trace.items():
        by_id = {str(s.get("spanId")): s for s in spans if s.get("spanId")}
        agents = [s for s in spans if is_agent_span(s) and _service(s)]
        if not agents:  # another framework: its root kind (obs336)
            agents = [s for s in spans if root_kind(s) and _service(s)]
        roots = [a for a in agents if not _has_agent_ancestor(a, by_id)] or agents
        if roots:
            root = min(roots, key=lambda a: int(a.get("startTimeUnixNano") or 0))
            own_by_trace[trace_id] = {str(_service(root))}
        elif body_services:
            own_by_trace[trace_id] = set(body_services)
    if not own_by_trace and not body_services:
        return records
    kept: list[dict[str, Any]] = []
    for rec in records:
        service = _service(rec)
        own = own_by_trace.get(str(rec.get("traceId") or "")) or body_services
        if service is None or not own or service in own:
            kept.append(rec)
    return kept


def _scoped(trace_id: Any, span_id: Any) -> Any:
    return f"{trace_id}:{span_id}" if trace_id and span_id else span_id


def scope_span_ids(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``spanId`` / ``parentSpanId`` → ``<traceId>:<id>`` (N17-obs315 MT2). A snapshot-restored
    microVM draws the same span-id sequence again, and the library keys spans by span id
    alone — a later turn would take an earlier turn's slot (reordered, or merged into it)."""
    out: list[dict[str, Any]] = []
    for rec in records:
        trace = rec.get("traceId")
        if trace and (rec.get("spanId") or rec.get("parentSpanId")):
            rec = dict(rec)
            for key in ("spanId", "parentSpanId"):
                if rec.get(key):
                    rec[key] = _scoped(trace, rec[key])
        out.append(rec)
    return out


class FinalAnswerSessionMapper(CloudWatchSessionMapper):
    """``CloudWatchSessionMapper`` whose agent response is each turn's final answer
    (N17-obs315) — same rule as Global AgentCore Evaluations: final response from the
    invoke_agent span.

    AgentCore Evaluations (``supported-frameworks-strands``) groups a session's spans by
    trace (one trace = one turn) and classifies them by ``gen_ai.operation.name``: the agent
    response and the user prompt come from the ``invoke_agent`` span only — its correlated
    record's ``body.output.messages[role=assistant].content.message`` (with a finish_reason)
    and ``body.input.messages[role=user]``; ``chat`` spans give message history, and
    ``execute_tool`` spans the tool call, whose output is ``{"message": …, "id": …}``.

    ADOT files every assistant message under ``body.output.messages`` — a ``chat`` span's
    replayed history (``gen_ai.assistant.message``) ahead of its own ``gen_ai.choice``, a tool
    span's result — and the library took the FIRST assistant text of the latest-STARTING
    span: the pre-tool preamble once ``aws/spans`` records are in the result set. Per trace:

    1. the output of the last-ending ``invoke_agent`` span that has one (its ``gen_ai.choice``,
       ``str(AgentResult)``); an empty text stays ``""`` — no answer is scored as no answer;
    2. without one (the turn died after a tool call, or ``runtime_logs`` mode with no span
       records to classify): the last finish_reason-tagged choice other than ``tool_use`` by
       span END time, else the last tagged choice (the text the user saw);
    3. else the last own assistant message (``{"message": …}`` without a finish_reason), else
       ``""`` — a failed turn never inherits an earlier turn's text (MT1).

    Replayed history (content keyed ``content``) and tool results (content with an ``id``)
    are never the answer.

    Tool calls are the trace's OWN (audit 1.3): one per ``execute_tool`` span — name from
    ``gen_ai.tool.name``, arguments and result from its record — or, with no span records to
    classify (``runtime_logs``), the ``toolUse`` blocks of the trace's own messages. The
    library rebuilt them from every ``toolUse`` in a body, replayed history included, so a
    turn that called nothing showed the previous turn's call. Like the managed service, a
    ``max_tokens`` continuation scores the continuing ``invoke_agent``'s output only, and
    text written before the last tool call is not scored (``AgentResult`` semantics).
    """

    def __init__(self) -> None:
        SessionMapper.__init__(self)  # skip the library's deprecation warning: chosen on purpose
        self._tagged_session = False
        self._catalog = tool_catalog.Catalog()

    def map_to_session(self, spans: list[dict[str, Any]], session_id: str) -> Session:
        # a session whose records carry finish_reason-tagged choices: a trace without one
        # made no call of its own (a failed turn) — its replayed history is not its calls
        self._catalog = tool_catalog.build(spans)
        self._tagged_session = any(
            isinstance(msg, dict) and isinstance(msg.get("content"), dict)
            and msg["content"].get("finish_reason") and "message" in msg["content"]
            for span in spans if (body := get_body(span))
            for msg in (body.get("output") or {}).get("messages") or [])
        return super().map_to_session(spans, session_id)

    def _body_to_messages(self, body: dict) -> list[UserMessage | AssistantMessage]:
        """An InferenceSpan's messages with each block's SOURCE role (N17-obs401): ADOT files
        an ``execute_tool`` record's result (``{"message": …, "id": …}``) under
        ``body.output`` as an assistant message, and the library re-emitted it as ASSISTANT
        text — judges read a loaded SKILL.md or a calculator's ``9`` as words the agent
        wrote. Such a block is the tool's: a ``ToolResultContent`` (tool role, as the model
        receives it), never assistant text."""
        output = (body.get("output") or {}).get("messages") or []
        results = [m for m in output if isinstance(m, dict)
                   and isinstance(m.get("content"), dict) and m["content"].get("id")]
        if not results:
            return super()._body_to_messages(body)
        rest = [m for m in output if m not in results]
        messages = super()._body_to_messages(
            {**body, "output": {**(body.get("output") or {}), "messages": rest}})
        for msg in results:
            content = msg["content"]
            messages.append(UserMessage(content=[ToolResultContent(
                content=self._extract_text_from_content(content) or "",
                tool_call_id=str(content["id"]))]))
        return messages

    def _own_messages(self, body: dict) -> list[tuple[str | None, str]]:
        """``(finish_reason, text)`` of the body's OWN assistant messages: content keyed
        ``message`` — never replayed history (keyed ``content``, N17-obs315 MT1) nor a tool
        result (carries an ``id``)."""
        out: list[tuple[str | None, str]] = []
        for msg in (body.get("output") or {}).get("messages") or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if (msg.get("role") == "assistant" and isinstance(content, dict)
                    and "message" in content and "id" not in content):
                text = self._extract_text_from_content(content) or ""
                finish = content.get("finish_reason")
                out.append((str(finish) if finish else None, text.strip()))
        return out

    def _tool_record(self, body: dict | None) -> tuple[str | None, dict, str | None]:
        """``(call id, arguments, result text)`` of an ``execute_tool`` record: input
        ``{"role": "tool", "content": <args JSON>, "id"}``, output ``{"message", "id"}``."""
        call_id, arguments, result = None, {}, None
        for msg in ((body or {}).get("input") or {}).get("messages") or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if msg.get("role") == "tool" and isinstance(content, dict) and content.get("id"):
                call_id = str(content["id"])
                raw = content.get("content")
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    parsed = None
                arguments = parsed if isinstance(parsed, dict) else {}
        for msg in ((body or {}).get("output") or {}).get("messages") or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, dict) and content.get("id"):
                call_id = call_id or str(content["id"])
                result = self._extract_text_from_content(content) or ""
        return call_id, arguments, result

    def _own_tool_uses(self, body: dict) -> list[ToolCall]:
        """``toolUse`` blocks of the body's own MODEL answers (``gen_ai.choice`` with a
        finish_reason) — never history (``{"content": …}``), nor the event-loop cycle's
        copy of the same message (``{"message", "tool.result"}``, no finish_reason)."""
        calls: list[ToolCall] = []
        for msg in (body.get("output") or {}).get("messages") or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if (msg.get("role") != "assistant" or not isinstance(content, dict)
                    or "message" not in content or "id" in content
                    or not content.get("finish_reason")):
                continue
            for item in self._parse_message_content(content["message"]) or []:
                if isinstance(item, dict) and isinstance(item.get("toolUse"), dict):
                    use = item["toolUse"]
                    calls.append(ToolCall(name=str(use.get("name") or ""),
                                          arguments=use.get("input") or {},
                                          tool_call_id=use.get("toolUseId")))
        return calls

    def _own_tools(self, spans: list[dict], session_id: str) -> list[ToolExecutionSpan]:
        """The trace's tool calls in time order. Call ids are not unique — GLM numbers them
        ``call_0`` in every model call — so results pair with calls by id IN ORDER (a queue
        per id), never by a lookup that would hand every call the first result."""
        by_start = sorted(spans, key=lambda s: s.get("start_time") or s.get("timeUnixNano") or 0)
        records: dict[str, list[tuple[dict, str]]] = {}  # call id → [(record span, result)]
        input_results: dict[str, list[ToolResult]] = {}  # toolResult blocks, by call id
        seen_inputs: set[tuple[str, str]] = set()
        own: list[tuple[dict, ToolCall]] = []  # this trace's own toolUse blocks, in order
        for span in by_start:
            body = get_body(span)
            if not body:
                continue
            call_id, _args, result = self._tool_record(body)
            if call_id and result is not None:
                records.setdefault(call_id, []).append((span, result))
            for tool_result in self._extract_tool_results(body):
                key = (tool_result.tool_call_id or "", tool_result.content)
                if tool_result.tool_call_id and key not in seen_inputs:  # history repeats
                    seen_inputs.add(key)
                    input_results.setdefault(tool_result.tool_call_id, []).append(tool_result)
            own += [(span, call) for call in self._own_tool_uses(body)]

        def result_of(call_id: str | None, error: str | None = None) -> tuple[ToolResult, Any]:
            queue = records.get(call_id or "")
            if queue:
                span, text = queue.pop(0)
                return ToolResult(content=text, error=error, tool_call_id=call_id), span
            fallback = input_results.get(call_id or "")
            if fallback:
                return fallback.pop(0), None
            return ToolResult(content="", error=error, tool_call_id=call_id), None

        tool_spans = [s for s in by_start if _is_tool_span(s)]
        out: list[ToolExecutionSpan] = []
        if tool_spans:  # Global: one tool call per execute_tool span of the trace
            unused = list(own)
            for span in tool_spans:
                attributes = span.get("attributes") or {}
                call_id, arguments, result = self._tool_record(get_body(span))
                call_id = call_id or attributes.get("gen_ai.tool.call.id")
                name = str(attributes.get("gen_ai.tool.name") or str(
                    span.get("name") or "").removeprefix("execute_tool").strip())
                # a span without its record: the own toolUse with that id, else that name
                match = next((u for u in unused if call_id and u[1].tool_call_id == call_id),
                             None) or next((u for u in unused if not call_id
                                            and u[1].name == name), None)
                if match is not None:
                    unused.remove(match)
                    call_id = call_id or match[1].tool_call_id
                    arguments = arguments or match[1].arguments
                status = str(attributes.get("gen_ai.tool.status") or "").lower()
                failed = status == "error" or (span.get("status") or {}).get("code") == "ERROR"
                error = "error" if failed else None
                if result is not None:  # the span's own record
                    queue = records.get(call_id or "") or []
                    if queue and queue[0][0] is span:
                        queue.pop(0)
                    tool_result = ToolResult(content=result, error=error, tool_call_id=call_id)
                else:
                    tool_result, _ = result_of(call_id, error)
                out.append(ToolExecutionSpan(
                    span_info=self._create_span_info(span, session_id),
                    tool_call=ToolCall(name=name, arguments=arguments, tool_call_id=call_id),
                    tool_result=tool_result, metadata={}))
            return out
        if any(is_agent_span(s) for s in spans):
            # span records but no execute_tool span: the turn executed nothing — a toolUse
            # the model wrote and never ran (a max_tokens stop) is no call (N17-obs381)
            return out
        for span, call in own:  # no span records to classify (runtime_logs)
            if not (records.get(call.tool_call_id or "")
                    or any(not r.error for r in input_results.get(call.tool_call_id or "", []))):
                continue  # never executed: no tool record, no successful result (obs381)
            tool_result, source = result_of(call.tool_call_id)
            out.append(ToolExecutionSpan(
                span_info=self._create_span_info(source or span, session_id), tool_call=call,
                tool_result=tool_result, metadata={}))
        return out

    def _convert_trace(self, trace_id: str, spans: list[dict], session_id: str) -> Trace:
        trace = super()._convert_trace(trace_id, spans, session_id)
        if not any(_is_tool_span(s) for s in spans) and not self._tagged_session:
            # no span records and no finish_reason markers (another format): neither rule
            # applies, so the library's calls stay — the one fallback
            tools = [s for s in trace.spans if isinstance(s, ToolExecutionSpan)]
        else:
            tools = self._own_tools(spans, session_id)
        kept = [s for s in trace.spans if not isinstance(s, ToolExecutionSpan)]
        called = list(dict.fromkeys(t.tool_call.name for t in tools))
        declared = next((names for s in sorted(spans, key=_end_time, reverse=True)
                         if is_agent_span(s) and (names := tool_catalog.declared_names(s))),
                        [])
        for span in kept:
            if isinstance(span, AgentInvocationSpan):
                # N17-obs334: every tool the agent had, with descriptions (Global); without
                # span attributes (runtime_logs) the tools the trace called
                span.available_tools = (self._catalog.tools(declared + called)
                                        if self._catalog else
                                        [ToolConfig(name=n) for n in sorted(called)])
        inference = [s for s in kept if not isinstance(s, AgentInvocationSpan)]
        agent = [s for s in kept if isinstance(s, AgentInvocationSpan)]
        return Trace(spans=[*inference, *tools, *agent], trace_id=trace.trace_id,
                     session_id=trace.session_id)

    def _create_agent_invocation_span(
        self, spans: list[dict], tool_calls: dict[str, ToolCall], session_id: str
    ) -> AgentInvocationSpan | None:
        by_end = sorted(spans, key=_end_time)
        agents = [s for s in by_end if is_agent_span(s) and get_body(s)]
        user_prompt = next((prompt for span in [*agents, *spans] if (body := get_body(span))
                            and (prompt := self._extract_user_prompt(body))), None)
        if not user_prompt:
            return None
        own_messages = [(span, finish, text) for span in by_end
                        if (body := get_body(span)) for finish, text in self._own_messages(body)]
        tagged = [t for t in own_messages if t[1]]
        agent_ids = {id(s) for s in agents}
        from_agent = [t for t in tagged if id(t[0]) in agent_ids]
        answered = [t for t in tagged if t[1] != "tool_use"]
        if from_agent:
            best, _, response = from_agent[-1]
        elif answered:
            best, _, response = answered[-1]
        elif tagged:
            best, _, response = tagged[-1]
        elif own_messages:
            best, _, response = own_messages[-1]
        else:  # the turn produced nothing of its own: no answer, never an earlier turn's text
            best, response = by_end[-1], ""
        # N17-obs373: the turn's (last-ending) invoke_agent span status — ERROR means the
        # turn failed; Global answers SpanStatusErrorException and scores nothing for it
        invocations = [s for s in by_end if is_agent_span(s) and s.get("status")]
        status = str(((invocations[-1].get("status") or {}) if invocations else {})
                     .get("code") or "")
        metadata: dict[str, Any] = {"span_status": status.upper()} if status else {}
        if not invocations and (self._tagged_session or not response) and not any(
                self._agent_result(body) for span in by_end if (body := get_body(span))):
            # N17-obs382: no span records (runtime_logs) — the turn failed when the agent
            # never returned: no invoke_agent result (``str(AgentResult)``, a plain-text
            # gen_ai.choice) in the trace's records, as on every errored / timed-out turn.
            # Only in a session that tags its choices (Strands' format) or for an empty
            # turn, so an untagged format is never written off wholesale.
            metadata = {"span_status": "ERROR", "span_status_source": "runtime_logs"}
        return AgentInvocationSpan(
            span_info=self._create_span_info(best, session_id), user_prompt=user_prompt,
            agent_response=response,
            system_prompt=self._system_prompt(
                [*[s for s in by_end if not is_agent_span(s)], *by_end]),
            available_tools=[ToolConfig(name=n)
                             for n in sorted({c.name for c in tool_calls.values()})],
            metadata=metadata,
        )

    def _system_prompt(self, spans: list[dict]) -> str | None:
        """The system prompt the MODEL saw this turn (N17-obs399): the first model-call
        (``chat``) record's system message, else the agent span's own. AgentSkills injects
        the offered catalogue (``<available_skills>``) into the model call's system message
        as a text block — the agent span carries only the configured prompt. The library
        dropped every system message, so ``parse_available_skills`` read [] and the skill
        judges never saw the alternatives."""
        for span in spans:
            body = get_body(span)
            for msg in ((body or {}).get("input") or {}).get("messages") or []:
                if isinstance(msg, dict) and msg.get("role") == "system":
                    text = self._extract_text_from_content(msg.get("content") or {})
                    if text:
                        return text
        return None

    def _agent_result(self, body: dict) -> bool:
        """The body carries the agent's own result: an output ``gen_ai.choice`` with a
        finish_reason whose message is plain text (``str(AgentResult)``), not the JSON
        content-block list a model choice serialises."""
        for msg in (body.get("output") or {}).get("messages") or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if (isinstance(content, dict) and content.get("finish_reason")
                    and "id" not in content and isinstance(content.get("message"), str)
                    and self._parse_message_content(content["message"]) is None):
                return True
        return False


class _DetectingMapper(SessionMapper):
    """The library's format detection, with :class:`FinalAnswerSessionMapper` standing in
    for its CloudWatch-body mapper and :class:`LangGraphSessionMapper` for its LangChain
    ones (r17-bf7, LangGraph; other frameworks' mappers are used as detected)."""

    def map_to_session(self, data: Any, session_id: str) -> Session:
        if isinstance(data, list) and is_langgraph(data):
            return LangGraphSessionMapper().map_to_session(data, session_id)
        # a Strands agent's LangChain records (a LangChain tool) do not decide its format
        mapper = detect_otel_mapper([s for s in data if not is_langgraph([s])]
                                    if isinstance(data, list) else data)
        if type(mapper) is CloudWatchSessionMapper:
            mapper = FinalAnswerSessionMapper()
        return mapper.map_to_session(data, session_id)


class LaunchpadSpansProvider(CloudWatchProvider):
    """``get_evaluation_data(session_id) -> TaskOutput`` — a ``TypedDict``
    ``{"output": str, "trajectory": Session}`` (item access, not attributes).

    ``log_groups`` are the groups the session's records live in (runtime group(s) and,
    with Transaction Search, ``aws/spans``). ``spans_source="runtime_logs"`` skips the
    best-effort ``aws/spans`` parent-hierarchy query; otherwise it runs against
    ``aws/spans`` exactly like the library. ``lookback_hours`` bounds every query window.
    """

    def __init__(
        self,
        logs_client: Any,
        log_groups: list[str],
        *,
        lookback_hours: int = 24,
        spans_source: SpansSource = "both",
        query_timeout_seconds: int = 55,
        mapper: Any = None,
        end_time: datetime | None = None,
        records_guard: Callable[[str, list[dict[str, Any]]], None] | None = None,
    ) -> None:
        # Deliberately no super().__init__(): it would build its own Logs client (funnel).
        groups = [g for g in log_groups if g]
        if not groups:
            raise ValueError("LaunchpadSpansProvider needs at least one log group")
        self._client = logs_client
        self._log_groups = groups
        runtime_groups = [g for g in groups if g != SPANS_LOG_GROUP]
        self._log_group = (runtime_groups or groups)[0]
        self._lookback_hours = max(1, int(lookback_hours))
        self._lookback_days = max(1, math.ceil(self._lookback_hours / 24))
        self._query_timeout_seconds = query_timeout_seconds
        self._mapper = mapper or _DetectingMapper()
        self._end_time = end_time
        self._records_guard = records_guard
        self._spans_source: SpansSource = spans_source
        # N17-obs316: the raw content records of the sessions read, for the runtime-logs
        # extraction reference (the main query of the calling thread, then per session)
        self._local = threading.local()
        self._records: dict[str, list[dict[str, Any]]] = {}

    # ── public ──────────────────────────────────────────────────────────────
    def get_evaluation_data(self, session_id: str):  # type: ignore[override]
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            raise ValueError(f"invalid session id for a spans query: {session_id!r}")
        self._local.records = None
        self._records.pop(session_id, None)
        self._local.session_id = session_id
        # The upstream provider uses `like` (substring matching). The session boundary is
        # exact; parsing and mapping remain the pinned SDK's operations after authorization.
        query = (f"fields @message | filter attributes.session.id = '{session_id}'"
                 " | sort @timestamp asc | limit 10000")
        raw_records = self._run_logs_insights_query(query)
        if not raw_records:
            raise SessionNotFoundError("no spans found for session")
        span_dicts = CloudWatchLogsParser(raw_records).parse()
        if not span_dicts:
            raise SessionNotFoundError("no normalizable spans found for session")
        hierarchy = self._fetch_span_hierarchy(session_id)
        for span in span_dicts:
            if parent := hierarchy.get(span.get("span_id", "")):
                span["parent_span_id"] = parent
        session = self._mapper.map_to_session(span_dicts, session_id)
        if not session.traces:
            raise SessionNotFoundError("no convertible spans found for session")
        data = {"trajectory": session, "output": self._extract_output(session)}
        self._records[session_id] = [r for r in (self._local.records or [])
                                     if isinstance(r.get("body"), dict)]
        # MT2: turns in time order (span ids are trace-scoped, so a restored microVM's repeated
        # ids no longer merge or reorder them); the output is the LAST turn's answer
        session = data["trajectory"]
        session.traces.sort(key=lambda t: min((s.span_info.start_time for s in t.spans),
                                              default=datetime.max.replace(tzinfo=UTC)))
        data["output"] = self._extract_output(session)
        return data

    def origin_reference(self, session_id: str) -> dict[str, Any] | None:
        """The extraction reference from the session's own runtime records (N17-obs316) —
        after :meth:`get_evaluation_data` read them; ``None`` before or without a turn."""
        return extraction_check.origin_reference(self._records.get(session_id) or [])

    def session(self, session_id: str) -> Session:
        """The strands ``Session`` alone (detectors, recommendations, trajectory scoring)."""
        return self.get_evaluation_data(session_id)["trajectory"]

    # ── query plumbing (the only two places the base touches CloudWatch) ────
    def _window(self) -> tuple[int, int]:
        end = self._end_time or datetime.now(tz=UTC)
        start = end - timedelta(hours=self._lookback_hours)
        return int(start.timestamp()), int(end.timestamp())

    def _run_logs_insights_query(self, query: str) -> list[dict[str, Any]]:
        start, end = self._window()
        try:
            response = self._client.start_query(
                logGroupNames=list(self._log_groups), startTime=start, endTime=end,
                queryString=query,
            )
            rows = self._poll_query_results(response["queryId"])
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"CloudWatch: failed to query spans: {exc}") from exc
        # N17-obs307: credential-like attributes are masked before any mapper reads the
        # records — nothing downstream (judge prompts, detectors, evidence) can see them
        records = query_records(rows, strict=self._records_guard is not None)
        if self._records_guard is not None:
            self._records_guard(self._local.session_id, records)
        # N17-obs315: unified-telemetry spans read like split-mode ones (inline events → body)
        # N17-obs380: only the evaluated agent's records (an A2A peer shares the trace)
        out = scope_span_ids(split_inline_events(
            own_agent_records([redact_value(r) for r in records])))
        self._local.records = out
        return out

    def _fetch_span_hierarchy(self, session_id: str) -> dict[str, str]:
        if self._spans_source == "runtime_logs":
            return {}
        query = (
            f"fields spanId, parentSpanId, traceId | filter attributes.session.id = "
            f"'{session_id}'"
            " | limit 10000"
        )
        if self._records_guard is not None:
            # Full provenance is required even for the separate hierarchy enrichment read.
            query = (f"fields @message | filter attributes.session.id = '{session_id}'"
                     " | limit 10000")
        start, end = self._window()
        try:
            response = self._client.start_query(
                logGroupNames=[SPANS_LOG_GROUP], startTime=start, endTime=end,
                queryString=query,
            )
            rows = self._poll_query_results(response["queryId"])
        except Exception:  # best-effort, exactly like the library
            return {}
        if self._records_guard is not None:
            records = query_records(rows, strict=True)
            # Keep authorization outside the best-effort AWS-error catch above.
            if records:
                self._records_guard(session_id, records)
            fields_rows = records
        else:
            fields_rows = [{f["field"]: f["value"] for f in row} for row in rows]
        hierarchy: dict[str, str] = {}
        for fields in fields_rows:
            if fields.get("spanId") and fields.get("parentSpanId"):
                trace = fields.get("traceId")
                hierarchy[_scoped(trace, fields["spanId"])] = _scoped(trace, fields["parentSpanId"])
        return hierarchy
