// ⌘K — one keystroke to any page, agent, glossary term or help action. Ported from
// console-v3 (v3/CommandPalette.tsx) onto V2 routes and the V2 theme. Entries,
// ranking and the open/close store live in ./palette (pure, unit-tested); this
// file is the overlay. Rendered in place inside `.v2` (no portal: the --v2-*
// tokens live there), above modal masks and below toasts.
import { BookOpen, Bot, MessagesSquare, PlayCircle, Search } from "lucide-react";
import {
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { api } from "../lib/api";
import { useWorkspace } from "../workspace/workspace-context";
import { GLOSSARY_TERMS, openGlossary } from "./glossaryTerms";
import { useLoad } from "./hooks";
import {
  buildEntries,
  closePalette,
  groupRanked,
  type PaletteEntry,
  palettePages,
  rank,
  shortcutLabel,
  usePaletteOpen,
} from "./palette";

/** ⌘K on Apple devices, Ctrl K elsewhere. */
export const PALETTE_SHORTCUT = shortcutLabel(
  typeof navigator === "undefined"
    ? ""
    : ((navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ??
        navigator.platform ??
        navigator.userAgent),
);

/** Mounted once by the shell; opened through `openPalette` / `togglePalette`. */
export function CommandPalette() {
  const open = usePaletteOpen();
  // Mount the body only while open: its agents load then, and its unmount hands
  // focus back before anything opened from it (the glossary) takes it.
  return open ? <PaletteBody /> : null;
}

function PaletteBody() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const tEn = useMemo(() => i18n.getFixedT("en"), [i18n]);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const optionId = (index: number) => `${listId}-o${index}`;

  // fetched each time the palette opens, never on page loads
  const agents = useLoad(() => api.listAgents(), `v2-palette-agents:${current?.id ?? ""}`);

  // Remember what had focus, focus the input in the same commit that shows it
  // (a deferred focus drops the first keys typed straight after ⌘K), and give
  // focus back when the palette closes.
  useLayoutEffect(() => {
    const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    inputRef.current?.focus();
    return () => {
      if (returnTo?.isConnected) returnTo.focus();
    };
  }, []);

  const pages = useMemo(() => palettePages(isAdmin), [isAdmin]);
  const icons = useMemo(() => new Map(pages.map((item) => [`page:${item.to}`, item.icon])), [pages]);

  const entries = useMemo(
    () =>
      buildEntries({
        pages: pages.map((item) => ({ to: item.to, label: t(item.labelKey), labelEn: tEn(item.labelKey) })),
        agents: agents.data?.agents ?? [],
        terms: GLOSSARY_TERMS.map((key) => ({
          key,
          label: t(`glossary.name.${key}`),
          labelEn: tEn(`glossary.name.${key}`),
        })),
        t,
        tEn,
      }),
    [pages, agents.data, t, tEn],
  );

  const groups = useMemo(() => groupRanked(rank(entries, query)), [entries, query]);
  const results = useMemo(() => groups.flatMap((g) => g.entries), [groups]);
  const active = results.length === 0 ? -1 : Math.min(cursor, results.length - 1);

  useEffect(() => setCursor(0), [query]);
  useEffect(() => {
    if (active >= 0) listRef.current?.querySelector(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const run = (entry: PaletteEntry | undefined) => {
    if (!entry) return;
    closePalette();
    if ("to" in entry.run) navigate(entry.run.to);
    else openGlossary(entry.run.glossary ?? undefined);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const n = results.length;
    switch (e.key) {
      case "Escape":
        // keep a Modal / Drawer underneath (window keydown listener) open
        e.preventDefault();
        e.stopPropagation();
        closePalette();
        break;
      case "ArrowDown":
        e.preventDefault();
        if (n) setCursor((active + 1) % n);
        break;
      case "ArrowUp":
        e.preventDefault();
        if (n) setCursor((active - 1 + n) % n);
        break;
      case "Enter":
        if (e.nativeEvent.isComposing) return; // an IME is committing a word
        e.preventDefault();
        run(results[active]);
        break;
      case "Tab":
        // the input is the dialog's only stop: keep focus inside the modal
        e.preventDefault();
        break;
    }
  };

  const iconOf = (entry: PaletteEntry): ReactNode => {
    const PageIcon = icons.get(entry.id);
    if (PageIcon) return <PageIcon size={15} aria-hidden="true" />;
    if (entry.id.startsWith("chat:")) return <MessagesSquare size={15} aria-hidden="true" />;
    if (entry.id === "help:videos") return <PlayCircle size={15} aria-hidden="true" />;
    if (entry.group === "glossary" || entry.group === "help") return <BookOpen size={15} aria-hidden="true" />;
    return <Bot size={15} aria-hidden="true" />;
  };

  let index = -1;
  return (
    <div className="v2-cmdk-mask" onMouseDown={(e) => e.target === e.currentTarget && closePalette()}>
      <div
        className="v2-cmdk"
        role="dialog"
        aria-modal="true"
        aria-label={t("v2.palette.label")}
        onKeyDown={onKeyDown}
        data-testid="v2-cmdk"
      >
        <div className="v2-cmdk-find">
          <Search size={16} aria-hidden="true" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("v2.palette.placeholder")}
            aria-label={t("v2.palette.placeholder")}
            role="combobox"
            aria-expanded="true"
            aria-autocomplete="list"
            aria-controls={listId}
            aria-activedescendant={active >= 0 ? optionId(active) : undefined}
            autoComplete="off"
            spellCheck={false}
            data-testid="v2-cmdk-input"
          />
        </div>
        <div className="v2-cmdk-list" id={listId} role="listbox" aria-label={t("v2.palette.label")} ref={listRef}>
          {groups.map((group) => (
            <div key={group.group} role="group" aria-labelledby={`${listId}-g-${group.group}`}>
              <div className="v2-cmdk-group" id={`${listId}-g-${group.group}`} role="presentation">
                {t(`v2.palette.group.${group.group}`)}
              </div>
              {group.entries.map((entry) => {
                index += 1;
                const i = index;
                return (
                  <div
                    key={entry.id}
                    id={optionId(i)}
                    data-idx={i}
                    className={i === active ? "v2-cmdk-item active" : "v2-cmdk-item"}
                    role="option"
                    aria-selected={i === active}
                    // keep focus (and the caret) in the input
                    onMouseDown={(e) => e.preventDefault()}
                    onMouseMove={() => i !== active && setCursor(i)}
                    onClick={() => run(entry)}
                    data-testid={`v2-cmdk-opt-${entry.id}`}
                  >
                    {iconOf(entry)}
                    <span className="txt">{entry.label}</span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
        {results.length === 0 && (
          <div className="v2-cmdk-empty" role="status">
            {t("v2.palette.none")}
          </div>
        )}
        <div className="v2-cmdk-foot">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd> {t("v2.palette.move")}
          </span>
          <span>
            <kbd>↵</kbd> {t("v2.palette.run")}
          </span>
          <span>
            <kbd>Esc</kbd> {t("v2.palette.close")}
          </span>
          {agents.loading && <span className="v2-cmdk-loading">{t("v2.palette.loadingAgents")}</span>}
        </div>
      </div>
    </div>
  );
}
