"""SkillEval rollout — drive an exec agent CLI on each task under the skill.

Each task gets an isolated work_dir seeded with the skill document (via
``prepare_workspace``, which writes ``.agents/skills/skillopt-target/SKILL.md``)
plus any task-declared files, then the configured exec backend drives the
agent: ``run_claude_code_exec`` for ``claude_code_exec`` (default) or
``run_codex_exec`` when the target backend is ``codex_exec``.
Failures are isolated per task: one crashing task never aborts the batch, and
rollout adds no retry of its own (the exec harness owns retries).
"""
from __future__ import annotations

import errno
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

from skillopt.envs.skilleval.artifacts import (
    ArtifactCollectionError,
    ArtifactValidationError,
    build_manifest,
    diff_manifests,
)
from skillopt.model.backend_config import get_target_backend
from skillopt.model.codex_harness import (
    extract_exec_failure,
    extract_exec_usage,
    prepare_workspace,
    run_claude_code_exec,
    run_codex_exec,
)

GUIDE_PROMPT = (
    "Read `.agents/skills/skillopt-target/SKILL.md` first and follow it while "
    "working. Relative paths mentioned in the skill (scripts/, references/, "
    "examples/, ...) resolve from `.agents/skills/skillopt-target/`. "
    "Then complete the task described in `task.md`. Give your final "
    "answer or a summary of what you produced at the end of your reply."
)
PLUGIN_GUIDE_PROMPT = (
    "Inspect the available skills under `.agents/skills/` and use the relevant "
    "skill or skills while completing the task in `task.md`. Do not assume that "
    "every installed skill is relevant. Relative paths in a skill resolve from "
    "that skill's own directory. Give your final answer or a summary of what "
    "you produced at the end of your reply."
)

# where prepare_workspace installs the skill inside each work_dir
SKILL_INSTALL_DIR = os.path.join(".agents", "skills", "skillopt-target")

_SKIP_DIRS = {"__pycache__", "node_modules", ".git"}


class RuntimeSkill(TypedDict):
    name: str
    content: str
    files: list[tuple[str, str]]


def _has_permission_denied_cause(exc: BaseException) -> bool:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in {errno.EACCES, errno.EPERM}:
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _record_target_artifact_failure(result: dict, exc: BaseException) -> None:
    result["artifact_error"] = f"{type(exc).__name__}: {exc}"
    result["artifact_failure"] = True
    result["artifact_error_type"] = "target_validation"
    if not result.get("error"):
        result["error"] = result["artifact_error"]


def collect_support_files(skill_dir: str) -> list[tuple[str, str]]:
    """Return a skill directory's supporting files for ``run_batch(skill_files=...)``.

    Walks *skill_dir* and returns every regular file except ``SKILL.md`` as an
    ``(absolute src, path relative to the skill dir)`` pair. Hidden entries and
    tooling caches are skipped; symlinks are not followed (a task workspace
    must never be able to reach back into the source skill).
    """
    if not os.path.isdir(skill_dir):
        raise ValueError(f"skill_dir is not a directory: {skill_dir}")
    support: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS]
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, skill_dir)
            if rel == "SKILL.md" or os.path.islink(full):
                continue
            support.append((os.path.abspath(full), rel))
    return support


