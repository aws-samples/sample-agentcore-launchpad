import { describe, expect, it } from "vitest";

import { navGroupsFor, V2_NAV } from "./nav";

const BUSINESS = [
  "/v2",
  "/v2/assistant",
  "/v2/agents",
  "/v2/knowledge-bases",
  "/v2/chat",
  "/v2/observability",
  "/v2/videos",
];

/** The sidebar as V2Shell rendered it before nav modes existed. */
function legacySidebar(isAdmin: boolean) {
  return V2_NAV.map((group) => ({
    ...group,
    items: group.items.filter((item) => !item.admin || isAdmin),
  })).filter((group) => group.items.length > 0);
}

const paths = (groups: ReturnType<typeof navGroupsFor>) =>
  groups.flatMap((group) => group.items.map((item) => item.to));

describe("navGroupsFor", () => {
  it("expert mode is the sidebar as before, for an admin and a member", () => {
    expect(navGroupsFor("expert", true)).toEqual(legacySidebar(true));
    expect(navGroupsFor("expert", false)).toEqual(legacySidebar(false));
    expect(navGroupsFor("expert", true).map((g) => g.key)).toEqual(
      ["home", "build", "run", "eval", "learn", "admin"],
    );
  });

  it("expert mode hides admin entries from a member", () => {
    const groups = navGroupsFor("expert", false);
    expect(groups.map((g) => g.key)).not.toContain("admin");
    expect(groups.flatMap((g) => g.items).some((item) => item.admin)).toBe(false);
  });

  it("business mode keeps exactly the seven essentials, in order", () => {
    expect(paths(navGroupsFor("business", true))).toEqual(BUSINESS);
    expect(paths(navGroupsFor("business", false))).toEqual(BUSINESS);
  });

  it("business mode drops the groups it empties", () => {
    for (const isAdmin of [true, false]) {
      const groups = navGroupsFor("business", isAdmin);
      expect(groups.map((g) => g.key)).toEqual(["home", "build", "run", "learn"]);
      expect(groups.every((g) => g.items.length > 0)).toBe(true);
    }
  });

  it("marks only the seven essentials as business", () => {
    const marked = V2_NAV.flatMap((g) => g.items)
      .filter((item) => item.business)
      .map((item) => item.to);
    expect(marked).toEqual(BUSINESS);
  });

  it("lists Spend & alerts as an admin-only entry", () => {
    const item = V2_NAV.flatMap((g) => g.items).find((entry) => entry.to === "/v2/costs");
    expect(item?.admin).toBe(true);
    expect(paths(navGroupsFor("expert", true))).toContain("/v2/costs");
    expect(paths(navGroupsFor("expert", false))).not.toContain("/v2/costs");
  });

  it("lists Environments for everyone, in the Run group after Observability", () => {
    const run = V2_NAV.find((g) => g.key === "run")?.items.map((item) => item.to) ?? [];
    expect(run.indexOf("/v2/environments")).toBe(run.indexOf("/v2/observability") + 1);
    const item = V2_NAV.flatMap((g) => g.items).find((entry) => entry.to === "/v2/environments");
    expect(item?.admin).toBeUndefined();
    expect(paths(navGroupsFor("expert", false))).toContain("/v2/environments");
  });

  it("does not mutate V2_NAV", () => {
    const before = paths(V2_NAV);
    navGroupsFor("business", false);
    expect(paths(V2_NAV)).toEqual(before);
  });
});
