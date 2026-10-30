"""Agent creator + workspace visibility isolation (`app.services.agent_access`; upstream
#263 ported).

Agents carry a creator (`created_by` / `created_by_name`, Family B step 1) and follow the
workspace `resource_visibility` rule: under `isolated` a member sees an agent iff they
created it, its creator is unknown (NULL — discovered runtimes, presets, legacy rows, the
config admin) or its creator is a current admin. A hidden agent answers each route's
existing "missing agent" error, on the console, on `/v1`, and in every derived surface.

CN adaptations: no Harness convert / inbound-auth routes; the local registry is reached
through the `registry_backend` Protocol; the memory console through `memory_backend`; the
member-own API-key rule of step 1 holds in both modes.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ApiKey, ChatSession, Job, KnowledgeBase, ManagedMemory
from app.routers.apikeys import hash_key
from app.services import agent_access, visibility
from app.services.visibility import Viewer
from tests import test_visibility as tv
from tests.test_visibility import _add, _isolate, _member, _refused, _set_role

# the auth-enabled console + members of the workspace visibility suite
app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/{rid}"


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    return launched


def _agent(name: str, created_by: str | None, *, method: str = "zip_runtime",
           status: str = "active", spec: dict | None = None, **kw: Any) -> str:
    rid = f"{name.replace('-', '_')}-abcdefghij"
    return _add(Agent(
        name=name, method=method, status=status, created_by=created_by,
        created_by_name="someone" if created_by else None,
        arn=ARN.format(rid=rid), resource_id=rid,
        spec=spec if spec is not None else {
            "name": name, "method": method, "system_prompt": "p", "protocol": "http",
        },
        **kw,
    ))


@pytest.fixture
def world(app, admin, ada, bob):
    """Four agents: ada's, bob's, admin-user carol's, and one creator-unknown."""
    carol_id, carol_client = _member(app, "carol", role="admin")
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    return SimpleNamespace(
        admin=admin, ada=ada_client, bob=bob_client, carol=carol_client,
        ada_id=ada_id, bob_id=bob_id, carol_id=carol_id,
        ada_agent=_agent("ada-agent", ada_id, registry_record_id="rec-ada"),
        bob_agent=_agent("bob-agent", bob_id, registry_record_id="rec-bob"),
        carol_agent=_agent("carol-agent", carol_id),
        legacy_agent=_agent("legacy-agent", None),
    )


def _agent_ids(client) -> set[str]:
    res = client.get("/api/agents")
    assert res.status_code == 200, res.text
    return {a["id"] for a in res.json()["agents"]}


# ─── creator stamping (R1) ───────────────────────────────────────────────────


def test_create_stamps_the_caller_and_ignores_a_forged_creator(admin, bob, no_real_deploy):
    bob_id, bob_client = bob
    spec = {"name": "mine", "method": "zip_runtime", "system_prompt": "p"}
    res = bob_client.post("/api/agents", json=spec)
    assert res.status_code == 202, res.text
    assert (res.json()["agent"]["created_by"], res.json()["agent"]["created_by_name"]) == (
        bob_id, "bob")
    # the creator is server-derived: a posted one is refused (step 1), never honoured
    forged = bob_client.post("/api/agents", json={
        **spec, "name": "forged", "created_by": "u-forged", "created_by_name": "mallory"})
    assert forged.status_code == 422, forged.text
    # the config admin has no users row: a known name, no ownership key
    res = admin.post("/api/agents", json={**spec, "name": "cfg"})
    assert res.status_code == 202, res.text
    assert (res.json()["agent"]["created_by"], res.json()["agent"]["created_by_name"]) == (
        None, "operator")


# ─── the agent surfaces (R2) ─────────────────────────────────────────────────


def test_isolated_agent_list_follows_the_visibility_rule(world):
    everyone = {world.ada_agent, world.bob_agent, world.carol_agent, world.legacy_agent}
    assert _agent_ids(world.bob) == everyone  # shared: unchanged
    _isolate()
    assert _agent_ids(world.bob) == {world.bob_agent, world.carol_agent, world.legacy_agent}
    assert _agent_ids(world.ada) == {world.ada_agent, world.carol_agent, world.legacy_agent}
    assert _agent_ids(world.admin) == everyone
    assert _agent_ids(world.carol) == everyone
    # a demoted admin's agents become private
    _set_role(world.carol_id, "member")
    assert _agent_ids(world.bob) == {world.bob_agent, world.legacy_agent}


