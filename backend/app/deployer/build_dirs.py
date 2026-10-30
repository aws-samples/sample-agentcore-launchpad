"""The local build directories under ``data/builds/<agent>`` and their retention (N17-obs151).

N12-bug10 put every package assembly INSIDE the checkout (git-ignored ``data/``, owned by
whoever runs the backend — never a shared ``/tmp`` a root-run bootstrap could leave root-owned),
one directory per agent NAME, recreated on every build (``zip_runtime.build_zip`` removes and
rebuilds it; the Harness method assembles in the very same place; BYOC uses a temp dir). Nothing
ever removed them: a host that had deployed 66 agents over its life carried 15 GB of scratch,
most of it for agents deleted long ago.

Two mechanisms, both best-effort and hermetic (a plain directory tree, no AWS):

* **agent delete** — ``teardown.cleanup_residue`` runs ``remove_for_agent`` as the ``build_dir``
  residue step, reported like every other step (``deleted`` with the byte count, ``skipped``
  when there is none, ``failed`` with the reason) and never blocking the delete;
* **startup sweep** — ``sweep`` (called from ``main.create_app`` before the interrupted jobs
  are resumed, so no build is in flight) removes the directories of agents without a live
  ledger row, keeps each live agent's newest ``Settings.builds_keep_per_agent`` directories
  (today one directory per agent exists, so any value ≥ 1 keeps it and ``0`` purges the scratch
  of live agents too) and then trims the oldest directories first until the whole root is under
  ``Settings.builds_max_gb`` (``0`` = no cap). One summary line is logged — counts and bytes,
  never a path.

This module raises nothing (``tests/test_fail_early_guard.py`` classifies every ``raise`` in
``app/deployer``): every failure is a status in the result.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import DATA_DIR

logger = logging.getLogger("launchpad.builds")

#: N12-bug10: inside the checkout, git-ignored, owned by the service user.
DEFAULT_BUILD_ROOT = DATA_DIR / "builds"
#: the root in use — tests point it at a temp dir (conftest), production never changes it
BUILD_ROOT: Path = DEFAULT_BUILD_ROOT

GIB = 1024**3


#: A build-dir name is ONE safe path component. Every caller's name source is constrained
#: today (AgentSpec.name, AgentCore's agentRuntimeName, preset constants), but this module calls
#: ``rmtree`` — defence in depth (rev-r17 on cn/r17-w): anything else is refused, never joined.
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def is_safe_build_name(agent_name: str) -> bool:
    """True when ``agent_name`` is a single safe path component (no separators, no ``..``)."""
    return bool(agent_name) and agent_name not in {".", ".."} and bool(
        _SAFE_NAME.fullmatch(agent_name))


def build_dir_for(agent_name: str) -> Path:
    """The scratch dir one agent's package is assembled in (recreated on every build)."""
    return BUILD_ROOT / agent_name


def dir_bytes(path: Path) -> int:
    """Apparent size of every regular file under ``path`` (symlinks not followed)."""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 160 else text[:159] + "…"


# ---------------------------------------------------------------------------
# agent delete — one residue step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Removal:
    status: str  # deleted | skipped | failed
    detail: str
    bytes_freed: int = 0
    reason: str = ""


def remove_for_agent(agent_name: str) -> Removal:
    """Remove ``data/builds/<agent_name>`` if present. Never raises."""
    rel = f"builds/{agent_name}"
    if not is_safe_build_name(agent_name):
        return Removal("skipped", "refused: not a build-dir name")
    path = build_dir_for(agent_name)
    root = BUILD_ROOT.resolve()
    if path.resolve().parent != root:
        return Removal("skipped", "refused: not a build-dir name")
    if not path.exists():
        return Removal("skipped", f"no local build dir ({rel})")
    if path.is_symlink() or not path.is_dir():
        return Removal("skipped", f"{rel} is not a directory — left alone")
    size = dir_bytes(path)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return Removal("failed", f"{rel}: {_short(exc)}", reason=_short(exc))
    return Removal("deleted", f"{size} bytes · {rel}", bytes_freed=size)


# ---------------------------------------------------------------------------
# startup — the retention sweep
# ---------------------------------------------------------------------------


