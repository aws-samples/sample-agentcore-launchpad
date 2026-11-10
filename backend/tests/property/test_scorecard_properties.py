# ruff: noqa: F811  — pytest fixtures are imported by name from tests.evaluation.test_scorecard
"""E11 — scorecard / compare / rerun properties (round 16, channel A, N16-obs10..).

Hypothesis over the ledger facts `app/evaluation/scorecard.py` ships (version resolution,
benchmark, alignment) and over the three routes under workspace isolation:

* **version resolution** — a differential against a six-line reference model of the
  documented rules (§23 of `evaluation-engine.md`);
* **alignment / cases** — every scenario at most once, every session of both runs placed,
  a side null only when that run lacks the scenario;
* **hidden names nowhere** — random run sets (owners, scopes, statuses, snapshots incl. an
  unrecorded version) and a string search of a member's scorecard / list / by-id / compare
  payloads for the names of the datasets and agents isolation hides; the administrator
  keeps every name;
* **rerun == create** — `POST /runs/{id}/rerun` against `POST /runs` with the equivalent
  body: same status and error code, same payload modulo the volatile fields, the caller as
  the creator; the re-run name suffix is idempotent.

**Contract sources** (every invariant below cites one; nothing is copied from the code):
`docs/cn/interfaces/evaluation-engine.md` §23 ("Version on read", "Benchmark", "Compare",
"Re-run", "Visibility / ownership"), `docs/cn/interfaces/users-and-ownership.md` §7 (isolation
rule), `docs/api.md` rows `POST /api/eval/runs` and `POST /api/eval/runs/{run_id}/rerun`.
Metamorphic checks: renaming a dataset changes a label and nothing else; the order of the
runs on a page never changes a run's version; permuting a run's sessions never changes the
case pairing. Where the console (not the backend) owns a rule — the 再评一次 affordance,
`v2/scorecard.ts::rerunnable` — the property is a DIFFERENTIAL against the backend's
`_rerun_request` through a small node runner (esbuild bundle of the real module).

The composite / regression math lives in the console (`frontend/src/v2/scorecard.ts`):
its properties are `frontend/src/v2/scorecard.property.test.ts` (vitest).

Each HTTP example writes its rows and deletes them again (the autouse table wipe runs once
per TEST, not per example); the example counts scale with ``LAUNCHPAD_PROPERTY_SCALE``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import delete

import app.evaluation.routers as eval_routers
import app.evaluation.service as svc
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation import scorecard
from app.evaluation.models import EvalDataset, EvalRun
from app.models.ledger import Agent, Deployment, SpecSnapshot
from tests import test_visibility as tv
from tests.evaluation.test_datasets_v2 import PERSONA
from tests.evaluation.test_scorecard import (  # noqa: F401 — fixtures by name
    EVALS,
    T0,
    _agent,
    _dataset,
    _get_run,
    _row,
    _run,
    _snapshot,
    queued,
    submitted,
)
from tests.test_visibility import _isolate

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

SCALE = float(os.environ.get("LAUNCHPAD_PROPERTY_SCALE", "1"))


def examples(n: int) -> int:
    return max(5, int(n * SCALE))


HTTP = settings(
    max_examples=examples(10), deadline=None, database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow,
                           HealthCheck.data_too_large],
)
PURE = settings(max_examples=examples(150), deadline=None, database=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture])
# the ledger-backed differential writes and wipes rows per example: fewer, heavier examples
LEDGER = settings(max_examples=examples(60), deadline=None, database=None,
                  suppress_health_check=[HealthCheck.function_scoped_fixture])

TERMINAL = ("completed", "failed", "stopped")
STATUSES = TERMINAL + ("queued", "evaluating")
WINDOW = {"dataset_name": "window:24h"}


def _wipe() -> None:
    """Drop every eval / agent row an example wrote (members and the workspace stay)."""
    db = SessionLocal()
    try:
        for table in (EvalRun, EvalDataset, SpecSnapshot, Deployment, Agent):
            db.execute(delete(table))
        db.commit()
    finally:
        db.close()


def _token(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ─── version resolution: a differential against the documented rules ─────────


@st.composite
def timelines(draw: Any) -> dict[str, Any]:
    """An agent's snapshots (seq 1..n, each deployed or not, deploy end = T0 + h hours),
    its succeeded deployments without a snapshot, and runs created at random hours."""
    n = draw(st.integers(min_value=0, max_value=4))
    snaps = []
    for seq in range(1, n + 1):
        deployed = draw(st.booleans())
        at = draw(st.integers(min_value=0, max_value=48))
        snaps.append({"seq": seq, "deployed": deployed, "at": at})
    # the deployed snapshots must be in seq order of time (a later publish deploys later)
    hours = sorted(s["at"] for s in snaps)
    for s, h in zip(snaps, hours, strict=True):
        s["at"] = h
    ends = draw(st.lists(st.integers(min_value=-48, max_value=48), max_size=3))
    runs = draw(st.lists(st.fixed_dictionaries({
        "created": st.integers(min_value=-72, max_value=72),
        "stamp": st.one_of(st.none(), st.integers(min_value=1, max_value=max(1, n))),
        "scope": st.sampled_from(["dataset", "window", "sessions", "logs"]),
    }), min_size=1, max_size=4))
    return {"snaps": snaps, "ends": ends, "runs": runs}


def _reference(run: dict[str, Any], deployed: list[tuple[int, int]], ends: list[int]
               ) -> dict[str, Any] | None:
    """Contract: evaluation-engine.md §23 "Version on read (`resolve_snapshots`)" — stamped;
    inferred (newest deployed snapshot whose deploy ended by the run's creation, else the
    oldest — null when a deployment succeeded between the run and the oldest snapshot);
    observed for a passive scope; null without an agent or a deployed snapshot. Written from
    the doc, not from the implementation."""
    if not deployed:
        return None
    passive = run["scope"] != "dataset"
    if run["stamp"] is not None:
        return {"seq": run["stamp"], "source": "observed" if passive else "stamped"}
    live = [seq for seq, at in deployed if at <= run["created"]]
    if live:
        seq: int | None = max(live)
    else:
        first_seq, first_at = deployed[0]
        between = any(run["created"] < end < first_at for end in ends)
        seq = None if between else first_seq
    if seq is None:
        return None
    return {"seq": seq, "source": "observed" if passive else "inferred"}


@LEDGER
@given(timelines())
def test_resolve_snapshots_matches_the_documented_rules(data):
    agent = _agent(name=_token("agent"))
    try:
        for s in data["snaps"]:
            _snapshot(agent, s["seq"], deployed=s["deployed"], at=T0 + timedelta(hours=s["at"]),
                      deployment_ended=T0 + timedelta(hours=s["at"]) if s["deployed"] else None)
        for end in data["ends"]:
            tv._add(Deployment(agent_id=agent, status="succeeded",
                               ended_at=T0 + timedelta(hours=end)))
        rows = []
        for r in data["runs"]:
            kw: dict[str, Any] = {"agent_id": agent, "agent_snapshot_seq": r["stamp"],
                                  "created_at": T0 + timedelta(hours=r["created"])}
            if r["scope"] == "dataset":
                kw.update(dataset_id="d1", dataset_name="ds")
            elif r["scope"] == "window":
                kw.update(dataset_name="window:6h")
            elif r["scope"] == "logs":
                kw.update(dataset_name="logs:1", session_ids=["s"])
            else:
                kw.update(session_ids=["s"])
            rows.append(_row(**kw))
        db = SessionLocal()
        try:
            resolved = scorecard.resolve_snapshots(db, rows)
            # metamorphic: a run's version does not depend on the other runs of the page nor
            # on their order (§23: "callers resolve a page at a time (`list_runs`) or one row")
            reversed_page = scorecard.resolve_snapshots(db, list(reversed(rows)))
            alone = {row.id: scorecard.resolve_snapshot(db, row) for row in rows}
        finally:
            db.close()
        assert reversed_page == resolved == alone
        deployed = [(s["seq"], s["at"]) for s in data["snaps"] if s["deployed"]]
        for r, row in zip(data["runs"], rows, strict=True):
            assert resolved[row.id] == _reference(r, deployed, data["ends"]), (r, deployed)
    finally:
        _wipe()


# ─── alignment / cases ───────────────────────────────────────────────────────


scenario_ids = st.lists(st.sampled_from([f"s{i}" for i in range(6)]), min_size=1, max_size=6,
                        unique=True)


@PURE
@given(scenario_ids, scenario_ids, st.permutations(range(6)))
def test_build_cases_places_every_session_once(ids_a, ids_b, order):
    """Contract: §23 "Compare" — `scenario` alignment when both runs carry a map; cases are
    B's scenario order then A-only scenarios; a side is null when that run lacks it."""
    a = _row(dataset_id="d1", dataset_name="ds", session_ids=[f"a-{s}" for s in ids_a],
             scenario_map={f"a-{s}": {"scenario_id": s, "input": f"in {s}"} for s in ids_a})
    b = _row(dataset_id="d1", dataset_name="ds", session_ids=[f"b-{s}" for s in ids_b],
             scenario_map={f"b-{s}": {"scenario_id": s, "input": f"in {s}"} for s in ids_b})
    aligned = scorecard.align(a, b, items_of=lambda run: None)
    assert aligned["alignment"] == "scenario"
    results = {"a": {f"a-{s}": [{"score": 0.5}] for s in ids_a},
               "b": {f"b-{s}": [{"score": 0.9}] for s in ids_b}}
    cases = scorecard.build_cases(a, b, aligned["maps"], results)
    seen = [c["scenario_id"] for c in cases]
    assert len(seen) == len(set(seen)) == len(set(ids_a) | set(ids_b))
    assert seen[:len(ids_b)] == list(ids_b)  # B's order first, then A-only scenarios
    for c in cases:
        assert (c["a"] is None) == (c["scenario_id"] not in ids_a)
        assert (c["b"] is None) == (c["scenario_id"] not in ids_b)
        assert c["input"] == f"in {c['scenario_id']}"
        if c["a"]:
            assert c["a"] == {"session_id": f"a-{c['scenario_id']}", "results": [{"score": 0.5}]}
    # metamorphic: permuting A's session order changes no pairing (the map, not the list,
    # pairs sessions — §23 "Alignment")
    shuffled = sorted(a.session_ids, key=lambda sid: order[int(sid[-1])])
    a2 = _row(dataset_id="d1", dataset_name="ds", session_ids=shuffled, scenario_map=a.scenario_map)
    cases2 = scorecard.build_cases(a2, b, scorecard.align(a2, b, items_of=lambda r: None)["maps"],
                                   results)
    pairs = lambda cs: {(c["scenario_id"], (c["a"] or {}).get("session_id"),  # noqa: E731
                         (c["b"] or {}).get("session_id")) for c in cs}
    assert pairs(cases2) == pairs(cases)


