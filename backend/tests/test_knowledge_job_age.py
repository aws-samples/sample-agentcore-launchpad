"""N17-obs286 (r17-s2, design review C4): one age computation for the knowledge-base orphan
rules. `_running_job` (the upload coalescer), `_reap_never_dispatched` (never-dispatched +
stalled) and `_doc_status` (INDEXING staleness) used to compute a 1800 s age inline, each with
its own stamp choice. They now share `job_age` / `stamp_age` / `is_stale`; the stamp choice
stays explicit per caller (`JOB_AGE_BASES`). This file pins the helpers against the three
inline formulas they replaced, over a grid of present / missing / unparsable / naive stamps.
The behavioural tests of the callers (obs204 / obs236 / obs247 / obs274 blocks in
`test_knowledge_selfhosted.py`) are unchanged."""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.knowledge_selfhosted import (
    JOB_AGE_BASES,
    RUNNING_JOB_STALE_S,
    is_stale,
    job_age,
    stamp_age,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
STAMPS: list[Any] = [
    None, "", "not-a-date",
    (NOW - timedelta(seconds=RUNNING_JOB_STALE_S + 1)).isoformat(),  # stale
    (NOW - timedelta(seconds=RUNNING_JOB_STALE_S)).isoformat(),      # exactly the bound
    (NOW - timedelta(seconds=60)).isoformat(),                       # fresh
    "2026-10-22T10:00:00",                                           # naive vs aware now
]


def _old_inline(stamp: Any) -> float | None:
    """The formula all three callers inlined before r17-s2."""
    try:
        return (NOW - datetime.fromisoformat(str(stamp))).total_seconds() if stamp else None
    except (ValueError, TypeError):
        return None


def test_stamp_age_matches_the_inline_formula():
    for stamp in STAMPS:
        assert stamp_age(stamp, NOW) == _old_inline(stamp), stamp


def test_each_basis_reads_the_stamp_its_caller_read():
    for updated, started, created in itertools.product(STAMPS, repeat=3):
        job = {"updated_at": updated, "started_at": started, "created_at": created}
        # `_running_job`: updated_at, else started_at, else created_at
        assert job_age(job, NOW, basis="last_write") == _old_inline(updated or started or created)
        # never-dispatched reaper: started_at only
        assert job_age(job, NOW, basis="dispatch") == _old_inline(started)
        # stalled reaper: updated_at only
        assert job_age(job, NOW, basis="progress") == _old_inline(updated)


def test_the_bases_are_the_three_documented_ones():
    assert JOB_AGE_BASES == {
        "last_write": ("updated_at", "started_at", "created_at"),
        "dispatch": ("started_at",),
        "progress": ("updated_at",),
    }


def test_is_stale_is_strictly_past_the_bound_and_never_for_an_unknown_age():
    assert is_stale(RUNNING_JOB_STALE_S + 0.001)
    assert not is_stale(RUNNING_JOB_STALE_S)
    assert not is_stale(None)
    assert not is_stale(-5.0)
