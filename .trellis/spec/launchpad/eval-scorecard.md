# Evaluation Scorecards (run stamps, scorecard / compare / re-run)

> Task tree `10-16-eval-agent-scorecard` (backend child `10-16-eval-scorecard-backend`).
> It backs the V2 评估任务 "按 Agent" views. The module is `backend/app/evaluation/scorecard.py`.
> Composite-score and regression math is deliberately **not** here: the console owns
> evaluator polarity (`v2/format.ts normalizedScore`).

## 1. Scope / Trigger

This spec applies when you:
- touch `submit_run` / `execute_run` (the stamps);
- add a run scope or a `dataset_name` encoding;
- change how a run's agent version is resolved;
- add a field that compare or the scorecard should expose;
- add a caller that starts a run.

It covers a DB change (4 columns), 3 routes, and a payload addition on every run row.

## 2. Signatures

`EvalRun` (all nullable, added in the `core/db.py` `_migrate` eval_runs loop):

| Column | Written by | Value |
|---|---|---|
| `agent_snapshot_seq` INTEGER | `submit_run`; **re-read** when a replay flips to `invoking` (`service._live_snapshot_seq`) | `scorecard.newest_deployed_seq(db, agent_id)`: max `SpecSnapshot.seq` with `aws_version IS NOT NULL` |
| `dataset_fingerprint` VARCHAR(16) | `submit_run` (items non-empty) | `scenarios.items_fingerprint(items)[0]`: sha256 of canonical JSON (sort_keys) over `normalize_scenarios(items)`, first 16 hex |
| `dataset_size` INTEGER | `submit_run` | `len(normalize_scenarios(items))` |
| `scenario_map` JSON | the replay loop, in the same `_update` as `session_ids` | `{session_id: {scenario_id, input}}`, where `input = scenarios.input_preview(scenario)` (≤ 200 chars) |

```python
# backend/app/evaluation/scorecard.py
newest_deployed_seq(db, agent_id) -> int | None
scope_of(run) -> "dataset"|"cloud"|"window"|"sessions"|"logs"|"online"
window_hours(run) -> int | None                    # N of "window:Nh"
dataset_labels(db, workspace_id, viewer, runs) -> {dataset_id: current name}   # visible only
benchmark_of(run, labels=None) -> {kind, key, label, dataset_id, revision, size}
agent_key(run) -> agent_id | "logs:<service_name>"
resolve_snapshots(db, runs) -> {run_id: {seq, source} | None}   # 2 queries per page
scorecard_out(db, workspace_id, runs, *, agent_id, viewer, truncated) -> dict
align(a, b, *, items_of) -> {alignment, reason, maps}
build_cases(a, b, maps, results) -> [{scenario_id, input, a, b}]
config_change(db, a, b, snap_a, snap_b) -> dict | None
```

Routes (`evaluation/routers.py`, prefix `/api/eval`):

| Route | ROUTE_POLICY | BILLING |
|---|---|---|
| `GET /scorecard?agent_id=` | MEMBER | (GET) |
| `GET /runs/compare?a=&b=` (declared **before** `/runs/{run_id}`) | MEMBER | (GET) |
| `POST /runs/{run_id}/rerun` `{name?}` → 201 | `PERM_EVAL_RUN` | `Billable(SOURCE_EVAL)` |

`create_run` and `rerun_run` share `_start_run(req, request, db, ws, viewer, *,
actor_fallback=None)`. All validation, visibility, budget and forecast logic lives there.

## 3. Contracts

**`snapshot` on every run payload** (`_run_out(run, *, snapshot=)`): list, detail, create,
stop, recheck, rerun. `list_runs` resolves a page in one call, and by-id routes use
`_run_one`. Resolution order:
1. No agent id (log source), or the agent has no deployed snapshot → `null`.
2. A passive scope (window / sessions / logs) → `observed` (the stamped seq, else the
   inferred one). Its sessions predate the run.
