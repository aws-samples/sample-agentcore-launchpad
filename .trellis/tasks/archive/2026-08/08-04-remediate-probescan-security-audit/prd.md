# Remediate ProbeScan security audit findings

## Goal

Resolve every finding in
`ProbeScanExport-84897f5e-e6ea-469c-82fc-219154aba214-main-20260804.csv`
without weakening runtime behavior or hiding unaudited risk. Each of the 94
records must end in one of two states:

1. the vulnerable behavior or dependency is removed; or
2. an audit-only/false-positive finding is explicitly suppressed at the exact
   site with a short, verifiable trust-boundary rationale.

## Requirements

- Reconcile all 94 CSV records and preserve a count-complete disposition matrix.
- Upgrade the listed vulnerable dependency versions wherever they occur in the
  active application manifests or lock files:
  - npm: `minimatch` 3.1.2 and 9.0.5, `tar` 7.4.3, and `rollup` 4.50.1.
  - Python: `python-multipart` 0.0.20 and `aiohttp` 3.10.11.
- Regenerate dependency locks with the owning package managers. Do not edit lock
  files by hand.
- Fix the actionable source findings:
  - run the Studio ECS image as a non-root user;
  - construct WebSocket URLs so HTTPS origins/configured HTTPS API bases always
    use WSS while HTTP development remains usable;
  - remove the mockup's `innerHTML` assignment;
  - remove the redundant Studio invoke-label ternary.
- Audit every reported subprocess call. Preserve argv-based, `shell=False`
  execution where it is already safe; do not add `shlex.escape()` to argv
  elements because that changes the arguments instead of improving safety.
- Keep the exported Harness fixture verbatim in behavior. It is test data loaded
  as text, not an executed platform code path.
- Keep intentional polling/retry delays. Do not replace them with busy loops or
  scanner-evasion wrappers.
- Mark only verified audit false positives with rule-specific inline
  suppressions and a nearby rationale. Do not ignore whole files or disable a
  scanner globally.
- Treat `apps/studio/` as the active vendored integration. Do not modify
  `vendor-src/strands_studio_ui` or other upstream mirrors.
- Preserve the supplied CSV as an untracked input artifact; do not commit it.
- Keep documentation and Trellis artifacts in English.

## Acceptance Criteria

- [ ] The disposition matrix accounts for exactly 94 findings with no
      unclassified record.
- [ ] None of the six dependency versions named by the report remains in an
      active manifest or resolved active lock entry.
- [ ] Current package audits no longer report the listed advisory families for
      the remediated packages; unrelated newer advisories are reported
      separately rather than conflated with this CSV.
- [ ] Every reported dynamic subprocess/async-exec call has either a real code
      fix or a narrow audit annotation backed by `shell=False`/argv provenance.
- [ ] Every reported intentional wait, decorated callback, fixture-only shell
      call, and public resource name has a rule-specific audit annotation.
- [ ] The Studio ECS Dockerfile has a final non-root `USER`, and an image build
      or equivalent config inspection confirms the runtime user when Docker is
      available.
- [ ] HTTP development produces `ws:` and HTTPS/configured HTTPS bases produce
      `wss:` without string-replacement ambiguity.
- [ ] The mockup contains no `innerHTML`/`outerHTML` assignment.
- [ ] `make verify` passes.
- [ ] Studio's `npm run build` passes. Full Studio lint is run and either
      passes or its pre-existing baseline is proven unchanged for touched
      legacy files; every newly added file must lint clean.
- [ ] The affected Studio page loads without a framework overlay or relevant
      console errors, and the target invoke/deployment surface remains usable.
- [ ] Any unavailable original ProbeScan rerun, Docker validation, or live AWS
      validation is called out explicitly; a failed audit command is never
      presented as a clean result.

## Notes

- The source report contains 14 dependency findings and 80 source findings.
- The source report is a finding list, not a reproducible scanner invocation.
  Verification therefore combines package-native audits, targeted static
  checks, build/test gates, and ProbeScan rerun only if its CLI/config is
  discoverable.
- Implementation-time baseline discovery: full Studio ESLint already contains
  85 errors across vendored legacy files. The two touched legacy TypeScript
  files retain the same 6 and 10 errors when compared with `HEAD`; the new
  `websocket-url.ts` helper has zero lint findings, and Studio build/type-check
  passes. Fixing the unrelated lint baseline is outside this report.
