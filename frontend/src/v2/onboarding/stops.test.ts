import { describe, expect, it } from "vitest";

import en from "../../locales/en/common.json";
import zh from "../../locales/zh-CN/common.json";
import {
  EDGE,
  GAP,
  isTourHome,
  OVERLAY_SELECTOR,
  placeCard,
  seekStop,
  TOUR_STOPS,
  tourKeyAction,
  visibleStops,
} from "./stops";

const onScreen = (...keys: string[]) => (selector: string) => keys.some((k) => selector === `[data-tour="${k}"]`);
const lookup = (locale: unknown, key: string) =>
  key.split(".").reduce<unknown>((at, part) => (at as Record<string, unknown> | undefined)?.[part], locale);

describe("tour stops", () => {
  it("walks palette, checklist, KPIs, agents, health, sidebar, help — at most 7", () => {
    expect(TOUR_STOPS.map((s) => s.key)).toEqual(["cmdk", "launch", "stats", "agents", "health", "modules", "help"]);
    expect(TOUR_STOPS.length).toBeLessThanOrEqual(7);
    for (const s of TOUR_STOPS) expect(s.selector).toBe(`[data-tour="${s.key}"]`);
  });

  it("has a title and a body for every stop in both locales", () => {
    for (const locale of [en, zh]) {
      for (const s of TOUR_STOPS) {
        expect(typeof lookup(locale, s.titleKey)).toBe("string");
        expect(typeof lookup(locale, s.bodyKey)).toBe("string");
      }
      for (const k of ["label", "counter", "skip", "back", "next", "done", "end"]) {
        expect(typeof lookup(locale, `v2.onboard.tour.${k}`)).toBe("string");
      }
    }
  });

  it("keeps only the stops whose target is on screen, in order", () => {
    const all = visibleStops(TOUR_STOPS, () => true);
    expect(all).toEqual(TOUR_STOPS);
    // a hidden checklist, a page without Home's cards
    expect(visibleStops(TOUR_STOPS, onScreen("help", "cmdk", "modules")).map((s) => s.key)).toEqual([
      "cmdk",
      "modules",
      "help",
    ]);
    expect(visibleStops(TOUR_STOPS, () => false)).toEqual([]);
  });

  it("asks the injected check about each stop's own selector", () => {
    const asked: string[] = [];
    visibleStops(TOUR_STOPS, (selector) => {
      asked.push(selector);
      return false;
    });
    expect(asked).toEqual(TOUR_STOPS.map((s) => s.selector));
  });
});

describe("seekStop", () => {
  const launch = TOUR_STOPS.findIndex((s) => s.key === "launch");
  it("finds the next stop on screen, skipping missing ones", () => {
    const exists = onScreen("cmdk", "health", "help");
    expect(seekStop(TOUR_STOPS, 0, 1, exists)).toBe(0);
    expect(seekStop(TOUR_STOPS, 1, 1, exists)).toBe(TOUR_STOPS.findIndex((s) => s.key === "health"));
    expect(seekStop(TOUR_STOPS, TOUR_STOPS.length - 1, 1, exists)).toBe(TOUR_STOPS.length - 1);
    expect(seekStop(TOUR_STOPS, TOUR_STOPS.length, 1, exists)).toBe(-1);
  });
  it("walks back the same way", () => {
    const exists = onScreen("cmdk", "health", "help");
    expect(seekStop(TOUR_STOPS, 3, -1, exists)).toBe(0);
    expect(seekStop(TOUR_STOPS, -1, -1, exists)).toBe(-1);
    expect(seekStop(TOUR_STOPS, 3, -1, onScreen("help"))).toBe(-1);
  });
  it("re-checks at the moment a stop is reached: a checklist that mounted late is included", () => {
    let mounted = false;
    const exists = (selector: string) => selector !== '[data-tour="launch"]' || mounted;
    expect(seekStop(TOUR_STOPS, 1, 1, exists)).toBe(launch + 1);
    mounted = true;
    expect(seekStop(TOUR_STOPS, 1, 1, exists)).toBe(launch);
  });
});

