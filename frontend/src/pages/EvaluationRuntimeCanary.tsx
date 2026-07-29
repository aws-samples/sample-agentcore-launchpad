import type { CSSProperties } from "react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Activity,
  ArrowRight,
  Play,
  RotateCcw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { Btn, Chip, ConfirmDialog, Panel, StageCard, useToast } from "../components";
import type { AgentInfo, RuntimeCanaryInfo } from "../lib/api";
import { api } from "../lib/api";
import { fmtScore } from "../lib/format";

interface DatasetInfo {
  id: string;
  name: string;
  kind: string;
  item_count: number;
}

const RAMP_STAGES = [
  { control: 90, treatment: 10 },
  { control: 50, treatment: 50 },
  { control: 1, treatment: 99 },
] as const;

type ConfirmState =
  | { action: "advance" | "complete"; allowNonSignificant: true }
  | { action: "rollback" | "cleanup" }
  | null;

function canaryTone(
  status: RuntimeCanaryInfo["status"],
): "good" | "warn" | "crit" | "muted" {
  if (status === "running") return "warn";
  if (status === "cleaned") return "muted";
  if (status === "rolled_back") return "crit";
  return "good";
}

function verdictTone(verdict: string): "good" | "warn" | "crit" | "muted" {
  if (verdict === "treatment-wins") return "good";
  if (verdict === "control-wins") return "crit";
  if (verdict.includes("insufficient") || verdict === "tie") return "warn";
  return "muted";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function RuntimeCanaryView() {
  const { t } = useTranslation();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [canaries, setCanaries] = useState<RuntimeCanaryInfo[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [agentId, setAgentId] = useState("");
  const [candidatePrompt, setCandidatePrompt] = useState("");
  const [candidateCode, setCandidateCode] = useState("");
  const [sourceExperimentId, setSourceExperimentId] = useState("");
  const [trafficDatasetId, setTrafficDatasetId] = useState("");
  const [busy, setBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await api.listRuntimeCanaries();
      setCanaries(result.canaries);
    } catch {
      /* backend offline */
    }
  }, []);

  const canaryParam = searchParams.get("canary");
  const handoffChampionId = searchParams.get("champion") ?? "";
  const handoffSourceExperimentId = searchParams.get("sourceExp") ?? "";
  const creatingNew = canaryParam === "new";
  const canary = creatingNew
    ? null
    : (canaries.find((row) => row.id === canaryParam) ?? canaries[0] ?? null);

  const selectCanary = (id: string | null) => {
    setSearchParams(
      id
        ? { view: "experiment", mode: "canary", canary: id }
        : { view: "experiment", mode: "canary" },
    );
  };

  useEffect(() => {
    if (creatingNew) {
      setAgentId(handoffChampionId);
      setSourceExperimentId(handoffSourceExperimentId);
    }
    setTrafficDatasetId("");
    setCreateError(null);
    setConfirm(null);
  }, [
    canaryParam,
    creatingNew,
    handoffChampionId,
    handoffSourceExperimentId,
  ]);

  useEffect(() => {
    api
      .listAgents()
      .then((result) => {
        const active = result.agents.filter((agent) => agent.status === "active");
        setAgents(active);
        const firstEligible = active.find((agent) => agent.canary_capability.eligible);
        setAgentId((current) => current || firstEligible?.id || "");
      })
      .catch(() => {});
    fetch("/api/eval/datasets")
      .then((response) => (response.ok ? response.json() : { datasets: [] }))
      .then((body: { datasets: DatasetInfo[] }) =>
        setDatasets(body.datasets.filter((dataset) => dataset.kind !== "simulated")))
      .catch(() => {});
    void refresh();
    const timer = setInterval(() => void refresh(), 8000);
    return () => clearInterval(timer);
  }, [refresh]);

  const runningAction = canary?.running_action ?? null;
  useEffect(() => {
    if (!runningAction) return;
    const timer = setInterval(() => void refresh(), 2500);
    return () => clearInterval(timer);
  }, [refresh, runningAction]);

  const selectedAgent = agents.find((agent) => agent.id === agentId) ?? null;
  const isStudio = selectedAgent?.method === "studio";

  // Prefill the candidate edit from the selected agent's current spec. Keyed on
  // the selected-agent object (an agent switch re-seeds) — typing changes
  // neither agentId nor the agents array, so drafts survive the canary refresh
  // interval (which never reloads agents).
  useEffect(() => {
    if (!creatingNew) return;
    const spec = (selectedAgent?.spec ?? {}) as {
      system_prompt?: string;
      code?: string;
    };
    setCandidatePrompt(typeof spec.system_prompt === "string" ? spec.system_prompt : "");
    setCandidateCode(typeof spec.code === "string" ? spec.code : "");
  }, [creatingNew, selectedAgent]);

  const candidateHasEdit =
    !!candidatePrompt.trim() || (!!isStudio && !!candidateCode.trim());

  const onCreate = async () => {
    setCreateError(null);
    setBusy(true);
    try {
      const candidate: { system_prompt?: string; code?: string } = {};
      if (candidatePrompt.trim()) candidate.system_prompt = candidatePrompt;
      if (isStudio && candidateCode.trim()) candidate.code = candidateCode;
      const row = await api.createRuntimeCanary({
        agent_id: agentId,
        candidate,
        ...(sourceExperimentId
          ? { source_experiment_id: sourceExperimentId }
          : {}),
      });
      await refresh();
      selectCanary(row.id);
    } catch (error) {
      setCreateError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const onAction = async (
    action: string,
    extra: { dataset_id?: string; allow_non_significant?: boolean } = {},
  ) => {
    if (!canary) return;
    setBusy(true);
    try {
      const result = await api.runtimeCanaryAction(canary.id, {
        action,
        ...extra,
      });
      setCanaries((current) =>
        current.map((row) => row.id === result.canary.id ? result.canary : row));
      void refresh();
    } catch (error) {
      toast(t("common.actionFailed", { msg: errorMessage(error) }));
    } finally {
      setBusy(false);
    }
  };

  const actionBtn = (
    action: string,
    label: string,
    options: {
      primary?: boolean;
      disabled?: boolean;
      extra?: { dataset_id?: string; allow_non_significant?: boolean };
      icon?: "play" | "advance" | "rollback" | "cleanup";
    } = {},
  ) => {
    if (!canary) return null;
    const running = canary.running_action === action;
    const failed = !running && !!canary.error?.startsWith(`${action}: `);
    const Icon = options.icon === "advance"
      ? ArrowRight
      : options.icon === "rollback"
        ? RotateCcw
        : options.icon === "cleanup"
          ? Trash2
          : Play;
    return (
      <div>
        <Btn
          primary={options.primary && !failed}
          disabled={busy || !!canary.running_action || options.disabled}
          data-testid={`canary-action-${action}`}
          onClick={() => void onAction(action, options.extra)}
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
        >
          {running ? <Activity size={14} /> : <Icon size={14} />}
          {running
            ? t("canaryPage.running")
            : failed
              ? t("canaryPage.retry")
              : label}
        </Btn>
        {running && (
          <div className="mono dim" data-testid="canary-progress"
               style={{ fontSize: 10, marginTop: 4 }}>
            {canary.progress ?? "…"}
          </div>
        )}
        {failed && (
          <div className="note" style={{ borderColor: "var(--crit)", marginTop: 6 }}>
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span className="mono" style={{ fontSize: 10.5 }}>{canary.error}</span>
          </div>
        )}
      </div>
    );
  };

  const eligibleAgents = agents.filter((agent) => agent.canary_capability.eligible);
  const unsupportedAgents = agents.filter((agent) => !agent.canary_capability.eligible);
  const capReason = (cap: AgentInfo["canary_capability"]) =>
    cap.reason_code ? t(`canaryPage.reason.${cap.reason_code}`) : cap.reason;
  const setup = canary?.artifacts.setup;
  const rounds = canary?.artifacts.rounds ?? [];
  const currentStage = setup?.ramp_stage ?? 0;
  const terminal = canary?.status !== "running";

  const createForm = (
    <>
      {sourceExperimentId && (
        <div className="note" style={{ marginBottom: 10 }} data-testid="canary-handoff-note">
          <span className="i">[i]</span>
          <span>{t("canaryPage.handoffSource", { id: sourceExperimentId })}</span>
        </div>
      )}
      <div className="field">
        <label>{t("canaryPage.agent")}</label>
        <select
          className="input"
          value={agentId}
          data-testid="canary-agent-select"
          onChange={(event) => {
            const nextAgent = event.target.value;
            setAgentId(nextAgent);
            if (sourceExperimentId && nextAgent !== handoffChampionId) {
              setSourceExperimentId("");
            }
          }}
        >
          <option value="">{t("canaryPage.pickAgent")}</option>
          {eligibleAgents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.name} · {agent.method}
            </option>
          ))}
          {unsupportedAgents.map((agent) => (
            <option key={agent.id} value={agent.id} disabled>
              {agent.name} · {agent.method} — {capReason(agent.canary_capability)}
            </option>
          ))}
        </select>
      </div>
      <div className="note" style={{ marginBottom: 10 }}>
        <span className="i">[i]</span>
        <span>{t("canaryPage.candidateHint")}</span>
      </div>
      <div className="field">
        <label>{t("canaryPage.candidatePrompt")}</label>
        <textarea
          className="input mono"
          rows={7}
          style={{ fontSize: 11, lineHeight: 1.5, resize: "vertical" }}
          value={candidatePrompt}
          data-testid="canary-candidate-prompt"
          placeholder={t("canaryPage.candidatePromptPlaceholder")}
          onChange={(event) => setCandidatePrompt(event.target.value)}
        />
      </div>
      {isStudio && (
        <div className="field">
          <label>{t("canaryPage.candidateCode")}</label>
          <textarea
            className="input mono"
            rows={9}
            style={{ fontSize: 11, lineHeight: 1.5, resize: "vertical" }}
            value={candidateCode}
            data-testid="canary-candidate-code"
            placeholder={t("canaryPage.candidateCodePlaceholder")}
            onChange={(event) => setCandidateCode(event.target.value)}
          />
        </div>
      )}
      {unsupportedAgents.length > 0 && (
        <div className="note" style={{ marginBottom: 10 }}>
          <span className="i">[i]</span>
          <span>{t("canaryPage.eligibilityHint")}</span>
        </div>
      )}
      {createError && (
        <div className="note" style={{ borderColor: "var(--crit)", marginBottom: 10 }}>
          <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
          <span>{createError}</span>
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Btn
          primary
          disabled={busy || !agentId || !candidateHasEdit}
          data-testid="create-runtime-canary"
          onClick={() => void onCreate()}
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
        >
          <Play size={14} />
          {t("canaryPage.create")}
        </Btn>
      </div>
    </>
  );

  return (
    <>
      <Panel
        brk
        pad={false}
        title={t("canaryPage.list.title")}
        sub={t("canaryPage.list.sub")}
        end={
          <Btn
            primary
            data-testid="new-runtime-canary"
            onClick={() => selectCanary("new")}
          >
            + {t("canaryPage.list.new")}
          </Btn>
        }
        style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
      >
        <div style={{ overflowX: "auto" }}>
          <table style={{ minWidth: 720 }}>
            <thead>
              <tr>
                <th>{t("canaryPage.list.name")}</th>
                <th>{t("canaryPage.list.agent")}</th>
                <th>{t("canaryPage.list.versions")}</th>
                <th>{t("canaryPage.list.weights")}</th>
                <th>{t("canaryPage.list.created")}</th>
                <th>{t("canaryPage.list.status")}</th>
              </tr>
            </thead>
            <tbody>
              {canaries.map((row) => (
                <tr
                  key={row.id}
                  data-testid={`runtime-canary-row-${row.id}`}
                  onClick={() => selectCanary(row.id)}
                  style={{
                    cursor: "pointer",
                    background: !creatingNew && canary?.id === row.id
                      ? "rgba(255,176,0,.045)"
                      : undefined,
                  }}
                >
                  <td className="pri">{row.name}</td>
                  <td>{row.champion_agent_name}</td>
                  <td className="mono dim">
                    {row.artifacts.setup
                      ? `v${row.artifacts.setup.v_current} → v${
                        row.artifacts.setup.v_candidate}`
                      : "—"}
                  </td>
                  <td className="mono dim">
                    {row.artifacts.setup
                      ? `${row.artifacts.setup.weights.C}/${
                        row.artifacts.setup.weights.T1}`
                      : "—"}
                  </td>
                  <td className="mono dim">
                    {row.created_at ? new Date(row.created_at).toLocaleString() : "—"}
                  </td>
                  <td>
                    <Chip
                      tone={canaryTone(row.status)}
                      icon={row.status === "running" ? "◐" : "●"}
                    >
                      {row.status.toUpperCase()}
                    </Chip>
                  </td>
                </tr>
              ))}
              {canaries.length === 0 && (
                <tr>
                  <td colSpan={6} className="dim mono" style={{ textAlign: "center" }}>
                    {t("canaryPage.list.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>

      <div className="eval-grid">
        <Panel
          brk
          title={canary ? canary.name : t("canaryPage.create")}
          sub={canary ? t("canaryPage.sub") : t("canaryPage.createHint")}
          end={canary && (
            <Chip tone={canaryTone(canary.status)}
                  icon={canary.status === "running" ? "◐" : "●"}>
              {canary.stage.toUpperCase()} · {canary.status.toUpperCase()}
            </Chip>
          )}
          style={{ "--i": 1 } as CSSProperties}
        >
          {!canary && createForm}

          {canary && (
            <>
              <div className="note" style={{ marginBottom: 10 }}>
                <span className="i">[i]</span>
                <span>{t("canaryPage.experimentalOnly")}</span>
              </div>
              {setup && (
                <div className="note" style={{ marginBottom: 10 }}
                     data-testid="canary-version-framing">
                  <span className="i">[⇄]</span>
                  <span>{t("canaryPage.versionFraming", {
                    current: setup.v_current,
                    candidate: setup.v_candidate,
                  })}</span>
                </div>
              )}
              {canary.error && ["advance:", "complete:"]
                .some((prefix) => canary.error?.startsWith(prefix)) && (
                <div className="note" style={{ borderColor: "var(--crit)",
                                               marginBottom: 10 }}>
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span className="mono" style={{ fontSize: 10.5 }}>
                    {canary.error}
                  </span>
                </div>
              )}

              <StageCard
                id="canary-setup"
                index={1}
                title={t("canaryPage.stage.setup")}
                active={!setup && !terminal}
                done={!!setup}
              >
                {!setup && !terminal &&
                  actionBtn("setup", t("canaryPage.setup"), {
                    primary: true,
                    icon: "play",
                  })}
                {setup && (
                  <div className="mono dim" style={{ fontSize: 10 }}>
                    v{setup.v_current} → v{setup.v_candidate}
                    <br />
                    gw {setup.gateway_id} · ab {setup.ab_test_id}
                    <br />
                    {setup.champion.target_name} ↔ {setup.challenger.target_name}
                  </div>
                )}
              </StageCard>

              {RAMP_STAGES.map((ramp, index) => {
                const reached = !!setup && index <= currentStage;
                const current = !!setup && index === currentStage;
                const round = rounds.find((item) => item.ramp_stage === index);
                const attempts = round?.traffic_attempts ?? [];
                const verdict = round?.verdict;
                const blocked = !!verdict && (
                  verdict.verdict === "control-wins"
                  || verdict.verdict === "insufficient-data"
                  || verdict.verdict === "insufficient-n"
                );
                const needsOverride = !blocked && !!verdict && (
                  verdict.verdict === "tie" || verdict.significant === false
                );
                const action = index === RAMP_STAGES.length - 1
                  ? "complete"
                  : "advance";
                return (
                  <StageCard
                    key={index}
                    id={`canary-ramp-${index}`}
                    index={index + 2}
                    title={t("canaryPage.stage.ramp", {
                      control: ramp.control,
                      treatment: ramp.treatment,
                    })}
                    active={current && canary.status === "running"}
                    done={index < currentStage
                      || (index === 2 && !!canary.artifacts.complete)}
                  >
                    {!reached && (
                      <div className="mono dim" style={{ fontSize: 10 }}>
                        {t("canaryPage.stage.locked")}
                      </div>
                    )}
                    {reached && (
                      <>
                        <div className="split" style={{ marginBottom: 4 }}>
                          <div style={{
                            flex: `0 0 ${ramp.control}%`,
                            background: "var(--s1)",
                          }} />
                          <div style={{ flex: 1, background: "var(--s3)" }} />
                        </div>
                        <div className="mono dim" style={{ fontSize: 9.5 }}>
                          {t("canaryPage.experimentalWeights", {
                            control: ramp.control,
                            treatment: ramp.treatment,
                          })}
                        </div>
                        {attempts.length > 0 && (
                          <div className="mono dim" style={{ fontSize: 10, marginTop: 7 }}>
                            {t("canaryPage.trafficEvidence", {
                              attempts: attempts.length,
                              sent: attempts.reduce((sum, item) => sum + item.sent, 0),
                              failed: attempts.reduce((sum, item) => sum + item.failed, 0),
                              baseline: attempts[attempts.length - 1].baseline_n,
                            })}
                          </div>
                        )}
                        {verdict && (
                          <div style={{ marginTop: 8 }}>
                            <Chip
                              tone={verdictTone(verdict.verdict)}
                              icon={verdict.verdict === "treatment-wins" ? "✓" : "!"}
                            >
                              {verdict.verdict.toUpperCase()} · n={verdict.n ?? 0}
                              {verdict.significant === false
                                ? ` · ${t("canaryPage.notSignificant")}`
                                : ""}
                            </Chip>
                            {verdict.metrics?.length > 0 && (
                              <div className="code" style={{ marginTop: 6, maxHeight: 110,
                                                            overflowY: "auto" }}>
                                {verdict.metrics.map((metric) => {
                                  const treatment = metric.variants[0];
                                  return `${metric.label}: C ${
                                    fmtScore(metric.control.mean)} (n=${
                                    metric.control.sampleSize ?? "—"}) · T ${
                                    fmtScore(treatment?.mean)} (n=${
                                    treatment?.sampleSize ?? "—"})`;
                                }).join("\n")}
                              </div>
                            )}
                          </div>
                        )}
                        {current && canary.status === "running" && (
                          <div style={{ display: "flex", gap: 8, flexWrap: "wrap",
                                        marginTop: 9, alignItems: "flex-start" }}>
                            <select
                              className="input"
                              style={{ maxWidth: 230 }}
                              value={trafficDatasetId}
                              data-testid="canary-traffic-dataset"
                              onChange={(event) => setTrafficDatasetId(event.target.value)}
                            >
                              <option value="">{t("canaryPage.builtinPrompts")}</option>
                              {datasets.map((dataset) => (
                                <option key={dataset.id} value={dataset.id}>
                                  {dataset.name} ({dataset.item_count})
                                </option>
                              ))}
                            </select>
                            {actionBtn(
                              "traffic",
                              attempts.length
                                ? t("canaryPage.sendMoreTraffic")
                                : t("canaryPage.sendTraffic"),
                              {
                                primary: attempts.length === 0,
                                extra: trafficDatasetId
                                  ? { dataset_id: trafficDatasetId }
                                  : undefined,
                                icon: "play",
                              },
                            )}
                            {attempts.length > 0 && !verdict &&
                              actionBtn("verdict", t("canaryPage.recordVerdict"), {
                                primary: true,
                                icon: "play",
                              })}
                            {verdict && !blocked && (
                              canary.running_action === action
                                || canary.error?.startsWith(`${action}: `)
                                ? actionBtn(
                                  action,
                                  action === "complete"
                                    ? t("canaryPage.complete")
                                    : t("canaryPage.advance"),
                                  {
                                    primary: !needsOverride,
                                    extra: needsOverride
                                      ? { allow_non_significant: true }
                                      : undefined,
                                    icon: "advance",
                                  },
                                )
                                : (
                                  <Btn
                                    primary={!needsOverride}
                                    disabled={busy || !!canary.running_action}
                                    data-testid={`canary-action-${action}`}
                                    onClick={() => {
                                      if (needsOverride) {
                                        setConfirm({
                                          action,
                                          allowNonSignificant: true,
                                        });
                                      } else {
                                        void onAction(action);
                                      }
                                    }}
                                    style={{
                                      display: "inline-flex",
                                      alignItems: "center",
                                      gap: 6,
                                    }}
                                  >
                                    {action === "complete"
                                      ? <ShieldCheck size={14} />
                                      : <ArrowRight size={14} />}
                                    {action === "complete"
                                      ? t("canaryPage.complete")
                                      : t("canaryPage.advance")}
                                  </Btn>
                                )
                            )}
                          </div>
                        )}
                        {blocked && current && (
                          <div className="note" style={{ borderColor: "var(--crit)",
                                                        marginTop: 8 }}>
                            <span className="i" style={{ color: "var(--crit)" }}>[!]</span>
                            <span>{t("canaryPage.blocked", {
                              verdict: verdict?.verdict,
                            })}</span>
                          </div>
                        )}
                        {needsOverride && current && (
                          <div className="note" style={{ borderColor: "var(--warn)",
                                                        marginTop: 8 }}>
                            <span className="i" style={{ color: "var(--warn)" }}>[!]</span>
                            <span>{t("canaryPage.overrideHint")}</span>
                          </div>
                        )}
                      </>
                    )}
                  </StageCard>
                );
              })}

              {(canary.artifacts.complete || canary.artifacts.rollback) && (
                <div className="note" style={{ marginBottom: 10 }}
                     data-testid="canary-terminal-summary">
                  <span className="i">
                    [{canary.artifacts.complete ? "✓" : "!"}]
                  </span>
                  <span>
                    {canary.artifacts.complete
                      ? t("canaryPage.completedSummary", {
                        version: canary.artifacts.complete.promoted_version ?? "—",
                      })
                      : t("canaryPage.rollbackSummary", {
                        version: canary.artifacts.rollback?.restored_version ?? "—",
                      })}
                  </span>
                </div>
              )}

              <div style={{ borderTop: "1px solid var(--line)", paddingTop: 10,
                            display: "flex", gap: 8, flexWrap: "wrap",
                            alignItems: "flex-start" }}>
                {setup && canary.status === "running"
                  && canary.running_action !== "rollback"
                  && !canary.error?.startsWith("rollback:") && (
                  <Btn
                    disabled={busy || !!canary.running_action}
                    data-testid="canary-rollback"
                    onClick={() => setConfirm({ action: "rollback" })}
                    style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                  >
                    <RotateCcw size={14} />
                    {t("canaryPage.rollback")}
                  </Btn>
                )}
                {!canary.artifacts.cleanup
                  && canary.running_action !== "cleanup"
                  && !canary.error?.startsWith("cleanup:") && (
                  <Btn
                    disabled={busy || !!canary.running_action}
                    data-testid="canary-cleanup"
                    onClick={() => setConfirm({ action: "cleanup" })}
                    style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                  >
                    <Trash2 size={14} />
                    {t("canaryPage.cleanup")}
                  </Btn>
                )}
                {(canary.running_action === "rollback"
                  || canary.error?.startsWith("rollback:"))
                  && actionBtn("rollback", t("canaryPage.rollback"), {
                    icon: "rollback",
                  })}
                {(canary.running_action === "cleanup"
                  || canary.error?.startsWith("cleanup:"))
                  && actionBtn("cleanup", t("canaryPage.cleanup"), {
                    icon: "cleanup",
                  })}
              </div>
              {canary.artifacts.cleanup && (
                <div className="code" style={{ marginTop: 10, maxHeight: 140,
                                              overflowY: "auto" }}>
                  {canary.artifacts.cleanup
                    .map((row) => `${row.status.padEnd(8)} ${row.category}`)
                    .join("\n")}
                </div>
              )}

              <ConfirmDialog
                open={confirm !== null}
                title={confirm?.action === "cleanup"
                  ? t("canaryPage.confirmCleanup.title")
                  : confirm?.action === "rollback"
                    ? t("canaryPage.confirmRollback.title")
                    : t("canaryPage.confirmOverride.title")}
                body={confirm?.action === "cleanup"
                  ? t("canaryPage.confirmCleanup.body")
                  : confirm?.action === "rollback"
                    ? t("canaryPage.confirmRollback.body")
                    : t("canaryPage.confirmOverride.body")}
                confirmLabel={confirm?.action === "cleanup"
                  ? t("canaryPage.cleanup")
                  : confirm?.action === "rollback"
                    ? t("canaryPage.rollback")
                    : confirm?.action === "complete"
                      ? t("canaryPage.complete")
                      : t("canaryPage.advance")}
                onConfirm={() => {
                  const pending = confirm;
                  setConfirm(null);
                  if (!pending) return;
                  void onAction(
                    pending.action,
                    "allowNonSignificant" in pending
                      ? { allow_non_significant: true }
                      : {},
                  );
                }}
                onCancel={() => setConfirm(null)}
              />
            </>
          )}
        </Panel>

        <Panel
          title={t("canaryPage.how.title")}
          sub={t("canaryPage.how.sub")}
          style={{ "--i": 2 } as CSSProperties}
        >
          {(["setup", "ramp90", "ramp50", "ramp99", "finish"] as const).map(
            (stage, index) => (
              <div className="kv" key={stage}>
                <span className="k mono">{String(index + 1).padStart(2, "0")}</span>
                <span className="v" style={{ textAlign: "left", flex: 1, marginLeft: 12 }}>
                  {t(`canaryPage.how.${stage}`)}
                </span>
              </div>
            ),
          )}
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("canaryPage.how.note")}</span>
          </div>
        </Panel>
      </div>
    </>
  );
}
