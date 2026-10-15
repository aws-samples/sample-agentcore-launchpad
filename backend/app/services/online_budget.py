"""Pause continuous judging whose payer is over budget (budgets child 3, R6).

Continuous online evaluation keeps billing on other people's live traffic until it
is paused, and reconciliation charges every evaluation to the config's creator
(``services/budget_reconcile.py``). After each reconciliation tick:

- a **standalone** config (one the console created for an agent: an
  ``online_eval_configs`` row) whose creator is a budgeted person at or over their
  limit is **paused** through the console's own pause path
  (``online.set_execution_status``); resuming it is refused there while the creator
  stays over budget;
- a config owned by an **experiment or canary** (``exp_*`` / ``can_*``, no ledger row)
  keeps running until its owner ends, so a running A/B or canary verdict is never
  broken — it is still charged;
- an **exempt** creator (an admin with no level, the config admin, the auth-disabled
  operator, a deleted account) is never paused, nor is an external config (no
  creator known).

A config this process already paused this period is not re-read on every tick (one
``GetOnlineEvaluationConfig`` per config, not per tick); the memo is cleared as soon
as its creator is back under the limit.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

from app.core.db import SessionLocal
from app.core.errors import NotFoundError
from app.evaluation.models import OnlineEvalConfig
from app.models.ledger import Workspace
from app.services import budgets

logger = logging.getLogger("launchpad.usage")

_LOCK = threading.Lock()
# config id → the period it was paused (or found paused) in by this process
_HANDLED: dict[str, str] = {}


def reset() -> None:
    with _LOCK:
        _HANDLED.clear()


def _scope(db: Any, workspace_id: str | None) -> Any:
    from app.routers.workspaces import WorkspaceScope
    from app.services.workspace import workspace_context

    row = db.get(Workspace, workspace_id or "default")
    if row is None:
        return None
    return WorkspaceScope(id=row.id, row=row, context=workspace_context(row))


def enforce(*, control_for: Any = None) -> list[dict[str, Any]]:
    """Pause every standalone config whose creator is over budget; returns what it
    paused. A config that cannot be read or paused is logged and retried on the
    next tick. ``control_for`` (tests) maps a workspace id to a control client."""
    from app.evaluation import online

    period = budgets.period_of()
    paused: list[dict[str, Any]] = []
    db = SessionLocal()
    try:
        by_creator: dict[str, list[OnlineEvalConfig]] = defaultdict(list)
        for row in db.query(OnlineEvalConfig).filter(OnlineEvalConfig.created_by.is_not(None)):
            by_creator[row.created_by].append(row)
        for user_id, configs in by_creator.items():
            payer = budgets.payer_for_user_id(db, user_id, configs[0].created_by_name)
            over = payer.budgeted and budgets.exceeded(db, payer) is not None
            if not over:
                with _LOCK:
                    for config in configs:
                        _HANDLED.pop(config.config_id, None)
                continue
            for config in configs:
                with _LOCK:
                    if _HANDLED.get(config.config_id) == period:
                        continue
                try:
                    scope = _scope(db, config.workspace_id)
                    if scope is None:
                        continue
                    control = control_for(scope.id) if control_for else None
                    current = online.get_config(db, scope, config.config_id, control)
                    if current.get("owner") == "agent" and current.get(
                            "execution_status") == "ENABLED":
                        online.set_execution_status(db, scope, config.config_id,
                                                    enabled=False, control=control)
                        paused.append({"config_id": config.config_id,
                                       "workspace_id": scope.id, "creator": user_id})
                        logger.warning(
                            "budget: paused online evaluation %s — its creator %s is over "
                            "their monthly budget", config.config_id, payer.name or user_id)
                    with _LOCK:
                        _HANDLED[config.config_id] = period
                except NotFoundError:
                    with _LOCK:  # deleted outside the console: nothing left to pause
                        _HANDLED[config.config_id] = period
                except Exception as exc:  # noqa: BLE001 — one config must not stop the rest
                    logger.warning("budget: could not pause online evaluation %s: %s: %s",
                                   config.config_id, type(exc).__name__, exc)
    finally:
        db.close()
    return paused
