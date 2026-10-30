# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AgentCore Launchpad is a production-grade sample asset: one React console over one
FastAPI backend that wires the real Amazon Bedrock AgentCore services (Runtime,
Gateway, Identity, Observability; Memory, Registry and Knowledge Bases through
pluggable backends) into a unified **create → deploy → invoke → observe** experience.
Everything targets a real AWS account in `cn-northwest-1` (partition `aws-cn`);
Bedrock models are reached in `us-west-2` through a Global Bedrock API key. Read
[docs/architecture.md](docs/architecture.md) first — it is the authoritative,
up-to-date map of how each console feature backs onto an AgentCore service and resource.

## Commands

All Python is managed by **uv** — run backend/infra commands from their own directory
with `uv run`, never bare `python`/`pip`.

**Before starting/stopping/updating the running stack**, read the agent runbooks:
[docs/agent-runbook-dev.md](docs/agent-runbook-dev.md) (local dev) /
[docs/agent-runbook-prod.md](docs/agent-runbook-prod.md) (prod, systemd) — they carry
the precondition probes and restart side-effect traps the table below does not.

| Task | Command |
|---|---|
| Full verify gate (**run before reporting done**) | `make verify` |
| Run local stack (backend :8000, frontend :5173) | `make dev` |
| One-time infra + AgentCore bootstrap (idempotent) | `make bootstrap` |
| Backend only / frontend only | `make backend` / `make frontend` |
| Backend lint + tests (parallel, as the gate runs them) | `cd backend && uv run ruff check . && uv run pytest -q -n auto` |
| Single backend test (serial — omit `-n`) | `cd backend && uv run pytest tests/test_agents_api.py::test_name -q` |
| Fault-injection / permission-matrix / API-contract suites (hermetic; part of the backend pytest step — `make contract` runs the Schemathesis file alone) | `cd backend && uv run pytest tests/faults tests/test_permission_matrix.py tests/contract -q -n auto` |
| Mutation testing — how strong the tests are, not whether they pass (NOT in `make verify`; backend: hours for a cold run on a loaded host, incremental afterwards — the 2026-10-19 baseline is PARTIAL; frontend ≈ 2 min; baseline + how to read: `docs/cn/reports/mutation-baseline-2026-10-19.md`, `docs/cn/interfaces/testing.md` §6) | `make mutation` / `make mutation-frontend` |
| Frontend lint / typecheck / unit tests / build | `cd frontend && npm run lint && npx tsc --noEmit && npm test && npm run build` |
| i18n key parity (en ↔ zh-CN) | `python3 scripts/i18n_check.py` |
| zh-CN full-width punctuation (`--fix` to convert) | `python3 scripts/i18n_zh_punct.py --check` |
| CN partition lint (no `arn:aws:` / `.amazonaws.com` / Global console-host literals) | `bash scripts/cn_lint.sh` |

`scripts/verify.sh` (= `make verify`) is the canonical gate: backend ruff+pytest, infra
ruff+pytest, frontend eslint+tsc+vitest+vite-build, i18n parity, zh-CN full-width
punctuation, the CN partition lint, the region-boundary check (only Global Bedrock model
inference may leave the China partition; `scripts/region_boundary_check.py`), cfn-lint on
`infra/ec2/*.yaml`, the Harness template self-check and the BYOC sample self-check. It must pass before any change is considered
complete. The gate runs the
backend suite in parallel via `pytest-xdist` (`-n auto`, ~1 min on 8 cores instead of
~6.4 min); `-n` is deliberately **not** in `addopts`, so one named test stays serial.

**`backend/tests/` vs `backend/scripts/e2e_*.py`:** `tests/` are hermetic unit tests
(SQLite is redirected to a temp DB in `conftest.py`; AWS is stubbed) and run in
`make verify`. The `e2e_*.py` scripts hit **real AWS** and require `make bootstrap` +
credentials — they are not part of the verify gate.

## Repo layout

