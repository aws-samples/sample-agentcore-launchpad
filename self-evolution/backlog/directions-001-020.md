# Directions 001–020

Priorities 1–20. One `## SE-NNN — heading` section per direction; the `- Status:` line is the only mutable state a run may edit outside `### Notes`.


## SE-001 — Catch-all route: unknown URLs render a not-found view inside the shell

- Status: done
- Priority: 1
- Path: ux
- Origin report: reports/research_2026-09-04.md#run-2026-09-04t11-45-25z
- Score: 14 (Importance 3 · Architecture fit 5 · Evidence 5 · Difficulty 1 · Risk 1)
- Branch: evo/se-001-catch-all-route-unknown-urls-render-a-no
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/72

### Requirement

Any URL the console does not route (for example `/nonexistent-route`, a stale bookmark such as an old sub-route, or a typo) renders a **not-found view inside the normal shell** — sidebar, topbar and footer stay visible — with: a kicker/heading in the house style (`ViewHead`), one sentence saying the page does not exist, the requested path shown in mono, and a primary `Btn` linking back to the Overview (`/`). Both locales. Today the router renders nothing and the user sees only the background grid.

### Evidence

- `frontend/src/App.tsx:26-39` — route table nested under `<Route element={<Shell />}>`; there is no `path="*"` element, so an unmatched path renders an empty `<Outlet />`.
- `.claude/self-evolution/reports/shots/en_desk_nonexistent_route.png` — the whole viewport is the background grid: no shell, no message.
- Playwright survey (`reports/research_2026-09-04.md`, S1) — `/nonexistent-route` is the only route in 31 that renders no `<h1>`.
- `frontend/src/layout/Shell.tsx:33` — `<Outlet />` inside `<main>`; the shell already renders for every matched route, so a `*` route placed **inside** the Shell group keeps the chrome.
- `frontend/src/layout/Topbar.tsx:37-39` — the breadcrumb uses `crumbKey`; see how `Shell.tsx` derives it (`ALL_NAV_ENTRIES` in `layout/nav.ts`) and give the not-found page a sensible crumb (e.g. `nav.notFound`) instead of an empty/undefined one.
- Shared components to use: `frontend/src/components/{ViewHead,Panel,Btn}.tsx`; i18n keys live in `frontend/src/locales/{en,zh-CN}/common.json` (parity enforced by `scripts/i18n_check.py`).

### Acceptance checks

- [ ] `frontend/src/App.tsx` has a `<Route path="*" element={<NotFound />} />` **inside** the `<Shell />` route group (so the sidebar/topbar/footer render).
- [ ] New page `frontend/src/pages/NotFound.tsx` uses `ViewHead` + `Panel` + `Btn`/`Link` to `/`, shows the requested pathname (`useLocation().pathname`) in a mono span, and uses only i18n keys (new keys in both `en` and `zh-CN`, `python3 scripts/i18n_check.py` passes).
- [ ] Screenshot evidence: with the dev stack running (`curl -s localhost:5173/ | grep -o '<title>[^<]*'` must print `AgentCore Launchpad`), a Playwright/agent-browser capture of `http://localhost:5173/nonexistent-route` saved as `.claude/self-evolution/runs/SE-001/notfound_en.png` and `notfound_zh.png` showing the shell plus the not-found panel. (Load `localStorage.i18nextLng` = `zh-CN` for the second one.)
- [ ] The topbar breadcrumb on that page is not blank/`undefined` (visible in the screenshot).
- [ ] `make verify` passes. No live AWS check required.

### Notes
- 2026-09-04T12:01:41Z — added as not-started
- 2026-09-04T12:02:21Z — not-started → in-progress
- 2026-09-04T12:28:50Z — in-progress → in-review: PR #72 open; host rerun: make verify PASS, Playwright crumb/path/chrome/back-link/zh-CN + registry crumb regression check PASS; 1 correction (Link-wrapped <button> → Link.btn.primary)
- 2026-09-04T14:29:10Z — in-review → done: PR #72 squash-merged 2026-09-04 as 84ebdc6 (--auto-merge run)

## SE-002 — Unreachable backend: health chip reflects reality and list pages show load errors, not empty states

- Status: done
- Priority: 2
- Path: ux
- Origin report: reports/research_2026-09-04.md#run-2026-09-04t11-45-25z
- Score: 12 (Importance 4 · Architecture fit 4 · Evidence 5 · Difficulty 3 · Risk 2)
- Branch: evo/se-002-unreachable-backend-health-chip-reflects
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/73

### Requirement

When the backend is unreachable or a list endpoint fails, the console must say so instead of pretending the account is empty:

1. **Health chip.** The topbar chip currently renders a constant green "ALL SYSTEMS GO". It must reflect `/api/health`: green + `topbar.allSystemsGo` when the last probe succeeded; red/`crit` LED + a "BACKEND UNREACHABLE" (i18n) label when the probe failed or never answered; and it must re-probe (e.g. every 30 s, and immediately on `window` `online`/focus) so it recovers without a reload. Keep the chip's markup/size stable so the topbar layout does not shift.
2. **List pages.** Overview (deployed-agents tile + launch feed), Registry (records table), Knowledge Bases (KB table), Chat (agent picker header) and the Evaluation module's Runs and Experiments tables must distinguish *loaded and empty* from *failed to load*. On failure render a shared error state with the localized message and a **Retry** button (same look as the existing Observability/Governance "… failed · RETRY" blocks), never the "create your first …" empty copy. The successful/empty behaviour is unchanged.
3. Do this through **one shared component** (e.g. `components/LoadError.tsx`, or an `error`/`onRetry` prop on `DataTable`) so the ~8 sites do not re-invent it; pages that already show an error (Memory, Observability, Users, Workspaces, Governance, Skill Lab, Datasets, Evaluators) may adopt it but must not regress.

### Evidence

- `frontend/src/layout/Topbar.tsx:41-44` — `<div className="syschip"><span className="led"></span>{t("topbar.allSystemsGo")}</div>`: constant, not bound to `health`.
- `frontend/src/layout/useHealth.ts` — single fetch on mount; `.catch(() => { /* backend not running — chips fall back to placeholders */ })` returns `null` and nothing distinguishes "loading" from "down". `Shell.tsx` passes `health` to `Topbar`.
- `frontend/src/pages/Overview.tsx:39-49` — `api.listAgents().catch(() => setAgents(prev => prev ?? []))  // backend offline — empty state`.
- `frontend/src/pages/KnowledgeBases.tsx:92-104` — `load()` sets `items` to `[]` on `!res.ok` and in `catch` ("backend offline — show empty state").
- `frontend/src/pages/Chat.tsx:~120-135` — agent list fetch `.catch(() => {})`; header then shows `no active agents — deploy one first`.
- `frontend/src/pages/Evaluation.tsx:164-166` (`/* backend offline */`) and `:182-208` (agents/datasets/cloud fetches `.catch(() => {})`).
- `frontend/src/pages/Registry.tsx` — `loadRecords`/list fetch catch → empty; `EvaluationExperiment.tsx` — experiments list fetch `.catch(() => {})`.
- Dead-backend screenshots `.claude/self-evolution/reports/shots/dead_{root,registry,knowledge_bases,chat,evaluation,evaluation_view_experiment}.png` (second vite on :5199 with `LAUNCHPAD_API=http://localhost:8999`): empty copy + green chip. Contrast `dead_observability.png`, `dead_governance.png`, `dead_users.png`: proper error + RETRY.
- Reproduction recipe (from `docs/agent-runbook-dev.md` §2): `cd frontend && LAUNCHPAD_API=http://localhost:8999 npx vite --port 5199 --strictPort` gives a console whose every `/api` call fails (vite proxy → 500 "Internal server error"); kill it with `pkill -f "port 5199"` afterwards.
- Error copy convention: `frontend/src/lib/api.ts:330-390` (`ApiError`, `localizedMessage`, `apiErrors.*` keys); existing error blocks in `pages/Observability.tsx` and `pages/Governance.tsx` for the visual pattern.

### Acceptance checks

