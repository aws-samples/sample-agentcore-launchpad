import { describe, expect, it } from "vitest";

import { agentProjectLabel, defaultAgentProjectId, pickedAgentProjectId } from "./agent-project";

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
