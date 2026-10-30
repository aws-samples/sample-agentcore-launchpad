"""Stream A6 (engine side): hub decisions on the real engine through I5 authorize_stateless —
tool decisions for users / API keys / the agent itself (Global parity, amendment 9: the invoke
item of amendment 4 is retired, nothing is projected at deploy time), the expected-set check
and shard fail-over (skipped unless the engine binary is built)."""

from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.services import policy_engine_control as control
from app.services import policy_engine_decide as decide
from app.services import policy_store
from app.services.policy_entities import Principal
from tests.policy_principal.engine_harness import ENGINE, Engine, demo_install

pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="engine binary not built")

WS = "default"
POLICIES = [
    {"id": "pol_members_call_tools", "version": 1, "mode": "active",
     "statement": 'permit (principal in AgentCore::Role::"member", action, resource);'},
    {"id": "pol_admins_call_tools", "version": 1, "mode": "active",
     "statement": 'permit (principal in AgentCore::Role::"admin", action, resource);'},
    {"id": "pol_agent_hr", "version": 2, "mode": "active",
     "statement": 'permit (principal in AgentCore::Agent::"ag_hrhelp01", '
                  'action in AgentCore::Action::"target:hr-database", resource);'},
    {"id": "pol_payout_admin", "version": 3, "mode": "active",
     "statement": 'forbid (principal, action == AgentCore::Action::"hr-database___create_payout",'
                  ' resource) unless { principal in AgentCore::Role::"admin" };'},
]


def _user(name: str, role: str) -> Principal:
    return Principal(kind="user", id=name, label=name, role=role,
                     attrs={"username": name, "role": role})


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    engine.start()
    calls: list[int] = []
    down: set[int] = set()

    def call(shard, message, **_kw):
        calls.append(shard)
        if shard in down:
            raise control.EngineControlError("unavailable", f"shard {shard} down")
        return engine.control_call(message)

    monkeypatch.setattr(control, "call", call)
    db = SessionLocal()
    row = policy_store.settings_row(db, WS)
    demo_install(engine, POLICIES, version="ps-1", tsv=0, ws=row.id)
    row.policy_set_seq, row.temporal_set_version = 1, 0
    agent = Agent(id="ag_hrhelp01", workspace_id=WS, name="hr-helpdesk", method="zip_runtime",
                  status="active", spec={})
    db.add(agent)
    db.commit()
    try:
        yield {"db": db, "agent": agent, "engine": engine, "calls": calls, "down": down}
    finally:
        db.close()
        engine.stop()


def test_tool_member_allowed_api_key_denied(world):
    db, agent = world["db"], world["agent"]
    ev = decide.evaluate_tool(db, WS, _user("hr-intern", "member"), agent,
                              "hr-database___get_employee", channel="chat")
    assert ev.outcome == "ALLOW"
    # both stages name their permit: the agent's own (Stage A) and the member's (Stage BT)
    assert set(ev.determining_policy_ids) == {"pol_agent_hr:v2", "pol_members_call_tools:v1"}
    assert ev.policy_versions == {"pol_agent_hr": 2, "pol_members_call_tools": 1}
    assert ev.stage is None  # A6: an allow names no denying stage
    key = Principal(kind="api_key", id="key_9f3a", label="ci", attrs={"name": "ci"})
    ev = decide.evaluate_tool(db, WS, key, agent, "hr-database___get_employee", channel="v1")
    assert ev.outcome == "DENY"
    # no stage-A forbid matched: like the Gateway (`interceptor.core`), a default deny is
    # attributed to the final (BT) stage
    assert ev.stage == "bt"


def test_tool_decisions_for_a_principal_are_final(world):
    db, agent = world["db"], world["agent"]
    member = decide.evaluate_tool(db, WS, _user("hr-intern", "member"), agent,
                                  "hr-database___create_payout", channel="chat")
    assert member.outcome == "DENY"
    assert "pol_payout_admin:v3" in member.determining
    admin = decide.evaluate_tool(db, WS, _user("demo-admin", "admin"), agent,
                                 "hr-database___create_payout", channel="chat",
                                 arguments={"employee_id": "EMP-1024", "amount": 420})
    assert admin.outcome == "ALLOW"
    intern = decide.evaluate_tool(db, WS, _user("hr-intern", "member"), agent,
                                  "hr-database___create_payout", channel="chat")
    assert (intern.outcome, intern.stage) == ("DENY", "bt")  # the per-user rule is BT


