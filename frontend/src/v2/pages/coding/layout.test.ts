import { describe, expect, it } from "vitest";

import {
  CHAT_MIN,
  closePanel,
  computeLayout,
  DEFAULT_LAYOUT,
  defaultWorkbenchWidth,
  dragWidth,
  FILES,
  fitsInline,
  GUTTER,
  keyWidth,
  type LayoutPrefs,
  openPanel,
  overlayWidth,
  RAIL,
  shownOverlay,
  WORKBENCH,
  withCollapsed,
  withWidth,
} from "./layout";

const prefs = (over: Partial<LayoutPrefs> = {}): LayoutPrefs => ({ ...DEFAULT_LAYOUT, ...over });

describe("computeLayout", () => {
  it("gives the defaults on a wide grid, the conversation taking the rest", () => {
    const l = computeLayout(1600, prefs());
    expect(l.files).toMatchObject({ open: true, squeezed: false, width: 240 });
    expect(l.workbench).toMatchObject({ open: true, squeezed: false, width: 640 });
    expect(l.chat).toBe(1600 - 2 * GUTTER - 240 - 640);
    expect(l.columns).toBe(`240px ${GUTTER}px minmax(0, 1fr) ${GUTTER}px 640px`);
    // the handles' ranges never squeeze the other panel or the conversation
    expect(l.files.max).toBe(FILES.max);
    expect(l.workbench.max).toBe(1600 - 2 * GUTTER - 240 - CHAT_MIN);
    expect(l.workbench.min).toBe(WORKBENCH.min);
  });

  it("shrinks the Workbench before the conversation drops below its minimum (1366 px laptop)", () => {
    const l = computeLayout(1102, prefs());
    expect(l.files.width).toBe(240);
    expect(l.workbench.width).toBe(1078 - 240 - CHAT_MIN);
    expect(l.chat).toBe(CHAT_MIN);
    expect(l.files.max).toBe(240); // no room to grow: the Workbench is already at what is left
  });

  it("caps Files so the Workbench keeps its minimum", () => {
    const l = computeLayout(1102, prefs({ filesWidth: 480 }));
    expect(l.workbench.width).toBe(WORKBENCH.min);
    expect(l.files.width).toBe(1078 - WORKBENCH.min - CHAT_MIN);
    expect(l.chat).toBe(CHAT_MIN);
  });

  it("squeezes the Workbench first, then Files, as the grid narrows", () => {
    const fits = computeLayout(2 * GUTTER + FILES.min + WORKBENCH.min + CHAT_MIN, prefs());
    expect([fits.files.open, fits.workbench.open]).toEqual([true, true]);
    expect([fits.files.width, fits.workbench.width, fits.chat]).toEqual([FILES.min, WORKBENCH.min, CHAT_MIN]);

    const tight = computeLayout(2 * GUTTER + FILES.min + WORKBENCH.min + CHAT_MIN - 1, prefs());
    expect(tight.workbench).toMatchObject({ open: false, squeezed: true, width: RAIL });
    expect(tight.files).toMatchObject({ open: true, squeezed: false, width: 240 });

    const narrow = computeLayout(2 * GUTTER + FILES.min + RAIL + CHAT_MIN - 1, prefs());
    expect(narrow.files).toMatchObject({ open: false, squeezed: true, width: RAIL });
    expect(narrow.workbench).toMatchObject({ open: false, squeezed: true, width: RAIL });
    expect(narrow.columns).toBe(`${RAIL}px ${GUTTER}px minmax(0, 1fr) ${GUTTER}px ${RAIL}px`);
  });

  it("keeps both rails on a phone-width grid without overflowing it", () => {
    const l = computeLayout(360, prefs());
    expect([l.files.open, l.workbench.open]).toEqual([false, false]);
    expect(l.chat).toBe(360 - 2 * GUTTER - 2 * RAIL);
  });

  it("does not report a user-collapsed panel as squeezed", () => {
    const l = computeLayout(1600, prefs({ filesCollapsed: true, workbenchCollapsed: true }));
    expect(l.files).toMatchObject({ open: false, squeezed: false, width: RAIL });
    expect(l.workbench).toMatchObject({ open: false, squeezed: false, width: RAIL });
    expect(l.chat).toBe(1600 - 2 * GUTTER - 2 * RAIL);
  });

  it("gives a collapsed Files' room to the Workbench and the conversation", () => {
    const l = computeLayout(1000, prefs({ filesCollapsed: true }));
    expect(l.workbench.open).toBe(true);
    expect(l.workbench.width).toBe(Math.min(defaultWorkbenchWidth(1000), 1000 - 2 * GUTTER - RAIL - CHAT_MIN));
  });

  it("clamps stored widths to what the grid allows", () => {
    const l = computeLayout(1600, prefs({ filesWidth: 9999, workbenchWidth: 3000 }));
    expect(l.files.width).toBe(FILES.max);
    expect(l.workbench.width).toBe(1600 - 2 * GUTTER - FILES.max - CHAT_MIN);
    expect(l.chat).toBe(CHAT_MIN);
    const small = computeLayout(1600, prefs({ filesWidth: 10, workbenchWidth: 10 }));
    expect([small.files.width, small.workbench.width]).toEqual([FILES.min, WORKBENCH.min]);
  });

  it("uses the preferences as they are before the grid was measured", () => {
    const l = computeLayout(0, prefs());
    expect(l.files).toMatchObject({ open: true, squeezed: false, width: FILES.initial });
    expect(l.workbench).toMatchObject({ open: true, squeezed: false, width: WORKBENCH.fallback });
  });

  it("always fills the grid exactly and keeps the conversation minimum while a panel is open", () => {
    const widths = [null, 100, 180, 300, 480, 700, 1200];
    for (let container = 300; container <= 2600; container += 37) {
      for (const filesWidth of widths) {
        for (const workbenchWidth of widths) {
          for (const filesCollapsed of [false, true]) {
            for (const workbenchCollapsed of [false, true]) {
              const p = prefs({ filesWidth, workbenchWidth, filesCollapsed, workbenchCollapsed });
              const l = computeLayout(container, p);
              expect(l.files.width + l.workbench.width + l.chat + 2 * GUTTER).toBe(container);
              if (l.files.open || l.workbench.open) expect(l.chat).toBeGreaterThanOrEqual(CHAT_MIN);
              if (l.files.open) {
                expect(l.files.width).toBeGreaterThanOrEqual(FILES.min);
                expect(l.files.width).toBeLessThanOrEqual(FILES.max);
                expect(l.files.width).toBeLessThanOrEqual(l.files.max);
              }
              if (l.workbench.open) {
                expect(l.workbench.width).toBeGreaterThanOrEqual(WORKBENCH.min);
                expect(l.workbench.width).toBeLessThanOrEqual(l.workbench.max);
              }
              // the Workbench is squeezed first: Files is never squeezed while it is inline
              if (l.files.squeezed) expect(l.workbench.open).toBe(false);
              expect(l.files.squeezed).toBe(!filesCollapsed && !l.files.open);
              expect(l.workbench.squeezed).toBe(!workbenchCollapsed && !l.workbench.open);
            }
          }
        }
      }
    }
  });
});

