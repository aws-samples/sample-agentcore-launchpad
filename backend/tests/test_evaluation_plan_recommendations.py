"""WB-006 (P0, 2026-10-14): a recommendation's **gate intent** reaches the plan.

A proposal saying "``hr-guardrails-judge`` must block publishing at threshold 1.00" used
to draft an entry ``{"blocking": false, "threshold": null}`` shown as *informational*,
with the plan valid and ``unresolved_recommendations = 0``. Now the intent is read from
the structured entry or parsed out of the prose (EN / ZH), carried onto every evaluator
the recommendation maps to, and a gate no evaluator carries leaves the recommendation
``unresolved`` AND the plan invalid with a message naming it. Hermetic: no AWS.
"""

import json

import pytest

from app.assistant import evaluation_plan as plans
from app.assistant.recommendation_intent import (
    REPAIR_NOTE_PREFIX,
    normalise_entry,
    parse_intent,
    resolve_intent,
)

HASH = "0" * 64
GOLDEN = [{"id": "GT-1", "input": "How many vacation days do I get?",
           "pass_criteria": "cites the leave policy", "source": "customer_pain_point"}]
SEED_JUDGE = {
    "kind": "judge", "key": "hr-guardrails-judge", "title": "HR guardrails judge",
    "name": "hr_guardrails_judge", "level": "TRACE",
    "instructions": "Judge {context} against the HR guardrails", "golden_test_ids": [],
}
SEED = {
    "evaluators": [SEED_JUDGE],
    "scenarios": [{"scenario_id": "GT-1", "golden_test_id": "GT-1",
                   "turns": [{"input": "How many vacation days do I get?"}],
                   "assertions": ["cites the leave policy"]}],
}


def _content(recommendations, *, seed=SEED):
    content = {
        "version": 1, "name": "hr-helpdesk", "model_id": "global.zai.glm-5.3",
        "system_prompt": "You answer HR policy questions.", "golden_tests": GOLDEN,
        "evaluator_recommendations": recommendations,
    }
    if seed is not None:
        content["evaluation_plan"] = json.loads(json.dumps(seed))
    return content


def _draft(content):
    return plans.draft_plan(content, revision=1, content_hash=HASH, agent_name="hr-helpdesk")


def _validate(draft, content):
    return plans.validate_plan(draft, content, revision=1, content_hash=HASH)


def _entry(draft, key):
    return next(e for e in draft["evaluators"] if e["key"] == key)


# ---------------------------------------------------------------------------
# the prose parser (EN / ZH markers)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, blocking, threshold", [
    ("hr-guardrails-judge must block publishing at threshold 1.00", True, 1.0),
    ("Builtin.Refusal is a blocking evaluator (threshold 0.95)", True, 0.95),
    ("Builtin.Harmfulness as the release gate; pass rate ≥ 0.9", True, 0.9),
    ("Builtin.Refusal: a hard gate, minimum score 90%", True, 0.9),
    ("Builtin.InstructionFollowing — must pass before release, at least 0.8", True, 0.8),
    ("Builtin.Helpfulness, threshold 0.7 (informational)", False, 0.7),
    ("Builtin.Helpfulness, informational only", False, None),
    ("Builtin.Helpfulness non-blocking, track the trend", False, None),
    ("Use Builtin.Helpfulness to track the quality trend", None, None),
])
def test_english_markers_are_parsed(text, blocking, threshold):
    intent = parse_intent(text)
    assert (intent.blocking, intent.threshold) == (blocking, threshold)
    assert intent.source == ("parsed" if blocking is not None or threshold is not None
                             else None)


@pytest.mark.parametrize("text, blocking, threshold", [
    ("hr_guardrails_judge 阻断发布，阈值 1.00", True, 1.0),
    ("Builtin.Refusal 作为门禁，阈值 0.95", True, 0.95),
    ("Builtin.Harmfulness 阻断发布，通过率至少 0.9", True, 0.9),
    ("Builtin.Refusal 作为发布门禁，不低于 95%", True, 0.95),
    ("Builtin.InstructionFollowing 必须通过，阈值为 0.8", True, 0.8),
    ("Builtin.Helpfulness 仅供参考", False, None),
    ("Builtin.Helpfulness 不阻断，阈值 0.7", False, 0.7),
    ("用 Builtin.Helpfulness 观察质量趋势", None, None),
])
def test_chinese_markers_are_parsed(text, blocking, threshold):
    intent = parse_intent(text)
    assert (intent.blocking, intent.threshold) == (blocking, threshold)


