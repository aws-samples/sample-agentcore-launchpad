# A2A Routing Demo · A2A 路由演示

A five-minute walkthrough showing the Registry's A2A layer end to end:
discovery, card-driven routing, standard A2A invocation, and governance.
五分钟演示 Registry A2A 层的完整闭环：发现、名片驱动路由、标准 A2A 调用、治理开关。

**Cast · 角色**

| Agent | Role | Transport on card |
|---|---|---|
| `front-desk` | Router — no domain knowledge, discovers specialists via Registry semantic search | (HTTP runtime agent) |
| `aurora-faq-a2a` | Product FAQ specialist — a REAL A2A-protocol server (serverProtocol=A2A) | `a2a-jsonrpc` |
| `hr-assistant` | HR policy specialist (managed harness) | `agentcore-http` |
| `aurora-support` | Product support w/ knowledge base (managed harness) | `agentcore-http` |

All four must be **APPROVED** in the Registry — discovery only sees APPROVED
records. 四者的注册记录都需处于 APPROVED（发现只看已发布记录）。

## 1. The business-card wall · 名片墙 (1 min)

Registry → AGENTS · A2A tab. Open `aurora-faq-a2a`: the drawer's AGENT CARD
panel shows transport `a2a-jsonrpc`, a **resolvable endpoint** (copy button —
`/.well-known/agent-card.json` under it really answers), and the skills
configured at create time. Compare with `aurora-support`: transport
`agentcore-http`, skills **derived from its knowledge base** description.

注册中心 → AGENTS · A2A。打开 `aurora-faq-a2a`：AGENT CARD 面板显示
`a2a-jsonrpc` 传输、可解析端点（复制按钮）和创建时配置的技能；对比
`aurora-support`：`agentcore-http` 传输、技能派生自其知识库描述。

## 2. Routing · 智能路由 (2 min)

Registry header → **⇄ A2A DEMO**. Routing agent = `front-desk`.

- Ask **"What is Aurora Deck's refund policy?"** — DISCOVER shows the
  semantic-search hits (aurora cards rank top), SELECT shows the model's
  routing reason, INVOKE shows the **JSON-RPC message/send envelope** going to
  `aurora-faq-a2a` (a standard A2A call), RESPOND opens with
  `[via aurora-faq-a2a]`.
- Ask **"How many paid vacation days do employees get?"** — routing flips to
  `hr-assistant` over the harness transport. Same agent, different wire
  protocol, decided entirely by the card.

先问产品问题（退款政策）——观察四个阶段卡片：发现命中、路由理由、发往
`aurora-faq-a2a` 的标准 JSON-RPC 报文、带出处的回答。再问 HR 问题（年假），
路由自动切到 `hr-assistant`，传输协议由名片决定。

## 3. Governance · 治理开关 (2 min)

1. Registry → `aurora-faq-a2a` record → **REJECT** (never DISABLE —
   DEPRECATED is terminal).
2. Re-ask the refund question: DISCOVER no longer lists it; front-desk
   degrades honestly (routes to another aurora card or says no specialist).
3. **APPROVE** the record again → next question routes back.

在注册中心 REJECT `aurora-faq-a2a`（勿用 DISABLE——终态），重问退款问题：
发现阶段不再出现它，前台诚实降级；重新 APPROVE 后恢复路由。Registry 由
"目录"变成实时的上下架控制面。

## Redeploying the cast · 重建演员

```bash
cd backend
# the front-desk router (ensures IAM: SearchDiscoverableRegistryRecords + InvokeHarness)
.venv/bin/python scripts/deploy_frontdesk_agent.py --api http://localhost:8000
# refresh existing cards after spec changes (restores APPROVED afterwards)
.venv/bin/python scripts/refresh_a2a_cards.py
```

`aurora-faq-a2a` was created through Agent Management (Strands zip →
SERVICE PROTOCOL = A2A) — see `.trellis/spec/launchpad/a2a-agents.md`.
