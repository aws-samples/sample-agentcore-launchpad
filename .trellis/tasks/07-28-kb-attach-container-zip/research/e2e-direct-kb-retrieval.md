# E2E evidence — direct-Retrieve KB mounting on zip_runtime + container

Run: 2026-07-28, account 434444145045, us-west-2, local backend on :8000.
KB: `lab-fund-kb` / `2MBGUNVMS4` (MANAGED, 1 data source, `Morgan_Stanley_Oct_21_(EMEA).pdf`).

## 0. IAM prerequisite — `make bootstrap` is NOT enough

`scripts/bootstrap.py` only runs `cdk deploy` when the stack is **missing**
(`deploy_cdk()` is guarded by a stack-exists check), so on an already-bootstrapped
account the new `ManagedKbRetrieval` statement does not land. It required:

```
cd infra && uv run cdk deploy --require-approval never    # 33s
```

Verified after:

```
aws iam get-role-policy --role-name launchpad-agent-execution-role \
  --policy-name AgentExecutionRoleDefaultPolicyF9CB1199 \
  --query 'PolicyDocument.Statement[?Sid==`ManagedKbRetrieval`]'
→ Action ["bedrock:GetKnowledgeBase","bedrock:Retrieve"]
  Resource arn:aws:bedrock:us-west-2:434444145045:knowledge-base/*
```

This is now documented in the lab troubleshooting table.

## 1. Deploys

Both created with the same one-KB `knowledge_bases` payload.

`kb-direct-zip` · `49a36238a4b441c5a4911af5ba06af00` · job `27c7407c` — **64s**
```
11:16:41 generate strands template · 9770 bytes · model global.anthropic.claude-sonnet-5
11:17:24 package  pip+zip 42.8s · 37.3MB → s3://…/agents/kb-direct-zip/deployment_package.zip
11:17:24 provision iam role reused · launchpad-base
11:17:25 deploy   CreateAgentRuntime accepted · runtimeId kb_direct_zip_740661-4hBZL3C9h2
11:17:45 deploy   runtime status: READY
11:17:48 register a2a record created · JzAKNE5K5q3v · auto-submitted
```
Note `generate` is 9770 bytes vs 6333 for a KB-less agent — the KB tool block.
No KB-gateway work in `provision` (that line is harness-only), as designed.

`kb-direct-container` · `98fee1d3fc284cc8a10d9de369f06dc6` · job `51a8b2d9` — **124s**
```
11:16:47 generate build context assembled: Dockerfile, README.md, buildspec.yml, main.py, requirements.txt, tracing.py
11:16:47 package  codebuild started · launchpad-agent-builder:83bef5d8…
11:18:38 package  codebuild · arm64 · 1.8m → :kb-direct-container-v1
11:18:39 deploy   CreateAgentRuntime accepted · runtimeId kb_direct_container_b4d6de-pimPtcAZ8k
11:18:49 deploy   runtime status: READY
11:18:51 register a2a record created · YCCSCJ9EmNXz · auto-submitted
```

## 2. Invoke — same question to both

Prompt: 「这只基金所属的新兴市场股票策略团队，管理的总资产规模（AUM）大约是多少？请引用来源。」

**zip (`mode: buffered`, 18.8s)** — returned the per-strategy table with the exact
PDF figures and named the source file:
> Global Emerging Markets $10,706 MM … Emerging Markets Leaders $2,339 MM …
> 占整体全球股票策略 AUM（$19,217 MM）的约 55.7%
> **来源**：Morgan_Stanley_Oct_21_(EMEA).pdf — "Our Global Equity Strategies
> Assets Under Management (MM) as of August 31, 2021"

**container (`mode: stream`, 23.0s)** — SSE carried an explicit
`{"event":"tool","name":"kb_search"}` frame (preceded by two `ToolSearch` frames:
the claude CLI resolving the deferred in-process MCP tool), then the same figures
and source.

## 3. Trace proof (not just plausible numbers)

`GET /api/observability/traces/{id}` — both traces carry a real retrieval call:

zip · `6a68902a672f5619655568bd4fcf56c6` (22 spans)
```
invoke_agent Strands Agents
  execute_event_loop_cycle → chat global.anthropic.claude-sonnet-5
  execute_tool kb_search              872.3 ms   ← gen_ai.tool.name = kb_search
    Bedrock Agent Runtime.Retrieve    859.2 ms   ← the direct data-plane call
  execute_event_loop_cycle → chat …   (final answer)
```

container · `6a68903d5f29d1520db276587ca4500c` (9 spans)
```
invoke_agent kb-direct-container
  Bedrock Agent Runtime.Retrieve      906.4 ms
  execute_tool ToolSearch ×2
  execute_tool kb_search
```
The container's `execute_tool` durations are ~0 because tracing.py emits them
retrospectively from the SDK message stream; the boto3 `Retrieve` span is the
auto-instrumented one and carries the real latency.

## 4. What this confirms

- `bedrock-agent-runtime:Retrieve` with `managedSearchConfiguration` works from
  inside a Runtime under `launchpad-agent-execution-role` (no gateway, no token).
- The generated `kb_search` tool is discovered and called by both a Strands agent
  (native `@tool`) and the claude CLI (in-process SDK-MCP server).
- Answers are grounded: figures and the source filename come from the retrieved
  passages, and the tool span proves retrieval actually happened.
- Existing telemetry needed no change — `kb_search` shows up as a normal tool call
  in Observability for both methods.
