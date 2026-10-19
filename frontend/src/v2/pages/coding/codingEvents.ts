// The coding conversation as a pure reducer over the runtime's seq-numbered event
// records (codex app-server notifications + `launchpad/*` platform events, parent
// design §2.3). History is replayed compact — the deltas of every completed item are
// dropped — so `item/completed` always replaces the whole item, and a compact replay
// renders the same conversation as the live stream did. Records at or below the
// state's `seq` are ignored, which makes re-delivery (a reconnect overlap) harmless.
// Type-only imports keep this loadable in the node test environment.
import type { CodingTurnRow } from "../../../lib/api";
import { parseTimestamp } from "../../../lib/timestamps";
import { displayPath, projectPath } from "./paths";

export interface CodingEventRecord {
  seq: number;
  ts?: number | string | null;
  method: string;
  params?: Record<string, unknown> | null;
}

export type TurnStatus = "inProgress" | "completed" | "interrupted" | "failed" | "abandoned";
export type ItemStatus = "inProgress" | "completed" | "failed" | "declined";
export type FileChangeKind = "add" | "delete" | "update";
export type NoticeCode = "appServerExited" | "serverRequestDeclined" | "restored" | "threadResumeFailed";

/** One file of a `fileChange` item. Only the counts are kept: codex sends an added
 *  file's whole content as its "diff", and the Changes tab reads diffs from the
 *  workspace anyway, so keeping the text would hold every written file in memory.
 *  codex reports absolute paths (`/mnt/workspace/project/README.md`); `path` and
 *  `movePath` are project-relative when inside the project, else as reported. */
export interface FileChangeView {
  path: string;
  kind: FileChangeKind;
  movePath: string | null;
  /** the project-relative path the Changes tab lists this change under (a move's
   *  destination); null when the file is outside the project (nothing to diff there) */
  openPath: string | null;
  added: number;
  removed: number;
}

export type ItemView =
  | { type: "userMessage"; id: string; text: string }
  | { type: "agentMessage"; id: string; text: string; streaming: boolean }
  | { type: "reasoning"; id: string; summary: string[]; content: string[]; streaming: boolean }
  | {
      type: "commandExecution";
      id: string;
      command: string;
      cwd: string | null;
      output: string;
      /** the head of a very long output was dropped (OUTPUT_CAP) */
      truncated: boolean;
      exitCode: number | null;
      status: ItemStatus;
      durationMs: number | null;
    }
  | { type: "fileChange"; id: string; changes: FileChangeView[]; status: ItemStatus }
  | {
      type: "mcpToolCall";
      id: string;
      server: string | null;
      tool: string;
      status: ItemStatus;
      error: string | null;
      durationMs: number | null;
    }
  | { type: "plan"; id: string; text: string; streaming: boolean }
  | { type: "error"; id: string; message: string; willRetry: boolean }
  | { type: "notice"; id: string; code: NoticeCode; detail: string | null }
  | { type: "other"; id: string; itemType: string; label: string | null };

export interface TokenUsage {
  input: number;
  cachedInput: number;
  cacheWrite: number;
  output: number;
  reasoningOutput: number;
  total: number;
}

export interface PlanStepView {
  step: string;
  status: string;
}

export interface TurnView {
  turnId: string;
  /** the ledger row id (`coding_turns.id`), once the project detail named it */
  rowId: string | null;
  prompt: string | null;
  model: string | null;
  effort: string | null;
  status: TurnStatus;
  items: ItemView[];
  /** the turn's unified diff (`turn/diff/updated`, last one wins) */
  diff: string | null;
  plan: { explanation: string | null; steps: PlanStepView[] } | null;
  usage: TokenUsage | null;
  /** epoch ms */
  startedAt: number | null;
  completedAt: number | null;
  durationMs: number | null;
  error: string | null;
}

/** A platform notice outside any turn, shown before turn `at` (`turns.length` = last). */
export interface ConversationNotice {
  id: string;
  code: NoticeCode;
  detail: string | null;
  at: number;
}

export interface ConversationState {
  seq: number;
  turns: TurnView[];
  activeTurnId: string | null;
  threadId: string | null;
  notices: ConversationNotice[];
}

/** Command output kept per item; a longer output keeps its tail. */
export const OUTPUT_CAP = 200_000;

export function emptyConversation(): ConversationState {
  return { seq: 0, turns: [], activeTurnId: null, threadId: null, notices: [] };
}

