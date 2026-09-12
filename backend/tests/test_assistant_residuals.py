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
    KB_AUTHORIZER,
    KB_GATEWAY_LIVE,
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

        url = "https://kb.example/mcp"
        authorizer_type = "CUSTOM_JWT"
        authorizer = KB_AUTHORIZER

        def get_gateway(self, gatewayIdentifier):
            if self.missing:
                raise RuntimeError("ResourceNotFoundException")
            return {"gatewayId": gatewayIdentifier, "gatewayArn": self.arn,
                    "gatewayUrl": self.url, "authorizerType": self.authorizer_type,
                    "authorizerConfiguration": self.authorizer, "status": self.status}

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


# ---------------------------------------------------------------------------
# review 3 residuals
# ---------------------------------------------------------------------------


def test_provenance_covers_nested_resource_attrs_and_merged_multi_sid_events(
    gated, harness, monkeypatch
):
    _admin, member, other, _ids, _preset = gated
    cid = _open(member)
    harness.reply("private CUSTOMER_SECRET_HOST")
    sid = _turn(member, cid, "workshop")[0][1]["session_id"]
    trace_id = "b" * 32
    span = {"traceId": trace_id, "spanId": "s1", "name": "invoke_agent", "kind": "SERVER",
            "startTimeUnixNano": "1", "endTimeUnixNano": "2", "durationNano": "1",
            "attributes": {"gen_ai.system": "x"},
            "resource": {"attributes": {
                "aws.log.group.names": "/aws/bedrock-agentcore/runtimes/r"}}}
    variants = {
        "nested-resource-attrs": [
            {"spanId": "s1", "resource": {"attributes": {"session.id": sid}},
             "body": {"input": {"messages": [{"role": "user",
                                              "content": "CUSTOMER_SECRET_HOST"}]}}}],
        "merged-public-then-private": [
            {"spanId": "s1", "attributes": {"session.id": "public-session-1234567890"},
             "body": {"input": {"messages": [{"role": "user", "content": "public"}]}}},
            {"spanId": "s1", "attributes": {"session.id": sid},
             "body": {"output": {"messages": [{"role": "assistant",
                                                "content": "CUSTOMER_SECRET_HOST"}]}}}],
    }
    def make_queries(events):
        def fake_queries(queries, hours, **kw):
            out = {}
            for key in queries:
                out[key] = ([{"@message": json.dumps(span)}] if key == "spans"
                            else [{"@message": json.dumps(e)} for e in events]
                            if key == "events" else [])
            return out
        return fake_queries

    for name, events in variants.items():
        monkeypatch.setattr(observability, "run_insights_queries", make_queries(events))
        observability.reset_cache()
        owner = member.get(f"/api/observability/traces/{trace_id}")
        assert owner.status_code == 200 and sid in owner.json()["meta"]["session_ids"], name
        res = other.get(f"/api/observability/traces/{trace_id}")  # cache is warm now
        assert res.status_code == 404 and "CUSTOMER_SECRET_HOST" not in res.text, name


@pytest.mark.parametrize("mode", ["asgi-2.0-disconnect", "asgi-2.4-send-error"])
def test_disconnect_while_the_upstream_read_blocks_is_cleaned_up_within_budget(
    client, ready, harness, mode
):
    """One delta, then the next upstream read BLOCKS. The response's own disconnect
    handling must close the upstream (unblocking the read) BEFORE waiting for the
    worker, so the whole request returns, persists and releases within the budget."""
    import gc

    release = threading.Event()

    class Blocking:
        closed = False

        def __init__(self):
            self.sent = False

        def __iter__(self):
            return self

        def __next__(self):
            if not self.sent:
                self.sent = True
                return {"contentBlockDelta": {"delta": {"text": "first delta "}}}
            if self.closed:
                raise StopIteration
            release.wait(timeout=30)  # a read that never returns on its own
            raise StopIteration

        def close(self):
            self.closed = True
            release.set()

    blocking = Blocking()
    harness.invoke_harness = lambda **kw: {"stream": blocking}
    gc.disable()
    try:
        cid = _open(client)
        started = time.monotonic()
        if mode == "asgi-2.0-disconnect":
            _asgi_turn(client.app, cid, spec_version="2.0", fail_send_after=None,
                       disconnect_after=2)
        else:
            _asgi_turn(client.app, cid, spec_version="2.4", fail_send_after=2,
                       disconnect_after=None)
        elapsed = time.monotonic() - started
        assert elapsed < 5, elapsed  # not the worker's 30 s block, not the 5 s cancel budget
        assert blocking.closed
        db = SessionLocal()
        try:
            conv = db.get(AssistantConversation, cid)
            assert conv.active_turn is None and conv.active_turn_token is None
            msgs = service._messages(db, cid)
            assert [m.role for m in msgs] == ["user", "assistant", "error"]
            assert msgs[1].text == "first delta " and "interrupted" in msgs[2].text
        finally:
            db.close()
    finally:
        gc.enable()


