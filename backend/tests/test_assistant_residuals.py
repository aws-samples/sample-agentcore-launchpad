# ruff: noqa: F811  — pytest fixtures are imported by name from test_assistant
"""SE-039 residual regressions (host review 2): versioned session cookie, principal
substitution, winner re-read, content-event provenance, whole-ASGI disconnect
cleanup, stale-claim takeover without publication, ingress/normalized byte caps,
durable job eligibility, pinned Harness stage execution and existing-only KB gateway.
Hermetic: temp ledger, sockets refused, AWS client factory refused."""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.assistant import proposal as contract
from app.assistant import service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import harness as harness_deployer
from app.deployer import pipeline
from app.deployer.pipeline import StageContext
from app.models.assistant import AssistantConversation, AssistantProposal
from app.models.ledger import Agent, Job, User, Workspace
from app.routers import auth as auth_module
from app.services import kb_gateway as kbgw
from app.services import observability, registry_console
from app.services import users as users_service
from app.services.workspace import workspace_context

from .test_assistant import (  # noqa: F401 — shared hermetic fixtures + helpers
    ADMIN_CREDS,
    BASE,
    CATALOG,
    GW_ARN,
    MEMBER_CREDS,
    OAUTH,
    RESOURCES,
    VALID_PROPOSAL,
    _activate,
    _approve,
    _block,
    _catalog,
    _count,
    _install_preset,
    _latest,
    _mark_ready,
    _open,
    _propose,
    _sse,
    _turn,
    gated,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

_REAL_EXECUTE = pipeline.execute_deploy_job


# ---------------------------------------------------------------------------
# versioned cookie bound to the immutable user id
# ---------------------------------------------------------------------------


def test_old_cookie_of_a_deleted_account_cannot_reach_the_recycled_username(gated, harness):
    admin, member, _other, ids, _preset = gated
    old_id = ids[MEMBER_CREDS["username"]]
    old_cookie = member.cookies.get(auth_module.COOKIE_NAME)
    cid_old = _open(member)
    assert admin.delete(f"/api/users/{old_id}").status_code == 200
    with TestClient(admin.app, client=("127.0.0.1", 4321)) as reborn:
        new_id = _activate(MEMBER_CREDS, reborn)
        assert new_id != old_id
        cid_new = _open(reborn)
        # the old cookie names the deleted id: it authenticates nobody, so it cannot
        # read the NEW account's conversation (nor anything else)
        stale = TestClient(admin.app, client=("127.0.0.1", 4321))
        stale.cookies.set(auth_module.COOKIE_NAME, old_cookie)
        assert stale.get("/api/auth/status").json()["authenticated"] is False
        assert stale.get(f"{BASE}/conversations/{cid_new}").status_code == 401
        assert stale.get(f"{BASE}/conversations").status_code == 401
        # the new account's cookie cannot read the old account's conversation
        assert reborn.get(f"{BASE}/conversations/{cid_old}").status_code == 404
        assert [c["id"] for c in reborn.get(f"{BASE}/conversations").json()["conversations"]] == [
            cid_new]
    # a v1 (username-only) cookie for a registered user is refused → re-login
    v1 = auth_module._sign(
        __import__("base64").urlsafe_b64encode(
            f"1:{MEMBER_CREDS['username']}:{int(time.time()) + 3600}".encode()
        ).decode().rstrip("="))
    legacy = TestClient(admin.app, client=("127.0.0.1", 4321))
    legacy.cookies.set(auth_module.COOKIE_NAME, v1)
    assert legacy.get("/api/auth/status").json()["authenticated"] is False
    # config admin keeps working (row-less, stable principal), incl. after user churn
    assert admin.get(BASE).json()["principal"] == "config-admin"


def test_open_console_and_admin_cookie_parity(client, ready):
    assert client.get(BASE).json()["principal"] == "local-operator"
    decoded = auth_module._decode(auth_module._issue("operator", int(time.time()) + 60))
    assert decoded is not None and decoded[1] is None  # admin cookie carries no user id


def test_principal_substitution_during_the_catalog_read_is_refused(gated, harness, monkeypatch):
    """The account is deleted and the same username re-registered while the approval
    waits in its live catalog read; the re-resolved identity is a different principal
    and must not become the approver."""
    admin, member, _other, ids, _preset = gated
    cid = _open(member)
    r1 = _propose(member, harness, cid)
    old_id = ids[MEMBER_CREDS["username"]]
    original = service.fetch_catalog
    swapped: dict = {}

    def swap_account(workspace):
        db = SessionLocal()
        try:  # delete + re-register the same username with a new users.id
            user = db.get(User, old_id)
            users_service.delete_user(db, user)
            db.commit()
        finally:
            db.close()
        with TestClient(admin.app, client=("127.0.0.1", 4321)) as reborn:
            swapped["new_id"] = _activate(MEMBER_CREDS, reborn)
        return original(workspace)

    monkeypatch.setattr(service, "fetch_catalog", swap_account)
    res = _approve(member, cid, r1)
    assert res.status_code in (401, 404), res.text  # cookie names a gone id, or not owner
    assert _count(Job) == 0 and swapped["new_id"] != old_id
    # and the service-level equality check, independent of the cookie
    monkeypatch.setattr(service, "fetch_catalog", original)
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        from app.routers.auth import Identity

        other = Identity(username=MEMBER_CREDS["username"], role="member",
                         user_id=swapped["new_id"])
        with pytest.raises(Exception) as exc:
            service.approve_proposal(
                db, conv, row, workspace_context(row),
                Identity(username=MEMBER_CREDS["username"], role="member", user_id=old_id),
                revision=1, content_hash=r1["content_hash"],
                recheck=lambda s: (other, s.get(Workspace, DEFAULT_WORKSPACE_ID)),
            )
        assert getattr(exc.value, "code", "") == "assistant.conversation_not_found"
        assert db.query(AssistantProposal).one().status == "draft"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# approval: winner re-read before any post-I/O catalog refusal
# ---------------------------------------------------------------------------


def test_losing_exact_approval_with_drifted_catalog_returns_the_winners_outcome(
    client, ready, harness, monkeypatch
):
    cid = _open(client)
    r1 = _propose(client, harness, cid)
    client.get(BASE)
    gate, released = threading.Event(), threading.Event()
    original = service.fetch_catalog
    calls: list[int] = []

    def paced(workspace):
        calls.append(1)
        if len(calls) == 1:  # the paused loser; its catalog view has drifted meanwhile
            gate.set()
            released.wait(timeout=10)
            drifted = _catalog()
            drifted["tools"][1]["url"] = "https://mcp.deepwiki.example/OTHER"
            return drifted
        return original(workspace)

    monkeypatch.setattr(service, "fetch_catalog", paced)
    results: dict = {}
    t = threading.Thread(target=lambda: results.__setitem__("slow", _approve(client, cid, r1)))
    t.start()
    assert gate.wait(timeout=10)
    fast = _approve(client, cid, r1)
    assert fast.status_code == 202
    released.set()
    t.join(timeout=30)
    slow = results["slow"]
    assert slow.status_code == 200 and slow.json()["job_id"] == fast.json()["job_id"]
    assert _count(Job) == 1
    # a DIFFERENT unapproved revision still cannot ride on the winner
    r2 = client.put(f"{BASE}/conversations/{cid}/proposal",
                    json={"content": {**VALID_PROPOSAL, "name": "hr-two"}}).json()["proposal"]
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: (_ for _ in ()).throw(
        AssertionError("should refuse before catalog")) if False else _catalog())
    drifted = _catalog()
    drifted["tools"][1]["url"] = "https://mcp.deepwiki.example/OTHER"
    monkeypatch.setattr(service, "fetch_catalog", lambda ws: drifted)
    res = _approve(client, cid, r2)
    assert res.status_code == 409 and res.json()["code"] == "assistant.bindings_changed"
    assert _count(Job) == 1


