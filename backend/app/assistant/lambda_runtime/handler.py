"""Static, reviewed AgentCore code-evaluator Lambda (stdlib only — json/os; no boto3,
no network, no eval/exec/subprocess/regex). Shipped byte-identical in every package
built by ``app.assistant.evaluation_assets``; the per-plan inputs are the canonical
``rules.json`` (evaluator NAME → declarative checks) and ``provenance.json`` (an
opaque nonce that only pins the package digest to its creating operation).

Contract (devguide "Custom code-based evaluator"):
  event = {schemaVersion: "1.0", evaluatorId, evaluatorName, evaluationLevel,
           evaluationInput: {sessionSpans: [...]}, evaluationReferenceInputs: [...],
           evaluationTarget: {traceIds?, spanIds?} | None}
  return {label, value, explanation} | {errorCode, errorMessage}

Telemetry model (what ``sessionSpans`` carries — the ADOT documents the installed
``bedrock_agentcore`` serializer emits, plus the raw OTel forms the devguide shows):
  * span documents: {traceId, spanId, name, startTimeUnixNano, endTimeUnixNano,
    attributes{gen_ai.operation.name, gen_ai.tool.name, session.id, …}, events?[]};
  * conversation log records: {traceId, spanId, timeUnixNano, body{input{messages[]},
    output{messages[{role: assistant, content{message, finish_reason}}]}}} —
    ``body.output`` is the CURRENT turn's output, ``body.input`` is history;
  * tool log records: body.input.messages[0].role == "tool" (never output);
  * raw Strands events: ``gen_ai.choice`` = current output (``message``,
    ``finish_reason``); ``gen_ai.user/assistant/tool.message`` = INPUT context;
  * devguide/semconv attributes ``gen_ai.completion`` / ``gen_ai.output.messages``.

Fail-closed evidence rules (every violation is an error envelope, never PASS):
  * schema/level/target validated; TOOL_CALL refused; every target trace needs spans;
  * the observed output is the FINAL model turn's current-turn assistant message —
    all of its text parts joined; history, prompts, tool inputs/results and
    reference inputs are never read as output; a structured field that is not valid
    JSON is malformed, not literal text;
  * the final turn must be complete: a ``tool_use``-style finish means the trace is
    incomplete, a length / content-filter finish means truncated, no output at all
    means no evidence; tool rules need that same complete final turn;
  * tool calls are identified by span id (no double counting of span + log record);
    a tool call without a name is unknown evidence; ordering uses start time and is
    an error when ambiguous; reference inputs must belong to this session and may not
    conflict; a missing reference for a reference rule is an error.
"""

import json
import os

SCHEMA_VERSION = "1.0"
LEVELS = ("TRACE", "TOOL_CALL", "SESSION")
MODEL_OPERATIONS = ("chat", "invoke_agent", "text_completion", "generate_content",
                    "invoke_model", "converse")
TOOL_OPERATION = "execute_tool"
TOOL_NAME_KEYS = ("gen_ai.tool.name", "tool.name")
TOOL_SPAN_PREFIXES = ("execute_tool ", "execute_tool:")
COMPLETE_FINISH = ("end_turn", "stop", "stop_sequence", "completed", "end", "eos")
CONTINUE_FINISH = ("tool_use", "tool_calls", "function_call")
TRUNCATED_FINISH = ("max_tokens", "length", "content_filtered", "content_filter",
                    "guardrail_intervened", "truncated", "model_length")
SESSION_KEYS = ("session.id", "gen_ai.conversation.id")

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


def _attributes(obj):
    """Flat dict of attributes from a dict or an OTLP list of {key, value}."""
    raw = obj.get("attributes") if isinstance(obj, dict) else None
    if isinstance(raw, dict):
        return raw
    out = {}
    if isinstance(raw, list):
        for kv in raw:
            if not isinstance(kv, dict) or "key" not in kv:
                continue
            value = kv.get("value")
            if isinstance(value, dict):
                for inner in ("stringValue", "intValue", "doubleValue", "boolValue"):
                    if inner in value:
                        value = value[inner]
                        break
            out[str(kv["key"])] = value
    return out


def _time(doc, *keys):
    for key in keys:
        value = doc.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip().isdigit():
            return float(value)
    return None


def _trace_id(doc):
    return str(doc.get("traceId") or doc.get("trace_id") or "")


def _span_id(doc):
    return str(doc.get("spanId") or doc.get("span_id") or "")


def _events(doc):
    events = doc.get("events")
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def _body(doc):
    body = doc.get("body")
    return body if isinstance(body, dict) else None


def _is_tool_log(body):
    inputs = ((body.get("input") or {}).get("messages") or []) if body else []
    return any(isinstance(m, dict) and m.get("role") == "tool" for m in inputs)


