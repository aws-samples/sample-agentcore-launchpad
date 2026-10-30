# Launchpad-CN end-to-end / UI tests (N10 v2 → v4)

Playwright suite that drives the **deployed** Launchpad-CN console exactly as a user
would: login form, V2 wizard, Chat Playground, Memory / Knowledge Bases / Registry /
Identity / Gateway / Observability / Skill Lab pages. It creates real AgentCore resources (named
`e2e4-…` in round 4; v3 `e2e3-…`, v2 `e2e2-…`, v1 `e2e-…`) through the product's own UI and HTTP API — it is therefore **not** part of
`make verify` and must never run in CI without a live target.

## Prerequisites

- Node ≥ 20 and the Playwright Chromium build (`npx playwright install chromium` once).
- Network reach to the console (`https://<host>:8443`, self-signed TLS is accepted).
- `zip` on PATH (the BYOC and skill cases build their upload bundles on the fly).
- Read-only AWS credentials are only needed by `cn/api-smoke.sh`'s caller to fetch the
  console password from SSM — the suite itself never touches AWS directly.

## Running

```bash
cd tests/e2e
npm install

export LAUNCHPAD_E2E_BASE_URL="https://203.0.113.50:8443"   # t1; default env is https://203.0.113.60:8443
export LAUNCHPAD_E2E_USERNAME="admin"
# fetch the password in-process; it is never written to disk, logs or screenshots
export LAUNCHPAD_E2E_PASSWORD="$(aws ssm get-parameter --name launchpad-console-password \
  --with-decryption --query Parameter.Value --output text \
  --profile cn-northwest-1 --region cn-northwest-1)"

bash cn/api-smoke.sh                 # read-only API smoke, ~5 s
npx playwright test                  # all cases, serial (deploy cases take 5–15 min each)
npx playwright test cn/02-*.spec.ts  # one case
N10_REUSE=1 npx playwright test cn/06-*.spec.ts   # reuse an already-deployed e2e2-<prefix> agent
N10_GROUP_B=1 npx playwright test cn/05-*.spec.ts cn/17-*.spec.ts   # cases gated on N11 group B ("B deployed")
N10_BOOTSTRAP_SUMMARY=/path/to/bootstrap.log npx playwright test cn/22-*.spec.ts
npx playwright show-report           # HTML report with per-case attachments (job logs, answers)
```

The `setup` project signs in once and stores the session as `.auth/state.json`
(git-ignored); every spec reuses it. Evidence screenshots land in
`docs/cn/evidence/round-4/` (`N10_EVIDENCE_ROUND=round-3|round-2|round-1` for the earlier layouts), a timestamped
ledger in `.superpowers/sdd/n10v4/progress.md` (git-ignored, `N10_LEDGER` overrides). Specs of one
run share a small JSON state (`.auth/n10v4-state.json`: run start, the resolved Skill Lab sample
names, AuthGate stall samples, the spec-31 member) so a Skill Lab run and an agents run can go in parallel.

