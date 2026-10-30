// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext } from "../auth/auth-context";
import en from "../locales/en/common.json";
import { notifyRegistryChanged } from "./registryPending";
import { V2Shell } from "./V2Shell";

/**
 * WB-004: at ≤960 px the sidebar is an overlay drawer behind `v2-nav-toggle` (nav,
 * workspace switcher, language toggle, sign out); Escape, the backdrop
 * and a route change close it and focus returns to the button. Above 960 px the shell
 * keeps its desktop markup, so the existing e2e test ids still resolve.
 */

// no network in the shell test: every module is on, nothing is loading
vi.mock("../lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/capabilities")>()),
  useFeatures: () => ({ features: null, loading: false }),
}));

// N17-obs466: this test bundle "was built from" BUNDLE (vitest compiles no sha in); a test
// may set `bundle.sha = null` to stand for a dev-server / unstamped bundle
const BUNDLE = "a096762d7f3e4c21b9d0e5f6a7b8c9d0e1f2a3b4";
const bundle = vi.hoisted(() => ({ sha: "a096762d7f3e4c21b9d0e5f6a7b8c9d0e1f2a3b4" as string | null }));
vi.mock("./buildSha", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./buildSha")>()),
  bundleSha: () => bundle.sha,
}));

const NARROW = "(max-width: 960px)";

