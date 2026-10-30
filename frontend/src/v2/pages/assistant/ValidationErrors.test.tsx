// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import type { ValidationErrorDetail } from "../../../lib/api";
import { errorDetails } from "../../../lib/validation-errors";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { ValidationErrorList } from "./ValidationErrors";

/**
 * N17-obs296: validation errors in the zh-CN console were raw English inside a Chinese
 * banner. Known rule codes now render in the member's language with the English sentence
 * as the collapsed detail; anything else shows the sentence.
 */

const REF = "evaluation_plan.evaluators.runtime_writes_and_trajectory: scenario 'gt1-policy-qna' "
  + "has no expected_trajectory — a reference-driven evaluator is applied to every session, so "
  + "every scenario must carry it";
const DETAIL: ValidationErrorDetail = {
  code: "evaluation_plan.reference_missing",
  params: { evaluator: "runtime_writes_and_trajectory", scenario: "gt1-policy-qna", fields: ["expected_trajectory"] },
  message: REF,
};
const PARTIAL: ValidationErrorDetail = {
  code: "evaluation_plan.evaluator_partial_routing",
  params: { evaluator: "helpful", golden_tests: ["GT-1", "GT-2"] },
  message: "evaluation_plan.evaluators.helpful: targets only ['GT-1', 'GT-2'] but a dataset run …",
};
const TRACE: ValidationErrorDetail = {
  code: "evaluation_plan.trace_turn_missing_response",
  params: { evaluator: "ref_judge", scenario: "GT-1" },
  message: "evaluation_plan.evaluators.ref_judge: scenario 'GT-1' has a turn without expected_response …",
};

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("zh-CN");
});

describe("ValidationErrorList (N17-obs296)", () => {
  it("renders every known rule code in Chinese and keeps the English as the detail", () => {
    const generic = { code: "proposal.validation_error", params: {}, message: "name: must be a lowercase slug" };
    render(<ValidationErrorList errors={[DETAIL, PARTIAL, TRACE, generic]} testId="errs" />);
    const items = screen.getByTestId("errs").querySelectorAll("li");
    expect(items).toHaveLength(4);
    const first = items[0].textContent ?? "";
    expect(first).toContain("评估器 runtime_writes_and_trajectory：场景 gt1-policy-qna 缺少 expected_trajectory。");
    expect(first).toContain("原始信息");
    expect(items[0].querySelector("details")?.textContent).toContain(REF);
    expect(items[1].textContent).toContain("只对应黄金测试 GT-1, GT-2");
    expect(items[2].textContent).toContain("有一轮缺少 expected_response");
    // an unknown code is never invented: the sentence itself, no detail toggle
    expect(items[3].textContent).toBe("name: must be a lowercase slug");
    expect(items[3].querySelector("details")).toBeNull();
  });

  it("renders the same codes in English", async () => {
    await i18next.changeLanguage("en");
    render(<ValidationErrorList errors={[DETAIL]} testId="errs" />);
    expect(screen.getByTestId("errs").textContent).toContain(
      "Evaluator runtime_writes_and_trajectory: scenario gt1-policy-qna has no expected_trajectory");
  });

  it("falls back to the plain sentences when a payload carries no details", () => {
    expect(errorDetails([REF], undefined)[0]).toEqual({ code: "proposal.validation_error", params: {}, message: REF });
    expect(errorDetails([REF], [DETAIL])[0]).toBe(DETAIL);
    // a details list that does not line up with the sentences is ignored
    expect(errorDetails(["other"], [DETAIL])[0].code).toBe("proposal.validation_error");
  });
});
