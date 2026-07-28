# Selectable online evaluators for config-bundle experiments

## Problem

The config-bundle A/B flow (`Evaluation → ⚗ Experiment`) creates its online evaluation
config with a hard-coded evaluator pair:

```python
# backend/app/optimization/service.py :: create_online_eval_idempotent
evaluators=[
    {"evaluatorId": "Builtin.GoalSuccessRate"},
    {"evaluatorId": "Builtin.Helpfulness"},
]
```

Every experiment is therefore judged on goal success + helpfulness, whatever the
treatment prompt actually changes. The 60-prompt run recorded in
`docs/lab/09-experiment-ab.md` §9.9 made the cost concrete: the treatment prompt
tightened *refusal* and *format following*, but the only two available metrics moved
+0.05 and −0.03 with p ≈ 0.85, so the verdict stayed `significant: false` at n=120. The
experiment could not measure the behavior it was changing.

AWS does not impose this limit: `CreateOnlineEvaluationConfig` accepts **up to 10
evaluators, mixing built-in and custom ones**
(https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-online-evaluations.html).

## Goal

Let the operator choose which evaluators the experiment's online evaluation config uses,
at the `GATEWAY` stage, without changing the behavior of existing experiments.

## Requirements

1. `POST /api/experiments/{id}/action {"action": "gateway"}` accepts an optional
   `online_evaluators: [<evaluatorId>, …]`.
2. Omitting it keeps today's pair (`Builtin.GoalSuccessRate`, `Builtin.Helpfulness`), so
   existing flows, tests and lab chapters stay correct.
3. Accepted ids: any of the 13 general-purpose built-ins (`ALL_BUILTIN_EVALUATORS`) plus
   custom evaluator ids created in `◆ 评估器`.
4. Rejected with 4xx and a stable error code, before any AWS call:
   - the three `Builtin.Trajectory*Match` matchers — they score against dataset ground
     truth, which online evaluation of live traces does not carry;
   - an unknown `Builtin.*` id;
   - an empty list, or more than 10 ids (the AWS cap).
   Duplicates are collapsed, order preserved.
5. The chosen ids are persisted on the `gateway` artifact, so the card and any later
   audit can show what the two arms were judged on.
6. The Experiment page lets the operator toggle evaluators before pressing
   `▸ 创建网关 + 在线评估`, defaults to the two current ones, and echoes the chosen set
   once the stage is done. Trajectory matchers are not offered.
7. All user-facing strings are i18n keys with en + zh-CN parity.

## Non-goals

- Changing the evaluator set of an **already created** config. AWS create is claimed
  idempotently by name, so a retry after a partial failure reuses the existing config and
  its evaluator set. Out of scope, but must be documented.
- Ground truth in online evaluation. A custom judge whose instruction uses
  `{expected_response}` sees it empty in the A/B path; ground-truth judging stays a
  batch-evaluation capability (chapter 08).
- Target-based A/B (`基于目标的 A/B`) and runtime canaries — separate flows, untouched.

## Acceptance Criteria

- [ ] `make verify` passes (backend ruff+pytest, infra, frontend eslint+tsc+build, i18n parity).
- [ ] Omitting `online_evaluators` produces exactly the two ids used today.
- [ ] Passing `["Builtin.InstructionFollowing", "Builtin.Refusal", "Builtin.Helpfulness"]`
      reaches `create_online_evaluation_config` in that order and lands on the `gateway`
      artifact.
- [ ] A trajectory matcher, an unknown builtin, an empty list and an 11-id list are each
      rejected before any AWS call.
- [ ] The Experiment page shows evaluator chips before the gateway stage runs and the
      chosen ids after it, in both locales.
- [ ] A real experiment rerun on `lab-fund-assistant` with the goal-aligned set records
      per-evaluator means, n and p for every chosen evaluator, and
      `docs/lab/09-experiment-ab.md` is updated with the measured numbers (not predictions).