def test_live_local_turn_is_never_taken_over_and_tool_writes_are_fenced(client, ready, harness):
    """The first request is LIVE (blocked upstream) with an aged claim: a second POST is
    refused, only one invocation happens, and the first request's later tool event can
    still only be written by the claim holder. An ORPHAN (no live owner) is recovered."""
    entered, release = threading.Event(), threading.Event()

    class Blocking:
        def __init__(self):
            self.step = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.step += 1
            if self.step == 1:
                entered.set()
                release.wait(timeout=30)
                return {"contentBlockStart": {"start": {"toolUse": {"name": "late_tool",
                                                                     "toolUseId": "t"}}}}
            if self.step == 2:
                return {"contentBlockDelta": {"delta": {"text": "done"}}}
            raise StopIteration

        def close(self):
            release.set()

    calls: list = []

    def invoke(**kw):
        calls.append(kw)
        return {"stream": Blocking()}

    harness.invoke_harness = invoke
    cid = _open(client)
    results: list = []
    t = threading.Thread(target=lambda: results.append(
        client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "first"})))
    t.start()
    assert entered.wait(timeout=10)
    db = SessionLocal()
    try:  # age the LIVE claim in the ledger as if the TTL had passed
        row = db.get(AssistantConversation, cid)
        row.active_turn_started_at = datetime.now(UTC) - timedelta(
            seconds=service.TURN_CLAIM_TTL_S + 5)
        db.commit()
    finally:
        db.close()
    second = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "second"})
    assert second.status_code == 409 and second.json()["code"] == "assistant.turn_in_progress"
    assert len(calls) == 1  # no concurrent second invocation
    release.set()
    t.join(timeout=30)
    assert _sse(results[0])[-1][0] == "done"
    detail = _latest(client, cid)
    assert [m["role"] for m in detail["messages"]] == ["user", "tool", "assistant"]
    assert detail["turns"] == 1 and detail["turn_in_progress"] is None
    # ORPHAN recovery still works through the unpatched route (no live owner here)
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        row.active_turn, row.active_turn_token = 9, "dead-process-token"
        row.active_turn_started_at = datetime.now(UTC) - timedelta(
            seconds=service.TURN_CLAIM_TTL_S + 5)
        row.turns = 9
        db.commit()
    finally:
        db.close()
    harness.reply("recovered")
    harness.invoke_harness = harness.__class__.invoke_harness.__get__(harness)
    events = _turn(client, cid, "again")
    assert events[0][1]["turn"] == 10 and events[-1][0] == "done"
    # a stale token (the dead process) cannot write a tool row afterwards
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        assert service._holds_claim(db, cid, 9, "dead-process-token") is False
        assert not any(m.turn == 9 for m in service._messages(db, cid))
        assert conv.active_turn is None
    finally:
        db.close()


