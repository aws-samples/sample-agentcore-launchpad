# Design — Existing Gateway Policy Management

## 1. Problem and design principles

Launchpad currently provisions and governs one configured `launchpad-gw`.
Registry synchronization is API-only and creates one record per target, while
Governance cannot discover other Gateways or manage their Policy lifecycle.

The design adds account/region-wide Gateway discovery and safe Policy
management while preserving these invariants:

1. AWS is the source of truth for Gateway, Registry, Policy, and telemetry.
2. A Registry record is a catalog/approval object, not an authorization rule.
3. A Harness attaches a whole Gateway; Cedar policies authorize actions.
4. External IAM roles and identity providers are never silently modified.
5. Every mutating request rereads live AWS state and uses optimistic conflict
   checks.
6. Policy changes enter LOG_ONLY before enforcement.
7. Destructive Policy/Engine lifecycle operations are out of scope.

## 2. Information architecture

Keep `/governance` as the top-level route and use the existing query-view
convention:

| View | URL | Purpose |
|---|---|---|
| Gateways | `/governance` | Live Gateway inventory, management state, authorizer, Registry and Policy summary |
| Gateway detail | `/governance?view=gateway&gateway=<id>` | Targets/actions, Registry publication, engine attachment, rollout state |
| Policy editor | `/governance?view=policy&gateway=<id>&policy=<id?>` | Template/custom/NL authoring, findings, diff, candidate workflow |
| Decisions | `/governance?view=decisions&gateway=<id>` | AWS LOG_ONLY/ENFORCE evidence with trace links |
| Audit | `/governance?view=audit&gateway=<id>` | Immutable mutation history and eligible rollback actions |
| Tools | `/governance?view=tools` | Existing tool catalog and builtin Code Interpreter/Browser demos |

The default is an operational Gateway table, not a marketing/dashboard page.
Selecting a row opens the Gateway detail view. Registry receives one
`IMPORT GATEWAY` entry that deep-links to the Governance Gateway inventory;
the import implementation has one owner in Governance.

## 3. Backend boundaries

### 3.1 Preview SDK wrapper

Add `backend/app/services/agentcore/policy.py`. It owns all volatile AgentCore
request/response shapes:

- paginated Gateway and target discovery
- Gateway tag reads/writes
- Policy Engine list/get/create
- Policy list/get/create/update
- Policy generation start/get/assets
- update-Gateway request reconstruction
- waiters for Gateway READY and Policy/Engine ACTIVE or failed states

The wrapper receives a control client; it never constructs one. Existing
bootstrap code should reuse wrapper primitives where practical without
changing bootstrap behavior.

`update_gateway_policy_configuration()` must build its request from a fresh
`GetGateway` response and preserve every supported existing field:
authorizer, role, protocol, exception level, description, transforms,
interceptors, protocol configuration, KMS, and WAF configuration. It changes
only `policyEngineConfiguration`. This isolates replace-style preview API
semantics and is covered by a full payload-preservation test.

### 3.2 Governance orchestration

Add `backend/app/services/governance.py` for product rules:

- management-tag enforcement
- shared-engine impact analysis
- Registry preview/import/migration
- action discovery and verification status
- IAM preflight result normalization
- optimistic conflict checks
- Policy authoring and candidate/cutover/rollback state machines
- audit journal writes

Routers validate HTTP inputs and delegate to this service. They do not call
boto3 directly.

### 3.3 Telemetry adapter

Add a policy-decision projection to
`backend/app/services/observability.py`, or a small sibling
`policy_observability.py` using its bounded Logs Insights runner and cache.

The adapter owns one normalized contract:

```python
PolicyDecisionView = {
    "at": str,
    "gateway_id": str,
    "gateway_arn": str,
    "engine_id": str | None,
    "policy_id": str | None,
    "principal": str,
    "action": str,
    "outcome": "ALLOW" | "DENY",
    "engine_mode": "LOG_ONLY" | "ENFORCE" | None,
    "policy_mode": "LOG_ONLY" | "ACTIVE" | None,
    "trace_id": str | None,
    "session_id": str | None,
    "source": "aws",
}
```

