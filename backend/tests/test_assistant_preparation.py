# ruff: noqa: F811 — shared pytest fixtures are imported explicitly
"""Preparation uses real API/ledger/proposal paths and hermetic Harness/S3 doubles."""

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from app.assistant import preparation, service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.assistant import AssistantConversation, AssistantProposal
from app.models.ledger import Agent, Job, Workspace
from app.services.workspace import WorkspaceContext, workspace_context

from .test_agent_skills_attach import _zip
from .test_assistant import (  # noqa: F401 — fixtures include hermetic guards
    BASE,
    MEMBER_CREDS,
    VALID_PROPOSAL,
    _approve,
    _block,
    _catalog,
    _count,
    _latest,
    _open,
    _propose,
    _set_permissions,
    _turn,
    gated,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

NEED = {"id": "policy", "kind": "knowledge_base", "title": "HR policies",
        "reason": "Ground answers in approved policy", "materials": ["Employee handbook"],
        "required": True}


def _requirements(requirements=None):
    return "```launchpad-preparation\n" + json.dumps(
        {"requirements": requirements if requirements is not None else [NEED]},
    ) + "\n```"


def _save(client, cid, *, kbs=(), skills=(), tools=None, revision=None, **kwargs):
    if revision is None:
        revision = _latest(client, cid)["preparation"]["revision"]
    return client.put(f"{BASE}/conversations/{cid}/preparation",
                      json={"expected_revision": revision,
                            "knowledge_bases": list(kbs), "skills": list(skills),
                            **({"tools": list(tools)} if tools is not None else {})}, **kwargs)


def _import(client, cid, sid, indexes=(0,), *, revision=None, **kwargs):
    if revision is None:
        revision = _latest(client, cid)["preparation"]["revision"]
    return client.post(
        f"{BASE}/conversations/{cid}/preparation/skills",
        json={"expected_revision": revision, "staging_id": sid,
              "selections": [{"index": i} for i in indexes]}, **kwargs,
    )


def _inspect(client, *, headers=None):
    res = client.post("/api/registry/skills/inspect",
                      files={"file": ("skill.zip", _zip(), "application/zip")},
                      headers=headers or {})
    assert res.status_code == 200, res.text
    return res.json()["staging_id"]


class S3:
    def __init__(self):
        self.files = {}
        self.uploaded = []
        self.fail_on = None
        self.on_upload = None

    def upload_file(self, src, bucket, key):
        if self.on_upload:
            self.on_upload()
        if self.fail_on and key.endswith(self.fail_on):
            raise RuntimeError("upload failed")
        self.uploaded.append(key)
        self.files[(bucket, key)] = Path(src).read_bytes()

    def delete_object(self, Bucket, Key):
        self.files.pop((Bucket, Key), None)

    def list_objects_v2(self, Bucket, Prefix):
        return {"Contents": [{"Key": k} for (b, k) in self.files if b == Bucket
                             and k.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.files[(Bucket, Key)])}


@pytest.fixture
def s3(monkeypatch):
    fake = S3()
    monkeypatch.setattr(WorkspaceContext, "client", lambda *a, **k: fake)
    return fake


def test_empty_and_legacy_projection_preserve_proposal_hash(client, ready, harness):
    cid = _open(client)
    assert _latest(client, cid)["preparation"] == {
        "revision": 0, "knowledge_bases": [], "skills": [], "tools": [], "requirements": [],
    }
    proposal = _propose(client, harness, cid)
    with SessionLocal() as db:
        row = db.get(AssistantConversation, cid)
        row.preparation = {}  # additive migration's default
        db.commit()
    detail = _latest(client, cid)
    assert detail["preparation"]["knowledge_bases"] == proposal["content"]["knowledge_bases"]
    assert detail["preparation"]["skills"] == proposal["content"]["skills"]
    assert detail["preparation"]["tools"] == proposal["content"]["tools"]
    assert detail["proposals"][0]["content_hash"] == proposal["content_hash"]
    assert "preparation_sources" not in detail
    with SessionLocal() as db:
        context = preparation.context(db.get(AssistantConversation, cid))
        assert '"skills": ["meeting-summarizer"]' in context
    # A newer invalid proposal does not clear a legacy conversation's valid selections.
    invalid = _propose(client, harness, cid, {"name": "bad"})
    assert invalid["status"] == "invalid"
    assert _latest(client, cid)["preparation"]["skills"] == proposal["content"]["skills"]


def test_selection_creates_member_revision_before_approval(client, ready, harness):
    cid = _open(client)
    original = _propose(client, harness, cid)
    result = _save(client, cid, kbs=["KB123ABC"])
    assert result.status_code == 200, result.text
    detail = result.json()
    assert detail["preparation"]["skills"] == []
    first, latest = detail["proposals"]
    assert first["status"] == "superseded"
    assert first["content_hash"] == original["content_hash"]
    assert latest["revision"] == first["revision"] + 1
    assert latest["status"] == "draft" and latest["source"] == "member"
    assert latest["content"]["skills"] == []
    assert latest["content"]["system_prompt"] == original["content"]["system_prompt"]
    assert _count(Job) == 0  # selection never starts a deployment


def test_model_preserves_explicit_selection_and_member_edit_updates_it(client, ready, harness):
    cid = _open(client)
    result = _save(client, cid, kbs=["KB123ABC"], skills=["meeting-summarizer"])
    assert result.status_code == 200
    raw = {**VALID_PROPOSAL, "knowledge_bases": [], "skills": []}
    proposed = _propose(client, harness, cid, raw)
    assert proposed["content"]["skills"] == ["meeting-summarizer"]
    assert proposed["content"]["knowledge_bases"] == ["KB123ABC"]
    context = harness.calls[-1]["messages"][0]["content"][0]["text"]
    assert "Member-selected preparation resources" in context
    assert '"skills": ["meeting-summarizer"]' in context
    edited = client.put(f"{BASE}/conversations/{cid}/proposal", json={"content": raw})
    assert edited.status_code == 200
    assert _latest(client, cid)["preparation"]["skills"] == []
    assert _propose(client, harness, cid)["content"]["skills"] == []


@pytest.mark.parametrize("mutate", [
    {"expected_revision": True}, {"expected_revision": -1},
    {"knowledge_bases": ["KB123ABC", "KB123ABC"]},
    {"tools": ["mcp:deepwiki", "mcp:deepwiki"]},
    {"tools": [f"mcp:server-{i}" for i in range(21)]},
    {"skills": ["s"] * 11}, {"sources": [{"path": "s3://private/arbitrary/"}]},
])
def test_selection_request_rejects_unsafe_or_unbounded_shape(client, ready, mutate):
    cid = _open(client)
    res = client.put(f"{BASE}/conversations/{cid}/preparation",
                     json={"expected_revision": 0, "knowledge_bases": [], "skills": [], **mutate})
    assert res.status_code == 422
    assert _latest(client, cid)["preparation"]["revision"] == 0


def test_mcp_selection_is_authoritative_and_deploys_only_selected_attachments(
    client, ready, harness,
):
    cid = _open(client)
    result = _save(client, cid, tools=["mcp:deepwiki"])
    assert result.status_code == 200, result.text
    assert result.json()["preparation"]["tools"] == ["mcp:deepwiki"]
    proposed = _propose(client, harness, cid, {**VALID_PROPOSAL, "tools": []})
    assert proposed["status"] == "draft", proposed["validation_errors"]
    assert proposed["content"]["tools"] == ["mcp:deepwiki"]
    assert '"tools": ["mcp:deepwiki"]' in harness.calls[-1]["messages"][0]["content"][0]["text"]
    approved = _approve(client, cid, proposed)
    assert approved.status_code == 202, approved.text
    with SessionLocal() as db:
        agent = db.get(Agent, approved.json()["agent"]["id"])
        assert [tool["name"] for tool in agent.spec["tools"]] == ["deepwiki"]
        assert agent.spec["native_tools"] == []
    # Approval locks the mounted resources even while the deployment is queued.
    cleared = _save(client, cid, tools=[])
    assert cleared.status_code == 409, cleared.text
    assert cleared.json()["code"] == "assistant.resources_locked"
    first = _latest(client, cid)["proposals"][0]
    assert first["content_hash"] == proposed["content_hash"]
    assert first["status"] == "approved"
    assert _propose(client, harness, cid)["content"]["tools"] == ["mcp:deepwiki"]
    assert _count(Job) == 1


@pytest.mark.parametrize("action", ["save", "unchanged-save", "import"])
@pytest.mark.parametrize("status", ["deploying", "active", "failed", "deleted"])
def test_approval_locks_preparation_before_catalog_or_upload(
    client, ready, harness, s3, monkeypatch, action, status,
):
    cid, sid = _open(client), _inspect(client)
    original = _propose(client, harness, cid)
    outcome = _approve(client, cid, original)
    assert outcome.status_code == 202, outcome.text
    with SessionLocal() as db:
        agent = db.get(Agent, outcome.json()["agent"]["id"])
        agent.status = status
        db.commit()
    before = _latest(client, cid)

    def forbidden(*args, **kwargs):
        raise AssertionError("locked preparation must refuse before resource I/O")

    monkeypatch.setattr(preparation, "live_catalog", forbidden)
    monkeypatch.setattr(service, "_lock_conversation", forbidden)
    if action == "import":
        response = _import(client, cid, sid, revision=0)
    elif action == "unchanged-save":
        response = _save(
            client, cid, kbs=original["content"]["knowledge_bases"],
            skills=original["content"]["skills"], tools=original["content"]["tools"],
        )
    else:
        response = _save(client, cid, revision=0)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "assistant.resources_locked"
    assert _latest(client, cid) == before
    assert s3.uploaded == []
    assert _count(Job) == 1


@pytest.mark.parametrize("field", ["knowledge_bases", "skills", "tools"])
@pytest.mark.parametrize("change", ["remove", "replace", "omit"])
def test_member_cannot_change_approved_resources_even_in_invalid_proposal(
    client, ready, harness, field, change,
):
    cid = _open(client)
    original = _propose(client, harness, cid)
    assert _approve(client, cid, original).status_code == 202
    before = _latest(client, cid)
    content = deepcopy(original["content"])
    content["system_prompt"] = ""  # Resource guard also applies to invalid content.
    if change == "omit":
        content.pop(field)
    else:
        content[field] = [] if change == "remove" else ["unknown-resource"]
    result = client.put(f"{BASE}/conversations/{cid}/proposal", json={"content": content})
    assert result.status_code == 409, result.text
    assert result.json()["code"] == "assistant.resources_locked"
    assert _latest(client, cid)["proposals"] == before["proposals"]
    assert _latest(client, cid)["preparation"] == before["preparation"]
    assert _count(Job) == 1


def test_member_evaluation_edit_preserves_approval_and_preparation(client, ready, harness):
    from .test_assistant_evaluation_repair import _fixed

    cid = _open(client)
    original = _propose(client, harness, cid)
    assert _approve(client, cid, original).status_code == 202
    before = _latest(client, cid)
    content = _fixed(original["content"])
    content["tools"].reverse()  # Same resource set remains legal.
    result = client.put(f"{BASE}/conversations/{cid}/proposal", json={"content": content})
    assert result.status_code == 200, result.text
    revised = result.json()["proposal"]
    assert revised["status"] == "draft"
    assert revised["content"]["evaluation_plan"] == content["evaluation_plan"]
    for field in preparation.RESOURCE_FIELDS:
        assert set(revised["content"][field]) == set(original["content"][field])
    detail = _latest(client, cid)
    assert detail["proposals"][0] == before["proposals"][0]
    assert detail["preparation"] == before["preparation"]
    prepared = client.post(f"{BASE}/conversations/{cid}/evaluation-plan/prepare",
                           json={"revision": revised["revision"]})
    assert prepared.status_code == 201, prepared.text
    assert prepared.json()["plan"]["status"] == "draft"
    assert _count(Job) == 1


@pytest.mark.parametrize("invalid_field", ["system_prompt", "tools"])
def test_member_edit_retaining_resources_still_receives_ordinary_validation(
    client, ready, harness, invalid_field,
):
    cid = _open(client)
    original = _propose(client, harness, cid)
    assert _approve(client, cid, original).status_code == 202
    before = _latest(client, cid)
    content = deepcopy(original["content"])
    if invalid_field == "tools":
        content["tools"].append(content["tools"][0])  # Same set, invalid duplicate.
    else:
        content["system_prompt"] = ""
    result = client.put(f"{BASE}/conversations/{cid}/proposal", json={"content": content})
    assert result.status_code == 200, result.text
    invalid = result.json()["proposal"]
    assert invalid["status"] == "invalid"
    assert any(invalid_field in error for error in invalid["validation_errors"])
    repaired = client.put(f"{BASE}/conversations/{cid}/proposal",
                          json={"content": original["content"]})
    assert repaired.status_code == 200 and repaired.json()["proposal"]["status"] == "draft"
    detail = _latest(client, cid)
    assert detail["preparation"] == before["preparation"]
    assert detail["proposals"][0] == before["proposals"][0]
    assert _count(Job) == 1


@pytest.mark.parametrize("newer_revision", [False, True])
def test_refresh_after_approval_reads_drift_without_minting_proposal(
    client, ready, harness, monkeypatch, newer_revision,
):
    cid = _open(client)
    original = _propose(client, harness, cid)
    assert _approve(client, cid, original).status_code == 202
    if newer_revision:
        _propose(client, harness, cid, {**original["content"], "summary": "Evaluation review"})
    before = _latest(client, cid)
    catalog = _catalog(skills=[], tools=[], knowledge_bases=[], warnings=["resources unavailable"])
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: catalog)
    result = client.post(f"{BASE}/conversations/{cid}/catalog")
    assert result.status_code == 200, result.text
    detail = result.json()["conversation"]
    assert detail["catalog"] == catalog
    assert detail["proposals"] == before["proposals"]
    for field in preparation.RESOURCE_FIELDS:
        assert detail["preparation"][field] == original["content"][field]
    assert detail["preparation"]["revision"] == before["preparation"]["revision"] + 1
    assert _count(Job) == 1


