# ruff: noqa: F811 — the make_world fixture is imported from test_engine_parity
"""A6 routing: ``policy_engine_decide.engine_decides`` is the one switch between cedarpy and
the engine for the hub's own engine consumer — the simulator's ``call_tool`` decision
(``policy_bundle.evaluate``; Global parity: no invoke gate, no deploy-time narrowing exist).
Routed, the decision is the engine's (and fails closed when it is down) and the simulator's
``engine`` block answers both stages; unrouted, cedarpy decides exactly as before. Skipped
unless the engine binary is built."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.models.policy import Policy, PolicyVersion
from app.services import policy_backend, policy_bundle, policy_store
from app.services import policy_engine_control as control
from app.services import policy_engine_decide as decide
from app.services.policy_entities import Principal
from tests.policy_principal.engine_harness import ENGINE
from tests.policy_principal.test_engine_parity import (  # noqa: F401 — fixture
    AGENTS_CALL_TOOLS,
    PERMIT_HR,
    USERS_CALL_TOOLS,
    WS,
    _user,
    make_world,
)

pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="engine binary not built")


@pytest.fixture
def routed(make_world, monkeypatch):
    db, build = make_world
    agents = build([{"id": "pol_agents", "statement": AGENTS_CALL_TOOLS},
                    {"id": "pol_users", "statement": USERS_CALL_TOOLS},
                    {"id": "pol_hr", "statement": PERMIT_HR}], [{"id": "ag_a"}])
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    db.commit()
    return db, agents["ag_a"]


def _simulate_url(db):
    return f"/api/policy-engines/{policy_store.settings_row(db, WS).id}/simulate"


def _counting_backend(monkeypatch):
    real = policy_backend.get_backend()
    calls = {"n": 0}

    class Counting:
        def __getattr__(self, name):
            attr = getattr(real, name)
            if name in ("authorize", "authorize_batch"):
                def wrapped(*a, **k):
                    calls["n"] += 1
                    return attr(*a, **k)
                return wrapped
            return attr

    policy_backend.set_backend(Counting())
    return calls


def test_routed_decisions_never_ask_cedarpy_and_unrouted_ones_do(routed, monkeypatch):
    db, agent = routed
    calls = _counting_backend(monkeypatch)
    ev = _member_tool(db, agent)
    assert ev.outcome == "ALLOW" and calls["n"] == 0
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", False)
    db.commit()
    ev = _member_tool(db, agent)
    assert ev.outcome == "ALLOW" and calls["n"] == 1


def test_routed_decisions_fail_closed_when_no_shard_answers(routed, monkeypatch):
    db, agent = routed
    db.commit()

    def down(shard, message, **_kw):
        raise control.EngineControlError("unavailable", f"shard {shard} down")

    monkeypatch.setattr(control, "call", down)
    # the routed decision fails CLOSED: EngineControlError, never a cedarpy fallback / permit
    with pytest.raises(control.EngineControlError) as err:
        _member_tool(db, agent)
    assert err.value.code == "unavailable"


def test_simulate_engine_block_answers_both_stages_only_when_routed(routed, client, monkeypatch):
    db, agent = routed
    body = {"principal": {"kind": "user", "id": "hr-intern", "role": "member"},
            "action": "call_tool", "resource": {"kind": "tool", "id": "hr-database___get_employee"},
            "engine": {"agent_id": agent.id, "input": {"employee_id": "EMP-1024"},
                       "at": "2026-10-27T10:00:00+08:00", "history": []}}
    res = client.post(_simulate_url(db), json=body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert (out["engine"], out["stage_a"], out["stage_bt"], out["final"]) == (
        True, "allow", "allow", "allow")
    assert out["source_set"] == "active" and out["trace"]
    # an API key has no user permit: outside PERMIT_HR's any-principal rule it is denied
    key = client.post(_simulate_url(db), json={
        "principal": {"kind": "api_key", "id": "key_x"}, "action": "call_tool",
        "resource": {"kind": "tool", "id": "hr-approvals___approve_payout"},
        "engine": {"agent_id": agent.id}})
    assert (key.status_code, key.json()["final"]) == (200, "deny")
    # the retired vocabulary is a schema error: no agent resource, no invoke action
    assert client.post(_simulate_url(db), json={
        "principal": {"kind": "api_key", "id": "key_x"}, "action": "invoke",
        "resource": {"kind": "agent", "id": agent.id}, "engine": {}}).status_code == 422
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", False)
    db.commit()
    res = client.post(_simulate_url(db), json=body)
    assert (res.status_code, res.json()["code"]) == (409, "policy.engine_not_routed")
    plain = dict(body)
    plain.pop("engine")
    assert client.post(_simulate_url(db), json=plain).status_code == 200


def test_flag_off_the_decision_is_cedarpy_exactly(routed, monkeypatch):
    db, agent = routed
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", False)
    calls = _counting_backend(monkeypatch)
    who = Principal.system()
    ev = policy_bundle.evaluate(
        db, WS, policy_bundle.call_tool_request(who, TOOL, agent_id=agent.id, channel="system"),
        principal=who, agent=agent, tool_ids=(TOOL,))
    assert ev.outcome in ("ALLOW", "DENY") and calls["n"] == 1


def test_console_flat_simulation_replays_history_on_the_selected_gateway(
        routed, client, monkeypatch):
    from app.services import policy_engines

    db, agent = routed
    gateway = "launchpad-gw-chosen"
    seen = []

    def facts(db, gateway_id, *, workspace_id):
        seen.append((gateway_id, workspace_id))
        return {"gateway_arn":
                f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:gateway/{gateway_id}"}

    monkeypatch.setattr(policy_engines, "gateway_facts", facts)
    # Exact body emitted by PolicySimulator, without legacy action/resource/engine wrappers.
    body = {
        "gateway_id": gateway, "principal": {"kind": "user", "id": "hr-intern"},
        "agent_id": agent.id, "tool": "hr-database___get_employee",
        "input": {"employee_id": "EMP-1024"}, "at": "2026-10-27T10:00:00Z",
        "history": [{"tool": "hr-database___get_employee", "kind": "request",
                     "input": {"employee_id": "EMP-1024"}, "output": {},
                     "at": "2026-10-27T09:55:00Z"}],
    }
    response = client.post(_simulate_url(db), json=body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert (out["stage_a"], out["stage_bt"], out["final"]) == ("allow", "allow", "allow")
    assert out["engine"] and out["gateway_id"] == gateway
    assert len(out["trace"]) == 2
    assert seen and all(pair == (gateway, WS) for pair in seen)
    assert client.post(_simulate_url(db), json={
        **body, "input": {"text": "x" * (17 * 1024)},
    }).status_code == 422
    assert client.post(_simulate_url(db), json={
        **body, "history": body["history"] * 51,
    }).status_code == 422
    # A legacy wrapper must never discard flat input/history and select Cedar-only.
    for patch in [{}, {"input": {"text": "x" * (17 * 1024)}},
                  {"history": body["history"] * 51}]:
        mixed = client.post(_simulate_url(db), json={
            **body, **patch, "action": "call_tool",
            "resource": {"kind": "tool", "id": "hr-database___other"},
        })
        assert mixed.status_code == 422, mixed.text
        assert "cannot be mixed" in mixed.text
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", False)
    stopped = client.post(_simulate_url(db), json=body)
    assert (stopped.status_code, stopped.json()["code"]) == (409, "policy.engine_not_routed")


FORBID_MEMBER_TOOLS = ('forbid(principal in Launchpad::Role::"member", '
                       'action == Launchpad::Action::"call_tool", resource);')
# outside PERMIT_HR's any-principal rule: only the user permits (pol_users) allow it
TOOL = "hr-approvals___approve_payout"


def _add_active(db, pid, statement):
    db.add(Policy(id=pid, workspace_id=WS, engine_id=policy_store.settings_row(db, WS).id,
                  name=pid, status="active",
                  enforcement_mode="active", active_version=1))
    db.add(PolicyVersion(workspace_id=WS, policy_id=pid, version=1, statement=statement,
                         dialect="cedar", schema_digest="x"))
    db.commit()


def _member_tool(db, agent, tool=TOOL):
    member = _user("hr-intern", "member")
    return policy_bundle.evaluate(
        db, WS, policy_bundle.call_tool_request(member, tool, agent_id=agent.id, channel="chat"),
        principal=member, agent=agent, tool_ids=(tool,))


def test_a_retire_while_the_global_switch_was_off_keeps_cedarpy_until_reconciled(
        routed, monkeypatch):
    """Review A6 D1, order 2: the global switch off → retire both baseline permits (the
    workspace permit grants every workspace principal on both paths) → switch on."""
    from app.services import policy_cedar, policy_engine_sync

    db, agent = routed
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", False)
    db.get(Policy, "pol_users").status = "retired"
    db.get(Policy, "pol_agents").status = "retired"
    db.commit()
    policy_cedar.invalidate(WS)
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    assert not decide.engine_decides(db, WS)
    assert _member_tool(db, agent).outcome == "DENY"  # cedarpy: no permit left
    policy_engine_sync.reconcile_workspace(db, WS)
    db.commit()
    assert decide.engine_decides(db, WS) and _member_tool(db, agent).outcome == "DENY"


def test_reconcile_reinstalls_when_a_shard_holds_another_set(routed, monkeypatch):
    """Review nit: an install whose ledger commit failed leaves the engine on ps-(n+1) while
    the hub says ps-n — the digests agree, the shards do not; reconcile re-installs."""
    from app.services import policy_engine_sync

    db, _agent = routed
    owner = policy_store.settings_row(db, WS)
    seq = owner.policy_set_seq
    monkeypatch.setattr(control, "status", lambda: [
        {"shard": 0, "ready": True, "sets": {owner.id: f"ps-{seq + 1}"}}])
    assert policy_engine_sync.reconcile_workspace(db, WS) is not None
    assert policy_store.settings_row(db, WS).policy_set_seq == seq + 1


def test_a_set_mismatch_fails_closed_and_schedules_one_reconcile(routed, monkeypatch):
    db, agent = routed
    scheduled: list[str] = []
    monkeypatch.setattr(decide, "schedule_reconcile", lambda ws: scheduled.append(ws) or True)
    policy_store.settings_row(db, WS).policy_set_seq = 99
    db.commit()
    with pytest.raises(control.EngineControlError) as err:
        decide.evaluate_tool(db, WS, _user("hr-intern", "member"), agent, TOOL, channel="chat")
    assert err.value.code == "set_mismatch" and scheduled == [policy_store.settings_row(db, WS).id]


def test_engine_simulation_inputs_are_bounded_and_rate_limited(routed, client, monkeypatch):
    from app.routers import policy_engines as policies_router

    db, agent = routed
    base = {"principal": {"kind": "user", "id": "hr-intern", "role": "member"},
            "action": "call_tool", "resource": {"kind": "tool", "id": "hr-database___get_employee"}}
    big = {"text": "x" * (17 * 1024)}
    for engine in ({"agent_id": agent.id, "input": big},
                   {"agent_id": agent.id, "history": [
                       {"tool": "t", "at": "2026-10-27T10:00:00Z", "output": big}]},
                   {"agent_id": agent.id, "history": [
                       {"tool": "t", "at": "2026-10-27T10:00:00Z"}] * 51}):
        res = client.post(_simulate_url(db), json={**base, "engine": engine})
        assert res.status_code == 422, res.text
    monkeypatch.setattr(policies_router, "ENGINE_SIMULATIONS_PER_MINUTE", 2)
    ok = {**base, "engine": {"agent_id": agent.id}}
    assert [client.post(_simulate_url(db), json=ok).status_code for _ in range(3)] == [
        200, 200, 429]
    assert client.post(_simulate_url(db), json=ok).json()["code"] == (
        "policy.simulate_rate_limited")


def test_list_tool_simulation_answers_visibility_from_stage_a(routed, client):
    from app.services import policy_cedar, policy_engine_sync
    from tests.policy_principal.test_engine_parity import PAYOUT_ADMIN_ONLY

    db, agent = routed
    db.add(Policy(id="pol_payout", workspace_id=WS,
                  engine_id=policy_store.settings_row(db, WS).id, name="p", status="active",
                  enforcement_mode="active", active_version=1))
    db.add(PolicyVersion(workspace_id=WS, policy_id="pol_payout", version=1,
                         statement=PAYOUT_ADMIN_ONLY, dialect="cedar", schema_digest="x"))
    db.flush()
    policy_engine_sync.install_workspace(db, WS)
    db.commit()
    policy_cedar.invalidate(WS)
    body = {"principal": {"kind": "user", "id": "hr-intern", "role": "member"},
            "resource": {"kind": "tool", "id": "hr-database___create_payout"},
            "engine": {"agent_id": agent.id, "input": {"employee_id": "E1", "amount": 1}}}
    call = client.post(_simulate_url(db), json={**body, "action": "call_tool"}).json()
    listed = client.post(_simulate_url(db), json={**body, "action": "list_tool"}).json()
    assert (call["final"], listed["final"], listed["list_filter"]) == ("deny", "allow", True)
