// The guided tour's stops and the pure parts of walking them: which stops are on
// screen, where the next one is, and where the card goes. No DOM in here — the
// "is it on screen" check is injected — so vitest covers it in node. Ported from
// console-v3's Tour.tsx onto V2's Home and shell.

export type TourStopKey = "cmdk" | "launch" | "stats" | "agents" | "health" | "modules" | "help";

export interface TourStop {
  key: TourStopKey;
  /** the element lit up; a stop whose target is not on screen is skipped */
  selector: string;
  titleKey: string;
  bodyKey: string;
}

const stop = (key: TourStopKey): TourStop => ({
  key,
  selector: `[data-tour="${key}"]`,
  titleKey: `v2.onboard.tour.${key}Title`,
  bodyKey: `v2.onboard.tour.${key}Body`,
});

/** In walking order: the top bar's palette, Home top to bottom, then the shell around it. */
export const TOUR_STOPS: TourStop[] = [
  stop("cmdk"), // the ⌘K button in the top bar
  stop("launch"), // the launch checklist (absent while hidden for the workspace)
  stop("stats"), // Home's KPI row
  stop("agents"), // recent deployments
  stop("health"), // service status
  stop("modules"), // the sidebar, with the nav-mode switch at its foot
  stop("help"), // the "?" button
];

export type Exists = (selector: string) => boolean;

/** The stops whose target is on screen now, in order. */
export function visibleStops(stops: TourStop[], exists: Exists): TourStop[] {
  return stops.filter((s) => exists(s.selector));
}

/**
 * The index of the first stop at or after `from` (dir 1) / at or before it (dir
 * -1) whose target is on screen now; -1 when there is none. Checked when a stop
 * is reached, not once up front: the checklist mounts after Home's data arrives,
 * which can be after the tour opened.
 */
export function seekStop(stops: TourStop[], from: number, dir: 1 | -1, exists: Exists): number {
  for (let i = from; i >= 0 && i < stops.length; i += dir) {
    if (exists(stops[i].selector)) return i;
  }
  return -1;
}

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Room kept between the card and the viewport edge / the lit area. */
export const EDGE = 16;
export const GAP = 14;

/**
 * Where the card goes for a lit `target` (null: centred over a plain dim): to the
 * right of a tall, narrow target (the sidebar), else below it when the card fits
 * there, else above it, else pinned to the bottom edge. Always inside the viewport.
 */
export function placeCard(
  target: Box | null,
  card: { width: number; height: number },
  viewport: { width: number; height: number },
): { left: number; top: number } {
  const { width: vw, height: vh } = viewport;
  const { width: w, height: h } = card;
  const clampLeft = (x: number) => Math.max(EDGE, Math.min(x, vw - w - EDGE));
  const clampTop = (y: number) => Math.max(EDGE, Math.min(y, vh - h - EDGE));
  if (!target) return { left: clampLeft(vw / 2 - w / 2), top: clampTop(vh / 2 - h / 2) };
  const bottom = target.top + target.height;
  const right = target.left + target.width;
  const tall = target.height > vh * 0.5 && target.width < 320;
  if (tall && vw - right - GAP >= w + EDGE) return { left: right + GAP, top: clampTop(target.top + 40) };
  if (vh - bottom - GAP >= h + EDGE) return { left: clampLeft(target.left), top: bottom + GAP };
  if (target.top - GAP >= h + EDGE) return { left: clampLeft(target.left), top: target.top - GAP - h };
  return { left: clampLeft(target.left), top: clampTop(vh - h - EDGE) };
}

/* ── when it runs, and which keys it takes ─────────────────────────────── */

/** V2 Home: the tour starts here by itself, and a replay comes here first. */
export const TOUR_HOME = "/v2";

export function isTourHome(pathname: string): boolean {
  return pathname === TOUR_HOME || pathname === `${TOUR_HOME}/`;
}

/**
 * Open layers the tour waits behind (the palette and the glossary are read from
 * their stores as well): a modal or drawer, a menu, a dropdown popup. The tour's
 * own root is a dialog too, so it is excluded.
 */
export const OVERLAY_SELECTOR = '.v2 [role="dialog"]:not(.v2-tour), .v2 [role="menu"], .v2 .v2-pop';

export type TourKeyAction = "next" | "back" | "end" | null;

/**
 * What a keydown does to a running tour: → / Enter next, ← back, Esc end. Nothing
 * while it is paused behind an overlay, while the palette has focus, while
 * typing, during IME composition, or with a modifier held (⌘K stays the shell's).
 * Enter on one of the card's buttons is left to that button.
 */
export function tourKeyAction(
  e: { key: string; metaKey: boolean; ctrlKey: boolean; altKey: boolean; isComposing?: boolean },
  where: { paused: boolean; inPalette: boolean; typing: boolean; onButton: boolean },
): TourKeyAction {
  if (where.paused || where.inPalette || where.typing) return null;
  if (e.isComposing || e.metaKey || e.ctrlKey || e.altKey) return null;
  switch (e.key) {
    case "Escape":
      return "end";
    case "ArrowRight":
      return "next";
    case "Enter":
      return where.onButton ? null : "next";
    case "ArrowLeft":
      return "back";
    default:
      return null;
  }
}
