# E2E evidence — `kb_deep_search` (AgenticRetrieveStream) on zip + container

Run: 2026-07-28, account 434444145045, us-west-2, local backend on :8000.
KB: `lab-fund-kb` / `2MBGUNVMS4` (one indexed PDF, `Morgan_Stanley_Oct_21_(EMEA).pdf`).

## 0. Prerequisites

`cd infra && uv run cdk deploy --require-approval never` (25.8s) — again required,
`make bootstrap` only deploys CDK when the stack is missing. Verified after:

```
Sid ManagedKbAgenticRetrieval · Action bedrock:AgenticRetrieveStream · Resource "*"
```

The local backend also had to be restarted: templates are rendered in-process, so a
running server keeps emitting the old `main.py`.

## 1. API shape probed before writing any code (botocore 1.43.44)

```python
client.agentic_retrieve_stream(
    messages=[{"role": "user", "content": {"text": q}}],   # content is a STRUCT…
    retrievers=[{"description": …, "configuration": {"knowledgeBase": {"knowledgeBaseId": kb}}}],
    agenticRetrieveConfiguration={"foundationModelType": "MANAGED",
                                  "rerankingModelType": "MANAGED", "maxAgentIteration": 3})
```

…**not** a list — the AWS blog example (`content: [{'text': …}]`,
`retrievers: [{'knowledgeBaseRetriever': …}]`) does not match the live model. Required
members: `agenticRetrieveConfiguration`, `messages`, `retrievers`.

`resp["stream"]` members: `traceEvent` (attributes.step/status/failures/warnings),
`responseEvent` (answer deltas — 110 of them in the probe), `result`
(`generatedResponse{answer,citations}` + `results[]{content,metadata,sourceRetriever}`),
plus **nine modeled error members that arrive inside the stream**
(`accessDeniedException`, `validationException`, …). Agentic results carry **no `score`
and no `location`** — the source URI is `metadata["_source_uri"]`, the KB id is
`sourceRetriever.identifier`.

Probe output (single KB, comparison question, maxAgentIteration=3):
`SpeculativeRetrieval IN_PROGRESS→SUCCEEDED`, `Planning IN_PROGRESS→SUCCEEDED`, then
`result` with 10 chunks, a 687-char answer and 3 citations. **No `Retrieval` step
fired** — the planner judged the speculative pass sufficient, so the tool must not
assume any fixed step sequence.

## 2. Deploys

`kb-deep-zip` · `61741526d09e47298269c699eb63a797` · job `573d88c4` — **66s**
```
12:58:15 generate strands template · 15672 bytes · model global.anthropic.claude-sonnet-5
12:58:58 package  pip+zip 42.7s · 37.3MB → s3://…/agents/kb-deep-zip/deployment_package.zip
12:58:58 deploy   CreateAgentRuntime accepted · runtimeId kb_deep_zip_eeaaea-ly9FpCDNz2
12:59:18 deploy   runtime status: READY
12:59:21 register a2a record created · W8d147CLLI91
```
Template size progression, same agent shape: **6333 B** (no KB) → **9770 B**
(`kb_search`) → **15672 B** (both tools).

`kb-deep-container` · `73ffd006852b45aab73058c7fcb89db6` · job `030b14ee` — **114s**
(CodeBuild 1.7m), runtime `kb_deep_container_9474ad-fuanan4EdE`.

## 3. Comparison question → both agents chose `kb_deep_search`

Prompt: 「对比 Emerging Markets Leaders 策略与 Global Emerging Markets 策略：各自的资产
规模是多少，投资流程/组合构建规则上有什么不同？请引用来源。」

Both answers were not only grounded but **caught an ambiguity in the source document**
that a single-shot retrieval had missed in the previous task's run: "Global Emerging
Markets" is used both as a sub-strategy ($7,246 MM) and as the umbrella for four
sub-strategies ($10,706 MM = 7,246 + 333 + 2,339 + 788), and both agents flagged the two
different denominators explicitly. They also pulled the funnel detail
(10,000 → 300-400 → ~100 → 25-40 holdings, 28 actual), Active Share 89.63%, turnover
17.86%, ROIC > 15% — evidence spread across several pages.

| | zip (`buffered`) | container (`stream`) |
|---|---|---|
| latency | 59.9s | 173.6s |
| tools called | `kb_deep_search` ×1, `kb_search` ×2 | `kb_deep_search` ×3, `kb_search` ×7 |

The zip agent's SSE carries no tool frames in buffered mode — the tool calls are visible
in the trace, not the stream.

## 4. Trace proof

zip · `6a68a7ed0c2e05622478d56c47ca9d17`
```
invoke_agent Strands Agents                             53809.2
  execute_event_loop_cycle → chat …                      2664.7
  execute_tool kb_deep_search                           13395.8   ← gen_ai.tool.name
    Bedrock Agent Runtime.AgenticRetrieveStream           160.0
  execute_tool kb_search ×2                               ~860 each
    Bedrock Agent Runtime.Retrieve ×2                     ~850 each
  execute_event_loop_cycle → chat …                     29601.8   (final synthesis)
```

container · `6a68a82916ca238332a14b834f0bb1c0`: 3× `AgenticRetrieveStream` +
7× `Retrieve` boto3 spans, and `execute_tool kb_deep_search` ×3 / `kb_search` ×7.

**Gotcha worth remembering:** the auto-instrumented
`Bedrock Agent Runtime.AgenticRetrieveStream` span is only **160 ms** — it covers the
initial API call, not the event-stream consumption that follows. The real cost is on the
enclosing `execute_tool kb_deep_search` span (13.4 s). Do not read the boto3 span as the
retrieval latency.

## 5. Steering check — the cheap tool still wins for cheap questions

Prompt: 「这只基金截至 2021 年 8 月 31 日持有多少只股票？」 (single fact)
→ container called **`kb_search` only** (no `kb_deep_search`), 29.3 s, answered
「28只股票」with the Portfolio Characteristics table as the source. So the prompt-section
guidance actually routes, rather than the model always reaching for the expensive tool.

## 6. Cleanup

Both probe agents deleted via `DELETE /api/agents/{id}`.
