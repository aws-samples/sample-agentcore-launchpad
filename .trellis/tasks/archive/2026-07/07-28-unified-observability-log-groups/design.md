# Design - AgentCore unified observability log groups

## Boundaries

Touched:

| File | Change |
|---|---|
| `backend/app/services/observability.py` | combined Logs Insights source, span-only filtering, metadata aggregation |
| `backend/tests/test_observability.py` | query-source, routing, and mixed-record regression coverage |
| `docs/architecture.md` | authoritative service mapping |
| `.trellis/spec/launchpad/observability-log-groups.md` | durable query contract |
| `.trellis/spec/launchpad/index.md` | spec index entry |

Not touched: runtime deployment environment, ADOT dependencies, AWS log-group
configuration, frontend response types, or the vendored Studio deployment code.

## Source selection

Account-wide queries begin with:

```text
SOURCE logGroups(
  namePrefix: ['aws/spans', '/aws/bedrock-agentcore/runtimes/']
)
```

`SOURCE` is part of the CWLI query string, so `_start_query` must omit
`logGroupName`/`logGroupNames` for these queries. This avoids the API's 50-group
enumeration ceiling; the live development account currently has 106 matching
groups.

Queries that already know exact groups keep passing `log_groups` to
`run_insights_queries`. `_start_query` then sends `logGroupNames` exactly as
today. This covers trace message enrichment and eval transcript reconstruction.

## Record discrimination

Unified groups contain:

- spans (`startTimeUnixNano`, `endTimeUnixNano`, `traceId`, `spanId`);
- OTel prompt/message events (`timeUnixNano`, `traceId`, `spanId`);
- structured application logs (often correlated with `traceId`/`spanId`);
- standard output streams.

`traceId` and absence of `parentSpanId` are not sufficient span predicates.
Every query whose numbers or rows represent spans adds:

```text
filter ispresent(startTimeUnixNano)
```

Session aggregation also requires `attributes.session.id`; raw prompt events are
not needed because AgentCore span telemetry carries the session id used by the
existing implementation.

## Metadata aggregation

Trace and session aggregation use `latest(...)` for string metadata:

- `attributes.session.id`
- `resource.attributes.service.name`
- `attributes.gen_ai.request.model`

CloudWatch accepts `max(string_field)` syntactically but returns an empty field,
which causes the console to map the agent to `unknown`. The implementation keeps
the existing single aggregation per trace/session and uses `latest()`; numeric
metrics retain `sum`, `min`, `max`, and `count_distinct`.

## Compatibility and failure behavior

- The legacy `aws/spans` prefix remains in the source, so existing and
  non-migrated agents remain visible.
- A missing prefix contributes no records. Explicit group lookups retain the
  existing `ResourceNotFoundException` to empty-result behavior.
- API payloads and cache keys do not change.
- Queries may scan more groups. Existing 60-second single-flight caching remains
  the cost/rate-control boundary.

## Rollback

Single commit revert. No database migration or AWS-side mutation is introduced.
