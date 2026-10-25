import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import type { OverviewInfo } from "../../../lib/api";
import { fmtSpan, inboxSignal, LIFECYCLE, qualitySignal, serviceRows } from "./modules";

const t = ((key: string, opts?: { n?: number }) => `${key}:${opts?.n ?? ""}`) as unknown as TFunction;

const info = (services: Record<string, boolean>): OverviewInfo => ({
  registry_assets: { agents: 0, tools: 0, skills: 0, total: 0 },
  active_sessions: 0,
  eval_pass_rate: null,
  eval_runs: 0,
  services,
  service_detail: { gateway: "gw-1" },
});

describe("Command center modules", () => {
  it("links every lifecycle step to a V3 page", () => {
    expect(LIFECYCLE.map((s) => s.key)).toEqual(["create", "chat", "observe", "evaluate", "optimize", "govern"]);
    for (const step of LIFECYCLE) expect(step.to.startsWith("/v3/")).toBe(true);
  });

  it("reads a TTFA span as minutes, hours or days", () => {
    expect(fmtSpan(t, 10)).toBe("v2.home.kpi.minutes:1");
    expect(fmtSpan(t, 30 * 60)).toBe("v2.home.kpi.minutes:30");
    expect(fmtSpan(t, 3 * 3600)).toBe("v2.home.kpi.hours:3");
    expect(fmtSpan(t, 72 * 3600)).toBe("v2.home.kpi.days:3");
  });

  it("colours online quality at the V2 thresholds", () => {
    expect(qualitySignal(null)).toBeUndefined();
    expect(qualitySignal(0.8)).toBe("ok");
    expect(qualitySignal(0.6)).toBeUndefined();
    expect(qualitySignal(0.49)).toBe("act");
  });

  it("maps inbox severities onto signals", () => {
    expect(inboxSignal("action")).toBe("act");
    expect(inboxSignal("warn")).toBe("wait");
    expect(inboxSignal("info")).toBe("info");
  });

  it("reads service health and links each row to its V3 page", () => {
    const rows = serviceRows(info({ gateway: true, memory: false, policy: false }), 0);
    const by = Object.fromEntries(rows.map((r) => [r.id, r]));
    expect(by.runtime).toMatchObject({ state: "idle", signal: "off", to: "/v3/agents" });
    expect(by.gateway).toMatchObject({ state: "ready", signal: "ok", to: "/v3/governance", detail: "gw-1" });
    expect(by.memory).toMatchObject({ state: "pending", signal: "wait", to: "/v3/memory" });
    expect(by.policy).toMatchObject({ state: "idle", signal: "off" });
    expect(by.evaluation.to).toBe("/v3/tasks");
    expect(by.registry.to).toBe("/v3/registry");
    expect(by.observability.to).toBe("/v3/observability");
    expect(serviceRows(info({}), 2).find((r) => r.id === "runtime")?.state).toBe("ready");
  });
});
