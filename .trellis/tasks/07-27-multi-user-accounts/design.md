# Design — multi-user accounts + admin user management

## 1. Current state (verified)

| Piece | Today |
|---|---|
| `backend/app/routers/auth.py` | `login` compares against `settings.auth_username` / `auth_password` with `compare_digest`; cookie payload is `"{expiry}.{hmac(expiry)}"` — **no identity**; `auth_middleware` guards `/api/*` minus `{/api/health, /api/auth/status, /api/auth/login}`; signing key = sha256 of `agentcore-launchpad-session:{username}:{password}` |
| `backend/app/core/config.py` | `auth_username` (default `admin`), `auth_password: SecretStr \| None`, `auth_cookie_secure` |
| `backend/app/core/db.py` | `init_db()` = `create_all` + hand-written additive `_migrate()`; **no Alembic** |
| `frontend/src/auth/AuthGate.tsx` | Fetches `/api/auth/status`, renders `LoginPage` or children, owns the `launchpad-unauthorized` event |
| `frontend/src/auth/auth-context.ts` | `{authRequired, username, logout}` |
| `frontend/src/layout/{nav.ts,Sidebar.tsx,Topbar.tsx}` | Static `NAV_ENTRIES` (9 entries, `PLATFORM_COUNT=6` splits platform/operate), dim placeholders idx `10`/`11`; Topbar shows `username · OPERATOR` + logout |
| Deps | No `passlib`/`bcrypt`, **no `email-validator`** (so no `EmailStr`) → stdlib only |

## 2. Component map

```
backend/app/
  core/config.py                +4 settings
  models/ledger.py              +User
  services/users.py             NEW  password KDF, validation, register/authenticate, list/stats/patch
  routers/auth.py               MOD  identity cookie, register, richer status/login, per-request account check
  routers/users.py              NEW  admin-only /api/users surface (require_admin dependency)
  main.py                       MOD  include users router
backend/tests/
  test_auth.py                  MOD  extended for new fields + member sessions
  test_users_api.py             NEW  registration rules, expiry enforcement, admin API, RBAC
frontend/src/
  lib/api.ts                    MOD  types + register/users/stats/patch/delete
  auth/auth-context.ts          MOD  role/email/expiresAt/isAdmin
  auth/AuthGate.tsx             MOD  sign-in ⇄ register tabs
  layout/nav.ts                 MOD  ADMIN_NAV_ENTRIES + navEntries(isAdmin)
  layout/Sidebar.tsx            MOD  render admin group when admin
  layout/Topbar.tsx             MOD  role chip + days-left for members
  pages/Users.tsx               NEW  stats tiles + trend + table + row actions
  App.tsx                       MOD  /users route
  locales/{en,zh-CN}/common.json  MOD  new keys (parity enforced)
  theme/app.css                 MOD  auth tab strip + a few users-page utility rules
.trellis/spec/launchpad/console-auth.md  MOD  contract update (step 3.3)
```

## 3. Data model

```python
class User(Base):
    __tablename__ = "users"
    id: str(32) pk = uuid4().hex
    username: String(64)                     # as typed, for display
    username_key: String(64) unique index    # username.lower() → case-insensitive uniqueness
    email: String(160) unique index          # stored lowercased
    password_hash: String(256)               # pbkdf2_sha256$<iters>$<salt_b64>$<dk_b64>
    role: String(16) = "member"              # member | admin
    status: String(16) = "active"            # active | disabled
    expires_at: DateTime(tz) | None          # None = never expires (admin-granted)
    created_at / updated_at: DateTime(tz)
    last_login_at: DateTime(tz) | None
    login_count: int = 0
    created_by: String(64) = "self"          # "self" | <admin username>
```

`create_all` creates the new table; **no `_migrate()` entry is needed** (additive
table, not a column). SQLite `DateTime(timezone=True)` returns naive datetimes on
read — every comparison goes through one helper that re-attaches `UTC`
(`_as_utc(dt)`), mirroring how the rest of the ledger is read.

Derived (never stored): `state = disabled | expired | active`, `days_remaining`.

## 4. Password hashing (stdlib)

```
pbkdf2_sha256$390000$<urlsafe_b64 salt(16B)>$<urlsafe_b64 dk(32B)>
hash:   hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations, dklen=32)
verify: recompute with the parsed params, hmac.compare_digest on the raw dk
```

Format is versioned by its `algo$iters` prefix so iterations can be raised later
without breaking stored hashes. Unknown/garbled prefix → `verify` returns False.

## 5. Session cookie — identity, one signature

```
payload   = "1:{subject}:{expiry_epoch}"          # subject = username as authenticated
cookie    = urlsafe_b64(payload) + "." + hmac_sha256(signing_key, b64_payload).hexdigest()
signing_key = sha256(f"agentcore-launchpad-session:{auth_username}:{auth_password}")
```

Decisions:

