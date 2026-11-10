"""Job-level verdict on a Skill Lab run that the CLI itself called a success (N10v3-bug3).

With the exec worker down, every rollout errored, the vendored CLIs still exited 0 and
the console showed a green job with "PASS RATE 0.0% · INVALID 0". Exit code 0 is the
CLI's verdict on itself, not on the run: a run in which not one rollout produced a
scorable result has no signal to report. This module turns that into a `failed` job
with a readable reason, and counts errored rollouts for the results summary.

Kept apart from `artifacts.py` on purpose: that module is restored verbatim from the
fork baseline (scripts/n13_restore_check.sh); the CN platform layer adds its verdict
here and in the enumerated `jobs.py` / `routers.py` edits.
"""

from __future__ import annotations

import re
from typing import Any

from app.skill_lab import artifacts

# One line per rollout batch, printed by the vendored `run_batch` on the eval AND the
# train path — the one shared signal that a run never scored anything.
_ROLLOUT_FINISHED = re.compile(
    r"\[skilleval\] rollout finished: (\d+) ok, (\d+) errored, (\d+) invalid"
)
_FIRST_ERROR = re.compile(r"\b(\w*(?:Error|Exception)): (.+)")
_REASON_EXCERPT_CHARS = 300


def errored_count(rows: list[dict[str, Any]]) -> int:
    """Rows whose rollout raised but that were still SCORED (as zeros — the vendored
    `artifact_failure`): exactly `run_batch`'s "errored". Disjoint from `invalid`
    (`score_valid is False`, excluded from scoring), so the console can state both."""
    return sum(1 for row in rows if row.get("error") and row.get("score_valid") is not False)


def with_errored_count(results: dict[str, Any]) -> dict[str, Any]:
    """`eval_results()` output plus `summary.errored` (the console's KPI note)."""
    summary = dict(results.get("summary") or {})
    summary["errored"] = errored_count(list(results.get("rows") or []))
    return {**results, "summary": summary}


def rollout_health(job_id: str) -> dict[str, int] | None:
    """Totals over every `rollout finished` line of the job log; None when the log
    carries none (a run that died before its first rollout, or a stub)."""
    path = artifacts.job_dir(job_id) / "log.txt"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ok = errored = invalid = batches = 0
    for match in _ROLLOUT_FINISHED.finditer(text):
        batches += 1
        ok += int(match.group(1))
        errored += int(match.group(2))
        invalid += int(match.group(3))
    if not batches:
        return None
    return {
        "batches": batches, "ok": ok, "errored": errored, "invalid": invalid,
        "total": ok + errored + invalid,
    }


def _first_log_error(job_id: str) -> str:
    path = artifacts.job_dir(job_id) / "log.txt"
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _FIRST_ERROR.search(line)
            if match:
                return f"{match.group(1)}: {match.group(2)}"[:_REASON_EXCERPT_CHARS]
    except OSError:
        pass
    return ""


def all_rollouts_failed_reason(job_id: str, job_type: str) -> str | None:
    """Why a CLI that exited 0 must still count as a FAILED job, or None.

    Eval jobs are judged on their results rows (every row carries `error` or
    `score_valid=False`); train jobs (whose history carries no error counts) and eval
    jobs without results on the shared rollout log line. A run with at least one scored
    rollout is a real, if poor, result and stays `succeeded`. Taskgen never rolls out
    (one generation call; its CLI already fails loudly).
    """
    if job_type == "taskgen":
        return None
    if job_type == "eval":
        results = artifacts.eval_results(job_id)
        if results is not None:
            rows = results["rows"]
            failed = [r for r in rows if r.get("error") or r.get("score_valid") is False]
            if rows and len(failed) == len(rows):
                invalid = results["summary"]["invalid"]
                first = str(next((r.get("error") for r in rows if r.get("error")), "") or "")
                return (
                    f"all {len(rows)} rollouts failed ({invalid} infrastructure · "
                    f"{len(rows) - invalid} exec errors) — nothing was scored; "
                    f"first error: {first[:_REASON_EXCERPT_CHARS] or 'see the per-row error'}"
                )
            return None
    health = rollout_health(job_id)
    if health is None or health["ok"] > 0 or health["total"] == 0:
        return None
    first = _first_log_error(job_id)
    return (
        f"all {health['total']} rollouts across {health['batches']} batch(es) failed "
        f"(0 ok · {health['errored']} errored · {health['invalid']} invalid) — the run "
        f"produced no scorable result" + (f"; first error: {first}" if first else "")
    )