`backend/` FastAPI control plane · `frontend/` React (Vite) console · `infra/` CDK app
(`launchpad-base` stack) · `scripts/` bootstrap/teardown/dev/verify ·
`config/` generated `launchpad.yaml` ·
`docs/` architecture/api/setup/troubleshooting (all bilingual). See the table in
[README.md](README.md#repo-layout).

## Architecture — the load-bearing patterns

These are the abstractions that span many files; understanding them is what makes you
productive here. The per-feature detail lives in `docs/architecture.md` and the specs
under `.trellis/spec/launchpad/`.

- **Three creation methods, one pipeline.** 方式B (self-hosted Harness, registered by
  the `harness` method), 方式C (Strands Studio canvas), `byoc` (member-uploaded code
  zip / Dockerfile context / existing ECR image), plus `zip_runtime`, all converge
  into the ordered stages `generate → package → provision → deploy → register` in
  `backend/app/deployer/pipeline.py`. Each method registers one callable per stage
  (or omits it) via `register_method()`; the method modules (`deployer/zip_runtime.py`,
  `byoc.py`) are imported **for their side effects** in `app/main.py`, so a new method
  must be imported there to exist.

- **Deploy is an async, resumable job.** `POST /api/agents` returns `202` with a
  `job_id`; the job runs on a background thread, persisting per-stage status onto the
  `Deployment` row and JSONL events onto `Job.log`. `resume_pending_jobs()` runs on
  startup and re-runs interrupted jobs from the first non-succeeded stage — so stages
  should be idempotent.

- **One invoke chain for both entrances.** The Chat console (`/api/chat/{id}`) and the
  public `/v1` API share the single entry point `app.services.invoke.invoke_agent_text`
  (+ `app.services.chat.chat_stream` for SSE). `/v1` only adds `X-Api-Key` auth
  (sha256-hashed); everything downstream of method dispatch is identical. Change invoke
  behavior in one place, not per-router. The session-lifecycle gate is part of that chain:
  a turn under an ENDED chat session id is `409 chat.session_ended` from
  `invoke.require_session_open` on every entrance (N16-obs79 console, N16-obs88 `/v1`).

- **AWS is the source of truth; the SQLite ledger holds only identifiers + derived
  progress.** The ledger (`data/launchpad.db`, models in `app/models/ledger.py` plus the
  assistant models) stores agents, deployments, jobs, chat sessions, api keys, and
  announcements. Authoritative resource state (runtime status, traces) is always read
  back from AWS.

- **All boto3 clients are built in exactly one place.** `app/services/aws_clients.py`
  constructs every AWS client/session, keyed by a `WorkspaceContext` (account, region,
  future assume-role) from `app/services/workspace.py`; a hermetic guard test
  (`tests/test_client_funnel.py`) fails on construction anywhere else. AgentCore client
  *names* stay in `app/services/agentcore/client.py`, and preview-API drift is contained
  there and in the sibling wrapper modules (`runtime.py`, `gateway.py`, `codebuild.py`).
  Everything else receives clients explicitly so tests can inject stubs — follow this;
  do not call `boto3.client(...)` elsewhere.

- **Partition-aware ARNs and hostnames.** Never write `arn:aws:` or `.amazonaws.com`.
  Backend code builds ARNs with `WorkspaceContext.arn(service, resource, region=None,
  account=None)` and hostnames with `ctx.dns_suffix`; CDK uses `self.partition` /
  `self.url_suffix`. Console hosts do not follow the DNS suffix (`console.amazonaws.cn`
  in aws-cn): build console deep links from `WorkspaceContext.console_host`.
  `scripts/cn_lint.sh` (part of `make verify`) fails on these literals and on the Global
  console / sign-in host (`console.aws.amazon.com`, `signin.aws.amazon.com`); the only
  exemptions are the partition→suffix and partition→console-host tables in
  `aws_clients.py`, marked `cn-lint: allow`, and the **partition-invariant IAM service
  principals** — `<service>.amazonaws.com` for the ec2 / lambda / codebuild / bedrock-agentcore
  principals and the service-linked-role paths, identical in aws-cn — which may carry
  `cn-lint: allow` where they are written (`harness_local.py`, `agent_iam.py`,
  `schemas/agent.py`, `workspace_permissions.py` do); a `.amazonaws.com.cn` hostname needs no
  marker (the pattern stops at `.com` followed by a non-dot).

- **Feature backends.** Memory, Registry, Knowledge Bases and Policy are Protocol modules
  (`services/{memory,registry,knowledge,policy}_backend.py`) with a `set_backend()` slot;
  their routers answer 503 `feature.unavailable` until a backend is registered.
  `Settings.features` (default: the kept modules) is served by `GET /api/capabilities` and
  drives both console navigations; `policy` is listed only while the Cedar engine is
  registered, with an additive `policy: {engines, attached_gateways}` block. **Policy follows
  the Global AgentCore Policy model (2026-10-28, `docs/cn/interfaces/policy-engines.md`):**
  a policy engine (`policy_engines`, `services/policy_engines.py`) is a workspace resource,
  a policy belongs to one engine (`enforcement_mode` active | log_only), a gateway is attached to
  at most one engine with a gateway-level `LOG_ONLY | ENFORCE` mode
  (`policy_engine_attachments`, `PUT /api/governance/gateways/{gid}/policy-engine`, ADMIN), one engine
  may be attached to many gateways, and only the Gateway interceptor decides (one Dogwood set
  per engine, set key = engine id; one bundle `launchpad.policy.gateway/3` per gateway with an
  `attachment` block). Routes live under `/api/policy-engines/**`
  (`routers/policy_engines.py`); `/api/policies/**` answers 410 `policy.moved`. There is no
  workspace mode, switch, confirmation name, agent scope or `apply` any more — the principal
  carrier is part of every deploy while the platform switch `policy_gateway_interceptor` is
  on; a pre-engine ledger is migrated at startup (`policy_engines.migrate`, journalled
  `policy_engine.migrated`). Every policy decision is a `policy_decisions` ledger row (with
  its `gateway_id` / `engine_id`) mirrored to CloudWatch `/launchpad/policy/decisions[-env]` by
  the batched, fail-soft `services/policy_audit.py` sink (never blocks a request; the `policy`
  bootstrap stage creates the group).

- **The model catalog is the admin-managed model registry (D21).** The ledger tables
  `model_providers` / `models` / `model_catalog_changes` (journal id = catalog version) /
  `workspace_model_settings` (`services/model_registry.py`) publish an immutable
  `schemas/model_catalog.Catalog` snapshot; `schemas/agent.py` exposes it only through
  **function views** (`allowed_model_ids()`, `providers()`, `model_catalog_table()`,
  `model_capabilities()`, `model_default_max_tokens()`, `model_catalog_entries()`, …) — the
  pre-D21 module constants and the `LAUNCHPAD_SILICONFLOW_MODELS` override were removed
  (C5); a test publishes a variant with `model_catalog.publish(model_catalog.extended(…))`.
  A fresh ledger is seeded from today's built-in tables at startup
  (`model_registry.ensure_seeded()`) and behaves byte-identically (parity test); writes
  republish in-process and a 30 s stamp check covers a second process. **Providers are
  owner-bound (N17-obs203, user decision 2026-10-21):** `/api/settings/model-providers`
  (`routers/model_providers.py`) is every member's — a member registers `scope: personal`
  providers (visible to them + administrators), edits / probes / deletes them and adds their
  models; an administrator does everything, shares (`scope: workspace` — sharing TRANSFERS the row
  to the workspace: `created_by` → NULL, the former creator loses mutation rights, N17-obs225)
  and touches members' rows; on a workspace-scope row `base_url` / `credential_ssm_parameter` /
  the key are an administrator's whoever the creator is (`require_admin_for_credential_fields`); a hidden row is 404, another creator's 403 `model_provider.forbidden`
  (`model_registry.require_provider_visible` / `require_provider_mutable`); the journal and
  the per-workspace flags stay ADMIN. A personal provider's SSM parameter / Identity provider
  are owner-derived (`launchpad-user-<uid>-<name>[-api-key]`, no host-env fallback) and the
  operator host allowlist is a lock mode, not a precondition (`model_provider_allowed_hosts`
  default `[]`; `model_provider_denied_hosts` always). A key may be TYPED in the console
  (`api_key`, write-only): probed first, written to the row's SSM SecureString by the console
  host (`ModelProbeHooks.store_api_key`, IAM `SsmWriteLaunchpadSecretParameters`), registered,
  kept as `key_last4` only. What a user may USE follows ownership too: `/api/capabilities`
  and the architect prose list shared + own models (`schemas/model_catalog.Principal`), a deploy
  naming another member's personal model is 422 `agent.model_not_visible` (hub-side USE sites —
  evaluator model, plan-approval judges, recommendation model, `build_chat_model(principal=)` —
  422 `models.model_not_visible`, N17-obs227), personal changes
  never bump the catalog version (`stamp()` skips `personal` journal rows; `Catalog.revision`
  drives the refresher). The registry stores SSM parameter / Identity provider **names**
  (+ last4) — never key material. **Model roles (r15-e,
  upstream #275 subset):** which catalog model the platform itself uses per job
  (`agent_default`, `judge`, `judge_fallback`, `failure_detection`, `actor`, `prompt_opt`,
  `skill_lab_judge`, `skill_lab_target` — `services/model_roles.py`) is a `Settings` field an admin overrides on
  the page's *Model roles* tab (`?view=roles`; `GET /api/model-defaults` for every role's
  effective value, admin `PUT`/`DELETE /api/settings/model-roles/{role}`); overrides live in the
  hub-global `platform_settings` table and are re-applied to every rebuilt `Settings` through
  `core/config.register_settings_hook`, so `get_settings.cache_clear()` never reverts them; a
  role accepts only an enabled, tool-capable-where-needed catalog chat row (422
  `models.model_not_allowed`), the referrers see the effective role values (409 `*.in_use` on
  delete AND disable), and every write is an audit-only `model_role.*` journal row. Contract:
  `docs/cn/interfaces/model-registry.md` (§12 for the roles).

