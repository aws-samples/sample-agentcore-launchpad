import type { CSSProperties, ReactNode } from "react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { Btn, Chip, ConfirmDialog, Panel, useToast } from "../components";
import type { ChipTone } from "../components";
import type { AssistantEvalOperation, AssistantEvalResource, AssistantProposal } from "../lib/api";
import { api, errorMessage } from "../lib/api";
import type { EvaluationRunInfo } from "../lib/evaluation";
import { RUN_TERMINAL_STATUSES } from "../lib/evaluation";

/**
 * NEXT STEPS — shown under the Evaluation Assets panel once the creation
 * operation has SUCCEEDED. Guidance with deep links into the existing pages, plus
 * one shortcut: START EVALUATION posts the same `POST /api/eval/runs` the New Run
 * form would send (agent + created Dataset + created evaluators), behind a
 * confirm dialog because it invokes the Agent and starts a billable batch job.
 */

const RUN_POLL_MS = 5000;
const RUN_HISTORY_LIMIT = 10;

const RUN_TONE: Record<string, ChipTone> = {
  queued: "muted",
  invoking: "warn",
  waiting: "warn",
  evaluating: "warn",
  completed: "good",
  failed: "crit",
  stopped: "muted",
};

export interface DeployedAgent {
  agentId: string | null;
  agentName: string | null;
  agentStatus: string | null;
  jobStatus: string | null;
}

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

  const runParams = new URLSearchParams({ view: "new" });
  if (deployed?.agentId) runParams.set("agent", deployed.agentId);
  if (datasetId) runParams.set("dataset", datasetId);
  if (evaluators.length) runParams.set("evaluators", evaluators.map((e) => e.id).join(","));
  const runLink = `/evaluation?${runParams.toString()}`;

  // One-click start: identical payload to New Run (dataset scope, evaluators mode).
  const canRun = can("eval.run");
  const startable = agentReady && !!deployed?.agentId && !!datasetId && evaluators.length > 0;
  const runLive = (runs ?? []).some((r) => !RUN_TERMINAL_STATUSES.has(r.status));
  const startReason = !canRun
    ? t("assistantNext.run.noPermission")
    : !agentReady
      ? t("assistantNext.run.agentNotReady")
      : !datasetId || evaluators.length === 0
        ? t("assistantNext.run.assetsMissing")
        : runLive
          ? t("assistantNext.run.oneAtATime")
          : undefined;

  const agentIdForRuns = deployed?.agentId ?? null;
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
    if (!deployed?.agentId || !startable) return;
    setStarting(true);
    setRunError(null);
    try {
      const created = await api.createEvaluationRun({
        agent_id: deployed.agentId,
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
            agentReady && deployed?.agentId ? (
              <Link
                className="assist-link"
                to={`/chat?agent=${encodeURIComponent(deployed.agentId)}`}
                data-testid="next-open-chat"
              >
                {t("assistantNext.chat.open")}
              </Link>
            ) : (
              <Link className="assist-link" to="/create" data-testid="next-open-agents">
                {t("assistantNext.agents")}
              </Link>
            )
          }
        >
          {agentReady
            ? t("assistantNext.chat.body", { name: agentName })
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
              agent: agentName,
              dataset: datasetRes?.name ?? datasetId,
              items: datasetItems,
              n: evaluators.length,
            })}
          </div>
          {evaluators.length > 0 && (
            <div className="assist-next-chips" data-testid="next-evaluators">
              {evaluators.map((e) => (
                <Chip key={e.id} tone={e.referenceDependent ? "warn" : "muted"} className="mono">
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
                  >
                    <span>{t("assistantNext.run.runLine", { id: run.id.slice(0, 8) })}</span>
                    <Chip tone={RUN_TONE[run.status] ?? "muted"}>
                      {t(`expPage.readiness.runStatus.${run.status}`, run.status.toUpperCase())}
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
                    {run.error && (
                      <div style={{ color: "var(--crit)", flexBasis: "100%", whiteSpace: "pre-wrap" }}>
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
              <Link className="assist-link" to="/create" data-testid="next-open-agents-iterate">
                {t("assistantNext.agents")}
              </Link>
            </>
          }
        >
          {t("assistantNext.iterate.body")}
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
          agent: agentName,
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
    </Panel>
  );
}
