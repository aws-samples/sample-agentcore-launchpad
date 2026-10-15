"""Skill Lab job lifecycle: submit, queue, run, cancel, restart sweep.

Execution model (parent design §5): a bounded worker pool (EvalRunQueue reuse)
runs one vendored-CLI subprocess per job; the worker thread owns the terminal
status transition. Cancel kills the whole process group via an in-memory
{job_id: Popen} registry — single-process uvicorn is an established assumption
(same as the registry inspect staging). After a backend restart the old process
group is gone, so non-terminal rows are swept to `interrupted`; only training
jobs can be picked up again (`resume_job` — the trainer checkpoints per step).

Spend budget (services/job_budget.py): the job's creator pays. A job that waited
in the queue starts only while they still have budget; it is debited a flagged
estimate as its work starts (eval: tasks × per-task, taskgen: a flat amount,
train: one per-step amount as each step starts). An eval job is trued up from
``results.json`` (exact target + judge tokens supersede the estimate). A train
job is stopped between steps once its creator is over budget — status
``interrupted``, resumable like any interrupted training. An eval job has no
safe boundary inside the vendored CLI (one pool rolls out every task, results
are written once), so it runs to completion once started.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.billing_policy import SOURCE_SKILL_LAB
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation.queue import EvalRunQueue
from app.services import budgets, creators, job_budget, visibility
from app.services.local_exec import kill_process_group
from app.services.visibility import Viewer
from app.services.workspace import context_for_workspace
from app.skill_lab import artifacts, runner, task_assets
from app.skill_lab import tasksets as taskset_svc
from app.skill_lab.models import SkillLabJob

logger = logging.getLogger("launchpad.skill_lab")

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")
# How often a running training job's step boundary (out/history.json) is read.
STEP_POLL_S = 2.0

job_queue = EvalRunQueue(max_concurrency=get_settings().skill_lab_max_concurrent_jobs)
_procs: dict[str, Any] = {}
_procs_lock = threading.Lock()


def job_out(row: SkillLabJob) -> dict[str, Any]:
    position = job_queue.position(row.id)
    return {
        "id": row.id,
        "type": row.type,
        "status": row.status,
        "queue_position": position if position is not None else 0,
        "progress": artifacts.job_progress(row.id, row.status),
        "skill_source": row.skill_source,
        "taskset_id": row.taskset_id,
        "taskset_name": row.taskset_name,
        "split": row.split,
        "params": row.params,
        "error": row.error,
        # `budget_exceeded` when the creator's monthly spend budget stopped it (a
        # train job between steps, or any job before it started), else null
        "stop_reason": job_budget.stop_reason_of(row.error),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        **creators.fields(row),
    }


def get_job(
    db: Session, workspace_id: str, job_id: str, *, viewer: Viewer = Viewer.ALL
) -> SkillLabJob:
    """The job, or 404 — also when workspace isolation hides it from ``viewer``."""
    row = db.get(SkillLabJob, job_id)
    if row is None or row.workspace_id != workspace_id:
        raise AppError("skill_lab.job_not_found", f"job '{job_id}' not found", status_code=404)
    visibility.require_seen(
        viewer, db, row.created_by,
        AppError("skill_lab.job_not_found", f"job '{job_id}' not found", status_code=404),
    )
    return row


def list_jobs(
    db: Session, workspace_id: str, type_filter: str | None = None, *,
    viewer: Viewer = Viewer.ALL,
) -> list[dict]:
    query = (
        select(SkillLabJob)
        .where(SkillLabJob.workspace_id == workspace_id)
        .where(visibility.clause(viewer, SkillLabJob.created_by))
        .order_by(SkillLabJob.created_at.desc())
    )
    if type_filter:
        query = query.where(SkillLabJob.type == type_filter)
    return [job_out(row) for row in db.scalars(query).all()]


def _resolve_tasks_file(
    db: Session, workspace_id: str, taskset_id: str, split: str | None, *,
    viewer: Viewer = Viewer.ALL,
) -> tuple[Path, str, str]:
    """(file path, resolved split, taskset name); split preference test→val→train."""
    row = taskset_svc.get_row(db, workspace_id, taskset_id, viewer=viewer)
    directory = taskset_svc.taskset_dir(row.id)
    if row.mode == "single":
        if split:
            raise AppError(
                "skill_lab.taskset_invalid",
                f"task set '{row.name}' has no splits (single mode) — omit `split`",
                status_code=422,
            )
        return directory / "tasks.json", "", row.name
    candidates = [split] if split else ["test", "val", "train"]
    for candidate in candidates:
        if candidate and (directory / f"{candidate}.json").is_file():
            return directory / f"{candidate}.json", candidate, row.name
    raise AppError(
        "skill_lab.taskset_invalid",
        (
            f"split '{split}' does not exist in task set '{row.name}'"
            if split
            else f"task set '{row.name}' carries none of the test/val/train splits"
        ),
        status_code=422,
    )


def _materialize_skill(
    workspace: Any,
    skill_source: dict[str, Any],
    directory: Path,
    log_lines: list[str],
) -> tuple[Path, dict[str, Any]]:
    kind = str(skill_source.get("kind") or "")
    if kind == "registry":
        record_id = str(skill_source.get("record_id") or "")
        if not record_id:
            raise AppError(
                "skill_lab.bad_params", "skill_source.record_id required", status_code=422
            )
        return runner.materialize_registry_skill(
            workspace, record_id, directory, log_lines.append
        )
    if kind == "upload":
        return runner.materialize_staged_skill(
            str(skill_source.get("staging_id") or ""),
            int(skill_source.get("index") or 0),
            directory,
        )
    raise AppError(
        "skill_lab.bad_params",
        "skill_source.kind must be 'registry' or 'upload'",
        status_code=422,
    )


def _enqueue(
    db: Session, row: SkillLabJob, command: list[str], *, notice: bool = False,
) -> dict[str, Any]:
    position = job_queue.submit(row.id, lambda: _run_job(row.id, command))
    row.queue_position = position
    db.commit()
    out = job_out(row)
    if notice:
        # still allowed either way; a train job stops between steps once the limit
        # is reached — say so up front when the estimate is more than is left
        out["budget_notice"] = None
        if row.created_by:
            units = job_budget.skill_lab_units(
                row.type, command, row.params, _taskset_counts(db, row))
            per_unit, _unit = job_budget.skill_lab_unit_usd(db, row.type)
            out["budget_notice"] = job_budget.notice(db, row.created_by, units * per_unit)
    return out


def _taskset_counts(db: Session, row: SkillLabJob) -> dict[str, Any]:
    if not row.taskset_id:
        return {}
    from app.skill_lab.models import SkillLabTaskset

    taskset = db.get(SkillLabTaskset, row.taskset_id)
    return dict(taskset.counts or {}) if taskset is not None else {}


def _refuse_unpriced(db: Session, creator: creators.Creator | None,
                     params: dict[str, Any]) -> None:
    """A budgeted creator cannot start a job on a model with no price (the route
    gate checked their spend; the models ride in the params)."""
    payer = budgets.payer_for_user_id(db, getattr(creator, "user_id", None),
                                      getattr(creator, "name", None))
    if not payer.budgeted:
        return
    for key in ("target_model", "judge_model", "model"):
        model = str(params.get(key) or "").strip()
        if model:
            budgets.check_model(payer, model)


def submit_eval_job(
    db: Session,
    workspace_id: str,
    workspace: Any,
    *,
    skill_source: dict[str, Any],
    taskset_id: str,
    split: str | None = None,
    params: dict[str, Any] | None = None,
    creator: creators.Creator | None = None,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    # The per-taskset lock keeps the source stable through snapshot copy and
    # the first commit that makes this job row visible. Delete then either
    # runs first or observes the committed reference and refuses.
    with taskset_svc.taskset_operation(taskset_id):
        runner.require_worker(workspace)
        merged_params = runner.clamp_params(params)
        _refuse_unpriced(db, creator, merged_params)
        tasks_file, resolved_split, taskset_name = _resolve_tasks_file(
            db, workspace_id, taskset_id, split, viewer=viewer
        )

        row = SkillLabJob(
            workspace_id=workspace_id,
            type="eval",
            taskset_id=taskset_id,
            taskset_name=taskset_name,
            split=resolved_split,
            params=merged_params,
        )
        creators.stamp(row, creator)
        db.add(row)
        db.flush()
        directory = artifacts.job_dir(row.id)
        directory.mkdir(parents=True, exist_ok=True)
        log_lines: list[str] = []
        try:
            inputs = taskset_svc.snapshot_taskset(taskset_id, directory / "inputs")
            tasks_file = inputs / tasks_file.name
            skill_dir, resolved_source = _materialize_skill(
                workspace, skill_source, directory, log_lines
            )
        except BaseException:
            runner.remove_job_dir(row.id)
            raise

        row.skill_source = resolved_source
        db.commit()
        if log_lines:  # materialization notes lead the job log
            (directory / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        command = runner.build_eval_command(
            skill_dir=skill_dir,
            tasks_file=tasks_file,
            out_dir=directory / "out",
            params=merged_params,
            assets_dir=inputs / "assets" if (inputs / "assets").is_dir() else None,
        )
        return _enqueue(db, row, command, notice=True)

def submit_train_job(
    db: Session,
    workspace_id: str,
    workspace: Any,
    *,
    skill_source: dict[str, Any],
    taskset_id: str,
    params: dict[str, Any] | None = None,
    creator: creators.Creator | None = None,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """Submit training after atomically snapshotting the whole task set. A task
    set hidden from ``viewer`` (workspace isolation) is a missing one."""
    # The per-taskset lock keeps the source stable through snapshot copy and
    # the first commit that makes this job row visible. Delete then either
    # runs first or observes the committed reference and refuses.
    with taskset_svc.taskset_operation(taskset_id):
        runner.require_worker(workspace)
        merged_params = runner.clamp_train_params(params)
        _refuse_unpriced(db, creator, merged_params)
        ts_row = taskset_svc.get_row(db, workspace_id, taskset_id, viewer=viewer)

        row = SkillLabJob(
            workspace_id=workspace_id,
            type="train",
            taskset_id=taskset_id,
            taskset_name=ts_row.name,
            split="",
            params=merged_params,
        )
        creators.stamp(row, creator)
        db.add(row)
        db.flush()
        directory = artifacts.job_dir(row.id)
        directory.mkdir(parents=True, exist_ok=True)
        log_lines: list[str] = []
        try:
            taskset_dir = taskset_svc.snapshot_taskset(taskset_id, directory / "inputs")
            skill_dir, resolved_source = _materialize_skill(
                workspace, skill_source, directory, log_lines
            )
            # Multi-doc training: join the checked files + SKILL.md into the one
            # seed document the trainer evolves. Runs at submit so a bad file
            # list 422s here instead of failing minutes into the run.
            seed_bundle = None
            if merged_params["trainable_files"]:
                seed_bundle = runner.build_seed_bundle(
                    skill_dir=skill_dir,
                    files=merged_params["trainable_files"],
                    out=directory / "seed_bundle.md",
                    log=log_lines.append,
                )
            split_env, has_test = runner.materialize_train_splits(
                taskset_dir, ts_row.mode, directory
            )
            config_file = runner.build_train_config(
                skill_dir=skill_dir,
                split_env=split_env,
                eval_test=has_test,
                out_config=directory / "config.yaml",
                params=merged_params,
                seed_bundle=seed_bundle,
            )
        except BaseException:
            runner.remove_job_dir(row.id)
            raise

        row.skill_source = resolved_source
        db.commit()
        if log_lines:
            (directory / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        command = runner.build_train_command(config_file=config_file, out_dir=directory / "out")
        return _enqueue(db, row, command, notice=True)

def _materialize_taskgen_skills(
    workspace: Any,
    skill_source: dict[str, Any],
    directory: Path,
    log_lines: list[str],
) -> tuple[list[Path], dict[str, Any]]:
    """Taskgen accepts the eval-style single source OR a registry multi-select
    (`record_ids`) for a unified multi-skill task set (upstream plugin mode)."""
    record_ids = skill_source.get("record_ids")
    if str(skill_source.get("kind") or "") == "registry" and record_ids is not None:
        ids = [str(rid) for rid in record_ids if str(rid).strip()]
        if not 1 <= len(ids) <= 8:
            raise AppError(
                "skill_lab.bad_params",
                "skill_source.record_ids must carry 1-8 registry record ids",
                status_code=422,
            )
        if len(set(ids)) != len(ids):
            raise AppError(
                "skill_lab.bad_params",
                "skill_source.record_ids must be unique",
                status_code=422,
            )
        skill_dirs: list[Path] = []
        names: list[str] = []
        for record_id in ids:
            skill_dir, resolved = runner.materialize_registry_skill(
                workspace, record_id, directory, log_lines.append
            )
            skill_dirs.append(skill_dir)
            names.append(str(resolved.get("name") or skill_dir.name))
        if len({d.name for d in skill_dirs}) != len(skill_dirs):
            # generate_tasks.py keys coverage on skill names — a duplicate would
            # also have collided on disk under <job>/skills/<name>/.
            raise AppError(
                "skill_lab.bad_params",
                f"selected records resolve to duplicate skill names: {sorted(names)}",
                status_code=422,
            )
        return skill_dirs, {"kind": "registry", "record_ids": ids, "names": names}
    skill_dir, resolved = _materialize_skill(workspace, skill_source, directory, log_lines)
    return [skill_dir], resolved


def _snapshot_taskgen_attachments(
    workspace_id: str, attachments: list[dict[str, Any]], directory: Path
) -> tuple[list[dict[str, Any]], Path | None]:
    """Resolve staged tokens into an immutable per-job attachment snapshot.

    Returns the trusted manifest plus the assets root, or `(<empty>, None)` when
    the job has no attachments. Names, media types and sizes come from the staging
    record — never from the request — and every blob is digest-verified on the way
    in, so a later task-set edit or a swept staging area cannot change what this
    job generates against.
    """
    if not attachments:
        return [], None
    if len(attachments) > runner.TASKGEN_MAX_ATTACHMENTS:
        raise AppError(
            "skill_lab.asset_limit_exceeded",
            f"at most {runner.TASKGEN_MAX_ATTACHMENTS} attachments per taskgen job",
            status_code=422,
        )
    inputs = directory / "inputs"
    assets_dir = inputs / task_assets.ASSETS_DIRNAME
    assets_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    consumed: list[tuple[Path, str]] = []
    for value in attachments:
        if not isinstance(value, dict) or "staged_asset" not in value:
            raise AppError(
                "skill_lab.asset_descriptor_invalid",
                "each taskgen attachment must carry a staged_asset token",
                status_code=422,
            )
        record, blob, stage_dir = task_assets.resolve_staged(
            workspace_id, str(value.get("staged_asset") or "")
        )
        name = str(record["name"])
        folded = name.casefold()
        if folded in seen:
            raise AppError(
                "skill_lab.asset_duplicate_name",
                f"attachment name {name!r} is duplicated in this taskgen job",
                status_code=422,
            )
        seen.add(folded)
        total += int(record["size"])
        if total > runner.TASKGEN_MAX_ATTACHMENT_BYTES:
            raise AppError(
                "skill_lab.asset_limit_exceeded",
                "taskgen attachments exceed the 25 MiB aggregate limit",
                status_code=413,
            )
        digest = str(record["sha256"])
        target = assets_dir / digest
        if not target.exists():
            shutil.copyfile(blob, target)
        task_assets._verify_blob(target, {"size": int(record["size"]), "sha256": digest})
        manifest.append(
            {
                "name": name,
                "media_type": str(record["media_type"]),
                "size": int(record["size"]),
                "sha256": digest,
            }
        )
        consumed.append((stage_dir, str(record["staged_asset"])))
    (inputs / "attachments.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    task_assets.consume_staged(consumed)
    return manifest, assets_dir


def submit_taskgen_job(
    db: Session,
    workspace_id: str,
    workspace: Any,
    *,
    skill_source: dict[str, Any],
    taskset_id: str | None = None,
    target_split: str | None = None,
    params: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    creator: creators.Creator | None = None,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """AI task-set generation (studio parity): an exec-backend agent authors
    tasks for the selected skill(s) on the AgentCore worker; the result is a
    reviewable artifact, imported into a task set only by an explicit action.

    `attachments` carries staged asset tokens: the bytes are snapshotted into the
    job and materialized into the generation work directory, so the agent authors
    tasks against real documents instead of pasted text."""
    runner.require_worker(workspace)
    merged_params = runner.clamp_taskgen_params(params)
    _refuse_unpriced(db, creator, merged_params)
    if (taskset_id is None) != (target_split is None):
        raise AppError(
            "skill_lab.bad_params",
            "taskset_id and target_split must be provided together for expansion",
            status_code=422,
        )

    ts_row = None
    tasks_by_split: dict[str, Any] = {}
    if taskset_id is not None:
        ts_row = taskset_svc.get_row(db, workspace_id, taskset_id, viewer=viewer)
        # Refuse at submit time, not at apply time: generating against a
        # read-only sample would only ever dead-end at apply-expansion.
        taskset_svc._reject_sample_write(ts_row)
        tasks_by_split = taskset_svc.read_taskset(
            db, workspace_id, taskset_id, full=True
        )["tasks_by_split"]
        if ts_row.mode == "single":
            if target_split != "tasks":
                raise AppError(
                    "skill_lab.bad_params",
                    "single-mode expansion target_split must be 'tasks'",
                    status_code=422,
                )
        elif target_split not in tasks_by_split and target_split != "test":
            raise AppError(
                "skill_lab.bad_params",
                "split-mode target_split must be an existing split or 'test', "
                f"got '{target_split}'",
                status_code=422,
            )

    row = SkillLabJob(
        workspace_id=workspace_id,
        type="taskgen",
        taskset_id=ts_row.id if ts_row is not None else "",
        taskset_name=ts_row.name if ts_row is not None else "",
        split=target_split or "",
        params=merged_params,
    )
    creators.stamp(row, creator)
    db.add(row)
    db.flush()
    directory = artifacts.job_dir(row.id)
    directory.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    try:
        skill_dirs, resolved_source = _materialize_taskgen_skills(
            workspace, skill_source, directory, log_lines
        )
        manifest, attachment_assets = _snapshot_taskgen_attachments(
            workspace_id, list(attachments or []), directory
        )
        if manifest:
            log_lines.append(
                "attachments: "
                + ", ".join(f"{row['name']} ({row['size']}B)" for row in manifest)
            )
        expansion = None
        if ts_row is not None and target_split is not None:
            snapshot = runner.write_expansion_snapshot(
                directory, ts_row.id, target_split, tasks_by_split
            )
            expansion = (snapshot, target_split)
    except BaseException:
        runner.remove_job_dir(row.id)
        raise

    row.skill_source = resolved_source
    if manifest:
        # Recorded in params (already echoed by job_out) rather than read from disk
        # per row: the panel can then show what a RUNNING or failed job was given,
        # while gen_summary only exists once generation succeeded.
        row.params = {
            **(row.params or {}),
            "attachment_names": [row_["name"] for row_ in manifest],
        }
    db.commit()
    if log_lines:
        (directory / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    command = runner.build_taskgen_command(
        skill_dirs=skill_dirs,
        out_root=directory / "out",
        params=merged_params,
        expansion=expansion,
        attachments=(directory / "inputs" / "attachments.json") if manifest else None,
        attachment_assets=attachment_assets,
    )
    return _enqueue(db, row, command, notice=True)


def strip_derived_task_fields(tasks: list[Any]) -> list[Any]:
    """Return generated tasks as an AUTHORED document, not a normalized one.

    `load_tasks` decorates every item with derived state — a `judge_mode` default,
    private `_`-prefixed bookkeeping — and taskgen output has been through it. Saving
    that verbatim is what broke a live prod run: the loader recomputes
    `_judge_mode_explicit` from the mere presence of `judge_mode`, so a default
    nobody chose came back as an explicit per-task choice and outranked the
    run-level `chat`, escalating artifact tasks to the agentic judge.

    So: private fields always go, and `judge_mode` goes unless the generator really
    declared it. A declared one is kept and will correctly re-read as explicit.

    Public loader defaults (`task_type`, `artifact_checks`, `files`) are left alone
    — they carry no such promotion, and dropping them would change what the review
    table and the task editor display.
    """
    stripped: list[Any] = []
    for task in tasks:
        if not isinstance(task, dict):
            stripped.append(task)
            continue
        explicit = bool(task.get("_judge_mode_explicit"))
        stripped.append(
            {
                key: value
                for key, value in task.items()
                if not key.startswith("_") and (key != "judge_mode" or explicit)
            }
        )
    return stripped


# Author fields an operator may change while reviewing generated tasks. Everything
# else on a row (files, target_skills, attachments, judge contract…) is rebuilt
# from the immutable generated_tasks.json, never taken from the request.
TASKGEN_AUTHOR_FIELDS = ("id", "question", "rubric", "task_type")
# Per-field caps for review edits: the generated document is bounded by the
# taskgen count clamp, so these bound one row's text, not the task set.
TASKGEN_EDIT_ID_MAX_CHARS = 200
TASKGEN_EDIT_TYPE_MAX_CHARS = 64
TASKGEN_EDIT_TEXT_MAX_CHARS = 64_000


def select_taskgen_rows(tasks: list[Any], edits: list[dict[str, Any]] | None) -> list[Any]:
    """Rebuild the rows to save from ORIGINAL-row references plus author-field edits.

    `edits` is the complete selection: a generated row absent from it is excluded.
    Each entry names one original row by its index in generated_tasks.json and
    may override any of TASKGEN_AUTHOR_FIELDS (a None value keeps the original;
    an empty `task_type` clears the field so the loader default applies). Rows
    keep their generated order regardless of the order the references arrive in.
    `None` (no edits at all) is the legacy full import of every row verbatim.

    The router's strict models already bound the indices, forbid unknown fields
    and reject coerced ints; this re-checks range/duplication because those are
    document-relative, and refuses duplicate ids so a bad edit fails here with a
    row-level message instead of deep inside the validator subprocess.
    """
    if edits is None:
        return list(tasks)
    if not edits:
        raise AppError(
            "skill_lab.taskgen_empty_selection",
            "no generated tasks selected — keep at least one row to save",
            detail={"reason": "empty"},
            status_code=422,
        )
    by_index: dict[int, dict[str, Any]] = {}
    for edit in edits:
        index = edit["index"]
        if type(index) is not int or index < 0:
            raise AppError(
                "skill_lab.taskgen_bad_selection",
                f"row reference {index!r} is not a non-negative integer",
                detail={"reason": "not_an_index", "index": str(index)},
                status_code=422,
            )
        if index >= len(tasks):
            raise AppError(
                "skill_lab.taskgen_bad_selection",
                f"row {index} does not exist — the job generated {len(tasks)} tasks",
                detail={"reason": "out_of_range", "index": index, "count": len(tasks)},
                status_code=422,
            )
        if index in by_index:
            raise AppError(
                "skill_lab.taskgen_bad_selection",
                f"row {index} is referenced more than once",
                detail={"reason": "repeated", "index": index},
                status_code=422,
            )
        by_index[index] = edit
    selected: list[Any] = []
    for index in sorted(by_index):
        original = tasks[index]
        if not isinstance(original, dict):
            raise AppError(
                "skill_lab.taskgen_bad_selection",
                f"row {index} is not a task object",
                detail={"reason": "not_a_task", "index": index},
                status_code=422,
            )
        edit = by_index[index]
        task = dict(original)
        for field in ("id", "question", "rubric"):
            value = edit.get(field)
            if value is not None:
                task[field] = value
        task_type = edit.get("task_type")
        if task_type is not None:
            if task_type:
                task["task_type"] = task_type
            else:
                task.pop("task_type", None)
        selected.append(task)
    seen: set[str] = set()
    duplicates: list[str] = []
    for task in selected:
        task_id = str(task.get("id"))
        if task_id in seen and task_id not in duplicates:
            duplicates.append(task_id)
        seen.add(task_id)
    if duplicates:
        raise AppError(
            "skill_lab.taskgen_duplicate_id",
            "edited ids are not unique: " + ", ".join(sorted(duplicates)),
            detail={"ids": sorted(duplicates)},
            status_code=422,
        )
    return selected


def _bind_taskgen_attachments(
    job_id: str, tasks: list[Any]
) -> tuple[list[Any], dict[str, Path]]:
    """Turn each task's `attachments` declaration into real asset descriptors.

    The generation agent only ever names documents; the digests come from this
    job's own manifest, so a declaration cannot reference bytes the job was not
    given. Returns the rewritten tasks plus the digest->blob map the task-set
    write draws from (`extra_sources`), since these bytes live in the job
    snapshot rather than in staging or in the task set.

    The declaration is dropped once bound: `files` then carries the whole truth,
    and two representations of the same input would only drift.
    """
    inputs = artifacts.job_dir(job_id) / "inputs"
    manifest_path = inputs / "attachments.json"
    declared_anywhere = any(
        isinstance(task, dict) and task.get("attachments") for task in tasks
    )
    if not manifest_path.is_file():
        if declared_anywhere:
            raise AppError(
                "skill_lab.attachment_missing",
                f"job {job_id} declares attachments but has no attachment manifest",
                status_code=409,
            )
        return tasks, {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_name = {str(row["name"]): row for row in manifest}
    assets_root = inputs / task_assets.ASSETS_DIRNAME
    bound: list[Any] = []
    sources: dict[str, Path] = {}
    for task in tasks:
        if not isinstance(task, dict) or not task.get("attachments"):
            bound.append(task)
            continue
        task = dict(task)
        names = task.pop("attachments")
        files = dict(task.get("files") or {})
        for name in names:
            record = by_name.get(str(name))
            if record is None:
                raise AppError(
                    "skill_lab.attachment_unknown",
                    f"task {task.get('id')!r} declares attachment {name!r}, which this "
                    "job was not given",
                    status_code=409,
                )
            destination = f"{runner.TASKGEN_ATTACHMENT_DIR}/{record['name']}"
            if destination in files:
                raise AppError(
                    "skill_lab.attachment_path_conflict",
                    f"task {task.get('id')!r} both declares attachment "
                    f"{record['name']!r} and defines {destination!r} inline",
                    status_code=409,
                )
            digest = str(record["sha256"])
            files[destination] = {
                "asset": f"sha256:{digest}",
                "name": str(record["name"]),
                "media_type": str(record["media_type"]),
                "size": int(record["size"]),
            }
            sources[digest] = assets_root / digest
        task["files"] = files
        bound.append(task)
    return bound, sources


def _finished_taskgen_job(db: Session, workspace_id: str, job_id: str) -> SkillLabJob:
    row = get_job(db, workspace_id, job_id)
    if row.type != "taskgen":
        raise AppError(
            "skill_lab.not_a_taskgen_job",
            f"job {job_id} is a {row.type} job",
            status_code=400,
        )
    if row.status != "succeeded":
        raise AppError(
            "skill_lab.job_not_finished",
            f"job {job_id} is {row.status} — generated tasks exist only after success",
            status_code=409,
        )
    return row


def import_taskgen_taskset(
    db: Session,
    workspace_id: str,
    job_id: str,
    *,
    name: str,
    edits: list[dict[str, Any]] | None = None,
    creator: creators.Creator | None = None,
) -> dict[str, Any]:
    """Save a taskgen job's reviewed output as a NEW single-mode task set.

    `edits` (see select_taskgen_rows) lets the reviewer drop rows and fix author
    fields first; None saves every generated row as before. Goes through the
    taskset service's validated staging swap — the same validator subprocess as
    an upload — so the import can never plant a file the eval/train CLIs would
    refuse. Nothing is written and the job stays un-imported when any check fails."""
    row = _finished_taskgen_job(db, workspace_id, job_id)
    if (row.params or {}).get("imported_taskset_id"):
        raise AppError(
            "skill_lab.already_imported",
            f"job {job_id} was already imported as task set "
            f"{row.params['imported_taskset_id']}",
            status_code=409,
        )
    results = artifacts.taskgen_results(row.id)
    if results is None:
        raise AppError(
            "skill_lab.results_missing",
            f"job {job_id} has no generated_tasks.json on disk",
            status_code=409,
        )
    tasks, extra_sources = _bind_taskgen_attachments(
        row.id, strip_derived_task_fields(select_taskgen_rows(results["tasks"], edits))
    )
    info = taskset_svc.create_taskset(
        db,
        workspace_id,
        name=name,
        mode="single",
        tasks_by_split={"tasks": tasks},
        extra_sources=extra_sources,
        creator=creator,
    )
    row.params = {**(row.params or {}), "imported_taskset_id": info["id"]}
    db.commit()
    return {"job": job_out(row), "taskset": info}


def apply_taskgen_expansion(
    db: Session,
    workspace_id: str,
    job_id: str,
    *,
    edits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append a taskgen expansion job's output to its target task set/split.

    `edits` (see select_taskgen_rows) narrows/fixes the rows first; None appends
    every generated row as before. Full-replace through update_taskset (validated
    staging swap). The id-collision re-check runs on the EDITED ids against every
    current split — the CLI checked its own snapshot, but the task set may have
    changed since, and a reviewer may have renamed a row onto an existing id."""
    row = _finished_taskgen_job(db, workspace_id, job_id)
    if not row.taskset_id:
        raise AppError(
            "skill_lab.not_an_expansion_job",
            f"job {job_id} did not target an existing task set",
            status_code=400,
        )
    if (row.params or {}).get("expanded"):
        raise AppError(
            "skill_lab.already_imported",
            f"job {job_id} was already applied to task set {row.taskset_id}",
            status_code=409,
        )
    results = artifacts.taskgen_results(row.id)
    if results is None:
        raise AppError(
            "skill_lab.results_missing",
            f"job {job_id} has no generated_tasks.json on disk",
            status_code=409,
        )
    selected = select_taskgen_rows(results["tasks"], edits)
    ts_row = taskset_svc.get_row(db, workspace_id, row.taskset_id)  # 404 if deleted
    merged = dict(
        taskset_svc.read_taskset(db, workspace_id, ts_row.id, full=True)["tasks_by_split"]
    )
    existing_ids = {
        str(task.get("id"))
        for tasks in merged.values()
        for task in tasks
        if isinstance(task, dict)
    }
    collisions = sorted(
        str(task.get("id"))
        for task in selected
        if isinstance(task, dict) and str(task.get("id")) in existing_ids
    )
    if collisions:
        raise AppError(
            "skill_lab.expansion_conflict",
            "the task set changed since generation — generated ids now collide: "
            + ", ".join(collisions),
            detail={"ids": collisions},
            status_code=409,
        )
    tasks, extra_sources = _bind_taskgen_attachments(
        row.id, strip_derived_task_fields(selected)
    )
    target = row.split
    merged[target] = list(merged.get(target, [])) + tasks
    info = taskset_svc.update_taskset(
        db, workspace_id, ts_row.id, tasks_by_split=merged, extra_sources=extra_sources
    )
    row.params = {**(row.params or {}), "expanded": True}
    db.commit()
    return {"job": job_out(row), "taskset": info}


