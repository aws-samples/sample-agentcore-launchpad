"""N17-obs398 — "the agent answered something" as a code rule: ``non_empty_output``.

The closest rule was ``output_contains`` with ``text: ""``, which passes whenever a final
output string exists — including an empty answer; ``EVIDENCE_INCOMPLETE`` fired only when no
output was recorded at all. ``non_empty_output`` fails on an empty / whitespace-only (or
missing) final answer and passes on any text of at least ``min`` characters after trimming
(default 1). Static handler, plan contract and the architect's rule list agree.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.assistant import evaluation_plan as plan_contract
from app.assistant import service as assistant_service
from app.assistant.lambda_runtime import handler


def _event(output):
    return {"schemaVersion": "1.0", "evaluator": "answered", "evaluationInput": {
        "input": "hi", "actual_output": output, "expected_output": None,
        "expected_assertion": None, "expected_trajectory": None, "actual_trajectory": []}}


def _rules(**check):
    return {"checks": [{"id": "answered", "type": "non_empty_output", **check}]}


@pytest.mark.parametrize(("output", "label"), [
    ("", "FAIL"), ("   \n\t", "FAIL"), (None, "FAIL"), ([{"text": " "}], "FAIL"),
    ("ok", "PASS"), ("  a  ", "PASS"), ([{"text": "an answer"}], "PASS"),
])
def test_empty_and_whitespace_fail_any_text_passes(output, label):
    out = handler.evaluate(_rules(), _event(output))
    assert out["label"] == label, out
    assert out["value"] == (1.0 if label == "PASS" else 0.0)


def test_min_counts_characters_after_trimming():
    assert handler.evaluate(_rules(min=5), _event("  abcd  "))["label"] == "FAIL"
    assert handler.evaluate(_rules(min=5), _event(" abcde "))["label"] == "PASS"


def test_the_plan_contract_accepts_the_check_and_validates_its_members():
    rules = plan_contract.CodeRules(**_rules())
    assert plan_contract._check_rules(rules) == []
    assert plan_contract._check_rules(plan_contract.CodeRules(**_rules(min=3))) == []
    assert any("non_empty_output takes no text" in e for e in plan_contract._check_rules(
        plan_contract.CodeRules(**_rules(text="x"))))
    assert any("min must be at least 1" in e for e in plan_contract._check_rules(
        plan_contract.CodeRules(**_rules(min=0))))
    with pytest.raises(ValidationError):
        plan_contract.CodeRules(**{"checks": [{"id": "x", "type": "nonempty"}]})


def test_handler_and_contract_list_the_same_check_types():
    assert set(handler.CHECK_TYPES) == set(plan_contract.CODE_CHECK_TYPES)
    assert "non_empty_output" in plan_contract.CODE_CHECK_TYPES


def test_the_architect_is_told_the_check_and_its_members():
    text = "\n".join(str(v) for v in vars(assistant_service).values() if isinstance(v, str))
    assert "`non_empty_output`" in text
