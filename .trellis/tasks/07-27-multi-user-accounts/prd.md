# Multi-user accounts with registration and admin user management

## Goal

Replace the console's single config-driven operator account with a **multi-user
account plane**: visitors can self-register with a username + company email and
get a **7-day** account, and the built-in `admin` gets a new **User Management**
console module to manage and get statistics on those accounts.

Today `backend/app/routers/auth.py` compares the submitted credentials against
`settings.auth_username` / `settings.auth_password` and issues an HMAC cookie
whose only payload is an expiry timestamp — there is no user record, no identity
in the session, and no role. This task introduces a `users` ledger table, an
identity-carrying session, a registration endpoint, and an admin-only
`/api/users` surface plus a `/users` console page.

## Scope decisions (confirmed with the user, 2026-07-27)

1. **Self-service registration, admin approval by default** (revised
   2026-07-27, superseding the original "immediate login" decision). A new
   registration lands in `pending` and cannot sign in until an admin approves it;
   the 7-day validity window starts at **approval**, not at registration. The
   old instant-activation behavior stays available behind
   `auth_registration_require_approval=false`. Still no email-verification link
   (no SES/SMTP dependency).
2. **"Company email" = free-mail blacklist.** Registration rejects public /
   disposable mail domains (gmail, qq, 163, outlook, mailinator, …); any other
   syntactically valid address is accepted. The blacklist is configurable.
3. **Shared data, role-based access only.** All authenticated users see the same
   agents / KBs / evaluations / traces. No per-user resource ownership or
   filtering is introduced. Only the User Management module is admin-gated.
4. Out of scope: password reset by email, SSO/Cognito console login, per-user
   quotas, audit log of console actions, invitation flows.

## Requirements

### R1 — User ledger

- New `users` table in the SQLite ledger: id, username (unique,
  case-insensitive), email (unique, case-insensitive), password hash, role
  (`admin` | `member`), status (`active` | `disabled`), `expires_at`,
  `created_at`, `last_login_at`, `login_count`, `created_by`.
- Passwords are stored **salted + hashed with a stdlib KDF**
  (`hashlib.pbkdf2_hmac`, per-user salt, versioned string format). No new
  third-party dependency (no passlib/bcrypt, no `email-validator`).
- The built-in admin stays **config-driven** (`LAUNCHPAD_AUTH_USERNAME` /
  `LAUNCHPAD_AUTH_PASSWORD`): it is not stored in the table and cannot be
  disabled or deleted from the console. Registered users never collide with it —
  the configured admin username is reserved.

### R2 — Registration

- `POST /api/auth/register {username, email, password}` is reachable without a
  session (added to the middleware open-path set).
- Validation: username 3–32 chars `[A-Za-z0-9._-]`, not the reserved admin name,
  not already taken; email syntactically valid, domain not in the blocked-domain
  list, not already registered; password ≥ 8 chars.
- With approval required (**default**) the account is created as
  `role=member`, `status=pending`, `expires_at=null`; it can neither sign in
  (`401 auth.account_pending`) nor hold a session. An admin approving it sets
  `status=active` and starts the clock: `expires_at = approval + auth_registration_valid_days`
  (default **7**). With `auth_registration_require_approval=false` the account is
  created `active` with the window starting at registration.
- `GET /api/auth/status` reports `registration_requires_approval` so the console
  can tell the visitor what happens after they submit, and the registration
  response echoes the resulting `status`.
- Registration is only available while the auth gate is enabled (a password is
  configured). With the gate disabled the console is already open, so
  registration returns `400 auth.registration_disabled`.

### R3 — Identity-carrying session + expiry enforcement

- The `launchpad_session` cookie payload carries the subject (username) plus the
  expiry, HMAC-signed as one unit; tampering with any field invalidates it. The
  role is resolved per request from the ledger row instead of being claimed by
  the cookie, so a demotion cannot be replayed. Cookie lifetime stays 12h but is
  clamped to the account's `expires_at`.
- Every guarded `/api/*` request re-validates the account behind the cookie: an
  unknown, disabled, or **expired** user is rejected with `401 auth.required`
  even if the cookie signature is still valid.
- `GET /api/auth/status` and `POST /api/auth/login` additionally report `role`,
  `email`, and account `expires_at`; `status` still discloses nothing before
  authentication apart from `auth_required` and whether registration is open.

### R4 — Admin user-management API

Admin-only (`403 auth.forbidden` for members):

