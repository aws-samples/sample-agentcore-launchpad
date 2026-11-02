import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../../auth/auth-context";
import i18n from "../../../i18n";
import { api, ApiError, type ConsoleUser } from "../../../lib/api";
import { usersApi } from "../../../lib/api/users";
import {
  canCreateUsers,
  createAccount,
  type CreateForm,
  createRolesFor,
  emptyCreateForm,
  serverFieldError,
} from "../../../lib/userCreate";
import { V2Users } from "../Users";
import { CreatedPassword, CreateUserForm, type CreateUserFormProps } from "./CreateUser";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.restoreAllMocks();
});

const auth = (over: Partial<AuthContextValue>): AuthContextValue => ({
  authRequired: true,
  username: "someone",
  role: "member",
  email: null,
  accountExpiresAt: null,
  builtin: false,
  accountSource: "local",
  isAdmin: false,
  can: () => true,
  logout: async () => undefined,
  ...over,
});

const usersPage = (value: AuthContextValue) =>
  renderToStaticMarkup(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={["/v2/users"]}>
        <V2Users />
      </MemoryRouter>
    </AuthContext.Provider>,
  );

const form = (over: Partial<CreateForm> = {}): CreateForm => ({
  ...emptyCreateForm(),
  username: " new.user ",
  email: "new.user@example.com",
  ...over,
});

const created = (over: Partial<ConsoleUser> = {}) => ({ id: "u-new", username: "new.user", ...over }) as ConsoleUser;

describe("V2 用户管理 — 新增用户 button", () => {
  it("is in the page head for an administrator", () => {
    const html = usersPage(auth({ isAdmin: true, role: "admin" }));
    expect(html).toContain('data-testid="v2-users-create-open"');
    expect(html).toContain("Add user");
  });

  it("is not offered to a member (who sees the admin-only notice instead)", () => {
    const html = usersPage(auth({ isAdmin: false, role: "member" }));
    expect(html).not.toContain('data-testid="v2-users-create-open"');
    expect(html).toContain('data-testid="users-forbidden-body"');
  });

  it("follows the backend's require_structure: administrators and stewards", () => {
    expect(canCreateUsers(true, "admin")).toBe(true);
    expect(canCreateUsers(false, "steward")).toBe(true);
    expect(canCreateUsers(false, "member")).toBe(false);
    expect(canCreateUsers(false, "operator")).toBe(false);
    expect(canCreateUsers(false, null)).toBe(false);
  });

  it("only an administrator hands out the platform roles", () => {
    expect(createRolesFor(true)).toEqual(["member", "operator", "admin", "steward"]);
    expect(createRolesFor(false)).toEqual(["member", "operator"]);
  });
});

describe("V2 用户管理 — submit body", () => {
  it("a generated password is left to the backend; a custom validity sends valid_days", async () => {
    const createUser = vi.spyOn(api, "createUser").mockResolvedValue(created({ generated_password: "Gen-pass-123" }));
    const withProjects = vi.spyOn(usersApi, "create");
    const result = await createAccount(form({ expiry: "days", days: "30" }));
    expect(withProjects).not.toHaveBeenCalled();
    expect(createUser).toHaveBeenCalledWith({
      username: "new.user",
      email: "new.user@example.com",
      role: "member",
      workspaces: ["default"],
      valid_days: 30,
    });
    expect(result.oneTime).toEqual({ username: "new.user", password: "Gen-pass-123", kind: "created" });
  });

  it("a set password and never-expiring validity ride in the body; no one-time password comes back", async () => {
    const createUser = vi.spyOn(api, "createUser").mockResolvedValue(created());
    const result = await createAccount(form({ passwordChoice: "set", password: "s3cret-pass", expiry: "never" }));
    expect(createUser.mock.calls[0][0]).toMatchObject({ password: "s3cret-pass", never_expires: true });
    expect(createUser.mock.calls[0][0]).not.toHaveProperty("valid_days");
    expect(result.oneTime).toBeNull();
  });

  it("an administrator carries no workspace grant", async () => {
    const createUser = vi.spyOn(api, "createUser").mockResolvedValue(created({ role: "admin" }));
    await createAccount(form({ role: "admin", workspaces: ["default", "spoke"] }));
    expect(createUser.mock.calls[0][0]).toMatchObject({ role: "admin", workspaces: [] });
  });

  it("project memberships go through the projects-aware create", async () => {
    const createUser = vi.spyOn(api, "createUser");
    const withProjects = vi.spyOn(usersApi, "create").mockResolvedValue(created() as never);
    await createAccount(form({ projects: [{ id: "risk", role: "developer" }] }));
    expect(createUser).not.toHaveBeenCalled();
    expect(withProjects.mock.calls[0][0]).toMatchObject({ projects: [{ project_id: "risk", role_key: "developer" }] });
  });
});

