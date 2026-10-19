"""Durable per-project snapshots (parent design §3, R5).

Session storage is wiped by a runtime version update (once the live session ends)
and after 14 idle days, so after every settled turn, after a manual save
(debounced by the watcher) and before every deploy the backend asks the runtime
to tar the project to a presigned PUT under ``coding/<ws>/<project>/snapshots/``.
The newest snapshot's key is the project's ``snapshot_key``: every later action
carries its presigned GET as ``restore_url`` (``runtime_client.project_call``).

One snapshot per project at a time (``project_lock``, process-local — the box runs
one uvicorn); a delete takes the same lock, so a snapshot can never re-create
objects under a prefix being deleted. A failed background snapshot marks the
project dirty, so the watcher retries it (backed off).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, update

from app.core.db import SessionLocal
from app.services.workspace import WorkspaceContext, context_for_workspace

from . import runtime_client, s3urls
from .models import PROJECT_DELETING, CodingProject

logger = logging.getLogger("launchpad.coding")

RETRY_BACKOFF_S = 300.0

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_FAILED_AT: dict[str, float] = {}
_clock = time.monotonic  # injectable in tests


def _spawn(fn: Callable[[], None], name: str) -> None:
    """Run ``fn`` off the caller's thread (tests replace this with an inline call)."""
    threading.Thread(target=fn, daemon=True, name=name).start()


@contextmanager
def project_lock(project_id: str) -> Iterator[None]:
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(project_id, threading.Lock())
    with lock:
        yield


def backing_off(project_id: str) -> bool:
    """A recent background failure: the watcher waits before trying again."""
    failed = _FAILED_AT.get(project_id)
    return failed is not None and _clock() - failed < RETRY_BACKOFF_S


def take(project_id: str, *, ctx: WorkspaceContext | None = None,
         reason: str = "manual") -> dict[str, Any] | None:
    """Snapshot the project now; ``{key, bytes, files, ms}`` or None when the
    project is gone. Raises ``CodingRuntimeError`` (or ``AppError`` when the
    runtime is not installed) on failure — the caller decides how loud."""
    with project_lock(project_id):
        db = SessionLocal()
        try:
            row = db.get(CodingProject, project_id)
            if row is None or row.status == PROJECT_DELETING:
                return None
            ctx = ctx or context_for_workspace(row.workspace_id)
            started = datetime.now(UTC)
            key = s3urls.snapshot_key(row.workspace_id or "", row.id, started)
            put_url = s3urls.presign_put(ctx, key)
            result = runtime_client.project_call(ctx, row, "snapshot", put_url=put_url) or {}
            done = datetime.now(UTC)
            db.execute(
                update(CodingProject)
                .where(CodingProject.id == project_id)
                .values(snapshot_key=key, snapshot_at=done)
                .execution_options(synchronize_session=False)
            )
            # a save that landed while the tar ran stays dirty for the next snapshot
            db.execute(
                update(CodingProject)
                .where(CodingProject.id == project_id,
                       or_(CodingProject.dirty_at.is_(None), CodingProject.dirty_at <= started))
                .values(dirty_at=None)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            workspace_id = row.workspace_id or ""
        finally:
            db.close()
        _FAILED_AT.pop(project_id, None)
        try:
            s3urls.prune_snapshots(ctx, workspace_id, project_id, keep_key=key)
        except Exception as exc:  # noqa: BLE001 — an extra old snapshot costs a few KB
            logger.warning("coding project %s: snapshot prune failed: %s", project_id, exc)
    logger.info("coding project %s: snapshot (%s) %s bytes in %s ms", project_id, reason,
                result.get("bytes"), result.get("ms"))
    return {"key": key, **{k: result.get(k) for k in ("bytes", "files", "ms")}}


def _mark_dirty(project_id: str) -> None:
    db = SessionLocal()
    try:
        db.execute(
            update(CodingProject)
            .where(CodingProject.id == project_id, CodingProject.dirty_at.is_(None))
            .values(dirty_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        db.commit()
    finally:
        db.close()


def take_quietly(project_id: str, *, ctx: WorkspaceContext | None = None,
                 reason: str = "background") -> bool:
    """``take`` that never raises: a failure is logged, the project marked dirty
    and the watcher's retry backed off."""
    try:
        return take(project_id, ctx=ctx, reason=reason) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("coding project %s: snapshot (%s) failed: %s", project_id, reason, exc)
        _FAILED_AT[project_id] = _clock()
        try:
            _mark_dirty(project_id)
        except Exception:  # noqa: BLE001
            logger.exception("coding project %s: could not mark it dirty", project_id)
        return False


def request(project_id: str, *, ctx: WorkspaceContext | None = None,
            reason: str = "settle") -> None:
    """Snapshot in the background (a settle on the SSE path must not hold the
    stream for the tar + upload)."""
    _spawn(lambda: take_quietly(project_id, ctx=ctx, reason=reason),
           name=f"coding-snapshot-{project_id[:8]}")
