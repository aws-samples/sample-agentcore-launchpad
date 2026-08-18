"""Helpers for running exec backends as the target harness."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import traceback
from typing import Any

from skillopt.model import agentcore_runner
from skillopt.model.backend_config import (
    get_claude_code_exec_config,
    get_codex_exec_config,
    get_target_backend,
)


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "final_response": {
            "type": "string",
            "description": "The exact final answer text to return, preserving required <answer>...</answer> tags.",
        },
        "final_answer": {
            "type": "string",
            "description": "The concise answer value without explanation, if separable.",
        },
    },
    "required": ["final_response", "final_answer"],
    "additionalProperties": False,
}


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


def _build_codex_trace_summary(raw: str, response: str) -> str:
    lines = [ln.rstrip() for ln in (raw or "").splitlines()]

    def _find(prefix: str) -> str:
        for ln in lines:
            if ln.startswith(prefix):
                return ln[len(prefix):].strip()
        return ""

    sandbox = _find("sandbox: ")
    reasoning = _find("reasoning effort: ")
    task_read = "unknown"
    skill_read = "unknown"
    exec_errors: list[str] = []
    tokens_used = ""

    for idx, ln in enumerate(lines):
        if ln.startswith("exec"):
            cmd = lines[idx + 1] if idx + 1 < len(lines) else ""
            outcome = lines[idx + 2] if idx + 2 < len(lines) else ""
            joined = f"{cmd}\n{outcome}"
            if "task.md" in joined:
                if "succeeded" in outcome:
                    task_read = "success"
                elif "failed" in outcome or "ERROR" in outcome:
                    task_read = "failed"
            if "SKILL.md" in joined:
                if "succeeded" in outcome:
                    skill_read = "success"
                elif "failed" in outcome or "ERROR" in outcome:
                    skill_read = "failed"
        if ln.startswith("ERROR:"):
            exec_errors.append(ln[len("ERROR:"):].strip())
        if ln == "tokens used" and idx + 1 < len(lines):
            tokens_used = lines[idx + 1].strip()

    match = re.search(r"<answer>\s*([A-E])\s*</answer>", response or "", re.IGNORECASE)
    if match:
        answer_format = "well_formed"
        answer_label = match.group(1).upper()
    elif "<answer>" in (response or "").lower():
        answer_format = "tagged_nonlabel"
        answer_label = ""
    elif (response or "").strip():
        answer_format = "plain_text"
        answer_label = ""
    else:
        answer_format = "missing"
        answer_label = ""

    parts = ["Codex Trace Summary"]
    if sandbox:
        parts.append(f"- sandbox: {sandbox}")
    if reasoning:
        parts.append(f"- reasoning: {reasoning}")
    parts.append(f"- read task.md: {task_read}")
    parts.append(f"- read SKILL.md: {skill_read}")
    if exec_errors:
        parts.append(f"- shell/tool errors: {' | '.join(exec_errors[:3])}")
    else:
        parts.append("- shell/tool errors: none")
    parts.append(f"- final answer format: {answer_format}")
    parts.append(f"- final answer label: {answer_label or '(none)'}")
    if tokens_used:
        parts.append(f"- tokens used: {tokens_used}")
    return "\n".join(parts)


def _build_claude_trace_summary(raw: str, response: str) -> str:
    answer_format = "missing"
    if "<answer>" in (response or "").lower():
        answer_format = "tagged"
    elif (response or "").strip():
        answer_format = "plain_text"
    errors: list[str] = []
    for ln in (raw or "").splitlines():
        if "error" in ln.lower() or "traceback" in ln.lower():
            errors.append(ln.strip())
        if len(errors) >= 3:
            break
    parts = ["Claude Code Trace Summary", f"- final answer format: {answer_format}"]
    parts.append(f"- final response chars: {len(response or '')}")
    parts.append(f"- errors: {' | '.join(errors) if errors else 'none'}")
    return "\n".join(parts)


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


def _persist_codex_artifacts(work_dir: str, raw: str, response: str) -> None:
    _persist_artifacts(
        work_dir=work_dir,
        raw=raw,
        response=response,
        prefix="codex",
        summary_builder=_build_codex_trace_summary,
    )


def _persist_claude_artifacts(work_dir: str, raw: str, response: str) -> None:
    _persist_artifacts(
        work_dir=work_dir,
        raw=raw,
        response=response,
        prefix="claude",
        summary_builder=_build_claude_trace_summary,
    )


def parse_codex_raw(raw: str) -> dict:
    """Parse raw Codex CLI output into step sections.

    Returns a dict with:
    - ``steps``: ordered sections beginning at the first ``user/codex/exec`` marker
    - ``trace_body``: raw trace starting at the first marker
    """
    lines = (raw or "").splitlines()
    markers = {"user", "codex", "exec"}
    first_step_line: int | None = None
    for idx, line in enumerate(lines):
        if line in markers:
            first_step_line = idx
            break
    if first_step_line is None:
        return {"steps": [], "trace_body": ""}

    steps: list[dict] = []
    current: dict | None = None
    for idx in range(first_step_line, len(lines)):
        line = lines[idx]
        if line in markers:
            if current is not None:
                current["end_line"] = idx
                current["content"] = "\n".join(current["content_lines"]).strip()
                current.pop("content_lines", None)
                steps.append(current)
            current = {
                "index": len(steps) + 1,
                "type": line,
                "start_line": idx,
                "content_lines": [],
            }
            continue
        if current is not None:
            current["content_lines"].append(line)
    if current is not None:
        current["end_line"] = len(lines)
        current["content"] = "\n".join(current["content_lines"]).strip()
        current.pop("content_lines", None)
        steps.append(current)

    trace_body = "\n".join(lines[first_step_line:]).strip()
    return {"steps": steps, "trace_body": trace_body}


def format_codex_trace_steps(raw: str, *, max_chars: int = 4000) -> str:
    """Render parsed Codex trace into numbered compact steps for optimizer prompts."""
    parsed = parse_codex_raw(raw)
    steps = parsed["steps"]
    if not steps:
        return ""

    rendered: list[str] = []
    for step in steps:
        summary = ""
        content = str(step.get("content") or "").strip()
        if step["type"] == "exec":
            body_lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
            cmd = body_lines[0] if body_lines else ""
            status = ""
            for ln in body_lines[1:]:
                low = ln.lower()
                if "succeeded in" in low or "failed in" in low or "timed out" in low or low.startswith("error"):
                    status = ln
                    break
            summary = cmd
            if status:
                summary = f"{summary} | {status}" if summary else status
        else:
            summary = " ".join(content.splitlines())
        summary = summary[:500] if summary else "(empty)"
        rendered.append(f"[{step['index']}] {step['type']}: {summary}")

    text = "\n".join(rendered)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[trace steps truncated]..."
    return text


def extract_codex_trace_prefix(raw: str, *, after_step: int) -> str:
    """Return raw trace body up to and including ``after_step``.

    ``after_step <= 0`` yields an empty string.
    """
    if after_step <= 0:
        return ""
    parsed = parse_codex_raw(raw)
    steps = parsed["steps"]
    if not steps:
        return ""
    clamped = min(after_step, len(steps))
    lines = parsed["trace_body"].splitlines()
    end_line = int(steps[clamped - 1]["end_line"]) - int(steps[0]["start_line"])
    return "\n".join(lines[:end_line]).strip()


_DENIED_DATA_DIR_NAMES = {"officeqa_split", "sealqa_split"}


def _normalize_tools(allowed_tools: list[str] | str | None) -> str:
    if allowed_tools is None:
        return ""
    if isinstance(allowed_tools, str):
        return ",".join(part.strip() for part in allowed_tools.split(",") if part.strip())
    return ",".join(str(tool).strip() for tool in allowed_tools if str(tool).strip())


def _tools_list(allowed_tools: list[str] | str | None) -> list[str]:
    tools = _normalize_tools(allowed_tools)
    return [part.strip() for part in tools.split(",") if part.strip()]


def _validate_exec_path(path: str) -> str:
    resolved = os.path.realpath(os.path.abspath(path))
    parts = set(resolved.split(os.sep))
    denied = parts & _DENIED_DATA_DIR_NAMES
    if denied:
        raise ValueError(f"Refusing to expose denied data directory to exec backend: {', '.join(sorted(denied))}")
    return resolved


def _validated_add_dirs(work_dir: str, data_dirs: list[str] | None, images: list[str] | None) -> list[str]:
    add_dirs = [_validate_exec_path(work_dir)]
    for data_dir in data_dirs or []:
        add_dirs.append(_validate_exec_path(data_dir))
    for image in images or []:
        add_dirs.append(_validate_exec_path(os.path.dirname(image) or work_dir))
    deduped: list[str] = []
    for path in add_dirs:
        if path not in deduped:
            deduped.append(path)
    return deduped


def _sdk_mode(value: Any) -> str:
    mode = str(value or "auto").strip().lower()
    if mode in {"1", "true", "yes", "on", "sdk"}:
        return "sdk"
    if mode in {"0", "false", "no", "off", "cli"}:
        return "cli"
    return "auto"


def _claude_effort(value: Any) -> str:
    effort = str(value or "medium").strip().lower()
    if effort in {"", "none", "off"}:
        return ""
    if effort == "xhigh":
        return "max"
    if effort not in {"low", "medium", "high", "max"}:
        return "medium"
    return effort


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, (list, tuple)):
        return list(obj)
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["exception"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "exception" in box:
        raise box["exception"]
    return box.get("result")


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
    """Avoid wording that makes Claude Code call an unregistered Skill tool."""
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


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    strict = json.loads(json.dumps(schema))
    strict["additionalProperties"] = False
    properties = strict.get("properties") or {}
    strict["required"] = list(properties.keys())
    return strict


def _structured_response(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", f"Structured output was not an object: {type(data).__name__}"
    final_response = str(data.get("final_response") or "").strip()
    final_answer = str(data.get("final_answer") or "").strip()
    if final_response:
        return final_response, ""
    if final_answer:
        if "<answer>" in final_answer.lower():
            return final_answer, ""
        return f"<answer>{final_answer}</answer>", ""
    return "", "Structured output did not contain a final response."


def _extract_claude_structured_output(messages: list[Any]) -> Any:
    """Claude Code SDK can finish with error_during_execution after StructuredOutput."""
    for msg in reversed(messages):
        structured = getattr(msg, "structured_output", None)
        if isinstance(structured, dict):
            return structured

        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if not isinstance(content, list):
            continue

        for item in reversed(content):
            name = getattr(item, "name", None)
            payload = getattr(item, "input", None)
            if isinstance(item, dict):
                name = item.get("name", name)
                payload = item.get("input", payload)
            if name == "StructuredOutput" and isinstance(payload, dict):
                return payload
    return None


def _raw_exception(label: str, exc: BaseException) -> str:
    return _json_dumps({
        "backend": label,
        "is_error": True,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback": traceback.format_exc(),
    })


def _run_claude_code_sdk_exec(
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
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    async def _query() -> tuple[str, str]:
        system_prompt: dict[str, Any] = {
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "Use the workspace files to solve the task. Read task.md and the skill at "
                ".agents/skills/skillopt-target/SKILL.md before answering. "
                "If ATTACHMENTS.md exists, read it and inspect the listed local files. "
                "Do not call a Skill tool; the ReflACT guidance is a local markdown file. "
                + (
                    "You may modify files in the workspace when the task asks you to create an artifact. "
                    if allow_file_edits
                    else "Do not modify files. "
                )
                + "Return structured output whose final_response preserves required <answer>...</answer> tags."
            ),
        }
        kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "output_format": {"type": "json_schema", "schema": ANSWER_SCHEMA},
            "allowed_tools": _tools_list(allowed_tools) or ["Read", "Bash"],
            "cwd": str(work_dir),
            "permission_mode": permission_mode or "bypassPermissions",
            "add_dirs": _validated_add_dirs(work_dir, data_dirs, images),
            "max_buffer_size": 8 * 1024 * 1024,
        }
        config = get_claude_code_exec_config()
        effort = _claude_effort(config.get("effort"))
        if effort:
            kwargs["effort"] = effort
        max_thinking_tokens = int(config.get("max_thinking_tokens", 0) or 0)
        if max_thinking_tokens > 0:
            kwargs["max_thinking_tokens"] = max_thinking_tokens
        options = ClaudeAgentOptions(**kwargs)
        if model:
            options.model = model.split("/", 1)[1] if model.startswith("anthropic/") else model

        messages = []
        async with ClaudeSDKClient(options) as client:
            await client.query(_normalize_target_exec_prompt(prompt))
            messages = [msg async for msg in client.receive_response()]
        last = messages[-1] if messages else None
        raw_structured_output = _extract_claude_structured_output(messages)
        response, parse_error = _structured_response(raw_structured_output)
        first = messages[0] if messages else None
        first_data = getattr(first, "data", {}) if first is not None else {}
        terminal_is_error = bool(getattr(last, "is_error", False)) if last is not None else False
        raw = _json_dumps({
            "backend": "claude_code_sdk",
            "uuid": first_data.get("uuid", "") if isinstance(first_data, dict) else "",
            "session_id": getattr(last, "session_id", "") if last is not None else "",
            "model": first_data.get("model", model) if isinstance(first_data, dict) else model,
            "tools": first_data.get("tools", _tools_list(allowed_tools)) if isinstance(first_data, dict) else _tools_list(allowed_tools),
            "duration_ms": getattr(last, "duration_ms", 0) if last is not None else 0,
            "total_cost_usd": getattr(last, "total_cost_usd", 0.0) if last is not None else 0.0,
            "num_turns": getattr(last, "num_turns", 0) if last is not None else 0,
            "usage": getattr(last, "usage", {}) if last is not None else {},
            "result": getattr(last, "result", "") if last is not None else "",
            "is_error": bool(parse_error) or (terminal_is_error and not response.strip()),
            "terminal_is_error": terminal_is_error,
            "parse_error": parse_error,
            "raw_structured_output": raw_structured_output,
            "messages": messages,
        })
        return response, raw

    return _run_async(asyncio.wait_for(_query(), timeout=timeout))


def _parse_claude_cli_json(stdout: str) -> dict | None:
    """Parse the claude CLI ``--output-format json`` result object from stdout.

    Tries the whole stdout first, then line-by-line (warnings may precede the
    JSON). Returns the result dict, or None when stdout is not CLI JSON —
    callers fall back to the legacy text semantics.
    """
    candidates = [stdout.strip()]
    candidates.extend(ln.strip() for ln in stdout.splitlines() if ln.strip().startswith("{"))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


def _append_exec_error(raw: str, *, backend: str, error: str) -> str:
    marker = json.dumps(
        {"type": "exec_error", "backend": backend, "error": error},
        ensure_ascii=False,
    )
    return f"{raw.rstrip()}\n{marker}" if raw.strip() else marker


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


_CODEX_TOKENS_USED_RE = re.compile(r"^tokens used$", re.MULTILINE)


def extract_exec_usage(raw: str) -> dict | None:
    """Normalize token usage out of a persisted exec *raw* transcript.

    Handles three sources, accumulating across attempt/turn segments:
    - claude CLI/SDK JSON: ``"input_tokens" / "cache_creation_input_tokens" /
      "cache_read_input_tokens" / "output_tokens"`` (per segment, the last
      complete group wins — SDK raws repeat usage in nested messages).
    - codex CLI: a ``tokens used`` line followed by a comma-grouped total.
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
            if groups:
                input_t, cache_write, cache_read, output_t = (int(v) for v in groups[-1])
                totals["input"] += input_t
                totals["cache_write"] += cache_write
                totals["cache_read"] += cache_read
                totals["output"] += output_t
                totals["total"] += input_t + cache_write + cache_read + output_t
                found = True
                continue
            match = _CODEX_TOKENS_USED_RE.search(segment)
            if match:
                after = segment[match.end():].lstrip("\n")
                first_line = after.splitlines()[0].strip() if after.splitlines() else ""
                digits = first_line.replace(",", "")
                if digits.isdigit():
                    totals["total"] += int(digits)
                    found = True
        return totals if found else None
    except Exception:  # noqa: BLE001 — usage extraction must never break a rollout
        return None


