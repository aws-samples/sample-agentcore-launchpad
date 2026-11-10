import { Download, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { methodLabel } from "../../components/methodChipMeta";
import {
  type AgentInfo,
  type AgentVersionsInfo,
  api,
  ApiError,
  type DeploymentInfo,
  errorMessage,
  type JobInfo,
  type StageInfo,
} from "../../lib/api";
import { jobPolicyCompileFailed } from "../../lib/policy-job";
import { creatorLabel, fmtTime } from "../format";
import { useMayMutate } from "../ownership";
import { AgentIdentity } from "./agents/AgentIdentity";
import { AgentWizard } from "./agents/AgentWizard";
import { TraceStatusTag } from "./agents/TraceStatus";
import { v2EditPath } from "./agents/classicUrl";
import { useLoad, usePaged, useV2Toast } from "../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  FilterSelect,
  FlowHeader,
  Kpi,
  LinkButton,
  PageHeader,
  Pager,
  SearchInput,
  Spin,
  Table,
  Tag,
  type TagTone,
} from "../ui";
import { entryShown } from "../hiddenSurfaces";
import { awsStatusLabel, wordStatusPrefix } from "../awsStatus";
import { zonedClock } from "../../lib/timestamps";

type Method = AgentInfo["method"];

const METHOD_TONE: Record<Method, TagTone> = {
  harness: "orange",
  zip_runtime: "green",
  studio: "green",
  byoc: "blue",
  discovered_runtime: "gray",
};

const STATUS_TONE: Record<string, TagTone> = {
  active: "green",
  deploying: "blue",
  failed: "red",
  draft: "gray",
};

const STAGE_TONE: Record<StageInfo["status"], TagTone> = {
  succeeded: "green",
  skipped: "gray",
  running: "blue",
  failed: "red",
  pending: "outline",
  cancelled: "gray", // N16-obs83: the agent was deleted while the job ran
  interrupted: "orange", // N17-obs113: stopped with the backend; resumes at the next start
};

const editPath = v2EditPath;

const EDITABLE_METHODS: Method[] = ["harness", "zip_runtime", "byoc"];

