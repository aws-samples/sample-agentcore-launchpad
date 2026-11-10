import type { TFunction } from "i18next";
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
  type AssistantCatalog,
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
  type SharedMemoryKind,
  type ValidationErrorDetail,
} from "./api";
import { MODEL_CATALOG, type ModelSource } from "./models";
import { WORKSPACE_HEADER } from "./workspace-header";
import { compareTimesDesc } from "./timestamps";

export const ASSISTANT_JOB_POLL_MS = 3000;

/** Which shared memory a conversation's catalog snapshot offers (N-ARCH-bug1). The
 * snapshot carries the resolved `memory_kind`; one taken before that field existed
 * only knew the Global `memory_arn`, which is still read as AgentCore Memory. */
export function catalogMemoryKind(
  res: AssistantCatalog["resources"] | undefined,
): SharedMemoryKind | null {
  if (!res) return null;
  if (res.memory_kind) return res.memory_kind;
  return res.memory_arn ? "agentcore" : null;
}
export const PROPOSAL_NAME_RE = /^[a-z][a-z0-9-]{2,47}$/;
export const PROPOSAL_MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$/;
// the full proposal and a revision of the stored one (`launchpad-proposal-patch`)
const PROPOSAL_FENCE_RE = /```launchpad-proposal(?:-patch)?[ \t]*\r?\n[\s\S]*?\r?\n[ \t]*```/g;
const PREPARATION_FENCE_RE = /```launchpad-preparation[ \t]*\r?\n[\s\S]*?\r?\n[ \t]*```/g;

// ─── SSE turn stream ──────────────────────────────────────────────────────

export interface SseFrame {
  event: string;
  data: never;
  /** the `id:` field — an assistant turn's event-log `seq` (N-ARCH-obs15); absent otherwise */
  seq?: number;
}

/** Parse a `text/event-stream` response body into `{event, data, seq?}` frames. */
export async function* sseEvents(res: Response): AsyncGenerator<SseFrame> {
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
      let seq: number | undefined;
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
        if (line.startsWith("id:")) {
          const n = Number(line.slice(3).trim());
          if (Number.isFinite(n)) seq = n;
        }
      }
      if (data) yield { event, data: JSON.parse(data) as never, ...(seq !== undefined ? { seq } : {}) };
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

/**
 * `GET …/conversations/{id}/turns/{turn}/stream?from=<seq>` — reattach to a turn that
 * runs (or ran) server-side: replays the events after `fromSeq`, tails the producer,
 * ends with its terminal event (N-ARCH-obs15). 404 unknown turn; 409
 * `assistant.turn_not_yours` for another member's turn.
 */