def resume_job(db: Session, workspace_id: str, workspace: Any, job_id: str) -> dict[str, Any]:
    """Re-enqueue an interrupted/failed TRAIN job — train.py resumes from
    out/runtime_state.json after the last completed step, so this is the same
    command over the same on-disk config/out, not new machinery. Eval jobs are
    cheap: re-submit instead."""
    runner.require_worker(workspace)
    row = get_job(db, workspace_id, job_id)
    if row.type != "train":
        raise AppError(
            "skill_lab.job_not_resumable",
            "only training jobs resume — submit evaluations again",
            status_code=400,
        )
    if row.status not in ("interrupted", "failed"):
        raise AppError(
            "skill_lab.job_not_resumable",
            f"job is {row.status}; only interrupted/failed training jobs resume",
            status_code=400,
        )
    config_file = artifacts.job_dir(job_id) / "config.yaml"
    if not config_file.is_file():
        raise AppError(
            "skill_lab.job_not_resumable",
            "the job's config.yaml is gone — submit a new training job",
            status_code=400,
        )
    # the job's creator keeps paying for its remaining steps, whoever resumes it
    # (the route gate checked the caller): refused while they are over budget
    budgets.check(db, budgets.payer_for_user_id(db, row.created_by, row.created_by_name))
    row.status = "queued"
    row.cancel_requested = False
    row.error = None
    row.started_at = None
    row.finished_at = None
    db.commit()
    command = runner.build_train_command(
        config_file=config_file, out_dir=artifacts.job_dir(job_id) / "out"
    )
    return _enqueue(db, row, command)


