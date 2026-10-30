// Hermetic: the wizard option domains, their structural constraints and the oracle.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { test } = require("node:test") as typeof import("node:test");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const assert: typeof import("node:assert/strict") = require("node:assert/strict");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { allPairs, comboLabel, uncoveredPairs } = require("./pairwise.ts") as typeof import("./pairwise");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const dom = require("./wizard-domains.ts") as typeof import("./wizard-domains");

// eslint-disable-next-line @typescript-eslint/no-require-imports
const fs = require("node:fs") as typeof import("node:fs");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const path = require("node:path") as typeof import("node:path");

import type { Combo } from "./pairwise";
import type { AgentEnv, TaskEnv } from "./wizard-domains";

const ENV: AgentEnv = {
  gateways: ["hr-database", "office-facts"],
  remoteMcp: [],
  a2aPeers: [],
  builtinMethods: { "code-interpreter": ["harness"], browser: ["harness"] },
  unavailableModel: "siliconflow.zai-org/GLM-5.3",
};

const shown = (c: Combo, name: string) => c[name] !== "na";

test("agent factors: every generated row is UI-reachable and all feasible pairs are covered", () => {
  const factors = dom.agentFactors(ENV);
  const r = allPairs(factors, { seed: 16, constraint: dom.agentConstraint });
  assert.equal(r.infeasiblePairs.length, 0, r.infeasiblePairs.map((p) => `${p.a.join("=")} ${p.b.join("=")}`).join("; "));
  assert.equal(uncoveredPairs(factors, r.combos, dom.agentConstraint).length, 0);
  assert.ok(r.combos.length > 0 && r.combos.length < 120, `size ${r.combos.length}`);
  assert.ok(r.fullProductSize > r.combos.length * 10, `product ${r.fullProductSize} vs ${r.combos.length}`);
  for (const c of r.combos) {
    assert.ok(dom.agentConstraint(c), comboLabel(c));
    // the wizard shows the harness knobs only on harness, the protocol only on zip, byoc cards only on byoc
    assert.equal(shown(c, "maxTokens"), c.method === "harness", comboLabel(c));
    assert.equal(shown(c, "protocol"), c.method === "zip_runtime", comboLabel(c));
    assert.equal(shown(c, "byocKind"), c.method === "byoc", comboLabel(c));
    assert.equal(shown(c, "memory"), c.method !== "byoc", comboLabel(c));
    const zipHttp = c.method === "zip_runtime" && c.protocol !== "a2a";
    assert.equal(shown(c, "toolkit"), zipHttp, comboLabel(c));
    assert.equal(shown(c, "a2aSkills"), c.method === "zip_runtime" && c.protocol === "a2a", comboLabel(c));
    assert.equal(shown(c, "attach"), c.method === "harness" || zipHttp, comboLabel(c));
    assert.equal(shown(c, "identity"), c.method === "byoc" || zipHttp, comboLabel(c));
    if (c.method === "byoc") {
      const image = c.byocKind === "container_image";
      assert.equal(["ecr-in-account", "not-ecr"].includes(c.byocArtifact), image, comboLabel(c));
    }
  }
});

