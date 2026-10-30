import { describe, expect, it } from "vitest";

import type { AssistantProposal } from "./api";
import { type AssistantLiveMessage, PROPOSAL_MISSING, PROPOSAL_MISSING_PENDING,
  PROPOSAL_MISSING_RECOVERED, retryableTurn, toLiveMessages } from "./assistant";
import { applyTurnEvent } from "./assistant-turn";

/**
 * N17-obs468: a reply without a proposal block that imitated the replay placeholder or
 * claimed a submitted revision — the server asks once more in the same turn. The live
 * transcript closes the reply, shows a pending notice naming the unchanged revision,
 * streams the follow-up below it and settles the notice on the proposal event; the notice
 * is never a failed turn.
 */

const translate = (_c: string | null, m: string) => m;
const start: AssistantLiveMessage[] = [
  { role: "user", text: "更新提案", turn: 2 },
  { role: "assistant", text: "修订已提交。", streaming: true },
];

describe("re-ask of a reply without a proposal block (N17-obs468)", () => {
  it("closes the reply, shows the pending notice and settles it on the proposal", () => {
    let rows = applyTurnEvent(start, { event: "reask", data: { revision: 1, reason: "replay_marker" } as never }, translate);
    expect(rows.map((r) => [r.role, r.name ?? null, !!r.streaming])).toEqual([
      ["user", null, false], ["assistant", null, false],
      ["error", PROPOSAL_MISSING_PENDING, false], ["assistant", null, true],
    ]);
    expect(rows[2].missing).toEqual({ base: 1, revision: null });
    rows = applyTurnEvent(rows, { event: "delta", data: { text: "已更新。" } as never }, translate);
    const replayed = applyTurnEvent(rows, { event: "retry", data: { attempt: 1, phase: "reask" } as never }, translate);
    expect(replayed.map((r) => r.text)).toEqual(["更新提案", "修订已提交。", "", ""]);
    const done = applyTurnEvent(rows, {
      event: "proposal", data: { status: "draft", revision: 2, validation_errors: [] } as unknown as AssistantProposal as never,
    }, translate);
    expect(done[2]).toMatchObject({ name: PROPOSAL_MISSING_RECOVERED, missing: { base: 1, revision: 2 } });
  });

  it("is a notice, not a failed turn, in every outcome", () => {
    for (const name of [PROPOSAL_MISSING_PENDING, PROPOSAL_MISSING, PROPOSAL_MISSING_RECOVERED]) {
      expect(retryableTurn([
        { role: "user", text: "更新提案", turn: 2 },
        { role: "assistant", text: "x", turn: 2 },
        { role: "error", name, text: "", turn: 2 },
      ])).toBeNull();
    }
  });

  it("keeps the stored notice's revision when the transcript reloads", () => {
    const [row] = toLiveMessages([{ id: 1, turn: 2, role: "error", name: PROPOSAL_MISSING, text: "x",
      author: null, at: null, missing: { base: 1, revision: null } }]);
    expect(row.missing).toEqual({ base: 1, revision: null });
  });
});
