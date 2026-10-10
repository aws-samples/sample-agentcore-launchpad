"""Online ledger backup (scripts/ledger_backup.py).

Every test works on a throwaway SQLite file under `tmp_path`: the copy is named like
the runbook's backups, passes `integrity_check` and matches the source row for row,
stays consistent against concurrent writers, flags a corrupt copy instead of pruning,
prunes only `<ledger>.bak-<stamp>` names, refuses non-file / non-SQLite URLs, and —
with `--db` — runs under a bare interpreter that cannot import `app`.
"""

import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "scripts" / "ledger_backup.py"
BACKUP_NAME = re.compile(r"launchpad\.db\.bak-\d{8}-\d{6}")


def _load_cli():
    spec = importlib.util.spec_from_file_location("ledger_backup_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli():
    return _load_cli()


def _ledger(path: Path, *, agents: int = 3, jobs: int = 5, eval_runs: int = 2) -> Path:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, log TEXT)")
        conn.execute("CREATE INDEX ix_jobs_log ON jobs (log)")
        conn.execute("CREATE TABLE eval_runs (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO agents (name) VALUES (?)",
                         [(f"agent-{i}",) for i in range(agents)])
        conn.executemany("INSERT INTO jobs (log) VALUES (?)",
                         [(f"log-{i}-" + "x" * 500,) for i in range(jobs)])
        conn.executemany("INSERT INTO eval_runs DEFAULT VALUES", [()] * eval_runs)
    conn.close()
    return path


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _backups(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if BACKUP_NAME.fullmatch(p.name))


def test_backup_is_named_verified_and_matches_the_source(cli, tmp_path, capsys):
    ledger = _ledger(tmp_path / "launchpad.db", agents=3, jobs=5, eval_runs=2)
    before = datetime.now(UTC).replace(microsecond=0)

    assert cli.main(["--db", str(ledger)]) == cli.EXIT_OK

    [copy] = _backups(tmp_path)  # default destination = the ledger's own directory
    stamp = datetime.strptime(copy.name.rsplit("bak-", 1)[1], "%Y%m%d-%H%M%S")
    assert before <= stamp.replace(tzinfo=UTC) <= datetime.now(UTC)  # UTC stamp
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    conn.close()
    for table in ("agents", "jobs", "eval_runs"):
        assert _count(copy, table) == _count(ledger, table)
    out = capsys.readouterr().out
    assert f"backup: {copy}" in out
    assert "rows:   agents=3  jobs=5  eval_runs=2" in out
    assert "integrity_check: ok" in out
    assert not list(tmp_path.glob("*.partial"))


def test_dest_override_and_missing_tables(cli, tmp_path, capsys):
    ledger = tmp_path / "launchpad.db"
    conn = sqlite3.connect(ledger)
    conn.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY)")
    conn.close()
    dest = tmp_path / "out"
    dest.mkdir()

    assert cli.main(["--db", str(ledger), "--dest", str(dest)]) == cli.EXIT_OK

    assert len(_backups(dest)) == 1 and not _backups(tmp_path)
    assert "rows:   agents=0  jobs=-  eval_runs=-" in capsys.readouterr().out


def test_the_source_is_never_written(cli, tmp_path):
    ledger = _ledger(tmp_path / "launchpad.db")
    stat = ledger.stat()
    assert cli.main(["--db", str(ledger)]) == cli.EXIT_OK
    assert ledger.stat().st_mtime_ns == stat.st_mtime_ns
    assert ledger.stat().st_size == stat.st_size