test("agent oracle: knob ranges, builtin catalog, byoc artifact rules, model availability", () => {
  const base: Combo = { method: "harness", path: "direct", builtin: "none", attach: "none", identity: "na", protocol: "na", memory: "lt-on", native: "default", model: "default", maxTokens: "empty", maxIter: "50", timeout: "300", toolkit: "na", a2aSkills: "na", fs: "on", byocKind: "na", byocArtifact: "na", byocModels: "na" };
  assert.deepEqual(dom.expectAgent(base, ENV), { ui: "allowed", uiReasons: [], server: "accept", serverReasons: [] });
  for (const [k, v] of [["maxTokens", "131073"], ["maxTokens", "0"], ["maxTokens", "abc"], ["maxIter", "0"], ["maxIter", "101"], ["timeout", "9"], ["timeout", "3601"], ["fs", "bad-mount"]] as const) {
    const e = dom.expectAgent({ ...base, [k]: v }, ENV);
    assert.equal(e.ui, "blocked", `${k}=${v}`);
    assert.equal(e.server, 422, `${k}=${v}`);
  }
  for (const [k, v] of [["maxTokens", "1"], ["maxTokens", "131072"], ["maxIter", "1"], ["maxIter", "100"], ["timeout", "10"], ["timeout", "3600"], ["builtin", "browser"], ["builtin", "both"], ["native", "none"], ["memory", "lt-off"], ["fs", "off"]] as const) {
    assert.equal(dom.expectAgent({ ...base, [k]: v }, ENV).ui, "allowed", `${k}=${v}`);
  }
  // a builtin the catalog does not list for harness → disabled in the UI, 422 on the server
  const noBrowser = { ...ENV, builtinMethods: { "code-interpreter": ["harness"], browser: [] } };
  const e = dom.expectAgent({ ...base, builtin: "browser" }, noBrowser);
  assert.deepEqual([e.ui, e.server], ["blocked", 422]);
  assert.ok(e.uiReasons.includes("builtin:browser:disabled"));
  // custom model id: the wizard lets it through, the server refuses (the "third outcome" the suite records)
  const custom = dom.expectAgent({ ...base, model: "custom-bogus" }, ENV);
  assert.deepEqual([custom.ui, custom.server], ["allowed", 422]);
  assert.equal(dom.expectAgent({ ...base, model: "unavailable" }, ENV).ui, "blocked");
  const byoc: Combo = { ...base, method: "byoc", builtin: "na", attach: "na", identity: "none", memory: "na", native: "na", maxTokens: "na", maxIter: "na", timeout: "na", fs: "na", byocKind: "code_zip", byocArtifact: "sample-zip", byocModels: "default" };
  assert.equal(dom.expectAgent(byoc, ENV).ui, "allowed");
  assert.equal(dom.expectAgent({ ...byoc, byocArtifact: "none" }, ENV).ui, "blocked");
  assert.equal(dom.expectAgent({ ...byoc, byocKind: "container_image", byocArtifact: "not-ecr" }, ENV).ui, "blocked");
  assert.equal(dom.expectAgent({ ...byoc, byocKind: "container_image", byocArtifact: "ecr-in-account" }, ENV).ui, "allowed");
  assert.equal(dom.expectAgent({ ...byoc, identity: "rest-open" }, ENV).ui, "blocked"); // byoc rows need a Connection
  const zip: Combo = { ...base, method: "zip_runtime", builtin: "na", native: "na", maxTokens: "na", maxIter: "na", timeout: "na", protocol: "http", identity: "rest-open", toolkit: "hr_assistant" };
  assert.equal(dom.expectAgent(zip, ENV).ui, "allowed");
  assert.deepEqual([dom.expectAgent({ ...zip, identity: "mcp-noconn" }, ENV).ui, dom.expectAgent({ ...zip, identity: "mcp-noconn" }, ENV).server], ["blocked", "accept"]);
});

test("api probes name a method, a rule and an expected status; ids are unique", () => {
  const ids = dom.AGENT_API_PROBES.map((p) => p.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const p of dom.AGENT_API_PROBES) {
    assert.ok(["harness", "zip_runtime", "byoc"].includes(p.method), p.id);
    assert.ok(p.rule.length > 10, p.id);
    assert.ok(p.expect === "accept" || [404, 409, 422].includes(p.expect), p.id);
  }
});

test("task factors: structural constraints hold on every generated row; pairs covered", () => {
  const factors = dom.taskFactors();
  const r = allPairs(factors, { seed: 16, constraint: dom.taskConstraint });
  assert.equal(r.infeasiblePairs.length, 0, r.infeasiblePairs.map((p) => `${p.a.join("=")} ${p.b.join("=")}`).join("; "));
  assert.equal(uncoveredPairs(factors, r.combos, dom.taskConstraint).length, 0);
  assert.ok(r.combos.length > 0 && r.combos.length < 80, `size ${r.combos.length}`);
  for (const c of r.combos) {
    assert.ok(dom.taskConstraint(c), comboLabel(c));
    if (c.target === "cloudwatch") assert.ok(!["sessions", "dataset"].includes(c.source) && c.strategy === "history", comboLabel(c));
    if (c.strategy === "continuous") assert.ok(c.source === "window" && c.mode === "evaluators" && c.sampling !== "na" && c.window === "na", comboLabel(c));
    else assert.equal(c.sampling, "na", comboLabel(c));
    assert.equal(shown(c, "dataset"), c.source === "dataset", comboLabel(c));
    assert.equal(shown(c, "picked"), c.source === "sessions" || c.source === "logs", comboLabel(c));
    assert.equal(shown(c, "evaluators"), c.mode === "evaluators", comboLabel(c));
    assert.equal(shown(c, "insights"), c.mode === "insights", comboLabel(c));
    assert.equal(shown(c, "window"), (c.source === "window" || c.source === "logs") && c.strategy !== "continuous", comboLabel(c));
  }
});

