# Launchpad-CN 需求理解（供确认）

- 日期：2026-10-08
- 依据：`CN-MIGRATION-ANALYSIS.md` §0–§7 中已确认的决策（D1–D11、I1–I4）与实测结果

## 1. 一句话目标

把 `aws-samples/sample-agentcore-launchpad` fork 出一个独立分支 `cn-region`，改造成能在 **AWS 中国宁夏区（cn-northwest-1，账号 123456789012）** 真实部署、真实运行的 Agent 运维平台：agent 跑在中国区 AgentCore Runtime（V2）上，模型跨分区调用 Global 账号（210987654321，us-west-2）Bedrock 上的 GLM 5.3 / Kimi K3，中国区没有的 AgentCore 服务按既定方案自建或删除。

## 2. 交付范围

### 2.1 保留并改造（中国区可用服务）
| 能力 | 做法 |
|---|---|
| Runtime | 全部 agent 以 `platformVersion=V2` 部署到宁夏区；zip-runtime 与 BYOC 两条路径保留 |
| Gateway | 共享网关改 `AWS_IAM` 入站，生成代码用 SigV4 直签；示例 Lambda / OpenAPI 目标保留 |
| Identity | 保留 API-key / CustomOauth2 凭证提供方；Global Bedrock API key 托管于此 |
| Observability | 保留；bootstrap 开启 Transaction Search，读 `aws/spans` 与 runtime 日志组 |
| Code Interpreter / Browser | 内置工具演示保留 |
| Workspaces / Users / Chat / `/v1` API | 保留 |

### 2.2 自建替代（中国区没有的服务）
| 能力 | 自建方案 | 阶段 |
|---|---|---|
| 方式B Managed Harness | 以 Global 区 Harness 导出代码为模板的 `harness_local` 方法，部署为 zip-runtime（§3.L） | 3.7 |
| Memory | S3 会话快照 + OpenSearch k-NN + SQS/Lambda 抽取，Titan 向量化（§3.H） | 3.5 |
| Knowledge Base | 与 Memory 共用 OpenSearch，S3 文档摄取 + `kb_search`（§3.H 末尾） | 3.5 |
| Registry | 本地 SQLite 账本 + FTS5（§3.I） | 1 |
| Evaluations / Optimizations / A/B | Strands Evals + Harness Optimizer，分流下沉到调用链（§3.J）；**前端沿用 Global 页面不改，后端换引擎（D14，节点 N12）** | 5（**已纳入**，2026-10-10） |

### 2.3 删除
方式A（Claude Agent SDK 容器）、Cedar Policy/Governance 的策略引擎部分、Harness/Eval/KB/Memory/Registry 的 AWS 实现、CloudFront 入口与视频栈、Cognito 全部、Mantle/OpenAI/Claude 模型路径。

### 2.4 模型与凭证
- 默认模型 `global.zai.glm-5.3`（已实测有权限），备选 `global.moonshotai.kimi-k3`；Bedrock 入口区 us-west-2。
- 凭证链：Global 账号 Bedrock 长期 API key → 中国区 AgentCore Identity API-key provider → 运行时 `GetResourceApiKey` → Strands `BedrockModel(api_key=…)`。API key 不落盘，经环境变量注入。
- Studio “AI 修复” 改为 Kimi 后端；架构师助手与系统预置 agent 保留，模型改 Kimi/GLM。
- **N-R6（2026-10-12）第二类模型提供方：OpenAI 兼容端点（首个为硅基流动 `https://api.siliconflow.cn/v1`）。** 约束不变：外部端点的 API key 同样只经环境变量（`LAUNCHPAD_SILICONFLOW_API_KEY`，EC2 由 SSM `launchpad-siliconflow-api-key` 写入）进入 bootstrap，再托管于 AgentCore Identity API-key provider（`launchpad-siliconflow`），运行时经 `GetResourceApiKey` 取用；不落盘、不进日志、不进 `launchpad.yaml`。提供方细节只存在于 `schemas/agent.py` 的 `PROVIDERS` / `MODEL_CATALOG`（`docs/cn/interfaces/model-providers.md`）；该参数可选，缺失时环境仅有 Bedrock 模型。