def test_skill_bytes_are_snapshotted_on_the_normalized_directory_and_deployed_from_a_copy(
    monkeypatch, workspace
):
    """Legacy `…/SKILL.md` source → the whole parent directory (incl. helper.py) is the
    reviewed content; the package stage proves the bytes and publishes an immutable
    content-addressed copy; the request loads the COPY, and a sibling change after
    review (or between listing and use) is refused."""
    import io

    store = {
        "skills/legacy/SKILL.md": b"---\nname: legacy\n---\nbody",
        "skills/legacy/helper.py": b"print('v1')",
    }
    copies: dict[str, bytes] = {}

    class S3:
        def list_objects_v2(self, Bucket, Prefix, **kw):
            src = store if Bucket == "bucket" else copies
            return {"Contents": [{"Key": k, "ETag": '"e"', "Size": len(v)}
                                 for k, v in sorted(src.items()) if k.startswith(Prefix)],
                    "IsTruncated": False}

        def get_object(self, Bucket, Key):
            src = store if Bucket == "bucket" else copies
            return {"Body": io.BytesIO(src[Key])}

        def put_object(self, Bucket, Key, Body, IfNoneMatch=None):
            assert Bucket == "launchpad-artifacts-test" and IfNoneMatch == "*"
            if Key in copies:
                from botocore.exceptions import ClientError

                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            copies[Key] = Body

    from app.services import workspace as workspace_mod

    monkeypatch.setattr(workspace_mod.WorkspaceContext, "client", lambda self, name, **k: S3())
    ws = workspace.__class__(account_id="111122223333", region="us-west-2",
                             resources={**RESOURCES,
                                        "artifacts_bucket": "launchpad-artifacts-test"})
    snap = service.skill_content_snapshot(ws, "s3://bucket/skills/legacy/SKILL.md")
    assert snap["source_prefix"] == "s3://bucket/skills/legacy/" and snap["object_count"] == 2
    digest = snap["content_digest"]
    # a sibling helper change (not SKILL.md) changes the reviewed identity
    store["skills/legacy/helper.py"] = b"print('v2')"
    assert service.skill_content_snapshot(ws, "s3://bucket/skills/legacy/SKILL.md")[
        "content_digest"] != digest
    store["skills/legacy/helper.py"] = b"print('v1')"
    pinned = {"path": "s3://bucket/skills/legacy/SKILL.md", **snap}
    uri = service.publish_skill_copy(ws, pinned)
    assert uri == f"s3://launchpad-artifacts-test/assistant-skills/{digest[:16]}/"
    assert set(copies) == {f"assistant-skills/{digest[:16]}/SKILL.md",
                           f"assistant-skills/{digest[:16]}/helper.py"}
    service.verify_skill_copy(ws, uri, pinned)  # the bytes the request will load
    assert service.publish_skill_copy(ws, pinned) == uri  # idempotent, conditional writes
    # source drift between review and the approved package → refused, nothing published
    store["skills/legacy/helper.py"] = b"print('evil')"
    before = dict(copies)
    with pytest.raises(RuntimeError, match="no longer match the reviewed content"):
        service.publish_skill_copy(ws, pinned)
    assert copies == before
    # tampering with an existing copy object (same key, different bytes) is refused
    copies[f"assistant-skills/{digest[:16]}/helper.py"] = b"tampered"
    with pytest.raises(RuntimeError, match="does not match the reviewed digest"):
        service.verify_skill_copy(ws, uri, pinned)


def test_package_stage_switches_the_agent_and_request_to_the_immutable_copy(
    client, ready, harness, monkeypatch
):
    cid = _open(client)
    r1 = _propose(client, harness, cid)
    body = _approve(client, cid, r1).json()
    published: dict = {}

    def fake_publish(ws, pinned):
        published["pinned"] = pinned
        return "s3://launchpad-artifacts-test/assistant-skills/dddddddddddddddd/"

    monkeypatch.setattr(service, "publish_skill_copy", fake_publish)
    monkeypatch.setattr(service, "verify_skill_copy", lambda ws, uri, pinned: None)
    monkeypatch.setattr(registry_console, "resolve_gateway_attachments", lambda tools, w: [
        {"gateway_id": "gw-1", "gateway_arn": GW_ARN, "outbound_auth": OAUTH}])
    db = SessionLocal()
    try:
        job = db.get(Job, body["job_id"])
        agent = db.get(Agent, body["agent"]["id"])
        ws = workspace_context(db.get(Workspace, DEFAULT_WORKSPACE_ID))
        ctx = StageContext(agent_id=agent.id, deployment_id=body["deployment_id"],
                           job_id=job.id, workspace=ws)
        ctx.scratch["assistant_pin"] = dict(job.payload["assistant"])
        ctx.scratch["mode"] = "create"
        ctx.log = lambda m: None
        result = harness_deployer._stage_package(ctx, agent)
        assert "immutable" in result.detail
        assert published["pinned"]["source_prefix"] == "s3://bucket/skills/meeting-summarizer/1.0.0/"
        db.expire_all()
        assert db.get(Agent, agent.id).spec["skills"] == [
            "s3://launchpad-artifacts-test/assistant-skills/dddddddddddddddd/"]
        assert db.get(Job, job.id).payload["assistant"]["skill_copies"] == {
            "s3://bucket/skills/meeting-summarizer/1.0.0/":
            "s3://launchpad-artifacts-test/assistant-skills/dddddddddddddddd/"}
        # the regenerated request loads the COPY, never the mutable source
        assert ctx.scratch["create_params"]["skills"] == [
            {"s3": {"uri": "s3://launchpad-artifacts-test/assistant-skills/dddddddddddddddd/"}}]
        # and the job-entry guard accepts the copy-rewritten spec (a resumed job)
        service.assert_job_bindings_pinned(db.get(Job, job.id).payload, db.get(Agent, agent.id), ws)
    finally:
        db.close()


