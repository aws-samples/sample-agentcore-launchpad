import i18next, { type TFunction } from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { localizedNote, type PlanNoteDetail } from "./plan-notes";

/**
 * N17-obs308: the evaluation plan keeps the platform's English notes; `note_details`
 * (backend `plan_notes.py`) codes them and the zh-CN console renders Chinese. The parts
 * below are the shapes `tests/test_assistant_plan_notes.py` pins on the backend.
 */

let tEn: TFunction;
let tZh: TFunction;

beforeAll(async () => {
  const inst = i18next.createInstance();
  await inst.init({
    lng: "en", fallbackLng: "en", defaultNS: "common", interpolation: { escapeValue: false },
    resources: { en: { common: en }, "zh-CN": { common: zh } },
  });
  tEn = inst.getFixedT("en");
  tZh = inst.getFixedT("zh-CN");
});

const ENTRY_NOTE = "seed note; gate from recommendation #2 (source: stated): blocking · threshold 1 · metric pass_rate";
const ENTRY: PlanNoteDetail = {
  message: ENTRY_NOTE,
  parts: [
    { code: "text", params: {}, text: "seed note" },
    {
      code: "evaluation_plan.note.gate_from_recommendation",
      params: { index: 2, source: "stated", blocking: true, threshold: 1, metric: "pass_rate" },
      text: "gate from recommendation #2 (source: stated): blocking · threshold 1 · metric pass_rate",
    },
  ],
};
const CARRIED_NOTE = "blocking · threshold 1 · metric adversarial_pass_rate — source: stated; carried onto refusal";
const CARRIED: PlanNoteDetail = {
  message: CARRIED_NOTE,
  parts: [{
    code: "evaluation_plan.note.gate_carried",
    params: { source: "stated", targets: ["refusal"], blocking: true, threshold: 1, metric: "adversarial_pass_rate" },
    text: CARRIED_NOTE,
  }],
};
const UNRESOLVED_NOTE = "UNRESOLVED gate: blocking · threshold 0.8 — source: stated; …";
const UNRESOLVED: PlanNoteDetail = {
  message: UNRESOLVED_NOTE,
  parts: [{
    code: "evaluation_plan.note.unresolved_gate",
    params: { source: "stated", blocking: true, threshold: 0.8, metric: null, why_code: "not_listed_or_seed", evaluator_id: "no-such-judge" },
    text: UNRESOLVED_NOTE,
  }],
};

describe("localizedNote (N17-obs308)", () => {
  it("renders an evaluator entry's gate tag in zh-CN and keeps the seed's own text", () => {
    expect(localizedNote(tZh, ENTRY_NOTE, ENTRY)).toBe(
      "seed note；门禁来自建议 #2（提案中明确给出）：阻断 · 阈值 1.00 · 指标 pass_rate");
    expect(localizedNote(tEn, ENTRY_NOTE, ENTRY)).toBe(
      "seed note; gate from recommendation #2 (stated in the proposal): blocking · threshold 1.00 · metric pass_rate");
  });

  it("renders the recommendation status notes in zh-CN", () => {
    expect(localizedNote(tZh, CARRIED_NOTE, CARRIED)).toBe(
      "阻断 · 阈值 1.00 · 指标 adversarial_pass_rate——提案中明确给出；已作用于 refusal");
    const unresolved = localizedNote(tZh, UNRESOLVED_NOTE, UNRESOLVED);
    expect(unresolved).toContain("未落实的门禁：阻断 · 阈值 0.80——提案中明确给出；没有评估器承载");
    expect(unresolved).toContain("（evaluator_id「no-such-judge」既不是已列出的评估器，也不是种子评估器的 key / 名称）");
    const unclassified = localizedNote(tZh, "no exact evaluator identified — classify or decline", {
      message: "no exact evaluator identified — classify or decline",
      parts: [{ code: "evaluation_plan.note.unclassified", params: { why_code: null }, text: "" }],
    });
    expect(unclassified).toBe("未识别出确切的评估器——请归类或拒绝");
  });

  it("renders a rename sentence in zh-CN", () => {
    const note = "renamed from 'refusal_judge': that evaluator name is already taken in this workspace";
    expect(localizedNote(tZh, note, {
      message: note, parts: [{ code: "evaluation_plan.note.renamed", params: { name: "refusal_judge" }, text: note }],
    })).toBe("已由「refusal_judge」改名：该评估器名称在本工作区已被占用");
  });

  it("falls back to the stored note without a matching detail", () => {
    expect(localizedNote(tZh, CARRIED_NOTE, undefined)).toBe(CARRIED_NOTE);
    expect(localizedNote(tZh, "edited by a member", CARRIED)).toBe("edited by a member");
    expect(localizedNote(tZh, CARRIED_NOTE, { message: CARRIED_NOTE, parts: [] })).toBe(CARRIED_NOTE);
  });
});
