# Design — policy decision evidence from CloudWatch metrics

## Boundaries

| Layer | File | Change |
|---|---|---|
| Service | `backend/app/services/governance_evidence.py` (new) | metric query + aggregation; the only new module |
| Service | `backend/app/services/governance.py` | delete `unavailable_policy_decisions()`; add `gateway_evidence_count()` helper used by the gate call sites |
| Client factory | `backend/app/services/observability.py` | promote `_cw_client()` to public `cw_client()` |
| Router | `backend/app/routers/governance.py` | decisions route stops discarding params; three gate call sites pass a real count |
| Client | `frontend/src/lib/api.ts` | response type extension |
| View | `frontend/src/pages/governance/DecisionView.tsx` | aggregate rendering + three-state handling |
| i18n | `frontend/src/locales/{en,zh-CN}/common.json` | new keys, added together |
| Docs | `docs/architecture.md`, `docs/lab/11-governance.md` | behavior update |

A separate module keeps the CloudWatch-metric shape volatility out of
`governance.py`, which is already ~1700 lines and owns the control-plane and
ledger logic. It mirrors how `observability.py` isolates telemetry access.

## Client construction

CLAUDE.md requires a single construction site per client family and explicit
injection so tests can stub. `observability.py:113` already builds the
`cloudwatch` client as a private `_cw_client()`. Rename it to `cw_client()`,
export it, and have the router pass it in:

```python
governance_evidence.gateway_decisions(control_client(), cw_client(), gateway_id, range_, policy_id, force)
```

Do **not** add a CloudWatch factory to `services/agentcore/client.py` — that
module is documented as the place AgentCore clients are built, and CloudWatch is
not an AgentCore client. Update the `_cw_client` reference inside
`observability.py` in the same edit.

## The dimension-projection problem (core correctness)

AWS emits several overlapping projections of the same decision event. Verified
projections present in the account for `DenyDecisions`:

```
{OperationName}
{OperationName, Mode, PolicyEngine}
{TargetResource, OperationName}
{TargetResource, OperationName, Mode}
{TargetResource, OperationName, Mode, PolicyEngine}
{TargetResource, ToolName, OperationName, Mode}
{TargetResource, ToolName, OperationName, Mode, PolicyEngine}
{Policy, TargetResource, OperationName, Mode, PolicyEngine}
...
```

Summing every stream returned by `list_metrics` inflates the count several-fold.

**Rule:** for each question, select streams by an *exact dimension-name set*, not
by a subset match.

| Question | Exact dimension set |
|---|---|
| Gateway total per outcome | `{TargetResource, OperationName, Mode}` |
| Per-policy | `{Policy, TargetResource, OperationName, Mode}` |
| Per-tool | `{TargetResource, ToolName, OperationName, Mode}` |

`evidence_count` = sum over the *gateway total* projection only, across both
`OperationName` values and both `Mode` values, for `AllowDecisions` +
`DenyDecisions`.

Per-policy and per-tool breakdowns are reported as their own sub-totals and are
**not** required to add up to `evidence_count` — AWS only publishes the `Policy`
dimension for decisions that had a determining policy, and `ToolName` only for
`PartiallyAuthorizeActions`. The response must not imply the breakdowns partition
the total; the UI labels them as breakdowns, not a decomposition.

Implementation helper:

```python
def _dim_map(metric): return {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}

def _select(metrics, exact_names: frozenset[str], target: str):
    return [m for m in metrics
            if frozenset(_dim_map(m)) == exact_names
            and _dim_map(m).get("TargetResource") == target]
```

## Query mechanics

Follow the verified pattern at `observability.py:708-757`:

1. `cw.get_paginator("list_metrics").paginate(Namespace="AWS/Bedrock-AgentCore", MetricName=<name>)`
   — note the namespace differs from `observability.py`'s `bedrock-agentcore`
   (the policy/service namespace is `AWS/Bedrock-AgentCore`; both exist in this
   account and they are not interchangeable).
2. Select streams per the table above.
3. One `get_metric_data` call with `Period = window_seconds` (clamped to ≥ 60),
   `Stat = "Sum"`, `StartTime/EndTime` from the range. Cap at 100 queries per
   call as `observability.py` does; if the cap truncates, say so in the response
   rather than silently dropping streams.
4. `Sum` over `Values` per stream.

Window map: `1h → 3600`, `6h → 21600`, `24h → 86400`, `7d → 604800`.

`list_metrics` returns streams that have reported in the last ~2 weeks
regardless of the query window, so a stream with no datapoints in-window yields
an empty `Values` list — that is the normal zero case, not an error.

## Response contract

```python
{
  "range": "24h",
  "available": True,              # channel readable
  "unavailable_reason": None,     # set only when available is False
  "source": "metrics",            # "metrics" | "spans" | "metrics+spans" (span child)
  "evidence_count": 64,
  "totals": {"allow": 58, "deny": 6},
  "by_operation": [{"operation": "AuthorizeAction", "allow": 58, "deny": 3}, ...],
  "by_mode":      [{"mode": "ENFORCE", "allow": 58, "deny": 6}],
  "by_policy":    [{"policy_id": "launchpad_baseline_allow-obafj1o9hj", "allow": 58, "deny": 0}],
  "by_tool":      [{"tool": "hr-database___create_payout", "allow": 0, "deny": 6}],
  "mismatch": {"determining": 12, "no_determining": 0, "errors": 0},
  "truncated": False,
  "decisions": [],                # stays empty; span child fills it
  "count": 0,                     # len(decisions) — unchanged meaning
  "cache": {"hit": False, "age_seconds": 0.0},
}
```

