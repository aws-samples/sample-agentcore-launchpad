# Live evidence - 2026-07-28, us-west-2

## Log-group topology

- `aws/spans` exists.
- 105 groups match `/aws/bedrock-agentcore/runtimes/`.
- A combined `SOURCE logGroups(namePrefix: [...])` query scanned all 106 groups,
  proving it avoids the `logGroupNames` maximum of 50.
- In the last 24 hours, records carrying `traceId` existed in both
  `aws/spans` (4,952) and a per-runtime group (6,497). The runtime-group records
  include OTel prompt events and ordinary correlated application logs, so
  `traceId` alone is not a span discriminator.

## Record shapes

The inspected per-runtime `otel-rt-logs` stream contained:

- ordinary logs with `timeUnixNano`, `traceId`, and `spanId`;
- `strands.telemetry.tracer` prompt/output events with the same identifiers and
  `attributes.session.id`;
- no `startTimeUnixNano` on those non-span records.

Span queries can therefore use `ispresent(startTimeUnixNano)` without admitting
prompt/log records.

## Query behavior

A combined-source session aggregate using the span predicate and:

```text
latest(resource.attributes.service.name) as service
latest(attributes.gen_ai.request.model) as model
```

returned populated values, including:

```text
session_id = ee760f57-2757-4761-947b-83f1ec6fa022
service    = lab_fund_assistant_c8fbf6.DEFAULT
model      = global.anthropic.claude-sonnet-5
traces     = 1
llm_calls  = 1
```

CloudWatch accepted `max()` over string metadata in earlier probes but omitted
the output value. That form must not be used because it maps the Agent to
`unknown`.
