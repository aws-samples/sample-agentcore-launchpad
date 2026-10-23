"""Agent scorecards: the run stamps (agent version, dataset fingerprint, scenario map),
the version / benchmark / alignment rules, and the scorecard, compare and re-run routes.

AWS is never called: the run queue is stubbed so a submitted run never executes, the
results-stream read (`service.run_results`) and cloud dataset reads are stubbed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

import app.evaluation.routers as eval_routers
import app.evaluation.service as svc
from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import scorecard
from app.evaluation.models import EvalDataset, EvalRun
from app.evaluation.scenarios import INPUT_PREVIEW_CHARS, input_preview, items_fingerprint
from app.models.ledger import Agent, Deployment, SpecSnapshot
from app.services import memory
from tests import test_visibility as tv
from tests.conftest import ws_ctx
from tests.evaluation.test_datasets_v2 import PERSONA, SCENARIO
from tests.test_visibility import _isolate

# the auth-enabled console + members of the workspace visibility suite
app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/sc-abcdefghij"
EVALS = ["Builtin.Correctness", "Builtin.Helpfulness"]
T0 = datetime(2026, 10, 1, 12, 0, 0)


# ── ledger helpers ───────────────────────────────────────────────────────────


def _add(row: Any) -> Any:
    db = SessionLocal()
    try:
        if getattr(row, "workspace_id", None) is None:
            row.workspace_id = DEFAULT_WORKSPACE_ID
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _agent(name: str = "sc-agent", created_by: str | None = None, **kw: Any) -> str:
    return _add(Agent(
        name=name, method=kw.pop("method", "zip_runtime"), status=kw.pop("status", "active"),
        arn=RUNTIME_ARN, resource_id="sc-abcdefghij", spec=kw.pop("spec", {"name": name}),
        created_by=created_by, **kw,
    ))


def _snapshot(agent_id: str, seq: int, *, deployed: bool = True, at: datetime | None = None,
              spec: dict | None = None, deployment_ended: datetime | None = None,
              note: str | None = None) -> None:
    db = SessionLocal()
    try:
        deployment_id = None
        if deployment_ended is not None or not deployed:
            dep = Deployment(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                             status="succeeded" if deployed else "running",
                             ended_at=deployment_ended)
            db.add(dep)
            db.flush()
            deployment_id = dep.id
        db.add(SpecSnapshot(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, seq=seq,
            spec=spec if spec is not None else {"system_prompt": f"v{seq}"},
            aws_version=str(seq) if deployed else None, deployment_id=deployment_id,
            note=note, created_at=at or T0 + timedelta(hours=seq),
        ))
        db.commit()
    finally:
        db.close()


def _dataset(items: list[dict], name: str = "ds", created_by: str | None = None) -> str:
    return _add(EvalDataset(name=name, kind="legacy", items=items, created_by=created_by))


def _run(agent_id: str = "", *, status: str = "completed", created_by: str | None = None,
         **kw: Any) -> str:
    kw.setdefault("agent_name", "sc-agent")
    kw.setdefault("evaluators", list(EVALS))
    kw.setdefault("mode", "evaluators")
    return _add(EvalRun(agent_id=agent_id, status=status, created_by=created_by, **kw))


def _get_run(run_id: str) -> EvalRun:
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()


@pytest.fixture
def queued(monkeypatch):
    """Runs are accepted but never executed (the queued row is the subject)."""
    queue = SimpleNamespace(submit=lambda run_id, fn: 0, position=lambda run_id: 0,
                            cancel=lambda run_id: False)
    monkeypatch.setattr(svc, "run_queue", queue)
    monkeypatch.setattr(eval_routers, "run_queue", queue)
    monkeypatch.setattr(svc, "resolve_telemetry", lambda agent, ws: ("svc.DEFAULT", "/lg"))
    return queue


# ── migration ────────────────────────────────────────────────────────────────


def test_an_old_eval_runs_table_gains_the_four_columns(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    db_module.Base.metadata.create_all(bind=engine)
    new = ("agent_snapshot_seq", "dataset_fingerprint", "dataset_size", "scenario_map")
    with engine.begin() as conn:
        for column in new:
            conn.execute(sa.text(f"ALTER TABLE eval_runs DROP COLUMN {column}"))
    assert set(db_module.schema_drift(engine).get("eval_runs", [])) == set(new)

    db_module._migrate(engine)
    db_module._migrate(engine)  # idempotent

    columns = {c["name"] for c in sa.inspect(engine).get_columns("eval_runs")}
    assert set(new) <= columns
    db_module.assert_no_schema_drift(engine)


# ── stamps ───────────────────────────────────────────────────────────────────


def test_fingerprint_is_stable_over_key_order_and_changes_on_an_edit():
    items = [{"prompt": "a", "expected": "x"}, {"prompt": "b"}]
    reordered_keys = [{"expected": "x", "prompt": "a"}, {"prompt": "b"}]
    edited = [{"prompt": "a", "expected": "x"}, {"prompt": "b!"}]
    fp, size = items_fingerprint(items)
    assert len(fp) == 16 and size == 2
    assert items_fingerprint(reordered_keys) == (fp, 2)
    assert items_fingerprint(edited)[0] != fp
    assert items_fingerprint(list(reversed(items)))[0] != fp  # order of items matters
    # a legacy list and its normalized form are the same scenarios
    normalized = [{"scenario_id": "item_1", "turns": [{"input": "a", "expected_response": "x"}]},
                  {"scenario_id": "item_2", "turns": [{"input": "b"}]}]
    assert items_fingerprint(normalized) == (fp, 2)
    assert items_fingerprint([]) is None


def test_input_preview_collapses_whitespace_and_caps_the_length():
    assert input_preview({"turns": [{"input": "  a\n\n b  "}, {"input": "second"}]}) == "a b"
    assert input_preview(PERSONA) == PERSONA["input"]
    long = input_preview({"turns": [{"input": "x" * 500}]})
    assert len(long) == INPUT_PREVIEW_CHARS and long.endswith("…")
    assert input_preview({"turns": [{"input": {"content": "dict input"}}]}) == "dict input"


def test_a_run_records_the_newest_deployed_snapshot_not_one_still_deploying(client, queued):
    agent = _agent()
    _snapshot(agent, 1)
    _snapshot(agent, 2)
    _snapshot(agent, 3, deployed=False)  # its deploy is still running
    dataset = _dataset([{"prompt": "q1"}, {"prompt": "q2"}])
    res = client.post("/api/eval/runs", json={
        "agent_id": agent, "dataset_id": dataset, "evaluators": EVALS, "wait_seconds": 0})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["snapshot"] == {"seq": 2, "source": "stamped"}
    row = _get_run(body["id"])
    assert row.agent_snapshot_seq == 2
    assert (row.dataset_fingerprint, row.dataset_size) == (
        items_fingerprint([{"prompt": "q1"}, {"prompt": "q2"}]))
    # by-id and list carry the same version
    assert client.get(f"/api/eval/runs/{body['id']}").json()["snapshot"]["seq"] == 2
    listed = client.get("/api/eval/runs").json()["runs"]
    assert [r["snapshot"] for r in listed] == [{"seq": 2, "source": "stamped"}]


def test_two_runs_of_one_dataset_share_a_fingerprint_until_an_item_changes(client, queued):
    agent = _agent()
    dataset = _dataset([{"prompt": "q1"}, {"prompt": "q2"}])
    payload = {"agent_id": agent, "dataset_id": dataset, "evaluators": EVALS,
               "wait_seconds": 0}
    first = client.post("/api/eval/runs", json=payload).json()
    second = client.post("/api/eval/runs", json=payload).json()
    db = SessionLocal()
    try:
        db.get(EvalDataset, dataset).items = [{"prompt": "q1"}, {"prompt": "q2 edited"}]
        db.commit()
    finally:
        db.close()
    third = client.post("/api/eval/runs", json=payload).json()
    prints = [_get_run(r["id"]).dataset_fingerprint for r in (first, second, third)]
    assert prints[0] == prints[1] != prints[2]


def test_no_snapshot_and_passive_scopes_record_no_version_or_fingerprint(client, queued):
    agent = _agent()  # no snapshot at all
    res = client.post("/api/eval/runs", json={
        "agent_id": agent, "lookback_hours": 6, "evaluators": EVALS})
    assert res.status_code == 201, res.text
    row = _get_run(res.json()["id"])
    assert row.agent_snapshot_seq is None and res.json()["snapshot"] is None
    assert row.dataset_fingerprint is None and row.dataset_size is None
    assert row.scenario_map is None


def test_a_log_source_run_records_no_version(queued):
    run = svc.submit_run(
        agent=None, workspace=ws_ctx(), dataset_items=[], dataset_id=None, dataset_name=None,
        evaluators=EVALS, session_ids=["s-1"],
        log_source={"service_name": "ext.DEFAULT", "log_group_names": ["/ext"]},
    )
    assert run.agent_snapshot_seq is None and run.dataset_fingerprint is None
    db = SessionLocal()
    try:
        assert scorecard.resolve_snapshot(db, run) is None
    finally:
        db.close()


# ── scenario map (execute_run) ───────────────────────────────────────────────


def test_a_skipped_persona_has_no_map_entry_and_previews_are_capped(monkeypatch):
    from tests.evaluation.test_simulation import _execute

    long_input = "word " * 100
    items = [{**PERSONA, "scenario_id": "p1", "input": long_input},
             {**PERSONA, "scenario_id": "p2"},
             {**PERSONA, "scenario_id": "p3"}]
    sessions = {"p1": ["sess-p1-" + "x" * 30], "p2": [RuntimeError("actor model refused")],
                "p3": ["sess-p3-" + "x" * 30]}
    row, _attempts, _batch = _execute(monkeypatch, sessions, items)
    assert row.session_ids == ["sess-p1-" + "x" * 30, "sess-p3-" + "x" * 30]
    assert set(row.scenario_map) == set(row.session_ids)  # p2 was skipped: no entry
    assert row.scenario_map["sess-p3-" + "x" * 30] == {
        "scenario_id": "p3", "input": PERSONA["input"],
        "actor_id": memory.eval_actor(None, row.id, 2)}
    p1 = row.scenario_map["sess-p1-" + "x" * 30]
    assert p1["scenario_id"] == "p1" and len(p1["input"]) <= INPUT_PREVIEW_CHARS


def test_a_budget_stopped_scenario_keeps_its_map_entry(monkeypatch):
    from tests.evaluation.test_eval_transient_retry import _execute, ok
    from tests.test_harness_execution import Stream, stop, text

    stopped = Stream([text("p"), stop("max_tokens")])
    row, sessions, _finished = _execute(monkeypatch, [ok(), stopped])
    assert row.budget_stops and row.budget_stops[0]["session_id"] == sessions[1]
    assert row.scenario_map == {
        sessions[0]: {"scenario_id": "first", "input": "first",
                      "actor_id": memory.eval_actor(None, row.id, 0)},
        sessions[1]: {"scenario_id": "second", "input": "second",
                      "actor_id": memory.eval_actor(None, row.id, 1)},
    }


def test_a_replay_re_reads_the_version_when_it_starts(monkeypatch):
    """A deploy that finishes while the run waits in the queue is what the replay
    invokes: the stamp is re-read when the replay starts."""
    from unittest.mock import MagicMock

    from tests.evaluation.test_eval_transient_retry import ok
    from tests.test_harness_execution import ARN

    agent = _agent()
    _snapshot(agent, 1)
    run_id = _run(agent, status="queued", agent_snapshot_seq=1)
    _snapshot(agent, 2)  # deployed while the run was queued
    _snapshot(agent, 3, deployed=False)  # still deploying: not what the replay invokes
    client = MagicMock()
    client.invoke_harness.side_effect = [{"stream": ok()}]
    client.start_batch_evaluation.return_value = {"batchEvaluationId": "batch-1"}
    monkeypatch.setattr(svc, "data_client", lambda _ws: client)
    monkeypatch.setattr(svc, "_wait_for_fresh_telemetry", lambda **_kw: None)
    monkeypatch.setattr(svc, "_poll_batch", lambda *_a, **_kw: {})
    monkeypatch.setattr(svc, "_finish_from_result", lambda *_a, **_kw: None)
    svc.execute_run(
        run_id, workspace=ws_ctx(), agent_arn=ARN, method="harness",
        service_name="harness_test.DEFAULT", log_group="/test",
        items=[{"scenario_id": "only", "turns": [{"input": "q"}]}],
        evaluators=["Builtin.Correctness"], mode="evaluators", wait_seconds=0,
        agent_id=agent,
    )
    assert _get_run(run_id).agent_snapshot_seq == 2


# ── version resolution / benchmark / alignment (unit) ────────────────────────


def _row(**kw: Any) -> EvalRun:
    kw.setdefault("agent_id", "")
    kw.setdefault("agent_name", "x")
    kw.setdefault("mode", "evaluators")
    kw.setdefault("status", "completed")
    kw.setdefault("created_at", T0 + timedelta(days=1))
    return EvalRun(id=kw.pop("id", None) or uuid.uuid4().hex[:12], **kw)


def test_resolve_snapshots_rules():
    agent = _agent()
    # #1 baseline backfilled AFTER the old runs (no deployment) at T0+1h,
    # #2 deployed (deployment ended T0+5h), #3 never deployed
    _snapshot(agent, 1, at=T0 + timedelta(hours=1), note="baseline")
    _snapshot(agent, 2, at=T0 + timedelta(hours=2), deployment_ended=T0 + timedelta(hours=5))
    _snapshot(agent, 3, deployed=False, at=T0 + timedelta(hours=6))
    bare = _agent(name="bare")
    runs = {
        "stamped": _row(agent_id=agent, dataset_id="d1", dataset_name="ds",
                        agent_snapshot_seq=1, created_at=T0 + timedelta(hours=9)),
        "inferred": _row(agent_id=agent, dataset_id="d1", dataset_name="ds",
                         created_at=T0 + timedelta(hours=6)),
        # after #2 was created but before its deploy finished → still #1
        "mid_deploy": _row(agent_id=agent, dataset_id="d1", dataset_name="ds",
                           created_at=T0 + timedelta(hours=3)),
        "pre_baseline": _row(agent_id=agent, dataset_id="d1", dataset_name="ds",
                             created_at=T0 - timedelta(days=30)),
        "window": _row(agent_id=agent, dataset_name="window:24h",
                       created_at=T0 + timedelta(hours=6)),
        "window_stamped": _row(agent_id=agent, dataset_name="window:24h",
                               agent_snapshot_seq=1, created_at=T0 + timedelta(hours=9)),
        "sessions": _row(agent_id=agent, session_ids=["s"], created_at=T0 + timedelta(hours=6)),
        "logs_source": _row(agent_id="", dataset_name="window:1h",
                            log_source={"service_name": "x", "log_group_names": ["/x"]}),
        "no_snapshot": _row(agent_id=bare, dataset_id="d1", dataset_name="ds"),
    }
    db = SessionLocal()
    try:
        resolved = scorecard.resolve_snapshots(db, list(runs.values()))
    finally:
        db.close()
    got = {name: resolved[run.id] for name, run in runs.items()}
    assert got["stamped"] == {"seq": 1, "source": "stamped"}
    assert got["inferred"] == {"seq": 2, "source": "inferred"}
    assert got["mid_deploy"] == {"seq": 1, "source": "inferred"}
    assert got["pre_baseline"] == {"seq": 1, "source": "inferred"}
    assert got["window"] == {"seq": 2, "source": "observed"}
    assert got["window_stamped"] == {"seq": 1, "source": "observed"}
    assert got["sessions"] == {"seq": 2, "source": "observed"}
    assert got["logs_source"] is None and got["no_snapshot"] is None


def test_a_run_before_every_snapshot_is_unknown_once_the_agent_was_redeployed():
    # the architect's loop before snapshots existed: a deploy, a run, an accepted
    # recommendation re-deploys, a run, again — and the baseline backfilled days later
    agent = _agent(name="pre-snapshot")
    for minutes in (0, 30, 60):
        _add(Deployment(agent_id=agent, status="succeeded",
                        ended_at=T0 + timedelta(minutes=minutes)))
    # a failed deploy changes nothing the agent runs
    _add(Deployment(agent_id=agent, status="failed", ended_at=T0 + timedelta(minutes=90)))
    _snapshot(agent, 1, at=T0 + timedelta(days=5), note="baseline")
    ds = {"dataset_id": "d1", "dataset_name": "ds"}
    runs = {
        "v1": _row(agent_id=agent, created_at=T0 + timedelta(minutes=10), **ds),
        "v2": _row(agent_id=agent, created_at=T0 + timedelta(minutes=40), **ds),
        # nothing succeeded after it until the baseline: #1 is what it evaluated
        "v3": _row(agent_id=agent, created_at=T0 + timedelta(minutes=70), **ds),
        "window_v1": _row(agent_id=agent, dataset_name="window:24h",
                          created_at=T0 + timedelta(minutes=10)),
        "stamped": _row(agent_id=agent, agent_snapshot_seq=1,
                        created_at=T0 + timedelta(minutes=10), **ds),
        "after": _row(agent_id=agent, created_at=T0 + timedelta(days=6), **ds),
    }
    db = SessionLocal()
    try:
        resolved = scorecard.resolve_snapshots(db, list(runs.values()))
    finally:
        db.close()
    got = {name: resolved[run.id] for name, run in runs.items()}
    assert got["v1"] is None and got["v2"] is None and got["window_v1"] is None
    assert got["v3"] == {"seq": 1, "source": "inferred"}
    assert got["stamped"] == {"seq": 1, "source": "stamped"}
    assert got["after"] == {"seq": 1, "source": "inferred"}


def test_benchmark_of_each_scope():
    local = _row(dataset_id="d1", dataset_name="old name", dataset_fingerprint="f" * 16,
                 dataset_size=3)
    assert scorecard.benchmark_of(local, {"d1": "new name"}) == {
        "kind": "dataset", "key": "ds:d1", "label": "new name", "dataset_id": "d1",
        "revision": "f" * 16, "size": 3}
    assert scorecard.benchmark_of(local)["label"] == "old name"  # the row is gone
    pinned = _row(dataset_id="cl-1", dataset_name="cloud:hr", dataset_version="2",
                  dataset_fingerprint="a" * 16)
    assert scorecard.benchmark_of(pinned)["key"] == "cloud:cl-1"
    assert scorecard.benchmark_of(pinned)["label"] == "hr"
    assert scorecard.benchmark_of(pinned)["revision"] == "v2"
    draft = _row(dataset_id="cl-1", dataset_name="cloud:hr", dataset_fingerprint="a" * 16)
    assert scorecard.benchmark_of(draft)["revision"] == "draft:" + "a" * 16
    legacy_draft = _row(dataset_id="cl-1", dataset_name="cloud:hr")
    assert scorecard.benchmark_of(legacy_draft)["revision"] is None
    for passive in (_row(dataset_name="window:24h"), _row(session_ids=["s"]),
                    _row(dataset_name="logs:3", session_ids=["s"])):
        bench = scorecard.benchmark_of(passive)
        assert (bench["kind"], bench["key"], bench["revision"]) == ("live", "live", None)
    assert [scorecard.scope_of(r) for r in (local, pinned, _row(dataset_name="window:2h"),
                                            _row(), _row(dataset_name="logs:2"))] == [
        "dataset", "cloud", "window", "sessions", "logs"]


def _items_of(items: list[dict] | None):
    calls: list[str] = []

    def read(run: EvalRun):
        calls.append(run.id)
        return items

    return read, calls


def test_align_by_scenario_when_both_runs_carry_a_map():
    a = _row(dataset_id="d1", dataset_name="ds", session_ids=["a1", "a2"],
             scenario_map={"a1": {"scenario_id": "s1", "input": "one"},
                           "a2": {"scenario_id": "s2", "input": "two"}})
    b = _row(dataset_id="d1", dataset_name="ds", session_ids=["b2", "b3"],
             scenario_map={"b2": {"scenario_id": "s2", "input": "two"},
                           "b3": {"scenario_id": "s3", "input": "three"}})
    read, calls = _items_of(None)
    aligned = scorecard.align(a, b, items_of=read)
    assert aligned["alignment"] == "scenario" and calls == []
    cases = scorecard.build_cases(a, b, aligned["maps"], {
        "a": {"a2": [{"score": 0.5}]}, "b": {"b2": [{"score": 0.9}]}})
    assert [(c["scenario_id"], bool(c["a"]), bool(c["b"])) for c in cases] == [
        ("s2", True, True), ("s3", False, True), ("s1", True, False)]
    assert cases[0]["a"] == {"session_id": "a2", "results": [{"score": 0.5}]}
    assert cases[0]["b"] == {"session_id": "b2", "results": [{"score": 0.9}]}
    assert cases[1]["b"]["results"] == []


def test_align_by_order_only_for_a_legacy_run_that_skipped_nothing():
    items = [{"prompt": "one"}, {"prompt": "two"}]
    legacy = _row(id="legacy", dataset_id="d1", dataset_name="ds", session_ids=["l1", "l2"])
    stamped = _row(dataset_id="d1", dataset_name="ds", session_ids=["n1"],
                   scenario_map={"n1": {"scenario_id": "item_2", "input": "two"}})
    read, calls = _items_of(items)
    aligned = scorecard.align(legacy, stamped, items_of=read)
    assert aligned["alignment"] == "order" and calls == ["legacy"]
    assert aligned["maps"]["a"] == {"l1": {"scenario_id": "item_1", "input": "one"},
                                    "l2": {"scenario_id": "item_2", "input": "two"}}

    skipped = _row(dataset_id="d1", dataset_name="ds", session_ids=["l1", "l2"],
                   scenario_failures=[{"scenario_id": "item_3"}])
    short = _row(dataset_id="d1", dataset_name="ds", session_ids=["l1"])
    for run, reason in ((skipped, "scenarios_skipped"), (short, "session_count_mismatch")):
        assert scorecard.align(run, stamped, items_of=read) == {
            "alignment": "none", "reason": reason, "maps": None}
    unreadable, _ = _items_of(None)
    assert scorecard.align(legacy, stamped, items_of=unreadable)["reason"] == (
        "dataset_unavailable")


def test_align_none_for_live_traffic_and_different_benchmarks():
    read, calls = _items_of([{"prompt": "x"}])
    window = _row(dataset_name="window:24h")
    ds1 = _row(dataset_id="d1", dataset_name="ds", scenario_map={"s": {"scenario_id": "x"}})
    ds2 = _row(dataset_id="d2", dataset_name="ds", scenario_map={"s": {"scenario_id": "x"}})
    assert scorecard.align(window, window, items_of=read)["reason"] == "live_traffic"
    assert scorecard.align(ds1, window, items_of=read)["reason"] == "live_traffic"
    assert scorecard.align(ds1, ds2, items_of=read)["reason"] == "different_benchmark"
    assert calls == []


# ── GET /api/eval/scorecard ──────────────────────────────────────────────────


def test_scorecard_groups_runs_and_excludes_insights(client):
    agent = _agent(spec={"name": "sc-agent"})
    _snapshot(agent, 1, spec={"system_prompt": "a", "model_id": "m1"})
    _snapshot(agent, 2, deployed=False, spec={"system_prompt": "b", "model_id": "m1"})
    _snapshot(agent, 3, spec={"system_prompt": "b", "model_id": "m2"})
    dataset = _dataset([{"prompt": "q"}], name="golden")
    ds_run = _run(agent, dataset_id=dataset, dataset_name="golden", agent_snapshot_seq=1,
                  dataset_fingerprint="f" * 16, dataset_size=1, session_ids=["s1"],
                  scores=[{"evaluatorId": "Builtin.Correctness", "score": 0.8}],
                  created_at=T0)
    window_run = _run(agent, dataset_name="window:24h", created_at=T0 + timedelta(days=1))
    sessions_run = _run(agent, session_ids=["x"], created_at=T0 + timedelta(days=2))
    insights = _run(agent, mode="insights", dataset_name="window:24h",
                    created_at=T0 + timedelta(days=3))
    log_run = _run("", agent_name="ext.DEFAULT", dataset_name="window:1h",
                   log_source={"service_name": "ext.DEFAULT", "log_group_names": ["/ext"]},
                   created_at=T0 + timedelta(days=4))

    body = client.get("/api/eval/scorecard").json()
    runs = {r["id"]: r for r in body["runs"]}
    assert insights not in runs
    assert list(runs) == [ds_run, window_run, sessions_run, log_run]  # oldest first
    assert body["truncated"] is False
    assert runs[ds_run]["benchmark"] == {
        "kind": "dataset", "key": f"ds:{dataset}", "label": "golden", "dataset_id": dataset,
        "revision": "f" * 16, "size": 1}
    assert runs[ds_run]["snapshot"] == {"seq": 1, "source": "stamped"}
    assert runs[ds_run]["sessions"] == 1 and runs[ds_run]["scope"] == "dataset"
    assert runs[window_run]["benchmark"]["key"] == runs[sessions_run]["benchmark"]["key"] == (
        "live")
    assert runs[window_run]["snapshot"]["source"] == "observed"
    assert runs[log_run]["agent_key"] == "logs:ext.DEFAULT" and runs[log_run]["snapshot"] is None

    agents = {a["key"]: a for a in body["agents"]}
    assert list(agents) == ["logs:ext.DEFAULT", agent]  # newest evaluation first
    assert agents["logs:ext.DEFAULT"]["log_source"]["service_name"] == "ext.DEFAULT"
    assert agents[agent]["current_seq"] == 3
    assert [v["seq"] for v in agents[agent]["versions"]] == [1, 2, 3]
    assert all("changes" not in v for v in agents[agent]["versions"])

    one = client.get(f"/api/eval/scorecard?agent_id={agent}").json()
    assert {r["id"] for r in one["runs"]} == {ds_run, window_run, sessions_run}
    [entry] = one["agents"]
    # each version vs the newest deployed one before it (#2 never deployed)
    assert [(v["seq"], v["changes"]) for v in entry["versions"]] == [
        (1, []), (2, ["prompt"]), (3, ["prompt", "model"])]


def test_scorecard_caps_to_the_newest_runs(client, monkeypatch):
    agent = _agent()
    ids = [_run(agent, dataset_name="window:1h", created_at=T0 + timedelta(hours=i))
           for i in range(4)]
    monkeypatch.setattr(scorecard, "SCORECARD_RUN_CAP", 3)
    body = client.get("/api/eval/scorecard").json()
    assert body["truncated"] is True
    assert [r["id"] for r in body["runs"]] == ids[1:]


def test_scorecard_keeps_a_deleted_agents_runs_and_an_unrun_agents_timeline(client):
    gone = _agent(name="gone", status="deleted")
    _snapshot(gone, 1)
    run = _run(gone, agent_name="gone-at-run", dataset_name="window:1h")
    fresh = _agent(name="fresh")
    _snapshot(fresh, 1)

    agents = {a["key"]: a for a in client.get("/api/eval/scorecard").json()["agents"]}
    assert set(agents) == {gone}  # an agent with no runs is not listed without agent_id
    assert agents[gone]["deleted"] is True and agents[gone]["versions"] == []
    assert agents[gone]["name"] == "gone-at-run"
    assert client.get("/api/eval/scorecard").json()["runs"][0]["id"] == run

    one = client.get(f"/api/eval/scorecard?agent_id={fresh}").json()
    assert one["runs"] == []
    assert [(a["key"], a["current_seq"]) for a in one["agents"]] == [(fresh, 1)]


def test_scorecard_visibility_matrix(app, admin, ada, bob):
    ada_id, _ = ada
    bob_id, bob_client = bob
    root_id, _ = tv._member(app, "root", role="admin")
    shared_agent = _agent(name="shared-agent")  # NULL creator: visible to all
    own = _run(shared_agent, created_by=bob_id, dataset_name="window:1h")
    peer = _run(shared_agent, created_by=ada_id, dataset_name="window:1h")
    by_admin = _run(shared_agent, created_by=root_id, dataset_name="window:1h")
    unknown = _run(shared_agent, created_by=None, dataset_name="window:1h")
    # a visible (NULL-creator) run of an agent bob cannot see is dropped too
    ada_agent = _agent(name="ada-agent", created_by=ada_id)
    on_hidden_agent = _run(ada_agent, created_by=None, dataset_name="window:1h")

    def seen(client, query: str = "") -> set[str]:
        body = client.get(f"/api/eval/scorecard{query}").json()
        return {r["id"] for r in body["runs"]}

    everything = {own, peer, by_admin, unknown, on_hidden_agent}
    assert seen(bob_client) == everything  # shared (the default)
    _isolate()
    assert seen(bob_client) == {own, by_admin, unknown}
    assert seen(admin) == everything
    keys = {a["key"] for a in bob_client.get("/api/eval/scorecard").json()["agents"]}
    assert keys == {shared_agent}
    assert seen(bob_client, f"?agent_id={ada_agent}") == set()
    assert bob_client.get(f"/api/eval/scorecard?agent_id={ada_agent}").json()["agents"] == []


# ── GET /api/eval/runs/compare ───────────────────────────────────────────────


@pytest.fixture
def results(monkeypatch):
    """Stub the results-stream read: ``answers[run_id]`` is the run_results payload."""
    answers: dict[str, dict] = {}
    calls: list[str] = []

    def run_results(run, *, workspace):
        calls.append(run.id)
        return answers.get(run.id) or {"available": False, "reason": "no_batch",
                                       "sessions": []}

    monkeypatch.setattr(svc, "run_results", run_results)
    return SimpleNamespace(answers=answers, calls=calls)


def _judged(sessions: dict[str, float]) -> dict:
    return {"available": True, "sessions": [
        {"session_id": sid, "results": [{"evaluator_id": "Builtin.Correctness",
                                         "score": score}]}
        for sid, score in sessions.items()]}


def test_compare_two_stamped_replays_aligns_by_scenario(client, results):
    agent = _agent()
    _snapshot(agent, 1)
    _snapshot(agent, 2, note="prompt tweak")
    _snapshot(agent, 3)
    dataset = _dataset([{"prompt": "one"}, {"prompt": "two"}])
    common = {"dataset_id": dataset, "dataset_name": "ds", "dataset_fingerprint": "f" * 16,
              "dataset_size": 2}
    a = _run(agent, agent_snapshot_seq=1, session_ids=["a1", "a2"], created_at=T0, **common,
             scenario_map={"a1": {"scenario_id": "item_1", "input": "one"},
                           "a2": {"scenario_id": "item_2", "input": "two"}})
    b = _run(agent, agent_snapshot_seq=3, session_ids=["b1", "b2"],
             created_at=T0 + timedelta(days=1), **common,
             scenario_map={"b1": {"scenario_id": "item_1", "input": "one"},
                           "b2": {"scenario_id": "item_2", "input": "two"}})
    results.answers[a] = _judged({"a1": 0.9, "a2": 0.4})
    results.answers[b] = _judged({"b1": 0.5, "b2": 0.95})

    res = client.get(f"/api/eval/runs/compare?a={a}&b={b}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["alignment"] == "scenario" and body["alignment_reason"] is None
    assert body["results"] == {"a": {"available": True}, "b": {"available": True}}
    assert [(c["scenario_id"], c["a"]["session_id"], c["b"]["session_id"])
            for c in body["cases"]] == [("item_1", "a1", "b1"), ("item_2", "a2", "b2")]
    assert body["cases"][0]["a"]["results"][0]["score"] == 0.9
    assert body["runs"]["a"]["snapshot"] == {"seq": 1, "source": "stamped"}
    assert body["runs"]["b"]["benchmark"]["key"] == f"ds:{dataset}"
    config = body["config"]
    assert (config["agent_id"], config["from_seq"], config["to_seq"]) == (agent, 1, 3)
    assert [(s["seq"], s["note"]) for s in config["between"]] == [(2, "prompt tweak"), (3, None)]
    assert sorted(results.calls) == sorted([a, b])

    same = client.get(f"/api/eval/runs/compare?a={b}&b={b}").json()
    assert same["config"] == {"same": True, "agent_id": agent, "seq": 3}


def test_compare_a_legacy_pair_aligns_by_order_and_degrades_per_side(client, results):
    agent = _agent()  # no snapshots: versions unknown
    dataset = _dataset([{"prompt": "one"}, {"prompt": "two"}])
    a = _run(agent, dataset_id=dataset, dataset_name="ds", session_ids=["a1", "a2"],
             created_at=T0)
    b = _run(agent, dataset_id=dataset, dataset_name="ds", session_ids=["b1", "b2"],
             created_at=T0 + timedelta(days=1))
    results.answers[a] = {"available": False, "reason": "unreadable", "detail": "boom",
                          "sessions": []}
    results.answers[b] = {**_judged({"b1": 0.5}), "truncated": True}

    body = client.get(f"/api/eval/runs/compare?a={a}&b={b}").json()
    assert body["alignment"] == "order"
    assert body["results"]["a"] == {"available": False, "reason": "unreadable",
                                    "detail": "boom"}
    assert body["results"]["b"] == {"available": True, "truncated": True}
    assert [(c["scenario_id"], c["input"]) for c in body["cases"]] == [
        ("item_1", "one"), ("item_2", "two")]
    assert body["cases"][0]["a"] == {"session_id": "a1", "results": []}
    assert body["cases"][0]["b"]["results"][0]["score"] == 0.5
    assert body["config"] is None


def test_compare_window_runs_reads_no_results(client, results):
    agent = _agent()
    a = _run(agent, dataset_name="window:24h")
    b = _run(agent, dataset_name="window:24h")
    body = client.get(f"/api/eval/runs/compare?a={a}&b={b}").json()
    assert (body["alignment"], body["alignment_reason"]) == ("none", "live_traffic")
    assert body["cases"] == [] and body["results"] is None
    assert results.calls == []


def test_compare_a_legacy_cloud_run_reads_its_pinned_version(client, results, monkeypatch):
    agent = _agent()
    read: list[tuple[str, str | None]] = []

    def cloud_items(cloud_id, ws, version=None):
        read.append((cloud_id, version))
        return "hr", [SCENARIO]

    monkeypatch.setattr(eval_routers, "_cloud_dataset_items", cloud_items)
    a = _run(agent, dataset_id="cl-1", dataset_name="cloud:hr", dataset_version="2",
             session_ids=["a1"])
    b = _run(agent, dataset_id="cl-1", dataset_name="cloud:hr", dataset_version="3",
             session_ids=["b1"], scenario_map={"b1": {"scenario_id": "refund_flow",
                                                      "input": "I want a refund"}})
    body = client.get(f"/api/eval/runs/compare?a={a}&b={b}").json()
    assert body["alignment"] == "order" and read == [("cl-1", "2")]
    assert [c["scenario_id"] for c in body["cases"]] == ["refund_flow"]
    assert body["runs"]["a"]["benchmark"]["revision"] == "v2"
    assert body["runs"]["b"]["benchmark"]["revision"] == "v3"


def test_compare_refuses_active_and_insights_runs(client, results):
    agent = _agent()
    done = _run(agent, dataset_name="window:1h")
    active = _run(agent, status="evaluating", dataset_name="window:1h")
    insights = _run(agent, mode="insights", dataset_name="window:1h")
    for other in (active, insights):
        res = client.get(f"/api/eval/runs/compare?a={done}&b={other}")
        assert res.status_code == 422, res.text
        assert res.json()["code"] == "run.compare_unsupported"
    assert client.get(f"/api/eval/runs/compare?a={done}").status_code == 422


def test_compare_is_not_read_as_a_run_id(client, results):
    # `/runs/compare` is declared before `/runs/{run_id}`: a missing query parameter is
    # the compare route's validation error, never a lookup of run "compare"
    res = client.get("/api/eval/runs/compare")
    assert res.status_code == 422, res.text
    assert res.json().get("code") != "run.not_found"


def test_a_hidden_dataset_shows_neither_its_current_name_nor_its_items(
        app, admin, ada, bob, results):
    ada_id, _ = ada
    _, bob_client = bob
    root_id, _ = tv._member(app, "root", role="admin")
    agent = _agent()  # NULL creator: visible to every member
    dataset = _dataset([{"prompt": "secret one"}, {"prompt": "secret two"}],
                       name="ada renamed", created_by=ada_id)
    # admin-made legacy replays (no scenario map), stored under the name at run time
    common = {"created_by": root_id, "dataset_id": dataset, "dataset_name": "ada original"}
    a = _run(agent, session_ids=["a1", "a2"], created_at=T0, **common)
    b = _run(agent, session_ids=["b1", "b2"], created_at=T0 + timedelta(days=1), **common)
    _isolate()
    url = f"/api/eval/runs/compare?a={a}&b={b}"

    labels = {r["benchmark"]["label"] for r in bob_client.get("/api/eval/scorecard").json()["runs"]}
    assert labels == {"ada original"}
    hidden = bob_client.get(url).json()
    assert (hidden["alignment"], hidden["alignment_reason"]) == ("none", "dataset_unavailable")
    assert hidden["cases"] == [] and results.calls == []
    assert hidden["runs"]["a"]["benchmark"]["label"] == "ada original"

    labels = {r["benchmark"]["label"] for r in admin.get("/api/eval/scorecard").json()["runs"]}
    assert labels == {"ada renamed"}
    shown = admin.get(url).json()
    assert shown["alignment"] == "order"
    assert [c["input"] for c in shown["cases"]] == ["secret one", "secret two"]


def test_compare_a_hidden_run_is_404(ada, bob, results):
    ada_id, _ = ada
    bob_id, bob_client = bob
    agent = _agent()
    mine = _run(agent, created_by=bob_id, dataset_name="window:1h")
    theirs = _run(agent, created_by=ada_id, dataset_name="window:1h")
    _isolate()
    res = bob_client.get(f"/api/eval/runs/compare?a={mine}&b={theirs}")
    assert res.status_code == 404 and res.json()["code"] == "run.not_found"
    assert bob_client.get(f"/api/eval/runs/compare?a={mine}&b={mine}").status_code == 200


def test_compare_hides_a_hidden_agents_publish_history(admin, ada, bob, results):
    ada_id, _ = ada
    _, bob_client = bob
    ada_agent = _agent(name="ada-agent", created_by=ada_id)
    _snapshot(ada_agent, 1)
    _snapshot(ada_agent, 2)
    # runs with an unknown creator stay visible to every member
    a = _run(ada_agent, agent_snapshot_seq=1, dataset_name="window:1h")
    b = _run(ada_agent, agent_snapshot_seq=2, dataset_name="window:1h")
    _isolate()
    url = f"/api/eval/runs/compare?a={a}&b={b}"
    assert bob_client.get(url).json()["config"] is None
    assert admin.get(url).json()["config"]["between"][0]["seq"] == 2


# ── POST /api/eval/runs/{id}/rerun ───────────────────────────────────────────


@pytest.fixture
def submitted(monkeypatch, queued):
    """Record every submit_run call's keyword arguments (then submit as usual)."""
    calls: list[dict] = []
    real = svc.submit_run

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(svc, "submit_run", spy)
    return calls


