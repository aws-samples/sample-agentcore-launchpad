#!/usr/bin/env python3
"""Consistent, verified backup of the SQLite ledger — safe while the backend is serving.

    cd backend && uv run python scripts/ledger_backup.py               # the service's ledger
    python3 backend/scripts/ledger_backup.py --db data/launchpad.db    # no venv needed
    ... --keep 10            # then prune the oldest backups beyond the newest 10
    ... --keep 10 --dry-run  # back up, but only list what the prune would delete

Copies the ledger with SQLite's online backup API (`sqlite3.Connection.backup`), which
reads under a shared lock, so a write committing mid-copy cannot tear the result the way
a `cp` of the live file can (the engine runs the default rollback journal). The copy is
`<ledger>.bak-<UTC YYYYmmdd-HHMMSS>` next to the ledger (or in `--dest`); it is written
under a `.partial` name and renamed only once `PRAGMA integrity_check` returns `ok`.
A copy that fails the check is kept as `….corrupt` for inspection and nothing is pruned.

Without `--db` the path comes from `settings.database_url` (so `LAUNCHPAD_DATABASE_URL`
is honored) and only a file SQLite URL is accepted. With `--db` nothing is imported from
`app` — the script is stdlib-only and runs under the system `python3`. The source is
opened read-only; the script never writes the ledger and makes no AWS call.

`--keep N` deletes the oldest `<ledger>.bak-<YYYYmmdd-HHMMSS>` files beyond the newest N
(the new backup included). Any other name — `….bak-pre-pr191-*`, `….pre-registry-ga-*`,
`….corrupt`, a hand-made copy — is never touched. Default: no pruning.

Exit code:

    0  backup written and verified (and pruned, if asked)
    1  no backup: bad ledger URL/path, destination clash, or a SQLite/OS error
    2  backup written but integrity_check failed — kept as `….corrupt`, nothing pruned
   64  bad command-line usage
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CORRUPT = 2
EXIT_USAGE = 64  # argparse's default (2) would read as "corrupt"

COUNTED_TABLES = ("agents", "jobs", "eval_runs")
_STAMP = re.compile(r"\d{8}-\d{6}")


class BackupError(Exception):
    """No backup was written; the message says why."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def ledger_from_settings() -> Path:
    """The service's ledger file, from `settings.database_url`. Imports `app` lazily so
    `--db` runs need neither the backend venv nor the backend on `sys.path`."""
    from sqlalchemy.engine import make_url

    from app.core.config import get_settings

    url_text = get_settings().database_url
    url = make_url(url_text)
    shown = url.render_as_string(hide_password=True)
    if url.get_backend_name() != "sqlite":
        raise BackupError(f"{shown} is not a SQLite ledger — back it up with its own tools")
    if url.database in (None, "", ":memory:") or url.database.startswith("file:"):
        raise BackupError(f"{shown} does not name a ledger file — pass --db PATH")
    return Path(url.database)


def backup_name(ledger: Path, now: datetime) -> str:
    return f"{ledger.name}.bak-{now.astimezone(UTC).strftime('%Y%m%d-%H%M%S')}"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def take_backup(ledger: Path, dest_dir: Path, *, now: datetime, timeout: float) -> Path:
    """Copy `ledger` into `dest_dir` with the online backup API; returns the `.partial`
    file. Waits (up to `timeout` seconds) while another connection is committing."""
    if not ledger.is_file():
        raise BackupError(f"no ledger at {ledger}")
    if not dest_dir.is_dir():
        raise BackupError(f"destination {dest_dir} is not a directory")
    final = dest_dir / backup_name(ledger, now)
    if final.exists():
        raise BackupError(f"{final} already exists")
    partial = final.with_name(final.name + ".partial")
    partial.unlink(missing_ok=True)  # leftover of an interrupted run
    deadline = time.monotonic() + timeout

    def _progress(status: int, _remaining: int, _total: int) -> None:
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and (
            time.monotonic() > deadline
        ):
            raise TimeoutError(f"the ledger stayed locked for {timeout:g}s")

    src = _connect_read_only(ledger)
    try:
        dst = sqlite3.connect(partial)
        try:
            # One step (pages=-1): the whole copy happens under a single read lock.
            src.backup(dst, pages=-1, progress=_progress, sleep=0.1)
        finally:
            dst.close()
    except (sqlite3.Error, TimeoutError) as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"backup of {ledger} failed: {exc}") from exc
    finally:
        src.close()
    return partial


