# ResponseRelevance on real turns: are the "Generally Yes" turns off topic? (2026-10-25)

Input to the ResponseRelevance pass-bar decision, the same method as
`coherence-real-turns-20261025.md`. On the easy calibration tier (8 pairs + 3 controls, r17-bh9)
a 1.0 bar costs nothing: 0 false fails on both models. This record asks whether the real turns
our judge rates "Generally Yes" (0.75, which would fail at 1.0) really contain off-topic content.

## Method

- **Source runs:** `make eval-diff` (eval-wb workbench), ResponseRelevance only, with the
  current (library) template, run 2026-10-25 on cn/r17-bh9. The judges are GLM 5.3 and Kimi K3.
  - research-17: 44 units, 2 runs per model.
  - fresh probe-35 (`eval-round2-close-20261024/raw/results.jsonl`): 73 units, 1 run per model.
  - Reports: `/tmp/r17bh3/rrel/{r17-glm-1,r17-glm-2,r17-kimi-1,r17-kimi-2,fresh-glm,fresh-kimi}`.
- **Items:** every OUR-judge ResponseRelevance record labelled "Generally Yes": 50 judgements on
  21 distinct turns. Every other record is Completely Yes; no lower label occurs.
- **Reading:** each judge explanation was read against its turn, and each turn put in one class:
  - A: content unrelated to the question that a user would notice;
  - B: related but unrequested supplementary content, or a boilerplate closing offer;
  - C: the judge is wrong (the content is relevant, or the issue is a different criterion).

## Result

| class | turns | judgements |
|---|---|---|
| A — unrelated content a user would notice | 9 | 23 (46 %) |
| B — related extra or a boilerplate closer | 10 | 25 (50 %) |
| C — judge wrong | 2 | 2 (4 %) |

Five of the nine A turns (18 of the 23 judgements) come from one research session, a696. Its
agent closes HR answers (leave balance, leave policy, payroll date, "remember my ID") with an
offer of energy-storage station services: a real, user-visible defect of that agent. The other
four A turns are an unrequested city fetch, an unrequested second-trip calculation, an
unprompted pending item in a greeting, and an invented open item in a session summary.

Pass rates on the same runs (OUR judge and Global, rep 0):

| set | ours ≥ 0.7 → ≥ 1.0 | Global ≥ 0.7 → ≥ 1.0 |
|---|---|---|
| research-17 | 44/44 → 34/44 | 44/44 → 32/44 |
| fresh probe-35 | 73/73 → 65–71/73 | 60/62 → 59/62 |

## Examples (source language)

**A**
- a696, "本人休假余额" (zh): a correct leave-balance answer ending with an offer to check the
  power station during the November leave and asking for a station ID. Both models, every rep.
- probe-studio mt3, "Which is warmer?" (en): Shanghai vs Shenzhen answered, plus Beijing's
  weather, fetched although no one asked about Beijing.
- 539d, "出差能报销多少钱？" (zh): the Shanghai answer plus an unsolicited calculation for a separate
  November Chengdu trip.

**B**
- 257f, "成都住宿标准" (zh): the lodging answer plus a short "顺带一提：每日补贴" section — the same
  trip and the same policy document, about 20 % of the reply.
- dops12-mem turn 1 (en): "Noted… blue… Shanghai" followed by "Is there anything HR-related I can
  help you with today?" — a boilerplate closer.
- smoke-kb, "黄金会员退货期多久？" (zh): 30 days plus the restocking fee and refund timing from the same
  passage.

**C**
- skill c864 (en): marked down for an invented identity and for commentary after "return only the
  rendered report". Those are Faithfulness and InstructionFollowing issues; the content is on
  topic.
- stub mt16 (en): a note linking the stock-out to the ORD-1002 queried earlier. The judge itself
  calls it "not unrelated … contextually meaningful" and still docks it.

## Reading for the decision

Fewer than half of the Generally Yes judgements (46 %) are content a user would call off topic,
and most of those come from one agent's persona leak. Half are related extras or a closing
offer, which a 1.0 bar would fail. Unlike Coherence, the B share dominates here.
