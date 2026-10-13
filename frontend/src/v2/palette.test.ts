import { afterEach, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { GLOSSARY_TERMS } from "./glossaryTerms";
import { navGroupsFor } from "./nav";
import {
  buildEntries,
  closePalette,
  groupRanked,
  isPaletteOpen,
  isPaletteToggle,
  isSlashOpen,
  isTypingTarget,
  openPalette,
  PALETTE_CAP,
  PALETTE_GROUPS,
  type PaletteEntry,
  palettePages,
  rank,
  score,
  shortcutLabel,
  togglePalette,
} from "./palette";

type Locale = typeof en;

/** A tiny i18next stand-in over the real locale JSON: dotted keys + {{name}} / {{term}}. */
function translator(locale: Locale) {
  return (key: string, options?: Record<string, unknown>) => {
    const node = key
      .split(".")
      .reduce<unknown>((at, part) => (at as Record<string, unknown> | undefined)?.[part], locale);
    const text = typeof node === "string" ? node : key;
    return text.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(options?.[name] ?? ""));
  };
}
const tEn = translator(en);
const tZh = translator(zh as Locale);

function entries(opts: { isAdmin?: boolean; zhUi?: boolean; agents?: { id: string; name: string }[] } = {}) {
  const t = opts.zhUi ? tZh : tEn;
  return buildEntries({
    pages: palettePages(opts.isAdmin ?? true).map((item) => ({
      to: item.to,
      label: t(item.labelKey),
      labelEn: tEn(item.labelKey),
    })),
    agents: opts.agents ?? [],
    terms: GLOSSARY_TERMS.map((key) => ({ key, label: t(`glossary.name.${key}`), labelEn: tEn(`glossary.name.${key}`) })),
    t,
    tEn,
  });
}
const routeOf = (entry: PaletteEntry | undefined) => (entry && "to" in entry.run ? entry.run.to : undefined);

describe("command palette scoring (ported from console-v3)", () => {
  it("finds a subsequence", () => {
    expect(score("shcs", "shop-cs")).toBeGreaterThan(0);
  });
  it("prefers a match at a word start", () => {
    expect(score("cs", "shop-cs")).toBeGreaterThan(score("cs", "discs"));
  });
  it("misses when a letter is absent", () => {
    expect(score("xyz", "shop-cs")).toBe(0);
  });
  it("ranks a substring above a scattered subsequence", () => {
    expect(score("shop", "shop-cs")).toBeGreaterThan(score("shcs", "shop-cs"));
  });
  it("matches everything on an empty query", () => {
    expect(score("", "anything")).toBe(1);
  });
});

describe("rank", () => {
  it("finds a page by its English name under the zh-CN UI", () => {
    const [top] = rank(entries({ zhUi: true }), "chat");
    expect(routeOf(top)).toBe("/v2/chat");
    expect(top.label).toBe(zh.nav.chat);
  });
  it("finds a page by its zh name", () => {
    const [top] = rank(entries({ zhUi: true }), zh.nav.observability);
    expect(routeOf(top)).toBe("/v2/observability");
  });
  it("finds a page by its English name under the English UI", () => {
    expect(routeOf(rank(entries(), "knowledge")[0])).toBe("/v2/knowledge-bases");
    expect(routeOf(rank(entries(), "obs")[0])).toBe("/v2/observability");
  });
  it("keeps the given order for an empty query: pages, then help, agents, glossary", () => {
    const ranked = rank(entries({ agents: [{ id: "a1", name: "shop-cs" }] }), "  ", 1000);
    const seen = ranked.map((e) => e.group).filter((g, i, all) => all.indexOf(g) === i);
    expect(seen).toEqual(PALETTE_GROUPS);
    expect(ranked.filter((e) => e.group === "page").map(routeOf)).toEqual(palettePages(true).map((p) => p.to));
  });
  it("caps the results at 40 by default", () => {
    const many = Array.from({ length: 30 }, (_, i) => ({ id: `a${i}`, name: `agent-${i}` }));
    expect(rank(entries({ agents: many }), "").length).toBe(PALETTE_CAP);
    expect(PALETTE_CAP).toBe(40);
    expect(rank(entries({ agents: many }), "agent").length).toBe(40);
    expect(rank(entries({ agents: many }), "", 5).length).toBe(5);
  });
  it("drops entries that do not match", () => {
    expect(rank(entries(), "zzqqxx")).toEqual([]);
  });
  it("is stable on ties", () => {
    const a: PaletteEntry = { id: "a", group: "page", label: "alpha", keywords: [], run: { to: "/a" } };
    const b: PaletteEntry = { id: "b", group: "page", label: "alpha", keywords: [], run: { to: "/b" } };
    expect(rank([a, b], "alpha").map((e) => e.id)).toEqual(["a", "b"]);
  });
});

