# V2 Attention Panels and the Signal Vocabulary

> A page tells the user what needs them as a ranked list, and every status colour means
> the same thing on every page. Introduced by task `10-13-v2-attention-signals` (#268),
> ported from `origin/console-v3` V3, and extended by `10-13-v2-attention-modules` to eight
> pages: Observability, Governance, Registry, Online, Experiments + canary, Tasks, Skill Lab
> and Workspaces. `10-16-v2-attention-dismiss` added per-row dismiss (every panel), moved
> capping into the panel, and made the eval panels' failed runs expire. Since
> `10-22-eval-online-merge` the Online panel renders on the 评估任务 持续评估 tab (still over
> **all** configs, scope `online`) and the Tasks panel on the 一次性评估 tab lists failed runs
> only — one panel per tab, scopes unchanged.

## 1. Scope / Trigger

- Trigger: a V2 page gets a "needs a look" queue, or a page maps its statuses to colours.
- In scope: `frontend/src/v2/`. Classic pages are out.

## 2. Signatures

```ts
// v2/signals.ts — the only Signal → colour mapping
export type Signal = "ok" | "wait" | "act" | "info" | "off";
export const SIGNAL_TONE: Record<Signal, TagTone>;   // ok green · wait orange · act red · info blue · off gray
export function worstSignal(signals: Signal[]): Signal | undefined; // act > wait > info > ok > off
export function rankBySignal<T extends { signal: Signal }>(items: T[]): T[]; // stable, most severe first

// v2/ui.tsx
export function Lamp(p: { signal: Signal; live?: boolean; title?: string });
export interface AttentionItem {
  key: string; signal: Signal; live?: boolean; title: ReactNode; hint?: ReactNode;
  meta?: ReactNode; onOpen?: () => void; openLabel?: string; actions?: ReactNode; testId?: string;
}
export function AttentionPanel(p: {
  title: ReactNode; items: AttentionItem[]; empty: ReactNode;
  cap?: number;           // rows shown at most, applied after dismissed rows are hidden
  dismissScope?: string;  // turns on per-row dismiss, stored per browser under this scope
  testId?: string;
});

// v2/attentionDismiss.ts — pure; storage is a parameter (defaults to localStorage)
export const DISMISS_STORAGE_KEY = "launchpad_v2_attention_dismissed";
export const DISMISS_TTL_MS;                                   // 30 days
export function dismissKey(item: { key; signal }): string;    // `${key}|${signal}`
export function visibleAttention<T>(items: T[], dismissed: ReadonlySet<string>, cap?: number):
  { shown: T[]; dismissed: T[]; remaining: number };          // filter first, then cap
export function loadScope(scope, storage?, now?): Record<string, string>;
export function saveScope(scope, entries, storage?, now?): void;

// v2/hooks.ts
export function useAttentionDismissals(scope: string | undefined):
  { dismissed: ReadonlySet<string>; dismiss(key: string): void; restore(key: string): void };
```

## 3. Contracts

- **What each signal means.** Pick the meaning first; the colour follows from it.
  - `ok`: running as intended.
  - `wait`: settling, or waiting on someone.
  - `act`: needs the user.
  - `info`: moving on its own.
  - `off`: inactive.

  An in-progress state is `wait` when it can stall and the user may need to step in (a
  deploy, settling). It is `info` when it finishes by itself.
- **Use `SIGNAL_TONE` for colours.** Don't add a new page-local `*_TONE` map for a status
  that has a signal; map the status to a `Signal` and colour it through `SIGNAL_TONE`.
  The roughly 40 existing maps are **not** migrated yet. Migrating recolours existing tags
  (queued gray → orange, some running states orange → blue), so it is a separate task with
  its own UX sign-off, never a side effect of adding a panel.
- **Ranking is the page's job.** Keep the logic that decides what needs a look as a pure
  function over data the page already loads, with no fetch inside it, so it can be
  unit-tested in vitest's node environment. Observability's version is
  `v2/pages/observability/attention.ts` `findings()`. `AttentionPanel` renders `items`
  in the order given. The card edge colour (`data-signal`) is `worstSignal` of the rows
  it actually shows (after dismiss and cap).
- **Module shape (copy this).** `pages/<module>/attention.ts` exports a pure function over
  the rows the page already loads. It returns entries `{ key, kind, signal, <id>, values? }`
  ranked with `rankBySignal`, with no React, i18n or fetch, and has an `attention.test.ts`.
  The list page maps entries to `AttentionItem`: copy from `v2.<module>.attention.*`, and
  `onOpen` = the module's existing detail navigation.
- **Cap long queues in the panel.** The module returns its **full** ranked list and the
  page passes the module's cap constant as `cap`: Tasks `TASK_ATTENTION_CAP` 5, 按 Agent
  `SCORECARD_ATTENTION_CAP` 5, Skill Lab `JOB_ATTENTION_CAP` 6, Registry `APPROVAL_CAP` 8.
  A queue that grows with real data (41 pending records on dev) must not push the list off
  screen. Never `slice` in the module: dismissing a shown row must promote the next one,
  so the cap has to apply after the dismiss filter. The header count is the number of rows
  not dismissed (the old pre-capped `count` prop is gone).
- **Dismiss (every panel opts in).** Each page passes its own `dismissScope`: `eval.agents`,
  `eval.tasks`, `skilllab`, `registry`, `experiments`, `canary`, `online`, `governance`,
  `observability`, `workspaces`. A new panel picks a new scope.
  - The stored identity is `dismissKey(item)` = `${item.key}|${item.signal}`. A row whose
    signal changes (wait → act, or it recovers) is a new identity and shows again, and so
    does a new failure or regression (new key). So item keys must stay stable for the same
    finding across reloads, and must not embed volatile values (counts, times).
    Observability's findings are keyed per kind (`errorRate`, `errorSpike`,
    `toolFailing:<tool>`), so a dismissal there covers that kind of finding, including a
    later spike in another bucket, until its signal changes or 30 days pass.
  - Storage is one localStorage entry `launchpad_v2_attention_dismissed` →
    `{ [scope]: { [dismissKey]: dismissedAtISO } }`. Entries older than 30 days are pruned
    from every scope on each write and ignored on read. Every read and write is wrapped in
    try/catch: a missing, throwing or garbled store means nothing is dismissed. A failed
    write keeps the dismissal in React state until the panel unmounts (a reload, or
    leaving the page).
  - The ✕ (`aria-label` `v2.attention.dismiss`) is a sibling of the row's open button,
    after any `actions`. When something is dismissed the header shows a
    `v2.attention.dismissedCount` toggle that lists those rows muted, each with Restore
    (no inline `actions` there). When every row is dismissed the panel shows `empty` plus
    the toggle. Stored keys whose rows are gone are not counted.
  - The pressed ✕ / Restore unmounts with its row, so focus moves to the same control of
    the row now in that slot, else the last one, else the header toggle (or the first ✕
    once the last row is restored). Keyboard users are never dropped to `<body>`.
  - It is per browser only; there is no server state and no cross-device sync.