Backward compatibility: every existing field keeps its name and meaning.
`count` stays `len(decisions)` so the existing `count` i18n string is not
silently redefined; `evidence_count` is the new aggregate number. Additive only,
so the span child extends rather than breaks it.

Failure mapping: `ClientError` / `BotoCoreError` → `available: False`,
`unavailable_reason` = the AWS error code (e.g. `AccessDeniedException`), same
`operator_error` convention already used by `preflight` at
`governance.py:426-432`. `policy_span_shape_not_verified` is deleted.

## Caching

Reuse the module-level TTL cache already in `observability.py` (`_CACHE`,
`_CACHE_LOCK`, `CACHE_TTL_SECONDS`, and the `cache: {hit, age_seconds}` envelope
the contract already carries). Key on
`("gov-decisions", gateway_id, range, policy_id or "")`. `force=True` bypasses
the read but still writes. If the existing cache helper is not cleanly
importable, replicate the same envelope shape rather than inventing a new one.

## Evidence gate wiring

Add to `governance.py`:

```python
def gateway_evidence_count(control, cw, gateway_id: str, window: str = "24h") -> int:
    """Best-effort real evidence count for the promotion gate. Never raises —
    an unreadable channel yields 0, which falls back to the existing override
    path rather than blocking the operator with a telemetry error."""
```

Swallowing errors here is deliberate: the gate's job is to require evidence *or*
an override, and a CloudWatch outage must not make a legitimate override
impossible. Log the failure.

Router: replace `evidence_count=0` at `:183`, `:207`, `:226` with
`evidence_count=governance_service.gateway_evidence_count(control_client(), cw_client(), gateway_id)`.

Gate window: `24h`, matching the promotion rule already documented in
`docs/lab/11-governance.md` ("界面要求 24 小时内有 LOG_ONLY 决策证据").

`_assert_evidence_or_override()` itself is **not** modified.

Known consequence to state in docs: the gate counts both LOG_ONLY and ENFORCE
decisions because a single metric query cannot express "LOG_ONLY only" without
splitting by `Mode`. Split by `Mode` and count only `LOG_ONLY` for the gate, so
the documented rule stays literally true. Aggregate display keeps both modes.

## Frontend

`GovernanceDecisionResponse` gains the fields above, all required (backend always
emits them), except `unavailable_reason` which stays `string | null`.

`DecisionView` branch order becomes:

1. `error` → existing error block.
2. `!data.available` → existing warn block, showing `unavailable_reason`.
3. `data.available && data.evidence_count === 0` → **new** neutral empty state
   (`governance.decisions.noEvidenceInWindow`), explicitly stating the channel is
   readable and the window is quiet, with a hint to widen the range.
4. otherwise → aggregate panels + the existing `DataTable` for `decisions[]`
   (empty for now, so the table renders its `noAwsEvidence` empty state; keep it
   so the span child needs no restructuring).

Aggregate rendering uses existing `Panel` / `DataTable` / `Chip` primitives; no
new component. Keep `?view=` routing untouched.

New i18n keys (en + zh-CN together): `noEvidenceInWindow`, `evidenceCount`,
`byOperation`, `byMode`, `byPolicy`, `byTool`, `aggregateNote` (the "breakdowns
are not a partition" caveat), `truncated`, `sourceMetrics`.

## Testing

`backend/tests/test_governance.py`, stubbed `cw` (AWS is stubbed in this suite;
`conftest.py` redirects SQLite):

1. **No double counting** — feed overlapping projections of the same event; assert
   `evidence_count` equals the gateway-total projection only.
2. **Zero window ≠ unavailable** — streams exist, `Values` empty → `available:
   True`, `evidence_count: 0`.
3. **Unreadable channel** — `ClientError(AccessDeniedException)` → `available:
   False` with that reason, and no exception escapes.
4. **`policy_id` filter** — narrows to the `Policy` projection.
5. **`force`** — bypasses cache (two calls, stub call-count assertion).
6. **Gate admits with evidence** — promote with no override succeeds when the
   stub reports LOG_ONLY evidence.
7. **Gate still refuses without evidence** — zero evidence + no override → the
   existing `governance.evidence_required` 409.
8. **LOG_ONLY vs ENFORCE** — ENFORCE-only evidence does not satisfy the gate.

Real-AWS spot check is manual and outside `make verify` (per the
`tests/` vs `e2e_*.py` split in CLAUDE.md).

## Tradeoffs

- **Aggregates instead of rows.** Less useful than per-decision rows, but it is
  what this channel can honestly support, and it is what the gate actually needs.
- **Two data sources will coexist.** Accepted: `source` discriminates, and the
  contract is additive.
- **`list_metrics` cost per request.** Mitigated by the TTL cache. A tighter
  option (cache the stream list separately from the datapoints) is deferred as
  premature.

## Rollout / rollback

Read-only feature behind no flag. Rollback is a straight revert — no AWS state,
no migration, no ledger change. The only external dependency is
`cloudwatch:ListMetrics` + `cloudwatch:GetMetricData`, which the dev box's
`admin_role_for_workshop` already has; confirm the deployed backend role has them
and note it in the docs if it does not.
