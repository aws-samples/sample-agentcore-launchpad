# Iteration log

One `## <UTC timestamp> — <SE-ID>` entry per supervised direction: child session, commits, host-rerun checks, cost, outcome.

## 2026-09-04 — SE-001 Catch-all route: unknown URLs render a not-found view inside the shell
- Branch `evo/se-001-catch-all-route-unknown-urls-render-a-no` · commits 98ea489, 27cc24e · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/72
- Child session 6f050b9d-819f-4c5a-a055-6c3b082e506a · attempts 2 (start $2.46 / 16 turns / 617 s; resume = one correction: `<Btn>` nested in `<Link>` → `<Link className="btn primary">`)
- Host rerun: `direction.sh verify` → make verify PASS; Playwright on worktree vite :5197 → `/nonexistent-route` + `/evaluation/old-subroute` render shell + not-found panel, crumb `CONSOLE / NOT FOUND` / `控制台 / 页面不存在`, back link → `/`, `/registry?view=register` crumb unchanged; tsc + lint re-run after the correction.
- Outcome: in-review (PR open, no live check declared). Note: `ROUTE_PATHS` in `layout/nav.ts` mirrors the route table — documented in architecture.md.

## 2026-09-04 — SE-002 Unreachable backend: health chip reflects reality and list pages show load errors, not empty states
- Branch `evo/se-002-unreachable-backend-health-chip-reflects` · commit 60b2a25 · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/73
- Child session 9d21c85a-7eb5-4344-a6fe-dad8655d8bac · attempts 2 (start $5.50 / 43 turns / 652 s — ended its turn waiting on a BACKGROUND make verify with everything uncommitted; resume $3.31 / 8 turns / 494 s finished, committed, captured screenshots). Total $8.82. Not an acceptance failure. Lesson for the brief: tell the child to never background make verify.
- Host rerun: `direction.sh verify` → make verify PASS; own Playwright dead-API/live check (`runs/SE-002/host_*.png`) PASS on every acceptance item incl. Retry request count.
- Outcome: in-review (PR open, no live check declared). Deviation accepted: Overview's other tiles/health rows also show "failed to load"; Registry network failure no longer routes to the "REGISTRY UNAVAILABLE" page. Known nit: chip is red for the sub-100 ms before the first probe answers.

## 2026-09-04 — SE-003 Stale "Phase" copy and unlabeled form controls
- Branch `evo/se-003-stale-phase-copy-and-unlabeled-form-cont` · commit eaf409f · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/74
- Child session 0e9b7f8c-3df3-4c9a-b94a-11dfca355b17 · attempts 1 ($1.81 / 15 turns / 913 s), no correction needed.
- Host rerun: `direction.sh verify` → make verify PASS; own probe (`/tmp/se3_check.mjs`) 30 routes × 2 locales → 0 unlabeled controls; grep for PHASE 9/phase 7 empty; protected keys have no hunk.
- Outcome: in-review (PR open, no live check declared).

## 2026-09-04 — SE-004 Small viewports: no horizontal page scroll below 720 px (run 2, --no-research)
- Branch `evo/se-004-small-viewports-no-horizontal-page-scrol` · commit 986616b · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/75
- Child session 22574d81-e8a9-4501-a689-f27e1d710673 · attempts 1 ($5.15 / 53 turns / 2556 s), no correction needed. Child found two extra facts: /create's residual overflow was the 3-step stepper, not the table; /memory?view=short-term also overflowed (fixed by the same grid rule).
- Host rerun: `direction.sh verify` → make verify PASS; own probe (`/tmp/se4_check.mjs`, worktree vite with a temporary `server.fs.allow` so @fontsource woffs are served — 0 font 403s) → 0 overflow failures on 30 routes at 390 and 1440; `host_narrow_{users,chat}.png` inspected.
- Outcome: in-review (PR open). Follow-up when #73 and this both merge: add `className="table-scroll"` to #73's third DataTable branch.
- Trap: `pkill -f "<pattern>"` in a host Bash whose command line also contains the pattern kills the host shell (exit 144) — use `fuser -k <port>/tcp`.