describe("buildEntries", () => {
  it("offers admin pages to administrators only", () => {
    const routes = (isAdmin: boolean) => entries({ isAdmin }).filter((e) => e.group === "page").map(routeOf);
    expect(routes(true)).toContain("/v2/users");
    expect(routes(false)).not.toContain("/v2/users");
    expect(routes(false)).not.toContain("/v2/workspaces");
    expect(routes(false)).toContain("/v2/eval/experiments");
  });
  it("offers every allowed page whatever the nav mode", () => {
    for (const isAdmin of [true, false]) {
      const expert = navGroupsFor("expert", isAdmin).flatMap((g) => g.items.map((i) => i.to));
      const business = navGroupsFor("business", isAdmin).flatMap((g) => g.items.map((i) => i.to));
      expect(palettePages(isAdmin).map((p) => p.to)).toEqual(expert);
      // pages the essentials sidebar hides stay in the palette
      expect(business.length).toBeLessThan(expert.length);
      expect(palettePages(isAdmin).map((p) => p.to)).toEqual(expect.arrayContaining(business));
    }
  });
  it("gives each agent an open and a chat action", () => {
    const list = entries({ agents: [{ id: "ag-1", name: "shop-cs" }] });
    const open = list.find((e) => e.id === "agent:ag-1");
    const chat = list.find((e) => e.id === "chat:ag-1");
    expect(open?.label).toBe("Open shop-cs");
    expect(routeOf(open)).toBe("/v2/agents?view=detail&id=ag-1");
    expect(chat?.label).toBe("Chat with shop-cs");
    expect(routeOf(chat)).toBe("/v2/chat?agent=ag-1");
    expect(rank(list, "shcs").map((e) => e.id).slice(0, 2)).toEqual(["agent:ag-1", "chat:ag-1"]);
  });
  it("localizes the agent actions and still answers to English", () => {
    const list = entries({ zhUi: true, agents: [{ id: "ag-1", name: "shop-cs" }] });
    expect(list.find((e) => e.id === "chat:ag-1")?.label).toBe("与 shop-cs 对话");
    expect(rank(list, "chat with shop")[0]?.id).toBe("chat:ag-1");
  });
  it("adds one glossary entry per term, opening the glossary at it", () => {
    const terms = entries().filter((e) => e.group === "glossary");
    expect(terms.map((e) => e.run)).toEqual(GLOSSARY_TERMS.map((key) => ({ glossary: key })));
    expect(terms.find((e) => e.id === "term:cedarPolicy")?.label).toBe("What is Cedar policy?");
  });
  it("finds a glossary term by its English name under the zh-CN UI", () => {
    const hit = rank(entries({ zhUi: true }), "cedar")[0];
    expect(hit?.run).toEqual({ glossary: "cedarPolicy" });
    expect(hit?.label).toBe("什么是Cedar 策略？");
  });
  it("offers the help actions: the whole glossary and the videos", () => {
    const help = entries().filter((e) => e.group === "help");
    expect(help.map((e) => e.run)).toEqual([{ glossary: null }, { to: "/v2/videos" }]);
    expect(rank(entries({ zhUi: true }), "glossary")[0]?.id).toBe("help:glossary");
  });
  it("gives every entry a unique id", () => {
    const list = entries({ agents: [{ id: "x", name: "x" }] });
    expect(new Set(list.map((e) => e.id)).size).toBe(list.length);
  });
});