def _split_best_bundle(job_id: str):
    """Deployable SkillBundle from a bundle-trained job: split best_skill.md
    onto a copy of the original skill dir (frozen files kept, trained files +
    SKILL.md overwritten). The split dir is rebuilt on every publish call."""
    import shutil

    from app.services.skill_ingest import bundle_from_dir

    directory = artifacts.job_dir(job_id)
    skills_root = directory / "skills"
    skill_dirs = [p for p in skills_root.iterdir() if p.is_dir()] if skills_root.is_dir() else []
    if len(skill_dirs) != 1:
        raise AppError(
            "skill_lab.publish_unsupported",
            "the job's materialized skill directory is missing — resubmit the training",
            status_code=400,
        )
    out_dir = directory / "publish_skill"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    log_lines: list[str] = []
    runner.split_trained_bundle(
        bundle_file=artifacts.out_root(job_id) / "best_skill.md",
        skill_dir=skill_dirs[0],
        out_dir=out_dir,
        log=log_lines.append,
    )
    return bundle_from_dir(out_dir)


def publish_job(
    db: Session,
    workspace_id: str,
    workspace: Any,
    job_id: str,
    *,
    reapprove: bool = False,
) -> dict[str, Any]:
    """Push best_skill.md back onto the source Registry record as a bumped
    minor version via update_record(skill_md=...) — the PUT path works for
    every source kind, unlike /reimport (git/url-only). Any update settles the
    record into DRAFT; with `reapprove` a previously-APPROVED record is
    re-approved (refresh_a2a_cards pattern)."""
    from app.services import registry_console

    row = get_job(db, workspace_id, job_id)
    if row.type != "train" or row.status != "succeeded":
        raise AppError(
            "skill_lab.publish_unsupported",
            "only a succeeded training job can publish its skill",
            status_code=400,
        )
    source = row.skill_source or {}
    if source.get("kind") != "registry" or not source.get("record_id"):
        raise AppError(
            "skill_lab.publish_unsupported",
            "this job's skill came from an upload — download best_skill.md from "
            "the artifacts instead",
            status_code=400,
        )
    diff = artifacts.skill_diff(job_id)
    if diff is None:
        raise AppError(
            "skill_lab.results_pending", "best_skill.md is not available", status_code=404
        )
    if not diff["changed"]:
        raise AppError(
            "skill_lab.publish_no_change",
            "training produced no accepted edits — nothing to publish",
            status_code=400,
        )
    record_id = str(source["record_id"])
    # A system-managed Skill (SE-043) is never published onto from here. Refused
    # BEFORE the record read, before the multi-file split rebuilds publish_skill/
    # (rmtree + splitter subprocess) and before update_record's own guard.
    from app.system_agents.skill_registry import refuse_protected_mutation

    refuse_protected_mutation(workspace, record_id, "edit")
    before = registry_console.console_get(workspace, record_id)
    status_before = str(before.get("status") or "")
    trainable = list((row.params or {}).get("trainable_files") or [])
    if trainable:
        # best_skill.md is a multi-doc bundle — split it back onto a copy of
        # the original skill dir (kept in the job dir since submit) and push
        # the whole bundle, replacing the trained files AND SKILL.md.
        updated = registry_console.update_record(
            record_id, workspace, bundle=_split_best_bundle(row.id)
        )
    else:
        updated = registry_console.update_record(record_id, workspace, skill_md=diff["best"])
    status_after = str(updated.get("status") or "")
    reapproved = False
    if reapprove and status_before == "APPROVED" and status_after != "APPROVED":
        updated = registry_console.console_action(workspace, record_id, "approve")
        status_after = str(updated.get("status") or status_after)
        reapproved = True
    result = {
        "record_id": record_id,
        "name": source.get("name"),
        "new_version": str(updated.get("recordVersion") or ""),
        "status_before": status_before,
        "status_after": status_after,
        "reapproved": reapproved,
    }
    with open(artifacts.job_dir(job_id) / "log.txt", "a", encoding="utf-8") as log:
        log.write(
            f"\n[publish] {result['name']} -> v{result['new_version']} "
            f"(status {status_before} -> {status_after}"
            f"{', re-approved' if reapproved else ''})\n"
        )
    return result


