# 第 12 章 · 收尾：本次实验创建了什么、怎么清理

> **目标**：把整套流程串起来回顾一遍，逐项列出实验创建的 AWS 资源与清理方式，并说明哪些该留、
> 哪些必须清。
>
> **前置条件**：完成第 01–11 章。
>
> **预计耗时**：回顾 5 分钟；如执行完整清理另加 10 分钟。

---

## 12.1 流程回顾

```
第01章 环境          bootstrap → 共享基础设施 + AgentCore 单例
   ↓
第02章 部署          方式C ZIP 通道 → lab-fund-assistant（69 秒）
第03章 部署          方式B Harness → lab-fund-advisor（18 秒）
                     方式A 容器   → lab-fund-packager（125 秒，CodeBuild）
   ↓
第04章 挂能力        PDF → 托管知识库；技能 → Registry(DRAFT→提交→批准) → 挂到 Harness
   ↓
第05章 测试          对话验证：有 KB 的答对，无 KB 的编数字；记忆分区与长期偏好抽取
第06章 集成（可选）  /v1 + API Key：同步与流式，与控制台同一条链路
   ↓
第07章 观测          仪表盘 / 会话还原 / trace 瀑布图：13 秒里 3 次模型调用 + 2 次检索
   ↓
第08章 评估          带真值的数据集 + 自定义评审 → 忠实性 0.95 但接地度只有 0.60
   ↓
第09章 优化          优化器读 trace，独立得出"编造数字"结论 → 配置包 A/B 50/50 → 判定
第10章 金丝雀        目标金丝雀：真实生产流量分档放量 + 每档证据门禁
   ↓
第11章 治理          Gateway 纳管（只加标签）→ Cedar LOG_ONLY 策略 → 决策证据 → 不可变审计
```

整套流程的检查依据包括部署日志、对话结果、trace、评估分数、实验判定和审计记录。

## 12.2 本次实验创建的资源清单

### Agent 与运行时

| 资源 | 标识 | 清理方式 |
|---|---|---|
| Agent `lab-fund-assistant`（zip runtime） | `runtime/lab_fund_assistant_c8fbf6-9ZkLYO3rAB` | `02 Agent 管理` 列表行 → `删除` |
| Agent `lab-fund-advisor`（harness） | `harness/lab_fund_advisor-9IoJvol1OL` | 同上 |
| Agent `lab-fund-packager`（container） | `runtime/lab_fund_packager_88c7cd-fMOWwcBt9f`（缺陷修复复验后为版本 2） | 同上 |
| ECR 镜像 tag | `launchpad-agents:lab-fund-packager-v1` | 删 Agent 不会删镜像；需在 ECR 控制台删 tag |
| S3 部署包 | `s3://launchpad-artifacts-<ACCT>-us-west-2/agents/lab-fund-assistant/deployment_package.zip` | S3 手动删（可留） |
| S3 构建源 | `s3://launchpad-artifacts-<ACCT>-us-west-2/builds/lab-fund-packager/source.zip` | 同上 |

> 控制台的 `删除` 会一并删除 AWS 侧的 Runtime / Harness 资源与本地台账行。

### 知识库

| 资源 | 标识 | 清理方式 |
|---|---|---|
| 托管知识库 `lab-fund-kb` | `2MBGUNVMS4` | 先在 `lab-fund-advisor` 编辑里取消勾选并重新发布，再到 `04 知识库` 详情点 `删除` |
| S3 数据源对象 | `s3://launchpad-artifacts-<ACCT>-us-west-2/kb/2MBGUNVMS4/` | 随 KB 删除清理；S3 对象可手动确认 |
| `launchpad-kb-gw` 上的 KB target | 平台自动管理 | 取消挂载后由平台回收；网关本身是共享资源，**不要删** |

### Registry 记录

| 记录 | id | 状态（本次结束时） | 清理方式 |
|---|---|---|---|
| `lab-fund-disclaimer`（技能） | `F6Qol0d8HKPD` | `APPROVED` | `03 注册中心` 详情 → `删除` |
| `lab-fund-assistant`（A2A，自动登记） | `FZuhhw9jbJaK` | `PENDING_APPROVAL` | 删 Agent 时一并处理，或手动删记录 |
| `lab-fund-advisor`（A2A） | `k2CPfzI7gOn1` | `DRAFT`（因重新发布被重置） | 同上 |
| `lab-fund-packager`（A2A） | `G5ccx6y2DjOR` | `PENDING_APPROVAL`（后续因修复复验重新发布，现为 `DRAFT`） | 同上 |

> **不要用"停用/DEPRECATED"来清理**。它是终态，之后既不能恢复也不能改。要清就直接 `删除`。

### 评估相关

| 资源 | 标识 | 清理方式 |
|---|---|---|
| 本地数据集 `lab-fund-dataset` | `6521039e898d` | `?view=datasets` 选中 → `删除` |
| AWS 数据集快照 | `dataset/lab_fund_dataset-71Ch45EX26` | 云端副本只能删除（不可编辑） |
| 自定义评估器 `fund_fact_grounding` | `fund_fact_grounding-b9ygS38Zq3` | `?view=evaluators` 选中 → `删除` |
| 评估运行记录 | `run-c8a37e` | 本地台账记录，留着做对比即可 |
| 回放产生的 Runtime 会话与遥测 | 5 个 session | CloudWatch 日志按保留期自动过期 |

