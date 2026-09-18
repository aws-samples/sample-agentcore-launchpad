# 第 13 章 · 部署你自己的代码（BYOC · 自带代码）

> **目标**：把你（或你的开发者）自己编写的 Agent 代码部署到 AgentCore Runtime——开发者全程不需要任何 AWS/IAM 权限。
>
> **前置条件**：完成[第 01 章](01-environment.md)；本地能运行 `zip` 命令。
>
> **本章将创建的 AWS 资源**：1 个 AgentCore Runtime、1 个按 Agent 的 IAM 执行角色、制品桶中的上传对象（`container_source` 还会产生 1 次 CodeBuild 构建与 1 个 ECR 镜像标签）。

---

## 13.0 什么时候用 BYOC

前几章的三种方式都由平台生成代码。当你的团队已经用 Claude Code / Codex 写好了
Agent——用 `bedrock-agentcore` SDK（`BedrockAgentCoreApp` + `@app.entrypoint`）
包装，或自带一个满足契约的 HTTP 服务——BYOC 让管理员直接上传部署，而不必给
开发者发 IAM 凭证。

**运行时契约**（可在向导里展开"运行时契约"帮助框查看）：

| 项 | 要求 |
|---|---|
| 架构 | ARM64（aarch64） |
| 端口 | 8080 |
| 路由 | `POST /invocations` + `GET /ping` |
| 调用负载 | `{"prompt": "...", "actor_id": "..."}` |
| zip 上限 | ≤250 MiB（解压后 ≤750 MiB） |

## 13.1 准备示例代码

仓库自带两个满足契约的示例（`samples/byoc/`）：

```bash
cd samples/byoc
zip -r hello-http.zip hello-http/          # code_zip：直连代码运行时
zip -r hello-container.zip hello-container/  # container_source：Dockerfile 构建
```

## 13.2 控制台部署（code_zip）

1. 打开 **Create**，选第 4 张卡片 **自带代码**，点 **NEXT**。
2. 构件类型保持 **代码 zip**；把 `hello-http.zip` 拖进上传框。
3. 上传完成后会显示检测摘要：入口候选（`main.py`）、requirements.txt、
   AgentCore SDK 标记。若没有检测到 SDK 标记，会出现黄色提示——确认你的代码
   自行实现了 `POST /invocations`。若 zip 带有 requirements.txt，上传时还会
   针对运行时目标做一次**干跑解析**：绿色表示可解析（并显示包数量），红色则
   给出具体原因（哪个包、为什么）——这样无需等到部署失败才发现依赖问题。
   切换 Python 版本会自动重新检查。
4. 入口文件选 `main.py`，Python 版本保持 3.13；可按需添加环境变量。
5. 在 **允许的模型** 列表里添加你的代码要调用的模型（1–20 个，可从目录选择
   或输入自定义 ID）。执行角色只允许调用列表中的这些模型 ID——你的代码调用
   其他模型会收到 AccessDenied。第一个条目是**主模型**（可用「设为主模型」
   调整顺序）：平台把它以环境变量 `MODEL_ID`、完整列表以 `ALLOWED_MODEL_IDS`
   （逗号分隔）传入运行时（若你自行添加了同名环境变量，则以你的值为准）。
   示例代码正是从 `MODEL_ID` 读取模型 ID，所以无需额外配置。
6. 填名称（如 `byoc-hello`），点 **LAUNCH**。流水线阶段与其他方式相同：
   generate（核验上传与溯源）→ package（解析 requirements → zip → S3）→
   provision（按 Agent 角色）→ deploy（CreateAgentRuntime）→ register。
7. 部署完成后到 **Chat** 发一句话验证；**Observability** 与 **VERSIONS &
   ENDPOINTS** 面板与其他 Runtime 型 Agent 一致。

### requirements.txt 怎么写

- 只列**直接依赖**、且只来自公共软件包索引；固定版本（`==`）可选——平台会把
  文件解析成带 hash 的锁定清单（`requirements.lock`，随产物下发），可复现性由
  锁提供，不要求你手工固定。
- 按 pip 文件格式解析：反斜杠续行、行内注释、空行、环境标记（`; python_version
  < "3.12"`）都被支持。**`--hash=` 选项会被丢弃**：平台针对自己的部署目标重新
  锁定并生成新的 hash，别的平台算出的 wheel hash 只会让构建失败。
- 会被明确报错拒绝（供应链边界——平台只从自己的索引安装）：`-r`/`-c` 引用、
  `-e`/可编辑安装、本地路径、直接 URL 与 VCS 引用（`git+…`）、
  `--index-url`/`--extra-index-url`/`--find-links`，以及超过 500 条的清单。
- 解析目标默认是 **linux/aarch64 + `manylinux_2_28`**（实测运行时为 Amazon
  Linux 2023、glibc 2.34，2026-09-18；官方文档推荐的 `manylinux2014` 是保守
  回退值，可用 `LAUNCHPAD_RUNTIME_PYTHON_PLATFORM` 配置）。平台**从不构建源码
  包**——某个依赖如果没有兼容的 aarch64 wheel，错误会点名它并给出三条出路：
  换一个发布了对应 wheel 的版本；改走 Dockerfile（`container_source`）路径，
  在镜像里自行安装；或把依赖直接打进 zip 并关闭「解析 requirements.txt」
  （`install_requirements=false`）。

## 13.3 Dockerfile 构建（container_source）

同一向导，构件类型选 **Dockerfile 构建**，上传 `hello-container.zip`。
package 阶段会走与方式A 相同的 CodeBuild（ARM64）→ ECR → digest 固定 →
镜像扫描闸门。构建配方由平台持有：平台的 `buildspec.yml` 会被注入 CodeBuild
源码包并**覆盖你 zip 里自带的任何 buildspec**——你只控制 Dockerfile。

## 13.4 现有镜像（container_image）

如果镜像已经在本账户本区域的私有 ECR 里，选 **现有 ECR 镜像**，粘贴
`<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>`。平台会用
`ecr.describe_images` 核验镜像存在且属于本工作区——公共镜像与其他账户会被拒绝。

## 13.5 溯源与治理

- Agent 详情页的 **BYOC 构件** 面板展示：构件类型、模型 ID（执行角色唯一
  允许调用的模型）、sha256、大小、上传者、上传时间（或镜像 URI）。
- v1 限制：仅 HTTP 协议；spec 上不支持工具/技能/知识库（在你自己的代码里
  配置）；配置包实验与金丝雀按 `custom-source-unverified` 降级。
- 平台**不**审查代码内容；zip 归档安全（zip-slip、符号链接、大小上限）在上传
  与打包两处都强制。

## 13.6 清理

删除 Agent 会一并删除 Runtime、按 Agent 的执行角色、暂存的上传对象，以及
`container_source` 构建出的 ECR 镜像标签。
