import { Copy, Plus, X } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import type { ConsoleRole } from "../../../lib/api";
import {
  createAccount,
  type CreateField,
  type CreateForm,
  createRolesFor,
  emptyCreateForm,
  isPlatformRole,
  MAX_EXTEND_DAYS,
  type MembershipRow,
  type OneTimePassword,
  serverFieldError,
  validateCreate,
} from "../../../lib/userCreate";
import { suggestedRole } from "../../../v3/pages/users/forms";
import { type RoleOption, useProjectRoleOptions, useStructure } from "../../../v3/pages/users/structure";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Field, Modal, Segmented, Select } from "../../ui";
import { GrantChip } from "./tags";

/** Translated messages per field: the client's checks, or the backend's refusal. */
export type CreateFieldMessages = Partial<Record<CreateField, string>>;

export interface CreateUserFormProps {
  form: CreateForm;
  onField: <K extends keyof CreateForm>(key: K, value: CreateForm[K]) => void;
  onSubmit: () => void;
  roles: ConsoleRole[];
  validDays: number;
  workspaces: { id: string; name: string }[];
  /** the platform has projects beyond the default one: offer memberships (projects §7.4) */
  showProjects: boolean;
  projectTargets: { id: string; name: string }[];
  rolesFor: (projectId: string) => RoleOption[];
  errors: CreateFieldMessages;
  /** a refusal of the whole request (not one field) */
  formError: string | null;
}

/** Project + role rows of the create form, V2 kit (the V3 dialog's `MembershipRows`). */
function ProjectRows({
  rows,
  onChange,
  targets,
  rolesFor,
  error,
}: {
  rows: MembershipRow[];
  onChange: (rows: MembershipRow[]) => void;
  targets: { id: string; name: string }[];
  rolesFor: (projectId: string) => RoleOption[];
  error?: string;
}) {
  const { t } = useTranslation();
  const set = (index: number, next: Partial<MembershipRow>) => onChange(rows.map((row, i) => (i === index ? { ...row, ...next } : row)));
  const taken = new Set(rows.map((row) => row.id));
  const firstFree = targets.find((target) => !taken.has(target.id));
  return (
    <Field
      full
      label={t("v3.users.memberships.projects")}
      error={error}
      hint={rows.length === 0 ? t("v3.users.memberships.projectsNone") : undefined}
    >
      <div className="v2-users-rows" data-testid="v2-users-create-projects">
        {rows.map((row, index) => (
          <div className="v2-users-row" key={index}>
            <Select
              value={row.id}
              placeholder="—"
              ariaLabel={t("v3.users.memberships.projectsTarget")}
              options={targets.map((target) => ({
                value: target.id,
                label: target.name,
                disabled: target.id !== row.id && taken.has(target.id),
              }))}
              onChange={(id) => {
                const options = rolesFor(id);
                set(index, { id, role: options.some((o) => o.key === row.role) ? row.role : suggestedRole(options) });
              }}
              testId={`v2-users-create-project-${index}`}
            />
            <Select
              value={row.role}
              placeholder="—"
              disabled={!row.id}
              ariaLabel={t("v3.users.memberships.role")}
              options={(row.id ? rolesFor(row.id) : []).map((role) => ({ value: role.key, label: role.label }))}
              onChange={(role) => set(index, { role })}
              testId={`v2-users-create-project-role-${index}`}
            />
            <Button size="sm" title={t("v3.users.memberships.remove")} onClick={() => onChange(rows.filter((_, i) => i !== index))}>
              <X size={13} aria-hidden="true" />
            </Button>
          </div>
        ))}
        <div>
          <Button
            size="sm"
            disabled={!firstFree}
            onClick={() => {
              const id = firstFree?.id ?? "";
              onChange([...rows, { id, role: suggestedRole(rolesFor(id)) }]);
            }}
            testId="v2-users-create-project-add"
          >
            <Plus size={13} aria-hidden="true" />
            {t("v3.users.memberships.projectsAdd")}
          </Button>
        </div>
      </div>
    </Field>
  );
}

