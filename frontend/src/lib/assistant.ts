/**
 * Architect assistant (SE-039 / SE-047) — the transport and pure business rules
 * shared by the classic `/create/assistant` page and the native V2 `/v2/assistant`
 * page: the SSE turn stream, the transcript projection, the proposal edit draft
 * and its validation, the creation-progress state machine, the evaluation-plan
 * row edits and the knowledge-base readiness rules. Nothing here renders.
 */
import { DEFAULT_HARNESS_NATIVE_TOOLS, DEFAULT_TIMEOUT_SECONDS } from "./agent-defaults";
import {
  ApiError,
  api,
  AUTH_UNAUTHORIZED_EVENT,
  HARNESS_NATIVE_TOOLS,
  type AssistantConversationDetail,
  type AssistantConversationFootprint,
  type AssistantConversationPurgeResult,
  type AssistantFishbone,
  type AssistantPurgeBlocker,
  type AssistantPurgeJobState,
  type AssistantMessage,
  type AssistantProposal,
  type AssistantProposalContent,
  type AssistantTurnRequest,
  type HarnessNativeTool,
} from "./api";
import { currentModelDefaults } from "./model-defaults";
import { defaultModelFor, type ModelSource } from "./models";
import { WORKSPACE_HEADER } from "./workspace-header";

export const ASSISTANT_JOB_POLL_MS = 3000;
export const PROPOSAL_NAME_RE = /^[a-z][a-z0-9-]{2,47}$/;
export const PROPOSAL_MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$/;
// the full proposal and a revision of the stored one (`launchpad-proposal-patch`)
const PROPOSAL_FENCE_RE = /```launchpad-proposal(?:-patch)?[ \t]*\r?\n[\s\S]*?\r?\n[ \t]*```/g;
const PREPARATION_FENCE_RE = /```launchpad-preparation[ \t]*\r?\n[\s\S]*?\r?\n[ \t]*```/g;

// ─── SSE turn stream ──────────────────────────────────────────────────────

/** Parse a `text/event-stream` response body into `{event, data}` frames. */
export async function* sseEvents(res: Response): AsyncGenerator<{ event: string; data: never }> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) yield { event, data: JSON.parse(data) as never };
    }
  }
}

/**
 * `POST …/conversations/{id}/turns` — opens one discussion turn as an SSE stream,
 * pinned to the workspace it started in. A non-2xx answer throws an `ApiError`
 * with the same session semantics as the typed client (a 401 signs out).
 */
export async function openAssistantTurn(
  conversationId: string,
  request: AssistantTurnRequest,
  workspaceId: string | null,
  signal: AbortSignal,
): Promise<Response> {
  const res = await fetch(
    `/api/assistant/architect/conversations/${encodeURIComponent(conversationId)}/turns`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(workspaceId ? { [WORKSPACE_HEADER]: workspaceId } : {}),
      },
      body: JSON.stringify(request),
      signal,
    },
  );
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
    const body = (await res.json().catch(() => ({}))) as { code?: string; message?: string };
    throw new ApiError(body.code ?? `http.${res.status}`, body.message ?? res.statusText, null);
  }
  return res;
}

export const isAssistantUnauthorized = (err: unknown) =>
  err instanceof ApiError &&
  (err.code === "http.401" ||
    err.code === "http.403" ||
    err.code === "auth.required" ||
    err.code === "workspace.forbidden" ||
    err.code === "auth.permission_required");

// ─── transcript ───────────────────────────────────────────────────────────

export interface AssistantLiveMessage {
  role: "user" | "assistant" | "tool" | "error";
  text: string;
  name?: string | null;
  /** sender of a user row; absent for a row typed in this tab (the caller) */
  author?: string | null;
  streaming?: boolean;
  /** the stored turn number; absent on rows streamed in this tab until the reload */
  turn?: number;
}

/** Backend ``TURN_RETRIED_NAME``: marks a failed turn the member retried (never replayed). */
export const TURN_RETRIED_NAME = "turn_retried";
const PROPOSAL_REJECTED_NAME = "proposal_rejected";