def _set_status(job_id: str, **values: Any) -> SkillLabJob | None:
    """Write through a dedicated session — worker threads own their transactions."""
    db = SessionLocal()
    try:
        row = db.get(SkillLabJob, job_id)
        if row is None:
            return None
        for key, value in values.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


def _cancel_requested(job_id: str) -> bool:
    db = SessionLocal()
    try:
        row = db.get(SkillLabJob, job_id)
        return bool(row and row.cancel_requested)
    finally:
        db.close()


def _job_budget(row: SkillLabJob) -> job_budget.JobBudget:
    """The job's payer: its creator (``SkillLabJob.created_by``)."""
    return job_budget.JobBudget.for_user(
        row.created_by, SOURCE_SKILL_LAB, user_name=row.created_by_name,
        origin_workspace_id=row.workspace_id, ref_type="skill_lab_job", ref_id=row.id,
    )


def _budget_stop_fields(job_type: str, error: AppError) -> dict[str, Any]:
    """The terminal fields of a job its creator's budget stopped: a training job is
    ``interrupted`` (resumable from its last completed step); an eval / taskgen job
    can only be stopped before it starts, so it ``failed`` without running."""
    message = job_budget.stop_message(error)
    if job_type == "train":
        return {"status": "interrupted", "finished_at": datetime.now(UTC),
                "error": f"{message} — resume it to continue from the last completed step"}
    return {"status": "failed", "finished_at": datetime.now(UTC),
            "error": f"{message} — it did not start; submit it again"}


