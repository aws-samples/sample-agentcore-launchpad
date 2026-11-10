import i18next from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import type { RuntimeCanaryInfo } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zhCN from "../../../locales/zh-CN/common.json";
import {
  CANARY_PROGRESS_CODES,
  completionSummary,
  insufficientArmHint,
  multipleComparisonsNote,
  normalQuantile,
  progressText,
  remainingPerArm,
  sessionsPerArm,
  VERDICT_CODES,
  VERDICT_REASON_CODES,
  verdictGate,
  verdictLabel,
  verdictReason,
  verdictTone,
} from "./common";

/**
 * N-SCN-obs14: at 90/10 a 9-prompt round routes ≈ 1 session to the treatment, so the live
 * canary read `control-wins` on C 32 vs T1 4 and rolled back. The backend now answers
 * `insufficient-n` with the per-arm counts; the card must (a) not treat that as terminal
 * and (b) tell the operator how far each arm is from the bar, in both languages.
 */

const insufficient = {
  verdict: "insufficient-n",
  n: 71,
  arms_n: { control: 32, treatment: 4 },
  min_arm_n: 10,
  metrics: [],
};

beforeAll(async () => {
  await i18next.init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zhCN } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

describe("verdictGate (ramp policy mirror)", () => {
  it("treats a significant control-wins and insufficient-data as terminal for the stage", () => {
    expect(verdictGate({ verdict: "control-wins", significant: true, metrics: [] })).toEqual({ blocked: true, needsOverride: false });
    // an older payload without the flag stays terminal (conservative)
    expect(verdictGate({ verdict: "control-wins", metrics: [] })).toEqual({ blocked: true, needsOverride: false });
    expect(verdictGate({ verdict: "insufficient-data", metrics: [] })).toEqual({ blocked: true, needsOverride: false });
  });

  it("lets insufficient-n, tie and a non-significant win advance only with the override", () => {
    expect(verdictGate(insufficient)).toEqual({ blocked: false, needsOverride: true });
    expect(verdictGate({ verdict: "tie", significant: false, metrics: [] })).toEqual({ blocked: false, needsOverride: true });
    expect(verdictGate({ verdict: "treatment-wins", significant: false, metrics: [] })).toEqual({ blocked: false, needsOverride: true });
  });

  // N-SCN-obs15 (ruling 2026-10-14): the scn-t01 50/50 verdict — control-wins, n 72, p 0.27 /
  // 0.16, both arms above the bar — is noise like a tie: overridable for advance, never completable.
  it("lets a NON-significant control-wins advance only with the override, symmetric with treatment-wins", () => {
    const nonSig = { verdict: "control-wins", significant: false, n: 72, arms_n: { control: 20, treatment: 16 }, min_arm_n: 10, metrics: [] };
    expect(verdictGate(nonSig)).toEqual({ blocked: false, needsOverride: true });
    expect(verdictGate(nonSig, "advance")).toEqual(verdictGate({ ...nonSig, verdict: "treatment-wins" }, "advance"));
  });

  it("blocks `complete` on ANY control-wins — the current version wins, nothing to promote", () => {
    expect(verdictGate({ verdict: "control-wins", significant: false, metrics: [] }, "complete")).toEqual({ blocked: true, needsOverride: false });
    expect(verdictGate({ verdict: "control-wins", significant: true, metrics: [] }, "complete")).toEqual({ blocked: true, needsOverride: false });
  });

  it("passes a significant treatment-wins and ignores a missing verdict", () => {
    expect(verdictGate({ verdict: "treatment-wins", significant: true, metrics: [] })).toEqual({ blocked: false, needsOverride: false });
    expect(verdictGate(undefined)).toEqual({ blocked: false, needsOverride: false });
  });

  it("blocks `complete` on insufficient-n even though `advance` may override it (terminal policy)", () => {
    expect(verdictGate(insufficient, "advance")).toEqual({ blocked: false, needsOverride: true });
    expect(verdictGate(insufficient, "complete")).toEqual({ blocked: true, needsOverride: false });
    // the other weak-evidence outcomes keep the override path at the final stage
    expect(verdictGate({ verdict: "tie", significant: false, metrics: [] }, "complete")).toEqual({ blocked: false, needsOverride: true });
    expect(verdictGate({ verdict: "treatment-wins", significant: false, metrics: [] }, "complete")).toEqual({ blocked: false, needsOverride: true });
    expect(verdictGate({ verdict: "treatment-wins", significant: true, metrics: [] }, "complete")).toEqual({ blocked: false, needsOverride: false });
    expect(verdictGate({ verdict: "control-wins", significant: true, metrics: [] }, "complete")).toEqual({ blocked: true, needsOverride: false });
  });
});

describe("insufficientArmHint", () => {
  // N17-obs446: the versions by name (current / candidate), never the internal arm keys C / T1
  it("names both versions against the bar in English", () => {
    const hint = insufficientArmHint(insufficient, i18next.getFixedT("en"));
    expect(hint).toBe(
      "scored records per version · current 32/10 · candidate 4/10 — send another traffic round, or advance with an explicit override",
    );
  });

  it("renders the zh-CN copy with the same counts", () => {
    const hint = insufficientArmHint(insufficient, i18next.getFixedT("zh-CN"));
    expect(hint).toContain("当前版本 32/10");
    expect(hint).toContain("候选版本 4/10");
    expect(hint).not.toMatch(/\bC 32|\bT1 4/);
    expect(hint).toBe(zhCN.canaryPage.insufficientArmHint.replace("{{control}}", "32").replace(/{{min}}/g, "10").replace("{{treatment}}", "4"));
  });

  it("tells the operator at the complete stage that both versions must reach the bar, in both languages", () => {
    const enHint = insufficientArmHint(insufficient, i18next.getFixedT("en"), "complete");
    expect(enHint).toBe(
      "scored records per version · current 32/10 · candidate 4/10 — promoting to production needs both versions at the bar (no override): send another traffic round with at least 10 sessions per version",
    );
    const zhHint = insufficientArmHint(insufficient, i18next.getFixedT("zh-CN"), "complete");
    expect(zhHint).toContain("当前版本 32/10");
    expect(zhHint).toContain("候选版本 4/10");
    expect(zhHint).toBe(zhCN.canaryPage.insufficientArmHintComplete.replace("{{control}}", "32").replace(/{{min}}/g, "10").replace("{{treatment}}", "4"));
  });

  it("is silent for other verdicts and for older payloads without the counts", () => {
    const t = i18next.getFixedT("en");
    expect(insufficientArmHint({ verdict: "treatment-wins", significant: true, arms_n: { control: 12, treatment: 11 }, min_arm_n: 10, metrics: [] }, t)).toBeNull();
    expect(insufficientArmHint({ verdict: "insufficient-n", n: 4, metrics: [] }, t)).toBeNull();
    expect(insufficientArmHint(undefined, t)).toBeNull();
  });
});

describe("judge-mismatch verdict (P3.2)", () => {
  it("is blocked for advance and complete, even with the override", () => {
    const verdict = { verdict: "judge-mismatch", reason: "mixed" } as never;
    expect(verdictGate(verdict, "advance")).toEqual({ blocked: true, needsOverride: false });
    expect(verdictGate(verdict, "complete")).toEqual({ blocked: true, needsOverride: false });
    expect(verdictTone("judge-mismatch")).toBe("red");
  });
});

describe("sessionsPerArm (R3-d power calculation, mirrors optimization/stats)", () => {
  it("matches the backend table", () => {
    // the same table as backend tests/optimization/test_canary_design.py
    const table: [number, number, number, number, number][] = [
      [0.2, 0.3, 0.05, 0.8, 36], [0.1, 0.3, 0.05, 0.8, 142], [0.2, 0.3, 0.05, 0.9, 48],
      [0.3, 0.5, 0.01, 0.8, 65],
    ];
    for (const [mde, sd, alpha, power, n] of table) {
      expect(sessionsPerArm({ mde, sd, alpha, power })).toBe(n);
    }
    expect(normalQuantile(0.975)).toBeCloseTo(1.959964, 5);
  });

  it("refuses a meaningless design", () => {
    expect(sessionsPerArm({ mde: 0, sd: 0.3, alpha: 0.05, power: 0.8 })).toBeNull();
    expect(sessionsPerArm({ mde: 0.2, sd: 0.3, alpha: 0.05, power: 0.2 })).toBeNull();
  });
});

describe("remainingPerArm (N17-obs427)", () => {
  const design = { planned_n_per_arm: 30 };
  const round = (over: Record<string, unknown>) => ({ ramp_stage: 0, weights: {}, traffic_attempts: [], ...over }) as never;

  it("is planned minus the thinner arm's scored sessions of the latest verdict", () => {
    expect(remainingPerArm(design, round({ verdict: { verdict: "insufficient-n", arms_n: { control: 12, treatment: 7 } } }))).toBe(23);
  });

  it("falls back to the sessions the attempts sent per arm, then to the whole plan", () => {
    expect(remainingPerArm(design, round({ traffic_attempts: [{ arms: { C: 6, T1: 2 } }, { arms: { C: 5, T1: 3 } }] }))).toBe(25);
    expect(remainingPerArm(design, round({}))).toBe(30);
    expect(remainingPerArm(design, undefined)).toBe(30);
  });

  it("never drops below one, never exceeds the server bound, and is null without a design", () => {
    expect(remainingPerArm(design, round({ verdict: { verdict: "tie", arms_n: { control: 40, treatment: 33 } } }))).toBe(1);
    expect(remainingPerArm({ planned_n_per_arm: 900 }, undefined)).toBe(500);
    expect(remainingPerArm(undefined, undefined)).toBeNull();
  });
});

/**
 * N17-obs443: the verdict tag showed the upper-cased raw code (INSUFFICIENT-N, TREATMENT-WINS)
 * and the reasons were the backend's English sentences on the zh console. Every code now has
 * a display name in both languages and every reason is worded from `reason_code` + params.
 */
describe("verdict copy (N17-obs443)", () => {
  const zh = () => i18next.getFixedT("zh-CN");
  const en = () => i18next.getFixedT("en");

  it("names every verdict code in both languages, never the raw code", () => {
    for (const code of VERDICT_CODES) {
      for (const t of [en(), zh()]) {
        const label = verdictLabel(code, t);
        expect(label).not.toBe(code);
        expect(label).not.toBe(code.toUpperCase());
        expect(label).not.toMatch(/canaryVerdict/);
      }
    }
    expect(verdictLabel("insufficient-n", zh())).toBe("评分会话不足");
    expect(verdictLabel("treatment-wins", zh())).toBe("候选版本胜出");
    expect(verdictLabel("some-new-code", zh())).toBe("some-new-code");
  });

  it("words every reason code in both languages with its params", () => {
    for (const code of VERDICT_REASON_CODES) {
      for (const t of [en(), zh()]) {
        const text = verdictReason({ verdict: "insufficient-n", reason: "EN", reason_code: code,
          reason_params: { primary: "p", min_arm_n: 1, control: 2, treatment: 3, planned: 4,
            alpha: 0.05, p: 0.5, delta: 0.1, evaluators: ["e"], judges: ["j"] }, metrics: [] }, t);
        expect(text).toBeTruthy();
        expect(text).not.toMatch(/canaryVerdict|\{\{/);
      }
    }
  });

  it("interpolates the per-arm counts and names the primary through nameOf", () => {
    const verdict = { verdict: "insufficient-n", reason: "a directional verdict needs …",
      reason_code: "primary_arm_n_below_bar",
      reason_params: { primary: "cust_1", min_arm_n: 10, control: 32, treatment: 4 }, metrics: [] };
    const text = verdictReason(verdict, zh(), (id) => (id === "cust_1" ? "hr_source_cited" : id));
    expect(text).toContain("hr_source_cited");
    expect(text).toContain("32");
    expect(text).toContain("4");
    expect(text).not.toContain("directional verdict");
  });

  it("falls back to the server text on an older payload or an unknown code", () => {
    expect(verdictReason({ verdict: "tie", reason: "old text", metrics: [] }, zh())).toBe("old text");
    expect(verdictReason({ verdict: "tie", reason: "old", reason_code: "brand_new", metrics: [] }, zh())).toBe("old");
    expect(verdictReason(undefined, zh())).toBeNull();
  });

  it("words the secondary-evaluator note from its count", () => {
    const v = { verdict: "tie", metrics: [], multiple_comparisons: "only the primary …", multiple_comparisons_k: 2 };
    expect(multipleComparisonsNote(v, zh())).toContain("2 个次要评估器");
    expect(multipleComparisonsNote({ ...v, multiple_comparisons_k: undefined }, zh())).toBe("only the primary …");
  });
});

/** N17-obs447: the stage card showed the canary's progress lines verbatim in English
 *  (「dispatching replay of 20 sessions to the evaluation worker…」); each line now carries a
 *  stable `progress_code` + params the console words in both languages. */
describe("canary progress copy (N17-obs447)", () => {
  const zh = () => i18next.getFixedT("zh-CN");
  const en = () => i18next.getFixedT("en");
  const params = { role: "control", control: 90, treatment: 10, sessions: 20, per_arm: 30, cap: 40,
    n: 3, needed: 4, status: "CREATING", key: "k.zip", timeout_s: 180, endpoints: ["a", "b"],
    version: "7", bucket: "b", source: "s", endpoint: "stableab", runtime: "rt", error: "E",
    poll: 2, interval_s: 5, phase: "evaluating", done: 1, total: 2, message: "" };

  it("words every code in both languages", () => {
    for (const code of CANARY_PROGRESS_CODES) {
      for (const t of [en(), zh()]) {
        const text = progressText({ progress: "EN", progress_code: code, progress_params: params }, t);
        expect(text, code).toBeTruthy();
        expect(text, code).not.toMatch(/canaryProgress|\{\{/);
      }
    }
  });

  it("reads the live examples in Chinese", () => {
    expect(progressText({ progress: "dispatching replay of 20 sessions to the evaluation worker…",
      progress_code: "dispatching_replay", progress_params: { sessions: 20 } }, zh()))
      .toBe("正在把 20 个会话发送给评估 worker 回放…");
    expect(progressText({ progress: "endpoint stableab status: CREATING", progress_code: "endpoint_status",
      progress_params: { endpoint: "stableab", status: "CREATING" } }, zh())).toBe("端点 stableab 状态：CREATING");
    expect(progressText({ progress: "minting candidate runtime version…", progress_code: "minting_candidate",
      progress_params: {} }, zh())).toBe("正在构建候选版本…");
    expect(progressText({ progress: "creating control online evaluation config…",
      progress_code: "creating_online_config", progress_params: { role: "control" } }, zh()))
      .toBe("正在创建当前版本的在线评估…");
  });

  it("falls back to the server text for a plain line, an older payload or an unknown code", () => {
    expect(progressText({ progress: "skill 'x' not found in registry — skipped", progress_code: null }, zh()))
      .toBe("skill 'x' not found in registry — skipped");
    expect(progressText({ progress: "old" }, zh())).toBe("old");
    expect(progressText({ progress: "new", progress_code: "brand_new", progress_params: {} }, zh())).toBe("new");
    expect(progressText({ progress: null }, zh())).toBeNull();
  });
});

/** N17-obs451: every completed canary read 「候选版本获胜…」, a tie promoted with the operator
 *  override included (live 0394f402 / 0871f702). The summary is worded by the verdict the
 *  canary was promoted on (`artifacts.complete.verdict`, or — completed before it was recorded
 *  — the verdict of the round it completed at). */
describe("completion summary (N17-obs451)", () => {
  const zh = () => i18next.getFixedT("zh-CN");
  const canary = (artifacts: Record<string, unknown>, status = "completed") =>
    ({ status, artifacts }) as unknown as RuntimeCanaryInfo;
  const done = (extra: Record<string, unknown> = {}) => ({ promoted_version: "7", completed_at_stage: 2, ...extra });
  const round = (verdict: Record<string, unknown>) => ({ ramp_stage: 2, verdict: { metrics: [], ...verdict } });

  it("says a significant win was a win", () => {
    const s = completionSummary(canary({ complete: done({ verdict: "treatment-wins", significant: true, override: false }) }), zh());
    expect(s).toEqual({ tone: "success", text: "候选版本显著胜出，已推广。生产现已服务 v7，A/B 测试已停止。" });
  });

  it("never claims a win for a tie promoted with the override", () => {
    const s = completionSummary(canary({ complete: done({ verdict: "tie", significant: false, override: true }) }), zh());
    expect(s?.tone).toBe("warn");
    expect(s?.text).toBe("判定为「无明显胜者」，已按操作员确认推广候选版本。生产现已服务 v7，A/B 测试已停止。");
    expect(s?.text).not.toMatch(/胜出/);
  });

  it("words a non-significant lead and an unrated win apart", () => {
    expect(completionSummary(canary({ complete: done({ verdict: "treatment-wins", significant: false, override: true }) }), zh())?.text)
      .toBe("候选版本领先但未达统计显著，已按操作员确认推广。生产现已服务 v7，A/B 测试已停止。");
    expect(completionSummary(canary({ complete: done({ verdict: "treatment-wins", override: false }) }), zh())?.text)
      .toBe("候选版本胜出，已推广。生产现已服务 v7，A/B 测试已停止。");
  });

  it("reads an older completion's verdict from the round it completed at", () => {
    const tie = completionSummary(canary({ complete: done(), rounds: [round({ verdict: "tie", significant: false })] }), zh());
    expect(tie?.text).toContain("无明显胜者");
    const win = completionSummary(canary({ complete: done(), rounds: [round({ verdict: "treatment-wins", significant: true })] }), zh());
    expect(win?.tone).toBe("success");
    expect(completionSummary(canary({ complete: done(), rounds: [] }), zh())?.text)
      .toBe("已推广候选版本（未记录判定）。生产现已服务 v7，A/B 测试已停止。");
  });

  it("keeps the rollback summary and names a cleanup without a decision", () => {
    expect(completionSummary(canary({ rollback: { winner: "champion", restored_version: "9" } }, "rolled_back"), zh())?.text)
      .toBe("已回滚到当前版本。生产服务 v9，A/B 测试已停止。");
    expect(completionSummary(canary({ setup: { ramp_stage: 0 } }, "cleaned"), zh())?.text).toBe("金丝雀已清理，未执行推广或回滚。");
    expect(completionSummary(canary({ setup: { ramp_stage: 0 } }, "running"), zh())).toBeNull();
    expect(completionSummary(canary({}, "cleaned"), zh())).toBeNull(); // nothing was ever set up
  });

  it("words every case in en too", () => {
    const en = i18next.getFixedT("en");
    for (const v of [{ verdict: "treatment-wins", significant: true, override: false }, { verdict: "tie", override: true },
      { verdict: "treatment-wins", significant: false, override: true }, {}]) {
      const text = completionSummary(canary({ complete: done(v) }), en)?.text ?? "";
      expect(text).toMatch(/v7/);
      expect(text).not.toMatch(/canaryPage|\{\{/);
    }
  });
});
