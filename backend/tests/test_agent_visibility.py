"""Agent creator + workspace visibility isolation (`app.services.agent_access`).

Agents get a real creator (`created_by` / `created_by_name`) on every console
creation path and follow the workspace `resource_visibility` rule: under
`isolated` a member sees an agent iff they created it, its creator is unknown
(NULL — discovered runtimes, presets, legacy rows, the config admin) or its
creator is a current admin. A hidden agent answers each route's existing
"missing agent" error, on the console, on `/v1`, and in every derived surface.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ApiKey, ChatSession, Job, ManagedMemory
from app.routers.apikeys import hash_key
from app.services import agent_access, creators, visibility
from app.services.creators import Creator
from app.services.visibility import Viewer
from tests import test_visibility as tv
from tests.test_visibility import _add, _isolate, _member, _refused, _set_role

# the auth-enabled console + members of the workspace visibility suite
app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/{rid}"


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
    res = bob_client.post("/api/agents", json={
        "name": "bob-made", "method": "harness", "system_prompt": "x",
        "created_by": "forged", "created_by_name": "mallory",
    })
    assert res.status_code == 202, res.text
    agent = res.json()["agent"]
    assert (agent["created_by"], agent["created_by_name"]) == (bob_id, "bob")
    assert agent["owner"] == "river"  # provenance label untouched

    # the config admin has no users row: (None, name)
    res = admin.post("/api/agents", json={
        "name": "admin-made", "method": "harness", "system_prompt": "x",
    })
    assert res.status_code == 202, res.text
    assert (res.json()["agent"]["created_by"], res.json()["agent"]["created_by_name"]) == (
        None, "operator",
    )
    listed = {a["name"]: a for a in admin.get("/api/agents").json()["agents"]}
    assert listed["bob-made"]["created_by_name"] == "bob"
    detail = admin.get(f"/api/agents/{agent['id']}").json()
    assert detail["created_by"] == bob_id


def test_conversion_twin_is_created_by_the_converter(monkeypatch, ada, bob):
    import app.services.harness_convert as hc
    from tests.test_harness_convert import MAIN_PY, PYPROJECT

    ada_id, _ = ada
    bob_id, bob_client = bob
    source = _agent("ada-harness", ada_id, method="harness",
                    spec={"name": "ada-harness", "method": "harness", "system_prompt": "sp"})
    monkeypatch.setattr(hc, "export_harness",
                        lambda arn: {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT})
    res = bob_client.post(f"/api/agents/{source}/convert")  # shared: bob sees ada's
    assert res.status_code == 202, res.text
    twin = res.json()["agent"]
    assert (twin["created_by"], twin["created_by_name"]) == (bob_id, "bob")

    # isolated: ada sees her source but not bob's twin among its conversions
    _isolate()
    _, ada_client = ada
    assert ada_client.get(f"/api/agents/{source}/conversions").json()["conversions"] == []
    # and the in-flight mutex no longer names the hidden twin
    refused = ada_client.post(f"/api/agents/{source}/convert")
    assert refused.status_code == 409, refused.text
    assert "ada-harness-rt" not in refused.json()["message"]


# ─── list + by-id (R2) ───────────────────────────────────────────────────────


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
        ("GET", f"/api/agents/{hidden}/conversions"),
        ("GET", f"/api/agents/{hidden}/versions"),
        ("GET", f"/api/agents/{hidden}/identity"),
        ("POST", f"/api/agents/{hidden}/redeploy", spec),
        ("POST", f"/api/agents/{hidden}/inbound-auth", {"inbound_auth": None}),
        ("POST", f"/api/agents/{hidden}/convert"),
        ("POST", f"/api/agents/{hidden}/invoke", {"prompt": "hi"}),
        ("DELETE", f"/api/agents/{hidden}"),
        ("POST", f"/api/chat/{hidden}", {"prompt": "hi"}),
        ("GET", f"/api/chat/{hidden}/sessions"),
        ("POST", f"/api/chat/{hidden}/sessions/sess-12345678/stop"),
        ("GET", f"/api/chat/{hidden}/history?session_id=sess-12345678"),
        ("GET", f"/api/chat/{hidden}/memory?session_id=sess-12345678"),
    )
    _refused(
        world.bob, "agent.not_active",
        ("POST", "/api/eval/runs", {"agent_id": hidden}),
        ("POST", "/api/eval/online",
         {"agent_id": hidden, "evaluators": ["Builtin.Helpfulness"]}),
        ("GET", f"/api/eval/agents/{hidden}/log-streams"),
        ("GET", f"/api/experiments/readiness?agent_id={hidden}"),
        ("POST", "/api/experiments", {"agent_id": hidden}),
        status=400,
    )
    _refused(world.bob, "canary.agent_not_active",
             ("POST", "/api/runtime-canaries", {"agent_id": hidden}), status=400)
    _refused(world.bob, "registry.a2a_demo_agent",
             ("POST", "/api/registry/a2a-demo", {"agent_id": hidden, "question": "q"}))
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
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        res = client.get("/v1/agents", headers={"X-Api-Key": raw})
        assert res.status_code == 200, res.text
        return {a["id"] for a in res.json()["agents"]}


def test_v1_key_follows_its_creators_current_visibility(app, world):
    from fastapi.testclient import TestClient

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


def test_isolated_member_manages_only_own_keys(world):
    def ids(client) -> set[str]:
        return {k["id"] for k in client.get("/api/apikeys").json()["keys"]}

    def mint(client) -> str:
        return client.post("/api/apikeys", json={"name": "k"}).json()["id"]

    ada_key, bob_key, admin_key = mint(world.ada), mint(world.bob), mint(world.admin)
    legacy_id = _add(ApiKey(name="legacy", prefix="p", key_hash=hash_key("legacy-raw-x")))
    every = {ada_key, bob_key, admin_key, legacy_id}
    assert ids(world.bob) == every  # shared: workspace-wide, unchanged
    _isolate()
    assert ids(world.bob) == {bob_key}  # a key is a credential: no NULL / admin keys
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
    """ada's KB + managed memory, and bob's own KB."""
    db = SessionLocal()
    try:
        creators.record(db, DEFAULT_WORKSPACE_ID, creators.KB, "KBADA0001",
                        Creator(world.ada_id, "ada"))
        creators.record(db, DEFAULT_WORKSPACE_ID, creators.KB, "KBBOB0001",
                        Creator(world.bob_id, "bob"))
        db.add(ManagedMemory(workspace_id=DEFAULT_WORKSPACE_ID, memory_id="adamem-abc1234567",
                             origin="created", created_by=world.ada_id, created_by_name="ada"))
        db.commit()
    finally:
        db.close()
    return world


def _harness_spec(name: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, "method": "harness", "system_prompt": "x", **kw}


def test_create_refuses_a_hidden_pin(hidden_pins):
    w = hidden_pins
    _isolate()
    res = w.bob.post("/api/agents", json=_harness_spec(
        "bob-kb", knowledge_bases=[{"kb_id": "KBADA0001"}]))
    assert res.status_code == 404 and res.json()["code"] == "kb.not_found", res.text
    res = w.bob.post("/api/agents", json=_harness_spec(
        "bob-mem", memory={"memory_id": "adamem-abc1234567"}))
    assert res.status_code == 422 and res.json()["code"] == "agent.memory_not_managed"
    res = w.bob.post("/api/agents", json=_harness_spec(
        "bob-kb", knowledge_bases=[{"kb_id": "KBBOB0001"}]))
    assert res.status_code == 202, res.text
    db = SessionLocal()
    try:
        assert {a.name for a in db.query(Agent)}.isdisjoint({"bob-mem"})
    finally:
        db.close()


def test_redeploy_keeps_an_existing_hidden_pin_but_refuses_adding_one(hidden_pins):
    w = hidden_pins
    pinned = {"name": "bob-pinned", "method": "harness", "system_prompt": "x",
              "knowledge_bases": [{"kb_id": "KBADA0001"}]}
    agent_id = _agent("bob-pinned", w.bob_id, method="harness", spec=pinned)
    plain_id = _agent("bob-plain", w.bob_id, method="harness",
                      spec={"name": "bob-plain", "method": "harness", "system_prompt": "x"})
    _isolate()
    # pinned while shared → re-publishing the same pins keeps working
    res = w.bob.post(f"/api/agents/{agent_id}/redeploy", json={**pinned, "system_prompt": "y"})
    assert res.status_code == 202, res.text
    res = w.bob.post(f"/api/agents/{plain_id}/redeploy", json=_harness_spec(
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


def test_visible_run_experiment_canary_cannot_mutate_a_hidden_agent(monkeypatch, world):
    from app.evaluation.models import EvalRun
    from app.optimization import canary_routers
    from app.optimization import routers as exp_routers
    from app.optimization.models import Experiment, RuntimeCanary

    hidden = world.ada_agent
    run = _add(EvalRun(agent_id=hidden, agent_name="ada-agent", status="completed",
                       created_by=world.bob_id))
    exp = _add(Experiment(name="e", agent_id=hidden, agent_name="ada-agent",
                          created_by=world.bob_id))
    can = _add(RuntimeCanary(name="c", champion_agent_id=hidden,
                             champion_agent_name="ada-agent", challenger_agent_id=hidden,
                             challenger_agent_name="ada-agent", created_by=world.bob_id))
    cleaned: list[str] = []
    monkeypatch.setattr(exp_routers.service, "stage_not_ready_reason", lambda *a: "not now")
    monkeypatch.setattr(canary_routers.canary_service, "stage_not_ready_reason",
                        lambda *a: "not now")
    _isolate()
    _refused(world.bob, "agent.not_found",
             ("POST", f"/api/eval/runs/{run}/recommendations/r1/accept"),
             ("POST", f"/api/experiments/{exp}/action", {"action": "accept"}),
             ("POST", f"/api/experiments/{exp}/action", {"action": "promote"}),
             ("POST", f"/api/experiments/{exp}/action", {"action": "traffic"}),
             ("POST", f"/api/runtime-canaries/{can}/action", {"action": "setup"}),
             ("POST", f"/api/runtime-canaries/{can}/action", {"action": "complete"}),
             ("POST", f"/api/runtime-canaries/{can}/action", {"action": "rollback"}))
    # the owner's own cleanup is not an agent mutation: it passes the agent gate
    # (and stops at the stubbed stage check)
    for url in (f"/api/experiments/{exp}/action", f"/api/runtime-canaries/{can}/action"):
        res = world.bob.post(url, json={"action": "cleanup"})
        assert res.status_code == 409, res.text
        cleaned.append(res.json()["code"])
    assert cleaned == ["experiment.stage_not_ready", "canary.stage_not_ready"]
    # an admin passes the gate
    res = world.admin.post(f"/api/experiments/{exp}/action", json={"action": "accept"})
    assert res.json()["code"] == "experiment.stage_not_ready"


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
    assert (inputs["source"], inputs["system_prompt"], inputs["agent_method"]) == (
        "manual", "", None)
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
    monkeypatch.setattr(obs, "get_session_transcript", lambda sid, db, ws, agent_id=None: {
        "session_id": sid, "transcript": {"available": False, "agent_id": agent_id}})
    return SimpleNamespace(world=world, ada_sid=ada_sid, bob_sid=bob_sid, chat_sid=chat_sid)


def test_observability_drops_and_404s_hidden_agents(spans):
    w = spans.world
    assert len(w.bob.get("/api/observability/traces").json()["traces"]) == 3
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
    assert w.bob.get(f"/api/observability/traces/{'b' * 32}").status_code == 200
    assert w.bob.get(f"/api/observability/sessions/{spans.bob_sid}").status_code == 200
    assert w.admin.get(f"/api/observability/sessions/{spans.ada_sid}").status_code == 200


def test_overview_active_sessions_and_a2a_count_are_per_viewer(monkeypatch, world):
    import app.routers.overview as overview_mod

    records = [
        {"recordId": "rec-ada", "descriptorType": "A2A", "status": "APPROVED"},
        {"recordId": "rec-bob", "descriptorType": "A2A", "status": "APPROVED"},
    ]
    monkeypatch.setattr(overview_mod, "console_list", lambda _ws: records)
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    monkeypatch.setattr(overview_mod, "attached_policy_engine_id", lambda _c, _ws: "")
    overview_mod._cache.clear()
    now = datetime.now(UTC)
    _add(ChatSession(agent_id=world.ada_agent, session_id="s-ada-ov", last_at=now))
    _add(ChatSession(agent_id=world.bob_agent, session_id="s-bob-ov", last_at=now))
    shared = world.bob.get("/api/overview").json()
    assert (shared["active_sessions"], shared["registry_assets"]["agents"]) == (2, 2)
    _isolate()
    isolated = world.bob.get("/api/overview").json()
    assert (isolated["active_sessions"], isolated["registry_assets"]["agents"]) == (1, 1)
    as_admin = world.admin.get("/api/overview").json()
    assert (as_admin["active_sessions"], as_admin["registry_assets"]["agents"]) == (2, 2)


# ─── memory console (R5) ─────────────────────────────────────────────────────


@pytest.fixture
def memory_world(monkeypatch, world):
    from app.services import memory_console as mc

    ada_actor, bob_actor = f"{world.ada_agent}__ada", f"{world.bob_agent}__bob"
    _add(ChatSession(agent_id=world.ada_agent, session_id="s-ada-mem", actor_id="default"))
    monkeypatch.setattr(mc, "list_actors", lambda *a, **k: {
        "items": [mc.decode_actor(x) for x in (ada_actor, bob_actor, "default")],
        "next_token": None})
    monkeypatch.setattr(mc, "list_sessions", lambda *a, **k: {
        "items": [{"session_id": "s-ada-mem"}, {"session_id": "s-other"}],
        "next_token": None})
    monkeypatch.setattr(mc, "list_events", lambda *a, **k: {"items": [], "next_token": None})
    monkeypatch.setattr(mc, "resolve_namespaces", lambda _ws, actor, *a: [
        {"namespace": f"/facts/{actor}", "strategy_id": "s1", "resolvable": True}])
    monkeypatch.setattr(mc, "list_records", lambda *a, **k: {"items": [], "next_token": None})
    monkeypatch.setattr(mc, "search_records", lambda *a, **k: {"items": []})
    monkeypatch.setattr(mc, "list_extraction_jobs", lambda *a, **k: {
        "items": [{"actor_id": ada_actor}, {"actor_id": bob_actor}], "next_token": None})
    return SimpleNamespace(world=world, ada_actor=ada_actor, bob_actor=bob_actor)


def test_memory_console_hides_hidden_agents_actors(memory_world):
    m = memory_world
    w = m.world
    assert len(w.bob.get("/api/memory/actors").json()["items"]) == 3
    _isolate()
    actors = [a["actor_id"] for a in w.bob.get("/api/memory/actors").json()["items"]]
    assert actors == [m.bob_actor, "default"]
    assert len(w.admin.get("/api/memory/actors").json()["items"]) == 3
    a = m.ada_actor
    _refused(w.bob, "memory.actor_not_found",
             ("GET", f"/api/memory/sessions?actor_id={a}"),
             ("GET", f"/api/memory/events?actor_id={a}&session_id=s-1"),
             ("GET", "/api/memory/events?actor_id=default&session_id=s-ada-mem"),
             ("GET", f"/api/memory/namespaces?actor_id={a}"),
             ("GET", f"/api/memory/records?actor_id={a}"),
             ("GET", f"/api/memory/records?namespace=/facts/{a}"),
             ("POST", "/api/memory/records/search", {"query": "q", "actor_id": a}),
             ("POST", "/api/memory/records/search", {"query": "q", "namespace": f"/facts/{a}"}),
             ("GET", f"/api/memory/extraction-jobs?actor_id={a}"))
    # the shared "default" actor stays, minus the sessions the ledger gives ada's agent
    sessions = w.bob.get("/api/memory/sessions?actor_id=default").json()["items"]
    assert [s["session_id"] for s in sessions] == ["s-other"]
    jobs = w.bob.get("/api/memory/extraction-jobs").json()["items"]
    assert [j["actor_id"] for j in jobs] == [m.bob_actor]
    assert w.bob.get(f"/api/memory/records?actor_id={m.bob_actor}").status_code == 200


# ─── leaks of hidden agents (R8) ─────────────────────────────────────────────


def test_name_exists_names_the_holder_only_when_visible(world):
    _isolate()
    hidden = world.bob.post("/api/agents", json=_harness_spec("ada-agent"))
    assert hidden.status_code == 409 and hidden.json()["code"] == "agent.name_exists"
    assert hidden.json()["detail"] == {"agent_id": None}  # uniqueness stays workspace-wide
    visible = world.bob.post("/api/agents", json=_harness_spec("legacy-agent"))
    assert visible.json()["detail"] == {"agent_id": world.legacy_agent}
    assert world.admin.post("/api/agents", json=_harness_spec("ada-agent")).json()[
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


def test_kb_used_by_omits_hidden_agent_names(monkeypatch, world):
    from app.services import knowledge

    monkeypatch.setattr(knowledge, "list_kbs", lambda _ws: [
        {"kb_id": "KB1", "attached_agents": ["ada-agent", "bob-agent"]}])
    monkeypatch.setattr(knowledge, "get_kb_detail", lambda _ws, kb_id: {
        "kb_id": kb_id, "attached_agents": ["ada-agent", "bob-agent"]})

    def refuse(_ws, kb_id, force=False):
        raise knowledge.KBAttachedError(kb_id, ["ada-agent", "bob-agent"])

    monkeypatch.setattr(knowledge, "delete_kb", refuse)
    _isolate()
    item = world.bob.get("/api/knowledge-bases").json()["items"][0]
    assert (item["attached_agents"], item["attached_agent_count"]) == (["bob-agent"], 2)
    detail = world.bob.get("/api/knowledge-bases/KB1").json()
    assert (detail["attached_agents"], detail["attached_agent_count"]) == (["bob-agent"], 2)
    refused = world.bob.delete("/api/knowledge-bases/KB1")
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"agents": ["bob-agent"]}
    assert "2 agent(s)" in refused.json()["message"]  # the count stays truthful
    assert world.admin.get("/api/knowledge-bases").json()["items"][0]["attached_agents"] == [
        "ada-agent", "bob-agent"]


def test_memory_used_by_omits_hidden_agents(monkeypatch, world):
    from app.services import memory_admin

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
    monkeypatch.setattr(memory_admin, "list_memory_resources", lambda _ws: {
        "items": [{"id": "sharedmem-abc1234567"}]})
    _isolate()
    item = world.bob.get("/api/memory/resources").json()["items"][0]
    assert [a["id"] for a in item["agents"]] == [world.bob_agent]
    refused = world.bob.delete("/api/memory/resources/sharedmem-abc1234567")
    assert refused.status_code == 409 and refused.json()["code"] == "memory.in_use"
    assert [a["id"] for a in refused.json()["detail"]["agents"]] == [world.bob_agent]
    assert "2 agent(s)" in refused.json()["message"]


def test_connection_refs_omit_hidden_agents(monkeypatch, world):
    import app.routers.identity as identity_router
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
        raise identity_providers.AppError(
            "identity.connection_referenced", "referenced",
            detail={"referenced_by": list(refs)}, status_code=409)

    monkeypatch.setattr(identity_providers, "delete_connection", refuse)
    _isolate()
    visible = [{"type": "agent", "id": world.bob_agent, "name": "bob-agent"}, refs[2]]
    listed = world.bob.get("/api/identity/connections").json()["connections"][0]
    assert listed["referenced_by"] == visible
    detail = world.bob.get("/api/identity/connections/oauth2/c").json()
    assert detail["referenced_by"] == visible
    refused = world.bob.delete("/api/identity/connections/oauth2/c")
    assert refused.status_code == 409
    assert refused.json()["detail"]["referenced_by"] == visible


def test_online_matched_agent_skips_hidden_agents(world):
    from app.evaluation import online

    ws = SimpleNamespace(id=DEFAULT_WORKSPACE_ID)
    db = SessionLocal()
    try:
        _isolate()
        log_group = f"{online.RUNTIME_LOG_GROUP_PREFIX}ada_agent-abcdefghij-DEFAULT"
        bob_view = online._agent_log_group_matchers(db, ws, Viewer(world.bob_id, True))
        assert online._match_agent([log_group], bob_view) is None
        everyone = online._agent_log_group_matchers(db, ws)
        assert online._match_agent([log_group], everyone) == {
            "id": world.ada_agent, "name": "ada-agent"}
    finally:
        db.close()


def test_registry_a2a_records_of_hidden_agents(monkeypatch, world):
    from app.services import registry_console

    records = [{"recordId": "rec-ada", "name": "ada-agent", "descriptorType": "A2A"},
               {"recordId": "rec-bob", "name": "bob-agent", "descriptorType": "A2A"},
               {"recordId": "rec-mcp", "name": "tool", "descriptorType": "MCP"}]
    monkeypatch.setattr(registry_console, "console_list", lambda *a: list(records))
    monkeypatch.setattr(registry_console, "console_search", lambda *a: list(records))
    monkeypatch.setattr(registry_console, "console_list_discoverable",
                        lambda *a: list(records))
    monkeypatch.setattr(registry_console, "console_get",
                        lambda _ws, rid: next(r for r in records if r["recordId"] == rid))
    deleted: list[str] = []
    monkeypatch.setattr(registry_console, "console_delete",
                        lambda _ws, rid: deleted.append(rid))
    _isolate()
    for url in ("/api/registry/records", "/api/registry/records/search?q=a",
                "/api/registry/records/discoverable"):
        ids = {r["record_id"] for r in world.bob.get(url).json()["records"]}
        assert ids == {"rec-bob", "rec-mcp"}, url
    _refused(world.bob, "aws.not_found",
             ("GET", "/api/registry/records/rec-ada"),
             ("POST", "/api/registry/records/rec-ada/action", {"action": "approve"}),
             ("PUT", "/api/registry/records/rec-ada", {"description": "x"}),
             ("POST", "/api/registry/records/rec-ada/reimport"),
             ("DELETE", "/api/registry/records/rec-ada"))
    assert deleted == []
    assert world.bob.get("/api/registry/records/rec-bob").status_code == 200
    ids = {r["record_id"] for r in world.admin.get("/api/registry/records").json()["records"]}
    assert ids == {"rec-ada", "rec-bob", "rec-mcp"}


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
    finally:
        db.close()
