# Judge self-preference — 2026-10-24T06:16:02+00:00

- code `fce25a8e508b +dirty` · fingerprint `c76c90a4a420b75b` · 71 questions (calibration pairs) · agents global.zai.glm-5.3, global.moonshotai.kimi-k3 · judges global.zai.glm-5.3, global.moonshotai.kimi-k3
- `S j→a` = mean score judge j gives agent a's answers; `pref j` = what judge j adds for its own family; `gap` = the mean of both prefs (a real quality difference cancels, an own-family bias adds up).

| evaluator | questions | S glm→glm | S glm→kimi | S kimi→glm | S kimi→kimi | pref glm | pref kimi | gap | 95 % CI |
|---|---|---|---|---|---|---|---|---|---|
| Builtin.Coherence | 71 | 0.9965 | 0.993 | 1.0 | 1.0 | +0.0035 | +0.0 | +0.0018 | [+0.0, +0.0053] |
| Builtin.Conciseness | 71 | 0.5211 | 0.5352 | 0.5423 | 0.5493 | -0.0141 | +0.007 | -0.0035 | [-0.0176, +0.0106] |
| Builtin.Correctness | 71 | 1.0 | 1.0 | 1.0 | 1.0 | +0.0 | +0.0 | +0.0 | [+0.0, +0.0] |
| Builtin.Faithfulness | 71 | 1.0 | 1.0 | 1.0 | 1.0 | +0.0 | +0.0 | +0.0 | [+0.0, +0.0] |
| Builtin.Helpfulness | 71 | 0.9153 | 0.9224 | 0.9036 | 0.9177 | -0.0071 | +0.0141 | +0.0035 | [-0.0059, +0.0129] |
