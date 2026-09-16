# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""Repair turns exercise the real router/composer with a stubbed Harness and temp DB."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from app.assistant import evaluation_assets as assets
from app.assistant import evaluation_plan as plan_contract
from app.assistant import service
from app.core.db import SessionLocal
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantMessage,
    AssistantProposal,
    EvaluationAssetOperation,
)
from app.models.ledger import Deployment, Job, Workspace

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    BASE,
    PRESET_ARN,
    VALID_PROPOSAL,
    _block,
    _count,
    _latest,
    _open,
    _sse,
    _turn,
    gated,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

PROMPT = "Repair this saved evaluation plan and explain the changes."


@pytest.fixture(autouse=True)
def no_materialization(monkeypatch, no_real_deploy):
    def forbidden(*args, **kwargs):
        raise AssertionError("a repair turn must never materialize evaluation assets")

    monkeypatch.setattr(assets, "approve_plan", forbidden)
    monkeypatch.setattr(assets, "start_async", forbidden)
    yield
    assert no_real_deploy == []
    assert _count(EvaluationAssetOperation) == _count(Deployment) == _count(Job) == 0


def _edit_proposal(client, cid, content):
    response = client.put(
        f"{BASE}/conversations/{cid}/proposal", json={"content": content}
    )
    assert response.status_code == 200, response.text
    return response.json()["proposal"]


def _seed(client):
    """Use real preparation/validation; edits need not exist in the chat history."""
    cid = _open(client)
    source = _edit_proposal(client, cid, deepcopy(VALID_PROPOSAL))
    assert source["status"] == "draft"
    response = client.post(
        f"{BASE}/conversations/{cid}/evaluation-plan/prepare",
        json={"revision": source["revision"]},
    )
    assert response.status_code == 201, response.text
    raw = response.json()["plan"]["content"]
    raw["summary"] = "Saved member intent: retain the vacation-policy scenario."
    raw["evaluators"].append({
        "kind": "existing",
        "key": "trajectory-reference",
        "title": "Exact tool trajectory",
        "evaluator_id": "Builtin.TrajectoryExactOrderMatch",
        "golden_test_ids": ["GT-1"],
    })
    response = client.put(
        f"{BASE}/conversations/{cid}/evaluation-plan", json={"content": raw}
    )
    assert response.status_code == 200, response.text
    plan = response.json()["plan"]
    assert plan["status"] == "invalid"
    assert any("expected_trajectory" in error for error in plan["validation_errors"])
    assert _latest(client, cid)["messages"] == []
    return cid, source, plan


def _ref(plan):
    return {"plan_revision": plan["revision"], "plan_hash": plan["content_hash"]}


def _repair(client, cid, plan, **kwargs):
    return client.post(
        f"{BASE}/conversations/{cid}/turns",
        json={"prompt": PROMPT, "evaluation_plan_repair": _ref(plan)},
        **kwargs,
    )


def _assert_refused(response, status, code, client, cid, harness, before):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.text
    assert harness.calls == []
    assert _latest(client, cid) == before


def _fixed(content):
    fixed = deepcopy(content)
    fixed["evaluation_plan"] = {
        "scenarios": [{
            "scenario_id": "vacation-policy",
            "golden_test_id": "GT-1",
            "turns": [{"input": "How many vacation days?",
                       "expected_response": "cites the policy"}],
            "review_required": False,
        }],
        "evaluators": [],
        "recommendation_keys": {},
        "blocked_golden_tests": [],
    }
    return fixed


