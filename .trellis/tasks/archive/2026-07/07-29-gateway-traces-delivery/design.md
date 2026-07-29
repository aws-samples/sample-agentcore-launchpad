# Design — gateway TRACES delivery in bootstrap

## Boundaries

| File | Change |
|---|---|
| `backend/app/services/policy_bootstrap.py` | new `ensure_gateway_traces()`; called from `run_policy_bootstrap()` |
| `backend/app/services/bootstrap.py` | pass `_client("logs", region)` into `run_policy_bootstrap()` |
| `backend/tests/test_governance.py` | six idempotency/failure cases (this file already covers `policy_bootstrap`) |
| `docs/setup.md`, `docs/architecture.md` | bootstrap step + IAM actions + span channel |
| `.trellis/spec/launchpad/gateway-policy-management.md` | span channel is opt-in per gateway; bootstrap owns it |

`policy_bootstrap.py` is the right home over `gateway_bootstrap.py`: the step is
governed by Transaction Search (already this module's concern) and is only
meaningful because a Policy engine is attached. `gateway_bootstrap.py` runs before
the engine exists.

## Signature

```python
def ensure_gateway_traces(
    logs: Any,
    gateway_arn: str,
    gateway_id: str,
    *,
    transaction_search_enabled: bool,
) -> dict[str, Any]:
```

Returns one of:

```python
{"status": "created",   "changed": True,  "source": name, "destination": name, "delivery_id": id}
{"status": "present",   "changed": False, ...}
{"status": "skipped",   "changed": False, "reason": "transaction_search_disabled"}
{"status": "failed",    "changed": False, "reason": "<AWS error code>"}
```

`status` rather than a bare bool because R4 requires distinguishing "already open"
from "we chose not to try" from "we tried and it broke" — a bool collapses the last
two into a lie.

Call site in `run_policy_bootstrap()`, after `tx` is computed:

```python
tx = ensure_transaction_search(xray)
...
traces = ensure_gateway_traces(
    logs, resources["gateway_arn"], resources["gateway_id"],
    transaction_search_enabled=bool(tx.get("enabled")),
)
```

and added to the returned summary as `"gateway_traces": traces`.

`run_policy_bootstrap` gains a `logs` parameter. It has exactly one caller
(`bootstrap.py:269`), so this is not a breaking change in practice; add it as a
keyword-only param so the positional signature stays stable.

## Idempotency

Three resources, three checks. Names are deterministic from `gateway_id`
(`SOURCE_SUFFIX = "-traces-source"`, `DEST_SUFFIX = "-traces-destination"`), so no
listing is needed to find "our" resources.

```python
def _exists(fn, **kwargs) -> bool:
    try:
        fn(**kwargs)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise
```

- source: `get_delivery_source(name=...)`
- destination: `get_delivery_destination(name=...)` → also yields its `arn`, which
  `create_delivery` needs. When absent, `put_delivery_destination` returns it.
- delivery: no per-name lookup exists, so paginate `describe_deliveries` and match
  on `deliverySourceName == source_name` (the destination arn is implied by our
  own naming, but match on it too when present).

`put_delivery_source` / `put_delivery_destination` are upserts, but they are
skipped when the resource already exists rather than re-put: re-putting with a
changed immutable field is exactly what raises `ConflictException`, and there is
nothing to update in the steady state.

`ConflictException` from any of the three is mapped to `present` — it means a
concurrent or prior run got there first, which is the desired end state, not a
failure. This matters because `make bootstrap` is explicitly resumable/re-runnable.

## Destination type

`deliveryDestinationType="XRAY"` with **no** `deliveryDestinationConfiguration`
— confirmed against the account, where every existing XRAY destination has an
empty `destinationResourceArn`. Spans land in the shared `aws/spans` log group;
there is no per-gateway span log group to configure.

## Failure posture

The whole body is wrapped so that `ClientError` / `BotoCoreError` becomes
`{"status": "failed", "reason": <code>}`. Rationale in the PRD (R4): bootstrap
must not abort over telemetry, but must not claim success either. `AccessDenied`
is the realistic case on a least-privilege operator role, and the reason string is
what tells the operator which IAM action to add.

Deliberately **not** caught: `KeyError` on `resources["gateway_arn"]`. If the
gateway is missing from config, bootstrap is already broken and should fail loudly
— the existing `run_policy_bootstrap` lines above do the same.

## Testing

Stub `logs` client as a small class recording calls, mirroring the `MagicMock`
style already in `test_governance.py`. The six R6 cases, plus assert that no test
path calls anything on a `control` client — this step must not touch the Gateway
resource (guards the "no `UpdateGateway`" requirement structurally, not just by
review).

## Tradeoffs

- **One destination per gateway** rather than a shared XRAY destination. Follows
  the account's observed convention and keeps deletion per-resource. A shared
  destination would be marginally tidier but diverges from what is already there.
- **No wait/verification loop.** Delivery creation is effectively immediate and
  there is nothing to poll that would prove spans flow — that needs traffic. Adding
  a sleep would create false confidence.

## Rollout / rollback

Additive and reversible. To undo:

```bash
aws logs delete-delivery --id <delivery-id> --region us-west-2
aws logs delete-delivery-source --name launchpad-gw-em0yuqmmdp-traces-source --region us-west-2
aws logs delete-delivery-destination --name launchpad-gw-em0yuqmmdp-traces-destination --region us-west-2
```

Note the shared account: the remote prod EC2 deployment drives the same AWS
account, so it will observe the delivery too. That is the intended outcome (both
consoles then see Policy spans), and it costs only `aws/spans` ingestion for
gateway traffic that already happens.