def test_rerun_a_local_dataset_run(client, submitted):
    agent = _agent()
    _snapshot(agent, 1)
    _snapshot(agent, 2)
    dataset = _dataset([{"prompt": "q1"}], name="golden")
    source = _run(agent, name="weekly", description="why", dataset_id=dataset,
                  dataset_name="golden", agent_snapshot_seq=1, session_ids=["s1"])
    res = client.post(f"/api/eval/runs/{source}/rerun")
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] != source and body["status"] == "queued"
    assert (body["name"], body["description"]) == ("weekly · 再评", "why")
    assert body["dataset_id"] == dataset and body["evaluators"] == EVALS
    assert body["snapshot"] == {"seq": 2, "source": "stamped"}  # the version live now
    assert body["budget_notice"] is None
    [call] = submitted
    assert call["dataset_items"] == [{"prompt": "q1"}] and call["actor_model_id"] is None

    named = client.post(f"/api/eval/runs/{source}/rerun", json={"name": "again"}).json()
    assert named["name"] == "again"
    # a re-run of a re-run keeps one suffix
    again = client.post(f"/api/eval/runs/{body['id']}/rerun").json()
    assert again["name"] == "weekly · 再评"


def test_rerun_a_persona_dataset_uses_the_platform_actor_model(client, submitted):
    agent = _agent()
    dataset = _dataset([PERSONA], name="personas")
    source = _run(agent, dataset_id=dataset, dataset_name="personas",
                  evaluators=["Builtin.GoalSuccessRate"])
    res = client.post(f"/api/eval/runs/{source}/rerun")
    assert res.status_code == 201, res.text
    assert submitted[0]["actor_model_id"] == get_settings().actor_model_id


