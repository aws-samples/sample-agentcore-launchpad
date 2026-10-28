# Troubleshooting / 故障排查

Real, verified gotchas from building and running the platform on AWS. Every
entry below was observed during implementation — none is speculative.

中文版: [troubleshooting.zh-CN.md](troubleshooting.zh-CN.md)

## Account & environment

- **AgentCore previews must be enabled per account.** Runtime, Harness,
  Gateway, Policy and Evaluation are previews that have to be turned on for
  your account in `us-west-2` before bootstrap will succeed. Agent Registry is
  GA (the `agent-registry` namespace since 2026-08-06, see
  [registry-ga-migration.md](registry-ga-migration.md)) and needs no preview
  enablement.
- **Default model of a new agent is `global.zai.glm-5.3`**
  (`agent_default_model_id`, env `LAUNCHPAD_AGENT_DEFAULT_MODEL_ID`). New agents
  default to this inference profile; other models stay selectable, and existing
  agents keep their own `model_id`. Override per agent with `model_id` in the
  AgentSpec, or change the default as in
  [setup.md → Choosing models](setup.md#choosing-models--切换模型).
- **OpenAI / Anthropic models not available in this account.** The platform's
  own model calls default to OpenAI and Anthropic models (judge
  `global.openai.gpt-6-sol`, intents and suggested questions
  `global.anthropic.claude-sonnet-5`, actor, architect, assistant, …), so such
  an account sees evaluator creation refused, a degraded intent view or
  `AccessDeniedException` on those features. An administrator switches them on
  **admin → Configuration → Models** (pick, **Test**, **Save**; applies at
  once), or on the host `scripts/switch_models.py` probes the account and
  writes working substitutes to `/etc/launchpad-models.env`
  ([setup.md → Choosing models](setup.md#choosing-models--切换模型)). Claude
  Agent SDK, Studio AI Fix and the Skill Lab claude / codex paths cannot move
  to another family. A new judge changes evaluation scores.
- **`make bootstrap` stops at `UpdateTraceSegmentDestination` with
  `AccessDeniedException: XRay does not have permission to call PutLogEvents on
  the aws/spans Log Group`.** The account/region never had Transaction Search
  on, so no CloudWatch Logs resource policy lets X-Ray write `aws/spans`.
  Bootstrap now places `TransactionSearchXRayAccess` before the update; on an
  older checkout, update and rerun `make bootstrap` (it is idempotent, so the
  stack and the resources already created are reused).
- **`config/launchpad.yaml` is gitignored.** It holds account ids and demo
  credentials, so it is never committed. If it is missing (fresh clone, or you
  deleted it), rerun `make bootstrap` — it is idempotent and rewrites the file
  from existing resources.
- **uv-managed venvs need `uv run`.** Run backend/infra commands through
  `uv run …` (as the Makefile does). The zip package stage additionally needs
  `pip` inside the venv — uv venvs don't ship it, so it is declared as an
  explicit dependency.

## Deploy timings & behavior

- **Deploy times vary by method:** harness ≈ 30 s (no build), zip ≈ 1–3 min
  (includes `pip install` of ARM64 wheels), container ≈ 2–4 min (CodeBuild
  docker build + push). Watch progress via `GET /api/jobs/{id}` or the agent's
  `deployment.stages`.
- **Container images need a non-root user.** The Claude CLI's
  `bypassPermissions` mode refuses to run as root, so the 方式A image builds
  and runs as a non-root user — keep that if you customize the Dockerfile.

## Registry

- **Records settle asynchronously.** A new record is `CREATING` and transitions
  to `DRAFT` a moment later — poll if you read it back immediately.
- **`DEPRECATED` is terminal.** There is no `PUBLISHED` state; `APPROVED` is the
  live state. The lifecycle is `DRAFT → PENDING → APPROVED`, and disabling a
  record moves it to `DEPRECATED`, from which it cannot return.
- Descriptor schema versions are strict (MCP `2025-07-09`, skills `0.1.0`) — the
  platform sends the exact versions the service expects.

## Evaluation & optimization

- **One active batch evaluation per account.** Runs are serialized behind an
  account lock; a submitted run reports its `queue_position` and starts when the
  lock frees. This is expected, not a hang.
- **Batch evaluation takes ~3–5 min; insights ~15–20 min.** A quick 2-item
  scoring run lands in a few minutes; a failure-analysis insights run is much
  longer. The run stays `evaluating` until CloudWatch traces are scored.
- **A/B per-arm metrics lag > 30 min for small samples.** Online-evaluation
  metrics take time to populate, so with a handful of invocations the verdict
  is honestly reported as *insufficient-data* rather than forced. Use larger
  traffic (or wait) for a real significance call.
- **A judge that needs ground truth is refused before the run starts.** A custom
  LLM-judge prompt referencing `{expected_response}`, `{expected_tool_trajectory}`
  or `{assertions}` is filled from the dataset's scenarios. Against a window /
  session scope, or a dataset carrying none of it, AgentCore throws
  `ValueError: Evaluator prompt requires: 'expected_response'` on every
  (session × evaluator) pair and the whole batch ends FAILED ~10 minutes later,
  so the console rejects the submission with `run.judge_needs_ground_truth`
  instead. Fix it by adding ground truth to the dataset scenarios
  (`turns[].expected_response` / `expected_trajectory` / `assertions`) or by
  editing the judge prompt to drop the placeholder.
- **A failed run names its cause.** A batch's `errorDetails` only counts the
  casualties ("All 30 sessions failed"); the per-trace reason is written only to
  the batch's results log stream
  (`/aws/bedrock-agentcore/evaluations/batch-evaluations/results/<workspace>`,
  stream `run-<batchEvaluationId>`). The run's error now carries both, so read the
  run row first and go to the log stream only for the remaining traces.
- **Harness agents are excluded from batch evaluation.** Managed-harness agents
  don't expose a span service name for trace scoping, so batch eval targets
  runtime-backed agents (`zip_runtime` / `studio` / `container`). The UI states
  this limitation.

## Knowledge Bases

- **Creation answers `202` and the data source appears a few minutes later.** A
  managed KB needs 1.5–3 min to leave `CREATING`, and its data source cannot be
  created before it is `ACTIVE`, so `POST /api/knowledge-bases` returns
  immediately and a backend thread (`knowledge._start_source_completion`) polls
  and creates the source afterwards. A backend restart inside that window kills
  the thread and leaves an `ACTIVE` KB with **zero data sources**. The detail page
  says so and offers the fix: "No data source yet. The backend creates it once the
  knowledge base turns ACTIVE (usually 1–3 min); if it never appears, repair it
  here — clicking twice cannot create a duplicate."
  (`knowledge.detail.sources.missingSource`) next to a `Repair data source`
  button, which posts `POST /api/knowledge-bases/{kb_id}/data-sources`.
  Double-clicking is safe: `_find_data_source_at` returns the existing connector
  for the same bucket/prefix instead of creating a second one.
- **A sync started too early is refused as `409 kb.sync_not_ready`.**
  `StartIngestionJob` throws `ValidationException` while the data source is still
  provisioning (not yet `AVAILABLE`) and `ConflictException` when a sync is already
  running for it; `knowledge.start_sync` catches both and answers one
  `409 kb.sync_not_ready` ("the data source may still be provisioning, or another
  sync is already running"). Wait for the source to report `AVAILABLE` — the
  console then starts the first ingestion by itself. On every *other* KB route
  those two AWS exceptions are not caught locally and reach the console through the
  global mapping instead, as `400 aws.validation` and `409 aws.conflict` (see the
  error table in [api.md](api.md)).
- **A wrong `kb_role_arn` only fails at ingestion.** `CreateKnowledgeBase` does
  not validate `roleArn`, so a KB created with a bad or under-privileged role
  still goes `ACTIVE`; the failure surfaces later as a failed ingestion job (its
  `failure_reasons` are shown per data source). The per-KB inline policy that
  grants `s3:GetObject`/`s3:ListBucket` on a BYO bucket is put on the role named
  by that same ARN (`knowledge._sync_kb_policy`), so it lands on the wrong role
  too. Missing entirely, the key is caught up front: `create_kb` refuses with
  "kb_role_arn missing from this workspace's resource map — run its bootstrap"
  rather than creating an unusable KB.

## Local dev

- **Vite auto-shifts the frontend port.** If `5173` is taken, the platform
  frontend lands on `5174` (or the next free port). Set `PLATFORM_UI_PORT` to
  pin it. The backend stays on `8000`. This applies to `make dev`;
  `start.py` uses strict ports and fails before starting if any configured
  port is occupied.
- **Studio is part of the console.** The Strands Studio canvas is served at
  `/create/studio`; there is no separate Studio process or port.

## Governance

- **A Cedar deny carries the deciding policy id.** When the gateway blocks a
  tool call in `ENFORCE` mode, the decision (and the decision log) name the
  policy that produced the DENY — use it to trace which statement fired.
- **Management is an opt-in tag, not resource ownership.** An unmanaged
  Gateway remains readable. Registry and Policy mutations require the
  Launchpad management tags. Unmanage removes only those tags and does not
  detach or delete any AWS resource.
- **Registry approval does not authorize a tool.** A Gateway-level Registry
  record publishes the entire Gateway catalog. A Harness attaches the whole
  Gateway; Cedar policies decide which exact actions may run.
- **External CUSTOM_JWT Gateways can be catalog-only.** Launchpad never accepts
  an operator JWT. Without a configured AgentCore Identity OAuth provider
  mapping, the Registry record can be approved but Harness attachment remains
  disabled.
- **Engine attachment can fail IAM preflight.** The Gateway role needs
  `bedrock-agentcore:GetPolicyEngine`, `AuthorizeAction`, and
  `PartiallyAuthorizeActions` scoped to the Engine/Gateway resources. Launchpad
  returns a remediation statement but never edits the external role.
- **A deleted Engine leaves the Gateway reference behind.** AWS keeps
  `policyEngineConfiguration.arn` after the Policy Engine itself is deleted.
  Governance shows that as `ENGINE DELETED` with the stale ARN rather than
  "not attached", refuses policy mutations with
  `governance.policy_engine_deleted`, and offers the create-and-attach form —
  confirming it creates a new Engine and overwrites the stale reference.
- **Policy decision telemetry may report unavailable.** The production parser
  stays disabled until real ALLOW and DENY Policy spans establish the preview
  field shape. The UI does not substitute local demo decisions. Promotion with
  zero evidence requires the exact Gateway name and a non-empty audit reason.

## Identity

- **`agent.inbound_issuer_mismatch` (409) on Chat, `/v1`, invoke or
  evaluation.** The agent's JWT authorizer trusts an IdP other than the
  workspace Cognito pool, and every platform invoke presents a workspace-Cognito
  token. This is a limit, not a fault: call the agent externally with a token
  from its IdP (`samples/inbound-jwt/`), or switch it to the Cognito preset or
  IAM on the agent page. The detail names both issuers
  ([identity.md §8.1](identity.md#81-inbound-auth-iam-sigv4-vs-jwt-bearer)).
- **The obo target was created with an issuer warning.** The Connection's IdP
  is not the gateway's inbound issuer. The exchange succeeds only if that IdP
  trusts the gateway issuer's tokens as subject tokens; configure that trust at
  the IdP or pick a Connection on the same issuer
  ([identity.md §8.3](identity.md#83-obo-on-behalf-of-token-exchange)).

## Agent-DLC: criteria, the golden set and the release gate

Most of these are refusals by design — the message names what to do instead.

| What you see | What it means → what to do |
|---|---|
| A deploy fails at `register` with `ConflictException: Concurrent update detected` | A transient AgentCore Registry conflict while it settles the record it was just handed — not an Agent-DLC problem, but it is what fails a gated deploy before the release even opens. `registry.submit_record` retries it three times (1 s / 3 s / 7 s) on AWS's own "please retry" wording; a *different* conflict (e.g. the record is already PENDING_APPROVAL) still surfaces, because that one is real |
| `422 criteria.invalid` with `detail.findings` | The criteria table breaks a rule of the methodology. `criteria.redline_judge`: a red line cannot be decided by an LLM judge — use a code assertion, a trajectory matcher or a metric. `criteria.dimension_uncovered`: every dimension needs a criterion or an explicit `n/a:<dimension>` note. `criteria.no_redline`: a table with no red line cannot be published |
| A gate criterion shows "declared gate · observed" | A judge criterion does not block until it is calibrated. Run a labelling task (Standards → Calibration) and record `aligned`; until then it is recorded, not enforced |
| `409 calibration.not_supported` | The numbers do not support calling the judge aligned. The detail carries judge–human κ, human–human κ, n and the floor. If **people** disagree with each other, rewrite the criterion — no judge can fix an unwritable rule |
| Gate verdict `INVALID` | Not a failure: the evidence cannot decide. `provenance.issues` names it — an unsigned criteria version, a run against another version or endpoint, no holdout evaluated — and each row's `reason` names missing verdicts or an undetermined share over 5%. Fix the evidence; a waiver is the wrong tool and a red line can never be waived |
| `409 golden.holdout_sealed` | The holdout is curated once and then sealed. That is what makes it a holdout; add the new cases to `dev` or `regression` |
| `409 golden.holdout_closed` | Admission and the item editor never write the holdout. Use `dev` or `regression` |
| `409 release.nothing_pending` | No candidate is waiting. In a `gated` workspace a deploy opens the release; in a `direct` one nothing is gated at all (`PUT /api/release-policies/{workspace}` → `release_mode`) |
| `409 release.not_gateable` + reason | A2A runtimes take no endpoint qualifier, system presets are released by the platform, and an agent with no deployed version has nothing to point an endpoint at |
| Gate reports "2 gate run(s) did not complete" with a telemetry timeout | The evaluation never saw the sessions. Each endpoint logs to its own group, so this is usually a brand-new endpoint that has never been invoked: invoke the agent once on that endpoint, then re-run the gate |
| `409 run.cost_over_limit` / `run.cost_confirm_required` | The workspace's spend guard. The detail carries the estimate; an administrator can start a run over the limit, and `confirm_cost: true` acknowledges the confirmation ceiling |
| `422 run.repeats_scope` | pass^k replays dataset scenarios against an agent — it cannot repeat past sessions or a log source |
| `409 watch.over_cost_ceiling` | A scheduled re-evaluation would cost more than its ceiling, so it was skipped and reported rather than spent. Raise `max_cost_usd` or shrink the split |
| A new-region workspace is `ready` but its traces, observability and evaluations are empty; bootstrap's observability stage says `unavailable · AccessDeniedException` | Transaction Search never turned on in that region. Update the workspace role (or the hub role) from the current `infra/spoke/` template — it now carries the permissions `UpdateTraceSegmentDestination` exercises on the caller's behalf — then `POST /api/workspaces/{id}/observability/repair` (administrator) re-runs just that stage. No re-bootstrap needed |
| A batch evaluation fails with `FAS credentials do not have permission to create CloudWatch log groups` | The role predates the `EvaluationResultLogGroups` statement: AgentCore creates the results log group with the caller's credentials. Update the role from the current `infra/spoke/` template |
| `409 annotation.links_not_allowed` | A prod-tier workspace does not hand out account-free labelling links: labelling there touches real customer transcripts. Invite the annotator as a member with `judge.calibrate` |
| `404 share.not_found` on an annotate link | Every unusable state is the same answer on purpose (unknown, revoked, expired, task gone, or the workspace promoted to prod). Mint a new link |
| `403 calibration.own_labels` | You produced this task's labels — as an annotator, its adjudicator, or by issuing one of its annotation links — so you cannot also rule on its judge. Have someone else with `judge.calibrate` (or an administrator) record the verdict |
| `409 calibration.not_supported` with `human_human_kappa: null` | Only one person labelled. A judge is certified against people who agreed with each other, so items with a single rater contribute nothing — get the second annotator to finish |
| `409 admission.redaction_blocked` | The PII guardrail refused this transcript, so it must not enter the golden set. The admit route computes the screening itself, so this is the answer even when the queue was never opened |
| `403 auth.permission_required` on sign / admit / calibrate | `criteria.sign`, `golden.admit` and `judge.calibrate` are granted to named people, never by role — they decide what "good" means. An administrator grants them per user |
| An agent deletes with `aws_resource_deleted: false` (a gated one, or one with Runtime Canary endpoints) | Expected, not a failure. A harness endpoint can stay DELETING for minutes and AgentCore will not delete a resource that still has one, so the console row goes immediately and the background sweep (`dlc.teardown`, every tick) finishes the AWS side. Nothing to do; `teardown_pending_at` cleared (and `endpoint_mode` back to `default`) on the deleted row is the sweep's receipt |
