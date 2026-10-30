"""The evaluation core shared by the backend engine and the Runtime worker (plan §B.2).

Pure library: it receives a spans provider, evaluator definitions, a judge-model factory
and (optionally) a Lambda client, and returns result records — the same code path runs
in ``evaluation/strands_engine.py`` (SCORE NOW, in-process dev) and inside the worker zip
(``launchpad_eval``). Nothing here touches SQLite or builds an AWS client.

Per session: ``provider.get_evaluation_data(sid)`` → ``EvaluationData`` (dataset ground
truth folded in) → every evaluator with a throttling retry → one record per
``EvaluationOutput`` (TOOL_CALL evaluators yield one per tool call), a judge exception
becoming an error record instead of a crashed run. N-SCN-bug2: a judge that hits its output
limit (``MaxTokensReachedException`` — GLM / Kimi reason inside ``max_tokens``) is re-run on
a fresh judge with the budget doubled, up to ``EvaluatorSet.budget_cap`` and at most twice
(``model_calls.call_with_budget``); the error record of a final failure names the budget.
N12-obs53 (live 2026-10-16 09:44Z): a judge call that dies on a transient UPSTREAM error — a
Bedrock ``internalServerException`` on ConverseStream surfaced as ``EventStreamError``, a
5xx, a read timeout (``runtime_invoke.is_upstream_transient``, the predicate the scenario
replay and the architect assistant share) — is retried up to ``JUDGE_TRANSIENT_ATTEMPTS``
times with the set's ``transient_backoff`` (5 s, 15 s) between attempts; when it still
fails the error record keeps the cause's class as ``error_type`` and names the attempts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import AgentInvocationSpan, Session, ToolExecutionSpan
from strands_evals.utils import is_throttling_error

from app.services.strands_eval import evaluators as evaluator_lib
from app.services.strands_eval import extraction_check, model_calls, runtime_invoke
from app.services.strands_eval.results import (
    SPAN_STATUS_ERROR,
    error_record,
    note_rescue,
    records_from_outputs,
)

log = logging.getLogger(__name__)
THROTTLE_DELAYS_S: tuple[float, ...] = (2.0, 4.0, 8.0)
TURN_ORDINAL_STRIDE = 1000  # record ordinals of turn k start at k * stride (N17-obs324)
JUDGE_TRANSIENT_ATTEMPTS = 3  # same budget as the scenario replay (TRANSIENT_SCENARIO_ATTEMPTS)
Sleep = Callable[[float], None]
# judge_factory(model_id, *, max_tokens=None, temperature=0.0) → strands Model; the budget
# retry passes ``max_tokens`` explicitly (the first call takes the factory's own default)
JudgeFactory = Callable[..., Any]


def ground_truth_from_scenarios(
    scenarios: list[dict[str, Any]], session_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """``{session_id: {scenario_id, input, expected_response, assertions,
    expected_trajectory, turns}}`` for the scenarios that carry any ground truth (positional
    pairing, like the baseline's ``sessionMetadata``). ``turns`` (N17-obs322) is the
    per-turn truth ``[{input, expected_response}]`` the per-turn TRACE cases read; the
    joined ``expected_response`` stays for the session case."""
    out: dict[str, dict[str, Any]] = {}
    for scenario, session_id in zip(scenarios, session_ids, strict=False):
        turns = scenario.get("turns") or []
        first = turns[0] if turns else {}
        raw_input = first.get("input") if isinstance(first, dict) else None
        if isinstance(raw_input, dict):
            raw_input = raw_input.get("content") or raw_input.get("prompt") or str(raw_input)
        expected = [str(t.get("expected_response")) for t in turns
                    if isinstance(t, dict) and t.get("expected_response")]
        per_turn = [{"input": _turn_input(t),
                     "expected_response": str(t["expected_response"])
                     if isinstance(t, dict) and t.get("expected_response") else None}
                    for t in turns]
        truth = {
            "scenario_id": str(scenario.get("scenario_id") or ""),
            "input": str(raw_input or scenario.get("input") or ""),
            "expected_response": "\n".join(expected) if expected else None,
            "assertions": [str(a) for a in scenario.get("assertions") or []],
            # N17-obs376: an explicit [] is "no tools expected"; absent / null stays None
            "expected_trajectory": ([str(t) for t in scenario["expected_trajectory"]]
                                    if isinstance(scenario.get("expected_trajectory"), list)
                                    else None),
            "turns": per_turn,
        }
        out[str(session_id)] = truth
    return out


def _turn_input(turn: Any) -> str:
    raw = turn.get("input") if isinstance(turn, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("content") or raw.get("prompt") or str(raw)
    return str(raw or "")


def _first_user_prompt(session: Any) -> str:
    try:
        for trace in session.traces:
            for span in trace.spans:
                prompt = getattr(span, "user_prompt", None)
                if prompt:
                    return str(prompt)
    except Exception:  # noqa: BLE001 — a malformed session is simply prompt-less
        pass
    return ""


def evaluation_data(
    session_id: str, task_output: Any, truth: dict[str, Any] | None
) -> EvaluationData:
    """One session's ``EvaluationData``: the dataset turn (or the session's own first
    prompt) as input, the agent's final answer as output, ground truth folded in."""
    truth = truth or {}
    session = task_output["trajectory"]
    assertions = truth.get("assertions") or []
    return EvaluationData(
        input=str(truth.get("input") or _first_user_prompt(session) or ""),
        actual_output=str(task_output.get("output") or ""),
        expected_output=truth.get("expected_response") or None,
        expected_assertion="\n".join(assertions) if assertions else None,
        expected_trajectory=(list(truth["expected_trajectory"])
                             if isinstance(truth.get("expected_trajectory"), list) else None),
        actual_trajectory=session,
        name=str(truth.get("scenario_id") or session_id),
    )