def test_hidden_agent_answers_each_routes_missing_error(world):
    hidden = world.ada_agent
    spec = {"name": "ada-agent", "method": "zip_runtime", "system_prompt": "p"}
    _isolate()
    _refused(
        world.bob, "agent.not_found",
        ("GET", f"/api/agents/{hidden}"),
        ("GET", f"/api/agents/{hidden}/versions"),
        ("GET", f"/api/agents/{hidden}/identity"),
        ("POST", f"/api/agents/{hidden}/redeploy", spec),
        ("POST", f"/api/agents/{hidden}/invoke", {"prompt": "hi"}),
        ("DELETE", f"/api/agents/{hidden}"),
        ("POST", f"/api/chat/{hidden}", {"prompt": "hi"}),
        ("GET", f"/api/chat/{hidden}/sessions"),
        ("POST", f"/api/chat/{hidden}/sessions/sess-12345678/stop"),
        ("GET", f"/api/chat/{hidden}/history?session_id=sess-12345678"),
        ("GET", f"/api/chat/{hidden}/memory?session_id=sess-12345678"),
        ("GET", f"/api/chat/{hidden}/sessions/sess-12345678/turns/1/stream"),
    )
    _refused(
        world.bob, "agent.not_active",
        ("POST", "/api/eval/runs", {"agent_id": hidden}),
        ("POST", "/api/eval/online",
         {"agent_id": hidden, "evaluators": ["Builtin.Helpfulness"]}),
        ("GET", f"/api/eval/agents/{hidden}/log-streams"),
        status=400,
    )
    _refused(world.bob, "canary.agent_not_active",
             ("POST", "/api/runtime-canaries", {"agent_id": hidden}), status=400)
    _refused(world.bob, "registry.record_not_deployed",
             ("GET", "/api/registry/records/rec-ada/live-agent-card"))
    # the agent was never touched
    db = SessionLocal()
    try:
        assert db.get(Agent, hidden).status == "active"
    finally:
        db.close()
    # visible to its creator, admins, and to everyone again once shared
    assert world.ada.get(f"/api/agents/{hidden}").status_code == 200
    assert world.admin.get(f"/api/agents/{hidden}").status_code == 200
    assert world.bob.get(f"/api/agents/{world.carol_agent}").status_code == 200
    assert world.bob.get(f"/api/agents/{world.legacy_agent}").status_code == 200
    _isolate(value=visibility.SHARED)
    assert world.bob.get(f"/api/agents/{hidden}").status_code == 200


def test_deploy_job_of_a_hidden_agent_reads_as_missing(world):
    def job_for(agent_id: str) -> str:
        return _add(Job(type="deploy_agent", payload={"agent_id": agent_id}))

    hidden_job, own_job = job_for(world.ada_agent), job_for(world.bob_agent)
    assert world.bob.get(f"/api/jobs/{hidden_job}").status_code == 200
    _isolate()
    _refused(world.bob, "job.not_found", ("GET", f"/api/jobs/{hidden_job}"))
    assert world.bob.get(f"/api/jobs/{own_job}").status_code == 200
    assert world.admin.get(f"/api/jobs/{hidden_job}").status_code == 200


# ─── /v1 keys follow their creator (R3) ──────────────────────────────────────


def _mint(client) -> str:
    res = client.post("/api/apikeys", json={"name": "k"})
    assert res.status_code == 201, res.text
    return res.json()["key"]


def _legacy_key() -> str:
    raw = "lp_live_legacyagentvis00"
    _add(ApiKey(name="legacy", prefix="lp_live_lega…", key_hash=hash_key(raw)))
    return raw


def _v1_ids(app, raw: str) -> set[str]:
    with TestClient(app) as client:
        res = client.get("/v1/agents", headers={"X-Api-Key": raw})
        assert res.status_code == 200, res.text
        return {a["id"] for a in res.json()["agents"]}


def test_v1_key_follows_its_creators_current_visibility(app, world):
    everyone = {world.ada_agent, world.bob_agent, world.carol_agent, world.legacy_agent}
    bob_key, carol_key, config_key = _mint(world.bob), _mint(world.carol), _mint(world.admin)
    legacy = _legacy_key()
    assert _v1_ids(app, bob_key) == everyone  # shared: unrestricted
    _isolate()  # takes effect on the next call — existing keys narrow at once
    assert _v1_ids(app, bob_key) == {world.bob_agent, world.carol_agent, world.legacy_agent}
    for unrestricted in (carol_key, config_key, legacy):
        assert _v1_ids(app, unrestricted) == everyone
    with TestClient(app) as client:
        for path in ("invoke", "invoke-stream"):
            res = client.post(f"/v1/agents/{world.ada_agent}/{path}",
                              headers={"X-Api-Key": bob_key}, json={"prompt": "hi"})
            assert res.status_code == 404, res.text
            assert res.json()["code"] == "agent.not_found"
    _set_role(world.carol_id, "member")  # a demoted admin's key narrows too
    assert _v1_ids(app, carol_key) == {world.carol_agent, world.legacy_agent}


def test_viewer_for_key_unit(world):
    db = SessionLocal()
    try:
        def key(created_by):
            return ApiKey(workspace_id=DEFAULT_WORKSPACE_ID, created_by=created_by)

        assert visibility.viewer_for_key(db, key(world.bob_id)) is Viewer.ALL  # shared
        _isolate()
        assert visibility.viewer_for_key(db, key(world.bob_id)) == Viewer(world.bob_id, True)
        assert visibility.viewer_for_key(db, key(world.carol_id)) is Viewer.ALL
        assert visibility.viewer_for_key(db, key(None)) is Viewer.ALL
    finally:
        db.close()


# ─── API key management (R4) ─────────────────────────────────────────────────


