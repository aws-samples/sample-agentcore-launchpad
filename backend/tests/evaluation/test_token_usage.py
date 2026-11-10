"""Production gaps P8.3 / P8.4 — token accounting and an optional per-run token budget.

``tokenUsage`` was never recorded, so a run's judge spend could not even be reported, and no
run had a budget. Every judge call now counts the model's ``usage`` (the stream's metadata
event): each result row carries ``token_usage {inputTokens, outputTokens, totalTokens}`` (the
Global result shape; a unit's tokens sit on its first row), ``done.json`` the job's totals,
the run ``token_usage`` the run's totals. ``Settings.eval_run_token_budget`` (0 = off), split
over the run's shards, stops a job from STARTING another session once it is spent — the
partial results stay, the job completes, and the run says ``exhausted``.
"""

from __future__ import annotations

import types
from typing import Any

from app.evaluation import jobs
from app.services.strands_eval import deadlines, engine_core, jobs_batch
from app.services.strands_eval.spans_provider import LaunchpadSpansProvider
from tests.evaluation.code_eval_support import CORPUS, GROUPS, SID, TURN_TRACES
from tests.evaluation.corpus_support import GroupLogs
from tests.evaluation.fakes import FakeJudge

USAGE_PER_CALL = {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}  # FakeJudge


def _session_records(definitions, judge, ledger):
    provider = LaunchpadSpansProvider(GroupLogs(CORPUS["records"]), GROUPS)
    evaluators = deadlines.DeadlineEvaluatorSet(definitions, lambda _m, **_kw: judge,
                                                deadline_s=30, usage=ledger)
    records = engine_core.evaluate_session(provider, SID, evaluators, None, job_id="run1",
                                           sleep=lambda _s: None)
    ledger.stamp(records)
    return records


def test_every_trace_row_carries_its_turns_tokens():
    ledger = deadlines.UsageLedger()
    judge = FakeJudge()
    records = _session_records([{"id": "Builtin.Helpfulness"}], judge, ledger)
    assert [r["trace_id"] for r in records] == TURN_TRACES
    calls_per_turn = len(judge.calls) // len(TURN_TRACES)
    for row in records:
        usage = row["token_usage"]
        assert usage["totalTokens"] == usage["inputTokens"] + usage["outputTokens"] > 0
        assert usage["inputTokens"] == calls_per_turn * USAGE_PER_CALL["inputTokens"]
    totals = ledger.totals()
    assert totals["totalTokens"] == len(judge.calls) * USAGE_PER_CALL["totalTokens"]


def test_a_session_unit_carries_its_tokens_once_and_deterministic_rows_none():
    ledger = deadlines.UsageLedger()
    records = _session_records([{"id": "Builtin.GoalSuccessRate"},
                                {"id": "Builtin.TrajectoryExactOrderMatch"}], FakeJudge(), ledger)
    by = {r["evaluator_id"]: r for r in records}
    assert by["Builtin.GoalSuccessRate"]["token_usage"]["totalTokens"] > 0
    assert by["Builtin.TrajectoryExactOrderMatch"].get("token_usage") is None


class Sink:
    def __init__(self) -> None:
        self.job = types.SimpleNamespace(job_id="j-tok")
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


def _batch(session_ids, budget=None):
    from tests.test_strands_engine import logs_with

    spec = {
        "run_id": "run-tok", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": [{"session_id": s, "scenario_id": None, "prompts": [], "replay": False}
                     for s in session_ids],
        "replay": False,
        "wait": {"seconds": 0, "log_groups": ["/aws/bedrock-agentcore/runtimes/x-DEFAULT"],
                 "spans_source": "runtime_logs", "lookback_hours": 48},
        "evaluator_defs": [{"id": "Builtin.GoalSuccessRate"}], "ground_truth": {},
        "judge_model_id": "judge",
        **({"token_budget": budget} if budget is not None else {}),
    }
    env = jobs_batch.WorkerEnv(logs_client=logs_with(*session_ids), data_client=None,
                               judge_factory=lambda m, **kw: FakeJudge(),
                               sleep=lambda s: None, judge_concurrency=1)
    sink = Sink()
    jobs_batch.run_job(jobs.JobSpec(job_id="j-tok", job_type="batch_eval", attempt=1,
                                    workspace={}, sink={}, spec=spec), sink, env)
    return sink


def test_the_batch_job_reports_its_totals():
    from tests.test_strands_engine import SID as S1
    from tests.test_strands_engine import SID2 as S2

    sink = _batch([S1, S2])
    state, _err, summary = sink.finished
    assert state == "COMPLETED" and len(sink.rows) == 2
    assert all(r["token_usage"]["totalTokens"] > 0 for r in sink.rows)
    assert summary["token_usage"]["totalTokens"] == sum(
        r["token_usage"]["totalTokens"] for r in sink.rows)
    assert "token_budget_exhausted" not in summary


def test_a_spent_budget_stops_starting_sessions_and_keeps_the_partial_results():
    from tests.test_strands_engine import SID as S1
    from tests.test_strands_engine import SID2 as S2

    sink = _batch([S1, S2], budget=1)  # the first session's judge spends more than that
    state, _err, summary = sink.finished
    assert state == "COMPLETED"
    assert [r["session_id"] for r in sink.rows] == [S1]
    assert summary["token_budget_exhausted"] is True
    assert summary["token_usage"]["totalTokens"] >= 1


def test_the_hub_splits_the_budget_and_folds_the_totals(monkeypatch):
    import app.evaluation.service as svc
    from app.evaluation.jobs import JobDone
    from app.evaluation.routers import _run_out
    from tests.evaluation.test_resume import get_run
    from tests.evaluation.test_run_record_errors import _record, _settle

    assert svc.shard_token_budget(1000, 3) == 334
    assert svc.shard_token_budget(0, 3) is None
    done = JobDone(state="COMPLETED", result_count=1, summary={
        "token_usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "token_budget_exhausted": True})
    assert svc.fold_token_usage([done, done], budget=100) == {
        "inputTokens": 20, "outputTokens": 10, "totalTokens": 30, "budget": 100,
        "exhausted": True}
    run = get_run(_settle([_record("s1", "Builtin.Helpfulness")]))
    assert _run_out(run, snapshot=None)["token_usage"] is None  # no worker totals → null


def test_score_now_rows_carry_their_tokens():
    from app.core.db import DEFAULT_WORKSPACE_ID
    from app.services.observability import _ondemand_result
    from tests.test_strands_engine import engine_with, evaluate, ws_ctx  # noqa: F401

    ws = ws_ctx(id=DEFAULT_WORKSPACE_ID, region="cn-northwest-1")
    [record] = evaluate(engine_with(), ws, evaluator_ids=["Builtin.GoalSuccessRate"],
                        persist=False)
    assert record["token_usage"]["totalTokens"] > 0
    usage = record["token_usage"]
    assert _ondemand_result(record, ws)["token_usage"] == {
        "input": usage["inputTokens"], "output": usage["outputTokens"],
        "total": usage["totalTokens"]}  # the console's SessionDetail shape
