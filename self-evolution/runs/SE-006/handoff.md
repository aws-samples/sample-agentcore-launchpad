# Direction SE-006 — Disabled primary actions explain what is missing (shared Btn disabledReason)

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch `evo/se-006-disabled-primary-actions-explain-what-is`, created
from `main`. A host session wrote this brief, will independently re-run the acceptance
checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

Whenever a form's primary action is disabled because input is missing or invalid, the console must say **what is missing**, in place, in both locales. Add an optional `disabledReason?: string` prop to the shared `Btn` (`frontend/src/components/Btn.tsx`): when set and `disabled`, the button gets `title={disabledReason}` and `aria-describedby` pointing at a small mono hint rendered next to it (same visual weight as `.dim` helper text) that reads the reason; when the button is enabled the hint disappears. Apply it to the six forms that today only dim the button: Registry Register (`▲ REGISTER`), Registry Edit (`▲ SAVE`), Knowledge Base Create (`▲ CREATE`), Strands Studio (`▲ Publish`), Online Evaluation Create (`▸ CREATE`), Workspace detail (`RUN BOOTSTRAP`). Each reason is specific ("Name must be 3–64 chars: lowercase letters, digits, hyphens" / "Add a SKILL.md" / "Pick a source: upload files or an S3 bucket" / "No changes to save" / "Add at least one node" / "Choose an agent and an evaluator" / "Bootstrap already ran / workspace is not registered"), derived from the same predicates that compute `disabled`. Do not change *when* the buttons are disabled.

## Repository evidence and extension points

- Form walk 2026-09-04 (`/tmp/ux_forms.mjs`): Register `primary=[{"txt":"▲ REGISTER","dis":true,"title":""}]`, Edit `▲ SAVE` disabled, KB Create `▲ CREATE` disabled, Studio `▲ Publish` disabled, Online Eval `▸ CREATE` disabled, Workspace detail `RUN BOOTSTRAP` disabled — all with empty `title` and no hint text; `required` count 0 on every form. Screenshot `reports/shots/form_registry_view_register.png`.
- `frontend/src/pages/registry/RegisterView.tsx:35-37` — `regValid = /^[a-z][a-z0-9-]{2,63}$/.test(regName) && (regType === "MCP" ? /^https?:\/\/.+/.test(regUrl) : regMd.trim().length > 0)`; `:159` `disabled={busy || !regValid}`.
- `frontend/src/pages/knowledge/CreateView.tsx:33-35` — `nameValid = NAME_RE.test(name.trim())`, `sourceValid = mode === "upload" ? files.length > 0 : bucket.trim().length > 0`, `canSubmit = nameValid && sourceValid && !busy`; `:159` `disabled={!canSubmit}`.
- `frontend/src/pages/registry/EditView.tsx:471` — `disabled={saving || !dirty || (isSkill && mode === "zip" && !!preview && !preview.valid)}`.
- `frontend/src/pages/CreateAgentStudio.tsx` — Publish button state (grep `Publish`, `publishOpen`); `frontend/src/pages/EvaluationOnline.tsx` — the create form's `▸ CREATE` predicate; `frontend/src/pages/workspaces/DetailView.tsx` — `RUN BOOTSTRAP` predicate.
- Existing good patterns to match: `frontend/src/pages/workspaces/CreateView.tsx:121-141` (explanatory `setError(t("workspacesPage.create.missing"))`), `frontend/src/pages/CreateAgent.tsx:2450,2560` (`permHint` → `title=`).
- `frontend/src/components/Btn.tsx` — 16-line wrapper over `ButtonHTMLAttributes`; the hint needs a wrapping element or a sibling, so keep the DOM change minimal (e.g. render `<span className="btn-hint">` after the button only when `disabled && disabledReason`).

The load-bearing frontend patterns from `CLAUDE.md` that apply here:

- React + Vite + `react-router-dom`, TypeScript strict; top-level routes in `frontend/src/App.tsx`; sub-surfaces are `?view=` query params, never nested routes.
- Every user-facing string is an i18n key in `frontend/src/locales/{en,zh-CN}/common.json`; `python3 scripts/i18n_check.py` enforces en ↔ zh-CN parity (part of `make verify`).
- `frontend/src/lib/api.ts` is the single typed backend client; shared UI primitives live in `frontend/src/components/` (`Btn`, `Panel`, `ViewHead`, `DataTable`, `Pager`, `Chip`, `Toast`, `ConfirmDialog`) and the house CSS in `frontend/src/theme/app.css`.
- Frontend gate = `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (no unit-test runner); browser evidence comes from Playwright — import it from `/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs` and launch chromium with `args:['--no-sandbox']`. The host's dev stack is on http://localhost:5173 (backend :8000); confirm with `curl -s localhost:5173/ | grep -o '<title>[^<]*'` → `AgentCore Launchpad`. It serves the HOST checkout, not your worktree — to see YOUR changes, run your own throwaway frontend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it with `pkill -f "port 5197"` when done). Never restart the host's :8000 backend or :5173 vite.
- Set `localStorage.i18nextLng` (`en` / `zh-CN`) via `context.addInitScript` to pick a locale in Playwright.

Notes specific to this direction:
- Run EVERY command in the FOREGROUND — never run_in_background, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- Save screenshots and probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-006/` (not the relative path inside your worktree). Nothing under `.claude/` is committed.
- Browser evidence: run a worktree vite pointed at the host backend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it by port with `fuser -k 5197/tcp`, never `pkill -f` with a pattern that appears in your own command line). The worktree's `frontend/node_modules` is a symlink into the host checkout, so @fontsource woffs may 403 — cosmetic only. Toasts render in `.toasts` OUTSIDE `<main>`; include them in any alert query. Never restart the host's :8000 backend or :5173 vite.


Read `CLAUDE.md`, then `docs/architecture.md` sections relevant to this direction, then
the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `Btn` accepts `disabledReason`; when `disabled` and set, the rendered `<button>` has `title` = reason and `aria-describedby` = the id of a visible hint element containing the reason; when not disabled, no hint element is rendered (add a short note to the component's JSDoc).
- [ ] Playwright check (worktree vite on :5197 → host :8000) on `/registry?view=register`, `/registry?view=edit&record=<any existing id from /api/registry/records>`, `/knowledge-bases?view=create`, `/create/studio`, `/evaluation?view=online&oe=new`, `/workspaces?view=detail&ws=lab-use2`: every disabled `.btn.primary` has a non-empty `title` and a visible hint; typing a valid name + URL on Register makes the hint disappear and the button enable. Save the output as `.claude/self-evolution/runs/SE-006/hints.txt` and screenshots `runs/SE-006/{register,kb_create}.png` (absolute host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-006/`).
- [ ] All reasons are i18n keys in en + zh-CN; `python3 scripts/i18n_check.py` passes; no hardcoded English.
- [ ] `make verify` passes. No live AWS check required (reads only).

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
