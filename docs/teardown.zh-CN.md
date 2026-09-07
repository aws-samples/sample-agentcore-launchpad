# 资源清理 / Teardown

清理分为两个截然不同的范围：你在使用平台过程中创建的 **演示资源**（Agent、
数据集、experiments），以及 bootstrap 一次性配置的 **共享基础设施**。先退役演示
资源，再清理共享基础设施。

English: [teardown.md](teardown.md)

> **安全准则。** 绝不要删除不是你创建的资源。teardown 只触及名为
> `launchpad*` / `launchpad_*` 的资源；它不会删除你账号里的任何其他东西。确认前
> 请先审阅 dry-run 输出。

## 1. 演示资源（逐 Agent，可从平台可逆地移除）

这些通过控制台或 API 创建，并以同样方式移除——不影响任何共享基础设施。

| 资源 | 如何移除 |
|---|---|
| **Agent** | 控制台 Agent 列表，或 `DELETE /api/agents/{id}`。这会拆除该 Agent 的 runtime/harness 资源，并软删除台账行；自动创建的 A2A 注册记录需另行禁用（见下）。 |
| **评估数据集** | 控制台 Evaluation 页面，或 `DELETE /api/eval/datasets/{id}`。 |
| **评估运行 / experiments** | 运行与 experiments 是覆盖在 AWS 侧产物之上的台账行；用 Experiments 的 **cleanup** 操作（`POST /api/experiments/{id}/action`，body `{"action":"cleanup"}`）移除优化闭环的网关 config bundle 与 A/B 配置。 |
| **注册记录** | 在 Registry 控制台禁用某条记录，或 `POST /api/registry/records/{id}/action`，body `{"action":"disable"}`——这会将其置为 `DEPRECATED`（终态）。 |
| **API 密钥** | 通过 `POST /api/apikeys/{id}/disable` 禁用。 |

用完演示 Agent 后请删除它们，以停止为其 runtime 与存储产物付费。golden-path E2E
（`backend/scripts/e2e_golden_path.py`）在其 cleanup 步骤正是这么做的：删除数据集、
禁用注册记录、删除 Agent。

## 2. 共享基础设施（bootstrap 配置，一条命令）

`make bootstrap`（以及惰性的知识库网关 bootstrap）创建的一切由 `scripts/teardown.py`
移除，按创建的逆序进行，让依赖方先于共享底座。它还会清扫逐 Agent IAM 角色，包括
Agent 删除失败时可能遗留的孤儿角色：

```
launchpad-gw / launchpad-kb-gw 的网关 target            # 网关的依赖方
   → 网关 launchpad-gw、launchpad-kb-gw                  # 按名称匹配；等待 target 从列表消失
      → 凭据提供方                                       # launchpad-office-facts-key（API key）、launchpad-gw-m2m（OAuth2）
         → 逐 Agent IAM 角色                             # launchpad-agent-* 且带 launchpad:agent-id 标签
            → AgentCore memory  (launchpad_memory-*)
               → AgentCore registry (launchpad-registry)   # 先删记录
                  → Skill Lab worker runtime 及其 IAM 角色
                     → CDK stack launchpad-base             # S3 自动清空，ECR 强制删除
```

**务必先 dry-run**，看清究竟会移除什么：

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # 列出目标，不删除任何东西
uv run python ../scripts/teardown.py --yes        # 删除（网关层 → 凭据提供方 → 角色 → memory → registry → CDK stack）
```

参数：`--region <region>`（默认使用配置的区域）、`--dry-run`（仅列出）、`--yes`
（真正删除所必需——不带它时，teardown 表现为 dry-run）。

说明：

- 删除是**尽力而为且依赖方优先**的：网关 target 先于其网关删除（网关删除会在有限
  时间内等待 target 消失，并在 API 不再报冲突后重试），注册记录先于注册表删除，一切
  都先于 CDK 栈移除。某个目标失败只打印警告，循环继续。
- CDK 栈删除执行 `cdk destroy --force`；S3 产物桶自动清空，ECR 仓库作为栈的一部分
  强制删除。
- teardown 目标按精确名称或名称前缀发现——网关 `launchpad-gw` / `launchpad-kb-gw`
  （按网关**名称**匹配，绝不使用 `config/launchpad.yaml` 里可能过期的 id）、凭据提供方
  `launchpad-office-facts-key` / `launchpad-gw-m2m`、`launchpad_memory-*`、
  `launchpad-registry`、Skill Lab worker、栈 `launchpad-base`——这些名称之外的资源
  绝不触及。
- 逐 Agent IAM 角色只在**两个条件同时成立**时才被清扫：名称以 `launchpad-agent-` 开头，
  **且**角色带有 `launchpad:agent-id` 标签。共享的 `launchpad-agent-execution-role`
  属于 CDK 栈，绝不会被匹配。每个逐 Agent 角色在创建时就带上了该标签，因此清扫也会
  列出你尚未删除的 Agent 的角色，而不只是 Agent 删除失败留下的孤儿角色。请先删除
  演示 Agent（第 1 节），并在确认前审阅 dry-run 输出。
- 已部署的 Agent runtime 与 Policy engine 属于演示资源（第 1 节），脚本不会触及；先删除
  你的演示 Agent 可让共享基础设施的清理保持干净。X-Ray Transaction Search 是账号级的
  共享可观测性设施，不在清理范围内。

## 建议的操作顺序

1. 删除演示 **Agent**（控制台或 `DELETE /api/agents/{id}`）。
2. 禁用其 **注册记录**、删除 **评估数据集** / 运行 **experiment cleanup**。
3. 运行 `scripts/teardown.py --dry-run`，审阅后再 `--yes`。
