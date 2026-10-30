// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { livePolicyDeny } from "../../../lib/policy-deny";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { type ChatMessage, Thread } from "./Thread";

/**
 * N17-obs507 (Global parity regression): a member's forbidden tool call is denied by the
 * Gateway interceptor and reaches the hub as the tool's result text. The hub now derives the
 * `policy_denied` frame; the thread shows the Chinese 「被策略拦截」 card naming the policies and
 * COLLAPSES the paired failed tool card's raw English result behind a details toggle.
 */
const RAW = "Tool execution failed: Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to pol_8be25bb490c9, pol_9bede8d9b67e]";

beforeAll(async () => {
  Element.prototype.scrollTo = () => undefined;
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("zh-CN");
});

const deny = livePolicyDeny({
  tool: "hr-database___create_payout", reason: RAW, policy_id: "pol_8be25bb490c9",
  policy_ids: ["pol_8be25bb490c9", "pol_9bede8d9b67e"],
  policy_names: { pol_8be25bb490c9: "hr_users_call_tools", pol_9bede8d9b67e: "test_payout_admin_only" },
  policy_name: "hr_users_call_tools", source: "gateway", stage: null,
});
const turn: ChatMessage[] = [
  { kind: "user", text: "给 EMP-1 发 800" },
  { kind: "tool", text: "hr-database___create_payout", name: "hr-database___create_payout", toolId: "t-1", toolStatus: "error", toolResult: RAW },
  { kind: "policy", text: RAW, name: "hr-database___create_payout", deny },
  { kind: "agent", text: "这次发薪被策略拒绝了。" },
];

const mount = (messages: ChatMessage[]) =>
  render(<MemoryRouter><Thread messages={messages} userLabel="我" agentLabel="助手" restoring={false} /></MemoryRouter>);

describe("Thread — a Gateway-denied tool call (N17-obs507)", () => {
  it("collapses the failed tool card's English result and shows the Chinese deny card naming both policies", () => {
    mount(turn);
    const collapsed = screen.getByTestId("tool-result-collapsed") as HTMLDetailsElement;
    expect(collapsed.open).toBe(false);
    expect(collapsed.querySelector("summary")?.textContent).toBe(zh.v2.chat.toolResultDenied);
    expect(screen.getByTestId("tool-result").textContent).toBe(RAW);
    // the card is Chinese, names BOTH policies, keeps the English text under its own toggle
    expect(screen.getByTestId("policy-deny-card").textContent).toContain(zh.v2.chat.policyDeny.title);
    expect(screen.getByTestId("policy-deny-summary").textContent).toBe("由策略「hr_users_call_tools、test_payout_admin_only」拒绝。");
    expect(screen.getByTestId("policy-deny-policy").textContent).toContain("pol_9bede8d9b67e");
    expect((screen.getByTestId("policy-deny-raw") as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByTestId("policy-deny-reason").textContent).toBe(RAW);
    // no generic error row: the denial is a card, not a red line
    expect(document.querySelector(".v2-chat-error")).toBeNull();
  });

  it("keeps a failed tool card expanded when no denial follows it, or when the denial is for another tool", () => {
    mount([turn[0], turn[1]]);
    expect(screen.queryByTestId("tool-result-collapsed")).toBeNull();
    expect(screen.getByTestId("tool-result").textContent).toContain(RAW);
    cleanup();
    mount([turn[0], turn[1], { ...turn[2], name: "other", deny: { ...deny, tool: "other" } }]);
    expect(screen.queryByTestId("tool-result-collapsed")).toBeNull();
    cleanup();
    // a denial in the NEXT turn does not reach back
    mount([turn[0], turn[1], { kind: "user", text: "again" }, turn[2]]);
    expect(screen.queryByTestId("tool-result-collapsed")).toBeNull();
  });
});