def test_rerun_a_pinned_cloud_run_replays_that_version(client, submitted, monkeypatch):
    from tests.evaluation.test_run_dataset_version import CLOUD_ID, _stub_cloud

    stub = _stub_cloud(monkeypatch)
    agent = _agent()
    source = _run(agent, dataset_id=CLOUD_ID, dataset_name="cloud:pinned",
                  dataset_version="2")
    res = client.post(f"/api/eval/runs/{source}/rerun")
    assert res.status_code == 201, res.text
    assert res.json()["dataset_version"] == "2"
    assert res.json()["dataset_name"] == "cloud:pinned"
    stub.get_dataset.assert_called_once_with(datasetId=CLOUD_ID, datasetVersion="2")

    stub.get_dataset.reset_mock()
    draft = _run(agent, dataset_id=CLOUD_ID, dataset_name="cloud:pinned")
    res = client.post(f"/api/eval/runs/{draft}/rerun")
    assert res.status_code == 201, res.text
    assert res.json()["dataset_version"] is None  # the draft stays the draft
    stub.get_dataset.assert_called_once_with(datasetId=CLOUD_ID)


def test_rerun_a_window_run_uses_the_same_hours(client, submitted):
    agent = _agent()
    source = _run(agent, dataset_name="window:36h")
    res = client.post(f"/api/eval/runs/{source}/rerun")
    assert res.status_code == 201, res.text
    assert res.json()["dataset_name"] == "window:36h"
    assert submitted[0]["lookback_hours"] == 36
    window = submitted[0]["time_range"]
    assert window["endTime"] - window["startTime"] == timedelta(hours=36)