function viewport(narrow: boolean) {
  window.matchMedia = ((query: string) =>
    ({
      matches: narrow && query === NARROW,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList) as typeof window.matchMedia;
}

const logout = vi.fn(async () => undefined);

function renderShell(narrow: boolean, isAdmin = true) {
  viewport(narrow);
  return render(
    <AuthContext.Provider
      value={{
        authRequired: true,
        username: "ops",
        userId: null,
        role: isAdmin ? "admin" : "member",
        email: null,
        accountExpiresAt: null,
        isAdmin,
        can: () => true,
        logout,
      }}
    >
      <MemoryRouter initialEntries={["/v2"]}>
        <Routes>
          <Route element={<V2Shell />}>
            <Route path="/v2" element={<div data-testid="page-home">home</div>} />
            <Route path="/v2/agents" element={<div data-testid="page-agents">agents</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

// N17-obs471: the page renders behind a lazy route chunk; on a loaded host that can take
// longer than the 1 s default of `findBy*`
const LOADED = { timeout: 5_000 };

/**
 * N17-obs471: every request the shell has made has ANSWERED, React has applied the result,
 * and no new request started meanwhile — the condition a negative assertion ("no badge",
 * "never read") needs. A single `setTimeout(0)` was not it: under load the shell's requests
 * and effects had not run yet (the "registry pending badge › member" flake).
 */
async function quiesce(fetchFn: { mock: { calls: unknown[]; results: { value: unknown }[] } }) {
  for (let round = 0; round < 50; round++) {
    const calls = fetchFn.mock.calls.length;
    await act(async () => {
      await Promise.allSettled(fetchFn.mock.results.map((r) => r.value));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    if (fetchFn.mock.calls.length === calls) return;
  }
  throw new Error("the shell kept issuing requests");
}

const aside = () => document.querySelector("aside.v2-side") as HTMLElement;
const toggle = () => screen.getByTestId("v2-nav-toggle");

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
  logout.mockClear();
  bundle.sha = BUNDLE;
});

describe("V2Shell build footer (N13-obs10)", () => {
  // every fetch the shell makes (the health probe, the demo-video catalog) gets this body;
  // only the probe reads `build`
  type Probe = (input: RequestInfo | URL, init?: RequestInit) => Promise<{ ok: boolean; json: () => Promise<unknown> }>;
  const health = (build: unknown) =>
    vi.fn<Probe>(async () => ({
      ok: true, json: async () => ({ status: "ok", version: "0.1.0", region: "cn-northwest-1", build }),
    }));

  // N17-obs466 follow-up (deploy-ops-43): the label is THIS TAB's bundle — read off
  // /api/health it claimed the new build while an old tab still ran the old JS
  const NEWER = "b33c5c43aa11bb22cc33dd44ee55ff6600112233";

  it("labels the tab's own bundle sha; the tooltip names tab, served build and deployed commit", async () => {
    // a backend-only update: the host moved to NEWER, the served dist is still this bundle
    const fetchMock = health({ sha: NEWER, short: NEWER.slice(0, 8), built_at: "2026-10-17T18:40:00Z", source: "file", dist_sha: BUNDLE });
    vi.stubGlobal("fetch", fetchMock);
    try {
      renderShell(false);
      const footer = await screen.findByTestId("v2-build-sha");
      await waitFor(() => expect(footer.getAttribute("data-server-sha")).toBe(BUNDLE));
      expect(footer.textContent).toBe("Build a096762d");
      expect(footer.getAttribute("data-sha")).toBe(BUNDLE);
      expect(footer.getAttribute("data-stale")).toBe("false");
      expect(footer.getAttribute("title")).toBe(`This tab a096762d · server a096762d · deployed commit ${NEWER} · 2026-10-18 02:40:00`);
      expect(aside().contains(footer)).toBe(true);
      expect(fetchMock.mock.calls.some(([url]) => url === "/api/health")).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("an old tab keeps its own sha and is marked like the banner once a newer bundle is served", async () => {
    vi.stubGlobal("fetch", health({ sha: NEWER, short: NEWER.slice(0, 8), built_at: null, source: "file", dist_sha: NEWER }));
    try {
      renderShell(false);
      const footer = await screen.findByTestId("v2-build-sha");
      await waitFor(() => expect(footer.getAttribute("data-stale")).toBe("true"));
      expect(footer.textContent).toBe("Build a096762d · new version available");
      expect(footer.getAttribute("title")).toContain("This tab a096762d · server b33c5c43");
      expect(await screen.findByTestId("v2-new-version")).toBeTruthy(); // the same verdict
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("shows the tab's sha while the server's build is unknown (an older backend / no stamp)", async () => {
    vi.stubGlobal("fetch", health({ sha: null, short: null, built_at: null, source: null }));
    try {
      renderShell(false);
      const footer = await screen.findByTestId("v2-build-sha");
      expect(footer.textContent).toBe("Build a096762d");
      expect(footer.getAttribute("title")).toBe("This tab a096762d · server — · deployed commit — · —");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("a dev-server tab (no compiled sha) falls back to the deployed short sha", async () => {
    bundle.sha = null;
    vi.stubGlobal("fetch", health({ sha: NEWER, short: NEWER.slice(0, 8), built_at: null, source: "file", dist_sha: NEWER }));
    try {
      renderShell(false);
      const footer = await screen.findByTestId("v2-build-sha");
      await waitFor(() => expect(footer.textContent).toBe("Build b33c5c43"));
      expect(footer.getAttribute("data-stale")).toBe("false");
      expect(footer.getAttribute("title")).toContain("This tab — · server b33c5c43");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("renders nothing when neither the tab nor the server knows its build", async () => {
    bundle.sha = null;
    const probe = health({ sha: null, short: null, built_at: null, source: null });
    vi.stubGlobal("fetch", probe);
    try {
      renderShell(false);
      await screen.findByTestId("page-home", undefined, LOADED);
      await waitFor(() => expect(probe.mock.calls.some(([url]) => url === "/api/health")).toBe(true));
      await quiesce(probe); // the probe answered and was applied
      expect(screen.queryByTestId("v2-build-sha")).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("V2Shell restart window (N17-obs470)", () => {
  it("says the console is updating while health fails, then hands over to the new-version banner", async () => {
    const NEWER = "c25ea5391122334455667788990011223344aabb";
    let down = true;
    vi.stubGlobal("fetch", vi.fn(async () => {
      if (down) throw new TypeError("Failed to fetch");
      return { ok: true, json: async () => ({ status: "ok", version: "0.1.0", region: "cn-northwest-1",
        build: { sha: NEWER, short: NEWER.slice(0, 8), built_at: null, source: "file", dist_sha: NEWER } }) };
    }));
    try {
      renderShell(false);
      const notice = await screen.findByTestId("v2-restarting");
      expect(notice.getAttribute("role")).toBe("status");
      expect(notice.textContent).toBe(en.v2.restarting.text);
      expect(screen.getByTestId("page-home")).toBeTruthy(); // non-blocking: the page stays
      down = false;
      await act(async () => { window.dispatchEvent(new Event("focus")); });
      await waitFor(() => expect(screen.queryByTestId("v2-restarting")).toBeNull());
      expect(await screen.findByTestId("v2-new-version")).toBeTruthy();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("V2Shell new-version banner (N17-obs466)", () => {
  type Probe = (input: RequestInfo | URL, init?: RequestInit) => Promise<{ ok: boolean; json: () => Promise<unknown> }>;
  const health = (build: unknown) =>
    vi.fn<Probe>(async () => ({
      ok: true, json: async () => ({ status: "ok", version: "0.1.0", region: "cn-northwest-1", build }),
    }));
  const NEWER = "b33c5c43aa11bb22cc33dd44ee55ff6600112233";

  it("offers a reload above the page when the served dist was built from another commit", async () => {
    vi.stubGlobal("fetch", health({ sha: NEWER, short: NEWER.slice(0, 8), built_at: null, source: "file", dist_sha: NEWER }));
    try {
      renderShell(false);
      const banner = await screen.findByTestId("v2-new-version");
      expect(banner.textContent).toContain("b33c5c43");
      expect(document.querySelector(".v2-main")?.contains(banner)).toBe(true);
      expect(screen.getByTestId("page-home")).toBeTruthy(); // non-blocking: the page stays
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("stays silent after a backend-only update: the host moved, the served bundle did not", async () => {
    vi.stubGlobal("fetch", health({ sha: NEWER, short: NEWER.slice(0, 8), built_at: null, source: "file", dist_sha: BUNDLE }));
    try {
      renderShell(false);
      await screen.findByTestId("v2-build-sha");
      expect(screen.queryByTestId("v2-new-version")).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

/**
 * N17-obs267 (user request 2026-10-22): the 「注册中心」 entry shows an administrator the
 * workspace's pending-approval count — hidden at 0, never for a member — re-read on a route
 * change and after a registry action reports a change (no polling).
 */
describe("V2Shell registry pending badge (N17-obs267)", () => {
  const COUNTS = "/api/registry/records/counts";
  let pending = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url === COUNTS
      ? { pending, approved: 1, draft: 0, rejected: 0, deprecated: 0, total: pending + 1 }
      : { status: "ok", version: "0.1.0", region: "cn-northwest-1" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  });
  const badge = () => screen.queryByTestId("v2-nav-badge-registry");
  const countReads = () => fetchMock.mock.calls.filter(([url]) => String(url) === COUNTS).length;

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockClear();
  });

  it("shows an administrator the pending count and re-reads it after an approval", async () => {
    pending = 2;
    vi.stubGlobal("fetch", fetchMock);
    renderShell(false);
    await waitFor(() => expect(badge()?.textContent).toBe("2"));
    expect(badge()?.getAttribute("aria-label")).toBe("2 registry record(s) awaiting approval");
    expect(screen.getByTestId("v2-nav-/v2/registry").contains(badge())).toBe(true);
    // an approve elsewhere in the console: one fewer pending
    pending = 1;
    act(() => notifyRegistryChanged());
    await waitFor(() => expect(badge()?.textContent).toBe("1"));
    // the last one approved: the badge disappears
    pending = 0;
    act(() => notifyRegistryChanged());
    await waitFor(() => expect(badge()).toBeNull());
  });

  it("re-reads the count on a route change", async () => {
    pending = 3;
    vi.stubGlobal("fetch", fetchMock);
    renderShell(false);
    await waitFor(() => expect(badge()?.textContent).toBe("3"));
    const before = countReads();
    pending = 4;
    fireEvent.click(screen.getByTestId("v2-nav-/v2/agents"));
    await waitFor(() => expect(badge()?.textContent).toBe("4"));
    expect(countReads()).toBeGreaterThan(before);
  });

  it("is hidden at zero", async () => {
    pending = 0;
    vi.stubGlobal("fetch", fetchMock);
    renderShell(false);
    await waitFor(() => expect(countReads()).toBeGreaterThan(0));
    await quiesce(fetchMock); // the count (0) answered and was applied
    expect(badge()).toBeNull();
  });

  it("is never shown to a member, who does not even read the count", async () => {
    pending = 5;
    vi.stubGlobal("fetch", fetchMock);
    renderShell(false, false);
    await screen.findByTestId("page-home", undefined, LOADED);
    // the shell is up and has made its own requests (the health probe) — had the badge hook
    // been enabled for a member, its read would have started with them
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/health")).toBe(true));
    await quiesce(fetchMock);
    act(() => notifyRegistryChanged());
    await quiesce(fetchMock);
    expect(badge()).toBeNull();
    expect(countReads()).toBe(0);
  });
});

describe("V2Shell drawer (WB-004)", () => {
  it("opens the sidebar as a drawer holding the nav and the secondary actions", async () => {
    renderShell(true);
    await screen.findByTestId("page-home", undefined, LOADED);
    const button = toggle();
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.getAttribute("aria-label")).toBe(en.v2.nav.open);
    expect(aside().className).toBe("v2-side");
    // the header holds only the menu button, the brand and the user at this width — the
    // secondary actions already live in the (closed, CSS-hidden) drawer, never twice
    expect(document.querySelector(".v2-top-right .v2-lang")).toBeNull();
    expect(aside().querySelector(".v2-lang")).not.toBeNull();

    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(button.getAttribute("aria-label")).toBe(en.v2.nav.close);
    expect(aside().className).toBe("v2-side open");
    expect(document.activeElement).toBe(aside()); // focus moves into the drawer
    const extras = screen.getByTestId("v2-side-extras");
    expect(aside().contains(extras)).toBe(true);
    expect(extras.querySelector(".v2-lang")).not.toBeNull(); // language toggle
    expect(document.querySelectorAll(".v2-lang")).toHaveLength(1); // never duplicated
    expect(aside().querySelector('a[href="/v2/agents"]')).not.toBeNull(); // the nav itself
    fireEvent.click(screen.getByTestId("v2-sign-out"));
    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape and hands focus back to the button", async () => {
    renderShell(true);
    await screen.findByTestId("page-home", undefined, LOADED);
    fireEvent.click(toggle());
    expect(aside().className).toBe("v2-side open");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(aside().className).toBe("v2-side");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(toggle());
    expect(screen.queryByTestId("v2-nav-backdrop")).toBeNull();
  });

  it("closes on a backdrop click", async () => {
    renderShell(true);
    await screen.findByTestId("page-home", undefined, LOADED);
    fireEvent.click(toggle());
    fireEvent.click(screen.getByTestId("v2-nav-backdrop"));
    expect(aside().className).toBe("v2-side");
    expect(document.activeElement).toBe(toggle());
  });

  it("closes when a nav link changes the route", async () => {
    renderShell(true);
    await screen.findByTestId("page-home", undefined, LOADED);
    fireEvent.click(toggle());
    fireEvent.click(screen.getByTestId("v2-nav-/v2/agents"));
    await screen.findByTestId("page-agents");
    expect(aside().className).toBe("v2-side");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
  });

  it("keeps the desktop markup above 960 px", async () => {
    renderShell(false);
    await screen.findByTestId("page-home", undefined, LOADED);
    // the actions stay in the top bar; the drawer extras do not exist
    expect(document.querySelector(".v2-top-right .v2-lang")).not.toBeNull();
    expect(screen.queryByTestId("v2-side-extras")).toBeNull();
    expect(screen.queryByTestId("v2-nav-backdrop")).toBeNull();
    expect(aside().className).toBe("v2-side");
    // the toggle is in the DOM (hidden by CSS below the breakpoint only) and inert here
    fireEvent.click(toggle());
    expect(aside().className).toBe("v2-side");
    expect(screen.queryByTestId("v2-nav-backdrop")).toBeNull();
  });
});
