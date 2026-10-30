"""D20 P1-a nit (rev-r8, delivered by P1-d): every baseline template under
`app/policy/templates/*.cedar` validates against `schema.cedarschema.json` with the real
engine **after** its placeholders are substituted the way the editor prefill does — not
only as shipped (`test_policies_api` covers that). A placeholder a future template adds
without a substitution here fails loudly."""

from __future__ import annotations

import re

import pytest

from app.models.ledger import Agent
from app.policy import TEMPLATES_DIR, list_templates, schema_json
from app.services.policy_backend import AuthzRequest
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import (
    Principal,
    build_entities,
    request_context,
    tool_ref,
)

PLACEHOLDER = re.compile(r"__[A-Z_]+__")
# The substitutions the editor prefill performs (docs/cn/interfaces/policy-backend.md
# §"Baseline templates"); every placeholder a template carries must be listed here.
SUBSTITUTIONS = {
    "__AGENT_ID__": "ag_hr_assistant",
    "__TOOL_PATTERN__": "hr-database___*",
    "__TOOL_ID__": "hr-database___create_payout",
    "__WORKSPACE_ID__": "default",
}
EXPECTED_PLACEHOLDERS = {
    "permit_agent_declared_tools": {"__AGENT_ID__", "__TOOL_PATTERN__"},
    "forbid_tool_for_members": {"__TOOL_ID__"},
    # Global parity (amendment 9): the two call_tool baselines — Stage A (every agent of the
    # workspace may call its declared tools) and Stage BT (members / admins / system principals)
    "permit_workspace_agents_call_tools": {"__WORKSPACE_ID__"},
    "permit_users_call_tools": set(),
}
AGENT = Agent(id="ag_hr_assistant", workspace_id="default", name="hr", method="zip_runtime",
              status="active", spec={})
TOOLS = ["hr-database___create_payout", "hr-database___get_employee"]


def _fill(statement: str) -> str:
    for key, value in SUBSTITUTIONS.items():
        statement = statement.replace(key, value)
    return statement


@pytest.fixture(scope="module")
def engine() -> CedarEngine:
    return CedarEngine()


# The console builders (`frontend/src/lib/governance.ts::buildPermitWorkspaceAgentsCallTools`
# / `buildPermitUsersCallTools`, r17-bp) must emit exactly these — each server template's
# canonical form (comment lines dropped), `__WORKSPACE_ID__` substituted client-side.
# D20-obs2 still holds: a user has ONE Role parent, so a member-only baseline default-denies
# administrators; the users baseline names member, admin and the system principals.
CONSOLE_PERMIT_WORKSPACE_AGENTS_CALL_TOOLS = '''permit(
  principal in Launchpad::Workspace::"__WORKSPACE_ID__",
  action == Launchpad::Action::"call_tool",
  resource
);'''

CONSOLE_PERMIT_USERS_CALL_TOOLS = '''permit(
  principal in Launchpad::Role::"member",
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal in Launchpad::Role::"admin",
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal is Launchpad::System,
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal is Launchpad::ApiKey,
  action == Launchpad::Action::"call_tool",
  resource
);'''

CONSOLE_BASELINE_PINS = {
    "permit_workspace_agents_call_tools": CONSOLE_PERMIT_WORKSPACE_AGENTS_CALL_TOOLS,
    "permit_users_call_tools": CONSOLE_PERMIT_USERS_CALL_TOOLS,
}


def _canonical(statement: str) -> str:
    return "\n".join(
        line for line in statement.splitlines() if not line.strip().startswith("//")
    ).strip()


@pytest.mark.parametrize("name", sorted(CONSOLE_BASELINE_PINS))
def test_console_baseline_builders_emit_the_templates_canonical_form(name):
    """What the console emits without the server template (`fromServer` absent) is byte-equal
    to the canonical form of the shipped template, so a prefilled baseline and a hand-published
    one compile to the same policy ids and the same decisions."""
    served = next(t["statement"] for t in list_templates() if t["name"] == name)
    assert _canonical(served) == CONSOLE_BASELINE_PINS[name]



def test_every_template_file_is_served_and_carries_only_known_placeholders():
    files = {p.stem for p in TEMPLATES_DIR.glob("*.cedar")}
    served = {t["name"] for t in list_templates()}
    assert files == served == set(EXPECTED_PLACEHOLDERS)
    for t in list_templates():
        found = set(PLACEHOLDER.findall(t["statement"]))
        assert found == EXPECTED_PLACEHOLDERS[t["name"]], t["name"]
        assert found <= set(SUBSTITUTIONS), f"{t['name']}: unknown placeholder {found}"


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_template_validates_against_the_schema_with_placeholders_substituted(engine, name):
    statement = _fill(next(t["statement"] for t in list_templates() if t["name"] == name))
    assert not PLACEHOLDER.search(statement), statement
    report = engine.validate(statement, schema_json())
    assert report.ok, [e.msg for e in report.errors]
    assert len(report.policy_ids) == (4 if name == "permit_users_call_tools" else 1)
    # formatting round-trips (the editor's FORMAT button on a prefilled statement)
    assert engine.validate(engine.format(statement), schema_json()).ok


