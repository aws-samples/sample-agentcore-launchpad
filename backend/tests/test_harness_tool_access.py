"""Selection must reach the wire without implicit native tools or widened selectors.

The CreateHarness/InvokeHarness integration tests (``build_create_params``, the
``services.agentcore.harness``/``invoke``/``chat`` harness dispatch) were removed
with the AWS Harness deploy/invoke paths (N1 Task 2) — there is no AWS Harness in
this build. The pure selector-derivation functions in ``app.harness_tool_access``
(kept — still consumed by the architect assistant and ``harness_convert.py``) keep
their coverage below.
"""

import pytest

from app.harness_tool_access import (
    BUILTIN_RUNTIME_TOOLS,
    NATIVE_HARNESS_TOOLS,
    qualify_remote_selectors,
    remap_tool_patterns,
    selected_native_tools,
    selected_tool_patterns,
)
from app.schemas.agent import AgentSpec


def spec(**overrides):
    return AgentSpec(**{
        "name": "selected-agent", "method": "harness", "system_prompt": "Help the user.",
        **overrides,
    })


@pytest.mark.parametrize("native", [
    ["unknown"], ["shell", "shell"], ["shell", "file_operations", "shell"], [None], "shell",
])
def test_invalid_native_selection_is_refused(native):
    with pytest.raises(ValueError):
        spec(native_tools=native)


@pytest.mark.parametrize("method", ["zip_runtime", "studio"])
def test_non_harness_methods_only_accept_empty_native_selection(method):
    assert spec(method=method).native_tools == []
    with pytest.raises(ValueError, match="harness method only"):
        spec(method=method, native_tools=["shell"])


@pytest.mark.parametrize(("overrides", "expected"), [
    ({}, set()),
    ({"native_tools": ["shell"]}, {"shell"}),
    ({"native_tools": ["file_operations"]}, {"file_operations"}),
    ({"native_tools": ["shell"], "allowed_tools": []}, set()),
    ({"native_tools": ["shell"], "allowed_tools": ["file_*"]}, {"file_operations"}),
    ({"allowed_tools": ["*"]}, set(NATIVE_HARNESS_TOOLS)),
    ({"allowed_tools": ["shell"]}, {"shell"}),
    ({"allowed_tools": ["@builtin"]}, set(NATIVE_HARNESS_TOOLS)),
    ({"allowed_tools": ["@builtin/shell"]}, {"shell"}),
    ({"allowed_tools": ["@builtin/file_*"]}, {"file_operations"}),
    ({"allowed_tools": ["@other/shell", "@docs", "skills"]}, set()),
])
def test_evaluator_native_projection_follows_effective_override(overrides, expected):
    assert selected_native_tools(overrides) == expected


def test_shared_selection_preserves_order_and_deduplicates_groups():
    assert selected_tool_patterns(
        [{"name": "docs"}, {"name": "docs"}, {"name": "other"}], True, ["shell"],
    ) == ["@docs", "@other", "skills", "shell"]


@pytest.mark.parametrize("name", [
    "builtin", "BUILTIN", "*", "built?n", "[b]uiltin", "@builtin", "builtin/shell",
    "builtin\n", "builtin\x00", " builtin", "docs tools", "docs\\*", "docs;builtin",
    "", None, "x" * 64,
])
def test_derived_group_names_cannot_admit_native_tools_or_selector_syntax(name):
    with pytest.raises(ValueError, match="cannot derive allowedTools"):
        selected_tool_patterns([{"name": name}], has_skills=False, native_tools=[])


def test_safe_group_names_fit_wire_bound_and_do_not_select_natives():
    allowed = selected_tool_patterns(
        [{"name": name} for name in ["aws-knowledge", "launchpad_kb_gw", "x" * 63]],
        has_skills=True, native_tools=[],
    )
    assert allowed == ["@aws-knowledge", "@launchpad_kb_gw", "@" + "x" * 63, "skills"]
    assert max(map(len, allowed)) == 64
    assert selected_native_tools({"allowed_tools": allowed}) == set()


def test_group_remapping_retains_narrow_suffix_without_wildcard_widening():
    names = {"deployed_alias": "launchpad_gw_user", "deployed_docs": "deployed_docs"}
    assert remap_tool_patterns(
        ["@deployed_alias/hr___lookup", "@deployed_*/read_*", "deployed_alias/lookup",
         "@untouched", "file_*", "*"],
        names,
    ) == [
        "@launchpad_gw_user/hr___lookup",
        "@launchpad_gw_user/read_*", "@deployed_docs/read_*",
        "launchpad_gw_user/lookup", "@untouched", "file_*", "*",
    ]


# ─── Gateway target scoping (a Registry record is ONE target of launchpad-gw) ─────

HR = {"type": "gateway", "name": "hr-database",
      "config": {"record_id": "rec-hr", "gateway_id": "shared-123",
                 "tools": ["hr-database___get_employee"]}}


@pytest.mark.parametrize("tools", [[], "hr___x", ["a b"], ["x", "x"], [""]])
def test_gateway_tool_lists_are_validated(tools):
    with pytest.raises(ValueError, match="config.tools"):
        spec(tools=[{**HR, "config": {**HR["config"], "tools": tools}}])


def test_remote_selectors_are_qualified_and_trimmed_within_the_limit():
    got = qualify_remote_selectors(
        ["@u/hr___get", "@u/hr___*", "@u", "@u/*", "@u/u_already", "@kb/x",
         "@u/hr___" + "y" * 80],
        ["u"],
    )
    assert got[:6] == ["@u/u_hr___get", "@u/u_hr___*", "@u", "@u/*", "@u/u_already", "@kb/x"]
    assert len(got[6]) == 64 and got[6].endswith("*") and got[6].startswith("@u/u_hr___y")


def test_builtin_render_map_is_the_harness_declaration():
    """N15-obs16 / round 16: the request-boundary guard and the `methods` catalog field read
    the method's declared domain, which must equal this render map's key set."""
    from app.deployer.pipeline import method_capabilities
    from app.routers.tools import builtin_methods

    assert method_capabilities("harness").builtin_tools == set(BUILTIN_RUNTIME_TOOLS)
    for name in BUILTIN_RUNTIME_TOOLS:  # every rendered builtin is offered for the Harness
        assert builtin_methods(name) == ["harness"]
    assert builtin_methods("not-a-builtin") == []
