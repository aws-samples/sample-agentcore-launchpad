import { describe, expect, it } from "vitest";

import type { AssistantProposal } from "./api";
import { planUnblockGoldenTest } from "./assistant";

// N17-obs376: `expected_trajectory: []` now means "no tools expected" (scored, as on Global),
// so a golden test without expected tools must re-draft WITHOUT the field.
function proposals(expected_tools?: string[]): AssistantProposal[] {
  return [{
    revision: 1,
    content: { golden_tests: [{ id: "gt1", input: "hi", expected_response: "hello", expected_tools }] },
  } as unknown as AssistantProposal];
}
const plan = { source_revision: 1, scenarios: [], evaluators: [], blocked_golden_tests: [{ golden_test_id: "gt1", reason: "x" }] } as unknown as Parameters<typeof planUnblockGoldenTest>[0];

describe("planUnblockGoldenTest trajectory (N17-obs376)", () => {
  it("omits the trajectory of a golden test that names no tools", () => {
    const out = planUnblockGoldenTest(plan, proposals(), "gt1");
    const scenario = (out?.scenarios as Record<string, unknown>[])[0];
    expect("expected_trajectory" in scenario).toBe(false);
  });

  it("keeps the tools a golden test names", () => {
    const out = planUnblockGoldenTest(plan, proposals(["kb_search"]), "gt1");
    expect((out?.scenarios as Record<string, unknown>[])[0].expected_trajectory).toEqual(["kb_search"]);
  });
});