class _Group:
    """Everything reported under one (traceId, spanId): at most one span document
    plus its log records."""

    def __init__(self, trace_id, span_id):
        self.trace_id = trace_id
        self.span_id = span_id
        self.span = None
        self.attrs = {}
        self.logs = []

    def time(self):
        if self.span is not None:
            t = _time(self.span, "startTimeUnixNano", "start_time_unix_nano", "startTime")
            if t is not None:
                return t
        for log in self.logs:
            t = _time(log, "timeUnixNano", "observedTimeUnixNano")
            if t is not None:
                return t
        return None

    def is_tool(self):
        if self.attrs.get("gen_ai.operation.name") == TOOL_OPERATION:
            return True
        if any(k in self.attrs for k in TOOL_NAME_KEYS):
            return True
        name = str((self.span or {}).get("name") or "")
        if any(name.startswith(p) for p in TOOL_SPAN_PREFIXES):
            return True
        return self.span is None and any(_is_tool_log(_body(log)) for log in self.logs)

    def tool_name(self):
        for key in TOOL_NAME_KEYS:
            if self.attrs.get(key):
                return str(self.attrs[key])
        name = str((self.span or {}).get("name") or "")
        for prefix in TOOL_SPAN_PREFIXES:
            if name.startswith(prefix) and name[len(prefix):].strip():
                return name[len(prefix):].strip()
        return None

    def is_model(self):
        if self.is_tool():
            return False
        if self.attrs.get("gen_ai.operation.name") in MODEL_OPERATIONS:
            return True
        if "gen_ai.completion" in self.attrs or "gen_ai.output.messages" in self.attrs:
            return True
        if any(str(e.get("name") or "") == "gen_ai.choice" for e in _events(self.span or {})):
            return True
        for log in self.logs:
            body = _body(log)
            if body is not None and "output" in body and not _is_tool_log(body):
                return True
        return False


def _group(docs):
    groups = {}
    order = []
    for doc in docs:
        key = (_trace_id(doc), _span_id(doc))
        g = groups.get(key)
        if g is None:
            g = _Group(*key)
            groups[key] = g
            order.append(g)
        if _body(doc) is not None and "name" not in doc:
            g.logs.append(doc)
        else:
            if g.span is not None:
                # the same span reported twice: keep one, never count twice
                continue
            g.span = doc
            g.attrs = _attributes(doc)
    return order


# ---------------------------------------------------------------------------
# output extraction (current turn only)
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


def _looks_structured(text):
    stripped = text.lstrip()
    return stripped.startswith("[") or stripped.startswith("{")


def _message_text(message):
    """All text parts of ONE assistant message joined. A string that looks like
    serialized JSON must parse (structured content blocks) — otherwise it is
    malformed evidence, not literal output. Returns None when the message has no
    text (e.g. only tool-use blocks)."""
    if isinstance(message, str):
        if _looks_structured(message):
            try:
                message = json.loads(message)
            except ValueError:
                raise Unusable("MALFORMED_OUTPUT", "assistant output is not valid JSON") from None
        else:
            return message.strip() or None
    if isinstance(message, dict):
        role = str(message.get("role") or "assistant").lower()
        if role != "assistant":
            return None
        if isinstance(message.get("content"), dict) and "message" in message["content"]:
            return _message_text(message["content"]["message"])
    parts = [p for p in _text_parts(message) if isinstance(p, str) and p.strip()]
    return "\n".join(p.strip() for p in parts) or None


def _finish(value):
    return str(value or "").strip().lower()