export function toLiveMessages(rows: AssistantMessage[]): AssistantLiveMessage[] {
  return rows.map((m) => ({ role: m.role, text: m.text, name: m.name, author: m.author, turn: m.turn }));
}

/** The latest turn when it failed and was not retried yet: `{turn, prompt}` to retry. */
export function retryableTurn(messages: AssistantLiveMessage[]): { turn: number; prompt: string } | null {
  const turns = messages.map((m) => m.turn).filter((n): n is number => typeof n === "number");
  if (!turns.length) return null;
  const latest = Math.max(...turns);
  const rows = messages.filter((m) => m.turn === latest);
  const failed = rows.some((m) => m.role === "error" && m.name !== PROPOSAL_REJECTED_NAME && m.name !== TURN_RETRIED_NAME);
  const retried = rows.some((m) => m.role === "error" && m.name === TURN_RETRIED_NAME);
  const prompt = rows.find((m) => m.role === "user")?.text ?? "";
  return failed && !retried && prompt ? { turn: latest, prompt } : null;
}

// ─── context window (assistant-context C1) ────────────────────────────────

/**
 * What the assistant's request replays of the transcript: `GET` detail reports the
 * NEXT turn's window, the turn stream's `meta` event the streaming turn's. Turns
 * before `first_replayed_turn` are kept in the ledger and shown, but the model no
 * longer sees them.
 */
export interface AssistantContextWindow {
  replayed_turns: number;
  omitted_turns: number;
  /** the stored turn number of the oldest replayed turn; null when none is replayed */
  first_replayed_turn: number | null;
  /** share of the request budget the composed request uses (0–1) */
  budget_used_ratio?: number;
}

/** The `context` member of a conversation detail (absent on an older backend). */
export function conversationContext(detail: object | null | undefined): AssistantContextWindow | null {
  const ctx = (detail as { context?: unknown } | null | undefined)?.context;
  return ctx && typeof ctx === "object" && typeof (ctx as AssistantContextWindow).omitted_turns === "number"
    ? (ctx as AssistantContextWindow)
    : null;
}

/** The window a turn stream's `meta` event announces, or null. A `retry` event after a
 *  context overflow carries the same members (the replay's smaller window): read it too. */
export function contextFromMeta(data: Record<string, unknown>): AssistantContextWindow | null {
  const omitted = data.omitted_turns;
  if (typeof omitted !== "number") return null;
  const first = data.first_replayed_turn;
  const nested = conversationContext(data);
  return nested ?? {
    replayed_turns: 0,
    omitted_turns: omitted,
    first_replayed_turn: typeof first === "number" ? first : null,
  };
}

/** A conversation-list row the backend marked as surely past the replay window. */
export const isContextTrimmed = (summary: object) =>
  (summary as { context_trimmed?: unknown }).context_trimmed === true;

/**
 * Where the "earlier N turns are no longer in context" divider goes: before the
 * first row of the oldest replayed turn. With no replayed turn, after the last stored
 * row (rows streamed in this tab carry no turn number yet). -1 = no divider.
 */
export function contextDividerIndex(
  messages: AssistantLiveMessage[],
  ctx: AssistantContextWindow | null,
): number {
  if (!ctx || ctx.omitted_turns <= 0) return -1;
  const first = ctx.first_replayed_turn;
  if (first !== null) {
    const index = messages.findIndex((m) => typeof m.turn === "number" && m.turn >= first);
    if (index >= 0) return index;
  }
  const stored = messages.findIndex((m) => typeof m.turn !== "number");
  return stored >= 0 ? stored : messages.length;
}

// ─── confirmed-items digest (assistant-context C2) ────────────────────────

/**
 * The summary of the turns outside the replay window, which the assistant sees in its
 * protocol preamble instead of those turns. Session state, never part of a proposal.
 */
export interface AssistantContextDigest {
  /** Markdown; null while no digest was stored yet */
  text: string | null;
  /** the last turn it covers (inclusive) */
  through_turn: number | null;
  updated_at: string | null;
  /** a refresh or a rebuild is running on the server */
  generating: boolean;
}

