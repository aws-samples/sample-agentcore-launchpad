import { type ReactNode, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import {
  type AgentInfo,
  api,
  type AssistantEvalOperation,
  type AssistantEvalResource,
  type AssistantProposal,
  errorMessage,
  type ExperimentSummary,
} from "../../../lib/api";
import { asRecord, type DeployedAgent } from "../../../lib/assistant";
import { type EvaluationRunInfo, evaluationRunPresentation, RUN_TERMINAL_STATUSES } from "../../../lib/evaluation";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, LinkButton, Tag } from "../../ui";
import { AGENT_TONE, CHIP_TAG, shortId } from "./common";

/**
 * NEXT STEPS — shown under the evaluation assets once the creation operation has
 * SUCCEEDED: guidance with deep links, plus START EVALUATION (the same
 * `POST /api/eval/runs` New Run sends: agent + created Dataset + created evaluators)
 * and CONVERT TO RUNTIME (`POST /api/agents/{id}/convert`, the A/B subject) — both
 * behind a confirm because they invoke the Agent / create billable AWS resources.
 */

const MAX_BATCH_EVALUATORS = 10;
const RUN_POLL_MS = 5000;
const TWIN_POLL_MS = 5000;
const RUN_HISTORY_LIMIT = 10;

const meanScore = (run: EvaluationRunInfo) =>
  run.scores.length > 0 ? run.scores.reduce((acc, s) => acc + (s.score ?? 0), 0) / run.scores.length : null;

function Step({ n, title, children, actions, testId }: {
  n: number; title: ReactNode; children: ReactNode; actions?: ReactNode; testId: string;
}) {
  return (
    <li data-testid={testId}>
      <span className="n">{n}</span>
      <div className="b">
        <h3>{title}</h3>
        <div>{children}</div>
        {actions && <div className="v2-row" style={{ marginTop: 10 }}>{actions}</div>}
      </div>
    </li>
  );
}

