import { Check, Loader2, Play, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  ApiError,
  errorMessage,
  type StageInfo,
  type WorkspaceBootstrapJob,
  type WorkspacePurgeResult,
} from "../../../lib/api";
import {
  formatRowCounts,
  PENDING_BOOTSTRAP_STAGES,
  readBootstrapJobIds,
  rememberBootstrapJobId,
} from "../../../lib/workspaces";
import { useWorkspace } from "../../../workspace/workspace-context";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, FlowHeader, Spin, Tag } from "../../ui";
import { ExternalTag, HubTag, StatusTag } from "./tags";
import { GrantsCard } from "./GrantsCard";

const POLL_MS = 2000;

function StageIcon({ stage, index }: { stage: StageInfo; index: number }) {
  if (stage.status === "succeeded" || stage.status === "skipped") return <Check size={13} aria-hidden="true" />;
  if (stage.status === "running") return <Loader2 size={13} className="v2-workspaces-spin" aria-hidden="true" />;
  if (stage.status === "failed") return <X size={13} aria-hidden="true" />;
  return <>{index + 1}</>;
}

/**
 * `?view=detail&id=` — one workspace: summary, bootstrap job (stages + log,
 * polled while it runs), member grants, and the detach / purge / bootstrap
 * actions with their server-side guards surfaced as copy.
 */
