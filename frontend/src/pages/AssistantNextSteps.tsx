import type { CSSProperties, ReactNode } from "react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { Btn, Chip, ConfirmDialog, Panel, useToast } from "../components";
import type { ChipTone } from "../components";
import type {
  AgentInfo,
  AssistantEvalOperation,
  AssistantEvalResource,
  AssistantProposal,
  ExperimentSummary,
} from "../lib/api";
import { api, errorMessage } from "../lib/api";
import type { DeployedAgent } from "../lib/assistant";
import type { EvaluationRunInfo } from "../lib/evaluation";
import { evaluationRunPresentation, RUN_TERMINAL_STATUSES } from "../lib/evaluation";

const MAX_BATCH_EVALUATORS = 10;

/**
 * NEXT STEPS — shown under the Evaluation Assets panel once the creation
 * operation has SUCCEEDED. Guidance with deep links into the existing pages, plus
 * two shortcuts: START EVALUATION posts the same `POST /api/eval/runs` the New Run
 * form would send (agent + created Dataset + created evaluators), and CONVERT TO
 * RUNTIME posts `POST /api/agents/{id}/convert` — both behind a confirm dialog
 * because they invoke the Agent / create billable AWS resources.
 */

const RUN_POLL_MS = 5000;
const TWIN_POLL_MS = 5000;
const RUN_HISTORY_LIMIT = 10;

const AGENT_TONE: Record<string, ChipTone> = {
  deploying: "warn",
  active: "good",
  failed: "crit",
};

export type { DeployedAgent } from "../lib/assistant";

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function Step({
  n,
  title,
  testid,
  children,
  actions,
}: {
  n: number;
  title: ReactNode;
  testid: string;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <li className="assist-next-step" data-testid={testid}>
      <span className="assist-next-n mono">{String(n).padStart(2, "0")}</span>
      <div className="assist-next-body">
        <h4>{title}</h4>
        <div className="assist-next-text">{children}</div>
        {actions && <div className="assist-actions" style={{ marginTop: 8 }}>{actions}</div>}
      </div>
    </li>
  );
}

