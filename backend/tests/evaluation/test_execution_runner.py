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


def test_session_id_drift_records_both_ids_then_refuses(monkeypatch):
    """The runtime answering under another session id is a protocol failure:
    both actual ids are persisted (batch scope + Observability), the step is
    failed, correlation of earlier turns is untouched and nothing continues."""
    class DriftingAgent(FakeAgent):
        def __call__(self, client, arn, prompt, session_id=None, **k):
            out = super().__call__(client, arn, prompt, session_id=session_id, **k)
            if len(self.calls) == 2:  # A's second step (new session a2) drifts
                out["session_id"] = "runtime-chose-another-id" + "x" * 20
            return out

    fake = DriftingAgent()
    run_id, data = run_items([copy.deepcopy(ISOLATION_SCENARIO)], fake, monkeypatch)
    run = get_run(run_id)
    assert run.status == "failed" and "session id drift" in run.error
    assert len(fake.calls) == 2
    data.start_batch_evaluation.assert_not_called()
    a1, a2 = run.execution["sessions"]
    assert a1["drift"] is False and a1["turns"] == [0] and a1["status"] == "ok"
    assert a2["drift"] is True and a2["status"] == "failed" and a2["turns"] == []
    assert a2["session_id"] == a2["requested_session_id"] == fake.calls[1][1]
    assert a2["returned_session_id"].startswith("runtime-chose-another-id")
    assert run.session_ids == [fake.calls[0][1], fake.calls[1][1], a2["returned_session_id"]]
    assert ex.session_actor(run.execution, a2["returned_session_id"]) == a2["actor_id"]
    assert [(c["id"], c["outcome"]) for c in run.execution["checks"]] == [
        ("seed", "pass"), ("recall", "error"), ("no_leak", "error")]
    assert run.execution["check_status"] == "error"


def test_same_alias_second_shift_never_reassigns_earlier_turns(monkeypatch):
    two = {"scenario_id": "two", "turns": [{"input": "remember amber"}, {"input": "colour?"}],
           "metadata": {"launchpad_execution": {"version": 1, "steps": [
               {"turn": 0, "actor": "A", "session": "s"},
               {"turn": 1, "actor": "A", "session": "s"}],
               "checks": [{"id": "c", "type": "not_contains", "turn": 1, "text": "zzz"}]}}}

    class Shift(FakeAgent):
        def __call__(self, client, arn, prompt, session_id=None, **k):
            out = super().__call__(client, arn, prompt, session_id=session_id, **k)
            out["session_id"] = "r2" + "x" * 40 if len(self.calls) == 2 else session_id
            return out

    run_id, data = run_items([two], Shift(), monkeypatch)
    run = get_run(run_id)
    (row,) = run.execution["sessions"]
    assert row["turns"] == [0] and row["returned_session_id"].startswith("r2")
    assert run.execution["checks"][0]["outcome"] == "error"  # never a not_contains pass
    data.start_batch_evaluation.assert_not_called()


@pytest.mark.parametrize("text", [None, "", "   \n", 42])
def test_missing_or_blank_response_never_passes_a_negative_check(monkeypatch, text):
    class Silent(FakeAgent):
        def __call__(self, *a, **k):
            out = super().__call__(*a, **k)
            if len(self.calls) == 3:  # B's turn: no usable reply
                out["text"] = text
                if text is None:
                    del out["text"]
            return out

    one = copy.deepcopy(ISOLATION_SCENARIO)
    one["metadata"]["launchpad_execution"]["repeat"] = 1
    run_id, data = run_items([one], Silent(), monkeypatch)
    blob = get_run(run_id).execution
    assert [(c["id"], c["outcome"]) for c in blob["checks"]] == [
        ("seed", "pass"), ("recall", "pass"), ("no_leak", "error")]
    assert blob["checks"][2]["evidence"] == "no assistant response recorded for T3"
    assert blob["steps"][2]["status"] == "empty" and "T3" in blob["steps"][2]["error"]
    assert blob["calls_done"] == 2 and blob["check_status"] == "error"
    assert get_run(run_id).status == "completed"  # the batch still judges the sessions
    data.start_batch_evaluation.assert_called_once()


def test_harness_wrapper_empty_event_stream_is_missing_evidence(monkeypatch):
    """Through the real harness wrapper: an event stream with no text deltas
    yields text '' → the step is empty, not a passing not_contains."""
    from app.services.agentcore import harness as hc_mod

    client = MagicMock()
    client.invoke_harness.return_value = {"stream": [{"metadata": {}}]}
    result = hc_mod.invoke_harness_text(client, "arn:h", "hi", session_id="s" * 40, actor_id="a")
    assert result["text"] == "" and ex.response_text(result) is None
    assert ex.response_text({"text": "  \t "}) is None and ex.response_text({"text": "ok"}) == "ok"


def test_pass_requires_full_procedure_completion(monkeypatch):
    """Only step 0 is checked; the last invoke of repeat 2 raises. The recorded
    checks all pass, but the procedure is incomplete → not a pass."""
    two = {"scenario_id": "two", "turns": [{"input": "remember amber"}, {"input": "colour?"}],
           "metadata": {"launchpad_execution": {"version": 1, "repeat": 2, "steps": [
               {"turn": 0, "actor": "A", "session": "s"},
               {"turn": 1, "actor": "A", "session": "s"}],
               "checks": [{"id": "seed", "type": "contains", "turn": 0, "text": "amber"}]}}}
    run_id, _ = run_items([two], FakeAgent(fail_at=4), monkeypatch)
    run = get_run(run_id)
    blob = run.execution
    assert run.status == "failed"
    assert blob["calls_done"] == 3 and blob["calls_planned"] == 4
    assert [c["outcome"] for c in blob["checks"]] == ["pass", "pass"]
    assert blob["check_status"] == "error"


