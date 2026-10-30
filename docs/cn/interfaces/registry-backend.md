# Interface: registry backend (N1 → N4 — implemented by LocalRegistryBackend)

Frozen 2026-10-08. Full Python signatures are also pasted verbatim in
`docs/cn/interfaces/features.md`; this file adds call sites, status semantics and the
Task-5 reviewer's contract notes for whoever implements `RegistryBackend` (N4, local
Registry on SQLite + FTS5 per `docs/cn/GRAPH-LOOP-SPEC.md` §3 N4).

**Implemented 2026-10-08 (N4)** by `backend/app/services/registry_local.py::LocalRegistryBackend`,
registered in `app/main.py::create_app` right after `init_db()` when `"registry"` is in
`Settings.features`. Records live in the `registry_records` ledger table
(workspace-scoped; unique index on `(workspace_id, name, record_version)`);
`registry_id = "local-<workspace_id>"` is written by both bootstraps
(`resources.registry_id`; no `registry_arn`).

## Where it lives

`backend/app/services/registry_backend.py` defines `RegistryBackend(Protocol)` plus
`set_backend(impl) / backend_or_none() / get_backend()`, `RegistryUnavailableError`
(409-free optional-capability 503), and a set of **pure, backend-independent**
descriptor/card builders (`build_a2a_card`, `build_a2a_descriptors`, `stored_a2a_card`,
`build_mcp_descriptors`, `build_skills_descriptors`, `derive_card_skills`,
`agent_card_diff`, `data_plane_invocations_url`, `ga_record_type`,
`platform_record_type`, `bump_minor_version`, `descriptor_fingerprint`, and the
`current_actor: ContextVar[str | None]` used for `decided_by`). The GA wire-format
translators `to_ga_descriptors` / `from_ga_descriptors` / `normalize_record` /
`wrap_descriptors_for_update` lost their last caller in N4 and are deleted (ruling R11).

Records are returned in the platform contract: `recordId`, `name`, `description`,
`descriptorType` ∈ `A2A`/`MCP`/`AGENT_SKILLS`/`CUSTOM`, `status`, `statusReason`,
`recordVersion`, `descriptors`, `createdAt`, `updatedAt` — the shape every
`LocalRegistryBackend` method returns directly and `routers/registry._record_out` reads.

**State machine** (ruling R4):

| from \ action | submit | approve / publish | reject | disable |
|---|---|---|---|---|
| DRAFT | → PENDING_APPROVAL | 409 | 409 | 409 |
| PENDING_APPROVAL | 409 | → APPROVED | → REJECTED | 409 |
| APPROVED | 409 | 409 | 409 | → DEPRECATED |
| REJECTED | → PENDING_APPROVAL | → APPROVED | 409 | 409 |
| DEPRECATED (terminal) | 409 | 409 | 409 | 409 |

409 = `RegistryConflictError("registry.invalid_transition")` with
`detail = {record_id, from_status, to_status}`. `update_record` with new
`descriptors`/`record_version` resets any non-terminal record to DRAFT
(`statusReason = "updated — awaiting review"`); description-only edits keep status and
version; `update_record` on DEPRECATED → 409 `registry.record_deprecated`.
`decided_at`/`decided_by` are stamped from `registry_backend.current_actor` (set by the
`/action` route).

**Who may take each action (N17-obs248, user ruling 2026-10-22).** `submit` is a member
operation (any member who can see the record). `approve`, `publish`, `reject` and `disable`
are an administrator's decision: `POST …/action` answers a member
`403 registry.approval_admin_only {action}` before the visibility check and any backend
call (rev-r23: no exists-but-hidden oracle), so the table above is never reached.
The platform paths that approve on their own —
`_approve_platform_target` / `_reconcile_platform_target` for shared-gateway targets
(bootstrap, `sync-defaults`, a platform-gateway import) and the sample-skill refresh — run as
the `system` actor. **Who may trigger an auto-approval (N17-obs257, user ruling
2026-10-22):** host bootstrap (`services/bootstrap.ensure_registry_defaults`, the workspace
bootstrap job) and an administrator's `sync-defaults` / platform import
(`auto_approve=True`). A member's sync or platform import creates new target records and
only submits them as `system` (PENDING_APPROVAL, null creator kept), reconciles no existing
DRAFT/PENDING row — a
member can add targets to the platform gateway (Identity → gateway targets, perm
`identity.manage`), so a member-run import must not publish them. A later administrator
sync / import or bootstrap reconciles those rows to APPROVED; that run is the
administrator's explicit approval, and the rows sit in the 待审批 tab until then so an
administrator can also approve or reject each one. The responses carry `auto_approved`.
They adopt only a **platform** record — server URL
= the workspace's `gateway_url` AND no creator (`created_by` and `created_by_name` both
null; the platform paths create rows outside any creator stamp). A member-authored record
under a target's name, whatever its URL, is never reconciled and the import reports it as
`registry.name_held` (N17-obs256). The console register / edit routes refuse the platform
gateway URL itself (normalised scheme/host/path) with `422
registry.platform_gateway_url_reserved`, for administrators too. `reapprove_record` (Skill Lab publish) approves only for
an administrator; for a member it submits (DRAFT / REJECTED → PENDING_APPROVAL) and stops.
A member's content edit of an APPROVED record still settles it into DRAFT (R2), so it needs a
new submit and an administrator's approval. Records a member approved before the rule stay
as they are (no migration).