/** The `context_digest` member of a conversation detail (absent on an older backend). */
export function conversationDigest(detail: object | null | undefined): AssistantContextDigest | null {
  const d = (detail as { context_digest?: unknown } | null | undefined)?.context_digest;
  return d && typeof d === "object" && "text" in d ? (d as AssistantContextDigest) : null;
}

/**
 * Whether the digest is still catching up with the window: turns before the oldest
 * replayed one are not all covered, or a refresh is running. The console re-reads the
 * conversation a few times while this holds (the refresh runs after the turn ended).
 */
export function digestPending(
  ctx: AssistantContextWindow | null,
  digest: AssistantContextDigest | null,
): boolean {
  if (!ctx || ctx.omitted_turns <= 0 || !digest) return false;
  if (digest.generating) return true;
  if (ctx.first_replayed_turn === null) return false; // nothing replayed: cannot tell
  return (digest.through_turn ?? 0) < ctx.first_replayed_turn - 1;
}

/**
 * `POST …/conversations/{id}/digest/regenerate` — the owner rebuilds the digest from
 * every omitted turn (synchronous: one model call). Pinned to the conversation's workspace.
 */
export async function regenerateDigest(
  conversationId: string,
  workspaceId: string | null,
): Promise<AssistantContextDigest> {
  const res = await fetch(
    `/api/assistant/architect/conversations/${encodeURIComponent(conversationId)}/digest/regenerate`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(workspaceId ? { [WORKSPACE_HEADER]: workspaceId } : {}),
      },
    },
  );
  const body = (await res.json().catch(() => ({}))) as {
    code?: string; message?: string; detail?: unknown; context_digest?: AssistantContextDigest;
  };
  if (!res.ok || !body.context_digest) {
    if (res.status === 401) window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
    throw new ApiError(body.code ?? `http.${res.status}`, body.message ?? res.statusText, body.detail ?? null);
  }
  return body.context_digest;
}

/** The typed proposal pane shows the block; the transcript shows a pointer instead. */
export function stripProposalBlock(text: string, marker: string): string {
  return text.replace(PROPOSAL_FENCE_RE, `> ${marker}`).replace(PREPARATION_FENCE_RE, "");
}

// ─── proposal edit draft ──────────────────────────────────────────────────

export type ProposalEditDraft = Pick<
  AssistantProposalContent,
  | "name"
  | "model_id"
  | "model_source"
  | "system_prompt"
  | "tools"
  | "skills"
  | "knowledge_bases"
  | "memory"
  | "max_iterations"
  | "timeout_seconds"
> & { native_tools: HarnessNativeTool[] };

/** The model a proposal edit lands on for `source`: the assistant's configured default
 *  on Bedrock, the first offered model on Mantle. */
export function proposalDefaultModel(source: ModelSource): string {
  return source === "bedrock" ? currentModelDefaults().assistant_model_id : defaultModelFor(source);
}

export function proposalDraftFrom(content: AssistantProposal["content"]): ProposalEditDraft {
  const source = (content.model_source as ModelSource) ?? "bedrock";
  return {
    name: String(content.name ?? ""),
    model_id: String(content.model_id ?? proposalDefaultModel(source)),
    model_source: source,
    system_prompt: String(content.system_prompt ?? ""),
    tools: Array.isArray(content.tools) ? content.tools.map(String) : [],
    native_tools: Array.isArray(content.native_tools)
      ? content.native_tools.filter((tool): tool is HarnessNativeTool =>
        HARNESS_NATIVE_TOOLS.some((name) => name === tool))
      : [...DEFAULT_HARNESS_NATIVE_TOOLS],
    skills: Array.isArray(content.skills) ? content.skills.map(String) : [],
    knowledge_bases: Array.isArray(content.knowledge_bases) ? content.knowledge_bases.map(String) : [],
    memory: content.memory === "workspace" ? "workspace" : "disabled",
    max_iterations: Number(content.max_iterations ?? 100),
    timeout_seconds: Number(content.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS),
  };
}

/** The draft an EDIT opens: once resources are locked by an approval, the saved
 *  preparation (not the proposal) is the source of the resource lists. */
