// Hermetic: the six page factor tables, their constraints and the URL each row promises.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { test } = require("node:test") as typeof import("node:test");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const assert: typeof import("node:assert/strict") = require("node:assert/strict");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { allPairs, comboLabel, uncoveredPairs } = require("./pairwise.ts") as typeof import("./pairwise");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const pf = require("./page-factors.ts") as typeof import("./page-factors");

// a type-only import keeps this a module for tsc (no global collisions) and vanishes at run time
import type { PageKey } from "./page-factors";

test("every page has role × page × filters; all pairs covered; structural na-rules hold", () => {
  for (const key of pf.PAGES as PageKey[]) {
    const factors = pf.PAGE_FACTORS[key];
    const names = factors.map((f) => f.name);
    assert.ok(names.includes("role") && names.includes("page"), key);
    assert.equal(new Set(names).size, names.length, `${key}: duplicate factor`);
    const r = allPairs(factors, { seed: 16, constraint: pf.PAGE_CONSTRAINTS[key] });
    assert.equal(r.infeasiblePairs.length, 0, key);
    assert.equal(uncoveredPairs(factors, r.combos, pf.PAGE_CONSTRAINTS[key]).length, 0, key);
    assert.ok(r.combos.length >= 6 && r.combos.length <= 60, `${key}: ${r.combos.length} rows`);
    assert.ok(r.combos.length < r.fullProductSize, key);
    for (const c of r.combos) {
      assert.ok(pf.PAGE_CONSTRAINTS[key](c), `${key} ${comboLabel(c)}`);
      const url = pf.pagePath(key, c, { agentKey: "k1" });
      assert.ok(url.startsWith("/v2/"), url);
      if (key === "tasks") assert.equal(c.status !== "na", c.tab === "all", comboLabel(c));
      if (key === "data") {
        assert.equal(c.range !== "na", c.tab === "traces", comboLabel(c));
        assert.equal(c.kind !== "na", c.tab === "datasets", comboLabel(c));
      }
      if (key === "registry") assert.equal(c.status !== "na", c.tab === "records", comboLabel(c));
      if (key === "users") {
        assert.equal(c.status !== "na", c.role === "admin", comboLabel(c));
        if (c.role === "member") assert.ok(c.q === "" && c.page === "1", comboLabel(c));
      }
    }
  }
});

test("promised URL params follow the source: tab / range / type / users filters; nothing for agents", () => {
  assert.deepEqual(pf.promisedParams("tasks", { tab: "all" }), { tab: "all" });
  assert.deepEqual(pf.promisedParams("tasks", { tab: "agents" }), { tab: null });
  assert.deepEqual(pf.promisedParams("data", { tab: "traces", range: "6h" }), { tab: "traces", range: "6h" });
  assert.deepEqual(pf.promisedParams("data", { tab: "datasets" }), { tab: "datasets" });
  assert.deepEqual(pf.promisedParams("registry", { tab: "records", type: "MCP" }), { type: "MCP" });
  assert.deepEqual(pf.promisedParams("registry", { tab: "records", type: "all" }), {});
  assert.deepEqual(pf.promisedParams("registry", { tab: "discoverable", type: "MCP" }), { view: "discoverable" });
  assert.deepEqual(pf.promisedParams("agents", { method: "harness" }), {});
  assert.deepEqual(pf.promisedParams("users", { role: "admin", status: "active", q: "x16n" }), { status: "active", q: "x16n" });
  assert.deepEqual(pf.promisedParams("users", { role: "admin", status: "all", q: "" }), { status: null, q: null });
  assert.deepEqual(pf.promisedParams("users", { role: "member", status: "na", q: "" }), {});
  assert.equal(pf.pagePath("scorecard", {}, { agentKey: "a b" }), "/v2/eval/tasks?view=agent&key=a%20b");
  assert.equal(pf.pagePath("data", { tab: "traces", range: "7d" }), "/v2/eval/data?tab=traces&range=7d");
});
