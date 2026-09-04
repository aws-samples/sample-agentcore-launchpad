import type { CSSProperties, ReactNode } from "react";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import {
  Btn,
  Chip,
  ConfirmDialog,
  Pager,
  Panel,
  StaleLink,
  StatTile,
  useStaleParam,
  useTablePage,
  useToast,
  ViewHead,
} from "../components";
import type { ChipTone } from "../components/Chip";
import { EvaluationNav } from "../components/EvaluationNav";
import { InsightClusters } from "../components/InsightClusters";
import type {
  AgentInfo,
  OnlineEvalConfigCreate,
  OnlineEvalConfigPatch,
  OnlineEvalConfigRow,
  OnlineEvalFilter,
  OnlineEvalFilterOperator,
  OnlineEvalFrequency,
  OnlineEvalMode,
  OnlineEvalRange,
  OnlineEvalReportDetail,
  OnlineEvalReportRow,
  OnlineEvalReports,
  OnlineEvalResults,
} from "../lib/api";
import { api, ApiError } from "../lib/api";
import { ACTIVE_RUN_STATUSES, hasInsightTrees } from "../lib/evaluation";
import { evaluatorLabel, evaluatorPolarity } from "../lib/evaluators";

// ─── constants (mirror backend/app/evaluation/online*.py + online_routers.py) ──

const ONLINE_EVAL_MAX = 10; // CreateOnlineEvaluationConfig caps the list at 10
const ONLINE_EVAL_DEFAULT = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness"];
const MAX_FILTERS = 5;
const FILTER_KEY_RE = /^[a-zA-Z0-9._-]{1,256}$/;
const OPERATORS: OnlineEvalFilterOperator[] = [
  "Equals",
  "NotEquals",
  "GreaterThan",
  "LessThan",
  "GreaterThanOrEqual",
  "LessThanOrEqual",
  "Contains",
  "NotContains",
];
const RANGES: OnlineEvalRange[] = ["1h", "6h", "24h", "7d"];
type RangeKey = OnlineEvalRange;
// Insights mode (mutually exclusive with evaluators on one config): 1..3 insight
// types clustered into a report on each selected cadence.
const INSIGHT_TYPES = [
  "Builtin.Insight.FailureAnalysis",
  "Builtin.Insight.UserIntent",
  "Builtin.Insight.ExecutionSummary",
];
const INSIGHT_LABEL_KEYS: Record<string, string> = {
  "Builtin.Insight.FailureAnalysis": "failureAnalysis",
  "Builtin.Insight.UserIntent": "userIntent",
  "Builtin.Insight.ExecutionSummary": "executionSummary",
};
const INSIGHTS_MAX = 3;
const FREQUENCIES: OnlineEvalFrequency[] = ["DAILY", "WEEKLY", "MONTHLY"];
// AWS defaults: 10 % for scoring, 100 % for insights.
const SAMPLING_DEFAULT: Record<OnlineEvalMode, string> = {
  scores: "10",
  insights: "100",
};
const MODE_TONE: Record<OnlineEvalMode, ChipTone> = {
  scores: "muted",
  insights: "blue",
};
// Batch-evaluation terminal statuses (a report is one batch evaluation).
const REPORT_TERMINAL = new Set([
  "COMPLETED",
  "COMPLETED_WITH_ERRORS",
  "FAILED",
  "STOPPED",
]);
const REPORT_POLL_MS = 8000;
// Same set as EVAL_SUPPORTED_METHODS — online evaluation reads the agent's
// runtime telemetry, so discovered runtimes (no ledger telemetry) are out.
const ELIGIBLE_METHODS = new Set<AgentInfo["method"]>([
  "zip_runtime",
  "studio",
  "container",
  "harness",
]);
const TRANSIENT = new Set(["CREATING", "UPDATING", "DELETING"]);
const FAILED = new Set(["CREATE_FAILED", "UPDATE_FAILED", "ERROR"]);
const OWNER_TONE: Record<OnlineEvalConfigRow["owner"], ChipTone> = {
  agent: "amber",
  experiment: "muted",
  external: "warn",
};

interface EvaluatorInfo {
  id: string;
  name?: string;
  level?: string;
  source: string;
  requires_ground_truth?: boolean;
  provider?: string | null;
}

// Editor row for one filter. The AWS value is a one-branch union, so the
// editor keeps a type toggle + one text field and encodes on submit.
interface FilterDraft {
  key: string;
  operator: OnlineEvalFilterOperator;
  kind: "string" | "number" | "boolean";
  value: string;
}

interface Draft {
  mode: OnlineEvalMode;
  description: string;
  evaluators: string[];
  insights: string[];
  frequencies: OnlineEvalFrequency[];
  sampling: string;
  /** the operator typed a sampling value — a mode switch must not overwrite it */
  samplingTouched: boolean;
  timeout: string;
  filters: FilterDraft[];
}

// Older backends carry no `mode`; derive it the way the backend does.
const modeOf = (d: OnlineEvalConfigRow): OnlineEvalMode =>
  d.mode ?? (d.insights.length ? "insights" : "scores");

const emptyFilter = (): FilterDraft => ({
  key: "session.id",
  operator: "Contains",
  kind: "string",
  value: "",
});

const draftFromDetail = (d: OnlineEvalConfigRow): Draft => ({
  mode: modeOf(d),
  description: d.description ?? "",
  evaluators: [...d.evaluators],
  insights: [...d.insights],
  frequencies: d.clustering_frequencies.filter((f): f is OnlineEvalFrequency =>
    (FREQUENCIES as string[]).includes(f),
  ),
  sampling:
    d.sampling_percentage != null ? String(d.sampling_percentage) : "10",
  samplingTouched: true,
  timeout:
    d.session_timeout_minutes != null
      ? String(d.session_timeout_minutes)
      : "15",
  filters: d.filters.map((f) => {
    const v = f.value ?? {};
    if (v.doubleValue != null)
      return {
        key: f.key,
        operator: f.operator,
        kind: "number",
        value: String(v.doubleValue),
      };
    if (v.booleanValue != null)
      return {
        key: f.key,
        operator: f.operator,
        kind: "boolean",
        value: String(v.booleanValue),
      };
    return {
      key: f.key,
      operator: f.operator,
      kind: "string",
      value: v.stringValue ?? "",
    };
  }),
});

const newDraft = (): Draft => ({
  mode: "scores",
  description: "",
  evaluators: [...ONLINE_EVAL_DEFAULT],
  insights: [...INSIGHT_TYPES],
  frequencies: ["DAILY"],
  sampling: SAMPLING_DEFAULT.scores,
  samplingTouched: false,
  timeout: "15",
  filters: [],
});

const encodeFilter = (f: FilterDraft): OnlineEvalFilter => ({
  key: f.key.trim(),
  operator: f.operator,
  value:
    f.kind === "number"
      ? { doubleValue: Number(f.value) }
      : f.kind === "boolean"
        ? { booleanValue: f.value === "true" }
        : { stringValue: f.value },
});

const fmtTime = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
};

const shortId = (id: string | null | undefined): string =>
  id ? (id.length > 14 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id) : "—";

const statusTone = (status: string | null | undefined): ChipTone =>
  status === "ACTIVE" ? "good" : FAILED.has(status ?? "") ? "crit" : "warn";

const reportTone = (status: string | null | undefined): ChipTone =>
  status === "COMPLETED"
    ? "good"
    : status === "COMPLETED_WITH_ERRORS"
      ? "warn"
      : status === "FAILED" || status === "STOPPED"
        ? "crit"
        : "aqua";

// A console run that has no batch yet is keyed by its run id.
const reportKey = (r: OnlineEvalReportRow): string =>
  r.batch_id ?? r.run_id ?? "";

// Console runs settle through the run status (the batch may lag a poll behind);
// AWS-scheduled reports only have the batch status.
const reportActive = (r: OnlineEvalReportRow): boolean =>
  r.run_status
    ? ACTIVE_RUN_STATUSES.has(r.run_status)
    : !REPORT_TERMINAL.has(r.status ?? "");

const insightLabel = (t: (k: string) => string, id: string): string =>
  INSIGHT_LABEL_KEYS[id]
    ? t(`evalPage.newRun.insightType.${INSIGHT_LABEL_KEYS[id]}`)
    : id;

