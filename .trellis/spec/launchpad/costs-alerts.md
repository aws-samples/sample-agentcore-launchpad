# Costs and Alert Rules

> Estimated spend by agent and by person, plus threshold alert rules with an opt-in webhook.
> Introduced by task `10-14-v2-costs-alerts` (step 4c of `10-13-v3-to-v2-port`), ported from
> `origin/console-v3` without the DLC scheduler, inbox and fleet coupling.

## 1. Scope / Trigger

This spec applies when you:
- touch `services/costs.py`, `services/alerts.py`, `services/alert_scheduler.py`,
  `services/webhook_safety.py` or `routers/alerts.py`;
- add an alert kind, or another outbound call to a URL a user typed;
- change the V2 page `/v2/costs`;
- change how spans are priced (`model_prices`, `estimate_cost`) or how the observability
  dashboard counts traces — alert values read both.

## 2. Contracts

### Spend

- **Source:** two Logs Insights queries over `SPANS_SOURCE`, using the dashboard's
  `_IS_LLM_FIELDS` token logic: by `resource.attributes.service.name` (limit 200) and by
  `attributes.session.id` (sorted by output tokens, then limit 1000, so `by_actor` drops the
  smallest sessions on a busy account).
- **Pricing:** each row is priced at its `latest(model)` rate with `estimate_cost` and
  `get_settings().model_prices`. A model with no price adds tokens, `est_cost_usd: null`, and
  its id goes into `unpriced_models`, which is never a silent `$0`. Tokens with no model id at
  all are listed as `(unknown model)`.
- **Wrapper-only fallback:** `_IS_LLM_FIELDS` drops the Strands wrapper span as a duplicate,
  but a non-Bedrock provider (Mantle `openai.*`) emits *only* that span. Both queries carry
  the wrapper sums too, and a row with no provider tokens is priced on them. This is the
  dashboard's tokens-by-model rule. Without it, those agents read $0. On us-east-1 it moved
  30 days from $178 to $201.
- **Account-wide.** Every runtime in the workspace's account and region counts. A service no
  ledger agent matches is kept with `known: false` and counted in the total. prod and prod2
  share AWS, so each sees the other's agents as `known: false`.
- **By person:** span `session.id` → `ChatSession(workspace_id, session_id).actor_id` (the bare
  username). `/v1` and evaluation traffic have no `ChatSession` → `actor: "—"`,
  `attributed: false`.
- **Ranges:** `costs.RANGES` has `1h/6h/24h/7d/30d`. Observability's `RANGE_HOURS` has no
  `30d` and must stay that way.
- **Month to date:** hours since 00:00 UTC on the 1st, clamped to 1..720.
- **Cache:** observability's `cached` (60 s, single-flight), keys `costs:{ws}:{range}` and
  `costs-mtd:{ws}:{hours}`; `force=true` bypasses it.
- **Cost of the scan (measured 2026-10-14):** 30 days on us-east-1 took 34 s against the 55 s
  `QUERY_DEADLINE_SECONDS`. A timeout is `502 observability.query_failed` on the page and
  `unknown` in an alert.

### Alert rules

`alert_rules` is workspace-scoped: it is in `WORKSPACE_SCOPED_TABLES` and in the
`_migrate_workspace_columns` ALTER list (the drift tests need both). The creator follows
`services/creators` (`created_by` = `users.id`, `created_by_name`). A ledger that ran
console-v3 has the table with a username in `created_by` and no `created_by_name`;
`_migrate_branch_creators` (shared with `spec_snapshots`) converts it once, guarded on the column being absent.

| kind | value | direction | window |
|---|---|---|---|
| `error_rate` | failed / total traces, 0–1 | above | rule's, `1h`–`7d` |
| `latency_p95_ms` | p95 trace latency, ms | above | rule's, `1h`–`7d` |
| `online_quality` | online-eval mean, 0–1 | below | fixed 24 h |
| `spend_usd` | `cost_report(range=window).total_est_cost_usd` | above | rule's, `1h`–`30d` |
| `cost_mtd_usd` | month-to-date spend | above | the month |

- **The inverse direction is refused** (`alert.comparison_never_fires`). The create route
  defaults `comparison`; PATCH cannot change `kind` or `comparison`.
- **An unreadable value is `unknown`, never `ok`.** That covers no traffic, a failed query,
  and a dashboard with `tiles.traces.total == 0`.
- **One notification per firing episode.** `last_fired_at` is when the rule last *started*
  firing (set on a transition, kept while it stays firing). Only
  `evaluate_rules(..., notify_transitions=True)` posts (the scheduler; the default is `False`),
  and it posts when `notification_owed`: the rule is firing, has a webhook, and
  `last_notified_at` is older than `last_fired_at`.
  - A manual re-check that sees the transition first records it without posting, and the next
    background pass still delivers it. Before the review, the re-check silently swallowed it.
  - A webhook added to a rule that is already firing gets one post.
  - `POST /api/alerts/evaluate` takes no notify parameter.
  - The outcome is appended to `last_detail` and sets `last_notified_at`, even on a failed
    send, so there are no retry storms. A send never raises.
  - SQLite returns naive datetimes; compare them through `_utc`.
- **Thresholds:** finite and non-negative. `online_quality` and `error_rate` are 0–1; the UI
  takes the error rate in %.
- **The webhook URL is write-only.** `rule_out` exposes `has_webhook`; no response or log line
  carries the URL.

### Webhook safety (`services/webhook_safety.py`)

- **Every outbound POST to a user-typed URL goes through `post_json`.** It reuses
  `services/public_fetch.resolve_public`.