## Methods — signature, return shape, caller

| method | return shape | called from |
|---|---|---|
| `find_record(ws, name, descriptor_type=None)` | `dict \| None` | deployer register stage; system skill registration (exact-name lookup) |
| `upsert_record(ws, *, name, description, descriptor_type, descriptors, record_version=None)` | `(record, created: bool)` — **`created=False` on a same-name match; never silently overwrite** | deployer register stage; `POST /api/registry/records`; skill import/reimport |
| `get_record(ws, record_id)` | full record incl. `descriptors` | `GET /api/registry/records/{record_id}` |
| `wait_record_settled(ws, record_id, timeout_s=60, sleeper=time.sleep)` | settled record | deployer register stage — **immediate `get_record`, no local CREATING state** |
| `list_records(ws, descriptor_type=None, status=None)` | `list[dict]` (every status) | `GET /api/registry/records` (publisher view); Overview tiles; `GET /api/registry/attachables` and the zip deployer read `APPROVED` only |
| `list_discoverable_records(ws, descriptor_type=None)` | `list[dict]` (APPROVED only, no `descriptors`) | `GET /api/registry/records/discoverable` — **raises `ValueError` on an unknown type → 422** |
| `update_record(ws, record_id, *, description=None, descriptors=None, record_version=None)` | updated record | `PUT /api/registry/records/{record_id}`; system skill roll-forward |
| `submit_record(ws, record_id)` | updated record → PENDING_APPROVAL | `POST …/action {submit}`; deployer auto-submit |
| `set_record_status(ws, record_id, status, reason)` | updated record | primitive behind approve/disable/reject |
| `approve_record(ws, record_id)` | updated record → APPROVED | `POST …/action {approve\|publish}` |
| `disable_record(ws, record_id)` | updated record → DEPRECATED (terminal; only delete remains) | `POST …/action {disable}` |
| `reject_record(ws, record_id)` | updated record → REJECTED (recoverable) | `POST …/action {reject}` |
| `delete_record(ws, record_id)` | `None` — the row only. The ROUTE then sweeps an AGENT_SKILLS record's `skills/<name>/` prefix (`registry_console.delete_skill_objects`, N16-obs35 fixed in `cn/r17-j`): current objects deleted (a versioned bucket keeps noncurrent versions for its lifecycle rules) unless another record or a live agent still references the prefix (`objects.kept_for` names them); `system-skills/…` is never a target; best-effort (`objects.error` on an S3 failure, the delete still 200). A REFUSED object is such a failure (N17-obs118, `cn/r17-t`): the sweep deletes through the one reporting deleter `services/s3_sweep.py` (`DeleteObjects` batches, `Errors[]` read), so a raised `DeleteObjects` or a key refused inside a 200 answers `objects_deleted: false` with the redacted code (`ClientError: AccessDenied (DeleteObjects)` / `1 of 1 objects refused (AccessDenied)`), never `true` with residue; the registration's orphan cleanup (`skill_ingest._delete_keys`) still never raises (the original failure wins) but logs the residue instead of swallowing it; the reimport's pre-upload sweep logs it and goes on | `DELETE /api/registry/records/{record_id}` → `{deleted, record_id, objects: {objects_deleted, prefix, kept_for?, error?}}` |
| `search_records(ws, query, max_results=20)` | `list[dict]` — hybrid: FTS5 prefix matches ranked by `rank`, then LIKE substring matches appended (de-duplicated, capped at `max_results`); every status of the workspace | `GET /api/registry/records/search?q=` — full-text search |