@PURE
@given(st.integers(min_value=0, max_value=5000))
def test_window_hours_round_trips_the_scope_encoding(hours):
    """Contract: api.md `POST /api/eval/runs` — a window scope is recorded as `window:<N>h`;
    §23 "Benchmark" — every passive run is the `live` benchmark."""
    row = _row(dataset_name=f"window:{hours}h")
    assert scorecard.scope_of(row) == "window" and scorecard.window_hours(row) == hours
    assert scorecard.benchmark_of(row)["kind"] == "live"


# ─── hidden names appear nowhere ─────────────────────────────────────────────


@st.composite
def isolated_worlds(draw: Any) -> dict[str, Any]:
    """Datasets and agents owned by ada / bob / admin / nobody; runs that mix them."""
    owners = ["ada", "bob", "root", None]
    datasets = draw(st.lists(st.sampled_from(owners), min_size=1, max_size=3))
    agents = draw(st.lists(st.sampled_from(owners), min_size=1, max_size=3))
    runs = draw(st.lists(st.fixed_dictionaries({
        "creator": st.sampled_from(owners),
        "agent": st.integers(min_value=0, max_value=len(agents) - 1),
        "dataset": st.one_of(st.none(), st.integers(min_value=0, max_value=len(datasets) - 1)),
        "scope": st.sampled_from(["dataset", "window", "sessions", "cloud"]),
        "status": st.sampled_from(STATUSES),
        "mode": st.sampled_from(["evaluators", "evaluators", "insights"]),
        "snapshot": st.sampled_from([None, 1, 2]),
        "with_map": st.booleans(),
    }), min_size=1, max_size=5))
    return {"datasets": datasets, "agents": agents, "runs": runs}


