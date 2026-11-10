"""N-SCN-bug1 — a scenario that fails to replay is an ERRORED SCENARIO, not a failed run.

Live (scenario counting run on default): the cold first turn of the heavy agent took longer
than botocore's default 60 s read timeout; ``ReadTimeoutError`` was not classified as
transient, so the scenario got ONE attempt while the message said "failed after 2 replays",
and ``_replay_dataset`` aborted the WHOLE run on that first failed scenario. Now botocore's
read / connect / endpoint errors are transient, the attempt count and the message agree,
the run continues with the remaining scenarios (the errored session gets one error record
per evaluator with ``error_kind: infrastructure`` — the hub's N12-bug4 verdict then reads
completed-with-errors) and only a run in which EVERY scenario failed is FAILED, with the
first error as its reason. The insights prologue shares the behaviour; cancel still stops
at once.
"""

from __future__ import annotations

import types

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.evaluation import jobs, telemetry
from app.services.strands_eval import jobs_batch, runtime_invoke

S1, S2, S3 = ("sess-1" + "x" * 30), ("sess-2" + "x" * 30), ("sess-3" + "x" * 30)
N = jobs_batch.TRANSIENT_SCENARIO_ATTEMPTS


def _timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url="https://bedrock-agentcore.cn-northwest-1.test")


class Sink:
    def __init__(self, cancelled: bool = False):
        self.job = types.SimpleNamespace(job_id="j-replay")
        self.statuses: list[tuple[str, int, str]] = []
        self.invocations: list[dict] = []
        self.result_rows: list[dict] = []
        self.artifacts: dict[str, object] = {}
        self.finished: tuple[str, str | None, dict] | None = None
        self._cancelled = cancelled

    def cancelled(self) -> bool:
        return self._cancelled

    def status(self, phase, *, done, total, message):
        self.statuses.append((phase, done, message))

    def invocation(self, record):
        self.invocations.append(record)

    def artifact(self, name, blob):
        self.artifacts[name] = blob

    def results(self, records):
        self.result_rows.extend(records)

    def finish(self, state, error=None, summary=None):
        self.finished = (state, error, summary or {})


class FakeRuntime:
    """``env.invoke`` stand-in: per session a queue of outcomes — an exception instance to
    raise, or the text to answer; the queue's last entry repeats."""

    def __init__(self, plan: dict[str, list]):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, prompt, session_id, session_spec):
        self.calls.append((session_id, prompt))
        queue = self.plan.get(session_id) or ["ok"]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return {"text": outcome, "session_id": session_id}


