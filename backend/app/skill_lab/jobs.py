"""Skill Lab job lifecycle: submit, queue, run, cancel, restart sweep.

Execution model (parent design §5): a bounded worker pool (EvalRunQueue reuse)
runs one vendored-CLI subprocess per job; the worker thread owns the terminal
status transition. Cancel kills the whole process group via an in-memory
{job_id: Popen} registry — single-process uvicorn is an established assumption
(same as the registry inspect staging). After a backend restart the old process
group is gone, so non-terminal rows are swept to `interrupted`; only training
jobs can be picked up again (`resume_job` — the trainer checkpoints per step).
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation.queue import EvalRunQueue
from app.services.local_exec import kill_process_group
from app.services.workspace import context_for_workspace
from app.skill_lab import artifacts, runner
from app.skill_lab import tasksets as taskset_svc
from app.skill_lab.models import SkillLabJob

logger = logging.getLogger("launchpad.skill_lab")

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

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
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def get_job(db: Session, workspace_id: str, job_id: str) -> SkillLabJob:
    row = db.get(SkillLabJob, job_id)
    if row is None or row.workspace_id != workspace_id:
        raise AppError("skill_lab.job_not_found", f"job '{job_id}' not found", status_code=404)
    return row


def list_jobs(db: Session, workspace_id: str, type_filter: str | None = None) -> list[dict]:
    query = (
        select(SkillLabJob)
        .where(SkillLabJob.workspace_id == workspace_id)
        .order_by(SkillLabJob.created_at.desc())
    )
    if type_filter:
        query = query.where(SkillLabJob.type == type_filter)
    return [job_out(row) for row in db.scalars(query).all()]


def _resolve_tasks_file(
    db: Session, workspace_id: str, taskset_id: str, split: str | None
) -> tuple[Path, str, str]:
    """(file path, resolved split, taskset name); split preference test→val→train."""
    row = taskset_svc.get_row(db, workspace_id, taskset_id)
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


def _enqueue(db: Session, row: SkillLabJob, command: list[str]) -> dict[str, Any]:
    position = job_queue.submit(row.id, lambda: _run_job(row.id, command))
    row.queue_position = position
    db.commit()
    return job_out(row)


def submit_eval_job(
    db: Session,
    workspace_id: str,
    workspace: Any,
    *,
    skill_source: dict[str, Any],
    taskset_id: str,
    split: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runner.require_worker(workspace)
    merged_params = runner.clamp_params(params)
    tasks_file, resolved_split, taskset_name = _resolve_tasks_file(
        db, workspace_id, taskset_id, split
    )

    row = SkillLabJob(
        workspace_id=workspace_id,
        type="eval",
        taskset_id=taskset_id,
        taskset_name=taskset_name,
        split=resolved_split,
        params=merged_params,
    )
    db.add(row)
    db.flush()
    directory = artifacts.job_dir(row.id)
    directory.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    try:
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
    )
    return _enqueue(db, row, command)


def submit_train_job(
    db: Session,
    workspace_id: str,
    workspace: Any,
    *,
    skill_source: dict[str, Any],
    taskset_id: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Training uses the whole task set (train/val[/test] splits or an auto
    ratio-split for single-mode sets); there is no per-job split selection."""
    runner.require_worker(workspace)
    merged_params = runner.clamp_train_params(params)
    ts_row = taskset_svc.get_row(db, workspace_id, taskset_id)
    taskset_dir = taskset_svc.taskset_dir(ts_row.id)

    row = SkillLabJob(
        workspace_id=workspace_id,
        type="train",
        taskset_id=taskset_id,
        taskset_name=ts_row.name,
        split="",
        params=merged_params,
    )
    db.add(row)
    db.flush()
    directory = artifacts.job_dir(row.id)
    directory.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    try:
        skill_dir, resolved_source = _materialize_skill(
            workspace, skill_source, directory, log_lines
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
        )
    except BaseException:
        runner.remove_job_dir(row.id)
        raise

    row.skill_source = resolved_source
    db.commit()
    if log_lines:
        (directory / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    command = runner.build_train_command(config_file=config_file, out_dir=directory / "out")
    return _enqueue(db, row, command)


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
    before = registry_console.console_get(workspace, record_id)
    status_before = str(before.get("status") or "")
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


def _run_job(job_id: str, command: list[str]) -> None:
    """Worker-thread body: spawn → wait → terminal transition. Never raises."""
    db = SessionLocal()
    try:
        row = db.get(SkillLabJob, job_id)
        if row is None or row.status != "queued":
            return  # cancelled while queued, or swept
        workspace_id = row.workspace_id or ""
    finally:
        db.close()

    try:
        workspace = context_for_workspace(workspace_id)
        env = runner.build_job_env(workspace)
        _set_status(job_id, status="running", started_at=datetime.now(UTC), queue_position=0)
        log_path = artifacts.job_dir(job_id) / "log.txt"
        with open(log_path, "ab") as log_file:
            proc = runner.spawn(command, env, log_file)
            with _procs_lock:
                _procs[job_id] = proc
            # A cancel between the `running` flip above and the registration
            # just above finds no process to kill, so it would otherwise let a
            # full-price run finish and only then report "cancelled".
            if _cancel_requested(job_id):
                kill_process_group(proc)
            returncode = proc.wait()
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
