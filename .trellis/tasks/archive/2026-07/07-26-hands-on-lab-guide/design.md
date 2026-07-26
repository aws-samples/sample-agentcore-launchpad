# Design — hands-on lab guide (docs/lab/)

## 1. Deliverable shape

```
docs/lab/
  README.md              总目录：实验概览、章节表(耗时/资源)、前置检查单、成本与清理提示
  01-environment.md      环境准备、bootstrap、启动本地栈、控制台导览
  02-deploy-runtime.md   方式C/zip Strands Runtime 部署（主线 agent，含流水线中间态）
  03-deploy-harness.md   方式B Managed Harness 部署（免构建）+ 方式A 容器路径说明与实跑
  04-capabilities.md     Registry 资产（MCP 工具 / 技能）+ Managed 知识库挂载
  05-chat-memory.md      Chat Playground 多轮测试 + 会话记忆 + Memory 控制台
  06-public-api.md       /v1 公共 API：API Key、curl 同步与流式调用
  07-observability.md    Dashboard / Sessions / Traces 瀑布图 / Token 与成本
  08-evaluation.md       数据集 + 自定义评估器 + 批量评估 + 洞察分析
  09-experiment-ab.md    优化推荐 → 配置包 → Gateway A/B → 流量 → 判定 → 晋升 → 清理
  10-canary.md           Runtime 金丝雀（目标金丝雀分步流程）
  11-governance.md       Registry 生命周期 + Gateway 纳管 + Cedar LOG_ONLY→ENFORCE + 审计
  12-wrapup-cleanup.md   资源清单、保留/删除、teardown
  images/NN-<slug>.png   截图
```

Chapter numbering in files is 01–12 (PRD's 11 content areas + a separate canary
chapter split out of area 9, since it is a distinct stepwise workflow with its
own table and state machine per `.trellis/spec/launchpad/experiment-stepwise.md`).

## 2. Lab resources and why each method is chosen

Eligibility is not uniform across creation methods — the chapter assignment is
driven by these product constraints (verified in code, not assumed):

| Constraint | Source | Consequence for the lab |
|---|---|---|
| `EVAL_SUPPORTED_METHODS = {zip_runtime, studio, container, harness}` | `backend/app/evaluation/service.py:43` | Any lab agent can be evaluated |
| Config-bundle A/B requires `method == "zip_runtime"` **and** platform-generated code (no `spec.code`/`code_bundle`), `protocol == http` | `optimization/service.py:experiment_capability` | The A/B chapter's subject must be a plain zip_runtime agent created from the form |
| Harness is explicitly experiment-excluded (backing runtime is invoke-locked; exported harness code has no config-bundle consultation) | `experiment_capability` + eval-eligibility spec | Do not build the A/B chapter on a harness agent |
| Canary eligible: `zip_runtime` / `studio` only (container = follow-up) | `canary_capability` | Canary chapter reuses the same zip_runtime agent |
| Knowledge bases are **harness-only** | `frontend/src/pages/CreateAgent.tsx:229` | KB chapter needs a harness agent |
| Harness telemetry log group exists only **after the first invocation** | eval-eligibility spec | Chat chapter must precede eval for the harness agent |
| One batch evaluation per account (queue-managed) | `docs/architecture.md` | Eval chapter warns about queueing; only one run at a time |
| Shared-Gateway mutex on A/B (`assert_shared_gateway_available`) | experiment-stepwise spec | A/B and canary chapters must not run concurrently; guide says so |
| Only one `running` experiment per account | `optimization/routers.py:60` | Cleanup before starting the canary/next experiment |

### Business scenario

One coherent case across all chapters: a **fund product assistant** for the
*MS INVF Emerging Leaders Equity Fund*, whose source document is
`Morgan_Stanley_Oct_21_(EMEA).pdf` (an Aug-2021 product deck, English,
PowerPoint-exported with a full text layer, ~1 MB, 40+ slides). The requester
confirmed the PDF may be committed and shown; it is moved to
`docs/lab/assets/Morgan_Stanley_Oct_21_(EMEA).pdf` so the lab is self-contained.

Why this document helps beyond "some file": it contains hard, checkable facts
(team roster, AUM table — Total `$19,217MM`, EMEA `$93MM`, strategy launch
years, investment process, performance/holdings tables), which become the
**ground truth** for chapter 08's dataset assertions and expected responses, so
evaluation scores mean something instead of scoring an empty run.

Therefore the lab creates **two primary agents plus one container demo agent**:

