# Design — selectable online evaluators (config-bundle experiments)

## Boundaries

Touched:

| File | Change |
|---|---|
| `backend/app/optimization/service.py` | `ONLINE_EVAL_DEFAULT`, `normalize_online_evaluators()`, evaluator param threaded through `create_online_eval_idempotent` → `stage_gateway` → `act_gateway` |
| `backend/app/optimization/routers.py` | `ActionRequest.online_evaluators`, validate + pass to `act_gateway` |
| `frontend/src/pages/EvaluationExperiment.tsx` | evaluator chips on the `gwab` card, `online_evaluators` in the action body, artifact echo |
| `frontend/src/lib/api.ts` | `gateway.online_evaluators?: string[]` on the experiment artifact type (if typed there) |
| `frontend/src/locales/{en,zh-CN}/common.json` | new `expPage.*` keys |
| `backend/tests/optimization/test_stepwise_actions.py` | default / pass-through / rejection cases |
| `docs/lab/09-experiment-ab.md` | 9.4 note + 9.9 rerun with the goal-aligned set |
| `docs/architecture.md` | one-line update if it states the pair is fixed |

Untouched by contract: `canary_service.py` (its own online-eval creation), target-based
A/B, `compute_verdict` (already loops over whatever metrics come back).

## Contract

```
POST /api/experiments/{id}/action
{ "action": "gateway",
  "online_evaluators": ["Builtin.InstructionFollowing", "Builtin.Refusal"] }   # optional
→ 202  { "experiment": … }
→ 400  { "code": "experiment.evaluator_unsupported", "message": …, "detail": {"evaluator": …} }
→ 422  pydantic: empty list / >10 items
```

Service-level normalizer (single source of truth, used by the router before spawning the
thread so the error is synchronous):

```python
ONLINE_EVAL_DEFAULT = ("Builtin.GoalSuccessRate", "Builtin.Helpfulness")
ONLINE_EVAL_MAX = 10          # AWS CreateOnlineEvaluationConfig cap

def normalize_online_evaluators(ids: Sequence[str] | None) -> list[str]:
    """Dedupe + validate; None → the default pair."""
```

Rules, in order: `None`/empty → default; strip blanks; dedupe preserving first
occurrence; reject `Builtin.Trajectory*` (`experiment.evaluator_unsupported`, reason
"needs dataset ground truth"); reject unknown `Builtin.*` ids; accept any non-`Builtin.`
id as a custom evaluator without a round-trip to AWS (an unknown custom id fails at
`CreateOnlineEvaluationConfig` and surfaces on the card like any other stage error);
reject `> ONLINE_EVAL_MAX`.

Artifact shape after the stage (additive, so old rows keep working):

```json
{"gateway_id": "...", "target_v1": "...", "target_id_v1": "...",
 "online_eval_arn": "...", "online_eval_id": "...",
 "online_evaluators": ["Builtin.InstructionFollowing", "Builtin.Refusal"]}
```

## Data flow

```
UI chips ──online_evaluators──▶ router (normalize → 400 on bad input)
                                     │
                                     ▼
                        act_gateway(exp_id, progress, evaluators)
                                     │
                        stage_gateway(..., evaluators)
                                     │
              create_online_eval_idempotent(..., evaluators=ids)
                                     │
                       CreateOnlineEvaluationConfig(evaluators=[{evaluatorId}, …])
                                     │
                        artifact["gateway"]["online_evaluators"] = ids
```

`stage_abtest` already binds the config by ARN (`evaluationConfig.onlineEvaluationConfigArn`),
so nothing downstream needs to know the evaluator set. `normalize_ab_results` keys on
whatever `evaluatorId`s AWS returns, and the verdict card renders one row per metric —
both already generalize past two evaluators (verified by reading
`app/evaluation/agentcore_eval.py::normalize_ab_results` and the verdict card).

## Tradeoffs

- **Validate in the router, not in the thread.** The other stage actions fail
  asynchronously onto `Experiment.error`, but a typo'd evaluator id should not cost the
  operator a gateway + target round-trip. Cheap synchronous validation, AWS-side errors
  still land on the card.
- **No `ListEvaluators` call for custom ids.** Keeps the request path free of AWS I/O and
  avoids a second failure mode when the preview API drifts. The UI only offers ids it
  just fetched from `/api/eval/evaluators`, so a bad custom id means someone hand-rolled
  the request.
- **Default unchanged** rather than "goal-aligned by default": chapters 09/9.4 and the
  existing tests describe the current pair, and picking evaluators is a per-experiment
  judgement call, not a platform-wide default.
- **Idempotent claim keeps the old set.** Documented, not fixed: updating the config
  in place would need `UpdateOnlineEvaluationConfig` (replace semantics, another preview
  surface) for a case that only appears after a mid-stage failure.

## Compatibility & rollout

- Additive request field, additive artifact key, unchanged default → no migration, no
  data backfill; experiments created before this change render exactly as before
  (`online_evaluators` absent → the card falls back to showing nothing extra).
- `make verify` is the gate. The AWS-touching rerun is manual (`e2e`-class work, not part
  of the gate).
- Rollback = revert the commit; nothing persisted depends on the new field.
