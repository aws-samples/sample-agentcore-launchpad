// The guided tour (ported from console-v3's Tour.tsx onto V2): one lit area at a
// time over a dimmed console, with a short card beside it. Mounted once by the
// shell, inside `.v2` (no portal: the --v2-* tokens live there), at z-index 140 —
// above modal masks, below dropdown popups, the palette and toasts.
//
// It starts by itself once per browser, on the first visit to V2 Home, and never
// again after it is finished or skipped; the help menu and the palette replay it.
// It never shows over another layer: while the palette, the glossary, a menu, a
// dropdown or a modal is open it pauses (hidden, same stop) and resumes when that
// layer closes. A pause never marks it done.
import { ArrowLeft, ArrowRight, X } from "lucide-react";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router-dom";

import { PALETTE_SHORTCUT } from "../CommandPalette";
import { getGlossary, useGlossary } from "../glossaryTerms";
import { isPaletteOpen, isTypingTarget, usePaletteOpen } from "../palette";
import "./onboarding.css";
import { isTourRequested, setTourDone, takeTourRequest, useTourDone, useTourRequest } from "./state";
import {
  type Box,
  EDGE,
  isTourHome,
  OVERLAY_SELECTOR,
  placeCard,
  seekStop,
  TOUR_STOPS,
  tourKeyAction,
  visibleStops,
} from "./stops";

/** First visit to Home: give the page a moment to load its cards. */
const AUTO_START_MS = 1200;
/** A replay: long enough for Home to mount (and load) after navigating there. */
const REPLAY_MS = 700;
/** Room drawn around the lit target. */
const PAD = 8;
const CARD_WIDTH = 360;
/** The smooth scroll and the page's own reveal move the target for a moment. */
const SETTLE_MS = 380;

/** The target element, if it is on screen (present and laid out). */
function targetOf(selector: string): HTMLElement | null {
  const el = document.querySelector<HTMLElement>(selector);
  return el && el.getClientRects().length > 0 ? el : null;
}

const onScreen = (selector: string) => targetOf(selector) !== null;

/** Another layer is open: the tour waits behind it. */
function overlayOpen(): boolean {
  return isPaletteOpen() || getGlossary().open || document.querySelector(OVERLAY_SELECTOR) !== null;
}

function reducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

function sameBox(a: Box | null, b: Box | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height;
}

/** Bumped (once per frame at most) whenever nodes are added or removed under `.v2`. */
function useDomVersion(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const root = document.querySelector(".v2") ?? document.body;
    let frame = 0;
    const observer = new MutationObserver(() => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        setVersion((n) => n + 1);
      });
    });
    observer.observe(root, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, []);
  return version;
}

/**
 * Decides when the tour runs: the first visit to Home (after a short delay) and
 * every replay request (taken on Home; the caller navigates there). Leaving Home
 * ends a running tour without marking it done, so it offers itself again on the
 * next Home visit.
 */
export function TourHost() {
  const location = useLocation();
  const done = useTourDone();
  const request = useTourRequest();
  const [running, setRunning] = useState(false);
  // a new run restarts the tour at its first stop
  const [run, setRun] = useState(0);
  const onHome = isTourHome(location.pathname);
  // the first-visit start fires once per Home visit, not again after a quiet close
  const autoArmed = useRef(true);

  const begin = useCallback(() => {
    setRun((n) => n + 1);
    setRunning(true);
  }, []);

  // first visit: while the tour has never been finished or skipped
  useEffect(() => {
    if (!onHome || done || running || !autoArmed.current) return;
    const timer = window.setTimeout(() => {
      autoArmed.current = false;
      begin();
    }, AUTO_START_MS);
    return () => window.clearTimeout(timer);
  }, [onHome, done, running, begin]);

  // a replay: start over once Home has had a moment to mount its cards
  useEffect(() => {
    if (!onHome || !isTourRequested()) return;
    setRunning(false);
    const timer = window.setTimeout(() => {
      if (takeTourRequest()) begin();
    }, REPLAY_MS);
    return () => window.clearTimeout(timer);
  }, [onHome, request, begin]);

  // the tour lights up Home's cards: elsewhere it has nothing to say
  useEffect(() => {
    if (onHome) return;
    setRunning(false);
    autoArmed.current = true;
    takeTourRequest();
  }, [onHome]);

  const close = useCallback(() => setRunning(false), []);
  return running ? <Tour key={run} onClose={close} /> : null;
}

