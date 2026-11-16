import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { MemoryRecord } from "../../../lib/api";
import { SessionSummary as V3Summary } from "../../../v3/pages/memory/ShortTerm";
import type { SessionSummaryView } from "./common";
import { SessionSummary as V2Summary } from "./ShortTermTab";

vi.mock("react-i18next", async (importOriginal) => ({
  ...await importOriginal<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../../components/MemoryRecordText", () => ({
  MemoryRecordText: ({ text }: { text: string }) => createElement("div", { "data-testid": "summary-text" }, text),
}));

const base = (patch: Partial<SessionSummaryView> = {}): SessionSummaryView => ({
  status: "pending", namespaces: [], selected: null, records: [], token: null,
  error: null, warning: null, loadingMore: false, refresh: vi.fn(), select: vi.fn(), loadMore: vi.fn(), ...patch,
});

const record: MemoryRecord = { record_id: "rec", record_type: "FULL_SESSION", text: "The session recap",
  raw_text: "The raw recap", structured: null, strategy_id: "summary", namespaces: [],
  created_at: null, score: null, metadata: {}, source: "shared" };

for (const [name, Component] of [["V2", V2Summary], ["V3", V3Summary]] as const) {
  const render = (summary: SessionSummaryView) => renderToStaticMarkup(createElement(Component, { summary }));
  describe(`${name} session summary presentation`, () => {
    it("does not request attention before a session is selected", () => {
      expect(render(base({ status: "idle" }))).toBe("");
    });
    it("distinguishes loading, unavailable strategies and pending generation", () => {
      expect(render(base({ status: "loading" }))).toContain('role="status"');
      expect(render(base({ status: "unavailable" }))).toContain("memoryPage.fullSummary.unavailable");
      const pending = render(base());
      expect(pending).toContain("memoryPage.fullSummary.pending");
      expect(pending).not.toContain("loadFailed");
    });
    it("exposes failure and user-triggered retry separately from pending generation", () => {
      const html = render(base({ status: "error", error: "AWS unavailable" }));
      expect(html).toContain("loadFailed");
      expect(html.toLowerCase()).toContain("retry");
      expect(html).not.toContain("memoryPage.fullSummary.pending");
    });
    it("shows the recap, original content, source and manual refresh", () => {
      const html = render(base({ status: "ready", records: [record], token: "next" }));
      expect(html).toContain("The session recap");
      expect(html).toContain("The raw recap");
      expect(html).toContain("memoryPage.fullSummary.title");
      expect(html.toLowerCase()).toContain("shared");
      expect(html.toLowerCase()).toContain("refresh");
      expect(html.toLowerCase()).toContain("loadmore");
    });
    it("shows legacy unavailability alongside a healthy project recap", () => {
      const html = render(base({ status: "ready", records: [record], warning: "legacy unavailable" }));
      expect(html).toContain("memoryPage.fullSummary.sharedUnavailable");
      expect(html).toContain("The session recap");
    });
    it("renders a strategy/source selector when multiple namespaces are available", () => {
      const first = { strategy_id: "s1", strategy_name: "First", strategy_type: "SUMMARIZATION",
        template: "/summaries/{actorId}/{sessionId}", namespace: "/summaries/a/s", resolvable: true, prefix: false };
      const second = { ...first, strategy_id: "s2", strategy_name: "Second", source: "shared" as const };
      const html = render(base({ status: "ready", namespaces: [first, second], selected: first, records: [record] }));
      expect(html).toContain("First");
      // Both kits render only the selected label until their custom listbox opens.
      expect(html).toContain('aria-haspopup="listbox"');
      expect(html).toContain('aria-expanded="false"');
    });
  });
}
