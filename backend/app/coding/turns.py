"""Coding turns: start, interrupt, and the idempotent settlement (parent design §7.5).

**Start.** The route gate already checked the payer's spend (``Billable``); the
handler refuses an unpriced model (``budgets.check_model``), claims the project
with a conditional ``UPDATE … WHERE status='idle'`` (one turn per project — a
second start is ``409 coding.turn_running``), asks the runtime to start the turn
(it returns at once; the turn runs in the microVM under ``/ping HealthyBusy``),
records the ``coding_turns`` row and maps the project's session to its payer in
``usage_sessions`` — so reconciliation knows a turn that runs for hours from its
first minute. Nothing is charged up front.

**Settle.** The runtime appends ``launchpad/turnUsage`` (or
``launchpad/turnAbandoned``) right after a turn ends, carrying the turn's token
usage (codex ``TokenUsageBreakdown``). Whichever reader sees it first — the SSE
proxy or the watcher — settles: one ``UPDATE coding_turns SET settled_at=… WHERE
… AND settled_at IS NULL``; only the winner charges the payer's budget
(``UsageMeter``) and requests a snapshot. The project goes back to ``idle`` on
every attempt (conditional on its active turn), so an interrupted settlement
heals on the next one.

**Usage.** codex's ``inputTokens`` *includes* ``cachedInputTokens`` and
``cacheWriteInputTokens`` (live 2026-10-19: 94 630 = 58 007 + 36 613 + 10), and
``outputTokens`` includes ``reasoningOutputTokens``. The meter gets them
disjoint: fresh input, cache read, cache write, output — never double counted.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.billing_policy import SOURCE_CODING
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.services import budgets
from app.services.usage_meter import USAGE_TURN, UsageMeter
from app.services.workspace import WorkspaceContext, context_for_workspace

from . import infra, install, runtime_client, snapshots
from .models import (
    PROJECT_IDLE,
    PROJECT_RUNNING,
    PROMPT_STORED_MAX,
    TURN_ABANDONED,
    TURN_COMPLETED,
    TURN_FAILED,
    TURN_INTERRUPTED,
    TURN_RUNNING,
    CodingProject,
    CodingTurn,
)
from .runtime_client import CodingRuntimeError

logger = logging.getLogger("launchpad.coding")

MAX_PROMPT_BYTES = 32 * 1024
TURN_COMPLETED_EVENT = "turn/completed"
TURN_USAGE_EVENT = "launchpad/turnUsage"
TURN_ABANDONED_EVENT = "launchpad/turnAbandoned"
SETTLE_EVENTS = (TURN_USAGE_EVENT, TURN_ABANDONED_EVENT)
REF_TYPE = "coding_turn"
_ERROR_MAX = 2000
_TERMINAL = (TURN_COMPLETED, TURN_INTERRUPTED, TURN_FAILED)


def _now() -> datetime:
    return datetime.now(UTC)


# ── models and efforts ──────────────────────────────────────────────────────


def allowed_models(ctx: WorkspaceContext) -> list[str]:
    """The configured coding models the installed runtime also allows (its role and
    container were built for the list recorded at install; a model added since
    needs a repair first)."""
    models = infra.coding_models()
    installed = (ctx.resources or {}).get(infra.KEY_MODELS)
    if isinstance(installed, list):
        models = [m for m in models if m in installed]
    return models


def resolve_model_and_effort(ctx: WorkspaceContext, model: str | None,
                             effort: str | None) -> tuple[str, str]:
    settings = get_settings()
    models = allowed_models(ctx)
    chosen = (model or "").strip() or (
        settings.coding_default_model if settings.coding_default_model in models else ""
    )
    if chosen not in models:
        raise AppError("coding.model_invalid",
                       f"model {chosen or '(none)'} is not offered by the coding runtime",
                       {"model": chosen, "models": models}, status_code=422)
    efforts = list(settings.coding_efforts)
    level = (effort or "").strip() or settings.coding_default_effort
    if level not in efforts:
        raise AppError("coding.effort_invalid", f"reasoning effort must be one of {efforts}",
                       {"effort": level, "efforts": efforts}, status_code=422)
    return chosen, level


# ── usage ───────────────────────────────────────────────────────────────────


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return 0
    return int(value)


def ledger_usage(raw: Any) -> dict[str, int] | None:
    """codex ``TokenUsageBreakdown`` → the ledger shape (codex counts kept as is)."""
    if not isinstance(raw, Mapping):
        return None
    return {
        "input": _count(raw.get("inputTokens")),
        "cached_input": _count(raw.get("cachedInputTokens")),
        "cache_write": _count(raw.get("cacheWriteInputTokens")),
        "output": _count(raw.get("outputTokens")),
        "reasoning_output": _count(raw.get("reasoningOutputTokens")),
    }


def meter_usage(usage: Mapping[str, int]) -> dict[str, int]:
    """The ledger usage as the meter's disjoint Bedrock-named counts."""
    cached = int(usage.get("cached_input") or 0)
    write = int(usage.get("cache_write") or 0)
    fresh = max(0, int(usage.get("input") or 0) - cached - write)
    return {
        "inputTokens": fresh,
        "outputTokens": int(usage.get("output") or 0),  # reasoning is part of it
        "cacheReadInputTokens": cached,
        "cacheWriteInputTokens": write,
    }


