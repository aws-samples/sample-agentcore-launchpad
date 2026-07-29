# Gateway TRACES delivery owned by bootstrap

Child of `07-29-policy-span-evidence`. Split out of `07-29-policy-span-detail`
R1 on 2026-07-29 so it can ship independently: the rest of that child (span
capture and the decision parser) is blocked on rejected demo Cognito credentials,
while this deliverable is not blocked by anything.

## Goal

`make bootstrap` idempotently creates the `TRACES` delivery for the launchpad
Gateway, so AgentCore Policy decision spans start reaching the `aws/spans` log
group. This is the missing prerequisite that made the whole span channel look
unverifiable — see the parent's `research/policy-evidence-channels.md`.

## Background

Policy decision spans are only emitted once trace delivery is enabled on the
attached Gateway. That is not a Gateway field and needs no `UpdateGateway`; it is
a CloudWatch vended-log delivery — three calls:

```python
logs.put_delivery_source(name=..., logType="TRACES", resourceArn=<gateway_arn>)
logs.put_delivery_destination(name=..., deliveryDestinationType="XRAY")
logs.create_delivery(deliverySourceName=..., deliveryDestinationArn=...)
```

Verified on 2026-07-29: the account has `TRACES` deliveries for several memory
and runtime resources and for one non-launchpad gateway, but **none** for
`launchpad-gw-em0yuqmmdp`. Transaction Search is already `ACTIVE` and `aws/spans`
already receives gateway MCP spans, so only the per-gateway delivery is missing.

## Confirmed decisions (from the parent)

- Target `launchpad-gw` only. Not `launchpad-kb-gw`, not a new disposable gateway.
- Owned by `make bootstrap`, idempotently — not a one-off script, not a console
  click.
- No `UpdateGateway` call.

## Requirements

### R1 — `ensure_gateway_traces()` in the bootstrap chain

- Lives in `backend/app/services/policy_bootstrap.py`, which already owns
  `ensure_transaction_search()` and the Gateway/engine attachment. Follows the
  existing `ensure_*` contract: takes injected clients, returns a summary dict
  including whether it changed anything.
- Wired into `run_policy_bootstrap()`, with the `logs` client built by
  `bootstrap._client("logs", region)` and passed in — no new `boto3.client(...)`
  construction site.
- Reported in the bootstrap summary alongside `transaction_search`.

### R2 — Idempotent, and honest about what it did

Re-running `make bootstrap` must not error, must not duplicate, and must not
silently claim success:

- Existence is checked with `get_delivery_source` / `get_delivery_destination`
  (`ResourceNotFoundException` means absent) and, for the delivery itself, by
  paginating `describe_deliveries` and matching the (source name, destination arn)
  pair — that API has no per-name lookup.
- `ConflictException` from any of the three calls is treated as "already exists",
  not as a failure.
- The summary distinguishes created / already-present / skipped / failed.

### R3 — Respect the documented prerequisite

AWS documents Transaction Search as required before enabling tracing.
`run_policy_bootstrap()` already computes that state; if it is not enabled, skip
this step with an explicit reason in the summary rather than attempting the calls.

### R4 — Failure must not abort bootstrap, but must be visible

A telemetry delivery is not worth failing the whole bootstrap over — the platform
works without spans. So a failure here is caught, reported in the summary with the
AWS error code, and does not raise. Equally, it must never be reported as success:
the follow-up span child depends on knowing whether the channel is actually open.

### R5 — Naming follows the account's existing convention

`{gateway_id}-traces-source` and `{gateway_id}-traces-destination`, matching the
pre-existing `LingoAceMemory-KDPsbEBvMa-traces-source` /
`acmesupport-fOfv652Bjq-traces-destination` pairs already in the account. One
destination per resource, as observed.

### R6 — Tests

Hermetic, in `backend/tests/`, with a stubbed `logs` client:

- fresh account → all three calls made, summary reports created
- re-run with everything present → **no** `put_*` / `create_delivery` calls, summary
  reports already-present
- delivery missing but source/destination present → only `create_delivery` called
- `ConflictException` on `create_delivery` → treated as already-present
- Transaction Search disabled → skipped with a reason, no calls
- AWS error → summary carries the code, no exception escapes

