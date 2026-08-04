# 环境搭建 / Setup

English: [setup.md](setup.md)

## 前置条件

- 已在 `us-west-2` 开启 Bedrock AgentCore 预览的 AWS 账号(Runtime、Harness、
  Registry、Gateway、Policy、Evaluation)
- 具备管理员级别权限的凭证(`aws sts get-caller-identity` 可用)
- `uv` ≥ 0.8、Node.js ≥ 20(`npm`)、AWS CDK CLI v2(`npm i -g aws-cdk`)、
  Docker(支持 ARM64,第 5 阶段容器路径需要)
- 每个账号/区域执行一次 CDK bootstrap:`cdk bootstrap aws://<account>/us-west-2`

## 引导(Bootstrap)

```bash
# 1. 安装依赖
cd backend  && uv sync && cd ..
cd frontend && npm install && cd ..
cd infra    && uv sync && cd ..

# 2. 部署共享基础设施 + AgentCore 单例,写出 config/launchpad.yaml
make bootstrap          # = cd backend && uv run python ../scripts/bootstrap.py
```

该引导是**幂等**的:CDK 栈(`launchpad-base`)仅在缺失时部署,AgentCore 注册表
(`launchpad-registry`)/ memory(`launchpad_memory`)只创建**一次**,后续每次运行
都复用。再次运行只会打印 `reused`,不会产生重复资源。

bootstrap 还负责安装 Harness 转 Runtime 时使用的 CLI。它会把固定版本
`@aws/agentcore@0.21.1` 安装到
`data/agentcore-cli/node_modules/.bin/agentcore`,不需要全局 npm 安装;安装后会校验
版本,后续运行直接复用。转换过程不会使用 `PATH` 中的 `agentcore`;如果这份托管安装
被删除或不可用,重新运行 `make bootstrap`。该版本同时支持不带 Skill 的 Harness
导出,以及生成代码调用
`get_or_create_agent(session_id, user_id, _skill_plugins)` 的 Skill 导出。

创建内容:

| 资源 | 名称 |
|---|---|
| S3 产物桶 | `launchpad-artifacts-<acct>-<region>` |
| ECR 仓库 | `launchpad-agents` |
| CodeBuild(ARM64) | `launchpad-agent-builder` |
| Cognito 用户池 | `launchpad-users`(+ 组 `platform-admin`、`hr-analyst`,演示用户 `river`/`demo`) |
| IAM 执行角色 | `launchpad-agent-execution-role` |
| AgentCore Registry | `launchpad-registry` |
| AgentCore Memory | `launchpad_memory`(短期事件 + 语义与用户偏好的长期策略) |
| 托管 AgentCore CLI | `data/agentcore-cli/` (`@aws/agentcore@0.21.1`) |

演示用户密码由 bootstrap 生成并存入 `config/launchpad.yaml`(**已 gitignore**——
视为本地机密;仓库中提交的是脱敏的 `config/launchpad.example.yaml`)。

### 策略 span 通道

bootstrap 还会为 Gateway 打开 AgentCore **策略决策 span** 通道。AgentCore 只在
挂载的 Gateway 上启用了 *trace 投递* 之后才会发这些 span,而这是一个 CloudWatch
vended-log delivery,不是 Gateway 的配置项——所以**不会修改任何 Gateway 资源**:

| Delivery 资源 | 名称 |
|---|---|
| Delivery source(`logType=TRACES`) | `<gateway-id>-traces-source` |
| Delivery destination(`XRAY`) | `<gateway-id>-traces-destination` |

span 随后落到共享的 `aws/spans` 日志组。这一步依赖 CloudWatch Transaction Search,
bootstrap 会先启用它;若它未启用则跳过这一步,summary 报
`gateway_traces: skipped · transaction_search_disabled`。

这一步是幂等的(重跑报 `present`),而且**永远不会让 bootstrap 失败**——不值得为一条
遥测投递中断引导。看 summary 里的 `gateway_traces`:`failed` 会带上 AWS 错误码,
通常是缺 IAM 动作。操作者凭据需要:

```
logs:GetDeliverySource      logs:PutDeliverySource
logs:GetDeliveryDestination logs:PutDeliveryDestination
logs:DescribeDeliveries     logs:CreateDelivery
```

