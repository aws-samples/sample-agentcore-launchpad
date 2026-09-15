"""Static, reviewed AgentCore code-evaluator Lambda (stdlib only — json/os; no boto3,
no network, no eval/exec/subprocess/regex). Shipped byte-identical in every package
built by ``app.assistant.evaluation_assets``; the per-plan inputs are the canonical
``rules.json`` (evaluator NAME → declarative checks) and ``provenance.json`` (an
opaque nonce that only pins the package digest to its creating operation).

Contract (devguide "Custom code-based evaluator"):
  event = {schemaVersion: "1.0", evaluatorId, evaluatorName, evaluationLevel,
           evaluationInput: {sessionSpans: [...]}, evaluationReferenceInputs: [...],
           evaluationTarget: {traceIds} (TRACE) | None (SESSION)}
  return {label, value, explanation} | {errorCode, errorMessage}
  Live AgentCore requests can omit both evaluator identity fields (2026-09-14).
  Only a single-evaluator package can resolve that envelope unambiguously; explicit
  unknown/malformed identities and identityless multi-evaluator packages fail closed.

Telemetry model — the representations the installed Strands tracer / bedrock_agentcore
ADOT serializer actually emit, typed by SOURCE (never by punctuation):
  * span documents: {traceId, spanId, name, startTimeUnixNano, endTimeUnixNano,
    attributes{gen_ai.operation.name, gen_ai.tool.name, gen_ai.response.finish_reasons,
    session.id, …}, events?[]} — attributes may be a dict or an OTLP key/value list
    (stringValue / intValue / arrayValue);
  * batch Evaluation also embeds ADOT log records in ``span_events`` on each span
    (snake_case log ids/timestamps, inherited trace id); bind these to the outer span
    before the same completeness, duplicate, conflict and truncation checks;
  * model spans (``gen_ai.operation.name`` = chat/…): ``gen_ai.choice.message`` and the
    ADOT ``body.output.messages[].content.message`` carry ``serialize(content)`` — a
    JSON list of content blocks (SERIALIZED envelope; must parse);
  * agent spans (``invoke_agent``): the same fields carry ``str(response)`` — PLAIN text,
    whatever punctuation it starts with;
  * ``gen_ai.client.inference.operation.details`` events / ``gen_ai.output.messages``
    attributes: a JSON list of {role, parts[{type, content}], finish_reason} (SERIALIZED);
  * ``gen_ai.completion`` (devguide example): plain text;
  * ``gen_ai.user/assistant/tool.message`` events and ADOT ``body.input`` are INPUT
    context — never output;
  * ``execute_event_loop_cycle`` spans are structural wrappers (they echo the cycle's
    model message and tool results) — neither an answer nor a tool call;
  * document ids (``traceId`` / ``spanId``, snake_case aliases) must be non-empty
    strings; timestamps keep exact integer nanoseconds.

Fail-closed evidence rules (every violation is an error envelope, never PASS):
  * strict schema (string "1.0"), level, target shape (TRACE: non-empty string traceIds
    only, ``spanIds`` forbidden by presence; SESSION: no target); every requested trace
    is scored independently and aggregated explicitly;
  * dropped attributes/events on any document, missing span ids, conflicting duplicate
    documents (same id, different content) and repeated OTLP attribute keys with
    different values are errors; exact duplicates are counted once;
  * the LATEST model turn decides: every finish indication of that turn — every choice
    event, every ADOT / DETAILS output message, the span ``finish_reasons`` list — is
    gathered before any output is chosen and must agree on a terminal stop; two sources
    reporting different current-output text are a conflict; a continuation
    (``tool_use``) or truncation (``length``/``max_tokens``/filter) anywhere in the
    set, no output, or a finished span without a valid end timestamp (≥ start) is not
    a complete answer;
  * a non-final model turn may legitimately end with ``tool_use`` (a tool trajectory);
    a non-final turn with no output and no continuation is missing evidence;
  * SESSION ``output_not_contains`` is a WHOLE-SESSION claim: every assistant turn's
    output must be present and complete, else the rule is unusable;
    ``output_contains`` / ``output_exact`` / TRACE rules judge the final turn only;
  * tool calls are identified by span id; a bare ``execute_tool`` without a name is
    unknown tool evidence (never zero tools); non-string or conflicting names are
    errors; ordering needs distinct start times (counts/sets do not);
  * references must carry a sessionId attributable to the spans and may not conflict.
"""

import json
import os

