# Implement — per-decision rows from Policy spans (R3)

Read `prd.md`, then `research/policy-span-corpus.md`, then `design.md`. The research
note is not optional background: it is the only authority for which attribute names
may appear in the code.

## Step 0 — baseline

```bash
cd backend && uv run pytest -q | tail -2
```

Expect 898 passed.

## Step 1 — `governance_spans.py`

Create `backend/app/services/governance_spans.py`:

- `RANGE_HOURS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}`
- `POLICY_SPAN_NAMES` / `INVOKE_SPAN_NAME` constants matching the captured names
  exactly (`AgentCore.Gateway.InvokeTool`, `AgentCore.Policy.AuthorizeAction`,
  `AgentCore.Policy.PartiallyAuthorizeActions`).
- `gateway_decision_rows(logs, gateway_arn, range_key, policy_id=None) -> dict`
  returning `{"decisions": [...], "unavailable_reason": None|str, "truncated": bool}`.
- Pass 1 / conditional pass 2 exactly as `design.md` specifies; select `@message`
  and `json.loads` each row.
- Row assembly: index Policy spans by `parentSpanId`; invocation rows from
  `InvokeTool`; `tool_listing` rows from `denied_tools` only.
- `principal=None` and `policy_mode=None`, each with a one-line comment saying why.
- **Do not write the string `authorization_reason` anywhere.**

```bash
cd backend && uv run ruff check app/services/governance_spans.py
```

## Step 2 — tests first, from the verbatim corpus

Create `backend/tests/test_governance_spans.py` with the eight cases in
`design.md` §Testing. Build the fixtures by **copying the JSON out of
`research/policy-span-corpus.md`** — do not hand-simplify it; the real attribute set
(including the two undocumented keys) is what the parser must survive.

```bash
cd backend && uv run pytest tests/test_governance_spans.py -q
```

**Review gate — two of these tests are load-bearing, check they can fail:**

1. Temporarily expand `allowed_tools` into rows and confirm case 2 fails. If it
   passes either way it is not pinning the documented behavior.
2. Temporarily add `attrs.get("aws.agentcore.policy.authorization_reason")` to the
   row and confirm case 5 fails. That test is the mechanical guard on the research
   gate — if it does not fire, delete and rewrite it.

## Step 3 — merge into the evidence response

In `governance_evidence.gateway_decisions()`, after the metric aggregation, call
`governance_spans.gateway_decision_rows(...)` and set `decisions`, `count`,
`source`, `spans_unavailable_reason` per `design.md`.

- Catch `AppError` from the span path and degrade to metrics-only. This is the one
  place that swallow is correct; say so in a comment.
- `evidence_count` must stay metric-derived — do not recompute it from rows.
- The gateway ARN is needed; `_require_gateway()` already returns the gateway, so
  take the ARN from there rather than re-fetching.

Add two tests to `tests/test_governance_evidence.py`:

- spans present → `source == "metrics+spans"`, `count == len(decisions)`, and
  `evidence_count` unchanged from the metrics-only value
- span query raises → aggregates intact, `decisions == []`,
  `spans_unavailable_reason` set, HTTP 200

```bash
cd backend && uv run ruff check . && uv run pytest -q
```

## Step 4 — frontend

- `api.ts`: extend `GovernancePolicyDecision` (`span_id`, `evaluation`,
  `determining_policies`, `mismatched_policies`, `log_only_matched_policies`;
  `principal`/`policy_mode` nullable) and add `spans_unavailable_reason` to the
  response.
- `DecisionView.tsx`: the table already exists — add the `evaluation` chip, render
  `principal` as absent **with an explanation** (M2M auth has no human principal),
  and surface `log_only_matched_policies`. Keep the four-branch state logic from the
  sibling task intact.
- New i18n keys in `en` **and** `zh-CN` together, including a note that listing rows
  cover denials only.

```bash
cd frontend && npm run lint && npx tsc --noEmit
python3 scripts/i18n_check.py
```

## Step 5 — verify gate

```bash
make verify
```

## Step 6 — real AWS check

The captured trace is from 2026-07-29 ~13:29 UTC and `aws/spans` retention is 30
days, so a `7d` window still covers it for a while. If it has aged out, regenerate
traffic the way R2 did — that path is known to work:

```bash
make backend   # separate shell
curl -sN -X POST localhost:8000/api/chat/90d6f49922c14769bf7972864dbee090 \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"List all departments in the company.","actor_id":"span-verify"}' >/dev/null
```

Then:

```bash
curl -s "localhost:8000/api/governance/gateways/launchpad-gw-em0yuqmmdp/decisions?range=7d" \
  | python3 -m json.tool | head -60
```

Expect `source: "metrics+spans"`, at least one `invocation` ALLOW row for
`hr-database___list_departments`, one `tool_listing` DENY row for
`hr-database___create_payout`, `principal: null` on every row, and a non-empty
`log_only_matched_policies` on the invocation row.

Cross-check one row against the raw span so the parser is not inventing values:

```bash
aws logs start-query --region us-west-2 --log-group-names aws/spans \
  --start-time $(( $(date +%s) - 7*86400 )) --end-time $(date +%s) \
  --query-string 'fields @message | filter name = "AgentCore.Gateway.InvokeTool" | sort @timestamp desc | limit 1'
```

Then render the view in a browser (playwright-cli, `en` + `zh-CN`) and confirm the
rows read correctly, `principal` shows as explained-absent rather than a bare dash,
and the console has no errors.

## Step 7 — docs + spec

- `docs/architecture.md` — the span channel now backs per-decision rows; state the
  `principal` limitation and that `evidence_count` stays metric-derived.
- `docs/lab/11-governance.md` §11.6 — real rows; keep the local-ledger comparison
  and explain why the AWS rows have no principal while the ledger rows do.
- `.trellis/spec/launchpad/gateway-policy-management.md` — span-derived rows,
  the two evaluation kinds, `principal` structurally absent, and
  `authorization_reason` recorded as unverified so nobody adds it later.

## Rollback points

- After step 3: reverting `governance_evidence.py` restores metrics-only.
- Whole task: plain revert. No AWS mutation, no migration; `decisions[]` returns to
  empty, a state the frontend already handles.

## Do not

- Reference `aws.agentcore.policy.authorization_reason`, or any attribute absent
  from the research note.
- Fabricate `principal` — not from the ledger, not from `session.id`, not from the
  OAuth provider name.
- Recompute `evidence_count` from spans.
- Let a span-query failure turn the endpoint into a 5xx.
- Expand `allowed_tools` into rows.
- Switch the Gateway to `LOG_ONLY` — the user declined that on 2026-07-29.
