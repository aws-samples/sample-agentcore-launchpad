# Per-person Budgets (core)

> Monthly USD budgets per person, with hard enforcement. The design and decisions are in parent
> task `10-15-user-level-budgets`; this spec covers what child `10-15-budgets-core` shipped.
> Children 2 (long jobs) and 3 (reconciliation + AgentCore fees) extend it.

## 1. Scope / Trigger

This spec applies when you:
- add a non-GET route; it needs a `core/billing_policy.py` row;
- add a code path that calls a model, an agent, an evaluator or a billed AgentCore tool;
- change the invoke chain (`chat_stream`, `invoke_agent_*`, `iter_harness_stream`,
  `_runtime_payload_events`);
- touch prices (`model_prices`, the refresher, `services/rates.py`), users or the Spend page.

## 2. Contracts

### Who is limited (`services/budgets.py`)

- **Period:** calendar month, UTC (`period_of`, `resets_at`). Spend is the sum of a payer's
  non-superseded `usage_events` rows for the period.
- **Limit precedence:** `override_usd` > assigned level > the default level (at most one
  `is_default`) > unlimited.
- **Exemptions:**
  - admins are unlimited until assigned a level or an override;
  - the config admin, the auth-disabled operator and NULL-creator API keys are always exempt;
  - a deleted user is charged by id and never refused.
- **Nothing is enforced while no level exists.**

### Gate (`core/billing_policy.py`)

- **Coverage:** every non-GET `/api` route is `Billable(source)` or `FREE`; `/v1` invoke and
  invoke-stream are billable. `tests/test_billing_policy.py` drift-tests both ways.
- **Unclassified routes are not refused at runtime.** `ROUTE_POLICY` stays the default-deny
  boundary; only the drift test catches a missing billing row.
- **Where the check runs:**
  - inside `enforce_route_policy`, after the role and workspace checks;
  - `/v1` in the handler, where the payer is `ApiKey.created_by`;
  - experiment and canary `/action` per action (`gate_action`). Billable actions: experiments
    `recommend/traffic/gateway/abtest`, canaries `setup/traffic`. `cleanup`, `rollback` and
    `verdict` are never refused.
- **Refusals:**
  - `402 budget.exceeded` `{used_usd, limit_usd, period, resets_at, level}`;
  - `402 budget.model_unpriced` `{model_id}`, for budgeted payers only.
  - **Model unknown:** an imported Harness with no `model_id` → `model_id: null`. Opaque
    runtimes with no known model pay the flat estimate instead.
  - Agent-less routes with a model (Studio AI fix) call `budgets.check_model` in the handler,
    before the stream opens.

### Meter (`services/usage_meter.py`)

- **How it's passed:** explicitly down the invoke chain, default `None`, so a caller that passes
  nothing sees no change. It writes through its own short `SessionLocal` and **never raises into
  a turn**: a failure is logged.
- **Harness:** every `metadata` event in `iter_harness_stream` is one model call (live probe:
  two for a one-tool turn). The turn total is their **sum**.
- **Claude SDK containers:** `complete.usage`. `cache_creation_input_tokens` maps to
  `cache_write` and `cache_read_input_tokens` to `cache_read`. Priced from the price map, never
  from `total_cost_usd`.
- **Opaque runtimes** (Strands zip, BYOC, Studio, A2A): a turn with no priced usage writes a
  `turn_estimate` row (`budget_settings.opaque_turn_estimate_usd`, default $0.01, flagged
  `estimated`).
- **Evaluate:** `tokenUsage` at the AgentCore evaluator rates. Custom evaluators pay the
  per-evaluation fee plus judge tokens. An error result is never charged.
- **Failed turns:** a failed turn with no usage writes nothing; one that reported usage is
  charged.
- **Sessions:** `usage_sessions` maps each billable session id to `users.id`: Chat, `/v1`,
  console invoke, Architect. Never the username; Chat's own ledger keys on usernames. The first
  payer keeps the session.

### Long-running jobs (`services/job_budget.py`, child 2)

- **Payer:**
  - an eval run → `created_by`;
  - an experiment or canary **action** → the caller, stored in `artifacts.action_callers` and
    passed to the worker;
  - a recommendation → `requested_by` / `requested_by_name` (literal ALTERs);
  - a Skill Lab job → `created_by`; its resume and a queued start re-check that payer;
  - an online-config resume → the config's creator.
  - Exempt payers are metered and never stopped. A deleted payer is charged by id and never
    stopped.
- **Exact rows:** eval replay on Harness and Claude SDK agents, paired canary Harness traffic, the
  gepa_lite provider (one row per job, no session), and on-demand Evaluate.
- **Estimates (`job_estimate`, flagged), charged per unit as work runs:**
  - **an opaque replay turn or a traffic prompt** (per answered 200): the agent's recent average,
    the mean of its last 20 measured rows **that carry a session id**. A recommendation job row
    must never count as a turn. The fallback is `opaque_turn_estimate_usd` /
    `agent_turn_estimate_usd`;
  - **a persona:** its max turns up front, then superseded by the turns actually taken;
  - **judges:** written only after `StartBatchEvaluation` returns, at sessions × evaluators ×
    the per-evaluation cost. Insights and online reports are free;
  - **Skill Lab:** per task (eval), per step (train), per job (taskgen).
