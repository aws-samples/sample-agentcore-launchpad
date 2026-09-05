## Summary

A deep link whose id no longer resolved failed silently: Evaluation `?ds=` / `?ev=` / `?oe=` / `?exp=` rendered the unselected list with the stale param still in the URL, `/chat?agent=<gone>` quietly selected the first active agent, and `/knowledge-bases?view=detail` without an id showed "LOADING" forever. Skill Lab and Workspaces already did the right thing.

- New shared `components/StaleLink.tsx` + `useStaleParam` hook: one dismissible `role="status"` notice — "<Kind> `<id>` no longer exists in this workspace — pick one from the table below / the agent picker." — that also strips the stale param (`replace: true`).
- Wired on Datasets, Evaluators, Online evaluation, Experiments, Chat and Knowledge Base detail (unknown id, any 4xx on the detail fetch, or a missing id). Chat no longer substitutes another agent: the picker stays on a placeholder until the user chooses, and the linked `session` is dropped with it.
- Copy in en + zh-CN (`staleLink.*`); `docs/architecture.md` + zh-CN twin record the pattern.

## Verification

- `make verify` PASS.
- Playwright on a worktree build: all six `does-not-exist` URLs plus the id-less KB detail show the notice and lose the stale param; Chat's `agent-select` value is `""`; valid deep links (real dataset / agent ids from the API) keep their param, show no notice, and select the linked resource.

Self-evolution direction SE-007 (`ux` path).
