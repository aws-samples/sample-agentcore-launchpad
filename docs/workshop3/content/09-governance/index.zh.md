---
title: "09 治理"
weight: 90
---

# 第 09 章 · 治理：Gateway 纳管、Cedar 策略与审计

> 本章是进阶部分（Part 2）的收尾：前两章量化并修复了 Agent 的行为，本章把「谁能调用哪个工具」
> 变成可执行、可审计的策略。内容使用 bootstrap 预建的 `launchpad-gw`（`hr-database` 与
> `office-facts` 工具），不依赖其它章节创建的资源。
>
> **目标**：纳管一个 Gateway，创建 Cedar 策略，产生真实的 ALLOW / DENY 判定，
> 再从决策视图与审计日志中读取证据。
>
> **前置条件**：完成[第 01 章](../01-environment)的环境准备（自有账号路径见
> [第 02 章](../02-own-account-local)）。
>
> **预计耗时**：约 25 分钟（含决策证据的摄取等待）。
>
> **本章将创建的 AWS 资源**：1 条 Cedar 策略、`launchpad-gw` 上的
> 纳管标签（留到第 10 章清理）。9.5 还会对 Gateway 发起若干真实调用，产生只读遥测，
> 无需清理。
>
---

## 9.1 Gateway 清单：治理的入口

打开 `09 治理`。默认标签为 `GATEWAY 列表`，数据从 AWS 实时读取。

![Gateway 清单](/static/images/09-gw-list.png)  
*图 9-1：每行一个真实 Gateway，列出状态、鉴权器、目标数、Registry/Harness 可用性、策略引擎与
模式、纳管状态。*

本章操作对象是 bootstrap 预建的 `launchpad-gw`，初始为「未纳管」：

| Gateway | 目标 | Registry | 策略引擎 | 纳管状态 |
|---|---|---|---|---|
| `launchpad-gw` | 2 | 未入目录 / HARNESS 可挂载 | `launchpad_pe` · **未挂载** | 未纳管 |

说明这个AgentCore Gateway是一个没有挂载任何Policy，`未入目录 `说明没有入Registry注册中心的“干净”的Gateway 

> 若完成过第 04 章，清单里还会多一行 `launchpad-kb-gw`（挂载知识库时由平台创建）；
> 只做进阶部分（Part 2）时没有这一行，属正常，不影响本章任何步骤。


## 9.2 打开一个 Gateway详情

点击`launchpad-gw`行进入详情。

![Gateway 详情](/static/images/09-gw-detail.png)  
*图 9-2：`launchpad-gw` 详情。*

详情页行显示：
- `策略引擎与 GATEWAY 模式` 显示未挂载，表示需要创建策略引擎

## 9.3 纳管 `launchpad-gw`

1. 打开 `launchpad-gw` 的详情。
2. 点击 `纳管`，在确认弹窗中确认。

`launchpad-gw` 未纳管时，下一步9.4 的 `新建策略` 处于禁用状态，策略编辑器为只读。

## 9.4 创建 策略引擎
1. 授权模型选择`白名单`
![createengine](/static/images/09-gw-create-engine.png)  
2. 点击`创建并以ENFORCE 挂载`
3. 弹出窗口中点击`确认`
4. 确认之后，稍等1分钟左右，刷新页面，会看到`策略引擎与 GATEWAY 模式`和`IAM 挂载预检`已经有内容。引擎状态为`Active`
![Gateway 详情2](/static/images/09-gw-detail-2.png)  
5. 右上区域的`REGISTRY 发布`-`导入 GATEWAY`按钮已经可以点击，表示这个网关可以纳入AgentCore 注册中心中（实验-04章节有介绍）。本章暂不用点击

## 9.5 Cedar 策略编辑器：先创建 `LOG_ONLY` 策略再提升

切到 `策略编辑器`，或从 Gateway 详情点击 `新建策略`。
![策略编辑器](/static/images/09-policy-editor.png)
*图 9-3：策略编辑器。*

本章创建的策略：

| 字段 | 取值 |
|---|---|
| 策略 | `launchpad_payout_admin_only` |
| 描述 | `实验用：白名单只读工具动作，只用 hr-database___create_payout；先以 LOG_ONLY 观察` |
| 授权模型 | `白名单` |
| 勾选 | `hr-database___create_payout`（发放款项，敏感动作，只允许admin操作） |

点击 `生成 CEDAR 草稿`，平台按勾选项生成：

```cedar
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"hr-database___create_payout",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:<AWS_REGION>:<ACCT>:gateway/launchpad-gw-<后缀>"
);
```

**关键步骤** 接着在最后")"和";"之间添加如下身份限制，表示这个操作只允许身份是`platform-admin`的用户:  
```cedar
when {
  principal.hasTag("cognito:groups") &&
  principal.getTag("cognito:groups") like "*platform-admin*"
}
```
![Gateway 详情3](/static/images/09-policy-draft2.png)  

*图 9-4：草稿填入 `CEDAR` 框后，发布按钮上的阻塞提示消失。*

**点击 `创建 LOG_ONLY 策略` 并确认。**

回到`GATEWAY`详情页，可以看到策略列表里已经新出现了一条策略，状态会由`CREATING` 变成`ACTIVE`才会生效
![Gateway 详情3](/static/images/09-gw-detail-3.png)  

### 将策略提升到ENFORCE
AWS 官方文档建议先用 LOG_ONLY 观察和验证，再切到 ENFORCE，所以 Launchpad 自己加了一道安全 gate。
- 新引擎、新 Gateway 挂载、新策略一律从 `LOG_ONLY` 开始。
- **提升到 `ENFORCE` 需要证据**：界面要求 24 小时内存在 LOG_ONLY 决策证据。
- **或者手动强制提升**
点击`查看`，再次进入策略详情，输入 Gateway 名称`launchpad-gw`，并填写原因`test`，点击`提升`：
![Gateway 编辑4](/static/images/09-policy-editor-prompt1.png)

