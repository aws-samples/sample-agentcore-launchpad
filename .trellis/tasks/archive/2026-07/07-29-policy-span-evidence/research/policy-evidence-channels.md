# Policy decision evidence channels — verified account facts

Captured 2026-07-29 against account `434444145045` / `us-west-2` with read-only
calls. **This supersedes the conclusion of
`.trellis/tasks/archive/2026-07/07-16-gateway-policy-management/research/policy-telemetry-shape.md`**,
which recorded the span channel as "blocked on live evidence" without
establishing *why* it was empty and without examining the metric channel at all.

## Root cause of the empty span channel

AgentCore Policy spans are emitted only after **trace delivery is enabled on the
attached Gateway resource**. Trace delivery is not a field on the Gateway — it is
a CloudWatch vended-log *delivery*, created with three `logs` API calls:

```python
logs.put_delivery_source(name=..., logType="TRACES", resourceArn=<gateway_arn>)
logs.put_delivery_destination(name=..., deliveryDestinationType="XRAY")
logs.create_delivery(deliverySourceName=..., deliveryDestinationArn=...)
```

Verified with `describe-delivery-sources`: the account has TRACES deliveries for
`memory/LingoAceMemory-KDPsbEBvMa`, `runtime/acmesupport-fOfv652Bjq`,
`runtime/agentcorePaymentDemo-OPM3rC7p9G`, `runtime/nanoclawbot_dev-vZD0uEEpZa`
and `gateway/ac-gateway-mcp-server1-almrluqb6h` — but **not** for
`gateway/launchpad-gw-em0yuqmmdp` or `gateway/launchpad-kb-gw-pmyq7mchum`.

So the launchpad gateways never had a span channel. The 0-result Logs Insights
query is explained, and no span-shape mystery is involved.

Prerequisites already satisfied: Transaction Search is `CloudWatchLogs` /
`ACTIVE` (`xray get-trace-segment-destination`), and `aws/spans` exists
(16.2 MB, 30-day retention). Gateway MCP traffic already reaches `aws/spans`
(`mcp tools/call …___Retrieve`, `mcp tools/list`), confirming the account-level
plumbing works — only the per-gateway TRACES delivery is missing.

Re-ran the archived query over the last 14 days: 0 matched / 13 660 scanned for
`name like /Authorize/ or attributes.aws.remote.operation like /Authorize/`.

## The metric channel is default-on and already populated

Per
<https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-policy-metrics.html>:
policy/policy-engine invocation metrics are published to the
`AWS/Bedrock-AgentCore` namespace **by default**; only *span* data requires
enabling gateway traces.

Metrics: `AllowDecisions`, `DenyDecisions`, `DeterminingPolicies`,
`NoDeterminingPolicies`, `PolicyMismatch`, `MismatchErrors`,
`TotalMismatchedPolicies`, plus `Invocations` / `Latency` / `SystemErrors` /
`UserErrors`.

Dimensions: `OperationName` (`AuthorizeAction` | `PartiallyAuthorizeActions`),
`PolicyEngine`, `Policy`, `TargetResource` (gateway id), `ToolName`,
`Mode` (`LOG_ONLY` | `ENFORCE`).

`list-metrics` counts in this account: `AllowDecisions` 46 streams,
`DenyDecisions` 36, `DeterminingPolicies` 7, `NoDeterminingPolicies` 4,
`MismatchErrors` 0. Dimension sets are **sparse** — AWS publishes several
projections of the same event (e.g. `{OperationName}` alone,
`{OperationName, PolicyEngine}`, `{Policy, TargetResource, OperationName, Mode}`),
so summing across all returned streams double-counts. A parser must pick one
dimension projection per question.

Both launchpad gateways appear:

