"""M1 — per-turn reconciliation of the extracted evaluation input with the origin logs.

Extracted side: the PRODUCTION ``LaunchpadSpansProvider`` (with its default mapper) of the
checkout the workbench runs from, over :class:`~evalwb.cache.CachedLogs`, built the way
``StrandsEngine.provider`` builds it (``[aws/spans, <runtime group>]`` + ``spans_source``).
Per trace (= turn) it yields the judged user prompt, answer and tool calls.

Origin side (the oracle, never the code under test):

- the turn's prompt and answer are the console's own rule,
  ``observability.eval_turns_from_content_logs`` run on that trace's runtime records
  (the transcript the user is shown: the ``end_turn`` output);
- the turn's tool calls are the trace's ``execute_tool`` span records in ``aws/spans`` —
  the agent framework's own (scope ``strands.telemetry.tracer``) when the trace has any, so
  the ADOT MCP-client span under a gateway tool (``mcp tools/call …``, also tagged
  ``execute_tool``) is not counted twice — and of the session's OWN agent: a called A2A
  peer's spans carry the propagated trace and ``session.id`` but the peer runtime's
  ``service.name`` (:func:`own_services`).

A turn whose origin has no ``end_turn`` output (timed out, died after a tool call) is a
FAILED turn: it has no final answer to compare, so it enters the M1 denominator only when
its extraction is wrong anyway — an earlier turn's text (LEAK, N17-obs315 MT1), tool calls
the turn never made (TOOLS) or a wrong prompt. Texts compare with all whitespace
removed (the console joins text blocks with newlines, the mapper with spaces) and as a
prefix when the console cut its copy at 4000 characters.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from evalwb.cache import RUNTIME, SPANS, CachedLogs, SessionRecords

CONSOLE_TEXT_CAP = 4000
STRANDS_SCOPE = "strands.telemetry.tracer"
MODES = {"both": "both", "runtime_only": "runtime_logs"}
_WS = re.compile(r"\s+")


def norm(text: Any) -> str:
    return _WS.sub("", str(text or ""))


def same_text(extracted: Any, origin: Any) -> bool:
    """Equal modulo whitespace; a prefix match when the console truncated ``origin``."""
    got, want = norm(extracted), norm(origin)
    if len(str(origin or "")) >= CONSOLE_TEXT_CAP:
        return got.startswith(want)
    return got == want


@dataclass
class OriginTurn:
    trace_id: str
    start_ns: int
    user: str = ""
    first_user: str = ""
    answer: str = ""
    completed: bool = False
    tools: list[str] = field(default_factory=list)


@dataclass
class ExtractedTurn:
    trace_id: str
    prompt: str = ""
    answer: str | None = None
    tools: list[str] = field(default_factory=list)
    # the production engine would NOT judge this turn (``engine_core.failed_turn``: agent
    # span status ERROR, N17-obs373) — False on code without that rule
    excluded: bool = False


@dataclass
class TurnRow:
    session_id: str
    turn: int
    trace_id: str
    status: str
    prompt_ok: bool | None
    tools_ok: bool | None
    extracted_answer: str | None
    origin_answer: str
    extracted_prompt: str
    origin_prompt: str
    extracted_tools: list[str]
    origin_tools: list[str]
    completed: bool
    judge_excluded: bool = False


@dataclass
class SessionResult:
    session_id: str
    mode: str
    rows: list[TurnRow]
    order_ok: bool
    output: str = ""
    error: str | None = None

    @property
    def completed(self) -> int:
        return sum(1 for r in self.rows if r.completed)

    @property
    def matched(self) -> int:
        return sum(1 for r in self.rows if r.completed and r.status == "match")

    @property
    def leaks(self) -> int:
        return sum(1 for r in self.rows if r.status == "LEAK")

    @property
    def failed_wrong(self) -> int:
        """Failed turns whose extraction is wrong (LEAK, TOOLS, PROMPT, MISSING)."""
        return sum(1 for r in self.rows if not r.completed
                   and r.status not in ("failed_turn", "EXTRA"))

    def to_json(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "mode": self.mode, "order_ok": self.order_ok,
                "output": self.output, "error": self.error, "completed": self.completed,
                "matched": self.matched, "leaks": self.leaks,
                "failed_wrong": self.failed_wrong,
                "rows": [asdict(r) for r in self.rows]}


# ── origin ──────────────────────────────────────────────────────────────────

def tool_name(span: dict[str, Any]) -> str | None:
    attributes = span.get("attributes") or {}
    name = str(span.get("name") or "")
    if attributes.get("gen_ai.operation.name") != "execute_tool" and not name.startswith(
            "execute_tool "):
        return None
    return str(attributes.get("gen_ai.tool.name") or name.removeprefix("execute_tool ").strip())


def _service(rec: dict[str, Any]) -> str:
    return str(((rec.get("resource") or {}).get("attributes") or {}).get("service.name") or "")


_RUNTIME_GROUP = re.compile(r"/runtimes/(?P<name>.+)-[A-Za-z0-9]+-(?P<endpoint>[^-/]+)$")


def own_services(entry: SessionRecords) -> set[str]:
    """The ``service.name`` of the session's own runtime: its runtime-group records' (and
    the name the runtime group encodes, ``…/runtimes/<name>-<id>-<endpoint>`` →
    ``<name>.<endpoint>``, for records whose ``resource`` was redacted away)."""
    services = {svc for rec in entry.group(RUNTIME) if (svc := _service(rec))}
    found = _RUNTIME_GROUP.search(entry.log_group or "")
    if found:
        services.add(f"{found['name']}.{found['endpoint']}")
    return services


def agent_tool_spans(entry: SessionRecords) -> list[dict[str, Any]]:
    """The session agent's own ``execute_tool`` span records (Strands scope when present,
    own service when known), in start order."""
    from app.services.strands_eval.langgraph_mapper import (
        LANGGRAPH_SCOPES,
        _kind_attribute,
        is_langgraph,
    )

    services = own_services(entry)
    records = entry.group(SPANS)
    if is_langgraph(records):  # r17-bm: OpenInference / Traceloop JS tools are not execute_tool
        spans = [r for r in records if _kind_attribute(r) == "tool"
                 and (r.get("scope") or {}).get("name") in LANGGRAPH_SCOPES
                 and (not services or _service(r) in services or not _service(r))]
        return sorted(spans, key=lambda r: int(r.get("startTimeUnixNano") or 0))
    spans = [r for r in records if tool_name(r)
             and (not services or _service(r) in services or not _service(r))]
    own = [r for r in spans if (r.get("scope") or {}).get("name") == STRANDS_SCOPE]
    return sorted(own or spans, key=lambda r: int(r.get("startTimeUnixNano") or 0))


def origin_turns(entry: SessionRecords) -> list[OriginTurn]:
    """The session's turns from its origin records, in time order (one trace = one turn)."""
    from app.services import observability

    spans, runtime = entry.group(SPANS), entry.group(RUNTIME)
    starts: dict[str, int] = {}
    for rec in spans:
        if rec.get("traceId"):
            start = int(rec.get("startTimeUnixNano") or 0)
            starts[rec["traceId"]] = min(starts.get(rec["traceId"], start), start)
    turns: dict[str, OriginTurn] = {}
    for rec in sorted(runtime, key=lambda r: r.get("timeUnixNano") or 0):
        trace = rec.get("traceId")
        body = rec.get("body")
        if not trace or not isinstance(body, dict):
            continue
        turn = turns.setdefault(trace, OriginTurn(
            trace_id=trace, start_ns=starts.get(trace, int(rec.get("timeUnixNano") or 0))))
        for kind, role, text, finish in observability._iter_body_messages(body):
            if kind == "input" and role == "user" and text and not turn.first_user:
                turn.first_user = text
            if kind == "output" and role == "assistant" and finish == "end_turn":
                turn.completed = True
    for rec in agent_tool_spans(entry):
        if rec.get("traceId") in turns:
            turns[rec["traceId"]].tools.append(str(tool_name(rec)))
    started = datetime.fromtimestamp(min((t.start_ns for t in turns.values()), default=0) / 1e9,
                                     UTC)
    for turn in turns.values():
        console = observability.eval_turns_from_content_logs(
            entry.log_group, entry.session_id, started, None,
            logs=CachedLogs({entry.session_id: entry}, trace=turn.trace_id))
        turn.user = next((t["text"] for t in console if t["role"] == "USER"), "")
        turn.answer = next((t["text"] for t in console if t["role"] == "ASSISTANT"), "")
    ordered = sorted(turns.values(), key=lambda t: t.start_ns)
    return [t for t in ordered if t.first_user or t.user]


