# Wait for evaluation telemetry readiness before batch evaluation

## Goal

Prevent fresh dataset evaluations from submitting `StartBatchEvaluation` while
the newest runtime session is not yet safely consumable by AgentCore
Evaluation.

## Requirements

- Preserve the existing run pipeline and account-level batch lock.
- For runs that invoke fresh dataset traffic, verify that the newest session
  has both:
  - its latest `invoke_agent Strands Agents` root span in `aws/spans`; and
  - the matching structured `input`/`output` content log in the runtime log
    group.
- Replace the blind caller-provided sleep with active readiness polling.
- Include a backend-owned ingestion-age threshold in readiness because
  AgentCore Evaluation exposes no direct index-readiness API.
- Bound readiness polling. If telemetry remains incomplete, fail the run with
  an actionable error before creating an AWS batch evaluation.
- Do not add readiness delay to passive time-window runs or evaluations over
  existing session ids.
- Continue treating AWS `COMPLETED_WITH_ERRORS` as a completed partial result
  after a batch has started.
- Support both Runtime-backed and managed Harness agents through their already
  resolved content-log group.
- Keep the implementation testable without AWS credentials and avoid changes
  to unrelated dirty worktree files.

## Acceptance Criteria

- [ ] A fresh dataset run cannot call `StartBatchEvaluation` until the newest
      session's latest root span has a matching content log and that log is
      old enough to satisfy the Evaluation-index stability threshold.
- [ ] A bounded timeout records an actionable failed run and creates no batch
      evaluation.
- [ ] Passive time-window and existing-session runs retain their current
      no-preflight behavior.
- [ ] Unit tests cover delayed readiness, mismatched span/log data, timeout,
      orchestration ordering, and passive-run bypass.
- [ ] The evaluation spec documents the readiness and stabilization contract.
- [ ] `make verify` passes.

## Notes

- Production incident: `run-083847` completed with 4/5 sessions because
  AgentCore Evaluation reported `LogEventMissingException` for the final
  session even though the span and content log became visible in CloudWatch.
- The preceding run over the same dataset also lost its final session with
  `Provided input has no spans to evaluate`.
