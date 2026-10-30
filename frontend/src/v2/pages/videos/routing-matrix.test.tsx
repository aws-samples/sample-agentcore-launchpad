// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext } from "../../../auth/auth-context";
import { ApiError } from "../../../lib/api";
import type { Feature } from "../../../lib/capabilities";
import type { LibraryVideo, ManagedVideo, ManagedVideoCatalog, VideoCatalog } from "../../../lib/videos";
import en from "../../../locales/en/common.json";
import { V2Shell } from "../../V2Shell";
import { V2Videos } from "../Videos";
import { legacyVideoManagementToV2 } from "./classicUrl";
import { useVideoPlayback } from "./useVideoPlayback";

/**
 * E22 — the page half of the round-16 routing matrix (r16-g). Contract: N15-obs14
 * (`docs/cn/reports/bugs-round-15.md`) and `docs/cn/acceptance/E22-video-library.md` §1
 * "Navigation": ONE sidebar entry; the manager is the library's admin-only `?view=manage`
 * tab; `/v2/video-management` redirects onto it (`?view=new` → `&edit=new`,
 * `?view=edit&id=` → `&edit=<id>`); a member deep-linking the tab sees
 * `video-manage-forbidden` and no loader is mounted. Roles × every entry URL is walked
 * below; the expected landing is derived from `legacyVideoManagementToV2` (the redirect
 * mapping under test) rather than typed twice.
 *
 * The chapter seek half pins the player helper's boundary behaviour (AC-E22-05 chapter
 * list; B charter "chapter jump to the last second"): a seek at 0 lands at 0, a seek at
 * or past the duration lands just inside the end, and an unknown duration passes the
 * requested offset through.
 */

let features: Set<Feature> | null = null;
vi.mock("../../../lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/capabilities")>()),
  useFeatures: () => ({ features, loading: false }),
}));
const EMPTY_CATALOG: VideoCatalog = { schemaVersion: 3, categories: [], collections: [], videos: [] };
vi.mock("../../../lib/useVideoCatalog", () => ({
  useVideoCatalog: () => ({ data: EMPTY_CATALOG, loading: false, error: null, reload: () => undefined }),
}));
const ROW: ManagedVideo = {
  id: "vid-7",
  content: {
    category_id: "build", section_id: "agents", console_version: "v2",
    title: { en: "Agents", "zh-CN": "Agent 管理" }, description: { en: "d", "zh-CN": "描述" },
    cdn_url: "https://cdn.example.com/media/agents/20261018-v2-cn-hr/agents.zh-CN.mp4",
    webm_url: null, poster_url: null, caption_url: null, duration_seconds: 540, chapters: [], sort_order: 10,
  },
  published_content: null, status: "draft", has_unpublished_changes: true, revision: 1,
  created_by: "ops", updated_by: "ops", created_at: "2026-10-19T09:00:00+00:00",
  updated_at: "2026-10-19T09:00:00+00:00", published_at: null,
};
const MANAGED: ManagedVideoCatalog = {
  taxonomy: {
    schemaVersion: 1,
    categories: [{ id: "build", title: { en: "Build", "zh-CN": "构建" } }],
    sections: [{ id: "agents", categoryId: "build", title: { en: "Agents", "zh-CN": "Agent 管理" }, path: "/v2/agents" }],
  },
  videos: [ROW],
};
const manageVideos = vi.fn(async () => MANAGED);
const getManagedVideo = vi.fn(async (id: string) => {
  if (id === ROW.id) return ROW;
  throw new ApiError("videos.not_found", "Video not found", null);
});
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      manageVideos: () => manageVideos(),
      getManagedVideo: (id: string) => getManagedVideo(id),
    },
  };
});

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

function Location() {
  const { pathname, search } = useLocation();
  return <output data-testid="location">{pathname}{search}</output>;
}

/** App.tsx's `ClassicRedirect` for the retired manager URL (the mapping under test). */
function LegacyRedirect() {
  const { search } = useLocation();
  return <Navigate to={legacyVideoManagementToV2(search)} replace />;
}

