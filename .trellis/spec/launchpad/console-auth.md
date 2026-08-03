# Console Authentication and Accounts

## 1. Scope / Trigger

Use this contract when changing Launchpad console authentication, console
accounts (registration, validity, roles, the admin User Management module), the
`/api` route boundary, **per-route authorization (`ROUTE_POLICY`)**, local
credential settings, the **local code-execution gate**, transport security
(`Secure` cookie / HSTS), or frontend handling of expired sessions. This gate is
deliberately independent from Cognito demo users and the public `/v1` API-key
surface.

**Adding any `/api` route puts you in scope**: an unclassified route is refused at
runtime (§3.4c).

## 2. Signatures

Backend:

```text
GET    /api/auth/status
POST   /api/auth/login     {"username": string, "password": string}
POST   /api/auth/register  {"username": string, "email": string, "password": string}   → 201
POST   /api/auth/logout

GET    /api/users?q=&status=all|active|expired|disabled&limit=&offset=    (admin)
GET    /api/users/stats                                                  (admin)
PATCH  /api/users/{id}  {status?, role?, extend_days?, expires_at?, password?}  (admin)
DELETE /api/users/{id}                                                   (admin)
```

Frontend:

```ts
type ConsoleRole = "admin" | "member";

interface AuthStatus {
  auth_required: boolean;
  authenticated: boolean;
  registration_enabled: boolean;
  username: string | null;
  role: ConsoleRole | null;
  email: string | null;
  account_expires_at: string | null;   // ISO, account validity (not the cookie)
}
```

The session cookie is named `launchpad_session`; the base lifetime is 12 hours,
clamped down to the account's `expires_at`.

## 3. Contracts

### 3.1 Configuration

```text
run_mode / LAUNCHPAD_RUN_MODE                               default: "dev" ("dev"|"prod")
auth_username / LAUNCHPAD_AUTH_USERNAME                     default: "admin"
auth_password / LAUNCHPAD_AUTH_PASSWORD                     default: unset
auth_cookie_secure / LAUNCHPAD_AUTH_COOKIE_SECURE           default: false
allow_open_console / LAUNCHPAD_ALLOW_OPEN_CONSOLE           default: false
auth_registration_enabled / …_AUTH_REGISTRATION_ENABLED     default: true
auth_registration_require_approval / …_AUTH_REGISTRATION_REQUIRE_APPROVAL  default: true
auth_registration_valid_days / …_AUTH_REGISTRATION_VALID_DAYS  default: 7
auth_allowed_email_domains / …_AUTH_ALLOWED_EMAIL_DOMAINS   default: [] (allow list wins when set)
auth_blocked_email_domains / …_AUTH_BLOCKED_EMAIL_DOMAINS   default: free/disposable mail list
```

An unset or empty password disables the login gate: the console stays open **to
loopback callers**, `/api/users*` is reachable as the implicit local admin, and
registration is refused with `auth.registration_disabled`. Non-loopback callers
are refused — see §3.4.

`run_mode` is the single production signal. `start.py` has always exported
`LAUNCHPAD_RUN_MODE` into its children; do **not** introduce a second mode
setting. It drives the effective cookie/HSTS posture (§3.4) and whether local code
execution is served at all (§3.7).

### 3.2 Two credential sources, one cookie

- The **built-in admin is config-only** (`auth_username`/`auth_password`). It
  must never be written to the `users` table, must not be disableable or
  deletable from the console, and its username is reserved against registration.
- **Registered accounts** are `users` rows: `username`/`username_key` (lowercase
  mirror carrying uniqueness), lowercased unique `email`, `password_hash`,
  `role`, `status` (`pending` | `active` | `disabled`), `expires_at`,
  `last_login_at`, `login_count`, `created_by`.
- **Approval is the default.** Self-registration creates `status=pending` with
  `expires_at=null`: the account cannot sign in (`401 auth.account_pending`) and
  cannot hold a session. Approval is `PATCH /api/users/{id} {"status":"active"}`,
  which — when the row has no window yet and the same patch does not set one —
  grants `now + auth_registration_valid_days`. The validity clock therefore
  starts at approval, never at registration. `auth_registration_require_approval=false`
  restores instant activation (window starts at registration). An admin-created
  account (`created_by != "self"`) is never forced through the queue.
