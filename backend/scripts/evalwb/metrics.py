"""M2–M4 against Global ``Evaluate`` (or the run's recorded rows), per evaluator — the Round 0
definitions of plans/DIFF-GLOBAL-EVAL.md (diff-global ``compare.py`` / ``compare2.py``).

A UNIT is what a result scores: ``(evaluator, session, trace)`` for TRACE,
``(evaluator, session, "<trace>:<tool span>")`` for TOOL_CALL, ``(evaluator, session, "")`` for
SESSION. A tool unit is trace-scoped because a span id alone is not unique: a snapshot-restored
microVM draws the same span-id sequence again (N17-obs315 MT2), so two calls in different turns
of one session can carry the same bare span id.

- Our TRACE units are keyed by the turn the judge actually READ. The production engine
  judges one TRACE output per session — the last agent turn (``Evaluator._get_last_turn``) —
  and stamps it with the session's first trace id (N17-obs322). A session with a single
  TRACE record per evaluator is therefore keyed to its last agent trace (diff-global's
  "production behaviour" pairing), and ``stamped_elsewhere`` counts how many of those rows
  carry another trace id. Once records are per turn (obs324), each keeps its own trace id.
- A TOOL_CALL record without a ``span_id`` is matched by ordinal to the session's agent
  (Strands) ``execute_tool`` spans in start order (``tool_spans``: session → trace-scoped
  ``<trace>:<span>`` ids); one with a span id but no trace id takes the session's only
  ``tool_spans`` entry of that span id (``:<span>`` when there is none, or more than one).
- A result with no score and no label (an error, e.g. Global's ``SpanStatusErrorException``
  on a turn whose agent span is ERROR) is not a unit; :func:`error_counts` counts them.

Metrics per evaluator:

- M2 = exact (lower-cased) label match / matched units labelled on both sides
- M3 = mean |Global value − our score| over matched units scored on both sides
- M4 = turns (tool calls, sessions) our engine scored / the agent turns (agent tool spans,
  sessions) there are; ``coverage_vs_reference`` = matched / reference units
- ``turn_mean_gap`` = mean over sessions of |Global's mean over the session's turns − our
  one session score| (what a per-session score misses on multi-turn sessions)
- Global rerun variance = share of units whose label is identical over every rep, and the
  mean per-unit score standard deviation
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any

Unit = tuple[str, str, str]


def tool_unit(rec: dict[str, Any], scoped: list[str]) -> str:
    """``<trace>:<span>`` of a TOOL_CALL record ("" when it names no span); ``scoped`` is the
    session's trace-scoped tool span ids. A provider-scoped ``span_id`` already is one."""
    span = str(rec.get("span_id") or "")
    if not span or ":" in span:
        return span
    if rec.get("trace_id"):
        return f"{rec['trace_id']}:{span}"
    hits = [unit for unit in scoped if unit.rsplit(":", 1)[-1] == span]
    return hits[0] if len(hits) == 1 else f":{span}"


def _label(value: Any) -> str:
    return str(value or "").strip().lower()


def _scored(rec: dict[str, Any]) -> bool:
    return rec.get("score") is not None or rec.get("label") is not None


def units(records: list[dict[str, Any]], tool_spans: dict[str, list[str]] | None = None,
          rep: int | None = None, agent_traces: dict[str, list[str]] | None = None,
          ) -> dict[Unit, dict[str, Any]]:
    """Scored records by unit. ``agent_traces`` (session → agent trace ids in order) turns
    a one-row-per-session TRACE record into a unit of the turn it judged (see above)."""
    chosen = [r for r in records if rep is None or int(r.get("rep") or 0) == rep]
    per_session = Counter((str(r.get("evaluator_id")), str(r.get("session_id"))) for r in chosen
                          if r.get("level") == "TRACE")
    out: dict[Unit, dict[str, Any]] = {}
    ordinals: dict[tuple[str, str], int] = defaultdict(int)
    for rec in chosen:
        evaluator, session = str(rec.get("evaluator_id")), str(rec.get("session_id"))
        level = rec.get("level")
        if level == "TOOL_CALL":
            ordinal = ordinals[(evaluator, session)]
            ordinals[(evaluator, session)] += 1
            spans = (tool_spans or {}).get(session) or []
            unit = tool_unit(rec, spans)
            if not unit:
                unit = spans[ordinal] if ordinal < len(spans) else f"#{ordinal}"
        elif level == "SESSION":
            unit = ""
        else:
            unit = str(rec.get("trace_id") or "")
            traces = (agent_traces or {}).get(session) or []
            if traces and per_session[(evaluator, session)] == 1:
                unit = traces[-1]
                rec = {**rec, "_stamped_elsewhere": str(rec.get("trace_id") or "") != unit}
        if _scored(rec):
            out[(evaluator, session, unit)] = rec
    return out


