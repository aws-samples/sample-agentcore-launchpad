"""N17-obs311 — a custom LLM judge sees the values its placeholders name.

The judge prompt used to carry the operator's instructions verbatim: ``{actual_tool_trajectory}``
and friends reached the judge model as literal text, so a verdict about tool use was a
guess from the answer text. Every placeholder is now rendered from the evaluation case
before the judge call; an absent value reads "(not available)", never the placeholder.
"""

import json
from pathlib import Path

import pytest
from strands_evals.types.evaluation import EvaluationData

from app.services.strands_eval import engine_core
from app.services.strands_eval import evaluators as ev
from app.services.strands_eval import judge_placeholders as jp
from app.services.strands_eval.spans_provider import LaunchpadSpansProvider
from tests.evaluation.fakes import FakeLogs, FakeModel

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "spans_session.json"
SCALE = [{"label": "pass", "value": 1.0, "definition": "ok"},
         {"label": "fail", "value": 0.0, "definition": "bad"}]


def _session():
    fx = json.loads(FIXTURE.read_text())
    logs = FakeLogs({"sort": [{"@message": json.dumps(r)} for r in fx["records"]]})
    provider = LaunchpadSpansProvider(logs, [fx["log_group"]], spans_source="runtime_logs")
    return fx["session_id"], provider.get_evaluation_data(fx["session_id"])


def _case(truth=None):
    session_id, task_output = _session()
    return engine_core.evaluation_data(session_id, task_output, truth)


def _row(instructions, level="SESSION"):
    return {"id": "cust_traj", "definition": "judge", "level": level,
            "instructions": instructions, "rating_scale": SCALE}


def _judge_prompt(judge: FakeModel) -> str:
    """Everything the judge model was sent (the user turn of each call)."""
    return json.dumps([c.get("messages") or c.get("prompt") for c in judge.calls],
                      ensure_ascii=False, default=str)


def test_the_judge_prompt_carries_the_rendered_trajectory_and_context():
    judge = FakeModel(structured=[{"score": 1.0, "test_pass": True, "reason": "called it",
                                   "label": "pass"}])
    built = ev.build(_row("Pass only when {actual_tool_trajectory} shows hr_lookup. "
                          "Conversation: {context}. Tools: {available_tools}"), judge)
    out = built.evaluate(_case())
    prompt = _judge_prompt(judge)
    for literal in ("{actual_tool_trajectory}", "{context}", "{available_tools}"):
        assert literal not in prompt
    assert "1. hr_lookup" in prompt and "employee" in prompt and "ada" in prompt
    assert "Ada has 12 vacation days left." in prompt  # the tool result and the answer
    assert "How many vacation days does Ada have?" in prompt  # the user turn in {context}
    assert "- hr_lookup" in prompt  # {available_tools}
    # the scoring and output shape are unchanged
    assert len(out) == 1 and out[0].score == 1.0 and out[0].label == "pass"
    assert built.name == "cust_traj" and built.model is judge


def test_ground_truth_placeholders_render_from_the_case():
    truth = {"scenario_id": "s1", "input": "How many vacation days does Ada have?",
             "expected_response": "Ada has 12 days.", "assertions": ["cites the tool",
                                                                    "is polite"],
             "expected_trajectory": ["hr_lookup", "hr_policy"]}
    judge = FakeModel(structured=[{"score": 0.0, "test_pass": False, "reason": "x"}])
    ev.build(_row("Want {expected_response}; also {expected_output}; check {assertions}; "
                  "path {expected_tool_trajectory} / {expected_trajectory}"), judge
             ).evaluate(_case(truth))
    prompt = _judge_prompt(judge)
    assert "Want Ada has 12 days.; also Ada has 12 days." in prompt
    assert "- cites the tool" in prompt and "- is polite" in prompt
    assert prompt.count("1. hr_lookup") >= 2 and "2. hr_policy" in prompt
    assert "{expected" not in prompt and "{assertions}" not in prompt


def test_absent_values_render_as_not_available():
    case = EvaluationData(input="hi", actual_output="hello")  # no spans, no ground truth
    text = jp.render_placeholders(
        "{actual_tool_trajectory}|{available_tools}|{expected_response}|{assertions}|"
        "{expected_tool_trajectory}|{tool_turn}|{invoked_skill}|{mystery_field}", case)
    assert text == "|".join([jp.NOT_AVAILABLE] * 8)
    assert jp.NOT_AVAILABLE == "(not available)"
    # the conversation still renders from the case's input and output
    assert jp.render_placeholders("{context}", case) == "User: hi\nAssistant: hello"
    assert jp.render_placeholders("{assistant_turn} / {user_message}", case) == "hello / hi"