AgentCore Policy telemetry attribute names are preview-sensitive. Before
implementation, capture one real ALLOW and DENY span in both LOG_ONLY and
ENFORCE and put only the tolerant attribute aliases in the adapter. Query
strings remain bounded to `1h|6h|24h|7d`, validate Gateway IDs before
interpolation, and use the existing 60-second cache with `force=true`.

The existing SQLite demo decision endpoint remains available as
`source=demo`; it is not merged invisibly with AWS production decisions.

## 4. AWS discovery and management state

### 4.1 Gateway discovery

`ListGateways` is paginated, filtered to `protocolType=MCP`, then enriched with:

- `GetGateway`
- `ListTagsForResource`
- paginated `ListGatewayTargets`
- attached Policy Engine summary
- number/names of other Gateways referencing that engine
- Registry record and legacy-record matches by remote Gateway URL
- Harness attachability

List results may use a short cache. Gateway detail and every mutation preflight
must bypass that cache.

### 4.2 Management tags

Use Gateway tags as the durable onboarding marker:

```text
agentcore-launchpad:managed = true
agentcore-launchpad:managed-by = agentcore-launchpad
```

`Manage` writes only these keys. `Unmanage` removes only these keys and does
not alter Registry, Policy, Engine, or Gateway configuration.

An unmanaged Gateway is fully readable. Registry and Policy mutation endpoints
return `409 governance.gateway_not_managed`.

### 4.3 Shared Policy Engine

The impact set is computed live by listing Gateways in the configured
account/region and matching `policyEngineConfiguration.arn`.

- Existing attachment: preserve and manage that engine.
- No attachment: create a dedicated engine, then attach it in LOG_ONLY.
- Shared engine: every mutation request includes
  `acknowledged_gateway_ids`; the backend compares it to the live impact set.
  A stale or incomplete set returns `409 governance.shared_engine_changed`.
- Launchpad never replaces one attached engine with another.

## 5. IAM attachment preflight

The Gateway execution role needs scoped access to evaluate the selected Engine.
Use IAM policy simulation against the role ARN for:

```text
bedrock-agentcore:GetPolicyEngine
bedrock-agentcore:AuthorizeAction
bedrock-agentcore:PartiallyAuthorizeActions
```

The result is `pass|fail|unknown`. `fail` and `unknown` block attachment.
Return a least-privilege statement scoped to the Engine and Gateway ARNs.
Launchpad never calls `PutRolePolicy`, `AttachRolePolicy`, or otherwise edits
the role.

If policy simulation itself is denied, report the missing Launchpad operator
permission separately from the Gateway role remediation.

## 6. Action discovery

Each action has:

```json
{
  "name": "target___tool",
  "target_id": "...",
  "target_name": "...",
  "description": "...",
  "input_schema": {},
  "verified": true,
  "source": "control_schema|live_tools_list|manual"
}
```

Discovery order:

1. Exact actions from static Lambda/OpenAPI/Smithy/MCP schemas returned by
   `GetGatewayTarget`.
2. Authenticated live `tools/list` enrichment for:
   - AWS_IAM Gateways callable with backend credentials.
   - `launchpad-gw` using its managed demo credentials.
3. Manual exact action entry for external CUSTOM_JWT dynamic targets.

Manual actions are stored only in the submitted Cedar/audit snapshot, marked
unverified in the editor, and never normalized or guessed. The UI provides an
external `tools/list` command template but never accepts an external JWT.

## 7. Registry model and migration

### 7.1 Gateway-level record

One AgentCore Gateway maps to one MCP Registry record:

- record name defaults to the Gateway name
- MCP remote URL is the Gateway URL
- tool descriptor aggregates every discovered target/tool
- description identifies the Gateway and target count

Do not add non-standard fields to the MCP server schema. Live Gateway identity
is resolved by matching the unique remote URL to discovered Gateways.

Before import, return a preview:

- proposed record name/descriptors
- exact existing Gateway-level match
- name conflict with a different URL
- legacy per-target records sharing the URL
- created/reused/changed/conflicted outcome

