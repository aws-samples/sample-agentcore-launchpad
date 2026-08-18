"""Shared Plugin contracts for SkillEval evaluation and training."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace

import yaml

from skillopt.envs.skilleval.rollout import RuntimeSkill, collect_support_files

_SAFE_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class PluginSkillState:
    """One named Skill in a complete Plugin snapshot."""

    name: str
    source_dir: str
    content: str
    files: tuple[tuple[str, str], ...] = ()
    trainable: bool = True


@dataclass(frozen=True)
class PluginState:
    """Ordered, independently editable Skill documents."""

    skills: tuple[PluginSkillState, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)

    @property
    def trainable_names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills if skill.trainable)

    def skill(self, name: str) -> PluginSkillState:
        for skill in self.skills:
            if skill.name == name:
                return skill
        raise KeyError(name)

    def replace_content(self, updates: dict[str, str]) -> PluginState:
        unknown = sorted(set(updates) - set(self.names))
        if unknown:
            raise ValueError(f"candidate updates contain unknown skills: {unknown}")
        frozen = sorted(name for name in updates if not self.skill(name).trainable)
        if frozen:
            raise ValueError(f"candidate updates contain non-trainable skills: {frozen}")
        return PluginState(
            tuple(
                replace(skill, content=updates.get(skill.name, skill.content))
                for skill in self.skills
            )
        )

    def runtime_skills(self) -> list[RuntimeSkill]:
        return [
            {
                "name": skill.name,
                "content": skill.content,
                "files": list(skill.files),
            }
            for skill in self.skills
        ]


def _read_skill_file(path: str) -> str:
    if not os.path.isfile(path):
        raise ValueError(f"skill file not found: {path}")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        raise ValueError(f"skill file is empty: {path}")
    return content


def _collect_skill(path: str) -> tuple[str, list[tuple[str, str]], str]:
    if os.path.isdir(path):
        source_dir = os.path.abspath(path)
        skill_md_path = os.path.join(source_dir, "SKILL.md")
        if not os.path.isfile(skill_md_path):
            raise ValueError(f"skill directory has no SKILL.md: {path}")
        return (
            _read_skill_file(skill_md_path),
            collect_support_files(source_dir),
            source_dir,
        )
    absolute = os.path.abspath(path)
    return _read_skill_file(absolute), [], os.path.dirname(absolute)


def skill_name(content: str, path: str) -> str:
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", content, re.DOTALL)
    if match:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()
    if os.path.isdir(path):
        return os.path.basename(os.path.normpath(path))
    return os.path.splitext(os.path.basename(path))[0]


def collect_plugin_state(
    paths: list[str],
    trainable_names: list[str] | None = None,
    *,
    require_multiple: bool = True,
) -> PluginState:
    """Collect and validate an ordered Plugin state from Skill paths."""
    if require_multiple and len(paths) < 2:
        raise ValueError("Plugin training requires at least two Skill paths")
    if not paths:
        raise ValueError("at least one Skill path is required")

    collected: list[PluginSkillState] = []
    seen: set[str] = set()
    for path in paths:
        content, files, source_dir = _collect_skill(path)
        name = skill_name(content, path)
        if not _SAFE_SKILL_NAME.fullmatch(name):
            raise ValueError(f"skill name must be filesystem-safe: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate skill name: {name!r}")
        seen.add(name)
        collected.append(
            PluginSkillState(
                name=name,
                source_dir=source_dir,
                content=content,
                files=tuple(files),
            )
        )

    requested = list(dict.fromkeys(trainable_names or []))
    if requested:
        unknown = sorted(set(requested) - seen)
        if unknown:
            raise ValueError(f"unknown trainable skills: {unknown}")
        trainable = set(requested)
    else:
        trainable = seen

    return PluginState(
        tuple(replace(skill, trainable=skill.name in trainable) for skill in collected)
    )


def collect_runtime_skills(paths: list[str]) -> list[RuntimeSkill]:
    """Backward-compatible runtime descriptor collector for evaluation."""
    return collect_plugin_state(paths, require_multiple=False).runtime_skills()


def normalize_plugin_tasks(items: list[dict], skill_names: set[str]) -> None:
    """Normalize attribution metadata and reject unknown runtime Skill names."""
    for index, item in enumerate(items):
        raw = item.get("target_skills")
        if raw is None:
            item["target_skills"] = []
        elif (
            not isinstance(raw, list)
            or not raw
            or any(not isinstance(name, str) or not name.strip() for name in raw)
        ):
            raise ValueError(
                f"item #{index} target_skills must be a non-empty string array"
            )
        else:
            targets = list(dict.fromkeys(name.strip() for name in raw))
            unknown = sorted(set(targets) - skill_names)
            if unknown:
                raise ValueError(
                    f"item #{index} target_skills contains unknown skills: {unknown}"
                )
            item["target_skills"] = targets

        raw_task_type = item.get("task_type")
        if raw_task_type is None:
            item["task_type"] = "default"
        elif not isinstance(raw_task_type, str):
            raise ValueError(f"item #{index} task_type must be a string")
        else:
            item["task_type"] = raw_task_type.strip() or "default"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _metrics(results: list[dict]) -> dict:
    # Rows with score_valid=False are infrastructure/evaluation failures, not
    # legitimate zero scores -- they must never silently pull down hard/soft
    # aggregates or training signal (see evaluator.evaluate_rollouts). They
    # are still counted so callers can see them via scored_count/invalid_count.
    scored = [result for result in results if result.get("score_valid") is not False]
    return {
        "count": len(results),
        "hard": _mean([float(result.get("hard", 0)) for result in scored]),
        "soft": _mean([float(result.get("soft", 0.0)) for result in scored]),
        "scored_count": len(scored),
        "invalid_count": len(results) - len(scored),
    }


def aggregate_results(
    results: list[dict],
    skill_names: list[str],
    *,
    require_valid: bool = False,
) -> dict:
    """Aggregate complete-Plugin results with deterministic group ordering.

    ``require_valid=True`` raises if any result has ``score_valid is False``
    (an infrastructure/evaluation failure) rather than silently averaging
    over it -- training must never proceed on corrupted or invalid judge
    signal.
    """
    if require_valid:
        invalid_ids = [
            str(result.get("id", "")) for result in results if result.get("score_valid") is False
        ]
        if invalid_ids:
            raise ValueError(
                f"aggregate_results: {len(invalid_ids)} result(s) have score_valid=False: {invalid_ids}"
            )
    by_skill = {
        name: _metrics(
            [result for result in results if name in result.get("target_skills", [])]
        )
        for name in skill_names
    }
    by_type = {
        task_type: _metrics(
            [
                result
                for result in results
                if result.get("task_type", "default") == task_type
            ]
        )
        for task_type in sorted(
            {str(result.get("task_type", "default")) for result in results}
        )
    }
    routing = [result for result in results if result.get("task_type") == "routing"]
    integration = [
        result
        for result in results
        if result.get("task_type") == "integration"
        or len(result.get("target_skills", [])) > 1
    ]
    weakest = min(
        ((name, metrics) for name, metrics in by_skill.items() if metrics["count"]),
        key=lambda entry: (entry[1]["hard"], entry[1]["soft"], entry[0]),
        default=None,
    )
    return {
        "mode": "plugin" if len(skill_names) > 1 else "skill",
        "skill_count": len(skill_names),
        "skill_names": list(skill_names),
        "overall": _metrics(results),
        "by_skill": by_skill,
        "by_task_type": by_type,
        "routing": _metrics(routing) if routing else None,
        "integration": _metrics(integration) if integration else None,
        "weakest_skill": (
            {"name": weakest[0], **weakest[1]} if weakest is not None else None
        ),
    }


def plugin_hash(state: PluginState) -> str:
    digest = hashlib.sha256()
    for skill in state.skills:
        digest.update(skill.name.encode())
        digest.update(b"\0")
        digest.update(skill.content.encode())
        digest.update(b"\0")
        for source, relative in skill.files:
            digest.update(relative.encode())
            digest.update(b"\0")
            with open(source, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()[:16]


def _write_snapshot_tree(state: PluginState, root: str) -> dict:
    skills_manifest: list[dict] = []
    for skill in state.skills:
        skill_dir = os.path.join(root, skill.name)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(skill.content)
        for src, rel in skill.files:
            destination = os.path.join(skill_dir, rel)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(src, destination)
        skills_manifest.append(
            {
                "name": skill.name,
                "trainable": skill.trainable,
                "source_dir": skill.source_dir,
            }
        )
    manifest = {
        "schema_version": 1,
        "plugin_hash": plugin_hash(state),
        "skill_names": list(state.names),
        "trainable_skill_names": list(state.trainable_names),
        "skills": skills_manifest,
    }
    with open(os.path.join(root, _MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def write_plugin_snapshot(
    state: PluginState,
    destination: str,
    *,
    replace_existing: bool = False,
) -> dict:
    """Atomically write a complete, deployable Plugin directory."""
    destination = os.path.abspath(destination)
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=f".{os.path.basename(destination)}-", dir=parent)
    try:
        manifest = _write_snapshot_tree(state, temp_dir)
        if os.path.exists(destination):
            if not replace_existing:
                raise FileExistsError(destination)
            shutil.rmtree(destination)
        os.replace(temp_dir, destination)
        return manifest
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def load_plugin_snapshot(path: str) -> PluginState:
    """Load a complete state and validate its ordered manifest."""
    root = os.path.abspath(path)
    manifest_path = os.path.join(root, _MANIFEST_NAME)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid Plugin snapshot manifest: {manifest_path}") from exc

    names = manifest.get("skill_names")
    entries = manifest.get("skills")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(names, list)
        or not names
        or not isinstance(entries, list)
        or [entry.get("name") for entry in entries if isinstance(entry, dict)] != names
    ):
        raise ValueError(f"invalid Plugin snapshot manifest: {manifest_path}")

    skills: list[PluginSkillState] = []
    for entry in entries:
        name = entry["name"]
        skill_dir = os.path.join(root, name)
        content = _read_skill_file(os.path.join(skill_dir, "SKILL.md"))
        files = tuple(collect_support_files(skill_dir))
        skills.append(
            PluginSkillState(
                name=name,
                source_dir=skill_dir,
                content=content,
                files=files,
                trainable=bool(entry.get("trainable", True)),
            )
        )
    state = PluginState(tuple(skills))
    if manifest.get("plugin_hash") != plugin_hash(state):
        raise ValueError(f"Plugin snapshot hash mismatch: {manifest_path}")
    return state
