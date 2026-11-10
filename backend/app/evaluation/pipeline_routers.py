"""Console V2 数据处理 API — observed sessions into local evaluation datasets.

* `POST /api/eval/datasets/from-sessions` — add chosen trajectories (sessions) to
  an existing dataset or a new one (the trajectory list/detail "加入数据集").
* `/api/eval/pipelines` — saved processing tasks (source filter → extraction →
  output dataset) that run on demand, in the background (upstream #239).
* `GET /api/eval/agents/{agent_id}/log-streams` — the log streams of an agent's
  runtime log group, keyword-filtered (the task wizard's 日志 data source).
* `GET /api/eval/log-services` · `/log-groups` · `/log-sessions` — service-name,
  log-group and session discovery behind a task that evaluates CloudWatch
  telemetry with no platform agent.

Sessions are read through the observability service with the same visibility
rule as `/api/observability/sessions`: another principal's private assistant
session is never read, and a session that is not visible is skipped, not leaked.

The per-session span reads (one Logs Insights pass each) run on a small thread pool
(N-SCN-obs10: nine serial reads took 22 s, then > 30 s as the window's trace volume
grew); the visibility checks and the extraction stay on the request thread, items keep
the request's session order, and the response carries a `timing` block.

Jobs run at most ``Settings.from_sessions_job_concurrency`` (default 2) at a time per
process; one past the cap waits ``queued`` on the gate (r8-a2).
Sync / async rule (round-6 follow-up, N-SCN-obs10): up to ``FROM_SESSIONS_SYNC_MAX`` (10)
sessions the route answers **201** with the dataset as before; above that it files a
``Job`` row (type ``FROM_SESSIONS_JOB_TYPE``), runs the same ``_read_items`` + ``_write`` on
a background thread and answers **202** ``{job_id, status, sessions, poll}``; the caller
polls ``GET /api/jobs/{job_id}`` — ``payload.progress {read, total}`` moves per session read,
``payload.result {dataset{id, name, kind, item_count}, added, skipped, timing}`` lands on
success and ``payload.error`` (the AppError envelope) on failure. ``?sync=false`` forces the
job for any size (the console always does, to show progress); ``?sync=true`` forces the
201 shape and is refused (400 ``from_sessions.sync_too_large``) above the cap. A job a
restart interrupted is marked failed on startup (``sweep_interrupted_from_sessions_jobs``)
rather than re-run: the write is the last step and re-running could create the dataset twice.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.principal import principal_of
from app.assistant.sessions import PrivateSessions
from app.core.config import get_settings
from app.core.db import SessionLocal, get_db
from app.core.display_time import DisplayTimeJSONResponse
from app.core.errors import AppError, NotFoundError
from app.evaluation import log_streams, pipelines
from app.evaluation.models import EvalDataset, EvalPipeline
from app.evaluation.online_routers import _agent_in
from app.evaluation.routers import (
    LOG_GROUP_NAME_RE,
    SERVICE_NAME_RE,
    _dataset_in,
    _dataset_out,
    _infer_kind,
    _validate_items,
)
from app.evaluation.service import resolve_telemetry
from app.models.ledger import Job
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import creators, observability, visibility
from app.services.creators import Creator
from app.services.visibility import Viewer, require_viewer
from app.services.workspace import WorkspaceContext, context_for_workspace

router = APIRouter(prefix="/api/eval", tags=["evaluation"])
logger = logging.getLogger(__name__)

RangeKey = Literal["1h", "6h", "24h", "7d"]
# N-SCN-obs10: concurrent per-session span reads. Small on purpose — each read is a
# Logs Insights query (billed per scan, 60 s TTL cache on the service).
FROM_SESSIONS_WORKERS = 4
# N-SCN-obs10 round-6: at most this many sessions are read inside the request (201);
# more → a 202 job (see the module docstring). 10 sessions answered in 9 s on the live
# environment after r7-G; the console never relies on this (it always asks for the job).
FROM_SESSIONS_SYNC_MAX = 10
FROM_SESSIONS_JOB_TYPE = "eval_dataset_from_sessions"
FROM_SESSIONS_STAGE = "from_sessions"
SessionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-#:.@]{8,256}$")]


def _now() -> datetime:
    return datetime.now(UTC)


def _read_session_detail(
    session_id: str, range_key: str, workspace: Any
) -> dict[str, Any] | None:
    """One session's spans + transcript on its own ledger session — SQLAlchemy sessions
    are not thread-safe and the pool below runs these concurrently. ``None`` = not found."""
    db = SessionLocal()
    try:
        return observability.get_session(session_id, range_key, db, workspace)
    except NotFoundError:
        return None
    finally:
        db.close()


Progress = Callable[[int, int], None]


def _read_details(
    session_ids: list[str], range_key: str, workspace: Any, *, workers: int,
    progress: Progress | None = None,
) -> list[dict[str, Any] | None]:
    """The details of ``session_ids`` in the same order, read ``workers`` at a time.
    ``progress(done, total)`` fires after each read completes (the job's progress block)."""
    total = len(session_ids)
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total)

    if total <= 1 or workers <= 1:
        out: list[dict[str, Any] | None] = []
        for s in session_ids:
            out.append(_read_session_detail(s, range_key, workspace))
            tick()
        return out

    def read(s: str) -> dict[str, Any] | None:
        try:
            return _read_session_detail(s, range_key, workspace)
        finally:
            tick()

    with ThreadPoolExecutor(
        max_workers=min(workers, total), thread_name_prefix="from-sessions"
    ) as pool:
        return list(pool.map(read, session_ids))


