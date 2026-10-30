"""Result records (the Global results-stream row) and run-score aggregation (plan §B.2)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from strands_evals.types.evaluation import EvaluationOutput

from app.evaluation.jobs import record_id
from app.services.strands_eval.evaluators import LEVELS

RECORD_FIELDS = ("session_id", "trace_id", "span_id", "tool_name", "evaluator_id", "level",
                 "score", "label", "explanation", "error_type", "error_message",
                 "extraction_status")
SPAN_STATUS_ERROR = "span_status_error"  # a failed turn's row (N17-obs373)
# N17-obs324: the unit a level's mean is taken over (``aggregate_scores``)
LEVEL_UNITS = {"SESSION": "session", "TRACE": "turn", "TOOL_CALL": "tool_call"}


def records_from_outputs(
    *, job_id: str, session_id: str, evaluator_id: str, level: str,
    outputs: list[EvaluationOutput], trace_id: str | None = None, ordinal_start: int = 0,
    units: list[dict[str, Any]] | None = None, extraction_status: str | None = None,
) -> list[dict[str, Any]]:
    """One record per ``EvaluationOutput`` (TOOL_CALL evaluators yield one per tool call),
    each with the deterministic ``id`` the ingest dedupes on. ``ordinal_start`` keeps the
    ids of a per-turn TRACE evaluation apart (turn k starts at its own ordinal);
    ``units[i]`` (``{trace_id, span_id, tool_name}``) stamps output i with the tool call it
    judged — given only when it lines up 1:1 with ``outputs`` (N17-obs324);
    ``extraction_status`` is the reconciliation status of the judged turn (a unit's own
    ``extraction_status`` wins)."""
    records = []
    stamps = units if units is not None and len(units) == len(outputs) else None
    for index, out in enumerate(outputs):
        # NOT_APPLICABLE and a categorical-scale verdict (``score`` None) carry no score
        not_applicable = (out.label or "").upper() == "NOT_APPLICABLE" or out.score is None
        # N17-obs328: an output that names an error (a code evaluator's unscored unit) is an
        # error row in its own slot
        error_type = getattr(out, "error_type", None)
        stamp = stamps[index] if stamps else {}
        records.append({
            "id": record_id(job_id, session_id, evaluator_id, ordinal_start + index),
            "session_id": session_id, "trace_id": stamp.get("trace_id") or trace_id,
            "span_id": stamp.get("span_id"), "tool_name": stamp.get("tool_name"),
            "evaluator_id": evaluator_id,
            "level": level,
            "score": None if not_applicable or error_type else float(out.score),
            "label": None if error_type else out.label,
            "explanation": None if error_type else out.reason,
            "error_type": str(error_type) if error_type else None,
            "error_message": str(getattr(out, "error_message", "") or "") if error_type
            else None,
            "extraction_status": stamp.get("extraction_status") or extraction_status,
            # N17-obs411: decided by a rule, not a judge (``evaluators.DeterministicOutput``)
            **({"deterministic": out.deterministic}
               if getattr(out, "deterministic", None) else {}),
        })
    return records


def note_rescue(records: list[dict[str, Any]], outputs: Any) -> list[dict[str, Any]]:
    """N17-obs385: the records of a judgement the budget rescue produced carry how
    (``judge_rescue``, folded into the row's ``judge_inference`` at ingest) and say so at the
    head of their explanation; other records pass through unchanged."""
    rescue = getattr(outputs, "rescue", None)
    if not rescue:
        return records
    how = (f"judged by {rescue.get('model_id')}"
           + (f" (the judge_fallback model for {rescue['fallback_from']})"
              if rescue.get("fallback_from") else "")
           + (" on a truncated context" if rescue.get("context_truncated") else "")
           + f" after the {rescue.get('budget')}-token output budget ran out")
    for record in records:
        record["judge_rescue"] = dict(rescue)
        record["explanation"] = f"[{how}] {record.get('explanation') or ''}".rstrip()
    return records


def error_record(
    *, job_id: str, session_id: str, evaluator_id: str, level: str, error: BaseException,
    trace_id: str | None = None, ordinal: int = 0, extraction_status: str | None = None,
    span_id: str | None = None, tool_name: str | None = None,
) -> dict[str, Any]:
    """A failed judgement: no score, the exception on the row (never a crashed run). A
    wrapper that names its cause's class (``error_type`` — ``engine_core.
    UpstreamTransientExhausted`` after the N12-obs53 retries) is typed as that cause.
    ``span_id`` / ``tool_name`` name the tool call of a TOOL_CALL error row."""
    return {
        "id": record_id(job_id, session_id, evaluator_id, ordinal),
        "session_id": session_id, "trace_id": trace_id, "span_id": span_id,
        "tool_name": tool_name,
        "evaluator_id": evaluator_id, "level": level, "score": None, "label": None,
        "explanation": None,
        "error_type": str(getattr(error, "error_type", None) or type(error).__name__),
        "error_message": str(error)[:2000], "extraction_status": extraction_status,
    }


def aggregate_scores(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``EvalRun.scores`` shape: ``[{evaluatorId, score, count, n, unit}]`` (N17-obs324).

    The headline ``score`` is the mean over the evaluator's scored rows, i.e. over its
    level's unit (``unit``): turns for a TRACE evaluator (one row per turn, as Global
    scores them), sessions for a SESSION evaluator, tool calls for a TOOL_CALL one. ``n`` is
    the number of units scored, ``count`` the number of sessions they came from (the
    historical meaning). Error and not-applicable rows carry no score and are skipped;
    ``failed_turns`` counts the rows of turns whose agent span failed (N17-obs373,
    ``span_status_error``), which the mean excludes."""
    sums: dict[str, float] = defaultdict(float)
    n: dict[str, int] = defaultdict(int)
    sessions: dict[str, set[str]] = defaultdict(set)
    levels: dict[str, str] = {}
    failed: dict[str, int] = defaultdict(int)
    for rec in records:
        if rec.get("error_type") == SPAN_STATUS_ERROR:
            failed[str(rec["evaluator_id"])] += 1
        if rec.get("score") is None:
            continue
        evaluator = str(rec["evaluator_id"])
        sums[evaluator] += float(rec["score"])
        n[evaluator] += 1
        sessions[evaluator].add(str(rec.get("session_id")))
        # a row without a level (older shards) takes its builtin's
        levels.setdefault(evaluator, str(rec.get("level") or LEVELS.get(evaluator) or ""))
    return [
        {"evaluatorId": evaluator, "score": round(sums[evaluator] / n[evaluator], 4),
         "count": len(sessions[evaluator]), "n": n[evaluator],
         "unit": LEVEL_UNITS.get(levels[evaluator], "session"),
         **({"failed_turns": failed[evaluator]} if failed.get(evaluator) else {})}
        for evaluator in sorted(sums)
    ]
