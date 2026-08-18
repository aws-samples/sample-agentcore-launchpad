"""Task-set service: skillopt-native artifacts + ledger metadata.

Content is validated by the vendored validator subprocess
(vendor/skillopt/scripts/validate_tasks.py → the same load_tasks the CLIs use),
so API acceptance and evaluate_skill.py/train.py acceptance cannot drift. The
backend process itself never imports vendored code (foundation boundary rule).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import DATA_DIR, get_settings
from app.core.errors import AppError
from app.skill_lab.models import SkillLabTaskset
from app.skill_lab.worker_build import VENDOR_ROOT

TASKSETS_DIR = DATA_DIR / "skill-lab" / "tasksets"
VALIDATOR_SCRIPT = VENDOR_ROOT / "scripts" / "validate_tasks.py"

SPLIT_KEYS = ("train", "val", "test")
SINGLE_KEY = "tasks"
MAX_TASKS_PER_SPLIT = 2000
PREVIEW_CAP = 20
_VALIDATOR_TIMEOUT_S = 60.0


def _not_found(taskset_id: str) -> AppError:
    return AppError(
        "skill_lab.taskset_not_found",
        f"task set '{taskset_id}' not found",
        status_code=404,
    )


def _invalid(detail: list[dict[str, str]]) -> AppError:
    return AppError(
        "skill_lab.taskset_invalid",
        "; ".join(f"{d['split']}: {d['message']}" for d in detail) or "invalid task set",
        detail=detail,
        status_code=422,
    )


def taskset_dir(taskset_id: str) -> Path:
    if not taskset_id or any(part in taskset_id for part in ("/", "\\", "..")):
        raise _not_found(taskset_id)
    return TASKSETS_DIR / taskset_id


def _split_keys_for(mode: str, tasks_by_split: dict[str, Any]) -> list[str]:
    """The split names this write must carry, in canonical order."""
    keys = set(tasks_by_split)
    if mode == "single":
        if keys != {SINGLE_KEY}:
            raise _invalid(
                [{"split": "tasks", "message": "single mode takes exactly one 'tasks' list"}]
            )
        return [SINGLE_KEY]
    if mode == "split":
        unknown = keys - set(SPLIT_KEYS)
        if unknown:
            raise _invalid(
                [
                    {"split": s, "message": "unknown split (expected train/val/test)"}
                    for s in sorted(unknown)
                ]
            )
        if not {"train", "val"} <= keys:
            raise _invalid(
                [{"split": "train/val", "message": "split mode requires both train and val"}]
            )
        return [k for k in SPLIT_KEYS if k in keys]
    raise _invalid([{"split": "mode", "message": f"unknown mode {mode!r} (single|split)"}])


def _check_shape(split: str, tasks: Any) -> None:
    if not isinstance(tasks, list):
        raise _invalid([{"split": split, "message": "tasks must be a JSON array"}])
    if not tasks:
        raise _invalid([{"split": split, "message": "at least one task is required"}])
    if len(tasks) > MAX_TASKS_PER_SPLIT:
        raise _invalid(
            [{"split": split, "message": f"too many tasks ({len(tasks)} > {MAX_TASKS_PER_SPLIT})"}]
        )


def _validator_crashed(reason: str) -> AppError:
    return AppError(
        "skill_lab.validator_error",
        f"task validator failed: {reason}",
        status_code=500,
    )


def _validator_python(override: str | None) -> str:
    python = override or get_settings().skill_lab_python
    if not Path(python).exists():
        raise AppError(
            "skill_lab.not_provisioned",
            f"skill-lab interpreter missing ({python}) — run `make bootstrap`",
            status_code=503,
        )
    return python


def _validate_files(files: dict[str, Path], validator_python: str | None) -> None:
    """Run the vendored validator on every split file; raise 422 on failures."""
    python = _validator_python(validator_python)
    ordered = list(files.items())
    try:
        proc = subprocess.run(
            [python, str(VALIDATOR_SCRIPT), *[str(path) for _, path in ordered]],
            capture_output=True,
            text=True,
            timeout=_VALIDATOR_TIMEOUT_S,
            env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
        )
    except subprocess.TimeoutExpired:
        raise _validator_crashed(f"no answer within {_VALIDATOR_TIMEOUT_S:.0f}s") from None
    except OSError as exc:  # interpreter vanished between the probe and the run
        raise _validator_crashed(str(exc)) from exc
    if proc.returncode != 0:
        raise _validator_crashed(f"exit {proc.returncode}: {proc.stderr[-500:]}")
    # The protocol is one JSON object on stdout; anything else means the vendored
    # import chain wrote to stdout, which is a crash for our purposes, not a
    # verdict we may guess at.
    try:
        results = json.loads(proc.stdout)["results"]
        if not isinstance(results, list) or len(results) != len(ordered):
            raise ValueError("one result per submitted file expected")
        verdicts = [(bool(result["ok"]), str(result["error"])) for result in results]
    except (ValueError, TypeError, KeyError) as exc:
        raise _validator_crashed(f"unreadable output ({exc}): {proc.stdout[-500:]}") from exc
    failures = [
        {"split": split, "message": message}
        for (split, _), (ok, message) in zip(ordered, verdicts, strict=True)
        if not ok
    ]
    if failures:
        raise _invalid(failures)


def _stage_validated(
    tasks_by_split: dict[str, Any], keys: list[str], validator_python: str | None
) -> Path:
    """Write the split files to a staging dir and validate them there.

    Nothing outside the staging dir is touched, and every failure path — shape
    check, write error, validator verdict, validator crash — removes it, so a
    refused write leaves no debris behind (R3).
    """
    for split in keys:  # cheap shape gate first: no staging dir for an obvious reject
        _check_shape(split, tasks_by_split[split])
    staging = TASKSETS_DIR / f".staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True)
    try:
        files: dict[str, Path] = {}
        for split in keys:
            path = staging / ("tasks.json" if split == SINGLE_KEY else f"{split}.json")
            path.write_text(
                json.dumps(tasks_by_split[split], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            files[split] = path
        _validate_files(files, validator_python)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _swap_in(staging: Path, live: Path) -> None:
    """Atomic-ish replace: old content is restored if the swap fails midway."""
    backup = live.with_name(live.name + ".old")
    had_live = live.exists()
    # A leftover backup is debris, never state anything reads: failing to remove
    # it must not fail the write (the rename below refuses a non-empty target
    # anyway, which lands in the restore path).
    shutil.rmtree(backup, ignore_errors=True)
    try:
        if had_live:
            live.rename(backup)
        staging.rename(live)
    except OSError:
        if had_live and backup.exists() and not live.exists():
            backup.rename(live)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _counts(tasks_by_split: dict[str, Any], keys: list[str]) -> dict[str, int]:
    return {split: len(tasks_by_split[split]) for split in keys}


def taskset_info(row: SkillLabTaskset) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "mode": row.mode,
        "counts": row.counts,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_tasksets(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(SkillLabTaskset)
        .where(SkillLabTaskset.workspace_id == workspace_id)
        .order_by(SkillLabTaskset.created_at.desc())
    ).all()
    return [taskset_info(row) for row in rows]


def get_row(db: Session, workspace_id: str, taskset_id: str) -> SkillLabTaskset:
    row = db.get(SkillLabTaskset, taskset_id)
    if row is None or row.workspace_id != workspace_id:
        raise _not_found(taskset_id)
    return row


def create_taskset(
    db: Session,
    workspace_id: str,
    *,
    name: str,
    description: str = "",
    mode: str,
    tasks_by_split: dict[str, Any],
    validator_python: str | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise _invalid([{"split": "name", "message": "name is required"}])
    keys = _split_keys_for(mode, tasks_by_split)
    staging = _stage_validated(tasks_by_split, keys, validator_python)
    try:
        row = SkillLabTaskset(
            workspace_id=workspace_id,
            name=name,
            description=description or "",
            mode=mode,
            counts=_counts(tasks_by_split, keys),
        )
        db.add(row)
        db.flush()  # allocate the id before the content claims its directory
        _swap_in(staging, taskset_dir(row.id))
    except BaseException:
        # closing the session rolls the flushed row back; the dir is ours to undo
        shutil.rmtree(staging, ignore_errors=True)
        raise
    db.commit()
    return taskset_info(row)


def read_taskset(
    db: Session, workspace_id: str, taskset_id: str, *, full: bool = False
) -> dict[str, Any]:
    row = get_row(db, workspace_id, taskset_id)
    directory = taskset_dir(row.id)
    tasks_by_split: dict[str, Any] = {}
    truncated = False
    keys = [SINGLE_KEY] if row.mode == "single" else list(SPLIT_KEYS)
    for split in keys:
        path = directory / ("tasks.json" if split == SINGLE_KEY else f"{split}.json")
        if not path.exists():
            continue
        tasks = json.loads(path.read_text(encoding="utf-8"))
        if not full and len(tasks) > PREVIEW_CAP:
            tasks = tasks[:PREVIEW_CAP]
            truncated = True
        tasks_by_split[split] = tasks
    return {"info": taskset_info(row), "tasks_by_split": tasks_by_split, "truncated": truncated}


def update_taskset(
    db: Session,
    workspace_id: str,
    taskset_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    tasks_by_split: dict[str, Any],
    validator_python: str | None = None,
) -> dict[str, Any]:
    row = get_row(db, workspace_id, taskset_id)
    keys = _split_keys_for(row.mode, tasks_by_split)  # mode is immutable
    if name is not None and not name.strip():
        raise _invalid([{"split": "name", "message": "name is required"}])
    staging = _stage_validated(tasks_by_split, keys, validator_python)
    try:
        _swap_in(staging, taskset_dir(row.id))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if name is not None:
        row.name = name.strip()
    if description is not None:
        row.description = description
    row.counts = _counts(tasks_by_split, keys)
    row.updated_at = datetime.now(UTC)
    db.commit()
    return taskset_info(row)


def delete_taskset(db: Session, workspace_id: str, taskset_id: str) -> None:
    row = get_row(db, workspace_id, taskset_id)
    from app.skill_lab.jobs import taskset_in_use

    if taskset_in_use(db, workspace_id, taskset_id):
        raise AppError(
            "skill_lab.taskset_in_use",
            "this task set is referenced by evaluation/training jobs — "
            "delete those jobs first",
            status_code=409,
        )
    directory = taskset_dir(row.id)
    db.delete(row)
    db.commit()
    shutil.rmtree(directory, ignore_errors=True)