@pytest.mark.parametrize("newer_baseline", [False, True], ids=["same-source", "older-source"])
def test_repair_stream_gets_saved_context_and_persists_new_validatable_revision(
    client, ready, harness, newer_baseline
):
    cid, source, plan = _seed(client)
    baseline = source
    if newer_baseline:
        content = deepcopy(source["content"])
        content.update({
            "system_prompt": "Latest requirement: answer HR questions in Spanish.",
            "summary": "Latest configuration must survive evaluation repair.",
            "tools": ["mcp:deepwiki"],
            "skills": [],
            "knowledge_bases": [],
            "memory": "disabled",
            "timeout_seconds": 180,
        })
        baseline = _edit_proposal(client, cid, content)
        assert baseline["status"] == "draft"
    originals = {
        p["revision"]: (p["content_hash"], p["content"])
        for p in _latest(client, cid)["proposals"]
    }
    harness.reply(_block(_fixed(baseline["content"])), tools=("aws_knowledge",))
    response = _repair(client, cid, plan)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse(response)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert "tool" in kinds and "delta" in kinds and "error" not in kinds
    assert events[0][1]["omitted_turns"] == 0
    assert len(harness.calls) == 1
    call = harness.calls[0]
    assert call["harnessArn"] == PRESET_ARN
    assert [m["role"] for m in call["messages"]] == ["user"]
    text = call["messages"][0]["content"][0]["text"]
    assert text.startswith("# Launchpad assistant protocol")
    assert PROMPT in text
    snapshot = json.loads(text[text.rfind('\n{"latest_proposal":') + 1:])
    assert snapshot["invalid_plan"] == {
        "revision": plan["revision"],
        "content_hash": plan["content_hash"],
        "content": plan["content"],
        "validation_errors": plan["validation_errors"],
    }
    assert snapshot["latest_proposal"] == {
        "revision": baseline["revision"],
        "content_hash": baseline["content_hash"],
        "content": baseline["content"],
    }
    expected_source = {
        "revision": source["revision"], "content_hash": source["content_hash"]
    }
    if newer_baseline:
        expected_source["content"] = source["content"]
    assert snapshot["source_proposal"] == expected_source
    assert "Use latest_proposal as the baseline" in text
    assert "do not restore that proposal's old configuration" in text

    revised = next(data for kind, data in events if kind == "proposal")
    assert revised["revision"] == baseline["revision"] + 1
    assert revised["status"] == "draft" and revised["source"] == "model"
    assert revised["content_hash"] != baseline["content_hash"]
    for key in ("system_prompt", "tools", "skills", "knowledge_bases", "memory",
                "timeout_seconds", "summary"):
        assert revised["content"][key] == baseline["content"][key]
    detail = _latest(client, cid)
    assert detail["turns"] == 1 and detail["turn_in_progress"] is None
    assert len(detail["proposals"]) == len(originals) + 1
    persisted = next(p for p in detail["proposals"] if p["revision"] == revised["revision"])
    assert persisted["id"] == revised["id"]
    assert persisted["content"] == revised["content"]
    assert persisted["content_hash"] == revised["content_hash"]
    assert persisted["status"] == "draft" and persisted["source"] == "model"
    for proposal in detail["proposals"]:
        if proposal["revision"] in originals:
            assert (proposal["content_hash"], proposal["content"]) == originals[
                proposal["revision"]
            ]
    assert harness.streams[0].closed
    with SessionLocal() as db:
        user_message = db.query(AssistantMessage).filter_by(
            conversation_id=cid, role="user"
        ).one()
        assert plan["validation_errors"][0] in user_message.text
        old_plan = db.get(AssistantEvaluationPlan, plan["id"])
        assert old_plan.content == plan["content"]
        assert old_plan.validation_errors == plan["validation_errors"]
        assert old_plan.status == "invalid"
        assert db.query(AssistantEvaluationPlan).filter_by(conversation_id=cid).count() == 2
    response = client.post(
        f"{BASE}/conversations/{cid}/evaluation-plan/prepare",
        json={"revision": revised["revision"]},
    )
    assert response.status_code == 201, response.text
    prepared = response.json()["plan"]
    assert prepared["revision"] == plan["revision"] + 1
    assert prepared["source_revision"] == revised["revision"]
    assert prepared["source_content_hash"] == revised["content_hash"]
    assert prepared["status"] == "draft" and prepared["validation_errors"] == []


@pytest.mark.parametrize("bad_ref", [
    {},
    {"plan_revision": 1},
    {"plan_hash": "a" * 64},
    {"plan_revision": True, "plan_hash": "a" * 64},
    {"plan_revision": "1", "plan_hash": "a" * 64},
    {"plan_revision": 1.0, "plan_hash": "a" * 64},
    {"plan_revision": 0, "plan_hash": "a" * 64},
    {"plan_revision": -1, "plan_hash": "a" * 64},
    {"plan_revision": 1, "plan_hash": "a" * 63},
    {"plan_revision": 1, "plan_hash": "a" * 65},
    {"plan_revision": 1, "plan_hash": "g" * 64},
    {"plan_revision": 1, "plan_hash": "A" * 64},
    {"plan_revision": 1, "plan_hash": 123},
    {"plan_revision": 1, "plan_hash": "a" * 64, "content": {"injected": True}},
])
def test_malformed_repair_reference_is_rejected_before_turn(client, ready, harness, bad_ref):
    cid = _open(client)
    before = _latest(client, cid)
    response = client.post(
        f"{BASE}/conversations/{cid}/turns",
        json={"prompt": PROMPT, "evaluation_plan_repair": bad_ref},
    )
    assert response.status_code == 422, response.text
    assert harness.calls == [] and _latest(client, cid) == before


