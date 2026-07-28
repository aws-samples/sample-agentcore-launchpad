"""Strands agent template renderer.

Placeholders use ``__LAUNCHPAD_*__`` markers instead of str.format so the
template stays brace-safe Python. Rendered output must always compile.
"""

from pathlib import Path

from app.schemas.agent import AgentSpec
from app.templates.kb_support import (
    KB_RESULTS,
    KB_TOOL_NAME,
    kb_prompt_section,
    kb_tool_description,
    mounted_kbs,
)

TEMPLATE_DIR = Path(__file__).parent


def render_main_py(spec: AgentSpec) -> str:
    source = (TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    kbs = mounted_kbs(spec)
    # kb_search lands in DEFAULT_TOOL_DESCRIPTIONS so the config-bundle A/B
    # contract can tune it; spec overrides still win (they merge after).
    kb_description = {KB_TOOL_NAME: kb_tool_description(kbs)} if kbs else {}
    system_prompt = spec.system_prompt + kb_prompt_section(kbs, KB_TOOL_NAME)
    return (
        source.replace("__LAUNCHPAD_AGENT_NAME__", spec.name)
        .replace("__LAUNCHPAD_MODEL_ID__", spec.model_id)
        .replace("__LAUNCHPAD_SYSTEM_PROMPT__", repr(system_prompt))
        .replace("__LAUNCHPAD_MOUNTED_KBS__", repr(kbs))
        .replace("__LAUNCHPAD_KB_RESULTS__", repr(KB_RESULTS))
        .replace("__LAUNCHPAD_KB_TOOL_DESCRIPTION__", repr(kb_description))
        .replace(
            "__LAUNCHPAD_TOOL_DESCRIPTION_OVERRIDES__",
            repr(spec.tool_description_overrides),
        )
    )


def base_requirements() -> list[str]:
    lines = (TEMPLATE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]
