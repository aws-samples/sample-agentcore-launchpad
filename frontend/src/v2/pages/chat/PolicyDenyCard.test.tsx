// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import type { PolicyDecision } from "../../../lib/api";
import { decisionPolicyDeny, livePolicyDeny } from "../../../lib/policy-deny";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { PolicyDenyCard } from "./PolicyDenyCard";

/**
 * Policies decide TOOL CALLS only, so the card has one kind — a Gateway tool call the
 * interceptor denied for this identity. From a Decisions row it carries the determining policy
 * linked to its editor (the Policy page resolves the engine), the decision id linked to the
 * engine's Decisions tab, the attachment mode tag and the agent. (The default AuthContext is an
 * open console, so no provider is needed.)
 */
beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("en");
});

const ROW: PolicyDecision = {
  id: "dec_42", request_id: null, source: "gateway", channel: "chat", principal_kind: "user",
  principal_id: "hr-intern", principal_label: "hr-intern", action: "call_tool", resource_kind: "tool",
  resource_id: "hr-database___create_payout", agent_id: "agt_hr", session_id: null, stage: "bt", gateway_id: "gw_hr", engine_id: "pe_hr",
  decision: "deny", mode: "ENFORCE", policy_ids: ["pol_payout1"], policy_versions: { pol_payout1: 2 },
  bundle_version: null, reason: "Policy evaluation denied due to pol_payout1:v2", latency_ms: 4,
  created_at: "2026-10-27T09:00:00+08:00",
};

describe("PolicyDenyCard (a Decisions row)", () => {
  it("renders the tool denial with policy, decision id, attachment mode, agent and the row's identity, linked to the Policy page", () => {
    render(
      <MemoryRouter>
        <PolicyDenyCard deny={decisionPolicyDeny(ROW)} />
      </MemoryRouter>,
    );
    const card = screen.getByTestId("policy-deny-card");
    expect(card.textContent).toContain(en.v2.chat.policyDeny.title);
    expect(card.textContent).toContain("hr-database___create_payout");
    expect(screen.getByTestId("policy-deny-reason").textContent).toBe(ROW.reason);
    expect(card.textContent).toContain("pol_payout1");
    expect(card.textContent).toContain("dec_42");
    // the attachment mode (Global LOG_ONLY / ENFORCE) reads in the console language
    expect(screen.getByTestId("policy-deny-mode").textContent).toBe(en.v2.policy.attachMode.enforce);
    expect(screen.getByTestId("policy-deny-agent").textContent).toBe("agt_hr");
    // N17-obs509: the ROW's principal (an open console has no viewer identity at all)
    expect(screen.getByTestId("policy-deny-identity").textContent).toBe("hr-intern · user");
    // no posture gate any more (Global structure): the links are always there, direct when the
    // row names the engine
    expect(screen.getByTestId("policy-deny-link").getAttribute("href")).toBe("/v2/policy?view=policy&engine=pe_hr&policy=pol_payout1");
    expect(screen.getByTestId("policy-deny-decisions-link").getAttribute("href")).toBe("/v2/policy?view=engine&engine=pe_hr&tab=decisions");
  });

  it("falls back to the Policy page and hides what the frame did not carry", () => {
    render(
      <MemoryRouter>
        <PolicyDenyCard deny={livePolicyDeny({ tool: "t", reason: "denied" })} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("policy-deny-link").getAttribute("href")).toBe("/v2/policy");
    expect(screen.getByTestId("policy-deny-link").textContent).toContain(en.v2.chat.policyDeny.openPolicies);
    expect(screen.queryByTestId("policy-deny-decisions-link")).toBeNull();
    expect(screen.queryByTestId("policy-deny-mode")).toBeNull();
    expect(screen.queryByTestId("policy-deny-agent")).toBeNull();
    expect(screen.queryByTestId("policy-deny-policy")).toBeNull();
  });
});

/**
 * N17-obs510: the 策略 row lists every determining policy as its own chip — name + id each —
 * never run together on one line.
 */
describe("PolicyDenyCard — one chip per policy (N17-obs510)", () => {
  it("renders two chips for two determining policies, each with its name and id", () => {
    const deny = livePolicyDeny({
      tool: "hr-database___create_payout", reason: "denied", policy_id: "pol_a",
      policy_ids: ["pol_a", "pol_b"], policy_names: { pol_a: "hr_users_call_tools", pol_b: "test_payout_admin_only" },
      policy_name: "hr_users_call_tools", source: "gateway",
    });
    render(<MemoryRouter><PolicyDenyCard deny={deny} /></MemoryRouter>);
    const row = screen.getByTestId("policy-deny-policy");
    const chips = row.querySelectorAll(".v2-tag");
    expect(chips).toHaveLength(2);
    expect(screen.getByTestId("policy-deny-policy-pol_a").textContent).toBe("hr_users_call_tools pol_a");
    expect(screen.getByTestId("policy-deny-policy-pol_b").textContent).toBe("test_payout_admin_only pol_b");
    // a single policy is one chip too
    cleanup();
    render(<MemoryRouter><PolicyDenyCard deny={livePolicyDeny({ tool: "t", reason: "denied", policy_id: "pol_payout1:v2", policy_name: "发薪上限" })} /></MemoryRouter>);
    expect(screen.getByTestId("policy-deny-policy").querySelectorAll(".v2-tag")).toHaveLength(1);
    expect(screen.getByTestId("policy-deny-policy-pol_payout1").textContent).toBe("发薪上限 pol_payout1");
  });
});

