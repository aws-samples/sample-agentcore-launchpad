# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""A proposal is checked against its evaluation draft when it is recorded, and later
revisions are written as changes to the stored proposal instead of a second copy."""

import json

import pytest

from app.assistant import proposal as contract
from app.assistant import service
from app.core.db import SessionLocal
from app.models.assistant import AssistantConversation, AssistantProposal

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    CATALOG,
    VALID_PROPOSAL,
    _block,
    _latest,
    _open,
    _turn,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

# Seed passes its own checks, but GT-2 has no typed scenario: the platform drafts one
# without a trajectory, and the trajectory evaluator then fails the drafted plan.
DRAFT_GAP = {
    **VALID_PROPOSAL,
    "tools": [], "skills": [], "knowledge_bases": [], "memory": "disabled",
    "native_tools": ["shell"],
    "golden_tests": [
        {"id": "GT-1", "input": "a", "expected_response": "x", "expected_tools": ["shell"],
         "source": "customer_pain_point"},
        {"id": "GT-2", "input": "b", "expected_response": "y", "source": "customer_pain_point"},
    ],
    "evaluator_recommendations": [],
    "evaluation_plan": {
        "evaluators": [{"kind": "existing", "key": "traj", "title": "trajectory",
                        "evaluator_id": "Builtin.TrajectoryInOrderMatch",
                        "golden_test_ids": []}],
        "scenarios": [{"scenario_id": "s1", "golden_test_id": "GT-1", "turns": [{"input": "a"}],
                       "expected_trajectory": ["shell"], "assertions": ["must answer"]}],
        "recommendation_keys": {}, "blocked_golden_tests": [],
    },
}


def _patch(obj) -> str:
    body = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return f"```{contract.PATCH_FENCE}\n{body}\n```"


def _proposal_event(events) -> dict:
    return next(d for k, d in events if k == "proposal")


# ---------------------------------------------------------------------------
# C: the evaluation draft is validated when the proposal is recorded
# ---------------------------------------------------------------------------


def test_recording_reports_what_preparing_the_plan_would_report():
    content, _display, errors = contract.validate(DRAFT_GAP, CATALOG)
    assert content is None
    assert errors == [
        "evaluation_plan (as drafted for review): evaluators.traj: scenario 'GT-2' has no "
        "expected_trajectory — a reference-driven evaluator is applied to every session, so "
        "every scenario must carry it"
    ]
    blocked = json.loads(json.dumps(DRAFT_GAP))
    blocked["evaluation_plan"]["blocked_golden_tests"] = [
        {"golden_test_id": "GT-2", "reason": "scored manually"}]
    assert contract.validate(blocked, CATALOG)[2] == []


def test_recording_checks_trajectories_against_the_runtime_catalog():
    wrong = json.loads(json.dumps(DRAFT_GAP))
    wrong["evaluation_plan"]["blocked_golden_tests"] = [
        {"golden_test_id": "GT-2", "reason": "scored manually"}]
    wrong["evaluation_plan"]["scenarios"][0]["expected_trajectory"] = ["not_a_tool"]
    errors = contract.validate(wrong, CATALOG)[2]
    assert len(errors) == 1 and "not in the selected runtime catalog" in errors[0]


def test_review_the_member_owes_a_drafted_scenario_is_not_a_proposal_error():
    # VALID_PROPOSAL has no seed at all: every golden test becomes a drafted scenario
    # that needs review, which is the member's step, not a defect of the proposal
    assert contract.validate(VALID_PROPOSAL, CATALOG)[2] == []


# ---------------------------------------------------------------------------
# A: the change contract
# ---------------------------------------------------------------------------


def test_extract_patch_is_independent_of_the_full_block():
    text = _block(VALID_PROPOSAL)
    assert contract.extract_patch(text) == (None, [])
    patch = _patch({"base_revision": 1, "operations": []})
    assert contract.extract_block(patch) == (None, [])
    body, errors = contract.extract_patch("see\n" + patch)
    assert errors == [] and body is not None and json.loads(body)["base_revision"] == 1
    assert contract.extract_patch(patch + "\n" + patch)[1]


def test_apply_patch_supports_the_rfc6902_subset_and_never_mutates_the_base():
    base = json.loads(json.dumps(VALID_PROPOSAL))
    ops = [
        {"op": "test", "path": "/name", "value": "hr-helpdesk"},
        {"op": "replace", "path": "/system_prompt", "value": "New prompt."},
        {"op": "add", "path": "/assumptions/-", "value": "added last"},
        {"op": "add", "path": "/assumptions/0", "value": "added first"},
        {"op": "remove", "path": "/manual_tasks/0"},
        {"op": "add", "path": "/golden_tests/0/expected_tools", "value": ["lookup"]},
        {"op": "remove", "path": "/max_iterations"},
    ]
    doc, errors = contract.apply_patch(base, {"base_revision": 3, "operations": ops},
                                       base_revision=3)
    assert errors == [] and doc is not None
    assert doc["system_prompt"] == "New prompt."
    assert doc["assumptions"] == ["added first", "policies are in the KB", "added last"]
    assert doc["manual_tasks"] == [] and "max_iterations" not in doc
    assert doc["golden_tests"][0]["expected_tools"] == ["lookup"]
    assert base == VALID_PROPOSAL


@pytest.mark.parametrize("patch, needle", [
    ({"base_revision": 2, "operations": [{"op": "remove", "path": "/summary"}]},
     "targets revision 2"),
    ({"base_revision": 3, "operations": []}, "no operations"),
    ({"base_revision": 3, "operations": [{"op": "move", "path": "/a", "from": "/b"}]},
     "add, remove, replace or test"),
    ({"base_revision": 3, "operations": [{"op": "replace", "path": "", "value": {}}]},
     "inside the proposal"),
    ({"base_revision": 3, "operations": [{"op": "replace", "path": "/nope", "value": 1}]},
     "does not exist"),
    ({"base_revision": 3, "operations": [{"op": "remove", "path": "/assumptions/5"}]},
     "out of range"),
    ({"base_revision": 3, "operations": [{"op": "test", "path": "/name", "value": "x"}]},
     "differs"),
    ({"base_revision": 3, "operations": [{"op": "replace", "path": "/name"}]},
     "takes exactly"),
    ({"base_revision": 3, "operations": [{"op": "remove", "path": "/name"}], "x": 1},
     "base_revision and operations only"),
    ("{not json", "not valid JSON"),
])
def test_apply_patch_refuses_the_whole_change_on_any_failure(patch, needle):
    doc, errors = contract.apply_patch(VALID_PROPOSAL, patch, base_revision=3)
    assert doc is None and len(errors) == 1 and needle in errors[0], errors
    assert "patch" not in errors[0].lower()  # the member reads these lines


def test_apply_patch_bounds_the_operation_count():
    ops = [{"op": "test", "path": "/name", "value": "hr-helpdesk"}] * (
        contract.PATCH_MAX_OPERATIONS + 1)
    assert "max" in contract.apply_patch(
        VALID_PROPOSAL, {"base_revision": 1, "operations": ops}, base_revision=1)[1][0]


# ---------------------------------------------------------------------------
# A: turns
# ---------------------------------------------------------------------------


def test_a_change_revises_the_stored_proposal_and_the_replay_carries_it_once(
    client, ready, harness
):
    cid = _open(client)
    harness.reply("Here is the design.\n" + _block(VALID_PROPOSAL))
    first = _proposal_event(_turn(client, cid, "propose"))
    assert first["status"] == "draft"

    harness.reply("I tightened the prompt.\n" + _patch({
        "base_revision": first["revision"],
        "operations": [{"op": "replace", "path": "/system_prompt",
                        "value": "You answer HR policy questions and cite the policy."}],
    }))
    events = _turn(client, cid, "tighten the prompt")
    second = _proposal_event(events)
    assert second["status"] == "draft" and second["source"] == "model"
    assert second["revision"] == first["revision"] + 1
    assert second["content"]["system_prompt"].endswith("cite the policy.")
    assert {k: v for k, v in second["content"].items() if k != "system_prompt"} == {
        k: v for k, v in first["content"].items() if k != "system_prompt"}
    assert not [m for m in _latest(client, cid)["messages"] if m["role"] == "error"]

    # the second call's preamble held the stored JSON of revision 1 exactly once, and
    # the replayed first reply no longer carried its block
    sent = "\n".join(m["content"][0]["text"] for m in harness.calls[1]["messages"])
    stored = json.dumps(first["content"], ensure_ascii=False, separators=(",", ":"))
    assert f"## Current stored proposal — revision {first['revision']} (draft)" in sent
    assert sent.count(stored) == 1
    assert f"```{contract.PROPOSAL_FENCE}\n" not in sent
    assert service.REPLAY_PROPOSAL_MARKER in sent
    assert "never mention patches" in " ".join(sent.split())


def test_a_change_corrects_an_invalid_revision_in_place(client, ready, harness):
    cid = _open(client)
    harness.reply(_block({**VALID_PROPOSAL, "name": "Bad Name!"}))
    bad = _proposal_event(_turn(client, cid, "propose"))
    assert bad["status"] == "invalid"

    harness.reply("Fixed the name.\n" + _patch({
        "base_revision": bad["revision"],
        "operations": [{"op": "replace", "path": "/name", "value": "hr-helpdesk"}],
    }))
    fixed = _proposal_event(_turn(client, cid, "please fix it"))
    assert fixed["status"] == "draft"
    assert fixed["content"]["name"] == "hr-helpdesk"
    # the invalid revision was the base and its errors were shown with it
    sent = harness.calls[1]["messages"][-1]["content"][0]["text"]
    assert f"revision {bad['revision']} (invalid)" in harness.calls[1]["messages"][0][
        "content"][0]["text"]
    assert sent.startswith("Launchpad rejected the proposal block")


def test_a_change_that_does_not_apply_keeps_the_previous_base(client, ready, harness):
    cid = _open(client)
    harness.reply(_block(VALID_PROPOSAL))
    first = _proposal_event(_turn(client, cid, "propose"))

    harness.reply(_patch({"base_revision": first["revision"],
                          "operations": [{"op": "remove", "path": "/no_such_member"}]}))
    failed = _proposal_event(_turn(client, cid, "change it"))
    assert failed["status"] == "invalid"
    assert failed["validation_errors"] == ["change 1 (remove /no_such_member): path does not "
                                           "exist"]
    rows = [(m["role"], m["name"]) for m in _latest(client, cid)["messages"]]
    assert rows[-1] == ("error", service.PROPOSAL_REJECTED_NAME)
    with SessionLocal() as db:
        base = service.patch_base(db, cid)
        assert base is not None and base.revision == first["revision"]

    harness.reply(_patch({"base_revision": first["revision"],
                          "operations": [{"op": "replace", "path": "/summary",
                                          "value": "Second try."}]}))
    retried = _proposal_event(_turn(client, cid, "try again"))
    assert retried["status"] == "draft" and retried["content"]["summary"] == "Second try."


@pytest.mark.parametrize("reply, needle", [
    (_block(VALID_PROPOSAL) + "\n" + _patch({"base_revision": 1, "operations": []}),
     "send exactly one"),
    (_patch({"base_revision": 1, "operations": [{"op": "remove", "path": "/summary"}]}),
     "no stored proposal to change yet"),
])
def test_unusable_replies_become_an_invalid_marker_revision(
    client, ready, harness, reply, needle
):
    cid = _open(client)
    harness.reply(reply)
    revision = _proposal_event(_turn(client, cid, "propose"))
    assert revision["status"] == "invalid"
    assert len(revision["validation_errors"]) == 1
    assert needle in revision["validation_errors"][0]
    with SessionLocal() as db:
        row = db.query(AssistantProposal).filter_by(conversation_id=cid).one()
        assert not contract.is_patch_base(row.content)
        assert service.patch_base(db, cid) is None
        conversation = db.get(AssistantConversation, cid)
        assert conversation is not None
        assert "## Current stored proposal" not in service._preamble(conversation)
