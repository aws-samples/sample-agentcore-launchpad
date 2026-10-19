import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { codingApi, type CodingProject, errorMessage } from "../../../lib/api";
import { creatorLabel, fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Field,
  LinkButton,
  Modal,
  PageHeader,
  Pager,
  SearchInput,
  Segmented,
  Table,
} from "../../ui";
import { RuntimeCard } from "./RuntimeCard";
import { ProjectStatusTag } from "./common";

export const NAME_MAX = 80;
export const DESCRIPTION_MAX = 500;

/** Create (no `project`) or rename / re-describe a project. */
export function ProjectFormModal({
  project,
  open,
  onClose,
  onSaved,
}: {
  project?: CodingProject | null;
  open: boolean;
  onClose: () => void;
  onSaved: (project: CodingProject) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(project?.name ?? "");
  const [description, setDescription] = useState(project?.description ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const trimmed = name.trim();
  const nameError = !trimmed ? t("v2.coding.form.nameRequired") : trimmed.length > NAME_MAX ? t("v2.coding.form.nameTooLong", { max: NAME_MAX }) : null;

  const submit = async () => {
    setTouched(true);
    if (nameError || description.length > DESCRIPTION_MAX) return;
    setBusy(true);
    setError(null);
    try {
      const saved = project
        ? await codingApi.updateProject(project.id, { name: trimmed, description: description.trim() })
        : await codingApi.createProject({ name: trimmed, description: description.trim() || undefined });
      onSaved(saved);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t(project ? "v2.coding.form.editTitle" : "v2.coding.form.createTitle")}
      onClose={onClose}
      testId="v2-coding-project-form"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy} onClick={() => void submit()} testId="v2-coding-project-save">
            {t(project ? "v2.common.save" : "v2.common.create")}
          </Button>
        </>
      }
    >
      {error && <Alert tone="error">{error}</Alert>}
      <form
        className="v2-stack"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Field label={t("v2.coding.form.name")} required error={touched ? nameError : null} hint={t("v2.coding.form.nameHint")}>
          <input
            className="v2-input"
            value={name}
            maxLength={NAME_MAX}
            placeholder={t("v2.coding.form.namePlaceholder")}
            aria-label={t("v2.coding.form.name")}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            data-testid="v2-coding-project-name"
          />
        </Field>
        <Field
          label={t("v2.coding.form.description")}
          hint={t("v2.coding.form.descriptionHint", { count: description.length, max: DESCRIPTION_MAX })}
        >
          <textarea
            className="v2-textarea"
            rows={3}
            value={description}
            maxLength={DESCRIPTION_MAX}
            placeholder={t("v2.coding.form.descriptionPlaceholder")}
            aria-label={t("v2.coding.form.description")}
            onChange={(e) => setDescription(e.target.value)}
            data-testid="v2-coding-project-description"
          />
        </Field>
      </form>
    </Modal>
  );
}

/** The landing page: the runtime card, then the member's projects. Administrators
 *  can switch to every project in the workspace (list and delete only — a project's
 *  IDE stays its creator's). */