def test_kb_gateway_url_and_authorizer_are_pinned_and_verified_before_target_writes(monkeypatch):
    from .test_assistant_residuals import (  # local reuse of the fake control above
        test_assistant_kb_mount_uses_an_existing_ready_gateway_and_never_creates_one as _,  # noqa
    )
    pinned = _pin()["bindings"]["resources"]["kb_gateway"]
    assert pinned["url"] == "https://kb.example/mcp" and pinned["authorizer_type"] == "CUSTOM_JWT"
    assert pinned["authorizer"] == KB_AUTHORIZER

    class Control:
        targets: list = []
        url = "https://kb.example/mcp"
        authorizer_type = "CUSTOM_JWT"
        authorizer = KB_AUTHORIZER

        def list_gateways(self, **kw):
            raise AssertionError("never list-and-create")

        def create_gateway(self, **kw):
            raise AssertionError("never create")

        def get_gateway(self, gatewayIdentifier):
            return {"gatewayId": gatewayIdentifier, "gatewayArn": RESOURCES["kb_gateway_arn"],
                    "gatewayUrl": self.url, "authorizerType": self.authorizer_type,
                    "authorizerConfiguration": self.authorizer, "status": "READY"}

    ws = workspace_context(Workspace(id="default", name="d", account_id="111122223333",
                                     region="us-west-2", bootstrap_status="ready",
                                     resources=dict(RESOURCES)))
    ok = kbgw.lookup_existing_kb_gateway(Control(), ws, expected=pinned)
    assert ok["url"] == "https://kb.example/mcp"
    # same id/ARN/READY but the live URL or inbound auth differ → refused
    for attr, value in (("url", "https://other.example/mcp"), ("authorizer_type", "AWS_IAM"),
                        ("authorizer", {"customJWTAuthorizer": {"discoveryUrl": "x",
                                                                 "allowedClients": []}})):
        control = Control()
        setattr(control, attr, value)
        with pytest.raises(RuntimeError, match="differs from the reviewed configuration"):
            kbgw.lookup_existing_kb_gateway(control, ws, expected=pinned)
    # through the real provision stage: drift → zero target writes, zero creations
    control = Control()
    control.authorizer_type = "AWS_IAM"
    monkeypatch.setattr(harness_deployer, "control_client", lambda w: control)
    monkeypatch.setattr(harness_deployer.agent_iam, "provision_execution_role",
                        lambda *a, **k: ("arn:aws:iam::1:role/x", "role ok"))
    monkeypatch.setattr(harness_deployer.kbgw, "ensure_retrieve_target",
                        lambda *a, **k: control.targets.append("retrieve"))
    monkeypatch.setattr(harness_deployer.kbgw, "sync_agentic_target",
                        lambda *a, **k: control.targets.append("agentic"))
    spec = contract.to_agent_spec(contract.parse_content(VALID_PROPOSAL)[0], _catalog())
    agent = Agent(id="a2", workspace_id="default", name="hr-helpdesk", method="harness",
                  status="deploying", spec=spec.model_dump())
    ctx = StageContext(agent_id="a2", deployment_id="d", job_id="j", workspace=ws)
    ctx.scratch["assistant_pin"] = _pin()
    ctx.log = lambda m: None
    with pytest.raises(RuntimeError, match="differs from the reviewed configuration"):
        harness_deployer._stage_provision(ctx, agent)
    assert control.targets == []


