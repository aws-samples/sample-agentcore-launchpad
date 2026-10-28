# Frontend ↔ Server Contracts: Ledger Timestamps and Whole-Set Permission Maps

> Two server shapes the console must read/write a specific way. Introduced by task
> `10-13-v2-bugfixes-from-v3` (ported from `origin/console-v3`; helpers kept
> byte-compatible with that branch so merges stay clean).

## 1. Scope / Trigger

- Trigger: any frontend code that parses a server timestamp string, or that PATCHes
  `/api/users/{id}` with `permissions`.
- In scope: V2 (`frontend/src/v2/`), classic pages, `frontend/src/lib/`.

## 2. Ledger timestamps are naive UTC

**Fact.** SQLite drops the zone even for `DateTime(timezone=True)` columns, and most
routers serialize with `row.created_at.isoformat()`, so ledger times arrive as
`"2026-10-08T10:01:13.754719"` (no offset). AWS-sourced times and a few services
(`services/announcements.py` `_iso`, observability `_ns_to_iso`) do carry `+00:00`.
`new Date(naive)` reads a naive string as **local** time → every ledger time shifts by
the browser's UTC offset (8 h in UTC+8), range filters cut the wrong window, and
"elapsed since start" clamps to 0.

**Rule.** Parse every server timestamp string with `parseTimestamp`
(`frontend/src/lib/timestamps.ts`): naive ISO with a `T` → `Z` appended; zoned and
date-only strings unchanged; invalid/empty → `null`. Display through `fmtTime`
(`v2/format.ts`), which already uses it.

```ts
// Wrong
new Date(task.createdAt).getTime() >= since
// Right
(parseTimestamp(task.createdAt)?.getTime() ?? 0) >= since
```

Exempt: epoch numbers (`new Date(ms)`), and `new Date()` for "now".

## 3. `permissions` PATCH replaces the whole set

**Fact.** `services/users.py` `apply_patch` → `_normalized_permissions` stores the sent
map as the account's entire override set (only explicit `false` kept; an unsent key
counts as granted; unknown keys → `users.invalid_permissions`). A one-key body
therefore re-grants every other denied key.

**Rule.** Build the body with `togglePermission(user.permissions, key, granted)`
(`frontend/src/lib/api.ts`), which sends all `AGENT_PERMISSIONS` keys with one changed.

```ts
// Wrong — wipes earlier denials
{ permissions: { [key]: !granted } }
// Right
{ permissions: togglePermission(user.permissions, key, !granted) }
```

## 4. Tests

- `lib/timestamps.test.ts` — naive → UTC; `Z` / `+00:00` / `+08:00` kept; null/garbage → null.
- `lib/skillLab.test.ts`, `v2/format.test.ts` — naive inputs with `process.env.TZ` pinned to
  `Asia/Shanghai` (restored in `afterAll`). **Pin the zone in any timestamp test:** on a
  UTC host a naive string parses the same either way, so the bug passes unseen. Vitest
  runs each file in its own forked process, so the pin cannot leak.
- `lib/permissions.test.ts` — two successive toggles keep the first denial; granting one
  chip keeps other denials; a missing key counts as granted.

## 5. Not done (follow-up)

The backend still emits naive strings, so classic pages and `/v1` consumers that use
`new Date()` directly are still exposed. Fixing it at the source means changing about 34
`.isoformat()` serializers to attach UTC.

## 6. Agent-DLC criteria editor and golden-run wizard (task `10-28-criteria-editor-fixes`)

Found while recording the 判据 demo on prod2 (2026-10-27); each was a silent mismatch
between what the console sent or showed and what the server does.

- **Executor kinds are `evaluator | metric | human`.** `app/dlc/criteria.validate`
  refuses anything else (`criteria.bad_executor`). 「人工」 is `human`; the client type is
  `ExecutorKind` in `frontend/src/lib/dlc.ts`. `normalize_criterion` still maps a legacy
  `manual` to `human` for old cached bundles — do not add new aliases there.
- **Never key an editable list by an editable field.** The editors keyed rows by
  `` `${row.key}:${index}` ``; typing the 编号 remounted the row on every keystroke and
  dropped the caret. Keep `EditorRow { uid, row }` (`editorRows()`) and key by `uid`.
- **A refusal carries its reasons.** Save and publish answer `422 criteria.invalid` with
  `detail.findings`; `refusedFindings(error)` reads them, and the editor lists them (with
  the 「没有保存或发布」 note) instead of the last saved version's findings until the next
  successful save.
- **A golden split's run is scored against its lineage's newest published version**
  (`evaluation/routers._golden_lineage`). `GET /api/eval/datasets` carries `role`,
  `split`, `criteria_lineage_id`; both task wizards call `goldenRunCriteria()`
  (`frontend/src/lib/goldenRun.ts`) when such a split is chosen and preselect the
  evaluators that version names. A copied run (`?from=run:`) or an `?evaluators=`
  hand-off keeps its own selection.

Tests: `backend/tests/test_dlc_criteria.py::test_a_human_judged_criterion_is_valid_and_manual_is_its_old_name`,
`backend/tests/test_dlc_api.py::test_the_dataset_list_names_a_golden_split_and_its_criteria`,
`frontend/src/lib/criteriaEditor.test.ts`.
