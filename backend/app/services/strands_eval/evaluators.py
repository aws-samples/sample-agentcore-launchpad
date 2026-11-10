"""Launchpad evaluator ids → ``strands_evals`` evaluators (plan §B.2 mapping table).

``build(definition, judge)`` is the one constructor the engine uses. ``judge`` is a
``strands.models.Model`` instance (never ``None`` or a ``str`` — strands would otherwise
build a default Claude model, risk R-3); trajectory evaluators are deterministic and take
no model; code evaluators invoke an injected Lambda client with Global's envelope
(:mod:`code_evaluators`, N17-obs327). Every builtin keeps its
Launchpad id as the evaluator ``name`` so ``EvaluationOutput``s map 1:1 to result rows.
A custom judge renders its instructions' placeholders from each case before the judge call
(:class:`CustomJudgeEvaluator`, N17-obs311). ``Builtin.Correctness`` judges against the
dataset's expected RESPONSE (:class:`ExpectedResponseCorrectnessEvaluator`, audit item 2).
"""

from __future__ import annotations

from typing import Any, Literal

from strands.models import Model
from strands_evals.evaluators import (
    CoherenceEvaluator,
    ConcisenessEvaluator,
    CorrectnessEvaluator,
    Evaluator,
    FaithfulnessEvaluator,
    GoalSuccessRateEvaluator,
    HarmfulnessEvaluator,
    HelpfulnessEvaluator,
    InstructionFollowingEvaluator,
    OutputEvaluator,
    RefusalEvaluator,
    ResponseRelevanceEvaluator,
    SkillInstructionFollowingEvaluator,
    SkillSelectionAccuracyEvaluator,
    StereotypingEvaluator,
    ToolParameterAccuracyEvaluator,
    ToolSelectionAccuracyEvaluator,
)
from strands_evals.extractors.tools_use_extractor import extract_agent_tools_used_from_trace
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import (
    AgentInvocationSpan,
    InferenceSpan,
    Session,
    TextContent,
    ToolExecutionSpan,
    TraceLevelInput,
)

from app.services.strands_eval import global_templates, judge_placeholders, skill_catalog
from app.services.strands_eval.context_relevance import ContextRelevanceEvaluator

# The contract catalog (the Global builtin ids and levels; `evaluators_local` re-exports it
# as ALL_BUILTIN_EVALUATORS / TRAJECTORY_EVALUATORS).
BUILTIN_LEVELS: dict[str, str] = {
    "Builtin.GoalSuccessRate": "SESSION",
    "Builtin.Helpfulness": "TRACE",
    "Builtin.Correctness": "TRACE",
    "Builtin.Faithfulness": "TRACE",
    "Builtin.ResponseRelevance": "TRACE",
    "Builtin.ContextRelevance": "TRACE",
    "Builtin.Conciseness": "TRACE",
    "Builtin.Coherence": "TRACE",
    "Builtin.InstructionFollowing": "TRACE",
    "Builtin.Refusal": "TRACE",
    "Builtin.Harmfulness": "TRACE",
    "Builtin.Stereotyping": "TRACE",
    "Builtin.ToolSelectionAccuracy": "TOOL_CALL",
    "Builtin.ToolParameterAccuracy": "TOOL_CALL",
    "Builtin.SkillSelectionAccuracy": "TOOL_CALL",
    "Builtin.SkillInstructionFollowing": "TOOL_CALL",
}
TRAJECTORY_LEVELS: dict[str, str] = {
    "Builtin.TrajectoryExactOrderMatch": "SESSION",
    "Builtin.TrajectoryInOrderMatch": "SESSION",
    "Builtin.TrajectoryAnyOrderMatch": "SESSION",
}
LEVELS: dict[str, str] = {**BUILTIN_LEVELS, **TRAJECTORY_LEVELS}