- [ ] New shared component (`frontend/src/components/LoadError.tsx` or equivalent, exported from `components/index.ts`) with message + Retry, used by Overview, Registry, Knowledge Bases, Chat, Evaluation runs, Evaluation experiments.
- [ ] `useHealth` returns `{ health, status: "loading" | "ok" | "down", refresh }` (or equivalent) and re-probes periodically; `Topbar` renders `topbar.allSystemsGo` with the green LED only when `status === "ok"`, otherwise a `crit`-toned LED + new key `topbar.backendDown` (en + zh-CN).
- [ ] Dead-backend screenshots saved under `.claude/self-evolution/runs/SE-002/`: `dead_overview.png`, `dead_registry.png`, `dead_knowledge_bases.png`, `dead_chat.png`, `dead_evaluation.png`, `dead_experiments.png` — each shows the red chip and the error+Retry block, and **none** shows "none yet / NO RECORDS / No knowledge bases yet / no active agents / NO EXPERIMENTS" copy.
- [ ] Live-backend screenshot `runs/SE-002/live_overview.png` shows the green chip and the normal Overview (no regression when the API is healthy).
- [ ] Clicking Retry re-issues the fetch (verify by watching the network tab or a second screenshot after bringing the backend back).
- [ ] `python3 scripts/i18n_check.py` passes; `make verify` passes. No live AWS check required.

### Notes
- 2026-09-04T12:01:41Z — added as not-started
- 2026-09-04T12:28:50Z — not-started → in-progress
- 2026-09-04T12:57:24Z — in-progress → in-review: PR open; host rerun: make verify PASS; Playwright dead-API (6 routes: chip down + LoadError/Retry, no empty copy; Retry = 1 request) + live (chip ok, 0 error blocks) PASS; child session needed one resume because it ended its turn waiting on a background make verify (not an acceptance failure)
- 2026-09-04T14:29:10Z — in-review → done: PR #73 squash-merged 2026-09-04 as b1fd561 (--auto-merge run)

## SE-003 — Stale "Phase" copy and unlabeled form controls

- Status: done
- Priority: 3
- Path: ux
- Origin report: reports/research_2026-09-04.md#run-2026-09-04t11-45-25z
- Score: 12 (Importance 2 · Architecture fit 5 · Evidence 5 · Difficulty 1 · Risk 1)
- Branch: evo/se-003-stale-phase-copy-and-unlabeled-form-cont
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/74

### Requirement

Two small, user-visible polish items, both locales:

1. **Stale "phase" copy.** `chatPage.tracePlaceholder` ("END-TO-END TRACE RAIL ARRIVES WITH OBSERVABILITY (PHASE 9)") is shown in the Chat trace rail before a session exists, although Observability and the trace rail (`loadTrace`) shipped. Replace it with copy that describes the current behaviour (e.g. "Send a message — the trace rail loads spans for this session"). Also fix `onSuccessV` ("auto-register → Registry (phase 7)") to drop the phase reference. Do **not** touch the deliberate `nav.phase02` / `footer.phase` / `footer.payments` strings (Payments deferral is a recorded product decision) or `agent.method_not_available`.
2. **Accessible names for form controls.** Every visible `<select>`, `<input>`, `<textarea>` in the console must have an accessible name (`aria-label`, `<label htmlFor>`, `aria-labelledby`, or a placeholder as last resort). Known offenders (Playwright probe, 2026-09-04): Chat agent picker `select[data-testid=agent-select]`; Experiments `exp-agent-select`, `baseline-dataset`; Evaluators model `<select>`, name `<input>`, prompt `<textarea>`; Datasets name/description `<input>`s; Online `online-sampling`, `online-timeout`. Reuse the adjacent visible label text as the `aria-label` (via i18n keys — no hardcoded English).

### Evidence

- `frontend/src/locales/en/common.json:1676` — `"tracePlaceholder": "END-TO-END TRACE RAIL ARRIVES WITH OBSERVABILITY (PHASE 9)"`; `:344` — `"onSuccessV": "auto-register → Registry (phase 7)"`; zh-CN twins at the same keys.
- `frontend/src/pages/Chat.tsx:547` — `{sessionId ? t("chatPage.traceEmpty") : t("chatPage.tracePlaceholder")}`; `Chat.tsx:293` — `loadTrace()` exists and hits `/api/traces/{sessionId}`.
- `frontend/src/layout/nav.ts` — Observability is nav entry 07 (shipped).
- Survey S10 in `reports/research_2026-09-04.md` (`inputNoLabel` rows) — the exact `outerHTML` prefixes of the unlabeled controls; screenshot `reports/shots/en_desk_chat.png` shows the agent picker with no label.
- Probe to re-run for the check (from repo root, dev stack running):
  ```js
  // in a Playwright page.evaluate on each route
  [...document.querySelectorAll('input,select,textarea')].filter(i=>i.type!=='hidden'&&i.getBoundingClientRect().width>0).filter(i=>!(i.id&&document.querySelector(`label[for="${i.id}"]`))&&!i.closest('label')&&!i.getAttribute('aria-label')&&!i.getAttribute('aria-labelledby')&&!i.getAttribute('placeholder')).map(i=>i.outerHTML.slice(0,100))
  ```
  Routes: `/chat`, `/evaluation?view=experiment`, `/evaluation?view=evaluators`, `/evaluation?view=datasets`, `/evaluation?view=online` — plus a sweep of all 30 routes listed in the report to catch others.

### Acceptance checks

- [ ] `grep -n "PHASE 9\|phase 7" frontend/src/locales/*/common.json` prints nothing; `nav.phase02`, `footer.phase`, `footer.payments`, `agent.method_not_available` are unchanged (`git diff` shows no hunk on those keys).
- [ ] The probe above returns `[]` on every one of the 30 console routes (save the run output as `.claude/self-evolution/runs/SE-003/a11y_probe.txt`).
- [ ] No hardcoded English: every new `aria-label` goes through `t(...)`; `python3 scripts/i18n_check.py` passes.
- [ ] `make verify` passes. No live AWS check required.

### Notes
- 2026-09-04T12:01:41Z — added as not-started
- 2026-09-04T12:57:24Z — not-started → in-progress
- 2026-09-04T13:24:10Z — in-progress → in-review: PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/74 open; host rerun: make verify PASS, own Playwright probe 30 routes × en/zh-CN = 0 unlabeled controls, trace copy + agent-select aria-label verified, protected keys untouched
- 2026-09-04T14:29:10Z — in-review → done: PR #74 squash-merged 2026-09-04 as 7f2ebe5 (--auto-merge run)

## SE-004 — Small viewports: no horizontal page scroll below 720 px

- Status: done
- Priority: 4
- Path: ux
- Origin report: reports/research_2026-09-04.md#run-2026-09-04t11-45-25z
- Score: 10 (Importance 3 · Architecture fit 4 · Evidence 5 · Difficulty 3 · Risk 2)
- Branch: evo/se-004-small-viewports-no-horizontal-page-scrol
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/75

### Requirement

Below the existing 720 px breakpoint the console must never scroll horizontally as a **page**: wide content (tables, code blocks, toolbars, long `<select>`s) scrolls inside its own container or wraps. Verify at a 390×844 viewport on every route; the shell already collapses the sidebar into a nav strip at that width, so this completes a deliberate responsive design rather than starting one. Desktop layout (≥ 1180 px) must not change.

### Evidence

- `frontend/src/theme/app.css:783-834` — the `@media(max-width:720px)` block; lines 833-834 `.panel:has(> table){overflow-x:auto}` `.panel > table{min-width:680px}` only match tables that are **direct** children of `.panel`.
- Narrow probe (`reports/research_2026-09-04.md` S7, `/tmp/ux_narrow.mjs`) at 390 px, `document.documentElement.scrollWidth`:
  - `/create` 410 — existing-agents `<table>` (680 px min-width) rendered inside `.pbody`/`DataTable`, not scrolling.
  - `/registry` 597 — the toolbar row (`IMPORT GATEWAY · A2A DEMO · + REGISTER` buttons) does not wrap; `.search{max-width:420px}` (app.css:361) holds a 231 px placeholder span.
  - `/knowledge-bases` 480 — KB table (455 px) not in a scroll container.
  - `/memory?view=long-term` 665 — actor `<select class="fsel">` is 639 px wide (as wide as its longest option; `white-space:pre`).
  - `/chat` 636 — `.panel.brk` children (`.kv` rows 600 px, `.code` curl block) lack `min-width:0` after `.chat-grid` collapses to one column (app.css:782).
  - `/users` 787 — accounts `<table>` 776 px.
  - By contrast `/evaluation?view=*`, `/skill-lab?view=train`, `/governance?view=policy` stay at 390: their tables sit directly under `.panel` (or `.gov-nav{overflow-x:auto}`), proving the intended pattern works when the selector matches.
