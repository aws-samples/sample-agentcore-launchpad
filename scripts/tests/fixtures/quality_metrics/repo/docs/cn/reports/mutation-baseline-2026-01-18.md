# Mutation baseline — fixture (2026-01-18)

## 1. Backend (mutmut 3.8.0)

| file | mutants | killed | survived | timeout | no tests | score | score (covered) |
|---|---|---|---|---|---|---|---|
| `app/services/ownership.py` | 40 | 36 | 4 | 0 | 0 | 90.0 % | 90.0 % |
| `app/services/users.py` | 160 | 100 | 40 | 0 | 20 | 62.5 % | 71.4 % |

## 2. Frontend (Stryker 10.0.0)

| file | mutants | killed | survived | timeout | no coverage | score | score (covered) |
|---|---|---|---|---|---|---|---|
| `src/v2/ownership.ts` | 20 | 13 | 4 | 0 | 3 | 65.0 % | 76.5 % |
| **total** | 20 | 13 | 4 | 0 | 3 | **65.0 %** | 76.5 % |

## 3. Surviving mutants

| mutant | operator | meaning |
|---|---|---|
| `app/services/users.py:120` | swap `>=` → `>` | the minimum-length boundary is untested — password=hunter2 would pass |
