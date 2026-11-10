"""D3-c (eval Round 3): a run records the dataset version it replayed, and every replayed
session says how many turns its scenario defines, how many the replay sent and how many a
turn-level evaluator judged."""

import uuid

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from app.services.strands_eval import jobs_batch
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal


def _scenario(sid: str, prompts: list[str]) -> dict:
    return {"scenario_id": sid, "turns": [{"input": p} for p in prompts]}


def test_a_local_dataset_revision_moves_with_its_items_only(client):
    ds = client.post("/api/eval/datasets", json={
        "name": "rev", "items": [_scenario("a", ["hi"])]}).json()
    assert ds["revision"] == 1
    renamed = client.put(f"/api/eval/datasets/{ds['id']}", json={"name": "rev-2"}).json()
    assert renamed["revision"] == 1
    same = client.put(f"/api/eval/datasets/{ds['id']}",
                      json={"items": [_scenario("a", ["hi"])]}).json()
    assert same["revision"] == 1  # an identical replacement is no new version
    edited = client.put(f"/api/eval/datasets/{ds['id']}",
                        json={"items": [_scenario("a", ["hello"])]}).json()
    assert edited["revision"] == 2


def test_a_run_records_the_revision_and_each_sessions_turns(client, monkeypatch):
    _env, calls = stub_environment(monkeypatch)
    db = SessionLocal()
    agent = make_agent(db, name=f"turns-{uuid.uuid4().hex[:6]}")
    db.close()
    ds = client.post("/api/eval/datasets", json={
        "name": "turns", "items": [_scenario("multi", ["one", "two", "three"]),
                                   _scenario("single", ["only"])]}).json()
    client.put(f"/api/eval/datasets/{ds['id']}", json={
        "items": [_scenario("multi", ["one", "two"]), _scenario("single", ["only"])]})
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds["id"],
        "evaluators": ["Builtin.Helpfulness"], "wait_seconds": 0})
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert run["dataset_revision"] == 2
    assert calls["prompts"] == ["one", "two", "only"]
    turns = {s["scenario_id"]: (s["turns_defined"], s["turns_replayed"])
             for s in run["session_scenarios"]}
    assert turns == {"multi": (2, 2), "single": (1, 1)}

    body = client.get(f"/api/eval/runs/{run['id']}/results").json()
    by_scenario = {s["scenario_id"]: s["turns"] for s in body["sessions"]}
    for sid, session in ((s["scenario_id"], s) for s in body["sessions"]):
        traces = {r["trace_id"] for r in session["results"] if r.get("level") == "TRACE"
                  and r.get("trace_id") and r.get("score") is not None}
        assert by_scenario[sid]["judged"] == (len(traces) or None)
    assert by_scenario["multi"]["defined"] == 2 and by_scenario["multi"]["replayed"] == 2
    assert all(t["judged"] for t in by_scenario.values())  # Helpfulness scores every turn

    # a later edit is a new revision; the finished run keeps the one it replayed
    client.put(f"/api/eval/datasets/{ds['id']}", json={"items": [_scenario("x", ["y"])]})
    assert client.get(f"/api/eval/runs/{run['id']}").json()["dataset_revision"] == 2


def test_a_failed_replay_counts_the_turns_it_sent():
    sent: list[str] = []

    def invoke(prompt, session_id, session_spec):
        sent.append(prompt)
        if prompt == "two":
            raise ValueError("boom")  # not transient: no retry
        return {"text": "ok", "session_id": session_id}

    env = jobs_batch.WorkerEnv(logs_client=None, data_client=None,
                               judge_factory=lambda _m: None, default_model_id="m",
                               invoke=invoke, sleep=lambda _s: None)

    class Sink:
        def cancelled(self):
            return False

    record = jobs_batch._replay_session(
        env, {"target": {}}, {"session_id": "s" * 33, "scenario_id": "multi",
                              "prompts": ["one", "two", "three"]}, Sink())
    assert record["status"] == "failed"
    assert record["turns_replayed"] == 1


def test_older_rows_report_unknown_turns(client):
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="", agent_name="old",
                      status="completed", mode="evaluators",
                      session_ids=["s1" + "x" * 32],
                      scenario_map={"s1" + "x" * 32: {"scenario_id": "a", "input": "q"}})
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    (entry,) = client.get(f"/api/eval/runs/{run_id}").json()["session_scenarios"]
    assert entry["turns_defined"] is None and entry["turns_replayed"] is None