注意:策略决策的**计数**(治理 → 决策的证据视图、以及切换门禁)来自 CloudWatch 指标,
完全不需要这些——它们不用任何启用就能工作。span 通道只是额外提供逐条决策明细。

`scripts/teardown.py` 有意不删这条 delivery,正如它也不删 Gateway 与策略引擎。
手工清理:

```bash
aws logs describe-deliveries --region us-west-2   # 找到 id
aws logs delete-delivery --region us-west-2 --id <delivery-id>
aws logs delete-delivery-source --region us-west-2 --name <gateway-id>-traces-source
aws logs delete-delivery-destination --region us-west-2 --name <gateway-id>-traces-destination
```

## 本地运行

```bash
./start.py          # 后台开发模式
./start.py --prod   # 构建并运行本地生产预览
./stop.sh
```

需要绑定当前终端的前台开发栈时,使用 `make dev`。

### 控制台登录

控制台支持本地账户登录,不依赖 Cognito 或其他 AWS 服务。未配置密码时登录网关关闭,
此时控制台顶栏会显示 `AUTH OFF` 徽标。

**未认证的控制台只响应 loopback 调用方。** `./start.py --prod` 会把两个服务都绑定到
`0.0.0.0`,因此对外可达的部署必须配置密码;未配置时,来自任何非 loopback 地址的
`/api` 请求都会被拒绝(`auth.open_console_refused`),并且 `./start.py` 会在启动前
检查失败而不是继续拉起服务。`/api/health` 与登录端点保持可达,以便被挡在外面的
运维仍能看到登录门禁。

```bash
export LAUNCHPAD_AUTH_USERNAME=admin
export LAUNCHPAD_AUTH_PASSWORD='replace-with-a-strong-password'
./start.py
```

会话使用 12 小时 HttpOnly Cookie。生产模式(`run_mode: prod`,`./start.py --prod`
会设置)下自动带上 `Secure` 并发送 HSTS 响应头;`LAUNCHPAD_AUTH_COOKIE_SECURE=true`
可在开发模式下强制开启。**两者都要求全链路 HTTPS** —— `Secure` Cookie 不会经明文
HTTP 回传,所以若 TLS 在某处终止后再以 HTTP 转发,登录会静默失败。

上述值也可写入 `config/launchpad.yaml` 的 `auth_username`、`auth_password`、
`auth_cookie_secure`,遵循常规配置优先级;密码建议放在进程环境变量中。修改凭证并
重启后端会使已有会话失效。

### 角色:成员能做什么

只有两个角色。`admin` 拥有整个控制台。`member` **实际上是只读的**:浏览智能体、
注册表记录与知识库,与智能体对话、调用,使用检索 playground,以及查看可观测性、
记忆、评估与治理。

所有会执行代码、改变已部署或云端状态、签发凭证、或改变治理策略的操作都仅限管理员
—— 创建与部署智能体、Studio 画布、注册表的注册/编辑/导入、知识库变更、API Key、
Cedar 策略写入,以及浏览器 / 代码解释器 demo。权威清单是
`backend/app/core/route_policy.py` 里的表;未登记的路由会被拒绝而不是放行。

这个限制是刻意的:控制台尚无按用户的数据隔离,因此一个能部署的成员同时也能看到并
修改其他所有成员的资源。

### 应急开关

| 变量 | 效果 |
|---|---|
| `LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` | 在可达网络接口上提供未认证的控制台。恢复硬化前的行为,仅可用于可信网络。 |
| `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` | 在生产模式下重新启用本地代码执行(见下)。 |
| `LAUNCHPAD_AUTH_COOKIE_SECURE=false` | 当控制台前面实际未终止 TLS 时,去掉 `Secure`。 |

没有任何开关可以关闭角色授权:能关掉授权的开关本身就是漏洞。要修正误分类的路由,
请直接改 `route_policy.py`。

### 按 Agent 的执行角色

每个已部署的 agent 都获得一个由其 spec 派生的独立 IAM 执行角色,而不是所有 agent 共用
一个 `launchpad-agent-execution-role`。目的在于隔离:在共享角色下,任何 agent 都能挂载
其他任意 agent 的文件系统、读取所有 agent 的 skill 包、检索账号内任意知识库,并改写
gateway 路由。

