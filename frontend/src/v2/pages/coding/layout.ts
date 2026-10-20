// The IDE's three-column layout as pure math: Files (left) · Conversation (center) ·
// Workbench (right: editor tabs + Changes). The side panels are user-sized and
// collapsible; the conversation takes what is left and never drops below CHAT_MIN —
// when the grid is too narrow for a panel the user asked to see, that panel is shown
// as a rail ("squeezed", the Workbench first, then Files) and opens as an overlay.
// No DOM here: `useIdeLayout` measures the grid and feeds the width in.

export type Panel = "files" | "workbench";

/** Width of a collapsed panel's rail. */
export const RAIL = 40;
/** The column between a side panel and the conversation (holds the resize handle). */
export const GUTTER = 12;
export const FILES = { min: 180, max: 480, initial: 240 } as const;
/** No fixed max: the conversation minimum is the limit. `ratio` of the grid is the
 *  default width (40 %: wide enough for a side-by-side diff, while the conversation
 *  stays the larger column at the console's 1480 px content cap); `fallback` before
 *  the grid was measured. */
export const WORKBENCH = { min: 320, ratio: 0.4, fallback: 520 } as const;
export const CHAT_MIN = 420;
/** A sane ceiling for a stored width (a value above it is junk, not a preference). */
export const STORED_MAX = 4000;
/** Below this width the Workbench stacks its Changes list over the diff. */
export const WORKBENCH_COMPACT = 560;
export const KEY_STEP = 16;
export const KEY_STEP_BIG = 64;

/** What the user asked for (persisted). A null width = the default. */
export interface LayoutPrefs {
  filesCollapsed: boolean;
  filesWidth: number | null;
  workbenchCollapsed: boolean;
  workbenchWidth: number | null;
}

export const DEFAULT_LAYOUT: LayoutPrefs = {
  filesCollapsed: false,
  filesWidth: null,
  workbenchCollapsed: false,
  workbenchWidth: null,
};

export interface PanelLayout {
  /** shown inline at `width`; otherwise a rail (`width` = RAIL) */
  open: boolean;
  /** asked to be open, but the grid has no room for it: a rail, opened as an overlay */
  squeezed: boolean;
  width: number;
  /** the resize handle's range at the current layout (only meaningful while open):
   *  growing never squeezes the other panel or the conversation below its minimum */
  min: number;
  max: number;
}

export interface Layout {
  /** the measured grid width (0 = not measured yet: nothing is squeezed) */
  container: number;
  files: PanelLayout;
  workbench: PanelLayout;
  /** the conversation column's width (the `1fr` track) */
  chat: number;
  /** `grid-template-columns` for files · gutter · chat · gutter · workbench */
  columns: string;
}

export function clamp(value: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, value));
}

export function isCollapsed(prefs: LayoutPrefs, panel: Panel): boolean {
  return panel === "files" ? prefs.filesCollapsed : prefs.workbenchCollapsed;
}

export function storedWidth(prefs: LayoutPrefs, panel: Panel): number | null {
  return panel === "files" ? prefs.filesWidth : prefs.workbenchWidth;
}

export function withCollapsed(prefs: LayoutPrefs, panel: Panel, collapsed: boolean): LayoutPrefs {
  if (isCollapsed(prefs, panel) === collapsed) return prefs;
  return panel === "files" ? { ...prefs, filesCollapsed: collapsed } : { ...prefs, workbenchCollapsed: collapsed };
}

/** `width` null resets the panel to its default width. */
export function withWidth(prefs: LayoutPrefs, panel: Panel, width: number | null): LayoutPrefs {
  const next = width === null ? null : Math.round(width);
  if (storedWidth(prefs, panel) === next) return prefs;
  return panel === "files" ? { ...prefs, filesWidth: next } : { ...prefs, workbenchWidth: next };
}

/** The Workbench's default width for a grid. */
export function defaultWorkbenchWidth(container: number): number {
  return container > 0 ? Math.round(container * WORKBENCH.ratio) : WORKBENCH.fallback;
}

function preferredFiles(prefs: LayoutPrefs): number {
  return clamp(prefs.filesWidth ?? FILES.initial, FILES.min, FILES.max);
}

function preferredWorkbench(prefs: LayoutPrefs, container: number): number {
  return Math.max(WORKBENCH.min, prefs.workbenchWidth ?? defaultWorkbenchWidth(container));
}

function columns(files: number, workbench: number): string {
  return `${files}px ${GUTTER}px minmax(0, 1fr) ${GUTTER}px ${workbench}px`;
}

/**
 * The layout for a grid `container` px wide. Files keeps its preferred width unless
 * that would push the Workbench below its minimum; the Workbench takes its preferred
 * width up to what leaves the conversation CHAT_MIN. When even the minimums do not
 * fit, the Workbench is squeezed to a rail first, then Files.
 */
