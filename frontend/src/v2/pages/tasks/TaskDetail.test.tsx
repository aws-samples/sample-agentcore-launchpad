// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { EvaluationRunInfo } from "../../../lib/evaluation";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { agentLabel, sourceLabel, taskFromRun } from "../../tasks";
import { resetScaleLabelCatalog } from "../../../lib/scale-label-catalog";
import { TaskDetail } from "./TaskDetail";

/**
 * N15-obs8 (isolation scenario V8): a run payload may name a dataset / an agent the viewer
 * cannot see — the server keeps the id, nulls the name and sets `hidden.<ref>`; the task
 * detail renders the placeholder in both places instead of a blank or a crash. A run with
 * nothing masked (and one from an older backend without `hidden`) keeps the names.
 */

let run: EvaluationRunInfo;
const NO_RESULTS = { run_id: "run-1", batch_eval_id: null, available: false, reason: "no_batch", sessions: [], count: 0, truncated: false };
let runResults: unknown = NO_RESULTS;
let catalogRows: unknown[] = [];
const base = {
  id: "run-1", agent_id: "agent-a", agent_name: "ada-agent", dataset_id: "ds-a", dataset_name: "ada-secret",
  mode: "evaluators", evaluators: ["Builtin.Helpfulness"], status: "completed", queue_position: 0,
  scores: [], insights: {}, session_ids: ["s1"], batch_eval_id: null, error: null,
  created_at: "2026-10-19T00:00:00Z", updated_at: "2026-10-19T00:00:00Z", created_by: "u-carol", created_by_name: "carol",
} as unknown as EvaluationRunInfo;

// N17-obs473: every mocked read is tracked, so a test waits until the page's data has
// ANSWERED and been applied (`settled`) instead of racing a 1 s `waitFor` deadline — on a
// loaded host (load ≈ 150) the zh-label tests needed longer than that.
const inflight = vi.hoisted(() => [] as Promise<unknown>[]);
const tracked = vi.hoisted(() => <T,>(value: () => T) => () => {
  const p = Promise.resolve(value());
  inflight.push(p);
  return p;
});

async function settled() {
  for (let round = 0; round < 50; round++) {
    const calls = inflight.length;
    await act(async () => {
      await Promise.allSettled(inflight);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    if (inflight.length === calls) return;
  }
  throw new Error("the page kept issuing reads");
}

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getEvaluationRun: tracked(() => run),
      evaluationRunResults: tracked(() => runResults),
      v2Evaluators: tracked(() => ({ evaluators: catalogRows, builtin_count: 0 })),
      // the "compare with the previous run" read — answered here, never the network
      evaluationScorecard: tracked(() => ({ runs: [] })),
      runRecommendationInputs: () => Promise.resolve({ source: "manual", eligible: false, reason_code: "no_sessions", tools: [], notes: [], evaluators: [], excluded_evaluators: [], default_evaluator: null, tools_eligible: false, accept_eligible: false, accept_ineligible_reason: null, system_prompt: "", agent_method: "zip_runtime" }),
      recommendationProviders: () => Promise.resolve({ providers: [] }),
      runRecommendations: () => Promise.resolve({ recommendations: [] }),
    },
  };
});

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  runResults = NO_RESULTS;
  inflight.length = 0;
});

function mount() {
  return render(
    <MemoryRouter initialEntries={["/v2/tasks?view=detail&kind=run&id=run-1"]}>
      <TaskDetail kind="run" id="run-1" />
    </MemoryRouter>,
  );
}