def _run_claude_code_cli_exec(
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
    config = get_claude_code_exec_config()
    tools = "Read,Bash" if allowed_tools is None else _normalize_tools(allowed_tools)
    cmd = [
        str(config["path"]),
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        permission_mode or "bypassPermissions",
        "--add-dir",
        work_dir,
        "--tools",
        tools,
        "--allowedTools",
        tools,
    ]
    if config.get("profile"):
        cmd.extend(["--settings", '{"env":{"CLAUDE_CODE_USE_BEDROCK":"0"}}'])
        cmd.extend(["--append-system-prompt", f"Profile: {config['profile']}"])
    if model:
        cmd.extend(["--model", model])
    effort = _claude_effort(config.get("effort"))
    if effort:
        cmd.extend(["--effort", effort])
    max_thinking_tokens = int(config.get("max_thinking_tokens", 0) or 0)
    if max_thinking_tokens > 0:
        cmd.extend(["--max-thinking-tokens", str(max_thinking_tokens)])
    for data_dir in data_dirs or []:
        cmd.extend(["--add-dir", _validate_exec_path(data_dir)])
    if images:
        for image in images:
            cmd.extend(["--add-dir", _validate_exec_path(os.path.dirname(image) or work_dir)])
    cmd.extend(["--", _exec_prompt(prompt, allow_file_edits=allow_file_edits)])

    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        raw = stdout
        if stderr:
            raw = f"{raw}\n[stderr]\n{stderr}" if raw else stderr
        raw = _append_exec_error(
            raw,
            backend="claude_code_exec",
            error=f"timed out after {timeout} seconds",
        )
        return "", raw

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    raw = stdout
    if stderr:
        raw = f"{raw}\n[stderr]\n{stderr}" if raw else stderr
    parsed = _parse_claude_cli_json(stdout)
    if parsed is not None:
        # API errors surface as is_error with the message in `result`; treat as
        # an empty response so the caller's empty-response retry loop engages,
        # matching the legacy text-mode behaviour (empty stdout on error).
        if parsed.get("is_error"):
            return "", raw
        response = str(parsed.get("result") or "").strip()
        return response, raw
    response = stdout.strip()
    if proc.returncode != 0 and not response:
        detail = stderr.strip() or "no diagnostic output"
        raw = _append_exec_error(
            raw,
            backend="claude_code_exec",
            error=f"process exited with code {proc.returncode}: {detail}",
        )
        return "", raw
    return response, raw


# ---------------------------------------------------------------------------
# Restricted judge transport (per-call policy; never weakens to SDK/CLI defaults)
# ---------------------------------------------------------------------------


def _is_judge_policy(policy: Any) -> bool:
    return isinstance(policy, dict) and policy.get("judge") is True


def _judge_mcp_server(policy: dict) -> tuple[str, list[str]]:
    """Return the single required Artifact MCP ``(name, command)`` or fail closed."""
    servers = policy.get("mcp_servers")
    if not isinstance(servers, dict) or len(servers) != 1:
        raise RuntimeError("judge policy must define exactly one required Artifact MCP server")
    name, spec = next(iter(servers.items()))
    if not isinstance(spec, dict) or spec.get("required") is not True:
        raise RuntimeError("judge Artifact MCP server must be marked required")
    command = spec.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(part, str) for part in command):
        raise RuntimeError("judge Artifact MCP server command must be a non-empty string argv")
    return str(name), [str(part) for part in command]