// Polarity-aware colour: a penalty evaluator (Refusal, Harmfulness, …) is good
// when LOW, so its thresholds mirror.
const meanColor = (mean: number | null, evaluatorId: string): string => {
  if (mean == null) return "var(--ink-3)";
  const oriented = evaluatorPolarity(evaluatorId) < 0 ? 1 - mean : mean;
  return oriented >= 0.7
    ? "var(--good)"
    : oriented >= 0.4
      ? "var(--warn)"
      : "var(--crit-text)";
};

interface ErrorEnvelope {
  code?: string;
  message?: string;
}

// Tiny SVG trend — no chart lib. Points with a null mean are skipped.
function Sparkline({
  points,
  evaluatorId,
}: {
  points: { mean: number | null }[];
  evaluatorId: string;
}) {
  const vals = points.map((p) => p.mean).filter((v): v is number => v != null);
  if (vals.length < 2) return null;
  const w = 72;
  const h = 18;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const pts = vals
    .map(
      (v, i) =>
        `${((i / (vals.length - 1)) * w).toFixed(1)},${(
          h -
          1 -
          ((v - min) / span) * (h - 2)
        ).toFixed(1)}`,
    )
    .join(" ");
  return (
    <svg
      width={w}
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      style={{ marginLeft: "auto" }}
    >
      <polyline
        points={pts}
        fill="none"
        stroke={meanColor(vals[vals.length - 1], evaluatorId)}
        strokeWidth="1.5"
      />
    </svg>
  );
}

