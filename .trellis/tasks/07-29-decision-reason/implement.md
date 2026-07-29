# Implement — surface the denial reason

## Step 0 — baseline

```bash
cd backend && uv run pytest -q | tail -2   # expect 922 passed
```

## Step 1 — parse the reason

`backend/app/services/governance_spans.py`:

- `_row()` gains
  `"reason": pattrs.get("aws.agentcore.policy.authorization_reason")`.
- Comment the conditionality: reason is DENY-only,
  `log_only_matched_policies` is ALLOW-only, so **neither is required** and a `None`
  is a correct value, not a parse failure.
- Update the module docstring: the paragraph explaining why
  `authorization_reason` is deliberately absent is now wrong and must be replaced with
  the verified behavior. Keep the general rule — no attribute enters this module
  without appearing in a captured span.

## Step 2 — replace the guard test

`backend/tests/test_governance_spans.py`:

- Add `aws.agentcore.policy.authorization_reason` to a DENY fixture built from the
  captured span (`Policy evaluation denied due to
  launchpad_payout_admin_only-x7gz5yjkrd`). The existing `AUTHORIZE_SPAN` fixture is an
  ALLOW and must **not** gain a reason — its absence there is the point.
- Replace `test_parser_never_references_the_unverified_reason_attribute` with a
  positive test: reason parsed on DENY, `None` on ALLOW, `None` on `tool_listing`.
- Keep an AST-based check that no attribute key outside a known allow-list is read, if
  it can be written cleanly; otherwise say so in the module docstring and drop it.

```bash
cd backend && uv run ruff check . && uv run pytest tests/test_governance_spans.py -q
```

**Review gate:** make the new positive test fail on purpose — remove the `.get()` —
and confirm it catches it. A test that passes either way is not coverage.

## Step 3 — contract + view

- `api.ts`: `reason: string | null` on `GovernancePolicyDecision`.
- `DecisionView.tsx`: render it as a note under the outcome cell (present on denials
  only; a dedicated column would be empty for most rows).
- i18n `en` + `zh-CN`.

```bash
cd frontend && npm run lint && npx tsc --noEmit
python3 scripts/i18n_check.py
```

## Step 4 — verify + local visual check

```bash
make verify
```

Then against real AWS (the 7d window still covers the captured DENY):

```bash
cd backend && nohup uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 >/tmp/be.log 2>&1 &
curl -s "localhost:8000/api/governance/gateways/launchpad-gw-em0yuqmmdp/decisions?range=7d" \
  | python3 -c "import json,sys; [print(r['outcome'], r['action'], '|', r['reason']) for r in json.load(sys.stdin)['decisions']]"
```

Expect a real reason string on the `AuthorizeAction` DENY rows and `None` elsewhere.
Then render in the browser (`en` + `zh-CN`) via playwright-cli, 0 console errors.

> `pkill -f uvicorn` also matches the shell running it — kill by pid from `pgrep`.

## Step 5 — docs, spec, commit, push

- Spec: drop the "not yet surfaced" clause.
- `docs/lab/11-governance.md`: add the reason to the row example.

```bash
make verify && git push origin main
```

## Step 6 — deploy to the remote prod EC2

Per the recorded procedure — **back up the ledger first**:

```bash
ssh -i ~/workspace/4344-us-east-1.pem ubuntu@54.221.233.74
cd /home/ubuntu/workspace/agentcore_launchpad
cp data/launchpad.db data/launchpad.db.bak-$(date +%Y%m%d-%H%M)
git merge --ff-only origin/main          # fetch first
cd frontend && npm run build             # systemd serves the built dist
sudo systemctl restart launchpad-backend launchpad-frontend
```

Confirm both services are active before browsing.

## Step 7 — verify the live site

`https://dh5fx2s7uotew.cloudfront.net`, auth required in prod mode (admin credentials
in the memory note). Via playwright-cli:

- log in, open Governance → Decisions for the us-east-1 gateway
- confirm the page renders, aggregates behave, console is clean

**Expect `decisions: []` and therefore no reason values** — no us-east-1 gateway has a
TRACES delivery, so prod has no Policy spans. Verify the honest empty/aggregate state,
not the reason itself, and report that limit rather than implying the field was
verified there.

## Do not

- Enable TRACES on the prod gateway without asking — it mutates a production resource.
- Synthesize a reason for `tool_listing` rows.
- Delete the AST guard without replacing its coverage.
- Force-push, or push anything other than `main`.
