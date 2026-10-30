"""Strands agent template renderer.

Placeholders use ``__LAUNCHPAD_*__`` markers instead of str.format so the
template stays brace-safe Python. Rendered output must always compile.
"""

from pathlib import Path

from app.schemas.agent import (
    MIN_MODEL_MAX_TOKENS,
    AgentSpec,
    allowed_model_ids,
    catalog_version,
    default_runtime_max_tokens,
    inference_param_support,
    model_descriptor,
    model_provider_table,
)
from app.templates.a2a_support import (
    a2a_prompt_section,
    a2a_tool_descriptions,
    mounted_agents,
    render_a2a_tools_source,
)
from app.templates.attachment_support import render_attachment_source
from app.templates.gateway_support import render_gateway_source, uses_gateway
from app.templates.identity_support import render_identity_source, uses_identity
from app.templates.kb_support import (
    KB_DEEP_TOOL_NAME,
    KB_TOOL_NAME,
    kb_deep_tool_description,
    kb_prompt_section,
    kb_tool_description,
    mounted_kbs,
    render_direct_kb_source,
)
from app.templates.memory_support import render_memory_source
from app.templates.principal_support import render_principal_placeholders
from app.templates.toolkits import (
    toolkit_source,
    toolkit_tool_descriptions,
    toolkit_tool_names,
)

TEMPLATE_DIR = Path(__file__).parent
TOOL_SCHEMA_MODULE = TEMPLATE_DIR.parent / "tool_schema.py"


def rendered_max_tokens(spec: AgentSpec) -> int:
    """Per-call output ceiling baked into the runtime: the spec's explicit value
    (harness-only today) or the model-aware platform default (N10-bug4: GLM/Kimi get
    `DEFAULT_REASONING_MAX_TOKENS`), never under the GLM reasoning floor (spec C3)."""
    ceiling = spec.max_tokens or default_runtime_max_tokens(spec.model_id)
    return max(ceiling, MIN_MODEL_MAX_TOKENS)


def rendered_model_inference_params() -> str:
    """Python literal of the catalog's inference-parameter support (N12-bug11), inlined
    into every generated runtime's model block — one line per catalog model, no runtime
    import of the hub package."""
    lines = ["{"]
    for model_id in allowed_model_ids():
        lines.append(f"    {model_id!r}: {inference_param_support(model_id)!r},")
    lines.append("}")
    return "\n".join(lines)


def rendered_model_providers() -> str:
    """Python literal of the catalog's provider table (N-R6: `schemas.agent.
    model_provider_table`), inlined into every generated runtime's model block like
    `MODEL_INFERENCE_PARAMS` — one line per catalog model, no runtime import of the hub."""
    lines = ["{"]
    for model_id, row in model_provider_table().items():
        lines.append(f"    {model_id!r}: {row!r},")
    lines.append("}")
    return "\n".join(lines)


def rendered_model_catalog_version() -> str:
    """Python literal of the published catalog's version (D21: the registry journal id; 0
    only before the seed) — `MODEL_CATALOG_VERSION` in every generated runtime."""
    return repr(int(catalog_version()))


def rendered_model_descriptor(model_id: str) -> str:
    """Python literal of `model_id`'s resolved descriptor (`schemas.agent.model_descriptor`),
    `None` for an id outside the catalog — `MODEL_DESCRIPTOR` in every generated runtime, the
    row `_model_provider` reads before the inlined table (D21)."""
    return repr(model_descriptor(model_id))


def rendered_tool_schema_source() -> str:
    """`app/templates/tool_schema.py` verbatim — the tool input-schema normalizer the hub
    imports, inlined into every generated runtime's model helpers (N17-obs302)."""
    return TOOL_SCHEMA_MODULE.read_text(encoding="utf-8").strip("\n")


