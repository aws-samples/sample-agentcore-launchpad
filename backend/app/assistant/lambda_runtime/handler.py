"""Static, reviewed AgentCore code-evaluator Lambda (stdlib only — no boto3, no
network, no eval/exec/subprocess/regex). Shipped byte-identical in every package
built by ``app.assistant.lambda_package``; the only per-plan input is the canonical
``rules.json`` next to it, mapping each evaluator NAME to declarative checks.

Contract (devguide "Custom code-based evaluator"):
  event = {schemaVersion, evaluatorId, evaluatorName, evaluationLevel,
           evaluationInput: {sessionSpans: [...]}, evaluationReferenceInputs: [...],
           evaluationTarget: {traceIds?, spanIds?} | None}
  return {label, value, explanation} | {errorCode, errorMessage}

Fail-closed evidence rules:
  * only spans inside the evaluation target are inspected (TRACE → traceIds; SESSION →
    all spans; TOOL_CALL targets are refused);
  * the observed assistant output is the LAST assistant message emitted by a model /
    agent span — user prompts, system prompts, tool inputs/results and reference
    inputs are never read as output;
  * no spans, no target match, no identifiable assistant output (for output_* /
    reference_response rules), or a missing reference input (for reference_* rules)
    → error, never PASS. A "no tool call" rule therefore cannot pass just because a
    trace is missing: tool evidence requires at least one recognized model/agent span.
"""

import json
import os

TOOL_NAME_KEYS = ("gen_ai.tool.name", "tool.name")
TOOL_SPAN_PREFIXES = ("execute_tool ", "execute_tool:", "tool ")
OUTPUT_ATTR_KEYS = ("gen_ai.completion", "gen_ai.output.messages")
OUTPUT_EVENT_NAMES = ("gen_ai.choice", "gen_ai.assistant.message")
MODEL_SPAN_MARKERS = ("model", "chat", "invoke_model", "converse", "agent", "harness",
                      "invoke_agent", "llm")

_RULES = None


def _load_rules():
    global _RULES
    if _RULES is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
        with open(path, encoding="utf-8") as fh:
            _RULES = json.load(fh)
    return _RULES


def _error(code, message):
    return {"errorCode": code, "errorMessage": message[:1000]}


# ---------------------------------------------------------------------------
# span normalization
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


def _span_trace_id(span):
    return str(span.get("traceId") or span.get("trace_id") or "")


def _span_name(span):
    return str(span.get("name") or span.get("spanName") or "")


def _events(span):
    events = span.get("events")
    return events if isinstance(events, list) else []


def _sort_key(span):
    for key in ("endTimeUnixNano", "end_time_unix_nano", "endTime", "startTimeUnixNano",
                "startTime"):
        value = span.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.isdigit():
            return float(value)
    return 0.0


def _target_spans(spans, level, target):
    if level == "TOOL_CALL":
        return None, "TOOL_CALL targets are not supported by this evaluator"
    if level == "TRACE":
        trace_ids = set()
        if isinstance(target, dict):
            trace_ids = {str(t) for t in (target.get("traceIds") or [])}
        if not trace_ids:
            return None, "TRACE evaluation without target traceIds"
        picked = [s for s in spans if _span_trace_id(s) in trace_ids]
        if not picked:
            return None, "no span of the session matches the target trace"
        return picked, None
    return list(spans), None


# ---------------------------------------------------------------------------
# evidence extraction
# ---------------------------------------------------------------------------


def _is_tool_span(span, attrs):
    if any(k in attrs for k in TOOL_NAME_KEYS):
        return True
    name = _span_name(span)
    return any(name.startswith(p) for p in TOOL_SPAN_PREFIXES)


def _tool_name(span, attrs):
    for key in TOOL_NAME_KEYS:
        if attrs.get(key):
            return str(attrs[key])
    name = _span_name(span)
    for prefix in TOOL_SPAN_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):].strip()
    return ""


def _is_model_span(span, attrs):
    if _is_tool_span(span, attrs):
        return False
    lowered = _span_name(span).lower()
    if any(m in lowered for m in MODEL_SPAN_MARKERS):
        return True
    return any(str(k).startswith("gen_ai.") for k in attrs)


def _texts_of_message(message):
    """Assistant text parts of one message object/string (never tool inputs)."""
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except ValueError:
            return [message]
    if isinstance(message, list):
        out = []
        for part in message:
            out += _texts_of_message(part)
        return out
    if not isinstance(message, dict):
        return []
    role = str(message.get("role") or "").lower()
    if role and role != "assistant":
        return []
    content = message.get("content")
    if content is None:
        content = message.get("parts")
    if content is None and "text" in message:
        content = message.get("text")
    if isinstance(content, str):
        return [content]
    out = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            if part.get("type") in (None, "text") and isinstance(part.get("text"), str):
                out.append(part["text"])
            elif part.get("type") == "text" and isinstance(part.get("content"), str):
                out.append(part["content"])
    return out


def _outputs_of_span(span, attrs):
    outputs = []
    for key in OUTPUT_ATTR_KEYS:
        if key in attrs:
            outputs += _texts_of_message(attrs[key])
    for event in _events(span):
        if not isinstance(event, dict) or str(event.get("name") or "") not in OUTPUT_EVENT_NAMES:
            continue
        eattrs = _attributes(event)
        for key in ("message", "gen_ai.completion", "content"):
            if key in eattrs:
                outputs += _texts_of_message(eattrs[key])
    return [o for o in outputs if isinstance(o, str) and o.strip()]


