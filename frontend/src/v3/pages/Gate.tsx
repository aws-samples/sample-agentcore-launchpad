import { Play, RotateCcw, ShieldCheck, ShieldX, Tag } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { ErrorCallout } from "../../components/ErrorCallout";
import { api, dlcApi } from "../../lib/api";
import { type ReleaseStorageEvidence, changeDelta, hasStorage, mountLabel, releaseStorage, storageDlcApi } from "../../lib/api/storageDlc";
import { type EvalCostEstimate, evalCostApi } from "../../lib/api/evalCost";
import { type GateReport, type GateRow, type ReleaseState, type Scorecard, canRunGate, metricValueLabel } from "../../lib/dlc";
import { useWorkspace } from "../../workspace/workspace-context";
import { firstReason, permReason } from "../reasons";
import { gateSignal, releaseSignal } from "../signals";
import { Btn, Chip, Dialog, Empty, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { useLoad, useToast } from "../hooks";
import type { NextStepNote } from "../story/flow";
import { useNextStep } from "../story/useNextStep";
import { StoryPrereqs } from "../story/Prereqs";
import { StoryHeader } from "../story/StoryHeader";
import { ago, pct } from "../format";
import { calibrateUrl } from "./standards/calibrationLink";
import { dlcText } from "./standards/common";
import { EstimateCostCard } from "./tasks/CostCard";
import { Select } from "../select";

const ROW_SIGNAL: Record<GateRow["verdict"], Signal> = {
  PASS: "ok",
  BLOCKED: "act",
  INVALID: "wait",
  WAIVED: "info",
  OBSERVED: "off",
};

/**
 * The four gates, in the order they are applied, as one pipeline the verdict
 * flows through. A gate that stopped the release lights up and everything after it
 * stays dark — so "where did it stop" is answered before anything is read.
 */
function Pipeline({ report }: { report: GateReport }) {
  const { t } = useTranslation();
  const stopIndex =
    report.redline_violations.length > 0 ? 0 : report.invalid.length > 0 || report.provenance.issues.length > 0 ? 1
      : report.gate_failures.length > 0 ? 2 : -1;
  const steps = [
    { key: "redline", count: report.redline_violations.length, s: "act" as Signal },
    { key: "denominator", count: report.invalid.length + report.provenance.issues.length, s: "wait" as Signal },
    { key: "gate", count: report.gate_failures.length, s: "act" as Signal },
    { key: "observe", count: report.criteria.filter((c) => c.verdict === "OBSERVED").length, s: "off" as Signal },
  ];
  return (
    <div className="v3-pipe">
      {steps.map((step, i) => {
        const stopped = i === stopIndex;
        const passed = stopIndex === -1 || i < stopIndex;
        const s: Signal = stopped ? step.s : passed ? (step.key === "observe" ? "info" : "ok") : "off";
        return (
          <div key={step.key} className="v3-pipe-step" data-s={s}>
            <div className="v3-pipe-node"><Lamp s={s} live={stopped} /></div>
            <div className="v3-stat">
              <div className="label">{`0${i + 1}`} · {t(`v3.gate.step.${step.key}`)}</div>
              <div className="mono" style={{ marginTop: 4 }}>
                {stopped ? t(`v3.gate.stopped.${step.key}`, { count: step.count })
                  : passed ? (step.key === "observe" ? t("v3.gate.recorded", { count: step.count }) : t("v3.gate.passed"))
                    : t("v3.gate.notReached")}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** A rate as a point inside its interval, against the threshold tick. */
function Interval({ row }: { row: GateRow }) {
  const { t } = useTranslation();
  // metric criteria (latency, tokens, cost) carry a value in their own unit, not a rate
  if (row.kind === "metric") return <span className="mono">{metricValueLabel(row)}</span>;
  const rate = typeof row.measured === "number" ? row.measured : null;
  if (rate === null) return <span style={{ color: "var(--v3-text-3)" }}>—</span>;
  const x = (v: number) => `${Math.max(0, Math.min(100, v * 100))}%`;
  const lo = row.wilson_low ?? null;
  const hi = row.wilson_high ?? null;
  return (
    <div className="v3-interval" data-undecided={row.threshold_inside_ci ? "true" : undefined}>
      <div className="track">
        {lo !== null && hi !== null && <div className="ci" style={{ left: x(lo), width: `${Math.max(1, (hi - lo) * 100)}%` }} />}
        {typeof row.threshold === "number" && <div className="thr" style={{ left: x(row.threshold) }} />}
        <div className="dot" data-s={ROW_SIGNAL[row.verdict]} style={{ left: x(rate) }} />
      </div>
      <span className="mono">{pct(rate, 1)}</span>
      {row.threshold_inside_ci && <Chip s="wait">{t("v3.gate.cannotDecide")}</Chip>}
    </div>
  );
}

function Dimensions({ card }: { card: Scorecard }) {
  const { t } = useTranslation();
  return (
    <div className="v3-grid" style={{ gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
      {card.dimensions.map((d) => {
        const metric = d.current === null && d.metrics?.length ? d.metrics[0] : null;
        const short = d.current !== null && d.standard !== null && d.current < d.standard;
        return (
          <Panel key={d.dimension} signal={d.not_applicable ? undefined : short || d.redline_violations ? "act" : "ok"}>
            <Stat
              label={t(`v2.dlc.dimension.${d.dimension}`)}
              value={d.not_applicable ? "—" : metric ? Math.round(metric.value ?? 0).toLocaleString() : pct(d.current)}
              unit={metric ? (metric.metric?.includes("ms") ? "ms" : undefined) : undefined}
              signal={d.not_applicable ? undefined : short || d.redline_violations ? "act" : "ok"}
              foot={d.not_applicable ? t("v3.gate.na") : metric ? `${metric.op} ${metric.bound}`
                : t("v3.gate.standard", { v: d.standard === null ? "—" : pct(d.standard) })}
            />
          </Panel>
        );
      })}
    </div>
  );
}

/**
 * The candidate's storage mounts against live (storage-mounts §8.1): one row per
 * changed mount, a lit row when the signer has to acknowledge it.
 */
function StoragePanel({ ev }: { ev: ReleaseStorageEvidence }) {
  const { t } = useTranslation();
  return (
    <Panel title={t("v3.gate.storage.title")} signal={ev.requires_ack ? "act" : undefined} flush={ev.changes.length > 0}>
      {!ev.live_known && (
        <div style={{ padding: ev.changes.length ? "12px 16px 0" : undefined }}>
          <Notice s="wait">{t("v3.gate.storage.liveUnknown")}</Notice>
        </div>
      )}
      {ev.changes.length === 0 ? (
        <span style={{ color: "var(--v3-text-2)" }}>{t("v3.gate.storage.unchanged", { count: ev.candidate.length })}</span>
      ) : (
        <table className="v3-table" data-testid="v3-gate-storage">
          <tbody>
            {ev.changes.map((c) => (
              <tr key={c.mount_path}>
                <td style={{ width: 30 }}><Lamp s={c.requires_ack ? "act" : "info"} /></td>
                <td className="mono">{c.mount_path}</td>
                <td>{mountLabel(c)}</td>
                <td>
                  <Chip s={c.requires_ack ? "act" : undefined}>{t(`v3.gate.storage.change.${c.change}`)}</Chip>
                  {changeDelta(c) && <span className="mono" style={{ marginLeft: 8, color: "var(--v3-text-2)" }}>{changeDelta(c)}</span>}
                </td>
                <td>
                  {c.mode && <Chip s={c.mode === "rw" ? "wait" : undefined}>{c.mode}</Chip>}{" "}
                  {c.data_class && <span style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{t(`v3.gate.storage.class.${c.data_class}`)}</span>}
                </td>
                <td style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{c.requires_ack ? t("v3.gate.storage.needsAck") : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {ev.acknowledged_by && (
        <div style={{ padding: "8px 16px", color: "var(--v3-text-2)" }}>{t("v3.gate.storage.acknowledged", { who: ev.acknowledged_by })}</div>
      )}
      {ev.write_confirmation && (
        <div style={{ padding: "8px 16px", color: "var(--v3-text-2)" }}>
          {t("v3.gate.storage.writesConfirmed", { who: ev.write_confirmation.confirmed_by, storage: ev.write_confirmation.storage.join(", ") })}
        </div>
      )}
    </Panel>
  );
}

export function V3Gate() {
  const { t } = useTranslation();
  const toast = useToast();
  const { can } = useAuth();
  const { current } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const agents = useLoad(() => api.listAgents(), `v3-gate-agents:${current?.id ?? ""}`);
  const active = useMemo(() => (agents.data?.agents ?? []).filter((a) => a.status === "active"), [agents.data]);
  const agentId = params.get("agent") ?? "";
  useEffect(() => {
    if (!agentId && active.length) setParams({ agent: active[0].id }, { replace: true });
  }, [agentId, active, setParams]);

  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  // sign / block / rollback each go through a dialog with a note, as in V2: a
  // block or rollback must say why, and the reason is recorded against the release
  const [deciding, setDeciding] = useState<"sign" | "block" | "rollback" | null>(null);
  const [note, setNote] = useState("");
  // storage-mounts §8: the sign-off of read-write storage changes, and the
  // confirmation that a gate evaluation writes production shared-state storage
  const [storageAck, setStorageAck] = useState(false);
  const [confirmWrites, setConfirmWrites] = useState(false);
  // what running the gate would cost; over the workspace limit it cannot run, and
  // the server decides whether the cost needs an explicit confirm
  const estimate = useLoad<EvalCostEstimate | null>(
    () => (agentId ? evalCostApi.estimate({ agent_id: agentId, repeats: 1, release: true }) : Promise.resolve(null)), `v3-gate-est:${agentId}`);
  const [confirmCost, setConfirmCost] = useState(false);
  const release = useLoad<ReleaseState | null>(
    () => (agentId ? dlcApi.release(agentId) : Promise.resolve(null)), `v3-gate-rel:${agentId}:${nonce}`);
  const card = useLoad<Scorecard | null>(
    () => (agentId ? dlcApi.scorecard(agentId) : Promise.resolve(null)), `v3-gate-card:${agentId}:${nonce}`);
  const pending = release.data?.pending ?? null;
  const evaluated = Boolean(pending && pending.run_ids.length > 0);
  // never evaluated, or INVALID (a run did not complete): the run panel is offered
  const runnable = canRunGate(pending);
  const gate = useLoad(
    () => (evaluated ? dlcApi.gate(agentId) : Promise.resolve(null)), `v3-gate-report:${agentId}:${evaluated}:${nonce}`);
  const report = gate.data?.report ?? null;
  const evaluating = gate.data?.status === "evaluating";
  const storage = releaseStorage(report) ?? releaseStorage(pending);
  const writes = pending && runnable ? (releaseStorage(pending)?.shared_state_writes ?? []) : [];

  // a failed gate action stays on the page with its way out (storyline-ux §4); a
  // signed release says what comes next instead of a toast (§3.1)
  const [failure, setFailure] = useState<unknown>(null);
  const nextStep = useNextStep();
  useEffect(() => setFailure(null), [agentId]);
  const act = (fn: () => Promise<unknown>, ok: string, note?: NextStepNote) => {
    setBusy(true);
    setFailure(null);
    nextStep.clear();
    fn()
      .then(() => {
        if (note) nextStep.show(note);
        else toast("ok", t(ok));
        setNonce((n) => n + 1);
      })
      .catch((err: unknown) => setFailure(err))
      .finally(() => setBusy(false));
  };

  // a gate evaluation runs for minutes: while it does, ask for the report quietly and
  // re-read the page once it is there, then say what comes next (§3.1)
  const showNext = nextStep.show;
  const watched = useRef(false);
  useEffect(() => {
    watched.current = false;
  }, [agentId]);
  useEffect(() => {
    if (!evaluating || !agentId) return;
    watched.current = true;
    const id = window.setInterval(() => {
      dlcApi.gate(agentId).then((g) => { if (g.report) setNonce((n) => n + 1); }).catch(() => undefined);
    }, 10_000);
    return () => window.clearInterval(id);
  }, [evaluating, agentId]);
  useEffect(() => {
    if (!watched.current || evaluating || !report) return;
    watched.current = false;
    showNext({ done: t("story.done.gateReport", { verdict: report.verdict }), after: "evaluate.gate", agentId });
  }, [evaluating, report, agentId, showNext, t]);

  const agent = active.find((a) => a.id === agentId);
  const state = release.data?.state;
  const verdictSignal = gateSignal(report?.verdict);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <StoryHeader />
      <PageHead
        eyebrow={t("v3.gate.eyebrow")}
        title={report ? t(`v3.gate.headline.${report.verdict}`) : pending ? t("v3.gate.headline.waiting") : t("v3.gate.headline.none")}
        sub={t("v3.gate.sub")}
        end={
          <Select className="v3-select" style={{ width: 260 }} value={agentId}
            onChange={(e) => setParams({ agent: e.target.value })} aria-label={t("v3.chat.agent")}>
            {active.map((a) => <option key={a.id} value={a.id}>{a.display_name || a.name}</option>)}
          </Select>
        }
      />

      {agents.loading && !agents.data ? <Skeleton rows={5} /> : !agent ? (
        <Panel><Empty title={t("v3.gate.noAgents")} /></Panel>
      ) : (
        <>
          <div className="v3-grid c4">
            <Panel signal="ok"><Stat label={t("v3.gate.live")} value={`v${state?.live_version ?? agent.version ?? "—"}`}
              foot={state?.endpoint_mode === "live" ? t("v3.gate.namedEndpoint") : t("v3.gate.defaultEndpoint")} /></Panel>
            <Panel signal={pending ? "wait" : undefined}><Stat label={t("v3.gate.candidate")}
              value={pending ? `v${pending.candidate_version ?? "—"}` : "—"} signal={pending ? "wait" : undefined}
              foot={pending ? t(`v2.dlc.release.decision.${pending.decision}`) : t("v3.gate.noCandidate")} /></Panel>
            <Panel signal={verdictSignal === "off" ? undefined : verdictSignal}><Stat label={t("v3.gate.verdict")}
              value={report?.verdict ?? "—"} signal={verdictSignal === "off" ? undefined : verdictSignal}
              foot={report ? ago(report.decided_at) : evaluating ? t("v3.gate.evaluating") : "—"} /></Panel>
            <Panel><Stat label={t("v3.gate.standardLabel")}
              value={release.data?.criteria_set ? `v${release.data.criteria_set.version}` : "—"}
              foot={release.data?.criteria_set?.signed_by ? t("v3.gate.signedBy", { who: release.data.criteria_set.signed_by })
                : t("v3.gate.unsigned")} signal={release.data?.criteria_set?.signed_by ? undefined : "wait"} /></Panel>
          </div>

          <ErrorCallout error={failure} params={{ agent_id: agent.id }} onDismiss={() => setFailure(null)}
            data-testid="v3-gate-error" />
          {nextStep.card}

          {/* nothing to gate yet: say which step before it is missing (§3.5) */}
          {!pending && !report && !release.loading && release.data && !state?.live_version && (
            <Panel>
              <Empty title={t("v3.gate.noCandidate")}>
                <StoryPrereqs agentId={agent.id} stepKey="evaluate.gate" />
              </Empty>
            </Panel>
          )}

          {state?.endpoint_mode !== "live" && (
            <Notice s="wait">
              {t("v3.gate.notGated")}{" "}
              <Link to={`/v2/eval/standards?agent=${agent.id}&view=release`} style={{ textDecoration: "underline" }}>
                {t("v3.gate.setUp")}
              </Link>
            </Notice>
          )}

          {report && (
            <Panel title={t("v3.gate.pipeline")} signal={verdictSignal === "off" ? undefined : verdictSignal}>
              <Pipeline report={report} />
              {report.provenance.issues.length > 0 && (
                <div style={{ marginTop: 14 }}>
                  <Notice s="wait">{report.provenance.issues.join(" · ")}</Notice>
                </div>
              )}
              {/* the regression run is the recommended calibration source; the holdout
                  offers no such link — labelling it would show people its samples */}
              {can("criteria.manage") && (report.runs ?? []).some((r) => r.split === "regression" && r.status === "completed") && (
                <div style={{ marginTop: 14, display: "grid", gap: 8 }}>
                  <div className="v3-rail-label">{t("v2.dlc.calibration.gateRuns")}</div>
                  {(report.runs ?? []).filter((r) => r.split === "regression" && r.status === "completed").map((r) => (
                    <div key={r.id} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                      <Chip>{t("v2.dlc.split.regression")}</Chip>
                      <span className="mono" style={{ color: "var(--v3-text-2)" }}>{r.id}</span>
                      <Link to={calibrateUrl(agent.id, r.id)} className="v3-btn ghost sm">
                        <Tag size={13} /> {t("v2.dlc.calibration.calibrateFromRegression")}
                      </Link>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          )}

          {hasStorage(storage) && <StoragePanel ev={storage} />}

          {pending && runnable && (
            <EstimateCostCard estimate={estimate.data} loading={estimate.loading} error={estimate.error}
              confirmed={confirmCost} onConfirm={setConfirmCost} />
          )}

          {pending && runnable && (
            <Panel title={t("v3.gate.run")} signal="wait">
              <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
                <span style={{ color: "var(--v3-text-2)" }}>{t(pending.decision === "invalid" ? "v3.gate.rerunHint" : "v3.gate.runHint")}</span>
                <span style={{ flex: 1 }} />
                <Btn kind="primary" busy={busy || estimate.loading}
                  disabled={!can("eval.run") || Boolean(estimate.data?.over_limit)
                  || (Boolean(estimate.data?.confirm_required) && !confirmCost)
                  || (writes.length > 0 && !confirmWrites)}
                  disabledReason={firstReason(
                    !can("eval.run") && permReason(t, "eval.run"),
                    Boolean(estimate.data?.over_limit) && t("story.reason.overCostLimit"),
                    Boolean(estimate.data?.confirm_required) && !confirmCost && t("story.reason.confirmCost"),
                    writes.length > 0 && !confirmWrites && t("story.reason.confirmFirst"),
                  )}
                  onClick={() =>
                    act(
                      () => writes.length > 0
                        ? storageDlcApi.evaluateRelease(agent.id, { repeats: 1, confirm_cost: Boolean(estimate.data?.confirm_required), confirm_storage: confirmWrites })
                        : dlcApi.evaluateRelease(agent.id, { repeats: 1, confirm_cost: Boolean(estimate.data?.confirm_required) }),
                      "v3.gate.started",
                    )
                  }>
                  <Play size={14} /> {t("v3.gate.runBtn")}
                </Btn>
              </div>
              {writes.length > 0 && (
                <div style={{ marginTop: 14, display: "grid", gap: 8 }}>
                  <Notice s="act">{t("v3.gate.storage.writesWarning", { storage: writes.map(mountLabel).join(", ") })}</Notice>
                  <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <input type="checkbox" checked={confirmWrites} onChange={(e) => setConfirmWrites(e.target.checked)}
                      data-testid="v3-gate-storage-confirm" />
                    {t("v3.gate.storage.confirmWrites")}
                  </label>
                </div>
              )}
            </Panel>
          )}
          {evaluating && <Notice s="wait">{t("v3.gate.evaluating")}</Notice>}

          {report && (
            <Panel title={t("v3.gate.criteria")} flush
              end={
                can("release.sign") ? (
                  <span style={{ display: "flex", gap: 8 }}>
                    <Btn kind="primary" size="sm" busy={busy} disabled={report.verdict !== "PASS"}
                      disabledReason={t("v2.dlc.release.onlyPassSigns")}
                      onClick={() => { setNote(""); setStorageAck(false); setDeciding("sign"); }}>
                      <ShieldCheck size={13} /> {t("v3.gate.sign")}
                    </Btn>
                    <Btn kind="danger" size="sm" disabled={busy}
                      onClick={() => { setNote(""); setDeciding("block"); }}>
                      <ShieldX size={13} /> {t("v3.gate.block")}
                    </Btn>
                  </span>
                ) : undefined
              }
            >
              <table className="v3-table">
                <thead>
                  <tr>
                    <th />
                    <th>{t("v3.gate.criterion")}</th>
                    <th>{t("v3.gate.tier")}</th>
                    <th style={{ width: "34%" }}>{t("v3.gate.measured")}</th>
                    <th className="num">n</th>
                  </tr>
                </thead>
                <tbody>
                  {report.criteria.map((row) => (
                    <tr key={row.key}>
                      <td style={{ width: 30 }}><Lamp s={ROW_SIGNAL[row.verdict]} /></td>
                      <td>
                        <div style={{ fontWeight: 600 }}><span className="mono" style={{ color: "var(--v3-text-3)" }}>{row.key}</span> {row.text}</div>
                        {row.reason && <div style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{dlcText(t, row.reason)}</div>}
                      </td>
                      <td>
                        <Chip s={row.tier === "redline" ? "act" : row.tier === "gate" ? "wait" : undefined}>
                          {t(`v2.dlc.tier.${row.effective_tier ?? row.tier}`)}
                        </Chip>
                      </td>
                      <td><Interval row={row} /></td>
                      <td className="num">{row.n ?? "—"}<span style={{ color: "var(--v3-text-3)" }}>/{row.expected ?? "—"}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          )}

          {card.data && (
            <>
              <div className="v3-rail-label" style={{ padding: "6px 0 0" }}>{t("v3.gate.dimensions")}</div>
              <Dimensions card={card.data} />
            </>
          )}

          {release.data && release.data.records.length > 0 && (
            <Panel title={t("v3.gate.history")} flush
              end={can("release.sign") && state?.live_version ? (
                <Btn size="sm" kind="ghost" disabled={busy}
                  onClick={() => { setNote(""); setDeciding("rollback"); }}>
                  <RotateCcw size={13} /> {t("v3.gate.rollback")}
                </Btn>
              ) : undefined}>
              <table className="v3-table">
                <tbody>
                  {release.data.records.slice(0, 8).map((r) => (
                    <tr key={r.id}>
                      <td style={{ width: 30 }}><Lamp s={releaseSignal(r.decision)} /></td>
                      <td className="mono">v{r.candidate_version ?? "—"}</td>
                      <td>{t(`v2.dlc.release.decision.${r.decision}`)}</td>
                      <td className="mono" style={{ color: "var(--v3-text-2)" }}>{r.decided_by ?? r.requested_by}</td>
                      <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.decided_at ?? r.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          )}

          <div style={{ display: "flex", gap: 10 }}>
            <Link to={`/v3/standards?agent=${agent.id}&view=criteria`} className="v3-btn ghost">
              {t("v3.gate.editCriteria")}
            </Link>
            <Link to={`/v3/standards?agent=${agent.id}&view=calibration`} className="v3-btn ghost">
              {t("v3.gate.calibrate")}
            </Link>
          </div>

          {deciding && (
            <Dialog
              title={t(deciding === "sign" ? "v3.gate.sign" : deciding === "block" ? "v3.gate.block" : "v3.gate.rollback")}
              onClose={() => setDeciding(null)}
              foot={
                <>
                  <Btn kind="ghost" onClick={() => setDeciding(null)}>{t("v3.common.cancel")}</Btn>
                  <Btn
                    kind={deciding === "sign" ? "primary" : "danger"}
                    busy={busy}
                    disabled={(deciding !== "sign" && !note.trim())
                      || (deciding === "sign" && Boolean(storage?.requires_ack) && !storageAck)}
                    disabledReason={deciding !== "sign" ? t("story.reason.fillReason") : t("story.reason.confirmFirst")}
                    onClick={() => {
                      const action = deciding;
                      setDeciding(null);
                      if (action === "sign") {
                        act(() => storage?.requires_ack ? storageDlcApi.signRelease(agent.id, note, storageAck)
                          : dlcApi.signRelease(agent.id, note), "v3.gate.signed",
                        { done: t("story.done.releaseSigned"), after: "release.sign", agentId: agent.id });
                      }
                      else if (action === "block") act(() => dlcApi.blockRelease(agent.id, note), "v3.gate.blocked");
                      else act(() => dlcApi.rollback(agent.id, note), "v3.gate.rolledBack");
                    }}
                  >
                    {t(deciding === "sign" ? "v3.gate.sign" : deciding === "block" ? "v3.gate.block" : "v3.gate.rollback")}
                  </Btn>
                </>
              }
            >
              <p style={{ marginTop: 0 }}>{t(`v2.dlc.release.${deciding}Hint`)}</p>
              {deciding === "sign" && report && (
                <dl className="v3-kv" style={{ marginBottom: 14 }}>
                  <dt>{t("v2.dlc.release.verdict")}</dt><dd><Chip s={gateSignal(report.verdict)}>{report.verdict}</Chip></dd>
                  <dt>{t("v2.dlc.release.waived")}</dt><dd>{report.waived.join(", ") || "—"}</dd>
                  <dt>{t("v2.dlc.release.liveAfter")}</dt><dd className="mono">{pending?.candidate_version ? `v${pending.candidate_version}` : "—"}</dd>
                  <dt>{t("v2.dlc.release.rollbackTo")}</dt><dd className="mono">{pending?.previous_live_version ? `v${pending.previous_live_version}` : "—"}</dd>
                </dl>
              )}
              {deciding === "sign" && storage?.requires_ack && (
                <label style={{ display: "flex", gap: 8, alignItems: "flex-start", marginBottom: 14 }}>
                  <input type="checkbox" checked={storageAck} onChange={(e) => setStorageAck(e.target.checked)}
                    data-testid="v3-gate-storage-ack" />
                  <span>
                    {t("v3.gate.storage.ack", {
                      changes: storage.changes.filter((c) => c.requires_ack)
                        .map((c) => `${c.mount_path} ${t(`v3.gate.storage.change.${c.change}`)}`).join(", "),
                    })}
                  </span>
                </label>
              )}
              <label className="v3-field">
                <span>{t("v2.dlc.release.note")}{deciding !== "sign" ? " *" : ""}</span>
                <textarea className="v3-input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
              </label>
            </Dialog>
          )}
        </>
      )}
    </div>
  );
}