## 2026-09-04 — run 3 (--auto-merge): landed the four in-review PRs
- #72 → 84ebdc6, #73 → b1fd561, #74 → 7f2ebe5 squash-merged clean. #75 turned CONFLICTING after #73 (both appended an architecture.md section at the same anchor; DataTable overlap auto-merged). Host merged `main` into the SE-004 branch in a temp worktree (kept both doc sections, wrapped #73's DataTable error branch in `.table-scroll`), `make verify` PASS, pushed (no force), squash-merged. SE-001..SE-004 set `done`.
- Trap: run `git pull --ff-only` from the MAIN checkout, not from a direction worktree (its branch diverges, the chain aborts).

## 2026-09-04 — SE-005 AWS ClientErrors on console routes map to 4xx envelopes (run 3, --auto-merge)
- Branch `evo/se-005-aws-clienterrors-on-console-routes-map-t` · commits a6d61c2, aff9095 · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/76 → squash-merged 3692638
- Child session 829ac2f4-432c-4c21-9907-2a0b52c9382f · attempts 2 (start $4.05 / 31 turns / 1609 s; resume $2.39 / 4 turns / 438 s = the one correction: `/v1` responses must not carry the raw AWS message — it names the deployment's role ARN + instance id — generic per-code message + `detail={aws_error_code}` only, with a hermetic test). Total $6.44.
- Host rerun: `direction.sh verify` → make verify PASS; own throwaway backend :8012 on the branch → registry 400 `aws.validation`, KB 403 `aws.access_denied`, memory actor 404 `aws.not_found`, eval run still 404 `run.not_found`; `test_errors_aws.py` 26 passed after the correction.
- Outcome: done (merged). Follow-on: 13 scattered `ResourceNotFoundException` mappings in services could now be simplified (review path).

## 2026-09-04 — SE-006 Disabled primary actions explain what is missing (run 3, --auto-merge)
- Branch `evo/se-006-disabled-primary-actions-explain-what-is` · commits f8c482d, 54b0247 · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/77 → squash-merged e9cf06c
- Child session 0e0eb7fd-4230-464a-9b2f-f351cc644d09 · attempts 2 (start $3.17 / 22 turns / 770 s; resume $1.96 / 5 turns — the one correction: it claimed docs/architecture.zh-CN.md does not exist and skipped the twin). Total $5.13.
- Host rerun: `direction.sh verify` → make verify PASS; own Playwright probe (`/tmp/se6_check.mjs`) six forms × en/zh-CN → every disabled primary has title + visible aria-describedby hint; Register enables after valid input. Child left a 44ch right-aligned hint before the button in DOM order.
- Outcome: done (merged). Trap: children may assert a file does not exist without `ls` — the brief should name twin paths explicitly.

## 2026-09-04 — SE-007 Stale deep links tell the user the resource is gone (run 3, --auto-merge)
- Branch `evo/se-007-stale-deep-links-tell-the-user-the-resou` · commit 89691de · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/78 → squash-merged 8374ab2
- Child session f7ff069d-c886-4f1f-af3b-2e5419e9f94a · attempts 1 ($4.09 / 25 turns / 958 s), no correction.
- Host rerun: `direction.sh verify` → make verify PASS; own probe (`/tmp/se7_check.mjs`) → 7 stale URLs show StaleLink + param stripped (chat also drops session, agent-select ""), valid ds/agent deep links keep param, no notice, agent selected.
- Outcome: done (merged). Run cap (3) reached; SE-008 (zh-CN punctuation, score 11) is the leftover.

## 2026-09-05 — SE-008 zh-CN typography: full-width punctuation (run 4, --no-research --auto-merge)
- Branch `evo/se-008-zh-cn-typography-full-width-punctuation-` · commits e4d7061, 00259f7 · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/79 → squash-merged 97ded04
- Child session d8f09de6-8d0c-4967-a587-7a3bdee24692 · attempts 1 ($4.08 / 22 turns / 875 s), no correction.
- Host rerun: `direction.sh verify` → make verify PASS (new `i18n_zh_punct` step green); own character-level diff of zh-CN: 182 keys changed, every change one of the 7 marks, key set + placeholders identical, en untouched; residual half-width marks are code fragments (`get_config_bundle()`, `http(s)`). The research count (351) was an over-count from a broad lookahead regex; the child's 181/182 is right.
- Housekeeping: removed a stray empty `agentcore_launchpad-worktrees/evo-se-002/frontend` dir left by the SE-002 child's vite (2 cache files, not a git worktree).
- Outcome: done (merged). Backlog exhausted.

## 2026-09-05 — SE-009 Evaluation runs can be stopped (StopBatchEvaluation + queued cancel)
- Branch `evo/se-009-evaluation-runs-can-be-stopped-stopbatch` · commit 76eded7 · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/80 (open, no auto-merge)
- Child session 8a191895-3a62-47c6-b46c-87def3c158f6 · attempts 1 ($5.07 / 27 turns / 1038 s), no correction.
- Host rerun: `direction.sh verify` → make verify PASS; `pytest -k "eval and stop"` 14 passed; route_policy + client_funnel 410 passed; diff read against every check (stop wrapper in agentcore_eval.py, queue cancel, replay-abort flag, STOPPED→`stopped` with partial scores, 202 route under `/api/eval` (brief said `/api/evaluation` — child corrected), STOP button + chip + confirm in Evaluation.tsx, en/zh-CN keys, api+architecture docs incl. zh-CN twins).
- Deviations accepted: `stop_requested` flag in-memory only; telemetry wait not interrupted mid-wait; frontend not browser-exercised.
- Live check not run (declared not required). Outcome: in-review.

## 2026-09-05 — SE-010 Agent detail lists AWS versions and endpoints (Runtime + Harness)
- Branch `evo/se-010-agent-detail-lists-aws-versions-and-endp` · commit e98eaf7 · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/81 (open, no auto-merge)
- Child session 464fb74f-88a1-41c3-878e-27a652e15257 · attempts 1 ($6.40 / 53 turns / 1103 s), no correction.
- Host rerun: `direction.sh verify` → make verify PASS; `pytest -k versions` 16 passed; route_policy + client_funnel pass; diff read: paginating list wrappers in agentcore/runtime.py + harness.py, new `services/agent_versions.py` (allow-listed projection, 409 agent.no_resource, resource-kind resolution incl. discovered rows), GET route MEMBER in route_policy, `VersionsPanel.tsx` (loading/LoadError/no_resource/empty, DEFAULT + CANARY tags, ledger-vs-latest MISMATCH), en/zh-CN keys, api + architecture docs with zh-CN twins; screenshots panel-en-ready.png etc. reviewed.
- Deviations accepted: extra `resource_id` + `canary_endpoints` fields; deleted agents → 409 not 404; no checked-in frontend test (repo has no RTL), Playwright probe artifacts in the runs dir instead.
- Live check not run (declared not required). Outcome: in-review.

## 2026-09-05 — SE-011 Chat can end the AgentCore Runtime session (StopRuntimeSession)
- Branch `evo/se-011-chat-can-end-the-agentcore-runtime-sessi` · commits ebc3ec6, d9c0f89, 9e7cb5e · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/82 (open, no auto-merge; custom pr-body.md with reviewer notes)
- Child session bccf76bc-2cd8-40de-a4ed-9dafcd938465 · attempts 1 ($6.47 / 52 turns / 1079 s), no correction.
- Host rerun: `direction.sh verify` → make verify PASS (infra included); `pytest -k "session and stop"` 17 passed; route_policy + client_funnel pass; diff read: `stop_runtime_session` wrapper (data plane) in agentcore/runtime.py, `stop_agent_session` dispatch in invoke.py (harness/discovered-harness → 409 chat.session_stop_unsupported, RNF → already_ended), route MEMBER, `ChatSession.ended_at` + additive ALTER in db.py, revive-on-new-turn, END SESSION + per-row action in Chat.tsx with disabledReason, en/zh-CN, api + architecture docs with twins.
- Flagged (not corrected): `bedrock-agentcore:StopRuntimeSession` added to the shared execution role's ABTestOrchestration statement (base_stack.py + workspace_iam.py) — unnecessary for the console (instance role / spoke `bedrock-agentcore:*`); left for the reviewer to strike or keep. `RetryableConflictException` added to AWS_ERROR_MAP (brief wrongly said it existed).
- Live check not run (declared not required). Outcome: in-review. Run cap (3) reached → halt.

## 2026-09-05 — SE-012 Memory resources: edit description and event expiry (UpdateMemory)
- Branch `evo/se-012-memory-resources-edit-description-and-ev` · commit bfce69a · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/83 (open, no auto-merge)
- Child session f97611b0-d7a9-420b-b323-aa25d0ce649f · attempts 1 ($5.56 / 27 turns / 1056 s), no correction.
- Host rerun: `direction.sh verify` → make verify PASS; memory_resources + memory_console + route_policy + client_funnel 486 passed; diff read: `UpdateMemoryResourceRequest` (≥1 field, expiry 7–365, description 1–4096), PUT route MEMBER, `memory_admin.update_memory_resource` sends exactly memoryId + changed fields (namespaceKeys never echoed, fallback documented) then GetMemory read-back, `_guard` now passes mapped ClientErrors to the global handler (aligned with routers/memory.py — GET/DELETE unknown ids now 404 not 502), inline EDIT form + confirm dialog listing agents in ResourcesTab.tsx, en/zh-CN, api + architecture docs with twins (zh-CN gained the missing resources row/section).
- Noted, not corrected: model min expiry is 3 (create form copy says 3–365; edit enforces 7–365 per the brief) — small copy inconsistency for a follow-up.
- Live check not run (declared not required). Outcome: in-review.

## 2026-09-05 — SE-013 Governance gateway detail manages Gateway rate limits
- Branch `evo/se-013-governance-gateway-detail-manages-gatewa` · commits 61bb37b, 16a5327, 8bfb639 · PR https://github.com/aws-samples/sample-agentcore-launchpad/pull/84 (open, no auto-merge)
- Child session 746b31e0-dcde-4057-b74f-f10ea3c4688e · attempts 1 ($8.81 / 61 turns / 1407 s, budget cap $40), no correction.
- Host rerun: `direction.sh verify` → make verify PASS; `pytest -k rate_limit` 37 passed; route_policy + client_funnel 416 passed; diff read: five wrappers in agentcore/policy.py (paginated list, get, create w/ clientToken, update, delete), `validate_rate_limit_spec` (all documented rules → 422 governance.rate_limit_invalid + detail.reason), `_require_managed` gate, inline `PolicyChange` journaling (running → succeeded/failed) for rate_limit.create/update/delete, four MEMBER routes with a dot-tolerant RATE_LIMIT_ID pattern, `RateLimitsPanel.tsx` + `rateLimits.ts` (client-side trailing-* and period checks, disabledReason), en/zh-CN, api + architecture docs with twins; screenshots ui-form/ui-list/ui-error reviewed.
- Deviations accepted: DELETE 200 with body (not 204); JWT claim names follow botocore pattern (no `custom:tenant`); two extra reasons (entry_dimension_empty, rate_config_count); update reads GetGatewayRateLimit for `before` + key set with entry-derived fallback; not blocked by in-flight policy operations.
- Live check not run (declared not required). Outcome: in-review. Backlog exhausted + run cap (2) → halt.