SCHEMA_VERSION = "1.0"
LEVELS = ("TRACE", "TOOL_CALL", "SESSION")
MODEL_OPERATIONS = ("chat", "text_completion", "generate_content", "invoke_model", "converse")
AGENT_OPERATIONS = ("invoke_agent",)
# Strands structural wrappers: they re-emit the cycle's model message (tool use or final
# text) and tool results for correlation — never an assistant answer, never a tool call
STRUCTURAL_OPERATIONS = ("execute_event_loop_cycle",)
STRUCTURAL_NAMES = ("execute_event_loop_cycle",)
TOOL_OPERATION = "execute_tool"
TOOL_NAME_KEYS = ("gen_ai.tool.name", "tool.name")
TOOL_SPAN_PREFIXES = ("execute_tool ", "execute_tool:")
COMPLETE_FINISH = ("end_turn", "stop", "stop_sequence", "completed", "end", "eos")
CONTINUE_FINISH = ("tool_use", "tool_calls", "function_call")
TRUNCATED_FINISH = ("max_tokens", "length", "content_filtered", "content_filter",
                    "guardrail_intervened", "truncated", "model_length")
SESSION_KEYS = ("session.id", "gen_ai.conversation.id")
DETAILS_EVENT = "gen_ai.client.inference.operation.details"
DROP_KEYS = ("droppedAttributesCount", "droppedEventsCount", "dropped_attributes_count",
             "dropped_events_count")
TARGET_KEYS = ("traceIds", "spanIds")

_RULES = None


class Unusable(Exception):
    """Evidence cannot support a verdict: (code, message)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _load_rules():
    global _RULES
    if _RULES is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
        with open(path, encoding="utf-8") as fh:
            _RULES = json.load(fh)
    return _RULES


def _error(code, message):
    return {"errorCode": str(code)[:64], "errorMessage": str(message)[:1000]}


# ---------------------------------------------------------------------------
# document normalization
# ---------------------------------------------------------------------------


def _otlp_value(value):
    if isinstance(value, dict):
        for inner in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if inner in value:
                return value[inner]
        if "arrayValue" in value:
            values = (value.get("arrayValue") or {}).get("values") or []
            return [_otlp_value(v) for v in values]
    return value


def _attributes(obj):
    """Flat dict of attributes from a dict or an OTLP list of {key, value}. A key
    repeated with a different value is a conflict, never last-write-wins."""
    raw = obj.get("attributes") if isinstance(obj, dict) else None
    if isinstance(raw, dict):
        return raw
    out = {}
    if isinstance(raw, list):
        for kv in raw:
            if not isinstance(kv, dict) or "key" not in kv:
                continue
            key = str(kv["key"])
            value = _otlp_value(kv.get("value"))
            if key in out and out[key] != value:
                raise Unusable("CONFLICTING_DUPLICATE",
                               f"attribute {key!r} repeated with different values")
            out[key] = value
    return out


def _time(doc, *keys):
    """Exact timestamp: JSON integers / digit strings stay integers (nanoseconds are
    never float-rounded); finite floats are accepted as given; anything else is None."""
    for key in keys:
        value = doc.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if value == value and value not in (float("inf"), float("-inf")) else None
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _identity(doc, camel, snake):
    """A raw document id: a non-empty, non-blank STRING (camelCase or the snake_case
    alias; both present must agree). Numbers, booleans, blanks and absence are not an
    identity and are never coerced into one."""
    values = [doc[k] for k in (camel, snake) if k in doc]
    if not values:
        raise Unusable("UNKNOWN_IDENTITY", f"a document has no {camel}")
    if len(values) == 2 and values[0] != values[1]:
        raise Unusable("UNKNOWN_IDENTITY", f"{camel} and {snake} disagree")
    value = values[0]
    if not isinstance(value, str) or not value.strip():
        raise Unusable("UNKNOWN_IDENTITY", f"{camel} is not a non-empty string")
    return value.strip()


def _trace_id(doc):
    return _identity(doc, "traceId", "trace_id")


def _span_id(doc):
    return _identity(doc, "spanId", "span_id")


def _events(doc):
    events = doc.get("events")
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def _body(doc):
    body = doc.get("body")
    return body if isinstance(body, dict) else None


def _is_tool_log(body):
    inputs = ((body.get("input") or {}).get("messages") or []) if body else []
    return any(isinstance(m, dict) and m.get("role") == "tool" for m in inputs)


def _dropped(doc):
    for key in DROP_KEYS:
        value = doc.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
        if isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
            return True
    return False


def _canonical(doc):
    return json.dumps(doc, sort_keys=True, default=str)


def _finish(value):
    return str(value or "").strip().lower()


def _finish_list(value):
    """All finish indications of one field: str, list, or JSON-encoded list."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [_finish(v) for v in parsed if _finish(v)]
            except ValueError:
                pass
        return [_finish(value)]
    if isinstance(value, list):
        return [_finish(v) for v in value if _finish(v)]
    return [_finish(value)]