@pytest.mark.parametrize("prompt", [None, ""])
def test_repair_reference_does_not_replace_required_prompt(client, ready, harness, prompt):
    cid, _, plan = _seed(client)
    before = _latest(client, cid)
    body = {"evaluation_plan_repair": _ref(plan)}
    if prompt is not None:
        body["prompt"] = prompt
    response = client.post(f"{BASE}/conversations/{cid}/turns", json=body)
    assert response.status_code == 422, response.text
    assert harness.calls == [] and _latest(client, cid) == before


@pytest.mark.parametrize("mismatch", [
    "missing-plan", "old-plan", "hash", "source-id", "source-hash",
    "source-revision", "source-workspace", "plan-workspace",
])
def test_stale_repair_reference_has_no_turn_side_effects(
    client, ready, harness, mismatch
):
    cid, source, plan = _seed(client)
    with SessionLocal() as db:
        row = db.get(AssistantEvaluationPlan, plan["id"])
        if mismatch == "missing-plan":
            db.query(AssistantEvaluationPlan).filter_by(conversation_id=cid).delete()
        elif mismatch == "old-plan":
            assets.prepare_plan(
                db, db.get(AssistantConversation, cid),
                revision=source["revision"], created_by="river",
            )
        elif mismatch == "hash":
            plan["content_hash"] = "0" * 64
        elif mismatch == "source-id":
            row.proposal_id = "missing-source"
        elif mismatch == "source-hash":
            row.source_content_hash = "0" * 64
        elif mismatch == "source-revision":
            row.source_revision = 999
        elif mismatch == "source-workspace":
            db.get(AssistantProposal, source["id"]).workspace_id = "another-workspace"
        elif mismatch == "plan-workspace":
            row.workspace_id = "another-workspace"
        db.commit()
    before = _latest(client, cid)
    _assert_refused(
        _repair(client, cid, plan), 409, "assistant.evaluation_repair_stale",
        client, cid, harness, before,
    )


@pytest.mark.parametrize("status,errors", [
    ("draft", ["still has findings"]),
    ("approved", ["still has findings"]),
    ("superseded", ["still has findings"]),
    ("invalid", []),
])
def test_only_invalid_plan_with_findings_can_be_repaired(
    client, ready, harness, status, errors
):
    cid, _, plan = _seed(client)
    with SessionLocal() as db:
        row = db.get(AssistantEvaluationPlan, plan["id"])
        row.status, row.validation_errors = status, errors
        db.commit()
    before = _latest(client, cid)
    _assert_refused(
        _repair(client, cid, plan), 409, "assistant.evaluation_repair_not_needed",
        client, cid, harness, before,
    )


def test_invalid_latest_proposal_is_not_replaced_with_older_valid_source(client, ready, harness):
    cid, _, plan = _seed(client)
    invalid = _edit_proposal(client, cid, {"unexpected": True})
    assert invalid["status"] == "invalid"
    before = _latest(client, cid)
    _assert_refused(
        _repair(client, cid, plan), 409, "assistant.evaluation_plan_source_invalid",
        client, cid, harness, before,
    )


def test_repair_conversation_and_plan_are_owner_and_workspace_scoped(gated, harness):
    admin, member, other, _, _ = gated
    cid, _, plan = _seed(member)
    before = _latest(member, cid)
    for outsider in (admin, other):
        _assert_refused(
            _repair(outsider, cid, plan), 404, "assistant.conversation_not_found",
            member, cid, harness, before,
        )
    own_cid = _open(member)
    own_before = _latest(member, own_cid)
    _assert_refused(
        _repair(member, own_cid, plan), 409, "assistant.evaluation_repair_stale",
        member, own_cid, harness, own_before,
    )
    with SessionLocal() as db:
        db.add(Workspace(
            id="other-lab", name="other-lab", account_id="222233334444",
            region="us-east-2", bootstrap_status="ready", resources={},
        ))
        db.commit()
    # The same owner has access to both workspaces, so only workspace scoping refuses this.
    admin_cid, _, admin_plan = _seed(admin)
    admin_before = _latest(admin, admin_cid)
    _assert_refused(
        _repair(admin, admin_cid, admin_plan, headers={"X-Workspace": "other-lab"}),
        404, "assistant.conversation_not_found", admin, admin_cid, harness, admin_before,
    )


