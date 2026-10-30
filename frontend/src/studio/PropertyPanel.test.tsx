// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Node } from "@xyflow/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { _resetModelCatalog, applyModelCatalog } from "../lib/models";
import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { PropertyPanel } from "./PropertyPanel";

/**
 * N-R6-obs2: the canvas model picker is the platform catalog — the environment's
 * OpenAI-compatible rows are offered next to the Bedrock ones, and a row whose provider has
 * no key renders disabled with the same `models.unavailable` tooltip as every other picker.
 */

const SF = "siliconflow.zai-org/GLM-5.3";

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(() => {
  cleanup();
  _resetModelCatalog();
});

function agentNode(data: Record<string, unknown>): Node {
  return { id: "a1", type: "agent", position: { x: 0, y: 0 }, data: { label: "Researcher", ...data } };
}

function mergeSiliconFlow(configured: boolean) {
  applyModelCatalog(
    [{ model_id: SF, label: "GLM 5.3 (SiliconFlow)", provider: "siliconflow", provider_kind: "openai_compatible",
       provider_label: "SiliconFlow", tools: true, available: configured }],
    { siliconflow: { kind: "openai_compatible", label: "SiliconFlow", configured,
      ssm_parameter: "launchpad-siliconflow-api-key" } },
  );
}

function renderPanel(node: Node, onUpdateNode = vi.fn()) {
  render(
    <MemoryRouter>
      <PropertyPanel selectedNode={node} onClose={() => undefined} onUpdateNode={onUpdateNode} />
    </MemoryRouter>,
  );
  return onUpdateNode;
}

describe("Studio PropertyPanel model picker (N-R6-obs2)", () => {
  it("offers every catalog model and lets a node pick a configured SiliconFlow row", () => {
    mergeSiliconFlow(true);
    const onUpdateNode = renderPanel(agentNode({ modelProvider: "AWS Bedrock", modelId: "global.zai.glm-5.3" }));
    const picker = screen.getByTestId("studio-model-picker") as HTMLSelectElement;
    const values = Array.from(picker.options).map((o) => o.value);
    expect(values.slice(0, 3)).toEqual(["global.zai.glm-5.3", "global.moonshotai.kimi-k3", SF]);
    const sf = picker.options[2];
    expect(sf.disabled).toBe(false);
    expect(sf.textContent).toBe("GLM 5.3 (SiliconFlow)");
    fireEvent.change(picker, { target: { value: SF } });
    expect(onUpdateNode).toHaveBeenCalledWith("a1", expect.objectContaining({ modelId: SF, modelName: "GLM 5.3 (SiliconFlow)" }));
    // the provider dropdown names the catalog, not a single vendor
    expect(screen.getByText(en.studio.prop.providerCatalog)).toBeTruthy();
    expect(screen.queryByTestId("studio-model-unavailable")).toBeNull();
  });

  it("renders an unconfigured provider's row disabled with the SSM-parameter tooltip, and explains a stored one", () => {
    mergeSiliconFlow(false);
    const onUpdateNode = renderPanel(agentNode({ modelProvider: "AWS Bedrock", modelId: SF }));
    const picker = screen.getByTestId("studio-model-picker") as HTMLSelectElement;
    const sf = Array.from(picker.options).find((o) => o.value === SF)!;
    const tooltip = "SiliconFlow not configured — store the key as SSM parameter launchpad-siliconflow-api-key";
    expect(sf.disabled).toBe(true);
    expect(sf.getAttribute("aria-disabled")).toBe("true");
    expect(sf.title).toBe(tooltip);
    expect(sf.textContent).toBe("GLM 5.3 (SiliconFlow) · not configured");
    // the node already carries that model: the hint under the picker says why it cannot publish
    expect(screen.getByTestId("studio-model-unavailable").textContent).toBe(tooltip);
    // the Bedrock rows stay selectable; choosing the disabled row is a no-op
    fireEvent.change(picker, { target: { value: SF } });
    expect(onUpdateNode).not.toHaveBeenCalled();
    fireEvent.change(picker, { target: { value: "global.moonshotai.kimi-k3" } });
    expect(onUpdateNode).toHaveBeenCalledWith("a1", expect.objectContaining({ modelId: "global.moonshotai.kimi-k3" }));
  });
});