class _Group:
    """Everything reported under one (traceId, spanId): at most one span document
    plus its log records."""

    def __init__(self, trace_id, span_id):
        self.trace_id = trace_id
        self.span_id = span_id
        self.span = None
        self.attrs = {}
        self.logs = []

    def start(self):
        if self.span is not None:
            t = _time(self.span, "startTimeUnixNano", "start_time_unix_nano", "startTime")
            if t is not None:
                return t
        for log in self.logs:
            t = _time(log, "timeUnixNano", "observedTimeUnixNano")
            if t is not None:
                return t
        return None

    def end(self):
        if self.span is not None:
            return _time(self.span, "endTimeUnixNano", "end_time_unix_nano", "endTime")
        return None

    def operation(self):
        return str(self.attrs.get("gen_ai.operation.name") or "")

    def is_structural(self):
        name = str((self.span or {}).get("name") or "")
        return self.operation() in STRUCTURAL_OPERATIONS or name in STRUCTURAL_NAMES

    def is_tool(self):
        if self.is_structural():
            return False
        if self.operation() == TOOL_OPERATION:
            return True
        if any(k in self.attrs for k in TOOL_NAME_KEYS):
            return True
        name = str((self.span or {}).get("name") or "")
        if name == TOOL_OPERATION or any(name.startswith(p) for p in TOOL_SPAN_PREFIXES):
            return True
        return self.span is None and any(_is_tool_log(_body(log)) for log in self.logs)

    def tool_name(self):
        names = []
        for key in TOOL_NAME_KEYS:
            if key in self.attrs:
                if not isinstance(self.attrs[key], str) or not self.attrs[key].strip():
                    raise Unusable("UNKNOWN_TOOL",
                                   f"tool call {self.span_id} has a non-string name")
                names.append(self.attrs[key].strip())
        name = str((self.span or {}).get("name") or "")
        for prefix in TOOL_SPAN_PREFIXES:
            if name.startswith(prefix) and name[len(prefix):].strip():
                names.append(name[len(prefix):].strip())
        if len(set(names)) > 1:
            raise Unusable("CONFLICTING_DUPLICATE",
                           f"tool call {self.span_id} carries conflicting names "
                           f"{sorted(set(names))}")
        return names[0] if names else None

    def is_model(self):
        if self.is_tool() or self.is_structural():
            return False
        if self.operation() in MODEL_OPERATIONS or self.operation() in AGENT_OPERATIONS:
            return True
        if "gen_ai.completion" in self.attrs or "gen_ai.output.messages" in self.attrs:
            return True
        if any(str(e.get("name") or "") in ("gen_ai.choice", DETAILS_EVENT)
               for e in _events(self.span or {})):
            return True
        for log in self.logs:
            body = _body(log)
            if body is not None and "output" in body and not _is_tool_log(body):
                return True
        return False

    def plain_output(self):
        """Whether this span's choice/ADOT ``message`` is plain ``str(response)`` (agent
        span) rather than a serialized content-block list (model span)."""
        return self.operation() in AGENT_OPERATIONS


def _with_span_events(docs):
    """Batch Evaluation nests ADOT log records in span_events, not OTel events.

    The outer span supplies the trace identity; a nested record's explicit ids
    must agree. Keep the existing grouping/duplicate/drop checks for these logs
    rather than treating input history or any arbitrary event as output.
    """
    for doc in docs:
        yield doc
        if "span_events" not in doc:
            continue
        events = doc["span_events"]
        if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
            raise Unusable("MALFORMED_SPAN_EVENT", "span_events must be a list of log records")
        trace_id, span_id = _trace_id(doc), _span_id(doc)
        for event in events:
            if not isinstance(event.get("body"), dict) or "name" in event:
                raise Unusable("MALFORMED_SPAN_EVENT",
                               "span_events entry is not an ADOT log record")
            if any(k in event for k in ("traceId", "trace_id")) and _trace_id(event) != trace_id:
                raise Unusable("UNKNOWN_IDENTITY",
                               "span_events trace identity differs from its span")
            if any(k in event for k in ("spanId", "span_id")) and _span_id(event) != span_id:
                raise Unusable("UNKNOWN_IDENTITY",
                               "span_events span identity differs from its span")
            log = {**event, "traceId": trace_id, "spanId": span_id}
            for camel, snake in (("timeUnixNano", "time_unix_nano"),
                                 ("observedTimeUnixNano", "observed_time_unix_nano")):
                if snake in log:
                    if camel in log and log[camel] != log[snake]:
                        raise Unusable("CONFLICTING_DUPLICATE", f"{camel} and {snake} disagree")
                    log[camel] = log[snake]
            yield log


