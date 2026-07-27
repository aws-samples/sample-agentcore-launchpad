# Console Authentication and Accounts

## 1. Scope / Trigger

Use this contract when changing Launchpad console authentication, console
accounts (registration, validity, roles, the admin User Management module), the
`/api` route boundary, local credential settings, or frontend handling of
expired sessions. This gate is deliberately independent from Cognito demo users
and the public `/v1` API-key surface.

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
auth_username / LAUNCHPAD_AUTH_USERNAME                     default: "admin"
auth_password / LAUNCHPAD_AUTH_PASSWORD                     default: unset
auth_cookie_secure / LAUNCHPAD_AUTH_COOKIE_SECURE           default: false
auth_registration_enabled / …_AUTH_REGISTRATION_ENABLED     default: true
auth_registration_valid_days / …_AUTH_REGISTRATION_VALID_DAYS  default: 7
auth_allowed_email_domains / …_AUTH_ALLOWED_EMAIL_DOMAINS   default: [] (allow list wins when set)
auth_blocked_email_domains / …_AUTH_BLOCKED_EMAIL_DOMAINS   default: free/disposable mail list
```

An unset or empty password disables the whole gate: the console stays open,
`/api/users*` is reachable as the implicit local admin, and registration is
refused with `auth.registration_disabled`.

### 3.2 Two credential sources, one cookie

- The **built-in admin is config-only** (`auth_username`/`auth_password`). It
  must never be written to the `users` table, must not be disableable or
  deletable from the console, and its username is reserved against registration.
- **Registered accounts** are `users` rows: `username`/`username_key` (lowercase
  mirror carrying uniqueness), lowercased unique `email`, `password_hash`,
  `role`, `status`, `expires_at`, `last_login_at`, `login_count`, `created_by`.
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

When the gate is enabled, middleware protects all `/api/*` paths except:

- `/api/health`
- `/api/auth/status`
- `/api/auth/login`
- `/api/auth/register`
- CORS `OPTIONS` requests

The middleware never guards `/v1/*`; those routes continue to require
`X-Api-Key`. A successful login sets an HMAC-signed HttpOnly, SameSite=Lax,
Path=/ cookie. Set `auth_cookie_secure=true` when HTTPS is used.

`/api/users*` requires `require_admin`, which returns the implicit admin when
the gate is disabled and otherwise demands `role == "admin"`.

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
- `password: null` asks the backend to generate one; the generated value is
  returned exactly once as `generated_password` and never stored in plaintext.
  An explicit password must not be echoed back.
- Data is **not** partitioned per user. Do not add owner filtering to resource
  endpoints as part of an auth change.

### 3.7 Frontend

The API boundary dispatches `launchpad-unauthorized` for a `401` outside
`/api/auth/*`. `AuthGate` owns that event and returns the entire console to the
login form. Do not duplicate `401` handling in individual pages. `AuthGate` also
owns the sign-in ⇄ register tab strip and maps backend error codes to i18n keys
through a lookup table (no hand-written wording per code). `isAdmin` is
`!authRequired || role === "admin"` so an open console keeps full local access;
admin nav entries live in `ADMIN_NAV_ENTRIES` and the `/users` page renders a
forbidden state instead of fetching when `!isAdmin`.

## 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| Password unset | Auth status reports disabled; console open; `/api/users*` open as implicit admin |
| Registration while the gate is disabled or `auth_registration_enabled=false` | `400 auth.registration_disabled` |
| Username not 3–32 of `[A-Za-z0-9._-]` | `400 auth.invalid_username` |
| Username taken, or equal to the configured admin name | `409 auth.username_taken` |
| Malformed email | `400 auth.invalid_email` |
| Free/disposable domain (or outside a configured allow list) | `400 auth.email_domain_blocked` |
| Email already registered | `409 auth.email_taken` |
| Password shorter than 8 characters | `400 auth.weak_password` |
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
- registration creates a `member` account with the configured validity and can
  sign in immediately; every validation code in §4 is produced;
- duplicate username/email detection is case-insensitive;
- an established member session dies on expiry, disable, and delete;
- the cookie expiry is clamped to a short account validity;
- admin list/search/status-filter/pagination, stats numbers, extend (live and
  lapsed), absolute/never expiry, disable→enable, role change, generated vs
  explicit password reset, and delete;
- a member gets `403` on all four `/api/users` routes, an anonymous caller
  `401`, and the gate-disabled console reaches them as implicit admin.

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

The local account gate owns only the console `/api` surface. Cognito remains a
Gateway/Cedar demo dependency, and `/v1` remains API-key authenticated.
