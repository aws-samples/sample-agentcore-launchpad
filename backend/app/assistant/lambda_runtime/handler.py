"""Static, reviewed code-evaluator Lambda for the Launchpad evaluation engine (CN, N12).

Stdlib only (json/os; no boto3, no network, no eval/exec/subprocess/regex). Shipped
byte-identical in every package built by ``app.assistant.evaluation_assets``; the
per-plan inputs are the canonical ``rules.json`` (evaluator NAME → declarative checks)
and ``provenance.json`` (an opaque nonce that only pins the package digest to its
creating operation).

Contract — the event the evaluation worker's ``LambdaEvaluator``
(``app/services/strands_eval/code_evaluators.py``) sends for ONE evaluation unit is Global
AgentCore's envelope (N17-obs327) plus the CN case fields this handler reads:

  event = {schemaVersion: "1.0", evaluatorId, evaluatorName: <name>, evaluationLevel,
           evaluationTarget, evaluationReferenceInputs, evaluator: <name>,
           evaluationInput: {sessionSpans: [...], input, actual_output, expected_output,
                             expected_assertion, expected_trajectory: [tool, …] | null,
                             actual_trajectory: [tool, …]}}
  return {label: "PASS" | "FAIL", value: 1.0 | 0.0, explanation} | {errorCode, errorMessage}

The worker has already extracted the case from the session's spans (final assistant
output, ordered tool-call names — a TRACE unit's own turn — the dataset's references); the
span forensics over ``sessionSpans`` are therefore not needed here. The packaged evaluator
is the one ``evaluatorName`` (else ``evaluator``) names; only a single-evaluator package can
resolve an event without a name; an unknown name fails closed.

Fail-closed rules (every violation is an error envelope, never PASS): a strict schema
(string "1.0"), a packaged evaluator with at least one check, an ``actual_trajectory``
that is a list of strings when a tool rule needs it, a non-empty ``expected_trajectory``
/ ``expected_output`` reference when a reference rule needs it, and a final output
present (a string) for every output rule. Every check must pass; one failing check is a
FAIL with every check's verdict in the explanation.
"""

import json
import os

SCHEMA_VERSION = "1.0"
_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
_rules_cache = None

CHECK_TYPES = ("tool_count", "tool_sequence", "tool_set", "output_contains",
               "output_not_contains", "output_exact", "reference_trajectory",
               "reference_response", "non_empty_output")