# Commander (the claude CLI's parser) prints these to stderr and exits non-zero
# during argument parsing — before any network call — when a flag is unknown or
# an option value is rejected. They distinguish flag drift from endpoint errors.
_CLI_FLAG_ERROR_MARKERS = (
    "unknown option",
    "unknown command",
    "unknown or unexpected option",
    "error: option",
)
_CLAUDE_JUDGE_FLAG_CHECK_TIMEOUT = 12.0


def _build_claude_judge_cli_command(
    *,
    config: dict,
    model: str,
    policy: dict,
    mcp_config_path: str,
    system_prompt_path: str,
    prompt: str,
) -> list[str]:
    """Build the restricted-judge argv for the claude CLI.

    Shared by the per-task judge call and the token-free preflight flag check so
    the two can never drift. Structured output uses ``--json-schema`` — the real
    claude CLI flag; the earlier ``--schema`` does not exist and made every
    judgment exit non-zero (``error: unknown option '--schema'``).
    """
    allowed_tools = ",".join(str(tool) for tool in policy.get("allowed_tools", []) if str(tool))
    cmd = [
        str(config["path"]),
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        "",
        "--allowedTools",
        allowed_tools,
        "--mcp-config",
        mcp_config_path,
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--append-system-prompt-file",
        system_prompt_path,
    ]
    if model:
        cmd.extend(["--model", model])
    effort = _claude_effort(config.get("effort"))
    if effort:
        cmd.extend(["--effort", effort])
    schema = policy.get("output_schema")
    if isinstance(schema, dict):
        cmd.extend(["--json-schema", json.dumps(schema)])
    cmd.extend(["--", prompt])
    return cmd