@pytest.mark.parametrize("explicit_selection", [False, True])
def test_model_evaluation_repair_preserves_approved_resources(
    client, ready, harness, explicit_selection,
):
    from .test_assistant import _sse
    from .test_assistant_evaluation_repair import _fixed, _repair, _seed

    cid, source, plan = _seed(client)
    assert _approve(client, cid, source).status_code == 202
    if not explicit_selection:
        with SessionLocal() as db:
            db.get(AssistantConversation, cid).preparation = {}
            db.commit()
    before = _latest(client, cid)
    fixed = _fixed(source["content"])
    fixed.update(knowledge_bases=[], skills=[], tools=["mcp:missing"])
    harness.reply(_block(fixed))
    result = _repair(client, cid, plan)
    assert result.status_code == 200, result.text
    events = _sse(result)
    assert events[-1][0] == "done" and not any(kind == "error" for kind, _ in events)
    revised = next(data for kind, data in events if kind == "proposal")
    assert revised["status"] == "draft", revised["validation_errors"]
    assert revised["content"]["evaluation_plan"] == fixed["evaluation_plan"]
    for field in preparation.RESOURCE_FIELDS:
        assert revised["content"][field] == source["content"][field]
    context = harness.calls[-1]["messages"][0]["content"][0]["text"]
    assert "Approved resources (locked)" in context and "/create" in context
    detail = _latest(client, cid)
    assert detail["preparation"] == before["preparation"]
    assert detail["proposals"][0] == before["proposals"][0]
    prepared = client.post(f"{BASE}/conversations/{cid}/evaluation-plan/prepare",
                           json={"revision": revised["revision"]})
    assert prepared.status_code == 201, prepared.text
    assert prepared.json()["plan"]["status"] == "draft"
    assert _save(client, cid).json()["code"] == "assistant.resources_locked"
    assert _count(Job) == 1


