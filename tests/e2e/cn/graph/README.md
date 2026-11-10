# Lifecycle graphs → V2 console (UI executor) — round 16, r16-o

The lifecycle graphs under `backend/tests/graph/graphs/*.yaml` describe every entity's states
and the actions between them; the hermetic API executor (`backend/tests/graph/test_graph_*.py`,
in `make verify`) walks their prime paths through the routes. This directory is the **UI twin**:
the same edges taken through the console's controls, on a deployed host. It is **not** part of
`make verify` and runs nothing live unless a lead-approved test-host window exists (C8).

## Design

* **Edge → control.** Every graph edge carries a `ui:` block — `{page, view, controls[]}` — or
  `ui: null` when the edge is not a console action (a job finishing, an extractor event, a `/v1`
  call, a ledger emulation). `controls` are `data-testid`s from the r16-m control inventory
  (`frontend/src/v2/__inventory__/controls.json`, N16-obs98): the inventory knows each control's
  selector, kind, roles and *recorded effect* (an `/api` route, a navigation, a dialog), so the
  walk can assert what a click did without a page object per screen.
* **Prime paths, walkable subset.** `edge-controls.mjs` recomputes the prime paths with the
  same definition as `paths.py` (maximal simple edge paths, closing cycles allowed) and walks
  only the paths whose EVERY edge has a known control — a path with an api-only edge in it is
  the API executor's job, not the console's.
* **Oracle per click.** (1) the control is present / absent exactly as the inventory's `roles`
  say; (2) the recorded effect fires within 5 s — for an `api` effect, the response status must
  equal the graph edge's `expect.status` (the contract's answer, never the UI's text); (3) after
  the path, the API executor's ledger view of the entity (status, rows) is read through the
  console API over the tunnel and compared with the graph's end node. Destructive controls stop
  at their Confirm dialog unless the fixture prefix is `x16o-` (the same rule as the crawler).
* **Gating.** `--walk` refuses to start unless `--live` is passed AND `LAUNCHPAD_E2E_BASE_URL` +
  `LAUNCHPAD_E2E_PASSWORD` are set (fetch the password in-process; never write it). Default mode
  is the offline `--check`.

## Running

```bash
# offline (no browser, no network): join every edge's controls with the inventory
node tests/e2e/cn/graph/edge-controls.mjs --check            # markdown report on stdout
node tests/e2e/cn/graph/edge-controls.mjs --check --graphs=agent,chat_session

# live (ONLY inside a lead-approved window on a test host, over the tunnel)
export LAUNCHPAD_E2E_BASE_URL="https://127.0.0.1:18443"
export LAUNCHPAD_E2E_PASSWORD="$(…fetch in-process…)"
node tests/e2e/cn/graph/edge-controls.mjs --walk --live --graphs=runtime_canary --out=/tmp/graph-walk
```

## Offline check — 2026-10-19 (inventory of 60f31e79)

| graph | edges | with controls | api-only | gap edges | prime paths | walkable prime paths |
|---|---|---|---|---|---|---|
| `agent` | 28 | 21 | 7 | 3 | 44 | 13 |
| `architect_proposal` | 26 | 20 | 6 | 0 | 31 | 23 |
| `chat_session` | 22 | 9 | 13 | 0 | 23 | 3 |
| `eval_run` | 21 | 14 | 7 | 2 | 24 | 7 |
| `memory_record` | 14 | 7 | 7 | 0 | 11 | 4 |
| `reference` | 15 | 12 | 3 | 0 | 15 | 12 |
| `runtime_canary` | 24 | 22 | 2 | 0 | 49 | 47 |
| **total** | 150 | 105 | 45 | 5 | 197 | 109 |

**UI gaps (register N16-obs115)** — contract-named transitions no inventory control can take:

| graph | edge | page / view | why |
|---|---|---|---|
| `agent` | `redeploy`, `redeploy_while_deploying`, `redeploy_deleted` | `/v2/agents?view=edit` | the edit form's re-publish submit has no `data-testid` (the view's only inventory control is the `retry` label) — a re-publish cannot be driven by a stable selector (N16-obs99 class) |
| `eval_run` | `stop_queued`, `stop_completed` | `/v2/eval/tasks?view=detail` | the task detail's stop is a label-only control (`t:v2.tasks.stop`, no `data-testid`); the 409 `run.not_active` refusal on a finished run is therefore unreachable by selector |

Walkable means "a control exists for every edge"; 109 of 197 prime paths. The rest are the API
executor's: 45 edges are not console actions by design (`ui: null`), 5 are the gaps above.

## Loops this feeds (testing.md §6.7)

* a new control in the inventory → re-run `--check`; a gap row closes when its edge gains a
  control (add the `data-testid` to the YAML `ui.controls`);
* a new register row touching an edge → the API executor's edge-pair walk already covers the
  neighbourhood; the UI walk takes the same prime paths when a window opens.