describe("groupRanked", () => {
  it("orders groups by their best entry and keeps rank within a group", () => {
    const e = (id: string, group: PaletteEntry["group"]): PaletteEntry => ({
      id,
      group,
      label: id,
      keywords: [],
      run: { to: "/" },
    });
    const grouped = groupRanked([e("a1", "agent"), e("p1", "page"), e("a2", "agent"), e("g1", "glossary")]);
    expect(grouped.map((g) => [g.group, g.entries.map((x) => x.id)])).toEqual([
      ["agent", ["a1", "a2"]],
      ["page", ["p1"]],
      ["glossary", ["g1"]],
    ]);
  });
});

describe("keys", () => {
  const key = (k: string, mods: Partial<Record<"metaKey" | "ctrlKey" | "altKey" | "isComposing", boolean>> = {}) => ({
    key: k,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    ...mods,
  });
  it("⌘K and Ctrl+K toggle, K alone does not", () => {
    expect(isPaletteToggle(key("k", { metaKey: true }))).toBe(true);
    expect(isPaletteToggle(key("K", { ctrlKey: true }))).toBe(true);
    expect(isPaletteToggle(key("k"))).toBe(false);
    expect(isPaletteToggle(key("k", { ctrlKey: true, altKey: true }))).toBe(false);
  });
  it("/ opens outside text fields and overlays only", () => {
    const body = { tagName: "BODY", isContentEditable: false };
    expect(isSlashOpen(key("/"), body, false)).toBe(true);
    expect(isSlashOpen(key("/"), null, false)).toBe(true);
    expect(isSlashOpen(key("/"), { tagName: "INPUT" }, false)).toBe(false);
    expect(isSlashOpen(key("/"), { tagName: "textarea" }, false)).toBe(false);
    expect(isSlashOpen(key("/"), { tagName: "SELECT" }, false)).toBe(false);
    expect(isSlashOpen(key("/"), { tagName: "DIV", isContentEditable: true }, false)).toBe(false);
    expect(isSlashOpen(key("/"), body, true)).toBe(false);
    expect(isSlashOpen(key("/", { ctrlKey: true }), body, false)).toBe(false);
    expect(isSlashOpen(key("/", { isComposing: true }), body, false)).toBe(false);
    expect(isSlashOpen(key("?"), body, false)).toBe(false);
  });
  it("recognizes typing targets", () => {
    expect(isTypingTarget({ tagName: "BUTTON" })).toBe(false);
    expect(isTypingTarget(undefined)).toBe(false);
  });
  it("labels the shortcut per platform", () => {
    expect(shortcutLabel("MacIntel")).toBe("⌘K");
    expect(shortcutLabel("macOS")).toBe("⌘K");
    expect(shortcutLabel("iPhone")).toBe("⌘K");
    expect(shortcutLabel("Win32")).toBe("Ctrl K");
    expect(shortcutLabel("Linux x86_64")).toBe("Ctrl K");
  });
});

describe("palette store", () => {
  afterEach(() => closePalette());
  it("opens, closes and toggles", () => {
    expect(isPaletteOpen()).toBe(false);
    openPalette();
    expect(isPaletteOpen()).toBe(true);
    togglePalette();
    expect(isPaletteOpen()).toBe(false);
    togglePalette();
    expect(isPaletteOpen()).toBe(true);
  });
});

describe("palette copy", () => {
  it("has every key in both locales", () => {
    for (const locale of [en, zh as Locale]) {
      for (const k of ["label", "search", "placeholder", "openAgent", "chatWith", "openGlossary", "openVideos", "none"]) {
        expect(typeof (locale.v2.palette as Record<string, unknown>)[k]).toBe("string");
      }
      for (const g of PALETTE_GROUPS) expect(typeof locale.v2.palette.group[g]).toBe("string");
      for (const k of ["label", "palette", "glossary", "videos"] as const) expect(typeof locale.v2.help[k]).toBe("string");
    }
  });
});
