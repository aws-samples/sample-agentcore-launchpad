"""The budget reconciliation thread: the reconcile tick (~10 min) and the hourly
calibration, on one daemon thread.

**On by default** (``budgets_reconcile_enabled``): hard limits depend on it, since
opaque runtimes, AgentCore fees and continuous judging are only charged here.
``LAUNCHPAD_BUDGETS_RECONCILE_ENABLED=false`` is the emergency off switch: the meters
and estimates keep working, nothing is reconciled.

Unlike the alert check it is safe on two boxes sharing one AWS account (prod +
prod2): each box reconciles only the sessions, runs and configs in its own ledger,
and calibration divides account-wide metrics by account-wide span session-time.

Modelled on ``services/alert_scheduler``: started from the app lifespan on a real
server only, at most one thread per process (the systemd unit runs one uvicorn),
a startup delay, a fixed cadence that never turns a failure into a tight loop.
Every workspace's AWS reads go through that workspace's own clients.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.services import budget_calibration, budgets, rates
from app.services import budget_reconcile as br

logger = logging.getLogger("launchpad.usage")

STARTUP_DELAY_SECONDS = 60
MIN_INTERVAL_SECONDS = 60

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
# Test seam: a test thread parks here instead of waking up mid-suite.
_sleep = time.sleep


def interval_seconds(settings: Settings | None = None) -> int:
    current = settings or get_settings()
    return max(MIN_INTERVAL_SECONDS, int(current.budgets_reconcile_interval_seconds))


def run_once(now: datetime | None = None) -> dict[str, Any]:
    """One pass: the reconcile tick, then the calibration when its hour is due.
    Never raises."""
    outcome: dict[str, Any] = {}
    try:
        outcome["reconcile"] = br.tick(now)
    except Exception as exc:  # noqa: BLE001 — tick never raises; belt and braces
        logger.warning("budget reconcile tick failed: %s", exc)
    try:
        outcome["calibration"] = budget_calibration.calibrate(now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("budget calibration failed: %s", exc)
    return outcome


def start(settings: Settings | None = None) -> threading.Thread | None:
    """Start the daemon thread unless switched off; at most one per process."""
    global _THREAD
    current = settings or get_settings()
    if not current.budgets_reconcile_enabled:
        logger.warning("budget reconciliation is switched off "
                       "(LAUNCHPAD_BUDGETS_RECONCILE_ENABLED=false)")
        return None
    interval = interval_seconds(current)
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD

        def loop() -> None:
            # Let startup settle (resumed jobs, the price refresher) before the first read.
            _sleep(STARTUP_DELAY_SECONDS)  # nosemgrep: arbitrary-sleep
            while True:
                result = run_once()
                failures = (result.get("reconcile") or {}).get("failures") or []
                if failures:
                    logger.warning("budget reconcile: %d failure(s): %s", len(failures),
                                   failures[:3])
                # Fixed cadence; a failure never turns into a tight retry loop.
                _sleep(interval)  # nosemgrep: arbitrary-sleep

        _THREAD = threading.Thread(target=loop, daemon=True, name="budget-reconcile")
        _THREAD.start()
        logger.info("budget reconciliation started: every %d s", interval)
        return _THREAD


def running() -> bool:
    return _THREAD is not None and _THREAD.is_alive()


def status(db: Session, settings: Settings | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """What the admin Budgets tab shows: whether reconciliation runs, the last tick,
    how far behind it is, its failures, the calibration and its factors, and this
    month's spend by kind (measured vs estimated)."""
    current = settings or get_settings()
    now = now or datetime.now(UTC)
    state = br.load_state(db)
    last_tick = state.get("last_tick") or None
    window_end = br._parse_iso((last_tick or {}).get("window_end"))
    pending, oldest = br.pending_sessions(db)
    cal = rates.calibration(db)
    min_sessions = int(cal["calibration_min_sessions"])
    factors = [
        {"runtime_id": runtime_id, "runtime_name": entry.get("runtime_name")
         or br.runtime_name_of(runtime_id), "gb": entry["gb"], "vcpu": entry["vcpu"],
         "sessions": entry["sessions"], "hours": int(entry.get("hours") or 0),
         "session_hours": entry.get("session_hours"), "last_hour": entry.get("last_hour"),
         "updated_at": entry.get("updated_at"), "used": entry["sessions"] >= min_sessions,
         "last_sample": entry.get("last_sample")}
        for runtime_id, entry in sorted(rates.runtime_factors(db).items())
    ]
    period = budgets.period_of(now)
    workspaces = [
        {"workspace_id": ws_id, "last_ok_at": entry.get("last_ok_at"),
         "window_end": entry.get("window_end"), "last_error": entry.get("last_error")}
        for ws_id, entry in sorted((state.get("workspaces") or {}).items())
    ]
    return {
        "enabled": bool(current.budgets_reconcile_enabled),
        "running": running(),
        "interval_seconds": interval_seconds(current),
        "first_tick_at": state.get("first_tick_at"),
        "last_tick": last_tick,
        "last_failure_at": state.get("last_failure_at"),
        "lag_seconds": (round((now - window_end).total_seconds())
                        if window_end is not None else None),
        "pending_sessions": pending,
        "oldest_pending_at": br._iso(oldest),
        "workspaces": workspaces,
        "calibration": {
            **(state.get("calibration") or {}),
            "min_sessions": min_sessions,
            "defaults": {key: cal[key] for key in (
                "runtime_harness_gb", "runtime_harness_vcpu", "runtime_default_gb",
                "runtime_default_vcpu")},
            "factors": factors,
        },
        "period": period,
        "by_kind": br.kind_totals(db, period),
        "superseded_usd": br.superseded_usd(db, period),
    }