# ---------------------------------------------------------------------------
# observability: content-event provenance survives normalization + cache
# ---------------------------------------------------------------------------


def test_trace_detail_hides_a_private_session_named_only_by_a_content_event(
    gated, harness, monkeypatch
):
    _admin, member, other, _ids, _preset = gated
    cid = _open(member)
    harness.reply("private CUSTOMER_SECRET_039")
    sid = _turn(member, cid, "workshop")[0][1]["session_id"]
    trace_id = "a" * 32
    span = {"traceId": trace_id, "spanId": "s1", "name": "invoke_agent", "kind": "SERVER",
            "startTimeUnixNano": "1", "endTimeUnixNano": "2", "durationNano": "1",
            "attributes": {"gen_ai.system": "x"},  # NO session.id on the span
            "resource": {"attributes": {
                "aws.log.group.names": "/aws/bedrock-agentcore/runtimes/r"}}}
    event = {"spanId": "s1", "attributes": {"session.id": sid},
             "body": {"input": {"messages": [{"role": "user", "content": "CUSTOMER_SECRET_039"}]}}}

    def fake_queries(queries, hours, **kw):
        out = {}
        for key in queries:
            if key == "spans":
                out[key] = [{"@message": json.dumps(span)}]
            elif key == "events":
                out[key] = [{"@message": json.dumps(event)}]
            else:
                out[key] = []
        return out

    monkeypatch.setattr(observability, "run_insights_queries", fake_queries)
    observability.reset_cache()
    # the owner warms the cache first — the other member must still get 404 on the hit
    owner = member.get(f"/api/observability/traces/{trace_id}")
    assert owner.status_code == 200 and sid in owner.json()["meta"]["session_ids"]
    res = other.get(f"/api/observability/traces/{trace_id}")
    assert res.status_code == 404 and "CUSTOMER_SECRET_039" not in res.text
    # an ordinary public trace (no private sid anywhere) is unaffected
    event["attributes"]["session.id"] = "public-session-1234567890"
    observability.reset_cache()
    assert other.get(f"/api/observability/traces/{trace_id}").status_code == 200


