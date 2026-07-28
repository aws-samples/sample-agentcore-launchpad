# Support AgentCore unified observability log groups

## Problem

Amazon Bedrock AgentCore now sends traces, prompts, structured logs, and standard
output for newly created runtimes to the per-agent log group:

`/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>`

Launchpad's Observability service still runs every dashboard, trace, and session
query only against the legacy shared `aws/spans` group. Agents using the unified
destination therefore disappear from the console even though their telemetry is
present in CloudWatch.

The new group also mixes span records with OTel events and ordinary application
logs. Queries that identify root spans only with `not ispresent(parentSpanId)` or
count every record sharing a `traceId` would count non-span records as spans.

## Goal

Keep the Observability dashboard, trace list/detail, and session list/detail
working across the legacy and unified AgentCore telemetry layouts without
double-counting non-span records or losing agent/model metadata.

## Requirements

### R1 - Read both telemetry layouts

- Account-wide span queries must include `aws/spans` and every per-runtime group
  under `/aws/bedrock-agentcore/runtimes/`.
- Use a Logs Insights `SOURCE logGroups(namePrefix: ...)` expression so accounts
  with more than the `StartQuery.logGroupNames` limit of 50 groups still work.
- Queries explicitly scoped to one or more runtime groups (prompt/event
  enrichment and eval transcript reconstruction) must keep their current
  `logGroupNames` behavior.

### R2 - Count only span records

- Trace counts, root latency/error aggregates, tool aggregates, and raw trace
  detail must require the span-only `startTimeUnixNano` field.
- Session aggregates must count distinct traces and sum span metrics only from
  span records.
- OTel prompt events and ordinary stdout records in a unified group must not
  inflate span, error, latency, token, or tool counts.

### R3 - Preserve metadata and compatibility

- Trace/session rows must keep resolving `service`, `agent`, `session_id`, and
  `model`; string metadata must use `latest()` rather than numeric `max()`.
- Legacy-only accounts and missing log groups must continue to degrade to valid
  empty results rather than 500 responses.
- Existing API response shapes, frontend types, cache behavior, and range
  semantics remain unchanged.

### R4 - Document the source-of-truth change

- The authoritative architecture and Launchpad spec must describe the dual
  legacy/unified read path and the span-record discriminator.
- The change must not automatically migrate existing runtimes or inject
  `UNIFIED_TRACES_DESTINATION_ENABLED`; runtime migration is an independent
  operational decision.

## Acceptance Criteria

- [x] Every account-wide Logs Insights query selects both legacy and per-runtime
      log groups without enumerating them through `logGroupNames`.
- [x] Unit tests prove explicit runtime-group queries still pass
      `logGroupNames`, while global span queries rely on `SOURCE`.
- [x] Unit tests prove trace/session/root/tool queries require span records and
      preserve service/session/model metadata.
- [x] Existing Observability endpoint tests pass without response-shape changes.
- [x] A live Logs Insights query in `us-west-2` returns populated session agent
      and model metadata across the combined source.
- [x] `make verify` passes.

## Notes

- Current generated container agents pin `aws-opentelemetry-distro==0.19.*`;
  zip runtimes resolve `>=0.10,<1` on fresh packaging. No dependency change is
  required for reading unified telemetry.