export async function openAssistantTurnStream(
  conversationId: string,
  turn: number,
  fromSeq: number,
  workspaceId: string | null,
  signal: AbortSignal,
): Promise<Response> {
  const res = await fetch(
    `/api/assistant/architect/conversations/${encodeURIComponent(conversationId)}/turns/${turn}/stream?from=${fromSeq}`,
    {
      method: "GET",
      headers: { ...(workspaceId ? { [WORKSPACE_HEADER]: workspaceId } : {}) },
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
  /** the streaming row's connection dropped; the turn keeps running server-side and the
   *  tab is reattaching (N-ARCH-obs15) */
  reconnecting?: boolean;
  /** the stored turn number; absent on rows streamed in this tab until the reload */
  turn?: number;
  /** coded errors of a notice row (N17-obs296) — the backend's `errors[]` on a stored
   *  row, the proposal's `validation_error_details` on a row streamed in this tab */
  errors?: ValidationErrorDetail[];
  /** N17-obs468: a "no proposal block" notice — the revision that stayed current
   *  (`base`, null before any proposal) and the one a re-ask stored (`revision`) */
  missing?: { base: number | null; revision: number | null };
}

/** Backend ``TURN_RETRIED_NAME``: marks a failed turn the member retried (never replayed). */
export const TURN_RETRIED_NAME = "turn_retried";
/** Backend ``TURN_INTERRUPTED_NAME``: the turn stopped before the reply finished — the
 *  producer failed, lost its claim or its process died (N-ARCH-obs12 / obs15; a closed
 *  connection no longer does this). What streamed is kept and a complete proposal block
 *  was recorded. Still retryable. */
export const TURN_INTERRUPTED_NAME = "turn_interrupted";
export const PROPOSAL_REJECTED_NAME = "proposal_rejected";
/** Backend ``AUTOCORRECT_NAMES`` (N17-obs295): the notice of the turn's automatic
 *  proposal correction — written pending, renamed to its outcome. A notice, never a
 *  failed turn. */
export const PROPOSAL_AUTOCORRECT_PENDING = "proposal_autocorrect_pending";
export const PROPOSAL_AUTOCORRECTED = "proposal_autocorrected";
export const PROPOSAL_AUTOCORRECT_FAILED = "proposal_autocorrect_failed";
export const AUTOCORRECT_NAMES: readonly string[] = [
  PROPOSAL_AUTOCORRECT_PENDING, PROPOSAL_AUTOCORRECTED, PROPOSAL_AUTOCORRECT_FAILED,
];
/** Backend ``MISSING_NAMES`` (N17-obs468): the reply carried no proposal block although it
 *  imitated the replay placeholder or claimed a submitted revision — written pending while
 *  the server asks once more, renamed to its outcome. A notice, never a failed turn. */
export const PROPOSAL_MISSING_PENDING = "proposal_missing_pending";
export const PROPOSAL_MISSING_RECOVERED = "proposal_missing_recovered";
export const PROPOSAL_MISSING = "proposal_missing";
export const MISSING_NAMES: readonly string[] = [
  PROPOSAL_MISSING_PENDING, PROPOSAL_MISSING_RECOVERED, PROPOSAL_MISSING,
];
/** error rows that are notices: a turn carrying only these did not fail */
export function isNoticeRow(m: { role: string; name?: string | null }): boolean {
  return m.role === "error" && (
    m.name === PROPOSAL_REJECTED_NAME || m.name === TURN_RETRIED_NAME
    || AUTOCORRECT_NAMES.includes(m.name ?? "") || MISSING_NAMES.includes(m.name ?? ""));
}

export function toLiveMessages(rows: AssistantMessage[]): AssistantLiveMessage[] {
  return rows.map((m) => ({
    role: m.role, text: m.text, name: m.name, author: m.author, turn: m.turn,
    ...(m.errors ? { errors: m.errors } : {}),
    ...(m.missing ? { missing: m.missing } : {}),
  }));
}

/** The latest turn when it failed and was not retried yet: `{turn, prompt}` to retry. */
export function retryableTurn(messages: AssistantLiveMessage[]): { turn: number; prompt: string } | null {
  const turns = messages.map((m) => m.turn).filter((n): n is number => typeof n === "number");
  if (!turns.length) return null;
  const latest = Math.max(...turns);
  const rows = messages.filter((m) => m.turn === latest);
  const failed = rows.some((m) => m.role === "error" && !isNoticeRow(m));
  const retried = rows.some((m) => m.role === "error" && m.name === TURN_RETRIED_NAME);
  const prompt = rows.find((m) => m.role === "user")?.text ?? "";
  return failed && !retried && prompt ? { turn: latest, prompt } : null;
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

export function proposalDraftFrom(content: AssistantProposal["content"]): ProposalEditDraft {
  return {
    name: String(content.name ?? ""),
    model_id: String(content.model_id ?? MODEL_CATALOG.bedrock[0].model_id),
    model_source: (content.model_source as ModelSource) ?? "bedrock",
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

/** The proposal fields the editor exposes (`ProposalEditor`): a member revision takes
 *  these from the draft. */
export const PROPOSAL_EDITED_KEYS = [
  "name",
  "model_id",
  "model_source",
  "system_prompt",
  "tools",
  "native_tools",
  "skills",
  "knowledge_bases",
  "memory",
  "max_iterations",
  "timeout_seconds",
] as const satisfies readonly (keyof ProposalEditDraft)[];

/** Every other key of the proposal contract (`backend/app/assistant/proposal.py::
 *  ProposalContent`), none of which the editor shows: a member revision carries each one
 *  VERBATIM from the revision being edited when that revision has it, and omits it when
 *  it does not (`evaluation_plan` / `fishbone` are optional there — an absent key must
 *  stay absent so an unchanged edit hashes like its base). N15-obs13: the rebuild used to
 *  enumerate the solution members by hand and lost `evaluation_plan`, so every member
 *  edit of a proposal with gate recommendations drafted a plan with no seed evaluators.
 *  `tool_functions` is carried too, narrowed to the still-selected tools (below). */
export const PROPOSAL_CARRIED_KEYS = [
  "summary",
  "requirements_baseline",
  "assumptions",
  "manual_tasks",
  "golden_tests",
  "evaluator_recommendations",
  "evaluation_plan",
  "fishbone",
  "builtin_tools",
] as const satisfies readonly (keyof AssistantProposalContent)[];

/** The new member revision: the edited fields from the draft, every contract key the
 *  form does not edit travelling with the revision unchanged. Keys outside the contract
 *  (the junk of an invalid revision, e.g. `code`) are not carried: the server refuses
 *  them and the editor cannot show them. */
export function proposalContentFromDraft(
  draft: ProposalEditDraft,
  base: AssistantProposal["content"],
): AssistantProposalContent {
  const carried: Record<string, unknown> = {};
  for (const key of PROPOSAL_CARRIED_KEYS) {
    if (key in base && base[key] !== undefined) carried[key] = base[key];
  }
  const edited = Object.fromEntries(
    PROPOSAL_EDITED_KEYS.map((key) => [key, draft[key]]),
  ) as Pick<ProposalEditDraft, (typeof PROPOSAL_EDITED_KEYS)[number]>;
  return {
    version: 1,
    ...carried,
    ...edited,
    // a reviewed function narrowing travels with its still-selected Gateway; dropping
    // it silently would widen that Gateway to every declared function
    ...carriedToolFunctions(base.tool_functions, draft.tools),
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
    // N17-obs376: [] means "no tools expected" — an unstated trajectory is omitted, as the
    // backend's plan materializer does
    ...(asArray<unknown>(gt.expected_tools).length > 0
      ? { expected_trajectory: asArray<unknown>(gt.expected_tools).map((x) => String(x).slice(0, 200)) }
      : {}),
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
    [...(source.ingestion_jobs ?? [])].sort((a, b) => compareTimesDesc(a.started_at, b.started_at))[0]);
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
export function clearCanaryList(fp: AssistantConversationFootprint, t?: TFunction): string {
  // N17-obs457: with `t` the parts and the canary status read in the console language
  return fp.canaries
    .map((c) => {
      const res = c.resources;
      const parts = [
        res.ab_test ? (t ? t("assistantPage.clear.part.abSplit") : "A/B split") : "",
        res.online_evaluations
          ? t ? t("assistantPage.clear.part.onlineEvals", { count: res.online_evaluations }) : `${res.online_evaluations} online eval`
          : "",
        res.endpoints.length
          ? t ? t("assistantPage.clear.part.endpoints", { list: res.endpoints.join(", ") }) : `endpoints ${res.endpoints.join(", ")}`
          : "",
      ].filter(Boolean);
      const status = t ? t(`v2.canary.status.${c.status}`, { defaultValue: c.status }) : c.status;
      return `${c.name} (${status}${parts.length ? " · " + parts.join(" · ") : ""})`;
    })
    .join("; ");
}

/** A clear job is still live (a repeated DELETE follows it instead of starting one). */
export function clearInProgress(fp: AssistantConversationFootprint): boolean {
  return fp.purge?.status === "queued" || fp.purge?.status === "running";
}

/** Human text for a clear job's current step (`canary:<id> · waiting · endpoint:x (DELETING)`). */
export function clearStepText(step: AssistantPurgeJobState["step"] | undefined | null, t?: TFunction): string {
  if (!step) return "";
  // N17-obs457: with `t` the step's state reads in the console language; the key (ids) and the
  // detail (the server's text) stay as written
  const state = step.state && t ? t(`assistantPage.clear.stepState.${step.state}`, { defaultValue: step.state }) : step.state;
  return [step.key, state, step.detail].filter(Boolean).join(" · ");
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
    /** `step` / `status`: the raw job state, for the caller to word (N17-obs457) */
    onProgress?: (text: string, step?: AssistantPurgeJobState["step"], status?: string) => void;
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
    opts.onProgress?.(clearStepText(payload.step) || job.status, payload.step, job.status);
    await sleep(opts.pollMs ?? ASSISTANT_JOB_POLL_MS);
  }
}