- `GET /api/users` — list with search (`q` over username/email), status filter
  (`all|active|expired|disabled`), pagination, and derived `expired` /
  `days_remaining` fields.
- `GET /api/users/stats` — totals (all / active / expired / disabled),
  `expiring_soon` (≤3 days), `registered_last_7d`, `active_last_7d` (logged in),
  a 14-day daily registration series, and the top email domains.
- `PATCH /api/users/{id}` — approve (`status=active`, which grants the default
  window when the account has none), reject/suspend (`status=disabled`), send
  back to `pending`, extend validity by N days, set an absolute `expires_at`,
  change role, reset password.
- Statistics and the list filter both carry `pending`, so the admin can see the
  approval queue at a glance.
- `DELETE /api/users/{id}` — remove the account.
- Admin actions on one's own configured-admin identity are impossible (it has no
  row); a member cannot reach any of these routes.

### R5 — Console surfaces

- **Login page** gains a register form (username, company email, password,
  confirm) with a clear "your account is valid for 7 days" note, inline field
  errors mapped from the backend error codes, and a success state that returns to
  the sign-in form (prefilled username).
- The register success state says whether the account is usable now or waiting
  for approval, and the sign-in form maps `auth.account_pending` to a "waiting
  for approval" message.
- The users table exposes **APPROVE / REJECT** actions on `pending` rows.
- **New `/users` page**, reachable from the nav **only for `role=admin`**
  (members and the disabled-gate case never see the entry, and the route itself
  renders a "forbidden" state instead of the table).
- The page shows the statistics tiles + registration trend, then a filterable /
  searchable user table with row actions (extend +7 / +30 / custom, enable,
  disable, reset password, delete) using the existing table + toast + modal
  idioms of the console.
- The shell header shows the signed-in identity's role and, for member accounts,
  the remaining validity (e.g. "6 days left").
- All new strings are i18n keys with **en + zh-CN parity**.

### R6 — Compatibility

- With no password configured (hermetic tests, local dev) the console stays
  fully open and `/api/users*` remains reachable as an implicit admin, exactly
  matching today's "gate disabled" behavior.
- The existing `/v1` API-key surface is untouched; the console cookie never
  guards `/v1`.
- Existing `backend/tests/test_auth.py` expectations for the configured admin
  (login, logout, tampering, expiry, password rotation, Secure cookie) keep
  passing, extended for the new response fields.
- The ledger migration is additive (`Base.metadata.create_all` + the
  `_migrate()` helper in `app/core/db.py`); existing `data/launchpad.db` files
  keep working with no manual step.

## Acceptance Criteria

- [x] `POST /api/auth/register` creates a `pending` `member` account that cannot
      sign in (`401 auth.account_pending`) until an admin approves it, and the
      7-day window starts at approval; with
      `auth_registration_require_approval=false` the account is active at once.
      The username/email/password/blocked-domain/duplicate rules each return
      their documented `4xx` code.
- [x] A member session is rejected with `401 auth.required` once the account's
      `expires_at` has passed or an admin disables it — without re-login.
- [x] `GET /api/users`, `GET /api/users/stats`, `PATCH /api/users/{id}`,
      `DELETE /api/users/{id}` work for the admin session and return
      `403 auth.forbidden` for a member session.
- [x] Admin extend / disable / enable / reset-password / delete actions are
      visible in the console and reflected in the table + stats after refresh.
- [x] The `/users` nav entry and page are visible only to admin; a member
      navigating to `/users` directly gets the forbidden state, not data.
- [x] Login page registration flow works end-to-end in the browser (register →
      success → sign in → console), including the zh-CN locale.
- [x] `.trellis/spec/launchpad/console-auth.md` documents the new contract
      (signatures, config keys, error matrix, role rules).
- [x] `make verify` passes (backend ruff+pytest, infra, frontend
      eslint+tsc+build, i18n parity).

## Notes

- New settings (all `LAUNCHPAD_`-prefixed, `config/launchpad.yaml`-overridable):
  `auth_registration_enabled` (default true), `auth_registration_valid_days`
  (default 7), `auth_blocked_email_domains` (default free-mail list),
  `auth_allowed_email_domains` (default empty = only the blacklist applies —
  kept so an operator can still pin their own corporate domains later).
- Security invariants to preserve: `hmac.compare_digest` for every credential
  and signature comparison, no password hash ever leaves the backend, no
  identity disclosure from unauthenticated `/api/auth/status`.
