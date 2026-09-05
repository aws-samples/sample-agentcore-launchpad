# Direction SE-007 — Stale deep links tell the user the resource is gone instead of silently falling back

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch `evo/se-007-stale-deep-links-tell-the-user-the-resou`, created
from `main`. A host session wrote this brief, will independently re-run the acceptance
checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

A deep link whose id no longer resolves must tell the user, not silently fall back. For these params — Evaluation `?view=datasets&ds=`, `?view=evaluators&ev=`, `?view=online&oe=`, `?view=experiment&exp=`, Chat `?agent=` (and its companion `?session=`), Knowledge Bases `?view=detail&kb=` (missing or unknown) — when the list has loaded and the id is not in it (or the detail fetch answers 404/400/403), render one shared, dismissible notice at the top of the page ("<Thing> `<id>` no longer exists in this workspace — pick one from the table below.") and remove the stale param from the URL (`setSearchParams(..., { replace: true })`). Chat additionally must not silently switch to another agent: keep the picker on the placeholder option until the user chooses. Reuse the wording/pattern Skill Lab already uses. Both locales.

## Repository evidence and extension points

- Probe 2026-09-04 (`/tmp/ux_badid.mjs`, `/tmp/ux_badid2.mjs`): `/evaluation?view=datasets&ds=does-not-exist`, `?view=evaluators&ev=does-not-exist`, `?view=online&oe=does-not-exist`, `?view=experiment&exp=does-not-exist` → URL unchanged, list rendered, no notice; `/chat?agent=does-not-exist` → first active agent selected, no notice; `/knowledge-bases?view=detail` (no `kb`) → "LOADING ▸" forever; `/knowledge-bases?view=detail&kb=does-not-exist` → error block (becomes a proper 4xx after SE-005).
- Good patterns to copy: `/skill-lab?view=eval&job=does-not-exist` → "That job no longer exists in this workspace — pick one from the table above." (`skillLab.*.gone` keys, `frontend/src/locales/en/common.json:1291,1495`); `/workspaces?view=detail&ws=does-not-exist` → "Workspace not found" page (`workspacesPage.*.gone`, `:3419`); `/observability?tab=sessions&session=…` → "SESSION NOT FOUND — …" (`:2662`).
- Param readers: `frontend/src/pages/EvaluationDatasets.tsx:335` (`dsParam`), `EvaluationEvaluators.tsx:200` (`evParam`), `EvaluationOnline.tsx:399` (`oeParam`), `EvaluationExperiment.tsx:293,391` (`exp`), `Chat.tsx:98-136` (`linkedAgent`, fallback `setAgentId(active[0].id)` with the comment "Linked agent unknown/inactive: drop the linked session too"), `KnowledgeBases.tsx:123` (`kbId={searchParams.get("kb") ?? ""}`) + `knowledge/DetailView.tsx`.
- Shared components: `frontend/src/components/LoadError.tsx` (SE-002) is for failed loads — this is a different state (loaded, id absent); add a small `StaleLink` / `GoneNotice` component next to it, or generalise Skill Lab's block.

The load-bearing frontend patterns from `CLAUDE.md` that apply here:

- React + Vite + `react-router-dom`, TypeScript strict; top-level routes in `frontend/src/App.tsx`; sub-surfaces are `?view=` query params, never nested routes.
- Every user-facing string is an i18n key in `frontend/src/locales/{en,zh-CN}/common.json`; `python3 scripts/i18n_check.py` enforces en ↔ zh-CN parity (part of `make verify`).
- `frontend/src/lib/api.ts` is the single typed backend client; shared UI primitives live in `frontend/src/components/` (`Btn`, `Panel`, `ViewHead`, `DataTable`, `Pager`, `Chip`, `Toast`, `ConfirmDialog`) and the house CSS in `frontend/src/theme/app.css`.
- Frontend gate = `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (no unit-test runner); browser evidence comes from Playwright — import it from `/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs` and launch chromium with `args:['--no-sandbox']`. The host's dev stack is on http://localhost:5173 (backend :8000); confirm with `curl -s localhost:5173/ | grep -o '<title>[^<]*'` → `AgentCore Launchpad`. It serves the HOST checkout, not your worktree — to see YOUR changes, run your own throwaway frontend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it with `pkill -f "port 5197"` when done). Never restart the host's :8000 backend or :5173 vite.
- Set `localStorage.i18nextLng` (`en` / `zh-CN`) via `context.addInitScript` to pick a locale in Playwright.

Notes specific to this direction:
- Run EVERY command in the FOREGROUND — never run_in_background, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- Save screenshots and probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-007/` (not the relative path inside your worktree). Nothing under `.claude/` is committed.
- Browser evidence: run a worktree vite pointed at the host backend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it by port with `fuser -k 5197/tcp`, never `pkill -f` with a pattern that appears in your own command line). The worktree's `frontend/node_modules` is a symlink into the host checkout, so @fontsource woffs may 403 — cosmetic only. Toasts render in `.toasts` OUTSIDE `<main>`; include them in any alert query. Never restart the host's :8000 backend or :5173 vite.


Read `CLAUDE.md`, then `docs/architecture.md` sections relevant to this direction, then
the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] One shared notice component under `frontend/src/components/` used by the six surfaces; text via i18n keys (en + zh-CN), interpolating the kind and id.
- [ ] Playwright (worktree vite :5197 → host :8000): each of the six URLs with `does-not-exist` shows the notice, the stale param is gone from `location.search` after render, and — for Chat — `select[data-testid=agent-select]` value is `""` (placeholder), not another agent's id. `/knowledge-bases?view=detail` without `kb` shows the notice instead of a permanent "LOADING". Save output as `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-007/stale_links.txt` + one screenshot per surface in that directory.
- [ ] A valid deep link (an existing `ds`/`ev`/`oe`/`exp`/`agent` id taken from the corresponding `/api/...` list) still selects the row / agent exactly as before (assert in the same script — no regression).
- [ ] `make verify` passes; `python3 scripts/i18n_check.py` passes. No live AWS check beyond the running dev stack's reads.

- [ ] `make verify` passes in this worktree (backend ruff+pytest, infra ruff+pytest, frontend eslint+tsc+build, i18n parity).
- [ ] New behaviour has a hermetic test in `backend/tests/` or a frontend check the gate runs; live AWS is never required by the gate.
- [ ] `docs/architecture.md` (and `docs/api.md` for new routes; `.zh-CN` twins for user-facing docs) updated when behaviour or contracts changed.

## Boundaries

- `main` now includes SE-005 (#76): unknown ids on `/api/knowledge-bases/{id}` and `/api/registry/records/{id}` answer 4xx envelopes with codes `aws.access_denied` / `aws.validation` / `aws.not_found` (console copy under `apiErrors.aws.*`). Treat any 4xx on the detail fetch as "gone" for this direction. `main` also includes SE-006 (#77): `Btn` has a `disabledReason` prop.
- User-facing docs have zh-CN twins that MUST move together: `docs/architecture.md` ↔ `docs/architecture.zh-CN.md` (both exist — `ls docs/*.zh-CN.md` before claiming otherwise). Match the surrounding register of each file.

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
