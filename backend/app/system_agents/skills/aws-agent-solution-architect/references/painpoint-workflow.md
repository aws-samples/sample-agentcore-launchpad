# Live pain points → metrics → golden test set

This step is the mandatory gate before formal technical design. It is not skipped when
the first two rounds were complete. Every new conversation asks about current state and
pain points again; pain points from earlier conversations, old designs or old golden
tests are not confirmed for this round and may only be used afterwards to point out
differences. The goal is not a vague "what problems do you have" but converting failures
the customer re-confirmed in this conversation into verifiable acceptance criteria.

## A. Current state

Ask, as a numbered list with a free-text option:
- In production — real users, logs and operations feedback
- Piloting — a small user group or PoC data
- Legacy system only — no agent yet, but manual / search / process pain points
- Not built yet — greenfield, no real run data
- Not sure yet — build the baseline from industry assumptions

## B. Pain-point collection

If an agent, a pilot or a legacy system exists, continue with a multi-select list:
- Knowledge and retrieval — not found, wrong citation, stale content, inconsistent answers
- Answer quality — incomplete, factual errors, hallucination, wrong tone or policy boundary
- Tools and process — wrong tool, wrong parameters, API failures, duplicate execution, inconsistent state
- Permissions and compliance — over-reach, data isolation, PII, audit or missing human approval
- Performance and stability — slow responses, timeouts, peak failures, unavailable dependencies
- Cost and operations — high token or infrastructure cost, manual repetition, no monitoring or regression

Remind the customer to add one to three concrete failure cases in free text. Without
concrete cases, "no cases, use industry assumptions" is an acceptable answer, but the
step is never skipped silently.

Then ask for existing evidence (multi-select): production traces / logs, tickets or
support records, user feedback, monitoring metrics, staff experience, no data yet. The
evidence source decides how much the metric baseline can be trusted.

## C. Mandatory conversion table

Output a table for confirmation. Every pain point carries at least:

| Field | Requirement |
|---|---|
| Original pain point / failure case | Keep the customer's meaning; do not enlarge it |
| Risk dimension | One or more of cognition, quality, accountability / compliance, cost, performance |
| Root-cause hypothesis | Marked as pending validation; a guess is never written as a fact |
| Quantifiable metric | Name plus numerator / denominator or measurement rule |
| Suggested threshold | Marked "suggested initial value" with its rationale, pending baseline calibration |
| Golden tests | 2–3 per pain point covering normal, boundary / failure, refusal / escalation or recovery |
| Expected trajectory | Whether to retrieve, which tool, key parameters, whether to confirm / escalate |
| Forbidden behavior | Hallucination, over-reach, duplicate writes, unsupported answers |
| Observation and response | Trace / span / log signal, alert and owner |

Golden tests use structured fields: `id`, `input`, `preconditions`, `expected_tools`,
`expected_evidence`, `expected_response`, `forbidden_behavior`, `pass_criteria`,
`source`, `evaluator_ids`, `evaluation_level`. `source` is `customer_pain_point` or
`industry_assumption` only; `evaluation_level` contains only `session`, `trace`, `span`.
Every test binds at least one evaluator; high-risk fact, authorization, parameter, write
and idempotency tests bind a code-based evaluator and never only an LLM-as-a-judge.

## D. Visibility and confirmation gate

After the table is generated, expand in the normal reply body, in this order:

1. the design-contract summary;
2. the complete conversion table;
3. every golden test with all structured fields, listed by `id`;
4. the source labels separating customer facts from industry assumptions.

This is deliverable content, not working notes. Never hide it in reasoning, tool output,
collapsed sections or attachments; never write "the nine tests above" without expanding
each one. The customer must be able to review the whole data set without opening
anything.

End the reply with the confirmation options as the last lines:
1. Accurate — proceed to technical design
2. Mostly accurate — I want to add or change items
3. Pain points are missing — I want to add more
4. No real pain points yet — proceed with industry assumptions

Only a confirmation, or an explicit choice of industry assumptions, opens the AWS
architecture and the formal document. Without a reply, never skip ahead to a "final
design".

## E. When there is no live agent

A greenfield project completes this step too: from the industry, capability scope and
risk level, generate 3–5 common industry pain-point assumptions with golden tests, all
marked `industry_assumption`. They are not customer facts; the first implementation week
replaces or calibrates them with interviews, historical tickets or pilot traces.