def test_winner_branches_revalidate_the_caller_and_never_mask_auth_errors(
    gated, harness, monkeypatch
):
    """A waits in the catalog read; B (same account) approves; meanwhile A's account is
    deleted and the username re-registered → A must get 401/404, never the winner."""
    admin, member, _other, ids, _preset = gated
    cid = _open(member)
    r1 = _propose(member, harness, cid)
    old_id = ids[MEMBER_CREDS["username"]]
    gate, released = threading.Event(), threading.Event()
    original = service.fetch_catalog
    n: list[int] = []

    def paced(workspace):
        n.append(1)
        if len(n) == 1:
            gate.set()
            released.wait(timeout=10)
        return original(workspace)

    monkeypatch.setattr(service, "fetch_catalog", paced)
    results: dict = {}
    t = threading.Thread(target=lambda: results.__setitem__("a", _approve(member, cid, r1)))
    t.start()
    assert gate.wait(timeout=10)
    with TestClient(admin.app, client=("127.0.0.1", 4321)) as twin:  # same account, B
        twin.cookies.set(auth_module.COOKIE_NAME, member.cookies.get(auth_module.COOKIE_NAME))
        winner = _approve(twin, cid, r1)
        assert winner.status_code == 202
    assert admin.delete(f"/api/users/{old_id}").status_code == 200
    with TestClient(admin.app, client=("127.0.0.1", 4321)) as reborn:
        _activate(MEMBER_CREDS, reborn)
    released.set()
    t.join(timeout=30)
    assert results["a"].status_code in (401, 404), results["a"].text
    assert results["a"].json()["code"] in ("auth.required", "assistant.conversation_not_found")
    assert _count(Job) == 1
    # a still-authorized caller of the same account gets the historical winner
    # (the twin did above: 202 winner, and a retry is 200)
    # catalog failure path: real low-level failure after a winner exists → winner for an
    # authorized caller, actionable 502 when there is none
    from botocore.exceptions import EndpointConnectionError

    def failing(workspace):
        raise EndpointConnectionError(endpoint_url="https://registry.example")

    monkeypatch.setattr(service, "fetch_catalog", original)
    acid = _open(admin)
    ra = _propose(admin, harness, acid, {**VALID_PROPOSAL, "name": "hr-admin-copy"})
    monkeypatch.setattr(service, "fetch_catalog", failing)
    res = _approve(admin, acid, ra)
    assert res.status_code == 502 and res.json()["code"] == "assistant.catalog_unavailable"
    assert _count(Job) == 1
    monkeypatch.setattr(service, "fetch_catalog", original)
    won = _approve(admin, acid, ra)
    assert won.status_code == 202
    monkeypatch.setattr(service, "fetch_catalog", failing)
    again = _approve(admin, acid, ra)  # winner exists → authorized caller gets it
    assert again.status_code == 200 and again.json()["job_id"] == won.json()["job_id"]



# ---------------------------------------------------------------------------
# review 4: ownership lifecycle + KB preflight ordering
# ---------------------------------------------------------------------------


def _pause_then(monkeypatch, target_module, name, gate: threading.Event, release: threading.Event):
    """Pause the FIRST call of ``target_module.name`` (signalling ``gate``) until ``release``."""
    original = getattr(target_module, name)
    calls: list[int] = []

    def paused(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            gate.set()
            assert release.wait(timeout=15)
        return result

    monkeypatch.setattr(target_module, name, paused)


def test_live_publication_is_atomic_with_the_durable_claim(client, ready, harness, monkeypatch):
    """Review-4 #1: pause the first request right after its claim returned (before it
    could do anything else), age the committed claim in the ledger, send a second
    ordinary POST: the live owner is not stolen, only one Harness invocation happens."""
    gate, release = threading.Event(), threading.Event()
    _pause_then(monkeypatch, service.hc, "new_session_id", gate, release)  # right after claim
    harness.reply("first reply")
    cid = _open(client)
    results: list = []
    t = threading.Thread(target=lambda: results.append(
        client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "first"})))
    t.start()
    assert gate.wait(timeout=10)
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        assert row.active_turn == 1 and row.active_turn_token  # committed claim …
        row.active_turn_started_at = datetime.now(UTC) - timedelta(
            seconds=service.TURN_CLAIM_TTL_S + 5)  # … aged as if the TTL had passed
        db.commit()
    finally:
        db.close()
    second = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "second"})
    assert second.status_code == 409 and second.json()["code"] == "assistant.turn_in_progress"
    release.set()
    t.join(timeout=30)
    assert _sse(results[0])[-1][0] == "done"
    assert len(harness.calls) == 1  # never two concurrent readers
    detail = _latest(client, cid)
    assert detail["turns"] == 1 and detail["turn_in_progress"] is None
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert cid not in service._LIVE_TURNS