def _meter(turn: CodingTurn, project: CodingProject, runtime_arn: str | None) -> UsageMeter:
    return UsageMeter.for_user(
        turn.payer_user_id, SOURCE_CODING, user_name=turn.payer_name,
        origin_workspace_id=project.workspace_id, model_id=turn.model or None,
        session_id=project.session_id, resource_id=runtime_arn,
        ref_type=REF_TYPE, ref_id=turn.id,
    )


# ── start / interrupt ───────────────────────────────────────────────────────


def _release_claim(db: Session, project_id: str) -> None:
    db.rollback()
    db.execute(
        update(CodingProject)
        .where(CodingProject.id == project_id, CodingProject.status == PROJECT_RUNNING,
               CodingProject.active_turn_id.is_(None))
        .values(status=PROJECT_IDLE, updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _adopt_running_turn(db: Session, ctx: WorkspaceContext, project: CodingProject,
                        payer: budgets.Payer) -> bool:
    """The runtime runs a turn the ledger does not know (the backend died between
    the runtime's answer and its commit): record it on the claimed project so it is
    watched and settled. True when adopted."""
    try:
        state = runtime_client.project_call(ctx, project, "status") or {}
    except Exception as exc:  # noqa: BLE001 — the 409 stands either way
        logger.info("coding project %s: status for adoption failed: %s", project.id, exc)
        return False
    active = state.get("active_turn") if isinstance(state, dict) else None
    turn_id = str((active or {}).get("turn_id") or "")
    if not turn_id:
        return False
    known = (
        db.query(CodingTurn)
        .filter(CodingTurn.project_id == project.id, CodingTurn.turn_id == turn_id)
        .first()
    )
    if known is None:
        db.add(CodingTurn(
            workspace_id=project.workspace_id, project_id=project.id, turn_id=turn_id,
            thread_id=(active or {}).get("thread_id"), prompt="",
            model=str((active or {}).get("model") or ""),
            effort=str((active or {}).get("effort") or ""), status=TURN_RUNNING,
            payer_user_id=payer.user_id, payer_name=payer.name,
        ))
    adopted = db.execute(
        update(CodingProject)
        .where(CodingProject.id == project.id, CodingProject.status == PROJECT_RUNNING,
               CodingProject.active_turn_id.is_(None))
        .values(active_turn_id=turn_id, updated_at=_now())
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    if not adopted:
        db.rollback()
        return False
    db.commit()
    logger.warning("coding project %s: adopted running turn %s", project.id, turn_id)
    return True


def start_turn(
    db: Session,
    ctx: WorkspaceContext,
    project: CodingProject,
    *,
    prompt: str,
    model: str | None,
    effort: str | None,
    payer: budgets.Payer,
) -> CodingTurn:
    """Start one turn asynchronously and return its ledger row (``running``)."""
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AppError("coding.prompt_too_long", "the prompt is larger than 32 KB",
                       {"max_bytes": MAX_PROMPT_BYTES}, status_code=413)
    model_id, level = resolve_model_and_effort(ctx, model, effort)
    runtime_arn = runtime_client.require_runtime_arn(ctx)
    live = install.live_job(db, project.workspace_id or "")
    if live is not None and live.type == install.UNINSTALL_JOB_TYPE:
        raise AppError("coding.runtime_busy", "the coding runtime is being uninstalled",
                       {"job_id": live.id, "type": live.type}, status_code=409)
    budgets.check_model(payer, model_id)

    claimed = db.execute(
        update(CodingProject)
        .where(CodingProject.id == project.id, CodingProject.status == PROJECT_IDLE)
        .values(status=PROJECT_RUNNING, active_turn_id=None, updated_at=_now())
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if claimed != 1:
        raise AppError("coding.turn_running", "a turn is running in this project",
                       status_code=409)
    try:
        result = runtime_client.project_call(
            ctx, project, "start_turn", prompt=prompt, model=model_id, effort=level
        ) or {}
        turn_id = str(result.get("turn_id") or "")
        if not turn_id:
            raise CodingRuntimeError(runtime_client.RUNTIME_PROTOCOL,
                                     "start_turn returned no turn id")
    except CodingRuntimeError as exc:
        # a turn the ledger lost keeps the claim (adopted); anything else frees it
        if not (exc.code == "turn_running" and _adopt_running_turn(db, ctx, project, payer)):
            _release_claim(db, project.id)
        raise runtime_client.api_error(exc) from exc
    except BaseException:
        _release_claim(db, project.id)
        raise

    started = _now()
    turn = CodingTurn(
        workspace_id=project.workspace_id, project_id=project.id, turn_id=turn_id,
        thread_id=result.get("thread_id"), prompt=prompt[:PROMPT_STORED_MAX], model=model_id,
        effort=level, status=TURN_RUNNING, payer_user_id=payer.user_id,
        payer_name=payer.name, started_at=started,
    )
    db.add(turn)
    seq = _count(result.get("seq"))
    db.execute(
        update(CodingProject)
        .where(CodingProject.id == project.id)
        .values(active_turn_id=turn_id, model=model_id, effort=level, updated_at=started)
        .execution_options(synchronize_session=False)
    )
    # the watcher reads this turn's events, not the whole history before it
    db.execute(
        update(CodingProject)
        .where(CodingProject.id == project.id, CodingProject.last_seq < seq)
        .values(last_seq=seq)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(turn)
    _meter(turn, project, runtime_arn).record_session()
    return turn


def interrupt(ctx: WorkspaceContext, project: CodingProject) -> dict[str, Any]:
    """Interrupt the running turn; the runtime then completes it as ``interrupted``
    and the settlement follows the ordinary path."""
    if project.status != PROJECT_RUNNING:
        return {"interrupted": False}
    result = runtime_client.run(ctx, project, "interrupt") or {}
    return {"interrupted": bool(result.get("interrupted"))}


# ── settlement ──────────────────────────────────────────────────────────────


def _timestamp(ms: Any) -> datetime | None:
    if isinstance(ms, bool) or not isinstance(ms, int | float) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def settle(
    project_id: str,
    turn_id: str,
    *,
    status: str,
    error: str | None = None,
    usage: Any = None,
    completed_at: datetime | None = None,
    end_seq: int | None = None,
    ctx: WorkspaceContext | None = None,
) -> bool:
    """Settle one turn; True when this call won (charged + snapshot requested).

    ``usage`` is codex's ``TokenUsageBreakdown`` (or None: nothing reported);
    ``end_seq`` the event-log seq of the turn's end record. Idempotent, and safe
    to race: exactly one caller charges."""
    if not turn_id:
        return False
    db = SessionLocal()
    stamp = _now()
    won = False
    try:
        ledger = ledger_usage(usage)
        values: dict[str, Any] = {
            "settled_at": stamp,
            "status": status,
            "completed_at": completed_at or stamp,
            "error": (error or None) and str(error)[:_ERROR_MAX],
        }
        if ledger is not None:
            values["usage"] = ledger
        won = db.execute(
            update(CodingTurn)
            .where(CodingTurn.project_id == project_id, CodingTurn.turn_id == turn_id,
                   CodingTurn.settled_at.is_(None))
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount == 1
        db.commit()
        project = db.get(CodingProject, project_id)
        if won and project is not None:
            turn = (
                db.query(CodingTurn)
                .filter(CodingTurn.project_id == project_id, CodingTurn.turn_id == turn_id)
                .first()
            )
            if turn is not None:
                arn = runtime_client.installed_runtime_arn(
                    ctx or context_for_workspace(project.workspace_id)
                )
                meter = _meter(turn, project, arn)
                if ledger and sum(ledger.values()):
                    meter.on_usage(meter_usage(ledger), USAGE_TURN)
                meter.settle(failed=status in (TURN_FAILED, TURN_ABANDONED))
        # every attempt (not only the winner) frees the project, so a settlement
        # that died between its two commits heals on the next reader
        db.execute(
            update(CodingProject)
            .where(CodingProject.id == project_id, CodingProject.status == PROJECT_RUNNING,
                   CodingProject.active_turn_id == turn_id)
            .values(status=PROJECT_IDLE, active_turn_id=None, updated_at=stamp)
            .execution_options(synchronize_session=False)
        )
        if end_seq:
            db.execute(
                update(CodingProject)
                .where(CodingProject.id == project_id, CodingProject.end_seq < end_seq)
                .values(end_seq=end_seq)
                .execution_options(synchronize_session=False)
            )
        db.commit()
    finally:
        db.close()
    if won:
        logger.info("coding project %s: turn %s settled (%s)", project_id, turn_id, status)
        snapshots.request(project_id, ctx=ctx, reason="settle")
    return won


def _seq_of(record: Mapping[str, Any]) -> int | None:
    seq = record.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 else None


def _status_of(value: Any) -> str:
    return value if value in _TERMINAL else TURN_FAILED


def settle_from_records(
    project_id: str,
    records: Iterable[Mapping[str, Any]],
    completions: dict[str, Mapping[str, Any]],
    *,
    ctx: WorkspaceContext | None = None,
    failed_seqs: list[int] | None = None,
) -> list[str]:
    """Settle every turn whose end record is in ``records`` (event-log order).
    ``completions`` carries ``turn/completed`` turns across pages (their error
    message); returns the turn ids whose end was settled (or found settled). Never
    raises: the seq of an end record whose settlement failed goes to
    ``failed_seqs``, so a cursor owner (the watcher) reads it again instead of
    skipping past it."""
    ended: list[str] = []
    for record in records:
        method = record.get("method")
        params = record.get("params") if isinstance(record.get("params"), Mapping) else {}
        try:
            if method == TURN_COMPLETED_EVENT:
                turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
                if turn.get("id"):
                    completions[str(turn["id"])] = turn
            elif method == TURN_USAGE_EVENT:
                turn_id = str(params.get("turnId") or "")
                done = completions.get(turn_id) or {}
                err = done.get("error") if isinstance(done.get("error"), Mapping) else {}
                settle(project_id, turn_id, status=_status_of(params.get("status")
                                                              or done.get("status")),
                       error=err.get("message"), usage=params.get("usage"),
                       completed_at=_timestamp(record.get("ts")),
                       end_seq=_seq_of(record), ctx=ctx)
                ended.append(turn_id)
            elif method == TURN_ABANDONED_EVENT:
                turn_id = str(params.get("turnId") or "")
                reason = str(params.get("reason") or "unknown")
                settle(project_id, turn_id, status=TURN_ABANDONED,
                       error=f"the turn was abandoned ({reason})", usage=params.get("usage"),
                       completed_at=_timestamp(record.get("ts")),
                       end_seq=_seq_of(record), ctx=ctx)
                ended.append(turn_id)
        except Exception:  # noqa: BLE001 — a reader keeps reading; the watcher retries
            logger.exception("coding project %s: settling from %s failed", project_id, method)
            seq = _seq_of(record)
            if failed_seqs is not None and seq is not None:
                failed_seqs.append(seq)
    return ended