/* ---------- small readers ---------- */

type Obj = Record<string, unknown>;

function obj(value: unknown): Obj {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Obj) : {};
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((v) => (typeof v === "string" ? v : "")) : [];
}

/** Epoch ms from epoch seconds, epoch ms or an ISO string (naive = UTC). */
export function toMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value < 1e12 ? value * 1000 : value;
  if (typeof value === "string") return parseTimestamp(value)?.getTime() ?? null;
  return null;
}

function itemStatus(value: unknown, fallback: ItemStatus): ItemStatus {
  return value === "inProgress" || value === "completed" || value === "failed" || value === "declined"
    ? value
    : fallback;
}

/** app-server `TurnStatus` or a ledger status → the view's status. */
export function turnStatusOf(value: unknown): TurnStatus | null {
  switch (value) {
    case "inProgress":
    case "running":
      return "inProgress";
    case "completed":
    case "interrupted":
    case "failed":
    case "abandoned":
      return value;
    default:
      return null;
  }
}

/** Token usage from codex's `TokenUsageBreakdown` (camelCase) or the ledger's
 *  `{input, cached_input, cache_write, output, reasoning_output}`. */
export function normalizeUsage(raw: unknown): TokenUsage | null {
  const u = obj(raw);
  const pick = (...keys: string[]) => {
    for (const key of keys) {
      const v = num(u[key]);
      if (v !== null) return v;
    }
    return null;
  };
  const input = pick("inputTokens", "input", "input_tokens");
  const output = pick("outputTokens", "output", "output_tokens");
  const cachedInput = pick("cachedInputTokens", "cached_input", "cache_read");
  const cacheWrite = pick("cacheWriteInputTokens", "cache_write");
  const reasoningOutput = pick("reasoningOutputTokens", "reasoning_output");
  const total = pick("totalTokens", "total", "total_tokens");
  if ([input, output, cachedInput, cacheWrite, reasoningOutput, total].every((v) => v === null)) return null;
  return {
    input: input ?? 0,
    cachedInput: cachedInput ?? 0,
    cacheWrite: cacheWrite ?? 0,
    output: output ?? 0,
    reasoningOutput: reasoningOutput ?? 0,
    total: total ?? (input ?? 0) + (output ?? 0),
  };
}

/** `+` / `-` line counts of one file change. A unified diff is counted line by line;
 *  codex sends an added (deleted) file's whole content instead, counted as added (removed). */
export function diffStats(kind: FileChangeKind, diff: string): { added: number; removed: number } {
  if (!diff) return { added: 0, removed: 0 };
  if (/^@@/m.test(diff)) {
    let added = 0;
    let removed = 0;
    for (const line of diff.split("\n")) {
      if (line.startsWith("+") && !line.startsWith("+++")) added += 1;
      else if (line.startsWith("-") && !line.startsWith("---")) removed += 1;
    }
    return { added, removed };
  }
  const lines = diff.endsWith("\n") ? diff.split("\n").length - 1 : diff.split("\n").length;
  if (kind === "add") return { added: lines, removed: 0 };
  if (kind === "delete") return { added: 0, removed: lines };
  return { added: 0, removed: 0 };
}

function fileChanges(value: unknown): FileChangeView[] {
  if (!Array.isArray(value)) return [];
  return value.map((raw) => {
    const change = obj(raw);
    const kindRaw = change.kind;
    const kindObj = obj(kindRaw);
    const kindName = typeof kindRaw === "string" ? kindRaw : str(kindObj.type);
    const kind: FileChangeKind = kindName === "add" || kindName === "delete" ? kindName : "update";
    const path = str(change.path) ?? "";
    const movePath = str(kindObj.move_path) ?? str(kindObj.movePath);
    return {
      path: displayPath(path),
      kind,
      movePath: movePath === null ? null : displayPath(movePath),
      openPath: projectPath(movePath ?? path),
      ...diffStats(kind, str(change.diff) ?? ""),
    };
  });
}

function capOutput(text: string): { output: string; truncated: boolean } {
  return text.length > OUTPUT_CAP ? { output: text.slice(-OUTPUT_CAP), truncated: true } : { output: text, truncated: false };
}

function userText(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      const p = obj(part);
      return p.type === "text" ? (str(p.text) ?? "") : "";
    })
    .filter(Boolean)
    .join("\n");
}

