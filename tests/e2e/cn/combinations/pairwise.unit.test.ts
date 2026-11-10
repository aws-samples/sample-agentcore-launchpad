// node --test cn/combinations/*.unit.test.ts  (Node >= 22.6 strips the types; `make verify`
// runs it through scripts/verify.sh's "e2e · combination generators" section)
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { test } = require("node:test") as typeof import("node:test");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const assert: typeof import("node:assert/strict") = require("node:assert/strict");

import type { Combo, Factor } from "./pairwise";

// explicit extension: Node strips the types in place (same pattern as cn/log-export-core.unit.test.ts)
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { allPairs, comboLabel, feasiblePairs, fullProductSize, rng, uncoveredPairs } = require("./pairwise.ts") as typeof import("./pairwise");

const F: Factor[] = [
  { name: "a", values: ["a1", "a2", "a3"] },
  { name: "b", values: ["b1", "b2"] },
  { name: "c", values: ["c1", "c2", "c3", "c4"] },
  { name: "d", values: ["d1", "d2"] },
];

test("allPairs covers every pair of an unconstrained domain", () => {
  const r = allPairs(F, { seed: 7 });
  assert.equal(uncoveredPairs(F, r.combos).length, 0);
  assert.equal(r.infeasiblePairs.length, 0);
  assert.equal(r.fullProductSize, 3 * 2 * 4 * 2);
  assert.equal(r.pairCount, feasiblePairs(F).length);
  // the largest two domains bound the suite from below; greedy stays close to it
  assert.ok(r.combos.length >= 12 && r.combos.length <= 16, `size ${r.combos.length}`);
  for (const combo of r.combos) assert.deepEqual(Object.keys(combo).sort(), ["a", "b", "c", "d"]);
});

test("allPairs is deterministic for a seed and differs across seeds", () => {
  const x = allPairs(F, { seed: 3 });
  const y = allPairs(F, { seed: 3 });
  assert.deepEqual(x.combos, y.combos);
  const z = allPairs(F, { seed: 4 });
  assert.equal(uncoveredPairs(F, z.combos).length, 0);
});

test("allPairs honours a monotone constraint and never emits an illegal row", () => {
  // d2 only with a1 (think: protocol applies to one method only)
  const constraint = (p: Combo) => !(p.d === "d2" && p.a !== undefined && p.a !== "a1");
  const r = allPairs(F, { seed: 1, constraint });
  for (const combo of r.combos) assert.ok(constraint(combo), comboLabel(combo));
  assert.equal(uncoveredPairs(F, r.combos, constraint).length, 0);
  assert.equal(r.infeasiblePairs.length, 0);
  assert.equal(r.fullProductSize, fullProductSize(F, constraint));
  // product shrank: rows with d2 exist only for a1
  assert.equal(r.fullProductSize, 3 * 2 * 4 * 1 + 1 * 2 * 4 * 1);
});

test("allPairs excludes pairs that pass alone but cannot complete into a legal row", () => {
  // (b2, c1) passes as a bare pair, yet every row needs a value of `a` and b2+c1 forbids all
  const constraint = (p: Combo) => !(p.b === "b2" && p.c === "c1" && p.a !== undefined);
  const r = allPairs(F, { seed: 2, constraint });
  const label = (p: { a: [string, string]; b: [string, string] }) => comboLabel({ [p.a[0]]: p.a[1], [p.b[0]]: p.b[1] });
  assert.ok(!feasiblePairs(F, constraint).some((p) => label(p) === "b=b2 c=c1"));
  assert.equal(r.infeasiblePairs.length, 0);
  assert.equal(uncoveredPairs(F, r.combos, constraint).length, 0);
  for (const combo of r.combos) assert.ok(constraint(combo));
  assert.equal(r.fullProductSize, 3 * 2 * 4 * 2 - 3 * 2);
});

test("rng is a stable uniform-ish sequence", () => {
  const r1 = rng(42);
  const r2 = rng(42);
  const xs = Array.from({ length: 1000 }, () => r1());
  const ys = Array.from({ length: 1000 }, () => r2());
  assert.deepEqual(xs, ys);
  assert.ok(xs.every((v) => v >= 0 && v < 1));
  const mean = xs.reduce((s, v) => s + v, 0) / xs.length;
  assert.ok(Math.abs(mean - 0.5) < 0.05, `mean ${mean}`);
});

test("comboLabel is key-sorted and stable", () => {
  assert.equal(comboLabel({ b: "2", a: "1" }), "a=1 b=2");
});