def _spec(**overrides) -> dict:
    sessions = [
        {"session_id": sid, "scenario_id": f"s{i}", "prompts": ["hi"], "replay": True,
         "arm": None, "qualifier": None, "baggage": None, "launchpad_meta": None}
        for i, sid in enumerate((S1, S2, S3), 1)
    ]
    spec = {
        "run_id": "run-1", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": sessions, "replay": True,
        "wait": {"seconds": 0, "log_groups": ["/aws/launchpad/runtime", "aws/spans"],
                 "content_log_group": "/aws/launchpad/runtime", "spans_source": "runtime_logs",
                 "lookback_hours": 48},
        "evaluator_defs": [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.Correctness"}],
        "ground_truth": {}, "judge_model_id": "judge-model", "service_name": "svc",
        "kinds": ["failure_categorization"],
    }
    spec.update(overrides)
    return spec


def _job(spec: dict, job_type: str = "batch_eval") -> jobs.JobSpec:
    return jobs.JobSpec(job_id=f"j-{job_type}", job_type=job_type, attempt=1, workspace={},
                        sink={"bucket": "b", "prefix": f"evaluations/j-{job_type}/a1"},
                        spec=spec)


@pytest.fixture
def harness(monkeypatch):
    """Spans are always ready; the engine scores every session it is given 0.9 per
    evaluator and records WHICH sessions it saw; the wait records WHICH session it probed."""
    probed: list[str] = []

    def probe(client, **kwargs):
        probed.append(kwargs["session_id"])
        return telemetry.TelemetryProbe(root_span_id="root", content_ingestion_ms=0)

    monkeypatch.setattr(telemetry, "probe_latest_session", probe)
    evaluated: list[list[str]] = []
    cutoffs: list[dict[str, int]] = []

    def evaluate_sessions(provider, session_ids, evaluators, truth, *, job_id, sleep,
                          stop=None, progress=None, on_records=None, concurrency=4, **_kw):
        evaluated.append(list(session_ids))
        cutoffs.append(dict(_kw.get("not_before") or {}))
        out = []
        for sid in session_ids:
            recs = [{"session_id": sid, "evaluator_id": d["id"], "level": "TRACE",
                     "score": 0.9, "label": "pass", "explanation": "fine",
                     "error_type": None, "error_message": None}
                    for d in evaluators.definitions]
            if on_records:
                on_records(sid, recs)
            if progress:
                progress(f"evaluating {len(out) + 1}/{len(session_ids)}")
            out.extend(recs)
        return out

    monkeypatch.setattr(jobs_batch.engine_core, "evaluate_sessions", evaluate_sessions)
    diagnosed: list[list[str]] = []

    def diagnose(provider, session_ids, kinds, judge, *, stop=None, progress=None, **_kw):
        diagnosed.append(list(session_ids))
        for index, _sid in enumerate(session_ids, 1):  # the real one reports per session
            if progress:
                progress(f"analysing {index}/{len(session_ids)}")
        return {"kinds": kinds, "clusters": [], "errors": []}

    monkeypatch.setattr(jobs_batch.insights, "diagnose_sessions", diagnose)

    def make(plan: dict[str, list]) -> types.SimpleNamespace:
        runtime = FakeRuntime(plan)
        env = jobs_batch.WorkerEnv(
            logs_client=object(), data_client=None, judge_factory=lambda m, **kw: f"judge:{m}",
            default_model_id="default-judge", invoke=runtime, spans_source="runtime_logs",
            sleep=lambda s: None, clock=lambda: 1_000.0,
        )
        return types.SimpleNamespace(env=env, runtime=runtime, probed=probed,
                                     evaluated=evaluated, diagnosed=diagnosed,
                                     cutoffs=cutoffs)

    return make


# ─── classification ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("exc", [
    ReadTimeoutError(endpoint_url="u"),
    ConnectTimeoutError(endpoint_url="u"),
    EndpointConnectionError(endpoint_url="u"),
    ClientError({"Error": {"Code": "throttlingException"}, "ResponseMetadata": {}}, "Invoke"),
    ClientError({"Error": {"Code": "x"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "Invoke"),
    RuntimeError("runtime client error: upstream"),
])
def test_botocore_timeouts_and_connection_failures_are_transient_like_throttling(exc):
    assert runtime_invoke.is_transient_invoke_error(exc) is True


@pytest.mark.parametrize("exc", [
    ValueError("the agent is gone"),
    ClientError({"Error": {"Code": "ValidationException"},
                 "ResponseMetadata": {"HTTPStatusCode": 400}}, "Invoke"),
    RuntimeError("runtime returned error: bad prompt"),
])
def test_caller_errors_stay_non_transient(exc):
    assert runtime_invoke.is_transient_invoke_error(exc) is False


# ─── (a) scenario 2 of 3 times out twice, recovers on the last attempt ───────────────
def test_a_scenario_that_recovers_on_retry_completes_the_run_3_of_3(harness):
    h = harness({S2: [_timeout(), _timeout(), "ok"]})
    sink = Sink()

    jobs_batch.run_job(_job(_spec()), sink, h.env)

    # scenario 2 was attempted N times in the SAME pre-assigned session; the others once
    assert [c[0] for c in h.runtime.calls] == [S1, S2, S2, S2, S3]
    assert [r["status"] for r in sink.invocations] == ["ok", "ok", "ok"]
    assert sink.invocations[1]["retries"] == N - 1 and "error_kind" not in sink.invocations[1]
    # the retry was announced with the attempt it was about to make, k/N
    retry_msgs = [m for _p, _d, m in sink.statuses if "attempt" in m]
    assert retry_msgs == [
        f"replaying 2/3: scenario s2 attempt 2/{N} after ReadTimeoutError",
        f"replaying 2/3: scenario s2 attempt 3/{N} after ReadTimeoutError",
    ]
    # all three sessions were evaluated and scored; nothing errored
    assert h.evaluated == [[S1, S2, S3]]
    assert sink.finished is not None and sink.finished[0] == "COMPLETED"
    assert sink.finished[2]["scores"] == [
        {"evaluatorId": "Builtin.Correctness", "score": 0.9, "count": 3, "n": 3, "unit": "turn"},
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.9, "count": 3, "n": 3, "unit": "turn"},
    ]
    assert "replay_failures" not in sink.finished[2]  # untouched summary stays byte-identical
    # N17-obs323: the retried session reused its id — only its last attempt's traces count
    assert sink.invocations[1]["last_attempt_started_ms"] == 1_000_000
    assert "last_attempt_started_ms" not in sink.invocations[0]
    assert h.cutoffs == [{S2: 1_000_000}]


