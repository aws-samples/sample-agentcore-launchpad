# Policy telemetry shape research

## Status

Blocked on live span evidence as of 2026-07-16. Do not implement the production
decision parser from this document yet.

## Account observation

The configured `us-west-2` account has CloudWatch Transaction Search enabled
and the `aws/spans` log group exists. A read-only Logs Insights query over the
last 14 days returned zero spans whose operation name was `AuthorizeAction` or
whose `aws.remote.operation` was `AuthorizeAction`.

Query used:

```text
fields @timestamp, name, traceId, spanId, attributes
| filter name like /AuthorizeAction/
    or `attributes.aws.remote.operation` = "AuthorizeAction"
| sort @timestamp desc
| limit 100
```

No Gateway configuration or enforcement mode was changed during this research.
The likely missing prerequisite is Gateway trace delivery, but enabling it is
an AWS mutation and belongs in the explicitly confirmed real-AWS E2E flow.

## Official documented fields

AWS documents these fields at:
<https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-policy-metrics.html>

`AuthorizeAction`:

- `aws.agentcore.policy.authorization_decision` (`ALLOW|DENY`)
- `aws.agentcore.policy.authorization_reason`
- `aws.agentcore.policy.determining_policies`
- `aws.agentcore.policy.mismatched_policies`
- `aws.agentcore.policy.target_resource.id`
- `aws.agentcore.gateway.policy.arn`
- `aws.agentcore.gateway.policy.mode` (`LOG_ONLY|ENFORCE`)

`PartiallyAuthorizeActions`:

- `aws.agentcore.policy.allowed_tools`
- `aws.agentcore.policy.denied_tools`
- `aws.agentcore.policy.target_resource.id`
- `aws.agentcore.gateway.policy.arn`
- `aws.agentcore.gateway.policy.mode`

These are documentation evidence, not the required live ALLOW/DENY captures.
The documentation does not establish the principal summary, individual action
projection, Policy ID mapping semantics, or session-link aliases required by
the product contract.

## Remaining gate

1. Enable trace delivery on a disposable Gateway.
2. Capture ALLOW and DENY in Gateway `LOG_ONLY`.
3. Capture ALLOW and DENY in Gateway `ENFORCE`.
4. Record complete raw spans, including principal/action/session fields.
5. Define a bounded alias map and only then implement the parser and evidence
   counter.

Until this is complete, the scoped decisions endpoint returns
`available=false`, `reason=policy_span_shape_not_verified`, and promotions
require the documented zero-evidence override.