- Screenshots: `reports/shots/en_narrow_{create,registry,knowledge_bases,memory_view_long_term,chat,users}.png` (full-page captures are wider than 390 px because the page itself overflows).
- `frontend/src/components/DataTable.tsx` — the shared table component; its empty branch wraps `<table>` in a bare `<div>`, which breaks the `.panel > table` selector. Giving `DataTable` (and raw `<table>`s in the six pages) a `.table-scroll` wrapper with `overflow-x:auto;min-width:0` is the natural single fix; alternatively widen the CSS selector, but note `:has()` on arbitrary depth is costly.
- Survey S1 recorded 0 px overflow at 1440×900 on every route — the desktop baseline to preserve.

### Acceptance checks

- [ ] Re-run the narrow probe (script text in the research report; Playwright at 390×844, dev stack running) on all 30 routes listed in the report: `scrollWidth === 390` for every route. Save the output as `.claude/self-evolution/runs/SE-004/narrow_probe.txt`.
- [ ] Re-run at 1440×900: `scrollWidth === 1440` on every route (no desktop regression), same file.
- [ ] Screenshots `runs/SE-004/narrow_{create,registry,knowledge_bases,memory_long_term,chat,users}.png` (viewport-only, not full-page) show tables/toolbars scrolling or wrapping inside their panels.
- [ ] Changes are CSS in `frontend/src/theme/app.css` plus a shared wrapper (e.g. in `components/DataTable.tsx`); no page-specific inline widths; no changes under `apps/studio/`.
- [ ] `make verify` passes. No live AWS check required.

### Notes
- 2026-09-04T12:01:41Z — added as not-started
- 2026-09-04T13:26:19Z — not-started → in-progress
- 2026-09-04T14:22:39Z — in-progress → in-review: PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/75 open; host rerun: make verify PASS; own Playwright probe (real fonts via temp server.fs.allow) 30 routes → 0 overflow at 390 and 1440; screenshots host_narrow_*.png
- 2026-09-04T14:37:38Z — in-review → done: PR #75 squash-merged 2026-09-04 after host-side merge of main (docs sections kept, DataTable error branch wrapped), make verify PASS on merged branch (--auto-merge run)


## SE-005 — AWS ClientErrors on console routes map to 4xx envelopes, never a bare 500 or raw boto text

- Status: done
- Priority: 5
- Path: ux
- Origin report: reports/research_2026-09-04.md#run-2026-09-04t14-36-36z
- Score: 14 (Importance 4 · Architecture fit 5 · Evidence 5 · Difficulty 2 · Risk 2)
- Branch: evo/se-005-aws-clienterrors-on-console-routes-map-t
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/76

### Requirement

A console request that hits an AWS `ClientError` the platform did not anticipate must come back as a **structured 4xx envelope** the console can translate, not as `Internal Server Error` or a message that starts with `An error occurred (…) when calling the … operation`. Concretely, in `app/core/errors.py`, extend the global `ClientError` handler (today `assume_role_error_handler`, which re-raises everything but assume-role failures) so that:

- `ResourceNotFoundException` → 404, code `aws.not_found`
- `ValidationException` → 400, code `aws.validation`
- `AccessDeniedException` / `UnauthorizedException` → 403, code `aws.access_denied`
- `ThrottlingException` / `TooManyRequestsException` / `ServiceQuotaExceededException` → 429, code `aws.throttled`
- `ConflictException` / `ResourceInUseException` → 409, code `aws.conflict`
- everything else keeps today's behaviour (re-raise → 500 with the traceback in the log).

The envelope `message` is the AWS message with the boto prefix stripped (no `An error occurred (X) when calling the Y operation:`), and `detail` carries `{"aws_error_code": "...", "operation": "..."}`. The console maps the new codes to localized copy (`apiErrors.aws.not_found` etc., en + zh-CN) through the existing `localizedMessage` / `apiErrors.*` path, so Knowledge Base detail and Registry edit for an unknown id read "not found / access denied", and Memory's toast no longer shows raw boto text. Existing service-level mappings (e.g. `kb.not_found`) stay and take precedence because they raise `AppError` before the ClientError reaches the handler.

### Evidence

- `backend/app/core/errors.py:66-96` — `assume_role_error_handler`: "Every other `ClientError` is re-raised, which leaves it exactly where it was before this handler existed: an unhandled 500 with the AWS error in the log." Registered at `:101` via `app.add_exception_handler(ClientError, …)`.
- `backend/app/core/errors.py:37-38` — `NotFoundError(AppError)` 404 and the `envelope(code, message, detail)` shape every other error uses.
- Live reproduction (2026-09-04, read-only): `curl localhost:8000/api/knowledge-bases/does-not-exist` → `Internal Server Error [500]`; `curl localhost:8000/api/registry/records/does-not-exist` → 500. Calling the services directly: `knowledge.get_kb_detail(ctx, "does-not-exist")` raises `ClientError AccessDeniedException` (IAM denies the unknown-id ARN); `registry_console.console_get(ctx, "does-not-exist")` raises `ClientError ValidationException` ("Value at 'recordId' failed to satisfy constraint…").
- `backend/app/services/knowledge.py:75-79` — `_get_kb` maps only `ResourceNotFoundException`; `grep -rn "except ClientError" backend/app/routers` → 1 hit; 13 scattered `ResourceNotFoundException` mappings under `backend/app/services/`.
- Console side: `/memory?view=short-term&actor=does-not-exist` toasts `Memory lookup failed: memory lookup failed: An error occurred (ResourceNotFoundException) when cal…` (502); `/knowledge-bases?view=detail&kb=does-not-exist` shows "COULD NOT LOAD KNOWLEDGE BASE ! HTTP 500"; `/registry?view=edit&record=does-not-exist` shows "Failed to load record: HTTP 500" (`reports/shots/badid_*.png`).
- `frontend/src/lib/api.ts:330-390` — `ApiError`, `localizedMessage(code, fallback)` and the `apiErrors.*` key family (`frontend/src/locales/{en,zh-CN}/common.json` around line 1600+), incl. `apiErrors.network` added by SE-002.
- Hermetic test pattern: `backend/tests/` build a `ClientError` with `botocore.exceptions.ClientError({"Error": {"Code": "ValidationException", "Message": "…"}}, "GetRegistryRecord")` and monkeypatch the service wrapper (see `backend/tests/conftest.py` note: AWS is not stubbed globally on a credentialed box).
- `/v1` (`backend/app/routers/public_api.py`) shares the app's exception handlers — check its tests still pass and note in `docs/api.md` that AWS-side errors now surface as 4xx envelopes.

### Acceptance checks

- [ ] New hermetic tests in `backend/tests/test_errors_aws.py` (or the existing errors test module): a route whose service raises `ClientError` with each mapped code returns the mapped status + envelope code + stripped message + `detail.aws_error_code`; an unmapped code (e.g. `InternalServerException`) still results in a 500; an assume-role failure still returns 502 `workspace.assume_role_failed`. `cd backend && uv run pytest -q tests/test_errors_aws.py` passes.
- [ ] `curl -s -w ' [%{http_code}]' localhost:8000/api/registry/records/does-not-exist` → `{"code":"aws.validation",…} [400]` and `…/api/knowledge-bases/does-not-exist` → 403 `aws.access_denied` (or 404 if IAM answers not-found) — **live read-only AWS check against the dev stack; run it from your own throwaway backend on another port** (`cd backend && uv run uvicorn app.main:app --port 8011`), never by restarting the host's :8000.
- [ ] Console: `frontend/src/locales/{en,zh-CN}/common.json` gain `apiErrors.aws.not_found|validation|access_denied|throttled|conflict`; `python3 scripts/i18n_check.py` passes; the KB-detail and Registry-edit error blocks render the localized text (screenshot each under `.claude/self-evolution/runs/SE-005/` from a worktree vite pointed at your :8011 backend).
- [ ] `docs/api.md` error section (+ `.zh-CN`) lists the `aws.*` codes; `docs/architecture.md` error-handling paragraph updated.
- [ ] `make verify` passes.

### Notes
- 2026-09-04T14:46:38Z — added as not-started
- 2026-09-04T14:46:57Z — not-started → in-progress
- 2026-09-04T15:29:52Z — in-progress → done: PR #76 squash-merged as 3692638 (auto-merge); host rerun: make verify PASS, live curl from throwaway :8012 (400 aws.validation / 403 aws.access_denied / 404 aws.not_found), one correction: /v1 gets generic messages (role ARN leak)

## SE-006 — Disabled primary actions explain what is missing (shared Btn disabledReason)

- Status: done
- Priority: 6
- Path: ux
- Origin report: reports/research_2026-09-04.md#run-2026-09-04t14-36-36z
- Score: 12 (Importance 3 · Architecture fit 4 · Evidence 5 · Difficulty 2 · Risk 1)
- Branch: evo/se-006-disabled-primary-actions-explain-what-is
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/77

