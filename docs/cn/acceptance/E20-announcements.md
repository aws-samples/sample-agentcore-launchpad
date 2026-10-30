# E20 Announcements 「公告管理」 (section `announcements`, `/v2/announcements`) — acceptance standard

**Risk score:** 4 (C2 × L2: hub-global, ledger-only, admin-authored; the only exposure is a stored link rendered to every member) → negatives ≥ 3 (this sheet lists 9) · **Recorded build:** to fill (host `GET /api/health`) · **Status:** draft

## 1. Scope and roles
- **Routes (`backend/app/core/route_policy.py`, all `WORKSPACE_EXEMPT` = hub-global):** `GET /api/announcements` = `MEMBER` (published snapshots only, ordered by `published_at` desc); `GET /api/announcements/manage`, `GET …/{id}`, `POST /api/announcements`, `PUT …/{id}`, `POST …/{id}/publish`, `POST …/{id}/unpublish`, `DELETE …/{id}?expected_revision=` = `ADMIN`. Draft and published snapshot are two projections of one row (`services/announcements.py`); every write carries `expected_revision` and a mismatch is `409 announcements.conflict`.
- **Roles:** `demo-admin` drafts, publishes, edits and withdraws "hr-helpdesk pilot goes live"; `hr-intern` reads the home board (`v2-board-featured-*`, `v2-board-item-*`) and gets 403 on `/v2/announcements` (`announcements-forbidden`). No `created_by` ownership rule: `created_by` / `updated_by` are display usernames; any admin may edit any announcement.
- **Out of scope:** the publisher preview / apply helpers used by the video pipeline (`test_announcements::test_publisher_*`) — they are tooling, not console behaviour; the videos directory (E22).

## 2. Capabilities and pass criteria (positive)
| id | action (UI / API) | observable pass criteria | evidence |
|---|---|---|---|
| AC-E20-01 | Create the draft (`POST /api/announcements {title, body, link_url "/v2/agents", link_label "Open Agents"}`) | 201; `status "draft"`, `revision 1`, `published_content null`, `has_unpublished_changes true`, `created_by "demo-admin"`; `GET /api/announcements` (member) does **not** list it | response.json |
| AC-E20-02 | Publish (`POST …/{id}/publish {expected_revision 1}`) | 200 `status "published"`, `revision 2`, `published_at` set, `published_content == content`, `has_unpublished_changes false` | response.json |
| AC-E20-03 | Home board without reload | the board (`v2-board-featured-<id>`) shows the announcement on the next render of `/v2` for admin and member; member `GET /api/announcements` 200 `{announcements:[{id, title, body, link_url, link_label, published_at}], total 1}` — no `revision`, no `created_by` in the public shape | shot.png ×2, response.json |
| AC-E20-04 | Edit after publish (`PUT …/{id} {…, expected_revision 2}`) | 200 `revision 3`, `has_unpublished_changes true`; the member board still shows the **old** text until the next publish | before/after response, shot.png |
| AC-E20-05 | Republish, then withdraw (`/unpublish {expected_revision 3}` then `{… 4}`) | republish 200 `revision 4` and the board shows the new text; unpublish 200 `status "draft"`, `published_at null`; board empty for members; the draft is kept (`content` intact) | response.json ×2 |
| AC-E20-06 | Manage list and detail | `GET /api/announcements/manage?limit=20&offset=0` 200 lists drafts and published rows ordered by `updated_at` desc with `total`; `GET …/{id}` 200 same admin shape; editor `announcement-editor` fields `announcement-title` / `announcement-body` / `announcement-url` / `announcement-link-label` | response.json, shot.png |
| AC-E20-07 | Delete (`DELETE …/{id}?expected_revision=5`) | 200 `{deleted true}`; `GET …/{id}` → `404 announcements.not_found`; board unchanged (it was unpublished) | response.json |

