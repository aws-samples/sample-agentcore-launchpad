# Direction SE-003 — Stale "Phase" copy and unlabeled form controls

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch `evo/se-003-stale-phase-copy-and-unlabeled-form-cont`, created
from `main`. A host session wrote this brief, will independently re-run the acceptance
checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

Two small, user-visible polish items, both locales:

1. **Stale "phase" copy.** `chatPage.tracePlaceholder` ("END-TO-END TRACE RAIL ARRIVES WITH OBSERVABILITY (PHASE 9)") is shown in the Chat trace rail before a session exists, although Observability and the trace rail (`loadTrace`) shipped. Replace it with copy that describes the current behaviour (e.g. "Send a message — the trace rail loads spans for this session"). Also fix `onSuccessV` ("auto-register → Registry (phase 7)") to drop the phase reference. Do **not** touch the deliberate `nav.phase02` / `footer.phase` / `footer.payments` strings (Payments deferral is a recorded product decision) or `agent.method_not_available`.
2. **Accessible names for form controls.** Every visible `<select>`, `<input>`, `<textarea>` in the console must have an accessible name (`aria-label`, `<label htmlFor>`, `aria-labelledby`, or a placeholder as last resort). Known offenders (Playwright probe, 2026-09-04): Chat agent picker `select[data-testid=agent-select]`; Experiments `exp-agent-select`, `baseline-dataset`; Evaluators model `<select>`, name `<input>`, prompt `<textarea>`; Datasets name/description `<input>`s; Online `online-sampling`, `online-timeout`. Reuse the adjacent visible label text as the `aria-label` (via i18n keys — no hardcoded English).

## Repository evidence and extension points

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

The load-bearing frontend patterns from `CLAUDE.md` that apply here:

- React + Vite + `react-router-dom`, TypeScript strict; top-level routes in `frontend/src/App.tsx`; sub-surfaces are `?view=` query params, never nested routes.
- Every user-facing string is an i18n key in `frontend/src/locales/{en,zh-CN}/common.json`; `python3 scripts/i18n_check.py` enforces en ↔ zh-CN parity (part of `make verify`).
- `frontend/src/lib/api.ts` is the single typed backend client; shared UI primitives live in `frontend/src/components/` (`Btn`, `Panel`, `ViewHead`, `DataTable`, `Pager`, `Chip`, `Toast`, `ConfirmDialog`) and the house CSS in `frontend/src/theme/app.css`.
- Frontend gate = `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (no unit-test runner); browser evidence comes from Playwright — import it from `/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs` and launch chromium with `args:['--no-sandbox']`. The host's dev stack is on http://localhost:5173 (backend :8000); confirm with `curl -s localhost:5173/ | grep -o '<title>[^<]*'` → `AgentCore Launchpad`. It serves the HOST checkout, not your worktree — to see YOUR changes, run your own throwaway frontend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it with `pkill -f "port 5197"` when done). Never restart the host's :8000 backend or :5173 vite.
- Set `localStorage.i18nextLng` (`en` / `zh-CN`) via `context.addInitScript` to pick a locale in Playwright.

Notes specific to this direction:
- Run the probe from your own throwaway frontend (port 5197 → :8000) so it reflects YOUR worktree. The full route list to sweep: /, /create, /create/studio, /registry, /registry?view=register, /registry?view=a2a-demo, /knowledge-bases, /knowledge-bases?view=create, /memory, /memory?view=short-term, /memory?view=long-term, /chat, /observability, /observability?tab=sessions, /observability?tab=traces, /evaluation, /evaluation?view=experiment, /evaluation?view=evaluators, /evaluation?view=datasets, /evaluation?view=online, /skill-lab, /skill-lab?view=eval, /skill-lab?view=train, /governance, /governance?view=policy, /governance?view=decisions, /governance?view=audit, /users, /workspaces, /workspaces?view=create.
- Wait for network idle + ~800 ms before probing; some selects render after data arrives.
- Save the probe output to `.claude/self-evolution/runs/SE-003/a11y_probe.txt` (gitignored — do not commit it).


Read `CLAUDE.md`, then `docs/architecture.md` sections relevant to this direction, then
the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `grep -n "PHASE 9\|phase 7" frontend/src/locales/*/common.json` prints nothing; `nav.phase02`, `footer.phase`, `footer.payments`, `agent.method_not_available` are unchanged (`git diff` shows no hunk on those keys).
- [ ] The probe above returns `[]` on every one of the 30 console routes (save the run output as `.claude/self-evolution/runs/SE-003/a11y_probe.txt`).
- [ ] No hardcoded English: every new `aria-label` goes through `t(...)`; `python3 scripts/i18n_check.py` passes.
- [ ] `make verify` passes. No live AWS check required.

- [ ] `make verify` passes in this worktree (backend ruff+pytest, infra ruff+pytest, frontend eslint+tsc+build, i18n parity).
- [ ] New behaviour has a hermetic test in `backend/tests/` or a frontend check the gate runs; live AWS is never required by the gate.
- [ ] `docs/architecture.md` (and `docs/api.md` for new routes; `.zh-CN` twins for user-facing docs) updated when behaviour or contracts changed.

## Boundaries

- Run every command in the FOREGROUND. Never use run_in_background or wait on a background task — in this non-interactive session your turn ends the moment you stop issuing foreground tool calls, and an unfinished background `make verify` means the run ends with nothing committed.

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