def test_failed_claim_never_publishes_and_orphan_recovery_still_works(client, ready, harness):
    cid = _open(client)
    db = SessionLocal()
    try:  # a live-looking claim (not stale) owned by nobody in this process
        row = db.get(AssistantConversation, cid)
        row.active_turn, row.turns, row.active_turn_token = 1, 1, "other"
        row.active_turn_started_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "x"})
    assert res.status_code == 409 and cid not in service._LIVE_TURNS
    db = SessionLocal()
    try:  # now an orphan (dead process): recovered through the ordinary route
        row = db.get(AssistantConversation, cid)
        row.active_turn_started_at = datetime.now(UTC) - timedelta(
            seconds=service.TURN_CLAIM_TTL_S + 5)
        db.commit()
    finally:
        db.close()
    harness.reply("recovered")
    events = _turn(client, cid, "again")
    assert events[0][1]["turn"] == 2 and events[-1][0] == "done"
    assert cid not in service._LIVE_TURNS


def test_initial_writes_and_invocation_are_fenced_when_ownership_was_replaced(
    client, ready, harness, monkeypatch
):
    """Review-4 #2: pause after replay composition, replace ownership in the ledger
    (as another process's orphan takeover would), resume: no user row is committed,
    no data-plane call is made, the stream reports the loss."""
    gate, release = threading.Event(), threading.Event()
    _pause_then(monkeypatch, service, "compose_messages", gate, release)
    harness.reply("never")
    cid = _open(client)
    results: list = []
    t = threading.Thread(target=lambda: results.append(
        client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "first"})))
    t.start()
    assert gate.wait(timeout=10)
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        row.active_turn, row.active_turn_token = 1, "replaced-by-another-process"
        row.active_turn_started_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()
    release.set()
    t.join(timeout=30)
    events = _sse(results[0])
    assert events[-1][0] == "error" and events[-1][1]["code"] == "assistant.turn_superseded"
    assert harness.calls == []  # no invocation without current ownership
    db = SessionLocal()
    try:
        assert service._messages(db, cid) == []  # not even the user row
        row = db.get(AssistantConversation, cid)
        assert row.active_turn == 1 and row.active_turn_token == "replaced-by-another-process"
    finally:
        db.close()


def test_claim_loss_mid_stream_closes_the_upstream_and_finalizes_the_producer(
    client, ready, harness
):
    """Review-4 #3: the token is replaced while the upstream streams; the tool fence
    ends the turn — and the upstream is closed and the producer thread finished, by the
    request itself (no manual close, no rescue)."""
    release = threading.Event()

    class Stream:
        closed = False

        def __init__(self):
            self.step = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.step += 1
            if self.step == 1:
                db = SessionLocal()
                try:  # ownership replaced by "another process" before the tool event lands
                    row = db.query(AssistantConversation).one()
                    row.active_turn_token = "stolen"
                    db.commit()
                finally:
                    db.close()
                return {"contentBlockStart": {"start": {"toolUse": {"name": "t",
                                                                     "toolUseId": "1"}}}}
            if self.closed:
                raise StopIteration
            release.wait(timeout=30)  # would block forever without a close
            raise StopIteration

        def close(self):
            self.closed = True
            release.set()

    stream = Stream()
    harness.invoke_harness = lambda **kw: {"stream": stream}
    cid = _open(client)
    started = time.monotonic()
    res = client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "go"})
    assert res.status_code == 200
    events = _sse(res)
    assert events[-1][0] == "error" and events[-1][1].get("code") == "assistant.turn_superseded"
    assert time.monotonic() - started < 6
    assert stream.closed  # closed by the turn's own cleanup, not by the test
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
            t.name.startswith("assistant-turn-") for t in threading.enumerate()):
        time.sleep(0.05)
    assert not any(t.name.startswith("assistant-turn-") for t in threading.enumerate())
    db = SessionLocal()
    try:
        assert not any(m.role == "tool" for m in service._messages(db, cid))  # fenced
        assert cid not in service._LIVE_TURNS
    finally:
        db.close()


