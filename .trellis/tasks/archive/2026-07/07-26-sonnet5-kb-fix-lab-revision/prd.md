# Fix KB data-source loss, revise lab guide, upgrade default model to sonnet-5

Driven by the remote-environment verification run of `docs/lab/`
(record: `/home/ubuntu/workspace/lab-verify-remote/REPORT.md`, 10 findings).

## Goal

Three deliverables in one pass:

1. Fix the product defect the verification run found — a managed knowledge base
   created in "upload" mode can end up permanently without a data source.
2. Apply the six guide revisions the verification run identified.
3. Move the platform's default Bedrock model to
   `global.anthropic.claude-sonnet-5` (verified ACTIVE in us-west-2 and
   us-east-1) and **re-capture the lab screenshots** so the guide's evidence
   shows the new default instead of `claude-sonnet-4-6`.

## Requirements

### R1 — KB data-source creation must not depend on the browser

`create_kb` currently waits up to 60 s for the KB to leave `CREATING`; if it is
still creating it returns `source_pending` and the **frontend** is responsible
for replaying `POST /data-sources` once the KB turns ACTIVE (via
`sessionStorage` + a DetailView effect). Navigating away in that window loses
the replay: the KB stays ACTIVE with zero data sources, the uploaded file sits
unreferenced in S3, and the UI reports no error. Reproduced on the remote
environment (KB `HINWFLTPHS`, ~90 s to ACTIVE).

- Completion must be owned by the backend and survive the operator leaving the
  page.
- Completion must be idempotent: no duplicate data source if something else
  (an old client, a retry, a manual call) also creates one.
- A KB that is ACTIVE with zero data sources must be **visible as a problem** in
  the UI with a one-click repair, so the case that no background worker can
  cover (backend restart mid-wait, KBs created before this fix) is recoverable.

### R2 — Six guide revisions (from the verification findings)

| # | Chapter | Change |
|---|---|---|
| 1 | 03 | The container step must not name a skill that only exists in one environment; tell the reader to pick any APPROVED skill |
| 2 | 04 | Warn that the data source completes in the background and how to spot / repair "ACTIVE but zero data sources" |
| 3 | 05 | The un-grounded agent's fabricated holdings count is **not** deterministic — present it as "you may see", give a fallback question, and state that chapter 07's scores work either way |
| 4 | 07 (eval) | Document the partial-result state (`N of M sessions failed`) |
| 5 | 08 (A/B) | State that every stage needs a manual click and that `RUNNING` in the stage header is the experiment's status, not the stage executing |
| 6 | 09 (canary) | State that the candidate prompt is prefilled only when entering via `+ 新建金丝雀`, and that the agent list's 版本 column is the platform revision, not the AWS runtime version |

Numbering above follows the shipped guide (chapter 06 is the optional
public-API chapter).

### R3 — Default model → sonnet-5, everywhere a user inherits a default

Platform defaults, not just guide text: agent spec default, Create-Agent form
default, Studio canvas default, evaluation judge defaults and model option
lists, codegen model. Keep `claude-sonnet-4-6` selectable.

### R4 — Re-captured evidence

Every guide screenshot and quoted log/trace excerpt that shows a model id must
be re-captured from a **real run on sonnet-5**. No hand-editing of recorded
output: where the guide quotes a job log or a span waterfall, the new text must
come from a new run.

### R5 — Gate

`make verify` passes; lab image/link integrity holds (no missing/orphan images,
no broken links); chapter/section/caption numbering stays consistent.

## Non-goals

- The other nine verification findings that are informational only (timings,
  `meeting-summarizer` availability, the evaluator-chip reset, the eval score
  differences) are recorded in the verification report; only the six in R2 are
  guide edits this round.
- No re-run of chapters 08–11 evidence (their screenshots carry no model id).
- Studio sample-flow fixtures are updated mechanically but not re-screenshotted
  (the guide has no Studio canvas screenshots).

## Acceptance Criteria

- [ ] A KB created in upload mode gets its data source even if the operator
      leaves the page immediately after clicking create
- [ ] Creating the data source twice does not produce two data sources
- [ ] KB detail shows a warning + repair action when ACTIVE with zero sources
- [ ] Backend tests cover the background completion and the idempotency guard
- [ ] `global.anthropic.claude-sonnet-5` is the default in agent create, Studio,
      evaluation judges and codegen; `claude-sonnet-4-6` still selectable
- [ ] All six R2 revisions present in `docs/lab/`
- [ ] No `claude-sonnet-4-6` left in `docs/lab/` text; re-captured screenshots
      show sonnet-5
- [ ] `make verify` passes and lab integrity checks are clean
