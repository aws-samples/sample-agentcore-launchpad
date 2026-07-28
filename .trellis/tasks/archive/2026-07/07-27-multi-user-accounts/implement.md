# Implementation plan — multi-user accounts + admin user management

Ordered, each step independently checkable. Backend first (frontend types depend
on the final response shapes), browser evidence last.

## Slice 1 — settings + ledger + account service (backend core)

- [x] `app/core/config.py`: add `auth_registration_enabled: bool = True`,
      `auth_registration_valid_days: int = Field(default=7, gt=0, le=3650)`,
      `auth_blocked_email_domains: list[str]` (default list from design §7),
      `auth_allowed_email_domains: list[str] = []`.
- [x] `app/models/ledger.py`: add `User` per design §3 (`username_key`/`email`
      unique+indexed).
- [x] `app/services/users.py`:
      - `hash_password` / `verify_password` (pbkdf2_sha256, versioned string)
      - `_as_utc`, `account_state`, `days_remaining`, `serialize`
      - `validate_username` / `validate_email` / `email_domain_allowed`
      - `register_user`, `authenticate`, `get_user`, `list_users`,
        `compute_stats`, `apply_patch`, `delete_user`, `generate_password`
      - all failures raise `AppError` with the design §6 codes
- [x] Validate: `cd backend && uv run ruff check . && uv run pytest -q`

## Slice 2 — auth router: identity cookie, register, per-request liveness

- [x] Rewrite cookie helpers: `_sign(b64_payload)`, `_issue(subject, expiry)`,
      `_decode(cookie) -> tuple[str, int] | None` (base64 payload, version `1`).
- [x] `Identity` dataclass + `resolve_identity(request, settings, db)`; keep
      `is_authenticated()` as a thin wrapper (other modules may import it).
- [x] `auth_middleware`: config-admin subject short-circuits; otherwise open a
      `SessionLocal()` and reject unknown/disabled/expired accounts with
      `401 auth.required`. Add `/api/auth/register` to `_OPEN_API_PATHS`.
- [x] `login`: config admin first, then `authenticate()`; distinct
      `auth.account_disabled` / `auth.account_expired`; bump
      `last_login_at`/`login_count`; cookie expiry clamped to the account expiry;
      response adds `username|role|email|account_expires_at`.
- [x] `status`: adds `role|email|account_expires_at|registration_enabled`, still
      null-identity before auth.
- [x] `POST /api/auth/register` → 201, refuses when the gate is disabled or
      `auth_registration_enabled=false`.
- [x] Validate: `uv run pytest tests/test_auth.py -q` (update the existing
      assertions for the new response keys — the old dict-equality checks on
      `/api/auth/status` must be widened).

## Slice 3 — admin users router

- [x] `app/routers/users.py`: `require_admin` dependency + the four endpoints
      (design §6), pydantic `UserPatch` with a `≥1 field` model validator.
- [x] `app/main.py`: include the router next to the other console routers.
- [x] `backend/tests/test_users_api.py`: registration happy path + every
      validation code; login of a fresh member; expiry/disable rejection mid-session;
      admin list/filter/search/pagination; stats numbers; extend/disable/enable/
      role/reset-password/delete; member → 403 on all four routes; gate-disabled
      implicit-admin access.
- [x] Validate: `cd backend && uv run ruff check . && uv run pytest -q`

## Slice 4 — frontend API client + auth context/gate

- [x] `lib/api.ts`: extend `AuthStatus`/`AuthLoginResult`; add `ConsoleUser`,
      `UserListResponse`, `UserStats`, `UserPatchBody`; add `register`, `users`,
      `userStats`, `updateUser`, `deleteUser`.
- [x] `auth/auth-context.ts`: `role`, `email`, `accountExpiresAt`, `isAdmin`,
      `registrationEnabled`.
- [x] `auth/AuthGate.tsx`: sign-in ⇄ register tab strip, register form + field
      error mapping, success state, richer login error codes; propagate the new
      fields into the context.
- [x] `theme/app.css`: `.auth-tabs`, `.auth-hint`, `.auth-success` rules.
- [x] Validate: `cd frontend && npm run lint && npx tsc --noEmit`

## Slice 5 — users page + nav wiring

- [x] `layout/nav.ts`: `ADMIN_NAV_ENTRIES` + export a combined lookup list.
- [x] `layout/Sidebar.tsx`: admin group when `isAdmin`; dim placeholders → 11/12.
- [x] `layout/Shell.tsx`: crumb lookup includes admin entries.
- [x] `layout/Topbar.tsx`: role chip + `auth.daysLeft` for members.
- [x] `pages/Users.tsx` per design §8 (guard → stats → controls → table → row
      action modals); `App.tsx` route `users`.
- [x] `locales/en/common.json` + `locales/zh-CN/common.json`: all new keys.
- [x] Validate: `cd frontend && npm run lint && npx tsc --noEmit && npm run build`
      and `python3 scripts/i18n_check.py`

## Slice 6 — browser evidence (real stack)

Run the backend with the gate enabled so registration is live:

```bash
cd backend && LAUNCHPAD_AUTH_PASSWORD=verify-pass uv run uvicorn app.main:app --port 8000
cd frontend && npm run dev     # confirm the actual port (5173 or 5174) before driving
```

- [x] Register `qa-user@acme-corp.com` → success state → sign in → console loads.
- [x] Blocked-domain (`x@gmail.com`) and duplicate-username errors render inline.
- [x] Member session: no `/users` nav entry; direct `/users` → forbidden panel.
- [x] Admin session: stats tiles + table render; `+7d`, disable, enable, reset
      password, delete each reflect in the table/stats.
- [x] zh-CN locale renders the whole login/register + users page without overflow.
- [x] Screenshots saved as `data/multiuser-*.png` (repo convention: `data/*.png`
      is gitignored, so evidence stays out of the commit).

## Slice 7 — docs, spec, gate

- [x] Update `.trellis/spec/launchpad/console-auth.md` (signatures, config keys,
      role rules, error matrix, tests-required, wrong/correct example).
- [x] `docs/architecture.md` + `docs/api.md` (bilingual, if these document the
      auth surface) and `docs/setup*` for the new env vars.
- [x] `make verify` — must pass end to end.
- [ ] Commit (Phase 3.4) after review.

## Rollback points

- Slices 1–3 are backend-only and additive; reverting `routers/auth.py` restores
  the single-operator gate (the `users` table can stay, unused).
- Slice 5 is UI-only; reverting `nav.ts`/`App.tsx` removes the module while the
  API stays reachable.
- No AWS resource is created or mutated anywhere in this task, so there is no
  cloud-side cleanup — rollback is `git revert` plus (optionally)
  `DROP TABLE users` on the local ledger.

## Risks / watch-outs

- The existing `/api/auth/status` tests assert **exact dict equality** — they must
  be updated in the same slice that changes the payload, or the gate breaks.
- SQLite returns naive datetimes: always compare through `_as_utc`.
- The middleware now touches the DB; keep the config-admin short-circuit first so
  the common path stays DB-free and the hermetic tests stay fast.
- `frontend` has no unit-test runner — correctness evidence is lint + tsc + build
  + the Slice 6 browser pass.
