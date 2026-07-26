# Existing Gateway Policy Management

## Goal

Let an operator discover an existing Amazon Bedrock AgentCore MCP Gateway,
publish the Gateway and its tool catalog into AgentCore Registry, and safely
create, review, attach, and roll out Cedar policies from the Launchpad console.

The workflow must remove the current API-only `sync-defaults` step while
preserving the separation between the live Gateway, Registry approval, and
Policy enforcement.

## Background

- AWS is the source of truth for Gateway, Registry, and Policy resources.
- `make bootstrap` provisions the Launchpad-owned `launchpad-gw`, its targets,
  a policy engine, and two policies, but does not register Gateway targets in
  Registry.
- `POST /api/registry/sync-defaults` discovers tools from only the configured
  `launchpad-gw` and creates/submits Registry MCP records. The Registry UI has
  no action that invokes this endpoint.
- The Create Agent wizard lists only APPROVED Registry MCP records. A Registry
  record controls catalog availability; it does not configure or authorize the
  underlying Gateway.
- Harness Gateway attachments require explicit outbound auth (`awsIam`,
  `none`, or `oauth`). An external CUSTOM_JWT Gateway is not callable by a
  Harness unless Launchpad also knows an AgentCore Identity OAuth credential
  provider ARN and grant configuration.
- Harness currently attaches the entire configured shared Gateway when any
  `type=gateway` tool is selected. The selected Registry target name does not
  scope the attached Gateway's exposed tools.
- The Governance page can read policies, run two fixed identity tests, and
  generate Cedar candidates from one fixed natural-language prompt. It cannot
  discover Gateways, create/update policies, attach/detach engines, change
  enforcement mode, or activate generated policy assets.
- AgentCore permits one policy-engine configuration per Gateway; an engine may
  contain multiple policies. Gateway mode `LOG_ONLY` evaluates and traces
  without blocking, while `ENFORCE` applies the engine's decisions.
- Cedar policies must reference the exact Gateway ARN. OAuth and IAM Gateways
  require different principal types.
- The pinned SDK exposes two rollout layers: Gateway attachment mode
  `LOG_ONLY|ENFORCE` and per-policy enforcement mode `LOG_ONLY|ACTIVE`.
- `UpdatePolicy` replaces the policy definition in place and AgentCore does not
  expose a policy-version history suitable for one-click rollback.
- The current Governance decision list reads only local rows created by the two
  demo test actions; it is not a complete feed of AgentCore Policy decisions.

## Requirements

- Discover eligible existing MCP Gateways from the configured AWS account and
  region without copying them into the SQLite ledger.
- List every eligible Gateway in the configured account/region, but require an
  explicit operator "manage" action before enabling Registry import or Policy
  mutations for that Gateway.
- Persist onboarding on the Gateway itself with Launchpad-owned AWS tags; do
  not create a local managed-Gateway table.
- Removing management removes only Launchpad-owned tags and never detaches,
  deletes, or edits the Gateway, Policy Engine, policies, or Registry records.
- Show each Gateway's identity, authorizer type, status, targets, attached
  policy engine, and enforcement mode from live AWS state.
- Provide an explicit Registry import/sync action. Importing metadata must not
  silently approve records or alter Gateway targets.
- Represent one AgentCore Gateway as one MCP Registry record containing the
  Gateway endpoint plus its discovered targets and tools. Registry records are
  not target-level authorization boundaries.
- Migrate legacy per-target MCP records non-destructively: create and approve
  the Gateway-level record first, retain legacy records during transition, and
  offer a separate explicit action to deprecate them.
- Never auto-deprecate legacy Registry records; `DEPRECATED` is terminal and
  existing deployed Agents may still reference the old catalog entry.
- Make the Create Agent wizard state explicitly that selecting such a record
  attaches the whole Gateway; Cedar policies control individual tool actions.
- Keep Registry catalog status separate from Harness attachability so an
  approved Gateway record is not presented as callable when outbound auth is
  unresolved.
- Derive Harness attachability as follows: AWS_IAM uses `awsIam`; unauthenticated
  Gateways use `none`; `launchpad-gw` reuses its configured OAuth provider; any
  other CUSTOM_JWT Gateway without a Launchpad-managed provider mapping remains
  catalog-only.
