"""Differential (lead request 2026-10-26): stored D20 policies, decided by today's cedarpy
engine in the Launchpad namespace, must decide IDENTICALLY after ``rewrite_d20`` on the
Dogwood engine — for the decision D20 actually enforced: agent-principal tool decisions
(deploy-time narrowing / the runtime guard: ``Agent`` calling a Gateway tool).

Human-principal tool rules were never enforced by D20 (``principal_rules`` was only a flag)
and are the new behaviour of this work, so they are not part of the differential; the
agent-invocation gate is gone (contract amendment 9), so neither is it.
Skipped unless the engine binary is built."""

from __future__ import annotations

import itertools
import json

import pytest

from app.policy import dogwood_compiler as dc
from app.services.policy_backend import AuthzRequest
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import Principal, build_entities, request_context
from tests.policy_principal.engine_harness import ASSETS, GW, ROLE
from tests.policy_principal.test_dogwood_compiler import needs_engine

pytestmark = needs_engine

TEMPLATES = ASSETS.parents[3] / "backend" / "app" / "policy" / "templates"
TOOLS = json.loads((ASSETS / "demo-tools.json").read_text())
TOOL_IDS = [t["name"] for t in TOOLS["result"]["tools"]]
WS = "default"
AGENTS = ["ag_hrhelp01", "ag_other001"]


class _Agent:
    def __init__(self, agent_id: str):
        self.id = agent_id
        self.name = agent_id
        self.method = "zip_runtime"
        self.system_key = None
        self.created_by_name = None
        self.workspace_id = WS
        self.spec = {}


def _template(name: str, **subs: str) -> str:
    text = (TEMPLATES / f"{name}.cedar").read_text()
    for key, value in subs.items():
        text = text.replace(key, value)
    return text


POLICY_SETS = {
    # amendment 9: the two baselines (Stage BT users; Stage A every agent of the workspace)
    "users_call_tools": [_template("permit_users_call_tools")],
    "workspace_agents_call_tools": [_template("permit_workspace_agents_call_tools",
                                              __WORKSPACE_ID__=WS)],
    "agent_tools_target": [_template("permit_agent_declared_tools", __AGENT_ID__="ag_hrhelp01",
                                     __TOOL_PATTERN__="hr-database___*")],
    "agent_tools_literal": [_template("permit_agent_declared_tools", __AGENT_ID__="ag_hrhelp01",
                                      __TOOL_PATTERN__="hr-approvals___verify_identity")],
    "agent_tools_plus_forbid": [
        _template("permit_agent_declared_tools", __AGENT_ID__="ag_hrhelp01",
                  __TOOL_PATTERN__="hr-database___*"),
        'forbid (principal == Launchpad::Agent::"ag_hrhelp01", '
        'action == Launchpad::Action::"call_tool", '
        'resource == Launchpad::Tool::"hr-database___create_payout");',
    ],
    # N17-obs495: the D20 "any agent of the workspace" shapes — `is Launchpad::Agent` (the
    # engine's Stage-A principal is the IamEntity, so the rewrite says `is IamEntity`) and
    # `in Launchpad::Workspace::"default"` (the agent chain IamEntity -> Agent -> Workspace)
    "any_agent_is": [
        'permit (principal is Launchpad::Agent, action == Launchpad::Action::"call_tool", '
        'resource);',
        'forbid (principal is Launchpad::Agent, action == Launchpad::Action::"call_tool", '
        'resource == Launchpad::Tool::"hr-database___create_payout");',
    ],
    "any_agent_in_workspace": [
        'permit (principal in Launchpad::Workspace::"default", '
        'action == Launchpad::Action::"call_tool", resource);',
    ],
}


def _old(statements: list[str], request: AuthzRequest, principal: Principal, agent: _Agent,
         tools: tuple[str, ...]) -> str:
    engine = CedarEngine()
    text = "\n".join(f'@id("p{i}:v1")\n{s}' for i, s in enumerate(statements))
    entities = build_entities(principal, workspace_id=WS, agent=agent, tool_ids=tools)
    return engine.authorize(request, engine.compile(text), entities).decision