def test_rerun_refuses_session_log_and_insights_runs(client, submitted):
    agent = _agent()
    for source in (
        _run(agent, session_ids=["s1"]),
        _run(agent, dataset_name="logs:2", session_ids=["s1", "s2"]),
        _run(agent, mode="insights", dataset_name="window:24h"),
    ):
        res = client.post(f"/api/eval/runs/{source}/rerun")
        assert res.status_code == 422, res.text
        assert res.json()["code"] == "run.rerun_unsupported"
    assert submitted == []
    assert client.post("/api/eval/runs/nope/rerun").json()["code"] == "run.not_found"


def test_rerun_of_a_hidden_run_is_404_and_of_a_hidden_agent_refused(ada, bob, submitted):
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    agent = _agent()
    theirs = _run(agent, created_by=ada_id, dataset_name="window:1h")
    ada_agent = _agent(name="ada-agent", created_by=ada_id)
    mine_on_theirs = _run(ada_agent, created_by=bob_id, dataset_name="window:1h")
    _isolate()
    res = bob_client.post(f"/api/eval/runs/{theirs}/rerun")
    assert res.status_code == 404 and res.json()["code"] == "run.not_found"
    # the same refusal POST /runs gives for a hidden agent
    res = bob_client.post(f"/api/eval/runs/{mine_on_theirs}/rerun")
    assert res.status_code == 400 and res.json()["code"] == "agent.not_active"
    assert ada_client.post(f"/api/eval/runs/{theirs}/rerun").status_code == 201


