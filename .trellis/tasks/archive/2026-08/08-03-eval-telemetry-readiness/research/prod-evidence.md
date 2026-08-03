# Production Evidence

Observed 2026-08-03 in the `us-east-1` production environment.

## Affected run

- Launchpad run: `083847972e73` (`run-083847` in the UI)
- Agent: `lab-fund-advisor-rt`
- Dataset: `lab-fund-dataset`
- AWS batch: `run_08384797-4d1b4ada19`
- AWS status: `COMPLETED_WITH_ERRORS`
- Result: 4 completed sessions, 1 failed session

The failed session was the fifth and final scenario,
`unknown-fact-refusal`, session
`3c62c8019ac545d48508d182fd40f5726a672be4fe1b4da18a21bb3d12f9c3d6`.
All four evaluators reported:

```text
LogEventMissingException: Session span data is incomplete. Span with ID:
7ed6107d5f275a6a and name: invoke_agent Strands Agents is missing a
corresponding log event.
```

The root span completed at `03:43:10.127Z`; its matching structured content
log was ingested by CloudWatch at `03:43:11.601Z`. The batch was created at
`03:45:10.946Z`, approximately 120.8 seconds after the root span completed.
The content log contains normal input/output and an appropriate answer that
declines to claim 2024 Q3 performance from 2021 source material.

The immediately preceding run, `a3870a9203de`, also failed only its fifth
session after approximately 120 seconds. That batch reported:

```text
ValidationException: Provided input has no spans to evaluate.
```

This establishes a repeatable race between the newest session telemetry and
AgentCore Evaluation's internal consumption/indexing, not an evaluator-score
failure or an Agent invocation failure.

## Read-only validation

The implemented parser was run against the affected production session with a
bounded `startTime`. It completed in 4 seconds, paired root span
`7ed6107d5f275a6a` with content ingestion time `1785728591601`, and passed the
180-second stability-age check. The same query without `startTime` scanned the
historical `aws/spans` group for more than one minute and was aborted, so the
bounded time range is part of the production contract.
