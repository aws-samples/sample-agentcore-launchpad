import { afterEach, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import {
  closeGlossary,
  getGlossary,
  GLOSSARY_TERMS,
  glossaryMatches,
  isGlossaryTerm,
  openGlossary,
} from "./glossaryTerms";
import { V2_NAV } from "./nav";

type Locale = typeof en;
const LOCALES: [string, Locale][] = [
  ["en", en],
  ["zh-CN", zh as Locale],
];

function lookup(locale: Locale, key: string): unknown {
  return key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown> | undefined)?.[part], locale);
}

describe("glossaryMatches", () => {
  const registry = ["registry", "注册中心", "Registry", "共享目录：发布 Agent、MCP 服务器和技能，供他人查找和复用。"] as const;
  const match = (query: string) => glossaryMatches(registry[0], query, registry[1], registry[2], registry[3]);

  it("matches the localized name", () => {
    expect(match("注册")).toBe(true);
  });
  it("matches the English name under another locale", () => {
    expect(match("Regis")).toBe(true);
  });
  it("matches the definition", () => {
    expect(match("复用")).toBe(true);
    expect(match("MCP")).toBe(true);
  });
  it("is case-insensitive and trims the query", () => {
    expect(match("  REGISTRY ")).toBe(true);
    expect(match("mcp")).toBe(true);
  });
  it("matches the term key", () => {
    expect(glossaryMatches("cedarPolicy", "cedarpol", "Cedar 策略", "Cedar policy", "…")).toBe(true);
  });
  it("matches everything on an empty query", () => {
    expect(match("")).toBe(true);
    expect(match("   ")).toBe(true);
  });
  it("rejects an unrelated query", () => {
    expect(match("canary")).toBe(false);
  });
});

describe("glossary terms", () => {
  it("are the 19 platform terms, without the Agent-DLC criteria", () => {
    expect(GLOSSARY_TERMS).toHaveLength(19);
    expect(new Set(GLOSSARY_TERMS).size).toBe(19);
    expect(isGlossaryTerm("criteria")).toBe(false);
  });

  it.each(LOCALES)("each has a name and a definition in %s", (_lng, locale) => {
    for (const term of GLOSSARY_TERMS) {
      const sentence = lookup(locale, `glossary.${term}`);
      const name = lookup(locale, `glossary.name.${term}`);
      expect(typeof sentence, term).toBe("string");
      expect((sentence as string).length, term).toBeGreaterThan(10);
      expect(typeof name, term).toBe("string");
      expect((name as string).length, term).toBeGreaterThan(0);
    }
  });

  it.each(LOCALES)("has the dialog and whatIs copy in %s", (_lng, locale) => {
    for (const key of ["glossary.whatIs", "v2.glossary.title", "v2.glossary.search", "v2.glossary.none"]) {
      expect(typeof lookup(locale, key), key).toBe("string");
    }
    expect(lookup(locale, "glossary.whatIs")).toContain("{{term}}");
  });

  it("covers every nav hintKey", () => {
    const hintKeys = V2_NAV.flatMap((group) => group.items.map((item) => item.hintKey)).filter(
      (key): key is string => key !== undefined,
    );
    expect(hintKeys.length).toBeGreaterThan(0);
    for (const key of hintKeys) {
      const term = key.replace(/^glossary\./, "");
      expect(key.startsWith("glossary."), key).toBe(true);
      expect(isGlossaryTerm(term), key).toBe(true);
    }
  });
});

describe("glossary store", () => {
  afterEach(() => closeGlossary());

  it("starts closed", () => {
    expect(getGlossary()).toEqual({ open: false, term: null });
  });
  it("opens at a known term", () => {
    openGlossary("memory");
    expect(getGlossary()).toEqual({ open: true, term: "memory" });
  });
  it("opens the whole list without a term or with an unknown one", () => {
    openGlossary();
    expect(getGlossary()).toEqual({ open: true, term: null });
    openGlossary("criteria");
    expect(getGlossary()).toEqual({ open: true, term: null });
  });
  it("closes", () => {
    openGlossary("a2a");
    closeGlossary();
    expect(getGlossary().open).toBe(false);
  });
});
