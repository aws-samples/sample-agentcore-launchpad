"""Workspace files of a coding project: thin proxies over the runtime actions.

The runtime's path guard is authoritative (it resolves real paths under the
project, refuses symlink escapes and ``.git``); this layer refuses an obviously
bad path — absolute, ``..``, NUL, ``.git`` — before any runtime call, and shapes
the answers to the console contract (parent design §7.3, console addenda 7–9).
A ``turn`` view of a project that has not run a turn yet is empty, not an error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

from . import runtime_client
from .models import PROJECT_RUNNING, CodingProject
from .runtime_client import CodingRuntimeError

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PATH_CHARS = 4096
MAX_TREE_DEPTH = 4


def _path_invalid(message: str) -> AppError:
    return AppError("coding.path_invalid", message, status_code=422)


def clean_path(path: str | None, *, allow_root: bool = False) -> str:
    """The runtime's ``cleanRelPath`` rules, checked before any call."""
    if path in (None, "", "."):
        if allow_root:
            return "."
        raise _path_invalid("a file path is required")
    if len(path) > MAX_PATH_CHARS or "\0" in path:
        raise _path_invalid("invalid path")
    unified = path.replace("\\", "/")
    if unified.startswith("/") or (len(unified) >= 2 and unified[1] == ":"):
        raise _path_invalid("absolute paths are not allowed")
    parts = [part for part in unified.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise _path_invalid("'..' is not allowed in a path")
    if not parts:
        if allow_root:
            return "."
        raise _path_invalid("a file path is required")
    if parts[0] == ".git":
        raise _path_invalid("the .git directory is not accessible")
    return "/".join(parts)


def tree(ctx: WorkspaceContext, project: CodingProject, path: str | None,
         depth: int) -> dict[str, Any]:
    rel = clean_path(path, allow_root=True)
    try:
        result = runtime_client.project_call(
            ctx, project, "tree", path=rel, depth=max(1, min(int(depth), MAX_TREE_DEPTH))
        ) or {}
    except CodingRuntimeError as exc:
        if exc.code == "not_found":  # the directory is gone: an empty listing
            return {"path": rel, "entries": [], "truncated": False}
        raise runtime_client.api_error(exc) from exc
    entries = []
    for entry in result.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        is_dir = bool(entry.get("dir"))
        item: dict[str, Any] = {
            "path": str(entry["path"]),
            "dir": is_dir,
            "size": None if is_dir else entry.get("size"),
        }
        if entry.get("symlink"):
            item["symlink"] = True
        entries.append(item)
    return {"path": rel, "entries": entries, "truncated": bool(result.get("truncated"))}


def _absent(rel: str) -> dict[str, Any]:
    return {"path": rel, "exists": False, "content": None, "sha256": None, "size": None,
            "binary": False}


def read_file(ctx: WorkspaceContext, project: CodingProject, path: str | None,
              rev: str | None) -> dict[str, Any]:
    rel = clean_path(path)
    payload: dict[str, Any] = {"path": rel}
    if rev:
        payload["rev"] = rev
    try:
        result = runtime_client.project_call(ctx, project, "read_file", **payload) or {}
    except CodingRuntimeError as exc:
        if exc.code == "no_turn" and rev == "turn":
            return _absent(rel)
        raise runtime_client.api_error(exc) from exc
    if not result.get("exists"):
        return _absent(rel)
    binary = bool(result.get("binary"))
    return {
        "path": str(result.get("path") or rel),
        "exists": True,
        "content": None if binary else result.get("content"),
        "sha256": result.get("sha256"),
        "size": result.get("size"),
        "binary": binary,
    }


def write_file(db: Session, ctx: WorkspaceContext, project: CodingProject, *, path: str,
               content: str, expected: dict[str, Any]) -> dict[str, Any]:
    """Save one file. ``expected`` is ``{"expected_sha256": hash | None}`` when the
    client sent the field (a hash = the file must still have it; None = it must
    not exist yet) and ``{}`` for an unconditional overwrite. Refused while a turn
    runs; a stale save is ``409 coding.file_conflict`` with the current hash."""
    rel = clean_path(path)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise AppError("coding.file_too_large", "the file is larger than 2 MB",
                       {"max_bytes": MAX_FILE_BYTES}, status_code=413)
    if project.status == PROJECT_RUNNING:
        raise AppError("coding.turn_running", "a turn is running in this project — "
                       "files are read-only until it ends", status_code=409)
    result = runtime_client.run(ctx, project, "write_file", path=rel, content=content,
                                **expected) or {}
    stamp = datetime.now(UTC)
    db.execute(
        update(CodingProject)
        .where(CodingProject.id == project.id)
        .values(dirty_at=stamp, updated_at=stamp)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return {"path": str(result.get("path") or rel), "sha256": result.get("sha256"),
            "size": result.get("size")}


def changes(ctx: WorkspaceContext, project: CodingProject, scope: str) -> dict[str, Any]:
    try:
        result = runtime_client.project_call(ctx, project, "changes", scope=scope) or {}
    except CodingRuntimeError as exc:
        if exc.code == "no_turn":
            return {"scope": scope, "changes": []}
        raise runtime_client.api_error(exc) from exc
    out = []
    for change in result.get("changes") or []:
        if not isinstance(change, dict) or not change.get("path"):
            continue
        item = {"path": str(change["path"]), "status": str(change.get("status") or "M")}
        if change.get("old_path"):
            item["old_path"] = str(change["old_path"])
        out.append(item)
    return {"scope": scope, "changes": out}


def diff(ctx: WorkspaceContext, project: CodingProject, scope: str) -> dict[str, Any]:
    try:
        result = runtime_client.project_call(ctx, project, "diff", scope=scope) or {}
    except CodingRuntimeError as exc:
        if exc.code == "no_turn":
            return {"scope": scope, "diff": "", "truncated": False}
        raise runtime_client.api_error(exc) from exc
    return {"scope": scope, "diff": str(result.get("diff") or ""),
            "truncated": bool(result.get("truncated"))}
