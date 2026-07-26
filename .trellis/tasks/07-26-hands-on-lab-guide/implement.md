# Implementation plan — hands-on lab guide

Pattern for every block: **run it live → capture screenshots → write the chapter
immediately**. Do not batch the writing to the end; prose must match the run.

## Stage 0 — preflight (no AWS mutation)

- [ ] Confirm local stack healthy: `curl -s localhost:8000/api/health`, console
      loads at `:5173` (already true this session; re-check if restarted).
- [ ] Confirm AWS identity + region: `aws sts get-caller-identity`,
      `config/launchpad.yaml` present.
- [ ] Create `docs/lab/`, `docs/lab/images/`.
- [ ] Record baseline inventory (agents / registry / eval runs / experiments /
      KBs) so chapter 12 can diff lab-created vs pre-existing:
      `curl -s localhost:8000/api/agents`, `/api/registry/records`,
      `/api/evaluation/runs`, `/api/experiments`, `/api/knowledge-bases`.
- [ ] Set up the agent-browser env exports once per shell block (executable
      path + `--no-sandbox`) and fix the viewport to 1440x900.

Validation: baseline JSON saved to the task's `research/` dir (not in `docs/`).

## Stage 1 — chapter 01 环境准备

- [ ] Verify the documented prerequisite commands against `README.md` /
      `docs/setup.md` (uv/node/cdk versions, `make bootstrap`, `./start.py`).
- [ ] Screenshots: Overview 全貌、服务健康面板、侧边导航（章节导览）、
      `/api/docs` 页面。
- [ ] Write `01-environment.md`.

## Stage 2 — chapter 02 部署主线 agent（zip_runtime）

- [ ] Create `lab-fund-assistant` via console form (方式 zip_runtime, default model,
      Chinese system prompt, no custom code so A/B stays eligible).
- [ ] While the job runs: poll `/create` (or agent detail) and capture the
      **in-progress** five-stage pipeline; then the finished ACTIVE state.
- [ ] Capture `GET /api/jobs/<id>` JSONL events as a text block for the guide.
- [ ] Record real timings per stage.
- [ ] Write `02-deploy-runtime.md` (pipeline explanation: generate→package→
      provision→deploy→register; resumability note).

Validation: agent status `active`, runtime ARN present, registry record created.

## Stage 3 — chapter 03 方式B Harness + 方式A 容器

- [ ] Create `lab-fund-advisor` (harness) — capture form + fast deploy (~30 s).
- [ ] Attempt one real container deploy (`lab-fund-packager`, 方式A) and
      capture the CodeBuild-backed package stage; on failure follow design §6.
- [ ] Write `03-deploy-harness.md` including the method comparison table
      (build artifact / timing / capability matrix incl. KB & experiment
      eligibility).

## Stage 4 — chapter 04 能力挂载（Registry + KB）

- [ ] Registry: show the existing catalogue, then register one **new** lab asset
      (skill or MCP) end-to-end through submit → approve; capture states.
- [ ] Attach an approved MCP tool + skill to `lab-fund-advisor` (harness update).
- [ ] Move the fund deck into the repo: `docs/lab/assets/Morgan_Stanley_Oct_21_(EMEA).pdf`
      (git-mv from repo root; it is currently untracked at the root).
- [ ] Managed KB: create `lab-fund-kb`, upload/ingest that PDF, wait READY,
      attach to `lab-fund-advisor`, verify retrieval in the KB playground with a
      fact from the deck (e.g. total AUM / EMEA AUM / Head of Emerging Markets).
- [ ] Note the CJK-PDF extraction issue (`docs/issues/2026-07-13-managed-kb-cjk-pdf-extraction.md`)
      only if relevant; this deck is English.
- [ ] Write `04-capabilities.md` (incl. the async-create/ingest latency quirk
      and `UpdateHarness` omit-keeps-value behavior where user-visible).

## Stage 5 — chapter 05 Chat + 记忆

- [ ] Multi-turn chat with `lab-fund-assistant`: turn 1 states a preference, turn 2
      relies on it; capture streaming + SESSION MEMORY rail.
- [ ] Also chat once with `lab-fund-advisor` so its harness telemetry log group
      exists (required before its eval) and to exercise KB retrieval.
- [ ] Memory console: short-term (actor→session→events), long-term (facts /
      preferences namespace) — capture the per-agent actor scoping
      (`<agent>__<human>`), extraction view if a job is visible.
- [ ] Write `05-chat-memory.md`.

## Stage 6 — chapter 06 公共 /v1 API

