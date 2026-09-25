import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api, errorMessage } from "../../../lib/api";
import {
  ACTIVE_RUN_STATUSES,
  DEFAULT_EVALUATORS,
  evaluationRunPresentation,
  type EvaluationRunInfo,
  type ExperimentReadiness,
} from "../../../lib/evaluation";
import { DEFAULT_TRACE_LOOKBACK_HOURS, TRACE_LOOKBACK_OPTIONS, traceLookbackFromParam } from "../../../lib/experiments";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Descriptions, Field, FlowHeader, Tag, type TagTone } from "../../ui";

const READINESS_TONE: Record<ExperimentReadiness["state"], TagTone> = {
  ready: "green",
  missing: "red",
  sparse: "orange",
  unavailable: "gray",
};

const RUN_TONE: Record<string, TagTone> = { good: "green", warn: "orange", crit: "red", muted: "gray" };

/**
 * 新建实验: pick an eligible agent, check its trace readiness over a lookback
 * window (generate a baseline evaluation run when it has none), then start.
 * URL: `?view=new&agent=&lookback=` plus the hand-offs `baselineRun=` / `sourceRun=`.
 */
export function ExperimentStart({ hasRunning }: { hasRunning: boolean }) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const toast = useV2Toast();
  const navigate = useNavigate();
  const requestedAgent = params.get("agent") ?? "";
  const lookback = traceLookbackFromParam(params.get("lookback"));
  const baselineRunId = params.get("baselineRun");
  const sourceRunId = params.get("sourceRun");
  const trackedRunId = baselineRunId ?? sourceRunId;

  const agentsLoad = useLoad(() => api.listAgents(), "agents");
  const datasetsLoad = useLoad(() => api.v2Datasets(), "datasets");
  const active = useMemo(() => (agentsLoad.data?.agents ?? []).filter((a) => a.status === "active"), [agentsLoad.data]);
  const eligible = active.filter((a) => a.experiment_capability.eligible);
  const unsupported = active.filter((a) => !a.experiment_capability.eligible);
  const agentId = eligible.some((a) => a.id === requestedAgent) ? requestedAgent : (eligible[0]?.id ?? "");
  const datasets = useMemo(() => (datasetsLoad.data?.datasets ?? []).filter((d) => d.kind !== "simulated"), [datasetsLoad.data]);

  const [readiness, setReadiness] = useState<ExperimentReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [sparseAck, setSparseAck] = useState(false);
  const [unavailableAck, setUnavailableAck] = useState(false);
  const [baselineDataset, setBaselineDataset] = useState("");
  const [baselineBusy, setBaselineBusy] = useState(false);
  const [run, setRun] = useState<EvaluationRunInfo | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0);

  const setUrl = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    next.set("view", "new");
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    setParams(next);
  };

  const loadReadiness = useCallback(
    async (force = false) => {
      const ticket = ++request.current;
      if (!agentId) {
        setReadiness(null);
        setReadinessLoading(false);
        return;
      }
      setReadinessLoading(true);
      try {
        const res = await api.v2ExperimentReadiness(agentId, lookback, force);
        if (ticket === request.current) setReadiness(res);
      } catch {
        if (ticket === request.current) {
          setReadiness({
            agent_id: agentId,
            lookback_hours: lookback,
            state: "unavailable",
            trace_count: 0,
            session_count: 0,
            latest_trace_at: null,
            observed_tools: [],
            expected_tools: [],
            missing_tools: [],
            latest_run: null,
            message: null,
          });
        }
      } finally {
        if (ticket === request.current) setReadinessLoading(false);
      }
    },
    [agentId, lookback],
  );

  useEffect(() => {
    setSparseAck(false);
    setUnavailableAck(false);
    setError(null);
    setReadiness(null);
    void loadReadiness();
  }, [loadReadiness]);

  useEffect(() => {
    setBaselineDataset((prev) => (datasets.some((d) => d.id === prev) ? prev : (datasets[0]?.id ?? "")));
  }, [datasets]);

  // the hand-off / baseline run (or readiness' latest run): follow it while it is active;
  // a baseline that changes status re-reads readiness
  const displayedRunId = trackedRunId ?? readiness?.latest_run?.id ?? null;
  useEffect(() => {
    if (!displayedRunId) {
      setRun(null);
      setRunError(null);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    let prior: string | null = null;
    const poll = async () => {
      try {
        const r = await api.getEvaluationRun(displayedRunId);
        if (cancelled) return;
        setRun(r);
        setRunError(null);
        if (baselineRunId && r.status !== prior) {
          prior = r.status;
          void loadReadiness(true);
        }
        if (ACTIVE_RUN_STATUSES.has(r.status)) timer = window.setTimeout(() => void poll(), 2500);
      } catch (err) {
        if (cancelled) return;
        setRunError(errorMessage(err));
        if (baselineRunId) timer = window.setTimeout(() => void poll(), 2500);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [displayedRunId, baselineRunId, loadReadiness]);

  const shownRun = run?.id === displayedRunId ? run : null;
  const presentation = shownRun ? evaluationRunPresentation(shownRun) : null;
  const allowsCreate =
    readiness?.agent_id === agentId &&
    (readiness.state === "ready" || (readiness.state === "sparse" && sparseAck) || (readiness.state === "unavailable" && unavailableAck));

  const startBaseline = async () => {
    if (!agentId || !baselineDataset) return;
    setBaselineBusy(true);
    setRunError(null);
    try {
      const r = await api.createEvaluationRun({ agent_id: agentId, dataset_id: baselineDataset, evaluators: DEFAULT_EVALUATORS });
      setRun(r);
      setUrl({ agent: agentId, baselineRun: r.id, sourceRun: null });
    } catch (err) {
      setRunError(errorMessage(err));
    } finally {
      setBaselineBusy(false);
    }
  };

  const start = async () => {
    setError(null);
    setBusy(true);
    try {
      const exp = await api.v2CreateExperiment(agentId, lookback);
      toast("success", t("v2.experiments.started", { name: exp.name }));
      setParams({ view: "detail", id: exp.id });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const runTag = presentation && (
    <Tag tone={RUN_TONE[presentation.tone] ?? "gray"}>{t(`expPage.readiness.runStatus.${presentation.status}`)}</Tag>
  );

  return (
    <>
      <FlowHeader
        title={t("expPage.start")}
        onBack={() => setParams({})}
        end={
          <Button
            kind="primary"
            disabled={busy || readinessLoading || !agentId || hasRunning || !allowsCreate}
            onClick={() => void start()}
            testId="v2-exp-start"
          >
            {t("expPage.start")}
          </Button>
        }
      />
      <Alert>{t("expPage.startHint")}</Alert>
      {hasRunning && <Alert tone="warn">{t("evalPage.experiment.runningGuard")}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}

      <Card title={t("v2.experiments.pickAgent")}>
        <div className="v2-form cols-2">
          <Field label={t("evalPage.newRun.agent")} required hint={unsupported.length ? t("expPage.unsupportedHint") : undefined}>
            <select
              className="v2-select"
              value={agentId}
              onChange={(e) => setUrl({ agent: e.target.value, baselineRun: null, sourceRun: null })}
              data-testid="v2-exp-agent"
            >
              {eligible.length === 0 && <option value="">{t("evalPage.newRun.noAgents")}</option>}
              {eligible.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} · {a.method}
                </option>
              ))}
              {unsupported.map((a) => (
                <option key={a.id} value="" disabled>
                  {a.name} · {a.method} —{" "}
                  {a.experiment_capability.reason_code ? t(`expPage.reason.${a.experiment_capability.reason_code}`) : a.experiment_capability.reason}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("expPage.readiness.window")}>
            <select
              className="v2-select"
              value={lookback}
              onChange={(e) => {
                const hours = Number(e.target.value);
                setUrl({ lookback: hours === DEFAULT_TRACE_LOOKBACK_HOURS ? null : String(hours) });
              }}
              data-testid="v2-exp-lookback"
            >
              {TRACE_LOOKBACK_OPTIONS.map((h) => (
                <option key={h} value={h}>
                  {t(`expPage.readiness.windowOption.h${h}`)}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </Card>

      <Card
        title={t("expPage.readiness.title")}
        end={
          <span className="v2-row">
            {readiness && <Tag tone={READINESS_TONE[readiness.state]}>{t(`expPage.readiness.state.${readiness.state}`)}</Tag>}
            <Button size="sm" disabled={readinessLoading || !agentId} onClick={() => void loadReadiness(true)}>
              <RefreshCw size={13} aria-hidden="true" />
              {t("expPage.readiness.retry")}
            </Button>
          </span>
        }
        testId="v2-exp-readiness"
      >
        {readinessLoading && !readiness ? (
          <span className="v2-muted">{t("expPage.readiness.loading")}</span>
        ) : readiness ? (
          <>
            <Descriptions
              items={[
                { label: t("expPage.readiness.traces"), value: readiness.trace_count },
                { label: t("expPage.readiness.sessions"), value: readiness.session_count },
                { label: t("expPage.readiness.latest"), value: fmtTime(readiness.latest_trace_at) },
                ...(readiness.latest_run && !trackedRunId
                  ? [
                      {
                        label: t("expPage.readiness.latestRun"),
                        value: (
                          <span className="v2-row">
                            <span className="mono">run-{readiness.latest_run.id.slice(0, 6)}</span>
                            {runTag}
                            {t("expPage.readiness.runSessions", { count: readiness.latest_run.session_count })}
                          </span>
                        ),
                      },
                    ]
                  : []),
                ...(readiness.expected_tools.length
                  ? [
                      { label: t("expPage.readiness.observedTools"), value: <span className="mono">{readiness.observed_tools.join(", ") || "—"}</span> },
                      { label: t("expPage.readiness.missingTools"), value: <span className="mono">{readiness.missing_tools.join(", ") || "—"}</span> },
                    ]
                  : []),
              ]}
            />
            <div className="v2-stack" style={{ marginTop: 16 }}>
              {readiness.state === "missing" && <Alert tone="error">{t("expPage.readiness.missingHint")}</Alert>}
              {readiness.state === "sparse" && (
                <label className="v2-check">
                  <input type="checkbox" checked={sparseAck} onChange={(e) => setSparseAck(e.target.checked)} data-testid="v2-exp-sparse-ack" />
                  {t("expPage.readiness.sparseAck")}
                </label>
              )}
              {readiness.state === "unavailable" && (
                <label className="v2-check">
                  <input type="checkbox" checked={unavailableAck} onChange={(e) => setUnavailableAck(e.target.checked)} data-testid="v2-exp-unavailable-ack" />
                  {t("expPage.readiness.unavailableAck")}
                </label>
              )}
              {trackedRunId && (
                <Alert>
                  <span className="v2-row">
                    {sourceRunId
                      ? t("expPage.readiness.sourceRun", { id: sourceRunId.slice(0, 6) })
                      : t("expPage.readiness.baselineRun", { id: baselineRunId?.slice(0, 6) ?? "" })}
                    {shownRun && runTag}
                    {shownRun && t("expPage.readiness.runSessions", { count: shownRun.session_ids.length })}
                  </span>
                </Alert>
              )}
              {shownRun?.error && (
                <Alert tone={presentation?.status === "completed_with_errors" ? "warn" : "error"}>{shownRun.error}</Alert>
              )}
              {runError && <Alert tone="error">{runError}</Alert>}
            </div>
          </>
        ) : (
          <span className="v2-muted">{t("v2.experiments.pickAgentFirst")}</span>
        )}
      </Card>

      {readiness?.state === "missing" && (
        <Card title={t("v2.experiments.baselineTitle")} sub={t("v2.experiments.baselineSub")}>
          <div className="v2-form cols-2">
            <Field label={t("expPage.readiness.baselineDataset")}>
              <select className="v2-select" value={baselineDataset} onChange={(e) => setBaselineDataset(e.target.value)} data-testid="v2-exp-baseline-dataset">
                {datasets.length === 0 && <option value="">{t("expPage.readiness.noBaselineDataset")}</option>}
                {datasets.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} · {d.item_count}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("v2.common.actions")}>
              <div className="v2-row">
                <Button
                  kind="primary"
                  disabled={baselineBusy || !baselineDataset || !!(run && ACTIVE_RUN_STATUSES.has(run.status))}
                  onClick={() => void startBaseline()}
                  testId="v2-exp-baseline"
                >
                  {baselineBusy ? t("expPage.readiness.startingBaseline") : t("expPage.readiness.generateBaseline")}
                </Button>
                <Button onClick={() => navigate("/v2/eval/tasks?view=new")} title={t("v2.experiments.fullTaskHint")}>
                  {t("v2.experiments.fullTask")}
                </Button>
              </div>
            </Field>
          </div>
        </Card>
      )}
    </>
  );
}
