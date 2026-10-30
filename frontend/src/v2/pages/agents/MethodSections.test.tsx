// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { emptyAgentForm } from "../../../lib/agent-spec";
import type { MethodCapabilities } from "../../../lib/capabilities";
import en from "../../../locales/en/common.json";
import zhCN from "../../../locales/zh-CN/common.json";
import { HarnessToolsCard } from "./MethodSections";
import type { WizardCatalogs } from "./wizardKit";

/** N15-obs16 / round 16: the Harness tools card offers a builtin (and a native tool) only
 *  where `GET /api/capabilities.methods` says the method renders it; an unsupported one is
 *  disabled with the reason, and an unreadable declaration (null) offers everything and
 *  leaves the refusal to the server's 422 `agent.method_unsupported`. */
beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zhCN } }, defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(cleanup);

const HARNESS = {
  tool_types: ["builtin", "gateway", "mcp", "a2a"], tool_auth_modes: [], protocols: ["http"],
  memory: true, knowledge_bases: true, byoc_kinds: [], requires: ["artifacts_bucket"],
};
function caps(builtin_tools: string[], native_tools: string[] = ["shell", "file_operations"]): MethodCapabilities {
  return { harness: { ...HARNESS, builtin_tools, native_tools } };
}

function cat(methodCaps: WizardCatalogs["methodCaps"]): WizardCatalogs {
  return { loading: false, gatewayTargets: [], remoteMcp: [], agents: [], skills: [], kbCatalog: [], memories: [], methodCaps };
}

function renderCard(methodCaps: WizardCatalogs["methodCaps"]) {
  const form = emptyAgentForm("harness");
  render(<HarnessToolsCard form={form} set={() => undefined} cat={cat(methodCaps)} err={() => undefined} />);
  const boxes = screen.getByTestId("v2-agent-builtins").querySelectorAll<HTMLInputElement>("input[type=checkbox]");
  const natives = screen.getByTestId("v2-agent-native-tools").querySelectorAll<HTMLInputElement>("input[type=checkbox]");
  return { codeInterpreter: boxes[0], browser: boxes[1], shell: natives[0] };
}

describe("HarnessToolsCard builtin offers", () => {
  it("disables a builtin the method does not render and explains why", () => {
    const { codeInterpreter, browser, shell } = renderCard(caps(["code-interpreter"], ["file_operations"]));
    expect(codeInterpreter.disabled).toBe(false);
    expect(browser.disabled).toBe(true);
    const label = browser.closest("label")!;
    expect(label.className).toContain("disabled");
    expect(label.textContent).toContain(en.v2.agents.wizard.optionUnsupported);
    expect(label.getAttribute("title")).toBe(en.v2.agents.wizard.optionUnsupportedHint.replace("{{method}}", "harness"));
    expect(zhCN.v2.agents.wizard.optionUnsupportedHint.length).toBeGreaterThan(5);
    // the same rule for native tools
    expect(shell.disabled).toBe(true);
  });

  it("offers every option the method declares", () => {
    const { codeInterpreter, browser, shell } = renderCard(caps(["code-interpreter", "browser"]));
    expect(codeInterpreter.disabled).toBe(false);
    expect(browser.disabled).toBe(false);
    expect(shell.disabled).toBe(false);
    expect(browser.closest("label")!.getAttribute("title")).toBeNull();
  });

  it("offers everything while the declaration is unknown (server 422 is the gate)", () => {
    const { codeInterpreter, browser, shell } = renderCard(null);
    expect(codeInterpreter.disabled).toBe(false);
    expect(browser.disabled).toBe(false);
    expect(shell.disabled).toBe(false);
  });
});
