import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext } from "../../../auth/auth-context";
import i18n from "../../../i18n";
import { pickedAgentProjectId } from "../../../lib/agent-project";
import type { EligibleAgentProject } from "../../../lib/api";
import type { AgentProjectPick } from "../../../lib/useAgentProjectPick";
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
const loaded = (over: Partial<AgentProjectPick> & { data?: { projects: EligibleAgentProject[] } | null }) => {
  const { data, ...rest } = over;
  return { rows: data?.projects ?? [], error: null, loading: false, reload: () => undefined, projectId: "", pick: () => undefined, label: null, ...rest };
};
const render = (pick: AgentProjectPick, projectId = "", error?: string, isAdmin = true) =>
  renderToStaticMarkup(
    <AuthContext.Provider
      value={{
        authRequired: true, username: "u", role: isAdmin ? "admin" : "member", email: null,
        accountExpiresAt: null, builtin: false, accountSource: "local", isAdmin,
        can: () => true, logout: async () => undefined,
      }}
    >
      <MemoryRouter>
        <ProjectCard pick={{ ...pick, projectId }} error={error} />
      </MemoryRouter>
    </AuthContext.Provider>,
  );

describe("ProjectCard (#351)", () => {
  it("a `default` global project starts unpicked, the picker says why and links the projects page", () => {
    const value = pickedAgentProjectId([risk], null, "default");
    expect(value).toBe("");
    const html = render(loaded({ data: { projects: [risk] } }), value, "Choose the project this agent belongs to.");
    expect(html).toContain('data-testid="v2-agent-project"');
    expect(html).toContain("Choose the project this agent belongs to.");
    expect(html).toContain('href="/v3/projects"');
    // V2 has no projects page: the link says it opens the V3 console
    expect(html).toContain("Manage projects in the V3 console");
  });

  it("gives a member no projects link: the page is administrators' (prod QA 2026-11-02)", () => {
    const picker = render(loaded({ data: { projects: [risk] } }), "", undefined, false);
    expect(picker).toContain('data-testid="v2-agent-project"');
    expect(picker).not.toContain("/v3/projects");
    const empty = render(loaded({ data: { projects: [] } }), "", undefined, false);
    expect(empty).toContain("You have no business project here you can create agents in.");
    expect(empty).not.toContain("/v3/projects");
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

  it("with the agent-project switch on: lists the default project with its tag and says so when empty", () => {
    const def: EligibleAgentProject = {
      id: "default",
      name: "Default",
      department_id: null,
      department_name: null,
      is_workspace_default: true,
      is_default: true,
    };
    const listed = render(loaded({ data: { projects: [def, risk] }, defaultAllowed: true }), "default");
    expect(listed).toContain("Default · default project, no department");
    expect(listed).toContain("An administrator also allowed the workspace default project");
    // the default project is a valid pick: the placeholder no longer asks for a business project
    const unpicked = render(loaded({ data: { projects: [def] }, defaultAllowed: true }), "");
    expect(unpicked).toContain("Choose a project");
    expect(unpicked).not.toContain("Choose a business project");
    const empty = render(loaded({ data: { projects: [] }, defaultAllowed: true }));
    expect(empty).toContain("the workspace default project included");
    expect(empty).not.toContain("You have no business project here");
  });

  it("shows a failed read with a retry, and a loading state before the list lands", () => {
    const failed = render(loaded({ error: new Error("boom") }));
    expect(failed).toContain("boom");
    expect(failed).toContain('data-testid="v2-agent-project-retry"');
    const loading = render(loaded({ loading: true }));
    expect(loading).toContain("Loading");
    expect(loading).not.toContain('data-testid="v2-agent-project"');
  });
});
