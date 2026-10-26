/**
 * The gate report and sign-off — §6 and §7.5.
 *
 * The decision this page supports is "does this version go to production", and the
 * methodology's answer is that the gate decides, not the room. So the page shows the
 * four gates in their fixed order (red line → denominator → per-dimension → observe)
 * and the provenance that makes the verdict trustworthy: which criteria version, who
 * signed it, which golden versions, which endpoint the runs actually invoked.
 *
 * Three readings are deliberately kept apart:
 * - **BLOCKED** — a standard was not met;
 * - **INVALID** — the evidence cannot decide (missing verdicts, too many undetermined,
 *   an unsigned standard), which is *not* a failure and must not be waived;
 * - **PASS** — and even then a person signs, because releasing is accountable.
 *
 * Traffic only moves when `live` is re-pointed, so a candidate can be gated without
 * a single user seeing it, and a rollback is re-pointing `live` back.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import { storedWorkspaceId } from "../../../lib/workspace-header";
import { type GateRow, type ReleaseState, type Waiver, canRunGate, gateTone, goldenVersionsLabel } from "../../../lib/dlc";
import { useLoad, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  Confirm,
  Descriptions,
  Field,
  Kpi,
  LinkButton,
  Modal,
  Select,
  Spin,
  Table,
  Tag,
} from "../../ui";
import { CiBar, TierChip } from "./charts";

const pct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;

function GateRows({ rows }: { rows: GateRow[] }) {
  const { t } = useTranslation();
  return (
    <Table
      rows={rows}
      rowKey={(row) => row.key}
      empty={t("v2.dlc.release.noRows")}
      columns={[
        {
          key: "key",
          title: t("v2.dlc.criteria.key"),
          render: (row) => (
            <div className="v2-dlc-qcell">
              <span className="mono">{row.key}</span>
              <TierChip tier={row.tier} effective={row.effective_tier} />
            </div>
          ),
        },
        { key: "text", title: t("v2.dlc.criteria.text"), render: (row) => row.text },
        {
          key: "verdict",
          title: t("v2.dlc.release.verdict"),
          render: (row) => (
            <>
              <Tag tone={gateTone(row.verdict)}>{t(`v2.dlc.release.rowVerdict.${row.verdict}`)}</Tag>
              {row.reason && <span className="v2-muted"> {row.reason}</span>}
            </>
          ),
        },
        {
          key: "measured",
          title: t("v2.dlc.release.measured"),
          render: (row) =>
            typeof row.measured === "number" || row.n ? (
              <CiBar
                rate={typeof row.measured === "number" ? row.measured : null}
                low={row.wilson_low}
                high={row.wilson_high}
                threshold={row.threshold}
                undecided={row.threshold_inside_ci}
                n={row.n}
              />
            ) : (
              <span className="v2-muted">{t("v2.dlc.noResult")}</span>
            ),
        },
        {
          key: "denom",
          title: t("v2.dlc.release.denominator"),
          render: (row) => (
            <span className="v2-dlc-denom">
              {row.n ?? 0} / {row.expected ?? 0}
              {(row.missing ?? 0) > 0 && <Tag tone="orange">{t("v2.dlc.release.missing", { n: row.missing })}</Tag>}
              {(row.undetermined_rate ?? 0) > 0 && (
                <span className="v2-muted">{t("v2.dlc.release.undetermined", { v: pct(row.undetermined_rate) })}</span>
              )}
            </span>
          ),
        },
        {
          key: "trend",
          title: t("v2.dlc.release.trend"),
          render: (row) =>
            typeof row.trend === "number" ? (
              <span className={row.trend >= 0 ? "v2-dlc-up" : "v2-dlc-down"}>
                {row.trend >= 0 ? "+" : ""}
                {(row.trend * 100).toFixed(1)}pp
              </span>
            ) : (
              "—"
            ),
        },
      ]}
    />
  );
}

function WaiverPanel({
  state,
  rows,
  onReload,
}: {
  state: ReleaseState;
  rows: GateRow[];
  onReload: () => void;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const [asking, setAsking] = useState(false);
  const [deciding, setDeciding] = useState<{ waiver: Waiver; approve: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    criterion_key: "",
    reason: "",
    risk_owner: "",
    compensating_control: "",
    days: 7,
  });
  const mayApprove = can("waiver.approve");
  // a red line is never waivable, so it is not even offered
  const waivable = rows.filter((r) => r.tier !== "redline" && r.verdict === "BLOCKED");

  const run = (fn: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    fn()
      .then(() => {
        toast("success", t(okKey));
        onReload();
      })
      .catch((error: unknown) => toast("error", errorMessage(error)))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <Card
        title={t("v2.dlc.waiver.title")}
        sub={t("v2.dlc.waiver.sub")}
        end={
          waivable.length > 0 ? (
            <Button
              size="sm"
              onClick={() => {
                setForm({ ...form, criterion_key: waivable[0].key });
                setAsking(true);
              }}
            >
              {t("v2.dlc.waiver.request")}
            </Button>
          ) : undefined
        }
      >
        <Table
          rows={state.waivers}
          rowKey={(row) => row.id}
          empty={t("v2.dlc.waiver.none")}
          columns={[
            { key: "key", title: t("v2.dlc.criteria.key"), render: (row) => <span className="mono">{row.criterion_key}</span> },
            {
              key: "status",
              title: t("v2.common.status"),
              render: (row) => (
                <Tag tone={row.active ? "blue" : row.status === "approved" ? "gray" : row.status === "rejected" ? "red" : "orange"}>
                  {t(`v2.dlc.waiver.status.${row.status}`)}
                </Tag>
              ),
            },
            {
              key: "gap",
              title: t("v2.dlc.waiver.gap"),
              render: (row) =>
                row.actual !== null && row.threshold !== null
                  ? `${pct(row.actual)} / ${pct(row.threshold, 0)}`
                  : "—",
            },
            { key: "owner", title: t("v2.dlc.waiver.riskOwner"), render: (row) => row.risk_owner },
            { key: "expires", title: t("v2.dlc.waiver.expires"), render: (row) => row.expires_on?.slice(0, 10) ?? "—" },
            {
              key: "times",
              title: t("v2.dlc.waiver.times"),
              render: (row) =>
                (row.times_waived ?? 0) > 1 ? (
                  <Tag tone="orange">{t("v2.dlc.waiver.repeat", { n: row.times_waived })}</Tag>
                ) : (
                  String(row.times_waived ?? 1)
                ),
            },
            {
              key: "act",
              title: "",
              render: (row) =>
                row.status === "requested" && mayApprove ? (
                  <>
                    <LinkButton onClick={() => setDeciding({ waiver: row, approve: true })}>
                      {t("v2.dlc.waiver.approve")}
                    </LinkButton>
                    <LinkButton danger onClick={() => setDeciding({ waiver: row, approve: false })}>
                      {t("v2.dlc.waiver.reject")}
                    </LinkButton>
                  </>
                ) : row.active && mayApprove ? (
                  <LinkButton danger onClick={() => run(() => dlcApi.revokeWaiver(row.id), "v2.dlc.waiver.revoked")}>
                    {t("v2.dlc.waiver.revoke")}
                  </LinkButton>
                ) : null,
            },
          ]}
        />
        {state.waivers.some((w) => (w.times_waived ?? 0) > 1) && (
          <Alert tone="warn">{t("v2.dlc.waiver.repeatWarn")}</Alert>
        )}
      </Card>

      <Modal
        open={asking}
        title={t("v2.dlc.waiver.requestTitle")}
        onClose={() => setAsking(false)}
        footer={
          <>
            <Button onClick={() => setAsking(false)}>{t("v2.common.cancel")}</Button>
            <Button
              kind="primary"
              disabled={busy || !form.criterion_key || form.reason.trim() === "" || form.risk_owner.trim() === ""}
              onClick={() => {
                setAsking(false);
                const row = rows.find((r) => r.key === form.criterion_key);
                const expires = new Date(Date.now() + form.days * 86400000).toISOString();
                void run(
                  () =>
                    dlcApi.requestWaiver(state.agent_id, {
                      criterion_key: form.criterion_key,
                      actual: typeof row?.measured === "number" ? row.measured : null,
                      threshold: row?.threshold ?? null,
                      reason: form.reason,
                      risk_owner: form.risk_owner,
                      compensating_control: form.compensating_control,
                      expires_on: expires,
                    }),
                  "v2.dlc.waiver.requested",
                );
              }}
            >
              {t("v2.dlc.waiver.request")}
            </Button>
          </>
        }
      >
        <Alert tone="warn">{t("v2.dlc.waiver.neverRedline")}</Alert>
        <Field label={t("v2.dlc.criteria.key")} required>
          <Select
            value={form.criterion_key}
            onChange={(v) => setForm({ ...form, criterion_key: v })}
            options={waivable.map((r) => ({ value: r.key, label: `${r.key} — ${r.text}` }))}
          />
        </Field>
        <Field label={t("v2.dlc.waiver.reason")} required hint={t("v2.dlc.waiver.reasonHint")}>
          <textarea className="v2-input" rows={3} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
        </Field>
        <Field label={t("v2.dlc.waiver.riskOwner")} required hint={t("v2.dlc.waiver.riskOwnerHint")}>
          <input className="v2-input" value={form.risk_owner} onChange={(e) => setForm({ ...form, risk_owner: e.target.value })} />
        </Field>
        <Field label={t("v2.dlc.waiver.control")} hint={t("v2.dlc.waiver.controlHint")}>
          <input
            className="v2-input"
            value={form.compensating_control}
            onChange={(e) => setForm({ ...form, compensating_control: e.target.value })}
          />
        </Field>
        <Field label={t("v2.dlc.waiver.days")} hint={t("v2.dlc.waiver.daysHint")}>
          <input
            className="v2-input"
            type="number"
            min={1}
            max={30}
            value={form.days}
            onChange={(e) => setForm({ ...form, days: Number(e.target.value) })}
          />
        </Field>
      </Modal>

      <Confirm
        open={deciding !== null}
        title={deciding?.approve ? t("v2.dlc.waiver.approveTitle") : t("v2.dlc.waiver.rejectTitle")}
        confirmLabel={deciding?.approve ? t("v2.dlc.waiver.approve") : t("v2.dlc.waiver.reject")}
        danger={deciding?.approve === false}
        busy={busy}
        onClose={() => setDeciding(null)}
        onConfirm={() => {
          const target = deciding;
          setDeciding(null);
          if (!target) return;
          void run(
            () =>
              target.approve
                ? dlcApi.approveWaiver(target.waiver.id, "")
                : dlcApi.rejectWaiver(target.waiver.id, ""),
            target.approve ? "v2.dlc.waiver.approved" : "v2.dlc.waiver.rejected",
          );
        }}
        body={
          deciding ? (
            <Descriptions
              one
              items={[
                { label: t("v2.dlc.criteria.key"), value: deciding.waiver.criterion_key },
                { label: t("v2.dlc.waiver.reason"), value: deciding.waiver.reason },
                { label: t("v2.dlc.waiver.riskOwner"), value: deciding.waiver.risk_owner },
                { label: t("v2.dlc.waiver.control"), value: deciding.waiver.compensating_control || "—" },
                { label: t("v2.dlc.waiver.expires"), value: deciding.waiver.expires_on?.slice(0, 10) ?? "—" },
              ]}
            />
          ) : null
        }
      />
    </>
  );
}


/**
 * The workspace's release policy, as far as Agent-DLC reads it (administrator).
 * The PUT replaces the policy wholesale, so the other keys are sent back untouched.
 */