def check_claude_judge_cli_flags(
    *,
    policy: dict,
    model: str = "",
    timeout: float = _CLAUDE_JUDGE_FLAG_CHECK_TIMEOUT,
) -> None:
    """Preflight the full judge argv against the installed claude CLI, token-free.

    The CLI validates every option while parsing arguments, before contacting an
    endpoint, so a renamed/unknown flag (e.g. the old ``--schema``) exits
    non-zero immediately with a commander usage error we recognize. Valid flags
    proceed to the endpoint; we point them at an unreachable local URL with a
    dummy key and a short timeout, so no tokens are spent and a valid argv simply
    times out (treated as success). Raises ``RuntimeError`` only when the CLI
    rejects a judge flag.
    """
    config = get_claude_code_exec_config()
    work_dir = tempfile.mkdtemp(prefix="skillopt-judge-flagcheck-")
    try:
        mcp_config_path = os.path.join(work_dir, "judge-mcp.json")
        with open(mcp_config_path, "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": {}}, handle)
        system_prompt_path = os.path.join(work_dir, "judge-system-prompt.txt")
        with open(system_prompt_path, "w", encoding="utf-8") as handle:
            handle.write("preflight")
        cmd = _build_claude_judge_cli_command(
            config=config,
            model=model,
            policy=policy,
            mcp_config_path=mcp_config_path,
            system_prompt_path=system_prompt_path,
            prompt="preflight flag check; do not answer",
        )
        # Force the endpoint unreachable so a fully-parsed argv cannot spend
        # tokens or reach a real API; the short timeout then reads as success.
        env = {
            **os.environ,
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
            "ANTHROPIC_API_KEY": "skillopt-preflight-unreachable",
        }
        try:
            proc = subprocess.run(
                cmd, cwd=work_dir, capture_output=True, text=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            return  # argv parsed and reached the (unreachable) endpoint -> flags OK
        if proc.returncode != 0:
            combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
            if any(marker in combined for marker in _CLI_FLAG_ERROR_MARKERS):
                detail = (proc.stderr or proc.stdout or "").strip()[:300]
                raise RuntimeError(f"claude judge CLI rejected a judge policy flag: {detail}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_claude_code_judge_cli(
    *,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    policy: dict,
) -> tuple[str, str]:
    """Restricted Claude Code judge call: no built-in tools, only the Artifact MCP.

    Fail-closed CLI transport used only by the judge. It never adds the rollout
    or evidence directories, disables all built-in tools (``--tools ""``),
    permits only the named Artifact MCP tools, loads no user/project settings,
    and replaces the target-runner system/prompt wrapper with a judge system
    prompt that never references ``task.md`` or ``skillopt-target``.
    """
    config = get_claude_code_exec_config()
    server_name, mcp_command = _judge_mcp_server(policy)
    mcp_config_path = os.path.join(work_dir, "judge-mcp.json")
    with open(mcp_config_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"mcpServers": {server_name: {"type": "stdio", "command": mcp_command[0], "args": list(mcp_command[1:])}}},
            handle,
        )
    system_prompt_path = os.path.join(work_dir, "judge-system-prompt.txt")
    with open(system_prompt_path, "w", encoding="utf-8") as handle:
        handle.write(str(policy.get("system_prompt") or ""))
    cmd = _build_claude_judge_cli_command(
        config=config,
        model=model,
        policy=policy,
        mcp_config_path=mcp_config_path,
        system_prompt_path=system_prompt_path,
        prompt=prompt,
    )

    try:
        proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude judge client timed out after {timeout}s") from exc
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    raw = stdout if not stderr else (f"{stdout}\n[stderr]\n{stderr}" if stdout else stderr)
    parsed = _parse_claude_cli_json(stdout)
    if parsed is not None:
        if parsed.get("is_error"):
            # Surface the envelope's own message ("Not logged in · Please run
            # /login", "API Error: ...") instead of returning an empty verdict:
            # the judge has no retry loop, and an empty string would only fail
            # verdict parsing later with a message that hides the real cause.
            detail = str(parsed.get("result") or "").strip() or (stderr or stdout).strip()
            raise RuntimeError(f"claude judge client reported an error: {detail[:500]}")
        result = parsed.get("result")
        # With --json-schema the CLI may surface structured output as an object;
        # hand parse_verdict a JSON string rather than a Python repr().
        if isinstance(result, (dict, list)):
            return json.dumps(result), raw
        return str(result or "").strip(), raw
    if proc.returncode != 0:
        raise RuntimeError(f"claude judge client exited {proc.returncode}: {(stderr or stdout)[:2000]}")
    return stdout.strip(), raw