角色命名为 `launchpad-agent-{name}-{agent-id 前缀}`,并打上 `launchpad:agent-id` 标签。
它们在 `provision` 阶段创建,在重新发布时对齐(被去掉的能力会让策略收缩),并随 agent
一起删除。

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `per_agent_execution_roles` | `true` | 设为 false 可回退到共享角色。 |
| `agent_role_count_warn_threshold` | `800` | 达到该数量后开始告警。 |

**IAM 默认配额是每账号 1000 个角色**,本特性按 agent 线性消耗。demo 规模下不成问题,
但真撞上配额时,表现会是一次莫名其妙的部署失败。

**已有 agent 仍在共享角色上正常运行。** 迁移方式是**重新发布**,而不是手写
`UpdateAgentRuntime`——后者会重置未传的字段,从而静默清掉文件系统挂载、protocol 配置或
环境变量。用下面的命令查看还有哪些未迁移:

```bash
cd backend && uv run python scripts/migrate_agent_roles.py
```

请**先**运行 `scripts/migrate_pin_requirements.py --apply`:重新发布会重新校验 spec,
未固定版本的依赖现在会被拒。

> **为什么共享角色仍然存在、且仍带着宽泛授权。** 它支撑那些尚未重新发布的 agent。在所有
> agent 迁移完成前缩减它,会直接抽掉仍在使用它的 agent 的授权。这项缩减刻意尚未执行;
> 信任策略里的 `aws:SourceArn` 条件同样尚未启用(需先探明 AgentCore 是否会发送该 key)。
> 另外注意 per-agent 角色**不**提供什么:记忆仍是单一共享实例、按 actor id 分区而非按
> IAM 隔离,而账号的 1000 个角色配额现在按每个 agent 一个的速度消耗。

**部署成功并不能证明策略正确。** 这些策略是按 spec 收窄的,而过紧的语句会在**调用**时
失败,而不是部署时。迁移之后,请逐个调用 agent 并检查 CloudTrail 是否出现
`AccessDenied`。

### 依赖与镜像供应链

**依赖必须固定版本。** `spec.requirements` 的每一项都必须指向唯一且不可变的产物——
`name==version`、带 `#sha256=` 的直链 URL,或
`pkg @ git+https://…@<40 位 commit>`。范围写法会在校验阶段被拒,错误信息里会给出
要求的形式。平台自带的依赖清单刻意保留范围;可复现性来自下面的 lockfile,而不是把
它们手工 pin 死。

已有 agent 可能早于该规则。在它们下一次部署之前不会有任何影响,可用下面的命令检查:

```bash
cd backend && uv run python scripts/migrate_pin_requirements.py
cd backend && uv run python scripts/migrate_pin_requirements.py --apply
```

同一脚本还会列出没有记录 commit 的 git skill 记录;这些需要从注册表**重新导入**,
因为拿到 commit SHA 必须发起一次拉取。

**每次 zip 构建都会锁定。** package 阶段用 `uv pip compile --generate-hashes` 针对
部署目标(aarch64、Python 3.13)解析依赖,再以 `--require-hashes` 安装,因此被替换或
重新上传过的发行包会让构建失败而不是被打包进去。lock 文件以 `requirements.lock` 随
部署 zip 一起下发。**因此后端在部署时需要 PATH 上有 `uv` CLI 且能访问包索引**;解析
失败就是部署失败——不会退回到未校验的安装路径。

**容器镜像会被扫描,并按 digest 部署。** ECR 在推送时扫描,package 阶段在镜像存在
达到或超过 `image_scan_block_severities` 的漏洞时拒绝继续:

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `image_scan_enabled` | `true` | 设为 false 可跳过该闸门(任务日志会写明镜像未被扫描)。 |
| `image_scan_block_severities` | `["CRITICAL"]` | 会阻断部署的严重级别。 |
| `image_scan_timeout_s` | `300` | 等待扫描的时长;超时会被记录,不会当作"干净"。 |

部署引用的是不可变的镜像 digest,而不是 `{agent}-v{version}` 标签,并且 digest 会记录
在该次部署上。镜像标签刻意保持**可变**:打包发生在版本号递增之前,因此重新发布会把
同一个标签推送两次,不可变标签策略会让第二次推送失败。

> **两套环境都要应用。** scan-on-push 是 CDK 改动,所以 `make bootstrap` 需要在
> `us-west-2` **和** `us-east-1` 主机上各跑一次。在此之前,那台主机上的闸门会报告
> 无法读取扫描结果。

