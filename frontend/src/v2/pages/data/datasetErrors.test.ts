// @vitest-environment jsdom
import i18next from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { datasetErrorText } from "./datasetErrors";

// N17-obs424 / obs318: a dataset refusal reads in the console's language, naming where
const t = i18next.t.bind(i18next);

beforeAll(async () => {
  await i18next.init({ lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common", interpolation: { escapeValue: false } });
});

describe("datasetErrorText", () => {
  it("states an invalid record and turn in Chinese", () => {
    const err = new ApiError("dataset.invalid_item", "item 2 turn 3: input required", { reason: "turn_input_required", item: 2, turn: 3 });
    expect(datasetErrorText(t, err)).toBe("第 2 条记录第 3 轮：这一轮的输入为空");
  });

  it("names the JSONL line", () => {
    expect(datasetErrorText(t, new ApiError("dataset.invalid_jsonl", "line 4: Expecting value", { line: 4 }))).toBe("第 4 行不是合法的 JSON。");
  });

  it("lists unknown tools, suggestions and the agent's tools", () => {
    const err = new ApiError("dataset.unknown_tool", "…", {
      unknown: { s1: ["kb_serch"] },
      agent_tools: ["kb_search", "launchpad_gw_hr___get"],
      suggestions: { kb_serch: ["kb_search"] },
    });
    const text = datasetErrorText(t, err);
    expect(text).toContain("s1: kb_serch");
    expect(text).toContain("kb_serch 是否应为 kb_search");
    expect(text).toContain("该智能体的工具：kb_search, launchpad_gw_hr___get");
  });

  it("every new zh-CN dataset string is Chinese (no raw English words)", () => {
    const walk = (o: unknown, path: string, out: string[]) => {
      if (typeof o === "string") out.push(`${path}=${o}`);
      else if (o && typeof o === "object") for (const [k, v] of Object.entries(o)) walk(v, `${path}.${k}`, out);
      return out;
    };
    const keys = ["revisionHint", "assertions", "assertionsHint", "trajectory", "trajectoryChecked", "trajectoryUnchecked", "splitHint", "splitDefault", "scenariosSub", "rewritesTitle", "errors", "colScenario", "scenarioId", "addTurn", "removeTurn"];
    const ds = (zh as { v2: { datasets: Record<string, unknown> } }).v2.datasets;
    const strings = keys.flatMap((k) => walk(ds[k], k, []));
    for (const s of strings) {
      // placeholders, the format names JSON / JSONL and code identifiers in braces are not prose
      const prose = s.split("=").slice(1).join("=").replace(/\{\{[^}]+\}\}/g, "").replace(/\{[^}]*\}/g, "").replace(/JSONL?|r\d*/g, "");
      expect(prose, s).not.toMatch(/[A-Za-z]{3,}/);
    }
  });
});
