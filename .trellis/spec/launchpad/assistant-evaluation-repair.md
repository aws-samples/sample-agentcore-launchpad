# Assistant evaluation-plan repair

## Contract

- `POST /api/assistant/architect/conversations/{id}/turns` accepts the existing
  `prompt` plus optional `evaluation_plan_repair: {plan_revision, plan_hash}`.
- The nested reference forbids extras, requires a positive strict integer and
  a lowercase 64-character hexadecimal hash. It never accepts client-supplied
  errors or plan content.
- Resolve conversation ownership and workspace before `repair_prompt`. Require the
  latest saved plan's exact revision/hash, invalid status, nonempty errors and
  matching source proposal identity/hash. Stale and no-longer-invalid references
  return 409 before opening SSE.
- The saved plan, findings and latest proposal are included explicitly, independent
  of bounded transcript replay. Include an older source when it differs from the
  latest proposal; retain the latest proposal as the configuration baseline.
- Apply existing prompt/replay budgets to the complete context. Refuse oversized
  context with 413 `assistant.evaluation_repair_too_large`; never truncate evidence.

## Lifecycle and UI

- Reuse ordinary turn claims, transcript persistence, SSE cancellation and inert
  proposal validation. Repair must not approve/deploy/materialize/sync/evaluate.
- The panel prepares from the exact newly returned valid proposal only after the
  stream completes successfully. A missing/invalid proposal, error event, truncated
  stream or stale view cannot trigger preparation.
- Keep unsent composer input. Block repair during existing turns or JSON/proposal
  edits. Use synchronous click guards as well as disabled controls.
- New plan validation is authoritative: remaining errors stay visible and can be
  repaired explicitly again or edited manually. No automatic retry loop.
- Every async continuation checks conversation/workspace generation, including
  returning to a previously selected conversation.

## Validation

`backend/tests/test_assistant_evaluation_repair.py` covers trusted context resolution,
invalid references, budgets and the ordinary turn path using the hermetic Harness
fixture. Browser verification covers the repair button through streamed proposal
and exact-revision prepare, preserved drafts, failures and view changes.
