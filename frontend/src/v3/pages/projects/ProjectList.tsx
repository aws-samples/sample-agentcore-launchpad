import { FolderPlus, Lock, Plus, RefreshCw, Search, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { api, type ConsoleUser, errorMessage } from "../../../lib/api";
import { type ProjectListFilter, type ProjectStatus, projectsApi, type ProjectSummary } from "../../../lib/api/projects";
import { useProject } from "../../../workspace/project-context";
import { useLoad, useToast } from "../../hooks";
import { firstReason } from "../../reasons";
import { Btn, Chip, Dialog, Empty, Lamp, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { AgentProjectSettingsPanel } from "./AgentProjectSettingsPanel";
import { isProjectId, slugFromName } from "./model";
import { useStructureNames } from "./useStructureNames";
import { Select } from "../../select";

function StatusChip({ status }: { status: ProjectStatus }) {
  const { t } = useTranslation();
  return <Chip s={status === "active" ? "ok" : undefined}>{t(`v3.projects.status.${status}`)}</Chip>;
}

export function ProjectList() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  // a department administrator (§11.1) creates projects in their departments
  const { managedDepartments } = useProject();
  const canCreate = isAdmin || managedDepartments.length > 0;
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState<ProjectListFilter>({ status: "active" });
  const [q, setQ] = useState("");
  const [creating, setCreating] = useState(false);
  const [tick, setTick] = useState(0);
  const names = useStructureNames();
  const query = { ...filter, q: q.trim() || undefined };
  const list = useLoad(() => projectsApi.list(query), `v3-projects:${JSON.stringify(query)}:${tick}`);
  const rows = list.data?.projects ?? [];
  const bindTarget = isAdmin ? params.get("bind") : null;
  const set = (patch: ProjectListFilter) => setFilter((prev) => ({ ...prev, ...patch }));
  const open = (id: string) => setParams({ id });
  const reload = () => setTick((n) => n + 1);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.projects.eyebrow")}
        title={t("v3.projects.title")}
        sub={t(isAdmin ? "v3.projects.sub" : "v3.projects.subMember")}
        end={
          <>
            <Btn kind="ghost" onClick={reload} title={t("v3.projects.reload")}><RefreshCw size={14} /></Btn>
            {canCreate && <Btn kind="primary" onClick={() => setCreating(true)}><Plus size={14} /> {t("v3.projects.new")}</Btn>}
          </>
        }
      />

      <div className="v3-prj-filters" data-testid="projects-filters">
        <Select className="v3-input" aria-label={t("v3.projects.filter.department")} value={filter.department_id ?? ""}
          onChange={(e) => set({ department_id: e.target.value || undefined })}>
          <option value="">{t("v3.projects.filter.department")}: {t("v3.projects.filter.all")}</option>
          {names.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </Select>
        <Select className="v3-input" aria-label={t("v3.projects.filter.status")} value={filter.status ?? ""}
          onChange={(e) => set({ status: (e.target.value || undefined) as ProjectStatus | undefined })}>
          <option value="">{t("v3.projects.filter.status")}: {t("v3.projects.filter.all")}</option>
          <option value="active">{t("v3.projects.status.active")}</option>
          <option value="archived">{t("v3.projects.status.archived")}</option>
        </Select>
        <Select className="v3-input" aria-label={t("v3.projects.filter.workspace")} value={filter.workspace_id ?? ""}
          onChange={(e) => set({ workspace_id: e.target.value || undefined })}>
          <option value="">{t("v3.projects.filter.workspace")}: {t("v3.projects.filter.all")}</option>
          {names.workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
        </Select>
        <div className="v3-prj-search">
          <Search size={14} aria-hidden="true" />
          <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.projects.search")} aria-label={t("v3.projects.search")} />
        </div>
      </div>

      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v3.projects.empty")} />
        ) : (
          <table className="v3-table" data-testid="projects-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.projects.col.name")}</th>
                <th>{t("v3.projects.col.department")}</th>
                <th>{t("v3.projects.col.workspaces")}</th>
                <th className="num">{t("v3.projects.col.members")}</th>
                <th className="num">{t("v3.projects.col.agents")}</th>
                <th>{t("v3.projects.col.status")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.id} className="click" onClick={() => open(p.id)}>
                  <td style={{ width: 30 }}><Lamp s={p.status === "active" ? "ok" : "off"} /></td>
                  <td>
                    <div className="v3-name">
                      <div>
                        <b>{p.name}{p.is_default && <> <Lock size={12} aria-label={t("v3.projects.defaultChip")} /></>}</b>
                        <small>{p.id}</small>
                      </div>
                    </div>
                  </td>
                  <td>{names.department(p.department_id) ?? <span className="v3-prj-muted">—</span>}</td>
                  <td className="mono">{p.workspaces.map((w) => w.name).join(", ") || "—"}</td>
                  <td className="num">{p.member_count}</td>
                  <td className="num">{p.agent_count}</td>
                  <td><StatusChip status={p.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {creating && (
        <CreateProjectDialog
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            open(id);
          }}
        />
      )}
      {bindTarget && (
        <BindProjectDialog
          workspaceId={bindTarget}
          onClose={() => setParams({})}
          onDone={() => {
            setParams({});
            reload();
          }}
        />
      )}
      {isAdmin && <AgentProjectSettingsPanel />}
    </div>
  );
}

