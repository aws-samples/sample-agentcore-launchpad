# Coherence on real turns: are the "Generally Yes" turns flawed? (2026-10-25)

Input to the Coherence pass-bar decision. The calibration tiers show a 1.0 bar costs nothing
there: 0 false fails on the easy and hard tiers, both models. On real turns, however, it moves
pass rates down by about 15–30 points. This record asks whether the real turns our judge rates
"Generally Yes" (0.75, which would fail at 1.0) are actually flawed. Decision taken: Coherence
passes at 1.0 (lead, 2026-10-25; `evaluators_local.PASS_THRESHOLDS`, r17-bi9).

## Method

- **Source runs:** `make eval-diff` (eval-wb workbench) with the Round 3 templates (N17-obs404:
  the tool-context Coherence prompt and live guidelines, r17-bh3 `fce25a8e`). The judges are
  GLM 5.3 and Kimi K3.
  - research-17: 44 units, 2 runs per model.
  - fresh probe-35 (`eval-round2-close-20261024/raw/results.jsonl`): 73 units, 1 run per model.
  - Reports: `/tmp/r17bh3/cc4/{r17-glm-1,r17-glm-2,r17-kimi-1,r17-kimi-2,fresh-glm,fresh-kimi}`.
- **Items:** every OUR-judge Coherence record labelled "Generally Yes", all runs and models: 62
  judgements on 29 distinct turns.
- **Reading:** each judge explanation was read against its turn, and each turn put in one class:
  - A: a real contradiction or reasoning slip a user would notice;
  - B: a minor style, ordering or wording nit;
  - C: the judge is wrong (an example or aside treated as a premise, or the wrong criterion).
- No judge was called for this record.

## Result

| class | turns | judgements |
|---|---|---|
| A — real contradiction or reasoning slip | 16 | 39 (63 %) |
| B — minor nit | 10 | 19 (31 %) |
| C — judge wrong | 3 | 4 (6 %) |

A splits in two:
- 8 turns of logic or arithmetic slips: a stats table claiming 10 shots and listing 9; a
  quarterly −7 % stated as the September drop; a leave gap computed from the wrong balance;
  "EMP + four digits" then "EMP-101" as an example; a plan titled "concise" after a request for a
  long one.
- 8 turns where an invented premise drives the conclusion: an ID format, a deadline, a colleague's
  task, an identity.

Pass rates on the same runs (OUR judge and Global, rep 0):

| set | ours ≥ 0.7 → ≥ 1.0 | Global ≥ 0.7 → ≥ 1.0 |
|---|---|---|
| research-17 | 41/44 → 28–30/44 | 37/44 → 24/44 |
| fresh probe-35 | 71/73 → 61–63/73 | 60/62 → 52/62 |

## Examples (source language)

**A**
- 06d7 turn 1 (zh): "员工编号标准格式为 EMP-NNNN（四位），例如 EMP-0001". The tool returned only
  `employee_not_found`; the answer recommends retrying with EMP-0001, which also failed.
- f046 turn 2 (zh): "您还有时间，不用慌… 别错过 11月17日". No trip end date and no 11-17 deadline
  exist in the conversation; the recommendation rests on both.
- attach (zh): "华东和华北在9月出现约7%的回落". The 7 % is the July→September change; the
  September drop is −8.9 % / −17.6 %.

**B**
- b0c5 (zh): the header "今年休假余额（截至 2026-10-15）" carries an unsupported as-of date; the
  balance itself is unaffected.
- b0c5 (zh): "数据库仅包含以下字段：姓名、部门、岗位、假期". It omits location, which nothing depends on.
- glm-maxtokens (en): "Shenzhen is the dampest" — a loose superlative in a travel note.

**C**
- toolerror (zh): "订单号格式一般为 ORD-NNNN", an example inside a correct failure report. Global
  rates it Completely Yes, and our guideline says an example does not lower the rating.
- tooltimeout (en): "I can retry with mode='ok' to get the actual data", a reasonable next step
  judged an unsupported inference.
- preamble (en): marked down for skipping the requested one-sentence plan. That is
  InstructionFollowing, not Coherence.

## Caveat (a696)

5 of the A/B turns (16 judgements) come from research session a696, whose agent cites
"此前对话中记录的信息". If those facts came from long-term memory, which the judge does not see,
some "invented prior conversation" calls there would be class C. Global flagged the same turns.

## Reading for the decision

At 1.0 the B + C share (37 %) fails for a nit or a judge error. Half of A, the invented
premises, overlaps Faithfulness, which already passes only at 1.0. The other half, the logic
slips, plus the easy-tier self-contradiction the 0.7 bar passed (`9 left, 3 pending → 12 remain`,
rated Generally Yes), is what a 1.0 bar catches.