def test_a_stale_hub_set_is_an_engine_refusal_not_a_decision(world):
    db, agent = world["db"], world["agent"]
    policy_store.settings_row(db, WS).policy_set_seq = 7
    db.commit()
    with pytest.raises(control.EngineControlError) as err:
        decide.evaluate_tool(db, WS, _user("hr-intern", "member"), agent,
                             "hr-database___get_employee", channel="chat")
    assert err.value.code == "set_mismatch"


def test_any_healthy_shard_answers_and_none_raises(world):
    db, agent = world["db"], world["agent"]
    world["down"].add(0)
    ev = decide.evaluate_tool(db, WS, _user("hr-intern", "member"), agent,
                              "hr-database___get_employee", channel="chat")
    assert ev.outcome == "ALLOW" and world["calls"][-2:] == [0, 1]
    world["down"].add(1)
    with pytest.raises(control.EngineControlError) as err:
        decide.evaluate_tool(db, WS, _user("hr-intern", "member"), agent,
                             "hr-database___get_employee", channel="chat")
    assert err.value.code == "unavailable"


# ── /simulate on the engine (temporal via replay) ──────────────────────────────────


def _active(db, pid, statement, *, scope="workspace", agent_ids=(), mode="active"):
    from app.models.policy import Policy, PolicyVersion

    db.add(Policy(id=pid, workspace_id=WS, engine_id=policy_store.settings_row(db, WS).id,
                  name=pid, scope=scope, agent_ids=list(agent_ids),
                  status="active", enforcement_mode=mode, active_version=1))
    db.add(PolicyVersion(workspace_id=WS, policy_id=pid, version=1, statement=statement,
                         dialect="dogwood", schema_digest="x"))


@pytest.fixture
def sim(world, monkeypatch):
    import json

    from app.services import policy_engine_sync
    from tests.policy_principal.engine_harness import ASSETS

    tools = json.loads((ASSETS / "demo-tools.json").read_text())["result"]
    monkeypatch.setattr(policy_engine_sync, "tools_list", lambda ws: tools)
    db = world["db"]
    for p in POLICIES:
        _active(db, p["id"], p["statement"])
    _active(db, "pol_verify_first",
            'forbid (principal, action == AgentCore::Action::"hr-database___get_employee", '
            'resource) unless temporal { formerly within 8h '
            'AgentCore::Action::"hr-approvals___verify_identity"::response{ '
            'output.employee_id: context.input.employee_id, output.verified: true } };')
    db.commit()
    return world


def test_simulate_decides_stateless_with_both_stages(sim):
    db, agent = sim["db"], sim["agent"]
    out = decide.simulate(db, WS, principal=_user("hr-intern", "member"), agent=agent,
                          tool="hr-database___create_payout",
                          arguments={"employee_id": "EMP-1024", "amount": 420})
    assert (out["stage_a"], out["stage_bt"], out["final"]) == ("allow", "deny", "deny")
    assert "pol_payout_admin:v1" in out["determining"]
    assert out["source_set"] == "active" and out["policy_versions"]["pol_payout_admin"] == 1


def test_simulate_runs_temporal_rules_over_the_history(sim):
    db, agent = sim["db"], sim["agent"]
    member = _user("hr-intern", "member")
    args = {"employee_id": "EMP-1024"}
    before = decide.simulate(db, WS, principal=member, agent=agent,
                             tool="hr-database___get_employee", arguments=args, at_s=1000)
    assert before["final"] == "deny"
    history = [
        {"tool": "hr-approvals___verify_identity", "kind": "request", "input": args, "at_s": 100},
        {"tool": "hr-approvals___verify_identity", "kind": "response", "input": args,
         "output": {"employee_id": "EMP-1024", "verified": True}, "at_s": 101},
    ]
    after = decide.simulate(db, WS, principal=member, agent=agent,
                            tool="hr-database___get_employee", arguments=args, at_s=1000,
                            history=history)
    assert after["final"] == "allow", after
    assert [t["tool"] for t in after["trace"]][-1] == "hr-database___get_employee"


def test_simulator_uses_the_selected_gateway_resource(sim):
    db, agent = sim["db"], sim["agent"]
    selected = "launchpad-gw-selected"
    resource = decide.gateway_resource(db, WS, selected)
    _active(db, "pol_gateway",
            f"forbid(principal, action, resource) unless {{ resource == {resource} }};")
    db.commit()
    kwargs = {"principal": _user("demo-admin", "admin"), "agent": agent,
              "tool": "hr-database___create_payout",
              "arguments": {"employee_id": "EMP-1024", "amount": 420}}
    assert decide.simulate(db, WS, gateway_id=selected, **kwargs)["final"] == "allow"
    assert decide.simulate(db, WS, gateway_id="launchpad-gw-other", **kwargs)["final"] == "deny"


