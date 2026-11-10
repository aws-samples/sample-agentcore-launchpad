"""``CedarEngine`` on the real cedarpy wheel (plan §6 — test_policy_engine).

Allow / deny / default-deny, parse error → ``no_decision`` (never allow), schema
validation errors, FORMAT, ``policy_ids`` from ``@id`` annotations, ``annotate`` stamping,
batch = N singles, the request-context filter and the perf budget.
"""

import time

import pytest

from app.policy import CONTEXT_ATTRIBUTES, schema_json
from app.services import policy_backend
from app.services.policy_backend import AuthzRequest, EntityRef
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import (
    Principal,
    build_entities,
    request_context,
    tool_ref,
)
from tests.policy_fakes import BASELINE, BROKEN, FORBID_KEY, TOOLS_ADMIN_ONLY


class _Agent:
    id = "ag_hr"
    name = "hr-agent"
    method = "zip_runtime"
    created_by = "u1"
    created_by_name = "river"  # the Cedar `owner` attribute
    system_key = None
    workspace_id = "default"
    spec: dict = {}


class _Identity:
    username = "river"
    role = "member"
    user_id = "u1"


class _Key:
    id = "key_1"
    name = "ci-key"
    created_by = "u1"
    created_by_name = "river"  # the Cedar `created_by` attribute


@pytest.fixture
def engine():
    eng = CedarEngine()
    policy_backend.set_backend(eng)
    return eng


EMPLOYEE = "hr-database___get_employee"


def _call(principal: Principal, agent=_Agent, tool: str = EMPLOYEE) -> AuthzRequest:
    """``call_tool`` on ``tool`` from ``agent`` — the only decision shape (Global parity)."""
    return AuthzRequest(
        principal=principal.ref(), action="call_tool", resource=tool_ref(tool),
        context=request_context("call_tool", channel="chat", agent_id=agent.id),
    )


def test_member_is_allowed_and_an_unknown_key_is_denied_by_default(engine):
    member = Principal.from_identity(_Identity())
    entities = build_entities(member, workspace_id="default", agent=_Agent(), tool_ids=[EMPLOYEE])
    allowed = engine.authorize(_call(member), BASELINE, entities)
    assert allowed.decision == "allow" and allowed.allowed
    assert allowed.reasons == ["policy0"]  # positional id — no @id in BASELINE

    key = Principal.from_api_key(_Key())
    entities = build_entities(key, workspace_id="default", agent=_Agent(), tool_ids=[EMPLOYEE])
    denied = engine.authorize(_call(key), BASELINE, entities)
    assert denied.decision == "deny" and denied.reasons == []  # Cedar default-deny


def test_forbid_wins_and_reports_the_forbidding_policy(engine):
    key = Principal.from_api_key(_Key())
    policies = BASELINE + FORBID_KEY.replace("__KEY__", "key_1")
    entities = build_entities(key, workspace_id="default", agent=_Agent(), tool_ids=[EMPLOYEE])
    denied = engine.authorize(_call(key), policies, entities)
    assert denied.decision == "deny"
    assert denied.reasons == ["policy4"]


def test_parse_error_is_no_decision_never_allow(engine):
    member = Principal.from_identity(_Identity())
    entities = build_entities(member, workspace_id="default", agent=_Agent(), tool_ids=[EMPLOYEE])
    result = engine.authorize(_call(member), BROKEN, entities)
    assert result.decision == "no_decision" and not result.allowed
    assert result.errors and "parse" in result.errors[0].lower()
    with pytest.raises(ValueError):
        engine.compile(BROKEN)


def test_validate_reports_ok_errors_and_policy_ids(engine):
    ok = engine.validate(BASELINE, schema_json())
    assert ok.ok and ok.errors == [] and ok.policy_ids == ["policy0", "policy1", "policy2",
                                                           "policy3"]

    bad_type = ('permit(principal == Launchpad::Nope::"x", '
                'action == Launchpad::Action::"invoke", resource);')
    report = engine.validate(bad_type, schema_json())
    assert not report.ok
    assert any("Launchpad::Nope" in e.message for e in report.errors)
    assert report.errors[0].policy_id == "policy0"
    body = report.as_dict()
    assert set(body) == {"ok", "errors", "warnings", "policy_ids", "scope"}
    assert body["scope"] == "launchpad_schema"
    assert set(body["errors"][0]) == {"line", "col", "msg", "policy_id"}

    broken = engine.validate(BROKEN, schema_json())
    assert not broken.ok and broken.policy_ids == []

    # the schema cannot type tool arguments — a context.input rule is a 422, not silent
    ctx_input = ('permit(principal, action == Launchpad::Action::"call_tool", resource) '
                 'when { context.input.amount < 1000 };')
    assert not engine.validate(ctx_input, schema_json()).ok
    assert not engine.validate("   ", schema_json()).ok


def test_annotated_ids_are_reported_as_reasons(engine):
    annotated = '@id("pol_a:v1")\n' + BASELINE.strip().splitlines()[0]
    assert engine.policy_ids(annotated) == ["pol_a:v1"]
    member = Principal.from_identity(_Identity())
    entities = build_entities(member, workspace_id="default", agent=_Agent(), tool_ids=[EMPLOYEE])
    assert engine.authorize(_call(member), annotated, entities).reasons == ["pol_a:v1"]