### Requirement

Whenever a form's primary action is disabled because input is missing or invalid, the console must say **what is missing**, in place, in both locales. Add an optional `disabledReason?: string` prop to the shared `Btn` (`frontend/src/components/Btn.tsx`): when set and `disabled`, the button gets `title={disabledReason}` and `aria-describedby` pointing at a small mono hint rendered next to it (same visual weight as `.dim` helper text) that reads the reason; when the button is enabled the hint disappears. Apply it to the six forms that today only dim the button: Registry Register (`▲ REGISTER`), Registry Edit (`▲ SAVE`), Knowledge Base Create (`▲ CREATE`), Strands Studio (`▲ Publish`), Online Evaluation Create (`▸ CREATE`), Workspace detail (`RUN BOOTSTRAP`). Each reason is specific ("Name must be 3–64 chars: lowercase letters, digits, hyphens" / "Add a SKILL.md" / "Pick a source: upload files or an S3 bucket" / "No changes to save" / "Add at least one node" / "Choose an agent and an evaluator" / "Bootstrap already ran / workspace is not registered"), derived from the same predicates that compute `disabled`. Do not change *when* the buttons are disabled.

### Evidence

- Form walk 2026-09-04 (`/tmp/ux_forms.mjs`): Register `primary=[{"txt":"▲ REGISTER","dis":true,"title":""}]`, Edit `▲ SAVE` disabled, KB Create `▲ CREATE` disabled, Studio `▲ Publish` disabled, Online Eval `▸ CREATE` disabled, Workspace detail `RUN BOOTSTRAP` disabled — all with empty `title` and no hint text; `required` count 0 on every form. Screenshot `reports/shots/form_registry_view_register.png`.
- `frontend/src/pages/registry/RegisterView.tsx:35-37` — `regValid = /^[a-z][a-z0-9-]{2,63}$/.test(regName) && (regType === "MCP" ? /^https?:\/\/.+/.test(regUrl) : regMd.trim().length > 0)`; `:159` `disabled={busy || !regValid}`.
- `frontend/src/pages/knowledge/CreateView.tsx:33-35` — `nameValid = NAME_RE.test(name.trim())`, `sourceValid = mode === "upload" ? files.length > 0 : bucket.trim().length > 0`, `canSubmit = nameValid && sourceValid && !busy`; `:159` `disabled={!canSubmit}`.
- `frontend/src/pages/registry/EditView.tsx:471` — `disabled={saving || !dirty || (isSkill && mode === "zip" && !!preview && !preview.valid)}`.
- `frontend/src/pages/CreateAgentStudio.tsx` — Publish button state (grep `Publish`, `publishOpen`); `frontend/src/pages/EvaluationOnline.tsx` — the create form's `▸ CREATE` predicate; `frontend/src/pages/workspaces/DetailView.tsx` — `RUN BOOTSTRAP` predicate.
- Existing good patterns to match: `frontend/src/pages/workspaces/CreateView.tsx:121-141` (explanatory `setError(t("workspacesPage.create.missing"))`), `frontend/src/pages/CreateAgent.tsx:2450,2560` (`permHint` → `title=`).
- `frontend/src/components/Btn.tsx` — 16-line wrapper over `ButtonHTMLAttributes`; the hint needs a wrapping element or a sibling, so keep the DOM change minimal (e.g. render `<span className="btn-hint">` after the button only when `disabled && disabledReason`).

### Acceptance checks