New records follow create -> submit -> approve. Re-sync updates only after a
diff preview and never approves. The response reports the actual AWS status.

### 7.2 Legacy records

Legacy records are MCP records sharing the Gateway URL but representing an
individual target. They remain unchanged until:

1. the Gateway-level record is APPROVED, and
2. the operator explicitly selects records and confirms retirement.

Retirement uses the existing Registry deprecate action. It is not part of
automatic sync and does not modify deployed Agent specs.

### 7.3 Harness attachability

`GET /api/registry/attachables` gains:

```json
{
  "record_id": "...",
  "gateway": true,
  "gateway_id": "...",
  "gateway_arn": "...",
  "attachable": true,
  "attachability_reason": null,
  "auth_type": "aws_iam|none|oauth"
}
```

Rules:

- AWS_IAM -> attach with `outboundAuth.awsIam={}`.
- No auth -> attach with `outboundAuth.none={}`.
- `launchpad-gw` CUSTOM_JWT -> configured OAuth provider and
  CLIENT_CREDENTIALS scope.
- Other CUSTOM_JWT without a managed provider mapping -> catalog-only.

New Agent specs store the Registry record ID and Gateway ID in `ToolRef.config`.
At deploy time, the backend rereads the record and Gateway and derives auth;
it does not trust a provider ARN supplied by the browser.

Legacy `type=gateway` specs without config continue to resolve to
`settings.resources.gateway_*`, preserving existing agents.

## 8. Policy authoring and rollout

### 8.1 Authorization models

The editor requires one explicit model:

- `allowlist` (default): explicit permits; unmatched actions default DENY.
- `preserve_traffic`: explicit broad permit plus forbid restrictions; high-risk
  acknowledgement required.
- `custom`: operator supplies complete Cedar.

Principal scaffolding follows Gateway authorizer:

- CUSTOM_JWT -> `AgentCore::OAuthUser`
- AWS_IAM -> `AgentCore::IamEntity`

All resource clauses use the exact selected Gateway ARN. Generated examples
never use wildcard Gateway resources.

### 8.2 Create and update

- New policies are created with `enforcementMode=LOG_ONLY`.
- Existing LOG_ONLY policies may be updated in place after checking
  `updatedAt`.
- Editing an ACTIVE policy creates a new LOG_ONLY candidate. The original
  remains ACTIVE.
- Generated Cedar is copied into the same review/diff flow; generation assets
  never activate directly.

Candidate metadata is kept in the local operation journal:
original policy ID, candidate policy ID, and intended replacement relation.
AWS remains authoritative for each policy's current definition/mode.

### 8.3 Evidence gate

Promotion checks the selected evidence range for at least one AWS decision
matching the candidate Policy ID.

No-evidence override requires:

- exact Gateway name
- non-empty reason
- current live Gateway/Policy timestamps

The override context is journaled.

### 8.4 Conservative cutover

Candidate cutover order:

1. Recheck original, candidate, Gateway, Engine, and shared impact set.
2. Set candidate `ACTIVE`.
3. Wait for candidate ACTIVE.
4. Set original `LOG_ONLY`.
5. Wait for original ACTIVE status with LOG_ONLY enforcement mode.

If step 4/5 fails, both policies remain ACTIVE. Record `partial` and expose an
idempotent retry that completes only the second transition.

Rollback order is conservative in reverse:

1. Set original ACTIVE and wait.
2. Set candidate LOG_ONLY and wait.

No policy is deleted.

### 8.5 Gateway mode

Attaching a new Engine always uses Gateway mode LOG_ONLY. Switching to ENFORCE
requires:

- managed Gateway
- READY Gateway and ACTIVE Engine
- IAM preflight pass
- no conflicting operation
- complete shared-engine acknowledgement
- evidence gate or documented override
- typed Gateway-name confirmation

Switching back to LOG_ONLY is always available with ordinary confirmation and
still uses optimistic conflict checks.

## 9. API contracts

All IDs are validated before interpolation or AWS calls. Mutations return an
operation object; Policy/Gateway mutations that wait for AWS settle return
`202`.

