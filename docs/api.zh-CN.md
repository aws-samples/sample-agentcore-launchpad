# 公开 API(/v1) / Public API

English: [api.md](api.md)

每个已部署的 Agent 都可通过平台的 `/v1` 接口调用——与 Chat 交互页面使用的是同一
条调用链。交互式文档:**`/api/docs`**。

鉴权:`X-Api-Key` 请求头。在控制台创建密钥(Chat → API KEYS),或:

```bash
curl -s -X POST localhost:8000/api/apikeys -H 'Content-Type: application/json' \
  -d '{"name": "integration"}'
# → {"id": "…", "prefix": "lp_live_ab12…", "key": "lp_live_<完整密钥,仅此一次展示>"}
```

密钥以 **哈希(sha256)** 存储——完整密钥仅在创建时展示一次。

## 同步调用 / Sync invoke

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2?", "session_id": null}'
# → {"agent":"…","text":"4","session_id":"…","latency_ms":1234}
```

## 流式调用(SSE) / Streaming invoke

```bash
curl -N -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke-stream \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "Tell me a two-sentence story."}'
# event: meta   → {"session_id": "…", "mode": "stream"}
# event: delta  → {"text": "Once"} … (增量分片)
# event: done   → {"latency_ms": 2100}
```

在下一次调用时传回返回的 `session_id` 即可延续对话(session 上下文与
AgentCore Memory 随之而来)。

## Python

```python
import requests

BASE, KEY, AGENT = "http://localhost:8000", "lp_live_…", "<AGENT_ID>"

# 同步
r = requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke",
    headers={"X-Api-Key": KEY},
    json={"prompt": "How many vacation days does EMP-1024 have left?"},
    timeout=120,
)
print(r.json()["text"])

# 流式(SSE)
with requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke-stream",
    headers={"X-Api-Key": KEY},
    json={"prompt": "Summarize our HR policy in one line."},
    stream=True, timeout=300,
) as stream:
    for line in stream.iter_lines(decode_unicode=True):
        if line.startswith("data:"):
            print(line[5:].strip())
```

错误使用平台统一信封 `{code, message, detail}`——例如
`auth.missing_api_key`(401)、`agent.not_active`(409)、`agent.not_found`(404)。

## 控制台在线评估 API / Console Online Evaluation API

`/api/eval/online/*` 管理 AgentCore **在线评估配置**:按采样比例持续给真实会话打分。AWS 是唯一事实来源,
ledger 只存标识。列表返回 workspace 账号内全部配置并按 `owner` 归类:`agent`(本控制台为 agent 创建)、
`experiment`(`exp_*`/`can_*` 实验 arm,只读)、`external`(其他来源)。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/eval/online` | `{configs, total}`:全部配置,含 `owner`、双状态、`failure_reason`、evaluators、采样率、超时、`matched_agent`、`duplicate_enabled`、`results_log_group` |
| `POST` | `/api/eval/online` | 为 active agent 创建:`{agent_id, evaluators[1..10], sampling_percentage 0.01–100(默认 10), session_timeout_minutes 1–1440(默认 15), filters[0..5], description?, enable_on_create(默认 true)}` → 201 |
| `GET` | `/api/eval/online/{config_id}` | 完整详情(`filters`、`data_source`、`execution_role_arn`) |
| `PATCH` | `/api/eval/online/{config_id}` | 仅 `owner=agent`:修改描述 / evaluators / 采样率 / 超时 / filters;后端总是重发完整 `rule`(AWS 整体替换) |
| `POST` | `/api/eval/online/{config_id}/pause` · `/resume` | 切换 `executionStatus`(`agent` 与 `external`) |
| `DELETE` | `/api/eval/online/{config_id}` | 删除 AWS 配置并删掉 ledger 行(`agent` 与 `external`);结果日志组保留并在响应里给出 |
| `GET` | `/api/eval/online/{config_id}/results?range=1h\|6h\|24h\|7d` | Logs Insights 聚合结果日志组:每个 evaluator 的均值 / 计数 / 会话数 / 标签分布、按时间分桶的趋势、最近 ≤50 条带 judge 解释的记录、错误计数 |

错误码:`online_eval.no_telemetry`(400,agent 还没有遥测日志组,先跑一次会话)、`online_eval.evaluator_unsupported`(400)、
`online_eval.read_only`(403)、`online_eval.not_found`(404)、`online_eval.conflict`(409)、
`online_eval.workspace_not_bootstrapped`(400)、`online_eval.invalid_filter` / `online_eval.bad_range`(422)。
结果最早在会话空闲 `session_timeout_minutes` 之后出现;ENABLED 配置引用的自定义 evaluator 会被 AWS 锁定。

在线评分也出现在查看会话的地方:

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/observability/sessions/{session_id}` | 会话详情附带 `online_scores: {configs[{config_id, config_name, owner, agent{id,name}?, records[{time, evaluator_id, level, score, label, explanation, trace_id}]}], total, unavailable, configs_exist}`——该会话在所有配置下的结果记录(agent 持有的块排在最前),用一条前缀 `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])` 查询读取。失败降级:结果查询失败只置 `unavailable: true`,绝不影响追踪与对话记录;`configs_exist` 表示 workspace 是否有 agent 持有的配置(结果与配置都没有时 UI 隐藏该区块) |
| `GET` | `/api/overview/online-quality` | 「在线质量 · 24h」tile:`{range: "24h", mean, scores, sessions, agents, configs, evaluators[{evaluator_id, mean, count, polarity}], cached}`——对每个 (evaluator, agent 持有配置) 组合按计数加权求均值,lower-is-better 的 evaluator 取 `1 − mean`,因此 tile 始终「越高越好」;`evaluators[].mean` 保持原始值;`configs` 统计 workspace 内 agent 持有的配置数(账本),`agents` 统计有评分的 agent 数,因此「已配置但尚无评分」与「没有配置」可区分。按 workspace 缓存 120 秒并单飞,`force=true` 绕过;没有 agent 持有配置的 workspace 直接返回空载荷,不调用 AWS |
