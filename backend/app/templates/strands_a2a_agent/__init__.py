"""A2A-protocol Strands agent template renderer.

Same __LAUNCHPAD_*__ marker scheme as the HTTP strands template (brace-safe
Python; rendered output must always compile). The skills list renders as a
Python literal via repr().

`spec.model_source` is deliberately IGNORED here: this template always hands the
bare model id to `Agent(model=...)`, i.e. Bedrock Converse. A Mantle id would
render and deploy fine and then fail on first invoke, so the wizard pins A2A
agents to `model_source="bedrock"` (`A2A_MODEL_SOURCE` in CreateAgent.tsx) and
hides the Model source control for them. Teaching this template the Mantle branch
also means adding the `openai` extra to `a2a_base_requirements()`, whose wheel set
is vendored into a ~46 MB zip — deferred until A2A on Mantle is actually asked
for.
"""

from pathlib import Path

from app.schemas.agent import AgentSpec

TEMPLATE_DIR = Path(__file__).parent


def render_a2a_main_py(spec: AgentSpec) -> str:
    skills = [s.model_dump() for s in spec.a2a_skills]
    source = (TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    return (
        source.replace("__LAUNCHPAD_SKILLS_ENABLED__", repr(bool(spec.skills)))
        .replace("__LAUNCHPAD_AGENT_NAME__", spec.name)
        .replace("__LAUNCHPAD_MODEL_ID__", spec.model_id)
        .replace("__LAUNCHPAD_SYSTEM_PROMPT__", repr(spec.system_prompt))
        .replace("__LAUNCHPAD_AGENT_DESCRIPTION__", repr(spec.system_prompt[:180]))
        .replace("__LAUNCHPAD_A2A_SKILLS__", repr(skills))
    )


def a2a_base_requirements() -> list[str]:
    lines = (TEMPLATE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]
