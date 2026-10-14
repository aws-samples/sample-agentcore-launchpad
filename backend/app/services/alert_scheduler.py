"""The opt-in background alert check — the only caller that sends webhooks.

Off by default (`alerts_scheduler_enabled`) and turned on per box: two consoles that
share one AWS account (prod + prod2) read the same spans, so both notifying would post
every breach twice. Modelled on the model-price refresher
(`services/model_prices.start_auto_refresh`): one daemon thread per process, started
from the app lifespan on a real server only.

**Single-process assumption.** There is no claim table: the systemd unit runs one
uvicorn. A deployment running several worker processes would run one thread in each,
and each would notify on the same transition.

Each tick evaluates, in its own DB session, every workspace that has an enabled rule,
with `notify_transitions=True`. A failure in one workspace (a spoke whose role cannot be
assumed, a Logs Insights error) is logged and never stops the others or the loop.
"""

import logging
import threading
import time
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.models.ledger import AlertRule, Workspace
from app.services import alerts
from app.services.workspace import workspace_context

logger = logging.getLogger("launchpad.alerts")

# Let startup settle (resumed jobs, the price refresher) before the first billed read.
STARTUP_DELAY_SECONDS = 60
MIN_INTERVAL_SECONDS = 60

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
# Test seam: a test thread must be able to park forever instead of waking up later
# and running a real tick in the middle of another test.
_sleep = time.sleep


def _workspace_ids() -> list[str]:
    """Workspaces with at least one enabled rule — the rest would be a no-op pass."""
    db = SessionLocal()
    try:
        return list(
            db.scalars(
                select(Workspace.id)
                .where(
                    Workspace.id.in_(
                        select(AlertRule.workspace_id).where(AlertRule.enabled.is_(True))
                    )
                )
                .order_by(Workspace.id)
            )
        )
    finally:
        db.close()


def tick() -> dict[str, Any]:
    """One pass over every workspace with an enabled rule, notifying on transitions.

    Returns `{workspace_id: {"firing", "unknown"} | {"error"}}` for logging and tests.
    Never raises.
    """
    outcome: dict[str, Any] = {}
    try:
        workspace_ids = _workspace_ids()
    except Exception as exc:  # noqa: BLE001 - the loop must never die
        logger.warning("alert check: could not list workspaces: %s", exc)
        return outcome
    for workspace_id in workspace_ids:
        db = SessionLocal()
        try:
            row = db.get(Workspace, workspace_id)
            if row is None:  # deleted since the listing
                continue
            result = alerts.evaluate_rules(db, workspace_context(row), notify_transitions=True)
            outcome[workspace_id] = {"firing": result["firing"], "unknown": result["unknown"]}
        except Exception as exc:  # noqa: BLE001 - one workspace must not stop the others
            db.rollback()
            logger.warning(
                "alert check failed for workspace %s: %s: %s",
                workspace_id, type(exc).__name__, exc,
            )
            outcome[workspace_id] = {"error": type(exc).__name__}
        finally:
            db.close()
    return outcome


def interval_seconds(settings: Settings | None = None) -> int:
    current = settings or get_settings()
    return max(MIN_INTERVAL_SECONDS, int(current.alerts_scheduler_interval_seconds))


def start(settings: Settings | None = None) -> threading.Thread | None:
    """Start the daemon thread when enabled; at most one per process."""
    global _THREAD
    current = settings or get_settings()
    if not current.alerts_scheduler_enabled:
        return None
    interval = interval_seconds(current)
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD

        def loop() -> None:
            # Background checks intentionally wait for startup to settle.
            _sleep(STARTUP_DELAY_SECONDS)  # nosemgrep: arbitrary-sleep
            while True:
                try:
                    result = tick()
                    if result:
                        logger.info("alert check: %s", result)
                except Exception as exc:  # noqa: BLE001 - tick never raises; belt and braces
                    logger.warning("alert check tick failed: %s", exc)
                # Fixed cadence; a failure never turns into a tight retry loop.
                _sleep(interval)  # nosemgrep: arbitrary-sleep

        _THREAD = threading.Thread(target=loop, daemon=True, name="alert-scheduler")
        _THREAD.start()
        logger.info("alert scheduler started: every %d s", interval)
        return _THREAD