def _group(docs):
    groups = {}
    order = []
    for doc in _with_span_events(docs):
        if _dropped(doc) or any(_dropped(e) for e in _events(doc)):
            raise Unusable("TRUNCATED_EVIDENCE", "span/event attributes or events were dropped")
        key = (_trace_id(doc), _span_id(doc))
        g = groups.get(key)
        if g is None:
            g = _Group(*key)
            groups[key] = g
            order.append(g)
        if _body(doc) is not None and "name" not in doc:
            if any(_canonical(existing) == _canonical(doc) for existing in g.logs):
                continue  # exact duplicate record
            body = _body(doc) or {}
            if "output" in body and not _is_tool_log(body) and any(
                    "output" in (_body(x) or {}) and not _is_tool_log(_body(x)) for x in g.logs):
                raise Unusable("CONFLICTING_DUPLICATE",
                               f"span {key[1]} has two different output records")
            g.logs.append(doc)
        else:
            if g.span is not None:
                if _canonical(doc) != _canonical(g.span):
                    raise Unusable("CONFLICTING_DUPLICATE",
                                   f"span {key[1]} reported twice with different content")
                continue
            g.span = doc
            g.attrs = _attributes(doc)
    return order


# ---------------------------------------------------------------------------
# output extraction (current turn only) — typed by SOURCE
# ---------------------------------------------------------------------------


