# Direction SE-008 — zh-CN typography: full-width punctuation inside Chinese copy, consistently

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch `evo/se-008-zh-cn-typography-full-width-punctuation-`, created
from `main`. A host session wrote this brief, will independently re-run the acceptance
checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

Chinese copy in `frontend/src/locales/zh-CN/common.json` uses **full-width punctuation consistently** when the punctuation sits inside or directly after Chinese text: `，` `：` `；` `？` `！` `（` `）` instead of `,` `:` `;` `?` `!` `(` `)`. Rules: convert only when the character is adjacent to a CJK character (either side); never touch anything inside `{{…}}` placeholders, backticks, URLs/ARNs/paths, identifiers such as `SKILL.md`, `session.id`, `us-west-2`, or purely Latin/technical fragments; keep `——` (correct Chinese dash) and `·`; leave `en/common.json` untouched. Do it with a script committed under `scripts/` (e.g. `scripts/i18n_zh_punct.py --check|--fix`) so the rule is mechanical and re-runnable, and wire `--check` into `scripts/verify.sh` next to `i18n_check.py` so drift cannot return.

## Repository evidence and extension points

- Scan 2026-09-04 over 2853 zh-CN keys: half-width `,` between CJK 58 keys · `:` after CJK 46 · `(` before CJK 53 · `)` after CJK 49 · `?` 5 · `;` 22 → **351 distinct keys**, while **316 keys already use full-width** `，：（）；？` — the locale mixes both styles today (e.g. `evalPage.runs.failureReason = "失败原因:"` vs full-width elsewhere; `knowledge.source.prefix = "前缀(可选)"`; `skillLab.tasksets.err.idUnsafe = "id 会用作任务工作目录名,必须文件系统安全(不含 '/'、'\\' 或 '..')"`).
- 0 untranslated values, 0 whitespace oddities, 3 identical en==zh values (all product nouns) — translation quality is otherwise good, so this is the remaining copy-register issue.
- `scripts/i18n_check.py` and `scripts/verify.sh` — the existing parity gate to extend.
- The Chinese-writing conventions in the repo's workshop content (`.claude/skills/aws-workshop-content`) and `humanizer-zh` both call for full-width punctuation in prose.

The load-bearing frontend patterns from `CLAUDE.md` that apply here:

- React + Vite + `react-router-dom`, TypeScript strict; top-level routes in `frontend/src/App.tsx`; sub-surfaces are `?view=` query params, never nested routes.
- Every user-facing string is an i18n key in `frontend/src/locales/{en,zh-CN}/common.json`; `python3 scripts/i18n_check.py` enforces en ↔ zh-CN parity (part of `make verify`).
- `frontend/src/lib/api.ts` is the single typed backend client; shared UI primitives live in `frontend/src/components/` (`Btn`, `Panel`, `ViewHead`, `DataTable`, `Pager`, `Chip`, `Toast`, `ConfirmDialog`) and the house CSS in `frontend/src/theme/app.css`.
- Frontend gate = `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (no unit-test runner); browser evidence comes from Playwright — import it from `/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs` and launch chromium with `args:['--no-sandbox']`. The host's dev stack is on http://localhost:5173 (backend :8000); confirm with `curl -s localhost:5173/ | grep -o '<title>[^<]*'` → `AgentCore Launchpad`. It serves the HOST checkout, not your worktree — to see YOUR changes, run your own throwaway frontend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it with `pkill -f "port 5197"` when done). Never restart the host's :8000 backend or :5173 vite.
- Set `localStorage.i18nextLng` (`en` / `zh-CN`) via `context.addInitScript` to pick a locale in Playwright.

Notes specific to this direction:
- Run EVERY command in the FOREGROUND — never run_in_background, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- Save screenshots and probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-008/` (not the relative path inside your worktree). Nothing under `.claude/` is committed.
- Browser evidence: run a worktree vite pointed at the host backend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it by port with `fuser -k 5197/tcp`, never `pkill -f` with a pattern that appears in your own command line). The worktree's `frontend/node_modules` is a symlink into the host checkout, so @fontsource woffs may 403 — cosmetic only. Toasts render in `.toasts` OUTSIDE `<main>`; include them in any alert query. Never restart the host's :8000 backend or :5173 vite.


Read `CLAUDE.md`, then `docs/architecture.md` sections relevant to this direction, then
the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] `python3 scripts/i18n_zh_punct.py --check` exits 0 on the branch and exits 1 (listing keys) when a half-width `,`/`:`/`(`/`)`/`;`/`?` is reintroduced next to CJK text (add a small pytest or shell assertion for the script under `backend/tests/` or `scripts/`, hermetic).
- [ ] `scripts/verify.sh` runs the `--check`; `make verify` passes.
- [ ] `git diff --stat main -- frontend/src/locales` touches only `zh-CN/common.json` (+ the script/gate files); `python3 scripts/i18n_check.py` parity still passes; **no `{{placeholder}}`, URL, ARN, path, or code identifier changed** — prove it with a diff filter in the report (e.g. `git diff main -- frontend/src/locales/zh-CN/common.json | grep '^[-+]' | grep -c '{{'` equal on both sides).
- [ ] Spot check in the console (worktree vite): `/evaluation` runs detail "失败原因：" and `/knowledge-bases?view=create` "前缀（可选）" render full-width; one screenshot under `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-008/`.

- [ ] `make verify` passes in this worktree (backend ruff+pytest, infra ruff+pytest, frontend eslint+tsc+build, i18n parity).
- [ ] New behaviour has a hermetic test in `backend/tests/` or a frontend check the gate runs; live AWS is never required by the gate.
- [ ] `docs/architecture.md` (and `docs/api.md` for new routes; `.zh-CN` twins for user-facing docs) updated when behaviour or contracts changed.

## Boundaries

- `main` (8374ab2) now carries SE-005..SE-007, which added new zh-CN keys (`apiErrors.aws.*`, `*.disabled*`, `staleLink.*`) — your script must handle them like every other key; re-run your scan on the current file rather than trusting the 351/316 counts.
- The zh-CN locale contains code-like fragments that must stay half-width: `{{placeholders}}`, backtick spans, URLs, ARNs, paths, CLI flags, model ids, keys like `session.id`, and any character sequence with no CJK on either side. Convert only where at least one side of the punctuation mark is a CJK character (U+4E00–U+9FFF and CJK punctuation), and never touch the en file.
- User-facing docs have zh-CN twins that move together (`docs/architecture.md` ↔ `docs/architecture.zh-CN.md` both exist — `ls docs/*.zh-CN.md`); if you document the new gate, do it in both. Wiring the check into `scripts/verify.sh` also means `CLAUDE.md`'s command table stays accurate — check whether the i18n row needs a mention.

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