def test_member_manages_only_own_keys_in_both_modes(world):
    """The CN step-1 rule is already the stricter one: a key is a credential, so a member
    lists and toggles only the keys they minted, shared or isolated alike."""

    def ids(client) -> set[str]:
        return {k["id"] for k in client.get("/api/apikeys").json()["keys"]}

    def mint(client) -> str:
        return client.post("/api/apikeys", json={"name": "k"}).json()["id"]

    ada_key, bob_key, admin_key = mint(world.ada), mint(world.bob), mint(world.admin)
    legacy_id = _add(ApiKey(name="legacy", prefix="p", key_hash=hash_key("legacy-raw-x")))
    every = {ada_key, bob_key, admin_key, legacy_id}
    assert ids(world.bob) == {bob_key}
    assert ids(world.admin) == every
    _isolate()
    assert ids(world.bob) == {bob_key}  # no NULL-creator / admin keys either
    assert ids(world.admin) == every
    for action in ("disable", "enable"):
        _refused(world.bob, "apikey.not_found",
                 ("POST", f"/api/apikeys/{ada_key}/{action}"),
                 ("POST", f"/api/apikeys/{admin_key}/{action}"),
                 ("POST", f"/api/apikeys/{legacy_id}/{action}"))
    assert world.bob.post(f"/api/apikeys/{bob_key}/disable").json()["enabled"] is False
    assert world.bob.post(f"/api/apikeys/{bob_key}/enable").json()["enabled"] is True
    assert world.admin.post(f"/api/apikeys/{ada_key}/disable").status_code == 200


# ─── pins (R6) ───────────────────────────────────────────────────────────────


@pytest.fixture
def hidden_pins(world):
    """ada's KB + managed memory, and bob's own KB — all ledger rows."""
    db = SessionLocal()
    try:
        db.add(KnowledgeBase(kb_id="KBADA0001", workspace_id=DEFAULT_WORKSPACE_ID,
                             name="ada-kb", status="ACTIVE", created_by=world.ada_id,
                             created_by_name="ada"))
        db.add(KnowledgeBase(kb_id="KBBOB0001", workspace_id=DEFAULT_WORKSPACE_ID,
                             name="bob-kb", status="ACTIVE", created_by=world.bob_id,
                             created_by_name="bob"))
        db.add(ManagedMemory(workspace_id=DEFAULT_WORKSPACE_ID, memory_id="adamem-abc1234567",
                             origin="created", created_by=world.ada_id, created_by_name="ada"))
        db.commit()
    finally:
        db.close()
    return world


def _spec(name: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, "method": "zip_runtime", "system_prompt": "x", **kw}


def test_create_refuses_a_hidden_pin(hidden_pins):
    w = hidden_pins
    _isolate()
    res = w.bob.post("/api/agents", json=_spec(
        "bob-kb", knowledge_bases=[{"kb_id": "KBADA0001"}]))
    assert res.status_code == 404 and res.json()["code"] == "kb.not_found", res.text
    res = w.bob.post("/api/agents", json=_spec(
        "bob-mem", memory={"memory_id": "adamem-abc1234567"}))
    assert res.status_code == 422 and res.json()["code"] == "agent.memory_not_managed"
    res = w.bob.post("/api/agents", json=_spec(
        "bob-kb", knowledge_bases=[{"kb_id": "KBBOB0001"}]))
    assert res.status_code == 202, res.text
    db = SessionLocal()
    try:
        assert {a.name for a in db.query(Agent)}.isdisjoint({"bob-mem"})
    finally:
        db.close()


def test_redeploy_keeps_an_existing_hidden_pin_but_refuses_adding_one(hidden_pins):
    w = hidden_pins
    pinned = {"name": "bob-pinned", "method": "zip_runtime", "system_prompt": "x",
              "knowledge_bases": [{"kb_id": "KBADA0001"}]}
    agent_id = _agent("bob-pinned", w.bob_id, spec=pinned)
    plain_id = _agent("bob-plain", w.bob_id,
                      spec={"name": "bob-plain", "method": "zip_runtime", "system_prompt": "x"})
    _isolate()
    # pinned while shared → re-publishing the same pins keeps working
    res = w.bob.post(f"/api/agents/{agent_id}/redeploy", json={**pinned, "system_prompt": "y"})
    assert res.status_code == 202, res.text
    res = w.bob.post(f"/api/agents/{plain_id}/redeploy", json=_spec(
        "bob-plain", knowledge_bases=[{"kb_id": "KBADA0001"}]))
    assert res.status_code == 404 and res.json()["code"] == "kb.not_found", res.text


def test_check_pins_unit(hidden_pins):
    w = hidden_pins
    ctx = SimpleNamespace(id=DEFAULT_WORKSPACE_ID, resources={})
    kb = {"knowledge_bases": [{"kb_id": "KBADA0001"}]}
    mem = {"memory": {"memory_id": "adamem-abc1234567"}}
    db = SessionLocal()
    try:
        bob = Viewer(w.bob_id, True)
        agent_access.check_pins(db, Viewer.ALL, ctx, {**kb, **mem})  # unfiltered: no check
        agent_access.check_pins(db, Viewer(w.bob_id, False), ctx, {**kb, **mem})
        agent_access.check_pins(db, bob, ctx, {**kb, **mem}, previous_spec={**kb, **mem})
        with pytest.raises(Exception) as kb_err:
            agent_access.check_pins(db, bob, ctx, kb)
        assert kb_err.value.code == "kb.not_found"
        with pytest.raises(Exception) as mem_err:
            agent_access.check_pins(db, bob, ctx, mem, previous_spec={})
        assert mem_err.value.code == "agent.memory_not_managed"
        agent_access.check_pins(db, Viewer(w.ada_id, True), ctx, {**kb, **mem})
    finally:
        db.close()


# ─── cross-module mutations (R7) ─────────────────────────────────────────────


