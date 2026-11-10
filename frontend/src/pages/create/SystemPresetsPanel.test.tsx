// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../auth/auth-context";
import { ToastProvider } from "../../components";
import type { SystemPresetInfo, SystemPresetInstallResult, Workspace } from "../../lib/api";
import en from "../../locales/en/common.json";
import zh from "../../locales/zh-CN/common.json";
import { WorkspaceContext } from "../../workspace/workspace-context";
import { SystemPresetsPanel } from "./SystemPresetsPanel";

/**
 * N17-obs234: an ACTIVE preset whose installed model-catalog pin is behind the ledger's
 * stamp (`snapshot_behind`) offers "Repair snapshot" on its card. The click posts the same
 * bodiless install (`{}`, pinned to the workspace the panel was read in) — the SERVER
 * decides to re-publish — and toasts `repairStarted`. Without the verdict the card is as
 * before: no repair button, no behind note.
 */

const sys = vi.hoisted(() => ({
  listSystemPresets: vi.fn(),
  installSystemPreset: vi.fn(),
  uninstallSystemPreset: vi.fn(),
  getSystemPresetJob: vi.fn(),
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, api: { ...actual.api, ...sys } };
});

const KEY = "eval-worker";

function preset(over: Partial<SystemPresetInfo> = {}): SystemPresetInfo {
  return {
    key: KEY,
    name: "eval-worker",
    label: "Evaluation worker",
    description: "runs evaluations",
    method: "harness",
    skill_version: "1.0.0",
    installed_skill_version: "1.0.0",
    update_available: false,
    status: "active",
    requirements: [],
    name_collision: null,
    agent_id: "a-eval",
    agent_status: "active",
    error: null,
    last_deploy_failed: false,
    job_id: "job-1",
    deployment_id: "dep-1",
    deployment_status: "succeeded",
    version: "3",
    release: { version: "1.0.0", digest: "d".repeat(64), tag: "1.0.0-dddddddddddd" },
    release_error: null,
    installed_release: "1.0.0-dddddddddddd",
    catalog_version: { installed: 20, current: 20 },
    snapshot_behind: false,
    last_job: null,
    model_id: "global.zai.glm-5.3",
    model_source: "bedrock",
    knowledge_bases: [],
    allowed_tools: ["kb_search"],
    memory: "disabled",
    settings: {
      model_id: "global.zai.glm-5.3", model_source: "bedrock", max_tokens: null, reasoning_effort: null,
      system_prompt: "", max_iterations: 10, timeout_seconds: 300, knowledge_bases: [],
    },
    defaults: {
      model_id: "global.zai.glm-5.3", model_source: "bedrock", max_tokens: null, reasoning_effort: null,
      system_prompt: "", max_iterations: 10, timeout_seconds: 300, knowledge_bases: [],
    },
    editable_fields: ["model_id"],
    operation: null,
    skill_registration: null,
    can_register_skill: true,
    can_install: false,
    can_repair: true,
    can_configure: true,
    can_uninstall: true,
    updated_at: "2026-10-21T08:00:00Z",
    ...over,
  };
}

const behind = (over: Partial<SystemPresetInfo> = {}) =>
  preset({ catalog_version: { installed: 10, current: 20 }, snapshot_behind: true, ...over });

const workspace = { id: "default", name: "default", bootstrap_status: "ready", is_default: true } as unknown as Workspace;

function auth(isAdmin: boolean): AuthContextValue {
  return {
    authRequired: true, username: isAdmin ? "ops" : "alice", userId: isAdmin ? null : "u-alice",
    role: isAdmin ? "admin" : "member", email: null,
    accountExpiresAt: null, isAdmin, can: () => isAdmin, logout: async () => undefined,
  };
}

