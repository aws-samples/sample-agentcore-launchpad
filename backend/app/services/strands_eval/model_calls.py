"""Judge / optimizer / summariser model calls under a growing output-token budget (N-SCN-bug2).

GLM 5.3 and Kimi K3 reason before they answer and the reasoning tokens count against
``max_tokens``; how much a judgement needs varies per session, so the same evaluator that
passed twice at 1024 tokens raised strands' ``MaxTokensReachedException`` on a 45-span
session (counting run #2, 2026-10-11) and the engine recorded a terminal error. Every model
call the evaluation stack makes goes through :func:`call_with_budget`: the configured budget
first, then — on the output limit — the budget doubled up to a cap, at most
``MAX_BUDGET_RETRIES`` retries, each one logged as ``budget k → 2k (attempt n/3)`` without
the prompt or the output. A partial answer the caller can still parse is accepted instead of
retried (``salvage``), and the final failure names the budget it tried
(``judge exceeded 4096 output tokens after 3 attempts``).

Pure library (inlined into the evaluation worker as ``launchpad_eval.model_calls``): no
settings, no AWS, no strands import — the exception is recognised by name so the worker's
pinned strands and the hub's agree. The per-model defaults live in the hub's one capability
table (``schemas.agent.MODEL_CAPABILITIES``) and reach this module as plain dicts
(``resolve_budget(defaults=…)``); the worker template inlines that table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)


DEFAULT_BUDGET = 1024  # the historical judge default (Settings.eval_judge_max_tokens)
DEFAULT_BUDGET_CAP = 16384  # Settings.eval_judge_max_tokens_cap default
MAX_BUDGET_RETRIES = 2  # 3 attempts: k, 2k, 4k (capped)
MIN_BUDGET = 256  # schemas.agent.MIN_MODEL_MAX_TOKENS: under this GLM returns no text at all
_MAX_TOKENS_EXCEPTION = "MaxTokensReachedException"  # strands.types.exceptions
_MAX_TOKENS_TEXT = "maximum token limit"  # its message, when only the text survives


class OutputTruncated(RuntimeError):
    """A text completion stopped at ``max_tokens`` with nothing the caller could parse.

    The text-completion paths (``jobs_recommend.model_converse``) turn strands' exception into
    a ``stop_reason == "max_tokens"`` result so the partial text can be inspected; when it
    is not usable they raise this, and :func:`call_with_budget` treats it exactly like the
    strands exception."""

    def __init__(self, max_tokens: int, detail: str = "") -> None:
        self.max_tokens = int(max_tokens)
        super().__init__(
            f"output truncated at {self.max_tokens} tokens" + (f": {detail}" if detail else "")
        )


class JudgeBudgetExceeded(RuntimeError):
    """The final failure: every attempt hit the output limit. ``budget`` is the LAST budget
    tried (the cap, or the configured value when it already was the cap)."""

    def __init__(self, label: str, budget: int, attempts: int, model_id: str | None = None,
                 ) -> None:
        self.label = label
        self.budget = int(budget)
        self.attempts = int(attempts)
        self.model_id = model_id
        plural = "" if self.attempts == 1 else "s"
        super().__init__(
            f"{label} exceeded {self.budget} output tokens after {self.attempts} attempt{plural}"
        )


def is_max_tokens_error(exc: BaseException) -> bool:
    """strands' ``MaxTokensReachedException`` (any version — matched by class name so the
    worker's pinned strands and the hub's agree), :class:`OutputTruncated`, or an error whose
    text is the strands message (a wrapper re-raised it as ``RuntimeError``)."""
    if isinstance(exc, OutputTruncated):
        return True
    if any(cls.__name__ == _MAX_TOKENS_EXCEPTION for cls in type(exc).__mro__):
        return True
    return _MAX_TOKENS_TEXT in str(exc)


def resolve_budget(
    model_id: str | None,
    *,
    floor: int,
    defaults: Mapping[str, int] | None = None,
    cap: int = DEFAULT_BUDGET_CAP,
    hard_limits: Mapping[str, int] | None = None,
) -> int:
    """The model-aware default budget: the larger of ``floor`` (``Settings.eval_judge_max_tokens``
    — a floor, not a ceiling) and the catalog default for ``model_id`` (GLM 5.3 4096, Kimi K3
    2048; an unknown model has none), never above :func:`effective_cap` nor under
    ``MIN_BUDGET``."""
    budget = max(int(floor), int((defaults or {}).get(str(model_id or ""), 0) or 0))
    budget = min(budget, effective_cap(model_id, cap, hard_limits))
    return max(budget, MIN_BUDGET)


def effective_cap(
    model_id: str | None, cap: int = DEFAULT_BUDGET_CAP,
    hard_limits: Mapping[str, int] | None = None,
) -> int:
    """``cap``, lowered to the model's documented output limit when the capability table
    carries one (``max_output_tokens`` — none verified for the CN catalog today)."""
    hard = (hard_limits or {}).get(str(model_id or ""))
    limit = int(cap)
    if hard:
        limit = min(limit, int(hard))
    return max(limit, MIN_BUDGET)


def next_budget(budget: int, cap: int) -> int | None:
    """``budget`` doubled, capped; ``None`` when there is no room left to grow."""
    grown = min(int(budget) * 2, int(cap))
    return grown if grown > int(budget) else None


def call_with_budget[T](
    call: Callable[[int], T],
    *,
    budget: int,
    cap: int = DEFAULT_BUDGET_CAP,
    retries: int = MAX_BUDGET_RETRIES,
    label: str = "judge",
    model_id: str | None = None,
    on_retry: Callable[[int, int, int], None] | None = None,
    salvage: Callable[[BaseException], T | None] | None = None,
    logger: logging.Logger = log,
) -> T:
    """``call(max_tokens)`` with the configured ``budget``; on the output limit retry with the
    budget doubled (≤ ``cap``), at most ``retries`` times.

    ``salvage(exc)`` may turn the failure into a result (a partial that still parses) — when
    it returns non-``None`` that result is accepted and nothing is retried.
    ``on_retry(attempt, previous, next)`` fires before each retry (progress lines). Every
    other exception propagates untouched. Logs name budgets and attempt counts only."""
    attempts_total = int(retries) + 1
    current = int(budget)
    who = f"{label} ({model_id})" if model_id else label
    for attempt in range(1, attempts_total + 1):
        try:
            return call(current)
        except Exception as exc:  # noqa: BLE001 — classified below
            if not is_max_tokens_error(exc):
                raise
            if salvage is not None:
                rescued = salvage(exc)
                if rescued is not None:
                    logger.info("%s hit the output limit at %d tokens — partial output accepted",
                                who, current)
                    return rescued
            grown = next_budget(current, cap)
            if attempt == attempts_total or grown is None:
                raise JudgeBudgetExceeded(label, current, attempt, model_id) from exc
            logger.warning("%s hit the output limit: budget %d → %d (attempt %d/%d)",
                           who, current, grown, attempt + 1, attempts_total)
            if on_retry is not None:
                on_retry(attempt + 1, current, grown)
            current = grown
    raise AssertionError("unreachable")  # pragma: no cover


def _config_params(config: dict[str, Any]) -> dict[str, Any] | None:
    """An ``OpenAIModel`` (N-R6) keeps its inference fields under ``params``; a
    ``BedrockModel`` carries them at the top level. ``None`` ⇒ top level."""
    params = config.get("params")
    return params if isinstance(params, dict) else None


def model_max_tokens(model: Any) -> int | None:
    """The ``max_tokens`` a strands ``Model`` currently carries (``get_config()``), if known."""
    try:
        config = model.get_config() or {}
        params = _config_params(config)
        value = (params if params is not None else config).get("max_tokens")
    except Exception:  # noqa: BLE001 — a model without the knob
        return None
    return int(value) if value else None


def _set_max_tokens(model: Any, max_tokens: int) -> None:
    params = _config_params(model.get_config() or {})
    if params is not None:
        model.update_config(params={**params, "max_tokens": int(max_tokens)})
    else:
        model.update_config(max_tokens=int(max_tokens))


@contextmanager
def model_budget(model: Any, max_tokens: int) -> Iterator[None]:
    """Run a call with ``model`` temporarily at ``max_tokens`` (``update_config``), restoring
    the previous value afterwards — for the sequential paths that share one judge object
    (insights, optimizer). A model without ``update_config`` runs unchanged."""
    before = model_max_tokens(model)
    changed = False
    if before != int(max_tokens):
        try:
            _set_max_tokens(model, int(max_tokens))
            changed = True
        except Exception:  # noqa: BLE001 — keep its own budget
            changed = False
    try:
        yield
    finally:
        if changed and before is not None:
            try:
                _set_max_tokens(model, before)
            except Exception:  # noqa: BLE001
                pass