describe("TaskDetail hidden references (N15-obs8)", () => {
  it("renders the placeholder for a hidden dataset and a hidden agent", async () => {
    run = { ...base, agent_name: null, dataset_name: null, hidden: { agent: true, dataset: true } };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-source")).toBeTruthy());
    expect(screen.getByTestId("v2-task-source").textContent).toBe("数据集 · 已隐藏（对你不可见）");
    expect(screen.getByTestId("v2-task-agent").textContent).toBe("已隐藏（对你不可见）");
    expect(document.body.textContent).not.toContain("ada-secret");
    expect(document.body.textContent).not.toContain("ada-agent");
  });

  it("keeps the names when nothing is masked, with or without the flag", async () => {
    run = { ...base, hidden: { agent: false, dataset: false } };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-source")).toBeTruthy());
    expect(screen.getByTestId("v2-task-source").textContent).toBe("数据集 · ada-secret");
    expect(screen.getByTestId("v2-task-agent").textContent).toBe("ada-agent");
    // an older backend sends no `hidden` at all
    const task = taskFromRun({ ...base });
    expect(task.hidden).toEqual({ agent: false, dataset: false });
    expect(sourceLabel(i18next.t.bind(i18next), task)).toBe("数据集 · ada-secret");
    expect(agentLabel(i18next.t.bind(i18next), task)).toBe("ada-agent");
  });

  it("names a masked run without the literal null", () => {
    const task = taskFromRun({ ...base, name: null, agent_name: null, dataset_name: null, hidden: { agent: true, dataset: true } });
    expect(task.name).not.toContain("null");
    expect(task.agentName).toBe("");
  });
});

describe("TaskDetail extraction reconciliation (eval accuracy item 1)", () => {
  it("flags a run whose extraction mismatched: banner, headline KPI and the session's rows", async () => {
    const check = {
      status: "mismatch", source: "replay", turns_checked: 1, mismatch_count: 1,
      mismatches: [{ turn: 1, expected_head: "已查到：450 元/晚", extracted_head: "我先查一下" }],
      anomalies: { turns: 1, empty: 0, tool_json_like: 0, repeated_previous: 0 },
    };
    run = {
      ...base, batch_eval_id: "run-1", session_ids: ["s1", "s2"],
      extraction_check: {
        status: "mismatch", sessions: 2, ok: 1, mismatch: 1, unverifiable: 0, turns_checked: 2,
        mismatched_sessions: ["s2"], anomalies: { turns: 2, empty: 0, tool_json_like: 0, repeated_previous: 0, empty_rate: 0 },
      },
    } as EvaluationRunInfo;
    const row = { evaluator_id: "Builtin.Helpfulness", level: "TRACE", score: 1, label: "Very Helpful", explanation: "ok", error_type: null, error_message: null };
    runResults = {
      run_id: "run-1", batch_eval_id: "run-1", available: true, count: 2, truncated: false,
      sessions: [
        { session_id: "s1", results: [row], extraction_check: { ...check, status: "ok", mismatches: [], mismatch_count: 0 } },
        { session_id: "s2", results: [row], extraction_check: check },
      ],
    };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-extraction-mismatch")).toBeTruthy());
    expect(screen.getByTestId("v2-extraction-mismatch").textContent).toContain("取数校验未通过：评分可能不可信");
    expect(screen.getByTestId("v2-kpi-extraction").textContent).toContain("1");
    await waitFor(() => expect(screen.getAllByTestId("v2-extraction-row-flag")).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("v2-extraction-detail").textContent).toContain("我先查一下"));
  });

  it("shows nothing for a run from a worker that reported no check", async () => {
    run = { ...base };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-source")).toBeTruthy());
    expect(screen.queryByTestId("v2-extraction-mismatch")).toBeNull();
    expect(screen.queryByTestId("v2-kpi-extraction")).toBeNull();
  });
});

describe("TaskDetail judge model (P3.1 / P3.2)", () => {
  it("names the judge and its temperature", async () => {
    run = {
      ...base,
      judge_model_id: "global.zai.glm-5.3",
      judge_inference: { model_id: "global.zai.glm-5.3", temperature: 0, temperature_applied: true, max_tokens: 4096 },
    };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-judge")).toBeTruthy());
    expect(screen.getByTestId("v2-task-judge").textContent).toBe("global.zai.glm-5.3 · 温度 0");
    expect(screen.queryByTestId("v2-task-failure-detection-judge")).toBeNull();
  });

  it("names an insights run's failure-detection judge beside the run's judge (N17-obs449)", async () => {
    run = {
      ...base,
      mode: "insights",
      judge_model_id: "global.zai.glm-5.3",
      judge_inference: {
        model_id: "global.zai.glm-5.3", temperature: 0, temperature_applied: true, max_tokens: 4096,
        failure_detection: { model_id: "global.moonshotai.kimi-k3", temperature: "provider default", temperature_applied: false, max_tokens: 16384 },
      },
    };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-failure-detection-judge")).toBeTruthy());
    expect(screen.getByTestId("v2-task-failure-detection-judge").textContent).toBe("失败检测评审：global.moonshotai.kimi-k3");
  });

  it("flags a judge that runs at the provider's default temperature", async () => {
    run = {
      ...base,
      judge_model_id: "global.moonshotai.kimi-k3",
      judge_inference: { model_id: "global.moonshotai.kimi-k3", temperature: "provider default", temperature_applied: false, max_tokens: 2048 },
    };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-judge")).toBeTruthy());
    const text = screen.getByTestId("v2-task-judge").textContent;
    expect(text).toContain("global.moonshotai.kimi-k3 · 服务商默认温度");
    expect(text).toContain("结果不确定");
  });

  it("shows the bare judge id, or a dash, on a row without the inference config", async () => {
    run = { ...base };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-source")).toBeTruthy());
    expect(screen.queryByTestId("v2-task-judge")).toBeNull();
    expect(document.body.textContent).toContain("评审模型");
  });
});

