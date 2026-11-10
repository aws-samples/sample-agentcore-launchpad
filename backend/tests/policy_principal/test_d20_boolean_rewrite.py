"""The editor/install regression: tool predicates retain Boolean structure and polarity."""

import itertools

import pytest

from app.policy import dogwood_compiler as dc
from app.services.policy_backend import AuthzRequest
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import Principal, build_entities, request_context, tool_ref
from tests.policy_principal.engine_harness import AGENT, user
from tests.policy_principal.test_dogwood_compiler import needs_engine

OFFICE = "office-facts___lookup"
FORECAST = "paipai___paipai-forecast___forecast"
OTHER = "unrelated___lookup"
TOOLS = {"tools": [
    {"name": name, "description": "regression", "inputSchema": {"type": "object", "properties": {}}}
    for name in (OFFICE, FORECAST, OTHER)
]}
HEADER = ('permit (principal in Launchpad::Role::"admin", '
          'action == Launchpad::Action::"call_tool", resource)')
PREFIX = 'resource.name like "office-facts___*"'
EXACT = f'resource == Launchpad::Tool::"{FORECAST}"'
ORIGINAL = f"{HEADER} when {{ {PREFIX} || {EXACT} }};"
BASELINE = ('permit(principal is AgentCore::IamEntity, '
            'action in AgentCore::Action::"CallTool", resource);')

CASES = {
    "original_v4": ORIGINAL,
    "nested": f'{HEADER} when {{ ({PREFIX} || {EXACT}) && context.channel == "chat" }};',
    "unless": f"{HEADER} unless {{ {PREFIX} || {EXACT} }};",
    "nested_unless": f'{HEADER} when {{ {PREFIX} || {EXACT} }} '
                     f'unless {{ {EXACT} && context.channel == "runtime" }};',
    "multiple": f'{HEADER} when {{ {PREFIX} }}; {HEADER} when {{ {EXACT} }};',
    "forbid": f'{ORIGINAL} forbid(principal is Launchpad::User, '
              'action == Launchpad::Action::"call_tool", '
              f'resource) when {{ {EXACT} }} unless {{ context.channel == "chat" }};',
    "name_equal": f'{HEADER} when {{ resource.name == "{OFFICE}" || {EXACT} }};',
    "target": f'{HEADER} when {{ resource.target == "office-facts" || {EXACT} }};',
    "reverse": f'{HEADER} when {{ Launchpad::Tool::"{FORECAST}" == resource || {PREFIX} }};',
    "negation": f'{HEADER} when {{ !({PREFIX} || {EXACT}) }};',
    "precedence": f'{HEADER} when {{ {PREFIX} || {EXACT} && context.channel == "chat" }};',
    "literal_like": f'{HEADER} when {{ resource.name like "{OFFICE}" || {EXACT} }};',
    "mixed_set": f'{HEADER} when {{ {PREFIX} || resource in '
                 f'[Launchpad::Tool::"{FORECAST}"] }};',
    "unless_set": f'{HEADER} unless {{ resource in [Launchpad::Tool::"{OFFICE}", '
                  f'Launchpad::Tool::"{FORECAST}"] }};',
    "record_fields": f'{HEADER} when {{ ({{Value: context.channel}}).Value == "chat" && '
                     f'({PREFIX} || {EXACT}) }};',
}


@needs_engine
@pytest.mark.parametrize("case", CASES)
def test_rewritten_decisions_match_original_cedar(case):
    """Both phases participate: a real Agent allow baseline prevents vacuous phase-A denial."""
    original = CASES[case]
    rewritten = dc.rewrite_d20(original)
    assert not rewritten.warnings
    policies = [
        {"id": "agents", "version": 1, "mode": "active", "statement": BASELINE},
        {"id": "regression", "version": 4, "mode": "active", "statement": rewritten.statement},
    ]
    compiled = dc.compile(TOOLS, policies)
    assert compiled.ok, compiled.errors
    cedar = CedarEngine()
    handle = cedar.compile(original)
    results = set()
    for role, tool, channel in itertools.product(
        ("admin", "member"), (OFFICE, FORECAST, OTHER), ("chat", "runtime"),
    ):
        principal = Principal.from_request("user", f"test-{role}", role=role)
        old = cedar.authorize(
            AuthzRequest(principal.ref(), "call_tool", tool_ref(tool),
                         request_context("call_tool", channel=channel, agent_id="ag_hrhelp01")),
            handle, build_entities(principal, workspace_id="default", tool_ids=(tool,)),
        )
        assert not old.errors, old.errors
        out = dc.replay(TOOLS, policies, agent=AGENT, channel=channel, ws="default",
                        request={"tool": tool, "principal": user(principal.id, role),
                                 "input": {}, "at_s": 1000})
        assert out["ok"], out
        assert out["decision"]["stage_a"] == "allow", out
        assert out["decision"]["decision"] == old.decision, (case, role, tool, channel, out)
        results.add(old.decision)
        if case == "original_v4":
            assert old.decision == (
                "allow" if role == "admin" and tool in (OFFICE, FORECAST) else "deny"
            )
    assert results == {"allow", "deny"}


def test_comments_and_strings_are_not_rewritten_or_dropped():
    text = (f'// Launchpad::Action::"invoke" is just a comment\n'
            f'{HEADER} when {{ context.channel == "resource.name Launchpad::Agent::x; '
            'context.channel" };')
    out = dc.rewrite_d20(text)
    assert not out.warnings
    assert '"resource.name Launchpad::Agent::x; context.channel"' in out.statement
    assert "context.launchpad" in out.statement
    body = next(iter(CedarEngine().parse_policies(out.statement).values()))["conditions"][0]["body"]
    assert body["=="]["left"] == {
        ".": {"left": {".": {"left": {"Var": "context"}, "attr": "launchpad"}}, "attr": "channel"},
    }


@needs_engine
def test_unsupported_pattern_fails_closed():
    rewritten = dc.rewrite_d20(f'{HEADER} when {{ resource.name like "office-*" }};')
    assert rewritten.warnings
    result = dc.compile(TOOLS, [{"id": "unsupported", "version": 1, "mode": "active",
                                "statement": rewritten.statement}])
    assert not result.ok
    assert any("name" in error["message"] for error in result.errors)
