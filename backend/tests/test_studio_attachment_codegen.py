"""Execute canvas-generated Python input handling against the native Bedrock adapter.

CN build (spec C5): the canvas has exactly one model-hosting path (native Bedrock
Converse) — Amazon Bedrock Mantle / OpenAI Responses codegen was removed, so this
file no longer generates or exercises a Mantle variant.
"""

import ast
import asyncio
import builtins
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.templates.attachment_support import has_attachment_contract
from app.templates.studio_agent import adapt_studio_code

from .test_runtime_attachments import FILES, assert_binary_content

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@pytest.fixture(scope="module")
def generated():
    if not shutil.which("node") or not (FRONTEND / "node_modules/esbuild").exists():
        pytest.skip("Studio codegen execution requires installed frontend dependencies")
    script = r"""
import { buildSync } from 'esbuild';
import { createRequire } from 'node:module';
const contents = `
import { generateStrandsAgentCode } from './src/studio/lib/code-generator';
import { SAMPLE_FLOWS } from './src/studio/lib/sample-flows';
export const generated = SAMPLE_FLOWS.map(flow => ({
  id: flow.id, ...generateStrandsAgentCode(flow.nodes, flow.edges, flow.graphMode)
}));
`;
const result = buildSync({
  stdin: {contents, resolveDir: process.cwd(), loader: 'ts'},
  bundle: true, platform: 'node', format: 'cjs', write: false
});
const module = {exports: {}};
new Function('module', 'exports', 'require', result.outputFiles[0].text)(
  module, module.exports, createRequire(import.meta.url)
);
console.log(JSON.stringify(module.exports.generated));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_all_sample_flows_generate_compilable_native_contracts(generated):
    for item in generated:
        assert not item["errors"], (item["id"], item["errors"])
        source = "\n".join(item["imports"]) + "\n\n" + item["code"]
        assert has_attachment_contract(source)
        compile(source, item["id"] + ".py", "exec")
        compile(adapt_studio_code(source), item["id"] + "_runtime.py", "exec")


@pytest.mark.parametrize(
    "flow_id",
    [
        "single-agent",
        "agent-swarm",
        "graph-dag",
        "orchestrator-sub-agents",
    ],
)
def test_generated_main_passes_native_content_and_keeps_text(generated, flow_id):
    item = next(item for item in generated if item["id"] == flow_id)
    tree = ast.parse(item["code"])
    main = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    seen = []
    result = SimpleNamespace(
        status="completed",
        execution_order=[],
        node_history=[],
        total_nodes=1,
        completed_nodes=1,
        failed_nodes=0,
        execution_time=0,
        results={},
    )

    class Executor:
        def __call__(self, content):
            seen.append(content)
            return result

        async def invoke_async(self, content):
            return self(content)

        async def stream_async(self, content):
            self(content)
            yield {"data": "answer"}
            yield {"result": result}

    namespace = {"asyncio": asyncio, "json": json}
    for node in ast.walk(main):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and not hasattr(builtins, node.id)
            and node.id not in namespace
        ):
            namespace[node.id] = Executor()
    main_source = "LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'\n" + ast.unparse(main)
    exec(compile(adapt_studio_code(main_source), flow_id + ".py", "exec"), namespace)
    native_result = asyncio.run(namespace["invoke"]({"prompt": "read", "attachments": FILES}))
    assert native_result["attachment_contract"] == "v1"
    assert len(seen) == 1
    assert_binary_content(seen[0])
    assert "attachment_contract" not in asyncio.run(namespace["invoke"]({"prompt": "text"}))
    assert seen[-1] == "text"
    asyncio.run(namespace["invoke"]({"prompt": "", "attachments": FILES}))
    assert_binary_content(seen[-1])