def test_rerun_on_a_dataset_hidden_since_is_refused_like_post_runs(ada, bob, submitted):
    ada_id, _ = ada
    bob_id, bob_client = bob
    agent = _agent()
    # bob's run on ada's dataset, made while the workspace was shared
    dataset = _dataset([{"prompt": "q"}], created_by=ada_id)
    source = _run(agent, created_by=bob_id, dataset_id=dataset, dataset_name="ds")
    _isolate()
    res = bob_client.post(f"/api/eval/runs/{source}/rerun")
    assert res.status_code == 404 and res.json()["code"] == "dataset.not_found"
    assert submitted == []


def test_rerun_refuses_a_payer_at_their_limit_like_post_runs(app, bob, submitted):
    from tests.test_budget_jobs import _limit, _spent

    bob_id, bob_client = bob
    agent = _agent()
    dataset = _dataset([{"prompt": "q"}], created_by=bob_id)
    source = _run(agent, created_by=bob_id, dataset_id=dataset, dataset_name="ds")
    _limit(bob_id, 1.0)
    _spent(bob_id, 1.0)

    rerun = bob_client.post(f"/api/eval/runs/{source}/rerun")
    create = bob_client.post("/api/eval/runs", json={
        "agent_id": agent, "dataset_id": dataset, "evaluators": EVALS, "wait_seconds": 0})
    for res in (rerun, create):
        assert res.status_code == 402, res.text
        assert res.json()["code"] == "budget.exceeded"
    assert rerun.json()["detail"] == create.json()["detail"]
    assert submitted == []


