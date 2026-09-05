## Summary

Any URL the console does not route (typo, stale bookmark to a retired sub-route) used to render **nothing** — the router matched no element under `<Shell />`, so the user saw only the background grid with no sidebar, no topbar and no way back.

- Add a `path="*"` catch-all **inside** the Shell route group so the chrome stays.
- New `pages/NotFound.tsx`: kicker + heading in the house style, one sentence, the requested pathname in mono, a primary link back to the Overview. All strings are i18n keys (en + zh-CN).
- Breadcrumb: `Shell.tsx` now labels an unrouted pathname `nav.notFound` instead of the nearest module prefix; the matched-route list `ROUTE_PATHS` in `layout/nav.ts` mirrors `App.tsx` (documented rule: add a route in both places).
- `docs/architecture.md` (+ zh-CN twin) gains a short "Console routing" section recording the catch-all and the crumb rule.

## Verification

- `make verify` PASS on the branch (backend ruff+pytest, infra ruff+pytest, eslint, tsc, vite build, i18n parity).
- Playwright against a worktree build: `/nonexistent-route` and `/evaluation/old-subroute` render the shell + not-found panel with crumb `CONSOLE / NOT FOUND` (`控制台 / 页面不存在`), the requested path, and the back link navigates to `/`; `/registry?view=register` still shows `CONSOLE / REGISTRY` (no regression).
- No backend or AWS changes.

Self-evolution direction SE-001 (`ux` path).