def test_negations_win_over_positive_markers_and_gateway_is_not_a_gate():
    assert parse_intent("not a blocking gate — informational").blocking is False
    assert parse_intent("非阻断门禁，仅供参考").blocking is False
    assert parse_intent("route through the Gateway tool and the gateway:hr key").blocking is None


@pytest.mark.parametrize("text", [
    # rev-r8 (cn/r9-d review): ordinary architect sentences used to produce a gate — and an
    # unmapped gate invalidates the proposal, so a false positive is a P0 regression
    "Add at least 10 golden tests covering refusals",
    "Run ≥ 3 tool calls per scenario",
    "至少 5 个场景覆盖拒答",
    "The judge must pass the session transcript as context",
    "避免阻断用户流程，保持对话流畅",
    "avoid blocking the user flow with confirmations",
    "必须通过 API 调用获取员工数据",
    "at least 1 example per intent in the golden set",
    "accuracy matters for every knowledge-base answer",
])
def test_ordinary_sentences_carry_no_gate(text):
    intent = parse_intent(text)
    assert intent.blocking is not True and intent.threshold is None, (intent.blocking,
                                                                      intent.threshold)
    assert intent.has_intent is False and intent.metric is None


@pytest.mark.parametrize("text, threshold", [
    ("at least 0.8", 0.8),
    ("threshold 90", 0.9),
    ("阈值 0.9", 0.9),
    ("≥ 90%", 0.9),
    ("score ≥ 0.9", 0.9),
    ("至少 95％", 0.95),
    ("通过率不低于 0.95", 0.95),
])
def test_score_like_thresholds_are_still_read(text, threshold):
    assert parse_intent(text).threshold == threshold


def test_bare_integers_are_counts_not_percentages():
    # only next to the threshold keyword is a bare integer a percentage
    assert parse_intent("threshold 90").threshold == 0.9
    assert parse_intent("at least 90 sessions").threshold is None
    assert parse_intent("≥ 90").threshold is None
    assert parse_intent("≥ 90 points").threshold == 0.9


def test_blocking_markers_need_a_release_decision():
    assert parse_intent("阻断发布").blocking is True
    assert parse_intent("阻断上线").blocking is True
    assert parse_intent("阻断用户的后续操作").blocking is None
    assert parse_intent("must pass before release").blocking is True
    assert parse_intent("must pass with a score of 0.9").blocking is True
    assert parse_intent("must pass the transcript along").blocking is None
    assert parse_intent("必须通过评估，阈值 0.8").blocking is True
    assert parse_intent("必须通过 HR 系统核验").blocking is None


def test_metric_is_read_only_next_to_a_gate():
    assert parse_intent("blocking, metric: refusal rate, threshold 0.9").metric == "refusal rate"
    assert parse_intent("门禁，指标：通过率，阈值 0.9").metric == "通过率"
    assert parse_intent("gate on the pass rate ≥ 0.9").metric == "pass rate"
    assert parse_intent("Builtin.Refusal gates releases at 0.9").metric is None
    assert parse_intent("track the pass rate over time, informational").metric is None


def test_structured_members_win_over_the_prose():
    intent = resolve_intent({"text": "Builtin.Refusal is informational", "blocking": True,
                             "threshold": "90%"})
    assert (intent.blocking, intent.threshold, intent.source) == (True, 0.9, "stated")
    # an explicit False is kept even when the prose says "blocking"
    assert resolve_intent({"text": "a blocking gate", "blocking": False}).blocking is False
    # unset members fall back to the prose
    intent = resolve_intent({"text": "block publishing at threshold 1.0",
                             "evaluator_id": "hr-guardrails-judge"})
    assert (intent.blocking, intent.threshold, intent.source) == (True, 1.0, "parsed")
    assert intent.evaluator_id == "hr-guardrails-judge"


# ---------------------------------------------------------------------------
# the draft carries the gate (the WB-006 repro, EN and ZH)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "hr-guardrails-judge must block publishing at threshold 1.00",
    "hr_guardrails_judge 阻断发布，阈值 1.00",
])
def test_prose_gate_is_carried_onto_the_seed_judge(text):
    content = _content([text])
    draft = _draft(content)
    judge = _entry(draft, "hr-guardrails-judge")
    assert judge["blocking"] is True and judge["threshold"] == 1.0
    assert "gate from recommendation #1 (source: parsed): blocking · threshold 1" in judge["note"]
    rec = draft["recommendations"][0]
    assert rec["status"] == "mapped" and rec["mapped_to"] == ["hr-guardrails-judge"]
    assert rec["blocking"] is True and rec["threshold"] == 1.0
    assert rec["intent_source"] == "parsed" and "source: parsed" in rec["note"]
    plan, errors = _validate(draft, content)
    assert plan is not None, errors
    summary = plans.plan_summary(draft)
    assert summary["unresolved_recommendations"] == 0
    assert summary["unresolved_blocking_recommendations"] == 0


