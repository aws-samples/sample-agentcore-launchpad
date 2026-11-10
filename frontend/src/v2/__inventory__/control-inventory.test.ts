// Round 16 (r16-m): the V2 control inventory is a committed snapshot of every interactive
// control the console renders — where it is, how a test reaches it (`data-testid`), what
// it should do and who may see it. This guard regenerates it from the TSX in-process and
// fails on drift, so a new or changed control has to be registered:
//
//     node scripts/control-inventory.mjs --write
//
// Controls no test can reach (no testId) or whose effect the generator cannot infer are
// findings in their own right ("untestable control"): they must be listed in
// `untestable.json` (append-only; each entry names its bugs-round-16 row), and nothing
// new may join that list silently.
import fs from "node:fs";

import { describe, expect, it } from "vitest";

import { CONTROLS_JSON, generate, semantic, UNTESTABLE_JSON, untestable, type Inventory, type UntestableEntry } from "../../../scripts/control-inventory.mjs";

const inv = generate();
const committed: Inventory | null = fs.existsSync(CONTROLS_JSON) ? (JSON.parse(fs.readFileSync(CONTROLS_JSON, "utf8")) as Inventory) : null;
const register: { floor?: number; entries: UntestableEntry[] } = fs.existsSync(UNTESTABLE_JSON)
  ? (JSON.parse(fs.readFileSync(UNTESTABLE_JSON, "utf8")) as { floor?: number; entries: UntestableEntry[] })
  : { entries: [] };

describe("V2 control inventory (src/v2/__inventory__/controls.json)", () => {
  it("is in sync with the TSX — regenerate with `node scripts/control-inventory.mjs --write`", () => {
    // compared without `line` (kept in the file for navigation): a pure reformat of a page is not drift;
    // ids are `file:testId` / `file:#ordinal` (source order), so they survive a reformat too
    const fresh = semantic(inv);
    const before = committed ? semantic(committed) : { count: 0, controls: [] };
    if (JSON.stringify(fresh) !== JSON.stringify(before)) {
      // name the first differing control so the failure is actionable
      const byId = new Map(before.controls.map((c) => [c.id, c]));
      const added = fresh.controls.filter((c) => !byId.has(c.id)).map((c) => c.id);
      const removed = before.controls.filter((c) => !fresh.controls.some((n) => n.id === c.id)).map((c) => c.id);
      const changed = fresh.controls
        .filter((c) => byId.has(c.id) && JSON.stringify(byId.get(c.id)) !== JSON.stringify(c))
        .map((c) => c.id);
      expect.fail(
        `control inventory drift: +${added.length} -${removed.length} ~${changed.length}\n` +
          `added: ${added.slice(0, 10).join(", ")}\nremoved: ${removed.slice(0, 10).join(", ")}\nchanged: ${changed.slice(0, 10).join(", ")}`,
      );
    }
  });

  it("covers every V2 page route from App.tsx", () => {
    const pages = new Set(inv.controls.map((c) => c.page));
    for (const route of [
      "/v2/",
      "/v2/agents",
      "/v2/assistant",
      "/v2/registry",
      "/v2/knowledge-bases",
      "/v2/chat",
      "/v2/memory",
      "/v2/connections",
      "/v2/governance",
      "/v2/observability",
      "/v2/eval/insights",
      "/v2/eval/data",
      "/v2/eval/tasks",
      "/v2/eval/online",
      "/v2/eval/evaluators",
      "/v2/eval/experiments",
      "/v2/skill-lab",
      "/v2/workspaces",
      "/v2/users",
      "/v2/settings/model-providers",
      "/v2/videos",
      "/v2/announcements",
    ]) {
      expect(pages, `no controls found under ${route}`).toContain(route);
    }
  });

  it("every control has a testId and an inferred effect, or is a registered untestable finding", () => {
    const known = new Set(register.entries.map((e) => e.id));
    const unregistered = untestable(inv).filter((u) => !known.has(u.id));
    expect(
      unregistered.map((u) => `${u.id} (${u.reason})`),
      "controls without a testId / inferred effect must be registered in untestable.json (append-only) — give the control a testId instead where you can",
    ).toEqual([]);
  });

  it("the untestable register is append-only and names a register row per entry", () => {
    const ids = new Set(inv.controls.map((c) => c.id));
    // `floor` is the largest entry count ever written by --write: deleting an entry fails here
    expect(register.floor, "untestable.json has no floor — regenerate with --write").toBeTypeOf("number");
    expect(register.entries.length, "entries were deleted from the append-only register (mark them resolved instead)").toBeGreaterThanOrEqual(register.floor ?? 0);
    for (const e of register.entries) {
      expect(e.register, `${e.id} has no bugs-round-16 row`).toMatch(/^N\d+-obs\d+/);
      // an entry may outlive its control (append-only), but one that still exists must still be untestable for the recorded reason
      const row = inv.controls.find((c) => c.id === e.id);
      if (row && row.testId && row.effect && !e.resolved) {
        expect.fail(`${e.id} is now testable (testId + effect) — keep the entry but mark it resolved: {"resolved": true}`);
      }
    }
    expect(ids.size).toBeGreaterThan(0);
  });

  it("admin-only pages expose admin-only controls; destructive controls are flagged", () => {
    for (const c of inv.controls) {
      // N17-obs203: /v2/settings/model-providers is every member's page now (owner-bound
      // providers); its admin-only controls are guarded per control, not per page
      if (["/v2/users", "/v2/workspaces"].includes(c.page)) {
        expect(c.roles, `${c.id} on an admin page`).toEqual(["admin"]);
      }
      if (c.effect?.type === "api" && c.effect.detail.startsWith("DELETE ")) {
        expect(c.destructive, `${c.id} issues a DELETE but is not flagged destructive`).toBe(true);
      }
    }
  });
});