# ---------------------------------------------------------------------------
# whole-ASGI disconnect cleanup (2.0 disconnect + 2.4 send failure), no forced GC
# ---------------------------------------------------------------------------


def _asgi_turn(app, cid: str, *, spec_version: str, fail_send_after: int | None,
               disconnect_after: int | None) -> list[bytes]:
    body = json.dumps({"prompt": "go"}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
             "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": f"{BASE}/conversations/{cid}/turns", "raw_path": b"", "query_string": b"",
             "root_path": "", "headers": [(b"content-type", b"application/json"),
                                          (b"content-length", str(len(body)).encode()),
                                          (b"host", b"testserver")],
             "client": ("127.0.0.1", 4321), "server": ("testserver", 80)}
    chunks: list[bytes] = []
    sent_body = {"n": 0, "done": False}
    disconnect = asyncio.Event()

    async def receive():
        if not sent_body["done"]:
            sent_body["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            data = message.get("body", b"")
            if data:
                sent_body["n"] += 1
                chunks.append(data)
                if fail_send_after is not None and sent_body["n"] >= fail_send_after:
                    raise OSError("peer reset")
                if disconnect_after is not None and sent_body["n"] >= disconnect_after:
                    disconnect.set()

    async def main():
        try:
            await app(scope, receive, send)
        except Exception:
            pass  # ClientDisconnect propagates on 2.4; irrelevant to the contract

    asyncio.run(main())
    return chunks


@pytest.mark.parametrize("mode", ["asgi-2.0-disconnect", "asgi-2.4-send-error"])
def test_whole_asgi_disconnect_finalizes_the_turn_deterministically(client, ready, harness, mode):
    """After the first delta the client goes away. Without any gc.collect the upstream
    stream is closed, the partial answer + interrupted row are persisted, and the
    claim is released — by the response's own cleanup, in both ASGI paths."""
    import gc

    gc.disable()
    try:
        cid = _open(client)
        harness.reply("partial answer that keeps going " * 20 + _block(VALID_PROPOSAL))
        entered = threading.Event()
        harness.on_invoke = lambda kw: entered.set()
        if mode == "asgi-2.0-disconnect":
            chunks = _asgi_turn(client.app, cid, spec_version="2.0", fail_send_after=None,
                                disconnect_after=2)
        else:
            chunks = _asgi_turn(client.app, cid, spec_version="2.4", fail_send_after=2,
                                disconnect_after=None)
        assert entered.is_set() and chunks  # the data-plane call really started
        db = SessionLocal()
        try:
            conv = db.get(AssistantConversation, cid)
            assert conv.active_turn is None and conv.active_turn_token is None
            roles = [m.role for m in service._messages(db, cid)]
            assert roles[0] == "user" and roles[-1] == "error"
            assert "interrupted" in service._messages(db, cid)[-1].text
            assert service.latest_proposal(db, cid) is None  # nothing from a cut reply
        finally:
            db.close()
        assert harness.streams[0].closed
    finally:
        gc.enable()


def test_cancel_unblocks_a_blocked_upstream_read(ready, harness):
    """The worker is blocked inside the upstream read; cancel closes the stream so the
    read returns, then closes the generator within the bounded wait."""
    release = threading.Event()

    class Blocking:
        closed = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.closed:
                raise StopIteration
            release.wait(timeout=10)
            raise StopIteration

        def close(self):
            self.closed = True
            release.set()

    blocking = Blocking()
    harness.invoke_harness = lambda **kw: {"stream": blocking}
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        from app.routers.auth import Identity

        identity = Identity(username="river", role="admin")
        conv = service.create_conversation(db, row, workspace_context(row), identity, "t")
        run = service.TurnRun()
        gen = service.run_turn(db, conv, row, workspace_context(row), identity, "go", run=run)
        run.generator = gen
        first = next(gen)  # meta; the worker thread now enters the blocked read
        assert first["event"] == "meta"
        worker_done = threading.Event()

        def drain():
            try:
                for _ in gen:
                    pass
            finally:
                worker_done.set()

        threading.Thread(target=drain, daemon=True).start()
        time.sleep(0.2)  # inside Blocking.__next__
        started = time.monotonic()
        run.cancel(wait_s=5)
        assert worker_done.wait(timeout=5) and time.monotonic() - started < 5
        assert blocking.closed and run.finished
        db.expire_all()
        assert db.get(AssistantConversation, conv.id).active_turn is None
        assert [m.role for m in service._messages(db, conv.id)][-1] == "error"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# stale claim takeover: the old worker can never publish
# ---------------------------------------------------------------------------


def test_expired_claim_is_taken_over_and_the_old_worker_cannot_publish(client, ready, harness):
    cid = _open(client)
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        row.active_turn, row.turns, row.active_turn_token = 1, 1, "oldtoken"
        row.active_turn_started_at = datetime.now(UTC) - timedelta(
            seconds=service.TURN_CLAIM_TTL_S + 5)
        db.commit()
    finally:
        db.close()
    harness.reply("fresh")
    events = _turn(client, cid, "again")  # real route, no precheck bypass
    assert events[0][1]["turn"] == 2 and events[-1][0] == "done"
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        assert conv.active_turn is None and conv.turns == 2
        # the old worker (turn 1, token "oldtoken") tries to publish its late result
        assert service._persist_partial(db, conv, 1, "oldtoken", "x" * 64, "late", "late") is False
        assert not any(m.turn == 1 and m.role in ("assistant", "error")
                       for m in service._messages(db, cid))
        # even while a NEW claim is live, the old token cannot release or publish
        conv.active_turn, conv.active_turn_token, conv.turns = 3, "newtoken", 3
        conv.active_turn_started_at = datetime.now(UTC)
        db.commit()
        service.release_turn(db, cid, 3, "oldtoken")
        db.expire_all()
        assert db.get(AssistantConversation, cid).active_turn == 3
        # a live (fresh) claim is NOT stealable through the route
        res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "steal"})
        assert res.status_code == 409 and res.json()["code"] == "assistant.turn_in_progress"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ingress + normalized byte caps
