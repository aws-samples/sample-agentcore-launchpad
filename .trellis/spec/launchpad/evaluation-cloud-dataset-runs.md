# Evaluation — AWS cloud datasets & simulated personas as run scopes

## Scenario: New Run / Insights can select cloud datasets, incl. persona simulation

### 1. Scope / Trigger

Contract between `backend/app/evaluation/routers.py` (`_cloud_dataset_items`,
`get_cloud_dataset`, `create_run`), `backend/app/evaluation/simulation.py`
(SDK executor adapter) and the New Run dialog in
`frontend/src/pages/Evaluation.tsx`. Touch this when changing dataset schemas,
run scopes, the runs-list scope encoding, or the actor-simulation loop.
Introduced 2026-07-13 (fix: cloud-only datasets — e.g. the HR sample pair —
were invisible to New Run, which listed `/api/eval/datasets` only; simulated
personas added the same day via the SDK dataset runner pattern).

**Load-bearing facts (live-probed 2026-07-13):**
- `StartBatchEvaluation` has NO dataset data source (`dataSourceConfig` is
  cloudWatchLogs | onlineEvaluationConfigSource only) — a dataset run must be
  driven locally: fetch examples, run them against the agent, then batch-
  evaluate the produced sessions. AWS cannot drive the agent from a dataset id.
- `ListDatasetExamples(datasetId)` (control plane, paginated via nextToken)
  returns examples verbatim in the schema they were created with, each with a
  service-assigned `exampleId` on top. Strip it → valid run items.
- Simulated personas (`AGENTCORE_EVALUATION_SIMULATED_V1`: items carry
  `actor_profile{traits,context,goal}`, `input`, `max_turns`, no `turns`) run
  through the SDK's `SimulatedScenarioExecutor` (preview): an LLM actor plays
  the user until a goal-completion tool signals stop or max_turns. Needs the
  `bedrock-agentcore[simulation]` extra (strands-agents-evals) — in
  backend deps since 2026-07-13 — and a per-run `actor_model_id`; the strands
  actor Agent resolves the Bedrock region from the default boto session
  (matches settings.region here).
- The executor generates a framework session id (`{scenario_id}-{uuid}`) —
  it is ONLY a conversation key. The adapter invokes through the platform's
  own invokers and records the RUNTIME session id (what telemetry carries and
  what StartBatchEvaluation must be scoped to).
- The executor swallows exceptions into `result.status == "FAILED"` — the
  adapter re-raises RuntimeError so execute_run fails the run honestly.
- Fresh dataset replay does not blindly sleep before `StartBatchEvaluation`.
  The final session is the sequential-replay ingestion watermark:
  `evaluation.telemetry.wait_for_evaluation_telemetry` polls `aws/spans` for
  its latest `invoke_agent Strands Agents` root span, pairs that span id with a
  structured `body.input`/`body.output` event in the resolved Runtime/Harness
  content-log group, and requires the content event's CloudWatch ingestion age
  to reach `wait_seconds` (default/UI: 180 seconds). AgentCore Evaluation has
  no direct index-readiness API, so ingestion age is the conservative
  readiness proxy. Both CloudWatch queries use the current replay's start time
  minus a 60-second clock-skew allowance as `startTime`; omitting the bound
  makes `aws/spans` scan the full account history. Polling is bounded to that
  age plus 120 seconds; timeout fails the run before creating a paid batch.
  Existing-session and time-window scopes skip this fresh-telemetry gate.

**Code-evaluator Lambda identity drift (live-probed 2026-09-14):**
`Evaluate` sends `{schemaVersion, evaluationLevel, evaluationInput, evaluationTarget}`
without the documented `evaluatorName` or `evaluatorId`. Batch run `a49e4e439f6c`
failed all 19 sessions because the packaged handler dispatched only by name (the
other five judges succeeded). The static handler may resolve an identityless event
ONLY when `rules.json.evaluators` contains exactly one evaluator. Explicit unknown
or malformed identities and identityless multi-evaluator packages remain errors;
never pick the first of several rule sets or infer identity from an ID prefix.
Existing evaluators pin a published Lambda version: local code changes do not
repair them. Publish the unchanged rules/provenance with the fixed handler, update
the evaluator binding and exact-version invocation grants, and preserve reviewed
ownership snapshots plus append-only repair evidence. Re-score the original
sessions instead of replaying the agent; keep the failed run as historical evidence.