def test_shipped_templates_validate_before_substitution_too(engine):
    """What `GET /entities.templates` serves: a `__TOOL_ID__` literal is a valid (if
    useless) entity id, so the raw template validates as well — the editor shows the
    placeholder, the operator fills it. Pinned so a schema change cannot break the prefill."""
    for t in list_templates():
        assert engine.validate(t["statement"], schema_json()).ok, t["name"]


def test_substituted_templates_decide_as_their_comments_promise(engine):
    """The semantic check behind the validation: with every template substituted and
    compiled as one set — the agent may call its declared tools, members
    may call tools (``permit_users_call_tools``, Global parity), and the forbidden payout tool
    is denied to the agent and to members while an administrator keeps it (the admin
    exemption lifts the forbid and the users permit allows the call)."""
    statement = "\n".join(_fill(t["statement"]) for t in list_templates())
    handle = engine.compile(statement)
    member = Principal.from_request("user", "river", role="member")
    admin = Principal.from_request("user", "ops", role="admin")
    key = Principal.from_request("api_key", "key_ci")  # option D: a /v1 key = Global's M2M client
    agent_principal = Principal.from_request("agent", "ag_hr_assistant")

    def decide(principal, action, resource, **ctx):
        entities = build_entities(principal, workspace_id="default", agent=AGENT,
                                  tool_ids=TOOLS, gateway_id="gw-1")
        request = AuthzRequest(principal=principal.ref(), action=action, resource=resource,
                               context=request_context(action, **ctx))
        decision = engine.authorize(request, handle, entities)
        assert decision.decision != "no_decision", decision.errors
        return decision.decision

    payout, employee = tool_ref(TOOLS[0]), tool_ref(TOOLS[1])
    assert decide(agent_principal, "call_tool", employee, channel="chat",
                  agent_id="ag_hr_assistant") == "allow"
    assert decide(agent_principal, "call_tool", payout, channel="chat",
                  agent_id="ag_hr_assistant") == "deny"
    assert decide(member, "call_tool", payout, channel="chat",
                  agent_id="ag_hr_assistant") == "deny"
    assert decide(member, "call_tool", employee, channel="chat",
                  agent_id="ag_hr_assistant") == "allow"
    assert decide(admin, "call_tool", payout, channel="chat",
                  agent_id="ag_hr_assistant") == "allow"
    # an API key is covered by the baseline like Global's M2M client, and the admin-only
    # forbid denies it like a member
    assert decide(key, "call_tool", employee, channel="v1",
                  agent_id="ag_hr_assistant") == "allow"
    assert decide(key, "call_tool", payout, channel="v1",
                  agent_id="ag_hr_assistant") == "deny"


# ── Console prefill shapes (frontend/src/lib/governance.ts) ──────────────────────────────
# The console builds Cedar text client-side; its vitest pins the strings, not their validity.
# These are the exact forms the builders emit (rev-r8 on p1-c3: a plural allowlist must be a
# `when { resource in [...] }` clause — `resource in <set>` in the scope is rejected by Cedar).
CONSOLE_PREFILL_FORMS = {
    "singular_concrete": (
        'permit(principal == Launchpad::Agent::"agt_1", action == Launchpad::Action::"call_tool", '
        'resource == Launchpad::Tool::"crm___getCustomer");'
    ),
    "plural_concrete_when": (
        'permit(principal == Launchpad::Agent::"agt_1", action == Launchpad::Action::"call_tool", '
        'resource) when { resource in [Launchpad::Tool::"a___x", Launchpad::Tool::"b___y"] };'
    ),
    "pattern": (
        'permit(principal == Launchpad::Agent::"agt_1", action == Launchpad::Action::"call_tool", '
        'resource) when { resource.name like "crm___*" };'
    ),
    "mixed": (
        'permit(principal == Launchpad::Agent::"agt_1", action == Launchpad::Action::"call_tool", '
        'resource) when { resource.name like "crm___*" || '
        'resource in [Launchpad::Tool::"wiki___search", Launchpad::Tool::"hr___get"] };'
    ),
    "forbid_pattern": (
        'forbid(principal, action == Launchpad::Action::"call_tool", resource) '
        'when { resource.name like "payroll___*" } '
        'unless { principal in Launchpad::Role::"admin" };'
    ),
}


@pytest.mark.parametrize("name", sorted(CONSOLE_PREFILL_FORMS))
def test_console_prefill_forms_validate_against_the_schema(engine, name):
    report = engine.validate(CONSOLE_PREFILL_FORMS[name], schema_json())
    assert report.ok, (name, report.errors)


def test_plural_allowlist_in_the_scope_is_rejected_by_cedar(engine):
    """The form p1-c shipped (and its vitest pinned) is invalid Cedar — keep this red-check so a
    string-only frontend test can never mask it again."""
    bad = (
        'permit(principal == Launchpad::Agent::"agt_1", action == Launchpad::Action::"call_tool", '
        'resource in [Launchpad::Tool::"a___x", Launchpad::Tool::"b___y"]);'
    )
    assert not engine.validate(bad, schema_json()).ok