export function proposalEditStart(
  latest: AssistantProposal,
  conversation: AssistantConversationDetail,
  resourcesLocked: boolean,
): ProposalEditDraft {
  const prep = conversation.preparation;
  return proposalDraftFrom(resourcesLocked && prep
    ? { ...latest.content, knowledge_bases: prep.knowledge_bases, skills: prep.skills, tools: prep.tools ?? [] }
    : latest.content);
}

export function proposalEditErrors(draft: ProposalEditDraft): Record<string, boolean> {
  return {
    name: !PROPOSAL_NAME_RE.test(draft.name),
    model_id: !PROPOSAL_MODEL_RE.test(draft.model_id),
    system_prompt: draft.system_prompt.trim().length === 0 || draft.system_prompt.length > 20000,
    max_iterations: !(draft.max_iterations >= 1 && draft.max_iterations <= 100),
    timeout_seconds: !(draft.timeout_seconds >= 10 && draft.timeout_seconds <= 3600),
  };
}

/** Shape guard for the optional fishbone member (the content type is a loose record;
 *  an invalid revision may carry anything). */
export function isFishbone(v: unknown): v is AssistantFishbone {
  if (!v || typeof v !== "object") return false;
  const f = v as Record<string, unknown>;
  return (
    typeof f.use_case === "string" &&
    typeof f.customer === "string" &&
    !!f.coverage && typeof f.coverage === "object" &&
    !!f.barriers && typeof f.barriers === "object"
  );
}

/** The new member revision: the edited fields, plus every solution member the form
 *  does not edit travelling with the revision unchanged. */
export function proposalContentFromDraft(
  draft: ProposalEditDraft,
  base: AssistantProposal["content"],
): AssistantProposalContent {
  return {
    version: 1,
    ...draft,
    summary: typeof base.summary === "string" ? base.summary : "",
    requirements_baseline: Array.isArray(base.requirements_baseline) ? base.requirements_baseline : [],
    assumptions: Array.isArray(base.assumptions) ? base.assumptions : [],
    manual_tasks: Array.isArray(base.manual_tasks) ? base.manual_tasks : [],
    golden_tests: Array.isArray(base.golden_tests) ? base.golden_tests : [],
    evaluator_recommendations: Array.isArray(base.evaluator_recommendations)
      ? base.evaluator_recommendations
      : [],
    ...(isFishbone(base.fishbone) ? { fishbone: base.fishbone } : {}),
    // built-in tools are not editable in the draft; an edit must not drop them
    ...(Array.isArray(base.builtin_tools) ? { builtin_tools: base.builtin_tools.map(String) } : {}),
    // a reviewed function narrowing travels with its still-selected Gateway; dropping
    // it silently would widen that Gateway to every declared function
    ...carriedToolFunctions(base.tool_functions, draft.tools),
    // storage mounts (storage-mounts §8.3) are not editable in the draft either; an
    // edit keeps the base's `{storage_id, mode}` list as it was
    ...(Array.isArray(base.storage) && base.storage.length ? { storage: base.storage } : {}),
  };
}

function carriedToolFunctions(
  raw: unknown,
  tools: string[],
): { tool_functions?: Record<string, string[]> } {
  if (!raw || typeof raw !== "object") return {};
  const kept = Object.fromEntries(
    Object.entries(raw as Record<string, unknown>).filter(
      (entry): entry is [string, string[]] =>
        tools.includes(entry[0]) && Array.isArray(entry[1]) && entry[1].length > 0,
    ),
  );
  return Object.keys(kept).length ? { tool_functions: kept } : {};
}

// ─── deployment outcome / creation progress ───────────────────────────────

/** Live snapshot of the Agent deployed from a conversation. */
export interface DeployedAgent {
  agentId: string | null;
  agentName: string | null;
  agentStatus: string | null;
  jobStatus: string | null;
}

export const CREATION_STEPS = ["goals", "resources", "review", "deploy"] as const;
export type CreationStepState = "done" | "current" | "pending" | "failed";

