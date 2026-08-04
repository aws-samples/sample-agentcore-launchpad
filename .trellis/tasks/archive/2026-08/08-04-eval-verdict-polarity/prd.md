# Fix evaluation verdict polarity and online judge placeholder validation

## Problem

Two defects in the evaluation/optimization module, both reported by river on 2026-08-04
and both confirmed in the code.

### P1 — `compute_verdict` averages metrics with opposite polarity

`backend/app/optimization/service.py:927` takes `t_mean - c_mean` unconditionally for
every evaluator metric, averages the raw deltas, and calls `avg_delta > 0`
"treatment-wins".

Three of the 13 built-in evaluators are **penalty** scores where a *lower* mean is the
better arm (verified against the AWS built-in prompt templates,
`prompt-templates-builtin`):

| evaluator | score meaning | direction |
|---|---|---|
| `Builtin.Refusal` | "Yes" = the response *is* a refusal | lower is better |
| `Builtin.Harmfulness` | "Harmful" / "Not Harmful" | lower is better |
| `Builtin.Stereotyping` | "Stereotyping" / "Not Stereotyping" | lower is better |

So the reported case is real: control Refusal `0.2` → treatment `0.0` is a genuine
improvement, but it contributes `-0.2` and pushes the verdict toward `control-wins`.

Blast radius is wider than the experiment UI:

- `app/optimization/service.py:1086,1093` (`act_verdict`) — the stored experiment verdict
  and the loop that waits for one.
- `app/optimization/canary_service.py:568` → `assert_verdict_allows`
  (`canary_service.py:288-318`) — the **Runtime Canary ramp gate**. It refuses to advance on
  `control-wins`, so a polarity-flipped verdict blocks a canary that genuinely improved,
  and conversely clears one that regressed. (Verified during implementation: the gate
  permits/blocks an operator ramp action; it is not a fully automatic promote/rollback.)
- `frontend/src/pages/EvaluationExperiment.tsx:1546` renders `variant.mean -
  control.mean` raw, and uses `delta >= 0` for the `+` sign — no polarity concept, so a
  safety improvement reads as a negative number with no cue.

There is also no weighting: an evaluator that produced 2 scores counts exactly as much as
one that produced 40.

### P2 — online evaluation does not reject custom judges that need ground truth

`normalize_online_evaluators()` (`app/optimization/service.py:617`) rejects
`TRAJECTORY_EVALUATORS` and unknown `Builtin.*` ids, but lets every custom (non-`Builtin.`)
id through unchecked — the docstring says so explicitly. A custom LLM-as-a-judge whose
instructions reference a ground-truth placeholder therefore passes validation.

Per AWS docs (`create-evaluator`), the ground-truth placeholders are `expected_response`,
`expected_tool_trajectory` and `assertions`, and: *"Custom evaluators that use ground
truth placeholders cannot be used in online evaluation configurations… The service
automatically detects ground truth placeholders during evaluator creation and enforces
this constraint."*

**Correction to the reported framing:** the judge is therefore *not* silently scoring
against empty ground truth — AWS rejects it. The actual defect is *when* it is rejected:
`stage_gateway()` (`service.py:812-830`) creates the shared gateway and the v1 runtime
target **before** `CreateOnlineEvaluationConfig`, so the failure surfaces as an opaque
mid-stage `ValidationException` after AWS resources already exist. That is precisely what
the comment at `app/optimization/routers.py:243-244` says validation is there to prevent
("a bad evaluator id shouldn't cost a gateway + runtime-target round-trip"). The fix is
the same one requested: reject in `normalize_online_evaluators()` with a clear 400.

## Scope

In scope:

1. Polarity-aware, sample-size-weighted `avg_delta` + winner in `compute_verdict`, so both
   the experiment verdict and the canary auto-decision are correct.
2. Per-metric polarity exposed to the console; polarity-aware delta rendering in the
   experiment verdict card (en + zh-CN strings).
3. Ground-truth-placeholder rejection for custom judges in `normalize_online_evaluators`.
4. Backend unit tests for both, plus a spec note.

Out of scope (explicitly not changed):

- No polarity field/UI on evaluator create/edit — decided: custom judges default to
  higher-is-better (see Decisions).
- No new warning on `POST`/`PUT /api/eval/evaluators` — the rejection lives only on the
  online-eval path.
- No statistical significance rework (`isSignificant` still comes from AWS as-is).
- `_evaluator_out` only reading `ratingScale.numerical` (categorical scales exist) is a
  pre-existing gap, untouched.

## Decisions (confirmed with river, 2026-08-04)

- **Custom judge polarity defaults to higher-is-better.** AWS exposes no direction on
  `ratingScale`, and the launchpad's own `DEFAULT_RATING_SCALE` is `pass=1.0 / fail=0.0`,
  so higher-is-better is the honest default. Only the three built-in penalty evaluators
  are marked lower-is-better. The assumption must be visible in the UI, not implicit.
- **Placeholder rejection only at the online-eval layer** (`normalize_online_evaluators`).
  Batch dataset runs must keep working with ground-truth judges — that is what they are
  for.
- **Trellis task + planning** before implementation.

## Acceptance criteria

1. `compute_verdict` on a single `Builtin.Refusal` metric with control mean `0.2`,
   treatment mean `0.0` and enough samples returns `treatment-wins` with a positive
   `avg_delta`.
2. `compute_verdict` on a single `Builtin.Helpfulness` metric with control `0.4`,
   treatment `0.6` still returns `treatment-wins` (no regression for higher-is-better).
3. A mixed metric set where a large-sample higher-is-better evaluator regresses and a
   small-sample penalty evaluator improves resolves toward the large-sample evaluator
   (weighting is by sample size, not per-metric one-vote-each).
4. An unknown/custom evaluator id is treated as higher-is-better and does not raise.
5. Existing `insufficient-data` / `insufficient-n` / `significant` behaviour and the
   verdict dict keys already consumed by the frontend (`verdict`, `avg_delta`, `n`,
   `significant`) are unchanged in name and type.
6. Every A/B metric returned by `normalize_ab_results` carries a `polarity` of `1` or
   `-1`; the console renders the raw delta but colours/annotates it by polarity, and
   lower-is-better metrics are visibly marked as such in both locales.
7. `normalize_online_evaluators` raises `AppError("experiment.evaluator_unsupported",
   status_code=400)` naming the placeholder when a selected custom judge's instructions
   reference `{expected_response}`, `{expected_tool_trajectory}` or `{assertions}`.
8. A custom judge with no ground-truth placeholder still passes; a built-in-only
   selection performs **no** `GetEvaluator` call at all (no added AWS latency on the
   common path).
9. `make verify` passes (backend ruff+pytest, infra, frontend eslint+tsc+build, i18n
   parity).
