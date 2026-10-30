# Round 1 — fixture register

| id | subsystem | severity | symptom | repro | evidence | suspected cause / file |
|---|---|---|---|---|---|---|
| N10-bug1 | gateway | **High** (blocks attach) | picker empty | steps | shot | `services/bootstrap.py` |
| N10-obs1 | memory | Low | observation only | — | — | — |

## Fixes

| id | fix | files | tests | commit |
|---|---|---|---|---|
| N10-bug1 | call ensure_default_records at bootstrap end. Live re-test pending. | `services/bootstrap.py` | `test_bootstrap.py` | abcd1234 |
