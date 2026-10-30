"""Exec-agent harness: workspace seeding, prompt wrappers, transcript helpers.

LAUNCHPAD PATCH (D16 / N13): the former Claude Code / Codex executors (CLI + SDK
paths, their judge transports and structured-output plumbing) are deleted, not
flagged. The one exec backend is OpenCode — ``skillopt.model.opencode_backend`` —
and ``run_target_exec`` dispatches to it. What stays here is everything that is NOT
about a particular CLI: ``prepare_workspace`` / ``render_skill_md`` (the seeded
work_dir every rollout gets), the prompt wrappers, artifact persistence next to the
work_dir, and the transcript readers (``extract_exec_usage`` /
``extract_exec_failure``) the pipelines consume. The module keeps its historical
name so the vendored import graph stays diffable against upstream.
"""
from __future__ import annotations

import json
import os
import re
import shutil

from skillopt.model.backend_config import get_target_backend
from skillopt.model.opencode_backend import OPENCODE_BACKEND, run_opencode_exec


def render_skill_md(
    skill_content: str,
    *,
    name: str = "skillopt-target",
    description: str = "Dynamic ReflACT skill for the current benchmark task.",
    preamble: str = "",
) -> str:
    body = skill_content.strip() or "No additional dynamic guidance was provided for this task."
    chunks = [
        "---",
        f'name: "{name}"',
        f'description: "{description}"',
        "---",
        "",
        "# ReflACT Target Skill",
        "",
    ]
    if preamble.strip():
        chunks.append(preamble.strip())
        chunks.append("")
    chunks.extend([
        "## Dynamic Guidance",
        "",
        body,
        "",
    ])
    return "\n".join(chunks)


def _workspace_target(work_dir: str, rel_path: str, *, label: str) -> str:
    if (
        not isinstance(rel_path, str)
        or not rel_path
        or rel_path.startswith(("~", "/", "\\"))
        or "\\" in rel_path
        or os.path.isabs(rel_path)
        or any(part in ("", ".", "..") for part in rel_path.split("/"))
    ):
        raise ValueError(f"{label} must be a safe relative path: {rel_path!r}")
    root = os.path.abspath(work_dir)
    target = os.path.abspath(os.path.join(root, os.path.normpath(rel_path)))
    if os.path.commonpath((root, target)) != root:
        raise ValueError(f"{label} escapes the workspace: {rel_path!r}")
    return target


def _reserve_workspace_target(
    work_dir: str,
    rel_path: str,
    *,
    label: str,
    reserved: dict[str, str],
) -> str:
    target = _workspace_target(work_dir, rel_path, label=label)
    for other, other_label in reserved.items():
        if (
            target == other
            or target.startswith(other + os.sep)
            or other.startswith(target + os.sep)
        ):
            raise ValueError(
                f"{label} collides with {other_label}: {rel_path!r}"
            )
    reserved[target] = label
    return target


