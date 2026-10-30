"""N17-obs123 (cn/r17-t): the poller's artefact-store reads retry a TRANSIENT fault in place.

Live class (round 8 fault walks): one S3 `SlowDown` / `InternalError` on the poller's
`status.json` / `results.jsonl` read escaped `_poll` into `execute_run`'s `except Exception`
and the run was `failed` with the ClientError while the worker job ran on and wrote a
`done.json` nobody read; only a manual `recheck` re-attached it. Now `service._read_store`
retries with the shared `is_upstream_transient` predicate and the bounded
`STORE_READ_BACKOFF_S` through the injectable `_sleep`; a persistent fault fails the run WITH
the re-check hint; a non-transient fault (AccessDenied) fails it at once, as before.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

import app.evaluation.service as svc
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor, strip_ci
from tests.evaluation.test_resume import get_run, make_run

RECORDS = [{"session_id": "s1", "evaluator_id": "Builtin.Correctness", "score": 0.5}]


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "User: arn:aws-cn:iam::123456789012:role/hub is not "
                                            "authorized to perform: s3:GetObject"},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


class FlakyStoreExecutor:
    """`FakeExecutor` whose ``at`` read (`status` / `ingest` / `done`) raises ``exc`` for the
    first ``times`` calls (every call when ``None``)."""

    def __init__(self, inner: FakeExecutor, at: str, exc: BaseException,
                 times: int | None) -> None:
        self.inner, self.at, self.exc, self.times, self.faulted = inner, at, exc, times, 0

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if name != self.at:
            return attr

        def read(*a, **kw):
            if self.times is None or self.faulted < self.times:
                self.faulted += 1
                raise self.exc
            return attr(*a, **kw)

        return read


@pytest.fixture
def harness(monkeypatch):
    fake = FakeExecutor()
    slept: list[float] = []
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(svc, "POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(svc, "_sleep", sleep)
    monkeypatch.setattr(svc, "_now", lambda: clock["t"])
    return fake, slept, monkeypatch


def _run(fake, monkeypatch, *, at: str, exc: BaseException, times: int | None) -> tuple[str, Any]:
    flaky = FlakyStoreExecutor(fake, at, exc, times)
    monkeypatch.setattr(svc, "select_executor", lambda settings, job_type: flaky)
    run_id = make_run(status="queued", mode="evaluators")
    fake.complete(run_id, RECORDS)  # the worker job is done the moment the poller looks
    plan = svc.RunPlan(run_id=run_id, job_type="batch_eval", target={},
                       sessions=[{"session_id": "s1"}], replay=False, wait={})
    svc.execute_run(run_id, workspace=ws_ctx(), plan=plan)
    return run_id, flaky


Any = object


@pytest.mark.parametrize("at", ["status", "ingest", "done"])
@pytest.mark.parametrize("code, status", [("SlowDown", 503), ("InternalError", 500)])
def test_one_transient_read_fault_is_retried_and_the_run_completes(harness, at, code, status):
    fake, slept, monkeypatch = harness
    run_id, flaky = _run(fake, monkeypatch, at=at, exc=_client_error(code, status), times=1)
    run = get_run(run_id)
    assert run.status == "completed" and run.error is None, (run.status, run.error)
    assert flaky.faulted == 1
    assert slept[0] == svc.STORE_READ_BACKOFF_S[0]  # the one backoff wait, then the poll sleeps
    assert strip_ci(run.scores) == [{"evaluatorId": "Builtin.Correctness", "score": 0.5,
        "count": 1, "n": 1, "unit": "turn"}]


def test_a_persistent_transient_fault_fails_the_run_with_the_recheck_hint(harness):
    fake, slept, monkeypatch = harness
    run_id, flaky = _run(fake, monkeypatch, at="status", exc=_client_error("SlowDown", 503),
                         times=None)
    run = get_run(run_id)
    assert run.status == "failed", run.status
    assert flaky.faulted == len(svc.STORE_READ_BACKOFF_S) + 1  # bounded: one more than waits
    assert slept[:len(svc.STORE_READ_BACKOFF_S)] == list(svc.STORE_READ_BACKOFF_S)
    assert run.error.startswith(
        f"RuntimeError: {svc.STORE_READ_FAILED_PREFIX} (status.json) after 4 attempts: "
        "ClientError: SlowDown (GetObject)"
    ), run.error
    assert "re-check the run" in run.error
    for marker in ("is not authorized", "An error occurred", "arn:"):
        assert marker not in run.error, marker  # redacted_error: type + code + operation only
    assert run.batch_eval_id == run_id  # recheck_run can re-attach the finished job


def test_a_non_transient_read_fault_fails_the_run_at_once(harness):
    fake, slept, monkeypatch = harness
    run_id, flaky = _run(fake, monkeypatch, at="status", exc=_client_error("AccessDenied", 403),
                         times=None)
    run = get_run(run_id)
    assert run.status == "failed" and flaky.faulted == 1
    assert slept == []  # no backoff for a fault a retry cannot outlive
    assert run.error.startswith("ClientError:")


def test_recheck_settles_the_run_the_persistent_fault_failed(harness):
    """The hint is honest: once the store answers again, `recheck_run` re-attaches by
    `batch_eval_id` and settles from the finished job (evaluation-engine.md §5)."""
    fake, _slept, monkeypatch = harness
    run_id, _flaky = _run(fake, monkeypatch, at="ingest", exc=_client_error("SlowDown", 503),
                          times=None)
    assert get_run(run_id).status == "failed"
    monkeypatch.setattr(svc, "select_executor", lambda settings, job_type: fake)  # healthy again
    run = svc.recheck_run(run_id, workspace=ws_ctx())
    assert run.status in ("evaluating", "completed")
    from tests.evaluation.test_resume import wait_status

    run = wait_status(run_id, "completed")
    assert run.error is None and run.scores
