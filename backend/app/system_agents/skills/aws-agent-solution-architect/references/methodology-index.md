# Enterprise production-agent guide — methodology index

Source: the enterprise guide to developing and deploying production-grade agents (56
pages). Page numbers below are PDF pages. Cite as "the guide, page X / section", never as
current AWS product documentation.

The full text is available only when an administrator mounted a knowledge base holding
it on this agent. With a retrieval tool present, read and cite the original passage. With
no retrieval tool, work from this index and say so explicitly; never claim original-text
verification you did not perform.

## 1. Evaluation-first and the ADLC

- p. 4: the enterprise bottleneck is rarely model capability; it is the missing
  engineering system that continuously measures "good".
- p. 7: why traditional QA fails for agents — non-determinism, prompts behave like source
  code but resist static analysis, implicit dependencies such as the model can drift.
- pp. 8–9: the agent development life cycle (ADLC) is a continuous flywheel, not a
  one-shot pipeline: define "good" → build → evaluate → quality gate / release → observe
  in production → mine failures and feed them back. Production is the most valuable data
  input.
- p. 21: evaluation plays four roles at once — specification, quality gate, production
  monitoring, improvement driver.

## 2. Architecture must be evaluable

- p. 12: rerun evaluation on every prompt, tool or model change; example metrics include
  tool selection, parameter extraction, refusal accuracy, answer quality, latency and
  token usage.
- p. 14: establish a single-agent quality baseline before scaling to multi-agent; scaling
  across the organization needs a unified tool catalogue, observability and evaluation
  standards.
- p. 16: a tool definition includes name, parameters, return format, error conditions
  and when to use / not use it; description quality beats tool count.
- p. 18: give deterministic problems to code; reserve agentic behavior for reasoning and
  natural-language understanding. Split an agent whose responsibilities grow too wide.

## 3. Two-pillar evaluation framework

- pp. 24–27:
  - three granularities — black box (final response), glass box (full trajectory), white
    box (single step / span);
  - three evidence layers — layer 1 mechanically verifiable; layer 2 semi-objective
    semantic scoring under a fixed judge; layer 3 subjective items refused by default;
  - three scorer types — code rules, calibrated LLM-as-a-judge, humans. Programmatically
    verifiable items go to code; subject-matter experts define rubrics and golden sets.
- p. 28: eight dimensions — goal completion; tool / action correctness; safety and PII;
  cost and latency; faithfulness / grounding; policy and compliance; brand tone;
  appropriateness of escalation to a human.
- pp. 28–29: distinguish `pass@k` (at least one success in k runs) from `pass^k` (all k
  runs succeed); capability evaluation climbs, regression evaluation guards; monitor drift
  between the development baseline and production reliability.

## 4. Data sets and judges

- pp. 30–31: the golden set is evaluation intellectual property. Center it on real
  production data and expert annotation, keep a holdout; start from about 20
  representative cases, read traces and cluster failure modes before defining rubrics;
  grow to 100, then 500+.
- p. 31: LLM-as-a-judge shows position, verbosity and authority bias; mitigate with
  bidirectional scoring, multiple judge models and human calibration. An uncalibrated
  judge is not objective truth.
- pp. 32–33: agent-based evaluation reads whole trajectories, calls tools to verify and
  performs process-level root-cause analysis; suited to deep evaluation in development
  and pre-production; slower and costlier, and itself needs calibration; never a
  replacement for the human gold standard.
- pp. 34–35: best practices — tracing before scoring, offline and online tracks, golden
  sets that keep growing from production, different report views for different
  audiences.

## 5. AWS engineering mapping

The following describes product state at the time the guide was written and must be
re-verified against current official AWS documentation before use:

- pp. 38–40: a three-layer evaluation library — foundation model, agent components,
  end-to-end; pick metrics per agent shape instead of enabling everything.
- p. 41: AgentCore Evaluations built-in and custom evaluator examples; custom evaluators
  split into LLM-as-a-judge and code-based.
- pp. 42–43: the trace-driven four steps — define trace / span inputs → run evaluators →
  distribute results → audit and respond. Observability signals include tokens, P50 / P95
  latency, error rate and tool-call patterns; OpenTelemetry keeps the stack
  framework-agnostic.
- pp. 44–46: evaluation covers quality, performance, accountability and cost and links to
  business KPIs; the online, on-demand and batch modes described there need their current
  GA / preview status re-verified.
- p. 46: observability → evaluation → optimization forms a loop; the status of
  optimization, configuration bundles and A/B testing may have changed — check current
  official documentation.

## 6. Citation discipline

- The guide supplies methodology; it never proves current Region support, latest models,
  prices, quotas or GA / preview status.
- Technical facts cite current official AWS documentation with the query date.
- The five-dimension pain-point classification is this skill's design heuristic, not a
  position of the guide.
- Example thresholds in the guide are references only; final thresholds come from the
  customer baseline, risk level and business SLA.
