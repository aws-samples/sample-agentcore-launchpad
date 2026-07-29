# Policy decision span corpus — captured 2026-07-29

**First Policy spans ever observed in account `434444145045` / `us-west-2`.** They
appeared within minutes of the `TRACES` delivery created by
`07-29-gateway-traces-delivery` (commit `5103a93`), which confirms that delivery —
not an unknowable span shape — was the only thing missing.

## How the traffic was driven

Not via `policy-test` / `mcp_client` (its demo Cognito credentials are rejected).
Instead the `hr-database` **Harness agent** was invoked through the platform's own
chat endpoint. That path authenticates to the Gateway with **OAuth
`CLIENT_CREDENTIALS`** against the token-vault provider `launchpad-gw-m2m` — a
completely different auth path from the demo user passwords, and it works:

```bash
curl -sN -X POST localhost:8000/api/chat/90d6f49922c14769bf7972864dbee090 \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"List all departments in the company.","actor_id":"span-capture"}'
```

Harness `hr_database-xXGENsMr2F`, `allowedTools: ["*"]`, model
`global.anthropic.claude-sonnet-5`, gateway
`launchpad-gw-em0yuqmmdp` in `ENFORCE`.

Query used:

```text
fields @message | filter name like /AgentCore.Policy/ | sort @timestamp desc | limit 10
```

Captured trace: `6a6a005492b3acc3e318e0f22ae9909e` (31 spans).

## Span names

Documented AWS attribute tables do not give the span names. They are:

- `AgentCore.Policy.AuthorizeAction` — `kind=CLIENT`
- `AgentCore.Policy.PartiallyAuthorizeActions` — `kind=CLIENT`
- `AgentCore.Gateway.InvokeTool` — `kind=SERVER` (**also carries the decision**)
- `AgentCore.Gateway.InvokeTool.<tool-name>` — `kind=CLIENT`
- `AgentCore.Gateway.ListTools` — `kind=SERVER`

## Verbatim span 1 — `AgentCore.Policy.AuthorizeAction` (ALLOW, ENFORCE)

```json
{
  "name": "AgentCore.Policy.AuthorizeAction",
  "kind": "CLIENT",
  "traceId": "6a6a005492b3acc3e318e0f22ae9909e",
  "spanId": "e94f9481b9a43495",
  "parentSpanId": "08b21d1592a9b9aa",
  "startTimeUnixNano": 1785331800758000000,
  "endTimeUnixNano": 1785331800813000000,
  "durationNano": 55000000,
  "status": {"code": "OK"},
  "attributes": {
    "PlatformType": "AWS::BedrockAgentCore",
    "aws.agentcore.gateway.policy.arn": "arn:aws:bedrock-agentcore:us-west-2:434444145045:policy-engine/launchpad_pe-rwtcceczvs",
    "aws.agentcore.gateway.policy.mode": "ENFORCE",
    "aws.agentcore.policy.authorization_decision": "ALLOW",
    "aws.agentcore.policy.determining_policies": ["launchpad_baseline_allow-obafj1o9hj"],
    "aws.agentcore.policy.log_only_matched_policies": ["lab_readonly_tools-be45dja2_p"],
    "aws.agentcore.policy.mismatched_policies": [],
    "aws.agentcore.policy.target_resource.id": "launchpad-gw-em0yuqmmdp",
    "aws.agentcore.policy.types": [["launchpad_baseline_allow-obafj1o9hj", "Cedar"]],
    "aws.local.environment": "bedrock-agentcore:default",
    "aws.local.operation": "UnmappedOperation",
    "aws.local.service": "launchpad-gw-em0yuqmmdp",
    "aws.remote.operation": "AuthorizeAction",
    "aws.remote.service": "AWS::launchpad_pe-rwtcceczvs",
    "aws.request.id": "5eaaa11d-64cc-445e-8c6e-948ebe76f632",
    "aws.resource.arn": "arn:aws:bedrock-agentcore:us-west-2:434444145045:gateway/launchpad-gw-em0yuqmmdp",
    "aws.resource.type": "AWS::BedrockAgentCore::Gateway",
    "aws.span.kind": "CLIENT",
    "aws.xray.origin": "AWS::BedrockAgentCore::Gateway",
    "http.response.status_code": 200,
    "http.status_code": 200,
    "rpc.method": "AuthorizeAction",
    "rpc.service": "launchpad_pe-rwtcceczvs",
    "rpc.system": "aws-api",
    "telemetry.extended": "true"
  },
  "resource": {"attributes": {
    "cloud.platform": "aws_bedrock_agentcore",
    "cloud.provider": "aws",
    "cloud.resource_id": "arn:aws:bedrock-agentcore:us-west-2:434444145045:gateway/launchpad-gw-em0yuqmmdp",
    "service.name": "launchpad-gw-em0yuqmmdp"
  }}
}
```