/* ── create ──────────────────────────────────────────────────────────────── */

function CreateProjectDialog({
  onClose,
  onCreated,
  initialWorkspace,
  makeDefault = false,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
  /** preselected (the §6.2 banner's workspace) */
  initialWorkspace?: string;
  /** …and make the new project that workspace's default */
  makeDefault?: boolean;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const { refresh, managedDepartments } = useProject();
  const names = useStructureNames();
  const [id, setId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // every project belongs to a department (required); a department administrator picks
  // one of their own, then its open workspaces
  const [departmentId, setDepartmentId] = useState(isAdmin ? "" : (managedDepartments[0]?.id ?? ""));
  const departments = isAdmin ? names.departments : names.departments.filter((d) => managedDepartments.some((m) => m.id === d.id));
  const openWs = managedDepartments.find((d) => d.id === departmentId)?.workspaces ?? [];
  const wsChoices = isAdmin
    ? names.workspaces
    : openWs.map((id) => names.workspaces.find((w) => w.id === id) ?? { id, name: id, region: "", tier: undefined });
  const [workspaces, setWorkspaces] = useState<string[]>(initialWorkspace ? [initialWorkspace] : []);
  const [owners, setOwners] = useState<Pick<ConsoleUser, "id" | "username">[]>([]);
  const [ownerQ, setOwnerQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const users = useLoad(
    () => (isAdmin && ownerQ.trim() ? api.listUsers({ q: ownerQ.trim(), status: "active", limit: 8 }) : Promise.resolve(null)),
    `v3-projects:owners:${ownerQ.trim()}`,
  );
  const suggestions = (users.data?.items ?? []).filter((u) => u.role !== "admin" && !owners.some((o) => o.id === u.id));
  const idValid = isProjectId(id);
  const chosen = workspaces.filter((ws) => isAdmin || openWs.includes(ws));
  const valid = idValid && name.trim() !== "" && chosen.length > 0 && departmentId !== "";

  const onName = (value: string) => {
    setName(value);
    if (!idTouched) setId(slugFromName(value));
  };
  const toggleWs = (ws: string) =>
    setWorkspaces((prev) => (prev.includes(ws) ? prev.filter((x) => x !== ws) : [...prev, ws]));

  const submit = async () => {
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      const row = await projectsApi.create({
        id, name: name.trim(), description, department_id: departmentId,
        workspaces: chosen, owners: owners.map((o) => o.id),
      });
      if (isAdmin && makeDefault && initialWorkspace && chosen.includes(initialWorkspace)) {
        await projectsApi.putWorkspaces(row.id, { make_default: [initialWorkspace] });
      }
      toast("ok", t("v3.projects.create.done", { name: row.name }));
      await refresh();
      onCreated(row.id);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      wide
      title={t("v3.projects.create.title")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.projects.cancel")}</Btn>
          <Btn kind="primary" onClick={() => void submit()} busy={busy} disabled={!valid}
            disabledReason={firstReason(
              !idValid && t("story.reason.fixProjectId"),
              !name.trim() && t("story.reason.fillName"),
              chosen.length === 0 && t("story.reason.pickWorkspace"),
              t("story.reason.pickDepartment"),
            )}>
            {t("v3.projects.create.submit")}
          </Btn>
        </>
      }
    >
      <div className="v3-prj-form" data-testid="project-create">
        <label className="v3-field">
          <span>{t("v3.projects.create.name")}*</span>
          <input className="v3-input" value={name} onChange={(e) => onName(e.target.value)} maxLength={64} autoFocus />
        </label>
        <label className="v3-field">
          <span>{t("v3.projects.create.id")}*</span>
          <input className="v3-input mono" value={id} maxLength={32} aria-invalid={id !== "" && !idValid}
            onChange={(e) => {
              setIdTouched(true);
              setId(e.target.value.toLowerCase());
            }} />
          <small className={id !== "" && !idValid ? "v3-err" : "v3-hint"}>{t("v3.projects.create.idHint")}</small>
        </label>
        <label className="v3-field">
          <span>{t("v3.projects.create.department")}*</span>
          <Select className="v3-input" value={departmentId} onChange={(e) => setDepartmentId(e.target.value)}>
            {isAdmin && <option value="">{t("v3.projects.create.pickDepartment")}</option>}
            {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            {!isAdmin && departmentId && !departments.some((d) => d.id === departmentId) && <option value={departmentId}>{departmentId}</option>}
          </Select>
        </label>
        <label className="v3-field">
          <span>{t("v3.projects.create.description")}</span>
          <textarea className="v3-input" value={description} maxLength={2000} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <div className="v3-field">
          <span>{t("v3.projects.create.workspaces")}*</span>
          <div className="v3-prj-checks">
            {wsChoices.map((w) => (
              <label key={w.id}>
                <input type="checkbox" checked={workspaces.includes(w.id)} onChange={() => toggleWs(w.id)} />
                {w.name}{w.region && <> · <span className="mono">{w.region}</span></>}
                {w.tier === "prod" && <Chip s="act">prod</Chip>}
              </label>
            ))}
          </div>
          {!isAdmin && wsChoices.length === 0 && <small className="v3-hint">{t("v3.projects.create.noOpenWorkspace")}</small>}
          {chosen.length === 0 && <small className="v3-hint">{t("v3.projects.create.workspacesRequired")}</small>}
        </div>
        {isAdmin && <div className="v3-field">
          <span>{t("v3.projects.create.owners")}</span>
          {owners.length > 0 && (
            <span className="v3-prj-chips">
              {owners.map((o) => (
                <Chip key={o.id}>
                  {o.username}{" "}
                  <button type="button" className="v3-btn ghost sm" aria-label={t("v3.projects.remove")}
                    onClick={() => setOwners((prev) => prev.filter((x) => x.id !== o.id))}><X size={11} /></button>
                </Chip>
              ))}
            </span>
          )}
          <input className="v3-input" value={ownerQ} onChange={(e) => setOwnerQ(e.target.value)}
            placeholder={t("v3.projects.create.ownersSearch")} aria-label={t("v3.projects.create.ownersSearch")} />
          {suggestions.length > 0 && (
            <div className="v3-prj-cands">
              {suggestions.map((u) => (
                <button key={u.id} type="button" onClick={() => {
                  setOwners((prev) => [...prev, { id: u.id, username: u.username }]);
                  setOwnerQ("");
                }}>
                  <b>{u.username}</b><span className="v3-prj-muted mono">{u.email}</span>
                </button>
              ))}
            </div>
          )}
        </div>}
        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      </div>
    </Dialog>
  );
}

/* ── bind a project to a workspace (the §6.2 banner's link) ──────────────── */

function BindProjectDialog({
  workspaceId,
  onClose,
  onDone,
}: {
  workspaceId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { refresh } = useProject();
  const [, setParams] = useSearchParams();
  const all = useLoad(() => projectsApi.list({ status: "active" }), `v3-projects:bind:${workspaceId}`);
  const workspaces = useLoad(() => api.listWorkspaces(), "v3-projects:workspaces");
  const ws = workspaces.data?.workspaces.find((w) => w.id === workspaceId);
  const [makeDefault, setMakeDefault] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [creating, setCreating] = useState(false);
  const candidates: ProjectSummary[] = (all.data?.projects ?? []).filter(
    (p) => !p.workspaces.some((b) => b.workspace_id === workspaceId),
  );
  // nothing else is bound there yet: the first project becomes what header-less requests mean
  const hasBound = (all.data?.projects ?? []).some((p) => p.workspaces.some((b) => b.workspace_id === workspaceId));
  useEffect(() => {
    if (hasBound) setMakeDefault(false);
  }, [hasBound]);

  const bind = async (p: ProjectSummary) => {
    setBusy(p.id);
    setError(null);
    try {
      await projectsApi.putWorkspaces(p.id, { bind: [workspaceId], make_default: makeDefault ? [workspaceId] : [] });
      toast("ok", t("v3.projects.bind.done", { name: p.name, workspace: ws?.name ?? workspaceId }));
      await refresh();
      onDone();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };

  if (creating) {
    return (
      <CreateProjectDialog
        initialWorkspace={workspaceId}
        makeDefault={makeDefault}
        onClose={() => setCreating(false)}
        onCreated={(id) => setParams({ id })}
      />
    );
  }
  return (
    <Dialog
      wide
      title={t("v3.projects.bind.title", { workspace: ws?.name ?? workspaceId })}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy !== null}>{t("v3.projects.cancel")}</Btn>
          <Btn kind="primary" onClick={() => setCreating(true)} disabled={busy !== null}>
            <FolderPlus size={14} /> {t("v3.projects.bind.createNew")}
          </Btn>
        </>
      }
    >
      <div className="v3-prj-form" data-testid="project-bind">
        <p style={{ margin: 0 }}>{t("v3.projects.bind.body")}</p>
        <label className="v3-prj-checks">
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="checkbox" checked={makeDefault} onChange={(e) => setMakeDefault(e.target.checked)} />
            {t("v3.projects.bind.makeDefault")}
          </span>
        </label>
        {all.loading && !all.data ? (
          <Skeleton rows={3} />
        ) : all.error ? (
          <Notice s="act"><ApiErrorDetail error={all.failure ?? all.error} /></Notice>
        ) : candidates.length === 0 ? (
          <Empty title={t("v3.projects.bind.none")} />
        ) : (
          <table className="v3-table">
            <tbody>
              {candidates.map((p) => (
                <tr key={p.id}>
                  <td><div className="v3-name"><div><b>{p.name}</b><small>{p.id}</small></div></div></td>
                  <td className="mono v3-prj-muted">{p.workspaces.map((w) => w.name).join(", ")}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <Btn size="sm" onClick={() => void bind(p)} disabled={busy !== null}>{t("v3.projects.bind.bindHere")}</Btn>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
        {workspaces.error && <Notice s="wait">{errorMessage(workspaces.failure)}</Notice>}
      </div>
    </Dialog>
  );
}