### 9.1 Gateway and Registry

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/governance/gateways` | Live MCP Gateway summaries |
| GET | `/api/governance/gateways/{gateway_id}` | Enriched detail, impact, actions, IAM and Registry state |
| POST | `/api/governance/gateways/{gateway_id}/manage` | Add management tags |
| DELETE | `/api/governance/gateways/{gateway_id}/manage` | Remove only management tags |
| GET | `/api/governance/gateways/{gateway_id}/registry-preview` | Diff and migration preview |
| POST | `/api/governance/gateways/{gateway_id}/registry-import` | Create/reuse/update and submit |
| POST | `/api/governance/gateways/{gateway_id}/retire-legacy-records` | Explicit legacy retirement |

### 9.2 Engine and Policy

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/governance/gateways/{gateway_id}/engine` | Create/attach LOG_ONLY or adopt existing |
| GET | `/api/governance/gateways/{gateway_id}/policies` | Live engine and policy detail |
| POST | `/api/governance/gateways/{gateway_id}/policies` | Create LOG_ONLY policy |
| PUT | `/api/governance/gateways/{gateway_id}/policies/{policy_id}` | Update LOG_ONLY or create candidate for ACTIVE |
| POST | `/api/governance/gateways/{gateway_id}/policies/{policy_id}/promote` | Candidate cutover or LOG_ONLY -> ACTIVE |
| POST | `/api/governance/gateways/{gateway_id}/policies/{policy_id}/rollback` | Snapshot/candidate rollback |
| POST | `/api/governance/gateways/{gateway_id}/mode` | Gateway LOG_ONLY/ENFORCE transition |
| POST | `/api/governance/gateways/{gateway_id}/generations` | Start NL generation |
| GET | `/api/governance/gateways/{gateway_id}/generations/{generation_id}` | Poll and read assets |

