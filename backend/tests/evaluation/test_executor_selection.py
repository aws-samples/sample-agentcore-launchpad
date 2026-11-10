"""N12 E0-5 — CA-8 tightening: `eval_executor="inprocess"` is a dev / hermetic / SCORE NOW
affordance; in run_mode=prod every job submit path refuses it with 409
`evaluation.executor_not_allowed`; the reserved Skill-Lab kinds answer 400
`evaluation.kind_unsupported` before any dispatch; no worker → 503."""

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.evaluation import executor as ex
from app.evaluation import jobs
from tests.evaluation.fakes import FakeExecutor

BATCH_KINDS = ("batch_eval", "online_eval", "insights", "recommend", "optimize")


@pytest.fixture(autouse=True)
def _reset():
    ex.set_runtime_executor(None)
    ex.set_inprocess_executor(None)
    yield
    ex.set_runtime_executor(None)
    ex.set_inprocess_executor(None)


@pytest.mark.parametrize("kind", BATCH_KINDS)
def test_prod_refuses_inprocess_for_every_batch_kind(kind):
    settings = Settings(run_mode="prod", eval_executor="inprocess")
    with pytest.raises(AppError) as exc:
        ex.select_executor(settings, job_type=kind)
    assert exc.value.code == "evaluation.executor_not_allowed" and exc.value.status_code == 409


def test_dev_allows_inprocess_and_builds_the_default_runner():
    settings = Settings(run_mode="dev", eval_executor="inprocess")
    chosen = ex.select_executor(settings, job_type="batch_eval")
    assert isinstance(chosen, ex.InProcessExecutor)
    assert ex.select_executor(settings, job_type="insights") is chosen  # one per process


def test_score_now_may_run_in_process_even_in_prod():
    settings = Settings(run_mode="prod", eval_executor="inprocess")
    assert isinstance(ex.select_executor(settings, job_type=ex.ONDEMAND), ex.InProcessExecutor)
    assert ex.inprocess_allowed(settings, ex.ONDEMAND) is True
    assert ex.inprocess_allowed(settings, "batch_eval") is False


def test_runtime_default_uses_the_registered_worker_executor_or_503():
    settings = Settings(run_mode="prod")  # eval_executor defaults to "runtime"
    with pytest.raises(AppError) as exc:
        ex.select_executor(settings, job_type="batch_eval")
    assert exc.value.code == "evaluation.worker_missing" and exc.value.status_code == 503
    fake = FakeExecutor()
    ex.set_runtime_executor(fake)
    assert ex.select_executor(settings, job_type="batch_eval") is fake
    # SCORE NOW never goes to the worker (CA-8): it stays in-process regardless
    assert isinstance(ex.select_executor(settings, job_type=ex.ONDEMAND), ex.InProcessExecutor)


@pytest.mark.parametrize("kind", jobs.RESERVED_JOB_TYPES)
def test_reserved_kinds_are_refused_before_dispatch(kind):
    for settings in (Settings(run_mode="dev", eval_executor="inprocess"),
                     Settings(run_mode="prod")):
        with pytest.raises(AppError) as exc:
            ex.select_executor(settings, job_type=kind)
        assert exc.value.code == "evaluation.kind_unsupported"
        assert exc.value.status_code == 400


def test_inprocess_executor_runs_the_runner_against_the_s3_layout(workspace):
    """The dev executor and the worker share one artefact protocol: status.json while
    running, results.jsonl flushed per record, done.json last (R-18)."""
    seen: list[str] = []

    def runner(job: jobs.JobSpec, sink: ex.JobSink) -> None:
        sink.status("invoking", done=0, total=2, message="replaying")
        sink.result({"session_id": "s1", "evaluator_id": "e", "score": 1.0})
        sink.status("evaluating", done=1, total=2, message="judging")
        sink.result({"session_id": "s2", "evaluator_id": "e", "score": 0.0})
        seen.append("ran")
        sink.finish("COMPLETED", summary={"scores": [{"evaluatorId": "e", "score": 0.5,
                                                      "count": 2}]})

    store = ex.MemoryStore()
    runner_ex = ex.InProcessExecutor(store, runner=runner)
    job = jobs.JobSpec(job_id="j1", job_type="batch_eval",
                       workspace={"account_id": "1", "region": "cn-northwest-1"},
                       sink={"bucket": "b", "prefix": jobs.job_prefix("j1", 1)}, spec={})
    assert runner_ex.submit(workspace, job) == "j1"
    assert runner_ex.wait("j1", timeout_s=5)
    assert seen == ["ran"]
    status = runner_ex.status(workspace, "j1")
    assert status is not None and status.state == "COMPLETED"
    done = runner_ex.done(workspace, "j1")
    assert done is not None and done.result_count == 2 and done.summary["scores"][0]["count"] == 2
    records = runner_ex.ingest(workspace, "j1", since=0)
    assert [r["session_id"] for r in records] == ["s1", "s2"]
    assert [r["session_id"] for r in runner_ex.ingest(workspace, "j1", since=1)] == ["s2"]
    assert all("id" in r for r in records)  # record_id stamped
    assert store.get(jobs.done_key("j1", 1)) is not None
    assert store.get(jobs.status_key("j1", 1)) is not None


