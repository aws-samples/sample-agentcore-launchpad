"""E02 acceptance gap (`plans/acceptance/E02-assistant.md` §5: NC-E02-02 / 08 / 13) — the
architect assistant's three refusal limits, hermetically, with their exact codes, statuses and
`detail` payloads (the console translates on `code` and shows the numeric limits from `detail`):

* `413 assistant.prompt_too_large` — the current message is NEVER truncated: the character
  cap, the byte cap and the request-budget (preamble + prompt) branch each refuse before any
  data-plane call and leave the conversation's turn count untouched.
* `409 assistant.proposal_stale` — approve / reject against a revision that is not the
  current one (edited meanwhile, unknown, or a changed content hash) names
  `detail.current_revision` and starts no deploy job.
* `409 assistant.workspace_not_ready` — approve while the workspace cannot deploy (bootstrap
  not `ready`, or no execution role) names each missing requirement; the availability
  endpoint exposes the same list as `deploy_requirements`.

Gap recorded, not invented (N13-obs9): E02 NC-13 expects `assistant.workspace_not_ready` when
a conversation is OPENED on a not-ready workspace; today only `proposal/approve` (and the
deploy claim behind it) refuse — opening a conversation and posting turns succeed. The last
test pins that boundary as it is so the plan's expectation can be decided on, not assumed.

Fixtures / helpers are `tests/test_assistant.py`'s (imported — registration only).
"""

from __future__ import annotations

import pytest

from app.assistant import service
from app.core.db import SessionLocal
from app.models.assistant import AssistantConversation
from app.models.ledger import Job
from app.system_agents import service as system_agents
from tests.test_assistant import (  # noqa: F401 — fixtures registered here
    BASE,
    VALID_PROPOSAL,
    _approve,
    _count,
    _latest,
    _mark_ready,
    _open,
    _propose,
    harness,
    ready,
)


@pytest.fixture
def arch(request):
    """`tests.test_assistant.harness` by name (the import above is the registration only)."""
    return request.getfixturevalue("harness")


@pytest.fixture
def ws_ready(request):
    """`tests.test_assistant.ready` by name: workspace marked ready + the preset installed."""
    return request.getfixturevalue("ready")


def _turns(client, cid: str) -> int:
    return _latest(client, cid)["turns"]


# ─── 413 assistant.prompt_too_large ─────────────────────────────────────────────────
def test_a_message_over_the_character_cap_is_refused_by_the_schema_first(client, ws_ready,
                                                                          arch):
    """E02 NC-E02-02 says "> 100 000 characters → 413"; over HTTP the `TurnRequest` schema
    (`max_length = MAX_PROMPT_CHARS`) refuses first with 422 `validation.invalid_request`, so
    the service's character branch is only reachable in-process. Pinned as it is (N13-obs9)."""
    cid = _open(client)
    res = client.post(f"{BASE}/conversations/{cid}/turns",
                      json={"prompt": "x" * (service.MAX_PROMPT_CHARS + 1)})
    assert res.status_code == 422 and res.json()["code"] == "validation.invalid_request"
    [error] = res.json()["detail"]
    assert error["loc"] == ["body", "prompt"] and error["type"] == "string_too_long"
    assert error["ctx"] == {"max_length": service.MAX_PROMPT_CHARS}
    assert arch.calls == [] and _turns(client, cid) == 0  # never reached the model
    assert _count(AssistantConversation) == 1
    # the service-level branch itself (what an in-process caller would hit)
    from app.core.errors import AppError
    with pytest.raises(AppError) as info:
        service.check_prompt(object(), "x" * (service.MAX_PROMPT_CHARS + 1))  # type: ignore[arg-type]
    assert info.value.status_code == 413 and info.value.code == "assistant.prompt_too_large"
    assert info.value.detail == {"max_chars": service.MAX_PROMPT_CHARS,
                                 "max_bytes": service.MAX_PROMPT_BYTES}


