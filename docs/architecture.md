# Architecture / 架构

AgentCore Launchpad is a thin, opinionated platform layer over Amazon Bedrock
AgentCore. Every feature in the console maps to a real AgentCore service and a
real resource in your account — the platform's job is to give those services a
unified create → deploy → invoke → observe experience, not to reimplement them.

中文版: [architecture.zh-CN.md](architecture.zh-CN.md)

## System diagram

```
 Browser
 ┌─────────────────────────────┐
 │ Platform console  :5173     │
 │  Overview · Create (incl.   │
 │  Studio canvas) · Chat ·    │
 │  Registry · Governance ·    │
 │  Evaluation                 │
 └──────────────┬──────────────┘
                │ /api  /v1
                ▼
 ┌─────────────────────────────┐
 │ Platform backend  :8000     │
 │  FastAPI                    │
 │  · deploy pipeline          │
 │  · invoke chain (/api,/v1)  │
 │  · SQLite ledger (data/)    │
 └──────────────┬──────────────┘
                │ boto3 (bedrock-agentcore control + data planes)
                ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ AWS · us-west-2                                                 │
 │  AgentCore: Runtime · Harness · Memory · Gateway · Identity ·   │
 │             Registry · Policy(Cedar) · Evaluation/Optimization  │
 │  Shared infra (CDK launchpad-base): S3 · ECR · CodeBuild ·      │
 │             Cognito · IAM exec role · HR Lambda · Facts API     │
 │  Observability: CloudWatch Logs (legacy + per-agent unified)     │
 └───────────────────────────────────────────────────────────────┘
```

## Overview announcements

The overview announcement panel replaces the static lab-guide banner.
`/announcements` provides administrator-only draft editing, publication,
withdrawal, and deletion. The hub-global `announcements` ledger table stores
separate editable and published snapshots with optimistic revisions; ordinary
members see only the published projection through `GET /api/announcements`.
The central route-policy table marks management endpoints `ADMIN` and exempts
all announcement routes from workspace resolution. These are platform-authored
messages, not AWS resource state. No content is published on startup or reads.
See [Announcements](announcements.md) for the lifecycle and explicit initial
publication workflow.

## Shared tutorial videos

The console's **Learn → Videos** page (`/videos`, `/v2/videos`) reads the
published, hub-global directory from `GET /api/videos`. First-level areas and
second-level modules are fixed to the V2 navigation in
`backend/app/data/video_sections.json`. Every recording declares the console
version it shows (`v2` or `classic`). The V2 and classic libraries default to
their matching version, offer both versions and an All view, and separate
mixed-version module playlists; filters use `version`, `category`, `section`,
and `q`.
`/videos?video=<id>` and the V2 equivalent keep stable video IDs and chapter
navigation. The admin-only **Configuration → Video management**
(`/v2/video-management`) saves drafts and explicitly publishes or withdraws
each video. A one-time database import preserves the previously bundled
`backend/app/data/videos.initial.json` entries; later changes come from the ledger,
so publishing metadata does not rebuild the frontend. Video bytes still play
directly from the browser's CDN URL, independent of workspace and AgentCore.
Every V2 module page also offers its own recordings: each published collection
carries its module's sidebar `path`, and the V2 shell adds a **Demo videos**
button to the page header of any module with V2 recordings, playing them in a
drawer. A module that became a tab of another entry keeps its section: the 在线评估
recordings (`/v2/eval/online`) play on 评估任务's 持续评估 tab and the online config
sub-pages, which the entry maps onto that path (`videoPath` in `v2/nav.ts`).
The separate `launchpad-videos` CDK stack retains its private S3 origin and
OAC-restricted CloudFront distribution. See [Video library](video-library.md)
for management and immutable media publication.

## The four-layer mapping (from prompt.md)

The brief organizes AgentCore capabilities into four layers; each is backed by
real, runnable code in this repo.

| Layer | Platform surface | AgentCore services |
|---|---|---|
| **1. Build Core** | Create Agent (方式A/B/C), unified pipeline, Chat memory | Runtime, Harness, Memory |
| **2. Build Tools** | Tool catalog, builtin-tool demos | Gateway (REST + Lambda → MCP), Builtin Tools (Code Interpreter, Browser) |
| **3. Governance** | Governance page, Registry console, trace rail | Observability (Transaction Search), Registry, Policy (Cedar) |
| **4. Evaluation & Optimization** | Evaluation page, Experiments (`?view=experiment` sub-page: stage pipeline + verdict semantics) | Evaluation (batch + online, LLM-judge, insights), Optimization (config bundles, A/B, canary) |

## Platform ↔ AgentCore service mapping

| AgentCore service | How the platform uses it |
|---|---|
| **Runtime** | Hosts zip and container agents (`CreateAgentRuntime`); the invoke chain calls the runtime data plane. Agent Management can also scan every `ListAgentRuntimes` page, inspect each resource with `GetAgentRuntime`, and explicitly import HTTP/A2A runtimes as externally owned ledger entries without changing the AWS resource. The agent detail's read-only VERSIONS & ENDPOINTS panel reads every `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints` page back (`GET /api/agents/{id}/versions`) so the operator sees the immutable versions, the `DEFAULT` endpoint, and any pinned named endpoints. |
| **Harness** | Hosts 方式B agents (`CreateHarness`) — a managed entrypoint with no build artifact. The same VERSIONS & ENDPOINTS panel reads `ListHarnessVersions` + `ListHarnessEndpoints` for harness-backed agents. |
| **Memory** | One shared `launchpad_memory` singleton: short-term session events + four long-term strategies — semantic facts (`/facts/{actorId}`), user preferences (`/preferences/{actorId}`), per-session summaries (`/summaries/{actorId}/{sessionId}`) and episodes (`/episodes/{actorId}/{sessionId}`) whose reflections consolidate on the per-actor prefix `/episodes/{actorId}`. The catalog lives in `services/memory_strategies.py`; `ensure_memory` creates a new memory with all four and, on re-bootstrap, adds whatever an existing memory lacks through `UpdateMemory addMemoryStrategies` (additive — nothing else on the resource is touched). Namespaces are keyed only on `{actorId}` (there is no `{agentId}` template), so the platform folds the agent id into the actor — `scoped_actor(agent_id, human)` → `<agent>__<human>` — which partitions **both** short-term events and long-term records (`/facts/<agent>__<human>`) per agent. Chat derives `human` server-side from the signed console session; the browser cannot choose it. Generated Strands runtimes restore short-term turns through `AgentCoreMemorySessionManager`. Claude Agent SDK containers create one request-local `MemorySessionManager`, inject bounded short-term turns plus `/facts/<actor>` and `/preferences/<actor>` records through a `UserPromptSubmit` hook, then persist the successful USER/ASSISTANT pair as one event. The chat rail lists the actor's facts and preferences plus this session's summary and episodes (exact per-session namespaces, so actor-level reflections stay out). A2A runtimes use `<agent>__a2a__<contextId>` because direct A2A currently has no authenticated human actor envelope; the internal `__agent_card__` factory context is deliberately stateless because it is not a valid Memory session id. One agent's learned facts never bleed into another's for the same person or A2A context; the ledger still stores the bare human actor for display. |
| **Knowledge Bases** *(Bedrock, not AgentCore)* | Managed Bedrock Knowledge Bases (`type: MANAGED` — the service owns the vector store, embeddings and reranking) are the grounding layer: S3 `MANAGED_KNOWLEDGE_BASE_CONNECTOR` data sources, ingestion jobs, and `Retrieve` / `AgenticRetrieveStream` retrieval. Agents mount them through the dedicated MCP gateway `launchpad-kb-gw` (managed Harness) or through `kb_search` / `kb_deep_search` tools baked into generated zip/container code — see [Managed Knowledge Bases](#managed-knowledge-bases-console-04). |
| **Gateway** | `launchpad-gw` turns a REST API (office-facts) and a Lambda (hr-database) into MCP tools with Cognito-JWT auth; agent tool calls flow through it. Governance manages **Gateway rate limits** (GA Aug 2026) on managed Gateways — `ListGatewayRateLimits` / `CreateGatewayRateLimit` / `UpdateGatewayRateLimit` / `DeleteGatewayRateLimit` behind the RATE LIMITS panel of the gateway detail, validated server-side and journaled in `policy_changes`. |
| **Identity** | Token vault backing the gateway — an OAuth2 provider (agent outbound auth) and an API-key provider. Members with `identity.manage` manage workspace **Connections** (console label: **Outbound credentials** / 出站凭证; OAuth2 / API-key credential providers; secrets go to AWS only) and Gateway targets bound to one (`credentialProviderConfigurations`, as-agent M2M or API key) from the v2 admin Outbound credentials module (`/v2/connections`, `?view=targets`) over `/api/identity/*`; agent specs reference a Connection per tool through `ToolRef.auth`, and the read-only 身份 page (`/v2/agents?view=identity&id=`) reads the agent's workload identity, inbound mode and downstreams back from AWS. **Inbound auth** (P3): a Runtime is IAM (SigV4) *or* a `customJWTAuthorizer`, one at a time; resolution is `spec.inbound_auth` > workspace default (`Workspace.settings.inbound_auth_default`) > IAM, snapshotted on the agent row per deploy, and every Create/UpdateAgentRuntime echoes the authorizer (UpdateAgentRuntime resets an omitted one). `POST /api/agents/{id}/inbound-auth` switches in place (same ARN, new version). Every JWT editor (wizard, workspace default, the agent page's editable switch dialog) can take the discovery URL from an OAuth2 Connection (`GET /api/identity/connections/oidc-sources`, never its outbound client id) and warns when the issuer is not the workspace pool's: platform invokes present workspace-Cognito tokens, so the invoke chain refuses such an agent up front with `agent.inbound_issuer_mismatch`. JWT agents are invoked over the data-plane HTTPS endpoint with a bearer — Chat's *invoke as me* sends the signed-in user's Cognito JWT, otherwise the workspace M2M token — while the Memory actor stays `scoped_actor(agent, human)` either way. `obo` Gateway targets use `grantType: TOKEN_EXCHANGE` against a Connection carrying `onBehalfOfTokenExchangeConfig` (a CUSTOM_JWT gateway and an RFC 8693 / RFC 7523 IdP required; Cognito refused with 422; a Connection issuer that differs from the gateway's inbound issuer returns a non-blocking `warnings[]` entry). Design and service-model findings: [identity.md](identity.md). |
| **Registry** | The GA `agent-registry` service hosts `launchpad-registry`, cataloguing A2A agents, MCP servers, and AGENT_SKILLS. `services/agentcore/registry.py` translates the GA `AGENT/MCP/SKILL` and `data/dataSchemaVersion` model into the stable Launchpad descriptor contract; other AgentCore services remain under `bedrock-agentcore`. GA uniqueness is `(name, recordVersion)`, so newly created records use type-qualified initial versions (`1.0.0-a2a`, `1.0.0-mcp`, `1.0.0-skill`) and content edits preserve the suffix. Every deploy auto-creates and submits an A2A record when Registry is available. In accounts whose SCP/IAM policy denies Registry setup, bootstrap records the capability as unavailable, Registry-only APIs return 503, and the deploy pipeline skips only the register stage; Runtime/Harness deployment remains usable. Governance can import one existing AgentCore Gateway as one MCP record containing the Gateway endpoint and its complete discovered tool catalog; legacy per-target records remain until an explicit retirement after the Gateway record is APPROVED. Registry approval controls catalog visibility, not Gateway authorization. `GET /api/registry/attachables` reports catalog status separately from Harness attachability and resolves Gateway auth server-side. For an A2A record owned by a deployed Launchpad A2A agent, the Registry drawer's LIVE CARD reads the card the runtime serves right now (`GET /api/registry/records/{id}/live-agent-card` → data-plane `GetAgentCard` on the ledger's `Agent.arn`, the session AWS opens is ended at once) and diffs it against the record's stored card; the live card is never persisted — AWS stays the source of truth. Both cards read their `version` from one platform constant, `A2A_CARD_VERSION` in `services/agentcore/registry.py`: the A2A runtime template passes it to Strands `A2AServer(version=...)` at package time and `build_a2a_card` stamps it on the record at register time, so the two agree by construction. It is deliberately **not** the AgentCore runtime version (`Agent.version`, shown in VERSIONS & ENDPOINTS) — that is assigned by Create/UpdateAgentRuntime only after the template has been rendered, so the card cannot carry it. A2A agents published before this constant existed still serve Strands' default `0.0.1` against a record `version` of `1`; the diff flags them until their next re-publish (re-render + re-register), which converges both sides. The Registry page has two views over the same registry: the **publisher list** (`GET /api/registry/records` → control-plane `ListRegistryRecords`, every record in every state) and the **consumer view** (`?view=discoverable`, `GET /api/registry/records/discoverable` → data-plane `ListDiscoverableRegistryRecords`, paginated to completion) — what a consumer or agent with data-plane access actually discovers. Discovery summaries carry no `descriptors`; opening a row reads the full record. Once the consumer view has been fetched in a page session, every control-plane record absent from it is chipped NOT DISCOVERABLE (DRAFT / PENDING_APPROVAL / REJECTED / DEPRECATED are the expected cases) — the diff, not the list, is the point. |
| **Policy** | Governance discovers existing MCP Gateways live, persists opt-in management through Launchpad-owned Gateway tags, and manages one attached Policy Engine plus Cedar policies. Initial Engine attachment mode is operator-selected (`ENFORCE` by default, `LOG_ONLY` available); new policies still start `LOG_ONLY`. A Gateway that references an Engine deleted out-of-band is surfaced as an explicit dangling state — reads keep the stale ARN visible, policy mutations return 409, and create-and-attach replaces the reference. ACTIVE edits create LOG_ONLY candidates, promotion and rollback use conservative ordering, and later Gateway transitions to `ENFORCE` require evidence or a typed zero-evidence override. Authenticated Chat calls to `launchpad-gw` use a server-minted Cognito user JWT, so Cedar `OAuthUser` tags such as `username` and `cognito:groups` reflect the signed-in console identity instead of the agent's M2M client. ZIP Runtime receives it in the sensitive invoke payload; Harness receives an invocation-scoped authenticated `remote_mcp` tool. A second CUSTOM_JWT Gateway gets the same treatment when it already admits these tokens (`services/user_gateways.py`): its authorizer's discovery URL is the workspace pool's, `allowedClients` holds both the console client and the M2M client, it has no `allowedAudience` (Cognito access tokens carry none), and no interceptor sets `passRequestHeaders`. Such a Gateway is Harness-attachable with the workspace M2M provider, and each logged-in Harness turn re-reads it and swaps it for a `remote_mcp` tool under its configured alias carrying the user JWT; one that no longer qualifies, or cannot be read, keeps its M2M identity. This is what lets a dedicated Gateway with *as user* (3LO) targets see the human instead of the agent. Public API/evaluation traffic and an auth-disabled local console remain M2M. When such a Harness call is denied at call time (a rule with a `context.input` condition — a scope-only deny instead filters the tool out of `tools/list`, so the model never calls it), the Harness returns the Gateway's denial as an error `toolResult`; Chat recognises its signature (`services/policy_denials.py`, shared with Policy Test) and streams `policy_denied`, which V2 Chat shows as a policy-deny card beside the as_user consent card and persists as a `policy` history row. ZIP Runtime agents do not forward tool results yet. Every mutation is journaled locally while AWS remains the source of current state. |
| **Evaluation** | Real `StartBatchEvaluation` / insights over CloudWatch traces. A run's scope is exactly one of: a **dataset** (replay items — multi-turn scenarios replay sequentially in one session), explicit **session ids**, or a **time window** (`lookback_hours` 1–336 — passive: no new invocations, `filterConfig.timeRange` over existing traffic). 14 general prompt-template evaluators (12 trace/session plus 2 ordinary tool-call), 2 skill `TOOL_CALL` prompt-template evaluators, and 3 ground-truth-only programmatic `Builtin.Trajectory*Match` session matchers (selectable only on dataset runs whose scenarios define `expected_trajectory`) plus **custom evaluators** with full CRUD on the `?view=evaluators` sub-page in three definitions — **LLM-as-a-judge** (`llmAsAJudge`: instructions with placeholders, numerical rating scale, Bedrock judge model), **derived** (`derived`: a Builtin/ThirdParty base evaluator's prompt on a chosen model) and **code-based** (`codeBased.lambdaConfig`: a Lambda function ARN in the workspace Region plus a 1–300 s timeout, default 60; no instructions, scale or model). Every definition needs `level` on `CreateEvaluator`. The judge model defaults to `global.openai.gpt-6-sol` (judge and derived, console and assistant evaluation assets); `CreateEvaluator`/`UpdateEvaluator` test-call the model with a fixed `max_output_tokens=10` that GPT-6 refuses (it needs ≥ 16, and an explicit `inferenceConfig.maxTokens` does not change the probe), so exactly that `ValidationException` is retried once on `global.anthropic.claude-sonnet-5-5` and the reply's `model_fallback` (`{requested, used, reason}`) says so — the assistant's materializer records the fallback and its own client token on the resource before the retry, so a resumed operation rebuilds the same request. The console detail projection carries `definition: judge|derived|code`; `UpdateEvaluator` is a full-config replace, so an update payload must be of the evaluator's own kind — a judge payload against a code-based evaluator (or any other cross-kind pair) is refused with `evaluator.definition_mismatch` instead of silently converting it. A code-based evaluator's Lambda receives `{schemaVersion, evaluatorId, evaluatorName, evaluationLevel, evaluationInput.sessionSpans, evaluationReferenceInputs, evaluationTarget}` and answers `{label, value?, explanation?}` or `{errorCode, errorMessage}` (300 s / 6 MB limits); the console manages **no IAM** for it — the evaluation execution role that batch/online runs pass as `evaluationExecutionRoleArn` needs `lambda:InvokeFunction` + `lambda:GetFunction` on the function and the function's resource policy must allow `bedrock-agentcore.amazonaws.com`, both stated as a hint under the ARN field. Code-based evaluators are selectable wherever custom evaluators are (batch runs, experiments, online configs). When the account listing fails (`ListEvaluators` AccessDenied on a spoke role, throttling, a non-AWS error), `GET /api/eval/evaluators` still answers the builtins and adds `custom_unavailable: {code, message}` (the mapped `aws.*` / `workspace.assume_role_failed` code or `evaluators.list_failed`, with a generic message — never the raw AWS text), logged once as a warning; the evaluator pages show it as a banner and the V2 pickers as an inline note, so a failed listing never reads as an account without custom evaluators. Insights runs pick a subset of the three analysis types (failure analysis / user intent / execution summary). Datasets live in SQLite as devguide scenarios (`?view=datasets` sub-page: scenario editor, JSON/JSONL import) and sync one-way to AWS Dataset resources (`AGENTCORE_EVALUATION_PREDEFINED_V1`): the first sync creates the dataset (`CreateDataset`), every later sync edits that dataset's **DRAFT** in place (`ListDatasetExamples` → `DeleteDatasetExamples` → `AddDatasetExamples`, each polled through `UPDATING` to `ACTIVE`) so the dataset id and its published versions survive; **PUBLISH VERSION** (`CreateDatasetVersion`) snapshots the draft as an immutable numbered version and flips `draftStatus` from `MODIFIED` to `UNMODIFIED`. The row's `cloud` blob caches id/ARN/status plus `draft_status`, `example_count` and the version list (`ListDatasetVersions`); cloud-only datasets show the same read-only and can publish too, and a single published version can be deleted (`DeleteDataset` with `datasetVersion`). A cloud-dataset run may **pin a published version** (`dataset_version`, validated against `ListDatasetVersions` before the run row exists; `GetDataset` and `ListDatasetExamples` then read that snapshot so the replayed scenarios and ground truth are exactly the version's); the draft is the default, and the pinned version is stored on the run and shown as `· v<N>` in the runs list. A recorded copy that AWS no longer knows (`ResourceNotFoundException`) or that was deleted through the console is re-created on the next sync; scenario ground truth (assertions / expected responses / expected trajectory) is injected into batch runs via `evaluationMetadata.sessionMetadata`. A dataset scenario whose invocation hits a transient upstream error (a mid-stream `runtimeClientError` / `internalServerException`, throttling, a 5xx, or a Harness stream that ended without answering — `harness.incomplete_response` right after a tool step (stop reason `tool_result` / `tool_use`) or with neither a stop event nor any text, where the agent never ran) is replayed in a fresh session up to `TRANSIENT_SCENARIO_RETRIES` (2) more times before the run fails; a Harness execution timeout (a model call that never returns holds the scenario silent until the agent budget runs out) is replayed once (`TIMEOUT_SCENARIO_RETRIES`). A scenario that then still ends on the agent's own budget — the timeout again, or an iteration / token limit (`harness.execution_limit`) — is the agent's result for that scenario, not a run failure: its session is kept and scored as it stands, and the run row records it in `budget_stops` (`[{scenario_id, session_id, code, stop_reason}]`, shown as a notice on the V2 task detail). Any other error still fails the run, naming the scenario — except for a **simulated persona** scenario: the SDK executor swallows the agent call's exception, so `simulation.run_simulated_scenario` re-raises that original exception (a Harness timeout then gets its one replay like a replayed scenario), and a persona that still fails is **skipped** — recorded in `scenario_failures` (`[{scenario_id, retries, code, error}]`, a notice on the V2 task detail) while the other personas run and are scored; the run fails only when no scenario produced a session (live 2026-10-10: one Harness GPT-6 call that returned 0 tokens for 600 s had discarded all six personas of a run). The V2 task wizard accepts simulated datasets and asks for the user actor model (`actor_model_id`) when one is picked. Runs execute through a bounded-concurrency queue — up to `eval_max_concurrent_runs` at once (default 5, capped at 5 to match the AWS active-batch-evaluations account quota); excess runs queue instead of failing. Lower the cap when other consumers in the same account need headroom. An operator can **stop** any active run from the Runs page (`POST /api/eval/runs/{id}/stop`): a run whose batch exists on AWS is stopped with `StopBatchEvaluation` (STOPPING → STOPPED — the sessions already judged keep their results, which the poller records as partial scores), a run still queued is cancelled locally before it ever reaches AWS, and a run replaying its dataset stops between prompts without calling `StartBatchEvaluation`. All three end in the terminal ledger status `stopped` (never `failed`) with the reason "stopped by operator"; `DeleteBatchEvaluation` is not exposed. The run row stores only the per-evaluator **averages** (`evaluatorSummaries.statistics.averageScore`, each with its judgement `count` = `totalEvaluated`; AWS leaves `statistics` empty for a code-based evaluator, so that evaluator's mean is computed from the batch's results stream when the run finishes), and the architect's next-steps card shows the polarity-normalized, count-weighted mean of them (`runMeanScore`) — the same number as the task detail's normalized mean over every result; the **judge's explanation of every score** lives in the batch's own results log stream (`GetBatchEvaluation.outputConfig.cloudWatchConfig` → `run-<batchId>` in `/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default`, `gen_ai.evaluation.result` records), which the Runs page reads on demand for the selected terminal run (`GET /api/eval/runs/{id}/results`, never persisted) and renders as a SESSION RESULTS panel — per session, one row per judgement (evaluator, level, score, label, expandable explanation; a span-level evaluator yields one row per tool call) with a link to the Observability session detail. **Online evaluation** (`?view=online`): one AgentCore `OnlineEvaluationConfig` per agent + evaluator set scores a sampled share (0.01–100 %) of live sessions after a session-idle timeout, no new invocations; results land in `/aws/bedrock-agentcore/evaluations/results/<configId>` (also EMF metrics under `Bedrock-AgentCore/Evaluations`) and the console aggregates them with Logs Insights (per-evaluator mean / labels / trend / recent records with judge explanations). The page lists **every** config in the workspace account classified by owner — `agent` (Launchpad-created, full control), `experiment` (`exp_*`/`can_*` arms, read-only), `external` (pause/resume/delete only). Update always sends the complete `rule` because AWS replaces it wholesale; create refuses a never-invoked agent (AWS validates the log group exists). A config runs in one of two **modes**: `scores` (evaluators) or `insights` (1–3 insight types + optional DAILY/WEEKLY/MONTHLY clustering — AWS forbids both on one config); insights configs produce **reports** (batch evaluations sourced from the config: AWS-scheduled on the clustering cadence, or RUN REPORT NOW from the console through the run queue), attributed via `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource` and rendered with the same insight-cluster trees as the Runs page; a report covers only the sessions the config sampled. Online scores also surface where sessions are looked at: the Observability session detail carries an ONLINE EVALUATION block (every config's result records for that session, owner-classified, fail-soft — a results-query failure never hides traces) and the Overview has an **ONLINE QUALITY · 24h** tile (polarity-normalised, count-weighted mean over the workspace's agent-owned configs, 120 s cache, no AWS call while no config exists). Both read all results log groups at once through `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])`. A third, **on-demand** mode scores one session synchronously through the data-plane `Evaluate` API from the Observability session detail (SCORE NOW: ≤5 evaluators, ≤10 results per call, nothing persisted) — for probing a custom evaluator or one suspicious session, where a batch run would be the durable answer. **Recommendations from a run** (V2 task detail): a completed run seeds `StartRecommendation` scoped to its sessions — the system-prompt job pins `agentTraces.batchEvaluation` to its batch (unless the run has red-team sessions: golden tests the architect marked `adversarial` — injections, nonpublic-information requests, pressure for prohibited advice — whose content makes AgentCore refuse the whole recommendation — those sessions are left out, the others' spans go inline, and the row records `result.excluded_sessions`; the scenarios stay in the evaluation), the tool-description job (which refuses a batch source) gets the same sessions' spans inline; a Managed Harness's inputs are read live from `GetHarness` (+ its Gateway target tool schemas, narrowed by `allowedTools`), other agents fall back to the spec, then to required operator input. Rows (`eval_recommendations`) hold the job id + confirmed inputs and are refreshed from `GetRecommendation` on read. The console shows, per kind, only the newest recommendation and any accepted one; superseded attempts (typically one AWS refused before a retry succeeded) collapse under an "earlier recommendations (n)" toggle (`splitRecommendations`), so a stale FAILED card never sits next to the result. The system prompt may instead come from the registered `gepa_lite` provider (`provider` + `model_id` on the create request): the experiment RECOMMEND stage's reflective pipeline over the run's own batch-results stream and session transcripts, run on a background thread with a `provider-` pointer instead of an AWS id (a row a restart left non-terminal reads as interrupted) — the fallback when the AgentCore job refuses the input (its prompt-attack protection rejects some ordinary Chinese system prompts outright). A completed system-prompt recommendation of a Managed Harness run can be **accepted** from the task detail as well as from the architect's step 3: the operator reviews the prompt in an editable dialog and the reviewed text is what re-publishes the Harness (`accept` body `system_prompt`; `accepted.edited` records an edit). |
| **Optimization** | Recommendations → configuration bundles → gateway A/B (config-bundle 50/50) → target-based canary → verdict → promote → cleanup. The system-prompt recommendation is **pluggable**: the AgentCore job by default, or a 3rd-party provider (`gepa_lite` — one GEPA-style reflective round over the pinned evaluation run's per-session judge scores, explanations and transcripts, on an operator-chosen Bedrock Converse model) that bypasses `StartRecommendation` and its content filter; its prompt still becomes the treatment configuration bundle, so the A/B measures it like any other. Dataset replay at the traffic stage posts prompts concurrently (at most `TRAFFIC_MAX_CONCURRENCY` = 10 in flight, `LAUNCHPAD_TRAFFIC_CONCURRENCY` dials it down); one prompt is one session is one arm, so the split is unaffected. A canary's replay is paired instead: each prompt goes to both versions (see Console V2 below). Every replay (and a canary's live gateway route) sends `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`, which the Gateway forwards as `runtimeUserId`: without it the runtime gets no workload access token, and a twin whose tools need an outbound Identity token (its Gateway MCP client) errors on every session, leaving the A/B test with no scores. |
| **Observability** | CloudWatch Logs Insights over both telemetry layouts: legacy traces in `aws/spans`, and unified traces/logs/prompts in `/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint>`. Span records are rendered as a per-session rail. The agent detail's Runtime logs card reads a runtime's raw stdout/stderr with `FilterLogEvents` (`GET /api/agents/{id}/runtime-logs`): the log group is derived server-side from the agent row, a session narrows to the streams named after it, and the shared `otel-rt-logs` stream is excluded unless asked for. On an agent that can host another principal's private session (a system preset), a line of a stream named after no session (a Harness's, `otel-rt-logs`) shows only when its own content names a session the caller may read — the trade-off: that agent's print-style stdout is never shown; the card says how many lines it hid (not under a keyword, where the count would reveal which hidden lines match). Every other agent shows such lines. |
| **Builtin Tools** | Code Interpreter (`aws.codeinterpreter.v1`) runs operator-editable Python in an inline execution demo. Browser accepts an operator-editable navigation URL, starts a five-minute `1280x720` session, and returns a server-generated SigV4 Live View URL rendered by the official `BrowserLiveView` DCV component. The operator can use the managed browser or select an existing READY custom Browser with `browserSigning.enabled` for Web Bot Auth, restore an existing Browser Profile, and explicitly opt into saving Profile state before stop. Explicit stop and backend expiry both release retained sessions. |

## The unified five-stage deploy pipeline

All creation methods converge into the same ordered stages, defined in
`backend/app/deployer/pipeline.py`:

```
generate → package → provision → deploy → register
```

Each method contributes one callable per stage (or omits it to skip). Stage
progress is persisted on the `Deployment` row and mirrored as JSONL events into
the `Job` log, so a restarted backend resumes from the first non-succeeded
stage (`resume_pending_jobs()` runs on startup).

`resume_pending_jobs()` is one of nine startup hooks in `create_app`, and they give
in-flight work different fates: deploy/bootstrap/uninstall/purge jobs and
evaluation-asset operations **resume**; eval runs with a started batch and Policy
changes **reconcile** from AWS; eval runs without a batch, Skill Lab jobs and open
architect turns **fail as interrupted**; experiment/canary `running_action` flags
are **cleared** for the user to retry; data pipelines and provider recommendation
jobs **fail on their next read**, so the startup journal never mentions them; and a
`Job.type` with no resume starter is **stranded**. `backend/scripts/inflight.py`
(over `app/services/inflight.py`) reads the ledger read-only — no app build, no AWS
call — and lists every in-flight row with its `restart_outcome`; its exit code
(`0` nothing, `2` only resumes/reconciles, `3` something is failed, cleared or
stranded) is the pre-restart step of the prod update recipe. A drift-guard test
parses `create_app` and the `starters` dict, so a new startup hook or resumable job
type fails the suite until the inventory classifies it.

| Stage | 方式B — harness | zip_runtime / 方式C — studio | 方式A — container | byoc — bring your own code |
|---|---|---|---|---|
| **generate** | Build `CreateHarness` request from the AgentSpec | Render the Strands template (studio: adapt user code verbatim) | Assemble ARM64 build context (Dockerfile + `main.py` + `.claude` scaffold) | *No code generated.* Verify the staged upload (or the ECR image) and stamp server-verified provenance (sha256, uploader, timestamp) onto the spec |
| **package** | *skipped* (no artifact) | resolve → hashed lock → `--require-hashes` install of ARM64 wheels → zip → S3 | zip context → S3 → CodeBuild (docker build+push) → ECR → resolve digest → scan gate | `code_zip`: download → safe-extract → verify entrypoint → resolve the zip's `requirements.txt` for linux/aarch64 (hashed lock) → zip → S3. `container_source`: verify Dockerfile → same CodeBuild → ECR → digest → scan gate as 方式A. `container_image`: *skipped* |
| **provision** | Reuse the shared execution role | Reuse the shared execution role | Reuse the shared execution role | Per-agent least-privilege role (same machinery) |
| **deploy** | `CreateHarness` + poll READY | `CreateAgentRuntime` + poll READY | `CreateAgentRuntime(containerConfiguration)` + poll READY | `CreateAgentRuntime` — `codeConfiguration` (user's Python version + entrypoint; ADOT launcher only when the bundle ships it) or `containerConfiguration` — + poll READY |
| **register** | A2A registry record, auto-submitted; skipped when Registry was explicitly unavailable at bootstrap | A2A registry record, auto-submitted; skipped when Registry was explicitly unavailable at bootstrap | A2A registry record, auto-submitted; skipped when Registry was explicitly unavailable at bootstrap | Same shared register stage — byoc agents are runtime-backed for chat/versions/observability |

Typical timings: harness ≈ 30 s, zip ≈ 1–3 min (incl. pip), container ≈ 2–4 min (observed: 1.7 min CodeBuild + seconds to READY)
(via CodeBuild). See [troubleshooting.md](troubleshooting.md).

### Per-agent execution roles

Every agent used to assume one shared `launchpad-agent-execution-role` carrying 14
statements, most account-wide. The exposure that mattered was not the wildcards in
the abstract but that **any agent had every other agent's reach**: mount any other
agent's file systems, read every agent's skill bundles, retrieve from every knowledge
base, and rewrite gateway routing.

`app/services/agent_iam.py` derives a role per agent from its spec. Sids are kept
identical to the CDK role so the two can be diffed statement by statement.

| Grant | Emitted when | Scope |
|---|---|---|
| `BedrockModels` | always | the configured `model_id` |
| `BedrockMantle*`, Marketplace | `model_source == "mantle"` | project/`*`; Marketplace guarded by `CalledViaLast` |
| `AgentCoreMemory` | memory enabled | the memory singleton |
| `AgentCoreWorkloadIdentity`, `IdentityVaultSecrets` | a gateway/MCP tool or KBs | — |
| `AgentCoreCodeInterpreter` / `AgentCoreBrowser` | that builtin is attached | — |
| `EcrPull` / `EcrAuth` | `method == "container"` | the repo |
| `SkillBundle*` | skills attached | **this agent's** prefixes |
| `ManagedKbRetrieval` | KBs attached | **the attached** KB ARNs |
| `A2AInvokePeerRuntimes` | `protocol == "a2a"` | account runtimes |
| `Telemetry` | always | the runtime log groups |
| BYO-mount policy | mounts configured | **this agent's** access points |

**Deliberately still `*`, and why**: `bedrock:AgenticRetrieveStream` and
`bedrock-mantle:CallWithBearerToken` and `ecr:GetAuthorizationToken` do not support
resource scoping, and neither do X-Ray ingestion or `cloudwatch:PutMetricData`.
Recorded at the statement rather than quietly narrowed.

**Two grants were removed**, which is worth knowing because a removal is what shows
up as a runtime failure: `ABTestOrchestration` (19 actions including
`CreateGatewayRule`, `UpdateGateway`, `InvokeAgentRuntime`) is what the *platform*
does from its own credentials, and the CloudWatch Logs **read** actions were console
paths that had leaked onto the workload role. `InvokeAgentRuntime` is kept for A2A
agents, which legitimately call peers.

**Per-agent roles do not give per-agent memory isolation.** There is one shared
memory, partitioned by folding the agent id into the actor id
(`services/memory.py::scoped_actor`), not by IAM. An agent whose spec pins its
own memory (`spec.memory.memory_id`, picked in the Create wizard) does get its
grant — and its `LAUNCHPAD_MEMORY_ID` / harness memory configuration — scoped to
that resource instead of the shared one; actor scoping still applies within it.

Lifecycle: created in `provision`, reconciled on re-publish so a dropped capability
shrinks the policy, deleted with the agent — **after** the runtime, since removing the
role first can wedge the runtime's own deletion. A failed delete never blocks deleting
the agent; the role is tagged `launchpad:agent-id` so an orphan is findable.
`ensure_role` adopts an existing role of the same name, so a half-failed delete does
not wedge re-creating an agent under a reused name.

Canary and A/B candidates keep whatever role **production is already on**, read from
`GetAgentRuntime.roleArn`. A candidate stands in for production, so giving it the
shared role would measure it with permissions production lacks — and reading the live
value rather than deriving the name means agents predating this still work.

The shared role remains and still carries broad grants: it backs agents that have not
been re-published. Reducing it before every agent has migrated would strip grants from
agents still using it, so that reduction is **not** done yet.

### Supply chain of a build

Two things about a deployed artifact have to be answerable: what went into it, and
whether what runs is still what was built. Both live in the `package` stage.

**Dependencies are resolved, then locked, then verified.** A single `pip install`
over the declared list — which is what this used to be — installs whatever the
index serves at that moment, including for the platform's own ranged pins, and
leaves no record. The stage now runs `uv pip compile --generate-hashes` for the
deploy target (aarch64, Python 3.13, defined once in
`app/core/runtime_target.py` so the resolve and the install cannot disagree)
with `--only-binary=:all:`, then installs those same wheel-only candidates with
`--require-hashes`. Without the matching binary constraint, the resolver can
lock an sdist-only release that the Runtime's ARM64 binary-only install
rejects. A substituted or re-uploaded distribution fails the build. The lock
ships inside the zip as `requirements.lock`, so the artifact carries its own
bill of materials. There is deliberately no fallback: a resolve failure fails
the stage.

The resolution target is **`manylinux_2_28` / aarch64** by default. The
AgentCore Runtime direct-code environment was measured (2026-09-18, from inside
a deployed PYTHON_3_13 agent) as Amazon Linux 2023 on aarch64 with glibc 2.34,
so it loads any manylinux wheel up to `manylinux_2_34`; the official docs'
`manylinux2014` recommendation is safe but rejects packages that only publish
`manylinux_2_26`/`2_28` aarch64 wheels (e.g. `google-re2`, a `chromadb`
dependency). The level is configurable via `runtime_python_platform`
(`LAUNCHPAD_RUNTIME_PYTHON_PLATFORM`); `manylinux2014` is the documented
fallback should a runtime image ever report an older glibc. Because pip treats
`--platform` tags as exact strings, the install passes the whole tag ladder
from the configured level down to `manylinux2014`.

Caller-supplied `spec.requirements` must additionally be pinned at *schema*
validation (`app/schemas/requirements.py`), so the console rejects a range before a
build starts. Each entry must be a single ASCII line without control characters
or a marker starting with `-`; this check runs before stripping markers, so a
valid pin cannot hide a second requirements-file line such as `--index-url`.
Both pin resolution and lock compilation repeat the check before writing their
input files, including for older stored specs. The platform's own lists keep their
ranges — the `MANTLE_EXTRA_REQUIREMENTS` comment explains that pip is meant to intersect two
specs for the same project — and the lock is what makes the resolved set
reproducible. Harness conversion is the one place the platform derives
requirements from somewhere else (the source Harness's `pyproject.toml`), so it
resolves those ranges to pins rather than being exempted from the rule. It also
records the ranges themselves on `spec.requirement_ranges` (plain
`name[extras] <specifiers> [; marker]` lines only — URLs, VCS references, options
and control characters are refused, so the field cannot widen the build's index
boundary), and re-publishes from either console carry the field.

Those pins were resolved against the platform's lists *as they stood at conversion
time*, and the platform's own pins move (`boto3>=1.43.66` forcing
`botocore>=1.43.66`; `strands-agents==1.47.0` capping `mcp<2.0.0`). Every zip build of
an agent spec — the package stage and the canary's candidate/restore mint — goes
through `zip_runtime.build_agent_zip`. When the lock fails for a **conversion twin**
(`spec.source_harness` set), it re-resolves the twin's pins once against the
*current* platform lists — from the recorded range of each project, or, for a twin
converted before ranges were recorded, from the bare `name[extras]` — and retries the
lock. Pins that still lock are never re-resolved. The job log names each changed pin
(`mcp==2.1.1 → mcp==1.30.0`). The healed pins are persisted where each caller's
contract allows: the package stage writes them to `agent.spec.requirements` (so the
as-deployed snapshot names them); canary setup writes them to the candidate's
`artifacts.edited_spec` (the mint never writes the agent row; promotion copies that
spec); canary rollback writes them to the agent row with the restored version and
records a `canary rolled back` snapshot. A non-twin spec is never re-resolved. If a
lock still fails, the error names the agent's own conflicting pin(s) and the platform
requirement uv cites against them (with an "update or remove" fix for a non-twin);
for a twin whose source ranges no longer fit, it says so. Failures that name no pin of
the agent's own (no usable wheel, unknown package) read exactly as before.

**Container images are scanned, and deployed by digest.** ECR scans on push. After
the build, `_stage_package` resolves the pushed tag to its immutable digest,
records it on the `Deployment` row, and runs the gate before the image can back a
runtime; `_stage_deploy` sends `repo@sha256:…` as `containerUri`. Deploying by the
`{agent}-v{version}` tag would mean what a runtime executes can change with no
record of it.

The gate's threshold and off switch are configurable, because an un-overridable
gate strands every agent the first time a base image picks up a CVE. A scan that
could not be read — scanning not enabled, an API error, a timeout — is logged as
exactly that and the deploy proceeds unscanned; it is never folded into "clean",
because an absent gate must not read as a passed one.

Image tags stay **mutable**: packaging runs before `_stage_deploy` bumps the
version, so a re-publish pushes the same tag twice and an immutable-tag policy
would fail that push. Digest pinning is the control, and an infra test asserts the
tag policy so this cannot drift into a broken re-publish.

Not covered: SBOM generation, provenance/attestation, signing, approved-mirror
enforcement, and skill *content* review. Immutable is not the same as trusted.

### Agent management routes

Since 2026-09-18 the module is list-first (`/create` and `/create?view=discover`
redirect; the query string is kept so Registry's `?gateway=` / `?skill=` prefill
still lands on the wizard):

| Route | View |
|---|---|
| `/agents` | landing: `+ New Agent` / `Import existing Runtime`, a stats strip (total / running / deploying / failed, derived from the loaded list) and the agent table (name → detail, CHAT + DETAILS visible, EDIT / CONVERT / DELETE in a per-row `···` menu, FAILED rows carry the error as tooltip + VIEW REASON) |
| `/agents/new` | the 3-step wizard; step 1 is the four method cards below, a button to the import page, and the system-preset cards (install / configure stay where they were, under the cards) |
| `/agents/import` | discovery of existing Runtime / Harness resources |
| `/agents/:id` | the agent's detail (the wizard's step-3 view: launch sequence, versions, BYOC provenance, conversion notes; live polling while deploying; OPEN CHAT / OBSERVABILITY / EDIT links). A deploy started on `/agents/new` navigates here when it goes active |
| `/agents/:id/edit` | the wizard preloaded for a re-publish (system presets open the shared editor, Studio agents go to `/create/studio?agent=`) |

### Creation entrances

The `/agents/new` picker shows four cards, in this order:

| # | Card | `AgentSpec.method` | What it is |
|---|---|---|---|
| 1 | **Managed Harness** | `harness` | 方式B — declarative, no build artifact |
| 2 | **Strands Studio** | `zip_runtime` | 方式C — Strands template on the zip fast path; the card's nested link opens the `/create/studio` canvas, which deploys as method `studio` |
| 3 | **Other Agent SDK** | `container` | 方式A — bring your own agent SDK, packaged as an ARM64 container via CodeBuild |
| 4 | **Bring Your Own Code** | `byoc` | user-written agent code uploaded as a zip (direct-code runtime or Dockerfile → CodeBuild) or referenced as an existing private-ECR image — see [BYOC](#byoc--bring-your-own-code) |

The V2 wizard adds a fifth card, **Agent IDE**, which is not a method of its
own: it opens the coding console (`/v2/coding`), where a codex agent builds the
project and the result deploys as `byoc` — see
[Agent IDE](#agent-ide--a-remote-web-ide-over-codex-on-agentcore).

Discovery of existing runtimes and harnesses is not a deploy method: it has its
own page at `/agents/import` (see below), reachable from the list header and
from a button next to NEXT on step 1.

The third card is a **category**, not one SDK. `AgentSpec.agent_sdk` records
which SDK a container agent packages, and the wizard exposes it as a
second-level choice on the configure step. It is a single-member `Literal`
(`claude_agent_sdk`) that defaults to that member, so container specs written
before the field existed read back unambiguously and adding a second SDK needs
no stored-spec migration. There is deliberately **no dispatch** on the field yet:
`app/deployer/container.py` and `app/templates/claude_sdk_agent/` stay
unconditional until the category has a second member.

### File systems (`AgentSpec.filesystem`)

`AgentSpec.filesystem` maps onto AgentCore `filesystemConfigurations` through one
helper (`app/deployer/filesystem.py`). It defaults to **managed session storage
(Preview) at `/mnt/workspace`**. That storage is per session, survives
stop/resume with the same `runtimeSessionId`, holds up to 1 GB, expires after
14 idle days and is reset by every new version. An explicit
`session_storage: null` turns it off.

| Method | Session storage | BYO S3 Files / EFS (+ VPC) | Where it is sent |
|---|---|---|---|
| `harness` | yes | yes (probe P3/P4 pending) | `environment.agentCoreRuntimeEnvironment.filesystemConfigurations` |
| `zip_runtime` / `studio` | yes | yes (probe P1/P2 pending) | `CreateAgentRuntime` / `UpdateAgentRuntime` |
| `container` | yes | yes (probed live 2026-07-13) | `CreateAgentRuntime` / `UpdateAgentRuntime` |
| `byoc` (code zip, image; agents and MCP servers) | yes | yes (probe P5 pending) | `CreateAgentRuntime` / `UpdateAgentRuntime` |

`schemas/agent.BYO_MOUNT_METHODS` lists the methods that accept BYO mounts (design
storage-mounts §3). The request shapes are in the `bedrock-agentcore-control`
service model for all of them; the live behaviour probes are
`backend/scripts/probe_storage_mounts.py` (P1 zip create, P2 zip path change, P3
harness create, P4 harness PUBLIC → VPC re-publish, P5 BYOC image, P8 read-only).
A method a probe refutes leaves the set and is refused with the probe's
conclusion (`BYO_MOUNT_UNSUPPORTED`). BYOC sent no `filesystemConfigurations`
at all before; it now sends the spec's list (session storage included) on create
and on every update, so an existing BYOC agent gains `/mnt/workspace` on its next
re-publish.

**Mounts need the agent's network.** An S3 Files / EFS mount target is reached
from the runtime's ENIs, so a spec with a BYO mount must name a network
(`spec.network`, a network profile — see *Agent network* below); a PUBLIC one is
refused (422), and `runtime.py` refuses the combination again before any
Create/Update call. The mount rides the same `spec_vpc` placement every method
already uses; there is no container-only VPC path any more.

**Who may set them.** Inline mounts (the form that predates the storage catalog
below) are access point ARNs, and — as inline subnets / security groups —
only administrators may add or change one (`services/mount_policy.py`,
`storage.inline_not_allowed`, 400). A member may re-publish an agent whose mounts
stay as an administrator set them, or remove them. The execution role gets the
`launchpad-fs-<agent>` inline policy for every method (always with
`ClientWrite`; a read-only mode arrives with the catalog).

**The storage catalog** (storage-mounts §4, SM-2, `services/storage_mounts.py`,
[api.md](api.md#console-storage-catalog-api)). An administrator registers an S3 Files
or EFS access point once per workspace as a catalog entry (`storage_mounts`: id, kind,
access point, `max_mode` `ro` / `rw`, `data_class`, an optional default network
profile) and allows it per project (`project_storage_mounts`), like network profiles;
an entry outside the project's allowed set reads as missing. A spec then mounts it by
id, `filesystem.mounts[{storage_id, mount_path, mode}]` (read-only by default, at most
`max_mode`), so members pick storage in the wizard without ever naming an ARN; the
server stamps the entry's kind and access point into the stored spec. The **check**
reads the access point, file system and mount targets through the workspace's
read-only describe calls; the **preflight** (`POST /api/storage-mounts/preflight` from
the wizard, and again at deploy time, `409 storage.preflight_failed`) runs each mount
against the spec's network profile: same VPC, a mount target in every subnet's AZ, NFS
(TCP 2049) out of the agent's security groups and into the mount targets'. **IAM is per
mode**: `storage_iam_statements` grants `ClientMount` for every mount and adds
`ClientWrite` only for `rw` ones (inline mounts stay read-write). On **promotion** a
catalog mount is a resource-mapping reference of kind `storage`: the catalog id is the
logical name, the target workspace's entry (or the one a mapping row names) must be
live and allowed for the project, else the release is refused (`storage.unmapped`),
and the resolved spec is re-stamped with the target's access point. Inline access
points name the source account and are not promotable
(`storage.inline_not_promotable`).

**Runs that write production storage** (storage-mounts §8.2). In a gated workspace an
evaluation replay or a canary of an agent that mounts `shared_state` storage read-write
writes into it, so the backend answers `409 storage.write_confirm_required` with the
mounts. The console cannot know beforehand, so the run and canary pages send their
request unchanged and, on that answer, ask once and resend with `confirm_storage: true`
(`lib/storage-confirm.ts`); the release gate shows the mount diff, its evaluate button
needs the same confirmation, and signing a read-write change needs `storage_ack`.

**What the agent is told** (`app/deployer/mounts.py`). Code runtimes (zip,
studio, container, BYOC) get `LAUNCHPAD_MOUNTS` — JSON
`[{name, path, kind, mode}]` — in their environment; the Strands HTTP template,
the Claude SDK container template and the harness also get a "Shared
directories" paragraph at the end of the system prompt (rendered into the code
for the templates; appended to `systemPrompt` for the harness, and stripped
again when a prompt is read back from the live harness). Studio and converted
code, and A2A agents (whose card description is cut from the prompt), read the
variable only. Managed session storage is not listed.

The two update APIs behave differently on a re-publish (probed live 2026-10-04):

- **UpdateAgentRuntime clears an omitted `filesystemConfigurations`**, the same
  way it resets `protocolConfiguration`. The zip, container, BYOC and
  canary-candidate paths therefore send the list on every update; leaving it out
  is how "session storage off" detaches the mount.
- **UpdateHarness keeps an omitted `environment`** and replaces the list as a
  whole when one is sent (`[]` detaches everything). The harness re-publish
  reads `GetHarness` first (`harness.environment_for_update`) and merges by
  `mountPath`: the spec's entries (session storage and BYO mounts) are the
  platform's and are sent as they are; a live entry at a path an earlier publish
  of the agent set (its spec snapshots) is the platform's too, so a mount removed
  from the spec is detached; any other live entry — an EFS / S3 Files mount made
  outside the platform — is kept, with the network/lifecycle settings. A live
  entry the platform never set at a path the spec now uses is replaced by the
  spec's, with a line in the deploy log. A canary rollback omits `environment`,
  so it keeps the current mounts.

Every stored spec already carries the default, because it is written with
`model_dump()`. So an existing harness or Strands agent gains `/mnt/workspace`
on its next re-publish unless the member turns it off. Session storage needs no
IAM grant. In a VPC network profile (see *Agent network* below) the runtime reaches
the `acr-storage-*` S3 buckets through the profile's S3 gateway endpoint, which the
profile preflight requires of every agent. A Harness reaches its mounts through its
native `shell` / `file_operations` tools. The V2 wizard (`FilesystemCard`) edits
the setting; the BYO rows are editable by administrators only, and the classic
console round-trips the stored value unchanged.

### Agent network (`AgentSpec.network`)

Every deploy method deploys into the network profile `spec.network` names: harness,
`zip_runtime` / `studio` (HTTP and A2A), `container` and BYOC (code zip and image,
agents and hosted MCP servers alike). `deployer/network.spec_vpc` is the one resolver:
`{profile_id}` (or a pre-profile inline `{subnets, security_groups}`, matched to its
`legacy-<hash8>` profile) becomes the `{subnets, security_groups}` each deployer hands
to `networkConfiguration` in VPC mode; an omitted network is `PUBLIC`. The Runtime paths
send it on create and on every update. `UpdateHarness` keeps the live network unless the
spec names a profile; a re-publish that moves a harness *off* its profile carries the old
network in the job payload (`network_from`), so the deploy sets `PUBLIC` instead of
keeping it. A network change is an ordinary re-publish: a new version on the same
resource.

**Deploy-time network gate.** `pipeline.network_gate` runs in `POST /api/agents`,
`POST /api/agents/{id}/redeploy` and `POST /api/agents/{id}/convert` before any job
exists:

- no network while the workspace forbids PUBLIC (`network_public_allowed: false`) →
  `400 network.public_not_allowed`;
- a profile → its preflight is re-run for *this* agent's features
  (`network_preflight.features_of_spec`: container image pulls, Bedrock models, memory,
  Gateway tools, Identity, A2A, knowledge bases), so the endpoints the agent needs are
  checked, not only the ones every agent needs. A Gateway data plane
  (`<id>.gateway.bedrock-agentcore.<region>.amazonaws.com`, used by Gateway tools and by
  a harness's mounted knowledge bases) needs its own `bedrock-agentcore.gateway`
  endpoint; when the VPC has a `bedrock-agentcore` interface endpoint with private DNS,
  that zone hides the gateway names, so NAT cannot stand in and the missing endpoint
  blocks (`details.shadowed_by`). Any blocking item →
  `409 network.preflight_failed`, with the blocking items in `detail.results`. A
  `legacy-<hash8>` profile is not preflighted: its deploys behave as before profiles.

**Where an agent runs.** `GET /api/agents/{agent_id}/network`
(`services/agent_network.py`) returns `mode` (`PUBLIC` / `VPC`), the resolved profile
with its last preflight (status, `checked_at`, blocking / warning counts; `missing`
when the workspace no longer has it), the placement (`vpc`) and the ENIs AgentCore holds
there (`ec2:DescribeNetworkInterfaces` filtered by the profile's subnets and security
groups). ENIs are listed per profile, since every agent placed in the same subnets and
security groups shares them; an older spoke role without the read reports
`status: unavailable` instead of failing the route.

**Console.** The V2 wizard's network step (`NetworkSection`) is on every method,
including the harness quick setup's advanced options. A new agent starts in the
project's default profile when `GET /api/network-profiles` marks a choosable one
`is_default`, and the review step shows the network for every method (`Public`, or the
profile). The Studio publish dialog offers the same choice and carries the stored
network on a re-publish (the spec is replaced as a whole).

### BYOC — bring your own code

The fourth card deploys code the member's developers wrote themselves — already
wrapped with the AgentCore SDK (`BedrockAgentCoreApp` + `@app.entrypoint`) or
any HTTP server satisfying the runtime contract (ARM64, port 8080,
`POST /invocations` + `GET /ping`, payload `{"prompt", "actor_id"}`). The
**response** is whatever the code answers — the Runtime HTTP contract requires
JSON or SSE and names no key. Chat, the public `/v1` API and evaluation replays
all read it through one parser (`services/agentcore/runtime.py::_runtime_payload_events`):
`{"result": …}` (BedrockAgentCoreApp's convention, preferred) or the
delta/tool/complete SSE envelope stream for real; any other JSON body is shown
by its first conventional text key (`response`, `answer`, `output`, `text`,
`message`, `content`, `completion`, `reply` — a nested `{"text"}` block under
one of them also counts), and a body with none of those is rendered as compact
JSON rather than a blank turn (measured 2026-09-18: a CrewAI agent answering
`{"answer", "session_id", "turns"}` produced an empty reply with no error).
Auxiliary `metadata` or `error: null` fields do not suppress a reply; actual
Converse bookkeeping events remain silent and non-empty `error` values surface
as failed turns. Three
artifact kinds, one `spec.byoc` block (`backend/app/schemas/agent.py::ByocConfig`):

| `artifact_kind` | Input | Path to Runtime |
|---|---|---|
| `code_zip` | zip of Python source (staged via `POST /api/agents/uploads`) | S3 → `CreateAgentRuntime(codeConfiguration)` with the member's Python version + entrypoint; the platform resolves the zip's `requirements.txt` into the bundle for linux/aarch64 (hashed lock, wheels only — nothing is executed) |
| `container_source` | zip carrying a Dockerfile | the shared `launchpad-agent-builder` CodeBuild project (ARM64) → ECR `launchpad-agents:{name}-v{version}` → `containerConfiguration`, including the digest pin and image-scan gate the container method uses |
| `container_image` | an existing image URI | verified with `ecr.describe_images` — must live in this workspace's account+region; public registries and other accounts are refused — then deployed as-is |

**Telemetry.** Observability and evaluation read the spans an agent exports to
`aws/spans`, which AgentCore Runtime does only under the ADOT
`opentelemetry-instrument` launcher. A `code_zip` gets the launcher when the
upload carries `aws-opentelemetry-distro` (vendored, or in `requirements.txt`
with `install_requirements`) — `spec.byoc.provenance.adot_launcher`, re-judged on
every publish; container kinds decide in their own image. A code zip without it
stays in the Observability agent drop-down, greyed with the reason
(`v3/pages/observability/agentOptions.ts`), and `POST /api/eval/runs` refuses it
with `eval.no_telemetry` while its endpoint has written no span in 14 days
(`evaluation/service.py::require_span_telemetry`, one bounded `FilterLogEvents`
on `aws/spans`; an inconclusive probe lets the run start).

**Security model.** Developers need no IAM: they hand a zip to whoever holds the
`perm:agents.deploy` console permission (uploads carry the same permission).
Each agent gets its own least-privilege execution role (`services/agent_iam.py`);
BYOC container kinds additionally get `ecr:BatchGetImage`/`GetDownloadUrlForLayer`
scoped to the image's repository. The role's `bedrock:InvokeModel` statement
covers exactly `spec.byoc.allowed_models` (1–20 ids; absent ⇒ `[spec.model_id]`)
— the union of each entry's foundation-model + inference-profile ARNs, deduped,
never a model wildcard. Literal IDs, foundation-model ARNs and system
inference-profile ARNs are supported. Wildcards, IAM variables and unsupported
ARN kinds (including application inference profiles) are rejected before
deployment; an unknown custom ID stays an exact resource rather than granting
all foundation models. Entry `[0]` is the primary (= `spec.model_id`); the deployer
injects it as env `MODEL_ID` and the full list as `ALLOWED_MODEL_IDS`
(comma-separated) so the code knows what it may call — `spec.env` values win.
Re-publish rewrites the role policy, so an edited list lands with the deploy. Uploads are workspace-scoped under
`byoc/{workspace_id}/{upload_id}/` in the artifacts bucket, and the server stamps
provenance (sha256, size, filename, uploader, time) onto the spec — the console
renders it on the agent detail view.

Each source packaging attempt owns a private temporary directory, removed after
the upload/build finishes, so same-named agents in different workspaces cannot
overwrite one another's sources. If deployment resumes after provision, it
reconciles the per-agent role again before calling Runtime; losing process-local
scratch state never selects the shared role. The shared role is used only when
the operator explicitly disables `per_agent_execution_roles`.

**What is validated / what is not.** The upload gate enforces archive safety
(zip-slip, absolute paths, symlinks, ≤250 MiB zip / ≤750 MiB uncompressed /
≤20k entries — the AgentCore direct-code caps) and *reports* detection
(entrypoint candidates, requirements.txt, Dockerfile, AgentCore-SDK markers).
When the zip carries a `requirements.txt`, the upload also dry-resolves it
against the deploy target for the selected Python version (`?python_version=`)
and reports `detected.requirements: {status: ok|failed|skipped, package_count,
error}` — so the wizard flags an unresolvable file before a deploy is
attempted. `skipped` (resolver timeout, `uv` unavailable) says nothing either
way; the deploy still runs the authoritative resolve.

**requirements.txt rules (`code_zip`).** The file is parsed per the pip
requirements-file format — backslash continuations, inline comments, blank
lines and environment markers are all honoured. `--hash=` options are dropped:
the platform re-locks the file against its own deploy target and generates
fresh hashes (`requirements.lock` inside the artifact). List direct
dependencies from the package index only; pins are optional (the hashed lock is
what makes the build reproducible). Refused with a clear error, because a
requirements file must not widen the platform-index-only supply-chain boundary:
`-r`/`-c` includes, `-e`/editable, local paths, direct URLs and VCS references,
`--index-url`/`--extra-index-url`/`--find-links`, and more than 500 entries.
When a dependency ships no compatible aarch64 wheel, the error names the
package and the alternatives: pin a release that does, use the Dockerfile
(`container_source`) path, or vendor the packages inside the zip with
`install_requirements=false`.
The platform does **not** review or scan the code itself; `container_source`
images do pass the existing ECR scan gate. User code is never executed on the
Launchpad host — package-time work is extraction and a wheels-only pip install
into the bundle directory. For `container_source`, the platform's own
`buildspec.yml` is always injected into the CodeBuild source zip, **overwriting
any buildspec the upload carries** — the member controls the Dockerfile only,
never the build recipe.

**v1 scope.** HTTP or MCP protocol (no A2A); no toolkits/skills/knowledge
bases/tools on the spec (the platform does not generate this code, so it cannot
wire them — configure capabilities inside your own code); `system_prompt` is
optional and serves as a description. Config-bundle experiments and canary
candidates degrade with `custom-source-unverified`, exactly like other
custom-source runtimes. Samples: [`samples/byoc/`](../samples/byoc/README.md);
lab walkthrough: [docs/lab/13-byoc.md](lab/13-byoc.md).

**Native attachments (`code_zip`).** A `code_zip` entrypoint can opt into the
[playground attachment contract](#playground-file-attachments) exactly as
generated code does: a top-level `LAUNCHPAD_ATTACHMENT_CONTRACT = "v1"` literal.
The package stage records it from the uploaded entrypoint and the deploy stage
stamps `Agent.attachment_version` only once the runtime is READY, so Chat then
sends the original images/PDFs as `attachments: [{name, media_type, data}]`
(base64) beside the prompt — provided `spec.model_id` is a multimodal model. The
code must acknowledge them before its answer (`"attachment_contract": "v1"` on a
JSON reply, or a leading `{"event": "attachments", "contract": "v1"}` SSE event).
Container kinds and stdio-bridged MCP servers never record a contract, so they
keep text/text-PDF input.

### Hosted MCP servers (`protocol: "mcp"`)

The V3 **MCP server** entry (`/v2/agents?view=new&method=byoc&protocol=mcp`) deploys a
BYOC artifact (any of the three kinds) that serves MCP over streamable HTTP on
`0.0.0.0:8000/mcp`, and makes the workspace Gateway its only caller
(`app/services/mcp_servers.py`; design: `docs/design` §15, minimal version). The
spec carries `mcp: {target_name}`; `client_id`, `provider_arn` and `target_id` are
server-owned — the create / redeploy routes replace whatever the request sends, so
no caller can point the pipeline at another Gateway target.
An MCP server may name a VPC network profile like any agent (§15.10): the profile
changes only the runtime's outbound path, and the Gateway still reaches it through the
AgentCore data plane.

| Stage | MCP-server work |
|---|---|
| generate | a Cognito app client of its own (`lp-mcp-<agent id12>`, client_credentials, scope `launchpad-gw/invoke`) in the workspace pool; stamps `inbound_auth` = JWT on that pool allowing only that client |
| provision | per-agent role **without** `BedrockModels` / Mantle grants (a tool calls no model); memory is forced off |
| deploy | `CreateAgentRuntime` / `UpdateAgentRuntime` with `protocolConfiguration.serverProtocol = MCP` — echoed on every update (an omitted one resets the runtime to HTTP); no `MODEL_ID` env |
| register | instead of an A2A record: an OAuth2 credential provider (same name) holding the client, then an MCP-server Gateway target whose endpoint is the runtime's data-plane URL on `DEFAULT`; creating it — or `SynchronizeGatewayTargets` after a re-publish — lists the tools; a same-named target pointing elsewhere is refused (`409 mcp.target_name_taken`), a renamed server's old target is deleted |

The Gateway-level Registry record is **not** rewritten (an update sends an approved
record back to DRAFT for every attaching agent); the register stage logs that
Governance → Registry import refreshes it. An MCP server is not invocable
(`invoke_capability.reason_code = not-agent-protocol`, so Chat, `/v1` and evaluation
skip it), not gateable, and its inbound auth cannot be switched (`422
agent.inbound_auth_unsupported`); a redeploy cannot switch the protocol to or from
`mcp`. `GET /api/agents/{id}/mcp` reads the target status and the tools agents see
through the Gateway (`<target>___<tool>`) back live — the V3 agent page renders it in
place of the lifecycle, chat and try-it panels. Delete runs in reverse: Gateway target →
runtime → OAuth2 provider → Cognito client, idempotent. Sample:
[`samples/byoc/hello-mcp/`](../samples/byoc/hello-mcp/README.md) (`mcp>=1.10,<2`).
A hosted server deploys into `spec.network`'s profile like any other BYOC agent (see
*Agent network*).

**Tool-review gate (§15.6).** A hosted server's tools are not served until a person
other than its deployer has approved the list. Creating the target, or re-syncing it
after a re-publish, lists the tools through the Gateway; `services/mcp_reviews.py`
snapshots them (`[{name, description, input_schema}]`, prefix stripped, sorted) and
compares the snapshot's SHA-256 with the last approved list. A match leaves the target
live. Otherwise a review row is opened in the `mcp_tool_reviews` table, with the diff
against the approved list and reviewer hints (injection-style phrases, URLs,
cross-tool instructions, destructive or write verbs, missing or very long
descriptions), and the target is deleted again. The register stage logs that the
target is withheld, so agents can neither call nor list the new tools. A list that
cannot be read is withheld too (fail closed). Deciding takes the project key
`mcp.approve`, held by the `release_ops` and `tool_steward` templates (and by an
`inherit` member who holds `promotion.approve`). The deployer never decides their own
review, administrators included. An approval re-creates the target in the background,
through the gate again, and records `publish_status` on the review.
`POST /api/mcp-servers/{agent_id}/reviews/check` re-syncs a live target to catch a
server that changed its tools without a re-deploy: a difference opens a `drift`
review and withholds the target. A server whose target predates the gate gets its
live list recorded as an approved `baseline` on its next deploy. Approving or
publishing the Registry record of a hosted server also requires `mcp.approve` and an
approved tool review (`409 mcp.review_pending`). Withholding the whole target stays
the gate; the Cedar scaffold below adds per-tool policies on request. Routes:
[api.md](api.md#console-mcp-tool-review-api).

**Endpoint, health and rollback (§15.8).** The `mcp_servers` table
(`models/lambda_tools.McpServerState`) keeps one row per hosted server: the endpoint
the target calls (`qualifier`), `live_version`, the last `health` and the last
background `operation`. The target calls the runtime's `DEFAULT` endpoint, which follows
every new version. The hourly scheduler task `mcp.health` (`dlc/scheduler.py`,
`mcp_servers.check_health`) checks every live server of an active project: it re-points
the target if it calls another endpoint than the server's current one, re-syncs it,
and compares the list with the approved one (`check_drift`: a drift opens a review and
withholds the target). The outcome is recorded as `health {status: healthy | drift |
failing | withheld, checked_at, tool_count, error, consecutive_failures, alert,
review_id, repointed}`; three failures in a row set `alert` and log a warning.
`POST /api/mcp-servers/{agent_id}/health` runs one check now. A rollback
(`POST …/rollback`) points the named endpoint `live` at an earlier version that has an
approved tool list, records that list as the current approved one (a `rollback`
review), and re-points the target there, through the gate again. The next deploy
un-pins the server back to `DEFAULT`. Moving a gated agent's live endpoint takes
`release.sign`; otherwise `agents.deploy`.

**Cedar scaffold (§15.7).** `POST /api/mcp-servers/{agent_id}/policies/scaffold` plans
(`dry_run`) or applies two Cedar policies on the policy engine attached to the
workspace Gateway (`governance.mcp_scaffold_plan`): `lp_mcp_<target>_unreviewed`
forbids the tools of an open review, and `lp_mcp_<target>_project` permits the approved
tools, conditioned on the project when the workspace binds several projects. Each one
is a `create`, `update`, `delete` or `keep` step. The apply runs in the background, one
governance operation at a time, and is recorded in `operation`. An optional mode switch
(`LOG_ONLY` / `ENFORCE`) in the same request changes shared infrastructure, so it takes
`infra+perm:mcp.approve`, and `ENFORCE` takes the evidence or override that Governance
requires.

**Lambda tool targets (§15.12).** `POST /api/gateway-tools/lambda` records a Lambda
function as a Gateway tool target (`lambda_tool_targets` table). The function must be in
the workspace's account and region, and the inline tool schema is recorded as a tool
review before anything exists on the Gateway, so the creator cannot wave it through. An
approval by a member holding `mcp.approve` other than the creator publishes it in the
background: `workspace_iam.sync_lambda_tools_policy` writes the `launchpad-lambda-tools`
inline policy on the Gateway role (`lambda:InvokeFunction` on exactly the functions of
the workspace's Lambda targets, the function and its `:*` versions and aliases; the
policy is deleted when none remain), then the `mcp.lambda` target is created with the
approved schema. Status: `reviewing` → `publishing` → `live` (or `failed`, `rejected`).
A delete runs in reverse. A Lambda can run in a VPC, which reaches private backends
without the Gateway reaching private endpoints.

**Externally hosted targets (§15.15).** MCP-server targets made by hand on the
workspace Gateway (through Connections) that no hosted server owns are listed by
`GET /api/gateway-tools/external` with `hosting: external`. Adopting one
(`external_mcp_targets` table) records what it lists today as the approved baseline, so
agents already calling it are not cut off; the hourly `mcp.health` task then compares
later lists with it. A drift opens a review, but the target is the customer's and is
never withheld or re-pointed. The V3 page `/v3/tools/gateway` (rail: *Gateway tools*)
lists both kinds, creates Lambda targets and adopts external ones. Routes:
[api.md](api.md#console-gateway-tool-targets-api).

**stdio bridge (§15.5).** A stdio-only server ships as a code zip with
`mcp.stdio_wrapper = {command, args, base}` (code_zip only — a Dockerfile context or
an image builds the bridge in itself). The deployer adds
[`samples/mcp_stdio_bridge/`](../samples/mcp_stdio_bridge/README.md), which serves
`0.0.0.0:8000/mcp` and starts the command (no shell, from the bundle root; argv in
env `MCP_STDIO_COMMAND`) as its subprocess: base `python` stays on the managed
Python runtime with the bridge as entrypoint, base `node` builds an image on the
bridge's Node base (CodeBuild, arm64) and deploys a container runtime. One
subprocess per container, so a server that keeps per-session state sees every
caller as one session; responses are plain JSON (the server's notifications are
dropped, its requests back answered as unsupported). The V2 wizard's MCP server
card has the switch, and hides the entrypoint picker while it is on.

### Recommendation trace source

`RECOMMEND` reads either a rolling `RECOMMEND_LOOKBACK_DAYS` (7) CloudWatch window —
the default — or one completed batch evaluation pinned by
`agentTraces.batchEvaluation`. Pinning matters twice over:

- **Lineage.** An Insights job and a recommendation over the same window merely
  overlap; pinning makes the recommendation provably generated *from* that analysis.
- **Reproducibility.** The 7-day window is *wider* than any single analysis, so the
  default path can ingest traffic nobody looked at — including a previous
  experiment's treatment arm — and re-running the same experiment tomorrow reads
  different traces.

The console offers the experiment agent's own completed runs
(`GET /api/eval/runs?agent_id=…`); the backend resolves the chosen run through
`GetBatchEvaluation`, which is also what validates it (exists, completed, same
agent). Both generators in one RECOMMEND share the pinned source, and the resolved
source — ARN, run id, batch id, mode — is stored on the `recommend` artifact for
both paths, so a finished experiment stays explainable.

### Recommendation providers

The system-prompt generator behind RECOMMEND is a **provider**
(`backend/app/optimization/providers/`); the tool-description generator always
stays AgentCore's. `recommend_provider` absent means the `StartRecommendation`
job runs exactly as before. `gepa_lite` instead reads the pinned run's
batch-evaluation results stream (per-session evaluator scores, labels and
explanations — the same `gen_ai.evaluation.result` records online evaluation
emits) joined with each session's transcript, samples up to 30 sessions
worst-first (polarity-normalised, with a best-scoring contrast set), and asks a
Bedrock model — Claude Opus 5 by default, Sonnet 5 / GPT-5.6 Sol selectable,
custom ids allowed — for one reflective rewrite: diagnosis, concrete changes,
revised prompt — and, in the same call, revised descriptions for the agent's
**own** tools (the discovered set the treatment bundle can overlay), reasoning
from each session's tool calls, results and tool-call judge verdicts; gateway /
MCP tools are shown as context and never rewritten, and a run with no tool
calls settles the tool side as `no-tool-calls` while the prompt proceeds. It is
GEPA's reflection step without GEPA's search loop: the configuration A/B that
follows is what evaluates the candidate. A provider
that cannot produce a usable prompt (no scored sessions, model access denied,
unparseable output, over the 8 000-character budget after one compression)
writes a `FAILED` status and reason and **no prompt** — the same ISSUE-007 rule
as a failed AWS job — so `accept` stays gated. The artifact records
`provider`, `provider_model_id` and evidence counts, and the treatment bundle's
commit message names them, so a finished experiment stays explainable. The
Bedrock call goes through the workspace client funnel; the `gepa` package (and
its litellm client construction) is deliberately not a dependency. The providers
themselves live in `backend/app/optimization/providers/`: `base.py` is the contract
every provider implements, `registry.py` the import-side-effect registry,
`evidence.py` the scored-session/conversation join, `bedrock_lm.py` the
ConverseStream text callable, `gepa_lite.py` the reflective round described above,
and `agentcore.py` the built-in AgentCore job listed for discovery only.

### Platform toolkits (`AgentSpec.toolkits`)

A **toolkit** is a named, platform-owned bundle of local `@tool` functions over
embedded seed data that the Strands ZIP template inlines into the generated
`main.py`. `zip_runtime` + `protocol=http` only; one member today,
`hr_assistant` (five HR tools: PTO balance/request, policy lookup, benefits
summary, pay stub).

It is deliberately **not** a `ToolRef.type` member: every existing member denotes
an external resource that drives IAM and deployer behaviour, while a toolkit
drives neither — no ARN, no grant, no gateway, no network call, no extra pip
requirement.

Two properties make it worth its own field:

- **It is rendered at generation time, so `spec.code` / `spec.code_bundle` stay
  `None`** and the agent keeps its config-bundle experiment eligibility. Writing
  generated source into either field returns `custom-source-unverified` from
  `experiment_capability` — which is why this is a spec *selection*, not
  materialized code.
- **A toolkit replaces the template's own `calculator` / `current_utc_time`**
  rather than adding to them, so the deployed tool surface is exactly the
  toolkit's. That matters for trace readiness: `missing_tools` being non-empty
  forces `state="sparse"`, so a tool that is expected but never exercised pins an
  agent below `ready` permanently.

Tool names and descriptions are derived from the toolkit source with `ast`, using
Strands' own docstring rule (docstring minus the `Args:` section), so
`discover_agent_tools` — and therefore `expected_tools`, readiness, and the
recommend UI's "current description" — reports exactly what the model sees. The
catalog itself is `backend/app/templates/toolkits/__init__.py` (each member's tool
source is a `*.py.tmpl` template beside it); the spec field is `AgentSpec.toolkits`
in `backend/app/schemas/agent.py`.

### The Strands method — builder options (SB-1, SB-2, SB-3)

The Strands method (`zip_runtime`, the platform-generated template) exposes a curated
part of the Strands Agents SDK beside the model, prompt, tools, knowledge bases,
skills, memory, guardrail, environment, limits, A2A, mounts and network it already
had. Every field is optional and applies to `zip_runtime` only (not with
`code` / `code_bundle`, which deploy their own source); a spec that sets none of them
renders byte-identical code (snapshot tests). The schema and its checks live in
`backend/app/schemas/strands_builder.py`; the generated support code is
`backend/app/templates/strands_agent/builder_support.py.tmpl`.

| Capability | Spec field | What the generated agent does | Limits |
|---|---|---|---|
| Built-in tools | `strands_tools[]`, `strands_tool_config` | Imports the selected `strands-agents-tools` (pinned `0.8.3`, added to `requirements.txt`): `current_time`, `calculator`, `http_request`, `think`, `retrieve`, `image_reader`, `journal`. They replace the template's own `calculator` / `current_utc_time` | `http_request` needs `strands_tool_config.http_request.allowed_hosts` (hosts or `*.host`; empty refuses the tool). `retrieve` searches the agent's mounted knowledge bases only (a mounted KB is required). `journal` writes under the runtime's scratch directory. `shell`, `python_repl`, `file_write` and `use_aws` are not offered here — they are the sandboxed tools below |
| Model parameters | `model_params{temperature, top_p, max_tokens, stop_sequences[], thinking_budget_tokens, prompt_caching}` | Passed to the model per call; `thinking_budget_tokens` turns on Claude extended thinking, `prompt_caching` the SDK's automatic cache point (system prompt and tool definitions) | Ranges checked by the schema; thinking for Claude models only |
| Conversation | `conversation{strategy: sliding_window\|summarizing, window_size, summary_model?}` | Sliding window (default 40, as before) or a summarizing manager that keeps `window_size` recent messages verbatim | `summary_model` is a Bedrock model for `summarizing` only; the agent's execution role is granted it beside the agent's own model (`agent_iam.allowed_model_resources`) |
| Structured output | `output_schema` (JSON Schema object) | Answers the parsed object as well as the text: the runtime's `complete` event carries `structured`, the console stream and `/v1` SSE a `structured` event (`{value}`), `/v1` sync a `structured` field | An object schema, at most 5 levels and 50 properties per object, 20 000 characters. Not with `protocol: a2a`, and not with PII protection (the guardrail screens the text only; the invoke layer also drops `structured` on a guardrail agent) |
| Prompt template | `prompt_variables{name: default}`, `few_shot_examples[{question, answer}]` (≤ 10) | `{{name}}` in the system prompt is filled per request from the invoke payload's `context` (`/v1` request field `context`), then the environment variable of that name, then the default; undeclared names are left as written. Few-shot pairs are appended as an examples block | ≤ 20 variables, names `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, defaults ≤ 2000 characters |
| Other agents as tools | `agent_tools[{agent_id, tool_name, description}]` (≤ 10) | Each entry becomes a tool that calls `InvokeAgentRuntime` with the agent's own role: the platform's SSE envelope for an `http` target, JSON-RPC `message/send` for an `a2a` target. The sub-agent session id is stable per caller session; chains stop at depth 3 | The target must be a deployed Runtime of the same project (or lent to it at `use` level) in the same workspace, protocol `http` or `a2a`, IAM inbound auth (not JWT); a foreign or unknown id answers 404, an ineligible agent or the agent itself 422. `runtime_arn` / `protocol` / `qualifier` come from the ledger, re-resolved at deploy (`services/agent_tools.py`); the role is granted `InvokeAgentRuntime` (and `InvokeAgentRuntimeForUser` when needed) on exactly those ARNs. Tool names are unique and may not shadow other tools |
| Model provider | `model_provider{kind: anthropic\|litellm, connection, base_url?}` (`model_id` is then the provider's model name) | `AnthropicModel`, or `OpenAIModel` pointed at a LiteLLM gateway's `base_url`. The API key is read at run time through the token vault from an `api_key` Connection (cached 5 minutes); the spec never holds a key. The provider's model name is baked in, so a per-project `LAUNCHPAD_MODEL_ID` does not replace it | `base_url` is required for `litellm`. HTTP agents on the Bedrock model source only: refused with `protocol: a2a`, on Mantle, and with extended thinking or prompt caching |
| Tool-call guards | `tool_hooks{approval_tools[], max_tool_calls_per_turn, log_tool_calls, mask_args[]}` | Strands hooks: a tool on the approval list (names or `*` patterns) is paused in the console chat — the stream carries `approval_required` (`tool`, `tool_use_id`, masked `input`) and the chat shows an approval card; Approve sends the next turn with `tool_approvals.approved = [tool]`, one name allowing one call. Any other caller (`/v1`, evaluations, A2A peers, other agents) is refused (`approval_refused`). A per-turn call cap that resets each turn, and a masked `[launchpad-tool]` log line per call | ≤ 50 approval entries; cap 1–500. Arguments named in `mask_args` (plus the built-in password / secret / token / api_key / authorization) are written as `***` in the log and on the approval card |
| Throttling retry | `retry{max_attempts, initial_delay_s, max_delay_s}` | Strands `ModelRetryStrategy`: a throttled model call is retried with an exponential backoff ladder | 1–10 attempts, initial delay 1–60 s, max delay 1–600 s and not below the initial delay |
| Custom Python tools | `custom_tools[{name, description, code, requirements[], execution: runtime\|code_interpreter}]` | Each is one `@tool` function written in the wizard. `runtime`: packaged as `custom_tools/<name>.py` in the zip and imported at start (its pinned `name==version` requirements join `requirements.txt`); `code_interpreter`: the source and the call's arguments are sent to the AgentCore Code Interpreter, a fresh namespace per call. A tool that cannot load becomes a stub that answers the error | A static check without running the code (`schemas/custom_tool_code.py`): exactly one top-level `@tool` function named after the tool, supported annotations, literal defaults. Imports outside the allow-list, `os.system`-style calls, `eval` / `exec`, computed `getattr`, `open()` outside the session directory, dunder escapes, import-time code and every declared requirement are findings: such a version needs an administrator's approval of its exact code hash (`/api/custom-tools/approvals`, per project), checked when the agent is saved and again at the deploy's generate stage. A sandboxed tool declares no requirements |
| Sandboxed tools | `sandbox_tools[]` ⊆ `python_repl`, `shell`, `file_write`, `use_aws` | Run only in the AgentCore Code Interpreter (a lazy session, one retry on a fresh one), never on the runtime; `use_aws` checks the service and operation names and runs boto3 inside the sandbox with the sandbox's own role. The execution role gets the Code Interpreter actions (as for the `code-interpreter` builtin) | The workspace sandbox setting (`/api/custom-tools/sandbox`, administrators) opens each tool and may name the workspace's own Code Interpreter (`code_interpreter_id`, stamped server-side at save and deploy; a client-sent id is dropped). A tool the setting has not opened is refused at save (`422 custom_tool.sandbox_tool_not_allowed`) |

SB-2 fields render a separate support file (`builder_sb2.py.tmpl`) only when one of
them is set, so SB-1 specs still render byte-identical code; SB-3 fields likewise
render `builder_sb3.py.tmpl` after it, so SB-2 specs are byte-identical too. In the console, the V2
and V3 chat pages render `approval_required` as an approval card (tool name and the
masked input, Approve / Decline); Decline sends nothing and the call stays unrun. The
create-agent chooser's capability comparison lists these options under "Strands
builder options".

**Experiments.** The generated code reads `model_params`, `conversation`,
`few_shot_examples` and `prompt_variables` from the configuration bundle per request
(same keys), so they can be compared in an A/B experiment: the experiment's control
bundle carries the agent's stored values and the treatment bundle the same values
with the treatment's overrides (`optimization/service.py` `stage_bundles`,
`BUNDLE_CONFIG_KEYS`). An agent that sets none of them gets the same bundle request
as before.

### Registry Skills and deployment snapshots

The Create Agent wizard reads only APPROVED `AGENT_SKILLS` records from
`GET /api/registry/attachables`. A selection stores the bundle's S3 prefix in
`AgentSpec.skills`; invocation never searches Registry. The selected prefixes
also drive the owning agent's `SkillBundle*` IAM statements.

Each method consumes that shared field according to its artifact model:

| Agent shape | Skill materialization | Runtime activation |
|---|---|---|
| Harness | Native Harness S3 Skill source | Harness progressive disclosure |
| Generated zip, HTTP or A2A | Package-time snapshot under `skills/<name>/` | Strands `AgentSkills` plugin, enabled only when at least one packaged `SKILL.md` exists |
| Container | Image-build snapshot under `.claude/skills/<name>/` | Claude Agent SDK project `Skill` tool |
| Studio | Generated-code references resolve APPROVED bundles into `skills/<name>/` | Studio-generated `AgentSkills` plugin |
| Harness-converted `code_bundle` | No platform snapshot; exported fetcher remains authoritative | Exported runtime fetcher |

Registry edits and reimports do not hot-update zip, container, or Studio
artifacts. Re-publish the agent to capture a new snapshot. A2A has two separate
Skill concepts: `AgentSpec.skills` mounts instruction/resource bundles, while
`AgentSpec.a2a_skills` publishes AgentCard routing metadata.

### System-managed presets (`aws-agent-solution-architect`)

A **system-managed preset** is an agent whose identity and spec belong to the
platform rather than to a member. The first (and so far only) preset is
`aws-agent-solution-architect`: a managed Harness (方式B) that turns an AI-agent
business requirement into an evaluation-first AWS design. It adapts an external
methodology package (three intake rounds, pain point → metric → golden test →
evaluator mapping, AgentCore-first trade-offs, evidence ranking, no autonomous
execution) into platform-owned **English** assets under
`backend/app/system_agents/skills/aws-agent-solution-architect/` — a `SKILL.md`
plus `references/` — and a system prompt in `backend/app/system_agents/presets.py`.
The original package is never vendored, and none of its PDF, DOCX, installer or
desktop scripts ship. The agent answers in the language of the user's latest message
instead of a hard-wired locale.

**Server-owned identity.** `Agent.system_key` (new, nullable, indexed) marks a preset
row. It is never read from a request: `AgentSpec` has no such field, so a client
sending `system_key`/`system` in a spec is ignored (Pydantic drops unknown members)
and the row stays ordinary. The reserved name is refused for ordinary agents
(`409 agent.name_reserved`) and skipped by discovery import, and a partial unique
index on `(workspace_id, system_key) WHERE system_key IS NOT NULL AND status !=
'deleted'` binds one live preset per workspace. The API projection carries a
`system` member (`{managed, key, label, skill_version, protected_actions}` or `null`)
which is what the console renders the SYSTEM chip from.

**Protected mutation paths.** `POST …/redeploy`, `DELETE /api/agents/{id}` and
`POST …/convert` answer `403 agent.system_managed` for a preset **before any AWS
client is built**, whatever `perm:agents.*` the caller holds — including an
administrator, who maintains presets only through `/api/system-agents`. The same
refusal guards the indirect writers: `POST /api/experiments/{id}/action` and
`POST /api/runtime-canaries/{id}/action` refuse any action on a (possibly stale)
row that references a preset before `running_action` is written, and the service
entry points a background thread would run (`act_promote`, canary `act_setup` /
`act_complete` / `act_rollback`, both `run_action` dispatchers) refuse before the
first AWS call; the capability projections additionally report
`reason_code: system-managed`. `DELETE /api/knowledge-bases/{kb_id}` — with or
without `force` — answers `409 kb.attached_to_system_agent` as a ledger-only
preflight when the KB is mounted on a preset, so a member can never force-detach a
preset's knowledge base or touch its gateway target; an administrator detaches it
first by repairing the preset with a `knowledge_bases` body that omits the KB.
Ordinary agents keep the 2026-08-07 member-lifecycle rights and the ordinary KB
force-delete semantics unchanged (`tests/test_system_agents.py` asserts the
parity).

**Explicit, idempotent installation — never on startup or read.**
`GET /api/system-agents` (member) is a ledger-only read reporting one of
`configuration_required` (workspace not `ready`, missing `artifacts_bucket` /
`execution_role_arn`, or per-agent roles disabled), `not_installed`, `deploying`,
`uninstalling` (a teardown job owns the row; `operation` carries its job id, status,
attempt, error and `retryable`), `active`, `failed`, plus `requirements` as `{code,
message}` pairs (also on an installed preset whose workspace later lost a
prerequisite), `name_collision` when
a pre-existing ordinary agent holds the reserved name (the preset **never adopts**
it — `409 system_agent.name_collision` on install) and the operation-specific
verdicts `can_install` / `can_repair` / `can_uninstall` (administrator +
operation-specific readiness). The console localizes descriptions and requirement
codes and shows loading, error and retry states; it consumes the install/uninstall
response directly and refreshes the agent list when a poll reaches a terminal
status.
`POST /api/system-agents/{key}/install` (admin) is the one path that reaches AWS:

| Preset state | Result |
|---|---|
| not installed | row + create job (`202`, `created: true`) |
| deploying | the in-flight job is returned (`202`, `changed: false`) — repeated clicks stack no jobs |
| active, same version + options | no-op (`200`, `job_id` = the job that produced the active preset) |
| failed / options changed / newer bundle / `force: true` | update job = in-place re-publish (`202`) |

The request body is a required JSON object and a **partial edit** (SE-040): `{}`
means "the preset defaults" on a first install and "exactly the stored choices" on
a repair; every member given replaces the stored value and every member omitted
keeps it; `reset: [...]` returns named members to this build's defaults and
`clear: ["max_tokens" | "reasoning_effort"]` unsets those two knobs (an unset
`max_tokens` is still sent as `65536` for an OpenAI GPT model — see below) (JSON `null` means "unchanged", never "clear"). Unknown members — `name`,
`allowed_tools`, `memory`, `skills`, `tools`, `system_key`, … — are refused with
`422` before any row, job or AWS call, as are out-of-range values and unsupported
pairings (`422 system_agent.invalid_options`, e.g. a `reasoning_effort` on a
non-OpenAI model). The partial edit is resolved against the stored spec **inside the claiming
transaction**, and the repair's compare-and-set is conditioned on the row's status
*and* its version (`updated_at`) as that resolution read it: a row that changed
meanwhile (a concurrent edit that was accepted and finished) fails the claim and the
same partial edit is re-resolved on the new state (up to three attempts, then `409
system_agent.conflict`), so a member the edit omits is never reverted to a stale
value. An explicit edit while a deploy job owns the row answers `409
system_agent.deploy_in_progress` with that job's id instead of coalescing onto it
(a bodiless repair click still coalesces), and so does the unique-index loser of two
concurrent *first* installs that asked for different settings (identical or bodiless
twins still coalesce onto the winner's job) — a concurrent save is never dropped or
falsely accepted. Maintenance claims are durable
and atomic: a fresh install races into the partial unique index and the loser
re-reads the winner **and returns the winner's job id**; a repair executes one
compare-and-set `UPDATE … WHERE status IN (active, failed)` in the same transaction
as the job row it creates, so two sessions that both loaded an active row converge
on one job (the second sees no claimed row and returns the first's in-flight job).
**Administrator-editable settings and the architect's inference defaults (SE-040).**
The preset's *inference* and *loop* settings are stored on its spec and reported by
`GET /api/system-agents` as `settings` (what is stored, `{}` when not installed),
`defaults` (this build's catalogue values) and `editable_fields`, plus the verdict
`can_configure` (administrator + settled + prerequisites met — the same predicate as
`can_repair`). The editable members are `model_id` / `model_source`, `max_tokens`,
`reasoning_effort`, `system_prompt`, `max_iterations`, `timeout_seconds` and
`knowledge_bases`; everything else (name, method, tools, versioned skill, allowed
tools, disabled memory, dedicated role) stays catalogue-owned and unreachable from a
request body. Two knobs are new on `AgentSpec` and harness-only: `max_tokens` is the
**per-model-call** output ceiling — `CreateHarness`/`UpdateHarness`
`model.bedrockModelConfig.maxTokens`, *not* the aggregate `InvokeHarness.maxTokens`
and not a spend cap; left unset it is sent as `DEFAULT_GPT_MAX_TOKENS` (`65536`) for an
OpenAI GPT model, whose hidden reasoning exhausts the service default before it writes an
answer (live 2026-10-04: one GPT-6 call reasoned 95 s and stopped at `max_tokens`), and
omitted for every other family (Bedrock rejects a ceiling above a model's own limit); `reasoning_effort` (`low | medium | high`) is accepted **only
for an OpenAI GPT-5.x model on native Bedrock** (`model_source=bedrock`, Converse)
and is sent through `bedrockModelConfig.additionalParams` as
`{"additionalModelRequestFields": {"reasoning": {"effort": …}}}` — the managed
harness merges `additionalParams` **verbatim into the raw Converse request kwargs**
(it is *not* a Strands `BedrockModel` config block, so the snake_case
`additional_request_fields` key fails botocore parameter validation on the real
InvokeHarness), and Bedrock accepts `reasoning.effort` for GPT-5.6 under that wire key
(a flat `reasoning_effort` is likewise rejected as an unknown parameter). Any other
pairing (a Claude/Nova model, Bedrock Mantle's Responses API, a non-harness method)
is refused by the schema rather than guessed at or silently dropped; a spec without
the knobs sends exactly the request it always did. The architect preset's **new-install
defaults** are `global.openai.gpt-6-astra` (GPT-6 Astra on the global cross-region
inference profile, native Bedrock/Converse — the per-agent role authorizes the profile
plus the underlying foundation model), `max_tokens: 65536`, `reasoning_effort: "high"`
and `max_iterations: 100`; the platform
`DEFAULT_MODEL_ID` and the ordinary wizard defaults are unchanged. Stored rows are
**not migrated**: a preset installed by an earlier build keeps its model, prompt and
absent knobs through reads, repairs and bundle updates until an administrator saves
an explicit change (or `reset`) — `options_from_spec` recovers every editable member
exactly as stored, including a system prompt that differs from this build's constant,
so a prompt change in the catalogue reaches an installed preset only through an
explicit `reset: ["system_prompt"]` (the console offers USE THIS BUILD'S PROMPT).
The console's **CONFIGURE** (System presets panel; also EDIT on the preset's row in
the Existing agents table for an administrator) opens the **same configure page an
existing agent's EDIT uses** — there is no preset-specific settings dialog and no
standalone REPAIR/UPDATE card button. The page is prefilled from the preset's *stored*
settings (never the wizard defaults), shows which members differ from this build's
defaults, validates bounds client-side, and on SAVE & RE-PUBLISH (explicit confirm)
posts **only the changed members** to `POST /api/system-agents/{key}/install` — never
to `POST …/redeploy`, which stays `403` for a preset. Emptying the output ceiling or
moving to a model that takes no reasoning effort sends `clear`; USE PRESET DEFAULTS
and USE THIS BUILD'S PROMPT remain on the page. With nothing changed the button reads
RE-PUBLISH and sends `{force: true}`: that is how an administrator retries a failed
deploy or repairs an out-of-band change, so a failed preset opens the editor like an
active one (`can_configure`). The protected members — name, method, tools, the
versioned skill, allowed tools, disabled memory — are rendered read-only and no skill
upload/import or tool attachment is offered; the `memory`/`tools` the ordinary
`buildSpec` would compose are never sent for a preset. Members (and an administrator
while the preset is deploying/uninstalling or the workspace lost a prerequisite) get
VIEW SETTINGS: the same page read-only with the reason and no save button; the table's
EDIT stays disabled for them, and `POST …/install` is `403` for a member whatever
`perm:agents.*` they hold. BACK posts nothing. Every read and save of this path —
panel polls, install/uninstall, the editor's KB catalog, the save and the deploy poll
that follows it — pins the workspace the row was read from as an explicit
`X-Workspace` header, so another tab switching the shared selection can never redirect
them (a same-tab switch remounts the page and drops the draft). A submit is
single-flight (BACK and the button lock while it is in flight) and its `202` lands as
the ordinary launch view (DEPLOYING → stages → job) polling the pinned workspace;
`409`/`422` render inline with the server's detail rows and keep the draft. Persistent
memory stays disabled:
that disables AgentCore *memory* only — the Launchpad chat transcript in the ledger
and the CloudWatch logs are kept, and the system prompt now tells the agent so
(never "nothing is retained").

**Uninstall is a durable, exclusively owned job, not a terminal flag**: `DELETE
/api/system-agents/{key}` moves the row to the non-terminal `uninstalling` status
**together with** an `uninstall_system_agent` job (`202 {job_id, attempt, started,
preset}`). The claim is an optimistic compare-and-set on the row's `updated_at` as
the request read it, so two simultaneous requests — an initial pair or two retries of
a failed attempt — create exactly one job and the loser adopts it (`started: false`).
The row keeps its system identity — and the partial unique index keeps holding the
key — until the worker's teardown is **verified**, so no install or repair can take
the key while the AWS resources are still being removed (both answer `409
system_agent.uninstalling`); a failed teardown leaves the row `uninstalling` with
the reason and per-step progress on the job (`operation.retryable`), and another
explicit uninstall starts attempt N+1. The worker (`system_agents/uninstall.py`) is
**exclusive and fenced**. Exclusivity is a single-host guarantee matching this
repository's topology (one process tree, one SQLite ledger): the worker holds an
advisory `fcntl` lock on `data/locks/system-agents/uninstall-<agent id>.lock` for
the whole run — a lock the kernel releases when its holder dies, so a startup
resume can adopt a `running` job a crashed process left while a still-alive twin
(thread or process on this host) is refused — and it must also win the job's
`queued → running` CAS. This is not a distributed lease. The fence re-reads job and
row before every cloud step, inside every progress write and inside the finalizing
transaction: right job type and workspace, job still `running`, row still
`uninstalling`, this job still the row's *newest* attempt — a duplicate, superseded
or late worker is inert and cannot finish or fail another attempt. Resource
identity is exact: the harness id comes from the row; the KB agentic target is
resolved **once** by the reserved name (while the row still owns that name
exclusively — the desired spec is not consulted, because a failed detach-repair
rewrites it before the old target is gone), its id is **pinned on the job before
the delete**, and deletion and readback use the pinned id only; the execution role
is the deterministic per-agent role, which must carry this agent's
`launchpad:agent-id` tag and must not be the shared workspace role. The teardown is
**strict** and uses low-level clients, never the best-effort `kb_gateway` helpers:
every gateway-target page is read, `DeleteGatewayTarget` is issued and
`GetGatewayTarget` polled until `ResourceNotFoundException` (`AccessDenied`,
throttling, `FAILED` and the 60 s bound are retryable failures); `DeleteHarness` is
issued and `GetHarness` polled until `ResourceNotFoundException` (`DELETE_FAILED` or
the 90 s bound are retryable failures); only then is the role deleted, by installed
ownership regardless of the current `per_agent_execution_roles` toggle (a preset is
never on the shared role), and an IAM delete that reports failure is a retryable
failure, never an ignored `False`. Progress per step (`kb_target`, `harness`, `role`,
with the exact resource id) is recorded on the job; a new attempt carries forward
verified-done steps as skip-eligible (trusted only while their resource id still
matches the row) and carries the **identity** of failed or pending steps — the
pinned target id and gateway id — as `pinned`, so the retry runs the step again
against the same resource and never resolves a replacement by name. A queued retry
requested while the failed predecessor still holds the lock waits boundedly
(30 s) for the release; a repeated `DELETE` for a still-queued job launches a worker
again (the `queued → running` CAS admits exactly one), so a queued attempt never
depends on an app restart. Waiting workers are coalesced: the starters keep at most
one live worker thread per job in this process (a synchronized registry cleared in
the worker's own `finally` and on a failed start), so twelve repeated requests park
one waiting thread, not twelve, and a later request re-wakes the same queued job
with a fresh thread once the earlier waiter has exited. The registry only bounds
waiters; the per-agent kernel lock and the job CAS remain the exclusivity and
ownership mechanisms. Only a fully verified teardown marks the row
`deleted`. The ordinary agent delete keeps its best-effort semantics; the preset does
not use it. The deploy job runs the **normal** `generate → package → provision →
deploy → register` pipeline with three preset-specific hardenings, guarded **at job
entry**: every system-preset deploy job — fresh or resumed, whatever stages already
succeeded or were skipped — proves its release pin (present, well-formed, matching
the stored spec and this build's snapshot) **and** that the spec's `skills` is
exactly the one complete expected URI for the job's workspace,
`s3://<workspace artifacts bucket>/system-skills/<preset name>/<version>-<digest12>/`
— a legacy plain-version directory, another bucket, another preset's path, a
foreign prefix or an extra skill source is refused with the repair instruction — or
it lands as a failed job without touching AWS. Reads may still display such a spec;
execution never accepts it:

- **release pinning, atomically** — before anything is written, the install reads
  the repository bundle **once** into an immutable in-memory snapshot, validates that
  snapshot (same `validate_bundle` as member skills, plus the version/name
  invariants) and hashes it; `{version, digest, files{rel: sha256}}` then lands on
  the job **in the same commit** as the agent, deployment and job rows
  (`create_deployment(payload_extra=…)`), so a crash can never leave a runnable
  job without its pin. The `package` stage (skipped for ordinary harnesses) fails
  closed on a missing or malformed pin — nothing "legacy" is accepted — and refuses
  when the stored spec, the pin and this build's snapshot disagree;
- **one byte snapshot, content-addressed release, conflict-safe publication** — the
  bytes that were validated and hashed are the bytes that are uploaded and read back.
  The release directory is content-addressed:
  `s3://<artifacts_bucket>/system-skills/<name>/<skill_version>-<digest12>/` (the
  first 12 hex digits of the snapshot digest), and the stored spec's `skills` URI
  names exactly that directory, so two valid snapshots of the same version (one with
  an extra reference file, say) can never share the directory the Harness loads — a
  losing writer cannot add bytes to the winner's deployed tree. Every object is
  created with
  `If-None-Match: *`; a 412 means another writer got there first and the existing
  bytes must equal ours (restart after a partial upload, or a same-content
  concurrent retry) or the stage fails without overwriting anything. The manifest
  (`.bundle-manifest.json`, with per-file digests) is created last, also
  conditionally; a competing manifest is accepted only when identical. An existing
  manifest is trusted only when it matches the snapshot exactly (malformed or
  differing → fail: a published version is immutable, bump `skill_version` and the
  SKILL.md `version` together). Finally **every object is read back and hashed
  against the snapshot**: a missing object is restored with `If-None-Match: *`, a
  corrupt one is replaced only with `If-Match` on the ETag that was read, and a
  prefix that still disagrees fails. Final verification also lists the **entire**
  release directory and requires it to be exactly the snapshot's files plus the
  manifest (a foreign object fails the stage and is never deleted), re-reads and
  re-validates the manifest itself (version, digest and per-file digests), and treats
  a manifest that is valid JSON of the wrong type as a fail-closed, zero-write
  conflict with actionable guidance — the stage never reports "verified" for an
  absent or altered `SKILL.md`, and never writes outside the release prefix;
- **idempotent AWS requests** — every harness create/update sends
  `clientToken = lp-<deployment id>` (persisted, not scratch state), so a job
  resumed after a crash between the AWS call and the ledger write repeats the same
  request instead of creating a second harness. This applies to every harness
  agent, not only presets;
- **the provisioned role is what AWS receives** — the deploy stage sets
  `executionRoleArn` from the provision stage's result (or, on a resume that lost
  scratch, from the deterministic `launchpad-agent-<name>-<id8>` role name) for
  every harness agent; the generate stage's shared-role placeholder never reaches
  CreateHarness/UpdateHarness anymore. A preset additionally **fails closed**: with
  `per_agent_execution_roles=false`, or if the resolved role is the shared
  workspace role, generate/deploy raise before any AWS call and the status read
  reports the `per_agent_roles_disabled` requirement.

The prefix family is disjoint from the member-writable `skills/` (registry) and
`agent-skills/` (wizard staging) prefixes.

**The preset's Skill as its own Registry record (SE-043).** The preset agent's A2A
record (`Agent.registry_record_id`) describes the *agent*; the versioned Skill it
loads is registered separately as an `AGENT_SKILLS` record so the Registry lists it
and APPROVED-catalog consumers (`GET /api/registry/attachables`, the wizard, the
assistant) can mount it. The record's `skillDefinition` points at the **existing
immutable release** — `path = s3://<bucket>/system-skills/<name>/<version>-<digest12>/`,
the real `SKILL.md` as `skillMd`, the five bundle files, `version`, and
`source = {kind: "system", preset_key, release_version, release_digest, manifest}` —
nothing is copied to the member-writable `skills/<name>/` prefix, uploaded or deleted:
registration's only S3 traffic is a read-back proving the published directory is
exactly the snapshot (manifest, listing, every object's bytes — the package stage's
final verification without its repair branch). The release is derived from the
**installed spec** and proven against this build's snapshot; a checkout that is an
unpublished newer revision (or an older build) is refused with the REPAIR/UPDATE
instruction (`409 system_skill.release_mismatch`) rather than registered.
Ownership is a server-owned ledger row — `SystemSkillRecord`
(`system_skill_records`: workspace, preset key, registry id, verified record id,
accepted-but-unverified `pending_record_id`, client token, the persisted
`create_request`, verified release + content digest, and the intended release as a
high-water mark), one per workspace and preset. Descriptor metadata
(`source.kind=system`, the preset key, the S3 path) is copyable and is **never**
consulted for ownership; the only proof that a record is ours is that our own
`CreateRegistryRecord` returned its id. A record is system-protected exactly when the
caller's workspace **and its current registry identity** map its id (verified or
pending) — a same-id record in another workspace, or in a replacement registry the
workspace switched to, is ordinary, with no re-registration required. `POST /api/system-agents/{key}/skill-registration`
(administrator, no body, workspace pinned like every preset write) registers the Skill
of an **active** preset; the deploy pipeline's `register` stage does the same for every
fresh install or repair after the A2A record (a failure fails the stage with the exact
reason — the job never claims a registration AWS did not confirm). Nothing registers on
startup or on a read. The write is idempotent and race-safe: on a fresh intent any
same-name Skill record is foreign by definition (this platform never created one) and
is refused before anything is persisted (`409 system_skill.foreign_record`); otherwise
the **complete** create request (token — 33–256 `[A-Za-z0-9-]` —, name, version,
descriptors including `imported_at`, tags) is committed to the row before the call, so
a crash between the AWS call and the ledger commit is recovered by replaying exactly
those bytes with the same token and letting the service answer the same id; a replay
the service does not honour (idempotency window closed, a foreign record) is refused
with nothing bound or written. An id AWS returned but whose read-back failed (a denied
`GetRegistryRecord`, say) leaves the row `accepted` with `pending_record_id`:
protected as ours, never projected as registered, verified by the next attempt without
a second create. Concurrent registrations serialize on an advisory `fcntl` lock per
(workspace, preset) and the unique row arbitrates across processes, so N racing calls
converge on one record. Under that lock the **current installed preset row is
re-read** and must pin exactly the release (version *and* digest) the caller's agent
object pins, the ledger's verified and *intended* releases must not be newer, and the
remote record's own release is read before any update — so an accepted update whose
response was lost (intent already committed as the high-water mark) is reconciled by
the next attempt as a no-op, and a stale worker holding the previous release can never
downgrade it (`409 system_skill.stale_release`). A first registration is **submitted for review, never
approved** — approval is the administrator's explicit action in the Registry. An
identical repeat is a no-op that issues no `UpdateRegistryRecord`, so an APPROVED
record stays APPROVED (a bodiless repair, a reinstall, a re-click); a newer release
updates the descriptor (bumping `recordVersion`, e.g. `1.0.0-skill → 1.1.0-skill`),
which the service resets to DRAFT — normal review again; a DEPRECATED record
(terminal) fails clearly instead of being rewritten (`409 system_skill.record_deprecated`,
recovery documented in the message). Uninstalling the preset keeps the record, the
mapping and the S3 release (other consumers may mount it); a reinstall reuses the same
record. **Protection at the service boundary**, before any AWS client or S3 object: the
console `PUT` (description *and* content, inline or staged replace), `reimport`,
`DELETE` and Skill Lab's publish (`update_record`) are refused for everyone with the
maintenance hint (`403 registry.system_skill_protected`); lifecycle actions
(`submit`/`approve`/`reject`/`disable`) are refused for members and allowed for an
administrator — the route passes the caller's role explicitly and the service default
is *not admin*, so an internal caller cannot approve one by omission; Skill Lab's
`publish_job` checks the source record first, before the multi-file split rebuilds its
`publish_skill/` directory or launches the splitter; the reserved
Skill name is refused for ordinary register/import/import-with-rename before any S3
write (`409 registry.name_reserved`; an MCP record may still use the name). Ordinary
records keep their member-operable lifecycle and edits unchanged. The record API
projection carries a server-derived `system` member (`{managed, preset_key, label,
skill_version, release_digest, path, protected_actions, admin_actions} | null`) the
console renders the SYSTEM chip from, hides edit/re-import/delete on, and shows the
lifecycle buttons for administrators only; `?view=edit` on such a record is a
read-only summary; USE IN NEW AGENT keeps its APPROVED gating; `GET /api/system-agents`
reports `skill_registration` (ledger mapping) and `can_register_skill` for API
callers. The console manages the record **in the Registry**: the System presets card
carries no Skill-record row, Registry link or REGISTER/VERIFY SKILL button; the record
is created/rolled forward by the deploy's register stage on every install or
re-publish, and `POST …/skill-registration` remains for an explicit API registration.

**Constrained tool surface.** The harness exposes `shell` and `file_operations` to
every session unless `allowedTools` restricts them. Launchpad always sends that
member on deployment: `AgentSpec.allowed_tools=None` derives exact selectors from
the final resolved tool configurations, Skill loading and explicit `native_tools`
choices. A new Managed Harness (both create wizards and architect proposals) starts with
both native tools selected; `AgentSpec.native_tools` itself still defaults to empty, so
an API spec or a stored agent never gains shell on redeploy. A completely empty selection
sends `[]`. Architect proposals may also mount the AgentCore Code Interpreter
(`builtin_tools: ["code-interpreter"]` → a `builtin` tool, allowed as `@code-interpreter`;
the model calls it `code_interpreter`, the name evaluation rules may use).
An explicit `allowed_tools` list remains an expert override (entries are 1–64 chars
matching `*|@?name(/tool)?`). The preset retains its explicit
`["shell", "file_*", "@aws_knowledge"]` override: the Harness sandbox shell, the file
tools its skill needs, and the public AWS Knowledge MCP server
(`https://knowledge-mcp.global.api.aws`, a `remote_mcp` tool, no credential). When knowledge bases are mounted, the deployer appends `@<kb gateway tool
name>` (`@launchpad_kb_gw`) — only then, and never `*` — so the retrieval tools
the prompt names are callable. `allowedTools` scopes LLM tool selection only; the
real boundary is the per-agent execution role: model invoke, `s3:GetObject` on
exactly the versioned skill prefix, telemetry — and nothing else for the docs-only
preset. The MCP ToolRef carries `auth: "none"`, which tells the role derivation to
skip the workload-identity and token-vault statements an authenticated MCP ref
gets (ordinary agents' MCP refs are unchanged). Mounting a KB adds **exactly the
three statements the harness devguide lists for an OAuth2 credential provider**
("Execution role policy → OAuth2 credential provider", read 2026-09-12),
instantiated for the KB gateway's real provider from the workspace's
`oauth_provider_arn`: `GetResourceOauth2Token` on `token-vault/default`,
`workload-identity-directory/default` and `…/workload-identity/harness_<name>-*`;
`GetResourceOauth2Token` on the provider ARN itself; and `secretsmanager:
GetSecretValue` on `bedrock-agentcore-identity!default/oauth2/<provider>-*` (the
provider-scoped secret **is** required and is kept). No `GetResourceApiKey`, no
`GetWorkloadAccessToken*`, no family-wide `bedrock-agentcore-identity!*` secret, and
no direct `bedrock:Retrieve` / `AgenticRetrieveStream` — a harness reaches the KB
through the gateway, whose connector role performs the retrieval. Installing with a
KB is refused (`409`, requirement `missing_oauth_provider`) when the workspace has
no provider to scope to. Ordinary agents keep their historical policy shape. The
wizard displays and preserves a stored expert override until the operator explicitly
switches to selection-derived access. Native checkboxes do not falsely claim to
override a preserved wildcard. New architect proposals expose only the bounded
`native_tools` choices, bind them and `resources.tool_access_policy=selected-v1` to
approval, and cannot submit arbitrary allowedTools patterns. Historical records are
not rewritten; old approval bindings require a fresh review if their meaning differs.
User-authenticated Gateway invocations read the deployed aliases and remap narrow
selectors to the request's Gateway name without broadening their tool suffixes.

**Memory.** The `short_term`/`long_term` flags cannot express "short-term only"
against the real API: the shared workspace memory carries long-term strategies,
and an *omitted* `memory` member on CreateHarness means the harness-managed
default, which creates a memory with the SEMANTIC + SUMMARIZATION strategies
(`HarnessManagedMemoryConfiguration`; its strategy list has a minimum of one). The
preset therefore sends `memory: {"disabled": {}}` — no persistent memory at all,
no memory grant on its role — and a new session's requirement baseline is
independent of every earlier one by construction. Conversation inside one runtime
session lives in the harness session (the service model describes memory as
persisting context *across* sessions); confirming that within-session continuity
is part of the pending live smoke. As a consistency fix, every flag-less harness
spec now sends the explicit `disabled` variant on create as it already did on
update.

**Optional knowledge base.** The install body may name existing, already-authorized
knowledge bases (`knowledge_bases: [{kb_id, name, description}]`); they mount through
the ordinary harness KB gateway path. Nothing is created automatically, and the skill
says so: with no retrieval tool the agent works from its methodology index and states
that the original guide was not consulted.

**Administrator choices** are the model (`model_id` + `model_source`, defaulting to
the platform's `DEFAULT_MODEL_ID`) and the optional knowledge bases (plus the SE-040
inference/loop settings above). The panel's INSTALL sends `{}` (the preset defaults);
afterwards an administrator changes them on the shared configure page (CONFIGURE) or
through the install API body. The knowledge-base references are shape-validated at request time and **verified in
the provision stage** (`GetKnowledgeBase` in the target workspace: exists, MANAGED,
ACTIVE) before any gateway target is created, failing the stage with an actionable
reason otherwise. Ordinary use never overwrites version or configuration.

**Pending live validation.** Everything above is hermetically tested (`tests/
test_system_agents.py`); the live smoke — install in an approved workspace, confirm
the S3 skill actually loads under the `allowedTools` restriction, the AWS Knowledge
tool answers, a member cannot delete/redeploy, and a repeated install creates no
duplicate — has **not** been run yet and is required before the preset is called
operational. If the harness's skill-loading tool turns out to need a name outside
`file_*`, add it to `ARCHITECT.allowed_tools` rather than widening to `*`.

### Architect assistant (SE-039) — reviewed, idempotent Harness proposals

The **architect assistant** (`/create/assistant`, reachable from the Managed Harness
entrance card and from the SYSTEM PRESETS panel once the preset is active) is a member
conversation with the protected `aws-agent-solution-architect` preset that ends in an
**inert, reviewable proposal for ONE new managed-Harness business agent**. It is a
creation assistant, not an administration bot: it never edits or deletes an existing
agent and never creates resources from conversation text alone. Explicit console
actions prepare resources, approve deployment and materialize evaluation assets.

The landing page explains **ADLC (Agent Development Lifecycle)** with a translated
six-stage diagram: define success, build, evaluate, gate/release, observe production,
and feed failure cases back into evaluation. A creation rail in normal document
flow distinguishes four required steps (goals, resources, review/approval, Harness
creation) from three optional follow-up activities (evaluation, A/B testing,
production feedback).
The rail derives progress from the proposal and deployment outcome, never from
elapsed time or opening a link. Only a succeeded job plus an active Agent completes
creation. Unsaved resource choices block approval until saved or cancelled.

Resource choices are editable **before the first approval**. Afterwards, the latest
approved proposal owns the conversation's KB/Skill/MCP selections, including during
deployment or after a failed job. The UI shows a read-only explanation and links to
Agent management for editing and redeployment. It no longer offers another creation
approval in that conversation. Preparation saves/imports and resource-changing
proposal edits return `409 assistant.resources_locked`; catalog refresh remains
available without minting resource revisions. Model revisions retain the approved
resource lists, so evaluation-plan repair can continue without changing the deployed
Agent. Historical approvals and their idempotent outcomes remain intact.

**Scope of what is supported here.** The proposal may name the agent, pick a model
(`model_id` + `model_source`), write the system prompt, choose memory flags, iteration
and timeout controls, and reference **existing** workspace resources by catalog key:
APPROVED registry MCP records (`gateway:<name>` / `mcp:<name>`), APPROVED registry
`AGENT_SKILLS` records (S3 skill paths), privately imported Skill bundles and ACTIVE
managed knowledge bases. Painpoint →
metric → golden-test tables, evaluator recommendations, assumptions and *manual tasks*
are carried as **solution content** on the revision and rendered for review; the
console renders them for review. Missing resources can be prepared through the
**Creation preparation** rail; no resource is provisioned just because the model
mentions it. Document upload belongs to KB creation, not transcript attachments.
Word/draw.io export is not supported.

**Preparation during intake.** The right rail offers a live, refreshable managed
KB list, multi-selection, and an inline name/description/file-upload window. KB
creation and upload reuse `/api/knowledge-bases`; failed uploads retry against the
created KB, and creation, upload and ingestion have distinct states. `ACTIVE`
alone does not prove that documents are indexed or that the workspace has the
ready KB Gateway needed by Harness. Existing KB selection reads indexing status
without starting ingestion. New KB upload explicitly starts its first ingestion
when the source becomes available. Closing the window retains the workspace KB;
its details page can finish setup, upload or indexing.

The same rail selects approved catalog Skills. **Create Skill in Registry** opens
`/registry?view=register&type=AGENT_SKILLS` in a new tab with the Skill form selected.
After registration and approval, the member returns and explicitly refreshes the
catalog; their conversation and unsaved selections remain intact. New Skill ZIP
uploads happen in Registry, not in this rail. Previously imported private Skill
sources remain supported. Natural-language Skill authoring is not supported. Preparation
selections persist on the private conversation and enter later turn context.
Before approval, changing selections creates a new reviewable proposal revision when applicable,
without changing an approved revision or deploying an Agent. A bounded optional
`launchpad-preparation` block supplies advisory requirement cards before a proposal
exists; its business explanations never establish AWS resource readiness.
Imported Skill paths are resolved server-side, remain private to the conversation,
and participate in the same live content-digest checks as catalog Skills.

The rail also selects approved Registry **MCP servers**, including attachable
Gateway records. It shows attachment failures separately from unavailable runtime
tool discovery, and exposes the exact callable names for evaluation review.
**Create MCP in Registry** opens the MCP form in a new tab; members register and
approve there, then return and refresh. Saved `tools` keys participate in the same
proposal bindings and derived Harness `allowedTools` as other selected resources.
Explicit `[]` clears MCP choices; older preparation requests that omit `tools`
preserve existing choices. Historical KB/Skill-only preparation inherits MCPs from
the latest valid proposal until explicitly changed.

An MCP change requires a fresh proposal/evaluation review. Incompatible global
zero-call rules are rejected, and positive tool allowlists, required counts and
sequences are checked against the selected runtime catalog at plan save and
materialization, as are scenario expected tool trajectories. Unknown catalogs
remain unresolved. A deliberately narrow
business allowlist may omit MCP write functions; the platform never expands it
automatically. Existing evaluator assets and results remain bound to their
original review, and adding an MCP does not enable native tools.

Legacy shared-Gateway Registry records stored short names such as `get_employee`.
Catalog reads restore `hr-database___get_employee` only when shared-Gateway
discovery verifies that exact callable name; missing or ambiguous names make
the evaluation catalog unavailable without preventing ordinary attachment.
This is a read-only projection and never widens a record to all tools of the
Gateway. New default Registry descriptors retain the full names from discovery.

**Conversation model.** `POST /api/assistant/architect/conversations` opens a
conversation bound to `(workspace, owner principal)` and snapshots the workspace
catalog: the registry-attachables + KB reads the create wizard performs, **plus** the
deployment identity an approval will pin — the live gateway ARN and outbound-auth
identity per gateway record (resolved with the deploy stage's own helper), the S3
content digest of every skill bundle (sorted key/ETag/size), and the workspace
prerequisites (`memory_arn`, `kb_gateway_id`/`kb_gateway_arn`/`oauth_provider_arn`,
`execution_role_arn`). The **principal** is immutable: `user:<users.id>` for a
registered account, `config-admin` for the row-less built-in administrator,
`local-operator` with the login gate off. The username is display only — an account
deleted and re-registered under the same name is a new principal and inherits
nothing. The session cookie completes the same boundary: since this change it is
**version 2** and names the registered account's immutable `users.id` (the config
admin's cookie carries no id — it is its own principal), so a deleted account's cookie
authenticates nobody and can never resolve to the account that re-registered its
username; version-1 cookies are refused, i.e. **every signed-in member and admin must
log in once again after this upgrade**. An approval additionally requires the
principal that started the request, the principal re-resolved at the claim and the
conversation's owner principal to be identical; a row with a NULL principal (pre-principal ledger) is visible to nobody, it
is never adopted by a username match. Every read and write is principal-bound on top
of the workspace scope — another member's or an administrator's request answers 404 —
because the pasted Workshop material is customer input, not a shared workspace
resource. The preset runs with persistent memory
**disabled**, so the assistant does not rely on service-side session continuity: the
transcript lives in `assistant_messages`, each turn mints a **fresh 64-hex runtime
session id** and replays a bounded window of the transcript through
`InvokeHarness.messages` (`[{role: user|assistant, content: [{text}]}]`). Turns are
**paired by turn number** (never by insert order): each replayed turn is the member's
text plus the reply when there was one, a failed/interrupted turn is replayed with an
explicit "no reply" marker. The member may **edit & retry** the latest failed turn
(`retry_of_turn`, accepted only for the latest turn and only when it failed): that
exchange stays in the ledger and the thread, marked with a `turn_retried` error row,
but is never replayed again — a message that keeps the model from answering (it can
return an empty `end_turn`) must not poison every later turn. Before that, the server
**replays a turn itself** once (`TURN_TRANSIENT_RETRIES`) when the Harness stream fails
transiently — cut short after a tool step or with nothing at all, a mid-stream
`runtimeClientError` / `internalServerException`, throttling, a 5xx (the evaluation
classifier `transient_invoke_error`), or when a model call stops at `max_tokens`
(`harness.execution_limit` with that stop reason — live 2026-10-05 a proposal call stopped
there 40 s in, far short of its 65 536-token budget, and the manual retry answered); an
execution timeout or an iteration limit is never replayed: the failed
attempt's partial text and tool rows are discarded, any inline submission it made is
forgotten, the replay runs in a fresh session reserved on the ledger by a hidden
`role = session` row before its data-plane call (the first session stays on the user
row, so both remain private), and an SSE `retry` event tells the console to clear what it
streamed (live 2026-10-04: about one architect turn in five failed this way and a manual
retry always worked). One **final** budget (`MAX_REPLAY_CHARS` = 160k
characters, ≤ 12 turns) covers preamble + catalog + replayed turns + the current
message: the newest turns that fit are kept, the number of omitted older turns is
disclosed in the preamble and in the `meta` event, and the current message is never
truncated — one that cannot fit (or exceeds 100k chars / 300k bytes) is refused with
`413 assistant.prompt_too_large` before any claim. The server-composed protocol
preamble (rules + the catalog keys + which memory modes exist here) rides on the
first user message; the harness request carries no `systemPrompt` or `model` override
(its `tools` / `allowedTools` override is described below). Nothing private is written to
shared long-term memory. Whether the model follows the replay/protocol faithfully is
part of the **pending live smoke**.

**Proposal submission, validation and revisions.** A proposal is checked when it is
recorded against everything the member will later meet: shape, catalog references,
and — `evaluation_plan.draft_plan_errors` — the evaluation plan `prepare_plan` will
draft from it (`validate_plan` + `rule_catalog_errors` on the draft, including the
evaluators the platform adds to the seed and the one-turn scenarios it drafts for
unseeded golden tests; only the review a member owes a drafted scenario is exempt). So
"valid proposal, invalid plan" cannot surface after approval any more. Each turn's
preamble carries the **current stored proposal** — the exact stored JSON of the newest
revision with real content (`patch_base`; any status, so an invalid revision is
corrected in place and an approved one is iterated after deployment), plus its errors
when invalid — and replayed replies have their proposal blocks replaced by a marker, so
the proposal is in the request once instead of once per earlier reply. A later revision
is a **change**: `{base_revision, operations}` with RFC 6902 `add` / `remove` / `replace`
/ `test` operations (`proposal.apply_patch`, all or none, `base_revision` must be the
offered base), validated like a full proposal afterwards. Delivery: the turn reads the
preset's deployed tools back (`GetHarness`; `InvokeHarness.tools` *replaces* the
configured list) and offers them plus the **`submit_proposal` inline function**
(`app/assistant/submission.py`) with `allowedTools` + `@submit_proposal` — live-verified
2026-09-29: a plain name matches builtins only and `@inline_function/<name>` matches
nothing. The Harness pauses at the call (`stopReason: tool_use`), `invoke_harness_events`
yields a `handoff` with the complete input, and the producer answers with Launchpad's
own verdict (`accepted`, or `rejected` + errors + the candidate's provisional revision)
as a `toolResult` on the **same session**; the model fixes a rejection with a change
against that candidate before the reply ends (≤ 8 submissions per reply). The candidate
validator applies the same preparation merge as `record_proposal`
(`service.effective_raw`), and nothing is stored mid-reply: the last submission of a
completed reply becomes the turn's revision (a fenced block in the same text is
ignored), an interrupted reply stores nothing. The transcript's tool row shows a summary
(`proposal <name>` / `proposal revision (N edits)`), never the JSON. If the read-back
fails the turn runs without the tool and the fenced protocol — `launchpad-proposal`, or
`launchpad-proposal-patch` for a change — applies unchanged; a change that does not
apply becomes an invalid marker revision, which leaves the previous base in place. The
model is told never to mention the tool, patches or operations to the member; the
member-facing copy (the evaluation plan's "let the assistant repair" prompt included) is
unchanged. Live (dev, 2026-09-29): a submission with an invalid name was rejected,
corrected with a one-edit change and stored as a valid draft in one 19 s reply; a
follow-up prompt change landed as a one-edit revision in 8 s.

**One in-flight turn, private runtime sessions.** A turn is an atomic conditional
claim on the conversation row (`active_turn` + a random `active_turn_token`, taken by
the first statement of a short write transaction): a second concurrent turn is
refused with `409 assistant.turn_in_progress` before it opens a stream (a lost race at
the claim itself is the same error inside the stream, never a fabricated second turn).
A claim older than `TURN_CLAIM_TTL_S` (30 min) is taken over by the next ordinary
turn request; every write the previous holder would make (partial answer, final
reply, release) is conditioned on its token, so a worker whose claim was reclaimed
publishes nothing (`assistant.turn_superseded` in its own stream). Startup clears all
claims. Cleanup is owned by the response object (`TurnResponse`), not by garbage
collection: on completion, an ASGI 2.0 disconnect or an ASGI 2.4 send failure it
closes the upstream event stream (unblocking a pending read), closes the body and
`run_turn` generators, persists the partial answer as an `interrupted` turn and
releases the claim. The per-turn session id is
written to the user row *before* the data-plane call, so it is private from the first
instant the harness could know it. The generic entrances — console Chat, `POST
/api/agents/{id}/invoke`, `/v1` sync and stream — call
`app.assistant.sessions.refuse_assistant_session` and answer `404
chat.session_not_found` for such an id on a system-managed agent; ordinary agents pay
no ledger read. Chat sessions/history never list assistant turns (no `ChatSession` /
`ChatMessage` row is written). **Observability** applies the same principal boundary
after its per-workspace cache: session and trace lists drop rows of another
principal's assistant sessions, session/trace detail and on-demand evaluation answer
404 when the payload names such a session anywhere (span attributes, transcript,
message events), while the owner still sees their own turns and ordinary agents'
sessions stay shared (`app.assistant.sessions.PrivateSessions`). A stream that errors
or is closed by the client before the reply completed persists the partial answer
plus an `error` row (`interrupted …`), never derives a proposal from the incomplete
output, releases the claim and closes the upstream event stream (closing the
transport does not claim the service-side computation stopped).

**Read-only evaluation semantics.** A proposal's `tools`, `knowledge_bases` and
`skills` together describe its tool capabilities. KB retrieval and Skill loading
can produce tool spans even when `tools` is empty. Proposal-seed and evaluation-plan
validation reject a global unqualified `tool_count max=0` or exact empty
`tool_sequence` against those capabilities or explicit expected tool trajectories.
Named business-write prohibitions remain valid; refusal-specific requirements belong
in scenario assertions. Known managed code evaluators are checked again when bound
as existing references and before an ordinary evaluation run, so historical
zero-call evaluators cannot silently score a tool-enabled agent. These checks do not
rewrite prior approvals, results or Lambda versions.

Positive tool allowlists use exact runtime callable names. The assistant catalog
reads bounded, paginated `tools/list` results for approved remote MCP attachments
and exposes the Harness namespace (`<attachment-name>_<tool-name>`); Gateway names
come from the approved Registry tool descriptor. Missing catalogs are explicit,
not guessed. KB and Skill support names follow the same platform helpers as deployment.
Proposal/plan validation rejects attachment selectors such as `mcp:aws-knowledge`
inside literal tool-rule fields and positive allowlists that omit mounted support
tools. Saving and approving a plan additionally check its allowed names against the selected
conversation catalog. Named business-write bans remain independent of discovery.
Native Harness `shell` and `file_operations` must be selected in `native_tools` (both
are by default for a new Harness) (or admitted by an explicit expert runtime override) before an
evaluation rule can permit or require them. Availability in the catalog does not
grant runtime access. A tool-name check cannot distinguish read-only shell commands
from writes. Skill loading is retained automatically; additional Skill files or
scripts may require explicitly reviewed file/command capabilities.

Ordinary Agent and assistant-proposal execution budgets default to **600 seconds** and
**100 iterations** (`max_iterations`; multi-step research needs dozens of tool steps).
Explicit user budgets and preset-specific defaults remain explicit. For Harness,
Launchpad sends `timeout_seconds` as the native `timeoutSeconds` parameter; AWS
enforces it independently of the console SDK's socket-read timeout. Sync invocation,
chat, public streaming and architect discussion share Harness stop validation.
`timeout_exceeded`, cancellation and execution limits are errors even after partial
text or an earlier model `end_turn`; ordinary `tool_use` / `tool_result` cycles
continue. Failed replay stops before `StartBatchEvaluation`; the run error retains
the service error code, stop reason, scenario ID and attempted Harness session ID,
even when that failed scenario was not appended to the completed-session list.
The evaluator handler
classifies cancelled/timed-out trace evidence as `INTERRUPTED`, never a completed
answer. Historical AWS `COMPLETED_WITH_ERRORS` runs retain ledger status `completed`
plus their error, but the console presents them as **completed with errors**, keeps
partial scores and does not recommend them as a clean experiment baseline.

**Inert proposals.** After an ordinary model turn the reply is scanned for exactly
one fenced block tagged `launchpad-proposal` (`app/assistant/proposal.py`). The block
is untrusted: every assistant write is first bounded at ingress (`AssistantBodyCap`, a
pure ASGI middleware refusing bodies above 512 000 received bytes with `413
assistant.request_too_large`, whatever Content-Length claims; unknown outer request
members are refused, not ignored), then one serialized-UTF-8 **byte cap** (64 000
bytes) applies to the model block and to a member edit alike, *before* validation and
before anything is stored — and again to the **normalized** content that is actually
stored and hashed (defaults filled in), so no over-cap blob is ever kept
(an oversized member edit is `413 assistant.proposal_too_large`; an oversized model
block is an `invalid` revision that keeps only a marker); `ProposalContent` is a
Pydantic allowlist with `extra="forbid"` and per-field/per-item bounds — no `env`,
`code`, `requirements`, `allowed_tools`, `protocol`, `filesystem`, `network`, URLs,
ARNs, S3 prefixes or roles can pass. A proposal that names no model gets
`PROPOSAL_DEFAULT_MODEL_ID` (`global.openai.gpt-6-sol`, the Create Agent wizard's
default; an approval is always a managed Harness, so a non-Claude default is safe),
not `AgentSpec`'s stored-spec fallback. `memory` is `"disabled"` or `"workspace"` — the
only two states the Harness API can enforce (`{"disabled": {}}`, or the workspace's
existing shared AgentCore Memory with every strategy it carries); a "short-term
only" opt-out is not representable and is not offered. References are validated
against the conversation's catalog snapshot **including prerequisites**: a gateway
tool needs a resolved gateway ARN + outbound-auth identity, a skill needs readable
bundle content, a mounted KB needs the workspace's **existing** ready KB gateway +
OAuth provider (mounting a KB where none exists is manual work — this flow never
creates a gateway), `workspace` memory needs the shared `memory_arn`; reserved preset
names and the `launchpad-`/`harness-`/`system-` prefixes are refused. The snapshot also
lists the **ready-made evaluators** a seed may adopt as `kind: existing` — the AWS
built-ins (static) plus the account's ACTIVE `ThirdParty.*` evaluators from one read-only
`ListEvaluators` (custom evaluators are excluded; a failed listing degrades to the
built-ins with a warning) — and an `existing` id outside that list is a reference error.
The protocol makes that list the first choice (built-in / third-party → per-scenario
assertions → custom judge or code rule only for what nothing listed scores) and asks for
a deliberately **minimal first-version `system_prompt`** (≤ 50 characters: who the
agent is and what it does; red lines, escalation, tone and tool instructions stay in
golden tests, evaluators and `requirements_baseline`) because the prompt is iterated afterwards through
Evaluation → Optimization rather than written exhaustively up front. `to_agent_spec`
is the single mapping into an `AgentSpec`, and `resource_bindings` the single mapping
into the **reviewed deployment identity**: the spec plus, per resource, the gateway
ARN/name/record and outbound-auth identity (provider ARN, grant type, scopes — never
a credential value), the skill record id + S3 path + content digest, the KB gateway
prerequisites and the memory mode + ARN. Every emission (and every member edit
through `PUT …/proposal`) becomes a new **revision** (`assistant_proposals`) whose
number comes from the conversation's `revision_seq`, bumped inside the same short
write transaction that stores the row (unique index on `(conversation_id, revision)`),
so two concurrent writers never share a number: `draft` when valid (with its
bindings), `invalid` (kept verbatim with its errors, shown but never executable)
otherwise; earlier drafts become `superseded`. `content_hash` covers content **and**
bindings, so an approval names exactly what was rendered. Words like "approved" in
the prompt or reply change nothing: a turn creates rows in the transcript and proposal
tables and nothing else.

**Launch-barrier fishbone (Agent-DLC DEFINE).** The preset's skill bundle carries the
Agent-DLC five-dimension fishbone methodology (`references/fishbone-methodology.md`:
认知 / 质量 / 责任 / 成本 / 性能 + 其他, one question at a time, business language,
every note read back and confirmed, every dimension asked about and — when the
customer raises nothing — offered a scenario-derived suggested barrier to confirm,
reword or strike, solutions parked, nothing recorded as confirmed without the
customer's yes). Its product is an optional, bounded
`fishbone` member of the proposal block — metadata (customer, date, scenario,
`internal|b2b|b2c`), per-dimension coverage (`confirmed|explored_empty|unresolved`),
barriers (`sticky_text`, evidence, redacted quote, `confirmed`, `selected`) and a
parking lot — validated cross-field like the rest of the contract (exactly the six
dimensions, ≤ 3 selected per dimension, selected ⇒ confirmed, coverage consistent with
the notes, an `unresolved` dimension never blank — it carries the unconfirmed
suggestion, drawn as a dashed "awaiting confirmation" note — and ≥ 1 confirmed barrier;
a violation is an *invalid* revision) and omitted from the stored content
when absent, so older revisions hash unchanged. It is inert: the console renders it as
an SVG fishbone in the Proposal panel (`FishboneDiagram`, self-contained markup with
DOWNLOAD SVG / JSON; the JSON has the shape of the skill's `fishbone-data.json`), a
member edit carries it through unchanged, and nothing on AWS reads it. There is no
draw.io template dependency.

**Clearing a conversation clears what it created.** The History panel's CLEAR
(`GET …/footprint` → confirm → `DELETE …/conversations/{id}`, `app/assistant/purge.py`)
never deletes only the transcript: the footprint lists every Agent an approval deployed,
the **canaries and experiments run on those Agents** (with the A/B test, online
evaluations, dedicated gateway and named endpoints each canary owns), every
evaluation-assets operation with its live cloud resources and the local Datasets those
operations created, plus what blocks the purge (a streaming turn or Skill import, a
queued / running / cleaning operation, a live deployment job, a canary or experiment
action still running) and the newest clear attempt (`purge`). A transcript-only
conversation is deleted inline; anything else becomes a **durable clear job**
(`purge_assistant_conversation`, `202 {job_id}`, polled via `GET /api/jobs/{id}`,
resumed on startup by `resume_pending_jobs`) claimed by `assistant_conversations.
purge_job_id`. The job pins the ownership evidence (Agent / canary / experiment /
Dataset ids) and composes the existing single-resource paths in dependency order,
fenced before every step and **verified by AWS readback**, never by a delete response:
each canary's `act_cleanup` under its own `running_action` claim, then a bounded wait
until `remaining_resources` reads the A/B test, online-eval configs, gateway and BOTH
named endpoints back gone (`DeleteHarness` answers `ConflictException` while a named
endpoint exists — issue #246; a canary whose status already says `cleaned` is still
read back); each experiment's `act_cleanup`; the fenced `cleanup_operation` per
operation; the local Dataset rows (an AWS copy a member synced by hand stays); per Agent
the method-specific resource delete (refused up front when its Harness carries a named
endpoint no canary of the conversation owns), a bounded wait until `GetHarness` /
`GetAgentRuntime` is not found, then its own execution role, the A2A Registry record its
deploy registered (deleted and read back gone; a record id that now names another
record is kept) and the ledger; the
canaries' retained candidate zips; and only then the ledger rows. A `skipped` cleanup
result, a resource still `DELETING` after the wait, a `DELETE_FAILED`, a conflict, an
unowned dependency or a refusal stops the job as `failed` with an actionable
`payload.blocker` — the conversation and its evidence stay, and the next CLEAR starts a
new job that carries the inventory, verified checkpoints and accepted delete requests
forward. While the job is live every conversation write answers `409
assistant.conversation_clearing` and its Agents refuse canary / experiment / redeploy /
convert / delete (`409 assistant.agent_clearing`). Owner-bound; a member may clear a
bare transcript, while cloud assets additionally need `perm:agents.deploy` and an Agent
`perm:agents.delete` (`403 auth.permission_required`; the footprint reports
`required_permissions` and the caller's `can_clear`) — the same bar as the individual
cleanup / agent-delete routes. Evaluation runs and canary / experiment rows already
recorded keep their rows.

**Approval — the only executor.** `POST …/proposal/approve` (`perm:agents.deploy`,
the same permission as `POST /api/agents`, re-asserted in the handler) names
`{revision, content_hash}`. The **exact requested revision** is resolved first: an
already-approved one returns its recorded outcome (`200 started:false`) even when
newer revisions exist — that is the idempotent retry; a hash mismatch or unknown
revision is `409 assistant.proposal_stale`; `invalid`/`rejected`/`superseded` is `409
assistant.proposal_not_approvable`. Snapshot preflight (permission, readiness) is
followed by the **live** reads, outside every lock: the catalog is re-fetched, the
content re-validated (`409 assistant.proposal_invalid` for a removed resource or lost
prerequisite) and `resource_bindings` recomputed — it must equal the stored bindings
byte for byte (`409 assistant.bindings_changed`, `detail.changed[]` names the drifted
parts: a key that now resolves to another URL, another gateway auth identity,
overwritten skill bytes under the same S3 prefix, another memory or KB gateway).
Then **one short write transaction** whose first statement takes the conversation's
write lock: the caller's account, deploy permission and workspace grant are
**re-resolved from the database** (`resolve_identity` + `_authorize`) and the
workspace readiness re-read — a revocation that happened during the catalog read is
honoured (`401/403`, nothing written); the revision is re-read (approved meanwhile →
its outcome; changed → stale); the agent name is **claimed atomically** through
`agent_name_claims` (a unique `claim_key`), the same reservation `POST /api/agents`
and `…/convert` make, so an assistant approval racing an ordinary creation or another
conversation's approval yields exactly one agent and one `409 agent.name_exists`
(legacy agents predating the table are still caught by the holder query, never
duplicated or deleted); a compare-and-set `UPDATE … WHERE status='draft'` stamps
`approved`, approver and time; the ordinary `Agent` row (`owner` = approver, no
`system_key`) and — through `create_deployment(commit=False,
payload_extra={"assistant": {conversation_id, proposal_id, revision, approved_by,
content, bindings}})` — the `Deployment` + `deploy_agent` `Job` are flushed and their
ids written **onto the proposal in the same commit** (no post-commit bookkeeping can
fail). A refusal or an `IntegrityError` inside the transaction rolls everything back
and re-reads the revision: a racing approval of the *same* revision wins → the loser
returns the winner's outcome, never a name conflict. Only the claim winner launches
the job thread (`202 started:true`); a repeated approval of a job that is still
`queued` with no live worker **re-wakes** it (`start_deploy_async` coalesces one live
worker per job in this process, so a retry can never run the pipeline twice), and
`resume_pending_jobs` picks a queued job up at startup. A failed deploy stays failed
on its original job; the assistant never restarts it, and a new proposal must use a
name that is still free. Conversations are bounded (200 turns, 50 revisions → `409
assistant.conversation_full`).

**Pinned execution.** The deploy job runs the normal pipeline, but an assistant
job carries its reviewed `{content, bindings}` into the stages (`scratch.assistant_pin`)
and the Harness request is built **from the pin**, never re-resolved: gateway ARNs and
outbound-auth identities, the memory ARN (or the explicit `disabled` opt-out) and the KB
gateway come from `bindings.resources` exactly as approved. Three fail-closed checks
guard the writes: at job entry (`assert_job_bindings_pinned`: stored spec = pinned
spec, content still valid, live bindings = pinned, KB gateway unchanged), in
`generate`, and again immediately before `CreateHarness` (`_verify_pinned_resources`:
the live gateway resolution still equals the pinned ARN/auth, the S3 skill bundle's
content digest still equals the reviewed one — changed skill bytes under the same
prefix are refused rather than deployed). A KB mount uses
`kb_gateway.lookup_existing_kb_gateway` — the workspace's **existing** gateway must be
READY and carry the reviewed ARN; a missing, not-ready or drifted gateway is an
actionable failure. The list-and-create helper is never called on this path
(configuring the per-agent retrieval targets on that existing gateway is the
permitted mount operation). Job eligibility is durable: a launch claims
`queued → running` with one conditional UPDATE, only the startup resume may adopt a job
a dead process left `running`, and a terminal job is inert — a stale approval retry
that re-wakes it runs nothing.

**Exact execution and cleanup (review 3).** The stages **consume** the pin instead of
re-resolving: the Harness request carries the reviewed gateway ARNs and outbound-auth
identities, the reviewed memory ARN (or the explicit `disabled` opt-out) and the
reviewed KB gateway. **Skills are deployed from an immutable copy, never from the
mutable source**: at review the catalog snapshots the *exact directory the Harness
loads* (a legacy `…/SKILL.md` source is normalized to its parent, so every sibling
object counts) and hashes the real bytes of every object (`source_prefix`,
`content_digest`, `object_count`, `total_bytes`); the approved `package` stage reads
those bytes again, refuses when they no longer hash to the reviewed digest, publishes
them as a content-addressed copy under the workspace's own artifacts bucket
(`assistant-skills/<digest16>/…`, conditional `If-None-Match: *` writes, an existing
object must carry identical bytes, nothing is ever deleted), and switches the agent's
spec, the job pin (`skill_copies`) and the request to the copy URI, which is re-hashed
right before `CreateHarness`. A KB mount verifies, before any IAM/target write, that
the workspace's **existing** gateway (never listed-and-created) is READY and still has
the reviewed id, ARN, URL, inbound authorizer type and configuration
(`lookup_existing_kb_gateway`). Every "winner" answer of an approval — before the
catalog read, after it (including when the catalog read itself fails with a live
registry error; `502 assistant.catalog_unavailable` when there is no winner) and inside
the transaction — first re-validates the caller (current session, permission, grant,
readiness, immutable principal equality with the conversation owner); authorization
and ownership errors are never converted into a success. A turn whose owner is still a
live request of this process is never taken over whatever its age (`_LIVE_TURNS`);
TTL takeover is for orphans of a dead process, and every write of a turn (user, tool,
reply, proposal) is fenced on the claim token. The durable claim and its local live
publication are one acquisition under the registry lock (no observable "claimed but not
yet live" window); the first user row and the data-plane call are fenced on current
ownership too; and every exit of a turn — completion, early error, claim loss — closes
the upstream stream and boundedly joins the producer thread. A KB mount's full reviewed
gateway identity/readiness check runs before the execution role or any target is
created. The upstream event stream is consumed
by a producer thread while the response generator waits at most one heartbeat (SSE
keep-alive), so a client disconnect (ASGI 2.0 or a failed ASGI 2.4 send) is observed
within a second, closes the upstream — unblocking a pending read — and the response
object finalizes the turn. Observability keeps **every** content event's
`session.id` (record attributes or nested `resource.attributes`, merged per span into
`meta.session_ids`), so a private session named only by a content event stays hidden
before and after the cache.

**Console.** The page (`pages/CreateAgentAssistant.tsx`) shows the transcript with
streaming (the raw proposal block is replaced by a pointer to the panel), the catalog
summary with the workspace's memory/KB-gateway capabilities, the proposal (fields,
**exact bindings** incl. gateway auth identity and skill content digest, prompt,
solution content), an inline typed editor (tools/skills/KBs picked from the catalog,
memory `disabled`/`workspace` only where the workspace has a shared memory), CANCEL
PROPOSAL, APPROVE & DEPLOY, and the deployment outcome. Staleness is handled by an
**operation generation**: every conversation selection, workspace change and unmount
bumps it, and every load, stream, reload and job poll drops its result when the
generation moved on — a slower load of conversation A never overwrites the newer
selection B, a pending turn or approval reload never pulls A back, and a job poll that
resolves after cleanup neither writes nor reschedules. Load callbacks read `t` through
refs, so a locale change re-renders without re-running mount effects (drafts, edits
and a streaming reply survive it). The approve dialog is **pinned** to the
conversation/revision/hash/name it was opened on and submits exactly that; it closes
itself the moment the latest revision or hash changes (the backend's stale refusal
remains the boundary). The outcome shown belongs to the latest revision when that one
was approved, otherwise to the most recent approved revision; earlier approvals are
listed with their revision, agent and job status. The raw SSE fetch dispatches the
console's unauthorized event on 401 like the typed client. It handles the
preset-not-active state (administrator → System presets; member → ask an
administrator), the missing-permission state (approve disabled with the reason),
401/403 mid-conversation, and discards drafts across a workspace switch (the routed
subtree remounts; conversations are per workspace server-side). en + zh-CN.

**Live check still required.** `tests/test_assistant.py` is hermetic. Not yet run: a
real preset conversation with a grounded AWS answer, a valid model-emitted proposal,
an authorized approval creating a test Harness, readback and cleanup of the test-owned
resources. `make verify` alone is not proof that the model follows the protocol.

### Evaluation-assets plan (SE-047) — reviewed materialization of golden tests

A proposal's `golden_tests` / `evaluator_recommendations` stay inert solution content.
SE-047 adds a **separate, private, versioned plan** on the same conversation that says
what those recommendations become, and one **`perm:agents.deploy`-gated, idempotent
materialization** that creates the assets. The Agent proposal, its approval and the
deployed Agent are never touched; approving an Agent is not permission for cloud
evaluation resources.

**Separate actions and their effects.**

| Action | Where | What happens | AWS write? |
|---|---|---|---|
| Prepare / edit a plan | `POST …/evaluation-plan/prepare`, `PUT …/evaluation-plan` (member, owner-bound) | a new plan revision row (`assistant_evaluation_plans`), validated against the exact proposal revision + content hash it names | none |
| Ask the assistant to fix an invalid plan | `POST …/turns` with `evaluation_plan_repair: {plan_revision, plan_hash}` (member, owner-bound) | an ordinary discussion turn with server-resolved plan/errors and proposal context → a new inert proposal; the console then prepares a plan from that exact new revision for validation and review | one ordinary preset invocation; no resource creation |
| Create assets | `POST …/evaluation-plan/materialize` (`perm:agents.deploy` **and** owner; exact plan revision + hash; disclosure acknowledged) | one `evaluation_asset_operations` row → local **Launchpad Dataset** (ledger), **AgentCore evaluators**, and one **Lambda** per code evaluator, each with its own role/log group/resource policy and additive execution-role policy | CreateEvaluator, Lambda/IAM/Logs — **never** StartBatchEvaluation, CreateDataset (AWS sync), online evaluation, InvokeHarness/Runtime or a model call |
| Sync the Dataset to AWS | existing `POST /api/eval/datasets/{id}/sync-to-aws` | unchanged, explicit, separate | CreateDataset/AddDatasetExamples |
| Run an evaluation | existing `POST /api/eval/runs` (`perm:eval.run`) | unchanged, separate, billable | invokes + StartBatchEvaluation |

**Repair works on the source proposal.** The invalid-plan notice offers **ASK
ASSISTANT TO FIX**, alongside manual preparation and JSON editing. The client names
the current plan revision/hash; `assistant/evaluation_repair.py` resolves the saved
plan and its validation errors inside the owned conversation and workspace. The
prompt includes the latest proposal as the baseline and, when different, the plan's
older source proposal, so later configuration changes are preserved rather than
silently replaced by old content. The assistant is asked to repair the proposal's
`evaluation_plan` seed and related golden tests/recommendations, using actual tool
names rather than invented trajectories. Unsupported global evaluator coverage
must be corrected in the design. Ordinary prompt/replay limits remain enforced:
oversized context is refused with a JSON-editing fallback, never silently truncated.

The same turn claim, SSE cancellation and inert proposal validation are used as
ordinary discussion. Only a successfully completed turn with a new usable proposal
triggers preparation from that exact revision. The console shows the resulting
validation state, including remaining errors; there is no automatic retry or asset
creation. Unsent composer text is preserved, open JSON/proposal edits block repair,
and responses arriving after a conversation/workspace switch are discarded.

**The typed plan** (`backend/app/assistant/evaluation_plan.py`, ≤ 160 000 bytes) is
bound to `source_revision` + `source_content_hash`, hashed canonically, and carries:
`scenarios[]` (one standard predefined Dataset item per golden test — **one runtime
session** whose turns replay in order — with the original golden-test id,
turns/expected responses/assertions/expected trajectory and a `review_required` flag),
`evaluators[]` (a discriminated union of **AgentCore evaluators only**: `existing` id
reference · `judge` with pinned instructions / rating scale / model / level · `derived` ·
`code` with **declarative rules only**), `recommendations[]` (every prose
recommendation of the revision exactly once, `mapped` to keys or explicitly
`unresolved` / `declined`) and `blocked_golden_tests[]`. A golden test that nothing in
AgentCore Evaluations can score on one session's trace — memory isolation across users,
session freshness across sessions, expert sign-off, a metric baseline, an external
control — is **blocked with its reason** (the proposal seed may do so directly via
`evaluation_plan.blocked_golden_tests`) and stays a `manual_tasks` item; it never
becomes a scenario, an evaluator entry or a locally computed check. The earlier
`scenarios[].execution` procedure (a former local runner's metadata) and
the `orchestration` / `manual_review` / `metric_baseline` / `external_control` kinds are
refused with an actionable message, in a plan revision and in a proposal seed alike.
Validation further refuses: a golden test that is neither a scenario nor blocked, a
scenario still `review_required`, a recommendation missing or altered, a mapped key
that is not an evaluator, judge placeholders not documented for the level (`{context}`
`{assistant_turn}` `{expected_response}` at TRACE; `{context}` `{available_tools}`
`{actual_tool_trajectory}` `{expected_tool_trajectory}` `{assertions}` at SESSION),
`reference_response` rules outside TRACE / `reference_trajectory` outside SESSION, more
than **10** AWS evaluators and TOOL_CALL code evaluators. Nothing in the plan may carry
an ARN, a Lambda name, Python or a regex — `extra="forbid"` everywhere.

**A plan applies at most ten evaluators.** `StartBatchEvaluation` accepts ten evaluators
per batch (a service limit, `MAX_BATCH_EVALUATORS`), and a dataset run applies every
evaluator of the plan — existing and created — in one batch, so the plan and its proposal
seed refuse the eleventh (`MAX_RUN_EVALUATORS`), the run route refuses an over-long
selection with `422 run.too_many_evaluators` **before** any replay, and the assistant's
NEXT STEPS panel disables one-click start for a legacy operation that exceeds it and points
at New Run to deselect. The protocol tells the model to pick the built-ins that matter for
this agent rather than list them all.

**Evaluator selection is global; per-golden-test mapping is refused, not faked.** A
dataset run applies one evaluator list to every session and the reference envelope does
not select evaluators, so the platform cannot route an AWS evaluator to a subset of golden
tests. Validation therefore refuses any cloud/existing evaluator whose `golden_test_ids`
is a proper subset (actionable: `[]` = all golden tests, or block the golden tests it
cannot cover). A global reference-driven evaluator (`{expected_response}`
/ `{assertions}` / `{expected_tool_trajectory}` placeholders, `reference_*` code rules,
`Builtin.Trajectory*`) is accepted only when EVERY scenario — and, at TRACE, every turn —
carries that reference. The created Dataset item keeps the reviewed golden-test facts
(`metadata.launchpad_assets.golden_test`: id, input, expected response, expected tools,
forbidden behaviour, pass criteria, evaluator note, source), the plan-key → kind / gate /
**resolved evaluator id** map and `applies` (all global keys) — never the transcript. **Run preflight
checks the CURRENT dataset, target by target.** `POST /api/eval/runs` resolves every chosen
evaluator's ground-truth needs from its real definition — canonical builtins (including the
`Builtin.Trajectory*` catalog), a managed evaluator's recorded create request, or one
`GetEvaluator` for an unmanaged custom id — and, through the shared pure helper
`app/evaluation/coverage.py`, requires the reference on every target the evaluator will be
applied to: every session (procedure seed sessions included; scenario-level assertions /
trajectory attach only to the outcome session, exactly as the runner's
`ground_truth_for_sessions` groups them) for SESSION evaluators, every turn for TRACE
evaluators. Any gap is `422 run.judge_needs_ground_truth` naming the offending targets
(`GT-003#r1/a1 lacks expected_tool_trajectory`, `GT-002/turn 1 lacks expected_response`)
before a run row, invoke or StartBatchEvaluation; a dataset edited after materialization is
therefore re-checked at run time. A custom evaluator the preflight cannot read has
**unknown** needs, not verified ones: a GetEvaluator failure is `422
run.evaluator_unverifiable` (NotFound: `422 run.evaluator_not_found`) before any run row,
queue entry, telemetry read, invoke or batch — the old fail-open ("the service enforces it
too") would already have invoked the agent for every scenario by the time the service
rejected the batch. Simulated persona items (`actor_profile`) have no predefined turns: the
helper emits one explicit `<scenario>/simulated turns` trace target carrying only the
session-scoped `assertions` / `expected_trajectory` the metadata composer really sends, so
a TRACE evaluator reading `{expected_response}` is refused (never passed vacuously on an
empty turn list) while session assertions known upfront stay valid. The same helper binds
an *existing* evaluator reference at materialization: its configuration is validated
member by member against the **installed** control-plane model (`EvaluatorConfig` and every
nested shape — the `RatingScale` / `EvaluatorModelConfig` / `CodeBasedEvaluatorConfig`
tagged unions need exactly one non-empty branch; every scale entry needs `definition`,
`value`/`label`; `modelId` is required on both the Bedrock and the Responses branch; typed
inference options, the Lambda ARN pattern and the 1–300 s timeout are enforced; unknown
members anywhere are refused, while documented optional provider fields such as
`inferenceConfig`, document-typed `additionalModelRequestFields` and Responses `reasoning`
bind), its needs are decoded from that configuration — or, for a code evaluator this
platform created in the **same workspace**, from its owning plan's recorded rules
(`source: managed`; another workspace's association is never read) — and a reference the
plan's scenarios cannot feed leaves the resource `conflict`, never `ready`; a truly external
code evaluator's needs are unknown and recorded as such. Observability SCORE NOW
(`observability.evaluator_needs_ground_truth`, before any span read or Evaluate call) and
online evaluation refuse managed reference-driven code evaluators outright.

**NEXT STEPS carries the agent into a config-bundle A/B on a Runtime twin.** Step 03 of
the panel under the Evaluation Assets adds a three-rung ladder: **a · runtime twin** —
CONVERT TO RUNTIME posts the existing `POST /api/agents/{id}/convert` (perm
`agents.convert`, behind a confirm dialog naming the billable resources and the per-agent
memory partition) because a managed Harness cannot consume a routed configuration bundle
and `experiment_capability` reports it `not-http-runtime`; **b · baseline** — the twin's
newest `completed` run on the created Dataset, which step 02's START EVALUATION now
produces because, once the twin is `active`, it is the **main version** of the panel (Chat,
runs and the experiment all target it; the Harness stays deployed and untouched); **c ·
experiment** — a deep link to `/evaluation?view=experiment&exp=new&agent=<twin>` with
`baselineRun=<id>` when a baseline exists. The baseline is recommended, not gated: the
link stays live without one (amber hint), and the experiment page reports trace readiness
itself. The relation is read back from the ledger through
`GET /api/agents/{id}/conversions` (the `-rt` agents whose `spec.source_harness.agent_id`
is this agent, newest first, each with its latest deployment) rather than stored on the
conversation, so a reload — or a conversion started from the Agents page — lands in the
same state; the panel polls it while a twin is `deploying`. A workspace allows one running
experiment, so the ladder names a running one and links to it instead of letting the
start 409.

**Drafts never turn prose into a scenario the model did not type.** A proposal may carry
an optional structured `evaluation_plan` seed — typed single-session `scenarios` (turns,
references), typed AgentCore `evaluators`, `recommendation_keys` and
`blocked_golden_tests` (golden tests the model itself declares unscorable by an
AgentCore evaluator, with the reason) — validated shape-first by the same union (a
malformed seed is an *invalid* revision, never a 500; a revision without a seed
serializes exactly as before, so old hashes are unchanged). `draft_plan` uses seeded
scenarios and blocks as-is; every other golden test becomes a single-turn scenario
marked `review_required` that the member confirms, rewrites or blocks. The model-facing
protocol asks for typed scenarios, global evaluator mappings and explicit blocks in the
seed — and forbids seeding multi-actor / multi-session procedures, runner-computed
checks, human review, metric baselines or external controls as scenarios or evaluators
(those go to `blocked_golden_tests` + `manual_tasks`) — so the normal generated flow
does not require hand-written schemas. The seed runs the **same routing rules as the
plan** (`_routing_errors`: global `golden_test_ids`, every-scenario references), so an
evaluator aimed at a subset of golden tests makes the proposal *invalid* at proposal time
rather than surfacing after approval and deployment when the assets are created. A rejected model block also leaves an `error` transcript row named
`proposal_rejected`, which `compose_messages` replays to the model as the member's side
of the next turn — the member says "fix it" instead of relaying the errors — and the
skill bundle carries `references/proposal-self-check.md`, a checklist mirroring every
contract rule that the model walks before its first submission (see "Proposal
submission, validation and revisions" for the in-reply check). Seeded evaluator keys are
reserved first; prose recommendations map only to ids identified exactly
(`Builtin.*` / `ThirdParty.*`, collision-safe keys, never removed by a seed mapping of
another kind) or to the seed's explicit `recommendation_keys`; everything else stays
`unresolved`. The single drafted judge is SESSION-level, global, and scores each
scenario against **its own** `assertions` (pass criteria / forbidden behaviour) via the
`{assertions}` reference — drafted only when every scenario carries assertions — and it is
labelled `draft: true`; the deterministic
`expected_tools` rule is drafted only when every scenario names expected tools.

**Code evaluators use isolated packages with a shared reviewed handler.** `app/assistant/lambda_runtime/
handler.py` is stdlib-only (json/os), contains no `eval`/`exec`/`subprocess`/`re`/network
client, and is shipped byte-identical in every package together with a canonical
`rules.json` containing exactly one evaluator **name** → rules entry. Each new code
evaluator binds to its own published Lambda version, with independently scoped
role, log group, invocation permission and execution-role grant. AgentCore callbacks
can omit the documented `evaluatorName` and `evaluatorId`; a single-rule package
resolves that input without guessing. An identityless multi-rule package is rejected
before scoring, and known legacy ambiguous packages are refused when reused or
selected for a new run. Historical shared packages remain auditable and cleanable.
Their operation projection carries `requires_new_plan`; a retry is rejected before
queuing or consuming an attempt. The console offers a replacement draft by copying
the saved plan through the existing edit API, preserving all member edits and the
source proposal binding. It then requires ordinary review and approval to materialize
the independent resource chains. It does not regenerate the plan from the proposal
or mutate the historical operation.
New-plan review counts reflect one chain per code evaluator; historical operation
counts come from its actual recorded resources. `build_package` produces a
deterministic ZIP (sorted entries, 1980-01-01 timestamps, fixed permissions, canonical
JSON) whose sha256 is persisted on the intent and compared with the function's
`CodeSha256` and the published version's readback. Rules: `tool_count` · `tool_sequence`
(exact / subsequence) · `tool_set` (allowed / forbidden) · `output_contains` /
`output_not_contains` / `output_exact` (literal, case-insensitive by default) ·
`reference_trajectory` (observed tools ⊇ or == `expectedTrajectory.toolNames`) ·
`reference_response` (final output contains `expectedResponse.text`). Evidence is
fail-closed: the handler inspects only the target (`evaluationTarget.traceIds` at TRACE,
all spans at SESSION, TOOL_CALL refused), reads the **last assistant output** of a
model/agent span (`gen_ai.completion`, `gen_ai.output.messages`, `gen_ai.choice` /
`gen_ai.assistant.message` events; dict or OTLP list attributes) and tool names
(`gen_ai.tool.name`, `tool.name`, `execute_tool <name>` spans; an MCP client `tools/call` span (`mcp.method.name`) nested under a tool span is that call's transport, not a second call, and the logged-in chat's `launchpad_gw_user_` remote_mcp prefix is removed so rules match the catalog's Gateway names on both invoke paths); user prompts, tool
inputs and reference inputs are never read as output. A model's input history may
contain tool messages without making its current output a tool result. A Strands
chat wrapper and its single direct provider child are one logical model call when
their trace, explicit parentage, source kinds and nested time bounds agree; the
wrapper's output and both finish indicators are checked together. Conflicting
outputs, truncated/unfinished children and ambiguous relationships remain errors.
No spans, an unmatched target,
no identifiable output for an output rule, a missing reference input for a reference
rule, or a tool rule without any model/agent span → `{errorCode, errorMessage}`, never
PASS. At SESSION level `output_not_contains` is a whole-session claim and is usable only
when every assistant turn of the session is present and complete (a missing or truncated
earlier turn is an error, not a pass); `output_contains` / `output_exact` and every
TRACE rule judge the final turn only. Every finish indication of a turn (event, message,
span `gen_ai.response.finish_reasons`) must agree on a terminal stop, and a finished span
needs a valid end timestamp. Output fields are typed by their source (serialized content
blocks on model spans, plain `str(response)` on agent spans, serialized message lists in
`operation.details`), never by punctuation. Deterministic rules assert literal text and
observed tool counts/sequences only;
PII solicitation, dependency-inducing language or child safety need a calibrated judge
and human review — a passing keyword rule is not a safety certificate, and reference
rules must not score live traffic (the operation flags them `reference_dependent`).

**Durable, fenced materialization** (`app/assistant/evaluation_assets.py`). Approval
is one atomic claim: a conditional UPDATE of the plan row (still `draft`, still this
hash, still the newest revision, conversation still owned by the approver's principal,
and — for a registered account — the approver still an active, unexpired account holding `agents.deploy`,
all as predicates of that same write) in the same transaction that inserts the operation
with every intent and the **pinned workspace identity** (account, region, assume-role
ARN/external id, execution-role ARN and — when a grant is requested — the execution
role's RoleId, accepted only if the role carries the `launchpad:managed` tag, never by
name prefix); the caller is re-resolved from the database inside that transaction and
must still hold `agents.deploy` and own the conversation. A superseded / edited /
already-claimed plan is `409 assistant.evaluation_plan_stale` before any write. One
host-local `flock` per operation plus a database lease token fence the worker: before
**every** cloud write it re-reads the token, re-checks the approver and compares the
workspace row with the pinned identity, and stops (recorded, no effect) on any change.
A quick restart re-acquires the free lock and resumes at once; a live worker is never
stolen. Order: `dataset` (ledger; member edits afterwards are never overwritten) →
`lambda_role` → `log_group` → `lambda_function` (python3.12, 256 MB, timeout = max rule
timeout ≤ 300 s; wait Active; `PublishVersion` pinned to the digest and reconciled
against `ListVersionsByFunction` — exactly one published version may carry the digest,
Lambda does not re-publish unchanged code; reserved concurrency 5, no provisioned
concurrency; readback of CodeSha256 / Runtime / Handler / Role / Version / Timeout /
MemorySize / State / reserved concurrency) → `lambda_permission`
(`bedrock-agentcore.amazonaws.com` + `SourceAccount` on the version; an existing
statement is accepted only when it equals that exact scope) → `role_grant` (the
pinned role re-read: ARN, RoleId and tag must match; an additive inline policy
`launchpad-evalop-<op>` granting `lambda:InvokeFunction` + `GetFunction` on the
**published version ARN only**, never `$LATEST`; trust and other policies untouched;
document read back) → every `evaluator:<key>` (CreateEvaluator with the persisted
token, GetEvaluator until ACTIVE, id/name/level/config must equal the request; code
evaluators pin the version ARN; an `existing` reference must resolve to the same id
and be usable). A failure or conflict in a code chain marks its remaining resources
and owning code evaluator `blocked`; other code chains, judges, derived and existing evaluators still
proceed.

`AddPermission` can advance the published version's `RevisionId` and `LastModified`.
The permission step checks the recorded identities and inventory before writing,
uses the existing policy's own revision as a CAS token when present, and verifies
the whole resulting policy and function state. Only a successful write may advance
its cleanup snapshot. Every other published configuration field must remain equal,
and a changed `LastModified` must fall within the call's time window, allowing five
seconds of clock skew. An audit entry retains both revisions, modification times
and the request ID. `$LATEST`, concurrency and the version/alias
inventory must remain unchanged. A lost response, unexplained drift or a historical
pre-permission snapshot cannot be repaired merely by finding a matching statement.
Cleanup continues to require the recorded identities exactly.

**Ownership is a service-issued identity returned to this operation, never content.**
Every dispatched create is recorded durably before the call (intent / request / a
per-dispatch `create_history` for evaluators), and the identity the service answers with
is persisted immediately, before any further write: RoleId / ARN from CreateRole, the log
group's creationTime / ARN read right after CreateLogGroup (the API returns none), the
FunctionArn / RevisionId returned by CreateFunction (every approved field of the answer is
verified first), the evaluatorId / ARN from CreateEvaluator. The random *provenance nonce*
(role description + tag, log-group tag, `provenance.json` in the package → the digest) is
only a clue for a reviewer: it is copyable, so after a lost response or a crash before the
acceptance checkpoint a resource found under our name is **`unknown`** — never adopted,
never written to, never deleted, dependents retained — and a collision established at
creation time is a **foreign collision**: recorded as `conflict`, never adopted, never
deleted. A `pending` / `blocked` display status with a dispatched create is an effect that
may exist and is accounted for by cleanup. Before publishing, the worker requires `$LATEST`
to still equal the identity CreateFunction returned (RevisionId included) and passes that
RevisionId as the PublishVersion precondition; it re-pins the RevisionId deliberately right
after each of its own writes (publish, reserved concurrency) with every other approved field
still equal, and refuses a replacement before any publish / concurrency / permission write.
Evaluators
replay the service's own `clientToken`; a name conflict with a different token is
foreign. Because AgentCore evaluator names are unique per account and Region, and a second plan of the same conversation carries the proposal's names again, **preparing** a platform draft renames every judge / derived / code evaluator whose name is taken — recorded by an asset operation of the workspace that created (or may have created) it, or listed by `ListEvaluators` — to `<name>_r<proposal revision>` (then `_2`, …; ≤ 48 chars) with a note, and **approval** refuses a plan that still names a taken evaluator (`409 assistant.evaluation_plan_name_taken`) before any write. Readback drift is `conflict` too — the record stays owned (`owned: true`)
but is never repaired. Retries are explicit and bounded (5 attempts); a partial
outcome stays `partial` with each resource's error.

**Cleanup** (`DELETE …/operations/{id}/assets`, `perm:agents.deploy` + owner) runs under the same
lock, lease, re-authorization and pinned-identity checks, one persisted checkpoint per
effect and a fresh fence before every single mutation, dependency first. **A create whose
response was lost before the service-issued identity was recorded is never adopted or
deleted afterwards**: a nonce in a role description / tag, a log-group tag or a
nonce-bearing package digest is copyable content, not ownership, so while a resource with
our name exists the resource is an explicit `unknown` (worker and cleanup alike; the
operator reviews it, then retries — a retry re-evaluates without adopting), its
dependents stay `blocked` / `retained` and the operation is never `cleaned`. A collision
established at creation (ConflictException, no lost response) is a foreign resource
(`conflict`, not owned) and does not block `cleaned`. A lost `CreateEvaluator` stays
`unknown` even when `ListEvaluators` does not show the name (visibility cannot prove the
create never happened; name, clientToken and any listed candidate id are recorded); a
worker retry replays the service's idempotency token to recover or create it under our
ownership — cleanup never creates. A definite 4xx rejection is recorded as such and counts
as nothing created. Owned evaluators are deleted only when id, name, level, configuration
and ARN still equal the recorded identity (a changed one stays a reviewable `conflict`; one
locked by an online configuration stays `delete_failed`), and `DeleteEvaluator` counts only
once a NotFound readback confirms it is gone (`delete_pending` otherwise). The additive
grant, the function (with its resource policy), the log group and the dedicated role are
removed **only once that chain's evaluators are confirmed gone** (historical ungrouped
operations retain their original shared barrier) and only after the identity snapshot
recorded when their create/readback succeeded still matches exactly: RoleId, ARN, trust
policy and inline policy of the role; creationTime, ARN and retention of the log group;
for the function the recorded published version **and** the unqualified `$LATEST`
(FunctionArn, CodeSha256, role, runtime, handler, limits and RevisionId, captured after
the platform's last write), the version set and the alias set — both inventories read with
the real `Marker` / `NextMarker` pagination to a structurally valid terminal page within a
bounded page budget; an exhausted budget, a repeated or unusable marker, a page without
its `Versions` / `Aliases` list or a malformed entry is an *incomplete* inventory (never an
empty one) that fails provisioning and refuses cleanup. A pre-history evaluator request
with no id and no recorded rejection is migrated into a durable `legacy-uncertain` entry
together with the next dispatch record, before the call, so later rejections cannot erase
it. A missing published version
is **not** a missing function; a whole-function delete is confirmed by a bounded unqualified
`GetFunction` NotFound before the log group and role are touched (`delete_pending` and
dependencies retained otherwise; a pending delete is re-verified and re-driven on the next
cleanup); an incomplete snapshot is a review-required `conflict`. Otherwise resources are
`retained` / `conflict` and reported; a function that could not be deleted keeps its log
group and role; a lost delete response is a recorded `delete_failed` that the next cleanup
resolves once the resource is confirmed gone. The delete APIs carry no precondition token
(installed models), so the check→delete window is one call wide against an external
administrator. Persisted `conflict` states of the code chain gate every later retry
(dependents stay `blocked` until the operator resolves them). The local Dataset stays (a member asset removable in
Evaluation → Datasets) and every foreign resource is left alone; `cleaned` is recorded
only when nothing owned remains. The ordinary `DELETE /api/eval/evaluators/{id}`
refuses (`409 evaluator.managed_by_operation`) an evaluator an operation owns.

**Privacy and ownership.** Plans and operations are visible only to the conversation's
immutable principal (foreign principal / workspace → 404, also for administrators). The
UI shows a disclosure before creation: the **selected** inputs, expected responses,
assertions and rubrics of the plan become readable to every workspace member in
Evaluation → Datasets / Evaluators; the transcript does not. Created Dataset items
carry `metadata.launchpad_assets` (conversation id, proposal/plan revision + hash,
operation id, golden-test id, plan-key → kind/golden-tests/blocking/threshold) so the
Evaluation console can show where an asset came from; the plan-key → evaluator-id map
lives on the operation. "Created" means registered — not passed, not child-safe, not
production-ready.

**The first initialization moves the RevisionId; that is reviewed, never rebased (SE-049).**
`CreateFunction` answers while the function is still provisioning (`State = Pending`,
`StateReasonCode = Creating`) and Lambda documents `RevisionId` as the latest *updated*
revision, not an immutable identity: once the function turns `Active` its `$LATEST` may
carry a new RevisionId with every other field — `LastModified` included — unchanged. The
worker therefore keeps the **raw allowlisted CreateFunction answer immutably** on the
intent at acceptance (`create_response`: the initial RevisionId, State / StateReasonCode,
LastModified, the approved configuration, the request id when present; `initial_revision_id`
beside it), waits for `State = Active` **and** `LastUpdateStatus = Successful` (bounded;
`InProgress`, `Failed` or a missing status cannot publish and is a plain retryable failure),
records `settled_revision_id` when the RevisionId did not move, and — when the RevisionId
ALONE moved on an owned, accepted, never-published create — **settles it with evidence**
when that evidence is complete (`initialization_transition_evidence`: the accepted answer
said `Pending`, `$LATEST` is `Active` / `Successful`, `LastModified` is byte-identical to
the answer, every identity field but RevisionId and every optional configuration member is
unchanged), recording an append-only `revision_history` entry
(`initial_activation_settled`, from → to, the evidence) and the `lambda_function:settled`
event before any write; every `UpdateFunctionCode` / `UpdateFunctionConfiguration` moves
`LastModified`, so an unchanged `LastModified` is what tells the service's own transition
from a replacement. Lambda bumps the RevisionId on that transition for **every** new
function, so without this rule every code evaluator needed a manual review. Without the
evidence (a legacy record without a lifecycle snapshot, a moved `LastModified`, a changed
member) the drift still records a `conflict` tagged `review.kind = initial_revision_changed`
with the observed RevisionId / LastModified, and nothing rebases: an equal digest and role
are downloadable content. An explicit retry re-attempts exactly two kinds of conflict —
that settleable Lambda drift and a read-only `existing` binding — and leaves every other
conflict durable. The **reviewed recovery**
(`POST …/operations/{id}/lambda-revision-review`, `perm:agents.deploy` **and** owner, exact plan
hash, expected created and current RevisionId, the CloudTrail event id and a reason) reads
the nominated `CreateFunction20150331` event server-side through the workspace client
funnel (`LookupEvents` by `EventId`; exactly one well-formed record, eventually consistent
history fails closed), requires it to be THIS operation's successful create (source,
account, region, request fields, response FunctionArn / initial RevisionId / CodeSha256 /
`Pending` / `Creating` / `lastModified`, and — for a create accepted after SE-049 — equal to
the persisted answer), then requires the settled `$LATEST` to be that answer plus only the
lifecycle transition (Active / Successful, exactly the expected current RevisionId, the same
LastModified, every approved field and every optional security-relevant member equal after
folding SDK and CloudTrail casing the same way), `$LATEST` the only version, no alias, no
resource policy, no reserved concurrency, and the recorded role / log-group identity
intact. Eligibility is decided on the ledger first: partial / failed operation, pinned
identity, the Lambda intent an owned accepted create blocked **solely** by the pre-publish
RevisionId drift (a lost create is `unknown` and never reviewable; a published version, a
publish intent or a re-pinned baseline is ordinary drift), no other unrelated open outcome.
The write happens under the host lock with the caller re-resolved inside it, the approver
and pinned workspace re-checked, and one conditional UPDATE (still partial / failed,
unclaimed, this hash): an **append-only** review entry (reviewer, reason, event id / time /
request id, verified fields, old and new snapshot, plan binding — never a CloudTrail actor
or token) is added to `reviews[]`, `revision_history[]` records the move, `revision_id` and
`settled_revision_id` become the reviewed value while `initial_revision_id` /
`created_identity` / `create_response` stay untouched, and only the Lambda conflict plus its
blocked dependents are re-queued; the ordinary worker launches after the commit (a crash
before that leaves a `queued` operation that startup resume or retry picks up) and its
`PublishVersion` still carries both preconditions, so any later change fails there and is
not reviewable again (a second, different review is refused; the exact same request is
idempotent and reads nothing). An operation accepted before SE-049 stored only the
identity: it is reviewable only through the positive CloudTrail event, never from the
current RevisionId alone. The comparison is **lossless and model-driven**: CloudTrail's
casing is rebuilt along the installed Lambda service model (structure member names only —
data-map keys / values such as environment variables and tags, and empty strings, are
content; the only absent-versus-empty equivalence is a documented envelope such as
`environment: {}`), the recorded request, the immutable accepted answer, the event and the
current `$LATEST` must agree member for member (present, absent and equal alike, so an
extra `DurableConfig` / `TenancyConfig` / `CapacityProviderConfig` / `MasterArn` or a member
unknown to the platform is a difference), and every answered member must be fixed by the
request or be a documented default; presence is compared with an explicit absent
sentinel (`null` or a wrong type is a difference, never absence), every side is
type-validated against the model before comparison, envelope folding applies only to
well-formed empty shapes, and `CodeSize` is retained and compared. Dependencies are re-compared with their recorded
snapshot (trust, inline policy, tags, retention — a `ready` ledger status blesses nothing,
the worker skips ready dependencies) and a resource policy is proven absent only by a
`NotFound`. The conditional UPDATE that records the review binds every value the review relied on
— the operation's status / token / attempts / plan binding / owner / approver / exact
pinned and intents JSON, the approved plan row including the exact JSON of its
validated content, the conversation owner, the workspace identity including its exact
`resources` JSON, and the approver's and reviewer's active `agents.deploy`-holding account rows — as
predicates of that one statement, with the caller re-resolved inside the host lock. The
verified state is persisted as `reviewed_baseline` and the resumed worker re-validates it
immediately before its first mutation (configuration + tags, dependencies, inventories,
policy absence, no reserved concurrency): an externally published same-code version, a
foreign alias / policy / concurrency or any drift is a `conflict`, never adopted or
overwritten. The ordinary worker, review or not, refuses to adopt a same-digest version
when its **first** `PublishVersion` dispatch is refused (only a lost answer of its own
dispatch reconciles to exactly one version) and never overwrites a reserved concurrency it
did not set. CloudTrail remains evidence for a human, not proof that no other
write happened, and the external-administrator check→write window is unchanged.

**Live check still required.** `tests/test_evaluation_assets.py` is hermetic (IAM /
Lambda / Logs / control-plane fakes). Not yet verified against AWS: that
`bedrock-agentcore.amazonaws.com` is the principal the Evaluations service invokes code
evaluators with (the devguide documents the execution-role statement, not the resource
policy), the exact span representation the service passes in `sessionSpans` for this
platform's Harness/Runtime agents (the handler accepts the documented
`gen_ai.completion` shape plus the Strands/OTLP variants above), CreateEvaluator's
acceptance of a **versioned** Lambda ARN, and GetEvaluator's exact `status` value
(`ACTIVE`/`READY` accepted). ACTIVE registration plus test doubles are not proof that a
batch run would score. Exclusion is host-local (`flock` under `data/locks/eval-assets`):
a second console host against the same ledger is not a supported deployment for this
feature. `operation.pinned` is added by the ledger migration; an operation approved
before identity pinning existed is refused by worker and cleanup (review required — prepare
a new plan revision) rather than given invented bindings.

### Model source (方式B + 方式C)

`AgentSpec.model_source` selects the model-hosting surface: `mantle` (Bedrock
Mantle) or `bedrock` (native Bedrock). **No API key is involved on either
surface** — the agent's own execution role authenticates both. Mantle does,
however, need its own IAM grants: `bedrock-mantle` is a separate IAM service and
`bedrock:InvokeModel` does not cover it, so `infra/stacks/base_stack.py` grants
`bedrock-mantle:Get*`/`List*`/`CreateInference`,
`bedrock-mantle:CallWithBearerToken`, and Marketplace subscribe scoped to
`aws:CalledViaLast = bedrock-mantle.amazonaws.com` (mirroring the AWS managed
policy `AmazonBedrockMantleInferenceAccess`). Without them a Mantle agent reaches
ACTIVE and then fails its first invoke with `401 access_denied`; the grant is
shared by harness and zip, and adding it needs a CDK deploy.
The field defaults to `bedrock` for backward compatibility with specs
stored before it existed. The console form also starts every method on `bedrock`,
with GLM-5.3 (`global.zai.glm-5.3`, a global inference profile offered in every region the
console targets) as the default model, which `AgentSpec` also defaults to; Mantle stays
selectable for harness and zip (`MODEL_SOURCE_BY_METHOD` in
`frontend/src/lib/agent-spec.ts`). The Claude Agent SDK method defaults to the
first Claude entry instead, and the backend gives a container spec that names no model
`global.anthropic.claude-sonnet-5` (`CLAUDE_SDK_DEFAULT_MODEL_ID`). Intent clustering and
answer suggestions keep their own platform model, and the Strands Studio canvas keeps its
Claude fallback for nodes without a model (changing it would change existing flows); new
canvas nodes start on GLM-5.3. The console's model catalog lives in
`frontend/src/lib/models.ts`, where the catalog's first entry is the default.

**Harness (方式B)** — both sources ride the **same** `bedrockModelConfig` branch
of the `HarnessModelConfiguration` union and differ only in `apiFormat`:
`responses` for Mantle, `converse_stream` for Bedrock
(`app/deployer/harness.py`). The harness resolves `responses` **and**
`chat_completions` against Bedrock Mantle, not the bedrock-runtime
`/openai/v1` Responses API. A live probe (2026-09-26, us-west-2) showed this:
`global.openai.gpt-6-astra` returned Mantle's `404 The model … does not exist`
under both formats and answered only under `converse_stream`. The bare
`openai.gpt-6-astra` answered under `responses`. So GPT-6 on native Bedrock
means a `global.`/`us.` profile id on `converse_stream`. The keyed union branches (`openAiModelConfig` /
`geminiModelConfig` / `liteLlmModelConfig`) are deliberately unused — each
requires an AgentCore Identity API-key credential provider ARN that Launchpad
never provisions.

**Zip / Strands Studio (方式C)** — the model reaches Strands as an argument to
`Agent(model=...)`, so the source changes the *generated code*. A bare id string
resolves to a Converse call, so `mantle` renders an explicit model object
instead (`app/templates/strands_agent/main.py.tmpl::build_model`):

```python
OpenAIResponsesModel(bedrock_mantle_config={"region": MANTLE_REGION}, model_id=MODEL_ID)
```

`bedrock_mantle_config` makes the Strands SDK mint a short-lived bearer token
from the ambient AWS credential chain — the Runtime execution role, which carries
the `bedrock-mantle` grants above — on **every request**, and derive the endpoint
itself. There is **no `BEDROCK_API_KEY`** on this path. Two consequences worth
knowing:

- The zip's `requirements.txt` gains `strands-agents[openai]` for a Mantle spec
  (`_method_requirements` in `app/deployer/zip_runtime.py`); that extra is what
  carries `openai` + `aws-bedrock-token-generator`. The
  `OpenAIResponsesModel` import is function-local so a Bedrock-source agent,
  which never installs the extra, still imports cleanly.
- Mantle models are hosted in **`us-east-1`**, not the Region the runtime runs
  in. `LAUNCHPAD_MANTLE_REGION` overrides it; the default is `us-east-1`, never
  `AWS_REGION`.

The `/create/studio` canvas emits the same two forms per node: no node `apiKey`
⇒ `bedrock_mantle_config`; an explicit key ⇒ today's
`client_args={"api_key": …, "base_url": …}` override, so flows published with a
key keep generating byte-identical code. The SDK rejects combining the two, and
one shared emitter (`mantleModelArgs` in `frontend/src/studio/lib/models.ts`)
serves all three canvas code generators.

A newly dropped canvas agent node starts on native Bedrock with GPT-6 Sol
(`DEFAULT_NEW_AGENT_MODEL`, the first `BEDROCK_MODELS` entry), matching the
Create Agent wizard; Mantle stays one provider switch away. On the Bedrock
provider an OpenAI GPT id (`isBedrockOpenAiGpt`) is emitted by
`gptBedrockModelConfig`. Its effort rides `additional_request_fields` as
`{"reasoning": {"effort": low|medium|high}}` (Claude-only tiers clamp to high),
the same Converse shape the harness sends. Claude's adaptive-thinking block and
cache kwargs are never emitted for GPT. Claude and Mantle output is unchanged.

**Studio execution roles follow the flow.** A canvas publish sends no
`model_id`, so `spec.model_id` is always `AgentSpec`'s default. For
`method == "studio"`, `allowed_model_resources` in `app/services/agent_iam.py`
therefore authorizes every native-Bedrock model on the flow's agent,
orchestrator and swarm nodes. A node without an id gets the codegen fallback
(`DEFAULT_MODEL_ID`). Any Mantle-provider node adds the Mantle statements
(`uses_mantle`). Before this, a flow on any model other than the default
deployed cleanly and was refused at invoke.

A2A zip agents render from a different template with no Mantle branch, so the
wizard pins them to `bedrock` and hides the selector. The Other Agent SDK
(container) entrance is likewise pinned to `bedrock` and offered only Claude ids,
because its one SDK today — the Claude Agent SDK — cannot drive anything else;
the wizard shows it the SDK choice in place of the Model source control.

### Existing Runtime and Harness discovery

`/agents/import` is an onboarding path alongside the three creation
methods, not a deploy method. `GET /api/agents/discovery` follows every Runtime
list page in the configured Region and performs one detail read per resource.
The backend returns only an allow-listed projection: Runtime identity, name,
description, protocol, artifact type, authorizer type, AWS status/version, and
last-update time. Environment values, artifact locations, execution roles, and
authorizer configuration never leave the backend.

An explicit `POST /api/agents/discovery/import` re-reads each selected Runtime
and creates or refreshes an `Agent` row with
`method=discovered_runtime` and `owner=aws-discovery`. It creates no Deployment
or Job, runs no pipeline stage, and performs no Registry registration. ARN then
Runtime ID provide idempotent identity; a matching Launchpad-created row is
reported as already managed and never rewritten. Removing an imported row is a
local detach only and never calls an AgentCore delete or update operation.

HTTP and A2A resources can be imported; MCP Runtime resources remain visible in
the scan but are not agents and cannot be imported. Import and invoke
capabilities are intentionally separate: imported HTTP/A2A resources are
invokable only while AWS reports `READY` and no custom JWT authorizer is
configured. Custom-JWT resources can be retained as inventory but are excluded
from Chat and `/v1`.

The managed Harness service materializes each harness as a backing Runtime it
owns (named `harness_<harnessName>`, running the service's own
`public.ecr.aws/…/harness-<region>` image) that rejects `InvokeAgentRuntime`.
The scan joins `ListHarnesses` to flag these rows as artifact type `harness`:
they are never importable (reason `harness-managed`), never invokable, and when
the owning harness is a Launchpad agent the row links to it as already managed.
If `ListHarnesses` fails, the image heuristic still flags them — only the owner
linkage is lost.

The **owning Harness** is what an operator imports instead. The same response
carries a `harnesses` array (identity, status, version, last update, owner
linkage) plus a fail-soft `harness_scan_error` — a `ListHarnesses` failure leaves
the Runtime half of the scan intact rather than failing the request. `POST
/api/agents/discovery/import` takes `harness_ids` alongside `runtime_ids` and
creates the same externally-owned row shape, discriminated by
`spec.discovery.resource_type = "harness"` (absent ⇒ `runtime`, so rows imported
before this existed keep their behavior). The row stores the **harness** ARN and
id, which is what makes the rest fall out: Chat and `/v1` dispatch to
`InvokeHarness` exactly as a Launchpad `method=harness` agent does, the harness's
backing runtime resolves its owner through the existing ARN join, re-publish is
refused, and removal is a ledger detach that never calls `DeleteHarness` or
touches IAM. A harness already deployed by Launchpad is reported as already
managed and never duplicated. Status gates the first import only (`CREATE_FAILED`
/`DELETING` cannot be imported); re-importing an existing row always refreshes it,
which is how the ledger learns an external harness broke. Import reads
`GetHarness`, so a harness fronted by a custom JWT authorizer is retained as
inventory and excluded from Chat — the same split the Runtime path makes.
Evaluation, experiments, and harness→zip conversion stay keyed on
`method=harness` and therefore do not offer imported harnesses.

### Versions and endpoints (read-only)

Every `UpdateAgentRuntime` / `UpdateHarness` publishes an immutable new version;
the `DEFAULT` endpoint auto-follows the latest while named endpoints (the target
canary's `stable`/`treatment`) pin one. The ledger only remembers the version a
Launchpad deploy minted (`Agent.version`), so the agent detail on `/agents/:id`
(details mode) carries a **VERSIONS & ENDPOINTS** panel backed by
`GET /api/agents/{agent_id}/versions`. The route resolves the row to one resource
family — `zip_runtime`/`studio`/`container` and imported rows whose
`spec.discovery.resource_type` is absent or `runtime` → `ListAgentRuntimeVersions`
+ `ListAgentRuntimeEndpoints`; `harness` and imported rows with
`resource_type == "harness"` → `ListHarnessVersions` + `ListHarnessEndpoints` —
follows every `nextToken` page, and returns the same allow-listed projection
style as discovery (version, status, description, timestamps, endpoint
live/target version, failure reason; never environment values, artifact
locations, execution roles or authorizer configuration). A row with no AWS
resource (a deploy still running, a failed first deploy, a deleted agent, or a
shape that resolves to neither family) answers 409 `agent.no_resource` with a
human reason the panel shows in place of the tables.

The panel marks `DEFAULT`, highlights the ledger version against AWS's latest —
a mismatch after an out-of-band update or a canary candidate mint is shown as a
warning, not treated as an error — and flags `stable`/`treatment` endpoint names
so canary leftovers are noticed. It is strictly read-only: it never re-points
`DEFAULT` and never creates, updates or deletes an endpoint; the canary owns those
operations.

### Code view (read-only)

The agent detail's **Code** button opens `/v2/agents?view=source&id=`, backed by
`GET /api/agents/{agent_id}/source` (`services/agent_source.py`). Generated code is never
stored in the ledger and the view never re-renders it; it reads back what AWS holds:

- **`zip_runtime` / `studio`** — `GetAgentRuntime`'s `codeConfiguration` names the zip the
  Runtime runs. The package is mostly pip-installed dependencies, so the service reads it
  over ranged S3 GETs (the central directory plus the members it shows) and lists only the
  platform's files: the entrypoint, `requirements.txt` / `requirements.lock`, a
  conversion's `code_bundle` files and the bundled `skills/`.
- **`container`** (方式A) — the build context CodeBuild last built from,
  `builds/{name}/source.zip`.
- **`byoc`** — `code_zip` / `container_source` show the member's staged upload
  (`byoc/{workspace}/{upload_id}/source.zip`), i.e. their files without the dependencies
  the package stage installed; `container_image` has no source, so the page shows the
  image URI and digest.
- **`harness`** — a managed loop has no code. The page shows `GetHarness` projected
  through an allow-list with the same rule as the versions view (no execution role,
  environment, artifact location or authorizer), with tool header values, OAuth
  `customParameters` values and URL query strings masked.

Contents are capped per file and per response; a file left out for the budget is
fetched alone with `?path=`. When the latest deployment did not succeed the page warns
that the live version may still run the earlier code. Imported runtimes and rows with
no AWS resource answer 409 `agent.source_unavailable` with a `detail.reason` the page
translates.

### Spec snapshots and rollback

`Agent.spec` is overwritten on every publish, so on its own the ledger cannot say what an
agent looked like two edits ago. `services/snapshots.py` keeps one row per publish in
`spec_snapshots`, a workspace-scoped table with a per-agent `seq`:

- **One write site.** `deployer.pipeline.create_deployment` records the snapshot in the
  same transaction as the Deployment and Job rows, including callers that commit
  themselves. So no publish path can forget it: create, redeploy, recommendation accept,
  the inbound-auth switch, convert, experiment promote, architect approve and preset
  releases. A resumed job never passes through there again. The two canary paths that
  change the spec without a deployment, the Runtime promote and the Harness rollback,
  record their own row, stamped with the version they leave live.
- **The spec as deployed.** On success, `_finish` stamps the AWS version and replaces the
  stored spec with the agent's spec at that moment. A stage may rewrite it mid-job (the
  architect's harness stage pins `spec.skills` to immutable copies), and a rollback must
  re-deploy what ran. A failed publish keeps `aws_version` null and cannot be a rollback
  target.
- **Baselines.** At startup, every active, non-discovered agent with an AWS resource and
  no snapshot gets #1, `note: "baseline"`, holding its current spec. That makes today's
  configuration a rollback target right after the first edit.
- **The diff** compares nested fields by dotted path, lists by membership, and code,
  bundles, Studio flows and the system prompt whole. It groups each change (prompt, model,
  tools, skills, knowledge bases, memory, guardrail). Bodies over 2 KB come back as their
  length.
- **Rollback re-publishes a stored spec through the ordinary redeploy path**
  (`republish_agent`), so it needs the same `agents.deploy` permission and passes the same
  guards. It mints a **new** AWS version and a new snapshot; the console never reverts an
  AWS version in place. It also refuses, before any deploy call, a snapshot that never
  deployed, a spec that no longer validates, and a knowledge base it would re-add that is
  gone. The pin check skips admins and shared workspaces, so it would not catch that. On
  shared AWS (prod and prod2) a rollback changes the agent for every console.
- **A rollback rebuilds from references, not from stored artifacts.** Code methods rebuild
  from the spec, and a BYOC zip is re-read from its staged upload. A BYOC `container_image`
  given by **tag** re-deploys whatever the tag points at now: the old `byoc.provenance`
  digest in the snapshot is a record, and the generate stage re-stamps it. A missing upload
  or image fails the job at that stage, as a redeploy would.

### Evaluation scorecards

The evaluation views can read one agent's scores across its runs: which version each run
evaluated, what it was measured on, and how two runs line up case by case.
`app/evaluation/scorecard.py` holds the rules; the routes are `GET /api/eval/scorecard`,
`GET /api/eval/runs/compare-pair` and `POST /api/eval/runs/{id}/rerun` (see the API reference).

- **Stamps on the run row.** `submit_run`, the one place a run row is created, records
  `agent_snapshot_seq` (the newest spec snapshot that reached AWS — a snapshot still
  deploying is not what the run invokes) and, for a replay, `dataset_fingerprint` (16 hex of
  sha256 over the normalized scenarios, key order irrelevant) with `dataset_size`. The
  replay loop writes `scenario_map` (`{session_id: {scenario_id, input, actor_id}}`) as each
  session lands; a skipped persona has no entry. All four columns are nullable and older rows
  keep them null.
- **A fresh memory actor per scenario.** Each replayed scenario (and each persona) invokes
  under its own actor, `memory.eval_actor` = `<agent>__eval-<run>-<ordinal>`, recorded as the
  entry's `actor_id`; the transcript read uses it (a legacy entry without one falls back to the
  bare `default` actor). Before this, every replay passed no actor, so all runs of every agent
  shared `default` — on the shared memory a scenario could be answered from the long-term
  records another run had left there (live on prod3, 2026-10-23: a case the knowledge base could
  not answer scored as correct). The agent prefix keeps workspace isolation filtering these
  actors with their agent.
- **Versions on read.** Every run payload carries `snapshot {seq, source}`: `stamped`, or
  for an older row `inferred` from the snapshot timeline (the newest snapshot whose deploy
  ended by the run's creation, else the oldest), or `observed` for a passive scope, whose
  sessions predate the run. Nothing inferred is written back.
  - A run older than every snapshot (the startup baseline came later) gets the oldest one
    only when no deployment of the agent succeeded between the run and that snapshot.
  - Otherwise its version is unknown and `snapshot` is null. The console labels it
    早于版本记录 and compares it as older than every numbered version, so an accepted
    recommendation from before the snapshots reads as a version change, not a re-run.
- **Benchmarks.** A local dataset (revision = the fingerprint), a cloud dataset (revision =
  the pinned version or the draft's fingerprint) or the agent's past sessions (console label
  历史会话 / Past sessions — one-off runs over existing traffic, not online evaluation), which
  groups every window, chosen-session and log-stream run. Trends only connect runs of one
  benchmark; the composite and regression math stays in the console, which owns evaluator
  polarity.
- **Compare.** Two replays of one benchmark pair sessions by scenario id. A legacy run
  without a map is paired by position only when it skipped no scenario and has one session
  per scenario of its dataset's items; anything else compares overall scores
  and reads no results. The configuration change between the two versions comes from the
  snapshots diff route. A per-session result record is keyed by evaluator id — the
  evaluator ARN's id tail when the record carries one, else the evaluation name
  (`agentcore_eval.normalize_result_record` → `_record_evaluator_id`) — the same key as
  the run's `scores`, so a custom judge (whose evaluation name is not its id) fills its
  per-case columns.
- **Re-run** rebuilds a `RunCreate` from the source run (same target, evaluators, dataset
  and pinned version, or window length) and goes through the same `_start_run` as
  `POST /api/eval/runs`, so validation, visibility and budget checks cannot drift apart.
  Chosen-session and log-stream runs are refused: re-scoring the same sessions would only
  measure judge noise.
- **Isolation.** The scorecard filters runs by their creator and drops runs of hidden agents
  in SQL, before its 1000-run cap. Compare withholds a hidden agent's publish history and
  never reads a hidden dataset's items. A hidden dataset is labelled with the name stored
  on the run.
- **Console.** The V2 task page renders the compare as `?view=compare&a=&b=`
  (`v2/pages/tasks/RunCompare.tsx`, with the pure rules in `v2/compare.ts`). It shows one KPI
  per shared evaluator plus a composite over the evaluators both runs scored, the
  comparability notice, and the configuration change (the Agent page's `DiffBody` between the
  two snapshots). When the runs are aligned it adds a per-case table, classed by the
  normalized change on the chosen basis (0.1 or more is improved or regressed). A completed,
  scored run's detail offers 与上一次对比: the newest earlier completed run of the same agent
  and benchmark, read from the scorecard.
- **按 Agent tab.** `/v2/eval/tasks` now opens on 按 Agent (`AgentScoreList`); the run list
  moved to `?tab=runs` (一次性评估; the earlier `?tab=all` still opens it), next to the
  持续评估 tab of online configs (below). Each agent row shows its main benchmark (the one with the most
  completed runs), the latest composite, and the change against the previous *version's*
  latest run on that benchmark, or the reason there is none.
  - The composite is over the evaluators every completed run of that benchmark includes.
  - 需要关注 lists regressions: the composite down 0.05 or more, or one evaluator down 0.1
    or more. It shows a dataset change as 待确认, and also lists failed runs.
  - `?view=agent&key=` (`AgentScorecard`) draws the benchmark's trend in run order, with
    snapshot version bands and dataset-revision markers. It adds a version × evaluator
    table, the agent's runs with A/B picks for compare, and 再评一次 through
    `POST /api/eval/runs/{id}/rerun`.
  - The rules live in `v2/scorecard.ts` and `pages/tasks/scorecardAttention.ts`.
- **The architect's step 2.** NEXT STEPS step 2 (`v2/pages/assistant/RunHistory.tsx`) reads
  the agent's scorecard and treats its runs on the plan's Dataset as one benchmark, with the
  same rules as above. It shows the latest composite against the previous version and a
  sparkline, then the newest five runs, each with its version, composite and change.
  - A same-version re-run is compared with that version's previous run and marked as noise.
  - A version change that regresses by the rule above is tagged 退步.
  - A collapsed evaluator × version breakdown shows the newest four versions.
  - Its links to the scorecard, the run detail and the compare page all return to the
    conversation on Back (`returnState` in `pages/tasks/back.ts`).

## The invoke chain

The Chat playground (`/api/chat/{id}`) and the public API
(`/v1/agents/{id}/invoke` + `/invoke-stream`) share **one** entry point,
`app.services.invoke.invoke_agent_text` (and `app.services.chat.chat_stream` for
SSE), so both entrances behave identically:

```
console /api  ─┐
               ├─▶ invoke_agent_text / chat_stream
public  /v1  ──┘        │
                        ├─ method dispatch:
                        │    harness            → harness data client
                        │    zip/studio/container → runtime data client
                        │    discovered HTTP/A2A → runtime data client
                        │    discovered harness  → harness data client
                        ▼
             AgentCore Runtime / Harness
                        │  (session isolation, streaming)
                        ├─ Memory        (session context read/write)
                        ├─ Gateway tools (MCP over Cognito JWT)
                        ├─ Policy        (Cedar ENFORCE at the gateway)
                        └─ Observability (spans → CloudWatch Transaction Search)
```

### Disconnect-safe console chat turns

A console chat turn (`POST /api/chat/{id}`) runs on a background worker thread, not in
the HTTP response (`app/services/chat_turns.py`, chat-streaming §5.2). The worker runs
the invoke chain above inside the turn's project context, persists the thread items
and publishes every event into the turn's in-memory buffer, numbered by `seq`. The
response is only the buffer's first subscriber. A closed tab, a reload or a dropped
proxy connection therefore no longer ends the turn: it keeps running and saving.

- `meta.turn_id` names the turn, and every non-heartbeat frame carries its `seq` as the SSE `id`. `GET /api/chat/{id}/turns/{turn_id}/events?after=<seq>` subscribes again: first the buffered events after `seq` (a `gap` event when some already left the buffer), then the live ones. The console reconnects with it after a drop, and `GET …/history` returns `running_turn` so a reopened session follows the turn still running.
- The stop button closes the stream and calls `POST …/turns/{turn_id}/stop`, which closes the upstream, saves the partial answer with an `interrupted` row and waits briefly for the worker so the next turn is accepted at once.
- One running turn per session (`409 chat.turn_in_progress`), so a resumed page cannot interleave two turns. A turn runs at most the agent's `timeout_seconds` + 60 s, then ends with `chat.turn_timeout`.
- The buffer is bounded (2000 events or 600 s), and a finished turn stays resumable for 60 s; after that, the ledger is all there is.
- **Single process (v1).** The turn registry is the serving process's memory. A deployment with several workers must route a resume to the process running the turn, and a restart loses the running turns; their partial answers stay in the ledger up to the last item flushed. Multi-process resume from the ledger is not built.

Not verified live: whether AgentCore keeps executing a turn after `InvokeHarness` /
`InvokeAgentRuntime`'s stream is closed (the `interrupted` row assumes it ends there).
`/v1` and the share page do not use the worker: their streams still end with the
connection. Routes: [api.md](api.md#console-chat-api).

### Playground file attachments

Chat and all sync/SSE invoke entrances accept the same optional
`attachments: [{name, media_type, data}]` contract, with base64 wire data and
server-side content validation. `services/attachments.py` prepares files once:
UTF-8 text and explicitly labeled PDF text extraction enter the user prompt;
native images/PDFs remain separate until the runtime adapter. The composer
offers selection, drop and image paste with previews, removal and limits read
from the agent's server-derived `attachment_capability`.

Managed Harness accepts text blocks, so its PDF support extracts text only.
Images and nonblank pages without extractable text require a native-capable
agent. PDF parsing is isolated in a resource-bounded process. Generated
Strands/Claude/Studio/converted HTTP artifacts hydrate native file bytes and
acknowledge them before model output; Strands A2A uses standard file parts.
Mantle documents use `file_data` plus `filename`, because its `file_url` accepts
only S3 references.

Native support is bound to a packaged artifact and its successful AWS runtime
version (`Agent.attachment_version`, server-owned). Old HTTP deployments retain
text/text-PDF input and need republishing for native files. Existing sessions
remain pinned to their old version; `ChatSession.runtime_version` catches stale
console sessions before invocation, and the runtime acknowledgement also guards
public sessions that have no console history. Native attachments are refused
during active canary routing until both artifacts can be proven compatible.

`ChatMessage.attachments` stores only filenames, media types, sizes and delivery
mode; raw bytes/base64 never enter the chat ledger. Reloaded history shows that
metadata, while original bytes remain transient in the invocation path. Existing
framework-owned AgentCore Memory behavior is unchanged. See
[attachment research and design](playground-attachments-research.md) and the
[Chat API contract](api.md#console-chat-api).

### Gateway (MCP) tools reach both a Harness and a zip runtime

A gateway `ToolRef` used to be a harness-only capability, which split the lab
along a line no participant would expect: chapter 11 governed tool calls only a
Harness could make, while chapters 09/10 experimented on runtimes that could make
none. Both methods now reach `launchpad-gw`; only *who performs the token
exchange* differs.

| | Managed Harness | Generated zip runtime |
|---|---|---|
| Tool wiring | declarative `agentcore_gateway` tool with an `outboundAuth` OAuth block | generated MCP client in the emitted `main.py` |
| Token exchange | the Harness service does it | the agent does it: workload identity token → `GetResourceOauth2Token(oauth2Flow="M2M")` |
| Execution role | `agent_iam._uses_gateway()` | **the same** — it keys off `tool.type`, never `spec.method` |
| Cedar | at the Gateway | at the Gateway, identically |

Three pieces make the runtime side work, and all three are required:

1. **A workload identity token must exist.** The Runtime injects one
   (`WorkloadAccessToken`) only when the caller supplies `runtimeUserId` on
   `InvokeAgentRuntime`. The invoke chain sends it **only** for agents whose spec
   carries a gateway ToolRef, so every other agent's call is unchanged. Verified
   live: without it the client logs `NOT injected` and runs tool-less.
2. **Env from `settings.resources`** — `LAUNCHPAD_GATEWAY_URL` / `_PROVIDER` /
   `_SCOPE`, injected by `runtime_environment()` only for a gateway spec, and only
   when all of them resolve (a half-set env would look configured and fail auth
   confusingly).
3. **Fail-soft by construction.** Every risky import in the generated client is
   function-local and every failure path logs and returns a neutral value, so no
   module-scope statement can raise. An import-time crash would be worse than
   missing tools: the deploy pipeline's health signal still reports the agent
   `active`, and every invoke then fails.

Harness→runtime conversion keeps its gateway tools for the same three reasons.
`POST /api/agents/{agent_id}/convert` (`convert_agent` in `routers/agents.py`)
accepts only an *active* `harness` agent and answers `202` with
`{agent, job_id, deployment_id}`: it never modifies the source harness, it creates a
**new** agent named `<source>-rt`. `services/harness_convert.py` does the work.
`resolve_agentcore_cli` locates the repository-managed `@aws/agentcore` CLI that
bootstrap installs under `data/agentcore-cli/`, and `export_harness` runs its
`export harness --build CodeZip` inside one reusable scratch project under a unique
target agent name, then reads the generated tree into memory and deletes it — the
spec's `code_bundle` is the artifact of record. `build_conversion_spec` grafts the
Launchpad config-bundle contract onto the exported `main.py`, which is mandatory
rather than cosmetic: the export bakes `DEFAULT_SYSTEM_PROMPT` as a constant, so an
ungrafted conversion would no-op A/B experiments exactly as the harness does, and a
missing graft anchor therefore fails the conversion instead of shipping a silently
non-A/B-able agent. The factory anchor accepts the CLI's zero-argument form when
Memory and Skills are disabled, the Skills-only form when Memory is disabled,
the session/user form, and the session/user/Skills
form. Tool-free exports can carry an unused gateway-client scaffold; only that
inert case skips the gateway grafts, while configured or constructed clients must
still pass their anchors. Native Harness tools (`shell`, `file_operations`) that the source
Harness selects are re-grafted when the export drops them — the CLI only enables a
builtin whose `allowedTools` entry matches `builtin/<name>`, while Launchpad and the
Harness service use the bare name — from the installed CLI's own template, so the twin
can still read its Skills' `references/` files. Gateway clients are rebuilt the same way: the CLI (0.21.x) only emits `mcp_client/client.py` Gateway clients when the Harness `allowedTools` is `*`, and Launchpad deploys target-scoped selectors (`@launchpad_gw/<target>___<tool>`), so for a Harness with Gateway `ToolRef`s the convert route reads the live Harness and `graft_missing_gateway_clients` regenerates the clients in the CLI's shape, carrying the narrowing over as Strands `tool_filters` and leaving out the KB gateway (replaced by direct retrieval). An OAuth Gateway client mints its M2M token at session-open time; an `AWS_IAM` Gateway (`outboundAuth.awsIam`, e.g. a Registry-attached remote MCP fronted by its own Gateway) gets a SigV4-signing `httpx.Auth` transport instead, with its URL built from the Gateway ARN (only the shared Gateway's URL is wired into the env) and access granted by the existing `bedrock-agentcore:InvokeGateway` statement scoped to the spec's gateway ids. Those clients are `_LaunchpadMCPClient`, which starts its session and hands every call to its background thread with the `aws.agentcore.configbundle_*` OTel baggage removed: behind the experiment Gateway the twin receives the routed bundle as baggage, OTel instrumentation would carry it onto the twin's own `launchpad-gw` calls, and `launchpad-gw` then fails to resolve it (percent-encoded ARN, truncated version) and answers `tools/list` with 400. The twin already applies its bundle itself. The emitted `zip_runtime` spec carries the harness's gateway
`ToolRef`s, skill prefixes, memory and KB configuration forward, records what was
wired in `conversion_notes`, and stamps `source_harness` so `experiment_capability`
reports the new agent eligible. The v1 "gateway MCP not wired" caveat is gone, not
reworded.

A routed configuration bundle makes **both** the runtime and the Gateway resolve
that bundle, each with its own role, so `GetConfigurationBundleVersion` is needed on
the per-agent execution role *and* on `launchpad-gateway-role`. Missing it on the
runtime side 500s the invoke from inside; missing it on the Gateway side answers the
MCP call with `HTTP 400 "Config bundle fetch failed"` and the agent silently loses
every Gateway tool. Both grants are in place, which is what lets a config-bundle A/B
vary a *Gateway* tool's description.

Still harness-only: remote (`type: "mcp"`) servers on a zip runtime, and Gateway
tools on the container method.

The public `/v1` surface adds `X-Api-Key` auth (keys stored sha256-hashed);
everything downstream of the dispatch is identical to the console path.
A key records its creator (`api_keys.created_by` = `users.id`) and `last_used_at`
(stamped by `require_api_key` on success, at most once a minute), and lives only
as long as that creator may hold it. One predicate,
`services.api_keys.creator_offboarded` (deleted / non-active / expired account,
or a member without a grant to the key's workspace — admins bypass grants as
everywhere), is used three times: `require_api_key` answers the ordinary
`401 auth.invalid_api_key` and flips the key to disabled; `POST
/api/apikeys/{id}/enable` refuses with `409 apikey.creator_offboarded`; and
`GET /api/apikeys` marks the key. Admin disable/delete (`users_service.apply_patch`
/ `delete_user`) and grant removal (per-user `PATCH /api/users/{id}` and bulk
`PUT /api/workspaces/{id}/grants`) disable the creator's keys eagerly in the same
transaction; account expiry has no write event, so the check at use is what
covers it. Keys are never deleted. A key whose creator has no `users` row (the
config admin, the auth-disabled operator) or a legacy NULL creator is never
offboarded.
Every agent response carries one backend-owned `invoke_capability`; console
invoke, Chat, and `/v1` enforce the same projection. Imported runtimes use the
buffered compatibility path because Launchpad cannot assume an arbitrary
external runtime emits the generated Claude SDK event contract.

Harness, Claude Agent SDK container, and generated Strands zip-runtime agents
stream native model deltas. Claude containers enable SDK partial messages, and
the Strands zip template drives `Agent.stream_async` from an async-generator
entrypoint; both yield the same `delta`, `tool`, and `complete` events (plus
`heartbeat` frames during long tool calls) through the AgentCore Runtime SSE
response. The platform parses the Runtime `StreamingBody` incrementally and
forwards those events without waiting for EOF, so a zip agent's tokens and tool
calls appear in Chat exactly as a Managed Harness agent's do. Synchronous invoke
consumes the same event parser and joins deltas. Studio runtimes, A2A runtimes,
and active canary Gateway routes retain the buffered compatibility path; a zip
runtime deployed from the older template still answers one JSON result, which
the same parser renders as a single delta. Existing runtimes must be republished
to pick up a changed generated template. AgentCore pins an existing runtime
session to the version that first served it, so a post-republish validation
must start a new Chat session; an old session continues on its original image.
Chat can also **end** the live runtime session explicitly — END SESSION (next to
NEW SESSION, and per row in the history rail) calls the data-plane
`StopRuntimeSession` through `POST /api/chat/{id}/sessions/{session_id}/stop`,
then clears the current id so the next prompt starts fresh. NEW SESSION alone only
forgets the id locally and leaves the runtime session to idle out. Only
runtime-backed agents qualify; a managed Harness has no session-stop operation
(409 `chat.session_stop_unsupported`). The `ChatSession` row is kept with an
`ended_at` stamp so the rail can show the session as ended while its transcript
stays replayable.

The versions involved are visible in the agent detail's VERSIONS & ENDPOINTS
panel (`GET /api/agents/{id}/versions`), which lists every AWS version alongside
the one the ledger recorded and the version each endpoint currently serves.

## Existing Gateway governance

`/governance` reads MCP Gateways, targets, Policy Engines, policies, and
Registry records directly from AgentCore. Opening a Gateway is read-only.
Selecting **Manage** adds only these durable tags:

```text
agentcore-launchpad:managed = true
agentcore-launchpad:managed-by = agentcore-launchpad
```

Registry import and Policy mutations require the tag plus a fresh
`updatedAt`. Unmanaging removes only those tags; it never detaches or deletes
Gateway, Engine, Policy, or Registry resources.

The Registry and Harness boundaries are intentionally separate. A Gateway MCP
record contains the whole Gateway tool catalog. Selecting that record attaches
the whole Gateway to a Harness; Cedar policies authorize individual actions.
AWS_IAM and unauthenticated Gateways resolve to `awsIam` and `none`. The
Launchpad-owned CUSTOM_JWT Gateway reuses its configured OAuth provider.
External CUSTOM_JWT Gateways without a managed provider mapping remain
catalog-only.

Policy decision evidence comes from the `AWS/Bedrock-AgentCore` CloudWatch
metrics (`AllowDecisions`, `DenyDecisions`, and the determining/mismatch family),
which AgentCore publishes by default — no per-gateway enablement is required.
`app/services/governance_evidence.py` owns that read and feeds both the scoped
decision endpoint and the real `evidence_count` behind the cutover gate; the gate
counts LOG_ONLY-mode decisions only, matching the documented promotion rule.
`available=false` is now reserved for an unreadable channel (the AWS error code is
reported); a readable channel with a quiet window is `available=true` with
`evidence_count=0`, and zero-evidence promotion still requires the typed Gateway
name plus a recorded reason.

Two properties of that metric channel shape the contract:

- **Aggregates only.** Metric dimensions cannot carry a principal, decision
  reason, or trace id, so `decisions[]` stays empty and is never synthesized.
  Per-decision rows require Policy spans, which do need trace delivery enabled on
  the attached Gateway.
- **Counting basis differs per operation.** `AuthorizeAction` publishes a
  gateway-level stream (one decision per call); `PartiallyAuthorizeActions` was
  observed publishing only `ToolName` projections (one decision per call/tool
  pair). Each operation therefore resolves its own dimension projection and
  reports the `basis` it counted in. AWS publishes several overlapping projections
  of the same event, so selections match an exact dimension-name set — summing
  across projections would inflate counts several-fold.

Per-decision rows come from that span channel, parsed by
`app/services/governance_spans.py`. The row source is the
`AgentCore.Gateway.InvokeTool` SERVER span, which carries `tool.name` **and**
`aws.agentcore.policy.authorization_decision` together; the child
`AgentCore.Policy.*` span adds the determining/mismatched policy ids and
`aws.agentcore.policy.log_only_matched_policies` — an undocumented attribute that
reveals what a LOG_ONLY *candidate* would have matched from an ENFORCE-mode span,
which the metric channel cannot express. `session.id` needs a second pass joined on
`traceId`. Three properties are load-bearing:

- **`principal` is structurally unavailable.** No span in the trace carries a
  principal, because the Harness authenticates to the Gateway with an OAuth M2M
  client credential — the request has no human subject. The field renders as
  explained-absent, never inferred. The local demo ledger keeps its own principal
  and the two are not conflated.
- **`PartiallyAuthorizeActions` denials are list-time tool-availability decisions,**
  not blocked calls: under ENFORCE the tool is filtered out of `tools/list` so the
  model never sees it. Rows carry an `evaluation` kind (`invocation` /
  `tool_listing`) so the two are not presented as the same event. Under ENFORCE the
  listing denial is the *only* DENY that can occur.
- **Spans never redefine `evidence_count`.** Spans are sampled while metrics are
  exact counts, so the gate's number stays metric-derived and a span-channel outage
  degrades to metrics-only (`spans_unavailable_reason`) rather than failing the
  request.

The decisions response also reports the live delivery configuration independently
as `span_channel_status` (`ready`, `missing`, or `unknown`) plus
`span_channel_reason`. A successful Logs Insights query with zero rows is not proof
that Gateway tracing is configured: `ready` requires the expected TRACES source,
XRAY destination, and connecting delivery. This probe is read-only; the GET route
never repairs AWS resources.

The span channel is the opt-in half, and it is **per Gateway**: AgentCore emits
Policy decision spans only after trace delivery is enabled on the attached
Gateway. That is a CloudWatch vended-log delivery (source `logType=TRACES` →
`XRAY` destination → delivery), not a Gateway setting, so enabling it never calls
`UpdateGateway`. `make bootstrap` enables the shared Transaction Search
prerequisite but deliberately does not create this Policy-specific delivery.
`policy_bootstrap.ensure_gateway_traces()` remains an idempotent primitive for
explicit operational tooling; normal bootstrap never calls it. The console's
delivery-status probe is read-only, and a missing channel is an expected state
until the operator opts into detailed Policy spans.

### Gateway rate limits

The gateway detail's **RATE LIMITS** panel manages AgentCore Gateway rate limits
(GA August 2026) through four synchronous routes under
`/api/governance/gateways/{id}/rate-limits` (`GET` list, `POST` create, `PUT
/{rate_limit_id}` update, `DELETE /{rate_limit_id}`). The wrappers
(`list_gateway_rate_limits`, `create_gateway_rate_limit`,
`update_gateway_rate_limit`, `delete_gateway_rate_limit`) sit in
`app/services/agentcore/policy.py` next to the other Gateway control-plane calls
and take the control client explicitly; the list follows every `nextToken`.
Reading works on any Gateway; every mutation requires the Launchpad managed tag
(`409 governance.gateway_not_managed`, same rule as policy mutations).

A rate limit is a fixed, ordered set of **dimension keys** plus up to 1000
**entries**. Each entry names one value per key (`*` = any) and one rate per
metric. `validate_rate_limit_spec` checks the documented rules before any AWS
call and answers `422 governance.rate_limit_invalid` with a stable
`detail.reason`:

| Rule | `detail.reason` |
|---|---|
| 1–10 keys, each `targetName`, `toolName`, `qualifiedModelId`, `$.context.jwt.<claim>`, `$.context.iam.principal` or `$.context.iam.sourceIdentity`, no duplicates | `dimension_keys_count`, `dimension_key_unknown`, `dimension_key_duplicate` |
| 1–1000 entries; each entry's `dimensions` has exactly the parent keys, no empty value | `entries_count`, `entry_dimensions_mismatch`, `entry_dimension_empty` |
| `*` only in trailing positions (once a value is `*`, every later key is `*`) | `wildcard_not_trailing` |
| at least one of `requests` / `tokens` / `connections`, one rate config each | `entry_no_metric`, `rate_config_count` |
| `rate` 0–10 000 000; `requests` per `second`/`minute`, `tokens` per `minute` only, `connections` per `second` only | `rate_out_of_range`, `period_not_allowed` |
| description ≤ 512 chars; `dimensionKeys` never on update | `description_too_long`, `dimension_keys_immutable` |

AWS `ConflictException` (a second rate limit with the same key set, or a busy
Gateway) surfaces as `409 aws.conflict` through the shared `ClientError`
envelope. Unlike the policy mutations there is no 202/operation hop: the
`PolicyChange` row (`rate_limit.create` / `rate_limit.update` /
`rate_limit.delete`; `before` = the prior rate limit or `{}`, `requested` = the
validated payload, `after` = the AWS response) is written as `running` around
the call and closed `succeeded`/`failed` inline, so the Audit view lists it and
a crash mid-call leaves a visible row. The panel states the documented
semantics — effective rate = min(service-managed, configured), propagation
≤ 30 s, fail-open, rate 0 blocks matching traffic, evaluated **before** Policy
— mirrors the trailing-`*` and period rules client-side, and explains disabled
actions (not managed / Gateway not READY / rate limit not ACTIVE / form invalid)
through the shared `Btn` `disabledReason`. No IAM change: the console's role
already carries `bedrock-agentcore:*`.

### Target synchronization

Every target row in the gateway detail's **TARGETS** table shows `lastSynchronizedAt`
(`-` when AWS has never synced it) and, for **dynamic MCP-server targets** on a
**managed** Gateway, a **SYNC** action. `POST
/api/governance/gateways/{id}/targets/{target_id}/synchronize` calls
`SynchronizeGatewayTargets(gatewayIdentifier, targetIdList=[target_id])` — the
service re-runs MCP `initialize` + paginated `tools/list` against the target
endpoint (with the configured Identity credential when there is one) and moves the
target to `SYNCHRONIZING`, then `READY` or `SYNCHRONIZE_UNSUCCESSFUL`. The wrapper
`synchronize_gateway_target` lives in `app/services/agentcore/policy.py`; the route
answers `202` with the target projection `{id, name, status, status_reasons,
description, listing_mode, last_synchronized_at, synchronizable,
not_synchronizable_reason}` — the same shape `gateway_detail` now returns for every
target, so the console never re-derives AWS rules.

Two gates run **before any AWS call**: the Gateway must carry the managed tag
(`409 governance.gateway_not_managed`) and the target must be synchronizable, else
`409 governance.target_not_synchronizable` with a stable `detail.reason`:

| Rule (from the SynchronizeGatewayTargets reference) | `detail.reason` |
|---|---|
| `targetConfiguration.mcp.mcpServer` must be present — Lambda / OpenAPI / Smithy / connector schemas are static by construction | `not_mcp_server` |
| a static `mcpServer.mcpToolSchema` disables sync | `static_tool_schema` |
| `CREATE_PENDING_AUTH` / `UPDATE_PENDING_AUTH` / `SYNCHRONIZE_PENDING_AUTH` are refused until the operator completes authorization | `pending_auth` |
| already `SYNCHRONIZING` | `synchronizing` |
| any other transient state (`CREATING`, `UPDATING`, `DELETING`); sync needs `READY`, `SYNCHRONIZE_UNSUCCESSFUL`, `UPDATE_UNSUCCESSFUL` or `FAILED` | `not_ready` |

The call is journaled inline exactly like the rate-limit mutations: one
`PolicyChange` row (`target.synchronize`; `before` = the target projection before
the call, `requested` = `{target_id, target_name}`, `after` = the AWS response
target) written as `running` and closed `succeeded`/`failed`. AWS
`ConflictException` reaches the client as `409 aws.conflict` through the shared
`ClientError` envelope, never as a 500. There is no operation row: after a
successful SYNC the console re-fetches the detail every few seconds while a target
is `SYNCHRONIZING` (giving up after ~2 min) and shows `statusReasons` under a
`SYNCHRONIZE_UNSUCCESSFUL` / `FAILED` chip. Disabled SYNC buttons explain themselves
through the shared `Btn` `disabledReason` (not managed / target type / pending auth /
already synchronizing / operation busy). Out of scope: listing a dynamic target's
tools (the control plane does not return them), creating or updating targets, and
batch sync. No IAM change — the console's role already carries
`bedrock-agentcore:*`.

### Target kinds

A Gateway target is not necessarily an MCP tool provider: the pinned
`bedrock-agentcore-control` model's `TargetConfiguration` is a three-way union —
`mcp{openApiSchema, smithyModel, lambda, mcpServer, apiGateway, connector}`,
`http{agentcoreRuntime, passthrough, connector}` (HTTP passthrough / AgentCore
Runtime targets) and `inference{connector, provider}` (inference targets). The
target projection therefore carries `kind: {protocol, variant}` — derived by the
pure helper `target_kind` in `app/services/governance.py` from whichever union
member AWS set, e.g. `{"protocol": "mcp", "variant": "lambda"}`,
`{"protocol": "http", "variant": "passthrough"}`,
`{"protocol": "inference", "variant": "provider"}`. The projection is tolerant by
design: an empty `targetConfiguration` is `{"protocol": "unknown", "variant":
null}` and a union member the pinned model does not know yet maps to
`protocol: <key>` / `variant: null` — never an exception. The detail's **TARGETS**
table shows the kind in a **KIND** column (localized per `(protocol, variant)`
under `governance.targetKind.*`, with a mono `protocol/variant` fallback for
unknown pairs), and the SYNC blocker for a non-`mcp` target names that kind
("… target kind: HTTP passthrough") while the reason code stays
`not_mcp_server`; `mcp` variants other than `mcpServer` keep the generic copy.
`discover_actions` is unchanged — only MCP schemas carry tools — so `gateway_detail`
also returns `actions_uncovered_targets: [names]` for every `http` / `inference`
target, which the panel renders as a one-line hint ("N target(s) expose no tool
schema: …") so an empty ACTIONS cell is not misread as a discovery failure.

## Console routing

The console is a single `react-router-dom` route table in `frontend/src/App.tsx`,
nested under one `<Shell />` element that owns the sidebar, topbar (breadcrumb)
and footer. Modules are top-level routes; their sub-surfaces are `?view=` query
params, never nested routes. The table ends with a `path="*"` catch-all **inside**
the Shell group, so an unrouted URL (typo, stale bookmark to a retired sub-route)
renders `pages/NotFound.tsx` — kicker, heading, the requested pathname in mono
and a primary link back to the Overview — with the chrome intact instead of a bare
background grid. The breadcrumb is derived in `layout/Shell.tsx`: a pathname that
matches none of `ROUTE_PATHS` (`layout/nav.ts`, mirrors the route table) gets the
`nav.notFound` crumb; otherwise the longest-prefix nav entry labels it. Adding a
route means adding it to both the `<Route>` table and `ROUTE_PATHS`.

**Pages load on demand.** Every module but the index route is a `React.lazy`
boundary in that same table: the entry chunk carries only the shell — React,
the router, i18n, the shared component and `lib/api` layers — and a route's code
arrives on the navigation that needs it. That keeps the Studio canvas
(`@xyflow/react` plus the monaco loader), the markdown/highlight stack and the
other twelve pages out of a first visit; the entry chunk is ~600 kB minified
instead of 1,936 kB, next to one chunk per page and a shared `Markdown` chunk for
Chat and the observability session detail. `Overview` and `NotFound` stay eager:
a chunk for the index route would cost the very first paint a round trip and
nothing else would use it, and the catch-all is a few hundred bytes that an
unrouted URL needs immediately. `layout/RouteChunk.tsx` is the boundary the Shell
wraps around `<Outlet />`, *inside* `.view`, so the sidebar, topbar and footer
never move: while a chunk is in flight it renders one translated mono line
(`routeChunk.loading`), never a blank page. When the import rejects it renders the
shared `LoadError` block with a RELOAD action instead of RETRY (`routeChunk.failed`
/ `routeChunk.reload`, `LoadError`'s `retryLabel` prop). That failure is expected
rather than exotic: chunk filenames are content-hashed, so rebuilding the box
under an open tab (prod serves a built `dist/` through `vite preview`, see
[agent-runbook-prod.md](agent-runbook-prod.md)) makes the hash the loaded shell
asks for disappear, and reloading is the whole fix — as it is when a dev/preview
server went away. The boundary claims that diagnosis **only** for a rejected
dynamic import (the message matched against the browsers' phrasings); any other
error a page throws is re-thrown untouched, so a real render bug still surfaces
as it did before. It is keyed on `location.pathname`, so navigating away clears a
failure while a `?view=` change never remounts the page. Only the vendored DCV
live-view chunk (already lazy, `pages/governance/ToolsView.tsx`) is above Vite's
500 kB chunk warning, and `build.chunkSizeWarningLimit` in `vite.config.ts` is
raised just far enough to cover that one chunk (2900 kB), so the warning still
fires if the entry or a page chunk regresses.

## Console V2

A second console experience ships **alongside** the classic one: a light,
enterprise-SaaS style UI modelled on the evaluation workflow customers expect
(top product bar, grouped collapsible sidebar, filter-bar list pages with a
"共 N 项" count, step wizards, `|`-titled section cards, status tags). It lives
under its own route group `/v2/*` in `frontend/src/App.tsx`, **outside** the
classic `<Shell />`, with its own shell (`v2/V2Shell.tsx`) — both share the auth
gate, the workspace provider and `lib/api.ts`, so every V2 read and write goes
through the same backend routes and permission checks as the classic pages.

- **Switching.** The classic topbar has a "Try V2" chip; V2's top bar has
  "Classic console". The choice is a per-browser convenience in `localStorage`
  (`lib/ui-version.ts`, key `launchpad_ui_version`, exposed reactively through
  `useUiVersion()`). **V3 is the default** (see [Console V3](#console-v3-preview)):
  only a stored `v2` (an explicit "Back to V2") keeps a browser on V2 and a stored
  `v1` (an explicit "Classic console" click) on the classic console; otherwise the
  index route `/` redirects to `/v3`. Opening a `/v2` page from the classic console
  selects V2. Storage that is blocked or empty means V3 (the switch still applies
  to the open tab).
- **Pre-shell pages.** Sign-in / register (`auth/AuthGate.tsx`), the
  session/workspace spinners and the "no workspace granted" notice
  (`workspace/WorkspaceProvider.tsx`) wear the chosen console's chrome: V3
  (`v3/AuthFrame.tsx` + `v3/auth.css`, dark command-center ground) by default, V2
  (`v2/AuthFrame.tsx` + `v2/auth.css`) once V2 or classic is chosen. The sign-in and
  register forms are shared; their class names carry the console prefix. The "no
  workspace granted" notice has a native V3 page (`v3/NoWorkspace.tsx`: the
  waiting state, sign-out and the public announcements feed on V3 panels); V2 and
  classic keep the V2 one, which embeds the classic feed.
- **Classic modules inside V2.** The classic route group renders through
  `ConsoleShell` in `App.tsx`: the classic `<Shell />`, or — once V2 is chosen —
  `<V2Shell classic />`. So `/chat`, `/agents/…`, `/registry` keep their URLs in both
  consoles, cross-module links work unchanged, and switching keeps the current page
  (only a native `/v2` page, which has no classic twin, falls back to `/`). The
  classic pages are styled entirely through the tokens in `theme/tokens.css`
  (the surfaces and tints they used to hard-code are tokens too: `--field`,
  `--code-bg`, `--on-amber`, `--amber-rgb`, `--tint-rgb`, `--bg-rgb`, …);
  `v2/v2-classic.css` re-points those tokens to the V2 palette on `body.v2-body`
  (so portaled toasts and dialogs follow) and adds shape tweaks under
  `.v2-classic`, the content wrapper of a classic page.
- **Styling isolation.** Everything in `v2/v2.css` is scoped under `.v2` (the shell
  root) or `body.v2-body`, a class the shell adds to `<body>` only while mounted to
  neutralize the classic dark background and film grain. The classic theme is
  untouched; V2 ships its own component kit (`v2/ui.tsx`: button, tag, filter
  select, search, table + pager, card, wizard steps, modal, drawer, descriptions,
  KPI, toast) instead of reusing the classic components.
- **Agent management (native).** `/v2/agents` (`v2/pages/Agents.tsx`) lists the
  workspace's agents with the classic permission rules per row; `?view=detail&id=`
  shows basic information, the five-stage deploy pipeline with the job log (polled
  while deploying), AWS versions/endpoints, BYOC artifact, conversion provenance and
  knowledge bases; `?view=new` is the creation wizard (`v2/pages/agents/AgentWizard.tsx`).
  The wizard configures the **managed Harness** end to end and posts the same
  `AgentSpecInput` the classic wizard builds for it (catalogs: `registryAttachables()`,
  the managed-KB list, memory resources); Strands, other Agent SDK and BYOC hand off to
  the classic wizard at `/agents/new?method=…`, which opens its configure step with
  that method preselected. Editing, importing, system presets and the Studio canvas
  stay classic. In V2 the classic `/agents` and `/agents/:id` URLs redirect to the
  native pages (`AgentsRoute` in `App.tsx`), so cross-module links and the classic
  wizard's post-deploy hand-over land there.
- **Online evaluation and experiments (native).** Online evaluation configs are 评估任务's
  持续评估 tab (`/v2/eval/tasks?tab=online`, `v2/pages/online/`, shared rules in
  `v2/online.ts`; there is no separate 在线评估 menu). The tab manages every config —
  agent-owned, experiment-owned (read-only) and external ones, scores and insights modes.
  An experiment-owned row says whose it is from the API's `parent`: the tag reads A/B 实验
  or 金丝雀, the Agent column falls back to that record's agent, and the detail names the
  record and the traffic it scores (both arms of an A/B test, a canary's control or
  candidate), links straight to it (`parentLink`; its list tab once it is gone) and adds
  a leftover note when that record was cleaned or no longer exists. The tab has
  a list with mode/owner/execution filters that opens on the agent-owned configs (polled
  while CREATING/UPDATING/DELETING); a detail (`?view=detail&kind=online&id=`) with the
  server's per-evaluator aggregates and trend plus the judged records (scores), or the
  scheduled / on-demand insight reports with a report drawer (insights); and a
  create/edit form with filters (`?view=new&strategy=continuous`, a copy via
  `from=online:<id>`, `?view=edit&kind=online&id=`) whose save sends only the changed
  fields (`api.v2UpdateOnlineConfig`, PATCH). The old `/v2/eval/online` URLs redirect to
  the matching tab or sub-page (`onlineToTasksUrl` in `v2/pages/tasks/urls.ts`), and the
  ⌘K palette still finds the tab as 在线评估. `/v2/eval/experiments` (`v2/pages/Experiments.tsx`)
  rebuilds the configuration-bundle experiment — list, `?view=new` (agent, trace
  readiness, baseline run) and `?view=detail&id=` with one stage card per step
  (recommend → bundles → gateway/A-B → traffic → verdict/promote → cleanup) posting the
  same actions as the classic page. A cleaned or failed experiment keeps showing every
  stage's result read-only (recommendation diff, bundles, gateway/A-B, traffic, verdict
  metrics, cleanup rows): they are ledger artifacts (`Experiment.artifacts`, merged per
  stage and never removed by cleanup), not AWS reads. The per-experiment RECOMMEND picker state lives in
  `lib/experiments.ts`, shared with the classic page. Runtime canaries are its first and
  default tab (`mode=canary`; the experiments tab is `mode=configuration`, and its
  `?view=new|detail` sub-pages open without `mode`; `v2/pages/canary/`): list, `canary=new` (champion + candidate prompt /
  Studio code, accepting the experiment's promote hand-off `champion=` / `sourceExp=`)
  and `canary=<id>` with setup plus one card per ramp stage (90/10 → 50/50 → 1/99:
  traffic, verdict, advance/complete with the non-significant override confirm),
  rollback and cleanup. A **Harness canary** (`artifacts.kind = "harness"`,
  `optimization/canary_harness.py`) A/Bs two EXISTING versions of one managed Harness
  instead of minting a candidate: `canary=new` asks for the control version (default the
  first) against the latest, which `DEFAULT` already serves. A Harness ARN is not a valid
  `http.agentcoreRuntime` Gateway target (the field validates a `runtime/` ARN), so setup
  pins two Harness endpoints (`ctl<id6>` → control, `trt<id6>` → treatment) and fronts
  each with an HTTP **passthrough** target (`protocolType CUSTOM`, SigV4
  `bedrock-agentcore` via the gateway role, endpoint
  `https://bedrock-agentcore.<region>.amazonaws.com/harnesses/invoke` with `harnessArn` +
  `qualifier` as static query parameters), turns on the dedicated gateway's trace
  delivery, and scores each variant with an online eval on that endpoint's telemetry
  (`service.name = harness_<harnessName>.<endpoint>`, log group
  `/aws/bedrock-agentcore/runtimes/<backingRuntimeId>-<endpoint>`, created up front
  because CreateOnlineEvaluationConfig refuses a missing group). Measured live
  2026-09-30: clients POST the InvokeHarness JSON body to `<gatewayUrl>/<target>/` (the
  `/invocations` suffix is a 404, and the trailing slash is load-bearing: a bare
  `/<target>` still answers 200 but bypasses the A/B test's `gatewayFilter`
  `/<target>/*`, so no session is ever attributed or scored), the gateway role is authorised as
  `InvokeAgentRuntime` on the harness ARN (CDK grants that plus `InvokeHarness` on
  `harness/*`), and the Harness spans carry the gateway's `routing_experiment_variant_name`,
  so `GetABTest` reports per-variant results. Dataset replay into a canary of **either
  kind** is **paired** instead (`canary_harness.PAIRED_MODE`,
  `canary_service._paired_traffic`): every question is sent to BOTH endpoints — a Harness
  through InvokeHarness with `qualifier`, a Runtime canary through InvokeAgentRuntime with
  `qualifier` = its stable (current version) or treatment (candidate) named endpoint, so
  the call lands in that endpoint's own log group and is scored by the per-endpoint online
  eval. Each side gets a fresh session, the canary traffic runtime user id and an
  agent-scoped synthetic memory actor (`scoped_actor(agent, "canary-traffic")`); at most
  `TRAFFIC_MAX_CONCURRENCY` calls run at once, transient errors are retried once, a budget
  stop skips whole pairs, and a failed side is recorded on the pair (a side that ended on
  the agent's own budget, a timeout after its retry or an iteration / token limit, keeps
  its pair and is scored as it stands, marked "budget stop" in the per-question table,
  while any other failure leaves the pair out). The gateway's random split therefore never
  hands the two versions different question mixes (issue #298: a weighted 50-prompt
  replay at 90/10 left a Runtime candidate 2 sessions), and repeating a set is not needed.
  Only real platform traffic still goes through the gateway, at the stage weights, so the
  ramp keeps limiting how much of it reaches the candidate. A Runtime arm call is metered
  per call: exact tokens when the runtime reports usage, otherwise the agent's turn
  estimate as a `job_estimate`. A Runtime round recorded before paired replay (a
  weighted attempt, no `mode`) keeps its `GetABTest` verdict; new traffic in such a round
  appends a paired attempt, and the verdict then reads only the paired attempts. The
  verdict reads both sessions' scores from the two arms' online-evaluation results
  (100 % sampling on each endpoint, no variant filter) by session id, waits until 90 % of
  the pairs are scored (30 min cap; judge lag ≈ 10 min), and compares per evaluator: both
  means, the mean difference, better/worse/equal counts from the candidate's side
  (polarity-aware) and a two-sided paired sign-flip p-value (exact up to 16 non-zero
  pairs, seeded Monte Carlo beyond); the per-question rows are kept with the verdict.
  Both canary kinds choose their evaluators at create (`online_evaluators`, default
  GoalSuccessRate + Helpfulness; built-ins and numeric custom judges that need no ground
  truth, at most 10, refused with a 400 before any row is written) and may name a
  `primary_evaluator` that alone decides the verdict's winner, significance and sample
  size — the V2 create flows always send one (the first chosen custom judge, else
  GoalSuccessRate), the others stay on the page as reference. Paired scores are keyed by
  the evaluator ARN's id tail (a custom judge's evaluation name is not its id), the 90 %
  wait covers the configured evaluators, and one with no score by the cap is reported as
  unscored (an unscored primary is `insufficient-data`). Without a primary the
  aggregate decides (`compute_verdict`). Either way a winner or a tie needs at least
  `min_n` (3) samples on **each** arm — the primary on its own counts, the aggregate on
  the largest count per arm over its evaluators (they score the same sessions, so a sum
  would let 2 sessions × 2 evaluators pass as 4) — and the verdict reports `n_control` /
  `n_treatment` next to the combined `n`; otherwise it is `insufficient-n`. The gate is
  shared with configuration-bundle experiments; a combined `2 × min_n` gate used to let
  47 control + 2 treatment sessions through, and the sign over 2 sessions decided. A
  paired verdict has equal arms, so it needs 3 pairs. More evidence means more questions
  in the dataset, not more passes. Complete is ledger-only (treatment is
  already `DEFAULT`) and may run straight from **50/50** on a `treatment-wins` or `tie`
  verdict that came from paired replay — any verdict of the two for a Harness canary,
  while a Runtime 50/50 verdict from the legacy weighted path does not qualify
  (`canary_service.early_complete_allowed`, exposed as `early_complete_allowed` on the
  canary API output so the console reads the rule instead of re-deriving it; a tie or a
  non-significant win still needs the override, control-wins and insufficient evidence
  stay blocked): paired replay already gave both versions every question, so 1/99 would
  only replay the same questions into the same two endpoints without new comparative
  evidence. The 90/10 stage is
  optional too, for both kinds: the create request's `start_stage: 1` (the 放量计划
  checkbox on `canary=new` and in the architect's step 4; ticked = run 90/10, the default
  on `canary=new`, while the architect's step 4 starts unticked, i.e. at 50/50) opens the
  A/B test at 50/50; `start_stage` > 1 is a 422. For a Harness, `DEFAULT` already serves
  the treatment, so 90/10 caps no exposure and only starves the treatment of samples. For
  a Runtime canary it is an opt-in with a real cost, which the unticked checkbox warns
  about: as soon as the A/B test exists, the gateway route sends half of the agent's
  platform invokes to the never-served candidate; with paired replay, running 90/10
  first costs one more round, not samples. A Runtime canary stores the choice as
  `artifacts.start_stage` (only when > 0), a Harness under `artifacts.harness`.
  `setup.start_stage` and `complete.completed_at_stage` record where it opened and left,
  and the detail page marks the stages outside that range skipped. A Harness rollback
  re-publishes the control version's behaviour (GetHarness at that version →
  UpdateHarness, a new version); its cleanup also deletes both Harness endpoints and the
  trace delivery. While a Harness canary is live, non-streaming platform invokes of the
  agent go through its gateway (fallback: the control endpoint). The architect's
  next-steps panel (`v2/pages/assistant/NextSteps.tsx`) drives this as step 3 (AI
  recommendations — AgentCore or `gepa_lite` — from a chosen clean run of the agent on any
  dataset, the newest by default, with the other runs' recommendations, an accepted one
  included, listed underneath; accepting one opens the prompt for review/editing and
  re-publishes the Harness as a new version with the reviewed text) and step 4's 金丝雀实验 (`HarnessCanary.tsx`). In V2 the classic
  `/evaluation?view=online|experiment` URLs are mapped onto these pages (`EvaluationRoute`
  in `App.tsx`: `oe=<id>` becomes the 持续评估 tab's `view=detail&kind=online&id=` and
  `oe=new` its continuous form, `exp=` becomes `view=detail&id=`); the classic evaluation
  page and its section nav are no longer reached from the V2 sidebar.
- **What is native V2.** The workbench (`/v2`), Agent management (above) and the Agent evaluation module:
  数据中心 `/v2/eval/data` (tabs: Agent trajectories = observability traces with a
  trace/session detail, datasets with a record editor and 复制所选到新数据集 — the picked
  records copied as stored, assertions / every turn / expected trajectory included, with
  `metadata.copied_from` — data-processing pipelines),
  评估任务 `/v2/eval/tasks` (one-off runs and, on its 持续评估 tab, online evaluation
  configs), 评估总览 `/v2/eval/insights` and 评估器 `/v2/eval/evaluators`. Sub-pages follow the console convention: `?view=` states
  of one route (`view=new|detail|edit|trace|dataset|pipeline…`). Every other
  sidebar entry (agent development, runtime, experiments, administration) is a
  classic module rendered inside the V2 shell until it is rebuilt natively.
- **Evaluation tasks cover two resources, by how they run.** A batch evaluation run is
  a one-off task (time window, hand-picked sessions or a dataset replay, evaluated
  once; `name`/`description` stored on the run row) and is listed on the 一次性评估 tab
  (`TaskList`, runs only — an online config's on-demand insights report, a run with
  `dataset_name` `online:<configId>`, is left to that config's 报告 panel); an online evaluation config is a continuous task (sampling
  rate, session timeout) and is listed on the 持续评估 tab (above). There is one 新建
  entry: a strategy switch (`pages/tasks/StrategySwitch.tsx`) over the run wizard
  (`TaskWizard`) and the online config form (`OnlineEditor`), carrying the chosen agent
  across. 首页 (and its launch checklist) and 评估总览 still read both as one `V2Task`
  list (`v2/tasks.ts`: runs plus agent-owned configs — 评估总览 only the scores-mode
  ones — whose description is the task name), with status mapped onto queued /
  running / completed / failed / stopped / paused, and open each in its own detail
  page. A one-off task picks
  an evaluation type: evaluator scoring, or insights (failure analysis / user
  intent / execution summary — the classic run form's insights mode,
  `mode: "insights"` on `POST /api/eval/runs`). An insights task's detail shows
  the run's clusters instead of scores; clustering needs at least 3 sessions, so
  hand-picked sessions require 3 and a smaller window/dataset warns. Continuous
  insights are created on the continuous form (they carry a report schedule).
  The run list shows the evaluation type under the evaluators (and as a filter); to
  stay within eight columns the update time rides under the creation time, and the
  operations column is pinned to the right edge.
- **数据处理 · 运行日志 source** (`v2/pages/data/LogSource.tsx`, backend
  `app/evaluation/pipeline_logs.py`): a pipeline can read 1–10 CloudWatch log groups
  instead of observed traces, which brings in agents that emit no OTel spans (or only
  their own JSON logs). Its wizard gains a 格式转换 step: a format rule embedded in the
  pipeline — `genai` (OTel GenAI content records, read like the session transcript),
  `message` (session / role / text fields) or `exchange` (input / output fields), all as
  JSON dot paths with no user regex — next to a live preview from
  `POST /api/eval/pipelines/preview-logs` (raw records beside the converted sessions,
  parse failures by reason, field-path suggestions), produced by the converter a run
  uses. Converted turns go through the trace extractor unchanged (`log-<session>`
  scenarios, `metadata.source = "logs"`, the datasets list's 运行日志 origin). Logs
  feed datasets only: AgentCore Evaluation reads OTel spans, so custom-format logs are
  never evaluated directly — they become questions and reference replies to replay
  against a platform agent. Either source can store inputs only
  (`processing.keep_replies: false`).
- **日志 data source** (`v2/pages/tasks/LogStreamPicker.tsx`): the wizard lists the
  agent's runtime log streams in a window (default 7 days) through
  `GET /api/eval/agents/{id}/log-streams` and filters them by keyword — stream name
  or log content (see the Data Processing API). One row is one session: a code
  runtime's `[runtime-logs-<sessionId>]` stream, or for a Harness runtime (which
  names streams per microVM) that session's slice of `otel-rt-logs`; streams no
  session owns are hidden behind a toggle and never selectable. The picked
  sessions start an ordinary `session_ids` run (no agent invocation), tagged
  `session_source: "logs"` so the list shows the source as 日志.
- **Agent 轨迹 data source** (`v2/pages/tasks/SessionPicker.tsx`): the agent's
  sessions observed in the last 7 days (`/api/observability/sessions`), each row
  showing the session's first user input and last agent reply so a session is
  picked by content. Previews load one page (10 rows) per
  `POST /api/observability/session-previews` call — the transcript sources read in
  parallel (chat ledger, Memory, eval content logs; for a session no ledger row
  claims, the agent's runtime content logs from its first span on), no long-term
  record count. 全选 picks every row the status / session-id / selected-only
  filters leave, the header checkbox the page; 查看 opens the whole conversation
  in a drawer that can also (de)select the session.
- **Evaluation target: platform agent or CloudWatch telemetry.** A task can
  evaluate an agent that is not a platform agent (for example one not hosted on
  AgentCore Runtime) straight from its CloudWatch telemetry
  (`v2/pages/tasks/LogSourceFields.tsx`): the operator gives the span
  `service.name` and 1–10 input log groups — picked from services discovered in
  `aws/spans` (`GET /api/eval/log-services`, which also pre-fills the content log
  group the spans' resource names and flags a service a platform agent owns) and
  a log-group search (`GET /api/eval/log-groups`). The run carries
  `log_source {service_name, log_group_names}` instead of `agent_id`, which is
  exactly `StartBatchEvaluation`'s `cloudWatchLogs` source. Only passive scopes
  apply: 链路 (a time window) or 日志 (sessions of the service in those log
  groups, found from its spans with Logs Insights — `GET /api/eval/log-sessions`
  — since an off-runtime agent's content logs need not carry a `session.id`);
  Agent 轨迹, dataset replay and continuous evaluation need a platform agent.
  The ledger row keeps `agent_id = ""` and the service name as `agent_name`.
- **Results are shown, not exported.** Task detail and 评估总览 read the judged
  records (`GET /api/eval/runs/{id}/results`, `GET /api/eval/online/{id}/results`)
  into one row model (`v2/results.ts`): outcome, raw and **normalized** score
  (0–1, penalty evaluators inverted via `evaluatorPolarity`), label, explanation.
  A normalized score below 0.7 is a Bad Case. 评估总览 aggregates the latest 12
  completed scored runs of the window (insights runs carry no scores) plus every continuous task, with KPIs, a
  per-evaluator breakdown, filters, CSV export and "bad cases → dataset", which
  feeds `POST /api/eval/datasets/from-sessions` (see the Data Processing API).
  Its **评估洞察** panel (`v2/InsightsPanel.tsx`) covers the completed insights runs
  of the window from the clusters already on the run rows (no extra read): task /
  failure-category / intent / summary counts, then the failure, intent and
  execution-summary clusters merged by name across tasks, biggest first, each
  with its top recommendation; a cluster opens a drawer with sub-categories, root
  causes and recommendations, affected sessions and links to its source tasks.
  The task, agent, source and search filters narrow the panel too; the
  evaluator / outcome / score-band filters are score-only.

### First-run home and Time to First Agent (roadmap T01/T02)

- **TTFA metric.** `User.first_login_at` is stamped once by
  `users.record_login` on a registered account's first successful login (an
  account that logged in before the column existed keeps NULL; the built-in
  admin has no row). `Agent.owner` is now the creating identity's username,
  stamped server-side by `POST /api/agents` and `/convert` (the assistant
  approval path already stamped the approver; presets stay `system`, discovery
  imports `aws-discovery`; older rows keep the legacy `river` default).
  `app/services/ttfa.py` computes, per account in the current workspace, the
  span from `first_login_at` (fallback `created_at`) to the end of the earliest
  **succeeded** `Deployment` of an agent it owns (owner matched
  case-insensitively on `username_key`; deleted agents still count; clamped at
  0). `GET /api/overview/ttfa` (**ADMIN** in `route_policy` — it lists per-user
  activity; ledger only, no AWS call) returns `{median_seconds, samples,
  users: [{username, ttfa_seconds|null, first_login_at, first_agent_at}]}`.
  Pending accounts are excluded; admins, members granted the workspace, and any
  owner of an agent in it are listed. The `/api/overview` contract is unchanged.
- **Empty state.** When the agents list has loaded and is empty, 工作台 replaces
  the KPI row with a "launch your first agent in 3 steps" hero
  (`v2/pages/home/FirstAgentHero.tsx`): describe it in one sentence → the
  architect assistant (primary), start from a template, configure it yourself.
  The targets live in `FIRST_AGENT_PATHS` (`home/common.ts`); the template step
  points at the wizard until the template gallery ships. Once agents exist the
  KPI row returns and the lifecycle card's assistant button is a primary button.
  Admins get a TTFA KPI tile (median + sample count); it is hidden for members
  and whenever the endpoint fails (403, older backend).

### Glossary hints and display names (roadmap T03/T04)

- **Glossary.** `v2/Glossary.tsx` exports `HintIcon` (a focusable (i) whose
  `role="tooltip"` bubble is referenced by `aria-describedby`, shown on hover and
  keyboard focus), `Term` (dotted-underline inline term) and `HintLabel` (label +
  icon, for `Field` labels). Sentences are i18n keys `glossary.<term>` with short
  names under `glossary.name.<term>`; the term list is `GLOSSARY_TERMS`. The V2
  sidebar takes an optional `hintKey` per `V2NavItem` (`v2/nav.ts`), rendered as
  the entry's native `title` so the sidebar stays uncluttered. `OptionCard` takes an
  optional `hint` string — a tooltip wired through `aria-describedby` on the card
  button itself (no nested focusable). The V2 agent wizard uses them on the method
  cards, the A2A protocol card, and the resource-name, gateway, remote MCP, skills,
  knowledge-base and memory labels.
- **Display names.** `AgentSpec.display_name` is optional (1–64 chars, any
  Unicode, whitespace-trimmed; blank ⇒ `None`). It lives only inside the JSON
  `spec` — no ledger column, never sent to AWS — while `name` keeps its
  `^[a-z][a-z0-9-]{2,47}$` slug rule and stays immutable because it names every AWS
  resource. Unlike `name`, `display_name` is editable (and clearable) on
  `/redeploy`. `_agent_out` (list + detail) and the public `GET /v1/agents` surface
  it top-level via `schemas.agent.display_name_of(spec)` (None for older specs).
  The architect-assistant proposal path does not set it. The V2 wizard's first
  field is the display name; the slug is auto-derived (`slugFromDisplayName`:
  lowercase ASCII letter/digit runs joined by `-`; if that is not a valid slug,
  a stable `agent-` + 6 random `[a-z0-9]`) and shown beneath as an editable
  "Resource name" — a manual edit sets the UI-only `AgentForm.nameEdited` and stops
  derivation. V2 agent list/detail, home recent agents and the chat picker show
  `display_name || name` with the slug as secondary text; the classic wizard has no
  input but carries a stored display name through a re-publish.

### Wizard quick mode and inline knowledge base (roadmap T06/T07)

- **Quick mode** (V2 `AgentWizard`, managed Harness only). A fresh create (no
  `edit`, no `gateway=` / `skill=` prefill) opens the configure step on three
  inputs: display name (slug derived beneath), "What should it do?" (the
  `system_prompt`) and knowledge. Model, max tokens, reasoning effort, tools,
  skills, memory, loop and timeout sit behind an "Advanced settings" toggle (it
  opens by itself if one of those fields is invalid), and "Switch to the full
  form" / "Switch to quick setup" flips between the two views over the same
  `AgentForm` state. Quick mode is a pure view: the form keeps today's defaults,
  so `buildAgentSpec` posts exactly the `AgentSpecInput` the full form would. The
  Steps header (method, configure, review) is unchanged; a re-publish always opens
  the full form.
- **Inline KB.** The knowledge field (quick mode and full form, every method that
  can mount KBs) lists ACTIVE managed KBs as before and adds "create a new
  knowledge base": a name (default `<resource-name>-kb`, editable) plus files. On
  "Create and mount" the wizard reuses the existing endpoints, no new route:
  `POST /api/knowledge-bases` (upload source), `POST /{kb_id}/files`, and then
  follows `GET /{kb_id}` every 5 s, starting the first ingestion with
  `POST /{kb_id}/data-sources/{ds_id}/sync` once the data source is AVAILABLE (as
  the KB detail page does). The KB id enters `knowledge_bases` only after create
  and upload both succeeded; a create error stays on the configure step with no
  mount, and an upload error keeps the created KB unmounted with a retry.
- **Deploy gating.** Ingestion never blocks deploy; the review step shows the KB
  name with its state (creating, preparing data source, ingesting, ready,
  failed) and the note that the agent answers from it once ingestion finishes.
  The KB itself must be ACTIVE before submit (the deploy creates its Retrieve
  target), which takes about 2 to 3 minutes after creation; a FAILED KB blocks
  submit. A KB created and then abandoned stays in the Knowledge Bases module.

### Deploy progress, try-chat and suggested questions (roadmap T08/T09)

The V2 agent detail view (`?view=detail&id=`) leads with one plain-language line
derived from the running stage and the agent's method (`agents/deployStatus.ts`:
harness "about 30 seconds", zip packaging "1 to 3 minutes", container build "2 to 4
minutes") plus elapsed time from `Deployment.started_at`. The five-stage cards and
job log sit behind a "Technical log" disclosure that is collapsed unless the deploy
failed, in which case a plain-language failure alert shows and the log opens.
Once the agent is `active` and invocable, an inline try-chat panel (`agents/TryChat.tsx`)
streams through the same `chatApi.stream` + `sseEvents` transport as the Chat
console (no second transport), keeps the last 8 messages and links to
`/chat?agent=<id>`. Chatting is allowed in prod, so the panel ignores the prod lock.

`GET /api/agents/{agent_id}/suggested-questions?lang=en|zh` (`MEMBER`) returns
`{"questions": [3-5 strings], "source": "model"|"fallback"}` for the panel's chips.
`services/suggestions.py` builds a tight prompt from the spec's `system_prompt`,
mounted knowledge-base document names (`knowledge.sample_document_names`, first data
source, one page) and tool names, then makes one non-streaming Bedrock Converse call
(`maxTokens` 300) through the client funnel. Any failure, unparsable output or an
empty spec yields generic localized questions, so the route never 500s (only an
unknown agent id is 404). Results are cached in-process, keyed on agent, language and a
spec fingerprint (prompt, KBs, tools): 10 minutes for model output, 60 seconds for a
fallback so a failing model is not retried on every poll.

## Console V3 (preview)

The default, dark "command center" console under `/v3/*` (`frontend/src/v3/`), built
beside V2 rather than replacing it. It reads and writes through the same `api` /
`dlcApi` clients, backend routes and permission checks as V2 — there is no V3-only
backend surface.

- **Switching.** V2's top bar has **Try V3** (`data-testid="v2-switch-v3"`); V3's has
  **Back to V2**. A browser with no stored choice lands on `/v3`; **Back to V2**
  stores `v2` in `launchpad_ui_version` (`lib/ui-version.ts`) and **Try V3** stores
  `v3`. Opening any `/v3` page counts as choosing V3.
- **Native pages.** Command center (`/v3`: a ranked "needs you" queue built from
  failed deploys, releases awaiting signature and gates that stopped —
  `v3/signals.ts`), Agents (`/v3/agents`, `?id=` for one agent: a header with
  Edit ▾ / ⋯ More — convert, roll back, delete with a typed-name confirm; an
  imported runtime is removed from the platform instead — and the tabs overview /
  deploy & versions / access & integration / release, kept in `?tab=`), Chat (`/v3/chat`, the same
  `chat_stream` SSE chain), and Release gate (`/v3/gate`: the four-gate pipeline,
  per-criterion intervals, sign / block / roll back / run the gate).
- **Rebuilt modules (batch 1, Agent build).** `/v3/create` is the launch page: the
  scenario templates (`GET /api/agent-templates`) and a blank start fill a quick
  managed-Harness form (name, instructions, mounted knowledge bases) that posts the
  same `buildAgentSpec` output as the V2 wizard and lands on the agent, whose detail
  polls while it deploys; other methods and non-Harness scenarios hand over to the
  full V2 wizard (`?method=` / the new `?scenario=<key>` prefill). `/v3/registry`
  lists records with an approval queue (approve / reject in place), a lifecycle track
  per record and the agent card / MCP endpoint / skill source; register and edit stay
  hosted. `/v3/knowledge` is a shelf of knowledge bases and a detail with the
  ingestion track, sync / remove / add source, the retrieve playground and documents —
  it carries V2's create-flow automation (first sync on its own, the bounded wait for
  the backend's data source, the repair offer), since a KB created in the hosted form
  lands here. `/v3/assistant` lists design conversations by where they stand
  (talking, proposal to review, approved) with share and CLEAR (footprint first); a
  conversation's working view stays hosted. `v3TwinOf` (`v3/nav.ts`) maps each
  module's V2 list/detail URL onto its V3 page, so the rail and every V2 link land
  there.
- **Rebuilt modules (batch 2, Agent run).** `/v3/releases` (the dev→ops release
  desk: requests waiting on a reviewer first, then approved-not-executed, in flight
  and history; a release's detail at `?id=` with review / execute / rollback behind
  confirms), `/v3/environments` (an agent × environment matrix with drift against
  AWS on demand), `/v3/observability` (a dashboard led by what needs attention,
  sessions and traces, and native session and trace views — the waterfall with the
  span inspector — at V2's `?view=session|trace&id=`), `/v3/memory` (overview,
  short-term actor → session → events, long-term records and retrieval, resources;
  a resource's editor stays hosted), `/v3/governance` (gateways with their targets,
  Policy Engine mode and recent denials, and the tool catalog; the Cedar and
  rate-limit editors stay hosted), `/v3/connections` (the member's as_user grants and
  revoke), `/v3/costs` (spend by agent and by person, month to date, and alert rules
  at `?view=alerts`) and `/v3/issues` (the issue box, answer rules and reviewers, at
  V2's `?view=` tabs). Each mirrors its V2 page's calls and guards; `v3TwinOf` routes
  V2 links to them, including the views they rebuilt.
- **Rebuilt modules (batches 3-4, evaluation, learning, administration).**
  `/v3/insights`, `/v3/intents`, `/v3/data` (traces, datasets, pipelines; trace and
  dataset views), `/v3/evaluators` (list, detail and the judge / derived / code
  editor), `/v3/tasks` (runs failed-first, a run's detail, and the new-run wizard
  with the same pass^k and `confirm_cost` handling as V2), `/v3/online` (list, detail,
  editor), `/v3/experiments` (experiment and canary boards), `/v3/standards` (all nine
  Agent-DLC views; the gate itself links to `/v3/gate`), `/v3/skill-lab` (landing and
  evaluation runs), `/v3/users`, `/v3/workspaces` (list and detail with bootstrap,
  tier, grants, purge), `/v3/identity` (the admin Connections page and gateway
  targets), `/v3/fleet`, `/v3/announcements`, `/v3/videos` and
  `/v3/video-management`. `v3TwinOf` maps these from a table (`MODULES` in
  `v3/nav.ts`) listing, per V2 path, which `view=`s the V3 page rebuilt.
  Where a flow is a large state machine with no data to prove a rewrite against
  (experiment / canary detail and start, Skill Lab editors and wizards, a few result
  tables and pickers), the V3 page embeds the V2 component in a `.v2.v3-host`
  wrapper instead, so its guards are V2's own. Still hosted outright: the agent
  wizard and agent detail/edit/identity, registry register/edit/consumer view,
  knowledge-base create, a conversation's assistant workspace, memory-resource and
  dataset/pipeline editors, Cedar policy and rate-limit editors, gateway detail, and
  workspace registration.
- **Onboarding (`v3/onboarding/`).** A *launch sequence* on the command center —
  workspace ready → first agent live → first conversation → first evaluation → a
  gated release — where every step is read from what exists (`launchSteps`, unit
  tested), never from a click; hidden per workspace once dismissed and folded to one
  line when complete. A first-visit *tour* lights one area at a time (⌘K, the launch
  sequence, the needs-you queue, the signal colours, the module rail, help),
  keyboard-driven and skippable, shown once (`launchpad_v3_tour_done`). The
  *glossary* reuses V2's `glossary.*` sentences: inline `Term` tooltips, a glossary
  dialog, and a ⌘K entry per term. The rail honours V2's business / expert mode
  (`lib/nav-mode.ts`) — a view filter only; ⌘K and URLs reach every page. The "?"
  menu in the top bar reopens the tour, the launch sequence and the glossary.
- **Menu settings (`/v3/menu-settings`, `services/nav_settings.py`).** An
  administrator hides entries from **everyone's** rail and ⌘K, administrators
  included. The list is one hub-global `platform_settings` row (`nav_hidden_modules`)
  holding three kinds of key: module entries by rail key — the entry path before the
  V3 twin mapping (`/v2/skill-lab`, `/v3/costs?view=project`, `V3NavItem.railKey`);
  the pinned entries 指挥中心 `/v3` and 放行门 `/v3/gate` (`PINNED_HIDEABLE`; Agents and
  Chat are not hideable); and the Command center sections 启动序列 `/v3?section=launch`
  and 轮到你 `/v3?section=turn` (`HOME_SECTIONS`). `GET /api/nav/hidden-modules` is
  every account's: the shell reads it for everyone but a steward, applies
  `withoutHidden()` / `withoutHiddenPinned()` before the business/expert slice, and
  shares the set with the page through `HiddenNavContext` (`v3/hiddenNav.ts`), which
  `V3Home` reads to leave hidden sections out (the tour skips their stops, and the "?"
  menu drops its launch-sequence item while that section is hidden). `PUT` is
  the administrators' (`ADMIN_DATA`: a steward cannot) and journaled as
  `console.nav_hidden`. Administrator-only entries are never offered and never
  filtered, so an administrator always keeps Menu settings to undo a choice. Hiding
  指挥中心 removes only its rail / ⌘K entry: `/v3` and the brand link still open it.
  The page's 快速设置 has two exact presets that fill the draft (Save applies):
  **Normal** = `NORMAL_HIDDEN` (`/v2/environments`, `/v2/eval/standards`, `/v3/gate`,
  both sections) and **Full** = nothing hidden; `menuMode()` shows Normal / Full /
  Custom, judged only on the keys the page offers. **Normal is the default**: while the
  row was never saved (`updated_at` null) the console applies `NORMAL_HIDDEN`
  (`effectiveHidden()`); once saved, the stored list is exact (an empty one is Full).
  With 放行门 hidden the Command center runs **without the release gate**: it reads no
  release states and drops the gate wording, the release-coverage notice, gate items
  from the queue and the release column. Like the nav mode it is a view filter, not
  authorization: no route closes, and a failed read hides nothing.
- **Storyline (`v3/story/`, storyline-ux).** The console follows the workshop's
  storyline (define → build → evaluate → release → observe → feedback). The steps are
  code constants (`AGENT_STORY` in `stories.ts`); each one names its page action from
  the one action table `NEXT_ACTIONS` (`actions.ts`), the role that does it and a
  `done` predicate over the facts `GET /api/agents/{id}/story` returns (cached 30 s).
  The current step is the first step that is not done and not optional; later steps
  are dimmed but stay reachable. On top of that: a **next-step card** replaces the
  success toast after the key actions (criteria published / signed, golden set sealed,
  evaluation done, candidate deployed, gate report, release signed, issue closed) and stays until dismissed; the agent detail shows a **journey strip**
  (one segment per chapter, "Continue" or "Waiting on {role}"); storyline pages reached
  with `?agent=` show a **position line** in the page head (`StoryHeader`); the home
  page lists **your turn / waiting on others** from `GET /api/me/next-steps`; empty
  states list the missing steps (`Prereqs`, derived from `AGENT_STORY`); errors carry
  `next` / `needs` in the envelope, which `ErrorCallout` renders as a step to go to;
  and the Agent-DLC term cards (`terms.ts`) link the steps that use each term.
  `/v3/create` opens on the **chooser** (three questions → a recommended method with
  why / trade-off / alternative, table-driven in `pages/create/chooser.ts`). MCP
  servers and system presets are not on the storyline.
- **Call it from your backend (invoke access).** The agent's "Access & integration"
  tab has a card with what a backend needs to call the agent on AgentCore directly
  (`GET /api/agents/{id}/invoke-access`): the Runtime or Harness ARN per endpoint
  (`DEFAULT`, and `live` / `candidate` for a gated agent), a least-privilege IAM policy
  (`InvokeAgentRuntime` on the runtime and endpoint ARNs, or `InvokeHarness`; optional
  session stop and memory read), the JWT authorizer's requirements instead of a policy
  for a JWT-inbound agent, and CLI / Python / TypeScript samples with every value
  filled in. Project settings → Integration lists every agent of the project with one
  merged policy and a CSV / JSON export (`GET /api/projects/{id}/invoke-access`). The
  policy is built by the backend from the ledger; the browser never assembles ARNs.
- **Overlays.** `.v3-reveal` children animate with `fill-mode: backwards`, never
  `forwards`: a filled transform animation makes each block a containing block for
  `position: fixed`, which trapped dialogs inside panels (and a filled opacity
  overrode an overlay's own fade). `Confirm` / `Dialog` close on Esc from anywhere.
- **Every other page is hosted, not handed off.** Once V3 is chosen, the `/v2/*`
  routes render through `V2Frame` (`App.tsx`) inside `<V3Shell hosted="v2" />`, and
  the classic routes through `<V3Shell hosted="classic" />` — same URLs, same page
  modules, V3 chrome. So every link a V2 page makes stays inside V3, and "Back to
  V2" on a hosted page just re-renders the same URL in the V2 shell. The two V2
  pages V3 rebuilt hand over to their V3 page (`/v2` → `/v3`, `/v2/chat` →
  `/v3/chat`; `/v2/chat?full=1` keeps the full V2 chat, which V3's chat links to for
  consent cards). The rail lists the hosted pages under "Modules" in V2's own
  collapsible groups, derived from `v2/nav.ts` (`v3/nav.ts` `hostedGroups`, held
  complete by `v3/nav.test.ts`), and the ⌘K palette (also `/`) reaches all of them
  plus every agent's chat and gate, matching English names and routes under zh-CN.
- **Hosted theme.** V2 names every literal colour it uses as a `--v2-*` token (the
  block under `--v2-mono` in `v2/v2.css`; V2 renders identically), and
  `v3/host.css` re-points those tokens to the V3 palette on the `.v2.v3-host`
  wrapper, plus panel/heading/table treatments. Classic tokens (`theme/tokens.css`)
  are re-pointed on `body.v3-body`, the classic brand amber becoming V3's mint
  since amber is V3's "waiting" signal. V2/classic modals are `position: fixed`
  inside the page, so a hosted `<main>` drops its stacking context
  (`.v3-main.hosted`). The assistant fishbone SVG keeps its literal light palette
  on purpose — it doubles as a standalone SVG download.
- **Styling.** All V3 CSS is scoped under `.v3` and every custom property is prefixed
  `--v3-*`, because the classic `theme/` tokens and class names (`--ink-2`, `.split`,
  `.caret`) are global and would otherwise collide. Signals use one vocabulary:
  `ok` / `wait` / `act` / `info` / `off`.
- **Timestamps.** Ledger timestamps are naive UTC; `lib/timestamps.ts`
  `parseTimestamp` reads them as UTC (V2's `fmtTime` uses it too) instead of letting
  `new Date()` treat them as local time.
- **Check.** `backend/scripts/e2e_v3_console_browser.py` (headless, read-mostly; `--chat`
  sends one message) walks the switch, every V3 page and ⌘K.

## Console authentication and accounts

The platform console has an optional local account gate, independent from both
the Cognito users used by Gateway/Cedar demos and the `/v1` API-key surface.
Setting `LAUNCHPAD_AUTH_PASSWORD` enables it; no AWS call is involved.

Two credential sources back one session cookie:

- the **built-in admin**, config-driven (`LAUNCHPAD_AUTH_USERNAME`, default
  `admin`). It has no ledger row, so a bad row can never lock the console out,
  and its username is reserved against registration;
- **registered accounts** in the `users` ledger table, created by self-service
  registration (`POST /api/auth/register`: username + company email + password)
  with `role=member`. By default they land in `status=pending` with no validity
  window and cannot sign in (`401 auth.account_pending`); an admin approving them
  (`PATCH /api/users/{id}` with `status=active`) starts the
  `LAUNCHPAD_AUTH_REGISTRATION_VALID_DAYS` (default 7) window from the approval
  moment. `LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL=false` restores instant
  activation at registration. Passwords are stored as `pbkdf2_sha256` hashes with a
  per-user salt — stdlib only, no passlib/bcrypt dependency. "Company email" is
  enforced as a configurable free-/disposable-mail blacklist, with an optional
  allow list that wins when set.

`POST /api/auth/login` verifies either source and issues an HMAC-signed HttpOnly
cookie whose payload is `version:subject:expiry` — 12 hours, clamped down to the
account's own `expires_at`. The **role is deliberately not in the cookie**:
authorization is resolved per request (configured admin → `admin`; otherwise the
`users` row is authoritative), so disabling, demoting or expiring an account
takes effect on the very next request instead of when the cookie lapses. The
cookie is otherwise stateless and survives a backend restart; changing the
configured admin credentials invalidates **all** sessions, because the signing
key derives from them.

**Changing passwords and adding administrators.** Any signed-in account changes its
own password with `POST /api/auth/password` (current + new); an admin resets anyone
else's (`PATCH /api/users/{id}` `password`) and creates accounts directly with
`POST /api/users` (active at once, any role including `admin`; promotion of an
existing account is `PATCH … role=admin`). The built-in admin's password can change
too: the new one is stored as a pbkdf2 hash in the `platform_settings` row
`auth.admin_password_hash` (`services/admin_credential.py`) and takes precedence over
the configured `auth_password`, which then stops working. A row that is not a
well-formed hash is ignored, and there are two break-glass paths back to the
configured password: `cd backend && uv run python scripts/reset_admin_password.py`
(deletes the row) or `LAUNCHPAD_AUTH_ADMIN_PASSWORD_OVERRIDE_DISABLED=true` (ignores
it). The session cookie carries a stamp of the account's password hash (the built-in
admin's override hash, empty while there is none), so a password change ends every
other session of that account. The built-in admin is listed in User Management as a
fixed `builtin` row: it cannot be deleted, disabled, demoted or expired. Account
writes are journaled in `audit_events` without the password.

Two guards run in order, and they answer different questions.

**Is the console allowed to be open at all?** An unauthenticated console serves
only loopback callers; anything else gets `403 auth.open_console_refused`. This is
checked per request rather than at startup because the request is the only place
the caller's address is known — `create_app()` cannot see uvicorn's `--host`, so a
startup-only check would be bypassed by launching uvicorn directly, which is how
the EC2 host and any container start it. The check uses the transport peer and
never `X-Forwarded-For` (spoofable). Measured over real sockets, forged
`X-Forwarded-For`, `X-Real-IP`, `Forwarded` and `Host` headers from a non-loopback
peer are all refused. The residual is narrower than "localhost is trusted": since
uvicorn's proxy-header middleware (default `forwarded_allow_ips=127.0.0.1`)
rewrites the peer from `X-Forwarded-For` when the peer *is* loopback, a same-host
proxy that sets that header gets its real client evaluated and refused. Only a
local proxy that forwards remote traffic **without** forwarded headers still looks
local. Either way the branch never runs on the real production path, where
authentication is on.
`LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` accepts the risk; `create_app()` and
`start.py` additionally fail fast so a misconfiguration surfaces at boot.

**Is this caller allowed on this route?** When the gate is enabled, middleware
requires a live session on every `/api/*` route except `/api/health`,
`/api/auth/status`, `/api/auth/login`, and `/api/auth/register`; `/v1/*` is not
guarded, its `X-Api-Key` contract remaining authoritative. Role authorization then
comes from **one declarative table**, `backend/app/core/route_policy.py`, enforced
by a single app-level dependency:

- a dependency, not middleware, because `scope["route"]` is only populated once
  the router has matched — so the check reads the exact `path_format` instead of
  re-implementing path matching (this holds under FastAPI 0.139's
  `_IncludedRouter` wrapping, which also means route enumeration must recurse);
- **default-deny**: an `/api` route with no entry raises `auth.route_unclassified`
  instead of serving, so a new endpoint cannot ship unauthorized;
- `tests/test_route_policy.py` enumerates the live routes and fails on drift in
  either direction, which is what keeps the table honest rather than decorative.

The classification principle (as amended 2026-08-11): **admin is user management
only** (`/api/users*`); **every other console surface is member-reachable** —
registry writes, knowledge bases, governance, evaluation datasets/evaluators,
experiments, canaries, API keys, tools/demos and the studio local-exec scaffolding
included. Invoking an agent (`/api/agents/{id}/invoke`, `/api/registry/a2a-demo`)
was member-reachable from the start — it is the same capability Chat already gives
every member. The studio local-exec routes remain safe in prod run mode through their
own handler guard (refused outright in prod unless an operator opts in), which —
not the route table — is the real boundary there.

**Studio local debug runs on one of two execution backends**
(`studio_exec_backend`, resolved in `app/services/local_exec.py` and consumed by
all three spawn sites — one-shot/streaming `/api/execute*` and the
`/api/conversations` per-turn replays — through the shared
`build_exec_invocation()`):

| | `subprocess` (default) | `docker` |
|---|---|---|
| Runs where | Host subprocess on the dedicated `data/exec-venv` interpreter (`scripts/setup_exec_env.sh`) | One-shot container from `launchpad-studio-exec:latest` (`scripts/setup_exec_docker.sh`) |
| Isolation | env allowlist + rlimits; optional uid drop + IMDS firewall (`--hardened`, needs a root backend) | env allowlist + `--cap-drop ALL`, `no-new-privileges`, read-only rootfs, memory/cpu/pids/fsize flags; optional `--harden-net` network for IMDS denial |
| Prod posture | Refused unless `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` | Served — selecting the backend is the opt-in; explicit `false` still disables |
| Termination | process-group kill | `docker kill` by container name (the CLI client's death does not stop the container), plus a startup janitor for crash leftovers |

Earlier amendments introduced per-user revocation, which survives the opening:
2026-08-07, the **agent-lifecycle routes are member-grantable** via `perm:agents.*`
(`agents.deploy` covering create/redeploy plus the wizard's skill-staging helpers,
`agents.import`, `agents.delete`, `agents.convert`); 2026-08-10,
`POST /api/eval/runs` joined as `perm:eval.run`. Admins implicitly hold all keys,
and a member holds them **by default** — `users.permissions` stores only explicit
denials, toggled per user in the User Management console and enforced on the
member's next request. A denied call answers `auth.permission_required` (403)
naming the missing key.

In a `shared` workspace (the default) data is **not** partitioned per user: every
authenticated account sees — and, since the opening, can mutate — the same agents,
records, datasets, knowledge bases and gateways. An admin can switch a workspace to
`isolated`, after which each member sees only their own, creator-unknown and
admin-created resources of the in-scope kinds — agents and their A2A registry records
and `/v1` API keys included (see *Workspaces — Member resource visibility*); MCP /
skill registry records and gateways stay shared either way. Revoking the `perm:*` keys restores a partial guardrail for
one user (no deploys, no eval runs), but the rest of the console remains writable
for any member; treat member accounts accordingly. The `/users` console module
renders an administrator-required panel instead of firing a request.
`auth.forbidden` is mapped in the `apiErrors` i18n block so any surface that
missed a gate still shows the localized reason.

There is deliberately no setting that disables this table — a flag that turns
authorization off is the vulnerability.

`Secure` on the session cookie and an HSTS response header follow
`run_mode == "prod"`; `LAUNCHPAD_AUTH_COOKIE_SECURE=true` forces `Secure` on in
development. Neither is hardcoded on, because a `Secure` cookie over a plain-HTTP
dev origin is never sent back and an HSTS header there pins `localhost` to HTTPS
in the developer's browser. Leaving the password unset keeps the gate off for
loopback (console open, registration refused with `auth.registration_disabled`,
`/api/users*` reachable as the implicit local admin), preserving the
bootstrap-free local development and test flow.

## Console single sign-on (OIDC)

With the login gate on, administrators can let people sign in through an OpenID
Connect identity provider (sso design §2; SSO-1, `routers/sso.py`,
`services/sso_oidc.py`, `services/sso_mapping.py`). The API is in
[api.md](api.md#console-single-sign-on-api-oidc).

- **Providers** (`console_identity_providers`, platform-wide, administrators only — a
  steward is refused, since a mapping can grant the admin role). Presets fill the
  scopes and claim names for Keycloak, Authentik, Okta, Entra ID, Google and generic
  OIDC. **Test** reads discovery and the JWKS afresh and reports whether PKCE S256 is
  advertised. The client secret is write-only: stored Fernet-encrypted, or as a
  Secrets Manager ARN read with the hub's credentials.
- **Flow.** Authorization code with PKCE S256. `start` writes a one-time `state`,
  `nonce` and code verifier to `sso_login_states` (10 minutes, server side) and
  redirects to the IdP. `callback` deletes the state row before anything else (so a
  replayed callback finds nothing), exchanges the code with the verifier and verifies
  the `id_token` in memory with `joserfc`: the JWKS signature (asymmetric algorithms
  only), `iss`, `aud` / `azp`, `exp` / `iat` with 2 minutes of skew, and the nonce.
  Tokens are never stored. Every outcome ends in a redirect: into the console with the
  session cookie, or to the login page with `?sso_error=<code>`. Journal:
  `auth.sso.login` (success / refused / failed, the reason in the target) and
  `auth.sso.config.*`.
- **Accounts.** An account is found by `(external_idp_id, external_subject)`, never by
  email, so re-binding an address at the IdP cannot take over another account; an
  email already held by a local account is refused (`auth.sso.email_taken`). The
  provider's `account_policy` is `jit` (create the account on first sign-in, active at
  once, with the default workspace grant, valid for `valid_days` and renewed at each
  sign-in) or `existing_only` (an administrator creates the account first). The email
  must pass the provider's `allowed_email_domains`, else the console's registration
  rule. An SSO account has no local password (no
  reset, no change, no registration approval); its session stamp is
  `idp:<id>:<subject>` plus `session_epoch`. Logout also answers the IdP's
  `end_session_url` (RP-initiated logout) when it has one.
- **Mappings.** At every sign-in the provider's group mappings set the global role,
  project roles and project-group roles. Only memberships the mapping created
  (`sso_memberships`) are added or removed; a membership or role an administrator set
  by hand wins, and the user page flags the difference ("differs from the SSO
  mapping"). The access epoch is bumped, so the change applies on the next request.
- **Unchanged.** The built-in admin always signs in locally (the way back in), `/v1`
  API keys and `/share` links are not affected.
- **Deployment.** `public_base_url` must be the browser's console URL: the redirect URI
  registered at the IdP is `<public_base_url>/api/auth/oidc/<id>/callback`. Set
  `LAUNCHPAD_SSO_SECRET_KEY` (a Fernet key) in production; without it the backend
  creates `data/sso-secret.key` (0600) next to the SQLite ledger, which must be backed
  up with it. See [setup.md](setup.md#single-sign-on-oidc--单点登录).

## Managed Knowledge Bases (console 04)

`/knowledge-bases` is the grounding layer, and the one console module that backs
onto **Bedrock** rather than an AgentCore service: a *managed* Bedrock Knowledge
Base is fully-managed RAG — the vector store, embeddings and reranking belong to
the service. `backend/app/services/knowledge.py`, over the `bedrock-agent`
control plane and the `bedrock-agent-runtime` data plane, owns it and is exposed
as `/api/knowledge-bases/*` (`backend/app/routers/knowledge.py`, tabulated in
[api.md](api.md#console-knowledge-bases-api)). There is no KB ledger table: AWS
holds the whole state, and the only thing the platform stores locally is the
`AgentSpec.knowledge_bases` reference on each agent.

**Resource model.** `create_kb` sends `CreateKnowledgeBase` with
`knowledgeBaseConfiguration.type = "MANAGED"` and
`managedKnowledgeBaseConfiguration.embeddingModelType = "MANAGED"` — nothing
about the index is configurable — and `roleArn` is the shared bootstrap role
from the workspace resource map's `kb_role_arn` (a missing key makes `create_kb`
refuse outright, "run its bootstrap"). Documents arrive through S3 data sources
of type `MANAGED_KNOWLEDGE_BASE_CONNECTOR` (`_data_source_configuration`: the
bucket plus `bucketOwnerAccountId` under
`connectorParameters.connectionConfiguration`, an optional prefix as
`filterConfiguration.inclusionPrefixes`, `SMART_PARSING`), each indexed by an
ingestion job (`StartIngestionJob` / `ListIngestionJobs`), and retrieval is
`bedrock-agent-runtime.retrieve` with a `managedSearchConfiguration`. Only
`type == "MANAGED"` KBs are in scope: list summaries do not carry the type, so
`list_kbs` reads `GetKnowledgeBase` per id and drops the rest, and every by-id
path runs `_require_managed`, which answers `kb.not_found` for a VECTOR KB that
really does exist in the account.

**Create returns `202` and finishes off-request.** A KB needs 1.5–3 min to leave
`CREATING` and its data source cannot be created before it is `ACTIVE`, so
`POST /api/knowledge-bases` answers `202` with the still-`CREATING` detail plus
the `source_pending` descriptor, and `_start_source_completion` polls
`GetKnowledgeBase` on a daemon thread (10 s interval, 15 min deadline, its own
client — a request's client must not outlive the request) and creates the data
source the moment the KB turns `ACTIVE`. This replaced an in-request poll that a
~60 s proxy origin timeout cut mid-request, which silently lost the browser's
follow-up upload. Data-source creation is therefore attempted from three places
that can race — the create path, that thread, a manual `POST …/data-sources` —
so `_create_data_source` first calls `_find_data_source_at`, which compares the
parsed (bucket, prefix) of every existing connector and returns the match
instead: a second connector for one S3 location cannot be produced. The client
polls `GET /api/knowledge-bases/{kb_id}` and starts the first ingestion itself
once a source reports `AVAILABLE`.

Two sub-pages hang off `?view=` (`frontend/src/pages/KnowledgeBases.tsx`); the
list is the default view, and a `?view=detail&kb=` whose id no longer resolves
goes through the shared stale-deep-link notice rather than a permanent LOADING.

| `?view=` | Shows |
|---|---|
| `create` | Name, description, and the source picker — files to upload, or an existing bucket + prefix (`CreateView.tsx`, `SourcePicker.tsx`). On submit: create, then (upload mode) `POST …/files` with the picked files, then straight to the new KB's detail |
| `detail&kb=<id>` | OVERVIEW (id, ARN, updated, inline description edit, DELETE), ATTACHED AGENTS, DATA SOURCES — bucket/prefix, status, the recent ingestion jobs with their statistics, and a collapsible per-source document page (`ListKnowledgeBaseDocuments`, token-paginated, each row's index status joined with S3 size and upload time) — and the RETRIEVAL PLAYGROUND (`POST …/query`, 1–100 results with scores and source URIs). It polls every 5 s while anything is in flight, auto-starts the first sync of an `AVAILABLE` source that has no jobs yet, and warns plus offers `Repair data source` when an `ACTIVE` KB has no data source at all |

**Sources.** `_resolve_source` accepts two modes. `upload` targets the platform
artifacts bucket under `kb/{kb_id}/`, which is exactly where `upload_files`
writes — files may land before the connector exists, while a KB whose only
sources are elsewhere refuses uploads with `kb.no_upload_target`. `existing`
takes the caller's bucket and optional prefix and validates both
(`_validate_external_source`): the bucket must match S3's own naming rule and the
prefix must be a literal path, because both are interpolated straight into the
grant ARNs below — a `*` or a `/` in the bucket name would widen that grant from
one bucket to the whole account.

**Per-KB IAM.** A BYO bucket also has to be readable by the KB role, so
`_create_data_source` calls `_sync_kb_policy`, which puts one inline policy
`launchpad-kb-<kb_id>` on the role named by `kb_role_arn`: `s3:GetObject` on
`<bucket>/<prefix>*` plus `s3:ListBucket` on the bucket, the latter conditioned
on `s3:prefix` whenever a prefix is set (`_kb_policy_document`). The artifacts
bucket is skipped — bootstrap granted it once. `_delete_kb_policy` removes the
policy again on delete. `roleArn` itself is never validated at create time, so a
wrong `kb_role_arn` surfaces only when ingestion fails.

**Delete.** `delete_kb` refuses with `409 kb.has_attached_agents` (the blocking
names in `detail.agents`) while any agent's spec mounts the KB. `force=true`
runs `_strip_kb_from_agents` first, which drops the KB from every mounted spec
and re-syncs the per-agent gateway target of the **harness** agents only —
zip/container agents have no such target, so touching the gateway for them would
*create* one nothing ever uses. Then the data sources are deleted best-effort,
the per-KB `Retrieve` target is removed (only if the gateway already exists — a
delete never provisions it), the inline policy goes, and `DeleteKnowledgeBase`
runs; a KB still `CREATING` answers `409 kb.delete_conflict`. An already deployed
harness keeps its stale prompt section until its next re-publish — harmless,
because the tool no longer routes to the dead KB.

**Two attach channels, picked by method.** `AgentSpec.knowledge_bases` holds up
to 10 `KnowledgeBaseRef`s (`kb_id` plus a denormalized name/description, so the
prompt and the detail views need no Bedrock round-trip); the spec validator
allows them on `harness`, `zip_runtime` and `container` and rejects the Studio
canvas and `protocol="a2a"`.

- **Gateway channel — 方式B (harness).** `services/kb_gateway.py` owns one shared
  MCP gateway, `launchpad-kb-gw` (Cognito-JWT inbound auth, `GATEWAY_IAM_ROLE`
  outbound, the `bedrock-knowledge-bases` connector), carrying two kinds of
  target: a per-KB `Retrieve` target named `<kb-slug>-<kb_id>` (one per KB,
  globally visible) and a per-agent `AgenticRetrieveStream` target
  `agentic-<agent>` whose `retrievers` are exactly that agent's KBs, with
  `MANAGED` foundation and reranking model types. Every `ensure_*` is
  create-if-missing by name, and the retrieve target adopts a concurrent
  publisher's winner on `ConflictException` instead of failing the publish. The
  harness deployer's **provision** stage bootstraps the gateway
  (`ensure_kb_gateway_persisted`, which persists `kb_gateway_{id,arn,url}` onto
  the workspace), ensures the per-KB targets, syncs the per-agent one, then
  re-renders the `CreateHarness` request — `generate` ran before the gateway
  existed on a first attach — and attaches it as an `agentcore_gateway` tool with
  `CLIENT_CREDENTIALS` outbound auth. `harness.py::_kb_prompt` appends a
  `## Knowledge bases` prompt section naming the gateway's MCP tools
  (`…___Retrieve`, `agentic-…___AgenticRetrieveStream`) and asking for each cited
  source as a Markdown link titled with the document. A retrieval result cites
  `location.s3Location.uri`, an https URL into the data source's private bucket, so
  the console never follows it directly: chat and playground links to an S3 object
  open through `GET /api/knowledge-bases/source`, which signs a 5-minute GET only
  for an object inside a visible managed KB's data source (`knowledge.source_link`).
  The gateway is created
  lazily: nothing provisions it until the first KB-mounting harness deploy, or an
  explicit `POST /api/knowledge-bases/ensure-gateway`.
- **Direct channel — 方式A (container) and `zip_runtime`.** No gateway; the
  generated runtime carries two tools that call the Bedrock data plane with the
  agent's own execution role: `kb_search` → `Retrieve` (one similarity search,
  no FM call) and `kb_deep_search` → `AgenticRetrieveStream` (a planning loop
  that decomposes the question, searches every mounted KB over up to 3 rounds for
  a single KB or 5 across several, and returns a cited answer). Both methods
  derive everything from `templates/kb_support.py` so they cannot drift:
  `mounted_kb_refs` bakes the KB literal into the rendered source and
  `kb_prompt_section` appends the prompt block that steers between the two tools;
  the container exposes them namespaced as `mcp__launchpad_kb__<tool>`. The
  grants are `ManagedKbRetrieval` (`bedrock:Retrieve` + `GetKnowledgeBase`,
  narrowed to the attached KB ARNs by the per-agent role in
  `services/agent_iam.py`) and `ManagedKbAgenticRetrieval`
  (`bedrock:AgenticRetrieveStream`, deliberately `*` — the action is not
  resource-scopable), both defined in `infra/stacks/base_stack.py`; the same pair
  sits on `launchpad-gateway-role` for the gateway channel.

`launchpad-kb-gw` is bootstrap-adjacent rather than bootstrap-created, so
teardown sweeps it by name together with its targets — see
[teardown.md](teardown.md).

## Context layer (COA)

`/v2/context-layer` connects a workspace to one **COA** (Context Ontology
Accelerator) deployment — a separately deployed product with governed metrics, an
induced ontology, a document knowledge graph and access-controlled SQL over source
data. Launchpad never deploys or tears COA down, and never creates COA users,
grants or namespaces: authoring stays in the COA console (the page links to it;
COA forbids framing). Everything is read **as the signed-in member**, so COA's
per-user grants decide what an agent may see.

- **Connect** (`services/context_layer.py`, admin): discovery reads SSM
  `/<prefix>/*` and the `<prefix>-<env>-{api,web,auth}` stack outputs in the COA
  region (`replace(workspace, region=…)`, which may differ from the workspace's),
  refuses a cross-account deployment, a missing agent OAuth client or a runtime
  that is not READY, then creates or adopts the OAuth2 Connection for COA's agent
  client and ensures the console workload identity `launchpad-console-<workspace>`.
  The `context_layers` row (one per workspace) holds identifiers only.
- **Tokens** (`services/context_layer_tokens.py`): a member's COA token comes only
  from the token vault, through the console workload identity, and lives only in
  process memory and on requests to COA — the custody exception documented in
  [identity.md](identity.md) §7.7.
- **Agents** (`AgentSpec.context_layer = {namespace_id, namespace_name, via?}`; pure
  helpers in `templates/context_layer_support.py`). `via` is direct (the default,
  never stored) or `"gateway"` (the managed COA gateway below):
  - *Harness*: the deploy adds a prompt addendum (namespace id + tool guidance)
    and no COA tool. Each console turn (`services/invoke.harness_turn_overrides`)
    re-reads the deployed tools, appends a `remote_mcp` server `coa` pointing at
    COA's MCP runtime with the member's bearer, and adds `@coa` to
    `allowedTools`. No consent yet ⇒ the turn is an `auth_required` event and
    InvokeHarness never runs.
  - *Harness via the gateway*: the deploy attaches the managed gateway as
    `launchpad_coa_gw` (M2M outbound auth, like any user-trusting gateway), selects
    `@launchpad_coa_gw`, and names the tools `launchpad_coa_gw_coa___<tool>` in the
    addendum. Each console turn runs the gateway's consent gate first, then swaps the
    attachment for a `remote_mcp` server carrying the member's pool JWT. A turn that
    cannot swap fails closed (`context_layer.gateway_unavailable`). The COA tools show
    in `GetHarness`, so the code view lists them.
  - *Strands* (`zip_runtime`, HTTP): every deploy stage builds from
    `context_layer.deploy_spec`, which expands the ref into one `as_user` MCP tool
    on the layer's Connection plus the addendum — the template, the execution
    role, the environment and the return-URL allow-list all read that one
    expansion, and the stored spec keeps only the ref. The baked tool carries
    `gates_turn`: when the member has not consented yet, the generated runtime
    emits the `auth_required` card and ends the turn without invoking the model —
    the same outcome as a Harness turn. Any other `as_user` MCP server only sits
    the turn out until its consent (the model still answers). A runtime picks
    this up on its next deploy.
  - *Strands via the gateway*: the expansion adds no tool. The managed gateway's URL
    (ledger only) is baked into `main.py` as `CONTEXT_GATEWAY = {"url": …, "tools":
    "coa___*"}` beside the launchpad-gw client, and the addendum names the tools
    `coa___<tool>`. Each console turn runs the gateway's consent gate in the backend
    first (`invoke._runtime_gateway_gate`), then sends the member's pool JWT as the
    payload's `gateway_access_token`; the runtime loads COA's tools from the gateway
    with it, with no M2M fallback. The code view shows `CONTEXT_GATEWAY`.
  - `/v1`, evaluation replays, canary / A-B and Harness conversion refuse a
    COA-attached agent: none of them has a console user to act as.
- **Module** (`routers/context_layer.py`, page `frontend/src/v2/pages/ContextLayer.tsx`
  + `contextLayer/`, expert-mode nav only). The status is read once and shared by
  three `?view=` sub-pages:
  - *overview*: the deployment card (live runtime / callback / agent-OAuth probes),
    the member's authorization (authorize / revoke), the namespaces COA lets them
    see, and COA's own `/system-health` (`GET /api/context-layer/health`). Admins
    connect, re-connect or disconnect here. Not connected: members are told to ask
    an admin. No consent: an authorize call to action and **no COA call**. No
    namespaces: grants come from the COA admin, with a link to the COA console.
  - `namespace&id=<uuid>[&tab=sources|metrics|ontology]`: the namespace as COA
    shows it, with tabs for sources (type, status, tables), governed metrics
    (unit, synonyms, per-dialect expressions) and ontology classes (one
    `graph/ontology-overview` per ontology, at most 8 read concurrently, 200
    classes each). Each tab deep-links into the matching COA console route
    (`/namespaces/{id}/sources|metrics|ontology/graph`): authoring stays in COA.
    "Attach to agent" opens the V2 wizard (new Harness or Strands agent) or an
    existing agent's editor with `context_ns` / `context_name` prefilled; saving is
    the ordinary create / redeploy.
  - `playground[&ns=<uuid>]`: one question at a time to COA's **Serve runtime**
    (`services/context_layer_playground.py`). The backend proxies
    `POST <serve invocations URL>` with `stream: true`, the member's bearer and a
    runtime session id stable per member + namespace (`lp-` + 40 hex), and
    normalizes COA's frames into `step`, `token`, `done` (answer, tier, confidence,
    up to 200 result rows, COA's `sessionId` for the next turn) and `error`
    (`context_layer.*`) events, with `: keep-alive` comments while COA is silent.
    It never sets COA's `includeDebugInfo`, and never forwards the generated SQL
    (`queryUsed`), because COA withholds it by default: it exposes internal schema. Missing consent is one `auth_required` event, the same card Chat
    shows. The upstream is an `httpx.AsyncClient` stream inside the response
    generator, so a client disconnect closes COA's connection. The read timeout
    is 180 s against COA's 170 s budget.
  - COA REST reads (`services/context_layer_api.py`) are allowlisted projections
    (`schemas/context_layer.py`), never a passthrough. Errors map COA 401 to
    `context_layer.reauthorize` (409), 403 to `forbidden`, 404 to `coa_not_found`,
    and 429 / 5xx / timeout to `upstream`, with at most 200 characters of COA's own
    message.
- **Demo account** (`services/context_layer_demo.py`). Most members have no COA
  account (COA users are admin-created only), so an operator creates one shared,
  read-only account with `samples/coa-demo/demo_account.py` (Cognito user + group
  `launchpad-demo`, whose COA grant survives re-creating the user) and stores
  `{username, password, sub}` as an SSM SecureString under
  `/<coa prefix>/launchpad/` in the COA region. The admin names it on the connect
  form; the row keeps the name only, and the backend never creates or changes COA
  users.
  - Any member may reveal it (`POST …/demo-account/reveal`, `no-store`, logged by
    member) on 我的授权 or on a Chat consent card for the layer's Connection. The
    member then types it at COA's own login page through the ordinary consent:
    Launchpad never signs in for anyone and never falls back to the account, and
    each member keeps their own vault binding. The consent carries no
    `customParameters` for any account choice: the token vault keys a token by
    them, so a consent made with `login_hint` would land in a vault slot that the
    broker, the Harness override, the playground and every Strands runtime never
    read. Nothing is prefilled at COA's login page; the panel's copy buttons are
    the way.
  - A browser that still holds a COA session consents silently as that user
    (COA's classic hosted UI ignores `prompt=login`, which Launchpad could not send
    anyway). So the status compares the vaulted
    token's `sub` with the account's (`authorization.identity: demo | own`), and the
    page guides a switch: revoke, sign out of COA (`sign_out_url`, COA's web-client
    `/logout`, which clears the session for every client on the domain), authorize
    again. A consent that resolved to the other account than the one chosen opens
    that guide by itself.
  - The password lives in SSM, in backend memory for at most 300 s (a rotated
    password shows within 5 minutes), in the reveal response and in the member's
    browser while the panel is open. The `/<prefix>/launchpad/` scope keeps the
    reveal from reading any other secret.
- **Managed COA gateway** (`services/context_layer_gateway.py`, gateway mode). This is
  an opt-in path per agent (`context_layer.via = "gateway"`, the wizard's 接入方式,
  offered while the gateway is enabled; Harness and Strands); direct stays the default. An admin enables one gateway per workspace,
  `launchpad-coa-gw`, from the overview's gateway card. It is separate from
  `launchpad-gw`, which is never modified.
  - `PUT /api/context-layer/gateway` is idempotent and resumable. It reads COA's
    `tools/list` with the admin's console COA token: no consent means a 409 and
    nothing is created. It then ensures the gateway (MCP `2025-11-25` + `2025-03-26`,
    CUSTOM_JWT on the workspace pool admitting the console + M2M clients, so it
    passes `user_gateways.trusts_workspace_users`). It adds the console return URL to
    the gateway's own workload identity, and ensures one target `coa`: COA's MCP
    runtime with an **inline** tool schema and outbound `OAUTH` `AUTHORIZATION_CODE`
    on the layer's Connection. A 3LO target without an inline schema waits in
    `CREATE_PENDING_AUTH`.
  - The ledger keeps `resources["gateway"] = {gateway_id, gateway_arn, gateway_url,
    target_id}`, written as soon as the gateway exists. `GET …/gateway` reads state,
    tools, versions, the return-URL allow-list and drift (`issues`) back from AWS;
    `GET /api/context-layer` carries only `gateway: {enabled, gateway_id}`.
  - Refresh re-reads COA's tools into the inline schema. Delete removes the target,
    then the gateway, and drops the `gw:<gateway_id>` grant and pending rows. It is
    refused while an agent uses gateway mode, and disconnecting the layer is refused
    while the gateway exists. Re-connecting keeps it.
  - **Consent** (`gateway_turn_auth`, the gate a gateway-mode turn runs before the
    agent): a `coa___list_metrics` preflight through the gateway with the member's
    pool JWT (MCP `2025-11-25`, `elicitation.url`). JSON-RPC `-32042` means no
    consent: a `user_jwt` pending session on `gw:<gateway_id>` and the usual
    `auth_required` card, before the model runs. `/auth/return` completes it with
    the same cached JWT (`policy_identity.gateway_user_token`). Any other answer
    means consented, and is remembered for 10 minutes per member. A failure fails
    open.
  - Consents given through the gateway live in the gateway's own vault slot and
    cannot be revoked on the platform: revoke skips `gw:` rows, 我的授权 marks them
    *Not revocable here*, and the card says so ([identity.md](identity.md) §7.7).
    The gateway role needs `GetWorkloadAccessTokenForJWT` (CDK and the spoke
    policy). Teardown sweeps the gateway by name.
- **Limits.** COA cannot be framed (`X-Frame-Options: DENY`), so every authoring
  link opens a new tab. COA's REST data layer caps at 29 s, which is why questions
  go to the streaming Serve runtime and not to `POST /namespaces/{id}/query`. `/v1`
  answers 409 for COA-attached agents; canary / A-B, evaluation replays and
  Harness conversion refuse them, because none of them has a console user to act
  as. COA's spend is in COA's account: the playground is `FREE` in the billing
  table and Launchpad does not meter it.

## The Memory console (console 05)

`/memory` is a **read-only** window onto the shared `launchpad_memory` singleton
(`backend/app/services/memory_console.py`, endpoints under `/api/memory/*`). It
is deliberately separate from `app/services/memory.py`, which sits on the chat
invoke hot path and stays minimal; the console module owns control-plane reads,
actor decoding, namespace resolution and pagination, and imports `SCOPE_SEP` /
`memory_id_or_none` from `memory.py` so the scoping contract has one source.

Read-only is structural, not a UI guard: no wrapper or handler for `CreateEvent`,
`DeleteEvent`, `DeleteMemoryRecord`, `Batch*MemoryRecords`,
`StartMemoryExtractionJob`, `CreateMemory`, `UpdateMemory` or `DeleteMemory`
exists in either file, and `tests/test_memory_console.py` asserts that. The one
mutating surface — the `resources` view — therefore lives in a **separate pair**
(`services/memory_admin.py` + `routers/memory_resources.py`, tested by
`tests/test_memory_resources.py`): it manages the memory *resources* themselves
(create/update/delete), never events or records, and the structural guarantee on
the console modules stays intact.

| `?view=` | Shows | AgentCore operations |
|---|---|---|
| `overview` | resource config (id/arn/status/event expiry/KMS/execution role), each long-term strategy with its `namespaces` + `namespaceTemplates`, and the account's other memory resources with the platform singleton marked | `GetMemory`, `ListMemories`, `ListActors` |
| `short-term` | actor → session → event drill-down; events render as a timeline of conversational role/text turns, JSON payloads (`{json: {content}}`) as a labelled, expandable JSON block, blob payloads as a byte count only | `ListActors`, `ListSessions`, `ListEvents` |
| `long-term` | records for a resolved namespace, plus semantic retrieval with relevance scores. Templates that end in `{sessionId}` segments (summaries, episodes) resolve to the actor-level prefix (`/summaries/<actor>`, flagged `prefix`) because both APIs match namespaces by prefix — the picker reads every session's records at once; only a placeholder in the middle of a path is unresolvable | `ListMemoryRecords`, `RetrieveMemoryRecords` |
| `resources` | every memory in the account/region (workspace default marked, plus the agents whose spec pins each one); create a memory (name, description, event expiry, strategy picks that mirror the bootstrap layout, and — API-only for now — up to 5 flexible namespace variable keys: CreateMemory `namespaceKeys` with optional `allowedValues`/`regexPattern` rules; the platform's canned strategies don't reference them and the invoke path supplies no `extractionConfig.namespaceVariables` on CreateEvent, so the console form hides the editor (`SHOW_NS_KEYS` in `ResourcesTab.tsx`) and the keys are pre-registered for externally managed templates); edit one's description and event expiry (7–365 days) inline — `UpdateMemory` is sent exactly `memoryId` + the changed fields and **never** `namespaceKeys`, which the API documents as replacing the existing set wholesale (an omitted key is removed), then the detail is read back with `GetMemory`; strategies, namespace variables and the execution role are not editable, editing is never blocked, and the confirm dialog names the agents on the memory since a shorter expiry reaches all of them; and delete one — the workspace default and any memory a live agent references are delete-protected | `ListMemories`, `GetMemory`, `CreateMemory`, `UpdateMemory`, `DeleteMemory` |

`ListEvents` payload entries are a tagged union — `conversational`, `blob` and, since
the August 2026 Memory release, `json` (`{json: {content: <any JSON value>}}`). The
console projects each to `{kind, role, text, parts, blob_bytes}`: a `json` entry keeps
`role` null (it is data an agent stored, not a turn) and carries the value serialized
verbatim into `text`, so `false`, `0`, `null`, `""`, arrays and objects all display as
themselves — the projection tests for the `content` key, never the value's truthiness.
Blob bytes still never leave the service, and union members the platform does not
recognise are still omitted. This is a **read** projection only: the platform does not
write JSON events.

Memories created here become selectable per agent: the Create wizard stores the
pick as `spec.memory.memory_id`, which overrides the workspace default across
the deployers (`LAUNCHPAD_MEMORY_ID` env / harness `agentCoreMemoryConfiguration`),
the IAM grant, and the platform's read-back paths (Chat memory rail,
observability transcripts). `None` keeps the shared bootstrap memory, so every
pre-existing spec is unaffected.

**Ownership, not the account, bounds what a spec may pin** (issue #55). The spoke
role holds `bedrock-agentcore:*` on `*`, so any memory in the account — another
team's included — would otherwise be reachable by pinning its id.
`services/memory_ownership.py` defines *managed*: the workspace's bootstrap memory,
or a `managed_memories` ledger row written only when this console creates a memory
or an administrator adopts one (`POST /api/memory/resources/{id}/adopt`); the row
also records who brought it in (see *Resource creators*). A pinned
id must be managed and `ACTIVE` — checked at create / re-publish / convert time and
again by `execute_deploy_job` before any stage, so the execution-role grant and the
runtime binding never see a foreign id, whichever path started the job. Read-back
refuses a legacy unmanaged pin rather than reading it. The lifecycle routes list
unmanaged memories as *not managed* and answer `404 memory.not_managed` on every
per-id route; writes ride the revocable `perm:memory.manage`.

**Extraction is not a console surface.** Turning short-term events into long-term
records is a job the AgentCore Memory service runs itself, asynchronously, from the
strategies configured on the resource — the platform never starts one.
`ListMemoryExtractionJobs` is not a job history either: its `status` enum has exactly
one value (`FAILED`), so it lists only the retry-eligible backlog that
`StartMemoryExtractionJob` would pick up, and a healthy resource answers with an empty
list. Showing that as a tab read as "nothing was ever extracted", so the console
dropped it; `GET /api/memory/extraction-jobs` remains available for debugging.

Two projections carry the load. **Actor decoding:** AWS returns the compound
`<agent_id>__<human>` that `scoped_actor` builds, so `/actors` splits on the
first `__` and resolves agent names in one batched ledger query per page; a
scoped actor whose agent row is gone stays `scoped: true` with a null name,
because the memory partition outlives the agent. **Namespace resolution:**
`ListMemoryRecords`/`RetrieveMemoryRecords` both require a concrete namespace, so
`/namespaces` substitutes `{actorId}` into each strategy template server-side and
flags any template with a leftover placeholder (e.g. `{sessionId}`) as
`resolvable: false` rather than sending a broken namespace to AWS.

Record payloads are strategy-dependent: `SEMANTIC` stores prose in
`content.text` while `USER_PREFERENCE`/`SUMMARIZATION` store a JSON object
(`{context, preference, categories}`). `memory.decode_record_text` — shared by
the console and the Chat rail — extracts a human-readable line, exposes the
parsed object as `structured`, and keeps the original in `raw_text`, so neither
surface renders a serialized object.

The Chat playground's SESSION MEMORY rail links into this page
(`OPEN IN MEMORY ↗` → `/memory?view=short-term&actor=…&session=…`), mirroring its
`OPEN IN OBSERVABILITY ↗` chip. `GET /api/chat/{agent_id}/memory` echoes the
compound `actor_id` it read, and the link uses that verbatim: the recorded
session actor can differ from the request actor, so deriving the partition in the
frontend would link somewhere that does not exist.

There is no TTL cache here — unlike Observability, whose Logs Insights queries
are billed per scan and take seconds, `GetMemory` is a single fast control-plane
read. Every list endpoint round-trips `next_token` (AWS caps pages at 100), and
the overview's actor count reports one page with an explicit
`actor_count_truncated` flag instead of a silently wrong total. Before
`make bootstrap`, `/overview` returns `configured: false` (a soft state the page
renders once) while every other endpoint returns `memory.not_configured` (409);
botocore failures map to `memory.unavailable` (502).

## The Observability module (console 06)

`/observability` is a read-only telemetry console over three data sources
(`backend/app/services/observability.py`, endpoints under
`/api/observability/*`):

| Source | Used for | How |
|---|---|---|
| Legacy `aws/spans` + unified `/aws/bedrock-agentcore/runtimes/*` log groups | trace/session lists, dashboard counts + p50/p95 + hourly series, top tools, span trees | Logs Insights `SOURCE logGroups(namePrefix: ...)`, one bounded query set per view |
| Online evaluation results `/aws/bedrock-agentcore/evaluations/results/<configId>` log groups | ONLINE EVALUATION block on the session detail (scores + judge explanations per config) | one prefix-`SOURCE` Logs Insights query filtered on `attributes.session.id`, run as its own call inside the cached session build so it degrades to `unavailable` on failure |
| `bedrock-agentcore` metrics namespace | tokens-by-model tile + chart | `ListMetrics` (dimension discovery) → `GetMetricData` sums of `gen_ai.client.token.usage` |
| AgentCore Memory `ListEvents` + ChatMessage ledger | session conversation transcript | ChatSession join (`session_id → actor_id`); Memory is primary, while the exact rendered-message ledger repairs lagging/incomplete or historically split actor partitions; harness envelopes are decoded and tool-result turns dropped |

Every view is served from a **60-second TTL cache** keyed by (view, range) —
Logs Insights is billed per scan — with `force=true` (the ⟳ REFRESH button)
bypassing it. Ranges are whitelisted (`1h/6h/24h/7d`); trace ids
(`^[0-9a-f]{32}$`) and session ids (`^[A-Za-z0-9_\-#:.@]{8,256}$` — `#`/`:`/`.`/`@`
admit composite ids such as `<ulid>#feishu#<chat_id>` from external callers) are validated at
the router **and** re-checked in the query builders before being interpolated
into Logs Insights query strings. Token sums use one framework-specific
token-bearing span: terminal LLM operations (`chat` / `text_completion` /
`generate_content`) for Strands, or the native OpenInference `AGENT` root for
Claude Agent SDK. Strands agent-level `invoke_agent` spans and framework
wrappers repeat child/provider usage and remain excluded. Unified groups also
contain prompts, OTel events, structured logs, and standard output;
span-derived queries require `startTimeUnixNano` so correlated non-span records
do not inflate trace, latency, error, token, or tool counts.

Cost figures are **advisory estimates**: token counts × `model_prices` from
`config/launchpad.yaml` (USD per 1M tokens, substring-matched against
`gen_ai.request.model` or native `llm.model_name`; unknown models show token
counts with a `—` cost). The UI labels them `≈ / EST`. The price map is kept fresh from litellm's public
price file (`app/services/model_prices.py`): a daily daemon plus the dashboard's
`⟳ UPDATE PRICES` button (`POST /api/observability/prices/refresh`) pull exact
per-model entries — including regional Bedrock premiums and cache read/write
rates — for every model seen in the account's telemetry, refresh the operator's
short fallback keys, and leave unmatched keys untouched. Source URL and
interval are configurable (`model_prices_source_url`,
`model_prices_refresh_hours`; `0` disables the daemon).

**Telemetry per creation method:** Strands (zip/studio) and harness agents emit
gen_ai spans natively. Claude Agent SDK containers install AgentCore's supported
`openinference-instrumentation-claude-agent-sdk` integration and keep the
existing ADOT launcher (`opentelemetry-instrument python main.py`). The
instrumentation wraps the SDK's `query()` call as `ClaudeAgentSDK.query`, emits
native AGENT/TOOL OpenInference spans, and automatically emits same-scope
structured content events for input/output messages. Model, token, cache-token,
cost, and tool data stay on the native spans. The runtime wraps each query in
`using_session(context.session_id)`, so the native span carries the same
`session.id` used by Chat, Evaluation, and Observability rather than the Claude
CLI's internal session id. Evaluation readiness pairs the completed native span
with its automatic content event by span id; Strands telemetry keeps the same
root-plus-content contract.

Tab IA: **DASHBOARD** (5 stat tiles + traffic/latency/tokens/tools charts) ·
**SESSIONS** (list → detail with Memory/ledger-reconciled transcript + traces-in-session cards) ·
**TRACES** (filterable list → waterfall Gantt with span drawer: token usage
incl. cache read/write, est cost, tool schema, raw attributes). Cross-links:
deep links `/observability?trace=<id>` / `?session=<id>`; the Chat trace rail
links to the current session's detail (`OPEN IN OBSERVABILITY ↗`) and session
detail links back (`OPEN IN CHAT ↗`); `service.name` values are mapped to
platform agent names via the ledger (`resource_id` base-name match, raw name
fallback).

**Span message formats.** Trace detail normalizes both combined
`body.input/output.messages` envelopes (Strands, Harness, and native Claude)
and Bedrock's individual `gen_ai.*.message` / `gen_ai.choice` events. The
individual system, user, assistant, and tool messages describe the request
history; `gen_ai.choice` contains the current completion and its finish reason.
Strands envelopes may also carry a separate `tool.result` beside `message`.
MCP tool-call spans expose their own `gen_ai.tool.call.arguments` and
`gen_ai.tool.call.result` attributes through the same input/output panel when
content events have not populated that side. Every projection stays attached
to its original span: a child never inherits its parent's prompt or response.
Message, block, and text limits keep this a bounded inspection view.
Session provenance is collected independently from recognized content events,
including events beyond display limits or with unrenderable payloads, so
truncation cannot weaken the trace's privacy checks.

**On-demand scoring (SCORE NOW).** The session detail can score the session
right now with 1–5 evaluators (built-in, third-party managed or custom — the
same list the run wizard offers) through the AgentCore **data-plane
`Evaluate`** API — the fourth data source of the page, and the module's only
write-like call. `POST /api/observability/sessions/{id}/evaluate` runs one
Logs Insights query over `SPANS_SOURCE` (`filter ispresent(scope.name) and
attributes.session.id = "<id>" | fields @message | sort @timestamp asc | limit
2000`), parses every `@message` as a span document (non-JSON rows — stdout,
structured logs sharing a unified group — are skipped), and calls
`evaluate(evaluatorId, evaluationInput={sessionSpans})` **once per evaluator,
sequentially** (each call is a judge model inference; ≤10 results per call).
The wrapper lives in `services/agentcore/evaluation.py`; the client is the
same `bedrock-agentcore` data-plane client the invoke chain uses. Contract:
synchronous, **not cached and not persisted** (the panel says so; the ledger
never sees these results), session-level only (no `evaluationTarget`, no
ground truth), partial failures come back as **error rows**
(`error_code`/`error_message` per result) rather than a failed request, and a
session whose spans have not landed yet answers **409
`observability.session_spans_missing`** with the readiness hint (spans reach
CloudWatch a couple of minutes after the invoke). Use it to test a custom
evaluator or investigate one session; a stored, repeatable score is the batch
run's job (below).

### Spend attribution and threshold alerts

**Spend.** The dashboard already estimates cost per model from the `model_prices` map;
what it cannot answer is *who spent it*. `services/costs.py` adds two breakdowns over the
same spans (`SPANS_SOURCE`, the dashboard's `_IS_LLM_FIELDS` token logic): **by agent**,
from one Logs Insights query grouped by the span's runtime service name, and **by person**,
from per-session tokens joined to `ChatSession.actor_id`. Sessions the console never
opened (direct `/v1` traffic, evaluation replays) land in an explicit unattributed row
rather than being dropped, and a service name that matches no ledger agent is still
reported (`known: false`) and counted — an imported or deleted agent, or another stack on
the same AWS account (prod and prod2 share one), spent real money. Every figure is an
estimate: each row is priced at its latest model's rate, and a model missing from the price
map adds tokens but no dollars and is named in `unpriced_models` (tokens under no model id
as `(unknown model)`), so the total is never quietly short. A non-Bedrock provider (Mantle
`openai.*`) emits only the Strands wrapper span, which the dashboard's token logic drops as
a duplicate; a row with no provider tokens is priced on its wrapper tokens instead, the same
fallback the dashboard's tokens-by-model uses (measured on dev 2026-10-14: five such services
over 30 days had shown 0 tokens). The per-person query is sorted by output tokens before its
1000-session cap, so the sessions that fall off are the smallest, not an arbitrary set. Month to date is a relative window from 00:00 UTC on the 1st, capped at
720 h. Results go through the dashboard's 60 s cache. The scan is account-wide, so its
duration grows with all span volume in the region, not with this console's agents
(measured 2026-10-14: 34 s for 30 days on us-east-1, against the 55 s query deadline).

**Alerts.** An `alert_rules` row (workspace-scoped) is a threshold on a value the
platform already computes: `error_rate` and `latency_p95_ms` from the dashboard
aggregates, `online_quality` from the online-evaluation mean, `spend_usd` and
`cost_mtd_usd` from the cost report. No new telemetry path. The properties that matter:

- **A direction that could never fire is refused** (`alert.comparison_never_fires`):
  quality is bad when it drops, an error rate when it rises. The create route defaults the
  comparison.
- **An unreadable value is `unknown`, never `ok`.** No traffic in the window, or a failed
  query, records `unknown` with the reason.
- **A transition notifies, not a state.** `state` lives on the row, so a rule that stays
  firing does not re-notify: one notification per firing episode, which starts at
  `last_fired_at`. A manual re-check that sees the transition first records it without
  posting, and the next background pass still delivers it (`last_notified_at` older than
  `last_fired_at` = owed), so clicking "Re-check now" during an incident cannot silence the
  webhook. A failed delivery is recorded in `last_detail` and never hides the breach.
- **Delivery is one generic https JSON webhook**, which a Slack or Feishu incoming hook
  already is, so the platform stores no channel credentials. `services/webhook_safety.py`
  checks the URL on save and again on every send (https only, no credentials in the URL,
  every resolved address public — IPv4-mapped IPv6 judged as its IPv4 address), connects
  to the address it checked, refuses redirects, times out after 5 s and never reads the
  response body.
- **Only the background check notifies.** `POST /api/alerts/evaluate` (the console's
  "Re-check now") records state but never posts, so opening the page cannot page anyone.
  The background check is the DLC scheduler's 60-second tick (`app/dlc/scheduler.py`,
  `evaluate_alerts`, every 5 minutes): it evaluates every workspace's rules with
  notification on, and its `scheduler_claims` row keeps a second backend process on the
  same ledger from running the same pass.

Every route is **admin**: the per-person breakdown names who spent what, and an
online-quality value is an aggregate over all agents, which a member in an isolated
workspace must not see. The V2 page is `/v2/costs` (`?view=costs|alerts`).

## Workspaces — multi-account/multi-region environments

Every environment the console manages is a **workspace**: one
`(account_id, region)` pair (UNIQUE-constrained) carrying its own AgentCore
resource map on a ledger row (`workspaces` table). The hub's original
environment survives as the reserved `default` workspace, whose row mirrors
`config/launchpad.yaml` on every startup; all other workspaces are
row-authoritative and are provisioned by a console-driven, resumable
**bootstrap job** (`POST /api/workspaces/{id}/bootstrap`, ten idempotent
stages: validate-access → iam → storage → codebuild → cognito → gateway →
memory → registry → observability → finalize). `validate-access` refuses a
region that already hosts a foreign Launchpad deployment, and IAM roles are
adopted only when they carry the `launchpad:workspace` tag. A workspace in
**another account** carries a `role_arn` + `external_id`: the hub assumes that
role (auto-refreshing one-hour sessions, cached per `(account, region, role)`)
and every call for the workspace — bootstrap, CodeBuild build, invoke,
CloudWatch read — signs with it. The spoke role ships as plain CloudFormation
(`infra/spoke/launchpad-workspace-role.yaml`); see
[cross-account-workspaces.md](cross-account-workspaces.md) for the setup flow
and the trust-boundary trade-off. `POST /api/workspaces/preflight` (the
registration form's TEST ACCESS button) probes that pair with one AssumeRole +
`GetCallerIdentity` before anything is recorded — a refusal comes back as
`ok: false` with the same diagnostic a bootstrap stage would print, so a wrong
ExternalId is caught in a second instead of by a failed provisioning run.

**Request boundary.** Console requests name their workspace with an
`X-Workspace` header (a frontend `window.fetch` wrapper stamps it globally;
admins fall back to `default`, members to their single grant). Resolution
runs inside the app-level route-policy dependency — grant check (members need
a `user_workspaces` row; admins bypass), readiness gate for mutating methods,
and `request.state.workspace` for handlers. Routes are workspace-scoped by
default; only the hub-global prefixes (`/api/auth`, `/api/users`,
`/api/workspaces`) are exempt, and drift tests enforce the classification in
both directions. All per-environment ledger tables carry a `workspace_id`
column; queries filter by it, so a foreign resource id answers 404. The
public `/v1` surface ignores the header entirely: an API key authorizes
exactly its home workspace.

**Background work** (deploy stages, eval runs, experiments, canaries, policy
reconciliation) rehydrates its `WorkspaceContext` from the persisted row it
belongs to, never from ambient settings, and startup refuses to boot if any
scoped row is missing its `workspace_id`. Users and console auth stay
hub-global. Grants are edited from either side: per account on the Users page
(approval assigns them, `PATCH /api/users/{id}` replacing that account's whole
list) or per workspace on the Workspaces detail view, whose members table pages,
searches and filters server-side (`GET /api/workspaces/{id}/grants`) and grants
or revokes a selection in one call (`PUT` on the same path). Both write only
`user_workspaces`; administrators are never rows there, because they reach every
workspace by role.

**Member resource visibility.** Each workspace is `shared` (the default — every
member sees every resource of the workspace, the behavior before the setting
existed) or `isolated`, stored as `Workspace.settings["resource_visibility"]` and
switched by an admin (`PUT /api/workspaces/{id}/settings/visibility`, the
Visibility card on the Workspaces detail view). Under `isolated` a member sees a
resource iff its `created_by` (see *Resource creators*) is the member's own
`users.id`, is NULL (creator unknown: legacy rows, samples, the config admin,
resources made outside the console) or belongs to a **current** admin — admins act
as publishers, so a demoted admin's resources turn private and a deleted creator's
dangling id is visible to admins only. Admins always see everything. The kinds are
knowledge bases, managed memories, eval datasets (cloud datasets follow their linked
local rows; an unlinked one is shared), pipelines, eval runs, online eval configs
(an `exp_<id8>_…` / `can_<id8>_…` config follows its experiment / canary), custom
evaluators (built-in and third-party always visible), experiments, runtime canaries
and Skill Lab jobs and task sets; the bootstrap memory is always visible. One module,
`app/services/visibility.py`, owns the rule: a `Viewer` (`filters` is true only for a
member in an isolated workspace) with a row predicate and its SQL clause, used by
every per-kind choke point (`_dataset_in`, `_run_in`, `online.get_config`,
`jobs.get_job`, …) through a keyword-only `viewer` defaulting to `Viewer.ALL`, so
workers, the materializer, purge, the pipeline thread and the deploy path stay
unfiltered and a visible resource that references a hidden one keeps working. Hidden
means hidden everywhere — lists (filtered in SQL before any count / offset / limit),
pickers (which read the same lists), by-id routes and create-time references all
answer the kind's ordinary not-found, so existence is not disclosed. The overview
pass rate / run count and the online-quality tile count only what the viewer sees
(the tile is cached per viewer), and the architect catalog lists the KBs its
conversation **owner** can see. The setting is re-read with the workspace row on
every request, so a switch applies to the next request and changes no data.

*Agents* follow the same rule on `Agent.created_by` (stamped on `POST /api/agents`,
on harness → runtime conversion — the converter owns the twin — and on architect
approval — the approver; discovered runtimes and system presets record nothing and
stay shared). `app/services/agent_access.py` is the one agent choke point:
`agent_in(..., viewer=)` behind every by-id helper (agents router, Chat, eval run /
online / log-streams, experiment readiness / create, canary create, registry A2A demo
and live card), each keeping its own miss answer (`agent.not_found` 404,
`agent.not_active` / `canary.agent_not_active` 400, `registry.*`); `agent_clause` for
the lists (`GET /api/agents`, which feeds every picker, conversion twins, `/v1/agents`);
and a request-scoped `HiddenAgents` snapshot for surfaces that hold an agent only by a
derived key. Those drop a hidden agent's observability traces / sessions (resolved
through the span `service.name` resolver or the ledger's chat / eval-run session ids;
details, transcripts and on-demand scoring answer `observability.*_not_found`), its
memory actors `<agent>__<human>` (`memory.actor_not_found` on events / records /
namespaces / search / extraction jobs, and the shared `default` actor loses the
sessions the ledger attributes to it), its A2A registry records (`aws.not_found`),
its deploy jobs (`job.not_found`), and its name from KB / memory used-by lists,
Connections references, online `matched_agent`, discovery `managed_agent_*` and
import `already_managed` — counts stay truthful, only names go. The overview's
active sessions and A2A count are per viewer; the dashboard charts stay workspace
totals. Name uniqueness stays workspace-wide; the `agent.name_exists` 409 carries the
holder's id only when the caller may see it. A `/v1` key follows its **creator's**
current visibility (`visibility.viewer_for_key`): a member's key reaches only what
that member sees, keys by admins / the config admin / legacy keys are unrestricted.
On `/api/apikeys` a member of an isolated workspace lists and toggles only the keys
they minted (stricter than the resource rule: a key is a credential). An agent spec
may pin only KBs / memories the caller can see (`agent_access.check_pins`, before any
row or AWS call); a re-publish checks only the pins it *adds*, so an agent pinned
while shared keeps re-publishing. A visible eval run, experiment or canary does not
make its agent the caller's: recommendation accept and every experiment / canary
action except `verdict` / `cleanup` require the agent to be visible. KB force-delete
still strips the KB from every mounting agent (integrity). Deploy pipeline, resume,
workers, purge, the observability resolver, name claims and the preset service keep
`Viewer.ALL`.

**Removal.** `DELETE /api/workspaces/{id}` is a detach — the row and its grants
go, AWS is untouched — and it is refused while any scoped row still names the
workspace. That guard also traps a *failed* registration: the one job row its
bootstrap left behind blocks the detach and keeps the `(account, region)` slot,
so the environment cannot be re-registered. `POST /api/workspaces/{id}/purge`
(admin; `?dry_run=true` previews the row counts) deletes the scoped rows, the
grants and the row in one transaction, and is admissible only for a workspace
that never became usable: `registered` or `failed`, agent-free, never `default`.
Whatever a failed run had already provisioned stays in the target account — the
response's `resource_keys` says which resource kinds that is.

### Region support

A workspace is one `(account, region)` pair and every AWS client is built from it, so
nothing in the control plane assumes `us-west-2`. The region-dependent *names* live in
one module, `backend/app/core/regions.py` (mirrored for the console in
`frontend/src/lib/regions.ts`):

- **Inference-profile prefix.** `inference_profile_prefix(region)`: `us-*` to `us`,
  `eu-*` to `eu`, `ap-*` to `apac`, `us-gov-*` to `us-gov`; any other region returns
  `None` and the id is left alone. `global.*` and bare ids work everywhere and are never
  rewritten. `localize_model_id` re-prefixes only `anthropic.`/`amazon.` geographic ids
  (never `openai.*`, so no id is invented). The console hides a geographic id (for
  example `us.openai.gpt-5.6-sol`) from workspaces outside its geography; a custom
  model id can always be typed.
- **Partition.** `partition_for_region` / `WorkspaceContext.partition` feed the ARNs in
  per-agent role policies, the harness role ARN, memory and ECR ARNs; the CDK stack uses
  `self.partition`. The workspace-role builders in `services/workspace_iam.py` and some
  assistant/evaluation ARNs still use the literal `aws` partition (AgentCore is only
  used from commercial-partition regions today).
- **Generated agents** read `AWS_REGION` (then `AWS_DEFAULT_REGION`) at runtime; the
  only pinned region is Mantle's `LAUNCHPAD_MANTLE_REGION` (default `us-east-1`), which
  is overridable because Mantle models are hosted independently of the runtime region.
- **Registration form.** `GET /api/workspaces` returns `suggested_regions`
  (`AGENTCORE_SUGGESTED_REGIONS`). It is a suggestion list only: keep it in step with
  AWS's published AgentCore regions. The form accepts any typed region and the bootstrap
  job's `validate-access` stage is the real availability check.
- **Deliberate defaults.** `core/config.py` `region`, `infra/app.py` (`CDK_DEFAULT_REGION`),
  and `LEGACY_REGION = "us-west-2"` (keeps pre-multi-region role names bare) are defaults
  or naming compatibility, not bindings of a running workspace.

### Scenario templates, role-based navigation, PII protection (roadmap T10–T12)

**Scenario templates (T10).** `GET /api/agent-templates` (MEMBER, hub-global — the
catalogue is static data with nothing to scope) serves
`app/services/agent_templates.py`: a template is a named set of *wizard defaults*
(method, system prompt, whether the scenario expects documents, demo toolkit, memory,
the PII preset, sample questions), never a resource and never a new creation method.
The V2 wizard's first step shows them as a gallery (`v2/pages/agents/TemplateGallery.tsx`);
picking one fills the form and jumps to configure, where every field stays editable, so
the wizard still posts an ordinary `AgentSpecInput` a member can see. Copy lives in
i18n (`label_key` / `description_key`), not in the payload.
`tests/test_agent_templates.py` builds an `AgentSpec` from every template, so a
catalogue entry that could not deploy fails the suite rather than the member's first
attempt.

**Role-based navigation (T11).** The V2 sidebar has two modes (`lib/nav-mode.ts`,
stored per browser like `ui-version`): `business` — the build → run essentials plus
Learn, the default for a member — and `expert`, the full table and the default for an
administrator. `navGroupsFor(mode, isAdmin)` in `v2/nav.ts` does the filtering and a
quiet switch at the foot of the rail flips it. It is a **view filter, not
authorization**: every route stays reachable by URL and `route_policy` is untouched.

**PII protection (T12).** `AgentSpec.guardrail` (`{enabled, mode}`, off by default)
opts an agent into screening by the **platform**, not by the agent: a managed Harness
runs its model call inside AgentCore, so Launchpad cannot pass a `guardrailConfig`
down — what it does own is the prompt it forwards and the answer it returns.
`app/services/guardrail.py` keeps one Launchpad-owned Bedrock guardrail per workspace
(`launchpad-pii`, created on first use, adopted by name if it already exists, ids
remembered in the workspace `resources` map) covering contact and credential entities
only — masking NAME or AGE would break ordinary HR questions. `invoke_agent_text`
screens the prompt before dispatch (so a `block` refusal costs no model call) and the
answer on the way out; `mode: "anonymize"` substitutes the masked text, `"block"`
raises `guardrail.blocked` (422). Screening **fails open** on an AWS error: an
unreachable guardrail must not take a working agent offline.

In `chat_stream` an enabled agent deliberately switches to **buffered** mode — an
entity can straddle two deltas, so deltas are collected, the whole answer is screened
once, and the masked text is emitted as one delta while tool and heartbeat events keep
flowing. Losing token-by-token streaming is the stated price of not leaking PII, and
the wizard's card says so. `GET /api/governance/guardrail` (MEMBER) reports whether the
preset exists; `POST` (ADMIN) provisions it, since that creates a real Bedrock resource.

### Fleet overview, governance health, shared templates (roadmap T37–T39)

**Fleet (T37).** `GET /api/fleet` (ADMIN, hub-global — the one read that deliberately spans
environments) puts every workspace in one table: agents active/deploying/failed, failed
jobs, pending releases, firing alerts. It is a **ledger read on purpose**: fanning Logs
Insights across every account would be slow, billed per scan, and would blank the whole page
when one spoke's role lapsed. The per-workspace telemetry stays one click away on that
workspace's own pages. A workspace whose bootstrap is `registered`, `bootstrapping` or
`failed` reports `readable: false` with **null** counts rather than zeros — a table whose
quiet rows might mean "healthy" or might mean "unreachable" is worse than no table.

**Governance health (T39).** `GET /api/governance/health` (MEMBER) computes findings for the
selected workspace from ledger state — active agents without PII protection, agents never
evaluated, firing alert rules, a workspace with no rules at all, and deployments stale past
90 days — each with the console link that fixes it, then deducts a weighted score. The score
is blunt by design: every point it removes is attributable to a listed finding, because a
number nobody can act on is not governance. System presets are excluded (they are
server-owned, so holding them against the workspace would be noise).

**Shared templates (T38).** T10's scenario templates are platform-authored; `shared_templates`
is the other half — a team publishes an agent that actually worked so another team can start
from it. The table is **not** workspace-scoped, which is the point: scoping it would mean no
other workspace could ever see it. `workspace_id` records provenance and decides who may
withdraw it (the publisher, or an administrator).

What an entry carries is pruned deliberately (`marketplace.publishable_spec`): the agent's
*shape* (method, prompt, model, toolkit/native-tool selection, memory and guardrail posture,
loop bounds) travels; environment-specific ids (knowledge bases, gateway targets, skills,
memory resources) and anything secret-shaped (`env`, BYOC upload ids and image URIs) do not.
The allow-list alone is not sufficient — `memory` is kept for its shape but nests
`memory_id`, a resource id from the publishing account, so nested ids are pruned explicitly.
What was stripped becomes **requirements**: readable labels ("HR policies", "hr-database")
telling a consumer what to supply, rather than opaque identifiers from someone else's
account. `POST .../use` returns those defaults and counts the use; it deliberately does not
create an agent — the consumer still goes through the wizard and supplies their own
resources.

### Spend attribution and threshold alerts (roadmap T28/T29)

**Spend (T28).** The Observability console already estimated cost per model and per
session from the `model_prices` map; what it could not answer is *who spent it*.
`app/services/costs.py` adds two dimensions over data the platform already has: **by
agent**, from one Logs Insights query grouped by the runtime service name on each span,
and **by person**, from per-session tokens joined to `ChatSession.actor_id`. Sessions the
console never opened (direct `/v1` traffic, evaluation replays) land in an explicit
unattributed bucket rather than being dropped — a total that looks like zero is worse than
one that says "unattributed" — and a service name matching no ledger agent is still
reported, because an imported or deleted agent spent real money. Every figure is labelled
an estimate: a model missing from the price map contributes tokens but no dollars and is
named in `unpriced_models`, so the number is never quietly short. `GET /api/costs` and
`GET /api/costs/month-to-date` are **ADMIN** for the same reason the TTFA view is — the
per-person breakdown names who spent what.

**Alerts (T29).** `alert_rules` (workspace-scoped) is a threshold on a value the platform
can already compute: `error_rate` and `latency_p95_ms` from the dashboard aggregates,
`online_quality` from the online-evaluation mean, `cost_mtd_usd` from the price map. No
new telemetry path. Four properties are deliberate:

- **A direction that could never fire is refused.** Quality is bad when it *drops*, an
  error rate when it *rises*, so `alert.comparison_never_fires` rejects the inverse
  instead of accepting a rule that would sit silent forever. The create route defaults the
  comparison, so the common case needs no thought.
- **An unreadable value is `unknown`, never `ok`.** No traffic in the window, or a failed
  query, records `unknown` with the reason — an alert that reports health it did not
  measure is worse than no alert.
- **A transition notifies, not a state.** `state` lives on the row, so a rule that stays
  firing does not re-notify every pass. A failed webhook is recorded in `last_detail` and
  never hides the breach.
- **Delivery is one generic https JSON webhook** — a Slack or Feishu incoming hook already
  is one, so a single mechanism covers both and the platform stores no channel
  credentials. Plain `http` is refused: the alert text names the workspace and the breach.

Evaluation is explicit (`POST /api/alerts/evaluate`, ADMIN) because each read is billed or
cached; the console calls it with `notify=false` so opening the page can never page
anyone. Firing rules also surface in the administrator inbox, read off the rows rather
than re-querying, and the V2 page is `/v2/costs` with a `?view=alerts` tab.

### The dev → ops hand-off: operator role, release bundles, promotions, inbox (roadmap T19–T22)

`prod` refuses member agent mutations (T05), so the only honest way into production is a
*promotion*. These four tasks build that path up to an approved release; executing one
into the target environment is P3 (T23–T27).

**The operator role (T19).** `User.role` gains `operator` next to `admin` and `member`,
and permissions now have **per-role defaults** (`users_service.DEFAULT_BY_ROLE`) instead
of "every key granted unless denied". A member holds the build keys plus
`promotion.request`; an operator holds `promotion.approve` and `eval.run` but **not**
`agents.deploy` / `agents.convert` — they release what others built rather than editing
prompts or code; an administrator holds everything by role. Two consequences worth
knowing: `_normalized_permissions` now persists an explicit **grant** as well as a denial
(storing only denials silently discarded the one way to give a single member the approval
right), and it measures overrides against the role *after* a same-request role change.

**Release bundles (T20).** `release_bundles` freezes one publish — the spec from its
`SpecSnapshot`, the artifact coordinates that publish actually used (image digest, staged
upload, AWS version), the evaluation evidence pinned to it, the policy posture — and
reduces them to a `digest` (sha256 over the canonical *name, method, spec, artifact*).
The digest is the point: promotion ships the bundle, so "what goes live is what was
tested" becomes checkable rather than asserted. Evidence blobs are deliberately **not**
digested — re-running evaluation must not mint a new identity for identical code.
Bundling is idempotent per digest, so pressing it twice does not fork the audit trail,
and an agent published before snapshots existed still bundles from its live spec. Bundling
is allowed in `prod` (one ledger row, no AWS call): refusing it would make a prod agent
the one thing that can never be re-released from its own environment.

**Promotions (T21).** `promotions` is one request to release a bundle into a target
workspace, carrying a required change note and rollback plan. `POST /api/promotions`
(`perm:promotion.request`), `POST /api/promotions/{id}/review`
(`perm:promotion.approve`). Rules: a second person must approve —
`promotion.self_approval` refuses the requester, administrators included, because the
hand-off *is* the control; a reviewed request cannot be reviewed again; one bundle may
have only one open request per target. The detail view diffs the bundle's spec against
whatever agent of that name runs in the **target** today — the question an approver
actually has — and records the gate results *at approval time* as evidence rather than a
live read. Gates (evaluation pinned, artifact reusable, PII posture, target ready, target
agent) are advisory here and become blocking with T26. Every decision is journaled in
`audit_events`.

**The inbox (T22).** `GET /api/inbox` (ADMIN) aggregates everything waiting on a human:
pending registrations, accounts expiring within a week, workspaces not `ready` (including
`registered` — a registration nobody finished cannot be used and holds its
`(account, region)` slot), failed jobs in the selected workspace, promotions awaiting
review, and active agents never evaluated. It mixes hub-global and workspace-scoped
reads on purpose, because that is how an administrator works: one environment in focus,
the tenancy always visible. Every item carries a `to` the console renders as a link —
an inbox whose rows cannot be acted on is a wall of numbers. It surfaces as a card at the
top of the V2 workbench, rendered only for an administrator and only when non-empty.

**The target is a grant boundary too.** The route policy authorizes only the request's
own workspace (`X-Workspace`), but a promotion names a *second* one. Every route that
reads or acts on the target — request, detail, review, plan, execution log, execute,
rollback, the mapping resolution preview, the release policy read — therefore also runs
`workspaces.authorized_workspace(db, identity, target_id)`: 404 when it does not exist,
403 `workspace.forbidden` when the caller holds no grant (admins reach every workspace by
role). Authorization runs before any state is revealed. The detail view returns the
request to anyone in the source workspace but withholds the target agent and the diff
(`target_visible: false`) from a caller not granted on the target, and the diff never
carries the *values* of `env`, `code`, `code_bundle` or `byoc` — only that they changed
(`redacted: true`). This closes a security-review finding: previously a member granted
one dev workspace could name any target and read the same-name agent's env, prompt and
code back through the diff, and an operator granted only dev could approve and execute a
release into prod. Any new route that takes a second workspace id must call
`authorized_workspace` for it.

One trap this exposed, worth remembering: `require_identity` takes an optional `settings`
argument for tests, which FastAPI reads as a **second body parameter** and silently
embeds a route's body under a wrapper key. A handler that wants an identity alongside a
request body must depend on `auth.current_identity` instead.

### Logical resource mapping, artifact copy, prod spoke template (roadmap T23–T25)

Three pieces that let a promotion carry an agent into another account without carrying
dev's identifiers or rebuilding its artifact.

**Logical resource mapping (T23).** `resource_mappings` (workspace-scoped, unique on
`workspace_id, kind, name`) maps a logical name — `kb:hr-policy` — to the real id in *that*
workspace. Kinds: `kb`, `memory`, `gateway`, `mcp_record`, `skill` (an `s3://` prefix).
`services/resource_mapping.py` finds the environment-specific ids in a spec
(`knowledge_bases[].kb_id`, `memory.memory_id`, gateway `config.gateway_id`/`record_id`,
`s3://` skills), names each one — a mapping in the *source* workspace that already owns the
id, else a name derived from the spec, else the id — and `resolve_spec` returns a rewritten
**copy** plus `resolved` and `unmapped` lists (path, logical name, dev id, reason), never
mutating the bundle. A workspace's own shared gateway/memory resolve to the target's own
without a hand-written row. `env` values and code inside a BYOC artifact are not scanned;
the platform cannot tell an id from any other string there. `evaluate_gates` gains a
`resource_mapping` check carrying the same lists, so the approver sees them in the
existing `gates` payload. Routes act on the *selected* workspace: `GET
/api/resource-mappings` (MEMBER), `PUT|DELETE /api/resource-mappings/{kind}/{name}`
(`perm:promotion.approve` — deciding which prod resource a release binds to is the
approver's call, not the builder's; journaled in `audit_events`), and `GET
/api/release-bundles/{id}/resolution?target_workspace_id=` (MEMBER, read-only preview).
The V2 workspace detail page has an editable table.

**Artifact copy (T24).** `services/artifact_copy.copy_artifact(bundle, source, target)`
deploys the *tested* artifact. Containers copy by digest through the ECR API: the manifest
is read with `BatchGetImage` and hashed against the bundle digest, missing layers stream
through the hub (`GetDownloadUrlForLayer` to `InitiateLayerUpload`/`UploadLayerPart`/
`CompleteLayerUpload`, each layer hashed before completion), then `PutImage` with the
expected `imageDigest`, and the target is asked for the digest again. Blob *mounting* is a
registry-v2 feature with no ECR-API form and would need a policy on the dev repository for
every prod account; ECR replication is registry-wide, push-triggered and asynchronous, so a
promotion could not wait on one digest. Multi-arch indexes copy their children first. Zip/
BYOC archives stream from the source artifacts bucket to the target's under the same
`upload_id` (the workspace segment of the key changes, so the deployer finds it without a
spec rewrite) and are checked against the SHA-256 recorded at upload. Both paths are
idempotent (digest present, or object present with the same recorded sha256, means
`already_present`; a partial run resumes at the missing layers), refuse on any mismatch with
`promotion.artifact_digest_mismatch`, and build every client through `WorkspaceContext.client`.
The result carries `target_artifact` (the target's `image_uri`/`upload_id`) for the executor
to overlay on the bundle.

**Prod spoke template (T25).** `infra/spoke/launchpad-workspace-role-prod.yaml` is the
standard role minus the build path: no CodeBuild, no PassRole to CodeBuild, ECR
receive-only on `launchpad-agents`, no `s3:DeleteObject`. Each removal is documented in the
template header and pinned by `infra/tests/test_spoke_template_prod.py`; the standard
template gained a single `EcrArtifactCopy` statement so a dev account can be a copy source.

### Release gates and promotion execution (roadmap T26–T27)

An approved promotion is now executable. `POST /api/promotions/{id}/execute` and `.../rollback`
(`perm:promotion.approve`; operator/admin only, so a member never reaches the prod guard, and
explicitly recorded in `PROD_UNPROTECTED_AGENT_ROUTES` because they act on the promotion's
*target*, not the request's workspace) admit a **background job**
(`jobs.type = promotion_execute`, `workspace_id` = the *target*, JSONL log) that
`resume_pending_jobs` re-enters after a restart. `services/promotion_exec.py` owns it;
`services/release_gates.py` owns the gates and the plan.

**Blocking gates (T26).** `evaluate_gates` stays the approver's DB-only, advisory view — an
approver may accept a flagged bundle and the record says so. Execution re-evaluates
everything live (`release_gates.execution_gates`) and refuses with `409
promotion.gate_failed.<gate>` (details list every failing gate) unless all blocking gates
pass. Every gate blocks except `guardrail` and `target_agent`, so a gate added later
(`resource_mapping` was) blocks by default; `artifact` is non-blocking for spec-built methods
(harness, zip, studio) and blocking for container/BYOC. New gates: `eval_score` (the run
pinned on the bundle is still completed and its mean score meets the threshold; the detail
names the dataset and its version), `policy_enforce` (the target gateway's
`policyEngineConfiguration.mode` is `ENFORCE` — one `GetGateway`; an unreadable gateway
fails, it does not pass) and `deploy_window`. Policy lives on the **target** workspace
(`workspaces.release_policy`, edited by an administrator through `PUT
/api/release-policies/{workspace_id}`, journaled): `min_eval_score` (default 0.7), a weekly
`window` (days, `HH:MM` start/end, IANA timezone), dated `freezes`, `require_policy_enforce`,
`observe_seconds` and `smoke_prompts`. Unset keys fall back to the tier: `prod` requires
ENFORCE and a 300 s observation window. A refusal by the window carries `next_allowed_at`,
computed exactly from the window edges and freeze ends. A rollback is deliberately not gated:
it is the way out of a bad release.

**Plan preview.** `GET /api/promotions/{id}/plan` (read-only; the one live read is the Policy
Engine mode) lists what execution would do in the target — agent create vs replace, IAM role
(per-agent for BYOC, else the shared role), artifact copy, registry record, canary vs skip,
mapping rewrites — plus the gates and `can_execute`.

**Stages.** `resolve → copy → provision → deploy → smoke → canary → observe → complete`,
persisted on `promotions.stages` (status, detail, timestamps) with resumable scratch on
`promotions.execution`; the job log carries the same events.

| Stage | What it does |
|---|---|
| resolve | `resource_mapping.resolve_spec` rewrites the frozen spec for the target; unmapped references, a method clash or a system preset stop the run |
| copy | `artifact_copy.copy_artifact` (T24); skipped, saying why, when the bundle has no artifact |
| provision | creates or re-stages the target agent row and queues an ordinary `deploy_agent` job (its own `provision` stage makes the IAM role) — or, on the canary path, publishes nothing |
| deploy | runs/awaits that job (create, or an in-place update that keeps the prior spec in history). On the canary path it instead mints the candidate version and gateway, because the champion must survive to be compared |
| smoke | three fixed prompts (policy-overridable) through the shared invoke chain; any empty answer or error fails the run |
| canary | drives the existing runtime canary in-line (`optimization.canary_service.act_*`) through 90/10 → 50/50 → 1/99, one traffic round and verdict each; a blocking verdict fails the release, rolls the canary back **and then cleans it up** — rolling back alone left the experiment gateway, A/B test, online-evaluation config and candidate endpoint behind, which leaked billable resources and made the agent undeletable (`DeleteAgentRuntime` refuses while an endpoint exists). Cleanup never runs if the rollback itself failed, since that canary may still be routing traffic. The verdict is the judge's, not the script's: in the cross-region e2e a candidate that scored lower than the running version was blocked at 90/10, which is the property the canary exists for. Skipped with the reason logged when the target has no running agent or cannot host a canary (only `zip_runtime`/`studio` runtimes can) |
| observe | holds at full traffic for `observe_seconds` (deadline persisted, so a restart does not restart the clock), then probes once more |
| complete | promotes the candidate and cleans the canary up |

Replay traffic uses the bundle's pinned evaluation dataset when it resolves to prompts,
else the smoke prompts. Status is honest: `executing` → `succeeded` | `failed` with
`error = "<stage>: <reason>"` and the failed stage in `stages`; a failed stage aborts a live
canary so the candidate never keeps serving. A failed run may be executed again.

**Rollback.** `previous_bundle_id` is recorded at execute time (the target's last
`succeeded` promotion of the same agent). `rollback` runs the same stages on that bundle —
canary and observe skipped — as a **new publish** through the ordinary deploy path, never an
AWS-side version revert. No previous bundle answers `409 promotion.no_previous_bundle`. It
ends `rolled_back`, or `failed` with `rollback failed at <stage>` (and may be retried).

Limits worth knowing: the `container` method's pipeline always rebuilds its image from the
spec, so its copy stage moves the artifact but the deploy still builds (logged); the canary
verdict needs real evaluator samples, so a canary-eligible release takes as long as those
take to arrive.

### Workspace tier and prod protection (roadmap T05)

Every workspace carries a `tier` — `dev` (default, and what existing rows migrate
to), `staging` or `prod` — set at registration and changed through
`PATCH /api/workspaces/{id}` (admin). The column is ledger-authoritative: the
startup mirror of `default` never writes it. A move **into or out of** `prod`
changes who may modify agents there, so it needs `confirm_tier_change: true`;
without it the patch answers 409 `workspace.tier_change_unconfirmed`. Every actual
tier change is journaled in `audit_events`.

`prod` is the only tier with behavior today. `route_policy.PROD_PROTECTED` lists
the agent-mutating routes (create/deploy, redeploy, delete, convert, discovery
import, BYOC uploads, deploy-flow skill import, the architect assistant's skill
preparation and proposal approval, the system-preset install/uninstall routes, and
the optimization flows that change what a live agent serves: creating and driving a
runtime canary, and an experiment's actions, whose promote redeploys the agent). On
a `prod` workspace a member calling one of them gets 403
`workspace.prod_protected` — changes must arrive through promotion — while an
administrator passes as break-glass and the call is journaled in `audit_events`
at admission (so a row records the attempt; the Job/Deployment row carries the
outcome). Reads, chat/invoke, evaluation and observability stay open. The guard
runs in `enforce_route_policy` right after the workspace resolves, so no handler
can forget it; `tests/test_prod_protection.py` pins every `perm:agents.*` route to
either `PROD_PROTECTED` or `PROD_UNPROTECTED_AGENT_ROUTES` (with a reason), and every
non-read route under `/api/agents`, `/api/system-agents`, `/api/runtime-canaries` and
`/api/experiments` to `PROD_PROTECTED` or a documented exemption, so a new lifecycle
route cannot ship without a prod decision.

`audit_events` is workspace-scoped (`WORKSPACE_SCOPED_TABLES`), so a workspace with
journal rows cannot be detached — only purged. In the console the tier shows as a
tag in the V2 top-bar workspace switcher (prod in the danger tone), is selectable
on registration and editable on the workspace detail view, and `useProdLock()`
disables the V2 agent create/edit/convert/delete/import controls for members on a
prod workspace with the reason as the tooltip — the backend stays the boundary.

### Scoped API keys, integration snippets, spec snapshots (roadmap T16–T18)

**Scoped keys (T16).** `api_keys` gained `agent_ids` (JSON; NULL or empty means every
agent in the workspace, exactly the pre-T16 behaviour), `expires_at`,
`rate_per_minute`, `last_used_at`, `use_count` and `created_by`, all through the
additive `_migrate_api_key_scope`. `require_api_key` now also answers an expired key
`401 auth.expired_api_key` and an over-limit call `429 auth.rate_limited` with a
`Retry-After` header (`AppError` gained an optional `headers`). An agent outside the
key's `agent_ids` reads exactly like a missing one (`404 agent.not_found`, also
absent from `GET /v1/agents`) so scope cannot be probed. The rate limit is a
per-key sliding 60 s window held **in process** (`services/api_keys.py`): the
backend is one process, a rejected call must not cost a ledger write, and a restart
merely forgives one window. Usage is durable: `use_count`/`last_used_at` on the key
plus one `api_key_usage` row per (key, UTC day), bumped by an atomic SQL increment
and counting admitted calls only. Console routes: `POST /api/apikeys` accepts the new
fields, `PATCH /api/apikeys/{id}` edits or clears them (only fields present in the
body change), `GET /api/apikeys/{id}/usage` returns a dense per-day series. Scope ids
must be live agents of the workspace (`422 apikey.unknown_agent`); an expiry in the
past is refused (`422 apikey.expiry_in_past`).

**Integration snippets (T17).** `frontend/src/lib/snippets.ts` builds curl, Python
(httpx) and JavaScript (fetch) text for the sync `invoke` and the SSE `invoke-stream`
calls from the agent id and `window.location.origin`, with a `YOUR_API_KEY`
placeholder. It is client-side so no route needs classifying or keeping in step with
`public_api.py`; the V2 agent detail shows it as the Integration card for agents that
can be invoked.

**Spec snapshots and rollback (T18).** `spec_snapshots` (workspace-scoped) stores the
full spec per publish: `(agent_id, seq)` unique, `aws_version` (filled by `_finish`
once the deploy stage knows it), `deployment_id`, `created_by`, `note`. The row is
written inside `pipeline.create_deployment`, the one function every create,
redeploy, rollback, promotion and preset release passes through, in the same
transaction as the Deployment and Job rows. `GET /api/agents/{id}/snapshots`,
`.../snapshots/{seq}` and `.../snapshots/diff?from_seq=&to_seq=` read the ledger only;
the diff is server-side (`services/snapshots.diff_specs`): dotted field paths, a
group (`prompt`, `model`, `tools`, `skills`, `knowledge_bases`, `memory`,
`guardrail`, `other`), and `added`/`removed` members for lists.
`POST .../snapshots/{seq}/rollback` validates the stored spec against the current
`AgentSpec` (`409 snapshot.spec_invalid` if it no longer does) and hands it to the
shared `_republish` that the redeploy route also uses, so every redeploy guard
applies unchanged (system presets, discovered runtimes, in-flight deploy, immutable
name/method, converted agents' baked prompt and model). It is an ordinary "update"
deploy carrying an older spec, recorded as a new snapshot noted `rollback to #N`;
nothing is reverted on AWS. The route is `perm:agents.deploy` and listed in
`PROD_PROTECTED`, so members are refused on a prod workspace. In the V2 agent detail
the Snapshots card lists them, diffs any two picked rows, and rolls back behind a
confirm dialog.

### Share links, external pages and thumbs feedback (roadmap T13–T15)

**External page foundation (T13).** A `share_links` row is a signed, revocable,
account-free way for an outsider to reach exactly one thing: `kind` (`chat` today;
T30 channels and T34 SME review add kinds), `target_id` (the agent), `token_hash`,
`label`, `created_by`, `enabled`, `expires_at`, `revoked_at`, `last_used_at`,
`use_count`. Like an `ApiKey`, only the sha256 of the token is stored; the raw
`shr_…` value (256 bits from `secrets.token_urlsafe`) is returned once at creation.
The table is workspace-scoped (`WORKSPACE_SCOPED_TABLES`).

The public surface is `GET /share/{token}`, `POST /share/{token}/chat` (SSE) and
`POST /share/{token}/feedback`. It sits **outside `/api`**, so the console session
middleware never applies, and it is classified `PUBLIC` + hub-global in
`route_policy` (prefix `/share` in `HUB_GLOBAL_PREFIXES`, three entries in
`WORKSPACE_EXEMPT`), so `enforce_route_policy` never resolves a workspace and the
`X-Workspace` header is never read. `tests/test_route_policy.py` enumerates `/share`
routes with `/api` ones, so an unclassified public route fails the suite.
`services/share_links.resolve` maps token → row → workspace context server-side:

- **Every unusable state is one 404** (`share.not_found`, identical body): unknown,
  malformed, disabled, revoked, expired, agent deleted/inactive/not invocable, agent
  in a different workspace than the row, workspace gone. A revoked link cannot be
  told from one that never existed.
- **Rate limiting** is a per-link token bucket (12-message burst, then one per 5 s;
  `429 share.rate_limited` with `Retry-After`). It is in-process and per worker: the
  backend runs as a single process, so the ceiling is exact; with several workers it
  would multiply by the worker count and need a shared store.
- **Bookkeeping**: `use_count` / `last_used_at` advance per chat turn.
- **Outsiders see less**: tool names, runtime mode and raw exception text are
  filtered from the stream (the full error is still recorded in the ledger), and the
  share error handler uses the same generic AWS messages as `/v1`. System presets can
  never be shared (409 `share.agent_not_shareable`).
- **The invoke chain is not forked.** The route calls `chat_stream` and persists
  through `services/chat_ledger.persist_events`, the same code the console chat now
  uses. Each anonymous conversation gets its own Memory actor
  (`share_<link>_<session digest>` → `scoped_actor`), distinct from every console
  user and from other visitors of the same link, and a visitor can only continue or
  rate sessions their link started (another link's or a console session id reads as
  `share.session_not_found`).

**Share links in the console (T14).** `GET|POST /api/agents/{id}/share-links` and
`POST /api/share-links/{id}/revoke` are `MEMBER`, workspace-scoped, and deliberately
absent from `PROD_PROTECTED` — handing out a chat link is not an agent mutation, so a
prod workspace still allows it. Creation returns the token and URL once; the list
never contains it. The V2 chat page's *Share* button opens the manager (label,
expiry of 7/30/90 days or never, copy, revoke). The visitor page is `/s/<token>`: `main.tsx`
renders `share/SharePage.tsx` on its own, before `AuthGate` and outside the V2 shell,
so a visitor never sees a sign-in form or console navigation. It streams replies over
`/share`, shows the agent's display name and offers thumbs with an optional comment.
Access control is **link-level only** (label, expiry, revoke); a per-user or
user-group ACL on a link is out of scope. Deployment note: the reverse proxy must
route `/share/` to the backend beside `/api/` and `/v1/` (the Vite dev/preview proxy
already does), and `/s/` is served by the SPA fallback. The token travels in the URL
path, so it can appear in proxy access logs; treat those logs as sensitive or revoke.

**Thumbs feedback → bad cases (T15).** `chat_feedback` is its own workspace-scoped
table rather than columns on `chat_messages`: one answer can be rated by several
actors, a verdict changes or is withdrawn (unique on message + actor, upserted), and it
carries a comment and an origin (`console` or `share`), none of which belong on a
transcript row. `chat_stream` persists each agent answer and now announces it with a
`saved` SSE event (`message_id`); history replay returns message ids and the caller's
verdict. `POST /api/chat/{agent_id}/feedback` (and the share equivalent) validates that
the message is an agent answer of that agent, session and workspace. A thumbs-down lands
where evaluation already looks: `GET /api/feedback?verdict=down` returns the items plus
`down_session_ids` (distinct, newest first, capped at the 50 one `from-sessions` call
accepts). The V2 Insights page shows a *User feedback* card whose *Bad cases → dataset*
button feeds those ids to the existing `AddToDatasetModal`, i.e.
`POST /api/eval/datasets/from-sessions` — no dataset-building logic is duplicated.

### Channel publishing, GitOps export, environment comparison and drift (roadmap T30–T32)

**Web embed (T30).** `/s/<token>?embed=1` renders the share page without its header or
language switcher, edge-to-edge, for an `<iframe>`. It is a rendering mode only: the same
link, expiry, revocation, rate limit and visitor-visible stream apply, and `GET
/share/{token}?embed=1` echoes `embed` back. Creating a chat link now also returns
`embed_url` and a copy-ready `embed_snippet` (`<iframe src=… style="width:100%;height:600px;border:0">`,
attribute values HTML-escaped so a label cannot break out). The snippet contains the token,
so like the token it appears only in the creation response, never in a list. The console's
share manager shows it with a copy button. Nothing in the backend sends `X-Frame-Options`
or `frame-ancestors`, so the page is frameable; if a reverse proxy adds them, exempt `/s/`.

**IM inbound: Slack and Feishu (T30).** A channel is a `ShareLink` whose `kind` is the
platform (`slack` | `feishu`) — not a parallel mechanism: same token hashing, expiry,
revoke, `use_count`, rate limiter and opaque 404s. Two additive nullable columns carry the
adapter's settings: `channel_config` (non-secret, e.g. Feishu domain) and `channel_secrets`.
`POST /api/agents/{id}/channel-links` (MEMBER, not prod-protected, like share links) creates
one; listing and revoking reuse `/api/agents/{id}/share-links` and `/api/share-links/{id}/revoke`.
The platform calls one public webhook, `POST /share/channels/{platform}/{token}`, under the
existing hub-global `/share` prefix (so the reverse-proxy rule already covers it): the link
row names the workspace and agent, `X-Workspace` is never read. Order is the security
posture — resolve the token (kind must equal the platform, else the usual 404) →
authenticate the request → parse → de-duplicate → rate-limit → dispatch — so a forged
request cannot spend a link's budget. The reply is posted from a background task because
Slack and Feishu expect an acknowledgement in about 3 seconds; the turn goes through
`invoke_agent_text` with a per-conversation runtime session (`ch-` + sha256 of link and
Slack channel/thread or Feishu chat/root message; DMs are one running conversation) and a
per-session memory actor, and is recorded as a `ChatSession` with its messages. A failed
turn replies with a generic sentence, never exception text.

*Verification schemes.* **Slack**: the app's signing secret — `X-Slack-Signature` is
HMAC-SHA256 over `v0:<timestamp>:<raw body>`, compared in constant time, with a 5-minute
timestamp window; it also signs the `url_verification` handshake, so the challenge is
answered only for a signed request. Replay inside the window is closed by event-id
de-duplication (in-process, bounded, per link — the same single-process assumption as the
rate limiter). The legacy verification token is not supported (it authenticates nothing
about the body). **Feishu**: the app's Verification Token (`header.token`, or `token` on
the handshake), compared in constant time against a SHA-256 stored at creation. Feishu's
request signature only exists together with payload encryption (Encrypt Key + AES), so it
is not an independent option; an `encrypt` payload is refused with an actionable error
(`channel.encrypted_unsupported`) instead of being guessed at. The token's weakness (a static
bearer in the body) is offset by the 256-bit link token in the URL path. *Secrets.* The
Slack signing secret, Slack bot token and Feishu app secret must be usable later (HMAC and
reply calls), so they are stored in `channel_secrets`; the Feishu verification token is
stored only as a hash. No route ever returns a secret: responses carry `channel.secrets_set`
(key names only). The ledger is treated as the platform's trust boundary (it also holds
workspace external IDs); envelope encryption with KMS is the open hardening step. Outbound
replies go only to `slack.com`, `open.feishu.cn` and `open.larksuite.com` (fixed in code, so
a stored value cannot become an SSRF target). Slack handles DMs and `app_mention`; bot
messages, edits and non-text Feishu messages are ignored.

*Microsoft Teams is out of scope.* Its inbound path is the Bot Framework: an Azure bot
registration, JWT validation of every activity against Microsoft's rotating JWKS, and
replies through a service URL that arrives in each activity. That is a tenant-level
registration plus token-cache machinery, not a per-link secret in a URL, and it does not
fit the stateless webhook model used here. It would be a fourth adapter behind the same
contract (`services/channels/base.py`) if demand appears.

**GitOps export and the `launchpad` CLI (T31).** `GET /api/release-bundles/{id}/export`
(MEMBER, read-only) returns the bundle as `application/yaml`: `apiVersion`, `kind:
ReleaseBundle`, `metadata` (agent name, `sha256:` digest), `spec` (method + agent spec),
`artifact` (the coordinates the digest covers) and `evidence` (a *reference* to the pinned
evaluation run: run id, dataset id/version — not the score blobs, which re-runs would
churn). Keys are sorted, the dumper is fixed, nothing folds or wraps, and the body has no
timestamp, row id or author, so a diff of the file is a diff of the release. Determinism is
guaranteed for one bundle, and across bundle rows sharing a digest and the same evidence
reference; evidence is deliberately outside the digest, so two rows of one release pinned to
different evaluation runs differ only in the `evidence` block. The CLI is
`backend/scripts/launchpad.py`, one stdlib-only file: a customer's pipeline needs `python3`
and that file, not a virtualenv or `uv`, and it sits with the other operator scripts. It
speaks the ordinary console HTTP API with a **session** (`LAUNCHPAD_USER`/`LAUNCHPAD_PASSWORD`
to log in, or `LAUNCHPAD_SESSION` for an existing cookie value; API keys authorize only
`/v1`). It pins the session cookie as a header because the cookie is `Secure` in prod mode
and a cookie jar would never return it over plain `http://`.

```
export LAUNCHPAD_URL=https://launchpad.example.com LAUNCHPAD_USER=ci LAUNCHPAD_PASSWORD=…
export LAUNCHPAD_WORKSPACE=dev          # the SOURCE workspace
python3 launchpad.py bundle  --agent hr-assistant -o releases/hr-assistant.release.yaml
python3 launchpad.py promote --agent hr-assistant --to prod \
    --change-note "Raise refund limit" --rollback-note "Redeploy bundle sha256:ab12…"
python3 launchpad.py compare --agent hr-assistant
python3 launchpad.py drift            # exit 2 on drift, 3 when only unknown answers came back
```

`bundle` freezes the latest publish (idempotent per digest) and prints/writes the export;
`promote` bundles and opens a promotion request — approval stays a second person's job.
Exit codes: 0 ok, 1 refused/unreachable, 2 drift, 3 unknown. An IaC (CloudFormation/CDK)
snippet export is not built.

**Environment comparison and drift (T32).** Both routes are MEMBER and read-only; they are
workspace-scoped (the selected workspace is the one drift is measured in and the one marked
`current`). `GET /api/environments/compare?agent=<name>` lists, for every workspace the
caller may see (administrators all, members their grants — the same filter as `GET
/api/workspaces`), the agent's status, version, last deploy, `spec_digest` and image digest,
ordered dev → staging → prod. The digest is `promotion.bundle_digest(name, method, spec,
artifact={})` — the bundle's own helper, with the artifact left out because its coordinates
(`source_arn`, AWS version) are environment-specific by nature and would make identical
specs look different everywhere. The highest tier that runs the agent is the reference;
other rows read `same`, `differs` or `absent`. Caveat: until logical resource mapping
normalises environment-bound ids (gateway, knowledge base), a spec that legitimately points
at different local resources reads as `differs`.
`GET /api/environments/drift[?agent=]` reads each active Runtime/Harness-backed agent back
through the `agentcore` wrappers (8 concurrent reads, 100-agent cap with a `truncated`
flag) and compares the ledger's expectation. `drift` findings: `resource_missing`,
`unhealthy` (status other than READY), `version_changed` (AWS version differs from the
ledger's — someone updated it outside Launchpad). **Fail soft, never fail green**: an
unreadable answer, an empty status, a resource mid-update, a missing recorded version, or a
version difference while a runtime canary is running (which mints a newer version on
purpose) is `unknown` with a `reason`; the workspace state is `drift` > `unknown` >
`in_sync`, so a single unreadable agent can never let the whole workspace read green. The
page is `/v2/environments` (nav entry in the Run group, matching video-taxonomy section),
with `?view=compare|drift`. It is its own page rather than a tab of Releases because
promotions are about *requests*, while this is the *current state* of an agent across
environments plus a workspace-wide check; drift runs only on demand because it spends AWS
calls.

### Business self-service: intents, SME review, curated answers, issue box (roadmap T33–T36)

The loop this phase closes for a non-technical owner is *find what the agent got wrong,
let an expert judge it, fix it without a redeploy, verify the fix*. Each step reuses an
existing primitive; the new code is the glue and the guard rails.

**Intent view (T33).** `GET /api/intents` (MEMBER, `?agent_id&days&lang&refresh`) groups
the last N days of ledger sessions by their first user turn. The existing
`Builtin.Insight.UserIntent` insight could not be pointed at this: it is an asynchronous,
billed AgentCore batch evaluation over CloudWatch spans (minutes of latency), not an
interactive table over ledger sessions. So `services/intents.py` makes **one Bedrock
Converse call** through the client funnel over at most 150 first turns and asks for at
most 12 business-language clusters plus the sessions that were not actually answered. The
result is cached in-process for ten minutes (sixty seconds when it is a fallback) keyed by
workspace, agent, window, language and a fingerprint of the sessions and their votes.
**Fallback:** if the call fails or returns nothing usable, identical questions (after
`answer_rules.normalize`) that recur become clusters, everything else one "other"
cluster, and `source` is `"fallback"` so the page says the grouping is mechanical. The
quality signal per cluster is the thumbs-down rate over *rated* sessions (`null`, not
0%, when nobody rated) plus the count of unanswered sessions; a per-cluster online-eval
score is deliberately left out because it is a billed Logs Insights read per session.
"Could not answer" is the union of the model's judgement and a deterministic check (no
reply, an error turn, or a refusal phrase in English or Chinese), so the fallback still
finds them. Each unanswered row can be sent to the issue box. The page is
`/v2/eval/intents`.

**SME review (T34).** A reviewer link is a `ShareLink` with `kind="review"`
(`services/review_links.py`, `routers/review.py`): the same hashed-at-rest token, the same
single 404 for every bad state (unknown, malformed, wrong kind, disabled, revoked, expired,
agent gone or cross-workspace; a review token cannot open a chat and vice versa), and the
row names the workspace. The public surface is `GET /share/review/{token}` (the queue: the
newest 25 answers of the agent, ones this reviewer has not rated first, each with its
question and whether it was curated) and `POST /share/review/{token}/rate`
(`{message_id, verdict, comment, correction}`), both `PUBLIC` + hub-global under the
existing `/share` prefix. The reviewer never holds a session id; the server derives it from
the message and refuses any message of another agent or workspace. Its rate limit is its
own bucket (60 burst, then one per second, per link): a reviewer works through a queue
faster than a visitor chats. Because a reviewer sees real user questions, an agent with the
PII guardrail on has the questions screened in `anonymize` mode before they leave the
server (fail-open like T12). Ratings go through `feedback.record_feedback` into
`chat_feedback` with `source="review"` and the new `correction` column, so the console
thumbs, the share page and reviewers feed one store. The page is `/r/<token>`, rendered by
`main.tsx` outside `AuthGate` and the V2 shell (`share/ReviewPage.tsx`); the reverse proxy
must serve `/r/` from the SPA like `/s/`. Links are managed on the Issue box page
(Reviewers tab) through `GET|POST /api/agents/{id}/review-links`; revoke reuses
`POST /api/share-links/{id}/revoke`.

**Curated answers (T35).** `answer_rules` holds an ordered list per agent
(`position`, `match`, `pattern`, `answer`, `enabled`, `hit_count`, `last_hit_at`,
`source_issue_id`); `answer_rule_sets` holds the per-agent master switch (no row = on).
*Matching* is deliberately deterministic: `exact` (the whole question equals the pattern)
or `contains` (whole words for Latin text, substring for CJK), both after Unicode NFKC,
case-folding, punctuation stripping and whitespace collapse. There is no fuzzy or semantic
match: a wrong canned answer to a similar-but-different question is worse than falling
through to the model, so a rule fires only on wording its author can read and predict. A
`contains` phrase under three characters (two for CJK) is refused. The first *enabled*
rule in position order wins; rules are skipped for turns with attachments. Enforcement is in
the invoke chain, in `invoke_agent_text` and `chat_stream` (the two places that dispatch):
a hit returns/streams the owner's text with no model call, `chat_stream` emits a `rule`
event before the delta, `persist_events` stores `chat_messages.answered_by = rule:<id>`,
and the `saved` event, history replay, the `/v1/.../invoke` body (`answered_by`) and the
Chat UI ("Curated answer" badge) all say so. **Order with the T12 PII screen:** the prompt
is screened first, rules match against the *screened* text, and the curated answer is
returned *without* output screening. A `block` agent therefore still refuses a PII-bearing
prompt even when a rule could answer it, an `anonymize` agent matches on the masked text,
and the owner-authored answer is not masked (it may intentionally contain a contact
address). A rule answer makes no runtime call, so it emits no runtime span; the visible
record is `answered_by`, the rule's hit counter and a log line. Rule writes are journaled in
`audit_events` and are deliberately not in `PROD_PROTECTED` (fixing production without a
redeploy is the point). `POST /api/agents/{id}/rules/test` dry-runs a question against the
rules without bumping counters, which is how a fix is verified. The page is the *Curated
answers* tab of `/v2/issues`.

**Issue box (T36).** `issues` (one row per answer, unique on agent and message) is opened
by a thumbs-down from any origin (a hook in `record_feedback`; `POST /api/issues/sync`
backfills earlier votes), by an unanswered question from the intent view, or by hand. The
owner opens the session transcript and picks a fix: a curated answer
(`POST /api/agents/{id}/rules` with `issue_id`, which links it), documents (the existing
Knowledge Bases page), or the evaluation dataset (the existing `AddToDatasetModal`, i.e.
`POST /api/eval/datasets/from-sessions`). **The issue box performs none of these**;
`POST /api/issues/{id}/fixes` only records that one happened, verifying a rule or dataset
reference against the workspace's ledger, and a test asserts the module builds no dataset
and imports no upload code. Creating a curated answer does not close the issue: marking it
fixed or won't-fix (`POST /api/issues/{id}/resolve`) is the owner's explicit step, and a
closed issue must be reopened before it changes again. Every transition is appended to
`history` with who and when, and `GET /api/issues` reports counts and the **median time to
close** (`created_at` to `resolved_at`) and the age of the oldest open issue; the median
because one issue left for a quarter should not hide that most close in a day.

Ledger: new workspace-scoped tables `answer_rules`, `answer_rule_sets`, `issues` (in
`WORKSPACE_SCOPED_TABLES`), and additive columns `chat_feedback.correction` and
`chat_messages.answered_by`. Known limits: a curated turn never reaches the runtime, so the
agent's own session memory does not contain it; a `rule` event is not forwarded to share-page
visitors; and rule matching runs on the whole prompt, so it is unsuited to multi-turn
context.

## Projects — the data and permission boundary inside a workspace

A workspace is one `(account, region)` environment; a **project** is the team
boundary inside it. Every agent and every row derived from an agent belongs to
exactly one project, members reach a project through a project role, and a project's
rows are invisible from every other project of the same workspace. Workspaces keep
their meaning (one per account and Region, used for DEV / PROD or for teams that need
account-level isolation); a project may be bound to several workspaces, so a
DEV → PROD promotion keeps its project. The design is
`docs/design/projects.zh-CN.md` (phase 1); the routes are in
[api.md](api.md#console-projects-api).

Phase 1 isolates **console and API data**, not runtime credentials: agents of every
project in a workspace still share the workspace's execution, gateway and knowledge
base roles, the M2M client of `launchpad-gw` / `launchpad-kb-gw` and the bootstrap
memory (partitioned only by actor). A team that needs runtime isolation today uses
its own workspace (phase 2 adds per-project IAM boundaries, memory and gateway
authorization).

### Model

| Entity | Table | What it is |
|---|---|---|
| Project | `projects` | The data and permission boundary. `id` is a slug (`^[a-z0-9][a-z0-9-]{1,31}$`), `status` `active` or `archived`, `settings` (`resource_visibility`, `sod_strict`). The reserved `default` project (`is_default`) is created by the migration |
| Project ↔ workspace | `project_workspaces` | An administrator allows a project to run in a workspace. One binding per workspace is `is_workspace_default`: the project a request without `X-Project` means |
| Member | `project_members` | A user's direct role in a project (`role_key`) plus per-key `overrides` |
| Custom role | `project_roles` | A per-project key set based on a built-in template |
| Project group | `project_groups`, `group_members` | An access bundle: its members get a built-in template in every project of the group. A project is in at most one group |
| Department | `departments`, `department_admins`, `department_workspaces` | An organizational label on projects and groups; grants no data access. Its administrators manage its projects and groups, in the workspaces opened to it (see *Department administrators*) |
| Access epoch | `access_epoch` | A one-row counter bumped by every access-affecting write (see *Caching*) |

All of these are platform tables (no `workspace_id`). A user's effective keys in a
project are the union of the direct role's and the group role's keys, then the direct
row's `overrides` (`false` removes, `true` adds). Built-in templates are code
constants in `app/services/access.py` (`ROLE_TEMPLATES`: `project_owner`, `developer`,
`biz_owner`, `release_ops`, `sme`, `tool_steward`, `viewer`). The project keys are
`project.use`, `project.write`, `project.members`, `project.audit`, `project.share`
and `mcp.approve`; they never appear in `users.permissions` or in
`/api/auth/status`, whose 16-key list keeps its meaning. A platform administrator
bypasses membership (`access.has_data_bypass`) and holds every key, but is still
filtered to the selected project: there is no mixed view except the fleet, costs,
TTFA and audit routes, which run outside any project.

**`inherit` and the default project.** The `inherit` role exists in the default
project only and reads the account's global keys (today's 16-key computation) plus
`project.use` / `project.write` (and `mcp.approve` when the account holds
`promotion.approve`). It is the compatibility layer: the migration adds every
non-admin account that holds a workspace grant to that workspace's default project as
`inherit`, and every later grant write (`POST` / `PATCH /api/users`, approval,
`PUT /api/workspaces/{id}/grants`) keeps the `inherit` rows in step in the same
transaction (`access.sync_default_membership`); revoking the grant removes the
`inherit` row, an explicit role is left alone. An `inherit` row reaches only the
workspaces the account itself is granted. So changing an account's global permission
chips still changes what it may do in the default project, and a single-project
install behaves exactly as before.

### Rows: `ProjectOwned`, `ProjectShared`, criteria and stamping

`app/core/project_scope.py` puts the boundary into the ORM, and each piece fails
closed:

- **Mixins.** A model inherits `ProjectOwned` (always one project: datasets, chat sessions and messages, eval runs, criteria, releases, API keys, …) or `ProjectShared` (`project_id` may be NULL, meaning workspace- or platform-level and readable from every project: agents — a system preset is NULL —, jobs, managed memories, governance changes, audit events, alert rules, resource mappings, Connections). `core/db.py` lists them as `PROJECT_STRICT_TABLES` / `PROJECT_SHARED_TABLES`; each has an indexed `project_id` column, plus a `(workspace_id, project_id)` index on the hot lists.
- **Automatic criteria.** A `do_orm_execute` listener adds `project_id = :current` (shared tables: `OR project_id IS NULL`) to every ORM SELECT, UPDATE and DELETE while a project is current (`with_loader_criteria`, aliases included). Lists are therefore filtered in SQL before any count, offset or limit, so totals do not leak.
- **`ProjectSession`.** `Session.get` answers from the identity map without SQL, which the listener never sees. `db.SessionLocal` builds `ProjectSession`, whose `get` re-checks the row and returns `None` for another project's; `owned_get` does the same on any session. Raw `text()` SQL is not filtered, so request-path raw SQL adds its own project condition.
- **Stamping.** A `before_flush` listener fills a new row's `project_id`: from its parent row (`__project_parent__`: an eval run from its agent, a message from its conversation, a share link from its agent or annotation task, …), else the current project, else the project of the row's workspace when that workspace is bound to exactly one. Otherwise the flush raises `ProjectStampError`: an owned row with no project would be invisible everywhere, and filing it under some guess would leak it. An existing row's project changes only through `POST /api/projects/{id}/transfer`.
- **Current project.** A context variable. `route_policy.bind_request_project` (an async app-level dependency, so sync handlers and streaming generators inherit it) sets it from the request's project; `/v1` sets it from the key, `/share/*` from the link. Background threads start with none, so a worker sees every row unless it enters `project_context(<row's project_id>)`, which the deploy pipeline, job resume, chat turns, evaluation and the scheduler all do with the project of the row they work on. Cross-project reads (name uniqueness, the fleet, a pperm check) enter `project_context(None)` explicitly.

Startup runs `assert_every_row_has_a_project` beside the workspace check: derived
rows take their parent's project, rows of a single-project workspace are adopted with
a WARNING, and a strict row with no project in a workspace bound to several projects
refuses to start (see *Operating projects*).

Name uniqueness stays workspace-wide: S3 keys, ECR tags, gateway targets and registry
records use the agent name, so two projects of one workspace cannot hold the same
agent name. `agent.name_exists` names the holder only when the caller can see it.

**New agents belong to a business project.** Every non-system creation path —
`POST /api/agents`, a Harness → Runtime conversion, a discovery import, the architect's
approve, and a promotion that creates its target — goes through
`services/agent_project.py`: the project is the body's `project_id` (it must equal an
`X-Project` header sent alongside, else `422 agent.project_mismatch`), else the request's
project, and it must be reachable by the caller, bound to the workspace, `active`, not a
workspace `default` project, and carry a `department_id`; the caller must hold the
route's key there. Refusals are `422` `agent.project_required` / `agent.project_not_found`
(missing and unreachable share it) / `agent.project_default_not_allowed` /
`agent.project_archived` / `agent.project_no_department`, with the projects page as the
next step. `Agent` sets `__project_no_default_stamp__`, so `before_flush` never files a
new non-system agent under `default` silently (`ProjectStampError`); system presets stay
platform rows (`project_id` NULL) and agents already in `default` keep it. The V3 create
form's project picker reads `GET /api/agents/eligible-projects`, which lists, server-side,
only the business projects the caller may create agents in.

### Requests: route roles and `X-Project`

`ROUTE_POLICY` (`app/core/route_policy.py`) classifies every route. Besides
`public` / `member` / `admin` / `perm:<key>`:

- `perm:project.use` (chat, invoke, feedback, labels, the caller's own sessions) and `perm:project.write` (every other ordinary write) are project keys. Every member write route has been reclassified, so a plain `member` entry is a read or a hub-global route.
- `perm:<key>` on a project-scoped route is checked against the effective keys of the request's project (`require_permission` reads `request.state.project`), not the account's.
- `infra` / `infra+perm:<key>` marks a write to workspace-shared infrastructure (governance policies, registry MCP / skill records, gateway Lambda and tool targets, marketplace publishing, price refresh). `require_infra` lets it through on `project.write` (or `<key>`) while the workspace holds one active project and requires an administrator once it holds several (`403 auth.infra_admin_required`): one project must not reshape what another runs on.
  The exception is a registry **Agent-skill** record: it belongs to the project that registered it (a `resource_creators` row of kind `registry_skill`, written by `POST /api/registry/records` and `/skills/import`), so in a multi-project workspace that project's members create one and run its lifecycle (`PUT`, `DELETE`, `/action`, `/reimport`) with `project.write`. MCP records, skills no project owns (system presets, records created outside the console) and every other `infra` route stay administrator-only (`PROJECT_SKILL_CREATE` / `PROJECT_SKILL_RECORD` in `route_policy.py`).
- `pperm:<key>` is for the hub-global `/api/projects/{project_id}/…` routes: `<key>` in the project named by the path (a non-member gets 404).
- `PROJECT_EXEMPT` lists the workspace-scoped routes that need no project (catalogues, workspace settings, release policies, `/api/costs`, `/api/inbox`, network profile writes, …). `HUB_GLOBAL_PREFIXES` gained `/api/projects`, `/api/role-templates`, `/api/me`, `/api/admins`, `/api/project-groups`, `/api/departments`.

`tests/test_route_policy.py` keeps the drift checks (every live route classified and
no stale entry, no workspace write left to a plain member, every `infra` route
project-scoped, every `PROJECT_EXEMPT` entry a live workspace route), and `scripts/check_api_docs.py` reports routes
api.md does not mention.

`enforce_route_policy` resolves the workspace, then — for a project-scoped route —
the project, **before** the permission check (the keys depend on the project):

1. `X-Project` (stamped by the frontend fetch wrapper next to `X-Workspace`, `lib/workspace-header.ts`; remembered per workspace in `localStorage` `launchpad_project:<workspace_id>`).
2. Without the header: an administrator gets the workspace's default project; a member with one reachable project gets it; a member with several gets the workspace default if they reach it, else `400 project.header_required` (`detail.available`). A member with none gets `403 project.none`.
3. A project that is not bound to the workspace or that the caller cannot reach answers **404 `project.not_found`**, never 403: a project's name and existence can be sensitive. The same holds for any resource id of another project (the kind's own not-found). Inside one's own project a missing key is still `403 auth.permission_required`.
4. A non-read request in an archived project answers `409 project.archived`.

Old clients, curl and the workshop seed scripts send no header and land in the
default project. `/api/auth/status` and `GET /api/me/access` feed the frontend: the
V3 top bar shows a project switcher next to the workspace switcher only when the
caller has more than one project there, and a read-only chip only when the workspace
is bound to more than one; the command palette gains "switch project" entries in the
same case.

### Caching: the access epoch

Each request needs the caller's reachable projects and keys. `access.snapshot_for`
loads all of a user's memberships in one query (projects × bindings × direct row ×
group row, plus referenced custom roles) and caches it in-process per user (LRU of
5,000, 60 s TTL as a backstop). Every request first reads `access_epoch.value` (one
primary-key read); a changed epoch drops the cache. Every write that changes who
reaches what — members, group members, group ↔ project, bindings, project status,
custom roles, account role / permissions / status, workspace grants — bumps the
epoch **in the same transaction** (`access.bump_epoch`), so a revocation applies to
the next request in every process and every instance sharing the ledger. Workspace
bindings are cached the same way (`workspace_bindings`).

### API keys, links, observability, audit

- **`/v1` API keys** carry `api_keys.project_id` (backfilled to the default project). Every `/v1` call runs as the key's project and ignores any header, so `/v1/agents` and invoke see only that project's agents. A key stops working (`401`, and is disabled for good) when its project is archived or unbound from the key's workspace, or when its creator no longer reaches that project; membership writes disable such keys in the same transaction (`api_keys.disable_offboarded_keys*`). Minting a key needs `project.write`.
- **Share, review, annotation and channel links** take their project from their target. `/share/*` runs as the link's project; a link whose target moved to another project or whose project is archived answers the same 404 as a revoked token, and archiving revokes the project's links.
- **Observability.** CloudWatch spans carry no project, so `observability.ProjectAgents` is an allow-set: a row is kept when its session or `service.name` resolves through the ledger to an agent of the current project or a platform preset. In a workspace bound to several projects an unattributed row is shown to administrators only; in a single-project workspace the filter is inert. Dashboard and intent caches are keyed by project. `/api/costs` stays admin-only and project-exempt, with a `?project=` filter.
- **Audit.** `audit_events` gains `project_id` (the current project, NULL for workspace-level actions) and `actor_user_id`. Structure events (`project.*`, `project.members.*`, `group.*`, `department.*`, `admin.grant` / `.revoke`, `user.sessions.revoke`, `project.transfer`) are hub-global rows. `GET /api/audit` (admin) filters by `project` and `actor` across projects.
- **Background work.** `jobs.project_id` is stamped from the agent; the pipeline, resume and promotion execution run inside that project. The scheduler runs each watch, alert and MCP health check as its agent's project and skips archived projects.
- **Visibility.** The workspace's `shared` / `isolated` setting (see *Member resource visibility*) applies inside the project filter: the per-creator rule narrows what a member sees of their own project. A project's `settings.resource_visibility` is stored and owner-editable, but the filter still reads the workspace's setting (`visibility.Viewer.of_request`).

### Separation of duties

The object-level checks are unchanged — nobody approves a promotion they requested
(`promotion.self_approval`), signs a release they requested, approves their own
waiver or signs their own criteria — whatever roles they hold. New SoD columns
(`*_user_id` beside `requested_by`, `reviewed_by`, `signed_by`, …) are compared first,
so a re-registered username is not the old actor. In addition:

- nobody but an administrator edits their own project membership (`403 project.self_membership`), so an owner cannot hand themselves a sign key;
- the member views flag a person who holds both sides of a pair in one project (`agents.deploy` + `release.sign`, `criteria.manage` + `criteria.sign`), and with the admin-only setting `settings.sod_strict` such writes are refused (`409 project.sod_conflict`).

### Department administrators

A department (`departments`) can have **department administrators**
(`department_admins`, appointed by a platform administrator) and a set of workspaces
**open** to it (`department_workspaces`). For their department's projects and groups a
department administrator passes the structure routes without being a member: create
projects (only in the open workspaces, up to `settings.max_projects`), rename, regroup,
archive, bind open workspaces (never `make_default`), manage members and custom roles,
and every project-group route (the group list only with `?department_id=`). With
`settings.can_create_users` they may also invite new `member` accounts into one of the
department's projects, valid at most `settings.user_validity_max_days`. The overlay is
`DEPARTMENT_ADMIN_ROUTES` in `core/route_policy.py`, read per request (no access epoch:
it grants no data access): another department's project or group answers 404, and
workspace-scoped routes still need a membership, so a department administrator sees
the department's projects but not their data. `GET /api/me/access` returns
`managed_departments[{id, name, workspaces}]` and project detail `can_manage`, which is
how the V3 console shows them the Configure pages and pickers.

### Phase 2: the project IAM boundary

When a project is switched to `isolated` (`PUT /api/projects/{id}/isolation`), the
provisioning job's `boundary` stage (`services/project_iam.py`) writes one customer
managed policy per project × workspace, `launchpad-project-<pid>` (with a Region
suffix where needed), and every agent of the project gets its own execution role
with that policy as its **permissions boundary**. The per-agent identity policy stays
least-privilege from the spec; the boundary is the ceiling over it, built from the
project's tag (`aws:ResourceTag/launchpad:project`), name prefixes (memory, S3, ECR,
M2M provider) and explicit ARNs (knowledge bases, Connections). It is versioned
(`agents.isolation_applied = boundary-v<N>`) and widened at deploy time when a spec adds
a Connection or a KB. An isolated project's agent never uses the shared execution role:
a deploy without a per-agent role is refused with
`409 project.isolation_requires_per_agent_role`, and one whose boundary cannot be made
current with `409 project.boundary_unavailable`.

### Per-project telemetry and cost

For an agent of an isolated project the deployer adds two marks
(`services/project_telemetry.py`): `OTEL_RESOURCE_ATTRIBUTES` carries
`launchpad.project`, `launchpad.agent` and `launchpad.workspace` (the platform's entries
first; a spec cannot set a `launchpad.*` key), and the runtime / harness / project
memory are created with the tags `launchpad:project`, `launchpad:workspace`,
`launchpad:agent-id`, derived from the same attributes so the two never disagree. The
observability queries filter on the span attribute, and rows then carry `project`;
spans without it (a shared project, BYOC code with its own OTEL setup) fall back to the
ledger resolver. Model spend is billed to the model, not the runtime, so behind
`LAUNCHPAD_PROJECT_INFERENCE_PROFILES` (off by default) an isolated project's agents
use an **application inference profile** per project × workspace × model, tagged with
the project, that Cost Explorer can split once the payer account activates the tag.
`/api/costs` attributes each row from the span attribute, else the profile in the model
id, else the ledger agent (`project_source`), and returns a `by_project` breakdown.
Shared projects keep today's requests exactly.

### Budgets: projects, departments and the three cost views

Budgets are **project budgets**; there is no per-person budget anywhere (a member's
username may label usage rows for drill-down, never a budget subject). The month is
the UTC calendar month.

- **Project budget** — `projects.settings.quotas.monthly_budget_usd`, enforced by
  `project_quotas.assert_budget` on every billable route (`core/billing_policy.py`) and
  at the one invoke entry point, against the project's month-to-date spend on its
  budget basis (price-map estimate or billed `launchpad:project` cost; cached 60 s).
  Its **overrun switch** `projects.settings.allow_overrun` (default off) picks the
  lines: off — alert rules at 80 % and 100 %, block at 100 % (`409
  project.budget_exhausted`); on — alerts at 80 % and 105 %, block at 110 %
  (`OVERRUN_ALERT_FRACTION` / `OVERRUN_BLOCK_FRACTION`, constants). The alert rules are
  ordinary `cost_mtd_usd` rules the platform keeps in step (`alerts.sync_budget_rule`).
- **Department budget** — `departments.settings.budget{monthly_budget_usd,
  allow_overrun}`, set by platform administrators only (`PUT
  /api/departments/{id}/budget`). It is an **allocation ceiling** and never blocks an
  operation: the sum of the department's project budgets (archived projects included)
  may not exceed it (`409 department.budget_overallocated`, `detail{budget_usd,
  allocated_usd, requested_usd, remaining_usd}`), it cannot be lowered below what is
  allocated (`409 department.budget_below_allocated`), and a project with a budget
  cannot move into a department that has no room for it. Only writes that *add*
  allocation are checked, so a legacy over-allocated department can always be lowered
  back under its ceiling. The department's overrun switch (default off) gates its
  projects' switches (`409 department.overrun_disabled`); turning it off while projects
  use theirs is refused with their list (`409 department.overrun_in_use`).
- **Who allocates** — `department_budgets.budget_scope` decides the level. The
  organization level takes the account key `budget.manage`
  (`users.PLATFORM_PERMISSIONS`; admins hold it by role, a steward only when an
  administrator grants it on the Users page): department ceilings and switches, and
  every project's budget, department-less ones (`default`) included. The department
  level comes with the identity and needs no key: a department administrator sets the
  budgets and switches of the projects of the departments they administer
  (`PUT /api/projects/{id}/budget`, the `dadmin` overlay); removing them as
  administrator removes it. Never the department's own ceiling. Anyone else gets
  `403 budget.forbidden`, on every path that can change a budget (the budget routes,
  `PUT …/quotas`, a project's settings). A department with no budget is unlimited: its
  projects' budgets are allocated without a ceiling. The other quotas (`PUT …/quotas`)
  stay platform structure. Budget writes are
  outside `BILLABLE_PREFIXES`, so they are never gated themselves, and are journalled
  as `project.budget.update` / `department.budget.update`.
- **Department alerts** — at the end of each alert evaluation pass,
  `department_budgets.evaluate_departments` sums each budgeted department's project
  spend (the gate's cached figure) and records crossing 80 % / 100 % once per month per
  line in a `platform_settings` row (`budget.department.<id>`, also a short daily
  history for the trend), journalled as `department.budget.alert`. Notify only. The
  department's administrators and the platform's read them from `GET
  /api/budgets/me`; they are not alert rules because a workspace-level rule is
  readable by every member of the workspace.
- **Cost views** (V3 `/v3/costs?view=project|department|platform`, entries shown only
  to whoever can open them): the project page reads `GET /api/projects/{id}/quotas`
  (members; budget, switch, the 80/100 or 80/105/110 lines); the department page `GET
  /api/departments/{id}/budget` (its administrators and platform administrators:
  budget, allocated, unallocated, spend, per-project rows, trend); the platform page
  `GET /api/budgets/platform` (administrators, not a steward: every department and
  project plus the **platform / unattributed** line — the month-to-date estimate no
  project carries in a multi-project workspace, with the shared components named).
- **Check.** `backend/scripts/e2e_costs_dept_admin_browser.py` (headless, no AWS
  call; in `run_live_e2e.sh`) drives the cost pages as a throwaway department
  administrator and as an administrator against its own fixture department.
- **COA cost attribution** — a turn's COA tool calls, direct or gateway mode, run
  inside the agent's invoke, so they meet the agent's project's `assert_budget` before
  the gateway preflight, a COA token or any COA call; their model tokens are on the
  agent's spans, attributed to its project. COA's fixed costs are the platform's: the
  managed gateway `launchpad-coa-gw` is tagged `launchpad:project=shared` by the cost-tag
  back-fill (`cost_tags.context_layer_infra`), so its fees land on the billing view's
  shared row; COA's own runtimes are a connected deployment Launchpad never tags.

### Administration

Administrators manage structure from **Configure** in the V3 console: **Users**
(project and group memberships, bulk "add to project", forced logout through
`users.session_epoch`), **Administrators** (`/v3/admins`), **Projects** (members,
roles, workspace bindings, settings, moving an agent), **Project groups** and
**Departments**. A project owner (`project.members`) reaches **Project settings** for
the current project. Account management, project structure and group / department
routes are hub-global; `access.can_manage_structure` is the single check (phase 3's
steward changes only it).

An **organization admin** (global role `steward`, projects design §11.4) manages
structure without project data. Its V3 rail is Configure's structure entries alone
(Users, Administrators, Projects, Project groups, Departments, Workspaces, Models,
Network, Storage: `STEWARD_ENTRIES` in `v3/nav.ts`); every other page renders
`StewardEmptyState` (`workspace/steward-paths.ts`), and the workspace and project gates
do not stop it for holding no grant. It creates and edits accounts on the Users page but
never the platform roles. Each membership change it makes in a project is noticed to the
project's owners, who see the notices — and, under the two-person rule, confirm or reject
the pending change — at the top of the project's Members tab (`/api/steward/*`).

### Operating projects

**Existing installs need no action.** On the first start of this version `init_db`
runs, in order: `create_all` (the new tables), `_migrate` (additive `project_id`
columns and indexes on every owned and shared table, `users.session_epoch`, the SoD
`*_user_id` columns), `_seed_default_projects` and the startup checks. The seed is
idempotent; its one-time part runs while the `platform_settings` marker
`migration.projects_v1` is absent:

1. create the `default` project and bind it to every workspace (default binding where the workspace has none);
2. file the rows of `agents` (except system presets), `jobs`, `managed_memories`, `api_keys`, `resource_creators`, `eval_datasets`, `eval_pipelines`, `skill_lab_tasksets`, `assistant_conversations` into their workspace's default project, and every derived row into its parent's project (an orphan into its workspace's default);
3. add every non-admin account with a workspace grant as `inherit` member of that workspace's default project, backfill the SoD user ids, bump the epoch, set the marker.

Nullable rows (governance, audit, alerts, mappings, Connections) stay NULL, meaning
workspace-level as before. A knowledge base with no `resource_creators` row belongs to
the workspace's default project. With one project per workspace nothing visible
changes: no switcher, the same `/api/auth/status`, the same pages, `infra` routes
behave like `project.write`. Copy `data/launchpad.db` before the first start (the
upgrade is additive; an older binary still starts on the migrated ledger because
drift checks look only for missing columns).

**Rehearse an upgrade** on a copy of a production ledger before the real one:

```bash
cd backend
uv run python scripts/rehearse_projects_migration.py /path/to/copy-of-launchpad.db [--keep] [--out /tmp/rehearsal.db]
```

The source file is only read (SQLite online backup, read-only); `init_db` runs on a
copy with AWS clients refused, and the script prints per table the rows, rows without
a project before and after, the orphans, the `init_db` time and the checks (no schema
drift, every row has a workspace, every strict row has a project, the marker set).
Exit 0 = the upgrade would succeed; 1 = a check failed (the copy is kept).

**Leftover rows after a rollback.** Rows written by an older binary after a downgrade
have no `project_id`. In a single-project workspace startup adopts them with a
WARNING. In a workspace bound to several projects startup refuses, naming the tables
and counts; stop the backend and file them:

```bash
cd backend
uv run python scripts/backfill_projects.py --dry-run                    # report only, on a throwaway copy
uv run python scripts/backfill_projects.py --workspace prod --project payments
uv run python scripts/backfill_projects.py --db /path/to/launchpad.db   # another ledger file
```

It applies the same schema steps as startup, files derived rows under their parents,
adopts single-project workspaces' rows, and with `--workspace W --project P` files
the rest of W's strict rows into P (which must be bound to W). It writes no audit
row, bumps no epoch and calls no AWS API. Exit 0 = startup will accept the ledger.

**Create and split projects.** As an administrator: create departments and groups if
wanted (**Configure → Departments / Project groups**), then **Configure → Projects →
New project** (id, name, workspaces to bind, owners; `POST /api/projects`). The first
binding of a second project to a workspace turns it multi-project: `infra` writes
become administrator-only there and unattributed telemetry is hidden from members.
Add members on the project's **Members** tab (or from **Users**, one account or in
bulk); members with several projects pick one in the top-bar switcher. Move existing
agents out of `default` with **Move agent** (`POST /api/projects/{id}/transfer`): a
ledger-only move of the agent, every derived row and its jobs, in one transaction;
source-project keys scoped to it lose it. The default project cannot be archived or
unbound from its workspaces; another project's **Archive** makes it read-only,
revokes its links and disables keys whose creators no longer reach it.

## Agent-DLC — criteria, golden sets, calibration and the release gate

The design is [docs/agent-dlc-design.md](agent-dlc-design.md); this section is the map
of what was built. The methodology's claim is *the release is decided by evaluation, not
by a meeting*, so the platform owns four things it previously left to people: the
standard (a criteria table), the evidence (a golden set), whether an LLM judge may stand
in for a person (calibration), and the decision itself (a gate in front of traffic).

**Modules.** `app/dlc/` holds the services — `criteria.py` (versions, templates,
validation, sign-off, diff), `engine.py` (per-criterion verdicts from evaluation records,
Wilson intervals, pass^k), `gate.py` (the four gates), `calibration.py` (blind labelling,
Cohen's κ with bootstrap CI), `golden.py` (three splits), `releases.py` (named endpoints,
sign/block/rollback, waivers), `cost.py`, `compare.py` (the fix ladder), `admission.py`,
`watch.py` and `scheduler.py`. Pure statistics are in `app/evaluation/stats.py`. Models
are `app/models/dlc.py` (11 workspace-scoped tables) plus additive columns on
`eval_datasets`, `eval_runs` and `agents.endpoint_mode`. Routes are `app/routers/dlc.py`
(console) and `app/routers/share_annotate.py` (public). The console is
`/v2/eval/standards` with `?view=scorecard|criteria|golden|admission|calibration|release|watch|compare|audit`.

**Criteria.** A criteria set is versioned under a `lineage_id`; publishing freezes a
version, editing opens the next one, and a published agent set must be signed by someone
other than its editor (`criteria.sign`). Validation is enforced, not advisory: a red line
cannot be decided by an LLM judge; cost and performance criteria must be metrics; every
dimension needs a criterion or an explicit `n/a:<dimension>` note; at least one red line.
A judge criterion's *effective* tier is `observe` until a calibration record says it is
aligned and unexpired — the editor, the gate and the scorecard all read the effective
tier. Templates (`kind="template"`) are adopted explicitly into a new agent version; a
newer template version is reported, never applied under the agent.

**Golden sets.** A parent `EvalDataset` (`role="golden"`) groups three split datasets,
each synced to its own AWS Dataset. Item provenance lives in `metadata.dlc` (case tier,
criteria ids, origin, `expected_source`). `POST /api/golden-sets/{id}/seed`
(`golden.admit`) is the **only** path that writes the holdout: it stratifies by case tier
and then seals the holdout (`golden.holdout_sealed`). Item adds, moves and admission
refuse the holdout. Coverage is criteria × case tier with `thin` (< 3 items) flagged.

**Runs and pass^k.** AgentCore has no per-scenario repetition parameter, so
`execute_run(repeats=k)` invokes each scenario k times in distinct sessions and records
`attempts`; `GetBatchEvaluation` returns aggregates only, so `engine.finalize_run` reads
per-session records from the evaluation output log stream and writes
`criterion_results`. A run pinned to a release carries `endpoint_qualifier`, so the
gate can prove which endpoint it invoked. Before a run, `POST /api/eval/runs/estimate`
returns items × k × (agent per-session + judge per-item) with its basis named, and the
workspace policy's `eval_cost_confirm_usd` / `eval_cost_max_usd` require confirmation or
refuse.

**The gate.** Applied in fixed order: red lines (any violation → BLOCKED, never
waivable) → denominator (missing verdicts or > 5% undetermined → INVALID) →
per-dimension thresholds at integer-percent precision (miss → BLOCKED unless an active
waiver covers it) → observed criteria (recorded only). Provenance issues — an unsigned
criteria version, a run against another version or endpoint, no holdout evaluated —
also make the verdict INVALID. INVALID is not a failure: the evidence cannot decide.

**Gate before traffic.** `UpdateAgentRuntime` / `UpdateHarness` auto-roll the DEFAULT
endpoint, so a gated agent (`agents.endpoint_mode="live"`) serves production through a
named `live` endpoint and every invoke path passes `qualifier="live"`
(`services/invoke.production_endpoint`). A new invoke path must go through that helper
or it bypasses the gate. With the workspace policy
`release_mode="gated"`, `pipeline._finish` calls `releases.after_deploy`, which points
`candidate` at the new version and opens a `ReleaseRecord`; `live` is untouched. Signing
(`release.sign`, signer ≠ requester) re-points `live`; rollback re-points it to the
previous version. Nothing is deleted. `POST /api/agents/{id}/release/migrate` moves an
agent onto named endpoints, and carries `release.sign`: what production serves is a
release decision, not a build step.

**Two traps named by the real-AWS e2e** (`backend/scripts/e2e_agent_dlc.py`), both of
which only appear once an agent actually has named endpoints:

* *Telemetry is per endpoint.* Every AgentCore endpoint writes its own content-log
  group (`…-<endpoint>`) and service name (`….<endpoint>`). Evaluation, observability,
  online-eval matching and experiment recommendations all used to hardcode `DEFAULT`,
  so a gate run against `candidate` timed out waiting for telemetry that was in
  `…-candidate` — and, worse, a gated agent's production traffic (`…-live`) would have
  vanished from the dashboards. `evaluation.service.telemetry_endpoint(agent,
  qualifier)` is now the single rule: a pinned run reads its own endpoint, a gated
  agent reads `live`, everything else `DEFAULT` (unchanged).
* *Endpoint deletion is asynchronous, and a harness endpoint is slow.* AgentCore
  refuses to delete a runtime or harness that still has endpoints, and
  `DeleteHarnessEndpoint` only moves the endpoint to DELETING — observed at several
  minutes. All four delete paths call `releases.delete_endpoints` first, which issues
  both deletes (AWS removes them in parallel) and waits a **short** bound (45 s: enough
  for a runtime endpoint). If AWS still holds the harness, `delete_agent_resources`
  raises `agent.teardown_pending`, the route marks the ledger row deleted with
  `aws_resource_deleted: false`, and `dlc.scheduler.sweep_deleted_resources` retries
  until AWS lets go — rather than holding an HTTP delete open for minutes or leaving an
  operator to retry by hand against an asynchronous state machine. The sweep asks
  `GetHarness` before deleting, because **AgentCore answers `DeleteHarness` on an
  already-gone harness with `AccessDenied`, not `ResourceNotFound`**: inferring "gone"
  from the delete error would either retry a vanished resource forever or swallow a real
  permission problem. The deferred row is stamped `teardown_pending_at`, and the sweep
  retries every stamped row whatever its `endpoint_mode` — a default-mode harness held
  by Runtime Canary endpoints is retried too: it re-issues the delete of any named
  endpoint not yet DELETING, then deletes the resource, its execution role and its A2A
  record. Its done-marker is `teardown_pending_at` cleared and `endpoint_mode` back to
  `default` (rows deferred before the column are still found by `live`), which keeps
  `resource_id` as the historical pointer every deleted row retains. Waivers need a reason, a named risk owner, an expiry
≤ 30 days and a second person (`waiver.approve`); the gate report counts how often a
criterion has been waived.

**Calibration and annotation links.** A labelling task hides the judge's verdict from
annotators until it closes. `agreement` reports human–human κ, judge–human κ with a
bootstrap CI, the confusion cells and the disagreements; `decide` refuses `aligned`
when the numbers (κ floor, n ≥ minimum, not worse than human–human − 0.05) do not
support it. A record expires after the workspace's `calibration.period_days`.
Annotation links (`ShareLink.kind="annotate"`, page `/r/annotate/<token>`) let an expert
label without an account: each link is appended to the task's annotators as
`link_<id>`, the public route serves only the items and that annotator's own labels,
and a **prod-tier workspace refuses to mint one** — a link issued while the workspace
was dev stops resolving once it is prod.

**Admission, watch, scheduler.** Admission candidates are collected from thumbs-down,
SME corrections, the issue box and Insights clusters; a candidate the evaluators scored
as a pass is ranked first. Admitting needs a human expected answer (`agent_observed` is
refused), a redaction that did not block, and writes dev or regression only. A watch
config re-runs the regression split daily/weekly with an optional cost ceiling; alerts
are separated into count (page), score (against an 8-point rolling median, silent until
the baseline exists) and distribution (TVD, naming the component that moved), and
`quiet: true` is returned explicitly. `scheduler.py` runs on a daemon thread started
next to `start_auto_refresh()`; each task is claimed through a conditional UPDATE on
`scheduler_claims` so two processes never run the same tick.

**Permissions.** Six keys join `AGENT_PERMISSIONS`: `criteria.manage` (member,
operator), `criteria.sign`, `golden.admit`, `judge.calibrate` (granted to named people,
never by role), `waiver.approve`, `release.sign` (operator). Every route is classified
in `route_policy.py`; publish, sign, seed, item adds **and removals** (`move` / `retire`
take a case out of the gate's denominator, which changes the standard just as adding one
does), admit, calibration `decide` (a `not_aligned` verdict demotes a gating judge),
waiver approval, migrate, evaluate and release sign/block/rollback are `PROD_PROTECTED`.
Decisions are written to `audit_events` and read back at `GET /api/audit`.

**Three things a security review found, all now enforced server-side.** They are listed
because each was a *consistency* failure rather than a missing idea — the rule existed
and one path did not apply it. (1) `GET /api/annotation-tasks` hardcoded
`privileged=True`, so the list handed every annotator the judge's verdicts the detail
route withholds; both now compute it the same way. (2) A watch config's `dataset_id`
was stored unchecked, so another workspace's sealed holdout could be replayed against an
agent the caller controls; it is resolved through `golden.get_parent(db, ws.id, …)` on
write *and* on run. (3) A single annotator's vote counted as a consensus and the
human-ceiling term was skipped when no second rater existed, so one person could label a
run to match the judge and certify it; an item now needs two agreeing raters to
contribute, `aligned` needs a real `human_human_kappa`, and `decide` refuses an
annotator on the task. Admission also computes the PII screening itself instead of
relying on the detail route having been opened, and records on the item how the text was
screened (`unscreened_items` in coverage when the workspace has no guardrail).

**Cost is enforced, not just displayed.** `dlc/cost.estimate` prices items × k ×
(agent per-session + judge per-item) with its basis named, and `assert_allowed` is
called on both entry points — `POST /api/eval/runs` and the release gate's
`start_evaluation` — so `eval_cost_max_usd` / `eval_cost_confirm_usd` refuse before a
session is spent. `PUT /api/release-policies/{workspace_id}` is where an administrator
sets `release_mode`, `calibration` and those two ceilings.

**Known limits.** pass^k multiplies cost by k and is opt-in with the estimate shown;
load and stress testing are out of scope; A/B experiments remain gateway-only with two
variants; ground-truth evaluators cannot run online, so watch runs replay golden splits
rather than scoring live traffic against expected answers.

## Skill Lab — skill evaluation & training (SkillOpt integration)

Skill Lab closes a loop no other console surface offers: a Registry skill record
is evaluated against a rubric-carrying task set on real AgentCore Runtime
microVMs, optimized by the vendored [SkillOpt](https://github.com/xiehust/SkillEvalOpt_Studio)
training loop (rollout → reflect → aggregate → select → update → gate), and the
improved SKILL.md is published back onto the same record as a bumped minor
version — ready to attach to agents.

**Vendored engine, subprocess-only.** `vendor/skillopt/` is a trimmed subset of
the SkillOpt research framework (upstream pin and every local patch documented
in `vendor/skillopt/LAUNCHPAD_DEVIATIONS.md`; notable patches: a `bedrock_chat`
judge/optimizer backend on the Converse API so the LLM judge runs zero-key off
the instance role, and a worker Dockerfile with a pinned claude CLI). The
backend process **never imports** the vendored tree — `evaluate_skill.py` /
`train.py` run as subprocesses in a dedicated venv (`data/skill-lab-venv/`,
provisioned by bootstrap) with an allowlisted environment. A guard test
enforces the boundary; task-set validation shells out to the same `load_tasks`
the CLIs use, so API acceptance can never drift from CLI acceptance.

**Execution topology.** The orchestrating subprocess stays on the backend host
(judge and optimizer calls go straight to Bedrock); each task's agent rollout
runs in its own AgentCore Runtime microVM session on the
`launchpad_skill_lab_worker` runtime (managed session storage, 5-minute idle /
8-hour lifetime, image content-addressed into the shared `launchpad-agents`
ECR repo, built by the shared CodeBuild project). Workspaces bootstrapped
before this feature simply show Skill Lab as unprovisioned — the worker's
resource keys are deliberately optional.

**Console surface** (`/skill-lab`, `?view=tasksets|eval|train`): task sets
(train/val/test splits, row-level validation with the vendored validator's own
locators), evaluation jobs (any-status Registry skills or ad-hoc zip uploads,
live log tail, per-task hard/soft judge results, artifact browser), training
jobs (live score curve + step timeline with ACCEPT/REJECT gate decisions,
SEED→BEST diff, resume-from-checkpoint for interrupted runs), and publish
(minor version bump via the record-update path; the record settles into DRAFT
— surfaced with an optional re-approve). The Registry drawer links here via
"Evaluate in Skill Lab".

Ledger: `skill_lab_tasksets` + `skill_lab_jobs` (workspace-scoped); artifacts
live under `data/skill-lab/` (task files, job logs, the CLI's out/ tree —
the files, not the ledger, are the source of truth for content).

**Artifact browser.** `GET /api/skill-lab/jobs/{job_id}/artifacts?path=`
(listing, or a text read capped at 512 KB) and
`GET /api/skill-lab/jobs/{job_id}/artifacts/raw?path=` (byte-exact download)
serve any job the caller's workspace owns, in every status: a job that has not written `out/` yet
answers an empty root listing, a sub-path that is gone is a 404. The server's
path guard (`_safe_resolve`: no absolute/`~`/backslash/NUL paths, both sides
resolved so a planted symlink cannot widen the window) is the authority; the
console never asks for anything outside it. The console shows the browser for
queued and running jobs as well as finished ones and **never polls the tree** —
the listing and the open file carry a "refreshed at" time and a manual refresh,
and a refresh that fails keeps the last successful load on screen labelled with
its time instead of clearing it. Every response is checked against the job, path
and request generation it was issued for, so a slow answer for a previous job,
directory or file cannot land on the current selection (including after the
viewer was closed). `.md` artifacts get a Preview/Source toggle: Preview is the
Chat renderer stack (GFM tables, fenced-code highlighting, no `rehype-raw` — HTML
in the artifact is inert) with a stricter link policy in
`skillLab/ArtifactMarkdown.tsx` — relative links resolve from the current file's
directory inside `out/` (`artifactLinks.ts`) and open through the same scoped
API, http/https/mailto links open in a new tab with `rel="noreferrer noopener"`,
every other scheme, absolute path, root escape, malformed percent-encoding or
encoded separator (`%2F`, `%5C`, `%00`) renders as inert text, and images are
never fetched (a placeholder names the source; an in-tree image opens as an
artifact). Source mode shows the server's capped, UTF-8-decoded text verbatim
(no rendering); only the raw download is byte-exact. The truncation notice is
shown in both modes.

**Evaluation token usage.** Each row of the CLI's `results.json` may carry the
token counters the vendored producers observed: `usage` for the target rollout
(from the exec transcript — claude transcripts yield `input` / `cache_write` /
`cache_read` / `output` plus a `total`; a codex transcript yields **only** a
total, its four counters being literal zeros) and `judge_usage` for the judge
(`input` / `output` only — the agentic judge worker folds cache reads and writes
into `input`, the chat judge never sees them). The backend
(`skill_lab/artifacts.py`) validates these per row into `token_usage.{target,
judge}` and sums them per side onto the summary, **without** touching the
scoring semantics (invalid-score rows stay out of the pass-rate denominators but
their usage counts — the tokens were spent). The projection is deliberately
strict: a counter no row reported is `null` (unknown, never 0); a bool,
negative, NaN/inf, fractional or non-numeric counter is dropped and the row
marked `malformed` rather than coerced to zero; a `total` beyond the counters
becomes `unattributed` (the codex total-only form, whose zero placeholders are
then reported as unknown; a total-only `0` is still a report), while a total
below them is ignored. The raw `usage` / `judge_usage` stay on the row, made
JSON-safe only where the file carried `NaN`/`Infinity` tokens. The summary
side distinguishes **report coverage** (`reports_complete`: every row reported
cleanly — `rows` / `reported_rows` / `missing_rows` / `malformed_rows`) from
**breakdown completeness** (`complete`: reports complete AND every counter
anyone reported was reported by every row; `counter_rows` / `counter_complete`
per counter). A counter only some rows reported is a partial sum the console
marks `k/n` — a claude row beside a codex total-only row can never read as a
complete breakdown. `scope` is always `reported`: this is observed usage over
the tasks that reported it — not a billing total, and the console shows no cost
estimate. The console renders it as a TOKEN USAGE table under the result tiles
and per task in the expanded row, with a dash for every unknown counter.

## Agent IDE — a remote web IDE over codex on AgentCore

The fifth way to build an agent (parent task `10-19-coding-agent`): a member opens
a repo in the browser IDE (`/v2/coding`, `?view=repo&id=`), converses with a
codex agent that writes, runs and tests code inside an AgentCore microVM, and hands
the finished repo to Launchpad, which deploys it through the existing `byoc`
pipeline — the result is an ordinary governed Agent.

**Runtime (platform-owned, admin-installed).** One AgentCore Runtime per workspace,
`launchpad_coding_runtime`, image source in `backend/app/coding/image/` (zero-
dependency Node 22 server driving `codex app-server` over stdio JSON-RPC; codex
pinned, the Agent Toolkit for AWS plugins baked in). Models go through codex's
built-in `amazon-bedrock-runtime` provider (bedrock-runtime `/openai/v1`, SigV4 —
never Mantle); selectable models `Settings.coding_models`, reasoning effort per
turn. The execution role grants only ECR pull, its log group and
`bedrock:InvokeModel*` on the coding models + `project/default`: any shell in the
microVM can read those credentials, so the role has **no S3**. Session storage is
mounted at `/mnt/workspace` (1 GB, wiped by a runtime version update and after 14
idle days); codex's SQLite and dependency dirs live on local disk. Installed,
repaired and uninstalled only by an administrator (`/api/coding/runtime*`, a
`Job`), never on startup or on a read; resource keys `coding_runtime_*` (outside
`REQUIRED_RESOURCE_KEYS`).

**Action protocol.** Every backend call is one short `InvokeAgentRuntime`
(`app/coding/runtime_client.call`) carrying `{action, …}` and answering
`{ok, result | error{code}}`: `status`, `start_turn`, `interrupt`, `events`
(seq-numbered event-log long-poll, `compact` drops deltas of completed items),
`tree`, `read_file` (`rev: head | turn`), `write_file` (`expected_sha256`),
`changes`, `diff`, `snapshot` / `export` / `upload_file` (to a presigned PUT). Every app-server
notification lands in `.launchpad/events.jsonl` on session storage, so readers
fetch by seq and a reload, a reconnect or a backend restart loses nothing.

**Repos (`backend/app/coding/`).** Ledger `coding_repos` + `coding_turns`
(workspace-scoped). A repo is **private to its creator in every visibility
mode** — deliberately stricter than `resource_visibility`, since a repo is a
live shell plus code: ownership binds to the immutable principal
(`assistant/principal.principal_of` → `user:<id>` | `config-admin` |
`local-operator`), and every by-id route resolves through
`repos.repo_in` (foreign or missing → `404 coding.repo_not_found`).
Administrators list every repo (`?all=1`) and delete any, but never open an
IDE, read files, stream events or run a turn in someone else's repo. Each
repo owns one stable `runtimeSessionId` (64 chars, minted at create); repos
never share a session. A repo is the member's git working directory on the
coding runtime — not the organisational project. Inside the microVM it still
lives at `/mnt/workspace/project` (the runtime image's layout, unchanged for
installed runtimes and their snapshots); a ledger from before the rename
(`coding_projects`, `coding_turns.project_id`) is renamed in place on startup
(`core/db._migrate_coding_repo_rename`).

**Turns are asynchronous.** `POST …/turns` claims the repo (`UPDATE … WHERE
status='idle'` — one turn per repo), starts the turn in the microVM and returns
the `running` turn row at once; the turn runs under `/ping HealthyBusy` (≤ 8 h),
so leaving or reloading the page, or restarting the backend, never stops it.
`POST …/interrupt` interrupts it. A runtime turn the ledger lost (the backend
died between the runtime's answer and its commit) is adopted on the next start.

**Event stream (SSE).** `GET …/events?after=<seq>` (or `Last-Event-ID`) sends a
`status` frame, then each event-log record as `id: <seq>` / `event: event`,
keep-alive comments, `reconnect` after 25 min and `error {code, message}` on a
failure (`app/coding/stream.py`). **Idle mode:** once the replay is drained and
the runtime runs no turn, the stream polls the *ledger* every 2 s instead of the
runtime, so an open IDE tab never keeps an idle microVM (billed memory) alive past
its 15-minute idle timeout; it switches back to runtime long-polls when a new turn
is claimed. The route serves the stream as an async body whose blocking steps run
in worker threads while the idle pauses await on the event loop, so an open idle
tab holds none of the shared worker pool's threads.

**Settlement, durability.** The runtime appends `launchpad/turnUsage`
(or `launchpad/turnAbandoned`) right after a turn ends. Whoever sees it first —
the SSE proxy or the **watcher** (`app/coding/watcher.py`, one daemon thread,
every 15 s, `LAUNCHPAD_CODING_WATCHER_ENABLED`) — settles it with `UPDATE
coding_turns SET settled_at=…, usage=… WHERE … AND settled_at IS NULL`; only the
winner requests a snapshot. The turn row keeps codex's counts as reported
(`inputTokens` includes cached and cache-write tokens, `outputTokens` includes
reasoning). The watcher also abandons a turn the runtime no longer runs (its
session storage came back from an older snapshot) or one past the 8-hour
lifetime, releases dead start claims and snapshots saved files (30 s debounce).
Snapshots go to `s3://<artifacts>/coding/<ws>/<repo>/snapshots/<ts>.tgz`
through backend-minted presigned URLs (newest 3 kept); every action carries the
newest snapshot's presigned GET as `restore_url`, so a microVM whose session
storage came back empty restores the repo before anything else.

**Files.** The IDE reads the tree, file contents (working tree, `HEAD` or the
latest turn's baseline), changes and unified diffs, and saves files with an
optimistic `expected_sha256` (`409 coding.file_conflict` carries the current
hash); saves are refused while a turn runs. Paths are checked in the backend
(absolute, `..`, NUL, `.git`) before any call, and authoritatively by the
runtime's path guard.

**Downloads.** `POST …/download` (owner-only, allowed while a turn runs) hands the
browser one file (`{path}`, text or binary, ≤ 200 MB) or the whole repo as
`<repo>.zip` (no `path`: the deploy export's file set, ≤ 50 MB). The bytes never
pass through `InvokeAgentRuntime`: the backend mints a presigned PUT under
`coding/<ws>/<repo>/downloads/<ts>-<rand>/`, the runtime uploads the file
(`upload_file`) or the zip (`export`) to it, and the backend answers a presigned GET
that lives 5 minutes and carries `Content-Disposition: attachment` (an ASCII
`filename` plus the RFC 5987 UTF-8 `filename*`), so the browser saves it in place
(`app/coding/downloads.py`). Download objects older than 15 minutes are pruned at
the repo's next download; deleting the repo deletes its whole prefix.

**Deploy handoff.** `POST …/deploy` (`perm:agents.deploy`) snapshots the repo,
has the runtime zip its files to a presigned PUT, downloads it and stages it with
`byoc_uploads.stage_upload` (the same checks as a member upload), then creates a
`byoc` Agent stamped with the caller (`routers.agents.start_create`, the code
behind `POST /api/agents`) or re-publishes the repo's existing Agent
(`republish_agent`, the code behind redeploy). The export's size is checked
against the 250 MiB BYOC zip cap before the download (the microVM runs arbitrary
code). A root `Dockerfile` selects
`container_source`, otherwise `code_zip` (`main.py`). The repo's `AGENTS.md`
tells the coding agent the BYOC contract.

## The SQLite ledger and job/event model

**Uploaded task inputs.** A task's `files` map remains backward-compatible with inline text and
also accepts staged XLSX, PDF, PNG, JPEG, WebP, Markdown, plain-text and CSV inputs. The browser
uploads multipart bytes to a workspace-hashed, opaque-token staging area under `data/skill-lab/`
(24-hour TTL); task-set create/update verifies content class, limits, digest and ownership, then
commits deduplicated blobs
as `tasksets/<id>/assets/<sha256>` while JSON stores only stable metadata descriptors. Destination
paths are safe POSIX-relative rollout paths and never storage paths. Full-replacement updates
atomically preserve kept assets and drop omitted ones. Eval and train submission re-hash and copy
the selected JSON/assets into `jobs/<id>/inputs`, so queued/running work is immutable. Vendored
SkillOpt receives the explicit snapshot assets root and copies exact bytes into each rollout work
directory before the existing binary-safe S3 tar → AgentCore Runtime → output-tar transport.
**Taskgen input attachments.** AI task generation accepts the same staged uploads. Submission
resolves the tokens, snapshots bytes into `jobs/<id>/inputs/assets/<digest>` with a trusted
manifest at `inputs/attachments.json`, and the vendored generator materializes each document at
`data/<name>` inside its working directory — the only channel to a worker, since the remote exec
runner refuses host paths outside `work_dir`. The prompt states that the evaluated agent will see
the same relative path, so a generated question that names one stays true. A task may declare
`attachments: [<name>, …]`; the agent only ever names documents, and import maps each name to a
verified descriptor from that job's manifest, dropping the declaration once `files` carries it.
Generation bounds are tighter than the per-task asset limits (8 documents, 25 MiB aggregate)
because the agent decides how much of a document to read into context.

**Reviewing generated tasks before save.** A finished generation job writes an immutable
`out/generated_tasks.json`; the console renders it as an editable review (per row: `id`,
`question`, `rubric`, optional `task_type`, an exclude/restore toggle, and a read-only
"documents" chip) and **nothing is written until the operator saves** — as a new task set
(`POST …/import-taskset`) or appended to the expansion target (`POST …/apply-expansion`).
The save request carries only a *selection*: `tasks: [{index, id?, question?, rubric?,
task_type?}]`, where `index` is the row's position in `generated_tasks.json` and the four
fields are the only author edits accepted (`extra="forbid"`, strict non-coerced `index`, per-field
length caps, at most `MAX_TASKS_PER_SPLIT` rows). Rows absent from the selection are excluded;
an omitted field keeps the generated value; `task_type: ""` clears it. The server reconstructs
every other field — `files`, `target_skills`, the `attachments` declaration — from the job's own
artifacts, then runs the unchanged pipeline (derived-field stripping → snapshot attachment
binding → validator subprocess → staging swap), so a client can never plant a file descriptor,
a path, or a judge contract through the review. Bad selections (empty, out-of-range or repeated
index, duplicate edited ids) and, for expansion, edited ids that collide with **any** current
split are refused before any write and leave the job un-imported; a request without `tasks`
(or the legacy no-body apply) still saves every generated row verbatim. The drafts are
client-side only: a job switch resets them, a status poll or language change does not; while a
save is in flight the editor, reset and save controls are locked, and an outcome that arrives
after the operator switched jobs, left the surface, or returned to the same job is dropped (a
per-view generation counter, advanced on every job-effect run and cleanup) rather than navigating
the console; the server write itself stands. **After a
save the job page shows the generator's ORIGINAL output, read-only and labelled as such** — the
console persists no receipt of the selection; excluded/edited rows exist only in the task set,
which the page links to (`imported_taskset_id`, or the expansion target). Refused saves are
localized from the error code plus its structured `detail` (`{ids}` for duplicates/collisions,
`{reason, index, count}` for bad references), so the Chinese UI never shows the English server
sentence and never loses the ids it named.

Formats are trusted by content, not by extension: binaries must match their magic bytes (with
extra member/ratio/macro hardening for XLSX), and the text formats — which have no signature — must
decode as UTF-8, contain no NUL byte, and not carry a binary signature under a text extension.
Uploaded bytes are stored verbatim, so a BOM or CRLF survives content addressing untouched.
Limits are 32 uploaded files per upload/task, 25 MiB per file, 100 MiB per task, and 256
references/200 MiB unique bytes per task set; legacy inline text maps keep their historical count
and case-distinct-path behavior. `.agents`, `.claude`, `.codex`, `.git`, and `task.md` are reserved
rollout roots. A route-specific middleware rejects known oversized multipart `Content-Length` values
before Starlette parses the form, while streamed per-file enforcement remains authoritative. Chunked
bodies have no length at this layer, so production ingress/proxies should also enforce a whole-body
limit when a hard cap for chunked transfer is required.

Task-set update, delete, and job snapshot operations use a process-local lock keyed by task-set id.
The lock spans filesystem rollback names, snapshot verification, and the job-row commit, so deletion
either wins before submission or observes the committed job reference. This matches the supported
single-host/single-backend-process SQLite architecture; sharing `data/skill-lab/` across processes
would require replacing it with an inter-process/file lock. Delete first renames the tree, and a cleanup
failure compensates the ledger delete and restores the canonical tree so the operator receives a stable
error and can retry instead of leaking unowned assets.

State that is cheap and local lives in a SQLite ledger at `data/launchpad.db`
(`backend/app/models/ledger.py` + the evaluation/optimization models):

| Table | Holds |
|---|---|
| `agents` | Agent records — name, method, status, ARN, resource id, registry record id, version, spec |
| `deployments` | One row per deploy run — the five-stage array with per-stage status/detail/timestamps |
| `jobs` | Async work (type `deploy_agent`) — status + a JSONL `log` of stage events |
| `chat_sessions` | Chat playground sessions — turns, actor, last-seen, `ended_at` once the runtime session was explicitly ended |
| `mcp_tool_reviews` | Tool reviews of hosted MCP servers, Lambda tool targets and adopted external targets — the tool-list snapshot and its hash, the diff against the approved list, hints, status (`reviewing`/`approved`/`rejected`/`superseded`), source (`deploy`/`drift`/`baseline`/`rollback`), requester, decider, note, and the re-publish status after an approval |
| `mcp_servers` | One row per hosted MCP server — the endpoint its Gateway target calls (`DEFAULT` / `live`), the pinned `live_version`, the last health check and the last background operation (rollback, Cedar scaffold) |
| `lambda_tool_targets` | Console-created Lambda Gateway targets — Lambda ARN, target name and id, inline tool schema, status (`reviewing`/`publishing`/`live`/`failed`/`rejected`), creator |
| `external_mcp_targets` | Hand-made MCP-server Gateway targets brought under tool review — target id and name, endpoint, adopter, last drift check |
| `users` | Console accounts created by registration — username/email, pbkdf2 password hash, role, status (`pending`/`active`/`disabled`), `expires_at` (null until approval), last sign-in + sign-in count (the built-in admin is config-only and has no row) |
| `api_keys` | Public-API keys — sha256 hash + prefix (plaintext never stored), creator (`created_by` user id + `created_by_name`, NULL on legacy keys), `last_used_at` |
| `policy_decisions` | Governance decision log — principal, tool, ALLOW/DENY, reason |
| `policy_changes` | Immutable Gateway/Engine/Policy mutation snapshots, operation progress, override reasons, and rollback inputs |
| `eval_datasets` / `eval_runs` | Evaluation datasets (legacy prompts or devguide scenarios + description + last AWS-sync blob) and run state (scores or insight trees; window runs encode their scope as `dataset_name="window:<N>h"`) |
| `online_eval_configs` | Online evaluation configs the console created for an agent — identifiers only (config id/ARN/name, agent, service name, source log group); status, rule and evaluators are always read back from `GetOnlineEvaluationConfig`. Configs without a row are classified by name at read time (`exp_*`/`can_*` → experiment-owned, else external) |
| `experiments` | Optimization loop — stage + per-stage artifacts, resumable |
| `resource_creators` | Creator of an AWS-only resource made through the console — (workspace, `kind` = `knowledge_base` \| `evaluator`, AWS resource id) → `created_by` / `created_by_name`; removed by the console's delete |

**Resource creators.** Every user-managed resource records the console principal
that created it, with the API-key semantics: `created_by` is the creator's
`users.id` (the ownership key; NULL for the config admin and the auth-disabled
operator, who have no `users` row) and `created_by_name` the username at creation
(display only). The ledger-backed kinds carry both columns on their own row —
`eval_datasets`, `eval_pipelines`, `eval_runs`, `online_eval_configs`,
`experiments`, `runtime_canaries`, `skill_lab_jobs`, `skill_lab_tasksets` and
`managed_memories`, and `agents` (whose older `owner` column stays a provenance
label) — while knowledge bases and custom evaluators, which AWS alone
stores, use `resource_creators`. One module, `app/services/creators.py`, stamps rows
(`Creator.of_request` in the routers; service functions take a keyword-only
`creator`) and renders the two response fields, so every list and detail response
carries them. In-process paths act for a person: the evaluation-asset materializer
stamps the plan's approver on the dataset and evaluators it creates, a pipeline run
stamps whoever started it on the dataset it creates, and an online report run the
caller of `/reports`. Paths with no human principal — Skill Lab samples, the
bootstrap memory, experiment/canary-owned online configs, discovered resources —
record nothing. Legacy rows are never backfilled (both fields NULL, the console
shows "—"), and no request body carries the creator. An adopted memory keeps the
adopting admin's name but no `created_by`: it pre-existed in AWS. `created_by` is
also the key workspace visibility isolation filters on (*Workspaces — Member
resource visibility*).

File-based SQLite uses SQLAlchemy `NullPool`. Every request-owned session
already closes deterministically, so retaining the SQLAlchemy 2 default
`QueuePool(5+10)` adds an artificial concurrency ceiling: a burst of sync
console requests can otherwise park every worker waiting up to 30 seconds for a
connection and make even health checks appear dead. The auth middleware also
caches its resolved identity on the request so route-policy enforcement does
not open a second ledger session for the same request.

**Job/event model.** Creating an agent returns `202` with a `job_id`. The
deploy job runs on a background thread, appending one JSONL event per stage
transition to `Job.log`; `GET /api/jobs/{id}` returns those events and
`GET /api/agents/{id}` returns the `Deployment.stages` array. The agent moves
`deploying → active` (or `failed`) as the job finishes. A failure raised outside
any stage (the job's workspace row is gone, the method is not registered, ledger
rows are missing) lands on the agent the same way: `Job`, `Deployment` and
`Agent` are all marked `failed` with the error, an `error` event is appended to
`Job.log`, and the pipeline's `launchpad.deploy` logger reports every stage or
job failure to the process log. Authoritative resource
state (runtime status, registry record status, eval/trace data) always lives in
AWS; the ledger holds identifiers and derived progress only.

## Console layout breakpoints

The console is desktop-first but has two deliberate responsive tiers in
`frontend/src/theme/app.css`. Below **1180 px** every two-column grid
(`.grid-2`, `.reg-grid`, `.chat-grid`, `.eval-grid`, the governance/observability
grids and `.mem-grid-3`) collapses to one column and its children get
`min-width:0`, so a wide child (a `<pre>` curl block, a long key/value row)
scrolls inside its panel instead of widening the track. Below **720 px** the
sidebar becomes a horizontal nav strip, the topbar drops its identity text, and
the page must never scroll horizontally as a whole: wide content scrolls inside
its own container or wraps. Tables are the main source of width, so the shared
`DataTable` component and every raw `<table>` that is not a direct child of
`.panel` sit inside a `.table-scroll` wrapper (`overflow-x:auto;min-width:0`,
a no-op when the table fits); the `.panel:has(> table)` rule covers tables
rendered directly under a panel. Toolbars (`.tabs`, `.tabs-actions`), filter
pickers (`.filters .fsel`, `.fsearch`), the creation stepper (`.steps`) and list
rows (`.histrow`) wrap or clamp to the panel width at that breakpoint. New
pages should reuse `DataTable` or the `.table-scroll` wrapper rather than
setting per-page widths; the ≥ 1180 px layout is unaffected by either tier.

## Error envelope and AWS `ClientError` mapping

Every error leaves the backend as `{code, message, detail}` through the handlers
registered in `app/core/errors.register_error_handlers`; the console translates
`code` through the `apiErrors.*` i18n block (`localizedMessage` in `lib/api.ts`)
and falls back to `message`. Services that anticipate a failure raise `AppError`
with their own code (`kb.not_found`, `agent.not_found`, `memory.unavailable`) and
those always win, because they are raised before any `ClientError` can escape.

An AWS `ClientError` nobody anticipated — a wrong id in a URL, an IAM gap, a
throttle — detonates at whichever route was signing the request, so it is mapped
in one place rather than per route: the global `ClientError` handler answers
`ResourceNotFoundException` → 404 `aws.not_found`, `ValidationException` → 400
`aws.validation`, `AccessDeniedException` / `UnauthorizedException` → 403
`aws.access_denied`, `ThrottlingException` / `TooManyRequestsException` /
`ServiceQuotaExceededException` → 429 `aws.throttled`, and `ConflictException` /
`ResourceInUseException` / `RetryableConflictException` → 409 `aws.conflict`, with the botocore
`An error occurred (…) when calling the … operation:` prefix stripped from
`message` and `{aws_error_code, operation}` in `detail`. The mapping is
deliberately a closed list (`AWS_ERROR_MAP`): any other code is re-raised and stays
an unhandled 500 with the traceback in the log, so a genuinely unexpected AWS
failure is still loud. A failed cross-account `AssumeRole` is checked first and
keeps its 502 `workspace.assume_role_failed` diagnostic. The Memory routers'
`memory.unavailable` wrapper lets a mapped `ClientError` through to this handler,
so an unknown actor toasts the localized "not found" copy instead of raw boto
text. `tests/test_errors_aws.py` pins the table; do not add per-route
`except ClientError` blocks for these codes.

## Console failure states (backend unreachable)

The console never reports an empty account it could not read. Two rules are
load-bearing:

- **The topbar health chip is bound to `/api/health`.** `useHealth` probes on
  mount, every 30 s, and immediately on `window` `online` / `focus`, and returns
  `{ health, status: "loading" | "ok" | "down", refresh }`. `Topbar` renders the
  green LED with `topbar.allSystemsGo` only while `status === "ok"`; a probe that
  failed (no answer, 5xx, non-JSON body from the dev proxy) or has not answered yet
  renders the same-sized chip with a `crit` LED and `topbar.backendDown`. The last
  successful payload is kept through an outage so the region / account chips do not
  blank while the backend restarts.
- **A failed list load renders the shared error state, not the empty copy.**
  `components/LoadError.tsx` (also reachable through `DataTable`'s `error` /
  `onRetry` props) is the one "Failed to load: … · RETRY" block; Overview (tiles,
  launch feed, health rows), Registry, Knowledge Bases, Chat (agent picker),
  Evaluation runs and Experiments all use it, matching the older Observability /
  Governance blocks. "Create your first …" / "NO RECORDS" copy is only rendered
  after a 200 answered with an empty list; rows that loaded once stay visible
  through a later failed poll, and Retry re-issues the fetch. Pages that fetch by
  path use `getJson` / `responseMessage` / `errorMessage` from `lib/api.ts` so the
  message follows the `apiErrors.*` localisation rules (`apiErrors.network` for a
  request that never got an HTTP answer).

## Stale deep links say the resource is gone

A deep link whose id no longer resolves never falls back silently. The shared
`components/StaleLink.tsx` is the one notice ("`<Kind>` `<id>` no longer exists in
this workspace — pick one from the table below.", `staleLink.*`, dismissible), and
`components/useStaleParam.ts` is the hook that pairs with it: the caller passes the
param's current value and its own verdict — true only once the list has loaded
without the id, or the detail fetch answered 4xx (`aws.not_found` /
`aws.validation` / `aws.access_denied` from the `ClientError` mapping above) — and
the hook captures the id for the notice and strips the param once through
`setSearchParams(..., { replace: true })`, so the same link never re-fires and the
page then reads as a plain visit. A list that failed to load is *not* a verdict:
`LoadError` owns that state and the param is kept for the retry. Surfaces wired:
Evaluation `?view=datasets&ds=` (local rows after the local list, `cloud:` rows
after the cloud list), `?view=evaluators&ev=`, `?view=online&oe=`,
`?view=experiment&exp=`, Chat `?agent=` (plus its companion `?session=`, dropped
too), and Knowledge Bases `?view=detail&kb=` — a missing `kb` is reported the same
way (`staleLink.bodyMissing`) instead of a permanent LOADING. Chat is the one
surface that must not pick a substitute: the picker stays on the
`chatPage.pickAgent` placeholder (`value=""`) until the user chooses, because an
auto-selected agent would silently receive the next prompt. Valid links keep
selecting the row / agent exactly as before.

## Disabled primary actions explain what is missing

A form's primary action is never *just* dimmed. The shared `components/Btn.tsx`
takes an optional `disabledReason`; while the button is `disabled` and a reason is
set, it renders `title={reason}` plus a sibling `.btn-hint` (mono, `--ink-3`, the
weight of `.dim` helper text) that the button points at through
`aria-describedby`. When the button is enabled — or no reason is given — no hint
element exists. The reason is derived from the *same* predicates that compute
`disabled`, read in order so the first unmet one is named; the prop never changes
*when* a button is disabled, only what the console says about it. Every reason is
an i18n key (en + zh-CN). Forms wired today: Registry Register (`▲ REGISTER` —
name rule / MCP URL / SKILL.md), Registry Edit (`▲ SAVE` — no changes / invalid
bundle), Knowledge Base Create (`▲ CREATE` — name rule / no files / no bucket),
Strands Studio (`▲ Publish` — no nodes; the publish dialog's name rule), Online
Evaluation Create (`▸ CREATE` — no agent / no evaluator / no insight) and the
Workspace detail `RUN BOOTSTRAP` (hub workspace / already running / already
READY). Busy states (`saving`, `busy`) deliberately carry no reason: the label
already says what is happening.

## Local process topology

`./start.py` starts the two platform processes, waits for every HTTP health
check, and records process ownership plus logs under `.run/`. `./stop.sh`
gracefully stops only those recorded process groups. The default uses
development servers; `./start.py --prod` builds the platform frontend and
serves its production bundle without backend auto-reload. `bash scripts/dev.sh`
(`make dev`) remains the foreground, terminal-attached alternative.

| Service | Port | Override |
|---|---|---|
| platform backend | 8000 | `PLATFORM_API_PORT` |
| platform frontend | 5173 | `PLATFORM_UI_PORT` |

The lifecycle script fails fast when a configured port is occupied. Development
mode binds both services to loopback by default; production mode binds both
services to `0.0.0.0`. `LAUNCHPAD_HOST` and `LAUNCHPAD_API_HOST` override those
bindings.

The Strands Studio canvas is part of the platform console at `/create/studio`;
the former vendored standalone app (`apps/studio/`) was removed because its
backend executed caller-supplied code without authentication. See
[studio-integration.md](studio-integration.md).
