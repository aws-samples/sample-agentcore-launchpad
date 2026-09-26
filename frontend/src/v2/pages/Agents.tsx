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
  type DeploymentInfo,
  errorMessage,
  type JobInfo,
  type StageInfo,
} from "../../lib/api";
import { fmtTime } from "../format";
import { AgentWizard } from "./agents/AgentWizard";
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

type Method = AgentInfo["method"];

const METHOD_TONE: Record<Method, TagTone> = {
  harness: "orange",
  container: "blue",
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
};

const editPath = v2EditPath;

const EDITABLE_METHODS: Method[] = ["harness", "zip_runtime", "container", "byoc"];

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
 *  preset is edited by administrators only and never deleted or converted here. */
function useAgentPermissions() {
  const { can, isAdmin } = useAuth();
  const canDeploy = can("agents.deploy");
  const canDelete = can("agents.delete");
  const canConvert = can("agents.convert");
  return {
    canEdit: (a: AgentInfo) =>
      a.method !== "discovered_runtime" && canDeploy && a.status !== "deploying" && (!a.system || isAdmin),
    canDelete: (a: AgentInfo) => canDelete && !a.system,
    canConvert: (a: AgentInfo) =>
      a.method === "harness" && a.status === "active" && canConvert && !a.system,
  };
}

/** Delete / convert confirmations shared by the list and the detail page. */
function useAgentActions(onDone: (action: "deleted" | "converted", agent: AgentInfo) => void) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [pending, setPending] = useState<{ kind: "delete" | "convert"; agent: AgentInfo } | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      if (pending.kind === "delete") {
        await api.deleteAgent(pending.agent.id);
        toast("success", t("v2.agents.deleted", { name: pending.agent.name }));
        onDone("deleted", pending.agent);
      } else {
        const res = await api.convertAgent(pending.agent.id);
        toast("success", t("v2.agents.convertStarted", { name: res.agent.name }));
        onDone("converted", res.agent);
      }
      setPending(null);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const external = pending?.agent.method === "discovered_runtime";
  const dialog = (
    <Confirm
      open={pending !== null}
      title={
        pending?.kind === "convert"
          ? t("v2.agents.convertTitle")
          : external
            ? t("v2.agents.removeTitle")
            : t("v2.agents.deleteTitle")
      }
      body={t(
        pending?.kind === "convert"
          ? "v2.agents.convertBody"
          : external
            ? "v2.agents.removeBody"
            : "v2.agents.deleteBody",
        { name: pending?.agent.name ?? "" },
      )}
      confirmLabel={
        pending?.kind === "convert"
          ? t("v2.agents.convert")
          : external
            ? t("v2.agents.remove")
            : t("v2.common.delete")
      }
      danger={pending?.kind === "delete"}
      busy={busy}
      onConfirm={() => void run()}
      onClose={() => setPending(null)}
    />
  );
  return {
    askDelete: (agent: AgentInfo) => setPending({ kind: "delete", agent }),
    askConvert: (agent: AgentInfo) => setPending({ kind: "convert", agent }),
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
        <Tag tone={STATUS_TONE[a.status] ?? "gray"} dot title={a.status === "failed" ? (a.error ?? undefined) : undefined}>
          {t(`status.${a.status}`, { defaultValue: a.status })}
        </Tag>
      ),
    },
    { key: "version", title: t("v2.agents.colVersion"), render: (a) => <span className="mono">{versionLabel(a)}</span> },
    { key: "owner", title: t("v2.agents.colOwner"), render: (a) => a.owner || "—" },
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
          {perms.canEdit(a) && (
            <LinkButton onClick={() => navigate(editPath(a))} testId={`v2-agent-edit-${a.name}`}>
              {t("v2.common.edit")}
            </LinkButton>
          )}
          {perms.canConvert(a) && (
            <LinkButton onClick={() => actions.askConvert(a)}>{t("v2.agents.convert")}</LinkButton>
          )}
          {perms.canDelete(a) && (
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
          <Button onClick={() => navigate("/agents/import")}>
            <Download size={14} aria-hidden="true" />
            {t("v2.agents.import")}
          </Button>
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
            <span className="n">{s.status === "succeeded" || s.status === "skipped" ? "✓" : s.status === "failed" ? "✕" : i + 1}</span>
            <div className="b">
              <div className="t">
                {t(`create.stages.${s.name}`, { defaultValue: s.name })}
                <Tag tone={STAGE_TONE[s.status]}>{t(`v2.agents.stage.${s.status}`)}</Tag>
              </div>
              <div className="d">{s.detail || "—"}</div>
            </div>
          </div>
        ))}
      </div>
      {job?.error && <Alert tone="error">{job.error}</Alert>}
      <h3 className="v2-sub-title">{t("v2.agents.logTitle")}</h3>
      <pre className="v2-pre" data-testid="v2-agent-log">
        {job?.events.length
          ? job.events.map((e) => `${e.ts.slice(11, 19)}  ${e.stage.padEnd(9)} ${e.msg}`).join("\n")
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
              { key: "s", title: t("v2.agents.colStatus"), render: (r) => r.status ?? "—" },
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
                render: (r) => <span title={r.failure_reason ?? undefined}>{r.status ?? "—"}</span>,
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
        if (live) setError(errorMessage(err));
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

  return (
    <>
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
            {perms.canConvert(agent) && (
              <Button onClick={() => actions.askConvert(agent)}>{t("v2.agents.convert")}</Button>
            )}
            {perms.canDelete(agent) && (
              <Button kind="danger" onClick={() => actions.askDelete(agent)}>
                {agent.method === "discovered_runtime" ? t("v2.agents.remove") : t("v2.common.delete")}
              </Button>
            )}
            {perms.canEdit(agent) && (
              <Button kind="primary" onClick={() => navigate(editPath(agent))} testId="v2-agent-edit">
                {t("v2.common.edit")}
              </Button>
            )}
          </>
        }
      />
      {agent.status === "failed" && agent.error && <Alert tone="error">{agent.error}</Alert>}
      {agent.system && (
        <Alert>
          {t("v2.agents.systemNote", { label: agent.system.label, version: agent.system.skill_version ?? "—" })}
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
            { label: t("v2.agents.model"), value: spec.model_id ? <span className="mono">{String(spec.model_id)}</span> : "—" },
            {
              label: t("v2.agents.protocol"),
              value: String(spec.protocol ?? "http").toUpperCase(),
            },
            { label: t("v2.agents.colOwner"), value: agent.owner || "—" },
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
  return <AgentList />;
}
