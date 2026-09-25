"""Console V2 数据处理 API — observed sessions into local evaluation datasets.

* `POST /api/eval/datasets/from-sessions` — add chosen trajectories (sessions) to
  an existing dataset or a new one (the trajectory list/detail "加入数据集").
* `/api/eval/pipelines` — saved processing tasks (source filter → extraction →
  output dataset) that run on demand.

Sessions are read through the observability service with the same visibility
rule as `/api/observability/sessions`: another principal's private assistant
session is never read, and a session that is not visible is skipped, not leaked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.principal import principal_of
from app.assistant.sessions import PrivateSessions
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.evaluation import pipelines
from app.evaluation.models import EvalDataset, EvalPipeline
from app.evaluation.routers import _dataset_in, _dataset_out, _infer_kind, _validate_items
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import observability

router = APIRouter(prefix="/api/eval", tags=["evaluation"])

RangeKey = Literal["1h", "6h", "24h", "7d"]
SessionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-#:.@]{8,256}$")]


def _now() -> datetime:
    return datetime.now(UTC)


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
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Items extracted from each visible session + a skip reason per miss."""
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for session_id in session_ids:
        if not private.visible(session_id):
            skipped.append({"session_id": session_id, "reason": "not_found"})
            continue
        try:
            detail = observability.get_session(session_id, range_key, db, ws.context)
        except NotFoundError:
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
    return items, skipped


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
) -> tuple[EvalDataset, int]:
    """Append to `dataset_id`, or create `dataset_name` — only when there is
    something to write (a dataset cannot be empty)."""
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
    db.add(dataset)
    db.commit()
    return dataset, added


def _private(request: Request, ws: WorkspaceScope, db: Session) -> PrivateSessions:
    return PrivateSessions(db, ws.id, principal_of(require_identity(request)))


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


@router.post("/datasets/from-sessions", status_code=201)
def datasets_from_sessions(
    body: FromSessions,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    kind = "predefined"
    if body.dataset_id:
        target = _dataset_in(db, ws, body.dataset_id)
        _require_receivable(target)
        kind = target.kind
    items, skipped = _read_items(
        db, ws, _private(request, ws, db), list(dict.fromkeys(body.session_ids)), body.range,
        kind=kind, first_turn_only=body.first_turn_only, min_input_chars=0,
    )
    dataset, added = _write(
        db, ws, dataset_id=body.dataset_id, dataset_name=body.name,
        description=body.description, items=items, skipped=skipped, dedupe=body.dedupe,
    )
    return {"dataset": _dataset_out(dataset), "added": added, "skipped": skipped}


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
    }


def _pipeline_in(db: Session, ws: WorkspaceScope, pipeline_id: str) -> EvalPipeline:
    row = db.get(EvalPipeline, pipeline_id)
    if row is None or row.workspace_id != ws.id:
        raise NotFoundError("pipeline.not_found", "pipeline not found")
    return row


def _check_output(db: Session, ws: WorkspaceScope, output: PipelineOutput) -> None:
    if output.dataset_id:
        _require_receivable(_dataset_in(db, ws, output.dataset_id))


@router.get("/pipelines")
def list_pipelines(
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    rows = db.scalars(
        select(EvalPipeline)
        .where(EvalPipeline.workspace_id == ws.id)
        .order_by(EvalPipeline.created_at.desc())
    ).all()
    return {"pipelines": [_pipeline_out(r) for r in rows]}


@router.post("/pipelines", status_code=201)
def create_pipeline(
    body: PipelineBody,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _check_output(db, ws, body.output)
    row = EvalPipeline(
        workspace_id=ws.id,
        name=body.name,
        description=body.description,
        config=body.model_dump(include={"source", "processing", "output"}),
    )
    db.add(row)
    db.commit()
    return _pipeline_out(row)


@router.get("/pipelines/{pipeline_id}")
def get_pipeline(
    pipeline_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _pipeline_out(_pipeline_in(db, ws, pipeline_id))


@router.put("/pipelines/{pipeline_id}")
def update_pipeline(
    pipeline_id: str,
    body: PipelineBody,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = _pipeline_in(db, ws, pipeline_id)
    if row.status == "running":
        raise AppError("pipeline.running", "the pipeline is running", status_code=409)
    _check_output(db, ws, body.output)
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
) -> dict[str, Any]:
    row = _pipeline_in(db, ws, pipeline_id)
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.post("/pipelines/{pipeline_id}/run")
def run_pipeline(
    pipeline_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Run once, synchronously (bounded: at most `max_sessions` session reads)."""
    row = _pipeline_in(db, ws, pipeline_id)
    if row.status == "running":
        raise AppError("pipeline.running", "the pipeline is already running", status_code=409)
    config = PipelineBody.model_validate({"name": row.name, **row.config})
    row.status = "running"
    db.commit()

    private = _private(request, ws, db)
    outcome: dict[str, Any] = {"at": _now().isoformat(), "scanned": 0, "matched": 0,
                               "added": 0, "skipped": [], "dataset_id": None, "error": None}
    try:
        src, proc, out = config.source, config.processing, config.output
        listing = observability.list_sessions(src.range, db, ws.context)
        rows = [r for r in listing.get("sessions", []) if private.visible(r.get("session_id"))]
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
        items, skipped = _read_items(
            db, ws, private, [str(r["session_id"]) for r in picked], src.range,
            kind=kind, first_turn_only=proc.first_turn_only,
            min_input_chars=proc.min_input_chars,
        )
        outcome["skipped"] = skipped
        if items or out.dataset_id:
            dataset, added = _write(
                db, ws, dataset_id=out.dataset_id, dataset_name=out.dataset_name,
                description=config.description, items=items, skipped=skipped,
                dedupe=proc.dedupe,
            )
            outcome["added"] = added
            outcome["dataset_id"] = dataset.id
            if not out.dataset_id:
                # the first run created the dataset: later runs append to it
                row.config = {**row.config, "output": {"dataset_id": dataset.id}}
        row.status = "succeeded"
    except AppError as exc:
        outcome["error"] = f"{exc.code}: {exc.message}"
        row.status = "failed"
    except Exception as exc:  # noqa: BLE001 — surfaced on the row, never swallowed silently
        outcome["error"] = f"{type(exc).__name__}: {exc}"[:500]
        row.status = "failed"
    row.last_run = outcome
    db.commit()
    return _pipeline_out(row)
