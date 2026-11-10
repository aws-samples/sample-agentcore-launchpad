"""Evaluation engine boundary (N12 E0-5; plan §B.2) — Protocol + slot.

W-E1's ``strands_engine.py`` implements it on the Strands adapter layer and registers it in
``main.py``; W-E2/W-E3 call it. Same slot pattern as ``services/memory_backend.py``: until
an engine is registered the routers answer 503 ``feature.unavailable``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol, TypedDict

from app.core.features import feature_unavailable

Source = Literal["run", "online", "ondemand"]
RecordsGuard = Callable[[str, list[dict[str, Any]]], None]


class EvalResultRecord(TypedDict):
    """One evaluator judgement on one session — the Global results-stream row and the
    ``eval_results`` columns (minus the ledger-only ``id``/``arm``/``source`` fields)."""

    session_id: str
    trace_id: str | None
    evaluator_id: str
    level: str | None
    score: float | None
    label: str | None
    explanation: str | None
    error_type: str | None
    error_message: str | None


class EvaluationEngine(Protocol):
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
        """Score ``session_ids`` with ``evaluator_ids``; ``persist`` writes ``eval_results``
        rows tagged ``source``/``owner_id`` (run id | online config id); ``stop()`` is
        polled between sessions; ``progress(text)`` feeds the run's progress line;
        ``on_check(session_id, extraction_check)`` receives each extracted session's
        reconciliation against ``references`` (eval accuracy item 1)."""
        ...

    def diagnose_sessions(
        self, ws: Any, *, session_ids: list[str], log_groups: list[str], kinds: list[str],
        judge_model_id: str | None,
    ) -> dict[str, Any]:
        """Insights blob in the baseline ``EvalRun.insights`` shape
        (``{failures, userIntents, executionSummaries}``)."""
        ...

    def session(self, ws: Any, *, session_id: str, log_groups: list[str]) -> Any:
        """The strands ``Session`` of one runtime session (recommendations, detectors)."""
        ...


_engine: EvaluationEngine | None = None


def set_engine(engine: EvaluationEngine | None) -> None:
    global _engine
    _engine = engine


def engine_or_none() -> EvaluationEngine | None:
    return _engine


def get_engine() -> EvaluationEngine:
    if _engine is None:
        raise feature_unavailable("evaluation")
    return _engine
