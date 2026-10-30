import "./workspaces.css";

import { ArrowLeft, Copy, Play, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { ErrorCallout } from "../../components/ErrorCallout";
import {
  api,
  ApiError,
  type StageInfo,
  type Workspace,
  type WorkspaceBootstrapJob,
  type WorkspaceBootstrapStatus,
  type WorkspacePurgeResult,
  WORKSPACE_TIERS,
  type WorkspaceTier,
} from "../../lib/api";
import { formatRowCounts, PENDING_BOOTSTRAP_STAGES, readBootstrapJobIds, rememberBootstrapJobId } from "../../lib/workspaces";
import { V2ToastProvider } from "../../v2/ui";
import { InboundDefaultCard } from "../../v2/pages/workspaces/InboundDefaultCard";
import { MappingsCard } from "../../v2/pages/workspaces/MappingsCard";
import { deployedLabel, roleTemplateSummary, useRoleTemplate } from "../../workspace/use-role-template";
import { useWorkspace } from "../../workspace/workspace-context";
import { useFailure } from "../failure";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat, Track } from "../ui";
import { Grants } from "./workspaces/Grants";
import { Select } from "../select";

const STATUS_SIGNAL: Record<WorkspaceBootstrapStatus, Signal> = { registered: "wait", bootstrapping: "info", ready: "ok", failed: "act" };
const TIER_SIGNAL: Record<WorkspaceTier, Signal | undefined> = { dev: undefined, staging: "wait", prod: "act" };
/** while any bootstrap runs, the list re-reads so its status moves on its own */
const LIST_POLL_MS = 5000;
const JOB_POLL_MS = 2000;

const STAGE_SIGNAL: Record<StageInfo["status"], Signal> = { succeeded: "ok", skipped: "ok", running: "wait", failed: "act", pending: "off" };

function StatusChip({ status }: { status: WorkspaceBootstrapStatus }) {
  const { t } = useTranslation();
  return <Chip s={STATUS_SIGNAL[status]}>{t(`v2.workspaces.status.${status}`)}</Chip>;
}

function TierChip({ tier }: { tier: WorkspaceTier | undefined }) {
  const { t } = useTranslation();
  const value = tier ?? "dev";
  return <Chip s={TIER_SIGNAL[value]} title={t(`v2.workspaces.tierHint.${value}`)}>{t(`v2.workspaces.tier.${value}`)}</Chip>;
}

/* ── list ────────────────────────────────────────────────────────────────── */

