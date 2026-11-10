"""Evidence for a recommendation: what one completed evaluation run scored, per session,
joined with the conversation each session had (N12 E2-4).

Scores are the run's ``eval_results`` rows (``source="run"``) — the Strands engine wrote
them where the Global batch evaluation used to stream its ``gen_ai.evaluation.result``
records. The conversation is rebuilt by ``observability.session_transcript`` (memory →
OTEL content logs for eval-run sessions); tool evidence by the content-log tool turns and
the tool spans. The backend collects all of it and ships plain dicts in the job spec:
the worker has no ledger.

Sampling is deliberate: the optimizer reads the WORST sessions first (that is where the
prompt's failure modes are), but keeps a minority of the best ones so it can see what the
current prompt already gets right and preserve it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalResult
from app.evaluation.online_evaluators import evaluator_polarity
from app.optimization.providers.base import (
    EvaluatorRecord,
    EvidenceStats,
    SessionEvidence,
    evidence_dict,
    stats_dict,
)
from app.services.strands_eval.jobs_recommend import tool_mentions
from app.services.workspace import WorkspaceContext

MAX_TURN_CHARS = 1500
_HEAD, _TAIL = 1200, 300
CONTRAST_SHARE = 0.2  # share of the selection reserved for best-scoring sessions
MIN_CONTRAST = 3
MAX_INSIGHT_LINES = 20
MAX_TOOL_TURNS = 20  # tool calls kept per session (each with its result)
MAX_TOOL_INPUT_CHARS = 400
SPAN_LOOKBACK_CAP_HOURS = 720

TranscriptFn = Callable[[str], list[dict[str, str]]]
# session id → tool_call / tool_result turns (see observability.eval_tool_turns_from_content_logs)
ToolTurnsFn = Callable[[str], list[dict[str, Any]]]
# session ids → {session id: [{"id", "name", "status", "description"}]} from tool spans
ToolSpansFn = Callable[[list[str]], dict[str, list[dict[str, Any]]]]

__all__ = [
    "MAX_TOOL_TURNS", "ToolSpansFn", "ToolTurnsFn", "TranscriptFn", "collect_evidence",
    "default_tool_spans", "default_tool_turns", "default_transcript", "evidence_payload",
    "group_records", "insight_feedback", "mean_polarized", "read_run_records",
    "select_sessions", "tool_mentions", "tool_spans_query", "truncate_text",
]


# ─── result records (eval_results rows of one run) ───────────────────────────


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def read_run_records(
    db: Session, run_id: str, workspace_id: str | None = None
) -> list[dict[str, Any]]:
    """The run's ``eval_results`` rows as plain dicts, oldest first."""
    query = select(EvalResult).where(EvalResult.source == "run", EvalResult.run_id == run_id)
    if workspace_id is not None:
        query = query.where(EvalResult.workspace_id == workspace_id)
    return [
        {
            "session_id": row.session_id, "trace_id": row.trace_id,
            "evaluator_id": row.evaluator_id, "level": row.level, "score": row.score,
            "label": row.label, "explanation": row.explanation,
            "error_type": row.error_type, "error_message": row.error_message,
        }
        for row in db.scalars(query.order_by(EvalResult.created_at.asc()))
    ]


def group_records(records: list[dict[str, Any]]) -> dict[str, list[EvaluatorRecord]]:
    grouped: dict[str, list[EvaluatorRecord]] = {}
    for rec in records:
        sid = rec.get("session_id")
        if not sid:
            continue
        grouped.setdefault(str(sid), []).append(
            EvaluatorRecord(
                evaluator_id=str(rec.get("evaluator_id")),
                score=_num(rec.get("score")),
                label=rec.get("label"),
                explanation=rec.get("explanation"),
                level=rec.get("level"),
                error=rec.get("error_message") or rec.get("error"),
            )
        )
    return grouped


def mean_polarized(records: list[EvaluatorRecord]) -> float | None:
    """Mean of ``polarity × score`` so "lower" always means "worse" — a penalty
    evaluator (Refusal) scores high when the agent misbehaved, and must not make a
    bad session look good."""
    vals = [
        evaluator_polarity(r.evaluator_id) * r.score
        for r in records
        if r.score is not None
    ]
    return sum(vals) / len(vals) if vals else None


