# 故障排查 / Troubleshooting

在 AWS 上构建与运行本平台时遇到的真实、已验证的坑。以下每一条都在实现过程中被
实际观察到——没有一条是臆测。

English: [troubleshooting.md](troubleshooting.md)

## 账号与环境

- **AgentCore 预览需按账号开启。** Runtime、Harness、Gateway、Policy 与
  Evaluation 都是预览功能，必须先在 `us-west-2` 为你的账号开启，bootstrap 才能
  成功。Agent Registry 已 GA（自 2026-08-06 起使用 `agent-registry` 命名空间，见
  [registry-ga-migration.md](registry-ga-migration.md)），无需开启预览。
- **默认模型是 `global.anthropic.claude-sonnet-5`**（`DEFAULT_MODEL_ID`）。
  新建 Agent 默认使用该 inference profile；Sonnet 4.6
  （`global.anthropic.claude-sonnet-4-6`）仍可选择，已有 Agent 保留各自的
  `model_id`。可在 AgentSpec 中用 `model_id` 逐个覆盖。
- **`config/launchpad.yaml` 已 gitignore。** 它包含账号 id 与演示凭证,因此从不
  提交。若缺失(全新 clone,或你删了它),重新运行 `make bootstrap`——它是幂等的,
  会从既有资源重写该文件。
- **登录返回 `429 auth.locked` / `auth.rate_limited`。** 控制台登录保护已触发（r12-g）：
  `auth.locked` 表示所提交的账户名连续 5 次密码错误，被锁定 60 秒，之后每再失败一次翻倍，
  上限 30 分钟——等待 `Retry-After` 到期，或由管理员在"用户管理"页点击"解锁"
  （`PATCH /api/users/{id} {"unlock": true}`）。`auth.rate_limited` 表示该源 IP 在 5 分钟内
  登录失败超过 20 次（或一小时内注册失败超过 10 次）且本次请求也失败了——正确密码、有效
  密钥或有效注册从不会被该窗口拒绝。在 EIP 路径上源 IP 是真实客户端（nginx 转发）；在控制台
  NLB 之后所有用户共用 NLB 的私有 IP，一个失控的客户端会让所有人的*失败*请求一段时间内都
  返回 429——应调大
  `LAUNCHPAD_AUTH_LOGIN_IP_LIMIT` / `LAUNCHPAD_AUTH_REGISTER_IP_LIMIT`（或设为 `0`），而不是
  削弱按账户的锁定。该状态在进程内：重启后端会清空所有锁定，包括内置 admin 的。每次锁定都
  会在 `journalctl -u launchpad-backend` 的 `launchpad.auth` 记录一行 `WARNING`。`/v1` 上
  的同一 429 表示该 IP 在 5 分钟内发送了 20 次无效 `X-Api-Key`。
- **部署后所有人被注销。** 自 r12-g 起会话 cookie 由 `data/session_secret`（或
  `LAUNCHPAD_SESSION_SECRET`）中的随机密钥签名，不再使用 admin 密码。升级后首次启动会生成
  该密钥并一次性注销所有既有会话（`launchpad.auth` 会有一行 `WARNING`）。修改或重置密码仍会
  结束该账户的其他会话（cookie 中的会话纪元；admin 的纪元跟随 `LAUNCHPAD_AUTH_PASSWORD`）。
  要注销*所有人*，请轮换密钥：删除 `data/session_secret`（或设置新的
  `LAUNCHPAD_SESSION_SECRET`）后重启后端。
- **admin 账户不会被锁定，而成员会。** 这是设计如此：内置 `LAUNCHPAD_AUTH_USERNAME` 是应急
  凭证，不受按账户名锁定约束（它依靠强配置密码与按 IP 窗口）。请为它设置一个足够长的随机密码。
- **注册时以前能用的密码现在报 `auth.weak_password`。** 策略现为 12 个字符以上、不能与
  用户名/邮箱相同、不能是常见密码（见 `detail.reason`）；既有账户在下次改密前不受影响。
  `LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH` 可调整长度（下限 6）。觉得过严？设置
  `LAUNCHPAD_AUTH_PASSWORD_POLICY=relaxed` 即只保留长度规则；`GET /api/auth/status` 的
  `password_policy` 显示当前生效的策略。
- **注册"成功"但待审批队列里没有它。** 审批模式下，已占用的用户名或邮箱会返回与待审批注册
  完全一致的响应（不泄露账户存在性）；冲突以 `INFO` 记录在 `launchpad.auth`。请让用户换一个
  名字注册。`409 auth.registration_backlog` 表示已有 `LAUNCHPAD_AUTH_REGISTRATION_PENDING_MAX`
  （50）个账户等待审核——请在用户管理中批准或拒绝一部分。
