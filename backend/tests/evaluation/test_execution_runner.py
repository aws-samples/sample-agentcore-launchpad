"""Multi-actor / multi-session procedure runner (SE-046) — hermetic.

Low-level invoke fakes record every call (prompt, session, actor) so the tests
assert the exact ordered sequence: same actor + same session reuses the id,
same actor + new session alias mints a fresh id under the same actor, another
actor never shares an identity, and every repeat / run gets its own actors.
"""

import copy
import time
from unittest.mock import MagicMock

import pytest

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import execution as ex
from app.evaluation.models import EvalDataset, EvalRun
from tests.conftest import ws_ctx
from tests.evaluation.test_execution_schema import ISOLATION_SCENARIO
from tests.evaluation.test_runs_flow import make_agent, stub_environment

PLAIN_SCENARIO = {"scenario_id": "plain", "turns": [{"input": "hello", "expected_response": "hi"}]}


class FakeAgent:
    """Deterministic agent double: remembers per actor across sessions (a
    memory-backed agent), never across actors. Optional overrides make a
    specific (actor alias, turn) answer wrongly or raise."""

    def __init__(self, marker="amber", leak=False, fail_at=None, wrong_seed=False):
        self.calls: list[tuple[str, str, str]] = []
        self.memory: dict[str, str] = {}
        self.marker = marker
        self.leak = leak
        self.fail_at = fail_at  # call index (1-based) that raises
        self.wrong_seed = wrong_seed

    def __call__(self, client, arn, prompt, session_id=None, actor_id="default",
                 runtime_user_id=None, **_):
        self.calls.append((prompt, session_id, actor_id))
        if self.fail_at == len(self.calls):
            raise RuntimeError("runtime exploded")
        if "remember" in prompt:
            self.memory[actor_id] = self.marker
            if self.wrong_seed:
                return {"text": "I cannot store that.", "session_id": session_id}
            return {"text": f"Noted — {self.marker}.", "session_id": session_id}
        known = self.memory.get(actor_id)
        if known is None and self.leak:
            known = next(iter(self.memory.values()), None)
        text = f"Your favourite colour is {known}." if known else "I don't know yet."
        return {"text": text, "session_id": session_id}


