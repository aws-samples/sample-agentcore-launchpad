# E18 User management 「用户管理」 (section `users`, `/v2/users`) — acceptance standard

**Risk score:** 20 (R1 public login without lockout + R10 administrator self-lockout + R9 authorization) → negatives ≥ 10 · **Recorded build:** to fill · **Status:** draft

Chinese working copy: `plans/acceptance/E18-users.md` (outside the repo) — ported 2026-10-18 after qa-planner's amendments; case ids, codes and tests identical, test paths repo-relative.

## 1. Scope and roles
- **Routes:** `POST /api/auth/login`, `POST /api/auth/register`, `GET /api/auth/status` = `PUBLIC`; `POST /api/auth/logout` = `MEMBER`; `GET /api/users`, `GET /api/users/stats`, `PATCH /api/users/{id}`, `DELETE /api/users/{id}` = `ADMIN` (all `WORKSPACE_EXEMPT`).
- **Roles:** `demo-admin` approves / disables / resets / deletes; `hr-intern` (member) and the newly registered `hr-intern-2`; an anonymous registrant. The built-in config admin has no `users` row and cannot be locked out. `created_by` cascades: deleting an account → its API keys are disabled, not deleted; assistant conversations cascade (N-ARCH-obs6).
- **Out of scope:** the workspace grant matrix in detail (E19); Cedar policies about users (E07).

## 2. Capabilities and pass criteria (positive)
| id | action | observable pass criteria | evidence |
|---|---|---|---|
| AC-E18-01 | Register `hr-intern-2` | `POST /api/auth/register` 201, `status pending`, `requires_approval true`, `expires_at null`; UI `register-success` | response.json |
| AC-E18-02 | Pending account signs in | `401 auth.account_pending`, message "waiting for administrator approval" | response.json |
| AC-E18-03 | Approve and grant | `PATCH {status:"active", workspace_ids:[default]}` 200 → `status active`, `role member`, `expires_at` = now + `auth_registration_valid_days`; UI `v2-users-approve` | response, ledger.redacted (users row) |
| AC-E18-04 | Member login and boundaries | 200; `/api/auth/status.role = member`; no `/v2/users` in the sidebar; `GET /api/users` 403; `DELETE /api/agents/{admin's}` 403 `agent.forbidden`; their own agent 200 | responses for both identities, shot |
| AC-E18-05 | Disable | `PATCH {status:"disabled"}` 200; the member's existing cookie gets 401 `auth.required` on the next request; their API keys `creator_offboarded = disabled` | response, apikeys.json |
| AC-E18-06 | Reset password | `PATCH {password:null}` 200, `generated_password` appears exactly once (**masked on screen**); the old password 401 | response (redacted) |
| AC-E18-07 | Extend validity | `PATCH {extend_days:30}` → `expires_at` +30 d; `days_remaining` updated | response |
| AC-E18-08 | Delete the account | `DELETE` 200 `{ok, conversations:{deleted,reassigned}}`; the account's keys still listed and `disabled`; assistant conversations it deployed from are reassigned to the admin | response, apikeys.json |
| AC-E18-09 | List / stats | `GET /api/users?q=hr&status=pending&limit=20` pagination fields correct; `/stats` counts agree with the list | response |

