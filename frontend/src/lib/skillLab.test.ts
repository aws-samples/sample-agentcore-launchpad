import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { SkillLabJobInfo, SkillLabStatus } from "./api";
import { setActiveRegion } from "./regions";
import { elapsed, skillLabJudgeSuggestions, skillLabTargetSuggestions } from "./skillLab";

const job = (started_at: string | null, finished_at: string | null = null) =>
  ({ started_at, finished_at }) as SkillLabJobInfo;

describe("elapsed", () => {
  // run in UTC+8 whatever the host zone: in UTC a naive string reads the same
  // either way, so the bug would pass unseen
  const hostTz = process.env.TZ;
  beforeAll(() => {
    process.env.TZ = "Asia/Shanghai";
  });
  afterAll(() => {
    if (hostTz === undefined) delete process.env.TZ;
    else process.env.TZ = hostTz;
  });
  afterEach(() => vi.useRealTimers());

  it("reads naive ledger times as UTC", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-13T06:00:30Z"));
    // naive: the ledger drops the offset. Read as local time it would sit 8 h in
    // the future and clamp to 0s.
    expect(elapsed(job("2026-10-13T06:00:00"))).toBe("30s");
  });

  it("measures a finished job between its own timestamps", () => {
    expect(elapsed(job("2026-10-13T06:00:00", "2026-10-13T06:03:00"))).toBe("3.0m");
    expect(elapsed(job("2026-10-13T06:00:00+00:00", "2026-10-13T06:00:45Z"))).toBe("45s");
  });

  it("shows a dash before the job starts", () => {
    expect(elapsed(job(null))).toBe("—");
  });
});

describe("model suggestions", () => {
  const status = {
    default_target_model: "global.anthropic.claude-opus-5-5",
    default_codex_target_model: "global.openai.gpt-6.1-sol",
    default_judge_model: "global.openai.gpt-6.1-sol",
  } as SkillLabStatus;
  const catalog = [
    "global.anthropic.claude-haiku-5-5",
    "global.moonshotai.kimi-k2.5",
    "global.openai.gpt-6-luna",
    "global.zai.glm-5.3",
  ];
  beforeAll(() => setActiveRegion("us-west-2"));
  afterAll(() => setActiveRegion(null));

  it("offers Opus / Sonnet 5.5 to the claude CLI and only Anthropic models", () => {
    const out = skillLabTargetSuggestions(status, "claude_code_exec", catalog);
    expect(out.slice(0, 2)).toEqual([
      "global.anthropic.claude-opus-5-5",
      "global.anthropic.claude-sonnet-5-5",
    ]);
    expect(out).toContain("global.anthropic.claude-haiku-5-5");
    expect(out.every((id) => id.includes(".anthropic."))).toBe(true);
  });

  it("offers the GPT-6.x and Kimi profiles to codex, never another family", () => {
    const out = skillLabTargetSuggestions(status, "codex_exec", catalog);
    expect(out[0]).toBe("global.openai.gpt-6.1-sol");
    for (const id of ["global.openai.gpt-6.1-terra", "global.openai.gpt-6-astra", "global.moonshotai.kimi-k3"]) {
      expect(out).toContain(id);
    }
    expect(out).toContain("global.moonshotai.kimi-k2.5");
    expect(out).not.toContain("global.zai.glm-5.3");
    expect(out.some((id) => id.includes(".anthropic."))).toBe(false);
  });

  it("suggests global judge profiles only", () => {
    const out = skillLabJudgeSuggestions(status, []);
    expect(out[0]).toBe("global.openai.gpt-6.1-sol");
    expect(out).toContain("global.anthropic.claude-sonnet-5-5");
    expect(out.every((id) => id.startsWith("global."))).toBe(true);
  });
});
