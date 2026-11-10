"""A2A-protocol Strands agent template renderer.

Same __LAUNCHPAD_*__ marker scheme as the HTTP strands template (brace-safe
Python; rendered output must always compile). The skills list renders as a
Python literal via repr().

The model is the Global Bedrock profile `spec.model_id`, entered from
`spec.model_region` with the Global Bedrock API key fetched from
`spec.model_credential` — the same `_resolve_bedrock_api_key()` contract as the
HTTP template (docs/cn/interfaces/model-credential.md).
"""

from pathlib import Path

from app.schemas.agent import AgentSpec
from app.services.registry_backend import A2A_CARD_VERSION
from app.templates.memory_support import render_memory_source
from app.templates.model_support import render_model_source

TEMPLATE_DIR = Path(__file__).parent


def render_a2a_main_py(spec: AgentSpec) -> str:
    skills = [s.model_dump() for s in spec.a2a_skills]
    source = (TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    return (
        source.replace("__LAUNCHPAD_SKILLS_ENABLED__", repr(bool(spec.skills)))
        .replace("__LAUNCHPAD_AGENT_NAME__", spec.name)
        .replace("__LAUNCHPAD_MODEL_SOURCE__", render_model_source(spec))
        .replace("__LAUNCHPAD_SYSTEM_PROMPT__", repr(spec.system_prompt))
        .replace("__LAUNCHPAD_MEMORY_SOURCE__", render_memory_source())
        .replace("__LAUNCHPAD_AGENT_DESCRIPTION__", repr(spec.system_prompt[:180]))
        .replace("__LAUNCHPAD_A2A_SKILLS__", repr(skills))
        .replace("__LAUNCHPAD_A2A_CARD_VERSION__", repr(A2A_CARD_VERSION))
    )


def a2a_base_requirements() -> list[str]:
    lines = (TEMPLATE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]
