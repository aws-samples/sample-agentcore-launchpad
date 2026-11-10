// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { Component, lazy } from "react";
import type { ReactNode } from "react";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import en from "../locales/en/common.json";
import { clearReloadMark, pageReloader } from "./chunk-reload";
import { RouteChunk } from "./RouteChunk";

/**
 * WB-003: a stale chunk (the host was rebuilt under an open tab) reloads the tab once by
 * itself; a chunk that is still missing after that reload shows the manual state; a page
 * that renders clears the one-shot mark again.
 */

const PATH = "/v2/eval/tasks";
const MARK = `launchpad.route-chunk.reloaded:${PATH}`;

/** The app-level boundary a genuine render bug must still reach. */
class Outer extends Component<{ children: ReactNode }, { message: string | null }> {
  state = { message: null as string | null };
  static getDerivedStateFromError(error: unknown) {
    return { message: error instanceof Error ? error.message : String(error) };
  }
  render() {
    return this.state.message === null ? (
      this.props.children
    ) : (
      <p data-testid="outer-boundary">{this.state.message}</p>
    );
  }
}

function stalePage() {
  // the message Chrome/Firefox produce when a hashed module 404s (or comes back as HTML)
  return lazy<() => null>(() =>
    Promise.reject(
      new TypeError("Failed to fetch dynamically imported module: http://h/assets/Eval-x.js"),
    ),
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

beforeEach(() => {
  window.history.replaceState({}, "", PATH);
  sessionStorage.clear();
  pageReloader.reload = vi.fn();
  // React reports boundary-caught errors loudly; the tests assert the behaviour instead
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RouteChunk (WB-003)", () => {
  it("reloads once on the first stale chunk and remembers it for the path", async () => {
    const Stale = stalePage();
    render(
      <RouteChunk>
        <Stale />
      </RouteChunk>,
    );
    await waitFor(() => expect(pageReloader.reload).toHaveBeenCalledTimes(1));
    expect(sessionStorage.getItem(MARK)).not.toBeNull();
    expect(screen.queryByTestId("route-chunk-error")).toBeNull();
    expect(screen.getByTestId("route-chunk-reloading").textContent).toBe(en.routeChunk.reloading);
  });

  it("falls back to the manual reload state when the chunk is still missing after a reload", async () => {
    sessionStorage.setItem(MARK, new Date().toISOString());
    const Stale = stalePage();
    render(
      <RouteChunk>
        <Stale />
      </RouteChunk>,
    );
    const manual = await screen.findByTestId("route-chunk-error");
    expect(manual.textContent).toContain(en.routeChunk.failed);
    expect(pageReloader.reload).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(MARK)).not.toBeNull(); // still armed until a page renders
  });

  it("goes straight to the manual state while the browser is offline — no auto reload", async () => {
    // r7-e: a reload while offline lands on the browser's offline page and loses the tab
    Object.defineProperty(window.navigator, "onLine", { configurable: true, value: false });
    try {
      const Stale = stalePage();
      render(
        <RouteChunk>
          <Stale />
        </RouteChunk>,
      );
      const manual = await screen.findByTestId("route-chunk-error");
      expect(manual.textContent).toContain(en.routeChunk.failed);
      expect(screen.queryByTestId("route-chunk-reloading")).toBeNull();
      expect(pageReloader.reload).not.toHaveBeenCalled();
      expect(sessionStorage.getItem(MARK)).toBeNull(); // the one-shot reload is still available
    } finally {
      Object.defineProperty(window.navigator, "onLine", { configurable: true, value: true });
    }
  });

  it("clears the mark once the page chunk renders", async () => {
    sessionStorage.setItem(MARK, new Date().toISOString());
    const Fine = lazy(() => Promise.resolve({ default: () => <p data-testid="page">ok</p> }));
    render(
      <RouteChunk>
        <Fine />
      </RouteChunk>,
    );
    await screen.findByTestId("page");
    await waitFor(() => expect(sessionStorage.getItem(MARK)).toBeNull());
    expect(pageReloader.reload).not.toHaveBeenCalled();
  });

  it("re-throws anything that is not a chunk-load error to the outer boundary", async () => {
    const Broken = lazy(() => Promise.reject(new Error("genuine render bug")));
    render(
      <Outer>
        <RouteChunk>
          <Broken />
        </RouteChunk>
      </Outer>,
    );
    const outer = await screen.findByTestId("outer-boundary");
    expect(outer.textContent).toBe("genuine render bug");
    expect(pageReloader.reload).not.toHaveBeenCalled();
    expect(screen.queryByTestId("route-chunk-error")).toBeNull();
    expect(sessionStorage.getItem(MARK)).toBeNull();
    clearReloadMark(PATH);
  });
});
