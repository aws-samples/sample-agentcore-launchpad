"""``app/policy/dogwood_compiler.py``: the D20 → Global rewrite (always) and the engine's
offline ``compile`` / ``replay`` commands (when the binary is built)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.policy import dogwood_compiler as dc

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "backend" / "app" / "policy" / "templates"
TOOLS = json.loads(
    (REPO / "policy-engine" / "crates" / "lp-dogwood" / "assets" / "demo-tools.json").read_text()
)


def _binary() -> bool:
    try:
        dc.engine_bin()
        return True
    except dc.EngineUnavailable:
        return False


needs_engine = pytest.mark.skipif(not _binary(), reason="engine binary not built")


@needs_engine
def test_engine_set_key_is_independent_of_the_calling_agents_workspace():
    from tests.policy_principal.engine_harness import user

    policies = [
        {"id": "agents", "version": 1, "mode": "active",
         "statement": 'permit(principal in AgentCore::Workspace::"default", action, resource);'},
        {"id": "users", "version": 1, "mode": "active",
         "statement": 'permit(principal is AgentCore::User, action, resource);'},
    ]
    for workspace, expected in (("default", "allow"), ("other", "deny")):
        out = dc.replay(TOOLS, policies, ws="pe_independent",
                        agent={**AGENT, "workspace_id": workspace}, request={
                            "tool": "hr-database___get_employee",
                            "principal": user("demo-admin", "admin"),
                            "input": {"employee_id": "EMP-1024"}, "at_s": 1000})
        assert out["ok"], out
        assert out["decision"]["stage_a"] == expected
        assert out["decision"]["decision"] == expected


def test_rewrite_forbid_tool_for_members():
    text = (TEMPLATES / "forbid_tool_for_members.cedar").read_text().replace(
        "__TOOL_ID__", "hr-database___create_payout")
    out = dc.rewrite_d20(text)
    assert 'action == AgentCore::Action::"hr-database___create_payout"' in out.statement
    assert 'principal in AgentCore::Role::"admin"' in out.statement
    assert "Launchpad::" not in out.statement and not out.warnings


def test_rewrite_agent_declared_tools_like_target():
    text = (TEMPLATES / "permit_agent_declared_tools.cedar").read_text().replace(
        "__AGENT_ID__", "ag_hrhelp01").replace("__TOOL_PATTERN__", "hr-database___*")
    out = dc.rewrite_d20(text)
    assert 'principal in AgentCore::Agent::"ag_hrhelp01"' in out.statement
    assert 'action in AgentCore::Action::"target:hr-database"' in out.statement
    assert "resource.name" not in out.statement and not out.warnings


def test_rewrite_call_tool_baselines():
    """Global parity (amendment 9): the two call_tool baselines rewrite to the engine's
    vocabulary — the workspace agent permit (Stage A: `in AgentCore::Workspace`) and the four
    user permits (Stage BT; `is Launchpad::System` → `is AgentCore::System`, `is Launchpad::ApiKey`
    → `is AgentCore::ApiKey` — the /v1 key permit, lead option D)."""
    out = dc.rewrite_d20((TEMPLATES / "permit_workspace_agents_call_tools.cedar").read_text()
                         .replace("__WORKSPACE_ID__", "default"))
    assert out.warnings == []
    assert 'principal in AgentCore::Workspace::"default"' in out.statement
    assert 'action in AgentCore::Action::"CallTool"' in out.statement
    out = dc.rewrite_d20((TEMPLATES / "permit_users_call_tools.cedar").read_text())
    assert out.warnings == []
    assert out.statement.count('AgentCore::Action::"CallTool"') == 4
    assert "is AgentCore::System" in out.statement and "is AgentCore::ApiKey" in out.statement
    assert "Launchpad::" not in out.statement


def test_rewrite_warns_on_untranslatable_patterns():
    out = dc.rewrite_d20(
        'permit (principal, action == Launchpad::Action::"call_tool", resource) '
        'when { resource.name like "hr-*" && context.env == "t1" };'
    )
    assert out.warnings  # the operator must edit it


@needs_engine
def test_rewritten_templates_compile_with_the_demo_tools():
    statements = [
        dc.rewrite_d20((TEMPLATES / "forbid_tool_for_members.cedar").read_text()
                       .replace("__TOOL_ID__", "hr-database___create_payout")).statement,
        dc.rewrite_d20((TEMPLATES / "permit_agent_declared_tools.cedar").read_text()
                       .replace("__AGENT_ID__", "ag_hrhelp01")
                       .replace("__TOOL_PATTERN__", "hr-database___*")).statement,
        dc.rewrite_d20((TEMPLATES / "permit_users_call_tools.cedar").read_text()).statement,
        dc.rewrite_d20((TEMPLATES / "permit_workspace_agents_call_tools.cedar").read_text()
                       .replace("__WORKSPACE_ID__", "default")).statement,
    ]
    out = dc.compile(TOOLS, [
        {"id": f"pol_{i}", "version": 1, "mode": "active", "statement": s}
        for i, s in enumerate(statements)
    ])
    assert out.ok, out.errors
    stages = {(i["id"], i["stage"]) for i in out.items}
    assert ("pol_0", "bt") in stages and ("pol_1", "a") in stages
    assert {i["stage"] for i in out.items if i["id"].startswith("pol_2")} == {"bt"}
    assert ("pol_3", "a") in stages
    assert out.closure_actions == []  # the BT user permits cover every tool: no D7 closure left


@needs_engine
def test_compile_reports_errors_per_policy():
    out = dc.compile(TOOLS, [{"id": "pol_bad", "version": 1, "mode": "active",
                              "statement": "permit (principal, action, resource"}])
    assert not out.ok and out.errors_for("pol_bad")


ADMIN = {"entity": 'AgentCore::User::"demo-admin"', "parents": ['AgentCore::Role::"admin"'],
         "attrs": {"username": "demo-admin", "role": "admin"}}
INTERN = {"entity": 'AgentCore::User::"hr-intern"', "parents": ['AgentCore::Role::"member"'],
          "attrs": {"username": "hr-intern", "role": "member"}}
AGENT = {"entity": 'AgentCore::IamEntity::"arn:aws-cn:iam::123456789012:role/r"',
         "agent_id": "ag_hrhelp01", "method": "zip_runtime", "system": False}
POLICIES = [
    {"id": "pol_agent", "version": 1, "mode": "active",
     "statement": 'permit (principal in AgentCore::Agent::"ag_hrhelp01", '
                  'action in AgentCore::Action::"CallTool", resource);'},
    # no synthetic closure permit since the Global model: the user stage needs its own permit
    {"id": "pol_admins", "version": 1, "mode": "active",
     "statement": 'permit (principal in AgentCore::Role::"admin", '
                  'action in AgentCore::Action::"CallTool", resource);'},
    {"id": "pol_payoutadm", "version": 1, "mode": "active",
     "statement": 'forbid (principal, action == AgentCore::Action::"hr-database___create_payout",'
                  ' resource) unless { principal in AgentCore::Role::"admin" };'},
    {"id": "pol_approval", "version": 1, "mode": "active",
     "statement": 'forbid (principal, action == AgentCore::Action::"hr-database___create_payout",'
                  ' resource) unless temporal { formerly within 1h '
                  'AgentCore::Action::"hr-approvals___approve_payout"::response{ '
                  'input.employee_id: context.input.employee_id, output.approved: true } };'},
]
PAYOUT = {"employee_id": "EMP-1024", "amount": 420}


@needs_engine
def test_simulator_replay_follows_the_demo_script():
    t0 = 1_793_022_000
    member = dc.replay(TOOLS, POLICIES, agent=AGENT, request={
        "tool": "hr-database___create_payout", "principal": INTERN, "input": PAYOUT, "at_s": t0})
    assert member["ok"] and member["decision"]["decision"] == "deny"
    assert "pol_payoutadm" in {d["policy_id"] for d in member["decision"]["determining"]}

    admin_no_approval = dc.replay(TOOLS, POLICIES, agent=AGENT, request={
        "tool": "hr-database___create_payout", "principal": ADMIN, "input": PAYOUT, "at_s": t0})
    assert admin_no_approval["decision"]["decision"] == "deny"

    approved = [
        {"tool": "hr-approvals___approve_payout", "kind": "request", "principal": ADMIN,
         "input": PAYOUT, "at_s": t0},
        {"tool": "hr-approvals___approve_payout", "kind": "response", "principal": ADMIN,
         "input": PAYOUT, "output": {"approved": True, "approval_id": "apr-1"}, "at_s": t0 + 1},
    ]
    allowed = dc.replay(TOOLS, POLICIES, agent=AGENT, history=approved, request={
        "tool": "hr-database___create_payout", "principal": ADMIN, "input": PAYOUT,
        "at_s": t0 + 1200})
    assert allowed["decision"]["decision"] == "allow", allowed
    expired = dc.replay(TOOLS, POLICIES, agent=AGENT, history=approved, request={
        "tool": "hr-database___create_payout", "principal": ADMIN, "input": PAYOUT,
        "at_s": t0 + 2 * 3600})
    assert expired["decision"]["decision"] == "deny"


def test_rewrite_d20_drops_invoke_statements_with_a_warning_and_keeps_the_rest():
    """Global parity compat (amendment 9): a stored D20 baseline naming the retired `invoke`
    action is dropped statement by statement with one warning each; the tool rules stay; a
    policy made only of invoke statements rewrites to an EMPTY statement the installer skips."""
    from app.policy import dogwood_compiler as dc

    mixed = ('permit(principal in Launchpad::Role::"member", '
             'action == Launchpad::Action::"invoke", resource);\n'
             'permit(principal == Launchpad::Agent::"ag_x", '
             'action == Launchpad::Action::"call_tool", resource) '
             'when { resource.name like "hr-database___*" };')
    out = dc.rewrite_d20(mixed)
    assert out.warnings == [dc.INVOKE_DROPPED]
    assert "invoke" not in out.statement
    assert out.statement == ('permit(principal in AgentCore::Agent::"ag_x", '
                             'action in AgentCore::Action::"CallTool", resource) '
                             'when { action in AgentCore::Action::"target:hr-database" };')
    only_invoke = ('permit(principal in Launchpad::Role::"member", '
                   'action == Launchpad::Action::"invoke", resource);\n'
                   'permit(principal in Launchpad::Role::"admin", '
                   'action == Launchpad::Action::"invoke", resource);\n'
                   'permit(principal == Launchpad::System::"hub", '
                   'action == Launchpad::Action::"invoke", resource);')
    out = dc.rewrite_d20(only_invoke)
    assert out.statement == "" and out.warnings == [dc.INVOKE_DROPPED] * 3
    tricky = ('forbid(principal, action == Launchpad::Action::"call_tool", '
              'resource == Launchpad::Tool::"a___b") '
              'when { context.input.note == "x; y" };')
    assert dc.rewrite_d20(tricky).warnings == []