/**
 * The four required creation steps. A proposal establishes a requirements baseline;
 * an empty or ongoing discussion alone is never evidence that the goals or resources
 * are ready. Approval completes the first three; the fourth completes only when the
 * deployed Agent is active.
 */
export function creationProgress({
  latest, deployed, resourcesDirty, editing,
}: {
  latest: AssistantProposal | null;
  deployed: DeployedAgent | null;
  resourcesDirty: boolean;
  editing: boolean;
}) {
  const approved = Boolean(deployed);
  const failed = deployed?.jobStatus === "failed" || deployed?.agentStatus === "failed";
  const ready = deployed?.jobStatus === "succeeded" && deployed?.agentStatus === "active";
  const running = deployed?.jobStatus === "queued" || deployed?.jobStatus === "running"
    || deployed?.agentStatus === "deploying";
  const valid = latest?.status === "draft" && latest.validation_errors.length === 0;
  const completed = approved
    ? [true, true, true, Boolean(ready)]
    : [
      Boolean(latest && typeof latest.content.system_prompt === "string" && latest.content.system_prompt.trim()),
      Boolean(valid && !resourcesDirty && !editing),
      false,
      false,
    ];
  const done = completed.filter(Boolean).length;
  const active = completed.findIndex((value) => !value);
  const states: CreationStepState[] = CREATION_STEPS.map((_, index) =>
    failed && index === 3 ? "failed" : completed[index] ? "done" : index === active ? "current" : "pending");
  const hint = failed ? "failed"
    : ready ? "ready"
      : approved ? (running ? "deploying" : "unavailable")
        : resourcesDirty ? "unsaved"
          : editing ? "editing"
            : latest?.status === "invalid" ? "invalid"
              : latest?.status === "rejected" ? "rejected"
                : valid ? "review" : latest ? "resources" : "goals";
  return { approved, failed, ready: Boolean(ready), done, active, states, hint };
}

// ─── evaluation-plan row edits (each one saves a new plan revision) ───────

export function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

export function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

/** A rule without its empty / default members (what a reviewer needs to read). */
export function compactRule(rule: unknown): string {
  if (!rule || typeof rule !== "object" || Array.isArray(rule)) return JSON.stringify(rule ?? null);
  const kept = Object.fromEntries(
    Object.entries(rule as Record<string, unknown>).filter(
      ([, v]) => !(v === null || v === "" || v === false || (Array.isArray(v) && v.length === 0)),
    ),
  );
  return JSON.stringify(kept);
}

type PlanContent = Record<string, unknown>;

/** Confirm review-required scenarios: all of them, or only `goldenTestId`. */
export function planConfirmScenarios(content: PlanContent, goldenTestId?: string): PlanContent {
  const scenarios = asArray<Record<string, unknown>>(content.scenarios).map((s) =>
    goldenTestId === undefined || String(s.golden_test_id) === goldenTestId
      ? { ...s, review_required: false }
      : s,
  );
  return { ...content, scenarios };
}

/** BLOCK one golden test: it leaves `scenarios`, lands in `blocked_golden_tests` with
 *  the member's reason, and is dropped from every evaluator's `golden_test_ids`. */
export function planBlockGoldenTest(content: PlanContent, goldenTestId: string, reason: string): PlanContent {
  const scenarios = asArray<Record<string, unknown>>(content.scenarios).filter(
    (s) => String(s.golden_test_id) !== goldenTestId,
  );
  const blockedList = asArray<Record<string, unknown>>(content.blocked_golden_tests).filter(
    (b) => String(b.golden_test_id) !== goldenTestId,
  );
  const evaluators = asArray<Record<string, unknown>>(content.evaluators).map((e) => ({
    ...e,
    golden_test_ids: asArray<string>(e.golden_test_ids).filter((g) => g !== goldenTestId),
  }));
  return {
    ...content,
    scenarios,
    evaluators,
    blocked_golden_tests: [...blockedList, { golden_test_id: goldenTestId, reason }],
  };
}

/** UNBLOCK: re-draft the golden test as ONE single-turn review-required scenario —
 *  the same shape the platform draft uses — from the proposal revision the plan is
 *  bound to. `null` when that revision no longer carries the golden test. */
