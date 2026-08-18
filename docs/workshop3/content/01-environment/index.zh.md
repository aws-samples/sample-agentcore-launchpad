---
title: "01 环境准备与控制台导览"
weight: 10
---

# 第 01 章 · 实验环境准备与控制台导览

> **目标**：打开控制台，登录，核对环境就绪。
>
> **前置条件**：已按[第 00 章](../00-access-account)加入本 Workshop Studio 活动，
> 活动账号与基础设施部署成功。一个浏览器即可，
> 个人电脑不需要装 AWS CLI，也不需要配置任何凭证。
>
> **预计耗时**：约 5 分钟。
>
> **预置资源**：活动部署时已运行 `make bootstrap`，创建共享基础设施和 AgentCore 单例，
> 并把控制台以生产模式发布到一个公网地址。本章只验证环境，不创建 Agent。

---

> **Self-paced 路径**：本章讲 Workshop Studio 预置环境。若要在本地开发机上连接自有
> AWS 账号，请跳过本章，改看
> [可选第 02 章 · Self-paced 自有 AWS 账号](../02-own-account-local)。两章命令不能混用。

## 1.1 打开控制台并登录

活动页面的 CloudFormation Stack Outputs 里有三项是本章需要的：

| 输出 | 用途 |
|---|---|
| `ConsoleUrl` | 控制台地址，形如 `https://d1234abcd.cloudfront.net` |
| `ConsoleUsername` | 固定为 `admin` |
| `ConsolePassword` | 本 team 专属密码，每个 team 不同 |

![Stack Outputs](/static/images/01-ws-output.png)
*图 1-1：Stack Outputs 顶部三项给出控制台地址与登录凭据。*

把 `ConsoleUrl` 粘到浏览器打开，用 `admin` 和 `ConsolePassword` 登录。

登录页只有用户名和密码两个输入框。登录成功后直接进入总览页。

> **密码属于本 team**。同一活动的其他 team 有各自的地址和密码，互不通用。

**预期结果**：浏览器显示控制台总览页，右上角为 `● 系统运行正常`。这个状态灯读的是后端健康
检查，页面能显示它，说明前端和后端都已就绪。

页面右上角先点 **中文** 切换语言，本指南所有截图都是中文界面。

## 1.2 控制台导览

![控制台总览](/static/images/01-overview.png)  
*图 1-2：总览页。*
> 由于Registry功能还处于Preview状态，所以实验账户中切换到*注册中心*会提示不可用，这是正常情况


## 1.3 认识后端 API（可选）

在 `ConsoleUrl` 后面加 `/api/docs` 打开 FastAPI 文档。实验用到的主要路由如下：

| 路由 | 用途 | 对应章节 |
|---|---|---|
| `POST /api/agents` | 创建并部署 Agent（返回 `202` + `job_id`） | 03 |
| `GET /api/jobs/{id}` | 部署任务的逐阶段 JSONL 事件 | 03 |
| `POST /api/chat/{agent_id}` | 控制台对话入口 | 05 |
| `POST /v1/agents/{id}/invoke` | 公共 API（`X-Api-Key` 鉴权） | 可选 |
| `GET /api/observability/*` | 仪表盘 / 会话 / 追踪 | 06 |
| `POST /api/eval/runs` | 批量评估与洞察 | 07 |
| `POST /api/experiments/{id}/action` | A/B 实验分步动作 | 08 |

> `/api/*` 是控制台内部接口，`/v1/*` 是给外部系统的公共接口。两者共用同一条 invoke
> 链路，差别只在鉴权。

## 1.4 需要命令行时（讲师排障用）

**本实验不需要你登录服务器**。控制台已覆盖全部章节的操作，这一节只在控制台打不开、
需要看服务日志时才用得上。

Stack Outputs 里另有 `SessionManagerUrl`，用浏览器打开即可获得实例终端，不需要本机装
AWS CLI 或 Session Manager Plugin。进入后先切换用户：

```bash
sudo -iu ubuntu
cat ~/WORKSHOP-READY.txt
```

控制台由一个 systemd 服务加一层 nginx 提供：nginx 直接托管前端构建产物，`/api` 与 `/v1`
反向代理到本机 8000 端口的后端。

```bash
systemctl status launchpad-backend nginx
journalctl -u launchpad-backend -n 50 --no-pager
sudo tail -n 100 /var/log/workshop-bootstrap.log
```

---

## 本章验证清单

- [ ] 已用 `ConsoleUrl` 打开控制台，并以 `admin` 登录成功
- [ ] 右上角显示 `● 系统运行正常`，区域与活动区域一致
- [ ] 「服务健康」面板中 Gateway / Memory / Registry / Policy / Observability 五项为「就绪」
- [ ] Runtime 与 Evaluation 为「尚未创建」（全新账号的预期状态，不是故障）
- [ ] 界面已切换到中文
- [ ] 已经知道第 03 章要把 `模型来源` 从默认的 `Bedrock Mantle` 改成 `Bedrock`

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `ConsoleUrl` 打不开或超时 | CloudFront 分发在活动部署后还需几分钟才全球生效 | 等 2–3 分钟重试；仍不通则查基础设施状态 |
| 打开是 502 / 503 | 实例上的服务还在启动，或已崩溃 | 等 1–2 分钟刷新；持续如此按 1.4 节看 `journalctl` |
| 提示密码错误 | 复制 `ConsolePassword` 时带了空格，或用了别的 team 的密码 | 从本 team 的 Outputs 重新复制，注意不要多选到空格 |
| 登录后立刻退回登录页 | 浏览器阻止了会话 Cookie | 换 Chrome/Edge/Safari 常规窗口，不要用无痕模式加严格隐私设置 |
| 注册中心无法使用 | Registry还在Preview状态，实验环境中无法使用| 无需处理 |
| Runtime / Evaluation 显示「尚未创建」 | 这两项统计本实验自建资源，尚未部署 Agent / 创建评估资源 | 属预期，继续第 03 章 |
| 页面能开但所有列表为空、统计为 0 | Workshop Studio 分配的是全新账号，还没有 Agent | 正常，继续第 03 章 |
| Agent 部署完成，但首次调用提示模型无权访问 | 创建时保留了默认的 `Bedrock Mantle`，或所选模型对当前账号不可用 | 先把模型来源改成 `Bedrock`；再按讲师给出的可用模型重新发布 |

---

上一章：[第 00 章 · 获取 Workshop Studio 实验账号](../00-access-account) ｜ 下一章：[第 03 章 · 部署托管 Harness](../03-deploy-harness)
