# Design — per-decision rows from Policy spans (R3)

Authored **after** the R2 capture, as `prd.md` requires. Every attribute referenced
here appears verbatim in `research/policy-span-corpus.md`.

## Boundaries

| File | Change |
|---|---|
| `backend/app/services/governance_spans.py` (new) | parse `aws/spans` into decision rows |
| `backend/app/services/governance_evidence.py` | merge rows into the existing response; `source` becomes `metrics+spans` |
| `backend/tests/test_governance_spans.py` (new) | stubbed on the captured corpus |
| `frontend/src/lib/api.ts` | extend `GovernancePolicyDecision` |
| `frontend/src/pages/governance/DecisionView.tsx` | the existing table finally has rows; absent-field rendering |
| `frontend/src/locales/{en,zh-CN}/common.json` | new keys, added together |
| `docs/architecture.md`, `docs/lab/11-governance.md`, spec | behavior update |

A separate module per channel, mirroring the existing split: `governance_evidence.py`
owns metric-dimension volatility, `governance_spans.py` owns span-attribute
volatility, and `observability.py` owns the Logs Insights transport.

## Transport

Reuse `observability.run_insights_queries(queries, hours, logs=...)` — it already
starts queries concurrently, polls with a deadline, stops lost queries, and returns
flattened rows. Range mapping: `1h→1`, `6h→6`, `24h→24`, `7d→168`.

**Select `@message`, not individual fields.** Logs Insights flattens arrays into
`attributes.x.0`, `attributes.x.1`, … which cannot express
`determining_policies` / `denied_tools` of unknown length. Parsing the raw JSON
per row keeps the arrays intact.

Pass 1 — the decision spans, scoped to one gateway:

```text
fields @message
| filter name in ["AgentCore.Gateway.InvokeTool",
                 "AgentCore.Policy.AuthorizeAction",
                 "AgentCore.Policy.PartiallyAuthorizeActions"]
  and `attributes.aws.resource.arn` = "<gateway arn>"
| sort @timestamp desc
| limit 400
```

Pass 2 — `session.id`, only if pass 1 produced rows, keyed on the traceIds found:

```text
fields traceId, `attributes.session.id`
| filter traceId in [...] and ispresent(`attributes.session.id`)
| limit 400
```

Two passes, the second conditional and bounded by pass 1. Skip pass 2 entirely when
pass 1 is empty.

## Row assembly

1. Parse each `@message` to JSON; bucket by `name`.
2. Index `AgentCore.Policy.*` spans by `parentSpanId` — the capture shows the Policy
   span is a **child of** the Gateway span (`AuthorizeAction` under
   `AgentCore.Gateway.InvokeTool`, `PartiallyAuthorizeActions` under
   `AgentCore.Gateway.ListTools`).
3. **Invocation rows** — one per `AgentCore.Gateway.InvokeTool` span. That span alone
   carries `tool.name` **and** `aws.agentcore.policy.authorization_decision`, so
   action and outcome need no join. Enrich from the sibling Policy span
   (`policy_spans[invoke_span.spanId]`) for `determining_policies`,
   `mismatched_policies`, `log_only_matched_policies`, and the engine ARN.
4. **Tool-listing rows** — one per entry in `denied_tools` on each
   `PartiallyAuthorizeActions` span, `outcome = DENY`,
   `evaluation = "tool_listing"`.
   `allowed_tools` is **not** expanded into rows: a single `tools/list` produced 5
   allowed tools, so expanding both sides would flood the table with one row per
   tool per list call while adding nothing the aggregate panel does not already
   show. The response therefore says, and the UI must say, that listing rows cover
   denials only.
5. `session_id` from the pass-2 `traceId → session.id` map.
6. Sort by timestamp desc, cap at 200 rows.

`evaluation` distinguishes the two kinds and is **required**, not cosmetic: a
`tool_listing` DENY means "the tool was withheld from the model at list time", not
"a call was blocked". Presenting them identically would misrepresent what happened
— and under `ENFORCE` the listing denial is the *only* DENY that can occur, so this
is the common case, not an edge case.

## Field mapping (all verified)

