# V2 Onboarding: Launch Checklist and Guided Tour

> Ported from `origin/console-v3` by task `10-13-v2-onboarding`. Frontend-only. The
> user decided two things (2026-10-13): the tour **auto-starts** on a browser's first V2
> Home visit, and the checklist **replaces** Home's static lifecycle strip.

## 1. Scope / Trigger

This spec applies when you:
- change V2 Home's layout or the elements the tour points at;
- add a tour stop or a checklist step;
- write any browser automation or recording that opens `/v2`.

## 2. Signatures

```ts
// v2/onboarding/steps.ts (pure)
launchSteps({ workspace, agents, chattedAgentIds, tasks, isAdmin }): LaunchStep[]  // workspace · agent · chat · evaluate · online
nextStep(steps): LaunchStep | null;  chatProbeIds(agents): string[]               // first 3 live agents

// v2/onboarding/state.ts — in-memory store + listeners, every storage access try/catch
TOUR_DONE_KEY = "launchpad_v2_tour_done";  launchHiddenKey(ws) = `launchpad_v2_launch_hidden:${ws}`
useTourDone() / setTourDone();  requestTour() (pending until it starts on Home)
useLaunchHidden(ws);  requestLaunchChecklist(ws) / takeLaunchScroll()

// v2/onboarding/stops.ts (pure) — TOUR_STOPS: cmdk · launch · stats · agents · health · modules · help
visibleStops(stops, exists); seekStop(...); placeCard(...); tourKeyAction(...); OVERLAY_SELECTOR
```

## 3. Contracts

### Checklist

- **Each step's state comes from real data, never from a manual tick.**

  | Step | Done when | Data source |
  |---|---|---|
  | workspace | `bootstrap_status === "ready"` | workspace context |
  | agent | any agent is active | the existing 10 s agents poll |
  | chat | a session exists for one of the first 3 live agents | one probe per agent, re-run only when the live-id string changes, never on each poll |
  | evaluate | a `run` task exists | Home's existing `loadTasks` |
  | online | an agent-owned online config exists | Home's existing `loadTasks`; this step replaces V3's DLC gate |

- **Members.** For a member, the workspace step has no link (`/v2/workspaces` is
  admin-only) and explains that an admin bootstraps the workspace.
- **Rendering.** The checklist renders only once all of its inputs have loaded, so steps
  never flip on the first paint. When every step is done it collapses to one line.
- **Hide.** Hiding is per workspace. The help menu's *Launch checklist* item unhides it
  and scrolls to it.

### Tour

- **Auto-start.** The tour starts once, about 1.2 s after a browser first lands on
  `/v2` with `launchpad_v2_tour_done` unset.
  - Skip, End, Esc and *Got it* set the flag.
  - Leaving Home mid-tour ends it **without** setting the flag.
  - Replay through `requestTour()` from the help menu or the palette's `{action:"tour"}`.
    It navigates to `/v2` first.
- **Pausing.** While any of these is open, the tour hides itself and does not set the
  flag, then resumes at the same stop:
  - the palette;
  - the glossary;
  - anything matching `.v2 [role=dialog]:not(.v2-tour)`, `.v2 [role=menu]` or
    `.v2 .v2-pop`.

  A `MutationObserver` on `.v2` re-checks, at most once per frame.
- **Keys.**
  - → and Enter move to the next stop, ← to the previous one, and Esc ends the tour.
  - Keys are ignored while paused, inside `.v2-cmdk`, in fields, during IME composition,
    with a modifier held, or when the event is already `defaultPrevented`.
  - ⌘K always stays with the shell.
- **Stops.**
  - A stop's target is looked up **when the tour reaches it**, so a late-mounting
    checklist still works.
  - A missing target, or one that isn't laid out, is skipped. If no stop is left, the
    tour closes without setting the flag.
  - Anchors use `data-tour` on wrapper divs. `Card` is not modified.
- **Layering.**
  - The tour renders inside `.v2` at z-index 140: below the palette (160) and toasts
    (200).
  - It catches clicks (`aria-modal="true"`), so the page can't be clicked mid-tour. Only
    ⌘K (or the glossary reached from the palette) can interrupt it.
  - It is positioned with top/left only, with no transforms. Reduced motion is guarded
    with equal-specificity selectors.

### Automation and recordings

**Any browser automation that opens `/v2` must set `launchpad_v2_tour_done` before its
first navigation.** Otherwise the tour covers the page and catches clicks:

```js
await context.addInitScript(() => { try { localStorage.setItem("launchpad_v2_tour_done", "1"); } catch {} });
```

The shared recorder `data/videos/architect-recorder/source/rec.mjs` (untracked) sets it,
and `docs/demo-video-production.md` documents it. Per-episode recorders under
`data/videos/*/source/` predate this; add the line before reusing one.

## 4. Gotchas

- In dev, React StrictMode doubles the initial chat probe (3 agents probed twice). That
  is expected. Measure "no growth per poll", not the first burst.
- Long English labels in the 5-column step grid need a `minmax(0, 1fr)` track,
  `min-width: 0` and wrapping buttons. Without them, at 1440 px the buttons widened the
  steps and clipped the hints.

## 5. Tests

- `steps.test.ts` covers the done and undone state of each step, the next step, all
  done, and the member variant.
- `state.test.ts` covers the tour flag, a flag set before load, the request store,
  per-workspace hide, storage that throws, and missing storage.
- `stops.test.ts` covers the stop order and cap, copy, `visibleStops`, `seekStop` with a
  late target, every `placeCard` branch, the overlay selector, and every `tourKeyAction`
  rule.
- `palette.test.ts` covers the `tour` action entry in en and zh.
- A Playwright run on dev with GET stubs only covers:
  - auto-start, all stops, Back, finish and Esc;
  - pausing for the palette, help and glossary;
  - replay;
  - the checklist none-done, all-done, member, hide and reopen states;
  - the probe request count.
