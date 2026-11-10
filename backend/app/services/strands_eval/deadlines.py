"""A wall-clock deadline on every judge call (production gap P2.2).

The judge client's own bounds multiply — the read timeout, botocore's retry attempts and the
engine's transient retries — so one hung judge call could hold its session (and its worker
slot) for the better part of an hour before it became an error row. :class:`DeadlineEvaluator`
runs one judge call on a helper thread and stops waiting after ``deadline_s``; the unit then
fails at once with :class:`JudgeDeadlineExceeded` (``error_type`` ``judge_deadline``), whose
name and text match neither the throttling predicate nor the transient-upstream markers, so
nothing re-runs it. The abandoned call finishes (or times out) on its own thread; its result
is discarded.

:class:`DeadlineEvaluatorSet` is ``engine_core.EvaluatorSet`` with every JUDGE evaluator it
hands out wrapped (deterministic trajectory matchers and code evaluators — which carry their
own Lambda timeout — are not). The hub puts ``Settings.eval_judge_deadline_s`` on the job
spec as ``judge_deadline_s`` (:func:`deadline_of`). Shipped into the worker zip.

Token accounting (production gap P8.3) rides on the same wrapper: :func:`counting_factory`
makes every judge model it builds add the stream's ``metadata.usage`` to the CURRENT call's
counter, and a :class:`UsageLedger` collects each judge call's tokens per unit — ``(session,
evaluator, trace)`` for a TRACE unit, ``(session, evaluator, None)`` otherwise — so
:meth:`UsageLedger.stamp` can put ``token_usage {inputTokens, outputTokens, totalTokens}``
(Global's result shape) on the unit's first row and the job can report its totals and stop at
a token budget (P8.4).
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from strands_evals.types.trace import Session

from app.services.strands_eval import engine_core
from app.services.strands_eval import evaluators as evaluator_lib

DEFAULT_JUDGE_DEADLINE_S = 300
USAGE_KEYS = ("inputTokens", "outputTokens", "totalTokens")
# the token counter of the judge call running in this context (set by DeadlineEvaluator)
_CALL_USAGE: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "_lp_call_usage", default=None)


def counting_factory(judge_factory: Callable[..., Any]) -> Callable[..., Any]:
    """``judge_factory`` whose models add each stream's ``metadata.usage`` to the running
    call's counter (no-op outside a counted call)."""

    def factory(*args: Any, **kwargs: Any) -> Any:
        model = judge_factory(*args, **kwargs)
        stream = getattr(model, "stream", None)
        if model is None or stream is None or getattr(stream, "_lp_counting", False):
            return model

        async def counted(*s_args: Any, **s_kwargs: Any) -> Any:
            async for event in stream(*s_args, **s_kwargs):
                usage = ((event or {}).get("metadata") or {}).get("usage") \
                    if isinstance(event, dict) else None
                counter = _CALL_USAGE.get()
                if usage and counter is not None:
                    for key in USAGE_KEYS:
                        counter[key] = counter.get(key, 0) + int(usage.get(key) or 0)
                yield event

        counted._lp_counting = True  # type: ignore[attr-defined]
        try:
            model.stream = counted
        except Exception:  # noqa: BLE001 — a model that refuses the hook is simply uncounted
            pass
        return model

    return factory


class UsageLedger:
    """Judge tokens per unit and in total for one job (thread-safe)."""

    def __init__(self) -> None:
        self._units: dict[tuple[str, str, str | None], dict[str, int]] = {}
        self._total = dict.fromkeys(USAGE_KEYS, 0)
        self._lock = threading.Lock()

    def add(self, key: tuple[str, str, str | None], usage: dict[str, int]) -> None:
        if not any(usage.get(k) for k in USAGE_KEYS):
            return
        with self._lock:
            unit = self._units.setdefault(key, dict.fromkeys(USAGE_KEYS, 0))
            for k in USAGE_KEYS:
                unit[k] += int(usage.get(k) or 0)
                self._total[k] += int(usage.get(k) or 0)

    def totals(self) -> dict[str, int]:
        with self._lock:
            return dict(self._total)

    def spent(self, budget: int | None) -> bool:
        return bool(budget) and self.totals()["totalTokens"] >= int(budget or 0)

    def stamp(self, records: list[dict[str, Any]]) -> None:
        """Put each unit's tokens on its first row (``token_usage``)."""
        with self._lock:
            for rec in records:
                if rec.get("token_usage") is not None:
                    continue
                trace = rec.get("trace_id") if rec.get("level") == "TRACE" else None
                key = (str(rec.get("session_id") or ""), str(rec.get("evaluator_id") or ""),
                       trace)
                usage = self._units.pop(key, None)
                if usage is not None:
                    rec["token_usage"] = usage


