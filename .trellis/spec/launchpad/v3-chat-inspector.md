# V3 chat inspector — trace + memory rail

## 1. Scope / Trigger
- `/v3/chat` right rail (task `10-26-v3-chat-inspector`): V2 Inspector parity for the
  session trace waterfall and session memory. No API tab (the left rail's `ApiKeysEntry`
  covers keys). Trigger: any change to the rail, `?inspect=`, or the chat page's URL writes.

## 2. Signatures
- `frontend/src/v3/pages/chat/inspect.ts`: `InspectTab = "trace" | "memory"`,
  `inspectTab(raw) → InspectTab | null`, `SPAN_LIMIT = 12`,
  `spanBars(trace, limit?) → { span, left, width }[]` (% of the drawn extent, width ≥ 0.8).
- `frontend/src/v3/pages/chat/Inspector.tsx`: `Inspector({ tab, onTab, onClose, agentId, sessionId, busy, memoryState })`.
- Backend (unchanged): `GET /api/traces/{session}` (`lookback_hours=3`), `GET /api/chat/{agent}/memory?session_id=`.

## 3. Contracts
- `?inspect=trace|memory` = rail open on that tab; absent/invalid = closed (two columns).
- Every `setParams` on the page goes through `nav()` (or keeps `inspect` itself): a write that
  replaces the query must carry `inspect`, or the rail closes on session/agent change.
- Parent keys `<Inspector>` on `agent:session` → a new conversation starts empty.
- The open tab loads on mount, on tab change and when `busy` (turn running or restoring) falls;
  never while busy. Each tab has its own request sequence (a newer read of the same tab wins).
- Memory tab: `memoryState === "off"` → note only, no fetch.
- Links: 在可观测中打开 → `/v3/observability?tab=sessions&view=session&id=`; 在记忆中打开 →
  `/v3/memory?tab=short-term&actor=<actor_id>&session=`.

## 4. Validation & Error Matrix
| Condition | Result |
|---|---|
| No session yet | placeholder (`v2.chat.tracePlaceholder` / `memoryPlaceholder`) |
| Trace read right after a turn | usually `span_count 0` → `v2.chat.traceEmpty` (spans land ~1 min later) |
| Session older than 3 h | `span_count 0` (lookback), same empty hint |
| Read fails | `v3.chat.inspect.failed` notice, previous data kept |
| > 12 spans | first 12 + `v2.chat.traceMore` |

## 5. Good / Base / Bad Cases
- Good: open rail → send → one trace read after the turn → Refresh shows spans; switch session keeps `inspect`, resets data.
- Base: rail closed → no trace/memory reads at all.
- Bad: `setParams({ agent, session })` in a new code path → rail silently closes.

## 6. Tests Required
- `inspect.test.ts`: `inspectTab` accepts only the two tabs; `spanBars` placement, min width, missing timings, limit, null trace.
- Browser (throwaway vite over dev, block non-chat writes): toggle, live turn → trace read count 1, spans after refresh, memory tab, session switch keeps tab, close, ≤980px stacks.

## 7. Wrong vs Correct
### Wrong
```ts
setParams({ agent: agentId, session: sid }, { replace: true }); // drops ?inspect=
```
### Correct
```ts
nav({ agent: agentId, session: sid }); // carries ?inspect= along
```
