# Fix policy-create gating feedback and dropped override reason

Parent: `07-29-workshop-backlog`. ISSUE-010 (P1).

## Report vs. reproduction

Report: "With a valid Cedar draft and the gateway-name field filled, the enabled-looking
`CREATE LOG_ONLY POLICY` button produces no dialog, no HTTP request, and no console
error. The blocking field is `ZERO-EVIDENCE OVERRIDE REASON`."

Reproduced live against the dev account
(`/governance?view=policy&gateway=launchpad-gw-em0yuqmmdp`, managed gateway with an
ACTIVE policy engine in ENFORCE mode):

- Landing state: `CREATE LOG_ONLY POLICY` is **`disabled`** while the Cedar draft is
  empty, and nothing tells the operator why. `.btn:disabled{opacity:.45}` on a primary
  (amber) button still reads as enabled, and a disabled button swallows clicks — which
  is exactly the reported symptom.
- Filling only the Cedar textarea (both `TYPE THE GATEWAY NAME` and
  `ZERO-EVIDENCE OVERRIDE REASON` left empty) **enables** the button. So the override
  reason is NOT the blocker: `saveReady` (`PolicyEditorView.tsx:192`) checks
  gatewayReady / nameValid / non-empty statement / not-busy only. The reported
  attribution is a red herring; the real blocker was the empty draft.

Two further defects confirmed by reading the backend:

- **The override reason is dropped on the create path.** `queue_policy_create`
  (`app/services/governance.py:993`) calls `_new_change(...)` **without**
  `override_reason=request.override_reason`, unlike `queue_policy_transition` (:1181)
  and `queue_gateway_mode` (:1242). The value survives only inside the `requested`
  JSON, so the `PolicyChange.override_reason` column stays NULL and `AuditView`
  (`AuditView.tsx:249`) renders `OVERRIDE REASON -`.
- **The zero-evidence field does not gate creation at all.**
  `_assert_evidence_or_override` runs for policy promote/rollback and for
  `gateway_mode → ENFORCE` only (:1168, :1230, :1420, :1572). `policy_create` never
  calls it, so the field the UI presents as a hard requirement is optional there. The
  report's "the gate fires even for a LOG_ONLY policy on an ENFORCE gateway" is
  therefore a UI presentation problem, not an over-strict backend gate.

## Requirements

1. The primary action in the policy editor states its unmet requirements instead of
   silently swallowing the click: while `saveReady` is false, render the specific
   reason(s) (no Cedar draft / invalid policy name / gateway not ready / shared-gateway
   ack missing / high-risk model not acknowledged / operation in flight) next to the
   button. Keeping the button `disabled` is correct — the missing piece is the stated
   reason.
2. `queue_policy_create` persists `override_reason` on the `PolicyChange` row so the
   justification the form collected appears in the audit entry.
3. The `ZERO-EVIDENCE OVERRIDE REASON` and `TYPE THE GATEWAY NAME` fields are presented
   truthfully for the action they apply to: for the create action they are an optional
   justification recorded in the audit trail; for promote / rollback / ENFORCE cutover
   they remain required. The label must say which, rather than implying a create-time
   gate that does not exist.
4. No change to the evidence gate itself — promote / rollback / ENFORCE keep requiring
   evidence or a confirmed override.
5. en + zh-CN parity for every new key.

## Acceptance Criteria

- [ ] With an empty Cedar draft, the editor shows why the create button is disabled
      (naming the draft), and the same mechanism covers each other unmet condition.
- [ ] With a valid draft, the button is enabled and opens the confirm dialog; no
      requirement is silently swallowed.
- [ ] `POST /api/governance/gateways/{id}/policies` with `override_reason` writes it to
      the `PolicyChange` row → `GET` audit shows the reason instead of `-` (backend
      test).
- [ ] Existing promote/rollback/ENFORCE gate behaviour and their 409
      `governance.evidence_required` responses are unchanged (existing tests still
      pass; a test pins that create does not require the override).
- [ ] The override/confirmation fields' copy distinguishes optional-for-create from
      required-for-cutover.
- [ ] Browser evidence of the disabled-with-reason state and the enabled state.
- [ ] `make verify` passes.
