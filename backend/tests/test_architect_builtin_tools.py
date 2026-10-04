"""Architect proposals may mount the AgentCore Code Interpreter (``builtin_tools``)."""

from pathlib import Path

from app.assistant import proposal as proposals
from app.assistant import service
from tests.test_assistant_readonly_evaluation import _source, _validate_plan

SKILL = (Path(__file__).resolve().parents[1]
         / "app/system_agents/skills/aws-agent-solution-architect/SKILL.md")


def test_builtin_tools_default_to_none_and_map_to_a_builtin_tool_ref():
    plain, errors = proposals.parse_content({"name": "calc-agent", "system_prompt": "x"})
    assert errors == [] and plain.builtin_tools == []
    chosen, errors = proposals.parse_content(
        {"name": "calc-agent", "system_prompt": "x", "builtin_tools": ["code-interpreter"]})
    assert errors == []
    spec = proposals.to_agent_spec(chosen, {})
    assert [(t.type, t.name) for t in spec.tools] == [("builtin", "code-interpreter")]
    bindings = proposals.resource_bindings(chosen, {})
    assert {"type": "builtin", "name": "code-interpreter", "config": {}} in bindings["tools"]
    assert "tools" in proposals.binding_diff(proposals.resource_bindings(plain, {}), bindings)


def test_only_the_code_interpreter_and_no_repeats_are_accepted():
    parsed, errors = proposals.parse_content(
        {"name": "calc-agent", "system_prompt": "x", "builtin_tools": ["browser"]})
    assert parsed is None and errors
    parsed, errors = proposals.parse_content(
        {"name": "calc-agent", "system_prompt": "x",
         "builtin_tools": ["code-interpreter", "code-interpreter"]})
    assert parsed is None and errors


def test_a_rule_may_require_code_interpreter_only_when_it_is_mounted():
    from app.assistant import evaluation_plan as plans
    from app.assistant import tool_catalog

    rule = [plans.CodeEvaluator(
        kind="code", key="calc", name="uses_calculator", title="Calculator", level="SESSION",
        rules=plans.CodeRules(checks=[plans.CodeCheck.model_validate(
            {"id": "calc", "type": "tool_count", "tool": "code_interpreter", "min": 1})]),
    )]
    content = {"name": "calc-agent", "tools": [], "native_tools": []}
    errors = tool_catalog.rule_catalog_errors(rule, content, {})
    assert errors and "code_interpreter" in errors[0]
    content["builtin_tools"] = ["code-interpreter"]
    assert tool_catalog.rule_catalog_errors(rule, content, {}) == []


def test_a_zero_call_rule_conflicts_with_a_mounted_code_interpreter():
    source = _source({"id": "zero", "type": "tool_count", "max": 0}, {})
    source.update(native_tools=[], builtin_tools=["code-interpreter"])
    _parsed, errors = _validate_plan(source)
    assert any("builtin_tools" in error for error in errors)


def test_guidance_names_the_builtin_and_the_adversarial_flag():
    text = SKILL.read_text(encoding="utf-8")
    assert '`builtin_tools: ["code-interpreter"]`' in text
    assert "`adversarial: true`" in text
    assert "`builtin_tools`: optional list containing only `\"code-interpreter\"`" in (
        service.PROTOCOL_PREAMBLE)
    assert "`adversarial` — `true`" in service.PROTOCOL_PREAMBLE