- **Base64 the whole payload** so a `:`/`.` inside a configured admin username
  cannot desync parsing; sign the encoded string, `compare_digest` the full cookie.
- **Role is NOT in the cookie.** Authorization is resolved per request:
  `subject == settings.auth_username` → the config admin (`role=admin`, no row);
  otherwise the `users` row is authoritative for role *and* liveness. This
  removes any stale-claim risk after an admin demotes/disables someone.
- Signing key still derives from the configured admin credentials, so rotating
  `LAUNCHPAD_AUTH_PASSWORD` invalidates **all** sessions (keeps the existing
  `test_password_rotation_invalidates_session` semantics; documented in the spec).
- Cookie lifetime = `min(now + 12h, account.expires_at)`; `max_age` follows it, so
  the browser drops it at account expiry too.

Resolution helper (used by middleware and the admin dependency):

```python
@dataclass(frozen=True)
class Identity:
    username: str
    role: str            # admin | member
    email: str | None
    expires_at: datetime | None
    user_id: str | None  # None for the config admin

def resolve_identity(request, settings, db) -> Identity | None
# None ⇒ 401: bad signature, expired cookie, unknown user, disabled, or account expired
```

`auth_middleware` opens its own `SessionLocal()` (try/finally) only when the gate
is enabled and the path is guarded; the config-admin subject short-circuits
before any DB hit, so the common single-operator case stays DB-free.

## 6. HTTP contracts

### Open (no session)

```
GET  /api/auth/status
  → {auth_required, authenticated, username, role, email, account_expires_at,
     registration_enabled}
    Unauthenticated ⇒ username/role/email/account_expires_at all null
       (no identity disclosure); registration_enabled still reported so the login
       page can show/hide the register tab.

POST /api/auth/register {username, email, password}
  → 201 {ok, username, email, expires_at, valid_days}
```

`_OPEN_API_PATHS` gains `/api/auth/register`.

### Session

```
POST /api/auth/login  {username, password}
  → {ok, auth_required, expires_at,            # cookie expiry (unchanged field)
     username, role, email, account_expires_at}
POST /api/auth/logout → {ok}
```

### Admin-only (`Depends(require_admin)`)

```
GET    /api/users?q=&status=all|active|expired|disabled&limit=50&offset=0
       → {items: ConsoleUser[], total, limit, offset}
GET    /api/users/stats
       → {total, active, expired, disabled, expiring_soon,       # ≤3 days
          registered_last_7d, active_last_7d,
          registrations: [{date: "YYYY-MM-DD", count}] × 14,
          top_domains: [{domain, count}] × ≤5,
          valid_days}                                            # configured default
PATCH  /api/users/{id} {status?, role?, extend_days?, expires_at?, password?}
       → ConsoleUser         (≥1 field required; 422 otherwise)
DELETE /api/users/{id} → {ok}
```

`ConsoleUser = {id, username, email, role, status, state, expires_at,
days_remaining, created_at, last_login_at, login_count, created_by}` — never the
hash.

`require_admin`: gate disabled → implicit admin (matches today's open console, so
hermetic tests need no session); gate enabled → `resolve_identity` must yield
`role == "admin"`, else `403 auth.forbidden` (`401 auth.required` is already
produced by the middleware for missing sessions).

`extend_days` semantics: extend from `max(now, current expires_at)` so extending a
live account adds time rather than truncating it; extending an expired account
revives it from now. `expires_at: null` in the patch body means "never expires"
(`{"expires_at": null}` is distinguished from an omitted key via
`model_fields_set`).

### Error matrix (new codes)

| Condition | Code | HTTP |
|---|---|---|
| Registration while gate disabled / `auth_registration_enabled=false` | `auth.registration_disabled` | 400 |
| Bad username shape | `auth.invalid_username` | 400 |
| Username taken / equals reserved admin name | `auth.username_taken` | 409 |
| Malformed email | `auth.invalid_email` | 400 |
| Blocked (free-mail/disposable) domain | `auth.email_domain_blocked` | 400 |
| Email already registered | `auth.email_taken` | 409 |
| Password < 8 chars | `auth.weak_password` | 400 |
| Correct credentials, account disabled | `auth.account_disabled` | 401 |
| Correct credentials, account expired | `auth.account_expired` | 401 |
| Wrong credentials (either surface) | `auth.invalid_credentials` | 401 |
| Member hitting `/api/users*` | `auth.forbidden` | 403 |
| Unknown user id | `users.not_found` | 404 |
| Empty patch body | `validation.invalid_request` | 422 |

## 7. Email policy

```python
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9]([A-Za-z0-9.-]{0,251})\.[A-Za-z]{2,24}$")
```

