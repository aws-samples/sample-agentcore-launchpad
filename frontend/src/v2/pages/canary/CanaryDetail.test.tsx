// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../../auth/auth-context";
import type { RuntimeCanaryInfo } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { CanaryDetail } from "./CanaryDetail";

/**
 * N17-obs427: the traffic action offers `sessions_per_arm`, defaulted to what the fixed-n
 * design still needs on the thinner arm (reaching planned_n took several one-pass rounds), and
 * 推广到生产 / advance — outside the retry path too — is creator-or-admin like every other
 * action (the server answers 403 `canary.forbidden`).
 */

let canary: RuntimeCanaryInfo;
const calls: Record<string, unknown>[] = [];

vi.mock("../../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/api")>()),
  api: {
    getRuntimeCanary: () => Promise.resolve(canary),
    runtimeCanaryAction: (_id: string, input: Record<string, unknown>) => {
      calls.push(input);
      return Promise.resolve({ canary });
    },
    v2Datasets: () => Promise.resolve({ datasets: [{ id: "ds1", name: "hr-faq", item_count: 9, kind: "local" }] }),
    v2Evaluators: () => Promise.resolve({ evaluators: [] }),
  },
}));

const auth = (over: Partial<AuthContextValue>): AuthContextValue => ({
  authRequired: true, username: "bob", userId: "u-bob", role: "member", email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined, ...over,
});
const MEMBER = auth({});
const CREATOR = auth({ username: "alice", userId: "u-alice" });
const ADMIN = auth({ username: "root", userId: null, role: "admin", isAdmin: true });

function make(stage: number, verdict?: Record<string, unknown>, design = true): RuntimeCanaryInfo {
  return {
    id: "c1", name: "c1", agent_id: "a1", status: "running", running_action: null, error: null,
    progress: null, created_by: "u-alice", created_by_name: "alice",
    artifacts: {
      ...(design ? { design: { mde: 0.2, sd: 0.3, alpha: 0.05, power: 0.8, planned_n_per_arm: 30 } } : {}),
      setup: { gateway_id: "invoke-chain", ab_test_id: "local_c1", ramp_stage: stage, v_current: "1", v_candidate: "2" },
      rounds: [{
        ramp_stage: stage, weights: {},
        traffic_attempts: [{ sent: 18, failed: 0, baseline_n: 0, arms: { C: 10, T1: 8 } }],
        ...(verdict ? { verdict: { metrics: [], n: 20, ...verdict } } : {}),
      }],
    },
  } as unknown as RuntimeCanaryInfo;
}