# ── extraction (production code) ───────────────────────────────────────────

def provider_for(entry: SessionRecords, sessions: dict[str, SessionRecords], mode: str) -> Any:
    from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider

    groups = [SPANS_LOG_GROUP, entry.log_group] if mode == "both" else [entry.log_group]
    return LaunchpadSpansProvider(CachedLogs(sessions), groups, spans_source=MODES[mode])


def extracted_turns(task_output: Any) -> list[ExtractedTurn]:
    from strands_evals.types.trace import AgentInvocationSpan, ToolExecutionSpan

    from app.services.strands_eval import engine_core

    failed_turn = getattr(engine_core, "failed_turn", None)
    out: list[ExtractedTurn] = []
    for trace in task_output["trajectory"].traces:
        turn = ExtractedTurn(trace_id=trace.trace_id)
        for span in trace.spans:
            if isinstance(span, AgentInvocationSpan):
                turn.prompt, turn.answer = span.user_prompt or "", span.agent_response
                turn.excluded = bool(failed_turn and failed_turn(span))
            elif isinstance(span, ToolExecutionSpan):
                turn.tools.append(span.tool_call.name)
        out.append(turn)
    return out


def reconcile_session(entry: SessionRecords, mode: str = "both",
                      sessions: dict[str, SessionRecords] | None = None) -> SessionResult:
    origin = origin_turns(entry)
    has_spans = bool(entry.group(SPANS))
    try:
        task_output = provider_for(entry, sessions or {entry.session_id: entry}, mode
                                   ).get_evaluation_data(entry.session_id)
    except Exception as exc:  # noqa: BLE001 — a provider failure is a row, not a crash
        rows = [TurnRow(entry.session_id, i + 1, o.trace_id, "MISSING", None, None, None,
                        o.answer, "", o.user, [], sorted(o.tools), o.completed)
                for i, o in enumerate(origin)]
        return SessionResult(entry.session_id, mode, rows, False,
                             error=f"{type(exc).__name__}: {exc}")
    extracted = extracted_turns(task_output)
    by_trace = {t.trace_id: t for t in extracted}
    # a failed turn's extraction must not be an EARLIER turn's text (replayed history only
    # flows forward; a failed turn's own visible text counts too — MT1)
    shown = [o.answer for o in origin]
    rows: list[TurnRow] = []
    for i, o in enumerate(origin):
        ext = by_trace.get(o.trace_id)
        if ext is None:
            rows.append(TurnRow(entry.session_id, i + 1, o.trace_id, "MISSING", None, None,
                                None, o.answer, "", o.user, [], sorted(o.tools), o.completed))
            continue
        # a failed turn may yield no agent span at all (nothing judged — Global skips it too)
        unjudged = not o.completed and ext.answer is None
        prompt_ok = None if unjudged else (same_text(ext.prompt, o.user)
                                           or same_text(ext.prompt, o.first_user))
        tools_ok = sorted(ext.tools) == sorted(o.tools) if has_spans else None
        if o.completed:
            status = "match" if ext.answer is not None and same_text(ext.answer, o.answer) \
                else "MISMATCH"
        else:
            leaked = bool(norm(ext.answer)) and any(
                norm(text) and same_text(ext.answer, text) for text in shown[:i])
            status = "LEAK" if leaked else "failed_turn"
        if status in ("match", "failed_turn") and prompt_ok is False:
            status = "PROMPT"
        if status in ("match", "failed_turn") and tools_ok is False:
            status = "TOOLS"
        rows.append(TurnRow(entry.session_id, i + 1, o.trace_id, status, prompt_ok, tools_ok,
                            ext.answer, o.answer, ext.prompt, o.user, sorted(ext.tools),
                            sorted(o.tools), o.completed, ext.excluded))
    known = {o.trace_id for o in origin}
    for ext in extracted:
        if ext.trace_id not in known and ext.answer is not None:
            rows.append(TurnRow(entry.session_id, len(rows) + 1, ext.trace_id, "EXTRA", None,
                                None, ext.answer, "", ext.prompt, "", sorted(ext.tools), [],
                                False))
    order = [t.trace_id for t in extracted if t.trace_id in known]
    order_ok = order == [o.trace_id for o in origin if o.trace_id in by_trace]
    return SessionResult(entry.session_id, mode, rows, order_ok,
                         output=str(task_output.get("output") or ""))


def m1_summary(results: list[SessionResult]) -> dict[str, Any]:
    """``{matched, denominator, rate, completed, failed_turns, failed_wrong, leaks, extra,
    sessions, sessions_ok}`` — the denominator is every completed turn, every failed turn
    extracted wrongly and every extracted turn the origin does not have."""
    completed = sum(r.completed for r in results)
    matched = sum(r.matched for r in results)
    wrong = sum(r.failed_wrong for r in results)
    failed = sum(1 for r in results for row in r.rows if not row.completed
                 and row.status != "EXTRA")
    extra = sum(1 for r in results for row in r.rows if row.status == "EXTRA")
    denominator = completed + wrong + extra
    sessions_ok = sum(1 for r in results if r.error is None and r.order_ok and all(
        row.status in ("match", "failed_turn") for row in r.rows))
    return {"matched": matched, "denominator": denominator,
            "rate": round(matched / denominator, 4) if denominator else None,
            "completed": completed, "failed_turns": failed, "failed_wrong": wrong,
            "leaks": sum(r.leaks for r in results), "extra": extra,
            "sessions": len(results), "sessions_ok": sessions_ok}