class Unusable(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(code, message):
    return {"errorCode": code, "errorMessage": str(message)[:2000]}


def load_rules(path=_RULES_PATH):
    global _rules_cache
    if _rules_cache is None:
        with open(path, encoding="utf-8") as fh:
            _rules_cache = json.load(fh)
    return _rules_cache


def resolve_evaluator(rules, name):
    """The packaged rule set for ``name``; a missing name resolves only when the package
    holds exactly one evaluator."""
    evaluators = rules.get("evaluators") if isinstance(rules, dict) else None
    if not isinstance(evaluators, dict) or not evaluators:
        raise Unusable("BAD_RULE", "no evaluators packaged")
    if name is None or name == "":
        if len(evaluators) != 1:
            raise Unusable("AMBIGUOUS_EVALUATOR",
                           "the event names no evaluator and the package holds several")
        return next(iter(evaluators.values()))
    if not isinstance(name, str) or name not in evaluators:
        raise Unusable("UNKNOWN_EVALUATOR", f"evaluator {name!r} is not packaged here")
    return evaluators[name]


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def _text(value):
    """The final assistant output as text: a string as is; a content list / dict as the
    joined text parts; ``None`` when there is no output."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "output", "message"):
            if key in value:
                return _text(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, list):
        parts = [_text(v) for v in value]
        return "".join(p for p in parts if isinstance(p, str))
    return str(value)


def _tools(value, what):
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise Unusable("MALFORMED_EVIDENCE", f"{what} must be a list of tool names")
    return list(value)


def _norm(text, case_sensitive):
    return text if case_sensitive else text.lower()


def _is_subsequence(needle, hay):
    it = iter(hay)
    return all(any(x == y for y in it) for x in needle)


def _need_output(evidence):
    if evidence["output"] is None:
        raise Unusable("EVIDENCE_INCOMPLETE", "the case carries no final assistant output")
    return evidence["output"]


def _need_tools(evidence):
    if evidence["tools"] is None:
        raise Unusable("EVIDENCE_INCOMPLETE", "the case carries no actual_trajectory")
    return evidence["tools"]


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def run_check(check, evidence):
    """(outcome, detail) with outcome ∈ pass | fail; raises Unusable."""
    kind = check.get("type")
    if kind == "tool_count":
        names = _need_tools(evidence)
        observed = [t for t in names if not check.get("tool") or t == check["tool"]]
        n = len(observed)
        lo, hi = check.get("min"), check.get("max")
        ok = (lo is None or n >= lo) and (hi is None or n <= hi)
        suffix = f" of {check['tool']}" if check.get("tool") else ""
        return ("pass" if ok else "fail"), f"observed {n} call(s){suffix}"
    if kind == "tool_sequence":
        tools = _need_tools(evidence)
        want = [str(t) for t in (check.get("tools") or [])]
        ok = tools == want if check.get("mode") == "exact" else _is_subsequence(want, tools)
        return ("pass" if ok else "fail"), f"observed sequence {json.dumps(tools)}"
    if kind == "tool_set":
        names = _need_tools(evidence)
        allowed = set(check.get("allowed") or [])
        forbidden = set(check.get("forbidden") or [])
        bad = [t for t in names if (allowed and t not in allowed) or t in forbidden]
        return ("pass" if not bad else "fail"), f"disallowed tools {json.dumps(bad)}"
    if kind == "reference_trajectory":
        names = _need_tools(evidence)
        want = evidence["expected_tools"]
        if not want:
            raise Unusable("REFERENCE_MISSING", "no expected_trajectory reference for this case")
        ok = names == want if check.get("mode") == "exact" else set(want) <= set(names)
        return ("pass" if ok else "fail"), (
            f"expected {json.dumps(want)}, observed {json.dumps(names)}")
    if kind == "non_empty_output":
        # N17-obs398: "the agent answered something" — an empty / whitespace-only answer, or
        # none recorded, FAILS (it is the very thing this rule checks, not missing evidence)
        output = evidence["output"]
        length = len(output.strip()) if isinstance(output, str) else 0
        need = check.get("min") or 1
        ok = length >= need
        return ("pass" if ok else "fail"), f"final output, {length} non-blank character(s)"
    if kind in ("output_contains", "output_not_contains", "output_exact", "reference_response"):
        cs = bool(check.get("case_sensitive"))
        output = _need_output(evidence)
        if kind == "reference_response":
            expected = evidence["expected_output"]
            if not isinstance(expected, str) or not expected.strip():
                raise Unusable("REFERENCE_MISSING", "no expected_output reference for this case")
            text = expected
        else:
            text = str(check.get("text") or "")
        needle, hay = _norm(text, cs), _norm(output, cs)
        if kind == "output_exact":
            ok = hay.strip() == needle.strip()
        elif kind == "output_not_contains":
            ok = needle not in hay
        else:
            ok = needle in hay
        return ("pass" if ok else "fail"), f"final output, length {len(output)}"
    raise Unusable("BAD_RULE", f"unknown check type {kind!r}")


def extract_evidence(evaluation_input):
    if not isinstance(evaluation_input, dict):
        raise Unusable("BAD_INPUT", "evaluationInput must be an object")
    expected_output = evaluation_input.get("expected_output")
    if expected_output is not None and not isinstance(expected_output, str):
        expected_output = _text(expected_output)
    return {
        "output": _text(evaluation_input.get("actual_output")),
        "tools": _tools(evaluation_input.get("actual_trajectory"), "actual_trajectory"),
        "expected_tools": _tools(evaluation_input.get("expected_trajectory"),
                                 "expected_trajectory"),
        "expected_output": expected_output,
    }


def evaluate(rules, event):
    """Pure evaluation of one event against one evaluator's rules (testable)."""
    try:
        if not isinstance(event, dict):
            return _error("BAD_SCHEMA", "the event must be an object")
        version = event.get("schemaVersion")
        if not isinstance(version, str) or version != SCHEMA_VERSION:
            return _error("BAD_SCHEMA", f"schemaVersion must be the string {SCHEMA_VERSION!r}")
        checks = rules.get("checks") if isinstance(rules, dict) else None
        if not isinstance(checks, list) or not checks or not all(
                isinstance(c, dict) for c in checks):
            return _error("BAD_RULE", "no checks packaged for this evaluator")
        evidence = extract_evidence(event.get("evaluationInput"))
        results = []
        for check in checks:
            outcome, detail = run_check(check, evidence)
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
    """Entry point: resolve the packaged evaluator the event names, then evaluate."""
    try:
        rules = load_rules()
        name = (event.get("evaluatorName") or event.get("evaluator")
                if isinstance(event, dict) else None)
        evaluator = resolve_evaluator(rules, name)
    except Unusable as exc:
        return _error(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error("INTERNAL", f"{type(exc).__name__}: {exc}")
    return evaluate(evaluator, event)
