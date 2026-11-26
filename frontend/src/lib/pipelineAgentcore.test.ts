import { describe, expect, it } from "vitest";

import type { AgentInfo, ObsSessionRow, V2Dataset, V2PipelineSource } from "./api";
import type { EvaluationRunInfo, EvaluationRunResultRow, EvaluationRunResults } from "./evaluation";
import {
  cleanAgentcoreProcessing,
  cleanAgentcoreSource,
  cleanRedaction,
  generationAgents,
  generationRuns,
  lowestNormalized,
  previewRunSessions,
  sessionsFromOf,
  previewAgentcoreSessions,
  receivableDatasets,
  redactionProblem,
} from "./pipelineAgentcore";

const NOW = Date.parse("2026-11-26T12:00:00Z");
const minsAgo = (m: number) => new Date(NOW - m * 60_000).toISOString();

const row = (id: string, patch: Partial<ObsSessionRow> = {}): ObsSessionRow => ({
  session_id: id,
  service: "svc.DEFAULT",
  agent: "hr-bot",
  traces: 1,
  llm_calls: 1,
  errors: 0,
  tokens: { input: 0, output: 0, total: 0 },
  est_cost_usd: null,
  first: minsAgo(60),
  last: minsAgo(30),
  platform: true,
  ...patch,
});

const src = (patch: Partial<V2PipelineSource> = {}): V2PipelineSource => ({
  type: "agentcore",
  agent: null,
  agent_id: "a1",
  range: "24h",
  status: "all",
  max_sessions: 20,
  scenario_type: "FIXED",
  expected_trajectory: true,
  redaction: null,
  ...patch,
});

describe("generationAgents", () => {
  it("keeps active agents of eval-supported methods, by name", () => {
    const a = (name: string, method: AgentInfo["method"], status: AgentInfo["status"] = "active") => ({ id: name, name, method, status }) as AgentInfo;
    const out = generationAgents([a("zeta", "harness"), a("disc", "discovered_runtime"), a("alpha", "zip_runtime"), a("gone", "container", "deleted"), a("box", "byoc")]);
    expect(out.map((x) => x.name)).toEqual(["alpha", "box", "zeta"]);
  });
});

describe("previewAgentcoreSessions", () => {
  it("keeps only the agent's own sessions and applies the status filter", () => {
    const rows = [row("mine"), row("other", { agent: "else" }), row("bad", { errors: 2 })];
    expect(previewAgentcoreSessions(rows, src(), "hr-bot", NOW).picked.map((s) => s.session_id)).toEqual(["mine", "bad"]);
    expect(previewAgentcoreSessions(rows, src({ status: "error" }), "hr-bot", NOW).picked.map((s) => s.session_id)).toEqual(["bad"]);
    expect(previewAgentcoreSessions(rows, src({ status: "ok" }), "hr-bot", NOW).picked.map((s) => s.session_id)).toEqual(["mine"]);
  });

  it("holds back sessions active in the last 10 minutes (and ones with no timestamp)", () => {
    const rows = [row("old", { last: minsAgo(11) }), row("hot", { last: minsAgo(3) }), row("unknown", { last: null })];
    const out = previewAgentcoreSessions(rows, src(), "hr-bot", NOW);
    expect(out.picked.map((s) => s.session_id)).toEqual(["old"]);
    expect(out.fresh.map((s) => s.session_id)).toEqual(["hot", "unknown"]);
  });

  it("DYNAMIC keeps multi-turn sessions only and counts the rest", () => {
    const rows = [row("one"), row("two", { traces: 2 }), row("three", { traces: 3 })];
    const out = previewAgentcoreSessions(rows, src({ scenario_type: "DYNAMIC" }), "hr-bot", NOW);
    expect(out.picked.map((s) => s.session_id)).toEqual(["two", "three"]);
    expect(out.singleTurn).toBe(1);
  });

  it("takes the newest max_sessions, capped at 25", () => {
    const rows = Array.from({ length: 30 }, (_, i) => row(`s${i}`, { last: minsAgo(20 + i) }));
    const three = previewAgentcoreSessions(rows, src({ max_sessions: 3 }), "hr-bot", NOW);
    expect(three.picked.map((s) => s.session_id)).toEqual(["s0", "s1", "s2"]);
    expect(three.overflow).toBe(27);
    expect(previewAgentcoreSessions(rows, src({ max_sessions: 50 }), "hr-bot", NOW).picked).toHaveLength(25);
  });

  it("previews nothing until an agent is picked", () => {
    expect(previewAgentcoreSessions([row("x")], src(), null, NOW).picked).toEqual([]);
  });
});