/** The fields of 新增用户 — the same as the V3 dialog's, in the V2 kit. Presentational. */
export function CreateUserForm({
  form,
  onField,
  onSubmit,
  roles,
  validDays,
  workspaces,
  showProjects,
  projectTargets,
  rolesFor,
  errors,
  formError,
}: CreateUserFormProps) {
  const { t } = useTranslation();
  const platform = isPlatformRole(form.role);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    onSubmit();
  };
  return (
    <form onSubmit={submit} data-testid="v2-users-create-form" noValidate>
      <p className="v2-users-dialog-body">{t("v3.users.create.body")}</p>
      <div className="v2-form cols-2">
        <Field label={t("v2.users.field.username")} required error={errors.username} hint={t("v3.users.create.usernameHint")}>
          <input
            className="v2-input"
            name="username"
            autoComplete="off"
            value={form.username}
            onChange={(e) => onField("username", e.target.value)}
            aria-invalid={errors.username ? true : undefined}
            data-testid="v2-users-create-username"
          />
        </Field>
        <Field label={t("v2.users.field.email")} required error={errors.email} hint={t("v3.users.create.emailHint")}>
          <input
            className="v2-input"
            name="email"
            type="email"
            autoComplete="off"
            value={form.email}
            onChange={(e) => onField("email", e.target.value)}
            aria-invalid={errors.email ? true : undefined}
            data-testid="v2-users-create-email"
          />
        </Field>
        <Field label={t("v2.users.col.role")} hint={t(`v3.users.create.roleHint.${form.role}`)}>
          <Select
            value={form.role}
            ariaLabel={t("v2.users.col.role")}
            options={roles.map((role) => ({ value: role, label: t(`v2.users.role.${role}`) }))}
            onChange={(role) => onField("role", role as ConsoleRole)}
            testId="v2-users-create-role"
          />
        </Field>
        <Field label={t("v2.users.col.validity")} error={errors.days}>
          <Select
            value={form.expiry}
            ariaLabel={t("v2.users.col.validity")}
            options={[
              { value: "default", label: t("v3.users.create.expiryDefault", { count: validDays }) },
              { value: "days", label: t("v3.users.create.expiryDays") },
              { value: "never", label: t("usersPage.neverExpires") },
            ]}
            onChange={(expiry) => onField("expiry", expiry as CreateForm["expiry"])}
            testId="v2-users-create-expiry"
          />
          {form.expiry === "days" && (
            <input
              className="v2-input"
              name="days"
              type="number"
              min={1}
              max={MAX_EXTEND_DAYS}
              value={form.days}
              onChange={(e) => onField("days", e.target.value)}
              aria-label={t("v2.users.extendDays")}
              aria-invalid={errors.days ? true : undefined}
              data-testid="v2-users-create-days"
            />
          )}
        </Field>
        <Field full label={t("v3.users.create.password")} error={errors.password} hint={t("v3.users.create.passwordHint")}>
          <Segmented
            value={form.passwordChoice}
            ariaLabel={t("v3.users.create.password")}
            options={[
              { value: "generate", label: t("v3.users.create.passwordGenerate"), testId: "v2-users-create-pw-generate" },
              { value: "set", label: t("v3.users.create.passwordSet"), testId: "v2-users-create-pw-set" },
            ]}
            onChange={(choice) => onField("passwordChoice", choice)}
          />
          {form.passwordChoice === "set" && (
            <input
              className="v2-input"
              name="password"
              type="password"
              autoComplete="new-password"
              value={form.password}
              onChange={(e) => onField("password", e.target.value)}
              aria-label={t("v3.users.create.password")}
              aria-invalid={errors.password ? true : undefined}
              data-testid="v2-users-create-password-input"
            />
          )}
        </Field>
        {showProjects && !platform && (
          <ProjectRows
            rows={form.projects}
            onChange={(rows) => onField("projects", rows)}
            targets={projectTargets}
            rolesFor={rolesFor}
            error={errors.projects}
          />
        )}
        <Field
          full
          label={t("v2.users.col.workspaces")}
          hint={
            form.role === "admin"
              ? t("v2.users.allByRole")
              : form.role === "steward"
                ? t("v3.steward.typeHint")
                : showProjects
                  ? t("v3.users.memberships.legacyHint")
                  : undefined
          }
        >
          {!platform && (
            <div className="v2-users-chips" data-testid="v2-users-create-workspaces">
              {workspaces.map((ws) => {
                const on = form.workspaces.includes(ws.id);
                return (
                  <GrantChip
                    key={ws.id}
                    on={on}
                    title={ws.name}
                    testId={`v2-users-create-ws-${ws.id}`}
                    onClick={() => onField("workspaces", on ? form.workspaces.filter((id) => id !== ws.id) : [...form.workspaces, ws.id])}
                  >
                    {ws.id}
                  </GrantChip>
                );
              })}
              {workspaces.length === 0 && <span className="v2-muted">—</span>}
            </div>
          )}
        </Field>
      </div>
      {!platform && form.workspaces.length === 0 && form.projects.length === 0 && (
        <Alert tone="warn">{t("v3.users.create.noGrant")}</Alert>
      )}
      {formError && <Alert tone="error">{formError}</Alert>}
      <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
    </form>
  );
}