class ExpectedResponseCorrectnessEvaluator(CorrectnessEvaluator):
    """``Builtin.Correctness`` with Global's ground-truth field (audit item 2).

    Global: "``Builtin.Correctness`` | Trace | ``expectedResponse``", and "Traces without
    ground truth are evaluated using the ground truth-free variant". The strands class keys
    its reference mode on ``expected_assertion`` and renders the ASSERTIONS under
    ``EXPECTED RESPONSE:`` (``correctness_evaluator.py:90-97,162``); this twin keys it on
    ``expected_output`` (the dataset's expected response) and renders that, so assertions
    stay with GoalSuccessRate. The reference system prompt is the library's copy of the
    Global "Correctness with ground truth" rules; the user message follows the Global
    template's order (USER QUERY, AGENT RESPONSE, EXPECTED RESPONSE) — the undefined
    ``{additional_context}`` block is not rendered."""

    def _has_reference(self, evaluation_case: EvaluationData) -> bool:
        return bool(str(evaluation_case.expected_output or "").strip())

    def _format_prompt(self, parsed_input: TraceLevelInput) -> str:
        """The ground-truth-free prompt in Global's layout (Round 2 item 3): ``Context:`` is
        the previous turns plus the current prompt and its tool calls, and the judged answer
        appears ONCE, as the candidate. The library put the answer inside the context too
        (``# Assistant's Response`` and the turn's own assistant message), and our judge then
        read the claim it was asked to verify as established context (+0.14 lenient)."""
        answer = parsed_input.agent_response.text
        history = list(parsed_input.session_history or [])
        last_user = max((k for k, m in enumerate(history) if not isinstance(m, list)
                         and m.role.value == "user"), default=-1)
        lines: list[str] = []
        for k, message in enumerate(history):
            if isinstance(message, list):
                for tool_exec in message:
                    lines.append(f"Tool call: {tool_exec.tool_call.name}"
                                 f"({tool_exec.tool_call.arguments})")
                    lines.append(f"Tool result: {tool_exec.tool_result.content}")
                continue
            text = message.content[0].text if message.content and hasattr(
                message.content[0], "text") else ""
            if k > last_user and message.role.value == "assistant" and text.strip() == (
                    answer or "").strip():
                continue  # the judged turn's own answer is the candidate, not context
            lines.append(f"{message.role.value.capitalize()}: {text}")
        context = "\n".join(lines) or "(none)"
        return f"Context: {context}\n\nCandidate Response: {answer}"

    def _format_reference_prompt(self, parsed_input: TraceLevelInput,
                                 evaluation_case: EvaluationData) -> str:
        return "\n\n".join((
            f"USER QUERY:\n{self._extract_user_prompt(parsed_input)}",
            f"AGENT RESPONSE:\n{parsed_input.agent_response.text}",
            f"EXPECTED RESPONSE:\n{evaluation_case.expected_output}",
        ))


class ToolContextCoherenceEvaluator(CoherenceEvaluator):
    """``Builtin.Coherence`` with the tool calls and results in the judge's context.

    N17-obs404 (root cause): the strands class drops every tool execution from the history
    it renders ("Skip tool execution lists"), so our judge saw an answer quoting a tool's
    18 °C with no tool behind it and rated it "fabricated" (fresh probe-35, Kimi: 9 of
    Global's Completely Yes → Not Generally); Global's Coherence template reads ``{context}``
    with the tool outputs ("The response reports the tool's data accurately …"). The previous
    turns keep the library's shape; each tool execution adds a ``Tool call:`` /
    ``Tool result:`` pair, as the base class's trace-level prompt writes them."""

    def _format_prompt(self, parsed_input: TraceLevelInput) -> str:
        lines = []
        for msg in parsed_input.session_history or []:
            if isinstance(msg, list):
                for execution in msg:
                    lines.append(f"Tool call: {execution.tool_call.name}"
                                 f"({execution.tool_call.arguments})")
                    lines.append(f"Tool result: {execution.tool_result.content}")
            elif self._has_text_content(msg) and isinstance(msg.content[0], TextContent):
                lines.append(f"{msg.role.value.capitalize()}: {msg.content[0].text}")
        parts = [f"# Previous turns:\n{chr(10).join(lines)}"] if lines else []
        user_prompt = self._extract_user_prompt(parsed_input)
        parts.append(f"# Target turn to evaluate:\nUser: {user_prompt}\n"
                     f"Assistant: {parsed_input.agent_response.text}")
        return "\n\n".join(parts)


def _focus_skill(evaluation_case: EvaluationData) -> Any:
    """The ONE skill load a per-load unit judges (``metadata["skill_load"]``, N17-obs402 —
    ``engine_core.skill_cases``), or ``None`` for a whole-session case."""
    load = (evaluation_case.metadata or {}).get("skill_load")
    if not isinstance(load, dict):
        return None
    from strands_evals.extractors.skills import InvokedSkill

    return InvokedSkill(name=str(load.get("name") or ""), body=load.get("body"),
                        status="failed" if load.get("status") == "failed" else "loaded",
                        error=load.get("error"))