@pytest.mark.parametrize("budget", ["characters", "utf8-bytes", "request-with-catalog"])
def test_full_repair_context_over_ordinary_budget_is_refused_without_truncation(
    client, ready, harness, budget
):
    cid, _, plan = _seed(client)
    with SessionLocal() as db:
        row = db.get(AssistantEvaluationPlan, plan["id"])
        content = deepcopy(row.content)
        if budget == "characters":
            content["summary"] = "x" * service.MAX_PROMPT_CHARS
        elif budget == "utf8-bytes":
            content["summary"] = "😀" * (service.MAX_PROMPT_BYTES // 4 + 1)
        else:
            content["summary"] = "x" * 70_000
            conversation = db.get(AssistantConversation, cid)
            catalog = deepcopy(conversation.catalog)
            catalog["tools"][0]["description"] = "c" * 90_000
            conversation.catalog = catalog
        content["summary"] += " END-OF-SAVED-CONTEXT"
        row.content = content
        row.content_hash = plan_contract.canonical_hash(content)
        plan["content_hash"] = row.content_hash
        db.commit()
        # The member's ordinary message fits; the authoritative repair snapshot does not.
        service.check_prompt(db.get(AssistantConversation, cid), PROMPT)
    before = _latest(client, cid)
    _assert_refused(
        _repair(client, cid, plan), 413, "assistant.evaluation_repair_too_large",
        client, cid, harness, before,
    )
    with SessionLocal() as db:
        assert db.query(AssistantMessage).filter_by(conversation_id=cid).count() == 0
        assert db.get(AssistantEvaluationPlan, plan["id"]).content == content
        assert db.get(AssistantConversation, cid).active_turn_token is None


@pytest.mark.parametrize("explicit_null", [False, True])
def test_ordinary_turn_ignores_invalid_plan_without_a_repair_reference(
    client, ready, harness, explicit_null
):
    cid, _, _ = _seed(client)
    body = {"prompt": "Continue the requirements discussion."}
    if explicit_null:
        body["evaluation_plan_repair"] = None
    harness.reply("Which policy applies?")
    response = client.post(f"{BASE}/conversations/{cid}/turns", json=body)
    assert response.status_code == 200, response.text
    events = _sse(response)
    assert events[0][0] == "meta" and events[-1][0] == "done"
    text = harness.calls[-1]["messages"][-1]["content"][0]["text"]
    assert text.endswith(body["prompt"]) and '"invalid_plan":' not in text
    assert _latest(client, cid)["turns"] == 1


def test_repair_and_ordinary_turns_share_the_same_concurrent_turn_claim(client, ready, harness):
    cid, _, plan = _seed(client)
    harness.reply("Please confirm the expected policy response.")
    entered, release = threading.Event(), threading.Event()

    def hold(_kwargs):
        entered.set()
        assert release.wait(timeout=10)

    harness.on_invoke = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_repair, client, cid, plan)
        try:
            assert entered.wait(timeout=10)
            during = _latest(client, cid)
            assert during["turn_in_progress"] == 1
            second = _repair(client, cid, plan)
            ordinary = client.post(
                f"{BASE}/conversations/{cid}/turns", json={"prompt": "Another turn"}
            )
            for response in (second, ordinary):
                assert response.status_code == 409, response.text
                assert response.json()["code"] == "assistant.turn_in_progress"
            assert _latest(client, cid) == during
            assert len(harness.calls) == 1
        finally:
            release.set()
        response = first.result(timeout=10)
    assert response.status_code == 200 and _sse(response)[-1][0] == "done"
    detail = _latest(client, cid)
    assert detail["turns"] == 1 and detail["turn_in_progress"] is None
    assert len(detail["proposals"]) == 1
    assert harness.streams[0].closed
    harness.on_invoke = None
    assert _turn(client, cid, "Now continue normally.")[-1][0] == "done"
