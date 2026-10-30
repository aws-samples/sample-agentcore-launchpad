"""Online-evaluation scheduler (N12 E2-2, plan §B.3) + the worker-side ``online_eval`` handler.

The scheduler runs in the backend: scan each ENABLED config's log group for idle sessions,
sample deterministically, dedupe through ``online_samples``, dispatch one ``online_eval`` job
per (config, sampled_at) group through the executor seam, ingest the worker's records into
``eval_results`` on later ticks. The handler runs on the worker (pure library): spans →
``EvaluationData`` → strands evaluators → result records on the job sink.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import jobs, online
from app.evaluation import online_scheduler as sched
from app.evaluation.executor import JobSink, MemoryStore
from app.evaluation.jobs import JobDone, JobSpec, JobStatus
from app.evaluation.models import EvalEvaluator, EvalResult, OnlineEvalConfig, OnlineSample
from app.services.strands_eval import jobs_online
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor, FakeLogs, FakeModel
from tests.evaluation.test_online_configs import LOG_GROUP, SERVICE, ledger_row, make_agent

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "spans_session.json").read_text()
)
FIX_SID, FIX_GROUP = FIXTURE["session_id"], FIXTURE["log_group"]
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WS = ws_ctx(resources={"artifacts_bucket": "launchpad-artifacts"})


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def scan_row(session_id: str, last: datetime, *, first: datetime | None = None, records=5):
    return {"session_id": session_id, "last": _ts(last), "first": _ts(first or last),
            "records": str(records)}


def fake_queries(rows: list[dict]):
    """A ``run_insights_queries`` double: records every call, answers the scan rows."""
    seen: list[dict] = []

    def run(queries, hours, logs=None, log_groups=None, workspace=None):
        seen.append({"queries": dict(queries), "hours": hours,
                     "log_groups": list(log_groups or []), "workspace": workspace})
        return {name: list(rows) for name in queries}

    run.seen = seen  # type: ignore[attr-defined]
    return run


def harness(*, rows=(), executor=None, settings=None):
    executor = executor or FakeExecutor()
    queries = fake_queries(list(rows))
    kwargs = {
        "settings": settings or get_settings(),
        "resolve_workspace": lambda _wid: WS,
        "run_queries": queries,
        "executor_for": lambda _settings, job_type: executor,
    }
    return executor, queries, kwargs


def _again(kwargs, rows=()):
    return {**kwargs, "run_queries": fake_queries(list(rows))}


# ─── pure helpers ───────────────────────────────────────────────────────────


def test_scan_query_shape_and_filter_rendering():
    q = sched.scan_query(SERVICE)
    assert f'resource.attributes.service.name = "{SERVICE}"' in q
    assert "ispresent(attributes.session.id)" in q
    assert ("stats max(@timestamp) as last, min(@timestamp) as first, count(*) as records "
            "by attributes.session.id as session_id") in q
    filters = [
        {"key": "user.region", "operator": "Equals", "value": {"stringValue": "cn-north"}},
        {"key": "latency_ms", "operator": "GreaterThan", "value": {"doubleValue": 2.5}},
        {"key": "session.id", "operator": "NotContains", "value": {"stringValue": "test-"}},
        {"key": "resource.attributes.vip", "operator": "Equals", "value": {"booleanValue": True}},
    ]
    clause = sched.filter_clause(filters)
    assert 'attributes.user.region = "cn-north"' in clause
    assert "attributes.latency_ms > 2.5" in clause
    assert 'not strcontains(attributes.session.id, "test-")' in clause
    assert "resource.attributes.vip = true" in clause  # already-qualified keys stay as they are
    assert clause in sched.scan_query(SERVICE, filters)
    with pytest.raises(ValueError):
        sched.filter_clause([{"key": "k", "operator": "Like", "value": {"stringValue": "x"}}])


def test_sampling_is_deterministic_and_roughly_proportional():
    ids = [f"session-{i:04d}" for i in range(1000)]
    chosen = [s for s in ids if sched.is_sampled("oe_x", s, 10.0)]
    assert 70 <= len(chosen) <= 130, len(chosen)  # 10 % of 1000, ±3σ
    assert chosen == [s for s in ids if sched.is_sampled("oe_x", s, 10.0)]  # stable
    assert len([s for s in ids if sched.is_sampled("oe_x", s, 100.0)]) == 1000
    assert len([s for s in ids if sched.is_sampled("oe_x", s, 0.01)]) <= 3
    # a different config samples a different subset of the same sessions
    assert [s for s in ids if sched.is_sampled("oe_y", s, 10.0)] != chosen


def test_job_id_is_derived_from_config_and_sample_stamp():
    stamp = datetime(2026, 9, 2, 12, 0, 1, 250000, tzinfo=UTC)
    job_id = sched.job_id_for("oe_a-1", stamp)
    assert job_id == "oe-oe_a-1-20260902T120001250000"
    assert sched.job_id_for("oe_a-1", stamp) == job_id
    assert len(jobs.runtime_session_id(job_id, 1)) >= 33
    # SQLite hands the stamp back naive — the id must not change
    assert sched.job_id_for("oe_a-1", stamp.replace(tzinfo=None)) == job_id


# ─── scan + sample + dispatch ───────────────────────────────────────────────


def test_tick_samples_idle_sessions_and_dispatches_one_job_per_config():
    db = SessionLocal()
    agent = make_agent(db)
    row = ledger_row(db, agent, "oe_a-1", sampling_percentage=100.0, session_timeout_minutes=3,
                     evaluators=["Builtin.Helpfulness", "cust_tone"])
    db.add(EvalEvaluator(id="cust_tone", workspace_id=DEFAULT_WORKSPACE_ID, name="cust_tone",
                         definition="judge", instructions="Rate the tone.",
                         rating_scale=[{"label": "good", "value": 1.0}], level="TRACE"))
    db.add(OnlineSample(workspace_id=DEFAULT_WORKSPACE_ID, config_id="oe_a-1",
                        session_id="s-seen", status="evaluated", sampled_at=NOW))
    db.commit()
    rows = [
        scan_row("s-idle-1", NOW - timedelta(minutes=10)),
        scan_row("s-idle-2", NOW - timedelta(minutes=4)),
        scan_row("s-active", NOW - timedelta(minutes=1)),   # still inside the timeout
        scan_row("s-seen", NOW - timedelta(minutes=30)),     # already sampled earlier
        {"session_id": "", "last": _ts(NOW)},                # malformed row — ignored
    ]
    executor, queries, kwargs = harness(rows=rows)

    report = sched.tick(db, NOW, **kwargs)

    assert report.scanned == 5 and report.sampled == 2
    assert len(report.dispatched) == 1
    call = queries.seen[0]
    # N17-obs353 (P4.8): the scan reads the job's groups — aws/spans carries unified telemetry
    assert call["log_groups"] == [LOG_GROUP, "aws/spans"] and call["workspace"] is WS
    assert call["hours"] >= 1 and f'"{SERVICE}"' in call["queries"]["scan"]
    samples = {s.session_id: s for s in db.query(OnlineSample).all()}
    assert set(samples) == {"s-seen", "s-idle-1", "s-idle-2"}
    assert samples["s-idle-1"].status == "dispatched"
    assert samples["s-idle-2"].status == "dispatched"
    assert samples["s-idle-1"].sampled_at.replace(tzinfo=UTC) == NOW
    db.refresh(row)
    assert row.last_scanned_at.replace(tzinfo=UTC) == NOW

    job = executor.submitted[0]
    assert job.job_type == "online_eval" and job.job_id == report.dispatched[0]
    assert job.job_id == sched.job_id_for("oe_a-1", NOW)
    assert job.workspace == {"account_id": WS.account_id, "region": WS.region}
    assert job.sink == {"bucket": "launchpad-artifacts", "prefix": jobs.job_prefix(job.job_id, 1)}
    assert job.spec["config_id"] == "oe_a-1" and job.spec["mode"] == "scores"
    assert sorted(s["session_id"] for s in job.spec["sessions"]) == ["s-idle-1", "s-idle-2"]
    assert job.spec["log_groups"] == [LOG_GROUP, "aws/spans"]
    assert job.spec["judge_model_id"] == get_settings().eval_judge_model_id
    defs = {d["id"]: d for d in job.spec["evaluator_defs"]}
    assert defs["Builtin.Helpfulness"] == {"id": "Builtin.Helpfulness"}
    assert defs["cust_tone"]["definition"] == "judge"
    assert defs["cust_tone"]["instructions"] == "Rate the tone."
    assert defs["cust_tone"]["rating_scale"] == [{"label": "good", "value": 1.0}]
    db.close()


def test_tick_respects_the_batch_cap_and_the_watermark_overlap():
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", sampling_percentage=100.0, session_timeout_minutes=5)
    rows = [scan_row(f"session-{i:02d}", NOW - timedelta(minutes=20)) for i in range(30)]
    executor, queries, kwargs = harness(rows=rows)

    first = sched.tick(db, NOW, **kwargs)
    assert first.sampled == 20 and len(first.dispatched) == 1
    assert len(executor.submitted[0].spec["sessions"]) == 20
    # first scan of a fresh config looks back an hour (+ timeout + overlap)
    assert 1 <= queries.seen[0]["hours"] <= 2

    later = NOW + timedelta(minutes=1)
    second = sched.tick(db, later, **kwargs)
    assert second.sampled == 10 and len(second.dispatched) == 1
    assert len(executor.submitted[1].spec["sessions"]) == 10
    assert executor.submitted[1].job_id == sched.job_id_for("oe_a-1", later)
    assert db.query(OnlineSample).filter_by(status="dispatched").count() == 30
    # the second window starts at last_scanned_at − timeout − 5 min → still ≥ 1 h granularity
    assert queries.seen[1]["hours"] == 1
    db.close()


def test_tick_ingests_results_marks_samples_and_is_idempotent():
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", sampling_percentage=100.0, session_timeout_minutes=1)
    rows = [scan_row(f"session-{i}", NOW - timedelta(minutes=5)) for i in (1, 2, 3)]
    executor, _, kwargs = harness(rows=rows)
    sched.tick(db, NOW, **kwargs)
    job_id = executor.submitted[0].job_id

    def rec(sid, evaluator, score, **extra):
        return {"session_id": sid, "trace_id": "t" * 32, "evaluator_id": evaluator,
                "level": "TRACE", "score": score, "label": "ok", "explanation": "fine",
                "error_type": None, "error_message": None,
                "id": jobs.record_id(job_id, sid, evaluator, 0), **extra}

    records = [
        rec("session-1", "Builtin.Helpfulness", 0.9),
        rec("session-1", "Builtin.Refusal", 0.0),
        rec("session-2", "Builtin.Helpfulness", None, error_type="RuntimeError",
            error_message="judge down"),
        # TOOL_CALL fan-out: two records for one (session, evaluator)
        rec("session-1", "Builtin.ToolSelectionAccuracy", 1.0),
        {**rec("session-1", "Builtin.ToolSelectionAccuracy", 0.0),
         "id": jobs.record_id(job_id, "session-1", "Builtin.ToolSelectionAccuracy", 1)},
    ]
    # results stream in before done.json — a partial ingest must be safe
    executor.results[job_id] = records[:2]
    second = sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    assert second.ingested == 2
    assert db.query(OnlineSample).filter_by(status="dispatched").count() == 3

    executor.complete(job_id, records, summary={"scores": []})
    third = sched.tick(db, NOW + timedelta(minutes=2), **_again(kwargs))
    assert third.ingested == 3  # only the new records
    results = {r.id: r for r in db.query(EvalResult).all()}
    assert len(results) == 5
    expected_id = jobs.record_id("oe_a-1", "session-1", "Builtin.Helpfulness", 0)
    assert expected_id in results  # ids are keyed on the CONFIG, not the job
    assert results[expected_id].source == "online" and results[expected_id].config_id == "oe_a-1"
    assert results[expected_id].workspace_id == DEFAULT_WORKSPACE_ID
    fan_out = jobs.record_id("oe_a-1", "session-1", "Builtin.ToolSelectionAccuracy", 1)
    assert results[fan_out].score == 0.0
    errored = results[jobs.record_id("oe_a-1", "session-2", "Builtin.Helpfulness", 0)]
    assert errored.error_message == "judge down" and errored.score is None
    samples = {s.session_id: s for s in db.query(OnlineSample).all()}
    assert samples["session-1"].status == "evaluated"
    assert samples["session-1"].evaluated_at is not None
    assert samples["session-2"].status == "evaluated"  # an errored judgement is still a result
    assert samples["session-3"].status == "failed" and "no result" in samples["session-3"].error

    # re-ingesting the finished job (a restart, a repeated tick) adds nothing
    fourth = sched.tick(db, NOW + timedelta(minutes=3), **_again(kwargs))
    assert fourth.ingested == 0 and db.query(EvalResult).count() == 5
    db.close()


def test_tick_marks_a_lost_job_failed_after_the_stale_window():
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", sampling_percentage=100.0, session_timeout_minutes=1)
    executor, _, kwargs = harness(rows=[scan_row("session-1", NOW - timedelta(minutes=5))])
    sched.tick(db, NOW, **kwargs)
    job_id = executor.submitted[0].job_id
    stale_s = get_settings().eval_job_stale_s

    # status still fresh → nothing happens
    executor.statuses[job_id] = JobStatus(phase="invoking", updated_at=NOW.isoformat())
    sched.tick(db, NOW + timedelta(seconds=stale_s - 30), **_again(kwargs))
    assert db.query(OnlineSample).one().status == "dispatched"
    # status older than eval_job_stale_s → the job is lost
    sched.tick(db, NOW + timedelta(seconds=stale_s + 30), **_again(kwargs))
    sample = db.query(OnlineSample).one()
    assert sample.status == "failed" and "lost" in sample.error
    # a job that never wrote status.json is only lost once the stale window has passed
    db.query(OnlineSample).delete()
    db.commit()
    executor2, _, kwargs2 = harness(rows=[scan_row("session-2", NOW - timedelta(minutes=5))])
    sched.tick(db, NOW, **kwargs2)
    executor2.statuses.clear()
    sched.tick(db, NOW + timedelta(seconds=60), **_again(kwargs2))
    assert db.query(OnlineSample).one().status == "dispatched"
    sched.tick(db, NOW + timedelta(seconds=stale_s + 60), **_again(kwargs2))
    assert db.query(OnlineSample).one().status == "failed"
    db.close()


def test_failed_done_marks_every_sample_failed_with_the_job_error():
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", sampling_percentage=100.0, session_timeout_minutes=1)
    executor, _, kwargs = harness(rows=[scan_row("session-1", NOW - timedelta(minutes=5))])
    sched.tick(db, NOW, **kwargs)
    job_id = executor.submitted[0].job_id
    executor.dones[job_id] = JobDone(state="FAILED", error="RuntimeError: bearer missing")
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    sample = db.query(OnlineSample).one()
    assert sample.status == "failed" and "bearer missing" in sample.error
    db.close()


def test_tick_skips_paused_and_failed_configs_and_samples_only_in_insights_mode():
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_paused", "oe_paused_x", execution_status="DISABLED",
               sampling_percentage=100.0)
    ledger_row(db, agent, "oe_failed", "oe_failed_x", status="FAILED", failure_reason="bad filter",
               sampling_percentage=100.0)
    ledger_row(db, agent, "oe_ins", "oe_ins_x", mode="insights", evaluators=[],
               insights=list(online.INSIGHT_IDS), sampling_percentage=100.0,
               session_timeout_minutes=1)
    executor, queries, kwargs = harness(rows=[scan_row("session-1", NOW - timedelta(minutes=5))])

    report = sched.tick(db, NOW, **kwargs)

    assert len(queries.seen) == 1  # only the insights config was scanned
    assert report.sampled == 1 and report.dispatched == []
    sample = db.query(OnlineSample).one()
    assert sample.config_id == "oe_ins" and sample.status == "sampled"
    assert executor.submitted == []
    assert online.sampled_sessions(db, "oe_ins", NOW - timedelta(hours=1)) == ["session-1"]
    db.close()


def test_worker_missing_and_bad_filter_fail_the_config_transient_errors_do_not():
    db = SessionLocal()
    agent = make_agent(db)
    row = ledger_row(db, agent, "oe_a-1", sampling_percentage=100.0, session_timeout_minutes=1)
    rows = [scan_row("session-1", NOW - timedelta(minutes=5))]
    scope = SimpleNamespace(id=DEFAULT_WORKSPACE_ID, context=WS)

    def no_worker(_settings, job_type):
        raise AppError("evaluation.worker_missing", "no worker", status_code=503)

    _, _, kwargs = harness(rows=rows)
    report = sched.tick(db, NOW, **{**kwargs, "executor_for": no_worker})
    db.refresh(row)
    assert row.status == "FAILED" and "evaluation.worker_missing" in row.failure_reason
    assert report.failed_configs == ["oe_a-1"]
    assert db.query(OnlineSample).one().status == "queued"  # kept — resumed later
    # the next tick skips the FAILED config entirely
    executor, queries, kwargs2 = harness(rows=rows)
    sched.tick(db, NOW + timedelta(minutes=1), **kwargs2)
    assert queries.seen == [] and executor.submitted == []

    # resume → ACTIVE again; a deterministic query failure (bad filter) fails it again
    online.set_execution_status(db, scope, "oe_a-1", enabled=True)

    def malformed(queries, hours, logs=None, log_groups=None, workspace=None):
        raise AppError("observability.query_failed",
                       "Logs Insights query 'scan' ended with status Failed", status_code=502)

    sched.tick(db, NOW + timedelta(minutes=2), **{**kwargs2, "run_queries": malformed})
    db.refresh(row)
    assert row.status == "FAILED" and "status Failed" in row.failure_reason

    # a transient failure leaves the config alone
    online.set_execution_status(db, scope, "oe_a-1", enabled=True)

    def throttled(queries, hours, logs=None, log_groups=None, workspace=None):
        raise RuntimeError("ThrottlingException")

    report = sched.tick(db, NOW + timedelta(minutes=3), **{**kwargs2, "run_queries": throttled})
    db.refresh(row)
    assert row.status == "ACTIVE" and report.failed_configs == []
    db.close()


def test_emf_mirror_emits_each_record_through_the_injected_logs_client(monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", sampling_percentage=100.0, session_timeout_minutes=1)
    settings = get_settings()
    monkeypatch.setattr(settings, "eval_results_log_group_prefix", "/launchpad/evaluations/results")

    class MirrorLogs:
        def __init__(self):
            self.groups: set[str] = set()
            self.events: list[dict] = []
            self.created: list[str] = []

        def put_log_events(self, **kw):
            if kw["logGroupName"] not in self.groups:
                raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}},
                                  "PutLogEvents")
            self.events.extend(kw["logEvents"])
            return {}

        def create_log_group(self, **kw):
            self.groups.add(kw["logGroupName"])
            self.created.append(kw["logGroupName"])

        def create_log_stream(self, **kw):
            return {}

    mirror = MirrorLogs()
    executor, _, kwargs = harness(rows=[scan_row("session-1", NOW - timedelta(minutes=5))],
                                  settings=settings)
    kwargs["logs_for"] = lambda _ctx: mirror
    sched.tick(db, NOW, **kwargs)
    job_id = executor.submitted[0].job_id
    executor.complete(job_id, [{
        "session_id": "session-1", "trace_id": "t" * 32, "evaluator_id": "Builtin.Helpfulness",
        "level": "TRACE", "score": 0.833, "label": "Very helpful", "explanation": "clear",
        "error_type": None, "error_message": None,
    }])
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    assert mirror.created == ["/launchpad/evaluations/results/oe_a-1"]
    assert len(mirror.events) == 1
    emf = json.loads(mirror.events[0]["message"])
    attrs = emf["attributes"]
    assert emf["name"] == "gen_ai.evaluation.result"
    assert attrs["gen_ai.evaluation.name"] == "Builtin.Helpfulness"
    assert attrs["gen_ai.evaluation.score.value"] == 0.833
    assert attrs["gen_ai.evaluation.score.label"] == "Very helpful"
    assert attrs["session.id"] == "session-1" and emf["onlineEvaluationConfigId"] == "oe_a-1"
    assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Launchpad/Evaluations"
    db.close()


def test_start_and_stop_respect_the_enabled_flag():
    disabled = SimpleNamespace(online_eval_enabled=False, online_eval_tick_s=60)
    assert sched.start_online_scheduler(disabled) is None
    assert sched.scheduler_running() is False
    enabled = SimpleNamespace(online_eval_enabled=True, online_eval_tick_s=3600)
    thread = sched.start_online_scheduler(enabled)
    try:
        assert thread is not None and thread.daemon and sched.scheduler_running() is True
        assert sched.start_online_scheduler(enabled) is thread  # idempotent
    finally:
        sched.stop_online_scheduler()
    assert sched.scheduler_running() is False
    sched.stop_online_scheduler()  # idempotent


# ─── worker-side handler ────────────────────────────────────────────────────


def _fixture_logs(session_ids=(FIX_SID,)):
    routes = {}
    for sid in session_ids:
        routes[f"filter attributes.session.id = '{sid}' | sort"] = [
            {"@message": json.dumps(r)} for r in FIXTURE["records"]]
    routes["fields spanId, parentSpanId"] = [
        {"spanId": r["spanId"], "parentSpanId": r.get("parentSpanId", "")}
        for r in FIXTURE["records"] if "startTimeUnixNano" in r]
    return FakeLogs(routes)


JOB_ID = "oe-oe_a-1-20260902T120000000000"


def _job(sessions, evaluator_defs, **spec):
    return JobSpec(
        job_id=JOB_ID, job_type="online_eval",
        workspace={"account_id": "111122223333", "region": "cn-northwest-1"},
        sink={"bucket": "b", "prefix": jobs.job_prefix(JOB_ID, 1)},
        spec={"config_id": "oe_a-1", "sessions": [{"session_id": s} for s in sessions],
              "log_groups": [FIX_GROUP, "aws/spans"], "evaluator_defs": evaluator_defs,
              "mode": "scores", "judge_model_id": "global.zai.glm-5.3", **spec},
    )


def _read(store, key):
    raw = store.get(key)
    return None if raw is None else raw.decode()


def _results(store):
    return [json.loads(line) for line in _read(store, jobs.results_key(JOB_ID, 1)).splitlines()]


def _done(store):
    return JobDone.from_dict(json.loads(_read(store, jobs.done_key(JOB_ID, 1))))


def test_handler_scores_sessions_and_writes_the_job_artefacts():
    store = MemoryStore()
    job = _job([FIX_SID, "missing-session-0001"],
               [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.TrajectoryInOrderMatch"}])
    sink = JobSink(store, job)
    factory_calls = []

    def judge_factory(model_id, **kw):
        factory_calls.append((model_id, kw))
        return FakeModel(structured=[{"reasoning": "clear answer", "score": "Very helpful"}] * 3)

    jobs_online.run(job, sink, logs_client=_fixture_logs(), judge_factory=judge_factory)

    assert factory_calls == [("global.zai.glm-5.3", {"max_tokens": 1024, "temperature": 0.0})]
    lines = _results(store)
    by_key = {(r["session_id"], r["evaluator_id"]): r for r in lines}
    assert len(lines) == 4
    helpful = by_key[(FIX_SID, "Builtin.Helpfulness")]
    assert helpful["score"] == 0.833 and helpful["label"] == "Very Helpful"
    assert helpful["explanation"] == "clear answer" and helpful["level"] == "TRACE"
    assert helpful["id"] == jobs.record_id(JOB_ID, FIX_SID, "Builtin.Helpfulness", 0)
    assert helpful["trace_id"]
    trajectory = by_key[(FIX_SID, "Builtin.TrajectoryInOrderMatch")]
    assert trajectory["score"] is None and trajectory["label"] == "NOT_APPLICABLE"  # no truth
    missing = by_key[("missing-session-0001", "Builtin.Helpfulness")]
    assert missing["score"] is None and missing["error_type"]
    done = _done(store)
    assert done.state == "COMPLETED" and done.result_count == 4
    assert done.summary["scores"] == [
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.833, "count": 1, "n": 1, "unit": "turn"}]
    status = JobStatus.from_dict(json.loads(_read(store, jobs.status_key(JOB_ID, 1))))
    assert status.phase == "evaluating"
    assert status.progress["done"] == 2 and status.progress["total"] == 2


def test_handler_builds_every_evaluator_through_the_injected_judge_and_lambda():
    store = MemoryStore()
    defs = [
        {"id": "cust_tone", "definition": "judge", "instructions": "Rate the tone.",
         "rating_scale": [{"label": "good", "value": 1.0}, {"label": "bad", "value": 0.0}],
         "model_id": "global.moonshotai.kimi-k3", "level": "TRACE"},
        {"id": "cust_code", "definition": "code", "level": "SESSION",
         "lambda_arn": "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:f",
         "lambda_timeout_s": 30},
    ]
    job = _job([FIX_SID], defs)
    models: dict[str, FakeModel] = {}

    def judge_factory(model_id, **kw):
        models[model_id] = FakeModel(structured=[{"score": 1.0, "reason": "good", "label": "good"}])
        return models[model_id]

    class FakeLambda:
        calls: list[dict] = []

        def invoke(self, **kw):
            self.calls.append(kw)
            body = json.dumps({"label": "pass", "value": 1.0, "explanation": "ok"}).encode()

            class _P:
                def read(self):
                    return body
            return {"StatusCode": 200, "Payload": _P()}

    jobs_online.run(job, JobSink(store, job), logs_client=_fixture_logs(),
                    judge_factory=judge_factory, lambda_client=FakeLambda())
    assert list(models) == ["global.moonshotai.kimi-k3"]  # the row's own model id, not the default
    by_key = {r["evaluator_id"]: r for r in _results(store)}
    assert by_key["cust_code"]["score"] == 1.0 and by_key["cust_code"]["level"] == "SESSION"
    assert FakeLambda.calls and FakeLambda.calls[0]["FunctionName"].endswith(":function:f")
    assert by_key["cust_tone"]["level"] == "TRACE"


def test_handler_honours_the_cancel_marker_between_sessions():
    store = MemoryStore()
    job = _job([FIX_SID, FIX_SID[:-1] + "x"], [{"id": "Builtin.TrajectoryInOrderMatch"}])
    sink = JobSink(store, job)
    store.put(jobs.cancel_key(JOB_ID), b"cancel")
    jobs_online.run(job, sink, logs_client=_fixture_logs(),
                    judge_factory=lambda *a, **k: FakeModel())
    done = _done(store)
    assert done.state == "STOPPED" and done.result_count == 0


def test_handler_retries_throttled_judges_with_backoff_and_records_other_errors():
    store = MemoryStore()
    job = _job([FIX_SID], [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.Correctness"}])
    sink = JobSink(store, job)
    attempts = {"n": 0}
    slept: list[float] = []

    class Throttle(Exception):
        pass

    Throttle.__name__ = "ThrottlingException"

    def judge_factory(model_id, **kw):
        return FakeModel(structured=[{"reasoning": "ok", "score": "Very helpful"}])

    real_build = jobs_online.evaluators_lib.build

    def build(definition, judge, **kw):
        evaluator = real_build(definition, judge, **kw)
        if definition["id"] == "Builtin.Helpfulness":
            inner = evaluator.evaluate

            def flaky(case):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise Throttle("slow down")
                return inner(case)
            evaluator.evaluate = flaky  # type: ignore[method-assign]
        else:
            def broken(case):
                raise ValueError("judge returned garbage")
            evaluator.evaluate = broken  # type: ignore[method-assign]
        return evaluator

    jobs_online.run(job, sink, logs_client=_fixture_logs(), judge_factory=judge_factory,
                    sleep=slept.append, build=build)
    assert attempts["n"] == 3 and slept == [2.0, 4.0]
    by_key = {r["evaluator_id"]: r for r in _results(store)}
    assert by_key["Builtin.Helpfulness"]["score"] == 0.833
    assert by_key["Builtin.Correctness"]["error_type"] == "ValueError"
    assert "garbage" in by_key["Builtin.Correctness"]["error_message"]
    done = _done(store)
    assert done.state == "COMPLETED" and done.result_count == 2


def test_handler_rejects_insights_mode_and_make_runner_binds_dependencies():
    job = _job([FIX_SID], [{"id": "Builtin.Helpfulness"}], mode="insights")
    with pytest.raises(ValueError, match="insights"):
        jobs_online.run(job, JobSink(MemoryStore(), job), logs_client=_fixture_logs(),
                        judge_factory=lambda *a, **k: FakeModel())
    runner = jobs_online.make_runner(logs_client=_fixture_logs(),
                                     judge_factory=lambda *a, **k: FakeModel())
    store = MemoryStore()
    ok = _job([FIX_SID], [{"id": "Builtin.TrajectoryInOrderMatch"}])
    runner(ok, JobSink(store, ok))
    assert _done(store).state == "COMPLETED"


def test_handler_module_is_pure_library_code():
    import inspect

    source = inspect.getsource(jobs_online)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import app.", "from app.")):
            assert stripped.startswith("from app.evaluation.jobs"), line
    assert "boto3" not in source
    assert isinstance(OnlineEvalConfig.__tablename__, str)  # keeps the model import honest


def test_online_handler_rejudges_on_a_fresh_judge_with_a_doubled_budget():
    """N-SCN-bug2: the spec's judge_max_tokens is the first attempt; a MaxTokensReachedException
    re-builds the evaluator on a fresh judge at 2× (≤ judge_max_tokens_cap) — the shared judge
    object is never mutated under the other evaluators."""
    from strands.types.exceptions import MaxTokensReachedException

    from tests.evaluation.fakes import FakeJudge

    store = MemoryStore()
    job = _job([FIX_SID], [{"id": "Builtin.Helpfulness"}], judge_max_tokens=1024,
               judge_max_tokens_cap=4096)
    factory_calls = []

    def judge_factory(model_id, **kw):
        factory_calls.append((model_id, kw))
        if kw["max_tokens"] < 2048:
            return FakeJudge(fail_with=MaxTokensReachedException("maximum token limit"))
        return FakeJudge(overrides={"reasoning": "fits", "score": "Very helpful"})

    jobs_online.run(job, JobSink(store, job), logs_client=_fixture_logs(),
                    judge_factory=judge_factory)
    assert factory_calls == [("global.zai.glm-5.3", {"max_tokens": 1024, "temperature": 0.0}),
                             ("global.zai.glm-5.3", {"max_tokens": 2048, "temperature": 0.0})]
    [record] = _results(store)
    assert record["score"] == 0.833 and record["error_type"] is None
    assert _done(store).state == "COMPLETED"


def test_online_handler_records_the_budget_tried_when_the_judge_never_fits():
    from strands.types.exceptions import MaxTokensReachedException

    from tests.evaluation.fakes import FakeJudge

    store = MemoryStore()
    job = _job([FIX_SID], [{"id": "Builtin.Helpfulness"}], judge_max_tokens=1024,
               judge_max_tokens_cap=4096)
    budgets = []

    def judge_factory(model_id, **kw):
        budgets.append(kw["max_tokens"])
        return FakeJudge(fail_with=MaxTokensReachedException("maximum token limit"))

    jobs_online.run(job, JobSink(store, job), logs_client=_fixture_logs(),
                    judge_factory=judge_factory)
    assert budgets == [1024, 2048, 4096]
    [record] = _results(store)
    assert record["error_type"] == "JudgeBudgetExceeded"
    assert record["error_message"] == (
        "judge Builtin.Helpfulness exceeded 4096 output tokens after 3 attempts"
        # N17-obs385: the rescue steps the error names
        "; the context had nothing to truncate; no judge_fallback model is configured")


def test_online_samples_are_reconciled_with_the_chat_ledger():
    # eval accuracy item 1: a sampled session that is a platform chat session ships its
    # ledger answers to the worker; the worker's check lands on the sample and the window's
    # summary on the results payload
    from app.services import chat_ledger
    from app.services.strands_eval import extraction_check

    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_chk-1", sampling_percentage=100.0, session_timeout_minutes=1)
    chat = "session-chat-" + "c" * 20
    chat_ledger.save_message(DEFAULT_WORKSPACE_ID, agent.id, chat, "user", "你好")
    chat_ledger.save_message(DEFAULT_WORKSPACE_ID, agent.id, chat, "agent", "您好！")
    other = "session-api-" + "d" * 20
    rows = [scan_row(sid, NOW - timedelta(minutes=5)) for sid in (chat, other)]
    executor, _, kwargs = harness(rows=rows)
    sched.tick(db, NOW, **kwargs)
    (job,) = executor.submitted
    refs = job.spec["extraction_references"]
    assert set(refs) == {chat} and refs[chat]["source"] == "chat_ledger"
    assert refs[chat]["turns"] == [extraction_check.digest("您好！")]
    checks = {chat: extraction_check.check(["您好！"], refs[chat]),
              other: extraction_check.check(["answer"], None)}
    records = [{"session_id": sid, "trace_id": None, "evaluator_id": "Builtin.Helpfulness",
                "level": "TRACE", "score": 1.0, "label": "ok", "explanation": "",
                "error_type": None, "error_message": None,
                "id": jobs.record_id(job.job_id, sid, "Builtin.Helpfulness", 0)}
               for sid in (chat, other)]
    executor.complete(job.job_id, records,
                      summary={"scores": [], "extraction_checks": checks})
    sched.tick(db, NOW + timedelta(minutes=1), **_again(kwargs))
    samples = {s.session_id: s for s in db.query(OnlineSample).all()}
    assert samples[chat].extraction_check["status"] == "ok"
    assert samples[other].extraction_check["status"] == "unverifiable"
    for row in db.query(EvalResult).filter_by(config_id="oe_chk-1"):  # into the live window
        row.created_at = datetime.now(UTC)
    db.commit()
    window = online.results(db, ws_ctx(), "oe_chk-1", "24h")
    assert window["extraction_check"]["status"] == "ok"
    assert (window["extraction_check"]["ok"], window["extraction_check"]["unverifiable"]) == (1, 1)
    db.close()