Round-4 run (default env, after the lead's go): `npx playwright test cn/02-* cn/04-* cn/06-* cn/10-* cn/11-* cn/16-* cn/17-* cn/22-* cn/29-* cn/30-* cn/31-*`
(agents, serial) alongside `npx playwright test cn/23-* cn/24-* cn/25-* cn/26-* cn/27-* cn/28-*` (Skill Lab),
then `npx playwright test cn/99-* cn/99b-*` (cleanup; `N10_KEEP` defaults to `none` — round 4 keeps nothing of its own).
Follow-up runs reuse the deployed agents: `N10_REUSE=1 npx playwright test cn/11-*` (case 11 again),
`N10_REUSE=1 N10_A2A_SOFT=1 npx playwright test cn/16-*` (record A's direct-chat outcome instead of asserting it and still
verify the consumer half — B's `ask_<A>` relaying the peer's failure text). Give each parallel/extra run its own
`PLAYWRIGHT_HTML_OUTPUT_DIR=playwright-report-<x>` and `--output test-results-<x>` so reports do not overwrite each other.

## Cases (docs/cn/REQUIREMENTS.md §4.1, v2 checklist)

| spec | case | quantified assertion |
|---|---|---|
| `01-login-overview-nav` | 1 | nav features == `/api/capabilities` (14 on default — 13 after N12/N13 + `policy` since D20; no literal) in en + zh-CN; zh-CN: 0 visible raw i18n keys on every feature page |
| `02-harness-create-deploy-chat` | 2 | Harness (method B) deploy ≤ 15 min; streamed model text (non-echo/non-error); memory rail ≥ 1 event **without reload** |
| `03-studio-create-deploy-chat` | 3 | Studio (method C) same limits as 2 |
| `04-byoc-create-deploy-chat` | 4 | BYOC answer is model text (no `[echo`); no `EndpointConnectionError` in the runtime log |
| `05-gateway` | 5 | hr-database attachable without `sync-defaults`; ≥ 1 tool call; answer names Maya Chen / HR Operations (**gated: `N10_GROUP_B`**) |
| `06-memory-longterm` | 6 | 2 facts → ≥ 2 long-term records ≤ 10 min → session 2 recalls 2/2 |
| `07-knowledge-base` + `78-kb-skill-agent` | 7 | ingestion ≤ 15 min, indexed docs == uploaded (2); top-1 hit = expected doc; agent calls `kb_search` and quotes it |
| `08-registry-skill` + `78-kb-skill-agent` | 8 | one A2A record per deployed agent; skill import → APPROVED; skill task completes without a MaxTokens error turn |
| `09-identity-provider` | 9 | `launchpad-global-bedrock` api_key ready; custom OAuth2 provider create/delete 200, gone after delete |
| `10-observability` | 10 | sessions ≥ deployed agents; first trace of a new turn ≤ 60 s |
| `11-assistant` | 11 | proposal ≤ 3 turns; active ≤ 15 min after approve; Chat model text |
| `12-users` | 12 | member: no Users nav, `/api/users` 403, DELETE other's agent 403; admin sees all |
| `13-workspaces` | 13 | default `bootstrap_status == ready`; invalid role ARN → 4xx + visible UI error |
| `14-public-api` | 14 | key → `/v1` 200 with the same answer as Chat; disabled → 401 |
| `15-builtin-tools` | 15 | Code Interpreter tool call visible; exact integers |
| `16-a2a` | 16 | A2A record approved + live card 200; agent B can mount A |
| `17-lifecycle` | 17 | version +1 after redeploy; runtime/role/log group/S3/A2A/memory records all gone (read-only AWS) |
| `18-error-paths` | 18 | invalid model → failed job + readable UI error; chat deleted agent → 404 + notice; wrong password → 401 + message |
| `19-gateway-admin` | 19 | new target's tool in `/api/tools`; gone after delete |
| `20-memory-admin` | 20 | memory page counts == Chat rail; delete one record → −1 |
| `21-chat-features` | 21 | END SESSION → new session id; New session; mid-stream reload → no hanging state |
| `22-ops-idempotency` | 22 | `/api/health` 200; 14 capabilities (13 + `policy` since D20) incl. `skill_lab`/`evaluation`/`optimization`; bootstrap re-run summary all reused (`N10_BOOTSTRAP_SUMMARY`) |
| `23-skill-lab-nav-status` … `28-skill-lab-publish-mount` | §4.2 | Skill Lab: nav + status + OpenCode-only picker; samples seeded by the product (24, N10v3-bug1); taskgen / train / eval run real rollouts on the worker (`ok ≥ 1`, N10v3-bug2) and an all-errored run is `failed` with a readable reason + `summary.errored` (N10v3-bug3); publish → mount → Chat uses the skill |
| `29-preset-uninstall` | bug6 | architect preset install (console) → uninstall → `not_installed`, role gone, **both** registry records gone (N10v3-bug6 / N11-r4-bug1); classic panel renders Uninstall (AuthGate stall attributed via `v2-auth-loading[data-reason]`) |
| `30-redeploy-failed-status` | bug7 | failed redeploy (package) keeps the agent `active`, `last_deploy_failed` + `last_deploy_error`, Chat 200, amber `v2-agent-redeploy-failed-<name>` tag; a redeploy failing inside `deploy` → `failed` (N10v3-bug7 / N10v2-bug9) |
| `31-preset-observability` | obs31 | preset entries carry `version/release/installed_release/last_job`; `GET /api/system-agents/eval-worker/jobs/<id>` admin 200 / member 403 (N12-obs31 a+c); exactly one APPROVED `expense-report-writer` (N11-obs31) |
| `99-cleanup` / `99b-cleanup-rest` | — | delete `e2e4-…` agents (except `N10_KEEP`, default `none`), KBs, e2e registry + orphan A2A records, this run's Skill Lab jobs/task sets, the member account(s), assistant conversations; never the keepers, the sample skills or `eval-worker` |

`cn/api-smoke.sh` is the curl smoke that runs first. Its `/api/capabilities` probe accepts three
known feature lists — the N10 10-feature baseline, the N12/N13 13-feature list and the current
14-feature default (13 + `policy` since D20) — and prints which one matched; when
`LAUNCHPAD_E2E_FEATURES` is set (a JSON array, e.g. `'["agents","chat",…]'`) it is the **only**
accepted list, so a host with a deliberately narrowed `Settings.features` can still pass.

## N12 evaluation campaign (docs/cn/REQUIREMENTS.md §4.1 cases 24–31, plan §E.3 flows 1–11)

Run serially with the campaign's own prefix / evidence dir / ledger — `cn/n12e.ts` holds the shared
helpers (eval-run poller, V2 dropdown + task-wizard drivers, read-only endpoint reader):

```bash
N10_PREFIX=n12e- N10_EVIDENCE_ROUND=round-n12 N10_LEDGER=../../../.superpowers/sdd/n12-e2e/progress.md \
  npx playwright test cn/40-*.spec.ts cn/41-*.spec.ts cn/42-*.spec.ts cn/43-*.spec.ts cn/44-*.spec.ts \
                      cn/45-*.spec.ts cn/46-*.spec.ts cn/47-*.spec.ts cn/48-*.spec.ts
# flow 11 (rebuilds the worker runtime — run LAST, once), then the cleanup
N10_PREFIX=n12e- N10_EVIDENCE_ROUND=round-n12 npx playwright test cn/49-*.spec.ts
N10_PREFIX=n12e- N10_EVIDENCE_ROUND=round-n12 npx playwright test cn/98-n12e-cleanup.spec.ts   # N12E_KEEP_AGENT=1 keeps the smoke agent
```

| spec | case / flow | quantified assertion |
|---|---|---|
| `40-n12e-smoke-agent` | precondition | Strands `zip_runtime` HTTP agent `n12e-smoke-…` active ≤ 15 min (only `zip_runtime` agents are experiment/canary-eligible); 3 chat sessions, 0 error bubbles |
| `41-evaluators` | 24 / flow 1 | judge-model picker == {GLM 5.3, Kimi K3}; custom judge 201 → listed `custom`, detail level + scale; PUT 200; twin DELETE 200 → gone; classic list shows it; Lambda "Code" probe recorded |
| `42-dataset` | 25 / flow 2 | 3 scenarios (1 `expected_response`, 1 `assertions`, `expected_trajectory` on all); `sync-to-aws` → snapshot `ACTIVE` in the CN artifacts bucket; `publish-version` → versions `["1"]`; listed by `/datasets/cloud` |
| `43-batch-run` | 26 / flow 3 | dataset replay run `invoking → … → completed` ≤ 30 min with scores + explanations (run A); trace-window run (run B) as fallback; judge errors in a "completed" run are soft-failed and reported |
| `44-score-now` | 27 / flow 4 | `v2-obs-score-now` → 2 evaluators → results rendered without a run; API `evaluate` 200 with 2/2 scores; 6 evaluators → 422 |
| `45-online` | 28 / flow 6 | config `ENABLED` (100 %, 1 min, 2 evaluators); results ≤ 3 min after 2 chats; PAUSE → `DISABLED`, RESUME → `ENABLED`; session `online_scores`; DELETE → 404 |
| `46-insights` | 29 / flow 5 | `mode=insights` run completed with `insights` populated; `/v2/eval/insights` shows clusters |
| `47-recommendation` | 30 / flow 7 | gepa_lite / GLM recommendation `COMPLETED` with prompt + explanation; ACCEPT → redeploy job → version +1 |
| `48-experiment-canary` | 31 / flows 8 + 9 | experiment stages recommend → … → verdict → promote → cleanup (API-driven — the configuration experiment has no console surface since N12-obs49; `?mode=configuration` must land on the canary list); canary created through CanaryStart (champion, candidate prompt, evaluators + primary — N12-obs46, hard UI expectations in `cn/canary-ui.ts`); spans of a live session carry `routing_experiment_variant_name` (R-1); canary setup creates `stable…`/`treat…` endpoints (read-only `list-agent-runtime-endpoints`), cleanup removes them |
| `49-worker-preset` | flow 11 | preset ACTIVE; REPAIR (`install {force:true}`) → new runtime version; UNINSTALL → run answers 503 `evaluation.worker_missing` while SCORE NOW works; reinstall → `active` |
| `98-n12e-cleanup` | — | deletes every `n12e-`/`n12e_` object through the product API (owners → runs → online → datasets → evaluators → agents); read-only `list-agent-runtimes` shows no `n12e` runtime |

Results: `docs/cn/reports/n12-e2e.md`; defects: `docs/cn/reports/bugs-round-1-n12.md`.

## End-to-end SCENARIO storyline (`cn/scenario/`, `scenario.config.ts`)

The campaigns above test each module in isolation. `cn/scenario/` chains them into **one
business storyline** — an operator builds the "Octank HR assistant" and takes it from knowledge
→ tools → skills → build → use → observe → evaluate → improve → go live → lifecycle, every step
consuming the artefacts of the previous one. The plan, thresholds, coverage matrix and time
budget are in [`docs/cn/reports/e2e-scenario-plan.md`](../../docs/cn/reports/e2e-scenario-plan.md).

**Status: skeleton only — not yet executed.** The specs transpile (`npm run typecheck`) and
`--list` enumerates the 42 tests / 15 files (29 storyline + 6 s11 govern + 7 s12 model providers), but no environment was available when they were
written; the first run will surface selector and timing slips. Treat the first execution as a
dry run and record its outcome in the plan's results section.

```bash
cd tests/e2e && npm install
export LAUNCHPAD_E2E_BASE_URL="https://<host>:8443"
export LAUNCHPAD_E2E_PASSWORD="$(aws ssm get-parameter --name launchpad-console-password \
  --with-decryption --query Parameter.Value --output text --profile cn-northwest-1 --region cn-northwest-1)"

npm run typecheck                                   # tsc over the whole e2e tree
npm run scenario:list                               # enumerate the steps (no environment needed)
npm run scenario                                    # the whole storyline, serial, ≈ 3 h
SCENARIO_RESUME_FROM=s07 npm run scenario           # resume: s00–s06 are skipped, their artefacts read back
npx playwright test -c scenario.config.ts cn/scenario/scenario-cleanup.spec.ts   # teardown, reverse order
```

The config pins `N10_PREFIX=e2es-` (evaluators `e2es_`), evidence `docs/cn/evidence/scenario/`,
ledger `.superpowers/sdd/e2e-scenario/progress.md`, reports `playwright-report-scenario/`.
Steps share `.auth/scenario-state.json` (git-ignored: agent ids, KB, dataset, run ids, versions,
the throwaway member credential of the second identity). `playwright.config.ts` ignores
`cn/scenario/`, so the campaign runs never pick the storyline up.

**Runtime-log export before teardown (N12-obs61).** `s10` and `scenario-cleanup` export every runtime log group of the
storyline agents (`/aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT` and the canary `-stable<hash>` / `-treat<hash>` arms) to
the private evidence bucket **before** the product's teardown deletes them: `s3://launchpad-cn-evidence-123456789012/round-<n>/<run-id>/runtime-logs/<log-group>.jsonl`
(redacted JSON lines, `cn-northwest-1` profile — the only AWS write the suite makes outside the product API, plan §7).
`SCENARIO_LOG_EXPORT=0` skips it; `SCENARIO_LOG_EXPORT_HOURS` (6) and `SCENARIO_LOG_EXPORT_MAX_MB` (20, a `TRUNCATED` marker line
when hit) bound it; `SCENARIO_EVIDENCE_BUCKET` overrides the bucket. The exported groups are ledgered and attached as
`runtime-logs-export.json`. Hermetic helpers (`cn/log-export-core.ts`) are unit-tested with `npm run unit` (node:test, no AWS).

| step | spec | what it chains | ceiling |
|---|---|---|---|
| s00 | `s00-preflight` | fresh-environment preconditions (14 capabilities — 13 + `policy` since D20, workspace ready, provider ready, `hr-database` APPROVED, presets, Skill Lab) + the member identity | 10 min |
| s01 | `s01-knowledge` | HR KB from two policy fixtures → ingest ≤ 15 min → top-1 retrieval hit | 30 min |
| s02 | `s02-tools` | `hr-database` gateway target APPROVED, in `/api/tools`, attachable | 5 min |
| s03 | `s03-skill-lab` | approve `expense-report-writer` → taskgen → import task set → train → eval → publish (version bump, re-approved) | 45 + 55 + 50 min |
| s04 | `s04-build-agents` | payroll A2A specialist → Harness HR assistant (KB + gateway + skill + memory + Code Interpreter + A2A peer) → zip_runtime HR twin | 3 × 40 min |
| s05 | `s05-use` | 7 capability sessions, long-term recall, `/v1` API key, twin sessions, member ownership boundary | 45 + 25 + 20 + 30 + 15 min |
| s06 | `s06-observe` | first-trace lag ≤ 120 s, sessions, session details, worker preset observability | 30 min |
| s07 | `s07-evaluate` | evaluators → dataset FROM SESSIONS → sync/publish → batch runs (both agents) → SCORE NOW → online → insights | ≈ 3.5 h of ceilings |
| s08 | `s08-improve` | recommendation → ACCEPT → redeploy; A/B experiment → promote; Runtime canary → complete/rollback → cleanup | 60 + 90 + 90 min |
| s09 | `s09-go-live` | promoted versions on Chat + `/v1`, registry current, DEFAULT endpoint only, ops idempotency | 30 min |
| s11 | `s11-govern` | D20 Policy Phase 1: posture → editor drafts + VALIDATE + simulate → publish (log_only, audited `allow`) → enforce (typed confirmation; member 403 `policy.denied` + PolicyDenyCard, admin 200) → apply (redeploy, `policy_stale` cleared, `policy_coverage`) → restore; runs **before s10** (own project in `scenario.config.ts`) | 10 / 20 / 25 / 30 / 30 / 15 min |
| s12 | `s12-model-providers` | D21 model registry (admin only, API + page shots, no AWS): posture + member 403 → provider without a key (201, allowlist 422, duplicate 409, sanitised failed probe) → model rows (manual, nested params, default flips, disable/enable) → workspace flags → journal order + cursor paging → embedding reindex guard (409 with indices, or 200 on an empty substrate) → delete back to baseline; runs **after s11, before s10** | 7 × 10 min |
| s10 | `s10-lifecycle` | edit → redeploy (version +1) → delete → runtime/role/log group/S3/memory/A2A gone | 45 min |
| — | `scenario-cleanup` | model providers (`e2es-` rows + providers, defaults handed back) → policies (mode → log_only, retire + delete `e2es_`) → canaries → experiments → runs → online → datasets → evaluators → agents → KBs → records → Skill Lab → member; read-only AWS shows no `e2es` residue | 60 min |

## Failure classification (ENV / TEST / PRODUCT) — quality-pack Q5, 2026-10-17

Every failed or skipped step of a live run is classified **before** it is graded, from its raw error text (ledger line,
Playwright error, `run.errors`, API body) — never from the symptom alone. The class decides what happens next.

| class | what it is | how it is recognised | consequence |
|---|---|---|---|
| **ENV** | an environment failure outside the product under test: an **upstream transient** from the model provider (the `UPSTREAM_TRANSIENT_RE` set in `cn/helpers.ts`: `internalServerException`, `ServiceUnavailable`, `Throttling`, "Try your request again", "too many requests", read timeouts, HTTP 502–504), a **host update in progress** (deploy-ops window overlapping the run), **tunnel / DNS / network** loss to the console | the raw error text matches the set, or the ledger's timestamps fall inside a documented host-update / network window; the product itself answered correctly around it (status codes, `run.errors`, residue) | does **not** count against the §6 verdict when **≤ 2 per run** and each is evidenced by the raw error text in the ledger / REPORT; the step is re-run (resume) or the whole chain is repeated; **twice in a row on the same spec → quarantine** (below) |
| **TEST** | a defect of the harness: wrong selection / state (a step needs a predecessor the run excluded), a stale or too-strict assertion, a missing wait, a counting nit | the error is an `Error: scenario state … missing`, an assertion that contradicts a ruled product contract, a blank screenshot, a miscount | **must be fixed (test-side commit + register row) before the next counting run**; the run that hit it is a repair run, never counting |
| **PRODUCT** | anything else: a status code, state transition, residue, verdict shape or UI path that contradicts the documented contract | reproducible against the product API / console with the run's inputs; not explained by ENV or TEST | **opens a register row** (`docs/cn/reports/bugs-round-*.md`, severity + repro + file:line) and stops the run at the failing spec (lead's rule); the chain is re-run after the fix |

