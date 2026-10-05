"""A new Managed Harness starts with both native tools; stored/API specs never gain them."""

from pathlib import Path

from app.assistant.proposal import ProposalContent, to_agent_spec
from app.schemas.agent import AgentSpec

FRONTEND = Path(__file__).resolve().parents[2] / "frontend/src"
BOTH = ["shell", "file_operations"]


def test_a_proposal_defaults_to_both_native_tools_and_keeps_an_explicit_choice():
    proposal = ProposalContent(name="research-agent", system_prompt="Be careful.")
    assert proposal.native_tools == BOTH
    mapped = to_agent_spec(proposal, {"tools": [], "skills": [], "knowledge_bases": []})
    assert mapped.native_tools == BOTH
    for explicit in ([], ["file_operations"]):
        chosen = ProposalContent(name="research-agent", system_prompt="x", native_tools=explicit)
        mapped = to_agent_spec(chosen, {"tools": [], "skills": [], "knowledge_bases": []})
        assert mapped.native_tools == explicit


def test_the_api_spec_default_stays_empty_so_redeploys_never_gain_shell():
    spec = AgentSpec(name="api-agent", method="harness", system_prompt="Be careful.")
    assert spec.native_tools == []


def test_both_create_wizards_start_from_the_shared_default():
    defaults = (FRONTEND / "lib/agent-defaults.ts").read_text(encoding="utf-8")
    assert 'DEFAULT_HARNESS_NATIVE_TOOLS = ["shell", "file_operations"]' in defaults
    form = (FRONTEND / "lib/agent-spec.ts").read_text(encoding="utf-8")
    assert 'nativeTools: method === "harness" ? [...DEFAULT_HARNESS_NATIVE_TOOLS] : [],' in form
    classic = (FRONTEND / "pages/CreateAgent.tsx").read_text(encoding="utf-8")
    assert "useState<HarnessNativeTool[]>([...DEFAULT_HARNESS_NATIVE_TOOLS])" in classic
    assert "setNativeTools([...DEFAULT_HARNESS_NATIVE_TOOLS]);" in classic


def test_architect_guidance_matches_the_default():
    from app.assistant import service

    skill = (Path(__file__).resolve().parents[1]
             / "app/system_agents/skills/aws-agent-solution-architect/SKILL.md")
    text = skill.read_text(encoding="utf-8")
    assert '`native_tools: ["shell", "file_operations"]`' in text
    assert "read the current date with shell" in text
    assert "Default\n  is both" in service.PROTOCOL_PREAMBLE
