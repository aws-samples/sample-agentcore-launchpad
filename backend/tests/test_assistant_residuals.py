# ruff: noqa: F811  — pytest fixtures are imported by name from test_assistant
"""SE-039 residual regressions (host review 2): versioned session cookie, principal
substitution, winner re-read, content-event provenance, whole-ASGI disconnect
cleanup, stale-claim takeover without publication, ingress/normalized byte caps,
durable job eligibility and pinned Harness stage execution.
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
from app.deployer import pipeline
from app.models.assistant import AssistantConversation, AssistantProposal
from app.models.ledger import ChatSession, Job, User, Workspace
from app.routers import auth as auth_module
from app.services import observability
from app.services import users as users_service
from app.services.workspace import workspace_context

from .test_assistant import (  # noqa: F401 — shared hermetic fixtures + helpers
    ADMIN_CREDS,
    BASE,
    CATALOG,
    GW_ARN,
    MEMBER_CREDS,
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
    sse_lines,
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
    # the service-level equality check, independent of the cookie: the re-resolved
    # approver is another principal under the same username
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        from app.routers.auth import Identity

        other = Identity(username=MEMBER_CREDS["username"], role="member", user_id="f" * 32)
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
    assert _count(Job) == 0

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
    # cookie names a gone id, not the owner, or the conversation left with the account
    # (N-ARCH-obs6 cascade) — never a 500 and never an approval
    assert res.status_code in (401, 404), res.text
    assert _count(Job) == 0 and swapped["new_id"] != old_id
    db = SessionLocal()
    try:
        assert db.query(AssistantProposal).filter_by(status="approved").count() == 0
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
    # a DIFFERENT unapproved revision still cannot ride on the winner — and (N16-obs40) is
    # refused as a second creation BEFORE the catalog is read at all
    r2 = client.put(f"{BASE}/conversations/{cid}/proposal",
                    json={"content": {**VALID_PROPOSAL, "name": "hr-two"}}).json()["proposal"]

    def never(ws):
        raise AssertionError("a second creation approval must refuse before the catalog read")

    monkeypatch.setattr(service, "fetch_catalog", never)
    res = _approve(client, cid, r2)
    assert res.status_code == 409 and res.json()["code"] == "assistant.already_approved"
    assert res.json()["detail"]["job_id"] == fast.json()["job_id"]
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
                "service.name": "arch.DEFAULT",
                "aws.log.group.names": "/aws/bedrock-agentcore/runtimes/r"}}}
    event = {"traceId": trace_id, "spanId": "s1", "attributes": {"session.id": sid},
             "resource": {"attributes": {"service.name": "arch.DEFAULT"}},
             "body": {"input": {"messages": [{"role": "user", "content": "CUSTOMER_SECRET_039"}]}}}

    def fake_queries(queries, hours, **kw):
        out = {}
        for key in queries:
            if key == "spans":
                out[key] = [{"@message": json.dumps(span)}]
            elif key == "events":
                out[key] = [{"@message": json.dumps(event)}]
            elif key == "ownership":
                out[key] = [{"service": "arch.DEFAULT", "record_count": "2",
                             "attributed_count": "2", "service_count": "1"}]
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
    # Public means attributable to this workspace, not an arbitrary account session.
    with SessionLocal() as db:
        db.add(ChatSession(agent_id=_preset, session_id="public-session-1234567890",
                           workspace_id=DEFAULT_WORKSPACE_ID, actor_id="public"))
        db.commit()
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


def _wait_turn_finished(cid: str, timeout: float = 15.0) -> None:
    """N-ARCH-obs15: a detached turn finishes on its driver thread; wait for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = service.live_run(cid)
        if live is None and not any(t.name.startswith("assistant-turn-")
                                    for t in threading.enumerate()):
            return
        time.sleep(0.02)
    raise AssertionError("the detached turn did not finish within the budget")


