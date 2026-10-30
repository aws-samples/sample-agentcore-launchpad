"""N17-obs422 — an idle-resumed session's later turns are scored online, as a new segment.

eval-wb's probe-glm-idle: turn 1 at 16:39, turn 2 at 16:56, 17 min later with a 15-min
session timeout. The scheduler sampled the session once turn 1 went idle, and a sample was
unique per (config, session), so turn 2 was never scored; Global ends a session at the
timeout and treats later activity as a new one. Now the later activity window is the
session's next SEGMENT: a new sample, idle in turn, whose job scores only the traces that
start after the earlier segment's last activity, with rows keyed apart, so nothing is scored
twice and the earlier sample stays as it was.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.db import SessionLocal
from app.evaluation import jobs
from app.evaluation import online_scheduler as sched
from app.evaluation.executor import JobSink, MemoryStore
from app.evaluation.models import EvalResult, OnlineSample
from app.services.strands_eval import jobs_online
from tests.evaluation import corpus_support as cs
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_online_configs import ledger_row, make_agent
from tests.evaluation.test_online_scheduler import _again, harness, scan_row

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "online"
                      / "probe-glm-idle-2f087217.json").read_text())
SID = FIXTURE["session_id"]
CONFIG = "oe_idle-1"
TIMEOUT = timedelta(minutes=15)


def _turns() -> list[tuple[str, datetime, datetime]]:
    """``[(trace_id, first start, last record)]`` of the probe's two turns, in order."""
    spans: dict[str, list[int]] = {}
    lasts: dict[str, int] = {}
    for x in FIXTURE["records"]:
        rec = x["record"]
        trace = rec.get("traceId")
        if not trace:
            continue
        if "startTimeUnixNano" in rec:
            spans.setdefault(trace, []).append(int(rec["startTimeUnixNano"]))
        end = int(rec.get("endTimeUnixNano") or rec.get("timeUnixNano") or 0)
        lasts[trace] = max(lasts.get(trace, 0), end)

    def at(ns: int) -> datetime:  # Logs Insights' millisecond precision
        return datetime.fromtimestamp(ns // 1_000_000 / 1000, tz=UTC)

    return sorted(((t, at(min(s)), at(lasts[t])) for t, s in spans.items()),
                  key=lambda turn: turn[1])


def _run(job) -> list[dict]:
    store = MemoryStore()
    jobs_online.run(job, JobSink(store, job), logs_client=cs.GroupLogs(FIXTURE["records"]),
                    judge_factory=lambda *_a, **_k: FakeJudge(), sleep=lambda _s: None)
    raw = store.get(jobs.results_key(job.job_id, 1)).decode()
    return [json.loads(line) for line in raw.splitlines()]


def test_an_idle_resumed_session_gets_a_second_segment_scoring_only_its_new_turn():
    (t1, _s1, last1), (t2, start2, last2) = _turns()
    assert start2 - last1 > TIMEOUT  # the probe's idle gap
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, CONFIG, sampling_percentage=100.0, session_timeout_minutes=15,
               evaluators=["Builtin.Helpfulness"])

    # turn 1 went idle: segment 1, bounded by its last activity
    now1 = last1 + TIMEOUT + timedelta(minutes=1)
    executor, _, kwargs = harness(rows=[scan_row(SID, last1)])
    sched.tick(db, now1, **kwargs)
    job1 = executor.submitted[0]
    assert job1.spec["sessions"] == [{"session_id": SID, "until": last1.isoformat()}]
    records1 = _run(job1)  # the worker already sees turn 2's records: the bound holds
    assert {r["trace_id"] for r in records1} == {t1}
    executor.complete(job1.job_id, records1)
    sched.tick(db, now1 + timedelta(minutes=1), **_again(kwargs, [scan_row(SID, last1)]))
    first = db.query(OnlineSample).filter_by(config_id=CONFIG).one()
    assert (first.segment, first.status) == (1, "evaluated")
    before = {r.id: (r.trace_id, r.score) for r in db.query(EvalResult).all()}

    # a late record of turn 1 (inside the timeout) is not a new segment
    sched.tick(db, now1 + timedelta(minutes=2),
               **_again(kwargs, [scan_row(SID, last1 + timedelta(minutes=2))]))
    assert db.query(OnlineSample).filter_by(config_id=CONFIG).count() == 1

    # turn 2 after the idle gap, idle in turn: segment 2
    now2 = last2 + TIMEOUT + timedelta(minutes=1)
    sched.tick(db, now2, **_again(kwargs, [scan_row(SID, last2)]))
    samples = sorted(db.query(OnlineSample).filter_by(config_id=CONFIG).all(),
                     key=lambda s: s.segment)
    assert [(s.segment, s.status) for s in samples] == [(1, "evaluated"), (2, "dispatched")]
    second = samples[1]
    assert sched._aware(second.segment_after) == last1
    assert sched._aware(second.last_activity) == last2
    job2 = executor.submitted[1]
    assert job2.job_id != job1.job_id
    assert job2.spec["sessions"] == [{"session_id": SID, "after": last1.isoformat(),
                                      "until": last2.isoformat()}]
    records2 = _run(job2)
    assert {r["trace_id"] for r in records2} == {t2}
    executor.complete(job2.job_id, records2)
    sched.tick(db, now2 + timedelta(minutes=1), **_again(kwargs, [scan_row(SID, last2)]))

    rows = db.query(EvalResult).all()
    assert sorted(r.trace_id for r in rows) == sorted([t1, t2])  # each turn once
    assert {r.id: (r.trace_id, r.score) for r in rows if r.trace_id == t1} == before
    assert jobs.record_id(CONFIG, f"{SID}#2", "Builtin.Helpfulness", 0) in {r.id for r in rows}
    assert all(r.session_id == SID for r in rows)
    samples = sorted(db.query(OnlineSample).filter_by(config_id=CONFIG).all(),
                     key=lambda s: s.segment)
    assert [(s.segment, s.status) for s in samples] == [(1, "evaluated"), (2, "evaluated")]
    # no third segment without new activity
    sched.tick(db, now2 + timedelta(hours=1), **_again(kwargs, [scan_row(SID, last2)]))
    assert db.query(OnlineSample).filter_by(config_id=CONFIG).count() == 2
    db.close()


