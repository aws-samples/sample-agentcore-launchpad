// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentInfo, RecommendProviderInfo } from "../../../lib/api";
import type { EvaluationRunInfo, RunRecommendation, RunRecommendationInputs } from "../../../lib/evaluation";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { readCanarySeed } from "../canary/common";
import { RunRecommendations } from "./RunRecommendations";

/**
 * N12-obs32: the CN optimizer is a self-hosted provider from the registry (GEPA-lite /
 * Strands harness-optimizer), so every surface that shows a run's recommendations names
 * that provider from `provider.label` — never the Global managed recommendation service
 * (its name is composed below so the repo-wide sweep for it stays empty). Without a
 * provider list the copy falls back to the neutral "AI recommendations".
 * The red-team exclusion note states what Launchpad does (`adversarial_sessions`).
 */

let providers: RecommendProviderInfo[] = [];
let recs: RunRecommendation[] = [];
let agent: Partial<AgentInfo> | null = null;
const getAgent = vi.fn((id: string) => (agent ? Promise.resolve({ id, ...agent }) : Promise.reject(new Error("gone"))));
// N17-obs464: the manual edit published on top of an accepted recommendation
const revise = vi.fn<(...args: unknown[]) => Promise<unknown>>(() =>
  Promise.resolve({ job_id: "job-edit", deployment_id: "dep-edit", agent: {}, recommendation: recs[0] }));

vi.mock("../../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/api")>()),
  api: {
    runRecommendationInputs: () => Promise.resolve(INPUTS),
    recommendationProviders: () => Promise.resolve({ providers }),
    runRecommendations: () => Promise.resolve({ recommendations: recs }),
    getAgent: (id: string) => getAgent(id),
    reviseRunRecommendation: (...args: unknown[]) => revise(...args),
  },
}));

const INPUTS: RunRecommendationInputs = {
  source: "spec",
  agent_method: "zip_runtime",
  system_prompt: "Help the user.",
  tools: [],
  notes: [],
  evaluators: [],
  excluded_evaluators: [],
  default_evaluator: "Builtin.Helpfulness",
  eligible: true,
  reason_code: null,
  tools_eligible: true,
  accept_eligible: true,
  accept_ineligible_reason: null,
};

const RUN = {
  id: "run-1",
  agent_id: "agent-1",
  agent_name: "demo",
  dataset_name: "set",
  mode: "dataset",
  evaluators: ["Builtin.Helpfulness"],
  status: "completed",
  queue_position: null,
  scores: [],
  insights: {},
  session_ids: ["sess-0001", "sess-0002"],
  batch_eval_id: "run-1",
} as unknown as EvaluationRunInfo;

const GEPA: RecommendProviderInfo = {
  id: "gepa_lite",
  label: "GEPA-lite · single reflective round (evaluation worker)",
  requires_source: true,
  supports: ["system_prompt", "tool_descriptions"],
  models: [],
  default_model_id: "global.zai.glm-5.3",
  enabled: true,
};

const STRANDS: RecommendProviderInfo = {
  ...GEPA,
  id: "harness_optimizer",
  label: "Strands harness-optimizer · contrastive reflection (evaluation worker)",
  supports: ["system_prompt"],
};

function completed(provider: string): RunRecommendation {
  return {
    id: "rec-1",
    run_id: "run-1",
    kind: "system_prompt",
    recommendation_id: `provider-${provider}-abc`,
    name: "rec",
    status: "COMPLETED",
    input_source: "spec",
    system_prompt: "Help the user.",
    evaluator: null,
    tools: {},
    skipped_tools: [],
    result: {
      recommended_prompt: "Help the user clearly.",
      explanation: "Why.",
      provider,
      provider_model_id: "global.zai.glm-5.3",
      excluded_sessions: [{ session_id: "sess-0002", scenario_id: "S2" }],
    },
    error: null,
    accepted: null,
    created_at: "2026-10-15T08:00:00Z",
    updated_at: "2026-10-15T08:00:00Z",
  };
}

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

beforeEach(() => {
  providers = [GEPA, STRANDS];
  recs = [];
  agent = null;
  getAgent.mockClear();
  revise.mockClear();
  window.sessionStorage.clear();
});

function Where() {
  const loc = useLocation();
  return <span data-testid="where">{loc.pathname + loc.search}</span>;
}