def extract_evidence(spans):
    """(tool_names_in_order, final_assistant_output_or_None, model_span_count)."""
    ordered = sorted(spans, key=_sort_key)
    tools = []
    outputs = []
    model_spans = 0
    for span in ordered:
        if not isinstance(span, dict):
            continue
        attrs = _attributes(span)
        if _is_tool_span(span, attrs):
            name = _tool_name(span, attrs)
            if name:
                tools.append(name)
            continue
        if _is_model_span(span, attrs):
            model_spans += 1
            outputs += _outputs_of_span(span, attrs)
    final = outputs[-1] if outputs else None
    return tools, final, model_spans


def _reference(reference_inputs, level, target):
    """Reference input for this target: trace-scoped entry for TRACE, else the
    session-scoped one. Returns a dict (possibly empty)."""
    if not isinstance(reference_inputs, list):
        return {}
    trace_ids = set()
    if level == "TRACE" and isinstance(target, dict):
        trace_ids = {str(t) for t in (target.get("traceIds") or [])}
    session_scoped, trace_scoped = {}, {}
    for entry in reference_inputs:
        if not isinstance(entry, dict):
            continue
        ctx = ((entry.get("context") or {}).get("spanContext") or {})
        tid = ctx.get("traceId")
        if tid and str(tid) in trace_ids:
            trace_scoped.update(entry)
        elif not tid:
            session_scoped.update(entry)
    merged = dict(session_scoped)
    merged.update(trace_scoped)
    return merged


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _norm(text, case_sensitive):
    return text if case_sensitive else text.lower()


def _is_subsequence(needle, hay):
    it = iter(hay)
    return all(any(x == y for y in it) for x in needle)


def run_check(check, tools, output, model_spans, reference):
    """(outcome, detail) with outcome ∈ pass | fail | error."""
    kind = check.get("type")
    if kind in ("tool_count", "tool_sequence", "tool_set", "reference_trajectory"):
        if model_spans == 0:
            return "error", "no model/agent span in the target — tool evidence unavailable"
    if kind == "tool_count":
        observed = [t for t in tools if not check.get("tool") or t == check["tool"]]
        n = len(observed)
        lo, hi = check.get("min"), check.get("max")
        ok = (lo is None or n >= lo) and (hi is None or n <= hi)
        suffix = f" of {check['tool']}" if check.get("tool") else ""
        return ("pass" if ok else "fail"), f"observed {n} call(s){suffix}"
    if kind == "tool_sequence":
        want = list(check.get("tools") or [])
        ok = tools == want if check.get("mode") == "exact" else _is_subsequence(want, tools)
        return ("pass" if ok else "fail"), f"observed sequence {json.dumps(tools)}"
    if kind == "tool_set":
        allowed = set(check.get("allowed") or [])
        forbidden = set(check.get("forbidden") or [])
        bad = [t for t in tools if (allowed and t not in allowed) or t in forbidden]
        return ("pass" if not bad else "fail"), f"disallowed tools {json.dumps(bad)}"
    if kind == "reference_trajectory":
        traj = (reference.get("expectedTrajectory") or {}).get("toolNames")
        if not isinstance(traj, list) or not traj:
            return "error", "no expectedTrajectory reference input for this session"
        want = [str(t) for t in traj]
        ok = tools == want if check.get("mode") == "exact" else set(want) <= set(tools)
        return ("pass" if ok else "fail"), (
            f"expected {json.dumps(want)}, observed {json.dumps(tools)}")
    if kind in ("output_contains", "output_not_contains", "output_exact", "reference_response"):
        if output is None:
            return "error", "no assistant output identified in the target spans"
        cs = bool(check.get("case_sensitive"))
        if kind == "reference_response":
            expected = (reference.get("expectedResponse") or {}).get("text")
            if not isinstance(expected, str) or not expected.strip():
                return "error", "no expectedResponse reference input for this trace"
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
    return "error", f"unknown check type {kind!r}"


def evaluate(rules, event):
    """Pure evaluation of one event against one evaluator's rules (testable)."""
    level = str(event.get("evaluationLevel") or "")
    spans_in = (event.get("evaluationInput") or {}).get("sessionSpans")
    if not isinstance(spans_in, list) or not spans_in:
        return _error("NO_SPANS", "evaluationInput.sessionSpans is empty or missing")
    spans, problem = _target_spans(spans_in, level, event.get("evaluationTarget"))
    if problem:
        return _error("TARGET_UNRESOLVED", problem)
    tools, output, model_spans = extract_evidence(spans)
    reference = _reference(event.get("evaluationReferenceInputs"), level,
                           event.get("evaluationTarget"))
    results = []
    for check in rules.get("checks") or []:
        outcome, detail = run_check(check, tools, output, model_spans, reference)
        results.append((str(check.get("id")), outcome, detail))
    errors = [r for r in results if r[1] == "error"]
    if errors:
        return _error("EVIDENCE_UNAVAILABLE", "; ".join(f"{r[0]}: {r[2]}" for r in errors))
    failed = [r for r in results if r[1] == "fail"]
    explanation = "; ".join(f"{r[0]}={r[1]} ({r[2]})" for r in results)
    if failed:
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
        return _error("UNKNOWN_EVALUATOR",
                      f"no rules packaged for evaluator name {name!r}")
    return evaluate(rules, event)
