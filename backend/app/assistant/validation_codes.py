"""Stable codes for proposal / evaluation-plan validation errors (N17-obs295 / obs296).

Validation errors are operator-readable English sentences (N-ARCH-obs2 / obs3) and stay
that way on every surface — the ledger, the model-facing rejection note, the API's
``validation_errors[]``. This module classifies a sentence into ``{code, params,
message}`` so that

* the console renders the known rules in the member's language (zh-CN / en) with the
  English sentence kept as the expandable detail (``validation_error_details[]`` on a
  proposal / plan payload, ``errors[]`` on a transcript notice row — N17-obs296), and
* the assistant service can tell a proposal that failed ONLY the structural
  evaluation-plan routing rules (``_routing_errors`` in ``evaluation_plan.py``) — the
  class it corrects automatically with one internal model call (N17-obs295,
  ``AUTOCORRECT_CODES``).

The patterns mirror the f-strings of ``evaluation_plan._routing_errors``;
``tests/test_assistant_validation_codes.py`` builds the real messages through that function and
fails when a sentence drifts away from its pattern. A sentence no pattern knows keeps the
generic code ``proposal.validation_error`` (the console shows it verbatim).
"""

from __future__ import annotations

import re
from typing import Any

GENERIC_CODE = "proposal.validation_error"
REFERENCE_MISSING = "evaluation_plan.reference_missing"
TRACE_TURN_MISSING_RESPONSE = "evaluation_plan.trace_turn_missing_response"
EVALUATOR_PARTIAL_ROUTING = "evaluation_plan.evaluator_partial_routing"

# The prefixes a plan rule carries: none on the evaluation-plan surface, ``evaluation_plan.``
# on a proposal's seed, ``evaluation_plan (as drafted for review): `` on the platform draft.
_PREFIX = r"(?:evaluation_plan\.|evaluation_plan \(as drafted for review\): )?"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (REFERENCE_MISSING, re.compile(
        _PREFIX + r"evaluators\.(?P<evaluator>[^:\s]+): scenario '(?P<scenario>[^']*)' has "
        r"no (?P<fields>[a-z_/]+) — a reference-driven evaluator is applied to every "
        r"session, so every scenario must carry it$")),
    (TRACE_TURN_MISSING_RESPONSE, re.compile(
        _PREFIX + r"evaluators\.(?P<evaluator>[^:\s]+): scenario '(?P<scenario>[^']*)' has "
        r"a turn without expected_response — a TRACE reference evaluator scores every "
        r"turn$")),
    (EVALUATOR_PARTIAL_ROUTING, re.compile(
        _PREFIX + r"evaluators\.(?P<evaluator>[^:\s]+): targets only (?P<golden_tests>\[.*\]) "
        r"but a dataset run applies every evaluator to every session — the platform cannot "
        r"route an AWS evaluator per golden test; map it to all golden tests "
        r"\(golden_test_ids: \[\]\) or block the golden tests it cannot cover$")),
)

# Structural evaluation-plan rules: the proposal is otherwise complete and the fix is
# mechanical (add the reference to every scenario, or drop / replace / re-scope the
# evaluator). Only a proposal whose EVERY error carries one of these codes is offered the
# automatic correction call — never a policy, catalog, shape or reference error.
AUTOCORRECT_CODES = frozenset({REFERENCE_MISSING, TRACE_TURN_MISSING_RESPONSE,
                               EVALUATOR_PARTIAL_ROUTING})


def classify(message: str) -> dict[str, Any]:
    """``{code, params, message}`` for one validation sentence (``message`` verbatim)."""
    text = str(message)
    for code, pattern in _PATTERNS:
        m = pattern.match(text)
        if m is None:
            continue
        params: dict[str, Any] = dict(m.groupdict())
        if "fields" in params:
            params["fields"] = params["fields"].split("/")
        if "golden_tests" in params:
            params["golden_tests"] = re.findall(r"'([^']*)'", params["golden_tests"])
        return {"code": code, "params": params, "message": text}
    return {"code": GENERIC_CODE, "params": {}, "message": text}


def details(errors: list[str] | None) -> list[dict[str, Any]]:
    return [classify(e) for e in errors or []]


def autocorrectable(errors: list[str] | None) -> bool:
    """True when there is at least one error and every one is a structural plan rule."""
    errors = list(errors or [])
    return bool(errors) and all(classify(e)["code"] in AUTOCORRECT_CODES for e in errors)


def note_lines(text: str | None) -> list[str]:
    """The ``- `` error lines of a transcript notice (``rejection_note`` / the
    auto-correction notice) — the same lines the console has always shown."""
    return [line[2:] for line in (text or "").split("\n") if line.startswith("- ")
            and not line.startswith("- … (more errors omitted)")]