describe("receivableDatasets", () => {
  const ds = (id: string, kind: string) => ({ id, kind }) as V2Dataset;
  const all = [ds("p", "predefined"), ds("s", "simulated"), ds("l", "legacy")];
  it("FIXED → predefined, DYNAMIC → simulated, never legacy", () => {
    expect(receivableDatasets(all, src()).map((d) => d.id)).toEqual(["p"]);
    expect(receivableDatasets(all, src({ scenario_type: "DYNAMIC" })).map((d) => d.id)).toEqual(["s"]);
  });
  it("leaves the local extractors' rule alone", () => {
    expect(receivableDatasets(all, src({ type: "traces" })).map((d) => d.id)).toEqual(["p", "l"]);
  });
});

describe("cleanRedaction / redactionProblem", () => {
  it("is null when nothing is picked; drops blank custom rows; dedupes ids in curated order", () => {
    expect(cleanRedaction(null)).toBeNull();
    expect(cleanRedaction({ identifiers: [], custom: [{ name: " ", regex: "" }] })).toBeNull();
    expect(cleanRedaction({ identifiers: ["NAME", "EMAIL_ADDRESS", "NAME"], custom: [{ name: " ORDER_ID ", regex: " ORD-[0-9]+ " }, { name: "", regex: "" }] })).toEqual({
      identifiers: ["EMAIL_ADDRESS", "NAME"],
      custom: [{ name: "ORDER_ID", regex: "ORD-[0-9]+" }],
    });
  });

  it("refuses bad custom identifiers", () => {
    const one = (name: string, regex: string) => redactionProblem({ identifiers: [], custom: [{ name, regex }] });
    expect(one("ORDER_ID", "ORD-[0-9]+")).toBeNull();
    expect(one("order id", "x")).toBe("customName");
    expect(one("ORDER_ID", "")).toBe("customRegex");
    expect(one("ORDER_ID", "x".repeat(201))).toBe("customRegex");
    expect(one("ORDER_ID", "(")).toBe("customRegex");
    expect(one("ORDER_ID", "foo(?=bar)")).toBe("customLookaround");
    expect(one("ORDER_ID", "(?<!a)b")).toBe("customLookaround");
    expect(redactionProblem({ identifiers: [], custom: [{ name: "A", regex: "a" }, { name: "A", regex: "b" }] })).toBe("customDuplicate");
    expect(redactionProblem({ identifiers: [], custom: Array.from({ length: 11 }, (_, i) => ({ name: `N${i}`, regex: "x" })) })).toBe("customTooMany");
  });
});

describe("cleanAgentcoreSource / cleanAgentcoreProcessing", () => {
  it("sends only agentcore fields, clamps the cap, carries the agent name for display", () => {
    const out = cleanAgentcoreSource({ ...src({ max_sessions: 40 }), log_groups: ["/x"], keyword: "k", format: null }, "hr-bot");
    expect(out).toEqual({
      batch_run_id: null,
      max_score: 0.7,
      type: "agentcore",
      agent: "hr-bot",
      agent_id: "a1",
      range: "24h",
      status: "all",
      max_sessions: 25,
      scenario_type: "FIXED",
      expected_trajectory: true,
      redaction: null,
    });
  });

  it("turns ground truth off for DYNAMIC", () => {
    expect(cleanAgentcoreSource(src({ scenario_type: "DYNAMIC", expected_trajectory: true }), "hr-bot").expected_trajectory).toBe(false);
    const proc = { first_turn_only: true, min_input_chars: 40, dedupe: true, keep_replies: true };
    expect(cleanAgentcoreProcessing(proc, "DYNAMIC")).toEqual({ first_turn_only: false, min_input_chars: 0, dedupe: true, keep_replies: false });
    expect(cleanAgentcoreProcessing(proc, "FIXED")).toEqual({ first_turn_only: false, min_input_chars: 0, dedupe: true, keep_replies: true });
  });

  it("defaults a missing scenario type to FIXED with trajectory on", () => {
    const out = cleanAgentcoreSource(src({ scenario_type: undefined, expected_trajectory: undefined }), null);
    expect(out.scenario_type).toBe("FIXED");
    expect(out.expected_trajectory).toBe(true);
  });
});