## 3. Negative / boundary cases (≥ 3)
| id | input | expected | source | risk |
|---|---|---|---|---|
| NC-E20-01 | Member `GET /api/announcements/manage` / `POST /api/announcements`; anonymous `GET /api/announcements` | `403` / `401` (route policy); UI `announcements-forbidden` for a member who deep-links `/v2/announcements` | `test_announcements::test_authenticated_roles_and_workspace_independence`; `test_route_policy::test_member_gets_403` | — |
| NC-E20-02 | Stale tab: `PUT` / `publish` / `unpublish` / `DELETE` with an old `expected_revision` | `409 announcements.conflict` "This announcement changed. Reload it before saving or publishing."; the newer content is **never** replaced or published; UI `announcement-action-error` | `test_announcements::test_stale_writes_never_replace_or_publish_newer_content`, `::test_database_revision_guard_catches_racing_sessions` | — |
| NC-E20-03 | `link_url` = `javascript:alert(1)` / `http://…` / `//evil` / `https://user:pw@host/` / `%zz` escape / contains `\` or a control char / whitespace | `422 validation.invalid_request` with the validator message ("link must be an app-relative path or an HTTPS URL …"); nothing saved | `routers/announcements.py` `safe_link`; `::test_unsafe_links_rejected` | — |
| NC-E20-04 | `link_url` without `link_label` (or the reverse) | `422` "link_url and link_label must be provided together" | `AnnouncementContent.link_has_label`; `::test_invalid_or_server_owned_content_rejected` | — |
| NC-E20-05 | `title` empty / 161 chars; `body` empty / 6001 chars; `link_label` 81 chars; unknown key (`created_by`, `status`, `published_at`) in the body | `422 validation.invalid_request` (`extra="forbid"`, length bounds); server-owned fields are never client-settable | `::test_invalid_or_server_owned_content_rejected` | — |
| NC-E20-06 | `expected_revision` as string `"1"` / `0` / missing | `422` (`strict=True`, `ge=1`) — no coercion | `RevisionRequest` / `AnnouncementEdit` | — |
| NC-E20-07 | `GET /api/announcements?limit=0` / `limit=101` / `offset=-1` | `422 validation.invalid_request` | `routers/announcements.py` `Query` bounds | — |
| NC-E20-08 | Unknown id on detail / edit / publish / delete | `404 announcements.not_found` | `services/announcements.get_announcement` | — |
| NC-E20-09 | `X-Workspace: not-a-workspace` on `GET /api/announcements` | 200 — hub-global, the header is ignored | `::test_authenticated_roles_and_workspace_independence` | — |

## 4. Evidence
Per TEMPLATE §4; this module must attach: the row JSON after every step (revision 1 → 5), member `GET /api/announcements` before publish / after publish / after edit-not-republished / after unpublish, home `shot.png` for admin and member at each state, `ledger.redacted.json` of `announcements` (display usernames are fine; no other PII in the table). No AWS call is made; no CloudWatch extract.

## 5. Automation mapping and GAPs
| case | Existing automation | Status |
|---|---|---|
| AC-01/02/04/05/07 | `backend/tests/test_announcements.py::test_draft_publication_edit_withdraw_and_delete` | covered |
| AC-03 | `::test_published_pagination_excludes_drafts`; home board rendering | partial: board UI without reload **GAP** → add to `tests/e2e/cn/01-login-overview-nav.spec.ts` (E01 supplementary chapter) |
| AC-06 | `::test_published_pagination_excludes_drafts` (admin list ordering not asserted) | partial |
| NC-01/09 | `::test_authenticated_roles_and_workspace_independence`; `test_route_policy` | covered |
| NC-02 | `::test_stale_writes_never_replace_or_publish_newer_content`, `::test_database_revision_guard_catches_racing_sessions` | covered |
| NC-03/04/05 | `::test_unsafe_links_rejected`, `::test_safe_links_and_plain_text_preserved`, `::test_invalid_or_server_owned_content_rejected` | covered |
| NC-06/07/08 | none | **GAP** → Q6a schemathesis over `app.openapi()` |

## 6. Failure classification
Ledger-only; no AWS dependency, so failures are TEST or PRODUCT. TEST: a board screenshot taken before the catalog/board refetch (the board reloads on navigation, not on a websocket); using the admin shape to assert the member response. PRODUCT: any member response carrying `revision` / `created_by` / draft `content`, any 200 on a stale revision, any saved `javascript:` link.

## 7. Open rulings
- None recorded by the writer; add rows here when a reading of the code needs a lead decision.
