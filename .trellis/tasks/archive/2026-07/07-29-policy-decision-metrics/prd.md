# Policy decision evidence from CloudWatch metrics

Child of `07-29-policy-span-evidence`. Read the parent `prd.md` first for the
source requirement, confirmed decisions, and constraints.

## Goal

Make Governance → Decisions report **real** AWS policy-decision evidence for a
gateway, using the default-on `AWS/Bedrock-AgentCore` CloudWatch policy metrics,
and feed a real `evidence_count` into the ENFORCE / promotion gate. No AWS
mutation, no span parsing, no guessed fields.

## Why this is the first deliverable

`research/policy-evidence-channels.md` (parent task) verified that this channel is
already populated in the target account — 46 `AllowDecisions` and 36
`DenyDecisions` metric streams, with real daily datapoints for both launchpad
gateways including the `hr-database___create_payout` DENY that
`docs/lab/11-governance.md` documents. Nothing needs to be enabled.

## Requirements

### R1 — Query real evidence

- Replace `unavailable_policy_decisions()` with a service function that queries
  `AWS/Bedrock-AgentCore` metrics (`AllowDecisions`, `DenyDecisions`, and the
  mismatch/determining family) scoped to one gateway and one evidence window
  (`1h` / `6h` / `24h` / `7d`).
- **Dimension projections must not be summed together.** AWS publishes several
  overlapping projections of the same decision event (e.g. `{OperationName}`,
  `{OperationName, PolicyEngine}`, `{Policy, TargetResource, OperationName, Mode}`).
  Pick exactly one projection per question; summing across all returned streams
  double-counts. This is the single highest-risk correctness detail in the task.
- Scope to the requested gateway via the `TargetResource` dimension.

### R2 — Honor the parameters that are currently discarded

`GET /api/governance/gateways/{gateway_id}/decisions` currently does
`del policy_id, force` (`backend/app/routers/governance.py:280`).

- `policy_id` filters evidence to that policy (via the `Policy` dimension).
- `force` bypasses the response cache.
- `range` selects the window (already plumbed).

### R3 — Extend the response contract honestly

- Metrics **cannot** supply `principal`, `action`, `trace_id`, or `session_id`.
  `decisions[]` must therefore stay empty in this child; do not synthesize rows.
- Add an aggregate shape: counts by outcome, and breakdowns by operation
  (`AuthorizeAction` / `PartiallyAuthorizeActions`), enforcement `Mode`, `Policy`,
  and `ToolName` where the dimension is present.
- Add `evidence_count` (total matching decisions in the window) and a `source`
  discriminator so the span child can later add `decisions[]` without a breaking
  change.

### R4 — Three distinct states, not two

The current UI collapses everything into "telemetry unavailable". After this
change the view must distinguish:

1. **Channel unreadable** (e.g. CloudWatch access denied) → `available: false`
   with a concrete reason. `policy_span_shape_not_verified` is retired as a
   reason string; it described a condition that no longer applies.
2. **Readable, zero evidence in this window** → `available: true`,
   `evidence_count: 0`. This is expected and correct — the account's most recent
   decision datapoint is 2026-07-26, so a live 24 h query legitimately reads 0
   until someone calls the gateway.
3. **Readable, evidence present** → `available: true` with aggregates.

### R5 — Unblock the evidence gate

`backend/app/routers/governance.py` passes `evidence_count=0` at three call
sites — promote (`:183`), rollback (`:207`), gateway mode (`:226`). These feed
`_assert_evidence_or_override()` (`backend/app/services/governance.py:1252`),
which is why every ENFORCE switch and promotion currently demands the typed
zero-evidence override.

- All three must pass a real count derived from R1 for the gateway.
- The zero-evidence override path must remain intact and reachable for the
  genuinely-zero case; this requirement removes a false blocker, it does not
  weaken the gate.

### R6 — Frontend

- `frontend/src/lib/api.ts`: extend `GovernanceDecisionResponse` in step with the
  backend schema.
- `frontend/src/pages/governance/DecisionView.tsx`: render the aggregate view,
  show `evidence_count`, and distinguish the three R4 states.
- New i18n keys in **both** `en` and `zh-CN`.

### R7 — Documentation

- `docs/architecture.md` — Governance/Policy evidence section.
- `docs/lab/11-governance.md` — §11.6 (currently prints the stub JSON as the
  expected result) and the FAQ row `决策一直是 0 条`.

### R8 — Spec contract change

`.trellis/spec/launchpad/gateway-policy-management.md:106` currently states the
stub as a *contract*:

> Policy decisions return an explicit `available=false` until real Policy span
> fields have been captured. Never infer rollout evidence from demo rows.