describe("placeCard", () => {
  const card = { width: 360, height: 200 };
  const viewport = { width: 1440, height: 900 };
  it("centres the card when nothing is lit", () => {
    expect(placeCard(null, card, viewport)).toEqual({ left: 540, top: 350 });
  });
  it("puts it right of the tall, narrow sidebar", () => {
    const side = { left: 0, top: 56, width: 216, height: 844 };
    expect(placeCard(side, card, viewport)).toEqual({ left: 216 + GAP, top: 96 });
  });
  it("puts it below a target with room underneath, inside the viewport", () => {
    const help = { left: 1300, top: 13, width: 30, height: 30 };
    expect(placeCard(help, card, viewport)).toEqual({ left: 1440 - 360 - EDGE, top: 43 + GAP });
  });
  it("puts it above a target near the bottom", () => {
    const low = { left: 300, top: 650, width: 600, height: 200 };
    expect(placeCard(low, card, viewport)).toEqual({ left: 300, top: 650 - GAP - 200 });
  });
  it("pins it to the bottom edge when neither side has room", () => {
    const huge = { left: 240, top: 60, width: 900, height: 800 };
    expect(placeCard(huge, card, viewport)).toEqual({ left: 240, top: 900 - 200 - EDGE });
  });
  it("clamps into a narrow viewport", () => {
    const pos = placeCard({ left: -50, top: 10, width: 20, height: 20 }, card, { width: 380, height: 700 });
    expect(pos.left).toBe(EDGE);
  });
});

describe("when the tour runs", () => {
  it("starts on V2 Home only", () => {
    expect(isTourHome("/v2")).toBe(true);
    expect(isTourHome("/v2/")).toBe(true);
    expect(isTourHome("/v2/agents")).toBe(false);
    expect(isTourHome("/")).toBe(false);
  });
  it("waits behind dialogs, menus and dropdowns, but not behind itself", () => {
    expect(OVERLAY_SELECTOR).toContain('[role="dialog"]:not(.v2-tour)');
    expect(OVERLAY_SELECTOR).toContain('[role="menu"]');
    expect(OVERLAY_SELECTOR).toContain(".v2-pop");
  });
});

describe("tourKeyAction", () => {
  const key = (k: string, mods: Partial<Record<"metaKey" | "ctrlKey" | "altKey" | "isComposing", boolean>> = {}) => ({
    key: k,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    ...mods,
  });
  const free = { paused: false, inPalette: false, typing: false, onButton: false };
  it("→ / Enter next, ← back, Esc end", () => {
    expect(tourKeyAction(key("ArrowRight"), free)).toBe("next");
    expect(tourKeyAction(key("Enter"), free)).toBe("next");
    expect(tourKeyAction(key("ArrowLeft"), free)).toBe("back");
    expect(tourKeyAction(key("Escape"), free)).toBe("end");
    expect(tourKeyAction(key("a"), free)).toBe(null);
    expect(tourKeyAction(key("Tab"), free)).toBe(null);
  });
  it("leaves Enter on a card button to the button (no double step)", () => {
    expect(tourKeyAction(key("Enter"), { ...free, onButton: true })).toBe(null);
    expect(tourKeyAction(key("ArrowRight"), { ...free, onButton: true })).toBe("next");
  });
  it("takes no key while paused, in the palette, or typing", () => {
    for (const where of [{ paused: true }, { inPalette: true }, { typing: true }]) {
      for (const k of ["Escape", "Enter", "ArrowRight", "ArrowLeft"]) {
        expect(tourKeyAction(key(k), { ...free, ...where })).toBe(null);
      }
    }
  });
  it("ignores modified keys and IME composition", () => {
    expect(tourKeyAction(key("k", { metaKey: true }), free)).toBe(null);
    expect(tourKeyAction(key("ArrowRight", { ctrlKey: true }), free)).toBe(null);
    expect(tourKeyAction(key("ArrowLeft", { altKey: true }), free)).toBe(null);
    expect(tourKeyAction(key("Enter", { isComposing: true }), free)).toBe(null);
  });
});
