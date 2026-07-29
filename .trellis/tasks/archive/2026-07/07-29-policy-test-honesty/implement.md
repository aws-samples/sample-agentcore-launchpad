# Implement — honest policy-test outcomes

Read `prd.md` then `design.md`. **Order matters:** the credential reset comes first,
because the classifier's policy-denial signature must be derived from a real captured
response rather than from the second-hand historical ledger row.

## Step 0 — baseline

```bash
cd backend && uv run pytest tests/test_governance.py -q   # expect 14 passed
```

Record the current ledger size, so a later "no new rows" claim is checkable:

```bash
python3 -c "import sqlite3;print(sqlite3.connect('data/launchpad.db').execute('select count(*) from policy_decisions').fetchone())"
```

## Step 1 — reset the demo credentials (real AWS)

Passwords come from `config/launchpad.yaml`; do not print them.

```bash
cd backend && uv run python - <<'EOF'
import boto3, yaml
c = yaml.safe_load(open("../config/launchpad.yaml"))
pw = c["demo_users"]["passwords"]
pool = c["resources"]["user_pool_id"]
cli = boto3.client("cognito-idp", region_name=c["region"])
for user in ("river", "demo"):
    cli.admin_set_user_password(UserPoolId=pool, Username=user,
                                Password=pw[user], Permanent=True)
    print("set:", user)
EOF
```

Then confirm — this is the acceptance gate for R2, not the set call:

```bash
cd backend && uv run python - <<'EOF'
import boto3, yaml
from botocore.exceptions import ClientError
c = yaml.safe_load(open("../config/launchpad.yaml"))
pw, cid = c["demo_users"]["passwords"], c["resources"]["user_pool_client_id"]
cli = boto3.client("cognito-idp", region_name=c["region"])
for user in ("river", "demo"):
    try:
        cli.initiate_auth(ClientId=cid, AuthFlow="USER_PASSWORD_AUTH",
                          AuthParameters={"USERNAME": user, "PASSWORD": pw[user]})
        print(f"  {user}: OK")
    except ClientError as e:
        print(f"  {user}: {e.response['Error']['Code']}")
EOF
```

Both must print `OK`. Do **not** run `make bootstrap` (it regenerates passwords and
rewrites the config).

## Step 2 — capture a real denial

With credentials working, drive one real denial through the endpoint and record the
verbatim response. This is what the classifier is derived from.

```bash
make backend   # separate shell
curl -s -X POST localhost:8000/api/governance/policy-test \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","tool":"hr-database___create_payout","arguments":{"employee_id":"EMP-1024","amount":1}}' \
  | python3 -m json.tool
```

Save the exact `detail` string into `research/policy-denial-response.md` alongside an
allowed-tool response for contrast. **If the message does not match the assumed
signature in `design.md`, change the matcher to fit the capture — not the reverse.**

Note: this call writes a ledger row (it is a genuine decision). That is expected;
account for it against the Step 0 count.

## Step 3 — the classifier

In `backend/app/routers/governance.py`:

- `_DENY_CODES` / `_ERROR_CODES` constants per `design.md`.
- `_is_policy_denial(message)` built from the Step 2 capture, with a comment saying
  the fragility is deliberate and fails safe toward `ERROR`.
- `_classify(exc) -> tuple[str, str]` returning `(outcome, reason)`.
- Journal only `ALLOW`/`DENY`; add `recorded` to the response and make `decision_id`
  nullable.

```bash
cd backend && uv run ruff check . && uv run pytest tests/test_governance.py -q
```

`test_policy_test_records_decision` must pass **unchanged** — its DENY case uses
`gateway.rpc_error` with the message `"Tool Execution Denied"`. If that test needs
editing to pass, the matcher is too narrow; widen it against the capture rather than
loosening the test.

## Step 4 — tests

Add the five R4 cases to `backend/tests/test_governance.py`, asserting ledger row
counts, not just response fields — the row count is the actual defect.

**Review gate:** temporarily restore the blanket `outcome = "DENY"` and confirm the
infrastructure-code cases fail. If they pass either way they are not testing the fix.

## Step 5 — e2e script

`backend/scripts/e2e_policy.py`: report `ERROR` distinctly instead of counting it as a
plain mismatch, so a future credential outage is diagnosable from its output rather
than looking like a policy regression.

## Step 6 — docs + spec

- `docs/lab/11-governance.md`: what the third outcome means, that infrastructure
  failures are no longer journaled, and that the direct `policy-test` path is not
  pre-filtered by `tools/list` (unlike the Harness path) — which is why it can be
  denied at call time.
- `.trellis/spec/launchpad/gateway-policy-management.md`: only authorization results
  are journaled, plus the classification table.

## Step 7 — verify + real AWS

```bash
make verify
cd backend && uv run python scripts/e2e_policy.py
```

`e2e_policy.py` must be green. Then confirm the negative case for real: point the
config password at a wrong value in memory only (monkeypatch or a scratch script —
**do not edit the config file**), call the endpoint, and check that the response is
`ERROR` and the ledger row count did not change.

## Rollback points

- Step 1 is independently reversible (`admin_set_user_password` again).
- Steps 3–5 are a plain revert.

## Do not

- Rewrite `config/launchpad.yaml` or run `make bootstrap`.
- Print or commit demo passwords.
- Touch the us-east-1 deployment.
- Change `mcp_client`'s codes or auth flow.
- Default an unrecognised failure to `ALLOW`.
- Reclassify or delete historical ledger rows.
