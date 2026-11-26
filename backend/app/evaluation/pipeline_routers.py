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

import json
import threading
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.assistant.principal import principal_of
from app.assistant.sessions import PrivateSessions
from app.core.db import SessionLocal, get_db
from app.core.errors import AppError, NotFoundError, aws_error_code
from app.core.project_scope import project_context
from app.evaluation import agentcore_eval as ac
from app.evaluation import generation, log_streams, pipeline_logs, pipelines, service
from app.evaluation import routers as eval_routers
from app.evaluation.models import EvalDataset, EvalPipeline
from app.evaluation.online_routers import _agent_in
from app.evaluation.routers import (
    LOG_GROUP_NAME_RE,
    SERVICE_NAME_RE,
    _assert_log_source_in_project,
    _dataset_in,
    _dataset_out,
    _infer_kind,
    _validate_items,
)
from app.evaluation.service import EVAL_SUPPORTED_METHODS, ProjectFence, resolve_telemetry
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import creators, observability, visibility
from app.services.agentcore.client import control_client, data_client
from app.services.creators import Creator
from app.services.visibility import Viewer, require_viewer

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
    fence = ProjectFence(db, ws.id)
    for session_id in session_ids:
        if not private.visible(session_id):
            skipped.append({"session_id": session_id, "reason": "not_found"})
            continue
        try:
            detail = observability.get_session(session_id, range_key, db, ws.context)
        except NotFoundError:
            skipped.append({"session_id": session_id, "reason": "not_found"})
            continue
        if private.mentions_hidden(detail) or not _in_project(fence, detail.get("traces")):
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


def _in_project(fence: ProjectFence, traces: list[dict[str, Any]] | None) -> bool:
    """An observed session's spans come from the whole account: with several
    projects in the workspace, only a session whose services resolve to this
    project's (or a platform) agent is read — an unattributable one is not (§5.9)."""
    if not fence.closed:
        return True
    services = {t.get("service") for t in traces or [] if t.get("service")}
    return bool(services) and all(fence.allows_service(s) for s in services)


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
        dataset = _dataset_in(db, ws, dataset_id, need="own")
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    kind = "predefined"
    if body.dataset_id:
        target = _dataset_in(db, ws, body.dataset_id, viewer=viewer, need="own")
        _require_receivable(target)
        kind = target.kind
    items, skipped = _read_items(
        db, ws, _private(request, ws, db), list(dict.fromkeys(body.session_ids)), body.range,
        kind=kind, first_turn_only=body.first_turn_only, min_input_chars=0,
    )
    dataset, added = _write(
        db, ws, dataset_id=body.dataset_id, dataset_name=body.name,
        description=body.description, items=items, skipped=skipped, dedupe=body.dedupe,
        creator=Creator.of_request(request),
    )
    return {"dataset": _dataset_out(dataset), "added": added, "skipped": skipped}


