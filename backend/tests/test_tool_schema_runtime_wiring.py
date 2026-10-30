"""N17-obs302: every generated runtime hands its model normalized tool specs.

The normalizer rides in the shared model-helpers block (`_launchpad_chat_model` wraps the
model it builds), so the zip runtime, the Harness `model/load.py`, the Studio adapter, the
A2A runtime and the evaluation worker all get it from one text; the hub's
`aws_clients.build_chat_model` imports the same module. The end-to-end case renders a
Harness agent with code-interpreter + KB + gateway + skills, builds its Agent offline on the
REAL strands / strands_tools with a stubbed bedrock-runtime client (diag-f4's capture.py
shape) and asserts on the ConverseStream request the model would send to Kimi K3."""

import ast
import importlib.util
import json

import pytest

from app.deployer import harness_local
from app.schemas.agent import AgentSpec, KnowledgeBaseRef, ToolRef
from app.services import aws_clients
from app.templates import model_support
from app.templates.strands_agent import render_main_py

from .conftest import ws_ctx

KIMI = "global.moonshotai.kimi-k3"
BEARER = "AWS_BEARER_TOKEN_BEDROCK"
GW_ID = "launchpad-gw-a1b2c3d4e5"
RES = {
    "artifacts_bucket": "launchpad-artifacts-test",
    "gateway_id": GW_ID,
    "gateway_url": f"https://{GW_ID}.gateway.bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/mcp",
    "memory_id": "mem-1",
    "execution_role_arn": "arn:aws-cn:iam::111122223333:role/launchpad-agent-execution-role",
}
WRAP_CALL = "return launchpad_normalize_model_tool_schemas(model)"


def _strict_violations(text: str) -> list[str]:
    found = []
    for node in _walk(json.loads(text)):
        if isinstance(node, dict):
            found += [k for k in ("oneOf", "discriminator") if k in node]
            if node.get("format") in ("binary", "byte"):
                found.append(f"format={node['format']}")
    return found


def _walk(node):
    yield node
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        yield from _walk(child)


def _harness_spec() -> AgentSpec:
    return AgentSpec(
        name="travel-expense", method="harness", system_prompt="You file expense reports.",
        model_id=KIMI,
        tools=[
            ToolRef(type="builtin", name="code-interpreter"),
            ToolRef(type="gateway", name="hr-database",
                    config={"record_id": "rec-1", "gateway_id": GW_ID,
                            "tools": ["hr-database___get_employee"]}),
        ],
        knowledge_bases=[KnowledgeBaseRef(kb_id="KB1", name="travel", description="policy")],
        skills=["s3://b/skills/expense-report-writer/"],
    )


def _load(path, name):
    module_spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


# ---- the one block every runtime's model code is sliced from ----------------------------

def test_shared_model_block_wraps_every_built_model():
    block = model_support.render_model_block(
        model_id=KIMI, model_region="us-west-2",
        model_credential="launchpad-global-bedrock", max_tokens=16384)
    assert "def launchpad_normalize_model_tool_schemas(" in block
    tree = ast.parse(block)
    dispatch = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_launchpad_chat_model")
    returns = [n for n in ast.walk(dispatch) if isinstance(n, ast.Return)]
    assert [ast.unparse(r) for r in returns] == [WRAP_CALL]
    build = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "build_model")
    assert "_launchpad_chat_model(" in ast.unparse(build)


@pytest.mark.parametrize("method", ["zip_runtime", "harness"])
def test_zip_and_harness_renders_carry_the_wrapped_dispatch(method):
    spec = AgentSpec(name="probe", method=method, system_prompt="p", model_id=KIMI)
    source = render_main_py(spec) if method == "zip_runtime" else (
        model_support.render_model_load(spec))
    assert "__LAUNCHPAD_TOOL_SCHEMA_SOURCE__" not in source
    assert source.count("def launchpad_normalize_tool_schema(") == 1
    assert WRAP_CALL in source
    compile(source, f"<{method}>", "exec")


def test_eval_worker_release_digest_covers_the_normalizer():
    from app.templates import eval_worker

    assert "tool_schema" in eval_worker.release()["files"]


def test_hub_chat_model_is_wrapped(monkeypatch):
    monkeypatch.setenv(BEARER, "bedrock-api-key")
    aws_clients.reset_cache()
    try:
        model = aws_clients.build_chat_model(KIMI, region="us-west-2")
    finally:
        aws_clients.reset_cache()
    assert getattr(model, "_launchpad_tool_schemas_normalized", False) is True


