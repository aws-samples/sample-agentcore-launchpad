# Judge calibration (HARD tier, EVAL-TRUST 2.1b) — 2026-10-26T19:49:36+00:00

- code: `841118406c96 +dirty` · pairs: 54 ({'zh': 25, 'en': 29}) · reps: 3 · models: global.zai.glm-5.3, global.moonshotai.kimi-k3
- labels known by construction (`tests/fixtures/judge_calibration/README.md`); κ bar 0.7

Graded judges (Global defines no pass rule for them — its Evaluate results carry only `value` and `label`): the primary metric is **P(bad < good)**, the share of pairs whose wrong side scores strictly lower; κ is shown at every cut point (`κ@cut` = calls with score ≥ cut as positive). The κ / false-pass / false-fail columns use the PRODUCT pass bar (`evaluators_local.pass_threshold`, N17-obs425: the rule the console and run summaries apply; `pass bar` column); `lenient` is the earlier calibration bar where it differs (Faithfulness ≥ 0.75, Helpfulness ≥ 0.667).

| evaluator | model | items | P(bad<good) | ties | anchor hit (≤ max) | κ@cuts | pass bar | κ | κ ≥ 0.7 | false pass | false fail | lenient: bar · κ · false pass | rerun flips | rerun std | verbosity Δverdict | position Δverdict | zh/en twins agree | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Builtin.Coherence | global.moonshotai.kimi-k3 | 11 | 100 % | 0 % | – | 1.0: 1.0, 0.75: 1.0, 0.5: 1.0, 0.25: 0.0 | 1.0 | 1.0 | ✓ | 0 % | 0 % | 0.7 · 1.0 · 0 % | 0 % | 0.0 | –/– | –/– | – | 0 |
| Builtin.Coherence | global.zai.glm-5.3 | 11 | 100 % | 0 % | – | 1.0: 1.0, 0.75: 0.7925, 0.5: 0.7925, 0.25: 0.0 | 1.0 | 1.0 | ✓ | 0 % | 0 % | 0.7 · 0.7925 · 25 % | 0 % | 0.0214 | –/– | –/– | – | 0 |
| Builtin.Conciseness | global.moonshotai.kimi-k3 | 6 | 100 % | 0 % | – | 1.0: 1.0, 0.5: 1.0 | 0.7 | 1.0 | ✓ | 0 % | 0 % | – | 0 % | 0.0 | –/– | –/– | – | 0 |
| Builtin.Conciseness | global.zai.glm-5.3 | 6 | 100 % | 0 % | – | 1.0: 1.0, 0.5: 1.0 | 0.7 | 1.0 | ✓ | 0 % | 0 % | – | 0 % | 0.0 | –/– | –/– | – | 0 |
| Builtin.Correctness | global.moonshotai.kimi-k3 | 58 | 100 % | 0 % | – | 1.0: 1.0, 0.5: 0.2081 | 1.0 | 1.0 | ✓ | 0 % | 0 % | – | 0 % | 0.0041 | –/– | –/– | – | 0 |
| Builtin.Correctness | global.zai.glm-5.3 | 58 | 100 % | 0 % | – | 1.0: 1.0, 0.5: 0.1671 | 1.0 | 1.0 | ✓ | 0 % | 0 % | – | 0 % | 0.0 | –/– | –/– | – | 0 |
| Builtin.Faithfulness | global.moonshotai.kimi-k3 | 70 | 100 % | 0 % | – | 1.0: 1.0, 0.75: 0.5205, 0.5: 0.2331, 0.25: 0.0338 | 1.0 | 1.0 | ✓ | 0 % | 0 % | 0.75 · 0.5205 · 50 % | 0 % | 0.0152 | –/– | –/– | – | 0 |
| Builtin.Faithfulness | global.zai.glm-5.3 | 70 | 100 % | 0 % | – | 1.0: 1.0, 0.75: 0.4578, 0.5: 0.1674, 0.25: 0.0338 | 1.0 | 1.0 | ✓ | 0 % | 0 % | 0.75 · 0.4578 · 56 % | 0 % | 0.0051 | –/– | –/– | – | 0 |
| Builtin.GoalSuccessRate | global.moonshotai.kimi-k3 | 8 | – | – | – | – | 1.0 | 1.0 | ✓ | 0 % | 0 % | – | 0 % | 0.0 | –/– | –/– | – | 0 |
| Builtin.GoalSuccessRate | global.zai.glm-5.3 | 8 | – | – | – | – | 1.0 | 1.0 | ✓ | 0 % | 0 % | – | 0 % | 0.0 | –/– | –/– | – | 0 |
| Builtin.Helpfulness | global.moonshotai.kimi-k3 | 10 | 100 % | 0 % | – | 1.0: 0.0, 0.833: 1.0, 0.667: 0.6, 0.5: 0.4 | 0.7 | 1.0 | ✓ | 0 % | 0 % | 0.667 · 0.6 · 40 % | 0 % | 0.0315 | –/– | –/– | – | 0 |
| Builtin.Helpfulness | global.zai.glm-5.3 | 10 | 100 % | 0 % | – | 1.0: 0.0, 0.833: 1.0, 0.667: 0.8, 0.5: 0.4 | 0.7 | 1.0 | ✓ | 0 % | 0 % | 0.667 · 0.8 · 20 % | 0 % | 0.0157 | –/– | –/– | – | 0 |

