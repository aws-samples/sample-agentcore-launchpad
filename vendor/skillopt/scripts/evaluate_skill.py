#!/usr/bin/env python3
"""SkillOpt skilleval: evaluate an arbitrary skill on a custom task set.

Runs a user-provided SKILL.md inside Claude Code CLI on each task of a
user-provided task file (JSON array / JSONL with id/question/rubric), scores
every response with an LLM judge against the task's rubric, and writes
``results.json`` + ``report.md``.

Usage
-----
    python3 scripts/evaluate_skill.py \
        --skill ~/.claude/skills/my-skill/SKILL.md \
        --tasks data/my_tasks.json \
        --out_root outputs/skilleval_myskill

Backend configuration follows the same environment conventions as train.py /
eval_only.py (AZURE_OPENAI_*, ANTHROPIC_*, etc.); the target backend defaults
to ``claude_code_exec`` and the judge uses the optimizer backend.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys

import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from skillopt.envs.skilleval.agentic_judge import preflight_agentic_judge
from skillopt.envs.skilleval.contracts import JUDGE_MODES
from skillopt.envs.skilleval.dataloader import load_tasks
from skillopt.envs.skilleval.inspectors import EvaluationError
from skillopt.envs.skilleval.evaluator import (  # noqa: F401 — merge_scores re-exported for tests/importers
    AgenticJudgeConfig,
    artifacts_listing,
    evaluate_rollouts,
    judge,
    merge_scores,
)
from skillopt.envs.skilleval.plugin import (
    _collect_skill as _collect_skill_source,
    aggregate_results,
    collect_runtime_skills,
    normalize_plugin_tasks,
    skill_name as _skill_name,
)
from skillopt.envs.skilleval.rollout import run_batch
from skillopt.model import (
    configure_claude_code_exec,
    configure_codex_exec,
    set_optimizer_backend,
    set_optimizer_deployment,
    set_target_backend,
    set_target_deployment,
)
from skillopt.model.common import default_model_for_backend


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SkillOpt skilleval — evaluate one or more custom skills")
    p.add_argument("--skill", type=str, required=True, action="append",
                   help="Skill to evaluate: a markdown file, or a skill directory "
                        "containing SKILL.md (supporting files are copied along)")
    p.add_argument("--tasks", type=str, required=True,
                   help="Task file (JSON array or JSONL; id/question/rubric per item)")
    p.add_argument("--out_root", type=str, required=True,
                   help="Output directory for results.json / report.md / rollouts/")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--timeout", type=int, default=600,
                   help="Per-task Claude Code timeout in seconds")
    p.add_argument("--limit", type=int, default=0,
                   help="Only run the first N tasks (0 = all)")
    p.add_argument("--model", type=str, default="",
                   help="Target model override for claude_code_exec")
    p.add_argument("--target_backend", type=str, default="claude_code_exec")
    p.add_argument("--optimizer_backend", type=str, default="openai_chat",
                   help="Judge backend")
    p.add_argument("--optimizer_model", type=str, default="",
                   help="Judge model override")
    p.add_argument("--claude_code_exec_path", type=str, default="claude")
    p.add_argument("--claude_code_exec_effort", type=str, default="medium")
    # ── Agentic binary judge (independent of the target backend/model) ──────
    p.add_argument("--judge_mode", type=str, default="auto",
                   help="auto | agentic | chat (routes binary-artifact tasks to the agentic judge)")
    p.add_argument("--judge_exec_backend", type=str, default="claude_code_exec",
                   help="Judge exec backend: claude_code_exec or codex_exec")
    p.add_argument("--judge_exec_model", type=str, default="",
                   help="Judge exec model override (blank = backend default)")
    p.add_argument("--judge_exec_timeout", type=int, default=300)
    p.add_argument("--judge_exec_effort", type=str, default="low")
    p.add_argument("--judge_cache", action=argparse.BooleanOptionalAction, default=True)
    # SKILLOPT_JUDGE_SANDBOX lets a deployment (e.g. SkillOpt Studio, whose
    # eval runner passes no judge flags) pick the launcher via .env — needed on
    # AppArmor hosts where unprivileged bwrap is blocked and the launcher must
    # be an elevated "sudo -n bwrap". An explicit flag still wins over the env.
    p.add_argument("--judge_sandbox_command",
                   default=os.environ.get("SKILLOPT_JUDGE_SANDBOX", "bwrap"),
                   help="Trusted sandbox launcher argv (shlex-split; never run through a shell; "
                        "default from $SKILLOPT_JUDGE_SANDBOX when set)")
    p.add_argument("--judge_max_evidence_bytes", type=int, default=536_870_912)
    p.add_argument("--judge_max_scratch_bytes", type=int, default=1_073_741_824)
    p.add_argument("--judge_max_render_pixels", type=int, default=500_000_000)
    return p.parse_args()


def _collect_skill(path: str) -> tuple[str, list[tuple[str, str]]]:
    """Compatibility wrapper around the shared Plugin Skill collector."""
    try:
        content, files, _source_dir = _collect_skill_source(path)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    return content, files


def _read_skill_file(path: str) -> str:
    """Compatibility helper retained for importers of this CLI module."""
    try:
        content, _files, _source_dir = _collect_skill_source(path)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    return content


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _is_invalid(result: dict) -> bool:
    """A row whose score is infrastructure-invalid (score_valid explicitly False)."""
    return result.get("score_valid") is False


def build_report(results: list[dict]) -> str:
    """Render the human-readable evaluation report (pure function).

    Invalid rows (``score_valid is False`` -- sandbox/probe/inspector/worker
    infrastructure failures) are excluded from the hard/soft denominators and
    listed separately, never counted as legitimate zero scores. Agentic rows
    additionally surface their criterion evidence and coverage. Legacy text
    tasks (no ``score_valid``/``judge_status``) keep every prior field and
    heading unchanged.
    """
    total = len(results)
    scored = [r for r in results if not _is_invalid(r)]
    invalid = [r for r in results if _is_invalid(r)]
    pass_rate = _mean([float(r.get("hard", 0)) for r in scored])
    soft_mean = _mean([float(r.get("soft", 0.0)) for r in scored])
    total_duration = sum(float(r.get("duration_s", 0.0)) for r in results)

    lines = [
        "# Skill Evaluation Report",
        "",
        "## Summary",
        "",
        f"- Tasks: {total}",
        f"- Scored tasks: {len(scored)}",
        f"- Invalid evaluations: {len(invalid)}",
        f"- Pass rate (hard): {pass_rate:.1%}",
        f"- Soft score mean: {soft_mean:.3f}",
        "",
    ]

    # Per-task_type breakdown (means over scored rows only)
    by_type: dict[str, list[dict]] = {}
    for r in results:
        by_type.setdefault(str(r.get("task_type", "default")), []).append(r)
    if by_type:
        lines += ["## By task type", "",
                  "| task_type | tasks | pass rate | soft mean |",
                  "|---|---|---|---|"]
        for task_type in sorted(by_type):
            group = by_type[task_type]
            group_scored = [r for r in group if not _is_invalid(r)]
            lines.append(
                f"| {task_type} | {len(group)} "
                f"| {_mean([float(r.get('hard', 0)) for r in group_scored]):.1%} "
                f"| {_mean([float(r.get('soft', 0.0)) for r in group_scored]):.3f} |"
            )
        lines.append("")

    # Per-task detail
    lines += ["## Tasks", "",
              "| id | pass | soft | reason | duration (s) |",
              "|---|---|---|---|---|"]
    for r in results:
        skipped = r.get("judge_skipped")
        reason_value = (
            r.get("error")
            or r.get("judge_error")
            or r.get("judge_reason")
            or (f"judge skipped: {skipped}" if skipped else "")
        )
        reason = str(reason_value).replace("|", "\\|").replace("\n", " ")
        if len(reason) > 80:
            reason = reason[:77] + "..."
        mark = "invalid" if _is_invalid(r) else ("✓" if r.get("hard") else "✗")
        lines.append(
            f"| {r.get('id')} | {mark} | {float(r.get('soft', 0.0)):.2f} "
            f"| {reason} | {float(r.get('duration_s', 0.0)):.1f} |"
        )
    lines.append("")

    # Agentic judge evidence: per-criterion evidence + inspection coverage
    agentic = [
        r for r in results
        if r.get("judge_mode") == "agentic" or r.get("judge_criteria") or r.get("judge_coverage")
    ]
    if agentic:
        lines += ["## Agentic judge evidence", ""]
        for r in agentic:
            lines.append(f"### `{r.get('id')}`")
            criteria = r.get("judge_criteria") or []
            if criteria:
                lines += ["", "| criterion | passed | score | evidence |", "|---|---|---|---|"]
                for c in criteria:
                    evidence = "; ".join(
                        f"{e.get('path', '')}@{e.get('locator', '')}"
                        for e in (c.get("evidence") or [])
                    ) or "(none)"
                    evidence = evidence.replace("|", "\\|")
                    passed = "✓" if c.get("passed") else "✗"
                    lines.append(
                        f"| {c.get('id')} | {passed} | {float(c.get('score', 0.0)):.2f} | {evidence} |"
                    )
            coverage = r.get("judge_coverage") or {}
            if coverage:
                lines += [
                    "",
                    f"- Coverage artifacts: {', '.join(coverage.get('artifacts', []) or []) or '(none)'}",
                    f"- Units inspected: {', '.join(coverage.get('units_inspected', []) or []) or '(none)'}",
                    f"- Units omitted: {', '.join(coverage.get('units_omitted', []) or []) or '(none)'}",
                ]
            lines.append("")

    # Cost
    lines += ["## Cost", "",
              f"- Total duration: {total_duration:.1f}s",
              f"- Mean duration per task: {_mean([float(r.get('duration_s', 0.0)) for r in results]):.1f}s",
              "- Token usage: n/a (not parsed in minimal version)",
              ""]

    # Failures (scored rows only). Invalid rows are reported separately below.
    errored = [r for r in results if r.get("error") and not _is_invalid(r)]
    judge_errored = [r for r in results if r.get("judge_error") and not _is_invalid(r)]
    lines += ["## Failures", ""]
    if not errored and not judge_errored:
        lines.append("none")
    if errored:
        lines.append("### Rollout errors (scored 0, judge skipped)")
        lines += [f"- `{r['id']}`: {r['error']}" for r in errored]
    if judge_errored:
        lines.append("### Judge errors (scored 0, verdict unavailable)")
        lines += [f"- `{r['id']}`: {r['judge_error']}" for r in judge_errored]
    lines.append("")

    # Invalid evaluations (excluded from scoring; the gate must not score them).
    if invalid:
        lines += ["## Invalid evaluations", ""]
        for r in invalid:
            status = r.get("judge_status", "evaluation_error")
            detail = str(r.get("judge_error") or r.get("error") or "").replace("\n", " ")
            lines.append(f"- `{r.get('id')}`: {status} — {detail}")
        lines.append("")

    return "\n".join(lines)


def _configure_backends(args: argparse.Namespace) -> None:
    set_target_backend(args.target_backend)
    set_optimizer_backend(args.optimizer_backend)
    if args.model:
        set_target_deployment(args.model)
    else:
        set_target_deployment(default_model_for_backend(args.target_backend))
    if args.optimizer_model:
        set_optimizer_deployment(args.optimizer_model)
    else:
        set_optimizer_deployment(default_model_for_backend(args.optimizer_backend))
    # Configure both exec backends: the target rollout and the agentic judge may
    # use different exec backends, and the judge backend/model are independent of
    # the target backend/model. (The judge worker re-pins its own CLI transport
    # and effort per call; here we just make each backend's globals available.)
    configure_claude_code_exec(
        path=args.claude_code_exec_path,
        effort=args.claude_code_exec_effort,
    )
    configure_codex_exec(use_sdk="cli")


def _build_judge_config(args: argparse.Namespace) -> AgenticJudgeConfig:
    """Build the independent agentic-judge config from CLI flags (fail fast).

    Invariant: the trusted sandbox launcher is a real argv vector -- shlex-split
    exactly once here, rejected if empty, and never passed through a shell.
    """
    if args.judge_mode not in JUDGE_MODES:
        sys.exit(f"error: --judge_mode must be one of {sorted(JUDGE_MODES)}: {args.judge_mode!r}")
    sandbox_command = shlex.split(args.judge_sandbox_command)
    if not sandbox_command:
        sys.exit("error: --judge_sandbox_command must not be an empty sandbox launcher")
    try:
        return AgenticJudgeConfig(
            mode=args.judge_mode,
            backend=args.judge_exec_backend,
            model=args.judge_exec_model,
            timeout=args.judge_exec_timeout,
            effort=args.judge_exec_effort,
            cache=args.judge_cache,
            sandbox_command=tuple(sandbox_command),
            max_evidence_bytes=args.judge_max_evidence_bytes,
            max_scratch_bytes=args.judge_max_scratch_bytes,
            max_render_pixels=args.judge_max_render_pixels,
        )
    except ValueError as exc:
        sys.exit(f"error: invalid judge configuration: {exc}")


def _runtime_state_hash(runtime_skills: list[dict]) -> str:
    """Deterministic cache-scoping hash of the ordered (name, content) skills."""
    digest = hashlib.sha256()
    for skill in runtime_skills:
        digest.update(str(skill["name"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(skill["content"]).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def main() -> None:
    args = parse_args()
    try:
        runtime_skills = collect_runtime_skills(args.skill)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        sys.exit(f"error: invalid skill: {exc}")
    plugin_mode = len(runtime_skills) > 1
    primary = runtime_skills[0]
    try:
        items = load_tasks(args.tasks)
        normalize_plugin_tasks(items, {skill["name"] for skill in runtime_skills})
        if args.limit and args.limit > 0:
            items = items[:args.limit]
    except (ValueError, OSError) as exc:
        sys.exit(f"error: invalid tasks file: {exc}")

    judge_config = _build_judge_config(args)
    _configure_backends(args)
    if judge_config.mode == "agentic":
        # Explicit agentic mode: preflight the whole judge environment (backend
        # executable, sandbox usability, MCP init, format tools) BEFORE any
        # rollout spend. Auto mode stays lazy and validates when the first
        # supported binary task is encountered.
        try:
            preflight_agentic_judge(judge_config, items)
        except EvaluationError as exc:
            sys.exit(f"error: agentic judge preflight failed: {exc}")
        print("[skilleval] agentic judge preflight: ok")
    os.makedirs(args.out_root, exist_ok=True)

    print(f"[skilleval] skills: {', '.join(skill['name'] for skill in runtime_skills)}")
    print(f"[skilleval] tasks: {len(items)} from {args.tasks}")

    rollout_results = run_batch(
        items,
        primary["content"],
        args.out_root,
        workers=args.workers,
        timeout=args.timeout,
        model=args.model,
        skill_files=primary["files"],
        runtime_skills=runtime_skills if plugin_mode else None,
    )

    print(f"[skilleval] judging {len(rollout_results)} responses")
    results = evaluate_rollouts(
        items,
        rollout_results,
        state_hash=_runtime_state_hash(runtime_skills),
        out_root=args.out_root,
        judge_config=judge_config,
        chat_judge=judge,
    )
    for item, result in zip(items, results):
        result["target_skills"] = list(item.get("target_skills") or [])
        result["task_type"] = item.get("task_type", "default")

    results_path = os.path.join(args.out_root, "results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    summary = aggregate_results(results, [skill["name"] for skill in runtime_skills])
    with open(os.path.join(args.out_root, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)

    report = build_report(results)
    report_path = os.path.join(args.out_root, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    passed = sum(1 for r in results if r.get("hard"))
    print(f"[skilleval] done: {passed}/{len(results)} passed")
    print(f"[skilleval] report: {report_path}")
    print(f"[skilleval] results: {results_path}")


if __name__ == "__main__":
    main()