# ─── saved pipelines ────────────────────────────────────────────────────────
class CustomIdentifier(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    # matched service-side (CloudWatch data protection): no lookaround, ≤ 200 chars
    regex: str = Field(min_length=1, max_length=200)


class Redaction(BaseModel):
    """PII masking applied by AgentCore before generated examples are written."""

    identifiers: list[Annotated[str, Field(pattern=r"^[A-Z0-9_]{2,64}$")]] = Field(
        default_factory=list, max_length=100
    )
    custom: list[CustomIdentifier] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _aws_rules(self) -> Redaction:
        # AWS refuses lookaround in a custom identifier, a repeated name, and a custom
        # name that shadows a managed one — refuse them at save, not at run time
        names = [c.name for c in self.custom]
        if len(set(names)) != len(names):
            raise ValueError("custom redaction identifier names must be unique")
        if set(names) & set(self.identifiers):
            raise ValueError("a custom redaction identifier cannot reuse a managed name")
        for c in self.custom:
            if any(tok in c.regex for tok in ("(?=", "(?!", "(?<=", "(?<!")):
                raise ValueError(f"redaction regex '{c.name}': lookahead/lookbehind "
                                 "is not supported")
        return self


class PipelineSource(BaseModel):
    """Observed traces (`agent` / `status` filters), with `type="logs"` the
    records of `log_groups` converted by `format`, or with `type="agentcore"` an
    AgentCore dataset generation over the sessions of the platform agent
    `agent_id` (`generation.py`). A row saved before log sources existed has no
    `type` — it reads as traces."""

    type: Literal["traces", "logs", "agentcore"] = "traces"
    agent: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    range: RangeKey = "24h"
    status: Literal["all", "ok", "error"] = "all"
    max_sessions: int = Field(default=20, ge=1, le=pipelines.MAX_SESSIONS_PER_CALL)
    log_groups: list[LogGroupName] = Field(default_factory=list, max_length=10)
    keyword: str | None = Field(default=None, max_length=128)
    format: pipeline_logs.LogFormat | None = None
    # agentcore source
    agent_id: str | None = Field(default=None, min_length=1, max_length=64)
    scenario_type: Literal["FIXED", "DYNAMIC"] = "FIXED"
    expected_trajectory: bool = True  # FIXED ground truth; ignored for DYNAMIC
    redaction: Redaction | None = None
    # an evaluation run of the agent as the session source (its bad cases: lowest
    # normalized score below max_score; null = every scored session)
    batch_run_id: str | None = Field(default=None, min_length=1, max_length=32)
    max_score: float | None = Field(default=0.7, ge=0, le=1)

    @model_validator(mode="after")
    def _logs_fields(self) -> PipelineSource:
        if self.type == "logs":
            if not self.log_groups:
                raise ValueError("a logs source needs at least one log group")
            if self.format is None:
                self.format = pipeline_logs.LogFormat()
        if self.type == "agentcore":
            if not self.agent_id:
                raise ValueError("an agentcore source needs agent_id")
            if self.max_sessions > generation.MAX_SESSIONS:
                raise ValueError(
                    f"an agentcore source sends at most {generation.MAX_SESSIONS} sessions"
                )
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
        **creators.fields(row),
    }


def _pipeline_in(
    db: Session, ws: WorkspaceScope, pipeline_id: str, *, viewer: Viewer = Viewer.ALL
) -> EvalPipeline:
    row = db.get(EvalPipeline, pipeline_id)
    if row is None or row.workspace_id != ws.id:
        raise NotFoundError("pipeline.not_found", "pipeline not found")
    visibility.require_seen(
        viewer, db, row.created_by, NotFoundError("pipeline.not_found", "pipeline not found")
    )
    return row


def _check_output(
    db: Session, ws: WorkspaceScope, output: PipelineOutput, *, viewer: Viewer,
    source: PipelineSource | None = None,
) -> None:
    target = (
        _dataset_in(db, ws, output.dataset_id, viewer=viewer, need="own")
        if output.dataset_id else None
    )
    if source is not None and source.type == "agentcore":
        _agentcore_agent(db, ws, source, viewer=viewer)
        if source.batch_run_id:
            _generation_run(db, ws, source, viewer=viewer)
        if target is not None:
            _require_generation_target(target, source.scenario_type)
        return
    if target is not None:
        _require_receivable(target)


# a FIXED generation writes replayable scenarios, a DYNAMIC one personas: the
# local row's (immutable) kind has to match what lands in it
GENERATION_TARGET_KIND = {"FIXED": "predefined", "DYNAMIC": "simulated"}


def _require_generation_target(dataset: EvalDataset, scenario_type: str) -> None:
    want = GENERATION_TARGET_KIND[scenario_type]
    if dataset.kind != want:
        raise AppError(
            "pipeline.target_kind_mismatch",
            f"a {scenario_type} generation writes into a '{want}' dataset, "
            f"not a '{dataset.kind}' one",
            {"scenario_type": scenario_type, "kind": dataset.kind},
            status_code=400,
        )


def _generation_run(
    db: Session, ws: WorkspaceScope, source: PipelineSource, *, viewer: Viewer = Viewer.ALL
) -> Any:
    """The evaluation run an agentcore source reads its sessions from: a visible,
    completed evaluators run of the source's own agent, with a batch."""
    run = eval_routers._run_in(db, ws, str(source.batch_run_id), viewer=viewer)
    reason = (
        "insights_run" if run.mode != "evaluators"
        else "not_completed" if run.status != "completed"
        else "no_batch" if not run.batch_eval_id else None
    )
    if reason:
        raise AppError("pipeline.run_not_generatable",
                       "only a completed evaluators run with a batch can feed a generation",
                       {"reason": reason}, status_code=400)
    if run.agent_id != source.agent_id:
        raise AppError("pipeline.run_agent_mismatch",
                       "the evaluation run belongs to another agent", status_code=400)
    return run