function PolicyCard() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const toast = useV2Toast();
  const workspaceId = storedWorkspaceId() ?? "default";
  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  const policy = useLoad(
    () => (isAdmin ? dlcApi.releasePolicy(workspaceId) : Promise.resolve(null)),
    `release-policy:${workspaceId}:${nonce}`,
  );
  const [form, setForm] = useState<{ mode: string; period: number; floor: number; confirm: string; max: string } | null>(
    null,
  );
  useEffect(() => {
    const raw = policy.data?.policy;
    if (!raw) return;
    const cal = (raw.calibration ?? {}) as { period_days?: number; kappa_floor?: number };
    const num = (v: unknown) => (v === undefined || v === null ? "" : String(v));
    setForm({
      mode: String(raw.release_mode ?? "direct"),
      period: cal.period_days ?? 90,
      floor: cal.kappa_floor ?? 0.6,
      confirm: num(raw.eval_cost_confirm_usd),
      max: num(raw.eval_cost_max_usd),
    });
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
      .then(() => {
        toast("success", t("v2.dlc.policy.saved"));
        setNonce((n) => n + 1);
      })
      .catch((error: unknown) => toast("error", errorMessage(error)))
      .finally(() => setBusy(false));
  };

  return (
    <Card title={t("v2.dlc.policy.title")} sub={t("v2.dlc.policy.sub", { tier: data.tier })}>
      <div className="v2-dlc-grid3">
        <Field label={t("v2.dlc.policy.mode")} hint={t("v2.dlc.policy.modeHint")}>
          <Select
            value={form.mode}
            onChange={(v) => setForm({ ...form, mode: v })}
            options={["direct", "gated"].map((m) => ({ value: m, label: t(`v2.dlc.release.mode.${m}`) }))}
          />
        </Field>
        <Field label={t("v2.dlc.policy.period")} hint={t("v2.dlc.policy.periodHint")}>
          <input
            className="v2-input"
            type="number"
            min={7}
            max={365}
            value={form.period}
            onChange={(e) => setForm({ ...form, period: Number(e.target.value) })}
          />
        </Field>
        <Field label={t("v2.dlc.policy.floor")} hint={t("v2.dlc.policy.floorHint")}>
          <input
            className="v2-input"
            type="number"
            min={0.2}
            max={0.95}
            step={0.05}
            value={form.floor}
            onChange={(e) => setForm({ ...form, floor: Number(e.target.value) })}
          />
        </Field>
        <Field label={t("v2.dlc.policy.confirm")} hint={t("v2.dlc.policy.confirmHint")}>
          <input
            className="v2-input"
            type="number"
            min={0}
            step={0.5}
            value={form.confirm}
            onChange={(e) => setForm({ ...form, confirm: e.target.value })}
          />
        </Field>
        <Field label={t("v2.dlc.policy.max")} hint={t("v2.dlc.policy.maxHint")}>
          <input
            className="v2-input"
            type="number"
            min={0}
            step={1}
            value={form.max}
            onChange={(e) => setForm({ ...form, max: e.target.value })}
          />
        </Field>
      </div>
      <div className="v2-dlc-actions">
        <Button kind="primary" disabled={busy} onClick={save}>
          {t("v2.common.save")}
        </Button>
      </div>
    </Card>
  );
}