def _run_codex_judge_cli(
    *,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    policy: dict,
) -> tuple[str, str]:
    """Restricted Codex judge call: read-only, no approvals/network, only the MCP.

    Fail-closed CLI transport used only by the judge: ``--sandbox read-only``,
    ``approval_policy="never"``, web search disabled, AGENTS.md/rules disabled
    (``project_doc_max_bytes=0``), user config ignored (isolated ``CODEX_HOME``),
    a fresh ``--ephemeral`` session, one required Artifact MCP server, and the
    strict verdict output schema.
    """
    config = get_codex_exec_config()
    server_name, mcp_command = _judge_mcp_server(policy)
    last_message_path = os.path.join(work_dir, "codex_last_message.txt")
    schema_path = os.path.join(work_dir, "verdict-schema.json")
    schema = policy.get("output_schema")
    if isinstance(schema, dict):
        with open(schema_path, "w", encoding="utf-8") as handle:
            json.dump(schema, handle)
    codex_home = os.path.join(work_dir, ".codex_home")
    os.makedirs(codex_home, exist_ok=True)
    cmd = [
        str(config["path"]),
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        work_dir,
        "--ephemeral",
        "--sandbox",
        "read-only",
        "-c",
        'approval_policy="never"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "tools.web_search=false",
        "-c",
        f"mcp_servers.{server_name}.command={json.dumps(mcp_command[0])}",
        "-c",
        f"mcp_servers.{server_name}.args={json.dumps(list(mcp_command[1:]))}",
    ]
    reasoning_effort = str(config.get("reasoning_effort", "")).strip()
    if reasoning_effort and reasoning_effort != "none":
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    if model:
        cmd.extend(["-m", model])
    if isinstance(schema, dict):
        cmd.extend(["--output-schema", schema_path])
    cmd.extend(["--output-last-message", last_message_path, prompt])

    env = {**os.environ, "CODEX_HOME": codex_home}
    try:
        proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"codex judge client timed out after {timeout}s") from exc
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    raw = stdout if not stderr else (f"{stdout}\n[stderr]\n{stderr}" if stdout else stderr)
    last_message = ""
    if os.path.exists(last_message_path):
        with open(last_message_path, encoding="utf-8") as handle:
            last_message = handle.read()
    if proc.returncode != 0:
        raise RuntimeError(f"codex judge client exited {proc.returncode}: {(stderr or stdout)[:2000]}")
    return last_message, raw