def _agentcore_agent(
    db: Session, ws: WorkspaceScope, source: PipelineSource, *, viewer: Viewer = Viewer.ALL
) -> Any:
    agent = _agent_in(db, ws, str(source.agent_id), viewer=viewer)
    if agent.method not in EVAL_SUPPORTED_METHODS:
        raise AppError(
            "eval.method_unsupported",
            f"dataset generation is not available for method '{agent.method}'",
            status_code=400,
        )
    return agent


@router.get("/pipelines")
def list_pipelines(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    rows = db.scalars(
        select(EvalPipeline)
        .where(EvalPipeline.workspace_id == ws.id)
        .where(visibility.clause(viewer, EvalPipeline.created_by))
        .order_by(EvalPipeline.created_at.desc())
    ).all()
    for row in rows:
        _reap(db, row, ws)
    return {"pipelines": [_pipeline_out(r) for r in rows]}


@router.post("/pipelines", status_code=201)
def create_pipeline(
    body: PipelineBody,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _check_output(db, ws, body.output, viewer=viewer, source=body.source)
    _refuse_foreign_groups(db, ws, body.source)
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
    _reap(db, row, ws)
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
    _reap(db, row, ws)
    if row.status == "running":
        raise AppError("pipeline.running", "the pipeline is running", status_code=409)
    _check_output(db, ws, body.output, viewer=viewer, source=body.source)
    _refuse_foreign_groups(db, ws, body.source)
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
    _reap(db, row, ws)
    if row.status == "running":
        # a run's merge commits on its own row: deleting mid-run would drop it
        raise AppError("pipeline.running", "the pipeline is running", status_code=409)
    db.delete(row)
    db.commit()
    return {"deleted": True}


def _claim(db: Session, pipeline_id: str) -> bool:
    """Mark the row `running` and register it live — atomically against another
    request or a concurrent `_reap`: the conditional UPDATE only flips a row that
    is not running yet, and the live set is filled before the commit."""
    with _live_lock:
        if pipeline_id in _live_runs:
            return False
        flipped = db.execute(
            update(EvalPipeline)
            .where(EvalPipeline.id == pipeline_id, EvalPipeline.status != "running")
            .values(status="running")
        ).rowcount
        if not flipped:
            db.rollback()
            return False
        _live_runs.add(pipeline_id)
        db.commit()
    return True


@router.post("/pipelines/{pipeline_id}/run", status_code=202)
def run_pipeline(
    pipeline_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Start one run in the background and return the row as `running` — a run
    reads up to `max_sessions` session details (seconds each), far too long to
    hold the request open; the outcome lands on `status` + `last_run`."""
    row = _pipeline_in(db, ws, pipeline_id, viewer=viewer)
    _reap(db, row, ws)
    if row.status == "running":
        raise AppError("pipeline.running", "the pipeline is already running", status_code=409)
    config = PipelineBody.model_validate({"name": row.name, **row.config})
    if config.source.type == "agentcore":
        # whoever starts the run must be able to see the agent (and the evaluation
        # run it reads), not only the pipeline's creator
        _agentcore_agent(db, ws, config.source, viewer=viewer)
        if config.source.batch_run_id:
            _generation_run(db, ws, config.source, viewer=viewer)
    private = _private(request, ws, db)  # snapshot under the caller's principal
    # whoever starts the run creates its output dataset (not necessarily the
    # pipeline's own creator)
    creator = Creator.of_request(request)
    rid, pid = row.id, row.project_id
    if not _claim(db, rid):
        raise AppError("pipeline.running", "the pipeline is already running", status_code=409)
    db.refresh(row)
    _spawn(lambda: _run_in_project(pid, rid, config, ws, private, creator=creator))
    return _pipeline_out(row)


def _run_in_project(project_id: str | None, *args: Any, **kwargs: Any) -> None:
    """A thread inherits no context: the run reads and writes as its pipeline's project
    (its output dataset is stamped there, its session reads are fenced to it)."""
    with project_context(project_id):
        _run_pipeline(*args, **kwargs)


_live_runs: set[str] = set()
_live_lock = threading.Lock()


def _spawn(fn: Any) -> None:
    """Run ``fn`` on a daemon thread (tests replace this to run inline)."""
    threading.Thread(target=fn, name="run-pipeline", daemon=True).start()


def _reap(db: Session, row: EvalPipeline, ws: WorkspaceScope | None = None) -> None:
    """A `running` row whose thread died with a previous server process fails —
    except an agentcore run whose dataset generation was already prepared: the
    generation runs on in AWS, so it is (re-)attached and merged on a thread under
    the pipeline's project instead of being thrown away. The check, the re-read of
    the row and the live registration happen under one lock, so two concurrent
    reads never resume the same run twice, and a run that just finished is not
    resumed from a stale row."""
    if row.status != "running":
        return
    resume = None
    with _live_lock:
        if row.id in _live_runs:
            return
        db.refresh(row)
        if row.status != "running":
            return
        gen = (row.last_run or {}).get("generation") or {}
        resumable = (
            ws is not None and gen.get("cloud_dataset_id")
            and gen.get("stage") in ("starting", "generating", "merging")
            and (gen.get("id") or gen.get("request"))
        )
        if resumable:
            try:
                config = PipelineBody.model_validate({"name": row.name, **row.config})
            except ValueError:  # a stored config the current model refuses
                config = None
            if config is not None:
                resume = (row.id, row.project_id, config, dict(row.last_run or {}))
                _live_runs.add(row.id)
        if resume is None:
            row.status = "failed"
            row.last_run = {**(row.last_run or {}), "at": _now().isoformat(),
                            "error": "the run was interrupted (the server restarted while "
                                     "it ran)"}
            db.commit()
    if resume is not None:
        rid, pid, config, last = resume
        _spawn(lambda: _run_in_project(pid, rid, config, ws, None, resume=last))


def _run_pipeline(
    pipeline_id: str,
    config: PipelineBody,
    ws: WorkspaceScope,
    private: PrivateSessions | None,
    *,
    creator: Creator | None = None,
    resume: dict[str, Any] | None = None,
) -> None:
    """One run, start to finish, on its own DB session; the row always reaches a
    terminal status."""
    if config.source.type == "agentcore":
        try:
            _run_agentcore(pipeline_id, config, ws, private, creator=creator, resume=resume)
        finally:
            with _live_lock:
                _live_runs.discard(pipeline_id)
        return
    assert private is not None  # only an agentcore run resumes without a caller
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
                target = _dataset_in(db, ws, out.dataset_id, need="own")
                _require_receivable(target)
                kind = target.kind
            if src.type == "logs":
                _refuse_foreign_groups(db, ws, src)
                items, skipped, log_stats = _read_log_items(ws, private, src, proc, kind=kind)
                outcome["scanned"] = log_stats.pop("sessions_found")
                outcome["matched"] = log_stats.pop("sessions_taken")
                outcome["log"] = log_stats
            else:
                listing = observability.list_sessions(src.range, db, ws.context)
                fence = ProjectFence(db, ws.id)
                rows = [r for r in listing.get("sessions", [])
                        if private.visible(r.get("session_id"))
                        and (not fence.closed or fence.allows_service(r.get("service")))]
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


# ─── agentcore source (AgentCore dataset generation) ─────────────────────────
_sleep = time.sleep  # tests replace it

# a throttled / briefly failing control call while the generation runs is retried
# within the poll budget instead of failing a run AWS is still working on
TRANSIENT_CODES = {"ThrottlingException", "TooManyRequestsException",
                   "ServiceUnavailableException", "InternalServerException"}


def _save_progress(
    db: Session, pipeline_id: str, outcome: dict[str, Any],
    *, output: dict[str, Any] | None = None,
) -> None:
    """Persist the run's progress mid-way (the generation request and id above all,
    so a restart can re-attach to it) — together with whatever dataset write is
    pending in the same session, and the pipeline's new `output` when the run just
    created its dataset."""
    row = db.get(EvalPipeline, pipeline_id)
    if row is not None:
        row.last_run = json.loads(json.dumps(outcome, default=str))
        if output is not None:
            row.config = {**row.config, "output": output}
    db.commit()


def _retention_days(ws: WorkspaceScope, log_group: str) -> int | None:
    """The log group's retention (AWS rejects a window older than it); None =
    never expire, or unreadable (the window is at most 7 days then anyway)."""
    try:
        groups = ws.context.client("logs").describe_log_groups(
            logGroupNamePrefix=log_group
        ).get("logGroups", [])
    except Exception:  # noqa: BLE001 — the clamp is a convenience, not a gate
        return None
    for group in groups:
        if group.get("logGroupName") == log_group:
            return group.get("retentionInDays")
    return None


def _delete_cloud_quietly(control: Any, cloud_id: str) -> None:
    """Remove the AWS dataset a run created for a new output (and everything the
    generation wrote into it)."""
    try:
        ac.delete_dataset(control, dataset_id=cloud_id)
    except Exception:  # noqa: BLE001 — best effort; the run reports its own error
        pass


def _cleanup_after_failure(control: Any, state: dict[str, Any]) -> str:
    """After the generation was prepared, a failing run must not leave unvetted
    examples behind: a dataset the run created for a new output (no local row yet)
    is deleted outright; otherwise this generation's examples are removed from the
    Draft — when the generation is still running that is impossible, and the note
    says the next sync removes them (the local row never holds them)."""
    cloud_id = state.get("cloud_dataset_id")
    if not cloud_id:
        return ""
    if state.get("created_dataset") and not state.get("local_dataset_id"):
        _delete_cloud_quietly(control, cloud_id)
        return " (the AWS dataset created for this run was deleted)"
    if not state.get("id"):
        return ""
    try:
        ids = [str(ex["exampleId"]) for ex in ac.list_dataset_examples(
            control, dataset_id=cloud_id, generation_id=state["id"]) if ex.get("exampleId")]
        if ids:
            ac.delete_dataset_examples(control, dataset_id=cloud_id, example_ids=ids)
        return ""
    except Exception:  # noqa: BLE001 — e.g. ConflictException: still running
        return (" (examples this generation still writes into the AWS draft are not "
                "in the local dataset; the next SYNC TO AWS removes them)")


def _run_agentcore(
    pipeline_id: str,
    config: PipelineBody,
    ws: WorkspaceScope,
    private: PrivateSessions | None,
    *,
    creator: Creator | None = None,
    resume: dict[str, Any] | None = None,
) -> None:
    """Select sessions locally → StartDatasetGeneration (identifiers only) → poll →
    pull the generated examples back into the local dataset, keeping the AWS Draft
    equal to the local items. Each stage is committed on the row; a run a restart
    interrupted re-attaches from `resume` (`_reap`): a `starting` one re-issues the
    persisted request (same client token → the same generation)."""
    db = SessionLocal()
    try:
        outcome: dict[str, Any] = dict(resume) if resume else {
            "at": _now().isoformat(), "scanned": 0, "matched": 0, "added": 0,
            "skipped": [], "dataset_id": None, "error": None, "generation": None,
        }
        try:
            if resume is None:
                assert private is not None
                prepared = _prepare_generation(db, pipeline_id, config, ws, private,
                                               outcome, creator=creator)
            else:
                prepared = True
            if prepared:
                state = outcome["generation"]
                if creator is None and state.get("creator"):
                    creator = Creator(**state["creator"])
                if not state.get("id"):
                    _issue_start(db, pipeline_id, config, ws, outcome)
                _finish_generation(db, pipeline_id, config, ws, outcome, creator=creator)
            status = "succeeded"
        except Exception as exc:  # noqa: BLE001 — surfaced on the row, never swallowed silently
            db.rollback()
            msg = (f"{exc.code}: {exc.message}" if isinstance(exc, AppError)
                   else f"{type(exc).__name__}: {exc}"[:500])
            gen = outcome.get("generation")
            if isinstance(gen, dict) and gen.get("stage") != "done":
                msg += _cleanup_after_failure(control_client(ws.context), gen)
                gen["stage"] = "done"
            outcome["error"] = msg
            status = "failed"
        row = db.get(EvalPipeline, pipeline_id)
        if row is not None:
            row.status = status
            row.last_run = json.loads(json.dumps(outcome, default=str))
            db.commit()
    finally:
        db.close()


def _prepare_generation(
    db: Session,
    pipeline_id: str,
    config: PipelineBody,
    ws: WorkspaceScope,
    private: PrivateSessions,
    outcome: dict[str, Any],
    *,
    creator: Creator | None,
) -> bool:
    """Pick the sessions, make sure the target has a live AWS copy and persist the
    exact generation request. False when there is nothing to send (the run
    succeeds with 0 and makes no AWS write)."""
    src, proc, out = config.source, config.processing, config.output
    agent = _agentcore_agent(db, ws, src)
    run = _generation_run(db, ws, src) if src.batch_run_id else None
    service_name, log_group = (
        ("", "") if run is not None else resolve_telemetry(agent, ws.context)
    )
    target = None
    if out.dataset_id:
        target = _dataset_in(db, ws, out.dataset_id, need="own")
        _require_generation_target(target, src.scenario_type)
    existing = list(target.items) if target is not None else []
    room = pipelines.MAX_DATASET_ITEMS - len(existing)
    if room <= 0:
        raise AppError("pipeline.dataset_full",
                       f"the output dataset already holds {len(existing)} items "
                       f"(the cap is {pipelines.MAX_DATASET_ITEMS})", status_code=409)
    now = _now()
    batch_arn = None
    retention = None
    if run is not None:
        picked, skipped, matched, batch_arn = _select_from_run(
            ws, run, src, private, present=generation.present_session_ids(existing),
            limit=min(src.max_sessions, room),
        )
        outcome["scanned"] = len(run.session_ids or [])
    else:
        listing = observability.list_sessions(src.range, db, ws.context)
        fence = ProjectFence(db, ws.id)
        rows = [r for r in listing.get("sessions", [])
                if private.visible(r.get("session_id"))
                and (not fence.closed or fence.allows_service(r.get("service")))]
        retention = _retention_days(ws, log_group)
        picked, skipped, matched = generation.select_sessions(
            rows, service=service_name, status=src.status,
            dynamic=src.scenario_type == "DYNAMIC",
            present=generation.present_session_ids(existing),
            limit=min(src.max_sessions, room), now=now,
            oldest=generation.retention_floor(now, retention),
        )
        outcome["scanned"] = sum(1 for r in rows if r.get("service") == service_name)
    outcome["matched"] = matched
    outcome["skipped"] = skipped
    if not picked:
        outcome["dataset_id"] = target.id if target is not None else None
        return False

    control = control_client(ws.context)
    if target is not None:
        cloud = target.cloud or {}
        if cloud.get("dataset_id") and cloud.get("status") != "deleted":
            _refuse_running_generation(ws, str(cloud["dataset_id"]))
        # the Draft must equal the local items before AWS adds to (and dedupes
        # against) it; this creates the cloud copy when there is none yet
        eval_routers.sync_dataset_to_aws(dataset_id=target.id, db=db, ws=ws, viewer=Viewer.ALL)
        db.refresh(target)
        cloud_id = str((target.cloud or {})["dataset_id"])
    else:
        name = ac.sanitize_dataset_name(out.dataset_name or "generated")
        try:
            created = ac.create_dataset(control, name=name, examples=[],
                                        description=config.description or "")
        except ClientError as exc:
            raise AppError("dataset.sync_failed", f"CreateDataset rejected: {exc}",
                           status_code=502) from exc
        cloud_id = created["datasetId"]
    source = generation.build_source(
        src, service_name=service_name, log_group=log_group, sessions=picked,
        keep_replies=proc.keep_replies, now=now, retention_days=retention,
        batch_arn=batch_arn,
    )
    outcome["generation"] = {
        "batch_run_id": run.id if run is not None else None,
        "id": None, "cloud_dataset_id": cloud_id, "created_dataset": target is None,
        "local_dataset_id": target.id if target is not None else None,
        "scenario_type": src.scenario_type, "stage": "starting", "status": None,
        "requested": len(picked), "session_ids": [str(r["session_id"]) for r in picked],
        "results": None, "error_details": [], "redaction": None, "failure_reason": None,
        # the exact request + token: a restart re-issues it and gets the same generation
        "request": {"source": source,
                    "client_token": generation.client_token(pipeline_id, outcome["at"])},
        "creator": ({"user_id": creator.user_id, "name": creator.name}
                    if creator is not None else None),
    }
    _save_progress(db, pipeline_id, outcome)
    if target is None:
        ac.poll_dataset_active(control, dataset_id=cloud_id, interval=2.0, max_polls=60)
    return True


def _select_from_run(
    ws: WorkspaceScope, run: Any, src: PipelineSource, private: PrivateSessions,
    *, present: set[str], limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int, str]:
    """The run's sessions to send (its bad cases below `max_score`, or all scored
    ones) and the batch's ARN for the `batchEvaluation` source."""
    results = service.run_results(run, workspace=ws.context)
    if not results.get("available"):
        raise AppError("pipeline.run_not_generatable",
                       "the evaluation run's results cannot be read",
                       {"reason": results.get("reason") or "unreadable"}, status_code=400)
    by_session = {s["session_id"]: s["results"] for s in results.get("sessions") or []}
    picked, skipped, matched = generation.select_run_sessions(
        list(run.session_ids or []) or list(by_session), by_session,
        max_score=src.max_score, visible=private.visible, present=present, limit=limit,
    )
    detail = ac.get_batch_evaluation(data_client(ws.context), batch_id=run.batch_eval_id)
    return picked, skipped, matched, str(detail["batchEvaluationArn"])


def _refuse_running_generation(ws: WorkspaceScope, cloud_id: str) -> None:
    """One generation per dataset: a target whose Draft another generation holds
    fails before its row is synced (the sync would hit ConflictException)."""
    try:
        gens = ac.list_dataset_generations(data_client(ws.context), dataset_id=cloud_id)
    except ClientError:
        return  # inconclusive: StartDatasetGeneration answers the conflict itself
    if any(g.get("status") == "IN_PROGRESS" for g in gens):
        raise AppError("dataset.generation_running",
                       "another dataset generation is running on the output dataset — "
                       "retry once it finishes", status_code=409)


def _issue_start(
    db: Session, pipeline_id: str, config: PipelineBody, ws: WorkspaceScope,
    outcome: dict[str, Any],
) -> None:
    state = outcome["generation"]
    request = state["request"]
    try:
        started = ac.start_dataset_generation(
            data_client(ws.context), dataset_id=state["cloud_dataset_id"],
            name=generation.generation_name(pipeline_id),
            scenario_type=state["scenario_type"], source=request["source"],
            client_token=request["client_token"], description=config.description or "",
        )
    except ClientError as exc:
        if aws_error_code(exc) == "ConflictException":
            raise AppError("dataset.generation_running",
                           "another dataset generation is running on this dataset — "
                           f"retry once it finishes ({exc})", status_code=409) from exc
        raise AppError("pipeline.generation_rejected",
                       f"StartDatasetGeneration rejected: {exc}", status_code=502) from exc
    state.update(id=started["generationId"], stage="generating",
                 status=started.get("status") or "IN_PROGRESS")
    _save_progress(db, pipeline_id, outcome)


def _poll_generation(data: Any, cloud_id: str, generation_id: str) -> dict[str, Any]:
    for _ in range(generation.POLL_MAX):
        try:
            resp = ac.get_dataset_generation(data, dataset_id=cloud_id,
                                             generation_id=generation_id)
        except ClientError as exc:
            if aws_error_code(exc) not in TRANSIENT_CODES:
                raise
            resp = {}
        if resp.get("status") in ac.GENERATION_TERMINAL:
            return resp
        _sleep(generation.POLL_SECONDS)
    raise TimeoutError(f"dataset generation {generation_id} still running after "
                       f"{int(generation.POLL_MAX * generation.POLL_SECONDS)} s")


def _generated_examples(control: Any, cloud_id: str, generation_id: str,
                        written: int) -> list[dict[str, Any]]:
    """The generation's examples; the listing can trail the terminal status, so it
    is re-read a few times while it shows fewer than `examplesWritten`."""
    examples: list[dict[str, Any]] = []
    for attempt in range(4):
        examples = ac.list_dataset_examples(control, dataset_id=cloud_id,
                                            generation_id=generation_id)
        if len(examples) >= written:
            break
        if attempt < 3:
            _sleep(generation.POLL_SECONDS)
    return examples


def _finish_generation(
    db: Session,
    pipeline_id: str,
    config: PipelineBody,
    ws: WorkspaceScope,
    outcome: dict[str, Any],
    *,
    creator: Creator | None = None,
) -> None:
    """Poll to terminal, then merge. Idempotent on resume: what is already merged
    stays, and only generated examples absent from the local items leave the Draft."""
    state = outcome["generation"]
    out, proc = config.output, config.processing
    control = control_client(ws.context)
    cloud_id = state["cloud_dataset_id"]
    resp = _poll_generation(data_client(ws.context), cloud_id, state["id"])
    state.update(generation.summary(resp))
    state["stage"] = "merging"
    failed = generation.failed_sessions(resp)
    known = {(s.get("session_id"), s.get("reason")) for s in outcome["skipped"]}
    outcome["skipped"] = outcome["skipped"] + [
        f for f in failed if (f["session_id"], f["reason"]) not in known
    ]
    _save_progress(db, pipeline_id, outcome)
    if resp.get("status") == "FAILED":
        raise AppError("pipeline.generation_failed",
                       resp.get("failureReason") or "the dataset generation failed",
                       status_code=502)

    written = int((state.get("results") or {}).get("examplesWritten") or 0)
    examples = _generated_examples(control, cloud_id, state["id"], written)
    kept, dropped = generation.keep_examples(
        examples, requested=set(state["session_ids"]), validate=_validate_items
    )
    local_id = out.dataset_id or state.get("local_dataset_id")
    dataset = _dataset_in(db, ws, local_id, need="own") if local_id else None
    existing = list(dataset.items) if dataset is not None else []
    merged, taken, merge_skips = generation.merge(existing, kept, dedupe=proc.dedupe)
    outcome["skipped"] = outcome["skipped"] + [d for _, d in dropped] + merge_skips
    held = {str(i.get("scenario_id")) for i in merged if i.get("scenario_id")}
    stray = [str(ex["exampleId"]) for ex in examples
             if ex.get("exampleId") and str(ex.get("scenario_id")) not in held]
    if dataset is None and not taken:
        raise AppError("dataset.nothing_extracted",
                       "the generation produced no usable scenario", status_code=422)
    if stray:
        # the Draft keeps only what the local row holds (else the next sync drops
        # it anyway, and a published version would carry it meanwhile)
        ac.delete_dataset_examples(control, dataset_id=cloud_id, example_ids=stray)
        ac.poll_dataset_active(control, dataset_id=cloud_id, interval=2.0, max_polls=60)

    new_output = None
    if dataset is None:
        _validate_items(merged)
        dataset = EvalDataset(
            workspace_id=ws.id, name=out.dataset_name or "generated", locale="en",
            description=config.description, items=merged,
            # the generation's kind, not the items' — a DYNAMIC run whose sessions all
            # produced input-only items must still accept the next DYNAMIC run
            kind=GENERATION_TARGET_KIND[state["scenario_type"]],
        )
        creators.stamp(dataset, creator)
        db.add(dataset)
        db.flush()
        state["local_dataset_id"] = dataset.id
        new_output = {"dataset_id": dataset.id}
    elif taken:
        _validate_items(merged)
        dataset.items = merged
    try:
        detail = ac.get_dataset(control, dataset_id=cloud_id)
        dataset.cloud = eval_routers._cloud_blob(
            detail, synced_at=_now().isoformat(),
            versions=eval_routers._cloud_versions(control, cloud_id),
        )
    except Exception:  # noqa: BLE001 — display cache only; never undo a finished merge
        dataset.cloud = {**(dataset.cloud or {}), "dataset_id": cloud_id,
                         "status": "ACTIVE", "synced_at": _now().isoformat()}
    outcome["added"] = len(taken)
    outcome["dataset_id"] = dataset.id
    state["stage"] = "done"
    # the dataset write, the progress that records it and the retargeted output
    # commit together: a restart never merges twice or creates the dataset twice
    _save_progress(db, pipeline_id, outcome, output=new_output)


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


def _refuse_foreign_groups(db: Session, ws: WorkspaceScope, src: PipelineSource) -> None:
    """Another project's agent runtime log group answers like a missing one."""
    fence = ProjectFence(db, ws.id)
    foreign = [g for g in src.log_groups if fence.foreign_log_group(g)]
    if foreign:
        raise NotFoundError("pipeline.log_group_not_found",
                            f"log group(s) not found: {', '.join(foreign)}",
                            {"missing": foreign})


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
    _refuse_foreign_groups(db, ws, src)
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
    _assert_log_source_in_project(db, ws, service_name, groups)
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
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Log groups whose name contains `q` (case-insensitive), to pick a task's
    input log groups — never another project's agent runtime group."""
    out = log_streams.list_log_groups(ws.context.client("logs"), q or None)
    fence = ProjectFence(db, ws.id)
    if fence.closed:
        out["log_groups"] = [
            g for g in out["log_groups"] if not fence.foreign_log_group(g["name"])
        ]
    return out


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
    fence = ProjectFence(db, ws.id)
    if fence.closed:  # another project's agent is not an unowned service here
        services = [row for row in services if not fence.foreign_service(row["service_name"])]
    needle = (q or "").strip().lower()
    if needle:
        services = [row for row in services if needle in row["service_name"].lower()]
    return {"services": services, "log_groups": groups, "hours": hours}