def prepare_workspace(
    *,
    work_dir: str,
    skill_md: str,
    task_text: str = "",
    task_filename: str = "task.md",
    images: list[str] | None = None,
    extra_files: dict[str, str] | None = None,
    copy_files: list[tuple[str, str]] | None = None,
    link_dirs: list[tuple[str, str]] | None = None,
    installed_skills: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    reserved: dict[str, str] = {}
    skill_writes: list[tuple[str, str]] = []
    if installed_skills is not None:
        if not installed_skills:
            raise ValueError("installed_skills must not be empty")
        seen_names: set[str] = set()
        for name, content in installed_skills:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
                raise ValueError(f"invalid installed skill name: {name!r}")
            if name in seen_names:
                raise ValueError(f"duplicate installed skill name: {name!r}")
            seen_names.add(name)
            rel_path = os.path.join(".agents", "skills", name, "SKILL.md")
            path = _reserve_workspace_target(
                work_dir,
                rel_path,
                label=f"installed skill {name!r}",
                reserved=reserved,
            )
            skill_writes.append((path, content))
        skill_path = skill_writes[0][0]
    else:
        skill_path = _reserve_workspace_target(
            work_dir,
            os.path.join(".agents", "skills", "skillopt-target", "SKILL.md"),
            label="target skill",
            reserved=reserved,
        )
        skill_writes.append((skill_path, skill_md))

    task_path = _reserve_workspace_target(
        work_dir,
        task_filename,
        label="task file",
        reserved=reserved,
    )
    extra_writes = [
        (
            _reserve_workspace_target(
                work_dir,
                rel_path,
                label=f"extra file {rel_path!r}",
                reserved=reserved,
            ),
            content,
        )
        for rel_path, content in (extra_files or {}).items()
    ]
    copy_writes = [
        (
            src,
            _reserve_workspace_target(
                work_dir,
                rel_dst,
                label=f"copied file {rel_dst!r}",
                reserved=reserved,
            ),
        )
        for src, rel_dst in (copy_files or [])
    ]
    link_writes = [
        (
            src,
            _reserve_workspace_target(
                work_dir,
                rel_dst,
                label=f"linked directory {rel_dst!r}",
                reserved=reserved,
            ),
        )
        for src, rel_dst in (link_dirs or [])
    ]

    image_writes: list[tuple[str, str, str]] = []
    if images:
        for index, image in enumerate(images, 1):
            if not os.path.exists(image):
                raise FileNotFoundError(image)
            src = os.path.abspath(image)
            base = os.path.basename(src) or f"image_{index}"
            rel_dst = os.path.join("attachments", f"{index:02d}_{base}")
            dst = _reserve_workspace_target(
                work_dir,
                rel_dst,
                label=f"attachment {index}",
                reserved=reserved,
            )
            image_writes.append((src, dst, rel_dst))
        attachments_manifest = _reserve_workspace_target(
            work_dir,
            "ATTACHMENTS.md",
            label="attachments manifest",
            reserved=reserved,
        )
    else:
        attachments_manifest = ""

    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)
    for path, content in skill_writes:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    if task_text:
        os.makedirs(os.path.dirname(task_path), exist_ok=True)
        with open(task_path, "w", encoding="utf-8") as f:
            f.write(task_text)

    for full_path, content in extra_writes:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

    for src, dst in copy_writes:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    for src, dst in link_writes:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.symlink(os.path.abspath(src), dst)

    attachment_lines: list[str] = []
    for src, dst, rel_dst in image_writes:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        attachment_lines.append(f"- `{rel_dst}` (source: `{src}`)")

    if attachment_lines:
        with open(attachments_manifest, "w", encoding="utf-8") as f:
            f.write(
                "# Attachments\n\n"
                "Use these local files when the task refers to attached images or documents.\n\n"
                + "\n".join(attachment_lines)
                + "\n"
            )

    return skill_path, task_path


def _persist_artifacts(
    *,
    work_dir: str,
    raw: str,
    response: str,
    prefix: str,
    summary_builder,
) -> None:
    pred_dir = os.path.dirname(work_dir.rstrip(os.sep))
    raw_path = os.path.join(pred_dir, f"{prefix}_raw.txt")
    summary_path = os.path.join(pred_dir, f"{prefix}_trace_summary.txt")

    combined_raw = raw
    if os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as f:
            prev = f.read()
        combined_raw = f"{prev}\n\n===== TURN BREAK =====\n\n{raw}" if prev.strip() else raw

    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(combined_raw)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary_builder(combined_raw, response))


def _exec_prompt(prompt: str, *, allow_file_edits: bool = False) -> str:
    edit_instruction = (
        "You may modify files in the workspace when the task asks you to create an artifact. "
        if allow_file_edits
        else "Do not modify files. "
    )
    return (
        "Use the workspace files to solve the task. Read task.md and the skill at "
        ".agents/skills/skillopt-target/SKILL.md before answering. "
        "If ATTACHMENTS.md exists, read it and inspect the listed local files. "
        "Do not call a Skill tool; the ReflACT guidance is a local markdown file. "
        f"Do not ask for permission. {edit_instruction}"
        "Return only the final answer text, keeping any required <answer>...</answer> tags exactly.\n\n"
        f"{_normalize_target_exec_prompt(prompt)}"
    )