# ---------------------------------------------------------------------------


def test_ingress_body_cap_and_forbidden_outer_fields(client, ready, harness):
    cid = _open(client)
    _propose(client, harness, cid)
    padded = {"content": VALID_PROPOSAL, "padding": "x" * 1_000_000}
    res = client.put(f"{BASE}/conversations/{cid}/proposal", content=json.dumps(padded).encode(),
                     headers={"content-type": "application/json", "content-length": "10"})
    assert res.status_code == 413 and res.json()["code"] == "assistant.request_too_large"
    res = client.put(f"{BASE}/conversations/{cid}/proposal",
                     json={"content": VALID_PROPOSAL, "padding": "x" * 10})
    assert res.status_code == 422  # unknown outer members are refused, not ignored
    res = client.post(f"{BASE}/conversations/{cid}/turns",
                      content=json.dumps({"prompt": "字" * 200_000}).encode(),
                      headers={"content-type": "application/json"})
    assert res.status_code == 413
    assert _count(AssistantProposal) == 1 and len(harness.calls) == 1


def test_normalized_stored_proposal_respects_the_cap(client, ready, harness):
    cid = _open(client)
    _propose(client, harness, cid)
    # raw ≈ 59k bytes; the normalized form (defaults filled in) exceeds 64k
    raw = {**VALID_PROPOSAL, "golden_tests": [
        {"id": f"g{i}", "input": "x" * 1470} for i in range(40)]}
    assert contract.serialized_bytes(raw) < contract.PROPOSAL_MAX_BYTES
    normalized = contract.parse_content(raw)[0].model_dump()
    assert contract.serialized_bytes(normalized) > contract.PROPOSAL_MAX_BYTES
    res = client.put(f"{BASE}/conversations/{cid}/proposal", json={"content": raw})
    assert res.status_code == 200
    p = res.json()["proposal"]
    assert p["status"] == "invalid" and any("normalized" in e for e in p["validation_errors"])
    db = SessionLocal()
    try:
        for row in db.query(AssistantProposal).all():
            assert contract.serialized_bytes(row.content) <= contract.PROPOSAL_MAX_BYTES
    finally:
        db.close()
    assert _approve(client, cid, p).status_code == 409