## 3. Negative / boundary cases (≥ 10)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E18-01 | username `ab` / with a space / 33 chars | `400 auth.invalid_username` (3–32, alphanumerics `._-`) | `backend/app/services/users.py` `_USERNAME_RE` | — |
| NC-E18-02 | Register the config admin's username | `409 auth.username_taken` "reserved" | `validate_username` | — |
| NC-E18-03 | Same username / email differing only in case | `409 auth.username_taken` / `409 auth.email_taken` | `test_users_api::test_duplicate_username_and_email_are_case_insensitive` | — |
| NC-E18-04 | Public mail domain (`@qq.com`) / domain not allowed | `400 auth.email_domain_blocked` | `validate_email` | — |
| NC-E18-05 | Password shorter than the effective minimum (`auth_password_min_length`, default 12, floor 6 = `MIN_PASSWORD_LENGTH`); in `strict` mode (default) also a password equal to the username / email / email local part, or on `COMMON_PASSWORDS` | `400 auth.weak_password` with `detail.reason` `too_short` (`min_length` = the configured minimum) / `matches_identity` / `common`; in `relaxed` mode only `too_short` can occur and an 8-char `password` is accepted when `min_length` ≤ 8. `GET /api/auth/status` → `password_policy: {mode, min_length}` must match the configured values, and the registration hint must render that minimum (no hard-coded 12). **Recording note:** the CN hosts run `relaxed` + `min_length 8` (user decision 2026-10-19), so the recorded refusal is a 7-char password → `too_short`, `min_length 8` | `validate_password`, `password_policy` (`backend/app/services/users.py`); `backend/tests/test_password_policy.py`; `frontend/src/auth/AuthGate.test.tsx` | — |
| NC-E18-06 | Register while `auth_registration_enabled false` | `400 auth.registration_disabled`; no register entry in the UI | `backend/app/routers/auth.py` | — |
| NC-E18-07 | Wrong password / unknown user | `401 auth.invalid_credentials`, identical message (no existence leak); visible UI hint | e2e `18::case 18c` | — |
| NC-E18-08 | Expired account signs in / holds an old cookie | `401 auth.account_expired`; the cookie never outlives the account | `test_users_api::test_session_cookie_never_outlives_the_account` | — |
| NC-E18-09 | `GET /api/users?status=foo` | `422 validation.invalid_request` | `backend/app/routers/users.py` | — |
| NC-E18-10 | Empty PATCH body / `workspaces` and `workspace_ids` together | `422` (pydantic) | `UserPatch` validator | — |
| NC-E18-11 | `workspace_ids:["nope"]` / `role:"owner"` | `400 users.invalid_workspaces` / `users.invalid_role` | `apply_patch` | — |
| NC-E18-11b | `extend_days:0` / `3651`; PATCH with an unknown key (e.g. `nickname`) | `422` (pydantic `ge/le`, **never** `users.invalid_extension`); unknown key `422` (`UserPatch extra=forbid`) | r13-d hermetic | — |
| NC-E18-12 | PATCH / DELETE a non-existent id | `404 users.not_found` | `get_user` | — |
| NC-E18-13 | **Login protection**: 6 wrong passwords for one username / 30 registrations within 1 minute | `429 auth.too_many_attempts` + `Retry-After`; the correct password also 429 during the lockout; recovers after 15 min | **GAP (no lockout today)** → Q2a `cn/r12-g` | — |
| NC-E18-14 | Admin demotes the last admin to member / disables them | expected `409 users.last_admin` (guard being implemented in `cn/r12-g`; no guard today) | R10 → r12-g | — |
| NC-E18-15 | Admin `DELETE`s / disables themselves | expected `409 users.self_delete` (same, r12-g pending) | R10 → r12-g | — |
| NC-E18-16 | Admin opens a deleted member's ledger-only conversation | 404 (cascade-deleted); the response's `conversations.deleted` listed the id | N-ARCH-obs6 | — |

## 4. Evidence
`response.json` (`generated_password` and cookie values always `***`), `shot.png`, `ledger.redacted.json` (`users` without `password_hash`, `api_keys` without `key_hash`), `auth_attempts` table snapshot (once Q2a lands). Passwords come from SSM only and never touch disk.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01/02/03/04 | `tests/e2e/cn/12-users.spec.ts::case 12`; `backend/tests/test_users_api.py::test_registration_awaits_admin_approval_by_default`, `::test_pending_account_cannot_sign_in`, `::test_approval_starts_the_validity_window_*`; `backend/tests/test_users_approval_grants.py` (3 tests) | covered |
| AC-05/08 | `backend/tests/test_users_api.py::test_disabled_account_loses_an_established_session`, `::test_deleted_account_loses_*`; `backend/tests/test_api_key_lifecycle.py` | covered; cascade response assertion partial |
| AC-06/07/09 | `backend/tests/test_users_api.py::test_list_search_filter_and_pagination` | partial: password reset, extension **GAP** |
| NC-01…08 | `backend/tests/test_users_api.py::test_validation_rules`, `::test_wrong_password_reports_generic_credentials_error`, `::test_allowed_domains_override_the_blacklist`, `backend/tests/test_auth.py::test_tampered_and_expired_cookies_are_rejected` | covered |
| NC-11b | r13-d hermetic tests (`cn/r13-d`) | covered |
| NC-09/10/11/12 | none dedicated | **GAP** → Q6a schemathesis |
| NC-13 | none | **GAP** → Q2a `test_login_protection.py` + e2e 18c extension |
| NC-14/15/16 | none | **GAP** → new `test_users_admin_guard.py`; NC-16 indirectly exercised by e2e `99-cleanup` |

## 6. Failure classification
The module does not depend on AWS, so a failure is almost always TEST or PRODUCT; the only ENV signal is an SSM password read failing (a recording precondition).

## 7. Open rulings
- None recorded by the writer; add rows here when a reading of the code needs a lead decision.
- **2026-10-19 (user decision, N14-obs3, `cn/r14-c`) — password policy relaxed.** NC-E18-05 originally read "shorter than 8 (`MIN_PASSWORD_LENGTH`)" against the r12-g policy (min 12, not the username / email, not a common password; floor 8). New truth: `auth_password_policy` `strict` (default; the r12-g rules) vs `relaxed` (minimum length only); floor of `auth_password_min_length` is 6; `GET /api/auth/status` and the login response carry the additive `password_policy: {mode, min_length}` the registration form hints from. **CN hosts: relaxed + min_length 8 (user decision 2026-10-19).** NC-E18-05 was rewritten in place to the new expectation; the recorded build must state which mode was active.