- **`check_webhook_url`** runs on create, on a PATCH that sends `webhook_url`, and again inside
  every `post_json`. It requires:
  - `https://`, a host, and no userinfo;
  - that the host resolves, and **every** resolved address is public (IPv4-mapped IPv6 is
    judged as its IPv4 address).
- **Refusals** are `400 alert.webhook_{not_https,credentials,invalid,unresolvable,non_public}`.
- **The send connects to the address that was checked**, keeping the original Host header
  (IPv6 literals bracketed) and TLS server name.
  - It does not follow redirects: a 3xx is `(False, "redirect refused …")`.
  - It streams the response and reads only the status, never the body, so an endless body
    cannot stall the scheduler thread.
  - It has a 5 s timeout per phase, not a total deadline, and a 16 KB body cap.
  - `public_fetch` also refuses IPv6 site-local `fec0::/10`, which `is_global` passes.
- **Body:** `{text, workspace_id, rule_id, kind, value, threshold, comparison, detail}`. Slack
  and Feishu incoming hooks render `text`.

### Background check (`services/alert_scheduler.py`)

- **Off by default.** `alerts_scheduler_enabled` (`LAUNCHPAD_ALERTS_SCHEDULER_ENABLED`) and
  `alerts_scheduler_interval_seconds` (300 by default, at least 60).
- **Startup:** `start()` runs in the main.py lifespan next to `start_auto_refresh()`, in the
  same real-server-only block. It starts at most one daemon thread per process. That thread
  waits 60 s, then loops `tick()` → sleep.
- **`tick()`:** for each workspace with an enabled rule, it opens its own DB session, builds
  `workspace_context(row)` and calls `evaluate_rules(notify_transitions=True)`. One workspace
  failing is logged and rolled back, the others still run, and nothing escapes.
- **Startup inventory:** it is listed in `services/inflight.py`'s startup-hook inventory
  (`test_inflight`).
- **Single process.** There is no cross-process claim, and several uvicorn processes would
  each run the thread. Enable it on **one** box per AWS account; the runbook covers this in
  `docs/agent-runbook-prod.md`, "Enabling the alert check".
- **Verify it from the environment or the banner, not the journal.** The backend configures no
  logging, so INFO lines (including "alert scheduler started") never reach the journal.
  Warnings do.

### Routes

All eight routes are **ADMIN** in `route_policy.py`:
- GET `/api/costs`
- GET `/api/costs/month-to-date`
- GET `/api/alerts`
- GET `/api/alerts/scheduler`
- POST `/api/alerts`
- PATCH `/api/alerts/{id}`
- DELETE `/api/alerts/{id}`
- POST `/api/alerts/evaluate`

The branch made GET `/api/alerts` MEMBER. Main does not: an `online_quality` value aggregates
every agent, which leaks across creator isolation.

### Frontend

- **Page:** `v2/pages/Costs.tsx`, with `?view=costs|alerts` and `?range=`. Pure logic lives in
  `v2/pages/costs/alertRules.ts`, tested:
  - kinds, natural comparisons and the windows each kind accepts;
  - per-kind value formatting (error rate typed in % and stored as a fraction);
  - the state → `Signal` mapping (firing = act, ok = ok, unknown = wait, disabled = off);
  - form → payload, including webhook keep, replace and remove on edit.
- **Access:** an admin nav entry (after Models, `Wallet` icon), which reaches the palette
  automatically, and a `costs` admin section in `backend/app/data/video_sections.json`
  (`test_videos` requires every V2 nav path). Members get the forbidden card and the page
  sends no costs or alerts request.
- **Copy:** `v2.costs.*`, `v2.alerts.*`, `v2.nav.costs`, and
  `apiErrors.alert.webhook_*` for translated refusals.

## 3. Tests

- `tests/test_costs_alerts.py` covers:
  - the cost math and the unattributed and `known: false` rows;
  - workspace scoping and the `30d` range;
  - validation per kind, `spend_usd` reading the rule's window, and unknown on a failed read;
  - transition-only notify, and a manual pass that never notifies (also with `?notify=true`);
  - the URL never appearing in a response, and the creators;
  - 403 for a member on every route;
  - the console-v3 ledger migration.
- `tests/test_webhook_safety.py` covers:
  - each refused address class with DNS stubbed, including a public + private mix;
  - the re-check on send and the connection to the checked address;
  - 301, 302, 307 and 308 not followed;
  - errors and oversized bodies.
- `tests/test_alert_scheduler.py` covers:
  - disabled → no thread, and at most one thread;
  - a tick covering every workspace with notify, one failing workspace not stopping the others;
  - an end-to-end post sent once.
- `frontend/src/v2/pages/costs/alertRules.test.ts` and `v2/nav.test.ts` (an admin-only entry).

## 4. Pitfalls

- **Don't add a notify path to the console.** If opening a page or clicking Re-check can post
  a webhook, someone gets paged by a page load.
- **Don't send to a user URL with a plain `httpx`/`urllib` call.** Use `post_json`, so the
  re-check, the pinned address and the redirect refusal all apply.
- **A dashboard query change affects the alerts too.** `error_rate`/`latency_p95_ms` read
  `get_dashboard(window)["tiles"]` and need `ROOT_SPAN_FILTER` (#274). Without it, AgentCore
  traffic counts 0 traces and those rules stay `unknown`.
- **A new kind needs:** `KINDS`, `Kind`, `NATURAL_COMPARISON`, a reader in `read_value`, a
  window rule in `validate`, the ledger comment, the frontend `alertRules.ts` tables, locale
  `kindName`/`kindHint`, and both API docs.