def _build_world(world: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    tokens: dict[str, list[str]] = {"hidden": [], "visible": [], "stored": []}
    datasets, agents = [], []
    for owner in world["datasets"]:
        name = _token("ds")
        datasets.append(_dataset([{"prompt": f"q-{name}"}], name=name, created_by=ids.get(owner)))
        tokens["hidden" if owner == "ada" else "visible"].append(name)
    for owner in world["agents"]:
        name = _token("agent")
        agents.append(_agent(name=name, created_by=ids.get(owner)))
        _snapshot(agents[-1], 1)
        _snapshot(agents[-1], 2)
        tokens["hidden" if owner == "ada" else "visible"].append(name)
    runs = []
    for i, r in enumerate(world["runs"]):
        kw: dict[str, Any] = {"status": r["status"], "mode": r["mode"],
                              "agent_snapshot_seq": r["snapshot"],
                              "created_at": T0 + timedelta(hours=i)}
        agent_name = _token("agentname")  # the stored name at run time, never the live one
        if r["scope"] == "dataset" and r["dataset"] is not None:
            ds_id = datasets[r["dataset"]]
            kw.update(dataset_id=ds_id, dataset_name=_token("dsname"),
                      session_ids=[f"s-{i}"], dataset_fingerprint="f" * 16, dataset_size=1)
            if r["with_map"]:
                kw["scenario_map"] = {f"s-{i}": {"scenario_id": "item_1", "input": "q"}}
            owner = world["datasets"][r["dataset"]]
            tokens["hidden" if owner == "ada" else "visible"].append(kw["dataset_name"])
            tokens["stored"].append(kw["dataset_name"])
        elif r["scope"] == "cloud":
            kw.update(dataset_id="cl-1", dataset_name="cloud:" + _token("cloud"),
                      session_ids=[f"s-{i}"])
        elif r["scope"] == "window":
            kw.update(dataset_name="window:24h")
        else:
            kw.update(session_ids=[f"s-{i}"])
        owner = world["agents"][r["agent"]]
        tokens["hidden" if owner == "ada" else "visible"].append(agent_name)
        tokens["stored"].append(agent_name)
        runs.append(_run(agents[r["agent"]], created_by=ids.get(r["creator"]),
                         agent_name=agent_name, **kw))
    return {"datasets": datasets, "agents": agents, "runs": runs, "tokens": tokens}


@HTTP
@given(isolated_worlds())
def test_hidden_dataset_and_agent_names_appear_nowhere_for_a_member(app, admin, ada, bob,
                                                                      monkeypatch, data):
    """Contract: users-and-ownership.md §7 (a member of an isolated workspace sees a resource
    only when its creator is theirs, NULL or a current admin's; a hidden id answers the kind's
    not-found) + §23 "Visibility / ownership" (the scorecard drops runs of hidden agents, the
    run payload masks hidden names — N15-obs8 / N15-obs15: the name appears NOWHERE)."""
    # a cloud run's pinned items come from the local snapshot store (S3): stub the read
    monkeypatch.setattr(eval_routers, "_cloud_dataset_items",
                        lambda cloud_id, ws, version=None: ("hr", [{"prompt": "cloud q"}]))
    ada_id, _ = ada
    bob_id, bob_client = bob
    root_id, _ = tv._member(app, "root-" + uuid.uuid4().hex[:6], role="admin")
    _isolate()
    ids = {"ada": ada_id, "bob": bob_id, "root": root_id}
    built = _build_world(data, ids)
    try:
        hidden = built["tokens"]["hidden"]
        listed = bob_client.get("/api/eval/runs?limit=200")
        pages = [bob_client.get("/api/eval/scorecard").text, listed.text]
        visible_runs = listed.json()["runs"]
        for run in visible_runs[:2]:
            pages.append(bob_client.get(f"/api/eval/runs/{run['id']}").text)
        terminal = [r["id"] for r in visible_runs
                    if r["status"] in TERMINAL and r["mode"] == "evaluators"]
        for a, b in [(a, b) for a in terminal for b in terminal][:3]:
            res = bob_client.get(f"/api/eval/runs/compare?a={a}&b={b}")
            assert res.status_code == 200, res.text
            pages.append(res.text)
        for page in pages:
            for name in hidden:
                assert name not in page, (name, page[:400])
        # the administrator keeps the stored agent / dataset name of every run
        shown = admin.get("/api/eval/runs?limit=200").text
        for name in built["tokens"]["stored"]:
            assert name in shown, name
    finally:
        _wipe()


@pytest.mark.xfail(strict=True, reason="N16-obs12: once a hidden dataset row is deleted, the "
                                      "run payload shows its stored name to the member "
                                      "(`hidden.dataset` false — the masking needs the row)")
def test_a_deleted_hidden_dataset_keeps_its_stored_name_out_of_a_members_run(ada, bob):
    """The dataset row is gone, the run (NULL creator → visible) still names it: a member
    of an isolated workspace must not learn the name through the run payload."""
    ada_id, _ = ada
    _, bob_client = bob
    agent = _agent()
    dataset = _dataset([{"prompt": "q"}], name="ada-current", created_by=ada_id)
    run = _run(agent, dataset_id=dataset, dataset_name="ada-stored-secret", session_ids=["s1"])
    db = SessionLocal()
    try:
        db.delete(db.get(EvalDataset, dataset))
        db.commit()
    finally:
        db.close()
    _isolate()
    body = bob_client.get(f"/api/eval/runs/{run}")
    assert body.status_code == 200
    assert "ada-stored-secret" not in body.text, body.text
    assert "ada-stored-secret" not in bob_client.get("/api/eval/scorecard").text


def test_a_deleted_hidden_agents_runs_stay_hidden(ada, bob):
    ada_id, _ = ada
    _, bob_client = bob
    gone = _agent(name="ada-gone", created_by=ada_id, status="deleted")
    run = _run(gone, agent_name="ada-gone-at-run", dataset_name="window:1h")
    _isolate()
    # the run (NULL creator) stays visible; its deleted agent is still ada's → masked
    body = bob_client.get(f"/api/eval/runs/{run}")
    assert body.status_code == 200 and "ada-gone" not in body.text, body.text
    assert body.json()["agent_name"] is None and body.json()["hidden"]["agent"] is True
    card = bob_client.get("/api/eval/scorecard")
    assert run not in {r["id"] for r in card.json()["runs"]}
    assert "ada-gone" not in card.text and "ada-gone" not in bob_client.get(
        "/api/eval/runs").text


# ─── rerun == create (differential) ──────────────────────────────────────────


VOLATILE = {"id", "created_at", "updated_at", "queue_position", "name"}

names = st.one_of(st.none(), st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Zs"), exclude_characters="\x00"),
    min_size=1, max_size=64).filter(lambda s: s.strip()))