# ---------------------------------------------------------------------------
# durable job eligibility: a stale retry cannot revive a terminal job
# ---------------------------------------------------------------------------


def test_stale_retry_cannot_revive_a_failed_job(client, ready, harness, monkeypatch):
    cid = _open(client)
    r1 = _propose(client, harness, cid)
    body = _approve(client, cid, r1).json()  # starter is stubbed → job stays queued
    stages_run: list[str] = []

    def failing_provision(ctx, agent, **kw):
        stages_run.append("provision")
        raise RuntimeError("provision boom")

    monkeypatch.setattr(pipeline, "get_method", lambda name: {
        "generate": lambda ctx, agent: pipeline.StageResult(detail="ok"),
        "provision": failing_provision})
    monkeypatch.setattr(service, "assert_job_bindings_pinned", lambda *a, **k: None)
    _REAL_EXECUTE(body["job_id"], resume=False)  # B's real worker: fails the job
    db = SessionLocal()
    try:
        assert db.get(Job, body["job_id"]).status == "failed"
    finally:
        db.close()
    # A's stale snapshot (it saw `queued`) re-approves → the router re-wakes only a
    # queued job; and even a direct fresh launch is inert on a terminal job
    again = _approve(client, cid, r1)
    assert again.status_code == 200 and again.json()["job_id"] == body["job_id"]
    _REAL_EXECUTE(body["job_id"], resume=False)
    _REAL_EXECUTE(body["job_id"], resume=True)
    assert stages_run == ["provision"]  # ran exactly once
    db = SessionLocal()
    try:
        assert db.get(Job, body["job_id"]).status == "failed"
    finally:
        db.close()
    # a fresh (non-resume) launch never adopts a `running` job; startup resume does
    db = SessionLocal()
    try:
        db.get(Job, body["job_id"]).status = "running"
        db.commit()
    finally:
        db.close()
    _REAL_EXECUTE(body["job_id"], resume=False)
    assert stages_run == ["provision"]
    _REAL_EXECUTE(body["job_id"], resume=True)
    assert stages_run == ["provision", "provision"]


