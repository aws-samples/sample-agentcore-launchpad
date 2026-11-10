// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext } from "../../../auth/auth-context";
import type { LibraryVideo, VideoCatalog } from "../../../lib/videos";
import en from "../../../locales/en/common.json";
import { V2Videos } from "../Videos";

/**
 * Document items (cn/learn-docs): a workshop manual (PDF + self-contained HTML) published
 * in the Learn library next to the recordings. The card and the detail route link the file
 * — opened in a NEW TAB, same origin (`/media/…`), because the console's CSP has
 * `frame-src 'none'` — and mount no `<video>`.
 */

const MANUAL = "/media/workshop/20261107-v2-cn-itsm";
const text = (en: string, zh: string) => ({ en, "zh-CN": zh });
const DOCUMENT: LibraryVideo = {
  id: "itsm-manual", kind: "document", consoleVersion: "v2",
  title: text("IT service desk student manual", "IT 服务台学员手册"),
  description: text("Hands-on manual for the IT service desk lab.", "IT 服务台实验的学员手册。"),
  publishedAt: "2026-11-07T09:00:00+08:00", durationSeconds: 0, language: "zh-CN", posterUrl: "",
  documentUrl: `${MANUAL}/student-manual-1.9.html`, altUrl: `${MANUAL}/student-manual-1.9.pdf`,
  sources: [], captions: [], chapters: [],
};
const RECORDING: LibraryVideo = {
  id: "agents-tour", kind: "video", consoleVersion: "v2",
  title: text("Agent management tour", "Agent 管理导览"), description: text("Create and deploy.", "创建并部署。"),
  publishedAt: "2026-10-19T09:00:00+00:00", durationSeconds: 540, language: "zh-CN", posterUrl: "",
  documentUrl: "", altUrl: null,
  sources: [{ url: "https://cdn.example.com/media/agents/20261018-v2-cn-hr/agents.zh-CN.mp4", type: "video/mp4" }],
  captions: [], chapters: [],
};
const CATALOG: VideoCatalog = {
  schemaVersion: 3,
  categories: [{ id: "build", title: text("Agent development", "Agent 开发") }, { id: "learn", title: text("Learn", "学习") }],
  collections: [
    { id: "agents", categoryId: "build", path: "/v2/agents", title: text("Agent Management", "Agent 管理"),
      description: RECORDING.description, videoIds: [RECORDING.id] },
    { id: "workshop", categoryId: "learn", path: "/v2/videos?category=learn&section=workshop",
      title: text("Workshop manuals", "实验手册"), description: DOCUMENT.description, videoIds: [DOCUMENT.id] },
  ],
  videos: [RECORDING, DOCUMENT],
};
vi.mock("../../../lib/useVideoCatalog", () => ({
  useVideoCatalog: () => ({ data: CATALOG, loading: false, error: null, reload: () => undefined }),
}));

const member = {
  authRequired: true, username: "reader", userId: "u-reader", role: "member" as const, email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined,
};

function Location() {
  const { pathname, search } = useLocation();
  return <output data-testid="location">{pathname}{search}</output>;
}

function renderLibrary(url: string) {
  // jsdom implements neither scrollIntoView nor the media methods the recording player calls
  Object.defineProperty(Element.prototype, "scrollIntoView", { configurable: true, value: () => undefined });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", { configurable: true, value: () => undefined });
  window.matchMedia = ((query: string) =>
    ({ matches: false, media: query, onchange: null, addEventListener: () => undefined,
       removeEventListener: () => undefined, addListener: () => undefined,
       removeListener: () => undefined, dispatchEvent: () => false }) as MediaQueryList) as typeof window.matchMedia;
  return render(
    <AuthContext.Provider value={member}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/v2/videos" element={<><V2Videos /><Location /></>} />
        </Routes>
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

afterEach(cleanup);

describe("a document in the library", () => {
  it("is a card with the document badge, an open link in a new tab, the other-format link and no <video>", async () => {
    renderLibrary("/v2/videos");
    await screen.findByTestId("video-library");
    const card = screen.getByTestId("video-card-workshop");
    // a card is its section, like a recording's card is its module
    expect(card.textContent).toContain("Workshop manuals");
    expect(card.textContent).toContain(DOCUMENT.description.en);
    expect(card.textContent).toContain(en.videos.kind.document);
    const open = screen.getByTestId(`document-open-${DOCUMENT.id}`) as HTMLAnchorElement;
    expect(open.tagName).toBe("A");
    expect(open.getAttribute("href")).toBe(DOCUMENT.documentUrl);
    expect(open.getAttribute("target")).toBe("_blank");
    expect(open.getAttribute("rel")).toContain("noopener");
    expect(open.textContent).toContain(en.videos.openDocument);
    expect(open.textContent).toContain(en.videos.format.html);
    const alt = screen.getByTestId(`document-alt-${DOCUMENT.id}`) as HTMLAnchorElement;
    expect(alt.getAttribute("href")).toBe(DOCUMENT.altUrl);
    expect(alt.getAttribute("target")).toBe("_blank");
    expect(alt.textContent).toBe(en.videos.format.pdf);
    // the links are not nested inside the card's button (valid, keyboard-reachable markup)
    expect(open.closest("button")).toBeNull();
    expect(document.querySelector("video")).toBeNull();
    // the recording's card has no open link
    expect(screen.getByTestId("video-card-agents").tagName).toBe("BUTTON");
    expect(screen.queryByTestId(`document-open-${RECORDING.id}`)).toBeNull();
  });

  it("the card body routes to the detail view, which shows the links and mounts no player", async () => {
    renderLibrary("/v2/videos");
    await screen.findByTestId("video-library");
    fireEvent.click(screen.getByTestId("video-card-workshop-detail"));
    await screen.findByTestId("document-detail");
    expect(screen.getByTestId("location").textContent).toBe(`/v2/videos?view=watch&video=${DOCUMENT.id}&version=v2`);
    expect(screen.queryByTestId("video-watch")).toBeNull();
    expect(screen.queryByTestId("video-player")).toBeNull();
    expect(document.querySelector("video")).toBeNull();
    expect(screen.getByTestId("document-panel").textContent).toContain(DOCUMENT.description.en);
    expect((screen.getByTestId(`document-open-${DOCUMENT.id}`) as HTMLAnchorElement).getAttribute("href")).toBe(DOCUMENT.documentUrl);
    expect((screen.getByTestId(`document-alt-${DOCUMENT.id}`) as HTMLAnchorElement).getAttribute("href")).toBe(DOCUMENT.altUrl);
    expect(screen.getByTestId("document-detail").textContent).toContain(en.videos.documentOpensNewTab);
    // a single manual needs no series list
    expect(screen.queryByTestId("video-directory")).toBeNull();
  });

  it("a deep link to the document id lands on the detail view; the recording still gets the player", async () => {
    renderLibrary(`/v2/videos?view=watch&video=${DOCUMENT.id}`);
    await screen.findByTestId("document-detail");
    expect(document.querySelector("video")).toBeNull();
    cleanup();
    renderLibrary(`/v2/videos?view=watch&video=${RECORDING.id}`);
    await screen.findByTestId("video-watch");
    expect(screen.queryByTestId("document-detail")).toBeNull();
    expect(document.querySelector("video")).not.toBeNull();
  });
});
