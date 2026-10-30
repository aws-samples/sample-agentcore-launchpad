import { describe, expect, it } from "vitest";

import type { AssistantProposal, ValidationErrorDetail } from "./api";
import { type AssistantLiveMessage, PROPOSAL_AUTOCORRECTED, PROPOSAL_AUTOCORRECT_FAILED,
  PROPOSAL_AUTOCORRECT_PENDING, retryableTurn } from "./assistant";
import { applyTurnEvent } from "./assistant-turn";

/**
 * N17-obs295: a proposal that failed only structural evaluation-plan rules is corrected by
 * the server with one internal call. The live transcript closes the reply, shows a notice
 * and settles it on the proposal event; a replay of the correction call drops only what it
 * streamed; the notice is never a failed (retryable) turn.
 */

const REF = "evaluation_plan.evaluators.runtime_writes_and_trajectory: scenario 'gt1-policy-qna' "
  + "has no expected_trajectory — a reference-driven evaluator is applied to every session, so "
  + "every scenario must carry it";
const DETAIL: ValidationErrorDetail = {
  code: "evaluation_plan.reference_missing",
  params: { evaluator: "runtime_writes_and_trajectory", scenario: "gt1-policy-qna", fields: ["expected_trajectory"] },
  message: REF,
};

describe("automatic correction in the live transcript (N17-obs295)", () => {
  const start: AssistantLiveMessage[] = [
    { role: "user", text: "design it", turn: 1 },
    { role: "assistant", text: "Here is the design.", streaming: true },
  ];
  const proposal = (status: "draft" | "invalid") => ({
    status, revision: status === "draft" ? 2 : 1,
    validation_errors: status === "draft" ? [] : [REF],
    validation_error_details: status === "draft" ? [] : [DETAIL],
  }) as unknown as AssistantProposal;
  const translate = (_c: string | null, m: string) => m;

  it("closes the reply, shows a pending notice and settles it on the proposal", () => {
    let rows = applyTurnEvent(start, { event: "autocorrect", data: { revision: 1, errors: [REF], validation_error_details: [DETAIL] } as never }, translate);
    expect(rows.map((r) => [r.role, r.name ?? null, !!r.streaming])).toEqual([
      ["user", null, false], ["assistant", null, false],
      ["error", PROPOSAL_AUTOCORRECT_PENDING, false], ["assistant", null, true],
    ]);
    expect(rows[2].errors).toEqual([DETAIL]);
    rows = applyTurnEvent(rows, { event: "delta", data: { text: "Fixed." } as never }, translate);
    // a transient replay of the correction call drops only what the correction streamed
    const replayed = applyTurnEvent(rows, { event: "retry", data: { attempt: 1, phase: "autocorrect" } as never }, translate);
    expect(replayed.map((r) => r.text)).toEqual(["design it", "Here is the design.", `- ${REF}`, ""]);
    const done = applyTurnEvent(rows, { event: "proposal", data: proposal("draft") as never }, translate);
    expect(done[2].name).toBe(PROPOSAL_AUTOCORRECTED);
    expect(done).toHaveLength(4);
    const failed = applyTurnEvent(rows, { event: "proposal", data: proposal("invalid") as never }, translate);
    expect(failed[2].name).toBe(PROPOSAL_AUTOCORRECT_FAILED);
    expect(failed[4]).toMatchObject({ role: "error", name: "proposal_rejected", errors: [DETAIL] });
  });

  it("is a notice, not a failed turn", () => {
    const rows: AssistantLiveMessage[] = [
      { role: "user", text: "design it", turn: 1 },
      { role: "assistant", text: "x", turn: 1 },
      { role: "error", name: PROPOSAL_AUTOCORRECT_FAILED, text: `- ${REF}`, turn: 1 },
      { role: "error", name: "proposal_rejected", text: `- ${REF}`, turn: 1 },
    ];
    expect(retryableTurn(rows)).toBeNull();
    expect(retryableTurn([...rows, { role: "error", name: null, text: "boom", turn: 1 }])).toEqual({ turn: 1, prompt: "design it" });
  });

  it("settles only the current turn's notice, never an earlier turn's", () => {
    const rows: AssistantLiveMessage[] = [
      { role: "user", text: "first", turn: 1 },
      { role: "error", name: PROPOSAL_AUTOCORRECT_PENDING, text: `- ${REF}`, turn: 1 },
      { role: "user", text: "second" },
      { role: "assistant", text: "", streaming: true },
    ];
    const next = applyTurnEvent(rows, { event: "proposal", data: proposal("draft") as never }, translate);
    expect(next[1].name).toBe(PROPOSAL_AUTOCORRECT_PENDING);
    const current = applyTurnEvent([...rows.slice(0, 3),
      { role: "error", name: PROPOSAL_AUTOCORRECT_PENDING, text: `- ${REF}` }],
    { event: "proposal", data: proposal("draft") as never }, translate);
    expect(current.map((r) => r.name ?? null)).toEqual([null, PROPOSAL_AUTOCORRECT_PENDING, null, PROPOSAL_AUTOCORRECTED]);
  });
});
