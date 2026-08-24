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
import threading
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, make_transient

from app.core.config import DATA_DIR, get_settings
from app.core.errors import AppError
from app.skill_lab import task_assets
from app.skill_lab.models import SkillLabTaskset
from app.skill_lab.worker_build import VENDOR_ROOT

TASKSETS_DIR = DATA_DIR / "skill-lab" / "tasksets"
VALIDATOR_SCRIPT = VENDOR_ROOT / "scripts" / "validate_tasks.py"

SPLIT_KEYS = ("train", "val", "test")
SINGLE_KEY = "tasks"
MAX_TASKS_PER_SPLIT = 2000
PREVIEW_CAP = 20
_VALIDATOR_TIMEOUT_S = 60.0

# File trees and their shared ``<id>.old`` rollback names are process-local
# resources. This application intentionally supports one backend process on one
# host for its SQLite/file-backed Skill Lab, so a keyed in-process RLock is the
# appropriate serialization boundary. A multi-process deployment must replace
# this with an inter-process/file lock before sharing DATA_DIR. RLock permits job
# submit to hold the boundary across row commit while snapshot_taskset nests it.
_TASKSET_LOCKS: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)


@contextmanager
def taskset_operation(taskset_id: str) -> Iterator[None]:
    """Serialize filesystem mutation/snapshot work for one task set in this process."""
    with _TASKSET_LOCKS[taskset_id]:
        yield


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


