"""Pure coverage planning for Plugin task sets."""
from __future__ import annotations

from dataclasses import dataclass

PLUGIN_MIN_TASKS_PER_SKILL = 2
PLUGIN_TEST_RESERVE = 1


@dataclass(frozen=True)
class PluginCoveragePlan:
    training_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    remaining_indices: tuple[int, ...]


def minimum_plugin_task_count(
    skill_count: int,
    minimum_per_skill: int = PLUGIN_MIN_TASKS_PER_SKILL,
    test_reserve: int = PLUGIN_TEST_RESERVE,
) -> int:
    if skill_count < 0:
        raise ValueError("skill_count must be non-negative")
    if minimum_per_skill < 1:
        raise ValueError("minimum_per_skill must be positive")
    if test_reserve < 0:
        raise ValueError("test_reserve must be non-negative")
    return skill_count * minimum_per_skill + test_reserve


def target_skill_counts(
    items: list[dict],
    skill_names: list[str] | tuple[str, ...],
) -> dict[str, int]:
    names = tuple(dict.fromkeys(skill_names))
    return {
        name: sum(
            name in {
                target
                for target in item.get("target_skills", [])
                if isinstance(target, str)
            }
            for item in items
        )
        for name in names
    }


def plan_disjoint_plugin_coverage(
    items: list[dict],
    required_training_skills: list[str] | tuple[str, ...],
    required_validation_skills: list[str] | tuple[str, ...],
) -> PluginCoveragePlan:
    """Reserve disjoint train/validation covers while retaining one task."""
    required_training = tuple(dict.fromkeys(required_training_skills))
    required_validation = tuple(dict.fromkeys(required_validation_skills))
    required = set(required_training) | set(required_validation)
    coverage = [
        {
            name
            for name in item.get("target_skills", [])
            if isinstance(name, str) and name in required
        }
        for item in items
    ]
    source_counts = {
        name: sum(name in targets for targets in coverage)
        for name in required
    }
    required_occurrences = {
        name: int(name in required_training) + int(name in required_validation)
        for name in required
    }
    insufficient = [
        name
        for name in (*required_training, *required_validation)
        if source_counts[name] < required_occurrences[name]
    ]
    insufficient = list(dict.fromkeys(insufficient))
    if insufficient:
        details = ", ".join(
            f"{name}={source_counts[name]}/{required_occurrences[name]}"
            for name in insufficient
        )
        raise ValueError(
            "Plugin ratio split cannot provide disjoint training and "
            "validation coverage; source task counts are insufficient for: "
            f"{details}"
        )

    def select_cover(
        available: list[int],
        required_names: tuple[str, ...],
        *,
        preserve: tuple[str, ...] = (),
    ) -> list[int]:
        selected: list[int] = []
        selected_set: set[int] = set()
        uncovered = set(required_names)
        remaining_counts = {
            name: sum(name in coverage[index] for index in available)
            for name in preserve
        }
        while uncovered:
            best_index = -1
            best_gain: set[str] = set()
            for index in available:
                if index in selected_set:
                    continue
                targets = coverage[index]
                if any(
                    name in targets and remaining_counts[name] <= 1
                    for name in preserve
                ):
                    continue
                gain = targets & uncovered
                if len(gain) > len(best_gain):
                    best_index = index
                    best_gain = gain
            if best_index < 0:
                raise ValueError(
                    "Plugin ratio split cannot construct disjoint coverage "
                    f"for: {list(required_names)}"
                )
            selected.append(best_index)
            selected_set.add(best_index)
            uncovered -= best_gain
            for name in preserve:
                if name in coverage[best_index]:
                    remaining_counts[name] -= 1
        return selected

    all_indices = list(range(len(items)))
    for reserved_test_index in all_indices:
        available = [
            index for index in all_indices if index != reserved_test_index
        ]
        try:
            validation_indices = select_cover(
                available,
                required_validation,
                preserve=required_training,
            )
            validation_set = set(validation_indices)
            after_validation = [
                index for index in available if index not in validation_set
            ]
            training_indices = select_cover(
                after_validation,
                required_training,
            )
        except ValueError:
            continue
        training_set = set(training_indices)
        remaining_indices = [
            index
            for index in all_indices
            if index not in training_set and index not in validation_set
        ]
        return PluginCoveragePlan(
            training_indices=tuple(training_indices),
            validation_indices=tuple(validation_indices),
            remaining_indices=tuple(remaining_indices),
        )

    raise ValueError(
        "Plugin ratio split cannot keep test non-empty while "
        "constructing disjoint training and validation coverage"
    )