Rules of thumb: an ENV failure that the product *surfaces correctly* (`run.errors`, an error row, a 5xx relayed with its
text) stays ENV — the product follow-up, if any, is a *resilience* candidate (e.g. a retry), not a bug. A TEST failure is
owned by the runner who wrote the selection or the assertion and is never "fixed" by loosening an assertion the lead has not
ruled on. When two classes seem to apply, the raw error text wins; when it is missing, the failure is PRODUCT until proven
otherwise.

**Quarantine.** A spec that fails ENV twice in a row is listed in [`cn/QUARANTINE.md`](cn/QUARANTINE.md) with its id, first /
last date, evidence link and an **exit criterion** (normally: passes in two consecutive runs after the mitigation that
addresses the transient). A quarantined spec still runs; its ENV failure is annotated `observation` and does not grade the
chain until it exits quarantine. Only the lead adds or removes entries.

### Worked examples — 2026-10-16 on t1

| when / step | raw error text (abridged) | class | why | follow-up |
|---|---|---|---|---|
| 02:13Z storyline run 1, **s05b** recall Chat turn | `RuntimeError: runtime returned error: An error occurred (internalServerException) when calling the ConverseStream operation: The system encountered an unexpected error during processing. Try your request again.` | **ENV** (upstream transient, Bedrock) | memory itself worked (8 records at 0 s incl. both facts); the runtime relayed the provider's 5xx faithfully; deploy-ops saw the same error on default at ≈ 02:05Z | TEST hardening: `chatOnce` retries once (N-SCN-obs18); s05c–e cascade re-run in storyline-2 (all green) |
| 09:44Z storyline-2, **s07c** twin batch run, GLM judge call | `EventStreamError: An error occurred (internalServerException) when calling the ConverseStream operation…` on 1 of 27 records; `run.errors {count 1, total 27}`, run `completed` | **ENV** | the evaluation worker surfaced the error per record and kept the other 26 scores; the hard `errors === 0` was a TEST strictness (N-SCN-obs21) | obs21 bounded soft-fail; product resilience candidate N12-obs53 (judge-call retry, r12-f) |
| 12:40Z s07-1 repair run, **s07c** | `Error: scenario state 'runtime' missing — run s04 first` | **TEST** (selection error, e2e-r11) | the selection excluded s04c although s07c replays against the twin | corrected selection (s04c included); run not counting |
| 13:29Z s07-2 repair run, **s07c** HR batch run, agent replay | `scenario trace-c09932a5… failed after 1/3 attempts: RuntimeError: runtime returned error: An error occurred (internalServerException) when calling the ConverseStream operation…` — 1 of 7 sessions → 4 / 28 records | **ENV** (second in a row on s07c → **quarantine**) | the worker recorded the failed session per evaluator and completed the run; the first obs21 bounds (≤ 10 %, ≥ 90 %) could not absorb one session on a 7-item dataset | obs21 small-dataset bounds; product resilience candidate N12-obs55 (replay retry, r12-f); s07c quarantined until two clean consecutive runs |

