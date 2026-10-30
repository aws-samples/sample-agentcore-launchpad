"""Stream A4 — principal / agent / tool entities in the single ``AgentCore`` namespace
(contract amendment 1, docs/cn/interfaces/policy-principal-contracts.md "Vocabulary and event
facts"; design §13.3, D1 revised, D6). Shared by the hub (invoke gate, simulator, narrowing)
and stream C's compiler, so it is stdlib-only and pinned to the I4 fixtures."""

from __future__ import annotations

import copy

import jsonschema
import pytest

from app.policy import contracts
from app.policy import principal_entities as pe
from app.services import principal_token as pt
from app.services.policy_entities import Principal
from tests.policy_principal.conftest import load_fixture

ROLE = "arn:aws-cn:iam::123456789012:role/launchpad-agent-hr-helpdesk-ab12cd34"
GW = ("arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:gateway/"
      "launchpad-gw-exampleid0")
MSG = load_fixture("engine_messages.json")


def test_refs_quote_and_escape_like_cedar():
    assert pe.ref("User", "hr-intern") == 'AgentCore::User::"hr-intern"'
    assert pe.ref("User", 'a"b\\c') == 'AgentCore::User::"a\\"b\\\\c"'
    with pytest.raises(ValueError):
        pe.ref("Launchpad::User", "x")  # one namespace only (amendment 1)


def test_the_user_descriptor_matches_the_decide_fixture():
    claim = pt.TokenPrincipal.user("hr-intern", role="member", user_id="u_hrintern01").claim()
    assert pe.principal_descriptor(claim) == MSG["engine_decide_request"]["principal"]


def test_the_agent_descriptor_and_actions_match_the_decide_fixture():
    req = MSG["engine_decide_request"]
    assert pe.agent_descriptor(ROLE, "ag_hrhelp01", method="zip_runtime", system=False) == \
        req["agent"]
    assert pe.tool_action("hr-database", "create_payout") == req["action"]
    assert pe.gateway_resource(GW) == req["resource"]
    assert pe.target_group("hr-database") == 'AgentCore::Action::"target:hr-database"'


def test_a_decide_request_built_from_these_parts_satisfies_the_schema():
    req = copy.deepcopy(MSG["engine_decide_request"])
    req["principal"] = pe.principal_descriptor(pt.TokenPrincipal.api_key(
        "key_demo0001", name="demo-key").claim())
    jsonschema.Draft202012Validator(contracts.load_schema("engine_decide_request")).validate(req)


def test_api_key_is_its_own_principal_with_no_parent_d1():
    d = pe.principal_descriptor(pt.TokenPrincipal.api_key("key_1", name="ops").claim())
    assert d == {"entity": 'AgentCore::ApiKey::"key_1"', "parents": [], "attrs": {"name": "ops"}}


def test_system_principals_carry_no_attrs():
    d = pe.principal_descriptor(pt.TokenPrincipal.system("canary").claim())
    assert d == {"entity": 'AgentCore::System::"canary"', "parents": [], "attrs": {}}


def test_an_unknown_principal_kind_is_refused():
    with pytest.raises(ValueError):
        pe.principal_descriptor({"kind": "agent", "id": "x", "label": "x"})


def test_the_agent_entity_graph_is_iam_entity_in_agent_in_workspace():
    graph = pe.agent_entities(ROLE, "ag_hrhelp01", "default")
    by_type = {e["uid"]["type"]: e for e in graph}
    assert by_type["AgentCore::IamEntity"]["uid"]["id"] == ROLE
    assert by_type["AgentCore::IamEntity"]["parents"] == [
        {"type": "AgentCore::Agent", "id": "ag_hrhelp01"}]
    assert by_type["AgentCore::Agent"]["parents"] == [
        {"type": "AgentCore::Workspace", "id": "default"}]
    assert by_type["AgentCore::Workspace"]["parents"] == []


def test_principal_entities_include_the_role_for_users():
    claim = pt.TokenPrincipal.user("demo-admin", role="admin").claim()
    graph = pe.principal_entities(claim)
    assert graph[0] == {"uid": {"type": "AgentCore::User", "id": "demo-admin"},
                        "attrs": {"username": "demo-admin", "role": "admin"},
                        "parents": [{"type": "AgentCore::Role", "id": "admin"}]}
    assert {"uid": {"type": "AgentCore::Role", "id": "admin"}, "attrs": {}, "parents": []} in graph


def test_the_d20_principal_bridges_to_the_token_principal():
    identity = type("I", (), {"username": "hr-intern", "role": "member", "user_id": "u_1"})()
    assert pt.TokenPrincipal.from_policy_principal(Principal.from_identity(identity)) == \
        pt.TokenPrincipal.user("hr-intern", role="member", user_id="u_1")
    key = type("K", (), {"id": "key_1", "name": "ops", "created_by_name": "carol"})()
    assert pt.TokenPrincipal.from_policy_principal(Principal.from_api_key(key)) == \
        pt.TokenPrincipal.api_key("key_1", name="ops")
    assert pt.TokenPrincipal.from_policy_principal(Principal.system("eval")) == \
        pt.TokenPrincipal.system("eval")
    # an agent acting for nobody has no on-behalf-of principal: stage BT sees the agent itself
    agent = type("A", (), {"id": "ag_1", "name": "hr"})()
    assert pt.TokenPrincipal.from_policy_principal(Principal.from_agent(agent)) is None


def test_the_d6_sender_is_the_principal_not_the_session_owner():
    owner = type("I", (), {"username": "hr-intern", "role": "member", "user_id": "u_1"})()
    admin = type("I", (), {"username": "demo-admin", "role": "admin", "user_id": "u_2"})()
    assert pt.principal_for_turn(sender=admin, session_owner=owner) == \
        pt.TokenPrincipal.user("demo-admin", role="admin", user_id="u_2")