def select_sessions(
    grouped: dict[str, list[EvaluatorRecord]], max_sessions: int
) -> list[str]:
    """Deterministic worst-first selection with a best-scoring contrast set.

    Only sessions with at least one numeric score qualify. When more qualify
    than ``max_sessions``, ``max(MIN_CONTRAST, 20 %)`` slots go to the best
    sessions and the rest to the worst; ties break on session id so a re-run
    reads the same evidence.
    """
    scored = [
        (mean, sid)
        for sid, recs in grouped.items()
        if (mean := mean_polarized(recs)) is not None
    ]
    if len(scored) <= max_sessions:
        return [sid for _, sid in sorted(scored)]
    worst_first = sorted(scored, key=lambda t: (t[0], t[1]))
    contrast_n = min(max(MIN_CONTRAST, round(max_sessions * CONTRAST_SHARE)), max_sessions)
    best = sorted(scored, key=lambda t: (-t[0], t[1]))[:contrast_n]
    chosen = {sid for _, sid in best}
    for _mean, sid in worst_first:
        if len(chosen) >= max_sessions:
            break
        chosen.add(sid)
    # worst first in the output too — that is the reading order the optimizer gets
    return [sid for _, sid in worst_first if sid in chosen]


# ─── transcripts ─────────────────────────────────────────────────────────────
def truncate_text(text: str, limit: int = MAX_TURN_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"{text[:_HEAD]}\n…[{len(text) - _HEAD - _TAIL} chars omitted]…\n{text[-_TAIL:]}"


def default_transcript(
    db: Any, workspace: WorkspaceContext, agent_row: Any
) -> TranscriptFn:
    from app.services import observability  # local — heavy module, avoids cycles

    def _fn(session_id: str) -> list[dict[str, str]]:
        result = observability.session_transcript(db, session_id, workspace, agent_row)
        return [
            {"role": str(t.get("role") or ""), "text": str(t.get("text") or ""),
             "at": str(t.get("at") or "")}
            for t in (result.get("turns") or [])
            if isinstance(t, dict)
        ]

    return _fn


def default_tool_turns(
    workspace: WorkspaceContext, agent_row: Any, started_at: datetime | None
) -> ToolTurnsFn | None:
    """Content-log tool turns for a runtime-backed agent; None when the agent has
    no runtime log group to read (tool evidence then comes from spans only)."""
    resource_id = getattr(agent_row, "resource_id", None)
    if not resource_id:
        return None
    from app.services import observability  # local — heavy module, avoids cycles

    log_group = f"/aws/bedrock-agentcore/runtimes/{resource_id}-DEFAULT"

    def _fn(session_id: str) -> list[dict[str, Any]]:
        return observability.eval_tool_turns_from_content_logs(
            log_group, session_id, started_at, workspace
        )

    return _fn


def tool_spans_query(session_ids: list[str]) -> str:
    """One Logs Insights query for every tool span of the given sessions."""
    from app.services.observability import SPANS_SOURCE  # local — avoids cycles

    ids = ", ".join(json.dumps(s) for s in session_ids)
    return f"""
{SPANS_SOURCE}
| filter attributes.session.id in [{ids}]
    and (ispresent(attributes.gen_ai.tool.name) or ispresent(attributes.tool.name))
| fields attributes.session.id as session_id,
         coalesce(attributes.gen_ai.tool.name, attributes.tool.name) as tool,
         attributes.gen_ai.tool.call.id as call_id,
         attributes.gen_ai.tool.status as tool_status,
         attributes.gen_ai.tool.description as description,
         status.code as status_code, startTimeUnixNano as started
| sort started asc
| limit 2000
"""


def default_tool_spans(
    workspace: WorkspaceContext, since: datetime | None
) -> ToolSpansFn:
    """Tool spans (name, call id, status, the description the model saw) per
    session — the span-side complement of the content-log turns. Fail-soft: any
    query problem yields {} (spans are advisory here)."""
    from app.services import observability  # local — heavy module, avoids cycles

    def _fn(session_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not session_ids:
            return {}
        start = since or datetime.now(UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        hours = math.ceil((datetime.now(UTC) - start).total_seconds() / 3600) + 1
        hours = max(1, min(hours, SPAN_LOOKBACK_CAP_HOURS))
        try:
            rows = observability.run_insights_queries(
                {"tools": tool_spans_query(session_ids)}, hours, workspace=workspace
            )["tools"]
        except Exception:
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            sid = r.get("session_id")
            if not sid or not r.get("tool"):
                continue
            errored = (r.get("tool_status") or "").lower() == "error" or "ERROR" in (
                r.get("status_code") or ""
            )
            out.setdefault(sid, []).append({
                "id": r.get("call_id") or None,
                "name": r["tool"],
                "status": "error" if errored else (r.get("tool_status") or "success"),
                "description": r.get("description") or None,
            })
        return out

    return _fn


def _tool_turns_from_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Span-only fallback: calls without arguments or results."""
    return [
        {"role": "tool_call", "id": sp.get("id"), "name": sp["name"], "input": None,
         "status": sp.get("status") or "", "source": "span"}
        for sp in spans
    ]


def _normalize_tool_turns(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Content-log turns → evidence turns: lower-case roles, truncated payloads,
    the per-session cap with an omission marker."""
    out: list[dict[str, Any]] = []
    calls = 0
    for t in raw:
        role = str(t.get("role") or "").lower()
        if role == "tool_call":
            calls += 1
            if calls > MAX_TOOL_TURNS:
                continue
            raw_in = t.get("input")
            text_in = json.dumps(raw_in, ensure_ascii=False) if raw_in is not None else ""
            out.append({
                "role": "tool_call", "id": t.get("id"), "name": str(t.get("name") or ""),
                "input": truncate_text(text_in, MAX_TOOL_INPUT_CHARS) or None,
                "at": str(t.get("at") or ""),
            })
        elif role == "tool_result":
            if calls > MAX_TOOL_TURNS:
                continue
            out.append({
                "role": "tool_result", "id": t.get("id"), "name": str(t.get("name") or ""),
                "status": str(t.get("status") or ""),
                "text": truncate_text(str(t.get("text") or "")),
                "at": str(t.get("at") or ""),
            })
    if calls > MAX_TOOL_TURNS:
        out.append({"role": "note", "text": f"… {calls - MAX_TOOL_TURNS} more tool calls omitted"})
    return out


def _merge_turns(
    text_turns: list[dict[str, Any]], tool_turns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Conversation order by timestamp when both sides carry one (ISO strings
    sort lexicographically); text before tool on ties; otherwise text first."""
    if not tool_turns:
        return text_turns
    if not text_turns:
        return tool_turns
    if all(t.get("at") for t in text_turns) and all(
        t.get("at") for t in tool_turns if t["role"] != "note"
    ):
        keyed = [(t["at"], 0, i, t) for i, t in enumerate(text_turns)] + [
            (t.get("at") or "￿", 1, i, t) for i, t in enumerate(tool_turns)
        ]
        return [t for _, _, _, t in sorted(keyed, key=lambda k: (k[0], k[1], k[2]))]
    return text_turns + tool_turns


def insight_feedback(insights: dict[str, Any] | None) -> list[str]:
    """Flatten an Insights run's failure-analysis tree into feedback lines."""
    lines: list[str] = []
    for category in (insights or {}).get("failures") or []:
        if not isinstance(category, dict):
            continue
        cat = category.get("category") or category.get("name") or ""
        for sub in category.get("subCategories") or [category]:
            if not isinstance(sub, dict):
                continue
            sub_name = sub.get("subCategory") or sub.get("name") or ""
            for rc in sub.get("rootCauses") or []:
                if not isinstance(rc, dict):
                    continue
                cause = rc.get("rootCause") or rc.get("description") or ""
                rec = rc.get("recommendation") or rc.get("fix_recommendation") or ""
                label = " / ".join(p for p in (cat, sub_name) if p)
                text = f"{label}: {cause}".strip(": ")
                if rec:
                    text += f" → {rec}"
                if text:
                    lines.append(text[:400])
                if len(lines) >= MAX_INSIGHT_LINES:
                    return lines
    return lines


# ─── entry point ─────────────────────────────────────────────────────────────
def collect_evidence(
    *,
    records: list[dict[str, Any]] | None = None,
    transcript: TranscriptFn,
    max_sessions: int,
    progress: Callable[[str], None] | None = None,
    tool_turns: ToolTurnsFn | None = None,
    tool_spans: ToolSpansFn | None = None,
    workspace: WorkspaceContext | None = None,
    log_group: str | None = None,
    log_stream: str | None = None,
    logs: Any = None,
) -> tuple[list[SessionEvidence], EvidenceStats]:
    """``records`` are the run's ``eval_results`` rows (:func:`read_run_records`). A
    caller may pass the run's *results identity* instead — ``results_log_group`` /
    ``results_log_stream``, where the stream **is the run id** — and the rows are read
    here; ``log_group`` / ``logs`` (the optional CloudWatch mirror) are accepted for
    call-compatibility and never read.
    ``tool_turns`` / ``tool_spans`` are given only when tool evidence is wanted (a
    provider producing tool descriptions). Content-log turns are the primary source
    (arguments + results); spans fill in sessions whose content records carry no tool
    parts and record the description the model actually saw."""
    if records is None:
        if not log_stream:
            raise ValueError("collect_evidence needs `records` or the run id as `log_stream`")
        session = SessionLocal()
        try:
            records = read_run_records(
                session, str(log_stream), getattr(workspace, "id", None) or DEFAULT_WORKSPACE_ID
            )
        finally:
            session.close()
    grouped = group_records(records)
    stats = EvidenceStats(
        records=len(records),
        sessions_scored=sum(1 for recs in grouped.values() if mean_polarized(recs) is not None),
    )
    chosen = select_sessions(grouped, max_sessions)
    stats.sessions_selected = len(chosen)
    spans_by_session: dict[str, list[dict[str, Any]]] = {}
    if tool_spans is not None and chosen:
        if progress:
            progress("reading tool spans…")
        try:
            spans_by_session = tool_spans(chosen) or {}
        except Exception:
            spans_by_session = {}
    evidence: list[SessionEvidence] = []
    for i, sid in enumerate(chosen, 1):
        if progress and (i == 1 or i % 5 == 0):
            progress(f"reading transcripts {i}/{len(chosen)}…")
        try:
            turns = transcript(sid)
        except Exception:
            turns = []  # one unreadable session never fails the evidence step
        text_turns = [
            {"role": str(t.get("role") or "").lower(), "text": truncate_text(t["text"]),
             "at": str(t.get("at") or "")}
            for t in turns
            if t.get("text", "").strip()
        ]
        if text_turns:
            stats.sessions_with_transcript += 1
        tool_part: list[dict[str, Any]] = []
        if tool_turns is not None:
            try:
                tool_part = _normalize_tool_turns(tool_turns(sid))
            except Exception:
                tool_part = []
        spans = spans_by_session.get(sid) or []
        if not tool_part and spans:
            tool_part = _tool_turns_from_spans(spans)  # spans only: no args/results
        for sp in spans:
            entry = stats.observed_tools.setdefault(
                sp["name"], {"calls": 0, "errors": 0, "description_seen": None}
            )
            entry["calls"] += 1
            if sp.get("status") == "error":
                entry["errors"] += 1
            if sp.get("description") and not entry["description_seen"]:
                entry["description_seen"] = str(sp["description"])
        calls = [t for t in tool_part if t["role"] == "tool_call"]
        if calls:
            stats.sessions_with_tool_calls += 1
            stats.tool_calls_seen += len(calls)
            if not spans:  # content logs only — still count the names
                for c in calls:
                    stats.observed_tools.setdefault(
                        c["name"], {"calls": 0, "errors": 0, "description_seen": None}
                    )["calls"] += 1
        evidence.append(
            SessionEvidence(
                session_id=sid,
                turns=_merge_turns(text_turns, tool_part),
                records=grouped[sid],
                mean_score=mean_polarized(grouped[sid]),
            )
        )
    return evidence, stats


def evidence_payload(
    evidence: list[SessionEvidence], stats: EvidenceStats
) -> dict[str, Any]:
    """The job-spec slice the worker-side reflection reads (``evidence`` + ``stats``)."""
    return {"evidence": [evidence_dict(ev) for ev in evidence], "stats": stats_dict(stats)}
