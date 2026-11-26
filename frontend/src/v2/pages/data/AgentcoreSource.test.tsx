import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { AgentInfo, ObsSessionRow, V2PipelineRun, V2PipelineSource } from "../../../lib/api";
import type { EvaluationRunInfo } from "../../../lib/evaluation";
import { AgentcoreLogic, AgentcorePreviewCard, AgentcoreSourceFields, GenerationRunCard, RunPreviewCard } from "./AgentcoreSource";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const src = (patch: Partial<V2PipelineSource> = {}): V2PipelineSource => ({
  type: "agentcore",
  agent: "hr-bot",
  agent_id: "a1",
  range: "24h",
  status: "all",
  max_sessions: 20,
  scenario_type: "FIXED",
  expected_trajectory: true,
  redaction: null,
  ...patch,
});
const proc = { first_turn_only: false, dedupe: true, min_input_chars: 0, keep_replies: true };
const noop = () => undefined;

// the design.md contract example, verbatim in shape
const run: V2PipelineRun = {
  at: "2026-11-26T12:00:00Z",
  scanned: 9,
  matched: 7,
  added: 5,
  dataset_id: "ds1",
  error: null,
  skipped: [
    { session_id: "s-fresh", reason: "too_fresh" },
    { session_id: "s-bad", reason: "generation_failed", code: "SPANS_INCOMPLETE" },
    { session_id: "s-int", reason: "interrupt_transcript" },
  ],
  generation: {
    id: "lp_ab12cd34-9f8e7d6c5b",
    cloud_dataset_id: "trajectories-Xy12",
    scenario_type: "FIXED",
    stage: "done",
    status: "COMPLETED",
    requested: 7,
    results: { sessionsMatched: 7, sessionsSelected: 7, sessionsUnscored: 0, sessionsFailed: 1, scenariosGenerated: 6, scenariosDeduplicated: 0, examplesWritten: 6, examplesWithGroundTruth: 6 },
    error_details: [{ code: "SPANS_INCOMPLETE", message: "An agent or tool span has no log event…", count: 1, session_ids: ["s-bad"] }],
    redaction: { scanned: 6, findings: { ORDER_ID: 8 } },
    failure_reason: null,
  },
};

describe("GenerationRunCard", () => {
  it("shows counts, error codes, redaction findings and coded skip reasons", () => {
    const html = renderToStaticMarkup(<GenerationRunCard run={run} />);
    expect(html).toContain("lp_ab12cd34-9f8e7d6c5b");
    expect(html).toContain("Examples written");
    expect(html).toContain("SPANS_INCOMPLETE");
    expect(html).toContain("ORDER_ID");
    expect(html).toContain("still active (telemetry not settled)");
    expect(html).toContain("tool-approval transcript");
    expect(html).toContain("(SPANS_INCOMPLETE)");
  });

  it("renders a generation that has not finished (no results / details yet)", () => {
    const html = renderToStaticMarkup(
      <GenerationRunCard run={{ ...run, skipped: [], generation: { id: null, cloud_dataset_id: null, scenario_type: "DYNAMIC", stage: "starting", status: null, requested: 3 } }} />,
    );
    expect(html).toContain("Starting…");
    expect(html).not.toContain("Session errors");
  });

  it("renders nothing for a run without a generation", () => {
    expect(renderToStaticMarkup(<GenerationRunCard run={{ ...run, generation: undefined }} />)).toBe("");
  });
});

describe("AgentcoreLogic", () => {
  it("offers both ground-truth toggles for FIXED only", () => {
    const fixed = renderToStaticMarkup(<AgentcoreLogic source={src()} processing={proc} onSource={noop} onProc={noop} />);
    expect(fixed).toContain("v2-pipeline-ac-expected-response");
    expect(fixed).toContain("v2-pipeline-ac-expected-trajectory");
    const dynamic = renderToStaticMarkup(<AgentcoreLogic source={src({ scenario_type: "DYNAMIC" })} processing={proc} onSource={noop} onProc={noop} />);
    expect(dynamic).not.toContain("v2-pipeline-ac-expected-response");
    expect(dynamic).not.toContain("v2-pipeline-ac-expected-trajectory");
    expect(dynamic).toContain("personas, not ground truth");
  });

  it("lists the curated managed identifiers and flags a bad custom row", () => {
    const html = renderToStaticMarkup(
      <AgentcoreLogic source={src({ redaction: { identifiers: ["EMAIL_ADDRESS"], custom: [{ name: "ORDER_ID", regex: "a(?=b)" }] } })} processing={proc} onSource={noop} onProc={noop} />,
    );
    expect(html).toContain("Email address");
    expect(html).toContain("Driver&#x27;s license (US)");
    expect(html).toContain("don&#x27;t support lookahead");
  });
});

