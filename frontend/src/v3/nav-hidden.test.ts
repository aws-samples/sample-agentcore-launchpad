import { describe, expect, it } from "vitest";

import {
  effectiveHidden,
  FULL_HIDDEN,
  HOME_SECTIONS,
  hideableGroups,
  hideableKeys,
  hostedGroups,
  isDefaultMenu,
  menuMode,
  NORMAL_HIDDEN,
  PINNED_HIDEABLE,
  withoutHidden,
  withoutHiddenPinned,
} from "./nav";

const tos = (groups: ReturnType<typeof hostedGroups>) => groups.flatMap((g) => g.items.map((i) => i.railKey));

/** Every key Menu settings offers (the set its save writes and its mode chip reads). */
const offered = hideableKeys;

describe("withoutHidden", () => {
  it("drops a hidden entry for a member by its rail key, even when the rail opens its V3 twin", () => {
    const member = hostedGroups(false);
    // /v2/skill-lab is listed at /v3/skill-lab: the key is the rail path, not the twin
    const skill = member.flatMap((g) => g.items).find((i) => i.railKey === "/v2/skill-lab");
    expect(skill?.to).toBe("/v3/skill-lab");
    const filtered = withoutHidden(member, new Set(["/v2/skill-lab"]));
    expect(tos(filtered)).not.toContain("/v2/skill-lab");
    expect(tos(filtered).length).toBe(tos(member).length - 1);
  });

  it("hides for administrators too", () => {
    const admin = hostedGroups(true);
    const filtered = withoutHidden(admin, new Set(["/v2/skill-lab", "/v2/eval/standards"]));
    expect(tos(filtered)).not.toContain("/v2/skill-lab");
    expect(tos(filtered)).not.toContain("/v2/eval/standards");
    expect(tos(filtered).length).toBe(tos(admin).length - 2);
  });

  it("never hides an administrator-only entry, even when its key is stored", () => {
    const admin = hostedGroups(true);
    const adminOnly = admin.flatMap((g) => g.items).filter((i) => i.admin).map((i) => i.railKey);
    expect(adminOnly).toContain("/v3/menu-settings");
    const filtered = withoutHidden(admin, new Set(adminOnly));
    expect(tos(filtered)).toEqual(tos(admin));
    expect(tos(filtered)).toContain("/v3/menu-settings");
  });

  it("drops a group whose every entry is hidden", () => {
    const member = hostedGroups(false);
    const group = member[member.length - 1];
    const filtered = withoutHidden(member, new Set(group.items.map((i) => i.railKey)));
    expect(filtered.map((g) => g.key)).not.toContain(group.key);
  });

  it("ignores keys that name no entry", () => {
    const member = hostedGroups(false);
    expect(withoutHidden(member, new Set(["/v2/nope"]))).toEqual(member);
    expect(withoutHidden(member, new Set([HOME_SECTIONS.launch, "/v3/gate"]))).toEqual(member);
  });
});

describe("withoutHiddenPinned", () => {
  const pinned = [{ to: "/v3" }, { to: "/v3/agents" }, { to: "/v3/chat" }, { to: "/v3/gate" }];

  it("drops the hidden pinned entries", () => {
    expect(withoutHiddenPinned(pinned, new Set(["/v3", "/v3/gate"])).map((i) => i.to)).toEqual(["/v3/agents", "/v3/chat"]);
    expect(withoutHiddenPinned(pinned, new Set(["/v3/gate"])).map((i) => i.to)).toEqual(["/v3", "/v3/agents", "/v3/chat"]);
  });

  it("never drops Agents or Chat, which are not offered", () => {
    expect(withoutHiddenPinned(pinned, new Set(["/v3/agents", "/v3/chat"]))).toEqual(pinned);
  });

  it("keeps everything for an empty list", () => {
    expect(withoutHiddenPinned(pinned, new Set())).toEqual(pinned);
  });

  it("never drops an administrator-only entry", () => {
    const items = [{ to: "/v3/gate", admin: true }];
    expect(withoutHiddenPinned(items, new Set(["/v3/gate"]))).toEqual(items);
  });
});

