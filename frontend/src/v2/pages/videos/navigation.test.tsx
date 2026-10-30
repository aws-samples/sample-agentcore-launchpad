// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext } from "../../../auth/auth-context";
import type { Feature } from "../../../lib/capabilities";
import type { ManagedVideoCatalog, VideoCatalog } from "../../../lib/videos";
import en from "../../../locales/en/common.json";
import { V2_NAV } from "../../nav";
import { V2Shell } from "../../V2Shell";
import { V2Videos } from "../Videos";
import { legacyVideoManagementToV2 } from "./classicUrl";

/**
 * One video entry (N15-obs14, user decision 2026-10-19): Learn → Videos is the LAST
 * sidebar group, for administrators and members alike, toggled by the `videos`
 * capability. Administrators manage the directory on the page's 管理 tab
 * (`?view=manage`); there is no second entry and no `/v2/video-management` page any
 * more (the URL redirects onto the tab).
 */

let features: Set<Feature> | null = null;
vi.mock("../../../lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/capabilities")>()),
  useFeatures: () => ({ features, loading: false }),
}));
const EMPTY_CATALOG: VideoCatalog = { schemaVersion: 3, categories: [], collections: [], videos: [] };
// no network: the shell's demo-video catalog and the library share this empty directory
vi.mock("../../../lib/useVideoCatalog", () => ({
  useVideoCatalog: () => ({ data: EMPTY_CATALOG, loading: false, error: null, reload: () => undefined }),
}));
const EMPTY_MANAGED: ManagedVideoCatalog = {
  taxonomy: { schemaVersion: 1, categories: [], sections: [] },
  videos: [],
};
const manageVideos = vi.fn(async () => EMPTY_MANAGED);
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return { ...actual, api: { ...actual.api, manageVideos: () => manageVideos() } };
});

const ALL_BUT_VIDEOS = new Set<Feature>([
  "agents", "chat", "registry", "memory", "knowledge_bases", "identity", "gateway",
  "observability", "workspaces", "users", "evaluation", "optimization", "skill_lab",
]);
const WITH_VIDEOS = new Set<Feature>([...ALL_BUT_VIDEOS, "videos"]);

function auth(isAdmin: boolean) {
  return {
    authRequired: true,
    username: isAdmin ? "ops" : "reader",
    userId: isAdmin ? null : "u-reader",
    role: isAdmin ? ("admin" as const) : ("member" as const),
    email: null,
    accountExpiresAt: null,
    isAdmin,
    can: () => true,
    logout: async () => undefined,
  };
}

function stubMatchMedia() {
  window.matchMedia = ((query: string) =>
    ({ matches: false, media: query, onchange: null, addEventListener: () => undefined,
       removeEventListener: () => undefined, addListener: () => undefined,
       removeListener: () => undefined, dispatchEvent: () => false }) as MediaQueryList) as typeof window.matchMedia;
}