@st.composite
def rerun_sources(draw: Any) -> dict[str, Any]:
    scope = draw(st.sampled_from(["dataset", "dataset", "window", "persona"]))
    evaluators = draw(st.one_of(
        st.just([]),
        st.lists(st.sampled_from(["Builtin.Correctness", "Builtin.Helpfulness",
                                  "Builtin.GoalSuccessRate", "cust_missing_judge"]),
                 min_size=1, max_size=3, unique=True),
    ))
    return {
        "scope": scope,
        "hours": draw(st.sampled_from([1, 24, 36, 336, 337, 0])),
        "evaluators": evaluators,
        "name": draw(names),
        "description": draw(st.one_of(st.none(), st.text(max_size=40))),
        "snapshots": draw(st.integers(min_value=0, max_value=2)),
        "stamp": draw(st.sampled_from([None, 1])),
        "items": draw(st.integers(min_value=1, max_value=3)),
    }


def _equivalent_body(source: dict[str, Any], src: EvalRun, dataset: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"agent_id": src.agent_id, "evaluators": list(src.evaluators or []),
                            "mode": "evaluators", "description": src.description}
    if src.name:
        body["name"] = src.name
    if source["scope"] == "window":
        body["lookback_hours"] = source["hours"]
    else:
        body["dataset_id"] = dataset
    if source["scope"] == "persona":
        from app.core.config import get_settings

        body["actor_model_id"] = get_settings().eval_actor_model_id
    return body


