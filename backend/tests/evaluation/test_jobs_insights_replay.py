"""N12-bug9 — an insights job that replays a dataset waits for the replayed sessions' spans.

Live: insights on a DATASET failed in 0.4 min with ``SessionNotFoundError: no spans found``
while insights on a trace window worked — ``run_insights`` read the sessions the moment the
backend had minted their ids, neither replaying nor waiting. Both job kinds now share the
``_replay_dataset`` prologue (``invoking`` → ``waiting``), and the wait is the very same
``wait_for_evaluation_telemetry`` poll/stability/timeout loop: the fake probe below is empty
for N polls and pairs up afterwards, so the test drives the real loop, not a stub of it.
"""

import types

import pytest

from app.evaluation import jobs, telemetry
from app.services.strands_eval import jobs_batch

SESSION_A = "sess-a" + "x" * 30
SESSION_B = "sess-b" + "x" * 30
STABILITY_S = 30
EMPTY_POLLS = 3


class Sink:
    def __init__(self, cancelled: bool = False):
        self.job = types.SimpleNamespace(job_id="j-insights")
        self.statuses: list[tuple[str, str]] = []
        self.invocations: list[dict] = []
        self.artifacts: dict[str, object] = {}
        self.finished: tuple[str, str | None, dict] | None = None
        self._cancelled = cancelled

    def cancelled(self) -> bool:
        return self._cancelled

    def status(self, phase, *, done, total, message):
        self.statuses.append((phase, message))

    def invocation(self, record):
        self.invocations.append(record)

    def artifact(self, name, blob):
        self.artifacts[name] = blob

    def results(self, records):
        pass

    def finish(self, state, error=None, summary=None):
        self.finished = (state, error, summary or {})


