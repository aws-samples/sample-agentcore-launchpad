# 环境搭建 / Setup

English: [setup.md](setup.md)

## 前置条件

- 已在 `us-west-2` 开启 Bedrock AgentCore 预览的 AWS 账号（Runtime、Harness、
  Gateway、Policy、Evaluation）；Agent Registry 已 GA（自 2026-08-06 起使用
  `agent-registry` 命名空间，见 [registry-ga-migration.md](registry-ga-migration.md)），
  无需开启预览
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
| Cognito 用户池 | `launchpad-users`(+ 组 `platform-admin`、`hr-analyst`,演示用户 `admin`/`demo`) |
| IAM 执行角色 | `launchpad-agent-execution-role` |
| AgentCore Registry | `launchpad-registry` |
| AgentCore Memory | `launchpad_memory`(短期事件 + 语义、用户偏好、会话摘要、情节这四种长期策略;对已存在的 memory 重跑 bootstrap 会补齐缺失的策略) |
| AgentCore Gateway | `launchpad-gw-<suffix>` |
| 托管 AgentCore CLI | `data/agentcore-cli/` (`@aws/agentcore@0.21.1`) |

演示用户密码由 bootstrap 生成并存入 `config/launchpad.yaml`(**已 gitignore**——
视为本地机密;仓库中提交的是脱敏的 `config/launchpad.example.yaml`)。

### Policy 由用户显式管理

bootstrap 会创建共享 Gateway，但不会创建 Policy Engine、创建 Cedar Policy、挂载
Engine，也不会替用户选择 Gateway 的执行模式。重跑 bootstrap 时，已有 Policy 资源与
挂载关系同样保持不变。

需要 Policy 时，请在治理页面中显式纳管目标 Gateway，创建或选择 Engine，以默认
`ENFORCE` 或可选 `LOG_ONLY` 模式挂载。`ENFORCE` 默认拒绝，因此应在依赖 Gateway
流量前创建并审核明确放行的 Policy。

bootstrap 仍会为通用可观测性开启 CloudWatch Transaction Search，但不创建逐条 Policy
决策 span 所需的 per-Gateway CloudWatch Logs delivery。Policy 配置完成后，决策计数
仍来自 CloudWatch 指标；逐条决策明细需要另行管理 trace delivery。

## 本地运行

```bash
./start.py          # 后台开发模式
./start.py --prod   # 构建并运行本地生产预览
./stop.sh
```

需要绑定当前终端的前台开发栈时,使用 `make dev`。

本文档中提到的每一个配置项，都既可以写成 `config/launchpad.yaml` 里的 key，也可以
写成同名大写加 `LAUNCHPAD_` 前缀的进程环境变量（`database_url` →
`LAUNCHPAD_DATABASE_URL`）。优先级为：默认值 < `config/launchpad.yaml` < 环境变量 <
init kwargs，因此环境变量总是压过 bootstrap 生成的那份文件。有三个 key 决定后端本身
如何启动：

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `database_url` | `sqlite:///<repo>/data/launchpad.db` | 台账的 SQLAlchemy URL。默认是 `data/` 下的单个 SQLite 文件；它只存标识符与派生进度——AWS 始终是权威状态——所以丢掉台账损失的是控制台历史，而不是资源本身。改这个值即可迁移文件位置。 |
| `cors_origins` | `["http://localhost:5173", "http://127.0.0.1:5173"]` | 允许调用 `/api` 的浏览器 origin。默认值覆盖开发态前端的两种 loopback 写法；当控制台由别的主机或端口提供时，需要把自己的 origin 加进来。`./start.py --prod` 让控制台与 API 同源，无需新增条目。 |
| `agentcore_read_timeout_s` | `1000` | AgentCore 数据面客户端的 boto 读超时（`backend/app/services/agentcore/client.py:30`）。AgentCore 同步调用**最长可运行 15 分钟**，因此该值必须高于这条服务侧上限——botocore 默认的 60 秒会在缓冲式 agent 返回最终响应之前就放弃。 |

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

只有两个角色,自 2026-08-11 起两者只差一处:**用户管理**(`/users`)仅限管理员,
**控制台的其余全部功能对成员开放** —— 注册表的注册/编辑/导入、知识库变更、评估
数据集/评估器/运行、AB 实验与金丝雀、Cedar 策略写入、API Key、Studio 画布,以及
浏览器 / 代码解释器 demo。权威清单是 `backend/app/core/route_policy.py` 里的表;
未登记的路由会被拒绝而不是放行。(本地代码执行在生产模式下对所有角色一律禁用,
除非用 `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED` 显式打开。)