describe("opening and closing panels", () => {
  it("opens inline when the panel fits, changing the preference", () => {
    const move = openPanel(1600, prefs({ workbenchCollapsed: true }), "workbench", null);
    expect(move).toEqual({ prefs: prefs(), overlay: null });
    expect(fitsInline(1600, prefs({ workbenchCollapsed: true }), "workbench")).toBe(true);
  });

  it("opens as an overlay when there is no room, leaving the preference alone", () => {
    const p = prefs({ workbenchCollapsed: true });
    expect(fitsInline(900, p, "workbench")).toBe(false);
    expect(openPanel(900, p, "workbench", "files")).toEqual({ prefs: p, overlay: "workbench" });
    // a squeezed panel the user wants open also opens as an overlay
    expect(openPanel(900, prefs(), "workbench", null)).toEqual({ prefs: prefs(), overlay: "workbench" });
  });

  it("fits the Workbench inline on a mid grid once Files is a rail", () => {
    expect(fitsInline(900, prefs(), "workbench")).toBe(false);
    expect(fitsInline(900, prefs({ filesCollapsed: true }), "workbench")).toBe(true);
  });

  it("never pushes the other panel to its rail to open one", () => {
    // 900 px: the Workbench fits next to the Files rail, not next to open Files
    const p = prefs({ filesCollapsed: true });
    expect(computeLayout(900, p).workbench.open).toBe(true);
    expect(fitsInline(900, p, "files")).toBe(false);
    expect(openPanel(900, p, "files", null)).toEqual({ prefs: p, overlay: "files" });
  });

  it("keeps the other panel's overlay when opening inline", () => {
    expect(openPanel(1600, prefs({ filesCollapsed: true }), "files", "workbench").overlay).toBe("workbench");
    expect(openPanel(1600, prefs({ filesCollapsed: true }), "files", "files").overlay).toBeNull();
  });

  it("closes an overlay, else collapses to the rail", () => {
    expect(closePanel(prefs(), "workbench", "workbench")).toEqual({ prefs: prefs(), overlay: null });
    expect(closePanel(prefs(), "files", null)).toEqual({ prefs: prefs({ filesCollapsed: true }), overlay: null });
    expect(closePanel(prefs(), "files", "workbench")).toEqual({ prefs: prefs({ filesCollapsed: true }), overlay: "workbench" });
  });

  it("drops an overlay whose panel is inline again (the grid grew)", () => {
    expect(shownOverlay(computeLayout(900, prefs()), "workbench")).toBe("workbench");
    expect(shownOverlay(computeLayout(1600, prefs()), "workbench")).toBeNull();
    expect(shownOverlay(computeLayout(1600, prefs({ workbenchCollapsed: true })), "workbench")).toBe("workbench");
    expect(shownOverlay(computeLayout(1600, prefs()), null)).toBeNull();
  });

  it("sizes an overlay by the preference, leaving both rails visible", () => {
    expect(overlayWidth(900, prefs(), "workbench")).toBe(WORKBENCH.fallback);
    expect(overlayWidth(1000, prefs({ workbenchWidth: 700 }), "workbench")).toBe(700);
    expect(overlayWidth(900, prefs({ workbenchWidth: 900 }), "workbench")).toBe(900 - 2 * (RAIL + GUTTER));
    expect(overlayWidth(900, prefs({ filesWidth: 300 }), "files")).toBe(300);
    expect(overlayWidth(400, prefs(), "workbench")).toBe(400 - 2 * (RAIL + GUTTER));
    expect(overlayWidth(400, prefs(), "files")).toBe(FILES.initial);
    expect(overlayWidth(50, prefs(), "files")).toBe(0);
  });

  it("updates preferences only on a change", () => {
    const p = prefs();
    expect(withCollapsed(p, "files", false)).toBe(p);
    expect(withCollapsed(p, "workbench", true)).toEqual(prefs({ workbenchCollapsed: true }));
    expect(withWidth(p, "files", null)).toBe(p);
    expect(withWidth(p, "files", 300.4)).toEqual(prefs({ filesWidth: 300 }));
    expect(withWidth(prefs({ workbenchWidth: 700 }), "workbench", null)).toEqual(p);
  });
});

