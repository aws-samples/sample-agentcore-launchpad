import { describe, expect, it } from "vitest";

import { V2_NAV } from "../v2/nav";
import { hostedGroups, isActive, V3_AFTER, v3TwinOf } from "./nav";

describe("hostedGroups", () => {
  it("reaches every V2 page V3 has not rebuilt", () => {
    const listed = new Set(
      hostedGroups(true).flatMap((g) => g.items.flatMap((i) => [i.to.split("?")[0], ...(i.also ?? [])])),
    );
    const missing = V2_NAV.flatMap((g) => g.items)
      .map((i) => i.to)
      // the overview, chat and the agent list are V3 pages at the top of the rail
      .filter((to) => !["/v2", "/v2/chat", "/v2/agents"].includes(to) && !listed.has(to));
    expect(missing).toEqual([]);
  });

  it("does not list a rebuilt page, and creating an agent opens the V2 wizard", () => {
    const items = hostedGroups(true).flatMap((g) => g.items);
    expect(items.map((i) => i.to)).not.toContain("/v2/chat");
    expect(items.find((i) => i.labelKey === "v3.nav.create")?.to).toBe("/v3/create");
  });

  it("lists the Agent IDE and the context layer for members in the build group", () => {
    const build = hostedGroups(false).find((g) => g.items.some((i) => i.to === "/v2/coding"));
    expect(build?.items.map((i) => i.to)).toEqual(expect.arrayContaining(["/v2/coding", "/v2/context-layer"]));
  });

  it("lists the V3-only Models page in the admin group, for administrators only", () => {
    expect(hostedGroups(true).find((g) => g.key === "admin")?.items.map((i) => i.to)).toContain("/v3/models");
    expect(hostedGroups(false).flatMap((g) => g.items.map((i) => i.to))).not.toContain("/v3/models");
  });

  it("places the people and structure pages right after Users (projects §7.9), for administrators only", () => {
    const admin = hostedGroups(true).find((g) => g.key === "admin")?.items.map((i) => i.to) ?? [];
    const at = admin.indexOf("/v3/users");
    expect(admin.slice(at, at + 4)).toEqual(["/v3/users", "/v3/admins", "/v3/projects", "/v3/departments"]);
    const member = hostedGroups(false).flatMap((g) => g.items.map((i) => i.to));
    expect(member.filter((to) => V3_AFTER["/v2/users"].some((i) => i.to === to))).toEqual([]);
  });

  it("shows Project settings to administrators and project.members holders only", () => {
    const tos = (admin: boolean, perms: string[] = []) => hostedGroups(admin, perms).flatMap((g) => g.items.map((i) => i.to));
    expect(tos(true)).toContain("/v3/project-settings");
    expect(tos(false, ["project.members"])).toContain("/v3/project-settings");
    expect(tos(false, ["project.use"])).not.toContain("/v3/project-settings");
    expect(tos(false, ["project.members"])).not.toContain("/v3/projects");
  });

  it("shows Projects, Project groups and Departments to a department administrator (§11.1)", () => {
    const tos = hostedGroups(false, [], true).flatMap((g) => g.items.map((i) => i.to));
    expect(tos).toEqual(expect.arrayContaining(["/v3/projects", "/v3/departments"]));
    expect(tos).not.toContain("/v3/users");
    expect(tos).not.toContain("/v3/admins");
    expect(tos).not.toContain("/v3/models");
  });

  it("lists the Gateway tools page for every member", () => {
    expect(hostedGroups(false).flatMap((g) => g.items.map((i) => i.to))).toContain("/v3/tools/gateway");
  });

  it("hides administrator pages from members", () => {
    const admin = V2_NAV.flatMap((g) => g.items).filter((i) => i.admin).map((i) => i.to);
    const member = hostedGroups(false).flatMap((g) => g.items.map((i) => i.to));
    expect(member.filter((to) => admin.includes(to))).toEqual([]);
  });
});

describe("isActive", () => {
  it("matches a sub-page entry only on that sub-page", () => {
    const create = { to: "/v2/agents?view=new" };
    expect(isActive(create, "/v2/agents", "?view=new")).toBe(true);
    expect(isActive(create, "/v2/agents", "?view=detail&id=1")).toBe(false);
  });

  it("matches a path and its children and the classic paths it owns", () => {
    const registry = { to: "/v2/registry", also: ["/registry"] };
    expect(isActive(registry, "/v2/registry", "")).toBe(true);
    expect(isActive(registry, "/registry", "?view=x")).toBe(true);
    expect(isActive(registry, "/v2/registry-x", "")).toBe(false);
  });
});