def test_latest_historical_approval_overrides_newer_draft_and_saved_selections(
    client, ready, harness,
):
    cid = _open(client)
    first = _propose(client, harness, cid)
    second = _propose(client, harness, cid, {
        **VALID_PROPOSAL, "tools": [], "skills": [], "knowledge_bases": [],
    })
    assert _approve(client, cid, second).status_code == 202
    # Reproduce a conversation saved before resource locking, including an older
    # approval and a newer, unapproved revision with different preparation state.
    with SessionLocal() as db:
        old = db.get(AssistantProposal, first["id"])
        old.status, old.approved_by, old.approved_at = "approved", "operator", old.created_at
        db.add(AssistantProposal(
            workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=cid, revision=3,
            content=first["content"], content_hash=first["content_hash"],
            bindings=old.bindings, validation_errors=[], status="draft", source="member",
            created_by="operator",
        ))
        row = db.get(AssistantConversation, cid)
        row.revision_seq = 3
        row.preparation = {**row.preparation, "selection_set": True, "tools_selection_set": True,
                           **{field: first["content"][field]
                              for field in preparation.RESOURCE_FIELDS}}
        db.commit()
    before = _latest(client, cid)
    for field in preparation.RESOURCE_FIELDS:
        assert before["preparation"][field] == []
    blocked = _approve(client, cid, before["proposals"][-1])
    assert blocked.status_code == 409 and blocked.json()["code"] == "assistant.resources_locked"
    # The historical approval keeps its idempotent outcome, despite the newer draft.
    assert _approve(client, cid, second).status_code == 200
    revised = _propose(client, harness, cid)
    assert revised["revision"] == 4 and revised["status"] == "draft"
    for field in preparation.RESOURCE_FIELDS:
        assert revised["content"][field] == []
    assert _latest(client, cid)["preparation"] == before["preparation"]
    result = client.put(f"{BASE}/conversations/{cid}/proposal",
                        json={"content": first["content"]})
    assert result.status_code == 409 and result.json()["code"] == "assistant.resources_locked"
    assert _save(client, cid).json()["code"] == "assistant.resources_locked"
    assert _count(Job) == 1