## By defect (first rep)

| evaluator | model | defect | items | false pass | false fail |
|---|---|---|---|---|---|
| Builtin.Coherence | global.moonshotai.kimi-k3 | control | 3 | 0 | 0 |
| Builtin.Coherence | global.moonshotai.kimi-k3 | distant_coherence | 8 | 0 | 0 |
| Builtin.Coherence | global.zai.glm-5.3 | control | 3 | 0 | 0 |
| Builtin.Coherence | global.zai.glm-5.3 | distant_coherence | 8 | 0 | 0 |
| Builtin.Conciseness | global.moonshotai.kimi-k3 | subtle_quality | 6 | 0 | 0 |
| Builtin.Conciseness | global.zai.glm-5.3 | subtle_quality | 6 | 0 | 0 |
| Builtin.Correctness | global.moonshotai.kimi-k3 | combine_tools | 12 | 0 | 0 |
| Builtin.Correctness | global.moonshotai.kimi-k3 | control | 6 | 0 | 0 |
| Builtin.Correctness | global.moonshotai.kimi-k3 | long_multi_fact | 16 | 0 | 0 |
| Builtin.Correctness | global.moonshotai.kimi-k3 | unit_rounding | 12 | 0 | 0 |
| Builtin.Correctness | global.moonshotai.kimi-k3 | wrong_row_or_stale | 12 | 0 | 0 |
| Builtin.Correctness | global.zai.glm-5.3 | combine_tools | 12 | 0 | 0 |
| Builtin.Correctness | global.zai.glm-5.3 | control | 6 | 0 | 0 |
| Builtin.Correctness | global.zai.glm-5.3 | long_multi_fact | 16 | 0 | 0 |
| Builtin.Correctness | global.zai.glm-5.3 | unit_rounding | 12 | 0 | 0 |
| Builtin.Correctness | global.zai.glm-5.3 | wrong_row_or_stale | 12 | 0 | 0 |
| Builtin.Faithfulness | global.moonshotai.kimi-k3 | combine_tools | 12 | 0 | 0 |
| Builtin.Faithfulness | global.moonshotai.kimi-k3 | control | 6 | 0 | 0 |
| Builtin.Faithfulness | global.moonshotai.kimi-k3 | long_multi_fact | 16 | 0 | 0 |
| Builtin.Faithfulness | global.moonshotai.kimi-k3 | partial_support | 12 | 0 | 0 |
| Builtin.Faithfulness | global.moonshotai.kimi-k3 | unit_rounding | 12 | 0 | 0 |
| Builtin.Faithfulness | global.moonshotai.kimi-k3 | wrong_row_or_stale | 12 | 0 | 0 |
| Builtin.Faithfulness | global.zai.glm-5.3 | combine_tools | 12 | 0 | 0 |
| Builtin.Faithfulness | global.zai.glm-5.3 | control | 6 | 0 | 0 |
| Builtin.Faithfulness | global.zai.glm-5.3 | long_multi_fact | 16 | 0 | 0 |
| Builtin.Faithfulness | global.zai.glm-5.3 | partial_support | 12 | 0 | 0 |
| Builtin.Faithfulness | global.zai.glm-5.3 | unit_rounding | 12 | 0 | 0 |
| Builtin.Faithfulness | global.zai.glm-5.3 | wrong_row_or_stale | 12 | 0 | 0 |
| Builtin.GoalSuccessRate | global.moonshotai.kimi-k3 | near_miss_refusal | 8 | 0 | 0 |
| Builtin.GoalSuccessRate | global.zai.glm-5.3 | near_miss_refusal | 8 | 0 | 0 |
| Builtin.Helpfulness | global.moonshotai.kimi-k3 | near_miss_refusal | 4 | 0 | 0 |
| Builtin.Helpfulness | global.moonshotai.kimi-k3 | subtle_quality | 6 | 0 | 0 |
| Builtin.Helpfulness | global.zai.glm-5.3 | near_miss_refusal | 4 | 0 | 0 |
| Builtin.Helpfulness | global.zai.glm-5.3 | subtle_quality | 6 | 0 | 0 |