describe("v3TwinOf", () => {
  it("sends a rebuilt V2 page to its V3 page", () => {
    expect(v3TwinOf("/v2", "")).toBe("/v3");
    expect(v3TwinOf("/v2/chat", "?agent=a")).toBe("/v3/chat?agent=a");
    expect(v3TwinOf("/v2/registry", "")).toBe("/v3/registry");
    expect(v3TwinOf("/v2/registry", "?view=detail&id=r1")).toBe("/v3/registry?id=r1");
    expect(v3TwinOf("/v2/knowledge-bases", "?view=detail&id=kb1&tab=retrieve")).toBe("/v3/knowledge?id=kb1");
    expect(v3TwinOf("/v2/knowledge-bases", "?view=new")).toBeNull();
    expect(v3TwinOf("/v2/agents", "?view=new")).toBe("/v3/create");
    expect(v3TwinOf("/v2/assistant", "")).toBe("/v3/assistant");
    expect(v3TwinOf("/v2/promotions", "?view=detail&id=p1")).toBe("/v3/releases?id=p1");
    expect(v3TwinOf("/v2/costs", "?view=alerts")).toBe("/v3/costs?view=alerts");
    expect(v3TwinOf("/v2/observability", "?view=trace&id=t1&range=6h")).toBe("/v3/observability?view=trace&id=t1&range=6h");
    expect(v3TwinOf("/v2/issues", "?view=rules")).toBe("/v3/issues?view=rules");
    expect(v3TwinOf("/v2/assistant", "?view=detail&id=c1")).toBeNull();
    expect(v3TwinOf("/v2/agents", "?view=new&scenario=it")).toBeNull();
    expect(v3TwinOf("/v2/agents", "?view=detail&id=a")).toBe("/v3/agents?id=a");
    expect(v3TwinOf("/v2/agents", "?view=detail&id=a%20b")).toBe("/v3/agents?id=a%20b");
  });

  it("keeps the agent editor, identity page and the full detail page hosted (v3-agent-management §2.5)", () => {
    expect(v3TwinOf("/v2/agents", "?view=edit&id=a")).toBeNull();
    expect(v3TwinOf("/v2/agents", "?view=identity&id=a")).toBeNull();
    expect(v3TwinOf("/v2/agents", "?view=detail&id=a&full=1")).toBeNull();
    expect(v3TwinOf("/v2/agents", "?view=detail")).toBeNull();
    expect(v3TwinOf("/v2/agents", "")).toBeNull();
  });

  it("keeps the V2 sub-pages V3 hosts, and anything asked for in full", () => {
    expect(v3TwinOf("/v2/registry", "?view=register")).toBeNull();
    expect(v3TwinOf("/v2/registry", "?view=detail&id=r1&full=1")).toBeNull();
    expect(v3TwinOf("/v2/chat", "?agent=a&full=1")).toBeNull();
    expect(v3TwinOf("/v2/eval/tasks", "")).toBe("/v3/tasks");
    expect(v3TwinOf("/v2/eval/tasks", "?view=agent&key=a1")).toBe("/v3/tasks?view=agent&key=a1");
    expect(v3TwinOf("/v2/eval/tasks", "?view=compare&a=r1&b=r2&full=1")).toBeNull();
    expect(v3TwinOf("/v2/no-such-page", "")).toBeNull();
    expect(v3TwinOf("/v2/governance", "?tab=tools")).toBe("/v3/governance?tab=tools");
    expect(v3TwinOf("/v2/governance", "?view=policy&id=p")).toBeNull();
  });

  it("lands 评估任务's tabs and the former 在线评估 page on /v3/tasks", () => {
    expect(v3TwinOf("/v2/eval/tasks", "?tab=online")).toBe("/v3/tasks?tab=online");
    expect(v3TwinOf("/v2/eval/tasks", "?view=new&strategy=continuous")).toBe("/v3/tasks?view=new&strategy=continuous");
    expect(v3TwinOf("/v2/eval/tasks", "?view=edit&kind=online&id=c1")).toBe("/v3/tasks?view=edit&kind=online&id=c1");
    expect(v3TwinOf("/v2/eval/online", "")).toBe("/v3/tasks?tab=online");
    expect(v3TwinOf("/v2/eval/online", "?view=new")).toBe("/v3/tasks?view=new&strategy=continuous");
    expect(v3TwinOf("/v2/eval/online", "?view=detail&id=c1")).toBe("/v3/tasks?view=detail&kind=online&id=c1");
    expect(v3TwinOf("/v2/eval/online", "?view=edit&id=c1")).toBe("/v3/tasks?view=edit&kind=online&id=c1");
  });

  it("lists a rebuilt module at its V3 page, lit on its hosted sub-pages", () => {
    const registry = hostedGroups(true).flatMap((g) => g.items).find((i) => i.to === "/v3/registry");
    expect(registry).toBeDefined();
    expect(isActive(registry!, "/v2/registry", "?view=register")).toBe(true);
  });
});