> **默认阈值第一天就会拦住部署。** 对现役 demo 镜像实测扫描得到 **4 个 CRITICAL**,
> 全部是 Debian 基础镜像里未修补的 OS 包(`glibc`、`perl`),而不是本项目安装的任何东西
> —— 也就是说在 `["CRITICAL"]` 默认值下,一旦开启扫描,容器部署会一直被拦到基础镜像
> 发布修复为止。拦截消息会点名 CVE 与包名,便于判断责任方。请在三者之间明确取舍:换用
> 更新的基础镜像重建、把 `image_scan_block_severities` 放宽为 `[]`(仅报告:每次部署都
> 记录 finding,但不拦截)、或接受被拦截。

未实现:SBOM 生成、构建 provenance/attestation、镜像签名、受信镜像源强制,以及 skill
**内容**审查。固定版本让来源不可变,并不等于可信。

### 本地代码执行

Studio 本地调试端点(`/api/execute`、`/api/execute/stream`,以及
`/api/conversations` 多轮对话面)会在**服务器上运行调用方提供的 Python**。因此它们
在**生产模式下默认禁用**,Studio 本地调试与 AI Fix 在生产环境不可用。设置
`LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` 表示接受该风险。

开发模式下子进程会拿到一份清洗过的环境(白名单,因此账本 URL、`LAUNCHPAD_*` 配置
以及你 shell 里的密钥都不会进去),外加内存 / CPU / 进程数 / 文件大小上限。

但它默认仍以后端用户身份运行,并且**仍能拿到你的 AWS 凭证**。把
`studio_exec_forward_aws_credentials` 设为 `false` 会让凭证不进入子进程环境,并设置
`AWS_EC2_METADATA_DISABLED=true` —— 这足以让 AWS SDK 与 CLI 不再取用实例角色,但该
变量只是 SDK 约定,不是边界。在 EC2 上凭证走网络,所以**自己去访问
`169.254.169.254` 的代码依然能拿到**。在 EC2 上实测:环境已清洗的情况下,约 20 行
`urllib` 仍取回了有效的实例角色密钥。要封住这一点:

```bash
sudo scripts/setup_exec_env.sh --hardened   # 仅 Linux
```

该命令会创建一个专用的非特权账户,并加一条**按该 uid 限定**的防火墙规则禁止它访问
元数据端点,然后打印需要补上的两个配置项。同一段探测代码在该账户下会超时失败。

**前置条件:**把子进程切换到另一个账户需要特权,因此 `studio_exec_user` 只在**后端
自身以 root 运行**时才生效。`make dev` 和 `start.py` 都以你自己的账户运行后端,此时
降权会失败 —— 所以执行端点会直接返回 `studio.exec.user_unavailable`(503)而不是执行
到一半才报错;你要么让后端以 root 运行,要么把 `studio_exec_user` 留空(即 tier 1:
只有资源上限与环境清洗)。

注意脚本同时说明的权衡:默认的 Bedrock Mantle 路径依赖 ambient 凭证来签发 bearer
token,因此一个无凭证的子进程要求每次本地调试请求显式带上
`bedrock_api_key` / `openai_api_key`。

完整沙箱(非 root 容器、seccomp、受限出网)**尚未**实现;在生产环境,禁用该端点就是
对应的缓解措施。

### 自助注册与用户管理

登录网关开启后,登录页同时提供**注册**:填写用户名、**公司邮箱**和密码提交申请。
默认情况下新账户处于 **`pending`(待审批)**,必须由管理员审批通过后才能登录,
**7 天有效期从审批时开始计算**。上面配置的内置 admin 不入库,因此永远不会被锁在
控制台之外。

公共/临时邮箱域名(Gmail、QQ、163、Outlook、mailinator 等)会被拒绝。相关配置:

```bash
export LAUNCHPAD_AUTH_REGISTRATION_ENABLED=true          # false 关闭注册
export LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL=true # false 则注册即生效
export LAUNCHPAD_AUTH_REGISTRATION_VALID_DAYS=7          # 审批通过后授予的有效期
# 白名单非空时优先生效,否则使用内置黑名单
export LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS='["your-company.com"]'
export LAUNCHPAD_AUTH_BLOCKED_EMAIL_DOMAINS='["gmail.com","qq.com"]'
```