def _bare_span_id(span_id: Any) -> str | None:
    """A provider span id is trace-scoped (``<traceId>:<spanId>``, N17-obs315 MT2)."""
    return str(span_id).rsplit(":", 1)[-1] if span_id else None


def turn_cases(
    case: EvaluationData, truth: dict[str, Any] | None = None
) -> list[tuple[str | None, EvaluationData]]:
    """``[(trace_id, case_k)]`` — one TRACE case per turn, as Global scores them
    (N17-obs324): turn k's trajectory is ``traces[:k+1]`` (so a builtin's last-turn read is
    turn k and its history the earlier turns), its input / output are turn k's own prompt
    and answer, its expected response turn k's (``truth["turns"]``, N17-obs322). The
    scenario-wide assertions stay with the session case — and with the final turn, the
    only turn of a single-turn scenario, so nothing a single-turn run judged changes.
    ``metadata["judged_turn"]`` tells a custom judge's ``{context}`` to stop before the
    turn's answer. A case without a strands ``Session`` is its own single unit."""
    session = case.actual_trajectory
    if not isinstance(session, Session):
        return [(None, case)]
    turns = list((truth or {}).get("turns") or [])
    indexed = [(k, next((s for s in t.spans if isinstance(s, AgentInvocationSpan)), None))
               for k, t in enumerate(session.traces)]
    indexed = [(k, agent) for k, agent in indexed if agent is not None]
    if not indexed:
        first = next((t.trace_id for t in session.traces if t.trace_id), None)
        return [(first, case)]
    last = indexed[-1][0]
    out: list[tuple[str | None, EvaluationData]] = []
    for n, (k, agent) in enumerate(indexed):
        expected = turns[n].get("expected_response") if n < len(turns) else None
        out.append((session.traces[k].trace_id, EvaluationData(
            input=str(agent.user_prompt or ""), actual_output=str(agent.agent_response or ""),
            expected_output=expected or None,
            expected_assertion=case.expected_assertion if k == last else None,
            actual_trajectory=Session(session_id=session.session_id,
                                      traces=session.traces[:k + 1]),
            name=case.name,
            metadata={"turn": n, "judged_turn": True,
                      "span_status": (agent.metadata or {}).get("span_status")},
        )))
    return out


class SpanStatusError(RuntimeError):
    """A turn whose ``invoke_agent`` span ended with status ERROR (N17-obs373): Global
    answers ``SpanStatusErrorException`` and scores nothing — so does this engine (an error
    record, ``error_type`` ``span_status_error``, never a judge call). ``failed`` (the
    failed turns' trace ids) makes it the SESSION form (N17-obs384): a session with any
    failed turn is not scored by a SESSION evaluator either."""

    error_type = SPAN_STATUS_ERROR

    def __init__(self, trace_id: str | None, failed: list[str | None] | None = None) -> None:
        if failed is not None:
            turns = ", ".join(str(t or "?") for t in failed)
            super().__init__(f"session not scored: {len(failed)} of its turns failed (agent "
                             f"span status ERROR — timeout, model or tool error): {turns}")
        else:
            super().__init__(f"turn {trace_id or '?'} not scored: its agent span ended with "
                             "status ERROR (the turn failed — timeout, model or tool error)")


def failed_turn(agent: Any) -> bool:
    return str(((getattr(agent, "metadata", None) or {}).get("span_status")) or "") == "ERROR"


def tool_units(case: EvaluationData) -> list[dict[str, Any]]:
    """``[{trace_id, span_id, tool_name}]`` per tool call in the order the TOOL_CALL
    builtins judge them (``TraceExtractor`` TOOL_LEVEL) — the row stamps (N17-obs324)."""
    session = case.actual_trajectory
    if not isinstance(session, Session):
        return []
    units: list[dict[str, Any]] = []
    for trace in session.traces:
        for span in trace.spans:
            if isinstance(span, ToolExecutionSpan):
                units.append({"trace_id": trace.trace_id,
                              "span_id": _bare_span_id(span.span_info.span_id),
                              "tool_name": span.tool_call.name})
    return units


SKILL_PLACEHOLDERS = ("invoked_skill", "skill_content")