**Batch log envelope (live-probed 2026-09-14):** the initial diagnostic inspected
only top-level `body` / `events` and incorrectly concluded output was absent. Full
structural probe `probe_a49e4e_envelope-a8e56d51fa` proved each Strands span carries
its ADOT output log in `span_events[].body.output.messages`, with `span_id`,
`time_unix_nano` and `observed_time_unix_nano`; the parent provides the trace id.
The span itself carries both camelCase and snake_case identity fields plus
`startTimeUnixNano` / `endTimeUnixNano`. Normalize embedded logs before grouping,
reject explicit identity/timestamp conflicts and malformed log collections, then
reuse the existing duplicate, truncation, session, current-output and finish guards.
No cloud evidence fetch or relaxed scoring is required. Raw CloudWatch also includes
nested botocore chat spans that this batch envelope filters out; folding those spans
is not the fix. Regression cases live in `tests/test_evaluation_handler_evidence.py`.

Live acceptance: Lambda version `:8`, recheck run `9e315699400f` / AWS batch
`run_9e315699-33769d5a0e` completed all 19 original sessions, zero failures;
all 20 `child_poc_r2_no_tools` trace results carry `PASS` / `1.0` in CloudWatch.
AWS returns an empty `statistics` object for this code evaluator even on success;
its absence from the run's average-score list is not a failed/missing judgement.
Read the per-session result records for the authoritative code-evaluator scores.




### 2. Signatures

```python
# simulation.py
is_simulated(scenario) -> bool          # "actor_profile" in item
run_simulated_scenario(data_client, *, agent_arn, method, scenario,
                       actor_model_id) -> runtime_session_id
# routers.py
RUNNABLE_CLOUD_SCHEMAS = {PREDEFINED_V1, SIMULATED_V1}
_cloud_dataset_items(cloud_id) -> (display_name, items)
#   400 dataset.cloud_not_active | 422 run.cloud_dataset_unsupported (unknown
#   schema) | 422 dataset.empty; strips exampleId; _validate_items() after
GET /api/eval/datasets/cloud/{cloud_id}
#   {datasetId, name, status, schemaType, exampleCount, runnable,
#    has_ground_truth} — persona assertions count as ground truth
RunCreate.cloud_dataset_id   # same XOR slot as dataset_id; both → 422
RunCreate.actor_model_id     # REQUIRED iff items carry actor_profile
#   (local or cloud) → else 422 run.actor_model_required
# run row: dataset_id = cloud dataset id, dataset_name = f"cloud:{name}"
#   (scope encoding like "window:{N}h"; frontend scopeLabel renders "☁ name")
# execute_run: per-scenario dispatch — is_simulated → actor loop, else turn
#   replay; ground_truth_metadata includes persona assertions (turns loop is
#   empty for them). _validate_items accepts turns | actor_profile | prompt
#   shapes; _infer_kind: actor_profile → "simulated" (sync-to-aws picks the
#   schema by kind via DATASET_SCHEMA_TYPES).
```

Frontend: dropdown = optgroup LOCAL + AWS CLOUD; cloud option value is
`"cloud:" + datasetId`, disabled only when not ACTIVE; simulated ones get a
"personas" tag. Selecting a simulated dataset (cloud schemaType or local
`kind === "simulated"`) reveals the ACTOR MODEL select (`ACTOR_MODELS`
curated us-west-2 list, default haiku-4-5) and the submit payload adds
`actor_model_id`. Trajectory* gating for a cloud selection fetches
`GET /datasets/cloud/{id}` lazily and caches `has_ground_truth` per id.