- [ ] `Btn` accepts `disabledReason`; when `disabled` and set, the rendered `<button>` has `title` = reason and `aria-describedby` = the id of a visible hint element containing the reason; when not disabled, no hint element is rendered (add a short note to the component's JSDoc).
- [ ] Playwright check (worktree vite on :5197 → host :8000) on `/registry?view=register`, `/registry?view=edit&record=<any existing id from /api/registry/records>`, `/knowledge-bases?view=create`, `/create/studio`, `/evaluation?view=online&oe=new`, `/workspaces?view=detail&ws=lab-use2`: every disabled `.btn.primary` has a non-empty `title` and a visible hint; typing a valid name + URL on Register makes the hint disappear and the button enable. Save the output as `.claude/self-evolution/runs/SE-006/hints.txt` and screenshots `runs/SE-006/{register,kb_create}.png` (absolute host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-006/`).
- [ ] All reasons are i18n keys in en + zh-CN; `python3 scripts/i18n_check.py` passes; no hardcoded English.
- [ ] `make verify` passes. No live AWS check required (reads only).

### Notes
- 2026-09-04T14:46:38Z — added as not-started
- 2026-09-04T15:29:52Z — not-started → in-progress
- 2026-09-04T15:57:50Z — in-progress → done: PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/77 squash-merged (auto-merge); host rerun: make verify PASS, six forms × en/zh-CN hints verified, one correction: zh-CN architecture twin was missing

## SE-007 — Stale deep links tell the user the resource is gone instead of silently falling back

- Status: done
- Priority: 7
- Path: ux
- Origin report: reports/research_2026-09-04.md#run-2026-09-04t14-36-36z
- Score: 11 (Importance 3 · Architecture fit 4 · Evidence 4 · Difficulty 2 · Risk 1)
- Branch: evo/se-007-stale-deep-links-tell-the-user-the-resou
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/78

### Requirement

A deep link whose id no longer resolves must tell the user, not silently fall back. For these params — Evaluation `?view=datasets&ds=`, `?view=evaluators&ev=`, `?view=online&oe=`, `?view=experiment&exp=`, Chat `?agent=` (and its companion `?session=`), Knowledge Bases `?view=detail&kb=` (missing or unknown) — when the list has loaded and the id is not in it (or the detail fetch answers 404/400/403), render one shared, dismissible notice at the top of the page ("<Thing> `<id>` no longer exists in this workspace — pick one from the table below.") and remove the stale param from the URL (`setSearchParams(..., { replace: true })`). Chat additionally must not silently switch to another agent: keep the picker on the placeholder option until the user chooses. Reuse the wording/pattern Skill Lab already uses. Both locales.

### Evidence

- Probe 2026-09-04 (`/tmp/ux_badid.mjs`, `/tmp/ux_badid2.mjs`): `/evaluation?view=datasets&ds=does-not-exist`, `?view=evaluators&ev=does-not-exist`, `?view=online&oe=does-not-exist`, `?view=experiment&exp=does-not-exist` → URL unchanged, list rendered, no notice; `/chat?agent=does-not-exist` → first active agent selected, no notice; `/knowledge-bases?view=detail` (no `kb`) → "LOADING ▸" forever; `/knowledge-bases?view=detail&kb=does-not-exist` → error block (becomes a proper 4xx after SE-005).
- Good patterns to copy: `/skill-lab?view=eval&job=does-not-exist` → "That job no longer exists in this workspace — pick one from the table above." (`skillLab.*.gone` keys, `frontend/src/locales/en/common.json:1291,1495`); `/workspaces?view=detail&ws=does-not-exist` → "Workspace not found" page (`workspacesPage.*.gone`, `:3419`); `/observability?tab=sessions&session=…` → "SESSION NOT FOUND — …" (`:2662`).
- Param readers: `frontend/src/pages/EvaluationDatasets.tsx:335` (`dsParam`), `EvaluationEvaluators.tsx:200` (`evParam`), `EvaluationOnline.tsx:399` (`oeParam`), `EvaluationExperiment.tsx:293,391` (`exp`), `Chat.tsx:98-136` (`linkedAgent`, fallback `setAgentId(active[0].id)` with the comment "Linked agent unknown/inactive: drop the linked session too"), `KnowledgeBases.tsx:123` (`kbId={searchParams.get("kb") ?? ""}`) + `knowledge/DetailView.tsx`.
- Shared components: `frontend/src/components/LoadError.tsx` (SE-002) is for failed loads — this is a different state (loaded, id absent); add a small `StaleLink` / `GoneNotice` component next to it, or generalise Skill Lab's block.

### Acceptance checks

- [ ] One shared notice component under `frontend/src/components/` used by the six surfaces; text via i18n keys (en + zh-CN), interpolating the kind and id.
- [ ] Playwright (worktree vite :5197 → host :8000): each of the six URLs with `does-not-exist` shows the notice, the stale param is gone from `location.search` after render, and — for Chat — `select[data-testid=agent-select]` value is `""` (placeholder), not another agent's id. `/knowledge-bases?view=detail` without `kb` shows the notice instead of a permanent "LOADING". Save output as `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-007/stale_links.txt` + one screenshot per surface in that directory.
- [ ] A valid deep link (an existing `ds`/`ev`/`oe`/`exp`/`agent` id taken from the corresponding `/api/...` list) still selects the row / agent exactly as before (assert in the same script — no regression).
- [ ] `make verify` passes; `python3 scripts/i18n_check.py` passes. No live AWS check beyond the running dev stack's reads.

### Notes
- 2026-09-04T14:46:38Z — added as not-started
- 2026-09-04T15:57:51Z — not-started → in-progress
- 2026-09-04T16:22:09Z — in-progress → done: PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/78 squash-merged (auto-merge); host rerun: make verify PASS, own Playwright probe: 7 stale links notice+param stripped, chat picker stays on placeholder, valid ds/agent links unchanged; no correction needed

## SE-008 — zh-CN typography: full-width punctuation inside Chinese copy, consistently

- Status: done
- Priority: 8
- Path: ux
- Origin report: reports/research_2026-09-04.md#run-2026-09-04t14-36-36z
- Score: 11 (Importance 2 · Architecture fit 5 · Evidence 5 · Difficulty 2 · Risk 1)
- Branch: evo/se-008-zh-cn-typography-full-width-punctuation-
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/79

### Requirement

Chinese copy in `frontend/src/locales/zh-CN/common.json` uses **full-width punctuation consistently** when the punctuation sits inside or directly after Chinese text: `，` `：` `；` `？` `！` `（` `）` instead of `,` `:` `;` `?` `!` `(` `)`. Rules: convert only when the character is adjacent to a CJK character (either side); never touch anything inside `{{…}}` placeholders, backticks, URLs/ARNs/paths, identifiers such as `SKILL.md`, `session.id`, `us-west-2`, or purely Latin/technical fragments; keep `——` (correct Chinese dash) and `·`; leave `en/common.json` untouched. Do it with a script committed under `scripts/` (e.g. `scripts/i18n_zh_punct.py --check|--fix`) so the rule is mechanical and re-runnable, and wire `--check` into `scripts/verify.sh` next to `i18n_check.py` so drift cannot return.

### Evidence

- Scan 2026-09-04 over 2853 zh-CN keys: half-width `,` between CJK 58 keys · `:` after CJK 46 · `(` before CJK 53 · `)` after CJK 49 · `?` 5 · `;` 22 → **351 distinct keys**, while **316 keys already use full-width** `，：（）；？` — the locale mixes both styles today (e.g. `evalPage.runs.failureReason = "失败原因:"` vs full-width elsewhere; `knowledge.source.prefix = "前缀(可选)"`; `skillLab.tasksets.err.idUnsafe = "id 会用作任务工作目录名,必须文件系统安全(不含 '/'、'\\' 或 '..')"`).
- 0 untranslated values, 0 whitespace oddities, 3 identical en==zh values (all product nouns) — translation quality is otherwise good, so this is the remaining copy-register issue.
- `scripts/i18n_check.py` and `scripts/verify.sh` — the existing parity gate to extend.
- The Chinese-writing conventions in the repo's workshop content (`.claude/skills/aws-workshop-content`) and `humanizer-zh` both call for full-width punctuation in prose.

### Acceptance checks

- [ ] `python3 scripts/i18n_zh_punct.py --check` exits 0 on the branch and exits 1 (listing keys) when a half-width `,`/`:`/`(`/`)`/`;`/`?` is reintroduced next to CJK text (add a small pytest or shell assertion for the script under `backend/tests/` or `scripts/`, hermetic).
- [ ] `scripts/verify.sh` runs the `--check`; `make verify` passes.
- [ ] `git diff --stat main -- frontend/src/locales` touches only `zh-CN/common.json` (+ the script/gate files); `python3 scripts/i18n_check.py` parity still passes; **no `{{placeholder}}`, URL, ARN, path, or code identifier changed** — prove it with a diff filter in the report (e.g. `git diff main -- frontend/src/locales/zh-CN/common.json | grep '^[-+]' | grep -c '{{'` equal on both sides).
- [ ] Spot check in the console (worktree vite): `/evaluation` runs detail "失败原因：" and `/knowledge-bases?view=create` "前缀（可选）" render full-width; one screenshot under `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-008/`.

### Notes
- 2026-09-04T14:46:38Z — added as not-started
- 2026-09-05T01:39:00Z — not-started → in-progress
- 2026-09-05T02:01:19Z — in-progress → done: PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/79 squash-merged (auto-merge); host rerun: make verify PASS incl. new i18n_zh_punct gate; independent diff check 182 keys, punctuation-only, en untouched; no correction needed


## SE-009 — Evaluation runs can be stopped (StopBatchEvaluation + queued cancel)

- Status: in-review
- Priority: 9
- Path: agentcore
- Origin report: reports/research_2026-09-05.md#run-2026-09-05t02-21-43z
- Score: 11 (Importance 4 · Architecture fit 4 · Evidence 4 · Difficulty 3 · Risk 2)
- Branch: evo/se-009-evaluation-runs-can-be-stopped-stopbatch
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/80

### Requirement

An operator can stop an evaluation run from the Evaluation Runs page before AWS finishes it.

- A run in status `invoking`, `waiting` or `evaluating` that has a `batch_eval_id` shows a **STOP** action (row + run detail). Clicking it (with the shared ConfirmDialog) calls a new console route `POST /api/evaluation/runs/{run_id}/stop`, which calls `StopBatchEvaluation(batchEvaluationId=…)` through a wrapper in `backend/app/evaluation/agentcore_eval.py` (the only place AgentCore evaluation shapes live) using the data-plane client from `aws_clients.py`.
- A run still `queued` (never submitted to AWS, `batch_eval_id` is null) can be **cancelled** locally: the route marks it and the queue worker skips it when it dequeues (the `EvaluationQueue` in `queue.py` has no cancel today — add a cancelled-set check at the start of the submitted callable, or equivalent).
- The poller in `service.py` treats AWS `STOPPING` as still-running and `STOPPED` as a new terminal ledger status `stopped` (not `failed`): scores for already-judged sessions are parsed with the existing `parse_eval_scores` / `parse_insights` when present, and the run row shows a "stopped by operator" reason. `COMPLETED_WITH_ERRORS` handling is unchanged.
- The Runs list/detail render `stopped` as its own chip (en + zh-CN), the disabled STOP button explains why via `disabledReason` (already completed / failed / no batch id yet), and the queue-state banner keeps counting only active runs.
- `docs/api.md` (+ zh-CN twin) documents the route; `docs/architecture.md` Evaluation row mentions stop/cancel and the `stopped` status.

Out of scope: `DeleteBatchEvaluation` (ledger and AWS would disagree about results), stopping online evaluation configs (already has pause).

### Evidence

- `backend/app/evaluation/routers.py:784-931` — `list_runs`, `get_run`, `create_run`, `queue_state` — no stop/cancel route exists.
- `backend/app/evaluation/service.py:183-371` — status transitions `queued → invoking → waiting → evaluating`; `:308-311` any AWS status other than `COMPLETED`/`COMPLETED_WITH_ERRORS` becomes `failed` with "batch evaluation ended <status>", so a stop today would render as a failure.
- `backend/app/evaluation/queue.py:19-79` — `EvaluationQueue.submit/_drain/state/position`; no cancel.
- `backend/app/evaluation/agentcore_eval.py:216-319` — `start_batch_evaluation`, `get_batch_evaluation`, `parse_eval_scores` — add `stop_batch_evaluation` beside them.
- `backend/app/evaluation/models.py:41-52` — `EvalRun.status` is `String(16)` default `queued`; `stopped` fits.
- `frontend/src/pages/Evaluation.tsx:346-368` — status → chip/label mapping (`queued`, `completed`, `failed`); `:1068` run-detail error block.
- botocore model `bedrock-agentcore/2024-02-28` (apiVersion 2024-02-28): `StopBatchEvaluation` input `{batchEvaluationId}` → output `{batchEvaluationId, batchEvaluationArn, status, description}`, HTTP 202; `BatchEvaluationStatus` enum `PENDING, IN_PROGRESS, COMPLETED, COMPLETED_WITH_ERRORS, FAILED, STOPPING, STOPPED, DELETING`.
- Docs (accessed 2026-09-05): SDK reference for StopBatchEvaluation — "Stops a running batch evaluation. Sessions that have already been evaluated retain their results." Regional availability: `Bedrock AgentCore+StopBatchEvaluation` isAvailableIn us-west-2 and us-east-1.
- `docs/architecture.md` Evaluation row: runs execute through a bounded-concurrency queue capped at the 5 active-batch-evaluations account quota — a stuck run holds a slot.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/ -q -k "eval and stop"` — new hermetic tests (stubbed data-plane client) cover: stop of an `evaluating` run calls `stop_batch_evaluation` with the ledger's `batch_eval_id` and returns 202/200 with the run; stop of a `completed` run returns 409 in the error envelope; cancel of a `queued` run never calls AWS and the worker skips it; poller maps AWS `STOPPED` (with partial `evaluatorScores`) to ledger status `stopped` with parsed scores and `STOPPING` to still-running.
- [ ] `frontend`: `npx tsc --noEmit && npm run lint` pass; `src/lib/api.ts` has the stop call and the `stopped` status in the run type; en/zh-CN keys added with parity (`python3 scripts/i18n_check.py`).
- [ ] `docs/api.md` + `docs/api.zh-CN.md` list `POST /api/evaluation/runs/{run_id}/stop`; `docs/architecture.md` Evaluation row mentions stop/cancel + `stopped`.
- [ ] `make verify` passes.
- [ ] Live AWS check: **not required by the gate**; the host may verify on a dev run later (StopBatchEvaluation on a real IN_PROGRESS batch). Record in the report that it was not run.

### Notes

- Wrapper naming: keep AgentCore evaluation shapes in `agentcore_eval.py` per CLAUDE.md volatility rule; do not call `boto3.client` anywhere (client funnel test).
- `resume_pending_jobs`-style restarts: `service.py:358-371` already re-attaches to `evaluating` runs on startup — make sure a `stopped` run is terminal there too.
- The route must be registered in `route_policy.py` if that file gates new routes (check `backend/app/core/route_policy.py` — memory note says new GET routes needed it).
- 2026-09-05T02:25:26Z — added as not-started
- 2026-09-05T02:25:44Z — not-started → in-progress
- 2026-09-05T02:52:24Z — in-progress → in-review: PR #80 open; host rerun make verify PASS + 14 -k 'eval and stop' tests + route_policy/client_funnel 410 passed; live StopBatchEvaluation not exercised

## SE-010 — Agent detail lists AWS versions and endpoints (Runtime + Harness)

- Status: in-review
- Priority: 10
- Path: agentcore
- Origin report: reports/research_2026-09-05.md#run-2026-09-05t02-21-43z
- Score: 10 (Importance 3 · Architecture fit 4 · Evidence 4 · Difficulty 3 · Risk 1)
- Branch: evo/se-010-agent-detail-lists-aws-versions-and-endp
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/81

### Requirement

The agent detail on `/create` (the list's details mode) gains a read-only **VERSIONS & ENDPOINTS** panel backed by AWS, for every runtime-backed or harness-backed agent.

- New console route `GET /api/agents/{agent_id}/versions` returns `{kind: "runtime"|"harness", versions: [{version, status, description, last_updated_at}], endpoints: [{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason?}], latest_version, ledger_version, next_token?}` — following every page (AWS caps pages; round-trip `next_token` like the Memory console does).
  - `method in {zip_runtime, studio, container, discovered_runtime(resource_type runtime)}` → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints` on the agent's runtime id.
  - `method == harness` or a discovered harness → `ListHarnessVersions` + `ListHarnessEndpoints` on the harness id.
  - Any other shape (e.g. an agent with no AWS resource yet, status `deploying`/`failed` without an ARN) → 409 `agent.no_resource` in the standard error envelope with a reason the UI can show.
- Wrappers live in `backend/app/services/agentcore/runtime.py` (`list_runtime_versions`, `list_runtime_endpoints`) and `backend/app/services/agentcore/harness.py` (`list_harness_versions`, `list_harness_endpoints`), taking the control client explicitly; the projection is allow-listed (no environment values, artifact locations or authorizer config — same rule as discovery).
- The panel marks the `DEFAULT` endpoint, highlights the version the ledger recorded (`Agent.version`) vs AWS latest, and flags the canary `stable`/`treatment` endpoint names when present so leftovers are visible. Loading / empty / error states use the shared `Panel`/`LoadError`; both locales; no new nested route (details mode already exists).
- `docs/architecture.md` (Runtime + Harness rows or the discovery section) and `docs/api.md` (+ zh-CN twin) document the route.

### Evidence

- `backend/app/services/agentcore/runtime.py:220-275` — `create_runtime_endpoint`, `update_runtime_endpoint`, `get_runtime_endpoint`, `delete_runtime_endpoint`, `wait_endpoint_ready`: endpoints are already a first-class wrapper concept; no list operation exists.
- `backend/app/optimization/canary_infra.py:294-340` — `ensure_endpoint_ready` mints `stable`/`treatment` named endpoints; nothing lists them afterwards.
- `backend/app/deployer/zip_runtime.py:550-577`, `backend/app/deployer/container.py:249-268` — ledger `row.version` is set from `agentRuntimeVersion` at deploy time only.
- `backend/app/routers/agents.py:224` — `GET /agents/{agent_id}` exists; add the sibling route there.
- `backend/app/services/agentcore/harness.py:19-89` — harness wrappers (`get_harness`, `list_harnesses`, …); add the list-versions/list-endpoints siblings.
- `frontend/src/pages/CreateAgent.tsx:2273-2380` — details mode panels (`create.launchPanel.*`, conversion panel, KB mounted panel) — insertion point.
- botocore `bedrock-agentcore-control/2023-06-05`: `ListAgentRuntimeVersions(agentRuntimeId, maxResults, nextToken)` → `agentRuntimes[] {agentRuntimeArn, agentRuntimeId, agentRuntimeVersion, agentRuntimeName, description, lastUpdatedAt, status}`; `ListAgentRuntimeEndpoints(agentRuntimeId, …)` → `runtimeEndpoints[] {name, liveVersion, targetVersion, agentRuntimeEndpointArn, agentRuntimeArn, status, id, description, createdAt, lastUpdatedAt}`; `ListHarnessVersions(harnessId, …)` → `harnessVersions[]`; `ListHarnessEndpoints(harnessId, …)` → `endpoints[] {harnessId, harnessName, endpointName, arn, status, createdAt, updatedAt, liveVersion, targetVersion, description, failureReason}`.
- Docs (accessed 2026-09-05) https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html — every update creates an immutable version, `DEFAULT` auto-follows latest, named endpoints pin; endpoint states `CREATING, CREATE_FAILED, READY, UPDATING, UPDATE_FAILED`. Regional availability: `ListAgentRuntimeVersions` isAvailableIn us-west-2 and us-east-1.
- `docs/architecture.md` §The invoke chain: "AgentCore pins an existing runtime session to the version that first served it, so a post-republish validation must start a new Chat session" — the operator currently has no way to see the versions involved.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/ -q -k versions` — hermetic tests with a stub control client: runtime agent → both list ops called with the runtime id, pages followed via `nextToken`, projection contains only the allow-listed keys; harness agent → harness ops called; agent without a resource → 409 `agent.no_resource`; workspace scoping honoured (agent from another workspace → 404).
- [ ] `tests/test_client_funnel.py` still passes (no new `boto3.client`).
- [ ] Frontend: panel renders loading/empty/error; `DEFAULT` marked; ledger-vs-latest mismatch visible; `npx tsc --noEmit && npm run lint`; i18n parity.
- [ ] `docs/api.md` + zh-CN twin document `GET /api/agents/{agent_id}/versions`; `docs/architecture.md` updated.
- [ ] `make verify` passes.
- [ ] Live AWS check: **not required by the gate** (read-only ops; host may spot-check against a dev runtime later).

### Notes

- Keep it read-only: no re-pointing of `DEFAULT`, no endpoint create/delete from this panel (the canary owns those).
- HarnessSummary has no `description`; use `updatedAt` for harness rows (memory `agentcore-api-shape-lookup`).
- Register the new GET in `route_policy.py` if that file gates routes.
- 2026-09-05T02:25:26Z — added as not-started
- 2026-09-05T02:52:24Z — not-started → in-progress
- 2026-09-05T03:18:06Z — in-progress → in-review: PR #81 open; host rerun make verify PASS + 16 -k versions tests + route_policy/client_funnel; screenshots panel-*-{ready,sync,empty,error,no_resource}.png reviewed; live read not exercised

## SE-011 — Chat can end the AgentCore Runtime session (StopRuntimeSession)

- Status: in-review
- Priority: 11
- Path: agentcore
- Origin report: reports/research_2026-09-05.md#run-2026-09-05t02-21-43z
- Score: 10 (Importance 3 · Architecture fit 4 · Evidence 4 · Difficulty 2 · Risk 2)
- Branch: evo/se-011-chat-can-end-the-agentcore-runtime-sessi
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/82

### Requirement

Chat can end the live AgentCore Runtime session behind a conversation, instead of only forgetting it locally.

- New console route `POST /api/chat/{agent_id}/sessions/{session_id}/stop` calls `StopRuntimeSession(agentRuntimeArn=<agent's runtime ARN>, runtimeSessionId=<session_id>)` through a new wrapper in `backend/app/services/agentcore/runtime.py` (data-plane `bedrock-agentcore` client from `aws_clients.py`), only for runtime-backed agents (`zip_runtime`, `studio`, `container`, `discovered_runtime` with `resource_type` runtime). Harness agents and A2A/discovered-harness rows get 409 `chat.session_stop_unsupported` with a reason (there is no harness session-stop operation in the model).
- Semantics: AWS `ResourceNotFoundException` (session already gone / idle-expired) is reported as success with `already_ended: true`, not an error; `RetryableConflictException` is left to the SDK's default retries and, if it still surfaces, maps to 409 via the existing ClientError → envelope mapping. The ledger `ChatSession` row is kept (history stays replayable) and gets an `ended_at` timestamp (new nullable column, additive migration consistent with how the ledger evolves today) so the history rail can show "ended".
- UI (`frontend/src/pages/Chat.tsx`): an **END SESSION** button next to **NEW SESSION** for the current session, and a per-row action in the history rail; both use the shared `Btn` with `disabledReason` (no session yet / harness agent / already ended) and a toast on success. **NEW SESSION** itself keeps its current behaviour (does not auto-stop) — ending is explicit.
- Optional, if cheap: the Observability session detail shows the same action when the session resolves to a runtime-backed Launchpad agent.
- `docs/api.md` (+ zh-CN) documents the route; `docs/architecture.md` (Chat / invoke chain or Memory console section that talks about sessions) mentions explicit session end and the version-pinning motivation.

### Evidence

- `frontend/src/pages/Chat.tsx:310-318` — `newSession` only resets local state; `:413-421` current-session chip + NEW SESSION button; `:495-520` history rail rows (`restoreSession`).
- `backend/app/routers/chat.py:201-253` — `list_sessions` (ledger `ChatSession` rows); no mutation route for a session.
- `backend/app/services/agentcore/runtime.py` — control-plane wrappers only; `backend/app/services/invoke.py` / `chat.py` own the data-plane `InvokeAgentRuntime` call — put `stop_runtime_session(client, *, runtime_arn, session_id, qualifier=None)` next to the other runtime wrappers and pass the data-plane client explicitly.
- `backend/app/models/ledger.py` — `ChatSession` (session_id, actor_id, turns, last_at, workspace_id).
- botocore `bedrock-agentcore/2024-02-28`: `StopRuntimeSession` input `{runtimeSessionId*, agentRuntimeArn*, qualifier, clientToken}` → `{runtimeSessionId, statusCode}`, `POST /runtimes/{agentRuntimeArn}/stopruntimesession`; no `*Harness*Session*` operation exists in either model.
- Docs (accessed 2026-09-05) https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-stop-session.html — "instantly terminates the specified session and stops any ongoing streaming responses"; scenarios: user-initiated end, quota management, stalled sessions; 404 = "Session not found or already terminated"; 409 `RetryableConflictException` is transient and auto-retried by SDKs. Availability: `Bedrock AgentCore+StopRuntimeSession` isAvailableIn us-west-2 and us-east-1.
- `docs/architecture.md` §The invoke chain — sessions are pinned to the version that first served them; explicit end + new session is the documented validation recipe after a re-publish.
- IAM: the console's caller role needs `bedrock-agentcore:StopRuntimeSession`; check `backend/app/services/workspace_iam.py` (derived workspace policies list allowed actions, e.g. `:197` `UpdateGateway`) and `infra/` for where console-side AgentCore actions are granted, and add the action there.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/ -q -k "session and stop"` — hermetic tests with a stubbed data-plane client: runtime agent → `stop_runtime_session` called with the agent's runtime ARN + session id, 200 `{ended: true, already_ended: false}`, `ChatSession.ended_at` set; stub raising `ResourceNotFoundException` → 200 `already_ended: true`; harness agent → 409 `chat.session_stop_unsupported`; session from another workspace/agent → 404.
- [ ] `tests/test_client_funnel.py` passes (no new `boto3.client`).
- [ ] Frontend: END SESSION disabled with a reason when no session / harness agent; enabled after a turn; history rows show ended state; `npx tsc --noEmit && npm run lint`; i18n parity.
- [ ] Docs: `docs/api.md` + zh-CN route entry; `docs/architecture.md` sentence on explicit session end; if IAM changed, the grants table / `workspace_iam.py` note.
- [ ] `make verify` passes.
- [ ] Live AWS check: **declared, not required by the gate** — a real stop on a dev-account runtime session (invoke once, stop, invoke again with the same session id and observe a fresh session) is left for the host. Record as not run.

### Notes

- Do not stop sessions from `/v1` (public API) in this direction; console only.
- Register the new POST in `route_policy.py` if that file gates routes.
- 2026-09-05T02:25:26Z — added as not-started
- 2026-09-05T03:18:09Z — not-started → in-progress
- 2026-09-05T03:43:51Z — in-progress → in-review: PR #82 open; host rerun make verify PASS + 17 -k 'session and stop' tests; IAM widening of shared exec role flagged for reviewer; live StopRuntimeSession not exercised

## SE-012 — Memory resources: edit description and event expiry (UpdateMemory)

- Status: in-review
- Priority: 12
- Path: agentcore
- Origin report: reports/research_2026-09-05.md#run-2026-09-05t02-21-43z
- Score: 8 (Importance 2 · Architecture fit 4 · Evidence 4 · Difficulty 2 · Risk 2)
- Branch: evo/se-012-memory-resources-edit-description-and-ev
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/83

### Requirement

The Memory console's `?view=resources` sub-page can edit an existing memory resource's **description** and **event expiry (days, 7–365)**.

- New route `PUT /api/memory/resources/{memory_id}` in `backend/app/routers/memory_resources.py` → `memory_admin.update_memory_resource(workspace, memory_id, *, description, event_expiry_days)` → `UpdateMemory`. Both fields optional; at least one required (422 otherwise). Strategies and namespace keys are **not** editable here.
- **Namespace-key trap (must be handled and tested):** the model documents `UpdateMemory.namespaceKeys` as "fully replaces the existing set — any key you omit is removed". The update must therefore never send `namespaceKeys` (omit the member entirely) and the hermetic test asserts the call kwargs contain exactly `memoryId`, optional `description`, optional `eventExpiryDuration`, optional `clientToken` — nothing else. If live verification later shows omission also clears keys, the fallback is to re-send `GetMemory().namespaceKeys`; leave a comment naming that fallback.
- Guard rails mirror delete: the workspace default memory is editable (only description/expiry — harmless) but the UI states that expiry changes affect every agent using it; a memory referenced by live agents shows those agents in the confirm dialog (reuse `_agents_by_memory`).
- The structural read-only guarantee stays: `tests/test_memory_console.py` must still pass (no `UpdateMemory` in `memory_console.py` / `memory.py`); the admin pair is the only place it appears.
- UI: an EDIT action on each resource row opens an inline form (description, expiry) with the shared Btn/ConfirmDialog; both locales; `docs/architecture.md` Memory console table `resources` row + `docs/api.md` (+ zh-CN) updated.

### Evidence

- `backend/app/routers/memory_resources.py:109-160` — GET list, POST create, GET one, DELETE (with `memory.in_use` 409 guard); no PUT.
- `backend/app/services/memory_admin.py:93-103` (`_detail` exposes `description`, `event_expiry_days`), `:156-201` (`create_memory_resource` builds `eventExpiryDuration`, `description`, `memoryStrategies`, namespace keys), `:234` (`delete_memory_resource`).
- `frontend/src/pages/memory/ResourcesTab.tsx:74-155` (create form state), `:282-360` (inputs for name/description/expiry/strategies) — reuse the same field components for the edit form.
- `docs/architecture.md` §The Memory console — "Read-only is structural … no wrapper or handler for … `UpdateMemory` … exists in either file, and `tests/test_memory_console.py` asserts that. The one mutating surface — the `resources` view — therefore lives in a separate pair (`services/memory_admin.py` + `routers/memory_resources.py`)". This direction extends that pair only.
- botocore `bedrock-agentcore-control/2023-06-05` `UpdateMemory` members: `clientToken, memoryId*, description, eventExpiryDuration (7–365), memoryExecutionRoleArn, memoryStrategies, addIndexedKeys, namespaceKeys ("fully replaces the existing set — any key you omit is removed"), streamDeliveryResources`.
- Availability (2026-09-05): `Bedrock AgentCore Control+UpdateMemory` isAvailableIn us-west-2 and us-east-1.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/test_memory_resources.py tests/test_memory_console.py -q` — new tests: PUT with description only / expiry only / both → `update_memory` kwargs exactly as specified (no `namespaceKeys`, no `memoryStrategies`); expiry 6 or 366 → 422; neither field → 422; response is the refreshed `_detail` projection; the read-only structural test still passes.
- [ ] Frontend: EDIT opens inline form, saves, refreshes the row; `npx tsc --noEmit && npm run lint`; i18n parity.
- [ ] `docs/architecture.md` Memory console `resources` row lists update; `docs/api.md` + zh-CN document `PUT /api/memory/resources/{memory_id}`.
- [ ] `make verify` passes.
- [ ] Live AWS check: **declared, not required by the gate** — on a dev memory created for the test with one namespace key, update the description and confirm `GetMemory().namespaceKeys` is unchanged; delete the test memory afterwards. Left for the host; record as not run.

### Notes

- Register the new PUT in `route_policy.py` if that file gates routes; IAM for `bedrock-agentcore:UpdateMemory` on the console role — check `workspace_iam.py` / infra grants the way delete is granted.
- 2026-09-05T02:25:26Z — added as not-started
- 2026-09-05T04:41:11Z — not-started → in-progress
- 2026-09-05T05:07:23Z — in-progress → in-review: PR #83 open; host rerun make verify PASS + memory_resources/memory_console/route_policy/client_funnel 486 passed; confirm.png reviewed; live UpdateMemory namespaceKeys check not exercised

## SE-013 — Governance gateway detail manages Gateway rate limits

- Status: in-review
- Priority: 13
- Path: agentcore
- Origin report: reports/research_2026-09-05.md#run-2026-09-05t02-21-43z
- Score: 8 (Importance 4 · Architecture fit 3 · Evidence 4 · Difficulty 4 · Risk 3)
- Branch: evo/se-013-governance-gateway-detail-manages-gatewa
- PR: https://github.com/aws-samples/sample-agentcore-launchpad/pull/84

### Requirement

The Governance gateway detail (`/governance?view=gateway&…`) gains a **RATE LIMITS** panel that manages AgentCore Gateway rate limits (GA August 2026) for Launchpad-managed gateways.

- Routes under `/api/governance/gateways/{gateway_id}/rate-limits`: `GET` (list, follows `nextToken`), `POST` (create), `PUT /{rate_limit_id}` (update `entries` + `description`; `dimensionKeys` are immutable), `DELETE /{rate_limit_id}`. Mutations require the Launchpad managed tag exactly like policy mutations do; reads work on any gateway. Wrappers live in `backend/app/services/agentcore/policy.py` (or a sibling `gateway_limits.py` under `agentcore/`) and take the control client explicitly.
- Validation server-side before calling AWS (422 with a specific reason): 1–10 dimension keys drawn from the documented set (`targetName`, `toolName`, `qualifiedModelId`, `$.context.jwt.<claim>`, `$.context.iam.principal`, `$.context.iam.sourceIdentity`); each entry's `dimensions` has exactly the parent keys; `*` only in trailing positions; rate 0–10 000 000; metric/period matrix — `requests` {second, minute}, `tokens` {minute}, `connections` {second}; at least one metric per entry. AWS `ConflictException` (duplicate dimension-key set) maps to 409 through the existing ClientError envelope.
- UI panel: table of rate limits (id, dimension keys, entry count, status chip `CREATING/ACTIVE/UPDATING/DELETING`, updated), expandable entries, **ADD RATE LIMIT** form (dimension-key picker with a free-text JWT claim, entries editor with per-metric rate + period, description), **EDIT ENTRIES**, **DELETE** with ConfirmDialog. A visible note states the documented semantics: effective rate = min(service-managed, configured), propagation ≤ 30 s, fail-open, rate 0 blocks. Disabled actions explain why (`disabledReason`: not managed / status not ACTIVE). Both locales.
- Every mutation is journaled in `policy_changes` like other Gateway mutations (see `docs/architecture.md` "Every mutation is journaled locally while AWS remains the source of current state"), and the audit view shows them.
- Docs: `docs/architecture.md` Gateway/Policy rows + §Existing Gateway governance; `docs/api.md` (+ zh-CN) for the four routes.

Out of scope: `BatchPutGatewayRateLimits` (whole-set replace) and token-limit estimation UI.

### Evidence

- `backend/app/routers/governance.py:42-367` — gateway routes incl. manage/unmanage, policies CRUD (`:156-251`), mode, audit, decisions — the managed-tag gate and 202/operation pattern to mirror.
- `backend/app/services/governance.py:237-264, 601-653` — gateway detail projection (`targets`, `target_count`, actions); `backend/app/services/agentcore/policy.py:223-253` — `update_gateway` rebuild with replace semantics (style reference for wrapper docstrings).
- `frontend/src/pages/governance/GatewayDetailView.tsx:334-898` — panels identity / registry / engine / iam / policies / targets; insert RATE LIMITS after policies.
- botocore `bedrock-agentcore-control/2023-06-05` (apiVersion 2023-06-05): `CreateGatewayRateLimit(gatewayIdentifier*, clientToken, rateLimitId, description, dimensionKeys*[DimensionKey], entries*[LimitEntry{dimensions*: map, requests: [RateConfig{rate*, period*}], tokens: [...], connections: [...]}])` → `GatewayRateLimitDetail{rateLimitId, gatewayIdentifier, description, dimensionKeys, entries, status, createdAt, updatedAt}`; `UpdateGatewayRateLimit(gatewayIdentifier*, rateLimitId*, description, entries*)`; `ListGatewayRateLimits(gatewayIdentifier*, maxResults, nextToken)` → `rateLimits[]`; `GetGatewayRateLimit`, `DeleteGatewayRateLimit`, `BatchPutGatewayRateLimits` also present.
- Docs (accessed 2026-09-05): release notes https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/release-notes.html §"Gateway: Configurable rate limits" (Aug 2026); https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits.html (components, status lifecycle, limits 50/gateway, 1000 entries, 10 keys, rate 0–10 000 000, fail-open, ≤30 s); `gateway-rate-limits-dimensions.html` (six dimension keys, `*` trailing-only, most-specific wins, unresolvable key ⇒ limit skipped); `gateway-rate-limits-metrics.html` (metric/period/target matrix). Availability: `Bedrock AgentCore Control+CreateGatewayRateLimit` isAvailableIn us-west-2 and us-east-1.
- `git log -S GatewayRateLimit` — never implemented or removed in this repository.

### Acceptance checks

- [ ] `cd backend && uv run pytest tests/ -q -k rate_limit` — hermetic tests with a stub control client: list follows pagination; create sends exactly the validated payload; each validation rule above yields 422 with its reason (bad key, `*` in a leading position, wrong period for `tokens`, rate > 10 000 000, entry dimensions ≠ keys, no metric); mutation on an unmanaged gateway → 403/409 per the existing manage gate; stub `ConflictException` → 409 envelope; a `policy_changes` row is written per mutation.
- [ ] `tests/test_client_funnel.py` passes.
- [ ] Frontend: panel renders list/empty/error; ADD form blocks invalid `*` placement client-side too; `npx tsc --noEmit && npm run lint`; i18n parity + zh-CN punctuation check.
- [ ] Docs: architecture + api (en/zh-CN) updated.
- [ ] `make verify` passes.
- [ ] Live AWS check: **declared, not required by the gate** — create a `[targetName]` / `*` requests-per-minute limit on the dev `launchpad-gw`, observe `ACTIVE`, delete it. Left for the host; record as not run.

### Notes

- Register the new routes in `route_policy.py` if that file gates routes; IAM for `bedrock-agentcore:*GatewayRateLimit*` on the console role — check `workspace_iam.py:197` (already lists `UpdateGateway`) and infra grants.
- Rate limits are evaluated before Policy (blog "Configure rate limits for AI traffic on AgentCore gateway") — worth one sentence in the panel note, but do not build Policy coupling.
- 2026-09-05T02:25:26Z — added as not-started
- 2026-09-05T05:07:25Z — not-started → in-progress
- 2026-09-05T05:39:02Z — in-progress → in-review: PR #84 open; host rerun make verify PASS + 37 -k rate_limit tests + route_policy/client_funnel 416; ui-form/ui-list/ui-error screenshots reviewed; live CreateGatewayRateLimit not exercised

