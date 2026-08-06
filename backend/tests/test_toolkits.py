"""Platform toolkit registry: derived names/descriptions and their Strands parity."""

import types

import pytest
import strands

from app.templates.toolkits import (
    TOOLKITS,
    toolkit_default_system_prompt,
    toolkit_source,
    toolkit_tool_descriptions,
    toolkit_tool_names,
)

HR_TOOL_NAMES = [
    "get_pto_balance",
    "submit_pto_request",
    "lookup_hr_policy",
    "get_benefits_summary",
    "get_pay_stub",
]


def test_hr_assistant_declares_exactly_the_expected_tools():
    """A malformed toolkit source must fail here, not ship an empty tool list."""
    assert toolkit_tool_names(["hr_assistant"]) == HR_TOOL_NAMES
    assert sorted(toolkit_tool_descriptions(["hr_assistant"])) == sorted(HR_TOOL_NAMES)


def test_empty_and_unknown_selections_are_inert():
    assert toolkit_source([]) == ""
    assert toolkit_tool_names([]) == []
    assert toolkit_tool_descriptions([]) == {}
    assert toolkit_source(["not_a_toolkit"]) == ""
    assert toolkit_tool_names(["not_a_toolkit"]) == []
    assert toolkit_default_system_prompt("not_a_toolkit") == ""


def test_duplicate_selection_inlines_the_source_once():
    """AgentSpec rejects duplicates, but the renderer must not depend on that."""
    once = toolkit_source(["hr_assistant"])
    assert toolkit_source(["hr_assistant", "hr_assistant"]) == once
    assert toolkit_tool_names(["hr_assistant", "hr_assistant"]) == HR_TOOL_NAMES


def test_source_carries_no_imports_of_its_own():
    """It is inlined into the template, which already imports `tool`."""
    source = toolkit_source(["hr_assistant"])
    for line in source.splitlines():
        assert not line.startswith(("import ", "from "))


def test_derived_descriptions_match_what_strands_derives():
    """The registry claims to apply Strands' own docstring rule — prove it.

    Executes the toolkit source with the real `@tool` decorator and compares each
    resulting `tool_spec["description"]` against the ast-derived value. Without
    this, a change to either side is a silent divergence between what the model
    sees and what the console reports as the current description.
    """
    namespace: dict = {"tool": strands.tool}
    exec(compile(toolkit_source(["hr_assistant"]), "hr_assistant", "exec"), namespace)
    derived = toolkit_tool_descriptions(["hr_assistant"])
    for name, description in derived.items():
        assert namespace[name].tool_spec["description"] == description
    # Args guidance stays in the parameter schema, not the description
    balance_schema = namespace["get_pto_balance"].tool_spec["inputSchema"]["json"]
    assert "EMP-001" in balance_schema["properties"]["employee_id"]["description"]
    assert "Args:" not in derived["get_pto_balance"]


def test_hr_assistant_default_prompt_is_not_pre_hardened():
    """The generic prompt IS the experiment subject — hardening it removes what
    a config-bundle recommendation is supposed to repair."""
    prompt = toolkit_default_system_prompt("hr_assistant")
    assert "HR Assistant for Acme Corp" in prompt
    assert "Always use the available tools" in prompt
    assert "Do not make up" in prompt
    # no error-handling instruction: that omission is what produces the defects
    lowered = prompt.lower()
    assert "error" not in lowered
    assert "if a tool" not in lowered


def test_every_registered_toolkit_has_a_readable_source_and_prompt():
    for name, definition in TOOLKITS.items():
        assert definition.path.is_file()
        assert toolkit_tool_names([name])
        assert definition.default_system_prompt.strip()


@pytest.fixture
def hr_tools():
    """The toolkit's functions, callable, with a pass-through `tool` decorator."""
    namespace: dict = {"tool": lambda fn: fn}
    exec(compile(toolkit_source(["hr_assistant"]), "hr_assistant", "exec"), namespace)
    return types.SimpleNamespace(**{name: namespace[name] for name in HR_TOOL_NAMES})


def test_seed_data_is_deterministic(hr_tools):
    assert hr_tools.get_pto_balance("EMP-001") == {
        "employee_id": "EMP-001",
        "total_days": 15,
        "used_days": 5,
        "remaining_days": 10,
    }
    assert hr_tools.get_pto_balance("EMP-002")["remaining_days"] == 3
    assert hr_tools.get_pto_balance("EMP-042")["remaining_days"] == 13


def test_pto_request_id_is_derived_from_the_request_not_a_counter(hr_tools):
    """Deviation from the upstream sample, which increments a module counter and
    so returns a different id for the same prompt on every call."""
    args = ("EMP-001", "2026-06-01", "2026-06-05")
    first = hr_tools.submit_pto_request(*args)
    assert hr_tools.submit_pto_request(*args) == first
    assert first["status"] == "APPROVED"
    assert first["request_id"] == f"PTO-2026-{int(first['request_id'][-3:]):03d}"
    assert len(first["request_id"]) == len("PTO-2026-000")
    assert hr_tools.submit_pto_request("EMP-002", *args[1:])["request_id"] != (
        first["request_id"]
    )
    # the reason is echoed, and its default is documented
    assert hr_tools.submit_pto_request(*args)["reason"] == "Personal time off"


def test_lookups_normalize_topics_and_report_unknown_ones(hr_tools):
    assert "accrue 15 days" in hr_tools.lookup_hr_policy("pto")["policy_text"]
    # the key is normalized; the echoed topic stays whatever the caller passed
    assert (
        hr_tools.lookup_hr_policy("Remote Work")["policy_text"]
        == hr_tools.lookup_hr_policy("remote-work")["policy_text"]
    )
    missing = hr_tools.lookup_hr_policy("dress_code")
    assert "not found" in missing["error"] and "pto" in missing["error"]


def test_benefits_and_pay_stubs_cover_success_and_error(hr_tools):
    assert "matches 100%" in hr_tools.get_benefits_summary("401k")["summary"]
    assert "error" in hr_tools.get_benefits_summary("pet_insurance")
    stub = hr_tools.get_pay_stub("EMP-001", "2026-01")
    assert stub["net_pay"] == 5362.50 and stub["period"] == "January 2026"
    assert "error" in hr_tools.get_pay_stub("EMP-002", "2026-01")
