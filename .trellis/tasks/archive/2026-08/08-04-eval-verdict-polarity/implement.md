# Implementation plan

Ordered so each step is independently verifiable. Backend first (it defines the contract the
frontend consumes), tests alongside, frontend last.

## 1. Polarity primitives — `backend/app/evaluation/agentcore_eval.py`

- [ ] Add `LOWER_IS_BETTER_EVALUATORS` + `evaluator_polarity()` next to
      `TRAJECTORY_EVALUATORS`, with the "verified against prompt-templates-builtin" note.
- [ ] Add `GROUND_TRUTH_PLACEHOLDERS` + `ground_truth_placeholders()` in the custom-evaluator
      section (next to `create_llm_judge_evaluator`).
- [ ] `normalize_ab_results`: add `"polarity": evaluator_polarity(arn or label)` to each
      emitted metric.

Validate: `cd backend && uv run pytest tests/optimization -q`

## 2. `compute_verdict` — `backend/app/optimization/service.py:913`

- [ ] Replace the `deltas` list with `weighted_sum` / `weight_total` per design.md; orient by
      `evaluator_polarity(metric["evaluatorId"] or metric["label"])`, weight by
      `min(control n, variant n) or 1.0`.
- [ ] Update the docstring: `avg_delta` is a sample-size-weighted, polarity-normalized
      average where positive always favours treatment.
- [ ] Leave `total_n`, the `min_n * 2` gate, `significant` and the winner mapping alone.

Validate: existing `tests/optimization/test_weights_and_cleanup.py` must still pass
unchanged.

## 3. Backend tests — `backend/tests/optimization/test_verdict_polarity.py` (new)

- [ ] Refusal-only improvement (`c=0.2 → t=0.0`, n≥3 per arm) → `treatment-wins`,
      `avg_delta > 0`  (AC1)
- [ ] Helpfulness `c=0.4 → t=0.6` → `treatment-wins` (AC2)
- [ ] Mixed set: Helpfulness regresses with n=40, Refusal improves with n=2 → verdict
      follows Helpfulness (AC3) — asserts the weighting, not just the sign
- [ ] Custom/unknown id → treated `+1`, no raise (AC4)
- [ ] `Builtin.Harmfulness` / `Builtin.Stereotyping` also oriented
- [ ] metric whose `evaluatorId` is a full ARN resolves polarity (ARN-suffix parsing)
- [ ] `normalize_ab_results` emits `polarity` for a builtin penalty + a custom evaluator

## 4. Ground-truth rejection — `backend/app/optimization/service.py:617`

- [ ] Signature → `normalize_online_evaluators(ids, control=None)`; collect custom ids in the
      existing loop.
- [ ] After the `ONLINE_EVAL_MAX` cap, inspect only custom ids: lazy `control_client()`,
      `ac.get_evaluator`, `llmAsAJudge.instructions`, `ac.ground_truth_placeholders`.
- [ ] Raise `AppError("experiment.evaluator_unsupported", …, 400)` naming evaluator +
      placeholder; `ResourceNotFoundException` → `AppError`; other errors → fail open.
- [ ] Rewrite the docstring paragraph that currently promises custom ids "pass through
      unchecked".
- [ ] `stage_gateway` (`service.py:824`) passes `control=control`.

## 5. Backend tests — `backend/tests/optimization/test_online_evaluator_validation.py` (new)

- [ ] custom judge with `{expected_response}` → `AppError`, 400, `experiment.evaluator_unsupported` (AC7)
- [ ] `{assertions}` and `{expected_tool_trajectory}` likewise
- [ ] clean custom judge → returned in the chosen list (AC8)
- [ ] built-in-only selection → stub client records **zero** `get_evaluator` calls (AC8)
- [ ] code-based (no `llmAsAJudge`) evaluator → passes, no crash
- [ ] `ResourceNotFoundException` → `AppError`; generic `Exception` → fails open (id kept)
- [ ] trajectory + unknown-builtin + cap + dedup + default fallback behaviour unchanged

Validate: `cd backend && uv run ruff check . && uv run pytest -q`

## 6. Frontend

- [ ] `src/lib/evaluators.ts`: `LOWER_IS_BETTER` set + `evaluatorPolarity(id)` (fallback for
      pre-change artifacts; comment why the duplication exists).
- [ ] `src/lib/api.ts`: `ABMetric.polarity?: number`.
- [ ] `src/pages/EvaluationExperiment.tsx`: `ABMetric` local type + verdict card — polarity
      colour, `↓` marker + title on lower-is-better metrics, title on the summary `Δ`.
- [ ] i18n: new `expPage` keys in `src/locales/en/common.json` and `zh-CN/common.json`.

Validate: `cd frontend && npm run lint && npx tsc --noEmit && npm run build`;
`python3 scripts/i18n_check.py`

## 7. Gate + docs

- [ ] `make verify`
- [ ] Spec update (step 3.3): record the polarity contract + the online-eval ground-truth
      constraint under `.trellis/spec/launchpad/` (evaluation/experiment spec); check whether
      `docs/architecture.md` describes the verdict computation and needs the same note.
- [ ] Commit (step 3.4).

## Review gates

- After step 2: re-read the diff against `canary_service.py:555-580` to confirm the canary
  auto-decision now reads an oriented delta and nothing else changed for it.
- After step 5: confirm no new AWS call on the built-in-only path (the common case).

## Rollback points

- Steps 1-3 are additive + one function body; revert alone if the weighting proves
  undesirable (polarity orientation and weighting are separable — orientation is the bug
  fix, weighting is the requested improvement).
- Step 4 is self-contained in one function + one call site.
- Step 6 is presentation-only.