`auth_blocked_email_domains` default (free + disposable, lowercase): gmail.com,
googlemail.com, yahoo.com, yahoo.co.jp, hotmail.com, outlook.com, live.com,
msn.com, aol.com, icloud.com, me.com, mac.com, proton.me, protonmail.com,
gmx.com, gmx.de, zoho.com, mail.com, mail.ru, yandex.com, yandex.ru, qq.com,
foxmail.com, 163.com, 126.com, yeah.net, sina.com, sina.cn, sohu.com, tom.com,
21cn.com, 139.com, 189.cn, aliyun.com, mailinator.com, guerrillamail.com,
10minutemail.com, tempmail.com, temp-mail.org, trashmail.com, throwawaymail.com,
yopmail.com, sharklasers.com, getnada.com, dispostable.com.

Matching is on the lowercased domain, exact **or** suffix (`.gmail.com`), so
regional variants of a blocked base domain do not slip through.
`auth_allowed_email_domains` (default `[]`) short-circuits as a whitelist when an
operator sets it, checked before the blacklist.

## 8. Frontend

**Auth context** → `{authRequired, username, role, email, accountExpiresAt,
isAdmin, registrationEnabled, logout}`. `isAdmin = !authRequired || role === "admin"`
so the gate-disabled dev console keeps full access.

**AuthGate / LoginPage** — one panel with a two-tab strip (`auth.signIn` /
`auth.register`), shown only when `registration_enabled`. The register form
(username, company email, password, confirm) validates locally (confirm match,
length) then maps backend codes to field-level messages via a
`code → i18n key` record. Success replaces the form with a confirmation block
("valid for N days") and a button back to sign-in with the username prefilled.
Login errors additionally distinguish `auth.account_expired` /
`auth.account_disabled` from `auth.invalid_credentials`.

**Nav** — `nav.ts` keeps `NAV_ENTRIES` (platform/operate) and adds:

```ts
export const ADMIN_NAV_ENTRIES: NavEntry[] = [
  { idx: "10", to: "/users", labelKey: "nav.users" },
];
```

`Sidebar` renders an `nav.administration` group from `ADMIN_NAV_ENTRIES` when
`isAdmin`; the existing dim placeholders shift to idx `11`/`12`. `Shell`'s
`crumbKeyFor` searches `[...NAV_ENTRIES, ...ADMIN_NAV_ENTRIES]`.

**`/users` page** (`pages/Users.tsx`) — reuses `ViewHead`, `Panel`, `StatTile`,
`DataTable`, `Chip`, `Btn`, `ConfirmDialog`, `useToast`:

1. Guard: `!isAdmin` → `Panel` with a forbidden notice (no fetch).
2. Stats row: total / active / expiring-soon / expired+disabled tiles, plus a
   14-day CSS bar sparkline and the top-domain list.
3. Controls: search box (debounced), status segmented filter, refresh — filter
   state lives in `?status=&q=` (URL-safe per project convention).
4. Table columns: user (username + email), role chip, state chip
   (active/expired/disabled), validity (`expires_at` + `days_remaining`),
   created, last login, logins, actions.
5. Row actions: `+7d`, `+30d`, custom-days prompt (inline modal), disable/enable,
   reset password (generated 12-char password shown once in a modal + copy),
   delete (`ConfirmDialog`). Each action → `PATCH`/`DELETE` → toast → refetch list
   + stats.

**Topbar** — role chip shows `ADMIN`/`MEMBER` from the resolved role; for a
member with an expiry, appends `t("auth.daysLeft", {days})`.

**i18n** — new `auth.register*`, `usersPage.*`, `nav.users`,
`nav.administration` keys added to both locales; `scripts/i18n_check.py` enforces
parity.

## 9. Security & compatibility notes

- Every credential/signature comparison uses `hmac.compare_digest`; unknown
  usernames still run one dummy PBKDF2 verify so response timing does not reveal
  account existence.
- Registration is rate-unlimited by design here (single-tenant demo asset); this
  is called out in the spec as a known limitation rather than silently ignored.
- Response models never include `password_hash`; reset-password returns the
  generated password exactly once and stores only the hash.
- Gate-disabled behavior (hermetic tests, `make dev` without a password) is
  unchanged: console open, `/api/users*` reachable, registration refused with a
  clear code.
- `/v1` API-key auth, the deploy pipeline, and every other router are untouched.

## 10. Rejected alternatives

| Option | Why not |
|---|---|
| Seed the admin into `users` on startup | Two sources of truth for the admin password; breaks `LAUNCHPAD_AUTH_PASSWORD` rotation semantics and the existing test |
| JWT / server-side session table | The signed-cookie + per-request DB liveness check already gives revocation; a new session store buys nothing here |
| `passlib[bcrypt]` | New dependency for a demo asset; `pbkdf2_hmac` is stdlib and sufficient at 390k iterations |
| `pydantic.EmailStr` | Requires the missing `email-validator` package; a regex plus the domain policy is enough |
| Per-user resource ownership | Explicitly out of scope (user decision 3); would touch every list endpoint |
| Role claim inside the cookie | Stale after demote/disable; resolving from the row is strictly safer |
