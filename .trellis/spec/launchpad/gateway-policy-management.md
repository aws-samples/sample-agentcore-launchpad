# Existing Gateway Policy Management

## Scenario: Govern an existing AgentCore MCP Gateway

### 1. Scope / Trigger

Use this contract when changing Gateway discovery, Gateway-level Registry
records, Harness attachment, Policy lifecycle, decision evidence, or the
Governance console. AWS is the source of current Gateway, Registry, Engine,
Policy, and telemetry state. SQLite stores only immutable mutation history and
operation progress.

### 2. Signatures

Core console APIs:

```text
GET    /api/governance/gateways
GET    /api/governance/gateways/{gateway_id}
POST   /api/governance/gateways/{gateway_id}/manage
DELETE /api/governance/gateways/{gateway_id}/manage
GET    /api/governance/gateways/{gateway_id}/registry-preview
POST   /api/governance/gateways/{gateway_id}/registry-import
POST   /api/governance/gateways/{gateway_id}/retire-legacy-records
POST   /api/governance/gateways/{gateway_id}/engine
GET    /api/governance/gateways/{gateway_id}/policies
POST   /api/governance/gateways/{gateway_id}/policies
PUT    /api/governance/gateways/{gateway_id}/policies/{policy_id}
POST   /api/governance/gateways/{gateway_id}/policies/{policy_id}/promote
POST   /api/governance/gateways/{gateway_id}/policies/{policy_id}/rollback
POST   /api/governance/gateways/{gateway_id}/mode
GET    /api/governance/gateways/{gateway_id}/decisions
GET    /api/governance/gateways/{gateway_id}/audit
GET    /api/governance/operations/{operation_id}
```

The `policy_changes` table stores Gateway/Engine/Policy identifiers, operator,
operation, before/requested/after JSON, expected timestamp, override reason,
status, error, and timestamps. Identifier and request snapshots are immutable
after insertion.

### 3. Contracts

- Listing and detail calls are read-only live AWS reads.
- Managed state is exactly the two Launchpad-owned Gateway tags. Unmanage
  removes only those tags.
- One Gateway maps to one MCP Registry record. Registry approval controls
  catalog visibility; it never changes Gateway targets or Policy.
- A Gateway record exposes the full Gateway. Harness auth is derived
  server-side: `AWS_IAM -> awsIam`, no auth -> `none`, managed
  `launchpad-gw` CUSTOM_JWT -> configured OAuth. Other CUSTOM_JWT Gateways are
  catalog-only.
- Mutations re-read live state and compare
  `expected_gateway_updated_at`/`expected_policy_updated_at`.
- AgentCore Policy create/generation `clientToken` values are stable per
  journal operation and must satisfy the preview SDK's 33-character minimum.
  The AgentCore wrapper prefixes the 32-character `PolicyChange.id`; callers
  must not change the journal primary-key format to satisfy an SDK constraint.
- Shared Engine mutations require the complete live
  `acknowledged_gateway_ids` set.
- New Engine attachments and policies start `LOG_ONLY`.
- Editing ACTIVE creates a LOG_ONLY candidate. Cutover activates candidate
  first; rollback reactivates original first.
- Audit rollback refreshes the live Gateway and Policy immediately before the
  request, then sends those current timestamps with the selected `audit_id`.
  The backend restores `before.policy` for in-place edits and
  `before.policies.selected` for standalone promotion; it never substitutes a
  different audit entry when `audit_id` is present.
- The Tools catalog treats live `launchpad-gw` discovery as optional. Cognito
  and Gateway SDK failures must be normalized to `AppError`; `GET /api/tools`
  returns builtins plus a stable `gateway_error` instead of failing the whole
  view. Catalog reads never repair or reset AWS credentials implicitly.
- The Tools view owns only the tool catalog and Builtin Tool demos. It does not
  render the legacy fixed-`launchpad-gw` Cedar preview or the local demo
  decision ledger; selected-Gateway Policy state belongs to Policy Editor,
  Decisions, and Audit.
- The Browser demo starts a five-minute `1280x720` AgentCore Browser session,
  returns a server-generated SigV4 Live View URL, and retains the remote
  session after Playwright automation disconnects. The frontend must render
  that URL with the official `BrowserLiveView` component using the exact
  returned viewport. `DELETE /api/demos/browser/{session_id}` stops a retained
  demo session, and backend expiry is the leak-prevention fallback.