3. A stamped seq → `stamped`.
4. Otherwise `inferred`: the newest deployed snapshot whose deploy time (`Deployment.ended_at`,
   else the snapshot's `created_at`) is ≤ `run.created_at`. If none qualifies (the run
   predates the baseline backfill), the oldest deployed snapshot.

Inferred values are **never written back**.

**Benchmark:**

| Scope | kind / key | revision |
|---|---|---|
| local | `dataset` / `ds:<id>` | the fingerprint (null on legacy rows) |
| cloud | `cloud` / `cloud:<id>` | `v<dataset_version>`, else `draft:<fingerprint>` |
| any passive scope | `live` / `live` | null (one live benchmark per agent). Console label 历史会话 / Past sessions (badge 真实会话), never "线上/live traffic": since `10-22-eval-online-merge` 持续评估 means online evaluation, and these are one-off runs over existing sessions |

`label` is the local dataset's *current* name only when the viewer can see that dataset
(`dataset_labels` applies the workspace filter and `visibility.clause`). Otherwise it is
the name stored on the run.

**Scorecard:**
- Filters, all in SQL before the cap: `mode == evaluators`, workspace,
  `visibility.clause(viewer, EvalRun.created_by)`, and
  `agent_id NOT IN agent_access.hidden_agent_ids`.
- The newest `SCORECARD_RUN_CAP` (1000) runs are returned oldest-first, with
  `truncated`.
- `agents[]`: `{key, agent_id, name, method, log_source, deleted, current_seq, versions}`.
  `versions[].changes` (diff group names via `snapshots.changed_groups(diff_specs(...))`)
  is computed **only with `agent_id`**, against the newest *deployed* snapshot before it.

**Compare alignment:**
- `none` when either side is `live` or the benchmark keys differ. No results are read.
- `scenario` when both runs have `scenario_map`.
- `order` for a legacy side only when it has a `dataset_id`, no `scenario_failures`, and
  `len(session_ids) == len(normalize_scenarios(items))`. `items` are the *current* local
  items, or the pinned cloud version. A dataset the viewer cannot see counts as
  `dataset_unavailable`.
- Otherwise `none` with `alignment_reason`: `live_traffic`, `different_benchmark`,
  `scenarios_skipped`, `session_count_mismatch`, `dataset_unavailable` or `no_dataset`.

When aligned, `service.run_results` runs once per side. Each side has
`results.{a,b}.{available, reason?, detail?, truncated?}` and degrades on its own.

**Compare `config`:**
- `{agent_id, from_seq, to_seq, between[]}` when the two versions are known and differ;
- `{same: true, agent_id, seq}` when they are the same;
- `null` when either version is unknown, the runs belong to different agents, or the agent
  is hidden from a filtering viewer.

The diff itself is `GET /api/agents/{id}/snapshots/diff?from_seq=&to_seq=`.

**Re-run** rebuilds a `RunCreate` from the source:
- target, evaluators and description are kept;
- `name` = body name, else `<source name minus " · 再评"> · 再评` (no stacking);
- local `dataset_id`; cloud `cloud_dataset_id` + the stored `dataset_version`;
  `window:Nh` → `lookback_hours`;
- `actor_fallback = get_settings().actor_model_id` (it includes console overrides). It is
  used only when the items carry `actor_profile`.

## 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| compare: a run not found / other workspace / hidden | 404 `run.not_found` |
| compare: insights run, or status in `ACTIVE_STATUSES` | 422 `run.compare_unsupported` |
| `GET /runs/compare` reaching `/runs/{run_id}` | must never happen (route order; tested) |
| rerun: sessions / logs / online / insights source, or a window without hours | 422 `run.rerun_unsupported` |
| rerun: stored scope fails `RunCreate` validation | 422 `run.rerun_unsupported` (`detail.errors`) |
| rerun: agent hidden or inactive | 400 `agent.not_active` (same as `POST /runs`) |
| rerun: dataset hidden since | 404 `dataset.not_found` (same as `POST /runs`) |
| rerun: budgeted payer at limit / unpriced | 402 `budget.exceeded` / `budget.model_unpriced` |