@pytest.mark.parametrize("mode", ["asgi-2.0-disconnect", "asgi-2.4-send-error"])
def test_whole_asgi_disconnect_finalizes_the_turn_deterministically(client, ready, harness, mode):
    """After the first delta the client goes away. Without any gc.collect the response's
    own cleanup detaches the subscriber in both ASGI paths — and (N-ARCH-obs15) the turn
    itself keeps running to its terminal row: the full reply and its proposal are
    persisted exactly as for a watched turn, the claim is released and the upstream is
    closed by the turn's own finalizer. No ``interrupted`` row any more."""
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
        _wait_turn_finished(cid)
        db = SessionLocal()
        try:
            conv = db.get(AssistantConversation, cid)
            assert conv.active_turn is None and conv.active_turn_token is None
            msgs = service._messages(db, cid)
            # adapted (ruled change): the turn completed — no interrupted marker
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[1].text.endswith(_block(VALID_PROPOSAL))
            latest = service.latest_proposal(db, cid)
            assert latest is not None and latest.status == "draft" and latest.revision == 1
            assert latest.content["name"] == VALID_PROPOSAL["name"]
            events = service.turn_events(db, cid, 1)
            assert events[-1].event == "done" and [e.seq for e in events] == list(
                range(1, len(events) + 1))
        finally:
            db.close()
        assert harness.streams[0].closed  # by the turn's finalizer, not by the disconnect
    finally:
        gc.enable()


