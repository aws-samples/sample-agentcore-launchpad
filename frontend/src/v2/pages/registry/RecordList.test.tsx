// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../../auth/auth-context";
import type { RegistryRecordOut } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { RecordList } from "./RecordList";

/**
 * N15-obs7: a registry record's 编辑 / 删除 follow the server's creator-or-admin rule
 * (`registry.forbidden`, users-and-ownership.md §8) — the list hides them for rows the
 * signed-in member may not change (another member's, a NULL-creator legacy row) and keeps
 * them on the member's own rows and for administrators. Reads stay shared: every row is listed.
 */

const record = (id: string, created_by: string | null): RegistryRecordOut => ({
  system: null, record_id: id, name: `srv-${id}`, description: "", type: "MCP", status: "DRAFT",
  status_reason: null, version: "1.0.0-mcp", created_at: "2026-10-19T00:00:00Z",
  updated_at: "2026-10-19T00:00:00Z", created_by, created_by_name: created_by ? "someone" : null,
});
const WITH_PENDING: RegistryRecordOut[] = [
  record("mine", "u-bob"), record("theirs", "u-ada"), record("legacy", null),
  { ...record("waiting", "u-bob"), status: "PENDING_APPROVAL" },
  { ...record("queued", "u-ada"), status: "PENDING_APPROVAL" },
  { ...record("live", "u-ada"), status: "APPROVED" },
  { ...record("refused", "u-ada"), status: "REJECTED" },
  { ...record("retired", null), status: "DEPRECATED" },
];
let RECORDS: RegistryRecordOut[] = WITH_PENDING;

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      registryRecords: () => Promise.resolve({ records: RECORDS }),
      registryDiscoverable: () => Promise.resolve({ records: [], count: 0 }),
    },
  };
});

const auth = (over: Partial<AuthContextValue>): AuthContextValue => ({
  authRequired: true, username: "bob", userId: "u-bob", role: "member", email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined, ...over,
});

let search = "";
function LocationProbe() {
  search = useLocation().search;
  return null;
}

function mount(value: AuthContextValue, url = "/v2/registry?status=all") {
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={[url]}>
        <RecordList />
        <LocationProbe />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  RECORDS = WITH_PENDING;
});

describe("RecordList creator-or-admin actions", () => {
  it("hides edit / delete on rows a member may not change and keeps every row listed", async () => {
    mount(auth({}));
    await waitFor(() => expect(screen.getByTestId("v2-registry-open-mine")).toBeTruthy());
    for (const id of ["mine", "theirs", "legacy"]) expect(screen.getByTestId(`v2-registry-open-${id}`)).toBeTruthy();
    expect(screen.getByTestId("v2-registry-edit-mine")).toBeTruthy();
    expect(screen.getByTestId("v2-registry-delete-mine")).toBeTruthy();
    expect(screen.queryByTestId("v2-registry-edit-theirs")).toBeNull();
    expect(screen.queryByTestId("v2-registry-delete-theirs")).toBeNull();
    // a record nobody is recorded to have registered is an administrator's
    expect(screen.queryByTestId("v2-registry-edit-legacy")).toBeNull();
    expect(screen.queryByTestId("v2-registry-delete-legacy")).toBeNull();
  });

  it("shows edit / delete on every row for an administrator", async () => {
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-registry-open-legacy")).toBeTruthy());
    for (const id of ["mine", "theirs", "legacy"]) {
      expect(screen.getByTestId(`v2-registry-edit-${id}`)).toBeTruthy();
      expect(screen.getByTestId(`v2-registry-delete-${id}`)).toBeTruthy();
    }
  });
});

/**
 * N17-obs267 (user request 2026-10-22 "可不可以把待审批和已经审批的分一下类，如果多了的话，可能遗漏
 * 待审批部分"): the publisher list is split into 待审批 / 已发布 / 草稿 / 已驳回 / 全部 status tabs
 * with counts, deep-linked by `?status=`. Without the param an administrator lands on 待审批
 * while anything is pending, everyone else (and an administrator with nothing pending) on 全部.
 */
const ADMIN = auth({ username: "root", userId: null, role: "admin", isAdmin: true });
const shown = (id: string) => screen.queryByTestId(`v2-registry-open-${id}`) !== null;
const selected = (tab: string) => screen.getByTestId(`v2-registry-tab-${tab}`).getAttribute("aria-selected");
const countOf = (tab: string) => screen.getByTestId(`v2-registry-tab-${tab}-count`).textContent;

describe("RecordList status tabs (N17-obs267)", () => {
  it("counts every tab from the loaded records", async () => {
    mount(ADMIN);
    await waitFor(() => expect(countOf("all")).toBe("8"));
    expect(countOf("pending")).toBe("2");
    expect(countOf("published")).toBe("1");
    expect(countOf("draft")).toBe("4"); // 3 drafts + 1 rejected
    expect(screen.getByTestId("v2-registry-tab-pending").textContent).toBe("Pending approval2");
    expect(screen.getByTestId("v2-registry-tab-draft").textContent).toBe("Draft / rejected4");
  });

  it("lands an administrator on the pending tab while records wait for approval", async () => {
    mount(ADMIN, "/v2/registry");
    await waitFor(() => expect(shown("waiting")).toBe(true));
    expect(selected("pending")).toBe("true");
    expect(shown("queued")).toBe(true);
    for (const id of ["mine", "live", "refused", "retired"]) expect(shown(id)).toBe(false);
    // a single-status tab needs no status select
    expect(screen.queryByTestId("v2-registry-status")).toBeNull();
  });

  it("lands an administrator on all when nothing is pending", async () => {
    RECORDS = WITH_PENDING.filter((r) => r.status !== "PENDING_APPROVAL");
    mount(ADMIN, "/v2/registry");
    await waitFor(() => expect(shown("mine")).toBe(true));
    expect(selected("all")).toBe("true");
    expect(countOf("pending")).toBe("0");
    expect(shown("retired")).toBe(true);
  });

  it("lands a member on all; the pending tab shows the pending records the member can see", async () => {
    mount(auth({}), "/v2/registry");
    await waitFor(() => expect(shown("mine")).toBe(true));
    expect(selected("all")).toBe("true");
    expect(countOf("pending")).toBe("2");
    fireEvent.click(screen.getByTestId("v2-registry-tab-pending"));
    await waitFor(() => expect(shown("mine")).toBe(false));
    expect(shown("waiting") && shown("queued")).toBe(true);
    expect(new URLSearchParams(search).get("status")).toBe("pending");
  });

  it("splits published from draft / rejected and keeps a deep link", async () => {
    mount(auth({}), "/v2/registry?status=published");
    await waitFor(() => expect(shown("live")).toBe(true));
    expect(selected("published")).toBe("true");
    for (const id of ["mine", "waiting", "refused", "retired"]) expect(shown(id)).toBe(false);
    fireEvent.click(screen.getByTestId("v2-registry-tab-draft"));
    await waitFor(() => expect(shown("refused")).toBe(true));
    for (const id of ["mine", "theirs", "legacy"]) expect(shown(id)).toBe(true);
    expect(shown("live") || shown("waiting") || shown("retired")).toBe(false);
    expect(new URLSearchParams(search).get("status")).toBe("draft");
  });

  it("an explicit tab wins over the administrator's pending landing", async () => {
    mount(ADMIN, "/v2/registry?status=all");
    await waitFor(() => expect(shown("retired")).toBe(true));
    expect(selected("all")).toBe("true");
  });
});