export function ProjectList() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [, setParams] = useSearchParams();
  const { isAdmin } = useAuth();
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const all = isAdmin && scope === "all";
  const runtime = useLoad(() => codingApi.runtime(), "coding-runtime");
  const projects = useLoad(() => codingApi.listProjects(all), `coding-projects:${all}`);
  const [q, setQ] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<CodingProject | null>(null);
  const [deleting, setDeleting] = useState<CodingProject | null>(null);
  const [busy, setBusy] = useState(false);

  const rows = useMemo(() => {
    const list = projects.data?.projects ?? [];
    const needle = q.trim().toLowerCase();
    return needle ? list.filter((p) => `${p.name} ${p.description} ${p.created_by_name ?? ""}`.toLowerCase().includes(needle)) : list;
  }, [projects.data, q]);
  const paged = usePaged(rows, 12);

  // "all" lists other members' projects too: only the caller's own open in the IDE.
  // The server decides ownership (`owned`, by principal) — a display name is not an
  // identity (a config admin and a same-named user differ).
  const owned = (p: CodingProject) => p.owned ?? !all;
  const open = (p: CodingProject) => setParams({ view: "project", id: p.id });

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await codingApi.deleteProject(deleting.id);
      toast("success", t("v2.coding.projects.deleted", { name: deleting.name }));
      setDeleting(null);
      projects.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<CodingProject>[] = [
    {
      key: "name",
      title: t("v2.coding.projects.colName"),
      render: (p) => (
        <>
          {owned(p) ? (
            <LinkButton onClick={() => open(p)} testId={`v2-coding-project-${p.name}`}>
              {p.name}
            </LinkButton>
          ) : (
            <span>{p.name}</span>
          )}
          {p.description && (
            <span className="sub v2-coding-clamp" title={p.description}>
              {p.description}
            </span>
          )}
        </>
      ),
    },
    { key: "status", title: t("v2.coding.projects.colStatus"), render: (p) => <ProjectStatusTag status={p.status} /> },
    {
      key: "agent",
      title: t("v2.coding.projects.colAgent"),
      render: (p) =>
        p.agent_id ? (
          <Link to={`/v2/agents?view=detail&id=${encodeURIComponent(p.agent_id)}`} className="mono">
            {p.agent_id}
          </Link>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    ...(all ? [{ key: "creator", title: t("v2.common.creator"), render: (p: CodingProject) => creatorLabel(p) }] : []),
    { key: "updated", title: t("v2.coding.projects.colUpdated"), className: "nowrap", render: (p) => fmtTime(p.updated_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (p) => (
        <div className="v2-actions">
          {owned(p) && <LinkButton onClick={() => open(p)}>{t("v2.coding.projects.open")}</LinkButton>}
          {owned(p) && <LinkButton onClick={() => setEditing(p)}>{t("v2.coding.projects.rename")}</LinkButton>}
          <LinkButton
            danger
            disabled={p.status === "running"}
            title={p.status === "running" ? t("v2.coding.projects.runningNoDelete") : undefined}
            onClick={() => setDeleting(p)}
            testId={`v2-coding-delete-${p.name}`}
          >
            {t("v2.common.delete")}
          </LinkButton>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader title={t("v2.coding.title")} desc={t("v2.coding.desc")} />
      <RuntimeCard runtime={runtime.data} loading={runtime.loading} error={runtime.error} reload={runtime.reload} />
      <Card title={t("v2.coding.projects.title")}>
        <div className="v2-toolbar">
          <Button onClick={projects.reload}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" onClick={() => setCreating(true)} testId="v2-coding-new">
            <Plus size={14} aria-hidden="true" />
            {t("v2.coding.projects.new")}
          </Button>
          {isAdmin && (
            <Segmented
              value={scope}
              onChange={setScope}
              ariaLabel={t("v2.coding.projects.scope")}
              options={[
                { value: "mine", label: t("v2.coding.projects.mine") },
                { value: "all", label: t("v2.coding.projects.all") },
              ]}
            />
          )}
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.coding.projects.search")} testId="v2-coding-search" />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        {all && <Alert>{t("v2.coding.projects.allNote")}</Alert>}
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(p) => p.id}
          loading={projects.loading}
          error={projects.error}
          onRetry={projects.reload}
          empty={
            projects.data?.projects.length ? (
              t("v2.coding.projects.noMatch")
            ) : (
              <>
                <b>{t("v2.coding.projects.empty")}</b>
                <div className="v2-coding-empty-body">{t("v2.coding.projects.emptyBody")}</div>
              </>
            )
          }
          testId="v2-coding-projects"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      {creating && (
        <ProjectFormModal
          open
          onClose={() => setCreating(false)}
          onSaved={(p) => {
            setCreating(false);
            toast("success", t("v2.coding.projects.created", { name: p.name }));
            open(p);
          }}
        />
      )}
      {editing && (
        <ProjectFormModal
          open
          project={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            toast("success", t("v2.coding.projects.saved"));
            projects.reload();
          }}
        />
      )}
      <Confirm
        open={deleting !== null}
        title={t("v2.coding.projects.deleteTitle")}
        body={t("v2.coding.projects.deleteBody", { name: deleting?.name ?? "" })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}
