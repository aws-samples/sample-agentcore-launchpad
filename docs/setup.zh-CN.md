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

### 可选的控制台登录

控制台支持本地账户登录,不依赖 Cognito 或其他 AWS 服务。未配置密码时登录网关关闭,
此时控制台顶栏会显示 `AUTH OFF` 徽标。注意 `./start.py --prod` 会把两个服务都绑定到
`0.0.0.0`,因此任何对外可达的部署都必须开启网关:

```bash
export LAUNCHPAD_AUTH_USERNAME=admin
export LAUNCHPAD_AUTH_PASSWORD='replace-with-a-strong-password'
export LAUNCHPAD_AUTH_COOKIE_SECURE=true   # HTTPS 部署时开启
./start.py
```

会话使用 12 小时 HttpOnly Cookie。上述值也可写入 `config/launchpad.yaml` 的
`auth_username`、`auth_password`、`auth_cookie_secure`,遵循常规配置优先级;密码
建议放在进程环境变量中。修改凭证并重启后端会使已有会话失效。

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
