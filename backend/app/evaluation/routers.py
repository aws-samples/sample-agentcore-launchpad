"""Evaluation API — datasets, evaluators, runs, insights, queue state.

Adapted from agentcore_eva_opt routers (datasets/evaluators/runs/insights).
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError, aws_error_code
from app.evaluation import agentcore_eval as ac
from app.evaluation import service
from app.evaluation.models import EvalDataset, EvalRun
from app.evaluation.queue import run_queue
from app.evaluation.scenarios import available_ground_truth, normalize_scenarios
from app.models.ledger import Agent
from app.routers.workspaces import WorkspaceScope, require_workspace
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


def _assert_judge_ground_truth(
    ws: WorkspaceScope, evaluators: list[str], items: list[dict[str, Any]]
) -> None:
    """Reject a custom judge whose prompt wants ground truth this scope lacks.

    A judge prompt referencing e.g. ``{expected_response}`` gets it from the
    ``sessionMetadata`` this run derives from the dataset's scenarios. With
    nothing to fill it, AgentCore throws ``ValueError: Evaluator prompt
    requires: 'expected_response'`` on EVERY (session x evaluator) pair and the
    whole batch ends FAILED ~10 minutes later — so the run is refused up front
    instead. Builtin and ThirdParty ids skip the lookup entirely — neither owns
    an authored judge prompt (``Builtin.Trajectory*`` has its own gate below). A
    control-plane error fails OPEN: the service enforces the same constraint,
    and a listing blip must not block submitting a run.
    """
    custom = [e for e in evaluators if not e.startswith(("Builtin.", "ThirdParty."))]
    if not custom:
        return
    available = available_ground_truth(items)
    control = control_client(ws.context)
    missing: dict[str, list[str]] = {}
    for evaluator in custom:
        try:
            detail = ac.get_evaluator(control, evaluator_id=evaluator)
        except Exception:
            continue
        wanted = ac.ground_truth_placeholders(ac.judge_instructions(detail))
        gap = [p for p in wanted if p not in available]
        if gap:
            missing[evaluator] = gap
    if not missing:
        return
    named = "; ".join(
        f"{e} needs " + ", ".join(f"{{{p}}}" for p in gap) for e, gap in missing.items()
    )
    raise AppError(
        "run.judge_needs_ground_truth",
        f"{named} — this run's scope carries no such ground truth. Add it to the "
        "dataset scenarios (turns[].expected_response / expected_trajectory / "
        "assertions), or edit the judge prompt to drop the placeholder.",
        {"evaluators": missing},
        status_code=422,
    )


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


def _dataset_in(db: Session, ws: WorkspaceScope, dataset_id: str) -> EvalDataset:
    """The dataset, or 404 — another workspace's dataset is not visible here."""
    dataset = db.get(EvalDataset, dataset_id)
    if dataset is None or dataset.workspace_id != ws.id:
        raise NotFoundError("dataset.not_found", "dataset not found")
    return dataset


