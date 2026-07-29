# Implement — policy decision evidence from CloudWatch metrics

Ordered checklist. Read `prd.md` then `design.md` first. Each step ends in a
runnable state; do not batch steps 3–7 into one edit.

## Step 0 — baseline

```bash
cd backend && uv run pytest tests/test_governance.py -q
```

Record the pass count. Everything below must keep it green.

## Step 1 — expose the CloudWatch client factory

- `backend/app/services/observability.py`: rename `_cw_client()` → `cw_client()`;
  update its internal call sites in the same edit.
- No other client construction. Do not touch `services/agentcore/client.py`.

```bash
cd backend && grep -rn "_cw_client" app/ ; uv run ruff check . && uv run pytest -q
```

The grep must return nothing.

## Step 2 — new service module

Create `backend/app/services/governance_evidence.py` per `design.md`:

- `WINDOW_SECONDS` map, `_dim_map`, `_select` (exact dimension-set match).
- `gateway_decisions(control, cw, gateway_id, range_, policy_id=None, force=False) -> dict`
  — calls `_require_gateway(control, gateway_id)` first, exactly as
  `unavailable_policy_decisions()` did, so a bad gateway id still 404s the same way.
- Namespace is `AWS/Bedrock-AgentCore` — **not** `bedrock-agentcore`.
- `ClientError` / `BotoCoreError` → `available: False` + AWS error code as
  `unavailable_reason`.
- Reuse the `observability.py` cache envelope; key
  `("gov-decisions", gateway_id, range_, policy_id or "")`.

```bash
cd backend && uv run ruff check app/services/governance_evidence.py
```

## Step 3 — tests for the service (before wiring the router)

Add to `backend/tests/test_governance.py` cases 1–5 from `design.md`
(no-double-count, zero≠unavailable, unreadable, policy filter, force).

Build the stub from the **real** projections recorded in the parent task's
`research/policy-evidence-channels.md` — do not invent a tidy dimension set; the
sparseness is the thing under test.

```bash
cd backend && uv run pytest tests/test_governance.py -q
```

**Review gate:** case 1 must fail if `_select` is changed to a subset match.
Verify by temporarily loosening it — the test is worthless otherwise.

## Step 4 — router: decisions route

`backend/app/routers/governance.py:273-285`:

- Drop `del policy_id, force`.
- Call `governance_evidence.gateway_decisions(control_client(), cw_client(), gateway_id, range, policy_id, force)`.
- Delete `unavailable_policy_decisions()` from `backend/app/services/governance.py`
  and the now-unused import.

```bash
cd backend && grep -rn "policy_span_shape_not_verified\|unavailable_policy_decisions" app/ && echo "STILL PRESENT — fix" || echo "clean"
cd backend && uv run pytest -q
```

## Step 5 — evidence gate wiring

- Add `gateway_evidence_count(control, cw, gateway_id, window="24h") -> int` to
  `backend/app/services/governance.py`. Never raises; returns 0 on failure and
  logs. Counts **LOG_ONLY mode only** (see `design.md`).
- Replace `evidence_count=0` at `routers/governance.py:183`, `:207`, `:226`.
- Do not modify `_assert_evidence_or_override()`.

Add tests 6–8 (gate admits with evidence, still refuses without, ENFORCE-only
does not satisfy).

```bash
cd backend && uv run ruff check . && uv run pytest -q
```

**Review gate:** confirm test 7 still produces `governance.evidence_required`
with 409. If the override path broke, stop — that is a governance regression, not
a test problem.

## Step 6 — frontend contract + view

- `frontend/src/lib/api.ts`: extend `GovernanceDecisionResponse` to match the
  backend exactly.
- `frontend/src/pages/governance/DecisionView.tsx`: the four-branch order from
  `design.md`; aggregate panels via existing `Panel`/`DataTable`/`Chip`.
- `frontend/src/locales/en/common.json` **and** `zh-CN/common.json`: add the new
  keys in the same commit.

```bash
cd frontend && npm run lint && npx tsc --noEmit
python3 scripts/i18n_check.py
```

## Step 7 — docs

- `docs/architecture.md` — policy evidence now comes from CloudWatch metrics;
  state the aggregate-only limitation and that spans are the follow-up child.
- `docs/lab/11-governance.md` §11.6 — replace the stub JSON with the real
  aggregate response; keep the local-ledger DENY comparison. Update the FAQ row
  `决策一直是 0 条` to describe the quiet-window case, not a known-broken state.
- Note the `cloudwatch:ListMetrics` + `GetMetricData` permission requirement.

Both docs are bilingual — check whether a zh counterpart file needs the same edit.

Spec clause `.trellis/spec/launchpad/gateway-policy-management.md:106` is a Phase 3
item (step 3.3), not a step-7 edit — but do not finish the task without it, and
keep its "never infer rollout evidence from demo rows" sentence intact.

## Step 8 — full gate

```bash
make verify
```

## Step 9 — real-AWS spot check (manual, outside verify)

```bash
make backend   # separate shell
curl -s "localhost:8000/api/governance/gateways/launchpad-gw-em0yuqmmdp/decisions?range=7d" | python3 -m json.tool
```

The gateway id comes from `config/launchpad.yaml` (`resources.gateway_id`).
Expect `available: true` and a non-zero `evidence_count` for `range=7d` if the
window still covers the 07-2x datapoints; `range=24h` may legitimately be 0.

Cross-check the number against the CLI, which must agree:

```bash
aws cloudwatch get-metric-data --region us-west-2 \
  --start-time "$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --metric-data-queries file://mq.json
```

If the API number is a multiple of the CLI number, the projection selection is
double-counting — go back to step 2.

Datapoints age out: `aws/spans` retention is 30 days and metrics ~15 months, but
the known datapoints are 07-09..07-26. If `range=7d` reads 0 by the time this
runs, generate fresh evidence by invoking the `hr-database` harness agent as
`hr-analyst` against `create_payout` (the documented repro in
`docs/lab/11-governance.md`) rather than widening the assertion.

## Rollback points

- After step 5: `git checkout backend/app/routers/governance.py` restores the
  hardcoded `evidence_count=0` gate behavior without touching the read path.
- Whole task: plain revert. No AWS state, no migration, no ledger change.

## Do not

- Populate `decisions[]`, `principal`, `action`, `trace_id`, or `session_id` from
  metrics.
- Add a `boto3.client(...)` call outside `observability.py` / `agentcore/client.py`.
- Touch `make bootstrap`, delivery sources, or `GET /api/governance/decisions`.
- Relax `_assert_evidence_or_override()`.
