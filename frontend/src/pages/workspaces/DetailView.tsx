import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  Btn,
  Chip,
  ConfirmDialog,
  Panel,
  useToast,
  ViewHead,
} from "../../components";
import {
  api,
  ApiError,
  type ConsoleUser,
  type StageInfo,
  type Workspace,
  type WorkspaceBootstrapJob,
} from "../../lib/api";
import { STATUS_TONE } from "./status";

/** The bootstrap job's stages, in order (`services/workspace_bootstrap.py`). */
const STAGE_ORDER = [
  "validate-access",
  "iam",
  "storage",
  "codebuild",
  "cognito",
  "gateway",
  "memory",
  "registry",
  "observability",
  "finalize",
] as const;

const PENDING_STAGES: StageInfo[] = STAGE_ORDER.map((name) => ({
  name,
  status: "pending",
  detail: "",
}));

/**
 * The job id of a running bootstrap, per workspace.
 *
 * A bootstrap outlives the page: the backend resumes an interrupted run, and
 * without this the console could no longer say which job to watch after a
 * reload (there is no "latest job of this workspace" endpoint).
 */
const JOB_STORE = "launchpad_ws_bootstrap_jobs";

function readJobIds(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(JOB_STORE);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}

function rememberJobId(workspaceId: string, jobId: string): void {
  try {
    window.localStorage.setItem(
      JOB_STORE,
      JSON.stringify({ ...readJobIds(), [workspaceId]: jobId }),
    );
  } catch {
    /* storage unavailable — progress is still visible in this session */
  }
}

function stageClass(stage: StageInfo): string {
  if (stage.status === "succeeded" || stage.status === "skipped") return " done";
  if (stage.status === "running") return " now";
  if (stage.status === "failed") return " fail";
  return "";
}

function stageNode(stage: StageInfo, index: number): string {
  if (stage.status === "succeeded" || stage.status === "skipped") return "✓";
  if (stage.status === "running") return "●";
  if (stage.status === "failed") return "✕";
  return String(index + 1);
}