export function computeLayout(container: number, prefs: LayoutPrefs): Layout {
  const measured = container > 0;
  let filesOpen = !prefs.filesCollapsed;
  let workbenchOpen = !prefs.workbenchCollapsed;

  if (!measured) {
    // before the first measurement: the preferences as they are (never painted —
    // the grid is measured in a layout effect)
    const files = filesOpen ? preferredFiles(prefs) : RAIL;
    const workbench = workbenchOpen ? preferredWorkbench(prefs, 0) : RAIL;
    return {
      container: 0,
      files: { open: filesOpen, squeezed: false, width: files, min: FILES.min, max: FILES.max },
      workbench: { open: workbenchOpen, squeezed: false, width: workbench, min: WORKBENCH.min, max: STORED_MAX },
      chat: 0,
      columns: columns(files, workbench),
    };
  }

  const inner = Math.max(0, container - 2 * GUTTER);
  const filesFloor = () => (filesOpen ? FILES.min : RAIL);
  let workbenchSqueezed = false;
  let filesSqueezed = false;
  if (workbenchOpen && filesFloor() + WORKBENCH.min + CHAT_MIN > inner) {
    workbenchOpen = false;
    workbenchSqueezed = true;
  }
  if (filesOpen && FILES.min + (workbenchOpen ? WORKBENCH.min : RAIL) + CHAT_MIN > inner) {
    filesOpen = false;
    filesSqueezed = true;
  }

  const workbenchFloor = workbenchOpen ? WORKBENCH.min : RAIL;
  const files = filesOpen
    ? clamp(preferredFiles(prefs), FILES.min, Math.min(FILES.max, inner - workbenchFloor - CHAT_MIN))
    : RAIL;
  const workbench = workbenchOpen
    ? clamp(preferredWorkbench(prefs, container), WORKBENCH.min, inner - files - CHAT_MIN)
    : RAIL;
  return {
    container,
    files: {
      open: filesOpen,
      squeezed: filesSqueezed,
      width: files,
      min: FILES.min,
      max: filesOpen ? Math.max(FILES.min, Math.min(FILES.max, inner - workbench - CHAT_MIN)) : FILES.max,
    },
    workbench: {
      open: workbenchOpen,
      squeezed: workbenchSqueezed,
      width: workbench,
      min: WORKBENCH.min,
      max: workbenchOpen ? Math.max(WORKBENCH.min, inner - files - CHAT_MIN) : WORKBENCH.min,
    },
    chat: Math.max(0, inner - files - workbench),
    columns: columns(files, workbench),
  };
}

/** Whether `panel` would show inline if the user opened it now — without pushing the
 *  other panel off to its rail (opening one panel never hides the other). */
export function fitsInline(container: number, prefs: LayoutPrefs, panel: Panel): boolean {
  const other: Panel = panel === "files" ? "workbench" : "files";
  const before = computeLayout(container, prefs);
  const after = computeLayout(container, withCollapsed(prefs, panel, false));
  return after[panel].open && after[other].open === before[other].open;
}

/** The result of a layout action: the new preferences and the overlay to show. */
export interface LayoutMove {
  prefs: LayoutPrefs;
  overlay: Panel | null;
}

/**
 * Open a panel (a rail click, or a file opened elsewhere for the Workbench): inline
 * when it fits — the preference changes — else as an overlay over the conversation
 * (transient: the preference stays as it was). One overlay at a time.
 */
export function openPanel(container: number, prefs: LayoutPrefs, panel: Panel, overlay: Panel | null): LayoutMove {
  if (fitsInline(container, prefs, panel)) {
    return { prefs: withCollapsed(prefs, panel, false), overlay: overlay === panel ? null : overlay };
  }
  return { prefs, overlay: panel };
}

/** Close a panel: its overlay if it is showing as one, else collapse it to its rail. */
export function closePanel(prefs: LayoutPrefs, panel: Panel, overlay: Panel | null): LayoutMove {
  if (overlay === panel) return { prefs, overlay: null };
  return { prefs: withCollapsed(prefs, panel, true), overlay };
}

/** The overlay to show for the current layout: an overlay of a panel that is now
 *  inline (the grid grew) is dropped. */
export function shownOverlay(layout: Layout, overlay: Panel | null): Panel | null {
  return overlay !== null && !layout[overlay].open ? overlay : null;
}

/** An overlay's width: the panel's preferred width — the Workbench at least its
 *  fallback width (it covers the conversation anyway, so the editor gets room) —
 *  leaving both rails visible. */
export function overlayWidth(container: number, prefs: LayoutPrefs, panel: Panel): number {
  const preferred =
    panel === "files" ? preferredFiles(prefs) : Math.max(WORKBENCH.fallback, preferredWorkbench(prefs, container));
  if (container <= 0) return preferred;
  return Math.max(0, Math.min(preferred, container - 2 * (RAIL + GUTTER)));
}

/** The panel width a resize key asks for, or null for a key the handle ignores.
 *  The Workbench's handle is on its left edge, so ← widens it. */
export function keyWidth(
  panel: Panel,
  key: string,
  shift: boolean,
  width: number,
  bounds: { min: number; max: number },
): number | null {
  const step = shift ? KEY_STEP_BIG : KEY_STEP;
  const grow = panel === "files" ? "ArrowRight" : "ArrowLeft";
  const shrink = panel === "files" ? "ArrowLeft" : "ArrowRight";
  let next: number;
  if (key === grow) next = width + step;
  else if (key === shrink) next = width - step;
  else if (key === "Home") next = bounds.min;
  else if (key === "End") next = bounds.max;
  else return null;
  return clamp(next, bounds.min, bounds.max);
}

/** The panel width while dragging its handle `dx` px from where the drag started. */
export function dragWidth(panel: Panel, startWidth: number, dx: number, bounds: { min: number; max: number }): number {
  return clamp(Math.round(panel === "files" ? startWidth + dx : startWidth - dx), bounds.min, bounds.max);
}
