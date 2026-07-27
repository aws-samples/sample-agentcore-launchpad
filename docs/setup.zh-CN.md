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

## 本地运行

```bash
./start.py          # 后台开发模式
./start.py --prod   # 构建并运行本地生产预览
./stop.sh
```

需要绑定当前终端的前台开发栈时,使用 `make dev`。

### 可选的控制台登录

控制台支持本地账户登录,不依赖 Cognito 或其他 AWS 服务。未配置密码时登录网关关闭:

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

## 资源清理

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # 列出将被移除的内容
uv run python ../scripts/teardown.py --yes        # 删除(memory → registry → CDK stack)
```

删除是尽力而为、依赖方优先的;S3 桶自动清空,ECR 仓库随栈强制删除。更完整的
清理指南(演示资源 vs 共享基础设施)见 [teardown.zh-CN.md](teardown.zh-CN.md)。