function fmt(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function WorkspaceDetailView({
  workspaceId,
  onBack,
  onChanged,
}: {
  workspaceId: string;
  onBack: () => void;
  /** re-read the list behind this view (table + topbar switcher) */
  onChanged: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [row, setRow] = useState<Workspace | null>(null);
  const [users, setUsers] = useState<ConsoleUser[]>([]);
  /** how many accounts exist, so a truncated chip list says so */
  const [userTotal, setUserTotal] = useState(0);
  const [grantedCount, setGrantedCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [busyUser, setBusyUser] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [jobId, setJobId] = useState<string | null>(() => readJobIds()[workspaceId] ?? null);
  const [job, setJob] = useState<WorkspaceBootstrapJob | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [list, accounts] = await Promise.all([
        api.listWorkspaces(),
        api.listUsers({ limit: 200 }),
      ]);
      setRow(list.workspaces.find((entry) => entry.id === workspaceId) ?? null);
      setUsers(accounts.items);
      setUserTotal(accounts.total);
      try {
        const grants = await api.listWorkspaceGrants(workspaceId);
        setGrantedCount(grants.users.length);
      } catch {
        setGrantedCount(null); // the workspace is gone; the empty state says so
      }
    } catch (err) {
      toast(t("workspacesPage.loadFailed", { msg: String(err) }), "crit");
    } finally {
      setLoading(false);
    }
  }, [t, toast, workspaceId]);

  useEffect(() => {
    void load();
  }, [load]);

  // A run started in another browser (or by another admin) is not in
  // localStorage — ask the backend which job is the latest one.
  useEffect(() => {
    if (jobId || workspaceId === "default") return;
    let alive = true;
    void api
      .getWorkspaceBootstrap(workspaceId)
      .then((status) => {
        if (!alive || !status.job) return;
        rememberJobId(workspaceId, status.job.id);
        setJobId(status.job.id);
      })
      .catch(() => {
        /* the panel just shows "no run" */
      });
    return () => {
      alive = false;
    };
  }, [jobId, workspaceId]);

  const reload = useCallback(async () => {
    await load();
    await onChanged();
  }, [load, onChanged]);

  const poll = useCallback(async () => {
    if (!jobId) return;
    try {
      // The job belongs to the workspace under management, which is not the
      // console's current selection — name it, or the scoped route 404s.
      const next = await api.getWorkspaceJob(jobId, workspaceId);
      setJob(next);
      if (next.status === "succeeded" || next.status === "failed") await reload();
    } catch {
      /* retried on the next tick */
    }
  }, [jobId, reload, workspaceId]);

  useEffect(() => {
    if (!jobId) return;
    void poll();
    if (job?.status === "succeeded" || job?.status === "failed") return;
    const timer = setInterval(() => void poll(), 2000);
    return () => clearInterval(timer);
  }, [job?.status, jobId, poll]);

  const startBootstrap = async () => {
    if (!row) return;
    setBusy(true);
    try {
      const ack = await api.bootstrapWorkspace(row.id);
      rememberJobId(row.id, ack.job_id);
      setJobId(ack.job_id);
      setJob(null);
      toast(t("workspacesPage.bootstrapQueued", { name: row.name }), "good");
      await reload();
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "crit");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!row) return;
    setBusy(true);
    try {
      await api.deleteWorkspace(row.id);
      toast(t("workspacesPage.deleted", { name: row.name }), "good");
      await onChanged();
      onBack();
    } catch (err) {
      if (err instanceof ApiError && err.code === "workspace.in_use") {
        const rows = (err.detail as { rows?: Record<string, number> } | null)?.rows ?? {};
        const listed = Object.entries(rows)
          .map(([table, count]) => `${table}: ${count}`)
          .join(" · ");
        toast(t("workspacesPage.deleteInUse", { rows: listed }), "crit");
      } else {
        toast(err instanceof Error ? err.message : String(err), "crit");
      }
    } finally {
      setBusy(false);
      setConfirmDelete(false);
    }
  };

  const toggleGrant = async (user: ConsoleUser) => {
    const granted = user.workspaces.includes(workspaceId);
    const next = granted
      ? user.workspaces.filter((id) => id !== workspaceId)
      : [...user.workspaces, workspaceId];
    setBusyUser(user.id);
    try {
      // The backend replaces the whole grant list, so the unchanged ids travel
      // along rather than being inferred from a diff.
      await api.updateUser(user.id, { workspaces: next });
      toast(
        t(granted ? "workspacesPage.grantRevoked" : "workspacesPage.grantAdded", {
          username: user.username,
        }),
        "good",
      );
      await load();
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "crit");
    } finally {
      setBusyUser(null);
    }
  };

  if (!loading && !row) {
    return (
      <>
        <ViewHead
          kicker={t("workspacesPage.kicker")}
          title={t("workspacesPage.detail.gone")}
        />
        <Panel brk>
          <div className="empty" data-testid="workspace-gone">
            {t("workspacesPage.detail.goneBody", { id: workspaceId })}
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
            <Btn onClick={onBack}>{t("workspacesPage.back")}</Btn>
          </div>
        </Panel>
      </>
    );
  }

  const stages = job?.payload?.stages ?? PENDING_STAGES;
  const bootstrapping = row?.bootstrap_status === "bootstrapping";
  const canBootstrap =
    row !== null &&
    !row.is_default &&
    !bootstrapping &&
    row.bootstrap_status !== "ready" &&
    !busy;

  return (
    <>
      <ViewHead
        kicker={t("workspacesPage.kicker")}
        title={row?.name ?? workspaceId}
        meta={t("workspacesPage.detail.meta", { id: workspaceId })}
      />

      <div className="eval-grid">
        <Panel
          brk
          title={t("workspacesPage.detail.title")}
          end={
            row ? (
              <Chip tone={STATUS_TONE[row.bootstrap_status]}>
                {t(`workspacesPage.status.${row.bootstrap_status}`)}
              </Chip>
            ) : null
          }
          style={{ "--i": 0 } as CSSProperties}
        >
          <div className="gov-kv-list">
            <div className="kv">
              <span className="k">{t("workspacesPage.cols.id")}</span>
              <span className="v">{row?.id ?? "—"}</span>
            </div>
            <div className="kv">
              <span className="k">{t("workspacesPage.cols.account")}</span>
              <span className="v">
                {row?.account_id ?? "—"}
                {row?.cross_account ? (
                  <>
                    {" "}
                    <Chip tone="blue">{t("workspacesPage.external")}</Chip>
                  </>
                ) : null}
              </span>
            </div>
            {row?.cross_account ? (
              <div className="kv" data-testid="workspace-role-arn">
                <span className="k">{t("workspacesPage.detail.roleArn")}</span>
                <span className="v mono">{row.role_arn ?? "—"}</span>
              </div>
            ) : null}
            <div className="kv">
              <span className="k">{t("workspacesPage.cols.region")}</span>
              <span className="v">{row?.region ?? "—"}</span>
            </div>
            <div className="kv">
              <span className="k">{t("workspacesPage.cols.created")}</span>
              <span className="v">{fmt(row?.created_at)}</span>
            </div>
            <div className="kv">
              <span className="k">{t("workspacesPage.detail.grantedMembers")}</span>
              <span className="v">{grantedCount ?? "—"}</span>
            </div>
          </div>
          <div
            style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 14 }}
          >
            <Btn onClick={onBack}>{t("workspacesPage.back")}</Btn>
            <Btn
              disabled={row === null || row.is_default || busy}
              onClick={() => setConfirmDelete(true)}
              data-testid="workspace-delete-btn"
            >
              {t("workspacesPage.detail.delete")}
            </Btn>
            <Btn
              primary
              disabled={!canBootstrap}
              onClick={() => void startBootstrap()}
              data-testid="workspace-bootstrap-btn"
            >
              {t(
                bootstrapping
                  ? "workspacesPage.detail.bootstrapRunning"
                  : row?.bootstrap_status === "failed"
                    ? "workspacesPage.detail.bootstrapResume"
                    : "workspacesPage.detail.bootstrapRun",
              )}
            </Btn>
          </div>
          {row?.is_default ? (
            <div className="note" data-testid="workspace-hub-note">
              <span className="i">[i]</span>
              {t("workspacesPage.detail.hubNote")}
            </div>
          ) : null}
        </Panel>

        <Panel brk title={t("workspacesPage.detail.howTitle")} style={{ "--i": 1 } as CSSProperties}>
          {[1, 2, 3, 4].map((step) => (
            <div className="kv" key={step}>
              <span className="k">{step}</span>
              <span className="v">{t(`workspacesPage.detail.how${step}`)}</span>
            </div>
          ))}
          <div className="note">
            <span className="i">[i]</span>
            {t("workspacesPage.detail.howNote")}
          </div>
        </Panel>
      </div>

      {/* Skipped for the hub, whose environment `make bootstrap` (CDK) provisions
          and where this job can never run — ten PENDING rows beside a READY chip
          would read as "never bootstrapped". */}
      {row?.is_default ? null : (
        <Panel
          brk
          pad={false}
          title={t("workspacesPage.detail.stagesTitle")}
          sub={jobId ? `job #${jobId.slice(0, 8)}` : t("workspacesPage.detail.noRun")}
          style={{ "--i": 2 } as CSSProperties}
        >
          <div className="pipeline" data-testid="workspace-stages">
            {stages.map((stage, index) => (
              <div key={stage.name} className={`pstage${stageClass(stage)}`}>
                <div className="node">{stageNode(stage, index)}</div>
                <div className="pn">{t(`workspacesPage.stages.${stage.name}`)}</div>
                <div className="pt">{stage.detail || "—"}</div>
              </div>
            ))}
          </div>
          {job?.error ? (
            <div className="pbody" style={{ paddingTop: 0 }}>
              <div className="note" style={{ borderColor: "var(--crit)" }}>
                <span className="i" style={{ color: "var(--crit)" }}>
                  [✕]
                </span>
                <span className="mono">{job.error}</span>
              </div>
            </div>
          ) : null}
          {job?.events?.length ? (
            <div
              className="code"
              style={{
                border: 0,
                maxHeight: 260,
                overflowY: "auto",
                margin: 0,
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
                overflowX: "hidden",
              }}
              data-testid="workspace-job-log"
            >
              {job.events.map((event, index) => (
                <div key={index}>
                  <span className="cm">{event.ts.slice(11, 19)}</span>{" "}
                  <span className={event.level === "error" ? "k1" : "k2"}>{event.stage}</span>{" "}
                  {event.msg}
                </div>
              ))}
            </div>
          ) : null}
        </Panel>
      )}

      <Panel
        brk
        title={t("workspacesPage.detail.grantsTitle")}
        sub={t("workspacesPage.detail.grantsSub")}
        style={{ "--i": 3 } as CSSProperties}
      >
        {users.length === 0 ? (
          <div className="empty">{t("workspacesPage.detail.noAccounts")}</div>
        ) : (
          <div className="selchips" data-testid="workspace-grants">
            {users.map((user) =>
              user.role === "admin" ? (
                <span className="selchip" key={user.id} title={t("workspacesPage.detail.adminHint")}>
                  {user.username} · {t("workspacesPage.detail.adminAll")}
                </span>
              ) : (
                <button
                  key={user.id}
                  type="button"
                  className={`selchip${user.workspaces.includes(workspaceId) ? " on" : ""}`}
                  style={{ cursor: "pointer" }}
                  disabled={busyUser === user.id}
                  title={t("workspacesPage.detail.grantHint")}
                  onClick={() => void toggleGrant(user)}
                  data-testid={`workspace-grant-${user.username}`}
                >
                  {user.username}
                </button>
              ),
            )}
          </div>
        )}
        {userTotal > users.length ? (
          <div className="note" data-testid="workspace-grants-truncated">
            <span className="i">[i]</span>
            {t("workspacesPage.detail.grantsTruncated", {
              shown: users.length,
              total: userTotal,
            })}
          </div>
        ) : null}
      </Panel>

      <ConfirmDialog
        open={confirmDelete}
        title={t("workspacesPage.detail.deleteTitle")}
        body={t("workspacesPage.detail.deleteBody", { name: row?.name ?? workspaceId })}
        confirmLabel={t("workspacesPage.detail.delete")}
        onConfirm={() => void remove()}
        onCancel={() => setConfirmDelete(false)}
      />
    </>
  );
}