def test_inprocess_cancel_writes_the_marker_and_the_runner_sees_it(workspace):
    import threading

    gate = threading.Event()

    def runner(job, sink):
        gate.wait(5)
        sink.status("evaluating", done=0, total=1, message="x")
        if sink.cancelled():
            sink.finish("STOPPED")
            return
        sink.finish("COMPLETED")

    store = ex.MemoryStore()
    runner_ex = ex.InProcessExecutor(store, runner=runner)
    job = jobs.JobSpec(job_id="j2", job_type="insights",
                       workspace={"account_id": "1", "region": "cn-northwest-1"},
                       sink={"bucket": "b", "prefix": jobs.job_prefix("j2", 1)}, spec={})
    runner_ex.submit(workspace, job)
    runner_ex.cancel(workspace, "j2")
    assert store.get(jobs.cancel_key("j2")) is not None
    gate.set()
    assert runner_ex.wait("j2", timeout_s=5)
    assert runner_ex.done(workspace, "j2").state == "STOPPED"


def test_runner_exception_becomes_a_failed_done(workspace):
    def runner(job, sink):
        raise RuntimeError("judge exploded")

    runner_ex = ex.InProcessExecutor(ex.MemoryStore(), runner=runner)
    job = jobs.JobSpec(job_id="j3", job_type="batch_eval",
                       workspace={"account_id": "1", "region": "cn-northwest-1"},
                       sink={"bucket": "b", "prefix": jobs.job_prefix("j3", 1)}, spec={})
    runner_ex.submit(workspace, job)
    assert runner_ex.wait("j3", timeout_s=5)
    done = runner_ex.done(workspace, "j3")
    assert done.state == "FAILED" and "judge exploded" in (done.error or "")


def test_s3_store_adapter_uses_the_injected_client():
    from tests.evaluation.fakes import FakeS3

    s3 = FakeS3()
    store = ex.S3Store(s3, "bucket")
    store.put("evaluations/j/a1/status.json", b"{}")
    assert store.get("evaluations/j/a1/status.json") == b"{}"
    assert store.list("evaluations/j/") == ["evaluations/j/a1/status.json"]
    store.delete("evaluations/j/a1/status.json")
    assert store.get("evaluations/j/a1/status.json") is None


# ─── N12-bug6: submission-time readiness probe ────────────────────────────────────
def test_assert_executor_ready_probes_the_executor_for_the_workspace(workspace):
    """A submit path asks the chosen executor whether the workspace is ready for it
    (the worker executor re-reads the preset row) — a refusal is the request's 503."""
    seen: list[object] = []

    class Probing(FakeExecutor):
        def assert_ready(self, ws):
            seen.append(ws)
            raise AppError("evaluation.worker_missing", "gone", {"worker_status": "not_installed"},
                           status_code=503)

    settings = Settings(run_mode="prod")
    probing = Probing()
    ex.set_runtime_executor(probing)
    assert ex.select_executor(settings, job_type="batch_eval") is probing  # no probe yet
    assert seen == []
    with pytest.raises(AppError) as exc:
        ex.assert_executor_ready(probing, workspace)
    assert exc.value.code == "evaluation.worker_missing" and seen == [workspace]
    # executors without a probe (in-process, fakes) are always ready
    ex.assert_executor_ready(FakeExecutor(), workspace)
    ex.assert_executor_ready(ex.inprocess_executor(), workspace)