### R7 — Documentation

- `docs/setup.md` (and its zh-CN counterpart if the passage exists there): the new
  bootstrap step and the IAM actions it needs —
  `logs:PutDeliverySource`, `logs:PutDeliveryDestination`, `logs:CreateDelivery`,
  `logs:GetDeliverySource`, `logs:GetDeliveryDestination`,
  `logs:DescribeDeliveries`.
- `docs/architecture.md`: the span channel and its prerequisite, next to the
  metric-channel paragraph the sibling child added.
- `.trellis/spec/launchpad/gateway-policy-management.md`: record that the span
  channel is opt-in per gateway and that bootstrap owns it.

## Out of scope

- Capturing spans, defining the alias map, and the decision parser — those stay in
  `07-29-policy-span-detail`.
- `launchpad-kb-gw`.
- `APPLICATION_LOGS` delivery for the gateway. Same API family, different concern,
  and nothing currently needs it.
- **Teardown cleanup.** `scripts/teardown.py` today removes only the memory,
  registry, and CDK stack — it does not delete the Gateway or the policy engine
  either, so leaving a delivery that points at a still-existing gateway is
  consistent with the current teardown scope. Adding delivery deletion without
  also handling the Gateway would be a partial, misleading cleanup. The manual
  commands go in the docs instead.

## Acceptance criteria

- [x] The `TRACES` delivery now exists for `launchpad-gw-em0yuqmmdp`, verified
      independently of the code:
      `describe-delivery-sources` → `launchpad-gw-em0yuqmmdp-traces-source`,
      `logType=TRACES`, `service=bedrock-agentcore`, `resourceArn` = that gateway;
      `describe-deliveries` → id `ePQBTbUmCYGHos19`, destination type `XRAY`.
      **Applied via the policy stage only, not full `make bootstrap`** — see
      Deviations.
- [x] Re-run reports `present` / `changed: false` with no writes (confirmed against
      real AWS, and unit-tested).
- [x] The summary carries the outcome (`gateway_traces: {status: created, …}`);
      failure paths report the AWS error code without raising.
- [x] No new `boto3.client(...)` construction site — the `logs` client comes from
      the existing `bootstrap._client()`.
- [x] No Gateway mutation: `updatedAt` is still `2026-07-09T12:48:59.675757+00:00`,
      unchanged from the pre-task baseline, and `attach_engine_to_gateway` reported
      `False` (no `update_gateway`).
- [x] Nine unit tests cover the six R6 cases plus three structural guards
      (signature has no `control` client; `_absent` re-raises non-404s; `logs` is a
      required keyword-only arg on `run_policy_bootstrap`).
- [x] `make verify` passes (898 backend tests).

## Deviations from the plan

**`implement.md` step 6 said to run `make bootstrap`; I ran only the policy stage
(`run_policy_bootstrap`) with real clients instead.** Reason found while executing:
full bootstrap calls `ensure_demo_user_passwords` → `admin_set_user_password`,
which rewrites the shared Cognito demo users' passwords and `config/launchpad.yaml`.
This AWS account is also driven by the remote prod EC2 deployment, which holds its
own copy of those passwords — rotating them is unrelated to this task and could
break that deployment. The policy stage is the exact production code path for this
change (including the `logs` client wiring) and reported no-change for every other
step (`transaction_search` ACTIVE/unchanged, engine and both policies
`created: False`, `gateway_attached: False`).

The one line not exercised end-to-end is `bootstrap.py`'s
`logs=_client("logs", region)` argument. That is safe by construction rather than by
test: `logs` is a **required keyword-only** parameter, so omitting it raises
`TypeError` on the next real bootstrap instead of silently skipping the step — and
a test now asserts that property.

## Follow-up

Nothing blocking. The sibling `07-29-policy-span-detail` R2 can now capture spans as
soon as its credential blocker is resolved — the channel is open, but no Policy span
will appear until real gateway traffic flows through it.

## Notes

Whether spans actually appear is **not** an acceptance criterion here: that needs
real gateway traffic, which is exactly what the sibling child is blocked on. This
task's deliverable is that the channel is open and provably configured. Confirming
the first real Policy span is the sibling's R2.