export function WorkspaceDetail({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [, setParams] = useSearchParams();
  const { refresh: refreshSwitcher } = useWorkspace();
  const [tick, setTick] = useState(0);
  const list = useLoad(() => api.listWorkspaces(), `ws-detail:${workspaceId}:${tick}`);
  const row = list.data?.workspaces.find((w) => w.id === workspaceId) ?? null;

  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"bootstrap" | "detach" | "purge" | null>(null);
  /** the dry run behind the purge dialog: what a purge would take with it */
  const [purgePreview, setPurgePreview] = useState<WorkspacePurgeResult | null>(null);
  const [jobId, setJobId] = useState<string | null>(() => readBootstrapJobIds()[workspaceId] ?? null);
  const [job, setJob] = useState<WorkspaceBootstrapJob | null>(null);
  const [grantedTotal, setGrantedTotal] = useState<number | null>(null);

  const back = () => setParams({});

  /** Re-read this row and the top-bar switcher (every mutation moves both). */
  const reload = useCallback(async () => {
    setTick((n) => n + 1);
    await refreshSwitcher();
  }, [refreshSwitcher]);

  // A run started in another browser (or by another admin) is not in
  // localStorage — ask the backend which job is the latest one (only for a row
  // that exists and is not the hub, whose environment this job never builds).
  const lookupJob = row !== null && !row.is_default;
  useEffect(() => {
    if (jobId || !lookupJob) return;
    let alive = true;
    void api
      .getWorkspaceBootstrap(workspaceId)
      .then((status) => {
        if (!alive || !status.job) return;
        rememberBootstrapJobId(workspaceId, status.job.id);
        setJobId(status.job.id);
      })
      .catch(() => {
        /* the card just shows "no run" */
      });
    return () => {
      alive = false;
    };
  }, [jobId, lookupJob, workspaceId]);

  const poll = useCallback(async () => {
    if (!jobId) return;
    try {
      // The job belongs to the workspace under management, not the console's
      // current selection — name it, or the scoped route 404s.
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
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [jobDone, jobId, poll]);

  const startBootstrap = async () => {
    if (!row) return;
    setBusy(true);
    try {
      const ack = await api.bootstrapWorkspace(row.id);
      rememberBootstrapJobId(row.id, ack.job_id);
      setJobId(ack.job_id);
      setJob(null);
      toast("success", t("workspacesPage.bootstrapQueued", { name: row.name }));
      await reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const detach = async () => {
    if (!row) return;
    setBusy(true);
    try {
      await api.deleteWorkspace(row.id);
      toast("success", t("workspacesPage.deleted", { name: row.name }));
      await refreshSwitcher();
      back();
    } catch (err) {
      if (err instanceof ApiError && err.code === "workspace.in_use") {
        const rows = (err.detail as { rows?: Record<string, number> } | null)?.rows ?? {};
        toast("error", t("workspacesPage.deleteInUse", { rows: formatRowCounts(rows) }));
      } else {
        toast("error", errorMessage(err));
      }
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  /** A refusal names the rule that stopped it, and there is copy per reason. */
  const purgeFailed = (err: unknown) => {
    if (err instanceof ApiError && err.code === "workspace.purge_refused") {
      const reason = (err.detail as { reason?: string } | null)?.reason ?? "";
      toast(
        "error",
        t("workspacesPage.detail.purgeRefused", {
          reason: t(`workspacesPage.detail.purgeReason.${reason}`, err.message),
        }),
      );
    } else {
      toast("error", errorMessage(err));
    }
  };

  const openPurge = async () => {
    if (!row) return;
    setPurgePreview(null);
    setConfirm("purge");
    try {
      setPurgePreview(await api.purgeWorkspace(row.id, { dryRun: true }));
    } catch (err) {
      // The dry run runs the same guardrails: a refusal means the button was
      // stale (a bootstrap started, an agent appeared) — say so and re-read.
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
      toast("success", t("workspacesPage.purged", { name: row.name }));
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

  if (list.loading && !list.data) {
    return (
      <>
        <FlowHeader title={workspaceId} onBack={back} />
        <Card>
          <Spin />
        </Card>
      </>
    );
  }
  if (list.error && !list.data) {
    return (
      <>
        <FlowHeader title={workspaceId} onBack={back} />
        <Card>
          <Alert tone="error" action={<Button size="sm" onClick={list.reload}>{t("v2.common.retry")}</Button>}>
            {t("workspacesPage.loadFailed", { msg: list.error })}
          </Alert>
        </Card>
      </>
    );
  }
  if (!row) {
    return (
      <>
        <FlowHeader title={t("workspacesPage.detail.gone")} onBack={back} />
        <Card testId="v2-ws-gone">
          <Alert tone="warn">{t("workspacesPage.detail.goneBody", { id: workspaceId })}</Alert>
        </Card>
      </>
    );
  }

  const stages = job?.payload?.stages ?? PENDING_BOOTSTRAP_STAGES;
  const bootstrapping = row.bootstrap_status === "bootstrapping";
  const canBootstrap = !row.is_default && !bootstrapping && row.bootstrap_status !== "ready" && !busy;
  // Read in canBootstrap's order: the first unmet predicate names the reason.
  const bootstrapDisabledReason = row.is_default
    ? t("workspacesPage.detail.bootstrapDisabledHub")
    : bootstrapping
      ? t("workspacesPage.detail.bootstrapDisabledRunning")
      : row.bootstrap_status === "ready"
        ? t("workspacesPage.detail.bootstrapDisabledReady")
        : undefined;
  // Only registration residue is purgeable: a READY environment is in use, and
  // retiring one means deleting its AWS resources, which purge does not do.
  const canPurge =
    !row.is_default && (row.bootstrap_status === "registered" || row.bootstrap_status === "failed") && !busy;
  const resume = row.bootstrap_status === "failed";

  const purgeBody = () => {
    if (!purgePreview) return <Spin label={t("workspacesPage.detail.purgeChecking")} />;
    const rows = formatRowCounts(purgePreview.rows);
    return (
      <div className="v2-stack">
        <span>
          {rows
            ? t("workspacesPage.detail.purgeBody", { name: row.name, rows })
            : t("workspacesPage.detail.purgeBodyEmpty", { name: row.name })}
        </span>
        {purgePreview.resource_keys.length > 0 && (
          <Alert tone="warn">
            {t("workspacesPage.detail.purgeAwsNote", { keys: purgePreview.resource_keys.join(", ") })}
          </Alert>
        )}
      </div>
    );
  };

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {row.name}
            <StatusTag status={row.bootstrap_status} />
            {row.is_default && <HubTag />}
          </span>
        }
        onBack={back}
        end={
          <>
            <Button onClick={() => void reload()}>
              <RefreshCw size={14} aria-hidden="true" />
              {t("v2.common.refresh")}
            </Button>
            <Button
              disabled={row.is_default || busy}
              title={row.is_default ? t("workspacesPage.detail.hubNote") : undefined}
              onClick={() => setConfirm("detach")}
              testId="v2-ws-detach"
            >
              {t("v2.workspaces.detach")}
            </Button>
            {canPurge && (
              <Button kind="danger" onClick={() => void openPurge()} testId="v2-ws-purge">
                <Trash2 size={14} aria-hidden="true" />
                {t("v2.workspaces.purge")}
              </Button>
            )}
            <Button
              kind="primary"
              disabled={!canBootstrap}
              title={bootstrapDisabledReason}
              onClick={() => setConfirm("bootstrap")}
              testId="v2-ws-bootstrap"
            >
              <Play size={14} aria-hidden="true" />
              {t(
                bootstrapping
                  ? "v2.workspaces.bootstrapRunning"
                  : resume
                    ? "v2.workspaces.bootstrapResume"
                    : "v2.workspaces.bootstrapRun",
              )}
            </Button>
          </>
        }
      />
      {row.is_default && (
        <Alert>
          <span data-testid="v2-ws-hub-note">{t("workspacesPage.detail.hubNote")}</span>
        </Alert>
      )}
      {row.bootstrap_status === "registered" && !row.is_default && !jobId && (
        <Alert tone="warn">{t("v2.workspaces.needsBootstrap")}</Alert>
      )}

      <div className="v2-workspaces-layout">
        <Card title={t("v2.workspaces.summary")}>
          <Descriptions
            items={[
              { label: t("v2.workspaces.field.id"), value: <span className="mono">{row.id}</span> },
              { label: t("v2.workspaces.field.name"), value: row.name },
              {
                label: t("v2.workspaces.field.account"),
                value: (
                  <span className="v2-row">
                    <span className="mono">{row.account_id}</span>
                    {row.cross_account && <ExternalTag />}
                  </span>
                ),
              },
              { label: t("v2.workspaces.field.region"), value: <span className="mono">{row.region}</span> },
              ...(row.cross_account
                ? [
                    {
                      label: t("v2.workspaces.field.roleArn"),
                      value: <span className="mono v2-workspaces-break">{row.role_arn ?? "—"}</span>,
                    },
                  ]
                : []),
              { label: t("v2.workspaces.grantedMembers"), value: grantedTotal ?? "—" },
              { label: t("v2.workspaces.col.created"), value: fmtTime(row.created_at) },
              { label: t("v2.workspaces.updated"), value: fmtTime(row.updated_at) },
            ]}
          />
        </Card>
        <Card title={t("v2.workspaces.howTitle")}>
          <ol className="v2-workspaces-how">
            {[1, 2, 3, 4].map((n) => (
              <li key={n}>
                <span className="n">{n}</span>
                <span>{t(`workspacesPage.detail.how${n}`)}</span>
              </li>
            ))}
          </ol>
          <p className="v2-workspaces-note">{t("workspacesPage.detail.howNote")}</p>
        </Card>
      </div>

      {/* Skipped for the hub: `make bootstrap` (CDK) provisions it, this job never
          runs there — ten pending stages beside a READY tag would mislead. */}
      {!row.is_default && (
        <Card
          title={t("v2.workspaces.stagesTitle")}
          sub={jobId ? `job #${jobId.slice(0, 8)}` : t("workspacesPage.detail.noRun")}
          end={job ? <Tag tone={job.status === "failed" ? "red" : job.status === "succeeded" ? "green" : "blue"}>{t(`v2.workspaces.jobStatus.${job.status}`, job.status)}</Tag> : undefined}
          testId="v2-ws-bootstrap-card"
        >
          <div className="v2-stages" data-testid="v2-ws-stages">
            {stages.map((stage, index) => (
              <div key={stage.name} className={`v2-stage ${stage.status}`}>
                <span className="n">
                  <StageIcon stage={stage} index={index} />
                </span>
                <div className="b">
                  <div className="t">{t(`v2.workspaces.stages.${stage.name}`, stage.name)}</div>
                  <div className="d">{stage.detail || "—"}</div>
                </div>
              </div>
            ))}
          </div>
          {job?.error && (
            <Alert tone="error">
              <span className="mono v2-workspaces-break">{job.error}</span>
            </Alert>
          )}
          {job?.events?.length ? (
            <>
              <div className="v2-sub-title">{t("v2.workspaces.log")}</div>
              <div className="v2-pre v2-workspaces-log" data-testid="v2-ws-job-log">
                {job.events.map((event, index) => (
                  <div key={index}>
                    <span className="v2-muted">{event.ts.slice(11, 19)}</span>{" "}
                    <span className={event.level === "error" ? "v2-workspaces-err" : "v2-workspaces-stage"}>
                      {event.stage}
                    </span>{" "}
                    {event.msg}
                  </div>
                ))}
              </div>
            </>
          ) : null}
        </Card>
      )}

      <GrantsCard workspaceId={workspaceId} onTotal={setGrantedTotal} />

      <Confirm
        open={confirm === "bootstrap"}
        title={t(resume ? "v2.workspaces.bootstrapResume" : "v2.workspaces.bootstrapRun")}
        body={t("v2.workspaces.bootstrapConfirm", { name: row.name, account: row.account_id, region: row.region })}
        confirmLabel={t(resume ? "v2.workspaces.bootstrapResume" : "v2.workspaces.bootstrapRun")}
        busy={busy}
        onConfirm={() => void startBootstrap()}
        onClose={() => setConfirm(null)}
      />
      <Confirm
        open={confirm === "detach"}
        title={t("v2.workspaces.detachTitle")}
        body={t("workspacesPage.detail.deleteBody", { name: row.name })}
        confirmLabel={t("v2.workspaces.detach")}
        danger
        busy={busy}
        onConfirm={() => void detach()}
        onClose={() => setConfirm(null)}
      />
      <Confirm
        open={confirm === "purge"}
        title={t("v2.workspaces.purgeTitle")}
        body={purgeBody()}
        confirmLabel={t("v2.workspaces.purge")}
        danger
        busy={busy || !purgePreview}
        onConfirm={() => void purge()}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}
