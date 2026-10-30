"""N17-obs302: the tool input-schema normalizer (app/templates/tool_schema.py).

Kimi K3 rejected every turn of a Harness agent with the code-interpreter builtin: the
strands_tools schema carries pydantic's discriminated `oneOf` and `format: binary`. The
fixture is the exact inputSchema diag-f4 captured from the failing ConverseStream."""

import copy
import json
from pathlib import Path

import pytest

from app.templates.tool_schema import (
    launchpad_normalize_model_tool_schemas,
    launchpad_normalize_tool_schema,
    launchpad_normalize_tool_specs,
)

CAPTURED = Path(__file__).parent / "fixtures" / "obs302_code_interpreter_input_schema.json"


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _strict_violations(schema) -> list[str]:
    found = []
    for node in _walk(schema):
        if not isinstance(node, dict):
            continue
        if "oneOf" in node:
            found.append("oneOf")
        if "discriminator" in node:
            found.append("discriminator")
        if node.get("format") in ("binary", "byte"):
            found.append(f"format={node['format']}")
    return found


def test_captured_code_interpreter_schema_has_no_strict_provider_triggers():
    raw = json.loads(CAPTURED.read_text(encoding="utf-8"))
    assert {"oneOf", "discriminator", "format=binary"} <= set(_strict_violations(raw))
    before = copy.deepcopy(raw)
    out = launchpad_normalize_tool_schema(raw)
    assert _strict_violations(out) == []
    assert raw == before  # pure: the input is never mutated
    action = out["$defs"]["CodeInterpreterInput"]["properties"]["action"]
    assert [b["$ref"] for b in action["anyOf"]] == [
        f"#/$defs/{n}Action" for n in ("InitSession", "ListLocalSessions", "ExecuteCode",
                                       "ExecuteCommand", "ReadFiles", "ListFiles",
                                       "RemoveFiles", "WriteFiles")
    ]
    # every action's own `type` const and the required list survive (still disjoint)
    assert out["$defs"]["ExecuteCodeAction"]["properties"]["type"]["const"] == "executeCode"
    assert out["required"] == raw["required"]
    blob = out["$defs"]["FileContent"]["properties"]["blob"]
    assert "format" not in json.dumps(blob)


def test_normalizer_is_idempotent():
    raw = json.loads(CAPTURED.read_text(encoding="utf-8"))
    once = launchpad_normalize_tool_schema(raw)
    assert launchpad_normalize_tool_schema(once) == once


def test_other_formats_and_property_names_are_kept():
    schema = {
        "type": "object",
        "properties": {
            # property NAMES that collide with keywords are data, never rewritten
            "format": {"type": "string", "format": "date-time"},
            "discriminator": {"type": "string", "format": "uri"},
            "oneOf": {"type": "string", "format": "email"},
            "blob": {"type": "string", "format": "byte"},
        },
        "required": ["format", "discriminator"],
        "default": {"oneOf": 1, "format": "binary"},
        "enum": [{"discriminator": "x"}],
    }
    out = launchpad_normalize_tool_schema(schema)
    props = out["properties"]
    assert props["format"] == {"type": "string", "format": "date-time"}
    assert props["discriminator"] == {"type": "string", "format": "uri"}
    assert props["oneOf"] == {"type": "string", "format": "email"}
    assert props["blob"] == {"type": "string"}
    assert out["required"] == ["format", "discriminator"]
    assert out["default"] == {"oneOf": 1, "format": "binary"}
    assert out["enum"] == [{"discriminator": "x"}]


def test_oneof_next_to_an_existing_anyof_is_kept_as_an_allof_conjunct():
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}],
              "oneOf": [{"minLength": 1}, {"maxLength": 0}]}
    out = launchpad_normalize_tool_schema(schema)
    assert out == {"anyOf": [{"type": "string"}, {"type": "null"}],
                   "allOf": [{"anyOf": [{"minLength": 1}, {"maxLength": 0}]}]}
    assert launchpad_normalize_tool_schema(out) == out


@pytest.mark.parametrize("bad", [None, 3, "x", [1, None, {"oneOf": "not-a-list"}],
                                 {"properties": "nope"}, {"properties": {"a": 1}},
                                 {"oneOf": None}, {1: {"oneOf": []}}])
def test_malformed_input_does_not_raise(bad):
    launchpad_normalize_tool_schema(bad)
    launchpad_normalize_tool_specs(bad)
    launchpad_normalize_tool_specs([bad, {"inputSchema": bad}, {"inputSchema": {"json": bad}}])


def test_cyclic_input_is_returned_unchanged():
    cyclic: dict = {"type": "object"}
    cyclic["properties"] = {"self": cyclic}
    assert launchpad_normalize_tool_schema(cyclic) is cyclic


def test_tool_specs_normalize_only_input_schema_json():
    spec = {"name": "t", "description": "d",
            "inputSchema": {"json": {"oneOf": [{"type": "string", "format": "binary"}]}}}
    other = {"name": "raw", "inputSchema": {"oneOf": []}}  # not the ToolSpec shape
    out = launchpad_normalize_tool_specs([spec, other])
    assert out[0] == {"name": "t", "description": "d",
                      "inputSchema": {"json": {"anyOf": [{"type": "string"}]}}}
    assert out[1] is other
    assert spec["inputSchema"]["json"]["oneOf"]  # input untouched
    assert launchpad_normalize_tool_specs(None) is None


def test_model_wrapper_normalizes_stream_tool_specs_and_is_idempotent():
    seen = []

    class Model:
        def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            seen.append((messages, tool_specs, system_prompt, kwargs))
            return "chunks"

    model = Model()
    assert launchpad_normalize_model_tool_schemas(model) is model
    assert launchpad_normalize_model_tool_schemas(model) is model  # wrapped once
    specs = [{"name": "t", "inputSchema": {"json": {"oneOf": [{"type": "string"}]}}}]
    assert model.stream([], specs, "sys", tool_choice=None) == "chunks"
    assert model.stream(messages=[], tool_specs=specs) == "chunks"
    assert model.stream([]) == "chunks"
    assert seen[0][1] == [{"name": "t", "inputSchema": {"json": {"anyOf": [{"type": "string"}]}}}]
    assert seen[0][2:] == ("sys", {"tool_choice": None})
    assert seen[1][1] == seen[0][1]
    assert seen[2][1] is None


def test_model_wrapper_never_raises_on_odd_models():
    class Slotted:
        __slots__ = ()

        def stream(self, *a, **k):
            return None

    slotted = Slotted()
    assert launchpad_normalize_model_tool_schemas(slotted) is slotted
    assert launchpad_normalize_model_tool_schemas(None) is None
    assert launchpad_normalize_model_tool_schemas("model-id") == "model-id"