def _debit_start(
    budget: job_budget.JobBudget, job_type: str, command: list[str],
    params: dict[str, Any], counts: dict[str, Any], first_step: int,
) -> list[str]:
    """The estimate debited as the job's work starts: every task of an eval job,
    the flat taskgen amount, the FIRST step of a training run (later steps are
    debited as they start, see ``_wait``)."""
    try:
        db = SessionLocal()
        try:
            per_unit, unit = job_budget.skill_lab_unit_usd(db, job_type)
        finally:
            db.close()
        if job_type == "train":
            units, detail = 1, {"estimate": "skill_lab_train_step", "step": first_step}
        else:
            units = job_budget.skill_lab_units(job_type, command, params, counts)
            detail = {"estimate": f"skill_lab_{job_type}"}
        row_id = budget.estimate(round(units * per_unit, 8), quantity=units, unit=unit,
                                 detail=detail)
        return [row_id] if row_id else []
    except Exception:  # noqa: BLE001 — the process is already running: never fail it here
        logger.warning("skill-lab %s: could not debit its estimate", budget.ref_id,
                       exc_info=True)
        return []


def _completed_steps(job_id: str) -> int | None:
    """Steps a training run has completed (its out/history.json, rewritten after
    every step); None on a torn read."""
    path = artifacts.out_root(job_id) / "history.json"
    if not path.is_file():
        return 0
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return len(history) if isinstance(history, list) else None


