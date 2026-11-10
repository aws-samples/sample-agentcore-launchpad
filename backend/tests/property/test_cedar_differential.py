"""E07 governance — Cedar differential / property tests (round 16, channel A).

Oracles, never thresholds copied from code:

* **Metamorphic** — ``format`` is idempotent; Unicode literals and long statements validate
  and decide (§HTTP surface: ``/validate``, ``/format``) through the simulator
  (``policy_bundle.evaluate``, what ``POST /api/policies/simulate`` runs; ``call_tool`` only
  since Global parity — the runtime-guard differential and the narrowing state machine this
  file used to hold went with the guard and the narrowing, contract amendment 9).

Hermetic: the real ``CedarEngine`` (cedarpy), ``FakeS3`` for the publish copy, no AWS.
Hypothesis re-runs one test body many times inside ONE pytest test, so every example
resets the policy tables itself (``_reset``).
"""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import delete

from app.core import config
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.models.policy import Policy, PolicyChange, PolicyDecision, PolicySettings, PolicyVersion
from app.services import (
    aws_clients,
    policy_backend,
    policy_bundle,
    policy_cedar,
    policy_store,
)
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import Principal
from tests.conftest import ws_ctx
from tests.policy_fakes import FakeS3
from tests.test_policy_deploy_narrowing import RESOURCES, _agent, _gateway_spec, _publish, _set_mode

WS = DEFAULT_WORKSPACE_ID
SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
SCALE = 10 if SOAK else 1
EXAMPLES = 25 * SCALE
STEPS = 20 * SCALE

_OUTCOME = {"ALLOW": "allow", "DENY": "deny"}


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = CedarEngine()
    policy_backend.set_backend(eng)
    yield eng
    policy_backend.set_backend(None)


@pytest.fixture
def s3(monkeypatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    return fake


@pytest.fixture
def workspace():
    return ws_ctx(RESOURCES, id=WS)


@pytest.fixture
def knob(monkeypatch):
    def _set(name: str, value: str) -> None:
        monkeypatch.setenv(f"LAUNCHPAD_{name.upper()}", value)
        config.get_settings.cache_clear()

    yield _set
    config.get_settings.cache_clear()


def _reset() -> None:
    """Wipe every policy table and agent of the workspace between Hypothesis examples
    (the autouse ``clean_tables`` wipe runs once per pytest test, not per example)."""
    db = SessionLocal()
    try:
        for model in (PolicyDecision, PolicyChange, PolicyVersion, Policy, PolicySettings, Agent):
            db.execute(delete(model))
        db.commit()
    finally:
        db.close()
    policy_cedar.invalidate()


def _simulate(db, agent: Agent, tool: str) -> str:
    who = Principal.from_agent(agent)
    ev = policy_bundle.evaluate(
        db, WS, policy_bundle.call_tool_request(who, tool, agent_id=agent.id, channel="runtime"),
        principal=who, agent=agent, tool_ids=(tool,),
    )
    return ev.outcome


def _agent_row(db, agent_id: str) -> Agent:
    db.expire_all()
    return db.get(Agent, agent_id)


# ── strategies ──────────────────────────────────────────────────────────────

TARGETS = ["hr-database", "payroll", "hr数据库"]  # policy-side; the spec declares hr-database
TOOL_WORDS = ["get_employee", "create_payout", "delete_all", "list", "x" * 110]
DECLARED_TARGET = "hr-database"  # `_gateway_spec`'s record name (its wildcard is <name>___*)


def _declared(literals: list[str]) -> tuple[dict, list[str]]:
    """The spec and its declared Gateway surface: literal tool names when given (an
    `AgentSpec` cannot declare a wildcard literally), else the record's own wildcard."""
    if literals:
        tools = [f"{DECLARED_TARGET}___{w}" for w in literals]
        return _gateway_spec(tools=tools), tools
    return _gateway_spec(tools=None), [f"{DECLARED_TARGET}___*"]
CLAUSES = ("literal", "like_prefix", "target")


def _policy_text(kind: str, clause: str, target: str, word: str, prefix: str,
                 scoped_agent: str | None) -> str:
    principal = (
        f'principal == Launchpad::Agent::"{scoped_agent}"' if scoped_agent else "principal"
    )
    head = f'{kind}({principal}, action == Launchpad::Action::"call_tool", '
    if clause == "literal":
        return head + f'resource == Launchpad::Tool::"{target}___{word}");'
    if clause == "like_prefix":
        return head + f'resource) when {{ resource.name like "{target}___{prefix}*" }};'
    return head + f'resource) when {{ resource.target == "{target}" }};'


policy_strategy = st.builds(
    lambda kind, clause, target, word, prefix: (kind, clause, target, word, prefix),
    st.sampled_from(["permit", "forbid"]), st.sampled_from(CLAUSES), st.sampled_from(TARGETS),
    st.sampled_from(TOOL_WORDS), st.sampled_from(["", "get", "create", "del", "x"]),
)


# ── 5. Unicode / long statements, format idempotence ────────────────────────

_UNICODE_TOOL = "hr数据库___获取员工"


def _long_statement(clauses: int) -> str:
    conds = " || ".join(f'resource.name == "hr-database___tool_{i:04d}"' for i in range(clauses))
    return (f'permit(principal, action == Launchpad::Action::"call_tool", resource) '
            f'when {{ resource == Launchpad::Tool::"{_UNICODE_TOOL}" || {conds} }};')


@settings(deadline=None, max_examples=max(5, EXAMPLES // 5),
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(clauses=st.integers(min_value=50, max_value=90))
def test_unicode_and_long_statements_validate_decide_and_format_idempotently(
    engine, s3, workspace, clauses,
):
    """§HTTP surface — ``POST /validate`` answers ``{ok, errors, policy_ids}`` and
    ``POST /format`` a reformatted statement for any well-formed Cedar; a Unicode tool
    literal is an ordinary entity id (§Cedar schema: ``Tool.name: String``). Metamorphic:
    ``format(format(s)) == format(s)``; differential: the Unicode tool is ALLOW for the
    simulator and the guard alike, never ``NO_DECISION``."""
    _reset()
    statement = _long_statement(clauses)
    assert len(statement) >= 2000
    report = policy_store.validate_statement(statement)
    assert report.ok, report.as_dict()
    once = engine.format(statement)
    assert engine.format(once) == once
    assert engine.policy_ids(statement) == engine.policy_ids(once)
    # an AgentSpec cannot declare a Unicode tool name: the Unicode tool is ledger-known
    # through the policy text's `Tool::"…"` literal, under the record's declared wildcard
    agent_id = _agent(_gateway_spec(tools=None), agent_id="ag_unicode")
    _publish(workspace, "long_permit", statement)
    _set_mode("enforce")
    db = SessionLocal()
    try:
        agent = _agent_row(db, agent_id)
        for tool, expected in ((_UNICODE_TOOL, "allow"), ("hr-database___tool_0001", "allow"),
                               ("hr-database___tool_9999", "deny")):
            sim = _simulate(db, agent, tool)
            assert sim in _OUTCOME and _OUTCOME[sim] == expected
    finally:
        db.close()
