"""``EvaluationEngine`` on the Strands adapter layer (N12 E1-3; plan §B.2).

The backend-side wrapper of ``services/strands_eval/engine_core.py``: it resolves the
evaluator rows, builds the funnel clients (``ws.client("logs")`` / ``("lambda")``) and the
judge (``aws_clients.global_bedrock_model`` — the only constructor, risk R-3), runs the same
core the Runtime worker runs, and persists ``eval_results`` rows. Registered in ``main.py``
with ``engine.set_engine(StrandsEngine())`` when the evaluation feature is on; SCORE NOW
(``persist=False``) and the in-process dev executor go through here, the production path
runs the identical library inside the worker.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.evaluation import evaluators_local, results_store
from app.evaluation.engine import EvalResultRecord, RecordsGuard, Source
from app.evaluation.judge_budget import (
    judge_budget,
    judge_budget_cap,
    judge_fallback_model_id,
    result_judges,
)
from app.services import aws_clients
from app.services.strands_eval import code_evaluators, deadlines, engine_core, insights
from app.services.strands_eval.spans_provider import LaunchpadSpansProvider


def default_judge_factory(model_id: str | None, *, max_tokens: int | None = None,
                          temperature: float = 0.0) -> Any:
    """``WorkerEnv.judge_factory`` for the in-process executor — the same keyword surface the
    worker's ``judge_model`` offers (E2's handlers pass ``max_tokens`` / ``temperature``).
    N-SCN-bug2: without an explicit ``max_tokens`` the budget is the model-aware default
    (``judge_budget``: GLM 5.3 4096, Kimi K3 2048, `eval_judge_max_tokens` as the floor)."""
    settings = get_settings()
    model = model_id or settings.eval_judge_model_id
    return aws_clients.global_bedrock_model(
        model, region=settings.model_region,
        max_tokens=max_tokens or judge_budget(model, settings), temperature=temperature,
    )


def evaluation_lookback_hours() -> int:
    """Shared bound for metadata preflight and the actual evaluation readers."""
    return max(1, get_settings().eval_batch_wait_s // 3600 + 24)


class _RequestRecordsGuard:
    """Retain a denial across the library's evaluator-error adapters, in this process only.

    No callable enters a job descriptor or the worker payload. A denied batch never reaches
    a mapper or evaluator, and the HTTP caller receives the original safe access error.
    """

    def __init__(self, guard: RecordsGuard) -> None:
        self._guard = guard
        self._error: Exception | None = None
        self._lock = threading.RLock()

    def raise_if_denied(self) -> None:
        with self._lock:
            if self._error is not None:
                raise self._error

    def __call__(self, session_id: str, records: list[dict[str, Any]]) -> None:
        with self._lock:
            self.raise_if_denied()
            try:
                self._guard(session_id, records)
            except Exception as exc:
                self._error = exc
                raise


class StrandsEngine:
    """``evaluation.engine.EvaluationEngine`` implementation.

    ``logs_client`` / ``lambda_client`` / ``judge_factory`` default to the funnel; tests
    inject fakes (a ``FakeLogs`` with the span fixture, a ``FakeJudge`` model)."""

    def __init__(
        self,
        *,
        logs_client: Callable[[Any], Any] | None = None,
        lambda_client: Callable[[Any], Any] | None = None,
        judge_factory: engine_core.JudgeFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
        session_factory: Callable[[], Any] = SessionLocal,
    ) -> None:
        self._logs_client = logs_client or (lambda ws: ws.client("logs"))
        # N17-obs333: per-timeout funnel clients (read timeout = lambda_timeout_s + margin)
        self._lambda_client = lambda_client or (lambda ws: code_evaluators.LambdaClients(
            lambda timeout_s: ws.client("lambda", cache_token=f"code-evaluator-{timeout_s}",
                                        config=code_evaluators.lambda_config(timeout_s))))
        self._judge_factory = judge_factory or default_judge_factory
        self._sleep = sleep
        self._session_factory = session_factory

    # ── wiring ──
    def provider(self, ws: Any, log_groups: list[str], *,
                 records_guard: RecordsGuard | None = None) -> LaunchpadSpansProvider:
        settings = get_settings()
        return LaunchpadSpansProvider(
            self._logs_client(ws), list(log_groups),
            lookback_hours=self._lookback_hours(),
            spans_source=settings.observability_spans_source,
            records_guard=records_guard,
        )

    @staticmethod
    def _lookback_hours() -> int:
        return evaluation_lookback_hours()

    def _evaluator_set(
        self, ws: Any, db: Any, evaluator_ids: list[str], judge_model_id: str | None,
        log_groups: list[str] | None = None, ground_truth: dict[str, dict] | None = None,
        records_guard: RecordsGuard | None = None,
    ) -> engine_core.EvaluatorSet:
        settings = get_settings()
        definitions = evaluators_local.resolve_definitions(db, ws, evaluator_ids)
        needs_lambda = any(d.get("definition") == "code" for d in definitions)
        model = judge_model_id or settings.eval_judge_model_id
        lambda_client = (self._lambda_client(ws) if needs_lambda
                         and settings.eval_code_evaluators_enabled else None)
        # P2.2: SCORE NOW / the in-process engine bound every judge call too
        return deadlines.DeadlineEvaluatorSet(
            definitions,
            self._judge_factory,
            deadline_s=settings.eval_judge_deadline_s,
            # N17-obs327: code evaluators get Global's envelope (session documents + truth)
            lambda_client=code_evaluators.runtime_for(
                lambda_client, logs_client=self._logs_client(ws) if lambda_client else None,
                log_groups=list(log_groups or []), lookback_hours=self._lookback_hours(),
                ground_truth=ground_truth, records_guard=records_guard),
            default_model_id=model,
            code_evaluators_enabled=settings.eval_code_evaluators_enabled,
            budget_cap=judge_budget_cap(model, settings),
            default_budget=judge_budget(model, settings),
            transient_backoff=settings.eval_transient_retry_backoff,
            fallback_model_id=judge_fallback_model_id(),
        )

    # ── Protocol ──
    def evaluate_sessions(
        self,
        ws: Any,
        *,
        source: Source,
        owner_id: str,
        session_ids: list[str],
        log_groups: list[str],
        evaluator_ids: list[str],
        ground_truth: dict[str, dict] | None,
        judge_model_id: str | None,
        stop: Callable[[], bool] | None,
        progress: Callable[[str], None] | None,
        persist: bool = True,
        references: dict[str, dict[str, Any]] | None = None,
        on_check: Callable[[str, dict[str, Any]], None] | None = None,
        records_guard: RecordsGuard | None = None,
    ) -> list[EvalResultRecord]:
        settings = get_settings()
        guard = _RequestRecordsGuard(records_guard) if records_guard is not None else None
        db = self._session_factory()
        try:
            evaluators = self._evaluator_set(ws, db, evaluator_ids, judge_model_id,
                                             log_groups, ground_truth, records_guard=guard)
            provider = self.provider(ws, log_groups, records_guard=guard)
            workspace_id = getattr(ws, "id", None)
            # P3.1 / P3.2: each row names the judge (and inference config) that produced it
            judges = result_judges(
                evaluators.definitions, judge_model_id or settings.eval_judge_model_id, settings)

            usage = deadlines.UsageLedger()  # P8.3: the judges' tokens per unit
            evaluators.usage = usage

            def flush(_session_id: str, records: list[dict[str, Any]]) -> None:
                if guard is not None:
                    guard.raise_if_denied()
                usage.stamp(records)
                if persist:
                    results_store.persist(db, workspace_id, source=source, owner_id=owner_id,
                                          records=records, judges=judges)

            records = engine_core.evaluate_sessions(
                provider, list(session_ids), evaluators, ground_truth, job_id=owner_id,
                sleep=self._sleep, stop=stop, progress=progress, on_records=flush,
                concurrency=settings.eval_judge_concurrency, references=references,
                on_check=on_check,
            )
            if guard is not None:
                guard.raise_if_denied()
        finally:
            db.close()
        return records  # type: ignore[return-value]

    def diagnose_sessions(
        self, ws: Any, *, session_ids: list[str], log_groups: list[str], kinds: list[str],
        judge_model_id: str | None,
    ) -> dict[str, Any]:
        settings = get_settings()
        model = judge_model_id or settings.eval_judge_model_id
        judge = self._judge_factory(model)
        # N17-obs449: FailureAnalysis on the `failure_detection` model role's judge
        detector_model = settings.eval_failure_detection_model_id
        detector = self._judge_factory(detector_model) if detector_model else None
        cap = max(judge_budget_cap(model, settings),
                  judge_budget_cap(detector_model, settings) if detector_model else 0)
        return insights.diagnose_sessions(self.provider(ws, log_groups), list(session_ids),
                                          list(kinds), judge, budget_cap=cap,
                                          detection_judge=detector)

    def session(self, ws: Any, *, session_id: str, log_groups: list[str]) -> Any:
        return self.provider(ws, log_groups).session(session_id)