## Verbatim span 2 — `AgentCore.Policy.PartiallyAuthorizeActions` (contains a DENY, ENFORCE)

```json
{
  "name": "AgentCore.Policy.PartiallyAuthorizeActions",
  "kind": "CLIENT",
  "traceId": "6a6a005492b3acc3e318e0f22ae9909e",
  "spanId": "9ca47dd6a8e67249",
  "parentSpanId": "857a1f35baff7a19",
  "startTimeUnixNano": 1785331797355000000,
  "endTimeUnixNano": 1785331797440000000,
  "durationNano": 85000000,
  "status": {"code": "OK"},
  "attributes": {
    "aws.agentcore.gateway.policy.arn": "arn:aws:bedrock-agentcore:us-west-2:434444145045:policy-engine/launchpad_pe-rwtcceczvs",
    "aws.agentcore.gateway.policy.mode": "ENFORCE",
    "aws.agentcore.policy.allowed_tools": [
      "hr-database___check_calendar",
      "hr-database___get_employee",
      "hr-database___list_departments",
      "office-facts___get_office_fact",
      "office-facts___list_office_topics"
    ],
    "aws.agentcore.policy.denied_tools": ["hr-database___create_payout"],
    "aws.agentcore.policy.target_resource.id": "launchpad-gw-em0yuqmmdp",
    "aws.remote.operation": "PartiallyAuthorizeActions",
    "aws.request.id": "9387e92e-f6a3-4e4b-ad2e-120659d2c934",
    "aws.resource.arn": "arn:aws:bedrock-agentcore:us-west-2:434444145045:gateway/launchpad-gw-em0yuqmmdp",
    "rpc.method": "PartiallyAuthorizeActions"
  }
}
```

(Other attributes identical in shape to span 1; omitted only where byte-identical.)

## Verbatim span 3 — `AgentCore.Gateway.InvokeTool` (SERVER) — the most useful one

Attribute keys, with the load-bearing values:

```json
{
  "name": "AgentCore.Gateway.InvokeTool",
  "kind": "SERVER",
  "spanId": "08b21d1592a9b9aa",
  "attributes": {
    "tool.name": "hr-database___list_departments",
    "aws.agentcore.policy.authorization_decision": "ALLOW",
    "aws.agentcore.gateway.policy.mode": "ENFORCE",
    "aws.agentcore.harness_id": "...",
    "aws.agentcore.endpoint_qualifier": "...",
    "gateway.id": "...", "gateway.name": "...",
    "aws.request.id": "...", "aws.account.id": "...", "aws.region": "...",
    "execute_tool_latency_ms": 289, "latency_ms": ..., "overhead_latency_ms": ...,
    "aws.operation.name": "...", "url.path": "...", "http.method": "...",
    "http.response.status_code": 200
  }
}
```

**This single span carries the exact action *and* the decision** — no join needed
for the common case.

## Trace shape (matters for the parser)

```
POST /invocations                (SERVER, root)  ← session.id lives here
└── invoke_agent Strands Agents
    └── execute_event_loop_cycle
        ├── mcp tools/list
        │   └── AgentCore.Gateway.ListTools            (SERVER)
        │       └── AgentCore.Policy.PartiallyAuthorizeActions
        └── execute_tool hr-database___list_departments
            └── mcp tools/call hr-database___list_departments   ← gen_ai.tool.name, session.id
                └── AgentCore.Gateway.InvokeTool       (SERVER)  ← tool.name + decision
                    ├── AgentCore.Policy.AuthorizeAction
                    └── AgentCore.Gateway.InvokeTool.<tool>
```

## What the product contract can and cannot get

`GovernancePolicyDecision` (`frontend/src/lib/api.ts`) field by field:

| Field | Source | Verified |
|---|---|---|
| `outcome` | `aws.agentcore.policy.authorization_decision`, on both the Policy span and the `InvokeTool` SERVER span | yes |
| `action` | `tool.name` on `AgentCore.Gateway.InvokeTool`; `denied_tools`/`allowed_tools` for the partial op | yes |
| `policy_id` | `aws.agentcore.policy.determining_policies[]` | yes |
| `engine_id` | derivable from `aws.agentcore.gateway.policy.arn` | yes |
| `policy_mode` / `engine_mode` | `aws.agentcore.gateway.policy.mode` | partially — one attribute, and it is the **Gateway attachment** mode; the per-policy mode is not in the span |
| `gateway_id` | `aws.agentcore.policy.target_resource.id` / `gateway.id` | yes |
| `trace_id` | span `traceId` | yes |
| `session_id` | `session.id` on the root `POST /invocations` and the `mcp tools/call` spans — needs a **traceId join** | yes, via join |
| `at` | `startTimeUnixNano` | yes |
| **`principal`** | **nowhere** | **NOT AVAILABLE** |

