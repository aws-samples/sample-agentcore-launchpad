# V2 Shell UX: Nav Mode, Command Palette, Glossary, Help Menu

> These four shell features help people find their way around the V2 console. They
> were ported from `origin/console-v3` by task `10-13-v2-shell-ux` and are
> frontend-only.

## 1. Scope / Trigger

- Applies when you add or rename a V2 page, add jargon to a V2 surface, add a global
  keyboard shortcut, or add an item to the top bar's help menu.

## 2. Signatures

```ts
// lib/nav-mode.ts
type NavMode = "business" | "expert";
NAV_MODE_KEY = "launchpad_nav_mode";          // localStorage; every access try/catch
defaultNavMode(): NavMode;                     // "expert" for EVERY role (decision, below)
useNavMode(): [NavMode, (m: NavMode) => void];

// v2/nav.ts — entry fields
business?: true;      // shown in business mode
hintKey?: string;     // glossary key, rendered as the item's title
navGroupsFor(mode, isAdmin): V2NavGroup[];   // admin filter, business filter, drops empty groups

// v2/glossaryTerms.ts (logic) + v2/Glossary.tsx (UI)
GLOSSARY_TERMS: string[];                      // 20 keys → glossary.<key>, glossary.name.<key>
openGlossary(term?: string); closeGlossary(); useGlossary();
HintIcon / HintLabel / Term; GlossaryDialog (mounted once in V2Shell)

// v2/palette.ts (logic) + v2/CommandPalette.tsx (UI)
score(query, text);                            // V3 verbatim (note the argument order)
buildEntries({ pages, agents, terms, t, tEn? }): PaletteEntry[];
rank(entries, query, cap = 40);
```

## 3. Contracts

- **Nav mode is a view filter.** It never changes permissions, and every page stays
  reachable by URL and from the palette.
  - **Everyone defaults to `expert`.** This was the user's decision on 2026-10-13; V3
    defaulted members to business. Business mode is opt-in, so nobody's sidebar
    changes without their consent. With no stored mode, the sidebar must deep-equal
    main's admin-filtered `V2_NAV`, and `v2/nav.test.ts` pins this.
  - Business mode shows exactly `/v2`, `/v2/assistant`, `/v2/agents`,
    `/v2/knowledge-bases`, `/v2/chat`, `/v2/observability` and `/v2/videos`. A new page
    is expert-only unless it is clearly build-and-run.
- **The palette lists every page the viewer's role allows.** It ignores nav mode, which
  is how hidden pages stay reachable.
  - A new V2 page shows up automatically, because entries come from `V2_NAV`.
  - `V2_PALETTE_ALIASES` (`v2/nav.ts`) adds palette-only entries for a retired sidebar
    item that now lives on a tab: 持续评估（在线评估） → `/v2/eval/tasks?tab=online`. They
    take the same role filter, never enter `V2_NAV` (sidebar, `activeNavItem`), and the
    label must contain the old name so both zh and en queries still find it.
  - English labels are always keywords, so English matches in the zh-CN UI.
  - An empty query lists only pages and help actions. Agents and glossary terms appear
    once something is typed: dev has 51 agents, which would otherwise fill the 40 slots.
  - Agents are fetched only while the palette is open.
- **Keys.** One `window` keydown listener lives in `V2Shell`.
  - ⌘K / Ctrl+K toggles the palette everywhere, including inside inputs.
  - `/` opens the palette only when:
    - no Ctrl, ⌘ or Alt is held;
    - no IME composition is in progress;
    - focus is not in an input, textarea, select or contenteditable;
    - no dialog, drawer, dropdown or menu is open.

  Any new global shortcut follows the same guards. Never steal a key that someone may be
  typing.
- **Focus.** The palette, the glossary dialog and the help menu each record
  `document.activeElement` on open, focus their own first control in a layout effect,
  and restore the recorded element on close. Esc closes only the topmost layer.
- **Glossary placement.**
  - Use `HintIcon` / `HintLabel` inside form labels. They show a tooltip on hover and on
    keyboard focus, and do not open the dialog, because a `<button>` inside a `<label>`
    would become the label's control.
  - Use `Term`, a button that opens the dialog at the term, in running text.
  - `OptionCard` takes a `hint`.
  - A new term needs `glossary.<key>` and `glossary.name.<key>` in both locales, and
    `glossary.test.ts` checks this.
- **Help menu.** Its items come from an array in `HelpMenu.tsx`. Step 3's tour and
  launch checklist are added there.
- **Layering.** Everything renders inside `.v2`, with no portals.

  | Layer | z-index |
  |---|---|
  | top bar | 20 |
  | mask | 100 |
  | dropdown popup | 150 |
  | palette | 160 |
  | toast | 200 |

  Don't add a transform or filter to a container, per `frontend/component-guidelines.md`.

## 4. Gotchas

- `score()` takes `(query, text)`, V3's order. It is kept verbatim so the ported tests
  hold.
- `navigator.platform` is deprecated, but it is only the ⌘ vs Ctrl label heuristic; the
  shortcut accepts both keys everywhere.
- A tooltip on an icon at the left edge of the content area spills over the sidebar edge.
  It is readable and not clipped; it's cosmetic.

## 5. Tests

- `lib/nav-mode.test.ts`: parsing, the expert default, a stored value winning, storage
  that throws or is missing.
- `v2/nav.test.ts`: expert deep-equals today's sidebar for an admin and a member, business
  shows the 7 entries, empty groups are dropped.
- `v2/glossary.test.ts`: search (localized name, English name, definition, key), copy in
  both locales, every nav `hintKey` resolving, the store.
- `v2/palette.test.ts`: `score`, en/zh queries, empty-query pages + help only, the cap,
  the role filter, nav-mode independence, agent URLs, glossary and help entries, key
  rules, the shortcut label.
- Rendering and keyboard behaviour are checked with a Playwright run on dev, using GET
  stubs only (there is no DOM test environment):
  - the shortcuts, including `/` inside inputs;
  - focus restore;
  - tooltips on hover and focus;
  - business mode surviving a reload;
  - a stubbed member account;
  - en + zh-CN.