export function AssistantNextSteps({
  operation,
  proposal,
  deployed,
  index,
}: {
  operation: AssistantEvalOperation;
  proposal: AssistantProposal | null;
  deployed: DeployedAgent | null;
  index: number;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const [confirmStart, setConfirmStart] = useState(false);
  const [starting, setStarting] = useState(false);
  // Run history of THIS agent on THIS Dataset, read from the ledger (survives reloads);
  // null until the first load answers.
  const [runs, setRuns] = useState<EvaluationRunInfo[] | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const resources = Array.isArray(operation.resources) ? operation.resources : [];
  const ready = (r: AssistantEvalResource) => r.status === "ready";

  const datasetRes = resources.find((r) => r.kind === "dataset" && ready(r)) ?? null;
  const datasetResult = asRecord(datasetRes?.result);
  const datasetId = String(datasetResult.dataset_id ?? operation.dataset_id ?? "");
  const datasetItems = Number(datasetResult.item_count ?? 0);

  const evaluators = resources
    .filter((r) => (r.kind === "evaluator" || r.kind === "existing") && ready(r))
    .map((r) => ({
      id: String(asRecord(r.result).evaluator_id ?? ""),
      name: r.name,
      referenceDependent: !!r.reference_dependent,
    }))
    .filter((e) => e.id);
  const referenceDependent = evaluators.filter((e) => e.referenceDependent).length;

  const manualTasks = Array.isArray(proposal?.content.manual_tasks)
    ? (proposal?.content.manual_tasks as unknown[]).map(String).filter(Boolean)
    : [];

  const agentName = deployed?.agentName ?? String(proposal?.content.name ?? "");
  const agentFailed = deployed?.jobStatus === "failed" || deployed?.agentStatus === "failed";
  const agentReady =
    !!deployed?.agentId && deployed.jobStatus === "succeeded" && deployed.agentStatus === "active";

  // ── Runtime twin — the subject of a config-bundle A/B ──────────────────────
  // A managed Harness cannot consume a routed configuration bundle, so the A/B
  // ladder in step 03 converts it into a NEW `<name>-rt` zip runtime. Once that
  // twin is active it is the main version here: Chat, runs and the experiment all
  // target it (the Harness stays deployed, untouched). The relation is read back
  // from the ledger (`spec.source_harness`) so a reload — or a conversion started
  // from the Agents page — lands in the same state.
  const harnessId = deployed?.agentId ?? null;
  const [twins, setTwins] = useState<AgentInfo[] | null>(null);
  const [twinError, setTwinError] = useState<string | null>(null);
  const [confirmConvert, setConfirmConvert] = useState(false);
  const [converting, setConverting] = useState(false);
  const [experiments, setExperiments] = useState<ExperimentSummary[]>([]);
  const loadTwins = useCallback(async () => {
    if (!harnessId) {
      setTwins([]);
      return;
    }
    try {
      const res = await api.listAgentConversions(harnessId);
      setTwins(res.conversions);
      setTwinError(null);
    } catch (err) {
      setTwinError(errorMessage(err));
    }
  }, [harnessId]);
  useEffect(() => {
    void loadTwins();
  }, [loadTwins]);
  // prefer an active twin; otherwise the newest one (deploying or failed)
  const twin = (twins ?? []).find((a) => a.status === "active") ?? (twins ?? [])[0] ?? null;
  const twinDeploying = twin?.status === "deploying";
  const twinActive = twin?.status === "active";
  const twinFailed = twin?.status === "failed";
  useEffect(() => {
    if (!twinDeploying) return;
    const timer = window.setInterval(() => void loadTwins(), TWIN_POLL_MS);
    return () => window.clearInterval(timer);
  }, [twinDeploying, loadTwins]);
  // one running experiment per workspace: say so instead of letting the link 409
  useEffect(() => {
    if (!twinActive) return;
    api
      .listExperiments()
      .then((res) => setExperiments(res.experiments))
      .catch(() => setExperiments([]));
  }, [twinActive]);
  const runningExperiment = experiments.find((e) => e.status === "running") ?? null;

  const canConvert = can("agents.convert");
  const convertable = agentReady && !!harnessId && (!twin || twinFailed) && !converting;
  const convertReason = !canConvert
    ? t("assistantNext.ab.convert.noPermission")
    : !agentReady
      ? t("assistantNext.run.agentNotReady")
      : undefined;
  const convert = async () => {
    if (!harnessId || !convertable) return;
    setConverting(true);
    setTwinError(null);
    try {
      const res = await api.convertAgent(harnessId);
      setTwins((prev) => [res.agent, ...(prev ?? []).filter((a) => a.id !== res.agent.id)]);
      toast(t("assistantNext.ab.convert.startedToast", { name: res.agent.name }), "good");
    } catch (err) {
      const message = errorMessage(err);
      setTwinError(message);
      toast(message);
    } finally {
      setConverting(false);
    }
  };

  // The agent every step targets: the active twin, else the deployed Harness.
  const targetId = twinActive && twin ? twin.id : (deployed?.agentId ?? null);
  const targetName = twinActive && twin ? twin.name : agentName;
  const targetReady = twinActive || agentReady;

  const runParams = new URLSearchParams({ view: "new" });
  if (targetId) runParams.set("agent", targetId);
  if (datasetId) runParams.set("dataset", datasetId);
  if (evaluators.length) runParams.set("evaluators", evaluators.map((e) => e.id).join(","));
  const runLink = `/evaluation?${runParams.toString()}`;

  // One-click start: identical payload to New Run (dataset scope, evaluators mode).
  const canRun = can("eval.run");
  // StartBatchEvaluation applies at most this many evaluators per run (service limit).
  const tooMany = evaluators.length > MAX_BATCH_EVALUATORS;
  const startable =
    targetReady && !!targetId && !!datasetId && evaluators.length > 0 && !tooMany;
  const runLive = (runs ?? []).some((r) => !RUN_TERMINAL_STATUSES.has(r.status));
  const startReason = !canRun
    ? t("assistantNext.run.noPermission")
    : !targetReady
      ? t("assistantNext.run.agentNotReady")
      : !datasetId || evaluators.length === 0
        ? t("assistantNext.run.assetsMissing")
        : tooMany
          ? t("assistantNext.run.tooManyEvaluators", {
              n: evaluators.length,
              max: MAX_BATCH_EVALUATORS,
            })
          : runLive
            ? t("assistantNext.run.oneAtATime")
            : undefined;

  const [removing, setRemoving] = useState<string | null>(null);
  const removeRun = async (run: EvaluationRunInfo) => {
    setRemoving(run.id);
    try {
      await api.deleteEvaluationRun(run.id);
      toast(t("assistantNext.run.removedToast", { id: run.id.slice(0, 8) }), "good");
      setRuns((rs) => (rs ?? []).filter((r) => r.id !== run.id));
    } catch (err) {
      toast(t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setRemoving(null);
    }
  };

  const agentIdForRuns = targetId;
  const loadRuns = useCallback(async () => {
    if (!agentIdForRuns || !datasetId) {
      setRuns([]);
      return;
    }
    try {
      const res = await api.listEvaluationRuns({
        agent_id: agentIdForRuns,
        dataset_id: datasetId,
        mode: "evaluators",
        limit: RUN_HISTORY_LIMIT,
      });
      setRuns(res.runs);
      setRunError(null);
    } catch (err) {
      // keep whatever is shown; say why the refresh failed
      setRunError(errorMessage(err));
    }
  }, [agentIdForRuns, datasetId]);

  const startRun = async () => {
    if (!targetId || !startable) return;
    setStarting(true);
    setRunError(null);
    try {
      const created = await api.createEvaluationRun({
        agent_id: targetId,
        dataset_id: datasetId,
        evaluators: evaluators.map((e) => e.id),
      });
      setRuns((prev) => [created, ...(prev ?? []).filter((r) => r.id !== created.id)]);
      toast(t("assistantNext.run.startedToast", { id: created.id.slice(0, 8) }), "good");
    } catch (err) {
      const message = errorMessage(err);
      setRunError(message);
      toast(message);
    } finally {
      setStarting(false);
    }
  };

  // Load the history on mount / when the (agent, dataset) pair changes …
  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);
  // … and keep polling the ledger while any listed run is still live.
  useEffect(() => {
    if (!runLive) return;
    const timer = window.setInterval(() => void loadRuns(), RUN_POLL_MS);
    return () => window.clearInterval(timer);
  }, [runLive, loadRuns]);

  // Baseline for the A/B: the newest cleanly completed run of the TWIN on this Dataset
  // (`runs` already targets the twin once it is active). Recommended, not
  // required — the experiment link stays live and the page reports readiness.
  const baseline = twinActive
    ? (runs ?? []).find((r) => evaluationRunPresentation(r).status === "completed") ?? null
    : null;
  const baselineMean =
    baseline && baseline.scores.length > 0
      ? baseline.scores.reduce((acc, s) => acc + (s.score ?? 0), 0) / baseline.scores.length
      : null;
  const experimentParams = new URLSearchParams({ view: "experiment", exp: "new" });
  if (targetId) experimentParams.set("agent", targetId);
  if (baseline) experimentParams.set("baselineRun", baseline.id);
  const experimentLink = `/evaluation?${experimentParams.toString()}`;

  let n = 0;
  return (
    <Panel
      brk
      className="assist-span"
      title={t("assistantNext.title")}
      sub={t("assistantNext.sub")}
      data-testid="next-steps"
      data-agent-ready={agentReady ? "true" : "false"}
      style={{ "--i": index } as CSSProperties}
    >
      <ol className="assist-next">
        <Step
          n={++n}
          testid="next-step-chat"
          title={t("assistantNext.chat.title")}
          actions={
            targetReady && targetId ? (
              <Link
                className="assist-link"
                to={`/chat?agent=${encodeURIComponent(targetId)}`}
                data-testid="next-open-chat"
              >
                {t("assistantNext.chat.open")}
              </Link>
            ) : (
              <Link className="assist-link" to="/agents" data-testid="next-open-agents">
                {t("assistantNext.agents")}
              </Link>
            )
          }
        >
          {targetReady
            ? t("assistantNext.chat.body", { name: targetName })
            : agentFailed
              ? t("assistantNext.chat.failed", { name: agentName })
              : t("assistantNext.chat.waiting", {
                  name: agentName,
                  status: String(deployed?.jobStatus ?? deployed?.agentStatus ?? "—"),
                })}
        </Step>

        <Step
          n={++n}
          testid="next-step-run"
          title={t("assistantNext.run.title")}
          actions={
            <>
              <Btn
                primary
                disabled={!startable || !canRun || starting || runLive}
                disabledReason={startReason}
                onClick={() => setConfirmStart(true)}
                data-testid="next-start-run"
              >
                {starting
                  ? t("assistantNext.run.starting")
                  : (runs?.length ?? 0) > 0
                    ? t("assistantNext.run.startAgain")
                    : t("assistantNext.run.start")}
              </Btn>
              <Link className="assist-link" to={runLink} data-testid="next-open-run">
                {t("assistantNext.run.open")}
              </Link>
            </>
          }
        >
          <div>
            {t("assistantNext.run.body", {
              agent: targetName,
              dataset: datasetRes?.name ?? datasetId,
              items: datasetItems,
              n: evaluators.length,
            })}
          </div>
          {twinActive && twin && (
            <div className="assist-next-chips" data-testid="next-run-target">
              <Chip tone="good" className="mono">
                {t("assistantNext.ab.target", { name: twin.name })}
              </Chip>
            </div>
          )}
          {evaluators.length > 0 && (
            <div className="assist-next-chips" data-testid="next-evaluators">
              {evaluators.map((e) => (
                <Chip
                  key={e.id}
                  tone={e.referenceDependent ? "warn" : "muted"}
                  className="mono"
                  style={{ whiteSpace: "normal", overflowWrap: "anywhere", maxWidth: "100%" }}
                >
                  {e.name}
                </Chip>
              ))}
            </div>
          )}
          {referenceDependent > 0 && (
            <div className="dim" style={{ marginTop: 6 }}>
              {t("assistantNext.run.referenceDependent", { n: referenceDependent })}
            </div>
          )}
          {runs && runs.length > 0 && (
            <div className="assist-next-runs" data-testid="next-runs" data-live={runLive ? "true" : "false"}>
              <div className="mono dim" style={{ fontSize: 9.5, letterSpacing: ".12em" }}>
                {t("assistantNext.run.historyTitle", { n: runs.length })}
              </div>
              {runs.map((run) => {
                const live = !RUN_TERMINAL_STATUSES.has(run.status);
                const presentation = evaluationRunPresentation(run);
                const mean =
                  run.scores.length > 0
                    ? run.scores.reduce((acc, s) => acc + (s.score ?? 0), 0) / run.scores.length
                    : null;
                return (
                  <div
                    key={run.id}
                    className="assist-next-run mono"
                    data-testid={`next-run-${run.id}`}
                    data-run-status={run.status}
                    data-run-display-status={presentation.status}
                  >
                    <span>{t("assistantNext.run.runLine", { id: run.id.slice(0, 8) })}</span>
                    <Chip tone={presentation.tone}>
                      {t(`expPage.readiness.runStatus.${presentation.status}`)}
                    </Chip>
                    {run.created_at && (
                      <span className="dim">{new Date(run.created_at).toLocaleString()}</span>
                    )}
                    {run.queue_position != null && live && (
                      <span className="dim">
                        {t("assistantNext.run.queued", { n: run.queue_position })}
                      </span>
                    )}
                    {mean != null && (
                      <span className="dim">
                        {t("assistantNext.run.meanScore", {
                          mean: mean.toFixed(2),
                          n: run.scores.length,
                        })}
                      </span>
                    )}
                    <Link to="/evaluation" style={{ color: "var(--amber)", textDecoration: "none" }}>
                      {t("assistantNext.run.openRuns")} ▸
                    </Link>
                    {canRun && (run.status === "failed" || run.status === "stopped") && (
                      <button
                        type="button"
                        className="rowact"
                        title={t("assistantNext.run.remove")}
                        aria-label={t("assistantNext.run.remove")}
                        data-testid={`next-run-remove-${run.id}`}
                        disabled={removing === run.id}
                        onClick={() => void removeRun(run)}
                        style={{ marginLeft: 4 }}
                      >
                        ✕
                      </button>
                    )}
                    {run.error && (
                      <div style={{
                        color: presentation.status === "completed_with_errors"
                          ? "var(--warn)" : "var(--crit)",
                        flexBasis: "100%",
                        whiteSpace: "pre-wrap",
                        overflowWrap: "anywhere",
                      }}>
                        {run.error}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {runError && (
            <div className="note" style={{ borderColor: "var(--crit)", marginTop: 8 }} data-testid="next-run-error">
              <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
              <span className="mono">{runError}</span>
            </div>
          )}
        </Step>

        <Step
          n={++n}
          testid="next-step-iterate"
          title={t("assistantNext.iterate.title")}
          actions={
            <>
              <Link className="assist-link" to="/observability" data-testid="next-open-obs">
                {t("assistantNext.iterate.openObs")}
              </Link>
              <Link className="assist-link" to="/agents" data-testid="next-open-agents-iterate">
                {t("assistantNext.agents")}
              </Link>
            </>
          }
        >
          <div>{t("assistantNext.iterate.body")}</div>
          {harnessId && (
            <div
              className="assist-next-runs"
              data-testid="next-ab"
              data-twin-status={twin?.status ?? "none"}
              data-target={targetId ?? ""}
            >
              <div className="mono dim" style={{ fontSize: 9.5, letterSpacing: ".12em" }}>
                {t("assistantNext.ab.title")}
              </div>
              <div style={{ marginTop: 4 }}>{t("assistantNext.ab.body")}</div>

              {/* a · convert the Harness into a runtime twin */}
              <div className="assist-next-run mono" data-testid="next-ab-convert">
                <span>{t("assistantNext.ab.convert.label")}</span>
                {twin ? (
                  <>
                    <Chip tone={AGENT_TONE[twin.status] ?? "muted"}>
                      {twin.status.toUpperCase()}
                    </Chip>
                    <span>{twin.name}</span>
                    {twinActive && (
                      <span className="dim">{t("assistantNext.ab.convert.main")}</span>
                    )}
                    {twinDeploying && (
                      <span className="dim">{t("assistantNext.ab.convert.deploying")}</span>
                    )}
                  </>
                ) : (
                  <span className="dim">{t("assistantNext.ab.convert.none")}</span>
                )}
                {(!twin || twinFailed) && (
                  <Btn
                    primary
                    disabled={!convertable || !canConvert}
                    disabledReason={convertReason}
                    onClick={() => setConfirmConvert(true)}
                    data-testid="next-convert"
                  >
                    {converting
                      ? t("assistantNext.ab.convert.starting")
                      : twinFailed
                        ? t("assistantNext.ab.convert.retry")
                        : t("assistantNext.ab.convert.start")}
                  </Btn>
                )}
                {twinFailed && twin?.error && (
                  <div style={{ color: "var(--crit)", flexBasis: "100%", whiteSpace: "pre-wrap" }}>
                    {twin.error}
                  </div>
                )}
              </div>

              {/* b · baseline = the newest cleanly completed run of the twin on this Dataset */}
              <div className="assist-next-run mono" data-testid="next-ab-baseline">
                <span>{t("assistantNext.ab.baseline.label")}</span>
                {baseline ? (
                  <>
                    <Chip tone="good">{t("assistantNext.run.runLine", { id: baseline.id.slice(0, 8) })}</Chip>
                    {baselineMean != null && (
                      <span className="dim">
                        {t("assistantNext.run.meanScore", {
                          mean: baselineMean.toFixed(2),
                          n: baseline.scores.length,
                        })}
                      </span>
                    )}
                  </>
                ) : (
                  <span className="dim">
                    {twinActive
                      ? t("assistantNext.ab.baseline.missing", { name: targetName })
                      : t("assistantNext.ab.baseline.waiting")}
                  </span>
                )}
              </div>

              {/* c · the experiment itself lives on the Evaluation page */}
              <div className="assist-next-run mono" data-testid="next-ab-experiment">
                <span>{t("assistantNext.ab.experiment.label")}</span>
                {twinActive && targetId ? (
                  <Link
                    to={experimentLink}
                    style={{ color: "var(--amber)", textDecoration: "none" }}
                    data-testid="next-open-experiment"
                  >
                    {t("assistantNext.ab.experiment.open")} ▸
                  </Link>
                ) : (
                  <span className="dim">{t("assistantNext.ab.experiment.waiting")}</span>
                )}
                {twinActive && !baseline && (
                  <span style={{ color: "var(--warn)" }}>
                    {t("assistantNext.ab.experiment.noBaseline")}
                  </span>
                )}
                {runningExperiment && (
                  <span className="dim" style={{ flexBasis: "100%" }}>
                    {t("assistantNext.ab.experiment.running", { name: runningExperiment.name })}{" "}
                    <Link
                      to={`/evaluation?view=experiment&exp=${encodeURIComponent(runningExperiment.id)}`}
                      style={{ color: "var(--amber)", textDecoration: "none" }}
                    >
                      {t("assistantNext.ab.experiment.openRunning")} ▸
                    </Link>
                  </span>
                )}
              </div>
              {twinError && (
                <div className="note" style={{ borderColor: "var(--crit)", marginTop: 8 }} data-testid="next-ab-error">
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span className="mono">{twinError}</span>
                </div>
              )}
            </div>
          )}
        </Step>

        {manualTasks.length > 0 && (
          <Step n={++n} testid="next-step-manual" title={t("assistantNext.manual.title")}>
            <div>{t("assistantNext.manual.body")}</div>
            <ul className="assist-steps" data-testid="next-manual-tasks">
              {manualTasks.map((task, i) => (
                <li key={i}>{task}</li>
              ))}
            </ul>
          </Step>
        )}

        <Step
          n={++n}
          testid="next-step-online"
          title={t("assistantNext.online.title")}
          actions={
            <Link
              className="assist-link"
              to="/evaluation?view=online"
              data-testid="next-open-online"
            >
              {t("assistantNext.online.open")}
            </Link>
          }
        >
          {t("assistantNext.online.body")}
        </Step>
      </ol>
      <div className="note" style={{ marginTop: 12 }} data-testid="next-steps-note">
        <span className="i">[i]</span>
        <span>{t("assistantNext.note")}</span>
      </div>
      <ConfirmDialog
        open={confirmStart}
        title={t("assistantNext.run.confirmTitle")}
        body={t("assistantNext.run.confirmBody", {
          agent: targetName,
          dataset: datasetRes?.name ?? datasetId,
          items: datasetItems,
          n: evaluators.length,
          account: operation.account_id,
          region: operation.region,
        })}
        confirmLabel={t("assistantNext.run.confirm")}
        onConfirm={() => {
          setConfirmStart(false);
          void startRun();
        }}
        onCancel={() => setConfirmStart(false)}
      />
      <ConfirmDialog
        open={confirmConvert}
        title={t("assistantNext.ab.convert.confirmTitle")}
        body={t("assistantNext.ab.convert.confirmBody", {
          name: agentName,
          twin: `${agentName}-rt`,
          account: operation.account_id,
          region: operation.region,
        })}
        confirmLabel={t("assistantNext.ab.convert.confirm")}
        onConfirm={() => {
          setConfirmConvert(false);
          void convert();
        }}
        onCancel={() => setConfirmConvert(false)}
      />
    </Panel>
  );
}
