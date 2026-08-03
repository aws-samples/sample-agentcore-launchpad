# Implementation Plan

- [x] Add a focused telemetry-readiness helper that pages CloudWatch results,
      pairs the latest root span with its structured content event, and polls
      with a bounded timeout.
- [x] Integrate the helper after the existing ingestion wait and before
      `StartBatchEvaluation` for fresh dataset runs, replacing the blind wait.
- [x] Make readiness include the matching content event's ingestion age and
      raise the UI/backend default from 120/90 seconds to 180 seconds.
- [x] Add hermetic helper and orchestration regression tests, including
      Runtime/Harness-compatible log-group handling and passive-run bypass.
- [x] Update `.trellis/spec/launchpad/evaluation-cloud-dataset-runs.md`.
- [x] Run targeted backend lint/tests, then `make verify`.
- [x] Review the diff for scope and confirm unrelated worktree changes remain
      untouched.

## Verification

- `backend`: evaluation suite `67 passed`; full suite `1005 passed`.
- `infra`: `9 passed`.
- Frontend ESLint, TypeScript, Vite build, and i18n parity passed.
- Read-only production probe paired span `7ed6107d5f275a6a` with its content
  event and passed the 180-second readiness threshold in 4 seconds.