**Observability transcript for eval sessions** (`observability.py
session_transcript`): eval-run sessions have no ChatSession ledger row — the
transcript falls back to `_eval_run_for_session` (membership scan over recent
EvalRun.session_ids; insights re-runs REUSE session ids, so the OLDEST match
is the creator — its created_at anchors the log window). Two data sources:
1. Memory events under the BARE `"default"` actor (eval invokers pass it
   straight to the runtime; `memory.scoped_actor` applies only to chat
   writes). Harness runtimes auto-persist the conversation there.
2. Runtime-backed agents (zip/studio/container) write no memory events —
   `eval_turns_from_content_logs` rebuilds turns from the runtime log group's
   `otel-rt-logs` stream (per-span records: `attributes."session.id"`,
   `traceId`, `body.input/output.messages` with polymorphic content — plain
   str or JSON-encoded [{"text"|"toolUse"|"toolResult"}] parts). One trace =
   one invocation → USER = latest input user message, ASSISTANT = `end_turn`
   output (fallback last assistant text). `filter_log_events` scans
   OLDEST-first: passing startTime (run created_at − 10 min) is load-bearing,
   without it the page budget dies in old log data and returns nothing.
   Works even for deleted agents (log groups outlive runtimes).
Response carries `source: "chat"|"eval"`, `origin: "memory"|"logs"`,
`run_id`; long_term_records is chat-only (the shared default actor's
namespaces aggregate across all agents). Frontend hides OPEN IN CHAT for
eval sources and picks `conversationEvalSub` / `conversationEvalLogsSub` by
origin.

### 3. Wrong vs Correct

```python
# WRONG: treating the cloud dataset as an AWS-side run scope
start_batch_evaluation(..., dataset_id=cloud_id)   # no such data source
# CORRECT: ListDatasetExamples → strip exampleId → run as ordinary items

# WRONG: recording the executor's framework session id for evaluation scoping
session_ids.append(result.session_id)   # telemetry never saw this id
# CORRECT: capture the invoker's returned runtime session id (state dict)

# WRONG: trusting executor status implicitly
return state["session_id"]              # FAILED runs would score nothing
# CORRECT: result.status != "COMPLETED" → raise RuntimeError
```

### 4. Tests

`tests/evaluation/test_simulation.py` (adapter: session threading, harness
dispatch, field mapping, FAILED re-raise, actor-model guard; plumbing:
validate/kind/normalize/ground-truth for persona items) and
`tests/evaluation/test_datasets_v2.py::test_run_on_cloud_dataset`,
`::test_run_on_simulated_dataset_requires_actor_model`,
`::test_run_on_simulated_cloud_dataset_with_actor_model`,
`::test_run_rejects_local_plus_cloud_dataset`,
`::test_cloud_dataset_detail_*`. Live-proven 2026-07-13: predefined run
`2faaa172b136` and simulated-persona run `3d01febf4ea1` (hr-assistant harness,
actor haiku-4-5) both submitted through the UI dropdown.

## Partially failed batches (`session_failures`, 2026-10-09)

- A COMPLETED_WITH_ERRORS batch stores `eval_runs.session_failures` =
  `{total, failed, kind, sessions:[{session_id, index, kind, error_type, message}]}`
  (`agentcore_eval.summarize_session_failures`). A session is `telemetry_incomplete`
  only when EVERY error on it is `LogEventMissingException` / `NO_SPANS` / "span data is
  incomplete"; run kind folds to telemetry / evaluator_error / mixed / unknown.
- Live cause (prod Harness, 2026-10-09): whole event-loop cycles of runtime
  `otel-rt-logs` events never arrive while their spans are in `aws/spans`; permanent,
  skews to long tool-heavy sessions — re-evaluating the same batch cannot recover it.
- Legacy rows backfill lazily: `run_results` (detail page read, no extra AWS call) or
  `recheck_run` (accepts completed+error+no summary; refuses a non-terminal batch so a
  finished run never returns to polling).
- Console rule (`runRecommendable`): a run seeds step-3 recommendations when clean, or
  telemetry-only with scored ≥ total/2. Backend `eligibility()` stays permissive.
- UI: shared `v2/RunCoverage.tsx` (tag + note); task detail honours `?outcome=error`.