def test_visible_run_and_canary_cannot_mutate_a_hidden_agent(monkeypatch, world):
    from app.evaluation.models import EvalRun
    from app.optimization import canary_routers
    from app.optimization.models import RuntimeCanary

    hidden = world.ada_agent
    run = _add(EvalRun(agent_id=hidden, agent_name="ada-agent", status="completed",
                       created_by=world.bob_id))
    can = _add(RuntimeCanary(name="c", champion_agent_id=hidden,
                             champion_agent_name="ada-agent", challenger_agent_id=hidden,
                             challenger_agent_name="ada-agent", created_by=world.bob_id))
    monkeypatch.setattr(canary_routers.canary_service, "stage_not_ready_reason",
                        lambda *a: "not now")
    _isolate()
    _refused(world.bob, "agent.not_found",
             ("POST", f"/api/eval/runs/{run}/recommendations/r1/accept"),
             ("POST", f"/api/runtime-canaries/{can}/action", {"action": "setup"}),
             ("POST", f"/api/runtime-canaries/{can}/action", {"action": "complete"}),
             ("POST", f"/api/runtime-canaries/{can}/action", {"action": "rollback"}))
    # the owner's own cleanup is not an agent mutation: it passes the agent gate
    # (and stops at the stubbed stage check)
    res = world.bob.post(f"/api/runtime-canaries/{can}/action", json={"action": "cleanup"})
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "canary.stage_not_ready"
    # an admin passes the gate
    res = world.admin.post(f"/api/runtime-canaries/{can}/action", json={"action": "setup"})
    assert res.json()["code"] == "canary.stage_not_ready"


def test_visible_run_does_not_disclose_a_hidden_agents_prompt(monkeypatch, world):
    from app.evaluation import recommendations
    from app.evaluation.models import EvalRun

    monkeypatch.setattr(recommendations, "evaluator_options", lambda *a, **k: ([], []))
    run = _add(EvalRun(agent_id=world.ada_agent, agent_name="ada-agent",
                       status="completed", created_by=world.bob_id))
    url = f"/api/eval/runs/{run}/recommendation-inputs"
    assert world.bob.get(url).json()["source"] == "spec"  # shared: the agent's spec
    _isolate()
    inputs = world.bob.get(url).json()
    assert (inputs["source"], inputs["system_prompt"]) == ("manual", "")
    assert world.admin.get(url).json()["source"] == "spec"


# ─── observability (R5) ──────────────────────────────────────────────────────


@pytest.fixture
def spans(monkeypatch, world):
    from app.services import observability as obs

    ada_sid, bob_sid, chat_sid = "sess-ada-0001", "sess-bob-0001", "sess-adachat-01"
    _add(ChatSession(agent_id=world.ada_agent, session_id=chat_sid, actor_id="ada"))
    rows = [
        {"trace_id": "a" * 32, "service": "ada_agent.DEFAULT", "agent": "ada-agent",
         "session_id": ada_sid, "status": "ok"},
        {"trace_id": "b" * 32, "service": "bob_agent.DEFAULT", "agent": "bob-agent",
         "session_id": bob_sid, "status": "ok"},
        # no span attribution, but the ledger says it is ada's chat
        {"trace_id": "c" * 32, "service": "unknown-svc", "agent": "unknown-svc",
         "session_id": chat_sid, "status": "ok"},
    ]
    monkeypatch.setattr(obs, "list_traces", lambda *a, **k: {"traces": list(rows)})
    monkeypatch.setattr(obs, "list_sessions", lambda *a, **k: {"sessions": list(rows)})
    by_trace = {r["trace_id"]: r for r in rows}
    monkeypatch.setattr(obs, "get_trace", lambda tid, *a, **k: {
        "trace_id": tid, "spans": [],
        "meta": {"service": by_trace[tid]["service"],
                 "session_ids": [by_trace[tid]["session_id"]]}})
    by_session = {r["session_id"]: r for r in rows}
    monkeypatch.setattr(obs, "get_session", lambda sid, *a, **k: {
        "session_id": sid, "traces": [by_session[sid]], "transcript": {"available": False}})
    monkeypatch.setattr(obs, "get_session_transcript", lambda sid, db, ws, agent_id=None, **_: {
        "session_id": sid, "transcript": {"available": False, "agent_id": agent_id}})
    monkeypatch.setattr(obs, "run_insights_queries", lambda *a, **k: {
        "ownership": [{"service": "bob_agent.DEFAULT", "record_count": "1",
                       "attributed_count": "1", "service_count": "1"}],
    })
    return SimpleNamespace(world=world, ada_sid=ada_sid, bob_sid=bob_sid, chat_sid=chat_sid)


def test_observability_drops_and_404s_hidden_agents(spans):
    w = spans.world
    # A concrete unknown service cannot borrow a local chat id as authorization.
    assert len(w.bob.get("/api/observability/traces").json()["traces"]) == 2
    _isolate()
    for path, key in (("traces", "traces"), ("sessions", "sessions")):
        rows = w.bob.get(f"/api/observability/{path}").json()[key]
        assert [r["session_id"] for r in rows] == [spans.bob_sid], path
        assert len(w.admin.get(f"/api/observability/{path}").json()[key]) == 3
    _refused(w.bob, "observability.trace_not_found",
             ("GET", f"/api/observability/traces/{'a' * 32}"),
             ("GET", f"/api/observability/traces/{'c' * 32}"))
    _refused(w.bob, "observability.session_not_found",
             ("GET", f"/api/observability/sessions/{spans.ada_sid}"),
             ("GET", f"/api/observability/sessions/{spans.chat_sid}"),
             ("GET", f"/api/observability/sessions/{spans.chat_sid}/transcript"),
             ("GET", f"/api/observability/sessions/{spans.bob_sid}/transcript"
                     f"?agent_id={w.ada_agent}"),
             ("POST", f"/api/observability/sessions/{spans.chat_sid}/evaluate",
              {"evaluator_ids": ["Builtin.Helpfulness"]}))
    # CN-only Chat trace rail (N-CHAT-obs2): a hidden agent's chat session reads as missing
    _refused(w.bob, "chat.session_not_found", ("GET", f"/api/traces/{spans.chat_sid}"))
    assert w.bob.get(f"/api/observability/traces/{'b' * 32}").status_code == 200
    assert w.bob.get(f"/api/observability/sessions/{spans.bob_sid}").status_code == 200
    assert w.admin.get(f"/api/observability/sessions/{spans.ada_sid}").status_code == 200


