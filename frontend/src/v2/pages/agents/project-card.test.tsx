import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { pickedAgentProjectId } from "../../../lib/agent-project";
import type { EligibleAgentProject } from "../../../lib/api";
import type { Loaded } from "../../hooks";
import { ProjectCard } from "./ProjectCard";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const risk: EligibleAgentProject = {
  id: "risk-bot",
  name: "Risk bot",
  department_id: "dept-risk",
  department_name: "Risk",
  is_workspace_default: false,
};
const loaded = (over: Partial<Loaded<{ projects: EligibleAgentProject[] }>>) => ({
  data: null,
  error: null,
  loading: false,
  reload: () => undefined,
  ...over,
});
const render = (projects: ReturnType<typeof loaded>, value = "", error?: string) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <ProjectCard projects={projects} value={value} onPick={() => undefined} error={error} />
    </MemoryRouter>,
  );

describe("ProjectCard (#351)", () => {
  it("a `default` global project starts unpicked, the picker says why and links the projects page", () => {
    const value = pickedAgentProjectId([risk], null, "default");
    expect(value).toBe("");
    const html = render(loaded({ data: { projects: [risk] } }), value, "Choose the project this agent belongs to.");
    expect(html).toContain('data-testid="v2-agent-project"');
    expect(html).toContain("Choose the project this agent belongs to.");
    expect(html).toContain('href="/v3/projects"');
  });

  it("shows the picked project as `Department · Project`", () => {
    const html = render(loaded({ data: { projects: [risk] } }), "risk-bot");
    expect(html).toContain("Risk · Risk bot");
    expect(html).not.toContain('href="/v3/projects"');
  });

  it("explains an empty eligible list with the projects page as the next step", () => {
    const html = render(loaded({ data: { projects: [] } }));
    expect(html).toContain("You have no business project here you can create agents in.");
    expect(html).toContain('href="/v3/projects"');
    expect(html).not.toContain('data-testid="v2-agent-project"');
  });

  it("shows a failed read with a retry, and a loading state before the list lands", () => {
    const failed = render(loaded({ error: "boom" }));
    expect(failed).toContain("boom");
    expect(failed).toContain('data-testid="v2-agent-project-retry"');
    const loading = render(loaded({ loading: true }));
    expect(loading).toContain("Loading");
    expect(loading).not.toContain('data-testid="v2-agent-project"');
  });
});
