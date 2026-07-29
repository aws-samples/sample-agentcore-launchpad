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

### R1 — Bootstrap owns the delivery — **MOVED OUT**

Split into its own child, `07-29-gateway-traces-delivery`, on 2026-07-29: it is
independently verifiable and, unlike R2/R3 below, not blocked by the credential
problem. **That child is a prerequisite for R2** — without the `TRACES` delivery
there is nothing to capture. Do not re-implement it here.

### R2 — Capture a real span corpus (research gate) — **DONE 2026-07-29**

Recorded verbatim in `research/policy-span-corpus.md` (commit `60938d1`). Traffic
was driven through the `hr-database` Harness agent, whose OAuth `CLIENT_CREDENTIALS`
path to the Gateway works and is independent of the rejected demo Cognito
credentials. Captured under `ENFORCE`:

- `AgentCore.Policy.AuthorizeAction` — ALLOW
- `AgentCore.Policy.PartiallyAuthorizeActions` — carrying a real DENY
  (`denied_tools: ["hr-database___create_payout"]`)
- `AgentCore.Gateway.InvokeTool` (SERVER) — the most useful span: `tool.name` **and**
  `aws.agentcore.policy.authorization_decision` together
- the full 31-span trace shape

**Deliberately not captured, by the user's 2026-07-29 decision:** the `LOG_ONLY`
corpus. An `AuthorizeAction` DENY is structurally impossible under `ENFORCE` —
`PartiallyAuthorizeActions` filters the denied tool out of `tools/list`, so the
model can never attempt it. Getting it would need a mode switch on a shared demo
gateway. Consequences: the `AuthorizeAction`-DENY shape and
`aws.agentcore.policy.authorization_reason` (documented, but absent on the captured
ALLOW span) remain **unverified**, and R3 must not implement a branch that depends
on either.

Repro path is already documented: invoke the `hr-database` harness agent as
`hr-analyst` against `create_payout` for DENY (`docs/lab/11-governance.md`), and
an allowed tool for ALLOW.

> **Known blocker, found 2026-07-29 in the sibling child.** That repro currently
> fails: `POST /api/governance/policy-test` returns
> `gateway.credentials_rejected` because the configured demo Cognito user's
> credentials are rejected, so the gateway is never called and no decision is
> produced. Capturing spans therefore requires first restoring those credentials
> or finding another way to drive real gateway traffic (e.g. a Harness agent
> invocation with working auth). **Resolve this before R2**, and note that a
> related defect makes the failure look like a Cedar DENY: `policy-test` writes
> any `AppError` to the decision ledger as a DENY row.

Record verbatim spans — full attribute maps, not summaries — plus the exact Logs
Insights query used. This is the artifact the 07-16 research was missing.

### R3 — The parser, bounded by what was captured

No attribute name may enter the parser without appearing in
`research/policy-span-corpus.md`. Field availability is now established, not
assumed:

| Field | Source | Status |
|---|---|---|
| `at`, `outcome`, `action` | `AgentCore.Gateway.InvokeTool`: `startTimeUnixNano`, `aws.agentcore.policy.authorization_decision`, `tool.name` | available |
| `policy_id`, `mismatched_policies`, `log_only_matched_policies`, engine arn | sibling `AgentCore.Policy.*` span, joined on `parentSpanId` | available |
| `engine_mode` | `aws.agentcore.gateway.policy.mode` | available (Gateway attachment mode) |
| `gateway_id`, `trace_id`, `span_id` | span fields / `target_resource.id` | available |
| `session_id` | `session.id` on the root `POST /invocations` / `mcp tools/call` spans, joined on `traceId` | available via join |
| `policy_mode` | not in any span — only the Gateway attachment mode is | **null** |
| **`principal`** | no span in the trace carries any principal/actor/subject attribute; the Harness authenticates with an M2M client credential, so there is no human principal in the request | **null, structurally** |

- `principal` and `policy_mode` render as **absent**, never back-filled or inferred.
  The local demo ledger keeps its own `principal`; the two must not be conflated.
- `PartiallyAuthorizeActions` denials are list-time *tool availability* decisions,
  not blocked invocations. They must be surfaced (they are the only DENY evidence
  under ENFORCE) but labelled as a different kind of evaluation — not presented as
  a blocked call.
