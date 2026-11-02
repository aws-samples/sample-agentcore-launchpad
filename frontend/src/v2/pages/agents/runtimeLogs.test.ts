import { describe, expect, it } from "vitest";

import type { RuntimeLogEvent } from "../../../lib/api";
import {
  appendEvents,
  logTime,
  runtimeLogsGate,
  runtimeLogsQuery,
  streamLabel,
  unavailableKey,
  validSessionId,
} from "./runtimeLogs";

const ev = (message: string, extra: Partial<RuntimeLogEvent> = {}): RuntimeLogEvent => ({
  timestamp: "2026-10-19T08:00:00.123+00:00",
  stream: "2026/10/19/[runtime-logs-sess-12345678]abcd",
  session_id: "sess-12345678",
  message,
  ...extra,
});

describe("runtimeLogsQuery", () => {
  it("omits blank fields and trims", () => {
    expect(runtimeLogsQuery({})).toBe("");
    expect(runtimeLogsQuery({ range: "1h", session_id: "  ", keyword: " " })).toBe("?range=1h");
    expect(runtimeLogsQuery({ session_id: " sess-12345678 ", include_otel: false })).toBe("?session_id=sess-12345678");
  });

  it("encodes every field", () => {
    const q = new URLSearchParams(
      runtimeLogsQuery({ range: "7d", session_id: "a#feishu#b", keyword: "tool failed", include_otel: true, limit: 50, next_token: "x/y=" }).slice(1),
    );
    expect(Object.fromEntries(q)).toEqual({
      range: "7d",
      session_id: "a#feishu#b",
      keyword: "tool failed",
      include_otel: "true",
      limit: "50",
      next_token: "x/y=",
    });
  });
});

describe("validSessionId", () => {
  it("follows the backend alphabet and length", () => {
    expect(validSessionId("sess-12345678")).toBe(true);
    expect(validSessionId("01J#feishu#oc_1:x.y@z")).toBe(true);
    expect(validSessionId("short")).toBe(false);
    expect(validSessionId('sess-1234"|x')).toBe(false);
    expect(validSessionId("a".repeat(257))).toBe(false);
  });
});

describe("unavailableKey", () => {
  it("maps every reason", () => {
    expect(unavailableKey("no_runtime")).toBe("noRuntime");
    expect(unavailableKey("log_group_not_found")).toBe("noLogGroup");
    expect(unavailableKey("access_denied")).toBe("accessDenied");
    expect(unavailableKey(null)).toBeNull();
  });
});

describe("line helpers", () => {
  it("formats a millisecond time and tolerates junk", () => {
    expect(logTime("2026-10-19T08:00:00.123+00:00")).toMatch(/^\d{2}:\d{2}:\d{2}\.123$/);
    expect(logTime(null)).toBe("—");
    expect(logTime("nope")).toBe("nope");
  });

  it("labels a stream by session, else by its bracket tag", () => {
    expect(streamLabel(ev("x"))).toBe("sess-12345678");
    expect(streamLabel(ev("x", { session_id: null, stream: "[runtime-logs]1111-2222" }))).toBe("runtime-logs");
    expect(streamLabel(ev("x", { session_id: null, stream: "otel-rt-logs" }))).toBe("otel-rt-logs");
  });

  it("appends a page without repeating lines", () => {
    const merged = appendEvents([ev("a"), ev("b")], [ev("b"), ev("c")]);
    expect(merged.map((e) => e.message)).toEqual(["a", "b", "c"]);
  });
});

describe("runtimeLogsGate", () => {
  it("shows the toolbar once the agent has a Runtime", () => {
    expect(runtimeLogsGate(true, "active")).toBeNull();
    // a redeploy keeps the live Runtime and its logs readable
    expect(runtimeLogsGate(true, "deploying")).toBeNull();
    expect(runtimeLogsGate(true, "failed")).toBeNull();
  });

  it("tells a first deploy apart from an agent that has no Runtime", () => {
    expect(runtimeLogsGate(false, "deploying")).toBe("deploying");
    expect(runtimeLogsGate(false, "failed")).toBe("notDeployed");
    expect(runtimeLogsGate(false, "draft")).toBe("notDeployed");
    expect(runtimeLogsGate(false, "deleted")).toBe("deleted");
    expect(runtimeLogsGate(true, "deleted")).toBe("deleted");
  });
});
