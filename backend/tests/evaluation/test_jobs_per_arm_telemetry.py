"""N-SCN-bug3 — the worker's readiness probe follows each replayed session to ITS arm's log
group, and a session whose telemetry never lands is an errored session, not a failed job.

Live (scenario counting run #4, s08c): a canary replays into one runtime log group per named
endpoint (``…-stableXXX`` / ``…-treatXXX``). ``_wait_for_spans`` probed the newest session in
the FIRST listed group only (alphabetically ``stable…``); whenever that session had been routed
to ``treat…`` the root span was found in ``aws/spans`` but the content log looked missing →
``TelemetryReadinessTimeout`` → the whole 50/50 traffic job FAILED, 8 good replays were lost
and the canary could not reach ``complete`` (≈ 50 % failure at 50/50, ≈ 99 % at 1/99).
"""

from __future__ import annotations

import json
import types

import pytest

from app.evaluation import jobs, telemetry
from app.services.strands_eval import jobs_batch

RESOURCE = "r-1"
STABLE = "stableab12cd"
TREAT = "treatab12cd"
G_STABLE = f"/aws/bedrock-agentcore/runtimes/{RESOURCE}-{STABLE}"
G_TREAT = f"/aws/bedrock-agentcore/runtimes/{RESOURCE}-{TREAT}"
LOG_GROUPS = [G_STABLE, G_TREAT, "aws/spans"]
EVALUATORS = [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.Correctness"}]
GRACE = jobs_batch.TELEMETRY_READY_GRACE_SECONDS


def _sid(n: int) -> str:
    return f"sess-{n:03d}" + "x" * 56


class Sink:
    def __init__(self):
        self.job = types.SimpleNamespace(job_id="j-arms")
        self.statuses: list[tuple[str, int, str]] = []
        self.invocations: list[dict] = []
        self.result_rows: list[dict] = []
        self.finished: tuple[str, str | None, dict] | None = None

    def cancelled(self) -> bool:
        return False

    def status(self, phase, *, done, total, message):
        self.statuses.append((phase, done, message))

    def invocation(self, record):
        self.invocations.append(record)

    def artifact(self, name, blob):
        pass

    def results(self, records):
        self.result_rows.extend(records)

    def finish(self, state, error=None, summary=None):
        self.finished = (state, error, summary or {})


class Clock:
    """Fake time: ``sleep`` advances it, so a probe that never pairs times out."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


def _session(n: int, arm: str, *, stamp_group: bool = True, qualifier: bool = True) -> dict:
    endpoint = STABLE if arm == "C" else TREAT
    spec = {"session_id": _sid(n), "scenario_id": f"s{n}", "prompts": ["hi"], "replay": True,
            "arm": arm, "qualifier": endpoint if qualifier else None, "baggage": None,
            "launchpad_meta": None}
    if stamp_group:
        spec["content_log_group"] = G_STABLE if arm == "C" else G_TREAT
    return spec


def _spec(sessions: list[dict], **wait_overrides) -> dict:
    wait = {"seconds": 0, "log_groups": list(LOG_GROUPS), "spans_source": "both",
            "lookback_hours": 48}
    wait.update(wait_overrides)
    return {"run_id": "run-arms", "replay": True,
            "target": {"runtime_arn": "rt", "protocol": "http"},
            "sessions": sessions, "wait": wait, "evaluator_defs": list(EVALUATORS),
            "ground_truth": {}, "judge_model_id": "judge"}


def _job(spec: dict) -> jobs.JobSpec:
    return jobs.JobSpec(job_id="j-arms", job_type="batch_eval", attempt=1, workspace={},
                        sink={"bucket": "b", "prefix": "evaluations/j-arms/a1"}, spec=spec)


@pytest.fixture
def harness(monkeypatch):
    """``content_at[session_id]`` is the ONE log group holding that session's content log
    (``None`` = nowhere); the root span is found in every group (``aws/spans`` is account-wide).
    The engine scores whatever it is given and records which sessions it saw."""
    content_at: dict[str, str | None] = {}
    probes: list[tuple[str, str]] = []

    def probe(client, *, session_id, content_log_group, start_time_ms=None, spans_source="both"):
        probes.append((session_id, content_log_group))
        if content_at.get(session_id) == content_log_group:
            return telemetry.TelemetryProbe(root_span_id="root", content_ingestion_ms=0)
        return telemetry.TelemetryProbe(
            root_span_id="root", content_ingestion_ms=None, framework="strands",
            detail="content log for strands span root is missing")

    monkeypatch.setattr(telemetry, "probe_latest_session", probe)
    evaluated: list[list[str]] = []

    def evaluate_sessions(provider, session_ids, evaluators, truth, *, job_id, sleep,
                          stop=None, progress=None, on_records=None, concurrency=4, **_kw):
        evaluated.append(list(session_ids))
        out = []
        for sid in session_ids:
            recs = [{"session_id": sid, "evaluator_id": d["id"], "level": "TRACE", "score": 0.8,
                     "label": "pass", "explanation": "fine", "error_type": None,
                     "error_message": None} for d in evaluators.definitions]
            if on_records:
                on_records(sid, recs)
            if progress:
                progress("evaluating")
            out.extend(recs)
        return out

    monkeypatch.setattr(jobs_batch.engine_core, "evaluate_sessions", evaluate_sessions)
    clock = Clock()
    env = jobs_batch.WorkerEnv(
        logs_client=object(), data_client=None, judge_factory=lambda m, **kw: f"judge:{m}",
        default_model_id="judge", spans_source="both", sleep=clock.sleep, clock=clock,
        invoke=lambda prompt, session_id, session: {"text": "ok", "session_id": session_id},
    )
    return types.SimpleNamespace(env=env, content_at=content_at, probes=probes,
                                 evaluated=evaluated, clock=clock)


def _probed_groups(h, session_id: str) -> set[str]:
    return {group for sid, group in h.probes if sid == session_id}


# ─── the live failure: the newest session was served by the treatment endpoint ───────
def test_newest_session_on_the_treatment_arm_is_probed_in_the_treat_group(harness):
    h = harness
    sessions = [_session(1, "C"), _session(2, "C"), _session(3, "T1")]
    for s in sessions:
        h.content_at[s["session_id"]] = s["content_log_group"]
    sink = Sink()

    jobs_batch.run_job(_job(_spec(sessions)), sink, h.env)

    # one probe per arm, each on the newest answered session of that arm, in ITS group
    assert _probed_groups(h, _sid(3)) == {G_TREAT}
    assert _probed_groups(h, _sid(2)) == {G_STABLE}
    assert _probed_groups(h, _sid(1)) == set()
    assert h.evaluated == [[_sid(1), _sid(2), _sid(3)]]
    assert [r["status"] for r in sink.invocations] == ["ok"] * 3
    state, error, summary = sink.finished
    assert state == "COMPLETED" and error is None and "replay_failures" not in summary
    waiting = [m for p, _d, m in sink.statuses if p == "waiting"]
    assert waiting == ["waiting for spans · log group 1/2", "waiting for spans · log group 2/2"]


def test_the_group_is_derived_from_the_qualifier_when_the_hub_did_not_stamp_it(harness):
    h = harness
    sessions = [_session(1, "C", stamp_group=False), _session(2, "T1", stamp_group=False)]
    h.content_at[_sid(1)] = G_STABLE
    h.content_at[_sid(2)] = G_TREAT
    sink = Sink()

    jobs_batch.run_job(_job(_spec(sessions)), sink, h.env)

    assert _probed_groups(h, _sid(1)) == {G_STABLE}
    assert _probed_groups(h, _sid(2)) == {G_TREAT}
    assert sink.finished[0] == "COMPLETED"


def test_without_arm_information_every_endpoint_group_is_probed(harness):
    """No stamped group, no qualifier, two endpoint groups listed: the content is looked for
    in ALL of them rather than in the alphabetically first."""
    h = harness
    sessions = [_session(1, "T1", stamp_group=False, qualifier=False)]
    h.content_at[_sid(1)] = G_TREAT  # the second listed group
    sink = Sink()

    jobs_batch.run_job(_job(_spec(sessions)), sink, h.env)

    assert _probed_groups(h, _sid(1)) == {G_STABLE, G_TREAT}
    assert sink.finished[0] == "COMPLETED" and h.evaluated == [[_sid(1)]]
    assert h.clock.now == 1_000.0  # found on the first poll — no waiting at all


def test_the_job_wide_content_log_group_still_wins_for_single_endpoint_jobs(harness):
    h = harness
    sessions = [_session(1, "C", stamp_group=False, qualifier=False)]
    h.content_at[_sid(1)] = "/aws/launchpad/runtime"
    sink = Sink()

    jobs_batch.run_job(_job(_spec(sessions, content_log_group="/aws/launchpad/runtime")),
                       sink, h.env)

    assert _probed_groups(h, _sid(1)) == {"/aws/launchpad/runtime"}
    assert sink.finished[0] == "COMPLETED"
    assert [m for p, _d, m in sink.statuses if p == "waiting"] == ["waiting for spans"]


# ─── one session's content never lands: errored session, the job completes ───────────
def test_a_session_whose_telemetry_never_lands_is_errored_and_the_job_completes(harness):
    h = harness
    sessions = [_session(1, "C"), _session(2, "T1"), _session(3, "C")]
    h.content_at[_sid(1)] = G_STABLE
    h.content_at[_sid(2)] = None  # answered, but its content log never arrives anywhere
    h.content_at[_sid(3)] = G_STABLE
    sink = Sink()

    jobs_batch.run_job(_job(_spec(sessions, seconds=30)), sink, h.env)

    # the treat probe waited out its budget (stability + grace) and gave up on session 2 only
    assert h.clock.now >= 1_000.0 + 30 + GRACE
    lines = [r for r in sink.invocations if r["session_id"] == _sid(2)]
    assert [r["status"] for r in lines] == ["ok", "failed"]  # the last line per session wins
    errored = lines[-1]
    assert errored["code"] == "TelemetryReadinessTimeout"
    assert errored["error_kind"] == "infrastructure" and errored["failure"] == "telemetry"
    assert errored["attempts"] == 1 and errored["arm"] == "T1"
    assert "content log for strands span root is missing" in errored["error"]
    # the engine saw the two stable sessions only; session 2 got one error record per evaluator
    assert h.evaluated == [[_sid(1), _sid(3)]]
    bad = [r for r in sink.result_rows if r["session_id"] == _sid(2)]
    assert {r["evaluator_id"] for r in bad} == {"Builtin.Helpfulness", "Builtin.Correctness"}
    assert all(r["score"] is None and r["error_type"] == "TelemetryReadinessTimeout" for r in bad)
    assert all(r["error_kind"] == "infrastructure" for r in bad)
    assert all("scenario s2 answered but its telemetry never became ready: "
               "TelemetryReadinessTimeout" in r["error_message"] for r in bad)
    state, error, summary = sink.finished
    assert state == "COMPLETED" and error is None
    assert summary["scores"] == [
        {"evaluatorId": "Builtin.Correctness", "score": 0.8, "count": 2, "n": 2, "unit": "turn"},
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.8, "count": 2, "n": 2, "unit": "turn"},
    ]
    assert summary["replay_failures"] == [{
        "session_id": _sid(2), "scenario_id": "s2", "code": "TelemetryReadinessTimeout",
        "attempts": 1, "error": errored["error"], "arm": errored["arm"],  # N12-obs62
    }]


def test_older_sessions_of_a_timed_out_group_get_one_probe_each(harness):
    """When the newest session of a group never pairs, its older siblings are probed once:
    the ones whose content landed are evaluated, the rest are errored too."""
    h = harness
    sessions = [_session(1, "T1"), _session(2, "T1"), _session(3, "T1")]
    h.content_at[_sid(1)] = G_TREAT
    h.content_at[_sid(2)] = None
    h.content_at[_sid(3)] = None
    sink = Sink()

    jobs_batch.run_job(_job(_spec(sessions)), sink, h.env)

    assert h.evaluated == [[_sid(1)]]
    failed = {r["session_id"] for r in sink.invocations if r["status"] == "failed"}
    assert failed == {_sid(2), _sid(3)}
    assert len([p for p in h.probes if p[0] == _sid(1)]) == 1  # one single-shot probe
    assert len([p for p in h.probes if p[0] == _sid(2)]) == 1
    state, _error, summary = sink.finished
    assert state == "COMPLETED"
    assert [f["session_id"] for f in summary["replay_failures"]] == [_sid(3), _sid(2)]


# ─── every session missing: the job fails with the first error ───────────────────────
def test_when_no_session_has_telemetry_the_job_fails_with_the_first_error(harness):
    h = harness
    sessions = [_session(1, "C"), _session(2, "T1")]
    h.content_at[_sid(1)] = None
    h.content_at[_sid(2)] = None
    sink = Sink()

    with pytest.raises(RuntimeError) as info:
        jobs_batch.run_job(_job(_spec(sessions)), sink, h.env)

    message = str(info.value)
    assert message.startswith("all 2 scenarios failed — first error: scenario s1 answered but "
                              "its telemetry never became ready: TelemetryReadinessTimeout: ")
    assert "evaluation telemetry did not become ready for session" in message
    assert [r["status"] for r in sink.invocations] == ["ok", "ok", "failed", "failed"]
    assert h.evaluated == [] and sink.result_rows == [] and sink.finished is None


def test_a_replay_failure_is_still_the_first_error_when_the_rest_never_land(harness):
    h = harness
    sessions = [_session(1, "C"), _session(2, "T1")]
    h.content_at[_sid(2)] = None

    def invoke(prompt, session_id, session):
        if session_id == _sid(1):
            raise ValueError("the agent is gone")
        return {"text": "ok", "session_id": session_id}

    h.env.invoke = invoke
    with pytest.raises(RuntimeError, match="first error: scenario s1 failed after 1/3 attempts: "
                                           "ValueError: the agent is gone"):
        jobs_batch.run_job(_job(_spec(sessions)), Sink(), h.env)


# ─── routing at 1/99: ~every session lands on the treatment endpoint ─────────────────
def test_a_1_99_round_completes_with_the_last_session_on_the_treatment_arm(harness):
    """100 pre-assigned sessions, 1 on control and 99 on treatment (what a 1/99 ramp stage
    sends): the probe follows the newest session of EACH arm to its own group, so the round
    completes instead of failing on the ``stable…`` probe of a ``treat…`` session."""
    h = harness
    sessions = [_session(1, "C")] + [_session(n, "T1") for n in range(2, 101)]
    for s in sessions:
        h.content_at[s["session_id"]] = s["content_log_group"]
    sink = Sink()

    jobs_batch.run_job(_job(_spec(sessions)), sink, h.env)

    assert _probed_groups(h, _sid(1)) == {G_STABLE}
    assert _probed_groups(h, _sid(100)) == {G_TREAT}
    assert len(h.probes) == 2  # exactly one probe per arm, nothing else waited on
    assert len(h.evaluated[0]) == 100
    assert sum(1 for r in sink.invocations if r["status"] == "ok") == 100
    assert sink.finished[0] == "COMPLETED" and len(sink.result_rows) == 200


# ─── the group resolver on its own ───────────────────────────────────────────────────
@pytest.mark.parametrize("session, expected", [
    ({"content_log_group": "/g/own", "qualifier": TREAT}, ["/g/own"]),
    ({"qualifier": TREAT}, [G_TREAT]),
    ({"qualifier": STABLE}, [G_STABLE]),
    ({"qualifier": "unknown"}, [G_STABLE, G_TREAT]),
    ({"qualifier": None}, [G_STABLE, G_TREAT]),
    ({}, [G_STABLE, G_TREAT]),
])
def test_content_log_groups_for(session, expected):
    assert jobs_batch.content_log_groups_for({"log_groups": LOG_GROUPS}, session) == expected


def test_content_log_groups_for_prefers_the_job_wide_group():
    wait = {"log_groups": LOG_GROUPS, "content_log_group": "/g/job"}
    assert jobs_batch.content_log_groups_for(wait, {"qualifier": TREAT}) == ["/g/job"]
    assert jobs_batch.content_log_groups_for(wait, {}) == ["/g/job"]


# ─── telemetry: several candidate groups in one poll loop ────────────────────────────
class _Logs:
    """``filter_log_events`` stub: the root span in ``aws/spans``, the content in ONE group."""

    def __init__(self, session_id: str, content_group: str | None):
        self.calls: list[str] = []
        root = {"message": json.dumps({"name": telemetry.ROOT_SPAN_NAME, "spanId": "sp1",
                                       "attributes": {"session.id": session_id}}),
                "timestamp": 10_000, "ingestionTime": 10_100}
        content = {"message": json.dumps({"spanId": "sp1", "attributes": {"session.id": session_id},
                                          "body": {"input": {}, "output": {}}}),
                   "timestamp": 10_000, "ingestionTime": 10_500}
        self.by_group = {"aws/spans": [root]}
        if content_group:
            self.by_group[content_group] = [content]

    def filter_log_events(self, **kwargs):
        group = kwargs["logGroupName"]
        self.calls.append(group)
        return {"events": self.by_group.get(group, [])}


def test_wait_for_evaluation_telemetry_finds_the_content_in_a_fallback_group():
    sid = "s" * 40
    logs = _Logs(sid, G_TREAT)
    clock = Clock(start=100.0)
    probe = telemetry.wait_for_evaluation_telemetry(
        logs, session_id=sid, content_log_group=G_STABLE, content_log_groups=[G_TREAT],
        stability_seconds=0, timeout_seconds=60, clock=clock, sleep=clock.sleep)
    assert probe.paired and probe.root_span_id == "sp1"
    assert G_STABLE in logs.calls and G_TREAT in logs.calls
    assert clock.now == 100.0  # first poll


def test_wait_for_evaluation_telemetry_timeout_reports_the_most_advanced_probe():
    sid = "s" * 40
    logs = _Logs(sid, None)
    clock = Clock(start=100.0)
    with pytest.raises(telemetry.TelemetryReadinessTimeout,
                       match="content log for strands span sp1 is missing"):
        telemetry.wait_for_evaluation_telemetry(
            logs, session_id=sid, content_log_group=G_STABLE, content_log_groups=[G_TREAT],
            stability_seconds=0, timeout_seconds=12, clock=clock, sleep=clock.sleep)
    assert clock.now >= 112.0