def unit_key(definition: dict[str, Any], case: Any) -> tuple[str, str, str | None]:
    session = getattr(case, "actual_trajectory", None)
    session_id = str(getattr(session, "session_id", None) or getattr(case, "name", "") or "")
    trace = None
    if evaluator_lib.level_of(definition) == "TRACE" and isinstance(session, Session):
        trace = next((t.trace_id for t in reversed(session.traces) if t.trace_id), None)
    return session_id, str(definition.get("id") or ""), trace


class JudgeDeadlineExceeded(RuntimeError):
    """A judge call that did not answer within its deadline → one error record."""

    error_type = "judge_deadline"

    def __init__(self, label: str, deadline_s: float) -> None:
        super().__init__(f"{label}: no answer within the {deadline_s:g} s judge deadline "
                         "(eval_judge_deadline_s) — the call was abandoned")


class DeadlineEvaluator:
    """``inner`` with ``evaluate`` bounded by ``deadline_s``; every other attribute is the
    inner evaluator's."""

    def __init__(self, inner: Any, *, deadline_s: float, label: str,
                 usage: UsageLedger | None = None,
                 definition: dict[str, Any] | None = None) -> None:
        self._inner = inner
        self._deadline_s = float(deadline_s)
        self._label = label
        self._usage = usage
        self._definition = dict(definition or {})

    def _counted(self, case: Any, counter: dict[str, int]) -> Any:
        _CALL_USAGE.set(counter)
        return self._inner.evaluate(case)

    def evaluate(self, case: Any) -> Any:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="judge-deadline")
        context = contextvars.copy_context()  # the request's contextvars reach the call
        counter: dict[str, int] = {}
        future = pool.submit(context.run, self._counted, case, counter)
        try:
            outputs = future.result(timeout=self._deadline_s)
        except FutureTimeout:
            raise JudgeDeadlineExceeded(self._label, self._deadline_s) from None
        finally:
            pool.shutdown(wait=False)
        if self._usage is not None and counter:
            self._usage.add(unit_key(self._definition, case), counter)
        return outputs

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# P2.2 follow-up: the run's own wall-clock bound, derived from the hub's wait so the worker
# job ends (and writes done.json) before the hub gives up on the run
RUN_DEADLINE_MARGIN_S = 120
MIN_RUN_DEADLINE_S = 60
# an in-flight unit can take this many judge-call deadlines: the call, two budget doublings,
# two N17-obs385 rescues
MAX_CALLS_PER_UNIT = 5


class RunDeadlineReached(RuntimeError):
    """A session the job never reached before its run deadline → one error record each."""

    error_type = "run_deadline"

    def __init__(self, deadline_s: float) -> None:
        super().__init__(f"not judged: the run reached its {deadline_s:g} s run deadline "
                         "(eval_batch_wait_s minus the in-flight judge allowance) first")


def run_deadline_s(batch_wait_s: float, judge_deadline_s: float) -> int:
    """``eval_batch_wait_s`` − the most an in-flight unit can still take − a margin."""
    return max(MIN_RUN_DEADLINE_S, int(batch_wait_s - MAX_CALLS_PER_UNIT * judge_deadline_s
                                       - RUN_DEADLINE_MARGIN_S))


def deadline_of(spec: dict[str, Any]) -> float:
    """The job's judge deadline (an older hub sends none → the default)."""
    value = spec.get("judge_deadline_s")
    return float(value) if value else float(DEFAULT_JUDGE_DEADLINE_S)


class DeadlineEvaluatorSet(engine_core.EvaluatorSet):
    """``EvaluatorSet`` whose judge evaluators run under ``deadline_s``."""

    def __init__(self, definitions: list[dict[str, Any]], judge_factory: Callable[..., Any],
                 *args: Any, deadline_s: float = DEFAULT_JUDGE_DEADLINE_S,
                 usage: UsageLedger | None = None, **kwargs: Any) -> None:
        super().__init__(definitions, counting_factory(judge_factory), *args, **kwargs)
        self.deadline_s = float(deadline_s)
        self.usage = usage

    def _wrap(self, definition: dict[str, Any], built: Any) -> Any:
        if not self.needs_judge(definition) or isinstance(built, DeadlineEvaluator):
            return built
        return DeadlineEvaluator(built, deadline_s=self.deadline_s,
                                 label=f"judge {definition.get('id')}", usage=self.usage,
                                 definition=definition)

    def evaluator(self, definition: dict[str, Any]) -> Any:
        return self._wrap(definition, super().evaluator(definition))

    def evaluator_with_budget(self, definition: dict[str, Any], max_tokens: int) -> Any:
        return self._wrap(definition, super().evaluator_with_budget(definition, max_tokens))

    def evaluator_on(self, definition: dict[str, Any], model_id: str | None,
                     max_tokens: int) -> Any:
        return self._wrap(definition, super().evaluator_on(definition, model_id, max_tokens))
