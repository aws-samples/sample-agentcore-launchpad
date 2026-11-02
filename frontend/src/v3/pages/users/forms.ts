import type { ConsoleRole } from "../../../lib/api";
import type { ProjectUser, UserListFilter, UserMembershipPatch } from "../../../lib/api/users";
import {
  DEFAULT_GRANT,
  type MembershipRow,
  MIN_PASSWORD_LENGTH,
  projectItems,
  validateMemberships,
} from "../../../lib/userCreate";
import { isStatusFilter } from "../../../v2/pages/users/common";

// The create-account form model lives in lib/userCreate (shared with the V2 console);
// re-exported so this module stays the one import of the V3 users pages.
export {
  createBody,
  type CreateErrors,
  type CreateForm,
  EMAIL_RE,
  emptyCreateForm,
  type ExpiryChoice,
  isPlatformRole,
  type MembershipRow,
  MIN_PASSWORD_LENGTH,
  oneTimePassword,
  type PasswordChoice,
  projectItems,
  USERNAME_RE,
  validateCreate,
  validateMemberships,
} from "../../../lib/userCreate";

/**
 * Pure form logic of the Users page dialogs (approve, memberships, list filters,
 * change password), kept out of the components so it is unit-tested. The rules
 * mirror the backend's (`services/users.py`): the backend stays authoritative and
 * re-checks everything; these only catch mistakes before a round-trip. Values are
 * i18n keys.
 */

/* ── approval (projects §7.5) ────────────────────────────────────────────── */

export interface ApproveForm {
  role: "member" | "operator";
  /** legacy workspace grants: `default` joins the default project as `inherit` */
  workspaces: string[];
  projects: MembershipRow[];
}

export function emptyApproveForm(user: Pick<ProjectUser, "role" | "workspaces">): ApproveForm {
  return {
    role: (user.role as string) === "operator" ? "operator" : "member",
    workspaces: user.workspaces.length > 0 ? user.workspaces : [DEFAULT_GRANT],
    projects: [],
  };
}

export function validateApprove(form: ApproveForm): Partial<Record<"projects", string>> {
  const errors: Partial<Record<"projects", string>> = {};
  const projects = validateMemberships(form.projects, "projects");
  if (projects) errors.projects = projects;
  return errors;
}

/**
 * `PATCH /api/users/{id}` of an approval: active, the grants as before, and the
 * projects when any were chosen. `role` only when it changes, so an
 * approval without choices is the same request as before projects.
 */
export function approveBody(form: ApproveForm, currentRole: ConsoleRole | "operator"): UserMembershipPatch {
  const body: UserMembershipPatch = { status: "active", workspaces: [...new Set(form.workspaces)] };
  if (form.role !== currentRole) body.role = form.role;
  if (form.projects.length > 0) body.projects = projectItems(form.projects);
  return body;
}

/* ── memberships of one account ─────────────────────────────────────────── */

/**
 * The direct project rows an editor starts from: a project the account holds
 * directly, with that direct role. The default project's `inherit` row follows the
 * workspace grants (the backend spares it on replacement), so it is not edited here.
 */
export function directProjectRows(user: Pick<ProjectUser, "projects">): MembershipRow[] {
  const rows: MembershipRow[] = [];
  for (const project of user.projects ?? []) {
    const direct = project.sources.find((s) => s.source === "direct");
    if (!direct || (project.is_default && direct.role_key === "inherit")) continue;
    rows.push({ id: project.id, role: direct.role_key });
  }
  return rows;
}

/* ── list filters in the URL (projects §7.4) ─────────────────────────────── */

const ROLES = ["admin", "steward", "operator", "member"] as const;

/** `?status= &q= &role= &project= &page=` → the list request. */
export function listFilterFrom(params: URLSearchParams, pageSize: number): UserListFilter & { page: number } {
  const status = params.get("status");
  const role = params.get("role");
  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);
  return {
    q: params.get("q") || undefined,
    status: isStatusFilter(status) ? status : "all",
    role: (ROLES as readonly string[]).includes(role ?? "") ? (role as UserListFilter["role"]) : undefined,
    project: params.get("project") || undefined,
    limit: pageSize,
    offset: (page - 1) * pageSize,
    page,
  };
}

export interface PasswordChangeForm {
  current: string;
  next: string;
  confirm: string;
}

export type PasswordChangeErrors = Partial<Record<keyof PasswordChangeForm, string>>;

export function validatePasswordChange(form: PasswordChangeForm): PasswordChangeErrors {
  const errors: PasswordChangeErrors = {};
  if (!form.current) errors.current = "v3.account.password.err.current";
  if (form.next.length < MIN_PASSWORD_LENGTH) errors.next = "v3.users.create.err.password";
  else if (form.next === form.current) errors.next = "v3.account.password.err.same";
  if (form.confirm !== form.next) errors.confirm = "v3.account.password.err.mismatch";
  return errors;
}

/** The role a new row starts with: the template the global role suggests (§3.7), else the first offered. */
export function suggestedRole(options: { key: string }[], globalRole: ConsoleRole | "operator" = "member"): string {
  const suggested = globalRole === "operator" ? "release_ops" : "developer";
  return options.some((o) => o.key === suggested) ? suggested : (options[0]?.key ?? "");
}
