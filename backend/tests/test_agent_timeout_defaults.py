"""Ordinary creation defaults must agree without rewriting explicit execution budgets."""

import pytest

from app.assistant.proposal import ProposalContent, to_agent_spec
from app.schemas.agent import AgentSpec
from app.system_agents.presets import ARCHITECT


def test_ordinary_creation_and_proposal_mapping_default_to_600_seconds():
    spec = AgentSpec(name="ordinary-agent", method="harness", system_prompt="Be helpful.")
    proposal = ProposalContent(name="ordinary-agent", system_prompt="Be helpful.")
    mapped = to_agent_spec(proposal, {"tools": [], "skills": [], "knowledge_bases": []})
    assert spec.timeout_seconds == proposal.timeout_seconds == mapped.timeout_seconds == 600



def test_model_facing_guidance_names_the_schema_default():
    """The architect protocol and skill must not steer proposals to a stale budget."""
    import re
    from pathlib import Path

    from app.assistant import service

    default = ProposalContent.model_fields["timeout_seconds"].default
    skill = (Path(__file__).resolve().parents[1]
             / "app/system_agents/skills/aws-agent-solution-architect")
    texts = {
        "protocol": service.PROTOCOL_PREAMBLE,
        "SKILL.md": (skill / "SKILL.md").read_text(encoding="utf-8"),
        "proposal-self-check.md": (
            skill / "references/proposal-self-check.md"
        ).read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        named = {int(n) for n in re.findall(r"(?:[Uu]se|default) (\d+)(?: seconds)?\b", text)}
        assert named == {default}, (name, named)

@pytest.mark.parametrize("budget", [30, 300, 900, 1200])
def test_explicit_budgets_survive_proposal_mapping(budget):
    proposal = ProposalContent(
        name="ordinary-agent", system_prompt="Be helpful.", timeout_seconds=budget,
    )
    mapped = to_agent_spec(proposal, {"tools": [], "skills": [], "knowledge_bases": []})
    assert mapped.timeout_seconds == budget
    spec = AgentSpec(
        name="ordinary-agent", method="harness", system_prompt="Be helpful.",
        timeout_seconds=budget,
    )
    assert spec.timeout_seconds == budget


def test_architect_preset_keeps_its_900_second_budget():
    assert ARCHITECT.timeout_seconds == 900


def test_a_proposal_without_a_model_gets_the_wizard_default_not_the_spec_fallback():
    """The architect proposes the Create Agent default (GPT-6 Sol on native Bedrock);
    AgentSpec's own default stays what a stored spec without a model_id means."""
    from app.assistant import service
    from app.assistant.proposal import PROPOSAL_DEFAULT_MODEL_ID
    from app.deployer.harness import model_config
    from app.schemas.agent import DEFAULT_MODEL_ID

    proposal = ProposalContent(name="ordinary-agent", system_prompt="Be helpful.")
    mapped = to_agent_spec(proposal, {"tools": [], "skills": [], "knowledge_bases": []})
    assert proposal.model_id == mapped.model_id == PROPOSAL_DEFAULT_MODEL_ID
    assert mapped.method == "harness" and mapped.model_source == "bedrock"
    assert model_config(mapped)["apiFormat"] == "converse_stream"
    assert DEFAULT_MODEL_ID != PROPOSAL_DEFAULT_MODEL_ID
    # the model-facing protocol announces the same default
    assert f"`{PROPOSAL_DEFAULT_MODEL_ID}` / `\"bedrock\"`" in service.PROTOCOL_PREAMBLE