function renderShell(isAdmin: boolean) {
  stubMatchMedia();
  return render(
    <AuthContext.Provider value={auth(isAdmin)}>
      <MemoryRouter initialEntries={["/v2"]}>
        <Routes>
          <Route element={<V2Shell />}>
            <Route path="/v2" element={<div data-testid="page-home">home</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

function Location() {
  const { pathname, search } = useLocation();
  return <output data-testid="location">{pathname}{search}</output>;
}

function renderVideos(isAdmin: boolean, url: string) {
  stubMatchMedia();
  return render(
    <AuthContext.Provider value={auth(isAdmin)}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/v2/videos" element={<><V2Videos /><Location /></>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const sideGroups = () =>
  [...document.querySelectorAll("aside.v2-side .v2-side-group")].map(
    (group) => group.querySelector(".v2-side-head")?.textContent ?? "",
  );

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(() => {
  cleanup();
  features = null;
  manageVideos.mockClear();
});

describe("the sidebar has one video entry, in the last group", () => {
  it("nav.ts: exactly one video item, in the Learn group, which comes after Configuration", () => {
    const videoItems = V2_NAV.flatMap((group) => group.items).filter((item) => item.to.includes("video"));
    expect(videoItems.map((item) => item.to)).toEqual(["/v2/videos"]);
    expect(videoItems[0].admin).toBeUndefined();
    expect(videoItems[0].feature).toBe("videos");
    const keys = V2_NAV.map((group) => group.key);
    expect(keys[keys.length - 1]).toBe("learn");
    expect(keys.indexOf("admin")).toBe(keys.length - 2);
  });

  it("V2 shell: Learn → Videos is the last group for an administrator; no management entry", async () => {
    features = WITH_VIDEOS;
    renderShell(true);
    await screen.findByTestId("page-home");
    const library = screen.getByTestId("v2-nav-/v2/videos");
    expect(library.textContent).toContain(en.nav.videos);
    expect(library.closest(".v2-side-group")?.textContent).toContain(en.v2.nav.groupLearn);
    expect(screen.queryByTestId("v2-nav-/v2/video-management")).toBeNull();
    const groups = sideGroups();
    expect(groups[groups.length - 1]).toBe(en.v2.nav.groupLearn);
    expect(groups[groups.length - 2]).toBe(en.v2.nav.groupAdmin);
    // an administrator's Configuration group: workspaces, users, then Model Providers last
    const admin = screen.getByTestId("v2-nav-/v2/users").closest(".v2-side-group");
    expect([...(admin?.querySelectorAll(".v2-side-item") ?? [])].map((a) => a.getAttribute("href"))).toEqual([
      "/v2/workspaces",
      "/v2/users",
      "/v2/settings/model-providers",
    ]);
  });

  it("V2 shell: a member sees the same single entry, also last; Configuration holds only Model Providers", async () => {
    features = WITH_VIDEOS;
    renderShell(false);
    await screen.findByTestId("page-home");
    expect(screen.queryByTestId("v2-nav-/v2/videos")).not.toBeNull();
    expect(screen.queryByTestId("v2-nav-/v2/video-management")).toBeNull();
    const groups = sideGroups();
    expect(groups[groups.length - 1]).toBe(en.v2.nav.groupLearn);
    // the Configuration group shows for a member too (user decision 2026-10-21): providers are
    // owner-bound (N17-obs203), so Model Providers is its one member-visible entry, right
    // before Learn; workspaces / users stay administrators-only
    expect(groups[groups.length - 2]).toBe(en.v2.nav.groupAdmin);
    const admin = screen.getByTestId("v2-nav-/v2/settings/model-providers").closest(".v2-side-group");
    expect(admin?.querySelector(".v2-side-head")?.textContent).toBe(en.v2.nav.groupAdmin);
    expect([...(admin?.querySelectorAll(".v2-side-item") ?? [])].map((a) => a.getAttribute("href"))).toEqual([
      "/v2/settings/model-providers",
    ]);
    expect(screen.queryByTestId("v2-nav-/v2/workspaces")).toBeNull();
    expect(screen.queryByTestId("v2-nav-/v2/users")).toBeNull();
  });

  it("V2 shell: without the flag neither the entry nor the Learn group exists", async () => {
    features = ALL_BUT_VIDEOS;
    renderShell(true);
    await screen.findByTestId("page-home");
    expect(screen.queryByTestId("v2-nav-/v2/videos")).toBeNull();
    expect(screen.queryByTestId("v2-nav-/v2/video-management")).toBeNull();
    expect(document.querySelector("aside.v2-side")?.textContent).not.toContain(en.v2.nav.groupLearn);
  });
});

describe("the Videos page: 观看 / 管理 tabs for administrators only", () => {
  it("an administrator lands on the library tab and switches to the management list", async () => {
    renderVideos(true, "/v2/videos");
    await screen.findByTestId("videos-empty");
    const library = screen.getByTestId("v2-tab-library");
    const manage = screen.getByTestId("v2-tab-manage");
    expect(library.getAttribute("aria-selected")).toBe("true");
    expect(manage.textContent).toBe(en.videos.tab.manage);
    expect(manageVideos).not.toHaveBeenCalled();
    fireEvent.click(manage);
    await screen.findByTestId("video-manage-table");
    expect(screen.getByTestId("location").textContent).toBe("/v2/videos?view=manage");
    expect(screen.getByTestId("v2-tab-manage").getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("video-manage-new")).toBeTruthy();
    expect(screen.queryByTestId("video-manage-forbidden")).toBeNull();
    expect(manageVideos).toHaveBeenCalledTimes(1);
    // back to 观看: the library again, the query string cleared
    fireEvent.click(screen.getByTestId("v2-tab-library"));
    await screen.findByTestId("videos-empty");
    expect(screen.getByTestId("location").textContent).toBe("/v2/videos");
  });

  it("`?view=manage&edit=new` opens the editor; its back arrow returns to the management list", async () => {
    renderVideos(true, "/v2/videos?view=manage&edit=new");
    await screen.findByTestId("video-manage-editor");
    expect(screen.queryByTestId("v2-tab-manage")).toBeNull();
    fireEvent.click(screen.getByText(en.v2.common.back));
    await screen.findByTestId("video-manage-table");
    expect(screen.getByTestId("location").textContent).toBe("/v2/videos?view=manage");
  });

  it("a member gets no tabs, and a deep link to the tab shows the admin-required card", async () => {
    renderVideos(false, "/v2/videos");
    await screen.findByTestId("videos-empty");
    expect(screen.queryByTestId("v2-tab-library")).toBeNull();
    expect(screen.queryByTestId("v2-tab-manage")).toBeNull();
    cleanup();
    renderVideos(false, "/v2/videos?view=manage");
    await screen.findByTestId("video-manage-forbidden");
    expect(screen.queryByTestId("v2-tab-manage")).toBeNull();
    expect(screen.queryByTestId("video-manage-table")).toBeNull();
    expect(manageVideos).not.toHaveBeenCalled();
  });
});

describe("the retired /v2/video-management URL lands on the 管理 tab", () => {
  it("maps the list, the blank draft and the editor deep links", () => {
    expect(legacyVideoManagementToV2("")).toBe("/v2/videos?view=manage");
    expect(legacyVideoManagementToV2("?view=new")).toBe("/v2/videos?view=manage&edit=new");
    expect(legacyVideoManagementToV2("?view=edit&id=vid-7")).toBe("/v2/videos?view=manage&edit=vid-7");
    // an edit link without its id, or an unknown view, falls back to the list
    expect(legacyVideoManagementToV2("?view=edit")).toBe("/v2/videos?view=manage");
    expect(legacyVideoManagementToV2("?view=old&x=1")).toBe("/v2/videos?view=manage");
  });
});
