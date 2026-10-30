// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { _resetModelCatalog, applyModelCatalog } from "../../../lib/models";
import { JUDGE_MODEL_SUGGESTIONS, judgeModelSuggestions } from "../../../lib/skillLab";
import { ModelFields } from "./ModelFields";

/**
 * N-R6-obs1 follow-up (r8-a): the Skill Lab judge `<datalist>` mirrors the catalog — an
 * unconfigured provider's rows are listed "not configured" and disabled, and a typed /
 * picked unavailable id is flagged under the field with the SSM parameter to create.
 */

const ROWS = [
  { model_id: "global.zai.glm-5.3", label: "GLM 5.3", provider: "bedrock",
    provider_kind: "bedrock" as const, provider_label: "Global Bedrock", tools: true, default: true },
  { model_id: "siliconflow.zai-org/GLM-5.3", label: "GLM-5.3 (SiliconFlow)", provider: "siliconflow",
    provider_kind: "openai_compatible" as const, provider_label: "SiliconFlow", tools: true, available: false },
  { model_id: "siliconflow.Qwen/Qwen3-8B", label: "Qwen3-8B (SiliconFlow)", provider: "siliconflow",
    provider_kind: "openai_compatible" as const, provider_label: "SiliconFlow", tools: false, available: false },
];
const PROVIDERS = {
  siliconflow: { kind: "openai_compatible" as const, label: "SiliconFlow", configured: false,
    ssm_parameter: "launchpad-siliconflow-api-key" },
};

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

function renderFields(judgeModel: string) {
  return render(
    <ModelFields
      status={null}
      testId="v2-eval"
      backend="opencode_exec"
      setBackend={() => undefined}
      targetModel=""
      setTargetModel={() => undefined}
      judge={{ model: judgeModel, setModel: () => undefined, mode: "chat", setMode: () => undefined }}
    />,
  );
}

describe("Skill Lab judge datalist mirrors the catalog (N-R6-obs1 follow-up)", () => {
  it("falls back to the two static suggestions before the catalog loads", () => {
    expect(judgeModelSuggestions().map((r) => r.model_id)).toEqual(JUDGE_MODEL_SUGGESTIONS);
    expect(judgeModelSuggestions().every((r) => r.available)).toBe(true);
  });

  it("lists every catalog row; unavailable ones are disabled and labelled not configured", () => {
    applyModelCatalog(ROWS, PROVIDERS);
    renderFields("global.zai.glm-5.3");
    const list = screen.getByTestId("v2-eval-judge-models");
    const options = Array.from(list.querySelectorAll("option"));
    expect(options.map((o) => o.value)).toEqual([
      "global.zai.glm-5.3", "global.moonshotai.kimi-k3",
      "siliconflow.zai-org/GLM-5.3", "siliconflow.Qwen/Qwen3-8B",
    ]);
    const byId = Object.fromEntries(options.map((o) => [o.value, o]));
    expect(byId["global.zai.glm-5.3"].disabled).toBe(false);
    expect(byId["global.zai.glm-5.3"].hasAttribute("label")).toBe(false);
    expect(byId["siliconflow.zai-org/GLM-5.3"].disabled).toBe(true);
    expect(byId["siliconflow.zai-org/GLM-5.3"].getAttribute("label")).toBe("siliconflow.zai-org/GLM-5.3 · not configured");
    // judge-only rows (tools: false) are offered to the judge too, same rule
    expect(byId["siliconflow.Qwen/Qwen3-8B"].disabled).toBe(true);
    // the available pick carries the ordinary hint, no warning
    expect(screen.queryByTestId("v2-eval-judge-model-unavailable")).toBeNull();
    expect(screen.getByTestId("v2-eval-judge-model").getAttribute("aria-invalid")).toBeNull();
  });

  it("flags a typed unavailable id under the field with the provider's SSM parameter", () => {
    applyModelCatalog(ROWS, PROVIDERS);
    renderFields("siliconflow.zai-org/GLM-5.3");
    expect(screen.getByTestId("v2-eval-judge-model-unavailable").textContent).toBe(
      "SiliconFlow not configured — store the key as SSM parameter launchpad-siliconflow-api-key",
    );
    expect(screen.getByTestId("v2-eval-judge-model").getAttribute("aria-invalid")).toBe("true");
  });

  it("never flags a custom / legacy id (the backend's catalog check refuses it)", () => {
    applyModelCatalog(ROWS, PROVIDERS);
    renderFields("us.anthropic.legacy-id");
    expect(screen.queryByTestId("v2-eval-judge-model-unavailable")).toBeNull();
  });
});
