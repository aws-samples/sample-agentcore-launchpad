"""Evaluation API — datasets, evaluators, runs, insights, queue state.

Adapted from agentcore_eva_opt routers (datasets/evaluators/runs/insights).
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SQLITE_MAX_INT, get_db
from app.core.errors import AppError, NotFoundError
from app.evaluation import (
    agent_tools,
    datasets_local,
    evaluators_local,
    recommendations,
    references,
    run_failure,
    run_stats,
    scorecard,
    service,
)
from app.evaluation.models import EvalDataset, EvalEvaluator, EvalRun
from app.evaluation.queue import run_queue
from app.evaluation.scenarios import (
    RUN_SPLITS,
    SPLIT_ALL,
    SPLITS,
    has_expected_trajectory,
    item_splits,
    normalize_scenarios,
    select_split,
    split_counts,
)
from app.models.ledger import Agent
from app.optimization import providers as rec_providers
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas import model_catalog
from app.schemas.agent import (
    MODEL_NOT_VISIBLE_CODE,
    AgentSpec,
    assert_model_available,
    assert_model_visible,
    model_refusal_code,
    principal_of_identity,
)
from app.services import agent_access, creators, ownership, visibility
from app.services.creators import Creator
from app.services.visibility import Viewer, require_viewer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/eval", tags=["evaluation"])


# ─── datasets ────────────────────────────────────────────────────────────────
MAX_DATASET_ITEMS = 200  # DatasetCreate / DatasetUpdate / the JSONL upload (N16-obs13)


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    locale: str = "en"
    description: str = Field(default="", max_length=1000)
    items: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_DATASET_ITEMS)
    # N17-obs407: the agent the scenarios are written for — its runtime tool names gate
    # every expected_trajectory (omitted = no link, nothing to check against)
    agent_id: str | None = Field(default=None, min_length=1, max_length=32)

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


def _invalid(message: str, reason: str, **where: Any) -> AppError:
    """N17-obs424: ``dataset.invalid_item`` with a machine-readable ``detail`` — ``{reason,
    item?, turn?, line?, scenario_id?}`` — so the console states WHICH item and why in the
    reader's language instead of the English message."""
    return AppError("dataset.invalid_item", message, {"reason": reason, **where},
                    status_code=422)


def _validate_items(items: list[dict[str, Any]]) -> None:
    """Per-item shape checks: predefined scenario items (they carry ``turns``)
    need a unique scenario_id and non-empty turns; simulated persona items
    (they carry ``actor_profile``) need a unique scenario_id, an initial input
    and the actor's context/goal; prompt items need a prompt."""
    seen_ids: set[str] = set()

    def check_scenario_id(idx: int, item: dict[str, Any]) -> None:
        scenario_id = str(item.get("scenario_id") or "").strip()
        if not scenario_id:
            raise _invalid(f"item {idx}: scenario_id required", "scenario_id_required", item=idx)
        if scenario_id in seen_ids:
            raise _invalid(f"item {idx}: duplicate scenario_id '{scenario_id}'",
                           "scenario_id_duplicate", item=idx, scenario_id=scenario_id)
        seen_ids.add(scenario_id)

    for idx, item in enumerate(items, 1):
        if item.get("split") is not None and item.get("split") not in SPLITS:
            # D3-d / R3-c: absent = the deterministic default (`scenarios.default_split`)
            raise _invalid(f"item {idx}: split must be one of {', '.join(SPLITS)} (or omitted)",
                           "split_invalid", item=idx)
        if "turns" in item:
            check_scenario_id(idx, item)
            turns = item.get("turns")
            if not isinstance(turns, list) or not turns:
                raise _invalid(f"item {idx}: turns must be a non-empty list", "turns_required",
                               item=idx)
            for turn_no, turn in enumerate(turns, 1):
                if not _turn_input_text(turn):
                    raise _invalid(f"item {idx} turn {turn_no}: input required",
                                   "turn_input_required", item=idx, turn=turn_no)
        elif "actor_profile" in item:
            check_scenario_id(idx, item)
            profile = item.get("actor_profile")
            if not str(item.get("input", "")).strip():
                raise _invalid(f"item {idx}: input required", "persona_input_required",
                               item=idx)
            if (
                not isinstance(profile, dict)
                or not str(profile.get("context", "")).strip()
                or not str(profile.get("goal", "")).strip()
            ):
                raise _invalid(f"item {idx}: actor_profile needs context and goal",
                               "persona_profile_required", item=idx)
        elif not str(item.get("prompt", "")).strip():
            raise _invalid(f"item {idx}: prompt required", "prompt_required", item=idx)


def _check_actor_model(items: list[dict[str, Any]], actor_model_id: str | None,
                       resources: dict[str, Any] | None = None,
                       principal: model_catalog.Principal | None = None) -> None:
    """Persona scenarios need an actor model and it must be a CN-catalog id (spec C3): the
    worker builds the actor on ``aws_clients.global_bedrock_model(actor_model_id)``, and an
    id outside the catalog would only fail inside the job."""
    if any("actor_profile" in item for item in items) and not actor_model_id:
        raise AppError(
            "run.actor_model_required",
            "this dataset contains simulated persona scenarios — pick an "
            "actor_model_id (the Bedrock model that plays the user)",
            status_code=422,
        )
    if actor_model_id:
        try:
            # N17-obs203 §6: another member's personal provider's model is refused first
            assert_model_visible(actor_model_id, principal, "actor_model_id")
            # catalog AND a configured provider in this workspace (N-R6-obs1)
            assert_model_available(actor_model_id, "actor_model_id", resources)
        except ValueError as exc:
            code = model_refusal_code(exc)
            if code == MODEL_NOT_VISIBLE_CODE:
                code = "run.model_not_visible"
            raise AppError(
                code, str(exc).split(": ", 1)[-1],
                {"actor_model_id": actor_model_id}, status_code=422,
            ) from exc


def _check_judge_model(judge_model_id: str | None, resources: dict[str, Any] | None = None,
                       principal: model_catalog.Principal | None = None) -> None:
    """P3.2 (N17-obs350): a run's chosen judge must be a visible catalog model with a
    configured provider, like the actor — an unusable id would only fail inside the job."""
    if not judge_model_id:
        return
    try:
        assert_model_visible(judge_model_id, principal, "judge_model_id")
        assert_model_available(judge_model_id, "judge_model_id", resources)
    except ValueError as exc:
        code = model_refusal_code(exc)
        if code == MODEL_NOT_VISIBLE_CODE:
            code = "run.model_not_visible"
        raise AppError(
            code, str(exc).split(": ", 1)[-1],
            {"judge_model_id": judge_model_id}, status_code=422,
        ) from exc


def _infer_kind(items: list[dict[str, Any]]) -> str:
    if any("actor_profile" in item for item in items):
        return "simulated"
    return "predefined" if any("turns" in item for item in items) else "legacy"


def _has_ground_truth(items: list[dict[str, Any]]) -> bool:
    for scenario in normalize_scenarios(items):
        if scenario.get("assertions") or has_expected_trajectory(scenario):
            return True
        if any(t.get("expected_response") for t in scenario.get("turns", [])):
            return True
    return False


def _assert_target_references(
    db: Session, ws: WorkspaceScope, evaluators: list[str], items: list[dict[str, Any]],
    dataset_scope: bool, *, agent_spec: dict[str, Any] | None = None,
) -> None:
    """Every chosen evaluator that reads ground truth must find it on EVERY target it
    will be applied to — each session or each TRACE turn of the CURRENT dataset
    snapshot — before any run row, invoke or worker job. Builtins resolve from the
    canonical catalog, custom judges / derived from their row, managed code evaluators
    from their owning plan's rules. An unknown custom id is refused with an actionable
    422 before anything downstream happens (the agent would otherwise already have been
    invoked by the time the engine rejected the evaluator). A scope without a dataset
    carries no references at all."""
    from app.evaluation import coverage
    from app.evaluation.managed import managed_evaluator, managed_rules

    problems: dict[str, list[str]] = {}
    for evaluator in dict.fromkeys(evaluators):
        if evaluator in coverage.KNOWN_BUILTINS:
            needs, level = coverage.builtin_needs(evaluator), coverage.KNOWN_BUILTINS[evaluator]
        elif evaluators_local.is_managed_id(evaluator):
            raise AppError(
                "run.evaluator_not_found",
                f"evaluator {evaluator} does not exist in this workspace — "
                "drop it or pick one from the catalog",
                {"evaluator": evaluator}, status_code=422,
            )
        else:
            owner = managed_evaluator(db, ws.id, evaluator)
            definition = evaluators_local.resolve_definitions(db, ws.context, [evaluator])[0]
            rules = managed_rules(db, owner) if owner else None
            needs, _kind = coverage.needs_from_definition(definition, rules)
            level = str(definition.get("level") or "TRACE")
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


def _dataset_agent(
    db: Session, ws: WorkspaceScope, agent_id: str | None, viewer: Viewer
) -> Agent | None:
    """N17-obs407: the dataset's linked agent, or 422 ``dataset.agent_not_found`` (a foreign,
    deleted or isolation-hidden id reads alike)."""
    if not agent_id:
        return None
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None:
        raise AppError("dataset.agent_not_found", "the linked agent does not exist here",
                       {"agent_id": agent_id}, status_code=422)
    return agent