This task supersedes the first sentence — evidence now comes from metrics, and
`available=false` is reserved for an unreadable channel. The second sentence still
holds and must be preserved: the local demo ledger is still never treated as
rollout evidence. Update the clause in Phase 3 (step 3.3, `trellis-update-spec`),
including the fact that the evidence gate counts `LOG_ONLY`-mode decisions only.

## Out of scope

- Per-decision rows, principal, decision reason, trace links — those need spans
  (child `07-29-policy-span-detail`).
- Enabling the `TRACES` delivery, and any change to `make bootstrap`.
- Any change to the local demo ledger `GET /api/governance/decisions`.
- `launchpad-kb-gw` specific work; the code must be gateway-generic but only
  `launchpad-gw` is the verification target.

## Acceptance criteria

- [x] Non-zero `evidence_count` against real AWS — **verified on
      `launchpad-kb-gw-pmyq7mchum`, not `launchpad-gw`.** launchpad-gw's newest
      datapoint (07-16) had aged out of even the 7d window by 07-29, and fresh
      evidence could not be generated (see Deviations). The read path is
      gateway-generic; kb-gw over 7d returned `evidence_count: 17`, matching an
      independent CLI baseline exactly (5 per-call `AuthorizeAction` +
      12 per-tool `PartiallyAuthorizeActions`).
- [x] `range=24h` on a quiet window returns `available: true` with
      `evidence_count: 0`, not `available: false` — confirmed against real AWS on
      launchpad-gw and rendered as the new neutral empty state.
- [x] `policy_id` and `force` change the result; neither is discarded.
- [x] A unit test feeds the real overlapping/sparse projections and asserts no
      double counting. Its value was checked by loosening `_select` to a subset
      match: 3 tests fail, as intended.
- [x] Promote / rollback / mode routes pass a real `evidence_count` (parametrized
      test over all three); the gate admits without an override when LOG_ONLY
      evidence exists and still raises `governance.evidence_required` (409)
      without it. ENFORCE-only evidence does not satisfy the gate.
- [x] `decisions[]` is empty and no field is fabricated.
- [x] No `boto3.client(...)` added outside the established factory locations
      (`observability.cw_client` was promoted from private, not duplicated).
- [x] `policy_span_shape_not_verified` is gone from backend code; remaining
      references are Trellis history plus the SUPERSEDED archived research note.
- [x] en ↔ zh-CN key parity holds.
- [x] `make verify` passes (889 backend tests; all 10 stages green).

Extra verification not required by the plan: the new UI surface was rendered in a
real browser in both `en` and `zh-CN` — aggregate panel, `basis` labels, the
breakdown caveat, and the quiet-window state, with 0 console errors.

## Deviations from the plan

1. **`design.md`'s chosen projection was wrong and was corrected during
   implementation.** It specified a single fixed `{TargetResource, OperationName,
   Mode}` projection for gateway totals. That projection does not exist for
   `DenyDecisions` on launchpad-gw, so `evidence_count` would have counted every
   ALLOW and silently missed every DENY. Root cause: `AuthorizeAction` counts one
   decision per call and publishes a gateway-level stream, while
   `PartiallyAuthorizeActions` counts one per (call, tool) and was observed
   publishing only `ToolName` projections. The shipped code resolves a projection
   **per operation** from a preference chain and reports the `basis` it counted
   in, derived from the projection rather than asserted.
2. **`evidence_count()` lives in `governance_evidence.py`,** not as a
   `gateway_evidence_count()` wrapper in `governance.py` as `design.md` sketched.
   Avoids a pure pass-through and guarantees the gate's number and the view's
   number come from one code path.
3. **The gate uses the request's `evidence_range`** instead of a hardcoded 24h —
   `PolicyTransitionRequest` already carries that field.
4. **Fresh evidence could not be generated, for an unrelated pre-existing
   reason.** `implement.md` step 9 called for the documented `policy-test` repro;
   it returns `gateway.credentials_rejected` because the configured demo Cognito
   user's credentials are rejected, so the gateway is never called. Out of scope
   here. Verification moved to kb-gw, which had in-window data.

## Follow-ups found but not fixed (out of scope)

- **`policy-test` records auth failures as policy DENYs.** Any `AppError` from
  the gateway call is written to the decision ledger as a DENY, so a Cognito
  outage produces fake Cedar denials. Two such rows created during verification
  (including a `get_employee` "DENY", a tool the lab documents as allowed) were
  deleted from `data/launchpad.db`; the historical demo rows were untouched.
- **The demo Cognito credentials are rejected**, which blocks the documented
  DENY repro in `docs/lab/11-governance.md` §11.6 and will block the span-capture
  child unless another way to drive gateway traffic is used.
