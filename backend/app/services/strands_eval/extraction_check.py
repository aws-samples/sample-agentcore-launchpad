"""Extraction reconciliation — a self-check on every evaluation (eval accuracy item 1).

N17-obs315 scored the pre-tool preamble (and, multi-turn, the previous turn's answer) while
every test was green: nothing compared what the judges were given with what the agent had
actually answered. Every evaluated session is now checked: each turn's extracted
``agent_response`` (what the TRACE / SESSION judges and ``actual_output`` read) is compared
with an independent REFERENCE of that turn's answer —

- ``replay``: the answer the worker itself collected while replaying the turn
  (``runtime_invoke.invoke_text`` → ``_collect``) — dataset / batch runs, canary traffic;
- ``chat_ledger``: the platform chat ledger's answer bubbles (``chat_messages`` role
  ``agent``, the last one of each user turn) — session-scope runs, SCORE NOW, online;
- ``runtime_logs`` (N17-obs316): with neither, the session's own runtime content records —
  per trace, its own ``end_turn`` answer (:func:`origin_answers`, the workbench's oracle and
  the console transcript's rule); a trace with no ``end_turn`` (a failed turn) is not
  compared;
- none of them (no content records at all): the check is ``unverifiable``.

Equality is on :func:`normalise`\\ d text (the documented rule there), shipped as a short
digest so a job spec carries no transcript. The result per session::

    {status: ok | mismatch | unverifiable, source, turns_checked,
     mismatches: [{turn, expected_head, extracted_head}], mismatch_count,
     anomalies: {turns, empty, tool_json_like, repeated_previous}}

The anomaly counters need no reference: an empty answer, an answer that parses as a JSON
object / array (a tool result or a content-block list scored as the answer), an answer
identical to the previous turn's (the multi-turn leak). Pure library: shipped into the
evaluation worker byte-for-byte like the rest of ``strands_eval``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

STATUS_OK, STATUS_MISMATCH, STATUS_UNVERIFIABLE = "ok", "mismatch", "unverifiable"
SOURCE_REPLAY, SOURCE_CHAT_LEDGER = "replay", "chat_ledger"
SOURCE_RUNTIME_LOGS = "runtime_logs"  # the session's own runtime content records (obs316)
HEAD_CHARS = 80
MISMATCHES_MAX = 5
MISMATCHED_SESSIONS_MAX = 50
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
# whitespace and the markdown markers a renderer / a block join may add or drop
_IGNORED = re.compile(r"[\s*_`#>~]+")


def normalise(text: Any) -> str:
    """The comparison form of an answer: NFKC (full-width ASCII → ASCII), zero-width
    characters dropped, every whitespace run and every markdown marker (``* _ ` # > ~``)
    removed. Two answers are the same answer when their normalised forms are equal — so a
    block joined with a newline instead of a space, a trailing newline, or ``**bold**``
    against ``bold`` all match, while any word, digit or punctuation difference does not."""
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text))
    return _IGNORED.sub("", _ZERO_WIDTH.sub("", value))


def head(text: Any) -> str:
    """The first ``HEAD_CHARS`` characters, whitespace collapsed (display only)."""
    return " ".join(str(text or "").split())[:HEAD_CHARS]


def digest(text: Any) -> dict[str, str]:
    """A reference turn as the spec carries it: ``{sha, head}`` (16 hex of sha256 over the
    normalised text, the display head)."""
    return {"sha": hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()[:16],
            "head": head(text)}


def reference(source: str, answers: list[Any]) -> dict[str, Any]:
    """``{source, turns: [digest | None]}`` — ``None`` marks a turn that cannot be compared
    (a ledger answer the runtime cut at its token limit carries the platform's notice)."""
    return {"source": source,
            "turns": [None if a is None else digest(a) for a in answers]}


def extracted_turns(task_output: Any) -> list[str]:
    """Each turn's extracted answer, in session order: the ``agent_response`` of every
    trace's agent-invocation span (a trace without one — no user prompt — is no turn)."""
    out: list[str] = []
    try:
        traces = task_output["trajectory"].traces
    except Exception:  # noqa: BLE001 — a malformed session has no turns to check
        return out
    for trace in traces:
        for span in getattr(trace, "spans", []) or []:
            if hasattr(span, "agent_response") and hasattr(span, "user_prompt"):
                out.append(str(span.agent_response or ""))
                break
    return out


def _tool_json_like(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        return isinstance(json.loads(stripped), (dict, list))
    except ValueError:
        return False


def anomalies(answers: list[str]) -> dict[str, int]:
    """``{turns, empty, tool_json_like, repeated_previous}`` over one session's answers."""
    norms = [normalise(a) for a in answers]
    return {
        "turns": len(answers),
        "empty": sum(1 for n in norms if not n),
        "tool_json_like": sum(1 for a in answers if _tool_json_like(a)),
        "repeated_previous": sum(1 for i in range(1, len(norms))
                                 if norms[i] and norms[i] == norms[i - 1]),
    }


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        try:
            parts = json.loads(message)
        except ValueError:
            return message
        if isinstance(parts, list):
            return "\n".join(str(p["text"]) for p in parts
                             if isinstance(p, dict) and isinstance(p.get("text"), str))
        return message
    return ""


def origin_answers(records: list[dict[str, Any]]) -> list[str | None]:
    """Per turn — each trace whose content records carry a user prompt, in the order of its
    first record — the trace's own LAST ``end_turn`` answer (an output assistant message
    keyed ``message`` with ``finish_reason`` ``end_turn``; never replayed history, keyed
    ``content``, nor a tool result, with an ``id``); ``None`` for a trace with none (a failed
    turn: nothing to compare). ``records`` are raw CloudWatch content records."""
    traces: dict[str, dict[str, Any]] = {}
    content = [r for r in records if isinstance(r, dict) and isinstance(r.get("body"), dict)]
    for rec in sorted(content, key=lambda r: int(r.get("timeUnixNano") or 0)):
        trace = traces.setdefault(str(rec.get("traceId") or ""), {"user": False, "final": None})
        body = rec["body"]
        for msg in (body.get("input") or {}).get("messages") or []:
            if isinstance(msg, dict) and msg.get("role") == "user":
                trace["user"] = True
        for msg in (body.get("output") or {}).get("messages") or []:
            value = msg.get("content") if isinstance(msg, dict) else None
            if (msg.get("role") == "assistant" and isinstance(value, dict)
                    and "message" in value and "id" not in value
                    and value.get("finish_reason") == "end_turn"):
                trace["final"] = _message_text(value.get("message"))
    return [t["final"] for t in traces.values() if t["user"]]


def origin_reference(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The ``runtime_logs`` reference of a session's raw records; ``None`` without a turn."""
    answers = origin_answers(records)
    return reference(SOURCE_RUNTIME_LOGS, answers) if answers else None


def check(answers: list[str], ref: dict[str, Any] | None) -> dict[str, Any]:
    """One session's ``extraction_check``. Turns pair by position over the longer side: a
    turn missing on either side is a mismatch (a replay retried in the same session, a
    turn the spans never recorded); a ``None`` reference turn is skipped. No reference →
    ``unverifiable``; nothing comparable → ``unverifiable``."""
    result: dict[str, Any] = {"status": STATUS_UNVERIFIABLE, "source": None,
                              "turns_checked": 0, "mismatches": [], "mismatch_count": 0,
                              "anomalies": anomalies(answers)}
    if not ref or not isinstance(ref.get("turns"), list):
        return result
    turns = list(ref["turns"])
    result["source"] = ref.get("source")
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for index in range(max(len(turns), len(answers))):
        expected = turns[index] if index < len(turns) else {"sha": None, "head": "(missing)"}
        if expected is None:
            continue
        checked += 1
        if index >= len(answers):
            mismatches.append({"turn": index + 1, "expected_head": expected.get("head", ""),
                               "extracted_head": "(missing)"})
            continue
        if digest(answers[index])["sha"] != expected.get("sha"):
            mismatches.append({"turn": index + 1, "expected_head": expected.get("head", ""),
                               "extracted_head": head(answers[index])})
    result["turns_checked"] = checked
    result["mismatch_count"] = len(mismatches)
    result["mismatches"] = mismatches[:MISMATCHES_MAX]
    if mismatches:
        result["status"] = STATUS_MISMATCH
    elif checked:
        result["status"] = STATUS_OK
    return result


def turn_statuses(answers: list[str], ref: dict[str, Any] | None) -> list[str]:
    """Each extracted turn's own reconciliation status (N17-obs324: a per-turn row carries
    its turn's): ``ok`` / ``mismatch`` against the reference turn at that position,
    ``unverifiable`` without one (no reference, a ``None`` reference turn, or past its end)."""
    turns = list((ref or {}).get("turns") or []) if isinstance((ref or {}).get("turns"),
                                                               list) else []
    out: list[str] = []
    for index, answer in enumerate(answers):
        expected = turns[index] if index < len(turns) else None
        if expected is None:
            out.append(STATUS_UNVERIFIABLE)
        else:
            out.append(STATUS_OK if digest(answer)["sha"] == expected.get("sha")
                       else STATUS_MISMATCH)
    return out


def session_check(task_output: Any, ref: dict[str, Any] | None) -> dict[str, Any]:
    return check(extracted_turns(task_output), ref)


def summarise(checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The run-level summary over ``{session_id: extraction_check}``: session counts per
    status, turns checked, the mismatching sessions (bounded) and the anomaly counters with
    the empty-answer rate. ``status`` is ``mismatch`` when any session mismatched, ``ok``
    when at least one verified and none mismatched, else ``unverifiable``."""
    counts = {STATUS_OK: 0, STATUS_MISMATCH: 0, STATUS_UNVERIFIABLE: 0}
    totals = {"turns": 0, "empty": 0, "tool_json_like": 0, "repeated_previous": 0}
    turns_checked = 0
    mismatched: list[str] = []
    for session_id, item in checks.items():
        status = str(item.get("status") or STATUS_UNVERIFIABLE)
        counts[status if status in counts else STATUS_UNVERIFIABLE] += 1
        turns_checked += int(item.get("turns_checked") or 0)
        if status == STATUS_MISMATCH:
            mismatched.append(str(session_id))
        for key in totals:
            totals[key] += int((item.get("anomalies") or {}).get(key) or 0)
    if counts[STATUS_MISMATCH]:
        status = STATUS_MISMATCH
    elif counts[STATUS_OK]:
        status = STATUS_OK
    else:
        status = STATUS_UNVERIFIABLE
    return {
        "status": status, "sessions": len(checks), **counts, "turns_checked": turns_checked,
        "mismatched_sessions": mismatched[:MISMATCHED_SESSIONS_MAX],
        "anomalies": {**totals, "empty_rate": round(totals["empty"] / totals["turns"], 4)
                      if totals["turns"] else 0.0},
    }