def run_claude_code_exec(
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
    policy: dict | None = None,
) -> tuple[str, str]:
    if policy is not None:
        if not _is_judge_policy(policy):
            raise ValueError("policy must be a judge backend policy dict")
        return _run_claude_code_judge_cli(
            work_dir=work_dir,
            prompt=prompt,
            model=model,
            timeout=timeout,
            policy=policy,
        )
    if agentcore_runner.is_enabled():
        response, raw = agentcore_runner.run_remote_exec(
            backend="claude_code_exec",
            work_dir=work_dir,
            prompt=prompt,
            model=model,
            timeout=timeout,
            images=images,
            data_dirs=data_dirs,
            exec_kwargs={
                "allowed_tools": allowed_tools,
                "permission_mode": permission_mode,
                "allow_file_edits": allow_file_edits,
            },
            exec_config=get_claude_code_exec_config(),
        )
        _persist_claude_artifacts(work_dir, raw, response)
        return response, raw
    config = get_claude_code_exec_config()
    mode = _sdk_mode(config.get("use_sdk"))
    retries = int(config.get("empty_response_retries", 0) or 0)
    last_response = ""
    all_raw: list[str] = []

    for attempt in range(retries + 1):
        attempt_prompt = _retry_prompt(prompt, attempt)
        if mode != "cli":
            try:
                response, raw = _run_claude_code_sdk_exec(
                    work_dir=work_dir,
                    prompt=attempt_prompt,
                    model=model,
                    timeout=timeout,
                    images=images,
                    data_dirs=data_dirs,
                    allowed_tools=allowed_tools,
                    permission_mode=permission_mode,
                    allow_file_edits=allow_file_edits,
                )
                all_raw.append(f"===== CLAUDE SDK ATTEMPT {attempt + 1} =====\n{raw}")
                if response.strip():
                    combined = "\n\n".join(all_raw)
                    _persist_claude_artifacts(work_dir, combined, response)
                    return response, combined
            except (ImportError, ModuleNotFoundError) as exc:
                raw = _raw_exception("claude_code_sdk", exc)
                all_raw.append(f"===== CLAUDE SDK ATTEMPT {attempt + 1} =====\n{raw}")
                if mode == "sdk":
                    _persist_claude_artifacts(work_dir, "\n\n".join(all_raw), "")
                    raise
            except Exception as exc:  # noqa: BLE001
                raw = _raw_exception("claude_code_sdk", exc)
                all_raw.append(f"===== CLAUDE SDK ATTEMPT {attempt + 1} =====\n{raw}")
                if mode == "sdk" and attempt >= retries:
                    _persist_claude_artifacts(work_dir, "\n\n".join(all_raw), "")
                    raise
        if mode != "sdk":
            response, raw = _run_claude_code_cli_exec(
                work_dir=work_dir,
                prompt=attempt_prompt,
                model=model,
                timeout=timeout,
                images=images,
                data_dirs=data_dirs,
                allowed_tools=allowed_tools,
                permission_mode=permission_mode,
                allow_file_edits=allow_file_edits,
            )
            all_raw.append(f"===== CLAUDE CLI ATTEMPT {attempt + 1} =====\n{raw}")
            last_response = response
            if response.strip():
                combined = "\n\n".join(all_raw)
                _persist_claude_artifacts(work_dir, combined, response)
                return response, combined

    combined = "\n\n".join(all_raw)
    _persist_claude_artifacts(work_dir, combined, last_response)
    return last_response, combined


