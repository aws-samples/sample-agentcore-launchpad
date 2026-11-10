"""Tool input-schema normalizer for strict providers (N17-obs302;
docs/cn/interfaces/model-credential.md §Tool schemas).

Kimi K3 rejects a ConverseStream (validationException "The task request was rejected by
the target") whose tool `inputSchema` carries `oneOf` it cannot prove disjoint, or
`format: binary|byte` — strands_tools' code_interpreter has both (pydantic's discriminated
`action` union, `FileContent.blob: bytes`). The rewrite only loosens what the model is
TOLD: `oneOf` → `anyOf`, `discriminator` dropped, `format` binary/byte dropped (date-time,
uri … kept). The tools still validate their own input, so it is model-agnostic and
applied to every tool spec every runtime hands its model.

Self-contained (stdlib only, no imports at all): the hub imports it and every generated
runtime inlines this text into its model helpers (strands_agent/main.py.tmpl).
Pure, idempotent, and never raises — an unexpected shape is returned unchanged.
"""

_LAUNCHPAD_DROPPED_FORMATS = frozenset({"binary", "byte"})
# keywords whose value maps NAMES to subschemas — the names are data, never keywords
_LAUNCHPAD_SCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
# keywords whose value is instance data, not a schema — copied verbatim
_LAUNCHPAD_SCHEMA_DATA = frozenset({"enum", "const", "default", "examples", "required"})


def _launchpad_normalize_schema_node(node: object) -> object:
    if isinstance(node, list):
        return [_launchpad_normalize_schema_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict = {}
    for key, value in node.items():
        if key == "discriminator":
            continue
        if key == "format" and isinstance(value, str) and value in _LAUNCHPAD_DROPPED_FORMATS:
            continue
        if key in _LAUNCHPAD_SCHEMA_DATA:
            out[key] = value
        elif key in _LAUNCHPAD_SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _launchpad_normalize_schema_node(sub) for name, sub in value.items()}
        else:
            out[key] = _launchpad_normalize_schema_node(value)
    branches = out.pop("oneOf", None)
    if branches is not None:
        if "anyOf" in out:
            # both on one node: keep the existing anyOf, AND the former oneOf in
            out["allOf"] = [*(out.get("allOf") or []), {"anyOf": branches}]
        else:
            out["anyOf"] = branches
    return out


def launchpad_normalize_tool_schema(schema: object) -> object:
    """`schema` with oneOf → anyOf, no `discriminator`, no binary/byte `format` — a new
    object (the input is never mutated); the input itself on any unexpected failure."""
    try:
        return _launchpad_normalize_schema_node(schema)
    except Exception:  # noqa: BLE001 - a normalizer must never break a model call
        return schema


def launchpad_normalize_tool_specs(tool_specs: object) -> object:
    """Strands `ToolSpec`s with each `inputSchema.json` normalized; a spec of another shape
    (and a non-list) passes through untouched."""
    if not isinstance(tool_specs, list):
        return tool_specs
    normalized = []
    for spec in tool_specs:
        try:
            wrapper = spec["inputSchema"]
            schema = wrapper["json"]
        except Exception:  # noqa: BLE001 - not the ToolSpec shape: leave it
            normalized.append(spec)
            continue
        normalized.append(
            {**spec, "inputSchema": {**wrapper, "json": launchpad_normalize_tool_schema(schema)}}
        )
    return normalized


def launchpad_normalize_model_tool_schemas(model: object) -> object:
    """`model` with its `stream` (the one call every Agent turn and Bedrock's
    `structured_output` make with `tool_specs`) normalizing the specs first. Idempotent:
    a model already wrapped is returned as is; one that refuses the attribute is returned
    unwrapped."""
    if getattr(model, "_launchpad_tool_schemas_normalized", False):
        return model
    original = getattr(model, "stream", None)
    if not callable(original):
        return model

    def stream(messages, tool_specs=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        return original(messages, launchpad_normalize_tool_specs(tool_specs), *args, **kwargs)

    try:
        model.stream = stream  # type: ignore[attr-defined]
        model._launchpad_tool_schemas_normalized = True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - e.g. __slots__: run unwrapped rather than fail
        return model
    return model
