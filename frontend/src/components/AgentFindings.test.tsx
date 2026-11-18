import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { BatchFindings } from "../lib/agent-findings";
import { BatchFindingResults } from "./AgentFindings";

const mock = vi.hoisted(() => ({ payload: {} as BatchFindings }));
vi.mock("../v2/hooks", () => ({ useLoad: () => ({ data: mock.payload, loading: false, error: null }) }));

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

function render(payload: Partial<BatchFindings>) {
  mock.payload = { source: "batch", state: "ready", aws_status: "COMPLETED", diagnostics: [], findings: [], ...payload };
  return renderToStaticMarkup(<MemoryRouter initialEntries={["/v3/tasks"]}><BatchFindingResults runId="run1" /></MemoryRouter>);
}

describe("Failure Analysis readback", () => {
  it("only treats a successful empty analysis as no failures", () => {
    const clean = i18n.t("agentFindings.batchClean");
    expect(render({})).toContain(clean);
    const partial = render({ state: "error", aws_status: "COMPLETED_WITH_ERRORS", diagnostics: ["analysis unavailable"] });
    expect(partial).not.toContain(clean);
    expect(partial).toContain("analysis unavailable");
  });
  it("keeps root-level and cause-level trace evidence alongside the fix", () => {
    const html = render({ state: "partial", findings: [
      { name: "Unit conversion", rootCauses: [{ name: "Wrong ratio", suggestedFix: "Use 100", sampleSessions: [{ sessionId: "cause/session" }] }] },
      { name: "Second observation", sampleSessions: [{ sessionId: "pattern/session", findings: [{ targetType: "TRACE_ID", targetId: "example-trace" }] }] },
    ] });
    expect(html).toContain("Use 100");
    expect(html).toContain("id=cause%2Fsession");
    expect(html).toContain("id=pattern%2Fsession");
    expect(html).toContain("view=trace&amp;id=example-trace");
  });
});