def test_kb_preflight_runs_before_any_iam_or_target_write(monkeypatch):
    """Review-4 #4: three drift cases at the reviewed KB gateway — each refused with ZERO
    IAM, target and Harness writes; the compliant case provisions normally."""

    class Iam:
        def __init__(self):
            self.calls: list[str] = []

        def __getattr__(self, name):
            def record(**kw):
                self.calls.append(name)
                if name == "get_role":
                    from botocore.exceptions import ClientError

                    raise ClientError({"Error": {"Code": "NoSuchEntity"}}, "GetRole")
                if name == "create_role":
                    return {"Role": {"Arn": "arn:aws:iam::111122223333:role/launchpad-agent-x"}}
                return {}
            return record

    class Control:
        def __init__(self, **drift):
            self.drift = drift
            self.writes: list[str] = []

        def list_gateways(self, **kw):
            self.writes.append("list_gateways")
            raise AssertionError("never list-and-create")

        def create_gateway(self, **kw):
            self.writes.append("create_gateway")
            raise AssertionError("never create")

        def get_gateway(self, gatewayIdentifier):
            base = {"gatewayId": gatewayIdentifier, "gatewayArn": RESOURCES["kb_gateway_arn"],
                    "gatewayUrl": "https://kb.example/mcp", "authorizerType": "CUSTOM_JWT",
                    "authorizerConfiguration": KB_AUTHORIZER, "status": "READY"}
            base.update(self.drift)
            return base

        def __getattr__(self, name):  # any other AgentCore call is a write we record
            def record(**kw):
                self.writes.append(name)
                return {"items": [], "targetId": "t", "status": "READY"}
            return record

    spec = contract.to_agent_spec(contract.parse_content(VALID_PROPOSAL)[0], _catalog())
    ws = workspace_context(Workspace(id="default", name="d", account_id="111122223333",
                                     region="us-west-2", bootstrap_status="ready",
                                     resources=dict(RESOURCES)))
    agent = Agent(id="a3", workspace_id="default", name="hr-helpdesk", method="harness",
                  status="deploying", spec=spec.model_dump())
    for drift in ({"gatewayUrl": "https://other.example/mcp"}, {"authorizerType": "AWS_IAM"},
                  {"status": "CREATING"}):
        control, iam = Control(**drift), Iam()
        monkeypatch.setattr(harness_deployer, "control_client", lambda w, c=control: c)
        ctx = StageContext(agent_id="a3", deployment_id="d", job_id="j", workspace=ws)
        ctx.scratch["assistant_pin"] = _pin()
        ctx.log = lambda m: None
        with pytest.raises(RuntimeError):
            harness_deployer._stage_provision(ctx, agent, iam_client=iam)
        assert iam.calls == [], drift            # no IAM write of any kind
        assert control.writes == [], drift       # no target/gateway write of any kind
    # compliant gateway: preflight passes, then the role and targets are provisioned
    control, iam = Control(), Iam()
    monkeypatch.setattr(harness_deployer, "control_client", lambda w: control)
    monkeypatch.setattr(harness_deployer.kbgw, "ensure_retrieve_target",
                        lambda c, gid, kb_id, name, desc: control.writes.append("retrieve"))
    monkeypatch.setattr(harness_deployer.kbgw, "sync_agentic_target",
                        lambda c, gid, name, kbs: control.writes.append("agentic"))
    ctx = StageContext(agent_id="a3", deployment_id="d", job_id="j", workspace=ws)
    ctx.scratch["assistant_pin"] = _pin()
    ctx.log = lambda m: None
    result = harness_deployer._stage_provision(ctx, agent, iam_client=iam)
    assert "kb targets ready" in result.detail
    assert "create_role" in iam.calls and control.writes == ["retrieve", "agentic"]
