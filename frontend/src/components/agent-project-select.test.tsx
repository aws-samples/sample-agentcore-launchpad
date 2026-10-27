import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { AgentProjectPick } from "../lib/useAgentProjectPick";
import { AgentProjectSelect } from "./AgentProjectSelect";
import { ConfirmDialog } from "./ConfirmDialog";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const risk = { id: "risk-bot", name: "Risk bot", department_id: "d", department_name: "Risk", is_workspace_default: false };
const pick = (over: Partial<AgentProjectPick> = {}): AgentProjectPick => ({
  rows: [],
  loading: false,
  error: null,
  reload: () => undefined,
  projectId: "",
  pick: () => undefined,
  label: null,
  ...over,
});
const render = (p: AgentProjectPick, touched = false) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <AgentProjectSelect pick={p} id="x-project" touched={touched} />
    </MemoryRouter>,
  );

describe("AgentProjectSelect (classic, #356)", () => {
  it("lists the eligible projects, unpicked by default, with the projects page as the way out", () => {
    const html = render(pick({ rows: [risk] }));
    expect(html).toContain('data-testid="x-project"');
    expect(html).toContain("Risk · Risk bot");
    expect(html).toContain('href="/v3/projects"');
    expect(render(pick({ rows: [risk] }), true)).toContain("Choose the project this agent belongs to.");
  });

  it("a pick hides the projects link", () => {
    expect(render(pick({ rows: [risk], projectId: "risk-bot" }))).not.toContain('href="/v3/projects"');
  });

  it("explains an empty list, a failed read (with retry) and a load in flight", () => {
    expect(render(pick())).toContain("You have no business project here you can create agents in.");
    const failed = render(pick({ error: new Error("boom") }));
    expect(failed).toContain("boom");
    expect(failed).toContain("Retry");
    const loading = render(pick({ loading: true }));
    expect(loading).toContain("Loading");
    expect(loading).not.toContain('data-testid="x-project"');
  });

  it("rides inside a ConfirmDialog, whose confirm waits for it", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <ConfirmDialog open title="Convert" body="Body" confirmLabel="Go" confirmDisabled onConfirm={() => undefined} onCancel={() => undefined}>
          <AgentProjectSelect pick={pick({ rows: [risk] })} id="x-project" />
        </ConfirmDialog>
      </MemoryRouter>,
    );
    expect(html).toContain('data-testid="x-project-field"');
    expect(html).toMatch(/<button[^>]*disabled[^>]*>Go<\/button>|<button[^>]*disabled[^>]*>[^<]*Go/);
  });
});
