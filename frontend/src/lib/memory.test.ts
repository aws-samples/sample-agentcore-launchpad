import { describe, expect, it } from "vitest";

import { MEMORY_NAME_RE, MERGE_DEFAULTS, MERGE_KEYS, type MergeKey, mergeFormValue, mergePatch, mergeValueValid, parseMergeValue } from "./memory";

/** r14-a (N14-obs1): the merge-strategy knobs mirror the backend's bounds and send only changes. */
const form = (over: Partial<Record<MergeKey, string>> = {}): Record<MergeKey, string> => ({
  ...(Object.fromEntries(MERGE_KEYS.map((k) => [k, mergeFormValue(MERGE_DEFAULTS[k])])) as Record<MergeKey, string>),
  ...over,
});

describe("merge settings helpers", () => {
  it("the memory name rule is anchored at both ends (mutation baseline N16-obs63)", () => {
    expect(MEMORY_NAME_RE.test("1abc")).toBe(false); // first char must be a letter
    expect(MEMORY_NAME_RE.test("abc-d")).toBe(false); // whole string, not a prefix
    expect(MEMORY_NAME_RE.test("a".repeat(49))).toBe(false); // 48 max
  });

  it("parses within bounds, refuses outside them and lets only the TTL be empty", () => {
    expect(parseMergeValue("recall_half_life_days", "30")).toBe(30);
    expect(parseMergeValue("recall_half_life_days", "0")).toBeUndefined();
    expect(parseMergeValue("max_facts_per_actor", "9")).toBeUndefined();
    expect(parseMergeValue("max_facts_per_actor", "1.5")).toBeUndefined();
    expect(parseMergeValue("long_term_ttl_days", "")).toBeNull();
    expect(parseMergeValue("actor_summary_interval", "")).toBeUndefined();
    expect(mergeValueValid("long_term_ttl_days", "6")).toBe(false);
    expect(mergeValueValid("long_term_ttl_days", "7")).toBe(true);
  });

  it("sends nothing for an untouched form and only the changed keys otherwise", () => {
    expect(mergePatch(form(), MERGE_DEFAULTS)).toEqual({});
    expect(mergePatch(form({ max_facts_per_actor: "50", long_term_ttl_days: "90" }), MERGE_DEFAULTS)).toEqual({
      max_facts_per_actor: 50,
      long_term_ttl_days: 90,
    });
    // clearing a stored TTL sends null (= reset to keep forever)
    expect(mergePatch(form({ long_term_ttl_days: "" }), { ...MERGE_DEFAULTS, long_term_ttl_days: 30 })).toEqual({
      long_term_ttl_days: null,
    });
    // an invalid field is left out rather than sent
    expect(mergePatch(form({ recall_half_life_days: "-1" }), MERGE_DEFAULTS)).toEqual({});
  });
});
