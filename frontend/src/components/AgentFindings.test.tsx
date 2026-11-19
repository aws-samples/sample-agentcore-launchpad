import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { AgentFindingsPage, BatchFindings, InsightStore } from "../lib/agent-findings";
import { BatchFindingResults, ConfigPatterns } from "./AgentFindings";

const mock = vi.hoisted(() => ({
  payload: {} as BatchFindings,
  store: { state: "ready" } as InsightStore,
  page: { application_name: "svc.DEFAULT", items: [], next_cursor: null } as AgentFindingsPage,
}));
vi.mock("../v2/hooks", () => ({
  useLoad: (_fetch: unknown, key: string) => ({
    data: key === "finding-store" ? mock.store : key.startsWith("findings:") ? mock.page : mock.payload,
    loading: false, error: null, reload: () => undefined,
  }),
}));

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
  it("on an online config, separates missing patterns from session scores and gates on the Store", () => {
    const patterns = (store: InsightStore) => {
      mock.store = store;
      return renderToStaticMarkup(<MemoryRouter initialEntries={["/v2/eval/tasks"]}><ConfigPatterns agentId="a1" /></MemoryRouter>);
    };
    const ready = patterns({ state: "ready" });
    expect(ready).toContain(i18n.t("agentFindings.noLiveConfig"));
    expect(ready).toContain("svc.DEFAULT");
    const disabled = patterns({ state: "disabled" });
    expect(disabled).toContain(i18n.t("agentFindings.store.disabled"));
    expect(disabled).not.toContain(i18n.t("agentFindings.noLiveConfig"));
  });
});
