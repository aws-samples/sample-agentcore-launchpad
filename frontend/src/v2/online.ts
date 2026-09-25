// 在线评估 (online evaluation configs) — shared state, draft encoding and the
// changed-fields PATCH, the same rules the classic online page applies.
import type { TFunction } from "i18next";

import type {
  AgentInfo,
  OnlineEvalConfigCreate,
  OnlineEvalConfigPatch,
  OnlineEvalConfigRow,
  OnlineEvalFilter,
  OnlineEvalFilterOperator,
  OnlineEvalFrequency,
  OnlineEvalMode,
  OnlineEvalReportRow,
} from "../lib/api";
import { ACTIVE_RUN_STATUSES } from "../lib/evaluation";
import type { TagTone } from "./ui";

export const TRANSIENT = new Set(["CREATING", "UPDATING", "DELETING"]);
const FAILED = new Set(["CREATE_FAILED", "UPDATE_FAILED", "ERROR"]);
export const POLL_MS = 8000;

export const INSIGHT_TYPES = [
  "Builtin.Insight.FailureAnalysis",
  "Builtin.Insight.UserIntent",
  "Builtin.Insight.ExecutionSummary",
] as const;
export const FREQUENCIES: OnlineEvalFrequency[] = ["DAILY", "WEEKLY", "MONTHLY"];
export const OPERATORS: OnlineEvalFilterOperator[] = [
  "Equals",
  "NotEquals",
  "GreaterThan",
  "LessThan",
  "GreaterThanOrEqual",
  "LessThanOrEqual",
  "Contains",
  "NotContains",
];
export const MAX_EVALUATORS = 10;
export const MAX_INSIGHTS = 3;
export const MAX_FILTERS = 5;
const FILTER_KEY_RE = /^[a-zA-Z0-9._-]{1,256}$/;
const SAMPLING_DEFAULT: Record<OnlineEvalMode, string> = { scores: "10", insights: "100" };
/** Agents an online config can watch (an imported runtime has no platform spans). */
const ELIGIBLE_METHODS = new Set<AgentInfo["method"]>(["zip_runtime", "studio", "container", "harness", "byoc"]);

export const eligibleAgent = (a: AgentInfo) => a.status === "active" && ELIGIBLE_METHODS.has(a.method);
export const isTransient = (row: OnlineEvalConfigRow) => TRANSIENT.has(row.status ?? "");
export const modeOf = (row: OnlineEvalConfigRow): OnlineEvalMode =>
  row.mode ?? (row.insights.length ? "insights" : "scores");
/** Only agent-owned configs are edited here; experiment-owned ones belong to their experiment. */
export const isEditable = (row: OnlineEvalConfigRow) => row.owner === "agent";
export const canToggle = (row: OnlineEvalConfigRow) => row.owner !== "experiment";

export function statusTone(status: string | null): TagTone {
  if (status === "ACTIVE") return "green";
  if (FAILED.has(status ?? "")) return "red";
  return "orange";
}

export const OWNER_TONE: Record<OnlineEvalConfigRow["owner"], TagTone> = {
  agent: "blue",
  experiment: "gray",
  external: "orange",
};

export function configName(row: OnlineEvalConfigRow): string {
  return row.name ?? row.config_id;
}

export function agentLabel(row: OnlineEvalConfigRow): string {
  return row.agent_name ?? (row.matched_agent ? `≈ ${row.matched_agent.name}` : "—");
}

export function insightLabel(t: TFunction, id: string): string {
  const key =
    id === "Builtin.Insight.FailureAnalysis"
      ? "failureAnalysis"
      : id === "Builtin.Insight.UserIntent"
        ? "userIntent"
        : id === "Builtin.Insight.ExecutionSummary"
          ? "executionSummary"
          : null;
  return key ? t(`evalPage.newRun.insightType.${key}`) : id;
}

export function filterText(f: OnlineEvalFilter): string {
  return `${f.key} ${f.operator} ${JSON.stringify(Object.values(f.value)[0])}`;
}

// ─── reports ───────────────────────────────────────────────────────────────
const REPORT_TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "STOPPED"]);

export const reportActive = (r: OnlineEvalReportRow) =>
  r.run_status ? ACTIVE_RUN_STATUSES.has(r.run_status) : !REPORT_TERMINAL.has(r.status ?? "");

export const reportKey = (r: OnlineEvalReportRow) => r.batch_id ?? r.run_id ?? "";

export function reportTone(status: string | null): TagTone {
  if (status === "COMPLETED") return "green";
  if (status === "COMPLETED_WITH_ERRORS") return "orange";
  if (status === "FAILED" || status === "STOPPED") return "red";
  return "blue";
}

// ─── editor draft ──────────────────────────────────────────────────────────
export type FilterKind = "string" | "number" | "boolean";

export interface FilterDraft {
  key: string;
  operator: OnlineEvalFilterOperator;
  kind: FilterKind;
  value: string;
}

export interface OnlineDraft {
  mode: OnlineEvalMode;
  description: string;
  evaluators: string[];
  insights: string[];
  frequencies: OnlineEvalFrequency[];
  sampling: string;
  samplingTouched: boolean;
  timeout: string;
  filters: FilterDraft[];
}

