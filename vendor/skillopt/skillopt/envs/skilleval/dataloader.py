"""SkillEval task set loading and validation.

Unlike benchmark envs, skilleval consumes a single user-provided task file
(JSON array or JSONL) rather than a train/val/test split directory.  Each
task item must carry its own acceptance criteria (``rubric``) for the LLM
judge.  Validation is fail-fast: any malformed item aborts the run before
a single model call is spent.

Task item schema::

    {
      "id": "task_001",            # required, unique, filesystem-safe
      "question": "...",           # required — task text given to the agent
      "rubric": "...",             # required — judge acceptance criteria
      "files": {"rel/path": "…"},  # optional — seeded into the work_dir
      "task_type": "..."           # optional — grouping key, default "default"
    }
"""
from __future__ import annotations

from skillopt.datasets.base import (
    SplitDataLoader,
    _compute_weighted_counts,
    _load_json_or_jsonl,
)
from skillopt.envs.skilleval.contracts import normalize_judge_contract
from skillopt.envs.skilleval.coverage import plan_disjoint_plugin_coverage
from skillopt.envs.skilleval.plugin import normalize_plugin_tasks

_REQUIRED_FIELDS = ("id", "question", "rubric")
_DEFAULT_TASK_TYPE = "default"


def _item_label(index: int, item: dict) -> str:
    """Human-readable locator for error messages: index plus id when present."""
    raw_id = item.get("id") if isinstance(item, dict) else None
    if isinstance(raw_id, str) and raw_id:
        return f"item #{index} (id={raw_id!r})"
    return f"item #{index}"


def _validate_id(index: int, item: dict) -> str:
    task_id = item["id"]
    if "/" in task_id or "\\" in task_id or ".." in task_id:
        raise ValueError(
            f"{_item_label(index, item)}: id must be filesystem-safe "
            "(no '/', '\\', or '..') because it names the task work_dir"
        )
    return task_id


def _validate_files(index: int, item: dict) -> dict:
    files = item.get("files")
    if files is None:
        return {}
    if not isinstance(files, dict):
        raise ValueError(
            f"{_item_label(index, item)}: 'files' must be a dict of "
            f"{{relative path: text content}}, got {type(files).__name__}"
        )
    for rel_path, content in files.items():
        if (
            not isinstance(rel_path, str)
            or not rel_path
            or rel_path.startswith(("~", "/", "\\"))
            or "\\" in rel_path
            or any(part in ("", ".", "..") for part in rel_path.split("/"))
        ):
            raise ValueError(
                f"{_item_label(index, item)}: 'files' path {rel_path!r} "
                "must be a safe relative path"
            )
        parts = rel_path.split("/")
        if parts[0] in {".agents", "task.md"}:
            raise ValueError(
                f"{_item_label(index, item)}: 'files' path {rel_path!r} "
                "collides with the evaluation runtime"
            )
        if not isinstance(content, str):
            raise ValueError(
                f"{_item_label(index, item)}: 'files' value for {rel_path!r} "
                f"must be str, got {type(content).__name__}"
            )
    return dict(files)


def _normalize_items(raw_items: list, source: str) -> list[dict]:
    """Validate and normalize raw task items (shared by file and split loading)."""
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError(f"No task items found in {source}")

    tasks: list[dict] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(
                f"item #{index}: expected an object, got {type(item).__name__}"
            )
        for field_name in _REQUIRED_FIELDS:
            value = item.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{_item_label(index, item)}: missing or empty required "
                    f"field {field_name!r}"
                )
        task_id = _validate_id(index, item)
        if task_id in seen_ids:
            raise ValueError(
                f"{_item_label(index, item)}: duplicate id {task_id!r}"
            )
        seen_ids.add(task_id)

        raw_task_type = item.get("task_type")
        if raw_task_type is not None and not isinstance(raw_task_type, str):
            raise ValueError(
                f"{_item_label(index, item)}: 'task_type' must be a string"
            )
        normalized = dict(item)
        normalized["files"] = _validate_files(index, item)
        normalized["task_type"] = raw_task_type or _DEFAULT_TASK_TYPE
        judge_mode, artifact_checks, mode_explicit = normalize_judge_contract(
            index,
            item,
        )
        normalized["judge_mode"] = judge_mode
        normalized["_judge_mode_explicit"] = mode_explicit
        normalized["artifact_checks"] = artifact_checks
        tasks.append(normalized)
    return tasks


