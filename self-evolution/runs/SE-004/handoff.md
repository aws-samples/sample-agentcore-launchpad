# Direction SE-004 — Small viewports: no horizontal page scroll below 720 px

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch `evo/se-004-small-viewports-no-horizontal-page-scrol`, created
from `main`. A host session wrote this brief, will independently re-run the acceptance
checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

Below the existing 720 px breakpoint the console must never scroll horizontally as a **page**: wide content (tables, code blocks, toolbars, long `<select>`s) scrolls inside its own container or wraps. Verify at a 390×844 viewport on every route; the shell already collapses the sidebar into a nav strip at that width, so this completes a deliberate responsive design rather than starting one. Desktop layout (≥ 1180 px) must not change.

## Repository evidence and extension points

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

The load-bearing frontend patterns from `CLAUDE.md` that apply here:

- React + Vite + `react-router-dom`, TypeScript strict; top-level routes in `frontend/src/App.tsx`; sub-surfaces are `?view=` query params, never nested routes.
- Every user-facing string is an i18n key in `frontend/src/locales/{en,zh-CN}/common.json`; `python3 scripts/i18n_check.py` enforces en ↔ zh-CN parity (part of `make verify`).
- `frontend/src/lib/api.ts` is the single typed backend client; shared UI primitives live in `frontend/src/components/` (`Btn`, `Panel`, `ViewHead`, `DataTable`, `Pager`, `Chip`, `Toast`, `ConfirmDialog`) and the house CSS in `frontend/src/theme/app.css`.
- Frontend gate = `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (no unit-test runner); browser evidence comes from Playwright — import it from `/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs` and launch chromium with `args:['--no-sandbox']`. The host's dev stack is on http://localhost:5173 (backend :8000); confirm with `curl -s localhost:5173/ | grep -o '<title>[^<]*'` → `AgentCore Launchpad`. It serves the HOST checkout, not your worktree — to see YOUR changes, run your own throwaway frontend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it with `pkill -f "port 5197"` when done). Never restart the host's :8000 backend or :5173 vite.
- Set `localStorage.i18nextLng` (`en` / `zh-CN`) via `context.addInitScript` to pick a locale in Playwright.

Notes specific to this direction:
- Run EVERY command in the FOREGROUND — never run_in_background, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- Save probe output and screenshots to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-004/` (not the relative path inside your worktree). Nothing under `.claude/` is committed.
- Merge-friendliness: an open PR (#73, branch `evo/se-002-unreachable-backend-health-chip-reflects`) adds `error`/`onRetry` props to `frontend/src/components/DataTable.tsx` and a third render branch (header table + `<LoadError>`), each branch wrapping `<table>` in a bare `<div>`. Keep your `DataTable` change small and structural (e.g. one `.table-scroll` wrapper class applied in every branch, or a CSS-only selector fix) so the eventual merge is a trivial conflict. Do NOT try to reproduce #73's changes.
- Full route list for the probe (30 routes): /, /create, /create/studio, /registry, /registry?view=register, /registry?view=a2a-demo, /knowledge-bases, /knowledge-bases?view=create, /memory, /memory?view=short-term, /memory?view=long-term, /chat, /observability, /observability?tab=sessions, /observability?tab=traces, /evaluation, /evaluation?view=experiment, /evaluation?view=evaluators, /evaluation?view=datasets, /evaluation?view=online, /skill-lab, /skill-lab?view=eval, /skill-lab?view=train, /governance, /governance?view=policy, /governance?view=decisions, /governance?view=audit, /users, /workspaces, /workspaces?view=create. Wait for network idle + ~800 ms before measuring `document.documentElement.scrollWidth`. The research report's probe (`/home/ubuntu/workspace/agentcore_launchpad/.claude/skills/self-evolution/scripts/ux/ux_narrow.mjs`) shows how to list the outermost offending elements per route — copy and adapt it (change the port to your own vite).
- Desktop must not change: also measure at 1440×900 and confirm `scrollWidth === 1440` everywhere, and eyeball two desktop screenshots (e.g. /create and /registry) against the current look.


Read `CLAUDE.md`, then `docs/architecture.md` sections relevant to this direction, then
the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] Re-run the narrow probe (script text in the research report; Playwright at 390×844, dev stack running) on all 30 routes listed in the report: `scrollWidth === 390` for every route. Save the output as `.claude/self-evolution/runs/SE-004/narrow_probe.txt`.
- [ ] Re-run at 1440×900: `scrollWidth === 1440` on every route (no desktop regression), same file.
- [ ] Screenshots `runs/SE-004/narrow_{create,registry,knowledge_bases,memory_long_term,chat,users}.png` (viewport-only, not full-page) show tables/toolbars scrolling or wrapping inside their panels.
- [ ] Changes are CSS in `frontend/src/theme/app.css` plus a shared wrapper (e.g. in `components/DataTable.tsx`); no page-specific inline widths; no changes under `apps/studio/`.
- [ ] `make verify` passes. No live AWS check required.

- [ ] `make verify` passes in this worktree (backend ruff+pytest, infra ruff+pytest, frontend eslint+tsc+build, i18n parity).
- [ ] New behaviour has a hermetic test in `backend/tests/` or a frontend check the gate runs; live AWS is never required by the gate.
- [ ] `docs/architecture.md` (and `docs/api.md` for new routes; `.zh-CN` twins for user-facing docs) updated when behaviour or contracts changed.

## Boundaries

- **Never** `git push`, open PRs, merge, rebase or force anything. Commit on the current branch with clear conventional messages; leave the tree clean.
- **Never** run `make bootstrap`, teardown scripts, `cdk deploy`, or anything against the production box. Read-only AWS calls are fine. Create AWS resources only if this brief says so, and delete what you create.
- Do not edit `apps/studio/`, `vendor/`, `vendor-src/`, `backend/samples/frontdesk_agent` unless the brief names them (they have their own conventions).
- Do not widen scope. If the requirement turns out to be wrong or already covered, stop and say so in the report instead of building something adjacent.
- Prefer editing platform code over adding new dependencies; if a dependency is unavoidable, pin it and say why.
- Stay within the budget cap the host set; if you are running out, commit what is verified and report what remains.

## Final report (the host reads only this)

End with exactly these sections:

1. **Changed** — files and what changed, one line each.
2. **Verified** — the commands you ran with their pass/fail outcome (paste the `make verify` tail).
3. **Acceptance checks** — the list above, each ✅/❌ with the evidence.
4. **Not done / deviations** — anything left, anything you interpreted differently, and why.
5. **Commits** — `git log --oneline main..HEAD`.
