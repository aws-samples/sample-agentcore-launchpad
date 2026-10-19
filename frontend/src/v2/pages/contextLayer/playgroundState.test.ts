import { describe, expect, it } from "vitest";

import type { ContextLayerDoneEvent } from "../../../lib/api";
import {
  answerOf,
  applyEvent,
  cellText,
  confidencePct,
  conversationId,
  failTurn,
  finishTurn,
  MAX_STEPS,
  newTurn,
  numericColumn,
  type PlaygroundTurn,
  retryQuestion,
  tierLabel,
} from "./playgroundState";

const QUESTION = "每个部门各有多少名员工？";
const done = (over: Partial<ContextLayerDoneEvent> = {}): ContextLayerDoneEvent => ({
  answer: "共 6 个部门。",
  tier: 2,
  confidence: 0.92,
  rationale: "governed table",
  columns: ["department", "employee_count"],
  rows: [{ department: "Engineering", employee_count: 41 }],
  row_count: 6,
  rows_truncated: false,
  partial: false,
  guardrail_blocked: false,
  data_sources: [],
  citations: [],
  total_ms: 17200,
  session_id: "coa-session-1",
  ...over,
});

const play = (turn: PlaygroundTurn, events: [string, unknown][]) =>
  events.reduce((acc, [event, data]) => applyEvent(acc, event, data), turn);

describe("a playground turn", () => {
  it("streams steps and tokens, then ends on done", () => {
    const turn = play(newTurn(1, QUESTION), [
      ["step", { name: "T1_METRIC_MATCH", status: "skipped", duration_ms: 120, tool: null, detail: null }],
      ["step", { name: "T2_NL_TO_SQL", status: "success", duration_ms: 9800, tool: "athena", detail: null }],
      ["token", { text: "各部门" }],
      ["token", { text: "员工数如下。" }],
      ["done", done()],
    ]);
    expect(turn.phase).toBe("done");
    expect(turn.steps.map((s) => s.name)).toEqual(["T1_METRIC_MATCH", "T2_NL_TO_SQL"]);
    expect(turn.text).toBe("各部门员工数如下。");
    expect(answerOf(turn)).toBe("共 6 个部门。");
  });

  it("shows the streamed text when COA synthesized no answer", () => {
    const turn = play(newTurn(1, QUESTION), [["token", { text: "partial" }], ["done", done({ answer: null })]]);
    expect(answerOf(turn)).toBe("partial");
  });

  it("ignores anything after the turn ended, and malformed events", () => {
    const ended = play(newTurn(1, QUESTION), [["done", done()], ["token", { text: "late" }], ["error", { code: "x" }]]);
    expect(ended.phase).toBe("done");
    expect(ended.text).toBe("");
    const same = newTurn(2, QUESTION);
    expect(applyEvent(same, "token", "not an object")).toBe(same);
    expect(applyEvent(same, "token", { text: 3 })).toBe(same);
    expect(applyEvent(same, "meta", { x: 1 })).toBe(same);
  });

  it("keeps a bounded trace", () => {
    let turn = newTurn(1, QUESTION);
    for (let i = 0; i < MAX_STEPS + 20; i += 1) {
      turn = applyEvent(turn, "step", { name: `s${i}`, status: "success", duration_ms: 1, tool: null, detail: null });
    }
    expect(turn.steps).toHaveLength(MAX_STEPS);
  });

  it("ends on a COA error with its code", () => {
    const turn = applyEvent(newTurn(1, QUESTION), "error", {
      code: "context_layer.forbidden",
      message: "Access denied",
      status: 403,
      coa_error: "AccessDenied",
    });
    expect(turn.phase).toBe("error");
    expect(turn.error?.code).toBe("context_layer.forbidden");
  });

  it("turns auth_required into the console-grant consent card", () => {
    const turn = applyEvent(newTurn(1, QUESTION), "auth_required", {
      provider: "coa-coa-w2",
      tool: "context_layer",
      url: "https://coa-auth.example/authorize?state=x",
      scopes: ["openid", "email", "profile"],
      agent_id: "",
    });
    expect(turn.phase).toBe("auth");
    expect(turn.auth).toEqual({
      provider: "coa-coa-w2",
      tool: "context_layer",
      url: "https://coa-auth.example/authorize?state=x",
      scopes: ["openid", "email", "profile"],
      agent_id: "",
    });
    expect(retryQuestion(turn)).toBe(QUESTION);
    // a non-https URL never becomes a link
    const unsafe = applyEvent(newTurn(2, QUESTION), "auth_required", { provider: "p", url: "javascript:alert(1)" });
    expect(unsafe.auth?.url).toBeNull();
  });

  it("is stopped when the member aborts, failed when the stream just ends", () => {
    expect(finishTurn(newTurn(1, QUESTION), true).phase).toBe("stopped");
    const dropped = finishTurn(newTurn(1, QUESTION), false);
    expect(dropped.phase).toBe("error");
    expect(dropped.error?.code).toBe("context_layer.upstream");
    const finished = play(newTurn(1, QUESTION), [["done", done()]]);
    expect(finishTurn(finished, false)).toBe(finished);
  });

  it("records a request that failed before any stream", () => {
    const turn = failTurn(newTurn(1, QUESTION), "context_layer.not_connected", "no layer");
    expect(turn.error).toEqual({ code: "context_layer.not_connected", message: "no layer", status: null, coa_error: null });
  });
});

describe("the conversation", () => {
  it("continues COA's newest conversation id", () => {
    const first = play(newTurn(1, "a"), [["done", done({ session_id: "s-1" })]]);
    const second = play(newTurn(2, "b"), [["done", done({ session_id: "s-2" })]]);
    const failed = play(newTurn(3, "c"), [["error", { code: "context_layer.upstream", message: "x" }]]);
    expect(conversationId([])).toBeNull();
    expect(conversationId([first])).toBe("s-1");
    expect(conversationId([first, second, failed])).toBe("s-2");
    expect(conversationId([play(newTurn(1, "a"), [["done", done({ session_id: null })]])])).toBeNull();
  });
});

describe("formatting", () => {
  it("labels tiers and confidence", () => {
    expect(tierLabel(2)).toBe("T2");
    expect(tierLabel(2.5)).toBe("T2.5");
    expect(tierLabel(null)).toBeNull();
    expect(confidencePct(0.924)).toBe(92);
    expect(confidencePct(1.4)).toBe(100);
    expect(confidencePct(null)).toBeNull();
  });

  it("renders cells and spots numeric columns", () => {
    expect(cellText(null)).toBe("—");
    expect(cellText(41)).toBe("41");
    expect(cellText(false)).toBe("false");
    expect(cellText({ a: 1 })).toBe('{"a":1}');
    const rows = [{ n: 1, s: "x" }, { n: null, s: "y" }];
    expect(numericColumn(rows, "n")).toBe(true);
    expect(numericColumn(rows, "s")).toBe(false);
    expect(numericColumn([], "n")).toBe(false);
  });
});