function errorText(value: unknown): string | null {
  if (typeof value === "string") return value;
  const e = obj(value);
  return str(e.message) ?? (Object.keys(e).length ? JSON.stringify(value) : null);
}

/**
 * An app-server `ThreadItem` as the view's item. `prev` is the same item as the
 * view held it, consulted only for what the record may omit (a completed command
 * whose `aggregatedOutput` is null keeps the output streamed so far).
 */
export function itemFromThreadItem(raw: unknown, completed: boolean, prev?: ItemView): ItemView | null {
  const item = obj(raw);
  const id = str(item.id);
  const type = str(item.type);
  if (!id || !type) return null;
  const streaming = !completed;
  switch (type) {
    case "userMessage":
      return { type, id, text: userText(item.content) };
    case "agentMessage": {
      const text = str(item.text) ?? "";
      // an `item/started` carries an empty text; never wipe what deltas already streamed
      const kept = !completed && prev?.type === "agentMessage" && prev.text.length > text.length ? prev.text : text;
      return { type, id, text: kept, streaming };
    }
    case "reasoning": {
      const summary = strings(item.summary);
      const content = strings(item.content);
      if (!completed && prev?.type === "reasoning") {
        return { type, id, summary: summary.length ? summary : prev.summary, content: content.length ? content : prev.content, streaming };
      }
      return { type, id, summary, content, streaming };
    }
    case "commandExecution": {
      const aggregated = str(item.aggregatedOutput);
      const before = prev?.type === "commandExecution" ? prev : null;
      const text = aggregated ?? before?.output ?? "";
      const capped = capOutput(text);
      return {
        type,
        id,
        command: str(item.command) ?? "",
        cwd: str(item.cwd),
        output: capped.output,
        truncated: capped.truncated || (aggregated === null && !!before?.truncated),
        exitCode: num(item.exitCode),
        status: itemStatus(item.status, completed ? "completed" : "inProgress"),
        durationMs: num(item.durationMs),
      };
    }
    case "fileChange":
      return {
        type,
        id,
        changes: fileChanges(item.changes),
        status: itemStatus(item.status, completed ? "completed" : "inProgress"),
      };
    case "mcpToolCall":
    case "dynamicToolCall":
      return {
        type: "mcpToolCall",
        id,
        server: type === "mcpToolCall" ? str(item.server) : str(item.namespace),
        tool: str(item.tool) ?? "",
        status: itemStatus(item.status, completed ? "completed" : "inProgress"),
        error: type === "dynamicToolCall" && item.success === false ? "failed" : errorText(item.error),
        durationMs: num(item.durationMs),
      };
    case "plan": {
      const text = str(item.text) ?? "";
      const kept = !completed && prev?.type === "plan" && prev.text.length > text.length ? prev.text : text;
      return { type, id, text: kept, streaming };
    }
    default: {
      // e.g. `imageView` carries the (absolute) path it showed
      const path = str(item.path);
      return {
        type: "other",
        id,
        itemType: type,
        label: str(item.query) ?? (path === null ? null : displayPath(path)) ?? str(item.review) ?? null,
      };
    }
  }
}

/* ---------- copy-on-write draft over one batch of records ---------- */

interface Draft {
  seq: number;
  turns: TurnView[];
  activeTurnId: string | null;
  threadId: string | null;
  notices: ConversationNotice[];
  noticesCopied: boolean;
  index: Map<string, number>;
  /** turns already copied in this batch → their item-id index */
  touched: Map<string, Map<string, number>>;
}

function begin(state: ConversationState): Draft {
  const index = new Map<string, number>();
  state.turns.forEach((turn, i) => index.set(turn.turnId, i));
  return {
    seq: state.seq,
    turns: state.turns.slice(),
    activeTurnId: state.activeTurnId,
    threadId: state.threadId,
    notices: state.notices,
    noticesCopied: false,
    index,
    touched: new Map(),
  };
}

function finish(draft: Draft): ConversationState {
  // command output is capped once per batch, not per delta: past the cap, slicing on
  // every delta would copy the whole kept tail again for each small chunk
  for (const turnId of draft.touched.keys()) {
    const at = draft.index.get(turnId);
    if (at === undefined) continue;
    const items = draft.turns[at].items; // this batch's copy (turnFor)
    items.forEach((item, i) => {
      if (item.type === "commandExecution" && item.output.length > OUTPUT_CAP) {
        items[i] = { ...item, ...capOutput(item.output), truncated: true };
      }
    });
  }
  return {
    seq: draft.seq,
    turns: draft.turns,
    activeTurnId: draft.activeTurnId,
    threadId: draft.threadId,
    notices: draft.notices,
  };
}

