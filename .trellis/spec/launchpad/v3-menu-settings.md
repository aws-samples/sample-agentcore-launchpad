# V3 Menu settings — hidden entries, sections and presets

## 1. Scope / Trigger

- One hub-global hidden list (`platform_settings.nav_hidden_modules`) drives what the V3
  rail, ⌘K and the Command center show. Since 2026-10-25 (task
  `10-25-v3-menu-modes-workbench`) it applies to **every account, administrators
  included** (was members-only in 737663fd), carries pinned-entry and Command-center
  section keys, and Menu settings has Normal / Full presets.
- Trigger: any change to the rail, ⌘K, the Command center sections, or a new key kind.

## 2. Signatures

- Backend (unchanged): `GET /api/nav/hidden-modules` (MEMBER) → `{hidden: string[], updated_by, updated_at}`;
  `PUT /api/nav/hidden-modules` (ADMIN) `{hidden: string[]}`; `services/nav_settings.py`
  validates shape only: `^/v[23](/[A-Za-z0-9_\-/]*)?(\?[A-Za-z0-9_\-=&]*)?$`, ≤ 200 chars,
  ≤ 100 entries; 422 `nav.bad_path` / `nav.too_many`.
- Frontend (`frontend/src/v3/nav.ts`):
  - `withoutHidden(groups, hidden)` — no admin bypass; keeps `item.admin === true`.
  - `withoutHiddenPinned(items, hidden)` — filters only `PINNED_HIDEABLE`.
  - `PINNED_HIDEABLE = ["/v3", "/v3/gate"]`
  - `HOME_SECTIONS = { launch: "/v3?section=launch", turn: "/v3?section=turn" }`
  - `NORMAL_HIDDEN` (sorted) = `/v2/eval/standards`, `/v3/gate`, `/v3?section=launch`, `/v3?section=turn`; `FULL_HIDDEN = []`
  - `hideableKeys()` — every key Menu settings offers (pinned ∪ sections ∪ non-admin module rail keys).
  - `menuMode(hidden, known): "normal" | "full" | "custom"` — compares only `known` keys.
- `frontend/src/v3/hiddenNav.ts`: `HiddenNavContext`, `useHiddenNav(): { hidden, isAdmin, reload, ready }`,
  provided by `Shell.tsx` around every `<main>` branch from the one Shell read.

## 3. Contracts

| Key kind | Example | Consumer | Effect |
|---|---|---|---|
| Module rail key (path before the V3 twin mapping) | `/v2/eval/standards` | `withoutHidden` | leaves rail + ⌘K pages |
| Pinned entry | `/v3`, `/v3/gate` | `withoutHiddenPinned` → `native` | leaves rail + ⌘K pages; `/v3/gate` also drops the per-agent "X 的放行门" ⌘K actions (`CommandPalette gate`) |
| Command center section | `/v3?section=launch` / `turn` | `V3Home` (+ Help menu for `launch`) | `LaunchSequence` / `YourTurn` not rendered; Help → 显示启动序列 removed |

- Never filtered: `admin: true` entries (Menu settings, Users, Workspaces, …) — an admin can always undo. 智能体 / 对话 are not hideable.
- Stewards skip the read (empty set, `ready = true`); a failed read hides nothing.
- Hiding is a view filter, not authorization: every route stays reachable by URL; `/v3` and the brand link still open the Command center when 指挥中心 is hidden.
- Presets fill the draft only; Save PUTs `hidden ∩ hideableKeys()`.

## 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| Hidden list read fails | nothing hidden (`ready = true`), rail intact |
| List still loading | sections not rendered until `ready` (no flash), rail shows everything |
| Stored key the catalog no longer lists | ignored by filters and by `menuMode` |
| Stored key for an `admin: true` entry | ignored (entry kept) |
| PUT with a malformed path / > 100 entries | 422 `nav.bad_path` / `nav.too_many` |

## 5. Good / Base / Bad Cases

- Good: Normal saved → every account's rail lacks 放行门 / 判据, Command center lacks 启动序列 / 轮到你, ⌘K keeps only glossary hits for 放行门; Menu settings still in the admin rail.
- Base: empty list (Full) → everything shows; existing module-only lists read as 自定义 unless they equal a preset.
- Bad: hiding a new pinned entry by adding it to `NATIVE` without listing it in `PINNED_HIDEABLE` → it can never be hidden (and Menu settings will not offer it).

## 6. Tests Required

- `frontend/src/v3/nav-hidden.test.ts`: admins are filtered too; `admin: true` never filtered; pinned filter leaves 智能体 / 对话; `menuMode` normal / full / custom with stale keys ignored; every `NORMAL_HIDDEN` key ∈ `hideableKeys()`; `hideableKeys()` excludes `/v3/agents`, `/v3/chat`, `/v3/menu-settings`.
- Browser (manual / Playwright over a throwaway vite): Normal → Save → admin and member views; Full → Save; restore the box's original list in a `finally` (the list is live, hub-global data). Mark the tour done (`localStorage.launchpad_v3_tour_done = "1"`) or its overlay intercepts clicks on `/v3`.

## 7. Wrong vs Correct

### Wrong
```ts
// re-adding an admin bypass, or filtering admin-only entries
if (isAdmin) return groups;
items.filter((item) => !hidden.has(item.railKey));
```
### Correct
```ts
items.filter((item) => item.admin === true || !hidden.has(item.railKey));
```

### Wrong
```tsx
// a page reading the list itself, rendering sections before it lands
const hidden = useLoad(() => navSettingsApi.hiddenModules(), "x");
{!hidden.data?.hidden.includes(HOME_SECTIONS.launch) && <LaunchSequence … />}
```
### Correct
```tsx
const { hidden, ready } = useHiddenNav();
{ready && !hidden.has(HOME_SECTIONS.launch) && <LaunchSequence … />}
```
