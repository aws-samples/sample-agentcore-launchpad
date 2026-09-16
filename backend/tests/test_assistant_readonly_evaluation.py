"""Read-only agents may retrieve KB evidence and load Skills through tools."""

from copy import deepcopy

import pytest

from app.assistant import evaluation_plan as plans
from app.assistant import proposal as proposals

CAPABILITIES = (
    {"knowledge_bases": ["KB-EARNINGS"]},
    {"skills": ["earnings-analysis"]},
    {"tools": ["mcp:reports"]},
)
ZERO_CALL_RULES = (
    {"id": "zero", "type": "tool_count", "max": 0},
    {"id": "zero", "type": "tool_count", "tool": None, "min": 0, "max": 0},
    {"id": "zero", "type": "tool_count", "tool": "", "max": 0},
    {"id": "zero", "type": "tool_sequence", "mode": "exact", "tools": []},
    {"id": "zero", "type": "tool_sequence", "mode": "exact"},
)
READONLY_RULES = (
    {"id": "no-email", "type": "tool_count", "tool": "send_email", "max": 0},
    {"id": "no-writes", "type": "tool_set", "forbidden": ["send_email", "update_report"]},
    {"id": "bounded", "type": "tool_count", "min": 0, "max": 5},
    {"id": "retrieval", "type": "tool_count", "min": 1},
    {"id": "retrieval", "type": "tool_sequence", "mode": "exact", "tools": ["Retrieve"]},
)


def _source(rule, capabilities):
    return {
        "name": "earnings-assistant", "system_prompt": "Read and explain published reports.",
        "tools": [], "skills": [], "knowledge_bases": [], **capabilities,
        # Empty expected_tools is the existing default, not a zero-call requirement.
        "golden_tests": [{"id": "facts", "input": "What was revenue?", "expected_tools": []}],
        "evaluation_plan": {
            "evaluators": [{
                "kind": "code", "key": "readonly", "name": "check_readonly",
                "title": "Business read-only behavior", "level": "SESSION",
                "rules": {"checks": [rule]},
            }],
            "scenarios": [{
                "scenario_id": "facts", "golden_test_id": "facts",
                "turns": [{"input": "What was revenue?"}],
                "assertions": ["Do not claim to have changed or sent a report."],
            }],
        },
    }


def _validate_plan(source):
    digest = proposals.canonical_hash(source)
    raw = plans.draft_plan(source, revision=1, content_hash=digest, agent_name=source["name"])
    return plans.validate_plan(raw, source, revision=1, content_hash=digest)


@pytest.mark.parametrize("capabilities", CAPABILITIES)
@pytest.mark.parametrize("rule", ZERO_CALL_RULES)
def test_zero_tool_rules_conflict_at_proposal_and_plan_validation(capabilities, rule):
    source = _source(rule, capabilities)
    before = deepcopy(source)
    for parsed, errors in (proposals.parse_content(source), _validate_plan(source)):
        assert parsed is None
        message = "; ".join(errors)
        assert "evaluators.readonly.rules.zero" in message
        assert "Read-only does not mean zero tool calls" in message
        assert "business-write tools" in message and "scenario assertions" in message
        for field, ids in capabilities.items():
            assert field in message and ids[0] in message
    assert source == before


@pytest.mark.parametrize("capabilities", ({}, *CAPABILITIES))
@pytest.mark.parametrize("rule", READONLY_RULES)
def test_named_write_bans_and_positive_call_rules_remain_valid(capabilities, rule):
    source = _source(rule, capabilities)
    for parsed, errors in (proposals.parse_content(source), _validate_plan(source)):
        assert parsed is not None, errors
        assert errors == []


@pytest.mark.parametrize("rule", ZERO_CALL_RULES)
@pytest.mark.parametrize("explicit_empty", [True, False])
def test_truly_tool_free_proposals_accept_zero_call_rules(rule, explicit_empty):
    source = _source(rule, {})
    if not explicit_empty:
        for field in ("tools", "skills", "knowledge_bases"):
            source.pop(field)
    for parsed, errors in (proposals.parse_content(source), _validate_plan(source)):
        assert parsed is not None, errors
        assert errors == []


@pytest.mark.parametrize("field", ["expected_trajectory", "expected_tools"])
def test_explicit_expected_tool_use_conflicts_even_without_catalog_capabilities(field):
    source = _source(ZERO_CALL_RULES[0], {})
    target = (source["golden_tests"][0] if field == "expected_tools"
              else source["evaluation_plan"]["scenarios"][0])
    target[field] = ["Retrieve"]
    for parsed, errors in (proposals.parse_content(source), _validate_plan(source)):
        assert parsed is None
        assert any(field in error and "Retrieve" in error for error in errors)


def test_blocked_golden_tools_do_not_forbid_a_tool_free_scenario():
    source = _source(ZERO_CALL_RULES[0], {})
    source["golden_tests"].append({
        "id": "future-search", "input": "Search the report", "expected_tools": ["Retrieve"],
    })
    source["evaluation_plan"]["blocked_golden_tests"] = [
        {"golden_test_id": "future-search", "reason": "Requires a future retrieval capability."},
    ]
    for parsed, errors in (proposals.parse_content(source), _validate_plan(source)):
        assert parsed is not None, errors


def test_evaluator_display_name_does_not_determine_semantics():
    source = _source(READONLY_RULES[0], CAPABILITIES[0])
    source["evaluation_plan"]["evaluators"][0].update(
        name="NoToolCalls", title="NoToolCalls", key="NoToolCalls",
    )
    for parsed, errors in (proposals.parse_content(source), _validate_plan(source)):
        assert parsed is not None, errors
