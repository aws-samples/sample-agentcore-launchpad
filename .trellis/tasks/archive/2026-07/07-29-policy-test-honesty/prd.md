# Honest policy-test outcomes and working demo credentials

Two defects found while building the policy-evidence tree
(`.trellis/tasks/archive/2026-07/07-29-policy-span-evidence/`), both recorded there
as out-of-scope follow-ups.

## Defect 1 — the decision ledger lies during an outage

`app/routers/governance.py:360` catches **every** `AppError` from
`mcp_client.tools_call()` and records `outcome = "DENY"` in the `policy_decisions`
ledger:

```python
except AppError as exc:
    outcome = "DENY"
    reason = str(exc.detail or exc.message)[:300]
```

`mcp_client` raises seven distinct codes. Only some are authorization results:

| Code | What it means |
|---|---|
| `gateway.rpc_error` | JSON-RPC error — a Cedar denial **or** a tool/argument failure |
| `gateway.unauthorized` | gateway returned 401/403 |
| `gateway.credentials_rejected` | Cognito refused the demo password |
| `gateway.identity_unavailable` | Cognito unreachable |
| `gateway.no_credentials` | password missing from config |
| `gateway.not_bootstrapped` | `gateway_url` missing |
| `gateway.bad_response` | malformed SSE/JSON |

The last five are infrastructure failures, yet each writes a permanent row that
reads as a Cedar denial. This surface is **audit-facing** — `docs/lab/11-governance.md`
presents ledger rows as evidence of real Cedar enforcement — so a Cognito outage
manufactures false audit evidence. This was observed live: two such rows had to be
deleted from `data/launchpad.db` on 2026-07-29.

## Defect 2 — the us-west-2 demo credentials are rejected

`initiate_auth` with the passwords in `config/launchpad.yaml` fails
`NotAuthorizedException: Incorrect username or password` for **both** `river` and
`demo`. Diagnosed 2026-07-29: not a config or flow problem — users `river`/`demo` are
`CONFIRMED` and enabled, the `launchpad-console` client has
`ALLOW_USER_PASSWORD_AUTH` and no client secret. The stored passwords simply no
longer match the pool.

Consequence: `POST /api/governance/policy-test` and
`backend/scripts/e2e_policy.py` cannot run at all, and the lab chapter's documented
DENY reproduction is broken.

**Blast radius (verified, and it corrects an earlier wrong assumption):** the remote
prod deployment is **entirely us-east-1** — its own Cognito pool
(`us-east-1_gEcEJlGep`), gateway, policy engine and memory. It shares only the AWS
account. Resetting the us-west-2 pool's demo passwords therefore affects nothing but
this box. (The memory index line claiming the two deployments share us-west-2 was
wrong and has been corrected.)

## Confirmed decisions

- Fix the credentials by **resetting Cognito to the passwords already in this box's
  `config/launchpad.yaml`** (`admin_set_user_password`), not by generating new ones.
  Smallest change, no config rewrite, nothing to redistribute.
- Both defects in one task.

## Requirements

### R1 — Classify the failure before recording anything

- Only genuine authorization results reach the ledger. Infrastructure failures must
  not create `policy_decisions` rows at all.
- The response gains a third outcome so callers can tell "could not evaluate" from
  "denied". `ALLOW` / `DENY` keep their exact current meaning.
- The classifier's policy-denial signal must be derived from a **real captured
  denial response**, not from a guess about message text. Capture it first (R2 makes
  that possible), exactly as the span work did.

### R2 — Restore the credentials

- `admin_set_user_password` for `river` and `demo` in `us-west-2_li8JBQUvY`, with
  `Permanent=True`, using the existing config values.
- Verify with `initiate_auth` for both users before touching any code that depends
  on it.
- Do **not** rewrite `config/launchpad.yaml`, and do not run full `make bootstrap`
  (which would generate new passwords and rewrite it).

### R3 — Keep the existing contract usable

- `backend/scripts/e2e_policy.py` expects `ALLOW`/`DENY` strings; update it to
  handle and report the new outcome rather than silently counting it as a mismatch.
- The existing `test_policy_test_records_decision` uses `gateway.rpc_error` for its
  DENY case and must keep passing — that code stays a denial when the message carries
  the policy signature.

### R4 — Tests