def render_main_py(spec: AgentSpec, *, principal_carrier: bool = False) -> str:
    """The generated entrypoint. ``principal_carrier`` (stream A3, default off) inlines the
    principal-token carrier, its capture, the sequential tool executor and the transport's
    injection line. Off, every placeholder line is removed so the output is byte for byte
    what it was before. (The former runtime policy guard splice is gone — Global parity: the
    Gateway interceptor decides every tool call.)"""
    source = render_principal_placeholders(
        (TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8"), enabled=principal_carrier)
    kbs = mounted_kbs(spec)
    # Both KB tools land in DEFAULT_TOOL_DESCRIPTIONS so the config-bundle A/B
    # contract can tune each one; spec overrides still win (they merge after).
    kb_descriptions = (
        {
            KB_TOOL_NAME: kb_tool_description(kbs),
            KB_DEEP_TOOL_NAME: kb_deep_tool_description(kbs),
        }
        if kbs
        else {}
    )
    # mounted A2A agents (N11 r3-C): the prompt names them, the tool descriptions join the
    # config-bundle contract, and the client source is inlined (inert stub when none)
    peers = mounted_agents(spec)
    system_prompt = spec.system_prompt + kb_prompt_section(kbs) + a2a_prompt_section(peers)
    # Toolkit tools are registered by *name* (the functions are inlined just above
    # the assignment), so this renders as a Python list literal, not a repr.
    toolkit_tools = "[" + ", ".join(toolkit_tool_names(list(spec.toolkits))) + "]"
    return (
        source.replace("__LAUNCHPAD_ATTACHMENT_SOURCE__", render_attachment_source())
        .replace("__LAUNCHPAD_A2A_SOURCE__", render_a2a_tools_source(peers))
        .replace("__LAUNCHPAD_A2A_TOOL_DESCRIPTIONS__", repr(a2a_tool_descriptions(peers)))
        .replace("__LAUNCHPAD_SKILLS_ENABLED__", repr(bool(spec.skills)))
        .replace("__LAUNCHPAD_AGENT_NAME__", spec.name)
        .replace("__LAUNCHPAD_MODEL_ID__", spec.model_id)
        .replace("__LAUNCHPAD_MODEL_REGION__", spec.model_region)
        .replace("__LAUNCHPAD_MODEL_CREDENTIAL__", spec.model_credential)
        .replace("__LAUNCHPAD_MODEL_MAX_TOKENS__", repr(rendered_max_tokens(spec)))
        .replace("__LAUNCHPAD_MODEL_INFERENCE_PARAMS__", rendered_model_inference_params())
        .replace("__LAUNCHPAD_MODEL_PROVIDERS__", rendered_model_providers())
        .replace("__LAUNCHPAD_MODEL_CATALOG_VERSION__", rendered_model_catalog_version())
        .replace("__LAUNCHPAD_MODEL_DESCRIPTOR__", rendered_model_descriptor(spec.model_id))
        .replace("__LAUNCHPAD_TOOL_SCHEMA_SOURCE__", rendered_tool_schema_source())
        .replace("__LAUNCHPAD_SYSTEM_PROMPT__", repr(system_prompt))
        # the memory library is env-driven (no-op without LAUNCHPAD_MEMORY_ID), so it
        # renders for every spec; the KB tools after it reuse its OpenSearch/Titan clients
        .replace("__LAUNCHPAD_MEMORY_SOURCE__", render_memory_source())
        .replace("__LAUNCHPAD_DIRECT_KB_SOURCE__", render_direct_kb_source(kbs))
        .replace("__LAUNCHPAD_KB_TOOL_DESCRIPTIONS__", repr(kb_descriptions))
        .replace("__LAUNCHPAD_TOOLKIT_SOURCE__", toolkit_source(list(spec.toolkits)))
        .replace("__LAUNCHPAD_GATEWAY_SOURCE__",
                 render_gateway_source(spec, principal_carrier=principal_carrier))
        .replace(
            "__LAUNCHPAD_GATEWAY_TOOLS_FN__",
            "gateway_tools" if uses_gateway(spec) else "lambda _stack: []",
        )
        .replace("__LAUNCHPAD_IDENTITY_SOURCE__", render_identity_source(spec))
        .replace(
            "__LAUNCHPAD_IDENTITY_TOOLS_FN__",
            "identity_tools"
            if uses_identity(spec)
            else 'lambda _stack, _session_id="", _force_reauth=(): []',
        )
        .replace(
            "__LAUNCHPAD_IDENTITY_NOTICES_FN__",
            "identity_drain_auth_notices" if uses_identity(spec) else "lambda: []",
        )
        .replace("__LAUNCHPAD_TOOLKIT_TOOLS__", toolkit_tools)
        .replace(
            "__LAUNCHPAD_TOOLKIT_TOOL_DESCRIPTIONS__",
            repr(toolkit_tool_descriptions(list(spec.toolkits))),
        )
        .replace(
            "__LAUNCHPAD_TOOL_DESCRIPTION_OVERRIDES__",
            repr(spec.tool_description_overrides),
        )
    )


def base_requirements() -> list[str]:
    lines = (TEMPLATE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]