def _total_steps(job_id: str) -> int | None:
    """epochs × steps per epoch, as the trainer resolved them (out/config.json)."""
    try:
        config = json.loads((artifacts.out_root(job_id) / "config.json").read_text("utf-8"))
        return int(config["num_epochs"]) * int(config["steps_per_epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _wait(
    job_id: str, proc: Any, budget: job_budget.JobBudget, job_type: str, step_usd: float,
    started_through: int,
) -> tuple[int, AppError | None]:
    """Wait for the CLI. A training run is watched at its step boundaries: after
    each completed step its creator's budget is checked — over the limit, the run
    is killed right there (the trainer checkpointed the step: resumable) — and the
    step now starting is debited. Returns ``(returncode, budget stop or None)``."""
    if job_type != "train":
        return proc.wait(), None
    seen = _completed_steps(job_id) or 0
    total: int | None = None
    while True:
        try:
            return proc.wait(timeout=STEP_POLL_S), None
        except subprocess.TimeoutExpired:
            pass
        done = _completed_steps(job_id)
        if done is None or done <= seen:
            continue
        seen = done
        stop = budget.exceeded()
        if stop is not None:
            kill_process_group(proc)
            return proc.wait(), stop
        total = total or _total_steps(job_id)
        through = done + 1 if total is None or done < total else done
        for step in range(started_through + 1, through + 1):
            budget.estimate(step_usd, quantity=1, unit="step",
                            detail={"estimate": "skill_lab_train_step", "step": step})
        started_through = max(started_through, through)


def _true_up_eval(job_id: str, budget: job_budget.JobBudget, params: dict[str, Any],
                  estimates: list[str]) -> None:
    """Replace an eval job's estimate with what ``results.json`` reports: the
    target rollouts' tokens (claude: all four counters; codex: only a ``total``,
    priced at the output rate) and the judge's ``{input, output}``, each priced at
    its own model. Without results (a failed or cancelled job), or when a model
    has no price, the estimate stands."""
    from app.services import rates
    from app.services.usage_meter import TokenUsage, supersede

    try:
        raw = json.loads((artifacts.out_root(job_id) / "results.json").read_text("utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, list):
        return
    sides = {"target": TokenUsage(), "judge": TokenUsage()}
    rows = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows += 1
        for side, key in (("target", "usage"), ("judge", "judge_usage")):
            record = artifacts.usage_record(item.get(key))
            usage = sides[side]
            usage.tokens_in += record["input"] or 0
            usage.cache_write += record["cache_write"] or 0
            usage.cache_read += record["cache_read"] or 0
            # a total with no breakdown (codex) is priced at the output rate
            usage.tokens_out += (record["output"] or 0) + (record["unattributed"] or 0)
    models = {"target": str(params.get("target_model") or "") or None,
              "judge": str(params.get("judge_model") or "") or None}
    reported = {side: usage for side, usage in sides.items() if usage.total}
    if not reported:
        return
    unpriced = [models[side] for side in reported if rates.price_of(models[side]) is None]
    if unpriced:
        logger.warning("skill-lab %s: true-up skipped, no price for %s", job_id, unpriced)
        return
    meter = budget.meter()
    written = [
        row_id for side, usage in reported.items()
        if (row_id := meter.record_tokens(
            usage, model_id=models[side], upsert_session=False,
            detail={"side": side, "tasks": rows, "true_up": "results.json"},
        ))
    ]
    if written:
        supersede(estimates, written[0])


def _run_job(job_id: str, command: list[str]) -> None:
    """Worker-thread body: spawn → wait → terminal transition. Never raises."""
    db = SessionLocal()
    try:
        row = db.get(SkillLabJob, job_id)
        if row is None or row.status != "queued":
            return  # cancelled while queued, or swept
        workspace_id = row.workspace_id or ""
        job_type = row.type
        params = dict(row.params or {})
        budget = _job_budget(row)
        counts = _taskset_counts(db, row)
        step_usd, _unit = job_budget.skill_lab_unit_usd(db, "train")
    finally:
        db.close()

    # a job that waited in the queue starts only while its creator has budget left
    stop = budget.exceeded()
    if stop is not None:
        _set_status(job_id, **_budget_stop_fields(job_type, stop))
        return

    estimates: list[str] = []
    try:
        workspace = context_for_workspace(workspace_id)
        env = runner.build_job_env(workspace)
        _set_status(job_id, status="running", started_at=datetime.now(UTC), queue_position=0)
        log_path = artifacts.job_dir(job_id) / "log.txt"
        first_step = (_completed_steps(job_id) or 0) + 1
        with open(log_path, "ab") as log_file:
            proc = runner.spawn(command, env, log_file)
            with _procs_lock:
                _procs[job_id] = proc
            estimates = _debit_start(budget, job_type, command, params, counts, first_step)
            # A cancel between the `running` flip above and the registration
            # just above finds no process to kill, so it would otherwise let a
            # full-price run finish and only then report "cancelled".
            if _cancel_requested(job_id):
                kill_process_group(proc)
            returncode, stop = _wait(job_id, proc, budget, job_type, step_usd, first_step)
    except Exception as exc:  # noqa: BLE001 — status carries the failure
        logger.exception("skill-lab job %s crashed before/at spawn", job_id)
        _set_status(
            job_id,
            status="failed",
            error=f"failed to start: {exc}",
            finished_at=datetime.now(UTC),
        )
        return
    finally:
        with _procs_lock:
            _procs.pop(job_id, None)

    if _cancel_requested(job_id):
        _set_status(job_id, status="cancelled", finished_at=datetime.now(UTC))
    elif stop is not None:
        _set_status(job_id, **_budget_stop_fields(job_type, stop))
    elif returncode == 0:
        _set_status(job_id, status="succeeded", finished_at=datetime.now(UTC))
    else:
        tail = artifacts.log_tail(job_id)[-800:]
        _set_status(
            job_id,
            status="failed",
            error=f"process exited {returncode}\n…{tail}",
            finished_at=datetime.now(UTC),
        )
    if job_type == "eval":
        _true_up_eval(job_id, budget, params, estimates)

    try:
        workspace = context_for_workspace(workspace_id)
        runner.sweep_exec_jobs_prefix(workspace, lambda m: logger.info("%s: %s", job_id, m))
    except Exception:  # noqa: BLE001
        logger.info("skill-lab janitor skipped for %s", job_id)


def cancel_job(db: Session, workspace_id: str, job_id: str) -> dict[str, Any]:
    row = get_job(db, workspace_id, job_id)
    if row.status == "queued":
        row.status = "cancelled"
        row.cancel_requested = True
        row.finished_at = datetime.now(UTC)
        db.commit()
        return job_out(row)
    if row.status != "running":
        raise AppError(
            "skill_lab.job_not_cancellable",
            f"job is {row.status}; only queued/running jobs can be cancelled",
            status_code=400,
        )
    row.cancel_requested = True
    db.commit()
    with _procs_lock:
        proc = _procs.get(job_id)
    if proc is not None:
        kill_process_group(proc)
    db.expire_all()
    return job_out(get_job(db, workspace_id, job_id))


def delete_job(db: Session, workspace_id: str, job_id: str) -> None:
    row = get_job(db, workspace_id, job_id)
    if row.status not in TERMINAL_STATUSES:
        raise AppError(
            "skill_lab.job_not_deletable",
            "only finished jobs can be deleted — cancel it first",
            status_code=400,
        )
    db.delete(row)
    db.commit()
    runner.remove_job_dir(job_id)


def taskset_in_use(db: Session, workspace_id: str, taskset_id: str) -> bool:
    row = db.scalars(
        select(SkillLabJob)
        .where(
            SkillLabJob.workspace_id == workspace_id,
            SkillLabJob.taskset_id == taskset_id,
        )
        .limit(1)
    ).first()
    return row is not None


def sweep_interrupted_jobs() -> None:
    """Startup janitor: a restart killed every job subprocess (they are children
    of this process), so non-terminal rows are marked interrupted honestly rather
    than resumed here. A training job can be continued afterwards on request
    (`resume_job`); an evaluation is cheap enough to re-submit. Registered in
    main.py next to the other resume hooks — resume_pending_jobs cannot see this
    table."""
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(SkillLabJob).where(SkillLabJob.status.in_(("queued", "running")))
        ).all()
        for row in rows:
            row.status = "interrupted"
            row.error = (
                "interrupted by a backend restart — the subprocess is gone; "
                + (
                    "resume it to continue from the last completed step"
                    if row.type == "train"
                    else "submit the job again"
                )
            )
            row.finished_at = datetime.now(UTC)
        if rows:
            db.commit()
            logger.info("skill-lab: swept %d interrupted job(s)", len(rows))
    finally:
        db.close()