function newTurn(turnId: string): TurnView {
  return {
    turnId,
    rowId: null,
    prompt: null,
    model: null,
    effort: null,
    status: "inProgress",
    items: [],
    diff: null,
    plan: null,
    usage: null,
    startedAt: null,
    completedAt: null,
    durationMs: null,
    error: null,
  };
}

/** The turn, writable in this batch (created at the end when unseen). */
function turnFor(draft: Draft, turnId: string): { turn: TurnView; items: Map<string, number> } {
  let at = draft.index.get(turnId);
  if (at === undefined) {
    at = draft.turns.length;
    draft.turns.push(newTurn(turnId));
    draft.index.set(turnId, at);
  }
  let items = draft.touched.get(turnId);
  if (!items) {
    const copy = { ...draft.turns[at], items: draft.turns[at].items.slice() };
    draft.turns[at] = copy;
    items = new Map();
    copy.items.forEach((item, i) => items!.set(item.id, i));
    draft.touched.set(turnId, items);
  }
  return { turn: draft.turns[at], items };
}

function findItem(ctx: { turn: TurnView; items: Map<string, number> }, id: string): ItemView | undefined {
  const at = ctx.items.get(id);
  return at === undefined ? undefined : ctx.turn.items[at];
}

function putItem(ctx: { turn: TurnView; items: Map<string, number> }, item: ItemView): void {
  const at = ctx.items.get(item.id);
  if (at === undefined) {
    ctx.items.set(item.id, ctx.turn.items.length);
    ctx.turn.items.push(item);
  } else {
    ctx.turn.items[at] = item;
  }
}

function isSettled(item: ItemView | undefined): boolean {
  if (!item) return false;
  switch (item.type) {
    case "agentMessage":
    case "reasoning":
    case "plan":
      return !item.streaming;
    case "commandExecution":
    case "fileChange":
    case "mcpToolCall":
      return item.status !== "inProgress";
    default:
      return true;
  }
}

/** The turn a record without a `turnId` belongs to: the active one, else the last. */
function currentTurnId(draft: Draft): string | null {
  return draft.activeTurnId ?? draft.turns[draft.turns.length - 1]?.turnId ?? null;
}

function endTurn(draft: Draft, turnId: string, status: TurnStatus, fields: Partial<TurnView>): void {
  const ctx = turnFor(draft, turnId);
  Object.assign(ctx.turn, fields, { status });
  ctx.turn.items.forEach((item, i) => {
    if ((item.type === "agentMessage" || item.type === "reasoning" || item.type === "plan") && item.streaming) {
      ctx.turn.items[i] = { ...item, streaming: false };
    }
  });
  if (draft.activeTurnId === turnId) draft.activeTurnId = null;
}

function addNotice(draft: Draft, record: CodingEventRecord, code: NoticeCode, detail: string | null): void {
  const params = obj(record.params);
  const id = `notice-${record.seq}`;
  const turnId = str(params.turnId) ?? draft.activeTurnId;
  if (turnId) {
    putItem(turnFor(draft, turnId), { type: "notice", id, code, detail });
    return;
  }
  if (!draft.noticesCopied) {
    draft.notices = draft.notices.slice();
    draft.noticesCopied = true;
  }
  draft.notices.push({ id, code, detail, at: draft.turns.length });
}

