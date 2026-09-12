# Formal deliverable template

The deliverable is a structured Markdown design document written in the reply. No
office documents, image files, editable diagram sources or download links are produced;
diagrams are Mermaid blocks plus a prose description.

## Document structure

0. Executive summary and ADLC stage statement
1. Requirement understanding, scope and assumptions pending validation
2. Design contract: responsibilities, boundaries, tools, knowledge sources, success criteria
3. Current baseline, pain points and golden test mapping
4. Architecture overview and end-to-end flow
5. AWS service and model selection (with alternatives compared)
6. Agent, tool, knowledge, memory and data design
7. Evaluation, reliability and quality gates
8. Security, privacy, compliance and governance
9. Observability, operations, incident handling and continuous improvement
10. Capacity and cost estimate
11. Implementation roadmap, RACI, risks and mitigations
12. Appendix: full golden test set, assumptions, official sources, glossary

Section 3 separates `customer_pain_point` from `industry_assumption` and shows pain
points, metrics, threshold rationale and the matching golden tests. The appendix renders
the full golden test set as a **table** — one row per test with `id`, `input`,
`expected_tools`, evidence + response, `forbidden_behavior`, `pass_criteria`,
`evaluator_ids` + `evaluation_level` as columns. Section 7 contains two separate tables:

1. **Recommended evaluator registry** — evaluator id / name, code-based / LLM-as-a-judge
   / human type, session / trace / span level, input signal, scoring method, threshold,
   AWS implementation, owner and failure response;
2. **Golden test → evaluator mapping** — per test the mandatory evaluators, blocking
   condition and owner. High-risk facts, authorization, tool parameters, write
   operations, idempotency and cost never rely on a judge alone; a code-based evaluator
   is mandatory. Built-in AWS evaluator names are verified from official material when
   writing; if unverifiable, mark the evaluator as custom.

Every section opens with a one-sentence conclusion; the body prefers decision tables,
data flows and acceptance criteria over generic introductions.

## Architecture description

- Main flow left to right along the request path.
- Mark at least: users / channels, identity boundary, entry point, orchestration /
  runtime, model, knowledge and data, tools / external systems, asynchronous components,
  monitoring and evaluation, security services.
- Distinguish synchronous, asynchronous and error / degradation paths.
- Use official AWS service names; never describe an icon set or rendering you did not
  produce.

## Cost expression

- State Region, currency, pricing date and workload assumptions.
- Break down model inference, runtime, storage / retrieval, network, evaluation, logging
  and human operations.
- Give low / baseline / high tiers, the largest cost drivers and the levers to reduce
  them.
- Prices not verified through an official source are not written as precise amounts;
  give the formula and the open item instead.

## Pre-delivery gate

- [ ] Scope, non-goals and assumptions pending validation are clear
- [ ] Every key choice has a compared alternative and a verification method
- [ ] Current AWS capabilities, models, Regions and prices carry a source and a verification date
- [ ] Security covers least privilege, encryption, secrets, audit, data boundaries, prompt injection and tool misuse
- [ ] Evaluation covers session / trace / span, the three evidence layers, the golden set, offline gates and online drift
- [ ] Metrics have a measurement rule, data source, threshold rationale, frequency, owner and response
- [ ] High-risk actions have confirmation, idempotency, rollback, human escalation or a circuit breaker
- [ ] Cost assumptions can be recomputed; unverified numbers are labelled
- [ ] The document has no placeholders, pseudo-citations or links to files that were never produced
- [ ] Every statement is labelled verified AWS fact, methodology, customer input or assumption
