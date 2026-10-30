"""N17-obs416 — a session without tool calls expects no TOOL_CALL unit.

``run_coverage`` expected ``max(rows, 1)`` TOOL_CALL units per session, so a session with no
tool call counted one expected-but-missing unit (live run 5fbc8ecafabd: one tool-less session,
ToolSelection / ToolParameter expected 1, judged 0 → coverage 0.8, below threshold, alarm).
The worker now reports each evaluated session's tool calls (``done.json`` ``tool_units``:
``{session_id: execute_tool spans}``, the units the TOOL_CALL evaluators judge) and that is the
expected count, 0 allowed.
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from typing import Any

from app.evaluation import jobs, run_coverage
from app.services.strands_eval import jobs_batch
from tests.evaluation.code_eval_support import CORPUS, GROUPS, SID
from tests.evaluation.corpus_support import GroupLogs
from tests.evaluation.fakes import FakeJudge

TSA = "Builtin.ToolSelectionAccuracy"


def test_a_tool_less_session_expects_no_tool_unit():
    run = SimpleNamespace(session_ids=["session-0001"], evaluators=[TSA, "Builtin.Helpfulness"],
                          extraction_check={"sessions": {"session-0001": {
                              "anomalies": {"turns": 1}}}},
                          tool_units={"session-0001": 0})
    records = [{"session_id": "session-0001", "evaluator_id": "Builtin.Helpfulness",
                "level": "TRACE", "trace_id": "t0", "score": 0.9, "label": "Good",
                "error_type": None, "error_message": None}]
    cov = run_coverage.run_coverage(records, run, threshold=0.9)
    by = {e["evaluator_id"]: e for e in cov["evaluators"]}
    assert (by[TSA]["expected"], by[TSA]["missing"]) == (0, 0)
    assert cov["coverage"] == 1.0 and cov["below_threshold"] is False


def test_a_session_with_calls_expects_one_unit_per_call():
    run = SimpleNamespace(session_ids=["session-0001"], evaluators=[TSA], extraction_check={},
                          tool_units={"session-0001": 3})
    rows = [{"session_id": "session-0001", "evaluator_id": TSA, "level": "TOOL_CALL",
             "trace_id": "t", "score": 1.0, "label": "Yes", "error_type": None,
             "error_message": None} for _ in range(2)]
    cov = run_coverage.run_coverage(rows, run, threshold=0.9)
    assert (cov["expected"], cov["judged"], cov["missing"]) == (3, 2, 1)


def test_without_the_worker_count_the_old_rule_stays():
    run = SimpleNamespace(session_ids=["session-0001"], evaluators=[TSA], extraction_check={})
    cov = run_coverage.run_coverage([], run, threshold=0.9)
    assert cov["expected"] == 1  # an older worker reports no tool_units


class Sink:
    def __init__(self) -> None:
        self.job = types.SimpleNamespace(job_id="j-tu")
        self.rows: list[dict] = []
        self.finished: tuple | None = None

    def cancelled(self) -> bool:
        return False

    def status(self, *_a: Any, **_kw: Any) -> None:
        pass

    def invocation(self, _r: Any) -> None:
        pass

    def results(self, records: list[dict]) -> None:
        self.rows.extend(records)

    def result(self, record: dict) -> None:
        self.rows.append(record)

    def finish(self, state: str, error: Any = None, summary: Any = None) -> None:
        self.finished = (state, error, summary or {})


def test_the_worker_reports_each_sessions_tool_calls():
    spec = {
        "run_id": "run-tu", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": [{"session_id": SID, "scenario_id": None, "prompts": [], "replay": False}],
        "replay": False,
        "wait": {"seconds": 0, "log_groups": GROUPS, "spans_source": "both",
                 "lookback_hours": 48},
        "evaluator_defs": [{"id": TSA}], "ground_truth": {}, "judge_model_id": "judge",
    }
    env = jobs_batch.WorkerEnv(logs_client=GroupLogs(CORPUS["records"]), data_client=None,
                               judge_factory=lambda m, **kw: FakeJudge(), sleep=lambda s: None)
    sink = Sink()
    jobs_batch.run_job(jobs.JobSpec(job_id="j-tu", job_type="batch_eval", attempt=1,
                                    workspace={}, sink={}, spec=spec), sink, env)
    assert sink.finished[2]["tool_units"] == {SID: 3}  # one gateway call per turn


def test_the_hub_folds_the_shards_counts():
    from app.evaluation import service as svc
    from app.evaluation.jobs import JobDone

    dones = [JobDone(state="COMPLETED", result_count=0, summary={"tool_units": {"a": 2}}),
             JobDone(state="COMPLETED", result_count=0, summary={"tool_units": {"b": 0}}),
             JobDone(state="COMPLETED", result_count=0, summary={})]
    assert svc.fold_tool_units(dones) == {"a": 2, "b": 0}