export function newDraft(): OnlineDraft {
  return {
    mode: "scores",
    description: "",
    evaluators: ["Builtin.GoalSuccessRate", "Builtin.Helpfulness"],
    insights: [...INSIGHT_TYPES],
    frequencies: ["DAILY"],
    sampling: SAMPLING_DEFAULT.scores,
    samplingTouched: false,
    timeout: "15",
    filters: [],
  };
}

/** Switching the mode moves the sampling default along unless it was typed. */
export function withMode(draft: OnlineDraft, mode: OnlineEvalMode): OnlineDraft {
  return { ...draft, mode, sampling: draft.samplingTouched ? draft.sampling : SAMPLING_DEFAULT[mode] };
}

export const emptyFilter = (): FilterDraft => ({ key: "session.id", operator: "Contains", kind: "string", value: "" });

export function encodeFilter(f: FilterDraft): OnlineEvalFilter {
  const value =
    f.kind === "number"
      ? { doubleValue: Number(f.value) }
      : f.kind === "boolean"
        ? { booleanValue: f.value === "true" }
        : { stringValue: f.value };
  return { key: f.key.trim(), operator: f.operator, value };
}

export function draftFromRow(row: OnlineEvalConfigRow): OnlineDraft {
  return {
    mode: modeOf(row),
    description: row.description ?? "",
    evaluators: [...row.evaluators],
    insights: [...row.insights],
    frequencies: row.clustering_frequencies.filter((f): f is OnlineEvalFrequency =>
      (FREQUENCIES as string[]).includes(f),
    ),
    sampling: String(row.sampling_percentage ?? 10),
    samplingTouched: true,
    timeout: String(row.session_timeout_minutes ?? 15),
    filters: row.filters.map((f) => {
      const v = f.value;
      if (v.doubleValue !== undefined) return { key: f.key, operator: f.operator, kind: "number", value: String(v.doubleValue) };
      if (v.booleanValue !== undefined) return { key: f.key, operator: f.operator, kind: "boolean", value: String(v.booleanValue) };
      return { key: f.key, operator: f.operator, kind: "string", value: v.stringValue ?? "" };
    }),
  };
}

/** First problem of the draft (null ⇒ valid). */
export function validateDraft(t: TFunction, d: OnlineDraft): string | null {
  const sampling = Number(d.sampling);
  if (!Number.isFinite(sampling) || sampling < 0.01 || sampling > 100) return t("v2.online.err.sampling");
  const timeout = Number(d.timeout);
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > 1440) return t("v2.online.err.timeout");
  if (d.mode === "insights") {
    if (d.insights.length < 1 || d.insights.length > MAX_INSIGHTS) return t("v2.online.err.insights", { max: MAX_INSIGHTS });
  } else if (d.evaluators.length < 1 || d.evaluators.length > MAX_EVALUATORS) {
    return t("v2.online.err.evaluators", { max: MAX_EVALUATORS });
  }
  if (d.filters.length > MAX_FILTERS) return t("v2.online.err.filterCount", { max: MAX_FILTERS });
  for (const f of d.filters) {
    if (!FILTER_KEY_RE.test(f.key.trim())) return t("v2.online.err.filterKey");
    if (f.kind === "string" && !f.value) return t("v2.online.err.filterValue");
    if (f.kind === "number" && (f.value.trim() === "" || !Number.isFinite(Number(f.value)))) return t("v2.online.err.filterValue");
  }
  if (d.description.length > 200) return t("v2.online.err.description");
  return null;
}

export function createBody(agentId: string, d: OnlineDraft, enable: boolean): OnlineEvalConfigCreate {
  return {
    agent_id: agentId,
    mode: d.mode,
    ...(d.mode === "insights" ? { insights: d.insights, clustering_frequencies: d.frequencies } : { evaluators: d.evaluators }),
    sampling_percentage: Number(d.sampling),
    session_timeout_minutes: Number(d.timeout),
    filters: d.filters.map(encodeFilter),
    description: d.description.trim() || null,
    enable_on_create: enable,
  };
}

/** Only the fields that differ from the stored config (list fields travel whole). */
export function patchBody(row: OnlineEvalConfigRow, d: OnlineDraft): OnlineEvalConfigPatch {
  const body: OnlineEvalConfigPatch = {};
  if (d.description !== (row.description ?? "")) body.description = d.description;
  if (modeOf(row) === "insights") {
    if (JSON.stringify(d.insights) !== JSON.stringify(row.insights)) body.insights = d.insights;
    if (JSON.stringify(d.frequencies) !== JSON.stringify(row.clustering_frequencies)) {
      body.clustering_frequencies = d.frequencies;
    }
  } else if (JSON.stringify(d.evaluators) !== JSON.stringify(row.evaluators)) {
    body.evaluators = d.evaluators;
  }
  if (Number(d.sampling) !== row.sampling_percentage) body.sampling_percentage = Number(d.sampling);
  if (Number(d.timeout) !== row.session_timeout_minutes) body.session_timeout_minutes = Number(d.timeout);
  const filters = d.filters.map(encodeFilter);
  if (JSON.stringify(filters) !== JSON.stringify(row.filters)) body.filters = filters;
  return body;
}
