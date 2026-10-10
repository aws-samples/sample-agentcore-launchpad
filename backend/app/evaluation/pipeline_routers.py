"""Console V2 数据处理 API — observed sessions into local evaluation datasets.

* `POST /api/eval/datasets/from-sessions` — add chosen trajectories (sessions) to
  an existing dataset or a new one (the trajectory list/detail "加入数据集").
* `/api/eval/pipelines` — saved processing tasks (source filter → extraction →
  output dataset) that run on demand, in the background. A source reads observed
  traces or (`type="logs"`) CloudWatch log groups through a format rule
  (`pipeline_logs`); `POST /api/eval/pipelines/preview-logs` shows what a logs
  source would extract.
* `GET /api/eval/agents/{agent_id}/log-streams` — the log streams of an agent's
  runtime log group, keyword-filtered (the task wizard's 日志 data source).
* `GET /api/eval/log-services` · `/log-groups` · `/log-sessions` — service-name,
  log-group and session discovery behind a task that evaluates CloudWatch
  telemetry with no platform agent.

Sessions are read through the observability service with the same visibility
rule as `/api/observability/sessions`: another principal's private assistant
session is never read, and a session that is not visible is skipped, not leaked.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.principal import principal_of
from app.assistant.sessions import PrivateSessions
from app.core.db import SessionLocal, get_db
from app.core.errors import AppError, NotFoundError
from app.evaluation import log_streams, pipeline_logs, pipelines
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
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import observability

router = APIRouter(prefix="/api/eval", tags=["evaluation"])

RangeKey = Literal["1h", "6h", "24h", "7d"]
SessionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-#:.@]{8,256}$")]
LogGroupName = Annotated[str, Field(pattern=LOG_GROUP_NAME_RE)]


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
    keep_replies: bool = True,
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
            keep_replies=keep_replies,
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
    """Observed traces (`agent` / `status` filters) or, with `type="logs"`, the
    records of `log_groups` converted by `format`. A row saved before log
    sources existed has no `type` — it reads as traces."""

    type: Literal["traces", "logs"] = "traces"
    agent: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    range: RangeKey = "24h"
    status: Literal["all", "ok", "error"] = "all"
    max_sessions: int = Field(default=20, ge=1, le=pipelines.MAX_SESSIONS_PER_CALL)
    log_groups: list[LogGroupName] = Field(default_factory=list, max_length=10)
    keyword: str | None = Field(default=None, max_length=128)
    format: pipeline_logs.LogFormat | None = None

    @model_validator(mode="after")
    def _logs_fields(self) -> PipelineSource:
        if self.type == "logs":
            if not self.log_groups:
                raise ValueError("a logs source needs at least one log group")
            if self.format is None:
                self.format = pipeline_logs.LogFormat()
        return self


class PipelineProcessing(BaseModel):
    first_turn_only: bool = False
    dedupe: bool = True
    min_input_chars: int = Field(default=0, ge=0, le=500)
    # false: inputs only — the observed replies are not stored as expected output
    keep_replies: bool = True


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
    for row in rows:
        _reap(db, row)
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
    row = _pipeline_in(db, ws, pipeline_id)
    _reap(db, row)
    return _pipeline_out(row)


@router.put("/pipelines/{pipeline_id}")
def update_pipeline(
    pipeline_id: str,
    body: PipelineBody,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    row = _pipeline_in(db, ws, pipeline_id)
    _reap(db, row)
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


@router.post("/pipelines/{pipeline_id}/run", status_code=202)
def run_pipeline(
    pipeline_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Start one run in the background and return the row as `running` — a run
    reads up to `max_sessions` session details (seconds each), far too long to
    hold the request open; the outcome lands on `status` + `last_run`."""
    row = _pipeline_in(db, ws, pipeline_id)
    _reap(db, row)
    if row.status == "running":
        raise AppError("pipeline.running", "the pipeline is already running", status_code=409)
    config = PipelineBody.model_validate({"name": row.name, **row.config})
    private = _private(request, ws, db)  # snapshot under the caller's principal
    row.status = "running"
    db.commit()
    with _live_lock:
        _live_runs.add(row.id)
    _spawn(lambda: _run_pipeline(row.id, config, ws, private))
    return _pipeline_out(row)


_live_runs: set[str] = set()
_live_lock = threading.Lock()


def _spawn(fn: Any) -> None:
    """Run ``fn`` on a daemon thread (tests replace this to run inline)."""
    threading.Thread(target=fn, name="run-pipeline", daemon=True).start()


def _reap(db: Session, row: EvalPipeline) -> None:
    """A `running` row whose thread died with a previous server process fails."""
    if row.status != "running":
        return
    with _live_lock:
        if row.id in _live_runs:
            return
    row.status = "failed"
    row.last_run = {**(row.last_run or {}), "at": _now().isoformat(),
                    "error": "the run was interrupted (the server restarted while it ran)"}
    db.commit()