def test_prose_gate_on_a_builtin_creates_a_blocking_existing_entry():
    content = _content(["Builtin.Refusal 作为门禁，阈值 0.9，指标：通过率",
                        "Builtin.Helpfulness, informational"], seed=None)
    draft = _draft(content)
    refusal = _entry(draft, "Builtin_Refusal")
    assert refusal["blocking"] is True and refusal["threshold"] == 0.9
    assert refusal["metric"] == "通过率"
    helpful = _entry(draft, "Builtin_Helpfulness")
    assert helpful["blocking"] is False and helpful["threshold"] is None
    recs = draft["recommendations"]
    assert recs[0]["blocking"] is True and recs[0]["metric"] == "通过率"
    assert recs[1]["blocking"] is False and recs[1]["intent_source"] == "parsed"


def test_structured_entry_carries_blocking_threshold_and_metric():
    content = _content([{"text": "Refusal must hold on every red-team case",
                         "evaluator_id": "Builtin.Refusal", "blocking": True,
                         "threshold": 1.0, "metric": "pass rate"}], seed=None)
    draft = _draft(content)
    refusal = _entry(draft, "Builtin_Refusal")
    assert (refusal["blocking"], refusal["threshold"], refusal["metric"]) == \
        (True, 1.0, "pass rate")
    assert "(source: stated)" in refusal["note"]
    rec = draft["recommendations"][0]
    assert rec["status"] == "mapped" and rec["intent_source"] == "stated"
    assert rec["text"] == "Refusal must hold on every red-team case"


def test_structured_evaluator_id_resolves_a_seed_judge_by_key_or_name():
    for ref in ("hr-guardrails-judge", "hr_guardrails_judge", "HR-Guardrails-Judge"):
        draft = _draft(_content([{"text": "guardrails gate", "evaluator_id": ref,
                                  "blocking": True, "threshold": 1.0}]))
        rec = draft["recommendations"][0]
        assert rec["mapped_to"] == ["hr-guardrails-judge"], ref
        assert _entry(draft, "hr-guardrails-judge")["blocking"] is True


def test_mixed_strings_and_objects_each_keep_their_own_gate():
    content = _content([
        "hr-guardrails-judge must block publishing at threshold 1.00",
        {"text": "helpfulness trend", "evaluator_id": "Builtin.Helpfulness", "blocking": False},
        "Builtin.Harmfulness 阻断发布，阈值 0.95",
    ])
    draft = _draft(content)
    by_index = {r["index"]: r for r in draft["recommendations"]}
    assert by_index[0]["blocking"] is True and by_index[0]["threshold"] == 1.0
    assert by_index[1]["blocking"] is False and by_index[1]["intent_source"] == "stated"
    assert by_index[2]["blocking"] is True and by_index[2]["threshold"] == 0.95
    assert _entry(draft, "Builtin_Helpfulness")["blocking"] is False
    assert _entry(draft, "Builtin_Harmfulness")["blocking"] is True
    plan, errors = _validate(draft, content)
    assert plan is not None, errors


def test_a_bare_word_in_the_prose_never_maps_a_seed_evaluator():
    seed = json.loads(json.dumps(SEED))
    seed["evaluators"][0].update({"key": "refusal", "name": "refusal"})
    draft = _draft(_content(["the agent must handle every refusal politely"], seed=seed))
    assert draft["recommendations"][0]["status"] == "unresolved"


# ---------------------------------------------------------------------------
# a gate nobody carries: unresolved AND invalid, naming the recommendation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", [
    "the compliance judge must block publishing at threshold 1.0",
    "合规评审必须阻断发布，阈值 1.0",
    {"text": "compliance gate", "evaluator_id": "no-such-judge", "blocking": True,
     "threshold": 0.8},
])
def test_an_uncarried_gate_is_unresolved_and_makes_the_plan_invalid(entry):
    content = _content([entry])
    draft = _draft(content)
    rec = draft["recommendations"][0]
    assert rec["status"] == "unresolved" and rec["mapped_to"] == []
    assert rec["note"].startswith("UNRESOLVED gate: blocking · threshold")
    assert rec["blocking"] is True
    summary = plans.plan_summary(draft)
    assert summary["unresolved_recommendations"] == 1
    assert summary["unresolved_blocking_recommendations"] == 1
    plan, errors = _validate(draft, content)
    assert plan is None
    text = entry if isinstance(entry, str) else entry["text"]
    line = next(e for e in errors if e.startswith("recommendations[0]:"))
    assert text[:40] in line and "asks for a gate (blocking · threshold" in line
    assert "map it to an evaluator entry" in line and "decline it explicitly" in line
    if isinstance(entry, dict):
        assert "evaluator_id 'no-such-judge' is neither a listed evaluator nor a seed" \
            in rec["note"]


