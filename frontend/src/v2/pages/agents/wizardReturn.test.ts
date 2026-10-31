import { describe, expect, it } from "vitest";

import { projectSwitchFor, pruneToCatalogs, wizardBackTarget, wizardDoneTarget } from "./wizardReturn";

describe("wizard return (v3-agent-management §4)", () => {
  it("a publish lands on the V3 detail in V3, on V2's own detail otherwise", () => {
    expect(wizardDoneTarget("v3", "a 1")).toBe("/v3/agents?id=a%201");
    expect(wizardDoneTarget("v2", "a1")).toBeNull();
    expect(wizardDoneTarget("v1", "a1")).toBeNull();
  });

  it("a new agent filed under another project lands in that project", () => {
    expect(projectSwitchFor("hr-shared", "default")).toBe("hr-shared");
    expect(projectSwitchFor("hr-shared", "hr-shared")).toBeNull();
    expect(projectSwitchFor(null, "default")).toBeNull();
    expect(projectSwitchFor("hr-shared", undefined)).toBe("hr-shared");
    expect(wizardDoneTarget("v3", "a1", "hr-shared")).toBe("/v3/agents?id=a1&project=hr-shared");
    expect(wizardDoneTarget("v3", "a1", null)).toBe("/v3/agents?id=a1");
    // V2 lands on its own detail; the wizard adds `project` to those params
    expect(wizardDoneTarget("v2", "a1", "hr-shared")).toBeNull();
  });

  it("back in V3 returns to the page that opened the wizard", () => {
    expect(wizardBackTarget("v3", "k3x9", undefined)).toBe(-1);
    expect(wizardBackTarget("v3", "k3x9", "a1")).toBe(-1);
  });

  it("opened straight from a link, back goes to the V3 page it belongs to", () => {
    expect(wizardBackTarget("v3", "default", "a1")).toBe("/v3/agents?id=a1");
    expect(wizardBackTarget("v3", "default", undefined)).toBe("/v3/create");
  });

  it("V2 keeps its own back", () => {
    expect(wizardBackTarget("v2", "k3x9", "a1")).toBeNull();
    expect(wizardBackTarget("v2", "default", undefined)).toBeNull();
  });
});

describe("pruneToCatalogs — a new agent's picks after a project switch", () => {
  const form = { name: "a", selectedKbs: ["KB-A", "KB-B"], skills: ["s3://x/skills/one/", "s3://x/skills/two/"] };

  it("keeps only the KBs and Skills the new project lists", () => {
    expect(pruneToCatalogs(form, { kbIds: ["KB-B", "KB-C"], skillPaths: ["s3://x/skills/two/"] })).toEqual({
      selectedKbs: ["KB-B"],
      skills: ["s3://x/skills/two/"],
    });
  });

  it("drops everything when the new project lists none, and leaves the rest of the form alone", () => {
    const out = pruneToCatalogs(form, { kbIds: [], skillPaths: [] });
    expect(out).toEqual({ selectedKbs: [], skills: [] });
    expect(Object.keys(out)).toEqual(["selectedKbs", "skills"]);
  });
});