| Lab agent | Method | Role | Serves chapters |
|---|---|---|---|
| `lab-fund-advisor` | `harness` | 基金文档问答（挂 KB + Registry 工具/技能） | 03, 04, 05, 08 (ground-truth eval) |
| `lab-fund-assistant` | `zip_runtime` | 基金投顾助手（平台生成 Strands 代码，可跑配置包实验） | 02, 05, 06, 07, 09, 10 |
| `lab-fund-packager` | `container` | 方式A CodeBuild 路径演示（best-effort real deploy） | 03 |

Chapter assignment therefore reads as a deliberate teaching point: the guide
carries one **capability matrix** table explaining that the KB lives on the
harness agent while config-bundle A/B and canary live on the zip runtime agent,
because of the constraints above — not because of arbitrary lab design.

方式A container: chapter 03 documents it and performs one **real** container
deploy only if CodeBuild + Docker path succeeds in this environment; if it
fails or exceeds ~6 min, the chapter keeps the real logs/screens obtained and
labels the remainder `（本次未实跑，仅走查）` with the reason (PRD R4).

Naming: every lab-created resource is prefixed `lab-` / `lab_` so chapter 12's
cleanup list is mechanical. Nothing pre-existing (16 agents, 47 registry
assets, 21 eval runs, `launchpad-gw`, `launchpad_memory`) is deleted; those are
reused read-only where a chapter benefits from populated data (e.g. dashboard
charts, registry catalogue).

## 3. Screenshot capture contract

- Tool: `agent-browser`, session `launchpad`, against `http://127.0.0.1:5173`.
- Environment workaround (known quirk, memory `launchpad-dev-environment-quirks`):
  `AGENT_BROWSER_EXECUTABLE_PATH=/home/ubuntu/.cache/ms-playwright/chromium-1232/chrome-linux/chrome`
  and `AGENT_BROWSER_ARGS=--no-sandbox`, otherwise 0.32.0 hangs on snap-chromium.
- Viewport: fixed `1440x900` for full-page shots so the guide's images are
  visually consistent; element-scoped shots (`screenshot --selector`) for
  detail panels (stage pipeline, span drawer, verdict card).
- File naming `docs/lab/images/NN-<slug>.png`, `NN` = chapter number.
- Redaction: no API-key plaintext in any image (capture the masked list view,
  not the one-time reveal dialog); account id tile already renders `ACCT —`.
- In-progress states are captured by polling the page while a background deploy
  runs (deploy 1–3 min for zip gives a usable window).

## 4. Content template per chapter

```markdown
# 第 N 章 · <标题>

> **目标** · **前置条件** · **预计耗时** · **本章将创建的 AWS 资源**

## N.1 <小节标题>
1. **打开** `控制台 → 页面 (?view=...)`
2. **操作** ...
3. **预期结果** ...
![说明](images/NN-slug.png)
*图 N-x：<中文说明>*

## 本章验证清单
- [ ] ...

## 常见问题
| 现象 | 原因 | 处理 |
```

Placeholders use `<AGENT_ID>` / `<RUN_ID>` / `<API_KEY>` style. Real ids from
the run are shown in example output blocks (they are non-secret identifiers).

## 5. Execution order (dependency-driven)

The write order must follow the runtime dependency chain, because later
chapters need telemetry produced by earlier ones:

```
env(01) → deploy zip(02) → deploy harness+container(03) → capabilities(04)
   → chat/memory(05) → /v1(06) → observability(07)   [needs 05/06 traffic]
   → evaluation(08)  [needs traces or dataset]
   → experiment A/B(09) → cleanup exp → canary(10)   [gateway mutex: serial]
   → governance(11) → wrapup(12)
```

Chapters are drafted immediately after their own live run, so screenshots and
prose stay consistent with what actually happened.

## 6. Failure policy while running the flow

- A step that fails for an environment reason (quota, preview API drift, KB
  ingestion latency) is retried once; if it still fails, the chapter documents
  the real error text in 常见问题 and marks the step 未实跑 with the reason.
- Product bugs discovered are recorded in the guide's troubleshooting table and
  reported to the requester at the end — no product code changes in this task
  (PRD R5).
- Every AWS-mutating step is confined to `lab-`-prefixed resources plus the
  shared singletons the console itself touches (Gateway targets during A/B are
  created and then removed by the experiment's own `cleanup` action).

## 7. Verification

- Link/image integrity: a scripted check that every `images/...` reference in
  `docs/lab/*.md` resolves and every file in `docs/lab/images/` is referenced.
- `make verify` at the end (doc-only change, but it is the repo's gate).
- `docs/lab/README.md` linked from `README.md` + `README.zh-CN.md`.
- No `TBD`/placeholder text left in shipped chapters.
