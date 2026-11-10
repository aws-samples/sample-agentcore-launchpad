# Round 1 — storyline run B (fixture, text-only)

Standalone run on **t1** (host 12345678), 2026-01-12 10:00 → 10:40 UTC, **8 passed / 2 failed** (s08c, s09).

- **s08c FAIL (test-side strictness; known N-SCN-obs2)** — `s08-improve.spec.ts:312` hard-asserts a decidable verdict.
- **s09 FAIL (cascade of s08c)** — `s09-go-live.spec.ts:65` "no canary endpoint left".
