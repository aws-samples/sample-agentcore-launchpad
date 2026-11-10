# UI combination suites (round 16, r16-n)

Three Playwright suites that look for **interaction** bugs — what single-control tests
(r16-m) cannot see — on a live Launchpad-CN console. They are **not** part of `make verify`;
their generators and spec tables are (`node --test cn/combinations/*.unit.test.ts`, the
"e2e · combination generators" section of `scripts/verify.sh`).

| file | what |
|---|---|
| `pairwise.ts` | all-pairs generator (greedy AETG, seeded, constraint-aware: a pair counts only when some legal full row contains it) + `fullProductSize` |
| `wizard-domains.ts` | AgentWizard / TaskWizard option domains, structural constraints ("na" = the wizard does not show it), the **oracle** (what the wizard must block / explain, what `POST /api/agents` / `POST /api/eval/runs` must answer), and `AGENT_API_PROBES` (rows the wizard cannot build, the capability matrix must refuse) |
| `page-factors.ts` | control factors of six list pages (tasks `?tab=all`, data center, scorecard `?view=agent`, agents, registry, users) derived from the source, their constraints and the URL params each row promises |
| `walk-model.ts` | the random walk's click policy (destructive only on `x16n-` fixture rows, never sign-out / deploy / install), effect detection, URL ↔ page rules |
| `w1-agent-pairwise.spec.ts` | step 1a: drives the AgentWizard for every row; live deploys capped (`X16N_LIVE_CAP`, default 6, the 4 anchor fixtures first); every other accepted row is **replayed under a reserved preset name** so the server validates the exact spec the wizard sent (409 `agent.name_reserved` = passes, 422 = refused) and nothing deploys; blocked rows are replayed from the method's captured base spec |
| `w2-task-pairwise.spec.ts` | step 1b: TaskWizard rows against the `x16n-fx-harness` fixture (3 sessions, two datasets, a CloudWatch service); runs are real, stopped + deleted immediately beyond `X16N_KEEP_RUNS` |
| `p1-page-pairwise.spec.ts` | step 2: filters × paging × tabs × selection × role × visibility mode; rows == API (same role's cookie), paging without drops / repeats, promised URL params + reload |
| `r1-random-walk.spec.ts` | step 3: seeded random walk, `X16N_WALK_RUNS` × `X16N_WALK_STEPS` per module (tasks, agents, memory) per role; invariants: no page / console error, effect ≤ 5 s, URL ↔ page, recoverable, list totals == API |
| `w9-cleanup.spec.ts` | deletes every `x16n-*` fixture and the member, restores the visibility mode, verifies |

## Running

```bash
cd tests/e2e && npm install
export LAUNCHPAD_E2E_BASE_URL="https://<t1>:8443"
# in-process only — never on disk, never in a screenshot
export LAUNCHPAD_E2E_PASSWORD="$(aws ssm get-parameter --name launchpad-console-password \
  --with-decryption --query Parameter.Value --output text --profile cn-northwest-1 --region cn-northwest-1)"
npx playwright test -c combinations.config.ts cn/combinations/w1-agent-pairwise.spec.ts
npx playwright test -c combinations.config.ts cn/combinations/w2-task-pairwise.spec.ts
npx playwright test -c combinations.config.ts cn/combinations/p1-page-pairwise.spec.ts   # X16N_PAGES=agents,users to narrow
npx playwright test -c combinations.config.ts cn/combinations/r1-random-walk.spec.ts     # X16N_WALK_RUNS / X16N_WALK_STEPS / X16N_WALK_ROLES
npx playwright test -c combinations.config.ts cn/combinations/w9-cleanup.spec.ts
```

The member fixture's password is derived in memory from the admin password
(`lib.memberPassword()`) unless `LAUNCHPAD_E2E_MEMBER_PASSWORD` is set. Evidence goes to
`X16N_OUT` (default `/home/ubuntu/designs/combinations-20261019/`): `evidence/results.jsonl`
(one row per combination: verdict, class DEAD / BROKEN / INCONSISTENT / UX / FLAKY, the
combination, expected vs observed, a reproducible `repro` with the seed), `evidence/*-plan.json`
(pairs generated vs full product), `screenshots/`. Request / response bodies pass `redact()`.

Reproduce one row: the same `X16N_SEED` regenerates the same rows in the same order; a
random-walk failure names its seed and step list (`walk-<module>-<role>-run<n>.json`).