def test_legacy_kb_skill_selection_and_old_client_preserve_proposal_mcps(
    client, ready, harness,
):
    cid = _open(client)
    original = _propose(client, harness, cid)
    with SessionLocal() as db:
        row = db.get(AssistantConversation, cid)
        row.preparation = {
            "selection_set": True, "skills": ["meeting-summarizer"],
            "knowledge_bases": [], "revision": 2,
        }
        db.commit()
    assert _latest(client, cid)["preparation"]["tools"] == original["content"]["tools"]
    res = _save(client, cid, kbs=["KB123ABC"], revision=2)
    assert res.status_code == 200, res.text
    assert res.json()["preparation"]["tools"] == original["content"]["tools"]
    assert res.json()["proposals"][-1]["content"]["tools"] == original["content"]["tools"]
    # Explicit [] is distinct from a missing field.
    res = _save(client, cid, tools=[])
    assert res.status_code == 200
    assert res.json()["preparation"]["tools"] == []


@pytest.mark.parametrize("action", ["old-client-save", "refresh"])
def test_inherited_mcp_selection_stays_implicit_until_member_edits_tools(
    client, ready, harness, monkeypatch, action,
):
    cid = _open(client)
    original = _propose(client, harness, cid)
    with SessionLocal() as db:
        db.get(AssistantConversation, cid).preparation = {
            "selection_set": True, "skills": original["content"]["skills"],
            "knowledge_bases": original["content"]["knowledge_bases"], "revision": 2,
        }
        db.commit()
    if action == "old-client-save":
        result = _save(client, cid, kbs=["KB123ABC"])
    else:
        catalog = _catalog()
        catalog["skills"][0]["content_digest"] = "e" * 64
        monkeypatch.setattr(service, "fetch_catalog", lambda ws: catalog)
        result = client.post(f"{BASE}/conversations/{cid}/catalog")
    assert result.status_code == 200, result.text
    detail = _latest(client, cid)
    assert detail["proposals"][-1]["revision"] == original["revision"] + 1
    assert detail["preparation"]["tools"] == original["content"]["tools"]
    with SessionLocal() as db:
        assert not db.get(AssistantConversation, cid).preparation.get("tools_selection_set")
    # A manual proposal edit is an explicit MCP selection, including an empty list.
    edited = client.put(f"{BASE}/conversations/{cid}/proposal",
                        json={"content": {**detail["proposals"][-1]["content"], "tools": []}})
    assert edited.status_code == 200, edited.text
    assert _latest(client, cid)["preparation"]["tools"] == []
    with SessionLocal() as db:
        assert db.get(AssistantConversation, cid).preparation["tools_selection_set"] is True
    assert _propose(client, harness, cid)["content"]["tools"] == []


