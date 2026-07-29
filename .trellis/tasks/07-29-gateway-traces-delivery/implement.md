# Implement — gateway TRACES delivery in bootstrap

Read `prd.md` then `design.md` first.

## Step 0 — baseline

```bash
cd backend && uv run pytest tests/test_governance.py -q
```

Record the count (was 5 before the sibling task; confirm current).

Also record the pre-change AWS state, so the "created" claim is provable later:

```bash
aws logs describe-delivery-sources --region us-west-2 \
  | python3 -c "import json,sys; print([s['name'] for s in json.load(sys.stdin)['deliverySources'] if 'launchpad-gw' in (s.get('resourceArns') or [''])[0]])"
```

Expect `[]`.

## Step 1 — `ensure_gateway_traces()`

In `backend/app/services/policy_bootstrap.py`, next to `ensure_transaction_search`:

- `SOURCE_SUFFIX = "-traces-source"`, `DEST_SUFFIX = "-traces-destination"` module
  constants.
- `_exists(fn, **kwargs)` helper per `design.md` — swallows only
  `ResourceNotFoundException`, re-raises everything else.
- `ensure_gateway_traces(logs, gateway_arn, gateway_id, *, transaction_search_enabled)`
  returning the `status` dict from `design.md`.
- Skip first: `transaction_search_enabled` false → `skipped` /
  `transaction_search_disabled`, **before** any AWS call.
- Wrap the body so `ClientError` / `BotoCoreError` → `failed` + AWS error code.
  Do not catch `KeyError`.

```bash
cd backend && uv run ruff check app/services/policy_bootstrap.py
```

## Step 2 — tests before wiring

Add the six R6 cases to `backend/tests/test_governance.py`. Write a small stub
that records calls rather than using bare `MagicMock`, so "no `put_*` on re-run" is
assertable.

Include the structural guard: assert the function never receives or uses a
`control` client, so a future edit cannot quietly add an `UpdateGateway` call.

```bash
cd backend && uv run pytest tests/test_governance.py -q
```

**Review gate:** make the re-run case fail on purpose — temporarily drop the
`_exists` check for the source — and confirm the test catches the extra
`put_delivery_source`. If it passes either way, the test is not testing
idempotency.

## Step 3 — wire into the bootstrap chain

- `run_policy_bootstrap(control, xray, config, *, logs)` — keyword-only, per
  `design.md`.
- Call after `tx`, pass `transaction_search_enabled=bool(tx.get("enabled"))`.
- Add `"gateway_traces": traces` to the returned summary.
- `backend/app/services/bootstrap.py:269` — pass `logs=_client("logs", region)`.

```bash
cd backend && uv run ruff check . && uv run pytest -q
```

## Step 4 — docs + spec

- `docs/setup.md` — the new bootstrap step and the six IAM actions from PRD R7.
  Check whether `docs/setup.zh-CN.md` carries the same passage; the zh
  `architecture` file is a shorter older cut and may not.
- `docs/architecture.md` — the span channel and its per-gateway prerequisite, next
  to the metric-channel paragraph added by commit `9bb3495`.
- `.trellis/spec/launchpad/gateway-policy-management.md` — span channel is opt-in
  per gateway; bootstrap owns the delivery.
- Include the manual rollback commands from `design.md` in the docs, since
  teardown deliberately does not remove the delivery.

```bash
python3 scripts/i18n_check.py   # only if any locale file was touched
```

## Step 5 — verify gate

```bash
make verify
```

## Step 6 — real AWS: run bootstrap

`make bootstrap` is idempotent across all its stages, so running it is the
intended way to apply this. It will also re-assert the memory/registry/gateway/
policy stages — expect "already present" from those.

```bash
make bootstrap 2>&1 | tail -40
```

Confirm the summary reports `gateway_traces: created`, then prove it in AWS:

```bash
aws logs describe-delivery-sources --region us-west-2 \
  | python3 -c "
import json,sys
for s in json.load(sys.stdin)['deliverySources']:
    if 'launchpad-gw-em0yuqmmdp' in (s.get('resourceArns') or [''])[0]:
        print(s['name'], s['logType'], s['resourceArns'])
"
aws logs describe-deliveries --region us-west-2 \
  | python3 -c "
import json,sys
for d in json.load(sys.stdin)['deliveries']:
    if 'launchpad-gw' in d['deliverySourceName']: print(d['id'], d['deliverySourceName'])
"
```

Expect one `TRACES` source on the gateway ARN and one delivery.

Then **run `make bootstrap` a second time** and confirm the summary reports
`present`, not `created`. This is the acceptance criterion that matters most; do
not skip it.

Also confirm the Gateway itself was untouched:

```bash
aws bedrock-agentcore-control get-gateway --region us-west-2 \
  --gateway-identifier launchpad-gw-em0yuqmmdp \
  | python3 -c "import json,sys; g=json.load(sys.stdin); print('updatedAt:', g['updatedAt'], '| mode:', g['policyEngineConfiguration']['mode'])"
```

`updatedAt` must still be `2026-07-09T12:48:59...` — unchanged from before this
task. If it moved, something called a Gateway mutation and that is a defect.

## Rollback points

- Before step 6 nothing has changed in AWS; a plain revert suffices.
- After step 6, the three `aws logs delete-*` commands in `design.md` remove the
  delivery.

## Do not

- Call `UpdateGateway` or any Gateway mutation.
- Add a `boto3.client(...)` outside `bootstrap._client`.
- Enable traces on `launchpad-kb-gw`.
- Configure `APPLICATION_LOGS`.
- Let a delivery failure abort `make bootstrap`, or report it as success.
- Add a sleep/poll pretending to verify that spans flow — that needs traffic and
  belongs to the sibling child.
