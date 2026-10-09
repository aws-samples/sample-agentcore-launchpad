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
} from "../../../lib/api";
import { asRecord, type DeployedAgent } from "../../../lib/assistant";
import {
  coverageDiffers,
  coverageInfo,
  type EvaluationRunInfo,
  evaluationRunPresentation,
  RUN_TERMINAL_STATUSES,
  runCoverage,
  runMeanScore,
  runRecommendable,
} from "../../../lib/evaluation";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { RunCoverageNote, RunCoverageTag } from "../../RunCoverage";
import { Alert, Button, Card, Confirm, LinkButton, Select, Tag } from "../../ui";
import { RunRecommendations } from "../tasks/RunRecommendations";
import { CHIP_TAG, shortId, useReadOnly } from "./common";
import { HarnessCanary } from "./HarnessCanary";
import { RecommendationHistory } from "./RecommendationHistory";

/**
 * NEXT STEPS — shown under the evaluation assets once the creation operation has
 * SUCCEEDED: guidance with deep links, plus START EVALUATION (the same
 * `POST /api/eval/runs` New Run sends: agent + created Dataset + created evaluators),
 * AI RECOMMENDATIONS from the first clean run (accepting one re-publishes the Harness
 * as a new version) and a HARNESS CANARY (that new version vs an earlier one) — each
 * billable action behind a confirm.
 */

const MAX_BATCH_EVALUATORS = 10;
const RUN_POLL_MS = 5000;
const AGENT_POLL_MS = 5000;
const RUN_HISTORY_LIMIT = 10;