def _run_codex_sdk_exec(
    *,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    images: list[str] | None = None,
    data_dirs: list[str] | None = None,
) -> tuple[str, str]:
    from openai_codex_sdk import Codex

    for data_dir in data_dirs or []:
        _validate_exec_path(data_dir)
    for image in images or []:
        _validate_exec_path(os.path.dirname(image) or work_dir)

    async def _query() -> tuple[str, str]:
        config = get_codex_exec_config()
        reasoning_effort = str(config.get("reasoning_effort", "") or "").strip()
        thread_options: dict[str, Any] = {
            "working_directory": work_dir,
            "skip_git_repo_check": True,
            "sandbox_mode": str(config.get("sandbox") or "workspace-write"),
            "network_access_enabled": bool(config.get("network_access", False)),
            "web_search_enabled": bool(config.get("web_search", False)),
            "approval_policy": str(config.get("approval_policy") or "never"),
        }
        if model:
            thread_options["model"] = model
        if data_dirs:
            thread_options["additional_directories"] = data_dirs
        if reasoning_effort and reasoning_effort != "none":
            thread_options["model_reasoning_effort"] = reasoning_effort

        codex_options: dict[str, Any] = {"env": os.environ.copy()}
        codex_path = str(config.get("path") or "").strip()
        if codex_path:
            codex_options["codexPathOverride"] = codex_path
        codex = Codex(codex_options)
        thread = codex.start_thread(thread_options)
        turn = await thread.run(prompt, {"output_schema": _strict_schema(ANSWER_SCHEMA)})
        result_text = str(getattr(turn, "final_response", "") or "")
        parsed: Any = None
        parse_error = ""
        response = ""
        if result_text.strip():
            try:
                parsed = json.loads(result_text)
                response, parse_error = _structured_response(parsed)
            except Exception as exc:  # noqa: BLE001
                parse_error = f"{type(exc).__name__}: {exc}"
        else:
            parse_error = "No response from Codex SDK (final_response is empty)."
        raw = _json_dumps({
            "backend": "codex_sdk",
            "id": getattr(turn, "id", ""),
            "thread_id": getattr(turn, "thread_id", ""),
            "model": model,
            "thread_options": thread_options,
            "final_response": result_text,
            "raw_structured_output": parsed,
            "parse_error": parse_error,
            "is_error": bool(parse_error),
            "items": getattr(turn, "items", []),
        })
        return response, raw

    return _run_async(asyncio.wait_for(_query(), timeout=timeout))


def _run_codex_cli_exec(
    *,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    images: list[str] | None = None,
    data_dirs: list[str] | None = None,
    sandbox: str | None = None,
    full_auto: bool | None = None,
) -> tuple[str, str]:
    config = get_codex_exec_config()
    last_message_path = os.path.join(work_dir, "codex_last_message.txt")
    cmd = [
        str(config["path"]),
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        work_dir,
        # Exec workspaces often live below the host repository. Keep parent
        # project config, instructions, skills, and hooks out of the target run.
        "-c",
        "project_root_markers=[]",
        "--disable",
        "hooks",
    ]
    if config.get("profile"):
        cmd.extend(["-p", str(config["profile"])])
    reasoning_effort = str(config.get("reasoning_effort", "")).strip()
    if reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    actual_full_auto = bool(config.get("full_auto", True)) if full_auto is None else bool(full_auto)
    actual_sandbox = str(sandbox or config["sandbox"])
    if actual_full_auto:
        cmd.append("--full-auto")
    else:
        cmd.extend(["--sandbox", actual_sandbox])
    if model:
        cmd.extend(["-m", model])
    for data_dir in data_dirs or []:
        _validate_exec_path(data_dir)
    for image in images or []:
        _validate_exec_path(os.path.dirname(image) or work_dir)
        cmd.extend(["-i", image])
    cmd.extend(["--output-last-message", last_message_path, prompt])

    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        raw = stdout
        if stderr:
            raw = f"{raw}\n[stderr]\n{stderr}" if raw else stderr
        _persist_codex_artifacts(work_dir, raw, "")
        raise
    try:
        from skillopt.model import azure_openai as _openai
        _openai.tracker.record("rollout", 0, 0)
    except Exception:
        pass
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    last_message = ""
    if os.path.exists(last_message_path):
        with open(last_message_path, encoding="utf-8") as f:
            last_message = f.read()
    raw = stdout
    if stderr:
        raw = f"{raw}\n[stderr]\n{stderr}" if raw else stderr
    if proc.returncode != 0:
        _persist_codex_artifacts(work_dir, raw, last_message)
        detail = (stderr or stdout).strip()
        raise RuntimeError(
            f"codex exec failed with exit code {proc.returncode}: {detail[:4000]}"
        )
    blocked_hook = re.search(
        r"(?m)^hook: ([^\r\n]+)\r?\nhook: \1 Blocked\r?$",
        raw,
    )
    if blocked_hook and not last_message.strip():
        _persist_codex_artifacts(work_dir, raw, last_message)
        raise RuntimeError(
            f"codex exec was blocked by hook {blocked_hook.group(1)!r}; "
            "isolated exec workspaces must not inherit lifecycle hooks"
        )
    return last_message, raw


