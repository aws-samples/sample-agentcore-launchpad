# Direction SE-005 — AWS ClientErrors on console routes map to 4xx envelopes, never a bare 500 or raw boto text

You are the implementation session for one direction of the AgentCore Launchpad
self-evolution loop. You are in a dedicated git worktree on branch `evo/se-005-aws-clienterrors-on-console-routes-map-t`, created
from `main`. A host session wrote this brief, will independently re-run the acceptance
checks on your branch, and owns push, PR and merge. Work only inside this worktree.

## Requirement

A console request that hits an AWS `ClientError` the platform did not anticipate must come back as a **structured 4xx envelope** the console can translate, not as `Internal Server Error` or a message that starts with `An error occurred (…) when calling the … operation`. Concretely, in `app/core/errors.py`, extend the global `ClientError` handler (today `assume_role_error_handler`, which re-raises everything but assume-role failures) so that:

- `ResourceNotFoundException` → 404, code `aws.not_found`
- `ValidationException` → 400, code `aws.validation`
- `AccessDeniedException` / `UnauthorizedException` → 403, code `aws.access_denied`
- `ThrottlingException` / `TooManyRequestsException` / `ServiceQuotaExceededException` → 429, code `aws.throttled`
- `ConflictException` / `ResourceInUseException` → 409, code `aws.conflict`
- everything else keeps today's behaviour (re-raise → 500 with the traceback in the log).

The envelope `message` is the AWS message with the boto prefix stripped (no `An error occurred (X) when calling the Y operation:`), and `detail` carries `{"aws_error_code": "...", "operation": "..."}`. The console maps the new codes to localized copy (`apiErrors.aws.not_found` etc., en + zh-CN) through the existing `localizedMessage` / `apiErrors.*` path, so Knowledge Base detail and Registry edit for an unknown id read "not found / access denied", and Memory's toast no longer shows raw boto text. Existing service-level mappings (e.g. `kb.not_found`) stay and take precedence because they raise `AppError` before the ClientError reaches the handler.

## Repository evidence and extension points

- `backend/app/core/errors.py:66-96` — `assume_role_error_handler`: "Every other `ClientError` is re-raised, which leaves it exactly where it was before this handler existed: an unhandled 500 with the AWS error in the log." Registered at `:101` via `app.add_exception_handler(ClientError, …)`.
- `backend/app/core/errors.py:37-38` — `NotFoundError(AppError)` 404 and the `envelope(code, message, detail)` shape every other error uses.
- Live reproduction (2026-09-04, read-only): `curl localhost:8000/api/knowledge-bases/does-not-exist` → `Internal Server Error [500]`; `curl localhost:8000/api/registry/records/does-not-exist` → 500. Calling the services directly: `knowledge.get_kb_detail(ctx, "does-not-exist")` raises `ClientError AccessDeniedException` (IAM denies the unknown-id ARN); `registry_console.console_get(ctx, "does-not-exist")` raises `ClientError ValidationException` ("Value at 'recordId' failed to satisfy constraint…").
- `backend/app/services/knowledge.py:75-79` — `_get_kb` maps only `ResourceNotFoundException`; `grep -rn "except ClientError" backend/app/routers` → 1 hit; 13 scattered `ResourceNotFoundException` mappings under `backend/app/services/`.
- Console side: `/memory?view=short-term&actor=does-not-exist` toasts `Memory lookup failed: memory lookup failed: An error occurred (ResourceNotFoundException) when cal…` (502); `/knowledge-bases?view=detail&kb=does-not-exist` shows "COULD NOT LOAD KNOWLEDGE BASE ! HTTP 500"; `/registry?view=edit&record=does-not-exist` shows "Failed to load record: HTTP 500" (`reports/shots/badid_*.png`).
- `frontend/src/lib/api.ts:330-390` — `ApiError`, `localizedMessage(code, fallback)` and the `apiErrors.*` key family (`frontend/src/locales/{en,zh-CN}/common.json` around line 1600+), incl. `apiErrors.network` added by SE-002.
- Hermetic test pattern: `backend/tests/` build a `ClientError` with `botocore.exceptions.ClientError({"Error": {"Code": "ValidationException", "Message": "…"}}, "GetRegistryRecord")` and monkeypatch the service wrapper (see `backend/tests/conftest.py` note: AWS is not stubbed globally on a credentialed box).
- `/v1` (`backend/app/routers/public_api.py`) shares the app's exception handlers — check its tests still pass and note in `docs/api.md` that AWS-side errors now surface as 4xx envelopes.

The load-bearing frontend patterns from `CLAUDE.md` that apply here:

