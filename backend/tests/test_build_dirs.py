"""N17-obs151: the `data/builds/<agent>` scratch is purged on agent delete and swept at startup.

Hermetic: a temp root (the conftest points `build_dirs.BUILD_ROOT` at one), no AWS. Live: t1
carried 15 GB / 66 directories, most of them for agents deleted long ago — nothing ever removed
them (the deployer kept every build dir for inspection; agent delete removed AWS resources and
ledger rows only).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.db import SessionLocal
from app.deployer import build_dirs
from app.main import create_app
from app.models.ledger import Agent


def _dir(root: Path, name: str, *, size: int, age_s: float = 0.0) -> Path:
    path = root / name
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "main.py").write_bytes(b"m" * (size // 2))
    (path / "deployment_package.zip").write_bytes(b"z" * (size - size // 2))
    if age_s:
        stamp = 1_700_000_000.0 - age_s
        os.utime(path, (stamp, stamp))
    return path


# ---------------------------------------------------------------------------
# the delete step
# ---------------------------------------------------------------------------


def test_remove_for_agent_deletes_only_that_agents_dir_and_reports_the_bytes():
    root = build_dirs.BUILD_ROOT
    mine = _dir(root, "hr-helpdesk", size=10)
    twin = _dir(root, "hr-helpdesk-2", size=4)

    removal = build_dirs.remove_for_agent("hr-helpdesk")

    assert removal.status == "deleted" and removal.bytes_freed == 10
    assert removal.detail == "10 bytes · builds/hr-helpdesk"
    assert not mine.exists() and twin.is_dir()


def test_remove_for_agent_is_skipped_when_there_is_nothing_and_for_a_non_directory():
    assert build_dirs.remove_for_agent("never-built").status == "skipped"
    assert build_dirs.remove_for_agent("never-built").detail == (
        "no local build dir (builds/never-built)")
    assert build_dirs.remove_for_agent("").status == "skipped"
    build_dirs.BUILD_ROOT.mkdir(parents=True)
    (build_dirs.BUILD_ROOT / "stray").write_text("not a dir")
    removal = build_dirs.remove_for_agent("stray")
    assert removal.status == "skipped" and "not a directory" in removal.detail
    assert (build_dirs.BUILD_ROOT / "stray").exists()


def test_remove_for_agent_reports_a_refused_delete_as_failed_with_the_reason(monkeypatch):
    _dir(build_dirs.BUILD_ROOT, "locked", size=2)

    def refuse(_path):
        raise PermissionError(13, "Permission denied", str(_path))

    monkeypatch.setattr(build_dirs.shutil, "rmtree", refuse)
    removal = build_dirs.remove_for_agent("locked")
    assert removal.status == "failed"
    assert removal.reason.startswith("PermissionError:")
    assert removal.detail.startswith("builds/locked: PermissionError:")


# ---------------------------------------------------------------------------
# the retention sweep
# ---------------------------------------------------------------------------


def test_sweep_removes_orphans_and_keeps_live_agents_dirs():
    root = build_dirs.BUILD_ROOT
    live_a = _dir(root, "alive-a", size=100)
    live_b = _dir(root, "alive-b", size=50)
    gone = _dir(root, "deleted-long-ago", size=1000)
    (root / "README.txt").write_text("a stray file is not ours to remove")

    result = build_dirs.sweep({"alive-a", "alive-b"}, keep_per_agent=2, max_gb=10)

    assert result.scanned == 3
    assert result.orphans_deleted == 1 and result.trimmed_deleted == 0
    assert result.capped_deleted == 0
    assert result.bytes_freed == 1000 and result.bytes_kept == 150 and result.kept == 2
    assert not gone.exists() and live_a.is_dir() and live_b.is_dir()
    assert (root / "README.txt").exists()
    assert result.failed == []
    assert result.summary() == (
        "build dirs: 3 scanned, 2 kept (150 bytes), 1 deleted (1000 bytes freed: 1 orphan, "
        "0 over the per-agent keep, 0 over the total cap)")


def test_sweep_keep_zero_purges_the_scratch_of_live_agents_too():
    root = build_dirs.BUILD_ROOT
    _dir(root, "alive", size=10)
    result = build_dirs.sweep({"alive"}, keep_per_agent=0, max_gb=10)
    assert result.trimmed_deleted == 1 and result.kept == 0
    assert not (root / "alive").exists()


def test_sweep_total_cap_trims_the_oldest_first_until_under_the_cap():
    root = build_dirs.BUILD_ROOT
    gib = build_dirs.GIB
    oldest = _dir(root, "old", size=600, age_s=3000)
    middle = _dir(root, "mid", size=600, age_s=2000)
    newest = _dir(root, "new", size=600, age_s=1000)
    # a cap of 1500 bytes expressed in GiB: two 600-byte dirs fit, three do not
    result = build_dirs.sweep({"old", "mid", "new"}, keep_per_agent=2, max_gb=1500 / gib)

    assert result.capped_deleted == 1 and result.orphans_deleted == 0
    assert not oldest.exists() and middle.is_dir() and newest.is_dir()
    assert result.kept == 2 and result.bytes_kept == 1200 and result.bytes_freed == 600


def test_sweep_cap_zero_means_no_cap_and_a_missing_root_is_a_no_op():
    root = build_dirs.BUILD_ROOT
    _dir(root, "big", size=5000)
    result = build_dirs.sweep({"big"}, keep_per_agent=1, max_gb=0)
    assert result.deleted == 0 and result.kept == 1
    assert (root / "big").is_dir()

    missing = build_dirs.sweep(set(), keep_per_agent=2, max_gb=10, root=root / "nope")
    assert missing.skipped_root and missing.summary().startswith("build dirs: no data/builds")


def test_sweep_records_a_refused_delete_and_goes_on(monkeypatch):
    root = build_dirs.BUILD_ROOT
    _dir(root, "stuck-orphan", size=10)
    other = _dir(root, "other-orphan", size=20)
    real_rmtree = build_dirs.shutil.rmtree

    def flaky(path, *a, **kw):
        if Path(path).name == "stuck-orphan":
            raise OSError(16, "Device or resource busy")
        real_rmtree(path, *a, **kw)

    monkeypatch.setattr(build_dirs.shutil, "rmtree", flaky)
    result = build_dirs.sweep(set(), keep_per_agent=2, max_gb=10)
    assert result.orphans_deleted == 1 and not other.exists()
    assert result.failed == ["stuck-orphan: OSError: [Errno 16] Device or resource busy"]
    assert result.bytes_freed == 20
    assert "1 failed (stuck-orphan: OSError" in result.summary()


# ---------------------------------------------------------------------------
# the startup hook — the ledger decides which names are live
# ---------------------------------------------------------------------------


def _row(name: str, status: str) -> None:
    db = SessionLocal()
    try:
        db.add(Agent(name=name, method="zip_runtime", status=status, spec={"name": name}))
        db.commit()
    finally:
        db.close()


def test_live_agent_names_excludes_deleted_rows():
    _row("alive", "active")
    _row("mid-deploy", "deploying")
    _row("broken", "failed")
    _row("gone", "deleted")
    assert build_dirs.live_agent_names() == {"alive", "mid-deploy", "broken"}


def test_startup_sweep_reads_the_ledger_and_logs_one_summary_line(caplog):
    root = build_dirs.BUILD_ROOT
    _row("alive", "active")
    _row("gone", "deleted")
    _dir(root, "alive", size=10)
    _dir(root, "gone", size=20)
    _dir(root, "never-in-the-ledger", size=30)

    with caplog.at_level(logging.INFO, logger="launchpad.builds"):
        result = build_dirs.startup_sweep(SimpleNamespace(builds_keep_per_agent=2,
                                                          builds_max_gb=10.0))

    assert result.orphans_deleted == 2 and result.kept == 1 and result.bytes_freed == 50
    assert (root / "alive").is_dir()
    lines = [r for r in caplog.records if r.name == "launchpad.builds"]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert lines[0].getMessage() == result.summary()
    assert str(root) not in lines[0].getMessage()  # counts and bytes, never a path


def test_startup_sweep_never_raises_when_the_ledger_cannot_be_read(monkeypatch, caplog):
    _dir(build_dirs.BUILD_ROOT, "untouched", size=1)

    def boom():
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(build_dirs, "live_agent_names", boom)
    with caplog.at_level(logging.WARNING, logger="launchpad.builds"):
        result = build_dirs.startup_sweep(SimpleNamespace(builds_keep_per_agent=2,
                                                          builds_max_gb=10.0))
    assert result.deleted == 0 and result.failed == ["ledger: RuntimeError: ledger locked"]
    assert (build_dirs.BUILD_ROOT / "untouched").is_dir()
    assert any("sweep skipped" in r.getMessage() for r in caplog.records)


def test_the_app_runs_the_sweep_at_startup_before_resuming_jobs(monkeypatch):
    """`create_app(resume_jobs=True)` sweeps before `resume_pending_jobs` (no package stage
    is writing under the root yet) and honours the Settings knobs."""
    from app import main as app_main

    order: list[str] = []
    seen: dict[str, object] = {}

    def fake_sweep(settings):
        order.append("sweep")
        seen["keep"] = settings.builds_keep_per_agent
        seen["cap"] = settings.builds_max_gb
        return build_dirs.SweepResult()

    monkeypatch.setattr(app_main.build_dirs, "startup_sweep", fake_sweep)
    monkeypatch.setattr(app_main, "resume_pending_jobs", lambda: order.append("resume") or [])
    create_app(resume_jobs=True)
    assert order[:2] == ["sweep", "resume"]
    assert seen == {"keep": 2, "cap": 10.0}


@pytest.mark.parametrize("field, bad", [("builds_keep_per_agent", -1), ("builds_max_gb", -0.5)])
def test_the_settings_refuse_negative_retention_values(field, bad):
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(**{field: bad})


# ---------------------------------------------------------------------------
# defence in depth around rmtree (rev-r17 on cn/r17-w)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["../x", "/tmp", "a/b", "..", ".", "", "x" * 65])
def test_remove_for_agent_refuses_anything_but_a_single_safe_component(name, tmp_path):
    root = build_dirs.BUILD_ROOT
    root.mkdir(parents=True, exist_ok=True)
    sibling = root.parent / "x"
    sibling.mkdir(exist_ok=True)
    (sibling / "keep").write_text("k")
    result = build_dirs.remove_for_agent(name)
    assert result.status == "skipped" and "refused" in result.detail, result
    assert (sibling / "keep").exists()
    assert tmp_path.exists()


def test_a_symlinked_child_is_neither_counted_nor_removed_and_its_target_survives(tmp_path):
    root = build_dirs.BUILD_ROOT
    root.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "main.py").write_text("m")
    (root / "linked-agent").symlink_to(target, target_is_directory=True)
    assert build_dirs.remove_for_agent("linked-agent").status == "skipped"
    result = build_dirs.sweep(set(), keep_per_agent=1, max_gb=0)
    assert (root / "linked-agent").is_symlink() and (target / "pkg" / "main.py").exists()
    assert result.orphans_deleted == 0 and result.failed == []