- `aws.agentcore.policy.log_only_matched_policies` is undocumented but captured, and
  is the one thing the metric channel cannot express: it shows what a LOG_ONLY
  candidate *would* have matched, from an ENFORCE-mode span. Surface it.
- `source` becomes `metrics+spans`; metric aggregates stay authoritative for
  `evidence_count`, because spans are sampled while metrics are exact counts.

### R4 — Reconcile the two channels

Where metrics and spans disagree on counts in the same window, the UI must not
silently prefer one. State which number is which (exact count vs sampled detail).

### R5 — Documentation

- `docs/architecture.md` — the span channel and its prerequisite.
- `docs/lab/11-governance.md` §11.6 — real per-decision rows.
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

- [x] (prerequisite) the `TRACES` delivery exists — delivered by
      `07-29-gateway-traces-delivery` (`5103a93`), and the very first Policy spans in
      this account appeared minutes later.
- [x] `research/policy-span-corpus.md` holds verbatim spans with the queries used.
      ALLOW (`AuthorizeAction`) and DENY (`PartiallyAuthorizeActions`) are both
      covered under `ENFORCE`. The two uncaptured cases —
      `AuthorizeAction`-DENY and `authorization_reason` — are recorded as
      **unverified with the structural reason**, not assumed.
- [x] Every attribute name in the parser appears in a captured span; a test parses
      the module AST and asserts the code (docstring excluded) never mentions
      `authorization_reason`. Verified it fails when the attribute is added back.
- [x] The Decisions view shows real per-decision rows for `launchpad-gw`:
      1 `invocation` ALLOW (`hr-database___list_departments`, with
      `log_only_matched_policies: ["lab_readonly_tools-be45dja2_p"]`) and 2
      `tool_listing` DENY (`hr-database___create_payout`), each with trace + session
      links. **Not with principal** — see the amended R3 table; that field is
      structurally unavailable.
- [x] Unestablished fields render as absent, not fabricated: `principal` shows an
      explained "not in span" marker, `policy_mode` a dash.
- [x] One parsed row cross-checked field-by-field against its raw span
      (`spanId 08b21d1592a9b9aa`): span_id, trace_id, action, outcome, mode all match.
- [x] Rendered in a browser in `en` and `zh-CN`, 0 console errors.
- [x] `make verify` passes (912 backend tests).

## Notes

`design.md` and `implement.md` are intentionally deferred: the parser design
depends on the captured span shape, and writing it now would repeat the exact
mistake the 07-16 research was created to prevent. Author them after R2 completes,
before implementing R3.

Two things must land before this child can start: the `TRACES` delivery
(`07-29-gateway-traces-delivery`) and a working way to drive real gateway traffic
(the credential blocker in R2).


## Deviations and discoveries during implementation

1. **`principal` turned out to be unobtainable**, which the original PRD had listed
   as a deliverable ("real per-decision rows … with principal and reason"). The
   capture settled it: no span carries a principal because the Harness uses an M2M
   client credential. R3 and the acceptance criteria were amended rather than the
   field faked.
2. **Promoted `observability._logs_client` to `logs_client`**, mirroring the
   `cw_client` promotion from the metrics child, so the router injects it explicitly
   instead of a third module building its own client.
3. **Two stale doc statements found and fixed**, both introduced by earlier work in
   this task tree:
   - The aggregate note still said "per-decision rows (principal, reason, trace)
     require Policy spans" — which now sat directly above a table of rows, and was
     wrong about principal even with spans.
   - `docs/lab/11-governance.md` claimed `launchpad-gw` had no evidence in the 7d
     window (true when written, false after the R2 capture generated traffic), and
     its DENY reproduction hint was wrong twice over: the `policy-test` path is
     blocked by rejected credentials, and under `ENFORCE` a Harness agent
     *cannot* call `create_payout` at all because it is filtered out of `tools/list`.
     Replaced with the verified Harness-driven procedure and an explanation of why
     only a `tool_listing` DENY can appear.

## Follow-up left open

- `AuthorizeAction`-DENY shape and `aws.agentcore.policy.authorization_reason` stay
  unverified. Capturing them needs the Gateway in `LOG_ONLY`, which the user declined
  on 2026-07-29 for a shared demo resource. Recorded in the spec so nobody adds the
  field speculatively.
- `policy-test` still records auth failures as Cedar DENYs in the local ledger
  (found by the metrics child, still unfixed, still out of scope).