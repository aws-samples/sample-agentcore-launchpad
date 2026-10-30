"""The custom-judge placeholder catalogue and its renderer (N17-obs311).

A custom LLM judge's instructions name what it reads — ``{context}``,
``{actual_tool_trajectory}``, ``{assertions}`` … The strands ``OutputEvaluator`` puts the
rubric into the prompt verbatim, so before N17-obs311 those names reached the judge model
as literal text and a verdict about tool use was inferred from the answer. Here every
``{name}`` is filled from the ``EvaluationData`` case before the judge call:

- trace-derived — ``context`` (the session transcript: user turns, tool calls, answers —
  for a TRACE judge the earlier turns plus the judged turn's prompt and tool calls),
  ``available_tools`` (the tool specs on the agent spans), ``actual_tool_trajectory`` (the
  ordered tool calls with their input and a short result), ``assistant_turn`` (the judged
  turn's answer), ``user_message`` (the judged turn's prompt; a SESSION judge's: the first);
- ground truth — ``expected_response`` (alias ``expected_output``), ``assertions``,
  ``expected_tool_trajectory`` (alias ``expected_trajectory``), from the dataset scenario.

A value the case does not carry — and any name outside the catalogue — renders as
:data:`NOT_AVAILABLE`, never as the placeholder. Substitution is a single pass, so text
inside a rendered value is never re-expanded. :data:`GROUND_TRUTH_PLACEHOLDERS` is the one
list the online / canary / recommendation refusals and the evaluator catalog import.

Pure library (shipped into the worker zip): imports nothing from ``app.*`` but its own
package.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from strands_evals.extractors.tools_use_extractor import extract_agent_tools_used_from_trace
from strands_evals.types.evaluation import EvaluationData
from strands_evals.types.trace import AgentInvocationSpan, Session, ToolExecutionSpan

NOT_AVAILABLE = "(not available)"
NO_TOOL_CALLS = "(no tool calls)"
MAX_VALUE_CHARS = 12_000  # one rendered placeholder
_MAX_INPUT_CHARS = 1_000  # one tool call's input
_MAX_RESULT_CHARS = 300  # one tool call's result

# Judge placeholders fed from dataset reference inputs rather than from the trace itself —
# a live session carries none of them (online evaluation / canary / SCORE NOW refuse such
# judges). ``{actual_tool_trajectory}`` is NOT one: it is measured from the session spans.
GROUND_TRUTH_PLACEHOLDERS = ("expected_response", "expected_tool_trajectory", "assertions")

# The documented placeholders per evaluation level (the evaluator form offers these).
LEVEL_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "TRACE": ("context", "assistant_turn", "expected_response"),
    "SESSION": ("context", "available_tools", "actual_tool_trajectory",
                "expected_tool_trajectory", "assertions"),
    "TOOL_CALL": ("context", "available_tools", "tool_turn", "invoked_skill", "skill_content",
                  "available_skills", "user_message"),
}

# Offered placeholders with no per-case value. Empty since N17-obs332: ``tool_turn`` and the
# skill placeholders are rendered from the per-call TOOL_CALL case (``engine_core.tool_cases``
# — the engine runs a custom TOOL_CALL judge once per call); on a case with no judged call
# they read ``NOT_AVAILABLE``.
SESSION_UNRENDERED: tuple[str, ...] = ()

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _session(case: EvaluationData) -> Session | None:
    trajectory = case.actual_trajectory
    return trajectory if isinstance(trajectory, Session) else None


def _tool_calls(case: EvaluationData) -> list[dict[str, Any]] | None:
    """``[{name, input, tool_result}]`` in call order; ``None`` when the case has no
    trajectory at all (as opposed to one with zero calls)."""
    trajectory = case.actual_trajectory
    if trajectory is None:
        return None
    if isinstance(trajectory, Session):
        return extract_agent_tools_used_from_trace(trajectory)
    calls: list[dict[str, Any]] = []
    for item in trajectory if isinstance(trajectory, list) else []:
        if isinstance(item, dict):
            calls.append({"name": item.get("name") or item.get("toolName") or "",
                          "input": item.get("input", item.get("arguments")),
                          "tool_result": item.get("tool_result", item.get("result"))})
        else:
            calls.append({"name": str(item), "input": None, "tool_result": None})
    return calls


def _call_line(call: dict[str, Any]) -> str:
    """``name(input) -> short result``."""
    line = str(call.get("name") or "?")
    if call.get("input") not in (None, {}, ""):
        line += f"({_clip(_compact(call['input']), _MAX_INPUT_CHARS)})"
    result = call.get("tool_result")
    if result not in (None, ""):
        line += f" -> {_clip(' '.join(_compact(result).split()), _MAX_RESULT_CHARS)}"
    return line


def _span_call(span: ToolExecutionSpan) -> dict[str, Any]:
    return {"name": span.tool_call.name, "input": span.tool_call.arguments,
            "tool_result": span.tool_result.content if span.tool_result else None}


def _actual_tool_trajectory(case: EvaluationData) -> str | None:
    calls = _tool_calls(case)
    if calls is None:
        return None
    return "\n".join(f"{i}. {_call_line(c)}" for i, c in enumerate(calls, 1)) or NO_TOOL_CALLS


def _available_tools(case: EvaluationData) -> str | None:
    session = _session(case)
    if session is None:
        return None
    seen: dict[str, str] = {}
    for trace in session.traces:
        for span in trace.spans:
            if isinstance(span, AgentInvocationSpan):
                for tool in span.available_tools or []:
                    if tool.name and tool.name not in seen:
                        seen[tool.name] = tool.description or ""
    return "\n".join(f"- {n}" + (f": {d}" if d else "") for n, d in seen.items()) or None


def _context(case: EvaluationData) -> str | None:
    """The transcript: per trace the user turn, its tool calls and the answer. For a
    per-turn TRACE case (``metadata["judged_turn"]``, N17-obs324) the judged turn — the
    trajectory's last trace — stops after its prompt and tool calls, as Global's TRACE
    ``{context}``: earlier turns in full, the current turn without its answer (which is
    ``{assistant_turn}``). A per-call TOOL_CALL case (N17-obs332) is cut the same way, its
    last trace holding only the calls before the judged one."""
    session = _session(case)
    judged = bool((case.metadata or {}).get("judged_turn"))
    traces = list(session.traces) if session else []
    turns: list[str] = []
    for index, trace in enumerate(traces):
        agents = [s for s in trace.spans if isinstance(s, AgentInvocationSpan)]
        tools = sorted((s for s in trace.spans if isinstance(s, ToolExecutionSpan)),
                       key=lambda s: s.span_info.start_time)
        root = next((a for a in agents if a.span_info.parent_span_id is None),
                    agents[0] if agents else None)
        lines: list[str] = []
        if root is not None and root.user_prompt:
            lines.append(f"User: {root.user_prompt}")
        lines += [f"Tool call: {_call_line(_span_call(t))}" for t in tools]
        current = judged and index == len(traces) - 1
        if root is not None and root.agent_response and not current:
            lines.append(f"Assistant: {root.agent_response}")
        if lines:
            turns.append("\n".join(lines))
    if turns:
        return "\n\n".join(turns)
    fallback = [f"User: {case.input}" if case.input else "",
                f"Assistant: {case.actual_output}" if case.actual_output else ""]
    return "\n".join(x for x in fallback if x) or None


def _expected_trajectory(case: EvaluationData) -> str | None:
    if case.expected_trajectory is None:
        return None
    names = [str(x) for x in case.expected_trajectory]
    # N17-obs376: an explicit [] is a stated expectation, not missing ground truth
    return "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1)) or "(no tools expected)"


def _assertions(case: EvaluationData) -> str | None:
    items = [a.strip() for a in str(case.expected_assertion or "").splitlines() if a.strip()]
    return "\n".join(f"- {a}" for a in items) or None


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _meta(case: EvaluationData, key: str) -> Any:
    return (case.metadata or {}).get(key)


def _tool_turn(case: EvaluationData) -> str | None:
    call = _meta(case, "tool_call")
    return _call_line(call) if isinstance(call, dict) else None


def _invoked_skill(case: EvaluationData) -> str | None:
    skill = _meta(case, "skill")
    return str(skill.get("name") or "") or None if isinstance(skill, dict) else None


def _skill_content(case: EvaluationData) -> str | None:
    skill = _meta(case, "skill")
    return _text(skill.get("body")) if isinstance(skill, dict) else None


def _available_skills(case: EvaluationData) -> str | None:
    offered = _meta(case, "available_skills")
    if not isinstance(offered, list):
        return None
    return "\n".join(f"- {s.get('name')}" + (f": {s['description']}" if s.get("description")
                                              else "") for s in offered) or None


RENDERED: dict[str, Callable[[EvaluationData], str | None]] = {
    "tool_turn": _tool_turn,
    "invoked_skill": _invoked_skill,
    "skill_content": _skill_content,
    "available_skills": _available_skills,
    "context": _context,
    "available_tools": _available_tools,
    "actual_tool_trajectory": _actual_tool_trajectory,
    "assistant_turn": lambda case: _text(case.actual_output),
    "user_message": lambda case: _text(case.input),
    "expected_response": lambda case: _text(case.expected_output),
    "expected_output": lambda case: _text(case.expected_output),
    "assertions": _assertions,
    "expected_tool_trajectory": _expected_trajectory,
    "expected_trajectory": _expected_trajectory,
}


def render_placeholders(instructions: str, case: EvaluationData) -> str:
    """``instructions`` with every ``{name}`` replaced by the case's value (single pass);
    absent values and names outside the catalogue read :data:`NOT_AVAILABLE`."""
    cache: dict[str, str] = {}

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in cache:
            render = RENDERED.get(name)
            rendered = render(case) if render else None
            cache[name] = _clip(rendered, MAX_VALUE_CHARS) if rendered else NOT_AVAILABLE
        return cache[name]

    return _PLACEHOLDER_RE.sub(value, instructions or "")
