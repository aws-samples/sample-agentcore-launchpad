## Summary

With the backend unreachable, half the console used to describe an **empty account**: the Overview said "DEPLOYED AGENTS 0 · none yet / NO DEPLOYMENTS YET", Registry "NO RECORDS", Knowledge Bases "No knowledge bases yet", Chat "no active agents — deploy one first", Experiments "NO EXPERIMENTS" — while the topbar chip stayed a hard-coded green "ALL SYSTEMS GO". The other half (Memory, Observability, Users, Governance…) already rendered an error with RETRY. This makes the two halves agree.

- **Health chip is bound to `/api/health`.** `useHealth` now probes on mount, every 30 s and on `online`/`focus`, and returns `{ health, status: loading|ok|down, refresh }`. The chip is green only while the last probe answered 2xx; otherwise the same-sized chip shows a red LED and "BACKEND UNREACHABLE" (en + zh-CN). The last good payload is kept so the region/account chips do not blank during a restart.
- **Shared `LoadError` component** (also via `DataTable`'s `error`/`onRetry` props): the one "Failed to load: … · RETRY" block. Adopted by Overview (tiles, launch feed, service health), Registry, Knowledge Bases, Chat agent picker, Evaluation runs, Experiments. Empty copy is rendered only after a 200 with an empty list; rows loaded once stay visible through a later failed poll; Retry re-issues the fetch.
- `lib/api.ts` gains `getJson` / `responseMessage` / `errorMessage` so path-fetching pages get the same localized envelope handling (`apiErrors.network` for a request that never got an HTTP answer).
- Registry: only a 503 `registry.unavailable` keeps the dedicated "no Registry in this account" page; a network failure now renders the load error instead of claiming the account has no Registry.
- `docs/architecture.md` (+ zh-CN) gains "Console failure states (backend unreachable)".

## Verification

- `make verify` PASS on the branch.
- Playwright against a worktree build with a dead API (`LAUNCHPAD_API` → closed port) on `/`, `/registry`, `/knowledge-bases`, `/chat`, `/evaluation`, `/evaluation?view=experiment`: chip `data-status="down"` "BACKEND UNREACHABLE", ≥ 1 `.load-error` with Retry, none of the empty-state copy present; Retry on Registry issues exactly one new `/api/registry/records` request. Same routes against the live backend: chip `ok`, zero error blocks, content unchanged. Chip recovers from down → ok on `focus` without reload.
- No backend or AWS changes.

Self-evolution direction SE-002 (`ux` path).
