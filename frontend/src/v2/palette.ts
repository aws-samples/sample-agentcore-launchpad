// The ⌘K command palette: which entries it offers, how a query ranks them, which
// keys open it, and whether it is open. Pure + a tiny store (like ./glossaryTerms),
// so vitest covers it without a DOM. Ported from console-v3 (v3/score.ts and the
// palette wiring of v3/Shell.tsx), on V2's routes.
import { useSyncExternalStore } from "react";

import { V2_NAV, type V2NavItem } from "./nav";

export type PaletteGroup = "page" | "agent" | "glossary" | "help";

/** The order groups are listed in when the query is empty. */
export const PALETTE_GROUPS: PaletteGroup[] = ["page", "help", "agent", "glossary"];

/** At most this many results are shown. */
export const PALETTE_CAP = 40;

export interface PaletteEntry {
  id: string;
  group: PaletteGroup;
  label: string;
  /** extra words it answers to: the English label under another UI language,
   *  the agent's name, the route */
  keywords: string[];
  /** navigate to a route, or open the glossary (at a term, or the whole list) */
  run: { to: string } | { glossary: string | null };
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

/**
 * Subsequence match with a preference for word starts — "shcs" finds "shop-cs".
 * Verbatim from console-v3 `v3/score.ts`, argument order included (query first).
 */
export function score(query: string, text: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 1;
  const t = text.toLowerCase();
  const at = t.indexOf(q);
  if (at >= 0) {
    // a match that starts a word ("cs" in "shop-cs") is what was meant, over one
    // buried inside a word ("cs" in "discs")
    const wordStart = at === 0 || /[\s\-_./·]/.test(t[at - 1]);
    return 100 - at + (wordStart ? 50 : 0);
  }
  let ti = 0;
  let hits = 0;
  for (const ch of q) {
    const found = t.indexOf(ch, ti);
    if (found < 0) return 0;
    if (found === 0 || /[\s\-_./·]/.test(t[found - 1])) hits += 2;
    hits += 1;
    ti = found + 1;
  }
  return hits;
}

/**
 * Every V2 page the viewer's role allows, in sidebar order — whatever the nav
 * mode: the palette is how a page hidden by the essentials sidebar is reached.
 */
export function palettePages(isAdmin: boolean): V2NavItem[] {
  return V2_NAV.flatMap((group) => group.items.filter((item) => !item.admin || isAdmin));
}

export interface PaletteInput {
  pages: { to: string; label: string; labelEn: string }[];
  agents: { id: string; name: string }[];
  terms: { key: string; label: string; labelEn: string }[];
  /** the UI language */
  t: Translate;
  /** English, for keywords; defaults to `t` */
  tEn?: Translate;
}

/** The palette's entries, in their empty-query order: pages, help, agents, glossary. */
export function buildEntries({ pages, agents, terms, t, tEn = t }: PaletteInput): PaletteEntry[] {
  const pageEntries: PaletteEntry[] = pages.map((page) => ({
    id: `page:${page.to}`,
    group: "page",
    label: page.label,
    // "chat" finds 对话 under zh-CN; the route words find a page by its URL
    keywords: [page.labelEn, page.to.replace(/[/?=&]/g, " ").trim()],
    run: { to: page.to },
  }));
  const help: PaletteEntry[] = [
    {
      id: "help:glossary",
      group: "help",
      label: t("v2.palette.openGlossary"),
      keywords: [tEn("v2.palette.openGlossary"), "glossary terms help"],
      run: { glossary: null },
    },
    {
      id: "help:videos",
      group: "help",
      label: t("v2.palette.openVideos"),
      keywords: [tEn("v2.palette.openVideos"), "videos demo help"],
      run: { to: "/v2/videos" },
    },
  ];
  const agentEntries: PaletteEntry[] = agents.flatMap((agent) => {
    const name = agent.name;
    const id = encodeURIComponent(agent.id);
    return [
      {
        id: `agent:${agent.id}`,
        group: "agent" as const,
        label: t("v2.palette.openAgent", { name }),
        keywords: [name, tEn("v2.palette.openAgent", { name })],
        run: { to: `/v2/agents?view=detail&id=${id}` },
      },
      {
        id: `chat:${agent.id}`,
        group: "agent" as const,
        label: t("v2.palette.chatWith", { name }),
        keywords: [name, tEn("v2.palette.chatWith", { name })],
        run: { to: `/v2/chat?agent=${id}` },
      },
    ];
  });
  const termEntries: PaletteEntry[] = terms.map((term) => ({
    id: `term:${term.key}`,
    group: "glossary",
    label: t("glossary.whatIs", { term: term.label }),
    keywords: [term.label, term.labelEn, term.key],
    run: { glossary: term.key },
  }));
  return [...pageEntries, ...help, ...agentEntries, ...termEntries];
}

function entryScore(entry: PaletteEntry, query: string): number {
  return Math.max(score(query, entry.label), ...entry.keywords.map((k) => score(query, k) * 0.7));
}

/**
 * The entries that answer to `query`, best first (ties keep the given order),
 * at most `cap`. An empty query lists the pages and help actions in order; agents
 * and glossary terms appear once something is typed (a workspace's agents would
 * otherwise fill the list).
 */
export function rank(entries: PaletteEntry[], query: string, cap = PALETTE_CAP): PaletteEntry[] {
  const typed = query.trim() !== "";
  const pool = typed ? entries : entries.filter((e) => e.group === "page" || e.group === "help");
  const scored = pool.map((entry) => ({ entry, s: entryScore(entry, query) })).filter((r) => r.s > 0);
  if (typed) scored.sort((a, b) => b.s - a.s); // Array#sort is stable
  return scored.slice(0, cap).map((r) => r.entry);
}

/**
 * Ranked entries under one header per group: groups ordered by their best entry,
 * entries keeping their rank inside a group. The flattened result is the visual
 * (and arrow-key) order.
 */
export function groupRanked(entries: PaletteEntry[]): { group: PaletteGroup; entries: PaletteEntry[] }[] {
  const out: { group: PaletteGroup; entries: PaletteEntry[] }[] = [];
  for (const entry of entries) {
    const bucket = out.find((g) => g.group === entry.group);
    if (bucket) bucket.entries.push(entry);
    else out.push({ group: entry.group, entries: [entry] });
  }
  return out;
}

/* ── keys ── */

/** Whether keys typed into `el` are text (an input, textarea, select, or editable). */
export function isTypingTarget(el: { tagName?: string; isContentEditable?: boolean } | null | undefined): boolean {
  if (!el) return false;
  return /^(input|textarea|select)$/i.test(el.tagName ?? "") || el.isContentEditable === true;
}

/** ⌘K / Ctrl+K: toggles the palette anywhere, even while typing. */
export function isPaletteToggle(e: { key: string; metaKey: boolean; ctrlKey: boolean; altKey: boolean }): boolean {
  return (e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k";
}

/**
 * `/` opens the palette — without a modifier, outside text fields, and only when
 * no dialog or dropdown is open (it must not steal `/` from a search box or a
 * listbox's typeahead).
 */
export function isSlashOpen(
  e: { key: string; metaKey: boolean; ctrlKey: boolean; altKey: boolean; isComposing?: boolean },
  active: { tagName?: string; isContentEditable?: boolean } | null | undefined,
  overlayOpen: boolean,
): boolean {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return false;
  return !isTypingTarget(active) && !overlayOpen;
}

/** The shortcut as the platform writes it: ⌘K on Apple devices, Ctrl K elsewhere. */
export function shortcutLabel(platform: string): string {
  return /mac|iphone|ipad|ipod/i.test(platform) ? "⌘K" : "Ctrl K";
}

/* ── whether the palette is open (one, shell-mounted, opened from anywhere) ── */

let open = false;
const listeners = new Set<() => void>();

function emit(next: boolean) {
  if (next === open) return;
  open = next;
  listeners.forEach((notify) => notify());
}

export function openPalette(): void {
  emit(true);
}

export function closePalette(): void {
  emit(false);
}

export function togglePalette(): void {
  emit(!open);
}

export function isPaletteOpen(): boolean {
  return open;
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => listeners.delete(notify);
}

export function usePaletteOpen(): boolean {
  return useSyncExternalStore(subscribe, isPaletteOpen, isPaletteOpen);
}