export function planUnblockGoldenTest(
  content: PlanContent,
  proposals: AssistantProposal[],
  goldenTestId: string,
): PlanContent | null {
  const source = proposals.find((p) => p.revision === Number(content.source_revision));
  const gt = asArray<Record<string, unknown>>(source?.content.golden_tests).find(
    (g) => String(g.id) === goldenTestId,
  );
  if (!gt) return null;
  const assertions: string[] = [];
  if (gt.pass_criteria) assertions.push(String(gt.pass_criteria).slice(0, 1000));
  if (gt.forbidden_behavior) assertions.push(`Must not: ${String(gt.forbidden_behavior)}`.slice(0, 1000));
  const scenario = {
    scenario_id: goldenTestId.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "") || "gt",
    golden_test_id: goldenTestId,
    turns: [{
      input: String(gt.input ?? "").slice(0, 8000),
      expected_response: String(gt.expected_response ?? "").slice(0, 2000),
    }],
    expected_trajectory: asArray<unknown>(gt.expected_tools).map((x) => String(x).slice(0, 200)),
    assertions,
    note: "re-drafted from the golden test after unblocking — confirm, rewrite or block it",
    review_required: true,
  };
  const blockedList = asArray<Record<string, unknown>>(content.blocked_golden_tests).filter(
    (b) => String(b.golden_test_id) !== goldenTestId,
  );
  return {
    ...content,
    scenarios: [...asArray<Record<string, unknown>>(content.scenarios), scenario],
    blocked_golden_tests: blockedList,
  };
}

// ─── knowledge-base readiness (preparation) ───────────────────────────────

export type AssistantKbDetail = Awaited<ReturnType<typeof api.getKnowledgeBase>>;
export type AssistantKbSource = AssistantKbDetail["data_sources"][number];
export type AssistantKbJob = NonNullable<AssistantKbSource["ingestion_jobs"]>[number];

export const KB_RUNNING_JOBS = new Set(["STARTING", "IN_PROGRESS", "STOPPING"]);
export const KB_COMPLETE_JOBS = new Set(["COMPLETE", "COMPLETED"]);
export const KB_NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;

export function kbHasRunningJob(source: AssistantKbSource): boolean {
  return (source.ingestion_jobs ?? []).some((job) => KB_RUNNING_JOBS.has(job.status));
}

/** The data source the assistant's KB upload writes into. */
export function kbUploadSource(detail: AssistantKbDetail): AssistantKbSource | undefined {
  return detail.data_sources.find((source) => source.prefix === `kb/${detail.kb_id}/`)
    ?? (detail.data_sources.length === 1 ? detail.data_sources[0] : undefined);
}

/** Read-only index readiness of a selected KB (`assistantPreparation.<key>`), and
 *  whether it is still moving (keep polling). */
export function kbReadiness(detail: AssistantKbDetail | null, failed: boolean) {
  const jobs = detail?.data_sources.map((source) =>
    [...(source.ingestion_jobs ?? [])].sort((a, b) =>
      (b.started_at ?? "").localeCompare(a.started_at ?? ""))[0]);
  const indexError = detail?.status === "FAILED"
    || detail?.data_sources.some((source) => source.status === "FAILED")
    || jobs?.some((job) => job && (["FAILED", "STOPPED"].includes(job.status)
      || (job.statistics?.numberOfDocumentsFailed ?? 0) > 0));
  const completed = detail?.status === "ACTIVE" && jobs && jobs.length > 0
    && jobs.every((job) => job?.status === "COMPLETE");
  const pending = detail?.status === "CREATING" || detail?.data_sources.some(
    (source) => source.status === "CREATING",
  ) || jobs?.some((job) => job && KB_RUNNING_JOBS.has(job.status));
  const key = failed ? "indexUnknown" : !detail ? "indexLoading"
    : indexError ? "indexFailed" : completed ? "indexComplete"
      : pending ? "indexPending" : "indexNotStarted";
  return { key, pending: Boolean(pending) };
}