仍有一小组能力**可在用户管理控制台按用户撤销**:智能体生命周期(部署、导入、删除、
转换)以及发起评估/洞察运行。成员默认全部持有;撤销后即可对该账号关闭部署与计费的
评估任务。

注意控制台没有按用户的数据隔离:每个成员看到并可修改同一套共享资源,因此成员账号
只应发给可以托付整个环境的人。

### 系统托管预置

创建页的**系统预置**面板列出平台自有的预置（目前是托管 Harness
`aws-agent-solution-architect`）。引导、启动和任何页面读取都不会安装它：由**管理员**在每个
Workspace 中通过面板的“安装”按钮（或 `POST /api/system-agents/<key>/install`）显式安装，这是
一次走标准管道的计费部署（约 30 秒）。Workspace 必须处于 `ready`（引导完成，具备
`artifacts_bucket` 与 `execution_role_arn`）。可选安装项：模型（`model_id`/`model_source`，
默认为平台默认值）与要挂载的既有知识库（`knowledge_bases`）——不会替你创建任何知识库。
“修复/更新”就地重新发布；“卸载”删除 Harness 与角色。成员可以像普通 Agent 一样与预置对话，
但会看到“系统”标签，编辑/重新发布/转换/删除按钮被禁用，后端也会无视权限拒绝这些调用。若已有
普通 Agent 占用保留名称，安装会被拒绝——请先删除或重命名该 Agent；预置绝不接管它。
**实机冒烟尚未完成**（见 architecture.zh-CN.md →“系统托管预置”）。

### 应急开关

| 变量 | 效果 |
|---|---|
| `LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` | 在可达网络接口上提供未认证的控制台。恢复硬化前的行为,仅可用于可信网络。 |
| `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` | 在生产模式下重新启用本地代码执行(见下)。 |
| `LAUNCHPAD_STUDIO_EXEC_BACKEND=docker` | 让本地调试代码在一次性 Docker 容器里运行而非宿主子进程 —— 同时也使这些端点在生产模式可用(见下)。 |
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

#### Docker 沙箱后端

另一种方式是让生成代码在**一次性 Docker 容器**里运行,而不是宿主子进程:

```bash
scripts/setup_exec_docker.sh          # 构建 launchpad-studio-exec:latest
export LAUNCHPAD_STUDIO_EXEC_BACKEND=docker   # 或在 launchpad.yaml 里写 studio_exec_backend: docker
```

每次运行都是一个全新容器:`--cap-drop ALL`、`no-new-privileges`、只读根文件系统
(tmpfs `/tmp`)、同一套环境白名单,内存 / CPU / 进程数 / 文件大小上限映射为 docker
参数。超时会击杀容器本身,后端启动时还有一个清扫器回收崩溃遗留的
`strands-exec-*` 容器。单次运行开销约 0.3 秒。

这些上限以及容器所用的镜像都是配置项：

| 配置项 | 默认值 | 对应 docker 参数 |
|---|---|---|
| `studio_exec_memory_mb` | `2048` | `--memory` 与 `--memory-swap`，两者取同一个值，使容器无法靠 swap 绕过限制 |
| `studio_exec_cpu_seconds` | `300` | `--ulimit cpu=<n>:<n>`——限制的是消耗的 CPU **秒数**，不是墙上时间；墙上时间由 `execute_timeout_s` 约束 |
| `studio_exec_max_processes` | `64` | `--pids-limit` |
| `studio_exec_max_file_mb` | `256` | `--ulimit fsize=<字节数>`——生成代码可写出的最大单文件 |
| `studio_exec_docker_image` | `launchpad-studio-exec:latest` | `docker run` 启动的镜像；只有当你用别的名字构建沙箱镜像时才需要改 |

同样这四项上限也作用于**子进程**后端，只不过在那里是 fork 后在子进程里下调的
`resource.RLIMIT_*`，而不是 docker 参数（`backend/app/services/local_exec.py`：
`_docker_run_argv` 从第 380 行开始拼装 docker 参数，rlimit 列表在第 557 行附近构造）。
有一处不对称：`studio_exec_max_processes` 映射到 `RLIMIT_NPROC`，而它是**按 uid**
统计进程与线程的，因此子进程后端只在配置了 `studio_exec_user` 时才施加该上限——落在
后端自己的 uid 上，它会把你整个登录会话都算进去，导致子进程每次创建线程都失败。