describe("V2 用户管理 — backend refusals land on their field", () => {
  it.each([
    ["auth.username_taken", "username"],
    ["auth.invalid_username", "username"],
    ["auth.email_taken", "email"],
    ["auth.email_domain_blocked", "email"],
    ["auth.weak_password", "password"],
    ["users.invalid_expiry", "days"],
  ])("%s → %s", (code, field) => {
    expect(serverFieldError(new ApiError(code, "localized copy", null))).toEqual({ field, message: "localized copy" });
  });

  it("a refusal of the whole request stays on the form", () => {
    expect(serverFieldError(new ApiError("auth.forbidden", "Not allowed", null))).toEqual({ field: null, message: "Not allowed" });
    expect(serverFieldError(new TypeError("Failed to fetch")).field).toBeNull();
  });

  const formProps = (over: Partial<CreateUserFormProps> = {}): CreateUserFormProps => ({
    form: form(),
    onField: () => undefined,
    onSubmit: () => undefined,
    roles: ["member", "operator"],
    validDays: 7,
    workspaces: [{ id: "default", name: "Hub" }],
    showProjects: false,
    projectTargets: [],
    rolesFor: () => [],
    errors: {},
    formError: null,
    ...over,
  });

  it("renders the field's message in place of its hint, and a request-level one at the bottom", () => {
    const html = renderToStaticMarkup(
      <CreateUserForm {...formProps({ errors: { email: "This email is already registered." }, formError: "Not allowed" })} />,
    );
    const email = html.indexOf('data-testid="v2-users-create-email"');
    const err = html.indexOf('<span class="err">This email is already registered.</span>');
    expect(email).toBeGreaterThan(-1);
    expect(err).toBeGreaterThan(email);
    expect(html).not.toContain("Same rules as self-registration");
    expect(html).toContain('aria-invalid="true"');
    expect(html).toContain("Not allowed");
  });

  it("offers the V3 fields: username, email, role, validity, password, workspaces — and projects when the platform has them", () => {
    const plain = renderToStaticMarkup(<CreateUserForm {...formProps()} />);
    for (const id of ["username", "email", "role", "expiry", "pw-generate", "pw-set", "workspaces"]) {
      expect(plain).toContain(`data-testid="v2-users-create-${id}"`);
    }
    expect(plain).toContain("Default (7 days)");
    expect(plain).not.toContain('data-testid="v2-users-create-projects"');
    const multi = renderToStaticMarkup(
      <CreateUserForm {...formProps({ showProjects: true, projectTargets: [{ id: "risk", name: "Risk" }] })} />,
    );
    expect(multi).toContain('data-testid="v2-users-create-projects"');
    // a platform role holds no grant or membership
    const admin = renderToStaticMarkup(<CreateUserForm {...formProps({ showProjects: true, form: form({ role: "admin" }) })} />);
    expect(admin).not.toContain('data-testid="v2-users-create-projects"');
    expect(admin).not.toContain('data-testid="v2-users-create-workspaces"');
    expect(admin).toContain("All (administrator)");
  });
});

describe("V2 用户管理 — the generated password", () => {
  it("is shown once, with a copy button and the shown-only-now warning", () => {
    const html = renderToStaticMarkup(
      <CreatedPassword
        created={{ username: "new.user", password: "Gen-pass-123", kind: "created" }}
        onCopy={() => undefined}
        onClose={() => undefined}
      />,
    );
    expect(html).toContain('data-testid="v2-users-create-password"');
    expect(html).toContain('<code class="v2-users-password" data-testid="v2-users-create-generated-password">Gen-pass-123</code>');
    expect(html).toContain('data-testid="v2-users-create-copy"');
    expect(html).toContain("It is shown only now");
    expect(html).toContain("new.user");
  });

  it("renders nothing without one", () => {
    expect(renderToStaticMarkup(<CreatedPassword created={null} onCopy={() => undefined} onClose={() => undefined} />)).toBe("");
  });
});