def test_stop_on_final_invoke_keeps_declared_checks(monkeypatch):
    one = copy.deepcopy(ISOLATION_SCENARIO)
    one["metadata"]["launchpad_execution"]["repeat"] = 1
    run_id = make_run([one])
    workspace = ws_ctx()

    class StopAtLast(FakeAgent):
        def __call__(self, *a, **k):
            out = super().__call__(*a, **k)
            if len(self.calls) == 3:
                svc.request_stop(run_id, workspace=workspace)
            return out

    _, data = run_items([one], StopAtLast(), monkeypatch, run_id=run_id)
    run = get_run(run_id)
    assert run.status == "stopped"
    data.start_batch_evaluation.assert_not_called()
    blob = run.execution
    assert [c["outcome"] for c in blob["checks"]] == ["pass", "pass", "pass"]
    assert blob["interrupted"] is True and blob["check_status"] == "inconclusive"


def test_stop_before_first_step_and_between_repeats(monkeypatch):
    run_id = make_run([copy.deepcopy(ISOLATION_SCENARIO)])
    # the operator stops while the first actor is being minted — before invoke #1
    real_actor = ex.synthetic_actor

    def stopping_actor(**kw):
        svc.stop_flags.request(run_id)
        return real_actor(**kw)

    monkeypatch.setattr(svc.execution, "synthetic_actor", stopping_actor)
    fake = FakeAgent()
    _, data = run_items([copy.deepcopy(ISOLATION_SCENARIO)], fake, monkeypatch, run_id=run_id)
    monkeypatch.setattr(svc.execution, "synthetic_actor", real_actor)
    run = get_run(run_id)
    assert run.status == "stopped" and fake.calls == [] and run.session_ids == []
    data.start_batch_evaluation.assert_not_called()
    # declared checks of the interrupted repeat are recorded (seed has no answer →
    # error; its dependents → inconclusive), never silently absent
    assert [c["outcome"] for c in run.execution["checks"]] == [
        "error", "inconclusive", "inconclusive"]
    assert run.execution["check_status"] == "error" and run.execution["interrupted"] is True

    run_id = make_run([copy.deepcopy(ISOLATION_SCENARIO)])
    workspace = ws_ctx()

    class StopAfterRepeat1(FakeAgent):
        def __call__(self, *a, **k):
            out = super().__call__(*a, **k)
            if len(self.calls) == 3:
                svc.request_stop(run_id, workspace=workspace)
            return out

    run_items([copy.deepcopy(ISOLATION_SCENARIO)], StopAfterRepeat1(), monkeypatch, run_id=run_id)
    blob = get_run(run_id).execution
    assert [c["outcome"] for c in blob["checks"]] == ["pass"] * 3
    assert blob["check_status"] == "inconclusive" and len(blob["sessions"]) == 3


def test_position_keyed_dispatch_survives_normalized_id_collision(monkeypatch):
    """Direct runner path (the API refuses this shape): an opt-in scenario named
    item_2 next to a legacy prompt must not make the legacy item run the plan."""
    adv = dict(copy.deepcopy(ISOLATION_SCENARIO), scenario_id="item_2")
    adv["metadata"]["launchpad_execution"]["repeat"] = 1
    fake = FakeAgent()
    run_id, data = run_items([adv, {"prompt": "bye"}], fake, monkeypatch)
    run = get_run(run_id)
    assert run.status == "completed", run.error
    assert [c[0] for c in fake.calls][-1] == "bye" and fake.calls[-1][2] == "default"
    assert len(fake.calls) == 4 and len(run.session_ids) == 4
    meta = data.start_batch_evaluation.call_args.kwargs["evaluationMetadata"]["sessionMetadata"]
    assert all(m["testScenarioId"].startswith("item_2#") for m in meta)


def test_items_are_snapshotted_before_enqueue(monkeypatch):
    """Mutating the caller's nested list AFTER submit_run returns but BEFORE the
    queued callable runs must not reach execute_run."""
    from app.evaluation.queue import run_queue

    db = SessionLocal()
    agent = make_agent(db, name="snap-agent")
    db.close()
    monkeypatch.setattr(svc, "resolve_telemetry", lambda *a, **k: ("svc.DEFAULT", "/lg"))
    held = {}
    monkeypatch.setattr(run_queue, "submit", lambda run_id, fn: held.setdefault("fn", fn) and 0)
    seen = {}
    monkeypatch.setattr(
        svc, "execute_run", lambda run_id, **kw: seen.setdefault("items", kw["items"]))
    items = [copy.deepcopy(ISOLATION_SCENARIO)]
    svc.submit_run(agent=agent, workspace=ws_ctx(), dataset_items=items, dataset_id="d",
                   dataset_name="d", evaluators=["Builtin.Correctness"])
    items[0]["turns"][0]["input"] = "MUTATED"
    items[0]["metadata"]["launchpad_execution"]["repeat"] = 5
    held["fn"]()
    assert seen["items"][0]["turns"][0]["input"].startswith("My favourite")
    assert seen["items"][0]["metadata"]["launchpad_execution"]["repeat"] == 2


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
    assert out[0]["outcome"] == "error" and out[0]["evidence"].endswith("for T1")
    for blank in ("", "   ", "\n\t", 7):
        neg = ex.evaluate_checks(plan, 1, {0: "Yes.", 1: blank})
        assert neg[2]["outcome"] == "error", blank
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