由于代码不再运行在控制面主机上,**选择 docker 后端本身就是生产环境的 opt-in**:
`run_mode=prod` + `studio_exec_backend=docker` 即可提供本地调试端点,不再需要
`LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true`(显式设为 `false` 仍然是总开关)。
前提:后端用户能访问 docker daemon(`docker` 组),且已用上面的脚本构建镜像。

有一道边界**不是**白来的:EC2 上 IMDS hop limit ≥ 2 时(这些机器的默认值),默认
bridge 网络里的容器仍能访问实例元数据服务 —— 这也正是默认 Mantle 路径无需 API key
就能工作的原因。要得到真正无凭证的沙箱:

```bash
sudo scripts/setup_exec_docker.sh --harden-net   # 仅 Linux
```

它会创建专用的 `launchpad-exec` bridge 网络,并加一条 `DOCKER-USER` iptables 规则
禁止该网段访问 `169.254.169.254`,然后打印需要补上的配置项
(`studio_exec_docker_network`,以及 `studio_exec_forward_aws_credentials: false`,
Mantle 权衡同上)。如果配置了无凭证姿态却没有配置该网络,后端会直接拒绝,而不是
假装隔离。

更深一层的沙箱(迁移到 AgentCore Code Interpreter)尚未实现;docker 后端是推荐的
中间档。

#### 代码生成（AI Fix）

Studio 的 **AI Fix** 会把执行失败的流程的生成代码、traceback 与校验错误交给一个编码
agent，由它在临时工作目录里重写 `generated_agent.py`，再通过 SSE 回传。控制它的有四个
配置项：

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `codegen_backend` | `claude` | 由哪个编码 agent 执行修复。目前只注册了 `claude`（Claude Agent SDK）；填入未注册的名字会让请求直接失败并列出已注册的名字，而不是静默回退。 |
| `codegen_model` | `global.anthropic.claude-sonnet-5` | 该编码 agent 使用的模型。更强的模型单次修得更多，单次也更贵。 |
| `codegen_timeout_s` | `180.0` | 一次 AI Fix 请求的端到端预算，含全部修复轮次。如果修复总在 agent 写完文件之前被打断，就调大它。 |
| `codegen_max_repair_rounds` | `2` | 一次重写最多可以被重新校验并重试几轮。每多一轮就是对首次修复仍无法导入的流程多发一次模型调用。 |

### Skill Lab