def test_open_write_transaction_yields_the_pre_transaction_state(cli, tmp_path):
    """A writer holding RESERVED (uncommitted rows in its cache) does not block the
    shared read lock: the copy is the last committed state, never a mix."""
    ledger = _ledger(tmp_path / "launchpad.db", jobs=5)
    writer = sqlite3.connect(ledger, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.executemany("INSERT INTO jobs (log) VALUES (?)", [("uncommitted",)] * 50)
    try:
        assert cli.main(["--db", str(ledger)]) == cli.EXIT_OK
    finally:
        writer.execute("COMMIT")
        writer.close()

    [copy] = _backups(tmp_path)
    assert _count(copy, "jobs") == 5
    assert _count(ledger, "jobs") == 55


def test_committing_writer_is_waited_for(cli, tmp_path):
    """A writer holding EXCLUSIVE (mid-commit) makes the backup wait, then copy the
    committed result."""
    ledger = _ledger(tmp_path / "launchpad.db", jobs=5)
    writer = sqlite3.connect(ledger, isolation_level=None, check_same_thread=False)
    writer.execute("BEGIN EXCLUSIVE")
    writer.executemany("INSERT INTO jobs (log) VALUES (?)", [("committed",)] * 10)
    timer = threading.Timer(0.5, lambda: writer.execute("COMMIT"))
    timer.start()
    try:
        started = time.monotonic()
        assert cli.main(["--db", str(ledger), "--timeout", "10"]) == cli.EXIT_OK
        assert time.monotonic() - started >= 0.4
    finally:
        timer.join()
        writer.close()

    [copy] = _backups(tmp_path)
    assert _count(copy, "jobs") == 15


def test_a_lock_that_outlasts_the_timeout_fails_cleanly(cli, tmp_path, capsys):
    ledger = _ledger(tmp_path / "launchpad.db")
    writer = sqlite3.connect(ledger, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        assert cli.main(["--db", str(ledger), "--timeout", "0.3"]) == cli.EXIT_FAILED
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert "stayed locked" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["launchpad.db"]


def test_backups_under_a_busy_writer_are_never_torn(cli, tmp_path):
    """Each writer transaction inserts a batch of 10 rows: every copy must pass the
    integrity check and hold a whole number of batches."""
    ledger = _ledger(tmp_path / "launchpad.db", jobs=0)
    stop = threading.Event()

    def write() -> None:
        conn = sqlite3.connect(ledger, timeout=10)
        while not stop.is_set():
            with conn:
                conn.executemany("INSERT INTO jobs (log) VALUES (?)",
                                 [("y" * 2000,)] * 10)
        conn.close()

    thread = threading.Thread(target=write)
    thread.start()
    try:
        for n in range(4):
            dest = tmp_path / f"d{n}"
            dest.mkdir()
            assert cli.main(["--db", str(ledger), "--dest", str(dest)]) == cli.EXIT_OK
            [copy] = _backups(dest)
            conn = sqlite3.connect(copy)
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            conn.close()
            assert _count(copy, "jobs") % 10 == 0
    finally:
        stop.set()
        thread.join()


def test_corrupt_copy_is_flagged_kept_and_nothing_is_pruned(cli, tmp_path, capsys):
    ledger = _ledger(tmp_path / "launchpad.db", jobs=400)
    old = tmp_path / "launchpad.db.bak-20260101-000000"
    old.write_bytes(b"old")
    page_size = 4096
    with ledger.open("r+b") as fh:  # scribble over pages past the schema page
        size = ledger.stat().st_size
        for offset in range(2 * page_size, size - page_size, page_size):
            fh.seek(offset + 100)
            fh.write(b"\xff" * 200)

    assert cli.main(["--db", str(ledger), "--keep", "1"]) == cli.EXIT_CORRUPT

    err = capsys.readouterr().err
    assert "integrity_check FAILED" in err
    [flagged] = list(tmp_path.glob("launchpad.db.bak-*.corrupt"))
    assert BACKUP_NAME.fullmatch(flagged.name.removesuffix(".corrupt"))
    assert old.exists()  # a bad new copy never costs a good old one
    assert _backups(tmp_path) == [old]


def _fake_backups(directory: Path) -> tuple[list[Path], list[Path]]:
    backups = [directory / f"launchpad.db.bak-2026100{d}-120000" for d in range(1, 6)]
    others = [directory / name for name in (
        "launchpad.db.pre-x",
        "launchpad.db.pre-registry-ga-20260807T072248Z",
        "launchpad.db.bak-pre-pr191-20261006",
        "launchpad.db.bak-20260101-000000.corrupt",
        "other.db.bak-20250101-000000",
    )]
    for path in backups + others:
        path.write_bytes(b"x")
    return backups, others


def test_prune_keeps_the_newest_and_ignores_other_names(cli, tmp_path):
    backups, others = _fake_backups(tmp_path)

    doomed = cli.prunable(tmp_path, "launchpad.db", keep=2)

    assert doomed == backups[:3]
    for path in others:
        assert path not in doomed


def test_keep_prunes_through_the_cli_counting_the_new_backup(cli, tmp_path, capsys):
    """Five fake backups + the new one, `--keep 3`: exactly the three oldest fakes go."""
    ledger = _ledger(tmp_path / "launchpad.db")
    backups, others = _fake_backups(tmp_path)

    assert cli.main(["--db", str(ledger), "--keep", "3"]) == cli.EXIT_OK

    remaining = _backups(tmp_path)
    assert remaining[:2] == backups[3:]
    assert len(remaining) == 3  # the two newest fakes + today's copy
    assert all(p.exists() for p in others) and ledger.exists()
    out = capsys.readouterr().out
    assert [f"deleted: {p.name}" in out for p in backups[:3]] == [True] * 3


def test_dry_run_deletes_nothing(cli, tmp_path, capsys):
    ledger = _ledger(tmp_path / "launchpad.db")
    backups, others = _fake_backups(tmp_path)

    assert cli.main(["--db", str(ledger), "--keep", "2", "--dry-run"]) == cli.EXIT_OK

    assert all(p.exists() for p in backups + others)
    out = capsys.readouterr().out
    assert sum(line.startswith("would delete: ") for line in out.splitlines()) == 4
    assert "deleted:" not in out


@pytest.mark.parametrize("argv", [
    ["--keep", "0"],
    ["--dry-run"],
    ["--bogus"],
])
def test_usage_errors_exit_64(cli, tmp_path, argv):
    ledger = _ledger(tmp_path / "launchpad.db")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(ledger), *argv])
    assert exc.value.code == cli.EXIT_USAGE
    assert not _backups(tmp_path)