- Do not add external OAuth provider creation, selection, authorization-code,
  token-exchange, or secret management to this MVP.
- Create or reuse a Policy Engine and attach it to a selected Gateway without
  losing the Gateway's existing authorizer or role configuration.
- Before attachment, verify that the Gateway execution role can evaluate the
  selected Policy Engine. If required permissions are missing, block the
  mutation and present a least-privilege IAM statement.
- Never modify an external Gateway execution role automatically.
- If a Gateway already has an attached Policy Engine, onboard that engine
  rather than replacing it. Before any policy mutation, discover every Gateway
  in the account/region that references the same engine.
- For a Policy Engine shared by multiple Gateways, show the full affected
  Gateway set and require an additional explicit confirmation before creating
  or updating a policy.
- Support Cedar policy list, inspect, create, and update operations with clear
  asynchronous AWS status handling and validation findings.
- Persist an immutable local policy-change journal containing Gateway, Engine,
  and Policy identifiers; operator; operation; before/after Cedar and modes;
  AWS result; timestamps; and the observed AWS `updatedAt`.
- Treat journal snapshots as audit and rollback inputs only. Every current
  policy view and mutation preflight must read live AWS state.
- Use the previously observed `updatedAt` as optimistic conflict protection
  before update or rollback so Launchpad cannot overwrite a newer external
  change silently.
- Update an existing LOG_ONLY policy in place. Editing an ACTIVE policy creates
  a separate LOG_ONLY candidate while the original policy remains ACTIVE.
- After candidate evidence passes, cut over by activating the candidate first
  and then moving the original to LOG_ONLY. If the second step fails, leave
  both ACTIVE, report a partial conservative state, and allow an idempotent
  retry.
- Rollback uses the inverse conservative order: reactivate the original before
  moving the candidate to LOG_ONLY. Policies are not deleted.
- Support a safe `LOG_ONLY` to `ENFORCE` rollout and an explicit rollback path.
- By default, require at least one corresponding LOG_ONLY decision in the
  selected evidence window before promotion to ENFORCE.
- Permit a no-evidence override only after the operator types the Gateway name
  and supplies a non-empty reason; record the override and zero-evidence state
  in the immutable audit journal.
- Keep destructive lifecycle operations out of MVP: no Policy deletion, Policy
  Engine detachment, or Policy Engine deletion.
- Safe deactivation consists of moving a Policy to `LOG_ONLY`, moving the
  Gateway attachment to `LOG_ONLY`, or restoring a prior Cedar snapshot.
- Generate Cedar from operator-provided natural language for the selected
  Gateway, present the generated asset for review, and require explicit create
  or update confirmation before it becomes active.
- When creating an engine/policy set, require an explicit authorization model:
  `Allowlist` (default and recommended), `Preserve traffic` (explicit baseline
  permit plus restrictions with a high-risk warning), or `Custom Cedar`.
- Never create a broad baseline permit merely as an implementation default.
- Read live LOG_ONLY/ENFORCE decision telemetry from AWS for the selected
  Gateway and Policy, including outcome, action, principal summary, policy ID,
  mode, timestamp, and trace/session link when present.
- Keep local demo-test decisions distinguishable from AWS decision telemetry;
  do not present the SQLite table as the complete production decision source.
- For external CUSTOM_JWT Gateways, validate through Cedar static validation
  and LOG_ONLY observations from real traffic; never accept, persist, or proxy
  operator-supplied bearer tokens.
- Preserve the existing `river`/`demo` live role tests only for the
  Launchpad-owned `launchpad-gw`, whose demo credentials are provisioned by the
  platform.
- Use exact Gateway action identifiers; do not synthesize them from display
  labels.
- Discover actions from control-plane target schemas where exact names are
  available. Optionally enrich from authenticated live `tools/list` only for
  AWS_IAM Gateways callable by the backend and the managed `launchpad-gw`.
- For external CUSTOM_JWT dynamic targets whose actions are unavailable from
  the control plane, allow manual entry of an exact action identifier, mark it
  unverified, and provide an external `tools/list` command template for
  operator-side discovery.
