# Observability Log Groups - Legacy and Unified Telemetry

## Scope

Use this contract when changing account-wide Logs Insights queries in
`backend/app/services/observability.py`, trace/session aggregation, or runtime
log-group selection.

AgentCore has two telemetry layouts that can coexist in one Region:

| Runtime generation | Trace destination | Logs, prompts, and events |
|---|---|---|
| Legacy or not migrated | `aws/spans` | `/aws/bedrock-agentcore/runtimes/<agent>-<endpoint>` |
| Unified destination | `/aws/bedrock-agentcore/runtimes/<agent>-<endpoint>` | same per-runtime group |

Launchpad must read both layouts during the compatibility period.

## Account-wide source contract

Global dashboard, trace, and session queries start with:

```text
SOURCE logGroups(
  namePrefix: ['aws/spans', '/aws/bedrock-agentcore/runtimes/']
)
```

Do not enumerate discovered runtime groups into `StartQuery.logGroupNames`.
CloudWatch accepts at most 50 names there, while a normal shared development
account can exceed that. When the query includes `SOURCE`, `_start_query` must
omit all log-group parameters.

Queries that deliberately target known runtime groups are different:

- trace prompt/message enrichment;
- eval transcript reconstruction from content logs.

Those queries do not contain `SOURCE` and must pass their exact groups through
`logGroupNames`.

## One span reader — never `FilterLogEvents`

The session trace rail (`app/services/traces.py`, serving `GET /api/traces/{id}` for
`frontend/src/pages/Chat.tsx`) is bound by the same source contract as the account-wide
queries above. It previously had its own reader — `filter_log_events` against `aws/spans`
alone — which violated this contract two ways and was fixed 2026-07-30:

- **`FilterLogEvents` scans oldest-first from `startTime` with bounded pagination**, so
  it silently returned **zero** spans once the lookback exceeded a few hours. Measured: 0
  returned where 32 existed in `aws/spans`. `observability.py:1213` already documents the
  oldest-first behavior for its transcript read. Never use that API for spans; use
  `run_insights_queries` with `SPANS_SOURCE`.
- It read one layout, not both.

`filter_log_events` remains legitimate for **transcript/content-log** reads, which target
known groups explicitly.

## Per-agent groups mix spans with logs — filter for spans

A `/aws/bedrock-agentcore/runtimes/<agent>` group holds three kinds of record: spans,
structured application logs (`{timestamp, level, message, logger, requestId, sessionId}`),
and OTEL **log records** (`{body, severityText, observedTimeUnixNano, spanId}` — note no
`name`). All three can carry a session id, so a session-scoped substring filter matches
all three.

Any query that turns these records into span rows must therefore require a span:
`filter ispresent(name) or ispresent(spanName)`, plus a client-side guard so a later
query edit cannot readmit log records. Without it, 16 nameless rows entered one real
session's trace rail.

Related measurement worth not re-deriving: as of 2026-07-30 the per-agent groups in this
account contain **no spans at all** (checked with
`ispresent(name) and ispresent(traceId) and ispresent(startTimeUnixNano)`) — every span
is still in `aws/spans`. Reading both layouts is contract compliance for the migration,
not a fix for currently-lost data.

## Span-record discriminator

A unified group mixes spans with OTel events, prompts, structured application
logs, and standard output. Many non-span records still carry `traceId` and
`spanId`; ordinary logs usually also lack `parentSpanId`. Therefore:

```text
# WRONG - includes correlated logs/events
filter ispresent(traceId)

# WRONG - treats every ordinary log as a root span
filter not ispresent(parentSpanId)

# CORRECT
filter ispresent(startTimeUnixNano)
```

Root queries add `not ispresent(parentSpanId)` only after the span predicate.
Trace detail also applies the predicate before parsing `@message` into the span
tree. Prompt/event enrichment intentionally does not apply it.

## Aggregation metadata

Use numeric aggregates for numeric values and `latest()` for string metadata:

```text
latest(attributes.session.id) as session_id
latest(resource.attributes.service.name) as service
latest(attributes.gen_ai.request.model) as model
```

Do not use `max()` for these strings. CloudWatch may accept the query but omit
the result, which makes the console display `unknown` for the owning agent.

Session totals use `count_distinct(traceId)` after filtering to spans. LLM token
and call sums retain the terminal-provider rule that excludes the
`strands-agents` wrapper duplicate.

## Compatibility and operations

- Keep `aws/spans` in the source until support for non-migrated runtimes is
  intentionally retired.
- Missing prefixes naturally contribute zero records; explicit missing groups
  continue to degrade to empty results.
- The 60-second single-flight cache remains the scan-cost boundary.
- This read-path contract does not enable unified telemetry on existing
  runtimes. Setting `UNIFIED_TRACES_DESTINATION_ENABLED=true` and verifying the
  deployed ADOT version is a separate rollout with its own rollback plan.

## Required tests

- Every span-derived query includes `SOURCE` and
  `ispresent(startTimeUnixNano)`.
- Global query calls omit `logGroupName` and `logGroupNames`.
- Explicit event/content-log calls retain `logGroupNames`.
- Trace/session metadata uses `latest()` for session, service, and model.
- Existing endpoint response-shape tests remain green.