// ─── CLEAR a conversation (durable clear job) ─────────────────────────────

/** The canaries a CLEAR will stop and remove (cleanup not run, or ran incomplete) and
 * the already-cleaned ones it only re-reads — both are listed so nothing is hidden. */
export function clearCanaryList(fp: AssistantConversationFootprint): string {
  return fp.canaries
    .map((c) => {
      const res = c.resources;
      const parts = [
        res.ab_test ? "A/B test" : "",
        res.online_evaluations ? `${res.online_evaluations} online eval` : "",
        res.gateway ? `gateway ${res.gateway}` : "",
        res.endpoints.length ? `endpoints ${res.endpoints.join(", ")}` : "",
      ].filter(Boolean);
      return `${c.name} (${c.status}${parts.length ? " · " + parts.join(" · ") : ""})`;
    })
    .join("; ");
}

/** A conversation title for a heading or a dialog title: older conversations were
 *  titled with their whole first message, so a display cut keeps them to one line's
 *  worth (code points, not UTF-16 units); the full title stays in a `title` tooltip. */
export function shortConversationTitle(title: string | null | undefined, fallback: string, max = 40): string {
  const text = (title ?? "").replace(/\s+/g, " ").trim() || fallback;
  const chars = Array.from(text);
  return chars.length <= max ? text : `${chars.slice(0, max - 1).join("").trimEnd()}…`;
}

/** The kinds a CLEAR names for evaluation-assets cloud resources, in dialog order. */
export const CLEAR_CLOUD_KINDS = [
  "evaluator",
  "lambda_function",
  "lambda_role",
  "log_group",
  "lambda_permission",
  "role_grant",
] as const;

export type ClearCloudKind = (typeof CLEAR_CLOUD_KINDS)[number] | "other" | "unknown";

/**
 * What the clear deletes across the conversation's evaluation-assets operations,
 * per kind (`cloud_kinds` from the footprint), so the confirm lists exactly those —
 * no Lambda line when the plan made no Lambda. A kind this console does not know
 * counts as `other`; an operation from a server without `cloud_kinds` contributes
 * its bare total as `unknown`.
 */
export function clearCloudKinds(
  operations: AssistantConversationFootprint["operations"],
): { kind: ClearCloudKind; count: number }[] {
  const totals = new Map<ClearCloudKind, number>();
  const add = (kind: ClearCloudKind, n: number) => {
    if (n > 0) totals.set(kind, (totals.get(kind) ?? 0) + n);
  };
  for (const op of operations) {
    if (!op.cloud_kinds) {
      add("unknown", op.cloud_resources);
      continue;
    }
    for (const [kind, n] of Object.entries(op.cloud_kinds)) {
      add((CLEAR_CLOUD_KINDS as readonly string[]).includes(kind) ? (kind as ClearCloudKind) : "other", n);
    }
  }
  const order: ClearCloudKind[] = [...CLEAR_CLOUD_KINDS, "other", "unknown"];
  return order.filter((k) => totals.has(k)).map((kind) => ({ kind, count: totals.get(kind)! }));
}

type ClearT = (key: string, options?: Record<string, unknown>) => string;

/** The confirm's evaluation-assets line: the operations and, per kind, exactly the
 *  cloud resources the clear deletes (or that it deletes none). */
export function clearOperationsText(t: ClearT, operations: AssistantConversationFootprint["operations"]): string {
  const kinds = clearCloudKinds(operations);
  if (!kinds.length) return t("assistantPage.clear.operationsNone", { n: operations.length });
  const list = kinds
    .map((k) => t(`assistantPage.clear.kinds.${k.kind}`, { count: k.count }))
    .join(t("assistantPage.clear.listSep"));
  return t("assistantPage.clear.operations", { n: operations.length, list });
}

/** The confirm's Agents line: each Agent with its creation method and status (a
 *  conversation may deploy a Harness or a Runtime). */
export function clearAgentList(fp: AssistantConversationFootprint, label: (method: string) => string): string {
  return fp.agents.map((a) => `${a.name} (${label(a.method)} · ${a.status})`).join(", ");
}