- **Skill Lab true-up:** `results.json` usage and judge_usage replace the eval estimate. Codex rows
  with only a total are priced at the output rate.
  - It runs **before** the final job status is written, so nothing reads a finished job with its
    estimate still in place.
  - A cancelled or failed job, or one with an unpriced model, keeps its estimate.
- **Boundaries:** the stop reason is `budget_exceeded`, and finished work is kept.
  - **Eval runs** stop before each scenario, prompt, retry and telemetry wait, and before
    `StartBatchEvaluation`. A running batch is never stopped.
  - **Traffic** stops before each prompt. The skip is recorded on the traffic record (`skipped`,
    `stop_message`), not as a row error, because a retry would only be refused.
  - **Canary `advance`** is a billable action; `complete`, `rollback`, `verdict` and `cleanup`
    stay free.
  - **Skill Lab training** stops between steps → `interrupted`, and stays resumable.
  - **Skill Lab eval and taskgen** are only checked at start: the vendored CLI has no safe
    mid-job boundary.
- **Unpriced models at a job's entry** are refused for budgeted payers: eval agent and actor
  models, the third-party recommendation model, Skill Lab models, and both arms of a Harness
  canary's paired traffic.
- **Estimate defaults** live in `budget_settings` (`GET/PUT /api/budgets/settings`). A create
  response may carry `budget_notice` when the estimate exceeds what is left.

### Reconciliation and AgentCore fees (`services/budget_reconcile.py`, child 3)

- **Thread** (`services/budget_scheduler.py`): **on by default**, with
  `LAUNCHPAD_BUDGETS_RECONCILE_ENABLED=false` to turn it off.
  - A tick runs every 10 minutes; calibration runs hourly.
  - One thread per process. It is listed in `inflight.STARTUP_HOOKS`, and is off in hermetic tests
    (`conftest.py`).
- **Scope: only this box's `usage_sessions`,** so prod and prod2, which share AWS, never charge
  each other's sessions. Gateway and Policy are joined through **this box's trace ids**.
- **Windows:**
  - per session, `[reconciled_until, now − 2 min)`, with no overlap; a span counts in the minute
    it ended;
  - `reconciled_until` advances in the same commit as the rows;
  - a session's first window starts 15 min before its first turn.
  - Rows are dated to the month the activity happened.
- **Tokens:** measured span tokens supersede `turn_estimate` / `job_estimate` rows.
  - A session with exact in-band rows is never re-measured.
  - An unpriced measurement supersedes nothing.
  - Persona-actor estimates are never superseded: the actor emits no spans.
  - Leftover estimates are matched only within 5 min after the watermark, so a later turn with no
    spans is not zeroed.
- **Exact per-session fees** (span names in `research/live-probe.md`), priced from
  `agentcore_rates`:
  - `CreateEvent` → `memory_event`; `RetrieveMemoryRecords` → `memory_retrieval`;
  - Gateway SERVER spans → `gateway_call`, plus Policy;
  - `___WebSearch` → `web_search`;
  - `___Retrieve` / `___AgenticRetrieveStream` → `kb_query`;
  - `StartCodeInterpreterSession` → `code_interpreter` (estimated per session).
- **Runtime compute:**
  - billable hours = `min(last end + tail, start + maxLifetime) − start`;
  - the tail is `lifecycleConfiguration.idleRuntimeSessionTimeout` on V1 (900 s), at most 120 s on
    V2;
  - × GB and vCPU factors × the platform-version rates.
  - **The one allowed in-place update:** a microVM's row grows while it stays alive. A row
    crossing the 1st starts billing where last month's row stopped (`bill_from`).
- **Calibration** (`services/budget_calibration.py`):
  - the factor = the hour's AWS/Bedrock-AgentCore `MemoryUsed-GBHours` / `CPUUsed-vCPUHours`
    (dimensions `Service=AgentCore.Runtime`, `Resource=<runtime ARN>`) ÷ the billable session-hours
    seen in spans for that runtime;
  - spans include every box, so the factor holds on a shared account;
  - outlier bounds and a `too_small` drop (< 0.05 session-hours);
  - a session-weighted merge, capped at 50;
  - below `calibration_min_sessions` (3) the seeds apply.
  - Factors live in `agentcore_rates.calibration.runtimes`; an admin PUT cannot set them.
  - Measured on dev: Harness ≈ 7.56 GB / 0.062 vCPU and Strands ≈ 1.46 GB / 0.019 vCPU per
    session-hour.
