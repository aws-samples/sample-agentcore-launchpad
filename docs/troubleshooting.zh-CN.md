# 故障排查 / Troubleshooting

在 AWS 上构建与运行本平台时遇到的真实、已验证的坑。以下每一条都在实现过程中被
实际观察到——没有一条是臆测。

English: [troubleshooting.md](troubleshooting.md)

## 账号与环境

- **AgentCore 预览需按账号开启。** Runtime、Harness、Registry、Gateway、Policy
  与 Evaluation 都是预览功能,必须先在 `us-west-2` 为你的账号开启,bootstrap 才能
  成功。
- **默认模型是 `global.anthropic.claude-sonnet-4-6`。** 目标账号中没有
  `sonnet-5` inference profile(已通过 `bedrock list-inference-profiles` 验证)。
  Agent 默认使用该 profile;可在 AgentSpec 中用 `model_id` 逐个覆盖。
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

## 本地开发

- **Vite 自动切换前端端口。** 若 `5173` 被占用,平台前端会落到 `5174`(或下一个
  空闲端口)。设置 `PLATFORM_UI_PORT` 可固定它。后端保持在 `8000`。此行为适用于
  `make dev`;`start.py` 使用严格端口,任一配置端口被占用时会在启动前失败。
- **根目录不再启动独立 Studio。** 根目录生命周期在 `/create/studio` 提供原生
  双语画布;仅在明确需要时,才从 `apps/studio/` 单独运行 vendored 应用。

## 治理

- **Cedar 的 deny 会带上作出判定的 policy id。** 当网关在 `ENFORCE` 模式下拦截
  一次工具调用时,决策(以及决策日志)会指明产生 DENY 的策略——用它追溯是哪条
  语句触发的。
- **引擎被删除后,网关上的引用还在。** 策略引擎本身被删除后,AWS 仍保留
  `policyEngineConfiguration.arn`。治理页面会把它显示为 `引擎已删除` 并保留失效
  ARN(而不是显示"未挂载"),策略类变更返回 `governance.policy_engine_deleted`,
  同时提供创建并挂载表单——确认时会新建引擎并覆盖该失效引用。
