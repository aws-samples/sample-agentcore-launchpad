import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  Btn,
  Chip,
  ConfirmDialog,
  DataTable,
  Pager,
  PAGE_SIZES,
  Panel,
  useToast,
  ViewHead,
} from "../../components";
import {
  api,
  ApiError,
  type StageInfo,
  type Workspace,
  type WorkspaceBootstrapJob,
  type WorkspaceGrantFilter,
  type WorkspaceGrants,
  type WorkspacePurgeResult,
} from "../../lib/api";
import { STATUS_TONE } from "./status";

/** Grant-state filters, in the order the toolbar renders them. */
const GRANT_FILTERS: WorkspaceGrantFilter[] = ["all", "granted", "ungranted"];

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
  const [params, setParams] = useSearchParams();
  const [row, setRow] = useState<Workspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmPurge, setConfirmPurge] = useState(false);
  /** the dry run behind the purge dialog: what a purge would take with it */
  const [purgePreview, setPurgePreview] = useState<WorkspacePurgeResult | null>(null);
  const [jobId, setJobId] = useState<string | null>(() => readJobIds()[workspaceId] ?? null);
  const [job, setJob] = useState<WorkspaceBootstrapJob | null>(null);

  // Grants table state. Search / filter / page live in the URL (the console's
  // sub-page convention) so a view of this table is linkable and survives a
  // reload; `ws` and `view` are carried along by `setGrantParam`.
  const query = params.get("gq") ?? "";
  const filterParam = params.get("granted");
  const filter: WorkspaceGrantFilter =
    filterParam === "granted" || filterParam === "ungranted" ? filterParam : "all";
  const pageNo = Math.max(1, Number(params.get("gpage") ?? "1") || 1);
  const [size, setSize] = useState<number>(PAGE_SIZES[0]);
  const [grants, setGrants] = useState<WorkspaceGrants | null>(null);
  const [grantsLoading, setGrantsLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [grantBusy, setGrantBusy] = useState(false);
  const grantsSeq = useRef(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.listWorkspaces();
      setRow(list.workspaces.find((entry) => entry.id === workspaceId) ?? null);
    } catch (err) {
      toast(t("workspacesPage.loadFailed", { msg: String(err) }), "crit");
    } finally {
      setLoading(false);
    }
  }, [t, toast, workspaceId]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadGrants = useCallback(async () => {
    const id = ++grantsSeq.current;
    setGrantsLoading(true);
    try {
      const page = await api.listWorkspaceGrants(workspaceId, {
        q: query || undefined,
        granted: filter,
        limit: size,
        offset: (pageNo - 1) * size,
      });
      if (id !== grantsSeq.current) return; // ignore out-of-order responses
      setGrants(page);
    } catch (err) {
      if (id !== grantsSeq.current) return;
      // A gone workspace is the expected case (detached or purged elsewhere);
      // the panel's empty state says so rather than a toast on every poll.
      setGrants(null);
      if (!(err instanceof ApiError && err.code === "workspace.not_found")) {
        toast(err instanceof Error ? err.message : String(err), "crit");
      }
    } finally {
      if (id === grantsSeq.current) setGrantsLoading(false);
    }
  }, [filter, pageNo, query, size, toast, workspaceId]);

  useEffect(() => {
    void loadGrants();
  }, [loadGrants]);

  // Selection is page-local: the checkboxes act on rows the operator can see, so
  // a page, search or filter change starts a fresh selection rather than
  // carrying invisible ids into the next batch.
  useEffect(() => {
    setSelected(new Set());
  }, [filter, pageNo, query, size]);

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

  /** A refusal names the rule that stopped it, and the console has copy per reason. */
  const purgeFailed = (err: unknown) => {
    if (err instanceof ApiError && err.code === "workspace.purge_refused") {
      const reason = (err.detail as { reason?: string } | null)?.reason ?? "";
      toast(
        t("workspacesPage.detail.purgeRefused", {
          reason: t(`workspacesPage.detail.purgeReason.${reason}`, err.message),
        }),
        "crit",
      );
    } else {
      toast(err instanceof Error ? err.message : String(err), "crit");
    }
  };

  const openPurge = async () => {
    if (!row) return;
    setPurgePreview(null);
    setConfirmPurge(true);
    try {
      setPurgePreview(await api.purgeWorkspace(row.id, { dryRun: true }));
    } catch (err) {
      // The dry run runs the same guardrails, so a refusal here means the button
      // was stale (a bootstrap started, an agent appeared): say so and re-read
      // rather than showing a dialog whose confirm would be refused too.
      setConfirmPurge(false);
      purgeFailed(err);
      await reload();
    }
  };

  const purge = async () => {
    if (!row) return;
    setBusy(true);
    try {
      await api.purgeWorkspace(row.id);
      toast(t("workspacesPage.purged", { name: row.name }), "good");
      await onChanged();
      onBack();
    } catch (err) {
      purgeFailed(err);
      await reload();
    } finally {
      setBusy(false);
      setConfirmPurge(false);
    }
  };

  /** Set one grants-table param, keeping `view`/`ws` and restarting paging. */
  const setGrantParam = (key: string, value: string | null) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "gpage") next.delete("gpage");
        return next;
      },
      { replace: true },
    );
  };

  const rows = grants?.users ?? [];
  const pageIds = rows.map((user) => user.id);
  const allOnPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const togglePage = () => {
    setSelected(() => (allOnPageSelected ? new Set() : new Set(pageIds)));
  };

  const applyGrants = async (action: "grant" | "revoke") => {
    const ids = [...selected];
    if (ids.length === 0) return;
    setGrantBusy(true);
    try {
      const result = await api.updateWorkspaceGrants(workspaceId, { [action]: ids });
      // The count reported is the selection, not `added`/`removed`: re-granting
      // an account that already held it changes no row but is not a failure, and
      // "granted 3 accounts" is what the operator just asked for.
      toast(
        t(
          action === "grant"
            ? "workspacesPage.detail.grantsGranted"
            : "workspacesPage.detail.grantsRevoked",
          { count: ids.length, total: result.granted_total },
        ),
        "good",
      );
      setSelected(new Set());
      await loadGrants();
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "crit");
    } finally {
      setGrantBusy(false);
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
  // Only registration residue is purgeable: a READY environment is in use, and
  // retiring one means deleting its AWS resources, which purge does not do.
  const canPurge =
    row !== null &&
    !row.is_default &&
    (row.bootstrap_status === "registered" || row.bootstrap_status === "failed") &&
    !busy;

  const purgeBody = (): string => {
    const name = row?.name ?? workspaceId;
    if (!purgePreview) return t("workspacesPage.detail.purgeChecking");
    const rows = Object.entries(purgePreview.rows)
      .map(([table, count]) => `${table}: ${count}`)
      .join(" · ");
    const body = rows
      ? t("workspacesPage.detail.purgeBody", { name, rows })
      : t("workspacesPage.detail.purgeBodyEmpty", { name });
    if (!purgePreview.resource_keys.length) return body;
    const keys = purgePreview.resource_keys.join(", ");
    return `${body} ${t("workspacesPage.detail.purgeAwsNote", { keys })}`;
  };

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
              <span className="v">{grants?.granted_total ?? "—"}</span>
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
            {canPurge ? (
              <Btn
                style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
                onClick={() => void openPurge()}
                data-testid="workspace-purge-btn"
              >
                {t("workspacesPage.detail.purge")}
              </Btn>
            ) : null}
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
        pad={false}
        title={t("workspacesPage.detail.grantsTitle")}
        sub={t("workspacesPage.detail.grantsSub", {
          granted: grants?.granted_total ?? 0,
        })}
        style={{ "--i": 3 } as CSSProperties}
      >
        <div className="filters">
          {GRANT_FILTERS.map((option) => (
            <button
              key={option}
              className={`fsel${filter === option ? " on-ok" : ""}`}
              onClick={() => setGrantParam("granted", option === "all" ? null : option)}
              data-testid={`workspace-grants-filter-${option}`}
            >
              {t(`workspacesPage.detail.grantFilters.${option}`)}
            </button>
          ))}
          <input
            className="fsearch"
            value={query}
            onChange={(event) => setGrantParam("gq", event.target.value || null)}
            placeholder={t("workspacesPage.detail.grantsSearch")}
            aria-label={t("workspacesPage.detail.grantsSearch")}
            data-testid="workspace-grants-search"
          />
          <span className="spacer" />
          <Btn
            disabled={selected.size === 0 || grantBusy}
            onClick={() => void applyGrants("grant")}
            data-testid="workspace-grants-grant"
          >
            {t("workspacesPage.detail.grantSelected", { count: selected.size })}
          </Btn>
          <Btn
            disabled={selected.size === 0 || grantBusy}
            style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
            onClick={() => void applyGrants("revoke")}
            data-testid="workspace-grants-revoke"
          >
            {t("workspacesPage.detail.revokeSelected", { count: selected.size })}
          </Btn>
        </div>
        <div className="pbody" style={{ paddingBottom: 0 }}>
          <div className="note" data-testid="workspace-grants-hint">
            <span className="i">[i]</span>
            {t("workspacesPage.detail.adminHint")}
          </div>
        </div>
        <DataTable
          columns={[
            {
              key: "sel",
              label: (
                <input
                  type="checkbox"
                  checked={allOnPageSelected}
                  disabled={pageIds.length === 0}
                  onChange={togglePage}
                  aria-label={t("workspacesPage.detail.selectPage")}
                  data-testid="workspace-grants-select-page"
                />
              ),
            },
            { key: "username", label: t("workspacesPage.detail.grantCols.account") },
            { key: "email", label: t("workspacesPage.detail.grantCols.email") },
            { key: "role", label: t("workspacesPage.detail.grantCols.role") },
            { key: "status", label: t("workspacesPage.detail.grantCols.status") },
            { key: "granted", label: t("workspacesPage.detail.grantCols.granted") },
          ]}
          isEmpty={!grantsLoading && rows.length === 0}
          empty={
            query || filter !== "all"
              ? t("workspacesPage.detail.noMatchingAccounts")
              : t("workspacesPage.detail.noAccounts")
          }
        >
          {rows.map((user) => (
            <tr key={user.id} data-testid={`workspace-grant-row-${user.username}`}>
              <td>
                <input
                  type="checkbox"
                  checked={selected.has(user.id)}
                  onChange={() => toggleOne(user.id)}
                  aria-label={user.username}
                  data-testid={`workspace-grant-select-${user.username}`}
                />
              </td>
              <td>
                <b>{user.username}</b>
              </td>
              <td>
                <small className="dim">{user.email}</small>
              </td>
              <td>
                <Chip tone="muted">{t("auth.roleMember")}</Chip>
              </td>
              <td>
                <Chip tone={user.status === "active" ? "good" : "muted"}>
                  {t(`usersPage.filters.${user.status}`)}
                </Chip>
              </td>
              <td>
                {user.granted ? (
                  <Chip tone="good">{t("workspacesPage.detail.grantYes")}</Chip>
                ) : (
                  <span className="dim mono">—</span>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
        <Pager
          total={grants?.total ?? 0}
          page={pageNo}
          size={size}
          onPage={(next) => setGrantParam("gpage", next > 1 ? String(next) : null)}
          onSize={(next) => {
            setSize(next);
            setGrantParam("gpage", null);
          }}
          always
        />
      </Panel>

      <ConfirmDialog
        open={confirmDelete}
        title={t("workspacesPage.detail.deleteTitle")}
        body={t("workspacesPage.detail.deleteBody", { name: row?.name ?? workspaceId })}
        confirmLabel={t("workspacesPage.detail.delete")}
        onConfirm={() => void remove()}
        onCancel={() => setConfirmDelete(false)}
      />

      <ConfirmDialog
        open={confirmPurge}
        title={t("workspacesPage.detail.purgeTitle")}
        body={purgeBody()}
        confirmLabel={t("workspacesPage.detail.purge")}
        onConfirm={() => void purge()}
        onCancel={() => setConfirmPurge(false)}
      />
    </>
  );
}
