"""Coded parts of the notes ``draft_plan`` writes on an evaluation plan (N17-obs308).

The plan's ``note`` strings stay English in the stored content (the hash, the architect's
view of the plan and every earlier plan are unchanged). This module splits a note into
``{code, params, text}`` parts at projection time — the same pattern as
``validation_codes`` (N17-obs296) — so the console renders the platform-written
sentences in the member's language and shows anything else verbatim:

* an evaluator entry's ``note``: the seed's own text, ``named in recommendation #N``,
  every ``gate from recommendation #N (source: S): <gate>`` tag ``_apply_intent``
  appended and the ``renamed from '<name>': …`` sentence ``rename_taken_evaluators`` adds when
  the name is taken;
* a recommendation's ``note``: the one sentence ``_recommendation_note`` wrote (gate
  carried onto entries / UNRESOLVED gate / no exact evaluator identified).

The patterns mirror the f-strings of ``evaluation_plan._apply_intent`` /
``_recommendation_note`` / ``Intent.describe`` /
``evaluation_assets.rename_taken_evaluators``; ``tests/test_assistant_plan_notes.py``
builds the real notes through ``draft_plan`` and fails when a sentence drifts.
"""

from __future__ import annotations

import re
from typing import Any

TEXT = "text"
NAMED_IN = "evaluation_plan.note.named_in_recommendation"
GATE_FROM = "evaluation_plan.note.gate_from_recommendation"
GATE_CARRIED = "evaluation_plan.note.gate_carried"
UNRESOLVED_GATE = "evaluation_plan.note.unresolved_gate"
UNCLASSIFIED = "evaluation_plan.note.unclassified"
RENAMED = "evaluation_plan.note.renamed"

_GATE_TAG_RE = re.compile(r"gate from recommendation #(?P<n>\d+) \(source: (?P<source>\w+)\): ")
_RENAMED_RE = re.compile(
    r"(?:^| · )(?P<text>renamed from '(?P<name>[^']*)': that evaluator name is already taken "
    r"in this workspace)$")
_NAMED_RE = re.compile(r"^named in recommendation #(?P<n>\d+)$")
_DESCRIBE_RE = re.compile(
    r"^(?P<blocking>blocking)?"
    r"(?:(?:(?<=blocking) · )?threshold (?P<threshold>[0-9][0-9.eE+-]*))?"
    r"(?:(?:(?<=\S) · )?metric (?P<metric>.+))?$")
_WHY = r"(?: \((?P<why>.+)\))?"
_CARRIED_RE = re.compile(r"^(?P<gate>.+) — source: (?P<source>\w+); carried onto (?P<targets>.+)$")
_UNRESOLVED_RE = re.compile(
    r"^UNRESOLVED gate: (?P<gate>.+) — source: (?P<source>\w+); no evaluator carries it"
    + _WHY + r" — name the evaluator \(evaluator_id: a listed Builtin\.\* id or a seed "
    r"evaluator key\) or decline this recommendation$")
_UNCLASSIFIED_RE = re.compile(r"^no exact evaluator identified" + _WHY + r" — classify or decline$")
_WHY_RES = (
    ("not_listed_or_seed", re.compile(
        r"^evaluator_id '(?P<evaluator_id>.*)' is neither a listed evaluator nor a seed "
        r"evaluator key / name$")),
    ("not_listed", re.compile(r"^evaluator_id '(?P<evaluator_id>.*)' is not a listed evaluator$")),
)


def _gate(text: str) -> dict[str, Any] | None:
    """``Intent.describe()`` read back: ``{blocking, threshold, metric}`` or None."""
    m = _DESCRIBE_RE.match(text)
    if m is None or not any(m.groupdict().values()):
        return None
    threshold: float | None = None
    if m.group("threshold") is not None:
        try:
            threshold = float(m.group("threshold"))
        except ValueError:
            return None
    return {"blocking": m.group("blocking") is not None, "threshold": threshold,
            "metric": m.group("metric")}


def _why(text: str | None) -> dict[str, Any]:
    if not text:
        return {"why_code": None, "evaluator_id": None, "why": None}
    for code, rx in _WHY_RES:
        m = rx.match(text)
        if m:
            return {"why_code": code, "evaluator_id": m.group("evaluator_id"), "why": text}
    return {"why_code": "other", "evaluator_id": None, "why": text}


def _text(text: str) -> dict[str, Any]:
    m = _NAMED_RE.match(text)
    if m:
        return {"code": NAMED_IN, "params": {"index": int(m.group("n"))}, "text": text}
    return {"code": TEXT, "params": {}, "text": text}


def entry_note_parts(note: str) -> list[dict[str, Any]]:
    """An evaluator entry's note → its parts, in order (``"; "`` separators dropped)."""
    parts: list[dict[str, Any]] = []
    renamed = _RENAMED_RE.search(note)
    if renamed:
        note = note[:renamed.start()]
    tags = list(_GATE_TAG_RE.finditer(note))
    head = note[:tags[0].start()] if tags else note
    parts += [_text(piece) for piece in head.split("; ") if piece.strip()]
    for i, tag in enumerate(tags):
        end = tags[i + 1].start() if i + 1 < len(tags) else len(note)
        body = note[tag.end():end]
        if body.endswith("; "):
            body = body[:-2]
        raw = note[tag.start():tag.end()] + body
        gate = _gate(body)
        if gate is None:
            parts.append({"code": TEXT, "params": {}, "text": raw})
            continue
        params = {"index": int(tag.group("n")), "source": tag.group("source"), **gate}
        parts.append({"code": GATE_FROM, "params": params, "text": raw})
    if renamed:
        parts.append({"code": RENAMED, "params": {"name": renamed.group("name")},
                      "text": renamed.group("text")})
    return parts


def recommendation_note_parts(note: str) -> list[dict[str, Any]]:
    """A recommendation's note → one coded part (or the verbatim text)."""
    if not note:
        return []
    m = _CARRIED_RE.match(note)
    if m and (gate := _gate(m.group("gate"))):
        return [{"code": GATE_CARRIED, "text": note, "params": {
            "source": m.group("source"), "targets": m.group("targets").split(", "), **gate}}]
    m = _UNRESOLVED_RE.match(note)
    if m and (gate := _gate(m.group("gate"))):
        return [{"code": UNRESOLVED_GATE, "text": note, "params": {
            "source": m.group("source"), **gate, **_why(m.group("why"))}}]
    m = _UNCLASSIFIED_RE.match(note)
    if m:
        return [{"code": UNCLASSIFIED, "text": note, "params": _why(m.group("why"))}]
    return [{"code": TEXT, "params": {}, "text": note}]


def note_details(content: Any) -> dict[str, Any]:
    """``{evaluators: {key: {message, parts}}, recommendations: {index: {message, parts}}}``
    for every non-empty note of a plan's content; ``message`` is the note verbatim so the
    console can tell a stale detail from the note it describes."""
    out: dict[str, Any] = {"evaluators": {}, "recommendations": {}}
    if not isinstance(content, dict):
        return out
    for e in content.get("evaluators") or []:
        if isinstance(e, dict) and isinstance(e.get("key"), str) \
                and isinstance(e.get("note"), str) and e["note"]:
            out["evaluators"][e["key"]] = {"message": e["note"],
                                           "parts": entry_note_parts(e["note"])}
    for r in content.get("recommendations") or []:
        if isinstance(r, dict) and isinstance(r.get("index"), int) \
                and isinstance(r.get("note"), str) and r["note"]:
            out["recommendations"][str(r["index"])] = {
                "message": r["note"], "parts": recommendation_note_parts(r["note"])}
    return out