### 2.5 部署与访问
- 控制平面：一台宁夏区 EC2（Ubuntu 24.04 arm64）+ systemd + nginx，**不开放外网**，安全组仅放行指定 CIDR。
- 共享基础设施：CDK `launchpad-base`（去 Cognito/Bedrock/CloudFront，分区用 `self.partition`）+ 新增 OpenSearch 域、SQS、抽取 Lambda。
- 全部为测试数据，跨境调用可接受。

## 3. 工作方式（按你的要求）

1. **先写代码**：在 fork 分支上按 §4 路线实现 Phase 0–3.7；**评估/优化（Phase 5 → N12）与 Skill Lab（N13）于 2026-10-10 纳入本轮。**
2. **真实部署**：用本机 `cn-northwest-1` 与 `default` 两个 profile，在宁夏区真实执行 `cdk bootstrap` → `make bootstrap` → 启动控制平面 → 创建并调用至少一个 zip-runtime agent、一个 BYOC agent、一个自建 Harness agent，Gateway 工具调用、Memory 读写、KB 检索各走通一次。
3. **检查 bug 并迭代**：每轮 = 部署 → 跑 `make verify` + 端到端冒烟 → 记录缺陷 → 修复；**最多 3 轮**，第 3 轮结束时交付：可运行的分支、部署记录、未修缺陷清单。
4. 每轮结束向你汇报结果；涉及删除/重建中国区资源的操作会先说明再执行。

## 4. 验收标准（本轮）

- [ ] `make bootstrap` 在 cn-northwest-1 幂等成功，`launchpad.yaml` 写出宁夏区资源
- [ ] 控制台在私网可登录（本地密码），导航只显示保留模块
- [ ] 方式C（Studio/zip-runtime）agent：创建 → 部署为 V2 Runtime → Chat 流式回答（GLM 5.3）
- [ ] BYOC zip agent 同上
- [ ] 自建 Harness（方式B）agent：声明式创建 → 部署 → Chat 回答
- [ ] Gateway（AWS_IAM）：hr-database Lambda 工具被 agent 调用成功
- [ ] Memory：多轮对话恢复；长期记忆（facts/preferences）抽取后在 Chat 记忆栏可见
- [ ] KB：上传文档 → 摄取 → `kb_search` 命中
- [ ] Registry：部署自动产生 A2A 记录，技能导入与挂载可用
- [ ] Observability：会话 trace 在控制台可见
- [ ] `make verify`（ruff + pytest + eslint + tsc + vitest + build + i18n）通过


### 4.1 v2 验收清单（2026-10-10 用户确认扩充；替代上表作为 L3 第 2/3 轮门禁）

每条须有可量化断言；用例按子系统分 spec，`tests/e2e/cn/*.spec.ts`，在默认环境与 t1 环境各跑一遍，结果进 `docs/cn/reports/bugs-round-2-n10.md` 与 `docs/cn/evidence/round-2/`。

