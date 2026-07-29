# Paginate the Evaluation module tables

Request: add pagination to the Evaluation page's tables. Confirmed scope: **all five
tables in the Evaluation module**, and `/api/eval/runs` becomes **truly paginated**
(not just a raised cap).

## Current state

| Table | File | Rows today (dev) | Upstream limit |
|---|---|---|---|
| Eval runs | `pages/Evaluation.tsx:811` | 25 | `/api/eval/runs` hard-caps at **50**, no params |
| Datasets | `pages/EvaluationDatasets.tsx:891` | 10 | none — full list |
| Evaluators | `pages/EvaluationEvaluators.tsx:590` | 19 | none — builtins + all custom |
| Experiments | `pages/EvaluationExperiment.tsx:1415` | 12 | none — full list |
| Runtime canaries | `pages/EvaluationRuntimeCanary.tsx:376` | 2 | none — full list |

All five are hand-rolled `<table>`s inside `Panel`s and render every row at once.
`components/DataTable.tsx` has no pagination either.

**A pager already exists**: `pages/observability/Pager.tsx` (prev / `PAGE n / m` / next
/ rows-per-page select, `.pagerbar` CSS, `obs.pager.*` keys), used by the Sessions and
Traces tabs. Reuse it rather than writing a second one.

## Requirements

1. **Promote the pager to a shared component** — move `observability/Pager.tsx` to
   `components/Pager.tsx` (exported from `components/index.ts`) and re-point the two
   observability tabs at it. Its i18n keys move from `obs.pager.*` to a top-level
   `pager.*` block (a shared component reading an observability namespace is wrong);
   en + zh-CN parity maintained.
2. **Every one of the five tables gets a pager in its panel footer**, with the same
   controls and behaviour as the observability tables (including rows-per-page).
   Default page size 20 (`PAGE_SIZES[0]`), so — matching the existing component's rule —
   the bar stays hidden while a single page suffices and today's lab screenshots of
   short lists are unchanged.
3. **URL-selected rows stay reachable.** Datasets, evaluators, experiments and canaries
   select a row through a query param (`?dataset=`, `?evaluator=`, `?exp=`, `?canary=`).
   When the selected row is not on the current page, the table opens on the page that
   contains it instead of stranding the selection.
4. **`/api/eval/runs` gains real pagination**: `?limit=&offset=` plus a `total` in the
   response, defaults preserving today's behaviour (`limit=50, offset=0`) so existing
   callers are unaffected. The runs table drives it server-side, so history past the
   first page is reachable.
5. **Two derived behaviours on the runs page must not silently weaken** when the table
   only holds one page:
   - the insights duplicate guards (`insightsPending` / `insightsAlreadyRan`,
     `Evaluation.tsx:379-404`) scan the run list for an insights run over the same
     session set; a duplicate would cost a real AWS analysis. They must stay
     page-independent — add an optional `mode` filter to the runs endpoint and feed the
     guards from an insights-only query rather than the displayed page.
   - the failed-run toast (`failedSeen`, `Evaluation.tsx:216-235`) scans polled runs.
     Runs are newest-first, so page 1 covers freshly failed runs; while the operator
     browses an older page a failure toast may be delayed until they return to page 1.
     That is acceptable (the status column still shows it) — but it must be *delayed,
     never lost*: the seen-set must not mark unseen runs.
6. The other four tables paginate client-side (their endpoints return complete lists);
   no backend change for them.

## Non-goals

- Sorting, filtering or search on these tables.
- Pagination for tables outside the Evaluation module (Registry, Overview, Governance …).
- Server-side pagination for datasets/evaluators/experiments/canaries.

## Acceptance Criteria

- [ ] `components/Pager.tsx` exists, is exported from `components/index.ts`, and the two
      observability tabs use it with unchanged behaviour; no `obs.pager.*` key remains.
- [ ] All five Evaluation tables show the pager when rows exceed the page size, and hide
      it when they do not; changing page or page size re-renders the table only (no lost
      selection, no full-page reload).
- [ ] With `?exp=<id>` (or dataset/evaluator/canary) pointing at a row beyond page 1,
      the table opens on that row's page and the row is visible.
- [ ] `GET /api/eval/runs?limit=5&offset=5` returns the second five runs newest-first
      with `total` = all runs; no params behaves exactly as before (≤50, offset 0);
      `?mode=insights` filters to insights runs (backend tests).
- [ ] Paging the runs table fetches the requested page (network evidence), and the
      insights button's duplicate guard still fires for a matching insights run that is
      not on the displayed page.
- [ ] Browser evidence for: runs table page 2, a short list with no pager, and a
      deep-linked selection on a later page.
- [ ] `make verify` passes (incl. i18n parity).