def test_rerun_route_is_classified():
    from app.core.billing_policy import BILLING, SOURCE_EVAL, Billable
    from app.core.route_policy import PERM_EVAL_RUN, ROUTE_POLICY

    assert ROUTE_POLICY[("POST", "/api/eval/runs/{run_id}/rerun")] == PERM_EVAL_RUN
    assert ROUTE_POLICY[("GET", "/api/eval/scorecard")] == "member"
    assert ROUTE_POLICY[("GET", "/api/eval/runs/compare")] == "member"
    assert BILLING[("POST", "/api/eval/runs/{run_id}/rerun")] == Billable(SOURCE_EVAL)


def test_rerun_window_of_a_log_source_keeps_its_source(client, submitted, monkeypatch):
    checked: list[list[str]] = []
    monkeypatch.setattr(eval_routers, "_assert_log_groups_exist",
                        lambda _ws, groups: checked.append(groups))
    source = _run("", agent_name="ext.DEFAULT", dataset_name="window:2h",
                  log_source={"service_name": "ext.DEFAULT", "log_group_names": ["/ext"]})
    res = client.post(f"/api/eval/runs/{source}/rerun")
    assert res.status_code == 201, res.text
    assert res.json()["log_source"] == {"service_name": "ext.DEFAULT",
                                        "log_group_names": ["/ext"]}
    assert submitted[0]["log_source"] == res.json()["log_source"]
    assert checked == [["/ext"]]  # the same log-group check as POST /runs


def test_times_on_the_scorecard_carry_utc(client):
    agent = _agent()
    _snapshot(agent, 1)
    _run(agent, dataset_name="window:1h", created_at=datetime(2026, 10, 2, tzinfo=UTC))
    body = client.get("/api/eval/scorecard").json()
    assert body["runs"][0]["created_at"].endswith("+00:00")
    assert body["agents"][0]["versions"][0]["created_at"].endswith("+00:00")