| # | 用例 | 量化断言 |
|---|---|---|
| 1 | 登录 → Overview 导航（en 与 zh-CN 各一遍） | 导航项 == `/api/capabilities` 的 10 项；zh-CN 页面无裸 i18n key（正则 `^[a-z0-9_.]+\.[a-z0-9_.]+$` 的可见文本为 0） |
| 2 | 方式B Harness：向导 → 部署 → Chat → 记忆栏 | 部署 ≤ 15 min 到 active；首轮流式回答含模型文本（非 echo/非错误）；回合结束后记忆栏事件数 ≥ 1 且**无需刷新** |
| 3 | 方式C Studio：画布示例 → 发布 → 部署 → Chat | 同 2 的时限与回答断言 |
| 4 | BYOC zip（`samples/byoc/hello-http`）→ 部署 → Chat | 回答为真实模型文本（不含 `[echo` 前缀）；日志无 `EndpointConnectionError` |
| 5 | Gateway：向导直接可选 hr-database（无需 `sync-defaults`）→ 提问员工信息 | 工具调用 ≥ 1；回答含夹具中的姓名与部门 |
| 6 | Memory 长期记忆：会话 1 告知 2 个事实 → 抽取 → 会话 2 提问 | ≤ 10 min 内长期记录 ≥ 2；会话 2 回答命中 2/2 事实 |
| 7 | KB：创建 → 上传 `samples/kb_docs` → 摄取 → 演练场 → 挂载 agent 提问 | 摄取 ≤ 15 min 完成且索引文档数 == 上传数；演练场 top-1 命中预期文档；agent 调用 `kb_search` 且回答引用文档内容 |
| 8 | Registry：A2A 记录自动产生；导入 `expense-report-writer` → 审批 → 挂载新 agent → 完成报销任务 | 记录状态流转正确；技能任务一次完成，无 `MaxTokensReached` 错误回合 |
| 9 | Identity：列出 `launchpad-global-bedrock`；创建自定义 OAuth2 provider（假端点）→ 列出 → 删除 | 列表含 api_key provider 且 ready；OAuth2 provider 创建/删除 200，删除后不再列出 |
| 10 | Observability：上述会话 trace / session / token 视图 | 会话数 ≥ 部署 agent 数；首个 trace 出现延迟 ≤ 120 s（2026-10-10 实测 cn-northwest-1 Transaction Search 索引延迟 86–103 s，原 60 s 阈值不现实） |
| 11 | 架构师助手：对话 → 提案 → 审批 → 部署 → Chat | ≤ 3 轮给出提案；审批后 ≤ 15 min active；Chat 回答含模型文本 |
| 12 | Users：注册 → 管理员审批 → 成员登录 → 权限边界 | 成员看不到 Users 页；成员删除他人 agent 返回 403；管理员可见全部 |
| 13 | Workspaces：默认工作区详情与 bootstrap 状态；非法跨账号配置被拒 | `bootstrap_status == ready`；非法 role ARN 返回 4xx 且 UI 显示错误 |
| 14 | 公共 `/v1` API：创建 API key → `X-Api-Key` 调用 → 吊销后调用 | 调用 200 且答案与 Chat 同路径；吊销后 401 |
| 15 | 内置工具：挂 Code Interpreter 的 agent 做计算题 | 工具调用可见；数值结果正确 |
| 16 | A2A：agent B 挂载 agent A 的 A2A 记录并提问 | B 的回合中出现对 A 的调用；回答含 A 的内容 |
| 17 | Agent 生命周期：编辑 → 重新部署 → 删除 | 版本号递增；删除后 Runtime、执行角色、日志组、S3 前缀、A2A 记录全部不存在（只读核验） |
| 18 | 错误路径：非法模型 id 部署；聊天已删除 agent；错误密码登录 | 部署任务失败且 UI 显示可读错误；404/401 有 UI 提示而非白屏 |
| 19 | Gateway 管理：新增/删除目标，工具列表刷新 | 新目标工具出现在 `/api/tools`；删除后消失 |
| 20 | Memory 管理页：浏览 actors/sessions/events/records，删除一条记录 | 列表与 Chat 记忆栏一致；删除后计数 -1 |
| 21 | Chat 功能：END SESSION、新会话、流式中断 | END SESSION 后新会话 id 变化；中断后 UI 无悬挂状态 |
| 22 | 运维幂等：第二次 `make bootstrap`、`cdk deploy --all` | 全部 reused / no changes；`/api/health` 200 |
| 23 | **区域边界（2026-10-10 用户要求）**：静态门禁 `scripts/region_boundary_check.py`（进 `make verify`）+ 活体审计 `docs/cn/reports/region-boundary-audit.md` | 代码中除标记为 Global Bedrock 模型调用的行外无任何非 cn-* 区域字面量；Global 账号中无 Launchpad 创建的资源；CloudTrail 中 Global 侧只有 Bedrock 模型调用事件；中国账号持有全部栈/资源；主机日志中唯一的 `amazonaws.com` 主机为 `bedrock-runtime.<model-region>` |
| 24–31 | **评估专项（N12 完成后启用）**：评估器创建 → 数据集 → 批量评估出分 → 按需评估 → 在线评估出结果 → Insights → 推荐生成 → 配置包 → 实验 A/B 分流统计 → 金丝雀判决 | 每步与 Global 版同样操作步骤录屏对比；批量评估 ≤ 30 min 出分；A/B 两臂流量比与权重偏差 ≤ 10% |
| 32 | **Policy 治理（D20 Phase 1，场景 s11a）**：capabilities / settings / Overview 健康行 | `features` 含 `policy`；`policy.mode == log_only`，`active_policies == 0`，无 `e2es_` 残留；`services.policy == false`；Governance → Policies 页签可见，首页 `policy` 健康行存在且非 Ready |
| 33 | Policy 编写（s11b）：控制台编辑器模板 → VALIDATE → SIMULATE（草稿） | 三条草稿各 **201 draft v1**，VALIDATE `ok == true`；模拟：成员 invoke **ALLOW**、成员 call_tool `hr-database___get_employee` **DENY**（determining 为该 forbid）、管理员 call_tool **ALLOW**；模拟器页面显示 DENY |
| 34 | Policy 发布（log_only，s11c） | 编辑器发布 **200 active v1** 且不触发 apply；`active_policies == 3`，`services.policy == true`；成员 Chat 一轮 **0 错误**；审计行 `hub/chat/invoke/allow/log_only` 指向该 permit；invoke 的 `would_deny` 为 0 |
| 35 | Policy 强制（s11d）：仅管理员 + 输入确认；成员被拒 | 成员 `PUT /settings` **403**；无证据时 **422 `policy.confirmation_required`**（`expected_confirmation` = 工作区 id）；确认后 `mode == enforce`；发布 `forbid members invoke <A>` 后成员 **403 `policy.denied`**（`detail.{policy_ids, decision_id, mode}`）且 Chat 出现 PolicyDenyCard；管理员 **200**；审计 `deny` 行 ≥ 2 |
| 36 | Policy 应用（s11e）：部署期收窄 | 应用前 `policy_stale == true`；`POST /apply` **202** 且 1 个 job；重部署 **succeeded ≤ 10 min**；`policy_stale == false`，`policy_coverage ∈ {full, partially_covered, iam_only}`；管理员工具轮 **0 错误** |
| 37 | Policy 恢复（s11f） | `mode == log_only`；retire / delete 各 **200**；`active_policies == 0`，`services.policy == false`，无 `e2es_` 残留；审计行保留（含 `deny`） |
| 38 | **模型提供方注册表（D21，场景 s12a）**：管理员姿态 / 成员拒绝 | `GET /api/settings/model-providers` **200**，`catalog_version ≥ 1`；`bedrock` 行 builtin · required · enabled · **configured == true**；`siliconflow` 行 builtin · 可选（`configured` 仅记录，取决于用户是否已创建 `launchpad-siliconflow-api-key`）；Titan v2 行 `kind == embedding`、默认、`embedding_dimension == 1024`、`source == builtin`；GLM 5.3 为默认聊天行；无 `e2es-` 残留；`/api/capabilities` 的 `catalog_version` 与注册表一致，`embedding == {titan, 1024, bedrock}`，`models[]` 全为 `kind == chat` 且 `enabled`，不含 Titan；成员 `GET` / `POST` 均 **403**，页面显示 `v2-mp-forbidden`；所有响应不含密钥材料 |
| 39 | 提供方生命周期（s12b） | `POST` 新建 OpenAI 兼容提供方（允许列表内主机，无凭证）**201**：`credential_provider == launchpad-<name>`、`credential_ssm_parameter == launchpad-<name>-api-key`、`credential_env == LAUNCHPAD_<NAME>_API_KEY`、`id_prefix == <name>.`、`configured == false`、`probe.state == never`；按 `name` 可 `GET`；`PUT` 标签 **200** 且 name 不变；允许列表外主机 **422 `model_provider.host_not_allowed`**；重名 **409 `model_provider.exists`**；`POST /probe` **200 `state == failed`**，`error` 含 `credential_unavailable`、≤ 300 字符，探测 / 行 / 注册表响应中无 `sk-`、`Bearer <token>`、`api_key=`；结果写回行上 |
| 40 | 模型行（s12c） | `POST /models {probe:false}` **201**：`source == manual`，id 为 `<prefix><api_model_name>`，OpenAI 兼容默认（tools + streaming，8192 / 4096，`inference_params {true,true}`，`capability_sources` 全 `manual`）；重复 **409 `model.exists`**；`PUT {inference_params:{temperature:false}, default_max_tokens:4096}` **200** 保留 `top_p`、两字段来源置 `manual`；以带斜杠的 Launchpad id 作 `model_ref` 的 `PUT` **200**；该行进入 capabilities 且 `available == false`；对其 `PUT is_default` **422 `model.default_invalid`**；Kimi K3 设默认 **200** → capabilities 的 `default` 跟随，取消默认 **409 `model.is_default`**，GLM 5.3 恢复默认 **200**；禁用 **200** → capabilities 不再列出而注册表仍列出（`enabled == false`），`catalog_version` 递增；重新启用 **200** → 重新列出 |
| 41 | 工作区开关（s12d） | `PUT /workspaces/<default>/models {disabled_models:[row]}` **200** 回显 `{id, name, disabled_models, workspace_id}`；capabilities 省略该行，注册表 `workspaces[]` 列出该开关，全局行仍 `enabled`；未知 id **422 `model.unknown`**；还原为 `[]` 后重新列出 |
| 42 | 变更日志（s12e） | `GET /changes` 最新在前（id 严格递减，每行有 `actor` 与 `created_at`，无密钥材料）；提供方行 `provider.create → provider.update → provider.probe`；聊天行 `model.create` + **4** 条 `model.update`（参数、标签、禁用、启用），禁用记录 `before.enabled true → after.enabled false`；两次默认切换按序（Kimi、GLM）；两条 `workspace.flags`；被拒的写入不产生日志；`limit=4&before=<id>` 游标分页得到相同 id 集且无重复 |
| 43 | 嵌入模型护栏（s12f） | 无维度的嵌入行 **422 `model.invalid`**；`POST {kind: embedding, probe:false, overrides:{embedding_dimension: 768}}` **201**（非默认、不进入 `models[]`）；`PUT is_default` 结果**二者取一**：向量索引非空时 **409 `embedding.reindex_required`**，`detail.indices[{name, docs>0}]`、`dimension_from 1024`、`dimension_to 768`、`model_from titan`、`model_to <row>`，且 `capabilities.embedding` 不变；或空基底时 **200**，`capabilities.embedding` 为新行，随后切回 Titan **200**；所走分支作为 `observation` 记录 |
| 44 | 注册表清理（s12g） | `DELETE` 嵌入行、聊天行、提供方 **204 ×3**；`GET /{id}` **404 `model_provider.not_found`**；提供方集合 / 行数 / capabilities `models[]` / 默认 / embedding 回到 s12a 基线，`catalog_version` 更大；`warnings[]` 与任何工作区开关中无 `e2es`；最后一条日志为 `provider.delete`；全程未注册凭证，AWS 上无新对象（C8） |

