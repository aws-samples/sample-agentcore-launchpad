// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import type { AssistantProposal } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { NameConflictAlert } from "./NameConflict";
import { nameConflictReason } from "./nameConflictReason";

/**
 * N17-obs469 (live 2026-10-26, conversation 785829de r2 "travel-expense-assistant"): 批准并部署
 * answered 409 agent.name_exists three times with only a transient toast. A revision whose
 * name a live agent holds now shows a persistent warning and approval is disabled with it.
 */

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common", interpolation: { escapeValue: false },
  });
});

afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("en");
});

const proposal = (conflict: { name: string } | null | undefined) =>
  ({ revision: 2, status: "draft", content: { name: "travel-expense-assistant" },
     ...(conflict !== undefined ? { name_conflict: conflict } : {}) }) as unknown as AssistantProposal;

describe("a taken agent name (N17-obs469)", () => {
  it("names the taken name as the reason approval is disabled", () => {
    expect(nameConflictReason(i18next.t, proposal({ name: "travel-expense-assistant" })))
      .toContain("travel-expense-assistant");
    expect(nameConflictReason(i18next.t, proposal(null))).toBeUndefined();
    expect(nameConflictReason(i18next.t, proposal(undefined))).toBeUndefined(); // older backend
    expect(nameConflictReason(i18next.t, null)).toBeUndefined();
  });

  it("shows a persistent inline warning, in Chinese too", async () => {
    render(<NameConflictAlert proposal={proposal({ name: "travel-expense-assistant" })} />);
    expect(screen.getByTestId("v2-assistant-name-conflict").textContent).toContain("travel-expense-assistant");
    cleanup();
    await i18next.changeLanguage("zh-CN");
    render(<NameConflictAlert proposal={proposal({ name: "travel-expense-assistant" })} />);
    expect(screen.getByTestId("v2-assistant-name-conflict").textContent)
      .toBe("名称 travel-expense-assistant 已被工作区内的 Agent 占用——请在「编辑」中改名后再批准。");
  });

  it("renders nothing for a free name", () => {
    render(<NameConflictAlert proposal={proposal(null)} />);
    expect(screen.queryByTestId("v2-assistant-name-conflict")).toBeNull();
  });
});
