"""N17-obs413 — a live evaluation job is never declared lost while it is still judging.

Live (runs 7a5086c3bd86, bdd265252e9f): the worker wrote ``status.json`` once per FINISHED
session and the hub calls a job stale after ``eval_job_stale_s`` (600 s) without a write, so
one slow session looked dead; the hub re-dispatched it while attempt 1 kept running (a1
10:35→11:21 COMPLETED, a2 from 10:48, a3 from 11:05 — three attempts at once), and the reader
only ever looked at the newest attempt, so a1's result was ignored. Now:

* the worker heartbeats: ``JobSink`` re-writes the last status every ``HEARTBEAT_S`` while
  the job runs (inside a session, a judge call, a replay or a readiness wait alike), so
  staleness means the worker is really gone;
* before a re-dispatch the hub stops the stale attempt's worker session — two attempts of
  one job never run at once;
* the reader takes the result of whichever attempt finished: a done earlier attempt wins over
  a newer one still running (its results and status included).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import app.evaluation.service as svc
from app.evaluation import jobs
from app.evaluation.jobs import JobDone, JobSpec, JobStatus
from app.services.strands_eval import jobs_batch
from app.services.strands_eval.sink import JobSink, MemoryStore, StoreReader


def _job(attempt: int = 1) -> JobSpec:
    return JobSpec(job_id="job-live", job_type="batch_eval", attempt=attempt, workspace={},
                   sink={}, spec={})


def _updated(store: MemoryStore, attempt: int = 1) -> str:
    return json.loads(store.get(jobs.status_key("job-live", attempt)))["updated_at"]


def test_the_sink_heartbeats_the_last_status_until_the_job_finishes():
    store = MemoryStore()
    sink = JobSink(store, _job())
    sink.status("evaluating", done=1, total=3, message="evaluating 2/3")
    first = _updated(store)
    sink.start_heartbeat(0.05)
    time.sleep(0.3)
    beat = json.loads(store.get(jobs.status_key("job-live", 1)))
    assert beat["updated_at"] > first
    assert (beat["phase"], beat["progress"]["message"]) == ("evaluating", "evaluating 2/3")
    sink.finish("COMPLETED")
    stopped = _updated(store)
    time.sleep(0.2)
    assert _updated(store) == stopped  # no beat after finish


def test_every_job_runs_with_a_heartbeat():
    started: list[float] = []

    class Sink:
        job = SimpleNamespace(job_id="j")

        def start_heartbeat(self, interval: float) -> None:
            started.append(interval)

        def finish(self, *a: Any, **kw: Any) -> None:
            pass

    jobs_batch.run_job(JobSpec(job_id="j", job_type="nope_kind_x", attempt=1, workspace={},
                               sink={}, spec={}), Sink(), SimpleNamespace())
    assert started == [jobs_batch.HEARTBEAT_S]
    assert jobs_batch.HEARTBEAT_S * 5 < 600  # well inside eval_job_stale_s


def test_the_stale_attempt_is_stopped_before_a_new_one_starts():
    calls: list[tuple[str, Any]] = []

    class Executor:
        def invocations(self, ws, job_id):
            return []

        def stop_session(self, ws, job_id):
            calls.append(("stop", job_id))
            return True

        def submit(self, ws, job):
            calls.append(("submit", job.attempt))

    plan = svc.RunPlan(run_id="r", job_type="batch_eval", target={}, sessions=[], replay=False,
                       wait={}, evaluator_defs=[], ground_truth={}, judge_model_id="j")
    item = svc._Tracked(job=_job(1), sessions=[])
    svc._redispatch("r", SimpleNamespace(id="w", account_id="1", region="cn-northwest-1",
                                         resources={}), Executor(), item, plan)
    assert calls == [("stop", "job-live"), ("submit", 2)]


def _put_done(store: MemoryStore, attempt: int, results: list[dict]) -> None:
    store.put(jobs.results_key("job-live", attempt),
              ("\n".join(json.dumps(r) for r in results) + "\n").encode())
    store.put(jobs.done_key("job-live", attempt), json.dumps(JobDone(
        state="COMPLETED", result_count=len(results)).to_dict()).encode())


def test_a_finished_earlier_attempt_wins_over_a_newer_one_still_running():
    store = MemoryStore()
    rows = [{"id": "x1", "session_id": "s1"}, {"id": "x2", "session_id": "s2"}]
    _put_done(store, 1, rows)
    store.put(jobs.status_key("job-live", 2),
              json.dumps(JobStatus(phase="evaluating").to_dict()).encode())
    reader = StoreReader(store)
    assert reader.done("job-live").state == "COMPLETED"
    assert reader.status("job-live").state == "COMPLETED"
    assert [r["id"] for r in reader.ingest("job-live", since=0)] == ["x1", "x2"]


def test_the_newest_attempt_still_wins_when_it_finished():
    store = MemoryStore()
    _put_done(store, 1, [{"id": "old"}])
    _put_done(store, 2, [{"id": "new"}])
    assert [r["id"] for r in StoreReader(store).ingest("job-live")] == ["new"]


def test_a_job_with_a_running_newest_attempt_and_no_done_reads_that_attempt():
    store = MemoryStore()
    store.put(jobs.status_key("job-live", 1),
              json.dumps(JobStatus(phase="evaluating").to_dict()).encode())
    store.put(jobs.status_key("job-live", 2),
              json.dumps(JobStatus(phase="waiting").to_dict()).encode())
    assert StoreReader(store).status("job-live").phase == "waiting"
    assert StoreReader(store).done("job-live") is None


def test_the_hub_reads_the_finished_attempts_results_whole():
    """A done earlier attempt after a re-dispatch: the offset counted the newer attempt's
    lines, so the hub re-reads the finished attempt from the start (upsert dedupes)."""
    store = MemoryStore()
    rows = [{"id": f"r{i}", "session_id": f"s{i}", "evaluator_id": "E", "level": "SESSION",
             "score": 1.0} for i in range(3)]
    _put_done(store, 1, rows)
    store.put(jobs.status_key("job-live", 2),
              json.dumps(JobStatus(phase="evaluating").to_dict()).encode())
    reader = StoreReader(store)
    seen: list[int] = []

    class Executor:
        def status(self, ws, job_id):
            return reader.status(job_id)

        def done(self, ws, job_id):
            return reader.done(job_id)

        def ingest(self, ws, job_id, since=0):
            seen.append(since)
            return reader.ingest(job_id, since)

    item = svc._Tracked(job=_job(2), sessions=[], ingested=2)
    persisted: list[int] = []
    original = svc._ingest

    def counting_ingest(run_id, workspace, executor, tracked):
        before = tracked.ingested
        original(run_id, workspace, executor, tracked)
        persisted.append(tracked.ingested - before)

    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(svc, "_ingest", counting_ingest)
    mp.setattr(svc.results_store, "persist", lambda *a, **kw: len(kw.get("records") or []))
    mp.setattr(svc, "_finish", lambda *a, **kw: None)
    mp.setattr(svc, "_mirror_phase", lambda *a, **kw: None)
    try:
        svc._poll("run-x", workspace=SimpleNamespace(id="w"), executor=Executor(),
                  tracked=[item], plan=None)
    finally:
        mp.undo()
    assert item.done is not None and item.done.state == "COMPLETED"
    assert seen[-1] == 0 and persisted[-1] == 3  # all of attempt 1's rows
