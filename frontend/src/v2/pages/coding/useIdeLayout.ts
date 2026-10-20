import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  closePanel,
  computeLayout,
  type Layout,
  type LayoutMove,
  openPanel,
  overlayWidth,
  type Panel,
  shownOverlay,
  withWidth,
} from "./layout";
import { loadLayoutPrefs, saveLayoutPrefs } from "./layoutPrefs";

export interface IdeLayout {
  /** attach to the grid: its width drives the layout (re-clamped on every resize) */
  gridRef: (el: HTMLDivElement | null) => void;
  layout: Layout;
  /** the side panel showing as an overlay over the conversation (no room inline) */
  overlay: Panel | null;
  /** an overlay's width for `panel` at the current grid */
  overlayWidthOf: (panel: Panel) => number;
  /** show a panel: inline when it fits, else as the overlay (stable) */
  open: (panel: Panel) => void;
  /** hide a panel: close its overlay, else collapse it to its rail (stable) */
  close: (panel: Panel) => void;
  /** set a panel's width (null = its default) (stable) */
  setWidth: (panel: Panel, width: number | null) => void;
  closeOverlay: () => void;
}

/**
 * The IDE's side-panel layout: preferences from this browser's storage (saved on every
 * change), the grid's measured width (ResizeObserver — measured before the first paint),
 * and the transient overlay. The pure rules live in `layout.ts`.
 */
export function useIdeLayout(): IdeLayout {
  const [grid, setGrid] = useState<HTMLDivElement | null>(null);
  const [container, setContainer] = useState(0);
  const [state, setState] = useState<LayoutMove>(() => ({ prefs: loadLayoutPrefs(), overlay: null }));
  const containerRef = useRef(container);
  containerRef.current = container;

  useLayoutEffect(() => {
    if (!grid) return;
    const measure = (width: number) => setContainer(Math.max(0, Math.floor(width)));
    measure(grid.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const last = entries[entries.length - 1];
      if (last) measure(last.contentRect.width);
    });
    observer.observe(grid);
    return () => observer.disconnect();
  }, [grid]);

  const { prefs } = state;
  useEffect(() => saveLayoutPrefs(prefs), [prefs]);

  const layout = useMemo(() => computeLayout(container, prefs), [container, prefs]);
  const overlay = shownOverlay(layout, state.overlay);
  // an overlay whose panel is inline again (the grid grew) is dropped for good, so it
  // does not come back when the grid shrinks later
  if (state.overlay !== null && overlay === null) setState((s) => ({ ...s, overlay: null }));

  const open = useCallback(
    (panel: Panel) =>
      setState((s) => {
        const next = openPanel(containerRef.current, s.prefs, panel, s.overlay);
        return next.prefs === s.prefs && next.overlay === s.overlay ? s : next;
      }),
    [],
  );
  const close = useCallback(
    (panel: Panel) =>
      setState((s) => {
        const next = closePanel(s.prefs, panel, s.overlay);
        return next.prefs === s.prefs && next.overlay === s.overlay ? s : next;
      }),
    [],
  );
  const setWidth = useCallback(
    (panel: Panel, width: number | null) =>
      setState((s) => {
        const next = withWidth(s.prefs, panel, width);
        return next === s.prefs ? s : { ...s, prefs: next };
      }),
    [],
  );
  const closeOverlay = useCallback(() => setState((s) => (s.overlay === null ? s : { ...s, overlay: null })), []);
  const overlayWidthOf = useCallback((panel: Panel) => overlayWidth(container, prefs, panel), [container, prefs]);

  return { gridRef: setGrid, layout, overlay, overlayWidthOf, open, close, setWidth, closeOverlay };
}
