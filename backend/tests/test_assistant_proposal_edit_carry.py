# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""N15-obs13 — a member edit that omits ``evaluation_plan`` must not strand the gates.

Live on build c9a4c91d (conversation 3bfe7e04, 2026-10-19): the console's 编辑 rebuilt
the content from the editable draft and dropped the model's ``evaluation_plan``, so
revision 5 drafted a review plan with no seed evaluators and the blocking
``hr_guardrails_judge`` recommendation became an unresolvable gate. The console now
carries every unedited key (vitest); this is the server-side defence for an older client
or a hand-written edit, and the exact semantics of the submitted key."""

import json
from copy import deepcopy

from app.assistant import evaluation_plan as plans
from app.assistant.service import EVALUATION_PLAN_CARRIED_NOTE, carry_evaluation_plan
from app.models.assistant import AssistantProposal

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    BASE,
    VALID_PROPOSAL,
    _approve,
    _latest,
    _open,
    _propose,
    gated,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

SEED = {
    "evaluators": [{
        "kind": "judge", "key": "hr-guardrails-judge", "title": "HR guardrails judge",
        "name": "hr_guardrails_judge", "level": "TRACE",
        "instructions": "Judge {context} against the HR guardrails", "golden_test_ids": ["GT-1"],
    }],
    "scenarios": [{
        "scenario_id": "GT-1", "golden_test_id": "GT-1",
        "turns": [{"input": "How many vacation days?"}], "assertions": ["cites the policy"],
    }],
    "recommendation_keys": {"0": ["hr-guardrails-judge"]},
}
SEEDED = {
    **VALID_PROPOSAL,
    "evaluator_recommendations": [
        {"text": "hr_guardrails_judge must block publishing at threshold 1.00",
         "evaluator_id": "hr_guardrails_judge", "blocking": True, "threshold": 1.0},
    ],
    "evaluation_plan": SEED,
}


def _edit(client, cid, content):
    res = client.put(f"{BASE}/conversations/{cid}/proposal", json={"content": content})
    assert res.status_code == 200, res.text
    return res.json()["proposal"]


def _without_seed(content):
    return {k: v for k, v in content.items() if k != "evaluation_plan"}


def test_an_edit_that_omits_the_seed_carries_it_from_the_previous_revision(client, ready, harness):
    cid = _open(client)
    r1 = _propose(client, harness, cid, SEEDED)
    assert r1["status"] == "draft" and r1["content"]["evaluation_plan"]["evaluators"]
    # the symptom: an older console rebuilt the content without `evaluation_plan`
    r2 = _edit(client, cid, {**_without_seed(SEEDED), "name": "hr-helpdesk-v2"})
    assert r2["revision"] == 2 and r2["status"] == "draft" and r2["source"] == "member"
    assert r2["content"]["name"] == "hr-helpdesk-v2"
    assert r2["content"]["evaluation_plan"] == r1["content"]["evaluation_plan"]
    assert r2["notes"] == [EVALUATION_PLAN_CARRIED_NOTE.format(revision=1)]
    assert r2["notes"][0].startswith("proposal_repaired: evaluation_plan carried from revision 1")
    assert r2["validation_errors"] == []
    # the plan drafted from the member revision still seeds the judge the blocking
    # recommendation names — the gate is resolvable again
    draft = plans.draft_plan(r2["content"], revision=2, content_hash=r2["content_hash"],
                             agent_name="hr-helpdesk-v2")
    judge = next(e for e in draft["evaluators"] if e["key"] == "hr-guardrails-judge")
    assert judge["blocking"] is True and judge["threshold"] == 1.0
    _plan, errors = plans.validate_plan(draft, r2["content"], revision=2,
                                        content_hash=r2["content_hash"])
    assert not any("gate" in e and "no evaluator" in e for e in errors), errors
    # the note is on the revision only; the detail lists it and the model revision has none
    detail = _latest(client, cid)["proposals"]
    assert [p["notes"] for p in detail] == [[], r2["notes"]]
    # the carried revision approves like any other draft
    assert _approve(client, cid, r2).status_code == 202


def test_an_explicit_null_removes_the_seed_and_an_edited_seed_is_stored_as_given(
    client, ready, harness,
):
    cid = _open(client)
    _propose(client, harness, cid, SEEDED)
    # explicit removal: respected, no note. (The member who drops the seed also drops
    # the blocking recommendation that named its judge — with it kept, the revision
    # would be the WB-006 "asks for a gate that no evaluator carries" invalid revision,
    # i.e. the contract still holds the member to what they submitted.)
    informational = {**SEEDED, "evaluator_recommendations": ["Builtin.Helpfulness"]}
    r2 = _edit(client, cid, {**informational, "evaluation_plan": None})
    assert r2["status"] == "draft", r2["validation_errors"]
    assert "evaluation_plan" not in r2["content"] and r2["notes"] == []
    # the previous revision (2) has no seed now → an omission carries nothing, no note
    r3 = _edit(client, cid, {**_without_seed(informational), "name": "hr-helpdesk-v3"})
    assert r3["status"] == "draft", r3["validation_errors"]
    assert "evaluation_plan" not in r3["content"] and r3["notes"] == []
    # an edited seed is stored exactly as submitted, never merged with an older one
    edited = deepcopy(SEED)
    edited["evaluators"][0]["title"] = "HR guardrails judge (strict)"
    edited["recommendation_keys"] = {}
    r4 = _edit(client, cid, {**SEEDED, "evaluation_plan": edited})
    assert r4["status"] == "draft" and r4["notes"] == []
    stored = r4["content"]["evaluation_plan"]
    assert stored["evaluators"][0]["title"] == "HR guardrails judge (strict)"
    assert stored["recommendation_keys"] == {}


def test_a_carried_seed_is_validated_against_the_edited_content(client, ready, harness):
    """The carry is verbatim, never a repair of the seed itself: an edit that drops the
    golden test the carried scenario names is an *invalid* revision with the seed's cross
    errors — shown, never corrected, and still noted as carried."""
    cid = _open(client)
    _propose(client, harness, cid, SEEDED)
    r2 = _edit(client, cid, {**_without_seed(SEEDED), "golden_tests": []})
    assert r2["status"] == "invalid"
    assert any("evaluation_plan.scenarios" in e and "GT-1" in e for e in r2["validation_errors"])
    assert r2["notes"] == [EVALUATION_PLAN_CARRIED_NOTE.format(revision=1)]


def test_carry_evaluation_plan_semantics_are_pure():
    prev = AssistantProposal(revision=4, content={"name": "x", "evaluation_plan": SEED})
    content, notes = carry_evaluation_plan({"name": "y"}, prev)
    assert content["evaluation_plan"] == SEED and content["evaluation_plan"] is not SEED
    assert notes == [EVALUATION_PLAN_CARRIED_NOTE.format(revision=4)]
    for submitted in ({"name": "y", "evaluation_plan": None},
                      {"name": "y", "evaluation_plan": {"evaluators": []}}):
        assert carry_evaluation_plan(submitted, prev) == (submitted, [])
    assert carry_evaluation_plan({"name": "y"}, None) == ({"name": "y"}, [])
    no_seed = AssistantProposal(revision=4, content={"name": "x"})
    assert carry_evaluation_plan({"name": "y"}, no_seed) == ({"name": "y"}, [])
    invalid_prev = AssistantProposal(revision=4, content={"_rejected": "too large"})
    assert carry_evaluation_plan({"name": "y"}, invalid_prev) == ({"name": "y"}, [])
    raw = json.dumps({"name": "y"})  # a non-object payload is left to the validator
    assert carry_evaluation_plan(raw, prev) == (raw, [])