def run_codex_exec(
    *,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    images: list[str] | None = None,
    data_dirs: list[str] | None = None,
    sandbox: str | None = None,
    full_auto: bool | None = None,
    policy: dict | None = None,
) -> tuple[str, str]:
    if policy is not None:
        if not _is_judge_policy(policy):
            raise ValueError("policy must be a judge backend policy dict")
        return _run_codex_judge_cli(
            work_dir=work_dir,
            prompt=prompt,
            model=model,
            timeout=timeout,
            policy=policy,
        )
    if agentcore_runner.is_enabled():
        response, raw = agentcore_runner.run_remote_exec(
            backend="codex_exec",
            work_dir=work_dir,
            prompt=prompt,
            model=model,
            timeout=timeout,
            images=images,
            data_dirs=data_dirs,
            exec_kwargs={
                "sandbox": sandbox,
                "full_auto": full_auto,
            },
            exec_config=get_codex_exec_config(),
        )
        _persist_codex_artifacts(work_dir, raw, response)
        return response, raw
    config = get_codex_exec_config()
    mode = _sdk_mode(config.get("use_sdk"))
    retries = int(config.get("empty_response_retries", 0) or 0)
    last_response = ""
    all_raw: list[str] = []

    for attempt in range(retries + 1):
        attempt_prompt = _retry_prompt(prompt, attempt)
        if mode != "cli":
            try:
                response, raw = _run_codex_sdk_exec(
                    work_dir=work_dir,
                    prompt=attempt_prompt,
                    model=model,
                    timeout=timeout,
                    images=images,
                    data_dirs=data_dirs,
                )
                all_raw.append(f"===== CODEX SDK ATTEMPT {attempt + 1} =====\n{raw}")
                if response.strip():
                    combined = "\n\n".join(all_raw)
                    _persist_codex_artifacts(work_dir, combined, response)
                    return response, combined
            except (ImportError, ModuleNotFoundError) as exc:
                raw = _raw_exception("codex_sdk", exc)
                all_raw.append(f"===== CODEX SDK ATTEMPT {attempt + 1} =====\n{raw}")
                if mode == "sdk":
                    _persist_codex_artifacts(work_dir, "\n\n".join(all_raw), "")
                    raise
            except Exception as exc:  # noqa: BLE001
                raw = _raw_exception("codex_sdk", exc)
                all_raw.append(f"===== CODEX SDK ATTEMPT {attempt + 1} =====\n{raw}")
                if mode == "sdk" and attempt >= retries:
                    _persist_codex_artifacts(work_dir, "\n\n".join(all_raw), "")
                    raise
        if mode != "sdk":
            response, raw = _run_codex_cli_exec(
                work_dir=work_dir,
                prompt=attempt_prompt,
                model=model,
                timeout=timeout,
                images=images,
                data_dirs=data_dirs,
                sandbox=sandbox,
                full_auto=full_auto,
            )
            all_raw.append(f"===== CODEX CLI ATTEMPT {attempt + 1} =====\n{raw}")
            last_response = response
            if response.strip():
                combined = "\n\n".join(all_raw)
                _persist_codex_artifacts(work_dir, combined, response)
                return response, combined

    combined = "\n\n".join(all_raw)
    _persist_codex_artifacts(work_dir, combined, last_response)
    return last_response, combined


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
    sandbox: str | None = None,
    full_auto: bool | None = None,
    allow_file_edits: bool = False,
) -> tuple[str, str]:
    backend = get_target_backend()
    if backend == "codex_exec":
        return run_codex_exec(
            work_dir=work_dir,
            prompt=prompt,
            model=model,
            timeout=timeout,
            images=images,
            data_dirs=data_dirs,
            sandbox=sandbox,
            full_auto=full_auto,
        )
    if backend == "claude_code_exec":
        return run_claude_code_exec(
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
