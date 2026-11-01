import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../i18n";
import type { ExternalMcpTarget, LambdaToolTarget } from "../../lib/api/mcpReviews";
import { targetAttentionOf, V3Governance } from "./Governance";
import { GatewayToolTargets } from "./tools/GatewayToolTargets";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const at = (search: string) =>
  renderToStaticMarkup(<MemoryRouter initialEntries={[`/v3/governance${search}`]}><V3Governance /></MemoryRouter>);

const gate = (state: "none" | "reviewing" | "approved" | "rejected") =>
  ({ state, open_review_id: null, approved_review_id: null });

const lambda = (name: string, status: LambdaToolTarget["status"], review: ReturnType<typeof gate>): LambdaToolTarget => ({
  id: name, kind: "lambda", gateway_id: "gw", target_id: null, target_name: name,
  lambda_arn: `arn:aws:lambda:us-west-2:111122223333:function:${name}`, description: "",
  tool_names: [], status, error: null, created_by: "u", created_at: null, review,
});

const external = (name: string, review: ReturnType<typeof gate> | null): ExternalMcpTarget => ({
  target_id: name, target_name: name, endpoint: "https://mcp.example.com", status: "READY",
  hosting: "external", tool_id: review ? `t-${name}` : null, adopted_elsewhere: false, review, health: null,
});

describe("Gateway governance", () => {
  it("is named Gateway governance and offers the tool targets tab", () => {
    const html = at("");
    expect(html).toContain('<h1 class="v3-title">Gateway governance</h1>');
    expect(html).toContain("Tool targets");
    expect(html).not.toContain("New Lambda tool target");
  });

  it("opens the tool targets on ?tab=targets, with the create button in the page head", () => {
    const html = at("?tab=targets");
    expect(html).toContain('data-testid="v3-gateway-tools"');
    const head = html.slice(html.indexOf('class="v3-head"'), html.indexOf("</header>"));
    expect(head).toContain("New Lambda tool target");
    expect(head).toContain("Lambda functions and externally hosted MCP servers");
  });

  it("falls back to the gateways tab on an unknown tab", () => {
    expect(at("?tab=nope")).not.toContain('data-testid="v3-gateway-tools"');
  });

  it("has a Chinese name", async () => {
    await i18n.changeLanguage("zh-CN");
    const html = at("?tab=targets");
    expect(html).toContain('<h1 class="v3-title">网关治理</h1>');
    expect(html).toContain("工具目标");
    await i18n.changeLanguage("en");
  });
});

describe("the embedded tool targets", () => {
  it("has no page head of its own and lists the targets before the create form", () => {
    const html = renderToStaticMarkup(<GatewayToolTargets />);
    expect(html).not.toContain('class="v3-head"');
    expect(html.indexOf("Lambda tool targets")).toBeLessThan(html.indexOf("Externally hosted"));
  });
});

describe("tool-target attention", () => {
  const t = (k: string, o?: Record<string, unknown>) => `${k}:${o?.count ?? ""}`;

  it("raises failed publishes, then targets waiting for a schema review", () => {
    const items = targetAttentionOf(
      [lambda("crm", "failed", gate("approved")), lambda("hr", "reviewing", gate("reviewing")), lambda("ok", "live", gate("approved"))],
      [external("ext", gate("reviewing")), external("free", null)],
      t,
    );
    expect(items.map((a) => [a.key, a.s, a.title, a.detail])).toEqual([
      ["targets:failed", "act", "v3.governance.att.targetsFailed:1", "crm"],
      ["targets:review", "wait", "v3.governance.att.targetsReview:2", "hr  ·  ext"],
    ]);
    expect(items.every((a) => a.to === "/v3/governance?tab=targets")).toBe(true);
  });

  it("is empty when every target is live and approved", () => {
    expect(targetAttentionOf([lambda("ok", "live", gate("approved"))], [external("free", null)], t)).toEqual([]);
  });
});