def tool_cases(case: EvaluationData, turns: list[tuple[str | None, EvaluationData]],
               ) -> list[tuple[dict[str, Any], EvaluationData]]:
    """``[(unit, case_j)]`` — one case per tool call, for a custom TOOL_CALL judge
    (N17-obs332), in the TOOL_CALL builtins' order (:func:`tool_units`). Call j of turn k
    reads as Global's tool-level placeholders do: ``{context}`` = the earlier turns in full
    plus turn k's prompt and the calls BEFORE j (trajectory ``traces[:k]`` + turn k with its
    earlier calls only, ``metadata["judged_turn"]`` hides the answer); ``{tool_turn}`` =
    call j (``metadata["tool_call"]``); the skill placeholders from the skill load this call
    made, if it was one (``metadata["skill"]``) and the skills the agent was offered
    (``metadata["available_skills"]``); ``{user_message}`` = turn k's prompt."""
    session = case.actual_trajectory
    if not isinstance(session, Session):
        return []
    from strands_evals.extractors.skills import extract_skill_load_events

    from .skill_catalog import available_skills

    loads: dict[str, list[Any]] = {}
    for event in extract_skill_load_events(session):
        if event.call_id:
            loads.setdefault(str(event.call_id), []).append(event)
    offered = [{"name": s.name, "description": s.description}
               for s in available_skills(session)]  # + the Deep Agents list
    turn_case = {trace_id: c for trace_id, c in turns}
    out: list[tuple[dict[str, Any], EvaluationData]] = []
    for k, trace in enumerate(session.traces):
        tools = [s for s in trace.spans if isinstance(s, ToolExecutionSpan)]
        others = [s for s in trace.spans if not isinstance(s, ToolExecutionSpan)]
        own = turn_case.get(trace.trace_id)
        for j, span in enumerate(tools):
            call = span.tool_call
            skill = None
            queue = loads.get(str(call.tool_call_id or ""))
            if queue:
                event = queue.pop(0)
                skill = {"name": event.name, "body": event.body, "status": event.status}
            cut = trace.model_copy(update={"spans": [*others, *tools[:j]]})
            unit = {"trace_id": trace.trace_id,
                    "span_id": _bare_span_id(span.span_info.span_id),
                    "tool_name": call.name, "skill": skill is not None}
            out.append((unit, EvaluationData(
                input=str(own.input if own is not None else case.input or ""),
                actual_output=str(own.actual_output if own is not None else ""),
                actual_trajectory=Session(session_id=session.session_id,
                                          traces=[*session.traces[:k], cut]),
                name=case.name,
                metadata={"judged_turn": True,
                          "tool_call": {"name": call.name, "input": call.arguments,
                                        "tool_result": span.tool_result.content
                                        if span.tool_result else None},
                          "skill": skill, "available_skills": offered,
                          "span_status": (getattr(own, "metadata", None) or {}).get(
                              "span_status") if own is not None else None},
            )))
    return out


SKILL_EVALUATORS = ("Builtin.SkillSelectionAccuracy", "Builtin.SkillInstructionFollowing")


def skill_loads(session: Any) -> list[Any]:
    """The session's skill loads (strands-evals' ``extract_skill_load_events``) — the units
    the skill builtins judge, one row each (:func:`skill_cases`), and the count the worker
    reports as their expected units (N17-obs435)."""
    if not isinstance(session, Session):
        return []
    from strands_evals.extractors.skills import extract_skill_load_events

    return list(extract_skill_load_events(session))


def is_skill_evaluator(definition: dict[str, Any] | None) -> bool:
    d = definition or {}
    return (str(d.get("id") or "") in SKILL_EVALUATORS
            or str(d.get("base_evaluator_id") or "") in SKILL_EVALUATORS)


def skill_cases(case: EvaluationData, turns: list[tuple[str | None, EvaluationData]],
                ) -> list[tuple[dict[str, Any], EvaluationData]]:
    """``[(unit, case_j)]`` — one case per skill-LOAD call, for the skill builtins
    (N17-obs402): Global anchors one result to each loading span; strands-evals folds every
    load of a skill into one row judged against the whole session. Load j (strands-evals'
    ``extract_skill_load_events``, paired with its ``execute_tool`` span by ``toolUseId``,
    in order — ids repeat) in turn k reads turn k as its task (N17-obs342: ``input`` = the
    TRIGGERING turn's prompt, not the scenario's first), ``traces[:k+1]`` as its trajectory
    and names its load in ``metadata["skill_load"]``. A load whose call is not found keeps
    the last turn."""
    session = case.actual_trajectory
    if not isinstance(session, Session):
        return []
    spans: dict[str, list[tuple[int, Any]]] = {}
    for k, trace in enumerate(session.traces):
        for span in trace.spans:
            if isinstance(span, ToolExecutionSpan) and span.tool_call.tool_call_id:
                spans.setdefault(str(span.tool_call.tool_call_id), []).append((k, span))
    turn_case = {trace_id: c for trace_id, c in turns}
    out: list[tuple[dict[str, Any], EvaluationData]] = []
    for event in skill_loads(session):
        queue = spans.get(str(event.call_id or ""))
        k, span = queue.pop(0) if queue else (len(session.traces) - 1, None)
        trace = session.traces[k] if session.traces else None
        own = turn_case.get(trace.trace_id) if trace is not None else None
        unit = {"trace_id": trace.trace_id if trace is not None else None,
                "span_id": _bare_span_id(span.span_info.span_id) if span is not None else None,
                "tool_name": span.tool_call.name if span is not None else None}
        out.append((unit, EvaluationData(
            input=str(own.input if own is not None else case.input or ""),
            actual_output=str(own.actual_output if own is not None else case.actual_output
                              or ""),
            expected_output=case.expected_output,
            actual_trajectory=Session(session_id=session.session_id,
                                      traces=session.traces[:k + 1]),
            name=case.name,
            metadata={"skill_load": {"name": event.name, "body": event.body,
                                     "status": event.status, "error": event.error},
                      "span_status": (getattr(own, "metadata", None) or {}).get(
                          "span_status") if own is not None else None},
        )))
    return out