def test_a_message_under_the_character_cap_but_over_the_byte_cap_is_refused(client, ws_ready,
                                                                             arch):
    cid = _open(client)
    # 80 000 four-byte characters: well under MAX_PROMPT_CHARS, 320 000 bytes > MAX_PROMPT_BYTES
    prompt = "😀" * 80_000
    assert len(prompt) < service.MAX_PROMPT_CHARS
    assert len(prompt.encode("utf-8")) > service.MAX_PROMPT_BYTES
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": prompt})
    assert res.status_code == 413 and res.json()["code"] == "assistant.prompt_too_large"
    assert res.json()["detail"]["max_bytes"] == service.MAX_PROMPT_BYTES
    assert arch.calls == [] and _turns(client, cid) == 0


def test_a_message_that_blows_the_request_budget_with_the_preamble_is_refused(client, ws_ready,
                                                                               arch,
                                                                               monkeypatch):
    """Second branch: the message alone fits, the message + protocol preamble does not —
    the refusal names the request budget and tells the member to split the message."""
    cid = _open(client)
    db = SessionLocal()
    try:
        preamble = len(service._preamble(db.get(AssistantConversation, cid)))
    finally:
        db.close()
    budget = preamble + 200 + 1_000  # room for a 1 000-char message, not a 1 500-char one
    monkeypatch.setattr(service, "MAX_REPLAY_CHARS", budget)
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "x" * 1_500})
    assert res.status_code == 413
    body = res.json()
    assert body["code"] == "assistant.prompt_too_large"
    assert body["detail"] == {"max_request_chars": budget}
    assert "split it over several turns" in body["message"]
    assert arch.calls == [] and _turns(client, cid) == 0
    # the same conversation still accepts a message that fits the budget
    arch.reply("Q1?")
    ok = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "short"})
    assert ok.status_code == 200 and _turns(client, cid) == 1


def test_the_prompt_cap_is_exact_at_the_boundary(client, ws_ready, arch):
    cid = _open(client)
    arch.reply("Q1?")
    exact = "x" * service.MAX_PROMPT_CHARS  # == cap: accepted (only `>` refuses)
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": exact})
    assert res.status_code == 200, res.text
    assert len(arch.calls) == 1 and _turns(client, cid) == 1


# ─── 409 assistant.proposal_stale ───────────────────────────────────────────────────
def test_approving_a_superseded_revision_after_a_member_edit_is_stale(client, ws_ready, arch):
    cid = _open(client)
    r1 = _propose(client, arch, cid)
    res = client.put(f"{BASE}/conversations/{cid}/proposal",
                     json={"content": {**VALID_PROPOSAL, "name": "hr-helpdesk-v2"}})
    assert res.status_code == 200 and res.json()["proposal"]["revision"] == 2
    # revision 1 by number with its (still valid) hash: superseded is refused by STATUS …
    res = _approve(client, cid, r1)
    assert res.status_code == 409 and res.json()["code"] == "assistant.proposal_not_approvable"
    # … while a wrong revision / hash pairing is STALE, pointing at the current revision
    res = client.post(f"{BASE}/conversations/{cid}/proposal/approve",
                      json={"revision": 2, "content_hash": r1["content_hash"]})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "assistant.proposal_stale"
    assert body["detail"] == {"current_revision": 2}
    assert "changed since it was shown" in body["message"]
    assert _count(Job) == 0  # no deploy started


def test_approving_an_unknown_revision_is_stale_and_names_the_current_one(client, ws_ready, arch):
    cid = _open(client)
    r1 = _propose(client, arch, cid)
    res = client.post(f"{BASE}/conversations/{cid}/proposal/approve",
                      json={"revision": 9, "content_hash": r1["content_hash"]})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "assistant.proposal_stale"
    assert body["detail"] == {"current_revision": 1}
    assert "does not exist" in body["message"]
    assert _count(Job) == 0


