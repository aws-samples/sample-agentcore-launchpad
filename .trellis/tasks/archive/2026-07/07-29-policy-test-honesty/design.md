# Design — honest policy-test outcomes

## Boundaries

| File | Change |
|---|---|
| `backend/app/routers/governance.py` | classify the failure; journal only authorization results |
| `backend/tests/test_governance.py` | the five R4 cases (this file already owns `policy_test`) |
| `backend/scripts/e2e_policy.py` | handle the third outcome |
| `docs/lab/11-governance.md`, spec | behavior update |

No new module: this is one `except` block's worth of logic plus a constant table, and
it belongs next to the endpoint that owns the ledger write. `mcp_client` is untouched —
its codes are the input to the classifier, not something to restructure.

## Outcome vocabulary

```python
ALLOW = "ALLOW"    # the call succeeded — the policy permitted it
DENY  = "DENY"     # the gateway refused it on authorization grounds
ERROR = "ERROR"    # no authorization decision was reached
```

`ERROR` rather than reusing `DENY` with a flag: the ledger's `outcome` column is read
directly by `GET /api/governance/decisions` and rendered as evidence, so the
distinction has to survive at the value level, not in a sibling field a reader might
ignore.

Ledger rule: **`ALLOW` and `DENY` are journaled; `ERROR` is not.** An error is not a
decision, and the ledger is the decision log. The failure detail still reaches the
caller in the response.

## Classification

```python
# Codes that represent the gateway refusing authorization.
_DENY_CODES = {"gateway.unauthorized"}          # 401/403 from the gateway itself
# Codes that mean we never got an authorization answer.
_ERROR_CODES = {
    "gateway.credentials_rejected", "gateway.identity_unavailable",
    "gateway.no_credentials", "gateway.not_bootstrapped", "gateway.bad_response",
}
```

`gateway.rpc_error` is the ambiguous one — it wraps *any* JSON-RPC error, so it covers
both a Cedar denial and an ordinary tool/argument failure. It is resolved by
inspecting the message for the policy-denial signature.

**The signature must come from a real captured denial**, which is why R2 (credentials)
lands before this classifier is written. The known historical ledger row suggests
`Tool Execution Denied: … not allowed due to policy enforcement [Policy evaluation
denied due to <policy-id>]`, but that row is second-hand — confirm against a live
response and derive the matcher from that.

Working assumption to be confirmed, not shipped blind:

```python
def _is_policy_denial(message: str) -> bool:
    lowered = message.lower()
    return "policy" in lowered and ("denied" in lowered or "not allowed" in lowered)
```

Substring matching on a preview-service message is the one fragile part of this
change, and it is fragile in a safe direction: an unrecognised denial degrades to
`ERROR` (no ledger row, visible failure) rather than being silently recorded as an
`ALLOW`. Say so in a comment so a future reader does not "tidy" it into a default.

## Handler shape

```python
try:
    result = mcp_client.tools_call(...)
    outcome, reason, excerpt = "ALLOW", None, str(result)[:300]
except AppError as exc:
    outcome, reason = _classify(exc)
    excerpt = reason

decision_id = None
if outcome in ("ALLOW", "DENY"):
    decision = PolicyDecision(...)
    db.add(decision); db.commit()
    decision_id = decision.id
return {..., "outcome": outcome, "detail": excerpt, "decision_id": decision_id,
        "recorded": decision_id is not None}
```

`decision_id` becomes nullable and a `recorded` boolean is added, so a caller can tell
whether anything was journaled without inferring it from the outcome string.

## Credential reset (R2)

One-off operation, not code:

```python
cognito.admin_set_user_password(
    UserPoolId="us-west-2_li8JBQUvY", Username=user,
    Password=<value already in config/launchpad.yaml>, Permanent=True,
)
```

Then `initiate_auth` for both users to confirm. `config/launchpad.yaml` is **not**
rewritten — the config is the source of truth here and Cognito is being brought back
into line with it, not the other way round.

Deliberately **not** `make bootstrap`: `ensure_demo_user_passwords` generates fresh
passwords and rewrites the config, which is a larger change than the defect warrants.

Scope note: verified that the remote deployment uses a separate us-east-1 Cognito
pool, so this touches only this box's demo users.

## Tradeoffs

- **Non-policy `rpc_error` becomes `ERROR`, not `ALLOW`.** A tool that crashes was
  arguably permitted by policy, so `ALLOW` is defensible — but we cannot distinguish
  "reached the tool and it failed" from "rejected in a way we did not recognise", and
  guessing `ALLOW` would fabricate positive evidence. `ERROR` is the honest reading.
- **Historical rows are left alone.** Reclassifying them would require re-deriving
  intent from truncated reason strings; the two rows known to be false were already
  deleted.

## Rollout / rollback

The code change is additive to the response and subtractive to ledger writes — revert
restores previous behavior. The credential reset is idempotent and independently
reversible by setting a password again.