- Keep all AgentCore client construction in
  `backend/app/services/agentcore/client.py`.
- Keep frontend/backend contracts typed and maintain English/zh-CN i18n parity.

## Acceptance Criteria

- [ ] An operator can select an eligible existing MCP Gateway and see its live
      targets and policy attachment state.
- [ ] Merely listing or opening an unmanaged Gateway performs no AWS mutation;
      mutation controls become available only after explicit onboarding.
- [ ] Managed state survives browser and backend restarts through Gateway tags;
      unmanaging removes only those tags.
- [ ] An operator can import a Gateway and its discovered tools into Registry;
      the Gateway record enters the normal submit/approve lifecycle and becomes
      attachable only after APPROVED.
- [ ] Re-running discovery/import is idempotent and reports created, reused,
      skipped, and conflicted records.
- [ ] A Gateway Registry record renders all discovered targets/tools, and the
      Create Agent wizard does not imply target-level attachment isolation.
- [ ] Registry records expose an attachability capability and reason; the
      Create Agent wizard cannot select a Gateway whose outbound auth is
      unresolved.
- [ ] Legacy per-target records remain usable until the operator explicitly
      retires them after the Gateway-level record is APPROVED.
- [ ] A Cedar policy can be created for the selected Gateway and reaches ACTIVE
      without requiring `make bootstrap`.
- [ ] An existing LOG_ONLY policy can be updated in place with optimistic
      conflict protection; editing an ACTIVE policy uses an evidence-gated
      candidate and conservative cutover.
- [ ] Every mutation appends an immutable success/failure audit entry, and a
      rollback refuses to proceed when the live AWS `updatedAt` has changed
      since the journal entry it would reverse.
- [ ] A Policy Engine can be attached in LOG_ONLY without changing unrelated
      Gateway configuration, then promoted to ENFORCE through an explicit
      confirmation.
- [ ] ENFORCE promotion is evidence-gated by default; a low-traffic/emergency
      override captures typed confirmation and reason in the audit journal.
- [ ] Attachment preflight detects missing `GetPolicyEngine`,
      `AuthorizeAction`, and `PartiallyAuthorizeActions` permissions and
      provides a scoped remediation statement without mutating IAM.
- [ ] Onboarding a Gateway with an existing engine preserves the attachment;
      shared-engine mutations identify every affected Gateway before
      confirmation.
- [ ] Failed attachment or enforcement changes leave enough prior-state detail
      to retry or restore the previous configuration.
- [ ] The UI exposes no Policy/Engine delete or detach command; unmanaging a
      Gateway does not clean up its AWS resources.
- [ ] Natural-language generation never activates a generated policy without
      explicit operator review and confirmation.
- [ ] A newly authored policy starts in `LOG_ONLY`; the authorization-model
      choice and any broad baseline permit are explicit in the review screen.
- [ ] The selected Gateway exposes a live AWS-backed policy decision view that
      can provide rollout evidence for external Gateway traffic.
- [ ] External Gateway validation works without external bearer tokens passing
      through the Launchpad frontend, backend, database, or logs.
- [ ] OAuth and IAM Gateway examples use the correct Cedar principal type, and
      tool policies use the exact action names reported by the Gateway.
- [ ] Dynamic external CUSTOM_JWT targets remain manageable without sending a
      token through Launchpad; manually entered actions are visibly unverified
      and never auto-normalized.
- [ ] Existing `launchpad-gw` bootstrap, Governance role tests, Registry
      lifecycle, and Harness deployment continue to work.
- [ ] Backend tests use stubbed AWS clients; real-AWS validation remains in an
      opt-in `backend/scripts/e2e_*.py` flow.

## Out Of Scope

- Cross-account or cross-region Gateway management in one console session.
- Creating or editing Gateway targets themselves.
- Automatically approving Registry records.
- Automatically switching a production Gateway directly to ENFORCE.
- Policy management for non-AgentCore remote MCP servers.
- Policy deletion, Policy Engine detachment, and Policy Engine deletion.
- External Identity/OAuth provider onboarding and secret management.
