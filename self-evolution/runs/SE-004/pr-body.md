## Summary

The console already has a deliberate `@media(max-width:720px)` tier (sidebar → nav strip), but on a 390 px viewport half the pages still scrolled **as a page**: the rule that makes tables scroll only matched `.panel > table`, so tables inside `.pbody`/`DataTable`/other wrappers (Agent Management, Users, Knowledge Bases, Registry) pushed the document to 410–787 px; the Registry toolbar did not wrap, the Memory actor `<select>` grew to its longest option, and the Chat side panels' `.kv`/`.code` children lacked `min-width:0`.

- Shared `.table-scroll` wrapper (`overflow-x:auto;min-width:0`, a no-op when the table fits) in `DataTable` and around the raw `<table>`s in Agent Management, Registry and Knowledge Bases.
- `< 1180 px`: children of the collapsing grids (`.grid-2`, `.reg-grid`, `.chat-grid`, `.mem-grid-3`) get `min-width:0`.
- `< 720 px`: `.tabs`/`.tabs-actions` wrap, `.search` loses its 420 px cap, filter pickers clamp to the panel, `.histrow` wraps, the creation stepper scrolls.
- `docs/architecture.md` (+ zh-CN) gains "Console layout breakpoints" recording the two tiers and the `.table-scroll` rule for new pages.

## Verification

- `make verify` PASS.
- Playwright probe on a worktree build with real fonts served, all 30 console routes: `scrollWidth === 390` at 390×844 (before: 7 routes overflowed, up to 787 px) and `scrollWidth === 1440` at 1440×900 (desktop unchanged). Screenshots of the six former offenders at 390 px show tables/toolbars scrolling or wrapping inside their panels.
- Merge note: PR #73 adds a third `DataTable` render branch whose bare `<div>` should also get `className="table-scroll"` — a one-word follow-up when both are in.

Self-evolution direction SE-004 (`ux` path).