- Passwords use `hashlib.pbkdf2_hmac` with a per-user salt, stored as
  `pbkdf2_sha256$<iters>$<salt_b64>$<dk_b64>`. Do not add passlib/bcrypt, and do
  not use `pydantic.EmailStr` (`email-validator` is not a dependency).

### 3.3 Session cookie

```text
payload = urlsafe_b64("1:{subject}:{expiry_epoch}")     # base64 so ':'/'.' cannot desync
cookie  = payload + "." + hmac_sha256(signing_key, payload).hexdigest()
signing_key = sha256(f"agentcore-launchpad-session:{auth_username}:{auth_password}")
```

The **role must not be in the cookie**. Authorization is resolved per request:
the configured admin subject short-circuits to `admin` with no DB access;
every other subject is resolved from its `users` row, which is authoritative for
role *and* liveness. A request is rejected (`401 auth.required`) when the cookie
is missing/tampered/expired **or** the account is unknown, disabled, or expired.

Rotating `LAUNCHPAD_AUTH_PASSWORD` invalidates all sessions (the signing key
derives from it) — that is intended, not a bug.

### 3.4 Route boundary

Three checks, in this order. They answer different questions; do not merge them.

**(a) May the console be open at all?** With the gate disabled, a request to a
guarded `/api` path from a **non-loopback transport peer** is refused with
`403 auth.open_console_refused` unless `allow_open_console` is set.

- This must stay a **per-request** check. `create_app()` cannot see uvicorn's
  `--host`, so a startup-only check is bypassed by running uvicorn directly —
  which is how the EC2 host and any container start it.
- Use `request.client.host` and **never** `X-Forwarded-For`: a spoofable header
  would make the check decorative. The accepted consequence is that a same-host
  reverse proxy is trusted. This branch never runs in real production, where the
  gate is enabled.
- A non-IP peer (e.g. a test transport) counts as non-loopback, i.e. fails closed.
  `TestClient`'s peer is the literal `"testclient"`, so `backend/tests/conftest.py`
  sets `LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` for the suite. **Do not special-case
  that string in production code.**
- `create_app()` additionally refuses to build when `run_mode=prod` and the gate
  is off, and `start.py` pre-flights the effective bind host. Both are fast
  failure, not the boundary.

**(b) Is there a live session?** When the gate is enabled, middleware requires one
on all `/api/*` paths except `/api/health`, `/api/auth/status`,
`/api/auth/login`, `/api/auth/register`, and CORS `OPTIONS`. The middleware never
guards `/v1/*`; those routes continue to require `X-Api-Key`.

**(c) Does this caller's role allow this route?** Every `/api` route is classified
in `ROUTE_POLICY` (`backend/app/core/route_policy.py`), enforced by one app-level
dependency registered as `FastAPI(dependencies=[Depends(enforce_route_policy)])`.

- A **dependency, not middleware**: `scope["route"]` is only populated after the
  router matches, so the check reads the exact `path_format` rather than
  re-implementing path matching. This holds under FastAPI 0.139's
  `_IncludedRouter` wrapping — which also means any code enumerating routes must
  **recurse** through `route.original_router.routes`, since `app.routes` is not
  flattened.
- **Default-deny**: an unclassified `/api` route raises
  `500 auth.route_unclassified`. Adding a route therefore requires adding an
  entry; `tests/test_route_policy.py` fails on drift in either direction.
- Classification principle: `ADMIN` for routes that execute code, change deployed
  or cloud state, mint credentials, or change governance posture; `MEMBER` for
  reads and for a member's own interaction with an agent; `PUBLIC` for the four
  open paths above (which must agree with `_OPEN_API_PATHS`).
- Invoking an agent (`/api/agents/{id}/invoke`, `/api/registry/a2a-demo`) is
  **MEMBER on purpose** — the same capability Chat gives every member.
- **Never add a setting that disables this table.** A flag that turns
  authorization off is the vulnerability; fix a misclassification by editing the
  entry.
- `require_admin` remains the enforcement primitive, so the 403 envelope
  (`auth.forbidden`) is unchanged and the frontend keeps working.

