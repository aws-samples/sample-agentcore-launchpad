"""N17-obs417 — a failed or lost run reports the judge tokens it spent.

``token_usage`` was folded from ``done.json`` only, so it was null on every FAILED run — and the
obs413 job-lost runs had spent up to three attempts' worth of judge calls: exactly the runaway
case the accounting is for. The worker now writes a per-attempt usage sidecar
(``usage.json``) as every session finishes; the hub folds the sidecars of EVERY attempt of
every job (a job without sidecars — an older worker — falls back to its ``done.json``) when a
run settles, and also when a run fails before settling (lost jobs, the hub's wait expiring).
"""

from __future__ import annotations

import json

import app.evaluation.service as svc
from app.evaluation import jobs
from app.evaluation.jobs import JobDone
from app.services.strands_eval import jobs_batch
from app.services.strands_eval.sink import JobSink, MemoryStore, StoreReader
from tests.conftest import ws_ctx
from tests.evaluation.test_resume import get_run, make_run
from tests.evaluation.test_token_usage import _batch  # noqa: F401  (fixture shapes)

USAGE = jobs_batch.USAGE_ARTIFACT


def _sidecar(store: MemoryStore, job_id: str, attempt: int, total: int) -> None:
    store.put(jobs.artifact_key(job_id, attempt, USAGE), json.dumps(
        {"inputTokens": total - 1, "outputTokens": 1, "totalTokens": total}).encode())


def test_the_worker_writes_the_usage_sidecar_as_sessions_finish():
    from app.evaluation.jobs import JobSpec
    from tests.evaluation.fakes import FakeJudge
    from tests.test_strands_engine import SID, logs_with

    store = MemoryStore()
    job = JobSpec(job_id="j-side", job_type="batch_eval", attempt=2, workspace={}, sink={},
                  spec={"run_id": "r", "target": {}, "replay": False,
                        "sessions": [{"session_id": SID, "replay": False, "prompts": []}],
                        "wait": {"seconds": 0, "log_groups": ["/g"],
                                 "spans_source": "runtime_logs", "lookback_hours": 48},
                        "evaluator_defs": [{"id": "Builtin.GoalSuccessRate"}],
                        "ground_truth": {}, "judge_model_id": "j"})
    env = jobs_batch.WorkerEnv(logs_client=logs_with(SID), data_client=None,
                               judge_factory=lambda m, **kw: FakeJudge(), sleep=lambda s: None)
    jobs_batch.run_job(job, JobSink(store, job), env)
    side = json.loads(store.get(jobs.artifact_key("j-side", 2, USAGE)))
    assert side["totalTokens"] > 0


def test_the_reader_lists_one_sidecar_per_attempt():
    store = MemoryStore()
    _sidecar(store, "j1", 1, 30)
    _sidecar(store, "j1", 3, 7)
    assert [s["totalTokens"] for s in StoreReader(store).attempt_artifacts("j1", USAGE)] == [30, 7]


class Executor:
    def __init__(self, store: MemoryStore) -> None:
        self._reader = StoreReader(store)

    def attempt_artifacts(self, ws, job_id, name):
        return self._reader.attempt_artifacts(job_id, name)

    def artifact(self, ws, job_id, name):
        return None


def test_a_failed_run_folds_every_attempts_usage():
    store = MemoryStore()
    run_id = make_run(mode="evaluators", status="evaluating", evaluators=["Builtin.Helpfulness"],
                      session_ids=["s1"])
    _sidecar(store, run_id, 1, 40)  # the attempt the hub thought was lost
    _sidecar(store, run_id, 2, 25)  # its re-dispatch
    done = JobDone(state="FAILED", error="job lost after attempt 3", result_count=0)
    svc._finish(run_id, "evaluators", [done], workspace=ws_ctx(), executor=Executor(store),
                job_ids=[run_id])
    run = get_run(run_id)
    assert run.status == "failed"
    assert run.token_usage["totalTokens"] == 65 and run.token_usage["inputTokens"] == 63


def test_a_job_without_sidecars_keeps_its_done_totals():
    store = MemoryStore()
    run_id = make_run(mode="evaluators", status="evaluating", evaluators=["Builtin.Helpfulness"],
                      session_ids=["s1"])
    done = JobDone(state="COMPLETED", result_count=0, summary={"token_usage": {
        "inputTokens": 8, "outputTokens": 2, "totalTokens": 10}})
    svc._finish(run_id, "evaluators", [done], workspace=ws_ctx(), executor=Executor(store),
                job_ids=[run_id])
    assert get_run(run_id).token_usage["totalTokens"] == 10


def test_a_run_that_fails_before_settling_still_records_its_usage():
    store = MemoryStore()
    run_id = make_run(mode="evaluators", status="evaluating", evaluators=["Builtin.Helpfulness"],
                      session_ids=["s1"])
    _sidecar(store, run_id, 1, 12)
    svc.record_usage_on_failure(run_id, workspace=ws_ctx(), executor=Executor(store),
                                job_ids=[run_id])
    assert get_run(run_id).token_usage["totalTokens"] == 12