- **Config precedence:** defaults < `config/launchpad.yaml` < `LAUNCHPAD_`-prefixed env
  < init kwargs (`app/core/config.py`); the model-role fields alone have one more layer on
  top — an administrator's console override (`platform_settings`, r15-e) beats env, and a
  reset returns to the env/yaml value. `launchpad.yaml` is written by `make bootstrap`
  and is gitignored; before bootstrap the defaults keep the app importable/testable, but
  real AWS calls will fail.

- **API times are display-zone ISO (N17-obs463).** Storage stays UTC (naive ledger columns,
  AWS / artefact `+00:00`), but every JSON body and stream event leaves through
  `app/core/display_time.py` (the app's `default_response_class`, `DisplayTimeJSONResponse`
  for an explicit response, `chat.sse_encode`), which writes each datetime object, and each
  ISO date-time string under a TIME KEY (`is_time_key`: `*_at`, `*At`, the enumerated
  `TIME_KEYS`), in `Settings.display_timezone` (default `Asia/Shanghai`, `+08:00`), the same
  instant; content subtrees (`CONTENT_KEYS`, dataset-item shapes, stream text events) are never
  rewritten. Never build a plain `JSONResponse` (a guard test fails); compare
  or store inbound times through `to_utc_naive` / `users.as_utc`. The console formats in that
  zone (`lib/timestamps` `zonedParts` / `formatInZone` / `zonedClock`), never the browser's.

