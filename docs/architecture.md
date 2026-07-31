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
| **Runtime** | Hosts zip and container agents (`CreateAgentRuntime`); the invoke chain calls the runtime data plane. Agent Management can also scan every `ListAgentRuntimes` page, inspect each resource with `GetAgentRuntime`, and explicitly import HTTP/A2A runtimes as externally owned ledger entries without changing the AWS resource. |
| **Harness** | Hosts 方式B agents (`CreateHarness`) — a managed entrypoint with no build artifact. |
| **Memory** | One shared `launchpad_memory` singleton: short-term session events + long-term semantic & user-preference strategies. Namespaces are keyed only on `{actorId}` (there is no `{agentId}` template), so the platform folds the agent id into the actor — `scoped_actor(agent_id, human)` → `<agent>__<human>` — which partitions **both** short-term events and long-term records (`/facts/<agent>__<human>`) per agent. Generated Strands runtimes restore short-term turns through `AgentCoreMemorySessionManager`. Claude Agent SDK containers create one request-local `MemorySessionManager`, inject bounded short-term turns plus `/facts/<actor>` and `/preferences/<actor>` records through a `UserPromptSubmit` hook, then persist the successful USER/ASSISTANT pair as one event. A2A runtimes use `<agent>__a2a__<contextId>` because direct A2A currently has no authenticated human actor envelope. One agent's learned facts never bleed into another's for the same person or A2A context; the ledger still stores the bare human actor for display. |
| **Gateway** | `launchpad-gw` turns a REST API (office-facts) and a Lambda (hr-database) into MCP tools with Cognito-JWT auth; agent tool calls flow through it. |
| **Identity** | Token vault backing the gateway — an OAuth2 provider (agent outbound auth) and an API-key provider. |
| **Registry** | `launchpad-registry` catalogues A2A agents, MCP servers, and AGENT_SKILLS. Every deploy auto-creates and submits an A2A record. Governance can import one existing AgentCore Gateway as one MCP record containing the Gateway endpoint and its complete discovered tool catalog; legacy per-target records remain until an explicit retirement after the Gateway record is APPROVED. Registry approval controls catalog visibility, not Gateway authorization. `GET /api/registry/attachables` reports catalog status separately from Harness attachability and resolves Gateway auth server-side. |
| **Policy** | Governance discovers existing MCP Gateways live, persists opt-in management through Launchpad-owned Gateway tags, and manages one attached Policy Engine plus Cedar policies. New Engines, Gateway attachments, and policies start in `LOG_ONLY`; ACTIVE edits create LOG_ONLY candidates, promotion and rollback use conservative ordering, and Gateway `ENFORCE` requires evidence or a typed zero-evidence override. Every mutation is journaled locally while AWS remains the source of current state. |
| **Evaluation** | Real `StartBatchEvaluation` / insights over CloudWatch traces. A run's scope is exactly one of: a **dataset** (replay items — multi-turn scenarios replay sequentially in one session), explicit **session ids**, or a **time window** (`lookback_hours` 1–336 — passive: no new invocations, `filterConfig.timeRange` over existing traffic). 13 general built-in evaluators plus the 3 ground-truth-only `Builtin.Trajectory*Match` matchers (selectable only on dataset runs whose scenarios define `expected_trajectory`) plus custom LLM-as-a-judge evaluators with full CRUD — create/edit (UpdateEvaluator is a full-config replace) on the `?view=evaluators` sub-page. Insights runs pick a subset of the three analysis types (failure analysis / user intent / execution summary). Datasets live in SQLite as devguide scenarios (`?view=datasets` sub-page: scenario editor, JSON/JSONL import) and sync one-way to immutable AWS Dataset resources (`AGENTCORE_EVALUATION_PREDEFINED_V1`); scenario ground truth (assertions / expected responses / expected trajectory) is injected into batch runs via `evaluationMetadata.sessionMetadata`. One batch per account — queue managed, unchanged. |
| **Optimization** | Recommendations → configuration bundles → gateway A/B (config-bundle 50/50) → target-based canary → verdict → promote → cleanup. |
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
| **package** | *skipped* (no artifact) | `pip install` ARM64 wheels → zip → S3 | zip context → S3 → CodeBuild (docker build+push) → ECR |
| **provision** | Reuse the shared execution role | Reuse the shared execution role | Reuse the shared execution role |
| **deploy** | `CreateHarness` + poll READY | `CreateAgentRuntime` + poll READY | `CreateAgentRuntime(containerConfiguration)` + poll READY |
| **register** | A2A registry record, auto-submitted | A2A registry record, auto-submitted | A2A registry record, auto-submitted |