describe("TaskDetail worker heartbeat (N17-obs418)", () => {
  it("shows the newest heartbeat while the run is running", async () => {
    const stamp = new Date(Date.now() - 42_000).toISOString();
    run = { ...base, status: "evaluating", last_heartbeat_at: stamp } as unknown as EvaluationRunInfo;
    mount();
    const line = await screen.findByTestId("v2-run-heartbeat");
    expect(line.textContent).toContain("最近心跳");
    expect(line.textContent).toMatch(/（4[0-9] 秒前）/);
  });

  it("is absent once the run has settled, and on an older backend", async () => {
    run = { ...base, last_heartbeat_at: "2026-10-19T00:00:00Z" } as unknown as EvaluationRunInfo;
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-source")).toBeTruthy());
    expect(screen.queryByTestId("v2-run-heartbeat")).toBeNull();
    cleanup();
    run = { ...base, status: "evaluating" } as unknown as EvaluationRunInfo;
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-source")).toBeTruthy());
    expect(screen.queryByTestId("v2-run-heartbeat")).toBeNull();
  });

  it("never reports a negative age", async () => {
    const { heartbeatAge } = await import("../../format");
    expect(heartbeatAge("2026-10-24T08:00:10Z", Date.parse("2026-10-24T08:00:00Z"))).toBe(0);
    expect(heartbeatAge("2026-10-24T08:00:00Z", Date.parse("2026-10-24T08:01:30Z"))).toBe(90);
    expect(heartbeatAge("not a time")).toBe(0);
  });
});

describe("TaskDetail run provenance (P7.1 / P7.6)", () => {
  it("names the evaluator versions the run was submitted with and the worker release", async () => {
    run = {
      ...base,
      evaluator_versions: [
        { id: "Builtin.Helpfulness", name: "Builtin.Helpfulness", kind: "builtin", version: "strands-agents-evals 1.5.0", definition_hash: null },
        { id: "ev1", name: "tone_judge", kind: "judge", version: 3, definition_hash: "abcdef0123456789" },
      ],
      worker_release: "1.0.0-0123456789ab",
    } as unknown as EvaluationRunInfo;
    mount();
    const line = await screen.findByTestId("v2-run-provenance");
    expect(line.textContent).toContain("Builtin.Helpfulness (strands-agents-evals 1.5.0) · tone_judge v3");
    expect(line.textContent).toContain("1.0.0-0123456789ab");
  });
});

describe("TaskDetail dataset revision (N17-obs424)", () => {
  it("names the dataset revision the run replayed", async () => {
    run = { ...base, dataset_revision: 4 };
    mount();
    expect((await screen.findByTestId("v2-task-dataset-revision")).textContent).toBe("r4");
    expect(screen.getByText("回放的数据集版本")).toBeTruthy();
  });

  it("shows nothing for a run without one", async () => {
    run = { ...base };
    mount();
    await waitFor(() => expect(screen.getByTestId("v2-task-source")).toBeTruthy());
    expect(screen.queryByTestId("v2-task-dataset-revision")).toBeNull();
  });
});