A successful login sets an HMAC-signed HttpOnly, SameSite=Lax, Path=/ cookie.
`Secure` comes from `cookie_secure(settings)` = `auth_cookie_secure or run_mode ==
"prod"`, and an HSTS response header is emitted in `prod` only. **Do not hardcode
either on**: a `Secure` cookie over a plain-HTTP dev origin is never sent back
(local sign-in breaks silently), and an HSTS header there pins `localhost` to
HTTPS in the developer's browser.

### 3.5 Email policy

Regex-validated address; the lowercased domain is matched **exactly or as a
suffix** (`x.gmail.com` counts as `gmail.com`). A non-empty
`auth_allowed_email_domains` short-circuits as a whitelist; otherwise
`auth_blocked_email_domains` (free + disposable providers) rejects the address.

### 3.6 Admin patch semantics

- `extend_days` extends from `max(now, expires_at)`, so extending a live account
  adds time and extending a lapsed one revives it from now.
- `expires_at: null` means "never expires"; an **omitted** key leaves validity
  alone (distinguish with `model_fields_set`).
- Approving (`pending → active`) grants the default window only when
  `expires_at` is still null and the patch itself carries neither `expires_at`
  nor `extend_days` — an admin who states the window explicitly wins.
- `password: null` asks the backend to generate one; the generated value is
  returned exactly once as `generated_password` and never stored in plaintext.
  An explicit password must not be echoed back.
- Data is **not** partitioned per user. Do not add owner filtering to resource
  endpoints as part of an auth change. This is why `member` is classified as
  near read-only in §3.4: a member who could deploy could also mutate every other
  member's resources.

### 3.7 Local code execution

`/api/execute`, `/api/execute/stream` and the `/api/conversations` surface run
caller-supplied Python on the server. They are gated by
`local_exec_enabled(settings)` = `studio_local_exec_enabled` when set, otherwise
`run_mode != "prod"` — so **production refuses them** with
`403 studio.exec.disabled`. Studio local debug and AI Fix consequently do not work
in production; that is the mitigation, not a bug.

- There are **three** spawn sites: `local_exec.spawn_execution_subprocess` and two
  in `conversation_service`. Any isolation change must cover all three, and any new
  entrance must call the same gate — closing one and not the others only moves the
  door.
- The child environment is an **allowlist** (`local_exec._ENV_ALLOWLIST`), never
  `os.environ.copy()`. AWS credential variables are a separate group, forwarded
  only while `studio_exec_forward_aws_credentials` is true.
- **Keep that forward on by default.** The default Bedrock Mantle path builds
  `OpenAIResponsesModel(bedrock_mantle_config=…)` and the SDK mints a bearer token
  from the ambient credentials, so a credential-less child breaks local debug
  unless the caller supplies `bedrock_api_key`/`openai_api_key`.
- Scrubbing the environment does **not** remove AWS access: on EC2 credentials
  arrive from IMDS over the network. Verified 2026-08-03 that
  `sts:GetCallerIdentity` still succeeds from inside the child with the allowlist
  applied. Only the uid-keyed firewall rule installed by
  `scripts/setup_exec_env.sh --hardened` closes it, so treat
  `AWS_EC2_METADATA_DISABLED` as belt-and-braces rather than the control.
- Isolation arguments come from one place, `local_exec.build_spawn_kwargs()`. The
  uid drop uses subprocess's `user`/`group` arguments, **not** `preexec_fn`:
  `preexec_fn` runs arbitrary Python after a fork and is deadlock-prone in a
  threaded process, and this backend runs deploy jobs on threads. Only `setrlimit`
  stays in `preexec_fn`, with its values computed before the fork.
- When a uid is configured, the run's workdir must be handed to it
  (`grant_workdir_to_exec_user`) — `mkdtemp` is 0700 and owned by the backend
  user, so the child could not otherwise read its own code or bundled skills.

### 3.7 Frontend

The API boundary dispatches `launchpad-unauthorized` for a `401` outside
`/api/auth/*`. `AuthGate` owns that event and returns the entire console to the
login form. Do not duplicate `401` handling in individual pages. `AuthGate` also
owns the sign-in ⇄ register tab strip and maps backend error codes to i18n keys
through a lookup table (no hand-written wording per code). The register form and
its success panel branch on `registration_requires_approval` /
`requires_approval`: in approval mode the panel says "waiting for approval"
(`data-testid="register-status"`) instead of showing a validity date, and the
users table shows **APPROVE / REJECT** on `pending` rows while hiding the
enable/disable toggle there. `isAdmin` is
`!authRequired || role === "admin"` so an open console keeps full local access;
admin nav entries live in `ADMIN_NAV_ENTRIES` and the `/users` page renders a
forbidden state instead of fetching when `!isAdmin`.

