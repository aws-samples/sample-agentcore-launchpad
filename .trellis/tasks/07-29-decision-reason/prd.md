# Surface the policy denial reason in decision rows

## Goal

Add `aws.agentcore.policy.authorization_reason` to span-derived decision rows. It was
omitted because it was unverified; that premise is gone — it is now captured and
recorded (`8b06adb`).

## Why it was omitted, and what changed

`governance_spans.py` deliberately never mentioned the attribute, guarded by a test
that parses the module AST and fails if it appears. That was correct while the field
was documented-but-unobserved.

Verified 2026-07-29 on a real `AuthorizeAction` DENY span:

```
aws.agentcore.policy.authorization_reason
  = "Policy evaluation denied due to launchpad_payout_admin_only-x7gz5yjkrd"
```

Two conditionality facts constrain the implementation:

- **`authorization_reason` appears on DENY only** — absent on ALLOW.
- **`log_only_matched_policies` is the mirror image**: present on ALLOW, absent on that
  DENY.

So neither may be required. Both are read defensively and render as absent when
missing. A row with `outcome: ALLOW` and `reason: null` is correct, not a parse bug.

## Requirements

### R1 — Parse it

- `governance_spans._row()` reads `aws.agentcore.policy.authorization_reason` from the
  Policy span with `.get()`, yielding `reason: str | None`.
- `tool_listing` rows (from `PartiallyAuthorizeActions`) get `reason: None` — that span
  carries `allowed_tools`/`denied_tools`, not a reason. Do not synthesize one from the
  tool name.

### R2 — Replace the guard test, don't just delete it

The AST guard asserting the module never mentions `authorization_reason` must be
**replaced by a positive test** built from the captured DENY span: the reason is
parsed, and it is `None` on an ALLOW row. Deleting the guard without a replacement
would remove coverage rather than move it.

The wider discipline the guard enforced still applies: no *other* undocumented or
unobserved attribute may be added. Keep a narrowed form of that check if it can be
expressed cleanly; otherwise state the rule in the module docstring.

### R3 — Contract + view

- `frontend/src/lib/api.ts`: `GovernancePolicyDecision.reason: string | null`.
- `DecisionView.tsx`: surface the reason on the row. It is long
  (~70 chars) and only present on denials, so it belongs under the outcome or action
  cell as a note rather than as a new column that would be empty for most rows.
- i18n keys in `en` + `zh-CN` together.

### R4 — Docs + spec

- `.trellis/spec/launchpad/gateway-policy-management.md`: drop the "not yet surfaced"
  clause and the note about retiring the test.
- `docs/lab/11-governance.md`: the decision-row example gains the reason.

## Out of scope

- Any AWS mutation. This is a parser/render change only.
- Enabling TRACES on the us-east-1 prod gateway (see the deployment note below).
- The `ERROR`-outcome ledger work — already shipped in `d29d36c`.

## Acceptance criteria

- [x] A captured DENY span yields the exact reason string; ALLOW rows yield `None`.
      Verified against live AWS: 2 DENY rows carry
      `Policy evaluation denied due to launchpad_payout_admin_only-x7gz5yjkrd`.
- [x] `tool_listing` rows have `reason: None` — nothing synthesized.
- [x] The AST guard was **replaced, not removed**: a positive test pins the reason in
      both directions, plus a new AST test asserting every `aws.agentcore.*` key the
      module reads is in the captured allow-list. Both verified to fail when broken.
- [x] `make verify` passes (925 backend tests).
- [x] Rendered locally against real AWS data — the reason shows under the DENY chip,
      ALLOW and `tool_listing` rows show none, no application console errors. No new
      i18n keys were needed (the reason is AWS-supplied data, not UI copy), so the
      zh-CN surface is unchanged from the previously verified state.
- [x] Pushed to `origin/main`.

## Deployment and verification note (agreed scope, with one limit)

After push: update the remote prod EC2 and verify the live site with playwright-cli.

**What can be verified there and what cannot.** The remote deployment is entirely
us-east-1 with its own gateway, and **no us-east-1 gateway has a `TRACES` delivery**
(checked: zero). So prod has no Policy spans, `decisions[]` will be empty there, and
the `reason` *values* cannot be seen on prod. Verifiable on prod: the deploy is
healthy, the page renders, the metric aggregates and the three-state logic behave, and
the console is clean.

Enabling TRACES on the prod gateway would fix that — one additive, reversible change,
and `make bootstrap` there would now do it automatically since `5103a93`. Not done
without a decision, because it mutates a production resource.


## Extra fix found during verification (in scope)

Real data contained a row with a **blank outcome**: an `AgentCore.Gateway.InvokeTool`
span that omits `aws.agentcore.policy.authorization_decision` while its child Policy
span carries it. The table rendered an empty outcome chip.

Fixed by falling back to the child Policy span, which recovers the row as `ALLOW`
rather than dropping it, and by dropping rows where **neither** span has a decision —
a row with no decision is not a decision, the same principle applied to the ledger in
`d29d36c`. Pinned by a test.