def _text_parts(node):
    """Text parts of a content value: str, list of blocks, or a block dict."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        out = []
        for part in node:
            out += _text_parts(part)
        return out
    if isinstance(node, dict):
        if isinstance(node.get("text"), str) and node.get("type") in (None, "text"):
            return [node["text"]]
        if node.get("type") == "text" and isinstance(node.get("content"), str):
            return [node["content"]]
        for key in ("content", "parts"):
            if key in node and not isinstance(node[key], str):
                return _text_parts(node[key])
            if isinstance(node.get(key), str) and node.get("role", "assistant") == "assistant":
                return [node[key]]
    return []


def _serialized_message(raw, what):
    """A field documented as a serialized message/content-block list. Must parse to a
    JSON list or object; anything else is a malformed envelope."""
    if isinstance(raw, (list, dict)):
        return raw
    if not isinstance(raw, str):
        raise Unusable("MALFORMED_OUTPUT", f"{what} is not a serialized message")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise Unusable("MALFORMED_OUTPUT", f"{what} is not valid JSON") from None
    if not isinstance(parsed, (list, dict)):
        raise Unusable("MALFORMED_OUTPUT", f"{what} is not a message list")
    return parsed


def _structured_text(message):
    """All text parts of ONE structured assistant message joined (None when the message
    has no text, e.g. only tool-use blocks)."""
    if isinstance(message, dict):
        role = str(message.get("role") or "assistant").lower()
        if role != "assistant":
            return None
    parts = [p for p in _text_parts(message) if isinstance(p, str) and p.strip()]
    return "\n".join(p.strip() for p in parts) or None


def _message_text(raw, plain, what):
    """Text of a choice/ADOT ``message`` field: plain ``str(response)`` on agent spans,
    a serialized content-block list on model spans."""
    if plain:
        if not isinstance(raw, str):
            raise Unusable("MALFORMED_OUTPUT", f"{what} is not a string")
        return raw.strip() or None
    return _structured_text(_serialized_message(raw, what))


def _turn_output(group):
    """(text | None, finish indications [..], found) of the group's current-turn output.

    EVERY present representation is read — ADOT output records (all assistant messages),
    every ``gen_ai.choice`` event, DETAILS events / ``gen_ai.output.messages`` (all
    assistant messages), ``gen_ai.completion`` and the span's ``finish_reasons`` — and
    every finish indication they carry is kept. No source is preferred: two copies of the
    current output that disagree are a conflict, not a choice."""
    finishes = _finish_list(group.attrs.get("gen_ai.response.finish_reasons"))
    plain = group.plain_output()
    texts = []
    found = False
    for log in group.logs:
        body = _body(log)
        if not body or _is_tool_log(body):
            continue
        messages = (body.get("output") or {}).get("messages")
        if not isinstance(messages, list):
            continue
        found = True
        for m in messages:
            if isinstance(m, dict) and isinstance(m.get("content"), dict) \
                    and "message" in m["content"] and str(m.get("role") or "") == "assistant":
                content = m["content"]
                texts.append(_message_text(content["message"], plain, "body.output message"))
                finishes += _finish_list(content.get("finish_reason"))
    for e in _events(group.span or {}):
        name = str(e.get("name") or "")
        attrs = _attributes(e)
        if name == "gen_ai.choice":
            found = True
            if attrs.get("message") not in (None, ""):
                texts.append(_message_text(attrs.get("message"), plain, "gen_ai.choice message"))
            finishes += _finish_list(attrs.get("finish_reason"))
        elif name == DETAILS_EVENT and "gen_ai.output.messages" in attrs:
            found = True
            texts += _output_messages(attrs["gen_ai.output.messages"], finishes)
    if "gen_ai.output.messages" in group.attrs:
        found = True
        texts += _output_messages(group.attrs["gen_ai.output.messages"], finishes)
    if "gen_ai.completion" in group.attrs:
        found = True
        raw = group.attrs["gen_ai.completion"]
        if not isinstance(raw, str):
            raise Unusable("MALFORMED_OUTPUT", "gen_ai.completion is not a string")
        texts.append(raw.strip() or None)
    distinct = {t for t in texts if t is not None}
    if len(distinct) > 1:
        raise Unusable("CONFLICTING_OUTPUT",
                       "the current-turn output is reported differently by two sources")
    text = next(iter(distinct)) if distinct else None
    return text, finishes, found


def _output_messages(raw, finishes):
    """Assistant texts of a serialized message list; appends every finish indication."""
    parsed = _serialized_message(raw, "gen_ai.output.messages")
    out = []
    for m in parsed if isinstance(parsed, list) else [parsed]:
        if isinstance(m, dict) and str(m.get("role") or "assistant") == "assistant":
            out.append(_structured_text(m))
            finishes += _finish_list(m.get("finish_reason"))
    return out


def _classify(finishes):
    """One verdict from ALL finish indications: truncated > continue > unknown > complete
    — a stop hidden behind a length or tool_use never counts as complete."""
    if any(f in TRUNCATED_FINISH for f in finishes):
        return "truncated"
    if any(f in CONTINUE_FINISH for f in finishes):
        return "continue"
    if any(f not in COMPLETE_FINISH for f in finishes):
        return "unknown"
    return "complete"


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def _select(docs, level, target):
    if level == "TOOL_CALL":
        raise Unusable("TARGET_UNRESOLVED", "TOOL_CALL targets are not supported")
    if level == "TRACE":
        wanted = {str(t) for t in target["traceIds"]}
        present = set()
        for d in docs:
            try:
                present.add(_trace_id(d))
            except Unusable:
                continue  # a document without a real identity can satisfy no target
        missing = sorted(wanted - present)
        if missing:
            raise Unusable("TARGET_UNRESOLVED", f"no spans for target trace(s) {missing}")
        selected = []
        for d in docs:
            try:
                if _trace_id(d) in wanted:
                    selected.append(d)
            except Unusable:
                continue
        return selected, wanted
    return list(docs), None


def _session_id(groups):
    ids = set()
    for g in groups:
        for key in SESSION_KEYS:
            if g.attrs.get(key):
                ids.add(str(g.attrs[key]))
        for log in g.logs:
            a = _attributes(log)
            for key in SESSION_KEYS:
                if a.get(key):
                    ids.add(str(a[key]))
    if len(ids) > 1:
        raise Unusable("MIXED_SESSIONS", f"spans carry {len(ids)} different session ids")
    return next(iter(ids)) if ids else None


def _valid_end(group):
    start, end = group.start(), group.end()
    return end is not None and end > 0 and (start is None or end >= start)


def extract_evidence(docs, level, target):
    """{tools, tool_count, tool_names, output, outputs, complete_scope, finish,
    session_id, traces} of the target scope. Raises Unusable when evidence cannot be
    trusted."""
    selected, traces = _select(docs, level, target)
    groups = _group(selected)
    session_id = _session_id(groups)
    tools = []
    for g in groups:
        if not g.is_tool():
            continue
        name = g.tool_name()
        if not name:
            raise Unusable("UNKNOWN_TOOL", f"tool call {g.span_id or '?'} has no tool name")
        tools.append((g.start(), name))
    models = [g for g in groups if g.is_model()]
    if not models:
        raise Unusable("NO_MODEL_TURN", "no model/agent turn with output in the target scope")
    if len(models) > 1 and any(g.start() is None for g in models):
        raise Unusable("AMBIGUOUS_ORDER", "model turns without start times cannot be ordered")
    starts = [g.start() for g in models]
    if len(set(starts)) != len(starts):
        raise Unusable("AMBIGUOUS_ORDER", "two model turns share the same start time")
    models.sort(key=lambda g: g.start() or 0.0)
    turns = []
    for g in models:
        text, finishes, found = _turn_output(g)
        turns.append({"group": g, "text": text if found else None,
                      "verdict": _classify(finishes), "finishes": finishes})
    # the LATEST model turn decides completeness — a complete earlier turn followed by a
    # turn with no output / a continuation / a truncation is not a complete answer
    last = turns[-1]
    if last["verdict"] == "truncated":
        raise Unusable("TRUNCATED", f"final turn finish {last['finishes']} — output truncated")
    if last["verdict"] == "continue":
        raise Unusable("INCOMPLETE", f"final turn finish {last['finishes']} — the trace is "
                                     "incomplete (a later turn is missing)")
    if last["verdict"] == "unknown":
        raise Unusable("UNKNOWN_FINISH", f"final turn finish {last['finishes']} is unknown")
    if last["text"] is None:
        raise Unusable("NO_OUTPUT", "the latest model turn carries no current-turn assistant "
                                    "output (input history is not output)")
    if last["group"].span is None:
        raise Unusable("INCOMPLETE", "the final output record has no ended span document")
    if not _valid_end(last["group"]):
        raise Unusable("INCOMPLETE", "the final model span has no valid end timestamp")
    # whole-scope completeness: every earlier turn is either a complete answer or a
    # legitimate tool-use continuation followed by later turns
    complete_scope, gap = True, None
    for i, t in enumerate(turns[:-1]):
        if t["verdict"] == "continue":
            continue  # intermediate tool trajectory turn
        if t["verdict"] != "complete" or t["text"] is None or t["group"].span is None \
                or not _valid_end(t["group"]):
            complete_scope, gap = False, i
            break
    if any(t["verdict"] == "continue" for t in turns[:-1]) and not tools:
        raise Unusable("INCONSISTENT", "a tool_use turn without any tool call span")
    ordered = None
    tool_starts = [t[0] for t in tools]
    if len(tools) <= 1 or (all(s is not None for s in tool_starts)
                           and len(set(tool_starts)) == len(tool_starts)):
        ordered = [name for _, name in sorted(tools, key=lambda t: t[0] or 0.0)]
    return {"tools": ordered, "tool_count": len(tools), "tool_names": [n for _, n in tools],
            "output": last["text"], "outputs": [t["text"] for t in turns if t["text"]],
            "complete_scope": complete_scope, "scope_gap": gap,
            "finish": last["finishes"], "session_id": session_id, "traces": traces}


def _reference(reference_inputs, level, traces, session_id):
    """Merged reference fields for this scope: session-level entries (+ the target's
    trace-level entries at TRACE). Every entry must name this session; conflicting values
    for one scope are errors."""
    if reference_inputs is None:
        return {}
    if not isinstance(reference_inputs, list):
        raise Unusable("BAD_REFERENCE", "evaluationReferenceInputs is not a list")
    merged = {}
    seen = {}
    for entry in reference_inputs:
        if not isinstance(entry, dict):
            raise Unusable("BAD_REFERENCE", "reference input is not an object")
        ctx = ((entry.get("context") or {}).get("spanContext") or {})
        ref_session = ctx.get("sessionId")
        if not ref_session:
            raise Unusable("BAD_REFERENCE", "reference input has no spanContext.sessionId")
        if session_id is None:
            raise Unusable("REFERENCE_UNATTRIBUTABLE",
                           "spans carry no session id — a reference input cannot be attributed")
        if str(ref_session) != session_id:
            raise Unusable("REFERENCE_MISMATCH", "reference input belongs to another session")
        tid = ctx.get("traceId")
        if tid and (level != "TRACE" or str(tid) not in (traces or set())):
            continue  # another trace's ground truth is not this target's
        for field in ("expectedResponse", "assertions", "expectedTrajectory"):
            if field not in entry:
                continue
            scope = (field, str(tid) if tid else "")
            if scope in seen and seen[scope] != entry[field]:
                raise Unusable("REFERENCE_CONFLICT", f"conflicting {field} reference inputs")
            seen[scope] = entry[field]
            if tid or field not in merged:
                merged[field] = entry[field]
    return merged


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _norm(text, case_sensitive):
    return text if case_sensitive else text.lower()


def _is_subsequence(needle, hay):
    it = iter(hay)
    return all(any(x == y for y in it) for x in needle)


def _ordered_tools(evidence):
    if evidence["tools"] is None:
        raise Unusable("AMBIGUOUS_ORDER", "tool calls without distinct start times cannot be "
                                          "ordered")
    return evidence["tools"]


def run_check(check, evidence, reference, level):
    """(outcome, detail) with outcome ∈ pass | fail; raises Unusable."""
    kind = check.get("type")
    names = evidence["tool_names"]
    output = evidence["output"]
    if kind == "tool_count":
        observed = [t for t in names if not check.get("tool") or t == check["tool"]]
        n = len(observed)
        lo, hi = check.get("min"), check.get("max")
        ok = (lo is None or n >= lo) and (hi is None or n <= hi)
        suffix = f" of {check['tool']}" if check.get("tool") else ""
        return ("pass" if ok else "fail"), f"observed {n} call(s){suffix}"
    if kind == "tool_sequence":
        want = [str(t) for t in (check.get("tools") or [])]
        tools = _ordered_tools(evidence)
        ok = tools == want if check.get("mode") == "exact" else _is_subsequence(want, tools)
        return ("pass" if ok else "fail"), f"observed sequence {json.dumps(tools)}"
    if kind == "tool_set":
        allowed = set(check.get("allowed") or [])
        forbidden = set(check.get("forbidden") or [])
        bad = [t for t in names if (allowed and t not in allowed) or t in forbidden]
        return ("pass" if not bad else "fail"), f"disallowed tools {json.dumps(bad)}"
    if kind == "reference_trajectory":
        traj = (reference.get("expectedTrajectory") or {}).get("toolNames") \
            if isinstance(reference.get("expectedTrajectory"), dict) else None
        if not isinstance(traj, list) or not traj:
            raise Unusable("REFERENCE_MISSING", "no expectedTrajectory reference for this session")
        want = [str(t) for t in traj]
        if check.get("mode") == "exact":
            ok = _ordered_tools(evidence) == want
        else:
            ok = set(want) <= set(names)
        return ("pass" if ok else "fail"), (
            f"expected {json.dumps(want)}, observed {json.dumps(names)}")
    if kind in ("output_contains", "output_not_contains", "output_exact", "reference_response"):
        cs = bool(check.get("case_sensitive"))
        if kind == "reference_response":
            if level != "TRACE":
                raise Unusable("UNSUPPORTED", "reference_response needs a TRACE target")
            expected = (reference.get("expectedResponse") or {}).get("text") \
                if isinstance(reference.get("expectedResponse"), dict) else None
            if not isinstance(expected, str) or not expected.strip():
                raise Unusable("REFERENCE_MISSING", "no expectedResponse reference for this trace")
            text = expected
        else:
            text = str(check.get("text") or "")
        needle = _norm(text, cs)
        if kind == "output_not_contains" and level == "SESSION":
            # whole-session negative rule: needs EVERY assistant turn present and complete
            if not evidence["complete_scope"]:
                raise Unusable("EVIDENCE_INCOMPLETE",
                               f"assistant turn {evidence['scope_gap']} of the session has no "
                               "complete output — a whole-session absence cannot be asserted")
            hits = [i for i, o in enumerate(evidence["outputs"]) if needle in _norm(o, cs)]
            return ("pass" if not hits else "fail"), (
                f"checked all {len(evidence['outputs'])} assistant turn(s); "
                f"violations in turn(s) {hits}" if hits else
                f"checked all {len(evidence['outputs'])} assistant turn(s)")
        hay = _norm(output, cs)
        if kind == "output_exact":
            ok = hay.strip() == needle.strip()
        elif kind == "output_not_contains":
            ok = needle not in hay
        else:
            ok = needle in hay
        return ("pass" if ok else "fail"), f"final turn, output length {len(output)}"
    raise Unusable("BAD_RULE", f"unknown check type {kind!r}")


def _validate_target(level, target):
    """Strict target shape by PRESENCE and type: TRACE = {traceIds: [non-empty str, …]}
    only; SESSION = no target."""
    if level == "SESSION":
        if target is not None:
            raise Unusable("BAD_TARGET", "SESSION evaluation takes no evaluationTarget")
        return [None]
    if not isinstance(target, dict):
        raise Unusable("TARGET_UNRESOLVED", "TRACE evaluation without target traceIds")
    if "spanIds" in target:
        raise Unusable("BAD_TARGET", "TRACE evaluation does not accept spanIds")
    unknown = sorted(set(target) - set(TARGET_KEYS))
    if unknown:
        raise Unusable("BAD_TARGET", f"unknown evaluationTarget members {unknown}")
    ids = target.get("traceIds")
    if not isinstance(ids, list) or not ids or not all(
            isinstance(t, str) and t.strip() for t in ids):
        raise Unusable("TARGET_UNRESOLVED", "traceIds must be a non-empty list of non-empty "
                                            "strings")
    return list(dict.fromkeys(ids))


def evaluate(rules, event):
    """Pure evaluation of one event against one evaluator's rules (testable)."""
    try:
        version = event.get("schemaVersion")
        if not isinstance(version, str) or version != SCHEMA_VERSION:
            return _error("BAD_SCHEMA", f"schemaVersion must be the string {SCHEMA_VERSION!r}")
        level = event.get("evaluationLevel")
        if not isinstance(level, str) or level not in LEVELS:
            return _error("BAD_LEVEL", f"evaluationLevel {level!r} is not one of {LEVELS}")
        if level == "TOOL_CALL":
            return _error("TARGET_UNRESOLVED", "TOOL_CALL targets are not supported")
        spans_in = (event.get("evaluationInput") or {}).get("sessionSpans")
        if not isinstance(spans_in, list) or not spans_in:
            return _error("NO_SPANS", "evaluationInput.sessionSpans is empty or missing")
        if not all(isinstance(d, dict) for d in spans_in):
            return _error("MALFORMED_SPAN", "sessionSpans contains a non-object entry")
        checks = rules.get("checks") if isinstance(rules, dict) else None
        if not isinstance(checks, list) or not checks or not all(
                isinstance(c, dict) for c in checks):
            return _error("BAD_RULE", "no checks packaged for this evaluator")
        scopes = _validate_target(level, event.get("evaluationTarget"))
        # each requested trace is scored on its own complete evidence and its own
        # references; the verdict aggregates explicitly (any error → error, any fail → FAIL)
        results = []
        for scope in scopes:
            scoped_target = {"traceIds": [scope]} if scope is not None else None
            evidence = extract_evidence(spans_in, level, scoped_target)
            reference = _reference(event.get("evaluationReferenceInputs"), level,
                                   evidence["traces"], evidence["session_id"])
            for check in checks:
                outcome, detail = run_check(check, evidence, reference, level)
                label = str(check.get("id")) + (f"@{scope}" if scope is not None else "")
                results.append((label, outcome, detail))
    except Unusable as exc:
        return _error(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 — never leak a traceback, never PASS
        return _error("INTERNAL", f"{type(exc).__name__}: {exc}")
    explanation = "; ".join(f"{r[0]}={r[1]} ({r[2]})" for r in results)
    if any(r[1] == "fail" for r in results):
        return {"label": "FAIL", "value": 0.0, "explanation": explanation[:2000]}
    return {"label": "PASS", "value": 1.0, "explanation": explanation[:2000]}


def lambda_handler(event, context):
    if not isinstance(event, dict):
        return _error("BAD_EVENT", "event is not an object")
    try:
        rules_by_name = _load_rules().get("evaluators") or {}
    except (OSError, ValueError) as exc:
        return _error("RULES_UNAVAILABLE", f"rules.json unreadable: {exc}")
    name = event.get("evaluatorName")
    if "evaluatorName" not in event and "evaluatorId" not in event:
        # The live service omits both documented identities. Never guess between
        # rule sets, nor ignore an explicitly supplied but unrecognized identity.
        if len(rules_by_name) != 1:
            return _error("UNKNOWN_EVALUATOR", "event has no evaluator identity; "
                          "exactly one packaged evaluator is required")
        name = next(iter(rules_by_name))
    rules = rules_by_name.get(name) if isinstance(name, str) and name else None
    if rules is None:
        return _error("UNKNOWN_EVALUATOR", f"no rules packaged for evaluator name {name!r}")
    return evaluate(rules, event)
