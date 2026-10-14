"""The opt-in background alert check (`services.alert_scheduler`).

Off by default (no thread at all); when on, one thread per process, and each tick
evaluates every workspace that has an enabled rule with notifications on — one failing
workspace never stops the others.
"""

import threading
from types import SimpleNamespace

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import AlertRule, Workspace
from app.services import alert_scheduler, alerts


def _settings(enabled: bool, interval: int = 300) -> SimpleNamespace:
    return SimpleNamespace(
        alerts_scheduler_enabled=enabled, alerts_scheduler_interval_seconds=interval
    )


def _workspace(workspace_id: str, account: str) -> None:
    db = SessionLocal()
    try:
        db.add(Workspace(id=workspace_id, name=workspace_id, account_id=account,
                         region="us-east-1", bootstrap_status="ready"))
        db.commit()
    finally:
        db.close()


def _rule(workspace_id: str, enabled: bool = True) -> None:
    db = SessionLocal()
    try:
        db.add(AlertRule(workspace_id=workspace_id, kind="error_rate", comparison="above",
                         threshold=0.1, window="24h", enabled=enabled))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def no_thread(monkeypatch):
    monkeypatch.setattr(alert_scheduler, "_THREAD", None)


def test_the_flags_default_off():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["alerts_scheduler_enabled"].default is False
    assert fields["alerts_scheduler_interval_seconds"].default == 300


def test_disabled_starts_no_thread(monkeypatch, no_thread):
    started: list[object] = []
    monkeypatch.setattr(alert_scheduler.threading, "Thread",
                        lambda *a, **k: started.append(k) or pytest.fail("thread started"))
    assert alert_scheduler.start(_settings(False)) is None
    assert started == []


def test_enabled_starts_one_thread_that_ticks(monkeypatch, no_thread):
    ticked = threading.Event()
    park = threading.Event()  # never set: the test thread parks here for good
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            park.wait()

    monkeypatch.setattr(alert_scheduler, "_sleep", fake_sleep)
    monkeypatch.setattr(alert_scheduler, "tick", lambda: ticked.set() or {})
    thread = alert_scheduler.start(_settings(True, 120))
    assert thread is not None and thread.daemon and thread.name == "alert-scheduler"
    assert ticked.wait(5), "the loop never ticked"
    # a second start (another create_app in the same process) reuses the thread
    assert alert_scheduler.start(_settings(True, 120)) is thread
    assert sleeps[:2] == [alert_scheduler.STARTUP_DELAY_SECONDS, 120]


def test_the_interval_has_a_floor():
    assert alert_scheduler.interval_seconds(_settings(True, 5)) == 60
    assert alert_scheduler.interval_seconds(_settings(True, 900)) == 900


def test_a_tick_evaluates_every_workspace_with_rules_and_notifies(monkeypatch):
    _workspace("east", "444455556666")
    _workspace("quiet", "777788889999")
    _workspace("off", "121212121212")
    _rule(DEFAULT_WORKSPACE_ID)
    _rule("east")
    _rule("off", enabled=False)  # only disabled rules: not evaluated at all
    calls: list[tuple[str, bool]] = []

    def fake_evaluate(db, workspace, *, notify_transitions=False):
        calls.append((workspace.id, notify_transitions))
        return {"firing": 1, "unknown": 0}

    monkeypatch.setattr(alerts, "evaluate_rules", fake_evaluate)
    result = alert_scheduler.tick()
    assert sorted(calls) == [(DEFAULT_WORKSPACE_ID, True), ("east", True)]
    assert result == {DEFAULT_WORKSPACE_ID: {"firing": 1, "unknown": 0},
                      "east": {"firing": 1, "unknown": 0}}


def test_one_failing_workspace_does_not_stop_the_others(monkeypatch):
    _workspace("east", "444455556666")
    _workspace("west", "333344445555")
    for workspace_id in (DEFAULT_WORKSPACE_ID, "east", "west"):
        _rule(workspace_id)
    seen: list[str] = []

    def fake_evaluate(db, workspace, *, notify_transitions=False):
        seen.append(workspace.id)
        if workspace.id == "east":
            raise RuntimeError("spoke role cannot be assumed")
        return {"firing": 0, "unknown": 1}

    monkeypatch.setattr(alerts, "evaluate_rules", fake_evaluate)
    result = alert_scheduler.tick()
    assert sorted(seen) == ["default", "east", "west"]
    assert result["east"] == {"error": "RuntimeError"}
    assert result["west"] == {"firing": 0, "unknown": 1}


def test_a_tick_survives_a_ledger_failure(monkeypatch):
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(alert_scheduler, "_workspace_ids", broken)
    assert alert_scheduler.tick() == {}


def test_a_real_tick_records_state_and_posts_once(monkeypatch):
    """End to end through the real evaluate_rules: a transition posts once."""
    _rule(DEFAULT_WORKSPACE_ID)
    db = SessionLocal()
    try:
        rule = db.query(AlertRule).one()
        rule.webhook_url = "https://hook.example/x"
        db.commit()
        rule_id = rule.id
    finally:
        db.close()
    from app.services import webhook_safety

    sent: list[dict] = []
    monkeypatch.setattr(webhook_safety, "post_json",
                        lambda url, body, **kw: sent.append(body) or (True, "HTTP 200"))
    monkeypatch.setattr(alerts, "read_value", lambda rule, db, ws: (0.5, "50/100 traces failed"))
    alert_scheduler.tick()
    alert_scheduler.tick()
    assert len(sent) == 1
    db = SessionLocal()
    try:
        stored = db.get(AlertRule, rule_id)
        assert stored.state == "firing" and stored.last_notified_at is not None
    finally:
        db.close()
