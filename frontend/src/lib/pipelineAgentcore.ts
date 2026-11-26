// 数据处理 · AgentCore 生成 source helpers (P2): which agents and sessions the
// editor offers, the source as the API takes it, and which datasets can receive
// the result. The backend re-checks all of it (design.md "API contract"); these
// only keep the editor honest about what a run will send.
import type { AgentInfo, ObsSessionRow, V2Dataset, V2PipelineConfig, V2PipelineRedaction, V2PipelineSource } from "./api";

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
 *  cap clamped to 25, trajectory ground truth off for DYNAMIC, redaction cleaned.
 *  `agent` carries the picked agent's name for display only (ignored by the API). */
export function cleanAgentcoreSource(src: V2PipelineSource, agentName: string | null): V2PipelineSource {
  const scenario = scenarioTypeOf(src);
  return {
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