def _read_items(
    db: Session,
    ws: WorkspaceScope,
    private: PrivateSessions,
    session_ids: list[str],
    range_key: str,
    *,
    kind: str,
    first_turn_only: bool,
    min_input_chars: int,
    workers: int = FROM_SESSIONS_WORKERS,
    progress: Progress | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    """Items extracted from each visible session + a skip reason per miss + timing.

    Three phases: visibility (ledger, request thread) → span reads (thread pool, each on
    its own ledger session) → hidden-mention check and extraction (request thread), so the
    items come out in the request's session order whatever order the reads finished in.
    ``progress`` (the job path) is told ``(read, total)`` after each span read."""
    started = time.monotonic()
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    visible = [s for s in session_ids if private.visible(s)]
    details = dict(zip(visible, _read_details(visible, range_key, ws.context, workers=workers,
                                              progress=progress),
                       strict=True))
    for session_id in session_ids:
        detail = details.get(session_id)
        if detail is None:
            skipped.append({"session_id": session_id, "reason": "not_found"})
            continue
        if private.mentions_hidden(detail):
            skipped.append({"session_id": session_id, "reason": "not_found"})
            continue
        transcript = detail.get("transcript") or {}
        if not transcript.get("available"):
            skipped.append({"session_id": session_id, "reason": "no_transcript"})
            continue
        found = pipelines.items_from_transcript(
            session_id,
            transcript.get("turns") or [],
            kind=kind,
            first_turn_only=first_turn_only,
            min_input_chars=min_input_chars,
            agent=(detail.get("summary") or {}).get("agent"),
        )
        if not found:
            skipped.append({"session_id": session_id, "reason": "no_exchange"})
            continue
        items.extend(found)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    timing = {
        "elapsed_ms": elapsed_ms,
        "sessions": len(session_ids),
        "read": len(visible),
        "workers": min(workers, len(visible)) if len(visible) > 1 else 1,
    }
    logger.info(
        "from-sessions: %d session(s) (%d read, %d worker(s)) → %d item(s) in %d ms",
        len(session_ids), len(visible), timing["workers"], len(items), elapsed_ms,
    )
    return items, skipped, timing


def _require_receivable(dataset: EvalDataset) -> None:
    if dataset.kind == "simulated":
        raise AppError(
            "dataset.kind_unsupported",
            "simulated-persona datasets cannot receive observed trajectories",
            status_code=400,
        )


def _write(
    db: Session,
    ws: WorkspaceScope,
    *,
    dataset_id: str | None,
    dataset_name: str | None,
    description: str,
    items: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    dedupe: bool,
    creator: Creator | None = None,
) -> tuple[EvalDataset, int]:
    """Append to `dataset_id`, or create `dataset_name` — only when there is
    something to write (a dataset cannot be empty). A created dataset records
    `creator`; an append leaves the existing creator alone."""
    if dataset_id:
        dataset = _dataset_in(db, ws, dataset_id)
        merged, added, reasons = pipelines.merge_items(dataset.items, items, dedupe=dedupe)
        skipped.extend({"session_id": "", "reason": r} for r in reasons)
        if added:
            _validate_items(merged)
            dataset.items = merged
            db.commit()
        return dataset, added
    merged, added, reasons = pipelines.merge_items([], items, dedupe=dedupe)
    skipped.extend({"session_id": "", "reason": r} for r in reasons)
    if not merged:
        raise AppError(
            "dataset.nothing_extracted",
            "none of the selected sessions produced a usable input/reply exchange",
            {"skipped": skipped},
            status_code=422,
        )
    _validate_items(merged)
    dataset = EvalDataset(
        workspace_id=ws.id,
        name=dataset_name or "trajectories",
        locale="en",
        description=description,
        items=merged,
        kind=_infer_kind(merged),
    )
    creators.stamp(dataset, creator)
    db.add(dataset)
    db.commit()
    return dataset, added


def _private(request: Request, ws: WorkspaceScope, db: Session) -> PrivateSessions:
    return PrivateSessions(db, ws.id, principal_of(require_identity(request)))


# ─── from-sessions as a job (N-SCN-obs10 round-6) ───────────────────────────
@dataclass(frozen=True)
class _JobScope:
    """What `_read_items` / `_write` need of a `WorkspaceScope`, rebuilt inside the worker
    from the job row's `workspace_id` (no request, no row)."""

    id: str
    context: WorkspaceContext


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _from_sessions_job_create(
    workspace_id: str, body: FromSessions, session_ids: list[str], kind: str, principal: str,
    creator: Creator | None = None,
) -> str:
    db = SessionLocal()
    try:
        job = Job(
            workspace_id=workspace_id,
            type=FROM_SESSIONS_JOB_TYPE,
            status="queued",
            payload={
                "session_ids": session_ids,
                "range": body.range,
                "dataset_id": body.dataset_id,
                "name": body.name,
                "description": body.description,
                "first_turn_only": body.first_turn_only,
                "dedupe": body.dedupe,
                "kind": kind,
                "principal": principal,
                # the dataset the job creates records whoever started it
                "creator": creators.fields(creator),
                "progress": {"read": 0, "total": len(session_ids)},
            },
            log=json.dumps({"ts": _now_iso(), "stage": FROM_SESSIONS_STAGE, "level": "info",
                            "msg": f"queued: {len(session_ids)} session(s)"},
                           ensure_ascii=False),
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _from_sessions_job_update(
    job_id: str,
    *,
    status: str | None = None,
    message: str | None = None,
    level: str = "info",
    error: str | None = None,
    payload_patch: dict[str, Any] | None = None,
) -> None:
    """Append one JSONL event (the deploy job's shape), patch the payload, move the status."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        if message is not None:
            line = json.dumps(
                {"ts": _now_iso(), "stage": FROM_SESSIONS_STAGE, "level": level, "msg": message},
                ensure_ascii=False,
            )
            job.log = (job.log + "\n" + line) if job.log else line
        if payload_patch:
            job.payload = {**(job.payload or {}), **payload_patch}
        if status is not None:
            job.status = status
        if error is not None:
            job.error = error
        db.commit()
    finally:
        db.close()


_job_gate: threading.BoundedSemaphore | None = None
_job_gate_lock = threading.Lock()


def _from_sessions_gate() -> threading.BoundedSemaphore:
    """The process-wide cap on from-sessions jobs running at once, sized from
    ``Settings.from_sessions_job_concurrency`` (default 2) on first use. Each job fans
    out ``FROM_SESSIONS_WORKERS`` Logs Insights reads, so without the gate a member filing
    N jobs would run N × 4 CloudWatch queries concurrently (r8-a2 review note B)."""
    global _job_gate
    with _job_gate_lock:
        if _job_gate is None:
            _job_gate = threading.BoundedSemaphore(get_settings().from_sessions_job_concurrency)
        return _job_gate


def _run_from_sessions_job(job_id: str) -> None:
    """Thread body: wait for a slot on the job gate (the row stays ``queued`` meanwhile),
    then run the job. The slot is released whatever the outcome."""
    gate = _from_sessions_gate()
    if not gate.acquire(blocking=False):
        _from_sessions_job_update(
            job_id,
            message="waiting for a slot: "
                    f"{get_settings().from_sessions_job_concurrency} from-sessions job(s) "
                    "run at once",
        )
        gate.acquire()
    try:
        _from_sessions_job_body(job_id)
    finally:
        gate.release()


def _from_sessions_job_body(job_id: str) -> None:
    """Body of the background job: the same three phases + write as the 201 path."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.type != FROM_SESSIONS_JOB_TYPE:
            return
        payload = dict(job.payload or {})
        workspace_id = job.workspace_id or ""
    finally:
        db.close()
    session_ids: list[str] = list(payload.get("session_ids") or [])
    total = len(session_ids)
    _from_sessions_job_update(
        job_id, status="running",
        message=f"reading spans: {total} session(s), {min(FROM_SESSIONS_WORKERS, max(total, 1))}"
                " worker(s)",
    )
    last_tick = [0.0]

    def progress(read: int, _total: int) -> None:
        # one ledger write per read, throttled to ≥ 0.2 s apart except for the last one
        now = time.monotonic()
        if read < _total and now - last_tick[0] < 0.2:
            return
        last_tick[0] = now
        _from_sessions_job_update(job_id, payload_patch={"progress": {"read": read,
                                                                      "total": _total}})

    db = SessionLocal()
    try:
        scope = _JobScope(id=workspace_id, context=context_for_workspace(workspace_id))
        private = PrivateSessions(db, workspace_id, payload.get("principal"))
        items, skipped, timing = _read_items(
            db, scope, private, session_ids, str(payload.get("range") or "24h"),  # type: ignore[arg-type]
            kind=str(payload.get("kind") or "predefined"),
            first_turn_only=bool(payload.get("first_turn_only")),
            min_input_chars=0,
            progress=progress,
        )
        _from_sessions_job_update(
            job_id,
            message=f"read: {len(items)} item(s) from {timing['read']} session(s), "
                    f"{len(skipped)} skipped, {timing['elapsed_ms']} ms",
            payload_patch={"progress": {"read": total, "total": total}},
        )
        stamped = payload.get("creator") or {}
        creator = (
            Creator(user_id=stamped.get("created_by"), name=stamped.get("created_by_name"))
            if stamped.get("created_by") or stamped.get("created_by_name") else None
        )
        dataset, added = _write(
            db, scope,  # type: ignore[arg-type]
            dataset_id=payload.get("dataset_id"), dataset_name=payload.get("name"),
            description=str(payload.get("description") or ""),
            items=items, skipped=skipped, dedupe=bool(payload.get("dedupe", True)),
            creator=creator,
        )
        result = {
            "dataset": {"id": dataset.id, "name": dataset.name, "kind": dataset.kind,
                        "item_count": len(dataset.items)},
            "added": added,
            "skipped": skipped,
            "timing": timing,
        }
    except AppError as exc:
        db.rollback()
        _from_sessions_job_update(
            job_id, status="failed", level="error", error=exc.message,
            message=f"{exc.code}: {exc.message}",
            payload_patch={"error": {"code": exc.code, "message": exc.message,
                                     "detail": exc.detail}},
        )
        return
    except Exception as exc:  # noqa: BLE001 — the job row is the report
        db.rollback()
        logger.exception("from-sessions job %s failed", job_id)
        _from_sessions_job_update(
            job_id, status="failed", level="error", error=f"{type(exc).__name__}: {exc}",
            message=f"{type(exc).__name__}: {exc}"[:500],
            payload_patch={"error": {"code": "from_sessions.failed",
                                     "message": f"{type(exc).__name__}: {exc}"[:500],
                                     "detail": None}},
        )
        return
    finally:
        db.close()
    _from_sessions_job_update(
        job_id, status="succeeded",
        message=f"dataset {result['dataset']['id']} ({result['dataset']['name']}): "
                f"+{added} item(s), {result['dataset']['item_count']} total",
        payload_patch={"result": result},
    )


def start_from_sessions_job(job_id: str) -> threading.Thread:
    thread = threading.Thread(
        target=_run_from_sessions_job, args=(job_id,), daemon=True,
        name=f"from-sessions-job-{job_id[:8]}",
    )
    thread.start()
    return thread


def sweep_interrupted_from_sessions_jobs() -> list[str]:
    """Startup: a from-sessions job a restart cut short is failed honestly, not re-run
    (the dataset write is the last step; a re-run could create the dataset twice)."""
    db = SessionLocal()
    try:
        rows = (
            db.query(Job)
            .filter(Job.type == FROM_SESSIONS_JOB_TYPE, Job.status.in_(["queued", "running"]))
            .all()
        )
        swept: list[str] = []
        for job in rows:
            job.status = "failed"
            job.error = "interrupted by a backend restart — run FROM SESSIONS again"
            line = json.dumps({"ts": _now_iso(), "stage": FROM_SESSIONS_STAGE, "level": "error",
                               "msg": job.error}, ensure_ascii=False)
            job.log = (job.log + "\n" + line) if job.log else line
            job.payload = {**(job.payload or {}),
                           "error": {"code": "from_sessions.interrupted", "message": job.error,
                                     "detail": None}}
            swept.append(job.id)
        if swept:
            db.commit()
        return swept
    finally:
        db.close()


# ─── trajectories → dataset ─────────────────────────────────────────────────
class FromSessions(BaseModel):
    session_ids: list[SessionId] = Field(
        min_length=1, max_length=pipelines.MAX_SESSIONS_PER_CALL
    )
    range: RangeKey = "24h"
    # exactly one: append to an existing local dataset, or create a new one
    dataset_id: str | None = Field(default=None, min_length=1, max_length=16)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str = Field(default="", max_length=1000)
    first_turn_only: bool = False
    dedupe: bool = True

    @model_validator(mode="after")
    def _one_target(self) -> FromSessions:
        if bool(self.dataset_id) == bool(self.name):
            raise ValueError("exactly one of dataset_id or name is required")
        return self


@router.post(
    "/datasets/from-sessions",
    status_code=201,
    responses={202: {
        "description": "N-SCN-obs10: more than FROM_SESSIONS_SYNC_MAX sessions, or "
                       "`?sync=false` — the dataset is written by a job; poll "
                       "`GET /api/jobs/{job_id}` (payload.progress / payload.result).",
        "content": {"application/json": {"schema": {"type": "object",
                                                    "additionalProperties": True}}},
    }},
)
def datasets_from_sessions(
    body: FromSessions,
    request: Request,
    sync: bool | None = Query(
        default=None,
        description="omitted: 201 up to FROM_SESSIONS_SYNC_MAX sessions, 202 job above; "
                    "true: force 201 (400 above the cap); false: always a 202 job",
    ),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    kind = "predefined"
    if body.dataset_id:
        target = _dataset_in(db, ws, body.dataset_id, viewer=viewer)
        _require_receivable(target)
        kind = target.kind
    session_ids = list(dict.fromkeys(body.session_ids))
    run_sync = sync if sync is not None else len(session_ids) <= FROM_SESSIONS_SYNC_MAX
    if run_sync and len(session_ids) > FROM_SESSIONS_SYNC_MAX:
        raise AppError(
            "from_sessions.sync_too_large",
            f"at most {FROM_SESSIONS_SYNC_MAX} sessions are read inside the request; "
            "drop ?sync=true to get a 202 job",
            {"sessions": len(session_ids), "max": FROM_SESSIONS_SYNC_MAX},
            status_code=400,
        )
    if not run_sync:
        principal = principal_of(require_identity(request))
        job_id = _from_sessions_job_create(
            ws.id, body, session_ids, kind, principal, creator=Creator.of_request(request)
        )
        start_from_sessions_job(job_id)
        logger.info("from-sessions: %d session(s) → job %s", len(session_ids), job_id)
        return DisplayTimeJSONResponse(  # type: ignore[return-value]
            status_code=202,
            content={"job_id": job_id, "status": "queued", "sessions": len(session_ids),
                     "poll": f"/api/jobs/{job_id}"},
        )
    items, skipped, timing = _read_items(
        db, ws, _private(request, ws, db), session_ids, body.range,
        kind=kind, first_turn_only=body.first_turn_only, min_input_chars=0,
    )
    dataset, added = _write(
        db, ws, dataset_id=body.dataset_id, dataset_name=body.name,
        description=body.description, items=items, skipped=skipped, dedupe=body.dedupe,
        creator=Creator.of_request(request),
    )
    return {"dataset": _dataset_out(dataset), "added": added, "skipped": skipped,
            "timing": timing}


# ─── saved pipelines ────────────────────────────────────────────────────────
class PipelineSource(BaseModel):
    agent: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    range: RangeKey = "24h"
    status: Literal["all", "ok", "error"] = "all"
    max_sessions: int = Field(default=20, ge=1, le=pipelines.MAX_SESSIONS_PER_CALL)


class PipelineProcessing(BaseModel):
    first_turn_only: bool = False
    dedupe: bool = True
    min_input_chars: int = Field(default=0, ge=0, le=500)


class PipelineOutput(BaseModel):
    dataset_id: str | None = Field(default=None, min_length=1, max_length=16)
    dataset_name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _one_target(self) -> PipelineOutput:
        if bool(self.dataset_id) == bool(self.dataset_name):
            raise ValueError("exactly one of dataset_id or dataset_name is required")
        return self


class PipelineBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=1000)
    source: PipelineSource = Field(default_factory=PipelineSource)
    processing: PipelineProcessing = Field(default_factory=PipelineProcessing)
    output: PipelineOutput


def _pipeline_out(row: EvalPipeline) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "config": row.config,
        "status": row.status,
        "last_run": row.last_run,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        **creators.fields(row),
    }


def _pipeline_in(
    db: Session, ws: WorkspaceScope, pipeline_id: str, *, viewer: Viewer = Viewer.ALL
) -> EvalPipeline:
    """The pipeline, or 404 — a foreign row and one workspace isolation hides read alike."""
    row = db.get(EvalPipeline, pipeline_id)
    if row is None or row.workspace_id != ws.id:
        raise NotFoundError("pipeline.not_found", "pipeline not found")
    visibility.require_seen(
        viewer, db, row.created_by, NotFoundError("pipeline.not_found", "pipeline not found")
    )
    return row


def _check_output(
    db: Session, ws: WorkspaceScope, output: PipelineOutput, *, viewer: Viewer = Viewer.ALL
) -> None:
    """The output dataset must exist, be receivable and be visible to the caller."""
    if output.dataset_id:
        _require_receivable(_dataset_in(db, ws, output.dataset_id, viewer=viewer))


@router.get("/pipelines")
def list_pipelines(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    rows = db.scalars(
        select(EvalPipeline)
        .where(EvalPipeline.workspace_id == ws.id)
        .where(visibility.clause(viewer, EvalPipeline.created_by))  # workspace isolation
        .order_by(EvalPipeline.created_at.desc())
    ).all()
    for row in rows:
        _reap(db, row)
    return {"pipelines": [_pipeline_out(r) for r in rows]}


@router.post("/pipelines", status_code=201)
def create_pipeline(
    body: PipelineBody,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _check_output(db, ws, body.output, viewer=viewer)
    row = EvalPipeline(
        workspace_id=ws.id,
        name=body.name,
        description=body.description,
        config=body.model_dump(include={"source", "processing", "output"}),
    )
    creators.stamp(row, Creator.of_request(request))
    db.add(row)
    db.commit()
    return _pipeline_out(row)


@router.get("/pipelines/{pipeline_id}")
def get_pipeline(
    pipeline_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    row = _pipeline_in(db, ws, pipeline_id, viewer=viewer)
    _reap(db, row)
    return _pipeline_out(row)


@router.put("/pipelines/{pipeline_id}")
def update_pipeline(
    pipeline_id: str,
    body: PipelineBody,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    row = _pipeline_in(db, ws, pipeline_id, viewer=viewer)
    _reap(db, row)
    if row.status == "running":
        raise AppError("pipeline.running", "the pipeline is running", status_code=409)
    _check_output(db, ws, body.output, viewer=viewer)
    row.name = body.name
    row.description = body.description
    row.config = body.model_dump(include={"source", "processing", "output"})
    db.commit()
    return _pipeline_out(row)


@router.delete("/pipelines/{pipeline_id}")
def delete_pipeline(
    pipeline_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    row = _pipeline_in(db, ws, pipeline_id, viewer=viewer)
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.post("/pipelines/{pipeline_id}/run", status_code=202)
def run_pipeline(
    pipeline_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Start one run in the background and return the row as `running` (upstream #239) —
    a run reads up to `max_sessions` session details (seconds each), far too long to hold
    the request open; the outcome lands on `status` + `last_run`. The caller's visibility
    rule (`PrivateSessions`) and creator are snapshotted on the request thread; the thread
    itself reads the ledger unfiltered (`Viewer.ALL`), like every worker."""
    row = _pipeline_in(db, ws, pipeline_id, viewer=viewer)
    _reap(db, row)
    if row.status == "running":
        raise AppError("pipeline.running", "the pipeline is already running", status_code=409)
    config = PipelineBody.model_validate({"name": row.name, **row.config})
    private = _private(request, ws, db)  # snapshot under the caller's principal
    # whoever starts the run creates its output dataset (not necessarily the
    # pipeline's own creator)
    creator = Creator.of_request(request)
    row.status = "running"
    db.commit()
    with _live_lock:
        _live_runs.add(row.id)
    _spawn(lambda: _run_pipeline(row.id, config, ws, private, creator))
    return _pipeline_out(row)


_live_runs: set[str] = set()
_live_lock = threading.Lock()
PIPELINE_RESTART_ERROR = "interrupted by a backend restart — run the pipeline again"


def _spawn(fn: Any) -> None:
    """Run ``fn`` on a daemon thread (tests replace this to run inline)."""
    threading.Thread(target=fn, name="run-pipeline", daemon=True).start()


def _reap(db: Session, row: EvalPipeline) -> None:
    """A `running` row whose thread is not alive in THIS process fails on read (upstream
    #239) — the in-request twin of the startup sweep ``sweep_interrupted_pipelines``."""
    if row.status != "running":
        return
    with _live_lock:
        if row.id in _live_runs:
            return
    row.status = "failed"
    row.last_run = {**(row.last_run or {}), "at": _now().isoformat(),
                    "error": PIPELINE_RESTART_ERROR}
    db.commit()


def sweep_interrupted_pipelines() -> list[str]:
    """Startup (CN, the `resume_pending_jobs` sweep style): a pipeline run the dead process
    left `running` is failed honestly, not re-run — the dataset write is its last step and
    a re-run could create the output dataset twice. Returns the swept ids."""
    db = SessionLocal()
    try:
        rows = db.query(EvalPipeline).filter(EvalPipeline.status == "running").all()
        swept: list[str] = []
        for row in rows:
            row.status = "failed"
            row.last_run = {**(row.last_run or {}), "at": _now().isoformat(),
                            "error": PIPELINE_RESTART_ERROR}
            swept.append(row.id)
        if swept:
            db.commit()
        return swept
    finally:
        db.close()


def _run_pipeline(
    pipeline_id: str, config: PipelineBody, ws: WorkspaceScope, private: PrivateSessions,
    creator: Creator,
) -> None:
    """One run, start to finish, on its own DB session; the row always reaches a
    terminal status."""
    db = SessionLocal()
    try:
        outcome: dict[str, Any] = {"at": _now().isoformat(), "scanned": 0, "matched": 0,
                                   "added": 0, "skipped": [], "dataset_id": None,
                                   "error": None}
        new_output: dict[str, Any] | None = None
        try:
            src, proc, out = config.source, config.processing, config.output
            listing = observability.list_sessions(src.range, db, ws.context)
            rows = [r for r in listing.get("sessions", [])
                    if private.visible(r.get("session_id"))]
            outcome["scanned"] = len(rows)
            picked = pipelines.select_sessions(
                rows, agent=src.agent, status=src.status, limit=src.max_sessions
            )
            outcome["matched"] = len(picked)
            kind = "predefined"
            if out.dataset_id:
                target = _dataset_in(db, ws, out.dataset_id)
                _require_receivable(target)
                kind = target.kind
            items, skipped, _timing = _read_items(
                db, ws, private, [str(r["session_id"]) for r in picked], src.range,
                kind=kind, first_turn_only=proc.first_turn_only,
                min_input_chars=proc.min_input_chars,
            )
            outcome["skipped"] = skipped
            if items or out.dataset_id:
                dataset, added = _write(
                    db, ws, dataset_id=out.dataset_id, dataset_name=out.dataset_name,
                    description=config.description, items=items, skipped=skipped,
                    dedupe=proc.dedupe, creator=creator,
                )
                outcome["added"] = added
                outcome["dataset_id"] = dataset.id
                if not out.dataset_id:
                    # the first run created the dataset: later runs append to it
                    new_output = {"dataset_id": dataset.id}
            status = "succeeded"
        except AppError as exc:
            db.rollback()
            outcome["error"] = f"{exc.code}: {exc.message}"
            status = "failed"
        except Exception as exc:  # noqa: BLE001 — surfaced on the row, never swallowed silently
            db.rollback()
            outcome["error"] = f"{type(exc).__name__}: {exc}"[:500]
            status = "failed"
        row = db.get(EvalPipeline, pipeline_id)
        if row is not None:  # deleted while it ran: nothing to record
            if new_output is not None:
                row.config = {**row.config, "output": new_output}
            row.status = status
            row.last_run = outcome
            db.commit()
    finally:
        db.close()
        # only once the outcome is durable — a read in between must not reap it
        with _live_lock:
            _live_runs.discard(pipeline_id)


# ─── runtime log streams (task wizard · 日志) ────────────────────────────────
@router.get("/agents/{agent_id}/log-streams")
def agent_log_streams(
    agent_id: str,
    request: Request,
    hours: int = Query(24, ge=1, le=336),
    q: str | None = Query(None, max_length=128),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Recent streams of the agent's runtime log group, newest first; with `q`
    only those whose name or content contains the keyword. A session stream
    carries its `session_id` — the unit a batch evaluation scores."""
    agent = _agent_in(db, ws, agent_id, viewer)
    logs = ws.context.client("logs")
    _service, log_group = resolve_telemetry(agent, ws.context, logs)
    since_ms = int((_now().timestamp() - hours * 3600) * 1000)
    out = log_streams.list_streams(
        logs, log_group, since_ms=since_ms, keyword=q,
        visible=_private(request, ws, db).visible,
    )
    return {**out, "hours": hours, "q": (q or "").strip() or None}


# ─── CloudWatch-only sources (task wizard · no platform agent) ───────────────
LogGroupName = Annotated[str, Field(pattern=LOG_GROUP_NAME_RE)]


@router.get("/log-sessions")
def log_sessions(
    request: Request,
    log_group: Annotated[list[LogGroupName], Query(min_length=1, max_length=10)],
    service_name: str = Query(pattern=SERVICE_NAME_RE),
    hours: int = Query(168, ge=1, le=336),
    q: str | None = Query(None, max_length=128),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Sessions of `service_name` in the input `log_group`s — the 日志 source of
    a task with no platform agent, in the stream-row shape of
    `/agents/{id}/log-streams` (the log group · stream each session's latest
    record came from). With `q`, only sessions with a record containing it
    (case-insensitive, spans or content logs)."""
    needle = (q or "").strip()
    queries = {"sessions": log_streams.sessions_query(service_name)}
    if needle:
        queries["hits"] = log_streams.keyword_query(needle)
    groups = list(dict.fromkeys(log_group))
    results = observability.run_insights_queries(
        queries, hours, logs=ws.context.client("logs"), log_groups=groups,
    )
    out = log_streams.sessions_from_rows(
        results["sessions"], results.get("hits"), keyword=needle or None,
        visible=_private(request, ws, db).visible,
    )
    return {**out, "log_group": ", ".join(groups), "hours": hours, "q": needle or None}


@router.get("/log-groups")
def log_groups(
    q: str | None = Query(None, max_length=128, pattern=r"^[.\-_/#A-Za-z0-9]*$"),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Log groups whose name contains `q` (case-insensitive), to pick a task's
    input log groups."""
    return log_streams.list_log_groups(ws.context.client("logs"), q or None)


@router.get("/log-services")
def log_services(
    hours: int = Query(168, ge=1, le=336),
    q: str | None = Query(None, max_length=128),
    log_group: Annotated[list[LogGroupName] | None, Query(max_length=10)] = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Service names seen in the spans of `aws/spans` (plus any `log_group`
    given — spans sent to an agent's own group), newest first, each with its
    span / session counts, last activity, the input log groups a batch
    evaluation needs (`aws/spans` + the content group ADOT names on the
    resource), the platform agent that owns it, if any, and its instrumentation
    `scopes` with `evaluable` — whether AgentCore Evaluation can read any of them
    as agent spans. `q` narrows by a case-insensitive substring of the name."""
    groups = list(dict.fromkeys([log_streams.SPANS_LOG_GROUP, *(log_group or [])]))
    results = observability.run_insights_queries(
        {"services": log_streams.SERVICES_QUERY, "scopes": log_streams.SCOPES_QUERY}, hours,
        logs=ws.context.client("logs"), log_groups=groups,
    )
    resolve = observability.build_agent_resolver(db, ws.id)
    services = log_streams.services_from_rows(
        results["services"], resolve, results.get("scopes")
    )
    needle = (q or "").strip().lower()
    if needle:
        services = [row for row in services if needle in row["service_name"].lower()]
    return {"services": services, "log_groups": groups, "hours": hours}
