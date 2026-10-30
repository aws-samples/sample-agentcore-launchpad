"""A6 parity: the same stored D20 set decided by cedarpy (today's hub path) and by the engine
(installed through A5's real compile → install path) must agree before a workspace may route
to the engine. Skipped unless the engine binary is built.

* tool-call decisions agree for every principal (users, system, API key, the agent itself —
  Stage A) × agent × tool — the only decision kind left (Global parity, amendment 9: invoking
  an agent is not a policy decision, nothing is narrowed at deploy time);
* a per-user forbid is a FINAL decision for that user on both paths (the member is denied,
  the administrator keeps the tool);
* agent scope + spec pins (N17-obs484, fixed by A5a-2): a scoped permit never reaches another
  agent, a scoped forbid never over-denies, a pinned policy applies to its pinner only.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.models.policy import Policy, PolicyVersion
from app.services import policy_backend, policy_bundle, policy_cedar, policy_store
from app.services import policy_engine_control as control
from app.services import policy_engine_decide as decide
from app.services import policy_engine_sync as sync
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import Principal
from tests.policy_principal.engine_harness import ASSETS, ENGINE, Engine

pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="engine binary not built")

WS = "default"
TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "policy" / "templates"
# the Global-parity default-deny baseline (r17-bn's two templates): Stage A — every agent of
# the workspace may call its declared tools (`permit_workspace_agents_call_tools`) — and
# Stage BT — members, administrators and system principals may call every tool
# (`permit_users_call_tools`). The engine needs BOTH halves (two stages); cedarpy's flat D20
# model needs either, so the parity oracle always installs both.
AGENTS_CALL_TOOLS = ('permit(principal in Launchpad::Workspace::"default", '
                     'action == Launchpad::Action::"call_tool", resource);')
USERS_CALL_TOOLS = (
    'permit(principal in Launchpad::Role::"member", action == Launchpad::Action::"call_tool", '
    'resource);\n'
    'permit(principal in Launchpad::Role::"admin", action == Launchpad::Action::"call_tool", '
    'resource);\n'
    'permit(principal is Launchpad::System, action == Launchpad::Action::"call_tool", resource);'
)
PERMIT_HR = ('permit(principal, action == Launchpad::Action::"call_tool", resource) '
             'when { resource.name like "hr-database___*" };')
PAYOUT_ADMIN_ONLY = (TEMPLATES / "forbid_tool_for_members.cedar").read_text().replace(
    "__TOOL_ID__", "hr-database___create_payout")
TOOLS = ["hr-database___get_employee", "hr-database___create_payout",
         "hr-approvals___approve_payout"]


def _user(name: str, role: str) -> Principal:
    return Principal(kind="user", id=name, label=name, role=role,
                     attrs={"username": name, "role": role})


PRINCIPALS = [_user("hr-intern", "member"), _user("demo-admin", "admin"), Principal.system(),
              Principal(kind="api_key", id="key_9f3a", label="ci", attrs={"name": "ci"})]


@pytest.fixture
def make_world(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    engine.start()
    monkeypatch.setattr(control, "call", lambda shard, message, **_kw: engine.control_call(message))
    # ONE real engine process stands for the shards: with the hub's per-shard install loop the
    # same install would reach it twice and the engine's version CAS (N17-obs492) refuses the
    # second — so the world is a one-shard engine
    monkeypatch.setattr(control, "shard_count", lambda: 1)
    tools = json.loads((ASSETS / "demo-tools.json").read_text())["result"]
    monkeypatch.setattr(sync, "tools_list", lambda ws: tools)
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    policy_backend.set_backend(CedarEngine())
    db = SessionLocal()

    def build(policies: list[dict], agents: list[dict]):
        owner = policy_store.settings_row(db, WS)
        for a in agents:
            db.add(Agent(id=a["id"], workspace_id=WS, name=a["id"].replace("_", "-"),
                         method="zip_runtime", status="active",
                         spec={"policy_ids": a.get("pins", [])}))
        for p in policies:
            db.add(Policy(id=p["id"], workspace_id=WS, engine_id=owner.id,
                          name=p["id"], status="active",
                          scope=p.get("scope", "workspace"), agent_ids=p.get("agent_ids", []),
                          enforcement_mode="active", active_version=1))
            db.add(PolicyVersion(workspace_id=WS, policy_id=p["id"], version=1,
                                 statement=p["statement"], dialect="cedar", schema_digest="x"))
        db.flush()
        assert sync.install_workspace(db, WS) is not None
        db.commit()
        policy_cedar.invalidate(WS)
        return {a["id"]: db.get(Agent, a["id"]) for a in agents}

    try:
        yield db, build
    finally:
        db.close()
        engine.stop()
        policy_cedar.invalidate(WS)


def _tool_both(db, principal, agent, tool):
    """One ``call_tool`` decided by cedarpy (the simulator's hub path) and by the engine."""
    with patch.object(sync, "engine_decides", return_value=False):
        cedar = policy_bundle.evaluate(
            db, WS, policy_bundle.call_tool_request(
                principal, tool, agent_id=agent.id, channel="chat"),
            principal=principal, agent=agent, tool_ids=(tool,))
    engine = decide.evaluate_tool(db, WS, principal, agent, tool, channel="chat")
    return cedar.outcome, engine.outcome


def _tools_both(db, agent, principal=None):
    """Every tool of ``TOOLS`` for ``principal`` (default: the agent itself — Stage A)."""
    who = principal or Principal.from_agent(agent)
    both = {t: _tool_both(db, who, agent, t) for t in TOOLS}
    return ({t: c.lower() for t, (c, _e) in both.items()},
            {t: e.lower() for t, (_c, e) in both.items()})


def test_tool_decisions_agree_for_every_principal_on_a_workspace_wide_set(make_world):
    db, build = make_world
    agents = build([{"id": "pol_agents", "statement": AGENTS_CALL_TOOLS},
                    {"id": "pol_users", "statement": USERS_CALL_TOOLS},
                    {"id": "pol_hr", "statement": PERMIT_HR}],
                   [{"id": "ag_a"}, {"id": "ag_b"}])
    # the agent itself is never a Stage-BT principal on the engine (the interceptor always
    # has a user / system / key token), so parity is over the token principals only
    for agent in agents.values():
        for p in PRINCIPALS:
            for tool in TOOLS:
                cedar, engine = _tool_both(db, p, agent, tool)
                assert cedar == engine, (p.kind, p.id, agent.id, tool, cedar, engine)
        # the API key has no user permit: only PERMIT_HR's any-principal rule lets it through
        key = PRINCIPALS[-1]
        assert _tool_both(db, key, agent, "hr-approvals___approve_payout") == ("DENY", "DENY")
        assert _tool_both(db, key, agent, "hr-database___get_employee") == ("ALLOW", "ALLOW")


def test_a_per_user_forbid_is_final_for_that_user_on_both_paths(make_world):
    """Global parity: there is no deploy-time projection any more — a per-user rule is decided
    at call time for the calling user, identically by cedarpy and the engine: the member is
    denied the payout tool, the administrator keeps it, the agent's own (Stage A) question
    is unaffected by the per-user forbid's `unless` clause only for the admin."""
    db, build = make_world
    agents = build([{"id": "pol_agents", "statement": AGENTS_CALL_TOOLS},
                    {"id": "pol_hr", "statement": PERMIT_HR},
                    {"id": "pol_payout", "statement": PAYOUT_ADMIN_ONLY}], [{"id": "ag_a"}])
    payout = "hr-database___create_payout"
    assert _tool_both(db, _user("hr-intern", "member"), agents["ag_a"], payout) == (
        "DENY", "DENY")
    assert _tool_both(db, _user("demo-admin", "admin"), agents["ag_a"], payout) == (
        "ALLOW", "ALLOW")
    assert _tool_both(db, _user("hr-intern", "member"), agents["ag_a"],
                      "hr-database___get_employee") == ("ALLOW", "ALLOW")


