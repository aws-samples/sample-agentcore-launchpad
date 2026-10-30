"""Real-record accuracy corpus (eval accuracy item 1) — shared by the corpus test and
``tests/fixtures/eval_corpus/build_corpus.py``.

A corpus file is one real session's redacted CloudWatch records,
``{"group": "runtime" | "aws/spans", "timestamp": <event ms>, "record": {...}}``, plus the
EXPECTED per-turn transcript (``user_prompt``, ``answer``, ``tools``) derived from the runtime
content records (``expected_turns`` below, the derivation the README documents). The test
replays the records through the production ``LaunchpadSpansProvider`` — never through the
derivation — so the two sides stay independent.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from app.services import observability
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval_corpus"
MODES = ("both", "runtime_only", "unified")


def corpus_files() -> list[Path]:
    return sorted(p for p in CORPUS_DIR.glob("*.json") if p.name != "edges.json")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_session(name: str) -> dict[str, Any]:
    return load(CORPUS_DIR / f"{name}.json")


class GroupLogs:
    """Logs Insights / filter_log_events double over corpus records: the content query
    returns the runtime group's records (plus ``aws/spans`` when that group is queried) in
    event-time order, like ``sort @timestamp asc``; the hierarchy query answers from the span
    records; ``filter_log_events`` (the console's reader) returns the runtime group only."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records, self.queries = records, {}

    def start_query(self, logGroupNames, startTime, endTime, queryString):  # noqa: N803
        qid = str(len(self.queries))
        self.queries[qid] = (list(logGroupNames), queryString)
        return {"queryId": qid}

    def get_query_results(self, queryId):  # noqa: N803
        groups, query = self.queries[queryId]
        spans = [x["record"] for x in self.records if x["group"] == SPANS_LOG_GROUP]
        if query.startswith("fields spanId"):
            rows = [[{"field": "spanId", "value": r["spanId"]},
                     {"field": "parentSpanId", "value": r.get("parentSpanId", "")},
                     {"field": "traceId", "value": r["traceId"]}]
                    for r in spans]
        else:
            pool = [x for x in self.records
                    if x["group"] == "runtime" or SPANS_LOG_GROUP in groups]
            rows = [[{"field": "@message", "value": json.dumps(x["record"], ensure_ascii=False)}]
                    for x in sorted(pool, key=lambda x: x["timestamp"])]
        return {"status": "Complete", "results": rows}

    def filter_log_events(self, **_kwargs: Any) -> dict[str, Any]:
        return {"events": [{"message": json.dumps(x["record"], ensure_ascii=False)}
                           for x in sorted(self.records, key=lambda x: x["timestamp"])
                           if x["group"] == "runtime"]}


# ── record transforms (the edge shapes and the unified delivery mode) ─────────────

_EVENT_NAMES = {"system": "gen_ai.system.message", "user": "gen_ai.user.message",
                "tool": "gen_ai.tool.message"}


def to_unified(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unified telemetry, converted from the split records: no runtime-group body records —
    each body message rides inline on its span record as the gen_ai event ADOT built it from
    (``gen_ai.choice`` for an own answer / tool result — content keyed ``message`` —,
    ``gen_ai.assistant.message`` for replayed history, the role's message event otherwise)."""
    bodies: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for x in records:
        if x["group"] == "runtime" and isinstance(x["record"].get("body"), dict):
            rec = x["record"]
            bodies.setdefault((rec["traceId"], rec["spanId"]), []).append(rec)
    out: list[dict[str, Any]] = []
    for x in records:
        if x["group"] != SPANS_LOG_GROUP:
            continue
        rec = copy.deepcopy(x["record"])
        events = []
        for body_rec in bodies.get((rec["traceId"], rec["spanId"]), []):
            for side in ("input", "output"):
                for msg in (body_rec["body"].get(side) or {}).get("messages") or []:
                    content = msg.get("content")
                    attrs = dict(content) if isinstance(content, dict) else {"content": content}
                    role = msg.get("role")
                    if role == "assistant":
                        name = "gen_ai.choice" if "message" in attrs else "gen_ai.assistant.message"
                    else:
                        name = _EVENT_NAMES.get(str(role), "gen_ai.user.message")
                    events.append({"timeUnixNano": body_rec["timeUnixNano"], "name": name,
                                   "attributes": attrs})
        if events:
            rec["events"] = events
        out.append({**x, "record": rec})
    return out


def _span_ids(records: list[dict[str, Any]], name_prefix: str, trace: str | None = None,
              ) -> list[str]:
    return [x["record"]["spanId"] for x in records if x["group"] == SPANS_LOG_GROUP
            and str(x["record"].get("name", "")).startswith(name_prefix)
            and (trace is None or x["record"]["traceId"] == trace)]


def _last_by_start(records: list[dict[str, Any]], name_prefix: str) -> str:
    """The last-starting Strands span of that name (the botocore ``chat <model>`` client
    span under each Strands ``chat`` carries no body and is not a model cycle of its own)."""
    spans = [x["record"] for x in records if x["group"] == SPANS_LOG_GROUP
             and str(x["record"].get("name", "")).startswith(name_prefix)
             and (x["record"].get("scope") or {}).get("name") == "strands.telemetry.tracer"]
    return max(spans, key=lambda r: int(r["startTimeUnixNano"]))["spanId"]


def ends_after_tool(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The turn dies after its tool calls (max cycles / timeout / tool exception): the last
    model call and its cycle never happen, the agent span records no output."""
    final_chat = _last_by_start(records, "chat")
    final_cycle = _last_by_start(records, "execute_event_loop_cycle")
    agents = set(_span_ids(records, "invoke_agent"))
    out = [copy.deepcopy(x) for x in records
           if x["record"].get("spanId") not in (final_chat, final_cycle)]
    for x in out:
        if x["group"] == "runtime" and x["record"].get("spanId") in agents:
            x["record"]["body"].pop("output", None)
    return out


def empty_final(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The final ``end_turn`` choice carries no text block (a reasoning-only end):
    ``str(AgentResult) == ""``."""
    final_chat = _last_by_start(records, "chat")
    agents = set(_span_ids(records, "invoke_agent"))
    out = copy.deepcopy(records)
    for x in out:
        rec = x["record"]
        if x["group"] != "runtime" or "body" not in rec:
            continue
        if rec["spanId"] == final_chat:
            final = rec["body"]["output"]["messages"][-1]["content"]
            final["message"] = json.dumps([b for b in json.loads(final["message"])
                                           if "text" not in b], ensure_ascii=False)
        if rec["spanId"] in agents:
            rec["body"]["output"]["messages"][-1]["content"]["message"] = ""
    return out


def collide(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A snapshot-restored microVM: turn 2 draws turn 1's span-id sequence again (every
    span id of trace 2 that has a turn-1 counterpart is replaced by it)."""
    spans = [x["record"] for x in records if x["group"] == SPANS_LOG_GROUP]
    traces = sorted({r["traceId"] for r in spans},
                    key=lambda t: min(int(r["startTimeUnixNano"]) for r in spans
                                      if r["traceId"] == t))
    seq = {t: [r["spanId"] for r in sorted((r for r in spans if r["traceId"] == t),
                                            key=lambda r: int(r["startTimeUnixNano"]))]
           for t in traces}
    remap = dict(zip(seq[traces[1]], seq[traces[0]], strict=False))
    out = copy.deepcopy(records)
    for x in out:
        rec = x["record"]
        if rec.get("traceId") == traces[1]:
            rec["spanId"] = remap.get(rec["spanId"], rec["spanId"])
            if rec.get("parentSpanId") in remap:
                rec["parentSpanId"] = remap[rec["parentSpanId"]]
    return out


TRANSFORMS = {"ends_after_tool": ends_after_tool, "empty_final": empty_final,
              "collide": collide}


# ── replay through production, and the expected-side derivation ──────────────────

def provider_data(corpus: dict[str, Any], records: list[dict[str, Any]], mode: str) -> Any:
    """``LaunchpadSpansProvider.get_evaluation_data`` over ``records`` in ``mode``:
    ``both`` (runtime group + ``aws/spans``, what jobs pass), ``runtime_only``
    (``spans_source=runtime_logs``) or ``unified`` (inline-event span records only)."""
    group = corpus["log_group"]
    if mode == "unified":
        records = to_unified(records)
    if mode == "runtime_only":
        provider = LaunchpadSpansProvider(GroupLogs(records), [group],
                                          spans_source="runtime_logs")
    else:
        provider = LaunchpadSpansProvider(GroupLogs(records), [group, SPANS_LOG_GROUP])
    return provider.get_evaluation_data(corpus["session_id"])


def _own_answer(content: Any) -> str | None:
    """An own answer's text (content keyed ``message``, not a tool result) — else None."""
    if not isinstance(content, dict) or "message" not in content or "id" in content:
        return None
    return observability._part_list_text(content.get("message")) or ""


def expected_turns(records: list[dict[str, Any]], session_id: str,
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(turns, console_deviations)`` from the RUNTIME content records only — the records
    the console transcript reads (``observability.eval_turns_from_content_logs`` /
    ``eval_tool_turns_from_content_logs``). One turn per trace that carries a user prompt,
    in time order:

    - ``user_prompt``: the trace's FIRST record's user message — the prompt that opened the
      turn (the console shows the LAST one, which differs only for a max_tokens
      continuation whose follow-up prompt the platform wrote itself);
    - ``answer``: the trace's last ``end_turn`` own answer (an empty one stays ``""``); else
      its last non-empty own answer of any finish (the text the user saw); else ``""``.
      Never replayed history (``{"content": …}``) nor a tool result (content with an
      ``id``) — the console's fallback reads those (N17-obs315 MT1/G1);
    - ``tools``: the ``toolUse`` names of the trace's OWN finish_reason-tagged choices (the
      calls the model made in this turn) that RAN — an ``execute_tool`` record
      (``{"message", "id"}``) of that id exists in the trace (a toolUse written into a
      max_tokens stop and never executed is no call, N17-obs381) — in time order, not
      replayed history; the builder cross-checks them against the trace's own
      ``execute_tool`` spans.

    ``console_deviations`` lists every turn where this differs from what the console
    transcript shows, with the reason; the console's 4000-char cut is not a deviation."""
    logs = GroupLogs(records)
    content = [x["record"] for x in sorted(records, key=lambda x: x["timestamp"])
               if x["group"] == "runtime" and isinstance(x["record"].get("body"), dict)]
    by_trace: dict[str, dict[str, Any]] = {}
    for rec in sorted(content, key=lambda r: r.get("timeUnixNano") or 0):
        inv = by_trace.setdefault(rec["traceId"], {"ts": rec.get("timeUnixNano") or 0})
        body = rec["body"]
        users = [t for kind, role, t, _f in observability._iter_body_messages(body)
                 if kind == "input" and role == "user" and t is not None]
        if users and "user" not in inv:
            inv["user"] = users[-1]
        for msg in (body.get("output") or {}).get("messages") or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, dict) and content.get("id") and "message" in content:
                # an execute_tool record: the call with this id RAN (N17-obs381)
                inv.setdefault("ran", []).append(str(content["id"]))
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            text = _own_answer(msg.get("content"))
            if text is None:
                continue
            finish = msg["content"].get("finish_reason")
            if finish:
                inv.setdefault("uses", []).extend(_tool_use_ids(msg["content"]["message"]))
            if finish == "end_turn":
                inv["final"] = text
            if text.strip():
                inv["last_own"] = text
    console_tools: dict[str, list[str]] = {}
    for turn in observability.eval_tool_turns_from_content_logs(
            "g", session_id, None, None, logs=logs):
        if turn["role"] == "TOOL_CALL":
            console_tools.setdefault(str(turn["trace_id"]), []).append(turn["name"])
    console = observability.eval_turns_from_content_logs("g", session_id, None, None, logs=logs)
    turns: list[dict[str, Any]] = []
    for trace_id, inv in sorted(by_trace.items(), key=lambda kv: kv[1]["ts"]):
        if not inv.get("user"):
            continue
        answer = inv.get("final")
        if answer is None:
            answer = inv.get("last_own") or ""
        ran = list(inv.get("ran", []))
        tools = []
        for name, call_id in inv.get("uses", []):  # a written call counts once it ran
            if call_id in ran:
                ran.remove(call_id)
                tools.append(name)
        turns.append({"trace_id": trace_id, "user_prompt": inv["user"].strip(),
                      "answer": answer.strip(), "tools": tools})
    deviations = _console_deviations(turns, console, console_tools)
    return turns, deviations


def _tool_use_ids(raw: Any) -> list[tuple[str, str]]:
    """``(name, toolUseId)`` of a JSON part list's ``toolUse`` blocks."""
    try:
        parts = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return []
    if not isinstance(parts, list):
        return []
    return [(str(p["toolUse"].get("name") or ""), str(p["toolUse"].get("toolUseId") or ""))
            for p in parts if isinstance(p, dict) and isinstance(p.get("toolUse"), dict)]


def _service(record: dict[str, Any]) -> str | None:
    return ((record.get("resource") or {}).get("attributes") or {}).get("service.name")


def executed_tools(records: list[dict[str, Any]], trace_id: str) -> list[str]:
    """The trace's ``execute_tool`` span names by start time (``aws/spans``) — the
    execution record the expected tools are cross-checked against."""
    in_trace = [x["record"] for x in records if x["group"] == SPANS_LOG_GROUP
                and x["record"]["traceId"] == trace_id]
    # the session's own agent: the service of the trace's FIRST invoke_agent span — a called
    # A2A peer's spans share the trace under its own service.name (N17-obs380)
    agents = sorted((r for r in in_trace if str(r.get("name", "")).startswith("invoke_agent")),
                    key=lambda r: int(r["startTimeUnixNano"]))
    own = _service(agents[0]) if agents else None
    spans = [r for r in in_trace if str(r.get("name", "")).startswith("execute_tool ")
             and (own is None or _service(r) in (None, own))]
    return [r["name"][len("execute_tool "):]
            for r in sorted(spans, key=lambda r: int(r["startTimeUnixNano"]))]


def _console_deviations(turns: list[dict[str, Any]], console: list[dict[str, Any]],
                        console_tools: dict[str, list[str]] | None = None,
                        ) -> list[dict[str, Any]]:
    """Where the expected turns differ from the console's USER/ASSISTANT turns (paired per
    invocation by position: the console emits USER then an optional ASSISTANT)."""
    pairs: list[dict[str, str]] = []
    for item in console:
        if item["role"] == "USER":
            pairs.append({"user": item["text"]})
        elif pairs:
            pairs[-1]["answer"] = item["text"]
    out: list[dict[str, Any]] = []
    for index, (turn, shown) in enumerate(zip(turns, pairs, strict=False), 1):
        shown_answer = shown.get("answer", "").strip()
        if not turn["user_prompt"].startswith(shown["user"].strip()[:4000]):
            out.append({"turn": index, "field": "user_prompt",
                        "console": shown["user"][:120],
                        "reason": "console shows the trace's LAST user message (a max_tokens "
                                  "continuation prompt); the turn's own prompt is the first"})
        shown_tools = (console_tools or {}).get(turn["trace_id"], [])
        if console_tools is not None and shown_tools != turn["tools"]:
            out.append({"turn": index, "field": "tools", "console": shown_tools,
                        "reason": "console TOOL_CALL turns dedupe by toolUseId session-wide; "
                                  "models that number calls per response (call_0) repeat ids"})
        if shown_answer[:4000] != turn["answer"][:4000].strip() and not (
                len(shown_answer) == 4000 and turn["answer"].startswith(shown_answer)):
            out.append({"turn": index, "field": "answer", "console": shown_answer[:120],
                        "reason": "console fallback reads replayed history / a tool result "
                                  "when the turn has no end_turn answer (N17-obs315 MT1/G1)"})
    if len(pairs) != len(turns):
        out.append({"turn": None, "field": "turns", "console": f"{len(pairs)} turns",
                    "reason": f"expected {len(turns)} turns"})
    return out