- React + Vite + `react-router-dom`, TypeScript strict; top-level routes in `frontend/src/App.tsx`; sub-surfaces are `?view=` query params, never nested routes.
- Every user-facing string is an i18n key in `frontend/src/locales/{en,zh-CN}/common.json`; `python3 scripts/i18n_check.py` enforces en ↔ zh-CN parity (part of `make verify`).
- `frontend/src/lib/api.ts` is the single typed backend client; shared UI primitives live in `frontend/src/components/` (`Btn`, `Panel`, `ViewHead`, `DataTable`, `Pager`, `Chip`, `Toast`, `ConfirmDialog`) and the house CSS in `frontend/src/theme/app.css`.
- Frontend gate = `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (no unit-test runner); browser evidence comes from Playwright — import it from `/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs` and launch chromium with `args:['--no-sandbox']`. The host's dev stack is on http://localhost:5173 (backend :8000); confirm with `curl -s localhost:5173/ | grep -o '<title>[^<]*'` → `AgentCore Launchpad`. It serves the HOST checkout, not your worktree — to see YOUR changes, run your own throwaway frontend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8000 npx vite --port 5197 --strictPort` (kill it with `pkill -f "port 5197"` when done). Never restart the host's :8000 backend or :5173 vite.
- Set `localStorage.i18nextLng` (`en` / `zh-CN`) via `context.addInitScript` to pick a locale in Playwright.

Notes specific to this direction:
- Run EVERY command in the FOREGROUND — never run_in_background, never wait on a background task. In this non-interactive session your turn ends the moment you stop issuing foreground tool calls; an unfinished background `make verify` means the run ends with nothing committed.
- Save screenshots and probe output to the ABSOLUTE host path `/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-005/` (not the relative path inside your worktree). Nothing under `.claude/` is committed.
- Backend side: the load-bearing rule from CLAUDE.md is that errors go through `app/core/errors.register_error_handlers` and that every boto3 client is built in `app/services/aws_clients.py` — do not add per-route try/except and do not construct clients elsewhere (`tests/test_client_funnel.py` guards this). The existing handler docstring explicitly chose to re-raise unknown ClientErrors; you are replacing that choice for the listed AWS error codes — update the docstring and the architecture doc accordingly.
- For the live check run your OWN backend from the worktree: `cd <worktree>/backend && uv run uvicorn app.main:app --port 8011` (no --reload) in the foreground with a timeout, or better: start it, curl, then kill it by port with `fuser -k 8011/tcp` (do NOT use `pkill -f` with a pattern that appears in your own command line — it kills your shell). Never restart the host's :8000 backend or :5173 vite. Read-only AWS calls only.
- Frontend: a worktree vite pointed at your :8011 backend: `cd <worktree>/frontend && LAUNCHPAD_API=http://localhost:8011 npx vite --port 5197 --strictPort`; the worktree's `frontend/node_modules` is a symlink into the host checkout, so fonts may 403 (cosmetic only for this direction).


Read `CLAUDE.md`, then `docs/architecture.md` sections relevant to this direction, then
the files named above, before editing.

## Acceptance checks (the host re-runs these — make each one pass)

- [ ] New hermetic tests in `backend/tests/test_errors_aws.py` (or the existing errors test module): a route whose service raises `ClientError` with each mapped code returns the mapped status + envelope code + stripped message + `detail.aws_error_code`; an unmapped code (e.g. `InternalServerException`) still results in a 500; an assume-role failure still returns 502 `workspace.assume_role_failed`. `cd backend && uv run pytest -q tests/test_errors_aws.py` passes.
- [ ] `curl -s -w ' [%{http_code}]' localhost:8000/api/registry/records/does-not-exist` → `{"code":"aws.validation",…} [400]` and `…/api/knowledge-bases/does-not-exist` → 403 `aws.access_denied` (or 404 if IAM answers not-found) — **live read-only AWS check against the dev stack; run it from your own throwaway backend on another port** (`cd backend && uv run uvicorn app.main:app --port 8011`), never by restarting the host's :8000.
- [ ] Console: `frontend/src/locales/{en,zh-CN}/common.json` gain `apiErrors.aws.not_found|validation|access_denied|throttled|conflict`; `python3 scripts/i18n_check.py` passes; the KB-detail and Registry-edit error blocks render the localized text (screenshot each under `.claude/self-evolution/runs/SE-005/` from a worktree vite pointed at your :8011 backend).
- [ ] `docs/api.md` error section (+ `.zh-CN`) lists the `aws.*` codes; `docs/architecture.md` error-handling paragraph updated.
- [ ] `make verify` passes.

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
