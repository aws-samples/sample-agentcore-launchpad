"""SkillEval LLM judge — rubric-based verdicts on agent responses.

Each task carries its own ``rubric``; the judge model (routed through
``chat_optimizer`` so it shares the optimizer backend configuration) reads
the task, the agent's response, and a listing of files produced in the
work_dir, then returns a strict JSON verdict.  Parsing is tolerant, retries
once on malformed output, and never raises: unjudgeable results surface as
``judge_error`` so the report can list them explicitly.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable

from skillopt.envs.skilleval.agentic_judge import AgenticJudgeConfig, run_agentic_judge
from skillopt.model import chat_optimizer

JUDGE_SYSTEM_PROMPT = (
    "You are a strict evaluator for agent task outputs. You are given a task, "
    "an acceptance rubric, the agent's final response, and a listing of files "
    "the agent produced (with the contents of produced text files, possibly "
    "truncated). Judge ONLY against the rubric, and only credit criteria the "
    "provided evidence verifies. Reply with ONLY a JSON "
    'object, no prose: {"pass": true|false, "score": <float 0.0-1.0>, '
    '"reason": "<short justification>"}. "pass" means the rubric is fully '
    'satisfied; "score" is partial credit toward the rubric.'
)

_RETRY_SUFFIX = (
    "\n\nYour previous reply was not valid JSON. Reply again with ONLY the "
    'JSON object {"pass": bool, "score": float, "reason": str}.'
)


def _find_balanced_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring of *text*, if any."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, len(text)):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start: pos + 1]
        start = text.find("{", start + 1)
    return None


def _extract_verdict(text: str) -> dict | None:
    """Tolerantly parse a judge verdict out of *text*.

    Tries raw JSON, then fence-stripped JSON, then the first balanced
    ``{...}`` block.  Returns ``None`` when no verdict with a boolean-able
    ``pass`` and numeric ``score`` can be recovered.
    """
    candidates = []
    stripped = (text or "").strip()
    if stripped:
        candidates.append(stripped)
        if stripped.startswith("```"):
            defenced = stripped.strip("`")
            if defenced.lower().startswith("json"):
                defenced = defenced[4:]
            candidates.append(defenced.strip())
        balanced = _find_balanced_object(stripped)
        if balanced:
            candidates.append(balanced)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "pass" not in data:
            continue
        try:
            score = float(data.get("score"))
        except (TypeError, ValueError):
            continue
        return {
            "pass": bool(data["pass"]),
            "score": min(1.0, max(0.0, score)),
            "reason": str(data.get("reason") or ""),
        }
    return None


def _build_judge_user_prompt(item: dict, response: str, artifacts_listing: str) -> str:
    return "\n\n".join([
        f"## Task\n{item['question']}",
        f"## Acceptance rubric\n{item['rubric']}",
        f"## Agent response\n{response}",
        f"## Files produced in the workspace\n{artifacts_listing or '(none)'}",
    ])


def artifacts_listing(work_dir: str) -> str:
    """List files the agent left in *work_dir* (relative path + size).

    Harness-internal files (hidden dirs, the task prompt) are skipped so the
    judge only sees artifacts the agent actually produced.
    """
    if not work_dir or not os.path.isdir(work_dir):
        return ""
    lines = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, work_dir)
            if rel.startswith((".agents", ".claude")) or rel == "task.md":
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            lines.append(f"{rel} ({size} bytes)")
    return "\n".join(lines)


def artifacts_excerpts(
    work_dir: str,
    exclude_rel: Iterable[str] = (),
    *,
    per_file_chars: int = 2000,
    max_files: int = 8,
    max_total_chars: int = 10000,
) -> str:
    """Contents of agent-produced text files, for the judge's evidence.

    A rubric usually constrains what the agent *writes*, not just that a file
    exists — a judge that only sees a name/size listing cannot verify those
    criteria and will (correctly) refuse to credit them. Walks *work_dir* with
    the same skips as ``artifacts_listing``, additionally excluding
    *exclude_rel* (task-seeded input files) and binary files. Truncation is
    always marked, never silent.
    """
    if not work_dir or not os.path.isdir(work_dir):
        return ""
    excluded = {os.path.normpath(str(rel)) for rel in (exclude_rel or ())}
    blocks: list[str] = []
    total = 0
    skipped = 0
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, work_dir)
            if rel.startswith((".agents", ".claude")) or rel == "task.md":
                continue
            if os.path.normpath(rel) in excluded:
                continue
            if len(blocks) >= max_files or total >= max_total_chars:
                skipped += 1
                continue
            try:
                with open(full, "rb") as f:
                    raw = f.read(per_file_chars * 4)
                size = os.path.getsize(full)
            except OSError:
                continue
            if b"\x00" in raw:
                continue  # binary — the listing still shows it
            decoded = raw.decode("utf-8", errors="replace")
            text = decoded[:per_file_chars][: max(0, max_total_chars - total)]
            header = f"--- {rel}"
            if len(raw) < size or len(text) < len(decoded):
                header += f" (truncated: first {len(text)} chars of {size} bytes)"
            header += " ---"
            blocks.append(f"{header}\n{text}")
            total += len(text)
    if skipped:
        blocks.append(f"... {skipped} more file(s) not shown")
    return "\n\n".join(blocks)


def merge_scores(items: list[dict], rollout_results: list[dict], judge_fn) -> list[dict]:
    """Merge rollout results with judge verdicts; errored tasks skip the judge."""
    merged = []
    for item, rollout_result in zip(items, rollout_results):
        result = dict(rollout_result)
        if result.get("score_valid") is False:
            result.update({
                "hard": 0,
                "soft": 0.0,
                "judge_reason": "",
                "judge_skipped": "invalid_rollout",
            })
        elif result.get("error"):
            result.update({"hard": 0, "soft": 0.0, "judge_reason": ""})
        else:
            work_dir = result.get("work_dir", "")
            listing = artifacts_listing(work_dir)
            excerpts = artifacts_excerpts(work_dir, exclude_rel=(item.get("files") or {}).keys())
            if excerpts:
                listing = (f"{listing}\n\n"
                           f"Contents of agent-produced text files:\n{excerpts}")
            verdict = judge_fn(item, result.get("response", ""), listing)
            result.update(verdict)
        merged.append(result)
    return merged


def judge(item: dict, response: str, artifacts_listing: str = "") -> dict:
    """Score one agent *response* against *item*'s rubric via the judge model.

    Never raises.  Returns a result fragment with ``id``, ``hard``, ``soft``,
    ``judge_reason`` and, when applicable, ``judge_skipped`` / ``judge_error``.
    """
    result = {
        "id": str(item["id"]),
        "hard": 0,
        "soft": 0.0,
        "judge_reason": "",
    }
    if not (response or "").strip():
        result["judge_skipped"] = "empty_response"
        return result

    user_prompt = _build_judge_user_prompt(item, response, artifacts_listing)
    last_error = "no response"
    judge_usage = {"input": 0, "output": 0}
    judge_calls = 0
    for attempt in range(2):
        prompt = user_prompt if attempt == 0 else user_prompt + _RETRY_SUFFIX
        try:
            reply, usage = chat_optimizer(system=JUDGE_SYSTEM_PROMPT, user=prompt, stage="skilleval_judge")
        except Exception as exc:  # noqa: BLE001 — judge must never crash the batch
            last_error = f"judge call failed: {type(exc).__name__}: {exc}"
            continue
        judge_calls += 1
        if isinstance(usage, dict):
            judge_usage["input"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            judge_usage["output"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        verdict = _extract_verdict(reply)
        if verdict is not None:
            result["hard"] = int(verdict["pass"])
            result["soft"] = verdict["score"]
            result["judge_reason"] = verdict["reason"]
            if judge_calls:
                result["judge_usage"] = judge_usage
            return result
        last_error = f"unparseable judge reply: {reply[:200]!r}"

    result["judge_error"] = last_error
    if judge_calls:
        result["judge_usage"] = judge_usage
    return result


# ---------------------------------------------------------------------------
# Routing-aware evaluation: chat judge for text tasks, agentic judge for
# binary-artifact tasks (see agentic_judge.py for the restricted sandbox
# client itself).
# ---------------------------------------------------------------------------

# Mirrors the supported binary kinds artifacts.py detects from real file
# bytes (inspectors/__init__.py keeps its own such copy too). Duplicated
# here, deliberately: a routing decision must be made from declared
# artifact/check metadata alone -- often before, or without ever needing,
# the real file on disk (a missing required binary output must still route
# to the agentic judge so it is scored as a failure, not silently chat-judged).
_ROUTING_SUFFIX_KINDS = {
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".docx": "docx",
    ".doc": "doc",
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".pptx": "pptx",
    ".ppt": "ppt",
}
_ROUTING_SUPPORTED_KINDS = frozenset(_ROUTING_SUFFIX_KINDS.values())
_ARTIFACT_ACTIVE_CHANGES = frozenset({"created", "modified"})


def _supported_kind_for_path(path: object) -> str | None:
    suffix = os.path.splitext(str(path or ""))[1].lower()
    return _ROUTING_SUFFIX_KINDS.get(suffix)


def _has_supported_binary_artifact(result: dict) -> bool:
    for row in result.get("artifacts") or []:
        if not isinstance(row, dict) or row.get("change") not in _ARTIFACT_ACTIVE_CHANGES:
            continue
        kind = row.get("kind") or _supported_kind_for_path(row.get("path"))
        if kind in _ROUTING_SUPPORTED_KINDS:
            return True
    return False


def _has_supported_binary_check(item: dict) -> bool:
    for check in item.get("artifact_checks") or []:
        if isinstance(check, dict) and _supported_kind_for_path(check.get("path")) is not None:
            return True
    return False


def should_use_agentic(item: dict, result: dict, judge_config: AgenticJudgeConfig | None) -> bool:
    """Resolve whether *item* should be scored by the agentic judge.

    An explicit per-task ``judge_mode`` (set only when the task document
    itself specified the field -- see ``contracts.normalize_judge_contract``,
    whose ``mode_explicit`` flag survives as ``item["_judge_mode_explicit"]``)
    always wins over the environment default. Absent that, the environment
    default is ``judge_config.mode`` when a judge is configured, else
    ``"auto"``. ``auto`` routes to the agentic judge when a produced/modified
    artifact resolves to a supported binary kind, or when a structured
    artifact check names a supported binary path -- so a missing required
    binary output becomes a scoreable ``artifact_failure`` rather than a
    chat judgment that cannot verify it. Unknown binary formats do not route.
    """
    mode = (
        item["judge_mode"]
        if item.get("_judge_mode_explicit")
        else (judge_config.mode if judge_config is not None else "auto")
    )
    if mode == "chat":
        return False
    if mode == "agentic":
        return True
    return _has_supported_binary_artifact(result) or _has_supported_binary_check(item)


def invalid_result(task_id: str, reason: str) -> dict:
    """Fail-closed fragment for a task that needed the agentic judge but has none configured."""
    return {
        "id": str(task_id),
        "hard": 0,
        "soft": 0.0,
        "judge_reason": "",
        "judge_mode": "agentic",
        "judge_status": "evaluation_error",
        "judge_criteria": [],
        "judge_coverage": {},
        "judge_usage": {"input": 0, "output": 0},
        "judge_cache_hit": False,
        "judge_error": reason,
        "score_valid": False,
    }


def _run_chat_judge(item: dict, result: dict, chat_judge) -> dict:
    """Run the chat judge, converting call failures/unparseable replies into
    ``evaluation_error`` fragments with ``score_valid=False`` -- never the
    legacy zero score a failed rubric would produce.
    """
    work_dir = result.get("work_dir", "")
    listing = artifacts_listing(work_dir)
    excerpts = artifacts_excerpts(work_dir, exclude_rel=(item.get("files") or {}).keys())
    if excerpts:
        listing = f"{listing}\n\nContents of agent-produced text files:\n{excerpts}"
    try:
        verdict = chat_judge(item, result.get("response", ""), listing)
    except Exception as exc:  # noqa: BLE001 — the chat judge must never crash evaluate_rollouts
        return {
            "hard": 0,
            "soft": 0.0,
            "judge_reason": "",
            "judge_status": "evaluation_error",
            "score_valid": False,
            "judge_error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(verdict, dict):
        return {
            "hard": 0,
            "soft": 0.0,
            "judge_reason": "",
            "judge_status": "evaluation_error",
            "score_valid": False,
            "judge_error": "chat judge returned a non-dict verdict",
        }
    fragment = dict(verdict)
    if "judge_error" in fragment:
        fragment["judge_status"] = "evaluation_error"
        fragment["score_valid"] = False
    return fragment


def evaluate_rollouts(
    items: list[dict],
    rollout_results: list[dict],
    *,
    state_hash: str,
    out_root: str,
    judge_config: AgenticJudgeConfig | None,
    chat_judge=judge,
) -> list[dict]:
    """Score rollouts, routing binary-artifact tasks to the agentic judge.

    ``merge_scores`` remains the backward-compatible chat-only wrapper for
    callers that never see binary artifacts; this is the routing-aware entry
    point trainers/CLIs that support the agentic judge should call instead.

    A rollout that already carries ``score_valid=False`` (an infrastructure
    failure -- e.g. an artifact-collection error, which does not necessarily
    also set ``error``, see ``rollout.py``) is never routed to either judge:
    judging it would waste a call and, on the agentic path, would let the
    judge's own ``score_valid=True`` success stamp silently overwrite the
    infrastructure failure, turning it into a scored result and defeating the
    ``require_valid=True`` aggregation gate this feature depends on. Mirrors
    ``merge_scores``' pre-existing ``score_valid is False`` short-circuit,
    checked ahead of routing for the same reason.
    """
    if len(items) != len(rollout_results):
        raise ValueError("item/result length mismatch")
    merged = []
    for item, rollout_result in zip(items, rollout_results):
        result = dict(rollout_result)
        if result.get("score_valid") is False:
            result.update({
                "hard": 0,
                "soft": 0.0,
                "judge_reason": "",
                "judge_skipped": "invalid_rollout",
                "judge_status": "evaluation_error",
            })
        elif result.get("error"):
            result.update({
                "hard": 0, "soft": 0.0, "judge_reason": "",
                "judge_status": "artifact_failure", "score_valid": True,
            })
        elif should_use_agentic(item, result, judge_config):
            if judge_config is None:
                result.update(invalid_result(item["id"], "agentic judge is not configured"))
            else:
                result.update(run_agentic_judge(
                    item=item,
                    rollout_result=result,
                    state_hash=state_hash,
                    out_root=out_root,
                    config=judge_config,
                ))
        else:
            result.update(_run_chat_judge(item, result, chat_judge))
            result.setdefault("judge_mode", "chat")
            result.setdefault("judge_status", "valid_pass" if result.get("hard") else "valid_fail")
            result.setdefault("score_valid", "judge_error" not in result)
        merged.append(result)
    return merged
