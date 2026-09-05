# Direction SE-001 — Catch-all route: unknown URLs render a not-found view inside the shell

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch `evo/se-001-catch-all-route-unknown-urls-render-a-no`, created
from `main`. A host session wrote this brief, will independently re-run the acceptance
checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

Any URL the console does not route (for example `/nonexistent-route`, a stale bookmark such as an old sub-route, or a typo) renders a **not-found view inside the normal shell** — sidebar, topbar and footer stay visible — with: a kicker/heading in the house style (`ViewHead`), one sentence saying the page does not exist, the requested path shown in mono, and a primary `Btn` linking back to the Overview (`/`). Both locales. Today the router renders nothing and the user sees only the background grid.

## Repository evidence and extension points

- `frontend/src/App.tsx:26-39` — route table nested under `<Route element={<Shell />}>`; there is no `path="*"` element, so an unmatched path renders an empty `<Outlet />`.
- `.claude/self-evolution/reports/shots/en_desk_nonexistent_route.png` — the whole viewport is the background grid: no shell, no message.
- Playwright survey (`reports/research_2026-09-04.md`, S1) — `/nonexistent-route` is the only route in 31 that renders no `<h1>`.
- `frontend/src/layout/Shell.tsx:33` — `<Outlet />` inside `<main>`; the shell already renders for every matched route, so a `*` route placed **inside** the Shell group keeps the chrome.
- `frontend/src/layout/Topbar.tsx:37-39` — the breadcrumb uses `crumbKey`; see how `Shell.tsx` derives it (`ALL_NAV_ENTRIES` in `layout/nav.ts`) and give the not-found page a sensible crumb (e.g. `nav.notFound`) instead of an empty/undefined one.
- Shared components to use: `frontend/src/components/{ViewHead,Panel,Btn}.tsx`; i18n keys live in `frontend/src/locales/{en,zh-CN}/common.json` (parity enforced by `scripts/i18n_check.py`).

The load-bearing frontend patterns from `CLAUDE.md` that apply here:

- React + Vite + `react-router-dom`, TypeScript strict; top-level routes in `frontend/src/App.tsx`; sub-surfaces are `?view=` query params, never nested routes.
- Every user-facing string is an i18n key in `frontend/src/locales/{en,zh-CN}/common.json`; `python3 scripts/i18n_check.py` enforces en ↔ zh-CN parity (part of `make verify`).
- `frontend/src/lib/api.ts` is the single typed backend client; shared UI primitives live in `frontend/src/components/` (`Btn`, `Panel`, `ViewHead`, `DataTable`, `Pager`, `Chip`, `Toast`, `ConfirmDialog`) and the house CSS in `frontend/src/theme/app.css`.
- Frontend gate = `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (no unit-test runner); browser evidence comes from Playwright — import it from `/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs` and launch chromium with `args:['--no-sandbox']`. The host's dev stack is on http://localhost:5173 (backend :8000); confirm with `curl -s localhost:5173/ | grep -o '<title>[^<]*'` → `AgentCore Launchpad`. It serves the HOST checkout, not your worktree — to see YOUR changes, run your own throwaway frontend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it with `pkill -f "port 5197"` when done). Never restart the host's :8000 backend or :5173 vite.
- Set `localStorage.i18nextLng` (`en` / `zh-CN`) via `context.addInitScript` to pick a locale in Playwright.

Notes specific to this direction: the breadcrumb key is derived in `frontend/src/layout/Shell.tsx` from `ALL_NAV_ENTRIES` (`layout/nav.ts`) — read how an unmatched pathname is handled there and make the not-found page's crumb a real key. Save the two screenshots to `.claude/self-evolution/runs/SE-001/` (that directory is gitignored — do not commit screenshots).


Read `CLAUDE.md`, then `docs/architecture.md` sections relevant to this direction, then
the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `frontend/src/App.tsx` has a `<Route path="*" element={<NotFound />} />` **inside** the `<Shell />` route group (so the sidebar/topbar/footer render).
- [ ] New page `frontend/src/pages/NotFound.tsx` uses `ViewHead` + `Panel` + `Btn`/`Link` to `/`, shows the requested pathname (`useLocation().pathname`) in a mono span, and uses only i18n keys (new keys in both `en` and `zh-CN`, `python3 scripts/i18n_check.py` passes).
- [ ] Screenshot evidence: with the dev stack running (`curl -s localhost:5173/ | grep -o '<title>[^<]*'` must print `AgentCore Launchpad`), a Playwright/agent-browser capture of `http://localhost:5173/nonexistent-route` saved as `.claude/self-evolution/runs/SE-001/notfound_en.png` and `notfound_zh.png` showing the shell plus the not-found panel. (Load `localStorage.i18nextLng` = `zh-CN` for the second one.)
- [ ] The topbar breadcrumb on that page is not blank/`undefined` (visible in the screenshot).
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
