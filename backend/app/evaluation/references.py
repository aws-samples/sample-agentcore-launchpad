"""Referential delete guards for datasets and evaluators (r13-g, N13-obs16).

A dataset or a custom evaluator is named BY ID from other ledger rows — runs, pipelines,
online configs, experiments and canaries. Deleting the row those ids point at used to
succeed silently (E09 NC-11, E10 NC-08): the next pipeline run failed with
`dataset.not_found`, the next run creation with `run.evaluator_not_found`, and a running
online config kept scoring with an evaluator that no longer existed. This module answers
**409 `<kind>.in_use`** with the referencing rows instead, unless an ADMINISTRATOR passes
`?force=true`.

What blocks, and why (the contract is `docs/cn/interfaces/evaluation-engine.md` §22):

* **Runs** — only while ACTIVE (`service.ACTIVE_STATUSES`: queued / invoking / waiting /
  evaluating). A finished run keeps a *snapshot*: `dataset_name` (display), its own copy
  of the items on the worker and the evaluator ids inside its `scores`; it never reads
  the dataset or evaluator row again, so completed / failed / stopped runs do NOT block a
  deletion. The worker of an active run may still resolve the definition (an evaluator
  it has not reached yet), hence the block.
* **Pipelines** — any status, when `config.output.dataset_id` is the dataset: a saved
  task would fail on its next run.
* **Online configs** — any status, when `evaluators` lists the evaluator: the scheduler
  resolves the definition on every sweep. Experiment- and canary-owned configs count too.
* **Canaries** — while `running`, when the canary's evaluator choice
  (`artifacts.online_evaluators` / `artifacts.setup.online_evaluators`) lists the
  evaluator: the next verdict reads it. (Configuration experiments were retired by r13-b;
  their legacy rows are read-only and never block.)
* **Cloud datasets** (`DELETE /api/eval/datasets/cloud/{id}`) — active runs on the cloud
  id, PLUS any run that pinned one of its published versions (`dataset_version`): the
  version rule already treats a replayed version as history (`dataset.version_pinned`),
  and deleting the whole dataset must not sidestep it.

The lists are bounded (`LIMIT`): the detail names enough rows to act on, `truncated`
says there are more. Ids are the ledger's own and were already visible to the caller
through the kind's list routes; the referrer names are the rows' display names.

`force=true` deletes anyway; the referencing rows keep their dangling id (a run its
snapshot name, a pipeline fails its next run with `dataset.not_found`, an online config
skips the evaluator) — an administrator's explicit decision, logged by the route. A member
passing `force` answers 403 `auth.forbidden` (the standard administrator-required code).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.evaluation.models import EvalPipeline, EvalRun, OnlineEvalConfig
from app.evaluation.service import ACTIVE_STATUSES
from app.optimization.models import RuntimeCanary

if TYPE_CHECKING:
    from app.routers.auth import Identity

LIMIT = 20  # referrers named in a 409 detail

CANARY_ACTIVE_STATUSES = ("running",)


def _ref(kind: str, row_id: str, name: str | None, status: str | None) -> dict[str, Any]:
    return {"kind": kind, "id": row_id, "name": name, "status": status}


def _bounded(refs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    return refs[:LIMIT], len(refs) > LIMIT


# ─── datasets ────────────────────────────────────────────────────────────────


def dataset_referrers(db: Session, workspace_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Rows that still need the local dataset: ACTIVE runs replaying it and pipelines
    (any status) whose output targets it."""
    refs: list[dict[str, Any]] = []
    runs = db.scalars(
        select(EvalRun).where(
            EvalRun.workspace_id == workspace_id,
            EvalRun.dataset_id == dataset_id,
            EvalRun.status.in_(ACTIVE_STATUSES),
        ).order_by(EvalRun.created_at.desc())
    )
    refs.extend(_ref("run", r.id, r.name or r.agent_name, r.status) for r in runs)
    pipelines = db.scalars(
        select(EvalPipeline).where(EvalPipeline.workspace_id == workspace_id)
        .order_by(EvalPipeline.created_at.desc())
    )
    for p in pipelines:
        output = (p.config or {}).get("output") or {}
        if output.get("dataset_id") == dataset_id:
            refs.append(_ref("pipeline", p.id, p.name, p.status))
    return refs