@pytest.mark.parametrize("key", ["mcp:dup", "mcp:missing", "builtin:shell"])
def test_unavailable_mcp_cannot_be_selected(client, ready, key):
    cid = _open(client)
    before = _latest(client, cid)["preparation"]
    res = _save(client, cid, tools=[key])
    assert res.status_code == 409 and res.json()["code"] == "assistant.preparation_invalid"
    assert _latest(client, cid)["preparation"] == before
    assert _count(Job) == 0


def test_mcp_refresh_retains_disappeared_selection_and_requires_new_review(
    client, ready, harness, monkeypatch,
):
    cid = _open(client)
    assert _save(client, cid, tools=["mcp:deepwiki"]).status_code == 200
    original = _propose(client, harness, cid)
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: _catalog(
        tools=[], warnings=["registry unavailable"]))
    res = client.post(f"{BASE}/conversations/{cid}/catalog")
    assert res.status_code == 200, res.text
    detail = res.json()["conversation"]
    assert detail["preparation"]["tools"] == ["mcp:deepwiki"]
    assert detail["proposals"][0]["content_hash"] == original["content_hash"]
    assert detail["proposals"][-1]["status"] == "invalid"
    assert any("deepwiki" in err for err in detail["proposals"][-1]["validation_errors"])
    assert _count(Job) == 0


def test_adding_mcp_invalidates_zero_call_seed_without_rewriting_rules(
    client, ready, harness,
):
    cid = _open(client)
    rule = {"id": "zero", "type": "tool_count", "max": 0}
    raw = {**VALID_PROPOSAL, "tools": [], "skills": [], "knowledge_bases": [],
           "evaluation_plan": {"evaluators": [{
               "kind": "code", "key": "zero", "name": "zero_calls", "level": "SESSION",
               "title": "No tool calls",
               "rules": {"version": 1, "checks": [rule]},
           }]}}
    original = _propose(client, harness, cid, raw)
    assert original["status"] == "draft", original["validation_errors"]
    changed = _save(client, cid, tools=["mcp:deepwiki"])
    assert changed.status_code == 200, changed.text
    latest = changed.json()["proposals"][-1]
    assert latest["status"] == "invalid"
    assert any("forbids all tool calls" in err for err in latest["validation_errors"])
    assert latest["content"]["evaluation_plan"]["evaluators"][0]["rules"]["checks"][0]["max"] == 0
    assert changed.json()["proposals"][0]["content_hash"] == original["content_hash"]
    assert _count(Job) == 0
    # Undoing the incompatible selection repairs the proposal without editing its rules.
    restored = _save(client, cid, tools=[])
    assert restored.status_code == 200, restored.text
    recovered = restored.json()["proposals"][-1]
    assert recovered["revision"] == latest["revision"] + 1
    assert recovered["status"] == "draft"
    assert recovered["content"]["tools"] == []
    assert recovered["content"]["evaluation_plan"] == original["content"]["evaluation_plan"]
    assert restored.json()["proposals"][-2]["content_hash"] == latest["content_hash"]


def test_stale_revision_and_unavailable_catalog_do_not_change_selection(client, ready, monkeypatch):
    cid = _open(client)
    assert _save(client, cid, skills=["meeting-summarizer"], revision=0).status_code == 200
    stale = _save(client, cid, revision=0)
    assert stale.status_code == 409 and stale.json()["code"] == "assistant.preparation_stale"
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: _catalog(
        skills=[], warnings=["registry catalog unavailable: access denied"]))
    refused = _save(client, cid, skills=["meeting-summarizer"])
    assert refused.status_code == 409
    assert refused.json()["code"] == "assistant.preparation_invalid"
    refreshed = client.post(f"{BASE}/conversations/{cid}/catalog")
    assert refreshed.status_code == 200, refreshed.text
    detail = refreshed.json()["conversation"]
    assert detail["preparation"]["skills"] == ["meeting-summarizer"]
    assert detail["catalog"]["skills"] == []
    assert "access denied" in detail["catalog"]["warnings"][0]
    assert "NOT evidence of an empty" in service.catalog_section(detail["catalog"])


@pytest.mark.parametrize("missing", ["kb_gateway_id", "kb_gateway_arn", "oauth_provider_arn",
                                     "status", "url"])
def test_kb_selection_needs_all_live_harness_gateway_prerequisites(
    client, ready, monkeypatch, missing,
):
    cid = _open(client)
    cat = _catalog()
    if missing in ("status", "url"):
        cat["resources"]["kb_gateway"].pop(missing)
    else:
        cat["resources"].pop(missing)
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: cat)
    res = _save(client, cid, kbs=["KB123ABC"])
    assert res.status_code == 409 and "ready knowledge-base gateway" in res.json()["message"]


