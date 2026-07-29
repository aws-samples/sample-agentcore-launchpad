# Fix vite /v1 proxy, canary verdict float rounding, nova-2-lite price

Parent: `07-29-workshop-backlog`. Three independent P3 items from the report, batched
because each is a few lines and none carries design risk.

## A · ISSUE-005 — dev server does not proxy `/v1`

`frontend/vite.config.ts:26` proxies only `/api`. `frontend/src/pages/Chat.tsx:591`
builds the "equivalent API call" snippet against `${window.location.origin}/v1/...`, so
in dev the copied curl hits vite on :5173 and returns an empty 404 before the backend
ever sees the API key.

Requirement: add `/v1` to the dev proxy so the snippet works as-is when copied. The
snippet keeps using `window.location.origin` (in prod the console and API share an
origin, which is the behaviour being demonstrated).

## B · ISSUE-014 — canary verdict metrics render unrounded floats

`frontend/src/pages/EvaluationRuntimeCanary.tsx:577-584` interpolates
`metric.control.mean` / `treatment.mean` straight into the string, printing
`C 0.5566666666666668`. The experiment page's score bars render the same values as
`mean?.toFixed(2)` (`EvaluationExperiment.tsx:1095,1105`).

Requirement: round in the canary verdict panel with the same formatting used elsewhere,
sharing one helper rather than duplicating `toFixed` (the canary page already imports
`ABMetric`/`ExperimentInfo` from the experiment page, so a shared exported formatter
follows the existing seam). `null` keeps rendering `—`.

## C · `model_prices` has no entry for `global.amazon.nova-2-lite-v1:0`

Cost shows `≈ —` whenever that model carries the traffic. Root cause is two-fold:

- `app/services/model_prices.py:refresh_map` step 1 only takes an **exact**
  (case-insensitive) match of the telemetry model id against a litellm key. litellm
  prices `amazon.nova-2-lite-v1:0`, `us.…`, `eu.…`, `apac.…` but has **no `global.…`
  key**, so the seen model is skipped and no entry is ever written.
- `app/core/config.py:116` has no fallback short key either, and
  `observability.match_price` needs some price-map key to be a substring of the model
  id.

Requirements:

1. When a telemetry model id has no exact source match, retry with a leading region
   prefix (`global.` / `us.` / `eu.` / `apac.`) stripped, and store the entry under the
   **full telemetry id**. Only as a fallback — an exact `us.*` hit must keep its own
   (premium) numbers rather than the base price.
2. Add a `nova-2-lite` fallback entry to the config defaults so a box that has not run
   the refresher still estimates cost, following the existing hand-entry precedent
   (`sonnet-5`). Numbers come from litellm's base `amazon.nova-2-lite-v1:0`:
   input `0.3`, output `2.5` USD/1M.

## Acceptance Criteria

- [ ] `frontend/vite.config.ts` proxies `/v1` to the same target as `/api`; a curl of
      the chat snippet against :5173 reaches the backend (API-key error, not an empty
      404).
- [ ] Canary verdict metrics print 2-decimal means (`C 0.56 (n=3) · T 0.83 (n=3)`), and
      the formatter is shared with the experiment page, not copy-pasted.
- [ ] `refresh_map` writes an entry for `global.amazon.nova-2-lite-v1:0` when the
      source has only `amazon.nova-2-lite-v1:0`; an exact `us.amazon.nova-2-lite-v1:0`
      hit still yields the `us.` numbers. Covered by a unit test on the pure merge.
- [ ] `match_price("global.amazon.nova-2-lite-v1:0", defaults)` returns the fallback
      entry with a stock config (unit test).
- [ ] `make verify` passes.
