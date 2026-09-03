"""Evidence for a 3rd-party recommendation: what one completed batch evaluation
scored, per session, joined with the conversation each session had.

Scores come from the batch's own results log stream
(``GetBatchEvaluation.outputConfig.cloudWatchConfig`` → ``run-<batchId>`` in the
account-wide ``…/batch-evaluations/results/default`` group — live-verified
2026-09-03, see the task's research notes). Records are the same
``gen_ai.evaluation.result`` family the online-evaluation queries read. The
conversation is rebuilt by ``observability.session_transcript`` (memory → OTEL
content logs for eval-run sessions).

Sampling is deliberate: the optimizer reads the WORST sessions first (that is
where the prompt's failure modes are), but keeps a minority of the best ones so
it can see what the current prompt already gets right and preserve it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.evaluation import agentcore_eval as ac
from app.optimization.providers.base import EvaluatorRecord, EvidenceStats, SessionEvidence
from app.services.workspace import WorkspaceContext

MAX_EVENTS = 5000  # hard stop on the stream read (≈ 30 evaluators × 150 sessions)
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


# ─── results stream ──────────────────────────────────────────────────────────
def read_result_records(
    logs: Any, log_group: str, log_stream: str, *, max_events: int = MAX_EVENTS
) -> list[dict[str, Any]]:
    """Every ``gen_ai.evaluation.result`` record's ``attributes`` in the stream.

    ``get_log_events`` pages forward until the token stops changing (the
    documented end-of-stream signal); ``max_events`` bounds a runaway read.
    Unparseable events are skipped — one bad line must not void the run.
    """
    out: list[dict[str, Any]] = []
    token: str | None = None
    while len(out) < max_events:
        kwargs: dict[str, Any] = {
            "logGroupName": log_group,
            "logStreamName": log_stream,
            "startFromHead": True,
        }
        if token:
            kwargs["nextToken"] = token
        page = logs.get_log_events(**kwargs)
        for event in page.get("events") or []:
            try:
                body = json.loads(event["message"])
            except (KeyError, TypeError, ValueError):
                continue
            attrs = body.get("attributes") if isinstance(body, dict) else None
            if not isinstance(attrs, dict) or not attrs.get("gen_ai.evaluation.name"):
                continue
            out.append(attrs)
        nxt = page.get("nextForwardToken")
        if not nxt or nxt == token:
            break
        token = nxt
    return out


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def group_records(attrs_list: list[dict[str, Any]]) -> dict[str, list[EvaluatorRecord]]:
    grouped: dict[str, list[EvaluatorRecord]] = {}
    for attrs in attrs_list:
        sid = attrs.get("session.id")
        if not sid:
            continue
        grouped.setdefault(str(sid), []).append(
            EvaluatorRecord(
                evaluator_id=str(attrs.get("gen_ai.evaluation.name")),
                score=_num(attrs.get("gen_ai.evaluation.score.value")),
                label=attrs.get("gen_ai.evaluation.score.label"),
                explanation=attrs.get("gen_ai.evaluation.explanation"),
                level=attrs.get("aws.bedrock_agentcore.evaluation_level"),
                error=attrs.get("error.message"),
            )
        )
    return grouped


def mean_polarized(records: list[EvaluatorRecord]) -> float | None:
    """Mean of ``polarity × score`` so "lower" always means "worse" — a penalty
    evaluator (Refusal, Harmfulness, Stereotyping) scores high when the agent
    misbehaved, and must not make a bad session look good."""
    vals = [
        ac.evaluator_polarity(r.evaluator_id) * r.score
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


_WORD = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])"


def tool_mentions(text: str | None, names: list[str]) -> list[str]:
    """Tool names an evaluator explanation mentions (word-boundary match,
    backticks/quotes tolerated). Best effort — TOOL_CALL records carry no tool
    id, so this is the only link from a judgement to a call."""
    if not text:
        return []
    return [n for n in names if n and re.search(_WORD.format(re.escape(n)), text)]


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
            (t.get("at") or "\uffff", 1, i, t) for i, t in enumerate(tool_turns)
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
                rec = rc.get("recommendation") or ""
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
    workspace: WorkspaceContext,
    log_group: str,
    log_stream: str,
    transcript: TranscriptFn,
    max_sessions: int,
    logs: Any = None,
    progress: Callable[[str], None] | None = None,
    tool_turns: ToolTurnsFn | None = None,
    tool_spans: ToolSpansFn | None = None,
) -> tuple[list[SessionEvidence], EvidenceStats]:
    """``tool_turns`` / ``tool_spans`` are given only when tool evidence is wanted
    (a provider producing tool descriptions). Content-log turns are the primary
    source (arguments + results); spans fill in sessions whose content records
    carry no tool parts and record the description the model actually saw."""
    attrs = read_result_records(
        logs or workspace.client("logs"), log_group, log_stream
    )
    grouped = group_records(attrs)
    stats = EvidenceStats(
        records=len(attrs),
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