# ---------------------------------------------------------------------------
# pinned Harness stage execution + existing-only KB gateway
# ---------------------------------------------------------------------------


def _pin(content=VALID_PROPOSAL, catalog=None):
    parsed = contract.parse_content(content)[0]
    return {"content": content,
            "bindings": contract.resource_bindings(parsed, catalog or _catalog())}


def test_stage_request_uses_the_pinned_gateway_auth_and_memory_not_the_live_workspace():
    pin = _pin()
    spec = contract.to_agent_spec(contract.parse_content(VALID_PROPOSAL)[0], _catalog())
    drifted_ws = workspace_context(Workspace(
        id="default", name="d", account_id="111122223333", region="us-west-2",
        bootstrap_status="ready",
        resources={**RESOURCES, "memory_arn": "arn:aws:x:memory/OTHER",
                   "kb_gateway_arn": "arn:aws:x:gateway/OTHER", "oauth_provider_arn": "arn:other"}))
    params = harness_deployer._build_live_params(spec, drifted_ws, pin)
    gateways = [t for t in params["tools"] if t["type"] == "agentcore_gateway"]
    assert {g["config"]["agentCoreGateway"]["gatewayArn"] for g in gateways} == {
        GW_ARN, RESOURCES["kb_gateway_arn"]}  # reviewed ARNs, not the drifted map
    hr = next(g for g in gateways if g["config"]["agentCoreGateway"]["gatewayArn"] == GW_ARN)
    assert hr["config"]["agentCoreGateway"]["outboundAuth"] == OAUTH
    kb = next(g for g in gateways if g["config"]["agentCoreGateway"]["gatewayArn"]
              == RESOURCES["kb_gateway_arn"])
    assert kb["config"]["agentCoreGateway"]["outboundAuth"]["oauth"]["providerArn"] == (
        RESOURCES["oauth_provider_arn"])
    assert params["memory"] == {"agentCoreMemoryConfiguration": {"arn": RESOURCES["memory_arn"]}}
    disabled = contract.parse_content({**VALID_PROPOSAL, "memory": "disabled"})[0]
    params = harness_deployer._build_live_params(
        contract.to_agent_spec(disabled, _catalog()), drifted_ws,
        _pin({**VALID_PROPOSAL, "memory": "disabled"}))
    assert params["memory"] == {"disabled": {}}