Counting rule applied: storyline run 1 and storyline-2 each carried **one** ENV failure (≤ 2) with its raw text in the
ledger, so neither is a PRODUCT failure; both stay **FAIL by §6** only because the ENV failure cascaded through a serial
file and left steps unexercised — the chain must still be repeated to count.

## Control crawler (`cn/controls/`, round 16 r16-m) — "every button works"

`frontend/src/v2/__inventory__/controls.json` is the committed inventory of every interactive
control the V2 console renders (generated by `frontend/scripts/control-inventory.mjs` from the
TSX; `control-inventory.test.ts` in `make verify` fails on drift — regenerate with
`node scripts/control-inventory.mjs --write`). `cn/controls/crawl.mjs` drives a **live** console
through that inventory: for every control × role it navigates to the page (+ `?view=` + fixture
id), asserts presence / role-correct absence, clicks, and waits ≤ 5 s for the inventory's effect
(an `/api` call on the route pattern, a URL change, a dialog by testId, or a DOM delta), recording
network and console / page errors. Destructive controls stop at their Confirm, which is cancelled;
direct mutations fire only on the crawler's own `x16m-` fixtures. Misses are classified DEAD /
BROKEN / MISLABELED / DISABLED-NO-HINT / ROLE-LEAK / FLAKY (see the report legend). Not part of
`make verify`.