def test_rejecting_a_revision_that_is_not_current_is_stale(client, ws_ready, arch):
    cid = _open(client)
    _propose(client, arch, cid)
    res = client.put(f"{BASE}/conversations/{cid}/proposal",
                     json={"content": {**VALID_PROPOSAL, "name": "hr-helpdesk-v2"}})
    assert res.status_code == 200
    res = client.post(f"{BASE}/conversations/{cid}/proposal/reject", json={"revision": 1})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "assistant.proposal_stale"
    assert body["detail"] == {"current_revision": 2}
    # the current revision is still a draft (nothing was rejected by the stale call)
    assert [p["status"] for p in _latest(client, cid)["proposals"]] == ["superseded", "draft"]
    res = client.post(f"{BASE}/conversations/{cid}/proposal/reject", json={"revision": 2})
    assert res.status_code == 200, res.text


def test_a_stale_approve_on_a_conversation_without_any_proposal_has_no_current_revision(
        client, ws_ready, arch):
    cid = _open(client)
    res = client.post(f"{BASE}/conversations/{cid}/proposal/approve",
                      json={"revision": 1, "content_hash": "0" * 64})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "assistant.proposal_stale"
    assert body["detail"] == {"current_revision": None}


# ─── 409 assistant.workspace_not_ready ──────────────────────────────────────────────
def test_approve_is_refused_while_the_workspace_bootstrap_is_not_ready(client, ws_ready, arch):
    cid = _open(client)
    r1 = _propose(client, arch, cid)
    _mark_ready(bootstrap_status="bootstrapping")
    try:
        res = _approve(client, cid, r1)
        assert res.status_code == 409
        body = res.json()
        assert body["code"] == "assistant.workspace_not_ready"
        assert [m["code"] for m in body["detail"]["requirements"]] == [
            system_agents.REQ_BOOTSTRAP]
        assert "workspace bootstrap is 'bootstrapping'" in body["message"]
        assert _count(Job) == 0
        # the availability endpoint tells the console the same thing before it tries
        avail = client.get(BASE).json()
        assert [m["code"] for m in avail["deploy_requirements"]] == [system_agents.REQ_BOOTSTRAP]
        # the proposal is untouched — approvable once the workspace is ready again
        assert _latest(client, cid)["proposals"][-1]["status"] == "draft"
    finally:
        _mark_ready()
    assert client.get(BASE).json()["deploy_requirements"] == []


def test_approve_is_refused_without_an_execution_role_and_names_every_requirement(
        client, ws_ready, arch):
    cid = _open(client)
    r1 = _propose(client, arch, cid)
    _mark_ready(bootstrap_status="failed", execution_role_arn=None)
    try:
        res = _approve(client, cid, r1)
        assert res.status_code == 409 and res.json()["code"] == "assistant.workspace_not_ready"
        codes = [m["code"] for m in res.json()["detail"]["requirements"]]
        assert codes == [system_agents.REQ_BOOTSTRAP, system_agents.REQ_ROLE]
        assert "execution_role_arn missing" in res.json()["message"]
        assert _count(Job) == 0
    finally:
        _mark_ready()


def test_opening_a_conversation_on_a_not_ready_workspace_is_not_refused_today(client, ws_ready,
                                                                             arch):
    """GAP pinned, not fixed (N13-obs9 / E02 NC-E02-13): the plan expects
    `assistant.workspace_not_ready` when a conversation is opened on a workspace whose
    bootstrap is not `ready`; the product only refuses at approve. Opening and talking work,
    the availability endpoint carries the warning — the deploy requirement is reported, the
    refusal is deferred to the approval."""
    _mark_ready(bootstrap_status="bootstrapping")
    try:
        avail = client.get(BASE).json()
        assert avail["available"] is True and avail["can_deploy"] is True
        assert [m["code"] for m in avail["deploy_requirements"]] == [system_agents.REQ_BOOTSTRAP]
        cid = _open(client)  # 201 — no refusal
        arch.reply("Q1?")
        res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "hi"})
        assert res.status_code == 200 and _turns(client, cid) == 1
    finally:
        _mark_ready()