- `{Policy: launchpad_baseline_allow-obafj1o9hj, TargetResource: launchpad-gw-em0yuqmmdp, OperationName: AuthorizeAction, Mode: ENFORCE}`
- `{TargetResource: launchpad-gw-em0yuqmmdp, ToolName: hr-database___create_payout, OperationName: PartiallyAuthorizeActions, Mode: ENFORCE}` (DENY)
- `{Policy: kb_demo_m2m_retrieve-u9ya6gh7o8, TargetResource: launchpad-kb-gw-pmyq7mchum, …}`
- `{TargetResource: launchpad-kb-gw-pmyq7mchum, ToolName: agentic-aurora-support___AgenticRetrieveStream, OperationName: PartiallyAuthorizeActions, Mode: ENFORCE}` (DENY)

`get-metric-data`, `Period=86400`, `Stat=Sum`, 30-day window — real datapoints:

| series | datapoints |
|---|---|
| `DenyDecisions` {launchpad-gw, hr-database___create_payout, PartiallyAuthorizeActions, ENFORCE} | 07-16: 2 · 07-15: 2 · 07-13: 1 · 07-12: 33 · 07-10: 2 · 07-09: 24 |
| `AllowDecisions` {AuthorizeAction} | 07-16: 2 · 07-15: 1 · 07-13: 2 · 07-12: 38 · 07-09: 15 |
| `DenyDecisions` {AuthorizeAction} | 07-26: 2 · 07-25: 3 · 07-16: 1 · 07-13: 1 · 07-09: 3 |

The `create_payout` DENY series is the same real Cedar block that
`docs/lab/11-governance.md` documents from the local ledger — so the metric
channel independently corroborates the demo.

## What each channel can and cannot answer

| Question | Metrics | Spans |
|---|---|---|
| How many ALLOW / DENY in a window | yes | yes |
| Per policy / tool / gateway / mode breakdown | yes (dimensions) | yes (attributes) |
| Is there LOG_ONLY evidence for the ENFORCE promotion gate | **yes** | yes |
| Which principal was denied | no | yes (`AuthorizeAction` span) |
| Human-readable decision reason | no | `aws.agentcore.policy.authorization_reason` |
| Determining / mismatched policy ids | count only | id list |
| Trace / session link for drill-down | no | traceId + spanId |

Consequence for the product contract: `GovernancePolicyDecision`
(`frontend/src/lib/api.ts:535`) requires `principal`, `action`, `trace_id`,
`session_id` — **metrics cannot populate per-decision rows**. The metrics track
must extend the response with an aggregate shape rather than fake row data.

## Documented span attributes (for the span track)

`AuthorizeAction`: `aws.agentcore.policy.authorization_decision` (ALLOW|DENY),
`…authorization_reason`, `…determining_policies`, `…mismatched_policies`,
`…target_resource.id`, `aws.agentcore.gateway.policy.arn`,
`aws.agentcore.gateway.policy.mode`.

`PartiallyAuthorizeActions`: `aws.agentcore.policy.allowed_tools`,
`…denied_tools`, `…target_resource.id`, `aws.agentcore.gateway.policy.arn`,
`aws.agentcore.gateway.policy.mode`.

Still documentation-only. The span track must confirm these against captured
spans before a parser is written — principal and session aliases are **not**
documented and must come from live capture.

## Gateway state at capture time

`launchpad-gw-em0yuqmmdp` — name `launchpad-gw`, `READY`, `MCP`, `CUSTOM_JWT`,
`policyEngineConfiguration = {arn: …policy-engine/launchpad_pe-rwtcceczvs, mode: ENFORCE}`,
`updatedAt 2026-07-09T12:48:59Z`. No Gateway-resource change is needed to enable
traces.

## Reproduction commands

```bash
aws xray get-trace-segment-destination --region us-west-2
aws logs describe-delivery-sources --region us-west-2
aws cloudwatch list-metrics --region us-west-2 \
  --namespace AWS/Bedrock-AgentCore --metric-name DenyDecisions
aws cloudwatch get-metric-data --region us-west-2 \
  --start-time "$(date -u -d '30 days ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --metric-data-queries file://mq.json
```

## Caveat for the 24 h evidence gate

The most recent decision datapoint is 07-26; nothing in the last 24 h, because
nothing has called the gateways since. A real 24 h-window evidence check will
still read 0 until traffic is generated. This is correct behavior, not a
regression — but any lab or test that asserts non-zero evidence must first
invoke a gateway tool.
