"""Evaluation API — datasets, evaluators, runs, insights, queue state.

Adapted from agentcore_eva_opt routers (datasets/evaluators/runs/insights).
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import (
    AppError,
    NotFoundError,
    aws_error_code,
    mapped_aws_error,
    public_aws_message,
)
from app.evaluation import agentcore_eval as ac
from app.evaluation import recommendations, scorecard, service
from app.evaluation.models import EvalDataset, EvalRecommendation, EvalRun
from app.evaluation.online_evaluators import require_visible_evaluators
from app.evaluation.queue import run_queue
from app.evaluation.scenarios import normalize_scenarios
from app.models.ledger import Agent
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import AgentSpec
from app.services import agent_access, budgets, creators, job_budget, visibility
from app.services.agentcore.client import control_client
from app.services.aws_clients import is_assume_role_failure
from app.services.creators import Creator
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/eval", tags=["evaluation"])
logger = logging.getLogger(__name__)


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


def _unverifiable(evaluator: str, exc: Exception) -> AppError:
    reason = f"{type(exc).__name__}: {exc}"[:300]
    return AppError(
        "run.evaluator_unverifiable",
        f"could not read evaluator {evaluator} ({reason}) — its reference requirements "
        "are unknown, so the run was not started. Retry when the control plane answers, "
        "or drop the evaluator.",
        {"evaluator": evaluator, "reason": reason}, status_code=422,
    )


def _assert_target_references(
    db: Session, ws: WorkspaceScope, evaluators: list[str], items: list[dict[str, Any]],
    dataset_scope: bool, *, agent_spec: dict[str, Any] | None = None,
    viewer: Viewer = Viewer.ALL,
) -> None:
    """Every chosen evaluator that reads ground truth must find it on EVERY target it
    will be applied to — each session or each TRACE turn of the CURRENT dataset
    snapshot — before any run row, invoke or
    StartBatchEvaluation. Builtins resolve from the canonical catalog, custom judges /
    derived from their real configuration (one GetEvaluator each — no retries beyond the
    client's own), managed code evaluators from their owning plan's rules. A custom
    evaluator that cannot be read has UNKNOWN requirements, not verified ones: the run is
    refused with an actionable 422 before anything downstream happens (the agent would
    otherwise already have been invoked by the time the service rejected the batch). A
    scope without a dataset carries no references at all."""
    from app.assistant.evaluation_assets import (
        managed_evaluator,
        managed_package_evaluator_count,
        managed_rules,
    )
    from app.assistant.evaluation_plan import CodeCheck, code_rule_capability_conflict_errors
    from app.evaluation import coverage

    problems: dict[str, list[str]] = {}
    control = None
    hidden = visibility.hidden_aws_ids(viewer, db, ws.id, creators.EVALUATOR, evaluators)
    for evaluator in dict.fromkeys(evaluators):
        if evaluator in hidden:
            # workspace isolation: a hidden custom evaluator is a missing one
            raise _evaluator_missing(evaluator)
        if evaluator in coverage.KNOWN_BUILTINS:
            needs, level = coverage.builtin_needs(evaluator), coverage.KNOWN_BUILTINS[evaluator]
        elif evaluator.startswith(("Builtin.", "ThirdParty.")):
            continue  # unknown managed ids are refused by the service itself
        else:
            owner = managed_evaluator(db, ws.id, evaluator)
            if owner and owner.get("evaluator_config"):
                # created by this platform: its exact request is on record — no control read
                detail = {"evaluatorConfig": owner["evaluator_config"], "level": owner["level"]}
            else:
                control = control or control_client(ws.context)
                try:
                    detail = ac.get_evaluator(control, evaluator_id=evaluator)
                except ClientError as exc:
                    if aws_error_code(exc) in ("ResourceNotFoundException", "NotFoundException"):
                        raise _evaluator_missing(evaluator) from exc
                    raise _unverifiable(evaluator, exc) from exc
                except Exception as exc:  # transport / timeout / SDK failures
                    raise _unverifiable(evaluator, exc) from exc
            # the run preflight decodes NEEDS; the complete-shape check belongs to binding an
            # existing reference at materialization (the service validates its own records)
            kinds = [k for k in (detail.get("evaluatorConfig") or {}) if k in coverage.CONFIG_KINDS]
            if len(kinds) != 1:
                problems[evaluator] = ["unusable configuration: exactly one of llmAsAJudge / "
                                       "derived / codeBased is required"]
                continue
            rules = managed_rules(db, owner) if owner else None
            if rules is not None:
                conflicts = code_rule_capability_conflict_errors(
                    [CodeCheck.model_validate(check) for check in rules],
                    agent_spec or {}, evaluator_key=evaluator,
                )
                if conflicts:
                    raise AppError(
                        "run.evaluator_capability_conflict", "; ".join(conflicts),
                        {"evaluators": {evaluator: conflicts}}, status_code=422,
                    )
                packaged = managed_package_evaluator_count(db, owner)
                if packaged is not None and packaged != 1:
                    raise AppError(
                        "run.evaluator_package_ambiguous",
                        f"evaluator {evaluator} uses a legacy Lambda package containing "
                        f"{packaged} rule sets; AgentCore callbacks can omit evaluator identity. "
                        "Create a replacement code evaluator with its own single-rule package.",
                        {"evaluator": evaluator, "packaged_evaluators": packaged,
                         "operation_id": owner["operation_id"]},
                        status_code=422,
                    )
            needs, _kind = coverage.needs_from_config(detail, rules)
            level = str(detail.get("level") or "TRACE")
            if needs is None:
                continue  # external code evaluator: needs unknown, nothing to assert
        if not needs:
            continue
        if not dataset_scope:
            problems[evaluator] = [f"needs {', '.join(sorted(needs))} — this scope carries no "
                                   "ground truth"]
            continue
        gaps = coverage.coverage_gaps(items, needs, level)
        if gaps:
            problems[evaluator] = gaps
    if problems:
        named = "; ".join(f"{e}: " + ", ".join(g[:6]) + (" …" if len(g) > 6 else "")
                          for e, g in problems.items())
        only_trajectory = all(e in coverage.TRAJECTORY_EVALUATORS for e in problems)
        raise AppError(
            "run.trajectory_needs_ground_truth" if only_trajectory
            else "run.judge_needs_ground_truth",
            f"{named} — every session / turn the evaluator is applied to must carry the "
            "reference it reads. Add the ground truth to those scenarios or drop the "
            "evaluator.",
            {"evaluators": problems}, status_code=422,
        )


def _evaluator_missing(evaluator: str) -> AppError:
    return AppError(
        "run.evaluator_not_found",
        f"evaluator {evaluator} does not exist in this workspace — "
        "drop it or pick one from the catalog",
        {"evaluator": evaluator}, status_code=422,
    )


def _require_visible_evaluator(
    db: Session, ws: WorkspaceScope, viewer: Viewer, evaluator_id: str
) -> None:
    """A custom evaluator hidden by workspace isolation answers like a missing one;
    built-in / third-party ids carry no creator and are always visible."""
    require_visible_evaluators(db, ws.id, viewer, [evaluator_id])


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
        **creators.fields(dataset),
    }


def _dataset_in(
    db: Session, ws: WorkspaceScope, dataset_id: str, *, viewer: Viewer = Viewer.ALL
) -> EvalDataset:
    """The dataset, or 404 — another workspace's dataset, or one hidden from the
    viewer by workspace isolation (`services/visibility.py`), is not visible here."""
    dataset = db.get(EvalDataset, dataset_id)
    if dataset is None or dataset.workspace_id != ws.id:
        raise NotFoundError("dataset.not_found", "dataset not found")
    visibility.require_seen(
        viewer, db, dataset.created_by, NotFoundError("dataset.not_found", "dataset not found")
    )
    return dataset


@router.get("/datasets")
def list_datasets(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    rows = (
        db.query(EvalDataset)
        .filter(EvalDataset.workspace_id == ws.id)
        .filter(visibility.clause(viewer, EvalDataset.created_by))
        .order_by(EvalDataset.created_at.desc())
        .all()
    )
    return {"datasets": [_dataset_out(d) for d in rows]}


@router.post("/datasets", status_code=201)
def create_dataset(
    req: DatasetCreate,
    request: Request,
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
    creators.stamp(dataset, Creator.of_request(request))
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
    request: Request,
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
    creators.stamp(dataset, Creator.of_request(request))
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    dataset = _dataset_in(db, ws, dataset_id, viewer=viewer)
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    dataset = _dataset_in(db, ws, dataset_id, viewer=viewer)
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Push the local dataset to AWS. Without a live cloud copy this creates an
    AWS Dataset (inline examples, devguide predefined schema) and waits for
    ACTIVE. With one, the cloud dataset's DRAFT is edited in place — its current
    examples are deleted (DeleteDatasetExamples) and the local scenarios added
    (AddDatasetExamples), each step polled to ACTIVE — so the dataset id, its
    published versions and any run pinned to them survive; the draft then reads
    MODIFIED until PUBLISH VERSION snapshots it. The recorded `cloud` blob caches
    id/arn/status plus draft_status, example_count and the version list."""
    dataset = _dataset_in(db, ws, dataset_id, viewer=viewer)
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Snapshot the cloud copy's DRAFT as the next immutable numbered version.
    The draft stays (draft_status flips to UNMODIFIED); the new version joins
    `cloud.versions`. 409 `dataset.not_synced` without a live cloud copy."""
    dataset = _dataset_in(db, ws, dataset_id, viewer=viewer)
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
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    out = []
    hidden = _hidden_cloud_ids(db, ws, viewer)
    for ds in ac.list_datasets(control_client(ws.context)):
        if ds.get("datasetId") in hidden:
            continue
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
    cloud_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Cloud dataset detail for the run form and the datasets page — whether it
    can drive a run, whether its scenarios carry ground truth (gates Trajectory*
    evaluators), the DRAFT status and the published versions."""
    _require_cloud_visible(db, ws, cloud_id, viewer)
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
    cloud_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """PUBLISH VERSION for a cloud-only dataset (no local row): CreateDatasetVersion,
    poll to ACTIVE, return the refreshed detail with the new version listed."""
    _require_cloud_visible(db, ws, cloud_id, viewer)
    client = control_client(ws.context)
    final = _publish_version(client, cloud_id)
    return _cloud_detail_out(client, final)


@router.delete("/datasets/cloud/{cloud_id}/versions/{version}")
def delete_cloud_dataset_version(
    cloud_id: str,
    version: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Delete ONE published version (DeleteDataset with datasetVersion); the draft
    and the other versions stay. A local row's cached version list is refreshed."""
    _require_cloud_visible(db, ws, cloud_id, viewer)
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


def _hidden_cloud_ids(db: Session, ws: WorkspaceScope, viewer: Viewer) -> set[str]:
    """Cloud dataset ids hidden from `viewer`: a cloud dataset follows the local
    rows linked to it — visible when ANY of them is — and one with no local row
    (made outside the console) has no known creator, so it stays shared."""
    if not viewer.filters:
        return set()
    linked: dict[str, bool] = {}
    for row in (
        db.query(EvalDataset)
        .filter(EvalDataset.workspace_id == ws.id, EvalDataset.cloud.isnot(None))
        .all()
    ):
        cloud_id = (row.cloud or {}).get("dataset_id")
        if cloud_id:
            linked[cloud_id] = linked.get(cloud_id, False) or visibility.sees(
                viewer, db, row.created_by
            )
    return {cloud_id for cloud_id, seen in linked.items() if not seen}


def _require_cloud_visible(
    db: Session, ws: WorkspaceScope, cloud_id: str, viewer: Viewer
) -> None:
    if cloud_id in _hidden_cloud_ids(db, ws, viewer):
        raise NotFoundError("dataset.not_found", "dataset not found")


@router.delete("/datasets/cloud/{cloud_id}")
def delete_cloud_dataset(
    cloud_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_cloud_visible(db, ws, cloud_id, viewer)
    ac.delete_dataset(control_client(ws.context), dataset_id=cloud_id)
    # A local row pointing at this cloud dataset loses its live copy — the next
    # sync creates a fresh dataset.
    for row in _rows_with_cloud(db, ws, cloud_id):
        row.cloud = {**row.cloud, "status": "deleted"}
    db.commit()
    return {"datasetId": cloud_id, "deleted": True}


# ─── evaluators ──────────────────────────────────────────────────────────────
_LIST_DEFINITION = {"Custom": "judge", "CustomDerived": "derived", "CustomCode": "code"}


@router.get("/evaluators")
def list_evaluators(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    builtin = [{**row, **creators.empty()} for row in _builtin_evaluator_rows()]
    live: list[dict[str, Any]] = []
    offered: set[str] = set()
    try:
        for ev in ac.list_evaluators(control_client(ws.context)):
            evaluator_id = ev.get("evaluatorId", "")
            if evaluator_id.startswith("Builtin."):
                offered.add(evaluator_id)
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
                    # ListEvaluators carries no config, so the kind comes from
                    # evaluatorType (Custom / CustomDerived / CustomCode).
                    "definition": (
                        None if third_party else _LIST_DEFINITION.get(evaluator_type, "judge")
                    ),
                }
            )
    except Exception as exc:
        # Builtins still render, but the console must not read an empty custom
        # section as "this account has none" (an operator would re-create one).
        logger.warning(
            "ListEvaluators failed for workspace %s: %s", ws.id, type(exc).__name__
        )
        return {
            "evaluators": builtin,
            "builtin_count": len(builtin),
            "custom_unavailable": _listing_failure(exc),
        }
    # the creator of an evaluator made through the console (AWS keeps no ledger
    # row of it); third-party and out-of-console ones read null
    hidden = visibility.hidden_aws_ids(
        viewer, db, ws.id, creators.EVALUATOR, (row["id"] for row in live)
    )
    live = [row for row in live if row["id"] not in hidden]
    # A documented built-in can still be rolling out in this region, and
    # StartBatchEvaluation refuses the whole run over one it does not have. A
    # listing that names no built-ins says nothing about them, so it hides none.
    if offered:
        builtin = [row for row in builtin if row["id"] in offered]
    known = creators.lookup(db, ws.id, creators.EVALUATOR, (row["id"] for row in live))
    for row in live:
        row.update(known.get(row["id"]) or creators.empty())
    return {"evaluators": builtin + live, "builtin_count": len(builtin)}


def _builtin_evaluator_rows() -> list[dict[str, Any]]:
    return [
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


def _listing_failure(exc: Exception) -> dict[str, str]:
    """`{code, message}` for a failed account listing.

    Never the raw AWS text — it names the workspace role ARN and the account id.
    """
    if is_assume_role_failure(exc):
        return {
            "code": "workspace.assume_role_failed",
            "message": "cannot assume this workspace's role",
        }
    mapped = mapped_aws_error(exc)
    if mapped is not None:
        code = mapped[1]
        return {"code": code, "message": public_aws_message(code)}
    return {
        "code": "evaluators.list_failed",
        "message": "the account's evaluators could not be listed",
    }


_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
# Lambda function ARN, optionally qualified with a version or alias.
_LAMBDA_ARN_PATTERN = (
    r"^arn:aws(-[a-z]+)?:lambda:[a-z0-9-]+:\d{12}:function:[A-Za-z0-9_-]+"
    r"(:[A-Za-z0-9_$-]+)?$"
)
DEFAULT_LAMBDA_TIMEOUT_S = 60

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
    # Code-based definition: a Lambda in the workspace Region implements the
    # scoring; the service invokes it with the platform's evaluation
    # execution role (lambda:InvokeFunction + lambda:GetFunction on the
    # function, and a resource policy trusting bedrock-agentcore.amazonaws.com
    # — neither is managed by the console).
    lambda_arn: str | None = Field(default=None, pattern=_LAMBDA_ARN_PATTERN)
    lambda_timeout_s: int | None = Field(default=None, ge=1, le=300)
    model_id: str = Field(default_factory=ac.judge_default_model_id)
    level: str = Field(default="TRACE", pattern="^(TOOL_CALL|TRACE|SESSION)$")
    description: str = Field(default="", max_length=200)  # AWS EvaluatorDescription max
    rating_scale: list[RatingScaleItem] | None = Field(default=None, min_length=2)


def _definition_kind(req: "JudgeCreate | JudgeUpdate") -> str:
    """``judge`` / ``derived`` / ``code`` — which of the three definitions a
    payload carries. Callers run :func:`_require_one_definition` first."""
    if req.lambda_arn is not None:
        return "code"
    if req.base_evaluator_id is not None:
        return "derived"
    return "judge"


def _require_one_definition(req: "JudgeCreate | JudgeUpdate") -> None:
    given = [
        field
        for field in ("instructions", "base_evaluator_id", "lambda_arn")
        if getattr(req, field) is not None
    ]
    if len(given) != 1:
        raise AppError(
            "evaluator.definition_ambiguous",
            "provide exactly one of instructions (LLM-as-a-judge), "
            "base_evaluator_id (derived evaluator) or lambda_arn (code-based)",
            status_code=400,
        )
    if req.rating_scale and given[0] != "instructions":
        raise AppError(
            "evaluator.rating_scale_not_allowed",
            "derived evaluators inherit the base evaluator's rating scale; "
            "code-based evaluators return their own label/value",
            status_code=400,
        )


def _require_lambda_region(lambda_arn: str, workspace_region: str) -> None:
    """A code-based evaluator's Lambda must live in the evaluator's Region."""
    arn_region = lambda_arn.split(":")[3]
    if arn_region != workspace_region:
        raise AppError(
            "evaluator.lambda_region_mismatch",
            f"the Lambda is in {arn_region} but the workspace is {workspace_region} — "
            "a code-based evaluator's function must be in the same Region",
            {"lambda_region": arn_region, "workspace_region": workspace_region},
            status_code=422,
        )


def _lambda_timeout(req: "JudgeCreate | JudgeUpdate") -> int:
    return (
        req.lambda_timeout_s if req.lambda_timeout_s is not None else DEFAULT_LAMBDA_TIMEOUT_S
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


def _current_definition(detail: dict[str, Any]) -> str:
    """Definition kind of a GetEvaluator record — keyed on which config member
    is present (``evaluatorType`` is absent on some records)."""
    config = detail.get("evaluatorConfig") or {}
    if config.get("codeBased"):
        return "code"
    if config.get("derived"):
        return "derived"
    return "judge"


def _evaluator_out(detail: dict[str, Any]) -> dict[str, Any]:
    config = detail.get("evaluatorConfig") or {}
    judge = config.get("llmAsAJudge") or {}
    derived = config.get("derived") or {}
    lambda_config = (config.get("codeBased") or {}).get("lambdaConfig") or {}
    model_config = ((derived or judge).get("modelConfig") or {}).get(
        "bedrockEvaluatorModelConfig"
    ) or {}
    return {
        "id": detail.get("evaluatorId"),
        "name": detail.get("evaluatorName"),
        "level": detail.get("level"),
        "description": detail.get("description"),
        "definition": _current_definition(detail),
        "instructions": "" if (derived or lambda_config) else judge.get("instructions"),
        "rating_scale": (judge.get("ratingScale") or {}).get("numerical", []),
        "model_id": model_config.get("modelId"),
        "base_evaluator_id": derived.get("baseEvaluatorId"),
        "lambda_arn": lambda_config.get("lambdaArn"),
        "lambda_timeout_s": lambda_config.get("lambdaTimeoutInSeconds"),
        "evaluator_type": detail.get("evaluatorType"),
        "provider": detail.get("provider"),
        "status": detail.get("status"),
    }


@router.post("/evaluators", status_code=201)
def create_judge(
    req: JudgeCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _require_one_definition(req)
    client = control_client(ws.context)
    if req.lambda_arn:
        _require_lambda_region(req.lambda_arn, ws.context.region)
        created = ac.create_code_evaluator(
            client,
            name=req.name,
            lambda_arn=req.lambda_arn,
            lambda_timeout_s=_lambda_timeout(req),
            level=req.level,
            description=req.description,
        )
    elif req.base_evaluator_id:
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
    creator = Creator.of_request(request)
    creators.record(db, ws.id, creators.EVALUATOR, created.get("evaluatorId") or "", creator)
    return {
        "evaluator_id": created.get("evaluatorId"),
        "arn": created.get("evaluatorArn"),
        # set only when AgentCore refused the requested judge model and the
        # evaluator was created on ac.judge_fallback_model_id() instead
        "model_fallback": created.get("launchpadModelFallback"),
        **creators.fields(creator),
    }


@router.get("/evaluators/{evaluator_id}")
def get_evaluator(
    evaluator_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _require_visible_evaluator(db, ws, viewer, evaluator_id)
    detail = _evaluator_out(
        ac.get_evaluator(control_client(ws.context), evaluator_id=evaluator_id)
    )
    return {**detail, **creators.lookup_one(db, ws.id, creators.EVALUATOR, evaluator_id)}


class JudgeUpdate(BaseModel):
    instructions: str | None = Field(default=None, min_length=10, max_length=4000)
    base_evaluator_id: str | None = Field(
        default=None, pattern=r"^(Builtin|ThirdParty)\.[A-Za-z0-9_.]+$"
    )
    lambda_arn: str | None = Field(default=None, pattern=_LAMBDA_ARN_PATTERN)
    lambda_timeout_s: int | None = Field(default=None, ge=1, le=300)
    model_id: str = Field(default_factory=ac.judge_default_model_id)
    level: str = Field(default="TRACE", pattern="^(TOOL_CALL|TRACE|SESSION)$")
    description: str = Field(default="", max_length=200)  # AWS EvaluatorDescription max
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
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _reject_managed(evaluator_id)
    _require_visible_evaluator(db, ws, viewer, evaluator_id)
    _require_one_definition(req)
    if req.instructions is not None:
        _require_placeholder(req.instructions)
    client = control_client(ws.context)
    # UpdateEvaluator full-replaces the config, so a payload of the wrong
    # definition type would silently convert the evaluator (a judge into a
    # Lambda evaluator, a Lambda evaluator into a judge, …) — reject instead.
    current = ac.get_evaluator(client, evaluator_id=evaluator_id)
    current_kind = _current_definition(current)
    payload_kind = _definition_kind(req)
    if current_kind != payload_kind:
        raise AppError(
            "evaluator.definition_mismatch",
            "update payload must match the evaluator's definition type "
            "(instructions for LLM-as-a-judge, base_evaluator_id for derived, "
            "lambda_arn for code-based)",
            {"current": current_kind, "payload": payload_kind},
            status_code=400,
        )
    updated: dict[str, Any] = {}
    if req.lambda_arn:
        _require_lambda_region(req.lambda_arn, ws.context.region)
        ac.update_code_evaluator(
            client,
            evaluator_id=evaluator_id,
            lambda_arn=req.lambda_arn,
            lambda_timeout_s=_lambda_timeout(req),
            level=req.level,
            description=req.description,
        )
    elif req.base_evaluator_id:
        updated = ac.update_derived_evaluator(
            client,
            evaluator_id=evaluator_id,
            description=req.description,
            base_evaluator_id=req.base_evaluator_id,
            model_id=req.model_id,
            level=_resolve_base_level(client, req.base_evaluator_id),
        )
    else:
        updated = ac.update_evaluator(
            client,
            evaluator_id=evaluator_id,
            instructions=req.instructions,
            rating_scale=_rating_scale_payload(req.rating_scale),
            model_id=req.model_id,
            level=req.level,
            description=req.description,
        )
    return {
        **_evaluator_out(ac.get_evaluator(client, evaluator_id=evaluator_id)),
        "model_fallback": updated.get("launchpadModelFallback"),
    }


@router.delete("/evaluators/{evaluator_id}")
def delete_evaluator(
    evaluator_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _reject_managed(evaluator_id)
    _require_visible_evaluator(db, ws, viewer, evaluator_id)
    from app.assistant.evaluation_assets import managed_evaluator

    owner = managed_evaluator(db, ws.id, evaluator_id)
    if owner is not None:
        # an evaluator created by an evaluation-assets operation has a Lambda / IAM
        # footprint that only the operation's cleanup removes — deleting the record
        # alone would leave undeclared orphans
        raise AppError(
            "evaluator.managed_by_operation",
            "this evaluator was created by an assistant evaluation-assets operation; remove it "
            "through that operation's cleanup so its Lambda/IAM artifacts go with it",
            owner, status_code=409,
        )
    ac.delete_evaluator(control_client(ws.context), evaluator_id=evaluator_id)
    creators.forget(db, ws.id, creators.EVALUATOR, evaluator_id)
    return {"deleted": True}


# ─── runs ────────────────────────────────────────────────────────────────────
LOG_GROUP_NAME_RE = r"^[.\-_/#A-Za-z0-9]{1,512}$"
# a service.name as spans carry it; no whitespace or quotes (it is echoed into
# CloudWatch filters by the discovery routes)
SERVICE_NAME_RE = r"^[A-Za-z0-9._:/@#\-]{1,256}$"


class LogSource(BaseModel):
    """An agent's telemetry already in CloudWatch Logs, with no platform agent
    behind it — `StartBatchEvaluation`'s `cloudWatchLogs` data source as is."""

    service_name: str = Field(pattern=SERVICE_NAME_RE)
    log_group_names: list[Annotated[str, Field(pattern=LOG_GROUP_NAME_RE)]] = Field(
        min_length=1, max_length=10
    )


class RunCreate(BaseModel):
    # exactly one target: a platform agent, or a CloudWatch log source
    agent_id: str | None = None
    log_source: LogSource | None = None
    # optional operator-facing task name/description (console V2 evaluation tasks)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
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
    # where session_ids came from, for display only: "logs" = runtime log streams
    # picked in the V2 task wizard (recorded as dataset_name "logs:<n>")
    session_source: Literal["logs"] | None = None
    lookback_hours: int | None = Field(default=None, ge=1, le=336)  # time window
    insights: list[str] | None = None  # insight-type subset (insights mode)


def _run_out(run: EvalRun, *, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """A run's payload. ``snapshot`` is the agent version it evaluated
    (``scorecard.resolve_snapshots``: ``{seq, source: stamped|inferred|observed}``, or
    null without an agent or a deployed snapshot) — resolved by the caller, a page at
    a time."""
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "agent_name": run.agent_name,
        # {service_name, log_group_names} when the run has no platform agent
        "log_source": run.log_source,
        "name": run.name,
        "description": run.description,
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
        # dataset scenarios that ended on the agent's budget and are scored as they are
        "budget_stops": run.budget_stops or [],
        "scenario_failures": run.scenario_failures or [],
        # sessions a partially failed batch skipped, and why (null when none / older row)
        "session_failures": run.session_failures,
        "error": run.error,
        # `budget_exceeded` when the run stopped at a boundary because its creator
        # reached their monthly spend budget (services/job_budget.py), else null
        "stop_reason": job_budget.stop_reason_of(run.error),
        # additive: an operator stop is pending on this run (in-memory flag;
        # the row turns `stopped` once the poller/worker observes it)
        "stop_requested": service.stop_requested(run.id),
        "snapshot": snapshot,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        **creators.fields(run),
    }


def _run_one(db: Session, run: EvalRun) -> dict[str, Any]:
    """One run's payload, its version resolved on its own (the by-id routes)."""
    return _run_out(run, snapshot=scorecard.resolve_snapshot(db, run))


@router.get("/runs")
def list_runs(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    mode: str | None = Query(None, pattern="^(evaluators|insights)$"),
    agent_id: str | None = Query(None, min_length=1, max_length=32),
    dataset_id: str | None = Query(None, min_length=1, max_length=16),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Newest-first page of runs plus the unpaginated `total`.

    Defaults reproduce the pre-pagination response (latest 50, no filter) so
    older callers are unaffected. `mode` exists for the console's insights
    duplicate guard, which must see insights runs beyond the displayed page;
    `agent_id` for the experiment RECOMMEND card, which offers one agent's own
    completed runs as a trace source and must not miss them behind other agents'
    newer runs; `dataset_id` (a local Dataset id) for the architect assistant's
    NEXT STEPS panel, which lists the runs of one agent on the Dataset the
    evaluation-assets operation created.
    """
    query = db.query(EvalRun).filter(
        EvalRun.workspace_id == ws.id, visibility.clause(viewer, EvalRun.created_by)
    )
    if mode:
        query = query.filter(EvalRun.mode == mode)
    if agent_id:
        query = query.filter(EvalRun.agent_id == agent_id)
    if dataset_id:
        query = query.filter(EvalRun.dataset_id == dataset_id)
    total = query.count()
    rows = (
        query.order_by(EvalRun.created_at.desc()).offset(offset).limit(limit).all()
    )
    snaps = scorecard.resolve_snapshots(db, rows)
    return {
        "runs": [_run_out(r, snapshot=snaps.get(r.id)) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/scorecard")
def get_scorecard(
    agent_id: str | None = Query(None, min_length=1, max_length=32),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Agent scorecards, ledger-only (no AWS call): the workspace's scored runs the
    viewer may see — insights runs excluded, runs of a hidden agent dropped — each
    with its benchmark and agent version, plus the agents they belong to with their
    snapshot timelines. ``agent_id`` narrows to one agent and adds each version's
    changed diff groups. The newest ``SCORECARD_RUN_CAP`` runs, oldest first;
    ``truncated`` says older ones were left out."""
    query = db.query(EvalRun).filter(
        EvalRun.workspace_id == ws.id,
        EvalRun.mode == "evaluators",
        visibility.clause(viewer, EvalRun.created_by),
    )
    if agent_id:
        query = query.filter(EvalRun.agent_id == agent_id)
    hidden = agent_access.hidden_agent_ids(db, viewer, ws.id)
    if hidden:  # in SQL, before the cap
        query = query.filter(EvalRun.agent_id.not_in(hidden))
    cap = scorecard.SCORECARD_RUN_CAP
    rows = query.order_by(EvalRun.created_at.desc(), EvalRun.id.desc()).limit(cap + 1).all()
    return scorecard.scorecard_out(
        db, ws.id, list(reversed(rows[:cap])),
        agent_id=agent_id, viewer=viewer, truncated=len(rows) > cap,
    )


def _replayed_items(
    db: Session, ws: WorkspaceScope, run: EvalRun, viewer: Viewer
) -> list[dict[str, Any]] | None:
    """The items a legacy replay run is assumed to have replayed: the
    local dataset's current items, or the cloud dataset's pinned version (the draft
    when none). None when they cannot be read, or the dataset is hidden from the
    viewer (its items would surface as case inputs) — the compare then shows overall
    scores."""
    scope = scorecard.scope_of(run)
    if scope == scorecard.SCOPE_DATASET:
        dataset = db.get(EvalDataset, run.dataset_id)
        if dataset is None or dataset.workspace_id != ws.id:
            return None
        if not visibility.sees(viewer, db, dataset.created_by):
            return None
        return list(dataset.items or [])
    if scope == scorecard.SCOPE_CLOUD:
        if run.dataset_id in _hidden_cloud_ids(db, ws, viewer):
            return None
        try:
            return _cloud_dataset_items(run.dataset_id, ws, version=run.dataset_version)[1]
        except Exception as exc:  # gone, not ACTIVE, AWS error — the compare degrades
            logger.info("compare: cloud dataset %s unreadable (%s)", run.dataset_id,
                        type(exc).__name__)
            return None
    return None


def _side_results(run: EvalRun, ws: WorkspaceScope) -> tuple[dict[str, Any], dict[str, list]]:
    """(``{available, reason?, detail?, truncated?}``, ``session_id → judge records``) of
    one run; ``truncated`` = the stream held more records than one read returns."""
    read = service.run_results(run, workspace=ws.context)
    status = {"available": bool(read.get("available"))}
    for key in ("reason", "detail", "truncated"):
        if read.get(key):
            status[key] = read[key]
    by_session = {s["session_id"]: list(s.get("results") or []) for s in read.get("sessions") or []}
    return status, by_session


# declared before `/runs/{run_id}`, which would read `compare` as a run id
@router.get("/runs/compare")
def compare_runs(
    a: str = Query(min_length=1, max_length=16),
    b: str = Query(min_length=1, max_length=16),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Two terminal evaluator runs side by side: both runs (with benchmark and
    version), how their sessions align (``scenario`` | ``order`` | ``none`` +
    ``alignment_reason``), the per-case judge records when aligned (one results read
    per side, each degrading on its own), and the agent's configuration change between
    the two versions (the diff itself is ``GET /api/agents/{id}/snapshots/diff``)."""
    run_a = _run_in(db, ws, a, viewer=viewer)
    run_b = _run_in(db, ws, b, viewer=viewer)
    for run in (run_a, run_b):
        if run.mode != "evaluators" or run.status in service.ACTIVE_STATUSES:
            raise AppError(
                "run.compare_unsupported",
                f"run {run.id} cannot be compared: only finished evaluator runs can "
                f"(this one is a {run.mode} run, {run.status})",
                {"run_id": run.id, "mode": run.mode, "status": run.status},
                status_code=422,
            )
    labels = scorecard.dataset_labels(db, [run_a, run_b], workspace_id=ws.id, viewer=viewer)
    snaps = scorecard.resolve_snapshots(db, [run_a, run_b])
    aligned = scorecard.align(run_a, run_b,
                              items_of=lambda r: _replayed_items(db, ws, r, viewer))
    cases: list[dict[str, Any]] = []
    results = None
    if aligned["alignment"] != scorecard.ALIGN_NONE:
        status_a, by_a = _side_results(run_a, ws)
        status_b, by_b = _side_results(run_b, ws)
        results = {"a": status_a, "b": status_b}
        cases = scorecard.build_cases(run_a, run_b, aligned["maps"], {"a": by_a, "b": by_b})
    config = scorecard.config_change(db, run_a, run_b, snaps.get(run_a.id), snaps.get(run_b.id))
    if config is not None and viewer.filters and not agent_access.agent_visible(
            db, viewer, db.get(Agent, run_a.agent_id)):
        # a visible run does not disclose its hidden agent's publish history (isolation)
        config = None
    return {
        "runs": {
            side: {**_run_out(run, snapshot=snaps.get(run.id)),
                   "benchmark": scorecard.benchmark_of(run, labels)}
            for side, run in (("a", run_a), ("b", run_b))
        },
        "alignment": aligned["alignment"],
        "alignment_reason": aligned["reason"],
        "cases": cases,
        "results": results,
        "config": config,
    }


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    return _run_one(db, _run_in(db, ws, run_id, viewer=viewer))


@router.get("/runs/{run_id}/results")
def get_run_results(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Per-session scores + judge explanations of a terminal evaluators run,
    read on demand from the batch's results log stream (never persisted).
    ``available=false`` + ``reason`` when there is nothing to read."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    return service.run_results(run, workspace=ws.context)


@router.post("/runs", status_code=201)
def create_run(
    req: RunCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    return _start_run(req, request, db, ws, viewer)


def _start_run(
    req: RunCreate,
    request: Request,
    db: Session,
    ws: WorkspaceScope,
    viewer: Viewer,
    *,
    actor_fallback: str | None = None,
) -> dict[str, Any]:
    """Validate and queue one run — ``POST /runs`` and the re-run route alike, so
    both apply the same scope, reference, visibility and budget checks.
    ``actor_fallback`` is the actor model a persona dataset gets when the request
    names none (the re-run's platform default); ``POST /runs`` passes none and
    requires the caller's."""
    if bool(req.agent_id) == bool(req.log_source):
        raise AppError(
            "run.target_required",
            "exactly one target required: agent_id or log_source",
            status_code=422,
        )
    agent = None
    if req.agent_id:
        agent = agent_access.agent_in(db, ws.id, req.agent_id, viewer=viewer)
        if agent is None or agent.status != "active":
            raise AppError("agent.not_active", "agent must be active", status_code=400)

    dataset_scope = bool(req.dataset_id) or bool(req.cloud_dataset_id)
    if req.log_source and dataset_scope:
        raise AppError(
            "run.log_source_scope",
            "a CloudWatch log source is evaluated passively — scope it with "
            "session_ids or lookback_hours (a dataset replay needs an agent to invoke)",
            status_code=422,
        )
    scopes = [dataset_scope, bool(req.session_ids), bool(req.lookback_hours)]
    if sum(scopes) != 1 or (req.dataset_id and req.cloud_dataset_id):
        raise AppError(
            "run.scope_required",
            "exactly one scope required: dataset_id, cloud_dataset_id, "
            "session_ids or lookback_hours",
            status_code=422,
        )
    if req.session_source and not req.session_ids:
        raise AppError(
            "run.session_source_scope",
            "session_source only applies to a session_ids scope",
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
        dataset = _dataset_in(db, ws, req.dataset_id, viewer=viewer)
        items = dataset.items
        dataset_name = dataset.name
    elif req.cloud_dataset_id:
        _require_cloud_visible(db, ws, req.cloud_dataset_id, viewer)
        cloud_name, items = _cloud_dataset_items(
            req.cloud_dataset_id, ws, version=req.dataset_version
        )
        # "cloud:" prefix marks the scope in the runs list (like "window:Nh");
        # the pinned version travels on its own column, never in this string.
        dataset_name = f"cloud:{cloud_name}"
    elif req.session_source == "logs":
        # like "window:Nh": the scope rides in dataset_name, no schema change
        dataset_name = f"logs:{len(set(req.session_ids or []))}"

    persona = any("actor_profile" in item for item in items)
    actor_model_id = req.actor_model_id or (actor_fallback if persona else None)
    if persona and not actor_model_id:
        raise AppError(
            "run.actor_model_required",
            "this dataset contains simulated persona scenarios — pick an "
            "actor_model_id (the Bedrock model that plays the user)",
            status_code=422,
        )

    applied_ids = req.evaluators if req.mode == "evaluators" else (req.insights or [])
    if len(set(applied_ids)) > ac.MAX_BATCH_EVALUATORS:
        raise AppError(
            "run.too_many_evaluators",
            f"a batch evaluation applies at most {ac.MAX_BATCH_EVALUATORS} "
            f"{'evaluators' if req.mode == 'evaluators' else 'insights'}; {len(set(applied_ids))} "
            "were selected — drop the ones that overlap or start a second run for the rest",
            {"max": ac.MAX_BATCH_EVALUATORS, "selected": len(set(applied_ids))},
            status_code=422,
        )
    if req.mode == "evaluators":
        _assert_target_references(
            db, ws, req.evaluators, items, dataset_scope,
            agent_spec=agent.spec if agent else None, viewer=viewer,
        )
    log_source = None
    if req.log_source:
        groups = list(dict.fromkeys(req.log_source.log_group_names))
        _assert_log_groups_exist(ws, groups)
        log_source = {"service_name": req.log_source.service_name, "log_group_names": groups}

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

    # The route gate checked the caller's spend (the agent rides in the body, not
    # the path): a replay also invokes the agent, and a persona run the actor model,
    # so a budgeted payer is refused here when either has no price.
    payer = budgets.payer_of_request(request)
    if payer.budgeted and items:
        budgets.check_agent(db, payer, agent)
        if persona:
            budgets.check_model(payer, actor_model_id)

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
        actor_model_id=actor_model_id,
        dataset_version=req.dataset_version if req.cloud_dataset_id else None,
        name=req.name,
        description=req.description,
        log_source=log_source,
        creator=Creator.of_request(request),
    )
    out = _run_one(db, run)
    out["budget_notice"] = None
    if payer.budgeted:
        # the run is allowed either way; it stops at its next boundary once the
        # limit is reached — say so up front when the forecast is more than is left
        forecast = job_budget.eval_run_forecast(
            db, agent=agent, items=items, evaluators=applied, mode=req.mode,
            actor_model_id=actor_model_id,
            session_count=len(set(req.session_ids or [])) or None,
        )
        out["budget_notice"] = job_budget.notice(db, payer.user_id, forecast)
    return out


def _assert_log_groups_exist(ws: WorkspaceScope, groups: list[str]) -> None:
    """A missing input log group only surfaces as a failed batch minutes later
    — refuse it up front instead (one DescribeLogGroups per group, ≤ 10)."""
    logs = ws.context.client("logs")
    missing = []
    for name in groups:
        found = logs.describe_log_groups(logGroupNamePrefix=name, limit=50).get("logGroups", [])
        if not any(g.get("logGroupName") == name for g in found):
            missing.append(name)
    if missing:
        raise AppError(
            "run.log_group_missing",
            f"log group(s) not found in this workspace: {', '.join(missing)}",
            {"missing": missing},
            status_code=422,
        )


@router.post("/runs/{run_id}/stop", status_code=202)
def stop_run(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Stop an active run. With a batch on AWS this is StopBatchEvaluation
    (202: the batch goes STOPPING → STOPPED and the row follows); a queued run
    is cancelled locally and comes back `stopped` at once; a run replaying its
    dataset stops before StartBatchEvaluation. Terminal runs → 409
    `run.not_active`."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    return _run_one(db, service.request_stop(run.id, workspace=ws.context))


@router.post("/runs/{run_id}/recheck", status_code=202)
def recheck_run(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Re-read a failed run's batch from AWS: a terminal batch settles the row
    (e.g. `completed` with scores); one still running resumes polling
    (`evaluating`). Only failed runs with a batch → else 409 `run.not_recheckable`."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    return _run_one(db, service.recheck_run(run.id, workspace=ws.context))


class RunRerun(BaseModel):
    # the new run's task name; default "<source name> · 再评"
    name: str | None = Field(default=None, min_length=1, max_length=64)


RERUN_SUFFIX = " · 再评"


def _rerun_request(source: EvalRun, name: str | None) -> RunCreate:
    """The ``RunCreate`` that evaluates ``source``'s target again on the same scope:
    the same dataset (a cloud run's pinned version stays pinned; the draft stays the
    draft) or the same window length, and the same evaluators. Chosen sessions and
    log streams would only re-score the same sessions (judge noise), and insights
    runs have no scores to trend → 422 ``run.rerun_unsupported``."""
    scope = scorecard.scope_of(source)
    hours = scorecard.window_hours(source) if scope == scorecard.SCOPE_WINDOW else None
    if source.mode != "evaluators" or (
        scope not in (scorecard.SCOPE_DATASET, scorecard.SCOPE_CLOUD) and not hours
    ):
        raise AppError(
            "run.rerun_unsupported",
            "only a dataset or time-window evaluator run can be evaluated again "
            f"(this one is a {source.mode} run on a {scope} scope)",
            {"scope": scope, "mode": source.mode},
            status_code=422,
        )
    fields: dict[str, Any] = {
        # a re-run of a re-run keeps one suffix
        "name": (name or f"{(source.name or source.agent_name).removesuffix(RERUN_SUFFIX)}"
                         f"{RERUN_SUFFIX}")[:64],
        "description": source.description,
        "evaluators": list(source.evaluators or []),
        "mode": "evaluators",
    }
    if source.agent_id:
        fields["agent_id"] = source.agent_id
    else:
        fields["log_source"] = source.log_source
    if scope == scorecard.SCOPE_DATASET:
        fields["dataset_id"] = source.dataset_id
    elif scope == scorecard.SCOPE_CLOUD:
        fields["cloud_dataset_id"] = source.dataset_id
        fields["dataset_version"] = source.dataset_version
    else:
        fields["lookback_hours"] = hours
    try:
        return RunCreate(**fields)
    except ValidationError as exc:
        raise AppError(
            "run.rerun_unsupported",
            "this run's stored scope can no longer start a run",
            {"errors": exc.errors(include_url=False, include_context=False)},
            status_code=422,
        ) from exc


@router.post("/runs/{run_id}/rerun", status_code=201)
def rerun_run(
    run_id: str,
    request: Request,
    body: RunRerun | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Evaluate a run's target again, as it is now, on the run's own scope — a new
    run, exactly as ``POST /runs`` would start it (same validation, visibility,
    budget refusals and ``budget_notice``). A simulated-persona dataset plays the
    user with the platform's current actor model."""
    source = _run_in(db, ws, run_id, viewer=viewer)
    req = _rerun_request(source, body.name if body else None)
    # the platform's effective actor model (console override included); only a
    # persona dataset uses it
    return _start_run(req, request, db, ws, viewer,
                      actor_fallback=get_settings().actor_model_id)


# ─── recommendations seeded from a run ──────────────────────────────────────
class RecommendationTool(BaseModel):
    name: str = Field(min_length=1, max_length=recommendations.TOOL_NAME_MAX)
    description: str = Field(default="", max_length=recommendations.TOOL_DESCRIPTION_MAX)


class RecommendationCreate(BaseModel):
    kinds: list[Literal["system_prompt", "tool_descriptions"]] = Field(min_length=1)
    # where the confirmed inputs came from — display only (harness | spec | manual)
    input_source: Literal["harness", "spec", "manual"] = "manual"
    system_prompt: str | None = Field(default=None, max_length=recommendations.SYSTEM_PROMPT_MAX)
    evaluator: str | None = Field(default=None, max_length=256)
    tools: list[RecommendationTool] | None = Field(default=None, max_length=100)
    # who generates the system-prompt recommendation: the AgentCore job (default) or a
    # registered 3rd-party provider reflecting on this run (tool descriptions stay AWS)
    provider: Literal["agentcore", "gepa_lite"] | None = None
    model_id: str | None = Field(default=None, max_length=200)


class RecommendationEdit(BaseModel):
    # the operator's revision of the recommended prompt; null / empty / unchanged
    # text drops a saved revision (back to the recommendation as generated)
    system_prompt: str | None = Field(default=None, max_length=recommendations.SYSTEM_PROMPT_MAX)


class RecommendationAccept(BaseModel):
    # the reviewed prompt to publish — the saved revision (``edit``) or else the
    # recommendation as generated when omitted; any difference from the generated
    # text (e.g. removing a loosened boundary) is recorded as an edit
    system_prompt: str | None = Field(default=None, max_length=recommendations.SYSTEM_PROMPT_MAX)


def _run_in(
    db: Session, ws: WorkspaceScope, run_id: str, *, viewer: Viewer = Viewer.ALL
) -> EvalRun:
    """The run, or 404 — a run of another workspace, or one hidden from the viewer
    by workspace isolation, reads as absent, like every other foreign id."""
    run = db.get(EvalRun, run_id)
    if run is None or run.workspace_id != ws.id:
        raise NotFoundError("run.not_found", "run not found")
    visibility.require_seen(
        viewer, db, run.created_by, NotFoundError("run.not_found", "run not found")
    )
    return run


@router.get("/runs/{run_id}/recommendation-inputs")
def get_recommendation_inputs(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The current system prompt + tool descriptions a recommendation would revise.

    ``source`` says where they came from: ``harness`` (live GetHarness, incl. the
    attached Gateways' tool schemas), ``spec`` (the Launchpad agent spec) or
    ``manual`` (nothing readable — the operator must enter them)."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    # a visible run does not disclose a hidden agent's prompt / tools (isolation)
    agent = db.get(Agent, run.agent_id) if run.agent_id and viewer.filters else None
    return recommendations.resolve_inputs(
        db, run, ws.context,
        agent_visible=agent is None or agent_access.agent_visible(db, viewer, agent),
    )


@router.get("/runs/{run_id}/recommendations")
def list_run_recommendations(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Recommendations started from this run, newest first; each non-terminal one
    is refreshed from GetRecommendation on read."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    rows = recommendations.list_for_run(db, run, ws.context)
    return {"recommendations": [recommendations.out(r) for r in rows]}


@router.post("/runs/{run_id}/recommendations", status_code=201)
def create_run_recommendations(
    run_id: str,
    req: RecommendationCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Start one StartRecommendation job per requested kind, its traces pinned to
    this run's batch evaluation (only completed runs with a batch → else 409). The
    caller is recorded as the requester: a 3rd-party provider's model calls are
    charged to them, and a budgeted caller is refused when that model has no price."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    if req.provider and req.provider != recommendations.AGENTCORE_PROVIDER:
        from app.optimization import providers as rec_providers

        if req.provider in rec_providers.PROVIDER_IDS:
            model = (req.model_id or "").strip() or (
                rec_providers.get_provider(req.provider).default_model_id() or None)
            budgets.check_model(budgets.payer_of_request(request), model)
    rows = recommendations.start(
        db, run, ws.context,
        kinds=list(dict.fromkeys(req.kinds)),
        input_source=req.input_source,
        system_prompt=req.system_prompt,
        evaluator=req.evaluator,
        tools={t.name: t.description for t in req.tools or []},
        provider=req.provider,
        model_id=req.model_id,
        requester=Creator.of_request(request),
    )
    return {"recommendations": [recommendations.out(r) for r in rows]}


@router.put("/runs/{run_id}/recommendations/{rec_id}/edit")
def edit_run_recommendation(
    run_id: str,
    rec_id: str,
    req: RecommendationEdit,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Save the operator's revision of a COMPLETED system-prompt recommendation without
    publishing anything; the card, copy and accept then use it. Ledger-only."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    row = db.get(EvalRecommendation, rec_id)
    if row is None or row.run_id != run.id or row.workspace_id != run.workspace_id:
        raise NotFoundError("recommendation.not_found", "recommendation not found")
    identity = getattr(request.state, "identity", None)
    recommendations.save_edit(db, row, req.system_prompt,
                              by=getattr(identity, "username", None) or "unknown")
    return {"recommendation": recommendations.out(row)}


@router.post("/runs/{run_id}/recommendations/{rec_id}/accept", status_code=202)
def accept_run_recommendation(
    run_id: str,
    rec_id: str,
    request: Request,
    req: RecommendationAccept | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Accept a COMPLETED system-prompt recommendation: re-publish the run's Harness
    with the recommended prompt (UpdateHarness → a NEW Harness version; DEFAULT
    follows it). Same guards and async deploy job as ``POST /agents/{id}/redeploy``;
    the acceptance is recorded on the recommendation (once)."""
    from app.deployer.harness import strip_generated_prompt
    from app.routers.agents import republish_agent

    run = _run_in(db, ws, run_id, viewer=viewer)
    # a visible run does not make its agent the caller's to re-publish (isolation):
    # refused before the target checks, which would describe the hidden agent
    agent_access.require_agent_visible(
        db, viewer, db.get(Agent, run.agent_id) if run.agent_id else None,
        NotFoundError("agent.not_found", "the run's agent no longer exists"),
    )
    row, agent, prompt = recommendations.acceptance_target(db, run, rec_id)
    reviewed = recommendations.edited_prompt(row)
    if req is not None and req.system_prompt is not None:
        reviewed = req.system_prompt.strip()
        if not reviewed:
            raise AppError("recommendation.accept_prompt_empty",
                           "the reviewed prompt is empty", status_code=422)
    edited = reviewed is not None and reviewed != prompt
    prompt = reviewed or prompt
    previous_version = agent.version
    current = AgentSpec(**(agent.spec or {}))
    # the recommendation revised the LIVE prompt, which already carries the
    # KB section the deployer appends — keep only the operator-owned part
    spec = current.model_copy(update={"system_prompt": strip_generated_prompt(current, prompt)})
    spec = AgentSpec(**spec.model_dump())
    result = republish_agent(
        db, agent, spec, ws, actor=Creator.of_request(request),
        note="recommendation accepted", viewer=viewer,
    )
    identity = getattr(request.state, "identity", None)
    recommendations.record_acceptance(
        db, row,
        by=getattr(identity, "username", None) or "unknown",
        agent_id=agent.id,
        previous_version=previous_version,
        job_id=result["job_id"],
        deployment_id=result["deployment_id"],
        edited=edited,
    )
    return {**result, "recommendation": recommendations.out(row)}


DELETABLE_RUN_STATUSES = ("failed", "stopped")


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Remove the ledger row of a run that produced no result — ``failed`` or
    ``stopped``. Completed runs are the evaluation history and stay; an active run is
    stopped first (409 ``run.not_deletable``). Only the row goes: a batch evaluation that
    reached AWS is left as it is (AWS stays the source of truth; the row was the
    console's pointer to it, and the response says whether one existed)."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    if run.status not in DELETABLE_RUN_STATUSES:
        raise AppError(
            "run.not_deletable",
            f"run {run_id} is {run.status}; only failed or stopped runs can be removed "
            "(stop an active run first; completed runs are kept as history)",
            {"status": run.status}, status_code=409,
        )
    batch_id = run.batch_eval_id
    db.delete(run)
    db.commit()
    return {"deleted": True, "run_id": run_id, "status": run.status,
            "aws_batch_left_in_place": batch_id}


@router.get("/queue")
def queue_state(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
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
                    EvalRun.id.in_(named),
                    EvalRun.workspace_id == ws.id,
                    visibility.clause(viewer, EvalRun.created_by),
                )
            )
        )
    return {
        **state,
        "running": [run_id for run_id in state["running"] if run_id in mine],
        "queued": [run_id for run_id in state["queued"] if run_id in mine],
    }
