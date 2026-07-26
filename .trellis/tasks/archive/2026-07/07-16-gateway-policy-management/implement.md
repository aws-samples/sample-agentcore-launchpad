# Implement — Existing Gateway Policy Management

## Delivery shape

Implement in ordered slices. Keep the task in planning until this design and
PRD are approved. At execution start, prefer child tasks for slices 1–4 because
each has an independent verification surface:

1. Gateway discovery/onboarding and AWS wrappers
2. Gateway-level Registry records and Harness attachability
3. Policy lifecycle, audit, and telemetry
4. Governance frontend and integration

The parent owns cross-slice contracts, compatibility, final E2E, docs, and
`make verify`.

## 0. Resolve preview telemetry evidence

- [ ] Bootstrap or reuse a disposable Gateway/Engine in real AWS.
- [ ] Capture one Policy ALLOW and DENY in engine LOG_ONLY and ENFORCE.
- [ ] Record the exact `aws/spans` fields and operation names in
      `research/policy-telemetry-shape.md`.
- [ ] Define the bounded alias map and Logs Insights query before implementing
      the decision projection.

Gate: no telemetry parser is written from guessed attribute names.

## 1. AgentCore wrappers and core contracts

- [ ] Add `backend/app/services/agentcore/policy.py` with paginated
      Gateway/target/engine/policy operations, tag helpers, generation helpers,
      status polling, and update-Gateway payload reconstruction.
- [ ] Add `backend/app/schemas/governance.py` for response/request contracts and
      identifier/mode/confirmation validation.
- [ ] Add `PolicyChange` to `backend/app/models/ledger.py`.
- [ ] Add operator identity helper using configured console auth username or
      `local-operator`.
- [ ] Add unit tests for SDK shape normalization, pagination, status failure,
      and preservation of every existing Gateway update field.

Validation:

```bash
cd backend
uv run ruff check app/services/agentcore/policy.py app/schemas/governance.py
uv run pytest tests/test_governance_policy_wrapper.py -q
```

Rollback point: wrapper/contracts only; no router or mutation is exposed.

## 2. Gateway discovery, management, and IAM preflight

- [ ] Add `backend/app/services/governance.py` discovery and detail projections.
- [ ] Add live management-tag read/manage/unmanage operations.
- [ ] Add shared-engine impact analysis over all MCP Gateways.
- [ ] Add IAM simulation and least-privilege remediation statement generation;
      never add IAM mutation permissions.
- [ ] Add Gateway list/detail/manage endpoints to
      `backend/app/routers/governance.py`.
- [ ] Add cache invalidation on manage/unmanage and preserve uncached mutation
      preflights.
- [ ] Test unmanaged mutation rejection, stale shared impact, IAM
      fail/unknown, tag preservation, and account pagination.

Validation:

```bash
cd backend
uv run ruff check app/routers/governance.py app/services/governance.py
uv run pytest tests/test_governance_gateways.py -q
```

Rollback point: remove new endpoints/tags; no Policy configuration has changed.

## 3. Gateway-level Registry import and Agent attachment

- [ ] Extend MCP descriptor construction to aggregate all Gateway targets/tools.
- [ ] Implement Registry import preview keyed by Gateway remote URL.
- [ ] Implement idempotent create/reuse/update/submit outcomes without approval.
- [ ] Detect legacy per-target records and add explicit retirement endpoint,
      gated on the Gateway-level record being APPROVED.
- [ ] Extend `/api/registry/attachables` with live Gateway identity,
      attachability, reason, and outbound-auth type.
- [ ] Update frontend Registry with an `IMPORT GATEWAY` deep link; do not
      duplicate import business logic there.
- [ ] Update Create Agent to show Gateway-level entries, whole-Gateway wording,
      and disabled catalog-only CUSTOM_JWT records.
- [ ] Store `record_id` + `gateway_id` in new `ToolRef.config` payloads.
- [ ] Update Harness deployment to resolve and attach each unique selected
      Gateway using `awsIam`, `none`, or managed launchpad OAuth.
- [ ] Preserve config-less legacy Gateway refs as launchpad-gw.
- [ ] Test name conflicts, descriptor diffing, retirement safety, tampered
      frontend auth config, multiple Gateways, and legacy specs.

Validation:

```bash
cd backend
uv run ruff check app/services/registry_console.py app/deployer/harness.py
uv run pytest tests/test_registry.py tests/test_registry_manage.py \
  tests/test_governance_registry_import.py tests/test_harness_deployer.py -q

cd ../frontend
npm run lint
npx tsc --noEmit
```

Rollback point: retain old attachable response fields and legacy resolver until
all new specs have a durable compatibility path.

## 4. Policy lifecycle and operation journal

- [ ] Implement one-running-operation-per-Gateway mutex.
- [ ] Implement operation creation, immutable snapshot fields, progress/result
      updates, and startup reconciliation.
- [ ] Implement create/adopt/attach Engine in LOG_ONLY with IAM and conflict
      preflights.
- [ ] Implement explicit `allowlist|preserve_traffic|custom` policy authoring;
      all creates use Policy LOG_ONLY.
- [ ] Implement LOG_ONLY in-place update with `updatedAt` conflict protection.
- [ ] Implement ACTIVE edit as candidate creation.
- [ ] Implement evidence-gated candidate cutover, conservative partial state,
      idempotent retry, and inverse rollback.