def inspect_copy(path: Path) -> tuple[list[str], dict[str, int | None]]:
    """`PRAGMA integrity_check` lines and the row counts of `COUNTED_TABLES` (None when
    the table does not exist, e.g. an old schema). A copy SQLite cannot even read
    reports the error as its check result."""
    conn = _connect_read_only(path)
    try:
        check = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if check != ["ok"]:
            return check, {}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master "
                                                 "WHERE type = 'table'")}
        counts: dict[str, int | None] = {}
        for table in COUNTED_TABLES:
            counts[table] = (conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                             if table in tables else None)
        return check, counts
    except sqlite3.DatabaseError as exc:
        return [f"unreadable: {exc}"], {}
    finally:
        conn.close()


def prunable(directory: Path, ledger_name: str, keep: int) -> list[Path]:
    """The `<ledger_name>.bak-<YYYYmmdd-HHMMSS>` files beyond the newest `keep`, oldest
    first. The stamp sorts chronologically, so the name is the age."""
    prefix = f"{ledger_name}.bak-"
    backups = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.name.startswith(prefix)
        and _STAMP.fullmatch(p.name[len(prefix):])
    )
    return backups[:-keep] if keep > 0 else backups


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n} B"


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--db", type=Path, help="ledger file (default: settings.database_url)")
    parser.add_argument("--dest", type=Path,
                        help="directory for the copy (default: the ledger's directory)")
    parser.add_argument("--keep", type=int, metavar="N",
                        help="then delete the oldest backups beyond the newest N")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --keep: list what would be deleted, delete nothing")
    parser.add_argument("--timeout", type=float, default=60.0, metavar="SECONDS",
                        help="how long to wait for a committing writer (default 60)")
    args = parser.parse_args(argv)
    if args.keep is not None and args.keep < 1:
        parser.error("--keep must be at least 1 (the new backup is always kept)")
    if args.dry_run and args.keep is None:
        parser.error("--dry-run only applies to --keep")

    try:
        ledger = args.db if args.db is not None else ledger_from_settings()
        dest_dir = args.dest if args.dest is not None else ledger.parent
        partial = take_backup(ledger, dest_dir, now=datetime.now(UTC), timeout=args.timeout)
        check, counts = inspect_copy(partial)
    except (BackupError, sqlite3.Error, OSError) as exc:
        print(f"ledger_backup: {exc}", file=sys.stderr)
        return EXIT_FAILED

    final = partial.with_name(partial.name.removesuffix(".partial"))
    if check != ["ok"]:
        flagged = final.with_name(final.name + ".corrupt")
        partial.replace(flagged)
        print(f"ledger_backup: integrity_check FAILED on the copy, kept as {flagged}:",
              file=sys.stderr)
        for line in check[:10]:
            print(f"  {line}", file=sys.stderr)
        print("nothing pruned — do not proceed with the update until this is understood",
              file=sys.stderr)
        return EXIT_CORRUPT
    partial.replace(final)

    print(f"backup: {final}")
    print(f"source: {ledger}")
    print(f"size:   {_size(final.stat().st_size)}")
    print("rows:   " + "  ".join(f"{t}={'-' if n is None else n}" for t, n in counts.items()))
    print("integrity_check: ok")

    if args.keep is not None:
        doomed = prunable(final.parent, ledger.name, args.keep)
        verb = "would delete" if args.dry_run else "deleted"
        for path in doomed:
            if not args.dry_run:
                path.unlink()
            print(f"{verb}: {path.name}")
        print(f"pruned: {len(doomed)} {'would be deleted (dry run)' if args.dry_run else 'deleted'}"
              f", newest {args.keep} kept")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