- The DCV static asset copy must preserve the SDK's `dcv/` and `lib/`
  subdirectories. Publish the `dcvjs-esm` tree at both the root SDK path and
  the Governance route-relative SDK path because decoder workers can resolve
  from the active SPA route; flattening the tree leaves Live View stuck while
  worker requests return `404`.
- The Browser demo lists Browser and Browser Profile resources live from the
  control plane. Enabling Web Bot Auth requires an explicitly selected READY
  custom Browser whose live `browserSigning.enabled` is true; the backend
  revalidates that state before starting the session and otherwise uses
  `aws.browser.v1`. The demo never creates, updates, or deletes Browser
  resources.
- Selecting a READY Browser Profile passes its identifier through
  `profileConfiguration` to restore cookies and local storage. Saving session
  state back to that Profile is a separate, default-off choice and must happen
  while the session is active, before stop. A failed save must not leak the
  Browser session.
- The Browser navigation URL and Code Interpreter Python source are controlled
  operator inputs with the existing backend length and URL safety validation;
  fixed sample values are defaults, not hard-coded execution payloads.
- Mutation response: `{"operation": GovernanceOperation}`. Polling uses the
  same envelope.
- Generation start response:
  `{"operation": ..., "generation_id": ..., "status": ...}`.
- **Policy decision evidence comes from CloudWatch metrics, not spans.**
  `AWS/Bedrock-AgentCore` publishes `AllowDecisions` / `DenyDecisions` and the
  determining/mismatch family **by default** — no per-gateway enablement. Spans
  are a separate, opt-in channel (see below). `app/services/governance_evidence.py`
  owns the metric read; `available=false` means the channel is unreadable and
  carries the AWS error code, while a readable channel with a quiet window is
  `available=true` with `evidence_count=0`. These are three distinct states and
  must not collapse into one message. Never infer rollout evidence from demo rows.
- **Two counting rules for the metric channel; both are load-bearing.**
  (1) AWS publishes many *overlapping* projections of one decision event
  (`{OperationName}`, `{TargetResource,OperationName,Mode}`,
  `{Policy,TargetResource,OperationName,Mode}`, …), so selections must match an
  **exact** dimension-name set — a subset match inflates counts several-fold.
  (2) Projection availability differs per operation, so the projection is chosen
  *per operation* from a preference chain: `AuthorizeAction` publishes a
  gateway-level stream (one decision per call), while `PartiallyAuthorizeActions`
  was observed publishing **only** `ToolName` projections (one decision per
  call/tool pair). A single fixed projection silently reports zero denials on a
  gateway whose denials all come from `PartiallyAuthorizeActions`. Each count
  therefore carries the `basis` it was measured in (`per_call` / `per_tool`),
  derived from the projection rather than asserted.
- **Breakdowns are not a decomposition.** `by_policy` only covers decisions that
  had a determining policy and `by_tool` only per-tool authorization, so they need
  not sum to `evidence_count`. Do not add them together, and do not let the UI
  imply they partition the total.
- **`decisions[]` requires spans and is never synthesized.** Metric dimensions
  cannot express principal, decision reason, or trace id, so the array stays empty
  while `source` is `metrics`.
- **Span-derived rows (`governance_spans.py`) come from
  `AgentCore.Gateway.InvokeTool`, not the Policy span.** That SERVER span carries
  `tool.name` *and* `aws.agentcore.policy.authorization_decision` together; the child
  `AgentCore.Policy.*` span (joined on `parentSpanId`) adds
  `determining_policies`, `mismatched_policies`, and the undocumented-but-captured
  `aws.agentcore.policy.log_only_matched_policies` — which shows what a LOG_ONLY
  *candidate* would have matched from an ENFORCE-mode span, and is the one thing the
  metric channel cannot express. `session.id` lives on the runtime/mcp spans and
  needs a second pass joined on `traceId`. Select `@message` and parse the JSON:
  Logs Insights flattens arrays into `attributes.x.0`, which cannot express a policy
  id list of unknown length.
- **`principal` is structurally unavailable, not merely unimplemented.** No span in a
  captured 31-span trace carries any principal/actor/subject attribute, because the
  Harness authenticates to the Gateway with an OAuth M2M client credential — the
  request has no human subject. `principal` and `policy_mode` (spans carry only the
  Gateway attachment mode) are hardcoded `None` and rendered as explained-absent.
  The local demo ledger keeps its own `principal`; never conflate the two.