def _resolve_trajectories(
    items: list[dict[str, Any]], agent: Agent | None, code: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """N17-obs407: ``items`` with every ``expected_trajectory`` name as ``agent`` calls the
    tool (``agent_tools.check_trajectories``: the Gateway alias prefix is added or dropped
    when that is the one known spelling) and the rewrites made; a name the agent has no
    spelling of is 422 ``code`` with the unknown names, the agent's tool names and the
    closest ones. No agent, or an agent whose catalogue is open, checks nothing."""
    if agent is None:
        return items, []
    tools = agent_tools.agent_tools(agent.spec)
    check = agent_tools.check_trajectories(items, tools)
    if check.unknown:
        named = "; ".join(f"{sid}: {', '.join(names)}" for sid, names in check.unknown.items())
        raise AppError(
            code,
            f"expected_trajectory names tools agent {agent.name} does not have ({named}) — "
            "use the names the agent calls its tools by (see agent_tools)",
            {"agent_id": agent.id, **check.detail(tools)}, status_code=422,
        )
    return check.items, check.rewrites


def _dataset_out(
    dataset: EvalDataset, rewrites: list[dict[str, str]] | None = None
) -> dict[str, Any]:
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
        # D3-c: bumped on every change of the items — the version a local run replayed
        "revision": dataset.revision or 1,
        # D3-d / R3-c: scenarios per split ({tuning, held_out}; explicit or the default) and
        # each item's split, in item order
        "split_counts": split_counts(dataset.items or []),
        "item_splits": item_splits(dataset.items or []),
        "agent_id": dataset.agent_id,
        "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
        **creators.fields(dataset),
        # N17-obs407 (write responses only): the trajectory names stored canonically
        **({"tool_name_rewrites": rewrites} if rewrites else {}),
    }


def _dataset_in(
    db: Session, ws: WorkspaceScope, dataset_id: str, *, viewer: Viewer = Viewer.ALL
) -> EvalDataset:
    """The dataset, or 404 — another workspace's dataset, or one workspace isolation
    hides from ``viewer``, is not visible here (the two read alike)."""
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
        .filter(
            EvalDataset.workspace_id == ws.id,
            visibility.clause(viewer, EvalDataset.created_by),  # workspace isolation
        )
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _validate_items(req.items)
    agent = _dataset_agent(db, ws, req.agent_id, viewer)
    items, rewrites = _resolve_trajectories(req.items, agent, "dataset.unknown_tool")
    dataset = EvalDataset(
        workspace_id=ws.id,
        name=req.name,
        locale=req.locale,
        description=req.description,
        items=items,
        kind=_infer_kind(items),
        agent_id=agent.id if agent else None,
    )
    creators.stamp(dataset, Creator.of_request(request))
    db.add(dataset)
    db.commit()
    return _dataset_out(dataset, rewrites)


class DatasetUpload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    locale: str = "en"
    description: str = Field(default="", max_length=1000)
    jsonl: str = Field(min_length=1)
    agent_id: str | None = Field(default=None, min_length=1, max_length=32)  # N17-obs407