/**
 * The one-time password of a created account: shown once, with a copy button.
 * Only the hash is stored, so closing this is the last chance to read it.
 */
export function CreatedPassword({
  created,
  onCopy,
  onClose,
}: {
  created: OneTimePassword | null;
  onCopy: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      open={created !== null}
      title={t("v3.users.create.passwordTitle")}
      onClose={onClose}
      testId="v2-users-create-password"
      footer={
        <>
          <Button onClick={onCopy} testId="v2-users-create-copy">
            <Copy size={14} aria-hidden="true" />
            {t("v2.users.copy")}
          </Button>
          <Button kind="primary" onClick={onClose} testId="v2-users-create-done">
            {t("v2.common.close")}
          </Button>
        </>
      }
    >
      <Alert tone="warn">{t("v3.users.create.passwordBody", { username: created?.username ?? "" })}</Alert>
      <code className="v2-users-password" data-testid="v2-users-create-generated-password">
        {created?.password}
      </code>
    </Modal>
  );
}

/**
 * 新增用户 (`POST /api/users`): the form, the call, field-level refusals, then the
 * one-time password when the platform generated one. `onCreated` runs as soon as
 * the account exists (the list refreshes behind the password dialog).
 */
export function CreateUserDialog({
  open,
  onClose,
  onCreated,
  validDays,
  reservedName,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
  validDays: number;
  /** the built-in administrator's name, which no account may take */
  reservedName: string | null;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const { workspaces } = useWorkspace();
  const [form, setForm] = useState<CreateForm>(emptyCreateForm);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [server, setServer] = useState<{ field: CreateField | null; message: string } | null>(null);
  const [created, setCreated] = useState<OneTimePassword | null>(null);
  const structure = useStructure(open);
  const rolesFor = useProjectRoleOptions(form.projects.map((row) => row.id), structure.templates);

  const reset = () => {
    setForm(emptyCreateForm());
    setTouched(false);
    setServer(null);
  };
  const close = () => {
    if (busy) return;
    reset();
    onClose();
  };
  const onField = <K extends keyof CreateForm>(key: K, value: CreateForm[K]) => {
    setServer(null);
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const clientErrors = touched ? validateCreate(form, reservedName) : {};
  const errors: CreateFieldMessages = {};
  for (const [field, key] of Object.entries(clientErrors) as [CreateField, string][]) {
    errors[field] = t(key, { max: MAX_EXTEND_DAYS });
  }
  if (server?.field) errors[server.field] = server.message;

  const submit = async () => {
    if (busy) return;
    setTouched(true);
    if (Object.keys(validateCreate(form, reservedName)).length > 0) return;
    setBusy(true);
    setServer(null);
    try {
      const { user, oneTime } = await createAccount(form);
      toast("success", t("v3.users.create.created", { username: user.username }));
      reset();
      onClose();
      setCreated(oneTime);
      onCreated();
    } catch (err) {
      // the dialog keeps what was typed, so a taken name is a quick fix
      setServer(serverFieldError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Modal
        open={open}
        wide
        tall
        title={t("v3.users.create.title")}
        onClose={close}
        testId="v2-users-create"
        footer={
          <>
            <Button disabled={busy} onClick={close}>
              {t("v2.common.cancel")}
            </Button>
            <Button kind="primary" disabled={busy} onClick={() => void submit()} testId="v2-users-create-submit">
              {t("v3.users.create.submit")}
            </Button>
          </>
        }
      >
        <CreateUserForm
          form={form}
          onField={onField}
          onSubmit={() => void submit()}
          roles={createRolesFor(isAdmin)}
          validDays={validDays}
          workspaces={workspaces.map((ws) => ({ id: ws.id, name: ws.name }))}
          showProjects={structure.multiProject}
          projectTargets={structure.projects.map((p) => ({ id: p.id, name: p.name }))}
          rolesFor={rolesFor}
          errors={errors}
          formError={server && !server.field ? server.message : null}
        />
      </Modal>
      <CreatedPassword
        created={created}
        onCopy={() => {
          if (!created) return;
          void navigator.clipboard?.writeText(created.password);
          toast("success", t("usersPage.copied"));
        }}
        onClose={() => setCreated(null)}
      />
    </>
  );
}
