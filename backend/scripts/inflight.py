#!/usr/bin/env python
"""Pre-restart probe: what would restarting the backend do to work in flight?

    cd backend && uv run python scripts/inflight.py          # table
    cd backend && uv run python scripts/inflight.py --json   # for agents

Reads the ledger the service uses (`settings.database_url`, so
`LAUNCHPAD_DATABASE_URL` is honored) through a read-only connection and lists every
in-flight row with its `restart_outcome` (see `app/services/inflight.py`). It never
calls `create_app`/`init_db` (no migration, no seeding), writes no row and makes no
AWS call, so it is safe to run on prod at any time — including after `git merge`
and before the restart, when the checkout may be ahead of the ledger schema.

Exit code — branch on it in an update recipe:

    0  nothing in flight (or only restart_safe work) — restart
    2  only resumes / reconciles — restart is fine; expect resume lines in the journal
    3  something would be failed, cleared or stranded — wait, or restart knowingly
    1  the ledger could not be read (e.g. the database file does not exist)
   64  bad command-line usage
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.inflight import (
    BENIGN_OUTCOMES,
    EXIT_DISRUPTS,
    EXIT_NOTHING,
    EXIT_RESUMES,
    OUTCOMES,
    InflightItem,
    collect_inflight,
    exit_code,
)

EXIT_UNREADABLE = 1
EXIT_USAGE = 64  # argparse's default (2) would read as "only resumes"

VERDICTS = {
    EXIT_NOTHING: "nothing in flight — safe to restart",
    EXIT_RESUMES: "restart is fine — every item resumes or reconciles; expect resume "
                  "lines in the journal",
    EXIT_DISRUPTS: "a restart would fail, clear or strand the items above — wait for "
                   "them, or restart knowingly and tell the affected users",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _read_only_engine(url_text: str):
    """An engine that cannot write: SQLite gets `query_only`, and an absent SQLite
    file is refused (connecting would create an empty ledger and report nothing)."""
    url = make_url(url_text)
    if url.get_backend_name() == "sqlite":
        if url.database in (None, "", ":memory:"):
            raise SystemExit(f"inflight: {url_text} is not a ledger file")
        if not Path(url.database).is_file():
            print(f"inflight: no ledger at {url.database}", file=sys.stderr)
            raise SystemExit(EXIT_UNREADABLE)
        engine = create_engine(url_text)

        @event.listens_for(engine, "connect")
        def _query_only(dbapi_conn, _record) -> None:
            dbapi_conn.execute("PRAGMA query_only = ON")

        return engine
    return create_engine(url_text)


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = max(int(seconds), 0)
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600:02d}h"


def _table(items: list[InflightItem]) -> str:
    header = ("OUTCOME", "KIND", "ID", "WORKSPACE", "STATUS", "AGE", "HANDLED BY", "WHAT")
    rows = [header] + [
        (i.restart_outcome, i.kind, i.id, i.workspace_id or "-", i.status,
         _age(i.age_seconds), i.handled_by or "-", i.label)
        for i in items
    ]
    widths = [max(len(r[c]) for r in rows) for c in range(len(header) - 1)]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(r[:-1], widths, strict=True)) + "  " + r[-1]
        for r in rows
    )


def _ordered(items: list[InflightItem]) -> list[InflightItem]:
    """Disruptive outcomes first, then oldest first."""
    rank = {o: (o in BENIGN_OUTCOMES, n) for n, o in enumerate(OUTCOMES)}
    return sorted(items, key=lambda i: (rank[i.restart_outcome], -(i.age_seconds or 0)))


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    url_text = get_settings().database_url
    engine = _read_only_engine(url_text)
    skipped: list[str] = []
    try:
        with Session(engine) as db:
            items = _ordered(collect_inflight(db, now=datetime.now(UTC), skipped=skipped))
            db.rollback()  # nothing to undo — never leave a transaction to commit
    finally:
        engine.dispose()
    code = exit_code(items)

    if args.json:
        counts = {o: sum(1 for i in items if i.restart_outcome == o) for o in OUTCOMES}
        print(json.dumps({
            "database": make_url(url_text).render_as_string(hide_password=True),
            "exit_code": code,
            "verdict": VERDICTS[code],
            "counts": {o: n for o, n in counts.items() if n},
            "items": [i.to_dict() for i in items],
            "skipped": skipped,
        }, indent=2))
        return code

    print(f"ledger: {make_url(url_text).render_as_string(hide_password=True)}")
    if items:
        print(_table(items))
    for note in skipped:
        print(f"note: skipped {note}")
    print(f"exit {code}: {VERDICTS[code]}")
    return code


if __name__ == "__main__":
    sys.exit(main())
