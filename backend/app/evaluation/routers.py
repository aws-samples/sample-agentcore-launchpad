"""Evaluation API — datasets, evaluators, runs, insights, queue state.

Adapted from agentcore_eva_opt routers (datasets/evaluators/runs/insights).
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.evaluation import agentcore_eval as ac
from app.evaluation import service
from app.evaluation.models import EvalDataset, EvalRun
from app.evaluation.queue import account_lock
from app.evaluation.scenarios import normalize_scenarios
from app.models.ledger import Agent
from app.services.agentcore.client import control_client

router = APIRouter(prefix="/api/eval", tags=["evaluation"])


# ─── datasets ────────────────────────────────────────────────────────────────
class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    locale: str = "en"
    description: str = Field(default="", max_length=1000)
    items: list[dict[str, Any]] = Field(min_length=1, max_length=200)

    @field_validator("items")
    @classmethod
    def _cap_item_size(cls, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in items:
            if len(str(item.get("prompt", ""))) > 8000:
                raise ValueError("dataset item prompt exceeds 8000 characters")
            if len(json.dumps(item, ensure_ascii=False)) > 16000:
                raise ValueError("dataset item exceeds 16000 characters serialized")
        return items


def _turn_input_text(turn: Any) -> str:
    raw = turn.get("input") if isinstance(turn, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("content") or raw.get("prompt")
    return str(raw or "").strip()


def _validate_items(items: list[dict[str, Any]]) -> None:
    """Per-item shape checks: predefined scenario items (they carry ``turns``)
    need a unique scenario_id and non-empty turns; simulated persona items
    (they carry ``actor_profile``) need a unique scenario_id, an initial input
    and the actor's context/goal; prompt items need a prompt."""
    seen_ids: set[str] = set()

    def check_scenario_id(idx: int, item: dict[str, Any]) -> None:
        scenario_id = str(item.get("scenario_id") or "").strip()
        if not scenario_id:
            raise AppError(
                "dataset.invalid_item", f"item {idx}: scenario_id required",
                status_code=422,
            )
        if scenario_id in seen_ids:
            raise AppError(
                "dataset.invalid_item",
                f"item {idx}: duplicate scenario_id '{scenario_id}'",
                status_code=422,
            )
        seen_ids.add(scenario_id)

    for idx, item in enumerate(items, 1):
        if "turns" in item:
            check_scenario_id(idx, item)
            turns = item.get("turns")
            if not isinstance(turns, list) or not turns:
                raise AppError(
                    "dataset.invalid_item", f"item {idx}: turns must be a non-empty list",
                    status_code=422,
                )
            for turn_no, turn in enumerate(turns, 1):
                if not _turn_input_text(turn):
                    raise AppError(
                        "dataset.invalid_item",
                        f"item {idx} turn {turn_no}: input required",
                        status_code=422,
                    )
        elif "actor_profile" in item:
            check_scenario_id(idx, item)
            profile = item.get("actor_profile")
            if not str(item.get("input", "")).strip():
                raise AppError(
                    "dataset.invalid_item", f"item {idx}: input required",
                    status_code=422,
                )
            if (
                not isinstance(profile, dict)
                or not str(profile.get("context", "")).strip()
                or not str(profile.get("goal", "")).strip()
            ):
                raise AppError(
                    "dataset.invalid_item",
                    f"item {idx}: actor_profile needs context and goal",
                    status_code=422,
                )
        elif not str(item.get("prompt", "")).strip():
            raise AppError(
                "dataset.invalid_item", f"item {idx}: prompt required", status_code=422
            )


def _infer_kind(items: list[dict[str, Any]]) -> str:
    if any("actor_profile" in item for item in items):
        return "simulated"
    return "predefined" if any("turns" in item for item in items) else "legacy"


def _has_ground_truth(items: list[dict[str, Any]]) -> bool:
    for scenario in normalize_scenarios(items):
        if scenario.get("assertions") or scenario.get("expected_trajectory"):
            return True
        if any(t.get("expected_response") for t in scenario.get("turns", [])):
            return True
    return False


def _dataset_out(dataset: EvalDataset) -> dict[str, Any]:
    return {
        "id": dataset.id,
        "name": dataset.name,
        "kind": dataset.kind,
        "locale": dataset.locale,
        "description": dataset.description or "",
        "item_count": len(dataset.items),
        "items": dataset.items,
        "cloud": dataset.cloud,
        "has_ground_truth": _has_ground_truth(dataset.items),
        "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
    }


