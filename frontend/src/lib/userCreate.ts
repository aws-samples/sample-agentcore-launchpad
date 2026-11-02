import { api, ApiError, type ConsoleRole, type ConsoleUser, errorMessage } from "./api";
import { isSteward } from "./api/steward";
import { type ProjectMembershipInput, type UserCreateWithProjects, usersApi } from "./api/users";

/**
 * "Add a user" (`POST /api/users`), shared by both consoles: the form model, its
 * client-side checks, the request body, the call itself, and where a backend
 * refusal belongs on the form. Pure logic plus one API call; each console keeps
 * its own dialog (V2 kit / V3 kit) on top of it.
 *
 * The rules mirror the backend's (`services/users.py`): the backend stays
 * authoritative and re-checks everything; these only catch mistakes before a
 * round-trip. Error values are i18n keys.
 */

/** Grant every new or approved account the hub workspace unless the admin says otherwise. */
export const DEFAULT_GRANT = "default";

/** Upper bound the backend accepts for a validity in days (`valid_days`, an extension). */
export const MAX_EXTEND_DAYS = 3650;

/** Same pattern as `services/users._USERNAME_RE`. */
export const USERNAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$/;
/** Same pattern as `services/users._EMAIL_RE`. */
export const EMAIL_RE = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9]([A-Za-z0-9.-]{0,251})\.[A-Za-z]{2,24}$/;
export const MIN_PASSWORD_LENGTH = 8;

export type ExpiryChoice = "default" | "days" | "never";
export type PasswordChoice = "generate" | "set";

/** One project row of a dialog: the target id and a role key. */
export interface MembershipRow {
  id: string;
  role: string;
}

export interface CreateForm {
  username: string;
  email: string;
  role: ConsoleRole;
  passwordChoice: PasswordChoice;
  password: string;
  workspaces: string[];
  expiry: ExpiryChoice;
  days: string;
  projects: MembershipRow[];
}

export function emptyCreateForm(): CreateForm {
  return {
    username: "",
    email: "",
    role: "member",
    passwordChoice: "generate",
    password: "",
    workspaces: [DEFAULT_GRANT],
    expiry: "default",
    days: "30",
    projects: [],
  };
}

/**
 * The platform roles (`services/users.PLATFORM_ROLES`): an administrator reaches
 * every project by role, a steward (organization admin) manages structure only —
 * neither holds a workspace grant or a membership, and the backend refuses one.
 */
export function isPlatformRole(role: string | null | undefined): boolean {
  return role === "admin" || role === "steward";
}

/**
 * Who may add accounts: the backend's `access.require_structure` — an administrator
 * or a steward (organization admin).
 */
export function canCreateUsers(isAdmin: boolean, role: string | null | undefined): boolean {
  return isAdmin || isSteward(role);
}

/** The roles a creator may hand out: only an administrator creates the platform roles (§11.4). */
export function createRolesFor(isAdmin: boolean): ConsoleRole[] {
  return isAdmin ? ["member", "operator", "admin", "steward"] : ["member", "operator"];
}

export type CreateField = "username" | "email" | "password" | "days" | "projects";
export type CreateErrors = Partial<Record<CreateField, string>>;

/**
 * A membership list's error key, or null: every row names a target and a role,
 * and no target twice (the backend refuses both).
 */
export function validateMemberships(rows: MembershipRow[], kind: "projects"): string | null {
  if (rows.some((row) => !row.id || !row.role)) return `v3.users.memberships.err.${kind}Incomplete`;
  const ids = rows.map((row) => row.id);
  if (new Set(ids).size !== ids.length) return `v3.users.memberships.err.${kind}Duplicate`;
  return null;
}

export function projectItems(rows: MembershipRow[]): ProjectMembershipInput[] {
  return rows.map((row) => ({ project_id: row.id, role_key: row.role }));
}

export function validateCreate(form: CreateForm, reservedName?: string | null): CreateErrors {
  const errors: CreateErrors = {};
  const name = form.username.trim();
  if (!USERNAME_RE.test(name)) errors.username = "v3.users.create.err.username";
  else if (reservedName && name.toLowerCase() === reservedName.trim().toLowerCase()) errors.username = "v3.users.create.err.reserved";
  if (!EMAIL_RE.test(form.email.trim().toLowerCase())) errors.email = "v3.users.create.err.email";
  if (form.passwordChoice === "set" && form.password.length < MIN_PASSWORD_LENGTH) errors.password = "v3.users.create.err.password";
  if (form.expiry === "days") {
    const days = Number(form.days);
    if (!Number.isInteger(days) || days < 1 || days > MAX_EXTEND_DAYS) errors.days = "v3.users.create.err.days";
  }
  if (!isPlatformRole(form.role)) {
    const projects = validateMemberships(form.projects, "projects");
    if (projects) errors.projects = projects;
  }
  return errors;
}

/**
 * The request body for a valid form. Administrators and stewards carry no workspace
 * grant and no membership (an administrator reaches every project by role, a steward
 * none); empty membership lists are left out, so a form without projects sends
 * exactly what it did before projects.
 */
export function createBody(form: CreateForm): UserCreateWithProjects {
  const admin = isPlatformRole(form.role);
  const body: UserCreateWithProjects = {
    username: form.username.trim(),
    email: form.email.trim(),
    role: form.role,
    workspaces: admin ? [] : [...new Set(form.workspaces)],
  };
  if (form.passwordChoice === "set") body.password = form.password;
  if (form.expiry === "days") body.valid_days = Number(form.days);
  if (form.expiry === "never") body.never_expires = true;
  if (!admin && form.projects.length > 0) body.projects = projectItems(form.projects);
  return body;
}

export interface OneTimePassword {
  username: string;
  password: string;
  kind: "created" | "reset";
}

/**
 * The one-time password view: a response carries `generated_password` exactly once
 * (create / reset); the dialog shows it until closed, and nothing else keeps it.
 */
export function oneTimePassword(
  response: { username: string; generated_password?: string },
  kind: "created" | "reset",
): OneTimePassword | null {
  return response.generated_password ? { username: response.username, password: response.generated_password, kind } : null;
}

/**
 * Create the account of a valid form: the base call unless memberships ride along
 * (the same request as before projects). The created account comes back with its
 * one-time password when the platform generated one.
 */
export async function createAccount(form: CreateForm): Promise<{ user: ConsoleUser; oneTime: OneTimePassword | null }> {
  const body = createBody(form);
  const user = body.projects ? await usersApi.create(body) : await api.createUser(body);
  return { user, oneTime: oneTimePassword(user, "created") };
}

/** The backend's refusals that name one field of the form (`services/users.py`). */
const SERVER_FIELD: Record<string, CreateField> = {
  "auth.invalid_username": "username",
  "auth.username_taken": "username",
  "auth.invalid_email": "email",
  "auth.email_taken": "email",
  "auth.email_domain_blocked": "email",
  "auth.weak_password": "password",
  "users.invalid_expiry": "days",
};

/**
 * Where a failed create belongs: the field the backend refused (a taken username, a
 * blocked email domain, …) with its localized message, or `field: null` for a
 * refusal of the whole request (permissions, a project, the network).
 */
export function serverFieldError(err: unknown): { field: CreateField | null; message: string } {
  const field = err instanceof ApiError ? (SERVER_FIELD[err.code] ?? null) : null;
  return { field, message: errorMessage(err) };
}
