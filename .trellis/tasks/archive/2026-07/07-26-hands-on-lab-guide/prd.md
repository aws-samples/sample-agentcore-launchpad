# Hands-on lab guide: full agent lifecycle walkthrough

## Goal

Produce a Chinese, chapter-organized, step-by-step hands-on lab guide under
`docs/lab/` that walks a workshop attendee through the complete AgentCore
Launchpad lifecycle on a real AWS account: **deploy → test → observe →
evaluate → optimize → A/B experiment → govern → clean up**.

Every chapter must be reproducible by a reader with no prior knowledge of this
repo, and every key step must carry a screenshot captured from a live local
stack driven against real AWS resources in `us-west-2`.

## Confirmed scope decisions (from the requester, 2026-07-26)

| Decision | Choice |
|---|---|
| Screenshot sourcing | **Run the whole flow for real** — create new AWS resources, capture live UI states (including in-progress ones) |
| Audience | **Workshop attendee, self-service** — includes prerequisites, expected result per step, troubleshooting |
| Output structure | **`docs/lab/` multi-file, one file per chapter** + `docs/lab/images/` |
| Language | **Chinese (zh-CN)** prose; English file names, code, and identifiers |
| Business case | **Fund product assistant** over `Morgan_Stanley_Oct_21_(EMEA).pdf` (MS INVF Emerging Leaders Equity Fund, Aug 2021 deck) as the knowledge-base document; the requester confirmed the PDF may be committed to the repo and shown in screenshots |
| Execution pace | Run all 12 chapters in one pass; report progress at milestones |
| Lab resources afterwards | **Keep** all `lab-` resources for future demos; chapter 12 documents the cleanup procedure without executing deletions |

Language note: `CLAUDE.md` states repo documentation is written in English (per
the launchpad spec index), while product-facing top-level docs are bilingual.
The requester explicitly asked for Chinese, so this guide is authored in Chinese
as a product-facing deliverable; an English mirror is out of scope for this task
and may be added later as `docs/lab/en/`.

## Requirements

### R1 — Content coverage

The guide must cover, in this order, with hands-on steps for each:

1. Prerequisites + one-time bootstrap + starting the local stack + console tour
2. Deploying an agent through the unified five-stage pipeline (方式B managed
   Harness as the primary spine; 方式A Claude-SDK container as a second, real
   deployment so the reader sees the CodeBuild path)
3. Attaching capabilities: Registry assets (MCP tools / skills) and a Managed
   Knowledge Base ingesting the fund deck PDF, so the lab has one coherent
   business scenario (fund product Q&A) and a ground-truth source for chapter 8
4. Functional testing: Chat playground multi-turn + session memory + the Memory
   console (short-term / long-term)
5. Machine-to-machine testing: public `/v1` API key + `curl` invoke and stream
6. Observability: dashboard tiles, sessions transcript, trace waterfall, span
   drawer, token/cost estimates
7. Evaluation: datasets (devguide scenarios) + custom LLM-as-a-judge evaluator +
   a real batch evaluation run + insights run
8. Optimization and configuration A/B experiment: recommend → accept → bundles
   → gateway → abtest → traffic → verdict → promote → cleanup
9. Runtime canary (separate stepwise workflow) — at least one real pass or an
   explicitly-labelled read-only walkthrough if an AWS-side constraint blocks it
10. Governance: Registry record lifecycle, Gateway onboarding (managed tags),
    Cedar policy `LOG_ONLY` → `ENFORCE` with evidence, decision/audit log
11. Wrap-up: what was created, what to keep, how to tear down

### R2 — Step form

- Every operational step is numbered and states: **where** (console page +
  `?view=` sub-page or CLI), **what to do**, **expected result**.
- Any value the reader must copy (agent id, ARN, API key, run id) is called out
  explicitly as a placeholder with a consistent convention (`<AGENT_ID>`).
- CLI blocks are copy-pasteable and use repo conventions (`uv run`, `make`).
- Each chapter opens with: 目标 / 前置条件 / 预计耗时 / 将创建的 AWS 资源, and
  closes with 本章验证清单 (checklist) + 常见问题.

### R3 — Screenshots

- Captured from the live stack at `http://127.0.0.1:5173` via agent-browser,
  real AWS data, no mockups and no hand-drawn diagrams standing in for a UI.
- Stored under `docs/lab/images/` with names `NN-<slug>.png` matching the
  chapter number.
- Referenced with relative markdown links and a Chinese caption line.
- Minimum coverage: every chapter has ≥1 screenshot; the deploy chapter must
  include an **in-progress** pipeline state, and the experiment chapter must
  include at least the A/B stage pipeline and the verdict view.
- No secret values visible (API keys, account id may appear only where the
  console itself masks them; otherwise crop or redact).

### R4 — Truthfulness

- Anything not actually executed is labelled `（本次未实跑，仅走查）` with the
  reason. No invented UI text, ids, numbers, or timings.
- Timings quoted per chapter come from the actual run.
- Known product quirks the reader will hit are documented inline (e.g. one
  batch evaluation per account, shared-Gateway mutex on A/B, republish needed
  after template change, new chat session after republish).

### R5 — Non-goals

- No product code changes. If a real blocker in the product is discovered while
  running the flow, record it in the guide's troubleshooting section and report
  it; do not fix it inside this task.
- No new AWS infrastructure beyond what the console itself creates.
- Not a rewrite of `docs/setup.md` / `docs/architecture.md` — link to them
  instead of duplicating.

## Constraints

- Real AWS account, region `us-west-2`; resources cost money. Lab-created
  resources use a `lab-` name prefix so cleanup is unambiguous.
- Existing 16 agents / 47 registry assets / 21 eval runs must not be deleted or
  mutated destructively; reuse them read-only where helpful.
- The verify gate (`make verify`) must still pass at the end (doc-only change,
  but run it).

## Acceptance Criteria

- [ ] `docs/lab/README.md` exists: lab overview, chapter table with 预计耗时,
      resource/cost summary, prerequisite checklist, cleanup pointer.
- [ ] One markdown file per chapter for chapters 1–11 in `docs/lab/`, Chinese,
      each with 目标/前置/耗时/资源 header and 验证清单/常见问题 footer.
- [ ] All 11 chapters have at least one real screenshot; deploy chapter has an
      in-progress pipeline shot; experiment chapter has stage-pipeline + verdict.
- [ ] Every screenshot file referenced in markdown exists in
      `docs/lab/images/`, and no image file is orphaned.
- [ ] The lab agent(s) were really deployed, invoked, traced, evaluated, and put
      through an A/B experiment; the guide's numbers/ids come from that run.
- [ ] Any step that could not be executed for real is explicitly labelled with
      its reason.
- [ ] `docs/lab/README.md` is linked from `README.md` / `README.zh-CN.md` docs
      index (and `docs/` listing if one exists).
- [ ] `make verify` passes.
- [ ] A cleanup chapter lists every lab-created AWS resource with how to delete
      it.

## Notes

- Load-bearing repo facts to respect while writing: five-stage pipeline names,
  `?view=` sub-page convention, `/v1` vs `/api` split, per-agent memory actor
  scoping (`<agent>__<human>`), 60s TTL cache on observability views, advisory
  cost estimates.
- The requester runs demos from this repo, so wording should be usable both as
  self-study and as a demo script skeleton.