## 5. Good / Base / Bad Cases

- **Good:** two stamped replays of one dataset → `alignment: scenario`, and the cases pair
  by scenario id even when a persona was skipped in one of them.
- **Base:** two legacy replays with no skips and an unchanged dataset → `order`. Two
  window runs → `none` / `live_traffic`, overall scores only.
- **Bad:**
  - pairing legacy sessions by position after a skipped scenario: every later case would
    be mislabelled, so it is refused with `scenarios_skipped`;
  - showing a hidden dataset's current name or items: refused (labels fall back to the
    stored name, and alignment gets `dataset_unavailable`).

## 6. Tests Required

`backend/tests/evaluation/test_scorecard.py` asserts:
- migration of an old schema, idempotent;
- the newest deployed seq is stamped, not one still deploying;
- the replay re-reads the seq when it starts;
- fingerprint key-order stability and change on edit;
- scenario map: no entry for a skipped persona, an entry for a budget-stopped scenario,
  preview cap;
- every resolution rule, incl. pre-baseline;
- every benchmark kind;
- every alignment branch, including per-side degrade + `truncated` and a legacy pinned
  cloud run;
- scorecard grouping / insights excluded / cap / deleted agent / visibility matrix (shared
  vs isolated × own / other / admin / NULL creator) / hidden agent's runs dropped;
- hidden dataset: neither its name nor its items leak;
- compare route order via TestClient;
- re-run per scope + 422 / 404 / 400 / 402 + suffix dedupe.

Plus `tests/test_route_policy.py` and `tests/test_billing_policy.py` (drift).

## 7. Wrong vs Correct

#### Wrong: stamping the newest snapshot

```python
agent_snapshot_seq = db.scalar(select(func.max(SpecSnapshot.seq)).where(...agent_id))
# a publish still deploying (aws_version NULL) is not what the run invokes
```

#### Correct

```python
agent_snapshot_seq = scorecard.newest_deployed_seq(db, agent.id)   # aws_version IS NOT NULL
# …and re-read when the replay starts: the run may have waited in the queue
```

#### Wrong: a second run-creation path

```python
@router.post("/runs/{run_id}/rerun")
def rerun_run(...):
    return service.submit_run(agent=..., dataset_items=...)   # skips scope, visibility,
                                                              # budget and forecast checks
```

#### Correct

```python
return _start_run(_rerun_request(source, name), request, db, ws, viewer,
                  actor_fallback=get_settings().actor_model_id)
```

## 8. Console: two-run compare page (`10-16-eval-scorecard-compare`)

**Route:** `/v2/eval/tasks?view=compare&a=<runId>&b=<runId>` → `pages/tasks/RunCompare.tsx`.
- 交换 A / B replaces the URL; it adds no history entry.
- Back goes to the previous history entry, else the task list.

**Pure rules** (`v2/compare.ts`, tested in `compare.test.ts`; only these, never ad-hoc math
in components):