describe("TaskDetail built-in scale labels in zh (N17-obs428)", () => {
  it("shows each built-in label's zh meaning with the stored label as the tooltip; custom verbatim", async () => {
    const judged = (evaluator_id: string, label: string) =>
      ({ evaluator_id, level: "TRACE", score: 1, label, explanation: "ok", error_type: null, error_message: null });
    run = { ...base, batch_eval_id: "run-1", evaluators: ["Builtin.Helpfulness", "Builtin.Refusal", "cust_tone"] };
    runResults = {
      run_id: "run-1", batch_eval_id: "run-1", available: true, count: 3, truncated: false,
      sessions: [{ session_id: "s1", results: [
        judged("Builtin.Helpfulness", "Very Helpful"), judged("Builtin.Refusal", "Yes"), judged("cust_tone", "Yes"),
      ] }],
    };
    mount();
    await settled(); // the run, its results and the evaluator catalogue answered and were applied
    expect(screen.getByText("非常有帮助").getAttribute("title")).toBe("Very Helpful");
    expect(screen.getByText("已拒答").getAttribute("title")).toBe("Yes");
    // the custom judge's label is its author's text: verbatim, no tooltip
    expect(screen.getAllByText("Yes").some((el) => !el.getAttribute("title"))).toBe(true);
    expect(document.body.textContent).not.toContain("Very Helpful");
  });
});

describe("TaskDetail derived evaluators' labels in zh (N17-obs428)", () => {
  it("reads a derived evaluator through its base, and an edited scale verbatim", async () => {
    resetScaleLabelCatalog();
    catalogRows = [
      { id: "correctness_kimi", source: "custom", level: "TRACE", definition: "derived", base_evaluator_id: "Builtin.Correctness", scale_labels: [] },
      { id: "correctness_mine", source: "custom", level: "TRACE", definition: "derived", base_evaluator_id: "Builtin.Correctness", scale_labels: ["Spot On", "Off"] },
    ];
    const judged = (evaluator_id: string, label: string) =>
      ({ evaluator_id, level: "TRACE", score: 1, label, explanation: "ok", error_type: null, error_message: null });
    run = { ...base, batch_eval_id: "run-1", evaluators: ["correctness_kimi", "correctness_mine"] };
    runResults = {
      run_id: "run-1", batch_eval_id: "run-1", available: true, count: 2, truncated: false,
      sessions: [{ session_id: "s1", results: [judged("correctness_kimi", "Perfectly Correct"), judged("correctness_mine", "Spot On")] }],
    };
    mount();
    await settled();
    expect(screen.getByText("完全正确").getAttribute("title")).toBe("Perfectly Correct");
    expect(screen.getAllByText("Spot On").every((el) => !el.getAttribute("title"))).toBe(true);
    expect(document.body.textContent).not.toContain("Perfectly Correct");
    catalogRows = [];
    resetScaleLabelCatalog();
  });
});

describe("TaskDetail custom evaluators by name (N17-obs429)", () => {
  it("names a custom evaluator from the catalogue, the id as tooltip; a deleted one keeps its id", async () => {
    resetScaleLabelCatalog();
    catalogRows = [
      { id: "cust_f623957dd060", name: "hr_policy_citation", source: "custom", level: "TRACE", definition: "judge", base_evaluator_id: null, scale_labels: ["Yes", "No"] },
    ];
    const judged = (evaluator_id: string, score: number) =>
      ({ evaluator_id, level: "TRACE", score, label: "Yes", explanation: "ok", error_type: null, error_message: null });
    run = { ...base, batch_eval_id: "run-1", evaluators: ["cust_f623957dd060", "cust_deadbeef0000"] };
    runResults = {
      run_id: "run-1", batch_eval_id: "run-1", available: true, count: 2, truncated: false,
      sessions: [{ session_id: "s1", results: [judged("cust_f623957dd060", 1), judged("cust_deadbeef0000", 0)] }],
    };
    mount();
    await settled();
    expect(screen.getAllByText("hr_policy_citation").length).toBeGreaterThanOrEqual(3);
    // the run's evaluator tags, the per-evaluator card and the results table all use the name
    const named = screen.getAllByText("hr_policy_citation");
    expect(named.some((el) => el.getAttribute("title") === "cust_f623957dd060")).toBe(true);
    expect(document.body.textContent).not.toContain("cust_f623957dd060");
    // a deleted evaluator: no catalogue row — its id, never a blank
    expect(screen.getAllByText("cust_deadbeef0000").length).toBeGreaterThan(0);
    catalogRows = [];
    resetScaleLabelCatalog();
  });
});
