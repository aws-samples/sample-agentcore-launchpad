import { describe, expect, it } from "vitest";

import { criteriaEvaluatorIds, editorRows, latestPublishedVersion, refusedFindings, type CriterionInput } from "./dlc";
import { isGoldenSplit } from "./goldenRun";

const input = (key: string): CriterionInput => ({ key, text: "", dimension: "quality", tier: "gate" });

describe("editorRows", () => {
  it("keys each row by a client id, so editing its 编号 keeps the same React key", () => {
    const rows = editorRows([input("C1"), input("C1"), input("C2")]);
    expect(new Set(rows.map((r) => r.uid)).size).toBe(3);
    const edited = rows.map((r) => (r.uid === rows[0].uid ? { uid: r.uid, row: { ...r.row, key: "R1" } } : r));
    expect(edited[0].uid).toBe(rows[0].uid);
    // rows added later never reuse an id
    const more = editorRows([input("C3")]);
    expect(rows.map((r) => r.uid)).not.toContain(more[0].uid);
  });
});

describe("refusedFindings", () => {
  const findings = [{ level: "error" as const, code: "criteria.redline_judge", message: "m", key: "R1" }];
  it("reads the findings of a 422 criteria.invalid", () => {
    expect(refusedFindings({ code: "criteria.invalid", detail: { findings } })).toEqual(findings);
  });
  it("ignores every other failure", () => {
    expect(refusedFindings({ code: "criteria.not_draft", detail: { findings } })).toBeNull();
    expect(refusedFindings({ code: "criteria.invalid", detail: null })).toBeNull();
    expect(refusedFindings(new Error("network"))).toBeNull();
    expect(refusedFindings(null)).toBeNull();
  });
});

describe("golden split criteria", () => {
  it("scores against the newest published version, never a draft", () => {
    const v = (version: number, status: string) => ({ version, status, signed_by: null, published_at: null });
    expect(latestPublishedVersion([v(1, "superseded"), v(2, "published"), v(3, "draft")])).toBe(2);
    expect(latestPublishedVersion([v(1, "draft")])).toBeNull();
  });

  it("takes each evaluator once, in table order, skipping metric and human rows", () => {
    expect(
      criteriaEvaluatorIds([
        { executor: { kind: "evaluator", evaluator_id: "Code-R1" } },
        { executor: { kind: "metric" } },
        { executor: { kind: "evaluator", evaluator_id: "Builtin.GoalSuccessRate" } },
        { executor: { kind: "human" } },
        { executor: { kind: "evaluator", evaluator_id: "Code-R1" } },
      ]),
    ).toEqual(["Code-R1", "Builtin.GoalSuccessRate"]);
  });

  it("recognises only a golden split bound to a lineage", () => {
    expect(isGoldenSplit({ role: "golden", split: "dev", criteria_lineage_id: "abc" })).toBe(true);
    expect(isGoldenSplit({ role: "golden", split: null, criteria_lineage_id: "abc" })).toBe(false); // the parent
    expect(isGoldenSplit({ role: "golden", split: "dev", criteria_lineage_id: null })).toBe(false);
    expect(isGoldenSplit({ role: "scratch", split: null, criteria_lineage_id: null })).toBe(false);
    expect(isGoldenSplit(null)).toBe(false);
  });
});
