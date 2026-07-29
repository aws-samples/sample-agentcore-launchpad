# Captured policy-test responses — 2026-07-29

Taken through `POST /api/governance/policy-test` against `launchpad-gw-em0yuqmmdp`
(`ENFORCE`) immediately after the credential reset. These are the authority for the
classifier; `design.md`'s assumed matcher was based on a second-hand ledger row and
two of its assumptions turned out wrong.

## DENY — `demo` (hr-analyst) calling `create_payout`

```json
{
  "principal": "demo@hr-analyst",
  "tool": "hr-database___create_payout",
  "outcome": "DENY",
  "detail": "{'code': -32002, 'message': 'Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to launchpad_payout_admin_only-x7gz5yjkrd]'}"
}
```

`mcp_client._rpc` raises `AppError("gateway.rpc_error", message, body["error"])`, so
`exc.detail` is the JSON-RPC error object:

```python
{"code": -32002, "message": "Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to launchpad_payout_admin_only-x7gz5yjkrd]"}
```

**Finding 1 — there is a structured signal, not just message text.** The JSON-RPC
error code is **`-32002`**. That is a far more robust discriminator than substring
matching, and it was invisible from the historical ledger row (which stored only the
truncated reason string). The classifier keys on the code first and keeps the message
check as a secondary signal.

The message also happens to satisfy the assumed `"policy" + ("denied" | "not
allowed")` heuristic, so both signals agree here. Keeping both means an
implementation-code change alone does not silently break detection.

## ALLOW — `demo` calling `get_employee`

```json
{"outcome": "ALLOW",
 "detail": "{'isError': False, 'content': [{'type': 'text', 'text': '{\"employee_id\":\"EMP-1024\",\"name\":\"Maya Chen\",…}'}]}"}
```

## ALLOW — `river` (platform-admin) calling `create_payout`

```json
{"outcome": "ALLOW",
 "detail": "{'isError': False, 'content': [{'type': 'text', 'text': '{\"payout_id\":\"PAY-1785337174\",\"employee_id\":\"EMP-1024\",\"amount\":1,\"currency\":\"USD\",\"status\":\"created\"}'}]}"}
```

Same tool, different principal, different outcome — `launchpad_payout_admin_only`
working as documented. Note this **actually created a demo payout record**
(`PAY-1785337174`, 1 USD); that is what `scripts/e2e_policy.py` case 3 does by
design.

## Tool-level failure — bad arguments

```json
{"outcome": "ALLOW",
 "detail": "{'content': [{'type': 'text', 'text': \"ValidationException - Parameter validation failed: Invalid request parameters:\\n- Missing required field(s): 'employee_id'\"}], 'isError': True}"}
```

**Finding 2 — a tool/argument failure does not raise `gateway.rpc_error` at all.** It
returns a *successful* MCP result carrying `isError: True` in the content. So
`design.md`'s premise that `rpc_error` "wraps a Cedar denial **or** an ordinary
tool/argument failure" is wrong for this gateway: the two are cleanly separated at the
transport level.

Consequence: `ALLOW` is the correct outcome here — the authorization question was
answered with a permit and the call reached the tool. The ledger records the
*authorization* result, not whether the tool succeeded. No change needed, but worth
stating so nobody later "fixes" it into a failure.

Remaining unknown: which other `code` values `-3200x` the gateway may use for other
refusal kinds. Unrecognised `rpc_error` codes therefore classify as `ERROR`
(no ledger row, visible failure) rather than being assumed to be denials.

## Reproduction

```bash
curl -s -X POST localhost:8000/api/governance/policy-test \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","tool":"hr-database___create_payout","arguments":{"employee_id":"EMP-1024","amount":1}}'
```

## Side note for the archived span task

This direct `tools/call` path is **not** preceded by `tools/list`, so the gateway must
run `AuthorizeAction` per call — unlike the Harness path, where `ENFORCE` filters the
denied tool out of the listing and the model never attempts it. The DENY above is
therefore a call-time authorization refusal, which means the `AuthorizeAction`-DENY
span shape that `07-29-policy-span-detail` recorded as unverifiable **is** reachable
from here, without the `LOG_ONLY` mode switch that was declined.
