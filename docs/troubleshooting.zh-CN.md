# 故障排查 / Troubleshooting

在 AWS 上构建与运行本平台时遇到的真实、已验证的坑。以下每一条都在实现过程中被
实际观察到——没有一条是臆测。

English: [troubleshooting.md](troubleshooting.md)

## 账号与环境

- **AgentCore 预览需按账号开启。** Runtime、Harness、Gateway、Policy 与
  Evaluation 都是预览功能，必须先在 `us-west-2` 为你的账号开启，bootstrap 才能
  成功。Agent Registry 已 GA（自 2026-08-06 起使用 `agent-registry` 命名空间，见
  [registry-ga-migration.md](registry-ga-migration.md)），无需开启预览。
- **新建 Agent 的默认模型是 `global.zai.glm-5.3`**（`agent_default_model_id`，
  环境变量 `LAUNCHPAD_AGENT_DEFAULT_MODEL_ID`）。新建 Agent 默认使用该
  inference profile；其他模型仍可选择，已有 Agent 保留各自的 `model_id`。可在
  AgentSpec 中用 `model_id` 逐个覆盖，或按
  [setup.zh-CN.md → 切换模型](setup.zh-CN.md#切换模型) 修改默认值。
- **账号里没有 OpenAI / Anthropic 模型。** 平台自己调用的模型默认是 OpenAI 与
  Anthropic 的（评审 `global.openai.gpt-6-sol`，意图与建议问题
  `global.anthropic.claude-sonnet-5`，以及 actor、架构师、架构助手等），这类账号
  会看到评估器创建被拒、意图视图降级，或这些功能报 `AccessDeniedException`。管理员在
  **admin → 配置 → 模型设置** 里切换（选模型、**测试**、**保存**，立即生效）；或在
  主机上用 `scripts/switch_models.py` 探测账号，把能用的替代模型写进
  `/etc/launchpad-models.env`（见 [setup.zh-CN.md → 切换模型](setup.zh-CN.md#切换模型)）。
  Claude Agent SDK、Studio AI Fix 和 Skill Lab 的 claude / codex 路径不能换成其他
  家族。换评审模型会改变评估分数。
- **`make bootstrap` 停在 `UpdateTraceSegmentDestination`，报
  `AccessDeniedException: XRay does not have permission to call PutLogEvents on
  the aws/spans Log Group`。** 该账号/区域从未开启过 Transaction Search，没有任何
  CloudWatch Logs 资源策略允许 X-Ray 写入 `aws/spans`。bootstrap 现在会在更新之前放置
  `TransactionSearchXRayAccess`；旧版本请先更新代码，再重新运行 `make bootstrap`（它是
  幂等的，已部署的栈和已创建的资源都会复用）。
- **`config/launchpad.yaml` 已 gitignore。** 它包含账号 id 与演示凭证,因此从不
  提交。若缺失(全新 clone,或你删了它),重新运行 `make bootstrap`——它是幂等的,
  会从既有资源重写该文件。
- **uv 管理的 venv 需要 `uv run`。** 后端/基础设施命令请通过 `uv run …` 运行
  (如 Makefile 所示)。zip package 阶段还需要 venv 内的 `pip`——uv venv 不自带,
  因此它被声明为显式依赖。

## 部署耗时与行为

- **部署耗时因方式而异:** harness ≈ 30 秒(无构建),zip ≈ 1–3 分钟(含 ARM64
  wheels 的 `pip install`),container ≈ 2–4 分钟(CodeBuild docker build + push)。
  通过 `GET /api/jobs/{id}` 或 Agent 的 `deployment.stages` 查看进度。
- **容器镜像需要非 root 用户。** Claude CLI 的 `bypassPermissions` 模式拒绝以
  root 运行,因此方式A镜像以非 root 用户构建并运行——你自定义 Dockerfile 时请保留
  这一点。

## Registry

- **记录异步落定。** 新记录先是 `CREATING`,片刻后转为 `DRAFT`——若立即回读,请
  轮询。
- **`DEPRECATED` 为终态。** 没有 `PUBLISHED` 状态;`APPROVED` 即为上线状态。
  生命周期为 `DRAFT → PENDING → APPROVED`,禁用记录会将其置为 `DEPRECATED`,且不可
  再返回。
- Descriptor schema 版本要求严格(MCP `2025-07-09`,skills `0.1.0`)——平台发送
  服务所期望的确切版本。

## 评估与优化

- **每账号仅一个活跃 batch evaluation。** 运行在一个账号锁后串行;已提交的运行会
  报告其 `queue_position`,并在锁释放后开始。这是预期行为,不是卡住。
- **batch evaluation 约 3–5 分钟;insights 约 15–20 分钟。** 一个快速的 2 条目
  打分运行数分钟内完成;失败归因 insights 运行则长得多。在 CloudWatch trace 被
  打分之前,运行会一直处于 `evaluating`。
- **小样本下 A/B 各臂指标滞后 > 30 分钟。** online-evaluation 指标需要时间填充,
  因此在只有少量调用时,verdict 会如实报告为 *insufficient-data*,而不是强行给出。
  要得到真实的显著性判定,请用更大的流量(或等待)。
- **需要参考答案的 judge 会在运行开始前被拒绝。** 自定义 LLM judge 的 prompt 若
  引用 `{expected_response}`、`{expected_tool_trajectory}` 或 `{assertions}`,这些
  值来自数据集场景。若运行范围是时间窗口 / 会话 id,或数据集本身不带这些内容,
  AgentCore 会对每个 (会话 × 评估器) 组合抛出
  `ValueError: Evaluator prompt requires: 'expected_response'`,整个 batch 在约
  10 分钟后以 FAILED 结束;因此控制台会直接以 `run.judge_needs_ground_truth`
  拒绝提交。修复方式:给数据集场景补上参考答案
  (`turns[].expected_response` / `expected_trajectory` / `assertions`),或编辑
  judge prompt 去掉该占位符。
- **失败的运行会写明原因。** batch 的 `errorDetails` 只统计伤亡("All 30 sessions
  failed");每条 trace 的真实原因只写在该 batch 自己的结果日志流里
  (`/aws/bedrock-agentcore/evaluations/batch-evaluations/results/<workspace>`,
  流名 `run-<batchEvaluationId>`)。现在运行记录的 error 同时带上两者,先读运行行,
  只在需要看其余 trace 时才去翻日志流。
- **Harness Agent 不参与 batch evaluation。** Managed-harness Agent 不暴露用于
  trace 范围限定的 span service name,因此 batch eval 面向 runtime 型 Agent
  (`zip_runtime` / `studio` / `container`)。UI 会说明这一限制。

## 知识库

- **创建返回 `202`，数据源在几分钟后才出现。** 托管知识库需要 1.5–3 分钟才离开
  `CREATING`，而它的数据源必须等到 `ACTIVE` 之后才能创建，所以
  `POST /api/knowledge-bases` 会立即返回，由后端线程
  （`knowledge._start_source_completion`）随后轮询并创建数据源。若后端在这个窗口内
  重启，线程随之消失，就会留下一个 `ACTIVE` 但**没有任何数据源**的知识库。详情页会
  把这件事说清楚并给出修复入口：「数据源还没建好。后端会在知识库变为 ACTIVE 后自动
  创建（通常 1–3 分钟）；若长时间没有出现，点右侧按钮补建——重复点击不会创建多个
  数据源。」（`knowledge.detail.sources.missingSource`），旁边是「补建数据源」按钮，
  它发出 `POST /api/knowledge-bases/{kb_id}/data-sources`。多点几次也安全：
  `_find_data_source_at` 会对同一桶/前缀返回既有连接器，而不是再建一个。
- **过早触发的同步会以 `409 kb.sync_not_ready` 被拒。** 数据源仍在预置（还不是
  `AVAILABLE`）时 `StartIngestionJob` 抛 `ValidationException`，已有同步在跑时抛
  `ConflictException`；`knowledge.start_sync` 把两者一起捕获，统一答以
  `409 kb.sync_not_ready`（「数据源可能仍在预置，或已有同步在运行」）。等数据源报告
  `AVAILABLE` 即可——控制台随后会自行发起首次 ingestion。在知识库的*其他*路由上，
  这两个 AWS 异常并不在本地捕获，而是经全局映射到达控制台，即 `400 aws.validation`
  与 `409 aws.conflict`（见 [api.zh-CN.md](api.zh-CN.md) 的错误码表）。
- **`kb_role_arn` 填错只会在 ingestion 时才失败。** `CreateKnowledgeBase` 不校验
  `roleArn`，因此用错误或权限不足的角色创建的知识库照样会变成 `ACTIVE`；问题要等到
  ingestion 作业失败时才暴露（其 `failure_reasons` 按数据源展示）。为自带桶授予
  `s3:GetObject`/`s3:ListBucket` 的按知识库内联策略，也是放到同一个 ARN 指向的角色上
  （`knowledge._sync_kb_policy`），所以同样会落到错误的角色。而该键完全缺失时会被提前
  拦住：`create_kb` 直接以「kb_role_arn missing from this workspace's resource map —
  run its bootstrap」拒绝，而不是创建一个不可用的知识库。

## 本地开发

- **Vite 自动切换前端端口。** 若 `5173` 被占用,平台前端会落到 `5174`(或下一个
  空闲端口)。设置 `PLATFORM_UI_PORT` 可固定它。后端保持在 `8000`。此行为适用于
  `make dev`;`start.py` 使用严格端口,任一配置端口被占用时会在启动前失败。
- **Studio 内置于控制台。** Strands Studio 画布位于 `/create/studio`;没有单独的
  Studio 进程或端口。

## 治理

- **Cedar 的 deny 会带上作出判定的 policy id。** 当网关在 `ENFORCE` 模式下拦截
  一次工具调用时,决策(以及决策日志)会指明产生 DENY 的策略——用它追溯是哪条
  语句触发的。
- **引擎被删除后,网关上的引用还在。** 策略引擎本身被删除后,AWS 仍保留
  `policyEngineConfiguration.arn`。治理页面会把它显示为 `引擎已删除` 并保留失效
  ARN(而不是显示"未挂载"),策略类变更返回 `governance.policy_engine_deleted`,
  同时提供创建并挂载表单——确认时会新建引擎并覆盖该失效引用。

## 身份

- **对话、`/v1`、调用或评估返回 `agent.inbound_issuer_mismatch`（409）。**
  该智能体的 JWT 授权器信任的不是工作区 Cognito 用户池，而平台的所有调用都携带
  工作区 Cognito 令牌。这是限制而非故障：请用该 IdP 签发的令牌从外部调用
  （`samples/inbound-jwt/`），或在智能体页面切换为 Cognito 预设或 IAM。错误详情
  会列出两个签发者（[identity.md §8.1](identity.md#81-inbound-auth-iam-sigv4-vs-jwt-bearer)）。
- **创建 obo 目标时出现签发者警告。** 连接的 IdP 不是网关的入站签发者。只有该
  IdP 信任网关签发者的令牌作为主体令牌时，交换才会成功；请在 IdP 侧配置该信任，
  或选择同一签发者的连接（[identity.md §8.3](identity.md#83-obo-on-behalf-of-token-exchange)）。

## Agent-DLC：判据、黄金集与放行门

这里多数情况是**有意的拒绝**——报错信息会说明该怎么做。

| 你看到的 | 含义 → 怎么处理 |
|---|---|
| 部署在 `register` 阶段报 `ConflictException: Concurrent update detected` | AgentCore Registry 在消化刚提交的记录时的瞬态冲突——它本身不属于 Agent-DLC，但正是它会让一次门控部署在放行记录开出之前就失败。`registry.submit_record` 会按 AWS 自己的“请重试”措辞重试三次（1 秒 / 3 秒 / 7 秒）；**其他**冲突（例如记录已是 PENDING_APPROVAL）仍会上报，因为那是真实的状态冲突 |
| `422 criteria.invalid`，带 `detail.findings` | 判据表违反了方法论的规则。`criteria.redline_judge`：红线不能交给大模型裁判——改用代码断言、轨迹匹配或指标。`criteria.dimension_uncovered`：每个维度要么有判据、要么写明 `n/a:<dimension>`。`criteria.no_redline`：没有红线的表不能发布 |
| 某条门限判据显示“声明为门限 · 仅观察” | 裁判类判据在校准通过前不拦人。去「判据 → 裁判校准」跑一次标注任务并记录 `aligned`；在那之前它只记录，不强制 |
| `409 calibration.not_supported` | 数据不支持把裁判判定为一致。detail 里带着裁判—人 κ、人—人 κ、样本量与下限。如果**人之间**就判不一致，请改写判据——写不下来的规则换裁判也救不了 |
| 放行判定为 `INVALID` | 这不是“不达标”：是证据判不了。`provenance.issues` 会说明原因——判据版本未签署、运行用的是别的版本或端点、没有评估保留集——每行的 `reason` 会指出判定缺失或无法判定占比超过 5%。该修的是证据；豁免是用错了工具，而红线永不可豁免 |
| `409 golden.holdout_sealed` | 保留集只编制一次，之后封存。正是这一点让它成为保留集；新用例请加到 `dev` 或 `regression` |
| `409 golden.holdout_closed` | 样本准入与样本编辑永不写入保留集。请用 `dev` 或 `regression` |
| `409 release.nothing_pending` | 没有等待判定的候选版本。`gated` 工作区里一次部署就会开出放行记录；`direct` 工作区根本不做门控（`PUT /api/release-policies/{workspace}` 的 `release_mode`） |
| `409 release.not_gateable` 并附原因 | A2A 运行时不接受端点限定符；系统预置由平台放行；尚未部署的智能体没有可指向的版本 |
| 放行报告显示“2 次放行运行未完成”，且是遥测超时 | 评估根本没看到这些会话。每个端点写自己的日志组，所以这通常是一个从未被调用过的新端点：先在该端点上调用一次，再重跑放行门 |
| `409 run.cost_over_limit` / `run.cost_confirm_required` | 工作区的成本闸门。detail 带预估值；超过上限的运行只有管理员能发起，`confirm_cost: true` 表示已确认成本 |
| `422 run.repeats_scope` | pass^k 是对智能体回放数据集用例——它无法重复历史会话或日志源 |
| `409 watch.over_cost_ceiling` | 排程重评的预估成本超过上限，于是被跳过并上报，而不是先花掉。调高 `max_cost_usd` 或缩小切分 |
| 新区域的工作区显示 `ready`，但追踪、可观测性和评估都是空的；bootstrap 的 observability 阶段显示 `unavailable · AccessDeniedException` | 该区域的 Transaction Search 没有开启。用当前 `infra/spoke/` 模板更新工作区角色（或 hub 角色）——模板已补齐 `UpdateTraceSegmentDestination` 代表调用方执行操作所需的权限——然后由管理员调用 `POST /api/workspaces/{id}/observability/repair` 只重跑这一阶段，无需重新 bootstrap |
| 批量评估报 `FAS credentials do not have permission to create CloudWatch log groups` | 角色早于 `EvaluationResultLogGroups` 这条权限：AgentCore 用调用方的凭证创建评估结果日志组。用当前 `infra/spoke/` 模板更新角色 |
| `409 annotation.links_not_allowed` | prod 级工作区不发放无账号标注链接：那里的标注会接触真实客户会话。请把标注人邀请为成员并授予 `judge.calibrate` |
| 标注链接返回 `404 share.not_found` | 所有不可用状态刻意返回同一个答案（未知、已撤销、已过期、任务已删，或工作区升为 prod）。重新生成一个链接 |
| `403 calibration.own_labels` | 你参与产生了这个任务的标注（作为标注人、仲裁人，或发放过它的标注链接），因此不能同时裁定它的裁判。请让另一位持 `judge.calibrate` 的人（或管理员）记录结论 |
| `409 calibration.not_supported` 且 `human_human_kappa: null` | 只有一个人做了标注。裁判是对照「彼此一致的人」来认证的，只有单人标注的样本不计入——请让第二位标注人完成 |
| `409 admission.redaction_blocked` | 个人信息护栏拒绝了这段会话，它不能进入黄金集。准入接口自己会做脱敏检测，所以即使没打开过候选详情，答案也是这个 |
| 签署 / 准入 / 校准时 `403 auth.permission_required` | `criteria.sign`、`golden.admit`、`judge.calibrate` 只授予具体的人，不按角色给——它们决定“什么叫好”。由管理员按用户授予 |
| 删除智能体返回 `aws_resource_deleted: false`（门控智能体，或带 Runtime Canary 端点的智能体） | 这是预期行为，不是失败。harness 端点可能 DELETING 数分钟，而 AgentCore 不允许删除仍带端点的资源，所以控制台的行立即删除，AWS 侧由后台 sweep（`dlc.teardown`，每个 tick 执行）收尾。无需处理；已删除行的 `teardown_pending_at` 清空（且 `endpoint_mode` 回到 `default`）就是 sweep 的回执 |
