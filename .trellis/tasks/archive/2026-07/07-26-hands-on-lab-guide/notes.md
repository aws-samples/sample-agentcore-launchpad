# Execution notes — hands-on lab guide

## Post-review change: chapter 06 (public `/v1` API) is marked optional

The user first asked to drop the standalone public-API chapter, then to restore
it as **optional** instead. Final shape: still 12 chapters, chapter 06 carries a
🔀 可选 banner and stays in the numbering — nothing was renumbered, so all
filenames, image prefixes, figure captions and cross-references are the
originals.

Where "optional" is expressed, so a future edit keeps them in sync:
`docs/lab/README.md` (row marker + a paragraph defining what 可选 means),
chapter 06's opening banner, chapter 05's next-chapter line (which offers the
skip link straight to 07), chapter 07's prev-chapter line, chapter 01's route
table, and chapter 12's cleanup rows for the API key (only exists if 06 ran).

The one real coupling is called out in both places: the two `curl` sessions from
chapter 06 appear in chapter 07's session list, so a reader who skips 06 sees two
fewer rows there. No chapter 07+ conclusion depends on it.

## Spec-update judgment (Trellis 3.3)

Reviewed `.trellis/spec/launchpad/*` against what this task produced.
**Conclusion: no spec change.** Reasoning:

- The deliverable is product documentation (`docs/lab/`), not a code contract.
  The spec layer documents backend/frontend contracts; nothing in those
  contracts changed.
- Every platform behavior the guide relies on was already specified:
  method eligibility (`experiment-stepwise.md`, `evaluation-agent-eligibility.md`),
  KB attach topology (`managed-kb.md`), memory actor scoping (`memory-console.md`),
  Gateway tags + Cedar lifecycle (`gateway-policy-management.md`),
  registry lifecycle (`registry-skill-ingestion.md`). The live run **confirmed**
  these specs rather than contradicting them.
- The one genuinely new finding is a defect, not a contract: new 方式A container
  builds crash on start because `tracing.py` imports `opentelemetry._events`
  while `requirements.txt` pins only `aws-opentelemetry-distro>=0.10,<1`.
  Recorded per repo convention in
  `docs/issues/2026-07-26-container-otel-events-import.md` (not a spec change;
  the fix will own the spec text if any is needed).

## Live-run facts worth remembering (confirmations, not new contracts)

| Fact | Observed |
|---|---|
| zip_runtime deploy | 69 s (pip+zip 45.1 s, 37.3 MB; CREATING→READY 20 s) |
| harness deploy | 18 s (package stage skipped) |
| container deploy | 125 s (CodeBuild 1.8 min) — deploy OK, **invoke broken** |
| KB create→ACTIVE + ingest | ~2 min create; ingestion 2 min 15 s for a 957.8 KB PDF |
| harness republish w/ KB | 28 s; `provision` adds `kb gateway ready`; `UpdateHarness` → version 2 |
| batch eval (5 scenarios, 4 evaluators) | 4 min 38 s end to end (invoking → waiting 90 s → evaluating) |
| A/B verdict aggregation | ~15 min after last session (hard deadline 900 s in `act_verdict`) |
| A/B result with n=10 | `control-wins`, `significant: false` — platform labels it 无显著差异 |
| Registry record after republish | `UpdateRegistryRecord` resets A2A record status to `DRAFT` (observed) |
| Gateway manage/unmanage | adds/removes exactly the two `agentcore-launchpad:*` tags (verified via `list-tags-for-resource`) |
| Cedar policy create | `status: ACTIVE` + `enforcement_mode: LOG_ONLY` are separate dimensions |
| Scoped policy decisions | `available: false · policy_span_shape_not_verified` (as specified) |

## UI-driving notes (agent-browser)

- The console's type-filter chips and stage-action buttons re-render often;
  `@ref` handles from an earlier snapshot go stale. Driving them through
  `eval` + `document.querySelectorAll('button')[i].click()` was reliable.
- Several destructive/mutating actions have a **second confirm button with the
  same label** (republish, policy create, experiment cleanup, gateway manage).
  A single click looks like a no-op if you don't handle the dialog.
- Evaluator selection chips come **pre-selected**; clicking toggles off. The
  lab run accidentally deselected `正确性` this way — the guide documents the
  actual selection and warns about it.
- Screenshots: viewport 1600x1000, Chinese UI. Final PNGs quantized to 256
  colors (35.5 MB → 5.0 MB) with no visible loss.
