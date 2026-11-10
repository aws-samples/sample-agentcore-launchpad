import { expect, type Page } from "@playwright/test";
import { ledger, stateGet, stateSet } from "./helpers";

/**
 * Shared helpers of the architect-assistant specs (11c re-test, 11d re-test 2): one conversation,
 * one turn, the stored proposal and the prose the member reads. Kept out of the spec files so the
 * two re-tests assert the same way.
 */

export const ALLOWED = ["global.zai.glm-5.3", "global.moonshotai.kimi-k3"];
export const SILICONFLOW_SSM = "launchpad-siliconflow-api-key";
export const MEMORY_RE = /memor|remember|recall|across (sessions|conversations)|记忆|记住/i;
export const QUESTION_RE = /\?|？/;
/** The trajectory-evaluator validation rule (N-ARCH-obs10). */
export const TRAJECTORY_RULE_RE = /expected_trajectory/i;

export function stamp(): string {
  return new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
}

export function rememberConversation(id: string | null): void {
  if (!id) return;
  const cur = stateGet<string[]>("arch_conversations") ?? [];
  if (!cur.includes(id)) stateSet("arch_conversations", [...cur, id]);
}

export async function newConversation(page: Page): Promise<string> {
  await page.goto("/v2/assistant");
  await page.getByTestId("v2-assistant-new").click();
  await expect(page.getByTestId("v2-assistant-detail")).toBeVisible({ timeout: 60_000 });
  const id = await page.getByTestId("v2-assistant-detail").getAttribute("data-conversation");
  rememberConversation(id);
  return id!;
}

export interface Turn { seconds: number; errors: number; reply: string; status: string; conv: Record<string, unknown> }

/**
 * Ceiling for one architect turn. 1.6.1 proposal turns run 470–520 s on the default host (10-12:
 * 250–400 s): with the earlier 8-min ceiling the test gave up while the architect was still
 * streaming, Playwright closed the page, the backend closed the Harness stream and the half-written
 * proposal was lost (arch-retest2 run A, rt2-1a/1b).
 */
export const TURN_CEILING_MS = 15 * 60 * 1000;

/** One architect turn; returns the new thread text, the stored proposal_status and the conversation. */
export async function sendTurn(page: Page, id: string, prompt: string): Promise<Turn> {
  const before = (await page.getByTestId("v2-assistant-thread").innerText()).length;
  const t0 = Date.now();
  await page.getByTestId("v2-assistant-input").fill(prompt);
  await page.getByTestId("v2-assistant-send").click();
  await expect(page.getByTestId("v2-assistant-streaming")).toHaveCount(0, { timeout: TURN_CEILING_MS });
  await expect(page.getByTestId("v2-assistant-input")).toBeEnabled({ timeout: 60_000 });
  const errors = await page.getByTestId("v2-assistant-turn-error").count();
  const thread = await page.getByTestId("v2-assistant-thread").innerText();
  const conv = (await (await page.request.get(`/api/assistant/architect/conversations/${id}`)).json()) as Record<string, unknown>;
  return { seconds: Math.round((Date.now() - t0) / 1000), errors, reply: thread.slice(before), status: String(conv.proposal_status ?? "none"), conv };
}

export type Proposal = { revision: number; status: string; content: Record<string, unknown>; validation_errors?: string[]; content_hash?: string };

export function latestProposal(conv: Record<string, unknown>): Proposal | undefined {
  const proposals = (conv.proposals ?? []) as Proposal[];
  return proposals.filter((p) => p.status !== "superseded").sort((a, b) => b.revision - a.revision)[0] ?? proposals[proposals.length - 1];
}

/** The first revision the architect emitted in this conversation (N-ARCH-obs10 accounting). */
export function firstProposal(conv: Record<string, unknown>): Proposal | undefined {
  const proposals = (conv.proposals ?? []) as Proposal[];
  return [...proposals].sort((a, b) => a.revision - b.revision)[0];
}

/** The architect's last reply text without fenced blocks (the prose the member reads). */
export function lastAssistantProse(conv: Record<string, unknown>): string {
  const msgs = ((conv.messages ?? []) as Array<{ role: string; text?: string }>).filter((m) => m.role === "assistant");
  return String(msgs[msgs.length - 1]?.text ?? "").replace(/```[\s\S]*?```/g, "");
}

/**
 * The prose without markdown table rows and quoted strings — the architect's design tables quote
 * sample employee questions ("How many vacation days do I have left?"), which are not questions put
 * to the member (arch-retest2 run A, rt2-1b false positive).
 */