def _rollout_one(
    item: dict,
    skill_content: str,
    out_root: str,
    *,
    timeout: int,
    model: str,
    skill_files: list[tuple[str, str]] | None = None,
    skill_docs: dict[str, str] | None = None,
    runtime_skills: list[RuntimeSkill] | None = None,
) -> dict:
    # absolute: codex exec receives work_dir both as cwd and as `-C`, so a
    # relative path would be resolved twice and fail with ENOENT
    work_dir = os.path.abspath(os.path.join(out_root, "rollouts", item["id"]))
    result = {
        "id": str(item["id"]),
        "task_type": item.get("task_type", "default"),
        "target_skills": list(item.get("target_skills") or []),
        "response": "",
        "duration_s": 0.0,
        "work_dir": work_dir,
        "artifacts": [],
        "score_valid": True,
    }
    start = time.time()
    try:
        if runtime_skills:
            copy_files = [
                (src, os.path.join(".agents", "skills", skill["name"], rel_dst))
                for skill in runtime_skills
                for src, rel_dst in skill["files"]
            ]
        else:
            copy_files = [
                (src, os.path.join(SKILL_INSTALL_DIR, rel_dst))
                for src, rel_dst in (skill_files or [])
            ]
        extra_files = dict(item.get("files") or {})
        for rel_dst, content in (skill_docs or {}).items():
            extra_files[os.path.join(SKILL_INSTALL_DIR, rel_dst)] = content
        prepare_workspace(
            work_dir=work_dir,
            skill_md=skill_content,
            task_text=item["question"],
            extra_files=extra_files or None,
            copy_files=copy_files or None,
            installed_skills=(
                [(skill["name"], skill["content"]) for skill in runtime_skills]
                if runtime_skills else None
            ),
        )
        before = build_manifest(work_dir)
        try:
            # Artifact-producing tasks are the norm in skill evaluation: allow
            # file edits (the default exec prompt injects "Do not modify
            # files.") and extend the read-only default tool set accordingly.
            # codex_exec gets the same freedom through its workspace-write
            # sandbox default.
            backend = "codex_exec" if get_target_backend() == "codex_exec" else "claude_code_exec"
            if backend == "codex_exec":
                response, raw = run_codex_exec(
                    work_dir=work_dir,
                    prompt=PLUGIN_GUIDE_PROMPT if runtime_skills else GUIDE_PROMPT,
                    model=model,
                    timeout=timeout,
                )
            else:
                response, raw = run_claude_code_exec(
                    work_dir=work_dir,
                    prompt=PLUGIN_GUIDE_PROMPT if runtime_skills else GUIDE_PROMPT,
                    model=model,
                    timeout=timeout,
                    allowed_tools="Read,Bash,Write,Edit,Glob,Grep",
                    allow_file_edits=True,
                )
            result["response"] = response
            usage = extract_exec_usage(raw)
            if usage is not None:
                result["usage"] = usage
            if not response.strip():
                detail = extract_exec_failure(raw)
                result["error"] = (
                    f"{backend} failed to produce a final response: {detail}"
                    if detail
                    else f"{backend} returned an empty response after all attempts"
                )
        except Exception as exc:  # noqa: BLE001 — isolate target failures
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["error_traceback"] = traceback.format_exc(limit=5)
        finally:
            try:
                after = build_manifest(work_dir)
                result["artifacts"] = diff_manifests(before, after)
            except ArtifactValidationError as exc:
                _record_target_artifact_failure(result, exc)
            except ArtifactCollectionError as exc:
                if _has_permission_denied_cause(exc):
                    validation_error = ArtifactValidationError(str(exc))
                    _record_target_artifact_failure(result, validation_error)
                else:
                    result["artifact_collection_error"] = f"{type(exc).__name__}: {exc}"
                    result["artifact_collection_error_type"] = "infrastructure"
                    result["score_valid"] = False
            except Exception as exc:  # noqa: BLE001 — unexpected collection failure
                result["artifact_collection_error"] = f"{type(exc).__name__}: {exc}"
                result["artifact_collection_error_type"] = "infrastructure"
                result["score_valid"] = False
    except (ArtifactCollectionError, ArtifactValidationError) as exc:
        result["artifact_collection_error"] = f"{type(exc).__name__}: {exc}"
        result["artifact_collection_error_type"] = "infrastructure"
        result["score_valid"] = False
    except Exception as exc:  # noqa: BLE001 — isolate task failures
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["error_traceback"] = traceback.format_exc(limit=5)
    result["duration_s"] = round(time.time() - start, 2)
    return result


def run_batch(
    items: list[dict],
    skill_content: str,
    out_root: str,
    *,
    workers: int = 4,
    timeout: int = 600,
    model: str = "",
    skill_files: list[tuple[str, str]] | None = None,
    skill_docs: dict[str, str] | None = None,
    runtime_skills: list[RuntimeSkill] | None = None,
) -> list[dict]:
    """Roll out every task in *items* under *skill_content*, input order preserved.

    *skill_files* carries a multi-file skill's supporting files as
    ``(absolute src, path relative to the skill dir)`` pairs; they are copied
    into each work_dir under ``.agents/skills/skillopt-target/`` so relative
    references (scripts/, references/, ...) keep resolving.

    *skill_docs* carries **trainable** documents as ``{rel_path: content}``
    (already split out of a bundle by the caller); they are written into the
    same install dir, taking the place of a frozen copy.
    """
    os.makedirs(out_root, exist_ok=True)
    if not items:
        return []

    print(f"  [skilleval] rolling out {len(items)} tasks (workers={workers})")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(
                _rollout_one,
                item,
                skill_content,
                out_root,
                timeout=timeout,
                model=model,
                skill_files=skill_files,
                skill_docs=skill_docs,
                runtime_skills=runtime_skills,
            )
            for item in items
        ]
        results = [future.result() for future in futures]

    invalid = sum(1 for result in results if result.get("score_valid") is False)
    failed = sum(
        1
        for result in results
        if result.get("error") and result.get("score_valid") is not False
    )
    ok = len(results) - failed - invalid
    print(
        f"  [skilleval] rollout finished: "
        f"{ok} ok, {failed} errored, {invalid} invalid"
    )
    return results