def scoring_units(
    level: str, case: EvaluationData, turns: list[tuple[str | None, EvaluationData]],
    first_trace: str | None, statuses: list[str] | None = None,
    session_status: str | None = None, definition: dict[str, Any] | None = None,
) -> list[tuple[EvaluationData, dict[str, Any]]]:
    """``[(case, stamp)]`` an evaluator of ``level`` runs on: one per turn for TRACE, the
    session case otherwise. ``stamp`` = ``{trace_id, ordinal, units, extraction_status,
    error}`` for the records: ``extraction_status`` is the reconciliation of the unit's turn
    (``statuses[n]``; a SESSION row takes the session's), ``error`` a reason not to judge
    the unit at all (a failed turn, N17-obs373)."""
    statuses = list(statuses or [])

    def status_of(n: int) -> str | None:
        return statuses[n] if n < len(statuses) else None

    if level == "TRACE":
        # turn 0 keeps ordinal 0 (a single-turn row's id is unchanged); later turns are
        # spaced apart so a judge yielding several outputs per turn never collides
        return [(turn_case, {
            "trace_id": trace_id, "ordinal": n * TURN_ORDINAL_STRIDE, "units": None,
            "extraction_status": status_of(n),
            "error": SpanStatusError(trace_id)
            if (getattr(turn_case, "metadata", None) or {}).get("span_status") == "ERROR"
            else None})
                for n, (trace_id, turn_case) in enumerate(turns)]
    if level == "TOOL_CALL" and is_skill_evaluator(definition):
        # N17-obs402: one result per skill-load call, stamped with that call
        loads = skill_cases(case, turns)
        if loads:
            turn_of = {trace_id: n for n, (trace_id, _c) in enumerate(turns)}
            return [(load_case, {
                "trace_id": unit["trace_id"], "ordinal": index,
                "units": [{**unit, "extraction_status":
                           status_of(turn_of.get(unit["trace_id"], -1))}],
                "extraction_status": status_of(turn_of.get(unit["trace_id"], -1)),
                "error": SpanStatusError(unit["trace_id"])
                if (load_case.metadata or {}).get("span_status") == "ERROR" else None})
                for index, (unit, load_case) in enumerate(loads)]
        # no load at all: the evaluator's own not-applicable row, once per session
        return [(case, {"trace_id": first_trace, "ordinal": 0, "units": None,
                        "extraction_status": session_status, "error": None})]
    if level == "TOOL_CALL" and (definition or {}).get("definition") == "judge":
        # N17-obs332: a custom TOOL_CALL judge runs once per call; one that reads the skill
        # placeholders judges skill calls only (Global's rule)
        turn_of = {trace_id: n for n, (trace_id, _c) in enumerate(turns)}
        instructions = str((definition or {}).get("instructions") or "")
        skills_only = any("{" + name + "}" in instructions for name in SKILL_PLACEHOLDERS)
        units_out: list[tuple[EvaluationData, dict[str, Any]]] = []
        for index, (unit, call_case) in enumerate(tool_cases(case, turns)):
            if skills_only and not unit["skill"]:
                continue
            failed = (call_case.metadata or {}).get("span_status") == "ERROR"
            stamp_unit = {**unit, "extraction_status":
                          status_of(turn_of.get(unit["trace_id"], -1))}
            units_out.append((call_case, {
                "trace_id": unit["trace_id"], "ordinal": index, "units": [stamp_unit],
                "extraction_status": stamp_unit["extraction_status"],
                "error": SpanStatusError(unit["trace_id"]) if failed else None}))
        return units_out
    if level == "TOOL_CALL":
        turn_of = {trace_id: n for n, (trace_id, _c) in enumerate(turns)}
        # N17-obs383: a call inside a failed turn is not scored (Global answers
        # SpanStatusErrorException for it) — its unit carries the error, the others are judged
        failed = {trace_id for trace_id, turn_case in turns
                  if (getattr(turn_case, "metadata", None) or {}).get("span_status") == "ERROR"}
        units = [{**unit, "extraction_status": status_of(turn_of.get(unit["trace_id"], -1)),
                  "error": SpanStatusError(unit["trace_id"]) if unit["trace_id"] in failed
                  else None}
                 for unit in tool_units(case)]
        return [(case, {"trace_id": first_trace, "ordinal": 0, "units": units,
                        "extraction_status": session_status,
                        "error": units[0]["error"] if units and all(
                            u["error"] is not None for u in units) else None})]
    # N17-obs384: a SESSION evaluator scores the conversation as a whole — with any failed
    # turn in it, Global answers SpanStatusErrorException for the session; so does this
    failed = [trace_id for trace_id, turn_case in turns
              if (getattr(turn_case, "metadata", None) or {}).get("span_status") == "ERROR"]
    return [(case, {"trace_id": first_trace, "ordinal": 0, "units": None,
                    "extraction_status": session_status,
                    "error": SpanStatusError(first_trace, failed) if failed else None})]