export function questionProse(prose: string): string {
  return prose
    .split("\n")
    .filter((line) => !/^\s*\|/.test(line))
    .join("\n")
    .replace(/"[^"\n]*"|“[^”\n]*”|「[^」\n]*」/g, '""');
}

/**
 * A request for the member's answer: a question mark, or the architect's imperative option style
 * ("(a) workspace … (b) disabled … Reply a or b" / "pick an option" — arch-retest2 run B, rt2-1a:
 * the memory item carried no question mark at all). Deliberately NOT "your decision" / a bare
 * "reply": the design recap writes "workspace (your decision)" and "no after-hours auto-reply"
 * (run C false positive).
 */
export const ASKS_RE = /\?|？|\b(please )?reply (a|b|with|a or b|one|here)\b|\bpick (an option|a or b|one)\b|\bchoose (one|a or b)\b|\b(answer|tell me) (a or b|which)\b|\byour call\b|请(回复|选择|告诉)/i;
/** Two or more labelled options "(a) … (b) …" in one block — the architect's decision-item style (run C turn 1 had no asking phrase at all). */
export const OPTION_LABEL_RE = /\(\s*[a-d]\s*\)/gi;
export function offersOptions(block: string): boolean {
  return (block.match(OPTION_LABEL_RE) ?? []).length >= 2;
}

/** Paragraphs / top-level numbered items of the prose — an item keeps its option sub-bullets. */
export function decisionBlocks(prose: string): string[] {
  return questionProse(prose).split(/\n\s*\n|\n(?=(?:\*\*)?\d+[.)]\s)/);
}

/** Does the prose put a memory decision/question to the member (same list item / paragraph)? */
export function asksAboutMemory(prose: string): boolean {
  return decisionBlocks(prose).some((block) => MEMORY_RE.test(block) && (ASKS_RE.test(block) || offersOptions(block)));
}

/** Does the prose ask the member anything (outside tables and quoted examples)? */
export function asksMember(prose: string): boolean {
  return decisionBlocks(prose).some((block) => ASKS_RE.test(block) || offersOptions(block));
}

/** Error rows of a turn that are turn FAILURES — not the designed validation feedback / replay notes. */
export function turnFailures(conv: Record<string, unknown>, turn?: number): string[] {
  const msgs = (conv.messages ?? []) as Array<{ role: string; name?: string | null; text?: string; turn?: number }>;
  return msgs
    .filter((m) => m.role === "error" && !["proposal_rejected", "preparation_rejected", "turn_retried"].includes(String(m.name ?? "")) && (turn === undefined || m.turn === turn))
    .map((m) => String(m.text ?? "").slice(0, 300));
}

/** Does the prose carry a numbered list ("1." / "1)")? */
export function hasNumberedList(prose: string): boolean {
  return /(^|\n)\s*(\*\*)?1[.)]/.test(prose);
}

export async function rejectIfOffered(page: Page, label: string): Promise<void> {
  const reject = page.getByTestId("v2-assistant-proposal-reject");
  if (!(await reject.isVisible().catch(() => false))) return;
  const rej = page.waitForResponse((r) => r.url().includes("/proposal/reject") && r.request().method() === "POST");
  await reject.click();
  const ok = page.getByTestId("v2-confirm-ok");
  if (await ok.isVisible({ timeout: 3000 }).catch(() => false)) await ok.click();
  ledger(`${label} reject → ${(await rej).status()}`);
}

export interface FirstProposalRecord { label: string; revision?: number; status?: string; errors: number; trajectoryErrors: number }

/** Record the first revision of a conversation for the N-ARCH-obs10 ratio (shared state + ledger). */
export function recordFirstProposal(label: string, conv: Record<string, unknown>): FirstProposalRecord {
  const first = firstProposal(conv);
  const errs = first?.validation_errors ?? [];
  const rec: FirstProposalRecord = { label, revision: first?.revision, status: first?.status, errors: errs.length, trajectoryErrors: errs.filter((e) => TRAJECTORY_RULE_RE.test(e)).length };
  const cur = stateGet<FirstProposalRecord[]>("arch_first_proposals") ?? [];
  stateSet("arch_first_proposals", [...cur.filter((r) => r.label !== label), rec]);
  ledger(`obs10 first proposal ${label}: revision=${rec.revision} status=${rec.status} errors=${rec.errors} trajectory-rule=${rec.trajectoryErrors}`);
  return rec;
}
