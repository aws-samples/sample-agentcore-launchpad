"""Round 2 item 3 — the Round 1 calibration targets, fixed on the platform side.

- Correctness was +0.14 lenient vs Global on research-17 (11/11 disagreements ours higher).
  Cause (prompt): the library's basic Correctness prompt put the judged answer INSIDE
  ``Context:`` (``# Assistant's Response: …``) and again as ``Candidate Response:`` — our judge
  wrote "the response is identical to the assistant's response in the context" and treated
  it as established. Global's ``{context}`` is the previous turns + the current prompt and
  tool calls; the answer appears once, as the candidate.
- GoalSuccessRate was 0.17 stricter on probe-35 (6/7 disagreements ours No / Global Yes).
  Cause (prompt): the live Global rubric applies guidelines the published template lacks —
  its explanations cite them ("a goal awaiting the user's reply for genuinely needed
  information does not count as a failed goal"; "minor imprecision in secondary details
  should not fail a goal"). Our judge failed exactly those cases (clarifying question,
  preamble requirement).
"""

import json

from app.services.strands_eval import evaluators as ev
from app.services.strands_eval import global_templates as gt
from tests.evaluation.fakes import FakeModel
from tests.evaluation.test_custom_judge_placeholders import _case
from tests.evaluation.test_judge_calibration import PAIRS, calibration

ANSWER = "Ada has 12 vacation days left."


def _sent(judge):
    return "\n".join(json.dumps(c.get("messages"), ensure_ascii=False, default=str)
                     for c in judge.calls)


def test_correctness_shows_the_judged_answer_once_as_the_candidate():
    judge = FakeModel(structured=[{"reasoning": "ok", "score": "Perfectly Correct"}])
    ev.build({"id": "Builtin.Correctness"}, judge).evaluate(_case())
    sent = _sent(judge)
    assert sent.count("Candidate Response: " + ANSWER) == 1
    context = sent.split("Candidate Response:")[0]
    assert "Assistant: " + ANSWER not in context  # never the judged answer as context
    assert "# Assistant's Response" not in sent
    context = sent.split("Candidate Response:")[0]
    assert "How many vacation days does Ada have?" in context  # the current prompt
    assert "hr_lookup" in context and "employee" in context  # its tool call and result


def test_correctness_context_keeps_earlier_turns_answers():
    pair = next(p for p in PAIRS if p["id"] == "stock-qty-en")
    judge = FakeModel(structured=[{"reasoning": "ok", "score": "Perfectly Correct"}])
    ev.build({"id": "Builtin.Correctness"}, judge).evaluate(calibration.case(pair, "good"))
    sent = _sent(judge)
    context, candidate = sent.split("Candidate Response:")
    assert "ORD-1001" in context and "SF Express" in context  # turn 1, answered before
    assert pair["good"]["answer"] in candidate and pair["good"]["answer"] not in context


def test_goal_success_applies_the_live_global_guidelines():
    prompt = ev.build({"id": "Builtin.GoalSuccessRate"}, FakeModel()).system_prompt
    assert prompt.startswith(gt.GOAL_SUCCESS_RATE)
    assert "waiting on the user's reply for information the assistant genuinely needs" in prompt
    assert "Minor imprecision in secondary details should not fail a goal" in prompt
    # the assertion mode (with ground truth) keeps its own Global template
    built = ev.build({"id": "Builtin.GoalSuccessRate"}, FakeModel())
    assert "genuinely needs" not in built.assertion_system_prompt


def test_n17_obs389_helpfulness_scores_a_redirect_only_refusal_down():
    """Global rates a polite refusal of a legitimate request that only redirects the user
    "Somewhat Unhelpful" (0.33); GLM rated it 0.5–0.667 (target-over-refuse)."""
    prompt = ev.build({"id": "Builtin.Helpfulness"}, FakeModel()).system_prompt
    assert prompt.startswith(gt.HELPFULNESS)
    assert "only redirects the user to another person or channel" in prompt
    assert "rate it Somewhat Unhelpful" in prompt
    # not covered: an attempted lookup that found nothing, a refusal it must make
    assert "attempted the request" in prompt and "another person's private data" in prompt


def test_n17_obs389_faithfulness_does_not_call_unsupported_facts_completely_yes():
    """Global's live Faithfulness lowers answers whose facts nothing in the conversation
    supports (target-no-kb); the published rubric only names conflicts."""
    prompt = ev.build({"id": "Builtin.Faithfulness"}, FakeModel()).system_prompt
    assert prompt.startswith(gt.FAITHFULNESS)
    assert 'rate it "Not Generally" or lower' in prompt
    assert "do not lower the rating" in prompt  # minor embellishments: Global keeps them


def test_the_calibration_set_carries_the_obs389_shapes():
    redirects = [p for p in PAIRS if p["id"].startswith("redirect-")]
    assert len(redirects) == 6 and {p["lang"] for p in redirects} == {"zh", "en"}
    for pair in redirects:
        assert pair["bad"]["tools"] is False and pair["good"]["tools"] is True
        assert pair["anchors"] == {"Builtin.Helpfulness": {"bad_max": 0.333}}
    ungrounded = [p for p in PAIRS if p["defect"] == "tool_not_used"]
    assert ungrounded and all("Builtin.Faithfulness" in p["labels"] for p in ungrounded)
    assert all(p["anchors"]["Builtin.Faithfulness"] == {"bad_max": 0.5} for p in ungrounded)


def test_n17_obs404_coherence_sees_the_tool_results():
    """The strands Coherence prompt dropped every tool execution, so our judge called an
    answer quoting its tool "fabricated"; Global's {context} carries the tool outputs."""
    judge = FakeModel(structured=[{"reasoning": "ok", "score": "Completely Yes"}])
    ev.build({"id": "Builtin.Coherence"}, judge).evaluate(_case())
    sent = _sent(judge)
    assert "Tool call: hr_lookup" in sent and "Tool result: Ada has 12 vacation days" in sent
    assert "# Target turn to evaluate" in sent


def test_n17_obs404_coherence_and_conciseness_apply_the_live_global_rules():
    from strands_evals.evaluators.prompt_templates.coherence import coherence_v0
    from strands_evals.evaluators.prompt_templates.conciseness import conciseness_v0

    coherence = ev.build({"id": "Builtin.Coherence"}, FakeModel()).system_prompt
    assert coherence.startswith(coherence_v0.SYSTEM_PROMPT)
    assert "contradicts the assistant's own earlier turn" in coherence
    assert "only an example or an aside does not lower the rating" in coherence
    conciseness = ev.build({"id": "Builtin.Conciseness"}, FakeModel()).system_prompt
    assert conciseness.startswith(conciseness_v0.SYSTEM_PROMPT)
    assert "- Partially Concise: the core answer plus" in conciseness and "dominates" in \
        conciseness
    assert "is not extra content" in conciseness
