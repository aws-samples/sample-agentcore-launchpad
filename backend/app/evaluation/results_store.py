"""``eval_results`` persistence (N12 E1-3; CA-3) — the Global results log stream, locally.

One row per result record, keyed by the worker's deterministic ``record_id`` so ingesting
the same ``results.jsonl`` twice (retry, re-check, resumed poller) is idempotent. ``arm`` is
filled from ``experiment_assignments`` at write time so A/B verdicts need no join; a record
that names its own ``arm`` (the worker's replay error records, N12-obs62) keeps it when no
assignment row exists.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.evaluation.jobs import record_id
from app.evaluation.judge_budget import row_judge
from app.evaluation.models import EvalResult
from app.optimization.models import ExperimentAssignment

RECORD_KEYS = ("session_id", "trace_id", "span_id", "tool_name", "evaluator_id", "level",
               "score", "label", "explanation", "error_type", "error_message",
               "extraction_status")


def arms_for(db: Session, workspace_id: str | None, session_ids: list[str]) -> dict[str, str]:
    """``{session_id: arm}`` from the routing truth (newest assignment per session)."""
    if not session_ids:
        return {}
    rows = (
        db.query(ExperimentAssignment)
        .filter(ExperimentAssignment.workspace_id == workspace_id,
                ExperimentAssignment.session_id.in_(list(session_ids)))
        .order_by(ExperimentAssignment.created_at.asc())
        .all()
    )
    return {row.session_id: row.arm for row in rows}


def persist(
    db: Session, workspace_id: str | None, *, source: str, owner_id: str,
    records: list[dict[str, Any]],
    judges: dict[str, dict[str, Any] | None] | None = None,
) -> int:
    """Upsert ``records`` as ``eval_results`` rows; returns the number written. ``judges``
    (``judge_budget.result_judges``: ``{evaluator_id: judge_inference | None}``) stamps each
    row with the judge that produced it (P3.1 / P3.2); an evaluator it does not name keeps
    whatever the row already has."""
    if not records:
        return 0
    judges = judges or {}
    arms = arms_for(db, workspace_id, sorted({str(r.get("session_id")) for r in records}))
    written = 0
    for position, rec in enumerate(records):
        row_id = str(rec.get("id") or record_id(owner_id, str(rec.get("session_id")),
                                                  str(rec.get("evaluator_id")), position))
        row = db.get(EvalResult, row_id)
        if row is None:
            row = EvalResult(id=row_id, workspace_id=workspace_id, source=source,
                             session_id=str(rec.get("session_id")),
                             evaluator_id=str(rec.get("evaluator_id")))
            db.add(row)
        row.source = source
        row.run_id = owner_id if source == "run" else None
        row.config_id = owner_id if source == "online" else None
        row.trace_id = rec.get("trace_id")
        row.span_id = rec.get("span_id")
        row.tool_name = rec.get("tool_name")
        row.extraction_status = rec.get("extraction_status")
        row.level = rec.get("level")
        row.score = None if rec.get("score") is None else float(rec["score"])
        row.label = rec.get("label")
        row.explanation = rec.get("explanation")
        row.error_type = rec.get("error_type")
        row.error_message = rec.get("error_message")
        row.arm = arms.get(str(rec.get("session_id"))) or rec.get("arm")  # N12-obs62
        row.token_usage = rec.get("token_usage")  # P8.3
        known, judge = row_judge(rec, judges)
        if known:
            row.judge_inference = judge
            row.judge_model_id = str(judge["model_id"]) if judge else None
        written += 1
    db.commit()
    return written


def record_of(row: EvalResult) -> dict[str, Any]:
    return {
        "id": row.id, "session_id": row.session_id, "trace_id": row.trace_id,
        "span_id": row.span_id, "tool_name": row.tool_name,
        "evaluator_id": row.evaluator_id, "level": row.level, "score": row.score,
        "label": row.label, "explanation": row.explanation, "error_type": row.error_type,
        "error_message": row.error_message, "arm": row.arm, "judge": row.judge_inference,
        "extraction_status": row.extraction_status,
        "token_usage": row.token_usage,  # P8.3
    }


def read_run_records(db: Session, run_id: str) -> list[dict[str, Any]]:
    rows = (
        db.query(EvalResult)
        .filter(EvalResult.source == "run", EvalResult.run_id == run_id)
        # ingest order: created_at, then the SQLite rowid for rows of one microsecond
        .order_by(EvalResult.created_at.asc(), text("rowid"))
        .all()
    )
    return [record_of(row) for row in rows]


def delete_run_records(db: Session, run_id: str) -> int:
    count = (
        db.query(EvalResult)
        .filter(EvalResult.source == "run", EvalResult.run_id == run_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(count)


def normalize_result_record(rec: dict[str, Any]) -> dict[str, Any]:
    """The console's stable per-result row (the frozen ``RESULT_RECORD`` key-set, plus the
    CN additive ``arm`` — N12-obs62: the session's pre-assigned ``C`` / ``T1``, null for a
    plain run — and the CN additive ``judge``: the effective inference config of the judge
    that produced the row, ``judge_budget.judge_inference`` | null; P3.1 / P3.2).

    N17-obs317 (additive): ``trace_id`` (the turn the row judged), ``span_id`` / ``tool_name``
    (the call a TOOL_CALL row judged), ``extraction_status`` (that turn's reconciliation:
    ok | mismatch | unverifiable), and ``judge_model_id`` / ``judge_inference`` — the same
    names as the run-level fields. ``judge`` stays as an alias of ``judge_inference`` for
    existing clients."""
    judge = rec.get("judge")
    return {
        "trace_id": rec.get("trace_id"),
        "span_id": rec.get("span_id"),
        "tool_name": rec.get("tool_name"),
        "extraction_status": rec.get("extraction_status"),
        "judge_model_id": judge.get("model_id") if isinstance(judge, dict) else None,
        "judge_inference": judge,
        "evaluator_id": str(rec.get("evaluator_id")),
        "level": rec.get("level"),
        "score": rec.get("score"),
        "label": rec.get("label"),
        "explanation": rec.get("explanation"),
        "error_type": rec.get("error_type"),
        "error_message": rec.get("error_message"),
        "arm": rec.get("arm"),
        "judge": rec.get("judge"),
    }
