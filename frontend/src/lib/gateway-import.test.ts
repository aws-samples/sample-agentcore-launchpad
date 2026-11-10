import { describe, expect, it } from "vitest";

import type { GatewayImportResult, GatewayImportTarget } from "./api";
import { memberApprovals, previewState, resultState, selectableIds, summarize } from "./gateway-import";

const item = (over: Partial<GatewayImportResult>): GatewayImportResult => ({
  name: "t",
  target: "t",
  target_id: "tgt0000001",
  gateway_id: "gw-1",
  record_id: "r1",
  created: false,
  reconciled: false,
  status: "APPROVED",
  tool_source: "live",
  ...over,
});

describe("gateway-import helpers (WB-005)", () => {
  it("classifies one result row as created / already / reconciled / failed", () => {
    expect(resultState(item({ created: true }))).toBe("created");
    expect(resultState(item({}))).toBe("already");
    expect(resultState(item({ reconciled: true }))).toBe("reconciled");
    // a failure wins over every other flag — the row never reads as a success
    expect(resultState(item({ created: true, error: "name held", error_code: "registry.name_held" }))).toBe("failed");
  });

  it("summarizes a result list by state", () => {
    expect(
      summarize([item({ created: true }), item({ created: true }), item({}), item({ reconciled: true }), item({ error: "x" })]),
    ).toEqual({ created: 2, already: 1, reconciled: 1, failed: 1 });
  });

  it("maps a preview target to new / imported / held", () => {
    const target = (over: Partial<GatewayImportTarget>): GatewayImportTarget => ({
      target_id: "tgt0000001",
      target: "t",
      target_status: "READY",
      tools: 1,
      tool_source: "live",
      record_id: null,
      status: null,
      imported: false,
      held: false,
      ...over,
    });
    expect(previewState(target({}))).toBe("new");
    expect(previewState(target({ record_id: "r", status: "APPROVED", imported: true }))).toBe("imported");
    expect(previewState(target({ record_id: "r", status: "DRAFT", held: true }))).toBe("held");
    expect(selectableIds({ targets: [target({}), target({ target_id: null })] } as never)).toEqual(["tgt0000001"]);
    expect(selectableIds(null)).toEqual([]);
  });
});

describe("memberApprovals (N17-obs261)", () => {
  const row = (over: Partial<GatewayImportTarget>): GatewayImportTarget => ({
    target_id: "tgt0000001", target: "t", target_status: "READY", tools: 1, tool_source: "live",
    record_id: "r", status: "PENDING_APPROVAL", imported: true, held: false, ...over,
  });
  const gateway = (targets: GatewayImportTarget[]) => ({
    gateway_id: "gw-1", name: "g", gateway_arn: "a", gateway_url: null, auth: "aws_iam" as const,
    auth_warning: null, platform: true, live_error: null, targets,
  });

  it("counts only ticked member submissions the import would approve", () => {
    const g = gateway([
      row({ target_id: "a", origin: "member", will_approve: true }),
      row({ target_id: "b", origin: "member", will_approve: true }),
      row({ target_id: "c", origin: "platform", will_approve: true, status: "DRAFT" }),
      row({ target_id: "d", origin: "member", will_approve: false }),
    ]);
    expect(memberApprovals(g, new Set(["a", "b", "c", "d"]))).toBe(2);
    expect(memberApprovals(g, new Set(["b", "c"]))).toBe(1);
    expect(memberApprovals(g, new Set())).toBe(0);
    expect(memberApprovals(null, new Set(["a"]))).toBe(0);
  });
});
