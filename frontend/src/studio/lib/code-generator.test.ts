import type { Edge, Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";
import { _resetModelCatalog, applyModelCatalog } from "../../lib/models";
import { generateStrandsAgentCode } from "./code-generator";

const REGION_LINE = 'region_name=os.environ.get("LAUNCHPAD_MODEL_REGION", "us-west-2")';

function flow(agentData: Record<string, unknown>): { nodes: Node[]; edges: Edge[] } {
  const node = (id: string, type: string, data: Record<string, unknown>): Node =>
    ({ id, type, position: { x: 0, y: 0 }, data }) as Node;
  return {
    nodes: [
      node("in", "input", { label: "Input" }),
      node("a1", "agent", { label: "Researcher", ...agentData }),
      node("out", "output", { label: "Output" }),
    ],
    edges: [
      { id: "e1", source: "in", target: "a1" },
      { id: "e2", source: "a1", target: "out" },
    ],
  };
}

describe("canvas Bedrock emitter (spec C3)", () => {
  it.each([false, true])("enters the Global profile from LAUNCHPAD_MODEL_REGION, max_tokens ≥ 256 (graphMode=%s)", (graphMode) => {
    const { nodes, edges } = flow({ modelProvider: "AWS Bedrock", modelId: "global.zai.glm-5.3", maxTokens: 60 });
    const result = generateStrandsAgentCode(nodes, edges, graphMode);
    expect(result.errors).toEqual([]);
    expect(result.code).toContain("researcher_model = BedrockModel(");
    expect(result.code).toContain('model_id="global.zai.glm-5.3"');
    expect(result.code).toContain(REGION_LINE);
    expect(result.code).toContain("max_tokens=256");
    expect(result.code).not.toMatch(/mantle/i);
    expect(result.imports).toContain("import os");
  });

  it("a node without a model id generates the GLM 5.3 default; a user ceiling above the floor is kept", () => {
    const bare = flow({});
    expect(generateStrandsAgentCode(bare.nodes, bare.edges).code).toContain('model_id="global.zai.glm-5.3"');
    const kimi = flow({ modelId: "global.moonshotai.kimi-k3", maxTokens: 32000 });
    const code = generateStrandsAgentCode(kimi.nodes, kimi.edges).code;
    expect(code).toContain('model_id="global.moonshotai.kimi-k3"');
    expect(code).toContain("max_tokens=32000");
    expect(code).not.toContain("temperature=");
  });

  it("adaptive thinking is Anthropic-only — a GLM node with the toggle gets a plain model (R17)", () => {
    const glm = flow({ modelId: "global.zai.glm-5.3", thinkingEnabled: true, reasoningEffort: "high" });
    const code = generateStrandsAgentCode(glm.nodes, glm.edges).code;
    expect(code).not.toContain('"thinking"');
    expect(code).toContain(REGION_LINE);
    const claude = flow({ modelId: "global.anthropic.claude-sonnet-5", thinkingEnabled: true });
    expect(generateStrandsAgentCode(claude.nodes, claude.edges).code).toContain('"type": "adaptive"');
  });

  it("an OpenAI-compatible catalog id is emitted by the SAME BedrockModel(model_id=…) line — the Studio adapter dispatches on it (N-R6-obs2)", () => {
    const SF = "siliconflow.zai-org/GLM-5.3";
    applyModelCatalog(
      [{ model_id: SF, label: "GLM 5.3 (SiliconFlow)", provider: "siliconflow", provider_kind: "openai_compatible",
         provider_label: "SiliconFlow", tools: true, available: true }],
      { siliconflow: { kind: "openai_compatible", label: "SiliconFlow", configured: true } },
    );
    try {
      const { nodes, edges } = flow({ modelProvider: "AWS Bedrock", modelId: SF, maxTokens: 512 });
      const result = generateStrandsAgentCode(nodes, edges);
      expect(result.errors).toEqual([]);
      expect(result.code).toContain("researcher_model = BedrockModel(");
      expect(result.code).toContain(`model_id="${SF}"`);
      expect(result.code).not.toContain("OpenAIModel(");  // no endpoint, key or client in canvas code
      expect(result.code).not.toMatch(/siliconflow\.cn|api_key/);
      expect(result.code).not.toContain("temperature=");  // sampling params come from the runtime table
    } finally {
      _resetModelCatalog();
    }
  });
});