### 9.3 Evidence and operations

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/governance/gateways/{gateway_id}/decisions` | AWS decision telemetry |
| GET | `/api/governance/gateways/{gateway_id}/audit` | Local mutation journal |
| GET | `/api/governance/operations/{operation_id}` | Poll async mutation/reconciliation |

Existing `/api/governance/policy-test` remains launchpad-gw-only. Existing
single-Gateway endpoints may be retained as compatibility facades during one
release, then removed after callers move to the scoped contracts.

### 9.4 Optimistic mutation envelope

Mutations carry the relevant observed timestamps and confirmations:

```json
{
  "expected_gateway_updated_at": "...",
  "expected_policy_updated_at": "...",
  "acknowledged_gateway_ids": ["..."],
  "confirmation_name": "gateway-name",
  "override_reason": null
}
```

Mismatch returns `409 governance.concurrent_change` with fresh summaries.

## 10. Audit and operation model

Add `PolicyChange` to `backend/app/models/ledger.py`:

```text
id                    string PK
gateway_id/arn/name   strings
engine_id/arn         nullable strings
policy_id/name        nullable strings
candidate_policy_id   nullable string
operation             string
operator              string
status                pending|running|succeeded|failed|partial|interrupted
before                 JSON
requested              JSON
after                  JSON nullable
expected_updated_at    nullable string
override_reason        nullable text
error                   nullable text
created_at/started_at/completed_at timestamps
```

`before`, `requested`, identifiers, operator, and override reason are immutable
after insertion. Only progress/result fields change. Current state is always
reread from AWS.

The operator is the configured console username when console auth is enabled,
otherwise `local-operator`.

One running Policy/Gateway mutation per Gateway is allowed. A second returns
`409 governance.operation_in_flight`.

On startup, `pending|running` operations are reconciled, not blindly replayed:

- live state equals requested state -> succeeded
- conservative candidate partial state -> partial/retryable
- otherwise -> interrupted, requiring explicit retry

Create calls use operation-derived client tokens where the SDK supports them.

## 11. Frontend component boundaries

Replace the monolithic Governance implementation with:

```text
pages/Governance.tsx                 query-view router and shared header
pages/governance/GatewayListView.tsx
pages/governance/GatewayDetailView.tsx
pages/governance/PolicyEditorView.tsx
pages/governance/DecisionView.tsx
pages/governance/AuditView.tsx
pages/governance/ToolsView.tsx       existing demos moved intact
pages/governance/types.ts
```

Add typed calls to `src/lib/api.ts`; components do not cast raw payloads.

Important UI states:

- unmanaged / managed tag status
- READY / transitional / failed Gateway and Policy states
- attached Engine mode and each Policy mode shown separately
- shared-engine affected Gateway warning
- IAM preflight pass/fail/unknown with copyable remediation JSON
- verified/unverified action labels
- cataloged vs Harness-attachable
- evidence count/range and stale-data refresh
- candidate/original relation and partial-cutover recovery

Confirmation dialogs show exact affected resources and never hide a shared
impact set behind a generic warning.

## 12. Error model

Add stable codes including:

```text
governance.gateway_not_found
governance.gateway_not_ready
governance.gateway_not_managed
governance.gateway_unsupported
governance.concurrent_change
governance.operation_in_flight
governance.shared_engine_changed
governance.iam_preflight_failed
governance.iam_preflight_unknown
governance.registry_name_conflict
governance.registry_record_not_approved
governance.action_unverified
governance.policy_not_found
governance.policy_not_settled
governance.evidence_required
governance.confirmation_mismatch
governance.cutover_partial
```

AWS status reasons and validation findings are preserved in `detail` but
bounded before persistence/display.

## 13. Compatibility and migration

- `make bootstrap` remains idempotent and continues provisioning
  `launchpad-gw`, `launchpad_pe`, and its current policies.
- The new discovery view automatically sees those resources.
- Existing target-level Registry records remain valid until explicit
  retirement.
- Existing Agent specs with config-less Gateway refs retain the shared
  launchpad-gw fallback.
- The current fixed role tests remain available only on launchpad-gw.
- No generated config migration is required.
- The new audit table is created by existing `Base.metadata.create_all`; no
  destructive SQLite migration is needed.

## 14. Operational safety and rollback

- Listing and opening resources is read-only.
- Managing a Gateway changes tags only.
- Registry import never approves.
- Engine attachment starts LOG_ONLY.
- Policy creation starts LOG_ONLY.
- Enforce transitions are evidence/confirmation gated.
- Every mutation snapshots live state before execution.
- Rollback restores policy definition/mode or conservative candidate relation;
  it never deletes resources.
- Unmanage is not cleanup.
- External IaC may revert tags or Gateway configuration; the next live read
  reflects that drift and optimistic checks prevent stale writes.

## 15. Verification strategy

### Hermetic backend

- discovery pagination/enrichment and tag state
- shared-engine impact changes
- IAM preflight pass/fail/unknown and remediation statement
- Gateway update payload preservation
- action discovery for static/dynamic target shapes
- Registry preview/import idempotency, name conflict, and legacy migration
- attachability/auth derivation and legacy AgentSpec fallback
- Policy create/update/candidate/cutover/partial/retry/rollback
- optimistic conflicts and operation mutex
- generation never activates
- telemetry normalization aliases and query validation
- audit immutability of snapshot fields

### Frontend

- query-view navigation and deep links
- unmanaged controls disabled
- Registry and attachability states
- shared-engine and IAM warnings
- LOG_ONLY/ACTIVE vs LOG_ONLY/ENFORCE mode distinction
- evidence override validation
- candidate partial-recovery state
- English/zh-CN parity

### Real AWS

Add an opt-in `backend/scripts/e2e_gateway_policy_management.py`:

1. discover and manage a disposable MCP Gateway
2. create/attach an Engine in LOG_ONLY
3. create a LOG_ONLY policy
4. send test traffic and observe a decision
5. promote with evidence
6. create a candidate, cut over, and rollback
7. publish the Gateway-level Registry record
8. unmanage without deleting AWS resources

Run `make verify` as the required final gate. The E2E script is documented but
is not part of `make verify`.

## 16. Known implementation research gate

Before coding the telemetry projection, capture real AgentCore Policy spans and
record the exact attribute aliases in task research. This is the only
preview-shape research item; it does not change the product contract above.