describe("run-sourced generation (P3)", () => {
  const res = (evaluator_id: string, score: number | null): EvaluationRunResultRow => ({
    evaluator_id, level: "SESSION", score, label: null, explanation: null, error_type: score == null ? "X" : null, error_message: null,
  });
  const results = (sessions: Record<string, EvaluationRunResultRow[]>): EvaluationRunResults => ({
    run_id: "r1", batch_eval_id: "b1", available: true, count: Object.keys(sessions).length, truncated: false,
    sessions: Object.entries(sessions).map(([session_id, rows]) => ({ session_id, results: rows })) as EvaluationRunResults["sessions"],
  });

  it("lowestNormalized inverts lower-is-better evaluators and ignores failed judgements", () => {
    expect(lowestNormalized([res("Builtin.Helpfulness", 0.9), res("Builtin.Correctness", 0.6)])).toBeCloseTo(0.6);
    expect(lowestNormalized([res("Builtin.Refusal", 0.8)])).toBeCloseTo(0.2); // penalty evaluator: 1 - score
    expect(lowestNormalized([res("Builtin.Helpfulness", null)])).toBeNull();
    expect(lowestNormalized([])).toBeNull();
  });

  it("marks sessions like the backend: unscored, above threshold, unsupported id, cap, run order", () => {
    const r = results({
      good: [res("Builtin.Helpfulness", 0.95)],
      bad1: [res("Builtin.Helpfulness", 0.95), res("Builtin.Correctness", 0.3)],
      none: [res("Builtin.Helpfulness", null)],
      "bad id!": [res("Builtin.Correctness", 0.1)],
      bad2: [res("Builtin.Correctness", 0.5)],
      bad3: [res("Builtin.Correctness", 0.69)],
    });
    const order = ["bad3", "good", "bad1", "none", "bad id!", "bad2", "bad1"];
    const rows = previewRunSessions(r, order, src({ batch_run_id: "r1", max_score: 0.7, max_sessions: 2 }));
    expect(rows.map((x) => [x.session_id, x.verdict])).toEqual([
      ["bad3", "send"], ["good", "above"], ["bad1", "send"], ["none", "unscored"], ["bad id!", "unsupported"], ["bad2", "overflow"],
    ]);
    // null threshold = every scored session; no run session list = the results' order
    const all = previewRunSessions(r, [], src({ batch_run_id: "r1", max_score: null, max_sessions: 25 }));
    expect(all.filter((x) => x.verdict === "send").map((x) => x.session_id)).toEqual(["good", "bad1", "bad2", "bad3"]);
    expect(previewRunSessions(null, order, src())).toEqual([]);
  });

  it("offers the agent's completed evaluator runs only, newest first", () => {
    const run = (id: string, patch: Partial<EvaluationRunInfo>) => ({ id, agent_id: "a1", mode: "evaluators", status: "completed", created_at: "2026-11-01T00:00:00Z", ...patch }) as EvaluationRunInfo;
    const runs = [
      run("old", {}),
      run("new", { created_at: "2026-11-20T00:00:00Z" }),
      run("insights", { mode: "insights" }),
      run("active", { status: "invoking" }),
      run("other", { agent_id: "a2" }),
    ];
    expect(generationRuns(runs, "a1").map((r) => r.id)).toEqual(["new", "old"]);
    expect(generationRuns(runs, null)).toEqual([]);
  });

  it("sends the run id and threshold; a run-mode draft without a run is still run mode", () => {
    expect(sessionsFromOf(src({ batch_run_id: "" }))).toBe("run");
    expect(sessionsFromOf(src({ batch_run_id: null }))).toBe("traffic");
    expect(cleanAgentcoreSource(src({ batch_run_id: "r1", max_score: 1.4 }), "hr-bot")).toMatchObject({ batch_run_id: "r1", max_score: 1 });
    expect(cleanAgentcoreSource(src({ batch_run_id: "r1", max_score: null }), "hr-bot").max_score).toBeNull();
    expect(cleanAgentcoreSource(src({ batch_run_id: "" }), "hr-bot").batch_run_id).toBeNull();
  });
});
