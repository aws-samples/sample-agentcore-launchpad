# Direction SE-002 — Unreachable backend: health chip reflects reality and list pages show load errors, not empty states

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch `evo/se-002-unreachable-backend-health-chip-reflects`, created
from `main`. A host session wrote this brief, will independently re-run the acceptance
checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

When the backend is unreachable or a list endpoint fails, the console must say so instead of pretending the account is empty:

1. **Health chip.** The topbar chip currently renders a constant green "ALL SYSTEMS GO". It must reflect `/api/health`: green + `topbar.allSystemsGo` when the last probe succeeded; red/`crit` LED + a "BACKEND UNREACHABLE" (i18n) label when the probe failed or never answered; and it must re-probe (e.g. every 30 s, and immediately on `window` `online`/focus) so it recovers without a reload. Keep the chip's markup/size stable so the topbar layout does not shift.
2. **List pages.** Overview (deployed-agents tile + launch feed), Registry (records table), Knowledge Bases (KB table), Chat (agent picker header) and the Evaluation module's Runs and Experiments tables must distinguish *loaded and empty* from *failed to load*. On failure render a shared error state with the localized message and a **Retry** button (same look as the existing Observability/Governance "… failed · RETRY" blocks), never the "create your first …" empty copy. The successful/empty behaviour is unchanged.
3. Do this through **one shared component** (e.g. `components/LoadError.tsx`, or an `error`/`onRetry` prop on `DataTable`) so the ~8 sites do not re-invent it; pages that already show an error (Memory, Observability, Users, Workspaces, Governance, Skill Lab, Datasets, Evaluators) may adopt it but must not regress.

## Repository evidence and extension points

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

The load-bearing frontend patterns from `CLAUDE.md` that apply here:

- React + Vite + `react-router-dom`, TypeScript strict; top-level routes in `frontend/src/App.tsx`; sub-surfaces are `?view=` query params, never nested routes.
- Every user-facing string is an i18n key in `frontend/src/locales/{en,zh-CN}/common.json`; `python3 scripts/i18n_check.py` enforces en ↔ zh-CN parity (part of `make verify`).
- `frontend/src/lib/api.ts` is the single typed backend client; shared UI primitives live in `frontend/src/components/` (`Btn`, `Panel`, `ViewHead`, `DataTable`, `Pager`, `Chip`, `Toast`, `ConfirmDialog`) and the house CSS in `frontend/src/theme/app.css`.
- Frontend gate = `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (no unit-test runner); browser evidence comes from Playwright — import it from `/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs` and launch chromium with `args:['--no-sandbox']`. The host's dev stack is on http://localhost:5173 (backend :8000); confirm with `curl -s localhost:5173/ | grep -o '<title>[^<]*'` → `AgentCore Launchpad`. It serves the HOST checkout, not your worktree — to see YOUR changes, run your own throwaway frontend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it with `pkill -f "port 5197"` when done). Never restart the host's :8000 backend or :5173 vite.
- Set `localStorage.i18nextLng` (`en` / `zh-CN`) via `context.addInitScript` to pick a locale in Playwright.

Notes specific to this direction:
- For the dead-backend screenshots run a SECOND throwaway frontend from your worktree pointed at a port nothing listens on: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8999 npx vite --port 5198 --strictPort` (every `/api` call then fails through the vite proxy). Use port 5197 (→ :8000) for the live-backend screenshot. Kill both when done (`pkill -f 'port 5197'; pkill -f 'port 5198'`).
- Keep the successful path byte-for-byte: when the API answers 200 with an empty list, the existing empty copy must still render.
- Save screenshots to `.claude/self-evolution/runs/SE-002/` (gitignored — do not commit them).
- `docs/architecture.md` has a console/frontend section; add one short paragraph recording the new rule (failed list loads render the shared error state, health chip states) so the decision is discoverable.


Read `CLAUDE.md`, then `docs/architecture.md` sections relevant to this direction, then
the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] New shared component (`frontend/src/components/LoadError.tsx` or equivalent, exported from `components/index.ts`) with message + Retry, used by Overview, Registry, Knowledge Bases, Chat, Evaluation runs, Evaluation experiments.
- [ ] `useHealth` returns `{ health, status: "loading" | "ok" | "down", refresh }` (or equivalent) and re-probes periodically; `Topbar` renders `topbar.allSystemsGo` with the green LED only when `status === "ok"`, otherwise a `crit`-toned LED + new key `topbar.backendDown` (en + zh-CN).
- [ ] Dead-backend screenshots saved under `.claude/self-evolution/runs/SE-002/`: `dead_overview.png`, `dead_registry.png`, `dead_knowledge_bases.png`, `dead_chat.png`, `dead_evaluation.png`, `dead_experiments.png` — each shows the red chip and the error+Retry block, and **none** shows "none yet / NO RECORDS / No knowledge bases yet / no active agents / NO EXPERIMENTS" copy.
- [ ] Live-backend screenshot `runs/SE-002/live_overview.png` shows the green chip and the normal Overview (no regression when the API is healthy).
- [ ] Clicking Retry re-issues the fetch (verify by watching the network tab or a second screenshot after bringing the backend back).
- [ ] `python3 scripts/i18n_check.py` passes; `make verify` passes. No live AWS check required.

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
