import { beforeEach, describe, expect, it, vi } from "vitest";

import { currentPeriod } from "../lib/budgets";
import { primeMyUsage, refreshMyUsageIfLimited } from "./myUsage";

const { myUsage } = vi.hoisted(() => ({ myUsage: vi.fn() }));
vi.mock("../lib/api", () => ({ api: { myUsage: () => myUsage() } }));

function usage(unlimited: boolean) {
  return {
    user_id: "u-1",
    username: "mia",
    level: unlimited ? null : { id: "l-1", name: "basic" },
    basis: unlimited ? ("unlimited" as const) : ("level" as const),
    limit_usd: unlimited ? null : 10,
    used_usd: 8.5,
    remaining_usd: unlimited ? null : 1.5,
    estimated_usd: 0,
    estimated_share: 0,
    percent_used: unlimited ? null : 85,
    unlimited,
    period: currentPeriod(),
    resets_at: "2099-01-01T00:00:00+00:00",
    by_source: [],
  };
}

describe("refreshMyUsageIfLimited (the banner after a charged turn)", () => {
  beforeEach(() => {
    myUsage.mockReset();
    myUsage.mockResolvedValue(usage(false));
  });

  it("costs no request for an unlimited person", () => {
    primeMyUsage(usage(true));
    refreshMyUsageIfLimited();
    expect(myUsage).not.toHaveBeenCalled();
  });

  it("re-reads once a limit applies", async () => {
    primeMyUsage(usage(false));
    refreshMyUsageIfLimited();
    refreshMyUsageIfLimited(); // concurrent calls share one request
    expect(myUsage).toHaveBeenCalledTimes(1);
    await Promise.resolve();
  });
});