# ─── (b) scenario 2 of 3 keeps timing out: 2 scored + 1 errored, run COMPLETED ────────
def test_a_scenario_that_keeps_failing_is_errored_and_the_run_continues(harness):
    h = harness({S2: [_timeout()]})
    sink = Sink()

    jobs_batch.run_job(_job(_spec()), sink, h.env)

    assert [c[0] for c in h.runtime.calls] == [S1] + [S2] * N + [S3]
    assert [r["status"] for r in sink.invocations] == ["ok", "failed", "ok"]
    errored = sink.invocations[1]
    # the invocation line: the error, the kind, and an attempt count that matches reality
    assert errored["code"] == "ReadTimeoutError" and "Read timeout" in errored["error"]
    assert errored["error_kind"] == "infrastructure"
    assert errored["attempts"] == N and errored["retries"] == N - 1
    # the engine only saw the sessions that answered …
    assert h.evaluated == [[S1, S3]]
    # … the newest ANSWERED session was the one waited on (S3, not the failed S2)
    assert h.probed == [S3]
    # … and the errored session got one error record per evaluator, scored as invalid
    bad = [r for r in sink.result_rows if r["session_id"] == S2]
    assert {r["evaluator_id"] for r in bad} == {"Builtin.Helpfulness", "Builtin.Correctness"}
    assert all(r["score"] is None and r["error_type"] == "ReadTimeoutError" for r in bad)
    assert all(r["error_kind"] == "infrastructure" for r in bad)
    assert all(f"scenario s2 failed after {N}/{N} attempts: ReadTimeoutError" in r["error_message"]
               for r in bad)
    assert all(r["id"] for r in bad)
    good = [r for r in sink.result_rows if r["session_id"] != S2]
    assert len(good) == 4 and all(r["score"] == 0.9 for r in good)
    # the run completed; the summary carries the scored sessions AND the failure
    state, error, summary = sink.finished
    assert state == "COMPLETED" and error is None
    assert summary["scores"] == [
        {"evaluatorId": "Builtin.Correctness", "score": 0.9, "count": 2, "n": 2, "unit": "turn"},
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.9, "count": 2, "n": 2, "unit": "turn"},
    ]
    assert summary["replay_failures"] == [{
        "session_id": S2, "scenario_id": "s2", "code": "ReadTimeoutError", "attempts": N,
        "error": errored["error"], "arm": None,  # arm: N12-obs62 (a plain run has none)
    }]
    # progress reads 3/3 at the end and the invoking status counted the failure
    assert ("invoking", 3, "replaying 3/3 (1 failed)") in sink.statuses
    assert sink.statuses[-1] == ("evaluating", 3, "evaluating 3/3")