@HTTP
@given(rerun_sources())
def test_rerun_answers_exactly_as_post_runs_with_the_equivalent_body(client, submitted, data):
    """Contract: api.md `POST /api/eval/runs/{run_id}/rerun` — "started exactly as
    `POST /api/eval/runs` would start it: the same validation and visibility refusals …; the
    caller is the new run's creator"; §23 "Re-run"."""
    agent = _agent(name=_token("agent"))
    try:
        for seq in range(1, data["snapshots"] + 1):
            _snapshot(agent, seq)
        dataset = None
        kw: dict[str, Any] = {}
        if data["scope"] == "window":
            kw["dataset_name"] = f"window:{data['hours']}h"
        else:
            items = ([PERSONA] if data["scope"] == "persona"
                     else [{"prompt": f"q{i}"} for i in range(data["items"])])
            dataset = _dataset(items, name=_token("ds"))
            kw.update(dataset_id=dataset, dataset_name="ds", session_ids=["s1"])
        stamp = data["stamp"] if data["snapshots"] else None
        source_id = _run(agent, name=data["name"], description=data["description"],
                         evaluators=data["evaluators"], agent_snapshot_seq=stamp, **kw)
        src = _get_run(source_id)

        rerun = client.post(f"/api/eval/runs/{source_id}/rerun")
        create = client.post("/api/eval/runs", json=_equivalent_body(data, src, dataset))
        if data["scope"] == "window" and data["hours"] in (0, 337):
            # the stored window can no longer start a run: refused, like POST /runs
            assert rerun.status_code == 422 and create.status_code == 422, (rerun.text,
                                                                           create.text)
            assert rerun.json()["code"] == "run.rerun_unsupported"
            return
        assert rerun.status_code == create.status_code, (rerun.text, create.text)
        if create.status_code != 201:
            assert rerun.json()["code"] == create.json()["code"], (rerun.text, create.text)
            assert submitted == []
            return
        a, b = rerun.json(), create.json()
        assert {k: v for k, v in a.items() if k not in VOLATILE} == {
            k: v for k, v in b.items() if k not in VOLATILE}
        assert a["created_by"] == b["created_by"]  # the caller, never the source's
        expected_name = (src.name or src.agent_name) + " · 再评"
        assert a["name"] == expected_name[:64] if len(expected_name) <= 64 else True
        rows = [_get_run(a["id"]), _get_run(b["id"])]
        for field in ("agent_snapshot_seq", "dataset_fingerprint", "dataset_size", "mode",
                      "evaluators", "dataset_name", "dataset_version", "created_by"):
            assert getattr(rows[0], field) == getattr(rows[1], field), field
        assert [c["dataset_items"] for c in submitted[-2:]] == [
            submitted[-1]["dataset_items"]] * 2
        assert submitted[-2]["actor_model_id"] == submitted[-1]["actor_model_id"]
    finally:
        submitted.clear()
        _wipe()


