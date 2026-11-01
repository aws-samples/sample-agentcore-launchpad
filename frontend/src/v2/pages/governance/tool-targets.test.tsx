import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import {
  type ExternalMcpTarget,
  type LambdaToolTarget,
  type McpGate,
  type McpReviewDetail,
  targetsNeedingAttention,
} from "../../../lib/api/mcpReviews";
import { V2Governance } from "../Governance";
import { ToolReviewView } from "./ToolReview";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const gate = (state: McpGate["state"]): McpGate => ({ state, open_review_id: null, approved_review_id: null, approved_hash: null });

const lambda = (name: string, status: LambdaToolTarget["status"], review: McpGate): LambdaToolTarget => ({
  id: name, kind: "lambda", gateway_id: "gw", target_id: null, target_name: name,
  lambda_arn: `arn:aws:lambda:us-west-2:111122223333:function:${name}`, description: "",
  tool_names: [], status, error: null, created_by: "u", created_at: null, review,
});

const external = (name: string, review: McpGate | null): ExternalMcpTarget => ({
  target_id: name, target_name: name, endpoint: "https://mcp.example.com", status: "READY",
  hosting: "external", tool_id: review ? `t-${name}` : null, adopted_elsewhere: false, review, health: null,
});

const at = (search: string) =>
  renderToStaticMarkup(<MemoryRouter initialEntries={[`/v2/governance${search}`]}><V2Governance /></MemoryRouter>);

describe("V2 Gateway governance", () => {
  it("is named Gateway governance and offers the tool targets tab", () => {
    const html = at("");
    expect(html).toContain("<h1>Gateway governance</h1>");
    expect(html).toContain('data-testid="v2-tab-targets"');
    expect(html).not.toContain('data-testid="v2-lambda-new"');
  });

  it("opens the tool targets on ?tab=targets, with the create button in the page head", () => {
    const html = at("?tab=targets");
    expect(html).toContain('data-testid="v2-tool-targets"');
    expect(html).toContain('data-testid="v2-lambda-new"');
    expect(html).toContain('data-testid="v2-lambda-form"');
    // the lists before the form, the externally hosted targets after it
    expect(html.indexOf('data-testid="v2-lambda-targets"')).toBeLessThan(html.indexOf('data-testid="v2-lambda-form"'));
    expect(html.indexOf('data-testid="v2-lambda-form"')).toBeLessThan(html.indexOf('data-testid="v2-external-targets"'));
  });

  it("falls back to the gateways tab on an unknown tab", () => {
    expect(at("?tab=nope")).not.toContain('data-testid="v2-tool-targets"');
  });

  it("has a Chinese name and tab", async () => {
    await i18n.changeLanguage("zh-CN");
    const html = at("?tab=targets");
    expect(html).toContain("<h1>网关治理</h1>");
    expect(html).toContain("工具目标");
    await i18n.changeLanguage("en");
  });
});

describe("targetsNeedingAttention", () => {
  it("names failed publishes, and the Lambda and adopted targets waiting for a review", () => {
    expect(
      targetsNeedingAttention(
        [lambda("crm", "failed", gate("approved")), lambda("hr", "reviewing", gate("reviewing")), lambda("ok", "live", gate("approved"))],
        [external("ext", gate("reviewing")), external("free", null)],
      ),
    ).toEqual({ failed: ["crm"], review: ["hr", "ext"] });
  });

  it("is empty when every target is live and approved", () => {
    expect(targetsNeedingAttention([lambda("ok", "live", gate("approved"))], [external("free", null)])).toEqual({ failed: [], review: [] });
  });
});

describe("the V2 tool review", () => {
  const review: McpReviewDetail = {
    id: "r1", agent_id: "t1", version: "1", target_name: "crm", tools_hash: "h", tool_count: 1,
    base_review_id: null, diff: { added: ["lookup"], removed: [], description_changed: [], schema_changed: [] }, hints: [{ tool: "lookup", kind: "write_name", detail: "" }],
    status: "reviewing", source: "deploy", requested_by: "alice", decided_by: null, decided_at: null, note: "",
    publish_status: null, publish_error: null, created_at: null,
    tools: [{ name: "lookup", description: "Look up", input_schema: { type: "object" } }],
    base_tools: [],
  };
  const render = (openId: string | null) =>
    renderToStaticMarkup(
      <ToolReviewView
        list={{ ...gate(openId ? "reviewing" : "approved"), agent_id: "t1", open_review_id: openId, reviews: [] }}
        review={review}
        reviewError={null}
        busy={false}
        note=""
        onNote={() => undefined}
        onDecide={() => undefined}
      />,
    );

  it("shows the tool list with its change mark and hints, and the decision on the open review", () => {
    const html = render("r1");
    expect(html).toContain("lookup");
    expect(html).toContain("Look up");
    expect(html).toContain('data-testid="v2-tool-review-approve"');
    expect(html).toContain('data-testid="v2-tool-review-reject"');
  });

  it("offers no decision once the review is closed", () => {
    expect(render(null)).not.toContain('data-testid="v2-tool-review-approve"');
  });
});