Typical timings: harness ≈ 30 s, zip ≈ 1–3 min (incl. pip), container ≈ 2–4 min (observed: 1.7 min CodeBuild + seconds to READY)
(via CodeBuild). See [troubleshooting.md](troubleshooting.md).

### Model source (方式A + 方式B)

`AgentSpec.model_source` selects the model-hosting surface: `mantle` (Bedrock
Mantle) or `bedrock` (native Bedrock). **No API key or bootstrap resource is
involved on either surface** — the agent's own execution role authenticates
both. The field defaults to `bedrock` for backward compatibility with specs
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

**Zip / Strands Studio (方式A)** — the model reaches Strands as an argument to
`Agent(model=...)`, so the source changes the *generated code*. A bare id string
resolves to a Converse call, so `mantle` renders an explicit model object
instead (`app/templates/strands_agent/main.py.tmpl::build_model`):

```python
OpenAIResponsesModel(bedrock_mantle_config={"region": MANTLE_REGION}, model_id=MODEL_ID)
```

`bedrock_mantle_config` makes the Strands SDK mint a short-lived bearer token
from the ambient AWS credential chain — the Runtime execution role, which
already holds `bedrock:InvokeModel` — on **every request**, and derive the
endpoint itself. There is **no `BEDROCK_API_KEY`** on this path. Two
consequences worth knowing:

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
wizard pins them to `bedrock` and hides the selector. The Claude Agent SDK
(container) method is likewise pinned to `bedrock` and offered only Claude ids,
since it cannot drive anything else.

### Existing Runtime discovery

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
                        ▼
             AgentCore Runtime / Harness
                        │  (session isolation, streaming)
                        ├─ Memory        (session context read/write)
                        ├─ Gateway tools (MCP over Cognito JWT)
                        ├─ Policy        (Cedar ENFORCE at the gateway)
                        └─ Observability (spans → CloudWatch Transaction Search)
