import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type RuntimeCanaryInfo } from "../../../lib/api";
import { fmtScore, fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Field, FlowHeader, Spin, Table, Tag } from "../../ui";
import { StageCard } from "../experiments/StageCard";
import { CANARY_TONE, RAMP_STAGES, verdictTone, versionsLabel, weightsLabel } from "./common";

const POLL_MS = 8000;
const FAST_POLL_MS = 2500;

type Pending =
  | { action: "advance" | "complete"; allowNonSignificant: true }
  | { action: "rollback" | "cleanup" };

type Round = NonNullable<RuntimeCanaryInfo["artifacts"]["rounds"]>[number];
type Metric = NonNullable<Round["verdict"]>["metrics"][number];

export function CanaryDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const [canary, setCanary] = useState<RuntimeCanaryInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<Pending | null>(null);
  const datasetsLoad = useLoad(() => api.v2Datasets(), "datasets");
  const datasets = useMemo(() => (datasetsLoad.data?.datasets ?? []).filter((d) => d.kind !== "simulated"), [datasetsLoad.data]);
  const [dataset, setDataset] = useState("");

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

  const onAction = async (action: string, extra: { dataset_id?: string; allow_non_significant?: boolean } = {}) => {
    setBusy(true);
    try {
      const res = await api.runtimeCanaryAction(id, { action, ...extra });
      setCanary(res.canary);
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
  const locked = busy || !!canary.running_action;
  // setup is persisted PARTIALLY (gateway + stable endpoint) as soon as provisioning
  // starts; only an A/B test id makes it live
  const setup = a.setup;
  const liveSetup = setup?.ab_test_id ? setup : undefined;
  const provisioning = !!setup && !liveSetup;
  const rounds = a.rounds ?? [];
  const currentStage = liveSetup?.ramp_stage ?? 0;
  const terminal = canary.status !== "running";

  /** Button while pending → progress line; a stored `<action>: …` error turns it into a retry. */
  const actionButton = (
    action: string,
    label: string,
    opts: { primary?: boolean; disabled?: boolean; extra?: { dataset_id?: string; allow_non_significant?: boolean } } = {},
  ) => {
    const isRunning = canary.running_action === action;
    const failed = !isRunning && !!canary.error?.startsWith(`${action}: `);
    return (
      <div className="v2-stack">
        <div>
          <Button
            kind={opts.primary && !failed ? "primary" : undefined}
            disabled={locked || opts.disabled}
            onClick={() => void onAction(action, opts.extra)}
            testId={`v2-canary-action-${action}`}
          >
            {isRunning ? t("canaryPage.running") : failed ? t("canaryPage.retry") : label}
          </Button>
        </div>
        {isRunning && <span className="v2-muted">{canary.progress ?? "…"}</span>}
        {failed && <Alert tone="error">{canary.error}</Alert>}
      </div>
    );
  };

  const metricsTable = (metrics: Metric[]) => (
    <Table
      columns={[
        { key: "m", title: t("v2.evaluators.colName"), render: (m: Metric) => m.label },
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
    const blocked =
      !!verdict && (verdict.verdict === "control-wins" || verdict.verdict === "insufficient-data" || verdict.verdict === "insufficient-n");
    const needsOverride = !blocked && !!verdict && (verdict.verdict === "tie" || verdict.significant === false);
    const action = index === RAMP_STAGES.length - 1 ? "complete" : "advance";
    const done = index < currentStage || (index === 2 && !!a.complete);
    const advanceLabel = action === "complete" ? t("canaryPage.complete") : t("canaryPage.advance");
    const state = done ? "done" : current && canary.status === "running" ? "active" : "pending";

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
              <span className="v2-muted">{t("canaryPage.experimentalWeights", { control: ramp.control, treatment: ramp.treatment })}</span>
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
                  <Tag tone={verdictTone(verdict.verdict)}>{verdict.verdict.toUpperCase()}</Tag>
                  <span className="v2-muted">n={verdict.n ?? 0}</span>
                  {verdict.significant === false && <Tag tone="gray">{t("canaryPage.notSignificant")}</Tag>}
                </div>
                {verdict.metrics?.length > 0 && metricsTable(verdict.metrics)}
              </>
            )}
            {current && canary.status === "running" && (
              <div className="v2-form cols-2">
                <Field label={t("expPage.datasetTag")}>
                  <select className="v2-select" value={dataset} onChange={(e) => setDataset(e.target.value)} data-testid="v2-canary-dataset">
                    {datasets.length === 0 && <option value="">{t("canaryPage.noTrafficDataset")}</option>}
                    {datasets.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name} ({d.item_count})
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label={t("v2.common.actions")}>
                  <div className="v2-row" style={{ alignItems: "flex-start" }}>
                    {actionButton("traffic", attempts.length ? t("canaryPage.sendMoreTraffic") : t("canaryPage.sendTraffic"), {
                      primary: attempts.length === 0,
                      disabled: !dataset,
                      extra: { dataset_id: dataset },
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
                        <Button
                          kind={needsOverride ? undefined : "primary"}
                          disabled={locked}
                          onClick={() => (needsOverride ? setConfirm({ action, allowNonSignificant: true }) : void onAction(action))}
                          testId={`v2-canary-action-${action}`}
                        >
                          {advanceLabel}
                        </Button>
                      ))}
                  </div>
                </Field>
              </div>
            )}
            {blocked && current && <Alert tone="error">{t("canaryPage.blocked", { verdict: verdict?.verdict })}</Alert>}
            {needsOverride && current && <Alert tone="warn">{t("canaryPage.overrideHint")}</Alert>}
          </div>
        )}
      </StageCard>
    );
  };

  const pipeline = [
    { key: "setup", label: t("canaryPage.stage.setup"), done: !!liveSetup, active: !liveSetup && !terminal },
    ...RAMP_STAGES.map((r, i) => ({
      key: `ramp-${i}`,
      label: `${r.control}/${r.treatment}`,
      done: i < currentStage || (i === 2 && !!a.complete),
      active: !!liveSetup && i === currentStage && !terminal,
    })),
    { key: "finish", label: t("v2.canary.finish"), done: terminal, active: false },
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
              <Button disabled={locked} onClick={() => setConfirm({ action: "rollback" })} testId="v2-canary-rollback">
                {t("canaryPage.rollback")}
              </Button>
            )}
            {!a.cleanup && canary.running_action !== "cleanup" && !canary.error?.startsWith("cleanup:") && (
              <Button kind="danger" disabled={locked} onClick={() => setConfirm({ action: "cleanup" })} testId="v2-canary-cleanup">
                {t("canaryPage.cleanup")}
              </Button>
            )}
          </>
        }
      />
      <Alert>{t("canaryPage.experimentalOnly")}</Alert>
      {liveSetup?.v_current && liveSetup.v_candidate && (
        <Alert>{t("canaryPage.versionFraming", { current: liveSetup.v_current, candidate: liveSetup.v_candidate })}</Alert>
      )}
      {canary.error && ["advance:", "complete:"].some((p) => canary.error?.startsWith(p)) && <Alert tone="error">{canary.error}</Alert>}
      {(canary.running_action === "rollback" || canary.error?.startsWith("rollback:")) && actionButton("rollback", t("canaryPage.rollback"))}
      {(canary.running_action === "cleanup" || canary.error?.startsWith("cleanup:")) && actionButton("cleanup", t("canaryPage.cleanup"))}
      {(a.complete || a.rollback) && (
        <Alert tone={a.complete ? "success" : "warn"}>
          {a.complete
            ? t("canaryPage.completedSummary", { version: a.complete.promoted_version ?? "—" })
            : t("canaryPage.rollbackSummary", { version: a.rollback?.restored_version ?? "—" })}
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
            { label: t("v2.canary.sourceExp"), value: canary.source_experiment_id ? <span className="mono">{canary.source_experiment_id}</span> : "—" },
            { label: t("v2.tasks.colCreated"), value: fmtTime(canary.created_at) },
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
              <span className="v2-muted">{canary.progress ?? t("canaryPage.running")}</span>
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
                  <Tag tone={r.status === "deleted" ? "green" : r.status === "error" ? "red" : "gray"}>{r.status}</Tag>
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