def _turn_output(group):
    """(text | None, finish_reason | None, found) of the group's current-turn output.
    Order of evidence: conversation log record → gen_ai.choice events →
    gen_ai.output.messages → gen_ai.completion. Input history is never consulted."""
    for log in group.logs:
        body = _body(log)
        if not body or _is_tool_log(body):
            continue
        messages = (body.get("output") or {}).get("messages")
        if not isinstance(messages, list):
            continue
        chosen = None
        for m in messages:
            if isinstance(m, dict) and isinstance(m.get("content"), dict) \
                    and "message" in m["content"] and str(m.get("role") or "") == "assistant":
                chosen = m
        if chosen is None:
            return None, None, True
        content = chosen["content"]
        return _message_text(content["message"]), _finish(content.get("finish_reason")), True
    choices = [e for e in _events(group.span or {}) if str(e.get("name") or "") == "gen_ai.choice"]
    if choices:
        attrs = _attributes(choices[-1])
        text = _message_text(attrs.get("message")) if attrs.get("message") else None
        return text, _finish(attrs.get("finish_reason")), True
    if "gen_ai.output.messages" in group.attrs:
        raw = group.attrs["gen_ai.output.messages"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raise Unusable("MALFORMED_OUTPUT",
                               "gen_ai.output.messages is not valid JSON") from None
        chosen = None
        for m in raw if isinstance(raw, list) else [raw]:
            if isinstance(m, dict) and str(m.get("role") or "assistant") == "assistant":
                chosen = m
        finish = _finish(chosen.get("finish_reason")) if isinstance(chosen, dict) else ""
        return (_message_text(chosen) if chosen is not None else None), finish, True
    if "gen_ai.completion" in group.attrs:
        raw = group.attrs["gen_ai.completion"]
        text = _message_text(raw) if isinstance(raw, str) else _message_text(json.dumps(raw))
        return text, "", True
    return None, None, False


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def _select(docs, level, target):
    if level == "TOOL_CALL":
        raise Unusable("TARGET_UNRESOLVED", "TOOL_CALL targets are not supported")
    if level == "TRACE":
        ids = target.get("traceIds") if isinstance(target, dict) else None
        if not isinstance(ids, list) or not ids:
            raise Unusable("TARGET_UNRESOLVED", "TRACE evaluation without target traceIds")
        wanted = {str(t) for t in ids}
        present = {_trace_id(d) for d in docs}
        missing = sorted(wanted - present)
        if missing:
            raise Unusable("TARGET_UNRESOLVED", f"no spans for target trace(s) {missing}")
        return [d for d in docs if _trace_id(d) in wanted], wanted
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


def extract_evidence(docs, level, target):
    """{tools: [names in call order] | None (ambiguous order), tool_count, output,
    finish} of the target scope. Raises Unusable when evidence cannot be trusted."""
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
        tools.append((g.time(), name))
    models = [g for g in groups if g.is_model()]
    if not models:
        raise Unusable("NO_MODEL_TURN", "no model/agent turn with output in the target scope")
    turns = []
    for g in models:
        text, finish, found = _turn_output(g)
        if found:
            turns.append((g.time(), text, finish))
    if not turns:
        raise Unusable("NO_OUTPUT", "no current-turn assistant output in the target scope "
                                    "(input history is not output)")
    if len(turns) > 1 and any(t[0] is None for t in turns):
        raise Unusable("AMBIGUOUS_ORDER", "model turns without start times cannot be ordered")
    turns.sort(key=lambda t: t[0] or 0.0)
    _, text, finish = turns[-1]
    if finish in CONTINUE_FINISH:
        raise Unusable("INCOMPLETE", f"final turn ended with '{finish}' — the trace is "
                                     "incomplete (a later turn is missing)")
    if finish in TRUNCATED_FINISH:
        raise Unusable("TRUNCATED", f"final turn ended with '{finish}' — output truncated")
    if finish and finish not in COMPLETE_FINISH:
        raise Unusable("UNKNOWN_FINISH", f"final turn finish reason '{finish}' is unknown")
    if not text:
        raise Unusable("NO_OUTPUT", "final turn carries no assistant text")
    ordered = None
    if all(t[0] is not None for t in tools) or len(tools) <= 1:
        ordered = [name for _, name in sorted(tools, key=lambda t: t[0] or 0.0)]
    return {"tools": ordered, "tool_count": len(tools), "tool_names": [n for _, n in tools],
            "output": text, "finish": finish, "session_id": session_id, "traces": traces}


def _reference(reference_inputs, level, traces, session_id):
    """Merged reference fields for this scope: session-level entries (+ trace-level
    entries of the target at TRACE). Entries of another session or conflicting
    values for one scope are errors."""
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
        if ref_session and session_id and str(ref_session) != session_id:
            raise Unusable("REFERENCE_MISMATCH", "reference input belongs to another session")
        tid = ctx.get("traceId")
        if tid:
            if level != "TRACE" or str(tid) not in (traces or set()):
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
        raise Unusable("AMBIGUOUS_ORDER", "tool calls without start times cannot be ordered")
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
        hay, needle = _norm(output, cs), _norm(text, cs)
        if kind == "output_exact":
            ok = hay.strip() == needle.strip()
        elif kind == "output_not_contains":
            ok = needle not in hay
        else:
            ok = needle in hay
        return ("pass" if ok else "fail"), f"output length {len(output)}"
    raise Unusable("BAD_RULE", f"unknown check type {kind!r}")


def evaluate(rules, event):
    """Pure evaluation of one event against one evaluator's rules (testable)."""
    try:
        if str(event.get("schemaVersion") or "") != SCHEMA_VERSION:
            return _error("BAD_SCHEMA", f"schemaVersion must be {SCHEMA_VERSION}")
        level = str(event.get("evaluationLevel") or "")
        if level not in LEVELS:
            return _error("BAD_LEVEL", f"evaluationLevel {level!r} is not one of {LEVELS}")
        spans_in = (event.get("evaluationInput") or {}).get("sessionSpans")
        if not isinstance(spans_in, list) or not spans_in:
            return _error("NO_SPANS", "evaluationInput.sessionSpans is empty or missing")
        if not all(isinstance(d, dict) for d in spans_in):
            return _error("MALFORMED_SPAN", "sessionSpans contains a non-object entry")
        target = event.get("evaluationTarget")
        evidence = extract_evidence(spans_in, level, target)
        reference = _reference(event.get("evaluationReferenceInputs"), level,
                               evidence["traces"], evidence["session_id"])
        checks = rules.get("checks") if isinstance(rules, dict) else None
        if not isinstance(checks, list) or not checks:
            return _error("BAD_RULE", "no checks packaged for this evaluator")
        results = []
        for check in checks:
            outcome, detail = run_check(check, evidence, reference, level)
            results.append((str(check.get("id")), outcome, detail))
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
    name = str(event.get("evaluatorName") or "")
    rules = rules_by_name.get(name)
    if rules is None:
        return _error("UNKNOWN_EVALUATOR", f"no rules packaged for evaluator name {name!r}")
    return evaluate(rules, event)