- Each of the five infrastructure codes → non-decision outcome, **zero** ledger rows.
- A captured real Cedar denial → `DENY` + one ledger row.
- `gateway.unauthorized` (401/403) → `DENY`.
- A non-policy `gateway.rpc_error` (e.g. bad arguments) → non-decision, no row.
- Success → `ALLOW` + one row.

### R5 — Documentation

- `docs/lab/11-governance.md`: the ledger section should say what the third outcome
  means, and the reproduction hint (already corrected once this session) should note
  that the direct `policy-test` path is *not* pre-filtered by `tools/list`.
- `.trellis/spec/launchpad/gateway-policy-management.md`: record that only
  authorization results are journaled, and why.

## Out of scope

- Changing `mcp_client`'s error codes or its auth flow.
- Rotating passwords / touching `config/launchpad.yaml`.
- The us-east-1 deployment.
- Backfilling or reclassifying historical ledger rows. The two known-false rows were
  already deleted; older rows predate this investigation and are left as-is.

## Acceptance criteria

- [x] `initiate_auth` succeeds for both `river` and `demo` against the us-west-2 pool.
- [x] Against live AWS: `ALLOW` for `get_employee` as `demo`, `DENY` for
      `create_payout` as `demo`, `ALLOW` for `create_payout` as `river`.
- [x] With the credential deliberately broken (in memory; the config file untouched)
      the endpoint returns `outcome: ERROR`, `recorded: false`, and the ledger stayed
      at 13 rows. This is exactly the case that used to write a false Cedar denial.
- [x] `backend/scripts/e2e_policy.py` green against live AWS; ledger grew by exactly
      3 rows for 3 decisions (10 → 13).
- [x] `test_policy_test_records_decision` passes **unchanged** (empty diff on that
      file's original test).
- [x] Six new tests, plus a verified review gate: restoring the blanket `DENY` makes
      6 of them fail.
- [x] `make verify` passes (922 backend tests).

## Opportunity worth recording (not scope)

`e2e_policy.py` case 3 (`river` calling `create_payout` directly) goes through
`tools/call` **without** a preceding `tools/list`, so the gateway must run
`AuthorizeAction` per call. That is precisely the path that can produce an
`AuthorizeAction` **DENY** span — the shape the archived span task could not capture,
because a Harness always lists first and never sees a filtered tool. Once credentials
work, capturing it (and settling whether
`aws.agentcore.policy.authorization_reason` is populated on a DENY) becomes possible
**without** the `LOG_ONLY` mode switch the user declined. Record the finding here; act
on it in a follow-up if wanted.


## Deviations and discoveries

`design.md` was written before a real denial had been captured, and the capture
(`research/policy-denial-response.md`) corrected two of its assumptions:

1. **There is a structured signal, not just message text.** The denial carries
   JSON-RPC code **`-32002`**. The classifier keys on that first and keeps the message
   signature as a second, independent signal, so neither a reworded message nor a
   changed code silently breaks detection. This was invisible from the historical
   ledger row, which stored only a truncated reason string.
2. **`gateway.rpc_error` does not wrap tool/argument failures.** `design.md` assumed
   it did. A tool that fails its own validation returns a *successful* MCP result with
   `isError: true`, so the two are cleanly separated at the transport level. That case
   stays `ALLOW` — the authorization question was answered with a permit — and a test
   pins it so nobody "fixes" it into a failure.
3. **The matcher was widened, not the test.** `test_policy_test_records_decision`
   failed at first because its synthetic message (`"Tool Execution Denied"`) lacks the
   word "policy". Per `implement.md`'s instruction, the matcher was widened using a
   substring that is genuinely present in the captured message rather than editing the
   test to fit the code.

Side effects worth stating: the `river` ALLOW path **really creates a demo payout
record** (`PAY-1785337174`, 1 USD) — that is what `e2e_policy.py` case 3 has always
done. The ledger grew from 6 to 13 rows during verification; all 7 are genuine
decisions from real gateway calls.

## Follow-up now unblocked

The `AuthorizeAction`-DENY span shape and whether
`aws.agentcore.policy.authorization_reason` is populated on a DENY — recorded as
unverifiable by `07-29-policy-span-detail` — are now reachable: `policy-test` issues
`tools/call` with no preceding `tools/list`, so the DENY above is a call-time
authorization refusal that should have produced exactly that span. No `LOG_ONLY` mode
switch needed.