- **用户管理页返回 `409 users.last_admin` / `users.self_delete`。** 你正在降级、禁用、设为
  过期或删除最后一个有效的管理员*账户*（配置文件中的内置 admin 不计入），或在删除自己的
  账户。请先将另一个账户提升为管理员，再重试。
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
- **架构师助手的一轮对话以 `harness.incomplete_response`（"the agent run ended
  without a complete text response"）失败。** 现在它只表示字面含义：Harness 流结束时
  没有任何回复文本（为空，或在工具步骤后被截断），且没有错误帧。工具步骤之后
  Harness 侧的失败（模型 `ReadTimeoutError`、`ModelThrottledException`、
  `ContextWindowOverflowException`、`ValidationException`、bearer 过期）改为从
  AgentCore SDK 的终止错误帧中呈现：该轮会在新会话中重放一次（先等待 15 秒，
  `LAUNCHPAD_ASSISTANT_REPLAY_BACKOFF_SECONDS`，避免再次撞上同一个 5xx），`retry` 的
  reason 带有原因；第二次失败时转录中显示 `runtime returned error: <error_type>: …`
  （`assistant_turn_events` 日志中带 `detail.error_type`）。cn/r11-j 之前该帧被跳过，
  所有此类失败都显示为响应不完整。
- **评估运行在一次 `internalServerException` 之后以"完成但有错误"结束（评审记录上的
  `EventStreamError`，或某个场景 `failed after 1/3 attempts: RuntimeError: runtime returned
  error: An error occurred (internalServerException)…`）。** 自 cn/r12-f 起，评估 worker
  对场景重放和评审调用都会在瞬时上游错误（与架构师重放相同的判定：5xx / 限流 / 读超时，
  `runtime_invoke.is_upstream_transient`）上重试，最多 3 次尝试，之间等待
  `LAUNCHPAD_EVAL_TRANSIENT_RETRY_BACKOFF_SECONDS`（默认 `5,15`）。仍然失败的记录会列出
  每一次尝试：重放失败显示为 `failed after 3/3 attempts: … (retried after: #1 …, waited 5 s;
  #2 …)`，评审失败显示为 `judge <id> errored on every one of 3 attempts (transient upstream;
  waited 5 s, 15 s): EventStreamError: …`（记录的 `error_type` 为原因的类名）。等待列表在
  安装 / 修复 `launchpad-eval-worker` 预置时写入 worker；进程内执行器和 SCORE NOW 实时读取。
  若某条记录在此类错误上只尝试了一次就报错，说明 worker 早于 cn/r12-f——请执行修复。
- **主机更新后控制台页面空白，或某个功能（Monaco 编辑器、浏览器实时画面、视频播放）失效，
  DevTools 显示 `Refused to load … Content Security Policy`。** nginx 现在对每个响应都下发强制
  CSP 及其他浏览器安全头（`infra/ec2/nginx-site.sh`，2026-10-17 安全扫描）。新增的第三方 CDN、
  iframe 或非 `wss:` 的 WebSocket 需要把来源加进该文件的指令表（`infra/tests/test_nginx_site.py`
  会钉住它），再运行 `scripts/host-update.sh`。逐条指令的理由与回滚见
  `docs/cn/interfaces/deploy.md` §11。

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
- **cn-northwest-1 中的 TypeScript(Node.js)Agent 通过平台的导出层获得 trace
  (N17-obs459 / N17-obs465)。** ADOT Node(<= 0.13.0)无法在中国区为 trace 导出签名，因此对 Node
  容器 Agent，部署会在你的镜像之上加上平台的中国区 trace 导出层(默认开启；向导中的开关 /
  `byoc.cn_trace_exporter: false` 可关闭)。Agent 详情会显示你的镜像、实际部署的镜像以及 **Trace**
  徽标。运行时日志在启动时打印一行，其状态为：`attached`(导出已为 `.amazonaws.com.cn` 端点签名)、
  `native`(你的 ADOT 版本自身已支持签名——导出层不做任何事)、`skipped (no ADOT Node in the app)`
  (请添加 `@aws/aws-distro-opentelemetry-node-autoinstrumentation` 并以 `--require …/register` 启动)、
  `skipped (traces disabled by the app: OTEL_TRACES_EXPORTER=…)` / `skipped (… OTEL_SDK_DISABLED=true)`
  (你自己的设置优先——去掉它才会有 trace)、`unavailable (no exporting span processor: …)`(ADOT 没有
  安装任何会导出 span 的处理器)、`unavailable (<原因>)`(新的 ADOT 版本改变了导出层依赖的内部实现——Agent 照常运行，但没有 trace；
  请反馈)。"trace 未到达" 表示导出层已挂上但最新会话没有留下 span——请检查执行角色的 X-Ray / Logs 权限。
  仅限 trace:导出层会关闭 OTLP 日志导出器。见 evaluation-engine.md §33.7。

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
- **Studio 即原生画布。** 根目录生命周期在 `/create/studio` 提供原生双语画布;
  不存在需要单独运行的独立 Studio 应用。

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
