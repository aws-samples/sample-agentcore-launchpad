"""Studio artifact path: adapter wrap, bundle shim, API acceptance."""

import py_compile

import app.routers.agents as agents_router
from app.schemas.agent import AgentSpec
from app.templates.studio_agent import adapt_studio_code

SAMPLE_STUDIO_CODE = '''
import asyncio
from strands import Agent
from strands.models import BedrockModel

async def main():
    model = BedrockModel(model_id="global.anthropic.claude-sonnet-4-6")
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, model=model,
        system_prompt="You are a helpful research assistant.",
    )
    response = agent("hello")
    print(response)

if __name__ == "__main__":
    asyncio.run(main())
'''


def test_adapt_studio_code_wraps_for_agentcore(tmp_path):
    adapted = adapt_studio_code(SAMPLE_STUDIO_CODE)
    assert "BedrockAgentCoreApp" in adapted
    assert "@app.entrypoint" in adapted
    # platform config-bundle shim injected
    assert "launchpad_config_bundle" in adapted or "get_config_bundle" in adapted
    target = tmp_path / "adapted.py"
    target.write_text(adapted, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def test_platform_accepts_studio_artifact(client, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    res = client.post(
        "/api/agents",
        json={
            "name": "studio-sample-agent",
            "method": "studio",
            "system_prompt": "Strands Studio generated agent",
            "code": SAMPLE_STUDIO_CODE,
        },
    )
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["agent"]["method"] == "studio"
    assert launched == [body["job_id"]]
    spec = AgentSpec(**body["agent"]["spec"])
    assert spec.code and "strands" in spec.code


def test_generate_stage_uses_studio_code():
    from app.deployer.zip_runtime import _generate_code

    spec = AgentSpec(
        name="studio-x", method="studio", system_prompt="s", code=SAMPLE_STUDIO_CODE
    )
    code, source = _generate_code(spec)
    assert source == "studio artifact (adapted)"
    assert "BedrockAgentCoreApp" in code

    template_spec = AgentSpec(name="tmpl-x", method="zip_runtime", system_prompt="s")
    _, template_source = _generate_code(template_spec)
    assert template_source == "strands template"
