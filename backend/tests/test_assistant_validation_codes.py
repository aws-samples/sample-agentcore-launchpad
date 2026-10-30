"""N17-obs296 — validation errors carry a stable code + params next to the English
sentence, so the console renders them in the member's language. The classifier follows the
validator's own sentences (drift guard): the routing-rule messages are built through
``evaluation_plan.seed_errors`` and must keep their codes."""

from app.assistant import evaluation_plan as plan_contract
from app.assistant import service
from app.assistant import validation_codes as vc

REF_ERROR = ("evaluation_plan.evaluators.trajectory_rule: scenario 'GT-2' has no "
             "expected_trajectory — a reference-driven evaluator is applied to every "
             "session, so every scenario must carry it")




def _seed_errors(scenarios, evaluators):
    return plan_contract.seed_errors({"scenarios": scenarios, "evaluators": evaluators,
                                      "recommendation_keys": {}, "blocked_golden_tests": []})


def test_every_routing_rule_sentence_has_its_code():
    s1 = {"scenario_id": "a", "golden_test_id": "GT-1",
          "turns": [{"input": "q1", "expected_response": "r1"}, {"input": "q2"}]}
    s2 = {"scenario_id": "b", "golden_test_id": "GT-2", "turns": [{"input": "q"}],
          "expected_trajectory": ["kb_search"]}
    trajectory = {"kind": "existing", "key": "traj", "title": "T",
                  "evaluator_id": "Builtin.TrajectoryInOrderMatch", "golden_test_ids": []}
    trace_judge = {"kind": "judge", "key": "ref_judge", "title": "J", "name": "ref_judge",
                   "level": "TRACE", "golden_test_ids": [],
                   "instructions": "Compare {assistant_turn} with {expected_response}."}
    partial = {"kind": "existing", "key": "helpful", "title": "H",
               "evaluator_id": "Builtin.Helpfulness", "golden_test_ids": ["GT-1"]}
    errors = _seed_errors([s1, s2], [trajectory, trace_judge, partial])
    by_code: dict[str, list[dict]] = {}
    for d in vc.details(errors):
        by_code.setdefault(d["code"], []).append(d)
    assert set(by_code) == {vc.REFERENCE_MISSING, vc.TRACE_TURN_MISSING_RESPONSE,
                            vc.EVALUATOR_PARTIAL_ROUTING}, errors
    missing = {(d["params"]["evaluator"], d["params"]["scenario"], tuple(d["params"]["fields"]))
               for d in by_code[vc.REFERENCE_MISSING]}
    assert ("traj", "GT-1", ("expected_trajectory",)) in missing
    assert ("ref_judge", "GT-2", ("expected_response",)) in missing
    assert by_code[vc.TRACE_TURN_MISSING_RESPONSE][0]["params"] == {
        "evaluator": "ref_judge", "scenario": "GT-1"}
    assert by_code[vc.EVALUATOR_PARTIAL_ROUTING][0]["params"] == {
        "evaluator": "helpful", "golden_tests": ["GT-1"]}
    assert vc.autocorrectable(errors)
    # the draft prefix and the bare plan surface classify the same way
    bare = REF_ERROR.removeprefix("evaluation_plan.")
    for text in (bare, "evaluation_plan (as drafted for review): " + bare):
        assert vc.classify(text)["code"] == vc.REFERENCE_MISSING


def test_other_sentences_stay_generic_and_block_the_correction():
    assert vc.classify("name: must be a lowercase slug")["code"] == vc.GENERIC_CODE
    assert not vc.autocorrectable([])
    assert not vc.autocorrectable([REF_ERROR, "model_id: 'x' is not available"])
    note = service.rejection_note([REF_ERROR] * 2)
    assert vc.note_lines(note) == [REF_ERROR, REF_ERROR]


def test_payloads_carry_the_coded_errors():
    """``proposal_out`` / the transcript notice rows expose ``{code, params, message}``."""
    from types import SimpleNamespace

    row = SimpleNamespace(
        id="p", conversation_id="c", revision=1, source="model", status="invalid",
        content={}, content_hash="h", bindings=None, validation_errors=[REF_ERROR, "x: bad"],
        created_by="u", created_at=None, rejected_by=None, rejected_at=None, notes=[])
    out = service.proposal_out(None, row)
    assert out["validation_errors"] == [REF_ERROR, "x: bad"]
    assert [d["code"] for d in out["validation_error_details"]] == [vc.REFERENCE_MISSING,
                                                                   vc.GENERIC_CODE]
    assert out["validation_error_details"][1] == {"code": vc.GENERIC_CODE, "params": {},
                                                  "message": "x: bad"}


def test_partial_routing_is_anchored_on_the_whole_sentence():
    """rev-r27 #2: a sentence that only starts like the routing rule (here: followed by a
    policy clause) is NOT that rule, so it is generic and never auto-corrected."""
    probe = ("evaluation_plan.evaluators.gate: targets only ['GT-1'] but a dataset run "
             "applies every evaluator to every session; and model_id 'x' is not available")
    assert vc.classify(probe)["code"] == vc.GENERIC_CODE
    assert not vc.autocorrectable([probe])


def test_the_draft_path_sentence_from_validate_has_its_code():
    """rev-r27 #3: the real ``proposal.validate`` output on the draft path
    (``draft_plan_errors``: a Builtin trajectory id named in a recommendation drafts an
    evaluator that every scenario's missing trajectory fails)."""
    from copy import deepcopy

    from tests.test_assistant import VALID_PROPOSAL, _catalog

    p = deepcopy(VALID_PROPOSAL)
    p["tools"] = []
    p["evaluator_recommendations"] = ["Builtin.TrajectoryInOrderMatch checks the tool order"]
    errors = service.proposal_contract.validate(p, _catalog())[2]
    assert errors and all(e.startswith("evaluation_plan (as drafted for review): ")
                          for e in errors), errors
    details = vc.details(errors)
    assert [d["code"] for d in details] == [vc.REFERENCE_MISSING]
    assert details[0]["params"]["scenario"] == "GT-1"
    assert details[0]["params"]["fields"] == ["expected_trajectory"]
    assert vc.autocorrectable(errors)


def test_the_correction_prompt_lists_every_error():
    """rev-r27 #6: the member-facing note is capped; the internal prompt is not cut at
    the same 2400 characters (40 sentences of the live shape)."""
    errors = [REF_ERROR.replace("GT-2", f"gt-{i:02d}-a-long-scenario-name") for i in range(40)]
    prompt = service.autocorrect_prompt(1, errors)
    assert all(e in prompt for e in errors) and "more errors omitted" not in prompt
    assert "more errors omitted" in service.rejection_note(errors)