| Export | Contract |
|---|---|
| `sharedEvaluators` / `oneSided` | the evaluators both runs used / only one used. One-sided ones never enter KPIs or the composite |
| `runScore` / `runComposite` | run means through `normalizedScore` (penalty evaluators flipped). Composite = the mean over the **shared** evaluators |
| `classify(a, b, basis)` | `down` \| `up` \| `same` \| `unscored` \| `onlyA` \| `onlyB`. Change ≥ `CASE_DELTA` (0.1) with a `1e-9` slack, so 0.7 − 0.6 still counts. `unscored` = both sides played but no basis evaluator is scored on both. down + up + same + unscored = the shared case count |
| `deltaTone` | within ±`FLAT_DELTA` (0.02) is `flat` (gray "≈ 持平"), always with ▲/▼ text |
| `caseColumns` | at most `CASE_COLUMNS` (4) per-evaluator columns. The rest are reachable through the basis selector + the expanded row |
| `notices(cmp)` | one notice per condition: aligned (ok), order (info), revision differs, partial, live traffic, different benchmark, unaligned reason, different agents, one-sided evaluators |
| `previousComparable(runs, id)` | newest earlier run with the same `agent_key` + `benchmark.key`, completed and scored (TaskDetail's "与上一次对比") |

**Reuse:**
- The config diff is `SnapshotsCard`'s exported `DiffBody` fed by `api.snapshotDiff(agent,
  from_seq, to_seq)`. `currentSpec` is an empty constant, which is safe because both sides
  are seqs.
- "退步用例加入数据集" = `AddToDatasetModal` with `regressedSessions`.
- `VersionTag` / `DeltaChip` / `ScorePair` live in `pages/tasks/scorecardUi.tsx` for the
  agent-view child.

**CSS gotcha:** the snapshot-diff rules live in `v2.css`, not `agents.css`. `agents.css`
loads only with the Agents page and carries a broad `.v2 .v2-field .v2-alert` rule that
would restyle TaskWizard alerts if the tasks page imported it. Compare-only overrides are
scoped under `.v2-compare` (e.g. `.v2-compare .v2-sec-title { flex-wrap: wrap }`).

**Live-verified 2026-10-16 (dev, throwaway stack on a ledger snapshot):**
- legacy pair → `order` (19 cases, both sides read);
- two fresh re-runs → `scenario` (3 cases);
- legacy vs stamped → `order`;
- window vs dataset → `live_traffic`, different datasets → `different_benchmark` (no
  results read);
- insights runs → 422;
- TaskDetail button → exact previous run, absent on the oldest run;
- no content overflow at 400 px. The V2 shell's 994 px top bar overflows on every page,
  which is pre-existing.

## 9. Console: 按 Agent tab + agent page (`10-16-eval-scorecard-agent-view`)

**Routes** (`pages/Tasks.tsx` owns the page header and the `SubTabs`):

| URL | Page |
|---|---|
| `/v2/eval/tasks` (no tab) | `AgentScoreList`: 按 Agent, the default |
| `?tab=runs` (legacy `?tab=all` reads the same — old links and saved Back state) | `TaskList` = 一次性评估, batch runs only (`loadRunTasks`, which drops `isOnlineReport` runs — `dataset_name` `online:<configId>` = an online config's on-demand insights report, shown in that config's 报告 panel). Testids `v2-tasks-table` / `v2-task-new` / `v2-tasks-attention` kept; tab testid `v2-tab-runs` |
| `?tab=online` | `OnlineList` = 持续评估 (the former `/v2/eval/online` page, task `10-22-eval-online-merge`): every online config, owner filter defaults to Agent. Owner `experiment` rows carry `parent` = `{kind: experiment\|canary, id, name, status, agent_name, arm}` (backend `online.py` `parent_ref`/`parent_index`/`parent_of`: 8-char id prefix of `exp_<id8>_…`/`can_<id8>_…`, ≤ 1 query per model per list; `status` = `optimization.service.experiment_status` so an unfinished legacy promotion reads `ready` like the A/B test page; arm `_oec` control, `_oet` treatment, legacy pre-split `exp_*_oe2` treatment with `agent_name` = `artifacts.canary.challenger_agent`, `_oe1` both). Tag A/B 实验 / 金丝雀, detail alert names the record + arm and links to it, leftover note when parent cleaned/gone |
| `?view=agent&key=<agentKey>[&bench=<benchKey>]` | `AgentScorecard`. `bench` is kept in the URL (replace) |
| `?view=new` | `TaskWizard`; `strategy=continuous` or `from=online:<id>` → `OnlineEditor` create. `StrategySwitch` on top of both carries only `agent` (replace + history state kept) |
| `?view=detail&kind=online&id=` / `?view=edit&kind=online&id=` | `OnlineDetail` / `OnlineEditor`; `TaskDetail` is run-only |
| `?view=compare` | unchanged |

**Back** (`pages/tasks/back.ts`): an opener passes `backState(location.search)` in the
history state, and the detail / wizard Back goes to `backTarget(state)`. A deep link
(no state) goes to the default tab — or `backTarget(state, "tab=online")` for the online
config pages. Back always pushes a stateless page, so chains end. URL rules live in
`pages/tasks/urls.ts` (`parseTasksTab`, `newStrategy`, `newTaskParams`, `onlineToTasksUrl`,
`classicOnlineToTasksUrl`) + `urls.test.ts`; `/v2/eval/online…` and the classic
`/evaluation?view=online&oe=` redirect (replace) through them in one hop.
RunCompare's Back is history `-1`.

**Pure rules:** `v2/scorecard.ts` + `scorecard.test.ts`, and
`pages/tasks/scorecardAttention.ts` + its test.
- **Benchmark composite** (`coreEvaluators` / `composite`): the normalized mean over the
  evaluators that every *completed, scored* run of the benchmark includes. It is not
  compare's two-run composite; keep both.
- **`headline`:** on the main benchmark (most completed runs, ties to the most recent),
  the latest completed run vs the latest completed run of an *earlier version*. A log
  source has no versions and compares with its previous run. Otherwise a
  `NoDeltaReason`: `noCompleted | single | differentDatasets | noEarlierVersion |
  noSharedScore`. The list shows `reasonShort.*`, with the full `reason.*` as a tooltip.
- **Regression:** the composite falls ≥ `REGRESSION_COMPOSITE` (0.05), or any evaluator
  falls ≥ `REGRESSION_EVALUATOR` (0.1).
- **Attention** (`scorecardAttention`; the panel caps it at 5, see v2-attention-signals):
  - regression → `act`; when both `benchmark.revision` values are non-null and differ →
    `wait` (数据集已变化，待确认);
  - failed runs → `act`, newest first, only within 7 days of creation and until a later
    run of the same agent on the same benchmark completes;
  - nothing for live benchmarks, same-version pairs or deleted agents;
  - regression rows open `?view=compare&a=<prev>&b=<latest>`.
- **Bands** (`versionBands`):
  - consecutive runs of one `#N` form a band;
  - "≈" only when no run in the band is `stamped`;
  - changes of skipped versions fold into the next band (`changesBetween`).