### 4.2 N13 验收（Skill Lab on OpenCode，D16）

- [ ] Global 版 Skill Lab 页面（Tasksets / Taskgen / Train / Eval / Artifacts）原样可用，导航随 `skill_lab` capability 出现
- [ ] 执行后端列表只含 `opencode`（Claude Code / Codex 不再出现），模型为 Kimi K3 / GLM 5.3
- [ ] taskgen → train → eval 三条流水线各跑通一次示例技能（`vendor/skillopt/data/skilleval_demo`），artifacts 可浏览，训练曲线与评测结果页有数据
- [ ] worker 容器从国内镜像构建，运行在 cn-northwest-1 AgentCore Runtime 上，作业异步、可取消
- [ ] 评测后的技能可发布到 Registry 并挂载到 agent

## 5. 本轮不做 / 待定
- Phase 5（Strands Evals / Harness Optimizer / A/B）——**2026-10-10 用户决定纳入（D14）**，作为节点 N12 在 N11 第 2 轮合并后执行；验收 = Global 版评估/实验/金丝雀/数据中心页面在中国区可用，后端无 AgentCore Evaluations/Optimizations 调用。
- 外部 OIDC JWT 入站、Cedar 策略、方式A——已决定不做。**Skill Lab 于 2026-10-10 由 D16 纳入（N13，OpenCode 替代 Claude Code / Codex）。**
- I5–I7（国内镜像源/代理、用户规模、现有 IdP）尚未提供；镜像源缺省先用清华 PyPI / npmmirror，若你的网络策略不同请告知。
