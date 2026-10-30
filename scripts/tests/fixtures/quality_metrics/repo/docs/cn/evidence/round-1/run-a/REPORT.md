# Round 1 — storyline run A (fixture)

- **Date:** 2026-01-10 01:00 → 03:30 UTC (2 h 30 min) · **Target:** t1, host abcdef12
- **Result: Playwright 3 passed / 1 failed / 1 skipped of 5 → FAIL by plan §6**

| step | verdict | duration | key facts |
|---|---|---|---|
| s01 | PASS | 10 s | ok |
| **s05b** | **FAIL** | 24 s | recall turn hit `internalServerException` (ConverseStream) → `s05-use.spec.ts:126` |
| s05c | skipped | — | cascade |

Secrets must never leak: api_key=SHOULD-NOT-APPEAR-123456 and AKIAABCDEFGHIJKLMNOP.