const meanScore = (run: EvaluationRunInfo) => runMeanScore(run.scores);

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
  const targetId = deployed?.agentId ?? null;

  // The live agent row: accepting a recommendation re-publishes it (status goes
  // deploying → active on a NEW Harness version), so it is read — and polled while
  // deploying — rather than trusted from the creation result.
  const [agent, setAgent] = useState<AgentInfo | null>(null);
  const [agentTick, setAgentTick] = useState(0);
  useEffect(() => {
    if (!targetId) return;
    let live = true;
    api.getAgent(targetId).then((row) => { if (live) setAgent(row); }).catch(() => undefined);
    return () => { live = false; };
  }, [targetId, agentTick]);
  const republishing = agent?.status === "deploying";
  useEffect(() => {
    if (!republishing) return;
    const timer = window.setInterval(() => setAgentTick((n) => n + 1), AGENT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [republishing]);
  const agentReady = agent
    ? agent.status === "active"
    : !!deployed?.agentId && deployed.jobStatus === "succeeded" && deployed.agentStatus === "active";
  const isHarness = (agent?.method ?? "harness") === "harness";

  const runParams = new URLSearchParams({ view: "new" });
  if (targetId) runParams.set("agent", targetId);
  if (datasetId) runParams.set("dataset", datasetId);
  if (evaluators.length) runParams.set("evaluators", evaluators.map((e) => e.id).join(","));
  const runLink = `/v2/eval/tasks?${runParams.toString()}`;

  const readOnly = useReadOnly();
  const canRun = can("eval.run") && !readOnly;
  // StartBatchEvaluation applies at most this many evaluators per run (service limit).
  const tooMany = evaluators.length > MAX_BATCH_EVALUATORS;
  const startable = agentReady && !!targetId && !!datasetId && evaluators.length > 0 && !tooMany;
  const runLive = (runs ?? []).some((r) => !RUN_TERMINAL_STATUSES.has(r.status));
  const startReason = !canRun
    ? t("assistantNext.run.noPermission")
    : !agentReady
      ? t("assistantNext.run.agentNotReady")
      : !datasetId || evaluators.length === 0
        ? t("assistantNext.run.assetsMissing")
        : tooMany
          ? t("assistantNext.run.tooManyEvaluators", { n: evaluators.length, max: MAX_BATCH_EVALUATORS })
          : runLive ? t("assistantNext.run.oneAtATime") : undefined;

  const [removing, setRemoving] = useState<string | null>(null);
  const [reading, setReading] = useState<string | null>(null);
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
  // step 3 may seed from ANY completed evaluator run of this agent — a focused dataset
  // the member ran from 评估任务 included, not only the plan's own dataset
  const [agentRuns, setAgentRuns] = useState<EvaluationRunInfo[] | null>(null);
  useEffect(() => {
    if (!targetId) { setAgentRuns([]); return; }
    let cancelled = false;
    api.listEvaluationRuns({ agent_id: targetId, mode: "evaluators", limit: RUN_HISTORY_LIMIT })
      .then((res) => { if (!cancelled) setAgentRuns(res.runs); })
      .catch(() => { if (!cancelled) setAgentRuns(null); });
    return () => { cancelled = true; };
  }, [targetId, runs]);
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

  // A partial run predating the skipped-session summary: re-checking the batch (reads
  // only) backfills why its sessions were not scored.
  const readDetail = async (run: EvaluationRunInfo) => {
    setReading(run.id);
    try {
      await api.recheckEvaluationRun(run.id);
      toast("success", t("v2.runCoverage.readDetailDone"));
      await loadRuns();
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setReading(null);
    }
  };

  // Recommendations are seeded from one usable completed run with a batch — clean, or
  // one that only lost a minority of sessions to incomplete telemetry — the newest by
  // default; the operator may pick an earlier one, and the other runs' recommendations
  // (an accepted one included) stay listed instead of vanishing behind a newer run.
  const candidateRuns = agentRuns ?? runs ?? [];
  const completedRuns = candidateRuns.filter((r) => runRecommendable(r).ok);
  // why no run qualifies yet, when a partial one exists (newest first)
  const blockedReason = completedRuns.length === 0
    ? candidateRuns.map((r) => runRecommendable(r).reason).find((r) => r && r !== "not_completed") ?? null
    : null;
  const historyCoverage = (runs ?? []).map(runCoverage);
  const [sourceRunId, setSourceRunId] = useState<string | null>(null);
  const baseline = completedRuns.find((r) => r.id === sourceRunId) ?? completedRuns[0] ?? null;
  const otherRuns = completedRuns.filter((r) => r.id !== baseline?.id);
  const baselineMean = baseline ? meanScore(baseline) : null;
  const baselineCoverage = baseline ? runCoverage(baseline) : null;
  const coverageLabel = (r: EvaluationRunInfo) => {
    const c = runCoverage(r);
    return c ? t("assistantNext.recommend.sourceCoverage", { scored: c.scored, total: c.total }) : null;
  };

  let n = 0;
  return (
    <Card title={t("assistantNext.title")} sub={t("assistantNext.sub")} testId="v2-assistant-next">
      <ol className="v2-assistant-next" data-agent-ready={agentReady ? "true" : "false"}>
        <Step
          n={++n}
          testId="v2-assistant-next-chat"
          title={t("assistantNext.chat.title")}
          actions={agentReady && targetId ? (
            <Link className="v2-btn sm primary" to={`/v2/chat?agent=${encodeURIComponent(targetId)}`}>
              {t("assistantNext.chat.open")}
            </Link>
          ) : (
            <Link className="v2-btn sm" to="/v2/agents">{t("assistantNext.agents")}</Link>
          )}
        >
          {agentReady
            ? t("assistantNext.chat.body", { name: agentName })
            : agentFailed
              ? t("assistantNext.chat.failed", { name: agentName })
              : t("assistantNext.chat.waiting", {
                name: agentName,
                status: String(agent?.status ?? deployed?.jobStatus ?? deployed?.agentStatus ?? "—"),
              })}
        </Step>

        <Step
          n={++n}
          testId="v2-assistant-next-run"
          title={t("assistantNext.run.title")}
          actions={
            <>
              {!readOnly && (
                <Button kind="primary" size="sm" disabled={!startable || !canRun || starting || runLive}
                  title={startReason} onClick={() => setConfirmStart(true)} testId="v2-assistant-next-start-run">
                  {starting ? t("assistantNext.run.starting")
                    : (runs?.length ?? 0) > 0 ? t("assistantNext.run.startAgain") : t("assistantNext.run.start")}
                </Button>
              )}
              {!readOnly && <Link className="v2-btn sm" to={runLink}>{t("assistantNext.run.open")}</Link>}
              {!readOnly && startReason && <span className="v2-muted" style={{ fontSize: 12.5 }}>{startReason}</span>}
            </>
          }
        >
          <div>
            {t("assistantNext.run.body", {
              agent: agentName, dataset: datasetRes?.name ?? datasetId, items: datasetItems, n: evaluators.length,
            })}
          </div>
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
              {coverageDiffers(historyCoverage) && (
                <div className="v2-muted" style={{ fontSize: 12.5 }} data-testid="v2-assistant-next-not-comparable">
                  {t("v2.runCoverage.notComparable")}
                </div>
              )}
              {runs.map((run, index) => {
                const live = !RUN_TERMINAL_STATUSES.has(run.status);
                const presentation = evaluationRunPresentation(run);
                const coverage = historyCoverage[index];
                const mean = meanScore(run);
                return (
                  <div key={run.id} className="v2-assistant-sub-row" data-run-status={run.status}>
                    <span className="mono">{t("assistantNext.run.runLine", { id: run.id.slice(0, 8) })}</span>
                    {coverage ? (
                      <RunCoverageTag coverage={coverage} />
                    ) : (
                      <Tag tone={CHIP_TAG[presentation.tone] ?? "gray"}>
                        {t(`expPage.readiness.runStatus.${presentation.status}`)}
                      </Tag>
                    )}
                    {run.created_at && <span className="v2-muted">{fmtTime(run.created_at)}</span>}
                    {run.queue_position != null && live && (
                      <span className="v2-muted">{t("assistantNext.run.queued", { n: run.queue_position })}</span>
                    )}
                    {mean != null && (
                      <span className="v2-muted">
                        {coverage
                          ? t("assistantNext.run.meanScoreCoverage", {
                            mean: mean.toFixed(2), n: run.scores.length, scored: coverage.scored, total: coverage.total,
                          })
                          : t("assistantNext.run.meanScore", { mean: mean.toFixed(2), n: run.scores.length })}
                      </span>
                    )}
                    <Link to={`/v2/eval/tasks?view=detail&id=${encodeURIComponent(run.id)}`}>{t("assistantNext.run.openRuns")} ›</Link>
                    {canRun && (run.status === "failed" || run.status === "stopped") && (
                      <LinkButton danger disabled={removing === run.id} onClick={() => void removeRun(run)}>
                        {t("assistantNext.run.remove")}
                      </LinkButton>
                    )}
                    {coverage ? (
                      <div className={coverageInfo(coverage) ? "err info" : "err warn"}>
                        <RunCoverageNote
                          coverage={coverage}
                          runId={run.id}
                          rawError={run.error}
                          onReadDetail={canRun ? () => void readDetail(run) : undefined}
                          reading={reading === run.id}
                        />
                      </div>
                    ) : run.error && (
                      <div className={presentation.status === "completed_with_errors" ? "err warn" : "err"}>{run.error}</div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {runError && <div style={{ marginTop: 8 }}><Alert tone="error">{runError}</Alert></div>}
        </Step>

        <Step n={++n} testId="v2-assistant-next-recommend" title={t("assistantNext.recommend.title")}>
          <div>{t("assistantNext.recommend.body")}</div>
          {baseline ? (
            <>
              <div className="v2-assistant-sub-row" style={{ marginTop: 6 }}>
                <span>{t("assistantNext.recommend.source")}</span>
                {completedRuns.length > 1 ? (
                  <Select
                    style={{ width: "auto", minWidth: 220 }}
                    value={baseline.id}
                    onChange={setSourceRunId}
                    testId="v2-assistant-next-rec-source"
                    options={completedRuns.map((r, i) => ({
                      value: r.id,
                      label: [
                        t("assistantNext.run.runLine", { id: shortId(r.id) }),
                        r.dataset_id && r.dataset_id !== datasetId ? (r.dataset_name ?? r.dataset_id) : null,
                        r.created_at ? fmtTime(r.created_at) : null,
                        coverageLabel(r),
                        i === 0 ? t("assistantNext.recommend.newest") : null,
                      ].filter(Boolean).join(" · "),
                    }))}
                  />
                ) : (
                  <Tag tone={!baselineCoverage ? "green" : coverageInfo(baselineCoverage) ? "blue" : "orange"}>
                    {[t("assistantNext.run.runLine", { id: shortId(baseline.id) }), coverageLabel(baseline)].filter(Boolean).join(" · ")}
                  </Tag>
                )}
                {baselineMean != null && (
                  <span className="v2-muted">
                    {baselineCoverage
                      ? t("assistantNext.run.meanScoreCoverage", {
                        mean: baselineMean.toFixed(2), n: baseline.scores.length,
                        scored: baselineCoverage.scored, total: baselineCoverage.total,
                      })
                      : t("assistantNext.run.meanScore", { mean: baselineMean.toFixed(2), n: baseline.scores.length })}
                  </span>
                )}
                {agent?.version && <span className="v2-muted">{t("assistantNext.recommend.version", { v: agent.version })}</span>}
              </div>
              {republishing && (
                <div style={{ marginTop: 8 }}><Alert>{t("assistantNext.recommend.publishing", { name: agentName })}</Alert></div>
              )}
              <div style={{ marginTop: 8 }}>
                <RunRecommendations
                  key={baseline.id}
                  run={baseline}
                  embedded
                  readOnly={readOnly}
                  acceptable={isHarness && agentReady}
                  onAccepted={() => setAgentTick((k) => k + 1)}
                />
              </div>
              {otherRuns.length > 0 && <RecommendationHistory runs={otherRuns} onOpen={setSourceRunId} />}
            </>
          ) : (
            <div className="v2-muted" style={{ marginTop: 6 }} data-testid="v2-assistant-next-rec-waiting">
              {blockedReason ? t(`assistantNext.recommend.blocked.${blockedReason}`) : t("assistantNext.recommend.waiting")}
            </div>
          )}
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
          {targetId && isHarness && (
            <HarnessCanary
              agentId={targetId}
              agentName={agentName}
              agentActive={agentReady}
              versionsKey={`${agent?.version ?? ""}:${agent?.status ?? ""}`}
              account={operation.account_id}
              region={operation.region}
            />
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
      <div style={{ marginTop: 12 }}><Alert>{t("assistantNext.noteHarness")}</Alert></div>
      <Confirm
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
        onConfirm={() => { setConfirmStart(false); void startRun(); }}
        onClose={() => setConfirmStart(false)}
      />
    </Card>
  );
}