def test_pinned_verification_refuses_live_auth_or_skill_drift_before_the_write(monkeypatch):
    pin = _pin()
    spec = contract.to_agent_spec(contract.parse_content(VALID_PROPOSAL)[0], _catalog())
    ws = workspace_context(Workspace(id="default", name="d", account_id="111122223333",
                                     region="us-west-2", bootstrap_status="ready",
                                     resources=dict(RESOURCES)))
    ctx = StageContext(agent_id="a", deployment_id="d", job_id="j", workspace=ws)
    live_auth = {"oauth": {**OAUTH["oauth"]}}
    monkeypatch.setattr(registry_console, "resolve_gateway_attachments", lambda tools, w: [
        {"gateway_id": "gw-1", "gateway_arn": GW_ARN, "outbound_auth": live_auth}])
    monkeypatch.setattr(service, "skill_content_snapshot",
                        lambda w, path: {"content_digest": "d" * 64, "object_count": 3})
    harness_deployer._verify_pinned_resources(ctx, spec, pin, skills=True)  # identical → ok
    live_auth["oauth"] = {**OAUTH["oauth"], "providerArn": "arn:aws:other-provider"}  # B ≠ A
    with pytest.raises(RuntimeError, match="live ARN/outbound auth differ"):
        harness_deployer._verify_pinned_resources(ctx, spec, pin, skills=True)
    live_auth["oauth"] = dict(OAUTH["oauth"])
    monkeypatch.setattr(service, "skill_content_snapshot",
                        lambda w, path: {"content_digest": "e" * 64, "object_count": 3})
    with pytest.raises(RuntimeError, match="no longer matches the reviewed content"):
        harness_deployer._verify_pinned_resources(ctx, spec, pin, skills=True)
    # the deploy stage consults the same guard on its final request
    monkeypatch.setattr(service, "skill_content_snapshot",
                        lambda w, path: {"content_digest": "d" * 64, "object_count": 3})
    monkeypatch.setattr(harness_deployer, "_execution_role_arn", lambda c, a: "arn:role")
    sent: list = []

    class Control:
        def create_harness(self, **kw):
            sent.append(kw)
            return {"harness": {"harnessId": "h1", "arn": "arn:h1", "harnessVersion": "1"}}

    monkeypatch.setattr(harness_deployer, "control_client", lambda w: Control())
    monkeypatch.setattr(harness_deployer.hc, "wait_harness_ready",
                        lambda c, h: {"arn": "arn:h1", "harnessVersion": "1"})
    monkeypatch.setattr(harness_deployer.agent_iam, "retry_iam_propagation", lambda fn, log: fn())
    db = SessionLocal()
    try:
        agent = Agent(workspace_id="default", name="hr-helpdesk", method="harness",
                      status="deploying", spec=spec.model_dump())
        db.add(agent)
        db.commit()
        ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j", workspace=ws)
        ctx.scratch["assistant_pin"] = pin
        ctx.scratch["mode"] = "create"
        ctx.log = lambda m: None
        harness_deployer._stage_deploy(ctx, agent)
        assert len(sent) == 1
        gw = [t for t in sent[0]["tools"] if t["type"] == "agentcore_gateway"]
        assert gw[0]["config"]["agentCoreGateway"]["outboundAuth"] == OAUTH  # pinned A, never B
        monkeypatch.setattr(service, "skill_content_snapshot",
                            lambda w, path: {"content_digest": "f" * 64, "object_count": 1})
        agent2 = Agent(workspace_id="default", name="hr-two", method="harness",
                       status="deploying", spec={**spec.model_dump(), "name": "hr-two"})
        db.add(agent2)
        db.commit()
        ctx2 = StageContext(agent_id=agent2.id, deployment_id="d2", job_id="j2", workspace=ws)
        ctx2.scratch["assistant_pin"] = pin
        ctx2.scratch["mode"] = "create"
        ctx2.log = lambda m: None
        with pytest.raises(RuntimeError, match="reviewed content"):
            harness_deployer._stage_deploy(ctx2, agent2)
        assert len(sent) == 1  # nothing was sent for the drifted skill
    finally:
        db.close()


