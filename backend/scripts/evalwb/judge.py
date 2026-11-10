"""``--judge``: the run's evaluators, scored locally by the production engine.

``engine_core.evaluate_sessions`` with an ``EvaluatorSet`` built like
``StrandsEngine._evaluator_set`` and the production ``default_judge_factory`` (Global Bedrock
in ``Settings.model_region`` through the bearer API key). The key is read from the CN
account's SSM parameter ``launchpad-global-bedrock-api-key`` into this process's
environment only — never printed, and scrubbed from every report string (:func:`scrub`).

Builtin and trajectory evaluators run as-is; a custom judge runs from its ledger row
(``evaluators_local.definition_of`` over the row the run resolver read). Code evaluators
are skipped: they invoke a Lambda in the CN account, which the workbench never does.
"""

from __future__ import annotations

import os
import time
from typing import Any

BEDROCK_KEY_PARAMETER = "launchpad-global-bedrock-api-key"
_SECRETS: list[str] = []


def scrub(text: Any) -> Any:
    """``text`` with any loaded secret replaced by ``***`` (non-strings pass through)."""
    if not isinstance(text, str):
        return text
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


def load_bedrock_key(cn_session: Any) -> None:
    """The Global Bedrock API key → this process's environment (the variable the
    production client reads, ``aws_clients.GLOBAL_BEDROCK_BEARER_ENV``, and the provider
    row's ``api_key_env``). Never logged."""
    from app.services import aws_clients
    from app.services.model_credential import provider_row

    value = cn_session.client("ssm").get_parameter(
        Name=BEDROCK_KEY_PARAMETER, WithDecryption=True)["Parameter"]["Value"].strip()
    if not value:
        raise RuntimeError(f"SSM parameter {BEDROCK_KEY_PARAMETER} is empty")
    _SECRETS.append(value)
    os.environ[aws_clients.GLOBAL_BEDROCK_BEARER_ENV] = value
    env_name = provider_row("bedrock").get("api_key_env")
    if env_name:
        os.environ[env_name] = value


def definitions(evaluator_ids: list[str], custom_rows: list[dict[str, Any]]
                ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(definitions, skipped)`` for ``evaluator_ids`` — builtins as ``{"id"}`` (what
    ``evaluators_local.resolve_definitions`` returns for them), customs from their rows."""
    from app.evaluation import evaluators_local
    from app.evaluation.models import EvalEvaluator
    from app.services.strands_eval import evaluators as evaluator_lib

    rows = {str(r.get("id")): r for r in custom_rows}
    out: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for evaluator_id in dict.fromkeys(evaluator_ids):
        if evaluator_id in evaluator_lib.LEVELS:
            out.append({"id": evaluator_id})
            continue
        row = rows.get(evaluator_id)
        if row is None:
            skipped.append({"id": evaluator_id, "reason": "no ledger row (pass --run)"})
            continue
        definition = evaluators_local.definition_of(EvalEvaluator(**row))
        if definition.get("definition") == "code":
            skipped.append({"id": evaluator_id, "reason": "code evaluator (CN Lambda)"})
            continue
        out.append(definition)
    return out, skipped


def ground_truth(payload: dict[str, Any] | None, session_ids: list[str]
                 ) -> dict[str, dict[str, Any]]:
    """The run's dataset ground truth per session (``engine_core.ground_truth_from_scenarios``
    over the scenarios the run's ``scenario_map`` pairs with each session)."""
    from app.services.strands_eval import engine_core

    if not payload:
        return {}
    items = ((payload.get("dataset") or {}).get("items")) or []
    by_id = {str(i.get("scenario_id")): i for i in items if isinstance(i, dict)}
    scenario_map = payload["run"].get("scenario_map") or {}
    pairs = [(by_id[str(m.get("scenario_id"))], sid) for sid in session_ids
             if isinstance(m := scenario_map.get(sid), dict)
             and str(m.get("scenario_id")) in by_id]
    if not pairs:
        return {}
    truth = engine_core.ground_truth_from_scenarios([p[0] for p in pairs],
                                                    [p[1] for p in pairs])
    for scenario, sid in pairs:  # Global tells "no tools expected" ([]) from "not given"
        if sid in truth:
            truth[sid]["expected_trajectory_defined"] = isinstance(
                scenario.get("expected_trajectory"), list)
    return truth


def judge_model(explicit: str | None, payload: dict[str, Any] | None) -> str:
    from app.core.config import get_settings

    host = ((payload or {}).get("settings") or {}).get("eval_judge_model_id")
    return explicit or host or get_settings().eval_judge_model_id


def evaluate(provider: Any, session_ids: list[str], defs: list[dict[str, Any]],
             truth: dict[str, dict[str, Any]], model_id: str, *, concurrency: int = 4,
             progress: Any = None) -> list[dict[str, Any]]:
    """Result records (the ``eval_results`` row shape) — one ``evaluate_sessions`` call."""
    from app.core.config import get_settings
    from app.evaluation.judge_budget import judge_budget, judge_budget_cap
    from app.evaluation.strands_engine import default_judge_factory
    from app.services.strands_eval import engine_core

    settings = get_settings()
    evaluators = engine_core.EvaluatorSet(
        defs, default_judge_factory, lambda_client=None, default_model_id=model_id,
        code_evaluators_enabled=False, budget_cap=judge_budget_cap(model_id, settings),
        default_budget=judge_budget(model_id, settings),
        transient_backoff=settings.eval_transient_retry_backoff,
    )
    records = engine_core.evaluate_sessions(
        provider, list(session_ids), evaluators, truth, job_id="evalwb", sleep=time.sleep,
        progress=progress, concurrency=concurrency)
    return [{k: scrub(v) for k, v in r.items()} for r in records]
