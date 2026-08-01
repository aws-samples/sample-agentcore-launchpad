"""Strands agent template renderer.

Placeholders use ``__LAUNCHPAD_*__`` markers instead of str.format so the
template stays brace-safe Python. Rendered output must always compile.
"""

from pathlib import Path

from app.schemas.agent import AgentSpec
from app.templates.kb_support import (
    KB_DEEP_TOOL_NAME,
    KB_TOOL_NAME,
    kb_deep_tool_description,
    kb_prompt_section,
    kb_tool_description,
    mounted_kbs,
    render_direct_kb_source,
)

TEMPLATE_DIR = Path(__file__).parent


def render_main_py(spec: AgentSpec) -> str:
    source = (TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
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
    system_prompt = spec.system_prompt + kb_prompt_section(kbs)
    return (
        source.replace("__LAUNCHPAD_AGENT_NAME__", spec.name)
        .replace("__LAUNCHPAD_MODEL_ID__", spec.model_id)
        .replace("__LAUNCHPAD_MODEL_SOURCE__", spec.model_source)
        .replace("__LAUNCHPAD_SYSTEM_PROMPT__", repr(system_prompt))
        .replace("__LAUNCHPAD_DIRECT_KB_SOURCE__", render_direct_kb_source(kbs))
        .replace("__LAUNCHPAD_KB_TOOL_DESCRIPTIONS__", repr(kb_descriptions))
        .replace(
            "__LAUNCHPAD_TOOL_DESCRIPTION_OVERRIDES__",
            repr(spec.tool_description_overrides),
        )
    )


def base_requirements() -> list[str]:
    lines = (TEMPLATE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]
