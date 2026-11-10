// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentInfo } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { CanaryStart } from "./CanaryStart";
import { canaryEligible, canaryHandoffHref, readCanarySeed, stashCanarySeed } from "./common";

/**
 * N17-obs309: 用金丝雀验证 on a prompt recommendation opens 新建金丝雀 with
 * `champion=<agent>&rec=<recommendation id>` and the recommended prompt in sessionStorage.
 * The page seeds the candidate with it (still editable); another champion reads its own spec.
 */

const agent = (id: string, prompt: string, eligible = true): AgentInfo =>
  ({
    id, name: id, method: "zip_runtime", status: "active", spec: { system_prompt: prompt },
    canary_capability: { eligible, reason: eligible ? null : "no", reason_code: eligible ? null : "method" },
  }) as unknown as AgentInfo;

let agents: AgentInfo[] = [];

vi.mock("../../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/api")>()),
  api: {
    listAgents: () => Promise.resolve({ agents }),
    v2Evaluators: () => Promise.resolve({ evaluators: [] }),
  },
}));

beforeAll(async () => {
  Element.prototype.scrollIntoView = () => undefined; // jsdom has none; the listbox scrolls its active option
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

beforeEach(() => {
  agents = [agent("first", "first spec prompt"), agent("champ", "champion spec prompt")];
  window.sessionStorage.clear();
});

afterEach(() => cleanup());

const renderAt = (url: string) =>
  render(
    <MemoryRouter initialEntries={[url]}>
      <CanaryStart />
    </MemoryRouter>,
  );
const prompt = () => screen.getByTestId("v2-canary-prompt") as HTMLTextAreaElement;

describe("新建金丝雀 hand-off from a prompt recommendation (N17-obs309)", () => {
  it("seeds the candidate with the recommended prompt for the hand-off champion, editable", async () => {
    stashCanarySeed("rec-9", "recommended prompt");
    renderAt(canaryHandoffHref("champ", "rec-9"));
    await waitFor(() => expect(prompt().value).toBe("recommended prompt"));
    fireEvent.change(prompt(), { target: { value: "recommended prompt, edited" } });
    expect(prompt().value).toBe("recommended prompt, edited");
  });

  it("picking another champion reads that agent's spec", async () => {
    stashCanarySeed("rec-9", "recommended prompt");
    renderAt(canaryHandoffHref("champ", "rec-9"));
    await waitFor(() => expect(prompt().value).toBe("recommended prompt"));
    fireEvent.click(screen.getByTestId("v2-canary-agent"));
    fireEvent.click(screen.getByText("first · zip_runtime"));
    await waitFor(() => expect(prompt().value).toBe("first spec prompt"));
  });

  it("without a stashed prompt the champion's own spec seeds the candidate", async () => {
    renderAt(canaryHandoffHref("champ", "rec-missing"));
    await waitFor(() => expect(prompt().value).toBe("champion spec prompt"));
  });

  it("a plain 新建金丝雀 ignores stashed prompts", async () => {
    stashCanarySeed("rec-9", "recommended prompt");
    renderAt("/v2/eval/experiments?canary=new");
    await waitFor(() => expect(prompt().value).toBe("first spec prompt"));
  });
});

describe("canary hand-off helpers", () => {
  it("builds the URL and round-trips the seed by recommendation id", () => {
    expect(canaryHandoffHref("a b", "r/1")).toBe("/v2/eval/experiments?canary=new&champion=a+b&rec=r%2F1");
    stashCanarySeed("r1", "p");
    expect(readCanarySeed("r1")).toBe("p");
    expect(readCanarySeed("r2")).toBeNull();
    expect(readCanarySeed(null)).toBeNull();
  });

  it("canaryEligible is the champion rule: active and marked eligible", () => {
    expect(canaryEligible(agent("x", ""))).toBe(true);
    expect(canaryEligible(agent("x", "", false))).toBe(false);
    expect(canaryEligible({ status: "active" })).toBe(false);
    expect(canaryEligible({ ...agent("x", ""), status: "deploying" } as AgentInfo)).toBe(false);
    expect(canaryEligible(null)).toBe(false);
  });
});

describe("experiment design before start (R3-d)", () => {
  it("shows the planned sessions per version and refuses a meaningless design", async () => {
    renderAt("/v2/experiments?mode=canary&view=start");
    await waitFor(() => expect(screen.getByTestId("v2-canary-design-planned").textContent)
      .toContain("36 sessions per version"));
    fireEvent.change(screen.getByTestId("v2-canary-design-mde"), { target: { value: "0.1" } });
    expect(screen.getByTestId("v2-canary-design-planned").textContent).toContain("142");
    fireEvent.change(screen.getByTestId("v2-canary-design-mde"), { target: { value: "0" } });
    expect(screen.queryByTestId("v2-canary-design-planned")).toBeNull();
    expect(screen.getByText(en.v2.canary.design.invalid)).toBeTruthy();
  });
});
