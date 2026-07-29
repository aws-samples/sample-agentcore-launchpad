# Policy decision span detail via gateway TRACES delivery

Child of `07-29-policy-span-evidence`. Read the parent `prd.md` and
`research/policy-evidence-channels.md` first. **Blocked on
`07-29-policy-decision-metrics` landing** — this child extends its response
contract.

## Goal

Turn on the span channel for `launchpad-gw`, capture real Policy decision spans,
and only then implement a parser that adds **per-decision detail rows** —
principal, decision reason, determining/mismatched policy ids, and a trace link —
to the Decisions view.

## Why the original research was blocked, and why it no longer is

The archived 07-16 research concluded "blocked on live span evidence" and inferred
the missing prerequisite was Gateway trace delivery. That inference is now
confirmed as the whole story: the launchpad gateways have **no `TRACES` delivery**,
while other resources in the same account do. Everything else was already in
place — Transaction Search is `ACTIVE`, `aws/spans` exists and receives gateway
MCP spans.

So the prerequisite is three additive `logs` API calls, not a mystery. What
remains genuinely unverified is the **span field shape**: AWS documents the
`aws.agentcore.policy.*` attributes, but not the principal or session aliases the
product contract needs. That is what the capture step establishes.

## Confirmed decisions (from the parent)

- Target: `launchpad-gw-em0yuqmmdp` only.
- The `TRACES` delivery is created idempotently by `make bootstrap`.
- No `UpdateGateway` call. Enabling traces does not require one, and Gateway
  updates in this preview API are omit-resets.

## Requirements

### R1 — Bootstrap owns the delivery (idempotent)

Extend the bootstrap path (`backend/app/services/policy_bootstrap.py` is the
natural home — it already owns `ensure_transaction_search()`; `gateway_bootstrap.py`
owns gateway resources, so justify the choice in `design.md`) with an
`ensure_gateway_traces()` step:

```python
logs.put_delivery_source(name=f"{gateway_id}-traces-source", logType="TRACES", resourceArn=gateway_arn)
logs.put_delivery_destination(name=f"{gateway_id}-traces-destination", deliveryDestinationType="XRAY")
logs.create_delivery(deliverySourceName=..., deliveryDestinationArn=...)
```

- Idempotent: re-running `make bootstrap` must not error or duplicate. Existing
  deliveries are detected via `describe-delivery-sources` /
  `describe-deliveries`.
- Must not run before Transaction Search is confirmed — AWS documents it as a
  hard prerequisite for enabling tracing.
- Report the outcome in the bootstrap summary like the other steps.
- Naming must follow the account's existing convention
  (`<resource-id>-traces-source` / `-traces-destination`), which the pre-existing
  memory and runtime deliveries already use.

### R2 — Capture a real span corpus (research gate)

Before any parser is written, capture and record complete raw spans in this
child's `research/`:

1. ALLOW and DENY under Gateway `ENFORCE` (the current mode).
2. ALLOW and DENY under `LOG_ONLY` — requires a mode switch on a shared demo
   resource; treat it as a separate confirmed action and restore the original mode
   afterwards. If restoring is risky, capture ENFORCE only and record LOG_ONLY as
   unverified rather than guessing.
3. Both `AuthorizeAction` and `PartiallyAuthorizeActions`.

Repro path is already documented: invoke the `hr-database` harness agent as
`hr-analyst` against `create_payout` for DENY (`docs/lab/11-governance.md`), and
an allowed tool for ALLOW.

Record verbatim spans — full attribute maps, not summaries — plus the exact Logs
Insights query used. This is the artifact the 07-16 research was missing.

### R3 — Only then, the parser

- Bounded alias map derived from the captured corpus; no attribute name enters
  the parser without appearing in a captured span.
- Populate `decisions[]` with the existing `GovernancePolicyDecision` fields
  (`principal`, `action`, `outcome`, `engine_mode`, `policy_mode`, `trace_id`,
  `session_id`, `policy_id`).
- Any field the corpus does not establish stays `null` and is rendered as absent —
  it is not back-filled from metrics or inferred.
- `source` becomes `metrics+spans`; the metric aggregates stay authoritative for
  `evidence_count` unless the corpus proves spans are complete, because sampling
  can drop spans while metrics are exact counts.

### R4 — Reconcile the two channels

Where metrics and spans disagree on counts in the same window, the UI must not
silently prefer one. State which number is which (exact count vs sampled detail).

### R5 — Documentation

- `docs/architecture.md` — the span channel and its prerequisite.
- `docs/lab/11-governance.md` §11.6 — real per-decision rows.
- `docs/setup.md` / bootstrap docs — the new bootstrap step and the IAM actions it
  needs (`logs:PutDeliverySource`, `logs:PutDeliveryDestination`,
  `logs:CreateDelivery`, `logs:DescribeDelivery*`).
- Correct the archived
  `07-16-gateway-policy-management/research/policy-telemetry-shape.md` status via
  `trellis-update-spec` so a future session does not re-derive the wrong root cause.

## Out of scope

- `launchpad-kb-gw` traces.
- Creating a disposable gateway.
- Vended application logs (`APPLICATION_LOGS`) for the gateway — separate concern
  from `TRACES`, even though the same API family enables it.
- Re-doing the metric aggregation from the sibling child.

## Acceptance criteria

- [ ] `make bootstrap` creates the `launchpad-gw` `TRACES` delivery on a fresh
      account and is a no-op on re-run.
- [ ] `describe-delivery-sources` shows a `TRACES` source for
      `gateway/launchpad-gw-em0yuqmmdp`.
- [ ] `research/` holds verbatim captured spans covering ALLOW and DENY for both
      operations, with the query used; any uncaptured case is recorded as
      unverified rather than assumed.
- [ ] Every attribute name in the parser appears in a captured span.
- [ ] The Decisions view shows real per-decision rows for `launchpad-gw` with
      principal and reason.
- [ ] Unestablished fields render as absent, not fabricated.
- [ ] `make verify` passes.

## Notes

`design.md` and `implement.md` are intentionally deferred: the parser design
depends on the captured span shape, and writing it now would repeat the exact
mistake the 07-16 research was created to prevent. Author them after R2 completes,
before implementing R3. R1 is independent and may be planned and executed first.
