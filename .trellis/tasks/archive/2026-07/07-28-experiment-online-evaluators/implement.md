# Implement — selectable online evaluators

## Ordered steps

1. **Service plumbing** (`backend/app/optimization/service.py`)
   - [ ] Add `ONLINE_EVAL_DEFAULT`, `ONLINE_EVAL_MAX`, `normalize_online_evaluators()`.
   - [ ] `create_online_eval_idempotent(..., evaluators: Sequence[str] | None = None)`,
         building `[{"evaluatorId": i} for i in ids]`.
   - [ ] Thread through `stage_gateway(..., evaluators=None)` and
         `act_gateway(exp_id, progress, evaluators=None)`; persist `online_evaluators`
         on the artifact.
   - Validate: `cd backend && uv run ruff check . && uv run pytest tests/optimization -q`

2. **Router** (`backend/app/optimization/routers.py`)
   - [ ] `online_evaluators: list[str] | None = Field(default=None, min_length=1, max_length=10)`
         on `ActionRequest`.
   - [ ] For `action == "gateway"`: normalize (raising `AppError`
         `experiment.evaluator_unsupported`, 400) before `service.run_action`.
   - Validate: same pytest run.

3. **Backend tests** (`backend/tests/optimization/test_stepwise_actions.py`)
   - [ ] default pair when the field is omitted;
   - [ ] explicit list reaches `create_online_evaluation_config` in order and lands on the
         artifact;
   - [ ] `Builtin.TrajectoryInOrderMatch` → 400 `experiment.evaluator_unsupported`;
   - [ ] unknown `Builtin.Nope` → 400;
   - [ ] `[]` and 11 ids → 422;
   - [ ] duplicates collapse.
   - Validate: `uv run pytest tests/optimization -q`

4. **Frontend** (`frontend/src/pages/EvaluationExperiment.tsx`)
   - [ ] Fetch `/api/eval/evaluators` alongside the existing datasets fetch; drop
         `requires_ground_truth` entries.
   - [ ] `chosenOnlineEvaluators` state seeded with the two defaults; `selchip` toggle row
         inside the `gwab` card, rendered only while `!a.gateway`.
   - [ ] `actionBtn("gateway", …, { extra: { online_evaluators: chosen } })`.
   - [ ] After the stage: echo `a.gateway.online_evaluators` via `evaluatorLabel`.
   - [ ] Type the new artifact field where the experiment artifacts are declared.
   - Validate: `cd frontend && npm run lint && npx tsc --noEmit`

5. **i18n** (`frontend/src/locales/{en,zh-CN}/common.json`)
   - [ ] `expPage.onlineEvaluators`, `expPage.onlineEvaluatorsHint`, `expPage.onlineEvalTag`.
   - Validate: `python3 scripts/i18n_check.py`

6. **Full gate**
   - [ ] `make verify`

7. **Real rerun (manual, hits AWS)** — review gate before starting: it costs ~55 min and
   60 real invocations.
   - [ ] New experiment on `lab-fund-assistant`; at `GATEWAY` pick
         `指令遵循 Builtin.InstructionFollowing` + `拒答 Builtin.Refusal` +
         `有用性 Builtin.Helpfulness`.
   - [ ] Traffic = `lab-fund-dataset-60`; **wait until every session is scored** before
         `▸ 监控结果` (see §9.9.3 — the verdict is computed once).
   - [ ] Record per-evaluator mean / n / p, screenshot the verdict card, then `清理`.

8. **Docs**
   - [ ] `docs/lab/09-experiment-ab.md`: note in 9.4 that the evaluator set is now chosen
         here; replace the "写死为 GoalSuccessRate + Helpfulness" conclusion in 9.9.3 with
         the measured goal-aligned result.
   - [ ] `docs/architecture.md` if it documents the fixed pair.
   - [ ] Re-run `make verify` after doc edits (i18n parity).

## Review gates

- After step 3: backend contract is testable without AWS — stop and read the diff.
- Before step 7: confirm the AWS spend/time with the operator.
- After step 8: `make verify` green, then commit.

## Rollback points

- Steps 1–5 are one commit; `git revert` restores the hard-coded pair (no data migration).
- Step 7 creates AWS resources; `清理` (cleanup action) is idempotent and removes them.
