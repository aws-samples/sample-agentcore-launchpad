"""N17-obs400 — a batch run over EXISTING sessions waits for their telemetry.

Live t1 (run e2caa4db5de3, 2026-10-24): a run named one session ~20 s after its chat turn with
``wait_seconds=180``; explicit sessions are ``replay: false``, the only readiness wait lived in
the replay path, so the job evaluated at once and every evaluator failed with
``SessionNotFoundError: no spans found`` — terminal, never retried. The spans had landed; Logs
Insights simply had not indexed them yet. Now explicit sessions get a readiness pass bounded by
``wait.seconds`` (old sessions pass on the first probe), an unready one becomes telemetry error
rows while the job COMPLETES, and a session the readiness probe saw but Logs Insights does not
return yet is re-fetched within the same window.
"""

from __future__ import annotations

import json
import types
from typing import Any

from app.evaluation import jobs
from app.services.strands_eval import jobs_batch
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP
from tests.evaluation.corpus_support import load_session

CORPUS = load_session("06d7a610023c")
SID = CORPUS["session_id"]
GROUP = CORPUS["log_group"]
T0 = 1_800_000_000.0
EVALUATOR = {"id": "Builtin.TrajectoryInOrderMatch"}


class Clock:
    def __init__(self) -> None:
        self.t, self.slept = T0, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += max(float(seconds), 0.001)


class TimedLogs:
    """CloudWatch over the corpus records, as seen at ``clock()``: the events exist from
    ``landed`` (``filter_log_events``, ingestion = landed), Logs Insights returns them only
    from ``indexed`` on (``None`` = never)."""

    def __init__(self, clock: Clock, landed: float | None, indexed: float | None) -> None:
        self.clock, self.landed, self.indexed = clock, landed, indexed
        self.queries: dict[str, tuple[list[str], str]] = {}

    def _visible(self, at: float | None) -> bool:
        return at is not None and self.clock() >= at

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        if not self._visible(self.landed):
            return {"events": []}
        group = kwargs["logGroupName"]
        want = SPANS_LOG_GROUP if group == SPANS_LOG_GROUP else "runtime"
        ms = int(self.landed * 1000)
        return {"events": [{"message": json.dumps(x["record"]), "timestamp": ms,
                            "ingestionTime": ms}
                           for x in CORPUS["records"] if x["group"] == want]}

    def start_query(self, logGroupNames, startTime, endTime, queryString):  # noqa: N803
        qid = str(len(self.queries))
        self.queries[qid] = (list(logGroupNames), queryString)
        return {"queryId": qid}

    def get_query_results(self, queryId):  # noqa: N803
        groups, query = self.queries[queryId]
        if not self._visible(self.indexed):
            return {"status": "Complete", "results": []}
        spans = [x["record"] for x in CORPUS["records"] if x["group"] == SPANS_LOG_GROUP]
        if query.startswith("fields spanId"):
            rows = [[{"field": "spanId", "value": r["spanId"]},
                     {"field": "parentSpanId", "value": r.get("parentSpanId", "")},
                     {"field": "traceId", "value": r["traceId"]}] for r in spans]
        else:
            rows = [[{"field": "@message", "value": json.dumps(x["record"])}]
                    for x in sorted(CORPUS["records"], key=lambda x: x["timestamp"])
                    if x["group"] == "runtime" or SPANS_LOG_GROUP in groups]
        return {"status": "Complete", "results": rows}


class Sink:
    def __init__(self) -> None:
        self.job = types.SimpleNamespace(job_id="j-ready")
        self.rows: list[dict] = []
        self.invocations: list[dict] = []
        self.statuses: list[str] = []
        self.finished: tuple | None = None

    def cancelled(self) -> bool:
        return False

    def status(self, phase: str, **_kw: Any) -> None:
        self.statuses.append(phase)

    def invocation(self, record: dict) -> None:
        self.invocations.append(record)

    def results(self, records: list[dict]) -> None:
        self.rows.extend(records)

    def result(self, record: dict) -> None:
        self.rows.append(record)

    def finish(self, state: str, error: Any = None, summary: Any = None) -> None:
        self.finished = (state, error, summary or {})


def _run(clock: Clock, logs: TimedLogs, wait_seconds: int = 180) -> Sink:
    spec = {
        "run_id": "run-ready", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": [{"session_id": SID, "scenario_id": None, "prompts": [], "replay": False}],
        "replay": False,
        "wait": {"seconds": wait_seconds, "log_groups": [GROUP, SPANS_LOG_GROUP],
                 "content_log_group": GROUP, "spans_source": "both", "lookback_hours": 48},
        "evaluator_defs": [EVALUATOR], "ground_truth": {}, "judge_model_id": "judge",
    }
    env = jobs_batch.WorkerEnv(logs_client=logs, data_client=None,
                               judge_factory=lambda m, **kw: None, sleep=clock.sleep,
                               clock=clock)
    sink = Sink()
    jobs_batch.run_job(jobs.JobSpec(job_id="j-ready", job_type="batch_eval", attempt=1,
                                    workspace={}, sink={}, spec=spec), sink, env)
    assert sink.finished is not None and sink.finished[0] == "COMPLETED", sink.finished
    return sink


def test_a_fresh_session_is_waited_for_then_scored():
    """(a) the live shape: landed 10 s after the job started, indexed by Logs Insights
    two minutes later — the job waits instead of failing on "no spans found"."""
    clock = Clock()
    sink = _run(clock, TimedLogs(clock, landed=T0 + 10, indexed=T0 + 120))
    assert [r["error_type"] for r in sink.rows] == [None]
    assert sink.rows[0]["session_id"] == SID  # judged (no reference trajectory: not applicable)
    assert "waiting" in sink.statuses and sum(clock.slept) >= 180  # the content aged 180 s
    assert sink.invocations == []


def test_a_session_whose_telemetry_never_lands_is_errored_and_the_job_completes():
    """(b) telemetry error rows, typed like the replay path's — never a failed job."""
    clock = Clock()
    sink = _run(clock, TimedLogs(clock, landed=None, indexed=None))
    [row] = sink.rows
    assert (row["score"], row["error_type"], row["error_kind"]) == (
        None, "TelemetryReadinessTimeout", jobs_batch.REPLAY_ERROR_KIND)
    assert sum(clock.slept) <= 180 + jobs_batch.TELEMETRY_READY_GRACE_SECONDS + 10
    [line] = sink.invocations
    assert line["failure"] == jobs_batch.TELEMETRY_FAILURE and line["session_id"] == SID


def test_an_old_session_is_ready_on_the_first_probe():
    """(c) a session that landed an hour ago costs no wait at all."""
    clock = Clock()
    sink = _run(clock, TimedLogs(clock, landed=T0 - 3600, indexed=T0 - 3500))
    assert [r["error_type"] for r in sink.rows] == [None]
    assert clock.slept == []


def test_a_session_the_probe_saw_but_insights_lags_on_is_re_fetched():
    """The readiness probe (filter_log_events) is ahead of Logs Insights: the fetch that
    answers "no spans found" is retried inside the run's wait window."""
    clock = Clock()
    sink = _run(clock, TimedLogs(clock, landed=T0 - 3600, indexed=T0 + 60))
    assert [r["error_type"] for r in sink.rows] == [None]
    assert 60 <= sum(clock.slept) <= 180


def test_without_a_wait_the_run_is_unchanged():
    clock = Clock()
    sink = _run(clock, TimedLogs(clock, landed=T0 + 10, indexed=T0 + 120), wait_seconds=0)
    assert [r["error_type"] for r in sink.rows] == ["SessionNotFoundError"]
    assert clock.slept == []
