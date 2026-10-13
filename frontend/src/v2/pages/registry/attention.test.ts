import { describe, expect, it } from "vitest";

import type { RegistryRecordOut, RegistryRecordSystem } from "../../../lib/api";
import { APPROVAL_CAP, approvalQueue, HINT_MAX } from "./attention";

function rec(id: string, over: Partial<RegistryRecordOut> = {}): RegistryRecordOut {
  return {
    system: null,
    record_id: id,
    name: `rec-${id}`,
    description: `about ${id}`,
    type: "MCP",
    status: "PENDING_APPROVAL",
    version: "1.0.0",
    created_at: "2026-10-12T08:00:00",
    updated_at: "2026-10-12T08:00:00",
    ...over,
  };
}

const system: RegistryRecordSystem = {
  managed: true,
  preset_key: "architect",
  label: "Architect",
  skill_version: "1.5.8",
  release_digest: null,
  path: null,
  protected_actions: ["edit", "replace", "reimport", "delete"],
  admin_actions: ["submit", "approve", "reject", "disable"],
};

describe("registry approval queue", () => {
  it("is empty when nothing is pending", () => {
    expect(approvalQueue([], false).entries).toEqual([]);
    const settled = ["APPROVED", "DRAFT", "REJECTED", "DEPRECATED"].map((status, i) => rec(`r${i}`, { status }));
    expect(approvalQueue(settled, true).entries).toEqual([]);
  });

  it("queues a pending record as wait with its name, type and version", () => {
    expect(approvalQueue([rec("a", { type: "AGENT_SKILLS", version: "2.1.0" })], false).entries).toEqual([
      {
        key: "a:pending",
        kind: "pending",
        signal: "wait",
        recordId: "a",
        values: { name: "rec-a", type: "AGENT_SKILLS", version: "2.1.0", detail: "about a" },
      },
    ]);
  });

  it("hides a system record from a member and shows it to an admin", () => {
    const records = [rec("sys", { system }), rec("own")];
    expect(approvalQueue(records, false).entries.map((e) => e.recordId)).toEqual(["own"]);
    expect(approvalQueue(records, true).entries.map((e) => e.recordId)).toEqual(["sys", "own"]);
  });

  it("excludes records that are not pending approval", () => {
    const records = [rec("a"), rec("b", { status: "APPROVED" }), rec("c", { status: "DRAFT" }), rec("d")];
    expect(approvalQueue(records, true).entries.map((e) => e.recordId)).toEqual(["a", "d"]);
  });

  it("keeps the API order instead of sorting by date", () => {
    const records = [
      rec("old", { updated_at: "2026-10-01T00:00:00" }),
      rec("new", { updated_at: "2026-10-12T00:00:00" }),
      rec("mid", { updated_at: "2026-10-05T00:00:00" }),
    ];
    expect(approvalQueue(records, false).entries.map((e) => e.recordId)).toEqual(["old", "new", "mid"]);
  });

  it("falls back to the record id, clips a long description and blanks a missing version", () => {
    const [bare] = approvalQueue([rec("a", { description: "", version: null })], false).entries;
    expect(bare.values).toMatchObject({ detail: "a", version: "" });
    const [long] = approvalQueue([rec("b", { description: "x".repeat(200) })], false).entries;
    expect(long.values.detail).toHaveLength(HINT_MAX);
    expect(long.values.detail.endsWith("…")).toBe(true);
    const [exact] = approvalQueue([rec("c", { description: "y".repeat(HINT_MAX) })], false).entries;
    expect(exact.values.detail).toBe("y".repeat(HINT_MAX));
  });

  it("shows at most APPROVAL_CAP rows but counts every pending record", () => {
    const many = Array.from({ length: APPROVAL_CAP + 3 }, (_, i) => rec(`r${i}`));
    const { entries, total } = approvalQueue(many, false);
    expect(entries.map((e) => e.recordId)).toEqual(many.slice(0, APPROVAL_CAP).map((r) => r.record_id));
    expect(total).toBe(APPROVAL_CAP + 3);
  });
});