admin 账号会看到**用户管理**模块(`/users`):审批队列(「待审批」统计卡片 +
`PENDING` 筛选 + 每行的**通过**/**拒绝**)、注册统计,以及逐账户操作(延期 +7/+30/
自定义天数或指定到期时间、禁用/启用、修改角色、重置密码(仅显示一次)、删除)。
到期与禁用在每次请求时校验,账户会**立即**失去控制台访问权限,无需等待会话
Cookie 过期。

## 生产部署

`./start.py --prod` 只是本地预览:构建前端、提供构建产物、关闭后端自动重载,并绑定到
`0.0.0.0`。长期运行的主机应改用进程管理器托管这两个服务,并在前面放一层终结 TLS 的
边缘。参考部署(workshop EC2 + CloudFront)的完整规格见
`.trellis/spec/launchpad/remote-production-deployment.md`,其拓扑为:

```text
浏览器 → CloudFront(TLS、不缓存、放通全部方法、注入一个密钥请求头)
           └─ 实例上的 nginx :80 —— 缺少该请求头的请求直接拒绝
                ├─ /api/、/v1/ → 127.0.0.1:8000   (后端,SSE 需要 proxy_buffering off)
                └─ /、/assets/ → 127.0.0.1:5173    (vite preview 提供 frontend/dist)
```

**1. 托管两个进程。** 认证配置写在后端单元里,没有别的东西会替你开启网关:

```ini
# /etc/systemd/system/launchpad-backend.service   (节选)
[Service]
WorkingDirectory=/home/ubuntu/workspace/agentcore_launchpad/backend
Environment=LAUNCHPAD_RUN_MODE=prod
Environment=LAUNCHPAD_AUTH_USERNAME=admin
Environment=LAUNCHPAD_AUTH_PASSWORD=<strong-password>
Environment=LAUNCHPAD_AUTH_COOKIE_SECURE=true
ExecStart=/home/ubuntu/.local/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
```

```ini
# /etc/systemd/system/launchpad-frontend.service  (节选)
[Service]
WorkingDirectory=/home/ubuntu/workspace/agentcore_launchpad/frontend
Requires=launchpad-backend.service
ExecStart=/usr/bin/npm run preview -- --host 127.0.0.1 --port 5173 --strictPort
Restart=on-failure
```

`vite preview` 提供的是 `frontend/dist`,所以**前端每次改动都必须先
`npm run build` 再重启**。两个进程都绑定 `127.0.0.1`,对外只暴露反向代理。

**2. 封闭 origin。** CloudFront 注入一个自定义请求头(如
`X-Launchpad-Origin-Key`),nginx 拒绝不带该头的请求,这样直连实例公网 IP 无法绕过
CDN:

```nginx
if ($http_x_launchpad_origin_key != "<shared-secret>") { return 403; }
proxy_set_header X-Forwarded-Proto https;   # TLS 在 CloudFront 终结
```

由于 TLS 在边缘终结,`LAUNCHPAD_AUTH_COOKIE_SECURE=true` 必须保持开启;纯 HTTP 下
浏览器会丢弃会话 Cookie。

**3. 更新已有主机。**

```bash
cp data/launchpad.db data/launchpad.db.bak-$(date +%Y%m%d-%H%M)
git merge --ff-only origin/main
cd backend && uv sync && cd ..
cd frontend && npm run build && cd ..          # 必须:preview 只吃 dist/
sudo systemctl restart launchpad-backend launchpad-frontend
curl -s localhost:8000/api/auth/status          # 预期 auth_required: true
```

新增的台账表(如 `users`)会在后端启动时自动创建,无需迁移步骤。网关一开启,注册
就是开放的 —— 如果不希望任何拿到 URL 的人都能提交申请,请设置
`LAUNCHPAD_AUTH_REGISTRATION_ENABLED=false` 或用
`LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS` 限定公司域名。

## 资源清理

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # 列出将被移除的内容
uv run python ../scripts/teardown.py --yes        # 删除(memory → registry → CDK stack)
```

删除是尽力而为、依赖方优先的;S3 桶自动清空,ECR 仓库随栈强制删除。更完整的
清理指南(演示资源 vs 共享基础设施)见 [teardown.zh-CN.md](teardown.zh-CN.md)。
