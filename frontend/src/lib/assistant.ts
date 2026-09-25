/**
 * Architect assistant (SE-039 / SE-047) — the transport and pure business rules
 * shared by the classic `/create/assistant` page and the native V2 `/v2/assistant`
 * page: the SSE turn stream, the transcript projection, the proposal edit draft
 * and its validation, the creation-progress state machine, the evaluation-plan
 * row edits and the knowledge-base readiness rules. Nothing here renders.
 */
import { DEFAULT_TIMEOUT_SECONDS } from "./agent-defaults";
import {
  ApiError,
  api,
  AUTH_UNAUTHORIZED_EVENT,
  HARNESS_NATIVE_TOOLS,
  type AssistantConversationDetail,
  type AssistantFishbone,
  type AssistantMessage,
  type AssistantProposal,
  type AssistantProposalContent,
  type AssistantTurnRequest,
  type HarnessNativeTool,
} from "./api";
import { MODEL_CATALOG, type ModelSource } from "./models";
import { WORKSPACE_HEADER } from "./workspace-header";

export const ASSISTANT_JOB_POLL_MS = 3000;
export const PROPOSAL_NAME_RE = /^[a-z][a-z0-9-]{2,47}$/;
export const PROPOSAL_MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$/;
const PROPOSAL_FENCE_RE = /```launchpad-proposal[ \t]*\r?\n[\s\S]*?\r?\n[ \t]*```/g;
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
  streaming?: boolean;
}

export function toLiveMessages(rows: AssistantMessage[]): AssistantLiveMessage[] {
  return rows.map((m) => ({ role: m.role, text: m.text, name: m.name }));
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
      : [],
    skills: Array.isArray(content.skills) ? content.skills.map(String) : [],
    knowledge_bases: Array.isArray(content.knowledge_bases) ? content.knowledge_bases.map(String) : [],
    memory: content.memory === "workspace" ? "workspace" : "disabled",
    max_iterations: Number(content.max_iterations ?? 10),
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
  };
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
