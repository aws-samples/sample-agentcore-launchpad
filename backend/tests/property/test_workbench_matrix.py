# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""E01 Workbench — the overview payload against the visibility rule and the list endpoints
(round 16, r16-g; `docs/cn/plans/BUG-DISCOVERY-PLAN.md` §E01: "capabilities × role ×
feature switches → the card set equals the capabilities declaration").

Contract sources:

* `docs/cn/acceptance/E01-workbench.md` AC-E01-02 (health rows: bootstrap rows follow
  `services[id]`, `runtime` Ready iff ≥ 1 agent `active`, `evaluation` lights with the first
  completed run and "can never contradict" the EVAL PASS RATE tile), AC-E01-04 (`eval_runs`
  / `eval_pass_rate`, `null` pass rate before any run), NC-E01-08 (`policy` row off-feature).
* `docs/cn/interfaces/users-and-ownership.md` §7 (isolated: a member sees a resource iff
  its creator is theirs, NULL, or a *current* admin's; admins see all) — the overview's
  counters are "the viewer's", so a member's tile must never count a hidden agent's
  sessions, A2A record or run.
* `docs/cn/interfaces/features.md` (`GET /api/overview` keeps the `policy` keys with
  `false` / `""` in an off-feature build).

Oracles are differential: the overview counters are recomputed from (1) the seeded ledger
state through the §7 rule and (2) the list endpoints the same viewer sees
(`GET /api/agents`, `GET /api/eval/runs`). Hypothesis draws the ledger state (0 / 1 / N
agents, failed deployments, running jobs, stale sessions, runs in every status).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import app.routers.overview as overview_mod
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, engine
from app.evaluation.models import EvalRun
from app.main import create_app
from app.models.ledger import Agent, ChatSession, Workspace
from app.services import registry_backend, visibility
from tests.test_agent_visibility import _agent, _StubRegistry
from tests.test_visibility import (  # noqa: F401 — fixtures by name
    PEER,
    _add,
    _member,
    admin,
    app,
)

# ─── the world: one admin, two members, one member-turned-admin ──────────────


@pytest.fixture
def world(app, admin):
    """Viewers: `bob` (member), `ada` (another member), `carol` (admin row), the config admin."""
    ada_id, ada_client = _member(app, "ada")
    bob_id, bob_client = _member(app, "bob")
    carol_id, carol_client = _member(app, "carol", role="admin")
    return SimpleNamespace(
        admin=admin, ada=ada_client, bob=bob_client, carol=carol_client,
        ids={"ada": ada_id, "bob": bob_id, "carol": carol_id, None: None},
        admin_ids={carol_id},
    )


def _wipe(*models: type) -> None:
    with engine.begin() as conn:
        for model in models:
            conn.execute(model.__table__.delete())


def _set_mode(mode: str) -> None:
    db = SessionLocal()
    try:
        visibility.set_mode(db, db.get(Workspace, DEFAULT_WORKSPACE_ID), mode)
    finally:
        db.close()


# ─── the ledger-state strategy ────────────────────────────────────────────────

CREATORS = st.sampled_from(["ada", "bob", "carol", None])
AGENT = st.fixed_dictionaries({
    "creator": CREATORS,
    "status": st.sampled_from(["active", "deploying", "failed", "deleted"]),
    "record": st.booleans(),  # has an A2A registry record
    "record_status": st.sampled_from(["APPROVED", "DRAFT", "DEPRECATED"]),
    "fresh_sessions": st.integers(0, 2),  # last_at inside the 24 h window
    "stale_sessions": st.integers(0, 1),  # last_at 3 days ago
})
RUN = st.fixed_dictionaries({
    "creator": CREATORS,
    "status": st.sampled_from(["completed", "running", "failed", "queued"]),
    "scores": st.lists(st.sampled_from([0.0, 0.25, 0.5, 1.0, None]), max_size=3),
})
STATE = st.fixed_dictionaries({
    "agents": st.lists(AGENT, max_size=4),
    "runs": st.lists(RUN, max_size=4),
    "loose_records": st.integers(0, 2),  # A2A records of nothing the ledger knows
})


def _seed(world, state: dict[str, Any]) -> dict[str, Any]:
    """Write the drawn state; return what the §7 rule predicts per viewer."""
    now = datetime.now(UTC)
    records: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    for i, spec in enumerate(state["agents"]):
        agent_id = _agent(
            f"wb-agent-{i}", world.ids[spec["creator"]], status=spec["status"],
            registry_record_id=f"rec-{i}" if spec["record"] else None,
        )
        if spec["record"]:
            records.append({"recordId": f"rec-{i}", "descriptorType": "A2A",
                            "status": spec["record_status"]})
        for j in range(spec["fresh_sessions"]):
            _add(ChatSession(agent_id=agent_id, session_id=f"s-{i}-{j}", last_at=now))
        for j in range(spec["stale_sessions"]):
            _add(ChatSession(agent_id=agent_id, session_id=f"old-{i}-{j}",
                             last_at=now - timedelta(days=3)))
        agents.append({**spec, "id": agent_id})
    for i in range(state["loose_records"]):
        records.append({"recordId": f"loose-{i}", "descriptorType": "A2A", "status": "APPROVED"})
    runs = []
    for spec in state["runs"]:
        scores = [{"score": s, "evaluator": f"e{k}"} for k, s in enumerate(spec["scores"])]
        run_id = _add(EvalRun(agent_id="a1", agent_name="agent", status=spec["status"],
                              created_by=world.ids[spec["creator"]], scores=scores))
        runs.append({**spec, "id": run_id})
    registry_backend.set_backend(_StubRegistry(records))
    return {"agents": agents, "runs": runs, "records": records}


def _visible(creator_name: str | None, viewer: str, mode: str, world) -> bool:
    """users-and-ownership.md §7."""
    if viewer in ("admin", "carol") or mode == visibility.SHARED:
        return True
    creator = world.ids[creator_name]
    return creator is None or creator == world.ids[viewer] or creator in world.admin_ids


def _expected(seeded: dict[str, Any], viewer: str, mode: str, world) -> dict[str, Any]:
    agents = seeded["agents"]
    visible_agents = [a for a in agents if _visible(a["creator"], viewer, mode, world)]
    hidden_record_ids = {f"rec-{i}" for i, a in enumerate(agents)
                         if a["record"] and a not in visible_agents}
    a2a = [r for r in seeded["records"]
           if r["descriptorType"] == "A2A" and r["status"] != "DEPRECATED"]
    runs = [r for r in seeded["runs"]
            if r["status"] == "completed" and _visible(r["creator"], viewer, mode, world)]
    scores = [s for r in runs for s in r["scores"] if s is not None]
    return {
        "active_sessions": sum(a["fresh_sessions"] for a in visible_agents),
        "registry_agents": len([r for r in a2a if r["recordId"] not in hidden_record_ids]),
        "eval_runs": len(runs),
        "eval_pass_rate": round(sum(scores) / len(scores), 3) if scores else None,
        "listed_agents": {a["id"] for a in visible_agents if a["status"] != "deleted"},
        "listed_runs": {r["id"] for r in seeded["runs"]
                        if _visible(r["creator"], viewer, mode, world)},
    }


VIEWERS = ("bob", "ada", "carol", "admin")


@settings(
    max_examples=40, deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(state=STATE)
def test_overview_counters_follow_the_visibility_rule_and_the_list_endpoints(
    monkeypatch, world, state,
):
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    _wipe(ChatSession, EvalRun, Agent)
    overview_mod._cache.clear()
    seeded = _seed(world, state)
    try:
        for mode in (visibility.SHARED, visibility.ISOLATED):
            _set_mode(mode)
            for viewer in VIEWERS:
                client = getattr(world, viewer)
                overview_mod._cache.clear()  # the A2A cache is per workspace, not per viewer
                body = client.get("/api/overview").json()
                want = _expected(seeded, viewer, mode, world)
                got = {
                    "active_sessions": body["active_sessions"],
                    "registry_agents": body["registry_assets"]["agents"],
                    "eval_runs": body["eval_runs"],
                    "eval_pass_rate": body["eval_pass_rate"],
                }
                assert got == {k: want[k] for k in got}, (viewer, mode, state)
                # AC-E01-02: the evaluation row can never contradict the tile
                assert body["services"]["evaluation"] is (body["eval_runs"] > 0)
                detail = body["service_detail"]["evaluation"]
                assert detail == (
                    None if body["eval_runs"] == 0
                    else {"runs": body["eval_runs"], "pass_rate": body["eval_pass_rate"]}
                )
                assert body["registry_assets"]["total"] == sum(
                    v for k, v in body["registry_assets"].items() if k != "total")
                # differential against the lists the same viewer sees
                listed = {a["id"] for a in client.get("/api/agents").json()["agents"]}
                assert listed == want["listed_agents"], (viewer, mode)
                runs = client.get("/api/eval/runs").json()["runs"]
                assert {r["id"] for r in runs} == want["listed_runs"], (viewer, mode)
                assert body["eval_runs"] == len([r for r in runs if r["status"] == "completed"])
    finally:
        _set_mode(visibility.SHARED)
        registry_backend.set_backend(None)
        overview_mod._cache.clear()


def test_a_member_tile_never_exceeds_the_admin_tile(monkeypatch, world):
    """Monotonicity: every counter a member sees under isolation is ≤ the same counter for
    an administrator, and equal to it under `shared` (§7: admins see everything)."""
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    seeded = _seed(world, {
        "agents": [
            {"creator": "ada", "status": "active", "record": True, "record_status": "APPROVED",
             "fresh_sessions": 2, "stale_sessions": 1},
            {"creator": "bob", "status": "failed", "record": True, "record_status": "APPROVED",
             "fresh_sessions": 1, "stale_sessions": 0},
            {"creator": None, "status": "deploying", "record": False,
             "record_status": "APPROVED", "fresh_sessions": 1, "stale_sessions": 0},
        ],
        "runs": [{"creator": "ada", "status": "completed", "scores": [1.0]},
                 {"creator": "bob", "status": "completed", "scores": [0.0]},
                 {"creator": "bob", "status": "running", "scores": []}],
        "loose_records": 1,
    })
    assert seeded["records"]
    try:
        def tile(client):
            overview_mod._cache.clear()
            body = client.get("/api/overview").json()
            return (body["active_sessions"], body["registry_assets"]["agents"], body["eval_runs"])

        _set_mode(visibility.SHARED)
        assert tile(world.bob) == tile(world.admin) == (4, 3, 2)
        _set_mode(visibility.ISOLATED)
        member, root = tile(world.bob), tile(world.admin)
        assert root == (4, 3, 2)
        assert member == (2, 2, 1)
        assert all(m <= r for m, r in zip(member, root, strict=True))
    finally:
        _set_mode(visibility.SHARED)
        registry_backend.set_backend(None)
        overview_mod._cache.clear()


# ─── capabilities × overview: the feature-gated card ─────────────────────────


def test_policy_card_keys_follow_the_feature_flag(monkeypatch):
    """features.md / NC-E01-08: off-feature builds keep the `policy` keys with
    `false` / `""`; `GET /api/capabilities.features` drops `policy` at the same time, so
    the home page's health row and the sidebar agree."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(["agents", "chat", "evaluation"]))
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        caps = client.get("/api/capabilities").json()["features"]
        body = client.get("/api/overview").json()
    finally:
        get_settings.cache_clear()
    assert "policy" not in caps
    assert body["services"]["policy"] is False
    assert body["service_detail"]["policy"] == ""
    # the full key set the console reads is stable whatever the flags
    assert set(body["services"]) == {
        "gateway", "memory", "registry", "policy", "evaluation", "observability"}
    assert set(body["service_detail"]) == set(body["services"])


def test_an_empty_ledger_reads_as_zero_not_null(monkeypatch, world):
    """AC-E01-04: `eval_pass_rate` is `null` before any run; counters are 0, never null."""
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    _wipe(ChatSession, EvalRun, Agent)
    overview_mod._cache.clear()
    for client in (world.bob, world.admin):
        body = client.get("/api/overview").json()
        assert body["active_sessions"] == 0
        assert body["eval_runs"] == 0 and body["eval_pass_rate"] is None
        assert body["registry_assets"] == {"agents": 0, "tools": 0, "skills": 0, "total": 0}
        assert body["services"]["evaluation"] is False
        assert body["service_detail"]["evaluation"] is None
