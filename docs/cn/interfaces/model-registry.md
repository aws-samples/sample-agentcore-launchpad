# Interface: model registry (D21 · N15)

Frozen 2026-10-14 by node **M1-a** (plan `docs/superpowers/plans/2026-10-14-d21-model-providers.md`
§2, §3, §5.1–5.4, §9). Supersedes [model-providers.md](model-providers.md) §1 and §4 (the two
module tables and "adding a provider by code"); that file's key flow (§2) and refusal codes (§3)
remain valid and are referenced below rather than repeated.

**One sentence:** the model catalog is a ledger registry of OWNER-BOUND providers (N17-obs203,
user decision 2026-10-21 — every member registers their own, administrators share and curate;
§4.1) published as an immutable in-process snapshot; `backend/app/schemas/agent.py` exposes it only through
**function views** (`allowed_model_ids()`, `providers()`, `provider_row()`,
`model_catalog_table()`, `model_capabilities()`, `model_default_max_tokens()`,
`model_catalog_entries()`, `model_provider_table()`, …) — the pre-D21 module constants were
removed by M1-e (C5, §3) — and an empty registry is seeded from today's built-in tables so
the pre-D21 build's behaviour is reproduced byte-for-byte (gate G2). **R13 (N13-obs7, user
decision 2026-10-17): the seed is Global Bedrock + Titan v2 only** — the optional SiliconFlow
provider and its four chat rows left the built-in tables; an administrator adds SiliconFlow (or
any OpenAI-compatible endpoint) with `POST /api/settings/model-providers` naming the SSM
parameter of its key (§4), and a ledger seeded before R13 keeps its rows (the seed runs once).

## 1. Tables (`backend/app/models/model_registry.py`)

| table | scope | columns (names only — never key material) |
|---|---|---|
| `model_providers` | hub-global | `id` (`mp_<hex12>`), `name` unique `^[a-z][a-z0-9-]{1,31}$` (= the former `PROVIDERS` key: `bedrock`; `siliconflow` when an administrator adds it), `kind` (`bedrock` \| `openai_compatible`), `label`, `base_url` (null for `bedrock`; https, host-allowlisted), `model_region` (bedrock), `credential_provider` (Identity name, derived `launchpad-<name>`), `credential_ssm_parameter` (`launchpad-<name>-api-key`, editable within the pattern), `credential_source` (`ssm` \| `host_env`), `credential_env` (`LAUNCHPAD_<NAME>_API_KEY`), `required`, `builtin`, `enabled`, `id_prefix` (`""` for the built-in Bedrock row, `<name>.` otherwise), `probe` JSON `{state: never\|ok\|failed, checked_at, latency_ms, models_seen[≤200], error}`, `created_by/at`, `updated_by/at` |
| `models` | hub-global | `id` (`md_<hex12>`), `provider_id` FK, `model_id` unique (Launchpad id = `id_prefix + api_model_name`), `api_model_name`, `label`, `kind` (`chat` \| `embedding`), `enabled`, `is_default` (≤ 1 per kind), `tools`, `streaming`, `vision`, `inference_params` `{temperature, top_p}`, `default_max_tokens`, `max_output_tokens`, `default_judge_max_tokens`, `embedding_dimension`, `embedding_max_chars`, `source` (`builtin` \| `probed` \| `manual`), `capability_sources` `{field: builtin\|probed\|manual\|unknown}` for the nine tracked fields (`tools`, `streaming`, `vision`, `temperature`, `top_p`, `default_max_tokens`, `max_output_tokens`, `default_judge_max_tokens`, `embedding_dimension`), `probe` JSON (plan §7.4), `sort_order`, audit columns |
| `model_catalog_changes` | hub-global | `id` AUTOINCREMENT (**= catalog version**), `operation` (`seed`, `provider.create\|update\|delete\|credential\|probe`, `model.create\|update\|delete\|probe`, `workspace.flags`), `actor`, `target_id`, `before`, `after`, `note`, `created_at` — immutable, never pruned |
| `workspace_model_settings` | **workspace-scoped** (`WORKSPACE_SCOPED_TABLES`) | `workspace_id`, `model_id`, `enabled`; unique `(workspace_id, model_id)`; absent row = enabled, only `false` rows are written |

Migration: `core/db.py::_migrate_model_registry_indexes` restores the unique flag index and the
`models(kind, enabled)` / `models(provider_id)` / `model_catalog_changes(created_at)` indexes
after the workspace-column rehearsal; `_migrate_workspace_columns` gained the
`workspace_model_settings` ALTER. `app.models.model_registry` is in `MODEL_MODULES`.

## 2. Snapshot (`backend/app/schemas/model_catalog.py`, pure — no DB import)

```python
ProviderRow(name, kind, label, base_url, model_region, credential_provider, ssm_parameter,
            api_key_env, required, enabled, id_prefix, id, builtin, credential_source, probe_state)
ModelRow(model_id, provider, provider_kind, kind, label, api_model_name, base_url,
         credential_provider, tools, streaming, vision, temperature, top_p, default_max_tokens,
         max_output_tokens, default_judge_max_tokens, embedding_dimension, embedding_max_chars,
         enabled, default, source, id, sort_order)
Catalog(version, providers: {name: ProviderRow}, models: {model_id: ModelRow},  # insertion = picker order
        workspace_disabled: {workspace_id: frozenset[model_id]})
  .chat_models() .embedding_models() .allowed_ids() .embedding_model() .default_chat_model()
  .model_enabled(model_id, workspace_id=None) .legacy_providers() .legacy_models() …
BUILTIN_CATALOG = builtin_catalog()   # build_model_catalog() (Bedrock rows) + Titan v2, version 0 — no SiliconFlow since R13
current() / publish(c) / set_refresher(fn) / force_refresh() / reset()   # the holder
extended(c, providers=…, models=…)   # a variant snapshot (tests / tooling; M1-e, replaces the overlay proxies)
```

* `ModelRow.kind` is `chat | embedding`; `provider_kind` is what the **legacy** row calls
  `kind`. `ModelRow.legacy()` / `ProviderRow.legacy()` reproduce today's dict shapes exactly
  (`LEGACY_MODEL_KEYS`, `LEGACY_PROVIDER_KEYS` — pinned by the parity test) and are cached per
  snapshot so `PROVIDERS["bedrock"] is PROVIDERS["bedrock"]` holds.
* `ModelRow.descriptor(version)` is the job-payload descriptor M1-d hands to workers:
  `{model_id, kind, model_kind, base_url, api_model_name, credential_provider, streaming,
  inference_params, judge_max_tokens, default_max_tokens, max_output_tokens, catalog_version}`;
  `schemas.agent.model_descriptor(model_id)` returns it.
* The built-in embedding row is `amazon.titan-embed-text-v2:0` (dimension 1024, max chars
  8000 — `services/opensearch_http.EMBED_*`), provider `bedrock`, `is_default`.

## 3. Resolution order, caching, invalidation (plan §5.1–5.4)

1. **Registry rows** — once seeded, `services/model_registry.load_catalog(db)` builds the
   `Catalog` (version = `max(model_catalog_changes.id)`) and `publish()`es it.
2. **Built-in defaults** — only while the `models` table is empty (pre-seed, or a test that
   never builds the app): the holder starts as `BUILTIN_CATALOG`.

`app/main.py` calls `model_registry.ensure_seeded()` after `init_db()` (inserts the built-in
rows with `source: builtin`, one `seed` journal row; idempotent — a COUNT when seeded) and
`install_refresher()`. Every registry write `refresh(db)`es in the same request; the holder's
`current()` additionally runs the installed refresher at most every `REFRESH_TTL_S` = 30 s
(one `SELECT max(id) FROM model_catalog_changes` — `content_stamp()` vs the published
`revision`, N17-obs281: the version is not compared as well, because `stamp()` is the max over
a subset of the same append-only rows and an equal revision implies an equal version — covers a
second process such as a `make dev` reload) and is fail-soft (a failing stamp check keeps the snapshot and
logs). Cost per `assert_model_*`: a dict lookup, as before.

### `schemas/agent.py` after D21

| name | now |
|---|---|
| `PROVIDERS`, `MODEL_CATALOG`, `MODEL_CAPABILITIES`, `MODEL_DEFAULT_MAX_TOKENS`, `ALLOWED_MODEL_IDS` | **Removed (M1-e, C5).** M1-a kept them as overlay-capable proxies for one release; every reader now calls the function views `providers()` / `provider_row(name)` / `model_catalog_table()` / `model_capabilities()` / `model_default_max_tokens()` / `allowed_model_ids()` (the snapshot's cached legacy dicts — identity stable per snapshot, read-only). A test that used to monkeypatch a constant publishes a variant snapshot instead: `model_catalog.publish(model_catalog.extended(model_catalog.current(), models={...}))` and `reset()`s afterwards |
| `providers()`, `catalog_version()`, `model_row(id)`, `model_descriptor(id)`, `active_embedding()`, `model_enabled(id, workspace_id=None)` | new functions over the snapshot |
| `model_available(id, resources, workspace_id=None)` | `provider configured ∧ model.enabled ∧ provider.enabled ∧ workspace flag ≠ false`; `provider_configured()` keeps its pure credential semantics (`enabled` is reported separately) |
| `assert_model_provider_configured` / `assert_model_available` / `assert_agent_model_available` / `assert_spec_models_available` | `+ workspace_id=None`; a **disabled** row is refused with the historical `model.not_allowed` code ("disabled in the model registry … Settings → Model Providers"); stored specs keep parsing (`ALLOWED_MODEL_IDS` lists disabled chat rows too) |
| `model_catalog_entries(resources, workspace_id=None)` / `model_catalog_lines(…)` | **enabled chat rows only** (a disabled row is omitted, like a row the server no longer lists); additive keys `kind`, `source`, `enabled`; the entry's `default` key follows the registry's default row; prose byte-identical for built-ins, except the " (default)" tag of a line, which marks the effective `agent_default` role model (§12.1, N17-obs298) |
| `model_provider_table()`, `judge_budget_defaults()`, `inference_param_support()`, … | unchanged shapes (templates render them byte-for-byte; M1-d adds `MODEL_CATALOG_VERSION` from `catalog_version()`) |
| `build_model_catalog(seed_spec="", provider_row=None)`, `parse_provider_models`, `openai_compatible_entry`, the `DEFAULT_*` constants (`DEFAULT_SILICONFLOW_MODELS` removed in R13) | moved to `schemas/model_catalog.py`, re-exported; the override grammar survives for the seed only |

The pre-D21 `Settings.siliconflow_models` / `LAUNCHPAD_SILICONFLOW_MODELS` override **no
longer exists** (M1-e, C5), and since R13 (N13-obs7) **neither does a SiliconFlow seed**: the
`DEFAULT_SILICONFLOW_MODELS` literal and the `after.siliconflow_seed` journal field are gone, the
environment variable is inert, and administrators register the provider and its rows on the
page (the spec grammar `api_model_name[:trait+…]` survives for tooling and tests — see
`backend/tests/model_fixtures.py`, which registers the former seed rows the admin way). **N17-obs203
(user decision 2026-10-21): the operator host allowlist is no longer a precondition.**
`Settings.model_provider_allowed_hosts` now defaults to `[]` — empty = any public https host is
accepted (the SSRF guard of §7.5 still runs before every request); a non-empty list is the
operator's **lock mode** and keeps restricting `base_url` to the listed hosts.
`Settings.model_provider_denied_hosts` (default `[]`, exact or `*.suffix`) is a denylist checked
always. Both are yaml / env, never widened from the console (§5.3).

## 4. API — `/api/settings/model-providers` (`routers/model_providers.py`; hub-global; **member CRUD on owned rows, two `ADMIN` routes** — §4.1)

### 4.1 Owner-bound providers (N17-obs203, r17-mp, user decision 2026-10-21)

"launchpad 和模型相对独立：无论管理员还是普通成员都可以增加、删除、修改自己加的模型提供方（管理员可以修改或删除普通成员的）；API key 直接在界面里输入。"

* **Columns.** `model_providers.created_by` is the creating account's `users.id` (the Family B
  ownership key — `services/creators.py`; NULL for the seed, the config admin and every
  pre-obs203 row = an administrator's), `created_by_name` the username at creation (display),
  `scope` = `personal` | `workspace`. A pre-obs203 ledger carried the journal ACTOR (a username)
  in `created_by`: `core/db._migrate_creator_columns` re-points it once like the other legacy
  tables (`system` / `bootstrap` record no creator) and the additive `scope` column defaults every
  existing row to `workspace`. The snapshot's `ProviderRow` carries `owner_id` / `scope` (not part
  of the legacy row).