def test_refresh_preserves_selection_and_mints_review_on_binding_drift(
    client, ready, harness, monkeypatch,
):
    cid = _open(client)
    original = _propose(client, harness, cid)
    changed = _catalog()
    changed["skills"][0]["content_digest"] = "e" * 64
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: changed)
    res = client.post(f"{BASE}/conversations/{cid}/catalog")
    detail = res.json()["conversation"]
    assert detail["preparation"]["skills"] == original["content"]["skills"]
    assert detail["proposals"][-1]["source"] == "member"
    assert detail["proposals"][-1]["content_hash"] != original["content_hash"]
    assert detail["proposals"][0]["content_hash"] == original["content_hash"]
    assert detail["proposals"][0]["status"] == "superseded"
    assert _approve(client, cid, original).status_code == 409


def test_advisory_intake_and_malformed_advice_are_independent_of_proposals(client, ready, harness):
    cid = _open(client)
    harness.reply("Please prepare a handbook.\n" + _requirements())
    events = _turn(client, cid, "Help prepare")
    detail = _latest(client, cid)
    assert detail["proposals"] == []
    assert detail["preparation"]["requirements"] == [NEED]
    assert next(d for k, d in events if k == "preparation") == detail["preparation"]
    harness.reply(_block(VALID_PROPOSAL) + "\n```launchpad-preparation\n{broken}\n```")
    _turn(client, cid, "Continue")
    detail = _latest(client, cid)
    assert detail["proposals"][-1]["status"] == "draft"
    assert detail["preparation"]["requirements"] == [NEED]
    assert any(m["name"] == "preparation_rejected" for m in detail["messages"])
    harness.reply(_requirements([]))
    _turn(client, cid, "All done")
    assert _latest(client, cid)["preparation"]["requirements"] == []


@pytest.mark.parametrize("text", [
    "```launchpad-preparation\n{}",
    "```launchpad-preparation\n[]\n```",
    _requirements() + "\n" + _requirements(),
    _requirements([NEED, NEED]),
    _requirements([{**NEED, "kind": "generate_skill"}]),
    _requirements([{**NEED, "title": "x" * 201}]),
    _requirements([{**NEED, "materials": ["x"] * 11}]),
    _requirements([{**NEED, "path": "s3://evil/"}]),
    _requirements([{**NEED, "reason": "x" * 24_001}]),
])
def test_advisory_bounds(text):
    requirements, errors = preparation.extract(text)
    assert requirements is None and errors


def test_advisory_materials_always_projects_a_list():
    need = {k: v for k, v in NEED.items() if k != "materials"}
    parsed, errors = preparation.extract(_requirements([need]))
    assert not errors
    assert parsed[0]["materials"] == []


def test_interrupted_reply_never_updates_preparation(client, ready, harness):
    cid = _open(client)
    harness.reply(_requirements())
    harness.fail_after = 2
    _turn(client, cid, "Prepare")
    assert _latest(client, cid)["preparation"]["revision"] == 0


def test_import_upload_pinning_and_live_approval_and_job_validation(client, ready, harness, s3):
    cid = _open(client)
    sid = _inspect(client)
    result = _import(client, cid, sid)
    assert result.status_code == 200, result.text
    detail = result.json()["conversation"]
    item = result.json()["results"][0]
    assert item["ok"] and item["key"].startswith("imported:")
    assert detail["preparation"]["skills"] == [item["key"]]
    imported = next(s for s in detail["catalog"]["skills"] if s["key"] == item["key"])
    assert imported["content_digest"] and imported["object_count"] == 2
    assert imported["path"].startswith("s3://launchpad-artifacts-test/agent-skills/")
    assert "preparation_sources" not in detail and "staging_id" not in json.dumps(detail)
    assert _count(Job) == 0
    other = _open(client)
    assert item["key"] not in [s["key"] for s in _latest(client, other)["catalog"]["skills"]]
    proposal = _propose(client, harness, cid)
    assert proposal["content"]["skills"] == [item["key"]]
    assert _approve(client, cid, proposal).status_code == 202
    with SessionLocal() as db:
        job = db.query(Job).one()
        agent = db.get(Agent, job.payload["agent_id"])
        workspace = workspace_context(db.get(Workspace, DEFAULT_WORKSPACE_ID))
        service.assert_job_bindings_pinned(job.payload, agent, workspace)
        payload = deepcopy(job.payload)
        payload["assistant"].pop("preparation_sources")
        with pytest.raises(RuntimeError, match="no longer validates"):
            service.assert_job_bindings_pinned(payload, agent, workspace)
        key = next(k for k in s3.files if k[1].endswith("SKILL.md"))
        s3.files[key] += b"\nchanged"
        with pytest.raises(RuntimeError, match="bindings changed"):
            service.assert_job_bindings_pinned(job.payload, agent, workspace)


def test_skill_drift_before_approval_requires_new_review(client, ready, harness, s3):
    cid = _open(client)
    assert _import(client, cid, _inspect(client)).status_code == 200
    proposal = _propose(client, harness, cid)
    key = next(k for k in s3.files if k[1].endswith("SKILL.md"))
    s3.files[key] += b"\nchanged after review"
    response = _approve(client, cid, proposal)
    assert response.status_code == 409 and response.json()["code"] == "assistant.bindings_changed"
    assert _count(Job) == 0
    refresh = client.post(f"{BASE}/conversations/{cid}/catalog")
    revised = refresh.json()["conversation"]["proposals"][-1]
    assert revised["content_hash"] != proposal["content_hash"]
    assert _approve(client, cid, revised).status_code == 202