# ---- the obs302 agent, end to end offline ------------------------------------------------

def test_harness_bundle_with_ci_kb_gateway_skills_sends_normalized_tool_specs(
    tmp_path, monkeypatch,
):
    from strands import Agent, AgentSkills
    from strands.tools.tools import PythonAgentTool

    bundle = harness_local.render_harness_bundle(
        _harness_spec(), ws_ctx(RES, region="cn-northwest-1"))
    # every Agent the bundle builds takes its model from model/load.py's load_model()
    main_tree = ast.parse(bundle.files["main.py"])
    agent_calls = [n for n in ast.walk(main_tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == "Agent"]
    assert agent_calls
    for call in agent_calls:
        model_kw = next(k for k in call.keywords if k.arg == "model")
        assert ast.unparse(model_kw.value) == "load_model()"
    assert WRAP_CALL in bundle.files["model/load.py"]

    for rel in ("model/load.py", "launchpad_builtin_tools.py", "launchpad_kb_tools.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bundle.files[rel], encoding="utf-8")
    monkeypatch.setenv(BEARER, "stub")  # restored afterwards: the loader exports the key
    load = _load(tmp_path / "model" / "load.py", f"obs302_load_{tmp_path.name}")
    load._BEDROCK_API_KEY["key"] = "stub-key"
    builtins = _load(tmp_path / "launchpad_builtin_tools.py", f"obs302_ci_{tmp_path.name}")
    monkeypatch.syspath_prepend(str(tmp_path))
    kb = _load(tmp_path / "launchpad_kb_tools.py", f"obs302_kb_{tmp_path.name}")

    # a gateway tool carrying the same constructs (an MCP target schema is the target's own)
    gw_name = "launchpad_gw_hr-database___get_employee"
    gw_schema = {"type": "object", "properties": {
        "id": {"oneOf": [{"type": "string", "format": "byte"}, {"type": "integer"}]},
        "since": {"type": "string", "format": "date-time"}}}
    gateway_tool = PythonAgentTool(
        gw_name, {"name": gw_name, "description": "employee", "inputSchema": {"json": gw_schema}},
        lambda tool_use, **kw: {"toolUseId": tool_use["toolUseId"], "status": "success",
                                "content": [{"text": "{}"}]})
    skill = tmp_path / "skills" / "expense-report-writer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: expense-report-writer\ndescription: Writes expense reports.\n---\nBody\n",
        encoding="utf-8")

    sent: list[dict] = []

    class StubRuntime:
        def converse_stream(self, **request):
            sent.append(json.loads(json.dumps(request, default=str)))
            return {"stream": iter([
                {"messageStart": {"role": "assistant"}},
                {"contentBlockDelta": {"delta": {"text": "ok"}, "contentBlockIndex": 0}},
                {"contentBlockStop": {"contentBlockIndex": 0}},
                {"messageStop": {"stopReason": "end_turn"}},
            ])}

    model = load.load_model()
    assert model.get_config()["model_id"] == KIMI
    model.client = StubRuntime()
    agent = Agent(model=model, system_prompt="p", callback_handler=None,
                  tools=[builtins.code_interpreter, kb.kb_search, kb.kb_deep_search,
                         gateway_tool],
                  plugins=[AgentSkills(skills=[str(skill)])])
    # the tool registry itself still holds the tools' own (strict) schemas
    raw = json.dumps(agent.tool_registry.get_all_tool_specs())
    assert "oneOf" in raw and '"binary"' in raw

    agent("hello")

    assert len(sent) == 1
    tools = sent[0]["toolConfig"]["tools"]
    names = {t["toolSpec"]["name"] for t in tools}
    assert {"code_interpreter", "kb_search", "kb_deep_search", gw_name} <= names
    assert _strict_violations(json.dumps(tools)) == []
    by_name = {t["toolSpec"]["name"]: t["toolSpec"]["inputSchema"]["json"] for t in tools}
    action = by_name["code_interpreter"]["$defs"]["CodeInterpreterInput"]["properties"]["action"]
    assert len(action["anyOf"]) == 8
    assert by_name[gw_name]["properties"]["since"]["format"] == "date-time"
