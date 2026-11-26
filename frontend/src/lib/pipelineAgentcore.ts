// 数据处理 · AgentCore 生成 source helpers (P2): which agents and sessions the
// editor offers, the source as the API takes it, and which datasets can receive
// the result. The backend re-checks all of it (design.md "API contract"); these
// only keep the editor honest about what a run will send.
import type { AgentInfo, ObsSessionRow, V2Dataset, V2PipelineConfig, V2PipelineRedaction, V2PipelineSource } from "./api";
import type { EvaluationRunInfo, EvaluationRunResultRow, EvaluationRunResults } from "./evaluation";
import { evaluatorPolarity } from "./evaluators";

/** sessions per generation (AWS `identifiers.sessionIds` ≤ 25) */
export const AGENTCORE_MAX_SESSIONS = 25;
/** sessions active this recently are still exporting telemetry (spike: SPANS_INCOMPLETE) */
export const FRESH_MINUTES = 10;
/** the methods platform evaluation supports — the only agents a generation can read */
export const GENERATION_METHODS: ReadonlySet<AgentInfo["method"]> = new Set(["zip_runtime", "studio", "container", "harness", "byoc"]);
/** a curated subset of the CloudWatch managed data identifiers */
export const MANAGED_REDACTION_IDS = [
  "EMAIL_ADDRESS",
  "PHONE_NUMBER_US",
  "NAME",
  "ADDRESS",
  "CREDIT_CARD_NUMBER",
  "IP_ADDRESS",
  "AWS_SECRET_KEY",
  "SSN_US",
  "PASSPORT_NUMBER_US",
  "DRIVERS_LICENSE_US",
] as const;
export const MAX_CUSTOM_IDENTIFIERS = 10;
const CUSTOM_NAME_RE = /^[A-Za-z0-9_]{1,128}$/;
const LOOKAROUND_RE = /\(\?<?[=!]/;

export type ScenarioType = NonNullable<V2PipelineSource["scenario_type"]>;

export const scenarioTypeOf = (src: V2PipelineSource): ScenarioType => src.scenario_type ?? "FIXED";

/** Agents a generation can read: active, of an eval-supported method, by name. */
export function generationAgents(agents: AgentInfo[]): AgentInfo[] {
  return agents
    .filter((a) => a.status === "active" && GENERATION_METHODS.has(a.method))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export interface AgentcorePreview {
  /** what a run would send now, newest first, at most `max_sessions` */
  picked: ObsSessionRow[];
  /** sessions that match but are still within the freshness window (skipped as `too_fresh`) */
  fresh: ObsSessionRow[];
  /** matching sessions beyond `max_sessions` */
  overflow: number;
  /** DYNAMIC only: single-turn sessions left out (they yield no persona) */
  singleTurn: number;
}

const lastMs = (s: ObsSessionRow) => (s.last ? Date.parse(s.last) : Number.NaN);

/** The design's preview rule: the agent's own sessions, status filter, DYNAMIC
 *  keeps multi-turn (`traces ≥ 2`) only, sessions active in the last
 *  {@link FRESH_MINUTES} minutes are held back, then the newest `max_sessions`.
 *  Sessions already in the target dataset are dropped by the backend at run time. */
export function previewAgentcoreSessions(
  rows: ObsSessionRow[],
  src: V2PipelineSource,
  agentName: string | null,
  now: number = Date.now(),
): AgentcorePreview {
  const empty: AgentcorePreview = { picked: [], fresh: [], overflow: 0, singleTurn: 0 };
  if (!agentName) return empty;
  const dynamic = scenarioTypeOf(src) === "DYNAMIC";
  const cutoff = now - FRESH_MINUTES * 60_000;
  let singleTurn = 0;
  const ready: ObsSessionRow[] = [];
  const fresh: ObsSessionRow[] = [];
  for (const s of rows) {
    if (s.agent !== agentName) continue;
    if (src.status === "error" && s.errors === 0) continue;
    if (src.status === "ok" && s.errors > 0) continue;
    if (dynamic && s.traces < 2) {
      singleTurn += 1;
      continue;
    }
    const last = lastMs(s);
    // no timestamp = unknown age: hold it back rather than send a half-exported session
    if (!(last <= cutoff)) fresh.push(s);
    else ready.push(s);
  }
  const newest = (a: ObsSessionRow, b: ObsSessionRow) => (lastMs(b) || 0) - (lastMs(a) || 0);
  ready.sort(newest);
  fresh.sort(newest);
  const take = clampSessions(src.max_sessions);
  return { picked: ready.slice(0, take), fresh, overflow: Math.max(0, ready.length - take), singleTurn };
}

export const clampSessions = (n: number) => Math.max(1, Math.min(AGENTCORE_MAX_SESSIONS, Math.floor(Number(n) || 1)));

/** The dataset kind a scenario type writes into (legacy is never a target). */
export const targetKind = (t: ScenarioType): "predefined" | "simulated" => (t === "DYNAMIC" ? "simulated" : "predefined");

/** Existing datasets a pipeline may write into: an agentcore source only into
 *  its scenario type's kind; the local extractors into anything but simulated. */
export function receivableDatasets(datasets: V2Dataset[], src: V2PipelineSource): V2Dataset[] {
  if (src.type !== "agentcore") return datasets.filter((d) => d.kind !== "simulated");
  const kind = targetKind(scenarioTypeOf(src));
  return datasets.filter((d) => d.kind === kind);
}

/** Redaction as the API takes it: managed ids deduped in curated order (unknown
 *  ones kept after), blank custom rows dropped, the rest trimmed; null when empty. */
export function cleanRedaction(r: V2PipelineRedaction | null | undefined): V2PipelineRedaction | null {
  if (!r) return null;
  const ids = [...new Set(r.identifiers.map((i) => i.trim()).filter(Boolean))];
  const curated = MANAGED_REDACTION_IDS.filter((id) => ids.includes(id));
  const rest = ids.filter((id) => !(MANAGED_REDACTION_IDS as readonly string[]).includes(id));
  const custom = r.custom
    .map((c) => ({ name: c.name.trim(), regex: c.regex.trim() }))
    .filter((c) => c.name || c.regex);
  if (!curated.length && !rest.length && !custom.length) return null;
  return { identifiers: [...curated, ...rest], custom };
}

export type RedactionProblem = "customName" | "customRegex" | "customLookaround" | "customDuplicate" | "customTooMany";

export const REDACTION_PROBLEM_KEY: Record<RedactionProblem, string> = {
  customName: "v2.pipelines.ac.errCustomName",
  customRegex: "v2.pipelines.ac.errCustomRegex",
  customLookaround: "v2.pipelines.ac.errCustomLookaround",
  customDuplicate: "v2.pipelines.ac.errCustomDuplicate",
  customTooMany: "v2.pipelines.ac.errCustomTooMany",
};

/** The first custom identifier the API (or AWS) would refuse. */
export function redactionProblem(r: V2PipelineRedaction | null | undefined): RedactionProblem | null {
  const clean = cleanRedaction(r);
  if (!clean) return null;
  if (clean.custom.length > MAX_CUSTOM_IDENTIFIERS) return "customTooMany";
  const names = new Set<string>();
  for (const c of clean.custom) {
    if (!CUSTOM_NAME_RE.test(c.name)) return "customName";
    if (!c.regex || c.regex.length > 200) return "customRegex";
    if (LOOKAROUND_RE.test(c.regex)) return "customLookaround";
    try {
      new RegExp(c.regex);
    } catch {
      return "customRegex";
    }
    if (names.has(c.name)) return "customDuplicate";
    names.add(c.name);
  }
  return null;
}

/** An agentcore source as the API takes it: only its own fields, the session
 *  cap clamped to 25, trajectory ground truth off for DYNAMIC, redaction cleaned,
 *  the run threshold clamped to 0..1 (null = every scored session).
 *  `agent` carries the picked agent's name for display only (ignored by the API). */
export function cleanAgentcoreSource(src: V2PipelineSource, agentName: string | null): V2PipelineSource {
  const scenario = scenarioTypeOf(src);
  return {
    batch_run_id: src.batch_run_id || null,
    max_score: src.max_score === null ? null : clampScore(src.max_score ?? DEFAULT_MAX_SCORE),
    type: "agentcore",
    agent: agentName,
    agent_id: src.agent_id ?? null,
    range: src.range,
    status: src.status,
    max_sessions: clampSessions(src.max_sessions),
    scenario_type: scenario,
    expected_trajectory: scenario === "FIXED" ? src.expected_trajectory !== false : false,
    redaction: cleanRedaction(src.redaction),
  };
}

/** Processing for an agentcore source: the local-extractor-only knobs go back to
 *  their defaults, and `keep_replies` (= expected_response) is off for DYNAMIC. */
export function cleanAgentcoreProcessing(
  proc: V2PipelineConfig["processing"],
  scenario: ScenarioType,
): V2PipelineConfig["processing"] {
  return {
    first_turn_only: false,
    min_input_chars: 0,
    dedupe: proc.dedupe,
    keep_replies: scenario === "FIXED" ? proc.keep_replies !== false : false,
  };
}

/** `results` keys of a generation, in reading order */
export const GENERATION_COUNTS = [
  "sessionsMatched",
  "sessionsSelected",
  "sessionsUnscored",
  "sessionsFailed",
  "scenariosGenerated",
  "scenariosDeduplicated",
  "examplesWritten",
  "examplesWithGroundTruth",
] as const;

// ─── P3: sessions from an evaluation run ───────────────────────────────────

/** the console's bad-case line (`v2/format.ts BAD_CASE_THRESHOLD`) */
export const DEFAULT_MAX_SCORE = 0.7;
/** session ids AgentCore accepts (backend `generation.SESSION_ID_RE`) */
const SESSION_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;

export const clampScore = (n: number) => (Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : DEFAULT_MAX_SCORE);

export type SessionsFrom = "traffic" | "run";

/** "" (run mode, no run picked yet) counts as run mode in the editor; the body
 *  sends `batch_run_id: null` only for recent traffic. */
export const sessionsFromOf = (src: V2PipelineSource): SessionsFrom => (src.batch_run_id != null ? "run" : "traffic");

/** Runs a generation can read: the agent's completed evaluators runs, newest first. */
export function generationRuns(runs: EvaluationRunInfo[], agentId: string | null | undefined): EvaluationRunInfo[] {
  if (!agentId) return [];
  const at = (r: EvaluationRunInfo) => (r.created_at ? Date.parse(r.created_at) : 0) || 0;
  return runs
    .filter((r) => r.agent_id === agentId && r.mode === "evaluators" && r.status === "completed")
    .sort((a, b) => at(b) - at(a));
}

/** A session's lowest normalized score (higher = better; lower-is-better evaluators
 *  inverted), failed judgements ignored; null when nothing scored. Same rule as the
 *  backend's `generation.lowest_normalized` and `v2/format.ts normalizedScore`. */
export function lowestNormalized(results: EvaluationRunResultRow[]): number | null {
  const values = results
    .filter((r) => r.score != null && Number.isFinite(r.score))
    .map((r) => (evaluatorPolarity(r.evaluator_id) < 0 ? 1 - (r.score as number) : (r.score as number)));
  return values.length ? Math.min(...values) : null;
}

export type RunVerdict = "send" | "above" | "unscored" | "unsupported" | "overflow";

export interface RunPreviewRow {
  session_id: string;
  lowest: number | null;
  verdict: RunVerdict;
}

/** What a run-sourced generation would send, mirroring the backend's
 *  `select_run_sessions`: the run's own session order (the results' order when the
 *  run lists none), unscored / above-threshold sessions skipped, ids AgentCore
 *  cannot take skipped, then the first `max_sessions`. Private sessions of other
 *  members and sessions already in the target dataset are dropped server-side. */
export function previewRunSessions(results: EvaluationRunResults | null, runSessionIds: string[], src: V2PipelineSource): RunPreviewRow[] {
  if (!results) return [];
  const bySession = new Map(results.sessions.map((s) => [s.session_id, s.results]));
  const order = [...new Set(runSessionIds.length ? runSessionIds : results.sessions.map((s) => s.session_id))];
  const max = src.max_score === null ? null : clampScore(src.max_score ?? DEFAULT_MAX_SCORE);
  const limit = clampSessions(src.max_sessions);
  let sent = 0;
  return order.map((session_id) => {
    const lowest = lowestNormalized(bySession.get(session_id) ?? []);
    let verdict: RunVerdict;
    if (lowest === null) verdict = "unscored";
    else if (max !== null && lowest >= max) verdict = "above";
    else if (!SESSION_ID_RE.test(session_id)) verdict = "unsupported";
    else if (sent < limit) {
      verdict = "send";
      sent += 1;
    } else verdict = "overflow";
    return { session_id, lowest, verdict };
  });
}