```bash
export LAUNCHPAD_E2E_BASE_URL="https://<host>:8443"
export LAUNCHPAD_E2E_PASSWORD="$(aws ssm get-parameter --name launchpad-console-password \
  --with-decryption --query Parameter.Value --output text --profile cn-northwest-1 --region cn-northwest-1)"
export PLAYWRIGHT_MODULE=/path/to/node_modules/playwright     # default: tests/e2e/node_modules/playwright
export CRAWL_OUT_DIR=/tmp/controls-run CRAWL_SHOTS=/tmp/controls-run/shots

node cn/controls/crawl.mjs --fixtures=create --deploy --roles=admin,member   # full run (~2 h; agents/chat pages last so the harness deploy finishes)
node cn/controls/crawl.mjs --fixtures=keep --roles=admin --pages=/v2/eval/evaluators --limit=12   # smoke
node cn/controls/crawl.mjs --resume ...                                        # continue an interrupted run (JSONL)
node cn/controls/crawl.mjs --fixtures=delete-only                              # teardown sweep by the x16m- prefix (exit 1 if anything is left)
node cn/controls/report.mjs $CRAWL_OUT_DIR/crawl-<YYYYMMDD>.json              # → docs/cn/reports/control-crawl-<date>.md + frontend/src/v2/__inventory__/crawl-<stamp>.json
```

Fixtures (all `x16m-` prefixed, deleted at the end of the run and verified by a prefix sweep):
one member account (`x16m-member`, registered through the public form and approved on the default
workspace; its password lives only in process memory), one dataset, one derived evaluator, one
knowledge base, one memory store, one announcement draft (never published), one MCP registry
record, and — with `--deploy` — one small Harness agent (otherwise the first active agent is
reused for the read-only agent controls). The password is never written: no storageState on
disk, every stored API body is redacted, and a screenshot is refused while a password field
holds text.