- **Evaluations:**
  - batch: `scores[].count` (a records query only when there are no counts);
  - online: result records since a watermark, read in steps of at most 60 min (halved when a step
    hits the row cap), at most 8 queries per tick;
  - charged as built-in count × the tokens-per-evaluation factor × the Tier1/Tier2 rate, and
    custom count × (the fee + judge tokens);
  - this supersedes child 2's judge estimate;
  - a **failed** run is counted only after 24 h, because its batch may still be judging on AWS.
- **Online pause:** a standalone config whose creator is over budget is paused through the
  existing path. `exp_*` / `can_*` configs and exempt creators are left alone, and nothing is
  auto-resumed.
- **Admin:** `GET /api/budgets/reconcile-status`, and `GET /api/budgets/calibration-report`
  (Cost Explorer, report-only, account-wide, cached 1 h, admin-triggered; $0.01 per call).
- **`observability._start_query`:** "No log groups were found" → empty rows. On a brand-new
  account, spend alerts read $0 rather than unknown, while trace and latency alerts still read
  unknown.

### Ledger (`models/budgets.py`)

- `budget_levels`, `usage_events` and `usage_sessions` are **hub-global**. They record
  `origin_workspace_id`, never `workspace_id`: any model with a `workspace_id` column must be
  workspace-scoped, and scoped rows would vanish with a deleted workspace.
- `users` has `budget_level_id` and `budget_override_usd`, added by a literal, idempotent ALTER.
- `usage_events.usd` is NULL for unpriced rows, never $0. `superseded_by` marks an estimate that
  a measured row replaced.

### Prices (`services/rates.py`)

- **Model price overrides** (`platform_settings.model_price_overrides`) are merged over
  `Settings.model_prices` by a settings hook, so they survive `get_settings.cache_clear()` and win
  over yaml, the refresher and the built-ins.
- **The refresher writes `rates.base_model_prices`** (without overrides) to yaml. Otherwise a
  console override would be baked into yaml and outlive its reset.
- **AgentCore fee rates and calibration factors** (`platform_settings.agentcore_rates`) are
  seeded from the 2026-10-15 public rates and are admin-editable. Child 3 consumes most of them.
- **Unpriced detection** uses the same `match_price` (longest substring) as pricing.

### Frontend

- **402 handling:**
  - a 402 is an `ApiError` with `code` and `detail`, never retried;
  - `chatApi.stream` throws it before the stream starts;
  - the filled-in text lives under `budgetRefusal.*`, not `apiErrors.budget.*`. About 22 pages
    call `t(\`apiErrors.${code}\`)` without passing values, so a templated key would print
    `{{used}}` there.
- **My usage data:** `v2/myUsage.ts` is the one shared copy of `/api/me/usage`. It is re-read
  after a 402, and at the end of a Chat, TryChat or Architect turn when the person is limited.
- **Banner:** `v2/BudgetBanner.tsx` shows at 80 % or more, and once the limit is reached. It is
  dismissible per period and state.
- **Admin pages:** Budgets is `/v2/costs?view=budgets`; prices and rates are
  `/v2/models?view=prices|rates`. The user detail page has a budget card.
- **My usage** (`/v2/usage`) is reached from the user menu. There is no nav entry, so no video
  section.

## 3. Tests

- `tests/test_budgets.py` covers:
  - precedence and exemptions; spend with supersede;
  - pricing, including cache rates and unpriced models; overrides surviving `cache_clear` and
    staying out of yaml;
  - a 402 per billable route class; `model_unpriced` for budgeted payers only; `check_model` for
    Studio AI fix;
  - the meters (the Harness sum, the Claude SDK mapping, Evaluate, opaque estimates incl.
    unpriced-token turns);
  - payer resolution; `usage_sessions`;
  - My usage isolation; admin routes;
  - a meter failure never breaks a turn.
- `tests/test_billing_policy.py`: the drift test both ways.
- Vitest: `lib/budgets.test.ts`, `v2/pages/costs/budgets.test.ts`, `v2/myUsage.test.ts`.

## 4. Pitfalls / known gaps

- **A new route that starts billable work needs a `Billable` row.** The drift test only knows a
  row is missing, not whether `FREE` is right, so review that choice.
- **Imported Harness agents** carry no `model_id`, so every budgeted payer is refused on them.
  The fix is to record the model at import (`GetHarness`); it is a follow-up.
- **Architect replay sessions:** only the first attempt's session goes into `usage_sessions`.
- **Live canary traffic** is priced at the champion's model.
- **A client disconnect mid-stream** loses the later `metadata` events: at most one turn
  undercounted.
- **Memory record search is `FREE`:** it is treated as workspace overhead.
- **One very busy session after a long outage** (≥ 10,000 (trace, minute) rows in its window)
  can stall a workspace's session reconciliation (`chunked_rows`). The fix would be to split
  windows by time.
- **Calibration's spans query has no runtime filter,** so a busy account may hit the row cap. The
  hour is then skipped and the defaults apply, which fails safe.
- **The GPT-6 Harness provider spans undercount input tokens** (about 38 vs about 460K on the
  wrapper spans). So the Spend page undercounts those sessions. Budgets are fine, because Harness
  is metered in-band.