def unit_records(
    evaluate: Callable[[EvaluationData], list[EvaluationOutput]], unit_case: EvaluationData,
    stamp: dict[str, Any], *, job_id: str, session_id: str, evaluator_id: str, level: str,
) -> list[dict[str, Any]]:
    """The records of one scoring unit (never raises) — shared by the batch engine and the
    online job. A unit whose ``stamp["error"]`` is set is never judged (a failed turn,
    N17-obs373): one error row, or — for a TOOL_CALL unit — one per call. Per-call errors
    (``units[i]["error"]``, a call inside a failed turn, N17-obs383) replace those calls'
    rows; the judge runs only when some call is left to judge."""
    units = stamp.get("units") or []

    def errors_for_calls() -> list[dict[str, Any]]:
        return [error_record(job_id=job_id, session_id=session_id, evaluator_id=evaluator_id,
                             level=level, error=unit["error"], trace_id=unit.get("trace_id"),
                             ordinal=stamp["ordinal"] + index,
                             extraction_status=unit.get("extraction_status"),
                             span_id=unit.get("span_id"), tool_name=unit.get("tool_name"))
                for index, unit in enumerate(units)]

    if stamp.get("error") is not None:
        if units and all(unit.get("error") is not None for unit in units):
            return errors_for_calls()
        return [error_record(job_id=job_id, session_id=session_id, evaluator_id=evaluator_id,
                             level=level, error=stamp["error"], trace_id=stamp["trace_id"],
                             ordinal=stamp["ordinal"],
                             extraction_status=stamp["extraction_status"])]
    try:
        outputs = evaluate(unit_case)
    except Exception as exc:  # noqa: BLE001 — the judgement failed, not the run
        call = units[0] if len(units) == 1 else {}  # a per-call unit names its call
        return [error_record(job_id=job_id, session_id=session_id, evaluator_id=evaluator_id,
                             level=level, error=exc, trace_id=stamp["trace_id"],
                             ordinal=stamp["ordinal"],
                             extraction_status=stamp["extraction_status"],
                             span_id=call.get("span_id"), tool_name=call.get("tool_name"))]
    # N17-obs385: a judgement the budget rescue produced says how on its records
    records = note_rescue(records_from_outputs(
        job_id=job_id, session_id=session_id, evaluator_id=evaluator_id, level=level,
        outputs=outputs, trace_id=stamp["trace_id"], ordinal_start=stamp["ordinal"],
        units=units or None, extraction_status=stamp["extraction_status"]), outputs)
    if units and len(units) == len(records) and any(u.get("error") for u in units):
        failed = errors_for_calls()
        records = [failed[i] if unit.get("error") is not None else rec
                   for i, (unit, rec) in enumerate(zip(units, records, strict=True))]
    return records


class UpstreamRetryAbandoned(RuntimeError):
    """The job was stopped while a judge call waited out its transient backoff (N13-obs8):
    the wait is sliced (``runtime_invoke.sliced_wait``) and polls ``stop()``, so the retry is
    abandoned within about a second of the cancel. Like :class:`UpstreamTransientExhausted`
    the error record keeps the LAST cause's class as ``error_type``; the wording avoids the
    replay failure's ``failed after k/N attempts:`` form (the hub reads it as
    ``evaluator_error``)."""

    def __init__(self, cause: BaseException, attempts: int, retried: list[dict[str, Any]],
                 label: str = "judge", backoff_s: float = 0.0) -> None:
        self.error_type = type(cause).__name__
        self.attempts = int(attempts)
        self.retried = list(retried)
        self.__cause__ = cause
        super().__init__(
            f"{label} retry abandoned — the job was stopped during the {float(backoff_s):g} s "
            f"wait before attempt {self.attempts + 1}/{JUDGE_TRANSIENT_ATTEMPTS} (transient "
            f"upstream): {runtime_invoke.retry_reason(cause, 600)}"
        )


class UpstreamTransientExhausted(RuntimeError):
    """Every ``JUDGE_TRANSIENT_ATTEMPTS`` judge attempts died on a transient upstream error
    (N12-obs53). ``error_type`` is the LAST cause's class (what the error record shows as
    its type — ``EventStreamError``, not this wrapper), ``attempts`` the number made and
    ``retried`` each retry's ``{attempt, reason, backoff_s}``. The wording deliberately
    avoids the replay failure's ``failed after k/N attempts:`` form, which the hub reads as
    an unanswered scenario (``service._failure_kind``)."""

    def __init__(self, cause: BaseException, attempts: int, retried: list[dict[str, Any]],
                 label: str = "judge") -> None:
        self.error_type = type(cause).__name__
        self.attempts = int(attempts)
        self.retried = list(retried)
        self.__cause__ = cause
        waited = ", ".join(f"{float(r['backoff_s']):g} s" for r in self.retried
                           if float(r.get("backoff_s") or 0) > 0)
        super().__init__(
            f"{label} errored on every one of {self.attempts} attempts (transient upstream"
            f"{'; waited ' + waited if waited else ''}): {runtime_invoke.retry_reason(cause, 600)}"
        )