def test_partial_import_retry_reuses_uploaded_source_and_consumes_only_on_success(
    client, ready, s3,
):
    from app.routers.registry import _staging

    cid = _open(client)
    sid = _inspect(client)
    first = _import(client, cid, sid, indexes=(0, 50))
    assert first.status_code == 200, first.text
    ok, failed = first.json()["results"]
    assert ok["ok"] is True and failed["ok"] is False
    assert sid in _staging
    uploaded = list(s3.uploaded)
    stale = _import(client, cid, sid, revision=0)
    assert stale.status_code == 409 and stale.json()["code"] == "assistant.preparation_stale"
    assert s3.uploaded == uploaded
    retry = _import(client, cid, sid)
    assert retry.status_code == 200
    assert retry.json()["results"][0] == ok
    assert s3.uploaded == uploaded
    assert sid not in _staging
    # A lost successful response can be retried after reloading even after staging expired.
    again = _import(client, cid, sid)
    assert again.status_code == 200 and again.json()["results"][0] == ok
    assert s3.uploaded == uploaded
    assert len(again.json()["conversation"]["preparation"]["skills"]) == 1


def test_upload_failure_cleans_partial_prefix_and_can_retry(client, ready, s3):
    cid = _open(client)
    sid = _inspect(client)
    s3.fail_on = "helper.py"
    res = _import(client, cid, sid)
    assert res.status_code == 200 and res.json()["results"][0]["ok"] is False
    assert s3.files == {}
    assert res.json()["conversation"]["preparation"]["skills"] == []
    s3.fail_on = None
    assert _import(client, cid, sid).json()["results"][0]["ok"] is True


def test_unreadable_uploaded_skill_is_reported_as_failed_and_retry_reuses_source(
    client, ready, s3, monkeypatch,
):
    from app.routers.registry import _staging

    cid, sid = _open(client), _inspect(client)
    original = service.skill_content_snapshot
    monkeypatch.setattr(service, "skill_content_snapshot", lambda *args: None)
    result = _import(client, cid, sid).json()
    assert result["results"][0]["ok"] is False
    assert "could not be verified" in result["results"][0]["error"]
    assert result["conversation"]["preparation"]["skills"] == []
    assert sid in _staging
    uploaded = list(s3.uploaded)
    monkeypatch.setattr(service, "skill_content_snapshot", original)
    retry = _import(client, cid, sid).json()
    assert retry["results"][0]["ok"] is True
    assert retry["conversation"]["preparation"]["skills"] == [retry["results"][0]["key"]]
    assert s3.uploaded == uploaded


def test_selection_recovers_proposal_invalidated_by_catalog_drift(
    client, ready, harness, monkeypatch,
):
    cid = _open(client)
    _propose(client, harness, cid)
    catalog = _catalog()
    catalog["skills"] = []
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: catalog)
    refresh = client.post(f"{BASE}/conversations/{cid}/catalog").json()
    assert refresh["conversation"]["proposals"][-1]["status"] == "invalid"
    saved = _save(client, cid).json()
    assert saved["proposals"][-1]["status"] == "draft"
    assert saved["proposals"][-1]["content"]["skills"] == []
    assert saved["proposals"][-2]["status"] == "superseded"
    assert saved["proposals"][-2]["content_hash"] == (
        refresh["conversation"]["proposals"][-1]["content_hash"]
    )


def test_retrying_known_import_cannot_exceed_selection_limit(client, ready, s3, monkeypatch):
    cid = _open(client)
    sid = _inspect(client)
    assert _import(client, cid, sid).status_code == 200
    cat = _catalog()
    cat["skills"] = [{**cat["skills"][0], "key": f"existing-{i}", "name": f"existing-{i}"}
                     for i in range(10)]
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: cat)
    assert _save(client, cid, skills=[s["key"] for s in cat["skills"]]).status_code == 200
    before = list(s3.uploaded)
    response = _import(client, cid, sid)
    assert response.status_code == 409 and response.json()["code"] == "assistant.preparation_full"
    assert s3.uploaded == before


def test_import_enforces_owner_workspace_permission_and_server_source_only(gated, s3):
    admin, member, other, ids, _ = gated
    cid = _open(member)
    sid = _inspect(member)
    assert _save(other, cid, revision=0).status_code == 404
    assert _import(other, cid, sid, revision=0).status_code == 404
    other_cid = _open(other)
    assert _import(other, other_cid, sid, revision=0).status_code == 410
    assert s3.uploaded == []
    _set_permissions(ids[MEMBER_CREDS["username"]], {"agents.deploy": False})
    assert _save(member, cid, revision=0).status_code == 200
    assert _import(member, cid, sid).status_code == 403
    _set_permissions(ids[MEMBER_CREDS["username"]], None)
    response = member.post(f"{BASE}/conversations/{cid}/preparation/skills",
                           json={"expected_revision": 1, "staging_id": sid,
                                 "selections": [{"index": 0, "path": "s3://evil/arbitrary/"}]})
    assert response.status_code == 422
    with SessionLocal() as db:
        row = db.get(AssistantConversation, cid)
        row.workspace_id = "other-workspace"
        db.commit()
    assert _import(member, cid, sid, revision=1).status_code == 404
    assert s3.uploaded == []


