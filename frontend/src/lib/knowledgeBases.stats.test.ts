import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { jobStatsSummary, numericStats, STAT_LABEL_KEYS, STAT_TITLE_KEYS, statLabel } from "./knowledgeBases";

/** N17-obs271: the ingestion-job statistics read in the console's own words. */
function translator(bundle: Record<string, unknown>) {
  return (key: string) => {
    const value = key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown>)?.[part], bundle);
    if (typeof value !== "string") throw new Error(`missing i18n key ${key}`);
    return value;
  };
}

const stats = {
  numberOfDocumentsScanned: 4,
  numberOfNewDocumentsIndexed: 1,
  numberOfDocumentsUnchanged: 3,
  numberOfDocumentsFailed: 0,
};

describe("job statistics labels (N17-obs271)", () => {
  it("reads 「扫描 4 · 新索引 1 · 未变化 3 · 失败 0」 in zh-CN", () => {
    expect(jobStatsSummary(translator(zh), stats)).toBe("扫描 4 · 新索引 1 · 未变化 3 · 失败 0");
    expect(jobStatsSummary(translator(zh), { ...stats, numberOfDocumentsDeleted: 2 })).toBe(
      "扫描 4 · 新索引 1 · 未变化 3 · 失败 0 · 已移除 2",
    );
  });

  it("reads the same in English and every labelled key exists in both locales", () => {
    expect(jobStatsSummary(translator(en), stats)).toBe("Scanned 4 · Newly indexed 1 · Unchanged 3 · Failed 0");
    for (const key of [...Object.values(STAT_LABEL_KEYS), ...Object.values(STAT_TITLE_KEYS)]) {
      expect(() => translator(en)(key)).not.toThrow();
      expect(() => translator(zh)(key)).not.toThrow();
    }
  });

  it("orders the known keys first and keeps an unknown key, humanized", () => {
    const mixed = { numberOfModifiedDocumentsIndexed: 2, numberOfDocumentsFailed: 1, prefix_empty: false, numberOfDocumentsScanned: 3 };
    expect(numericStats(mixed).map(([k]) => k)).toEqual([
      "numberOfDocumentsScanned",
      "numberOfDocumentsFailed",
      "numberOfModifiedDocumentsIndexed",
    ]);
    expect(statLabel(translator(en), "numberOfModifiedDocumentsIndexed")).toBe("Modified Documents Indexed");
    expect(jobStatsSummary(translator(en), undefined)).toBe("");
  });
});