def _validate_files(
    files: dict[str, Path], validator_python: str | None, assets_dir: Path | None = None
) -> None:
    """Run the vendored validator on every split file; raise 422 on failures."""
    python = _validator_python(validator_python)
    ordered = list(files.items())
    command = [python, str(VALIDATOR_SCRIPT)]
    if assets_dir is not None:
        command += ["--assets-dir", str(assets_dir)]
    command += [str(path) for _, path in ordered]
    try:
        proc = subprocess.run(
            command,
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


def _owned_descriptors(current_live: Path | None) -> set[tuple[str, str, str, int]]:
    """Canonical descriptors already owned by the task set being replaced."""
    owned: set[tuple[str, str, str, int]] = set()
    if current_live is None:
        return owned
    for task_file in current_live.glob("*.json"):
        try:
            tasks = json.loads(task_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for task in tasks if isinstance(tasks, list) else ():
            files = task.get("files") if isinstance(task, dict) else None
            for value in files.values() if isinstance(files, dict) else ():
                if not isinstance(value, dict) or "asset" not in value:
                    continue
                try:
                    digest = task_assets.digest_from_descriptor(value)
                    descriptor = (
                        digest,
                        str(value["name"]),
                        str(value["media_type"]),
                        int(value["size"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                owned.add(descriptor)
    return owned


def _canonicalize_assets(
    tasks_by_split: dict[str, Any],
    keys: list[str],
    workspace_id: str,
    staging: Path,
    current_live: Path | None,
) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    """Resolve descriptors into a complete staged assets tree without mutating input."""
    canonical = json.loads(json.dumps(tasks_by_split))
    assets_dir = staging / task_assets.ASSETS_DIRNAME
    references = 0
    unique_sizes: dict[str, int] = {}
    consumed: list[tuple[Path, str]] = []
    owned = _owned_descriptors(current_live)
    for split in keys:
        for index, task in enumerate(canonical[split]):
            if not isinstance(task, dict):
                continue  # vendored validator owns the row-level shape message
            files = task.get("files")
            if files is None:
                continue
            if not isinstance(files, dict):
                continue
            binary_values = [value for value in files.values() if not isinstance(value, str)]
            if len(binary_values) > task_assets.MAX_FILES_PER_TASK:
                raise _invalid([{"split": split, "message": f"item #{index}: too many files"}])
            # These constraints belong to new binary descriptors only. Legacy
            # inline text paths continue through the vendored loader's historical
            # path rules (including case-distinct names and >32 text files).
            seen_binary_paths: set[str] = set()
            task_bytes = 0
            for destination, value in list(files.items()):
                if isinstance(value, str):
                    continue
                safe = task_assets.validate_destination(destination)
                folded = safe.casefold()
                if folded in seen_binary_paths:
                    raise task_assets._error(
                        "asset_duplicate_path", f"duplicate task asset destination {destination!r}"
                    )
                seen_binary_paths.add(folded)
                if not isinstance(value, dict):
                    raise task_assets._error(
                        "asset_descriptor_invalid", f"invalid task asset value for {destination!r}"
                    )
                source: Path
                if "staged_asset" in value:
                    record, source, stage_dir = task_assets.resolve_staged(
                        workspace_id, str(value.get("staged_asset") or "")
                    )
                    descriptor = task_assets.stable_descriptor(record)
                    consumed.append((stage_dir, str(record["staged_asset"])))
                    digest = str(record["sha256"])
                else:
                    digest = task_assets.digest_from_descriptor(value)
                    if current_live is None:
                        raise task_assets._error(
                            "asset_not_owned", "stable task assets may only be kept during update"
                        )
                    descriptor = {
                        "asset": f"sha256:{digest}",
                        "name": str(value.get("name") or ""),
                        "media_type": str(value.get("media_type") or ""),
                        "size": int(value.get("size", -1)),
                    }
                    identity = (
                        digest,
                        descriptor["name"],
                        descriptor["media_type"],
                        descriptor["size"],
                    )
                    if identity not in owned:
                        raise task_assets._error(
                            "asset_not_owned",
                            "stable task asset descriptor is not owned by this task set",
                        )
                    source = current_live / task_assets.ASSETS_DIRNAME / digest
                    task_assets._verify_blob(
                        source,
                        {"size": descriptor["size"], "sha256": digest},
                    )
                size = int(descriptor["size"])
                references += 1
                task_bytes += size
                if task_bytes > task_assets.MAX_TASK_BYTES:
                    raise task_assets._error(
                        "asset_limit_exceeded", "task binary assets exceed 100 MiB"
                    )
                unique_sizes.setdefault(digest, size)
                assets_dir.mkdir(parents=True, exist_ok=True)
                target = assets_dir / digest
                if not target.exists():
                    shutil.copyfile(source, target)
                files[destination] = descriptor
    if references > task_assets.MAX_TASKSET_REFERENCES:
        raise task_assets._error("asset_limit_exceeded", "task set has too many binary assets")
    if sum(unique_sizes.values()) > task_assets.MAX_TASKSET_UNIQUE_BYTES:
        raise task_assets._error("asset_limit_exceeded", "task set binary assets exceed 200 MiB")
    return canonical, consumed


def _stage_validated(
    tasks_by_split: dict[str, Any],
    keys: list[str],
    validator_python: str | None,
    run_validator: bool = True,
    *,
    workspace_id: str = "",
    current_live: Path | None = None,
) -> tuple[Path, dict[str, Any], list[tuple[Path, str]]]:
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
        canonical, consumed = _canonicalize_assets(
            tasks_by_split, keys, workspace_id, staging, current_live
        )
        files: dict[str, Path] = {}
        for split in keys:
            path = staging / ("tasks.json" if split == SINGLE_KEY else f"{split}.json")
            path.write_text(
                json.dumps(canonical[split], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            files[split] = path
        if run_validator:
            assets_dir = staging / task_assets.ASSETS_DIRNAME
            _validate_files(files, validator_python, assets_dir if assets_dir.exists() else None)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging, canonical, consumed


def _swap_in(staging: Path, live: Path) -> Path | None:
    """Replace the live tree while retaining a rollback copy through DB commit."""
    backup = live.with_name(live.name + ".old")
    had_live = live.exists()
    shutil.rmtree(backup, ignore_errors=True)
    try:
        if had_live:
            live.rename(backup)
        staging.rename(live)
    except OSError:
        if had_live and backup.exists() and not live.exists():
            backup.rename(live)
        raise
    return backup if had_live else None


def _restore_swap(live: Path, backup: Path | None) -> None:
    shutil.rmtree(live, ignore_errors=True)
    if backup is not None and backup.exists():
        backup.rename(live)


def _finish_swap(backup: Path | None) -> None:
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _counts(tasks_by_split: dict[str, Any], keys: list[str]) -> dict[str, int]:
    return {split: len(tasks_by_split[split]) for split in keys}


def taskset_info(row: SkillLabTaskset) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "mode": row.mode,
        "sample": bool(row.sample),
        "counts": row.counts,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _reject_sample_write(row: SkillLabTaskset) -> None:
    """Built-in samples are read-only (upstream semantics): runnable by eval and
    train, but never edited, expanded, or deleted — a user who wants a variant
    creates their own set. Bootstrap re-seeds missing samples, so mutability
    would only create drift between installs."""
    if row.sample:
        raise AppError(
            "skill_lab.sample_readonly",
            f"task set '{row.name}' is a built-in sample and is read-only",
            status_code=409,
        )


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
    sample: bool = False,
    run_validator: bool = True,
) -> dict[str, Any]:
    """`sample`/`run_validator` are seeding-only knobs (app/skill_lab/samples.py):
    the router never passes them. Skipping the validator subprocess is safe only
    for the vendored demo files — a hermetic test runs the real validator over
    them, and seeding must not depend on the skill-lab venv existing yet."""
    name = (name or "").strip()
    if not name:
        raise _invalid([{"split": "name", "message": "name is required"}])
    keys = _split_keys_for(mode, tasks_by_split)
    staging, canonical, consumed = _stage_validated(
        tasks_by_split,
        keys,
        validator_python,
        run_validator,
        workspace_id=workspace_id,
    )
    try:
        row = SkillLabTaskset(
            workspace_id=workspace_id,
            name=name,
            description=description or "",
            mode=mode,
            sample=sample,
            counts=_counts(canonical, keys),
        )
        db.add(row)
        db.flush()  # allocate the id before the content claims its directory
        live = taskset_dir(row.id)
        backup = _swap_in(staging, live)
    except BaseException:
        # closing the session rolls the flushed row back; the dir is ours to undo
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        db.commit()
    except BaseException:
        _restore_swap(live, backup)
        raise
    _finish_swap(backup)
    task_assets.consume_staged(consumed)
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


def snapshot_taskset(taskset_id: str, destination: Path) -> Path:
    """Copy and integrity-check immutable job inputs under the task-set lock."""
    with taskset_operation(taskset_id):
        source = taskset_dir(taskset_id)
        if not source.is_dir():
            raise _not_found(taskset_id)
        if destination.exists():
            raise AppError(
                "skill_lab.snapshot_exists",
                f"job input snapshot already exists at {destination}",
                status_code=409,
            )
        staging = destination.with_name(f".{destination.name}-{uuid.uuid4().hex[:8]}.staging")
        try:
            shutil.copytree(source, staging)
            assets = staging / task_assets.ASSETS_DIRNAME
            for task_file in staging.glob("*.json"):
                tasks = json.loads(task_file.read_text(encoding="utf-8"))
                for task in tasks:
                    files = task.get("files") if isinstance(task, dict) else None
                    for value in files.values() if isinstance(files, dict) else ():
                        if isinstance(value, dict):
                            digest = task_assets.digest_from_descriptor(value)
                            task_assets._verify_blob(
                                assets / digest,
                                {"size": int(value.get("size", -1)), "sha256": digest},
                            )
            staging.rename(destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return destination


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
    # Lock includes reading the current tree, staging from owned descriptors,
    # the shared .old swap, ledger commit, and staged-token consumption.
    with taskset_operation(taskset_id):
        row = get_row(db, workspace_id, taskset_id)
        _reject_sample_write(row)
        keys = _split_keys_for(row.mode, tasks_by_split)  # mode is immutable
        if name is not None and not name.strip():
            raise _invalid([{"split": "name", "message": "name is required"}])
        live = taskset_dir(row.id)
        staging, canonical, consumed = _stage_validated(
            tasks_by_split,
            keys,
            validator_python,
            workspace_id=workspace_id,
            current_live=live,
        )
        try:
            backup = _swap_in(staging, live)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if name is not None:
            row.name = name.strip()
        if description is not None:
            row.description = description
        row.counts = _counts(canonical, keys)
        row.updated_at = datetime.now(UTC)
        try:
            db.commit()
        except BaseException:
            _restore_swap(live, backup)
            raise
        _finish_swap(backup)
        task_assets.consume_staged(consumed)
        return taskset_info(row)


def delete_taskset(db: Session, workspace_id: str, taskset_id: str) -> None:
    """Delete ledger + tree, compensating a failed post-commit tree cleanup.

    Renaming first removes the canonical tree atomically. The ledger deletion is
    committed before recursive cleanup; if cleanup fails, the same row and tree
    are restored and a stable error is returned instead of reporting success
    while leaking an unowned task-set directory.
    """
    with taskset_operation(taskset_id):
        row = get_row(db, workspace_id, taskset_id)
        _reject_sample_write(row)
        from app.skill_lab.jobs import taskset_in_use

        if taskset_in_use(db, workspace_id, taskset_id):
            raise AppError(
                "skill_lab.taskset_in_use",
                "this task set is referenced by evaluation/training jobs — delete those jobs first",
                status_code=409,
            )
        directory = taskset_dir(row.id)
        tombstone = directory.with_name(f".deleting-{row.id}-{uuid.uuid4().hex[:8]}")
        if directory.exists():
            try:
                directory.rename(tombstone)
            except OSError as exc:
                raise AppError(
                    "skill_lab.taskset_cleanup_failed",
                    "task set assets could not be prepared for deletion; the task set was retained",
                    status_code=500,
                ) from exc
        db.delete(row)
        try:
            db.commit()
        except BaseException:
            if tombstone.exists() and not directory.exists():
                tombstone.rename(directory)
            raise
        try:
            if tombstone.exists():
                shutil.rmtree(tombstone)
        except OSError as exc:
            # Compensating transaction: retain a visible/retryable ledger row.
            make_transient(row)
            db.add(row)
            db.commit()
            if tombstone.exists() and not directory.exists():
                tombstone.rename(directory)
            raise AppError(
                "skill_lab.taskset_cleanup_failed",
                "task set asset cleanup failed; the task set was restored and can be retried",
                status_code=500,
            ) from exc