- **`revisionMarkers`:** a dashed line where consecutive revisions differ (both non-null).
- **Colours:** `seriesColor` maps sorted evaluator ids of the whole agent page onto
  `--v2-series-1…6` (validated categorical order). The 7th evaluator onward uses
  `--v2-series-extra`, and the composite uses `--v2-series-composite` (ink). Switching
  benchmark or hiding a series never repaints the others.
- The trend omits failed, stopped and score-less runs. Active runs are a dashed pending
  marker.
- The matrix shows ≤ `MATRIX_EVALUATORS` (4) evaluators, basis first, with a note naming
  how many more are on the trend only.

**Re-run** (`RerunDialog`):
- the source is the newest completed, re-runnable run (`dataset` / `cloud` / `window`
  scope) of the benchmark;
- disabled for other scopes, deleted agents, and without `eval.run`;
- a name from an i18n template; busy-guarded;
- on success: toast + `rememberBudgetNotice` + reload. The agent page polls every 8 s
  while any of its runs is active.

**Shared kit fix:** `.v2-attention-text` gained `overflow-wrap: anywhere`, and the row's
meta and actions can wrap. At 400 px the 全部任务 failed-task panel overflowed by 249 px
before this change. Desktop rendering of the other attention panels is unchanged.

**Live-verified 2026-10-16 (dev snapshot, throwaway stack):**
- the list (28 agents) at 1280 / 1440 px with no clipped column and the sticky ops
  column clear;
- eval-target / store-service-assistant / hr-assistant-v3 agent pages;
- one real 再评一次 through the dialog: one POST, then the pending marker, then a real
  point after completion;
- attention rows within 400 px on both tabs.

Dev agents all have a single snapshot, so live regression rows were not observable; the
vitest fixtures cover them.

## 10. V3 console: 评估任务 (`/v3/tasks`, `10-26-v3-eval-tasks-tabs`)

V3 keeps V2's URL contract (§9) and reuses `v2/pages/tasks/urls.ts`; only the
presentation is V3's.

