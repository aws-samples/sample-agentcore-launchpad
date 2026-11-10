# E01 Workbench 「工作台」 (section `home`, `/v2`) — acceptance standard

**Risk score:** 7.5 (card: 2 / 2 / 3 / 0.5) → negatives ≥ 3 · **Recorded build:** to fill (`GET /api/health` `version`) · **Status:** draft

## 1. Scope and roles
- **Routes (`route_policy.py`):** `GET /api/health`, `GET /api/auth/status` = `PUBLIC`; `GET /api/overview`, `GET /api/overview/online-quality`, `GET /api/agents`, `GET /api/capabilities`, `GET /api/announcements` = `MEMBER`; `GET /api/announcements/manage`, `GET/PUT/DELETE /api/announcements/{id}`, `POST /api/announcements`, `…/publish`, `…/unpublish` = `ADMIN` (all announcement routes are `WORKSPACE_EXEMPT`, hub-global).
- **Page (`frontend/src/v2/pages/Home.tsx` + `home/*`):** health row ids `runtime, gateway, memory, registry, evaluation, observability, policy` (`home/common.ts`; `policy` is feature-gated and waits for `/api/capabilities`); agents poll every 10 s, overview and online quality every 30 s; announcement board loads `GET /api/announcements` on mount and on its RELOAD control. Test ids: `v2-health-<id>`, `v2-board-item-<id>`, `v2-board-featured-<id>`, `v2-home-step-<key>`.
- **Roles:** `demo-admin` reads everything and authors announcements; `hr-intern` reads the published board only; anonymous reaches `/api/health` and the login surface. No `created_by` rule applies (announcements are platform-authored).
- **Out of scope:** the Videos pages (E20), workspace switching (E19), the content of the modules the health row links to.

## 2. Capabilities and pass criteria (positive)
| id | action (UI / API) | observable pass criteria | evidence |
|---|---|---|---|
| AC-E01-01 | `demo-admin` signs in, lands on `/v2` | `GET /api/auth/status` `role admin`; sidebar items equal the `features` array of `GET /api/capabilities` (en + zh-CN); no classic-console link anywhere (r13-a) | response.json, shot.png |
| AC-E01-02 | Health row | seven `v2-health-<id>` rows; each bootstrap row (`gateway`, `memory`, `registry`, `observability`) is Ready iff `services[id]` is `true` in `GET /api/overview`; `runtime` Ready iff ≥ 1 agent `active`; `policy` reads `"<mode> · <n> active"` from `service_detail.policy` and equals `GET /api/policies/settings` (`mode`, `active_policies`); `observability` detail `aws/spans` | overview.json, settings.json, shot.png |
| AC-E01-03 | Quick entry to the architect assistant | click lands on `/v2/assistant`; `v2-home-step-*` chips link to the module routes in `home/common.ts` | shot.png |
| AC-E01-04 | Recent agents + evaluation card | `hr-helpdesk-wb006` listed with status and creator; card shows `eval_runs` / `eval_pass_rate` from the overview (`null` pass rate before any run) and the ONLINE QUALITY tile from `/api/overview/online-quality` (`cached` flag present) | overview.json, quality.json |
| AC-E01-05 | Admin publishes 「hr-helpdesk 试运行上线」 | `POST /api/announcements` 201 (draft, `revision 1`) → `POST …/{id}/publish` 200; `GET /api/announcements` lists the published projection only; board shows `v2-board-item-<id>` (featured row when flagged) after the next board load, without a hard page reload | request.json, response.json, shot.png |
| AC-E01-06 | Member view (`hr-intern`) | board visible with the same published item; no manage entry in the UI; `GET /api/announcements/manage` → 403 | two-identity response.json |
| AC-E01-07 | Build stamp | `GET /api/health` 200 `{status: "ok", version, region}`; `version` recorded as the module's build id | health.json |