function apply(draft: Draft, record: CodingEventRecord): void {
  const params = obj(record.params);
  const method = record.method;
  const turnIdParam = str(params.turnId);
  switch (method) {
    case "thread/started": {
      draft.threadId = str(obj(params.thread).id) ?? draft.threadId;
      return;
    }
    case "turn/started": {
      const turn = obj(params.turn);
      const turnId = str(turn.id) ?? turnIdParam;
      if (!turnId) return;
      draft.threadId = str(params.threadId) ?? draft.threadId;
      const ctx = turnFor(draft, turnId);
      const status = turnStatusOf(turn.status) ?? "inProgress";
      // a late duplicate never re-opens a finished turn
      if (ctx.turn.status === "inProgress") ctx.turn.status = status;
      ctx.turn.startedAt = toMs(turn.startedAt) ?? ctx.turn.startedAt ?? toMs(record.ts);
      if (ctx.turn.status === "inProgress") draft.activeTurnId = turnId;
      return;
    }
    case "turn/completed": {
      const turn = obj(params.turn);
      const turnId = str(turn.id) ?? turnIdParam;
      if (!turnId) return;
      const prev = draft.turns[draft.index.get(turnId) ?? -1];
      const startedAt = toMs(turn.startedAt) ?? prev?.startedAt ?? null;
      const completedAt = toMs(turn.completedAt) ?? toMs(record.ts);
      const durationMs =
        num(turn.durationMs) ?? (startedAt !== null && completedAt !== null ? Math.max(0, completedAt - startedAt) : null);
      const status = turnStatusOf(turn.status);
      endTurn(draft, turnId, status && status !== "inProgress" ? status : "completed", {
        startedAt,
        completedAt,
        durationMs,
        error: errorText(turn.error) ?? prev?.error ?? null,
      });
      return;
    }
    case "launchpad/turnAbandoned": {
      const turnId = turnIdParam ?? draft.activeTurnId;
      if (!turnId) return;
      const prev = draft.turns[draft.index.get(turnId) ?? -1];
      if (prev && prev.status !== "inProgress") return;
      // the runtime reports the usage seen before the microVM / app-server went away
      endTurn(draft, turnId, "abandoned", {
        completedAt: toMs(record.ts),
        usage: normalizeUsage(params.usage) ?? prev?.usage ?? null,
      });
      return;
    }
    case "launchpad/turnUsage": {
      const turnId = turnIdParam ?? currentTurnId(draft);
      if (!turnId) return;
      const ctx = turnFor(draft, turnId);
      ctx.turn.usage = normalizeUsage(params.usage) ?? ctx.turn.usage;
      ctx.turn.model = ctx.turn.model ?? str(params.model);
      ctx.turn.effort = ctx.turn.effort ?? str(params.effort);
      return;
    }
    case "item/started":
    case "item/completed": {
      const item = obj(params.item);
      const turnId = turnIdParam ?? currentTurnId(draft);
      const id = str(item.id);
      if (!turnId || !id) return;
      const ctx = turnFor(draft, turnId);
      const prev = findItem(ctx, id);
      const completed = method === "item/completed";
      // a started record never downgrades an item that already completed
      if (!completed && isSettled(prev)) return;
      const next = itemFromThreadItem(item, completed, prev);
      if (next) putItem(ctx, next);
      if (next?.type === "userMessage" && next.text && !ctx.turn.prompt) ctx.turn.prompt = next.text;
      return;
    }
    case "item/agentMessage/delta":
    case "item/plan/delta": {
      const turnId = turnIdParam ?? currentTurnId(draft);
      const id = str(params.itemId);
      const delta = str(params.delta) ?? "";
      if (!turnId || !id || !delta) return;
      const ctx = turnFor(draft, turnId);
      const prev = findItem(ctx, id);
      const type = method === "item/plan/delta" ? "plan" : "agentMessage";
      if (prev && (prev.type !== type || !prev.streaming)) return;
      putItem(ctx, { type, id, text: (prev?.text ?? "") + delta, streaming: true });
      return;
    }
    case "item/reasoning/summaryTextDelta":
    case "item/reasoning/summaryPartAdded":
    case "item/reasoning/textDelta": {
      const turnId = turnIdParam ?? currentTurnId(draft);
      const id = str(params.itemId);
      if (!turnId || !id) return;
      const ctx = turnFor(draft, turnId);
      const prev = findItem(ctx, id);
      if (prev && (prev.type !== "reasoning" || !prev.streaming)) return;
      const base = prev?.type === "reasoning" ? prev : { type: "reasoning" as const, id, summary: [], content: [], streaming: true };
      const summary = base.summary.slice();
      const content = base.content.slice();
      const delta = str(params.delta) ?? "";
      if (method === "item/reasoning/textDelta") {
        const at = num(params.contentIndex) ?? Math.max(0, content.length - 1);
        while (content.length <= at) content.push("");
        content[at] += delta;
      } else {
        const at = num(params.summaryIndex) ?? Math.max(0, summary.length - 1);
        while (summary.length <= at) summary.push("");
        summary[at] += delta;
      }
      putItem(ctx, { ...base, summary, content, streaming: true });
      return;
    }
    case "item/commandExecution/outputDelta": {
      const turnId = turnIdParam ?? currentTurnId(draft);
      const id = str(params.itemId);
      const delta = str(params.delta) ?? "";
      if (!turnId || !id || !delta) return;
      const ctx = turnFor(draft, turnId);
      const prev = findItem(ctx, id);
      if (prev && (prev.type !== "commandExecution" || prev.status !== "inProgress")) return;
      const base =
        prev?.type === "commandExecution"
          ? prev
          : {
              type: "commandExecution" as const,
              id,
              command: "",
              cwd: null,
              output: "",
              truncated: false,
              exitCode: null,
              status: "inProgress" as const,
              durationMs: null,
            };
      // capped at the end of the batch (finish)
      putItem(ctx, { ...base, output: base.output + delta });
      return;
    }
    case "item/fileChange/patchUpdated": {
      const turnId = turnIdParam ?? currentTurnId(draft);
      const id = str(params.itemId);
      if (!turnId || !id) return;
      const ctx = turnFor(draft, turnId);
      const prev = findItem(ctx, id);
      if (prev && (prev.type !== "fileChange" || prev.status !== "inProgress")) return;
      putItem(ctx, { type: "fileChange", id, changes: fileChanges(params.changes), status: "inProgress" });
      return;
    }
    case "turn/diff/updated": {
      const turnId = turnIdParam ?? currentTurnId(draft);
      if (!turnId) return;
      turnFor(draft, turnId).turn.diff = str(params.diff);
      return;
    }
    case "turn/plan/updated": {
      const turnId = turnIdParam ?? currentTurnId(draft);
      if (!turnId) return;
      const steps = Array.isArray(params.plan)
        ? params.plan.map((s) => ({ step: str(obj(s).step) ?? "", status: str(obj(s).status) ?? "pending" }))
        : [];
      turnFor(draft, turnId).turn.plan = { explanation: str(params.explanation), steps };
      return;
    }
    case "error": {
      const turnId = turnIdParam ?? currentTurnId(draft);
      if (!turnId) return;
      const error = obj(params.error);
      const message = str(error.message) ?? errorText(params.error) ?? "error";
      const details = str(error.additionalDetails);
      putItem(turnFor(draft, turnId), {
        type: "error",
        id: `error-${record.seq}`,
        message: details ? `${message}\n${details}` : message,
        willRetry: params.willRetry === true,
      });
      return;
    }
    case "launchpad/appServerExited": {
      // the runtime sends the process's `{code, signal}`
      const code = num(params.code);
      const detail = str(params.message) ?? str(params.reason) ?? str(params.signal) ?? (code !== null ? `exit ${code}` : null);
      addNotice(draft, record, "appServerExited", detail);
      return;
    }
    case "launchpad/threadResumeFailed":
      // the previous conversation's rollout could not be resumed: the agent continues
      // in a new thread without that context
      addNotice(draft, record, "threadResumeFailed", str(params.message) ?? null);
      return;
    case "launchpad/serverRequestDeclined":
      addNotice(draft, record, "serverRequestDeclined", str(params.method) ?? null);
      return;
    case "launchpad/restored":
      addNotice(draft, record, "restored", null);
      return;
    default:
      return; // other notifications (rate limits, MCP startup, thread status…) carry nothing to show
  }
}