- **Per-agent memory scoping.** A memory namespace is keyed only on `{actorId}`, so the
  platform folds the agent id into the actor (`services/memory_backend.py::scoped_actor
  (agent_id, human)` → `<agent>__<human>`) to partition both short-term events and
  long-term facts per agent regardless of which backend is registered. The ledger still
  stores the bare human actor for display. A spec may pin its own `memory.memory_id`
  only if the workspace *manages* it (bootstrap memory or a `managed_memories` row —
  `services/memory_ownership.py`): the AWS account is not the workspace boundary.

- **System-managed presets are server-owned.** `Agent.system_key` (never settable via
  `AgentSpec`) marks a platform preset (`backend/app/system_agents/`); the ordinary
  redeploy/delete/convert routes refuse such rows before any AWS call, and admins
  install/repair/uninstall only through `/api/system-agents`. Nothing installs on
  startup or on a read. Preset skills upload to the versioned
  `system-skills/<name>/<version>/` S3 prefix, disjoint from member-writable prefixes.

- **Every user-managed resource records its creator (Family B step 1).** `created_by`
  (= `users.id`, the ownership key; NULL for the config admin / auth-disabled operator,
  presets, discovered runtimes and legacy rows) + `created_by_name` (display) on agents,
  chat sessions, API keys, memories, KBs, Connections, eval / optimization / Skill Lab rows and
  local registry records (stamped through `registry_backend.current_creator`, N15-obs7);
  `services/creators.py` (`Creator.of_request`, `stamp`, `fields`) is the one helper and the
  creator is never client-settable. `/v1` keys live only while their creator may hold them
  (`services/api_keys.creator_offboarded`); agent redeploy / delete, memory record delete,
  runtime-canary actions other than `verdict`, Skill Lab job / task set mutations, dataset /
  evaluator PUT / DELETE and registry record PUT / reimport / DELETE are creator-or-admin
  (`services/ownership.require_creator_or_admin`,
  403 `<kind>.forbidden`; a NULL creator is an administrator's); deleting a dataset / evaluator
  still named by an active run, pipeline, online config or canary is 409
  `<kind>.in_use` unless an admin passes `?force=true` (`evaluation/references.py`; finished
  runs never block); members list only the keys they minted and the chat sessions they
  opened. The retired `Agent.owner` column is migrated and dropped at startup. Contract:
  `docs/cn/interfaces/users-and-ownership.md`.

- **Workspace isolation (Family B step 2).** `Workspace.settings["resource_visibility"]` =
  `shared` (default) | `isolated`: a member then sees a resource only when `created_by` is
  theirs, NULL, or a *current* admin's; admins see all. `services/visibility.py` holds the
  rule (`Viewer`, `sees()` / `clause()`, `viewer_for_key`) and `services/agent_access.py`
  the agent lookup; every kind's choke point takes `viewer: Viewer = Viewer.ALL`, so
  workers / deploy / purge stay unfiltered and only routers pass `require_viewer`. A hidden
  id answers the kind's existing not-found. Policies, MCP / skill records and gateways stay
  shared for read. A run payload masks the names of referenced rows the viewer cannot see
  (`evaluation/routers._runs_out`, additive `hidden: {agent, dataset}`, N15-obs8). Contract:
  `docs/cn/interfaces/users-and-ownership.md` §7.

- **Spec snapshots are the agent version ledger (upstream #277 subset, r15-a).** Every publish
  that passes `deployer.pipeline.create_deployment` writes the next `spec_snapshots` row (per-agent
  `seq`, `services/snapshots.py`) in the caller's transaction; `_finish` stamps the AWS version and
  the spec the job packaged (`Job.payload.packaged_spec`, N17-obs13 — never the row's current spec);
  the Runtime-canary promote and rollback write their own (N17-obs2; both refused with
  `409 agent.deploy_in_progress` while a deploy job owns the agent, `agents.md` §1.2); a startup backfill gives every
  live agent a `baseline` once (idempotent). The evaluation scorecards (`evaluation/scorecard.py`,
  `GET /api/eval/scorecard`, `/runs/compare`, `POST /runs/{id}/rerun`) read `seq` as the version a
  run evaluated. Reads only in CN: no rollback route, no Environments page. Contract:
  `docs/cn/interfaces/evaluation-engine.md` §23.

- **The sign-in form is the only gate behind the open NLB (r12-g).**
  `services/login_guard.py` holds in-process state (one uvicorn worker, no Redis): a
  per-submitted-name lockout (5 wrong passwords → 60 s, doubling to 30 min; 429
  `auth.locked` + `Retry-After`; admin `PATCH /api/users/{id} {"unlock": true}`) is the
  PRIMARY control (the built-in config admin is exempt — break-glass) and per-source-IP
  windows (20 failed sign-ins / 5 min, 10 failed registrations / h, 20 invalid `/v1` keys /
  5 min; 429 `auth.rate_limited`) the coarse secondary ones — consulted only AFTER a failed
  check, so a correct password / valid key is never refused by them: the peer is nginx's
  forwarded client on the EIP path (uvicorn `--proxy-headers`) but the NLB's private IP for
  everyone behind the NLB. Unknown name and wrong password stay one 401; public registration
  never discloses a taken username / email. Password policy (`users.validate_password`;
  `auth_password_policy` `strict` = `auth_password_min_length` 12, not the username / email,
  not `COMMON_PASSWORDS`; `relaxed` = length only; floor 6; advertised as `password_policy`
  on `GET /api/auth/status`) applies to registration and admin resets only. The session cookie is signed with the random
  persisted `data/session_secret` (`LAUNCHPAD_SESSION_SECRET` override), never the admin
  password, and carries `users.session_epoch` (bumped on every password change; the admin's
  is a password digest) so a new password ends that account's other sessions. `users.apply_patch` / `delete_user` refuse to remove
  the last active admin row (409 `users.last_admin`; the config admin does not count) and a
  self-delete (409 `users.self_delete`). Tests reset the guard around every test
  (`conftest.reset_login_guard`). Contract: `docs/cn/interfaces/users-and-ownership.md` §8.

## Independent CN distribution environments

The distribution example uses `MemoryDomainMode=dedicated` and `console_entry=public_nlb`.
The former flows through `LAUNCHPAD_MEMORY_DOMAIN_MODE` into CDK context
`memory_domain_mode`; domain stack, domain name, admin role and SSM discovery path are
suffixed/scoped to the nonempty environment. Existing configs default to `shared` and keep
D17 naming unchanged. Never change an existing environment's mode without migrating its
indices, updating workspace resources and re-publishing Agents that embed KB/Memory
endpoints. The NLB is the existing separate CloudFormation stack, orchestrated by
`scripts/deploy_cn.py`; it must pass target-health and unauthenticated-API checks before
`platform_ready`. See `docs/cn/distribution.md` for modes, migration and cleanup.

## Frontend conventions

React + Vite + `react-router-dom`, TypeScript strict. **The V2 console is the only
console** (the classic console was retired 2026-10-17, r13-a): native pages live under
`src/v2/pages/*` on the `/v2/*` routes in `src/App.tsx`, and every classic URL (`/`,
`/agents`, `/chat`, `/evaluation?view=…`, `/registry`, …) is a redirect to its V2
counterpart through the module's `classicUrl` helper (`src/v2/pages/<module>/classicUrl.ts`,
`src/lib/evaluation-url.ts`), so deep links keep working — add a mapping there, never a
page, when a classic link must land somewhere new. The only non-V2 pages left are the agent
flows without a native twin (`src/pages/CreateAgent.tsx` for the bare `/agents/new`
system-presets wizard, `/agents/import` and a system preset's `/agents/:id/edit`;
`src/pages/CreateAgentStudio.tsx` for `/create/studio`); they render inside
`<V2Shell classic />` on the V2 palette (`v2/v2-classic.css`). Complex pages expose
**sub-pages via a `?view=` query param** (e.g. `/v2/agents?view=detail&id=`, Gateway's
`?view=gateway|tools`) rather than nested routes — follow that pattern for new
sub-surfaces. `src/lib/api.ts`
is the single typed client for the backend; keep its interfaces in sync with the FastAPI
schemas. All user-facing strings are i18n keys with **en + zh-CN parity enforced** by
`scripts/i18n_check.py`. Chinese copy uses **full-width punctuation** (`，：；？！（）`)
wherever a mark touches CJK text; `scripts/i18n_zh_punct.py --check` gates it and `--fix`
converts mechanically (placeholders, backticks, URLs, ARNs and Latin-only fragments are
never touched).

## Conventions & gotchas

- **All documentation is written in English** (per the launchpad spec index), even
  though the product UI and top-level docs are bilingual.
- **`AGENTS.md` mirrors this file's body** (everything below the title, above its Trellis
  block) for non-Claude agents — update both in the same commit;
  `backend/tests/test_agents_md_mirror.py` fails on drift.
- Python: ruff (line length 100, target py312, rules `E,F,I,W,UP,B`); FastAPI routers
  live in `app/routers/` (console `/api`) and `app/routers/public_api.py` (public `/v1`);
  errors go through `app/core/errors.register_error_handlers`.
- `bedrock-agentcore` is a **preview SDK pinned to `1.17.*`** — treat API shapes as
  volatile and keep the volatility inside the `agentcore/` wrappers.
- **Hermetic quality suites (N12-obs58).** `tests/faults/` injects upstream faults into the
  stubbed clients (`bedrock_5xx`, `throttle`, `read_timeout`, `sdk_error_frame`,
  `worker_replay_5xx`, `worker_judge_5xx`, …; scenarios that only hold once a shared transient
  classifier lands are `xfail(strict)` on a behaviour probe); `tests/test_permission_matrix.py`
  is the living role × kind × operation permission spec (700 cells, both visibility modes —
  a code / spec disagreement is listed in `DISAGREEMENTS`, never silently matched);
  `tests/contract/test_openapi_schemathesis.py` property-tests the hermetic `GET` surface
  against the OpenAPI (no 5xx, 2xx schema, the error envelope on every 4xx). Every integer
  query parameter is bounded by `core/db.SQLITE_MAX_INT`. Round 16 adds the **model /
  property suites** (`tests/model/` — Hypothesis `RuleBasedStateMachine`s for the architect
  assistant, the Runtime canary and Skill Lab; `tests/property/` — two-client chat
  concurrency, the Cedar simulator ↔ runtime-guard differential and the shared ledger scan
  `ledger_invariants.violations`): every invariant cites its contract clause, a reproduced
  failure stays a strict `xfail` register row (`N16-obs*`), and the soak variants
  (`@pytest.mark.slow`, 10× steps) run only with `LAUNCHPAD_SOAK=1`. **Lifecycle graphs**
  (`tests/graph/`, r16-o): one YAML graph per entity whose every edge cites its contract
  sentence (or carries a register gap row), a prime-path / edge-pair generator and one hermetic
  executor world per graph walking them through the real routes; `make graph-coverage` writes
  `docs/cn/reports/graph-coverage.md` for the board (testing.md §6.7). **Round 17 loop engine**
  (r17-b, testing.md §6.8): seeded random walks over every graph (`tests/graph/walks.py`, 8 seeds
  per graph in verify, 200 under `LAUNCHPAD_SOAK=1`, failures shrunk on fresh worlds to a minimal
  edge sequence + `repro-hash`), bug-neighbourhood paths around every pinned edge
  (`neighbourhood.py`), cross-graph walks on the reference graph's dependencies
  (`test_graph_crossgraph.py`) and the loop-round ledger `tests/graph/loop_rounds.yaml` whose
  rule `make graph-coverage` prints (a zero-yield round switches method; the loop stops after
  three consecutive zero-yield rounds); `make graph-soak` is the long run; round 6 (r17-l, testing.md §6.9) adds the
  **fault-injected walks** — the recorded seam error shapes (`tests/fixtures/seams/`)
  served at the data-plane seams mid-walk (`tests/graph/faults.py`, `fault_seams.py`,
  `test_graph_fault_walks_<graph>.py`), with the half-write / envelope / recovery oracles
  and symptom-keyed pins (the seeded fault walks are NOT in `make verify` since r17-s2 —
  `make graph-faults`; the deterministic fault grids and pins stay in the gate). A failing walk is a
  FINDING — register + strict pin, never a weaker oracle. r17-ws (testing.md §6.10) adds the
  eighth graph, `workspace` (the bootstrap job stage by stage through the real
  `execute_bootstrap_job`, interrupted / resumed between every stage, with a fault world that
  injects AccessDenied / AssumeRole failure / region-unsupported / interruption at every stage)
  and the **permission contract**: `services/workspace_permissions.py` declares the IAM action +
  resource every bootstrap stage issues, and `tests/consistency/test_workspace_permissions.py`
  evaluates it against the rendered EC2 template role for a workspace region (N17-obs112 is
  its strict pin). r17-bl added the ninth graph, `policy_principal` (E07), rewritten 2026-10-28
  for the Global AgentCore Policy model (contract amendment 11): engine create → policy publish →
  attach gateway A (ENFORCE) → attach gateway B (LOG_ONLY) → a decision per gateway → detach A →
  A unguarded. Contract:
  `docs/cn/interfaces/testing.md` (§6).
  **Round 16:** `tests/test_creation_matrix.py`
  (pairwise method × option cells: create 2xx ⇒ the generate stage dry-runs clean, else a 4xx
  with no row) and `tests/test_fail_early_guard.py` (every `raise` in `app/deployer/*.py` is
  classified in `tests/fail_early_registry.py` as moved-to-creation or allowed-late). What a
  method can render is declared ONCE on its `MethodCapabilities` at `register_method`
  (`builtin_tools`, `tool_types`, `tool_auth_modes`, `native_tools`, `protocols`, `memory`,
  `knowledge_bases`, `byoc_kinds`, `requires`), enforced by
  `services/method_validation.require_method_support` on every creation entry point (422
  `agent.method_unsupported` / 409 `workspace.resource_missing`), published as
  `GET /api/capabilities.methods` and frozen in `tests/fixtures/method_capabilities.json` for the
  wizard's vitest. Contract: `docs/cn/interfaces/testing.md`.
- `backend/samples/frontdesk_agent` is a vendored / demo asset with its
  **own `CLAUDE.md` and conventions** — don't apply this file's rules to it
  blindly, and prefer editing the platform-side integration over the vendored code.
