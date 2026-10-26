/**
 * The release standard of one agent: where traffic is, the candidate and its gate
 * runs, waivers, the workspace policy and the history. Reading the four-gate report
 * and signing / blocking / rolling back live on the release gate page (`/v3/gate`),
 * which this view links to rather than duplicates. What the gate page does not do
 * is here: moving production onto named endpoints, a pass^k evaluation with its cost
 * estimate, waivers (never on a red line), and the policy.
 */
import { ArrowRight, Scale } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { dlcApi } from "../../../lib/api";
import { type GateRow, type ReleaseState, type Waiver, canRunGate, estimateValue, goldenVersionsLabel, waiverGapLabel } from "../../../lib/dlc";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { firstReason } from "../../reasons";
import { Btn, Chip, Confirm, Dialog, Empty, Lamp, Notice, Panel, Skeleton, Stat } from "../../ui";
import { decisionSignal, gateSignal, stamp } from "./common";
import { Select } from "../../select";

function Waivers({ state, rows, onReload }: { state: ReleaseState; rows: GateRow[]; onReload: () => void }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const [asking, setAsking] = useState(false);
  const [deciding, setDeciding] = useState<{ waiver: Waiver; approve: boolean } | null>(null);
  const [revoking, setRevoking] = useState<Waiver | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ criterion_key: "", reason: "", risk_owner: "", compensating_control: "", days: 7 });
  const mayApprove = can("waiver.approve");
  // a red line is never waivable, so it is not even offered
  const waivable = rows.filter((r) => r.tier !== "redline" && r.verdict === "BLOCKED");

  const [failure, setFailure] = useState<unknown>(null);
  const run = (fn: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    setFailure(null);
    fn()
      .then(() => { toast("ok", t(okKey)); onReload(); })
      .catch((err: unknown) => setFailure(err))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <ErrorCallout error={failure} params={{ agent_id: state.agent_id }} onDismiss={() => setFailure(null)}
        data-testid="v3-waiver-error" />
      <Panel title={t("v2.dlc.waiver.title")} flush={state.waivers.length > 0}
        signal={state.waivers.some((w) => w.status === "requested") ? "wait" : undefined}
        end={waivable.length > 0 ? <Btn size="sm" onClick={() => { setForm({ ...form, criterion_key: waivable[0].key }); setAsking(true); }}>{t("v2.dlc.waiver.request")}</Btn> : undefined}>
        {state.waivers.length === 0 ? (
          <p className="v3-std-muted" style={{ margin: 0 }}>{t("v2.dlc.waiver.none")}</p>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v2.dlc.criteria.key")}</th>
                <th>{t("v3.standards.status")}</th>
                <th>{t("v2.dlc.waiver.gap")}</th>
                <th>{t("v2.dlc.waiver.riskOwner")}</th>
                <th>{t("v2.dlc.waiver.expires")}</th>
                <th>{t("v2.dlc.waiver.times")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {state.waivers.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.criterion_key}</td>
                  <td><Chip s={row.active ? "info" : row.status === "rejected" ? "act" : row.status === "requested" ? "wait" : undefined}>{t(`v2.dlc.waiver.status.${row.status}`)}</Chip></td>
                  <td className="mono">{waiverGapLabel(row, rows)}</td>
                  <td>{row.risk_owner}</td>
                  <td>{row.expires_on?.slice(0, 10) ?? "—"}</td>
                  <td>{(row.times_waived ?? 0) > 1 ? <Chip s="wait">{t("v2.dlc.waiver.repeat", { n: row.times_waived })}</Chip> : row.times_waived ?? 1}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    {row.status === "requested" && mayApprove ? (
                      <span style={{ display: "inline-flex", gap: 6 }}>
                        <Btn size="sm" kind="primary" onClick={() => setDeciding({ waiver: row, approve: true })}>{t("v2.dlc.waiver.approve")}</Btn>
                        <Btn size="sm" kind="ghost" onClick={() => setDeciding({ waiver: row, approve: false })}>{t("v2.dlc.waiver.reject")}</Btn>
                      </span>
                    ) : row.active && mayApprove ? (
                      <Btn size="sm" kind="ghost" onClick={() => setRevoking(row)}>{t("v2.dlc.waiver.revoke")}</Btn>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {state.waivers.some((w) => (w.times_waived ?? 0) > 1) && <div style={{ padding: 14 }}><Notice s="wait">{t("v2.dlc.waiver.repeatWarn")}</Notice></div>}
      </Panel>

      {asking && (
        <Dialog
          title={t("v2.dlc.waiver.requestTitle")}
          onClose={() => setAsking(false)}
          foot={
            <>
              <Btn kind="ghost" onClick={() => setAsking(false)}>{t("v3.common.cancel")}</Btn>
              <Btn kind="primary" busy={busy} disabled={!form.criterion_key || form.reason.trim() === "" || form.risk_owner.trim() === ""}
                disabledReason={firstReason(
                  !form.criterion_key && t("story.reason.pickCriterion"),
                  form.reason.trim() === "" && t("story.reason.fillReason"),
                  form.risk_owner.trim() === "" && t("story.reason.fillRiskOwner"),
                )}
                onClick={() => {
                  setAsking(false);
                  const row = rows.find((r) => r.key === form.criterion_key);
                  const expires = new Date(Date.now() + form.days * 86400000).toISOString();
                  run(() => dlcApi.requestWaiver(state.agent_id, {
                    criterion_key: form.criterion_key,
                    actual: typeof row?.measured === "number" ? row.measured : null,
                    threshold: row?.threshold ?? null,
                    reason: form.reason,
                    risk_owner: form.risk_owner,
                    compensating_control: form.compensating_control,
                    expires_on: expires,
                  }), "v2.dlc.waiver.requested");
                }}>
                {t("v2.dlc.waiver.request")}
              </Btn>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <Notice s="wait">{t("v2.dlc.waiver.neverRedline")}</Notice>
            <label className="v3-field">
              <span>{t("v2.dlc.criteria.key")}</span>
              <Select className="v3-select" value={form.criterion_key} onChange={(e) => setForm({ ...form, criterion_key: e.target.value })}>
                {waivable.map((r) => <option key={r.key} value={r.key}>{`${r.key} — ${r.text}`}</option>)}
              </Select>
            </label>
            <label className="v3-field">
              <span>{t("v2.dlc.waiver.reason")}</span>
              <textarea className="v3-input" rows={3} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
              <small className="v3-hint">{t("v2.dlc.waiver.reasonHint")}</small>
            </label>
            <div className="v3-grid c2">
              <label className="v3-field">
                <span>{t("v2.dlc.waiver.riskOwner")}</span>
                <input className="v3-input" value={form.risk_owner} onChange={(e) => setForm({ ...form, risk_owner: e.target.value })} />
              </label>
              <label className="v3-field">
                <span>{t("v2.dlc.waiver.days")}</span>
                <input className="v3-input" type="number" min={1} max={30} value={form.days} onChange={(e) => setForm({ ...form, days: Number(e.target.value) })} />
              </label>
            </div>
            <label className="v3-field">
              <span>{t("v2.dlc.waiver.control")}</span>
              <input className="v3-input" value={form.compensating_control} onChange={(e) => setForm({ ...form, compensating_control: e.target.value })} />
            </label>
          </div>
        </Dialog>
      )}
      {deciding && (
        <Confirm
          title={deciding.approve ? t("v2.dlc.waiver.approveTitle") : t("v2.dlc.waiver.rejectTitle")}
          confirmLabel={deciding.approve ? t("v2.dlc.waiver.approve") : t("v2.dlc.waiver.reject")}
          cancelLabel={t("v3.common.cancel")}
          danger={!deciding.approve}
          busy={busy}
          onCancel={() => setDeciding(null)}
          onConfirm={() => {
            const target = deciding;
            setDeciding(null);
            run(() => (target.approve ? dlcApi.approveWaiver(target.waiver.id, "") : dlcApi.rejectWaiver(target.waiver.id, "")),
              target.approve ? "v2.dlc.waiver.approved" : "v2.dlc.waiver.rejected");
          }}
        >
          <dl className="v3-kv">
            <dt>{t("v2.dlc.criteria.key")}</dt><dd className="mono">{deciding.waiver.criterion_key}</dd>
            <dt>{t("v2.dlc.waiver.reason")}</dt><dd>{deciding.waiver.reason}</dd>
            <dt>{t("v2.dlc.waiver.riskOwner")}</dt><dd>{deciding.waiver.risk_owner}</dd>
            <dt>{t("v2.dlc.waiver.control")}</dt><dd>{deciding.waiver.compensating_control || "—"}</dd>
            <dt>{t("v2.dlc.waiver.expires")}</dt><dd>{deciding.waiver.expires_on?.slice(0, 10) ?? "—"}</dd>
          </dl>
        </Confirm>
      )}
      {revoking && (
        <Confirm title={t("v2.dlc.waiver.revoke")} confirmLabel={t("v2.dlc.waiver.revoke")} cancelLabel={t("v3.common.cancel")} danger busy={busy}
          onCancel={() => setRevoking(null)}
          onConfirm={() => { const w = revoking; setRevoking(null); run(() => dlcApi.revokeWaiver(w.id), "v2.dlc.waiver.revoked"); }}>
          {t("v3.standards.revokeWaiverBody", { key: revoking.criterion_key })}
        </Confirm>
      )}
    </>
  );
}

/** The workspace release policy (administrator). The PUT replaces it wholesale, so other keys go back untouched. */
function PolicyPanel() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const toast = useToast();
  const failed = useFailure();
  const { current } = useWorkspace();
  const workspaceId = current?.id ?? "default";
  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  const policy = useLoad(() => (isAdmin ? dlcApi.releasePolicy(workspaceId) : Promise.resolve(null)), `v3-std-policy:${workspaceId}:${nonce}`);
  const [form, setForm] = useState<{ mode: string; period: number; floor: number; confirm: string; max: string } | null>(null);
  useEffect(() => {
    const raw = policy.data?.policy;
    if (!raw) return;
    const cal = (raw.calibration ?? {}) as { period_days?: number; kappa_floor?: number };
    const num = (v: unknown) => (v === undefined || v === null ? "" : String(v));
    setForm({ mode: String(raw.release_mode ?? "direct"), period: cal.period_days ?? 90, floor: cal.kappa_floor ?? 0.6, confirm: num(raw.eval_cost_confirm_usd), max: num(raw.eval_cost_max_usd) });
  }, [policy.data]);
  const data = policy.data;
  if (!isAdmin || !data || !form) return null;

  const save = () => {
    setBusy(true);
    dlcApi
      .putReleasePolicy(workspaceId, {
        ...data.policy,
        release_mode: form.mode,
        calibration: { period_days: form.period, kappa_floor: form.floor },
        eval_cost_confirm_usd: form.confirm === "" ? null : Number(form.confirm),
        eval_cost_max_usd: form.max === "" ? null : Number(form.max),
      })
      .then(() => { toast("ok", t("v2.dlc.policy.saved")); setNonce((n) => n + 1); })
      .catch((err: unknown) => failed.fail(err))
      .finally(() => setBusy(false));
  };

  return (
    <Panel title={t("v2.dlc.policy.title")} end={<Chip>{data.tier}</Chip>}>
      <p className="v3-std-muted" style={{ margin: "0 0 12px" }}>{t("v2.dlc.policy.sub", { tier: data.tier })}</p>
      <ErrorCallout error={failed.error} params={{ workspace_id: workspaceId }} onDismiss={failed.clear} data-testid="v3-release-policy-error" />
      <div className="v3-grid c3">
        <label className="v3-field">
          <span>{t("v2.dlc.policy.mode")}</span>
          <Select className="v3-select" value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}>
            {["direct", "gated"].map((m) => <option key={m} value={m}>{t(`v2.dlc.release.mode.${m}`)}</option>)}
          </Select>
          <small className="v3-hint">{t("v2.dlc.policy.modeHint")}</small>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.policy.period")}</span>
          <input className="v3-input" type="number" min={7} max={365} value={form.period} onChange={(e) => setForm({ ...form, period: Number(e.target.value) })} />
          <small className="v3-hint">{t("v2.dlc.policy.periodHint")}</small>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.policy.floor")}</span>
          <input className="v3-input" type="number" min={0.2} max={0.95} step={0.05} value={form.floor} onChange={(e) => setForm({ ...form, floor: Number(e.target.value) })} />
          <small className="v3-hint">{t("v2.dlc.policy.floorHint")}</small>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.policy.confirm")}</span>
          <input className="v3-input" type="number" min={0} step={0.5} value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} />
          <small className="v3-hint">{t("v2.dlc.policy.confirmHint")}</small>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.policy.max")}</span>
          <input className="v3-input" type="number" min={0} step={1} value={form.max} onChange={(e) => setForm({ ...form, max: e.target.value })} />
          <small className="v3-hint">{t("v2.dlc.policy.maxHint")}</small>
        </label>
      </div>
      <div style={{ marginTop: 14 }}><Btn kind="primary" disabled={busy} onClick={save}>{t("v3.standards.save")}</Btn></div>
    </Panel>
  );
}

export function Release({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  const [repeats, setRepeats] = useState(1);
  const [confirmMigrate, setConfirmMigrate] = useState(false);
  const state = useLoad<ReleaseState>(() => dlcApi.release(agentId), `v3-std-rel:${agentId}:${nonce}`);
  const pending = state.data?.pending ?? null;
  // only ask for the report once runs exist: before that the server answers 409
  const gated = Boolean(pending && (pending.run_ids ?? []).length > 0);
  const gate = useLoad(() => (gated ? dlcApi.gate(agentId) : Promise.resolve(null)), `v3-std-gate:${agentId}:${pending?.id ?? "none"}:${gated}:${nonce}`);
  const estimate = useLoad(() => dlcApi.estimate({ agent_id: agentId, repeats, release: true }), `v3-std-est:${agentId}:${repeats}`);
  const report = gate.data?.report ?? null;
  const rows = report?.criteria ?? [];
  const evaluating = gate.data?.status === "evaluating";

  // evaluate / migrate fail in place, with the way out (storyline-ux §4)
  const [failure, setFailure] = useState<unknown>(null);
  const run = (fn: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    setFailure(null);
    fn()
      .then(() => { toast("ok", t(okKey)); setNonce((n) => n + 1); })
      .catch((err: unknown) => setFailure(err))
      .finally(() => setBusy(false));
  };

  if (state.loading && !state.data) return <Skeleton rows={6} />;
  if (!state.data) return <Notice s="act">{state.error ?? t("v2.dlc.release.notFound")}</Notice>;
  const live = state.data.state;
  const est = estimate.data;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <ErrorCallout error={failure} params={{ agent_id: agentId }} onDismiss={() => setFailure(null)}
        data-testid="v3-release-error" />
      <div className="v3-grid c4">
        <Panel signal={live.endpoint_mode === "live" ? "ok" : "wait"}>
          <Stat label={t("v2.dlc.release.endpointModeLabel")} value={t(`v2.dlc.release.endpointMode.${live.endpoint_mode}`)} foot={t(`v2.dlc.release.mode.${state.data.release_mode}`)} />
        </Panel>
        <Panel><Stat label={t("v2.dlc.release.liveVersion")} value={live.live_version ?? live.ledger_version ?? "—"} /></Panel>
        <Panel signal={pending ? "wait" : undefined}><Stat label={t("v2.dlc.release.candidateVersion")} value={live.candidate_version ?? "—"} /></Panel>
        <Panel signal={state.data.criteria_set?.signed_by ? "ok" : "wait"}>
          <Stat label={t("v2.dlc.release.criteria")} value={state.data.criteria_set ? `v${state.data.criteria_set.version}` : "—"}
            foot={state.data.criteria_set?.signed_by ?? t("v2.dlc.criteria.unsigned")} />
        </Panel>
      </div>

      {live.endpoint_mode !== "live" && (
        <Notice s="wait">
          {t("v2.dlc.release.defaultWarn")}
          {!live.gateable && live.gateable_reason ? ` ${live.gateable_reason}` : ""}{" "}
          {/* moving production onto named endpoints is a release decision */}
          {can("release.sign") && (
            <Btn size="sm" busy={busy} disabled={!live.gateable} disabledReason={live.gateable_reason ?? t("story.reason.unavailable")}
              onClick={() => setConfirmMigrate(true)}>
              {t("v2.dlc.release.migrate")}
            </Btn>
          )}
        </Notice>
      )}
      {live.error && <Notice s="act">{live.error}</Notice>}

      <Panel
        title={pending ? t("v2.dlc.release.pendingTitle", { version: pending.candidate_version ?? "—" }) : t("v2.dlc.release.nothingPending")}
        signal={report ? gateSignal(report.verdict) : pending ? "wait" : undefined}
        end={
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
            {report && <Chip s={gateSignal(report.verdict)}>{report.verdict}</Chip>}
            <Link to={`/v3/gate?agent=${encodeURIComponent(agentId)}`} className="v3-btn sm"><Scale size={13} /> {t("v3.standards.openGate")} <ArrowRight size={13} /></Link>
          </span>
        }
      >
        {!pending ? (
          <p className="v3-std-muted" style={{ margin: 0 }}>{t("v2.dlc.release.nothingPendingHint")}</p>
        ) : pending.run_ids.length === 0 ? (
          <div style={{ display: "grid", gap: 12 }}>
            <Notice>{t("v2.dlc.release.notEvaluated")}</Notice>
            <div className="v3-grid c3" style={{ alignItems: "end" }}>
              <label className="v3-field">
                <span>{t("v2.dlc.release.repeats")}</span>
                <input className="v3-input" type="number" min={1} max={10} value={repeats} onChange={(e) => setRepeats(Math.max(1, Math.min(10, Number(e.target.value))))} />
                <small className="v3-hint">{t("v2.dlc.release.repeatsHint")}</small>
              </label>
              <Stat label={t("v2.dlc.cost.estimate")}
                value={estimateValue(est, t("v2.dlc.cost.unknown"))}
                signal={est?.over_limit ? "act" : est?.confirm_required ? "wait" : undefined}
                foot={est ? t("v2.dlc.cost.sessions", { sessions: est.sessions ?? 0, minutes: est.duration_minutes ?? 0 }) : undefined} />
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {est?.unpriced && <Chip s="wait">{t("v2.dlc.cost.unpriced")}</Chip>}
                {est?.over_limit && <Chip s="act">{t("v2.dlc.cost.overLimit")}</Chip>}
                {est?.confirm_required && !est.over_limit && <Chip s="wait">{t("v3.standards.costConfirm")}</Chip>}
              </div>
            </div>
            {repeats > 1 && <Notice s="wait">{t("v2.dlc.release.passKCost", { k: repeats })}</Notice>}
            <div>
              {/* over the ceiling is refused; above the confirm line the click is the confirmation */}
              <Btn kind="primary" busy={busy} disabled={est?.over_limit} disabledReason={t("story.reason.overCostLimit")}
                onClick={() => run(() => dlcApi.evaluateRelease(agentId, { repeats, confirm_cost: Boolean(est?.confirm_required) }), "v2.dlc.release.evaluating")}>
                {t("v2.dlc.release.evaluate")}
              </Btn>
            </div>
          </div>
        ) : evaluating ? (
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <Lamp s="wait" live /> <span>{t("v2.dlc.release.waitingRuns")}</span>
            <Btn size="sm" kind="ghost" onClick={() => setNonce((n) => n + 1)}>{t("v3.standards.refresh")}</Btn>
          </div>
        ) : report ? (
          <div style={{ display: "grid", gap: 10 }}>
            {report.verdict === "INVALID" && (
              <Notice s="wait">{t("v2.dlc.release.invalidHint")}{report.provenance.issues.length > 0 ? ` ${report.provenance.issues.join("; ")}` : ""}</Notice>
            )}
            {report.verdict === "INVALID" && canRunGate(pending) && (
              <div>
                <Btn kind="primary" busy={busy} disabled={est?.over_limit} disabledReason={t("story.reason.overCostLimit")}
                  onClick={() => run(() => dlcApi.evaluateRelease(agentId, { repeats, confirm_cost: Boolean(est?.confirm_required) }), "v2.dlc.release.evaluating")}>
                  {t("v2.dlc.release.rerun")}
                </Btn>
              </div>
            )}
            {report.verdict === "BLOCKED" && report.redline_violations.length > 0 && (
              <Notice s="act">{t("v2.dlc.release.redlineHint", { keys: report.redline_violations.join(", ") })}</Notice>
            )}
            <dl className="v3-kv">
              <dt>{t("v2.dlc.release.criteriaVersion")}</dt><dd>v{report.provenance.criteria_set_version ?? "—"}</dd>
              <dt>{t("v2.dlc.release.signedBy")}</dt><dd>{report.provenance.criteria_signed_by ?? t("v2.dlc.criteria.unsigned")}</dd>
              <dt>{t("v2.dlc.release.goldenVersions")}</dt>
              <dd className="mono">{goldenVersionsLabel(report.provenance.golden_versions)}</dd>
              <dt>{t("v2.dlc.release.evaluatorHash")}</dt><dd className="mono">{report.provenance.evaluator_set_hash?.slice(0, 12) ?? "—"}</dd>
              <dt>{t("v2.dlc.release.decidedAt")}</dt><dd>{stamp(report.decided_at, 19)}</dd>
            </dl>
            <p className="v3-std-muted" style={{ margin: 0 }}>{t("v3.standards.gateHandoff")}</p>
          </div>
        ) : (
          <Skeleton rows={2} />
        )}
      </Panel>

      <Waivers state={state.data} rows={rows} onReload={() => setNonce((n) => n + 1)} />
      <PolicyPanel />

      <Panel title={t("v2.dlc.release.history")} flush>
        {state.data.records.length === 0 ? (
          <Empty title={t("v2.dlc.release.noHistory")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.dlc.release.version")}</th>
                <th>{t("v2.dlc.release.decisionCol")}</th>
                <th>{t("v2.dlc.release.decidedBy")}</th>
                <th>{t("v2.dlc.release.decidedAt")}</th>
                <th>{t("v2.dlc.release.note")}</th>
              </tr>
            </thead>
            <tbody>
              {state.data.records.map((row) => (
                <tr key={row.id}>
                  <td style={{ width: 30 }}><Lamp s={decisionSignal(row.decision)} /></td>
                  <td className="mono">{row.candidate_version ?? "—"}</td>
                  <td><Chip s={decisionSignal(row.decision)}>{t(`v2.dlc.release.decision.${row.decision}`)}</Chip></td>
                  <td>{row.decided_by ?? row.requested_by}</td>
                  <td>{stamp(row.decided_at, 19)}</td>
                  <td className="v3-std-muted">{row.note || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {confirmMigrate && (
        <Confirm title={t("v2.dlc.release.migrate")} confirmLabel={t("v2.dlc.release.migrate")} cancelLabel={t("v3.common.cancel")} busy={busy}
          onCancel={() => setConfirmMigrate(false)}
          onConfirm={() => { setConfirmMigrate(false); run(() => dlcApi.migrateRelease(agentId), "v2.dlc.release.migrated"); }}>
          {t("v3.standards.migrateBody")}
        </Confirm>
      )}
    </div>
  );
}
