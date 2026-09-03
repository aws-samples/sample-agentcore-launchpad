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
from collections.abc import Callable
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

TranscriptFn = Callable[[str], list[dict[str, str]]]


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
            {"role": str(t.get("role") or ""), "text": str(t.get("text") or "")}
            for t in (result.get("turns") or [])
            if isinstance(t, dict)
        ]

    return _fn


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
) -> tuple[list[SessionEvidence], EvidenceStats]:
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
    evidence: list[SessionEvidence] = []
    for i, sid in enumerate(chosen, 1):
        if progress and (i == 1 or i % 5 == 0):
            progress(f"reading transcripts {i}/{len(chosen)}…")
        try:
            turns = transcript(sid)
        except Exception:
            turns = []  # one unreadable session never fails the evidence step
        turns = [
            {"role": t["role"], "text": truncate_text(t["text"])}
            for t in turns
            if t.get("text", "").strip()
        ]
        if turns:
            stats.sessions_with_transcript += 1
        evidence.append(
            SessionEvidence(
                session_id=sid,
                turns=turns,
                records=grouped[sid],
                mean_score=mean_polarized(grouped[sid]),
            )
        )
    return evidence, stats