@router.post("/datasets/upload", status_code=201)
def upload_dataset(
    req: DatasetUpload,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    # N16-obs16: JSONL lines end at "\n" (a trailing "\r" is tolerated) — never at the
    # Unicode separators `str.splitlines` also breaks on (U+2028, NEL, FS …), which are legal
    # INSIDE a JSON string and would turn one valid line into two broken ones.
    for line_no, line in enumerate(req.jsonl.split("\n"), 1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError) as exc:  # N16-obs14: nesting past the C stack
            raise AppError(
                "dataset.invalid_jsonl", f"line {line_no}: {exc}", {"line": line_no},
                status_code=422,
            ) from exc
        if not isinstance(item, dict):
            raise _invalid(f"line {line_no}: expected an object", "not_an_object",
                           line=line_no)
        items.append(item)
    if not items:
        raise AppError("dataset.empty", "no items in upload", status_code=422)
    # N16-obs13: the same caps as DatasetCreate / DatasetUpdate (an upload is one of them)
    if len(items) > MAX_DATASET_ITEMS:
        raise AppError(
            "dataset.too_many_items",
            f"{len(items)} items in upload — a dataset holds at most {MAX_DATASET_ITEMS}",
            {"items": len(items), "max": MAX_DATASET_ITEMS}, status_code=422,
        )
    for idx, item in enumerate(items, 1):
        try:
            DatasetCreate._cap_item_size([item])
        except ValueError as exc:
            raise _invalid(f"item {idx}: {exc}", "item_too_large", item=idx) from exc
    _validate_items(items)
    agent = _dataset_agent(db, ws, req.agent_id, viewer)
    items, rewrites = _resolve_trajectories(items, agent, "dataset.unknown_tool")
    dataset = EvalDataset(
        workspace_id=ws.id,
        name=req.name,
        locale=req.locale,
        description=req.description,
        items=items,
        kind=_infer_kind(items),
        agent_id=agent.id if agent else None,
    )
    creators.stamp(dataset, Creator.of_request(request))
    db.add(dataset)
    db.commit()
    return _dataset_out(dataset, rewrites)


class DatasetUpdate(BaseModel):
    """Partial update — only provided fields change (kind is immutable)."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    items: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=200)
    # N17-obs407: link ("<id>") or unlink ("") the agent the trajectories are checked against
    agent_id: str | None = Field(default=None, max_length=32)


def _require_dataset_creator(request: Request, dataset: EvalDataset, action: str) -> None:
    """Creator-or-admin on a dataset mutation (N13-obs15, E09): runs after `_dataset_in`
    (a hidden row stays a 404), before any validation or in-use check."""
    ownership.require_creator_or_admin(
        dataset, require_identity(request), "dataset.forbidden",
        message="only the dataset's creator or an administrator may " + action + " it",
        detail={"dataset_id": dataset.id, "action": action},
    )


@router.put("/datasets/{dataset_id}")
def update_dataset(
    dataset_id: str,
    req: DatasetUpdate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    dataset = _dataset_in(db, ws, dataset_id, viewer=viewer)
    _require_dataset_creator(request, dataset, "edit")
    if req.items is not None:
        _validate_items(req.items)
        if _infer_kind(req.items) != dataset.kind:
            raise AppError(
                "dataset.kind_immutable",
                f"dataset kind '{dataset.kind}' cannot change — replacement items "
                "must keep the same shape",
                status_code=400,
            )
    rewrites: list[dict[str, str]] = []
    agent_id = dataset.agent_id if req.agent_id is None else (req.agent_id or None)
    if req.items is not None or agent_id != dataset.agent_id:
        # N17-obs407: the stored items are re-checked when the link changes, new items always
        agent = _dataset_agent(db, ws, agent_id, viewer)
        items, rewrites = _resolve_trajectories(
            req.items if req.items is not None else list(dataset.items or []), agent,
            "dataset.unknown_tool")
        if req.items is not None or rewrites:
            dataset.items = items
        dataset.agent_id = agent_id
    if req.name is not None:
        dataset.name = req.name
    if req.description is not None:
        dataset.description = req.description
    db.commit()
    return _dataset_out(dataset, rewrites)


@router.delete("/datasets/{dataset_id}")
def delete_dataset(
    dataset_id: str,
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Creator-or-admin; 409 ``dataset.in_use`` while an ACTIVE run replays it or a
    pipeline outputs into it (finished runs keep their snapshot and never block), unless an
    administrator passes ``?force=true`` (N13-obs16, E09 NC-11)."""
    dataset = _dataset_in(db, ws, dataset_id, viewer=viewer)
    _require_dataset_creator(request, dataset, "delete")
    forced = references.require_unreferenced(
        references.dataset_referrers(db, ws.id, dataset_id),
        code="dataset.in_use", resource_id=dataset_id, force=force,
        identity=require_identity(request),
    )
    if forced:
        logger.warning("dataset %s force-deleted by %s while referenced",
                       dataset_id, require_identity(request).username)
    db.delete(dataset)
    db.commit()
    return {"deleted": True, **({"forced": True} if forced else {})}


# ─── cloud datasets (S3 snapshots: draft in place + published versions) ──────
# One local row ↔ one snapshot store under ``eval-datasets/<cloud_id>/`` in the
# workspace artifacts bucket (CA-6). The first sync creates it; every later sync
# replaces the DRAFT examples in place; PUBLISH VERSION snapshots the draft as an
# immutable numbered version. The store is the source of truth — the `cloud` blob
# on the row only caches display state.
def _existing_cloud_draft(
    store: datasets_local.SnapshotStore, dataset: EvalDataset
) -> dict[str, Any] | None:
    """The live meta of the row's recorded cloud copy, or None when the row has none,
    the copy was deleted through the console, or the store no longer holds it — those
    all re-create."""
    blob = dataset.cloud or {}
    cloud_id = blob.get("dataset_id")
    if not cloud_id or blob.get("status") == "deleted":
        return None
    return store.meta(str(cloud_id))


@router.post("/datasets/{dataset_id}/sync-to-aws")
def sync_dataset_to_aws(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Push the local dataset to its cloud snapshot store. Without a live cloud copy
    this creates one (``draft.json`` + ``meta.json``); with one, the DRAFT is replaced
    in place — so the cloud id, its published versions and any run pinned to them
    survive; the draft then reads MODIFIED until PUBLISH VERSION snapshots it. The
    recorded `cloud` blob caches id/arn/status plus draft_status, example_count and the
    version list. Creator-or-admin: it rewrites the row's `cloud` blob and the shared
    cloud draft (N13-obs15a)."""
    dataset = _dataset_in(db, ws, dataset_id, viewer=viewer)
    _require_dataset_creator(request, dataset, "sync")
    examples = normalize_scenarios(dataset.items)
    store = datasets_local.store_for(ws.context)
    synced_at = datetime.now(UTC).isoformat()
    existing = _existing_cloud_draft(store, dataset)
    previous = dataset.cloud or {}
    try:
        if existing is not None:
            meta = store.replace_draft(str(existing["datasetId"]), examples)
        else:
            meta = store.create(
                name=datasets_local.sanitize_dataset_name(dataset.name),
                # simulated persona datasets keep their own schema id; scenario and
                # legacy prompt datasets both normalize to predefined
                schema_type=datasets_local.DATASET_SCHEMA_TYPES.get(
                    dataset.kind, datasets_local.DATASET_SCHEMA_TYPES["predefined"]
                ),
                examples=examples,
                description=dataset.description or "",
            )
    except Exception as exc:  # noqa: BLE001 — the store's failure is the sync's failure
        reason = f"{type(exc).__name__}: {exc}"[:300]
        if existing is not None:
            dataset.cloud = datasets_local.cloud_blob(
                store, existing, synced_at=synced_at, status="UPDATE_FAILED",
                failure_reason=reason,
            )
        else:
            dataset.cloud = {
                **datasets_local.cloud_blob(
                    store, {"datasetId": previous.get("dataset_id")}, synced_at=synced_at,
                    status="CREATE_FAILED", failure_reason=reason,
                ),
                "versions": [],
            }
        db.commit()
        raise AppError("dataset.sync_failed", reason, status_code=502) from exc
    dataset.cloud = datasets_local.cloud_blob(store, meta, synced_at=synced_at)
    db.commit()
    return _dataset_out(dataset)


@router.post("/datasets/{dataset_id}/publish-version")
def publish_dataset_version(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Snapshot the cloud copy's DRAFT as the next immutable numbered version.
    The draft stays (draft_status flips to UNMODIFIED); the new version joins
    `cloud.versions`. 409 `dataset.not_synced` without a live cloud copy. Creator-or-admin:
    it rewrites the row's `cloud` blob and adds a version to the shared store (N13-obs15a)."""
    dataset = _dataset_in(db, ws, dataset_id, viewer=viewer)
    _require_dataset_creator(request, dataset, "publish")
    store = datasets_local.store_for(ws.context)
    existing = _existing_cloud_draft(store, dataset)
    if existing is None:
        raise AppError(
            "dataset.not_synced",
            "this dataset has no live cloud copy — SYNC first",
            status_code=409,
        )
    previous = dataset.cloud or {}
    synced_at = previous.get("synced_at") or datetime.now(UTC).isoformat()
    try:
        meta = store.publish_version(str(existing["datasetId"]))
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"[:300]
        dataset.cloud = datasets_local.cloud_blob(
            store, existing, synced_at=synced_at, status="UPDATE_FAILED", failure_reason=reason,
        )
        db.commit()
        raise AppError("dataset.publish_failed", reason, status_code=502) from exc
    dataset.cloud = datasets_local.cloud_blob(store, meta, synced_at=synced_at)
    db.commit()
    return _dataset_out(dataset)


def _cloud_dataset_items(
    cloud_id: str, ws: WorkspaceScope, version: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """(display name, run items) for an ACTIVE cloud dataset — the DRAFT, or the
    published ``version`` when pinned (the scenarios replayed and the ground truth
    gated on are exactly the snapshot's)."""
    store = datasets_local.store_for(ws.context)
    meta = store.require(cloud_id)
    if version:
        known = {str(v.get("datasetVersion")) for v in meta.get("versions") or []}
        if version not in known:
            raise AppError(
                "run.dataset_version_unknown",
                f"cloud dataset has no published version '{version}'",
                status_code=422,
            )
    if meta.get("status") != "ACTIVE":
        raise AppError(
            "dataset.cloud_not_active",
            f"cloud dataset is {meta.get('status')} — only ACTIVE datasets can run",
            status_code=400,
        )
    if meta.get("schemaType") not in datasets_local.RUNNABLE_SCHEMAS:
        raise AppError(
            "run.cloud_dataset_unsupported",
            f"cloud dataset schema '{meta.get('schemaType')}' is not runnable here",
            status_code=422,
        )
    examples = (
        store.version_examples(cloud_id, version) if version else store.draft_examples(cloud_id)
    )
    items = datasets_local.strip_example_ids(examples)
    if not items:
        raise AppError("dataset.empty", "cloud dataset has no examples", status_code=422)
    _validate_items(items)
    return str(meta.get("datasetName") or cloud_id), items


def _hidden_cloud_ids(db: Session, ws: WorkspaceScope, viewer: Viewer) -> set[str]:
    """Cloud dataset ids whose LOCAL rows workspace isolation ALL hide from ``viewer`` —
    a cloud copy follows the rows it was synced from, so one visible row keeps it
    visible. A cloud dataset with no local row (made outside the console) has an
    unknown creator and stays visible."""
    if not viewer.filters:
        return set()
    rows = (
        db.query(EvalDataset)
        .filter(EvalDataset.workspace_id == ws.id, EvalDataset.cloud.isnot(None))
        .all()
    )
    hidden: set[str] = set()
    visible: set[str] = set()
    for row in rows:
        cloud_id = (row.cloud or {}).get("dataset_id")
        if not cloud_id:
            continue
        bucket = visible if visibility.sees(viewer, db, row.created_by) else hidden
        bucket.add(str(cloud_id))
    return hidden - visible


def _require_cloud_mutable(
    request: Request, db: Session, ws: WorkspaceScope, cloud_id: str, action: str
) -> list[EvalDataset]:
    """Creator-or-admin on a cloud-store mutation (N13-obs15a). The store itself records
    no creator: its ownership is DERIVED from the local rows that point at it (several
    creators may have synced into one store), and every one of those rows gets its `cloud`
    blob rewritten by the mutation. The caller must therefore be allowed to mutate EVERY
    pointing row; the first one they may not names itself in the 403 `dataset.forbidden`.
    A store no local row points at (made outside the console) has no derivable owner and
    stays open to every member who sees it — unchanged from before this gate; the
    NULL-creator rule applies to stamped rows, which the store is not. Returns the rows."""
    rows = _rows_with_cloud(db, ws, cloud_id)
    identity = require_identity(request)
    for row in rows:
        ownership.require_creator_or_admin(
            row, identity, "dataset.forbidden",
            message="only the creator of every dataset synced into this cloud store, or an "
                    "administrator, may " + action + " it",
            detail={"cloud_id": cloud_id, "dataset_id": row.id, "action": action},
        )
    return rows


def _require_cloud_visible(db: Session, ws: WorkspaceScope, viewer: Viewer, cloud_id: str) -> None:
    """A hidden cloud dataset answers like a missing one — before any S3 read."""
    if cloud_id in _hidden_cloud_ids(db, ws, viewer):
        raise NotFoundError("dataset.not_found", f"cloud dataset {cloud_id} not found")


@router.get("/datasets/cloud")
def list_cloud_datasets(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    store = datasets_local.store_for(ws.context)
    hidden = _hidden_cloud_ids(db, ws, viewer)
    return {
        "datasets": [
            datasets_local.list_entry(meta)
            for meta in store.list_meta()
            if str(meta.get("datasetId")) not in hidden
        ]
    }


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
    _require_cloud_visible(db, ws, viewer, cloud_id)
    store = datasets_local.store_for(ws.context)
    return _cloud_detail_out(store, store.require(cloud_id))


def _cloud_detail_out(store: datasets_local.SnapshotStore, meta: dict[str, Any]) -> dict[str, Any]:
    cloud_id = str(meta["datasetId"])
    runnable = (
        meta.get("status") == "ACTIVE"
        and meta.get("schemaType") in datasets_local.RUNNABLE_SCHEMAS
    )
    has_ground_truth = False
    if runnable:
        items = datasets_local.strip_example_ids(store.draft_examples(cloud_id))
        has_ground_truth = bool(items) and _has_ground_truth(items)
    return {
        "datasetId": meta.get("datasetId"),
        "name": meta.get("datasetName"),
        "status": meta.get("status"),
        "schemaType": meta.get("schemaType"),
        "exampleCount": meta.get("exampleCount"),
        "draft_status": meta.get("draftStatus"),
        "failure_reason": meta.get("failureReason"),
        "versions": datasets_local.versions_out(meta),
        "runnable": runnable,
        "has_ground_truth": has_ground_truth,
    }


@router.post("/datasets/cloud/{cloud_id}/publish-version")
def publish_cloud_dataset_version(
    cloud_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """PUBLISH VERSION for a cloud-only dataset (no local row): snapshot the draft and
    return the refreshed detail with the new version listed. Creator-or-admin on every
    local row that points at the store (`_require_cloud_mutable`)."""
    _require_cloud_visible(db, ws, viewer, cloud_id)
    _require_cloud_mutable(request, db, ws, cloud_id, "publish")
    store = datasets_local.store_for(ws.context)
    store.require(cloud_id)
    try:
        meta = store.publish_version(cloud_id)
    except Exception as exc:  # noqa: BLE001
        raise AppError("dataset.publish_failed", f"{type(exc).__name__}: {exc}"[:300],
                       status_code=502) from exc
    return _cloud_detail_out(store, meta)


@router.delete("/datasets/cloud/{cloud_id}/versions/{version}")
def delete_cloud_dataset_version(
    cloud_id: str,
    version: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Delete ONE published version; the draft and the other versions stay. A version a
    run replayed is history and is refused (409 ``dataset.version_pinned``). A local
    row's cached version list is refreshed — so the caller must be allowed to mutate every
    pointing row (`_require_cloud_mutable`, 403 before the 409)."""
    _require_cloud_visible(db, ws, viewer, cloud_id)
    rows = _require_cloud_mutable(request, db, ws, cloud_id, "delete a version of")
    pinned = (
        db.query(EvalRun)
        .filter(EvalRun.workspace_id == ws.id, EvalRun.dataset_id == cloud_id,
                EvalRun.dataset_version == version)
        .count()
    )
    if pinned:
        raise AppError(
            "dataset.version_pinned",
            f"version {version} was replayed by {pinned} run(s) and stays as their record",
            {"runs": pinned}, status_code=409,
        )
    store = datasets_local.store_for(ws.context)
    meta = store.delete_version(cloud_id, version)
    for row in rows:
        row.cloud = {**row.cloud, "versions": datasets_local.versions_out(meta)}
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
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """409 ``dataset.in_use`` while an ACTIVE run replays the cloud dataset or any run
    pinned one of its published versions (the `dataset.version_pinned` rule, applied to
    the whole store), unless an administrator passes ``?force=true``. The store records no
    creator: the caller must be allowed to mutate every local row pointing at it
    (`_require_cloud_mutable` — those rows are stamped `cloud.status = "deleted"` below);
    403 before the 409."""
    _require_cloud_visible(db, ws, viewer, cloud_id)
    rows = _require_cloud_mutable(request, db, ws, cloud_id, "delete")
    forced = references.require_unreferenced(
        references.cloud_dataset_referrers(db, ws.id, cloud_id),
        code="dataset.in_use", resource_id=cloud_id, force=force,
        identity=require_identity(request),
    )
    if forced:
        logger.warning("cloud dataset %s force-deleted by %s while referenced",
                       cloud_id, require_identity(request).username)
    datasets_local.store_for(ws.context).delete(cloud_id)
    # A local row pointing at this cloud dataset loses its live copy — the next
    # sync creates a fresh one.
    for row in rows:
        row.cloud = {**row.cloud, "status": "deleted"}
    db.commit()
    return {"datasetId": cloud_id, "deleted": True, **({"forced": True} if forced else {})}


# ─── evaluators ──────────────────────────────────────────────────────────────
@router.get("/evaluators")
def list_evaluators(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The builtin catalog (local, exact) followed by this workspace's custom rows."""
    builtin = [
        {
            "id": name,
            "level": level,
            "source": "builtin",
            "evaluator_type": "Builtin",
            # N17-obs403: an id Global does not have is listed as Launchpad's own
            "provider": ("Launchpad" if name in evaluators_local.LAUNCHPAD_ONLY_BUILTINS
                         else "AWS"),
            # N17-obs425: the normalized score a row must reach to count as passed
            "pass_threshold": evaluators_local.pass_threshold(name),
        }
        for name, level in evaluators_local.ALL_BUILTIN_EVALUATORS.items()
    ] + [
        {
            "id": name,
            "level": level,
            "source": "builtin",
            "requires_ground_truth": True,
            "evaluator_type": "Builtin",
            "provider": "AWS",
            "pass_threshold": evaluators_local.pass_threshold(name),
        }
        for name, level in evaluators_local.TRAJECTORY_EVALUATORS.items()
    ]
    # builtins have no creator; a custom row carries the console principal that made it
    builtin = [{**row, **creators.empty()} for row in builtin]
    rows = [
        {**evaluators_local.catalog_entry(row), **_ground_truth_fields(row, db)}
        for row in evaluators_local.list_rows(db, ws.id)
        if visibility.sees(viewer, db, row.created_by)  # workspace isolation
    ]
    return {"evaluators": builtin + rows, "builtin_count": len(builtin)}


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
    # scoring on the caller-supplied model instead of authoring one.
    base_evaluator_id: str | None = Field(
        default=None, pattern=r"^(Builtin|ThirdParty)\.[A-Za-z0-9_.]+$"
    )
    # Code-based definition: a Lambda in the workspace Region implements the
    # scoring; the evaluation worker invokes it with its execution role
    # (lambda:InvokeFunction on the function — not managed by the console).
    lambda_arn: str | None = Field(default=None, pattern=_LAMBDA_ARN_PATTERN)
    lambda_timeout_s: int | None = Field(default=None, ge=1, le=300)
    model_id: str = evaluators_local.JUDGE_DEFAULT_MODEL_ID
    level: str = Field(default="TRACE", pattern="^(TOOL_CALL|TRACE|SESSION)$")
    description: str = Field(default="", max_length=200)  # AWS EvaluatorDescription max
    rating_scale: list[RatingScaleItem] | None = Field(default=None, min_length=2)


def _judge_model(req: "JudgeCreate | JudgeUpdate") -> str:
    """The judge model a request means (N17-obs252): the one it names, else the `judge`
    model role AT THIS CALL (`schemas.agent.judge_default_model_id()`). The pydantic default
    stays the CN constant so the OpenAPI contract (`ALLOWED_DELTA` CA-1c) is unchanged —
    `model_fields_set` tells an omitted member from an explicit one."""
    from app.schemas.agent import judge_default_model_id

    return req.model_id if "model_id" in req.model_fields_set else judge_default_model_id()


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


def _require_code_evaluators_enabled() -> None:
    """N12-obs41: the Lambda "Code" definition is an operator opt-in
    (``Settings.eval_code_evaluators_enabled``, default off — the worker role only gains
    ``lambda:InvokeFunction`` with it). Refused before any row is written; the console
    hides the option from ``GET /api/capabilities``."""
    if not get_settings().eval_code_evaluators_enabled:
        raise AppError(
            "evaluation.code_evaluators_disabled",
            "code-based (Lambda) evaluators are disabled in this workspace — set "
            "eval_code_evaluators_enabled: true in config/launchpad.yaml (or "
            "LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED=1), re-run `make bootstrap` so the "
            "evaluation worker may invoke the function, and restart; LLM-as-a-judge and "
            "derived evaluators are unaffected",
            {"setting": "eval_code_evaluators_enabled"},
            status_code=403,
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


def _require_placeholder(instructions: str, level: str | None = None) -> None:
    if not _PLACEHOLDER_RE.search(instructions):
        raise AppError(
            "evaluator.missing_placeholder",
            "instructions need at least one {placeholder} for the evaluated "
            "content (e.g. {context}, {assistant_turn})",
            status_code=422,
        )
    if level is None:
        return
    # N17-obs338: "Each evaluator level supports only a fixed set of placeholder values"
    # (Global create-evaluator) — a TRACE judge with {assertions} would render a value its
    # level has no unit for
    from app.services.strands_eval.judge_placeholders import LEVEL_PLACEHOLDERS

    allowed = set(LEVEL_PLACEHOLDERS.get(level, ()))
    if "expected_tool_trajectory" in allowed:
        allowed.add("expected_trajectory")  # the renderer's alias
    used = {m.group(0)[1:-1] for m in _PLACEHOLDER_RE.finditer(instructions)}
    outside = sorted(used - allowed)
    if outside:
        raise AppError(
            "evaluator.placeholder_not_allowed",
            f"{', '.join('{' + p + '}' for p in outside)} cannot be used at the {level} level; "
            f"allowed: {', '.join('{' + p + '}' for p in sorted(allowed))}",
            {"level": level, "placeholders": outside, "allowed": sorted(allowed)},
            status_code=422,
        )


def _rating_scale_payload(scale: list[RatingScaleItem] | None) -> list[dict[str, Any]]:
    if not scale:
        return DEFAULT_RATING_SCALE
    return [item.model_dump() for item in scale]


def _ground_truth_fields(row: EvalEvaluator, db: Session | None = None) -> dict[str, Any]:
    """N17-obs437 (additive): what ground truth a custom evaluator reads — the pickers of the
    no-ground-truth surfaces (online, canary, SCORE NOW) disable it, as the trajectory
    matchers; ``db`` also reads a managed code evaluator's reference rules."""
    from app.evaluation import coverage

    fields = coverage.ground_truth_needs(evaluators_local.definition_of(row), db, row.workspace_id)
    return {"requires_ground_truth": bool(fields), "ground_truth_fields": fields}


def _evaluator_out(row: EvalEvaluator, db: Session | None = None) -> dict[str, Any]:
    return {**evaluators_local.evaluator_out(row), **_ground_truth_fields(row, db)}


def _visible_evaluator(
    db: Session, ws: WorkspaceScope, viewer: Viewer, evaluator_id: str
) -> EvalEvaluator:
    """The workspace's custom evaluator the caller may see; a row workspace isolation
    hides answers the same 404 ``evaluator.not_found`` a missing id gets."""
    row = evaluators_local.get(db, ws.id, evaluator_id)
    visibility.require_seen(
        viewer, db, row.created_by,
        NotFoundError("evaluator.not_found", f"evaluator {evaluator_id} not found"),
    )
    return row


def _require_visible_evaluators(
    db: Session, ws: WorkspaceScope, viewer: Viewer, evaluator_ids: list[str]
) -> None:
    """A run may not apply a custom evaluator its creator cannot see (workspace
    isolation): the hidden id is refused exactly like an unknown one — 422
    ``run.evaluator_not_found`` — before any replay or judge call."""
    if not viewer.filters:
        return
    for evaluator_id in dict.fromkeys(evaluator_ids):
        if evaluators_local.is_managed_id(evaluator_id):
            continue
        row = evaluators_local.find(db, ws.id, evaluator_id)
        if row is not None and not visibility.sees(viewer, db, row.created_by):
            raise AppError(
                "run.evaluator_not_found",
                f"evaluator {evaluator_id} does not exist in this workspace — "
                "drop it or pick one from the catalog",
                {"evaluator": evaluator_id}, status_code=422,
            )


@router.post("/evaluators", status_code=201)
def create_judge(
    req: JudgeCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _require_one_definition(req)
    creator = Creator.of_request(request)
    principal = principal_of_identity(require_identity(request))  # N17-obs227
    if req.lambda_arn:
        _require_code_evaluators_enabled()
        _require_lambda_region(req.lambda_arn, ws.context.region)
        row = evaluators_local.create_code(
            db, ws.id,
            name=req.name,
            lambda_arn=req.lambda_arn,
            lambda_timeout_s=_lambda_timeout(req),
            level=req.level,
            description=req.description,
            creator=creator,
        )
    elif req.base_evaluator_id:
        row = evaluators_local.create_derived(
            db, ws.id,
            name=req.name,
            description=req.description,
            base_evaluator_id=req.base_evaluator_id,
            model_id=_judge_model(req),
            resources=ws.context.resources,
            creator=creator,
            principal=principal,
        )
    else:
        _require_placeholder(req.instructions, req.level)
        row = evaluators_local.create_judge(
            db, ws.id,
            name=req.name,
            instructions=req.instructions,
            rating_scale=_rating_scale_payload(req.rating_scale),
            model_id=_judge_model(req),
            level=req.level,
            description=req.description,
            resources=ws.context.resources,
            creator=creator,
            principal=principal,
        )
    return {
        "evaluator_id": row.id,
        "arn": ws.context.arn("bedrock-agentcore", f"evaluator/{row.id}"),
        # the CN judge runs on the catalog model it was created with — there is no
        # service-side model probe, so no fallback can ever be reported here
        "model_fallback": None,
        **creators.fields(row),
    }


@router.get("/evaluators/{evaluator_id}")
def get_evaluator(
    evaluator_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    if evaluators_local.is_builtin(evaluator_id):
        return evaluators_local.builtin_out(evaluator_id)
    return _evaluator_out(_visible_evaluator(db, ws, viewer, evaluator_id), db)


class JudgeUpdate(BaseModel):
    instructions: str | None = Field(default=None, min_length=10, max_length=4000)
    base_evaluator_id: str | None = Field(
        default=None, pattern=r"^(Builtin|ThirdParty)\.[A-Za-z0-9_.]+$"
    )
    lambda_arn: str | None = Field(default=None, pattern=_LAMBDA_ARN_PATTERN)
    lambda_timeout_s: int | None = Field(default=None, ge=1, le=300)
    model_id: str = evaluators_local.JUDGE_DEFAULT_MODEL_ID
    level: str = Field(default="TRACE", pattern="^(TOOL_CALL|TRACE|SESSION)$")
    description: str = Field(default="", max_length=200)  # AWS EvaluatorDescription max
    rating_scale: list[RatingScaleItem] | None = Field(default=None, min_length=2)


def _reject_managed(evaluator_id: str) -> None:
    if evaluators_local.is_managed_id(evaluator_id):
        raise AppError(
            "evaluator.builtin_immutable",
            "built-in and third-party managed evaluators cannot be modified",
            status_code=400,
        )


def _require_evaluator_creator(request: Request, row: EvalEvaluator, action: str) -> None:
    """Creator-or-admin on a custom evaluator mutation (N13-obs15, E10): runs after
    `_visible_evaluator` (a hidden row stays a 404), before any definition / in-use check."""
    ownership.require_creator_or_admin(
        row, require_identity(request), "evaluator.forbidden",
        message="only the evaluator's creator or an administrator may " + action + " it",
        detail={"evaluator_id": row.id, "action": action},
    )


def _proposed_scoring(row: EvalEvaluator, req: "JudgeUpdate") -> str:
    """Audit 7.6: the definition hash the update would give ``row`` — the same fields the
    ``evaluators_local.update_*`` writers set, through ``definition_hash`` (P7.1), so a
    description-only edit hashes equal and a scoring edit does not."""
    from types import SimpleNamespace

    fields = {f: getattr(row, f, None) for f in evaluators_local.SCORING_FIELDS}
    if req.lambda_arn:
        fields.update(lambda_arn=req.lambda_arn, lambda_timeout_s=_lambda_timeout(req),
                      level=req.level)
    elif req.base_evaluator_id:
        fields.update(base_evaluator_id=req.base_evaluator_id, model_id=_judge_model(req),
                      level=evaluators_local.base_level(req.base_evaluator_id))
    else:
        fields.update(instructions=req.instructions,
                      rating_scale=_rating_scale_payload(req.rating_scale),
                      model_id=_judge_model(req), level=req.level)
    return evaluators_local.definition_hash(SimpleNamespace(**fields))  # type: ignore[arg-type]


@router.put("/evaluators/{evaluator_id}")
def update_evaluator(
    evaluator_id: str,
    req: JudgeUpdate,
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    _reject_managed(evaluator_id)
    _require_one_definition(req)
    if req.instructions is not None:
        _require_placeholder(req.instructions)
    row = _visible_evaluator(db, ws, viewer, evaluator_id)
    if req.instructions is not None:
        _require_placeholder(req.instructions, getattr(req, "level", None) or row.level)
    _require_evaluator_creator(request, row, "edit")
    principal = principal_of_identity(require_identity(request))  # N17-obs227
    # An update full-replaces the definition, so a payload of the wrong definition
    # type would silently convert the evaluator (a judge into a Lambda evaluator, a
    # Lambda evaluator into a judge, …) — reject instead.
    payload_kind = _definition_kind(req)
    if row.definition != payload_kind:
        raise AppError(
            "evaluator.definition_mismatch",
            "update payload must match the evaluator's definition type "
            "(instructions for LLM-as-a-judge, base_evaluator_id for derived, "
            "lambda_arn for code-based)",
            {"current": row.definition, "payload": payload_kind},
            status_code=400,
        )
    if _proposed_scoring(row, req) != evaluators_local.definition_hash(row):
        # audit 7.6: a scoring edit under an ENABLED online config or a running canary
        # would change what their scores mean mid-flight — 409 with the referrers (a
        # description-only edit passes), unless an administrator forces it
        forced = references.require_unreferenced(
            references.evaluator_lock_referrers(db, ws.id, evaluator_id),
            code="evaluator.in_use", resource_id=evaluator_id, force=force,
            identity=require_identity(request), action="edit",
        )
        if forced:
            logger.warning("evaluator %s scoring definition force-edited by %s while in use",
                           evaluator_id, require_identity(request).username)
    elif force:
        references.require_unreferenced([], code="evaluator.in_use", resource_id=evaluator_id,
                                        force=True, identity=require_identity(request),
                                        action="edit")
    if req.lambda_arn:
        _require_code_evaluators_enabled()
        _require_lambda_region(req.lambda_arn, ws.context.region)
        evaluators_local.update_code(
            db, row,
            lambda_arn=req.lambda_arn,
            lambda_timeout_s=_lambda_timeout(req),
            level=req.level,
            description=req.description,
        )
    elif req.base_evaluator_id:
        evaluators_local.update_derived(
            db, row,
            description=req.description,
            base_evaluator_id=req.base_evaluator_id,
            model_id=_judge_model(req),
            resources=ws.context.resources,
            principal=principal,
        )
    else:
        evaluators_local.update_judge(
            db, row,
            instructions=req.instructions,
            rating_scale=_rating_scale_payload(req.rating_scale),
            model_id=_judge_model(req),
            level=req.level,
            description=req.description,
            resources=ws.context.resources,
            principal=principal,
        )
    return {**_evaluator_out(row, db), "model_fallback": None}


@router.delete("/evaluators/{evaluator_id}")
def delete_evaluator(
    evaluator_id: str,
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Creator-or-admin; 409 ``evaluator.in_use`` while an ACTIVE run, an online config,
    a running canary or an un-promoted experiment names it (finished runs keep their scores
    and never block), unless an administrator passes ``?force=true`` (N13-obs16, E10
    NC-08). The operation-managed 409 stays first: its Lambda / IAM footprint is removed
    only through the operation's cleanup, forced or not."""
    _reject_managed(evaluator_id)
    row = _visible_evaluator(db, ws, viewer, evaluator_id)  # hidden → 404 before any 409
    _require_evaluator_creator(request, row, "delete")
    from app.evaluation.managed import managed_evaluator

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
    forced = references.require_unreferenced(
        references.evaluator_referrers(db, ws.id, evaluator_id),
        code="evaluator.in_use", resource_id=evaluator_id, force=force,
        identity=require_identity(request),
    )
    if forced:
        logger.warning("evaluator %s force-deleted by %s while referenced",
                       evaluator_id, require_identity(request).username)
    evaluators_local.delete(db, row)
    return {"deleted": True, **({"forced": True} if forced else {})}


# ─── runs ────────────────────────────────────────────────────────────────────
LOG_GROUP_NAME_RE = r"^[.\-_/#A-Za-z0-9]{1,512}$"
# a service.name as spans carry it; no whitespace or quotes (it is echoed into
# CloudWatch filters by the discovery routes)
SERVICE_NAME_RE = r"^[A-Za-z0-9._:/@#\-]{1,256}$"


class LogSource(BaseModel):
    """An agent's telemetry already in CloudWatch Logs, with no platform agent
    behind it — the evaluation job reads these CloudWatch Logs groups as they are."""

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
    # P3.2 (N17-obs350): the judge of the run's built-in evaluators (a custom judge keeps
    # its own model); omitted = the `judge` model role when the run is submitted
    judge_model_id: str | None = Field(default=None, min_length=1, max_length=120)
    # D3-d / R3-c: which of the dataset's scenarios the run replays — every one, the tuning
    # split, or the held-out split (an acceptance run); dataset scopes only
    split: Literal["all", "tuning", "held_out"] = "all"
    evaluators: list[str] = Field(default_factory=lambda: list(evaluators_local.BUILTIN_EVALUATORS))
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


# The referenced rows a run payload names (N15-obs8); every key is always present.
_RUN_REFERENCES = ("agent", "dataset")


def session_scenarios(run: EvalRun) -> list[dict[str, Any]]:
    """``[{session_id, scenario_id, scenario_label, split, turns_defined, turns_replayed}]``
    from ``EvalRun.scenario_map`` (``split``: D3-d, the scenario's tuning / held_out split;
    the turn counts: D3-c; each null on older rows)."""
    mapping = dict(run.scenario_map or {})
    return [{"session_id": sid, "scenario_id": (mapping.get(sid) or {}).get("scenario_id"),
             "scenario_label": (mapping.get(sid) or {}).get("input"),
             "split": (mapping.get(sid) or {}).get("split"),
             # D3-c: the scenario's defined turns and what the replay sent (null: unknown)
             "turns_defined": (mapping.get(sid) or {}).get("turns_defined"),
             "turns_replayed": (mapping.get(sid) or {}).get("turns_replayed")}
            for sid in (run.session_ids or []) if sid in mapping]


def _run_out(
    run: EvalRun, *, snapshot: dict[str, Any] | None, hidden: dict[str, bool] | None = None
) -> dict[str, Any]:
    """The run payload. ``snapshot`` is the agent version it evaluated
    (``scorecard.resolve_snapshots``: ``{seq, source: stamped|inferred|observed}``, or
    null without an agent or a deployed snapshot) — resolved by the caller, a page at
    a time. ``hidden`` (N15-obs8) names the referenced rows the viewer may not see: the
    id stays, the NAME is nulled and ``hidden.<ref>`` is true, so a member of an
    isolated workspace never learns a hidden dataset's / agent's name through an
    administrator's run while the console still renders its placeholder. Routers reach
    this through :func:`_runs_out` — the one place the viewer (and the version) is
    applied; every run serializer, compare and rerun included, goes through it."""
    masked = {ref: bool((hidden or {}).get(ref)) for ref in _RUN_REFERENCES}
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "agent_name": None if masked["agent"] else run.agent_name,
        # {service_name, log_group_names} when the run has no platform agent
        "log_source": run.log_source,
        "name": run.name,
        "description": run.description,
        "dataset_id": run.dataset_id,
        "dataset_name": None if masked["dataset"] else run.dataset_name,
        # N15-obs8 (additive): which referenced rows were masked for this viewer
        "hidden": masked,
        # pinned published cloud-dataset version; null = draft / not a cloud run
        "dataset_version": run.dataset_version,
        "mode": run.mode,
        "evaluators": run.evaluators,
        "status": run.status,
        "queue_position": run_queue.position(run.id),
        "session_ids": run.session_ids,
        # N17-obs391 (additive): the dataset scenario each session replayed — {session_id,
        # scenario_id, scenario_label (the scenario's input preview)} in session order; []
        # for a run that replayed no dataset
        "session_scenarios": session_scenarios(run),
        "batch_eval_id": run.batch_eval_id,
        "scores": run.scores,
        "insights": run.insights,
        # dataset scenarios that ended on the agent's budget and are scored as they are
        "budget_stops": run.budget_stops or [],
        # simulated persona scenarios skipped after their replay attempts (upstream #251)
        "scenario_failures": run.scenario_failures or [],
        # N12-bug4 (additive): evaluator records / insight sessions that errored on the
        # worker — {count, total, first_type, first_message}; null when none did
        "errors": run.record_errors,
        # N12-obs47 (additive): the sessions a partially failed evaluators run left unscored
        # and why — {total, failed, kind, sessions[{session_id, index, kind, error_type,
        # message, arm}]} (arm: N12-obs62, the pre-assigned C | T1, null on a plain run);
        # null on a clean run or a row finished before the summary existed
        "session_failures": run.session_failures,
        # eval accuracy item 1 (additive): the extraction reconciliation summary —
        # {status: ok|mismatch|unverifiable, sessions, ok, mismatch, unverifiable,
        # turns_checked, mismatched_sessions[], anomalies{turns, empty, empty_rate,
        # tool_json_like, repeated_previous}}; null when the worker reported none
        "extraction_check": (run.extraction_check or {}).get("summary"),
        # R3-f (additive): expected vs judged units per evaluator, failures by class
        # (timeout | throttling | context_overflow | truncation | failed_turn | infrastructure
        # | evaluator_error), missing units, the coverage and `below_threshold`; null on
        # insights runs and older rows
        "unit_coverage": run.unit_coverage,
        # N17-obs450 (additive): `error` read back as a stable reason (`run_failure.classify`)
        # — error_code (stopped_by_operator | job_lost | still_running | all_records_failed |
        # some_records_errored | app_error | error) + error_params; null for no error or
        # text in no known shape (the console then shows `error` as it is)
        **run_failure.payload(run.error),
        # P8.3 / P8.4 (additive): the run's judge tokens {inputTokens, outputTokens,
        # totalTokens, budget, exhausted}; null when no worker reported any
        "token_usage": run.token_usage,
        # N17-obs418 (additive): the newest worker heartbeat (`status.json` updated_at, ISO
        # UTC) the poller read while the run's jobs ran; null before the first poll / older rows
        "last_heartbeat_at": run.last_heartbeat_at,
        # P7.1 / P7.6 (additive): the evaluator versions the run was submitted with
        # [{id, name, kind, version, definition_hash}] ([] on older rows) and the evaluation
        # worker release it was dispatched to (null on older rows)
        "evaluator_versions": [
            {k: entry.get(k) for k in ("id", "name", "kind", "version", "definition_hash")}
            for entry in (run.evaluator_snapshot or [])],
        "worker_release": run.worker_release,
        # P3.1 / P3.2 (additive): the built-in judge and its effective inference config —
        # {model_id, temperature (0.0 | "provider default"), temperature_applied,
        # max_tokens}; null on older rows
        "judge_model_id": run.judge_model_id,
        "judge_inference": run.judge_inference,
        # N17-obs407 (additive): expected-trajectory names the run start rewrote to the
        # agent's own — [{scenario_id, from, to}]; [] when none
        "tool_name_rewrites": run.tool_name_rewrites or [],
        # D3-d / R3-c (additive): the dataset split the run replayed — all | tuning |
        # held_out; null for a passive scope and older rows
        "dataset_split": run.dataset_split,
        # D3-c (additive): the local dataset revision the run replayed (a cloud run pins
        # `dataset_version`); null for cloud / passive scopes and older rows
        "dataset_revision": run.dataset_revision,
        # N17-obs407 (additive): non-blocking warnings — an expected tool that never appears
        # in the run's spans when the agent's tool names could not be checked; [] when none
        "warnings": run.warnings or [],
        "error": run.error,
        # additive: an operator stop is pending on this run (in-memory flag;
        # the row turns `stopped` once the poller/worker observes it)
        "stop_requested": service.stop_requested(run.id),
        "snapshot": snapshot,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        **creators.fields(run),
    }


def _runs_out(
    db: Session, ws: WorkspaceScope, viewer: Viewer, runs: list[EvalRun], *,
    snapshots: dict[str, dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """``_run_out`` for the request's viewer (N15-obs8), each run's version resolved
    for the page (``snapshots``: pass ``scorecard.resolve_snapshots`` when the caller
    already has it, else it is resolved here). A visible run may still name a dataset
    (local row or cloud store) or an agent workspace isolation hides from the viewer —
    those names are masked. One query per kind; none for an unfiltered viewer."""
    if snapshots is None:
        snapshots = scorecard.resolve_snapshots(db, runs) if runs else {}
    if not viewer.filters or not runs:
        return [_run_out(run, snapshot=snapshots.get(run.id)) for run in runs]
    hidden_datasets = _hidden_dataset_ids(db, ws, viewer, runs)
    hidden_agents = agent_access.hidden_agent_ids(db, viewer, ws.id)
    return [
        _run_out(run, snapshot=snapshots.get(run.id), hidden={
            "dataset": run.dataset_id in hidden_datasets,
            "agent": run.agent_id in hidden_agents,
        })
        for run in runs
    ]


def _run_out_for(db: Session, ws: WorkspaceScope, viewer: Viewer, run: EvalRun) -> dict[str, Any]:
    return _runs_out(db, ws, viewer, [run])[0]


def _hidden_dataset_ids(
    db: Session, ws: WorkspaceScope, viewer: Viewer, runs: list[EvalRun]
) -> set[str]:
    """The dataset ids among ``runs`` that workspace isolation hides from the viewer —
    local rows via ``visibility.hidden_ledger_ids``, cloud stores (``cloud:`` runs) via
    ``_hidden_cloud_ids``. One query per kind; empty for an unfiltered viewer. The one
    source for every payload that names a run's dataset: the run payload (``_runs_out``),
    the scorecard rows and the compare sides' benchmarks (N15-obs8 / N15-obs15)."""
    if not viewer.filters:
        return set()
    dataset_ids = {run.dataset_id for run in runs if run.dataset_id}
    if not dataset_ids:
        return set()
    hidden = visibility.hidden_ledger_ids(
        viewer, db, EvalDataset.id, EvalDataset.created_by,
        EvalDataset.workspace_id == ws.id, EvalDataset.id.in_(dataset_ids),
    )
    if any((run.dataset_name or "").startswith("cloud:") for run in runs):
        hidden |= _hidden_cloud_ids(db, ws, viewer)
    return hidden


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
    # the signature is contract-frozen (openapi_baseline_eval.json), so the storage bound
    # is a clamp here rather than a `le=` on the parameter (N12-obs58)
    offset = min(offset, SQLITE_MAX_INT)
    query = db.query(EvalRun).filter(
        EvalRun.workspace_id == ws.id,
        visibility.clause(viewer, EvalRun.created_by),  # isolation: before count/offset
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
    return {
        "runs": _runs_out(db, ws, viewer, rows),
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
    page = list(reversed(rows[:cap]))
    return scorecard.scorecard_out(
        db, ws.id, page, agent_id=agent_id, viewer=viewer, truncated=len(rows) > cap,
        # N15-obs15: a hidden dataset's name appears nowhere in the viewer's payload
        hidden_datasets=_hidden_dataset_ids(db, ws, viewer, page),
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
        if (scores := service.backfill_confidence(run)) is not None:  # EVAL-TRUST 3.2
            run.scores = scores
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
    # the same serializer as every other run payload: the viewer's masking applies here,
    # to each side's benchmark label too (N15-obs15)
    outs = _runs_out(db, ws, viewer, [run_a, run_b], snapshots=snaps)
    hidden_ds = _hidden_dataset_ids(db, ws, viewer, [run_a, run_b])
    return {
        "runs": {
            side: {**out, "benchmark": scorecard.benchmark_of(
                run, labels, hidden=run.dataset_id in hidden_ds)}
            for side, run, out in (("a", run_a, outs[0]), ("b", run_b, outs[1]))
        },
        "alignment": aligned["alignment"],
        "alignment_reason": aligned["reason"],
        "cases": cases,
        "results": results,
        "config": config,
        # P3.2 (additive): each side's built-in judge; ``judge_mismatch`` when both are
        # known and differ — the scores are then not a like-for-like comparison
        "judges": {"a": run_a.judge_model_id, "b": run_b.judge_model_id},
        "judge_mismatch": bool(run_a.judge_model_id and run_b.judge_model_id
                               and run_a.judge_model_id != run_b.judge_model_id),
        # EVAL-TRUST 3.2 (additive): per shared evaluator, whether B − A is a real difference —
        # {label: significant | not_significant | insufficient, p (Welch), ci_overlap}
        "significance": run_stats.compare_runs(run_a.scores, run_b.scores),
    }


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    # A run of another workspace — or one workspace isolation hides — reads as absent.
    run = _run_in(db, ws, run_id, viewer=viewer)
    if (scores := service.backfill_confidence(run)) is not None:  # EVAL-TRUST 3.2
        run.scores = scores
    return _run_out_for(db, ws, viewer, run)


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
    both apply the same scope, reference, visibility and evaluator checks (incl. the
    r13-h rulings: ``evaluators: []`` → 422, an empty insights window → 409).
    ``actor_fallback`` is the actor model a persona dataset gets when the request
    names none (the re-run's platform default); ``POST /runs`` passes none and
    requires the caller's. CN: no per-person budget gate (budgets are not ported)."""
    if bool(req.agent_id) == bool(req.log_source):
        raise AppError(
            "run.target_required",
            "exactly one target required: agent_id or log_source",
            status_code=422,
        )
    agent = None
    if req.agent_id:
        # a foreign, missing or isolation-hidden id answers 400 agent.not_active alike
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
    if req.split != SPLIT_ALL and not dataset_scope:
        raise AppError(
            "run.split_scope",
            "split only applies to a dataset_id / cloud_dataset_id scope",
            status_code=422,
        )
    if req.dataset_version and req.dataset_version.upper() == "DRAFT":
        raise AppError(
            "run.dataset_version_scope",
            "dataset_version is a published version number — omit it for the draft",
            status_code=422,
        )
    if req.insights:
        invalid = [i for i in req.insights if i not in evaluators_local.INSIGHT_TYPES]
        if invalid:
            raise AppError(
                "run.invalid_insight",
                f"unknown insight type(s): {', '.join(invalid)}",
                status_code=422,
            )

    items: list[dict[str, Any]] = []
    dataset_name = None
    dataset_revision = None
    time_range = None
    if req.dataset_id:
        dataset = _dataset_in(db, ws, req.dataset_id, viewer=viewer)
        items = dataset.items
        dataset_name = dataset.name
        dataset_revision = dataset.revision or 1  # D3-c
    elif req.cloud_dataset_id:
        _require_cloud_visible(db, ws, viewer, req.cloud_dataset_id)
        cloud_name, items = _cloud_dataset_items(
            req.cloud_dataset_id, ws, version=req.dataset_version
        )
        # "cloud:" prefix marks the scope in the runs list (like "window:Nh");
        # the pinned version travels on its own column, never in this string.
        dataset_name = f"cloud:{cloud_name}"
    elif req.session_source == "logs":
        # like "window:Nh": the scope rides in dataset_name, no schema change
        dataset_name = f"logs:{len(set(req.session_ids or []))}"

    if dataset_scope and req.split != SPLIT_ALL:
        # D3-d / R3-c: only the scenarios of the chosen split are replayed and scored
        total = len(items)
        items = select_split(items, req.split)
        if not items:
            raise AppError(
                "run.split_empty",
                f"this dataset has no {req.split} scenarios ({total} in all) — mark some "
                "scenarios with that split, or run the whole dataset",
                {"split": req.split, "items": total}, status_code=422,
            )

    # N17-obs407: the trajectories name the tools as THIS agent calls them (or 422)
    items, rewrites = _resolve_trajectories(items, agent, "run.unknown_tool")

    # the re-run's platform default plays the user of a persona dataset when the
    # request names no actor model (upstream #289); POST /runs requires the caller's
    persona = any("actor_profile" in item for item in items)
    actor_model_id = req.actor_model_id or (actor_fallback if persona else None)
    _check_actor_model(items, actor_model_id, ws.context.resources,
                       principal_of_identity(require_identity(request)))
    _check_judge_model(req.judge_model_id, ws.context.resources,
                       principal_of_identity(require_identity(request)))

    if req.mode == "evaluators" and not req.evaluators:
        # N13-obs18 (E11 NC-10): an explicit `evaluators: []` used to make a 201 run that
        # scored nothing (the default list applies only when the field is omitted).
        raise AppError(
            "run.no_evaluators",
            "an evaluators run must apply at least one evaluator — omit the field for the "
            "built-in set, or switch to mode \"insights\"",
            status_code=422,
        )
    applied_ids = req.evaluators if req.mode == "evaluators" else (req.insights or [])
    if len(set(applied_ids)) > evaluators_local.MAX_BATCH_EVALUATORS:
        raise AppError(
            "run.too_many_evaluators",
            f"a batch evaluation applies at most {evaluators_local.MAX_BATCH_EVALUATORS} "
            f"{'evaluators' if req.mode == 'evaluators' else 'insights'}; {len(set(applied_ids))} "
            "were selected — drop the ones that overlap or start a second run for the rest",
            {"max": evaluators_local.MAX_BATCH_EVALUATORS, "selected": len(set(applied_ids))},
            status_code=422,
        )
    if req.mode == "evaluators":
        _require_visible_evaluators(db, ws, viewer, req.evaluators)
        _assert_target_references(
            db, ws, req.evaluators, items, dataset_scope,
            agent_spec=agent.spec if agent else None,
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
        applied = req.insights or list(evaluators_local.INSIGHT_TYPES)

    run = service.submit_run(
        agent=agent,
        workspace=ws.context,
        creator=Creator.of_request(request),
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
        judge_model_id=req.judge_model_id,
        tool_name_rewrites=rewrites,
        dataset_split=req.split if dataset_scope else None,
        dataset_revision=dataset_revision,
    )
    return _run_out_for(db, ws, viewer, run)


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
    dataset stops at the worker's next checkpoint. Terminal runs → 409
    `run.not_active`."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    return _run_out_for(db, ws, viewer, service.request_stop(run.id, workspace=ws.context))


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
    return _run_out_for(db, ws, viewer, service.recheck_run(run.id, workspace=ws.context))


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
        # P3.2: the re-run keeps the source's judge, so its scores compare (an older row
        # without one gets the `judge` model role, as before)
        "judge_model_id": source.judge_model_id,
    }
    if source.agent_id:
        fields["agent_id"] = source.agent_id
    else:
        fields["log_source"] = source.log_source
    if scope in (scorecard.SCOPE_DATASET, scorecard.SCOPE_CLOUD):
        # D3-d / R3-c: a re-run replays the same split (an older row: the whole dataset)
        fields["split"] = source.dataset_split if source.dataset_split in RUN_SPLITS \
            else SPLIT_ALL
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
    run, exactly as ``POST /runs`` would start it (same validation and visibility; the
    caller becomes the new run's creator, never the source's). A simulated-persona
    dataset plays the user with the platform's current actor model."""
    source = _run_in(db, ws, run_id, viewer=viewer)
    req = _rerun_request(source, body.name if body else None)
    # the platform's actor model (`eval_actor_model_id`); only a persona dataset uses it
    return _start_run(req, request, db, ws, viewer,
                      actor_fallback=get_settings().eval_actor_model_id)


# ─── recommendations seeded from a run ──────────────────────────────────────
@router.get("/recommendation-providers")
def recommendation_providers(
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The recommendation generators the console can offer (no AWS call) — the picker on
    a run's recommendations reads `enabled` / `models[].available` from here. Lived at
    `GET /api/experiments/providers` until the configuration experiment retired (N13-obs2)."""
    return {"providers": rec_providers.describe_providers()}


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
    # who generates the recommendation: one of the self-built providers reflecting on
    # this run (CA-1b; gepa_lite is the default, harness_optimizer arrives with W-E2-5)
    provider: Literal["gepa_lite", "harness_optimizer"] | None = None
    model_id: str | None = Field(default=None, max_length=200)


class RecommendationAccept(BaseModel):
    # the reviewed prompt to publish — the recommendation as generated when omitted;
    # an operator edit (e.g. removing a loosened boundary) is recorded as such
    system_prompt: str | None = Field(default=None, max_length=recommendations.SYSTEM_PROMPT_MAX)


def _run_in(
    db: Session, ws: WorkspaceScope, run_id: str, *, viewer: Viewer = Viewer.ALL
) -> EvalRun:
    """The run, or 404 — a foreign run and one workspace isolation hides from ``viewer``
    read exactly like a missing one."""
    row = db.get(EvalRun, run_id)
    if row is None or row.workspace_id != ws.id:
        raise NotFoundError("run.not_found", "run not found")
    visibility.require_seen(
        viewer, db, row.created_by, NotFoundError("run.not_found", "run not found")
    )
    return row


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
    """Start one recommendation job per requested kind, its traces pinned to
    this run's batch evaluation (only completed runs with a batch → else 409). N17-obs227:
    the reflection `model_id` must be visible to the caller (422 `models.model_not_visible`)."""
    run = _run_in(db, ws, run_id, viewer=viewer)
    rows = recommendations.start(
        db, run, ws.context,
        kinds=list(dict.fromkeys(req.kinds)),
        input_source=req.input_source,
        system_prompt=req.system_prompt,
        evaluator=req.evaluator,
        tools={t.name: t.description for t in req.tools or []},
        provider=req.provider,
        model_id=req.model_id,
        principal=principal_of_identity(require_identity(request)),
    )
    # D3-d (additive): the held-out scenarios the tuning paths left out, said out loud
    notice = next((r.result.get("held_out_excluded") for r in rows
                   if (r.result or {}).get("held_out_excluded")), None)
    return {"recommendations": [recommendations.out(r) for r in rows],
            **({"held_out_excluded": notice} if notice else {})}


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
    from app.evaluation.generated_prompt import strip_generated_prompt
    from app.routers.agents import republish_agent
    from app.routers.auth import require_identity

    run = _run_in(db, ws, run_id, viewer=viewer)
    # a visible run does not make its agent the caller's to re-publish (isolation):
    # refused before the target checks, which would describe the hidden agent
    agent_access.require_agent_visible(
        db, viewer, db.get(Agent, run.agent_id) if run.agent_id else None,
        NotFoundError("agent.not_found", "the run's agent no longer exists"),
    )
    row, agent, prompt = recommendations.acceptance_target(db, run, rec_id)
    edited = False
    if req is not None and req.system_prompt is not None:
        reviewed = req.system_prompt.strip()
        if not reviewed:
            raise AppError("recommendation.accept_prompt_empty",
                           "the reviewed prompt is empty", status_code=422)
        edited = reviewed != prompt
        prompt = reviewed
    previous_version = agent.version
    current = AgentSpec(**(agent.spec or {}))
    # the recommendation revised the LIVE prompt, which already carries the
    # KB section the deployer appends — keep only the operator-owned part
    spec = current.model_copy(update={"system_prompt": strip_generated_prompt(current, prompt)})
    spec = AgentSpec(**spec.model_dump())
    # R3-A: every spec re-publish passes the owner-or-admin gate (403 agent.forbidden)
    accepting = require_identity(request)
    result = republish_agent(
        db, agent, spec, ws, identity=accepting, actor=Creator.of(accepting),
        note="recommendation accepted",
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
        system_prompt=prompt,
    )
    return {**result, "recommendation": recommendations.out(row)}


class RecommendationRevise(BaseModel):
    # N17-obs464: the hand-edited prompt to publish on top of the live version, and the
    # version the editor was opened on (a concurrent publish since then → 409)
    system_prompt: str = Field(max_length=recommendations.SYSTEM_PROMPT_MAX)
    expected_version: str = Field(min_length=1, max_length=32)


@router.post("/runs/{run_id}/recommendations/{rec_id}/revise", status_code=202)
def revise_run_recommendation(
    run_id: str,
    rec_id: str,
    req: RecommendationRevise,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """N17-obs464: publish a manual edit of an ACCEPTED system-prompt recommendation as a NEW
    Harness version on top of the live one — the accept path (same eligibility, permission,
    owner-or-admin, preset / converted refusals), pinned to ``expected_version``: a
    compare-and-set on the agent row (version AND still active) in the publish transaction,
    so a concurrent publish answers 409 and nothing is clobbered. Recorded on the
    recommendation (``accepted.edits[]``) and as the version's snapshot note
    ``recommendation edited``."""
    from sqlalchemy import update as sa_update

    from app.evaluation.generated_prompt import strip_generated_prompt
    from app.routers.agents import republish_agent
    from app.routers.auth import require_identity

    run = _run_in(db, ws, run_id, viewer=viewer)
    agent_access.require_agent_visible(
        db, viewer, db.get(Agent, run.agent_id) if run.agent_id else None,
        NotFoundError("agent.not_found", "the run's agent no longer exists"),
    )
    reviewed = req.system_prompt.strip()
    if not reviewed:
        raise AppError("recommendation.accept_prompt_empty",
                       "the reviewed prompt is empty", status_code=422)
    row, agent, _prompt = recommendations.acceptance_target(db, run, rec_id, revising=True)
    if agent.version != req.expected_version:
        raise AppError(
            "recommendation.version_moved",
            f"the agent moved to version {agent.version} since the editor opened on "
            f"{req.expected_version} — review the live prompt and edit again",
            {"expected_version": req.expected_version, "current_version": agent.version},
            status_code=409,
        )
    # the pin as a compare-and-set in THIS transaction: SQLite serializes writers, so a
    # concurrent publish waits here and then finds the agent deploying (or moved)
    claimed = db.execute(
        sa_update(Agent)
        .where(Agent.id == agent.id, Agent.version == req.expected_version,
               Agent.status == "active")
        .values(updated_at=datetime.now(UTC))
    ).rowcount
    if claimed != 1:
        db.rollback()
        fresh = db.get(Agent, agent.id)
        db.refresh(fresh)
        raise AppError(
            "recommendation.version_moved",
            "another publish of this agent started meanwhile — review the live prompt and "
            "edit again",
            {"expected_version": req.expected_version,
             "current_version": fresh.version if fresh else None},
            status_code=409,
        )
    current = AgentSpec(**(agent.spec or {}))
    spec = current.model_copy(update={"system_prompt": strip_generated_prompt(current, reviewed)})
    spec = AgentSpec(**spec.model_dump())
    editing = require_identity(request)
    result = republish_agent(
        db, agent, spec, ws, identity=editing, actor=Creator.of(editing),
        note="recommendation edited",
    )
    recommendations.record_edit(
        db, row, by=getattr(editing, "username", None) or "unknown",
        version_before=req.expected_version, job_id=result["job_id"],
        deployment_id=result["deployment_id"], system_prompt=spec.system_prompt or reviewed,
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
                    EvalRun.id.in_(named), EvalRun.workspace_id == ws.id,
                    visibility.clause(viewer, EvalRun.created_by),  # isolation
                )
            )
        )
    return {
        **state,
        "running": [run_id for run_id in state["running"] if run_id in mine],
        "queued": [run_id for run_id in state["queued"] if run_id in mine],
    }