def test_assistant_kb_mount_uses_an_existing_ready_gateway_and_never_creates_one(monkeypatch):
    class Control:
        def __init__(self, status="READY", arn=RESOURCES["kb_gateway_arn"], missing=False):
            self.status, self.arn, self.missing = status, arn, missing
            self.targets: list = []

        def list_gateways(self, **kw):
            raise AssertionError("assistant KB mount must never list-and-create a gateway")

        def create_gateway(self, **kw):
            raise AssertionError("assistant KB mount must never create a gateway")

        def get_gateway(self, gatewayIdentifier):
            if self.missing:
                raise RuntimeError("ResourceNotFoundException")
            return {"gatewayId": gatewayIdentifier, "gatewayArn": self.arn,
                    "gatewayUrl": "https://kb.example/mcp", "status": self.status}

    ws = workspace_context(Workspace(id="default", name="d", account_id="111122223333",
                                     region="us-west-2", bootstrap_status="ready",
                                     resources=dict(RESOURCES)))
    gw = kbgw.lookup_existing_kb_gateway(Control(), ws, expected_arn=RESOURCES["kb_gateway_arn"])
    assert gw["id"] == "kbgw-1" and gw["url"] == "https://kb.example/mcp"
    for control, needle in ((Control(status="CREATING"), "not READY"),
                            (Control(missing=True), "could not be read"),
                            (Control(arn="arn:aws:x:gateway/other"), "no longer the one")):
        with pytest.raises(RuntimeError, match=needle):
            kbgw.lookup_existing_kb_gateway(control, ws, expected_arn=RESOURCES["kb_gateway_arn"])
    bare = workspace_context(Workspace(id="default", name="d", account_id="111122223333",
                                       region="us-west-2", bootstrap_status="ready",
                                       resources={k: v for k, v in RESOURCES.items()
                                                  if k != "kb_gateway_id"}))
    with pytest.raises(RuntimeError, match="manual prerequisite"):
        kbgw.lookup_existing_kb_gateway(Control(), bare)
    # the real provision stage with a pin: existing lookup + target mount, no creation
    control = Control()
    monkeypatch.setattr(harness_deployer, "control_client", lambda w: control)
    monkeypatch.setattr(harness_deployer.agent_iam, "provision_execution_role",
                        lambda *a, **k: ("arn:aws:iam::1:role/x", "role ok"))
    monkeypatch.setattr(harness_deployer.kbgw, "ensure_retrieve_target",
                        lambda c, gid, kb_id, name, desc: control.targets.append(
                            ("retrieve", gid, kb_id)))
    monkeypatch.setattr(harness_deployer.kbgw, "sync_agentic_target",
                        lambda c, gid, name, kbs: control.targets.append(("agentic", gid, name)))
    spec = contract.to_agent_spec(contract.parse_content(VALID_PROPOSAL)[0], _catalog())
    agent = Agent(id="a1", workspace_id="default", name="hr-helpdesk", method="harness",
                  status="deploying", spec=spec.model_dump())
    ctx = StageContext(agent_id="a1", deployment_id="d", job_id="j", workspace=ws)
    ctx.scratch["assistant_pin"] = _pin()
    ctx.log = lambda m: None
    result = harness_deployer._stage_provision(ctx, agent)
    assert "kb targets ready" in result.detail
    assert control.targets == [("retrieve", "kbgw-1", "KB123ABC"),
                               ("agentic", "kbgw-1", "hr-helpdesk")]
    kb_tool = ctx.scratch["create_params"]["tools"][-1]["config"]["agentCoreGateway"]
    assert kb_tool["gatewayArn"] == RESOURCES["kb_gateway_arn"]
    # gateway gone at provision time → actionable failure, still no creation
    control.missing = True
    with pytest.raises(RuntimeError, match="could not be read"):
        harness_deployer._stage_provision(ctx, agent)
    # ordinary (non-assistant) agents keep the ensure helper — unchanged behaviour
    assert harness_deployer.kbgw.ensure_kb_gateway_persisted is kbgw.ensure_kb_gateway_persisted


def test_pipeline_entry_seeds_the_pin_for_the_stages(client, ready, harness, monkeypatch):
    cid = _open(client)
    r1 = _propose(client, harness, cid)
    body = _approve(client, cid, r1).json()
    seen: dict = {}

    def capture_generate(ctx, agent):
        seen["pin"] = ctx.scratch.get("assistant_pin")
        return pipeline.StageResult(detail="ok")

    monkeypatch.setattr(pipeline, "get_method", lambda name: {"generate": capture_generate})
    _REAL_EXECUTE(body["job_id"], resume=False)
    assert seen["pin"]["bindings"]["resources"]["gateways"]["gw-1"]["outbound_auth"] == OAUTH
    assert seen["pin"]["revision"] == 1
