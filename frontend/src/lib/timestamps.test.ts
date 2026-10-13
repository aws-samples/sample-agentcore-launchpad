import { describe, expect, it } from "vitest";

import { parseTimestamp } from "./timestamps";

describe("parseTimestamp", () => {
  it("reads a naive ledger timestamp as UTC, not local time", () => {
    expect(parseTimestamp("2026-10-07T06:20:04.314988")?.toISOString()).toBe("2026-10-07T06:20:04.314Z");
  });

  it("keeps an explicit offset", () => {
    expect(parseTimestamp("2026-10-07T06:20:26.993636+00:00")?.toISOString()).toBe("2026-10-07T06:20:26.993Z");
    expect(parseTimestamp("2026-10-07T14:20:00+08:00")?.toISOString()).toBe("2026-10-07T06:20:00.000Z");
    expect(parseTimestamp("2026-10-07T06:20:00Z")?.toISOString()).toBe("2026-10-07T06:20:00.000Z");
  });

  it("returns null for nothing or garbage", () => {
    expect(parseTimestamp(null)).toBeNull();
    expect(parseTimestamp("")).toBeNull();
    expect(parseTimestamp("not a date")).toBeNull();
  });
});