### 实验与金丝雀（**必须清理**）

| 资源 | 标识 | 清理方式 |
|---|---|---|
| 配置包 control / treatment | `exp_fe7b1d5a_control-…` / `exp_fe7b1d5a_treatment-…` | 实验详情 → `清理` |
| 实验专属 Gateway + target | `launchpad-exp-gw-dskpucnugn` / `expfe7b1dv1` | 同上 |
| A/B 测试 | `exp_fe7b1d5a_bundle-115e6d3187` | 同上 |
| 在线评估配置 | `exp_fe7b1d5a_oe1-wKhd95F84Z` | 同上 |
| 金丝雀网关与候选版本 | 见第 10 章 | 金丝雀记录 → `清理` |

> **注意**：实验/金丝雀是唯一一类不清理会影响后续实验的资源。它们会占用共享实验网关互斥锁，
> 不清理会让下一次实验直接失败；专属网关与在线评估配置也会持续存在。
> 清理动作本身有幂等的删除清单（`deleted` / `skipped` 逐项列出）。

### 其它

| 资源 | 标识 | 清理方式 |
|---|---|---|
| API Key `console-3`（仅跑过第 06 章才有） | 前缀 `lp_live_2381…` | 对话页 API 密钥面板 → 切到 `已停用`（或删除） |
| Cedar 策略 `lab_readonly_tools` | `lab_readonly_tools-be45dja2_p` | `09 治理` → 策略列表 → 删除（它是 `LOG_ONLY`，留着也不拦流量） |
| Gateway 纳管标签 | 本章 11.3 已在验证后移除 | — |
| 记忆事件与长期记录 | `launchpad_memory` 中 `<agent_id>__river` 分区 | Memory 控制台是**只读**的；短期事件按资源的 30 天过期策略自动清除。要立刻清就删对应 Agent 分区（需自行调 AWS API） |

## 12.3 本次实验的处理方式

按需求方决定：**保留所有 `lab-` 资源**供后续演示，本章只提供清理步骤而不执行删除。

唯一的例外是**实验/金丝雀的中间资源**。它们会占用共享网关互斥锁，必须清理，见第 09/10 章
各自的 `CLEANUP` 阶段。

如果你是自己做实验、想清空环境，推荐顺序（依赖在前）：

```text
1. 实验 / 金丝雀 → 各自的「清理」按钮
2. 取消 Harness 上的 KB 挂载 → 重新发布 → 删除知识库
3. 删除三个 lab- Agent（会同时删 AWS 侧 Runtime/Harness）
4. 删除 Registry 记录（技能 + 三条 A2A）
5. 删除数据集（本地 + 云端快照）与自定义评估器
6. 停用/删除本次签发的 API Key（若跑过第 06 章）
7. 删除 lab_readonly_tools 策略
```

## 12.4 共享基础设施：什么时候才动它

`make bootstrap` 建的东西是**整个平台共享**的，不属于本实验的清理范围：

`launchpad-artifacts-*` 桶、`launchpad-agents` ECR、`launchpad-agent-builder` CodeBuild、
`launchpad-users` Cognito、`launchpad-agent-execution-role`、`launchpad-registry`、
`launchpad_memory`、`launchpad-gw`、`launchpad-kb-gw`、`launchpad_pe`。

只有在确定要拆掉整个环境时才执行：

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # 先看会删什么
uv run python ../scripts/teardown.py --yes        # 真删（memory → registry → CDK 栈）
```

删除是尽力而为、依赖方优先；S3 桶会被自动清空，ECR 随栈强制删除。更细的说明见
[docs/teardown.zh-CN.md](../teardown.zh-CN.md)。

## 12.5 成本提示

本次实验的花费集中在三处，量级不大，但演示时仍应说明：

| 来源 | 本次量级 |
|---|---|
| Bedrock 模型调用（对话 + 评估回放 + 评审 + A/B 流量 + 优化器） | 数十次调用；可观测页的估算成本合计约 $0.2 量级 |
| 知识库（向量库 + embedding + 检索） | 1 份 ~1MB PDF 的索引与若干次检索 |
| CloudWatch Logs Insights（可观测页每次查询按扫描量计费） | 平台已用 60 秒 TTL 缓存降低查询次数 |

> 可观测页的成本是**估算值**（token × 本地价格表），界面标注 `≈ / EST`，不要拿它当账单。

## 12.6 继续往下走

- 想改这套实验做自己的 demo：换掉第 04 章的 PDF 与第 08 章的数据集真值即可，其余步骤不变。
- 想深入某个模块：[docs/architecture.zh-CN.md](../architecture.zh-CN.md) 是权威的
  「控制台功能 ↔ AgentCore 服务」映射表。
- 遇到问题：[docs/troubleshooting.zh-CN.md](../troubleshooting.zh-CN.md)。

---

上一章：[第 11 章 · 治理](11-governance.md) ｜ 返回：[实验总目录](README.md)