Skill Lab 负责评估与训练 Registry 里的技能记录：vendored 的 SkillOpt CLI 以**子进程**
形式跑在后端主机上，每个任务的 agent rollout 跑在 `launchpad_skill_lab_worker` 上各自
独立的 AgentCore Runtime microVM 会话里，LLM 判分器则直接调用 Bedrock。
`make bootstrap` 会把两半都开通好——专用解释器与 worker 镜像——所以下面这些配置项调的是
一个已开通的 Skill Lab，而不是开关。

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `skill_lab_python` | `<repo>/data/skill-lab-venv/bin/python` | 运行 vendored 的 `evaluate_skill.py` / `train.py` / 任务集校验器的解释器。bootstrap 依据 `vendor/skillopt/requirements-launchpad.txt` 构建它，并在该文件变化时重建；后端进程自身永不 import vendored 目录树。该路径缺失时 `GET /api/skill-lab/status` 会返回 `venv_ready: false`——这就是未开通的 workspace 的样子。 |
| `skill_lab_max_concurrent_jobs` | `1`（1–4） | 同时运行的评估/训练任务数。每个任务是一个 CLI 子进程外加它自己的 worker 会话，因此这是在墙上时间与主机 CPU/内存、worker runtime 压力之间取舍；超出的任务排队而不是失败。 |
| `skill_lab_judge_model_id` | `us.openai.gpt-5.6-sol` | 给 rollout 打分的模型。必须是 Bedrock 的 **Converse inference-profile id**——`bedrock_chat` 判分器会拒绝裸 model id。它同时按模型家族决定 agentic 判分器使用的宿主 CLI：`openai.*` 的 id 会去掉 profile 前缀后路由到宿主的 `codex`，其余路由到宿主的 `claude`（`runner.judge_exec_route`）。因此改这一项也就改变了宿主必须安装哪个 CLI。 |
| `skill_lab_target_model_id` | `global.anthropic.claude-opus-5` | `claude_code_exec` 目标后端下被测技能默认运行的模型，同样是 Converse inference-profile id。逐任务参数可以覆盖它；空值永远不会被下发，因为空的 `--model` 会让 vendored CLI 换上它自己的非 Bedrock 默认值。 |
| `skill_lab_codex_target_model_id` | `openai.gpt-5.6-sol` | `codex_exec` 目标后端下的同一个默认值，但它是 Bedrock 的**目录 slug**而不是 Converse profile id：codex 通过自己配置里内置的 `amazon-bedrock` provider 自行解析模型。单独设一个 key，是为了让两个后端永远不会拿到对方的 id 家族。 |
| `skill_lab_codex_catalog_path` | `~/.codex/model-catalogs/bedrock-models.json` | 构建时从后端主机读取、并塞进 worker 镜像 codex-home 的 Bedrock 模型目录。该文件内嵌了专有的模型指令，因此从不入库；缺失时构建会塞一个空的 `{}` 目录并在日志里写明——那个镜像上的 codex 目标也就没有目录可供解析。 |
| `skill_lab_judge_sandbox` | `bwrap` | agentic 判分器的产物解析器所用的沙箱启动 argv（按 shlex 切分），它跑在后端**主机**上——worker microVM 无法运行 bubblewrap。若主机上非特权 `bwrap` 被 AppArmor 拦住，改成 `sudo -n bwrap`。`GET /api/skill-lab/status` 会探测该 argv 的第一个词来给出 `agentic_judge_ready`，vendored 那层 fail-closed 的边界校验仍然叠加生效。 |
| `skill_lab_worker_cli_version` | `2.1.234` | 报告 worker 镜像里内置的 `claude` CLI 版本。 |
| `skill_lab_worker_codex_version` | `0.147.0` | 报告 worker 镜像里内置的 `codex` CLI 版本。 |

最后两个 `*_version` 是**镜像值，不是输入值**。真正生效的是
`vendor/skillopt/deploy/agentcore/Dockerfile` 里 `ARG CLAUDE_CLI_VERSION` /
`ARG CODEX_CLI_VERSION` 的默认值——被镜像内容哈希覆盖的是那个 Dockerfile——这两个配置项
只用于控制台展示。**必须成对修改：**`backend/tests/test_skill_lab_foundation.py` 会断言
两边一致，只改一边会让 `make verify` 失败。版本号变化同时会改变构建上下文的哈希，下一次
bootstrap 正是据此判断要重建并重新推送 worker 镜像。

### 提示词优化

实验的 RECOMMEND 阶段会让第三方 provider（`backend/app/optimization/providers`）依据某次
被固定的评估运行中得分最差与最好的会话，改写 agent 的系统提示词与工具描述。这里的 model
id 都是 Bedrock Converse 的 inference-profile id，并和其他调用一样走同一个 workspace
客户端漏斗。

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `prompt_opt_models` | `["global.anthropic.claude-opus-5", "global.anthropic.claude-sonnet-5", "global.anthropic.claude-sonnet-4-6", "us.openai.gpt-5.6-sol"]` | 控制台提供选择的反思模型清单。想让某个 id 可选，就加到这里。 |
| `prompt_opt_default_model_id` | `global.anthropic.claude-opus-5` | 其中哪一个排在最前、并在请求未指定模型时被使用。 |
| `prompt_opt_max_sessions` | `30`（3–100） | provider 从被固定的那次运行里读取多少个会话（最差优先，另加一组得分最好的对照）。读得少更便宜更快，读得多则给反思更多可归纳的证据。 |
| `prompt_opt_max_tokens` | `8192`（512–16000） | 反思调用的输出预算。含提示词与若干工具描述的双组件反思实测超过了 4096；响应被截断时 provider 会自行把该值翻倍一次。 |
| `prompt_opt_read_timeout_s` | `900` | 反思调用的读超时。该调用是流式的，因此它约束的是两个数据块之间的间隔而不是总时长。botocore 默认的 60 秒在这里远远不够：生产环境里大模型处理 30 个会话时超过了它，botocore 静默重发了整个请求五次才最终失败。 |

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
边缘。参考部署(workshop EC2 + CloudFront)的单元文件与实测过的更新流程见
[agent-runbook-prod.md](agent-runbook-prod.md#3-shape-b--systemd-reference-the-us-east-1-box),其拓扑为:

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