- **Eval failure expiry** (`pages/tasks/scorecardAttention.ts`, `pages/tasks/attention.ts`
  `failedTasks`, both with an injectable `now`). A failed **run** is listed only while both
  hold:
  - it was created within `FAILED_RUN_TTL_MS` (7 days; parsed with `parseTimestamp`, and a
    run whose time cannot be parsed is kept);
  - no **completed** run of the same agent on the same benchmark was created after it. On
    按 Agent that is `agent_key` + `benchmark.key`, where every passive scope shares the one
    `live` benchmark. On 全部任务 it is mode + agent (a log source by its service name) +
    `${source}:${dataset_id}`, or the source kind (window / sessions / logs) for a run with
    no dataset. A later failed or stopped run supersedes nothing.

  Failed online configs are live state and never expire. Regression rows are untouched: they
  clear once a newer version stops regressing. Both panels' subtitles say so
  (`v2.scorecard.attention.sub`, `v2.tasks.attention.sub`).
- **No extra fan-out.** Feed the panel from data the page already has, or from one
  existing call. Skill Lab uses a single `GET /api/skill-lab/jobs` for every type instead
  of one call per type. Per-item calls, like V3's per-gateway decisions bar, are out.
- **Rows.**
  - A row with `onOpen` is a `<button>`.
  - `actions` (inline approve/reject, resume, …) render outside that button. Never put
    a button inside the row button.
  - `live` animates a breathing ring, but only on `wait` lamps, and never under
    `prefers-reduced-motion`.
