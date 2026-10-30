"""D21 (M1-d): the model descriptors a job payload carries to the evaluation worker.

The hub resolves every catalog model the job spec names — ``judge_model_id``,
``actor_model_id``, per-evaluator ``model_id`` and any other ``*model_id`` key, at any
depth — into ``schemas.agent.model_descriptor`` rows and puts them on ``JobSpec.models``
(additive, plan §5.5). The worker (`templates/eval_worker/main.py.tmpl`) registers them in
its model block's ``MODEL_DESCRIPTORS`` before the inlined ``MODEL_PROVIDERS`` /
``JUDGE_DEFAULT_MAX_TOKENS`` tables, so a capability edit reaches a judge through its job
without a preset repair; a worker that predates the field (or a payload without it) keeps
the tables. Hub-only: never shipped into the worker zip, imports the catalog views.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.evaluation.jobs import JobSpec
from app.schemas.agent import model_descriptor

MODEL_ID_KEY_SUFFIX = "model_id"


def referenced_model_ids(spec: Any) -> list[str]:
    """Every string under a ``*model_id`` key anywhere in ``spec`` (dicts / lists), in
    first-seen order, de-duplicated."""
    seen: dict[str, None] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).endswith(MODEL_ID_KEY_SUFFIX) and isinstance(value, str) and value:
                    seen.setdefault(value, None)
                else:
                    walk(value)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)

    walk(spec)
    return list(seen)


def descriptors_for(model_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """``{model_id: descriptor}`` for the ids the catalog knows; unknown ids are skipped (the
    worker's table / Bedrock default handles them exactly as before)."""
    out: dict[str, dict[str, Any]] = {}
    for model_id in model_ids:
        descriptor = model_descriptor(model_id)
        if descriptor is not None:
            out[model_id] = descriptor
    return out


def attach(job: JobSpec) -> JobSpec:
    """Fill ``job.models`` from the spec's model references (in place; returns the job)."""
    job.models = descriptors_for(referenced_model_ids(job.spec))
    return job