/**
 * A6: the card names the denying POLICY and the engine STAGE in the console's language — 阶段 A /
 * 阶段 BT — and keeps the raw English engine text collapsed under a details toggle. The same card
 * renders in Chat and in the Decisions detail.
 */
const RAW = "Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to pol_payout1:v2]";

describe("PolicyDenyCard — Chinese deny card (A6)", () => {
  it("names the policy and the stage in Chinese, the English engine text collapsed", async () => {
    await i18next.changeLanguage("zh-CN");
    const deny = livePolicyDeny({
      tool: "hr-database___create_payout", reason: RAW, policy_id: "pol_payout1:v2", decision_id: "gw-7",
      policy_name: "发薪上限", stage: "bt", source: "gateway",
    });
    render(<MemoryRouter><PolicyDenyCard deny={deny} /></MemoryRouter>);
    expect(screen.getByTestId("policy-deny-summary").textContent).toBe("由策略「发薪上限」拒绝（阶段 BT）。");
    expect(screen.getByTestId("policy-deny-stage").textContent).toBe("阶段 BT");
    expect(screen.getByTestId("policy-deny-policy").textContent).toBe("发薪上限 pol_payout1");
    expect(screen.getByTestId("policy-deny-source").textContent).toBe("Gateway 拦截器");
    const raw = screen.getByTestId("policy-deny-raw") as HTMLDetailsElement;
    expect(raw.open).toBe(false);
    expect(raw.querySelector("summary")?.textContent).toBe("原始引擎文本（英文）");
    expect(screen.getByTestId("policy-deny-reason").textContent).toBe(RAW);
    // no English leaks into the visible copy
    const visible = (screen.getByTestId("policy-deny-card").textContent ?? "").replace(RAW, "").replace(/Gateway|Cedar|pol_payout1|gw-7|hr-database___create_payout/g, "");
    expect(visible).not.toMatch(/[A-Za-z]{4,}/);
  });

  it("a stage-A denial and a default deny read differently; a log-only observation is marked", async () => {
    await i18next.changeLanguage("zh-CN");
    const { unmount } = render(<MemoryRouter><PolicyDenyCard deny={livePolicyDeny({ tool: "t", reason: RAW, policy_id: "pol_payout1", stage: "a" })} /></MemoryRouter>);
    expect(screen.getByTestId("policy-deny-summary").textContent).toBe("由策略「pol_payout1」拒绝（阶段 A）。");
    unmount();
    render(<MemoryRouter><PolicyDenyCard deny={livePolicyDeny({ tool: "t", reason: "No policy applies to the request (denied by default).", policy_id: null, stage: "bt" })} /></MemoryRouter>);
    expect(screen.getByTestId("policy-deny-summary").textContent).toBe("没有策略允许此次调用（默认拒绝，阶段 BT）。");
    cleanup();
    render(<MemoryRouter><PolicyDenyCard deny={{ ...livePolicyDeny({ tool: "t", reason: RAW, policy_id: "pol_payout1", policy_name: "发薪上限" }), observed: true }} /></MemoryRouter>);
    expect(screen.getByTestId("policy-deny-summary").textContent).toBe("策略「发薪上限」将拒绝此次调用（仅记录，调用已执行）。");
    expect(screen.getByTestId("policy-deny-card").textContent).toContain("将拒绝（仅记录）");
  });

  it("a HISTORIC hub row (the retired invoke gate) renders with its source marked historic and no stage (English copy)", () => {
    const deny = decisionPolicyDeny(
      { ...ROW, id: "h1", source: "hub", stage: undefined, action: "invoke", resource_kind: "agent", resource_id: "agt_hr", mode: "enforce", policy_ids: ["pol_members:v1"], reason: "denied" },
      { pol_members: "members may invoke" },
    );
    render(<MemoryRouter><PolicyDenyCard deny={deny} /></MemoryRouter>);
    expect(screen.getByTestId("policy-deny-summary").textContent).toBe("Denied by policy members may invoke.");
    expect(screen.queryByTestId("policy-deny-stage")).toBeNull();
    expect(screen.getByTestId("policy-deny-source").textContent).toBe(en.v2.chat.policyDeny.source.hub);
    // the retired workspace mode on a historic row still reads
    expect(screen.getByTestId("policy-deny-mode").textContent).toBe(en.v2.policy.attachMode.enforce);
    expect(en.v2.chat.policyDeny.source.hub).toContain("historic");
  });

  it.each(["en", "zh-CN"])("names temporal version mismatch accurately in %s", async (locale) => {
    await i18next.changeLanguage(locale);
    render(<MemoryRouter><PolicyDenyCard deny={livePolicyDeny({
      tool: "t", reason: "temporal_set_mismatch", policy_id: null, stage: "a",
    })} /></MemoryRouter>);
    const copy = locale === "en" ? en : zh;
    expect(screen.getByTestId("policy-deny-summary").textContent)
      .toBe(copy.v2.chat.policyDeny.temporalMismatch);
    expect(screen.getByTestId("policy-deny-summary").textContent)
      .not.toBe(copy.v2.chat.policyDeny.byDefault);
  });

  it("without the A6 facts the card renders as before — no stage tag, no policy row", () => {
    render(<MemoryRouter><PolicyDenyCard deny={livePolicyDeny({ tool: "t", reason: "No policy applies to the request (denied by default).", policy_id: null })} /></MemoryRouter>);
    expect(screen.queryByTestId("policy-deny-stage")).toBeNull();
    expect(screen.queryByTestId("policy-deny-policy")).toBeNull();
    expect(screen.queryByTestId("policy-deny-source")).toBeNull();
    expect(screen.getByTestId("policy-deny-summary").textContent).toBe(en.v2.chat.policyDeny.byDefault);
  });
});