def _new(statements: list[str], *, tool: str, principal: dict, agent_id: str,
         resource: str | None = None) -> str:
    policies = [{"id": f"p{i}", "version": 1, "mode": "active",
                 "statement": dc.rewrite_d20(s).statement} for i, s in enumerate(statements)]
    agent = {"entity": ROLE.replace("ab12cd34", agent_id), "agent_id": agent_id,
             "method": "zip_runtime", "system": False}
    out = dc.replay(TOOLS, policies, agent=agent, request={
        "tool": tool, "principal": principal, "input": _input(tool), "at_s": 1_793_022_000,
        "resource": resource or GW}, ws=WS)
    assert out["ok"], out
    # D20's agent-only oracle is Stage A. The final decision also requires an explicit
    # user permit; Global intentionally no longer synthesizes that Stage-BT closure.
    return out["decision"]["stage_a"]


def _input(tool: str) -> dict:
    match = [t for t in TOOLS["result"]["tools"] if t["name"] == tool]
    if not match:
        return {}
    schema = match[0]["inputSchema"]
    sample = {"string": "EMP-1024", "number": 1, "integer": 1, "boolean": True}
    return {k: sample[v.get("type", "string")] for k, v in schema.get("properties", {}).items()}


@pytest.mark.parametrize("set_name", sorted(POLICY_SETS))
def test_agent_tool_decisions_decide_identically(set_name):
    statements = POLICY_SETS[set_name]
    for agent_id, tool in itertools.product(AGENTS, TOOL_IDS):
        agent = _Agent(agent_id)
        principal = Principal.from_agent(agent)
        request = AuthzRequest(
            principal=principal.ref(), action="call_tool", resource=_tool_ref(tool),
            context=request_context("call_tool", channel="runtime", agent_id=agent_id),
        )
        old = _old(statements, request, principal, agent, (tool,))
        # no human token: the engine's Stage BT principal is the agent's IamEntity
        role = ROLE.replace("ab12cd34", agent_id)
        new_principal = {"entity": role, "parents": [f'AgentCore::Agent::"{agent_id}"'],
                         "attrs": {"agent_id": agent_id}}
        new = _new(statements, tool=tool, principal=new_principal, agent_id=agent_id)
        assert old == new, f"{set_name}: {agent_id} -> {tool}: cedarpy {old}, engine {new}"



def test_the_matrix_is_not_vacuous():
    """Both outcomes are reached, so the equalities above mean something."""

    def as_agent(agent_id: str) -> dict:
        return {"entity": ROLE.replace("ab12cd34", agent_id),
                "parents": [f'AgentCore::Agent::"{agent_id}"'], "attrs": {"agent_id": agent_id}}

    target = POLICY_SETS["agent_tools_target"]
    assert _new(target, tool="hr-database___list_departments", principal=as_agent("ag_hrhelp01"),
                agent_id="ag_hrhelp01") == "allow"
    assert _new(target, tool="hr-database___list_departments", principal=as_agent("ag_other001"),
                agent_id="ag_other001") == "deny"
    # the workspace baseline reaches every agent of the workspace, the users baseline none
    assert _new(POLICY_SETS["workspace_agents_call_tools"], tool="hr-database___list_departments",
                principal=as_agent("ag_other001"), agent_id="ag_other001") == "allow"
    assert _new(POLICY_SETS["users_call_tools"], tool="hr-database___list_departments",
                principal=as_agent("ag_other001"), agent_id="ag_other001") == "deny"


def _agent_ref(agent_id: str):
    from app.services.policy_entities import agent_ref

    return agent_ref(agent_id)


def _tool_ref(tool: str):
    from app.services.policy_entities import tool_ref

    return tool_ref(tool)