```

The public `/v1` surface adds `X-Api-Key` auth (keys stored sha256-hashed);
everything downstream of the dispatch is identical to the console path.
Every agent response carries one backend-owned `invoke_capability`; console
invoke, Chat, and `/v1` enforce the same projection. Imported runtimes use the
buffered compatibility path because Launchpad cannot assume an arbitrary
external runtime emits the generated Claude SDK event contract.

Harness and Claude Agent SDK container agents stream native model deltas.
Claude containers enable SDK partial messages and yield `delta`, `tool`, and
`complete` events through the AgentCore Runtime SSE response; the platform
parses the Runtime `StreamingBody` incrementally and forwards those events
without waiting for EOF. Synchronous invoke consumes the same event parser and
joins deltas. Zip/studio runtimes and active canary Gateway routes retain the
buffered compatibility path. Existing containers must be republished to pick
up a changed generated runtime template. AgentCore pins an existing runtime
session to the version that first served it, so a post-republish validation
must start a new Chat session; an old session continues on its original image.

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

The span channel is the opt-in half, and it is **per Gateway**: AgentCore emits
Policy decision spans only after trace delivery is enabled on the attached
Gateway. That is a CloudWatch vended-log delivery (source `logType=TRACES` →
`XRAY` destination → delivery), not a Gateway setting, so enabling it never calls
`UpdateGateway`. `policy_bootstrap.ensure_gateway_traces()` owns it and runs inside
`make bootstrap` after Transaction Search, which AWS requires first; spans land in
the shared `aws/spans` log group. The step is idempotent and non-fatal — a failure
is reported in the bootstrap summary with the AWS error code rather than aborting
the run, because the platform is usable without spans. Missing this delivery is
the whole reason the span channel previously looked unverifiable.

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

When enabled, middleware protects every `/api/*` route, including API docs,
except `/api/health`, `/api/auth/status`, `/api/auth/login`, and
`/api/auth/register`. The middleware does not guard `/v1/*`, whose existing
`X-Api-Key` contract remains authoritative.

Admins additionally get the **User Management** console module (`/users`) over
`GET /api/users`, `GET /api/users/stats`, `PATCH /api/users/{id}`, and
`DELETE /api/users/{id}` — the approval queue (pending filter + count, approve /
reject), list/search/filter, registration statistics, extend validity,
enable/disable, role change, one-time password reset, and delete. A
member session receives `403 auth.forbidden` on all four routes and the console
renders a forbidden state instead of the table. Data itself is **not** partitioned
per user: every authenticated account sees the same agents, knowledge bases and
traces; only this module is role-gated.

The default cookie works with the local HTTP stack. HTTPS deployments must set
`LAUNCHPAD_AUTH_COOKIE_SECURE=true`. Leaving the password unset disables the
gate entirely (console open, registration refused with
`auth.registration_disabled`, `/api/users*` reachable as the implicit local
admin), preserving the bootstrap-free local development and test flow.

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
exists in either file, and `tests/test_memory_console.py` asserts that.

| `?view=` | Shows | AgentCore operations |
|---|---|---|
| `overview` | resource config (id/arn/status/event expiry/KMS/execution role), each long-term strategy with its `namespaces` + `namespaceTemplates`, and the account's other memory resources with the platform singleton marked | `GetMemory`, `ListMemories`, `ListActors` |
| `short-term` | actor → session → event drill-down; events render as a timeline of conversational role/text turns, blob payloads as a byte count only | `ListActors`, `ListSessions`, `ListEvents` |
| `long-term` | records for a resolved namespace, plus semantic retrieval with relevance scores | `ListMemoryRecords`, `RetrieveMemoryRecords` |

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
| `bedrock-agentcore` metrics namespace | tokens-by-model tile + chart | `ListMetrics` (dimension discovery) → `GetMetricData` sums of `gen_ai.client.token.usage` |
| AgentCore Memory `ListEvents` + ChatMessage ledger | session conversation transcript | ChatSession join (`session_id → actor_id`); Memory is primary, while the exact rendered-message ledger repairs lagging/incomplete or historically split actor partitions; harness envelopes are decoded and tool-result turns dropped |

Every view is served from a **60-second TTL cache** keyed by (view, range) —
Logs Insights is billed per scan — with `force=true` (the ⟳ REFRESH button)
bypassing it. Ranges are whitelisted (`1h/6h/24h/7d`); trace ids
(`^[0-9a-f]{32}$`) and session ids (`^[A-Za-z0-9_-]{8,128}$`) are validated at
the router **and** re-checked in the query builders before being interpolated
into Logs Insights query strings. Token sums count only terminal LLM
operations (`chat` / `text_completion` / `generate_content`) because
agent-level `invoke_agent` spans repeat their children's `gen_ai.usage.*`
values. Unified groups also contain prompts, OTel events, structured logs, and
standard output; span-derived queries require `startTimeUnixNano` so correlated
non-span records do not inflate trace, latency, error, token, or tool counts.

Cost figures are **advisory estimates**: token counts × `model_prices` from
`config/launchpad.yaml` (USD per 1M tokens, substring-matched against
`gen_ai.request.model`; unknown models show token counts with a `—` cost). The
UI labels them `≈ / EST`. The price map is kept fresh from litellm's public
price file (`app/services/model_prices.py`): a daily daemon plus the dashboard's
`⟳ UPDATE PRICES` button (`POST /api/observability/prices/refresh`) pull exact
per-model entries — including regional Bedrock premiums and cache read/write
rates — for every model seen in the account's telemetry, refresh the operator's
short fallback keys, and leave unmatched keys untouched. Source URL and
interval are configurable (`model_prices_source_url`,
`model_prices_refresh_hours`; `0` disables the daemon).

**Telemetry per creation method:** Strands (zip/studio) and harness agents emit
gen_ai spans natively. Claude Agent SDK containers drive the `claude` CLI as a
subprocess — invisible to ADOT auto-instrumentation — so the generated agent
emits the telemetry manually (`app/templates/claude_sdk_agent/tracing.py`,
adapted from the agentxray demo-agent): an `invoke_agent` root span, one
`execute_tool` span per tool call, one aggregate `chat` span carrying the
query's token usage (`ResultMessage.usage`; the SDK's `cache_creation` maps to
`cache_write`), and Strands-shaped content events for the span drawer's
input/output messages. The scope name must stay `strands.telemetry.tracer` —
AgentCore only parses spans/events from supported instrumentation scopes.

Tab IA: **DASHBOARD** (5 stat tiles + traffic/latency/tokens/tools charts) ·
**SESSIONS** (list → detail with Memory/ledger-reconciled transcript + traces-in-session cards) ·
**TRACES** (filterable list → waterfall Gantt with span drawer: token usage
incl. cache read/write, est cost, tool schema, raw attributes). Cross-links:
deep links `/observability?trace=<id>` / `?session=<id>`; the Chat trace rail
links to the current session's detail (`OPEN IN OBSERVABILITY ↗`) and session
detail links back (`OPEN IN CHAT ↗`); `service.name` values are mapped to
platform agent names via the ledger (`resource_id` base-name match, raw name
fallback).

## The SQLite ledger and job/event model

State that is cheap and local lives in a SQLite ledger at `data/launchpad.db`
(`backend/app/models/ledger.py` + the evaluation/optimization models):

| Table | Holds |
|---|---|
| `agents` | Agent records — name, method, status, ARN, resource id, registry record id, version, spec |
| `deployments` | One row per deploy run — the five-stage array with per-stage status/detail/timestamps |
| `jobs` | Async work (type `deploy_agent`) — status + a JSONL `log` of stage events |
| `chat_sessions` | Chat playground sessions — turns, actor, last-seen |
| `users` | Console accounts created by registration — username/email, pbkdf2 password hash, role, status (`pending`/`active`/`disabled`), `expires_at` (null until approval), last sign-in + sign-in count (the built-in admin is config-only and has no row) |
| `api_keys` | Public-API keys — sha256 hash + prefix (plaintext never stored) |
| `policy_decisions` | Governance decision log — principal, tool, ALLOW/DENY, reason |
| `policy_changes` | Immutable Gateway/Engine/Policy mutation snapshots, operation progress, override reasons, and rollback inputs |
| `eval_datasets` / `eval_runs` | Evaluation datasets (legacy prompts or devguide scenarios + description + last AWS-sync blob) and run state (scores or insight trees; window runs encode their scope as `dataset_name="window:<N>h"`) |
| `experiments` | Optimization loop — stage + per-stage artifacts, resumable |

**Job/event model.** Creating an agent returns `202` with a `job_id`. The
deploy job runs on a background thread, appending one JSONL event per stage
transition to `Job.log`; `GET /api/jobs/{id}` returns those events and
`GET /api/agents/{id}` returns the `Deployment.stages` array. The agent moves
`deploying → active` (or `failed`) as the job finishes. Authoritative resource
state (runtime status, registry record status, eval/trace data) always lives in
AWS; the ledger holds identifiers and derived progress only.

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