export function ReleaseGate({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  const [repeats, setRepeats] = useState(1);
  const [signing, setSigning] = useState<"sign" | "block" | "rollback" | null>(null);
  const [note, setNote] = useState("");
  const state = useLoad<ReleaseState>(() => dlcApi.release(agentId), `release:${agentId}:${nonce}`);
  const pending = state.data?.pending ?? null;
  // only ask for the report once runs exist: before that the server answers 409
  // `release.not_evaluated`, which is an expected state, not something to report
  const gated = Boolean(pending && (pending.run_ids ?? []).length > 0);
  const gate = useLoad(
    () => (gated ? dlcApi.gate(agentId) : Promise.resolve(null)),
    `gate:${agentId}:${pending?.id ?? "none"}:${gated}:${nonce}`,
  );
  const estimate = useLoad(
    () => dlcApi.estimate({ agent_id: agentId, repeats, release: true }),
    `estimate:${agentId}:${repeats}`,
  );
  const maySign = can("release.sign");
  const report = gate.data?.report ?? null;
  const rows = report?.criteria ?? [];
  const evaluating = gate.data?.status === "evaluating";

  const run = (fn: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    fn()
      .then(() => {
        toast("success", t(okKey));
        setNonce((n) => n + 1);
      })
      .catch((error: unknown) => toast("error", errorMessage(error)))
      .finally(() => setBusy(false));
  };

  if (state.loading && !state.data) return <Spin />;
  if (!state.data) return <Alert tone="error">{state.error ?? t("v2.dlc.release.notFound")}</Alert>;
  const live = state.data.state;

  return (
    <>
      <Card
        title={t("v2.dlc.release.title")}
        sub={t(`v2.dlc.release.mode.${state.data.release_mode}`)}
        end={
          <Tag tone={live.endpoint_mode === "live" ? "green" : "gray"}>
            {t(`v2.dlc.release.endpointMode.${live.endpoint_mode}`)}
          </Tag>
        }
      >
        <div className="v2-dlc-kpis">
          <Kpi label={t("v2.dlc.release.liveVersion")} value={live.live_version ?? live.ledger_version ?? "—"} />
          <Kpi label={t("v2.dlc.release.candidateVersion")} value={live.candidate_version ?? "—"} />
          <Kpi
            label={t("v2.dlc.release.criteria")}
            value={
              state.data.criteria_set
                ? `v${state.data.criteria_set.version}${state.data.criteria_set.signed_by ? "" : " ⚠"}`
                : "—"
            }
            sub={state.data.criteria_set?.signed_by ?? t("v2.dlc.criteria.unsigned")}
          />
        </div>
        {live.endpoint_mode !== "live" && (
          <Alert
            tone="warn"
            action={
              // moving production onto named endpoints is a release decision
              can("release.sign") ? (
                <LinkButton
                  disabled={busy || !live.gateable}
                  title={live.gateable_reason ?? undefined}
                  onClick={() => run(() => dlcApi.migrateRelease(agentId), "v2.dlc.release.migrated")}
                >
                  {t("v2.dlc.release.migrate")}
                </LinkButton>
              ) : undefined
            }
          >
            {t("v2.dlc.release.defaultWarn")}
            {!live.gateable && live.gateable_reason ? ` ${live.gateable_reason}` : ""}
          </Alert>
        )}
        {live.error && <Alert tone="error">{live.error}</Alert>}
      </Card>

      {pending ? (
        <Card
          title={t("v2.dlc.release.pendingTitle", { version: pending.candidate_version ?? "—" })}
          sub={t(`v2.dlc.release.decision.${pending.decision}`)}
          end={report ? <Tag tone={gateTone(report.verdict)}>{report.verdict}</Tag> : undefined}
        >
          {pending.run_ids.length === 0 ? (
            <>
              <Alert>{t("v2.dlc.release.notEvaluated")}</Alert>
              <div className="v2-dlc-grid3">
                <Field label={t("v2.dlc.release.repeats")} hint={t("v2.dlc.release.repeatsHint")}>
                  <input
                    className="v2-input"
                    type="number"
                    min={1}
                    max={10}
                    value={repeats}
                    onChange={(e) => setRepeats(Math.max(1, Math.min(10, Number(e.target.value))))}
                  />
                </Field>
                <Field label={t("v2.dlc.cost.estimate")} hint={t("v2.dlc.cost.basisHint")}>
                  <div className="v2-dlc-cost">
                    <b>
                      {estimate.data?.total_usd === null || estimate.data?.total_usd === undefined
                        ? t("v2.dlc.cost.unknown")
                        : `$${estimate.data.total_usd.toFixed(2)}`}
                    </b>
                    <span className="v2-muted">
                      {t("v2.dlc.cost.sessions", {
                        sessions: estimate.data?.sessions ?? 0,
                        minutes: estimate.data?.duration_minutes ?? 0,
                      })}
                    </span>
                    {estimate.data?.unpriced && <Tag tone="orange">{t("v2.dlc.cost.unpriced")}</Tag>}
                    {estimate.data?.over_limit && <Tag tone="red">{t("v2.dlc.cost.overLimit")}</Tag>}
                  </div>
                </Field>
              </div>
              {repeats > 1 && <Alert tone="warn">{t("v2.dlc.release.passKCost", { k: repeats })}</Alert>}
              <div className="v2-dlc-actions">
                <Button
                  kind="primary"
                  disabled={busy || estimate.data?.over_limit}
                  onClick={() =>
                    run(
                      () =>
                        dlcApi.evaluateRelease(agentId, {
                          repeats,
                          confirm_cost: Boolean(estimate.data?.confirm_required),
                        }),
                      "v2.dlc.release.evaluating",
                    )
                  }
                >
                  {t("v2.dlc.release.evaluate")}
                </Button>
              </div>
            </>
          ) : evaluating ? (
            <>
              <Spin label={t("v2.dlc.release.waitingRuns")} />
              <Button size="sm" onClick={() => setNonce((n) => n + 1)}>
                {t("v2.common.refresh")}
              </Button>
            </>
          ) : report ? (
            <>
              {report.verdict === "INVALID" && (
                <Alert tone="error">
                  {t("v2.dlc.release.invalidHint")}
                  {report.provenance.issues.length > 0 ? ` ${report.provenance.issues.join("; ")}` : ""}
                </Alert>
              )}
              {report.verdict === "INVALID" && canRunGate(pending) && (
                <div className="v2-dlc-actions">
                  <Button
                    kind="primary"
                    disabled={busy || estimate.data?.over_limit}
                    onClick={() =>
                      run(
                        () =>
                          dlcApi.evaluateRelease(agentId, {
                            repeats,
                            confirm_cost: Boolean(estimate.data?.confirm_required),
                          }),
                        "v2.dlc.release.evaluating",
                      )
                    }
                  >
                    {t("v2.dlc.release.rerun")}
                  </Button>
                </div>
              )}
              {report.verdict === "BLOCKED" && report.redline_violations.length > 0 && (
                <Alert tone="error">{t("v2.dlc.release.redlineHint", { keys: report.redline_violations.join(", ") })}</Alert>
              )}
              <Descriptions
                items={[
                  { label: t("v2.dlc.release.gateOrder"), value: report.order.map((o) => t(`v2.dlc.release.gateStep.${o}`)).join(" → ") },
                  { label: t("v2.dlc.release.criteriaVersion"), value: `v${report.provenance.criteria_set_version ?? "—"}` },
                  { label: t("v2.dlc.release.signedBy"), value: report.provenance.criteria_signed_by ?? t("v2.dlc.criteria.unsigned") },
                  {
                    label: t("v2.dlc.release.goldenVersions"),
                    value: goldenVersionsLabel(report.provenance.golden_versions),
                  },
                  { label: t("v2.dlc.release.evaluatorHash"), value: report.provenance.evaluator_set_hash?.slice(0, 12) ?? "—" },
                  { label: t("v2.dlc.release.decidedAt"), value: report.decided_at },
                ]}
              />
              <GateRows rows={rows} />
              <div className="v2-dlc-actions">
                {maySign && (
                  <>
                    <Button
                      kind="primary"
                      disabled={busy || report.verdict !== "PASS"}
                      title={report.verdict !== "PASS" ? t("v2.dlc.release.onlyPassSigns") : undefined}
                      onClick={() => {
                        setNote("");
                        setSigning("sign");
                      }}
                    >
                      {t("v2.dlc.release.sign")}
                    </Button>
                    <Button
                      kind="danger"
                      disabled={busy}
                      onClick={() => {
                        setNote("");
                        setSigning("block");
                      }}
                    >
                      {t("v2.dlc.release.block")}
                    </Button>
                  </>
                )}
                <Button size="sm" disabled={busy} onClick={() => setNonce((n) => n + 1)}>
                  {t("v2.common.refresh")}
                </Button>
              </div>
            </>
          ) : (
            <Spin />
          )}
        </Card>
      ) : (
        <Card title={t("v2.dlc.release.nothingPending")}>
          <p className="v2-dlc-empty">{t("v2.dlc.release.nothingPendingHint")}</p>
        </Card>
      )}

      <WaiverPanel state={state.data} rows={rows} onReload={() => setNonce((n) => n + 1)} />

      <PolicyCard />

      <Card
        title={t("v2.dlc.release.history")}
        end={
          maySign && live.live_version ? (
            <Button
              size="sm"
              kind="danger"
              disabled={busy}
              onClick={() => {
                setNote("");
                setSigning("rollback");
              }}
            >
              {t("v2.dlc.release.rollback")}
            </Button>
          ) : undefined
        }
      >
        <Table
          rows={state.data.records}
          rowKey={(row) => row.id}
          empty={t("v2.dlc.release.noHistory")}
          columns={[
            { key: "v", title: t("v2.dlc.release.version"), render: (row) => row.candidate_version ?? "—" },
            {
              key: "decision",
              title: t("v2.dlc.release.decisionCol"),
              render: (row) => (
                <Tag
                  tone={
                    row.decision === "released"
                      ? "green"
                      : row.decision === "blocked" || row.decision === "invalid"
                        ? "red"
                        : row.decision === "rolled_back"
                          ? "orange"
                          : "gray"
                  }
                >
                  {t(`v2.dlc.release.decision.${row.decision}`)}
                </Tag>
              ),
            },
            { key: "by", title: t("v2.dlc.release.decidedBy"), render: (row) => row.decided_by ?? row.requested_by },
            { key: "at", title: t("v2.dlc.release.decidedAt"), render: (row) => row.decided_at?.slice(0, 19).replace("T", " ") ?? "—" },
            { key: "note", title: t("v2.dlc.release.note"), render: (row) => row.note || "—" },
          ]}
        />
      </Card>

      <Modal
        open={signing !== null}
        title={t(`v2.dlc.release.${signing ?? "sign"}Title`)}
        onClose={() => setSigning(null)}
        footer={
          <>
            <Button onClick={() => setSigning(null)}>{t("v2.common.cancel")}</Button>
            <Button
              kind={signing === "sign" ? "primary" : "danger"}
              disabled={busy}
              onClick={() => {
                const action = signing;
                setSigning(null);
                if (action === "sign") run(() => dlcApi.signRelease(agentId, note), "v2.dlc.release.signed");
                else if (action === "block") run(() => dlcApi.blockRelease(agentId, note), "v2.dlc.release.blocked");
                else if (action === "rollback") run(() => dlcApi.rollback(agentId, note), "v2.dlc.release.rolledBack");
              }}
            >
              {t("v2.common.confirm")}
            </Button>
          </>
        }
      >
        <p className="v2-muted">{t(`v2.dlc.release.${signing ?? "sign"}Hint`)}</p>
        {signing === "sign" && report && (
          <Descriptions
            one
            items={[
              { label: t("v2.dlc.release.verdict"), value: report.verdict },
              { label: t("v2.dlc.release.waived"), value: report.waived.join(", ") || "—" },
              { label: t("v2.dlc.release.liveAfter"), value: pending?.candidate_version ?? "—" },
              { label: t("v2.dlc.release.rollbackTo"), value: pending?.previous_live_version ?? "—" },
            ]}
          />
        )}
        <Field label={t("v2.dlc.release.note")} required={signing !== "sign"}>
          <textarea className="v2-input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}