The top bar exposes the signed-in identity, the role, the remaining validity for
time-boxed accounts, and a **labelled sign-out control**
(`data-testid="logout-button"`, `auth.logout`) that collapses to its icon under
720px while keeping a ≥28px tap target. Sign-out must go through the context's
`logout()` (which calls `POST /api/auth/logout` and then resets gate state
in a `finally`, so a dead session still returns to the login form) — never a
page reload or a direct `fetch`.

When the gate is disabled there is no session to end, so the same slot renders a
non-interactive **`auth.gateOff`** badge (`data-testid="auth-off-badge"`) whose
tooltip names `LAUNCHPAD_AUTH_PASSWORD`. Keep that badge: an empty slot reads as
a missing sign-out button and hides the fact that the console is open to anyone
who can reach it.

## 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| Password unset | Auth status reports disabled; console open; `/api/users*` open as implicit admin |
| Registration while the gate is disabled or `auth_registration_enabled=false` | `400 auth.registration_disabled` |
| Username not 3–32 of `[A-Za-z0-9._-]` | `400 auth.invalid_username` |
| Username taken, or equal to the configured admin name | `409 auth.username_taken` |
| Malformed email | `400 auth.invalid_email` |
| Free/disposable domain (or outside a configured allow list) | `400 auth.email_domain_blocked` |
| `status` outside `pending`/`active`/`disabled` | `400 users.invalid_status` |
| Email already registered | `409 auth.email_taken` |
| Password shorter than 8 characters | `400 auth.weak_password` |
| Correct credentials, account still `pending` | `401 auth.account_pending` |
| Missing/invalid login fields | `422 validation.invalid_request` |
| Wrong username or password | `401 auth.invalid_credentials` |
| Correct credentials, account disabled / expired | `401 auth.account_disabled` / `401 auth.account_expired` |
| Missing, malformed, tampered, or expired session | `401 auth.required` |
| Valid cookie whose account is now disabled, expired, or deleted | `401 auth.required` |
| Member session on `/api/users*` | `403 auth.forbidden` |
| Unknown user id | `404 users.not_found` |
| Patch body with no fields | `422 validation.invalid_request` |
| Valid session | Protected `/api/*` request proceeds unchanged |
| Missing `/v1` API key while console auth is enabled | Existing `401 auth.missing_api_key` |

Credential and cookie-signature comparisons must use `hmac.compare_digest`, and
an unknown username still burns one PBKDF2 verification so timing does not
disclose account existence. The status endpoint must not disclose the configured
username, role, or email before authentication. Registration is intentionally
un-rate-limited in this sample asset — call that out rather than assuming a
limiter exists.

## 5. Good / Base / Bad Cases

- Good: configure a strong admin password in the process environment, set
  `LAUNCHPAD_AUTH_COOKIE_SECURE=true` behind HTTPS, and let operators
  self-register with company email under the default 7-day validity.
- Base: leave the password unset for bootstrap-free local development and
  hermetic tests.
- Bad: protect `/v1` with the console cookie, store login state in localStorage,
  return the username/role from unauthenticated status, put the role in the
  cookie, seed the built-in admin into `users`, return a password hash from any
  endpoint, or add per-page `401` handlers.

## 6. Tests Required

Backend tests must assert:

- disabled mode remains open, login is a no-op, and registration is refused;
- console APIs and API docs are protected when enabled;
- health, auth bootstrap routes (including `register`), CORS preflight, and
  `/v1` remain independent;
- wrong credentials set no cookie; a successful login sets the required cookie
  attributes; logout, tampered/expired cookies, an authentic cookie for an
  unknown subject, password rotation, and Secure cookies all behave;
- registration creates a `pending` `member` account with no window that cannot
  sign in; approval activates it and starts the window; rejection keeps it out;
  `auth_registration_require_approval=false` yields an immediately usable account;
  every validation code in §4 is produced;