@dataclass
class SweepResult:
    scanned: int = 0
    kept: int = 0
    bytes_kept: int = 0
    orphans_deleted: int = 0
    trimmed_deleted: int = 0  # per-agent keep count exceeded
    capped_deleted: int = 0  # total cap exceeded
    bytes_freed: int = 0
    failed: list[str] = field(default_factory=list)  # "<name>: <reason>" — a name, not a path
    skipped_root: bool = False  # the root does not exist — nothing to do

    @property
    def deleted(self) -> int:
        return self.orphans_deleted + self.trimmed_deleted + self.capped_deleted

    def summary(self) -> str:
        if self.skipped_root:
            return "build dirs: no data/builds root — nothing to sweep"
        text = (
            f"build dirs: {self.scanned} scanned, {self.kept} kept ({self.bytes_kept} bytes), "
            f"{self.deleted} deleted ({self.bytes_freed} bytes freed: "
            f"{self.orphans_deleted} orphan, {self.trimmed_deleted} over the per-agent keep, "
            f"{self.capped_deleted} over the total cap)"
        )
        if self.failed:
            text += f", {len(self.failed)} failed ({'; '.join(self.failed)})"
        return text


@dataclass(frozen=True)
class _Entry:
    path: Path
    agent: str  # the agent name the directory belongs to (today: the directory's own name)
    mtime: float
    size: int


def _entries(root: Path) -> tuple[list[_Entry], list[str]]:
    out: list[_Entry] = []
    failed: list[str] = []
    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        return out, [f"{root.name}: {_short(exc)}"]
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue  # a stray file or link under the root is not ours to remove
            mtime = child.stat().st_mtime
        except OSError as exc:
            failed.append(f"{child.name}: {_short(exc)}")
            continue
        out.append(_Entry(child, child.name, mtime, dir_bytes(child)))
    return out, failed


def _rmtree(entry: _Entry, result: SweepResult) -> bool:
    try:
        shutil.rmtree(entry.path)
    except OSError as exc:
        result.failed.append(f"{entry.agent}: {_short(exc)}")
        return False
    result.bytes_freed += entry.size
    return True


def sweep(
    live_agent_names: Iterable[str],
    *,
    keep_per_agent: int,
    max_gb: float,
    root: Path | None = None,
) -> SweepResult:
    """Apply the retention rule to ``root`` (default ``BUILD_ROOT``). Pure directory work.

    Order: orphans (no live agent of that name) → per-agent keep (newest ``keep_per_agent``
    of each live agent's directories by mtime) → total cap (oldest first until the kept bytes
    are ≤ ``max_gb`` GiB; ``max_gb <= 0`` = no cap)."""
    root = root or BUILD_ROOT
    result = SweepResult()
    if not root.is_dir():
        result.skipped_root = True
        return result
    live = set(live_agent_names)
    entries, failed = _entries(root)
    result.failed.extend(failed)
    result.scanned = len(entries)

    kept: list[_Entry] = []
    by_agent: dict[str, list[_Entry]] = {}
    for entry in entries:
        if entry.agent not in live:
            if _rmtree(entry, result):
                result.orphans_deleted += 1
            continue
        by_agent.setdefault(entry.agent, []).append(entry)
    keep_n = max(0, int(keep_per_agent))
    for group in by_agent.values():
        group.sort(key=lambda e: e.mtime, reverse=True)  # newest first
        for i, entry in enumerate(group):
            if i < keep_n:
                kept.append(entry)
            elif _rmtree(entry, result):
                result.trimmed_deleted += 1

    cap_bytes = int(max_gb * GIB) if max_gb and max_gb > 0 else None
    kept.sort(key=lambda e: e.mtime)  # oldest first
    total = sum(e.size for e in kept)
    while cap_bytes is not None and total > cap_bytes and kept:
        oldest = kept.pop(0)
        total -= oldest.size
        if _rmtree(oldest, result):
            result.capped_deleted += 1
    result.kept = len(kept)
    result.bytes_kept = total
    return result


def live_agent_names() -> set[str]:
    """Every agent name with a non-deleted ledger row, across all workspaces (a deleted
    agent keeps its row as ``status = deleted`` so its name can be reused — that row does
    NOT keep its scratch alive)."""
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        rows = db.execute(select(Agent.name).where(Agent.status != "deleted")).scalars()
        return {str(name) for name in rows if name}
    finally:
        db.close()


def startup_sweep(settings: Any) -> SweepResult:
    """The startup hook: the retention sweep against the live ledger, logged in one line.
    Never raises — a failure to read the ledger is logged and leaves every directory alone."""
    try:
        names = live_agent_names()
    except Exception as exc:  # noqa: BLE001 — a startup sweep must never stop the app
        result = SweepResult()
        result.failed.append(f"ledger: {_short(exc)}")
        logger.warning("build dirs: sweep skipped — could not list live agents (%s)", _short(exc))
        return result
    result = sweep(
        names,
        keep_per_agent=int(getattr(settings, "builds_keep_per_agent", 2)),
        max_gb=float(getattr(settings, "builds_max_gb", 10.0)),
    )
    (logger.warning if result.failed else logger.info)("%s", result.summary())
    return result
