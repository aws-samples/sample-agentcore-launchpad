"""Round 2 item 2 — judge template parity with Global AgentCore Evaluations.

- obs335: Builtin.Refusal uses Global's rubric (criteria 1–2 only; the library's criteria
  3–4, "inability due to tool limitations is NOT a refusal", lowered our refusal rate).
- obs339: GoalSuccessRate / Faithfulness / Harmfulness carry Global's "tool output ALWAYS
  takes priority" line; Helpfulness uses Global's label case; every builtin's system
  prompt is hash-pinned, so a strands-evals bump cannot change judge text silently.
- obs338: a custom judge's prompt is its rendered instructions + the rating scale + a
  reason-then-label suffix, without the library's <Input>/<Output>/<ExpectedOutput> frame;
  placeholders outside the evaluator level's set are refused at create / update.
- audit 3.2: Builtin.ContextRelevance is labelled a Launchpad approximation; N17-obs403 makes
  it a Launchpad-only judge with a fixed scale (its prompt lives in global_templates too).
- N17-obs404: Coherence / Conciseness carry the live Global rules (Round 3).
- N17-obs478: Refusal keeps the library rubric plus the live rule for explicit declines.
- N17-obs477: ToolSelectionAccuracy carries Global's two live rules (exact repeat = redundant;
  identity / fraud / safety out of scope).
"""

import hashlib
import json

import pytest

from app.services.strands_eval import evaluators as ev
from app.services.strands_eval import global_templates as gt
from tests.evaluation.fakes import FakeModel
from tests.evaluation.test_custom_judge_placeholders import _case

PRIORITY = "**IMPORTANT**: The tool output ALWAYS takes priority over your own knowledge."


def _system(evaluator_id):
    return ev.build({"id": evaluator_id}, FakeModel()).system_prompt


def test_refusal_keeps_the_library_rubric_the_live_global_service_applies():
    """obs335 reversed by evidence: the live Global judge cites the library's criteria 3–4
    ("a data/tool limitation, not an unwillingness to help"), which the published template
    lacks — the library rubric is the one that matches Global's verdicts. N17-obs478 appends
    the live rule that an explicit decline is a refusal even next to a limitation."""
    from strands_evals.evaluators.prompt_templates.refusal import get_template

    prompt = _system("Builtin.Refusal")
    assert prompt.startswith(get_template("v0").SYSTEM_PROMPT)
    assert prompt.endswith(gt.REFUSAL_LIVE_GUIDELINES)
    assert "tool limitations" in prompt and PRIORITY in prompt
    assert "explicitly declines to do what the user asked, it is a refusal" in prompt


@pytest.mark.parametrize("evaluator_id", ["Builtin.GoalSuccessRate", "Builtin.Faithfulness",
                                          "Builtin.Harmfulness", "Builtin.Helpfulness",
                                          "Builtin.Refusal", "Builtin.Correctness"])
def test_the_tool_output_priority_line_is_in_every_global_rubric_that_has_it(evaluator_id):
    assert PRIORITY in _system(evaluator_id)


def test_helpfulness_uses_globals_label_case():
    prompt = _system("Builtin.Helpfulness")
    for label in ("Not Helpful At All", "Very Unhelpful", "Somewhat Unhelpful",
                  "Somewhat Helpful", "Very Helpful", "Above And Beyond"):
        assert label in prompt
    assert "Very helpful" not in prompt


def test_no_global_rubric_keeps_a_placeholder_or_the_json_schema():
    for text in gt.SYSTEM_PROMPTS.values():
        assert "{context}" not in text and "{assistant_turn}" not in text
        assert "JSON schema" not in text


def test_tool_selection_accuracy_carries_globals_live_rules():
    prompt = gt.SYSTEM_PROMPTS["Builtin.ToolSelectionAccuracy"]
    assert prompt.startswith("You are an objective judge evaluating if an AI assistant's action")
    assert "the same tool with the same arguments) is redundant" in prompt
    assert "Identity, fraud and safety concerns are out of scope" in prompt
    assert "same purpose" not in prompt  # Global's strict rule, not a substantive one