- **`aws.agentcore.policy.authorization_reason` is documented but UNVERIFIED** — it
  was absent from the captured ALLOW span, and no `AuthorizeAction` DENY can be
  captured under `ENFORCE` (see below). Do not reference it. A test asserts the
  parser source does not mention it.
- **Two evaluation kinds, and the listing one is the common case.**
  `PartiallyAuthorizeActions` denials are list-time *tool availability* decisions:
  under `ENFORCE` the denied tool is filtered out of `tools/list`, so the model never
  sees it and **no `AuthorizeAction` DENY can ever occur**. Rows carry
  `evaluation: invocation | tool_listing` so the two are not presented as the same
  event. Only `denied_tools` become listing rows — expanding `allowed_tools` would
  emit one row per tool per list call for no added information.
- **Spans never redefine `evidence_count`.** Spans are sampled; metrics are exact. A
  span-channel failure degrades to metrics-only with `spans_unavailable_reason` and
  must never turn the endpoint into a 5xx, because the aggregates are what the
  cutover gate reads.
- **The local decision ledger journals authorization results only.**
  `policy-test` returns `ALLOW` / `DENY` / `ERROR`, and **only the first two are
  written to `policy_decisions`** — an error is not a decision, and the ledger is
  audit-facing evidence. The original implementation recorded every `AppError` as a
  `DENY`, so a Cognito outage manufactured a Cedar denial that never happened
  (observed live; two false rows had to be deleted). Classification:
  `gateway.unauthorized` → DENY; `credentials_rejected` / `identity_unavailable` /
  `no_credentials` / `not_bootstrapped` / `bad_response` → ERROR;
  `gateway.rpc_error` → DENY only when it carries the captured policy-denial
  signature (JSON-RPC code `-32002`, or a `Tool Execution Denied` / policy-denied
  message), else ERROR. **Unrecognised failures must fail safe to ERROR** — never to
  ALLOW or DENY. A tool that fails its own validation returns a *successful* MCP
  result with `isError: true`, which stays `ALLOW`: the authorization question was
  answered with a permit.
- **`policy-test` is not pre-filtered, unlike the Harness path.** It issues
  `tools/call` with no preceding `tools/list`, so the Gateway runs `AuthorizeAction`
  per call and a call-time DENY is reachable — the one under `ENFORCE` that a Harness
  can never produce. Note the ALLOW path for `create_payout` really creates a demo
  payout record.
- **The span channel is opt-in per Gateway and bootstrap owns it.** Policy decision
  spans are emitted only after trace delivery is enabled on the attached Gateway —
  a CloudWatch vended-log delivery (`put_delivery_source(logType="TRACES")` →
  `put_delivery_destination(deliveryDestinationType="XRAY")` → `create_delivery`),
  **not** a Gateway field, so enabling it must never call `UpdateGateway`.
  `policy_bootstrap.ensure_gateway_traces()` creates it idempotently inside
  `make bootstrap`, named `<gateway-id>-traces-source` / `-traces-destination` per
  the account's existing convention. Requires Transaction Search first (AWS
  prerequisite) and skips with a reason when it is off. All three write calls can
  raise `ConflictException`, and `describe_deliveries` has no per-name lookup, so
  existence is checked with `get_delivery_*` plus a paginated scan, and a conflict
  is treated as already-present. The step returns a `status`
  (`created`/`present`/`skipped`/`failed`) rather than a bool and **never raises**:
  telemetry must not abort bootstrap, but must not be reported as success either.
- The cutover gate reads `log_only_count`, not `evidence_count` — only LOG_ONLY
  decisions satisfy the documented promotion rule. `evidence_count()` never
  raises: an unreadable channel yields 0 so a legitimate typed override stays
  reachable during a CloudWatch outage.
- **The evidence/override gate applies to cutovers only.**
  `_assert_evidence_or_override` guards policy promote/rollback and
  `gateway_mode → ENFORCE`. Attaching an engine, creating or updating a LOG_ONLY
  draft, and starting a generation are NOT gated. The editor may still collect a
  justification for those, and every `_new_change` call — all six — passes
  `override_reason=request.override_reason` so it lands in the audit column
  instead of only inside `requested` (it used to be dropped on create/update/
  attach/generation, so the audit entry read `OVERRIDE REASON -`, ISSUE-010).
