import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type RuntimeCanaryInfo } from "../../../lib/api";
import { ONLINE_EVAL_DEFAULT } from "../../../lib/canary";
import { creatorLabel, fmtScore, fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { useMayMutate } from "../../ownership";
import { Alert, Button, Card, Confirm, Descriptions, Field, FlowHeader, Select, Spin, Table, Tag } from "../../ui";
import { StageCard } from "./StageCard";
import {
  CANARY_TONE,
  completionSummary,
  insufficientArmHint,
  multipleComparisonsNote,
  progressText,
  RAMP_STAGES,
  remainingPerArm,
  SESSIONS_PER_ARM_MAX,
  verdictGate,
  verdictLabel,
  verdictReason,
  verdictTone,
  versionsLabel,
  weightsLabel,
} from "./common";
import { evaluatorIdOf, evaluatorName } from "./evaluatorChoice";
import { useEvaluatorNames } from "../../useEvaluatorNames";

const POLL_MS = 8000;
const FAST_POLL_MS = 2500;

type Pending =
  | { action: "advance" | "complete"; allowNonSignificant: true }
  | { action: "rollback" | "cleanup" };

type Round = NonNullable<RuntimeCanaryInfo["artifacts"]["rounds"]>[number];
type Metric = NonNullable<Round["verdict"]>["metrics"][number];

export function CanaryDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const mayMutate = useMayMutate();
  const [canary, setCanary] = useState<RuntimeCanaryInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<Pending | null>(null);
  const datasetsLoad = useLoad(() => api.v2Datasets(), "datasets");
  const datasets = useMemo(() => (datasetsLoad.data?.datasets ?? []).filter((d) => d.kind !== "simulated"), [datasetsLoad.data]);
  const [dataset, setDataset] = useState("");
  // N17-obs427: the traffic round's sessions_per_arm — "" until edited (the design's
  // remaining planned n is the default), cleared = one dataset pass
  const [perArm, setPerArm] = useState<string | null>(null);
  // custom judge names for the evaluator summary and the metric tables (N12-obs46)
  const evaluatorsLoad = useLoad(() => api.v2Evaluators(), "evaluators");
  const evaluatorRows = evaluatorsLoad.data?.evaluators;

  useEffect(() => {
    setDataset((prev) => (datasets.some((d) => d.id === prev) ? prev : (datasets[0]?.id ?? "")));
  }, [datasets]);

  const refresh = useCallback(async () => {
    try {
      setCanary(await api.getRuntimeCanary(id));
      setLoadError(null);
    } catch (err) {
      setLoadError(errorMessage(err));
    }
  }, [id]);

  const running = canary?.running_action ?? null;
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), running ? FAST_POLL_MS : POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh, running]);

  const onAction = async (action: string, extra: { dataset_id?: string; allow_non_significant?: boolean; sessions_per_arm?: number } = {}) => {
    setBusy(true);
    try {
      const res = await api.runtimeCanaryAction(id, { action, ...extra });
      setCanary(res.canary);
      if (action === "traffic") setPerArm(null); // the next round re-derives what is left
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
      void refresh();
    }
  };

  if (!canary) {
    return loadError ? (
      <>
        <FlowHeader title={id} onBack={() => setParams({ mode: "canary" })} />
        <Alert tone="error">{loadError}</Alert>
      </>
    ) : (
      <Spin />
    );
  }

  const a = canary.artifacts;
  const summary = completionSummary(canary, t); // N17-obs451
  // N17-obs452: setup moved production's DEFAULT to the candidate — complete or roll back first
  const cleanupNeedsDecision = canary.status === "running" && !!a.setup?.v_candidate;
  // creator-or-admin on every action except `verdict` (server rule `canary.forbidden`,
  // r13-g): a member who did not start this canary sees the actions disabled with a hint
  const owner = mayMutate(canary);
  const locked = busy || !!canary.running_action;
  const ownerLocked = (action: string) => !owner && action !== "verdict";
  // setup is persisted PARTIALLY (gateway + stable endpoint) as soon as provisioning
  // starts; only an A/B test id makes it live
  const setup = a.setup;
  const liveSetup = setup?.ab_test_id ? setup : undefined;
  const provisioning = !!setup && !liveSetup;
  const rounds = a.rounds ?? [];
  const currentStage = liveSetup?.ramp_stage ?? 0;
  const terminal = canary.status !== "running";
  // the stage `complete` left from: the final one unless recorded otherwise
  const completedStage = a.complete?.completed_at_stage ?? RAMP_STAGES.length - 1;
  // the evaluators both arms are scored by; a row from before the choice used the default pair
  const configured = a.online_evaluators ?? setup?.online_evaluators ?? null;
  const primaryEvaluator = a.primary_evaluator ?? setup?.primary_evaluator ?? null;
  // configured evaluators the most recent verdict got no score on both versions from
  const latestUnscored = new Set([...rounds].reverse().find((r) => r.verdict)?.verdict?.unscored ?? []);
  const nameOf = (id: string) => evaluatorName(t, evaluatorRows, id);
  const namesOf = (ids: string[]) => ids.map(nameOf).join(t("v2.canary.eval.sep"));
  const metricId = (m: Metric) => evaluatorIdOf(m.evaluatorId ?? m.label);
  /** N17-obs446: custom judges by name, built-ins by their localized label (the stored label is
   *  the bare id, e.g. `Builtin.Helpfulness`) — the id is the cell's tooltip */
  const metricName = (m: Metric) => nameOf(metricId(m));
  const metricCell = (m: Metric, primary: string | undefined) => (
    <span className="v2-row" title={metricId(m)}>
      {metricName(m)}
      {primary && metricId(m) === primary && <Tag tone="green">{t("v2.canary.eval.primaryTag")}</Tag>}
    </span>
  );

  /** Button while pending → progress line; a stored `<action>: …` error turns it into a retry. */
  const actionButton = (
    action: string,
    label: string,
    opts: { primary?: boolean; disabled?: boolean; extra?: { dataset_id?: string; allow_non_significant?: boolean; sessions_per_arm?: number } } = {},
  ) => {
    const isRunning = canary.running_action === action;
    const failed = !isRunning && !!canary.error?.startsWith(`${action}: `);
    return (
      <div className="v2-stack">
        <div>
          <Button
            kind={opts.primary && !failed ? "primary" : undefined}
            disabled={locked || opts.disabled || ownerLocked(action)}
            title={ownerLocked(action) ? t("v2.common.creatorOnly") : undefined}
            onClick={() => void onAction(action, opts.extra)}
            testId={`v2-canary-action-${action}`}
          >
            {isRunning ? t("canaryPage.running") : failed ? t("canaryPage.retry") : label}
          </Button>
        </div>
        {ownerLocked(action) && !isRunning && <span className="v2-muted">{t("v2.common.creatorOnly")}</span>}
        {isRunning && <span className="v2-muted">{progressText(canary, t) ?? "…"}</span>}
        {failed && <Alert tone="error">{canary.error}</Alert>}
      </div>
    );
  };

  const metricsTable = (metrics: Metric[], primary?: string) => (
    <Table
      columns={[
        { key: "m", title: t("v2.evaluators.colName"), render: (m: Metric) => metricCell(m, primary) },
        {
          key: "c",
          title: t("v2.canary.champion"),
          render: (m: Metric) => `${fmtScore(m.control.mean)} (n=${m.control.sampleSize ?? "—"})`,
        },
        {
          key: "v",
          title: t("v2.canary.candidate"),
          render: (m: Metric) => `${fmtScore(m.variants[0]?.mean ?? null)} (n=${m.variants[0]?.sampleSize ?? "—"})`,
        },
      ]}
      rows={metrics}
      rowKey={(m) => m.label}
      density="dense"
    />
  );

  const rampCard = (index: number) => {
    const ramp = RAMP_STAGES[index];
    const reached = !!liveSetup && index <= currentStage;
    const current = !!liveSetup && index === currentStage;
    const round = rounds.find((r) => r.ramp_stage === index);
    const attempts = round?.traffic_attempts ?? [];
    const verdict = round?.verdict;
    const action = index === RAMP_STAGES.length - 1 ? "complete" : "advance";
    const { blocked, needsOverride } = verdictGate(verdict, action);
    const armHint = insufficientArmHint(verdict, t, action);
    const reason = verdictReason(verdict, t, nameOf);
    const done = index < currentStage || (index === completedStage && !!a.complete);
    const advanceLabel = action === "complete" ? t("canaryPage.complete") : t("canaryPage.advance");
    const state = done ? "done" : current && canary.status === "running" ? "active" : "pending";
    const defaultPerArm = remainingPerArm(a.design, round);
    const perArmText = perArm ?? (defaultPerArm != null ? String(defaultPerArm) : "");
    const perArmValue = Number(perArmText);
    const perArmValid = perArmText === "" || (Number.isInteger(perArmValue) && perArmValue >= 1 && perArmValue <= SESSIONS_PER_ARM_MAX);

    return (
      <StageCard
        key={index}
        id={`ramp-${index}`}
        index={index + 2}
        title={t("canaryPage.stage.ramp", { control: ramp.control, treatment: ramp.treatment })}
        state={state}
      >
        {!reached ? (
          <span className="v2-muted">{t("canaryPage.stage.locked")}</span>
        ) : (
          <div className="v2-form">
            <div className="v2-stack">
              <div className="v2-split">
                <span style={{ flex: `0 0 ${ramp.control}%` }} className="c" />
                <span style={{ flex: 1 }} className="t" />
              </div>
              <span className="v2-muted">
                {t("canaryPage.experimentalWeights", { control: ramp.control, treatment: ramp.treatment })}
              </span>
            </div>
            {attempts.length > 0 && (
              <Descriptions
                items={[
                  { label: t("v2.canary.attempts"), value: attempts.length },
                  { label: t("v2.experiments.sent"), value: attempts.reduce((sum, x) => sum + x.sent, 0) },
                  { label: t("v2.experiments.failed"), value: attempts.reduce((sum, x) => sum + x.failed, 0) },
                  { label: t("v2.canary.baseline"), value: attempts[attempts.length - 1].baseline_n },
                ]}
              />
            )}
            {verdict && (
              <>
                <div className="v2-row">
                  <Tag
                    tone={verdictTone(verdict.verdict)}
                    title={t("canaryVerdict.rawCode", { code: verdict.verdict })}
                    testId={`v2-canary-verdict-${index}`}
                  >
                    {verdictLabel(verdict.verdict, t)}
                  </Tag>
                  <span className="v2-muted">n={verdict.n ?? 0}</span>
                  {verdict.significant === false && <Tag tone="gray">{t("canaryPage.notSignificant")}</Tag>}
                  {verdict.primary ? (
                    <span className="v2-muted" data-testid="v2-canary-verdict-primary">
                      {t("v2.canary.eval.decidedBy", { primary: nameOf(verdict.primary) })}
                    </span>
                  ) : (
                    verdict.evaluators && <span className="v2-muted">{t("v2.canary.eval.aggregate")}</span>
                  )}
                </div>
                {armHint && (
                  <div className="v2-muted" title={t("canaryPage.armKeysHint")} data-testid={`v2-canary-arm-hint-${index}`}>
                    {armHint}
                  </div>
                )}
                {!armHint && reason && (verdict.verdict.startsWith("insufficient") || verdict.verdict === "judge-mismatch") && (
                  <div className="v2-muted" data-testid={`v2-canary-reason-${index}`}>{reason}</div>
                )}
                {verdict.design && (
                  <div className="v2-muted" data-testid={`v2-canary-design-${index}`}>
                    {t("v2.canary.design.fixedN", { n: verdict.design.planned_n_per_arm })}
                    {verdict.p_value !== undefined && ` · ${t("v2.canary.design.pValue", { p: verdict.p_value })}`}
                    {verdict.frozen && ` · ${t("v2.canary.design.frozen")}`}
                  </div>
                )}
                {verdict.verdict === "tie" && reason && (
                  <div className="v2-muted" data-testid={`v2-canary-reason-${index}`}>{reason}</div>
                )}
                {verdict.held_out && (
                  <div className="v2-muted" data-testid={`v2-canary-held-out-${index}`}>
                    {t("v2.canary.design.heldOut", {
                      delta: verdict.held_out.delta, p: verdict.held_out.p ?? "—",
                      c: verdict.held_out.arms_n.control, t: verdict.held_out.arms_n.treatment,
                    })}
                  </div>
                )}
                {verdict.multiple_comparisons && (
                  <div className="v2-muted" data-testid={`v2-canary-secondary-${index}`}>
                    {multipleComparisonsNote(verdict, t)}
                  </div>
                )}
                {(verdict.unscored?.length ?? 0) > 0 && (
                  <div data-testid="v2-canary-unscored">
                    <Alert tone="warn">{t("v2.canary.eval.unscored", { evaluators: namesOf(verdict.unscored ?? []) })}</Alert>
                  </div>
                )}
                {verdict.metrics?.length > 0 && metricsTable(verdict.metrics, verdict.primary)}
              </>
            )}
            {current && canary.status === "running" && (
              <div className="v2-form cols-2">
                <Field label={t("expPage.datasetTag")}>
                  <Select
                    value={dataset}
                    onChange={setDataset}
                    testId="v2-canary-dataset"
                    placeholder={datasets.length === 0 ? t("canaryPage.noTrafficDataset") : undefined}
                    options={datasets.map((d) => ({ value: d.id, label: `${d.name} (${d.item_count})` }))}
                  />
                </Field>
                <Field
                  label={t("v2.canary.traffic.perArm")}
                  hint={defaultPerArm != null ? t("v2.canary.traffic.perArmDesignHint", { n: a.design?.planned_n_per_arm, left: defaultPerArm }) : t("v2.canary.traffic.perArmHint")}
                >
                  <input
                    className="v2-input"
                    type="number"
                    min={1}
                    max={SESSIONS_PER_ARM_MAX}
                    value={perArmText}
                    placeholder={t("v2.canary.traffic.perArmPlaceholder")}
                    data-testid="v2-canary-sessions-per-arm"
                    onChange={(e) => setPerArm(e.target.value)}
                  />
                </Field>
                <Field label={t("v2.common.actions")}>
                  <div className="v2-row" style={{ alignItems: "flex-start" }}>
                    {actionButton("traffic", attempts.length ? t("canaryPage.sendMoreTraffic") : t("canaryPage.sendTraffic"), {
                      primary: attempts.length === 0,
                      disabled: !dataset || !perArmValid,
                      extra: { dataset_id: dataset, ...(perArmText !== "" && perArmValid ? { sessions_per_arm: perArmValue } : {}) },
                    })}
                    {attempts.length > 0 && !verdict && actionButton("verdict", t("canaryPage.recordVerdict"), { primary: true })}
                    {verdict &&
                      !blocked &&
                      (canary.running_action === action || canary.error?.startsWith(`${action}: `) ? (
                        actionButton(action, advanceLabel, {
                          primary: !needsOverride,
                          extra: needsOverride ? { allow_non_significant: true } : undefined,
                        })
                      ) : (
                        // N17-obs427: creator-or-admin like every other action (server 403)
                        <div className="v2-stack">
                          <div>
                            <Button
                              kind={needsOverride ? undefined : "primary"}
                              disabled={locked || ownerLocked(action)}
                              title={ownerLocked(action) ? t("v2.common.creatorOnly") : undefined}
                              onClick={() => (needsOverride ? setConfirm({ action, allowNonSignificant: true }) : void onAction(action))}
                              testId={`v2-canary-action-${action}`}
                            >
                              {advanceLabel}
                            </Button>
                          </div>
                          {ownerLocked(action) && <span className="v2-muted">{t("v2.common.creatorOnly")}</span>}
                        </div>
                      ))}
                  </div>
                </Field>
              </div>
            )}
            {blocked && current && <Alert tone="error">{t("canaryPage.blocked", { verdict: verdict ? verdictLabel(verdict.verdict, t) : "" })}</Alert>}
            {needsOverride && current && (
              // N17-obs446: the verdicts by their localized names (canaryVerdict.code), no raw codes
              <Alert tone="warn" testId="v2-canary-override-hint">
                {t("canaryPage.overrideHint", {
                  tie: verdictLabel("tie", t),
                  insufficient: verdictLabel("insufficient-n", t),
                  controlWins: verdictLabel("control-wins", t),
                })}
              </Alert>
            )}
          </div>
        )}
      </StageCard>
    );
  };

  const pipeline = [
    { key: "setup", label: t("canaryPage.stage.setup"), sub: undefined as string | undefined, done: !!liveSetup, active: !liveSetup && !terminal },
    ...RAMP_STAGES.map((r, i) => ({
      key: `ramp-${i}`,
      label: `${r.control}/${r.treatment}`,
      sub: undefined,
      done: i < currentStage || (i === completedStage && !!a.complete),
      active: !!liveSetup && i === currentStage && !terminal,
    })),
    { key: "finish", label: t("v2.canary.finish"), sub: undefined, done: terminal, active: false },
  ];

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {canary.name}
            <Tag tone={CANARY_TONE[canary.status]}>{t(`v2.canary.status.${canary.status}`)}</Tag>
          </span>
        }
        onBack={() => setParams({ mode: "canary" })}
        end={
          <>
            <Button onClick={() => void refresh()}>{t("v2.common.refresh")}</Button>
            {setup && canary.status === "running" && canary.running_action !== "rollback" && !canary.error?.startsWith("rollback:") && (
              <Button disabled={locked || !owner} title={!owner ? t("v2.common.creatorOnly") : undefined} onClick={() => setConfirm({ action: "rollback" })} testId="v2-canary-rollback">
                {t("canaryPage.rollback")}
              </Button>
            )}
            {!a.cleanup && canary.running_action !== "cleanup" && !canary.error?.startsWith("cleanup:") && (
              <Button
                kind="danger"
                disabled={locked || !owner || cleanupNeedsDecision}
                title={!owner ? t("v2.common.creatorOnly") : cleanupNeedsDecision ? t("canaryPage.cleanupNeedsDecision") : undefined}
                onClick={() => setConfirm({ action: "cleanup" })}
                testId="v2-canary-cleanup"
              >
                {t("canaryPage.cleanup")}
              </Button>
            )}
          </>
        }
      />
      <Alert>{t("canaryPage.experimentalOnly")}</Alert>
      {!owner && !terminal && (
        <Alert data-testid="v2-canary-creator-only">{t("v2.common.creatorOnlyActions", { name: creatorLabel(canary) })}</Alert>
      )}
      {liveSetup?.v_current && liveSetup.v_candidate && (
        <Alert>{t("canaryPage.versionFraming", { current: liveSetup.v_current, candidate: liveSetup.v_candidate })}</Alert>
      )}
      {canary.error && ["advance:", "complete:"].some((p) => canary.error?.startsWith(p)) && <Alert tone="error">{canary.error}</Alert>}
      {(canary.running_action === "rollback" || canary.error?.startsWith("rollback:")) && actionButton("rollback", t("canaryPage.rollback"))}
      {(canary.running_action === "cleanup" || canary.error?.startsWith("cleanup:")) && actionButton("cleanup", t("canaryPage.cleanup"))}
      {summary && (
        <Alert tone={summary.tone} testId="v2-canary-summary">
          {summary.text}
        </Alert>
      )}

      <Card title={t("v2.taskDetail.overview")} testId="v2-canary-overview">
        <Descriptions
          items={[
            { label: t("v2.tasks.colName"), value: canary.name },
            { label: "ID", value: <span className="mono">{canary.id}</span> },
            { label: t("v2.canary.champion"), value: canary.champion_agent_name },
            { label: t("canaryPage.list.versions"), value: <span className="mono">{versionsLabel(setup)}</span> },
            { label: t("canaryPage.list.weights"), value: <span className="mono">{weightsLabel(setup)}</span> },
            {
              label: t("v2.canary.eval.title"),
              value: configured ? (
                <span className="v2-tags" data-testid="v2-canary-detail-evaluators">
                  {configured.map((id) => (
                    <Tag key={id} tone={latestUnscored.has(id) ? "orange" : id === primaryEvaluator ? "green" : "outline"}>
                      {nameOf(id)}
                      {id === primaryEvaluator && ` · ${t("v2.canary.eval.primaryTag")}`}
                      {latestUnscored.has(id) && ` · ${t("v2.canary.eval.unscoredTag")}`}
                    </Tag>
                  ))}
                </span>
              ) : (
                t("v2.canary.eval.defaultPair", { evaluators: namesOf(ONLINE_EVAL_DEFAULT) })
              ),
            },
            { label: t("v2.tasks.colCreated"), value: fmtTime(canary.created_at) },
            { label: t("v2.common.creator"), value: creatorLabel(canary) },
            {
              label: t("v2.experiments.colStage"),
              value: canary.running_action
                ? t("v2.canary.actionRunning", { action: t(`v2.canary.action.${canary.running_action}`, { defaultValue: canary.running_action }) })
                : t(`v2.canary.stage.${canary.stage}`, { defaultValue: canary.stage }),
            },
          ]}
        />
      </Card>

      <Card title={t("v2.canary.pipeline")} sub={t("canaryPage.how.note")}>
        <div className="v2-stages">
          {pipeline.map((s, i) => (
            <div key={s.key} className={`v2-stage ${s.done ? "succeeded" : s.active ? "running" : "pending"}`}>
              <span className="n">{s.done ? "✓" : i + 1}</span>
              <div className="b">
                <div className="t">{s.label}</div>
                {s.sub && <div className="v2-muted" style={{ fontSize: 12 }}>{s.sub}</div>}
              </div>
            </div>
          ))}
        </div>
      </Card>

      <StageCard id="setup" index={1} title={t("canaryPage.stage.setup")} state={liveSetup ? "done" : !terminal ? "active" : "pending"}>
        <div className="v2-form">
          {!setup && !terminal && actionButton("setup", t("canaryPage.setup"), { primary: true })}
          {provisioning &&
            (canary.running_action === "setup" ? (
              <span className="v2-muted">{progressText(canary, t) ?? t("canaryPage.running")}</span>
            ) : (
              // a partial setup cannot be retried (the backend refuses a second one): the exit is rollback + cleanup
              <Alert tone="error">
                {t("canaryPage.setupIncomplete")}
                {canary.error ? ` ${canary.error}` : ""}
              </Alert>
            ))}
          {setup && (
            <Descriptions
              items={[
                { label: t("canaryPage.list.versions"), value: <span className="mono">{versionsLabel(setup)}</span> },
                { label: "Gateway", value: <span className="mono">{setup.gateway_id}</span> },
                { label: "A/B test", value: <span className="mono">{liveSetup?.ab_test_id ?? "—"}</span> },
                {
                  label: t("v2.canary.targets"),
                  value: liveSetup ? <span className="mono">{`${liveSetup.champion?.target_name ?? "—"} ↔ ${liveSetup.challenger?.target_name ?? "—"}`}</span> : "—",
                },
              ]}
            />
          )}
        </div>
      </StageCard>
      {RAMP_STAGES.map((_, index) => rampCard(index))}

      {a.cleanup && (
        <Card title={t("expPage.card.cleanup")}>
          <Table
            columns={[
              { key: "c", title: t("v2.experiments.resource"), render: (r: { category: string; status: string }) => <span className="mono">{r.category}</span> },
              {
                key: "s",
                title: t("v2.tasks.colStatus"),
                render: (r: { category: string; status: string }) => (
                  // N17-obs446: localized, the raw status as tooltip (an unknown one stays raw)
                  <Tag tone={r.status === "deleted" ? "green" : r.status === "error" ? "red" : "gray"} title={r.status}>
                    {t(`v2.canary.cleanupStatus.${r.status}`, { defaultValue: r.status })}
                  </Tag>
                ),
              },
            ]}
            rows={a.cleanup}
            rowKey={(r) => `${r.category}:${r.status}`}
            density="dense"
          />
        </Card>
      )}

      <Confirm
        open={confirm !== null}
        title={
          confirm?.action === "cleanup"
            ? t("canaryPage.confirmCleanup.title")
            : confirm?.action === "rollback"
              ? t("canaryPage.confirmRollback.title")
              : t("canaryPage.confirmOverride.title")
        }
        body={
          confirm?.action === "cleanup"
            ? t("canaryPage.confirmCleanup.body")
            : confirm?.action === "rollback"
              ? t("canaryPage.confirmRollback.body")
              : confirm?.action === "complete"
                ? t("canaryPage.confirmOverride.completeBody")
                : t("canaryPage.confirmOverride.body")
        }
        confirmLabel={
          confirm?.action === "cleanup"
            ? t("canaryPage.cleanup")
            : confirm?.action === "rollback"
              ? t("canaryPage.rollback")
              : confirm?.action === "complete"
                ? t("canaryPage.complete")
                : t("canaryPage.advance")
        }
        danger={confirm?.action === "cleanup" || confirm?.action === "rollback"}
        busy={busy}
        onConfirm={() => {
          const pending = confirm;
          setConfirm(null);
          if (!pending) return;
          void onAction(pending.action, "allowNonSignificant" in pending ? { allow_non_significant: true } : {});
        }}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}