const renderPanel = (isAdmin = true) =>
  render(
    <AuthContext.Provider value={auth(isAdmin)}>
      <WorkspaceContext.Provider
        value={{ workspaces: [workspace], current: workspace, allWorkspaces: isAdmin, select: () => undefined, refresh: async () => undefined }}
      >
        <ToastProvider>
          <MemoryRouter>
            <SystemPresetsPanel />
          </MemoryRouter>
        </ToastProvider>
      </WorkspaceContext.Provider>
    </AuthContext.Provider>,
  );

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common", interpolation: { escapeValue: false },
  });
});

beforeEach(() => {
  for (const fn of Object.values(sys)) fn.mockReset();
  sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [preset()] }));
});
afterEach(cleanup);

describe("SystemPresetsPanel — Repair snapshot (N17-obs234)", () => {
  it("shows no repair button and no behind note while the snapshot is current", async () => {
    renderPanel();
    await screen.findByTestId(`system-preset-${KEY}`);
    expect(screen.queryByTestId(`repair-snapshot-${KEY}`)).toBeNull();
    expect(screen.queryByTestId("preset-snapshot-behind")).toBeNull();
    // the existing actions are untouched
    expect(screen.getByTestId(`uninstall-${KEY}`)).toBeTruthy();
    expect(screen.queryByTestId(`install-${KEY}`)).toBeNull();
  });

  it("shows the button only for an ACTIVE preset that is behind — a deploying one shows the usual state", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({
      workspace_id: "default",
      presets: [behind(), { ...behind(), key: "aws-agent-solution-architect", label: "Architect", status: "deploying", agent_status: "deploying" }],
    }));
    renderPanel();
    const btn = await screen.findByTestId(`repair-snapshot-${KEY}`);
    expect(btn.textContent).toBe(en.create.system.repairSnapshot);
    expect(btn.className).toContain("primary");
    expect((btn as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByTestId("preset-snapshot-behind").textContent).toContain(
      en.create.system.snapshotBehind.replace("{{installed}}", "10").replace("{{current}}", "20"),
    );
    expect(screen.queryByTestId("repair-snapshot-aws-agent-solution-architect")).toBeNull();
  });

  it("click posts the bodiless install pinned to the workspace and toasts repairStarted", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [behind()] }));
    const deploying = preset({ status: "deploying", agent_status: "deploying", job_id: "job-2", snapshot_behind: true, catalog_version: { installed: 10, current: 20 } });
    sys.installSystemPreset.mockImplementation(async () => {
      // the ledger now holds the deploying row — the follow-up status read sees it too
      sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [deploying] }));
      return {
        agent: { id: "a-eval", status: "deploying" }, job_id: "job-2", deployment_id: "dep-2",
        created: false, changed: true, preset: deploying,
      } as unknown as SystemPresetInstallResult;
    });
    renderPanel();
    fireEvent.click(await screen.findByTestId(`repair-snapshot-${KEY}`));
    await waitFor(() => expect(sys.installSystemPreset).toHaveBeenCalledTimes(1));
    expect(sys.installSystemPreset).toHaveBeenCalledWith(KEY, {}, "default"); // no force: the server decides
    const toast = await screen.findByRole("status");
    expect(toast.textContent).toContain(en.create.system.repairStarted.replace("{{name}}", "Evaluation worker"));
    expect(toast.textContent).not.toContain(en.create.system.alreadyCurrent.replace("{{name}}", "Evaluation worker"));
    // the response's deploying row is rendered at once: the usual in-flight state, no repair button
    await waitFor(() => expect(screen.getByTestId(`system-preset-${KEY}`).getAttribute("data-status")).toBe("deploying"));
    expect(screen.queryByTestId(`repair-snapshot-${KEY}`)).toBeNull();
  });

  it("a member sees the button disabled with the administrator hint (the server refuses anyway)", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [behind({ can_repair: false, can_configure: false, can_uninstall: false })] }));
    renderPanel(false);
    const btn = await screen.findByTestId(`repair-snapshot-${KEY}`);
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    expect(btn.getAttribute("title")).toBe(en.create.system.adminOnly);
    fireEvent.click(btn);
    expect(sys.installSystemPreset).not.toHaveBeenCalled();
  });

  it("uses the zh-CN copy 「修复快照」 under zh-CN", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [behind()] }));
    await i18next.changeLanguage("zh-CN");
    try {
      renderPanel();
      expect((await screen.findByTestId(`repair-snapshot-${KEY}`)).textContent).toBe("修复快照");
    } finally {
      await i18next.changeLanguage("en");
    }
  });
});

