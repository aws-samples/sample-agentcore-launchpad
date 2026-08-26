"""Read-side of Skill Lab jobs: log tail, eval results, progress, artifacts.

Ports of skillopt_studio/artifacts.py adapted to the launchpad job layout
(data/skill-lab/jobs/<id>/). Everything here is pure filesystem reading — the
API re-reads per request; nothing is cached or persisted (finished artifacts
are cheap to parse and running ones change under us).
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from app.core.config import DATA_DIR
from app.core.errors import AppError

JOBS_DIR = DATA_DIR / "skill-lab" / "jobs"
TEXT_ARTIFACT_CAP = 512 * 1024
# Bytes one log poll may return. The child writes without bound, so an
# uncapped read would put the whole log in memory and in one response; the
# caller keeps polling with `next_offset` until `eof`.
LOG_CHUNK_CAP = 512 * 1024
_RESPONSE_EXCERPT_CHARS = 2000

# Per-task projection for the results table (studio _ROW_FIELDS + judge fields
# the UI renders; response is excerpted, artifacts reduced to path+size).
_ROW_FIELDS = (
    "id",
    "task_type",
    "hard",
    "soft",
    "score_valid",
    "duration_s",
    "judge_status",
    "judge_reason",
    "judge_error",
    "error",
    "usage",
    "judge_usage",
)

# A judge worker that cannot start reports the missing binary through a Python
# FileNotFoundError, which otherwise reaches the operator only as a stack trace
# on a row counted as `invalid` — the shape that turned a one-line host
# prerequisite into a long investigation. Only a KNOWN judge CLI is reported as a
# prerequisite: an unrelated FileNotFoundError inside the judge must keep looking
# like the crash it is.
_MISSING_BINARY = re.compile(r"FileNotFoundError:[^']*'([\w.\-/]+)'")

_PROGRESS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[STEP (\d+) done\]"), "step {0} done"),
    (re.compile(r"STEP (\d+)\b"), "step {0} running"),
    (re.compile(r"\[skilleval\] judging (\d+)"), "judging {0} responses"),
    (re.compile(r"\[skilleval\] tasks: (\d+)"), "rollout {0} tasks"),
    (re.compile(r"(rollout)", re.IGNORECASE), "rollout in progress"),
)


def job_dir(job_id: str) -> Path:
    if not job_id or any(part in job_id for part in ("/", "\\", "..")):
        raise AppError("skill_lab.job_not_found", f"job '{job_id}' not found", status_code=404)
    return JOBS_DIR / job_id


def out_root(job_id: str) -> Path:
    return job_dir(job_id) / "out"


def read_log(job_id: str, offset: int = 0) -> dict[str, Any]:
    """Byte-offset log tailing; errors='replace' makes a mid-multibyte offset safe.

    `eof` is False when the cap cut the chunk short — the caller polls again from
    `next_offset` to catch up rather than waiting for the next tick.
    """
    path = job_dir(job_id) / "log.txt"
    if not path.is_file():
        return {"content": "", "next_offset": 0, "eof": True}
    offset = max(0, int(offset))
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(LOG_CHUNK_CAP + 1)
    eof = len(data) <= LOG_CHUNK_CAP
    data = data[:LOG_CHUNK_CAP]
    return {
        "content": data.decode("utf-8", errors="replace"),
        "next_offset": offset + len(data),
        "eof": eof,
    }


def log_tail(job_id: str, size: int = 8192) -> str:
    """Last `size` bytes of the log — the excerpt a failed job's `error` carries
    and the input to the progress phrase. Bounded on purpose: a training log runs
    to tens of MB and neither caller wants it in memory."""
    path = job_dir(job_id) / "log.txt"
    if not path.is_file():
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, path.stat().st_size - size))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def job_progress(job_id: str, status: str) -> str:
    """Short phrase for the UI, derived from the log tail — never persisted."""
    if status != "running":
        return status
    for line in reversed(log_tail(job_id).splitlines()):
        for pattern, template in _PROGRESS_PATTERNS:
            match = pattern.search(line)
            if match:
                return template.format(*match.groups())
    if (out_root(job_id) / "results.json").is_file():
        return "finalizing"
    return "running"


def _excerpt(text: Any) -> str:
    value = str(text or "")
    if len(value) > _RESPONSE_EXCERPT_CHARS:
        return value[:_RESPONSE_EXCERPT_CHARS] + " …[truncated]"
    return value


def eval_results(job_id: str) -> dict[str, Any] | None:
    """Summary + per-task rows from out/results.json; None until it exists.

    `score_valid is False` rows are infrastructure failures (vendored
    semantics): they are counted as `invalid` and excluded from the pass-rate
    and soft-mean denominators rather than scored as zeros.
    """
    path = out_root(job_id) / "results.json"
    if not path.is_file():
        return None
    try:
        results = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(results, list):
        return None

    rows: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key) for key in _ROW_FIELDS}
        row["judge_prerequisite"] = _judge_prerequisite(row)
        row["response"] = _excerpt(item.get("response"))
        row["artifacts"] = [
            {"path": a.get("path"), "size": a.get("size")}
            for a in (item.get("artifacts") or [])
            if isinstance(a, dict)
        ]
        rows.append(row)

    scored = [r for r in rows if r.get("score_valid") is not False]
    invalid = len(rows) - len(scored)
    passed = sum(1 for r in scored if r.get("hard"))
    # One aggregate so the console can state the fix once instead of per row.
    missing_clis = sorted(
        {str(r["judge_prerequisite"]) for r in rows if r.get("judge_prerequisite")}
    )
    return {
        "summary": {
            "tasks": len(rows),
            "passed": passed,
            "invalid": invalid,
            "judge_prerequisite_missing": missing_clis,
            "pass_rate": round(passed / len(scored), 4) if scored else 0.0,
            "soft_mean": (
                round(sum(float(r.get("soft") or 0.0) for r in scored) / len(scored), 4)
                if scored
                else 0.0
            ),
            "duration_s": round(sum(float(r.get("duration_s") or 0.0) for r in rows), 1),
        },
        "rows": rows,
    }


def _judge_prerequisite(row: dict[str, Any]) -> str | None:
    """Name the host judge CLI a judge failure blames, or None.

    `runner` imports this module, so the binary map is fetched locally rather
    than at import time.
    """
    match = _MISSING_BINARY.search(str(row.get("judge_error") or ""))
    if match is None:
        return None
    from app.skill_lab import runner

    binary = match.group(1).rsplit("/", 1)[-1]
    return binary if binary in set(runner.JUDGE_CLI_BINARY.values()) else None


def taskgen_results(job_id: str) -> dict[str, Any] | None:
    """Generated tasks + gen_summary from a taskgen job's out/; None until the
    CLI wrote its validated output. The tasks are returned verbatim (they went
    through load_tasks in the CLI) so the review UI and the import path see
    exactly what will be saved."""
    tasks_path = out_root(job_id) / "generated_tasks.json"
    if not tasks_path.is_file():
        return None
    try:
        tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(tasks, list):
        return None
    summary: dict[str, Any] = {}
    summary_path = out_root(job_id) / "gen_summary.json"
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                summary = loaded
        except (OSError, json.JSONDecodeError):
            summary = {}
    return {"count": len(tasks), "tasks": tasks, "summary": summary}


def _safe_resolve(job_id: str, rel: str) -> Path:
    """Resolve a user-supplied path strictly inside the job's out/ tree.

    Both sides are resolved, so a symlink the CLI (or a rollout artifact) planted
    inside out/ cannot widen the window either — the link's target is what gets
    compared, not the link.
    """
    root = out_root(job_id).resolve()
    rel = (rel or "").strip()
    # A NUL would raise ValueError out of resolve() and surface as a 500.
    if rel.startswith(("/", "~")) or "\\" in rel or "\0" in rel:
        raise AppError("skill_lab.bad_path", f"invalid artifact path {rel!r}", status_code=400)
    target = (root / rel).resolve() if rel else root
    if target != root and root not in target.parents:
        raise AppError("skill_lab.bad_path", f"invalid artifact path {rel!r}", status_code=400)
    return target


def list_artifacts(job_id: str, rel: str = "") -> dict[str, Any]:
    target = _safe_resolve(job_id, rel)
    if target.is_dir():
        dirs: list[str] = []
        files: list[dict[str, Any]] = []
        for entry in target.iterdir():
            try:  # a running job writes under us — a vanished entry is not an error
                if entry.is_dir():
                    dirs.append(entry.name)
                elif entry.is_file():
                    files.append({"name": entry.name, "size": entry.stat().st_size})
            except OSError:
                continue
        dirs.sort()
        files.sort(key=lambda f: f["name"])
        return {"kind": "dir", "path": rel, "dirs": dirs, "files": files}
    if target.is_file():
        return read_artifact(job_id, rel)
    raise AppError("skill_lab.artifact_not_found", f"no artifact at {rel!r}", status_code=404)


def read_artifact(job_id: str, rel: str) -> dict[str, Any]:
    target = _safe_resolve(job_id, rel)
    if not target.is_file():
        raise AppError("skill_lab.artifact_not_found", f"no artifact at {rel!r}", status_code=404)
    size = target.stat().st_size
    with open(target, "rb") as handle:  # read the cap, not the file: an artifact
        data = handle.read(TEXT_ARTIFACT_CAP)  # can be a multi-GB rollout dump
    truncated = size > TEXT_ARTIFACT_CAP
    if b"\0" in data:
        return {"kind": "binary", "path": rel, "size": size}
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            return {"kind": "binary", "path": rel, "size": size}
        # The cap can land mid-codepoint — that alone doesn't make a big text
        # file binary.
        content = data.decode("utf-8", errors="replace")
    return {
        "kind": "text",
        "path": rel,
        "size": size,
        "truncated": truncated,
        "content": content,
    }


def artifact_file(job_id: str, rel: str) -> Path:
    """Path for a raw download; existence-checked."""
    target = _safe_resolve(job_id, rel)
    if not target.is_file():
        raise AppError("skill_lab.artifact_not_found", f"no artifact at {rel!r}", status_code=404)
    return target


# ── training reads ─────────────────────────────────────────────────────────

_STEP_FIELDS = (
    "step",
    "epoch",
    "action",
    "selection_hard",
    "selection_soft",
    "current_score",
    "best_score",
    "best_step",
    "skill_len",
    "wall_time_s",
    "gate_reasons",
    "excluded_failures",
)


def _read_json_file(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def train_summary(job_id: str) -> dict[str, Any] | None:
    """Timeline + totals from out/history.json (grows step by step — usable
    MID-RUN) and out/summary.json (written once, at the end). None until the
    first step record lands."""
    out = out_root(job_id)
    history = _read_json_file(out / "history.json")
    summary = _read_json_file(out / "summary.json")
    finished = isinstance(summary, dict)
    if not isinstance(history, list):
        # No step file: either nothing has landed yet (or a torn mid-write read),
        # or the run ended before its first step — the latter still has totals.
        if not finished:
            return None
        history = []
    steps = [
        {key: record.get(key) for key in _STEP_FIELDS}
        for record in history
        if isinstance(record, dict)
    ]
    if finished:
        totals = {
            "steps": summary.get("total_steps"),
            "accepts": summary.get("total_accepts"),
            "rejects": summary.get("total_rejects"),
            "skips": summary.get("total_skips"),
            "wall_time_s": summary.get("total_wall_time_s"),
        }
        baseline = summary.get("baseline_selection_hard")
        best_step = summary.get("best_step")
        best_score = summary.get("best_selection_hard")
        # `final` is the score of the skill this job actually hands over —
        # best_skill.md, i.e. the trainer's `test_hard` (what train.py itself
        # prints as "Final test"). `final_test_hard` grades the LAST accepted
        # skill, which with the gate on need not be the best one, so it is only
        # the fallback.
        best_test = summary.get("test_hard")
        test_scores = {
            "baseline": summary.get("baseline_test_hard"),
            "final": summary.get("final_test_hard") if best_test is None else best_test,
        }
    else:
        actions = [str(step.get("action") or "") for step in steps]
        totals = {
            "steps": len(steps),
            "accepts": sum("accept" in a for a in actions),
            "rejects": sum("reject" in a for a in actions),
            "skips": sum("skip" in a for a in actions),
            "wall_time_s": round(
                sum(float(step.get("wall_time_s") or 0.0) for step in steps), 1
            ),
        }
        baseline = None
        best_step = steps[-1].get("best_step") if steps else None
        best_score = steps[-1].get("best_score") if steps else None
        test_scores = {"baseline": None, "final": None}
    return {
        "steps": steps,
        "finished": finished,
        "baseline_selection_hard": baseline,
        "best_step": best_step,
        "best_score": best_score,
        "test_scores": test_scores,
        "totals": totals,
    }


def skill_diff(job_id: str) -> dict[str, Any] | None:
    """SEED (skills/skill_v0000.md, re-written every run) vs best_skill.md.
    None until both exist; `changed` False means no edit was ever accepted."""
    out = out_root(job_id)
    seed_path = out / "skills" / "skill_v0000.md"
    best_path = out / "best_skill.md"
    if not (seed_path.is_file() and best_path.is_file()):
        return None
    seed = seed_path.read_text(encoding="utf-8")
    best = best_path.read_text(encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            seed.splitlines(keepends=True),
            best.splitlines(keepends=True),
            fromfile="SEED skill_v0000.md",
            tofile="BEST best_skill.md",
        )
    )
    return {"seed": seed, "best": best, "changed": seed != best, "diff": diff}