## 3. Negative / boundary cases (≥ 3)
| id | input | expected (status + `error.code` + message / UI) | source | risk |
|---|---|---|---|---|
| NC-E01-01 | anonymous `GET /api/overview` | `401 auth.required`; UI redirects to login | `test_route_policy::test_anonymous_gets_401` | R9 authorization drift |
| NC-E01-02 | `hr-intern` `GET /api/announcements/manage` or `POST /api/announcements` | `403` (route table `ADMIN`); no draft content leaks to members | `test_route_policy::test_member_gets_403`, `test_announcements::test_authenticated_roles_and_workspace_independence` | R9 |
| NC-E01-03 | title 161 chars / empty body / `link_label` 81 chars | `422 validation.invalid_request` (`AnnouncementContent` field limits: title ≤ 160, body ≤ 6000, link_url ≤ 2048, link_label ≤ 80) | `routers/announcements.py` | R9 (input hygiene) |
| NC-E01-04 | `link_url` with a `javascript:` scheme or server-owned fields in the body | rejected before any write (`test_announcements::test_unsafe_links_rejected`, `::test_invalid_or_server_owned_content_rejected` pin the code) | `services/announcements.py` | R4 (XSS on the public board) |
| NC-E01-05 | edit / publish with a stale `expected_revision` | `409 announcements.conflict` "This announcement changed. Reload it before saving or publishing."; the newer content stays published | `services/announcements.py::_conflict` | operator race |
| NC-E01-06 | unknown announcement id | `404 announcements.not_found` | `services/announcements.py` | — |
| NC-E01-07 | registry backend unreachable while the home page polls | `GET /api/overview` still 200; `registry_assets` from cache or an `unavailable_reason`; the health row shows the registry row not Ready, other rows unaffected | `test_overview::test_overview_registry_failure_falls_back_to_cache`, `::test_overview_exposes_registry_unavailable_reason` | R2 upstream failure |
| NC-E01-08 | workspace with 0 ACTIVE policies (or `mode off`) | `policy` row not Ready, detail reads the mode and `0 active`; this is expected, not a defect | `test_overview::test_overview_policy_row_is_ledger_derived_not_a_resource_key`, `::test_overview_policy_row_is_off_in_mode_off` | expectation setting |

## 4. Evidence
Per TEMPLATE §4. This module adds: `overview.json` and `policies-settings.json` captured in the same minute as `shot.png` of the health row; `capabilities.json` next to the sidebar screenshot; announcement `request.json` with the published body. No Runtime call is involved, so no `cw/` capture.

## 5. Automation mapping and GAPs
| case | existing automation | status |
|---|---|---|
| AC-01 | `tests/e2e/cn/01-login-overview-nav.spec.ts::case 1` (nav ⊆ capabilities, en + zh-CN) | covered |
| AC-02 / NC-07 / NC-08 | `backend/tests/test_overview.py::test_overview_tiles_and_health`, `::test_overview_policy_row_*`, `::test_overview_registry_failure_falls_back_to_cache`; `frontend/src/v2/pages/home/ServiceHealth.test.tsx` | covered (API); **GAP**: no e2e cross-checks the rendered health row against `/api/policies/settings` → add a step to spec 01 |
| AC-04 | `test_overview_eval.py::test_overview_carries_eval_pass_rate_and_run_count`, `::test_online_quality_route_exists_with_the_contract_keys` | covered (API); UI card **GAP** |
| AC-05 / AC-06 / NC-03…06 | `test_announcements.py` (12 tests: lifecycle, stale revisions, unsafe links, roles) | covered (API); **GAP**: no e2e publishes an announcement and asserts the board (`v2-board-item-*`) → new spec `50-announcements` |
| AC-07 / NC-01 / NC-02 | `test_health.py::test_health_returns_ok`; `test_route_policy.py::test_anonymous_gets_401`, `::test_member_gets_403` | covered |

## 6. Failure classification
No AWS call on the critical path except the overview's registry/observability probes: a registry `unavailable_reason` or a Logs Insights error in the health row is `ENV` (record the reason string). Everything else here is `TEST` (selectors, 10 s / 30 s poll timing) or `PRODUCT`.

## 7. Open rulings
- The video card lists health rows `runtime/gateway/identity/observability/memory/registry/kb/policy`; the page renders `runtime, gateway, memory, registry, evaluation, observability, policy` (no identity or KB row). Narration should follow the code; or ask for the two rows to be added (PRODUCT request).
- The board is pull-based (mount + RELOAD); "appears without refresh" should be demonstrated by navigating back to `/v2`, not by waiting on the open page.