def _retry_prompt(prompt: str, attempt: int) -> str:
    if attempt <= 0:
        return prompt
    return (
        f"{prompt}\n\n"
        "Previous execution returned an empty final response. Re-read task.md and "
        ".agents/skills/skillopt-target/SKILL.md. If ATTACHMENTS.md exists, use the listed files. "
        "Then produce the final answer inside <answer>...</answer>."
    )


def _normalize_target_exec_prompt(prompt: str) -> str:
    """Avoid wording that makes the exec agent call an unregistered Skill tool."""
    text = prompt or ""
    replacements = {
        "Use the `skillopt-target` skill available in this workspace.": (
            "Read `.agents/skills/skillopt-target/SKILL.md` directly; do not call a Skill tool."
        ),
        "- Use the local `skillopt-target` skill before writing code.": (
            "- Read `.agents/skills/skillopt-target/SKILL.md` before writing code; do not call a Skill tool."
        ),
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text



def extract_exec_failure(raw: str) -> str | None:
    """Extract concise per-attempt failure reasons from an exec transcript."""
    if not (raw or "").strip():
        return None

    attempt = ""
    reasons: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        match = re.fullmatch(r"===== (.+?) ATTEMPT (\d+) =====", stripped)
        if match:
            attempt = f"{match.group(1)} attempt {match.group(2)}"
            continue
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue

        reason = ""
        if payload.get("type") == "exec_error":
            reason = str(payload.get("error") or "").strip()
        elif payload.get("is_error"):
            reason = str(
                payload.get("result")
                or payload.get("error")
                or payload.get("parse_error")
                or ""
            ).strip()
        if not reason:
            continue

        reason = re.sub(r"\s+", " ", reason)
        if len(reason) > 1000:
            reason = reason[:997] + "..."
        label = attempt or str(payload.get("backend") or "").strip()
        detail = f"{label}: {reason}" if label else reason
        if detail not in reasons:
            reasons.append(detail)

    return "; ".join(reasons) if reasons else None


def extract_exec_usage(raw: str) -> dict | None:
    """Normalize token usage out of a persisted exec *raw* transcript.

    Reads the ``"input_tokens" / "cache_creation_input_tokens" /
    "cache_read_input_tokens" / "output_tokens"`` group the OpenCode backend
    appends per attempt (``opencode_backend.usage_line``), accumulating across
    attempt/turn segments (the last complete group of a segment wins).
    Returns ``{"input", "cache_write", "cache_read", "output", "total"}`` or
    None when no usage is present. Never raises.
    """
    if not (raw or "").strip():
        return None
    try:
        totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "total": 0}
        found = False
        segments = re.split(r"===== .*? =====|\n===== TURN BREAK =====\n", raw)
        group_re = re.compile(
            r'"input_tokens"\s*:\s*(\d+).{0,400}?'
            r'"cache_creation_input_tokens"\s*:\s*(\d+).{0,400}?'
            r'"cache_read_input_tokens"\s*:\s*(\d+).{0,400}?'
            r'"output_tokens"\s*:\s*(\d+)',
            re.DOTALL,
        )
        for segment in segments:
            groups = group_re.findall(segment)
            if not groups:
                continue
            input_t, cache_write, cache_read, output_t = (int(v) for v in groups[-1])
            totals["input"] += input_t
            totals["cache_write"] += cache_write
            totals["cache_read"] += cache_read
            totals["output"] += output_t
            totals["total"] += input_t + cache_write + cache_read + output_t
            found = True
        return totals if found else None
    except Exception:  # noqa: BLE001 — usage extraction must never break a rollout
        return None


def run_target_exec(
    *,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    images: list[str] | None = None,
    data_dirs: list[str] | None = None,
    allowed_tools: list[str] | str | None = None,
    permission_mode: str | None = None,
    allow_file_edits: bool = False,
) -> tuple[str, str]:
    backend = get_target_backend()
    if backend == OPENCODE_BACKEND:
        return run_opencode_exec(
            work_dir=work_dir,
            prompt=prompt,
            model=model,
            timeout=timeout,
            images=images,
            data_dirs=data_dirs,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
            allow_file_edits=allow_file_edits,
        )
    raise ValueError(f"Unsupported exec backend: {backend}")
