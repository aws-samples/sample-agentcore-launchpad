"""N17-obs418 — a healthy slow run is seen to be alive.

The worker re-writes ``status.json`` every ``HEARTBEAT_S`` (N17-obs413) but the run payload
only carried the phase, so a run judging one long session looked frozen in the console for
minutes. The poller now mirrors the newest ``updated_at`` of the run's live jobs onto
``eval_runs.last_heartbeat_at``; the run payload (and so the task detail) returns it.
"""

from __future__ import annotations

from typing import Any

import app.evaluation.service as svc
from app.evaluation.jobs import JobDone, JobSpec, JobStatus
from tests.conftest import ws_ctx
from tests.evaluation.test_resume import get_run, make_run


def _tracked(job_id: str) -> svc._Tracked:
    return svc._Tracked(job=JobSpec(job_id=job_id, job_type="batch_eval", attempt=1,
                                    workspace={}, sink={}, spec={}), sessions=[])


class Executor:
    def __init__(self, statuses: dict[str, JobStatus | None]) -> None:
        self.statuses = statuses

    def status(self, ws: Any, job_id: str) -> JobStatus | None:
        return self.statuses.get(job_id)


def _status(updated_at: str, phase: str = "evaluating") -> JobStatus:
    return JobStatus(phase=phase, updated_at=updated_at)


def test_the_poller_mirrors_the_newest_live_heartbeat():
    run_id = make_run(mode="evaluators", status="evaluating")
    executor = Executor({"j1": _status("2026-10-24T08:00:00+00:00"),
                         "j2": _status("2026-10-24T08:01:30+00:00")})
    svc._mirror_phase(run_id, ws_ctx(), executor, [_tracked("j1"), _tracked("j2")])
    assert get_run(run_id).last_heartbeat_at == "2026-10-24T08:01:30+00:00"
    # the next heartbeat moves it on; the phase is left as it is
    executor.statuses["j1"] = _status("2026-10-24T08:02:30+00:00")
    svc._mirror_phase(run_id, ws_ctx(), executor, [_tracked("j1"), _tracked("j2")])
    run = get_run(run_id)
    assert run.last_heartbeat_at == "2026-10-24T08:02:30+00:00"
    assert run.status == "evaluating"


def test_a_settled_job_no_longer_counts_and_a_missing_status_changes_nothing():
    run_id = make_run(mode="evaluators", status="evaluating")
    done = _tracked("j1")
    done.done = JobDone(state="COMPLETED")
    executor = Executor({"j1": _status("2026-10-24T09:00:00+00:00"),
                         "j2": _status("2026-10-24T08:00:00+00:00")})
    svc._mirror_phase(run_id, ws_ctx(), executor, [done, _tracked("j2")])
    assert get_run(run_id).last_heartbeat_at == "2026-10-24T08:00:00+00:00"
    svc._mirror_phase(run_id, ws_ctx(), Executor({}), [_tracked("j2")])
    assert get_run(run_id).last_heartbeat_at == "2026-10-24T08:00:00+00:00"


def test_mixed_offsets_compare_as_instants():
    run_id = make_run(mode="evaluators", status="evaluating")
    executor = Executor({"j1": _status("2026-10-24T16:00:00+08:00"),   # 08:00 UTC
                         "j2": _status("2026-10-24T08:30:00Z")})
    svc._mirror_phase(run_id, ws_ctx(), executor, [_tracked("j1"), _tracked("j2")])
    assert get_run(run_id).last_heartbeat_at == "2026-10-24T08:30:00Z"


def test_the_phase_still_follows_the_slowest_job():
    run_id = make_run(mode="evaluators", status="evaluating")
    executor = Executor({"j1": _status("2026-10-24T08:00:00+00:00", phase="invoking")})
    svc._mirror_phase(run_id, ws_ctx(), executor, [_tracked("j1")])
    run = get_run(run_id)
    assert run.status == "invoking" and run.last_heartbeat_at == "2026-10-24T08:00:00+00:00"