export function OnlineView({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [rows, setRows] = useState<OnlineEvalConfigRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [catalog, setCatalog] = useState<EvaluatorInfo[]>([]);
  const [detail, setDetail] = useState<OnlineEvalConfigRow | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(newDraft());
  const [agentId, setAgentId] = useState("");
  const [enableOnCreate, setEnableOnCreate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] =
    useState<OnlineEvalConfigRow | null>(null);
  const [range, setRange] = useState<RangeKey>("24h");
  const [results, setResults] = useState<OnlineEvalResults | null>(null);
  const [resultsLoading, setResultsLoading] = useState(false);
  const [resultsError, setResultsError] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  // Insights mode: reports instead of results.
  const [reports, setReports] = useState<OnlineEvalReports | null>(null);
  const [reportsLoading, setReportsLoading] = useState(false);
  const [reportsError, setReportsError] = useState(false);
  const [reportRange, setReportRange] = useState<RangeKey>("24h");
  const [reportBusy, setReportBusy] = useState(false);
  const [expandedReport, setExpandedReport] = useState<string | null>(null);
  const [reportDetails, setReportDetails] = useState<
    Record<
      string,
      { detail?: OnlineEvalReportDetail; error?: string; loading?: boolean }
    >
  >({});
  const [showUnattributed, setShowUnattributed] = useState(false);

  const errorText = useCallback(
    async (res: Response): Promise<string> => {
      const env = (await res.json().catch(() => ({}))) as ErrorEnvelope;
      if (env.code && env.message)
        return t(`apiErrors.${env.code}`, { defaultValue: env.message });
      return env.message ?? `HTTP ${res.status}`;
    },
    [t],
  );

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/eval/online");
      if (!res.ok) throw new Error(`http ${res.status}`);
      setRows(
        ((await res.json()) as { configs: OnlineEvalConfigRow[] }).configs,
      );
      setLoadError(false);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    api
      .listAgents()
      .then((b) => setAgents(b.agents))
      .catch(() => {});
    fetch("/api/eval/evaluators")
      .then((res) => (res.ok ? res.json() : { evaluators: [] }))
      .then((body: { evaluators: EvaluatorInfo[] }) =>
        setCatalog(body.evaluators),
      )
      .catch(() => {});
  }, [load]);

  // Create/Update/Delete transit for a few seconds — poll the list only while
  // some row is still moving, like the runs dashboard does.
  const transient = rows.some((r) => TRANSIENT.has(r.status ?? ""));
  useEffect(() => {
    if (!transient) return;
    const timer = setInterval(() => void load(), 8000);
    return () => clearInterval(timer);
  }, [transient, load]);

  // "?oe=<configId>" selects a row (linkable, back-button friendly);
  // "?oe=new" opens the create form even while configs exist.
  const [searchParams, setSearchParams] = useSearchParams();
  const oeParam = searchParams.get("oe");
  const creatingNew = oeParam === "new";
  const selected = creatingNew
    ? null
    : (rows.find((r) => r.config_id === oeParam) ?? rows[0] ?? null);
  const selectOe = (id: string | null) => {
    setSearchParams(id ? { view: "online", oe: id } : { view: "online" });
  };
  const backToList = () => {
    setSearchParams({ view: "online" }, { replace: true });
  };
  // A stale "?oe=" (list loaded, id absent) says so at the top of the page
  // and drops the param; the rows[0] fallback then reads as a plain visit.
  const staleOe = useStaleParam(
    oeParam,
    !loading && !loadError && !creatingNew && selected?.config_id !== oeParam,
    backToList,
  );
  const { rows: pageRows, pagerProps } = useTablePage(
    rows,
    rows.findIndex((r) => r.config_id === selected?.config_id),
  );

  const eligibleAgents = agents.filter(
    (a) => a.status === "active" && ELIGIBLE_METHODS.has(a.method),
  );
  const showCreate = creatingNew || (!loading && !loadError && !selected);

  // Detail + draft hydrate declaratively from the selected row. Keyed on the
  // stable selection key (never row identity): list polls replace the rows
  // array, and re-running here would wipe unsaved edits mid-typing.
  const selKey = creatingNew ? "new" : (selected?.config_id ?? null);
  const fetchDetail = useCallback(
    async (id: string, rehydrate: boolean, isCancelled: () => boolean) => {
      try {
        const res = await fetch(`/api/eval/online/${id}`);
        if (!res.ok) throw new Error(await errorText(res));
        const d = (await res.json()) as OnlineEvalConfigRow;
        if (isCancelled()) return;
        setDetail(d);
        if (rehydrate) setDraft(draftFromDetail(d));
      } catch (err) {
        if (!isCancelled())
          setDetailError(err instanceof Error ? err.message : String(err));
      }
    },
    [errorText],
  );
  useEffect(() => {
    setFormError(null);
    setDetail(null);
    setDetailError(null);
    setExpanded(new Set());
    setReports(null);
    setReportsError(false);
    setExpandedReport(null);
    setReportDetails({});
    setShowUnattributed(false);
    if (selKey === "new" || selKey === null) {
      setDraft(newDraft());
      setEnableOnCreate(true);
      return;
    }
    let cancelled = false;
    void fetchDetail(selKey, true, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [selKey, fetchDetail]);

  // A polled list row that has settled (CREATING → ACTIVE, …) refreshes the
  // detail's status fields without touching the draft.
  const listStatus = selected?.status ?? null;
  const detailRef = useRef(detail);
  detailRef.current = detail;
  useEffect(() => {
    const cur = detailRef.current;
    if (!selKey || selKey === "new" || !listStatus || !cur) return;
    if (cur.status === listStatus || TRANSIENT.has(listStatus)) return;
    let cancelled = false;
    void fetchDetail(selKey, false, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [listStatus, selKey, fetchDetail]);

  // The detail decides which lower section a config gets: scores → RESULTS
  // (Logs Insights over the results log group), insights → REPORTS (batch
  // evaluations). An insights config never writes score records, so results are
  // not even requested for it.
  const detailMode: OnlineEvalMode | null = detail ? modeOf(detail) : null;

  // Results follow the selection and the range.
  useEffect(() => {
    setResults(null);
    setResultsError(false);
    if (!selKey || selKey === "new" || detailMode !== "scores") return;
    let cancelled = false;
    setResultsLoading(true);
    void (async () => {
      try {
        const res = await fetch(
          `/api/eval/online/${selKey}/results?range=${range}`,
        );
        if (!res.ok) throw new Error(`http ${res.status}`);
        const body = (await res.json()) as OnlineEvalResults;
        if (!cancelled) setResults(body);
      } catch {
        if (!cancelled) setResultsError(true);
      } finally {
        if (!cancelled) setResultsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selKey, range, detailMode]);

  // Reports follow the selection (insights mode only). A refresh drops the
  // cached detail of reports still running so an expanded row re-reads it.
  const loadReports = useCallback(
    async (id: string, isCancelled: () => boolean) => {
      try {
        const body = await api.onlineEvalReports(id);
        if (isCancelled()) return;
        setReports(body);
        setReportsError(false);
        const active = new Set(
          body.reports.filter(reportActive).map(reportKey),
        );
        if (active.size) {
          setReportDetails((prev) =>
            Object.fromEntries(
              Object.entries(prev).filter(([k]) => !active.has(k)),
            ),
          );
        }
      } catch {
        if (!isCancelled()) setReportsError(true);
      } finally {
        if (!isCancelled()) setReportsLoading(false);
      }
    },
    [],
  );
  useEffect(() => {
    if (!selKey || selKey === "new" || detailMode !== "insights") return;
    let cancelled = false;
    setReportsLoading(true);
    void loadReports(selKey, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [selKey, detailMode, loadReports]);

  // Poll while any report is still running (a console run just queued, an AWS
  // report IN_PROGRESS) — same cadence as the config list.
  const reportsActive = !!reports?.reports.some(reportActive);
  useEffect(() => {
    if (!reportsActive || !selKey || selKey === "new") return;
    let cancelled = false;
    const timer = setInterval(
      () => void loadReports(selKey, () => cancelled),
      REPORT_POLL_MS,
    );
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [reportsActive, selKey, loadReports]);

  // The expanded report's detail (trees + errors) loads once per batch; the
  // placeholder entry stops a poll-triggered re-run from fetching twice.
  const expandedRow =
    reports?.reports.find((r) => reportKey(r) === expandedReport) ??
    reports?.unattributed?.find((r) => reportKey(r) === expandedReport);
  const expandedBatchId = expandedRow?.batch_id ?? null;
  // The cache is read through a ref: if it were an effect dependency, writing the
  // `{loading: true}` placeholder would re-run the effect and its cleanup would
  // cancel the very fetch that just started (the row then shows LOADING forever).
  const reportDetailsRef = useRef(reportDetails);
  reportDetailsRef.current = reportDetails;
  useEffect(() => {
    if (!selKey || selKey === "new" || !expandedReport || !expandedBatchId)
      return;
    if (reportDetailsRef.current[expandedReport]) return;
    const key = expandedReport;
    let cancelled = false;
    setReportDetails((prev) => ({ ...prev, [key]: { loading: true } }));
    void (async () => {
      try {
        const d = await api.onlineEvalReport(selKey, expandedBatchId);
        if (!cancelled)
          setReportDetails((prev) => ({ ...prev, [key]: { detail: d } }));
      } catch (err) {
        if (!cancelled)
          setReportDetails((prev) => ({
            ...prev,
            [key]: { error: err instanceof Error ? err.message : String(err) },
          }));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selKey, expandedReport, expandedBatchId]);

  const editable = !!detail && detail.owner === "agent";
  const canToggle = !!detail && detail.owner !== "experiment";

  // ─── validation (mirrors the router's pydantic bounds) ──────────────────

  const validate = (): string | null => {
    const sampling = Number(draft.sampling);
    if (!Number.isFinite(sampling) || sampling < 0.01 || sampling > 100)
      return t("evalPage.online.err.sampling");
    const timeout = Number(draft.timeout);
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 1440)
      return t("evalPage.online.err.timeout");
    if (draft.mode === "insights") {
      if (draft.insights.length < 1 || draft.insights.length > INSIGHTS_MAX)
        return t("evalPage.online.err.insights", { max: INSIGHTS_MAX });
      if (draft.frequencies.length > FREQUENCIES.length)
        return t("evalPage.online.err.frequencies", {
          max: FREQUENCIES.length,
        });
    } else if (
      draft.evaluators.length < 1 ||
      draft.evaluators.length > ONLINE_EVAL_MAX
    ) {
      return t("evalPage.online.err.evaluators", { max: ONLINE_EVAL_MAX });
    }
    if (draft.filters.length > MAX_FILTERS)
      return t("evalPage.online.err.filterCount", { max: MAX_FILTERS });
    for (const f of draft.filters) {
      if (!FILTER_KEY_RE.test(f.key.trim()))
        return t("evalPage.online.err.filterKey");
      if (f.kind === "string" && !f.value)
        return t("evalPage.online.err.filterValue");
      if (f.kind === "number" && !Number.isFinite(Number(f.value)))
        return t("evalPage.online.err.filterValue");
    }
    if (draft.description.length > 200)
      return t("evalPage.online.err.description");
    return null;
  };

  // Only the changed fields travel — the backend merges the rule server-side
  // and re-sends it whole, so a sampling-only patch keeps filters + timeout.
  // Mode is immutable: an insights config only ever sends insights /
  // frequencies (complete lists), a scores config only evaluators.
  const patchBody = (): OnlineEvalConfigPatch => {
    if (!detail) return {};
    const body: OnlineEvalConfigPatch = {};
    if (draft.description !== (detail.description ?? ""))
      body.description = draft.description;
    if (modeOf(detail) === "insights") {
      if (JSON.stringify(draft.insights) !== JSON.stringify(detail.insights))
        body.insights = draft.insights;
      if (
        JSON.stringify(draft.frequencies) !==
        JSON.stringify(detail.clustering_frequencies)
      )
        body.clustering_frequencies = draft.frequencies;
    } else if (
      JSON.stringify(draft.evaluators) !== JSON.stringify(detail.evaluators)
    ) {
      body.evaluators = draft.evaluators;
    }
    if (Number(draft.sampling) !== detail.sampling_percentage)
      body.sampling_percentage = Number(draft.sampling);
    if (Number(draft.timeout) !== detail.session_timeout_minutes)
      body.session_timeout_minutes = Number(draft.timeout);
    const encoded = draft.filters.map(encodeFilter);
    if (JSON.stringify(encoded) !== JSON.stringify(detail.filters))
      body.filters = encoded;
    return body;
  };
  const dirty = editable && Object.keys(patchBody()).length > 0;

  // ─── mutations ──────────────────────────────────────────────────────────

  const create = async () => {
    setFormError(null);
    if (!agentId) {
      setFormError(t("evalPage.online.err.agent"));
      return;
    }
    const invalid = validate();
    if (invalid) {
      setFormError(invalid);
      return;
    }
    setBusy(true);
    try {
      const kind: Partial<OnlineEvalConfigCreate> =
        draft.mode === "insights"
          ? {
              insights: draft.insights,
              clustering_frequencies: draft.frequencies,
            }
          : { evaluators: draft.evaluators };
      const payload: OnlineEvalConfigCreate = {
        agent_id: agentId,
        mode: draft.mode,
        ...kind,
        sampling_percentage: Number(draft.sampling),
        session_timeout_minutes: Number(draft.timeout),
        filters: draft.filters.map(encodeFilter),
        description: draft.description.trim() || null,
        enable_on_create: enableOnCreate,
      };
      const res = await fetch("/api/eval/online", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        setFormError(await errorText(res));
        return;
      }
      const created = (await res.json()) as OnlineEvalConfigRow;
      toast(t("evalPage.online.created"), "good");
      await load();
      selectOe(created.config_id);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!detail) return;
    setFormError(null);
    const invalid = validate();
    if (invalid) {
      setFormError(invalid);
      return;
    }
    const body = patchBody();
    if (!Object.keys(body).length) return;
    setBusy(true);
    try {
      const res = await fetch(`/api/eval/online/${detail.config_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        setFormError(await errorText(res));
        return;
      }
      const d = (await res.json()) as OnlineEvalConfigRow;
      setDetail(d);
      setDraft(draftFromDetail(d));
      toast(t("evalPage.online.saved"), "good");
      await load();
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (action: "pause" | "resume") => {
    if (!detail) return;
    setBusy(true);
    try {
      const res = await fetch(
        `/api/eval/online/${detail.config_id}/${action}`,
        {
          method: "POST",
        },
      );
      if (!res.ok) {
        toast(t("common.actionFailed", { msg: await errorText(res) }));
        return;
      }
      // Status fields only — an in-progress edit must survive a pause click.
      setDetail((await res.json()) as OnlineEvalConfigRow);
      toast(
        t(
          action === "pause"
            ? "evalPage.online.paused"
            : "evalPage.online.resumed",
        ),
      );
      await load();
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async (row: OnlineEvalConfigRow) => {
    const res = await fetch(`/api/eval/online/${row.config_id}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      toast(t("common.actionFailed", { msg: await errorText(res) }));
      return;
    }
    toast(t("evalPage.online.deleted", { logGroup: row.results_log_group }));
    if (oeParam === row.config_id) selectOe(null);
    await load();
  };

  // On-demand insights report over the sessions this config sampled in the
  // range. Queued like any run; the row shows up as `console · QUEUED` and the
  // poll carries it to a terminal status.
  const runReport = async () => {
    if (!detail) return;
    setReportBusy(true);
    try {
      await api.onlineEvalRunReport(detail.config_id, reportRange);
      toast(t("evalPage.online.reports.started"), "good");
      await loadReports(detail.config_id, () => false);
    } catch (err) {
      const msg =
        err instanceof ApiError || err instanceof Error
          ? err.message
          : String(err);
      toast(t("common.actionFailed", { msg }));
    } finally {
      setReportBusy(false);
    }
  };

  // ─── shared renderers ──────────────────────────────────────────────────

  const setFilter = (index: number, patch: Partial<FilterDraft>) => {
    setDraft({
      ...draft,
      filters: draft.filters.map((f, i) =>
        i === index ? { ...f, ...patch } : f,
      ),
    });
  };

  // One chip renderer for both evaluator groups (main + third-party): the
  // ONLINE_EVAL_MAX gate applies across the whole selection. Ground-truth
  // matchers stay visible but disabled — live traces carry no ground truth.
  const evalChip = (e: EvaluatorInfo) => {
    const on = draft.evaluators.includes(e.id);
    const gt =
      !!e.requires_ground_truth || e.id.startsWith("Builtin.Trajectory");
    const full = !on && draft.evaluators.length >= ONLINE_EVAL_MAX;
    const off = gt || full;
    return (
      <button
        key={e.id}
        type="button"
        className={`selchip${on ? " on" : ""}`}
        data-testid={`online-eval-${e.id}`}
        disabled={off}
        style={{
          opacity: off ? 0.4 : undefined,
          cursor: off ? "not-allowed" : "pointer",
        }}
        title={
          gt
            ? t("evalPage.newRun.trajectoryNeedsGt")
            : e.source === "custom"
              ? t("expPage.customEvaluator")
              : e.id
        }
        onClick={() =>
          setDraft({
            ...draft,
            evaluators: on
              ? draft.evaluators.filter((x) => x !== e.id)
              : [...draft.evaluators, e.id],
          })
        }
      >
        {e.source === "custom" ? (e.name ?? e.id) : evaluatorLabel(t, e.id)}
        {gt && (
          <span className="mono" style={{ fontSize: 8.5, marginLeft: 6 }}>
            ◆ GT
          </span>
        )}
        {e.source === "custom" && !gt && (
          <span className="mono" style={{ fontSize: 8.5, marginLeft: 6 }}>
            ◆
          </span>
        )}
        {e.source === "third_party" && e.provider && (
          <span
            className="mono"
            style={{
              fontSize: 8.5,
              marginLeft: 6,
              letterSpacing: ".08em",
              opacity: 0.7,
            }}
          >
            {e.provider}
          </span>
        )}
      </button>
    );
  };

  const evaluatorPicker = (
    <div className="field">
      <label>{t("evalPage.online.form.evaluators")}</label>
      <div className="note" style={{ marginBottom: 6 }}>
        <span className="i">[i]</span>
        <span>
          {t("evalPage.online.form.evaluatorsHint", { max: ONLINE_EVAL_MAX })}
        </span>
      </div>
      <div style={{ maxHeight: 160, overflowY: "auto" }}>
        <div className="selchips">
          {catalog.filter((e) => e.source !== "third_party").map(evalChip)}
        </div>
        {catalog.some((e) => e.source === "third_party") && (
          <>
            <div
              className="mono dim"
              style={{
                fontSize: 9.5,
                letterSpacing: ".08em",
                margin: "8px 0 4px",
              }}
            >
              {t("expPage.thirdPartyGroup")}
            </div>
            <div className="selchips">
              {catalog.filter((e) => e.source === "third_party").map(evalChip)}
            </div>
          </>
        )}
      </div>
      <div className="mono dim" style={{ fontSize: 9.5, marginTop: 6 }}>
        {t("evalPage.online.form.evaluatorsSelected", {
          count: draft.evaluators.length,
          max: ONLINE_EVAL_MAX,
        })}
      </div>
    </div>
  );

  // Create only — the mode is immutable once the config exists. Switching
  // flips the sampling default (10 % scores / 100 % insights) unless the
  // operator already typed a value.
  const switchMode = (mode: OnlineEvalMode) => {
    if (mode === draft.mode) return;
    setDraft({
      ...draft,
      mode,
      sampling: draft.samplingTouched ? draft.sampling : SAMPLING_DEFAULT[mode],
    });
  };
  const modeToggle = (
    <div className="field">
      <label>{t("evalPage.online.form.mode")}</label>
      <div className="selchips">
        {(["scores", "insights"] as const).map((mode) => (
          <button
            key={mode}
            type="button"
            className={`selchip${draft.mode === mode ? " on" : ""}`}
            data-testid={`online-mode-${mode}`}
            onClick={() => switchMode(mode)}
          >
            {t(`evalPage.online.mode.${mode}`)}
          </button>
        ))}
      </div>
      <div className="mono dim" style={{ fontSize: 9.5, marginTop: 6 }}>
        {t(`evalPage.online.form.modeHint.${draft.mode}`)}
      </div>
    </div>
  );

  // Insights mode replaces the evaluator picker: 1..3 insight types + the
  // report cadences AWS schedules (none → on-demand reports only).
  const insightsPicker = (
    <>
      <div className="field">
        <label>
          {t("evalPage.online.form.insights", { max: INSIGHTS_MAX })}
        </label>
        <div className="note" style={{ marginBottom: 6 }}>
          <span className="i">[i]</span>
          <span>{t("evalPage.online.form.insightsHint")}</span>
        </div>
        <div className="selchips">
          {INSIGHT_TYPES.map((id) => {
            const on = draft.insights.includes(id);
            return (
              <button
                key={id}
                type="button"
                className={`selchip${on ? " on" : ""}`}
                data-testid={`online-insight-${id}`}
                title={id}
                onClick={() =>
                  setDraft({
                    ...draft,
                    insights: on
                      ? draft.insights.filter((x) => x !== id)
                      : INSIGHT_TYPES.filter(
                          (x) => x === id || draft.insights.includes(x),
                        ),
                  })
                }
              >
                {insightLabel(t, id)}
              </button>
            );
          })}
        </div>
      </div>
      <div className="field">
        <label>{t("evalPage.online.form.frequencies")}</label>
        <div className="selchips">
          {FREQUENCIES.map((f) => {
            const on = draft.frequencies.includes(f);
            return (
              <button
                key={f}
                type="button"
                className={`selchip${on ? " on" : ""}`}
                data-testid={`online-freq-${f}`}
                onClick={() =>
                  setDraft({
                    ...draft,
                    frequencies: on
                      ? draft.frequencies.filter((x) => x !== f)
                      : FREQUENCIES.filter(
                          (x) => x === f || draft.frequencies.includes(x),
                        ),
                  })
                }
              >
                {t(`evalPage.online.freq.${f}`)}
              </button>
            );
          })}
        </div>
        <div className="mono dim" style={{ fontSize: 9.5, marginTop: 6 }}>
          {t("evalPage.online.form.frequenciesHint")}
        </div>
      </div>
    </>
  );

  const ruleFields = (
    <>
      <div style={{ display: "flex", gap: 12 }}>
        <div className="field" style={{ flex: 1 }}>
          <label>{t("evalPage.online.form.sampling")}</label>
          <input
            className="input mono"
            data-testid="online-sampling"
            aria-label={t("evalPage.online.form.sampling")}
            type="number"
            min={0.01}
            max={100}
            step="any"
            value={draft.sampling}
            onChange={(e) =>
              setDraft({
                ...draft,
                sampling: e.target.value,
                samplingTouched: true,
              })
            }
          />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label>{t("evalPage.online.form.timeout")}</label>
          <input
            className="input mono"
            data-testid="online-timeout"
            aria-label={t("evalPage.online.form.timeout")}
            type="number"
            min={1}
            max={1440}
            step={1}
            value={draft.timeout}
            onChange={(e) => setDraft({ ...draft, timeout: e.target.value })}
          />
        </div>
      </div>
      <div className="field">
        <label>{t("evalPage.online.form.filters", { max: MAX_FILTERS })}</label>
        {draft.filters.map((f, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              gap: 6,
              marginBottom: 6,
              flexWrap: "wrap",
            }}
          >
            <input
              className="input mono"
              data-testid={`online-filter-key-${i}`}
              value={f.key}
              placeholder="session.id"
              style={{ flex: "1 1 140px" }}
              onChange={(e) => setFilter(i, { key: e.target.value })}
            />
            <select
              className="input mono"
              value={f.operator}
              style={{ flex: "0 1 150px" }}
              onChange={(e) =>
                setFilter(i, {
                  operator: e.target.value as OnlineEvalFilterOperator,
                })
              }
            >
              {OPERATORS.map((op) => (
                <option key={op} value={op} style={{ background: "#141816" }}>
                  {op}
                </option>
              ))}
            </select>
            <select
              className="input mono"
              value={f.kind}
              style={{ flex: "0 1 100px" }}
              onChange={(e) => {
                const kind = e.target.value as FilterDraft["kind"];
                setFilter(i, { kind, value: kind === "boolean" ? "true" : "" });
              }}
            >
              {(["string", "number", "boolean"] as const).map((k) => (
                <option key={k} value={k} style={{ background: "#141816" }}>
                  {t(`evalPage.online.form.kind.${k}`)}
                </option>
              ))}
            </select>
            {f.kind === "boolean" ? (
              <select
                className="input mono"
                value={f.value}
                style={{ flex: "1 1 120px" }}
                onChange={(e) => setFilter(i, { value: e.target.value })}
              >
                <option value="true" style={{ background: "#141816" }}>
                  true
                </option>
                <option value="false" style={{ background: "#141816" }}>
                  false
                </option>
              </select>
            ) : (
              <input
                className="input mono"
                data-testid={`online-filter-value-${i}`}
                type={f.kind === "number" ? "number" : "text"}
                step="any"
                value={f.value}
                placeholder={t("evalPage.online.form.filterValue")}
                style={{ flex: "1 1 120px" }}
                onChange={(e) => setFilter(i, { value: e.target.value })}
              />
            )}
            <Btn
              title={t("evalPage.online.form.removeFilter")}
              onClick={() =>
                setDraft({
                  ...draft,
                  filters: draft.filters.filter((_, idx) => idx !== i),
                })
              }
            >
              ✕
            </Btn>
          </div>
        ))}
        <Btn
          data-testid="online-filter-add"
          disabled={draft.filters.length >= MAX_FILTERS}
          onClick={() =>
            setDraft({ ...draft, filters: [...draft.filters, emptyFilter()] })
          }
        >
          + {t("evalPage.online.form.addFilter")}
        </Btn>
        <div className="mono dim" style={{ fontSize: 9.5, marginTop: 6 }}>
          {t("evalPage.online.form.filtersHint")}
        </div>
      </div>
      <div className="field">
        <label>{t("evalPage.online.form.description")}</label>
        <input
          className="input"
          maxLength={200}
          value={draft.description}
          placeholder={t("evalPage.online.form.descriptionPlaceholder")}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })}
        />
      </div>
    </>
  );

  const errorNote = formError && (
    <div
      className="note"
      style={{ borderColor: "var(--crit)", marginBottom: 10 }}
    >
      <span className="i" style={{ color: "var(--crit)" }}>
        [✕]
      </span>
      <span>{formError}</span>
    </div>
  );

  const createForm = (
    <>
      <div className="field">
        <label>{t("evalPage.online.form.agent")}</label>
        <select
          className="input"
          data-testid="online-agent"
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
        >
          <option value="" style={{ background: "#141816" }}>
            {t("evalPage.online.form.agentPick")}
          </option>
          {eligibleAgents.map((a) => (
            <option key={a.id} value={a.id} style={{ background: "#141816" }}>
              {a.name} · {a.method}
            </option>
          ))}
        </select>
        <div className="mono dim" style={{ fontSize: 9.5, marginTop: 6 }}>
          {t("evalPage.online.form.agentHint")}
        </div>
      </div>
      {modeToggle}
      {draft.mode === "insights" ? insightsPicker : evaluatorPicker}
      {ruleFields}
      <div className="field">
        <label className="gov-demo-switch" style={{ letterSpacing: 0 }}>
          <input
            type="checkbox"
            data-testid="online-enable"
            checked={enableOnCreate}
            onChange={(e) => setEnableOnCreate(e.target.checked)}
          />
          <span className="gov-demo-switch-track" aria-hidden="true">
            <span />
          </span>
          <span>{t("evalPage.online.form.enableOnCreate")}</span>
        </label>
      </div>
      {errorNote}
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Btn
          primary
          data-testid="online-create"
          disabled={
            busy ||
            !agentId ||
            (draft.mode === "insights"
              ? draft.insights.length === 0
              : draft.evaluators.length === 0)
          }
          disabledReason={
            !agentId
              ? t("evalPage.online.form.disabledAgent")
              : draft.mode === "insights" && draft.insights.length === 0
                ? t("evalPage.online.form.disabledInsights")
                : draft.mode !== "insights" && draft.evaluators.length === 0
                  ? t("evalPage.online.form.disabledEvaluators")
                  : undefined
          }
          onClick={() => void create()}
        >
          ▸ {t("evalPage.online.form.create")}
        </Btn>
      </div>
    </>
  );

  const kv = (k: string, v: ReactNode) => (
    <div className="kv" key={k}>
      <span className="k">{k}</span>
      <span
        className="v"
        style={{ wordBreak: "break-all", textAlign: "right" }}
      >
        {v}
      </span>
    </div>
  );

  const detailBody = detail && (
    <>
      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
          marginBottom: 10,
        }}
      >
        <Chip tone={OWNER_TONE[detail.owner]}>
          {t(`evalPage.online.owner.${detail.owner}`)}
        </Chip>
        <span
          data-testid="online-detail-mode"
          style={{ display: "inline-flex" }}
        >
          <Chip tone={MODE_TONE[modeOf(detail)]}>
            {t(`evalPage.online.mode.${modeOf(detail)}`)}
          </Chip>
        </span>
        {detail.status && (
          <Chip tone={statusTone(detail.status)}>{detail.status}</Chip>
        )}
        {detail.execution_status && (
          <Chip tone={detail.execution_status === "ENABLED" ? "aqua" : "muted"}>
            {detail.execution_status}
          </Chip>
        )}
        {detail.name && (
          <span className="mono dim" style={{ fontSize: 10 }}>
            {detail.name}
          </span>
        )}
      </div>
      {detail.failure_reason && (
        <div
          className="note"
          style={{ borderColor: "var(--crit)", marginBottom: 10 }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>{detail.failure_reason}</span>
        </div>
      )}
      {detail.duplicate_enabled && (
        <div
          className="note"
          style={{ marginBottom: 10 }}
          data-testid="online-duplicate-warn"
        >
          <span className="i">[!]</span>
          <span>{t("evalPage.online.duplicateWarn")}</span>
        </div>
      )}
      {detail.owner === "experiment" && (
        <div className="note" style={{ marginBottom: 10 }}>
          <span className="i">[i]</span>
          <span>
            {t("evalPage.online.experimentReadonly")}{" "}
            <Link to="/evaluation?view=experiment">
              {t("evalPage.nav.experiments")}
            </Link>
          </span>
        </div>
      )}
      {detail.owner === "external" && (
        <div className="note" style={{ marginBottom: 10 }}>
          <span className="i">[i]</span>
          <span>
            {detail.matched_agent
              ? t("evalPage.online.externalMatched", {
                  name: detail.matched_agent.name,
                })
              : t("evalPage.online.externalReadonly")}
          </span>
        </div>
      )}
      {kv(
        t("evalPage.online.kv.agent"),
        detail.agent_name ??
          (detail.matched_agent ? `≈ ${detail.matched_agent.name}` : "—"),
      )}
      {kv(
        t("evalPage.online.kv.serviceName"),
        detail.data_source.service_name ?? "—",
      )}
      {kv(
        t("evalPage.online.kv.logGroups"),
        detail.data_source.log_groups.length
          ? detail.data_source.log_groups.join(", ")
          : "—",
      )}
      {kv(t("evalPage.online.kv.resultsLogGroup"), detail.results_log_group)}
      {kv(t("evalPage.online.kv.created"), fmtTime(detail.created_at))}
      {kv(t("evalPage.online.kv.updated"), fmtTime(detail.updated_at))}
      {!editable && (
        <>
          {modeOf(detail) === "insights" ? (
            <>
              {kv(
                t("evalPage.online.kv.insights"),
                detail.insights.length
                  ? detail.insights.map((id) => insightLabel(t, id)).join(", ")
                  : "—",
              )}
              {kv(
                t("evalPage.online.kv.frequencies"),
                detail.clustering_frequencies.length
                  ? detail.clustering_frequencies
                      .map((f) =>
                        t(`evalPage.online.freq.${f}`, { defaultValue: f }),
                      )
                      .join(", ")
                  : t("evalPage.online.kv.noFrequencies"),
              )}
            </>
          ) : (
            kv(
              t("evalPage.online.kv.evaluators"),
              detail.evaluators.length
                ? detail.evaluators
                    .map((id) => evaluatorLabel(t, id))
                    .join(", ")
                : "—",
            )
          )}
          {kv(
            t("evalPage.online.kv.sampling"),
            detail.sampling_percentage != null
              ? `${detail.sampling_percentage}%`
              : "—",
          )}
          {kv(
            t("evalPage.online.kv.timeout"),
            detail.session_timeout_minutes != null
              ? t("evalPage.online.minutes", {
                  count: detail.session_timeout_minutes,
                })
              : "—",
          )}
          {detail.filters.length > 0 &&
            kv(
              t("evalPage.online.kv.filters"),
              detail.filters
                .map(
                  (f) =>
                    `${f.key} ${f.operator} ${JSON.stringify(Object.values(f.value)[0])}`,
                )
                .join(" · "),
            )}
        </>
      )}
      <div className="note" style={{ margin: "10px 0" }}>
        <span className="i">[i]</span>
        <span>
          {t(
            modeOf(detail) === "insights"
              ? "evalPage.online.reportsHint"
              : "evalPage.online.firstResultsHint",
            { count: detail.session_timeout_minutes ?? 15 },
          )}
        </span>
      </div>
      {editable && (
        <>
          <div
            className="mono dim"
            style={{
              fontSize: 9.5,
              letterSpacing: ".18em",
              margin: "14px 0 8px",
            }}
          >
            {t(
              modeOf(detail) === "insights"
                ? "evalPage.online.editTitleInsights"
                : "evalPage.online.editTitle",
            )}
          </div>
          {modeOf(detail) === "insights" ? insightsPicker : evaluatorPicker}
          {ruleFields}
          {errorNote}
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <Btn
              primary
              data-testid="online-save"
              disabled={busy || !dirty || TRANSIENT.has(detail.status ?? "")}
              onClick={() => void save()}
            >
              ▸ {t("evalPage.online.save")}
            </Btn>
          </div>
        </>
      )}
    </>
  );

  // ─── results ────────────────────────────────────────────────────────────

  const resultsBody = detail && (
    <div style={{ marginTop: 18 }} data-testid="online-results">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: 10,
        }}
      >
        <span
          className="mono dim"
          style={{ fontSize: 9.5, letterSpacing: ".18em" }}
        >
          {t("evalPage.online.results.title")}
        </span>
        <span style={{ flex: 1 }} />
        <div
          className="range"
          role="group"
          aria-label={t("evalPage.online.results.range")}
        >
          {RANGES.map((key) => (
            <button
              key={key}
              type="button"
              data-testid={`online-range-${key}`}
              className={range === key ? "on" : ""}
              onClick={() => setRange(key)}
            >
              {key.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
      {resultsLoading && !results && (
        <div className="empty">{t("common.loading")}</div>
      )}
      {resultsError && (
        <div className="note" style={{ borderColor: "var(--crit)" }}>
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>{t("evalPage.online.results.failed")}</span>
        </div>
      )}
      {results &&
        results.evaluators.length === 0 &&
        results.recent.length === 0 && (
          <div className="empty" data-testid="online-results-empty">
            {t("evalPage.online.results.empty", {
              count: detail.session_timeout_minutes ?? 15,
            })}
          </div>
        )}
      {results && results.evaluators.length > 0 && (
        <div
          className="tiles"
          style={{
            gridTemplateColumns: "repeat(auto-fill, minmax(170px, 1fr))",
          }}
        >
          {results.evaluators.map((ev) => {
            const labels = Object.entries(ev.labels);
            return (
              <StatTile
                key={`${ev.evaluator_id}:${ev.level ?? ""}`}
                label={
                  <span title={ev.evaluator_id}>
                    {evaluatorLabel(t, ev.evaluator_id)}
                    {ev.level && (
                      <span style={{ marginLeft: 6, opacity: 0.7 }}>
                        {ev.level}
                      </span>
                    )}
                    {evaluatorPolarity(ev.evaluator_id) < 0 && (
                      <span
                        style={{ marginLeft: 6, opacity: 0.7 }}
                        title={t("evalPage.online.results.lowerIsBetter")}
                      >
                        ↓
                      </span>
                    )}
                  </span>
                }
                value={
                  <span
                    data-testid={`online-tile-${ev.evaluator_id}`}
                    style={{ color: meanColor(ev.mean, ev.evaluator_id) }}
                  >
                    {ev.mean != null ? ev.mean.toFixed(2) : "—"}
                  </span>
                }
                foot={
                  <>
                    <span>
                      {t("evalPage.online.results.tileFoot", {
                        count: ev.count,
                        sessions: ev.sessions,
                      })}
                    </span>
                    <Sparkline
                      points={results.series[ev.evaluator_id] ?? []}
                      evaluatorId={ev.evaluator_id}
                    />
                    {labels.length > 0 && (
                      <span
                        style={{
                          display: "flex",
                          gap: 4,
                          flexWrap: "wrap",
                          flexBasis: "100%",
                        }}
                      >
                        {labels.map(([label, n]) => (
                          <Chip
                            key={label}
                            tone="muted"
                            style={{ fontSize: 8.5 }}
                          >
                            {label} {n}
                          </Chip>
                        ))}
                      </span>
                    )}
                  </>
                }
              />
            );
          })}
        </div>
      )}
      {results && results.errors.count > 0 && (
        <div
          className="note"
          style={{ borderColor: "var(--crit)", marginBottom: 10 }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>
            {t("evalPage.online.results.errors", {
              count: results.errors.count,
            })}
            {results.errors.first_message && (
              <span
                className="mono"
                style={{ display: "block", fontSize: 10, marginTop: 4 }}
              >
                {results.errors.first_message}
              </span>
            )}
          </span>
        </div>
      )}
      {results && results.recent.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ minWidth: 560 }}>
            <thead>
              <tr>
                <th>{t("evalPage.online.results.col.time")}</th>
                <th>{t("evalPage.online.results.col.session")}</th>
                <th>{t("evalPage.online.results.col.evaluator")}</th>
                <th>{t("evalPage.online.results.col.score")}</th>
                <th>{t("evalPage.online.results.col.label")}</th>
                <th>{t("evalPage.online.results.col.explanation")}</th>
              </tr>
            </thead>
            <tbody>
              {results.recent.map((r, i) => {
                const open = expanded.has(i);
                const text = r.error ?? r.explanation;
                return (
                  <tr
                    key={i}
                    data-testid={`online-recent-${i}`}
                    onClick={() =>
                      setExpanded((prev) => {
                        const next = new Set(prev);
                        if (next.has(i)) next.delete(i);
                        else next.add(i);
                        return next;
                      })
                    }
                    style={{
                      cursor: text ? "pointer" : undefined,
                      verticalAlign: "top",
                    }}
                  >
                    <td className="mono dim" style={{ whiteSpace: "nowrap" }}>
                      {fmtTime(r.time)}
                    </td>
                    <td className="mono" title={r.session_id ?? undefined}>
                      {shortId(r.session_id)}
                    </td>
                    <td title={r.evaluator_id ?? undefined}>
                      {r.evaluator_id ? evaluatorLabel(t, r.evaluator_id) : "—"}
                      {r.level && (
                        <span
                          className="mono dim"
                          style={{ fontSize: 8.5, marginLeft: 6 }}
                        >
                          {r.level}
                        </span>
                      )}
                    </td>
                    <td
                      className="mono"
                      style={{
                        color: r.error
                          ? "var(--crit-text)"
                          : r.score != null && r.evaluator_id
                            ? meanColor(r.score, r.evaluator_id)
                            : undefined,
                      }}
                    >
                      {r.error
                        ? "✕"
                        : r.score != null
                          ? r.score.toFixed(2)
                          : "—"}
                    </td>
                    <td className="mono dim">{r.label ?? "—"}</td>
                    <td
                      style={{
                        fontSize: 11,
                        maxWidth: open ? undefined : 260,
                        whiteSpace: open ? "pre-wrap" : "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        color: r.error ? "var(--crit-text)" : undefined,
                      }}
                    >
                      {text ? `${open ? "▾" : "▸"} ${text}` : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );

  // ─── reports (insights mode) ────────────────────────────────────────────

  const consoleReportRunning = !!reports?.reports.some(
    (r) => r.origin === "console" && reportActive(r),
  );
  const canRunReport = editable && detailMode === "insights";

  const reportRow = (r: OnlineEvalReportRow, unattributed = false) => {
    const key = reportKey(r);
    const open = expandedReport === key;
    const cached = reportDetails[key];
    const rd = cached?.detail;
    const sessions = r.sessions;
    const sessionsText =
      sessions.total == null
        ? "—"
        : `${sessions.completed}${sessions.failed ? ` / ✕ ${sessions.failed}` : ""}${
            sessions.in_progress ? ` / … ${sessions.in_progress}` : ""
          }`;
    return (
      <Fragment key={key}>
        <tr
          data-testid={`online-report-${key}`}
          onClick={() => setExpandedReport(open ? null : key)}
          style={{
            cursor: "pointer",
            verticalAlign: "top",
            background: open ? "rgba(255,176,0,.045)" : undefined,
          }}
        >
          <td
            className="mono dim"
            style={{ whiteSpace: "nowrap" }}
            title={r.name ?? r.batch_id ?? undefined}
          >
            {open ? "▾" : "▸"} {fmtTime(r.created_at)}
          </td>
          <td>
            <Chip
              tone={
                unattributed
                  ? "muted"
                  : r.origin === "console"
                    ? "amber"
                    : "blue"
              }
            >
              {t(
                `evalPage.online.reports.origin.${unattributed ? "unknown" : r.origin}`,
              )}
            </Chip>
          </td>
          <td>
            <Chip tone={reportTone(r.status)}>{r.status ?? "—"}</Chip>
          </td>
          <td className="mono dim" style={{ whiteSpace: "nowrap" }}>
            {sessionsText}
          </td>
          <td style={{ fontSize: 11 }}>
            {r.insights.length ? (
              r.insights.map((id) => insightLabel(t, id)).join(" · ")
            ) : (
              <span
                className="dim mono"
                title={t("evalPage.online.reports.insightsInherited")}
              >
                {t("evalPage.online.reports.insightsFromConfig")}
              </span>
            )}
          </td>
        </tr>
        {open && (
          <tr data-testid={`online-report-detail-${key}`}>
            <td colSpan={5} style={{ padding: "6px 12px 12px" }}>
              {r.error && (
                <div
                  className="note"
                  style={{ borderColor: "var(--crit)", marginBottom: 8 }}
                >
                  <span className="i" style={{ color: "var(--crit)" }}>
                    [✕]
                  </span>
                  <span className="mono" style={{ fontSize: 10 }}>
                    {r.error}
                  </span>
                </div>
              )}
              {!r.batch_id && (
                <div className="empty">
                  {t("evalPage.online.reports.noBatch")}
                </div>
              )}
              {r.batch_id && (cached?.loading || (!cached && !rd)) && (
                <div className="empty">{t("common.loading")}</div>
              )}
              {cached?.error && (
                <div className="note" style={{ borderColor: "var(--crit)" }}>
                  <span className="i" style={{ color: "var(--crit)" }}>
                    [✕]
                  </span>
                  <span>
                    {t("evalPage.online.reports.detailFailed")} — {cached.error}
                  </span>
                </div>
              )}
              {rd && (
                <>
                  {rd.error_details.length > 0 && (
                    <div
                      className="note"
                      style={{ borderColor: "var(--crit)", marginBottom: 8 }}
                    >
                      <span className="i" style={{ color: "var(--crit)" }}>
                        [✕]
                      </span>
                      <span>
                        {t("evalPage.online.reports.errors", {
                          count: rd.error_details.length,
                        })}
                        {rd.error_details.slice(0, 3).map((e, i) => (
                          <span
                            key={i}
                            className="mono"
                            style={{
                              display: "block",
                              fontSize: 10,
                              marginTop: 4,
                            }}
                          >
                            {e}
                          </span>
                        ))}
                      </span>
                    </div>
                  )}
                  {hasInsightTrees(rd.insights) ? (
                    <div style={{ maxHeight: 460, overflowY: "auto" }}>
                      <InsightClusters insights={rd.insights} />
                    </div>
                  ) : !REPORT_TERMINAL.has(rd.status ?? "") ? (
                    <div className="empty">
                      {t("evalPage.online.reports.running")}
                    </div>
                  ) : (rd.sessions.total ?? 0) === 0 ? (
                    <div className="empty" data-testid="online-report-empty">
                      {t("evalPage.online.reports.noSessions", {
                        count: detail?.session_timeout_minutes ?? 15,
                      })}
                    </div>
                  ) : (
                    <div className="empty">
                      {t("evalPage.online.reports.noTrees")}
                    </div>
                  )}
                </>
              )}
            </td>
          </tr>
        )}
      </Fragment>
    );
  };

  const reportsBody = detail && (
    <div style={{ marginTop: 18 }} data-testid="online-reports">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: 10,
          flexWrap: "wrap",
        }}
      >
        <span
          className="mono dim"
          style={{ fontSize: 9.5, letterSpacing: ".18em" }}
        >
          {t("evalPage.online.reports.title")}
        </span>
        <span style={{ flex: 1 }} />
        {canRunReport && (
          <>
            <div
              className="range"
              role="group"
              aria-label={t("evalPage.online.reports.range")}
            >
              {RANGES.map((key) => (
                <button
                  key={key}
                  type="button"
                  data-testid={`online-report-range-${key}`}
                  className={reportRange === key ? "on" : ""}
                  onClick={() => setReportRange(key)}
                >
                  {key.toUpperCase()}
                </button>
              ))}
            </div>
            <Btn
              primary
              data-testid="online-report-run"
              disabled={
                reportBusy ||
                consoleReportRunning ||
                TRANSIENT.has(detail.status ?? "")
              }
              title={
                consoleReportRunning
                  ? t("evalPage.online.reports.pendingHint")
                  : undefined
              }
              onClick={() => void runReport()}
            >
              ▸ {t("evalPage.online.reports.run")}
            </Btn>
          </>
        )}
      </div>
      {reportsLoading && !reports && (
        <div className="empty">{t("common.loading")}</div>
      )}
      {reportsError && (
        <div className="note" style={{ borderColor: "var(--crit)" }}>
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>{t("evalPage.online.reports.failed")}</span>
        </div>
      )}
      {reports?.aws_unavailable && (
        <div
          className="note"
          style={{ marginBottom: 8 }}
          data-testid="online-reports-aws-unavailable"
        >
          <span className="i">[!]</span>
          <span>{t("evalPage.online.reports.awsUnavailable")}</span>
        </div>
      )}
      {reports && reports.reports.length === 0 && (
        <div className="empty" data-testid="online-reports-empty">
          {t("evalPage.online.reports.empty")}
        </div>
      )}
      {reports && reports.reports.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ minWidth: 560 }}>
            <thead>
              <tr>
                <th>{t("evalPage.online.reports.col.created")}</th>
                <th>{t("evalPage.online.reports.col.origin")}</th>
                <th>{t("evalPage.online.reports.col.status")}</th>
                <th>{t("evalPage.online.reports.col.sessions")}</th>
                <th>{t("evalPage.online.reports.col.insights")}</th>
              </tr>
            </thead>
            <tbody>{reports.reports.map((r) => reportRow(r))}</tbody>
          </table>
        </div>
      )}
      {reports && (reports.unattributed?.length ?? 0) > 0 && (
        <div style={{ marginTop: 10 }}>
          <button
            type="button"
            className="selchip"
            data-testid="online-reports-unattributed"
            onClick={() => setShowUnattributed((v) => !v)}
          >
            {showUnattributed ? "▾" : "▸"}{" "}
            {t("evalPage.online.reports.unattributed", {
              count: reports.unattributed?.length ?? 0,
            })}
          </button>
          {showUnattributed && (
            <>
              <div
                className="mono dim"
                style={{ fontSize: 9.5, margin: "6px 0" }}
              >
                {t("evalPage.online.reports.unattributedNote")}
              </div>
              <div style={{ overflowX: "auto" }}>
                <table style={{ minWidth: 560 }}>
                  <tbody>
                    {(reports.unattributed ?? []).map((r) =>
                      reportRow(r, true),
                    )}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );

  // ─── page ───────────────────────────────────────────────────────────────

  const panelEnd = showCreate ? (
    <Chip tone="muted">{t("evalPage.online.form.badge")}</Chip>
  ) : detail ? (
    <div style={{ display: "flex", gap: 6 }}>
      {canToggle && detail.execution_status === "ENABLED" && (
        <Btn
          data-testid="online-pause"
          disabled={busy || TRANSIENT.has(detail.status ?? "")}
          onClick={() => void toggle("pause")}
        >
          {t("evalPage.online.pause")}
        </Btn>
      )}
      {canToggle && detail.execution_status === "DISABLED" && (
        <Btn
          data-testid="online-resume"
          disabled={busy || TRANSIENT.has(detail.status ?? "")}
          onClick={() => void toggle("resume")}
        >
          {t("evalPage.online.resume")}
        </Btn>
      )}
      {canToggle && (
        <Btn
          data-testid="online-delete"
          disabled={busy || detail.status === "DELETING"}
          onClick={() => setConfirmDelete(detail)}
        >
          {t("evalPage.online.delete")}
        </Btn>
      )}
      {!canToggle && (
        <Chip tone="muted">{t("evalPage.evaluators.readonly")}</Chip>
      )}
    </div>
  ) : null;

  return (
    <section>
      <ViewHead
        kicker={t("evaluation.kicker")}
        title={t(
          creatingNew
            ? "evalPage.online.formTitleCreate"
            : "evalPage.online.title",
        )}
        meta={t(
          creatingNew ? "evalPage.online.formSub" : "evalPage.online.meta",
        )}
      />
      <EvaluationNav />
      {staleOe.staleId !== null && (
        <StaleLink
          kind={t("staleLink.kind.onlineConfig")}
          id={staleOe.staleId}
          onDismiss={staleOe.dismiss}
        />
      )}
      <div style={{ marginBottom: 14 }}>
        <Btn onClick={creatingNew ? backToList : onBack}>
          ◂ {t(creatingNew ? "evalPage.online.title" : "evalPage.backToRuns")}
        </Btn>
      </div>

      {!creatingNew && (
        <Panel
          brk
          pad={false}
          title={t("evalPage.online.listTitle")}
          sub={t("evalPage.online.listSub")}
          end={
            <Btn
              primary
              data-testid="new-online-btn"
              onClick={() => selectOe("new")}
            >
              + {t("evalPage.online.new")}
            </Btn>
          }
          style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
        >
          <table>
            <thead>
              <tr>
                <th>{t("evalPage.online.col.name")}</th>
                <th>{t("evalPage.online.col.agent")}</th>
                <th>{t("evalPage.online.col.owner")}</th>
                <th>{t("evalPage.online.col.mode")}</th>
                <th>{t("evalPage.online.col.evaluators")}</th>
                <th>{t("evalPage.online.col.sampling")}</th>
                <th>{t("evalPage.online.col.status")}</th>
                <th>{t("evalPage.online.col.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row) => (
                <tr
                  key={row.config_id}
                  data-testid={`online-row-${row.config_id}`}
                  onClick={() => selectOe(row.config_id)}
                  style={{
                    cursor: "pointer",
                    background:
                      selected?.config_id === row.config_id
                        ? "rgba(255,176,0,.045)"
                        : undefined,
                  }}
                >
                  <td className="pri" title={row.config_id}>
                    {row.name ?? row.config_id}
                    {row.duplicate_enabled && (
                      <span
                        className="mono"
                        style={{
                          fontSize: 8.5,
                          marginLeft: 6,
                          color: "var(--warn)",
                        }}
                        title={t("evalPage.online.duplicateWarn")}
                      >
                        ⚠
                      </span>
                    )}
                  </td>
                  <td>
                    {row.agent_name ??
                      (row.matched_agent ? (
                        <span title={t("evalPage.online.matchedHint")}>
                          ≈ {row.matched_agent.name}
                        </span>
                      ) : (
                        <span className="mono dim">—</span>
                      ))}
                  </td>
                  <td>
                    <Chip tone={OWNER_TONE[row.owner]}>
                      {t(`evalPage.online.owner.${row.owner}`)}
                    </Chip>
                  </td>
                  <td>
                    <Chip tone={MODE_TONE[modeOf(row)]}>
                      {t(`evalPage.online.mode.${modeOf(row)}`)}
                    </Chip>
                  </td>
                  <td className="mono dim">
                    {modeOf(row) === "insights"
                      ? t("evalPage.online.insightsCount", {
                          count: row.insights.length,
                        })
                      : row.detailed
                        ? row.evaluators.length
                        : "—"}
                  </td>
                  <td className="mono dim">
                    {row.sampling_percentage != null
                      ? `${row.sampling_percentage}%`
                      : "—"}
                  </td>
                  <td>
                    <span
                      style={{
                        display: "inline-flex",
                        gap: 4,
                        flexWrap: "wrap",
                      }}
                    >
                      {row.status && (
                        <Chip tone={statusTone(row.status)}>{row.status}</Chip>
                      )}
                      {row.execution_status && (
                        <Chip
                          tone={
                            row.execution_status === "ENABLED"
                              ? "aqua"
                              : "muted"
                          }
                        >
                          {row.execution_status}
                        </Chip>
                      )}
                    </span>
                  </td>
                  <td className="mono dim" style={{ whiteSpace: "nowrap" }}>
                    {fmtTime(row.updated_at ?? row.created_at)}
                  </td>
                </tr>
              ))}
              {loading && (
                <tr>
                  <td
                    colSpan={8}
                    className="dim mono"
                    style={{ textAlign: "center" }}
                  >
                    {t("common.loading")}
                  </td>
                </tr>
              )}
              {!loading && loadError && (
                <tr>
                  <td
                    colSpan={8}
                    className="mono"
                    style={{ textAlign: "center", color: "var(--crit)" }}
                  >
                    ✕ {t("evalPage.online.loadFailed")}
                  </td>
                </tr>
              )}
              {!loading && !loadError && rows.length === 0 && (
                <tr>
                  <td
                    colSpan={8}
                    className="dim mono"
                    style={{ textAlign: "center" }}
                  >
                    {t("evalPage.online.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <Pager {...pagerProps} always />
        </Panel>
      )}

      <div className="eval-grid">
        <Panel
          brk
          title={
            showCreate
              ? t("evalPage.online.formTitleCreate")
              : (selected?.name ??
                selected?.config_id ??
                t("evalPage.online.detailTitle"))
          }
          sub={
            showCreate
              ? t("evalPage.online.formSub")
              : selected
                ? `${selected.config_id} · ${selected.agent_name ?? selected.matched_agent?.name ?? t(`evalPage.online.owner.${selected.owner}`)}`
                : undefined
          }
          end={panelEnd}
          style={{ "--i": 1 } as CSSProperties}
        >
          {showCreate && createForm}
          {!showCreate && selected && (
            <>
              {!detail && !detailError && (
                <div className="empty">{t("common.loading")}</div>
              )}
              {detailError && (
                <div className="note" style={{ borderColor: "var(--crit)" }}>
                  <span className="i" style={{ color: "var(--crit)" }}>
                    [✕]
                  </span>
                  <span>
                    {t("evalPage.online.detailFailed")} — {detailError}
                  </span>
                </div>
              )}
              {detailBody}
              {detailMode === "insights" ? reportsBody : resultsBody}
            </>
          )}
        </Panel>

        <Panel
          title={t("evalPage.online.how.title")}
          sub={t("evalPage.online.how.sub")}
          style={{ "--i": 2 } as CSSProperties}
        >
          {(["s1", "s2", "s3", "s4", "s5"] as const).map((step, i) => (
            <div className="kv" key={step}>
              <span className="k mono">{`0${i + 1}`}</span>
              <span
                className="v"
                style={{ textAlign: "left", flex: 1, marginLeft: 12 }}
              >
                {t(`evalPage.online.how.${step}`)}
              </span>
            </div>
          ))}
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("evalPage.online.how.note")}</span>
          </div>
          <div className="note" style={{ marginTop: 8 }}>
            <span className="i">[i]</span>
            <span>{t("evalPage.online.how.insightsNote")}</span>
          </div>
        </Panel>
      </div>

      <ConfirmDialog
        open={confirmDelete !== null}
        title={t("evalPage.online.confirmDelete.title")}
        body={t("evalPage.online.confirmDelete.body", {
          name: confirmDelete?.name ?? confirmDelete?.config_id ?? "",
          logGroup: confirmDelete?.results_log_group ?? "",
        })}
        confirmLabel={t("evalPage.online.delete")}
        onConfirm={() => {
          const row = confirmDelete;
          setConfirmDelete(null);
          if (row) void doDelete(row);
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </section>
  );
}