- [ ] Create an API key in the console; capture the masked list (never the
      plaintext reveal).
- [ ] `curl` sync invoke + `curl -N` stream invoke against `lab-fund-assistant`;
      paste real (trimmed) responses.
- [ ] Write `06-public-api.md` (auth model, session id semantics, error codes).

## Stage 7 — chapter 07 可观测性

- [ ] Wait for spans to land; DASHBOARD tiles + traffic/latency/token/tool
      charts; SESSIONS list → detail transcript; TRACES → waterfall → span
      drawer (tokens, est cost, tool schema, raw attrs).
- [ ] Capture the Chat↔Observability cross-links.
- [ ] Write `07-observability.md` (60 s TTL cache + ⟳ REFRESH, advisory cost
      estimate + price refresh button, per-method telemetry differences).

## Stage 8 — chapter 08 评估

- [ ] `?view=datasets`: create `lab-fund-dataset` with 3–5 devguide scenarios
      (with expected responses/assertions), optionally sync to AWS.
- [ ] `?view=evaluators`: create one custom LLM-as-a-judge evaluator.
- [ ] Main view: run one **batch** evaluation of `lab-fund-assistant` scoped to the
      dataset with 2–3 built-in evaluators + the custom one; capture queued →
      running → completed and the per-evaluator scores.
- [ ] Run one **insights** run (failure analysis subset) over a time window.
- [ ] Write `08-evaluation.md` (scope exclusivity: dataset | sessions | window;
      one batch per account; ground-truth-only trajectory matchers).

## Stage 9 — chapter 09 配置 A/B 实验

- [ ] `?view=experiment`: create experiment on `lab-fund-assistant`; step through
      recommend → accept → bundles → gateway → abtest → traffic (using
      `lab-fund-dataset`) → verdict → promote → cleanup, capturing the stage
      pipeline, each artifact panel, and the verdict card.
- [ ] Note prerequisites that block (shared-Gateway mutex, single running
      experiment) and the manual evidence gates.
- [ ] Write `09-experiment-ab.md`.

## Stage 10 — chapter 10 Runtime 金丝雀

- [ ] After the A/B experiment is cleaned up, run the canary flow on
      `lab-fund-assistant` (candidate mint → traffic split → ramp → verdict →
      promote/rollback → cleanup) as far as the environment allows.
- [ ] Write `10-canary.md`, labelling any non-executed step per design §6.

## Stage 11 — chapter 11 治理

- [ ] Registry lifecycle: DRAFT → submit → approve (and note DEPRECATED is
      terminal); show the lab asset's record states.
- [ ] Gateway: open a live Gateway read-only, **Manage** it (adds only the two
      launchpad tags), import as one MCP record.
- [ ] Cedar: create a policy in `LOG_ONLY`, exercise it via a tool call, review
      the decision log, then promote toward `ENFORCE` (evidence gate; if the
      account cannot produce decision evidence, document the typed
      zero-evidence override and stop before enforcing anything destructive).
- [ ] Unmanage/rollback demonstration + audit trail (`policy_changes`).
- [ ] Write `11-governance.md`.

## Stage 12 — chapter 12 收尾与清理 + 索引

- [ ] Enumerate every lab-created resource (agents, runtimes, registry records,
      KB, dataset, evaluator, eval runs, experiment artifacts, API key, gateway
      targets/policies) with the exact console action or CLI to remove it.
- [ ] Per the requester's decision, KEEP all lab- resources; document cleanup
      steps without executing them (except experiment/canary `cleanup` actions,
      which must run to release the shared Gateway).
- [ ] Write `12-wrapup-cleanup.md` and `docs/lab/README.md` (chapter table with
      real timings from this run).
- [ ] Link from `README.md` and `README.zh-CN.md`.

## Stage 13 — final verification

- [ ] Image/link integrity script: every referenced image exists; no orphans.
- [ ] Grep for leftover `TBD`, `<placeholder>`, English-only paragraphs that
      should be Chinese, and any accidental secret.
- [ ] `make verify`.
- [ ] Report to the requester: what ran for real, what was labelled 未实跑 and
      why, any product issues found, and the AWS resources left behind.

## Rollback points

- Doc-only artifacts: `git checkout -- docs/lab` (or delete the dir) reverts the
  deliverable with no product impact.
- AWS side: each chapter's cleanup action is the rollback; chapter 12's list is
  the master. Experiment/canary `cleanup` actions must run before abandoning a
  partially-executed experiment, otherwise the shared Gateway stays occupied.