def evaluate_with_retry(
    evaluator: Any, case: EvaluationData, *, sleep: Sleep,
    delays: tuple[float, ...] = THROTTLE_DELAYS_S,
    transient_delays: tuple[float, ...] = runtime_invoke.DEFAULT_TRANSIENT_BACKOFF_S,
    label: str = "judge", stop: Callable[[], bool] | None = None,
) -> list[EvaluationOutput]:
    """``evaluator.evaluate(case)`` with a bounded back-off on judge throttling (unchanged:
    ``delays``, then one last try) and — N12-obs53 — on a transient UPSTREAM error
    (``runtime_invoke.is_upstream_transient``: a 5xx / ``internalServerException`` surfaced
    as ``EventStreamError``, a read timeout) up to ``JUDGE_TRANSIENT_ATTEMPTS`` attempts,
    waiting ``transient_delays[k]`` before retry k — in ≤ 1 s slices that poll ``stop()``
    (N13-obs8: the job's cancel marker), so a stop mid-wait raises
    :class:`UpstreamRetryAbandoned` within about a second (→ an error record typed as the
    cause). A third kind of failure propagates at once (→ an error record); exhausting the
    transient budget raises :class:`UpstreamTransientExhausted` (→ an error record typed as
    the cause)."""
    throttles = 0
    transient_attempts = 0
    retried: list[dict[str, Any]] = []
    while True:
        try:
            return list(evaluator.evaluate(case))
        except Exception as exc:  # noqa: BLE001 — classified below
            if is_throttling_error(exc):
                if throttles >= len(delays):
                    raise
                sleep(delays[throttles])
                throttles += 1
                continue
            if not runtime_invoke.is_upstream_transient(exc):
                raise
            transient_attempts += 1
            if transient_attempts >= JUDGE_TRANSIENT_ATTEMPTS:
                log.warning("%s attempt %d/%d failed: %s — giving up", label,
                            transient_attempts, JUDGE_TRANSIENT_ATTEMPTS,
                            runtime_invoke.retry_reason(exc))
                raise UpstreamTransientExhausted(exc, transient_attempts, retried, label) from exc
            backoff = runtime_invoke.backoff_for(transient_delays, transient_attempts - 1)
            log.warning("%s attempt %d/%d failed: %s — retrying in %g s", label,
                        transient_attempts, JUDGE_TRANSIENT_ATTEMPTS,
                        runtime_invoke.retry_reason(exc), backoff)
            retried.append({"attempt": transient_attempts,
                            "reason": runtime_invoke.retry_reason(exc), "backoff_s": backoff})
            if backoff > 0 and not runtime_invoke.sliced_wait(backoff, sleep, stop=stop):
                log.warning("%s retry abandoned: the job was stopped during the %g s wait",
                            label, backoff)
                raise UpstreamRetryAbandoned(exc, transient_attempts, retried, label,
                                             backoff) from exc


class EvaluatorSet:
    """Evaluator definitions built once per job: the judge model is created lazily per
    model id, trajectory evaluators take none, code evaluators need the Lambda client."""

    def __init__(
        self, definitions: list[dict[str, Any]], judge_factory: JudgeFactory, *,
        lambda_client: Any = None, default_model_id: str | None = None,
        code_evaluators_enabled: bool = True,
        budget_cap: int = model_calls.DEFAULT_BUDGET_CAP, default_budget: int | None = None,
        transient_backoff: tuple[float, ...] = runtime_invoke.DEFAULT_TRANSIENT_BACKOFF_S,
        fallback_model_id: str | None = None,
    ) -> None:
        self.definitions = list(definitions)
        # N17-obs385: the ``judge_fallback`` model role a judge that ran out of output
        # budget is re-tried on ("" / None = none)
        self.fallback_model_id = fallback_model_id or None
        # N12-obs53: the waits before the 2nd / 3rd judge attempt on a transient upstream error
        self.transient_backoff = tuple(float(x) for x in transient_backoff)
        self._judge_factory = judge_factory
        self._judges: dict[str | None, Any] = {}
        self._lambda = lambda_client
        self._default_model_id = default_model_id
        self._code_enabled = code_evaluators_enabled
        self._built: dict[str, Any] = {}
        # N-SCN-bug2: the doubling retries stop here; ``default_budget`` is what the first
        # call is assumed to run at when the judge object does not expose its max_tokens
        self.budget_cap = int(budget_cap)
        self._default_budget = int(default_budget) if default_budget else None

    def _judge(self, model_id: str | None) -> Any:
        key = model_id or self._default_model_id
        if key not in self._judges:
            self._judges[key] = self._judge_factory(key)
        return self._judges[key]

    def model_id_of(self, definition: dict[str, Any]) -> str | None:
        return definition.get("model_id") or self._default_model_id

    def needs_judge(self, definition: dict[str, Any]) -> bool:
        """Trajectory and code evaluators never call a model."""
        evaluator_id = str(definition.get("id") or "")
        return evaluator_id not in evaluator_lib.TRAJECTORY_LEVELS and (
            definition.get("definition") != "code")

    def budget_of(self, definition: dict[str, Any]) -> int:
        """The output budget the cached judge of ``definition`` runs at (its ``max_tokens``,
        else the set's default, else the historical 1024)."""
        judge = self._judges.get(self.model_id_of(definition))
        known = model_calls.model_max_tokens(judge) if judge is not None else None
        return known or self._default_budget or model_calls.DEFAULT_BUDGET

    def evaluator_with_budget(self, definition: dict[str, Any], max_tokens: int) -> Any:
        """An UNCACHED twin of ``evaluator(definition)`` on a fresh judge built with
        ``max_tokens`` — the N-SCN-bug2 retry; the cached judge keeps its configured budget
        for the other threads of the pool."""
        if not self.needs_judge(definition):
            return self.evaluator(definition)
        judge = self._judge_factory(self.model_id_of(definition), max_tokens=int(max_tokens))
        return evaluator_lib.build(definition, judge)

    def evaluator_on(self, definition: dict[str, Any], model_id: str | None,
                     max_tokens: int) -> Any:
        """An uncached evaluator of ``definition`` on a fresh ``model_id`` judge at
        ``max_tokens`` — the N17-obs385 rescue (truncated context / fallback model)."""
        judge = self._judge_factory(model_id or self.model_id_of(definition),
                                    max_tokens=int(max_tokens))
        return evaluator_lib.build(definition, judge)

    def evaluator(self, definition: dict[str, Any]) -> Any:
        evaluator_id = str(definition.get("id") or "")
        if evaluator_id in self._built:
            return self._built[evaluator_id]
        kind = definition.get("definition")
        if evaluator_id in evaluator_lib.TRAJECTORY_LEVELS:
            built = evaluator_lib.build(definition, None)
        elif kind == "code":
            if not self._code_enabled:
                raise RuntimeError(
                    "code evaluators are disabled in this workspace "
                    "(LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED)"
                )
            built = evaluator_lib.build(definition, None, lambda_client=self._lambda)
        else:
            built = evaluator_lib.build(definition, self._judge(definition.get("model_id")))
        self._built[evaluator_id] = built
        return built