/** RunRecommendations inside a router, with the current URL rendered for assertions */
function renderRec(props: Partial<Parameters<typeof RunRecommendations>[0]> = {}) {
  return render(
    <MemoryRouter initialEntries={["/v2/eval/tasks?view=detail"]}>
      <Routes>
        <Route path="*" element={<><RunRecommendations run={RUN} {...props} /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("en");
});

const sub = () => screen.getByTestId("v2-rec-card").querySelector(".sub")?.textContent ?? "";

describe("RunRecommendations names the provider from the registry (N12-obs32)", () => {
  it("subtitles the card with the selected provider's short label", async () => {
    renderRec();
    await waitFor(() => expect(sub()).toBe("GEPA-lite reads this run's traces"));
    expect(sub()).not.toContain("AgentCore");
  });

  it("tags a completed recommendation with its producer's label and states the CN exclusion rule", async () => {
    recs = [completed("gepa_lite")];
    renderRec();
    const tag = await screen.findByTestId("v2-rec-provider-tag");
    expect(tag.textContent).toBe("GEPA-lite · global.zai.glm-5.3");
    const note = screen.getByTestId("v2-rec-excluded-sessions").textContent ?? "";
    expect(note).toContain("Left out 1 red-team test session(s) (S2)");
    expect(note).toContain("Launchpad keeps sessions whose golden test is marked adversarial");
    expect(note).toContain("out of the evidence the optimizer reads");
    expect(note).not.toContain("AgentCore");
  });

  it("falls back to the neutral wording and the provider id without a provider list", async () => {
    providers = [];
    recs = [completed("gepa_lite")];
    renderRec();
    const tag = await screen.findByTestId("v2-rec-provider-tag");
    expect(tag.textContent).toBe("gepa_lite · global.zai.glm-5.3");
    expect(sub()).toBe("AI recommendations read this run's traces");
  });

  it("renders the zh-CN copy with the same provider name", async () => {
    await i18next.changeLanguage("zh-CN");
    recs = [completed("harness_optimizer")];
    renderRec();
    await waitFor(() => expect(sub()).toBe("GEPA-lite 读取本任务的 trace"));
    const tag = await screen.findByTestId("v2-rec-provider-tag");
    expect(tag.textContent).toBe("Strands harness-optimizer · global.zai.glm-5.3");
    const note = screen.getByTestId("v2-rec-excluded-sessions").textContent ?? "";
    expect(note).toContain("已排除 1 个红线测试会话（S2）");
    expect(note).toContain("Launchpad 会把黄金测试标记为 adversarial");
    expect(note).not.toContain("AgentCore");
  });
});

describe("recommendation copy never names the Global service (N12-obs32 sweep)", () => {
  it("en and zh-CN locales carry no Global recommendation-service wording", () => {
    const globalService = ["AgentCore", "Recommendations"].join(" ");
    for (const bundle of [en, zh]) {
      const flat = JSON.stringify(bundle);
      expect(flat).not.toContain(globalService);
      expect(flat).not.toContain(["AgentCore", "recommendation"].join(" "));
      expect(flat).not.toContain("AgentCore 推荐");
      expect(flat).not.toContain("come from AgentCore");
      expect(flat).not.toContain("由 AgentCore 生成");
    }
  });
});

describe("用金丝雀验证 hands a prompt recommendation to 新建金丝雀 (N17-obs309)", () => {
  const ELIGIBLE: Partial<AgentInfo> = { status: "active", canary_capability: { eligible: true, reason: null, reason_code: null } };

  it("opens 新建金丝雀 with the run's agent as champion and stashes the recommended prompt", async () => {
    agent = ELIGIBLE;
    recs = [completed("gepa_lite")];
    renderRec();
    const button = await screen.findByTestId("v2-rec-canary");
    expect(button.textContent).toBe("Verify with a canary");
    expect(getAgent).toHaveBeenCalledWith("agent-1");
    fireEvent.click(button);
    expect(screen.getByTestId("where").textContent).toBe("/v2/eval/experiments?canary=new&champion=agent-1&rec=rec-1");
    expect(readCanarySeed("rec-1")).toBe("Help the user clearly.");
  });

  it("reads 用金丝雀验证 in zh-CN", async () => {
    await i18next.changeLanguage("zh-CN");
    agent = ELIGIBLE;
    recs = [completed("gepa_lite")];
    renderRec();
    expect((await screen.findByTestId("v2-rec-canary")).textContent).toBe("用金丝雀验证");
  });

  it.each([
    ["not canary-eligible", { status: "active", canary_capability: { eligible: false, reason: "x", reason_code: "method" } }],
    ["without a canary verdict", { status: "active" }],
    ["not active", { status: "failed", canary_capability: { eligible: true, reason: null, reason_code: null } }],
    ["unreadable", null],
  ] as [string, Partial<AgentInfo> | null][])("is hidden when the agent is %s", async (_label, a) => {
    agent = a;
    recs = [completed("gepa_lite")];
    renderRec();
    await screen.findByTestId("v2-rec-accept");
    await waitFor(() => expect(getAgent).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId("v2-rec-canary")).toBeNull();
  });

  it("is hidden for a read-only viewer and never reads the agent", async () => {
    agent = ELIGIBLE;
    recs = [completed("gepa_lite")];
    renderRec({ readOnly: true });
    await screen.findByTestId("v2-rec-provider-tag");
    expect(screen.queryByTestId("v2-rec-canary")).toBeNull();
    expect(getAgent).not.toHaveBeenCalled();
  });
});

/** N17-obs455 / obs453: the card read 「COMPLETED」 and 「CHANGED」 in English and named
 *  Builtin.GoalSuccessRate — the form's hidden default — for a GEPA-lite recommendation that
 *  never optimised toward it. */
describe("recommendation card copy and target (N17-obs455 / obs453)", () => {
  const itemSub = () => screen.getByTestId("v2-rec-system_prompt").querySelector(".sub")?.textContent ?? "";

  it("names the status and the diff label in zh-CN, the raw status on hover", async () => {
    await i18next.changeLanguage("zh-CN");
    recs = [completed("gepa_lite")];
    renderRec();
    const status = await screen.findByTestId("v2-rec-status");
    expect(status.textContent).toBe("已完成");
    expect(status.getAttribute("title")).toBe("COMPLETED");
    expect(screen.getByText("已修改")).toBeTruthy();
    expect(screen.queryByText("CHANGED")).toBeNull();
    expect(screen.queryByText("COMPLETED")).toBeNull();
  });

  it("never names an evaluator GEPA-lite did not optimise toward", async () => {
    await i18next.changeLanguage("zh-CN");
    recs = [{ ...completed("gepa_lite"), evaluator: "Builtin.GoalSuccessRate", optimized_toward: null }];
    renderRec();
    await screen.findByTestId("v2-rec-status");
    expect(itemSub()).not.toContain("GoalSuccessRate");
    expect(itemSub()).not.toContain("目标达成");
    expect(itemSub()).toContain(zh.v2.rec.providerAllEvaluators);
  });

  it("names the evaluator a target provider optimised toward", async () => {
    recs = [{ ...completed("harness_optimizer"), evaluator: "Builtin.Helpfulness", optimized_toward: "Builtin.Helpfulness" }];
    renderRec();
    await screen.findByTestId("v2-rec-status");
    expect(itemSub()).toContain("Helpfulness");
  });

  it("offers the evaluator picker only for a provider that takes a target", async () => {
    providers = [GEPA];
    renderRec();
    await screen.findByTestId("v2-rec-submit");
    expect(screen.queryByTestId("v2-rec-evaluator")).toBeNull();
    cleanup();
    providers = [{ ...STRANDS, target_evaluator: true }];
    renderRec();
    expect(await screen.findByTestId("v2-rec-evaluator")).toBeTruthy();
  });
});

/** N17-obs464: the user (2026-10-26) wants, right on the recommendation card, to start from the
 *  AI-recommended prompt, edit it by hand and publish — the edit path used to hide inside the
 *  accept dialog, and an ACCEPTED recommendation was read-only. */
describe("editing a recommendation by hand (N17-obs464)", () => {
  const AI = "Help the user clearly.";
  const accepted = (finalPrompt: string, edits: unknown[] = []) => ({
    ...completed("gepa_lite"),
    final_prompt: finalPrompt,
    edits,
    accepted: { by: "demo-admin", at: "2026-10-26T05:57:00+00:00", agent_id: "agent-1", previous_version: "2",
      job_id: "job-acc", deployment_id: "dep-acc", edited: true, system_prompt: finalPrompt, edits },
  }) as unknown as RunRecommendation;
  const LIVE = (prompt: string): Partial<AgentInfo> => ({
    status: "active", version: "3", method: "harness", spec: { system_prompt: prompt } as never,
    canary_capability: { eligible: false, reason: null, reason_code: null },
  });

  it("offers 编辑提示词 on an unaccepted recommendation and focuses the editor", async () => {
    await i18next.changeLanguage("zh-CN");
    recs = [completed("gepa_lite")];
    renderRec();
    const edit = await screen.findByTestId("v2-rec-edit");
    expect(edit.textContent).toBe("编辑提示词");
    fireEvent.click(edit);
    const editor = (await screen.findByTestId("v2-rec-accept-prompt")) as HTMLTextAreaElement;
    expect(editor.value).toBe(AI);
    await waitFor(() => expect(document.activeElement).toBe(editor));
  });

  it("edits an accepted recommendation from its final text and publishes on the live version", async () => {
    await i18next.changeLanguage("zh-CN");
    recs = [accepted("Help the user clearly. Never skip approval.")];
    agent = LIVE("Help the user clearly. Never skip approval.");
    renderRec();
    const button = await screen.findByTestId("v2-rec-revise");
    expect(button.textContent).toBe("在此建议基础上修改");
    fireEvent.click(button);
    const editor = (await screen.findByTestId("v2-rec-revise-prompt")) as HTMLTextAreaElement;
    expect(editor.value).toBe("Help the user clearly. Never skip approval.");
    expect(screen.queryByTestId("v2-rec-revise-drift")).toBeNull();
    expect(screen.getByText(zh.v2.rec.accept.reviewHint)).toBeTruthy(); // the red-line hint stays
    fireEvent.change(editor, { target: { value: "Help the user clearly. Never skip approval. Be brief." } });
    fireEvent.click(screen.getByTestId("v2-rec-revise-publish"));
    await waitFor(() => expect(revise).toHaveBeenCalledWith(
      "run-1", "rec-1", "Help the user clearly. Never skip approval. Be brief.", "3"));
  });

  it("restores the AI text with 恢复为 AI 建议原文", async () => {
    await i18next.changeLanguage("zh-CN");
    recs = [accepted("edited by hand")];
    agent = LIVE("edited by hand");
    renderRec();
    fireEvent.click(await screen.findByTestId("v2-rec-revise"));
    const editor = (await screen.findByTestId("v2-rec-revise-prompt")) as HTMLTextAreaElement;
    fireEvent.click(screen.getByTestId("v2-rec-revise-restore"));
    expect(editor.value).toBe(AI);
  });

  it("warns, shows the diff and needs a confirmation when the live prompt changed since", async () => {
    await i18next.changeLanguage("zh-CN");
    recs = [accepted("the text this recommendation published")];
    agent = LIVE("someone changed the live prompt afterwards");
    renderRec();
    fireEvent.click(await screen.findByTestId("v2-rec-revise"));
    const drift = await screen.findByTestId("v2-rec-revise-drift");
    expect(drift.textContent).toContain("v3");
    const publish = screen.getByTestId("v2-rec-revise-publish") as HTMLButtonElement;
    expect(publish.disabled).toBe(true);
    fireEvent.click(screen.getByTestId("v2-rec-revise-confirm-drift"));
    expect(publish.disabled).toBe(false);
  });

  it("lists the manual edits on the card", async () => {
    await i18next.changeLanguage("zh-CN");
    recs = [accepted("v4 text", [{ by: "ada", at: "2026-10-26T06:30:00+00:00", version_before: "3",
      job_id: "job-e", deployment_id: "dep-e", system_prompt: "v4 text" }])];
    renderRec();
    const row = await screen.findByTestId("v2-rec-edit-0");
    expect(row.textContent).toContain("ada");
    expect(row.textContent).toContain("3");
  });

  it("disables both edit paths without agents.deploy", async () => {
    const { AuthContext } = await import("../../../auth/auth-context");
    const deny = { authRequired: true, username: "bob", userId: "u-bob", role: "member", email: null,
      accountExpiresAt: null, isAdmin: false, can: () => false, logout: async () => undefined };
    for (const rows of [[completed("gepa_lite")], [accepted("x")]]) {
      recs = rows as RunRecommendation[];
      render(
        <AuthContext.Provider value={deny as never}>
          <MemoryRouter><RunRecommendations run={RUN} /></MemoryRouter>
        </AuthContext.Provider>,
      );
      const id = (rows[0] as RunRecommendation).accepted ? "v2-rec-revise" : "v2-rec-edit";
      const button = (await screen.findByTestId(id)) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      expect(button.getAttribute("title")).toBe(en.v2.rec.accept.noPermission);
      cleanup();
    }
  });
});