def _run_pipeline(
    pipeline_id: str, config: PipelineBody, ws: WorkspaceScope, private: PrivateSessions
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
            kind = "predefined"
            if out.dataset_id:
                target = _dataset_in(db, ws, out.dataset_id)
                _require_receivable(target)
                kind = target.kind
            if src.type == "logs":
                items, skipped, log_stats = _read_log_items(ws, private, src, proc, kind=kind)
                outcome["scanned"] = log_stats.pop("sessions_found")
                outcome["matched"] = log_stats.pop("sessions_taken")
                outcome["log"] = log_stats
            else:
                listing = observability.list_sessions(src.range, db, ws.context)
                rows = [r for r in listing.get("sessions", [])
                        if private.visible(r.get("session_id"))]
                outcome["scanned"] = len(rows)
                picked = pipelines.select_sessions(
                    rows, agent=src.agent, status=src.status, limit=src.max_sessions
                )
                outcome["matched"] = len(picked)
                items, skipped = _read_items(
                    db, ws, private, [str(r["session_id"]) for r in picked], src.range,
                    kind=kind, first_turn_only=proc.first_turn_only,
                    min_input_chars=proc.min_input_chars, keep_replies=proc.keep_replies,
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


# ─── log sources (数据处理 · 运行日志) ─────────────────────────────────────────
def _convert_logs(
    ws: WorkspaceScope, private: PrivateSessions, src: PipelineSource
) -> tuple[list[dict[str, str]], bool, dict[str, Any]]:
    """(raw rows, truncated, converted sessions) for a logs source."""
    assert src.format is not None  # the model fills the default preset
    rows, truncated = pipeline_logs.fetch_events(
        ws.context.client("logs"), list(dict.fromkeys(src.log_groups)),
        observability.RANGE_HOURS[src.range], src.keyword, src.format.preset,
    )
    converted = pipeline_logs.convert(
        rows, src.format, max_sessions=src.max_sessions, visible=private.visible
    )
    # a transcript naming another principal's private session is not read either
    converted["sessions"] = [
        s for s in converted["sessions"] if not private.mentions_hidden(s["turns"])
    ]
    return rows, truncated, converted


def _read_log_items(
    ws: WorkspaceScope,
    private: PrivateSessions,
    src: PipelineSource,
    proc: PipelineProcessing,
    *,
    kind: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    """Items from a logs source + a skip reason per session + the parse stats."""
    _rows, truncated, converted = _convert_logs(ws, private, src)
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for session in converted["sessions"]:
        found = pipelines.items_from_transcript(
            session["session_id"], session["turns"], kind=kind,
            first_turn_only=proc.first_turn_only, min_input_chars=proc.min_input_chars,
            keep_replies=proc.keep_replies, source="logs",
            metadata={"log_group": session["log_group"]} if session["log_group"] else None,
        )
        if not found:
            skipped.append({"session_id": session["session_id"], "reason": "no_exchange"})
            continue
        items.extend(found)
    stats = {
        "events": converted["events"], "parsed": converted["parsed"],
        "failed": converted["failed"], "truncated": truncated,
        "sessions_found": converted["sessions_found"],
        "sessions_taken": len(converted["sessions"]),
    }
    return items, skipped, stats


class LogsPreview(BaseModel):
    source: PipelineSource


PREVIEW_TURN_CHARS = 1000


@router.post("/pipelines/preview-logs")
def preview_logs(
    body: LogsPreview,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """What a logs source reads and extracts right now — the raw newest records
    beside the sessions the format rule turns them into (the converter a run
    uses), with per-reason parse failures and the field paths seen in records."""
    src = body.source
    if src.type != "logs":
        raise AppError("pipeline.not_logs", "only a logs source can be previewed",
                       status_code=422)
    rows, truncated, converted = _convert_logs(ws, _private(request, ws, db), src)
    sessions = [
        {**s, "turns": [{**t, "text": str(t.get("text") or "")[:PREVIEW_TURN_CHARS]}
                        for t in s["turns"]]}
        for s in converted["sessions"][:pipeline_logs.MAX_PREVIEW_SESSIONS]
    ]
    return {**converted, "sessions": sessions, "truncated": truncated,
            "samples": pipeline_logs.samples(rows)}


# ─── runtime log streams (task wizard · 日志) ────────────────────────────────
@router.get("/agents/{agent_id}/log-streams")
def agent_log_streams(
    agent_id: str,
    request: Request,
    hours: int = Query(24, ge=1, le=336),
    q: str | None = Query(None, max_length=128),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Recent streams of the agent's runtime log group, newest first; with `q`
    only those whose name or content contains the keyword. A session stream
    carries its `session_id` — the unit a batch evaluation scores."""
    agent = _agent_in(db, ws, agent_id)
    logs = ws.context.client("logs")
    _service, log_group = resolve_telemetry(agent, ws.context, logs)
    since_ms = int((_now().timestamp() - hours * 3600) * 1000)
    out = log_streams.list_streams(
        logs, log_group, since_ms=since_ms, keyword=q,
        visible=_private(request, ws, db).visible,
    )
    return {**out, "hours": hours, "q": (q or "").strip() or None}


# ─── CloudWatch-only sources (task wizard · no platform agent) ───────────────


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
