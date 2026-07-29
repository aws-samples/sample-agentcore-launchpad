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

Marked `[x]` where the metrics child (`07-29-policy-decision-metrics`, commit
`9bb3495`) satisfied them; the rest belong to the span child.

- [x] `policy_span_shape_not_verified` is gone from the product; the Decisions
      view reports real evidence when the window has any.
- [x] `evidence_count` reflects real AWS data, so the cutover gate no longer
      forces the zero-evidence override when genuine LOG_ONLY evidence exists
      (verified: 17 LOG_ONLY decisions on kb-gw over 7d).
- [x] Zero evidence in a window is reported distinguishably from "channel
      unavailable" — three states, verified in the browser.
- [x] `docs/architecture.md` and `docs/lab/11-governance.md` (§11.6, the FAQ rows,
      and the chapter checklist) match the shipped behavior; no doc describes the
      stub as expected. The spec clause and the archived 07-16 research note were
      corrected too.
- [x] `make verify` passes.
- [x] No `boto3.client(...)` outside the established factory locations.
- [x] `make bootstrap` idempotently owns the `launchpad-gw` `TRACES` delivery —
      delivered by `07-29-gateway-traces-delivery` (`5103a93`). Applied to the
      account; the first Policy spans in this account followed within minutes.
- [~] Per-decision rows with **trace link — yes**; with **principal and reason — no,
      and not achievable.** Delivered by `07-29-policy-span-detail` (`f96ceb2`) with
      an amendment the capture forced:
      - `principal`: no span in a captured 31-span trace carries one, because the
        Harness authenticates to the Gateway with an OAuth M2M client credential.
        Structural, not an implementation gap. Rendered as explained-absent.
      - `reason`: `aws.agentcore.policy.authorization_reason` is documented by AWS
        but was absent from the capture, and no `AuthorizeAction` DENY can be
        produced under `ENFORCE` (the denied tool is filtered out of `tools/list`),
        so its shape is unverifiable without a `LOG_ONLY` mode switch — which the
        user declined for a shared demo resource on 2026-07-29.

      This criterion was written before the channel had ever been observed. Writing
      those two fields anyway would have been exactly the failure the research gate
      exists to prevent, so they are recorded as unavailable in the spec instead.

## Outcome

The original report — Governance → Decisions showing
`policy_span_shape_not_verified` — is resolved, and the root cause the 07-16
research had misdiagnosed is corrected in the archived note and the spec. Both
telemetry channels now back the view: CloudWatch metrics for exact counts and the
cutover gate, Policy spans for per-decision detail.

Credential blocker: **bypassed, not fixed.** Gateway traffic is driven through the
`hr-database` Harness agent's OAuth M2M path. The demo Cognito user credentials are
still rejected, and `policy-test` still records such auth failures as Cedar DENYs in
the local ledger — both found here, both left as follow-ups outside this scope.

## Notes

- `research/policy-evidence-channels.md` supersedes the archived
  `07-16-gateway-policy-management/research/policy-telemetry-shape.md`. When this
  parent completes, that document's "Status: Blocked" conclusion should be
  corrected via `trellis-update-spec` so a future session does not re-derive the
  wrong root cause.