def test_cancel_unblocks_a_blocked_upstream_read(ready, harness):
    """The worker is blocked inside the upstream read; cancel closes the stream so the
    read returns, then closes the generator within the bounded wait."""
    release = threading.Event()

    class Blocking:
        closed = False

        def iter_lines(self, chunk_size=None):
            if self.closed:
                return
            release.wait(timeout=10)
            yield from ()

        def close(self):
            self.closed = True
            release.set()

    blocking = Blocking()
    harness.invoke_agent_runtime = lambda **kw: {"response": blocking,
                                                 "contentType": "text/event-stream"}
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
# pinned Harness stage execution
# ---------------------------------------------------------------------------


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
    assert seen["pin"]["bindings"]["resources"]["gateways"]["gw-1"]["auth_type"] == "aws_iam"
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
    with SessionLocal() as db:
        db.add(ChatSession(agent_id=_preset, session_id="public-session-1234567890",
                           workspace_id=DEFAULT_WORKSPACE_ID, actor_id="public"))
        db.commit()
    trace_id = "b" * 32
    span = {"traceId": trace_id, "spanId": "s1", "name": "invoke_agent", "kind": "SERVER",
            "startTimeUnixNano": "1", "endTimeUnixNano": "2", "durationNano": "1",
            "attributes": {"gen_ai.system": "x"},
            "resource": {"attributes": {
                "service.name": "arch.DEFAULT",
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
                            if key == "events" else
                            [{"service": "arch.DEFAULT", "record_count": "2",
                              "attributed_count": "2", "service_count": "1"}]
                            if key == "ownership" else [])
            return out
        return fake_queries

    for name, events in variants.items():
        for event in events:
            event["traceId"] = trace_id
            attrs = event.setdefault("resource", {}).setdefault("attributes", {})
            attrs["service.name"] = "arch.DEFAULT"
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
    handling must detach the subscriber BEFORE anything waits for the worker, so the
    whole request returns within the budget — while (N-ARCH-obs15) the upstream stays
    open and the turn keeps running: once the upstream delivers, the reply is persisted
    as a completed turn and the claim released by the turn's own finalizer."""
    import gc

    release = threading.Event()

    class Blocking:
        closed = False

        def iter_lines(self, chunk_size=None):
            yield from sse_lines([{"contentBlockDelta": {"delta": {"text": "first delta "}}}])
            if self.closed:
                return
            release.wait(timeout=30)  # a read that never returns on its own

        def close(self):
            self.closed = True
            release.set()

    blocking = Blocking()
    harness.invoke_agent_runtime = lambda **kw: {"response": blocking,
                                                 "contentType": "text/event-stream"}
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
        # adapted (ruled change): the disconnect closed nothing but the subscription
        assert not blocking.closed
        db = SessionLocal()
        try:
            conv = db.get(AssistantConversation, cid)
            assert conv.active_turn == 1 and conv.active_turn_token is not None
            live = service.live_run(cid)
            assert live is not None and live.turn == 1 and live.state == "running"
        finally:
            db.close()
        release.set()  # the upstream finally answers; the detached turn completes
        _wait_turn_finished(cid)
        assert blocking.closed  # by the turn's finalizer
        db = SessionLocal()
        try:
            conv = db.get(AssistantConversation, cid)
            assert conv.active_turn is None and conv.active_turn_token is None
            msgs = service._messages(db, cid)
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[1].text == "first delta "
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
        def iter_lines(self, chunk_size=None):
            entered.set()
            release.wait(timeout=30)
            yield from sse_lines([
                {"contentBlockStart": {"start": {"toolUse": {"name": "late_tool",
                                                             "toolUseId": "t"}}}},
                {"contentBlockDelta": {"delta": {"text": "done"}}},
            ])

        def close(self):
            release.set()

    calls: list = []

    def invoke(**kw):
        calls.append(kw)
        return {"response": Blocking(), "contentType": "text/event-stream"}

    harness.invoke_agent_runtime = invoke
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
    harness.invoke_agent_runtime = harness.__class__.invoke_agent_runtime.__get__(harness)
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
    ws = workspace.__class__(account_id="111122223333", region="cn-northwest-1",
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
    _pause_then(monkeypatch, service.rt, "new_session_id", gate, release)  # right after claim
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

        def iter_lines(self, chunk_size=None):
            db = SessionLocal()
            try:  # ownership replaced by "another process" before the tool event lands
                row = db.query(AssistantConversation).one()
                row.active_turn_token = "stolen"
                db.commit()
            finally:
                db.close()
            yield from sse_lines([{"contentBlockStart": {"start": {
                "toolUse": {"name": "t", "toolUseId": "1"}}}}])
            if self.closed:
                return
            release.wait(timeout=30)  # would block forever without a close

        def close(self):
            self.closed = True
            release.set()

    stream = Stream()
    harness.invoke_agent_runtime = lambda **kw: {"response": stream,
                                                 "contentType": "text/event-stream"}
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


# ---------------------------------------------------------------------------
# N-ARCH-obs12: a disconnect while the Harness still streams is an INTERRUPTED turn —
# the ledger never carries the transport's exception text, and a proposal block that
# had already streamed completely survives the cut
# ---------------------------------------------------------------------------


class _ClosableBody:
    """An upstream body that streams ``events`` and then BLOCKS in its read. ``close()``
    — what the response's disconnect handling does (``TurnRun.signal``) — makes that read
    raise exactly the way urllib3 does on a closed ``_fp``: the live N-ARCH-obs12 row."""

    def __init__(self, events):
        self._lines = sse_lines(events)
        self._release = threading.Event()
        self.closed = False

    def iter_lines(self, chunk_size=None):
        yield from self._lines
        self._release.wait(timeout=30)
        if self.closed:
            raise AttributeError("'NoneType' object has no attribute 'read'")

    def close(self):
        self.closed = True
        self._release.set()


def _deltas(text: str, size: int = 40) -> list[dict]:
    return [{"contentBlockDelta": {"delta": {"text": text[i:i + size]}}}
            for i in range(0, len(text), size)]


def _interrupted_turn(harness, streamed: str, mode: str):
    """Stream ``streamed`` whole, then cut the turn: ``disconnect`` = the response layer
    signals the run (client gone) while the producer is blocked upstream and its read
    raises; ``generator_close`` = the body iterator is closed at the pending yield
    (GeneratorExit). Returns ``(conversation_id, events, body)``."""
    body = _ClosableBody(_deltas(streamed))
    harness.invoke_agent_runtime = lambda **kw: {"response": body,
                                                 "contentType": "text/event-stream"}
    expected_deltas = len(_deltas(streamed))
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        from app.routers.auth import Identity

        identity = Identity(username="river", role="admin")
        conv = service.create_conversation(db, row, workspace_context(row), identity, "t")
        run = service.TurnRun()
        gen = service.run_turn(db, conv, row, workspace_context(row), identity, "go", run=run)
        run.generator = gen
        got: list[dict] = []
        for event in gen:
            got.append(event)
            if sum(1 for e in got if e["event"] == "delta") == expected_deltas:
                if mode == "disconnect":
                    run.signal()  # close upstream NOW; the blocked read raises
                else:
                    gen.close()
                    break
        assert run.finished and body.closed
        return conv.id, got, body
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["disconnect", "generator_close"])
@pytest.mark.parametrize("complete_block", [True, False])
def test_disconnect_mid_stream_is_an_interrupted_turn_that_keeps_a_complete_proposal(
    ready, harness, mode, complete_block
):
    """Live 2026-10-14 (rt2-1a run A): the member's tab closed 480 s into a proposal turn;
    the ledger showed ``error: AttributeError: 'NoneType' object has no attribute 'read'``
    and the 16 k-char proposal was discarded. Now: the partial text is kept, the error row
    is the ``turn_interrupted`` marker (never a traceback), and a ``launchpad-proposal``
    block that had streamed completely is recorded as a revision; an unclosed block is
    not."""
    prose = "Here is the design for the helpdesk agent. " * 4
    block = _block(VALID_PROPOSAL)
    tail = "\n\nNext steps: review the golden tests. " * 3
    streamed = prose + block + tail if complete_block else prose + block[: len(block) // 2]
    cid, events, _body = _interrupted_turn(harness, streamed, mode)
    if mode == "disconnect":
        assert events[-1] == {"event": "error", "data": {"message": "interrupted"}}
    assert not any(e["event"] in ("done", "proposal") for e in events)
    db = SessionLocal()
    try:
        assert db.get(AssistantConversation, cid).active_turn is None  # claim released
        msgs = service._messages(db, cid)
        assert [m.role for m in msgs] == ["user", "assistant", "error"]
        assert msgs[1].text == streamed  # what the member saw is kept
        assert msgs[2].name == service.TURN_INTERRUPTED_NAME
        assert msgs[2].text == service.TURN_INTERRUPTED_TEXT and "interrupted" in msgs[2].text
        assert not any("AttributeError" in m.text or "NoneType" in m.text for m in msgs)
        latest = service.latest_proposal(db, cid)
        if complete_block:
            assert latest is not None and latest.status == "draft" and latest.revision == 1
            assert latest.content["name"] == VALID_PROPOSAL["name"]
        else:
            assert latest is None  # never a proposal from an unclosed block
    finally:
        db.close()


def test_disconnect_after_an_invalid_block_records_the_rejection_for_the_next_turn(
    ready, harness
):
    """The kept block is validated like a completed reply: an invalid one becomes an
    ``invalid`` revision with its ``proposal_rejected`` note, so the next turn replays
    the verdict and the model corrects it without the member relaying anything."""
    bad = {**VALID_PROPOSAL, "model_id": "anthropic.claude-sonnet-4-5"}
    cid, _events, _body = _interrupted_turn(harness, "Design.\n" + _block(bad), "disconnect")
    db = SessionLocal()
    try:
        latest = service.latest_proposal(db, cid)
        assert latest is not None and latest.status == "invalid"
        assert any(e.startswith("model_id:") for e in latest.validation_errors)
        names = [m.name for m in service._messages(db, cid) if m.role == "error"]
        assert names == [service.TURN_INTERRUPTED_NAME, service.PROPOSAL_REJECTED_NAME]
    finally:
        db.close()