- **Empty state.** The panel renders once its data has loaded. It is hidden while loading
  and on a load error, which the page already shows. `empty` is one line saying why nothing
  stands out: an `ok` lamp with `attention.empty` when rows exist, or an `off` lamp with
  the module's existing empty key (or its own neutral line) when the list itself is empty.
  Never reuse a table's call-to-action empty text, such as the all-caps "create one ▸".
- **Deep links.** A row's open action should land on the list already filtered. The
  filter lives in the URL: Observability's traces tab reads and writes `?status=` through
  `asTraceStatus`. When a page gets a new deep link, it moves that filter to the URL
  with a functional `setParams(prev => …, { replace: true })`, so the other params are
  kept and no history entry is pushed.
- **Copy.** Keys go under `v2.<module>.attention.*`, in en and zh-CN. Use the module's
  existing zh terminology ("Agent", "链路", "根 Span"). The kit's own strings (忽略, 恢复,
  已忽略 N, the per-browser hint) are under `v2.attention.*`.

## 4. Gotchas

- A reduced-motion override must use a selector at least as specific as the rule it
  cancels. A media query adds no specificity, so `.v2-lamp::after` lost to
  `.v2-lamp[data-live][data-signal]::after` and the ring kept animating; review caught it.
- Observability buckets arrive as `"2026-10-12 08:00:00.000"`. A stub that uses ISO
  strings renders raw, because `fmtBucket` returns the input unchanged when it can't
  parse it. Stubs should use the real shape.
- `signals.ts` ↔ `ui.tsx` is a type-only cycle (`import type { TagTone }`). Keep it type-only.
- At ≤ 720 px the inline `actions` take their own line. A `dismissable` row overrides the
  main part's `flex-basis: 100%` with `flex: 1 1 0%` and gives the actions `order: 1;
  flex-basis: 100%`, so the ✕ / Restore stays beside the text instead of adding a line.
- `.v2-link` comes later in `v2.css` and resets padding / font-size, so the Restore rule is
  `.v2-attention-row > .v2-attention-restore` to win on specificity.

## 5. Tests

- `v2/signals.test.ts`: the tone map is total and distinct, plus the severity order.
- `v2/pages/observability/attention.test.ts`: each rule fires, and each stays quiet exactly
  at its threshold. Also asserts `P95_TIMEOUT_MS === TIMEOUT_MS`.
- `v2/pages/observability/common.test.ts`: `asTraceStatus`.
- `v2/pages/{governance,registry,online,experiments,tasks,skilllab,workspaces}/attention.test.ts`:
  membership, signal, precedence and order, the full list behind the cap, and the empty
  case. Tasks and Skill Lab pin `TZ`.
- `v2/attentionDismiss.test.ts`: filter, cap after filter, remaining count, the
  signal-keyed identity, the 30-day prune (and its edge), garbage and throwing storage.
- `v2/pages/tasks/{attention,scorecardAttention}.test.ts` "failed run expiry": the 7-day
  edge, superseded by a later completed run, not by an earlier one, not by a later failed
  run, not across benchmarks / agents / modes, and online configs untouched. They pass a
  fixed `now`.
- Rendering is checked with a Playwright run on dev that stubs
  `**/api/observability/dashboard*` (there is no DOM test environment). The run checks:
  - the panel renders, and its rows come in the right order;
  - clicking a row lands on `?tab=traces&status=error`;
  - the URL state survives a reload, and the select stays in sync with the URL;
  - the copy reads correctly in en and zh-CN.