describe("SystemPresetsPanel — model default changed (N17-obs202)", () => {
  const moved = (over: Partial<SystemPresetInfo> = {}) =>
    preset({
      model_origin: "default",
      default_changed: true,
      defaults: { ...preset().defaults, model_id: "global.moonshotai.kimi-k3" },
      ...over,
    });

  it("an active preset that follows the role shows the note and the one repair button", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [moved()] }));
    renderPanel();
    const note = await screen.findByTestId("preset-default-changed");
    expect(note.textContent).toContain(
      en.create.system.defaultChanged
        .replace("{{installed}}", "global.zai.glm-5.3")
        .replace("{{current}}", "global.moonshotai.kimi-k3"),
    );
    expect(screen.queryByTestId("preset-snapshot-behind")).toBeNull(); // the catalog pin is current
    const btn = screen.getByTestId(`repair-snapshot-${KEY}`);
    expect(btn.textContent).toBe(en.create.system.repairSnapshot);
    expect((btn as HTMLButtonElement).disabled).toBe(false);
    expect(btn.getAttribute("title")).toBe(en.create.system.repairDefaultHint);
    // the click is the same bodiless install — the server applies the new default
    const deploying = moved({ status: "deploying", agent_status: "deploying", job_id: "job-2", default_changed: false });
    sys.installSystemPreset.mockImplementation(async () => ({
      agent: { id: "a-eval", status: "deploying" }, job_id: "job-2", deployment_id: "dep-2",
      created: false, changed: true, preset: deploying,
    } as unknown as SystemPresetInstallResult));
    fireEvent.click(btn);
    await waitFor(() => expect(sys.installSystemPreset).toHaveBeenCalledTimes(1));
    expect(sys.installSystemPreset).toHaveBeenCalledWith(KEY, {}, "default");
  });

  it("both notes, one button when the snapshot is behind AND the default moved", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [moved({ catalog_version: { installed: 10, current: 20 }, snapshot_behind: true })] }));
    renderPanel();
    await screen.findByTestId("preset-default-changed");
    expect(screen.getByTestId("preset-snapshot-behind")).toBeTruthy();
    expect(screen.getAllByTestId(`repair-snapshot-${KEY}`)).toHaveLength(1);
    expect(screen.getByTestId(`repair-snapshot-${KEY}`).getAttribute("title")).toBe(en.create.system.repairSnapshotHint);
  });

  it("an administrator override never shows the note; neither does a deploying preset", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({
      workspace_id: "default",
      presets: [
        preset({ model_origin: "override", default_changed: false, model_id: "global.moonshotai.kimi-k3" }),
        { ...moved(), key: "aws-agent-solution-architect", label: "Architect", status: "deploying", agent_status: "deploying" },
      ],
    }));
    renderPanel();
    await screen.findByTestId(`system-preset-${KEY}`);
    expect(screen.queryByTestId("preset-default-changed")).toBeNull();
    expect(screen.queryByTestId(`repair-snapshot-${KEY}`)).toBeNull();
    expect(screen.queryByTestId("repair-snapshot-aws-agent-solution-architect")).toBeNull();
  });

  it("zh-CN renders the note with full-width punctuation", async () => {
    await i18next.changeLanguage("zh-CN");
    try {
      sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [moved()] }));
      renderPanel();
      const note = await screen.findByTestId("preset-default-changed");
      expect(note.textContent).toContain("当前的 Agent 默认模型为 global.moonshotai.kimi-k3。");
    } finally {
      await i18next.changeLanguage("en");
    }
  });
});