def make_run(items, **fields) -> str:
    db = SessionLocal()
    try:
        run = EvalRun(
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id="agent-1",
            agent_name="eval-target",
            mode="evaluators",
            evaluators=["Builtin.Correctness"],
            status="queued",
            **fields,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def get_run(run_id) -> EvalRun:
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()


def run_items(items, fake, monkeypatch, run_id=None, method="zip_runtime", protocol="http"):
    data = MagicMock()
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(svc, "_wait_for_fresh_telemetry", MagicMock())
    monkeypatch.setattr(svc.rt, "invoke_runtime_text", fake)
    monkeypatch.setattr(svc.hc, "invoke_harness_text", fake)
    data.start_batch_evaluation.return_value = {"batchEvaluationId": "be-1"}
    data.get_batch_evaluation.return_value = {"status": "COMPLETED", "evaluationResults": {
        "evaluatorSummaries": [{"evaluatorId": "Builtin.Correctness",
                                "statistics": {"averageScore": 1.0}}]}}
    run_id = run_id or make_run(items)
    svc.execute_run(
        run_id, workspace=ws_ctx(), agent_arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/rt-1",
        method=method, protocol=protocol, service_name="rt.DEFAULT", log_group="/lg",
        items=items, evaluators=["Builtin.Correctness"], mode="evaluators", wait_seconds=0,
        agent_id="a" * 32,
    )
    return run_id, data


def test_happy_path_identities_order_and_checks(monkeypatch):
    fake = FakeAgent()
    run_id, data = run_items([copy.deepcopy(ISOLATION_SCENARIO)], fake, monkeypatch)
    run = get_run(run_id)
    assert run.status == "completed", run.error
    # 3 steps × repeat 2, in declared order
    assert [c[0][:12] for c in fake.calls] == [
        "My favourite", "What is my f", "What is my f"] * 2
    r1, r2 = fake.calls[:3], fake.calls[3:]
    for calls in (r1, r2):
        (p0, s0, a0), (p1, s1, a1), (p2, s2, a2) = calls
        assert a0 == a1 != a2  # same actor across a1/a2, B is someone else
        assert len({s0, s1, s2}) == 3  # every session alias is a fresh runtime session
        assert all(len(s) >= 33 for s in (s0, s1, s2))
    # repeats never share an actor
    assert {c[2] for c in r1}.isdisjoint({c[2] for c in r2})
    assert all(c[2].startswith("a" * 32 + "__eval__") for c in fake.calls)
    # every actual session persisted, in creation order, and scoped to the batch
    assert run.session_ids == [c[1] for c in fake.calls]
    kwargs = data.start_batch_evaluation.call_args.kwargs
    assert kwargs["dataSourceConfig"]["cloudWatchLogs"]["filterConfig"]["sessionIds"] == (
        run.session_ids
    )
    # ledger
    blob = run.execution
    assert blob["check_status"] == "pass"
    assert blob["calls_done"] == blob["calls_planned"] == 6
    assert [(s["repeat"], s["actor"], s["session"], s["turns"]) for s in blob["sessions"]] == [
        (1, "A", "a1", [0]), (1, "A", "a2", [1]), (1, "B", "b1", [2]),
        (2, "A", "a1", [0]), (2, "A", "a2", [1]), (2, "B", "b1", [2]),
    ]
    assert all(s["actor_id"] and s["status"] == "ok" and not s["drift"] for s in blob["sessions"])
    assert [(c["repeat"], c["id"], c["outcome"]) for c in blob["checks"]] == [
        (1, "seed", "pass"), (1, "recall", "pass"), (1, "no_leak", "pass"),
        (2, "seed", "pass"), (2, "recall", "pass"), (2, "no_leak", "pass"),
    ]
    assert "amber" in blob["checks"][0]["evidence"]


def test_ground_truth_split_by_actual_session(monkeypatch):
    fake = FakeAgent()
    run_id, data = run_items([copy.deepcopy(ISOLATION_SCENARIO)], fake, monkeypatch)
    run = get_run(run_id)
    meta = data.start_batch_evaluation.call_args.kwargs["evaluationMetadata"]["sessionMetadata"]
    by_sid = {m["sessionId"]: m for m in meta}
    assert set(by_sid) == set(run.session_ids)
    assert len({m["testScenarioId"] for m in meta}) == 6  # unique per repeat/session
    a2 = [m for m in meta if m["metadata"]["launchpad_session"] == "a2"]
    b1 = [m for m in meta if m["metadata"]["launchpad_session"] == "b1"]
    a1 = [m for m in meta if m["metadata"]["launchpad_session"] == "a1"]
    assert a2[0]["testScenarioId"] == "gt_isolation#r1/a2"
    assert a2[0]["groundTruth"]["inline"] == {"turns": [{
        "input": {"prompt": "What is my favourite colour?"},
        "expectedResponse": {"text": "amber"}}]}
    # the scenario-level assertion goes to the outcome (last-step) session only
    assert b1[0]["groundTruth"]["inline"]["assertions"] == [
        {"text": "The agent never tells actor B what actor A said."}]
    assert "groundTruth" not in a1[0]
    assert a1[0]["metadata"] == {"launchpad_scenario": "gt_isolation", "launchpad_repeat": "1",
                                 "launchpad_actor": "A", "launchpad_session": "a1"}


def test_mixed_dataset_keeps_legacy_ordering_and_call_shape(monkeypatch):
    fake = FakeAgent()
    items = [copy.deepcopy(PLAIN_SCENARIO), copy.deepcopy(ISOLATION_SCENARIO),
             {"prompt": "bye"}]
    run_id, data = run_items(items, fake, monkeypatch)
    run = get_run(run_id)
    assert run.status == "completed", run.error
    # plain scenarios still call with the bare default actor
    assert fake.calls[0][2] == "default" and fake.calls[-1][2] == "default"
    assert len(run.session_ids) == 1 + 6 + 1
    meta = data.start_batch_evaluation.call_args.kwargs["evaluationMetadata"]["sessionMetadata"]
    assert meta[0] == {"sessionId": run.session_ids[0], "testScenarioId": "plain",
                       "groundTruth": {"inline": {"turns": [{
                           "input": {"prompt": "hello"}, "expectedResponse": {"text": "hi"}}]}}}
    assert run.execution["scenarios"] == [
        {"scenario_id": "gt_isolation", "repeat": 2, "steps": 3, "checks": 3}]


def test_run_without_extension_has_no_execution_blob(monkeypatch):
    fake = FakeAgent()
    run_id, _ = run_items([copy.deepcopy(PLAIN_SCENARIO)], fake, monkeypatch)
    run = get_run(run_id)
    assert run.status == "completed" and run.execution is None
    assert fake.calls == [("hello", fake.calls[0][1], "default")]


def test_leak_fails_and_seed_failure_makes_dependents_inconclusive(monkeypatch):
    run_id, _ = run_items([copy.deepcopy(ISOLATION_SCENARIO)], FakeAgent(leak=True), monkeypatch)
    blob = get_run(run_id).execution
    assert blob["check_status"] == "fail"
    assert [c["outcome"] for c in blob["checks"] if c["id"] == "no_leak"] == ["fail", "fail"]

    run_id, _ = run_items([copy.deepcopy(ISOLATION_SCENARIO)], FakeAgent(wrong_seed=True),
                          monkeypatch)
    blob = get_run(run_id).execution
    assert [(c["id"], c["outcome"]) for c in blob["checks"][:3]] == [
        ("seed", "fail"), ("recall", "inconclusive"), ("no_leak", "inconclusive")]
    assert "precondition not met: seed=fail" in blob["checks"][1]["evidence"]
    assert blob["check_status"] == "fail"


def test_repeat_aggregate_requires_every_repeat(monkeypatch):
    class FlakyAgent(FakeAgent):
        def __call__(self, *a, **k):
            out = super().__call__(*a, **k)
            if len(self.calls) == 6:  # repeat 2, actor B → leaks once
                out["text"] = "Your favourite colour is amber."
            return out

    run_id, _ = run_items([copy.deepcopy(ISOLATION_SCENARIO)], FlakyAgent(), monkeypatch)
    blob = get_run(run_id).execution
    outcomes = [(c["repeat"], c["id"], c["outcome"]) for c in blob["checks"]]
    assert (1, "no_leak", "pass") in outcomes and (2, "no_leak", "fail") in outcomes
    assert blob["check_status"] == "fail"


def test_invoke_failure_persists_partial_sessions_and_errors_checks(monkeypatch):
    fake = FakeAgent(fail_at=2)  # A's recall turn blows up
    run_id, data = run_items([copy.deepcopy(ISOLATION_SCENARIO)], fake, monkeypatch)
    run = get_run(run_id)
    assert run.status == "failed" and "runtime exploded" in run.error
    data.start_batch_evaluation.assert_not_called()
    # both sessions that were actually opened are on the row
    assert run.session_ids == [fake.calls[0][1], fake.calls[1][1]]
    blob = run.execution
    assert [s["status"] for s in blob["steps"]] == ["ok", "failed"]
    assert blob["steps"][1]["error"].startswith("RuntimeError")
    assert [(s["session"], s["status"]) for s in blob["sessions"]] == [
        ("a1", "ok"), ("a2", "failed")]
    # the seed passed, recall has no answer → error, no_leak never ran → error
    assert [(c["id"], c["outcome"]) for c in blob["checks"]] == [
        ("seed", "pass"), ("recall", "error"), ("no_leak", "error")]
    assert blob["check_status"] == "error"
    assert blob["calls_done"] == 1


def test_stop_between_steps_persists_sessions_and_never_starts_batch(monkeypatch):
    fake = FakeAgent()
    run_id = make_run([copy.deepcopy(ISOLATION_SCENARIO)])
    workspace = ws_ctx()

    class StoppingAgent(FakeAgent):
        def __call__(self, *a, **k):
            out = super().__call__(*a, **k)
            if len(self.calls) == 2:
                svc.request_stop(run_id, workspace=workspace)
            return out

    fake = StoppingAgent()
    _, data = run_items([copy.deepcopy(ISOLATION_SCENARIO)], fake, monkeypatch, run_id=run_id)
    run = get_run(run_id)
    assert run.status == "stopped" and run.error == svc.STOP_REASON
    assert len(fake.calls) == 2  # the flag was checked before step 3
    data.start_batch_evaluation.assert_not_called()
    assert run.session_ids == [fake.calls[0][1], fake.calls[1][1]]
    blob = run.execution
    assert [(c["id"], c["outcome"]) for c in blob["checks"]] == [
        ("seed", "pass"), ("recall", "pass"), ("no_leak", "error")]
    # a stopped run with all recorded checks passing is still not a pass
    assert blob["check_status"] == "error"
    assert not svc.stop_requested(run_id)


def test_a2a_agent_refused_before_any_invoke(monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(svc.rt, "invoke_a2a_text", fake)
    run_id, data = run_items([copy.deepcopy(ISOLATION_SCENARIO)], fake, monkeypatch,
                             protocol="a2a")
    run = get_run(run_id)
    assert run.status == "failed" and "A2A" in run.error
    assert fake.calls == [] and run.session_ids == []
    data.start_batch_evaluation.assert_not_called()


def test_harness_path_passes_actor_id(monkeypatch):
    fake = FakeAgent()
    run_id, _ = run_items([copy.deepcopy(ISOLATION_SCENARIO)], fake, monkeypatch, method="harness")
    assert get_run(run_id).status == "completed"
    assert all(c[2].startswith("a" * 32 + "__eval__") for c in fake.calls)


def test_session_id_drift_is_tracked(monkeypatch):
    class DriftingAgent(FakeAgent):
        def __call__(self, client, arn, prompt, session_id=None, **k):
            out = super().__call__(client, arn, prompt, session_id=session_id, **k)
            if len(self.calls) == 1:
                out["session_id"] = "runtime-chose-another-id" + "x" * 20
            return out

    run_id, _ = run_items([copy.deepcopy(ISOLATION_SCENARIO)], DriftingAgent(), monkeypatch)
    run = get_run(run_id)
    row = run.execution["sessions"][0]
    assert row["drift"] is True
    assert row["session_id"].startswith("runtime-chose-another-id")
    assert row["requested_session_id"] != row["session_id"]
    assert row["session_id"] in run.session_ids
    assert row["requested_session_id"] not in run.session_ids


def test_evaluate_checks_unit_semantics():
    plan = ex.parse_plan({
        "scenario_id": "u", "turns": [{"input": "a"}, {"input": "b"}],
        "metadata": {"launchpad_execution": {"version": 1, "steps": [
            {"turn": 0, "actor": "A", "session": "s"}, {"turn": 1, "actor": "A", "session": "s"}],
            "checks": [
                {"id": "exact", "type": "exact", "turn": 0, "text": "Yes."},
                {"id": "cs", "type": "contains", "turn": 0, "text": "Yes", "case_sensitive": True},
                {"id": "dep", "type": "not_contains", "turn": 1, "text": "x", "depends_on": ["cs"]},
            ]}}})
    out = ex.evaluate_checks(plan, 1, {0: " yes. ", 1: "fine"})
    assert [(c["id"], c["outcome"]) for c in out] == [
        ("exact", "pass"), ("cs", "fail"), ("dep", "inconclusive")]
    out = ex.evaluate_checks(plan, 1, {0: None, 1: "fine"})
    assert out[0]["outcome"] == "error" and "no assistant response" in out[0]["evidence"]
    assert ex.aggregate_outcome([]) == "none"
    assert ex.aggregate_outcome([{"outcome": "pass"}, {"outcome": "inconclusive"}]) == (
        "inconclusive")


def test_run_out_exposes_execution_and_dataset_snapshot_is_pinned(client, monkeypatch):
    """Through the API: the row echoes the ledger, and editing the dataset while
    the run is queued does not change what the queued run replays."""
    from app.evaluation.queue import run_queue

    db = SessionLocal()
    agent = make_agent(db, name="proc-agent")
    ds = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="iso", kind="predefined",
                     items=[copy.deepcopy(ISOLATION_SCENARIO)])
    db.add(ds)
    db.commit()
    ds_id, agent_id = ds.id, agent.id
    db.close()
    data, _ = stub_environment(monkeypatch)
    fake = FakeAgent()
    monkeypatch.setattr(svc.rt, "invoke_runtime_text", fake)
    # hold the worker so the run sits queued while the dataset is edited
    gate = MagicMock()
    started = {"items": None}
    original = svc.execute_run

    def slow_execute(run_id, **kw):
        gate()  # blocks until released
        started["items"] = copy.deepcopy(kw["items"])
        return original(run_id, **kw)

    import threading
    release = threading.Event()
    gate.side_effect = lambda: release.wait(5)
    monkeypatch.setattr(svc, "execute_run", slow_execute)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": ds_id,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0})
    assert res.status_code == 201, res.text
    run_id = res.json()["id"]
    edited = copy.deepcopy(ISOLATION_SCENARIO)
    edited["metadata"]["launchpad_execution"]["repeat"] = 1
    assert client.put(f"/api/eval/datasets/{ds_id}", json={"items": [edited]}).status_code == 200
    release.set()
    for _ in range(100):
        body = client.get(f"/api/eval/runs/{run_id}").json()
        if body["status"] in ("completed", "failed", "stopped"):
            break
        time.sleep(0.05)
    assert body["status"] == "completed", body.get("error")
    assert started["items"][0]["metadata"]["launchpad_execution"]["repeat"] == 2
    assert body["execution"]["check_status"] == "pass"
    assert len(body["execution"]["sessions"]) == 6
    assert len(fake.calls) == 6
    run_queue.cancel(run_id)


@pytest.mark.parametrize("bad_protocol", ["a2a"])
def test_service_refusal_message_names_protocol(bad_protocol):
    with pytest.raises(Exception):  # noqa: B017 — AppError shape asserted below
        ex.require_actor_envelope([ISOLATION_SCENARIO], method="zip_runtime",
                                  protocol=bad_protocol)
    ex.require_actor_envelope([ISOLATION_SCENARIO], method="harness", protocol=bad_protocol)
    ex.require_actor_envelope([PLAIN_SCENARIO], method="zip_runtime", protocol=bad_protocol)
