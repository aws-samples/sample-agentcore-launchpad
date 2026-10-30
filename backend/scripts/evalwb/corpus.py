"""``--corpus``: a designed probe corpus (``results.jsonl`` of the probe generator) as input.

Each line is one session the generator drove through the console: ``session_id``,
``agent_id``, ``start`` / ``end``, ``tags`` (``turns``, ``tools``, ``ending``, ``feature``,
``runtime``, ``model``, …) and ``turns[]`` with the client-side ``prompt``, streamed
``delta_text``, ``tools`` and ``error``. The workbench resolves each agent's runtime group,
fetches the sessions' records, and adds two corpus views to the report:

- M1 per tag dimension and value (:func:`tag_breakdown`);
- the error turns (a turn the client saw end in an ``error`` frame, e.g. a transient model
  ``internalServerException``): whether the production extraction would put them in front
  of a judge (N17-obs373: an errored turn must not be judged) — :func:`error_turns`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from evalwb.reconcile import SessionResult, m1_summary, norm

SKIP_TAGS = {"smoke"}


@dataclass
class CorpusSession:
    session_id: str
    agent_id: str
    agent: str
    start: str
    end: str
    tags: dict[str, Any]
    turns: list[dict[str, Any]] = field(default_factory=list)


def load(path: Path) -> list[CorpusSession]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out.append(CorpusSession(
            session_id=row["session_id"], agent_id=row["agent_id"], agent=row.get("agent", ""),
            start=row["start"], end=row["end"], tags=dict(row.get("tags") or {}),
            turns=list(row.get("turns") or [])))
    return out


def window(sessions: list[CorpusSession]) -> tuple[datetime, datetime]:
    """The fetch window of a group of sessions: first start −10 min … last end +30 min
    (records land at span END; a slow tail can trail the client's end)."""
    starts = [datetime.fromisoformat(s.start) for s in sessions]
    ends = [datetime.fromisoformat(s.end) for s in sessions]
    return min(starts) - timedelta(minutes=10), max(ends) + timedelta(minutes=30)


def tag_breakdown(results: list[SessionResult], sessions: list[CorpusSession]
                  ) -> dict[str, dict[str, dict[str, Any]]]:
    """``{dimension: {value: m1_summary of the sessions with that tag value}}``, plus the
    ``agent`` dimension."""
    by_id = {r.session_id: r for r in results}
    groups: dict[str, dict[str, list[SessionResult]]] = defaultdict(lambda: defaultdict(list))
    for session in sessions:
        result = by_id.get(session.session_id)
        if result is None:
            continue
        groups["agent"][session.agent].append(result)
        for key, value in session.tags.items():
            if key not in SKIP_TAGS:
                groups[key][str(value)].append(result)
    return {dim: {value: m1_summary(rs) for value, rs in sorted(values.items())}
            for dim, values in sorted(groups.items())}


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        detail = error.get("detail") or {}
        return str(detail.get("error_type") or "") + ": " + str(error.get("message") or "")
    return str(error)


def error_turns(results: list[SessionResult], sessions: list[CorpusSession]
                ) -> list[dict[str, Any]]:
    """Every client-side error turn, aligned to its logged turn by order and prompt.

    ``judged`` = the production engine would put the turn in front of a judge: the
    extraction yields an agent invocation for it and the engine does not exclude it
    (``engine_core.failed_turn`` where the code has that rule — what obs373 requires);
    ``in_logs`` = the origin logs have the turn."""
    by_id = {r.session_id: r for r in results}
    out: list[dict[str, Any]] = []
    for session in sessions:
        result = by_id.get(session.session_id)
        rows = [r for r in (result.rows if result else []) if r.status != "EXTRA"]
        cursor = 0
        for index, turn in enumerate(session.turns):
            prompt = norm(turn.get("prompt"))
            match = None
            for j in range(cursor, len(rows)):
                if prompt and (prompt in norm(rows[j].origin_prompt)
                               or prompt in norm(rows[j].extracted_prompt)):
                    match, cursor = rows[j], j + 1
                    break
            if not turn.get("error"):
                continue
            out.append({
                "session_id": session.session_id, "agent": session.agent, "turn": index + 1,
                "error": _error_text(turn["error"])[:200], "in_logs": match is not None,
                "status": match.status if match else None,
                "completed_in_logs": bool(match and match.completed),
                "judged": bool(match and match.extracted_answer is not None
                               and not match.judge_excluded),
                "excluded_by_engine": bool(match and match.judge_excluded),
                "extracted_answer": (match.extracted_answer if match else None),
                "client_text": str(turn.get("delta_text") or "")[:300],
            })
    return out


def error_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    kinds: dict[str, int] = defaultdict(int)
    for row in rows:
        kinds[row["error"].split(":")[0] or "unknown"] += 1
    return {"error_turns": len(rows), "in_logs": sum(r["in_logs"] for r in rows),
            "judged": sum(r["judged"] for r in rows),
            "judged_nonempty": sum(1 for r in rows if r["judged"] and norm(r["extracted_answer"])),
            "kinds": dict(kinds)}