### `principal` is not obtainable from spans for this traffic

No span in the 31-span trace carries any principal / user / actor / subject /
identity attribute. Searched every span's attribute keys for
`principal|session|user|actor|identity|auth|subject`; only `session.id` matched.

This is structural, not a sampling artifact: the Harness authenticates to the
Gateway with an **OAuth M2M client credential**, so there is no human principal in
the request. Per the child PRD, this field must therefore render as **absent**, not
be back-filled or inferred. The local demo ledger keeps its own `principal` for the
`policy-test` surface; the two must not be conflated.

### Two undocumented attributes, one of them valuable

Neither appears in
<https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-policy-metrics.html>:

- **`aws.agentcore.policy.log_only_matched_policies`** → `["lab_readonly_tools-be45dja2_p"]`.
  This is the LOG_ONLY *candidate* policy created in the lab chapter. It means an
  **ENFORCE-mode span reveals what a LOG_ONLY candidate would have matched** —
  exactly the evidence the promotion gate wants, and something the metric channel
  cannot express. Strong candidate for the decision detail row.
- `aws.agentcore.policy.types` → `[["launchpad_baseline_allow-obafj1o9hj", "Cedar"]]`
  — policy id paired with its language.

### Documented but absent

`aws.agentcore.policy.authorization_reason` is documented for `AuthorizeAction` but
is **not present** on the captured ALLOW span. Whether it appears on a DENY is
still unverified — see the gap below.

## RESOLVED 2026-07-29 (after `07-29-policy-test-honesty`)

The gap below was real for *Harness-driven* traffic but not absolute. `policy-test`
issues `tools/call` with **no preceding `tools/list`**, so the Gateway runs
`AuthorizeAction` per call and a call-time DENY is reachable — no `LOG_ONLY` switch
needed. Captured:

```json
{
  "name": "AgentCore.Policy.AuthorizeAction",
  "attributes": {
    "aws.agentcore.policy.authorization_decision": "DENY",
    "aws.agentcore.policy.authorization_reason": "Policy evaluation denied due to launchpad_payout_admin_only-x7gz5yjkrd",
    "aws.agentcore.policy.determining_policies": ["launchpad_payout_admin_only-x7gz5yjkrd"],
    "aws.agentcore.policy.mismatched_policies": [],
    "aws.agentcore.policy.types": [["launchpad_payout_admin_only-x7gz5yjkrd", "Cedar"]],
    "aws.agentcore.gateway.policy.mode": "ENFORCE"
  }
}
```

So **`authorization_reason` exists, on DENY only** — absent on ALLOW, which is why the
ALLOW-only capture below concluded otherwise. And symmetrically
`log_only_matched_policies` is present on the ALLOW span but **absent on this DENY**:
both attributes are conditional and must be read defensively.

The text below is kept as the original ENFORCE/Harness finding, which still holds for
that path.

## Gap: no `AuthorizeAction` DENY via a Harness, and why ENFORCE cannot produce one

The DENY attempt ("Create a payout of 1 USD for EMP-1024") produced **no**
`AuthorizeAction` DENY span. The reason is structural and worth recording:

In `ENFORCE` mode, `PartiallyAuthorizeActions` runs at `tools/list` and **filters
the denied tool out of the tool list the model ever sees** (`denied_tools:
["hr-database___create_payout"]`). The model therefore cannot attempt it — it
answered that it is unable to perform financial transactions. No per-call
authorization happens, so no `AuthorizeAction` DENY span exists.

This also explains the metric channel's shape found in the sibling task:
`launchpad-gw` publishes `DenyDecisions` only under `PartiallyAuthorizeActions`
(per tool), never under `AuthorizeAction`.

Capturing an `AuthorizeAction` DENY — and with it, whether
`authorization_reason` is populated — requires the Gateway in **`LOG_ONLY`** mode,
where the tool is not filtered and the call proceeds while being logged. That is a
mode switch on a shared demo resource and is **not** done here; it needs explicit
confirmation. Until then, `AuthorizeAction`-DENY shape and `authorization_reason`
stay **unverified** and no parser branch may assume them.

## Consequences for R3

1. Prefer `AgentCore.Gateway.InvokeTool` (SERVER) as the row source: action +
   decision + mode + gateway in one span.
2. Enrich from the sibling `AgentCore.Policy.*` span under the same
   `parentSpanId` for `determining_policies`, `mismatched_policies`,
   `log_only_matched_policies`, and the engine ARN.
3. Join on `traceId` for `session_id`.
4. `principal` renders absent. `authorization_reason` must not be referenced until
   captured.
5. Metric counts stay authoritative for `evidence_count`: spans are sampled, and
   this capture confirms only that spans exist, not that they are complete.