@settings(max_examples=examples(60), deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=st.characters(categories=("L", "N", "Zs"), max_codepoint=0x4E00),
               min_size=1, max_size=64).filter(lambda s: s.strip()))
@example("x" * 64)
@example("x" * 61)
@example("名" * 60)
@pytest.mark.xfail(strict=True, reason="N16-obs10: a source name of 60-64 chars gets a truncated "
                                      "re-run suffix (` · 再` or none at all): `[:64]` cuts "
                                      "` · 再评` instead of shortening the name")
def test_the_rerun_name_suffix_is_idempotent(client, submitted, name):
    assume(not name.endswith(" · 再评"))
    agent = _agent(name=_token("agent"))
    try:
        source = _run(agent, name=name, dataset_name="window:24h")
        first = client.post(f"/api/eval/runs/{source}/rerun")
        assert first.status_code == 201, first.text
        second = client.post(f"/api/eval/runs/{first.json()['id']}/rerun")
        assert second.status_code == 201, second.text
        # the contract: "a re-run of a re-run keeps one suffix"
        assert second.json()["name"] == first.json()["name"], (name, first.json()["name"],
                                                               second.json()["name"])
        assert first.json()["name"].endswith(" · 再评"), first.json()["name"]
    finally:
        submitted.clear()
        _wipe()