def evaluate_with_budget(
    evaluators: EvaluatorSet, definition: dict[str, Any], case: EvaluationData, *,
    sleep: Sleep, delays: tuple[float, ...] = THROTTLE_DELAYS_S,
    stop: Callable[[], bool] | None = None,
) -> list[EvaluationOutput]:
    """``evaluate_with_retry`` on the set's evaluator; when the judge hits its output limit
    (N-SCN-bug2) the case is re-judged on a fresh evaluator with the budget doubled, up to
    ``evaluators.budget_cap`` and at most twice; the final failure is
    ``model_calls.JudgeBudgetExceeded`` (→ an error record naming the budget). ``stop`` is
    the job's cancel predicate the transient backoff polls (N13-obs8)."""
    evaluator = evaluators.evaluator(definition)
    evaluator_id = str(definition.get("id") or "")
    label = f"judge {evaluator_id}"
    if not evaluators.needs_judge(definition):
        return evaluate_with_retry(evaluator, case, sleep=sleep, delays=delays,
                                   transient_delays=evaluators.transient_backoff,
                                   label=f"evaluator {evaluator_id}", stop=stop)
    budget = evaluators.budget_of(definition)

    def call(max_tokens: int) -> list[EvaluationOutput]:
        chosen = (evaluator if max_tokens == budget
                  else evaluators.evaluator_with_budget(definition, max_tokens))
        return evaluate_with_retry(chosen, case, sleep=sleep, delays=delays,
                                   transient_delays=evaluators.transient_backoff, label=label,
                                   stop=stop)

    model_id = evaluators.model_id_of(definition)
    try:
        return model_calls.call_with_budget(
            call, budget=budget, cap=evaluators.budget_cap, label=f"judge {evaluator_id}",
            model_id=model_id,
        )
    except model_calls.JudgeBudgetExceeded as exc:

        def judge_at(model: str | None, max_tokens: int, smaller: EvaluationData,
                     ) -> list[EvaluationOutput]:
            return evaluate_with_retry(
                evaluators.evaluator_on(definition, model, max_tokens), smaller, sleep=sleep,
                delays=delays, transient_delays=evaluators.transient_backoff, label=label,
                stop=stop)

        return rescue_judgement(exc, case=case, judge_at=judge_at, model_id=model_id,
                                fallback_model_id=evaluators.fallback_model_id,
                                cap=evaluators.budget_cap)


class JudgedOutputs(list):
    """Outputs of a judgement the N17-obs385 rescue produced; ``rescue`` says how
    (``{reason, budget, context_truncated, model_id, fallback_from}``)."""

    def __init__(self, outputs: list[EvaluationOutput], rescue: dict[str, Any]) -> None:
        super().__init__(outputs)
        self.rescue = dict(rescue)


def rescue_judgement(
    exc: model_calls.JudgeBudgetExceeded, *, case: EvaluationData,
    judge_at: Callable[[str | None, int, EvaluationData], list[EvaluationOutput]],
    model_id: str | None, fallback_model_id: str | None, cap: int,
) -> JudgedOutputs:
    """N17-obs385: after the doubling retries ran out (``exc``) — (1) one re-judge on the
    case with overlong text cut with a visible marker (``evaluators.truncate_case``) at the
    cap; (2) the ``judge_fallback`` model on that case. ``judge_at(model, max_tokens, case)``
    makes one judgement. A non-budget error propagates; when every step runs out of budget
    too, ``JudgeBudgetExceeded`` names all of them (→ the error record)."""
    smaller, truncated = evaluator_lib.truncate_case(case)
    tried = [str(exc)]
    steps: list[tuple[str | None, str | None]] = []
    if truncated:
        steps.append((model_id, None))
    else:
        tried.append("the context had nothing to truncate")
    if fallback_model_id and fallback_model_id != model_id:
        steps.append((fallback_model_id, model_id))
    for model, fallback_from in steps:
        try:
            outputs = judge_at(model, int(cap), smaller)
        except Exception as error:  # noqa: BLE001 — only the output limit is rescued
            if not model_calls.is_max_tokens_error(error):
                raise
            tried.append(f"{model} on the truncated context ran out too" if truncated
                         else f"{model} ran out too")
            continue
        return JudgedOutputs(outputs, {
            "reason": type(exc).__name__, "budget": exc.budget, "context_truncated": truncated,
            "model_id": model, "fallback_from": fallback_from,
        })
    if not (fallback_model_id and fallback_model_id != model_id):
        tried.append("no judge_fallback model is configured")
    failed = model_calls.JudgeBudgetExceeded(exc.label, exc.budget, exc.attempts, model_id)
    failed.args = ("; ".join(tried),)
    raise failed from exc


CheckSink = Callable[[str, dict[str, Any]], None]


ATTEMPT_CLOCK_MARGIN_MS = 1000  # the worker's clock vs the runtime's span clock