/** `?view=edit&id=` — reads the agent fresh, then opens the wizard on its stored spec. */
function AgentEdit({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const { data, error, reload } = useLoad(() => api.getAgent(id), `agent-edit:${id}`);
  // canvas agents and system presets have their own editors
  const elsewhere = data && (data.method === "studio" || data.system) ? editPath(data) : null;
  useEffect(() => {
    if (elsewhere) navigate(elsewhere, { replace: true });
  }, [elsewhere, navigate]);
  const back = () => setParams({ view: "detail", id });
  if (error && !data)
    return (
      <>
        <FlowHeader title={t("v2.common.edit")} onBack={back} />
        <Alert tone="error">
          {error}{" "}
          <button type="button" className="v2-link" onClick={reload}>
            {t("v2.common.retry")}
          </button>
        </Alert>
      </>
    );
  if (!data || elsewhere) return <Spin />;
  const blocked = !EDITABLE_METHODS.includes(data.method)
    ? t("v2.agents.wizard.editUnsupported")
    : data.status === "deploying"
      ? t("apiErrors.agent.deploy_in_progress", "a deployment is already in progress for this agent")
      : null;
  if (blocked)
    return (
      <>
        <FlowHeader title={t("v2.agents.wizard.editTitle", { name: data.name })} onBack={back} />
        <Alert tone="warn">{blocked}</Alert>
      </>
    );
  return <AgentWizard key={id} edit={data} />;
}

/** The platform revision (one per (re)publish): the list carries it as
 *  `revision`, the single-agent read as its `deployments` history. An imported
 *  runtime shows its AWS version instead. */
function versionLabel(agent: AgentInfo): string {
  if (agent.method === "discovered_runtime") return agent.version ? `v${agent.version}` : "—";
  const revision = agent.revision ?? agent.deployments?.length;
  return revision ? String(revision) : "—";
}

/** Row permissions, mirroring the classic list: editing re-publishes; a system
 *  preset is edited by administrators only and never deleted here. `creatorOnly` mirrors
 *  the server's creator-or-admin gate on redeploy / delete (`require_agent_creator`,
 *  N15-obs9): the role may allow the action, but the row belongs to another member (or to
 *  nobody — an administrator's); a discovered runtime detaches for any `agents.delete` holder. */
function useAgentPermissions() {
  const { can, isAdmin } = useAuth();
  const mayMutate = useMayMutate();
  const canDeploy = can("agents.deploy");
  const canDelete = can("agents.delete");
  return {
    canEdit: (a: AgentInfo) =>
      a.method !== "discovered_runtime" && canDeploy && a.status !== "deploying" && (!a.system || isAdmin),
    canDelete: (a: AgentInfo) => canDelete && !a.system,
    creatorOnly: (a: AgentInfo) => a.method !== "discovered_runtime" && !mayMutate(a),
  };
}

/** The canaries a refused delete names (`409 agent.in_use {canaries}`, N16-obs111). */
interface BlockingCanary {
  id: string;
  name: string;
  status: string;
  stage: string;
}

/** The ACTIVE evaluation runs a refused delete names (`409 agent.in_use {runs}`, N17-obs18). */
interface BlockingRun {
  id: string;
  name: string | null;
  status: string;
}

interface BlockingReferrers {
  canaries: BlockingCanary[];
  runs: BlockingRun[];
}

function blockingReferrers(err: unknown): BlockingReferrers | null {
  if (!(err instanceof ApiError) || err.code !== "agent.in_use") return null;
  const detail = err.detail as { canaries?: BlockingCanary[]; runs?: BlockingRun[] } | null | undefined;
  return { canaries: detail?.canaries ?? [], runs: detail?.runs ?? [] };
}

/** The run detail page (`/v2/eval/tasks?view=detail&id=<run>`, where stop lives). */
function runDetailPath(id: string): string {
  return `/v2/eval/tasks?view=detail&id=${encodeURIComponent(id)}`;
}

/** The canary detail page (`V2Experiments`: `?canary=<id>` opens `CanaryDetail`). */
function canaryDetailPath(id: string): string {
  return `/v2/eval/experiments?mode=canary&canary=${encodeURIComponent(id)}`;
}

/** Delete confirmation shared by the list and the detail page. */
function useAgentActions(onDone: (action: "deleted", agent: AgentInfo) => void) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [pending, setPending] = useState<{ kind: "delete"; agent: AgentInfo } | null>(null);
  const [busy, setBusy] = useState(false);
  // N16-obs111 / N17-obs18: a delete refused with `409 agent.in_use` keeps the dialog open and
  // names the running canaries (linked to their detail page, where cleanup / rollback /
  // complete live) and the ACTIVE evaluation runs (linked to the run detail, where stop lives)
  const [blocked, setBlocked] = useState<BlockingReferrers | null>(null);

  const close = () => {
    setPending(null);
    setBlocked(null);
  };

  const run = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      await api.deleteAgent(pending.agent.id);
      toast("success", t("v2.agents.deleted", { name: pending.agent.name }));
      onDone("deleted", pending.agent);
      close();
    } catch (err) {
      const referrers = blockingReferrers(err);
      if (referrers) {
        // N17-obs3: the dialog lists the blocking canaries / runs — it IS the message; no
        // generic toast above it
        setBlocked(referrers);
      } else {
        toast("error", errorMessage(err));
      }
    } finally {
      setBusy(false);
    }
  };

  const external = pending?.agent.method === "discovered_runtime";
  const dialog = (
    <Confirm
      open={pending !== null}
      title={external ? t("v2.agents.removeTitle") : t("v2.agents.deleteTitle")}
      body={
        <>
          {t(external ? "v2.agents.removeBody" : "v2.agents.deleteBody", { name: pending?.agent.name ?? "" })}
          {blocked && (
            <div data-testid="v2-agent-delete-blocked" style={{ marginTop: 12 }}>
              <Alert tone="warn">
                {blocked.canaries.length > 0 && (
                  <>
                    {t("v2.agents.deleteBlockedByCanaries", { count: blocked.canaries.length })}
                    <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                      {blocked.canaries.map((c) => (
                        <li key={c.id}>
                          <Link to={canaryDetailPath(c.id)} onClick={close} data-testid={`v2-agent-delete-blocked-canary-${c.id}`}>
                            {t("v2.agents.deleteBlockedCanaryLink", {
                              name: c.name,
                              status: t(`v2.canary.status.${c.status}`, { defaultValue: c.status }),
                              stage: t(`v2.canary.stage.${c.stage}`, { defaultValue: c.stage }),
                            })}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
                {blocked.runs.length > 0 && (
                  <div style={{ marginTop: blocked.canaries.length > 0 ? 8 : 0 }}>
                    {t("v2.agents.deleteBlockedByRuns", { count: blocked.runs.length })}
                    <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                      {blocked.runs.map((r) => (
                        <li key={r.id}>
                          <Link to={runDetailPath(r.id)} onClick={close} data-testid={`v2-agent-delete-blocked-run-${r.id}`}>
                            {t("v2.agents.deleteBlockedRunLink", {
                              name: r.name ?? r.id,
                              status: awsStatusLabel(t, r.status),
                            })}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </Alert>
            </div>
          )}
        </>
      }
      confirmLabel={external ? t("v2.agents.remove") : t("v2.common.delete")}
      danger={pending?.kind === "delete"}
      busy={busy}
      onConfirm={() => void run()}
      onClose={close}
    />
  );
  return {
    askDelete: (agent: AgentInfo) => {
      setBlocked(null);
      setPending({ kind: "delete", agent });
    },
    dialog,
  };
}

// ─── list ──────────────────────────────────────────────────────────────────
function AgentList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const { can } = useAuth();
  const perms = useAgentPermissions();
  const { data, loading, error, reload } = useLoad(() => api.listAgents(), "agents");
  const actions = useAgentActions(() => reload());
  const [method, setMethod] = useState("");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");

  const agents = useMemo(() => data?.agents ?? [], [data]);
  const methods = useMemo(() => [...new Set(agents.map((a) => a.method))].sort(), [agents]);
  const statuses = useMemo(() => [...new Set(agents.map((a) => a.status))].sort(), [agents]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return agents.filter((a) => {
      if (method && a.method !== method) return false;
      if (status && a.status !== status) return false;
      return !needle || `${a.name} ${a.id}`.toLowerCase().includes(needle);
    });
  }, [agents, method, status, q]);
  const paged = usePaged(rows, 12);
  const count = (s: string) => agents.filter((a) => a.status === s).length;

  // a deploy in flight moves on its own: refresh the list until it settles
  const deploying = agents.some((a) => a.status === "deploying");
  useEffect(() => {
    if (!deploying) return;
    const timer = window.setInterval(reload, 5000);
    return () => window.clearInterval(timer);
  }, [deploying, reload]);

  const open = (a: AgentInfo) => setParams({ view: "detail", id: a.id });

  const columns: Column<AgentInfo>[] = [
    {
      key: "name",
      title: t("v2.agents.colName"),
      render: (a) => (
        <>
          <span className="v2-row" style={{ flexWrap: "nowrap" }}>
            <LinkButton onClick={() => open(a)} testId={`v2-agent-${a.name}`}>
              {a.name}
            </LinkButton>
            {a.system && (
              <Tag tone="blue" title={t("v2.agents.systemHint")}>
                {t("v2.agents.system")}
              </Tag>
            )}
          </span>
          <span className="sub mono">{a.id}</span>
        </>
      ),
    },
    {
      key: "method",
      title: t("v2.agents.colMethod"),
      render: (a) => <Tag tone={METHOD_TONE[a.method] ?? "gray"}>{methodLabel(a.method)}</Tag>,
    },
    {
      key: "status",
      title: t("v2.agents.colStatus"),
      render: (a) => (
        <span className="v2-row">
          <Tag tone={STATUS_TONE[a.status] ?? "gray"} dot title={a.status === "failed" ? (a.error ?? undefined) : undefined}>
            {t(`status.${a.status}`, { defaultValue: a.status })}
          </Tag>
          {a.last_deploy_failed && (
            <Tag tone="orange" title={a.last_deploy_error ?? undefined} testId={`v2-agent-redeploy-failed-${a.name}`}>
              {t("v2.agents.lastDeployFailed")}
            </Tag>
          )}
          {a.policy_stale && (
            <Tag tone="orange" title={t("v2.agents.policyStaleHint")} testId={`v2-agent-policy-stale-${a.name}`}>
              {t("v2.agents.policyStale")}
            </Tag>
          )}
          {/* D21 (M1-d): this agent's catalog model row changed since its last deploy */}
          {a.catalog_hint?.changed && (
            <Tag
              tone="orange"
              title={t("v2.agents.catalogHintTitle", { deployed: a.catalog_hint.deployed, current: a.catalog_hint.current })}
              testId={`v2-agent-catalog-hint-${a.name}`}
            >
              {t("v2.agents.catalogHint")}
            </Tag>
          )}
        </span>
      ),
    },
    { key: "version", title: t("v2.agents.colVersion"), render: (a) => <span className="mono">{versionLabel(a)}</span> },
    { key: "creator", title: t("v2.common.creator"), render: (a) => creatorLabel(a) },
    { key: "updated", title: t("v2.agents.colUpdated"), className: "nowrap", render: (a) => fmtTime(a.updated_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (a) => (
        <div className="v2-actions">
          {a.invoke_capability.eligible && (
            <LinkButton onClick={() => navigate(`/chat?agent=${a.id}`)}>{t("v2.agents.chat")}</LinkButton>
          )}
          <LinkButton onClick={() => open(a)}>{t("v2.common.view")}</LinkButton>
          {perms.canEdit(a) && !perms.creatorOnly(a) && (
            <LinkButton onClick={() => navigate(editPath(a))} testId={`v2-agent-edit-${a.name}`}>
              {t("v2.common.edit")}
            </LinkButton>
          )}
          {perms.canDelete(a) && !perms.creatorOnly(a) && (
            <LinkButton danger onClick={() => actions.askDelete(a)} testId={`v2-agent-delete-${a.name}`}>
              {a.method === "discovered_runtime" ? t("v2.agents.remove") : t("v2.common.delete")}
            </LinkButton>
          )}
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader title={t("v2.agents.title")} desc={t("v2.agents.desc")} />
      <div className="v2-kpis">
        <Kpi label={t("v2.agents.kpiTotal")} value={data ? agents.length : "—"} testId="v2-agents-kpi-total" />
        <Kpi label={t("v2.agents.kpiActive")} value={data ? count("active") : "—"} tone="good" />
        <Kpi label={t("v2.agents.kpiDeploying")} value={data ? count("deploying") : "—"} />
        <Kpi label={t("v2.agents.kpiFailed")} value={data ? count("failed") : "—"} tone={count("failed") ? "bad" : undefined} />
      </div>
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <Button
            kind="primary"
            onClick={() => setParams({ view: "new" })}
            disabled={!can("agents.deploy")}
            title={can("agents.deploy") ? undefined : t("v2.agents.noPermission")}
            testId="v2-agent-new"
          >
            <Plus size={14} aria-hidden="true" />
            {t("v2.agents.new")}
          </Button>
          {entryShown("import") && (
            <Button onClick={() => navigate("/agents/import")}>
              <Download size={14} aria-hidden="true" />
              {t("v2.agents.import")}
            </Button>
          )}
          <FilterSelect
            label={t("v2.agents.colMethod")}
            value={method}
            allLabel={t("v2.common.all")}
            onChange={setMethod}
            options={methods.map((m) => ({ value: m, label: methodLabel(m) }))}
          />
          <FilterSelect
            label={t("v2.agents.colStatus")}
            value={status}
            allLabel={t("v2.common.all")}
            onChange={setStatus}
            options={statuses.map((s) => ({ value: s, label: t(`status.${s}`, { defaultValue: s }) }))}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.agents.search")} testId="v2-agents-search" />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(a) => a.id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={agents.length ? t("v2.agents.noMatch") : t("v2.agents.empty")}
          testId="v2-agents-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      {actions.dialog}
    </>
  );
}

// ─── detail ────────────────────────────────────────────────────────────────
function DeployProgress({ deployment, job }: { deployment: DeploymentInfo; job: JobInfo | null }) {
  const { t } = useTranslation();
  return (
    <Card
      title={t("v2.agents.deployTitle")}
      sub={deployment.job_id ? `job ${deployment.job_id.slice(0, 8)}` : undefined}
      testId="v2-agent-deploy"
    >
      <div className="v2-stages">
        {deployment.stages.map((s, i) => (
          <div key={s.name} className={`v2-stage ${s.status}`}>
            <span className="n">{s.status === "succeeded" || s.status === "skipped" ? "✓" : s.status === "failed" || s.status === "cancelled" ? "✕" : i + 1}</span>
            <div className="b">
              <div className="t">
                {t(`create.stages.${s.name}`, { defaultValue: s.name })}
                <Tag tone={STAGE_TONE[s.status]}>{t(`v2.agents.stage.${s.status}`)}</Tag>
              </div>
              <div className="d">{s.detail ? wordStatusPrefix(t, s.detail) : "—"}</div>
            </div>
          </div>
        ))}
      </div>
      {job?.error && <Alert tone="error">{job.error}</Alert>}
      {jobPolicyCompileFailed(job) && (
        // a 409 policy.compile_failed stopped the deploy — fix or retire the policy
        <Alert tone="warn" action={<Link className="v2-link" to="/v2/policy">{t("v2.agents.policyStaleOpen")}</Link>}>
          <span data-testid="v2-agent-policy-compile-failed">{t("v2.agents.policyCompileFailedHint")}</span>
        </Alert>
      )}
      <h3 className="v2-sub-title">{t("v2.agents.logTitle")}</h3>
      <pre className="v2-pre" data-testid="v2-agent-log">
        {job?.events.length
          ? job.events.map((e) => `${zonedClock(e.ts)}  ${e.stage.padEnd(9)} ${e.msg}`).join("\n")
          : t("v2.agents.logEmpty")}
      </pre>
    </Card>
  );
}

function VersionsCard({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { data, loading, error } = useLoad<AgentVersionsInfo>(() => api.agentVersions(agentId), `versions:${agentId}`);
  return (
    <Card
      title={t("v2.agents.versionsTitle")}
      sub={data ? t("v2.agents.versionsSub", { latest: data.latest_version ?? "—", ledger: data.ledger_version ?? "—" }) : undefined}
      testId="v2-agent-versions"
    >
      {loading ? (
        <Spin />
      ) : error ? (
        <Alert tone="warn">{error}</Alert>
      ) : data ? (
        <div className="v2-grid-2">
          <Table
            columns={[
              { key: "v", title: t("v2.agents.colVersion"), render: (r) => <span className="mono">{r.version ?? "—"}</span> },
              { key: "s", title: t("v2.agents.colStatus"), render: (r) => awsStatusLabel(t, r.status) },
              { key: "u", title: t("v2.agents.colUpdated"), className: "nowrap", render: (r) => fmtTime(r.last_updated_at) },
            ]}
            rows={data.versions}
            rowKey={(r) => String(r.version)}
            density="dense"
          />
          <Table
            columns={[
              { key: "n", title: t("v2.agents.endpoint"), render: (r) => <span className="mono">{r.name ?? "—"}</span> },
              {
                key: "v",
                title: t("v2.agents.liveVersion"),
                render: (r) => (
                  <span className="mono">
                    {r.live_version ?? "—"}
                    {r.target_version && r.target_version !== r.live_version ? ` → ${r.target_version}` : ""}
                  </span>
                ),
              },
              {
                key: "s",
                title: t("v2.agents.colStatus"),
                render: (r) => <span title={r.failure_reason ?? undefined}>{awsStatusLabel(t, r.status)}</span>,
              },
            ]}
            rows={data.endpoints}
            rowKey={(r) => String(r.name)}
            density="dense"
          />
        </div>
      ) : null}
    </Card>
  );
}

interface ByocSummary {
  artifact_kind?: string;
  image_uri?: string | null;
  entrypoint?: string | null;
  allowed_models?: string[];
  /** server-stamped (N17-obs465): the member's image and the deployed trace-layer image */
  telemetry?: { member_image?: string; layer_image?: string; cn_exporter?: string };
}

function AgentDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const perms = useAgentPermissions();
  const [agent, setAgent] = useState<AgentInfo | null>(null);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const actions = useAgentActions((action, target) => {
    if (action === "deleted") setParams({});
    else setParams({ view: "detail", id: target.id });
  });

  const jobId = agent?.deployments?.[0]?.job_id ?? null;
  const deploying = agent?.status === "deploying";

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const fresh = await api.getAgent(id);
        if (!live) return;
        setAgent(fresh);
        setError(null);
        const latestJob = fresh.deployments?.[0]?.job_id;
        if (latestJob) {
          const j = await api.getJob(latestJob).catch(() => null);
          if (live) setJob(j);
        }
      } catch (err) {
        if (!live) return;
        setError(errorMessage(err));
        // N16-obs76: Refresh after a delete (this tab or another) answered 404 but kept
        // the stale ACTIVE view — the agent is gone, so is its data; the notice takes over
        if (err instanceof ApiError && err.code === "agent.not_found") {
          setAgent(null);
          setJob(null);
        }
      }
    };
    void load();
    // follow a deploy in flight until it settles
    const timer = deploying ? window.setInterval(() => void load(), 2500) : undefined;
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [id, deploying, nonce]);

  if (error && !agent) {
    return (
      <>
        <FlowHeader title={id} onBack={() => setParams({})} />
        <Alert tone="error">{error}</Alert>
      </>
    );
  }
  if (!agent) return <Spin />;

  const spec = (agent.spec ?? {}) as Record<string, unknown>;
  const deployment = agent.deployments?.[0] ?? agent.deployment ?? null;
  const kbs = (spec.knowledge_bases as { kb_id: string; name: string }[] | undefined) ?? [];
  const source = spec.source_harness as { agent_name?: string } | undefined;
  const notes = (spec.conversion_notes as Record<string, string> | undefined) ?? {};
  const byoc = agent.method === "byoc" ? ((spec.byoc as ByocSummary | undefined) ?? null) : null;
  const hasResource = !!agent.arn && agent.method !== "discovered_runtime";
  // the server's creator-or-admin rule on redeploy / delete (N15-obs9): disable + hint, never a 403
  const creatorOnly = perms.creatorOnly(agent);

  return (
    <>
      {error && <Alert tone="error">{error}</Alert>}
      <FlowHeader
        title={
          <span className="v2-row">
            {agent.name}
            <Tag tone={STATUS_TONE[agent.status] ?? "gray"} dot>
              {t(`status.${agent.status}`, { defaultValue: agent.status })}
            </Tag>
          </span>
        }
        onBack={() => setParams({})}
        end={
          <>
            <Button onClick={() => setNonce((n) => n + 1)}>{t("v2.common.refresh")}</Button>
            {agent.invoke_capability.eligible && (
              <Button onClick={() => navigate(`/chat?agent=${agent.id}`)}>{t("v2.agents.chat")}</Button>
            )}
            <Button onClick={() => navigate("/observability")}>{t("v2.agents.observability")}</Button>
            {agent.method !== "discovered_runtime" && (
              <Button onClick={() => setParams({ view: "identity", id: agent.id })} testId="v2-agent-identity-open">
                {t("v2.agents.identity.open")}
              </Button>
            )}
            {perms.canDelete(agent) && (
              <Button
                kind="danger"
                disabled={creatorOnly}
                title={creatorOnly ? t("v2.common.creatorOnly") : undefined}
                onClick={() => actions.askDelete(agent)}
                testId="v2-agent-delete"
              >
                {agent.method === "discovered_runtime" ? t("v2.agents.remove") : t("v2.common.delete")}
              </Button>
            )}
            {perms.canEdit(agent) && (
              <Button
                kind="primary"
                disabled={creatorOnly}
                title={creatorOnly ? t("v2.common.creatorOnly") : undefined}
                onClick={() => navigate(editPath(agent))}
                testId="v2-agent-edit"
              >
                {t("v2.common.edit")}
              </Button>
            )}
          </>
        }
      />
      {creatorOnly && (perms.canDelete(agent) || perms.canEdit(agent)) && (
        <Alert>
          <span data-testid="v2-agent-creator-only">{t("v2.common.creatorOnlyActions", { name: creatorLabel(agent) })}</span>
        </Alert>
      )}
      {agent.status === "failed" && agent.error && <Alert tone="error">{agent.error}</Alert>}
      {agent.last_deploy_failed && (
        <Alert tone="warn">
          {t("v2.agents.lastDeployFailedDetail")}{" "}
          <span className="mono">{agent.last_deploy_error}</span>
        </Alert>
      )}
      {agent.kb_stale && (
        <Alert tone="warn" data-testid="v2-agent-kb-stale">
          {t("v2.agents.kbStaleDetail")}{" "}
          <span className="mono">{(agent.last_strip?.kb_ids ?? []).join(", ")}</span>
        </Alert>
      )}
      {agent.system && (
        <Alert>
          {t("v2.agents.systemNote", { label: agent.system.label, version: agent.system.skill_version ?? "—" })}
        </Alert>
      )}
      {agent.policy_stale && (
        <Alert tone="warn" action={<Link className="v2-link" to="/v2/policy">{t("v2.agents.policyStaleOpen")}</Link>}>
          {t("v2.agents.policyStaleDetail")}
        </Alert>
      )}
      {/* D21 (M1-d): deployed with catalog vN · current vM — Redeploy when the model row changed */}
      {agent.catalog_hint?.changed && (
        <Alert tone="warn">
          <span data-testid="v2-agent-catalog-hint">
            {t("v2.agents.catalogHintDetail", { deployed: agent.catalog_hint.deployed, current: agent.catalog_hint.current })}
          </span>
        </Alert>
      )}
      <Card title={t("v2.agents.basic")} testId="v2-agent-basic">
        <Descriptions
          items={[
            { label: t("v2.agents.colName"), value: agent.name },
            { label: "ID", value: <span className="mono">{agent.id}</span> },
            {
              label: t("v2.agents.colMethod"),
              value: <Tag tone={METHOD_TONE[agent.method] ?? "gray"}>{methodLabel(agent.method)}</Tag>,
            },
            { label: t("v2.agents.colVersion"), value: <span className="mono">{versionLabel(agent)}</span> },
            // Global parity (amendment 9): the only policy fact of a deploy — whether the live
            // version carries the user token the Gateway interceptor decides on
            ...(agent.policy_carrier != null
              ? [{
                  label: t("v2.agents.policyCarrier"),
                  value: (
                    <Tag tone={agent.policy_carrier ? "green" : "gray"} title={t("v2.agents.policyCarrierHint")} testId="v2-agent-policy-carrier">
                      {t(agent.policy_carrier ? "v2.agents.policyCarrierYes" : "v2.agents.policyCarrierNo")}
                    </Tag>
                  ),
                }]
              : []),
            { label: t("v2.agents.model"), value: spec.model_id ? <span className="mono">{String(spec.model_id)}</span> : "—" },
            {
              label: t("v2.agents.protocol"),
              value: String(spec.protocol ?? "http").toUpperCase(),
            },
            { label: t("v2.common.creator"), value: creatorLabel(agent) },
            { label: t("v2.common.createdAt"), value: fmtTime(agent.created_at) },
            { label: t("v2.agents.colUpdated"), value: fmtTime(agent.updated_at) },
            {
              label: t("v2.agents.registryRecord"),
              value: agent.registry_record_id ? (
                <Link to="/registry" className="mono">
                  {agent.registry_record_id}
                </Link>
              ) : (
                "—"
              ),
            },
            { label: "ARN", value: agent.arn ? <span className="mono">{agent.arn}</span> : "—" },
            ...(hasResource
              ? [{ label: t("inboundAuth.modeLabel"), value: <Tag tone="blue">{t("inboundAuth.mode.iam")}</Tag> }]
              : []),
          ]}
        />
      </Card>
      {deployment && <DeployProgress deployment={deployment} job={jobId ? job : null} />}
      {hasResource && agent.status !== "deploying" && <VersionsCard agentId={agent.id} />}
      {byoc && (
        <Card title={t("v2.agents.byocTitle")}>
          <Descriptions
            items={[
              { label: t("v2.agents.byocKind"), value: <span className="mono">{byoc.artifact_kind ?? "—"}</span> },
              { label: t("v2.agents.byocImage"), value: byoc.image_uri ? <span className="mono">{byoc.image_uri}</span> : "—" },
              ...(byoc.telemetry?.layer_image
                ? [
                    { label: t("v2.agents.byocMemberImage"), value: <span className="mono">{byoc.telemetry.member_image || "—"}</span> },
                    { label: t("v2.agents.byocLayerImage"), value: <span className="mono">{byoc.telemetry.layer_image}</span> },
                  ]
                : []),
              ...(byoc.artifact_kind !== "code_zip" && hasResource
                ? [{ label: t("v2.agents.traceStatus.label"), value: <TraceStatusTag agentId={agent.id} /> }]
                : []),
              { label: t("v2.agents.byocEntrypoint"), value: byoc.entrypoint ?? "—" },
              {
                label: t("v2.agents.byocModels"),
                value: byoc.allowed_models?.length ? <span className="mono">{byoc.allowed_models.join(", ")}</span> : "—",
              },
            ]}
          />
        </Card>
      )}
      {source?.agent_name && (
        <Card title={t("v2.agents.convertedTitle")} sub={t("v2.agents.convertedFrom", { name: source.agent_name })}>
          {Object.keys(notes).length ? (
            <Descriptions one items={Object.entries(notes).map(([cap, note]) => ({ label: cap, value: note }))} />
          ) : (
            <span className="v2-muted">—</span>
          )}
        </Card>
      )}
      {kbs.length > 0 && (
        <Card title={t("v2.agents.kbTitle")}>
          <div className="v2-tags">
            {kbs.map((kb) => (
              <Tag key={kb.kb_id} tone="outline">
                {kb.name}
              </Tag>
            ))}
          </div>
        </Card>
      )}
      {actions.dialog}
    </>
  );
}

/** Agent management (native V2): list, `?view=detail&id=`, `?view=new` (the
 *  creation wizard for Harness / Strands / other SDK / bring-your-own-code, with
 *  the classic `method=` / `gateway=` / `skill=` prefills) and `?view=edit&id=` (the
 *  same wizard as a re-publish editor); importing and the system presets open the
 *  classic flows inside the V2 shell. */
export function V2Agents() {
  const [params] = useSearchParams();
  const id = params.get("id");
  const view = params.get("view");
  if (view === "detail" && id) return <AgentDetail key={id} id={id} />;
  if (view === "new") return <AgentWizard />;
  if (view === "edit" && id) return <AgentEdit key={id} id={id} />;
  if (view === "identity" && id) return <AgentIdentity key={id} id={id} />;
  return <AgentList />;
}