def test_replay_session_transcript_follows_the_run_visibility(monkeypatch, world):
    """N17-obs432: a dataset-replay session reads through the run that replayed it (real
    service, content logs stubbed); a member the run is hidden from gets the existing 404,
    as for any session they may not see — even on an agent they do see."""
    from app.evaluation.models import EvalRun
    from app.services import memory_backend
    from app.services import observability as obs

    sid = "replay-session-ada-run-0001"
    run_id = _add(EvalRun(agent_id=world.legacy_agent, agent_name="legacy-agent",
                          dataset_id="ds0000000001", status="completed", session_ids=[sid],
                          scenario_map={sid: {"scenario_id": "s1"}}, created_by=world.ada_id))
    # memory off: the content logs are the only record (restored after the test)
    monkeypatch.setattr(memory_backend, "_backend", None)
    monkeypatch.setattr(obs, "_eval_content_log_group", lambda ag, ws: f"/logs/{ag.id}")
    monkeypatch.setattr(obs, "eval_turns_from_content_logs", lambda *a, **k: [
        {"role": "USER", "text": "q", "at": "t"}, {"role": "ASSISTANT", "text": "a", "at": "t"}])
    url = f"/api/observability/sessions/{sid}/transcript"
    for client in (world.bob, world.ada, world.admin):  # shared: everyone
        body = client.get(url).json()["transcript"]
        assert (body["available"], body["source"], body["run_id"]) == (True, "eval", run_id)
        assert [t["text"] for t in body["turns"]] == ["q", "a"]
    _isolate()
    _refused(world.bob, "observability.session_not_found", ("GET", url),
             ("GET", f"{url}?agent_id={world.legacy_agent}"))
    for client in (world.ada, world.admin):
        assert client.get(url).json()["transcript"]["run_id"] == run_id


class _StubRegistry:
    """The local registry Protocol over a fixed record list."""

    def __init__(self, records):
        self.records = records
        self.deleted: list[str] = []

    def list_records(self, workspace, descriptor_type=None, status=None):
        return list(self.records)

    def list_discoverable_records(self, workspace, descriptor_type=None):
        return list(self.records)

    def search_records(self, workspace, query, max_results=20):
        return list(self.records)

    def get_record(self, workspace, record_id):
        return next(r for r in self.records if r["recordId"] == record_id)

    def delete_record(self, workspace, record_id):
        self.deleted.append(record_id)


def test_session_previews_blank_hidden_agents_sessions(spans, monkeypatch):
    from app.services import observability as obs

    w = spans.world
    read: list = []
    monkeypatch.setattr(obs, "run_insights_queries", lambda *a, **k: {
        "ownership": [{"service": "bob_agent.DEFAULT", "record_count": "1",
                       "attributed_count": "1", "service_count": "1"}],
    })

    def fake_transcripts(sessions, workspace, agent_id=None, **_):
        read.append(list(sessions))
        return {sid: {"available": True, "turns": [{"role": "USER", "text": "q"}]}
                for sid in sessions}

    monkeypatch.setattr(obs, "session_transcripts", fake_transcripts)
    url = "/api/observability/session-previews"
    body = {"sessions": [{"session_id": spans.bob_sid}, {"session_id": spans.chat_sid}]}
    _isolate()
    res = w.bob.post(url, json=body)
    assert res.status_code == 200, res.text
    rows = {p["session_id"]: p for p in res.json()["previews"]}
    assert rows[spans.bob_sid]["input"] == "q"
    # ada's chat keeps its row but is never read for bob
    assert (rows[spans.chat_sid]["available"], rows[spans.chat_sid]["reason"]) == (
        False, "not_found")
    assert read == [[spans.bob_sid]]
    # the hidden agent named as the attribution → the same 404 the transcript route gives
    res = w.bob.post(url, json={**body, "agent_id": w.ada_agent})
    assert res.status_code == 404 and res.json()["code"] == "observability.session_not_found"
    assert all(p["available"] for p in w.admin.post(url, json=body).json()["previews"])


def test_overview_active_sessions_and_a2a_count_are_per_viewer(monkeypatch, world):
    import app.routers.overview as overview_mod
    from app.services import registry_backend

    records = [
        {"recordId": "rec-ada", "descriptorType": "A2A", "status": "APPROVED"},
        {"recordId": "rec-bob", "descriptorType": "A2A", "status": "APPROVED"},
    ]
    registry_backend.set_backend(_StubRegistry(records))
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    overview_mod._cache.clear()
    now = datetime.now(UTC)
    _add(ChatSession(agent_id=world.ada_agent, session_id="s-ada-ov", last_at=now))
    _add(ChatSession(agent_id=world.bob_agent, session_id="s-bob-ov", last_at=now))
    try:
        shared = world.bob.get("/api/overview").json()
        assert (shared["active_sessions"], shared["registry_assets"]["agents"]) == (2, 2)
        _isolate()
        isolated = world.bob.get("/api/overview").json()
        assert (isolated["active_sessions"], isolated["registry_assets"]["agents"]) == (1, 1)
        as_admin = world.admin.get("/api/overview").json()
        assert (as_admin["active_sessions"], as_admin["registry_assets"]["agents"]) == (2, 2)
    finally:
        registry_backend.set_backend(None)
        overview_mod._cache.clear()


