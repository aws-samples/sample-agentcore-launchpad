import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { SessionFiles } from "../../../lib/api";
import { FilesBody } from "./FilesPanel";
import { FILES_IDLE, type FilesState } from "./filesState";

const render = (state: Partial<FilesState>, sessionId: string | null = "s".repeat(40)) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <FilesBody
        state={{ ...FILES_IDLE, ...state }}
        sessionId={sessionId}
        editTo="/v2/agents?view=edit&id=a1"
        onRetry={() => undefined}
        onDownload={async () => ({ url: "https://example.test/f" })}
      />
    </MemoryRouter>,
  );
const files = (data: Partial<SessionFiles>) => ({
  data: { enabled: true, files: [], truncated: false, retention_days: 30, ...data } as SessionFiles,
});

describe("V2 chat inspector Files tab", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("asks for a session before it has one", () => {
    expect(render({}, null)).toContain('data-testid="files-placeholder"');
  });

  it("shows a spinner while the first read runs", () => {
    expect(render({ loading: true })).toContain("v2-spin");
  });

  it("says an agent without the folder has none, and links to its settings", () => {
    const html = render(files({ enabled: false }));
    expect(html).toContain('data-testid="files-disabled"');
    expect(html).toContain("This agent has no session output folder.");
    expect(html).toContain("/v2/agents?view=edit&amp;id=a1");
  });

  it("names the mount path in the empty state", () => {
    const html = render(files({ mount_path: "/mnt/out" }));
    expect(html).toContain('data-testid="files-empty"');
    expect(html).toContain("/mnt/out");
  });

  it("lists the files with size, a download each, the truncated notice and the retention note", () => {
    const html = render(files({
      truncated: true,
      files: [
        { key_rel: "report.md", size: 2048, last_modified: "2026-11-23T12:00:00+00:00" },
        { key_rel: "data/rows.csv", size: 12, last_modified: null },
      ],
    }));
    expect(html).toContain("report.md");
    expect(html).toContain("data/rows.csv");
    expect(html).toContain("2.0 KB");
    expect(html).toContain("12 B");
    expect(html.match(/data-testid="files-download"/g)).toHaveLength(2);
    expect(html).toContain("Showing the newest 2 files.");
    expect(html).toContain("kept for 30 days");
  });

  it("shows a failed read with a retry", () => {
    const html = render({ error: "boom" });
    expect(html).toContain('data-testid="files-error"');
    expect(html).toContain("boom");
    expect(html).toContain("Retry");
  });

  it("explains a session that is not the caller's instead of failing", () => {
    const html = render({ forbidden: true });
    expect(html).toContain('data-testid="files-forbidden"');
    expect(html).toContain("Only the session&#x27;s owner or an administrator can see its files.");
    expect(html).not.toContain("files-error");
  });
});