Every method takes the `WorkspaceContext` it targets first (`ws` above).

## Console list — status tabs and the pending badge (N17-obs267)

User request 2026-10-22 ("可不可以把待审批和已经审批的分一下类，如果多了的话，可能遗漏待审批部分"):
the publisher list (`/v2/registry`) is split by review state into four tabs with counts —
「待审批」 (PENDING_APPROVAL), 「已发布」 (APPROVED), 「草稿 / 已驳回」 (DRAFT + REJECTED) and
「全部」 (every status; DEPRECATED records are listed only here) — selected by the deep-linkable
`?status=pending|published|draft|all` query param. Without the param an administrator lands
on 「待审批」 while anything is pending, otherwise on 「全部」; a member always lands on 「全部」
and the member's 「待审批」 lists the pending records the member can see under the list's
visibility. The tab counts come from the list response itself (`GET /api/registry/records` is
unpaginated), so the page makes no extra request.

The sidebar's 「注册中心」 entry carries the pending count for an administrator (hidden at 0,
never for a member) from `GET /api/registry/records/counts`
(`{pending, approved, draft, rejected, deprecated, total}`, MEMBER, the list's visibility —
`_visible_records` drops a hidden agent's A2A record for a filtering viewer). The route is
one `list_records` call counted in the router: the Protocol has no count primitive and the
isolation filter is by record id, so a grouped SQL count would bypass both; it skips the
per-record system projection and the payload the list route builds. The shell re-reads the
count on every route change and when a registry page reports a change
(`v2/registryPending.notifyRegistryChanged` after approve / reject / submit / disable /
delete / import) — no polling loop.

## Search / FTS fallback

`app/core/db.py::_migrate_registry_search_index` creates `registry_records_fts`
(FTS5, `unicode61`, columns `record_id UNINDEXED, name, description, search_text`) and
the triggers `registry_records_ai/_ad/_au`, backfilling once. When the SQLite build has
no FTS5 module the migration logs
`SQLite has no FTS5 module — registry search falls back to LIKE` and creates nothing;
`LocalRegistryBackend.fts_enabled` (= `fts_index_present(engine)`) is then `False` and
search runs the LIKE leg over `name`, `description`, `search_text` (the flattened
descriptor text `build_search_text` derives on every write).

## No backend registered

`registry_backend.get_backend()` raises `feature.unavailable` (503) only when the slot
is empty — in the CN build it is filled at startup; the ten former
`registry.not_implemented` skeletons are implemented in `routers/registry.py` on top of
`services/registry_console.py`.

## How a node registers a backend

```python
from app.services import registry_backend
registry_backend.set_backend(LocalRegistryBackend(...))  # at app startup (N4)
```

The bootstrap CLI (`make bootstrap` → `scripts/bootstrap.py`) is **not** the API
process, so its slot starts empty: `services/bootstrap.py::ensure_registry_defaults`
brings the ledger up (`init_db()`) and installs `LocalRegistryBackend()` itself when
nothing is registered, then imports the default records (next section). The server
process keeps registering the backend in `create_app` exactly as before.

## Default records — bootstrap import and platform-owned approval (N11 r2-B, N10-bug1)

`registry_console.ensure_default_records(workspace, *, log=None)` is the local build's
gateway → MCP record import (ruling R6). Until N11 round 2 nothing called it except the
undocumented `POST /api/registry/sync-defaults`, so a fresh bootstrap left the registry
empty and the Create-Agent wizard showed "No approved gateway targets" (N10-bug1). Now:

| Trigger | Where | Behaviour |
|---|---|---|
| Hub bootstrap | `services/bootstrap.py::run_bootstrap` — last step, after the gateway, through `ensure_registry_defaults` | best-effort: a failure (gateway not answering yet, S3 denied) is reported in the summary's `registry.defaults.unavailable_reason` and printed with the `sync-defaults` hint; the bootstrap still succeeds |
| Workspace bootstrap | `workspace_bootstrap.py::_stage_registry` — after recording `registry_id` | same degrade: the stage stays green, detail ends `… defaults not imported (<ExcType>)`; otherwise `… N default record(s) · M new · K approved` |
| Console / operator | `POST /api/registry/sync-defaults` | same function; N17-obs257: auto-approves only for an administrator — a member's call submits new target records (PENDING_APPROVAL) and reconciles nothing |
| Sample skill (every trigger above) | `registry_console._ensure_sample_skill` | N17-obs262 (user ruling 2026-10-22), independent of who triggers: a new record is submitted (PENDING_APPROVAL, an administrator approves); on a bundle `version` change an APPROVED platform record is re-approved by `system` (`_approve_platform_target`, platform reason) and stays attachable, one still in review is re-submitted, DEPRECATED is left alone. Its bytes are the repo's `samples/skills/expense-report-writer` files (`upload_skill_bundle` → `skills/expense-report-writer/`), and the record's creator is null, so its PUT / reimport are administrator-only. Only a creator-less row is the platform's (N17-obs256 provenance): a member-authored skill under the name is never refreshed, deduplicated or approved, nothing is uploaded or created over it, and the result carries `name_held: true` |

Idempotent by name: a re-run answers `created: false` for every record and never
touches an existing record's content. Each result is
`{name, type, record_id, created, reconciled, status}`.

**Reconcile on every run (N11 r3-A, N11-r3-bug1).** An environment upgraded from before
r2-B still held its gateway-target records in PENDING_APPROVAL (or DRAFT), and the
import only promoted *new* ones — the live default env kept logging
`office-facts already imported (… · PENDING_APPROVAL)`. Now every bootstrap /
`sync-defaults` run that may approve (bootstrap or an administrator's, N17-obs257) also
walks the **existing** target records and moves a platform-owned one that is still `DRAFT` or `PENDING_APPROVAL` to **APPROVED**
(DRAFT is submitted first; `decidedBy = "system"`, the platform `statusReason`),
reporting `reconciled: true` and logging `… → APPROVED (system-owned, reconciled)`.
*Platform-owned* means the record's MCP server URL is this workspace's `gateway_url`
(`registry_console.is_platform_target_record`); an MCP server a member registered —
even under a target's name — points elsewhere and is left alone (logged `… left
alone`). `APPROVED`, `REJECTED` and `DEPRECATED` records are decisions and are never
touched (a target an admin **disabled** stays DEPRECATED). The second run is a no-op
(`reconciled: false` everywhere).

**Ownership rule.** The records the import creates for the shared gateway's own targets
(`hr-database`, `office-facts`, …) are platform-owned, like system presets: they are
moved DRAFT → PENDING_APPROVAL → **APPROVED** on creation through
`set_record_status`, with `decidedBy = "system"` (`PLATFORM_TARGET_ACTOR`) and
`statusReason = PLATFORM_TARGET_REASON`, so the wizard's `/attachables` and
`/records?status=APPROVED` list them right after bootstrap. Everything a member imports
— `register_mcp_server`, `register_skill*`, the sample skill record the same import
creates — keeps DRAFT → submit → approve. A workspace whose resource map has no
`gateway_url` (a spoke before its gateway stage; its gateway starts empty anyway)
imports no targets and only the skill, without calling `tools/list`.

## Skill bundle writes — publish ownership, prefix semantics, reserved names (N17-obs263 / 264 / 265)

* **Who may rewrite a skill record's bytes.** Every content write onto an existing record —
  `PUT`, `POST …/reimport`, `DELETE` and **Skill Lab publish** — goes through one rule,
  `registry_console.require_record_mutable` (creator-or-admin, N15-obs7): `403
  registry.forbidden {created_by, created_by_name, record_id, action}`, a `null` creator
  (platform sample, Skill Lab samples, administrator imports, legacy rows) is an
  administrator's. Publish (`action: "publish"`) checks the **source record** after the job's
  own creator-or-admin gate and the system-skill refusal, before the idempotence check, the
  multi-file split and any S3 / record write (N17-obs263). Training on a record the caller may
  only read stays allowed; only the publish is refused.
* **A prefix holds exactly one bundle.** Consumers (zip_runtime, `_reupload_and_update`)
  download the whole `skills/<name>/` prefix, so a complete-bundle write must leave no key the
  bundle does not ship. Reimport / bundle replace sweep the prefix before the upload (as
  before); the platform sample upload (`upload_skill_bundle`) prunes every key outside the
  bundle's file list **after** its upload — the prefix stays complete for a deploy reading it
  mid-refresh — and a refused delete raises `S3SweepResidue` (counts and S3 codes only)
  instead of shipping the stale file (N17-obs264). `_update_skill_md` (single-file edit)
  overwrites only `SKILL.md` and keeps the record's file list.
* **Register claims the name before any upload** (N17-obs265). `register_skill_bundle` →
  reserved-name refusals → `_require_new_name` → `upsert_record` (create-if-absent) → upload.
  A record that appeared after the name check (a concurrent register, a defaults sync)
  answers `created=False`: that is `409 registry.name_exists` with nothing uploaded — the other
  record's prefix is untouched and its row is never handed back. A failed upload after the
  claim deletes the uploaded keys and the claimed record. The claimed record is DRAFT while
  its files upload, so no deploy picks it up half-written.
* **A new record never inherits residue** (N17-obs269, `cn/r17-lp`, rev-r23 L2). A refused
  delete or a stray write can leave keys of a deleted same-name record under `skills/<name>/`.
  `register_skill_bundle` used to upload on top of them, so the new record shipped them with
  every deploy that downloads the prefix. After its upload it now prunes the prefix to the
  bundle's files (`_prune_prefix`, keep = the uploaded keys), the obs264 rule. A residue key S3
  refuses to delete fails the registration with `S3SweepResidue` (counts and S3 codes only). It
  is rolled back like a failed upload: the uploaded keys and the claimed record go, and only
  the refused key stays. The batch import reports it as that skill's `registry.import_failed`
  item. The registry bootstrap stage's permission contract now also declares `s3:ListBucket`
  / `s3:DeleteObject` for the prune (`services/workspace_permissions.py`).
* **The refusal copy names publish** (N17-obs269, rev-r23 L1). `registry.forbidden` is also
  the Skill Lab publish refusal, so its message reads "may edit, reimport, publish or delete
  it". The console copy changed to match: 「只有该记录的创建者或管理员才能编辑、重新导入、发布或删除它。」 /
  "Only the record's creator or an administrator may edit, reimport, publish or delete it."
  Tests: `tests/test_skill_publish_integrity.py::
  test_register_prunes_residue_a_deleted_same_name_record_left`,
  `…::test_register_rolls_back_when_the_residue_cannot_be_deleted`,
  `…::test_member_publish_onto_a_record_they_did_not_create_is_refused` (message);
  `frontend/src/v2/pages/registry/registryForbiddenCopy.test.ts`.
* **Reserved names.** A system preset's Skill name answers `409 registry.name_reserved`
  (SE-043); the platform sample skill's name (`SKILL_NAME`, `expense-report-writer`) answers
  `409 registry.sample_name_reserved` for every caller — it is registered by the defaults sync
  (`_ensure_sample_skill`) only. A member record already holding the sample name from before
  the rule is left alone (`name_held`).

## Import Gateway targets — selective gateway → MCP record import (WB-005)

Registry → **Import Gateway targets** (`v2/pages/registry/ImportGatewayDialog`, state machine in
`lib/gateway-import.ts`; the classic twin went with the classic console, r13-a) used to be a button that only navigated to Governance
(bugs-round-5 WB-005). It now opens a dialog over two additive routes
(`routers/registry.py` → `registry_console.gateway_import_preview` /
`import_gateway_targets`); `POST /api/registry/sync-defaults` keeps its contract and its
callers (bootstrap, workspace bootstrap). Shipped in two steps on `cn/r8-f2`: the first
commit's dialog previewed the platform gateway only and ran `sync-defaults` on confirm
(the whole platform import); the second replaced that confirm action with the selective
import below (gateway picker + per-target checkboxes, all ticked by default) once the
user's acceptance criteria required choosing the Gateway and the targets.

| Route | Result |
|---|---|
| `GET /api/registry/import-gateway/preview` | `{gateways[{gateway_id, name, gateway_arn, gateway_url, auth, auth_warning, platform, live_error, targets[{target_id, target, target_status, tools, tool_source, record_id, status, imported, held, held_by}]}]}` — every MCP Gateway of the account/region (the Governance inventory, `GetGateway` + `GetGatewayTarget` detail each), platform gateway first. Read-only. `tools`/`tool_source`: what the import would declare — `live` (the platform gateway's `tools/list`, grouped by the `target___` prefix), `schema` (Governance's `discover_actions` over the target's control-plane schema: Lambda/OpenAPI/static MCP/Smithy/connector) or `none` (a dynamic MCP-server target the live list does not know → the descriptor omits the listing). `record_id`/`status` = the registry record already holding the target's name; `imported` when it is this gateway's own record (server URL = the gateway URL), `held` when another server owns the name; `held_by` (N17-obs250, read-only, `null` unless `held`) = `{record_id, name, status, origin, gateway_id?, gateway_name?}` — `origin: gateway` when the holder's server URL is the `gatewayUrl` of a gateway in this same inventory (no extra AWS call), else `member` (an external MCP server URL). The import's failure item carries the same `held_by`, resolved against the platform gateway (by `resources.gateway_url`, named by its id) and the requested gateway only. Additive since N17-obs261: per target `endpoint`, `endpoint_kind`, `origin`, `will_approve`; per gateway `will_approve`, `member_submissions_to_approve` (see below). |
| `POST /api/registry/import-gateway` `{gateway_id?, target_ids?}` | `{gateway_id, gateway_name, gateway_arn, auth, platform, live_error, results[{name, target, target_id, gateway_id, record_id, created, reconciled, status, tool_source, auth_warning?, error?, error_code?, held_by?}]}` — `gateway_id` defaults to the platform gateway (`resources.gateway_id`, else derived from `gateway_url`; 503 `gateway.not_bootstrapped` when neither exists), `target_ids` omitted = every target; an unknown target id is 404 `registry.gateway_target_not_found` (`detail.target_ids`) **before any write**; an unknown gateway is Governance's 404 `governance.gateway_not_found`. |

Per target the import upserts one MCP record named after the target — unique by name
within the workspace, exactly as `ensure_default_records` does it:

* **Platform gateway** (this workspace's `gateway_url` / `gateway_id`): the same
  semantics as `sync-defaults` to the letter — tools from the live `tools/list`
  (falling back to the control-plane schema when the gateway does not answer; the
  response then carries `live_error`), a new record is APPROVED on the spot by `system`
  (`PLATFORM_TARGET_ACTOR` / `PLATFORM_TARGET_REASON`), an existing record of the
  gateway still in DRAFT / PENDING_APPROVAL is **reconciled** to APPROVED
  (`reconciled: true`), anything else is `created: false` and left alone.
* **Any other Gateway** of the inventory: a member import. The record is created and
  **submitted** (PENDING_APPROVAL) so an administrator approves it — the flow
  `register_mcp_server` and the sample skill follow; the AWS account is not the
  workspace boundary, so nothing a member points the console at is auto-approved.
  Re-imports answer `created: false` with the current status and are never reconciled.
* **Non-IAM/non-NONE authorizer** (`CUSTOM_JWT` → `auth: oauth`, anything else →
  `unmapped`): the record is still created, but every result item carries
  `auth_warning` (the same reason `attachables` reports), the record description ends
  `· outbound auth <auth> · Gateway outbound auth is not mapped …`, and the descriptor's
  `gateway.auth_note` repeats it — so binding shows why the target is not attachable.
* **Name collision**: a name held by a record that is *not* this gateway's (a
  member-registered MCP server, another gateway's target) is a failure item —
  `error_code: registry.name_held`, `error` naming the holder's id and status,
  `record_id` = the holder — never a duplicate, never an overwrite or re-point.

**Descriptor fields (additive).** `registry_backend.build_mcp_descriptors(…, gateway=)`
stores the block verbatim as `descriptors.mcp.gateway`:
`{gateway_id, gateway_arn, auth, runtime_tools[, auth_note]}` — `gateway_arn` is AWS's
own `gatewayArn` (already in the workspace partition; built with
`WorkspaceContext.arn("bedrock-agentcore", "gateway/<id>")` only when a detail lacks
it), `auth` ∈ `aws_iam | none | oauth | unmapped`, `runtime_tools` the tool names the
record declares. `descriptors.mcp.server` / `.tools` are unchanged, so every existing
reader (`_mcp_record_url`, `_declared_tools`, the console's `descriptorExcerpt`) is
untouched; `sync-defaults` records carry no `gateway` block (they predate it) and are
still recognised as platform records by their server URL.

**What an administrator's import approves (N17-obs261, `cn/r17-lp`, rev-r23 N1).** An
administrator's platform-gateway import ticks every target by default, and its reconcile
promotes the PENDING_APPROVAL rows a member's sync / import had only submitted (N17-obs257).
The preview used to show those rows only as "Imported · PENDING_APPROVAL". Now:

* **Endpoint.** Every target row names its `endpoint` / `endpoint_kind`, taken from the target
  detail the inventory already fetched (`_target_endpoint`, no extra AWS call): an MCP-server
  URL, a Lambda function ARN, an API Gateway `restApiId/stage`, or `null` for schema-only
  targets.
* **Origin.** This gateway's own record carries `origin` (`platform_target_origin`): `member`
  when it waits in PENDING_APPROVAL, `platform` otherwise. The platform's own path never
  leaves its record in PENDING: an approving trigger goes straight to APPROVED. So only a
  member's trigger, or a console submit on an older environment, queues one. A member-triggered
  submission now also carries the reason `MEMBER_TRIGGERED_REASON`, visible on the record.
* **Will be approved.** For an administrator (`gateway_import_preview(…, auto_approve=)`, from
  the route's identity) each platform record still DRAFT / PENDING_APPROVAL is
  `will_approve: true`. The gateway carries `will_approve` and `member_submissions_to_approve`
  counts. A member's preview tags nothing, because a member's import never approves.
* **Console.** The dialog adds an Endpoint column and tags the rows 「成员提交」 / "Submitted by a
  member" or 「平台」 / "Platform", plus 「将被批准」 / "Will be approved". Above the table, a
  warning counts the TICKED member submissions: "N record(s) submitted by members will be
  approved by this import." / 「本次导入将批准 N 条由成员提交的记录。」 The default selection is
  unchanged.

Tests: `tests/test_registry_platform_record_guard.py::
test_admin_preview_tags_member_submitted_rows_that_the_import_will_approve`,
`…::test_admin_preview_counts_a_platform_draft_as_will_approve_not_as_a_member_submission`,
`…::test_an_approved_or_held_row_is_never_will_approve`; `lib/gateway-import.test.ts`
(`memberApprovals`), `v2/pages/registry/ImportGatewayDialog.test.tsx` (N17-obs261 block).

Route policy: both routes are `MEMBER` (parity with `sync-defaults` and
`POST /api/registry/records`). Tests: `tests/test_registry_import_gateway.py` (real
`LocalRegistryBackend`, stubbed control plane + `tools/list`); frontend
`lib/gateway-import.test.ts`, `v2/pages/registry/ImportGatewayDialog.test.tsx`,
`pages/registry/ImportGatewayDialog.test.tsx`.

**Agent delete removes the A2A record.** `deployer/registration.py` creates one A2A
record per deployed agent (auto-submitted). `DELETE /api/agents/{id}` now deletes it
(`deployer/teardown.py::_step_registry_record`, N10-bug8) — only when the id on
`Agent.registry_record_id` is an A2A record of the same name; an id that drifted onto
another kind/name is skipped and reported, never deleted. Delete, not DEPRECATED: a
record for a runtime that no longer exists has nothing left to retire.

## A2A consumer path — `attachables.agents` and the `ask_<agent>` tool (N11 r3-C, N10v2-bug5)

Until round 3 an APPROVED A2A record could be *read* (live card) but nothing could
*consume* it: `GET /api/registry/attachables` listed MCP servers and skills only, and the
only consumer in the repo (the front-desk demo) used the AWS Agent Registry's
`SearchDiscoverableRegistryRecords`, which the local registry does not have. The CN
consumer path is now:

| step | where | behaviour |
|---|---|---|
| catalog | `registry_console.attachable_records()` → **`agents`** (`_a2a_attachables`) | every APPROVED `A2A` record of the workspace, joined to the ledger agent whose `registry_record_id` is the record (`_agents_by_record`, same chain as the live-card route). Row: `{name, description, record_id, agent_id, runtime_arn, url, skills[{id,name,description}], attachable, attachability_reason}`. `url` is the stored card's data-plane URL; `runtime_arn` is set only when the owning agent is an **active A2A server of this workspace** — an orphan card (registered by hand), a non-A2A agent or a failed/deleting one is listed with the reason and `attachable: false`, never offered. 60 s cache per workspace as before; `agents` is always present |
| spec | `schemas/agent.py::ToolRef(type="a2a", name=<record name>, config={record_id, runtime_arn, agent_id?, url?, description?})` | `name` must match `^[a-z][a-z0-9-]{0,47}$` (it becomes the `ask_<name>` tool); `runtime_arn` is validated structurally (`arn:<partition>:bedrock-agentcore:<region>:<account>:runtime/<id>`, any partition). `AgentSpec` accepts it for `harness` and `zip_runtime` (protocol `http`) only; `protocol=a2a` (the server template has no tool channel), `code`/`code_bundle` (studio / converted), and `byoc` (already refused: tools without auth) are refused. Legacy specs are untouched — the Literal only grew |
| wizard | `frontend/src/lib/agent-spec.ts::a2aToolRefs`, V2 `AgentsField` (Harness + Strands cards) | the live catalog row wins (current ARN/url/description); a pick the catalog no longer lists re-publishes its stored config verbatim; a pick with neither is dropped (the backend would refuse it). Keys `v2.agents.wizard.{agents,noAgents,agentsHint}` |
| runtime | `templates/a2a_support.py` + `a2a_tools.py.tmpl` | one plain `@tool def ask_<slug>(message: str) -> str` per peer (docstring = generated description, also in `DEFAULT_TOOL_DESCRIPTIONS` for the config-bundle contract; the prompt gains a `## Other agents` section). The tool calls `bedrock-agentcore:InvokeAgentRuntime` on the peer's ARN with the standard A2A JSON-RPC `message/send` envelope — the same one `rt.invoke_a2a_text` sends — signed with the **consumer's execution role** (boto3 default session); `runtimeSessionId = sha256("<consumer session>|<peer arn>")` (stable per consumer session and peer, ≥ 33 chars; the Strands entrypoint binds the session per turn, a Harness falls back to `BedrockAgentCoreContext.get_session_id()`). Errors come back as `[a2a error] <peer>: …` text so the model can tell the user. Strands inlines the source; the Harness bundle writes `launchpad_a2a_tools.py` and grafts on A2 (harness-template.md) |
| IAM | `agent_iam.policy_document` → Sid `A2AInvokeMountedPeers` | `InvokeAgentRuntime` on exactly the mounted runtime ARNs (+ `<arn>/runtime-endpoint/*`); the pre-existing `A2AInvokePeerRuntimes` (`runtime/*`) stays reserved for A2A *servers* |

**Why a minimal client and not `strands_tools.a2a_client`.** The pinned
`strands-agents-tools==0.8.3` does ship `A2AClientToolProvider`, but it resolves the agent
card and sends messages over plain httpx to a URL. Every Launchpad runtime is AWS_IAM-inbound
(D9) and only reachable through the signed data-plane API, so that provider cannot talk to a
peer here; the template's client (≈100 lines, no new dependency — boto3 is already a
platform pin) is the whole integration. The front-desk sample (`backend/samples/frontdesk_agent`)
still discovers through the AWS registry client and remains a Global-only demo (not in this
round's scope).

## Contract notes (from the Task 5 review, binding on N4 — dispositions below)

- Register via `registry_backend.set_backend()` at startup ✅ done in
  `app/main.py::create_app`; `agentcore/client.py` registry factories stay gone, but
  `services/registry_console.py` is **not** gone — N4 rebuilt it, Protocol-backed (no
  `boto3` registry client, ruling R7), and the ten former `registry.not_implemented`
  skeleton bodies in `routers/registry.py` call into it.
- `"created=False"` … ✅ `LocalRegistryBackend.upsert_record` (create-if-absent; never
  overwrites).
- `wait_record_settled` ✅ immediate `get_record`.
- `list_discoverable_records` ✅ `ValueError` → 422.
- Statuses ✅ (table above).
- Restore tests ✅ `test_registry.py`,
  `test_registry_{manage,update,reimport,skill_ingest,live_agent_card,attachables,search,local}.py`,
  `test_system_skill_registry.py`, `test_frontdesk_sample.py` (the sample's pure helpers;
  the `/api/registry/a2a-demo` route and its tests were removed in R13, N13-obs6).
- `data_plane_invocations_url` percent-encoding ✅ preserved (covered by
  `test_registry.py::test_a2a_card_data_plane_url_is_partition_aware_and_percent_encoded`).
- AWS-shaped leftovers ✅ removed: `skill_registry` `ClientError` arms and GA-shaped
  `create_request`; `to_ga_descriptors`/`from_ga_descriptors`/`normalize_record`/
  `wrap_descriptors_for_update` deleted; `set_record_status` is now the state-machine
  primitive.
- Not restored (ruling R6): the per-Gateway governance
  `registry-preview/import/retire` surface — `sync-defaults` is the gateway → MCP
  record import; `disable` is retirement.