* **Who sees what** (`model_registry.provider_visible`): administrators every row; a member every
  `workspace`-scope row and their OWN `personal` rows — never another member's personal provider.
  `GET /` is narrowed accordingly (providers, their models, their probe warnings; `workspaces` reads
  `[]` for a member — the flag matrix is an administrator's view); a hidden `provider_id` answers
  the kind's `404 model_provider.not_found` on EVERY route, indistinguishable from a missing id.
  Hub-global, so identical under `shared` and `isolated` workspaces.
* **Who changes what** (`require_provider_mutable` = `services/ownership.require_creator_or_admin`):
  the creator or an administrator — `PUT` / `DELETE /{id}`, `/credential`, `/probe`, every
  `/models*` write. Anyone else answers `403 model_provider.forbidden {provider_id, action,
  created_by, created_by_name}` AFTER the visibility 404 and BEFORE the body is validated (the
  gate is a FastAPI dependency, so a forbidden caller never learns a request schema — the
  D21-obs8 precedent). A NULL creator (the built-in Bedrock row, legacy rows) is an
  administrator's. Members cannot move the platform default (`is_default` → 403 `action:
  is_default`). **Credential fields of a shared row are an administrator's (N17-obs225, rev-r20
  B1):** on a `workspace`-scope row `base_url`, `credential_ssm_parameter` (the Advanced path)
  and the key — typed on `PUT` / `POST /credential {api_key}` or read from the parameter by a
  body-less `POST /credential` — may be changed by an administrator only, whoever the creator
  is (`model_registry.require_admin_for_credential_fields`, after the two gates and before any
  write or probe: 403 `model_provider.forbidden {action: base_url | credential_ssm_parameter |
  api_key | credential, scope: workspace}`); the creator keeps `label` / `enabled` /
  `model_region` and the models. An unchanged echo of `base_url` / the parameter (the edit
  drawer sends every `ProviderUpdate` key) is not a change. Without this a member whose row an
  administrator shared (or a legacy re-pointed creator who is no longer an administrator) could
  repoint the shared row at the operator's parameter and their own host, register the
  operator's key for the row's Identity provider, probe it against that host and overwrite the
  operator's parameter with a typed key — `tests/test_model_provider_keys.py::
  test_a_shared_member_row_cannot_be_repointed_at_the_operators_key` replays the chain and
  asserts 403 at every step.
* **Scope.** `ProviderCreate.scope` / `ProviderUpdate.scope` ∈ `personal | workspace`. A member's
  create is `personal` (omitted or explicit; `workspace` → 403 `{scope, action: scope}`); an
  administrator's defaults to `workspace`. Flipping a scope (sharing / unsharing) is an
  administrator's `PUT`. **Sharing transfers ownership (N17-obs225):** flipping `personal →
  workspace` sets `created_by` / `created_by_name` to NULL — the row is the workspace's (an
  administrator's, like the built-in row), the former creator reads it like every member and
  loses every mutation right; the `provider.update` journal row's `note` names the former
  creator (`ownership transferred to the workspace on sharing (former creator: <name> / <id>)`).
  Unsharing afterwards leaves an administrator's personal row (the former creator no longer
  sees it).
* **Owner-scoped credential references.** A personal provider's SSM parameter is
  `launchpad-user-<users.id>-<name>-api-key` and its Identity provider
  `launchpad-user-<users.id>-<name>` (`model_registry.owner_scoped_*`; ≤ 80 chars of `[a-z0-9-]`,
  within the ops role's `launchpad-*-api-key` grant and Identity's name rule); `credential_env`
  is NULL (no host-env fallback). `credential_ssm_parameter` is NOT client-settable for a personal
  row — a different name is 422 `model_provider.invalid_ssm_parameter {expected}` on create and on
  update — because a chosen name (`launchpad-global-bedrock-api-key`) or a chosen env variable
  (`LAUNCHPAD_GLOBAL_BEDROCK_API_KEY` from a provider named `global-bedrock`) would let a member
  send someone else's key to their own endpoint. Workspace-scope rows keep the D21 rules.
* **Response.** `ProviderOut` gains `created_by_name`, `scope`, `owned` (the CALLER's account
  created the row; an administrator owns only what they created). `name` stays unique hub-wide
  (it is the Launchpad id prefix) — a second member picking a taken name gets 409
  `model_provider.exists` and still cannot see whose it is.
* **Catalog version.** A personal provider's changes never advance `stamp()` / the catalog
  version; sharing (or unsharing) bumps once; the snapshot `revision` keeps every row for the
  second-process refresher — §5.4.
* **Usage scoping (§6 of the decision).** `schemas/model_catalog.Principal(user_id, is_admin)`
  is who a catalog read is for: `Catalog.model_visible(model_id, principal)` /
  `visible_model_ids` = the workspace-scope providers' models + the principal's own personal
  providers' models (administrators: everything; `WORKSPACE_ONLY` = the shared catalog alone;
  `None` = every row — workers and the deploy-job re-validation). Applied at: `GET
  /api/capabilities` (`models[]` and `model_providers{}` for the caller — every console picker,
  the agent wizard and the eval judge / actor selectors read it, so another member's personal
  provider never shows anywhere); `schemas.agent.model_catalog_entries` / `model_catalog_lines`
  (`principal=`) → the architect's protocol preamble and proposal validation use the
  conversation OWNER's principal (`assistant.service.owner_model_principal`; the approver's on
  approve; the deploy-job re-validation stays unrestricted) and the preset bundles render
  `WORKSPACE_ONLY`; agent create / redeploy — `assert_spec_models_available(…, principal)`
  refuses a model of another member's personal provider FIRST with **422
  `agent.model_not_visible {model_id}`** (the owner or an administrator may still deploy it);
  an evaluation actor — 422 `run.model_not_visible`; a platform role (§12) accepts
  workspace-scope rows only — 422 `models.model_not_allowed` reason `personal_scope`, and the
  roles tab lists shared providers' models only. **Hub-side USE sites (N17-obs227, rev-r20):**
  wherever the HUB spends a provider's key on a member's behalf the named model is held to the
  caller's principal first, 422 **`models.model_not_visible {model_id}`** otherwise
  (`schemas.agent.hub_model_refusal_code`): a judge / derived evaluator's `model_id` on
  `POST` / `PUT /api/eval/evaluators` (`evaluators_local._require_model(…, principal)`), the
  judges of an assistant evaluation plan at approval (`evaluation_assets._require_judge_models`
  on the APPROVER's principal — `approve_plan(approver_is_admin=…)`), a recommendation's
  reflection `model_id` (`recommendations._model_for(…, principal)` from `POST
  /runs/{id}/recommendations`) and the hub judge builder `aws_clients.build_chat_model(…,
  principal=)` (None = a worker / in-process caller without a request — its ids were held to
  the caller's visibility where they were named). A member naming another member's guessable
  `<provider>.<model>` can therefore not make the hub spend that member's key. Tests:
  `tests/test_model_provider_visibility.py`.
* **Route policy.** 11 routes `MEMBER` (the handler adds the two gates), `GET /changes` and
  `PUT /workspaces/{id}/models` `ADMIN`; still no member-grantable `models.manage`.
* **Console.** The page is every member's. Its sidebar entry stays the last item of the Configuration group (user decision 2026-10-21; it sat in the platform-services group briefly, N17-obs224) without the admin flag, so a member sees the Configuration group with this one entry while workspaces / users remain administrators-only. Provider cards carry a scope badge
  (「个人 / Personal」·「我的 / mine」, 「工作区 / Workspace」) and the creator; cards and model
  tables of rows the member cannot change are read-only (`useMayMutate`, the server's rule
  mirrored); the add / edit drawer shows the scope picker to administrators only and, for a
  personal row, the owner-derived SSM name read-only; the roles / workspaces / changes tabs are
  administrators'.
* **Tests.** `tests/test_model_provider_ownership.py` (creation + derived references, the hijack
  refusals, 404-before-403 in both modes, sharing / unsharing, the NULL-creator rule, owner
  writes + the admin-only journal, the legacy re-point, the seed), `test_model_providers_api.py::
  test_member_reach_anonymous_401_and_the_admin_only_remainder`, `test_route_policy.py`,
  `ModelProviders.test.tsx` "owner-bound providers".

Contract pairing: **cn/m1-c a143c8cf, `api.ts` `MODEL_PROVIDERS_BASE`** (= this prefix) + rev-r8
fix list M1–M8. Schemas: `backend/app/schemas/model_providers.py` — the frozen contract the
console codes against; `tests/test_model_providers_api.py::test_the_pydantic_contract_pins_every_response_key_set`
pins every key set. Request bodies forbid unknown keys (422 `validation.invalid_request`; the
provider `PUT` names the offending keys as 422 `model_provider.invalid_update` — D21-obs8).

| method / path | body → response | notes / codes |
|---|---|---|
| `GET /` | → `RegistryOut {catalog_version, providers: [ProviderOut], models: [ModelOut], workspaces: [{id, name, disabled_models[]}], warnings: [{code, message, detail}]}` | one read for the page; models include the embedding rows; warnings: `probe_failed` (N17-obs205: a DISABLED provider without a configured credential does not raise it — its stale probe stays on its own card), `preset_snapshot_behind` (M1-d); the M1-a `siliconflow_models_ignored` code was removed with the override (M1-e, C5) |
| `POST /` | `ProviderCreate {name, kind, label, base_url?, model_region?, credential_ssm_parameter?, enabled=true, scope?}` → `ProviderOut` 201 (§4.1: a member's row is `personal`, `scope: workspace` → 403 `model_provider.forbidden`) | 409 `model_provider.exists`; 422 `model_provider.invalid_name` / `invalid_kind` / `invalid_base_url` (http, IP literal, userinfo, missing) / `host_not_allowed` (only under the operator's lock-mode allowlist or denylist — N17-obs203; `detail {host, setting, mode: lock\|deny}`) / `invalid_ssm_parameter` / `invalid_model_region`; touches neither SSM nor Identity (`configured` false until `/credential`) |
| `GET /{provider_id}` | → `ProviderOut` | `provider_id` = row id or `name`; 404 `model_provider.not_found` |
| `PUT /{provider_id}` | `ProviderUpdate {label?, base_url?, model_region?, credential_ssm_parameter?, enabled?, scope?}` → `ProviderOut` (§4.1: creator-or-admin; `scope` administrators only) | `name`/`kind` immutable — a body carrying them (or any key outside `ProviderUpdate`) → 422 `model_provider.invalid_update {fields, immutable, allowed}` (D21-obs8; before cn/r10-b this was the generic `validation.invalid_request`). The console's `providerBody()` / edit drawer send exactly the five `ProviderUpdate` keys (`model_region: null`, never `""`, for an OpenAI-compatible row) — pinned by `test_model_providers_contract.py::test_console_provider_update_payload_validates_and_name_kind_do_not` (`CONSOLE_PROVIDER_UPDATE_KEYS`) and `ModelProviders.test.tsx`. Disabling the required Bedrock row while it hosts a default model or is the only configured provider → 409 `model_provider.required`. **N17-obs228:** a `base_url` change on a row that holds a registered key (`key_version` / `key_last4`) needs `api_key` in the same body (probed against the NEW host first) — otherwise the stored key is cleared (console-written SSM parameter deleted, operator-created kept; Identity provider deleted; hub / spoke record `configured: false` + `cleared_on_host_change`; cache dropped — all best-effort) and the row reads 「未配置」 until a key is entered again; the `provider.update` journal note names the old and the new host and the residue either way; an unchanged echo of the host (trailing slash) is not a change; administrators only on a workspace-scope row (N17-obs225) |
| `DELETE /{provider_id}` | → 204 | 409 `model_provider.builtin` — **the hub Global Bedrock row only** (N17-obs207: the legacy `siliconflow` seed is un-pinned once at startup by the ledger migration `core/db._migrate_legacy_preset_providers` (`init_db`, idempotent — N17-obs282; never on a read), no journal row, and deletes like any provider); 409 `model_provider.in_use` `{referrers, agents: [{id, name, workspace_id, system_key}], roles: [{role, field, model_ids}]}` (non-deleted agent specs incl. system presets, platform-role overrides, evaluators) — the console's dialog names the roles and presets; otherwise cascades its models + flags and (§8.1) forgets a console-written SSM parameter, drops the hub key cache for the name and flips the hub / spoke `model_credentials[<name>]` record to `configured: false` (N17-obs226 / obs229); the Identity provider is **not** deleted (rotation history). Creator-or-admin (§4.1) |
| `POST /{provider_id}/credential` | `{}` or `{api_key}` (§8.1, write-only) → `CredentialOut {configured, created, rotated, workspaces_updated, key_version, last4}` | **M1-b** (§8): reads the row's SSM parameter, creates / rotates the Identity provider, records `model_credentials[<name>]` on the hub config + every workspace row; 422 `model_provider.ssm_parameter_missing`, 502 `model_provider.ssm_read_failed` / `model_provider.identity_failed`; 501 `model_providers.credential_unavailable` only in a build without the hooks (`isProbeUnavailable` recognises it next to `model_providers.probe_unavailable`) |
| `POST /{provider_id}/probe` | → `ProviderProbeOut {state, checked_at, latency_ms, models_seen[], error}` | **M1-b** (§7.1); stored into `probe`, journaled `provider.probe`; 501 `model_providers.probe_unavailable` only without the hooks |
| `POST /{provider_id}/models` | `ModelCreate {api_model_name, kind=chat, label?, probe=true, overrides?: ModelCapabilityOverrides, enabled=true}` → `ModelOut` 201 | Launchpad id = `id_prefix + api_model_name` (409 `model.exists`); `probe: true` (default) runs M1-b's probe **before** the insert (`source: probed`, per-field `capability_sources`; 422 `model.probe_failed {findings, errors}` when the model is missing or `exists` could not be settled, §7.4); `overrides` apply after (`manual`); `probe: false` = kind defaults as `manual` (chat: tools/streaming on, 16384/4096 bedrock or 8192/4096 OpenAI-compatible; embedding: needs `embedding_dimension` → 422 `model.invalid`) |
| `PUT /{provider_id}/models/{model_ref}` | `ModelUpdate {label?, enabled?, is_default?, sort_order?, + capability fields}` → `ModelOut` | `model_ref` = row id **or** Launchpad id (slashes allowed — `:path` converter); every capability field sent flips its `capability_sources[field]` to `manual` and the row to `manual`; `is_default` (chat) needs enabled + tools + hub-configured provider else 422 `model.default_invalid`; disabling / un-defaulting the default → 409 `model.is_default`; embedding default/disable consults M1-d's guard (`set_embedding_guard`) |
| `DELETE /{provider_id}/models/{model_ref}` | → 204 | 409 `model.builtin` (seeded rows: disable instead), 409 `model.is_default`, 409 `model.in_use` `{referrers, suggest: "disable"}` |
| `POST /{provider_id}/models/{model_ref}/probe` | `{apply=false}` → `ModelProbeOut {findings, diff: {field: {stored, probed, source}}, errors, raw_hint, latency_ms, applied: bool}` | **M1-b** (§7.4): one `diff` entry per settled capability field; `apply: true` writes only fields whose `capability_sources[field] != "manual"`, stores `probe`, journals `model.probe`; `applied` = at least one field written. The re-probe route never 422s — a missing model shows as `findings.exists: false` + `errors.exists` in the diff |
| `PUT /workspaces/{workspace_id}/models` | `{disabled_models: [model_id…]}` → `{id, name, disabled_models, workspace_id}` (the `workspaces[]` element + `workspace_id`) | replace semantics; 404 `workspace.not_found`, 422 `model.unknown` |
| `GET /changes?limit&before&target_id` | → `{items: [ChangeOut {id, operation, actor, target_id, before, after, created_at}]}` | newest first; `before` pages by id; `target_id` (N17-obs203 §4) = one provider's audit trail — every provider create / update (label, host = `base_url`, scope flip) / key (`provider.key`, `provider.credential`) / delete is one row with the console user, the row, the action and the time, never key material; the console shows it as the 「变更记录 / Changes」 card under the selected provider (administrators) |

`ProviderOut` carries the credential references in ONE flat shape (`credential_provider`,
`credential_ssm_parameter`, `credential_source`, `credential_env`, `configured`, `key_version`,
`key_modified_at`, `key_last4` — the same names the request bodies use; N17-obs283 dropped the
nested `credential {provider, ssm_parameter, source, env, configured, last4}` mirror of rev-r8 M1,
whose only extra fact moved to `key_last4`), plus the N17-obs203 owner keys `created_by` (users.id), `created_by_name`, `scope`,
`owned` — names / version facts only; `configured` is whether the **hub** holds the key (`ProviderRow.hub_holds_key()`, §8.2: a registered / console-typed key on the row, the required / Bedrock row, or the exported host variable — the same answer as `provider_configured(name, None)`; per-workspace configured-ness stays on the workspace resource maps and is what `/api/capabilities` reports).
Timestamps are ISO-8601 strings.

**Referrers** (409 `*.in_use`): non-deleted agents (`spec.model_id`, BYOC `allowed_models`,
Studio node models), `eval_evaluators.model_id`, and the hub Settings
`eval_judge_model_id` / `eval_actor_model_id` / `prompt_opt_default_model_id` /
`prompt_opt_models` — each as `{kind: agent|evaluator|setting, type (= kind), id, name, workspace_id, model_ids}`.

**Plug-in slots** (`services/model_registry.py`): `set_probe_hooks(hooks)` — implemented by
`services/model_probe.ModelProbeHooks` (registered in `app/main.py` right after
`install_refresher()`): `probe_provider(db, provider, actor)`, `register_credential(db, provider, actor)`,
`probe_model(db, provider, api_model_name, kind, model|None, *, apply, actor)` (returns
`{findings, errors, record}` before an insert, `{findings, diff, errors, raw_hint, latency_ms, applied}` for a re-probe — §7);
`set_embedding_guard(fn)` — M1-d's reindex guard raising 409 `embedding.reindex_required`.

### `GET /api/capabilities` (workspace-scoped, every role)

Every pre-D21 key is byte-identical (parity test against the 58e44a50 capture). Additive:
top-level `catalog_version` and `embedding: {model_id, dimension, provider} | null`;
`models[].{kind, source, enabled}` (chat rows only; a row disabled by model, provider or **this
workspace's flag** is omitted); `model_providers.*.{id, enabled, probe_state}`.

## 5. Security (plan §9)

1. **Superseded by §4.1 (N17-obs203):** the CRUD routes are `MEMBER` in `core/route_policy.py`
   with the owner gates in the handler (visibility 404 → creator-or-admin 403); `GET /changes`
   and the per-workspace flags stay `ADMIN` (`/api/settings` is a hub-global prefix;
   `test_route_policy.py` pins the 13 entries); still no member-grantable `models.manage`.
2. No key at rest, in the journal or in any response: the tables and `ProviderOut` carry
   names (`launchpad-<name>`, `launchpad-<name>-api-key`, `LAUNCHPAD_<NAME>_API_KEY`); the
   contract has no field a key could travel in; tests plant a key in the environment and scan
   every response and journal row.
3. `base_url`: https only, a hostname (no IP literal, no userinfo, no query/fragment) → else
   422 `model_provider.invalid_base_url`. **N17-obs203 (user decision 2026-10-21):** the
   operator allowlist `Settings.model_provider_allowed_hosts` is **no longer a precondition** —
   with the new default `[]` any public https host passes this write-time check and the
   per-request SSRF guard of §7.5 is the boundary (hostname resolved before every request;
   loopback / private / link-local / IMDS / internal names refused). A **non-empty** list is
   the operator's *lock mode* and still restricts to the listed hosts (exact or `*.suffix`);
   `Settings.model_provider_denied_hosts` (exact or `*.suffix`) is checked **always**. Either
   refusal is 422 `model_provider.host_not_allowed` with `detail {host, setting:
   model_provider_allowed_hosts | model_provider_denied_hosts, mode: lock | deny}`. Both lists
   are yaml / env only — the console cannot widen or shrink them.
4. Audit: every write is one `model_catalog_changes` row with actor + full before/after
   (N17-obs203 §4: this is the audit trail of the owner-bound providers too — a member's create
   / edit / host change / key entry and an administrator's scope flip or delete each leave one
   row naming the console user; `GET /changes?target_id=` reads one provider's trail;
   `tests/test_model_provider_audit.py` pins the row per operation and that no key material is
   ever in it).
   **Not every row is a catalog version** (D21-obs5, D21-obs6): `provider.credential`,
   `provider.probe` and `model.probe` rows are audit-only — listed by `/changes`, excluded
   from `stamp()` / `Catalog.version` / `GET /api/capabilities.catalog_version`
   (`model_registry.AUDIT_ONLY_OPERATIONS`), because a key registration or a probe records
   what happened against the endpoint and changes nothing a rendered runtime, a preset
   snapshot or a picker can observe. The version advances only on seed, provider / model
   create · update · delete and `workspace.flags`; `preset_snapshot_behind` and
   `Deployment.catalog_version` therefore never flag a bootstrap that only re-registered keys
   or an admin who only clicked *Probe*. A no-op registration (key already current, §8) writes
   no row at all. **An applied re-probe that really changes capability fields** (§7.4) is the
   one probe that shapes the catalog: besides its audit `model.probe` row the hook journals one
   catalog-shaping `model.update` row — actor the admin, `note: "applied from probe"`,
   before/after limited to the changed fields (`{tools: true} → {tools: false}`) — so the
   version bumps exactly when the content did. **N17-obs203 §5 — personal providers never
   bump the version:** every journal row carries the `scope` of the provider it is about
   (`model_catalog_changes.scope`; NULL for the seed, the flags, the roles and every
   pre-obs203 row); `stamp()` skips `personal` rows, so a member's own provider (create,
   edit, models, key, delete) changes nothing the `preset_snapshot_behind` banner or
   `Deployment.catalog_version` compare against — the N17-obs150 / obs202 banner class cannot
   be raised by another member's private endpoint. A scope flip is journaled `workspace`
   either way (the shared catalog gained or lost the rows) and bumps once; a shared provider's
   changes bump as before. The snapshot's `revision` (`content_stamp()`, every catalog-shaping
   row) is the ONE value the 30 s refresher compares (N17-obs281), so a second process still
   reloads personal rows;
   `/api/capabilities.catalog_version` and the page's `catalog_version` stay `stamp()`
   (`tests/test_model_provider_stamp.py`). Residual: `ProviderRow.probe_state` (served by
   `/api/capabilities.providers[].probe_state`) is read from the snapshot, which the probing
   process republishes in-process; a *second* process's 30 s stamp check does not re-read it
   until the next catalog-shaping write (the admin page reads the ledger and is always current).
5. Blast radius: delete refused while referenced (409 with referrers); disable never touches a
   deployed runtime (stored specs keep parsing — only new picks are refused); the built-in
   Bedrock provider cannot be disabled while it hosts a default model or is the only configured
   provider; built-in rows may be disabled, never deleted.

## 6. Tests (hermetic, in `make verify`)

| file | proves |
|---|---|
| `tests/test_model_registry_parity.py` (+ `tests/fixtures/model_catalog_pre_d21.json`) | G2: built-in snapshot, seeded registry and `/api/capabilities` equal the pre-D21 capture on every existing key; legacy key sets pinned; Titan row = `opensearch_http.EMBED_*`; seed journaled once |
| `tests/test_model_registry.py` | seed idempotence, provider/model CRUD + validation codes, sources (`builtin → manual`, probed + overrides), default rules, disable semantics (specs parse, picks refused, prose omits), per-workspace flags, referrers, refresh / stamp / TTL / fail-soft, proxy overlay + monkeypatching, no secret at rest, override deprecation, 501 slots, embedding guard slot, index migration |
| `tests/test_model_provider_ownership.py`, `tests/test_model_provider_keys.py` (N17-obs203) | owner-bound providers (§4.1: creation + derived references, hijack refusals, 404-before-403 in both modes, sharing, NULL creator, owner writes, legacy re-point) · keys typed in the console (§8.1: probe-first, SSM write + tags, Identity, last4, replace through PUT and /credential, delete residue, 502 naming the Sid, 501 without hooks, the no-leak scan over responses / DEBUG log / journal / ledger / resource maps) |
| `tests/test_model_providers_api.py` | exact key sets of every response model, member reach / anonymous 401 / the admin-only remainder on the 13 routes, CRUD over HTTP, `model_ref` by row id and by slashed Launchpad id, 501 stubs, fake hooks drive probe / credential / probe-on-add, flags scope capabilities, journal paging, no secret echo |
| `tests/test_capabilities.py`, `tests/test_route_policy.py`, `tests/test_ledger_migration.py`, `tests/test_workspaces.py`, `tests/test_model_catalog.py` | additive keys, ADMIN + hub-global classification, migrations, scoped table, every pre-D21 catalog assertion unchanged |
| `tests/test_system_agents_auto_repair.py` (N17-obs206 / obs259 / obs260, §9) | the plan lists every installed, active, not-failed preset across two workspaces and claims nothing; a claim offers one candidate to `install_preset` — `_repair` claims the behind / older-release ones and answers no-op for current ones (incl. a newer pin); deploying / failed / uninstalling are pre-filtered, a not-ready workspace is the funnel's refusal; a failed re-publish keeps `snapshot_behind: true` (serving pin = newest non-failed deployment, also when the failure pinned nothing) and is skipped `failed`, never retried, while the admin's 修复快照 still works; the queue claims lazily and launches one job at a time (at most one non-terminal; a restart mid-drain resumes exactly that job and the next startup sweep repairs the rest; 30 min cap moves on); three journal writes debounce into one sweep, a never-pausing burst is swept after 15 min, personal / audit-only rows arm nothing, `model_role.*` writes do without moving `stamp()`; the startup trigger honours the flag; bootstrap step 10 and the sweep claim one job in either order; `create_app` arms nothing under the suite flag |

## 7. Probes (M1-b — `services/model_probe.py`, `model_probe_openai.py`, `model_probe_bedrock.py`)

All probes run in the request thread when an admin clicks *Probe* (never on a schedule), answer
**facts about the endpoint** (never quality), send minimal requests (`max_tokens` 16, prompt
`"Reply with the single word OK."`; Bedrock `maxTokens` 256 because GLM reasons) and store
nothing but the dicts below. Budgets: provider 15 s, model 30 s; per request connect 5 s / read
≤ 20 s (both capped by the remaining budget), no retries. Every collaborator (HTTP transport,
DNS resolver, key resolver, Bedrock runtime, SSM / Identity clients, clock) is injectable.

### 7.1 Provider — `probe_provider(db, provider, actor)`

| kind | request | result |
|---|---|---|
| `openai_compatible` | `GET {base_url}/models` with `Authorization: Bearer <hub key>` (`aws_clients.provider_api_key`) | 200 → `state: ok`, `models_seen = data[].id` (≤ 200); 401/403 → `failed`, `error: "auth_failed: HTTP …"`; **404 → `ok`** with `error: "models_endpoint_unsupported (HTTP 404) …"` (some gateways have no list); no credential → `failed`, `error: "credential_unavailable: <variable> … <SSM parameter>"` and **no request leaves the host**; blocked host / timeout / redirect → `failed` with the sanitised reason |
| `bedrock` (built-in) | **none** — `require_global_bedrock_bearer()` only (the bearer is a runtime key; `ListFoundationModels` is not attempted) | bearer present → `ok`, `models_seen` = the registry's own Bedrock chat rows; absent → `failed`, `error: "credential_unavailable: AWS_BEARER_TOKEN_BEDROCK …"` |

Result (= `ProviderProbeOut`, stored verbatim into `model_providers.probe`):
`{state: never|ok|failed, checked_at (ISO-8601 UTC), latency_ms, models_seen: [≤200], error: ≤300 chars | null}`;
`db.commit()`, `refresh(db)`, journal `provider.probe {before: {probe}, after: {probe}}`.

### 7.2 Chat model — `probe_model(db, provider, api_model_name, "chat", model, *, apply, actor)`

| step | `openai_compatible` | `bedrock` | finding |
|---|---|---|---|
| hint | `GET /models` → for the id: `max_output_tokens` (or `max_tokens`), `context_length` (or `context_window`) | — | `raw_hint`, **never applied** |
| 1 exists | `POST /chat/completions {model, messages, max_tokens: 16, stream: false}` → 200; 404, or 400/422 naming the model as missing/unknown/invalid → `exists: false` (`errors.exists: "model_missing: …"`); 401/403 → unsettled (`auth_failed`) | `Converse(maxTokens 256)`; `ResourceNotFoundException` / `ValidationException` "model identifier … invalid" → `exists: false` | `exists` — on `false` or unsettled the run stops (the other fields are `skipped: …`) |
| 2 tools | `tools: [ping], tool_choice: "required"` → `tool_calls` ⇒ `true`; 200 without them, or 400 naming `tool_choice` ⇒ retry `tool_choice: "auto"` + "Call the ping tool." → `tool_calls` ⇒ `true`, none ⇒ **unsettled**; 400 naming tools/functions ⇒ `false` | `Converse` + `toolConfig{tools: [ping], toolChoice: {any}}` → `stopReason == "tool_use"` ⇒ `true`; `ValidationException` naming tool ⇒ `false`; other stop reason ⇒ unsettled | `tools` |
| 3 streaming | `stream: true` → first `data:` line within the budget ⇒ `true`; 400 naming stream ⇒ `false`; 200 without a chunk ⇒ unsettled | `ConverseStream` first event ⇒ `true` | `streaming` |
| 4 temperature | body + `temperature: 0.1` → 200 ⇒ `true`; 400 naming `temperature` ⇒ `false` | `inferenceConfig{temperature: 0.1}`; `ValidationException "… doesn't support the temperature field"` (the live N12-bug11 signature, Kimi K3) ⇒ `false` | `temperature` |
| 5 top_p | `top_p: 0.9` likewise | `inferenceConfig{topP: 0.9}` likewise | `top_p` |

≤ 6 completions (+ the one `/models` read). `vision`, `default_max_tokens`, `max_output_tokens`,
`default_judge_max_tokens` are **not probed** (kind defaults, `manual`).

### 7.3 Embedding model (`kind: embedding`)

`openai_compatible`: `POST /embeddings {model, input: "ping"}` → `embedding_dimension = len(data[0].embedding)`;
`bedrock`: `InvokeModel(body={"inputText": "ping"})` → `len(embedding)` (a non-Titan body without
`embedding` leaves the field unsettled: set it manually). Fields: `exists`, `embedding_dimension`.

### 7.4 Return shapes, unsettled fields, `apply`

* **Pre-insert** (`model is None`, called by `POST /{id}/models` with `probe: true`):
  `{findings: {exists, tools, streaming, temperature, top_p, embedding_dimension}, errors: {field: ≤300-char sanitised msg}, record: {checked_at, latency_ms, findings, errors, raw_hint}}`.
  `findings` carries **settled values only**; a field the probe could not settle is absent from
  `findings`, explained in `errors[field]` and stored as `null` in `record.findings`. The router
  inserts with `source: probed` per settled field, keeps the kind default (`manual`) for the
  rest, stores `record` as `models.probe`, and raises 422 `model.probe_failed {findings, errors}`
  itself when `exists` is `false` or (`errors` non-empty and `exists` not `true`) — the hook
  never raises it.
* **Re-probe** (`model` given): `{findings, diff: {field: {stored, probed, source}}, errors, raw_hint, latency_ms, applied: bool}`.
  `diff` has one entry per settled capability field. With `apply: true` the hook writes only
  fields whose `capability_sources[field] != "manual"` (setting them `probed`), stores
  `models.probe = record`, flips the row to `source: probed` when no field is `manual`, commits,
  `refresh(db)`, journals `model.probe {before, after}` (full `ModelOut` dicts — **audit-only**,
  never a catalog version, §5.4) and, only when ≥ 1 written field's value differs from the
  stored one, a second catalog-shaping row `model.update {before: {field: old}, after:
  {field: new}}` over exactly those fields with `note: "applied from probe"` (D21-obs6 — the
  version bumps iff the catalog content changed). `applied` is true iff at least one field was
  written, changed or not (all-`manual` rows: `false`, the record is still stored). Without
  `apply` nothing is written and nothing is journaled. The provider probe (§7.1) journals
  `provider.probe {probe: before} → {probe: after}`, audit-only as well.

### 7.5 Egress — the SSRF / region-boundary guard (plan §9.3)

`model_probe.guard_base_url(url, allowed_hosts=None, denied_hosts=None, resolver=None)` runs
**before every request** (DNS may change between two requests of one probe):
`model_registry.validate_base_url` (https only, a hostname — no IP literal, no userinfo / query /
fragment; the operator denylist always and the allowlist only in lock mode, §5.3 — 422
`model_provider.invalid_base_url` / `host_not_allowed`) **plus** resolution of the host with
every returned address checked:
loopback, RFC-1918, CGNAT `100.64/10`, link-local incl. IMDS `169.254.169.254`, multicast,
reserved, unspecified, documentation ranges, `::1`, `fe80::/10`, `fc00::/7`, v4-mapped v6, and
the names `localhost` / `*.internal` / `*.local` / `*.localhost` / `*.localdomain` / `*.home.arpa`
are refused (`HostBlockedError` → the probe fails with the host + address, nothing is sent). The
transport follows **no redirects** (a 3xx is a failure), reads **no proxy environment**
(`trust_env=False`), caps bodies at 1 MiB (JSON) / 64 KiB (SSE) and carries only the
`Authorization` bearer. Residual: resolve-then-connect is not pinned to the resolved address
(httpx); the per-request re-check narrows the window to one request.

Sanitisation (`model_probe_openai.sanitize`): the hub key, `Bearer …`, `sk-…`-looking tokens,
`api_key=`/`authorization:`/`token=` pairs and query strings are redacted, whitespace collapsed,
≤ 300 chars — applied to every vendor text before it is stored or returned.

## 8. Credentials (M1-b — `register_credential`, `services/model_credential.py`)

```
admin → POST /{id}/credential ──► key source by `credential_source`:
   ssm       → aws_clients.ssm_client(hub).get_parameter(Name=<credential_ssm_parameter>, WithDecryption=True)
   host_env  → os.environ[<credential_env>] (the built-in Bedrock row), SSM as the fallback
             │ name re-checked against launchpad-*-api-key; 422 model_provider.ssm_parameter_missing
             │ (absent / empty parameter, or no source at all); 502 model_provider.ssm_read_failed (AccessDenied …)
             ▼
   current = key_is_current(secret, row.key_version, row.key_modified_at)   # D21-obs5 idempotency rule (below)
   model_credential.register_from_registry(control, provider, value, rotate=not current)
             │ = ensure_api_key_credential_provider: GET → create when missing; else Update only when rotate,
             │   otherwise one line "api-key credential provider <name> up to date (key version N)"
             │ 502 model_provider.identity_failed {credential_provider, code}; the value is dropped here
             ▼
   resources.model_credentials[<name>] = {name, arn, secret_arn, configured: true, key_version, source: ssm|host_env}
   written to the HUB row (mirrored into config/launchpad.yaml) and, as {configured, key_version, source} over its
   own record, to every spoke row ALREADY configured for the provider; a spoke without the credential is left
   alone (`provider_configured` reads the workspace map — D21-obs3); `workspaces_updated` counts the rows written
             ▼
   created or rotated → model_providers.key_version / key_modified_at ← SSM Version / LastModifiedDate; journal
   provider.credential {configured, created, rotated, key_version, key_modified_at} (audit-only: never a
   catalog version, §5.4); refresh(db).  Neither (up to date) → no row write, NO journal row.
   Always: aws_clients.invalidate_provider_api_key(<name>)
             ▼
   CredentialOut {configured: true, created, rotated, workspaces_updated, key_version}
     created=false, rotated=false ⇒ the key was already current (console toast "already up to date")
```

**Idempotency rule (D21-obs5, `model_credential.key_is_current`).** The key Identity holds is
treated as *current* when the stored `key_version` equals the parameter's `Version` **and**,
when both sides know it, the stored `key_modified_at` equals its `LastModifiedDate`. Then the
Identity update and the journal row are skipped (`rotated: false`). Anything else — no stored
version (first registration, a pre-D21 record), a different version or date, a provider
Identity no longer has — creates / rotates and journals. A `host_env` key (the built-in
Bedrock row) carries no version metadata, so it can never be proven current and is always
rotated in place; that path never journaled a version. The parameter is still read on every
run (its `Version` is the only evidence); the value is dropped unused when the key is current.
To force a re-push of an unchanged key, store the parameter again (`aws ssm put-parameter
--overwrite` → new `Version`).

**Never stored, returned, logged or journaled:** the value. Log lines name the Identity provider,
the SSM parameter, the version and the actor. `SsmSecret.__repr__` shows version + date only.

### 8.1 Keys typed in the console (N17-obs203 §3, r17-mp — user decision 2026-10-21: "API key 直接在界面里输入")

The precedent is the Identity *Connections* page (`POST /api/identity/connections/api-key`,
`services/identity_providers.create_api_key_connection`): a secret typed in the console exists
in the request body and in the outbound AWS call only, under a field name the validation
handler already redacts (`core/errors.is_sensitive_field("api_key")` — a 422 never echoes the
input). Model providers mirror it, with SSM as the store:

```
member / admin → ProviderCreate.api_key | ProviderUpdate.api_key | POST /{id}/credential {api_key}   (write-only)
   ├─ Bedrock row → 422 model_provider.api_key_unsupported (it rides the host's Global bearer)
   ├─ 1. PROBE FIRST — ModelProbeHooks.probe_api_key: GET {base_url}/models with the TYPED key, the
   │     §7.5 egress guard before it; 401/403 / blocked host / timeout / unexpected status →
   │     422 model_provider.probe_failed {reason (sanitised), host|status} and NOTHING is stored
   │     (no row on create, no edit on update); a 404 list endpoint passes (some gateways have none)
   ├─ 2. the row is inserted / updated (provider.create / provider.update as before)
   ├─ 3. ModelProbeHooks.store_api_key: aws_clients.ssm_client(hub).put_parameter(Name=<row's
   │     credential_ssm_parameter>, Type=SecureString) — launchpad-<name>-api-key for
   │     a workspace-scope row, launchpad-user-<uid>-<name>-api-key for a personal one (§4.1).
   │     N17-obs232: a NEW parameter is created in that one call WITH its tags (Overwrite=False,
   │     Tags=launchpad:managed / launchpad:provider — no separate tag call to be refused; a tagged
   │     create refused AccessDenied is retried untagged); ParameterAlreadyExists → Overwrite=True
   │     (a key replacement) followed by AddTagsToResource best-effort with ONE retry on a fresh
   │     funnel client (`ssm_client(ctx, fresh=True)`; a refusal is a WARNING naming the code,
   │     never a gate — the SSM client is the hub's ambient instance-role session, the
   │     AWS_BEARER_TOKEN_BEDROCK env token is scoped by botocore to `bedrock` only). A parameter that
   │     PRE-EXISTED the first console entry was the operator's (rev-r22): its value is replaced
   │     but `key_entered_at` is NOT stamped (`record_entered_key(console_written=False)`, journal
   │     note says so), so delete / host change never remove it; `last4` is taken
   │     BEFORE the write and the value's reference dropped; a ClientError → 502
   │     model_provider.ssm_write_failed {ssm_parameter, code, statement: SsmWriteLaunchpadSecretParameters}
   │     (the row stays, "not configured" — fix IAM, re-enter)
   ├─ 4. the existing §8 path: register_credential reads the parameter back, creates / rotates the
   │     Identity provider, records model_credentials[<name>] {source: ssm, key_version} on the hub
   │     and every configured spoke (each workspace's availability record — §8.2) and
   │     key_version on the row (what `aws_clients.provider_api_key` reads SSM on)
   └─ 5. model_registry.record_entered_key: key_last4 + key_entered_at on the row, one AUDIT-ONLY
         journal row `provider.key {ssm_parameter, last4, key_version}` (never a catalog version);
         log line "key entered in the console by <actor> written to SSM parameter <name> (…abcd)"
→ ProviderOut.key_last4 = "abcd" ("configured · ****abcd" badge); CredentialOut.last4 on the
  /credential path; `configured` is now true as soon as a key is registered (the hub predicate
  `ProviderRow.hub_holds_key()` reads the row's key facts, §8.2), not only when the host
  variable is set.
```

"Replace key" is the same path (`PUT /{id} {api_key}` — with or without other edits; the probe
uses the body's `base_url` when it changes — or the card's dialog → `POST /{id}/credential
{api_key}`); Identity is rotated in place (`rotated: true`, `key_version` increments).
**A host change never carries the stored key along (N17-obs228, rev-r20):** `PUT /{id}
{base_url}` on a row that holds a registered key either comes with `api_key` in the same body —
the typed key is probed against the NEW host and stored, the journal note reads `base_url
changed from <old host> to <new host>; key re-entered` — or clears the credential:
`ModelProbeHooks.clear_api_key` deletes the console-written SSM parameter (an operator-created
one is kept — `forget_api_key`'s rule), deletes the Identity API-key provider (a runtime's
`GetResourceApiKey` for the old host stops at once; `ResourceNotFound` tolerated), flips the hub /
spoke record to `configured: false` (+ `cleared_on_host_change`) and drops the cache;
`model_registry.update_provider(clear_credential=True)` resets `key_version` / `key_modified_at`
/ `key_last4` / `key_entered_at`, so the card shows 「未配置」 and the next probe / model call has
no key (`credential_unavailable`, offline) until one is entered again. **Order (N17-obs230):**
the row write — with every validation and refusal inside it (422 `invalid_base_url`, 409
`in_use` on a disable in the same body) — commits FIRST; only then does `clear_api_key` run on a
snapshot of the row's previous credential facts, so a rejected body leaves the parameter, the
Identity provider and the key facts exactly as they were. **Step order and partial failure
(N17-obs231):** `clear_api_key` runs three independent steps — (1) the hub / spoke record →
`configured: false` + `cleared_on_host_change` and the process cache (a local ledger write,
`model_registry.unconfigure_credential_record`, first so a workspace's availability never
outlives the key; since N17-obs280 the record no longer licenses the hub's SSM read — the row
does), (2) the console-written SSM parameter, (3) the Identity provider; an AWS refusal in one
step is reported (`error`) and the next step still runs, anything else is collected per step and
raised ONCE after every step ran as 502 `model_provider.credential_clear_failed` whose detail is the
residue report (`failed_steps`, `record_unconfigured`, …) — the residue journal row carries the same
`error:` text either way. Without the probe module the local step still runs and the note reads
`error: no probe module registered`. **The row's own key facts gate the read (N17-obs254,
rev-r22; the ONLY gate since N17-obs280, §8.2):** `aws_clients.provider_api_key` reads the SSM
parameter only when the catalog row says `key_registered` (`ProviderRow.key_registered` =
`key_version` / `key_last4` / `key_entered_at` on the registry row, published with every
snapshot; asked through `ProviderRow.hub_holds_key(host_env=False)`).
`update_provider(clear_credential=True)` resets those facts in the SAME commit as the new
`base_url`, so from that commit on no outcome of the clearing steps — a record write that failed,
an operator-created parameter (never deleted), a refused `DeleteParameter` — can send the old key
to the new host. The refusal reads `the provider row holds no registered key — SSM parameter
<name> is not read and <variable> is not set on the hub; enter the <label> key under Settings →
Model Providers …` (env fallback still consulted first). Tests: `tests/test_model_provider_keys.py::
test_a_failed_record_step_with_a_surviving_parameter_still_cannot_send_the_old_key` (rev-r22's
repro: zero outbound requests, the parameter not even read),
`…::test_the_key_is_unreadable_from_the_row_commit_on_before_any_clearing_step_runs`,
`tests/test_model_probe.py::test_provider_api_key_resolution_order_and_cache`. Two journal rows: the `provider.update`
note names both hosts (`base_url changed from <old> to <new>; the stored key was cleared — enter
the key again`), the audit-only `provider.credential` row (`{configured: false, cleared: true}`)
carries the residue (`SSM parameter <name> deleted|kept, Identity provider deleted|kept`).
The edit drawer warns before the save (`modelProviders.hint.hostChangeClearsKey`) and the
warning disappears when a key is typed. Tests: `tests/test_model_provider_keys.py::
test_changing_the_host_of_a_keyed_provider_clears_the_key_unless_it_is_re_entered`,
`…::test_a_members_personal_row_clears_its_key_on_a_host_change_too`,
`…::test_an_operator_created_parameter_is_kept_when_the_host_changes`,
`…::test_a_rejected_host_change_leaves_the_stored_key_untouched` (obs230). Deleting
a provider whose key the console wrote (`key_entered_at` set) deletes the parameter best-effort
(`ModelProbeHooks.forget_api_key`, after the 409 refusals and before the row): the residue report
lands in the `provider.delete` journal note (`SSM parameter <name> deleted` / `left behind
(<code>)`) and a WARNING log line; an operator-created parameter (nothing typed) is never
touched. Without the probe module the typed-key routes answer 501
`model_providers.credential_unavailable` before anything is written.

**What is kept where:** the ledger — the parameter NAME, `key_last4` (4 chars), `key_entered_at`,
`key_version` / `key_modified_at`; the journal — the same four facts; the response — `last4`;
the log — parameter name + `…abcd`. **Nowhere** the value: `tests/test_model_provider_keys.py`
plants a recognisable key and scans every response, the whole log at DEBUG, every journal row,
every ledger column and every workspace resource map for it and for its first n-4 characters.
The AWS SDK's own loggers are part of that promise (rev-r20 Nit 3): `core/logging.py` pins
`botocore` / `boto3` / `urllib3` at INFO (`PINNED_LOGGERS`) so an operator's root DEBUG never
reaches them by inheritance — at DEBUG botocore prints the `PutParameter` / `GetParameter`
bodies, value included; an explicit operator level on them is kept
(`tests/test_logging_config.py::test_botocore_boto3_and_urllib3_are_pinned_at_info_even_under_a_root_at_debug`).

**IAM (hub template, `infra/ec2/launchpad-cn-ec2.yaml`):** new Sid
`SsmWriteLaunchpadSecretParameters` — `ssm:PutParameter`, `ssm:DeleteParameter`,
`ssm:AddTagsToResource` on
`arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/launchpad-*-api-key`
(the family the read statement grants; the two boot-path parameters do not match; no KMS twin —
the default `alias/aws/ssm` key needs none, as the read already relies on). Pinned by
`infra/tests/test_ec2_template.py` and, as the console's own permission manifest
(`workspace_permissions.CONSOLE_NEEDS`, evaluated against the rendered hub-region policy by
name), `tests/consistency/test_workspace_permissions.py`. Rollout: deploy.md §8.9.

**Console (amended 2026-10-21, user-confirmed UI):** the password 「API key」 field is the
PRIMARY credential input of the add / edit drawer (hint 「只在保存时传输一次，存入 SSM
SecureString，之后仅显示末四位」; `autocomplete=off`, sent once when non-empty, cleared before
the request, never pre-filled — an edit leaves it blank to keep the current key); the SSM
parameter NAME survives only under a collapsed 「高级 / Advanced」 `<details>` for
operator-managed keys (existing rows keep working unchanged — `credential_source: ssm`
reference; a personal row shows its derived name read-only there); the card shows 「已配置 · ****abcd / configured · ****abcd」 and 「输入密钥 / Enter
key」 or 「替换密钥 / Replace key」 (a dialog → `POST /{id}/credential {api_key}`); *Register
credential* / *Rotate* keep the D21 SSM-read path for operator-created parameters.

**Hub-side resolution** — `aws_clients.provider_api_key(name)` (alias `require_provider_api_key`):
Bedrock → `require_global_bedrock_bearer()`; otherwise, **when the registry row holds a
registered key** (`ProviderRow.hub_holds_key(host_env=False)` — the row's key facts, written by
this route, a console-typed key or the hub bootstrap pass; N17-obs280, §8.2), the SSM parameter
is read through `aws_clients.ssm_client(default context)` and cached for 10 min
(`PROVIDER_API_KEY_TTL_S`; `invalidate_provider_api_key` drops it on registration / rotation),
falling back to the provider's environment variable; without key facts the variable alone
(the host-boot path — an unregistered provider fails fast and **offline**, never a stray
GetParameter). `RuntimeError` names the variable and the parameter, never a value.
**N17-obs226 / N17-obs229 (rev-r20 B2):** the cache is keyed by `(provider name, registry row
id, parameter name)` — never by the name alone — and the registry drops a name's entries on
`insert_provider`, `delete_provider` and a `base_url` change (`model_registry._forget_cached_key`),
so a provider deleted and re-created under the same name (any member's personal `foo` after
the administrator's `foo`) is a new row that never inherits the old row's key and never sends it
to its own host. With the row, `DELETE /{id}` also flips the hub / spoke
`model_credentials[<name>]` record to `configured: false` (+ `deleted_provider_id`;
`ModelProbeHooks.forget_credential_record` — the Identity provider itself stays, rotation
history), so no workspace offers the successor until it registers its own key; the hub itself
never consults that record — the successor is a new row without key facts and fails fast and
offline (N17-obs280).
`tests/test_model_provider_keys.py::test_a_deleted_providers_cached_key_never_reaches_a_same_named_successor`.

### 8.2 The hub's single credential source (N17-obs280, design simplification C3)

**The registry ROW is the hub's one source for "is a key registered / may the hub read it".**
One predicate, `ProviderRow.hub_holds_key(*, host_env=True)` (`schemas/model_catalog.py`;
`model_registry.hub_holds_key(row)` for a ledger row), replaced three that answered the same
question from different inputs — `model_registry._hub_configured` (`key_version` or the host
env), `model_registry.holds_registered_key` (the three key facts) and the hub-record gate in
`aws_clients.provider_api_key` (`model_credentials[<name>].source == "ssm"` and `configured`,
plus the row since obs254). obs222 / obs226 / obs229 / obs231 / obs254 were each "two of them
disagreed".

| question | call | true when |
|---|---|---|
| the row holds a registered key — licenses the hub's SSM read; a host change clears it | `hub_holds_key(host_env=False)` | `key_version` set, or `key_last4` / `key_entered_at` (a console-typed key) |
| the hub holds the key — Settings card `configured`, probe-failed banner filter, default-chat-model guard, required-provider disable guard, the hub-side model builder and preset prose (`schemas.agent.provider_configured(resources=None)`) | `hub_holds_key()` | the above, or the required / Bedrock row (the host's Global Bedrock key — `credential_source: host_env`, `key_version` NULL by design, deploy-ops-35), or the row's `credential_env` exported in the hub process |

Before N17-obs280 the hub-side model builder (`aws_clients.build_chat_model` →
`assert_model_available(resources=None)`) read the host env ALONE, so a judge / plan-approval /
recommendation call on a console-keyed provider was 422 `model.provider_unconfigured` although
`provider_api_key` could read the key; it now asks the same predicate.

**What still reads `resources.model_credentials[<name>]`** (each workspace's record of the
Identity credential provider registered in THAT workspace's account — hub row included, written
by the same code path for every workspace; never a hub decision):

* `schemas.agent.provider_configured(name, ctx.resources)` → `configured_providers` /
  `model_available` / `assert_model_provider_configured`: a workspace's availability —
  `/api/capabilities.model_providers[*].configured` and the `available` trait of its catalog
  rows, the deploy / redeploy model guard, the evaluator / actor / recommendation model guards,
  the architect's proposal validation. A runtime of that workspace reads its key from that
  account's Identity vault (`GetResourceApiKey`), which the row cannot vouch for: the bootstrap
  env pass registers host-env keys there without any row facts, and a spoke registers in its
  own account (D21-obs3).
* `workspace_bootstrap._stage_model_credential` on a spoke: `known_versions` — the record's
  `key_version` is the D21-obs5 "up to date" compare (the hub compares against the row).
* `merge_workspace_resources` mirrors the hub row's map into `config/launchpad.yaml`
  (operator-visible names / ARNs; no reader in the backend).

Residual (unchanged from N17-obs231, rev-r20): a record that still says `configured: true`
while the row holds no key — the unconfigure step failed after a host change — can still let a
deploy in that workspace pass the availability guard, and its runtime then reads the workspace's
Identity vault. That is fail-open only if the Identity delete (step 3 of `clear_api_key`) failed
as well; the hub itself never serves the key, because both the SSM read and the process cache are
gated on the row (the cache lookup runs after `hub_holds_key(host_env=False)`, rev-r20 nit).

The writers keep the record honest for those consumers: `ModelProbeHooks._record_on_workspaces`
(registration), `model_registry.unconfigure_credential_record` (delete, host change). Pins:
`tests/test_hub_credential_predicate.py` (host-env Bedrock row, host-env workspace row,
console-typed key incl. the hub judge, operator-created parameter, cleared key + base_url cache
key, deleted provider + same-named successor with a planted lying hub record, a stale cache
entry for a row without key facts).

## 11. Bootstrap stage + IAM (M1-b)

* **Hub** (`services/bootstrap.py`): stage `model-providers`, right after `model-credential` —
  `model_credential.ensure_registry_credentials(control, ssm)` iterates every registry row with
  `credential_source: ssm` (every admin-registered OpenAI-compatible provider — e.g.
  SiliconFlow; the built-in Bedrock row is `host_env`), reads `launchpad-<name>-api-key`, creates / rotates the Identity
  provider, records `key_version` + `source: ssm` on the row (journal `provider.credential`,
  actor `bootstrap`) and on the hub config. Per provider it degrades with one line naming the
  parameter (absent → "not configured … click Register credential"; AccessDenied → "could not
  be read (AccessDeniedException; the ops role needs ssm:GetParameter on launchpad-*-api-key)";
  an Identity refusal → the code); the stage itself never aborts the bootstrap (summary
  `model_providers {configured, not_configured, unavailable_reason}`). **Idempotent
  (D21-obs5):** a second run with the parameter at the version the hub row records calls
  `GetApiKeyCredentialProvider` only, logs `api-key credential provider launchpad-<name> up to
  date (key version n)`, writes no journal row and leaves `catalog_version` unchanged; a run
  after `put-parameter --overwrite` rotates in place (`reused · key rotated from SSM parameter …
  (version n+1)`) and journals `provider.credential {rotated: true}` — which is audit-only and
  still does not advance the catalog version (§5.4). Two host updates without admin edits
  therefore leave `GET /api/capabilities.catalog_version` where it was.
* **Workspace** (`workspace_bootstrap._stage_model_credential`): after the env pass, the same
  registry pass through the **workspace's** `ssm` client (same parameter name, expected in the
  workspace's account/region); a registry result replaces the env one only when it configured
  the provider. A spoke keeps `key_version` on its own resource map and never writes the
  hub-global row's `key_version` / journal (`record_on_row=False`, D21-obs4); its idempotency
  check compares the parameter against that map (`known_versions=resources.model_credentials`).
  Stage detail: `launchpad-siliconflow · created · SSM v4` → `… · reused (up to date) · SSM v4`
  → `… · reused (key rotated) · SSM v5`; the env-sourced Bedrock entry stays `reused (key
  rotated)` (no version to compare — never journaled).
* **IAM** (`infra/ec2/launchpad-cn-ec2.yaml`, Sid `ReadLaunchpadSecretParameters`): one added
  resource `arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/launchpad-*-api-key`
  (the two explicit parameters stay for the boot path; `SiliconFlowApiKeyParameter` is legacy).
  Applying it is an IAM-only stack update — `docs/cn/interfaces/deploy.md` §8.6. N17-obs203 §3
  adds the WRITE twin `SsmWriteLaunchpadSecretParameters` (Put / Delete / AddTags on the same
  family) for keys typed in the console — §8.1, deploy.md §8.9.

## 9. Propagation — what each consumer receives (M1-d; plan §5.5)

The registry is read at **render / dispatch time**; nothing is pushed to a running
runtime. Every consumer carries a *descriptor* (`schemas.agent.model_descriptor(model_id)`,
pure, no DB) and the **catalog version** it was rendered from:

```text
descriptor = {model_id, kind: bedrock|openai_compatible, model_kind: chat|embedding,
              base_url, api_model_name, credential_provider, streaming,
              inference_params: {temperature, top_p}, judge_max_tokens, default_max_tokens,
              max_output_tokens, catalog_version,
              provider, model_region,                    # M1-d additive
              embedding: {dimension, max_chars}}         # embedding rows only (M1-d additive)
```

| consumer | mechanism | fallback | on catalog change |
|---|---|---|---|
| **Generated runtimes** (zip, A2A, Harness `model/load.py`, Studio prelude, eval worker) | the shared model block (`templates/strands_agent/main.py.tmpl`, markers `launchpad-model-constants` / `-helpers`, sliced by `templates/model_support.render_model_block`) now renders `MODEL_CATALOG_VERSION = <n>` and `MODEL_DESCRIPTOR = <descriptor of MODEL_ID>`; a process-level registry `MODEL_DESCRIPTORS` is seeded from it and from the env var **`LAUNCHPAD_MODEL_DESCRIPTOR`** (JSON: one descriptor, a list, or a `{model_id: descriptor}` map — an operator override, not written by the deployer); `_model_provider(model_id)` and `_supported_model_config` read the registry **first** | the inlined `MODEL_PROVIDERS` / `MODEL_INFERENCE_PARAMS` tables (byte-identical to the pre-D21 render for the seed), then the Bedrock default | nothing automatic; `Deployment.catalog_version` + `Deployment.model_descriptor` are pinned by the pipeline when the `generate` stage runs (every method); the agent view's `catalog_hint = {deployed, current, changed}` compares the pinned descriptor with today's (minus the version stamp) — `changed` only when **this agent's** row differs, a new unrelated model does not raise it; the console shows the *Model changed* tag / alert → Redeploy |
| **Eval worker** (judge / actor / summariser / optimizer) | `JobSpec.models = {model_id: descriptor}` (additive) filled by `evaluation/job_descriptors.attach` at the one dispatch funnel (`runtime_executor.submit`) from every `*model_id` key in the spec; the worker's `_handle` registers them (`_launchpad_register_descriptors(job.models)`) before `judge_model()` / `judge_budget()` run | the inlined provider / `JUDGE_DEFAULT_MAX_TOKENS` tables (an old payload or an old worker) | no repair needed for a capability edit; the digest still moves on a template change (repair once per release) |
| **Skill Lab / OpenCode** | `opencode_providers()` from the snapshot at job submit; each provider entry carries `catalog_version` (per provider — the vendored `extra_providers()` treats every top-level key as a provider) | — | no repair |
| **System presets** | `GET /api/system-agents` → `catalog_version: {installed, current}` + `snapshot_behind: bool` + `model_origin: default | override` + `default_changed: bool` (N17-obs202: the preset's model follows the `agent_default` role at install / repair; `default_changed` = a following row's stored model ≠ the role now — the same **Repair** re-publishes it) per preset (`installed` = the preset agent's latest `Deployment.catalog_version`, `null` before a pinned deployment; `current` = `model_registry.stamp(db)` (N17-obs281 — the same source as `snapshot_behind`); `snapshot_behind` = `installed is not None and installed < model_registry.stamp(db)`, N17-obs234); `model_registry.warnings(db)` gains `preset_snapshot_behind {installed, current, preset_key, workspace_id, agent_id}` through the `add_warning_producer` hook (`system_agents.service.catalog_snapshot_warnings`, registered in `app/main.py`) | — | banner → the card's **Repair snapshot** (below) |
| **Console pickers / hub model builder** | `/api/capabilities` (`catalog_version`, `embedding`) — unchanged by M1-d | — | immediate |

**Repair semantics of a behind preset snapshot (N17-obs234 / N17-obs235, user ruling
2026-10-21).** `system_agents.service.catalog_behind(db, agent)` is the ONE predicate: the
preset's newest **non-failed** deployment (`service.serving_pin` — N17-obs259: a failed
re-publish is pinned at the new catalog before its `generate` stage, or not at all, while the
Runtime still serves the previous snapshot) pinned a `catalog_version` that is not `None` and
is `<` the current catalog version. **One source for "current" (N17-obs281):**
`service.current_catalog_version(db)` = `model_registry.stamp(db)`, the ledger — read by
`catalog_behind` AND by the status read's `catalog_version.current` (which read the in-process
snapshot before, so a process whose snapshot lagged the ledger for ≤ 30 s could show
"current v<N-1>" next to `snapshot_behind: true`). The snapshot's `catalog_version()` stays what a
render pins (`Deployment.catalog_version`) and the release memo key — the version the rendered
bundle states. Ordinary agents use a different staleness rule on purpose: their
`catalog_hint` is a descriptor diff of the ONE row the agent embeds, a preset's version compare
covers the whole catalog its snapshot states (SKILL.md tables). It drives three things that must
never disagree: (1) the
`preset_snapshot_behind` warning above; (2) `snapshot_behind` on the status read; (3) the
repair decision — `_repair` treats such a preset as **changed**: a bodiless
`POST /api/system-agents/{key}/install {}` (no `force`) re-publishes through the same
claim / job path as `force`, the job payload carries `repair_reason` and the spec snapshot
`note` reads `catalog snapshot behind v<installed> < v<current>`; an equal or newer pin with
an unchanged spec stays the `200 changed: false` no-op, and a row without a pin (no
`generate` stage ever succeeded) is never behind. `catalog_version.installed` on the status
read is the same serving pin. The presets panel shows a primary **Repair
snapshot** (「修复快照」) action on an ACTIVE behind preset (`create.system.repairSnapshot`,
`data-testid="repair-snapshot-<key>"`, plus the note `create.system.snapshotBehind` naming
both versions); it posts the same `{}` body, toasts `repairStarted`, and the card then shows
the usual deploying state. The model-providers banner names the workspace from the warning's
`detail.workspace_id` (`modelProviders.warning.preset_snapshot_behind {{preset}} {{workspace}}`;
display name from the page's `workspaces[]` or the session list, the id when unknown) so two
workspaces behind on the same preset are two distinct banners. Tests:
`tests/test_system_agents.py::test_repair_republishes_when_the_pinned_catalog_is_behind_the_stamp`,
`frontend/src/pages/create/SystemPresetsPanel.test.tsx`, `ModelProviders.test.tsx`
"names the workspace".

**Auto-repair sweep (N17-obs206, user ruling 2026-10-22 — `system_agents/auto_repair.py`;
rev-r24 fixes N17-obs259 / obs260).** Bootstrap step 10 repairs only the default workspace's
evaluation worker; every other installed preset (the architect, and every preset of a
non-default workspace) used to stay stale after each catalog-shaping write and each host
update until an administrator clicked 修复快照 per preset per workspace. The sweep makes that
repair automatic ("随重新部署自动更新"). **Plan, then claim lazily.** `plan(db)` walks EVERY
workspace row and EVERY catalogue preset and returns the *candidates* — the installed, active
presets whose newest deployment and job did not fail — plus a skip row for every other pair
(`not_installed | deploying | uninstalling | failed`); nothing is claimed in the plan. A
candidate is offered to the existing `install_preset(db, row, preset, edit=None, force=False,
trigger="auto")` only when its turn comes (`claim(db, candidate)`, which re-runs the
pre-filter on the row as it is then). The staleness decision is NOT re-implemented: `_repair`
is the single funnel — it re-publishes on `catalog_behind` (the predicate above, read off the
newest non-failed deployment) or when the spec it re-derives from this build differs from
the stored one (a newer release, an added spec member), and answers `changed=False` for a
current preset, which the sweep reports as `skipped: current`; a refusal from the funnel
(e.g. `409 system_agent.workspace_not_ready`, `409 system_agent.bundle_invalid`) or an
unexpected error is that candidate's `skipped: refused | error` row, the others are still
offered. The durable claim in `_repair` (status + row version CAS in the job's transaction)
is what guarantees ONE job between the sweep, a card click and bootstrap step 10 — whoever
loses adopts the winner's (`skipped: in_flight`;
`tests/test_system_agents_auto_repair.py::test_bootstrap_step_10_and_the_sweep_claim_one_job`).
**A failed re-publish is never masked and never auto-retried (N17-obs259):** the row may
still read `active` because the live version survived (N10v3-bug7); its newest deployment /
job is `failed`, so the plan skips it with `reason: failed`, the failed job's id and
`Agent.error`, and — because the serving pin skips failed deployments — the status read keeps
`snapshot_behind: true` / the banner until an administrator's own 修复快照 (which still
re-publishes) or a later successful deploy. The job payload carries `repair_trigger: auto`
and `repair_reason: "auto: catalog snapshot behind v<installed> < v<current>"` (`"auto:
release update"` when only the re-derived spec differs), both surfaced on the status read's
`last_job {trigger, repair_reason}` so the card reads **自动修复中** / AUTO-REPAIRING while
it deploys (`create.system.autoRepairing`). **Serialization (N17-obs260):** the scheduler's
background thread drains the candidate FIFO one at a time — claim this candidate (one
`queued` job row), `start_deploy_async`, `join` the worker, next — so at most ONE auto-repair
job is non-terminal at any time: a host update or crash mid-drain leaves at most that one
job for `resume_pending_jobs` to resume and the remaining candidates are re-planned by the
next startup sweep (never N-1 parallel billable deploys, never a ledger of queued jobs
holding the deploy gate closed). A job still running past the 30 min cap is no longer waited
for: the queue moves on to the next candidate, the job itself runs to its own end. **Triggers
— exactly two:** (a) startup — `schedule_startup()` after `resume_pending_jobs()` arms a
sweep `system_presets_auto_repair_start_delay_s` (60 s) later, so a release that arrived
with the host update is re-published and the resumed jobs (rows still `deploying`) are
skipped; (b) every journal row that moves `stamp()` **or writes a model role**
(`model_registry.arms_auto_repair`: non-audit, non-personal rows, plus `model_role.set /
reset` — audit-only for `stamp()` yet an installed preset renders the role's default,
N17-obs202; a personal provider's rows and probes / credential / key rows schedule nothing)
arms a **debounced** sweep `system_presets_auto_repair_debounce_s` (180 s) after the LAST
write (trailing edge: a burst of provider edits is one sweep), never later than
`DEBOUNCE_MAX_WAIT_S` (15 min) after the burst's FIRST write. `Settings.
system_presets_auto_repair = false` (`config/launchpad.yaml` / `LAUNCHPAD_SYSTEM_PRESETS_
AUTO_REPAIR`) turns both off; the per-card 修复快照 stays. The scheduler is a state machine
with an injectable clock / planner / claimer / launcher (`AutoRepairScheduler.run_due()` is
the thread's one step), so the suite drives it without sleeping and runs with the flag off
(`conftest.py`). Tests: `tests/test_system_agents_auto_repair.py`,
`frontend/src/pages/create/SystemPresetsPanel.autoRepair.test.tsx`.

**Sweep logging (N17-obs268, `cn/r17-lp`).** deploy-ops-34 found that a sweep's skips lived
only in the in-process `AutoRepairScheduler.history`, so the host log could not say why a preset
was not repaired. Every sweep (a due deadline, not a bare queue drain) now logs on
`launchpad.system_agents.auto_repair`:

- **One INFO summary.** `auto-repair sweep: planned=N launched=K skipped={current: a, failed:
  b, in_flight: c, refused: d, not_installed: e}`. `planned` counts the candidates the plan
  queued and `launched` the jobs handed to the launcher. The five reasons are always named,
  zeros included (`SUMMARY_REASONS`). `deploying`, `uninstalling` and `error` are appended
  only when they occurred. A plan that raised logs `planned=0` after its exception line.
- **One INFO line per skipped preset.** `auto-repair skip: workspace=<id> preset=<key>
  reason=<r>`, plus `job=` (the failed job, the running one, the winner's on `in_flight`),
  `code=` (a refusal's error code) and `message=` (bounded to 200 characters) when present. A
  launched preset keeps its own `auto-repair: <preset> in workspace <id> → job <id> (<why>)`
  line.

The log helper never raises. Tests: `tests/test_system_agents_auto_repair.py::
test_every_sweep_logs_one_summary_and_one_line_per_skipped_preset`,
`…::test_the_skip_lines_carry_the_job_code_and_message`,
`…::test_a_broken_plan_still_logs_an_empty_summary`.

Precedence inside a runtime's registry: for the same `model_id` a strictly **newer**
`catalog_version` wins, an equal one **replaces** (so the env override beats the rendered
row); a malformed `LAUNCHPAD_MODEL_DESCRIPTOR` is a loud `RuntimeError` at import.
`deployer/environment.py` is unchanged (`LAUNCHPAD_MODEL_REGION` / `_CREDENTIAL` only).

Byte-identity pin (`tests/test_model_support.py::test_seed_data_literals_are_byte_identical_to_the_pre_d21_tables`):
for the seeded catalog the three data literals (`rendered_model_providers()`,
`rendered_model_inference_params()`, `rendered_judge_budget_defaults()`) equal what the
pre-D21 module dicts rendered; the Harness `model/load.py`, the Studio prelude and the eval
worker block stay the Strands slice by construction (existing tests).

## 10. Embeddings (M1-d; plan §6)

* The **active embedding row** (`schemas.agent.active_embedding()`: the enabled default,
  else the first enabled embedding row; Titan v2 / 1024 for the seed) drives, hub-side
  (`services/embedding_backend.py`): the embedder class — `TitanEmbedder` (mirrored core)
  for a Bedrock row, `OpenAIEmbedder` (`POST {base_url}/embeddings`, key via
  `aws_clients.provider_api_key` → fallback `require_provider_api_key`) for an
  `openai_compatible` row; the k-NN `dimension` of a **new** index
  (`vector_index_body(core.*_index_body())` — memory index in the bootstrap's
  `fixed_indices()`, per-KB index at KB creation); and the `embedding` block of every SQS
  message (`message_block()`: `{model_id, kind, base_url, api_model_name,
  credential_provider, dimension, max_chars, provider, model_region, catalog_version}`).
* The mirrored core (`app/services/opensearch_http.py` = Lambda copy = runtime template
  copy, R15) is **untouched**; its `EMBED_*` constants stay Titan's and remain the fallback.
* **Extractor Lambda**: `embedder_for(body, deps)` builds the embedder from the message
  block (Titan through the existing bearer; an OpenAI-compatible row through
  `GetApiKeyCredentialProvider` + `GetSecretValue` on **its** `credential_provider` — the
  same two calls, so `infra/stacks/memory_stack.py` adds the `launchpad-*` family to both
  Sids, additively, never `*`), caches it per descriptor, and keeps `deps.embedder`
  (`LAUNCHPAD_EMBED_MODEL_ID`) for a message without the block. An `OpenAIEmbedder`
  refuses a vector of another length than the descriptor's `dimension`.
* **Guard** (`services/embedding_guard.py`, installed into
  `model_registry.set_embedding_guard` by `app/main.py`): on `PUT …/models/{ref}
  is_default=true` for an embedding row and on disabling an embedding row, it counts docs
  with `_cat/indices/<pattern>?format=json` for `naming.index_patterns()` on every
  substrate this hub serves (one count per shared endpoint; the jobs index excluded) and
  refuses with **409 `embedding.reindex_required` `{indices: [{name, docs}],
  dimension_from, dimension_to, model_from, model_to}`** unless every count is zero.
  Same-dimension swaps are refused too (vectors are not comparable across models).
  D21-obs1 (fixed `92e55478`): the guard runs on the default *flip* only; disabling a
  non-default embedding row — never the active embedder — is not guarded.
* Scope limit: generated runtimes' own memory / KB retrieval (the third mirror,
  `templates/memory_runtime.py.tmpl`) still embeds with Titan; the guard keeps that
  consistent by allowing a row change only on an empty substrate. A `reindex` job and the
  runtime-side descriptor are the §12.1-2 follow-up.
| `tests/test_model_probe.py` (M1-b) | provider / chat / embedding probes against a scripted OpenAI-compatible endpoint (`httpx.MockTransport`) and a stub Bedrock runtime speaking the N12-bug11 texts (→ `temperature: false`), tool_choice fallback, unsettled fields, `/models` `raw_hint`, probe-on-add 201 / 422, re-probe diff + `apply` never over `manual`, credential registration (SSM → Identity → hub + every workspace row, `key_version`, journal, rotation), error codes, `provider_api_key` resolution order / cache / invalidation, SSRF matrix (allowed host, wildcard, `http://`, IP literal, userinfo, host not allowed, DNS rebinding to private / IMDS / loopback / v4-mapped, mid-probe rebind, redirect refused, no proxy env), sanitiser, no-leak scan over every response / journal / ledger / log / resource map |
| `tests/test_model_credential.py` (+ registry pass), `tests/test_bootstrap.py`, `tests/test_workspace_bootstrap.py`, `infra/tests/test_ec2_template.py` | `read_ssm_parameter` (pattern, missing / empty, version + date, value-free repr), `ensure_registry_credentials` (SSM → Identity, degrade per provider), the hub `model-providers` stage and the workspace stage's registry pass (records `source: ssm` + `key_version`, degrade, no value in output), the `launchpad-*-api-key` IAM pattern |

## 12. Model roles (r15-e, upstream #275 subset)

Which catalog model the **platform itself** uses or pre-selects for each job, and the
administrator's overrides — the useful subset of upstream #275 (`800f8895`) ported INTO this
page as a fourth tab 「模型角色」/ *Model roles* (`/v2/settings/model-providers?view=roles`,
admin-only like the page) instead of a new page (user decision 2026-10-19). Not ported: the
Claude SDK, Studio-fallback and codex roles, the price table, the GPT-6 default.

### 12.1 Roles (`services/model_roles.py::ROLES`, in API order)

| role | Settings field (= `platform_settings.key`) | default today | needs tools | allow empty | consumer |
|---|---|---|---|---|---|
| `agent_default` | `agent_default_model_id` (new) | `global.zai.glm-5.3` | yes | no | `schemas.agent.agent_default_model_id()` — `AgentSpec.model_id` default (a spec submitted without one; stored specs carry their id), `ProposalContent.model_id` default + the live id in the architect's protocol preamble (`assistant.service.protocol_preamble()`), the V2 wizard pre-selection, **the system presets' model at install / repair time** (N17-obs202, `SystemPreset.resolved_model_id()` — see below) |
| `judge` | `eval_judge_model_id` | `global.zai.glm-5.3` | no | no | strands_evals judge (`services/strands_eval`, `evaluation/judge_budget.py`, SCORE NOW, online evaluator, recommendations); the judge `model_id` the architect's evaluation plans default to (`schemas.agent.judge_default_model_id()` → `assistant/evaluation_plan.default_judge_model`, a per-call `default_factory`, N17-obs202) |
| `judge_fallback` | `eval_judge_fallback_model_id` (new, `""`) | `""` (none) | no | yes | **reserved** — stored, served and a referrer, but no consumer falls back to it in this build (the engine reads `eval_judge_model_id` only) |
| `failure_detection` | `eval_failure_detection_model_id` (new, N17-obs449) | `global.moonshotai.kimi-k3` | yes (the root-cause step forces a structured-output tool call) | no | the insights FailureAnalysis detector's judge (`strands_eval.insights.diagnose_sessions(detection_judge=…)`): stamped on the run as `judge_inference.failure_detection` at submit, shipped as the insights job spec's `failure_detection_model_id`, built by the worker; every other judge call stays on `judge`. Separate because GLM-5.3 reasoned past 16384 output tokens on the detector prompt without answering (diagnostic 2026-10-25); the default is one line (`core/config._CN_FAILURE_DETECTION_MODEL_ID`) |
| `actor` | `eval_actor_model_id` | `global.zai.glm-5.3` | yes | no | persona-scenario actor simulator |
| `prompt_opt` | `prompt_opt_default_model_id` | `global.zai.glm-5.3` | no | no | recommendation providers' default reflection model (`prompt_opt_models` stays a yaml / env list) |
| `skill_lab_judge` | `skill_lab_judge_model_id` | `global.zai.glm-5.3` | no | no | Skill Lab judge / optimizer (`bedrock_chat`); `feature: skill_lab` — hidden on the roles view while Skill Lab is (`HIDDEN_SURFACES.features`, N17-obs252) |
| `skill_lab_target` | `skill_lab_target_model_id` | `global.moonshotai.kimi-k3` | yes | no | the OpenCode worker's target model; `feature: skill_lab` |
| `memory_extract` | `memory_extract_model_id` (new, N17-obs252) | `global.zai.glm-5.3` | no | no | the memory-extraction summariser — the hub names it in every `memory_extract` SQS message and the extraction Lambda uses it (cn/r17-aj); its input limit bounds one extraction (N17-obs237) |
| `codegen` | `codegen_model` (existing field, registered as a role N17-obs252) | `global.moonshotai.kimi-k3` | no | no | the Strands Studio AI-fix coding agent (`codegen/config.py::get_model`, read per call); `feature: studio` — hidden on the roles view while `HIDDEN_SURFACES.createEntries.studio` is false |
| `embedding` | — (no Settings field; `field: ""`, `kind: embedding`) | `amazon.titan-embed-text-v2:0` (the built-in row) | no | no | the registry's **ACTIVE embedding row** (`schemas.agent.active_embedding`, knowledge bases + long-term memory). Listed last by `effective()` (`model_id` = the active row, `overridden_by` = the row's `updated_by` when it is not the built-in one); `PUT` / `DELETE` go through the registry's own guarded default flip `model_registry.update_model(…, {is_default: true})` — 409 `embedding.reindex_required` while any vector index holds documents (`services/embedding_guard.py`, never weakened; the console says 「已有知识库/记忆索引含文档，更换向量模型需先重建索引」), journaled `model.update`, catalog version advances; a chat row is 422 `reason: not_embedding` |

There is **no `assistant` role**: the system presets follow `agent_default` instead
(N17-obs202, user ruling 2026-10-22). `SystemPreset.model_id` is `None` for every shipped
preset and `build_spec` resolves the spec's `model_id` through `SystemPreset.resolved_model_id()`
= the `agent_default` role **as it stands at install or repair time** (never at import;
`DEFAULT_MODEL_ID` only while the role is unset). A changed role does NOT touch an installed
preset by itself: `GET /api/system-agents` reports `model_origin: default` + `default_changed:
true` (and `defaults.model_id` = the role now), the presets card shows 「模型默认已变化——修复以应用」
and the bodiless repair re-publishes with the new model (`repair_reason: "model default
changed <old> -> <new>"`). An explicit `model_id` set through the preset's settings edit is an
**override** (`Agent.preset_model_pinned = True`, `model_origin: override`): it wins and is
kept on every repair until `reset: ["model_id"]`. The architect's evaluation-plan judges follow
`judge` the same way (`default_judge_model()`); member-facing evaluator creation keeps its
constant defaults. The Model roles tab carries one hint line (`modelProviders.roles.presetHint`)
saying so. Defaults are unchanged by this port (`tests/test_model_roles.py::
test_defaults_are_todays_config_values`, `tests/test_settings_eval.py`).

**Consumers that read a role per call (N17-obs252, user ruling 2026-10-22 「需要都读模型角色来确定模型」):**
wherever the PLATFORM picks a default model it reads the role at the moment of use; an explicit
user / administrator choice stored on a resource wins. `agent_default` → `AgentSpec.model_id`
default, `ProposalContent.model_id`, the architect preamble (`protocol_preamble()`) and the
" (default)" tag of the architect's per-turn model catalog (N17-obs298, below), the system
presets at install / repair (N17-obs202), the Studio template's platform fallback
(`templates/studio_agent.render_model_prelude(None)`). `judge` → a new evaluator without
`model_id` (`evaluation/routers._judge_model`: the OpenAPI default stays the CN constant —
contract CA-1c — and `model_fields_set` tells an omitted member from an explicit one), the
architect's plan judges (`assistant/evaluation_plan.default_judge_model`), the engine / budget
sites. The console's new-evaluator form says 「默认来自模型角色：评估裁判」.

**One "default" in the architect prose (N17-obs298):** `schemas.agent.model_catalog_lines()`
tags " (default)" on the line whose id equals `agent_default_model_id()` — the id the preamble's
`model_id` default line names — and on no line when the visible catalog does not list it. The
registry's default-row flag (`is_default`, the 模型提供方 默认 column, `model_catalog_entries()`
`default`, `GET /api/capabilities`) keeps its meaning and no longer reaches the architect prose.
The architect skill's install-time catalog (`presets.render_model_catalog_section()`,
`tag_default=False`) tags no model: it would go stale when the role moves, so it points at the
preamble instead. Pinned by `tests/test_model_roles.py::
test_architect_catalog_default_tag_follows_the_agent_default_role`. The former
`evaluators_local.JUDGE_FALLBACK_MODEL_ID` literal had no consumer and was deleted.

### 12.2 Persistence — `platform_settings` + the Settings hook

* Table `platform_settings` (`app/models/model_registry.py::PlatformSetting`): `key` (the
  Settings field, PK), `value` (the model id, `""` = none), `updated_by`, `updated_at`.
  **Hub-global** (not in `WORKSPACE_SCOPED_TABLES`), created by `create_all` (no column
  migration yet); column names / types match upstream's table. Precedence: console override >
  `LAUNCHPAD_*` env > `launchpad.yaml` > built-in default.
* `core/config.register_settings_hook(fn)`: `get_settings()` runs every registered hook on a
  freshly built `Settings`; `model_roles._apply_snapshot` re-applies the process-wide
  override snapshot, so **`get_settings.cache_clear()` never reverts an override** (ported
  baseline / re-apply mechanism: the first Settings seen after a rebuild records the baseline
  = what a reset returns to; a racing sibling build keeps the true default —
  `test_a_concurrent_rebuild_keeps_the_default_and_reset_working`). `app/main.py` loads the
  snapshot from the ledger after `init_db()` (`load_overrides`); every set / clear replaces it
  and updates the cached `Settings` in place (no restart). `effective(db)` re-syncs when the
  ledger rows differ from the snapshot (a second process wrote) — consumers of another
  process see the change on its next `GET /api/model-defaults` or restart.
  `conftest.reset_model_role_overrides` drops the snapshot around every test.
* Validators: `agent_default_model_id` joins the `_catalog_model_id` validator (must be a
  catalog id); `eval_judge_fallback_model_id` may be `""`.

### 12.3 Rules

1. A role accepts only a model that is **in the catalog AND enabled** (model row and provider
   — D21 disable semantics; the per-workspace flags are a member-side picker rule, not a
   platform one), is a **chat** row, and — for `needs_tools` roles (`agent_default`, `actor`,
   `skill_lab_target`) — has `tools: true`. Otherwise **422 `models.model_not_allowed`
   `{role, model_id, reason}`**, `reason` ∈ `empty | not_in_catalog | not_chat | not_embedding |
   disabled | personal_scope | no_tool_use` (`not_embedding`: a chat row offered to the `embedding` role). `""` is accepted only where `allow_empty`. Unknown role → **404
   `models.unknown_role`**. Nothing refused reaches the ledger.
2. **Referrers see the roles.** `model_registry.setting_referrers()` lists every role's
   EFFECTIVE value as `{kind: setting, id: <field>, name: <field>, role: <role>, model_ids}`
   (additive `role` key; `prompt_opt_models` stays without one). Consequences: deleting a model /
   provider a role points at is 409 `model.in_use` / `model_provider.in_use` as before; **and**
   disabling such a model (`PUT …/models/{ref} {enabled: false}`) or its provider is now 409
   too (`{referrers: [setting rows only], suggest: "repoint"}`) — agents / evaluators naming the
   model still do NOT block a disable (§5.5). The required-provider rule
   (`model_provider.required`) still wins for the built-in Bedrock row. Today's defaults make
   the Kimi K3 row un-disableable until `skill_lab_target` is repointed — **unless the role's
   feature is hidden (rev-r25 F3 on N17-obs252):** a role of a feature in
   `Settings.hidden_role_features` (default `["skill_lab", "studio"]`, the server-side mirror
   of the console's `HIDDEN_SURFACES.features` / `.createEntries.studio`) or outside `Settings.features`
   is never a referrer (`model_roles.role_hidden`), so an administrator is never blocked by a
   role they cannot see. When such a model is then disabled / deleted (model, or its provider),
   `model_roles.on_model_unavailable` drops the hidden role's console override (journaled
   `model_role.reset`, one WARNING) so the feature, once shown again, never points at a dead
   model; a role whose DEFAULT is the dead model keeps it and is flagged `default_disabled`.
   Independently, `model_roles.resolve(role)` — behind `schemas.agent.agent_default_model_id()`
   / `judge_default_model_id()` — answers the role's default (one WARNING per change) when the
   effective model is missing / disabled and the default is usable.
   **rev-r25 F1 on N17-obs202:** `system_agents.install_preset` / `preset_status` call
   `model_roles.ensure_loaded(db)` first, so a process that never ran `load_overrides` (the
   bootstrap CLI installing the evaluation worker) resolves the same roles as the console.
3. **Journal, no second log.** `PUT` writes one `model_catalog_changes` row `model_role.set`
   (`target_id` = role, `before` / `after` = `{field, model_id, overridden_by}`), `DELETE` one
   `model_role.reset` (a no-op without an override writes none). Both are in
   `CHANGE_OPERATIONS` and **`AUDIT_ONLY_OPERATIONS`**: a role moves a platform default, not
   the catalog a runtime or a picker's option list is rendered from, so the catalog version
   does not advance (`/changes` lists them; `api.ts` `ModelCatalogOperation` names them).
4. **Probe = the registry's re-probe.** The tab's *Probe* opens the existing
   `ModelProbeDialog` on the picked row (`POST …/models/{model_ref}/probe {apply: false}`);
   there is no second probe route or probe code. A value that is no registry row (the empty
   fallback) cannot be probed.

### 12.4 API

| method / path | role | body → response |
|---|---|---|
| `GET /api/model-defaults` | every signed-in role (hub-global, `HUB_GLOBAL_PREFIXES`) | → `{roles: [ModelRoleOut]}`, `ModelRoleOut = {role, field, model_id, default, overridden_by, updated_at, needs_tools, allow_empty, default_disabled, kind, feature}` (`overridden_by` null when not overridden; N17-obs252: `kind ∈ chat | embedding`, `feature ∈ skill_lab | studio | null`; the Settings-backed roles in `ROLES` order, then `embedding`). **N17-obs251 (user ruling 2026-10-22): a member reads the same rows with the overriding administrator's username redacted — `overridden_by` is `""` when overridden (the console says 「管理员已覆盖」 / "Overridden by an administrator"), `null` when not; no row ever carried a secret** |
| `PUT /api/settings/model-roles/{role}` | ADMIN | `{model_id}` → `ModelRoleOut`; 404 `models.unknown_role`; 422 `models.model_not_allowed` (`reason` also `not_embedding` for the embedding role); `embedding`: the registry's default flip — 409 `embedding.reindex_required` while a vector index holds documents |
| `DELETE /api/settings/model-roles/{role}` | ADMIN | → `ModelRoleOut` (the default again; `embedding`: the built-in Titan row through the same guarded flip) |

Schemas in `schemas/model_providers.py` (`ModelRoleOut`, `ModelRolesOut`, `ModelRoleSetIn`;
key sets pinned by `test_model_roles.py`); router `routers/model_roles.py`; `route_policy`
rows + `WORKSPACE_EXEMPT`; the 13-route registry pin is unaffected (different prefix).

### 12.5 Console

* `frontend/src/lib/model-defaults.ts` (adapted from upstream): `loadModelDefaults()` (one
  fetch per page load, static fallback not cached), `reloadModelDefaults()`,
  `currentModelDefaults()`, `useModelDefaults()`, `useModelDefaultsArrival(onArrive)`,
  `adoptModelDefault(current, seeded, configured)` (only a still-seeded value is replaced —
  never a stored id or the member's pick), `roleModelOptions(role, catalog)` (enabled chat
  rows, tool-capable where `needs_tools`, current + default always first).
* Pickers seeded from the server defaults: the V2 **AgentWizard** (new agents;
  `lib/models.defaultModelFor` prefers the configured `agent_default` when it is an offered,
  available option — so `emptyAgentForm` and a method switch seed it too), **TaskWizard**
  (`actor`), **Evaluators** create drawer (`judge`), assistant **Proposal** editor (source
  switch → `agent_default`). Stored ids are never rewritten.
* Tab `?view=roles` (`v2/pages/modelProviders/RolesTab.tsx`): one row per role — label +
  hint + field, Select over the accepted rows, status (Overridden by … / Built-in default +
  default), Probe / Save / Reset; writes toast, reload the tab and `reloadModelDefaults()`.
  **Members see the tab READ-ONLY (N17-obs251):** `MEMBER_VIEWS = [providers, roles]`, the
  pickers disabled, no actions column, one line 「仅管理员可修改模型角色」 / "Only administrators
  can change model roles"; the writes stay ADMIN in `route_policy` (403); roles of hidden
  features stay hidden for everyone (N17-obs252).
  i18n `modelProviders.roles.*` (en + zh). One neutral line under the table
  (`modelProviders.roles.presetHint`, N17-obs202): system presets pick their model at install
  or repair time — after changing a default, repair them under Create Agent → System presets.

### 12.6 Tests

`backend/tests/test_model_roles.py` (defaults pinned; consumers read the override without a
restart; set / reset / read; survives `cache_clear()`; racing rebuild; restart + `create_app`;
second-process row picked up; console > env; 404 unknown role; 422 reasons incl. disabled
provider; 409 through a role on disable / delete of model and provider; referrers; journal
rows audit-only; route policy; member reads / cannot write; admin write records who),
`test_model_registry.py::test_delete_model_rules_and_referrers` (roles in the referrer list),
`test_model_providers_contract.py` (operations pin). Frontend: `lib/model-defaults.test.ts`
(normalise, fetch-once / fallback / reload, adopt, options, the Create Agent form seed) and
`ModelProviders.test.tsx` "model roles tab" (tab order, rows, option filtering, PUT / DELETE /
re-probe, 422 surfaced, member forbidden, the presets hint line). N17-obs202:
`test_system_agents.py` (install takes the role model; role unset → constant; role moved after
install → `default_changed`, bodiless repair re-publishes with the new model and the
`repair_reason`; an override is kept when the role moves and `reset: ["model_id"]` returns to
following; legacy rows classified by the constant; plan judges follow the `judge` role) and
`SystemPresetsPanel.test.tsx` "model default changed" (note + the one repair button, both notes
one button, no note for an override / a deploying row, zh punctuation). Frozen eval slice untouched (`JudgeCreate` /
`JudgeUpdate` defaults stay the constant; the Evaluators picker seeds from the server).