def test_declining_the_recommendation_clears_the_obligation():
    content = _content(["the compliance judge must block publishing at threshold 1.0"])
    draft = _draft(content)
    draft["recommendations"][0].update({"status": "declined", "mapped_to": []})
    plan, errors = _validate(draft, content)
    assert plan is not None, errors


def test_a_member_cannot_silently_downgrade_a_mapped_gate():
    content = _content(["hr-guardrails-judge must block publishing at threshold 1.00"])
    draft = _draft(content)
    judge = _entry(draft, "hr-guardrails-judge")
    judge["blocking"] = False
    judge["threshold"] = None
    plan, errors = _validate(draft, content)
    assert plan is None
    assert any("states a blocking gate but evaluator 'hr-guardrails-judge' is informational"
               in e for e in errors)
    assert any("states threshold 1 but evaluator 'hr-guardrails-judge' has none" in e
               for e in errors)


def test_the_proposal_time_draft_check_reports_an_uncarried_gate():
    """``validate`` runs ``draft_plan_errors`` on every recorded proposal, so the architect
    learns at proposal time that its blocking recommendation names no evaluator."""
    content = _content(["the compliance judge must block publishing at threshold 1.0"])
    errors = plans.draft_plan_errors(content, {})
    assert any("asks for a gate (blocking · threshold 1)" in e for e in errors), errors
    assert all(e.startswith("evaluation_plan (as drafted for review): ") for e in errors)


def test_a_seed_threshold_is_kept_and_blocking_is_or_ed_in():
    seed = json.loads(json.dumps(SEED))
    seed["evaluators"][0]["threshold"] = 0.95
    draft = _draft(_content(["hr-guardrails-judge must block publishing at threshold 1.00"],
                            seed=seed))
    judge = _entry(draft, "hr-guardrails-judge")
    assert judge["blocking"] is True and judge["threshold"] == 0.95


def test_recommendations_without_a_gate_keep_the_legacy_behaviour():
    content = _content(["Builtin.Helpfulness", "consider a latency dashboard later"], seed=None)
    draft = _draft(content)
    recs = {r["index"]: r for r in draft["recommendations"]}
    assert recs[0]["status"] == "mapped" and "blocking" not in recs[0]
    assert recs[1]["status"] == "unresolved" and "blocking" not in recs[1]
    assert recs[1]["note"] == "no exact evaluator identified — classify or decline"
    assert _entry(draft, "Builtin_Helpfulness")["blocking"] is False
    for s in draft["scenarios"]:
        s["review_required"] = False
    plan, errors = _validate(draft, content)
    assert plan is not None, errors  # an unresolved recommendation WITHOUT a gate is allowed
    assert plans.plan_summary(draft)["unresolved_blocking_recommendations"] == 0


def test_plans_stored_before_the_gate_fields_still_validate():
    content = _content(["Builtin.Helpfulness"], seed=None)
    draft = _draft(content)
    for s in draft["scenarios"]:
        s["review_required"] = False
    for r in draft["recommendations"]:
        for k in ("blocking", "threshold", "metric", "intent_source"):
            r.pop(k, None)
    for e in draft["evaluators"]:
        e.pop("metric", None)
    plan, errors = _validate(draft, content)
    assert plan is not None, errors
    assert plan.recommendations[0].blocking is None and plan.recommendations[0].threshold is None


def test_repaired_object_entries_draft_like_structured_ones():
    entry, error = normalise_entry({"name": "hr-guardrails-judge", "rationale": "block publishing",
                                    "suggested_threshold": "1.00", "owner": "HR ops"}, 0)
    assert error is None and entry["note"].startswith(REPAIR_NOTE_PREFIX)
    draft = _draft(_content([entry]))
    judge = _entry(draft, "hr-guardrails-judge")
    assert judge["blocking"] is True and judge["threshold"] == 1.0
