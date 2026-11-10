// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../../auth/auth-context";
import type { RegistryRecordOut } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { REGISTRY_CHANGED_EVENT } from "../../registryPending";
import { RecordDetail } from "./RecordDetail";

/**
 * N17-obs248 (user ruling 2026-10-22): the registry review decision is an administrator's.
 * A member sees only 提交审批 / "Submit for approval" (DRAFT, REJECTED) and, while the record
 * is PENDING_APPROVAL, a muted 等待管理员审批 / "Waiting for administrator approval" — never
 * 批准并发布 / 驳回 / 停用. Administrators keep every lifecycle button.
 */

let current: RegistryRecordOut;
const record = (status: string): RegistryRecordOut => ({
  system: null, record_id: "rec-1", name: "srv-one", description: "", type: "MCP", status,
  status_reason: null, version: "1.0.0-mcp", created_at: "2026-10-22T00:00:00Z",
  updated_at: "2026-10-22T00:00:00Z", created_by: "u-bob", created_by_name: "bob",
});

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      registryRecord: () => Promise.resolve(current),
      registryAction: (_id: string, action: string) => {
        current = { ...current, status: action === "approve" ? "APPROVED" : "REJECTED" };
        return Promise.resolve(current);
      },
      listAgents: () => Promise.resolve({ agents: [] }),
      registryDiscoverable: () => Promise.resolve({ records: [], count: 0 }),
    },
  };
});

const auth = (over: Partial<AuthContextValue>): AuthContextValue => ({
  authRequired: true, username: "bob", userId: "u-bob", role: "member", email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined, ...over,
});
const MEMBER = auth({});
const ADMIN = auth({ username: "root", userId: null, role: "admin", isAdmin: true });

function mount(value: AuthContextValue, status: string) {
  current = record(status);
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={["/v2/registry?view=detail&id=rec-1"]}>
        <RecordDetail id="rec-1" />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const ready = () => waitFor(() => expect(screen.getByTestId("v2-registry-flow")).toBeTruthy());
const has = (id: string) => screen.queryByTestId(id) !== null;

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

describe("RecordDetail lifecycle by role (N17-obs248)", () => {
  it.each(["DRAFT", "REJECTED"])("a member may only submit a %s record", async (status) => {
    mount(MEMBER, status);
    await ready();
    expect(screen.getByTestId("v2-registry-submit").textContent).toBe("Submit for approval");
    for (const id of ["v2-registry-approve", "v2-registry-reject", "v2-registry-disable"]) expect(has(id)).toBe(false);
  });

  it("a member waits on a pending record instead of approving it", async () => {
    mount(MEMBER, "PENDING_APPROVAL");
    await ready();
    expect(screen.getByTestId("v2-registry-awaiting-admin").textContent).toBe("Waiting for administrator approval");
    for (const id of ["v2-registry-submit", "v2-registry-approve", "v2-registry-reject"]) expect(has(id)).toBe(false);
  });

  it("a member cannot disable an approved record", async () => {
    mount(MEMBER, "APPROVED");
    await ready();
    expect(has("v2-registry-disable")).toBe(false);
    expect(has("v2-registry-awaiting-admin")).toBe(false);
  });

  it("an administrator keeps approve / reject / disable", async () => {
    mount(ADMIN, "PENDING_APPROVAL");
    await ready();
    expect(screen.getByTestId("v2-registry-approve").textContent).toBe("Approve & publish");
    expect(has("v2-registry-reject")).toBe(true);
    expect(has("v2-registry-awaiting-admin")).toBe(false);
    cleanup();
    mount(ADMIN, "REJECTED");
    await ready();
    expect(has("v2-registry-approve")).toBe(true);
    expect(has("v2-registry-submit")).toBe(false); // admins unchanged: REJECTED → approve
    cleanup();
    mount(ADMIN, "APPROVED");
    await ready();
    expect(has("v2-registry-disable")).toBe(true);
  });

  it("names who submits and who approves in the flow card, in both languages", async () => {
    mount(MEMBER, "DRAFT");
    await ready();
    expect(screen.getByText("Draft → Submit (member) → Approve (administrator)")).toBeTruthy();
    cleanup();
    await i18next.changeLanguage("zh-CN");
    mount(MEMBER, "PENDING_APPROVAL");
    await ready();
    expect(screen.getByText("草稿 → 提交（成员）→ 批准（管理员）")).toBeTruthy();
    expect(screen.getByTestId("v2-registry-awaiting-admin").textContent).toBe("等待管理员审批");
  });
});

describe("RecordDetail decisions refresh the pending badge (N17-obs267)", () => {
  it.each([["approve", "v2-registry-approve"], ["reject", "v2-registry-reject"]])(
    "an administrator's %s tells the shell to re-read the pending count",
    async (_action, testId) => {
      const seen = vi.fn();
      window.addEventListener(REGISTRY_CHANGED_EVENT, seen);
      try {
        mount(ADMIN, "PENDING_APPROVAL");
        await ready();
        expect(seen).not.toHaveBeenCalled();
        fireEvent.click(screen.getByTestId(testId));
        await waitFor(() => expect(seen).toHaveBeenCalledTimes(1));
      } finally {
        window.removeEventListener(REGISTRY_CHANGED_EVENT, seen);
      }
    },
  );
});
