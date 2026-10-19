"""Studio artifact path: adapter wrap, bundle shim, API acceptance."""

import ast
import asyncio
import py_compile
import sys
import types

import pytest

import app.routers.agents as agents_router
from app.schemas.agent import AgentSpec
from app.templates.studio_agent import _drop_removed_tools, adapt_studio_code

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


# Shape of canvas code generated before 0b380650 (2026-08-10), e.g. dev's
# math-agent: the static import line names mem0_memory, and a mem0 tool node
# wired it into tools=[...]. Since then the package stage installs plain
# strands-agents-tools, so `mem0` is absent and the import kills the runtime.
LEGACY_MEM0_STUDIO_CODE = '''
from strands import Agent, tool
from strands.models import BedrockModel
from strands_tools import calculator, file_read, current_time, mem0_memory
import asyncio

model = BedrockModel(model_id="global.anthropic.claude-sonnet-4-6")
memory_agent = Agent(
    model=model,
    system_prompt="Remember facts with [mem0_memory, calculator].",
    tools=[mem0_memory, calculator],
)
math_agent = Agent(model=model, tools=[calculator, mem0_memory])
helper_agent = Agent(model=model, tools=[
    calculator,
    mem0_memory,
    current_time,
])
solo_agent = Agent(model=model, tools=[mem0_memory, mem0_memory])

async def main(user_input_arg: str = None, messages_arg: str = None):
    async for event in math_agent.stream_async(user_input_arg):
        if "data" in event:
            print(event["data"], end="")

if __name__ == "__main__":
    asyncio.run(main("hi"))
'''


def _tool_lists(source: str) -> dict[str, list[str]]:
    lists = {}
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        for keyword in node.value.keywords:
            if keyword.arg == "tools" and isinstance(keyword.value, ast.List):
                target = ast.unparse(node.targets[0])
                lists[target] = [ast.unparse(elt) for elt in keyword.value.elts]
    return lists


def test_adapt_studio_code_drops_mem0_from_legacy_code():
    adapted = adapt_studio_code(LEGACY_MEM0_STUDIO_CODE)
    tree = ast.parse(adapted)
    assert not any(isinstance(n, ast.Name) and n.id == "mem0_memory" for n in ast.walk(tree))
    assert "from strands_tools import calculator, file_read, current_time\n" in adapted
    assert _tool_lists(adapted) == {
        "memory_agent": ["calculator"],
        "math_agent": ["calculator"],
        "helper_agent": ["calculator", "current_time"],
        "solo_agent": [],
    }
    # user text is never rewritten — only code tokens
    assert 'system_prompt="Remember facts with [mem0_memory, calculator]."' in adapted
    # a pasted U+2028 is a line break to str.splitlines but not to tokenize
    pasted = _drop_removed_tools(LEGACY_MEM0_STUDIO_CODE.replace("facts with", "facts\u2028with"))
    assert _tool_lists(pasted) == _tool_lists(adapted)
    assert "Remember facts\u2028with [mem0_memory, calculator]." in pasted

    # a sole-name import goes away whole, already-wrapped modules included
    wrapped = adapt_studio_code("from strands_tools import mem0_memory\n@app.entrypoint\n")
    assert "strands_tools" not in wrapped


def test_legacy_mem0_studio_module_imports_without_mem0(monkeypatch):
    """The adapted module imports and answers against a strands_tools without mem0."""

    class FakeAgent:
        def __init__(self, model=None, tools=(), **_):
            self.tools = list(tools)

        async def stream_async(self, prompt):
            yield {"data": f"echo:{prompt}"}

    strands = types.ModuleType("strands")
    strands.__dict__.update(Agent=FakeAgent, tool=lambda fn: fn)
    models = types.ModuleType("strands.models")
    models.__dict__.update(BedrockModel=dict)
    strands_tools = types.ModuleType("strands_tools")  # the extra-less install
    strands_tools.__dict__.update(calculator="calculator", file_read="file_read")
    strands_tools.__dict__.update(current_time="current_time")
    for module in (strands, models, strands_tools):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(ImportError, match="mem0_memory"):
        exec(compile(LEGACY_MEM0_STUDIO_CODE, "legacy.py", "exec"), {"__name__": "legacy"})

    namespace = {"__name__": "legacy"}
    exec(compile(adapt_studio_code(LEGACY_MEM0_STUDIO_CODE), "adapted.py", "exec"), namespace)
    assert namespace["helper_agent"].tools == ["calculator", "current_time"]
    assert asyncio.run(namespace["invoke"]({"prompt": "2+2"})) == {"result": "echo:2+2"}