- 回到Gatewway详情页，策略表格中，策略将会开始Updating，1~2分钟后，策略从`LOG_ONLY` 变成`ACTIVE`， 表示从记录日志模式，正式生效，开始拦截。
![policy table](/static/images/09-policy-updating.png)

### 用策略测试面板模拟不同的用户真实测试
`launchpad-gw` 详情页底部有一个 `策略测试` 面板。它以指定身份对 Gateway 发起一次真实的
`tools/call`，两个内置身份的权限不同：

| 身份 | 角色 | 对 `create_payout` 的预期 |
|---|---|---|
| `demo@hr-analyst` | 普通分析师 | 被 `launchpad_payout_admin_only` 拦下 |
| `admin@platform-admin` | 平台管理员 | 放行 |

在 `GATEWAY` 标签打开 `launchpad-gw` 详情，滚动到策略列表下方：

1. `身份` 选 `demo@hr-analyst`；`精确动作` 选 `hr-database___create_payout / 已验证`——
   下拉列出的正是 9.4 编辑器里那份从 Gateway 实时发现的动作清单。
2. `参数（JSON）`区输入： 
```json
{"amount":123,"employee_id":"111"}
```
3. 点 `运行测试`。面板直接发起以所选身份的用户对Gateway的调用。

结果区出现一条 `DENY`，错误记录，说明非Admin用户调用`hr-database___create_payout`工具被拦截了：
```text
DENY   demo@hr-analyst   已记录
精确动作   hr-database___create_payout
判定策略   launchpad_payout_admin_only-<后缀>
决策 ID    <DECISION_ID>
原始详情   {'code': -32002, 'message': 'Tool Execution Denied: Tool call not allowed
           due to policy enforcement [Policy evaluation denied due to
           launchpad_payout_admin_only-<后缀>]'}
```
![策略测试面板](/static/images/09-policy-test-no-admin.png)

*图 9-6：策略测试面板。*

将 `身份` 换成 `admin@platform-admin`，动作不变，再点一次 `运行测试`。同一工具即被放行：徽章变为
`ALLOW`,说明同样的工具，对admin用户
![策略测试面板](/static/images/09-policy-test.png)

**同一工具、同一 Gateway，身份不同则判定不同。** 这是 Cedar 按动作授权的能力

两点需注意：
- **`ALLOW` 表示授权通过，不表示调用成功。** admin 那条的工具报错发生在授权已放行之后，授权与
  业务成败属于两层。
- **只有 `ALLOW` 与 `DENY` 入账（显示「已记录」）。** 凭证错误、Gateway 不可达等情况显示
  `ERROR` 且不入账。错误不构成判定，不应计入 Cedar 拦截统计。

**（可选）创建基线策略，实验其他操作**
按前面9.5的步骤，创建一个新的基线策略，用于允许所有用户的除了hr-database___create_payout之外的所有操作： 

| 字段 | 取值 |
|---|---|
| 策略 | `launchpad_baseline` |
| 描述 | `允许除了hr-database___create_payout之外的所有操作` |
| 授权模型 | `白名单` |
| 勾选 | 除了`hr-database___create_payout`之外的都都选 |

![策略测试面板](/static/images/09-policy-editor-baseline.png)  

换几个动作各运行一次，例如 `hr-database___list_departments`、
`office-facts___get_office_fact`。看看效果


## 9.6 审计：不可变变更日志

切到 `审计` 标签。

![审计日志](/static/images/09-audit.png)
*图 9-8：策略变更审计。左侧每次变更一行，展开后右侧为该次变更的完整快照。*

定位本章创建策略产生的记录，核对时间、引擎 ID、操作人与状态：

```
<TIME> | policy_create | <POLICY_ENGINE_ID> | <OPERATOR> | succeeded
```

---

## 本章验证清单

- [ ] Gateway 清单列出 `launchpad-gw` 及其策略引擎与模式（做过第 04 章时还有 `launchpad-kb-gw`）
- [ ] `launchpad-gw` 详情的 IAM 挂载预检为 `PASS`
- [ ] `launchpad-gw` 纳管后，AWS 侧只增加 `agentcore-launchpad:managed` 与 `managed-by` 两个标签
- [ ] `launchpad-gw` 已纳管，`新建策略` 按钮可点
- [ ] 生成的 Cedar 语句中没有 `create_payout`
- [ ] 策略测试面板以 `demo@hr-analyst` 运行 `create_payout` 得到 `DENY`，判定策略为
      `launchpad_payout_admin_only`
- [ ] 同一动作换成 `admin@platform-admin` 得到 `ALLOW`

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `创建 LOG_ONLY 策略` 不可点 | 还没有 Cedar 草稿 | 按钮上方会写明原因，先点 `生成 CEDAR 草稿`。保存草稿无需填写 Gateway 名称与覆盖原因 |
| `新建策略` 是灰的，或编辑器提示「未纳管，只读」 | `launchpad-gw` 没有 launchpad 标签 | 按 9.3 末尾先完成纳管 |
| 策略测试面板显示 `ERROR`、未入账 | 评估未发生（凭证问题、Gateway 不可达等） | 错误不入台账；先确认 Gateway 状态为 `READY` 再重试 |
| 切 ENFORCE 被阻止 | 24h 内没有 LOG_ONLY 证据 | 补充证据，或手动输入 Gateway 名称加覆盖原因（谨慎） |

---

上一章：[第 08 章 · 配置包 A/B 实验](../08-experiment-ab) ｜
下一章：[第 10 章 · 收尾与资源清理](../10-wrapup-cleanup)
