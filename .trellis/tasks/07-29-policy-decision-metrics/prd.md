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

- [ ] `GET /api/governance/gateways/launchpad-gw-em0yuqmmdp/decisions?range=7d`
      against real AWS returns `available: true` with a non-zero `evidence_count`
      when the window covers known decision datapoints.
- [ ] Requesting `range=24h` on a quiet account returns `available: true` with
      `evidence_count: 0` — **not** `available: false`.
- [ ] `policy_id` and `force` change the result; neither is discarded.
- [ ] A unit test feeds overlapping/sparse dimension projections and asserts the
      count is **not** double-counted.
- [ ] Promote / rollback / mode routes pass a real `evidence_count`; a unit test
      shows the gate admits a change without an override when evidence exists,
      and still refuses without one when evidence is zero.
- [ ] `decisions[]` is empty and no field is populated with fabricated data.
- [ ] No `boto3.client(...)` added outside the established factory locations.
- [ ] `policy_span_shape_not_verified` no longer appears in backend code, and
      remaining references in docs describe history, not current behavior.
- [ ] en ↔ zh-CN key parity holds (`python3 scripts/i18n_check.py`).
- [ ] `make verify` passes.