```python
{
  "at": iso(span["startTimeUnixNano"]),
  "gateway_id": attrs["aws.agentcore.policy.target_resource.id"] or attrs["gateway.id"],
  "gateway_arn": attrs["aws.resource.arn"],
  "action": attrs["tool.name"],                                    # or the denied tool
  "outcome": attrs["aws.agentcore.policy.authorization_decision"],  # ALLOW | DENY
  "engine_mode": attrs["aws.agentcore.gateway.policy.mode"],
  "engine_id": _engine_id_from(attrs["aws.agentcore.gateway.policy.arn"]),
  "policy_id": determining[0] if determining else None,
  "determining_policies": determining,
  "mismatched_policies": attrs.get("aws.agentcore.policy.mismatched_policies") or [],
  "log_only_matched_policies": attrs.get("aws.agentcore.policy.log_only_matched_policies") or [],
  "trace_id": span["traceId"],
  "span_id": span["spanId"],
  "session_id": sessions.get(span["traceId"]),
  "evaluation": "invocation" | "tool_listing",
  "principal": None,
  "policy_mode": None,
  "source": "aws",
}
```

`principal` and `policy_mode` are **hardcoded `None` with a comment stating why** —
no span in the 31-span trace carries a principal (the Harness authenticates with an
M2M client credential, so the request has no human subject), and only the Gateway
attachment mode is present, not the per-policy mode. They stay in the shape so the
frontend contract is unchanged and so a future capture can fill them.

**Not referenced anywhere:** `aws.agentcore.policy.authorization_reason`. It is
documented but was absent on the captured ALLOW span, and no DENY `AuthorizeAction`
could be captured under `ENFORCE`. Adding a `.get()` for it would be the exact
"documented-but-unverified field" mistake the research gate exists to prevent — so
the parser must not mention it, and `reason` is not part of the row.

## Merging into the existing response

`governance_evidence.gateway_decisions()` gains the span read after the metric
aggregation:

```python
spans = governance_spans.gateway_decision_rows(logs, gateway_arn, range_key, policy_id)
result["decisions"] = spans["decisions"]
result["count"] = len(spans["decisions"])
result["source"] = "metrics+spans" if spans["decisions"] else "metrics"
result["spans_unavailable_reason"] = spans["unavailable_reason"]  # None when fine
```

**A span failure must not break the metric response.** The metric channel is what
the cutover gate reads and what already ships; a Logs Insights outage, throttle, or
timeout degrades to metrics-only with `spans_unavailable_reason` set, never a 5xx.
So the span call is wrapped and `AppError` from `run_insights_queries` is caught
here — the one place where swallowing it is correct.

`evidence_count` stays metric-derived. Spans are sampled (X-Ray indexing rules) while
metrics are exact counts, so spans must never redefine the number the gate trusts.
The UI already carries the "which number is which" caveat from the sibling task.

`policy_id` filter: applied to rows by matching `determining_policies`.

## Frontend

`GovernancePolicyDecision` gains `span_id`, `evaluation`, `determining_policies`,
`mismatched_policies`, `log_only_matched_policies`; `principal` and `policy_mode`
become explicitly nullable.

The table in `DecisionView.tsx` already renders `decisions[]` with the right
columns, so it needs: a chip for `evaluation`, absent-rendering for `principal`
(an em dash plus a tooltip/note explaining M2M auth rather than a bare `-`, so it
does not read as a bug), and `log_only_matched_policies` surfaced — that is the one
piece of information the metric channel cannot produce and it directly answers "what
would the LOG_ONLY candidate have done".

## Testing

`backend/tests/test_governance_spans.py`, stubbing `run_insights_queries` with the
**verbatim spans from the research note** (copied, not paraphrased):

1. `InvokeTool` + child `AuthorizeAction` → one invocation row, ALLOW, action
   `hr-database___list_departments`, `policy_id` = the determining policy,
   `log_only_matched_policies` carried through.
2. `PartiallyAuthorizeActions` → one `tool_listing` DENY row for
   `hr-database___create_payout`, and **no** rows for the 5 allowed tools.
3. `session_id` joined from pass 2 by `traceId`.
4. `principal` and `policy_mode` are `None` on every row.
5. No row contains a `reason` key, and the module source does not mention
   `authorization_reason` (guards the research gate mechanically).
6. Pass-1 empty → pass 2 never runs.
7. `AppError` from the query → `decisions: []` with `spans_unavailable_reason`, and
   the metric aggregates still present.
8. `policy_id` filter narrows rows by `determining_policies`.

## Tradeoffs

- **`allowed_tools` not expanded into rows.** Keeps the table readable; the counts
  live in the aggregate panel. Stated in the response and UI.
- **Two Logs Insights passes.** One scan cannot both fetch decision spans and
  resolve `session.id`, which lives on a different span. The second pass is
  conditional and bounded.
- **200-row cap.** Logged in the response rather than silently truncated.

## Rollout / rollback

Read-only, no AWS mutation, no migration. Revert restores metrics-only behavior;
`decisions[]` simply goes back to empty, which the frontend already handles because
the sibling task shipped that state.