describe("AgentcoreSourceFields / AgentcorePreviewCard", () => {
  it("keeps a saved agent that is no longer offered visible, and hints DYNAMIC", () => {
    const html = renderToStaticMarkup(
      <AgentcoreSourceFields source={src({ scenario_type: "DYNAMIC", agent_id: "gone", agent: "old-bot" })} agents={[{ id: "a1", name: "hr-bot", method: "harness", status: "active" } as AgentInfo]} agentsLoading={false} runs={[]} runsLoading={false} onChange={noop} />,
    );
    expect(html).toContain("Only multi-turn sessions are used");
    expect(html).toContain("old-bot (unavailable)");
  });

  it("marks sessions that are still active as held back", () => {
    const row = { session_id: "s1", service: null, agent: "hr-bot", traces: 2, llm_calls: 1, errors: 0, tokens: { input: 0, output: 0, total: 0 }, est_cost_usd: null, first: null, last: null, platform: true } as ObsSessionRow;
    const html = renderToStaticMarkup(
      <AgentcorePreviewCard preview={{ picked: [], fresh: [row], overflow: 0, singleTurn: 1 }} picked loading={false} error={null} />,
    );
    expect(html).toContain("Too recent");
    expect(html).toContain("1 single-turn session(s) left out");
  });
});

describe("run-sourced generation (P3)", () => {
  const agents = [{ id: "a1", name: "hr-bot", method: "harness", status: "active" } as AgentInfo];
  const runs = [{ id: "r1", name: "nightly", agent_id: "a1", mode: "evaluators", status: "completed", session_ids: ["s1", "s2"], created_at: "2026-11-20T00:00:00Z" } as EvaluationRunInfo];

  it("run mode swaps range/status for the run picker and threshold", () => {
    const traffic = renderToStaticMarkup(<AgentcoreSourceFields source={src()} agents={agents} agentsLoading={false} runs={[]} runsLoading={false} onChange={noop} />);
    expect(traffic).not.toContain("v2-pipeline-ac-run");
    const run = renderToStaticMarkup(
      <AgentcoreSourceFields source={src({ batch_run_id: "r1", max_score: 0.7 })} agents={agents} agentsLoading={false} runs={runs} runsLoading={false} onChange={noop} />,
    );
    expect(run).toContain("v2-pipeline-ac-run");
    expect(run).toContain("v2-pipeline-ac-max-score");
    expect(run).toContain("nightly");
    expect(run).not.toContain("Session status");
  });

  it("a DYNAMIC run source does not claim single-turn sessions are dropped", () => {
    const html = renderToStaticMarkup(
      <AgentcoreSourceFields source={src({ batch_run_id: "r1", scenario_type: "DYNAMIC" })} agents={agents} agentsLoading={false} runs={runs} runsLoading={false} onChange={noop} />,
    );
    expect(html).toContain("single-turn sessions yield a plain input");
    expect(html).not.toContain("Only multi-turn sessions are used");
  });

  it("previews each session's lowest score and verdict", () => {
    const html = renderToStaticMarkup(
      <RunPreviewCard
        rows={[
          { session_id: "s1", lowest: 0.3, verdict: "send" },
          { session_id: "s2", lowest: 0.9, verdict: "above" },
          { session_id: "s3", lowest: null, verdict: "unscored" },
        ]}
        results={{ run_id: "r1", batch_eval_id: "b1", available: true, sessions: [], count: 3, truncated: false }}
        pickedRun
        loading={false}
        error={null}
      />,
    );
    expect(html).toContain("1 session(s) will be sent");
    expect(html).toContain("0.30");
    expect(html).toContain("Above threshold");
    expect(html).toContain("Unscored");
  });

  it("names the source run in the last-run card", () => {
    const html = renderToStaticMarkup(<GenerationRunCard run={{ ...run, generation: { ...run.generation!, batch_run_id: "r1" } }} />);
    expect(html).toContain("Source run");
    expect(html).toContain("Sessions in the run");
  });
});
