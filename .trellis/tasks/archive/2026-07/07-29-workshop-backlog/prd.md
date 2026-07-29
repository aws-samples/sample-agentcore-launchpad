# Workshop test backlog: platform-side fixes

Source: the workshop lab test report's **Platform-Side Backlog** section. ISSUE-007
(failed recommendation presented as acceptable output) is already fixed and archived as
`07-28-failed-rec-visibility`; this parent owns the rest of the platform-side items.

## Source requirements (verbatim intent, per issue)

| Issue | P | Report ask | Child task |
|---|---|---|---|
| 010 | P1 | `CREATE LOG_ONLY POLICY` silently does nothing; button should state the unmet requirement instead of swallowing the click, and the override reason it insists on must be retained in the audit entry; clarify the label if the gate is intentional for LOG_ONLY on an ENFORCE gateway | `07-29-policy-create-gating` |
| 011 | P2 | Canary cleanup leaks the S3 candidate packages (~75 MB per run); record the uploaded keys and delete them at cleanup, or use a lifecycle-managed prefix | `07-29-canary-s3-cleanup` |
| 001 | P2 | Readiness panel reads as a fault on a fresh account; distinguish "not provisioned yet, expected" from "unhealthy", e.g. a neutral state naming the chapter that creates the resource | `07-29-readiness-panel` |
| 005 | P3 | Vite dev server does not proxy `/v1`, so the chat page's "equivalent API call" snippet 404s | `07-29-quick-wins` |
| 014 | P3 | Canary verdict metrics render unrounded floats (`C 0.5566666666666668`) | `07-29-quick-wins` |
| — | P3 | `model_prices` has no entry for `global.amazon.nova-2-lite-v1:0`, so observability shows `≈ —` for cost | `07-29-quick-wins` |

## Cross-child acceptance criteria

- [ ] Every child's own acceptance criteria pass, each with its own commit.
- [ ] `make verify` passes on the final state of the whole set.
- [ ] Each fixed issue's behaviour is documented where the workshop content
      currently documents the defect as a known behaviour, so the content
      workaround can be reverted (spec under `.trellis/spec/launchpad/`, and
      `docs/lab/*` where the chapter text describes the workaround).
- [ ] No change widens scope into the items the report classifies as
      environment constraints (ISSUE-003/004/006/013) or the unattributed
      ISSUE-008.

## Explicitly out of scope

- ISSUE-004/003 — fixed in the workshop repo (`participant-policy.json` + bootstrap
  pre-warm), not here. The related "model not enabled in this account" hint in the
  model picker is a **new feature**, not a defect fix; deferred.
- ISSUE-006 / ISSUE-013 — Workshop Studio SCP / role restrictions. The app already
  degrades honestly; only message wording could improve. Deferred.
- ISSUE-008 — empty online-evaluator metrics for a 5-request bundle experiment could
  not be attributed (5 samples split 50/50 may be too few). Needs a fresh
  investigation with more traffic, not a speculative fix.

## Ordering

`quick-wins` → `readiness-panel` → `policy-create-gating` → `canary-s3-cleanup`.
Children are independent; this order is smallest-blast-radius first. Each child is
verifiable and committable on its own.