test("task oracle: empty pickers block, empty windows 409, ground truth without a dataset 422, sampling clamps", () => {
  const env: TaskEnv = { sessionsInWindow: (h) => (h >= 24 ? 3 : 0), gtDatasetHasGroundTruth: true };
  const base: Combo = { target: "agent", source: "window", mode: "evaluators", strategy: "history", window: "24", sampling: "na", evaluators: "two", insights: "na", dataset: "na", picked: "na" };
  assert.deepEqual(dom.expectTask(base, env).ui, "allowed");
  assert.equal(dom.expectTask(base, env).server, "accept");
  assert.deepEqual([dom.expectTask({ ...base, evaluators: "none" }, env).ui, dom.expectTask({ ...base, evaluators: "none" }, env).server], ["blocked", 422]);
  assert.equal(dom.expectTask({ ...base, window: "1" }, env).server, 409);
  assert.equal(dom.expectTask({ ...base, evaluators: "gt" }, env).server, 422);
  assert.equal(dom.expectTask({ ...base, source: "dataset", window: "na", dataset: "gt", evaluators: "gt" }, env).server, "accept");
  assert.equal(dom.expectTask({ ...base, source: "dataset", window: "na", dataset: "sim", evaluators: "gt" }, env).server, 422);
  const ins: Combo = { ...base, mode: "insights", evaluators: "na", insights: "all", source: "sessions", window: "na", picked: "one" };
  assert.ok(dom.expectTask(ins, env).uiReasons.includes("errInsightSessions"));
  assert.equal(dom.expectTask({ ...ins, picked: "three" }, env).ui, "allowed");
  assert.equal(dom.expectTask({ ...ins, insights: "none", picked: "three" }, env).server, 422);
  const cont: Combo = { ...base, strategy: "continuous", window: "na", sampling: "0" };
  assert.equal(dom.expectTask(cont, env).samplingSent, 10);
  assert.equal(dom.expectTask({ ...cont, sampling: "101" }, env).samplingSent, 100);
  assert.equal(dom.expectTask({ ...cont, sampling: "0.01" }, env).samplingSent, 0.01);
  assert.equal(dom.expectTask(cont, env).server, "accept");
});

test("oracle method sets equal the backend's frozen MethodCapabilities declaration", () => {
  // backend/tests/fixtures/method_capabilities.json is frozen by tests/test_method_capabilities.py;
  // a declaration that moves re-freezes it and this test names the oracle set that must follow
  const file = path.resolve(__dirname, "../../../../backend/tests/fixtures/method_capabilities.json");
  const fixture = JSON.parse(fs.readFileSync(file, "utf8")) as Record<string, { builtin_tools: string[]; tool_types: string[]; protocols: string[]; byoc_kinds: string[]; knowledge_bases: boolean; native_tools: string[] }>;
  // the V2 AgentWizard offers no `studio` method (studio agents come from the canvas page), so the
  // oracle pins harness / zip_runtime / byoc only; the declaration must still carry `studio`, and a
  // method that appears in the fixture without an oracle set is a drift this test must name
  assert.ok(Object.hasOwn(fixture, "studio"), "fixture lost its studio declaration");
  const unpinned = Object.keys(fixture).filter((m) => m !== "studio" && !(m in dom.ORACLE_METHOD_SETS));
  assert.deepEqual(unpinned, [], `methods declared without an oracle set: ${unpinned.join(", ")}`);
  for (const method of ["harness", "zip_runtime", "byoc"] as const) {
    const ours = dom.ORACLE_METHOD_SETS[method];
    const theirs = fixture[method];
    assert.ok(theirs, `${method} missing from the fixture`);
    for (const field of ["builtin_tools", "tool_types", "protocols", "byoc_kinds", "native_tools"] as const) {
      assert.deepEqual([...ours[field]].sort(), [...theirs[field]].sort(), `${method}.${field}`);
    }
    assert.equal(ours.knowledge_bases, theirs.knowledge_bases, `${method}.knowledge_bases`);
  }
  // the probes' rules follow the sets: a method without "rest" refuses rest tools, without "a2a" refuses the protocol
  assert.ok(!dom.ORACLE_METHOD_SETS.harness.tool_types.includes("rest"));
  assert.ok(!dom.ORACLE_METHOD_SETS.harness.protocols.includes("a2a"));
  assert.ok(!dom.ORACLE_METHOD_SETS.byoc.knowledge_bases);
});
