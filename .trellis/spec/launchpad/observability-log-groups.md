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

Root queries add `ROOT_SPAN_FILTER` only after the span predicate. Trace detail
also applies the span predicate before parsing `@message` into the span tree.
Prompt/event enrichment intentionally does not apply it.

### What a root span is (`ROOT_SPAN_FILTER`)

```text
(not ispresent(parentSpanId) or name = "POST /invocations")
```

- AgentCore Runtime and Harness never export a parentless span. Even
  `POST /invocations` carries the caller's `parentSpanId`, so "no parent" alone
  matched nothing on the us-east-1 hub (0 traces over 24h, measured 2026-10-14).
- **A trace can hold two `POST /invocations` spans** (a Harness and its backing
  runtime), so a root query is NOT one row per trace.
- **Anything that counts traces or takes latency percentiles must aggregate per
  `traceId` first.** Use `stats max(durationNano) as dur, max(is_error) as err by
  traceId[, bin(B) as bucket]`, then a second `stats count(*) … by bucket`. A plain
  `count(*)` counted that trace twice and skewed p95.
- The trace list keys root rows by `traceId` over a newest-first list, so the
  earliest-starting (outermost) root wins.
- Two-stage semantics, measured on us-west-2 `aws/spans` 7d (2026-10-14):
  - The intermediate `stats … by traceId` stage is **not** capped at the 10,000-row
    default result `limit` (that applies to final results only): a probe that grouped
    15,662 spans `by spanId` and re-counted kept all 15,632 groups.
  - `strcontains` yields 1/0, so `max(is_error)` then `sum(err)` counts error
    **traces** (7 = `count_distinct(traceId)` of error roots).
  - `bin()` is per span `@timestamp`, so a trace whose two roots fall in different
    bins counts once in each bucket. Sum of 7d series buckets = totals (1,746), so this
    is theoretical today. Don't "fix" it with `bin` in the second stage: the docs forbid
    it (the timestamp isn't propagated).

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

## Trace message normalization

Keep message decoding at the backend telemetry boundary. Both console versions
consume the existing `ObsSpanMessages` shape; they must not parse raw AWS event
formats independently.

Supported sources for a span's input/output panel:

| Source | Input | Output |
|---|---|---|
| Strands / Harness / native Claude content event | `body.input.messages` | `body.output.messages` |
| Bedrock per-message events | `gen_ai.system.message`, `gen_ai.user.message`, `gen_ai.assistant.message`, `gen_ai.tool.message` | `gen_ai.choice` |
| MCP tool-call span attributes | `gen_ai.tool.call.arguments` | `gen_ai.tool.call.result` |

Contracts:

- Historical assistant messages in a Bedrock request remain on the input
  side. Only a choice is the current completion; preserve its finish reason.
- Accept structured content blocks and serialized equivalents. A Strands
  envelope may carry both `message` and `tool.result`; neither may hide the
  other.
- Deduplicate alternate representations of the same tool call/result using
  its ID and content. Do not deduplicate ordinary repeated text messages.
- Order content events by their recorded timestamps, with stable input order
  when timestamps are absent. The bounded query must filter content records
  before applying its limit so ordinary logs cannot crowd them out.
- Keep message/block/text and message-span limits. Once the span limit is
  reached, continue processing later events for spans already admitted.
- Collect recognized content events' session IDs independently of rendering,
  including events beyond the message-span cap, empty/malformed payloads,
  and duplicates that add no visible message. This provenance feeds
  private-session checks before and after the response cache. Ordinary
  correlated application logs are not content events.
- Attribute fallback fills only a missing side of the **same span**. Never
  borrow a parent's or sibling's content: framework wrappers can contain a
  different history from the underlying model request.
- No recorded content is a valid state for HTTP, OAuth, and transport spans.
  Missing content must not be fabricated from timing or token counts.

The production incident that exposed this contract had content on all three
Bedrock provider spans, but the envelope-only parser returned no messages.
Two MCP tool spans also held arguments/results only in their attributes.
Use synthetic fixtures matching these shapes; production prompts and tool
results must stay out of test fixtures.

CloudWatch does not expose an entire discovered message array to
`ispresent(body.input.messages)`. That predicate missed all nine envelope
records in the incident even though their indexed child fields existed.
Parse the record first and inspect the map/list value:

```text
fields jsonParse(@message) as record
| filter ispresent(record.body.input.messages)
    or ispresent(record.body.output.messages)
    or record.eventName in [...]
```

When accessing a parsed attribute whose literal key contains a dot, quote it
with backticks, for example ``record.attributes.`event.name` ``.

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
- Message tests cover each source above, input-history versus completion,
  tool-result duplication, out-of-order events, malformed blocks, payload
  limits, and session provenance.
- Exact-span fallback must not replace existing event messages or assign
  content to a span that did not record any.