## By difficulty type (every rep, base variant)

| type | evaluator | model | good / bad items | false pass | false fail | κ | P(bad<good) | gap | false pass (lenient) |
|---|---|---|---|---|---|---|---|---|---|
| combine_tools | Builtin.Correctness | global.moonshotai.kimi-k3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.5833 | – |
| combine_tools | Builtin.Correctness | global.zai.glm-5.3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.5833 | – |
| combine_tools | Builtin.Faithfulness | global.moonshotai.kimi-k3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.5139 | 17 % |
| combine_tools | Builtin.Faithfulness | global.zai.glm-5.3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.5139 | 17 % |
| control | Builtin.Coherence | global.moonshotai.kimi-k3 | 9 / 0 | – | 0 % | – | – | – | – |
| control | Builtin.Coherence | global.zai.glm-5.3 | 9 / 0 | – | 0 % | – | – | – | – |
| control | Builtin.Correctness | global.moonshotai.kimi-k3 | 18 / 0 | – | 0 % | – | – | – | – |
| control | Builtin.Correctness | global.zai.glm-5.3 | 18 / 0 | – | 0 % | – | – | – | – |
| control | Builtin.Faithfulness | global.moonshotai.kimi-k3 | 18 / 0 | – | 0 % | – | – | – | – |
| control | Builtin.Faithfulness | global.zai.glm-5.3 | 18 / 0 | – | 0 % | – | – | – | – |
| distant_coherence | Builtin.Coherence | global.moonshotai.kimi-k3 | 12 / 12 | 0 % | 0 % | 1.0 | 100 % | 0.75 | 0 % |
| distant_coherence | Builtin.Coherence | global.zai.glm-5.3 | 12 / 12 | 0 % | 0 % | 1.0 | 100 % | 0.7083 | 8 % |
| long_multi_fact | Builtin.Correctness | global.moonshotai.kimi-k3 | 24 / 24 | 0 % | 0 % | 1.0 | 100 % | 0.5 | – |
| long_multi_fact | Builtin.Correctness | global.zai.glm-5.3 | 24 / 24 | 0 % | 0 % | 1.0 | 100 % | 0.5 | – |
| long_multi_fact | Builtin.Faithfulness | global.moonshotai.kimi-k3 | 24 / 24 | 0 % | 0 % | 1.0 | 100 % | 0.4271 | 46 % |
| long_multi_fact | Builtin.Faithfulness | global.zai.glm-5.3 | 24 / 24 | 0 % | 0 % | 1.0 | 100 % | 0.3542 | 58 % |
| near_miss_refusal | Builtin.GoalSuccessRate | global.moonshotai.kimi-k3 | 12 / 12 | 0 % | 0 % | 1.0 | – | – | – |
| near_miss_refusal | Builtin.GoalSuccessRate | global.zai.glm-5.3 | 12 / 12 | 0 % | 0 % | 1.0 | – | – | – |
| near_miss_refusal | Builtin.Helpfulness | global.moonshotai.kimi-k3 | 6 / 6 | 0 % | 0 % | 1.0 | 100 % | 0.2773 | 33 % |
| near_miss_refusal | Builtin.Helpfulness | global.zai.glm-5.3 | 6 / 6 | 0 % | 0 % | 1.0 | 100 % | 0.333 | 0 % |
| partial_support | Builtin.Faithfulness | global.moonshotai.kimi-k3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.25 | 100 % |
| partial_support | Builtin.Faithfulness | global.zai.glm-5.3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.25 | 100 % |
| subtle_quality | Builtin.Conciseness | global.moonshotai.kimi-k3 | 9 / 9 | 0 % | 0 % | 1.0 | 100 % | 1.0 | – |
| subtle_quality | Builtin.Conciseness | global.zai.glm-5.3 | 9 / 9 | 0 % | 0 % | 1.0 | 100 % | 1.0 | – |
| subtle_quality | Builtin.Helpfulness | global.moonshotai.kimi-k3 | 9 / 9 | 0 % | 0 % | 1.0 | 100 % | 0.4629 | 11 % |
| subtle_quality | Builtin.Helpfulness | global.zai.glm-5.3 | 9 / 9 | 0 % | 0 % | 1.0 | 100 % | 0.3887 | 11 % |
| unit_rounding | Builtin.Correctness | global.moonshotai.kimi-k3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.5 | – |
| unit_rounding | Builtin.Correctness | global.zai.glm-5.3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.5 | – |
| unit_rounding | Builtin.Faithfulness | global.moonshotai.kimi-k3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.375 | 61 % |
| unit_rounding | Builtin.Faithfulness | global.zai.glm-5.3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.2917 | 83 % |
| wrong_row_or_stale | Builtin.Correctness | global.moonshotai.kimi-k3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.7778 | – |
| wrong_row_or_stale | Builtin.Correctness | global.zai.glm-5.3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.75 | – |
| wrong_row_or_stale | Builtin.Faithfulness | global.moonshotai.kimi-k3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.625 | 17 % |
| wrong_row_or_stale | Builtin.Faithfulness | global.zai.glm-5.3 | 18 / 18 | 0 % | 0 % | 1.0 | 100 % | 0.5833 | 33 % |

## Cross-judge agreement (first rep)

| evaluator | A | B | items | agree | κ | disagreements |
|---|---|---|---|---|---|---|
| Builtin.Coherence | global.moonshotai.kimi-k3 | global.zai.glm-5.3 | 11 | 100 % | 1.0 |  |
| Builtin.Conciseness | global.moonshotai.kimi-k3 | global.zai.glm-5.3 | 6 | 100 % | 1.0 |  |
| Builtin.Correctness | global.moonshotai.kimi-k3 | global.zai.glm-5.3 | 58 | 100 % | 1.0 |  |
| Builtin.Faithfulness | global.moonshotai.kimi-k3 | global.zai.glm-5.3 | 70 | 100 % | 1.0 |  |
| Builtin.GoalSuccessRate | global.moonshotai.kimi-k3 | global.zai.glm-5.3 | 8 | 100 % | 1.0 |  |
| Builtin.Helpfulness | global.moonshotai.kimi-k3 | global.zai.glm-5.3 | 10 | 100 % | 1.0 |  |
