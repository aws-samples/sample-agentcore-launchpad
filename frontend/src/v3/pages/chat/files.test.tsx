import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { SessionFiles } from "../../../lib/api";
import { FilesBody } from "./Inspector";

const read = (data: SessionFiles | null, busy = false) => ({ data, error: null, busy });
const render = (data: SessionFiles | null, busy = false) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <FilesBody read={read(data, busy)} agentId="a1" editTo="/v2/agents?view=edit&id=a1" sessionId={"s".repeat(40)} />
    </MemoryRouter>,
  );

describe("chat inspector Files tab", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("says an agent without the folder has none, and links to its settings", () => {
    const html = render({ enabled: false, files: [], truncated: false, retention_days: 30 });
    expect(html).toContain('data-testid="v3-chat-files-disabled"');
    expect(html).toContain("This agent has no session output folder.");
    expect(html).toContain("/v2/agents?view=edit&amp;id=a1");
  });

  it("names the mount path in the empty state", () => {
    const html = render({ enabled: true, files: [], truncated: false, retention_days: 30, mount_path: "/mnt/out" });
    expect(html).toContain('data-testid="v3-chat-files-empty"');
    expect(html).toContain("/mnt/out");
    expect(html).not.toContain("<table");
  });

  it("lists the files with size, a download each and the retention note", () => {
    const html = render({
      enabled: true,
      truncated: true,
      retention_days: 30,
      mount_path: "/mnt/outputs",
      files: [
        { key_rel: "report.md", size: 2048, last_modified: "2026-11-23T12:00:00+00:00" },
        { key_rel: "data/rows.csv", size: 12, last_modified: null },
      ],
    });
    expect(html.match(/data-testid="v3-chat-file"/g)).toHaveLength(2);
    expect(html).toContain("report.md");
    expect(html).toContain("2.0 KB");
    expect(html).toContain("12 B");
    expect(html.match(/data-testid="v3-chat-file-download"/g)).toHaveLength(2);
    expect(html).toContain("Showing the newest 2 files.");
    expect(html).toContain("kept for 30 days");
  });

  it("shows a skeleton while the first read runs and nothing before it", () => {
    expect(render(null, true)).not.toBe("");
    expect(render(null, false)).toBe("");
  });
});