function WorkspaceList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [tick, setTick] = useState(0);
  const list = useLoad(() => api.listWorkspaces(), `v3-workspaces:${tick}`);
  const [state, setState] = useState<"all" | WorkspaceBootstrapStatus>("all");
  const [kind, setKind] = useState<"all" | "local" | "external">("all");
  const [q, setQ] = useState("");
  const all = useMemo(() => list.data?.workspaces ?? [], [list.data]);
  const running = all.some((w) => w.bootstrap_status === "bootstrapping");
  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => setTick((n) => n + 1), LIST_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [running, list.data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((w) => {
      if (state !== "all" && w.bootstrap_status !== state) return false;
      if (kind === "local" && w.cross_account) return false;
      if (kind === "external" && !w.cross_account) return false;
      return !needle || `${w.id} ${w.name} ${w.account_id} ${w.region}`.toLowerCase().includes(needle);
    });
  }, [all, state, kind, q]);
  const count = (s: WorkspaceBootstrapStatus) => all.filter((w) => w.bootstrap_status === s).length;
  const attention = all.filter((w) => !w.is_default && (w.bootstrap_status === "failed" || w.bootstrap_status === "registered"));
  const open = (id: string) => setParams({ view: "detail", id });

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.workspaces.eyebrow")}
        title={t("nav.workspaces")}
        sub={t("v2.workspaces.desc")}
        end={
          <>
            <Btn kind="ghost" onClick={list.reload}><RefreshCw size={14} /></Btn>
            <Link to="/v2/workspaces?view=new" className="v3-btn primary"><Plus size={14} /> {t("v2.workspaces.new")}</Link>
          </>
        }
      />

      <div className="v3-grid c4">
        <Panel><Stat label={t("v2.workspaces.kpi.total")} value={list.data ? all.length : "—"} /></Panel>
        <Panel signal={count("ready") ? "ok" : undefined}><Stat label={t("v2.workspaces.kpi.ready")} value={list.data ? count("ready") : "—"} /></Panel>
        <Panel signal={count("bootstrapping") ? "info" : undefined}><Stat label={t("v2.workspaces.kpi.bootstrapping")} value={list.data ? count("bootstrapping") : "—"} /></Panel>
        <Panel signal={count("failed") ? "act" : attention.length ? "wait" : undefined}>
          <Stat label={t("v2.workspaces.kpi.attention")} value={list.data ? count("registered") + count("failed") : "—"}
            signal={count("failed") ? "act" : undefined} foot={t("v2.workspaces.kpi.attentionSub", { failed: count("failed") })} />
        </Panel>
      </div>

      {attention.length > 0 && (
        <Panel title={t("v3.workspaces.needsYou")} signal={attention.some((w) => w.bootstrap_status === "failed") ? "act" : "wait"} flush>
          <table className="v3-table">
            <tbody>
              {attention.map((w) => (
                <tr key={w.id} className="click" onClick={() => open(w.id)}>
                  <td style={{ width: 30 }}><Lamp s={STATUS_SIGNAL[w.bootstrap_status]} /></td>
                  <td><div className="v3-name"><div><b>{w.name}</b><small>{w.id}</small></div></div></td>
                  <td className="mono" style={{ color: "var(--v3-text-2)" }}>{w.account_id} · {w.region}</td>
                  <td style={{ color: "var(--v3-text-2)" }}>
                    {w.bootstrap_status === "failed" ? t("v3.workspaces.failedHint") : t("v2.workspaces.needsBootstrap")}
                  </td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}><StatusChip status={w.bootstrap_status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.workspaces.all"), count: all.length },
            ...(["ready", "bootstrapping", "registered", "failed"] as const).map((s) => ({
              value: s, label: t(`v2.workspaces.status.${s}`), s: STATUS_SIGNAL[s], count: count(s),
            })),
          ]}
        />
        <span style={{ width: 1, height: 20, background: "var(--v3-line)" }} aria-hidden="true" />
        <Filters
          value={kind}
          onChange={setKind}
          options={[
            { value: "all", label: t("v3.workspaces.anyAccount") },
            { value: "local", label: t("v2.workspaces.kind.local") },
            { value: "external", label: t("v2.workspaces.kind.external") },
          ]}
        />
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v2.workspaces.search")} aria-label={t("v2.workspaces.search")} />
        </div>
      </div>

      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{t("workspacesPage.loadFailed", { msg: list.error })}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.workspaces.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.workspaces.col.name")}</th>
                <th>{t("v2.workspaces.col.account")}</th>
                <th>{t("v2.workspaces.col.region")}</th>
                <th>{t("v2.workspaces.field.tier")}</th>
                <th>{t("v2.workspaces.col.status")}</th>
                <th className="num">{t("v2.workspaces.col.created")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((w) => (
                <tr key={w.id} className="click" onClick={() => open(w.id)}>
                  <td style={{ width: 30 }}><Lamp s={STATUS_SIGNAL[w.bootstrap_status]} live={w.bootstrap_status === "bootstrapping"} /></td>
                  <td>
                    <div className="v3-name">
                      <div>
                        <b>{w.name}{w.is_default && <> <Chip>{t("v2.workspaces.hub")}</Chip></>}</b>
                        <small>{w.id}</small>
                      </div>
                    </div>
                  </td>
                  <td className="mono">{w.account_id}{w.cross_account && <> <Chip s="info">{t("v2.workspaces.external")}</Chip></>}</td>
                  <td className="mono">{w.region}</td>
                  <td><TierChip tier={w.tier} /></td>
                  <td><StatusChip status={w.bootstrap_status} /></td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(w.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

/* ── one workspace ───────────────────────────────────────────────────────── */

function WorkspaceDetail({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [, setParams] = useSearchParams();
  const { refresh: refreshSwitcher } = useWorkspace();
  const [tick, setTick] = useState(0);
  const list = useLoad(() => api.listWorkspaces(), `v3-ws-detail:${workspaceId}:${tick}`);
  const row: Workspace | null = list.data?.workspaces.find((w) => w.id === workspaceId) ?? null;
  const roleTemplate = useRoleTemplate(row);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"bootstrap" | "detach" | "purge" | "tier" | null>(null);
  /** a tier move into or out of prod, waiting on the operator's confirmation */
  const [pendingTier, setPendingTier] = useState<WorkspaceTier | null>(null);
  /** the dry run behind the purge dialog: what a purge would take with it */
  const [purgePreview, setPurgePreview] = useState<WorkspacePurgeResult | null>(null);
  const [jobId, setJobId] = useState<string | null>(() => readBootstrapJobIds()[workspaceId] ?? null);
  const [job, setJob] = useState<WorkspaceBootstrapJob | null>(null);
  const [grantedTotal, setGrantedTotal] = useState<number | null>(null);
  const back = () => setParams({});

  /** re-read this row and the top-bar switcher (every mutation moves both) */
  const reload = useCallback(async () => {
    setTick((n) => n + 1);
    await refreshSwitcher();
  }, [refreshSwitcher]);

  // a run started elsewhere is not in localStorage: ask which job is the latest
  // (never for the hub, whose environment this job does not build)
  const lookupJob = row !== null && !row.is_default;
  useEffect(() => {
    if (jobId || !lookupJob) return;
    let alive = true;
    void api.getWorkspaceBootstrap(workspaceId).then((status) => {
      if (!alive || !status.job) return;
      rememberBootstrapJobId(workspaceId, status.job.id);
      setJobId(status.job.id);
    }).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [jobId, lookupJob, workspaceId]);

  const poll = useCallback(async () => {
    if (!jobId) return;
    try {
      // the job belongs to the workspace under management, not the current selection
      const next = await api.getWorkspaceJob(jobId, workspaceId);
      setJob(next);
      if (next.status === "succeeded" || next.status === "failed") await reload();
    } catch {
      /* retried on the next tick */
    }
  }, [jobId, reload, workspaceId]);
  const jobDone = job?.status === "succeeded" || job?.status === "failed";
  useEffect(() => {
    if (!jobId) return;
    void poll();
    if (jobDone) return;
    const timer = window.setInterval(() => void poll(), JOB_POLL_MS);
    return () => window.clearInterval(timer);
  }, [jobDone, jobId, poll]);

  const startBootstrap = async () => {
    if (!row) return;
    setBusy(true);
    try {
      failed.clear();
      const ack = await api.bootstrapWorkspace(row.id);
      rememberBootstrapJobId(row.id, ack.job_id);
      setJobId(ack.job_id);
      setJob(null);
      toast("ok", t("workspacesPage.bootstrapQueued", { name: row.name }));
      await reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };
  const changeTier = async (tier: WorkspaceTier, confirmed: boolean) => {
    if (!row) return;
    setBusy(true);
    try {
      failed.clear();
      await api.patchWorkspace(row.id, { tier, confirm_tier_change: confirmed });
      toast("ok", t("v2.workspaces.tierChanged", { name: row.name, tier: t(`v2.workspaces.tier.${tier}`) }));
      await reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
      setPendingTier(null);
      setConfirm(null);
    }
  };
  /** crossing prod (in or out) changes who may modify agents, so it is confirmed */
  const pickTier = (tier: WorkspaceTier) => {
    if (!row || tier === row.tier) return;
    if (tier === "prod" || row.tier === "prod") {
      setPendingTier(tier);
      setConfirm("tier");
      return;
    }
    void changeTier(tier, false);
  };
  const detach = async () => {
    if (!row) return;
    setBusy(true);
    try {
      failed.clear();
      await api.deleteWorkspace(row.id);
      toast("ok", t("workspacesPage.deleted", { name: row.name }));
      await refreshSwitcher();
      back();
    } catch (err) {
      if (err instanceof ApiError && err.code === "workspace.in_use") {
        const rows = (err.detail as { rows?: Record<string, number> } | null)?.rows ?? {};
        failed.fail(err, t("workspacesPage.deleteInUse", { rows: formatRowCounts(rows) }));
      } else failed.fail(err);
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };
  /** a refusal names the rule that stopped it, with copy per reason */
  const purgeFailed = (err: unknown) => {
    if (err instanceof ApiError && err.code === "workspace.purge_refused") {
      const reason = (err.detail as { reason?: string } | null)?.reason ?? "";
      failed.fail(err, t("workspacesPage.detail.purgeRefused", { reason: t(`workspacesPage.detail.purgeReason.${reason}`, err.message) }));
    } else failed.fail(err);
  };
  const openPurge = async () => {
    if (!row) return;
    setPurgePreview(null);
    setConfirm("purge");
    try {
      setPurgePreview(await api.purgeWorkspace(row.id, { dryRun: true }));
    } catch (err) {
      // the dry run runs the same guardrails: a refusal means the button was stale
      setConfirm(null);
      purgeFailed(err);
      await reload();
    }
  };
  const purge = async () => {
    if (!row) return;
    setBusy(true);
    try {
      await api.purgeWorkspace(row.id);
      toast("ok", t("workspacesPage.purged", { name: row.name }));
      await refreshSwitcher();
      back();
    } catch (err) {
      purgeFailed(err);
      await reload();
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const backBtn = <div><button type="button" className="v3-btn ghost sm" onClick={back}><ArrowLeft size={14} /> {t("nav.workspaces")}</button></div>;
  if (list.loading && !list.data) return <div style={{ display: "grid", gap: 16 }}>{backBtn}<Skeleton rows={6} /></div>;
  if (list.error && !list.data) {
    return <div style={{ display: "grid", gap: 16 }}>{backBtn}<Notice s="act">{t("workspacesPage.loadFailed", { msg: list.error })}</Notice></div>;
  }
  if (!row) {
    return <div style={{ display: "grid", gap: 16 }}>{backBtn}<Notice s="wait">{t("workspacesPage.detail.goneBody", { id: workspaceId })}</Notice></div>;
  }

  const stages = job?.payload?.stages ?? PENDING_BOOTSTRAP_STAGES;
  const bootstrapping = row.bootstrap_status === "bootstrapping";
  const canBootstrap = !row.is_default && !bootstrapping && row.bootstrap_status !== "ready" && !busy;
  const bootstrapDisabledReason = row.is_default
    ? t("workspacesPage.detail.bootstrapDisabledHub")
    : bootstrapping
      ? t("workspacesPage.detail.bootstrapDisabledRunning")
      : row.bootstrap_status === "ready"
        ? t("workspacesPage.detail.bootstrapDisabledReady")
        : undefined;
  // only registration residue is purgeable: a READY environment is in use
  const canPurge = !row.is_default && (row.bootstrap_status === "registered" || row.bootstrap_status === "failed") && !busy;
  const resume = row.bootstrap_status === "failed";
  const s = STATUS_SIGNAL[row.bootstrap_status];
  const jobSignal: Signal | undefined = job ? (job.status === "failed" ? "act" : job.status === "succeeded" ? "ok" : "wait") : undefined;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      {backBtn}
      <PageHead
        eyebrow={`${t("v3.workspaces.eyebrow")} · ${row.id}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={s} live={row.bootstrap_status === "ready" || bootstrapping} />
            {row.name}
          </span>
        }
        sub={<span className="mono">{row.account_id} · {row.region}</span>}
        end={
          <>
            <Btn kind="ghost" onClick={() => void reload()}><RefreshCw size={14} /></Btn>
            <Btn busy={busy} disabled={row.is_default} disabledReason={t("workspacesPage.detail.hubNote")}
              onClick={() => setConfirm("detach")}>{t("v2.workspaces.detach")}</Btn>
            {canPurge && <Btn kind="danger" onClick={() => void openPurge()}><Trash2 size={14} /> {t("v2.workspaces.purge")}</Btn>}
            <Btn kind="primary" busy={busy} disabled={!canBootstrap} disabledReason={bootstrapDisabledReason} onClick={() => setConfirm("bootstrap")}>
              <Play size={14} />
              {t(bootstrapping ? "v2.workspaces.bootstrapRunning" : resume ? "v2.workspaces.bootstrapResume" : "v2.workspaces.bootstrapRun")}
            </Btn>
          </>
        }
      />
      <ErrorCallout error={failed.error} params={{ workspace_id: workspaceId }} onDismiss={failed.clear} data-testid="v3-workspace-error" />
      {row.is_default && <Notice>{t("workspacesPage.detail.hubNote")}</Notice>}
      {row.bootstrap_status === "registered" && !row.is_default && !jobId && <Notice s="wait">{t("v2.workspaces.needsBootstrap")}</Notice>}
      {roleTemplate.data?.status === "outdated" && roleTemplate.data.stack_command && (
        <Notice s="wait">
          <div data-testid="v3-ws-role-template-outdated">
            <strong>{t("v2.workspaces.roleTemplate.outdatedTitle")}</strong>
            <p style={{ margin: "4px 0 8px" }}>
              {t("v2.workspaces.roleTemplate.outdatedBody", {
                account: row.account_id,
                deployed: deployedLabel(t, roleTemplate.data),
                expected: roleTemplate.data.expected,
              })}
            </p>
            <pre className="v3-pre">{roleTemplate.data.stack_command}</pre>
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <Btn onClick={() => void navigator.clipboard?.writeText(roleTemplate.data?.stack_command ?? "")
                .then(() => toast("ok", t("v2.workspaces.roleTemplate.copied")))}>
                <Copy size={14} /> {t("v2.workspaces.roleTemplate.copy")}
              </Btn>
              <Btn busy={roleTemplate.loading} onClick={() => void roleTemplate.recheck()}>
                <RefreshCw size={14} /> {t("v2.workspaces.roleTemplate.recheck")}
              </Btn>
            </div>
          </div>
        </Notice>
      )}

      <div className="v3-grid c4">
        <Panel signal={s === "off" ? undefined : s}><Stat label={t("v2.workspaces.col.status")} value={t(`v2.workspaces.status.${row.bootstrap_status}`)} /></Panel>
        <Panel signal={TIER_SIGNAL[row.tier ?? "dev"]}>
          <div className="v3-stat">
            <div className="label">{t("v2.workspaces.field.tier")}</div>
            <Select className="v3-select v3-wsp-tier" value={row.tier ?? "dev"} disabled={busy}
              onChange={(e) => pickTier(e.target.value as WorkspaceTier)} aria-label={t("v2.workspaces.field.tier")}>
              {WORKSPACE_TIERS.map((tier) => <option key={tier} value={tier}>{t(`v2.workspaces.tier.${tier}`)}</option>)}
            </Select>
            <div className="foot">{t(`v2.workspaces.tierHint.${row.tier ?? "dev"}`)}</div>
          </div>
        </Panel>
        <Panel><Stat label={t("v2.workspaces.grantedMembers")} value={grantedTotal ?? "—"} /></Panel>
        <Panel><Stat label={t("v2.workspaces.updated")} value={ago(row.updated_at)} foot={`${t("v2.workspaces.col.created")} ${ago(row.created_at)}`} /></Panel>
      </div>

      <div className="v3-grid v3-split">
        <Panel title={t("v2.workspaces.summary")}>
          <dl className="v3-kv">
            <dt>{t("v2.workspaces.field.id")}</dt><dd className="mono">{row.id}</dd>
            <dt>{t("v2.workspaces.field.name")}</dt><dd>{row.name}{row.is_default && <> <Chip>{t("v2.workspaces.hub")}</Chip></>}</dd>
            <dt>{t("v2.workspaces.field.account")}</dt>
            <dd className="mono">{row.account_id}{row.cross_account && <> <Chip s="info">{t("v2.workspaces.external")}</Chip></>}</dd>
            <dt>{t("v2.workspaces.field.region")}</dt><dd className="mono">{row.region}</dd>
            {row.cross_account && <><dt>{t("v2.workspaces.field.roleArn")}</dt><dd className="mono">{row.role_arn ?? "—"}</dd></>}
            {row.cross_account && <><dt>{t("v2.workspaces.roleTemplate.label")}</dt>
              <dd data-testid="v3-ws-role-template">{roleTemplate.eligible ? roleTemplateSummary(t, roleTemplate.data, roleTemplate.loading) : "—"}</dd></>}
          </dl>
        </Panel>
        <Panel title={t("v2.workspaces.howTitle")}>
          <ol className="v3-wsp-how">
            {[1, 2, 3, 4].map((n) => <li key={n}><span className="n">{n}</span><span>{t(`workspacesPage.detail.how${n}`)}</span></li>)}
          </ol>
          <p style={{ margin: "10px 0 0", color: "var(--v3-text-3)", fontSize: 12.5 }}>{t("workspacesPage.detail.howNote")}</p>
        </Panel>
      </div>

      {/* skipped for the hub: `make bootstrap` provisions it; this job never runs there */}
      {!row.is_default && (
        <Panel title={t("v2.workspaces.stagesTitle")} signal={jobSignal}
          end={<span className="mono">{jobId ? `job #${jobId.slice(0, 8)}` : t("workspacesPage.detail.noRun")}{job ? ` · ${t(`v2.workspaces.jobStatus.${job.status}`, job.status)}` : ""}</span>}>
          <div className="v3-wsp-stages">
            <Track nodes={stages.map((st) => ({
              key: st.name,
              label: t(`v2.workspaces.stages.${st.name}`, st.name),
              s: STAGE_SIGNAL[st.status],
              here: st.status === "running" || st.status === "failed",
            }))} />
          </div>
          {stages.some((st) => st.detail) && (
            <table className="v3-table" style={{ marginTop: 14 }}>
              <tbody>
                {stages.filter((st) => st.detail).map((st) => (
                  <tr key={st.name}>
                    <td style={{ width: 30 }}><Lamp s={STAGE_SIGNAL[st.status]} live={st.status === "running"} /></td>
                    <td className="mono" style={{ width: 180 }}>{t(`v2.workspaces.stages.${st.name}`, st.name)}</td>
                    <td style={{ color: "var(--v3-text-2)" }}>{st.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {job?.error && <div style={{ marginTop: 12 }}><Notice s="act"><span className="mono">{job.error}</span></Notice></div>}
          {job?.events?.length ? (
            <pre className="v3-pre" style={{ marginTop: 12 }}>
              {job.events.map((ev, i) => (
                <div key={i}>
                  <span style={{ color: "var(--v3-text-3)" }}>{ev.ts.slice(11, 19)}</span>{" "}
                  <span style={{ color: ev.level === "error" ? "var(--v3-act)" : "var(--v3-info)" }}>{ev.stage}</span> {ev.msg}
                </div>
              ))}
            </pre>
          ) : null}
        </Panel>
      )}

      <Grants workspaceId={workspaceId} onTotal={setGrantedTotal} />

      {/* the inbound-auth default (JWT authorizer form) and resource mappings are
          V2's own components on the V3 theme, so their checks stay exactly V2's */}
      <V2ToastProvider>
        <div className="v2 v3-host v3-wsp-embed">
          {(row.is_default || row.bootstrap_status === "ready") && <InboundDefaultCard workspaceId={workspaceId} />}
          <MappingsCard workspaceId={workspaceId} />
        </div>
      </V2ToastProvider>

      {confirm === "bootstrap" && (
        <Confirm title={t(resume ? "v2.workspaces.bootstrapResume" : "v2.workspaces.bootstrapRun")}
          confirmLabel={t(resume ? "v2.workspaces.bootstrapResume" : "v2.workspaces.bootstrapRun")}
          cancelLabel={t("v3.common.cancel")} busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void startBootstrap()}>
          {t("v2.workspaces.bootstrapConfirm", { name: row.name, account: row.account_id, region: row.region })}
        </Confirm>
      )}
      {confirm === "tier" && pendingTier !== null && (
        <Confirm title={t("v2.workspaces.tierConfirmTitle")} confirmLabel={t("v2.workspaces.tierConfirmOk")}
          cancelLabel={t("v3.common.cancel")} danger={pendingTier === "prod"} busy={busy}
          onCancel={() => {
            setPendingTier(null);
            setConfirm(null);
          }}
          onConfirm={() => void changeTier(pendingTier, true)}>
          {t(pendingTier === "prod" ? "v2.workspaces.tierConfirmToProd" : "v2.workspaces.tierConfirmFromProd", {
            name: row.name, tier: t(`v2.workspaces.tier.${pendingTier}`),
          })}
        </Confirm>
      )}
      {confirm === "detach" && (
        <Confirm title={t("v2.workspaces.detachTitle")} confirmLabel={t("v2.workspaces.detach")} cancelLabel={t("v3.common.cancel")}
          danger busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void detach()}>
          {t("workspacesPage.detail.deleteBody", { name: row.name })}
        </Confirm>
      )}
      {confirm === "purge" && (
        <Confirm title={t("v2.workspaces.purgeTitle")} confirmLabel={t("v2.workspaces.purge")} cancelLabel={t("v3.common.cancel")}
          danger busy={busy || !purgePreview} onCancel={() => setConfirm(null)} onConfirm={() => void purge()}>
          {!purgePreview ? (
            <span>{t("workspacesPage.detail.purgeChecking")}</span>
          ) : (
            <div style={{ display: "grid", gap: 10 }}>
              <span>
                {formatRowCounts(purgePreview.rows)
                  ? t("workspacesPage.detail.purgeBody", { name: row.name, rows: formatRowCounts(purgePreview.rows) })
                  : t("workspacesPage.detail.purgeBodyEmpty", { name: row.name })}
              </span>
              {purgePreview.resource_keys.length > 0 && (
                <Notice s="wait">{t("workspacesPage.detail.purgeAwsNote", { keys: purgePreview.resource_keys.join(", ") })}</Notice>
              )}
            </div>
          )}
        </Confirm>
      )}
    </div>
  );
}

/**
 * Workspaces (admin-only): the environments (AWS account × region) the console
 * can target, led by those that need attention (registered, failed bootstrap);
 * one workspace at `?view=detail&id=` with its bootstrap run, tier, grants,
 * inbound-auth default and resource mappings. Registration (`?view=new`) stays
 * the hosted V2 form.
 */
export function V3Workspaces() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [params] = useSearchParams();
  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.workspaces.eyebrow")} title={t("nav.workspaces")} />
        <Notice s="wait">{t("workspacesPage.forbiddenBody")}</Notice>
      </div>
    );
  }
  const id = params.get("id") ?? "";
  // keyed on the selection: the detail remembers which bootstrap job it watches
  if (params.get("view") === "detail" && id) return <WorkspaceDetail key={id} workspaceId={id} />;
  return <WorkspaceList />;
}