# ─── memory console (R5) ─────────────────────────────────────────────────────


@pytest.fixture
def memory_world(monkeypatch, world):
    from app.services import memory_backend as mb

    ada_actor, bob_actor = f"{world.ada_agent}__ada", f"{world.bob_agent}__bob"
    _add(ChatSession(agent_id=world.ada_agent, session_id="s-ada-mem", actor_id="default"))
    fake = SimpleNamespace(
        list_actors=lambda *a, **k: {
            "items": [mb.decode_actor(x) for x in (ada_actor, bob_actor, "default")],
            "next_token": None},
        list_sessions=lambda *a, **k: {
            "items": [{"session_id": "s-ada-mem"}, {"session_id": "s-other"}],
            "next_token": None},
        list_events=lambda *a, **k: {"items": [], "next_token": None},
        resolve_namespaces=lambda _ws, actor, *a, **k: [
            {"namespace": f"/facts/{actor}", "strategy_id": "s1", "resolvable": True}],
        list_records=lambda *a, **k: {"items": [], "next_token": None},
        search_records=lambda *a, **k: {"items": []},
        list_extraction_jobs=lambda *a, **k: {
            "items": [{"actor_id": ada_actor}, {"actor_id": bob_actor}], "next_token": None},
    )
    monkeypatch.setattr(mb, "get_backend", lambda: fake)
    return SimpleNamespace(world=world, ada_actor=ada_actor, bob_actor=bob_actor)


def test_memory_console_hides_hidden_agents_actors(memory_world):
    m = memory_world
    w = m.world
    assert len(w.bob.get("/api/memory/actors").json()["items"]) == 3
    _isolate()
    actors = [a["actor_id"] for a in w.bob.get("/api/memory/actors").json()["items"]]
    # N17-obs531: the unscoped legacy "default" actor is nobody's own and is dropped too
    # (`tests/test_memory_actor_scope.py` pins the human-segment rule)
    assert actors == [m.bob_actor]
    assert len(w.admin.get("/api/memory/actors").json()["items"]) == 3
    a = m.ada_actor
    _refused(w.bob, "memory.actor_not_found",
             ("GET", f"/api/memory/sessions?actor_id={a}"),
             ("GET", f"/api/memory/events?actor_id={a}&session_id=s-1"),
             ("GET", "/api/memory/events?actor_id=default&session_id=s-ada-mem"),
             ("GET", "/api/memory/sessions?actor_id=default"),
             ("GET", f"/api/memory/namespaces?actor_id={a}"),
             ("GET", f"/api/memory/records?actor_id={a}"),
             ("GET", f"/api/memory/records?namespace=/facts/{a}"),
             ("POST", "/api/memory/records/search", {"query": "q", "actor_id": a}),
             ("POST", "/api/memory/records/search", {"query": "q", "namespace": f"/facts/{a}"}),
             ("GET", f"/api/memory/extraction-jobs?actor_id={a}"),
             ("DELETE", f"/api/memory/records/r1?actor_id={a}"))
    # bob's own actor stays, minus the sessions the ledger gives ada's (hidden) agent
    sessions = w.bob.get(f"/api/memory/sessions?actor_id={m.bob_actor}").json()["items"]
    assert [s["session_id"] for s in sessions] == ["s-other"]
    jobs = w.bob.get("/api/memory/extraction-jobs").json()["items"]
    assert [j["actor_id"] for j in jobs] == [m.bob_actor]
    assert w.bob.get(f"/api/memory/records?actor_id={m.bob_actor}").status_code == 200


# ─── leaks of hidden agents (R8) ─────────────────────────────────────────────


def test_name_exists_names_the_holder_only_when_visible(world):
    _isolate()
    hidden = world.bob.post("/api/agents", json=_spec("ada-agent"))
    assert hidden.status_code == 409 and hidden.json()["code"] == "agent.name_exists"
    assert hidden.json()["detail"] == {"agent_id": None}  # uniqueness stays workspace-wide
    visible = world.bob.post("/api/agents", json=_spec("legacy-agent"))
    assert visible.json()["detail"] == {"agent_id": world.legacy_agent}
    assert world.admin.post("/api/agents", json=_spec("ada-agent")).json()[
        "detail"] == {"agent_id": world.ada_agent}


