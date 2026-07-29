# Policy decision evidence: metrics track, then span track

Parent task. Owns the requirement set and cross-child acceptance; the
implementation lives in the two children.

## Source requirement

The Governance → Decisions view shows
`AWS 策略遥测不可用 · policy_span_shape_not_verified`. Investigation on
2026-07-29 established that this is not a failure but a hardcoded stub:
`unavailable_policy_decisions()` (`backend/app/services/governance.py:453`)
returns a constant `available: false` object, and
`GET /api/governance/gateways/{id}/decisions`
(`backend/app/routers/governance.py:273`) discards its `policy_id` and `force`
parameters. No telemetry is ever queried.

The user's ask: replace the stub with real AWS evidence, starting from capturing
real spans.

Research (`research/policy-evidence-channels.md`) changed the shape of the work
and the user confirmed the resulting split on 2026-07-29:

1. The span channel was empty because **no `TRACES` delivery has ever existed for
   the launchpad gateways** — not because span field shapes are unknowable.
2. A **second, default-on channel** (`AWS/Bedrock-AgentCore` CloudWatch policy
   metrics) already holds real ALLOW/DENY data for both launchpad gateways,
   including the documented `hr-database___create_payout` DENY.

## Confirmed decisions

- **Sequencing:** metrics track first (no AWS mutation, data already present),
  span track second.
- **Trace target:** `launchpad-gw-em0yuqmmdp` only. Not `launchpad-kb-gw`, not a
  new disposable gateway.
- **Ownership of the prerequisite:** the `TRACES` delivery is created idempotently
  by `make bootstrap`, not by a one-off script or a console click.

## Constraints

- The us-west-2 account is shared with the remote prod EC2 deployment
  (`launchpad-remote-prod-env`). Enabling a TRACES delivery is additive and
  reversible, but it is still a shared-resource mutation.
- Do not fabricate per-decision rows from aggregate data. Metrics cannot supply
  `principal`, `action`, `trace_id`, or `session_id`; the honest-reporting
  principle that motivated the original stub still holds.
- No Gateway-resource mutation. Enabling traces must not call
  `UpdateGateway` — Gateway updates in this preview API are omit-resets and are
  risky, and traces do not require one.
- The existing local demo ledger (`GET /api/governance/decisions`) keeps its
  current behavior and stays unmerged with AWS sources.

## Task map

| Child | Deliverable | AWS mutation |
|---|---|---|
| `07-29-policy-decision-metrics` | Aggregate decision evidence + real `evidence_count` from CloudWatch policy metrics; Decisions view renders it | none |
| `07-29-policy-span-detail` | `TRACES` delivery in bootstrap; captured span corpus; per-decision detail rows | one (additive, reversible) |

Ordering: the metrics child must land first. The span child depends on its
response-contract extension and its verified dimension-projection logic.

## Cross-child acceptance criteria

- [ ] The Decisions view no longer reports `policy_span_shape_not_verified` for
      `launchpad-gw` when evidence exists in the queried window.
- [ ] `evidence_count` reflects real AWS data, so the ENFORCE promotion gate stops
      forcing the zero-evidence override whenever genuine LOG_ONLY evidence exists.
- [ ] Zero evidence in a window is still reported honestly and distinguishably
      from "channel unavailable" — those are different states and must not collapse
      into one message.
- [ ] `docs/architecture.md`, `docs/lab/11-governance.md` (§11.6 and the FAQ row
      `决策一直是 0 条`) are updated to match the shipped behavior; no doc keeps
      describing the stub as the expected state.
- [ ] `make verify` passes.
- [ ] No `boto3.client(...)` outside the established factory locations.

## Notes

- `research/policy-evidence-channels.md` supersedes the archived
  `07-16-gateway-policy-management/research/policy-telemetry-shape.md`. When this
  parent completes, that document's "Status: Blocked" conclusion should be
  corrected via `trellis-update-spec` so a future session does not re-derive the
  wrong root cause.
