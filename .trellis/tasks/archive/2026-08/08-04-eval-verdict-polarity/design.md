# Design

## Where polarity knowledge lives

`app/evaluation/agentcore_eval.py` already owns the evaluator registry
(`ALL_BUILTIN_EVALUATORS`, `TRAJECTORY_EVALUATORS`) and is the only module allowed to hold
AgentCore-shaped facts. Polarity is exactly that kind of fact, so it goes next to the
registry:

```python
# Built-in evaluators whose score is a penalty: the judge answers "Yes"/"Harmful"/
# "Stereotyping" when the response is BAD, so the lower-mean arm is the better arm.
# Verified against the AWS built-in prompt templates (prompt-templates-builtin).
LOWER_IS_BETTER_EVALUATORS = frozenset({
    "Builtin.Refusal", "Builtin.Harmfulness", "Builtin.Stereotyping",
})


def evaluator_polarity(evaluator: str) -> int:
    """+1 when a higher mean is the better arm, -1 when a lower mean is.

    Accepts a bare id (``Builtin.Refusal``) or an evaluator ARN. Custom judges are
    +1: AWS exposes no direction on ``ratingScale``, and the launchpad's own default
    scale is pass=1.0 / fail=0.0.
    """
    return -1 if (evaluator or "").rsplit("/", 1)[-1] in LOWER_IS_BETTER_EVALUATORS else 1
```

`normalize_ab_results` annotates each metric with `"polarity": evaluator_polarity(arn or
label)` so the value is stored in the experiment/canary artifact and does not have to be
re-derived by every consumer.

## `compute_verdict` — orient, then weight

Contract stays identical (same keys, same insufficient-* verdicts); only the meaning of
`avg_delta` sharpens: **positive always means "treatment is better"**, regardless of the
evaluator's direction.

```
for each metric:
    polarity = evaluator_polarity(metric["evaluatorId"] or metric["label"])
    for each variant:
        total_n += control.sampleSize + variant.sampleSize   # unchanged
        if both means present:
            weight = min(control.sampleSize, variant.sampleSize) or 1.0
            weighted_sum += polarity * (t_mean - c_mean) * weight
            weight_total += weight
avg_delta = weighted_sum / weight_total
```

- **Orient before averaging** — the whole point of the fix.
- **Weight by `min` of the two arms**, because a delta is only as precise as its smaller
  arm; an evaluator that returned 2 scores can no longer outvote one that returned 40.
  `or 1.0` keeps a metric that has means but no reported sample size from being dropped.
- `total_n` keeps summing both arms across all metrics (unchanged) so the `min_n * 2` gate
  and the canary's `metric_sample_count` fresh-evidence marker keep their current meaning.
- `weight_total == 0` replaces the old `not deltas` check → same `"arms have no means yet"`
  insufficient-data branch.
- `significant` and the `winner` mapping (`> 0` / `< 0` / tie) are untouched — they now
  read an oriented number, which is the fix.

`canary_service.py:568` and `service.py:1086/1093` need **no change**: they call
`compute_verdict`, so both the stored verdict and the canary ramp gate
(`assert_verdict_allows`, which blocks `control-wins` from advancing) are corrected by
construction.

## Frontend

Two sources for polarity, deliberately:

1. `metric.polarity` from the backend (authoritative, and present in newly stored
   artifacts).
2. `evaluatorPolarity()` in `src/lib/evaluators.ts` as the fallback — verdict artifacts
   written *before* this change have no `polarity` field, and `src/lib/evaluators.ts`
   already mirrors the built-in evaluator list for labels, so the duplication follows an
   existing convention rather than inventing one.

`EvaluationExperiment.tsx` verdict card (~1546):

- keep showing the **raw** delta (operators want the actual mean change),
- colour it by `delta * polarity` (`var(--good)` / `var(--warn)`),
- add a `↓` marker + `title` on lower-is-better metrics so the negative number reads as an
  improvement,
- add a `title` on the summary `Δ {avg_delta}` explaining it is a sample-size-weighted,
  polarity-normalized average in which custom judges are assumed higher-is-better.

`ABMetric` in `src/lib/api.ts` gains `polarity?: number`. New i18n keys under `expPage`
(en + zh-CN, parity enforced by `scripts/i18n_check.py`).

## Ground-truth placeholder rejection

`agentcore_eval.py`:

```python
# Placeholders fed from evaluationReferenceInputs. AWS: "Custom evaluators that use
# ground truth placeholders cannot be used in online evaluation configurations."
GROUND_TRUTH_PLACEHOLDERS = ("expected_response", "expected_tool_trajectory", "assertions")


def ground_truth_placeholders(instructions: str) -> list[str]:
    """Ground-truth placeholders a custom judge's instructions reference."""
```

`normalize_online_evaluators(ids, control=None)` gains a second pass that runs **after**
dedup and the `ONLINE_EVAL_MAX` cap (so a request that is rejected anyway costs no AWS
call), and only over non-`Builtin.` ids (so the common built-in-only path stays at zero
extra calls):

- `client = control or control_client()`, built lazily and only when a custom id exists.
- `ac.get_evaluator(client, evaluator_id=cid)` →
  `evaluatorConfig.llmAsAJudge.instructions`. A code-based (Lambda) evaluator has no
  instructions → skip, nothing to inspect.
- any hit → `AppError("experiment.evaluator_unsupported", …, status_code=400)` naming both
  the evaluator and the placeholder, mirroring the existing trajectory-rejection message.
- `ResourceNotFoundException` → `AppError` (an id AWS does not know would fail at
  `CreateOnlineEvaluationConfig` anyway; failing here is strictly better).
- any other lookup error → **fail open** (skip that id). A transient control-plane blip
  must not block gateway creation, and AWS enforces the constraint server-side per its
  docs, so the worst case degrades to today's behaviour.

`stage_gateway` passes its existing `control` client through
(`normalize_online_evaluators(evaluators, control=control)`) so the resume path validates
without building a second client. `routers.py` keeps its current call and lets the lazy
client be built — it only pays for it when a custom evaluator is actually selected.

## Compatibility / rollback

- Verdict dict keys, types and verdict strings unchanged → no frontend or ledger
  migration; stored verdicts from before the change keep rendering (polarity falls back to
  the frontend map).
- `avg_delta` values recomputed for *new* verdicts only; historical artifacts are not
  rewritten (they are a record of what was decided at the time).
- Rollback = revert the commit; nothing is persisted in a new shape that older code cannot
  read (`polarity` is additive and optional).