def test_a_resumed_segment_still_active_waits_until_it_is_idle():
    (_t1, _s1, last1), (_t2, _start2, last2) = _turns()
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_idle-2", sampling_percentage=100.0, session_timeout_minutes=15)
    executor, _, kwargs = harness(rows=[scan_row(SID, last1)])
    sched.tick(db, last1 + TIMEOUT + timedelta(minutes=1), **kwargs)
    # turn 2 just happened: active, not idle for the timeout yet
    sched.tick(db, last2 + timedelta(minutes=5), **_again(kwargs, [scan_row(SID, last2)]))
    assert db.query(OnlineSample).filter_by(config_id="oe_idle-2").count() == 1
    db.close()


def test_the_worker_scores_only_the_segment_and_says_when_it_is_not_readable_yet():
    (t1, _s1, last1), (t2, _start2, last2) = _turns()
    provider_data = cs.provider_data(FIXTURE, FIXTURE["records"], "both")
    whole, positions = jobs_online.segment_of(provider_data)
    assert [t.trace_id for t in whole["trajectory"].traces] == [t1, t2] and positions == [0, 1]
    seg, positions = jobs_online.segment_of(provider_data, last1.isoformat(), last2.isoformat())
    assert [t.trace_id for t in seg["trajectory"].traces] == [t2] and positions == [1]
    assert seg["output"] == whole["output"]
    seg1, _ = jobs_online.segment_of(provider_data, None, last1.isoformat())
    assert [t.trace_id for t in seg1["trajectory"].traces] == [t1]
    assert seg1["output"] != whole["output"]
    try:
        jobs_online.segment_of(provider_data, last2.isoformat(), None)
    except LookupError as exc:
        assert "telemetry" in str(exc)  # readiness: P4.6 retries it
    else:
        raise AssertionError("an empty segment must raise")


def test_a_recent_sampling_floor_keeps_both_segments_on_the_real_probe():
    # N17-obs444: the config started sampling a minute before turn 1. Segment 1 is bounded
    # after the floor and still scores turn 1; segment 2 starts after turn 1's activity (later
    # than the floor), and a floor between the turns would have dropped turn 1 only.
    (t1, start1, last1), (t2, _start2, last2) = _turns()
    floor = start1 - timedelta(minutes=1)
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, CONFIG, sampling_percentage=100.0, session_timeout_minutes=15,
               evaluators=["Builtin.Helpfulness"], created_at=floor)
    now1 = last1 + TIMEOUT + timedelta(minutes=1)
    executor, _, kwargs = harness(rows=[scan_row(SID, last1)])
    sched.tick(db, now1, **kwargs)
    job1 = executor.submitted[0]
    assert job1.spec["sessions"] == [{"session_id": SID, "after": floor.isoformat(),
                                      "until": last1.isoformat()}]
    records1 = _run(job1)
    assert {r["trace_id"] for r in records1} == {t1}
    executor.complete(job1.job_id, records1)
    sched.tick(db, now1 + timedelta(minutes=1), **_again(kwargs, [scan_row(SID, last1)]))

    now2 = last2 + TIMEOUT + timedelta(minutes=1)
    sched.tick(db, now2, **_again(kwargs, [scan_row(SID, last2)]))
    job2 = executor.submitted[1]
    assert job2.spec["sessions"] == [{"session_id": SID, "after": last1.isoformat(),
                                      "until": last2.isoformat()}]
    assert {r["trace_id"] for r in _run(job2)} == {t2}
    db.close()