**Create (`10-26-v3-eval-strategy-first`):**
- `?view=new` dispatches on `newStrategy(params)` in `v3/pages/Tasks.tsx`: the run wizard
  (`tasks/Wizard.tsx`) or, for `strategy=continuous` / `from=online:<id>`, the V3
  `online/Editor.tsx` (`seedFrom` = copy via `copyDraft`, `agent=` preselect, only an
  `eligibleAgent` is picked). A copy keeps its source's kind whatever `strategy` says.
- `tasks/StrategySwitch.tsx` is the first panel of both create forms (not in edit). It
  always navigates to `/v3/tasks` (`strategyUrl` in `tasks/model.ts`), replace +
  `location.state` kept, carrying only `agent` — the editor is also mounted at
  `/v3/online?view=new`, where a search-param write would not reach the wizard.
- The run wizard has **no** strategy, sampling or session-timeout state and never
  creates an online config; `api.v2CreateOnlineConfig` is called only by the editor.
- The editor imports `online.css` itself (it renders outside `Online.tsx`).
- Tests: `v3/pages/tasks/strategy.test.tsx` (dispatch, switch, `strategyUrl`).

**List tabs (`10-26-v3-eval-runs-online-tabs`):**

| URL | Renders |
|---|---|
| `/v3/tasks`, `?tab=runs`, `?tab=all` | `tasks/List.tsx` `RunList` = 一次性评估 (`loadRunTasks`: no online configs, no online insights reports) |
| `?tab=online` | `online/List.tsx` `OnlineList` = 持续评估 (every config, owner filter defaults to `agent` like V2) |
| `?view=detail&kind=run&id=` (or no `kind`) | `tasks/Detail.tsx` — **run-only** (`TaskDetail({ id })`) |
| `?view=detail&kind=online&id=` / `?view=edit&kind=online&id=` | `online/Detail.tsx` / `online/Editor.tsx` |

- One `PageHead` for the tabs (`Filters` under it, like Memory / Data); sub copy per tab
  = V2's `TAB_DESC`; 新建 opens `strategyUrl(tab === "online" ? "continuous" : "history")`.
- Every V3 online link goes through `onlineHref` / `onlineList` (`online/actions.tsx`) →
  `/v3/tasks`. `/v3/online` is only a `<Navigate replace>` (`v3OnlineToTasks`), kept for
  bookmarks. `v3TwinOf("/v2/eval/online", …)` = V2 `onlineToTasksUrl` with the path
  swapped to `/v3/tasks`.
- `OnlineCostCard` (cost per day) lives on `online/Detail.tsx` for agent-owned configs.
- Tests: `strategy.test.tsx` (tabs + detail dispatch), `online/actions.test.ts`,
  `nav.test.ts`.

**按 Agent (`10-26-v3-eval-by-agent-tab`):**
- The bare `/v3/tasks` is 按 Agent, the first tab (`parseTasksTab` → `agents`).
  `tasks/ByAgent.tsx` is V2's `AgentScoreList` on the V3 kit: same `api.evaluationScorecard()`
  read and pure rules (`agentRows`, `filterAgentRows`, `scorecardAttention`, `rerunSource`),
  8 columns + the 再评一次 row action (V2 `RerunDialog` in `HostedV2` + `AtShell`).
  需要关注 uses the **same** browser-local dismissal scope as V2 (`eval.agents`), with
  per-row restore (`restore` closes over its entries — never loop it in one tick).
- `?view=agent&key=` / `?view=compare&a=&b=` render V2's `AgentScorecard` / `RunCompare`
  inside `HostedV2`; their navigation is relative `setParams`, so it stays on `/v3/tasks`
  (Back → the bare page = 按 Agent). `v3TwinOf` twins every `/v2/eval/tasks` view.
- Naming: the eval-run summary is “按 Agent” / “Agent 评估”, never 记分卡 — that word belongs
  to 判据 → 记分卡 (`/v3/standards`, `dlcApi.scorecard`).