/** Apply a batch of records in order (one render per network chunk). */
export function applyCodingEvents(state: ConversationState, records: readonly CodingEventRecord[]): ConversationState {
  let draft: Draft | null = null;
  for (const record of records) {
    if (typeof record.seq !== "number" || record.seq <= (draft?.seq ?? state.seq)) continue;
    draft ??= begin(state);
    draft.seq = record.seq;
    apply(draft, record);
  }
  return draft ? finish(draft) : state;
}

export function applyCodingEvent(state: ConversationState, record: CodingEventRecord): ConversationState {
  return applyCodingEvents(state, [record]);
}

/* ---------- ledger turns (prompt, model, effort, settled status and usage) ---------- */

function turnFromRow(row: CodingTurnRow, turnId: string): TurnView {
  return {
    ...newTurn(turnId),
    rowId: row.id,
    prompt: row.prompt || null,
    model: row.model || null,
    effort: row.effort || null,
    status: turnStatusOf(row.status) ?? "inProgress",
    usage: normalizeUsage(row.usage),
    startedAt: toMs(row.started_at),
    completedAt: toMs(row.completed_at),
    error: row.error,
  };
}

const SCALARS = ["rowId", "prompt", "model", "effort", "status", "startedAt", "completedAt", "durationMs", "error"] as const;

