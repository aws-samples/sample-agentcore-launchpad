// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../../auth/auth-context";
import type { OnlineEvalConfigRow } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { OnlineDetail } from "./OnlineDetail";
import { OnlineList } from "./OnlineList";

/** N17-obs438: a paused online config is RESUMED (恢复 / Resume) — the button and its dialog
 *  say the same verb; it used to read 「启动」 next to a 「恢复在线评估」 dialog.
 *  N17-obs440: edit / pause / resume / delete are creator-or-admin on the server (obs426) — a
 *  member who did not create the config sees them disabled with the creator-only hint; a NULL
 *  creator is an administrator's; the creator and administrators are unaffected. */

const PAUSED = {
  config_id: "oe_hr-1", arn: null, name: "oe_hr", description: "", owner: "agent",
  status: "ACTIVE", execution_status: "DISABLED", failure_reason: null, agent_id: "a1",
  agent_name: "hr", matched_agent: null, detailed: true, mode: "scores",
  evaluators: ["Builtin.Helpfulness"], sampling_percentage: 10, session_timeout_minutes: 5,
  filter_count: 0, filters: [], data_source: { log_groups: [], service_name: null }, insights: [],
  clustering_frequencies: [], execution_role_arn: null, results_log_group: "", duplicate_enabled: false,
  created_at: null, updated_at: null, created_by: null, created_by_name: null,
} as unknown as OnlineEvalConfigRow;
let configs: OnlineEvalConfigRow[] = [PAUSED];

// N17-obs475: every mocked read is tracked, so a test asserts once the page's reads have
// ANSWERED and been applied (`settled`) instead of racing `findBy*`'s 1 s deadline — on a
// loaded host (load ≈ 133) 「恢复」 was not found in time.
const inflight = vi.hoisted(() => [] as Promise<unknown>[]);
const tracked = vi.hoisted(() => <T,>(value: () => T) => () => {
  const p = Promise.resolve(value());
  inflight.push(p);
  return p;
});

async function settled() {
  for (let round = 0; round < 50; round++) {
    const calls = inflight.length;
    await act(async () => {
      await Promise.allSettled(inflight);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    if (inflight.length === calls) return;
  }
  throw new Error("the page kept issuing reads");
}

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      v2OnlineConfigs: tracked(() => ({ configs, total: configs.length })),
      v2OnlineConfig: tracked(() => configs[0]),
      v2OnlineResults: tracked(() => ({ recent: [], evaluators: [], errors: null })),
      v2Evaluators: tracked(() => ({ evaluators: [], builtin_count: 0 })),
      // the detail page's report list — answered here, never the network
      onlineEvalReports: tracked(() => ({ reports: [], unattributed: [] })),
    },
  };
});

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(async () => {
  cleanup();
  inflight.length = 0;
  configs = [PAUSED];
  await i18next.changeLanguage("zh-CN");
});

const ADMIN: AuthContextValue = {
  authRequired: true, username: "admin", userId: "u-admin", role: "admin", email: null,
  accountExpiresAt: null, isAdmin: true, can: () => true, logout: async () => undefined,
};
const member = (userId: string): AuthContextValue => ({ ...ADMIN, username: userId, userId, role: "member", isAdmin: false });

function mount(auth?: AuthContextValue, page: "list" | "detail" = "list") {
  const body = (
    <MemoryRouter>
      {page === "list" ? <OnlineList /> : <OnlineDetail id={configs[0].config_id} />}
    </MemoryRouter>
  );
  render(auth ? <AuthContext.Provider value={auth}>{body}</AuthContext.Provider> : body);
}

const CREATOR_ONLY = "只有创建者或管理员才能更改。";