- [ ] Implement Gateway LOG_ONLY/ENFORCE transitions and documented zero-evidence
      override.
- [ ] Implement NL generation scoped to selected Gateway/Engine; assets feed the
      normal editor and cannot activate directly.
- [ ] Retain launchpad-gw role-test compatibility endpoints.
- [ ] Add tests for every transition and injected failure between multi-step
      operations.

Validation:

```bash
cd backend
uv run ruff check app/routers/governance.py app/services/governance.py
uv run pytest tests/test_governance.py tests/test_governance_operations.py \
  tests/test_governance_policy_lifecycle.py -q
```

Rollback point: Engine and Policy resources are not deleted. Safe rollback is
Gateway LOG_ONLY, Policy LOG_ONLY, or audited definition restoration.

## 5. AWS Policy telemetry

- [ ] Add the normalized Policy decision projection from the research alias map.
- [ ] Add Gateway/policy/range filtering with strict interpolation validation.
- [ ] Add 60-second cache and `force=true` behavior.
- [ ] Keep demo SQLite decisions explicitly labeled `source=demo`.
- [ ] Add trace/session deep links where telemetry supplies identifiers.
- [ ] Test aliases, malformed attributes, pagination/query timeout, cache, and
      no-evidence promotion behavior.

Validation:

```bash
cd backend
uv run ruff check app/services/observability.py
uv run pytest tests/test_governance_decisions.py tests/test_observability.py -q
```

Rollback point: decision view can fall back to a clear unavailable state;
Policy mutation contracts do not infer evidence from local demo rows.

## 6. Governance frontend

- [ ] Split `Governance.tsx` into query-param subviews described in `design.md`.
- [ ] Add all Governance contracts to `frontend/src/lib/api.ts`.
- [ ] Build Gateway inventory/detail with live refresh and managed-state actions.
- [ ] Build Registry preview/import/migration flow.
- [ ] Build Policy editor with templates, exact action picker/manual unverified
      entry, Cedar diff, findings, and generation review.
- [ ] Build separate Engine mode and Policy mode controls.
- [ ] Build evidence view, audit view, typed confirmations, shared impact, IAM
      remediation, partial-cutover recovery, and operation polling.
- [ ] Move existing builtin demos into Tools view without behavior changes.
- [ ] Add English and zh-CN keys together.
- [ ] Add focused component/browser tests for all high-risk states.

Validation:

```bash
cd frontend
npm run lint
npx tsc --noEmit
npm run build
cd ..
python3 scripts/i18n_check.py
```

Visual verification:

- desktop 1440x900: inventory, detail, policy editor, decision and audit views
- mobile 390x844: no clipped identifiers, dialogs, tables, or mode controls
- verify no nested cards and no action available in an invalid state

## 7. Integration, docs, and real AWS E2E

- [ ] Add `backend/scripts/e2e_gateway_policy_management.py`; require explicit
      flags and keep it outside `make verify`.
- [ ] Cover manage -> attach LOG_ONLY -> policy LOG_ONLY -> evidence -> enforce
      -> candidate -> cutover -> rollback -> Registry import -> unmanage.
- [ ] Update `docs/architecture.md`, `docs/api.md`, and
      `docs/troubleshooting.md` in English.
- [ ] Add a launchpad spec for Gateway/Policy management and link it from
      `.trellis/spec/launchpad/index.md`.
- [ ] Verify bootstrap remains idempotent and the existing policy E2E remains
      valid.

Real AWS commands:

```bash
make bootstrap
make dev
cd backend
uv run python scripts/e2e_policy.py
uv run python scripts/e2e_gateway_policy_management.py --confirm-real-aws
```

## 8. Final quality gate

- [ ] Review every PRD acceptance criterion against code/tests/evidence.
- [ ] Run targeted backend and frontend suites once more.
- [ ] Run the canonical repository gate:

```bash
make verify
```

- [ ] Run browser visual QA against the local dev server.
- [ ] Confirm git diff contains no generated `config/launchpad.yaml`, database,
      screenshots, credentials, or unrelated vendored changes.
- [ ] Use `trellis-check`, update the project spec, then commit/archive through
      the normal finish workflow.

## Risk register

| Risk | Mitigation / rollback |
|---|---|
| Preview SDK field drift | Keep shapes in `agentcore/policy.py`; model-shape tests |
| UpdateGateway resets unrelated config | Fresh read + full preservation test |
| Shared Engine blast radius changes | Live impact set + acknowledged IDs + 409 |
| External IaC races Launchpad | `updatedAt` checks before every mutation |
| Missing Gateway role permission | IAM simulation blocks attach; no auto-patch |
| Policy telemetry field drift | Real evidence gate + tolerant central normalizer |
| ACTIVE edit bypasses shadow testing | Candidate Policy; old remains ACTIVE |
| Candidate cutover fails halfway | Conservative ordering; partial/retryable state |
| Registry target records imply false isolation | One Gateway record + explicit whole-Gateway copy |
| External CUSTOM_JWT cannot be called | Catalog-only attachability with explicit reason |
| Browser/API retry duplicates create | operation mutex + client tokens + reconciliation |
| Local audit diverges from AWS | AWS live read is authoritative; journal is audit only |