def test_import_staging_cannot_cross_workspace(client, ready, s3):
    from app.routers.registry import _staging

    cid = _open(client)
    sid = _inspect(client)
    _staging[sid]["workspace_id"] = "other"
    assert _import(client, cid, sid).status_code == 410
    assert not s3.uploaded


def test_import_claim_blocks_turn_refresh_selection_edit_approval_and_purge(
    client, ready, harness, s3,
):
    cid = _open(client)
    proposal = _propose(client, harness, cid)
    sid = _inspect(client)
    observed = []

    def during_upload():
        responses = [
            client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "hello"}),
            client.post(f"{BASE}/conversations/{cid}/catalog"),
            _save(client, cid, revision=0),
            client.put(f"{BASE}/conversations/{cid}/proposal", json={"content": VALID_PROPOSAL}),
            _approve(client, cid, proposal),
            client.delete(f"{BASE}/conversations/{cid}"),
        ]
        observed.extend(res.status_code for res in responses)

    s3.on_upload = during_upload
    res = _import(client, cid, sid)
    assert res.status_code == 200, res.text
    assert observed and set(observed) == {409}
    assert _count(Job) == 0
    with SessionLocal() as db:
        assert db.get(AssistantConversation, cid).preparation_token is None


def test_preparation_mutations_refuse_during_turn_before_aws_read(client, ready, monkeypatch):
    cid = _open(client)
    with SessionLocal() as db:
        db.get(AssistantConversation, cid).active_turn = 1
        db.commit()

    def forbidden(*args):
        raise AssertionError("read before turn guard")

    monkeypatch.setattr(service, "fetch_catalog", forbidden)
    assert _save(client, cid, revision=0).status_code == 409
    assert _import(client, cid, "missing", revision=0).status_code == 409
    assert client.post(f"{BASE}/conversations/{cid}/catalog").status_code == 409


def test_racing_selection_writes_only_one_revision_wins(client, ready, monkeypatch):
    cid = _open(client)
    barrier = threading.Barrier(2)

    def fetch(ws):
        barrier.wait(timeout=5)
        return _catalog()

    monkeypatch.setattr(service, "fetch_catalog", fetch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_save, client, cid, revision=0, skills=skills)
                   for skills in ([], ["meeting-summarizer"])]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(r.status_code for r in results) == [200, 409]
    assert _latest(client, cid)["preparation"]["revision"] == 1


@pytest.mark.parametrize("action", ["save", "invalid-save", "import", "edit", "refresh"])
def test_approval_winning_resource_mutation_race_is_rechecked_under_lock(
    client, ready, harness, s3, monkeypatch, action,
):
    cid, sid = _open(client), _inspect(client)
    proposal = _propose(client, harness, cid)
    before = _latest(client, cid)
    entered, release = threading.Event(), threading.Event()
    if action in ("save", "invalid-save", "refresh"):
        target, method = preparation, "live_catalog"
    else:
        target, method = service, "_lock_conversation"
    original = getattr(target, method)
    calls = 0

    def pause_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=10)
            if action == "refresh":
                return _catalog(skills=[], tools=[], knowledge_bases=[])
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method, pause_first)

    def mutate():
        if action in ("save", "invalid-save"):
            return _save(client, cid, skills=["missing"] if action == "invalid-save" else [])
        if action == "import":
            return _import(client, cid, sid)
        if action == "edit":
            return client.put(f"{BASE}/conversations/{cid}/proposal",
                              json={"content": {**proposal["content"], "skills": []}})
        return client.post(f"{BASE}/conversations/{cid}/catalog")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(mutate)
        try:
            assert entered.wait(timeout=10)
            approved = _approve(client, cid, proposal)
            assert approved.status_code == 202, approved.text
        finally:
            release.set()
        result = future.result(timeout=10)
    if action == "refresh":
        assert result.status_code == 200, result.text
    else:
        assert result.status_code == 409, result.text
        assert result.json()["code"] == "assistant.resources_locked"
    detail = _latest(client, cid)
    assert len(detail["proposals"]) == 1
    assert detail["proposals"][0]["status"] == "approved"
    assert detail["proposals"][0]["content_hash"] == proposal["content_hash"]
    for field in preparation.RESOURCE_FIELDS:
        assert detail["preparation"][field] == before["preparation"][field]
    assert s3.uploaded == [] and _count(Job) == 1
    with SessionLocal() as db:
        row = db.get(AssistantConversation, cid)
        assert row.preparation_token is None and not row.preparation_sources


def test_late_refresh_cannot_overwrite_new_selection(client, ready, monkeypatch):
    cid = _open(client)
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def fetch(ws):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
        return _catalog()

    monkeypatch.setattr(service, "fetch_catalog", fetch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, f"{BASE}/conversations/{cid}/catalog")
        assert entered.wait(5)
        assert _save(client, cid, revision=0, skills=["meeting-summarizer"]).status_code == 200
        release.set()
        result = future.result(timeout=10)
    assert result.status_code == 409
    assert _latest(client, cid)["preparation"]["skills"] == ["meeting-summarizer"]