def test_missing_ledger_is_refused_and_not_created(cli, tmp_path, capsys):
    missing = tmp_path / "launchpad.db"
    assert cli.main(["--db", str(missing)]) == cli.EXIT_FAILED
    assert "no ledger at" in capsys.readouterr().err
    assert not missing.exists() and not list(tmp_path.iterdir())


def _run_from_settings(database_url: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "LAUNCHPAD_DATABASE_URL": database_url}
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("url, message", [
    ("postgresql://user:hunter2@db.example/launchpad", "is not a SQLite ledger"),
    ("sqlite://", "does not name a ledger file"),
    ("sqlite:///:memory:", "does not name a ledger file"),
])
def test_non_file_or_non_sqlite_url_is_refused(url, message):
    proc = _run_from_settings(url, BACKEND)
    assert proc.returncode == 1, proc.stderr
    assert message in proc.stderr
    assert "hunter2" not in proc.stderr


def test_settings_url_is_honored(tmp_path):
    ledger = _ledger(tmp_path / "service.db")
    proc = _run_from_settings(f"sqlite:///{ledger}", BACKEND)
    assert proc.returncode == 0, proc.stderr
    [copy] = [p for p in tmp_path.iterdir() if p.name.startswith("service.db.bak-")]
    assert f"backup: {copy}" in proc.stdout


_SYSTEM_PYTHON = shutil.which("python3", path="/usr/bin:/bin")


@pytest.mark.parametrize("interpreter", [
    # The suite's interpreter with no site-packages at all (-S): the backend venv
    # installs `app` there, so this is what "outside the venv" means for it.
    pytest.param([sys.executable, "-I", "-S"], id="venv-python-no-site"),
    pytest.param([_SYSTEM_PYTHON, "-I"], id="system-python3", marks=pytest.mark.skipif(
        _SYSTEM_PYTHON is None, reason="no system python3")),
])
def test_db_flag_runs_without_the_backend_on_the_path(tmp_path, interpreter):
    """`--db` is stdlib-only: isolated mode (-I ignores PYTHONPATH and the user site),
    cwd outside the backend, no PYTHONPATH — `app` is not importable."""
    ledger = _ledger(tmp_path / "launchpad.db")
    work = tmp_path / "elsewhere"
    work.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LAUNCHPAD_"))}
    probe = subprocess.run([*interpreter, "-c", "import app"], cwd=work, env=env,
                           capture_output=True, text=True)
    assert probe.returncode != 0  # the premise: no `app` here

    proc = subprocess.run([*interpreter, str(SCRIPT), "--db", str(ledger),
                           "--keep", "1", "--dry-run"],
                          cwd=work, env=env, capture_output=True, text=True, timeout=60)

    assert proc.returncode == 0, proc.stderr
    assert "integrity_check: ok" in proc.stdout
    assert len(_backups(tmp_path)) == 1
