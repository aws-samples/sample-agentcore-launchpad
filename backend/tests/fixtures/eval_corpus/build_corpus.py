"""Build the real-record accuracy corpus from the research raw records (eval accuracy item 1).

    cd backend && uv run python tests/fixtures/eval_corpus/build_corpus.py \
        /tmp/research-obs315/raw [<eval-wb cache>/sessions]

Input: ``rt-<sid>.json`` (``filter-log-events`` on the agent's runtime log group) and
``spans-<sid>.json`` (``filter-log-events`` on ``aws/spans``) per session, read-only from the
CN account by research-obs315 on 2026-10-23. Output: one ``<sid>.json`` per session plus
``edges.json`` (the transformed edge shapes) beside this script. Trimming, redaction and the
expected-side derivation are documented in README.md; this script is the executable form.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND))

from tests.evaluation import corpus_support as cs  # noqa: E402

OUT = Path(__file__).resolve().parent

# short id → (agent, shape) — the sessions research-obs315 replayed (plans/RESEARCH-N17-obs315.md)
SESSIONS: dict[str, tuple[str, str]] = {
    "257f17b16cbb": ("travel_expense_assistant", "run fb8a7e230b11: preamble → kb_search + get_employee → answer"),
    "7b394e2feeaa": ("travel_expense_assistant", "run fb8a7e230b11: preamble → 2 tools → answer"),
    "7f557e29febe": ("travel_expense_assistant", "run fb8a7e230b11: preamble → kb_search → refusal"),
    "f0469605c5f0": ("travel_expense_assistant", "run fb8a7e230b11: preamble → kb_search → refusal; an earlier attempt died on internalServerException"),
    "2335a9363850": ("travel_expense_assistant", "run fb8a7e230b11: no tool, refusal"),
    "7574fd2ea83d": ("travel_expense_assistant", "run fb8a7e230b11: 5 tools, no text before the tools"),
    "539d13ba067b": ("travel_expense_assistant", "run fb8a7e230b11: tools, no text before the tools"),
    "35f49b824328": ("travel_expense_assistant", "2 turns, tools in both"),
    "a696365333a4": ("energy_agent", "8 turns, mostly text, refusals, one tool turn"),
    "b0c5a4e1918f": ("hr_helpdesk_wb006", "8 turns, tools in 5, refusals in turns 6-7"),
    "cc8cc1baf12e": ("hr_helpdesk_wb007", "5 turns, text / 2 turns with 2 tools / text"),
    "06d7a610023c": ("hr_helpdesk_wb006", "3 turns, a tool call in every turn"),
    "d38bac2b014e": ("drama_content_copilot", "3 turns: file attachment, max_tokens continuation, microVM restore (REAL span-id collisions across turns)"),
    "3cdd38248181": ("paipai_energy_agent", "4 turns: ok / model read timeout after tools / 2x runtime timeout (timed-out turns)"),
    "ec38059c086c": ("memory_console_demo", "2 turns, long-term memory injected"),
    "3ebbac0b072e": ("store_ops_memory_demo", "2 turns, long-term memory injected"),
    "dops12-mem-2": ("hr_helpdesk_wb006", "3 turns, long-term memory injected"),
}
# eval-loop probe sessions (eval-wb cache entries: ``backend/.eval-cache/sessions/<id>.json``,
# fetched read-only 2026-10-23) — full session id → (agent, shape); the fixture is named by
# the id's first 24 characters
CACHE_SESSIONS: dict[str, tuple[str, str]] = {
    "probe-multi-a2a-mt8-9d8c5e4d0b3f427abfa24477fc36d34d": (
        "probe_multiagent", "8 turns calling an A2A peer: the peer's spans share the trace and "
        "session.id under its own service.name (N17-obs380)"),
    "05440314cc974b4fb5f52f362cc4accf22c791095d034e6eb10739e03a451b77": (
        "target_skills_sloppy", "run 756fc78ffdd4 (target-skills-sloppy, K3): skill loads whose "
        "SKILL.md bodies came back as tool results (N17-obs401)"),
    "230112bf094d4790b152efc559f5aa2fde4cc27cd952446099d4e96da027e8eb": (
        "target_skills_sloppy", "run 2814a2fc8655 (target-skills-sloppy, K3): skill loads and a "
        "calculator call, results as tool records (N17-obs401)"),
    "c9f5f7e5e7d647dea62d6f26d90e5f9a4b8c223c6c4a4968a548a075518cb978": (
        "target_skills_good", "run 1891e12a5efc (target-skills-good): AgentSkills' "
        "<available_skills> catalogue in the system message, one skill load (N17-obs399)"),
    "d371e407437945f7a28dfa433f97ec2e1da985c90a3e4eb28d6f19b9bc46e3a0": (
        "target_skills", "run 772ba33ffb6b: 3 turns, the same skill loaded in turn 1 and "
        "again in turn 3 (N17-obs402 / obs342)"),
    "probe-glm-mt3-e12df09adf2240afa51770cf5b9c137c": (
        "probe_stub_glm", "3 turns, GLM max_tokens: turn 3 wrote 10 calculator toolUse blocks "
        "it never executed, agent span ERROR (N17-obs381/obs373)"),
}
EDGES = [  # (name, base session, transform, why)
    ("257f17b16cbb-ends_after_tool", "257f17b16cbb", "ends_after_tool",
     "the turn dies after its tool calls: the user saw the preamble, never the HR record (G1)"),
    ("257f17b16cbb-empty_final", "257f17b16cbb", "empty_final",
     "reasoning-only end: the answer is empty, never the preamble (G2)"),
    ("35f49b824328-collide", "35f49b824328", "collide",
     "every turn-2 span id repeats a turn-1 id (snapshot-restored microVM, MT2)"),
]

GEN_AI_SPANS = ("invoke_agent", "chat", "execute_event_loop_cycle", "execute_tool")
SPAN_KEYS = ("traceId", "spanId", "parentSpanId", "name", "kind", "startTimeUnixNano",
             "endTimeUnixNano", "scope")
SPAN_ATTRIBUTES = ("session.id", "gen_ai.operation.name", "gen_ai.system", "gen_ai.request.model",
                   "gen_ai.agent.name", "gen_ai.tool.name", "gen_ai.tool.call.id",
                   "gen_ai.tool.status", "gen_ai.response.finish_reasons", "event_loop.cycle_id",
                   "event_loop.parent_cycle_id", "error.type")
CONTENT_KEYS = ("traceId", "spanId", "timeUnixNano", "observedTimeUnixNano", "eventName",
                "severityNumber", "scope", "body")
SYSTEM_PROMPT_CHARS = 300
ACCOUNT_ID = "123456789012"

REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"arn:aws(?:-cn)?:[A-Za-z0-9\-]*:[A-Za-z0-9\-]*:\d{0,12}:[^\s\"'\\,)]*"),
     "arn:aws-cn:REDACTED"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "AKIAREDACTED0000000"),
    (re.compile(re.escape(ACCOUNT_ID)), "000000000000"),
    (re.compile(r"\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b"), "192.0.2.1"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "user@example.com"),
    (re.compile(r"(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])"), "13800000000"),
    (re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])"), "110101199001010000"),
)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in REDACTIONS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def _trim_system(body: dict[str, Any]) -> None:
    """System prompts are the bulk of every record — kept to their first characters. The
    ``<available_skills>`` block (AgentSkills' offered catalogue, N17-obs399: read into the
    Session's system prompt) is kept whole, block by block, the other text blocks cut."""
    for msg in (body.get("input") or {}).get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = _cut(content, SYSTEM_PROMPT_CHARS)
            elif isinstance(content, dict) and isinstance(content.get("content"), str):
                raw = content["content"]
                content["content"] = (_keep_skills_block(raw) if "<available_skills>" in raw
                                      else _cut(raw, SYSTEM_PROMPT_CHARS))


def _keep_skills_block(raw: str) -> str:
    try:
        parts = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(parts, list):
        return raw
    for part in parts:
        if (isinstance(part, dict) and isinstance(part.get("text"), str)
                and "<available_skills>" not in part["text"]):
            part["text"] = _cut(part["text"], SYSTEM_PROMPT_CHARS)
    return json.dumps(parts, ensure_ascii=False)


HISTORY_TEXT_CHARS = 200
TOOL_RESULT_TEXT_CHARS = 1500
REASONING_TEXT_CHARS = 100


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …[trimmed]"


def _trim_blocks(raw: Any, limit: int | None) -> Any:
    """Cut every ``text`` (and every ``toolResult`` text) of a JSON part list to ``limit``
    characters (``None`` = keep) and every ``reasoningContent`` text to
    ``REASONING_TEXT_CHARS`` — no extractor reads reasoning; a non-list string is cut as a
    whole."""
    if not isinstance(raw, str):
        return raw
    try:
        parts = json.loads(raw)
    except ValueError:
        return raw if limit is None else _cut(raw, limit)
    if not isinstance(parts, list):
        return raw
    for part in parts:
        if not isinstance(part, dict):
            continue
        reasoning = (part.get("reasoningContent") or {}).get("reasoningText")
        if isinstance(reasoning, dict) and isinstance(reasoning.get("text"), str):
            reasoning["text"] = _cut(reasoning["text"], REASONING_TEXT_CHARS)
        if limit is None:
            continue
        if isinstance(part.get("text"), str):
            part["text"] = _cut(part["text"], limit)
        result = part.get("toolResult")
        if isinstance(result, dict):
            for item in result.get("content") or []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    item["text"] = _cut(item["text"], limit)
    return json.dumps(parts, ensure_ascii=False)


def _trim_replays(body: dict[str, Any]) -> None:
    """Replayed history (an output assistant message keyed ``content``) is never an answer
    and repeats every earlier turn in every record: its text blocks are cut, its toolUse
    blocks kept. Tool results (input ``tool`` messages) are cut longer — the corpus test
    checks no answer carries tool-result text."""
    for msg in (body.get("output") or {}).get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if msg.get("role") == "assistant" and isinstance(content, dict) and "content" in content:
            content["content"] = _trim_blocks(content["content"], HISTORY_TEXT_CHARS)
        elif isinstance(content, dict) and "message" in content:
            content["message"] = _trim_blocks(content["message"], None)
    for msg in (body.get("input") or {}).get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if msg.get("role") == "tool" and isinstance(content, dict):
            content["content"] = _trim_blocks(content.get("content"), TOOL_RESULT_TEXT_CHARS)


def _events(path: Path) -> list[tuple[int, dict[str, Any]]]:
    out = []
    for event in json.loads(path.read_text(encoding="utf-8"))["events"]:
        try:
            rec = json.loads(event["message"])
        except ValueError:
            continue
        if isinstance(rec, dict) and (rec.get("attributes") or {}).get("session.id"):
            out.append((int(event["timestamp"]), rec))
    return out


def _cache_events(path: Path) -> tuple[list[tuple[int, dict[str, Any]]],
                                        list[tuple[int, dict[str, Any]]]]:
    """An eval-wb cache entry's records as ``(runtime, spans)`` event lists."""
    entry = json.loads(path.read_text(encoding="utf-8"))
    runtime, spans = [], []
    for item in entry["records"]:
        rec = item["record"]
        if not (rec.get("attributes") or {}).get("session.id"):
            continue
        (spans if item["group"] == "aws/spans" else runtime).append(
            (int(item["timestamp"]), rec))
    return runtime, spans


def build(raw: Path, sid: str, events: tuple[list[Any], list[Any]] | None = None,
          meta: tuple[str, str] | None = None, source: str | None = None) -> dict[str, Any]:
    runtime, spans = events or (_events(raw / f"rt-{sid}.json"),
                                _events(raw / f"spans-{sid}.json"))
    session_id = runtime[0][1]["attributes"]["session.id"]
    group = runtime[0][1].get("resource", {}).get("attributes", {}).get("aws.log.group.names")
    if not group:
        group = next(r["resource"]["attributes"]["aws.log.group.names"] for _, r in spans
                     if "aws.log.group.names" in r.get("resource", {}).get("attributes", {}))
    # span ids that repeat across traces (restored microVM): every such span is kept, so the
    # real collision survives the trimming
    traces_of: dict[str, set[str]] = {}
    for _, rec in spans:
        traces_of.setdefault(rec["spanId"], set()).add(rec["traceId"])
    colliding = {sid_ for sid_, traces in traces_of.items() if len(traces) > 1}
    records: list[dict[str, Any]] = []
    dropped = {"runtime_non_content": 0, "spans_non_gen_ai": 0}
    for ts, rec in runtime:
        if not isinstance(rec.get("body"), dict):
            dropped["runtime_non_content"] += 1
            continue
        kept = {k: rec[k] for k in CONTENT_KEYS if k in rec}
        kept["attributes"] = {k: rec["attributes"][k] for k in ("session.id", "event.name")
                              if k in rec["attributes"]}
        _trim_system(kept["body"])
        _trim_replays(kept["body"])
        records.append({"group": "runtime", "timestamp": ts, "record": kept})
    for ts, rec in spans:
        if not (str(rec.get("name", "")).startswith(GEN_AI_SPANS) or rec["spanId"] in colliding):
            dropped["spans_non_gen_ai"] += 1
            continue
        kept = {k: rec[k] for k in SPAN_KEYS if k in rec}
        kept["attributes"] = {k: rec["attributes"][k] for k in SPAN_ATTRIBUTES
                              if k in rec.get("attributes", {})}
        status = rec.get("status") or {}
        kept["status"] = {k: status[k] for k in ("code", "message") if k in status}
        service = ((rec.get("resource") or {}).get("attributes") or {}).get("service.name")
        if service:  # N17-obs380: whose agent wrote the span (an A2A peer shares the trace)
            kept["resource"] = {"attributes": {"service.name": service}}
        records.append({"group": "aws/spans", "timestamp": ts, "record": kept})
    ids = [(x["record"].get("traceId"), x["record"].get("spanId"),
            x["record"]["attributes"].get("session.id")) for x in records]
    records = redact(sorted(records, key=lambda x: (x["timestamp"], x["group"])))
    # redaction must never touch an identifier the provider joins on
    assert sorted(ids) == sorted((x["record"].get("traceId"), x["record"].get("spanId"),
                                  x["record"]["attributes"].get("session.id"))
                                 for x in records), f"{sid}: redaction changed an id"
    turns, deviations = cs.expected_turns(records, session_id)
    for turn in turns:  # the expected calls ARE the executions the spans recorded
        assert turn["tools"] == cs.executed_tools(records, turn["trace_id"]), (sid, turn)
    agent, shape = meta or SESSIONS[sid]
    return {
        "session_id": session_id, "log_group": redact(group), "agent": agent, "shape": shape,
        "source": source or f"research-obs315 raw records rt-{sid}.json + spans-{sid}.json "
                  "(CN account, read-only, 2026-10-23); redacted + trimmed (README.md)",
        "trimmed": {**dropped, "system_prompt_chars": SYSTEM_PROMPT_CHARS,
                    "history_text_chars": HISTORY_TEXT_CHARS,
                    "tool_result_text_chars": TOOL_RESULT_TEXT_CHARS,
                    "reasoning_text_chars": REASONING_TEXT_CHARS,
                    "span_events": "dropped (exception stack traces)"},
        "colliding_span_ids": len(colliding),
        "expected": {"turns": turns, "console_deviations": deviations},
        "records": records,
    }


def main(raw: Path, cache: Path | None = None) -> None:
    built: dict[str, dict[str, Any]] = {}
    jobs: list[tuple[str, dict[str, Any]]] = [(sid, {}) for sid in SESSIONS]
    if cache is not None:
        jobs += [(full[:24], {"events": _cache_events(cache / f"{full}.json"), "meta": meta,
                              "source": f"eval-wb cache entry sessions/{full}.json (CN account, "
                                        "read-only, 2026-10-23); redacted + trimmed (README.md)"})
                 for full, meta in CACHE_SESSIONS.items()]
    for sid, extra in jobs:
        corpus = build(raw, sid, **extra)
        built[sid] = corpus
        (OUT / f"{sid}.json").write_text(
            json.dumps(corpus, ensure_ascii=False, indent=None, separators=(",", ":")) + "\n",
            encoding="utf-8")
        print(f"{sid}: {len(corpus['records'])} records, {len(corpus['expected']['turns'])} "
              f"turns, deviations={len(corpus['expected']['console_deviations'])}")
    edges = []
    for name, base, transform, why in EDGES:
        records = cs.TRANSFORMS[transform](built[base]["records"])
        turns, deviations = cs.expected_turns(records, built[base]["session_id"])
        edges.append({"name": name, "base": base, "transform": transform, "why": why,
                      "expected": {"turns": turns, "console_deviations": deviations}})
    (OUT / "edges.json").write_text(json.dumps(edges, ensure_ascii=False, indent=1) + "\n",
                                    encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/research-obs315/raw"),
         Path(sys.argv[2]) if len(sys.argv) > 2 else None)