def test_discovery_does_not_name_a_hidden_owner(monkeypatch, world):
    def candidate(agent_id, name):
        return {"runtime_id": f"rt-{name}", "managed_agent_id": agent_id,
                "managed_agent_name": name, "managed_agent_method": "zip_runtime",
                "importable": True, "reason_code": None, "reason": None}

    monkeypatch.setattr(agents_router, "control_client", lambda _ctx=None: object())
    monkeypatch.setattr(agents_router, "scan_runtimes", lambda *a, **k: [
        candidate(world.ada_agent, "ada-agent"), candidate(world.bob_agent, "bob-agent")])
    monkeypatch.setattr(agents_router, "scan_harnesses", lambda *a, **k: ([], None))
    monkeypatch.setattr(agents_router, "import_runtimes", lambda *a, **k: {
        "imported": [], "updated": [], "failed": [], "already_managed": [
            {"runtime_id": "rt-ada", "agent_id": world.ada_agent, "agent_name": "ada-agent"}]})
    monkeypatch.setattr(agents_router, "import_harnesses", lambda *a, **k: {
        "imported": [], "updated": [], "failed": [], "already_managed": []})
    _isolate()
    runtimes = world.bob.get("/api/agents/discovery").json()["runtimes"]
    ada_row, bob_row = runtimes
    assert (ada_row["managed_agent_id"], ada_row["managed_agent_name"]) == (None, None)
    assert ada_row["managed"] is True and ada_row["importable"] is False
    assert ada_row["managed_agent_method"] is None
    assert ada_row["reason_code"] == "already-managed"
    assert bob_row["managed_agent_id"] == world.bob_agent
    res = world.bob.post("/api/agents/discovery/import", json={"runtime_ids": ["rt-ada"]})
    assert res.json()["already_managed"] == [
        {"runtime_id": "rt-ada", "agent_id": None, "agent_name": None, "managed": True}]
    admin_rows = world.admin.get("/api/agents/discovery").json()["runtimes"]
    assert admin_rows[0]["managed_agent_id"] == world.ada_agent


def test_kb_used_by_omits_hidden_agent_names(world):
    from app.services import knowledge_backend

    # bob's own KB (N17-obs530: a member's delete reaches the attached check only on a KB
    # they created — another creator's answers 403 first)
    _add(KnowledgeBase(kb_id="KB1", workspace_id=DEFAULT_WORKSPACE_ID, name="kb1",
                       status="ACTIVE", created_by=world.bob_id, created_by_name="bob"))
    db = SessionLocal()
    try:
        for agent_id in (world.ada_agent, world.bob_agent):
            row = db.get(Agent, agent_id)
            row.spec = {**row.spec, "knowledge_bases": [{"kb_id": "KB1"}]}
        db.commit()
    finally:
        db.close()
    backend = SimpleNamespace(
        list_kbs=lambda _ws: [{"kb_id": "KB1", "attached_agents": ["ada-agent", "bob-agent"]}],
        get_kb_detail=lambda _ws, kb_id: {
            "kb_id": kb_id, "attached_agents": ["ada-agent", "bob-agent"]},
    )
    knowledge_backend.set_backend(backend)
    try:
        _isolate()
        item = world.bob.get("/api/knowledge-bases").json()["items"][0]
        assert (item["attached_agents"], item["attached_agent_count"]) == (["bob-agent"], 2)
        detail = world.bob.get("/api/knowledge-bases/KB1").json()
        assert (detail["attached_agents"], detail["attached_agent_count"]) == (["bob-agent"], 2)
        refused = world.bob.delete("/api/knowledge-bases/KB1")
        assert refused.status_code == 409
        assert refused.json()["detail"] == {"agents": ["bob-agent"]}
        assert "2 agent(s)" in refused.json()["message"]  # the count stays truthful
        listed = world.admin.get("/api/knowledge-bases").json()["items"][0]
        assert listed["attached_agents"] == ["ada-agent", "bob-agent"]
    finally:
        knowledge_backend.set_backend(None)


def test_memory_used_by_omits_hidden_agents(monkeypatch, world):
    from app.services import memory_backend as mb

    pin = {"memory": {"memory_id": "sharedmem-abc1234567"}}
    db = SessionLocal()
    try:
        for agent_id in (world.ada_agent, world.bob_agent):
            row = db.get(Agent, agent_id)
            row.spec = {**row.spec, **pin}
        db.add(ManagedMemory(workspace_id=DEFAULT_WORKSPACE_ID,
                             memory_id="sharedmem-abc1234567", origin="created"))
        db.commit()
    finally:
        db.close()
    fake = SimpleNamespace(
        list_memory_resources=lambda _ws: {"items": [{"id": "sharedmem-abc1234567"}]},
    )
    monkeypatch.setattr(mb, "get_backend", lambda: fake)
    _isolate()
    item = world.bob.get("/api/memory/resources").json()["items"][0]
    assert [a["id"] for a in item["agents"]] == [world.bob_agent]
    refused = world.bob.delete("/api/memory/resources/sharedmem-abc1234567")
    assert refused.status_code == 409 and refused.json()["code"] == "memory.in_use"
    assert [a["id"] for a in refused.json()["detail"]["agents"]] == [world.bob_agent]
    assert "2 agent(s)" in refused.json()["message"]