def cloud_dataset_referrers(
    db: Session, workspace_id: str, cloud_id: str
) -> list[dict[str, Any]]:
    """Rows that still need the cloud dataset: ACTIVE runs on it and any run that pinned
    one of its published versions (history — the same rule as `dataset.version_pinned`)."""
    rows = db.scalars(
        select(EvalRun).where(
            EvalRun.workspace_id == workspace_id, EvalRun.dataset_id == cloud_id,
        ).order_by(EvalRun.created_at.desc())
    )
    refs: list[dict[str, Any]] = []
    for r in rows:
        if r.status in ACTIVE_STATUSES or r.dataset_version:
            ref = _ref("run", r.id, r.name or r.agent_name, r.status)
            if r.dataset_version:
                ref["dataset_version"] = r.dataset_version
            refs.append(ref)
    return refs


# ─── evaluators ──────────────────────────────────────────────────────────────


def _lists(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list | tuple) else []


def evaluator_referrers(
    db: Session, workspace_id: str, evaluator_id: str
) -> list[dict[str, Any]]:
    """Rows that still resolve the custom evaluator: ACTIVE runs, online configs (any
    status) and running canaries that chose it."""
    refs: list[dict[str, Any]] = []
    runs = db.scalars(
        select(EvalRun).where(
            EvalRun.workspace_id == workspace_id, EvalRun.status.in_(ACTIVE_STATUSES),
        ).order_by(EvalRun.created_at.desc())
    )
    refs.extend(
        _ref("run", r.id, r.name or r.agent_name, r.status)
        for r in runs if evaluator_id in _lists(r.evaluators)
    )
    configs = db.scalars(
        select(OnlineEvalConfig).where(OnlineEvalConfig.workspace_id == workspace_id)
        .order_by(OnlineEvalConfig.created_at.desc())
    )
    refs.extend(
        _ref("online_config", c.config_id, c.name, c.execution_status)
        for c in configs if evaluator_id in _lists(c.evaluators)
    )
    canaries = db.scalars(
        select(RuntimeCanary).where(
            RuntimeCanary.workspace_id == workspace_id,
            RuntimeCanary.status.in_(CANARY_ACTIVE_STATUSES),
        ).order_by(RuntimeCanary.created_at.desc())
    )
    for c in canaries:
        artifacts = c.artifacts or {}
        chosen = _lists(artifacts.get("online_evaluators")) or _lists(
            (artifacts.get("setup") or {}).get("online_evaluators")
        )
        if evaluator_id in chosen:
            refs.append(_ref("canary", c.id, c.name, c.status))
    return refs


def evaluator_lock_referrers(
    db: Session, workspace_id: str, evaluator_id: str
) -> list[dict[str, Any]]:
    """Audit 7.6: the rows that are SCORING with the evaluator right now — ENABLED online
    configs and running canaries that chose it. A scoring-field edit under them would change
    what their verdict / series mean mid-flight. Active runs are not listed: a run resolves
    its definitions when it is planned (and records the version, P7.1)."""
    return [r for r in evaluator_referrers(db, workspace_id, evaluator_id)
            if r["kind"] == "canary"
            or (r["kind"] == "online_config" and r.get("status") == "ENABLED")]


# ─── the gate ────────────────────────────────────────────────────────────────


def require_unreferenced(
    refs: list[dict[str, Any]],
    *,
    code: str,
    resource_id: str,
    force: bool,
    identity: Identity | None,
    action: str = "delete",
) -> bool:
    """Refuse the deletion while ``refs`` is non-empty — 409 ``code`` with the bounded
    referrer list — unless an administrator passed ``force``. A member's ``force`` is
    403 ``auth.forbidden`` whether or not anything references the row (the flag is an
    administrator's override, not a member capability). Returns whether the deletion
    is a forced one (the route logs it)."""
    if force and not (identity is not None and identity.is_admin):
        raise AppError(
            "auth.forbidden",
            f"force-{action.rstrip('e')}ing a referenced resource requires an administrator",
            {"id": resource_id},
            status_code=403,
        )
    if not refs:
        return False
    if force:
        return True
    shown, truncated = _bounded(refs)
    kinds = sorted({r["kind"] for r in refs})
    raise AppError(
        code,
        f"still referenced by {len(refs)} row(s) ({', '.join(kinds)}) — remove or finish "
        f"those first, or an administrator may {action} with ?force=true",
        {"id": resource_id, "referrers": shown, "total": len(refs), "truncated": truncated,
         "force_allowed": "admin"},
        status_code=409,
    )