def error_counts(records: list[dict[str, Any]], rep: int | None = 0
                 ) -> dict[str, dict[str, int]]:
    """``{evaluator: {error code: n}}`` of the unscored results (Global's
    ``SpanStatusErrorException`` is the expected one: an ERROR agent span is not judged)."""
    out: dict[str, Counter] = defaultdict(Counter)
    for rec in records:
        if (rep is None or int(rec.get("rep") or 0) == rep) and not _scored(rec):
            out[str(rec.get("evaluator_id"))][str(rec.get("error_type") or "unknown")] += 1
    return {e: dict(c) for e, c in out.items()}


def _denominator(evaluator: str, level: str | None, sessions: list[str],
                 agent_traces: dict[str, list[str]], tool_spans: dict[str, list[str]]) -> int:
    if level == "TOOL_CALL":
        return sum(len(tool_spans.get(s) or []) for s in sessions)
    if level == "SESSION":
        return len(sessions)
    return sum(len(agent_traces.get(s) or []) for s in sessions)


def compare(ours: dict[Unit, dict[str, Any]], reference: dict[Unit, dict[str, Any]],
            evaluators: list[str] | None = None, *,
            agent_traces: dict[str, list[str]] | None = None,
            tool_spans: dict[str, list[str]] | None = None,
            sessions: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Per evaluator: ``{ours, reference, matched, coverage_vs_reference, labelled,
    label_agree, label_rate, scored, mad, bias, m4_scored, m4_turns, m4, turn_mean_gap,
    stamped_elsewhere}``."""
    evaluators = sorted(evaluators if evaluators is not None
                        else {u[0] for u in ours} | {u[0] for u in reference})
    agent_traces, tool_spans = agent_traces or {}, tool_spans or {}
    sessions = sessions or sorted({u[1] for u in ours} | {u[1] for u in reference})
    out: dict[str, dict[str, Any]] = {}
    for evaluator in evaluators:
        mine = {u: r for u, r in ours.items() if u[0] == evaluator}
        ref = {u: r for u, r in reference.items() if u[0] == evaluator}
        matched = [u for u in ref if u in mine]
        labelled = [u for u in matched if mine[u].get("label") and ref[u].get("label")]
        agree = sum(1 for u in labelled if _label(mine[u]["label"]) == _label(ref[u]["label"]))
        scored = [u for u in matched
                  if mine[u].get("score") is not None and ref[u].get("score") is not None]
        diffs = [float(mine[u]["score"]) - float(ref[u]["score"]) for u in scored]
        level = next((r.get("level") for r in [*mine.values(), *ref.values()]), None)
        turns = _denominator(evaluator, level, sessions, agent_traces, tool_spans)
        gaps = []
        if level == "TRACE":
            for session in sessions:
                own = [r for u, r in mine.items() if u[1] == session and r.get("score") is not None]
                theirs = [float(r["score"]) for u, r in ref.items()
                          if u[1] == session and r.get("score") is not None]
                if len(own) == 1 and theirs:
                    gaps.append(abs(statistics.mean(theirs) - float(own[0]["score"])))
        out[evaluator] = {
            "level": level, "ours": len(mine), "reference": len(ref), "matched": len(matched),
            "coverage_vs_reference": round(len(matched) / len(ref), 4) if ref else None,
            "labelled": len(labelled), "label_agree": agree,
            "label_rate": round(agree / len(labelled), 4) if labelled else None,
            "scored": len(scored),
            "mad": round(sum(abs(d) for d in diffs) / len(diffs), 4) if diffs else None,
            "bias": round(sum(diffs) / len(diffs), 4) if diffs else None,
            "m4_scored": len(mine), "m4_turns": turns,
            "m4": round(min(len(mine), turns) / turns, 4) if turns else None,
            "turn_mean_gap": round(statistics.mean(gaps), 4) if gaps else None,
            "stamped_elsewhere": sum(1 for r in mine.values() if r.get("_stamped_elsewhere")),
        }
    return out


def rerun_variance(global_records: list[dict[str, Any]], reps: int,
                   tool_spans: dict[str, list[str]] | None = None) -> dict[str, dict[str, Any]]:
    """Global against itself over ``reps`` reps: per evaluator ``{units, identical,
    label_rate, mean_std}`` (units = those with a result on every rep)."""
    if reps < 2:
        return {}
    by_rep = [units(global_records, tool_spans, rep=r) for r in range(reps)]
    common = set(by_rep[0]).intersection(*by_rep[1:])
    out: dict[str, dict[str, Any]] = {}
    for evaluator in sorted({u[0] for u in common}):
        mine = [u for u in common if u[0] == evaluator]
        identical = sum(1 for u in mine if len({_label(b[u].get("label")) for b in by_rep}) == 1)
        stds = [statistics.pstdev([float(b[u]["score"]) for b in by_rep]) for u in mine
                if all(b[u].get("score") is not None for b in by_rep)]
        out[evaluator] = {"units": len(mine), "identical": identical,
                          "label_rate": round(identical / len(mine), 4) if mine else None,
                          "mean_std": round(statistics.mean(stds), 4) if stds else None}
    return out