function renderAt(isAdmin: boolean, url: string) {
  stubMatchMedia();
  return render(
    <AuthContext.Provider value={auth(isAdmin)}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/v2/videos" element={<><V2Videos /><Location /></>} />
          <Route path="/v2/video-management" element={<LegacyRedirect />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

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
  getManagedVideo.mockClear();
});

// ─── roles × entry URLs ─────────────────────────────────────────────────────────

type Landing = "library" | "manage-list" | "editor-new" | "editor-vid-7" | "forbidden";

/** Where each entry URL must land, per role (N15-obs14). The legacy URLs' expected
 *  location is computed through the redirect helper so the matrix tests the helper's
 *  wiring into the page rather than restating its table. */
const ENTRIES: { url: string; admin: Landing; member: Landing; location: string }[] = [
  { url: "/v2/videos", admin: "library", member: "library", location: "/v2/videos" },
  { url: "/v2/videos?view=manage", admin: "manage-list", member: "forbidden", location: "/v2/videos?view=manage" },
  { url: "/v2/videos?view=manage&edit=new", admin: "editor-new", member: "forbidden", location: "/v2/videos?view=manage&edit=new" },
  { url: "/v2/videos?view=manage&edit=vid-7", admin: "editor-vid-7", member: "forbidden", location: "/v2/videos?view=manage&edit=vid-7" },
  { url: "/v2/video-management", admin: "manage-list", member: "forbidden", location: legacyVideoManagementToV2("") },
  { url: "/v2/video-management?view=new", admin: "editor-new", member: "forbidden", location: legacyVideoManagementToV2("?view=new") },
  { url: "/v2/video-management?view=edit&id=vid-7", admin: "editor-vid-7", member: "forbidden", location: legacyVideoManagementToV2("?view=edit&id=vid-7") },
  // an edit link without its id falls back to the list (classicUrl.ts)
  { url: "/v2/video-management?view=edit", admin: "manage-list", member: "forbidden", location: legacyVideoManagementToV2("?view=edit") },
];

async function expectLanding(landing: Landing, isAdmin: boolean) {
  switch (landing) {
    case "library":
      await screen.findByTestId("videos-empty");
      expect(screen.queryByTestId("video-manage-forbidden")).toBeNull();
      // the tabs are an administrator affordance
      expect(screen.queryByTestId("v2-tab-manage") !== null).toBe(isAdmin);
      return;
    case "manage-list":
      await screen.findByTestId("video-manage-table");
      expect(screen.getByTestId("v2-tab-manage").getAttribute("aria-selected")).toBe("true");
      expect(manageVideos).toHaveBeenCalled();
      return;
    case "editor-new":
      await screen.findByTestId("video-manage-editor");
      expect(screen.getByTestId("video-manage-save")).toBeTruthy();
      expect(getManagedVideo).not.toHaveBeenCalled();
      return;
    case "editor-vid-7":
      await screen.findByTestId("video-manage-editor");
      expect(getManagedVideo).toHaveBeenCalledWith("vid-7");
      expect((screen.getByTestId("video-manage-title") as HTMLInputElement).value).toBe(ROW.content.title["zh-CN"]);
      return;
    case "forbidden":
      await screen.findByTestId("video-manage-forbidden");
      expect(screen.queryByTestId("video-manage-table")).toBeNull();
      expect(screen.queryByTestId("video-manage-editor")).toBeNull();
      expect(screen.queryByTestId("v2-tab-manage")).toBeNull();
      // no loader is mounted for a member (N15-obs14)
      expect(manageVideos).not.toHaveBeenCalled();
      expect(getManagedVideo).not.toHaveBeenCalled();
      return;
  }
}

describe("roles × entry URLs land where N15-obs14 says", () => {
  for (const entry of ENTRIES) {
    for (const role of ["admin", "member"] as const) {
      it(`${role} → ${entry.url}`, async () => {
        renderAt(role === "admin", entry.url);
        await expectLanding(entry[role], role === "admin");
        expect(screen.getByTestId("location").textContent).toBe(entry.location);
      });
    }
  }

  it("a member on the library sees no manage tab and no manage loader", async () => {
    renderAt(false, "/v2/videos");
    await screen.findByTestId("videos-empty");
    expect(screen.queryByTestId("v2-tab-library")).toBeNull();
    expect(manageVideos).not.toHaveBeenCalled();
  });
});

describe("the videos feature flag gates the page, not only the sidebar", () => {
  it("without `videos` the shell renders neither the entry nor the library", async () => {
    stubMatchMedia();
    features = new Set<Feature>(["agents", "chat"]);
    render(
      <AuthContext.Provider value={auth(true)}>
        <MemoryRouter initialEntries={["/v2/videos"]}>
          <Routes>
            <Route element={<V2Shell />}>
              <Route path="/v2/videos" element={<V2Videos />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>,
    );
    await act(async () => undefined);
    expect(screen.queryByTestId("v2-nav-/v2/videos")).toBeNull();
    expect(screen.queryByTestId("videos-empty")).toBeNull();
    expect(screen.queryByTestId("v2-tab-manage")).toBeNull();
  });
});

// ─── chapter seek boundaries in the player helper ─────────────────────────────

const VIDEO: LibraryVideo = {
  id: "vid-7", consoleVersion: "v2", title: { en: "Agents", "zh-CN": "Agent 管理" },
  description: { en: "d", "zh-CN": "描述" }, publishedAt: "2026-10-19T09:00:00+00:00",
  durationSeconds: 540, language: "zh-CN", posterUrl: "",
  sources: [{ url: "https://cdn.example.com/media/a.mp4", type: "video/mp4" }],
  captions: [],
  chapters: [
    { startSeconds: 0, title: { en: "Start", "zh-CN": "开场" } },
    { startSeconds: 539, title: { en: "Last second", "zh-CN": "最后一秒" } },
  ],
};

let seek: (startSeconds: number) => void = () => undefined;
function Harness() {
  const { media, playChapter } = useVideoPlayback(VIDEO, "en");
  seek = playChapter;
  return <div>{media}</div>;
}

function mountPlayer(duration: number) {
  stubMatchMedia();
  // jsdom implements neither scrollIntoView nor the media methods; the helper only needs
  // them to exist
  Object.defineProperty(Element.prototype, "scrollIntoView", { configurable: true, value: () => undefined });
  Object.defineProperty(HTMLMediaElement.prototype, "play", { configurable: true, value: () => Promise.resolve() });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", { configurable: true, value: () => undefined });
  Object.defineProperty(HTMLMediaElement.prototype, "load", { configurable: true, value: () => undefined });
  render(<Harness />);
  const player = screen.getByTestId("video-player") as HTMLVideoElement;
  Object.defineProperty(player, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_METADATA });
  Object.defineProperty(player, "duration", { configurable: true, value: duration });
  let time = 0;
  Object.defineProperty(player, "currentTime", {
    configurable: true,
    get: () => time,
    set: (v: number) => { time = v; },
  });
  return player;
}

describe("chapter seek boundaries (useVideoPlayback.applyPendingSeek)", () => {
  it.each([
    ["0 → 0", 0, 0],
    ["inside → itself", 123.4, 123.4],
    ["last second → itself", 539, 539],
    ["== duration → just inside the end", 540, 540 - 0.01],
    ["duration + 1 → just inside the end", 541, 540 - 0.01],
  ])("%s", (_label, target, expected) => {
    const player = mountPlayer(540);
    act(() => seek(target));
    expect(player.currentTime).toBeCloseTo(expected, 6);
  });

  it("an unknown duration (metadata without a length) passes the offset through", () => {
    const player = mountPlayer(Number.NaN);
    act(() => seek(541));
    expect(player.currentTime).toBe(541);
  });

  it("a zero-length media never seeks below 0", () => {
    const player = mountPlayer(0);
    act(() => seek(5));
    expect(player.currentTime).toBe(0);
  });
});