describe("resizing", () => {
  const files = { min: FILES.min, max: FILES.max };
  const bench = { min: WORKBENCH.min, max: 900 };

  it("steps Files with ←/→ (Shift for a big step), Home/End to the ends", () => {
    expect(keyWidth("files", "ArrowRight", false, 240, files)).toBe(256);
    expect(keyWidth("files", "ArrowRight", true, 240, files)).toBe(304);
    expect(keyWidth("files", "ArrowLeft", false, 240, files)).toBe(224);
    expect(keyWidth("files", "ArrowLeft", true, 200, files)).toBe(FILES.min);
    expect(keyWidth("files", "ArrowRight", true, 470, files)).toBe(FILES.max);
    expect(keyWidth("files", "Home", false, 300, files)).toBe(FILES.min);
    expect(keyWidth("files", "End", false, 300, files)).toBe(FILES.max);
    expect(keyWidth("files", "Enter", false, 300, files)).toBeNull();
    expect(keyWidth("files", "ArrowUp", false, 300, files)).toBeNull();
  });

  it("steps the Workbench the other way (its handle is on its left edge)", () => {
    expect(keyWidth("workbench", "ArrowLeft", false, 500, bench)).toBe(516);
    expect(keyWidth("workbench", "ArrowRight", false, 500, bench)).toBe(484);
    expect(keyWidth("workbench", "ArrowRight", true, 330, bench)).toBe(WORKBENCH.min);
    expect(keyWidth("workbench", "ArrowLeft", true, 880, bench)).toBe(900);
  });

  it("follows the pointer within the handle's range", () => {
    expect(dragWidth("files", 240, 50, files)).toBe(290);
    expect(dragWidth("files", 240, -100, files)).toBe(FILES.min);
    expect(dragWidth("files", 240, 400, files)).toBe(FILES.max);
    expect(dragWidth("workbench", 500, -50.6, bench)).toBe(551);
    expect(dragWidth("workbench", 500, 300, bench)).toBe(WORKBENCH.min);
    expect(dragWidth("workbench", 500, -1000, bench)).toBe(900);
  });
});
