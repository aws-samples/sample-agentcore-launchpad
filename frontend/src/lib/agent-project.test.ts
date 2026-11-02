import { describe, expect, it } from "vitest";

import {
  agentProjectCopyKeys,
  agentProjectLabel,
  agentProjectManageLink,
  agentProjectOptionLabel,
  defaultAgentProjectId,
  pickedAgentProjectId,
} from "./agent-project";

const eligible = [{ id: "sales-bot" }, { id: "hr-faq" }];

describe("defaultAgentProjectId", () => {
  it("starts on the shell's project when it is eligible", () => {
    expect(defaultAgentProjectId(eligible, "hr-faq")).toBe("hr-faq");
  });

  it("starts empty when the shell's project is not eligible (e.g. `default`)", () => {
    expect(defaultAgentProjectId(eligible, "default")).toBe("");
    expect(defaultAgentProjectId(eligible, null)).toBe("");
    expect(defaultAgentProjectId([], "hr-faq")).toBe("");
  });
});

describe("pickedAgentProjectId", () => {
  it("keeps an explicit pick that is still listed", () => {
    expect(pickedAgentProjectId(eligible, "sales-bot", "hr-faq")).toBe("sales-bot");
  });

  it("drops a pick the server no longer lists", () => {
    expect(pickedAgentProjectId(eligible, "gone", "hr-faq")).toBe("");
  });

  it("falls back to the default when nothing was picked", () => {
    expect(pickedAgentProjectId(eligible, null, "hr-faq")).toBe("hr-faq");
  });
});

describe("agentProjectLabel", () => {
  it("prefixes the department", () => {
    expect(agentProjectLabel({ name: "HR FAQ", department_name: "People" })).toBe("People · HR FAQ");
    expect(agentProjectLabel({ name: "HR FAQ", department_name: null })).toBe("HR FAQ");
  });
});

describe("the agent-project switch (default project listed)", () => {
  const withDefault = [{ id: "default", is_default: true }, { id: "hr-faq" }];

  it("never preselects the default project, even as the shell's project", () => {
    expect(defaultAgentProjectId(withDefault, "default")).toBe("");
    expect(defaultAgentProjectId(withDefault, "hr-faq")).toBe("hr-faq");
  });

  it("keeps an explicit pick of the default project", () => {
    expect(pickedAgentProjectId(withDefault, "default", "hr-faq")).toBe("default");
  });

  it("labels the default project with its no-department tag", () => {
    const tag = "default project, no department";
    expect(agentProjectOptionLabel({ name: "Default", department_name: null, is_default: true }, tag)).toBe(
      "Default · default project, no department",
    );
    expect(agentProjectOptionLabel({ name: "HR FAQ", department_name: "People" }, tag)).toBe("People · HR FAQ");
  });

  it("switches the hint and empty-list copy with the switch", () => {
    expect(agentProjectCopyKeys(false)).toEqual({
      hint: "v3.create.project.hint",
      none: "v3.create.project.none",
      placeholder: "v3.create.project.placeholder",
    });
    expect(agentProjectCopyKeys(undefined).none).toBe("v3.create.project.none");
    expect(agentProjectCopyKeys(true)).toEqual({
      hint: "v3.create.project.hintDefault",
      none: "v3.create.project.noneDefault",
      placeholder: "v3.create.project.placeholderDefault",
    });
  });
});

describe("agentProjectManageLink", () => {
  it("links only administrators, and says the page is in the V3 console", () => {
    expect(agentProjectManageLink(false)).toBeNull();
    expect(agentProjectManageLink(true)).toEqual({ to: "/v3/projects", labelKey: "v3.create.project.openInV3" });
  });
});