def test_connection_refs_omit_hidden_agents(monkeypatch, world):
    import app.routers.identity as identity_router
    from app.core.errors import AppError
    from app.services import identity_providers

    refs = [{"type": "agent", "id": world.ada_agent, "name": "ada-agent"},
            {"type": "agent", "id": world.bob_agent, "name": "bob-agent"},
            {"type": "gateway_target", "id": "t1", "name": "target"}]
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx=None: object())
    monkeypatch.setattr(identity_router.gateway_targets, "bound_targets_safe",
                        lambda *a: [])
    monkeypatch.setattr(identity_router.gateway_targets, "list_targets", lambda *a: [])
    monkeypatch.setattr(identity_providers, "list_connections", lambda *a, **k: [
        {"name": "c", "referenced_by": list(refs)}])
    monkeypatch.setattr(identity_providers, "get_connection", lambda *a, **k: {
        "name": "c", "referenced_by": list(refs)})

    def refuse(*a, **k):
        raise AppError("identity.connection_referenced", "referenced",
                       detail={"referenced_by": list(refs)}, status_code=409)

    monkeypatch.setattr(identity_providers, "delete_connection", refuse)
    _isolate()
    visible = [{"type": "agent", "id": world.bob_agent, "name": "bob-agent"}, refs[2]]
    listed = world.bob.get("/api/identity/connections").json()["connections"][0]
    assert listed["referenced_by"] == visible
    detail = world.bob.get("/api/identity/connections/oauth2/c").json()
    assert detail["referenced_by"] == visible
    refused = world.admin.delete("/api/identity/connections/oauth2/c")
    assert refused.status_code == 409
    assert refused.json()["detail"]["referenced_by"] == refs  # an admin sees every agent


def test_registry_a2a_records_of_hidden_agents(world):
    from app.services import registry_backend

    records = [{"recordId": "rec-ada", "name": "ada-agent", "descriptorType": "A2A"},
               {"recordId": "rec-bob", "name": "bob-agent", "descriptorType": "A2A"},
               {"recordId": "rec-mcp", "name": "tool", "descriptorType": "MCP"}]
    stub = _StubRegistry(records)
    registry_backend.set_backend(stub)
    try:
        _isolate()
        for url in ("/api/registry/records", "/api/registry/records/search?q=a",
                    "/api/registry/records/discoverable"):
            ids = {r["record_id"] for r in world.bob.get(url).json()["records"]}
            assert ids == {"rec-bob", "rec-mcp"}, url
        _refused(world.bob, "registry.record_not_found",
                 ("GET", "/api/registry/records/rec-ada"),
                 ("POST", "/api/registry/records/rec-ada/action", {"action": "submit"}),
                 ("PUT", "/api/registry/records/rec-ada", {"description": "x"}),
                 ("POST", "/api/registry/records/rec-ada/reimport"),
                 ("DELETE", "/api/registry/records/rec-ada"))
        # N17-obs248 / rev-r23: a member's DECISION is refused by role before the
        # visibility check — the same 403 for a hidden, a visible and a missing record
        for rid in ("rec-ada", "rec-bob", "rec-missing"):
            res = world.bob.post(f"/api/registry/records/{rid}/action", json={"action": "approve"})
            assert res.status_code == 403, (rid, res.text)
            assert res.json()["code"] == "registry.approval_admin_only"
        assert stub.deleted == []
        assert world.bob.get("/api/registry/records/rec-bob").status_code == 200
        ids = {r["record_id"] for r in world.admin.get("/api/registry/records").json()["records"]}
        assert ids == {"rec-ada", "rec-bob", "rec-mcp"}
    finally:
        registry_backend.set_backend(None)


def test_policy_entity_picker_omits_hidden_agents_while_policies_stay_shared(world):
    """CN extra: D20 policies are workspace governance and stay shared under isolation;
    only the agent picker of the policy editor is filtered, so no hidden id/name leaks."""
    from app.services import policy_backend

    if policy_backend.backend_or_none() is None:
        pytest.skip("the Cedar engine is not registered in this test process")
    engine = world.admin.post("/api/policy-engines", json={"name": "vis_engine"})
    if engine.status_code == 503:
        pytest.skip("the Cedar engine is not registered in this test process")
    assert engine.status_code == 201, engine.text
    entities = f"/api/policy-engines/{engine.json()['id']}/entities"
    res = world.bob.get(entities)
    before = {a["id"] for a in res.json()["agents"]}
    assert {world.ada_agent, world.bob_agent} <= before
    _isolate()
    after = {a["id"] for a in world.bob.get(entities).json()["agents"]}
    assert world.ada_agent not in after and world.bob_agent in after
    assert world.ada_agent in {a["id"] for a in world.admin.get(entities).json()["agents"]}


# ─── internal paths stay unfiltered (R9) ─────────────────────────────────────


def test_internal_defaults_see_everything(world):
    _isolate()
    db = SessionLocal()
    try:
        assert agent_access.agent_in(db, DEFAULT_WORKSPACE_ID, world.ada_agent) is not None
        assert agent_access.hidden_agent_ids(db, Viewer.ALL, DEFAULT_WORKSPACE_ID) == set()
        assert not agent_access.HiddenAgents(db, Viewer.ALL, DEFAULT_WORKSPACE_ID)
        bob = Viewer(world.bob_id, True)
        assert agent_access.hidden_agent_ids(db, bob, DEFAULT_WORKSPACE_ID) == {world.ada_agent}
        assert agent_access.agent_in(
            db, DEFAULT_WORKSPACE_ID, world.ada_agent, viewer=bob) is None
        # a foreign workspace id never resolves, whatever the viewer
        assert agent_access.agent_in(db, "elsewhere", world.bob_agent) is None
        # a deleted row still counts as hidden history, but reads as missing by id
        db.get(Agent, world.ada_agent).status = "deleted"
        db.commit()
        assert agent_access.agent_in(db, DEFAULT_WORKSPACE_ID, world.ada_agent) is None
        assert agent_access.agent_in(
            db, DEFAULT_WORKSPACE_ID, world.ada_agent, include_deleted=True) is not None
        assert world.ada_agent in agent_access.hidden_agent_ids(db, bob, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
