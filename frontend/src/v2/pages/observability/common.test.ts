import { describe, expect, it } from "vitest";

import { asTraceStatus } from "./common";

describe("asTraceStatus", () => {
  it("accepts the two trace statuses", () => {
    expect(asTraceStatus("error")).toBe("error");
    expect(asTraceStatus("ok")).toBe("ok");
  });

  it("reads anything else as all", () => {
    for (const value of [null, "", "ERROR", "timeout", "all"]) expect(asTraceStatus(value)).toBe("");
  });
});