@router.get("/datasets")
def list_datasets(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(EvalDataset).order_by(EvalDataset.created_at.desc()).all()
    return {"datasets": [_dataset_out(d) for d in rows]}


@router.post("/datasets", status_code=201)
def create_dataset(req: DatasetCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    _validate_items(req.items)
    dataset = EvalDataset(
        name=req.name,
        locale=req.locale,
        description=req.description,
        items=req.items,
        kind=_infer_kind(req.items),
    )
    db.add(dataset)
    db.commit()
    return _dataset_out(dataset)


class DatasetUpload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    locale: str = "en"
    description: str = Field(default="", max_length=1000)
    jsonl: str = Field(min_length=1)


@router.post("/datasets/upload", status_code=201)
def upload_dataset(req: DatasetUpload, db: Session = Depends(get_db)) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for line_no, line in enumerate(req.jsonl.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise AppError(
                "dataset.invalid_jsonl", f"line {line_no}: {exc}", status_code=422
            ) from exc
        if not isinstance(item, dict):
            raise AppError(
                "dataset.invalid_item", f"line {line_no}: expected an object",
                status_code=422,
            )
        items.append(item)
    if not items:
        raise AppError("dataset.empty", "no items in upload", status_code=422)
    _validate_items(items)
    dataset = EvalDataset(
        name=req.name,
        locale=req.locale,
        description=req.description,
        items=items,
        kind=_infer_kind(items),
    )
    db.add(dataset)
    db.commit()
    return _dataset_out(dataset)


class DatasetUpdate(BaseModel):
    """Partial update — only provided fields change (kind is immutable)."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    items: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=200)


@router.put("/datasets/{dataset_id}")
def update_dataset(
    dataset_id: str, req: DatasetUpdate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    dataset = db.get(EvalDataset, dataset_id)
    if dataset is None:
        raise NotFoundError("dataset.not_found", "dataset not found")
    if req.items is not None:
        _validate_items(req.items)
        if _infer_kind(req.items) != dataset.kind:
            raise AppError(
                "dataset.kind_immutable",
                f"dataset kind '{dataset.kind}' cannot change — replacement items "
                "must keep the same shape",
                status_code=400,
            )
        dataset.items = req.items
    if req.name is not None:
        dataset.name = req.name
    if req.description is not None:
        dataset.description = req.description
    db.commit()
    return _dataset_out(dataset)


@router.delete("/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    dataset = db.get(EvalDataset, dataset_id)
    if dataset is None:
        raise NotFoundError("dataset.not_found", "dataset not found")
    db.delete(dataset)
    db.commit()
    return {"deleted": True}


# ─── AWS cloud datasets (one-way sync) ───────────────────────────────────────
@router.post("/datasets/{dataset_id}/sync-to-aws")
def sync_dataset_to_aws(dataset_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Create an AWS Dataset resource from the local dataset (inline examples,
    devguide predefined schema), wait for ACTIVE, and record the cloud copy on
    the row. Cloud datasets are immutable — re-syncing creates a NEW cloud
    dataset and overwrites the recorded copy (the old one stays listed)."""
    dataset = db.get(EvalDataset, dataset_id)
    if dataset is None:
        raise NotFoundError("dataset.not_found", "dataset not found")
    examples = normalize_scenarios(dataset.items)
    name = ac.sanitize_dataset_name(dataset.name)
    client = control_client()
    synced_at = datetime.now(UTC).isoformat()
    try:
        created = ac.create_dataset(
            client,
            name=name,
            # simulated persona datasets sync with their own schema; scenario
            # and legacy prompt datasets both normalize to predefined
            schema_type=ac.DATASET_SCHEMA_TYPES.get(
                dataset.kind, ac.DATASET_SCHEMA_TYPES["predefined"]
            ),
            examples=examples,
            description=dataset.description or "",
        )
    except Exception as exc:
        raise AppError(
            "dataset.sync_failed", f"CreateDataset rejected: {exc}", status_code=502
        ) from exc
    cloud_id = created["datasetId"]
    try:
        final = ac.poll_dataset_active(
            client, dataset_id=cloud_id, interval=2.0, max_polls=60
        )
    except (RuntimeError, TimeoutError) as exc:
        dataset.cloud = {
            "dataset_id": cloud_id,
            "arn": created.get("datasetArn"),
            "status": "CREATE_FAILED",
            "synced_at": synced_at,
            "failure_reason": str(exc),
        }
        db.commit()
        raise AppError("dataset.sync_failed", str(exc), status_code=502) from exc
    dataset.cloud = {
        "dataset_id": cloud_id,
        "arn": final.get("datasetArn"),
        "status": final.get("status"),
        "synced_at": synced_at,
        "failure_reason": None,
    }
    db.commit()
    return _dataset_out(dataset)


# Locally runnable cloud schemas: predefined scenarios replay their turns;
# simulated persona datasets run through the SDK's LLM-actor simulation
# (requires an actor_model_id on the run).
RUNNABLE_CLOUD_SCHEMAS = {
    ac.DATASET_SCHEMA_TYPES["predefined"],
    ac.DATASET_SCHEMA_TYPES["simulated"],
}


def _cloud_dataset_items(cloud_id: str) -> tuple[str, list[dict[str, Any]]]:
    """(display name, run items) for an ACTIVE AWS cloud dataset."""
    client = control_client()
    detail = ac.get_dataset(client, dataset_id=cloud_id)
    if detail.get("status") != "ACTIVE":
        raise AppError(
            "dataset.cloud_not_active",
            f"cloud dataset is {detail.get('status')} — only ACTIVE datasets can run",
            status_code=400,
        )
    if detail.get("schemaType") not in RUNNABLE_CLOUD_SCHEMAS:
        raise AppError(
            "run.cloud_dataset_unsupported",
            f"cloud dataset schema '{detail.get('schemaType')}' is not runnable here",
            status_code=422,
        )
    items = [
        {k: v for k, v in example.items() if k != "exampleId"}
        for example in ac.list_dataset_examples(client, dataset_id=cloud_id)
    ]
    if not items:
        raise AppError("dataset.empty", "cloud dataset has no examples", status_code=422)
    _validate_items(items)
    return detail.get("datasetName") or cloud_id, items


@router.get("/datasets/cloud")
def list_cloud_datasets() -> dict[str, Any]:
    out = []
    for ds in ac.list_datasets(control_client()):
        out.append(
            {
                "datasetId": ds.get("datasetId"),
                "name": ds.get("datasetName"),
                "status": ds.get("status"),
                "schemaType": ds.get("schemaType"),
                "exampleCount": ds.get("exampleCount"),
                "updatedAt": str(ds["updatedAt"]) if ds.get("updatedAt") else None,
            }
        )
    return {"datasets": out}


@router.get("/datasets/cloud/{cloud_id}")
def get_cloud_dataset(cloud_id: str) -> dict[str, Any]:
    """Cloud dataset detail for the run form — whether it can drive a run and
    whether its scenarios carry ground truth (gates Trajectory* evaluators)."""
    client = control_client()
    detail = ac.get_dataset(client, dataset_id=cloud_id)
    runnable = (
        detail.get("status") == "ACTIVE"
        and detail.get("schemaType") in RUNNABLE_CLOUD_SCHEMAS
    )
    has_ground_truth = False
    if runnable:
        items = [
            {k: v for k, v in example.items() if k != "exampleId"}
            for example in ac.list_dataset_examples(client, dataset_id=cloud_id)
        ]
        has_ground_truth = bool(items) and _has_ground_truth(items)
    return {
        "datasetId": detail.get("datasetId"),
        "name": detail.get("datasetName"),
        "status": detail.get("status"),
        "schemaType": detail.get("schemaType"),
        "exampleCount": detail.get("exampleCount"),
        "runnable": runnable,
        "has_ground_truth": has_ground_truth,
    }


@router.delete("/datasets/cloud/{cloud_id}")
def delete_cloud_dataset(cloud_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    ac.delete_dataset(control_client(), dataset_id=cloud_id)
    # A local row pointing at this cloud dataset loses its live copy.
    for row in db.query(EvalDataset).filter(EvalDataset.cloud.isnot(None)).all():
        if (row.cloud or {}).get("dataset_id") == cloud_id:
            row.cloud = {**row.cloud, "status": "deleted"}
    db.commit()
    return {"datasetId": cloud_id, "deleted": True}


# ─── evaluators ──────────────────────────────────────────────────────────────
@router.get("/evaluators")
def list_evaluators() -> dict[str, Any]:
    builtin = [
        {"id": name, "level": level, "source": "builtin"}
        for name, level in ac.ALL_BUILTIN_EVALUATORS.items()
    ] + [
        {"id": name, "level": level, "source": "builtin", "requires_ground_truth": True}
        for name, level in ac.TRAJECTORY_EVALUATORS.items()
    ]
    custom: list[dict[str, Any]] = []
    try:
        for ev in ac.list_evaluators(control_client()):
            evaluator_id = ev.get("evaluatorId", "")
            if not evaluator_id.startswith("Builtin."):
                custom.append(
                    {
                        "id": evaluator_id,
                        "name": ev.get("evaluatorName"),
                        "level": ev.get("level"),
                        "status": ev.get("status"),
                        "source": "custom",
                    }
                )
    except Exception:
        pass  # account listing unavailable — builtins still render
    return {"evaluators": builtin + custom, "builtin_count": len(builtin)}


_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")

DEFAULT_RATING_SCALE = [
    {"value": 1.0, "label": "pass", "definition": "meets the instruction"},
    {"value": 0.0, "label": "fail", "definition": "does not meet the instruction"},
]


class RatingScaleItem(BaseModel):
    value: float
    label: str = Field(min_length=1, max_length=64)
    definition: str = Field(min_length=1, max_length=1000)


class JudgeCreate(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
    instructions: str = Field(min_length=10, max_length=4000)
    model_id: str = "global.anthropic.claude-sonnet-5"
    level: str = Field(default="TRACE", pattern="^(TOOL_CALL|TRACE|SESSION)$")
    description: str = Field(default="", max_length=1000)
    rating_scale: list[RatingScaleItem] | None = Field(default=None, min_length=2)


def _require_placeholder(instructions: str) -> None:
    if not _PLACEHOLDER_RE.search(instructions):
        raise AppError(
            "evaluator.missing_placeholder",
            "instructions need at least one {placeholder} for the evaluated "
            "content (e.g. {context}, {assistant_turn})",
            status_code=422,
        )


def _rating_scale_payload(scale: list[RatingScaleItem] | None) -> list[dict[str, Any]]:
    if not scale:
        return DEFAULT_RATING_SCALE
    return [item.model_dump() for item in scale]


def _evaluator_out(detail: dict[str, Any]) -> dict[str, Any]:
    judge = (detail.get("evaluatorConfig") or {}).get("llmAsAJudge") or {}
    model_config = (judge.get("modelConfig") or {}).get(
        "bedrockEvaluatorModelConfig"
    ) or {}
    return {
        "id": detail.get("evaluatorId"),
        "name": detail.get("evaluatorName"),
        "level": detail.get("level"),
        "description": detail.get("description"),
        "instructions": judge.get("instructions"),
        "rating_scale": (judge.get("ratingScale") or {}).get("numerical", []),
        "model_id": model_config.get("modelId"),
        "status": detail.get("status"),
    }


@router.post("/evaluators", status_code=201)
def create_judge(req: JudgeCreate) -> dict[str, Any]:
    _require_placeholder(req.instructions)
    created = ac.create_llm_judge_evaluator(
        control_client(),
        name=req.name,
        instructions=req.instructions,
        rating_scale=_rating_scale_payload(req.rating_scale),
        model_id=req.model_id,
        level=req.level,
        description=req.description,
    )
    return {"evaluator_id": created.get("evaluatorId"), "arn": created.get("evaluatorArn")}


@router.get("/evaluators/{evaluator_id}")
def get_evaluator(evaluator_id: str) -> dict[str, Any]:
    return _evaluator_out(ac.get_evaluator(control_client(), evaluator_id=evaluator_id))


class JudgeUpdate(BaseModel):
    instructions: str = Field(min_length=10, max_length=4000)
    model_id: str = "global.anthropic.claude-sonnet-5"
    level: str = Field(default="TRACE", pattern="^(TOOL_CALL|TRACE|SESSION)$")
    description: str = Field(default="", max_length=1000)
    rating_scale: list[RatingScaleItem] | None = Field(default=None, min_length=2)


@router.put("/evaluators/{evaluator_id}")
def update_evaluator(evaluator_id: str, req: JudgeUpdate) -> dict[str, Any]:
    if evaluator_id.startswith("Builtin."):
        raise AppError(
            "evaluator.builtin_immutable",
            "built-in evaluators cannot be modified",
            status_code=400,
        )
    _require_placeholder(req.instructions)
    client = control_client()
    ac.update_evaluator(
        client,
        evaluator_id=evaluator_id,
        instructions=req.instructions,
        rating_scale=_rating_scale_payload(req.rating_scale),
        model_id=req.model_id,
        level=req.level,
        description=req.description,
    )
    return _evaluator_out(ac.get_evaluator(client, evaluator_id=evaluator_id))


@router.delete("/evaluators/{evaluator_id}")
def delete_evaluator(evaluator_id: str) -> dict[str, Any]:
    ac.delete_evaluator(control_client(), evaluator_id=evaluator_id)
    return {"deleted": True}


# ─── runs ────────────────────────────────────────────────────────────────────
class RunCreate(BaseModel):
    agent_id: str
    dataset_id: str | None = None
    cloud_dataset_id: str | None = None  # AWS cloud dataset
    # Bedrock model that plays the user for simulated persona scenarios —
    # required whenever the selected dataset carries actor_profile items.
    actor_model_id: str | None = Field(default=None, min_length=1, max_length=120)
    evaluators: list[str] = Field(default_factory=lambda: list(ac.BUILTIN_EVALUATORS))
    mode: str = Field(default="evaluators", pattern="^(evaluators|insights)$")
    wait_seconds: int = Field(default=90, ge=0, le=600)
    session_ids: list[str] | None = None  # insights/passive over past sessions
    lookback_hours: int | None = Field(default=None, ge=1, le=336)  # time window
    insights: list[str] | None = None  # insight-type subset (insights mode)


def _run_out(run: EvalRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "agent_name": run.agent_name,
        "dataset_id": run.dataset_id,
        "dataset_name": run.dataset_name,
        "mode": run.mode,
        "evaluators": run.evaluators,
        "status": run.status,
        "queue_position": account_lock.position(run.id),
        "session_ids": run.session_ids,
        "batch_eval_id": run.batch_eval_id,
        "scores": run.scores,
        "insights": run.insights,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/runs")
def list_runs(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(EvalRun).order_by(EvalRun.created_at.desc()).limit(50).all()
    return {"runs": [_run_out(r) for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(EvalRun, run_id)
    if run is None:
        raise NotFoundError("run.not_found", "run not found")
    return _run_out(run)


@router.post("/runs", status_code=201)
def create_run(req: RunCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    agent = db.get(Agent, req.agent_id)
    if agent is None or agent.status != "active":
        raise AppError("agent.not_active", "agent must be active", status_code=400)

    dataset_scope = bool(req.dataset_id) or bool(req.cloud_dataset_id)
    scopes = [dataset_scope, bool(req.session_ids), bool(req.lookback_hours)]
    if sum(scopes) != 1 or (req.dataset_id and req.cloud_dataset_id):
        raise AppError(
            "run.scope_required",
            "exactly one scope required: dataset_id, cloud_dataset_id, "
            "session_ids or lookback_hours",
            status_code=422,
        )
    if req.insights:
        invalid = [i for i in req.insights if i not in ac.INSIGHT_TYPES]
        if invalid:
            raise AppError(
                "run.invalid_insight",
                f"unknown insight type(s): {', '.join(invalid)}",
                status_code=422,
            )

    items: list[dict[str, Any]] = []
    dataset_name = None
    time_range = None
    if req.dataset_id:
        dataset = db.get(EvalDataset, req.dataset_id)
        if dataset is None:
            raise NotFoundError("dataset.not_found", "dataset not found")
        items = dataset.items
        dataset_name = dataset.name
    elif req.cloud_dataset_id:
        cloud_name, items = _cloud_dataset_items(req.cloud_dataset_id)
        # "cloud:" prefix marks the scope in the runs list (like "window:Nh")
        dataset_name = f"cloud:{cloud_name}"

    if any("actor_profile" in item for item in items) and not req.actor_model_id:
        raise AppError(
            "run.actor_model_required",
            "this dataset contains simulated persona scenarios — pick an "
            "actor_model_id (the Bedrock model that plays the user)",
            status_code=422,
        )

    # Trajectory*Match evaluators score against expectedTrajectory ground
    # truth — only a dataset run whose scenarios carry it can supply that.
    if any(e.startswith("Builtin.Trajectory") for e in req.evaluators):
        has_trajectory_gt = any(
            s.get("expected_trajectory") for s in normalize_scenarios(items)
        )
        if not (dataset_scope and has_trajectory_gt):
            raise AppError(
                "run.trajectory_needs_ground_truth",
                "trajectory evaluators need a dataset whose scenarios define "
                "expected_trajectory",
                status_code=422,
            )

    if req.lookback_hours:
        now = datetime.now(UTC)
        time_range = {
            "startTime": now - timedelta(hours=req.lookback_hours),
            "endTime": now,
        }

    # The run row's evaluators column records what was applied: evaluator ids
    # for scored runs, the selected insight types for insights runs.
    applied = req.evaluators
    if req.mode == "insights":
        applied = req.insights or list(ac.INSIGHT_TYPES)

    run = service.submit_run(
        agent=agent,
        dataset_items=items,
        dataset_id=req.dataset_id or req.cloud_dataset_id,
        dataset_name=dataset_name,
        evaluators=applied,
        mode=req.mode,
        wait_seconds=req.wait_seconds,
        session_ids=req.session_ids,
        time_range=time_range,
        insights=req.insights,
        lookback_hours=req.lookback_hours,
        actor_model_id=req.actor_model_id,
    )
    return _run_out(run)


@router.get("/queue")
def queue_state() -> dict[str, Any]:
    return account_lock.state()
