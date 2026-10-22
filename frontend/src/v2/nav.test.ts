import { describe, expect, it } from "vitest";

import { activeNavItem, navGroupsFor, V2_NAV, V2_PALETTE_ALIASES } from "./nav";

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

  it("does not mutate V2_NAV", () => {
    const before = paths(V2_NAV);
    navGroupsFor("business", false);
    expect(paths(V2_NAV)).toEqual(before);
  });

  it("lists the coding console as an expert-only Build entry for everyone", () => {
    const build = V2_NAV.find((g) => g.key === "build");
    const item = build?.items.find((entry) => entry.to === "/v2/coding");
    expect(item).toMatchObject({ v2: true, labelKey: "v2.nav.coding" });
    expect(item?.admin).toBeUndefined();
    expect(item?.business).toBeUndefined();
    expect(paths(navGroupsFor("expert", false))).toContain("/v2/coding");
    expect(paths(navGroupsFor("business", false))).not.toContain("/v2/coding");
  });
});

describe("评估任务 took over 在线评估", () => {
  const items = V2_NAV.flatMap((g) => g.items);
  const tasks = items.find((entry) => entry.to === "/v2/eval/tasks");
  const videoOf = (search: string) => {
    const item = activeNavItem("/v2/eval/tasks", search);
    return item?.videoPath?.(new URLSearchParams(search)) ?? item?.to;
  };

  it("has no 在线评估 sidebar entry; 评估任务 stays in the eval group", () => {
    expect(items.some((entry) => entry.to.startsWith("/v2/eval/online"))).toBe(false);
    expect(items.some((entry) => entry.labelKey === "v2.nav.online")).toBe(false);
    expect(V2_NAV.find((g) => g.key === "eval")?.items.map((i) => i.to)).toContain("/v2/eval/tasks");
  });

  it("plays the 在线评估 videos on the 持续评估 tab and the online sub-pages", () => {
    expect(videoOf("?tab=online")).toBe("/v2/eval/online");
    expect(videoOf("?view=detail&kind=online&id=c1")).toBe("/v2/eval/online");
    expect(videoOf("?view=edit&kind=online&id=c1")).toBe("/v2/eval/online");
    expect(videoOf("?view=new&strategy=continuous")).toBe("/v2/eval/online");
    expect(videoOf("?view=new&from=online:c1")).toBe("/v2/eval/online");
  });

  it("plays the 评估任务 videos everywhere else", () => {
    for (const search of ["", "?tab=runs", "?tab=all", "?view=new", "?view=new&from=run:r1", "?view=detail&kind=run&id=r1"]) {
      expect(videoOf(search)).toBe("/v2/eval/tasks");
    }
    expect(tasks?.videoPath?.(new URLSearchParams("view=agent&key=a1"))).toBeNull();
  });

  it("keeps the palette aliases out of the sidebar", () => {
    expect(V2_PALETTE_ALIASES.map((a) => a.to)).toEqual(["/v2/eval/tasks?tab=online"]);
    for (const alias of V2_PALETTE_ALIASES) expect(items).not.toContain(alias);
    // the alias's location belongs to the 评估任务 entry
    expect(activeNavItem("/v2/eval/tasks", "?tab=online")).toBe(tasks);
  });
});