def test_rerun_of_a_rerun_with_a_short_name_keeps_one_suffix(client, submitted):
    """The passing half of N16-obs10: names short enough for the suffix are idempotent."""
    agent = _agent()
    source = _run(agent, name="weekly", dataset_name="window:24h")
    first = client.post(f"/api/eval/runs/{source}/rerun").json()
    second = client.post(f"/api/eval/runs/{first['id']}/rerun").json()
    assert first["name"] == second["name"] == "weekly · 再评"
    assert svc.run_queue is not None


# ─── metamorphic: a dataset rename changes a label and nothing else ─────────


@settings(max_examples=examples(20), deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=st.characters(categories=("L", "N", "Zs")), min_size=1, max_size=64)
       .filter(lambda s: s.strip()))
def test_renaming_a_dataset_changes_only_the_benchmark_label(client, new_name):
    """Contract: §23 "Benchmark" — `label` is the dataset's CURRENT name; the revision is the
    fingerprint of what the run replayed; scores, versions and the stored `dataset_name`
    belong to the run. So a rename may change `benchmark.label` and nothing else."""
    agent = _agent(name=_token("agent"))
    try:
        _snapshot(agent, 1)
        dataset = _dataset([{"prompt": "q"}], name=_token("ds"))
        _run(agent, dataset_id=dataset, dataset_name="stored", agent_snapshot_seq=1,
             dataset_fingerprint="f" * 16, dataset_size=1, session_ids=["s1"],
             scores=[{"evaluatorId": "Builtin.Correctness", "score": 0.7}])
        before = client.get("/api/eval/scorecard").json()
        runs_before = client.get("/api/eval/runs").json()
        res = client.put(f"/api/eval/datasets/{dataset}", json={"name": new_name})
        assert res.status_code == 200, res.text
        after = client.get("/api/eval/scorecard").json()

        def strip(card: dict) -> dict:
            return {**card, "runs": [{**r, "benchmark": {**r["benchmark"], "label": None}}
                                     for r in card["runs"]]}

        assert strip(before) == strip(after)
        assert [r["benchmark"]["label"] for r in after["runs"]] == [new_name]
        assert client.get("/api/eval/runs").json() == runs_before  # the stored name stands
    finally:
        _wipe()