- duplicate username/email detection is case-insensitive;
- an established member session dies on expiry, disable, and delete;
- the cookie expiry is clamped to a short account validity;
- the `pending` filter and the `pending` stat surface the approval queue;
- admin list/search/status-filter/pagination, stats numbers, extend (live and
  lapsed), absolute/never expiry, disable→enable, role change, generated vs
  explicit password reset, and delete;
- a member gets `403` on all four `/api/users` routes, an anonymous caller
  `401`, and the gate-disabled console reaches them as implicit admin;
- **the open-console guard**: a non-loopback peer is refused with
  `auth.open_console_refused` while `/api/health` and the auth bootstrap routes
  stay reachable and `/v1` is untouched; a loopback peer keeps full access;
  `allow_open_console` restores access; an enabled gate yields the ordinary `401`
  instead of the refusal; `create_app()` raises under `run_mode=prod` + no auth;
- **the route table**: every live `/api` route is classified and every entry
  matches a live route (both directions — this is what stops the table rotting);
  for every `ADMIN` route, anonymous → `401` and member → `403`; removing an entry
  makes the route answer `auth.route_unclassified`;
- **cookie/HSTS posture** per `run_mode`, including that `auth_cookie_secure`
  still forces `Secure` on in dev;
- **local execution**: production refuses `/api/execute`, `/api/execute/stream`
  **and** `/api/conversations`; the opt-in restores them; the child environment
  contains no unrelated host variables; AWS credentials are forwarded by default
  and withheld under `studio_exec_forward_aws_credentials=false`; the resource
  ceilings are actually applied (read them back from a real child); the uid drop
  does not go through `preexec_fn`.

Do **not** drive the `MEMBER` route sweep over HTTP: that executes the real
handlers, which call live AWS. Assert the authorization decision by calling
`enforce_route_policy` with a request whose `scope["route"]` is set. (An earlier
version of this sweep made real `GetRegistryRecord` calls from the hermetic
suite.)

Frontend validation must assert:

- login error and pending states render, including the expired/disabled codes;
- the register tab validates locally, surfaces backend field errors, and shows
  the "valid for N days" success state that returns to sign-in;
- valid login unlocks the console and shows the operator identity, role, and
  remaining validity for member accounts;
- the `/users` nav entry and page are admin-only; a member sees the forbidden
  state instead of data;
- admin row actions (extend, disable/enable, reset password, delete) update the
  table and stats;
- language switching works on the gate and the users page;
- desktop and mobile layouts have no overlap or horizontal overflow.

Run `make verify` after all focused checks.

## 7. Wrong vs Correct

### Wrong

```python
# Trusting a role claim from the cookie: a demoted or disabled account keeps
# admin access until the cookie lapses.
subject, role, expiry = decode(cookie)
if role == "admin":
    return Identity(subject, "admin")
```

### Correct

```python
subject, _ = decode(cookie)                    # payload carries no role
if compare_digest(subject, settings.auth_username):
    return Identity(subject, ROLE_ADMIN)       # config admin: no DB row exists
user = find_by_username(db, subject)           # the row is authoritative
if user is None or user.status != "active" or is_expired(user):
    return None                                # → 401 auth.required
return Identity(user.username, user.role, ...)
```

### Wrong — authorization the reviewer cannot see

```python
# Per-route Depends: invisible to review across 19 routers, and a route added
# later defaults to *open* — the failure mode this contract exists to remove.
@router.post("/agents")
def create_agent(_: Identity = Depends(require_admin)): ...
```

### Correct

```python
# One auditable, default-deny table; an unclassified route refuses to serve.
ROUTE_POLICY = {("POST", "/api/agents"): ADMIN, ...}
app = FastAPI(dependencies=[Depends(enforce_route_policy)])
```

### Wrong — a startup-only host check

```python
# Bypassed by `uvicorn --host 0.0.0.0`, which is how the EC2 host and containers
# start the app. create_app() cannot see uvicorn's bind address at all.
if bind_host != "127.0.0.1" and not auth_enabled():
    raise SystemExit("refusing to start")
```

### Correct

```python
# Checked per request, where the caller's address is actually known.
if not enabled(settings) and not settings.allow_open_console:
    if _is_guarded_api_path(path) and not _peer_is_loopback(request):
        return JSONResponse(status_code=403, content=envelope(...))
```

The local account gate owns only the console `/api` surface. Cognito remains a
Gateway/Cedar demo dependency, and `/v1` remains API-key authenticated.
