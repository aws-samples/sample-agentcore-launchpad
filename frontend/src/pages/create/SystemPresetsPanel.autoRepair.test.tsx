// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../auth/auth-context";
import { ToastProvider } from "../../components";
import type { SystemPresetInfo, Workspace } from "../../lib/api";
import en from "../../locales/en/common.json";
import zh from "../../locales/zh-CN/common.json";
import { WorkspaceContext } from "../../workspace/workspace-context";
import { SystemPresetsPanel } from "./SystemPresetsPanel";

/**
 * N17-obs206: a deploying card whose running job the host's auto-repair sweep claimed
 * (`last_job.trigger === "auto"`) reads AUTO-REPAIRING / 自动修复中 instead of DEPLOYING; a
 * card click's own job keeps the usual label, and a finished auto job never re-labels an
 * active card. The panel's header is untouched (reload only).
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
const ARCH = "aws-agent-solution-architect";

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

const autoRepairing = (over: Partial<SystemPresetInfo> = {}) =>
  preset({
    status: "deploying", agent_status: "deploying", job_id: "job-9", deployment_id: "dep-9",
    deployment_status: "running",
    last_job: {
      id: "job-9", type: "deploy_agent", status: "running", error: null,
      created_at: "2026-10-22T02:00:00Z", updated_at: "2026-10-22T02:00:01Z",
      trigger: "auto", repair_reason: "auto: catalog snapshot behind v10 < v20",
    },
    ...over,
  });

const workspace = { id: "default", name: "default", bootstrap_status: "ready", is_default: true } as unknown as Workspace;

function auth(isAdmin: boolean): AuthContextValue {
  return {
    authRequired: true, username: isAdmin ? "ops" : "alice", userId: isAdmin ? null : "u-alice",
    role: isAdmin ? "admin" : "member", email: null,
    accountExpiresAt: null, isAdmin, can: () => isAdmin, logout: async () => undefined,
  };
}

const renderPanel = (isAdmin = true, onChanged?: () => void) =>
  render(
    <AuthContext.Provider value={auth(isAdmin)}>
      <WorkspaceContext.Provider
        value={{ workspaces: [workspace], current: workspace, allWorkspaces: isAdmin, select: () => undefined, refresh: async () => undefined }}
      >
        <ToastProvider>
          <MemoryRouter>
            <SystemPresetsPanel onChanged={onChanged} />
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

describe("SystemPresetsPanel — 自动修复中 (N17-obs206)", () => {
  it("keeps the header as before: reload only, no bulk action", async () => {
    renderPanel();
    await screen.findByTestId(`system-preset-${KEY}`);
    expect(screen.getByTestId("system-presets-reload")).toBeTruthy();
    expect(screen.queryByTestId("system-presets-repair-all")).toBeNull();
  });

  it("a deploying card whose running job the sweep claimed reads AUTO-REPAIRING", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({
      workspace_id: "default",
      presets: [
        autoRepairing(),
        // the architect deploys from a card click: no trigger → the usual DEPLOYING
        autoRepairing({ key: ARCH, label: "Architect", last_job: { ...autoRepairing().last_job!, trigger: null, repair_reason: null } }),
      ],
    }));
    renderPanel();
    const card = await screen.findByTestId(`system-preset-${KEY}`);
    expect(card.getAttribute("data-status")).toBe("deploying");
    expect(card.textContent).toContain(en.create.system.autoRepairing);
    expect(card.textContent).not.toContain(en.create.system.status.deploying);
    const arch = screen.getByTestId(`system-preset-${ARCH}`);
    expect(arch.textContent).toContain(en.create.system.status.deploying);
    expect(arch.textContent).not.toContain(en.create.system.autoRepairing);
    // an auto job that already finished never re-labels an active card
    cleanup();
    sys.listSystemPresets.mockImplementation(async () => ({
      workspace_id: "default",
      presets: [preset({ last_job: { ...autoRepairing().last_job!, status: "succeeded" } })],
    }));
    renderPanel();
    const active = await screen.findByTestId(`system-preset-${KEY}`);
    expect(active.textContent).toContain(en.create.system.status.active);
    expect(active.textContent).not.toContain(en.create.system.autoRepairing);
  });

  it("uses the zh-CN copy 「自动修复中」 and a member sees the same label", async () => {
    sys.listSystemPresets.mockImplementation(async () => ({ workspace_id: "default", presets: [autoRepairing()] }));
    await i18next.changeLanguage("zh-CN");
    try {
      renderPanel(false);
      const card = await screen.findByTestId(`system-preset-${KEY}`);
      expect(card.textContent).toContain("自动修复中");
      expect(card.textContent).not.toContain(zh.create.system.status.deploying);
    } finally {
      await i18next.changeLanguage("en");
    }
  });
});
