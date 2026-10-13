import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { SkillLabJobInfo } from "./api";
import { elapsed } from "./skillLab";

const job = (started_at: string | null, finished_at: string | null = null) =>
  ({ started_at, finished_at }) as SkillLabJobInfo;

describe("elapsed", () => {
  // run in UTC+8 whatever the host zone: in UTC a naive string reads the same
  // either way, so the bug would pass unseen
  const hostTz = process.env.TZ;
  beforeAll(() => {
    process.env.TZ = "Asia/Shanghai";
  });
  afterAll(() => {
    if (hostTz === undefined) delete process.env.TZ;
    else process.env.TZ = hostTz;
  });
  afterEach(() => vi.useRealTimers());

  it("reads naive ledger times as UTC", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-13T06:00:30Z"));
    // naive: the ledger drops the offset. Read as local time it would sit 8 h in
    // the future and clamp to 0s.
    expect(elapsed(job("2026-10-13T06:00:00"))).toBe("30s");
  });

  it("measures a finished job between its own timestamps", () => {
    expect(elapsed(job("2026-10-13T06:00:00", "2026-10-13T06:03:00"))).toBe("3.0m");
    expect(elapsed(job("2026-10-13T06:00:00+00:00", "2026-10-13T06:00:45Z"))).toBe("45s");
  });

  it("shows a dash before the job starts", () => {
    expect(elapsed(job(null))).toBe("—");
  });
});
