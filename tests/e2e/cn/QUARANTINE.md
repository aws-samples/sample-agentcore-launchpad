# e2e quarantine list (ENV failures twice in a row) — rules in `../README.md` § "Failure classification"

Only the lead adds or removes entries. A quarantined spec still runs; its ENV failure is annotated `observation` and does
not grade the chain until the exit criterion is met.

| spec / step | id | first | last | evidence | exit criterion | state |
|---|---|---|---|---|---|---|
| `cn/scenario/s07-evaluate.spec.ts` s07c — batch replay runs (HR assistant + runtime twin) | N-SCN-obs21 (test-side bounds) · N12-obs53 / N12-obs55 (product resilience, r12-f) | 2026-10-16 09:44Z (storyline-2, judge call) | 2026-10-16 13:29Z (s07-2, agent replay) | `docs/cn/evidence/round-11/storyline-2/json/s07c-rt-results.json`, `docs/cn/evidence/round-11/s07-targeted/json/s07c-hr-results.json` | s07c passes (no ENV annotation needed, or obs21-soft only) in **two consecutive runs** after r12-f (worker retry on upstream transients) is live on the target host | **exited** 2026-10-18 — criterion met: two consecutive clean runs with r12-f live on t1 (s07-3 2026-10-17 19/19; storyline-3 2026-10-18 14:47Z HR 36/36 + twin 27/27 — evidence curated by e2e-r11 under `docs/cn/evidence/round-13/` on `cn/e2e-r11`); graded normally again from the next run (register N13-obs24) |
