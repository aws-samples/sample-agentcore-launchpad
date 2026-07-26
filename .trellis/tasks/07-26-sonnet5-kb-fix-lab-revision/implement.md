# Implementation plan

Order matters: code defaults first, so the re-captured screenshots show them.

## S1 — KB defect fix (backend)

- [ ] `_create_data_source`: front guard returning the existing data-source id
      when one already points at the same S3 location (upload + existing modes).
- [ ] `_start_source_completion(kb_id, source)`: daemon thread, own client,
      polls until ACTIVE (generous budget), then `_create_data_source`; logs and
      swallows failures.
- [ ] `create_kb`: call it on the slow path; keep returning `source_pending`.
- Check: `cd backend && uv run pytest tests/test_knowledge_kb.py -q`

## S2 — KB defect fix (frontend)

- [ ] `DetailView`: warning panel + repair button when ACTIVE with zero sources.
- [ ] Remove the `sessionStorage` replay (`CreateView` write, `DetailView`
      replay, `pendingSourceKey` helper).
- Check: `npx tsc --noEmit && npm run lint`

## S3 — Tests

- [ ] background completion creates the source after the KB turns ACTIVE
- [ ] second attempt is a no-op (no duplicate `create_data_source` call)
- [ ] `create_kb` fast path unchanged

## S4 — Default model → sonnet-5

- [ ] backend: `schemas/agent.py`, `core/config.py` (+ `model_prices` entry),
      `evaluation/routers.py`, codegen guidance docs
- [ ] frontend: `CreateAgent.tsx`, `Evaluation.tsx`, `EvaluationEvaluators.tsx`,
      `studio/lib/models.ts`, `studio/lib/sample-flows/*`
- [ ] grep for stragglers: `grep -rn "claude-sonnet-4-6" backend/app frontend/src`
- Check: `make verify`

## S5 — Re-capture evidence on the local stack

- [ ] confirm local stack up; capture `02-create-config`, `08-evaluator-create`
      from the forms (new default visible)
- [ ] re-publish `lab-fund-assistant` (zip) on sonnet-5 → capture
      `02-deploy-inprogress`, `02-deploy-done`, copy the real job log
- [ ] re-publish `lab-fund-advisor` (harness) on sonnet-5 → capture
      `03-harness-config`, `03-harness-deploy`, copy the real job log
- [ ] one chat turn on the advisor → wait for spans → capture `07-obs-trace`,
      `07-obs-span-drawer`, `07-obs-dashboard`, copy the real waterfall excerpt
- [ ] compress new PNGs to 256 colours like the rest

## S6 — Guide revisions

- [ ] R2 items 1–6 (ch03, ch04, ch05, ch07, ch08, ch09)
- [ ] replace model ids in instructional tables; replace quoted logs/traces with
      the newly captured ones
- [ ] update any number the re-run changed (trace count, tokens, durations)
- [ ] note in `README.md` that the model default is sonnet-5

## S7 — Close out

- [ ] lab integrity script (images referenced/orphans, links, anchors)
- [ ] `make verify`
- [ ] fold the verification report into `docs/issues/` for the KB defect
- [ ] Trellis 3.3 judgment → `notes.md`; report; ask before committing

## Rollback points

- After S1–S4: `git checkout -- backend/app frontend/src`
- After S5: old PNGs recoverable from HEAD (`git checkout -- docs/lab/images`)