export function NextStepsCard({
  operation, proposal, deployed,
}: {
  operation: AssistantEvalOperation;
  proposal: AssistantProposal | null;
  deployed: DeployedAgent | null;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const [confirmStart, setConfirmStart] = useState(false);
  const [starting, setStarting] = useState(false);
  // run history of THIS agent on THIS Dataset (ledger; survives reloads)
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
    .map((r) => ({ id: String(asRecord(r.result).evaluator_id ?? ""), name: r.name, referenceDependent: !!r.reference_dependent }))
    .filter((e) => e.id);
  const referenceDependent = evaluators.filter((e) => e.referenceDependent).length;
  const manualTasks = Array.isArray(proposal?.content.manual_tasks)
    ? (proposal?.content.manual_tasks as unknown[]).map(String).filter(Boolean)
    : [];

  const agentName = deployed?.agentName ?? String(proposal?.content.name ?? "");
  const agentFailed = deployed?.jobStatus === "failed" || deployed?.agentStatus === "failed";
  const agentReady = !!deployed?.agentId && deployed.jobStatus === "succeeded" && deployed.agentStatus === "active";

  // ── Runtime twin: a managed Harness cannot consume a routed configuration bundle,
  // so the A/B converts it into a NEW `<name>-rt` zip runtime; once active it is the
  // main version here (Chat, runs and the experiment target it). Read from the ledger.
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
  useEffect(() => { void loadTwins(); }, [loadTwins]);
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
    api.listExperiments().then((res) => setExperiments(res.experiments)).catch(() => setExperiments([]));
  }, [twinActive]);
  const runningExperiment = experiments.find((e) => e.status === "running") ?? null;

  const canConvert = can("agents.convert");
  const convertable = agentReady && !!harnessId && (!twin || twinFailed) && !converting;
  const convertReason = !canConvert
    ? t("assistantNext.ab.convert.noPermission")
    : !agentReady ? t("assistantNext.run.agentNotReady") : undefined;
  const convert = async () => {
    if (!harnessId || !convertable) return;
    setConverting(true);
    setTwinError(null);
    try {
      const res = await api.convertAgent(harnessId);
      setTwins((prev) => [res.agent, ...(prev ?? []).filter((a) => a.id !== res.agent.id)]);
      toast("success", t("assistantNext.ab.convert.startedToast", { name: res.agent.name }));
    } catch (err) {
      const message = errorMessage(err);
      setTwinError(message);
      toast("error", message);
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
  const runLink = `/v2/eval/tasks?${runParams.toString()}`;

  const canRun = can("eval.run");
  // StartBatchEvaluation applies at most this many evaluators per run (service limit).
  const tooMany = evaluators.length > MAX_BATCH_EVALUATORS;
  const startable = targetReady && !!targetId && !!datasetId && evaluators.length > 0 && !tooMany;
  const runLive = (runs ?? []).some((r) => !RUN_TERMINAL_STATUSES.has(r.status));
  const startReason = !canRun
    ? t("assistantNext.run.noPermission")
    : !targetReady
      ? t("assistantNext.run.agentNotReady")
      : !datasetId || evaluators.length === 0
        ? t("assistantNext.run.assetsMissing")
        : tooMany
          ? t("assistantNext.run.tooManyEvaluators", { n: evaluators.length, max: MAX_BATCH_EVALUATORS })
          : runLive ? t("assistantNext.run.oneAtATime") : undefined;

  const [removing, setRemoving] = useState<string | null>(null);
  const removeRun = async (run: EvaluationRunInfo) => {
    setRemoving(run.id);
    try {
      await api.deleteEvaluationRun(run.id);
      toast("success", t("assistantNext.run.removedToast", { id: run.id.slice(0, 8) }));
      setRuns((rs) => (rs ?? []).filter((r) => r.id !== run.id));
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setRemoving(null);
    }
  };

  const loadRuns = useCallback(async () => {
    if (!targetId || !datasetId) {
      setRuns([]);
      return;
    }
    try {
      const res = await api.listEvaluationRuns({
        agent_id: targetId, dataset_id: datasetId, mode: "evaluators", limit: RUN_HISTORY_LIMIT,
      });
      setRuns(res.runs);
      setRunError(null);
    } catch (err) {
      setRunError(errorMessage(err));
    }
  }, [targetId, datasetId]);
  useEffect(() => { void loadRuns(); }, [loadRuns]);
  useEffect(() => {
    if (!runLive) return;
    const timer = window.setInterval(() => void loadRuns(), RUN_POLL_MS);
    return () => window.clearInterval(timer);
  }, [runLive, loadRuns]);

  const startRun = async () => {
    if (!targetId || !startable) return;
    setStarting(true);
    setRunError(null);
    try {
      const created = await api.createEvaluationRun({
        agent_id: targetId, dataset_id: datasetId, evaluators: evaluators.map((e) => e.id),
      });
      setRuns((prev) => [created, ...(prev ?? []).filter((r) => r.id !== created.id)]);
      toast("success", t("assistantNext.run.startedToast", { id: created.id.slice(0, 8) }));
    } catch (err) {
      const message = errorMessage(err);
      setRunError(message);
      toast("error", message);
    } finally {
      setStarting(false);
    }
  };

  // Baseline for the A/B: the newest cleanly completed run of the TWIN on this Dataset.
  const baseline = twinActive
    ? (runs ?? []).find((r) => evaluationRunPresentation(r).status === "completed") ?? null
    : null;
  const baselineMean = baseline ? meanScore(baseline) : null;
  const experimentParams = new URLSearchParams({ view: "new" });
  if (targetId) experimentParams.set("agent", targetId);
  if (baseline) experimentParams.set("baselineRun", baseline.id);
  const experimentLink = `/v2/eval/experiments?${experimentParams.toString()}`;

  let n = 0;
  return (
    <Card title={t("assistantNext.title")} sub={t("assistantNext.sub")} testId="v2-assistant-next">
      <ol className="v2-assistant-next" data-agent-ready={agentReady ? "true" : "false"}>
        <Step
          n={++n}
          testId="v2-assistant-next-chat"
          title={t("assistantNext.chat.title")}
          actions={targetReady && targetId ? (
            <Link className="v2-btn sm primary" to={`/v2/chat?agent=${encodeURIComponent(targetId)}`}>
              {t("assistantNext.chat.open")}
            </Link>
          ) : (
            <Link className="v2-btn sm" to="/v2/agents">{t("assistantNext.agents")}</Link>
          )}
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
          testId="v2-assistant-next-run"
          title={t("assistantNext.run.title")}
          actions={
            <>
              <Button kind="primary" size="sm" disabled={!startable || !canRun || starting || runLive}
                title={startReason} onClick={() => setConfirmStart(true)} testId="v2-assistant-next-start-run">
                {starting ? t("assistantNext.run.starting")
                  : (runs?.length ?? 0) > 0 ? t("assistantNext.run.startAgain") : t("assistantNext.run.start")}
              </Button>
              <Link className="v2-btn sm" to={runLink}>{t("assistantNext.run.open")}</Link>
              {startReason && <span className="v2-muted" style={{ fontSize: 12.5 }}>{startReason}</span>}
            </>
          }
        >
          <div>
            {t("assistantNext.run.body", {
              agent: targetName, dataset: datasetRes?.name ?? datasetId, items: datasetItems, n: evaluators.length,
            })}
          </div>
          {twinActive && twin && (
            <div className="v2-tags" style={{ marginTop: 6 }}>
              <Tag tone="green">{t("assistantNext.ab.target", { name: twin.name })}</Tag>
            </div>
          )}
          {evaluators.length > 0 && (
            <div className="v2-tags" style={{ marginTop: 6 }}>
              {evaluators.map((e) => (
                <Tag key={e.id} tone={e.referenceDependent ? "orange" : "outline"}>{e.name}</Tag>
              ))}
            </div>
          )}
          {referenceDependent > 0 && (
            <div className="v2-muted" style={{ marginTop: 6 }}>
              {t("assistantNext.run.referenceDependent", { n: referenceDependent })}
            </div>
          )}
          {runs && runs.length > 0 && (
            <div className="v2-assistant-sub-rows" data-testid="v2-assistant-next-runs" data-live={runLive ? "true" : "false"}>
              <div className="v2-muted" style={{ fontSize: 12.5 }}>{t("assistantNext.run.historyTitle", { n: runs.length })}</div>
              {runs.map((run) => {
                const live = !RUN_TERMINAL_STATUSES.has(run.status);
                const presentation = evaluationRunPresentation(run);
                const mean = meanScore(run);
                return (
                  <div key={run.id} className="v2-assistant-sub-row" data-run-status={run.status}>
                    <span className="mono">{t("assistantNext.run.runLine", { id: run.id.slice(0, 8) })}</span>
                    <Tag tone={CHIP_TAG[presentation.tone] ?? "gray"}>
                      {t(`expPage.readiness.runStatus.${presentation.status}`)}
                    </Tag>
                    {run.created_at && <span className="v2-muted">{fmtTime(run.created_at)}</span>}
                    {run.queue_position != null && live && (
                      <span className="v2-muted">{t("assistantNext.run.queued", { n: run.queue_position })}</span>
                    )}
                    {mean != null && (
                      <span className="v2-muted">
                        {t("assistantNext.run.meanScore", { mean: mean.toFixed(2), n: run.scores.length })}
                      </span>
                    )}
                    <Link to={`/v2/eval/tasks?view=detail&id=${encodeURIComponent(run.id)}`}>{t("assistantNext.run.openRuns")} ›</Link>
                    {canRun && (run.status === "failed" || run.status === "stopped") && (
                      <LinkButton danger disabled={removing === run.id} onClick={() => void removeRun(run)}>
                        {t("assistantNext.run.remove")}
                      </LinkButton>
                    )}
                    {run.error && (
                      <div className={presentation.status === "completed_with_errors" ? "err warn" : "err"}>{run.error}</div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {runError && <div style={{ marginTop: 8 }}><Alert tone="error">{runError}</Alert></div>}
        </Step>

        <Step
          n={++n}
          testId="v2-assistant-next-iterate"
          title={t("assistantNext.iterate.title")}
          actions={
            <>
              <Link className="v2-btn sm" to="/v2/observability">{t("assistantNext.iterate.openObs")}</Link>
              <Link className="v2-btn sm" to="/v2/agents">{t("assistantNext.agents")}</Link>
            </>
          }
        >
          <div>{t("assistantNext.iterate.body")}</div>
          {harnessId && (
            <div className="v2-assistant-sub-rows" data-testid="v2-assistant-next-ab" data-twin-status={twin?.status ?? "none"}>
              <div style={{ fontWeight: 600 }}>{t("assistantNext.ab.title")}</div>
              <div className="v2-muted">{t("assistantNext.ab.body")}</div>
              <div className="v2-assistant-sub-row">
                <span>{t("assistantNext.ab.convert.label")}</span>
                {twin ? (
                  <>
                    <Tag tone={AGENT_TONE[twin.status] ?? "gray"} dot>{t(`status.${twin.status}`, { defaultValue: twin.status })}</Tag>
                    <span className="mono">{twin.name}</span>
                    {twinActive && <span className="v2-muted">{t("assistantNext.ab.convert.main")}</span>}
                    {twinDeploying && <span className="v2-muted">{t("assistantNext.ab.convert.deploying")}</span>}
                  </>
                ) : (
                  <span className="v2-muted">{t("assistantNext.ab.convert.none")}</span>
                )}
                {(!twin || twinFailed) && (
                  <Button size="sm" kind="primary" disabled={!convertable || !canConvert} title={convertReason}
                    onClick={() => setConfirmConvert(true)} testId="v2-assistant-next-convert">
                    {converting ? t("assistantNext.ab.convert.starting")
                      : twinFailed ? t("assistantNext.ab.convert.retry") : t("assistantNext.ab.convert.start")}
                  </Button>
                )}
                {twinFailed && twin?.error && <div className="err">{twin.error}</div>}
              </div>
              <div className="v2-assistant-sub-row">
                <span>{t("assistantNext.ab.baseline.label")}</span>
                {baseline ? (
                  <>
                    <Tag tone="green">{t("assistantNext.run.runLine", { id: shortId(baseline.id) })}</Tag>
                    {baselineMean != null && (
                      <span className="v2-muted">
                        {t("assistantNext.run.meanScore", { mean: baselineMean.toFixed(2), n: baseline.scores.length })}
                      </span>
                    )}
                  </>
                ) : (
                  <span className="v2-muted">
                    {twinActive ? t("assistantNext.ab.baseline.missing", { name: targetName }) : t("assistantNext.ab.baseline.waiting")}
                  </span>
                )}
              </div>
              <div className="v2-assistant-sub-row">
                <span>{t("assistantNext.ab.experiment.label")}</span>
                {twinActive && targetId ? (
                  <Link to={experimentLink} data-testid="v2-assistant-next-experiment">{t("assistantNext.ab.experiment.open")} ›</Link>
                ) : (
                  <span className="v2-muted">{t("assistantNext.ab.experiment.waiting")}</span>
                )}
                {twinActive && !baseline && (
                  <span style={{ color: "var(--v2-warning)" }}>{t("assistantNext.ab.experiment.noBaseline")}</span>
                )}
                {runningExperiment && (
                  <span className="v2-muted" style={{ flexBasis: "100%" }}>
                    {t("assistantNext.ab.experiment.running", { name: runningExperiment.name })}{" "}
                    <Link to={`/v2/eval/experiments?view=detail&id=${encodeURIComponent(runningExperiment.id)}`}>
                      {t("assistantNext.ab.experiment.openRunning")} ›
                    </Link>
                  </span>
                )}
              </div>
              {twinError && <Alert tone="error">{twinError}</Alert>}
            </div>
          )}
        </Step>

        {manualTasks.length > 0 && (
          <Step n={++n} testId="v2-assistant-next-manual" title={t("assistantNext.manual.title")}>
            <div>{t("assistantNext.manual.body")}</div>
            <ul className="v2-list">{manualTasks.map((task, i) => <li key={i}>{task}</li>)}</ul>
          </Step>
        )}

        <Step
          n={++n}
          testId="v2-assistant-next-online"
          title={t("assistantNext.online.title")}
          actions={<Link className="v2-btn sm" to="/v2/eval/online">{t("assistantNext.online.open")}</Link>}
        >
          {t("assistantNext.online.body")}
        </Step>
      </ol>
      <div style={{ marginTop: 12 }}><Alert>{t("assistantNext.note")}</Alert></div>
      <Confirm
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
        onConfirm={() => { setConfirmStart(false); void startRun(); }}
        onClose={() => setConfirmStart(false)}
      />
      <Confirm
        open={confirmConvert}
        title={t("assistantNext.ab.convert.confirmTitle")}
        body={t("assistantNext.ab.convert.confirmBody", {
          name: agentName,
          twin: `${agentName}-rt`,
          account: operation.account_id,
          region: operation.region,
        })}
        confirmLabel={t("assistantNext.ab.convert.confirm")}
        onConfirm={() => { setConfirmConvert(false); void convert(); }}
        onClose={() => setConfirmConvert(false)}
      />
    </Card>
  );
}
