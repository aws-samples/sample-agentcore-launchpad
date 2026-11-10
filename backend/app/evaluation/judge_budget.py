"""The hub's judge / optimizer output budget (N-SCN-bug2) — settings × capability table.

``Settings.eval_judge_max_tokens`` is the FLOOR every judge call starts from;
``schemas.agent.MODEL_CAPABILITIES[model]["default_judge_max_tokens"]`` raises it for the
reasoning models (GLM 5.3 → 4096, Kimi K3 → 2048) whose thinking tokens count against
``max_tokens``; ``Settings.eval_judge_max_tokens_cap`` bounds the doubling retries
(``services/strands_eval/model_calls.call_with_budget``). The worker inlines the same table
(``templates/eval_worker``) and gets the floor / cap rendered at install time, so dev and
prod start every judge at the same budget.

P3.1 / P3.2 (N17-obs349 / obs350): :func:`judge_inference` is what a scoring judge on a model
actually runs with — recorded on every run, online config and result row.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.schemas.agent import (
    inference_param_support,
    judge_budget_defaults,
    judge_default_model_id,
    model_max_output_tokens,
)
from app.services.strands_eval import model_calls


def judge_budget(model_id: str | None = None, settings: Any = None) -> int:
    """Starting ``max_tokens`` for a judge call on ``model_id`` (default judge when ``None``)."""
    settings = settings or get_settings()
    model = model_id or settings.eval_judge_model_id
    return model_calls.resolve_budget(
        model, floor=int(settings.eval_judge_max_tokens), defaults=judge_budget_defaults(),
        cap=judge_budget_cap(model, settings),
    )


def judge_budget_cap(model_id: str | None = None, settings: Any = None) -> int:
    """The ceiling the doubling retries stop at for ``model_id``."""
    settings = settings or get_settings()
    model = model_id or settings.eval_judge_model_id
    limits = {model: lim for model, lim in ((model, model_max_output_tokens(model)),) if lim}
    return model_calls.effective_cap(model, int(settings.eval_judge_max_tokens_cap), limits)


# Every scoring judge asks for this temperature (engine_core / jobs_batch / jobs_online / the
# worker's ``judge_model``); ``aws_clients.build_chat_model`` drops it for a model whose
# catalog row rejects it (Kimi K3), which then samples at its provider's default.
JUDGE_TEMPERATURE = 0.0
PROVIDER_DEFAULT = "provider default"


def judge_inference(model_id: str | None = None, settings: Any = None) -> dict[str, Any]:
    """The effective inference config of a scoring judge on ``model_id`` (the ``judge`` model
    role when ``None``): ``{model_id, temperature, temperature_applied, max_tokens}``.
    ``temperature`` is ``0.0`` when the model takes it, else the string
    ``"provider default"`` (``temperature_applied`` false) — the judge is not deterministic
    and the operator is told so instead of reading a temperature that was never sent."""
    settings = settings or get_settings()
    model = model_id or judge_default_model_id()
    applied = bool(inference_param_support(model).get("temperature", True))
    return {
        "model_id": model,
        "temperature": JUDGE_TEMPERATURE if applied else PROVIDER_DEFAULT,
        "temperature_applied": applied,
        "max_tokens": judge_budget(model, settings),
    }


def result_judges(evaluator_defs: list[dict[str, Any]], default_model_id: str | None,
                  settings: Any = None) -> dict[str, dict[str, Any] | None]:
    """``{evaluator_id: judge_inference | None}`` for the result rows of a run / config:
    a custom or derived row's own model, else ``default_model_id``; ``None`` for the
    evaluators that call no model (trajectory matchers, code evaluators)."""
    from app.services.strands_eval.evaluators import TRAJECTORY_LEVELS

    cache: dict[str, dict[str, Any]] = {}
    out: dict[str, dict[str, Any] | None] = {}
    for definition in evaluator_defs:
        evaluator_id = str(definition.get("id") or "")
        if evaluator_id in TRAJECTORY_LEVELS or definition.get("definition") == "code":
            out[evaluator_id] = None
            continue
        model = str(definition.get("model_id") or default_model_id or "") or None
        key = model or ""
        if key not in cache:
            cache[key] = judge_inference(model, settings)
        out[evaluator_id] = cache[key]
    return out


def judge_fallback_model_id() -> str | None:
    """N17-obs385: the ``judge_fallback`` model role a judge that ran out of output budget is
    re-tried on (``eval_judge_fallback_model_id``, "" = none) — None when unset."""
    from app.services import model_roles

    return model_roles.resolve("judge_fallback") or None


def row_judge(record: dict[str, Any], judges: dict[str, dict[str, Any] | None],
              settings: Any = None) -> tuple[bool, dict[str, Any] | None]:
    """``(known, judge_inference)`` for one ingested record: the evaluator's entry of
    ``judges`` (``result_judges``), or — when the budget rescue judged it (N17-obs385,
    ``record["judge_rescue"]``) — the config of the model that actually did, with the
    rescue on it; a row a deterministic rule decided (N17-obs411, ``record["deterministic"]``)
    gets ``{model_id: None, deterministic: <rule>}``. ``known`` is false when none says
    anything (the row keeps its stamp)."""
    evaluator_id = str(record.get("evaluator_id") or "")
    if record.get("deterministic"):  # N17-obs411: no judge produced this row
        return True, {"model_id": None, "deterministic": str(record["deterministic"])}
    rescue = record.get("judge_rescue")
    if isinstance(rescue, dict) and rescue.get("model_id"):
        return True, {**judge_inference(str(rescue["model_id"]), settings),
                      "rescue": dict(rescue)}
    if evaluator_id in judges:
        judge = judges[evaluator_id]
        return True, dict(judge) if judge else None
    return False, None


def failure_detection_inference(settings: Any = None) -> dict[str, Any]:
    """N17-obs449: the insights FailureAnalysis judge's inference config — the
    ``failure_detection`` model role's model, starting at the detector's own budget
    (``insights.DETECTION_START_BUDGET``, bounded by the model's cap)."""
    from app.services.strands_eval.insights import DETECTION_START_BUDGET

    settings = settings or get_settings()
    model = settings.eval_failure_detection_model_id
    info = judge_inference(model, settings)
    info["max_tokens"] = min(max(int(info["max_tokens"]), DETECTION_START_BUDGET),
                             judge_budget_cap(model, settings))
    return info