@router.get("/datasets")
def list_datasets(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    rows = (
        db.query(EvalDataset)
        .filter(EvalDataset.workspace_id == ws.id)
        .order_by(EvalDataset.created_at.desc())
        .all()
    )
    return {"datasets": [_dataset_out(d) for d in rows]}


@router.post("/datasets", status_code=201)
def create_dataset(
    req: DatasetCreate,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _validate_items(req.items)
    dataset = EvalDataset(
        workspace_id=ws.id,
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
def upload_dataset(
    req: DatasetUpload,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
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
        workspace_id=ws.id,
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
    dataset_id: str,
    req: DatasetUpdate,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    dataset = _dataset_in(db, ws, dataset_id)
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
def delete_dataset(
    dataset_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    dataset = _dataset_in(db, ws, dataset_id)
    db.delete(dataset)
    db.commit()
    return {"deleted": True}


# ─── AWS cloud datasets (draft in place + published versions) ───────────────
# One local row ↔ one AWS Dataset. The first sync creates the dataset; every
# later sync replaces the examples of its DRAFT in place; PUBLISH VERSION
# snapshots the draft as an immutable numbered version. AWS is the source of
# truth — the `cloud` blob on the row only caches display state.
def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _cloud_versions(client: Any, cloud_id: str) -> list[dict[str, Any]]:
    """Published versions of a cloud dataset, newest first (ListDatasetVersions)."""
    versions = [
        {
            "version": v.get("datasetVersion"),
            "example_count": v.get("exampleCount"),
            "created_at": _iso(v.get("createdAt")),
        }
        for v in ac.list_dataset_versions(client, dataset_id=cloud_id)
    ]
    versions.sort(key=lambda v: (v["created_at"] or "", str(v["version"])), reverse=True)
    return versions


def _cloud_blob(
    detail: dict[str, Any],
    *,
    synced_at: str,
    versions: list[dict[str, Any]] | None,
    failure_reason: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "dataset_id": detail.get("datasetId"),
        "arn": detail.get("datasetArn"),
        "status": status or detail.get("status"),
        "synced_at": synced_at,
        "failure_reason": failure_reason,
        "draft_status": detail.get("draftStatus"),
        "example_count": detail.get("exampleCount"),
        "versions": versions if versions is not None else [],
    }


def _existing_cloud_draft(client: Any, dataset: EvalDataset) -> dict[str, Any] | None:
    """The live GetDataset detail of the row's recorded cloud copy, or None when
    the row has none, the copy was deleted through the console, or AWS no longer
    knows it (ResourceNotFoundException) — those all re-create."""
    blob = dataset.cloud or {}
    cloud_id = blob.get("dataset_id")
    if not cloud_id or blob.get("status") == "deleted":
        return None
    try:
        return ac.get_dataset(client, dataset_id=cloud_id)
    except ClientError as exc:
        if aws_error_code(exc) == "ResourceNotFoundException":
            return None
        raise


@router.post("/datasets/{dataset_id}/sync-to-aws")
def sync_dataset_to_aws(
    dataset_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Push the local dataset to AWS. Without a live cloud copy this creates an
    AWS Dataset (inline examples, devguide predefined schema) and waits for
    ACTIVE. With one, the cloud dataset's DRAFT is edited in place — its current
    examples are deleted (DeleteDatasetExamples) and the local scenarios added
    (AddDatasetExamples), each step polled to ACTIVE — so the dataset id, its
    published versions and any run pinned to them survive; the draft then reads
    MODIFIED until PUBLISH VERSION snapshots it. The recorded `cloud` blob caches
    id/arn/status plus draft_status, example_count and the version list."""
    dataset = _dataset_in(db, ws, dataset_id)
    examples = normalize_scenarios(dataset.items)
    client = control_client(ws.context)
    synced_at = datetime.now(UTC).isoformat()
    existing = _existing_cloud_draft(client, dataset)
    if existing is not None:
        return _resync_draft(db, dataset, client, existing, examples, synced_at)

    name = ac.sanitize_dataset_name(dataset.name)
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
        dataset.cloud = _cloud_blob(
            {"datasetId": cloud_id, "datasetArn": created.get("datasetArn")},
            synced_at=synced_at,
            versions=[],
            status="CREATE_FAILED",
            failure_reason=str(exc),
        )
        db.commit()
        raise AppError("dataset.sync_failed", str(exc), status_code=502) from exc
    dataset.cloud = _cloud_blob(final, synced_at=synced_at, versions=[])
    db.commit()
    return _dataset_out(dataset)


def _resync_draft(
    db: Session,
    dataset: EvalDataset,
    client: Any,
    detail: dict[str, Any],
    examples: list[dict[str, Any]],
    synced_at: str,
) -> dict[str, Any]:
    """Replace the DRAFT examples of the row's cloud dataset with ``examples``."""
    cloud_id = detail["datasetId"]
    previous = dataset.cloud or {}

    def fail(exc: BaseException) -> AppError:
        reason = str(exc)
        dataset.cloud = _cloud_blob(
            detail,
            synced_at=synced_at,
            versions=previous.get("versions") or [],
            status="UPDATE_FAILED",
            failure_reason=reason,
        )
        db.commit()
        return AppError("dataset.sync_failed", reason, status_code=502)

    try:
        # a draft still settling from an earlier edit must reach ACTIVE first;
        # AWS accepts example edits only on ACTIVE / *_FAILED datasets
        if detail.get("status") not in ac.DATASET_TERMINAL:
            ac.poll_dataset_active(client, dataset_id=cloud_id, interval=2.0, max_polls=60)
        current_ids = [
            ex["exampleId"]
            for ex in ac.list_dataset_examples(client, dataset_id=cloud_id)
            if ex.get("exampleId")
        ]
        if current_ids:
            ac.delete_dataset_examples(client, dataset_id=cloud_id, example_ids=current_ids)
            ac.poll_dataset_active(client, dataset_id=cloud_id, interval=2.0, max_polls=60)
        ac.add_dataset_examples(client, dataset_id=cloud_id, examples=examples)
        final = ac.poll_dataset_active(client, dataset_id=cloud_id, interval=2.0, max_polls=60)
        versions = _cloud_versions(client, cloud_id)
    except (RuntimeError, TimeoutError) as exc:
        raise fail(exc) from exc
    except ClientError as exc:
        raise fail(exc) from exc
    dataset.cloud = _cloud_blob(final, synced_at=synced_at, versions=versions)
    db.commit()
    return _dataset_out(dataset)


def _publish_version(client: Any, cloud_id: str) -> dict[str, Any]:
    """CreateDatasetVersion then poll the draft through UPDATING to ACTIVE.
    Returns the final GetDataset detail; raises AppError dataset.publish_failed
    (502) on UPDATE_FAILED / timeout with the AWS failure reason."""
    try:
        ac.create_dataset_version(client, dataset_id=cloud_id)
        return ac.poll_dataset_active(client, dataset_id=cloud_id, interval=2.0, max_polls=60)
    except (RuntimeError, TimeoutError) as exc:
        raise AppError("dataset.publish_failed", str(exc), status_code=502) from exc


@router.post("/datasets/{dataset_id}/publish-version")
def publish_dataset_version(
    dataset_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Snapshot the cloud copy's DRAFT as the next immutable numbered version.
    The draft stays (draft_status flips to UNMODIFIED); the new version joins
    `cloud.versions`. 409 `dataset.not_synced` without a live cloud copy."""
    dataset = _dataset_in(db, ws, dataset_id)
    client = control_client(ws.context)
    detail = _existing_cloud_draft(client, dataset)
    if detail is None:
        raise AppError(
            "dataset.not_synced",
            "this dataset has no live AWS copy — SYNC TO AWS first",
            status_code=409,
        )
    cloud_id = detail["datasetId"]
    previous = dataset.cloud or {}
    synced_at = previous.get("synced_at") or datetime.now(UTC).isoformat()
    try:
        final = _publish_version(client, cloud_id)
    except AppError as exc:
        dataset.cloud = _cloud_blob(
            detail,
            synced_at=synced_at,
            versions=previous.get("versions") or [],
            status="UPDATE_FAILED",
            failure_reason=exc.message,
        )
        db.commit()
        raise
    dataset.cloud = _cloud_blob(
        final, synced_at=synced_at, versions=_cloud_versions(client, cloud_id)
    )
    db.commit()
    return _dataset_out(dataset)


# Locally runnable cloud schemas: predefined scenarios replay their turns;
# simulated persona datasets run through the SDK's LLM-actor simulation
# (requires an actor_model_id on the run).
RUNNABLE_CLOUD_SCHEMAS = {
    ac.DATASET_SCHEMA_TYPES["predefined"],
    ac.DATASET_SCHEMA_TYPES["simulated"],
}


def _cloud_dataset_items(
    cloud_id: str, ws: WorkspaceScope, version: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """(display name, run items) for an ACTIVE AWS cloud dataset — the DRAFT,
    or the published ``version`` when pinned (both GetDataset and
    ListDatasetExamples read that version, so the scenarios replayed and the
    ground truth gated on are exactly the snapshot's)."""
    client = control_client(ws.context)
    if version:
        known = {str(v["version"]) for v in _cloud_versions(client, cloud_id)}
        if version not in known:
            raise AppError(
                "run.dataset_version_unknown",
                f"cloud dataset has no published version '{version}'",
                status_code=422,
            )
    detail = ac.get_dataset(client, dataset_id=cloud_id, version=version)
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
        for example in ac.list_dataset_examples(client, dataset_id=cloud_id, version=version)
    ]
    if not items:
        raise AppError("dataset.empty", "cloud dataset has no examples", status_code=422)
    _validate_items(items)
    return detail.get("datasetName") or cloud_id, items


@router.get("/datasets/cloud")
def list_cloud_datasets(
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    out = []
    for ds in ac.list_datasets(control_client(ws.context)):
        out.append(
            {
                "datasetId": ds.get("datasetId"),
                "name": ds.get("datasetName"),
                "status": ds.get("status"),
                "schemaType": ds.get("schemaType"),
                "exampleCount": ds.get("exampleCount"),
                "draftStatus": ds.get("draftStatus"),
                "updatedAt": str(ds["updatedAt"]) if ds.get("updatedAt") else None,
            }
        )
    return {"datasets": out}


@router.get("/datasets/cloud/{cloud_id}")
def get_cloud_dataset(
    cloud_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """Cloud dataset detail for the run form and the datasets page — whether it
    can drive a run, whether its scenarios carry ground truth (gates Trajectory*
    evaluators), the DRAFT status and the published versions."""
    client = control_client(ws.context)
    detail = ac.get_dataset(client, dataset_id=cloud_id)
    return _cloud_detail_out(client, detail)


def _cloud_detail_out(client: Any, detail: dict[str, Any]) -> dict[str, Any]:
    cloud_id = detail["datasetId"]
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
        "draft_status": detail.get("draftStatus"),
        "failure_reason": detail.get("failureReason"),
        "versions": _cloud_versions(client, cloud_id),
        "runnable": runnable,
        "has_ground_truth": has_ground_truth,
    }


@router.post("/datasets/cloud/{cloud_id}/publish-version")
def publish_cloud_dataset_version(
    cloud_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    """PUBLISH VERSION for a cloud-only dataset (no local row): CreateDatasetVersion,
    poll to ACTIVE, return the refreshed detail with the new version listed."""
    client = control_client(ws.context)
    final = _publish_version(client, cloud_id)
    return _cloud_detail_out(client, final)


@router.delete("/datasets/cloud/{cloud_id}/versions/{version}")
def delete_cloud_dataset_version(
    cloud_id: str,
    version: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Delete ONE published version (DeleteDataset with datasetVersion); the draft
    and the other versions stay. A local row's cached version list is refreshed."""
    client = control_client(ws.context)
    ac.delete_dataset(client, dataset_id=cloud_id, version=version)
    for row in _rows_with_cloud(db, ws, cloud_id):
        row.cloud = {
            **row.cloud,
            "versions": [
                v for v in (row.cloud.get("versions") or []) if str(v.get("version")) != version
            ],
        }
    db.commit()
    return {"datasetId": cloud_id, "version": version, "deleted": True}


def _rows_with_cloud(db: Session, ws: WorkspaceScope, cloud_id: str) -> list[EvalDataset]:
    return [
        row
        for row in db.query(EvalDataset)
        .filter(EvalDataset.workspace_id == ws.id, EvalDataset.cloud.isnot(None))
        .all()
        if (row.cloud or {}).get("dataset_id") == cloud_id
    ]


@router.delete("/datasets/cloud/{cloud_id}")
def delete_cloud_dataset(
    cloud_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    ac.delete_dataset(control_client(ws.context), dataset_id=cloud_id)
    # A local row pointing at this cloud dataset loses its live copy — the next
    # sync creates a fresh dataset.
    for row in _rows_with_cloud(db, ws, cloud_id):
        row.cloud = {**row.cloud, "status": "deleted"}
    db.commit()
    return {"datasetId": cloud_id, "deleted": True}


# ─── evaluators ──────────────────────────────────────────────────────────────
@router.get("/evaluators")
def list_evaluators(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    builtin = [
        {
            "id": name,
            "level": level,
            "source": "builtin",
            "evaluator_type": "Builtin",
            "provider": "AWS",
        }
        for name, level in ac.ALL_BUILTIN_EVALUATORS.items()
    ] + [
        {
            "id": name,
            "level": level,
            "source": "builtin",
            "requires_ground_truth": True,
            "evaluator_type": "Builtin",
            "provider": "AWS",
        }
        for name, level in ac.TRAJECTORY_EVALUATORS.items()
    ]
    live: list[dict[str, Any]] = []
    try:
        for ev in ac.list_evaluators(control_client(ws.context)):
            evaluator_id = ev.get("evaluatorId", "")
            if evaluator_id.startswith("Builtin."):
                continue  # rendered from the local dicts above
            evaluator_type = ev.get("evaluatorType")
            third_party = evaluator_type == "ThirdParty" or evaluator_id.startswith(
                "ThirdParty."
            )
            live.append(
                {
                    "id": evaluator_id,
                    "name": ev.get("evaluatorName"),
                    "level": ev.get("level"),
                    "status": ev.get("status"),
                    "source": "third_party" if third_party else "custom",
                    "evaluator_type": evaluator_type,
                    "provider": ev.get("provider"),
                }
            )
    except Exception:
        pass  # account listing unavailable — builtins still render
    return {"evaluators": builtin + live, "builtin_count": len(builtin)}


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
    instructions: str | None = Field(default=None, min_length=10, max_length=4000)
    # Derived (CustomDerived) definition: run this base evaluator's prompt and
    # scoring on the caller-supplied Bedrock model instead of authoring one.
    base_evaluator_id: str | None = Field(
        default=None, pattern=r"^(Builtin|ThirdParty)\.[A-Za-z0-9_.]+$"
    )
    model_id: str = "global.anthropic.claude-sonnet-5"
    level: str = Field(default="TRACE", pattern="^(TOOL_CALL|TRACE|SESSION)$")
    description: str = Field(default="", max_length=1000)
    rating_scale: list[RatingScaleItem] | None = Field(default=None, min_length=2)


def _require_one_definition(req: "JudgeCreate | JudgeUpdate") -> None:
    if (req.instructions is None) == (req.base_evaluator_id is None):
        raise AppError(
            "evaluator.definition_ambiguous",
            "provide exactly one of instructions (LLM-as-a-judge) or "
            "base_evaluator_id (derived evaluator)",
            status_code=400,
        )
    if req.base_evaluator_id and req.rating_scale:
        raise AppError(
            "evaluator.rating_scale_not_allowed",
            "derived evaluators inherit the base evaluator's rating scale",
            status_code=400,
        )


def _resolve_base_level(client: Any, base_evaluator_id: str) -> str:
    """Level for a derived evaluator — CreateEvaluator requires one even for
    derived configs, so it is taken from the base evaluator."""
    level = ac.ALL_BUILTIN_EVALUATORS.get(base_evaluator_id)
    if level:
        return level
    try:
        return ac.get_evaluator(client, evaluator_id=base_evaluator_id)["level"]
    except Exception as exc:
        raise AppError(
            "evaluator.base_not_found",
            f"base evaluator {base_evaluator_id} not found",
            status_code=400,
        ) from exc


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
    config = detail.get("evaluatorConfig") or {}
    judge = config.get("llmAsAJudge") or {}
    derived = config.get("derived") or {}
    model_config = ((derived or judge).get("modelConfig") or {}).get(
        "bedrockEvaluatorModelConfig"
    ) or {}
    return {
        "id": detail.get("evaluatorId"),
        "name": detail.get("evaluatorName"),
        "level": detail.get("level"),
        "description": detail.get("description"),
        "instructions": "" if derived else judge.get("instructions"),
        "rating_scale": (judge.get("ratingScale") or {}).get("numerical", []),
        "model_id": model_config.get("modelId"),
        "base_evaluator_id": derived.get("baseEvaluatorId"),
        "evaluator_type": detail.get("evaluatorType"),
        "provider": detail.get("provider"),
        "status": detail.get("status"),
    }


@router.post("/evaluators", status_code=201)
def create_judge(
    req: JudgeCreate, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    _require_one_definition(req)
    client = control_client(ws.context)
    if req.base_evaluator_id:
        created = ac.create_derived_evaluator(
            client,
            name=req.name,
            description=req.description,
            base_evaluator_id=req.base_evaluator_id,
            model_id=req.model_id,
            level=_resolve_base_level(client, req.base_evaluator_id),
        )
    else:
        _require_placeholder(req.instructions)
        created = ac.create_llm_judge_evaluator(
            client,
            name=req.name,
            instructions=req.instructions,
            rating_scale=_rating_scale_payload(req.rating_scale),
            model_id=req.model_id,
            level=req.level,
            description=req.description,
        )
    return {"evaluator_id": created.get("evaluatorId"), "arn": created.get("evaluatorArn")}


@router.get("/evaluators/{evaluator_id}")
def get_evaluator(
    evaluator_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    return _evaluator_out(
        ac.get_evaluator(control_client(ws.context), evaluator_id=evaluator_id)
    )


class JudgeUpdate(BaseModel):
    instructions: str | None = Field(default=None, min_length=10, max_length=4000)
    base_evaluator_id: str | None = Field(
        default=None, pattern=r"^(Builtin|ThirdParty)\.[A-Za-z0-9_.]+$"
    )
    model_id: str = "global.anthropic.claude-sonnet-5"
    level: str = Field(default="TRACE", pattern="^(TOOL_CALL|TRACE|SESSION)$")
    description: str = Field(default="", max_length=1000)
    rating_scale: list[RatingScaleItem] | None = Field(default=None, min_length=2)


def _reject_managed(evaluator_id: str) -> None:
    if evaluator_id.startswith(("Builtin.", "ThirdParty.")):
        raise AppError(
            "evaluator.builtin_immutable",
            "built-in and third-party managed evaluators cannot be modified",
            status_code=400,
        )


@router.put("/evaluators/{evaluator_id}")
def update_evaluator(
    evaluator_id: str,
    req: JudgeUpdate,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _reject_managed(evaluator_id)
    _require_one_definition(req)
    if req.instructions is not None:
        _require_placeholder(req.instructions)
    client = control_client(ws.context)
    # UpdateEvaluator full-replaces the config, so a payload of the wrong
    # definition type would silently convert the evaluator — reject instead.
    current = ac.get_evaluator(client, evaluator_id=evaluator_id)
    is_derived = "derived" in (current.get("evaluatorConfig") or {})
    if is_derived != bool(req.base_evaluator_id):
        raise AppError(
            "evaluator.definition_mismatch",
            "update payload must match the evaluator's definition type "
            "(instructions for LLM-as-a-judge, base_evaluator_id for derived)",
            status_code=400,
        )
    if req.base_evaluator_id:
        ac.update_derived_evaluator(
            client,
            evaluator_id=evaluator_id,
            description=req.description,
            base_evaluator_id=req.base_evaluator_id,
            model_id=req.model_id,
            level=_resolve_base_level(client, req.base_evaluator_id),
        )
    else:
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
def delete_evaluator(
    evaluator_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    _reject_managed(evaluator_id)
    ac.delete_evaluator(control_client(ws.context), evaluator_id=evaluator_id)
    return {"deleted": True}


# ─── runs ────────────────────────────────────────────────────────────────────
class RunCreate(BaseModel):
    agent_id: str
    dataset_id: str | None = None
    cloud_dataset_id: str | None = None  # AWS cloud dataset
    # Published version of the cloud dataset to replay ("2"); omitted = DRAFT.
    # Only meaningful with cloud_dataset_id.
    dataset_version: str | None = Field(default=None, min_length=1, max_length=16)
    # Bedrock model that plays the user for simulated persona scenarios —
    # required whenever the selected dataset carries actor_profile items.
    actor_model_id: str | None = Field(default=None, min_length=1, max_length=120)
    evaluators: list[str] = Field(default_factory=lambda: list(ac.BUILTIN_EVALUATORS))
    mode: str = Field(default="evaluators", pattern="^(evaluators|insights)$")
    # Minimum ingestion age for the newest paired span/content log. This is an
    # active readiness threshold, not an unconditional sleep.
    wait_seconds: int = Field(default=180, ge=0, le=600)
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
        # pinned published cloud-dataset version; null = draft / not a cloud run
        "dataset_version": run.dataset_version,
        "mode": run.mode,
        "evaluators": run.evaluators,
        "status": run.status,
        "queue_position": run_queue.position(run.id),
        "session_ids": run.session_ids,
        "batch_eval_id": run.batch_eval_id,
        "scores": run.scores,
        "insights": run.insights,
        "error": run.error,
        # additive: an operator stop is pending on this run (in-memory flag;
        # the row turns `stopped` once the poller/worker observes it)
        "stop_requested": service.stop_requested(run.id),
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/runs")
def list_runs(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    mode: str | None = Query(None, pattern="^(evaluators|insights)$"),
    agent_id: str | None = Query(None, min_length=1, max_length=32),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Newest-first page of runs plus the unpaginated `total`.

    Defaults reproduce the pre-pagination response (latest 50, no filter) so
    older callers are unaffected. `mode` exists for the console's insights
    duplicate guard, which must see insights runs beyond the displayed page;
    `agent_id` for the experiment RECOMMEND card, which offers one agent's own
    completed runs as a trace source and must not miss them behind other agents'
    newer runs.
    """
    query = db.query(EvalRun).filter(EvalRun.workspace_id == ws.id)
    if mode:
        query = query.filter(EvalRun.mode == mode)
    if agent_id:
        query = query.filter(EvalRun.agent_id == agent_id)
    total = query.count()
    rows = (
        query.order_by(EvalRun.created_at.desc()).offset(offset).limit(limit).all()
    )
    return {
        "runs": [_run_out(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    run = db.get(EvalRun, run_id)
    # A run of another workspace reads as absent, like every other foreign id.
    if run is None or run.workspace_id != ws.id:
        raise NotFoundError("run.not_found", "run not found")
    return _run_out(run)


@router.post("/runs", status_code=201)
def create_run(
    req: RunCreate,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = db.get(Agent, req.agent_id)
    if agent is not None and agent.workspace_id != ws.id:
        agent = None
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
    if req.dataset_version and not req.cloud_dataset_id:
        raise AppError(
            "run.dataset_version_scope",
            "dataset_version only applies to a cloud_dataset_id scope",
            status_code=422,
        )
    if req.dataset_version and req.dataset_version.upper() == "DRAFT":
        raise AppError(
            "run.dataset_version_scope",
            "dataset_version is a published version number — omit it for the draft",
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
        dataset = _dataset_in(db, ws, req.dataset_id)
        items = dataset.items
        dataset_name = dataset.name
    elif req.cloud_dataset_id:
        cloud_name, items = _cloud_dataset_items(
            req.cloud_dataset_id, ws, version=req.dataset_version
        )
        # "cloud:" prefix marks the scope in the runs list (like "window:Nh");
        # the pinned version travels on its own column, never in this string.
        dataset_name = f"cloud:{cloud_name}"

    if any("actor_profile" in item for item in items) and not req.actor_model_id:
        raise AppError(
            "run.actor_model_required",
            "this dataset contains simulated persona scenarios — pick an "
            "actor_model_id (the Bedrock model that plays the user)",
            status_code=422,
        )

    if req.mode == "evaluators":
        _assert_judge_ground_truth(ws, req.evaluators, items)

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
        workspace=ws.context,
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
        dataset_version=req.dataset_version if req.cloud_dataset_id else None,
    )
    return _run_out(run)


@router.post("/runs/{run_id}/stop", status_code=202)
def stop_run(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Stop an active run. With a batch on AWS this is StopBatchEvaluation
    (202: the batch goes STOPPING → STOPPED and the row follows); a queued run
    is cancelled locally and comes back `stopped` at once; a run replaying its
    dataset stops before StartBatchEvaluation. Terminal runs → 409
    `run.not_active`."""
    run = db.get(EvalRun, run_id)
    if run is None or run.workspace_id != ws.id:
        raise NotFoundError("run.not_found", "run not found")
    return _run_out(service.request_stop(run.id, workspace=ws.context))


@router.get("/queue")
def queue_state(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Queue depth and the concurrency cap stay global numbers — the AgentCore
    batch quota they exist for is per account, and one process serves every
    workspace. The run ids are not global: naming another environment's runs
    would disclose that they exist.
    """
    state = run_queue.state()
    named = set(state["running"]) | set(state["queued"])
    mine: set[str] = set()
    if named:
        mine = set(
            db.scalars(
                select(EvalRun.id).where(
                    EvalRun.id.in_(named), EvalRun.workspace_id == ws.id
                )
            )
        )
    return {
        **state,
        "running": [run_id for run_id in state["running"] if run_id in mine],
        "queued": [run_id for run_id in state["queued"] if run_id in mine],
    }