def test_annotate_stamps_every_policy_and_round_trips(engine):
    stamped = engine.annotate(BASELINE, "pol_x:v2")
    assert engine.policy_ids(stamped) == ["pol_x:v2#0", "pol_x:v2#1", "pol_x:v2#2",
                                          "pol_x:v2#3"]
    single = engine.annotate('@id("author") permit(principal, action, resource);', "pol_y:v1")
    assert engine.policy_ids(single) == ["pol_y:v1"]  # author ids are replaced, not kept
    with pytest.raises(ValueError):
        engine.annotate(BROKEN, "pol_z:v1")


def test_format_round_trip_and_error(engine):
    out = engine.format("permit(principal,action,resource);")
    assert out.strip() == "permit (principal, action, resource);"
    with pytest.raises(ValueError):
        engine.format(BROKEN)


def test_tool_rules_admin_vs_member(engine):
    agent = _Agent()
    tools = ["hr-database___get_employee", "hr-database___create_payout", "crm___lookup"]
    member = Principal.from_identity(_Identity())
    entities = build_entities(member, workspace_id="default", agent=agent, tool_ids=tools)
    requests = [
        AuthzRequest(member.ref(), "call_tool", tool_ref(t),
                     request_context("call_tool", channel="chat", agent_id=agent.id))
        for t in tools
    ]
    batch = engine.authorize_batch(requests, TOOLS_ADMIN_ONLY, entities)
    assert [d.decision for d in batch] == ["allow", "deny", "deny"]
    singles = [engine.authorize(r, TOOLS_ADMIN_ONLY, entities) for r in requests]
    assert [d.decision for d in singles] == [d.decision for d in batch]

    class _Admin(_Identity):
        role = "admin"

    admin = Principal.from_identity(_Admin())
    entities = build_entities(admin, workspace_id="default", agent=agent, tool_ids=tools)
    payout = AuthzRequest(admin.ref(), "call_tool", tool_ref(tools[1]),
                          request_context("call_tool", channel="chat", agent_id=agent.id))
    assert engine.authorize(payout, TOOLS_ADMIN_ONLY, entities).decision == "allow"
    assert engine.authorize_batch([], TOOLS_ADMIN_ONLY, entities) == []


def test_request_context_filters_to_the_schema():
    ctx = request_context("call_tool", channel="chat", agent_id="a", input={"amount": 5}, extra=1)
    assert ctx == {"channel": "chat", "agent_id": "a"}
    assert request_context("list_tool", agent_id="a", channel="v1") == {"agent_id": "a"}
    assert request_context("call_tool", agent_id="a") == {"channel": "", "agent_id": "a"}
    # Global parity (2026-10-27): `invoke` left the vocabulary with the hub gate
    assert set(CONTEXT_ATTRIBUTES) == {"call_tool", "list_tool"}
    with pytest.raises(ValueError):
        request_context("fly")
    with pytest.raises(ValueError):
        request_context("invoke", channel="chat")


def test_entities_match_the_schema_exactly(engine):
    member = Principal.from_identity(_Identity())
    key = Principal.from_api_key(_Key())
    agent = _Agent()
    entities = build_entities(member, workspace_id="default", agent=agent,
                              tool_ids=["hr-database___get_employee"], gateway_id="gw1")
    by_uid = {(e["uid"]["type"], e["uid"]["id"]): e for e in entities}
    assert by_uid[("Launchpad::User", "river")]["attrs"] == {
        "username": "river", "role": "member", "user_id": "u1"}
    assert by_uid[("Launchpad::User", "river")]["parents"] == [
        {"type": "Launchpad::Role", "id": "member"}]
    assert by_uid[("Launchpad::Agent", "ag_hr")]["attrs"] == {
        "name": "hr-agent", "method": "zip_runtime", "system": False, "owner": "river"}
    assert by_uid[("Launchpad::Tool", "hr-database___get_employee")]["attrs"] == {
        "name": "hr-database___get_employee", "tool": "get_employee", "target": "hr-database"}
    assert ("Launchpad::Gateway", "gw1") in by_uid and ("Launchpad::Role", "admin") in by_uid
    assert key.entities()[0]["attrs"] == {"name": "ci-key", "created_by": "river"}
    assert Principal.system().ref() == EntityRef("Launchpad::System", "hub")
    # an undeclared attribute would be NoDecision — prove the schema accepts ours
    assert engine.authorize(_call(member), BASELINE, entities).decision == "allow"


def test_perf_budget_fifty_policies_hundred_requests(engine):
    many = "\n".join(
        f'permit(principal in Launchpad::Role::"member", action == Launchpad::Action::"call_tool", '
        f'resource) when {{ resource.name == "hr-database___tool{i}" }};' for i in range(50)
    )
    handle = engine.compile(many)
    member = Principal.from_identity(_Identity())
    entities = build_entities(member, workspace_id="default", agent=_Agent(), tool_ids=[EMPLOYEE])
    request = _call(member)
    started = time.perf_counter()
    for _ in range(100):
        engine.authorize(request, handle, entities)
    assert time.perf_counter() - started < 1.0
