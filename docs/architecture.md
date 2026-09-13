# Architecture / 架构

AgentCore Launchpad is a thin, opinionated platform layer over Amazon Bedrock
AgentCore. Every feature in the console maps to a real AgentCore service and a
real resource in your account — the platform's job is to give those services a
unified create → deploy → invoke → observe experience, not to reimplement them.

中文版: [architecture.zh-CN.md](architecture.zh-CN.md)

## System diagram

```
 Browser
 ┌─────────────────────────────┐        ┌──────────────────────────┐
 │ Platform console  :5173     │        │ Strands Studio UI  :5273 │
 │  Overview · Create · Chat   │        │  drag-and-drop canvas    │
 │  Registry · Governance ·    │        │  (方式C, vendored)       │
 │  Evaluation                 │        └────────────┬─────────────┘
 └──────────────┬──────────────┘            /api,/ws │  /launchpad-api
                │ /api  /v1                           │  (→ platform /api)
                ▼                                     ▼
 ┌─────────────────────────────┐        ┌──────────────────────────┐
 │ Platform backend  :8000     │◀───────│ Studio backend    :8100  │
 │  FastAPI                    │ deploy  │  FastAPI (local run,     │
 │  · deploy pipeline          │ via     │  chat, exec history)     │
 │  · invoke chain (/api,/v1)  │ pipeline└──────────────────────────┘
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
| **Identity** | Token vault backing the gateway — an OAuth2 provider (agent outbound auth) and an API-key provider. |
| **Registry** | The GA `agent-registry` service hosts `launchpad-registry`, cataloguing A2A agents, MCP servers, and AGENT_SKILLS. `services/agentcore/registry.py` translates the GA `AGENT/MCP/SKILL` and `data/dataSchemaVersion` model into the stable Launchpad descriptor contract; other AgentCore services remain under `bedrock-agentcore`. GA uniqueness is `(name, recordVersion)`, so newly created records use type-qualified initial versions (`1.0.0-a2a`, `1.0.0-mcp`, `1.0.0-skill`) and content edits preserve the suffix. Every deploy auto-creates and submits an A2A record when Registry is available. In accounts whose SCP/IAM policy denies Registry setup, bootstrap records the capability as unavailable, Registry-only APIs return 503, and the deploy pipeline skips only the register stage; Runtime/Harness deployment remains usable. Governance can import one existing AgentCore Gateway as one MCP record containing the Gateway endpoint and its complete discovered tool catalog; legacy per-target records remain until an explicit retirement after the Gateway record is APPROVED. Registry approval controls catalog visibility, not Gateway authorization. `GET /api/registry/attachables` reports catalog status separately from Harness attachability and resolves Gateway auth server-side. For an A2A record owned by a deployed Launchpad A2A agent, the Registry drawer's LIVE CARD reads the card the runtime serves right now (`GET /api/registry/records/{id}/live-agent-card` → data-plane `GetAgentCard` on the ledger's `Agent.arn`, the session AWS opens is ended at once) and diffs it against the record's stored card; the live card is never persisted — AWS stays the source of truth. Both cards read their `version` from one platform constant, `A2A_CARD_VERSION` in `services/agentcore/registry.py`: the A2A runtime template passes it to Strands `A2AServer(version=...)` at package time and `build_a2a_card` stamps it on the record at register time, so the two agree by construction. It is deliberately **not** the AgentCore runtime version (`Agent.version`, shown in VERSIONS & ENDPOINTS) — that is assigned by Create/UpdateAgentRuntime only after the template has been rendered, so the card cannot carry it. A2A agents published before this constant existed still serve Strands' default `0.0.1` against a record `version` of `1`; the diff flags them until their next re-publish (re-render + re-register), which converges both sides. The Registry page has two views over the same registry: the **publisher list** (`GET /api/registry/records` → control-plane `ListRegistryRecords`, every record in every state) and the **consumer view** (`?view=discoverable`, `GET /api/registry/records/discoverable` → data-plane `ListDiscoverableRegistryRecords`, paginated to completion) — what a consumer or agent with data-plane access actually discovers. Discovery summaries carry no `descriptors`; opening a row reads the full record. Once the consumer view has been fetched in a page session, every control-plane record absent from it is chipped NOT DISCOVERABLE (DRAFT / PENDING_APPROVAL / REJECTED / DEPRECATED are the expected cases) — the diff, not the list, is the point. |
| **Policy** | Governance discovers existing MCP Gateways live, persists opt-in management through Launchpad-owned Gateway tags, and manages one attached Policy Engine plus Cedar policies. Initial Engine attachment mode is operator-selected (`ENFORCE` by default, `LOG_ONLY` available); new policies still start `LOG_ONLY`. A Gateway that references an Engine deleted out-of-band is surfaced as an explicit dangling state — reads keep the stale ARN visible, policy mutations return 409, and create-and-attach replaces the reference. ACTIVE edits create LOG_ONLY candidates, promotion and rollback use conservative ordering, and later Gateway transitions to `ENFORCE` require evidence or a typed zero-evidence override. Authenticated Chat calls to `launchpad-gw` use a server-minted Cognito user JWT, so Cedar `OAuthUser` tags such as `username` and `cognito:groups` reflect the signed-in console identity instead of the agent's M2M client. ZIP Runtime receives it in the sensitive invoke payload; Harness receives an invocation-scoped authenticated `remote_mcp` tool. Public API/evaluation traffic and an auth-disabled local console remain M2M. Every mutation is journaled locally while AWS remains the source of current state. |
| **Evaluation** | Real `StartBatchEvaluation` / insights over CloudWatch traces. A run's scope is exactly one of: a **dataset** (replay items — multi-turn scenarios replay sequentially in one session), explicit **session ids**, or a **time window** (`lookback_hours` 1–336 — passive: no new invocations, `filterConfig.timeRange` over existing traffic). 14 general prompt-template evaluators (12 trace/session plus 2 ordinary tool-call), 2 skill `TOOL_CALL` prompt-template evaluators, and 3 ground-truth-only programmatic `Builtin.Trajectory*Match` session matchers (selectable only on dataset runs whose scenarios define `expected_trajectory`) plus **custom evaluators** with full CRUD on the `?view=evaluators` sub-page in three definitions — **LLM-as-a-judge** (`llmAsAJudge`: instructions with placeholders, numerical rating scale, Bedrock judge model), **derived** (`derived`: a Builtin/ThirdParty base evaluator's prompt on a chosen model) and **code-based** (`codeBased.lambdaConfig`: a Lambda function ARN in the workspace Region plus a 1–300 s timeout, default 60; no instructions, scale or model). Every definition needs `level` on `CreateEvaluator`. The console detail projection carries `definition: judge|derived|code`; `UpdateEvaluator` is a full-config replace, so an update payload must be of the evaluator's own kind — a judge payload against a code-based evaluator (or any other cross-kind pair) is refused with `evaluator.definition_mismatch` instead of silently converting it. A code-based evaluator's Lambda receives `{schemaVersion, evaluatorId, evaluatorName, evaluationLevel, evaluationInput.sessionSpans, evaluationReferenceInputs, evaluationTarget}` and answers `{label, value?, explanation?}` or `{errorCode, errorMessage}` (300 s / 6 MB limits); the console manages **no IAM** for it — the evaluation execution role that batch/online runs pass as `evaluationExecutionRoleArn` needs `lambda:InvokeFunction` + `lambda:GetFunction` on the function and the function's resource policy must allow `bedrock-agentcore.amazonaws.com`, both stated as a hint under the ARN field. Code-based evaluators are selectable wherever custom evaluators are (batch runs, experiments, online configs). Insights runs pick a subset of the three analysis types (failure analysis / user intent / execution summary). Datasets live in SQLite as devguide scenarios (`?view=datasets` sub-page: scenario editor, JSON/JSONL import) and sync one-way to AWS Dataset resources (`AGENTCORE_EVALUATION_PREDEFINED_V1`): the first sync creates the dataset (`CreateDataset`), every later sync edits that dataset's **DRAFT** in place (`ListDatasetExamples` → `DeleteDatasetExamples` → `AddDatasetExamples`, each polled through `UPDATING` to `ACTIVE`) so the dataset id and its published versions survive; **PUBLISH VERSION** (`CreateDatasetVersion`) snapshots the draft as an immutable numbered version and flips `draftStatus` from `MODIFIED` to `UNMODIFIED`. The row's `cloud` blob caches id/ARN/status plus `draft_status`, `example_count` and the version list (`ListDatasetVersions`); cloud-only datasets show the same read-only and can publish too, and a single published version can be deleted (`DeleteDataset` with `datasetVersion`). A cloud-dataset run may **pin a published version** (`dataset_version`, validated against `ListDatasetVersions` before the run row exists; `GetDataset` and `ListDatasetExamples` then read that snapshot so the replayed scenarios and ground truth are exactly the version's); the draft is the default, and the pinned version is stored on the run and shown as `· v<N>` in the runs list. A recorded copy that AWS no longer knows (`ResourceNotFoundException`) or that was deleted through the console is re-created on the next sync; scenario ground truth (assertions / expected responses / expected trajectory) is injected into batch runs via `evaluationMetadata.sessionMetadata`. Runs execute through a bounded-concurrency queue — up to `eval_max_concurrent_runs` at once (default 3, capped at 5 to match the AWS active-batch-evaluations account quota); excess runs queue instead of failing. An operator can **stop** any active run from the Runs page (`POST /api/eval/runs/{id}/stop`): a run whose batch exists on AWS is stopped with `StopBatchEvaluation` (STOPPING → STOPPED — the sessions already judged keep their results, which the poller records as partial scores), a run still queued is cancelled locally before it ever reaches AWS, and a run replaying its dataset stops between prompts without calling `StartBatchEvaluation`. All three end in the terminal ledger status `stopped` (never `failed`) with the reason "stopped by operator"; `DeleteBatchEvaluation` is not exposed. The run row stores only the per-evaluator **averages** (`evaluatorSummaries.statistics.averageScore`); the **judge's explanation of every score** lives in the batch's own results log stream (`GetBatchEvaluation.outputConfig.cloudWatchConfig` → `run-<batchId>` in `/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default`, `gen_ai.evaluation.result` records), which the Runs page reads on demand for the selected terminal run (`GET /api/eval/runs/{id}/results`, never persisted) and renders as a SESSION RESULTS panel — per session, one row per judgement (evaluator, level, score, label, expandable explanation; a span-level evaluator yields one row per tool call) with a link to the Observability session detail. **Online evaluation** (`?view=online`): one AgentCore `OnlineEvaluationConfig` per agent + evaluator set scores a sampled share (0.01–100 %) of live sessions after a session-idle timeout, no new invocations; results land in `/aws/bedrock-agentcore/evaluations/results/<configId>` (also EMF metrics under `Bedrock-AgentCore/Evaluations`) and the console aggregates them with Logs Insights (per-evaluator mean / labels / trend / recent records with judge explanations). The page lists **every** config in the workspace account classified by owner — `agent` (Launchpad-created, full control), `experiment` (`exp_*`/`can_*` arms, read-only), `external` (pause/resume/delete only). Update always sends the complete `rule` because AWS replaces it wholesale; create refuses a never-invoked agent (AWS validates the log group exists). A config runs in one of two **modes**: `scores` (evaluators) or `insights` (1–3 insight types + optional DAILY/WEEKLY/MONTHLY clustering — AWS forbids both on one config); insights configs produce **reports** (batch evaluations sourced from the config: AWS-scheduled on the clustering cadence, or RUN REPORT NOW from the console through the run queue), attributed via `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource` and rendered with the same insight-cluster trees as the Runs page; a report covers only the sessions the config sampled. Online scores also surface where sessions are looked at: the Observability session detail carries an ONLINE EVALUATION block (every config's result records for that session, owner-classified, fail-soft — a results-query failure never hides traces) and the Overview has an **ONLINE QUALITY · 24h** tile (polarity-normalised, count-weighted mean over the workspace's agent-owned configs, 120 s cache, no AWS call while no config exists). Both read all results log groups at once through `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])`. A third, **on-demand** mode scores one session synchronously through the data-plane `Evaluate` API from the Observability session detail (SCORE NOW: ≤5 evaluators, ≤10 results per call, nothing persisted) — for probing a custom evaluator or one suspicious session, where a batch run would be the durable answer. |
| **Optimization** | Recommendations → configuration bundles → gateway A/B (config-bundle 50/50) → target-based canary → verdict → promote → cleanup. The system-prompt recommendation is **pluggable**: the AgentCore job by default, or a 3rd-party provider (`gepa_lite` — one GEPA-style reflective round over the pinned evaluation run's per-session judge scores, explanations and transcripts, on an operator-chosen Bedrock Converse model) that bypasses `StartRecommendation` and its content filter; its prompt still becomes the treatment configuration bundle, so the A/B measures it like any other. Dataset replay at the traffic stage posts prompts concurrently (at most `TRAFFIC_MAX_CONCURRENCY` = 10 in flight, `LAUNCHPAD_TRAFFIC_CONCURRENCY` dials it down); one prompt is one session is one arm, so the split is unaffected. |
| **Observability** | CloudWatch Logs Insights over both telemetry layouts: legacy traces in `aws/spans`, and unified traces/logs/prompts in `/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint>`. Span records are rendered as a per-session rail. |
| **Builtin Tools** | Code Interpreter (`aws.codeinterpreter.v1`) runs operator-editable Python in an inline execution demo. Browser accepts an operator-editable navigation URL, starts a five-minute `1280x720` session, and returns a server-generated SigV4 Live View URL rendered by the official `BrowserLiveView` DCV component. The operator can use the managed browser or select an existing READY custom Browser with `browserSigning.enabled` for Web Bot Auth, restore an existing Browser Profile, and explicitly opt into saving Profile state before stop. Explicit stop and backend expiry both release retained sessions. |

## The unified five-stage deploy pipeline

All three creation methods converge into the same ordered stages, defined in
`backend/app/deployer/pipeline.py`:

```
generate → package → provision → deploy → register
```

Each method contributes one callable per stage (or omits it to skip). Stage
progress is persisted on the `Deployment` row and mirrored as JSONL events into
the `Job` log, so a restarted backend resumes from the first non-succeeded
stage (`resume_pending_jobs()` runs on startup).

| Stage | 方式B — harness | zip_runtime / 方式C — studio | 方式A — container |
|---|---|---|---|
| **generate** | Build `CreateHarness` request from the AgentSpec | Render the Strands template (studio: adapt user code verbatim) | Assemble ARM64 build context (Dockerfile + `main.py` + `.claude` scaffold) |
| **package** | *skipped* (no artifact) | resolve → hashed lock → `--require-hashes` install of ARM64 wheels → zip → S3 | zip context → S3 → CodeBuild (docker build+push) → ECR → resolve digest → scan gate |
| **provision** | Reuse the shared execution role | Reuse the shared execution role | Reuse the shared execution role |
| **deploy** | `CreateHarness` + poll READY | `CreateAgentRuntime` + poll READY | `CreateAgentRuntime(containerConfiguration)` + poll READY |
| **register** | A2A registry record, auto-submitted; skipped when Registry was explicitly unavailable at bootstrap | A2A registry record, auto-submitted; skipped when Registry was explicitly unavailable at bootstrap | A2A registry record, auto-submitted; skipped when Registry was explicitly unavailable at bootstrap |

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
deploy target (aarch64, Python 3.13, named once in `zip_runtime.py` so the resolve
and the install cannot disagree) with `--only-binary=:all:`, then installs those
same wheel-only candidates with `--require-hashes`. Without the matching binary
constraint, the resolver can lock an sdist-only release that the Runtime's
ARM64/manylinux2014 binary-only install rejects. A substituted or re-uploaded
distribution fails the build. The lock ships inside the zip as
`requirements.lock`, so the artifact carries its own bill of materials. There
is deliberately no fallback: a resolve failure fails the stage.

Caller-supplied `spec.requirements` must additionally be pinned at *schema*
validation (`app/schemas/requirements.py`), so the console rejects a range before a
build starts. The platform's own lists keep their ranges — the
`MANTLE_EXTRA_REQUIREMENTS` comment explains that pip is meant to intersect two
specs for the same project — and the lock is what makes the resolved set
reproducible. Harness conversion is the one place the platform derives
requirements from somewhere else (the source Harness's `pyproject.toml`), so it
resolves those ranges to pins rather than being exempted from the rule.

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

### Creation entrances

The `/create` picker shows four cards, in this order:

| # | Card | `AgentSpec.method` | What it is |
|---|---|---|---|
| 1 | **Managed Harness** | `harness` | 方式B — declarative, no build artifact |
| 2 | **Strands Studio** | `zip_runtime` | 方式C — Strands template on the zip fast path; the card's nested link opens the `/create/studio` canvas, which deploys as method `studio` |
| 3 | **Other Agent SDK** | `container` | 方式A — bring your own agent SDK, packaged as an ARM64 container via CodeBuild |
| 4 | **Discover existing runtimes and harnesses** | — | not a deploy method (see below) |

The third card is a **category**, not one SDK. `AgentSpec.agent_sdk` records
which SDK a container agent packages, and the wizard exposes it as a
second-level choice on the configure step. It is a single-member `Literal`
(`claude_agent_sdk`) that defaults to that member, so container specs written
before the field existed read back unambiguously and adding a second SDK needs
no stored-spec migration. There is deliberately **no dispatch** on the field yet:
`app/deployer/container.py` and `app/templates/claude_sdk_agent/` stay
unconditional until the category has a second member.

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
`clear: ["max_tokens" | "reasoning_effort"]` sends nothing for those two knobs
(JSON `null` means "unchanged", never "clear"). Unknown members — `name`,
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
and not a spend cap; `reasoning_effort` (`low | medium | high`) is accepted **only
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
defaults** are `us.openai.gpt-5.6-sol` (the US cross-region inference profile, native
Bedrock/Converse — the per-agent role authorizes the profile plus the underlying
foundation model), `max_tokens: 65536` and `reasoning_effort: "high"`; the platform
`DEFAULT_MODEL_ID` and the ordinary wizard defaults are unchanged. Stored rows are
**not migrated**: a preset installed by an earlier build keeps its model, prompt and
absent knobs through reads, repairs and bundle updates until an administrator saves
an explicit change (or `reset`) — `options_from_spec` recovers every editable member
exactly as stored, including a system prompt that differs from this build's constant,
so a prompt change in the catalogue reaches an installed preset only through an
explicit `reset: ["system_prompt"]` (the console offers USE THIS BUILD'S PROMPT).
The console's **CONFIGURE** dialog (System presets panel; members get VIEW SETTINGS
with the same fields read-only, and `POST …/install` stays `403` for them whatever
`perm:agents.*` they hold) prefills the stored values, shows which differ from the
defaults, validates bounds client-side, sends only the changed members after an
explicit confirm, and consumes the `202`/`200`/`409`/`422` outcome like the install
button does; cancel posts nothing; the dialog cannot be dismissed while a save is in
flight (the panel owns the request and its completion, so an accepted `202` always
lands as DEPLOYING + job); and every read and save of this surface — panel polls,
install/repair/uninstall, the editor's KB catalog and save — pins the workspace the
panel is displaying as an explicit `X-Workspace` header, so another tab switching the
shared selection can never redirect them to a different workspace (a same-tab switch
still closes the dialog). Persistent memory stays disabled:
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

**Constrained tool surface.** The harness exposes `shell` and `file_operations` to
every session unless `allowedTools` restricts them, so `AgentSpec.allowed_tools`
(new, harness-only, `None` = API default for every existing agent; each entry
1–64 chars matching the service model's `*|@?name(/tool)?`) maps to the request's
`allowedTools`, and the preset sends `["file_*", "@aws_knowledge"]`: the file tools
its skill needs, the public AWS Knowledge MCP server
(`https://knowledge-mcp.global.api.aws`, a `remote_mcp` tool, no credential), and no
shell. When knowledge bases are mounted, the deployer appends `@<kb gateway tool
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
wizard round-trips a stored `allowed_tools` untouched on edit/re-publish (typed
in `AgentSpecInput`), so a console re-publish can never widen an agent's tool
surface — note that omitting `allowedTools` on UpdateHarness keeps the live
restriction per the service model, so the risk was lost ledger intent on a later
recreate, not immediate widening.

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
the platform's `DEFAULT_MODEL_ID`) and the optional knowledge bases. They are
**API-only** — the console panel installs with `{}` (defaults, or the stored
choices on repair) and says so; there is no model/KB field in the panel. The
knowledge-base references are shape-validated at request time and **verified in
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
agent, never creates a knowledge base, gateway or evaluator, and never runs anything
by itself.

**Scope of what is supported here.** The proposal may name the agent, pick a model
(`model_id` + `model_source`), write the system prompt, choose memory flags, iteration
and timeout controls, and reference **existing** workspace resources by catalog key:
APPROVED registry MCP records (`gateway:<name>` / `mcp:<name>`), APPROVED registry
`AGENT_SKILLS` records (S3 skill paths) and ACTIVE managed knowledge bases. Painpoint →
metric → golden-test tables, evaluator recommendations, assumptions and *manual tasks*
are carried as **solution content** on the revision and rendered for review; the
console labels them "not created here" and nothing provisions them. No DOCX/PDF
upload, no Word/draw.io export.

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
explicit "no reply" marker. One **final** budget (`MAX_REPLAY_CHARS` = 160k
characters, ≤ 12 turns) covers preamble + catalog + replayed turns + the current
message: the newest turns that fit are kept, the number of omitted older turns is
disclosed in the preamble and in the `meta` event, and the current message is never
truncated — one that cannot fit (or exceeds 100k chars / 300k bytes) is refused with
`413 assistant.prompt_too_large` before any claim. The server-composed protocol
preamble (rules + the catalog keys + which memory modes exist here) rides on the
first user message; the harness request carries no `systemPrompt`, `tools` or
`model` override. Nothing private is written to shared long-term memory. Whether the model
follows the replay/protocol faithfully is part of the **pending live smoke**.

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
ARNs, S3 prefixes or roles can pass. `memory` is `"disabled"` or `"workspace"` — the
only two states the Harness API can enforce (`{"disabled": {}}`, or the workspace's
existing shared AgentCore Memory with every strategy it carries); a "short-term
only" opt-out is not representable and is not offered. References are validated
against the conversation's catalog snapshot **including prerequisites**: a gateway
tool needs a resolved gateway ARN + outbound-auth identity, a skill needs readable
bundle content, a mounted KB needs the workspace's **existing** ready KB gateway +
OAuth provider (mounting a KB where none exists is manual work — this flow never
creates a gateway), `workspace` memory needs the shared `memory_arn`; reserved preset
names and the `launchpad-`/`harness-`/`system-` prefixes are refused. `to_agent_spec`
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
stored before it existed; Mantle is a *form* default, chosen per method in the
console (`MODEL_SOURCE_BY_METHOD` in `frontend/src/pages/CreateAgent.tsx`). The
console's model catalog lives in `frontend/src/lib/models.ts`.

**Harness (方式B)** — both sources ride the **same** `bedrockModelConfig` branch
of the `HarnessModelConfiguration` union and differ only in `apiFormat`:
`responses` for Mantle, `converse_stream` for Bedrock
(`app/deployer/harness.py`). The keyed union branches (`openAiModelConfig` /
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

A2A zip agents render from a different template with no Mantle branch, so the
wizard pins them to `bedrock` and hides the selector. The Other Agent SDK
(container) entrance is likewise pinned to `bedrock` and offered only Claude ids,
because its one SDK today — the Claude Agent SDK — cannot drive anything else;
the wizard shows it the SDK choice in place of the Model source control.

### Existing Runtime and Harness discovery

`/create?view=discover` is an onboarding path alongside the three creation
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
Launchpad deploy minted (`Agent.version`), so the agent detail on `/create`
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
non-A/B-able agent. The emitted `zip_runtime` spec carries the harness's gateway
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
every member. The studio local-exec routes remain safe in production through their
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

Data is **not** partitioned per user: every authenticated account sees — and,
since the opening, can mutate — the same agents, records, datasets, knowledge
bases and gateways. Revoking the `perm:*` keys restores a partial guardrail for
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
  (`…___Retrieve`, `agentic-…___AgenticRetrieveStream`). The gateway is created
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
| `users` | Console accounts created by registration — username/email, pbkdf2 password hash, role, status (`pending`/`active`/`disabled`), `expires_at` (null until approval), last sign-in + sign-in count (the built-in admin is config-only and has no row) |
| `api_keys` | Public-API keys — sha256 hash + prefix (plaintext never stored) |
| `policy_decisions` | Governance decision log — principal, tool, ALLOW/DENY, reason |
| `policy_changes` | Immutable Gateway/Engine/Policy mutation snapshots, operation progress, override reasons, and rollback inputs |
| `eval_datasets` / `eval_runs` | Evaluation datasets (legacy prompts or devguide scenarios + description + last AWS-sync blob) and run state (scores or insight trees; window runs encode their scope as `dataset_name="window:<N>h"`) |
| `online_eval_configs` | Online evaluation configs the console created for an agent — identifiers only (config id/ARN/name, agent, service name, source log group); status, rule and evaluators are always read back from `GetOnlineEvaluationConfig`. Configs without a row are classified by name at read time (`exp_*`/`can_*` → experiment-owned, else external) |
| `experiments` | Optimization loop — stage + per-stage artifacts, resumable |

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

The standalone app under `apps/studio/` is not started by the root lifecycle.
The platform console provides the supported native canvas at `/create/studio`.
See [studio-integration.md](studio-integration.md).