describe("resuming a paused online config (N17-obs438)", () => {
  it("reads 恢复 on the button and in its dialog", async () => {
    mount();
    await settled();
    const button = screen.getByRole("button", { name: "恢复" });
    expect(screen.queryByRole("button", { name: "启动" })).toBeNull();
    fireEvent.click(button);
    await settled();
    expect(screen.getByText("恢复在线评估")).toBeTruthy();
  });

  it("reads Resume in en", async () => {
    await i18next.changeLanguage("en");
    mount();
    await settled();
    expect(screen.getByRole("button", { name: "Resume" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
  });
});

describe("creator-only online actions (N17-obs440)", () => {
  const actions = ["编辑", "恢复", "删除"];

  it("disables edit / resume / delete for a member who did not create the config, with the hint", async () => {
    configs = [{ ...PAUSED, created_by: "u-ada", created_by_name: "ada" }];
    mount(member("u-bob"));
    await settled();
    for (const name of actions) {
      const button = screen.getByRole("button", { name });
      expect((button as HTMLButtonElement).disabled, name).toBe(true);
      expect(button.getAttribute("title"), name).toBe(CREATOR_ONLY);
    }
    expect((screen.getByRole("button", { name: "查看" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("treats a config with no recorded creator as an administrator's", async () => {
    mount(member("u-bob"));  // PAUSED: created_by null
    await settled();
    expect(((screen.getByRole("button", { name: "恢复" })) as HTMLButtonElement).disabled).toBe(true);
  });

  it("leaves the creator and administrators unaffected", async () => {
    configs = [{ ...PAUSED, created_by: "u-ada", created_by_name: "ada" }];
    for (const auth of [member("u-ada"), ADMIN]) {
      mount(auth);
      await settled();
      for (const name of actions) {
        const button = screen.getByRole("button", { name });
        expect((button as HTMLButtonElement).disabled, `${auth.username} ${name}`).toBe(false);
        expect(button.getAttribute("title")).toBeNull();
      }
      cleanup();
    }
  });

  it("disables the detail page's actions for a non-creator and says who may act", async () => {
    configs = [{ ...PAUSED, execution_status: "ENABLED", created_by: "u-ada", created_by_name: "ada" }];
    mount(member("u-bob"), "detail");
    await settled();
    const pause = screen.getByRole("button", { name: "暂停" });
    expect((pause as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("v2-online-edit") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("v2-online-creator-only").textContent).toContain("ada");
    cleanup();
    mount(member("u-ada"), "detail");
    await settled();
    expect(((screen.getByRole("button", { name: "暂停" })) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByTestId("v2-online-creator-only")).toBeNull();
  });
});

describe("online task name (N17-obs441)", () => {
  const NAMED = { ...PAUSED, name: "oe_hr_eval_demo_edb3d0", config_id: "oe_hr_eval_demo_edb3d0-0a1b2c3d4e",
                  description: "HR 帮助台在线评估" } as OnlineEvalConfigRow;

  it("shows the 任务描述 as the name, the generated name as tooltip — as the task list does", async () => {
    const { taskFromOnline } = await import("../../tasks");
    expect(taskFromOnline(NAMED).name).toBe("HR 帮助台在线评估");
    configs = [NAMED];
    mount();
    await settled();
    const link = screen.getByText("HR 帮助台在线评估");
    expect(link.getAttribute("title")).toBe("oe_hr_eval_demo_edb3d0");
    cleanup();
    mount(undefined, "detail");
    await settled();
    const shown = screen.getAllByText("HR 帮助台在线评估");
    expect(shown.length).toBeGreaterThanOrEqual(2); // header + 任务名称 row (+ 描述)
    expect(shown.some((el) => el.closest("[title]")?.getAttribute("title") === "oe_hr_eval_demo_edb3d0")).toBe(true);
  });

  it("falls back to the generated name when there is no description", async () => {
    configs = [{ ...NAMED, description: "  " }];
    mount();
    await settled();
    expect(screen.getByText("oe_hr_eval_demo_edb3d0")).toBeTruthy();
  });
});

/** N17-obs445: a finished canary's ENABLED arm config shows 暂停 (creator-or-admin against
 *  the canary's creator); a running canary's arm stays read-only — no pause, resume, edit or
 *  delete — and nothing else changes on arm rows. */
describe("pausing a finished canary's arm (N17-obs445)", () => {
  const arm = (status: string, terminal: boolean) => ({
    ...PAUSED, config_id: "can_50564110_oet-x", name: "can_50564110_oet", owner: "experiment",
    execution_status: "ENABLED",
    owner_run: { kind: "canary", id: "50564110a7b5", status, terminal, created_by: "u-ada",
      created_by_name: "ada" },
  }) as unknown as OnlineEvalConfigRow;

  for (const page of ["list", "detail"] as const) {
    it(`offers 暂停 on a rolled-back canary's arm (${page})`, async () => {
      configs = [arm("rolled_back", true)];
      mount(member("u-ada"), page);
      await settled();
      const pause = screen.getByRole("button", { name: "暂停" });
      expect((pause as HTMLButtonElement).disabled).toBe(false);
      for (const name of ["恢复", "编辑", "删除"]) expect(screen.queryByRole("button", { name })).toBeNull();
    });

    it(`keeps a running canary's arm read-only (${page})`, async () => {
      configs = [arm("running", false)];
      mount(ADMIN, page);
      await settled();
      screen.getAllByText(/can_50564110_oet|50564110/);
      for (const name of ["暂停", "恢复", "编辑", "删除"]) expect(screen.queryByRole("button", { name })).toBeNull();
    });
  }

  it("disables the arm's 暂停 for a member who did not create the canary", async () => {
    configs = [arm("completed", true)];
    mount(member("u-bob"));
    await settled();
    const pause = screen.getByRole("button", { name: "暂停" });
    expect((pause as HTMLButtonElement).disabled).toBe(true);
    expect(pause.getAttribute("title")).toBe(CREATOR_ONLY);
  });
});
