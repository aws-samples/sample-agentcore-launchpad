"""The creation-time capability declaration (round 16) — one source of truth, checked
against every place that used to carry its own copy.

* ``BUILTIN_RUNTIME_TOOLS`` (the Harness render map) keys == ``caps("harness").builtin_tools``
* the schema-level method constants (``KB_METHODS``, ``TOOL_AUTH_METHODS``,
  ``A2A_CONSUMER_METHODS``, ``SUPPORTED_ACTING_MODES``) agree with the declarations
* ``tests/fixtures/method_capabilities.json`` is the frozen wire form the frontend vitest
  reads (``frontend/src/lib/agent-spec.test.ts``): a declaration change must re-freeze it
  (``UPDATE_FIXTURES=1 uv run pytest tests/test_method_capabilities.py``) so the review
  sees the domain move.
"""

from __future__ import annotations

# CONTRACT: docs/cn/interfaces/testing.md §6.1 (one declaration, three consumers). Every
# assertion below is DIFFERENTIAL — two independent sources that must agree (render map vs
# declaration, schema constants vs declaration, frozen fixture vs declaration) — never a
# value copied from the implementation.
import json
import os
import re
from pathlib import Path

from app.deployer.pipeline import method_capabilities, registered_methods
from app.harness_tool_access import BUILTIN_RUNTIME_TOOLS, NATIVE_HARNESS_TOOLS
from app.schemas import agent as schema

FIXTURE = Path(__file__).parent / "fixtures" / "method_capabilities.json"


def declared() -> dict[str, dict]:
    return {name: method_capabilities(name).as_dict() for name in registered_methods()}


def test_the_four_methods_are_registered():
    assert registered_methods() == ["byoc", "harness", "studio", "zip_runtime"]


def test_harness_builtin_render_map_equals_the_declaration():
    caps = method_capabilities("harness")
    assert set(BUILTIN_RUNTIME_TOOLS) == caps.builtin_tools
    assert set(NATIVE_HARNESS_TOOLS) == caps.native_tools


def test_schema_method_constants_agree_with_the_declarations():
    by_name = {name: method_capabilities(name) for name in registered_methods()}
    assert {n for n, c in by_name.items() if c.knowledge_bases} == set(schema.KB_METHODS)
    assert {n for n, c in by_name.items() if c.tool_auth_modes} == set(schema.TOOL_AUTH_METHODS)
    assert ({n for n, c in by_name.items() if "a2a" in c.tool_types}
            == set(schema.A2A_CONSUMER_METHODS))
    modes = set().union(*(c.tool_auth_modes for c in by_name.values()))
    assert modes == set(schema.SUPPORTED_ACTING_MODES)
    assert {n for n, c in by_name.items() if "a2a" in c.protocols} == {"zip_runtime"}
    assert {n for n, c in by_name.items() if c.byoc_kinds} == {"byoc"}
    assert all("artifacts_bucket" in c.requires for c in by_name.values())


def test_fixture_is_frozen():
    current = declared()
    if os.environ.get("UPDATE_FIXTURES"):
        FIXTURE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    frozen = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert frozen == current, (
        "the method capability declaration changed — review the move, then re-freeze with "
        "`UPDATE_FIXTURES=1 uv run pytest tests/test_method_capabilities.py` (the frontend "
        "vitest consistency test reads the same file)"
    )


def test_reserved_placeholder_is_refused_everywhere_but_env():
    """CONTRACT docs/api.md "Creation-time validation": the renderers' marker is refused in
    every member string the templates may embed, naming the dotted path; `env` values are
    never rendered; look-alikes without the trailing underscore / in lower case pass."""
    import pytest

    base = {"name": "ph-agent", "method": "harness", "system_prompt": "p"}
    for path, extra in [
        ("system_prompt", {"system_prompt": "see __LAUNCHPAD_X__"}),
        ("tools[0].config.url", {"tools": [{"type": "mcp", "name": "d",
                                            "config": {"url": "https://x/__LAUNCHPAD_Z__"}}]}),
        ("tools[0].config.tools[0]", {"tools": [{"type": "gateway", "name": "g",
                                                 "config": {"record_id": "r", "gateway_id": "gw",
                                                            "tools": ["g___a__LAUNCHPAD_Z__"]}}]}),
        ("skills[0]", {"skills": ["s3://b/__LAUNCHPAD_Z__/"]}),
        ("knowledge_bases[0].name",
         {"knowledge_bases": [{"kb_id": "KB1", "name": "__LAUNCHPAD_"}]}),
        ("tool_description_overrides.__LAUNCHPAD_K",
         {"tool_description_overrides": {"__LAUNCHPAD_K": "v"}}),
    ]:
        with pytest.raises(ValueError, match=re.escape(path)):
            schema.AgentSpec(**{**base, **extra})
    lookalikes = "LAUNCHPAD LAUNCHPAD_MODE __LAUNCHPAD __launchpad_x__"
    ok = schema.AgentSpec(**{**base, "env": {"MARKER": "__LAUNCHPAD_ENV__"},
                             "system_prompt": lookalikes})
    assert ok.env["MARKER"] == "__LAUNCHPAD_ENV__"