def load_tasks(path: str, limit: int = 0) -> list[dict]:
    """Load and validate a skilleval task file (JSON array or JSONL).

    The entire file is validated before any slicing so a corrupt item fails
    the run deterministically regardless of ``limit``.

    Raises
    ------
    ValueError
        On any missing/empty required field, duplicate id, unsafe id, or
        non-str ``files`` value.  The message names the offending item.
    """
    tasks = _normalize_items(_load_json_or_jsonl(path), path)
    if limit and limit > 0:
        tasks = tasks[:limit]
    return tasks


class SkillEvalDataLoader(SplitDataLoader):
    """Split-based task loading for training on skilleval task sets.

    Each split directory (train/, val/, test/) holds one JSON array of task
    items with the same schema (and the same fail-fast validation) as the
    single-file evaluation path.
    """

    def setup(self, cfg: dict) -> None:
        self.plugin_skill_names = self._normalize_skill_names(
            cfg.get("plugin_skill_names"),
            "plugin_skill_names",
        )
        self.required_training_skills = self._normalize_skill_names(
            cfg.get("required_training_skills"),
            "required_training_skills",
        )
        self.required_validation_skills = self._normalize_skill_names(
            cfg.get("required_validation_skills"),
            "required_validation_skills",
        )
        installed = set(self.plugin_skill_names)
        for key, names in (
            ("required_training_skills", self.required_training_skills),
            ("required_validation_skills", self.required_validation_skills),
        ):
            unknown = [name for name in names if name not in installed]
            if unknown:
                raise ValueError(
                    f"{key} must be installed Plugin Skills: {unknown}"
                )
        self._minimum_training_count = 0
        self._minimum_validation_count = 0
        super().setup(cfg)

    @staticmethod
    def _normalize_skill_names(value: object, key: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{key} must be a string array")
        names: list[str] = []
        for raw_name in value:
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError(f"{key} must contain non-empty strings")
            name = raw_name.strip()
            if name not in names:
                names.append(name)
        return tuple(names)

    def _normalize_plugin_metadata(self, items: list[dict]) -> list[dict]:
        if self.plugin_skill_names:
            normalize_plugin_tasks(items, set(self.plugin_skill_names))
        return items

    def load_raw_items(self, data_path: str) -> list[dict]:
        items = _normalize_items(_load_json_or_jsonl(data_path), data_path)
        return self._normalize_plugin_metadata(items)

    def write_split_items(self, split_path: str, items: list[dict]) -> None:
        serialized_items: list[dict] = []
        for item in items:
            serialized = dict(item)
            mode_explicit = serialized.pop("_judge_mode_explicit")
            if not mode_explicit:
                serialized.pop("judge_mode", None)
            serialized_items.append(serialized)
        super().write_split_items(split_path, serialized_items)

    def load_split_items(self, split_path: str) -> list[dict]:
        items = super().load_split_items(split_path)
        normalized = _normalize_items(items, split_path)
        return self._normalize_plugin_metadata(normalized)

    def _partition_ratio_items(
        self,
        shuffled: list[dict],
        counts: tuple[int, int, int],
        ratio: tuple[int, int, int],
    ) -> tuple[list[dict], list[dict], list[dict]]:
        required_training = self.required_training_skills
        required_validation = self.required_validation_skills
        if not required_training and not required_validation:
            return super()._partition_ratio_items(shuffled, counts, ratio)

        del counts
        coverage_plan = plan_disjoint_plugin_coverage(
            shuffled,
            required_training,
            required_validation,
        )
        training_indices = list(coverage_plan.training_indices)
        validation_indices = list(coverage_plan.validation_indices)
        unassigned = list(coverage_plan.remaining_indices)

        self._minimum_training_count = len(training_indices)
        self._minimum_validation_count = len(validation_indices)
        minimum_total = (
            self._minimum_training_count
            + self._minimum_validation_count
            + 1
        )
        extra_total = len(shuffled) - minimum_total
        extra_train, extra_val, _extra_test = _compute_weighted_counts(
            extra_total,
            ratio,
        )

        training_indices.extend(unassigned[:extra_train])
        cursor = extra_train
        validation_indices.extend(unassigned[cursor: cursor + extra_val])
        cursor += extra_val
        test_indices = unassigned[cursor:]

        train_items = [shuffled[index] for index in training_indices]
        val_items = [shuffled[index] for index in validation_indices]
        test_items = [shuffled[index] for index in test_indices]
        return train_items, val_items, test_items

    def _ratio_split_manifest(self) -> dict:
        if not self.required_training_skills and not self.required_validation_skills:
            return {}
        return {
            "coverage_aware": True,
            "required_training_skills": list(self.required_training_skills),
            "required_validation_skills": list(self.required_validation_skills),
            "minimum_training_count": self._minimum_training_count,
            "minimum_validation_count": self._minimum_validation_count,
        }