def test_a_session_without_tool_calls_says_so_rather_than_not_available():
    session_id, task_output = _session()
    session = task_output["trajectory"]
    for trace in session.traces:
        trace.spans = [s for s in trace.spans if type(s).__name__ != "ToolExecutionSpan"]
    case = engine_core.evaluation_data(session_id, task_output, None)
    assert jp.render_placeholders("{actual_tool_trajectory}", case) == jp.NO_TOOL_CALLS


def test_a_plain_list_trajectory_renders_names_and_inputs():
    case = EvaluationData(input="q", actual_output="a", actual_trajectory=[
        {"name": "get_pto_balance", "input": {"employee_id": "EMP-0001"}, "tool_result": "9"},
        "submit_leave"])
    rendered = jp.render_placeholders("{actual_tool_trajectory}", case)
    assert rendered.splitlines() == [
        '1. get_pto_balance({"employee_id": "EMP-0001"}) -> 9', "2. submit_leave"]


def test_rendering_is_single_pass_and_bounded():
    # a value that itself reads like a placeholder is not rendered again
    case = EvaluationData(input="{assertions}", actual_output="x" * 50_000)
    assert jp.render_placeholders("{user_message}", case) == "{assertions}"
    assert len(jp.render_placeholders("{assistant_turn}", case)) <= jp.MAX_VALUE_CHARS + 20
    # JSON examples in the instructions are left alone
    assert jp.render_placeholders('{"score": 1}', case) == '{"score": 1}'


def test_the_rubric_attribute_stays_the_authored_text():
    built = ev.build(_row("Rate {actual_tool_trajectory}"), FakeModel())
    assert isinstance(built, ev.CustomJudgeEvaluator)
    assert "{actual_tool_trajectory}" in built.rubric  # what the operator wrote
    rendered = built.rubric_for(_case())
    assert "{actual_tool_trajectory}" not in rendered and "hr_lookup" in rendered
    assert "pass = 1.0" in rendered  # the rating scale is still appended


def test_every_placeholder_the_platform_offers_is_renderable():
    """One catalogue: the online / canary refusal, the evaluator catalog and the
    architect plan's level table all name placeholders this renderer fills."""
    from app.assistant import evaluation_plan
    from app.evaluation import evaluators_local, online_evaluators

    assert online_evaluators.GROUND_TRUTH_PLACEHOLDERS is jp.GROUND_TRUTH_PLACEHOLDERS
    assert evaluators_local.GROUND_TRUTH_PLACEHOLDERS is jp.GROUND_TRUTH_PLACEHOLDERS
    assert set(evaluation_plan.REFERENCE_PLACEHOLDERS) == set(jp.GROUND_TRUTH_PLACEHOLDERS)
    for level, names in evaluation_plan.JUDGE_PLACEHOLDERS.items():
        assert names <= set(jp.LEVEL_PLACEHOLDERS[level]) | set(jp.RENDERED), level
    offered = {n for names in jp.LEVEL_PLACEHOLDERS.values() for n in names}
    assert offered <= set(jp.RENDERED) | set(jp.SESSION_UNRENDERED)
    assert not set(jp.SESSION_UNRENDERED) & set(jp.RENDERED)
    # the trajectory the agent actually took is measured from the trace, not ground truth
    assert "actual_tool_trajectory" not in jp.GROUND_TRUTH_PLACEHOLDERS
    assert "actual_tool_trajectory" in jp.LEVEL_PLACEHOLDERS["SESSION"]


def test_the_worker_zip_ships_the_renderer():
    from app.templates import eval_worker as ew

    files = ew.library_files()
    assert "launchpad_eval/judge_placeholders.py" in files
    assert ("from launchpad_eval import global_templates, judge_placeholders"
            in files["launchpad_eval/evaluators.py"])


@pytest.mark.parametrize("instructions", [
    "Pass when {actual_tool_trajectory} calls hr_lookup",
    "Given {context} and {available_tools}, did {actual_tool_trajectory} fit?",
])
def test_a_trajectory_only_judge_needs_no_ground_truth(instructions):
    from app.evaluation.online_evaluators import ground_truth_placeholders

    assert ground_truth_placeholders(instructions) == []