describe("hideableGroups", () => {
  it("offers only entries a member could see, and not the settings page itself", () => {
    const keys = tos(hideableGroups());
    expect(keys).toContain("/v2/skill-lab");
    expect(keys).not.toContain("/v3/menu-settings");
    expect(keys).not.toContain("/v3/models");
  });
});

describe("presets", () => {
  it("Normal is exactly the gate, criteria, environments, issues, releases and the two Command center sections", () => {
    expect([...NORMAL_HIDDEN]).toEqual([
      "/v2/environments", "/v2/eval/standards", "/v2/issues", "/v2/promotions",
      "/v3/gate", "/v3?section=launch", "/v3?section=turn",
    ]);
    expect(FULL_HIDDEN).toEqual([]);
    // Normal keeps the Command center itself
    expect(NORMAL_HIDDEN).not.toContain("/v3");
  });

  it("Menu settings offers the pinned entries, both sections and every module entry, no admin-only one", () => {
    const known = offered();
    for (const key of [...PINNED_HIDEABLE, HOME_SECTIONS.launch, HOME_SECTIONS.turn]) expect(known.has(key), key).toBe(true);
    for (const key of tos(hideableGroups())) expect(known.has(key), key).toBe(true);
    expect(known.has("/v3/agents")).toBe(false);
    expect(known.has("/v3/chat")).toBe(false);
    expect(known.has("/v3/menu-settings")).toBe(false);
  });

  it("every Normal key is offered by Menu settings", () => {
    const known = offered();
    for (const key of NORMAL_HIDDEN) expect(known.has(key), key).toBe(true);
  });

  it("every Normal key passes the backend's path shape", () => {
    const shape = /^\/v[23](\/[A-Za-z0-9_\-/]*)?(\?[A-Za-z0-9_\-=&]*)?$/;
    for (const key of [...NORMAL_HIDDEN, ...PINNED_HIDEABLE]) expect(shape.test(key), key).toBe(true);
  });
});

describe("menuMode", () => {
  const known = offered();

  it("reads Normal for exactly the Normal keys, in any order", () => {
    expect(menuMode(new Set([...NORMAL_HIDDEN].reverse()), known)).toBe("normal");
  });

  it("reads Full for an empty list", () => {
    expect(menuMode(new Set(), known)).toBe("full");
  });

  it("reads Custom for anything else", () => {
    expect(menuMode(new Set(["/v2/skill-lab"]), known)).toBe("custom");
    expect(menuMode(new Set([...NORMAL_HIDDEN, "/v3"]), known)).toBe("custom");
    expect(menuMode(new Set([HOME_SECTIONS.turn]), known)).toBe("custom");
  });

  it("ignores stale keys the page no longer offers", () => {
    expect(menuMode(new Set([...NORMAL_HIDDEN, "/v2/retired-module"]), known)).toBe("normal");
    expect(menuMode(new Set(["/v2/retired-module"]), known)).toBe("full");
  });
});

describe("effectiveHidden — Normal is the default mode", () => {
  it("applies the Normal preset while Menu settings was never saved", () => {
    const never = { hidden: [], updated_by: "", updated_at: null };
    expect(isDefaultMenu(never)).toBe(true);
    expect(effectiveHidden(never)).toEqual([...NORMAL_HIDDEN]);
    expect(effectiveHidden(never)).toContain("/v2/environments");
  });

  it("keeps a saved list exactly, an empty one being Full", () => {
    const savedFull = { hidden: [], updated_by: "admin", updated_at: "2026-10-25T14:00:00" };
    expect(isDefaultMenu(savedFull)).toBe(false);
    expect(effectiveHidden(savedFull)).toEqual([]);
    const custom = { hidden: ["/v2/eval/standards"], updated_by: "admin", updated_at: "2026-10-25T14:00:00" };
    expect(effectiveHidden(custom)).toEqual(["/v2/eval/standards"]);
  });

  it("hides nothing before the read answers or when it failed", () => {
    expect(isDefaultMenu(null)).toBe(false);
    expect(effectiveHidden(null)).toEqual([]);
    expect(effectiveHidden(undefined)).toEqual([]);
  });

  it("treats a steward's stand-in answer (updated_at set) as saved, not as the default", () => {
    expect(effectiveHidden({ hidden: [], updated_at: "" })).toEqual([]);
  });
});