class FakeClock:
    """Advances only when the job sleeps, so stability/timeout are deterministic."""

    def __init__(self):
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class SpanProbe:
    """Stands in for ``probe_latest_session``: nothing for ``empty_polls`` calls, then a
    paired root span whose content log is already ``STABILITY_S`` old."""

    def __init__(self, clock: FakeClock, empty_polls: int):
        self.clock = clock
        self.empty_polls = empty_polls
        self.calls: list[dict] = []

    def __call__(self, client, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.empty_polls:
            return telemetry.TelemetryProbe(root_span_id=None, content_ingestion_ms=None)
        ready_at_ms = int((self.clock.now - STABILITY_S) * 1000)
        return telemetry.TelemetryProbe(root_span_id="root-1", content_ingestion_ms=ready_at_ms)


def _spec(*, replay: bool) -> dict:
    sessions = [
        {"session_id": SESSION_A, "scenario_id": "s1", "prompts": ["hi"], "replay": replay,
         "arm": None, "qualifier": None, "baggage": None, "launchpad_meta": None},
        {"session_id": SESSION_B, "scenario_id": "s2", "prompts": ["bye"], "replay": replay,
         "arm": None, "qualifier": None, "baggage": None, "launchpad_meta": None},
    ]
    return {
        "run_id": "run-1", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": sessions, "replay": replay,
        "wait": {"seconds": STABILITY_S, "log_groups": ["/aws/launchpad/runtime", "aws/spans"],
                 "content_log_group": "/aws/launchpad/runtime", "spans_source": "runtime_logs",
                 "lookback_hours": 48},
        "evaluator_defs": [], "ground_truth": {}, "judge_model_id": "judge-model",
        "service_name": "svc", "kinds": ["failure_categorization"],
    }


def _job(spec: dict, job_type: str = "insights") -> jobs.JobSpec:
    return jobs.JobSpec(job_id=f"j-{job_type}", job_type=job_type, attempt=1, workspace={},
                        sink={"bucket": "b", "prefix": f"evaluations/j-{job_type}/a1"},
                        spec=spec)


@pytest.fixture
def harness(monkeypatch):
    clock = FakeClock()
    probe = SpanProbe(clock, EMPTY_POLLS)
    monkeypatch.setattr(telemetry, "probe_latest_session", probe)
    invoked: list[tuple[str, str]] = []

    def invoke(prompt, session_id, session_spec):
        invoked.append((session_id, prompt))
        return {"text": "ok", "session_id": session_id}

    diagnosed: list[dict] = []

    def diagnose(provider, session_ids, kinds, judge, *, stop=None, progress=None, **_kw):
        diagnosed.append({"session_ids": list(session_ids), "kinds": list(kinds),
                          "judge": judge, "probe_calls": len(probe.calls)})
        return {"kinds": kinds, "clusters": [], "errors": []}

    monkeypatch.setattr(jobs_batch.insights, "diagnose_sessions", diagnose)
    env = jobs_batch.WorkerEnv(
        logs_client=object(), data_client=None, judge_factory=lambda m, **kw: f"judge:{m}",
        default_model_id="default-judge", invoke=invoke, spans_source="runtime_logs",
        sleep=clock.sleep, clock=clock,
    )
    return types.SimpleNamespace(env=env, clock=clock, probe=probe, invoked=invoked,
                                 diagnosed=diagnosed)


def test_dataset_insights_replays_then_waits_for_spans_before_the_detectors_run(harness):
    sink = Sink()

    jobs_batch.run_job(_job(_spec(replay=True)), sink, harness.env)

    # every scenario was replayed with its pre-assigned session id, in order
    assert harness.invoked == [(SESSION_A, "hi"), (SESSION_B, "bye")]
    assert [r["status"] for r in sink.invocations] == ["ok", "ok"]
    # the wait polled the NEWEST replayed session until the probe paired up (N empty polls)
    assert len(harness.probe.calls) == EMPTY_POLLS + 1
    assert {c["session_id"] for c in harness.probe.calls} == {SESSION_B}
    assert all(c["content_log_group"] == "/aws/launchpad/runtime" for c in harness.probe.calls)
    assert len(harness.clock.sleeps) == EMPTY_POLLS  # one poll interval per empty probe
    # the detectors ran only after the wait returned, over both sessions
    [diag] = harness.diagnosed
    assert diag["probe_calls"] == EMPTY_POLLS + 1
    assert diag["session_ids"] == [SESSION_A, SESSION_B]
    assert diag["kinds"] == ["failure_categorization"] and diag["judge"] == "judge:judge-model"
    # the phases mirror batch_eval's: invoking → waiting → evaluating
    phases = [p for p, _ in sink.statuses]
    assert phases.index("invoking") < phases.index("waiting") < phases.index("evaluating")
    assert ("waiting", "waiting for spans") in sink.statuses
    assert sink.finished is not None and sink.finished[0] == "COMPLETED"
    assert "insights.json" in sink.artifacts


def test_trace_window_insights_neither_replays_nor_waits(harness):
    sink = Sink()

    jobs_batch.run_job(_job(_spec(replay=False)), sink, harness.env)

    assert harness.invoked == [] and harness.probe.calls == [] and harness.clock.sleeps == []
    phases = [p for p, _ in sink.statuses]
    assert phases and "invoking" not in phases and "waiting" not in phases
    [diag] = harness.diagnosed
    assert diag["session_ids"] == [SESSION_A, SESSION_B]
    assert sink.finished is not None and sink.finished[0] == "COMPLETED"


def test_dataset_insights_fails_like_batch_eval_when_the_spans_never_land(harness):
    harness.probe.empty_polls = 10_000  # never pairs → the shared wait times out
    sink = Sink()

    # N-SCN-bug3: a telemetry timeout is a per-session failure; with EVERY session empty the
    # job still fails, with the first session's telemetry error as the reason
    with pytest.raises(RuntimeError, match="all 2 scenarios failed") as info:
        jobs_batch.run_job(_job(_spec(replay=True)), sink, harness.env)

    assert "TelemetryReadinessTimeout: evaluation telemetry did not become ready" in str(info.value)
    assert [r["status"] for r in sink.invocations] == ["ok", "ok", "failed", "failed"]
    assert harness.diagnosed == []  # the detectors never ran on empty sessions
    assert harness.clock.now >= 1_000.0 + STABILITY_S + jobs_batch.TELEMETRY_READY_GRACE_SECONDS


def test_cancel_during_the_replay_stops_the_insights_job(harness):
    sink = Sink(cancelled=True)

    jobs_batch.run_job(_job(_spec(replay=True)), sink, harness.env)

    assert harness.invoked == [] and harness.diagnosed == []
    assert sink.finished is not None
    assert sink.finished[0] == "STOPPED" and sink.finished[2]["insights"] is None


def test_batch_eval_and_insights_share_one_replay_prologue(monkeypatch, harness):
    """The two kinds must not drift apart again: both go through ``_replay_dataset``."""
    calls: list[str] = []
    real = jobs_batch._replay_dataset

    def spy(env, sink, spec, start_ms, *rest):
        calls.append(spec["run_id"])
        return real(env, sink, spec, start_ms, *rest)

    monkeypatch.setattr(jobs_batch, "_replay_dataset", spy)
    monkeypatch.setattr(jobs_batch.engine_core, "evaluate_sessions", lambda *a, **kw: [])
    jobs_batch.run_job(_job(_spec(replay=True)), Sink(), harness.env)
    harness.probe.calls.clear()
    jobs_batch.run_job(_job({**_spec(replay=True), "run_id": "run-batch"}, "batch_eval"),
                       Sink(), harness.env)
    assert calls == ["run-1", "run-batch"]
