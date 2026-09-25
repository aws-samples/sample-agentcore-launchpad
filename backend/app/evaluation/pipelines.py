"""Observed sessions → evaluation dataset items (console V2 数据中心 / 数据处理).

One extraction path serves both entrances: "add these trajectories to a dataset"
from the trajectory list/detail, and a saved pipeline run that selects sessions by
agent / time window / status. Transcripts come from the observability session
detail (AgentCore Memory or the content logs); nothing here calls a model.

Item shapes follow the dataset contract in `routers._validate_items`: a
*predefined* dataset receives one scenario per session (`scenario_id` + `turns`
of `{input, expected_response}`), a *legacy* one a `{prompt, expected}` pair from
the session's first exchange. Simulated-persona datasets cannot receive observed
traffic (they carry actor profiles, not replies).
"""

from __future__ import annotations

import re
from typing import Any

MAX_DATASET_ITEMS = 200  # mirrors DatasetCreate/DatasetUpdate max_length
MAX_SESSIONS_PER_CALL = 50
MAX_TEXT = 8000  # DatasetCreate caps a prompt at 8000 characters

_SCENARIO_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def scenario_id_for(session_id: str) -> str:
    """Stable, dataset-safe scenario id for an observed session."""
    return "trace-" + _SCENARIO_UNSAFE.sub("-", session_id)[:48]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT]


def exchanges(turns: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(user input, following assistant reply) pairs in transcript order.

    Consecutive user turns merge into one input; an input with no reply yet keeps
    an empty reply so the item still replays the question."""
    pairs: list[tuple[str, str]] = []
    pending: list[str] = []
    for turn in turns:
        role = str(turn.get("role") or "").lower()
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        if role == "user":
            pending.append(text)
        elif role == "assistant" and pending:
            pairs.append(("\n".join(pending), text))
            pending = []
    if pending:
        pairs.append(("\n".join(pending), ""))
    return pairs


def items_from_transcript(
    session_id: str,
    turns: list[dict[str, Any]],
    *,
    kind: str,
    first_turn_only: bool = False,
    min_input_chars: int = 0,
    agent: str | None = None,
) -> list[dict[str, Any]]:
    """Dataset items for one session (empty when nothing usable was said)."""
    pairs = [
        (_clip(q), _clip(a)) for q, a in exchanges(turns) if len(q) >= max(1, min_input_chars)
    ]
    if not pairs:
        return []
    if kind == "legacy":
        prompt, expected = pairs[0]
        item: dict[str, Any] = {"prompt": prompt}
        if expected:
            item["expected"] = expected
        return [item]
    if first_turn_only:
        pairs = pairs[:1]
    scenario_turns = []
    for prompt, expected in pairs:
        turn: dict[str, Any] = {"input": prompt}
        if expected:
            turn["expected_response"] = expected
        scenario_turns.append(turn)
    metadata: dict[str, Any] = {"source": "trace", "session_id": session_id}
    if agent:
        metadata["agent"] = agent
    return [{"scenario_id": scenario_id_for(session_id), "turns": scenario_turns,
             "metadata": metadata}]


def _first_input(item: dict[str, Any]) -> str:
    if "turns" in item:
        turns = item.get("turns") or []
        return str((turns[0] or {}).get("input") or "") if turns else ""
    return str(item.get("prompt") or "")


def merge_items(
    existing: list[dict[str, Any]], new: list[dict[str, Any]], *, dedupe: bool = True
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """`existing` + the `new` items that fit: returns (merged, added, reasons).

    A scenario id already present is always skipped (the validator rejects
    duplicates); with `dedupe`, an item whose first input matches one already in
    the dataset is skipped too. The dataset cap is enforced last."""
    merged = list(existing)
    ids = {str(i.get("scenario_id")) for i in merged if i.get("scenario_id")}
    inputs = {_first_input(i).strip() for i in merged}
    added = 0
    reasons: list[str] = []
    for item in new:
        sid = item.get("scenario_id")
        if sid and str(sid) in ids:
            reasons.append("duplicate")
            continue
        key = _first_input(item).strip()
        if dedupe and key in inputs:
            reasons.append("duplicate")
            continue
        if len(merged) >= MAX_DATASET_ITEMS:
            reasons.append("dataset_full")
            continue
        merged.append(item)
        if sid:
            ids.add(str(sid))
        inputs.add(key)
        added += 1
    return merged, added, reasons


def select_sessions(
    rows: list[dict[str, Any]], *, agent: str | None, status: str, limit: int
) -> list[dict[str, Any]]:
    """Session rows matching a pipeline source filter, newest first."""
    picked = []
    for row in rows:
        if agent and row.get("agent") != agent:
            continue
        errors = int(row.get("errors") or 0)
        if status == "error" and errors == 0:
            continue
        if status == "ok" and errors > 0:
            continue
        picked.append(row)
    picked.sort(key=lambda r: str(r.get("last") or r.get("first") or ""), reverse=True)
    return picked[:limit]