function sameUsage(a: TokenUsage | null, b: TokenUsage | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return (Object.keys(a) as (keyof TokenUsage)[]).every((key) => a[key] === b[key]);
}

/**
 * Merge the project detail's ledger turns into the conversation by codex `turn_id`.
 * The events stay authoritative for what happened; the ledger fills what they lack
 * (the prompt, model, effort, the settled usage) and a settled status overrides an
 * event turn still in progress (a turn the watcher abandoned). A ledger turn the
 * events never mentioned (a just-started turn, a turn whose log was lost) is placed
 * by its start time. Idempotent and cheap: the IDE applies it at render time over the
 * event-only state, so the events alone decide the order of the turns they know.
 */
export function withLedgerTurns(state: ConversationState, rows: readonly CodingTurnRow[]): ConversationState {
  if (rows.length === 0) return state;
  const turns = state.turns.slice();
  let activeTurnId = state.activeTurnId;
  let changed = false;
  for (const row of rows) {
    const turnId = row.turn_id;
    if (!turnId) continue;
    const at = turns.findIndex((turn) => turn.turnId === turnId);
    const rowStatus = turnStatusOf(row.status);
    if (at >= 0) {
      const turn = turns[at];
      const status = turn.status === "inProgress" && rowStatus && rowStatus !== "inProgress" ? rowStatus : turn.status;
      const next: TurnView = {
        ...turn,
        rowId: row.id,
        prompt: turn.prompt ?? (row.prompt || null),
        model: turn.model ?? (row.model || null),
        effort: turn.effort ?? (row.effort || null),
        status,
        usage: turn.usage ?? normalizeUsage(row.usage),
        startedAt: turn.startedAt ?? toMs(row.started_at),
        completedAt: turn.completedAt ?? toMs(row.completed_at),
        error: turn.error ?? row.error,
      };
      if (next.durationMs === null && next.startedAt !== null && next.completedAt !== null && status !== "inProgress") {
        next.durationMs = Math.max(0, next.completedAt - next.startedAt);
      }
      let itemsChanged = false;
      if (status !== "inProgress") {
        next.items = turn.items.map((item) => {
          if ((item.type === "agentMessage" || item.type === "reasoning" || item.type === "plan") && item.streaming) {
            itemsChanged = true;
            return { ...item, streaming: false };
          }
          return item;
        });
        if (activeTurnId === turnId) activeTurnId = null;
      }
      if (itemsChanged || SCALARS.some((key) => next[key] !== turn[key]) || !sameUsage(next.usage, turn.usage)) {
        turns[at] = next;
        changed = true;
      } else {
        next.items = turn.items;
      }
      continue;
    }
    const fresh = turnFromRow(row, turnId);
    const start = fresh.startedAt;
    const before = start === null ? -1 : turns.findIndex((turn) => turn.startedAt !== null && turn.startedAt > start);
    if (before >= 0) turns.splice(before, 0, fresh);
    else turns.push(fresh);
    if (fresh.status === "inProgress" && activeTurnId === null) activeTurnId = turnId;
    changed = true;
  }
  return changed ? { ...state, turns, activeTurnId } : state;
}

/* ---------- selectors ---------- */

/** The turn a run indicator should follow: the active one, else the last in progress. */
export function liveTurn(state: ConversationState): TurnView | null {
  if (state.activeTurnId) {
    const active = state.turns.find((turn) => turn.turnId === state.activeTurnId);
    if (active && active.status === "inProgress") return active;
  }
  return null;
}

/** Whether a record may have changed files on disk (the IDE re-lists the tree). */
export function affectsFiles(record: CodingEventRecord): boolean {
  if (record.method === "turn/completed" || record.method === "launchpad/restored" || record.method === "launchpad/turnAbandoned") {
    return true;
  }
  if (record.method === "item/completed") {
    const type = obj(obj(record.params).item).type;
    return type === "fileChange" || type === "commandExecution";
  }
  return false;
}

/** The prompt a turn shows: its user message, else the ledger prompt. */
export function turnPrompt(turn: TurnView): string | null {
  const message = turn.items.find((item) => item.type === "userMessage");
  return (message?.type === "userMessage" && message.text) || turn.prompt;
}