# sha256 of every builtin judge's system prompt as built (``system_prompt`` and, where the
# class has one, its reference-mode prompt). Update ONLY with a reviewed template change.
PINNED = {
    'Builtin.Coherence': {'system': 'cd5f2d45e5acea24'},
    'Builtin.Conciseness': {'system': '02ac0b67e3d6b058'},
    'Builtin.ContextRelevance': {'system': 'a15d86f3529b6e5f'},
    'Builtin.Correctness':
        {'reference_system_prompt': '10ae30d6b15e247d', 'system': 'c716f382382d659d'},
    'Builtin.Faithfulness': {'system': 'c8ee327d6fbb3232'},
    'Builtin.GoalSuccessRate':
        {'assertion_system_prompt': '058a7659dda801c4', 'system': 'edc7e53bb27414d7'},
    'Builtin.Harmfulness': {'system': 'be5faace95e40af7'},
    'Builtin.Helpfulness': {'system': '13bd203f5b36422a'},
    'Builtin.InstructionFollowing': {'system': '846720d3c9cb5421'},
    'Builtin.Refusal': {'system': 'ad8380c8ece1071c'},
    'Builtin.ResponseRelevance': {'system': '444650c877bed5e7'},
    'Builtin.SkillInstructionFollowing': {'system': '0f9d681d8db02366'},
    'Builtin.SkillSelectionAccuracy': {'system': '676adaf404f15e80'},
    'Builtin.Stereotyping': {'system': '3be1c87825708486'},
    'Builtin.ToolParameterAccuracy': {'system': 'edb454b20e971982'},
    'Builtin.ToolSelectionAccuracy': {'system': '20ae2cf5f07c5426'},
}


def _prompts(evaluator_id):
    built = ev.build({"id": evaluator_id}, FakeModel())
    out = {"system": built.system_prompt}
    for attr in ("reference_system_prompt", "assertion_system_prompt", "rubric"):
        if getattr(built, attr, None):
            out[attr] = getattr(built, attr)
    return out


def _digest(evaluator_id):
    return {k: hashlib.sha256(v.encode()).hexdigest()[:16]
            for k, v in sorted(_prompts(evaluator_id).items())}


@pytest.mark.parametrize("evaluator_id", sorted(ev.BUILTIN_LEVELS))
def test_every_builtin_judge_prompt_is_pinned(evaluator_id):
    assert _digest(evaluator_id) == PINNED[evaluator_id], (
        f"{evaluator_id}: its judge prompt changed — review the diff against Global's "
        f"template, then update PINNED with {_digest(evaluator_id)!r}")


SCALE = [{"label": "pass", "value": 1.0, "definition": "ok"},
         {"label": "fail", "value": 0.0, "definition": "bad"}]


def _sent(judge):
    return json.dumps([c.get("messages") for c in judge.calls], ensure_ascii=False,
                      default=str)


def test_a_custom_judge_sends_its_instructions_only():
    judge = FakeModel(structured=[{"score": 1.0, "test_pass": True, "reason": "r",
                                   "label": "pass"}])
    row = {"id": "cust_t", "definition": "judge", "level": "TRACE",
           "instructions": "Is the answer polite? Answer: {assistant_turn}",
           "rating_scale": SCALE}
    built = ev.build(row, judge)
    built.evaluate(_case())
    sent = _sent(judge)
    for frame in ("<Input>", "<Output>", "<ExpectedOutput>", "<Rubric>"):
        assert frame not in sent
    assert "Is the answer polite?" in sent and "Ada has 12 vacation days left." in sent
    assert "pass = 1.0" in sent and "reasoning first" in sent
    assert "<Input>" not in built.system_prompt


def test_create_refuses_a_placeholder_outside_the_level(client):
    res = client.post("/api/eval/evaluators", json={
        "name": "trace_assertions", "level": "TRACE",
        "instructions": "Check {assertions} against {assistant_turn}"})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "evaluator.placeholder_not_allowed"
    assert "assertions" in res.json()["message"]
    ok = client.post("/api/eval/evaluators", json={
        "name": "trace_ok", "level": "TRACE",
        "instructions": "Rate {assistant_turn} given {context}"})
    assert ok.status_code in (200, 201), ok.text
    upd = client.put(f"/api/eval/evaluators/{ok.json()['evaluator_id']}", json={
        "instructions": "Now with {tool_turn} and {context}"})
    assert upd.status_code == 422 and upd.json()["code"] == "evaluator.placeholder_not_allowed"


def test_context_relevance_is_labelled_a_launchpad_approximation():
    from app.evaluation import evaluators_local

    out = evaluators_local.builtin_out("Builtin.ContextRelevance")
    assert "Launchpad approximation" in out["description"]
    assert evaluators_local.builtin_out("Builtin.Correctness")["description"] == ""