function Tour({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const titleId = useId();
  const bodyId = useId();
  const paletteOpen = usePaletteOpen();
  const glossaryOpen = useGlossary().open;
  const dom = useDomVersion();
  const [paused, setPaused] = useState(overlayOpen);
  const [at, setAt] = useState(() => seekStop(TOUR_STOPS, 0, 1, onScreen));
  const [rect, setRect] = useState<Box | null>(null);
  const [cardHeight, setCardHeight] = useState(200);
  const [progress, setProgress] = useState({ n: 1, total: 1, first: true, last: false });
  const cardRef = useRef<HTMLDivElement>(null);
  const pausedRef = useRef(paused);
  const returnTo = useRef<HTMLElement | null>(null);
  const recorded = useRef(false);

  // pause behind any other open layer (re-checked whenever one could have appeared)
  useLayoutEffect(() => {
    setPaused(overlayOpen());
  }, [paletteOpen, glossaryOpen, dom]);
  useLayoutEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  const finish = useCallback(() => {
    setTourDone();
    onClose();
  }, [onClose]);

  // nothing to show at all (no stop on screen): leave quietly, not done
  useEffect(() => {
    if (at < 0) onClose();
  }, [at, onClose]);

  // give focus back to where it was before the tour first showed
  useEffect(
    () => () => {
      if (returnTo.current?.isConnected) returnTo.current.focus();
    },
    [],
  );

  // Reaching a stop (or resuming at it): bring it into view if it is not, and
  // focus the card so the keys and a screen reader start from it.
  useLayoutEffect(() => {
    if (paused || at < 0) return;
    const el = targetOf(TOUR_STOPS[at].selector);
    if (!recorded.current) {
      recorded.current = true;
      returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    }
    if (el) {
      const r = el.getBoundingClientRect();
      const topBar = document.querySelector(".v2-top")?.getBoundingClientRect().bottom ?? 0;
      const covered = el.closest(".v2-top") === null && r.top < topBar;
      if ((covered || r.bottom > window.innerHeight) && r.height < window.innerHeight) {
        el.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
      }
    }
    cardRef.current?.focus({ preventScroll: true });
  }, [at, paused]);

  // Measure the lit area and where this stop sits among those on screen now;
  // again on resize, on any scroll, once the scroll settled, and when the page
  // changed. A target that disappeared moves the tour to the nearest one left.
  useLayoutEffect(() => {
    if (paused || at < 0) return;
    const measure = () => {
      const stop = TOUR_STOPS[at];
      const el = targetOf(stop.selector);
      if (!el) {
        const forward = seekStop(TOUR_STOPS, at + 1, 1, onScreen);
        const moved = forward >= 0 ? forward : seekStop(TOUR_STOPS, at - 1, -1, onScreen);
        if (moved >= 0) setAt(moved);
        else onClose();
        return;
      }
      const r = el.getBoundingClientRect();
      const box = { left: r.left, top: r.top, width: r.width, height: r.height };
      setRect((prev) => (sameBox(prev, box) ? prev : box));
      const shown = visibleStops(TOUR_STOPS, onScreen);
      const next = {
        n: Math.max(1, shown.findIndex((s) => s.key === stop.key) + 1),
        total: Math.max(1, shown.length),
        first: seekStop(TOUR_STOPS, at - 1, -1, onScreen) < 0,
        last: seekStop(TOUR_STOPS, at + 1, 1, onScreen) < 0,
      };
      setProgress((prev) =>
        prev.n === next.n && prev.total === next.total && prev.first === next.first && prev.last === next.last ? prev : next,
      );
      const height = cardRef.current?.offsetHeight;
      if (height) setCardHeight((prev) => (prev === height ? prev : height));
    };
    measure();
    const settle = window.setTimeout(measure, SETTLE_MS);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.clearTimeout(settle);
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [at, paused, dom, onClose]);

  // each move re-checks the stops: a target that mounted late is included
  const next = useCallback(() => {
    const i = seekStop(TOUR_STOPS, at + 1, 1, onScreen);
    if (i >= 0) setAt(i);
  }, [at]);
  const back = useCallback(() => {
    const i = seekStop(TOUR_STOPS, at - 1, -1, onScreen);
    if (i >= 0) setAt(i);
  }, [at]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      const target = e.target instanceof HTMLElement ? e.target : null;
      const action = tourKeyAction(e, {
        // read from the last commit: a layer closed by this very key still owns it
        paused: pausedRef.current,
        inPalette: target?.closest(".v2-cmdk") != null,
        typing: isTypingTarget(target),
        onButton: target?.tagName === "BUTTON",
      });
      if (!action) return;
      e.preventDefault();
      if (action === "end") finish();
      else if (action === "back") back();
      else if (seekStop(TOUR_STOPS, at + 1, 1, onScreen) >= 0) next();
      // Enter on the last stop is "Got it"; → there stays put
      else if (e.key === "Enter") finish();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [at, next, back, finish]);

  // Tab cycles inside the card: the dimmed page is not reachable meanwhile
  const onCardKey = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Tab") return;
    const buttons = Array.from(cardRef.current?.querySelectorAll<HTMLElement>("button") ?? []);
    if (buttons.length === 0) return;
    const i = buttons.indexOf(document.activeElement as HTMLElement);
    const to = e.shiftKey ? (i <= 0 ? buttons.length - 1 : i - 1) : i < 0 || i === buttons.length - 1 ? 0 : i + 1;
    e.preventDefault();
    buttons[to].focus();
  };

  if (paused || at < 0) return null;
  const stop = TOUR_STOPS[at];
  const vw = window.innerWidth;
  const width = Math.min(CARD_WIDTH, vw - EDGE * 2);
  const pos = placeCard(rect, { width, height: cardHeight }, { width: vw, height: window.innerHeight });

  return (
    <div className="v2-tour" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={bodyId} data-testid="v2-tour">
      {rect ? (
        <div
          className="v2-tour-spot"
          aria-hidden="true"
          style={{ left: rect.left - PAD, top: rect.top - PAD, width: rect.width + PAD * 2, height: rect.height + PAD * 2 }}
        />
      ) : (
        <div className="v2-tour-dim" aria-hidden="true" />
      )}
      <div
        key={stop.key}
        ref={cardRef}
        className="v2-tour-card"
        style={{ left: pos.left, top: pos.top, width }}
        tabIndex={-1}
        onKeyDown={onCardKey}
        data-testid={`v2-tour-stop-${stop.key}`}
      >
        <div className="v2-tour-head">
          <span className="v2-tour-count">
            {t("v2.onboard.tour.label")} · {t("v2.onboard.tour.counter", { n: progress.n, total: progress.total })}
          </span>
          <button
            type="button"
            className="v2-tour-x"
            onClick={finish}
            aria-label={t("v2.onboard.tour.end")}
            title={t("v2.onboard.tour.end")}
            data-testid="v2-tour-end"
          >
            <X size={15} aria-hidden="true" />
          </button>
        </div>
        <h3 id={titleId}>{t(stop.titleKey)}</h3>
        <p id={bodyId}>{t(stop.bodyKey, { shortcut: PALETTE_SHORTCUT })}</p>
        <div className="v2-tour-foot">
          <button type="button" className="v2-btn sm" onClick={finish} data-testid="v2-tour-skip">
            {t("v2.onboard.tour.skip")}
          </button>
          <span className="v2-tour-keys" aria-hidden="true">
            <kbd>←</kbd>
            <kbd>→</kbd>
          </span>
          {!progress.first && (
            <button type="button" className="v2-btn sm" onClick={back} data-testid="v2-tour-back">
              <ArrowLeft size={13} aria-hidden="true" />
              {t("v2.onboard.tour.back")}
            </button>
          )}
          {progress.last ? (
            <button type="button" className="v2-btn sm primary" onClick={finish} data-testid="v2-tour-done">
              {t("v2.onboard.tour.done")}
            </button>
          ) : (
            <button type="button" className="v2-btn sm primary" onClick={next} data-testid="v2-tour-next">
              {t("v2.onboard.tour.next")}
              <ArrowRight size={13} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
