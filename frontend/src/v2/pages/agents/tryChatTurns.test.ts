import { describe, expect, it } from "vitest";

import type { ChatStreamPayload } from "../../../lib/api";
import {
  applyStreamEvent,
  awaitingReply,
  closeOpen,
  MAX_TURNS,
  pushTurn,
  type Turn,
  type TurnCopy,
} from "./tryChatTurns";

const copy: TurnCopy = {
  localize: (code, fallback) => (code ? `[${code}] ${fallback}` : fallback),
  sendFailed: "send failed",
  needsChat: "open chat",
};

function run(frames: [string, ChatStreamPayload][], start: Turn[] = []): Turn[] {
  return frames.reduce((turns, [event, data]) => applyStreamEvent(turns, event, data, copy), start);
}

const user: Turn = { role: "user", text: "hi" };

describe("applyStreamEvent", () => {
  it("streams deltas into one open agent bubble and closes it on done", () => {
    const streaming = run([["meta", { session_id: "s1" }], ["delta", { text: "Hel" }], ["delta", { text: "lo" }]], [user]);
    expect(streaming).toEqual([user, { role: "agent", text: "Hello", open: true }]);
    expect(applyStreamEvent(streaming, "done", {}, copy)).toEqual([user, { role: "agent", text: "Hello" }]);
  });

  it("a tool row closes the bubble, so the next delta opens a new one", () => {
    const turns = run([["delta", { text: "Let me look." }], ["tool", { name: "kb_search" }], ["delta", { text: "Found it." }]], [user]);
    expect(turns).toEqual([
      user,
      { role: "agent", text: "Let me look." },
      { role: "tool", text: "kb_search" },
      { role: "agent", text: "Found it.", open: true },
    ]);
  });

  it("a nameless tool falls back to 'tool'", () => {
    expect(run([["tool", {}]])).toEqual([{ role: "tool", text: "tool" }]);
  });

  it("consent asks and policy denials collapse into one pointer to Chat", () => {
    const turns = run([["auth_required", { provider: "github" }], ["policy_denied", { tool: "pay" }], ["policy_denied", {}]], [user]);
    expect(turns).toEqual([user, { role: "notice", text: "open chat" }]);
  });

  it("error frames are localized by code, with the send-failed fallback", () => {
    expect(run([["error", { code: "chat.x", message: "boom" }]])).toEqual([{ role: "error", text: "[chat.x] boom" }]);
    expect(run([["error", {}]])).toEqual([{ role: "error", text: "send failed" }]);
  });

  it("meta and unknown frames change nothing", () => {
    const turns = [user];
    expect(applyStreamEvent(turns, "meta", { session_id: "s" }, copy)).toBe(turns);
    expect(applyStreamEvent(turns, "saved", {}, copy)).toBe(turns);
  });
});

describe("pushTurn / closeOpen", () => {
  it("keeps only the last MAX_TURNS turns", () => {
    let turns: Turn[] = [];
    for (let i = 0; i < MAX_TURNS + 3; i += 1) turns = pushTurn(turns, { role: "user", text: String(i) });
    expect(turns).toHaveLength(MAX_TURNS);
    expect(turns[0].text).toBe("3");
  });

  it("closeOpen is a no-op without an open bubble", () => {
    const turns = [user, { role: "agent" as const, text: "done" }];
    expect(closeOpen(turns)).toBe(turns);
    expect(closeOpen([])).toEqual([]);
  });
});

describe("awaitingReply", () => {
  it("shows the placeholder only while busy and before text streams", () => {
    expect(awaitingReply([user], false)).toBe(false);
    expect(awaitingReply([user], true)).toBe(true);
    expect(awaitingReply([user, { role: "tool", text: "t" }], true)).toBe(true);
    expect(awaitingReply([user, { role: "agent", text: "x", open: true }], true)).toBe(false);
  });
});
