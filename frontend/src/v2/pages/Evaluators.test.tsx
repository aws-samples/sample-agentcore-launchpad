// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { EvaluatorDetail } from "../../lib/api";
import en from "../../locales/en/common.json";
import zh from "../../locales/zh-CN/common.json";
import { ToastContext } from "../hooks";
import { V2Evaluators } from "./Evaluators";

/**
 * N17-obs434: the evaluator pages show the evaluator's current version — a custom / derived
 * evaluator's `v<N>` (detail and list), a built-in's library release (detail) — and the save
 * confirmation says whether the edit bumped it.
 */

const JUDGE: EvaluatorDetail = {
  id: "cust_f623957dd060", name: "hr_policy_citation", level: "TRACE", description: "cites the policy",
  definition: "judge", instructions: "Does the answer cite the policy? {assistant_turn}",
  rating_scale: [{ value: 1, label: "Yes", definition: "cites" }, { value: 0, label: "No", definition: "does not" }],
  model_id: "global.zai.glm-5.3", base_evaluator_id: null, lambda_arn: null, lambda_timeout_s: null,
  status: "ACTIVE", evaluator_type: "Custom", provider: "Custom", version: 2, definition_hash: "abcdef0123456789",
  created_by: null, created_by_name: null,
};
let saved: EvaluatorDetail = JUDGE;

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      v2Evaluators: () => Promise.resolve({
        evaluators: [
          { id: "Builtin.Helpfulness", level: "TRACE", source: "builtin" },
          { id: JUDGE.id, name: JUDGE.name, level: "TRACE", source: "custom", definition: "judge", version: 2, created_by: null },
        ],
        builtin_count: 1,
      }),
      v2Evaluator: (id: string) => Promise.resolve(
        id === JUDGE.id ? JUDGE : { ...JUDGE, id, name: "Helpfulness", version: "strands-agents-evals 1.5.0" },
      ),
      v2UpdateEvaluator: () => Promise.resolve({ ...saved, model_fallback: null }),
    },
  };
});
vi.mock("../../lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/capabilities")>()),
  useCodeEvaluatorsEnabled: () => false,
}));

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  saved = JUDGE;
});

function mount(query: string, toast = vi.fn()) {
  render(
    <ToastContext.Provider value={toast}>
      <MemoryRouter initialEntries={[`/v2/eval/evaluators${query}`]}>
        <V2Evaluators />
      </MemoryRouter>
    </ToastContext.Provider>,
  );
  return toast;
}

describe("evaluator version (N17-obs434)", () => {
  it("shows a custom evaluator's version on its detail page and in the list", async () => {
    mount(`?view=detail&id=${JUDGE.id}`);
    expect((await screen.findByTestId("v2-evaluator-version")).textContent).toBe("v2");
    cleanup();
    mount("");
    await waitFor(() => expect(document.body.textContent).toContain(`${JUDGE.id} · v2`));
  });

  it("shows a built-in's library release", async () => {
    mount("?view=detail&id=Builtin.Helpfulness");
    expect((await screen.findByTestId("v2-evaluator-version")).textContent).toBe(
      "strands-agents-evals 1.5.0（内置评估器随评估库版本发布）",
    );
  });

  it("says whether a save bumped the version", async () => {
    saved = { ...JUDGE, version: 3 };
    let toast = mount(`?view=edit&id=${JUDGE.id}`);
    fireEvent.click(await screen.findByTestId("v2-evaluator-save"));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("success", "评估器已保存，评分定义已变更：版本 v2 → v3"));
    cleanup();
    saved = JUDGE; // a description-only edit keeps the version
    toast = mount(`?view=edit&id=${JUDGE.id}`);
    fireEvent.click(await screen.findByTestId("v2-evaluator-save"));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("success", "评估器已保存，评分定义未变，版本仍为 v2"));
  });
});
