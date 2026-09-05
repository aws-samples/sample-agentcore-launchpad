## Summary

Two small console polish items, both locales:

- **Stale "phase" copy.** The Chat trace rail placeholder still promised that the trace rail "arrives with Observability (Phase 9)" even though Observability and the rail itself shipped long ago; it now says what happens ("send a message — the trace rail loads spans for this session"). The Create-page summary "auto-register → Registry (phase 7)" drops the phase reference. The deliberate `PHASE 02` / footer / Payments-deferred strings are untouched.
- **Accessible names for every visible form control.** Ten `<select>`/`<input>`/`<textarea>` elements had no label, `aria-label`, or placeholder (Chat agent picker; Experiments agent + baseline dataset; Evaluators model/description/instructions; Datasets name/description; Online sampling/timeout). Each now carries an `aria-label` through `t(...)`, reusing the adjacent visible label key; the agent picker gets one new key.

## Verification

- `make verify` PASS.
- Playwright probe on a worktree build across all 30 console routes, en and zh-CN: 0 controls without an accessible name (baseline before the change: 10).
- `grep -n "PHASE 9\|phase 7" frontend/src/locales/*/common.json` → no matches; `nav.phase02`, `footer.phase`, `footer.payments`, `agent.method_not_available` have no diff hunk.

Self-evolution direction SE-003 (`ux` path).