def test_a_non_transient_error_gets_one_attempt_and_the_count_says_so(harness):
    h = harness({S2: [ValueError("the agent is gone")]})
    sink = Sink()

    jobs_batch.run_job(_job(_spec()), sink, h.env)

    assert [c[0] for c in h.runtime.calls] == [S1, S2, S3]  # no retry on a caller error
    errored = sink.invocations[1]
    assert errored["status"] == "failed" and errored["code"] == "ValueError"
    assert errored["attempts"] == 1 and errored["retries"] == 0
    bad = [r for r in sink.result_rows if r["session_id"] == S2]
    assert all(f"failed after 1/{N} attempts: ValueError: the agent is gone"
               in r["error_message"] for r in bad)
    assert sink.finished[0] == "COMPLETED"


# ─── (c) every scenario fails: the run is FAILED with the first error ─────────────────
def test_when_every_scenario_fails_the_run_fails_with_the_first_error(harness):
    h = harness({S1: [_timeout()], S2: [ValueError("the agent is gone")], S3: [_timeout()]})
    sink = Sink()

    with pytest.raises(RuntimeError) as info:
        jobs_batch.run_job(_job(_spec()), sink, h.env)

    message = str(info.value)
    assert message.startswith("all 3 scenarios failed to replay — first error: ")
    assert f"scenario s1 failed after {N}/{N} attempts: ReadTimeoutError" in message
    # every scenario was still attempted (and recorded) before the verdict
    assert [r["status"] for r in sink.invocations] == ["failed"] * 3
    assert [r["attempts"] for r in sink.invocations] == [N, 1, N]
    # nothing was waited on or evaluated; no done.json yet (the host writes FAILED)
    assert h.probed == [] and h.evaluated == [] and sink.result_rows == []
    assert sink.finished is None


# ─── insights shares the prologue ────────────────────────────────────────────────────
def test_the_insights_prologue_continues_past_an_errored_scenario(harness):
    h = harness({S2: [_timeout()]})
    sink = Sink()

    jobs_batch.run_job(_job(_spec(), "insights"), sink, h.env)

    assert [r["status"] for r in sink.invocations] == ["ok", "failed", "ok"]
    assert h.probed == [S3]
    # the detectors ran over the answered sessions only; the errored one is listed under
    # `errors` in the detectors' own `{session_id, error: "Type: message"}` shape
    assert h.diagnosed == [[S1, S3]]
    blob = sink.artifacts["insights.json"]
    [err] = blob["errors"]
    assert err["session_id"] == S2 and err["error_kind"] == "infrastructure"
    assert err["error"].startswith(f"ReadTimeoutError: scenario s2 failed after {N}/{N} attempts")
    state, _error, summary = sink.finished
    assert state == "COMPLETED" and summary["insights"] is blob
    assert summary["replay_failures"][0]["session_id"] == S2
    assert sink.statuses[-1] == ("evaluating", 3, "analysing 3/3")


def test_the_insights_prologue_fails_when_every_scenario_fails(harness):
    h = harness({S1: [_timeout()], S2: [_timeout()], S3: [_timeout()]})
    with pytest.raises(RuntimeError, match="all 3 scenarios failed to replay"):
        jobs_batch.run_job(_job(_spec(), "insights"), Sink(), h.env)
    assert h.diagnosed == []


# ─── cancel still stops immediately ───────────────────────────────────────────────────
def test_cancel_stops_the_replay_at_once_even_mid_retry(harness):
    h = harness({S1: [_timeout()]})
    sink = Sink()
    calls = h.runtime.calls

    class CancelAfterFirstCall(Sink):
        def cancelled(self) -> bool:
            return len(calls) >= 1

    sink = CancelAfterFirstCall()
    jobs_batch.run_job(_job(_spec()), sink, h.env)

    assert [c[0] for c in calls] == [S1]  # the retry checkpoint saw the marker first
    assert sink.invocations == [] and sink.result_rows == []
    assert sink.finished[0] == "STOPPED" and sink.finished[1] == "stopped by operator"
