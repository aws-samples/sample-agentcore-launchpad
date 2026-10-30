"""Managed-evaluator lookups for the ordinary Evaluation surfaces (N12 W-E5, CA-5).

An evaluator "managed by an operation" is one the architect assistant's
evaluation-assets materialization created (``app.assistant.evaluation_assets``); the
evaluator CRUD refuses to delete it directly, coverage decodes a managed code
evaluator's reference needs from its plan rules, and SCORE NOW / online configs refuse
reference-driven code evaluators without ground truth. The signatures are the Global
baseline's; the bodies delegate to the assets module (imported lazily — it imports the
evaluation package itself).
"""

from typing import Any


def managed_evaluator(db: Any, workspace_id: str | None, evaluator_id: str) -> dict | None:
    from app.assistant import evaluation_assets as assets

    return assets.managed_evaluator(db, workspace_id, evaluator_id)


def managed_rules(db: Any, owner: dict | None) -> list[dict] | None:
    from app.assistant import evaluation_assets as assets

    return assets.managed_rules(db, owner)


def managed_package_evaluator_count(db: Any, owner: dict | None) -> int | None:
    from app.assistant import evaluation_assets as assets

    return assets.managed_package_evaluator_count(db, owner)


def managed_reference_gap(
    db: Any, workspace_id: str | None, evaluator_ids: list[str], available: set[str]
) -> dict[str, list[str]]:
    from app.assistant import evaluation_assets as assets

    return assets.managed_reference_gap(db, workspace_id, evaluator_ids, available)
