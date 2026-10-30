"""N17-obs444 — an online config samples only activity after it started sampling.

The form promises 「参与评估的新会话比例」, but a config's first scan reached back
``FRESH_LOOKBACK`` (1 h) before now and sampled every idle session in it: live,
oe_hr_eval_demo_ec0307 was created at 12:42:00Z and its first results (12:44–12:45Z) were for
sessions that ended at 11:11–11:12Z. After a pause the scan window starts at the last scan
before the pause, so a resume back-filled everything active while it was paused.

The rule: a config samples from its **sampling floor** — the moment it was last ENABLED
(``sampling_since``, set on a DISABLED → ENABLED transition) or, never paused, its
``created_at``. A candidate segment needs activity after the floor (scan ``last``), and its
job scores only the traces that start after it (``after`` = the floor, or the previous
segment's last activity when that is later), so a session that straddles the floor is judged
on its new turns only. Sessions active only while paused are never back-filled. Canary arm
configs are scanned by the same code, so pre-canary traffic cannot reach an arm.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.db import SessionLocal
from app.evaluation import online
from app.evaluation import online_scheduler as sched
from app.evaluation.models import OnlineEvalConfig, OnlineSample
from tests.evaluation.test_online_configs import ledger_row, make_agent
from tests.evaluation.test_online_scheduler import WS, harness, scan_row

CREATED = datetime(2026, 10, 25, 12, 42, tzinfo=UTC)
TIMEOUT = timedelta(minutes=2)
OLD = "a" * 32  # ended before the config existed (the live 11:11–11:12Z sessions)
NEW = "b" * 32  # started after it
STRADDLE = "c" * 32  # one turn before creation, one after


def _row(db, config_id="oe_floor-1", **cols):
    agent = make_agent(db)
    return ledger_row(db, agent, config_id, sampling_percentage=100.0,
                      session_timeout_minutes=2, created_at=CREATED, **cols)


def _samples(db, config_id="oe_floor-1"):
    return {s.session_id: s for s in db.scalars(
        select(OnlineSample).where(OnlineSample.config_id == config_id))}


def test_a_new_config_never_samples_sessions_that_ended_before_it_existed():
    db = SessionLocal()
    _row(db)
    now = CREATED + timedelta(minutes=3)
    rows = [scan_row(OLD, CREATED - timedelta(minutes=90), first=CREATED - timedelta(minutes=91)),
            scan_row(NEW, CREATED + timedelta(seconds=30), first=CREATED + timedelta(seconds=10))]
    executor, _, kwargs = harness(rows=rows)
    sched.tick(db, now, **kwargs)
    samples = _samples(db)
    assert set(samples) == {NEW}
    item = executor.submitted[0].spec["sessions"][0]
    assert item["session_id"] == NEW
    assert item["after"] == CREATED.isoformat()  # traces before the floor are never judged
    db.close()


def test_a_session_straddling_creation_is_judged_on_its_new_turns_only():
    db = SessionLocal()
    _row(db)
    now = CREATED + timedelta(minutes=5)
    rows = [scan_row(STRADDLE, CREATED + timedelta(minutes=1),
                     first=CREATED - timedelta(minutes=8))]
    executor, _, kwargs = harness(rows=rows)
    sched.tick(db, now, **kwargs)
    sample = _samples(db)[STRADDLE]
    assert sample.segment == 1
    assert sample.segment_after.replace(tzinfo=UTC) == CREATED
    assert executor.submitted[0].spec["sessions"][0]["after"] == CREATED.isoformat()
    db.close()


def test_resume_does_not_back_fill_what_was_active_while_paused(monkeypatch):
    db = SessionLocal()
    row = _row(db)
    # sampled once while enabled
    t0 = CREATED + timedelta(minutes=5)
    _ex, _, kwargs = harness(rows=[scan_row(NEW, CREATED + timedelta(minutes=1))])
    sched.tick(db, t0, **kwargs)
    assert set(_samples(db)) == {NEW}
    paused_at = t0 + timedelta(minutes=1)
    online.set_execution_status(db, WS, row.config_id, enabled=False)
    db.close()

    resumed = paused_at + timedelta(minutes=30)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return resumed

    monkeypatch.setattr(online, "datetime", _Clock)
    db = SessionLocal()
    online.set_execution_status(db, WS, row.config_id, enabled=True)
    db.close()
    monkeypatch.undo()

    db = SessionLocal()
    stored = db.scalars(select(OnlineEvalConfig).where(
        OnlineEvalConfig.config_id == row.config_id)).one()
    assert stored.sampling_since.replace(tzinfo=UTC) == resumed
    paused_only = "d" * 32
    rows = [
        scan_row(paused_only, paused_at + timedelta(minutes=10),
                 first=paused_at + timedelta(minutes=9)),  # active only while paused
        # NEW resumed while paused, then again after the resume: the next segment, after
        # the resume only
        scan_row(NEW, resumed + timedelta(minutes=2), first=CREATED + timedelta(minutes=1)),
        scan_row(STRADDLE, resumed + timedelta(minutes=1),
                 first=paused_at + timedelta(minutes=12)),
    ]
    executor, _, kwargs = harness(rows=rows)
    sched.tick(db, resumed + timedelta(minutes=6), **kwargs)
    by = {(s.session_id, s.segment): s for s in db.scalars(select(OnlineSample).where(
        OnlineSample.config_id == row.config_id))}
    assert (paused_only, 1) not in by
    assert by[(NEW, 2)].segment_after.replace(tzinfo=UTC) == resumed
    assert by[(STRADDLE, 1)].segment_after.replace(tzinfo=UTC) == resumed
    db.close()


def test_pausing_and_enabling_an_enabled_config_do_not_move_the_floor():
    db = SessionLocal()
    row = _row(db)
    online.set_execution_status(db, WS, row.config_id, enabled=True)  # already ENABLED
    assert row.sampling_since is None
    online.set_execution_status(db, WS, row.config_id, enabled=False)
    assert row.sampling_since is None
    db.close()


def test_a_canary_arm_config_follows_the_same_floor():
    db = SessionLocal()
    agent = make_agent(db)
    arm = online.create_for_owner(WS, agent, name="can_abcdef12_oet", evaluators=None,
                                  log_group="/lg/treat", service_name="svc.treat",
                                  sampling=100.0, timeout=2, db=db)
    created = arm.created_at.replace(tzinfo=UTC)
    pre_canary = "e" * 32
    canary = "f" * 32
    rows = [scan_row(pre_canary, created - timedelta(minutes=10)),
            scan_row(canary, created + timedelta(seconds=40))]
    executor, _, kwargs = harness(rows=rows)
    sched.tick(db, created + timedelta(minutes=4), **kwargs)
    assert set(_samples(db, arm.config_id)) == {canary}
    db.close()


def test_an_old_config_s_floor_beyond_the_scan_reach_adds_no_bound():
    # a floor older than any query can reach excludes nothing: the job item stays as it was
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_old-1", sampling_percentage=100.0, session_timeout_minutes=2,
               created_at=CREATED - timedelta(days=30))
    executor, _, kwargs = harness(rows=[scan_row(NEW, CREATED)])
    sched.tick(db, CREATED + timedelta(minutes=5), **kwargs)
    assert executor.submitted[0].spec["sessions"] == [
        {"session_id": NEW, "until": CREATED.isoformat()}]
    db.close()