function mount(value: AuthContextValue) {
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={["/v2/eval?mode=canary&id=c1"]}>
        <CanaryDetail id="c1" />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const perArm = () => screen.findByTestId("v2-canary-sessions-per-arm") as Promise<HTMLInputElement>;
const button = (action: string) => screen.findByTestId(`v2-canary-action-${action}`) as Promise<HTMLButtonElement>;

beforeAll(async () => {
  Element.prototype.scrollIntoView = () => undefined;
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
beforeEach(() => {
  calls.length = 0;
});
afterEach(() => cleanup());

describe("canary traffic sessions_per_arm (N17-obs427)", () => {
  it("defaults to the design's remaining n on the thinner arm and sends it", async () => {
    canary = make(0, { verdict: "insufficient-n", significant: false, arms_n: { control: 12, treatment: 7 }, min_arm_n: 30 });
    mount(CREATOR);
    await waitFor(async () => expect((await perArm()).value).toBe("23"));
    await waitFor(async () => expect((await button("traffic")).disabled).toBe(false));
    fireEvent.click(await button("traffic"));
    await waitFor(() => expect(calls).toEqual([{ action: "traffic", dataset_id: "ds1", sessions_per_arm: 23 }]));
  });

  it("before any verdict counts the sessions the round's attempts sent per arm", async () => {
    canary = make(0);
    mount(CREATOR);
    await waitFor(async () => expect((await perArm()).value).toBe("22"));
  });

  it("is editable, empty = one pass, and an out-of-range value disables the send", async () => {
    canary = make(0);
    mount(CREATOR);
    const input = await perArm();
    fireEvent.change(input, { target: { value: "0" } });
    await waitFor(async () => expect((await button("traffic")).disabled).toBe(true));
    fireEvent.change(input, { target: { value: "" } });
    await waitFor(async () => expect((await button("traffic")).disabled).toBe(false));
    fireEvent.click(await button("traffic"));
    await waitFor(() => expect(calls).toEqual([{ action: "traffic", dataset_id: "ds1" }]));
  });

  it("without a design the field starts empty (the server's one dataset pass)", async () => {
    canary = make(0, undefined, false);
    mount(CREATOR);
    expect((await perArm()).value).toBe("");
    expect(screen.getByText(en.v2.canary.traffic.perArmHint)).toBeTruthy();
  });
});

describe("推广到生产 is creator-or-admin (N17-obs427)", () => {
  const win = { verdict: "treatment-wins", significant: true, arms_n: { control: 30, treatment: 30 }, min_arm_n: 30 };

  it("is disabled with the creator-only hint for a member who did not start the canary", async () => {
    canary = make(2, win);
    mount(MEMBER);
    const complete = await button("complete");
    expect(complete.textContent).toBe(en.canaryPage.complete);
    expect(complete.disabled).toBe(true);
    expect(complete.title).toBe(en.v2.common.creatorOnly);
  });

  it("stays enabled for the creator and for an administrator", async () => {
    for (const who of [CREATOR, ADMIN]) {
      canary = make(2, win);
      mount(who);
      expect((await button("complete")).disabled).toBe(false);
      cleanup();
    }
  });

  it("the advance button follows the same rule", async () => {
    canary = make(0, win);
    mount(MEMBER);
    expect((await button("advance")).disabled).toBe(true);
  });
});

describe("verdict tag and reason copy (N17-obs443)", () => {
  it("shows the verdict's name with the raw code as a tooltip and the worded reason", async () => {
    canary = make(0, { verdict: "insufficient-data", reason: "no new evaluator samples arrived after current-stage traffic",
      reason_code: "no_new_samples", reason_params: {} });
    mount(CREATOR);
    const tag = await screen.findByTestId("v2-canary-verdict-0");
    expect(tag.textContent).toBe("Not enough data");
    expect(tag.getAttribute("title")).toBe("Verdict code: insufficient-data");
    expect(tag.textContent).not.toMatch(/INSUFFICIENT/);
    expect((await screen.findByTestId("v2-canary-reason-0")).textContent).toBe(
      "No new evaluator scores arrived after this stage's traffic.");
  });
});

describe("canary detail copy in zh: names, verdicts and versions, never raw ids (N17-obs446)", () => {
  const metric = (id: string, c: number, t: number) => ({
    evaluatorId: id, label: id, control: { mean: 0.94, sampleSize: c }, variants: [{ name: "T1", mean: 1, sampleSize: t }],
  });

  afterEach(async () => {
    await i18next.changeLanguage("en");
  });

  it("names the built-in, words the per-version counts and the override hint without codes or arm keys", async () => {
    i18next.addResourceBundle("zh-CN", "common", zh, true, true);
    await i18next.changeLanguage("zh-CN");
    // the EV-07 ch04 frame: insufficient-n at 10/90, C 17/16 · T1 3/16
    canary = make(0, {
      verdict: "insufficient-n", significant: false, arms_n: { control: 17, treatment: 3 }, min_arm_n: 16,
      metrics: [metric("Builtin.Helpfulness", 17, 3)],
    });
    mount(CREATOR);
    const name = await screen.findByText("有用性");
    expect(name.closest("[title]")?.getAttribute("title")).toBe("Builtin.Helpfulness");
    const arms = await screen.findByTestId("v2-canary-arm-hint-0");
    expect(arms.textContent).toContain("当前版本 17/16 · 候选版本 3/16");
    expect(arms.getAttribute("title")).toBe("内部臂标识：C = 当前版本，T1 = 候选版本");
    const override = await screen.findByTestId("v2-canary-override-hint");
    expect(override.textContent).toContain("「无明显胜者」、「评分会话不足」");
    expect(override.textContent).toContain("「当前版本胜出」");
    const body = document.body.textContent ?? "";
    for (const raw of ["Builtin.Helpfulness", "insufficient-n", "control-wins", "C 17", "T1 3"]) {
      expect(body, raw).not.toContain(raw);
    }
  });
});


describe("running action progress copy (N17-obs447)", () => {
  it("words the coded progress line instead of the English text", async () => {
    canary = { ...make(0), running_action: "traffic",
      progress: "dispatching replay of 20 sessions to the evaluation worker…",
      progress_code: "dispatching_replay", progress_params: { sessions: 20 } } as unknown as RuntimeCanaryInfo;
    mount(CREATOR);
    expect((await screen.findAllByText("Sending 20 sessions to the evaluation worker for replay…")).length).toBeGreaterThan(0);
    expect(screen.queryByText("dispatching replay of 20 sessions to the evaluation worker…")).toBeNull();
  });
});

describe("completion summary (N17-obs451)", () => {
  it("a tie promoted with the override does not read as a win", async () => {
    const base = make(2, { verdict: "tie", significant: false });
    canary = { ...base, status: "completed",
      artifacts: { ...base.artifacts, complete: { winner: "challenger", ab_test_status: "STOPPED", completed_at: "",
        promoted_version: "7", completed_at_stage: 2, verdict: "tie", significant: false, override: true } } } as unknown as RuntimeCanaryInfo;
    mount(CREATOR);
    const summary = await screen.findByTestId("v2-canary-summary");
    expect(summary.textContent).toContain("No clear winner");
    expect(summary.textContent).toContain("operator's override");
    expect(summary.textContent).not.toMatch(/candidate won/i);
  });
});

describe("cleanup waits for a decision (N17-obs452)", () => {
  const button = () => screen.findByTestId("v2-canary-cleanup") as Promise<HTMLButtonElement>;

  it("disables 清理 on a running canary whose setup moved production to the candidate", async () => {
    const base = make(0);
    canary = { ...base, artifacts: { ...base.artifacts, setup: { ...base.artifacts.setup, v_candidate: "2" } } } as unknown as RuntimeCanaryInfo;
    mount(CREATOR);
    const cleanup = await button();
    expect(cleanup.disabled).toBe(true);
    expect(cleanup.getAttribute("title")).toBe(en.canaryPage.cleanupNeedsDecision);
  });

  it("leaves it enabled once the canary is rolled back, and for a setup that never minted", async () => {
    const base = make(0);
    canary = { ...base, status: "rolled_back", artifacts: { ...base.artifacts,
      setup: { ...base.artifacts.setup, v_candidate: "2" }, rollback: { winner: "champion", restored_version: "9" } } } as unknown as RuntimeCanaryInfo;
    mount(CREATOR);
    expect((await button()).disabled).toBe(false);
    cleanup();
    const unminted = make(0);
    const partialSetup: Record<string, unknown> = { ...(unminted.artifacts.setup as Record<string, unknown>) };
    delete partialSetup.v_candidate;
    canary = { ...unminted, artifacts: { ...unminted.artifacts, setup: partialSetup } } as unknown as RuntimeCanaryInfo;
    mount(CREATOR);
    expect((await button()).disabled).toBe(false);
  });
});