def since_attempt(task_output: Any, not_before_ms: int | None) -> Any:
    """``task_output`` with only the traces that started at or after ``not_before_ms``
    (N17-obs323): a scenario retried on a transient error reuses its pre-assigned session
    id, so the spans hold every attempt — only the answered (last) attempt is scored. The
    output is the last kept turn's answer; with nothing left the data is unchanged."""
    if not not_before_ms:
        return task_output
    from datetime import UTC, datetime

    cutoff = datetime.fromtimestamp((not_before_ms - ATTEMPT_CLOCK_MARGIN_MS) / 1000, UTC)
    session = task_output["trajectory"]

    def started(trace: Any) -> datetime:
        times = [s.span_info.start_time for s in trace.spans]
        times = [t if t.tzinfo else t.replace(tzinfo=UTC) for t in times]
        return min(times) if times else datetime.max.replace(tzinfo=UTC)

    kept = [t for t in session.traces if started(t) >= cutoff]
    if not kept or len(kept) == len(session.traces):
        return task_output
    trimmed = session.model_copy(update={"traces": kept})
    agents = [s for t in kept for s in t.spans if isinstance(s, AgentInvocationSpan)]
    output = agents[-1].agent_response if agents else task_output.get("output")
    return {**task_output, "trajectory": trimmed, "output": output}


def evaluate_session(
    provider: Any, session_id: str, evaluators: EvaluatorSet, truth: dict[str, Any] | None,
    *, job_id: str, sleep: Sleep, stop: Callable[[], bool] | None = None,
    reference: dict[str, Any] | None = None, on_check: CheckSink | None = None,
    not_before_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Every evaluator of one session → result records (never raises). Eval accuracy item 1:
    once the session's data is extracted, ``on_check(session_id, extraction_check)`` gets
    the reconciliation of its per-turn answers against ``reference`` (``unverifiable``
    without one) — before any judge runs, so even a stopped session is checked."""
    records: list[dict[str, Any]] = []
    try:
        task_output = since_attempt(provider.get_evaluation_data(session_id), not_before_ms)
        case = evaluation_data(session_id, task_output, truth)
    except Exception as exc:  # noqa: BLE001 — a missing session fails every evaluator of it
        return [
            error_record(job_id=job_id, session_id=session_id,
                         evaluator_id=str(d.get("id")), level=evaluator_lib.level_of(d), error=exc)
            for d in evaluators.definitions
        ]
    if reference is None and hasattr(provider, "origin_reference"):
        # N17-obs316: no replay / chat-ledger answer — the session's own runtime records
        reference = provider.origin_reference(session_id)
    check = extraction_check.session_check(task_output, reference)
    if on_check is not None:
        on_check(session_id, check)
    statuses = extraction_check.turn_statuses(
        extraction_check.extracted_turns(task_output), reference)
    trace_id = None
    try:
        trace_id = next((t.trace_id for t in case.actual_trajectory.traces if t.trace_id), None)
    except Exception:  # noqa: BLE001
        trace_id = None
    turns = turn_cases(case, truth)
    for definition in evaluators.definitions:
        if stop and stop():
            break
        evaluator_id = str(definition.get("id"))
        level = evaluator_lib.level_of(definition)
        for unit_case, stamp in scoring_units(level, case, turns, trace_id, statuses,
                                              check.get("status"), definition):
            if stop and stop():
                break
            records.extend(unit_records(
                lambda c, d=definition: evaluate_with_budget(evaluators, d, c, sleep=sleep,
                                                             stop=stop),
                unit_case, stamp, job_id=job_id, session_id=session_id,
                evaluator_id=evaluator_id, level=level))
    return records


def evaluate_sessions(
    provider: Any,
    session_ids: list[str],
    evaluators: EvaluatorSet,
    ground_truth: dict[str, dict[str, Any]] | None,
    *,
    job_id: str,
    sleep: Sleep,
    stop: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    on_records: Callable[[str, list[dict[str, Any]]], None] | None = None,
    concurrency: int = 4,
    references: dict[str, dict[str, Any]] | None = None,
    on_check: CheckSink | None = None,
    not_before: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """All sessions, ``concurrency`` at a time (judge calls are I/O-bound); ``stop()`` is
    polled before each session is started — sessions already judged keep their records;
    ``on_records(session_id, records)`` fires as each session finishes (incremental
    flush); the return value keeps the sessions' order. ``references`` /
    ``on_check``: the per-session extraction reconciliation (:func:`evaluate_session`)."""
    truth = ground_truth or {}
    refs = references or {}
    results: dict[str, list[dict[str, Any]]] = {}
    total = len(session_ids)
    done = 0

    def one(session_id: str) -> tuple[str, list[dict[str, Any]]]:
        return session_id, evaluate_session(provider, session_id, evaluators,
                                            truth.get(session_id), job_id=job_id, sleep=sleep,
                                            stop=stop, reference=refs.get(session_id),
                                            on_check=on_check,
                                            not_before_ms=(not_before or {}).get(session_id))

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        pending = []
        for session_id in session_ids:
            if stop and stop():
                break
            pending.append(pool.submit(one, session_id))
        for future in pending:
            session_id, records = future.result()
            results[session_id] = records
            done += 1
            if on_records:
                on_records(session_id, records)
            if progress:
                progress(f"evaluating {done}/{total}")
    return [rec for sid in session_ids if sid in results for rec in results[sid]]