class PerLoadSkillSelectionAccuracyEvaluator(SkillSelectionAccuracyEvaluator):
    """SkillSelectionAccuracy judging the load its case names (N17-obs402): Global anchors
    one result to each loading span; the library folds every load of a skill into one row.
    The offered skills come from :func:`skill_catalog.available_skills`, which also reads
    the LangGraph Deep Agents ``**Available Skills:**`` list."""

    def _available_str(self, evaluation_case: EvaluationData) -> str:
        session = evaluation_case.actual_trajectory
        if offered := skill_catalog.available_skills(session):
            return "\n".join(f"- {s.name}: {s.description}" for s in offered)
        if skill_catalog.advertised(session):
            return "(none: this harness advertised no skills)"
        return "(not recorded by this harness)"

    def _has_catalog(self, evaluation_case: EvaluationData) -> bool:
        return bool(skill_catalog.available_skills(evaluation_case.actual_trajectory))

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        skill = _focus_skill(evaluation_case)
        if skill is None:
            return super().evaluate(evaluation_case)
        rating = self._judge(self._prompt_for(self._case_context(evaluation_case), skill))
        return [self._rating_to_output(rating, decision=skill.name)]

    async def evaluate_async(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        skill = _focus_skill(evaluation_case)
        if skill is None:
            return await super().evaluate_async(evaluation_case)
        rating = await self._judge_async(
            self._prompt_for(self._case_context(evaluation_case), skill))
        return [self._rating_to_output(rating, decision=skill.name)]


class PerLoadSkillInstructionFollowingEvaluator(SkillInstructionFollowingEvaluator):
    """SkillInstructionFollowing judging the load its case names (N17-obs402)."""

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        skill = _focus_skill(evaluation_case)
        if skill is None:
            return super().evaluate(evaluation_case)
        if reason := self._unscorable_reason(skill):
            return [self._not_applicable_row(reason)]
        from strands import Agent
        from strands_evals.evaluators.skill_instruction_following_evaluator import (
            SkillFollowingRating,
        )

        judge = Agent(model=self.model, system_prompt=self.system_prompt, callback_handler=None)
        result = judge(self._build_prompt(skill, evaluation_case),
                       structured_output_model=SkillFollowingRating)
        return [self._rating_to_output(skill, result.structured_output)]

    async def evaluate_async(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


_SAME_NAMED: dict[str, type[Evaluator]] = {
    "Builtin.GoalSuccessRate": GoalSuccessRateEvaluator,
    "Builtin.Helpfulness": HelpfulnessEvaluator,
    "Builtin.Correctness": ExpectedResponseCorrectnessEvaluator,
    "Builtin.Faithfulness": FaithfulnessEvaluator,
    "Builtin.ResponseRelevance": ResponseRelevanceEvaluator,
    "Builtin.Conciseness": ConcisenessEvaluator,
    "Builtin.Coherence": ToolContextCoherenceEvaluator,
    "Builtin.InstructionFollowing": InstructionFollowingEvaluator,
    "Builtin.Refusal": RefusalEvaluator,
    "Builtin.Harmfulness": HarmfulnessEvaluator,
    "Builtin.Stereotyping": StereotypingEvaluator,
    "Builtin.ToolSelectionAccuracy": ToolSelectionAccuracyEvaluator,
    "Builtin.ToolParameterAccuracy": ToolParameterAccuracyEvaluator,
    "Builtin.SkillSelectionAccuracy": PerLoadSkillSelectionAccuracyEvaluator,
    "Builtin.SkillInstructionFollowing": PerLoadSkillInstructionFollowingEvaluator,
}

# N17-obs377: Global's label strings per builtin (the template enums, and the labels the
# service returned on 2026-10-23 — DIFF-GLOBAL-EVAL B6). The values are unchanged; only the
# string a record carries. ``_LABEL_SYNONYMS`` are library labels with another Global word.
_YES_NO = ("Yes", "No")
_FIVE = ("Not At All", "Not Generally", "Neutral/Mixed", "Generally Yes", "Completely Yes")
GLOBAL_LABELS: dict[str, tuple[str, ...]] = {
    "Builtin.GoalSuccessRate": _YES_NO,
    "Builtin.Helpfulness": ("Not Helpful At All", "Very Unhelpful", "Somewhat Unhelpful",
                            "Neutral/Mixed", "Somewhat Helpful", "Very Helpful",
                            "Above And Beyond"),
    "Builtin.Correctness": ("Perfectly Correct", "Partially Correct", "Incorrect", "Correct"),
    "Builtin.Faithfulness": _FIVE,
    "Builtin.ResponseRelevance": _FIVE,
    "Builtin.Coherence": _FIVE,
    "Builtin.Conciseness": ("Not Concise", "Partially Concise", "Perfectly Concise"),
    "Builtin.InstructionFollowing": _YES_NO,
    "Builtin.Refusal": _YES_NO,
    "Builtin.Harmfulness": ("Harmful", "Not Harmful"),
    "Builtin.Stereotyping": ("Stereotyping", "Not Stereotyping"),
    "Builtin.ToolSelectionAccuracy": _YES_NO,
    "Builtin.ToolParameterAccuracy": _YES_NO,
    **{evaluator_id: _YES_NO for evaluator_id in TRAJECTORY_LEVELS},
}
_LABEL_SYNONYMS: dict[str, dict[str, str]] = {
    # GoalSuccessRate with assertions: the library's verdict enum, Global answers Yes / No
    "Builtin.GoalSuccessRate": {"success": "Yes", "failure": "No"},
}


def global_label(evaluator_id: str, label: Any) -> Any:
    """``label`` in Global's vocabulary for builtin ``evaluator_id`` (case-insensitive match,
    then the synonyms); a label outside it — or an evaluator without a table — unchanged."""
    if label is None or evaluator_id not in GLOBAL_LABELS:
        return label
    text = str(getattr(label, "value", label)).strip()
    folded = text.casefold()
    for word in GLOBAL_LABELS[evaluator_id]:
        if word.casefold() == folded:
            return word
    return _LABEL_SYNONYMS.get(evaluator_id, {}).get(folded, text)


class _GlobalLabels:
    """Mixin: the evaluator's outputs carry Global's label strings (N17-obs377)."""

    global_id = ""

    def _relabel(self, outputs: list[EvaluationOutput]) -> list[EvaluationOutput]:
        for out in outputs:
            out.label = global_label(self.global_id, out.label)
        return outputs

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        return self._relabel(super().evaluate(evaluation_case))  # type: ignore[misc]

    async def evaluate_async(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        return self._relabel(await super().evaluate_async(evaluation_case))  # type: ignore[misc]


# N17-obs411: the TRACE answer-quality judges are not called on an empty (or whitespace)
# answer — the row is decided here, deterministically, and says so (``DeterministicOutput``):
# - a bottom score where an empty response fails the criterion (it helps nobody, answers
#   nothing, follows no instruction, does not address the question — Conciseness: there is
#   no answer to be concise about), so empty turns pull the run mean down;
# - NOT_APPLICABLE (score None) where there is nothing to rate: Faithfulness (no claims) and
#   Coherence (no reasoning) — the judges scored "" 1.0 there.
# Refusal and the trajectory / tool / skill judges still judge the turn.
EMPTY_ANSWER_BOTTOM: dict[str, str] = {
    "Builtin.Helpfulness": "Not Helpful At All",
    "Builtin.Correctness": "Incorrect",
    "Builtin.InstructionFollowing": "No",
    "Builtin.Conciseness": "Not Concise",
    "Builtin.ResponseRelevance": "Not At All",
}
EMPTY_ANSWER_NOT_APPLICABLE = frozenset({"Builtin.Faithfulness", "Builtin.Coherence"})
EMPTY_ANSWER = "empty_answer"  # the ``deterministic`` flag of those rows


class DeterministicOutput(EvaluationOutput):
    """A row decided without a judge call; ``deterministic`` names the rule (the record and
    the stored row's ``judge_inference`` carry it, so it never reads as a judge verdict)."""

    deterministic: str = EMPTY_ANSWER


def judged_answer(evaluation_case: EvaluationData) -> str:
    """The answer a TRACE judge rates: the last agent span's response of the case's
    ``Session`` (what the strands builtins read), else ``actual_output``."""
    session = evaluation_case.actual_trajectory
    if isinstance(session, Session):
        for trace in reversed(session.traces):
            agent = next((s for s in trace.spans if isinstance(s, AgentInvocationSpan)), None)
            if agent is not None:
                return str(agent.agent_response or "")
    return str(evaluation_case.actual_output or "")


def empty_answer_row(evaluator_id: str) -> DeterministicOutput:
    """The deterministic row of ``evaluator_id`` for an empty answer."""
    if evaluator_id in EMPTY_ANSWER_BOTTOM:
        return DeterministicOutput(
            score=0.0, test_pass=False, label=EMPTY_ANSWER_BOTTOM[evaluator_id],
            reason="empty answer — the turn's final answer is empty; scored the bottom of "
                   "the scale without a judge call (deterministic rule, N17-obs411)")
    return DeterministicOutput(
        score=0.0, test_pass=False, label="NOT_APPLICABLE",
        reason="empty answer — the turn's final answer is empty, so there is nothing for "
               "this judge to rate; not judged (deterministic rule, N17-obs411)")


class _EmptyAnswerRule:
    """Mixin (outermost): an empty answer gets :func:`empty_answer_row`, never a judge call."""

    global_id = ""

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        if not judged_answer(evaluation_case).strip():
            return [empty_answer_row(self.global_id)]
        return super().evaluate(evaluation_case)  # type: ignore[misc]

    async def evaluate_async(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        if not judged_answer(evaluation_case).strip():
            return [empty_answer_row(self.global_id)]
        return await super().evaluate_async(evaluation_case)  # type: ignore[misc]


def _wrapped(evaluator_id: str, cls: type[Evaluator]) -> type[Evaluator]:
    bases: tuple[type, ...] = (cls,)
    if evaluator_id in GLOBAL_LABELS:
        bases = (_GlobalLabels, *bases)
    if evaluator_id in EMPTY_ANSWER_BOTTOM or evaluator_id in EMPTY_ANSWER_NOT_APPLICABLE:
        bases = (_EmptyAnswerRule, *bases)
    if bases == (cls,):
        return cls
    return type(cls.__name__, bases, {"global_id": evaluator_id})


_SAME_NAMED = {evaluator_id: _wrapped(evaluator_id, cls)
               for evaluator_id, cls in _SAME_NAMED.items()}

_TRAJECTORY_MODES: dict[str, Literal["exact", "in_order", "any_order"]] = {
    "Builtin.TrajectoryExactOrderMatch": "exact",
    "Builtin.TrajectoryInOrderMatch": "in_order",
    "Builtin.TrajectoryAnyOrderMatch": "any_order",
}


def _in_order_count(actual: list[str], expected: list[str]) -> int:
    """How many expected tools, from the start, appear in order within ``actual``."""
    calls = iter(actual)
    return next((k for k, name in enumerate(expected) if name not in calls), len(expected))


def trajectory_verdict(mode: Literal["exact", "in_order", "any_order"], actual: list[str],
                       expected: list[str]) -> tuple[bool, int]:
    """Global's binary rule for ``mode`` (audit item 6) → ``(passed, matched)``; ``matched``
    counts the expected tools the rule found, for the reason text only.

    - ``exact``: the same tools in the same order, no extras;
    - ``in_order``: ``expected`` is a subsequence of ``actual`` (extras allowed between);
    - ``any_order``: every expected tool is present at least once, in any order."""
    if mode == "exact":
        matched = sum(1 for a, e in zip(actual, expected, strict=False) if a == e)
        return actual == expected, matched
    if mode == "in_order":
        matched = _in_order_count(actual, expected)
        return matched == len(expected), matched
    called = set(actual)
    matched = sum(1 for name in expected if name in called)
    return matched == len(expected), matched


def _require_judge(judge: Any, evaluator_id: str) -> Model:
    if not isinstance(judge, Model):
        raise ValueError(
            f"{evaluator_id} needs a judge model object (got {type(judge).__name__}); build it "
            "with aws_clients.global_bedrock_model — a None/str would default to Claude"
        )
    return judge


def actual_tool_names(trajectory: Any) -> list[str]:
    """Tool names in call order from a strands ``Session`` or a plain list."""
    if isinstance(trajectory, Session):
        return [str(t.get("name") or "") for t in extract_agent_tools_used_from_trace(trajectory)]
    names: list[str] = []
    for item in trajectory or []:
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("toolName") or ""))
        else:
            names.append(str(item))
    return names


class TrajectoryMatchEvaluator(Evaluator[Any, Any]):
    """Deterministic twin of the Global trajectory evaluators (no LLM): a binary ``Pass``
    (1.0) / ``Fail`` (0.0) per :func:`trajectory_verdict` — the library's scorers zip the
    lists (trailing extras ignored) and give partial credit, which Global does not."""

    def __init__(self, mode: Literal["exact", "in_order", "any_order"], *, name: str) -> None:
        super().__init__(name=name)
        self.mode = mode

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        if evaluation_case.expected_trajectory is None:
            return [EvaluationOutput(score=0.0, test_pass=False, label="NOT_APPLICABLE",
                                     reason="no expected trajectory on this scenario")]
        expected = [str(x) for x in evaluation_case.expected_trajectory]
        actual = actual_tool_names(evaluation_case.actual_trajectory)
        # N17-obs376: [] = "no tools expected" (Global) — passes only when nothing was called
        passed, matched = (trajectory_verdict(self.mode, actual, expected) if expected
                           else (not actual, 0))
        return [EvaluationOutput(
            score=1.0 if passed else 0.0, test_pass=passed, label="Yes" if passed else "No",
            reason=(f"{self.mode}: {matched}/{len(expected)} expected tools matched; "
                    f"actual={actual} expected={expected}" if expected else
                    f"{self.mode}: no tools expected; actual={actual}"),
        )]

    async def evaluate_async(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


def _is_value(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _scale_entries(rating_scale: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [item for item in rating_scale or []
            if isinstance(item, dict) and str(item.get("label") or "").strip()]


def render_rubric(instructions: str, rating_scale: list[dict[str, Any]] | None) -> str:
    """The custom judge's rubric: the operator's instructions + the scale. A numerical
    entry reads ``- label = value``; a categorical one (no value) ``- label``."""
    entries = _scale_entries(rating_scale)
    numerical = any(_is_value(item.get("value")) for item in entries)
    header = ("Rating scale (return `score` = the value of the label you choose and `label` = "
              "that label):" if numerical else
              "Rating scale (return `label` = exactly one of these labels; `score` is not used):")
    lines = [str(instructions or "").strip(), "", header]
    for item in entries:
        label, value = item.get("label"), item.get("value")
        definition = item.get("definition") or ""
        head = f"- {label} = {value}" if _is_value(value) else f"- {label}"
        lines.append(head + (f": {definition}" if definition else ""))
    return "\n".join(lines).strip()


class JudgeLabelOffScale(ValueError):
    """The custom judge answered with a label that is not on its rating scale (audit item 7)
    → an error record typed ``invalid_label``, never a free-float score."""

    error_type = "invalid_label"


class CategoricalOutput(EvaluationOutput):
    """A verdict on a categorical scale: the label alone — Global gives such an entry no
    value, so the record's ``score`` is ``None`` and it never enters a mean."""

    score: float | None = None  # type: ignore[assignment]


def snap_to_scale(output: EvaluationOutput,
                  rating_scale: list[dict[str, Any]] | None) -> EvaluationOutput:
    """``output`` with its label mapped onto ``rating_scale`` (audit item 7).

    The judge's ``label`` picks the entry (case- and whitespace-insensitive); the result
    carries the entry's own label and, on a numerical entry, ``score`` = its value (a
    categorical entry gives a :class:`CategoricalOutput`). Without a label, a score equal to
    exactly one entry's value picks that entry. Anything else raises
    :class:`JudgeLabelOffScale`. No scale → ``output`` unchanged."""
    entries = _scale_entries(rating_scale)
    if not entries:
        return output
    said = str(output.label or "").strip()
    entry = next((item for item in entries
                  if str(item["label"]).strip().casefold() == said.casefold()), None)
    if entry is None and not said:
        hits = [item for item in entries
                if _is_value(item.get("value")) and float(item["value"]) == float(output.score)]
        entry = hits[0] if len(hits) == 1 else None
    if entry is None:
        allowed = ", ".join(repr(str(item["label"])) for item in entries)
        raise JudgeLabelOffScale(
            f"the judge returned label {said or None!r} (score {output.score!r}), which is not "
            f"on the rating scale ({allowed})")
    label, value = str(entry["label"]), entry.get("value")
    if not _is_value(value):
        return CategoricalOutput(test_pass=False, label=label, reason=output.reason)
    top = max(float(item["value"]) for item in entries if _is_value(item.get("value")))
    return EvaluationOutput(score=float(value), test_pass=float(value) >= top, label=label,
                            reason=output.reason)


# N17-obs338: Global appends a standardization prompt that enforces a reason and a score;
# ours asks for the reason first, then the scale label (snapped to its value afterwards)
CUSTOM_JUDGE_SYSTEM_PROMPT = (
    "You are an objective judge. Evaluate the content in the user message exactly as its "
    "evaluation instructions say. Keep your reasoning concise.")
CUSTOM_JUDGE_SUFFIX = (
    "Give your reasoning first (`reason`), then your decision: `label` = exactly one label "
    "of the rating scale above, and `score` = that label's value.")


class CustomJudgeEvaluator(OutputEvaluator):
    """A custom LLM judge whose placeholders are filled per case (N17-obs311) and whose
    verdict is snapped to its rating scale (audit item 7).

    The strands ``OutputEvaluator`` fixes its rubric at construction and puts it into the
    prompt verbatim, so ``{actual_tool_trajectory}`` & friends reached the judge as literal
    text. This twin keeps ``rubric`` as the authored text and renders
    ``judge_placeholders.render_placeholders(instructions, case)`` into the prompt of each
    call; the prompt frame (``<Input>`` / ``<Output>`` / ``<ExpectedOutput>``) and the
    structured ``EvaluationOutput`` are the base class's. The judge's free ``score`` is never
    stored: :func:`snap_to_scale` maps its label onto the scale."""

    def __init__(self, *, instructions: str, rating_scale: list[dict[str, Any]] | None,
                 model: Model, name: str) -> None:
        super().__init__(rubric=render_rubric(instructions, rating_scale), model=model,
                         name=name, system_prompt=CUSTOM_JUDGE_SYSTEM_PROMPT)
        self.instructions = instructions
        self.rating_scale = list(rating_scale or [])

    def rubric_for(self, evaluation_case: EvaluationData) -> str:
        """The rubric the judge reads for ``evaluation_case``."""
        rendered = judge_placeholders.render_placeholders(self.instructions, evaluation_case)
        return render_rubric(rendered, self.rating_scale)

    def _build_prompt(self, evaluation_case: EvaluationData) -> str:
        # N17-obs338: what Global sends — the rendered instructions (with the scale) and the
        # standardization suffix; no <Input> / <Output> / <ExpectedOutput> frame the author
        # never referenced
        return f"{self.rubric_for(evaluation_case)}\n\n{CUSTOM_JUDGE_SUFFIX}"

    def evaluate(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        return [snap_to_scale(out, self.rating_scale)
                for out in super().evaluate(evaluation_case)]

    async def evaluate_async(self, evaluation_case: EvaluationData) -> list[EvaluationOutput]:
        return [snap_to_scale(out, self.rating_scale)
                for out in await super().evaluate_async(evaluation_case)]


# N17-obs385: the budget rescue re-judges a case whose overlong text is cut with a visible
# marker — the judged answer keeps more than the context around it
ANSWER_CHAR_LIMIT = 8000
CONTEXT_CHAR_LIMIT = 3000


def _cut(text: Any, limit: int) -> tuple[Any, bool]:
    if not isinstance(text, str) or len(text) <= limit:
        return text, False
    head, tail = text[: limit * 3 // 4], text[-(limit // 4):]
    dropped = len(text) - len(head) - len(tail)
    return (f"{head}\n[… {dropped} characters truncated by the evaluator to fit the judge's "
            f"output budget …]\n{tail}"), True


def truncate_case(case: EvaluationData, *, answer_chars: int = ANSWER_CHAR_LIMIT,
                  context_chars: int = CONTEXT_CHAR_LIMIT) -> tuple[EvaluationData, bool]:
    """``(case, truncated)``: a copy of ``case`` whose agent answers (over ``answer_chars``)
    and tool results / history messages (over ``context_chars``) keep their head and tail
    around a ``[… N characters truncated …]`` marker the judge can see (N17-obs385). The
    input is never changed; a case with nothing to cut is returned as is."""
    session = case.actual_trajectory
    if not isinstance(session, Session):
        output, cut = _cut(case.actual_output, answer_chars)
        return (case.model_copy(update={"actual_output": output}), True) if cut else (case, False)
    copy = session.model_copy(deep=True)
    changed = False
    for trace in copy.traces:
        for span in trace.spans:
            if isinstance(span, AgentInvocationSpan):
                span.agent_response, cut = _cut(span.agent_response, answer_chars)
                changed |= cut
            elif isinstance(span, ToolExecutionSpan):
                span.tool_result.content, cut = _cut(span.tool_result.content, context_chars)
                changed |= cut
            elif isinstance(span, InferenceSpan):
                for message in span.messages:
                    for block in message.content:
                        if isinstance(block, TextContent):
                            block.text, cut = _cut(block.text, context_chars)
                            changed |= cut
    output, cut = _cut(case.actual_output, answer_chars)
    if not (changed or cut):
        return case, False
    return case.model_copy(update={"actual_trajectory": copy, "actual_output": output}), True


def level_of(definition: dict[str, Any]) -> str:
    """Evaluation level of a builtin or a custom row (derived rows take their base's)."""
    evaluator_id = str(definition.get("id") or "")
    if evaluator_id in LEVELS:
        return LEVELS[evaluator_id]
    if definition.get("definition") == "derived":
        return LEVELS.get(str(definition.get("base_evaluator_id") or ""), "TRACE")
    return str(definition.get("level") or "TRACE")


def build(definition: dict[str, Any], judge: Any, *, lambda_client: Any = None) -> Evaluator:
    """One evaluator for ``definition`` = ``{"id": "Builtin.X"}`` or a custom row
    (``{id, definition judge|derived|code, instructions, rating_scale, base_evaluator_id,
    lambda_arn, lambda_timeout_s, level}``)."""
    evaluator_id = str(definition.get("id") or "")
    if evaluator_id in _TRAJECTORY_MODES:
        return TrajectoryMatchEvaluator(_TRAJECTORY_MODES[evaluator_id], name=evaluator_id)
    if evaluator_id == "Builtin.ContextRelevance":
        return ContextRelevanceEvaluator(model=_require_judge(judge, evaluator_id),
                                         name=evaluator_id)
    if evaluator_id in _SAME_NAMED:
        # N17-obs335 / obs339: Global's rubric where the library's differs
        extra = ({"system_prompt": global_templates.SYSTEM_PROMPTS[evaluator_id]}
                 if evaluator_id in global_templates.SYSTEM_PROMPTS else {})
        return _SAME_NAMED[evaluator_id](model=_require_judge(judge, evaluator_id),
                                         name=evaluator_id, **extra)
    kind = definition.get("definition")
    if evaluator_id.startswith("Builtin.") or not kind:
        raise ValueError(f"unknown evaluator id {evaluator_id!r}")
    if kind == "judge":
        return CustomJudgeEvaluator(
            instructions=str(definition.get("instructions") or ""),
            rating_scale=definition.get("rating_scale"),
            model=_require_judge(judge, evaluator_id), name=evaluator_id,
        )
    if kind == "derived":
        base = str(definition.get("base_evaluator_id") or "")
        if base not in _SAME_NAMED and base != "Builtin.ContextRelevance":
            raise ValueError(f"{evaluator_id}: base evaluator {base!r} is not a builtin judge")
        built = build({"id": base}, judge)
        built.name = evaluator_id
        return built
    if kind == "code":
        if lambda_client is None:
            raise ValueError(f"{evaluator_id}: a code evaluator needs an injected lambda client")
        # the Global envelope (N17-obs327) reads engine_core's per-turn units and engine_core
        # imports this module: a relative import at call time (the worker zip keeps it as is)
        from .code_evaluators import LambdaEvaluator

        return LambdaEvaluator(
            lambda_arn=str(definition["lambda_arn"]), lambda_client=lambda_client,
            timeout_s=int(definition.get("lambda_timeout_s") or 60), name=evaluator_id,
            evaluator_name=str(definition.get("name") or evaluator_id),
            level=level_of(definition),
        )
    raise ValueError(f"{evaluator_id}: unknown evaluator definition {kind!r}")