export function clearExperimentList(fp: AssistantConversationFootprint): string {
  return fp.experiments
    .filter((e) => e.pending)
    .map((e) => `${e.name} (${e.status})`)
    .join(", ");
}

/** A clear job is still live (a repeated DELETE follows it instead of starting one). */
export function clearInProgress(fp: AssistantConversationFootprint): boolean {
  return fp.purge?.status === "queued" || fp.purge?.status === "running";
}

/** Human text for a clear job's current step (`canary:<id> · waiting · endpoint:x (DELETING)`). */
/** The phases of a CLEAR job, in the order the worker runs them (`purge._STEPS`, then
 *  the ledger rows), as the dialog names them. */
export const CLEAR_PHASES = [
  "canaries",
  "experiments",
  "operations",
  "datasets",
  "agents",
  "canary_artifacts",
  "ledger",
] as const;
export type ClearPhase = (typeof CLEAR_PHASES)[number];

/** A job step (`{key: "agent:<id>", state: "deleting", …}`) → the phase the dialog
 *  shows in words, and whether it finished; null for a step this console does not know
 *  (the raw `clearStepText` is shown instead). */
export function clearStepPhase(
  step: AssistantPurgeJobState["step"] | undefined | null,
): { phase: ClearPhase; done: boolean } | null {
  if (!step?.key) return null;
  const head = step.key.split(":", 1)[0];
  const phase = (
    { canary: "canaries", experiment: "experiments", operation: "operations", dataset: "datasets",
      agent: "agents", canary_artifacts: "canary_artifacts", ledger: "ledger" } as Record<string, ClearPhase>
  )[head] ?? ((CLEAR_PHASES as readonly string[]).includes(head) ? (head as ClearPhase) : null);
  return phase ? { phase, done: step.state === "done" } : null;
}

/** Whole minutes and seconds since `startedAt`, for the clear's progress line. */
export function clearElapsed(startedAt: number, now: number): { minutes: number; seconds: number } {
  const total = Math.max(0, Math.floor((now - startedAt) / 1000));
  return { minutes: Math.floor(total / 60), seconds: total % 60 };
}

export function clearStepText(step: AssistantPurgeJobState["step"] | undefined | null): string {
  if (!step) return "";
  return [step.key, step.state, step.detail].filter(Boolean).join(" · ");
}

export type ClearOutcome =
  | { kind: "done"; result: AssistantConversationPurgeResult }
  | { kind: "failed"; reason: string }
  | { kind: "detached" };

/**
 * DELETE the conversation; a cloud clear answers with a durable job, which is polled
 * until it is terminal. `onProgress` receives each step's text; `detached()` returning
 * true stops polling (the job keeps running server-side).
 */
export async function runConversationClear(
  id: string,
  opts: {
    onProgress?: (text: string, step?: AssistantPurgeJobState["step"] | null) => void;
    detached?: () => boolean;
    pollMs?: number;
    sleep?: (ms: number) => Promise<void>;
  } = {},
): Promise<ClearOutcome> {
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const res = await api.assistantDeleteConversation(id);
  if (res.deleted || !res.job_id) return { kind: "done", result: res };
  const jobId = res.job_id;
  for (;;) {
    if (opts.detached?.()) return { kind: "detached" };
    const job = await api.getJob(jobId);
    const payload = (job.payload ?? {}) as {
      step?: AssistantPurgeJobState["step"];
      blocker?: AssistantPurgeBlocker | null;
      result?: Partial<AssistantConversationPurgeResult>;
    };
    if (job.status === "succeeded") {
      return {
        kind: "done",
        result: {
          operations_cleaned: [],
          datasets: [],
          agents: [],
          ...payload.result,
          deleted: true,
          conversation_id: id,
          job_id: jobId,
          status: job.status,
        },
      };
    }
    if (job.status === "failed") {
      return { kind: "failed", reason: payload.blocker?.message || job.error || job.status };
    }
    opts.onProgress?.(clearStepText(payload.step) || job.status, payload.step ?? null);
    await sleep(opts.pollMs ?? ASSISTANT_JOB_POLL_MS);
  }
}
