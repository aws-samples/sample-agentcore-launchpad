# Design

## Current failure

Fresh dataset runs invoke scenarios sequentially, sleep for the request's
`wait_seconds`, and immediately call `StartBatchEvaluation`. Production uses
120 seconds. AgentCore Evaluation can lag behind the CloudWatch APIs: the
newest root span and its content event were already queryable, but the
evaluation service still failed to pair them.

Increasing one frontend constant alone would reduce frequency but would not
detect genuinely incomplete telemetry or protect API callers.

## Proposed flow

For fresh dataset traffic only:

1. Invoke scenarios and persist every runtime session id as today.
2. Poll CloudWatch for the final session, which is the ingestion watermark for
   sequential replay:
   - read matching events from `aws/spans`;
   - select the latest `invoke_agent Strands Agents` root span;
   - read matching events from the resolved runtime/Harness log group;
   - require a structured content event with the same span id and both
     `body.input` and `body.output`.
   Both queries are bounded by the replay start time minus 60 seconds for clock
   skew; an unbounded `aws/spans` filter was live-probed and scanned account
   history for more than one minute.
3. Treat the telemetry as Evaluation-ready only when the matching content
   event's CloudWatch `ingestionTime` has reached the requested stability age.
   Production callers currently request 120 seconds; this fix raises the UI
   and backend default to 180 seconds based on the repeated 120-second race.
4. Start the batch evaluation.

Polling is bounded to the requested stability age plus 120 seconds, at a
5-second interval. A timeout raises an actionable runtime error before an AWS
batch is created. Existing-session and time-window scopes skip this flow
because they do not create fresh telemetry.

## Boundaries

- Put CloudWatch event parsing/polling in
  `backend/app/evaluation/telemetry.py`; orchestration remains in `service.py`.
- Inject the Logs client, clock, and sleeper into the helper so unit tests are
  hermetic.
- Use the already resolved content-log group, so Runtime and Harness share the
  same readiness contract.
- Do not change score parsing, AWS partial-result handling, queue behavior, or
  the public run schema.

## Tradeoffs

- The final session is used as a watermark instead of issuing two CloudWatch
  queries per dataset item. Replays are sequential, so earlier sessions have
  had at least as long to ingest.
- The ingestion-age threshold is part of active readiness rather than a blind
  sleep: late log arrival moves the threshold, old complete telemetry passes
  immediately, and missing/mismatched telemetry times out explicitly.
- The age threshold remains necessary because the production failure occurred
  after raw logs were visible and AgentCore exposes no direct
  evaluation-index readiness API.
- A pre-batch timeout fails the run rather than knowingly creating a partial
  paid evaluation with incomplete input.

## Rollback

Remove the preflight call from `execute_run` and the new telemetry module. No
database or API migration is involved.
