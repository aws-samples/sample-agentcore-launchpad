// Hermetic: the random walk's click policy, effect detection and URL rules.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { test } = require("node:test") as typeof import("node:test");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const assert: typeof import("node:assert/strict") = require("node:assert/strict");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const wm = require("./walk-model.ts") as typeof import("./walk-model");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { rng } = require("./pairwise.ts") as typeof import("./pairwise");

import type { Control, Snapshot } from "./walk-model";

const ctl = (over: Partial<Control>): Control => ({ id: "x", kind: "button", text: "View", rowText: "", href: null, disabled: false, inDialog: false, ...over });

test("click policy: destructive only on fixture rows, never sign-out / deploy, never disabled, stay in module", () => {
  assert.equal(wm.isClickable(ctl({ text: "Delete", rowText: "x16n-c12-harness active" }), "/v2/agents").ok, true);
  assert.equal(wm.isClickable(ctl({ text: "Delete", rowText: "launchpad-eval-worker active" }), "/v2/agents").ok, false);
  assert.equal(wm.isClickable(ctl({ text: "删除", rowText: "x16n-t3" }), "/v2/eval/tasks").ok, true);
  assert.equal(wm.isClickable(ctl({ text: "Stop", rowText: "e2es-batch" }), "/v2/eval/tasks").ok, false);
  assert.equal(wm.isClickable(ctl({ text: "Sign out", rowText: "x16n-" }), "/v2/agents").ok, false);
  assert.equal(wm.isClickable(ctl({ text: "Deploy", rowText: "x16n-" }), "/v2/agents").ok, false);
  assert.equal(wm.isClickable(ctl({ text: "Redeploy", rowText: "x16n-" }), "/v2/agents").ok, false);
  assert.equal(wm.isClickable(ctl({ text: "View", disabled: true }), "/v2/agents").ok, false);
  assert.equal(wm.isClickable(ctl({ kind: "link", text: "Chat", href: "/v2/chat?agent=1" }), "/v2/agents").why, "leaves module");
  assert.equal(wm.isClickable(ctl({ kind: "link", text: "detail", href: "/v2/agents?view=detail&id=1" }), "/v2/agents").ok, true);
  assert.equal(wm.isClickable(ctl({ kind: "link", text: "docs", href: "https://example.invalid/docs" }), "/v2/agents").why, "external link");
  assert.equal(wm.isClickable(ctl({ text: "Confirm", inDialog: true, rowText: "Delete launchpad-eval-worker?" }), "/v2/agents").ok, false);
  assert.equal(wm.isClickable(ctl({ text: "Confirm", inDialog: true, rowText: "Delete x16n-c1?" }), "/v2/agents").ok, true);
  assert.equal(wm.isClickable(ctl({ text: "Refresh" }), "/v2/agents").ok, true);
  assert.equal(wm.isClickable(ctl({ text: "中文" }), "/v2/agents").ok, false);
  assert.equal(wm.isClickable(ctl({ kind: "tab", text: "All", alreadyOn: true }), "/v2/agents").why, "already selected");
  assert.ok(wm.NO_EFFECT_OK_RE.test("Export CSV"));
});

test("effects: url / text / dom / network / dialog; refresh may legitimately change nothing", () => {
  const a: Snapshot = { url: "u", textHash: "h", elementCount: 10, requests: 3, openDialogs: 0 };
  assert.deepEqual(wm.effectOf(a, { ...a }), []);
  assert.deepEqual(wm.effectOf(a, { ...a, url: "v" }), ["url"]);
  assert.deepEqual(wm.effectOf(a, { ...a, textHash: "x", requests: 4 }), ["text", "network"]);
  assert.deepEqual(wm.effectOf(a, { ...a, openDialogs: 1 }), ["dialog"]);
  assert.ok(wm.NO_EFFECT_OK_RE.test("Refresh"));
  assert.ok(!wm.NO_EFFECT_OK_RE.test("Delete"));
});

test("URL rules select by path and param value", () => {
  const r = wm.rulesFor("https://h/v2/eval/tasks?view=new");
  assert.equal(r.length, 1);
  assert.ok(r[0].mustSee.includes("v2-task-name"));
  assert.equal(wm.rulesFor("https://h/v2/eval/tasks?tab=all").length, 1);
  assert.equal(wm.rulesFor("https://h/v2/eval/tasks").length, 0);
  assert.equal(wm.rulesFor("https://h/v2/memory?tab=long-term")[0].mustSee, '[data-testid="v2-tab-long-term"][aria-selected="true"]');
  assert.equal(wm.rulesFor("https://h/v2/elsewhere?tab=x").length, 0);
});

test("chooseControl is seeded, prefers unseen controls, never returns a control outside the list", () => {
  const controls = ["a", "b", "c", "d"].map((id) => ctl({ id, text: id }));
  const seen = new Set(["a", "b", "c"]);
  let fresh = 0;
  const random = rng(5);
  for (let i = 0; i < 200; i += 1) {
    const c = wm.chooseControl(random, controls, seen)!;
    assert.ok(controls.includes(c));
    if (c.id === "d") fresh += 1;
  }
  assert.ok(fresh > 120, `fresh picks ${fresh}`); // ~70 % + 1/4 of the rest
  assert.equal(wm.chooseControl(rng(1), [], seen), null);
  const x = Array.from({ length: 20 }, () => wm.chooseControl(rng(9), controls, new Set())!.id);
  assert.equal(new Set(x).size, 1, "same seed, same first pick");
});