# ─── differential: the console's 再评一次 affordance vs the backend's re-run rule ──

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
_BUNDLE: list[Path] = []


def _scorecard_bundle() -> Path | None:
    """`frontend/src/v2/scorecard.ts` bundled once per session with the repo's esbuild (the
    REAL console rules, not a port); None when node / esbuild are unavailable here."""
    if _BUNDLE:
        return _BUNDLE[0]
    esbuild = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not esbuild.exists() or shutil.which("node") is None:
        return None
    out = Path(tempfile.gettempdir()) / f"r16b-scorecard-{os.getpid()}.mjs"
    subprocess.run([str(esbuild), str(FRONTEND / "src/v2/scorecard.ts"), "--bundle",
                    "--platform=node", "--format=esm", f"--outfile={out}",
                    "--log-level=error"], check=True, cwd=FRONTEND, timeout=120)
    _BUNDLE.append(out)
    return out


def _console_rerunnable(rows: list[dict[str, Any]]) -> list[bool]:
    bundle = _scorecard_bundle()
    if bundle is None:
        pytest.skip("node / esbuild not available: the console differential needs them")
    script = (f"import * as sc from {json.dumps(str(bundle))};\n"
              "const rows = JSON.parse(process.argv[1]);\n"
              "console.log(JSON.stringify(rows.map((r) => sc.rerunnable(r))));")
    out = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(rows)],
                         check=True, capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _backend_rerunnable(source: EvalRun) -> bool:
    """api.md rerun row: a dataset / cloud / window source with evaluators starts a run;
    chosen sessions, log streams, insights and an empty evaluator list are refused."""
    try:
        eval_routers._rerun_request(source, None)
    except AppError:
        return False
    return bool(source.evaluators)


RERUN_GRID = [
    (scope, hours, evaluators)
    for scope in ("dataset", "cloud", "window", "sessions", "logs")
    for hours in (0, 1, 24, 336, 337, 720)
    for evaluators in ([], ["Builtin.Correctness"])
    if scope == "window" or hours == 24
]


@pytest.mark.xfail(strict=True, reason="N16-obs19: the console enables 再评一次 for sources the "
                                      "backend refuses (window:0h, a window over 336 h, an "
                                      "empty evaluator list) — the affordance rule "
                                      "`v2/scorecard.ts::rerunnable` has no contract line and "
                                      "diverges from api.md's re-run rule")
def test_the_console_rerun_affordance_matches_the_backend_rule():
    sources: list[EvalRun] = []
    for scope, hours, evaluators in RERUN_GRID:
        kw: dict[str, Any] = {"agent_id": "a1", "evaluators": evaluators}
        if scope == "dataset":
            kw.update(dataset_id="d1", dataset_name="ds")
        elif scope == "cloud":
            kw.update(dataset_id="cl-1", dataset_name="cloud:hr")
        elif scope == "window":
            kw.update(dataset_name=f"window:{hours}h")
        elif scope == "logs":
            kw.update(dataset_name="logs:2", session_ids=["s1", "s2"])
        else:
            kw.update(session_ids=["s1"])
        sources.append(_row(**kw))
    console = _console_rerunnable([
        {"scope": scorecard.scope_of(src), "dataset_name": src.dataset_name,
         "status": "completed", "evaluators": list(src.evaluators or [])}
        for src in sources
    ])
    disagreements = [
        (scope, hours, evaluators, {"console": ui, "backend": _backend_rerunnable(src)})
        for (scope, hours, evaluators), src, ui in zip(RERUN_GRID, sources, console, strict=True)
        if ui != _backend_rerunnable(src)
    ]
    assert disagreements == [], disagreements