- **A disabled primary action states its unmet preconditions.** The policy
  editor derives `saveBlockers` / `transitionBlockers` lists (rendered as
  `governance.blockers.*`) rather than a bare boolean, because a disabled button
  swallows the click and reads as "the action did nothing". The override and
  gateway-name fields say which actions actually require them.

### 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| Gateway absent/non-MCP | `governance.gateway_not_found` or `gateway_unsupported` |
| Mutation on unmanaged Gateway | `governance.gateway_not_managed` |
| Live `updatedAt` differs | `governance.concurrent_change` |
| Shared Engine acknowledgement stale | `governance.shared_engine_changed` |
| IAM simulation deny/unknown | `governance.iam_preflight_failed`/`unknown` |
| Registry name points to another URL | `governance.registry_name_conflict` |
| Legacy retirement before Gateway record approval | `governance.registry_record_not_approved` |
| Promotion/rollback/ENFORCE has no evidence and no typed override | `governance.evidence_required` |
| LOG_ONLY draft create/update without evidence or override | accepted (no cutover gate) |
| Confirmation name differs | `governance.confirmation_mismatch` |
| Second mutation while one is running | `governance.operation_in_flight` |
| Live Policy changes after the rollback preflight | `governance.concurrent_change` |
| Selected audit entry has no compatible Policy snapshot | `governance.rollback_unavailable` |

### 5. Good/Base/Bad Cases

- Good: manage a READY disposable Gateway, attach in LOG_ONLY, create a
  LOG_ONLY policy, observe decisions, then explicitly promote.
- Base: read an unmanaged Gateway and preview its catalog without changing AWS.
- Bad: trust browser-supplied OAuth/provider data, treat a Registry target as
  an authorization boundary, replace an attached Engine, or auto-edit an
  external Gateway IAM role.

### 6. Tests Required

- Wrapper tests assert pagination, wait failures, and complete UpdateGateway
  payload preservation.
- Gateway tests assert read-only discovery, tag-only management, exact action
  names, shared impact, and IAM pass/fail/unknown.
- Registry/Harness tests assert one Gateway record, idempotent import,
  explicit legacy retirement, server-side auth, multiple Gateways, and legacy
  config-less refs.
- Lifecycle tests assert LOG_ONLY update, ACTIVE candidate, conservative
  cutover/partial retry/rollback, timestamp conflict, mutex, and audit
  immutability.
- Wrapper tests pass a 32-character journal ID through Engine, Policy, and
  generation creates and assert an SDK-valid stable `clientToken`.
- Standalone lifecycle tests assert
  `LOG_ONLY -> ACTIVE -> audit rollback -> LOG_ONLY` using the promotion
  operation ID and the live post-promotion Policy timestamp.
- Tools tests assert Cognito authentication failures become stable domain
  errors and the catalog still returns builtin tools.
- Browser demo tests assert that the Live View URL and matching viewport are
  returned, the session remains active after navigation, and explicit stop
  releases it.
- Browser configuration tests assert live options mapping, Web Bot Auth
  eligibility validation, Profile restoration, opt-in Profile save before
  stop, and managed-browser defaults.
- Router tests assert operation and generation envelopes match
  `frontend/src/lib/api.ts`.
- Final validation is `make verify`; real AWS runs use the guarded
  `backend/scripts/e2e_gateway_policy_management.py`.

### 7. Wrong vs Correct

#### Wrong

```python
# Browser data chooses credentials and a Registry target is treated as a
# separately attached/authorized tool.
outbound_auth = request.tool.config["outboundAuth"]
```

#### Correct

```python
# Resolve the approved record and live Gateway again, then derive auth from the
# live authorizer and Launchpad-managed provider mapping.
attachments = registry_console.resolve_gateway_attachments(spec.tools)
```

The same rule applies to Policy state: journal snapshots support audit and
rollback, but every current view and mutation preflight reads AWS again.

#### Wrong

```python
# A journal ID is 32 characters and the preview API rejects it client-side.
client.create_policy_engine(clientToken=change.id, ...)
```

#### Correct

```python
# Keep the journal ID stable and normalize only at the AgentCore wrapper.
client.create_policy_engine(clientToken=f"launchpad-{change.id}", ...)
```
