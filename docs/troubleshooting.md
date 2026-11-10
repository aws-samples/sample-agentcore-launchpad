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
- **Default model is `global.anthropic.claude-sonnet-5`** (`DEFAULT_MODEL_ID`).
  New agents default to this inference profile; Sonnet 4.6
  (`global.anthropic.claude-sonnet-4-6`) stays selectable, and existing agents
  keep their own `model_id`. Override per agent with `model_id` in the
  AgentSpec.
- **`config/launchpad.yaml` is gitignored.** It holds account ids and demo
  credentials, so it is never committed. If it is missing (fresh clone, or you
  deleted it), rerun `make bootstrap` — it is idempotent and rewrites the file
  from existing resources.
- **Sign-in answers `429 auth.locked` / `auth.rate_limited`.** The console's login
  protection fired (r12-g): `auth.locked` means the submitted account name had 5
  consecutive wrong passwords and is locked for 60 s, doubling per further failure up
  to 30 min — wait out `Retry-After`, or have an administrator press *Unlock* on the
  Users page (`PATCH /api/users/{id} {"unlock": true}`). `auth.rate_limited` means the
  source IP exceeded 20 failed sign-ins in 5 min (or 10 failed registration attempts in
  an hour) and this request ALSO failed — a correct password, a valid key or a valid
  registration is never refused by the window. On the EIP path the source IP is the
  real client (nginx forwards it); behind the console NLB every user shares the NLB's
  private IP, so one noisy client makes everyone's *failures* 429 for a while — raise
  `LAUNCHPAD_AUTH_LOGIN_IP_LIMIT` / `LAUNCHPAD_AUTH_REGISTER_IP_LIMIT` (or set `0`)
  rather than weakening the per-account lock. The state is in-process: restarting the backend clears every lock,
  including the built-in admin's. Each lock is a `WARNING` on `launchpad.auth` in
  `journalctl -u launchpad-backend`. The same 429 on `/v1` means 20 invalid
  `X-Api-Key` attempts from that IP in 5 min.
- **Everyone was signed out after a deploy.** Since r12-g the session cookie is signed
  with a random secret in `data/session_secret` (or `LAUNCHPAD_SESSION_SECRET`), not with
  the admin password. The first start after the upgrade generates it and signs every
  existing session out once (a `WARNING` on `launchpad.auth` says so). A password change
  or reset still signs that one account's other sessions out (session epoch in the
  cookie; the admin's follows `LAUNCHPAD_AUTH_PASSWORD`). To sign *everyone* out, rotate
  the secret: delete `data/session_secret` (or set a new `LAUNCHPAD_SESSION_SECRET`) and
  restart the backend.
- **The admin account cannot be locked out, but members can.** By design: the built-in
  `LAUNCHPAD_AUTH_USERNAME` is the break-glass credential and is exempt from the
  per-name lockout (it keeps the strong configured password and the per-IP window).
  Give it a long random password.
- **Registration says `auth.weak_password` for a password that used to be accepted.**
  The policy is now 12+ characters, not your username / email, and not a common
  password (`detail.reason`); existing accounts are untouched until they change their
  password. `LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH` tunes the length (floor 6).
  Too strict for your deployment? `LAUNCHPAD_AUTH_PASSWORD_POLICY=relaxed` keeps only the
  length rule; `GET /api/auth/status` → `password_policy` shows what is in effect.
- **A registration "succeeded" but nothing shows up in the pending queue.** Under
  approval mode a taken username or email answers exactly like a pending registration
  (no account enumeration); the collision is an `INFO` line on `launchpad.auth`. Ask the
  user to register with another name. `409 auth.registration_backlog` means
  `LAUNCHPAD_AUTH_REGISTRATION_PENDING_MAX` (50) accounts await review — approve or
  reject some in the Users console.
- **`409 users.last_admin` / `users.self_delete` on the Users page.** You are demoting,
  disabling, expiring or deleting the last active administrator *account* (the
  config-driven admin does not count), or deleting your own account. Promote another
  account to administrator first, then retry.
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
- **An architect turn fails with `harness.incomplete_response` ("the agent run
  ended without a complete text response").** This now means only what it says:
  the Harness stream ended with no reply text (empty, or cut after a tool step)
  and no error frame. A Harness-side failure after a tool step (model
  `ReadTimeoutError`, `ModelThrottledException`, `ContextWindowOverflowException`,
  `ValidationException`, an expired bearer) is surfaced from the AgentCore SDK's
  terminal error frame instead: the turn is replayed once in a fresh session with
  the cause in the `retry` reason (after a 15 s backoff —
  `LAUNCHPAD_ASSISTANT_REPLAY_BACKOFF_SECONDS` — so the replay does not hit the same
  5xx), and a second failure shows
  `runtime returned error: <error_type>: …` in the transcript (with
  `detail.error_type` in the `assistant_turn_events` log). Before cn/r11-j that
  frame was skipped and every such failure read as an incomplete response.
- **An evaluation run completed with errors after an `internalServerException`
  (`EventStreamError` on a judge record, or a scenario `failed after 1/3 attempts:
  RuntimeError: runtime returned error: An error occurred (internalServerException)…`).**
  Since cn/r12-f the evaluation worker retries both the scenario replay and the judge
  call on any transient upstream error (the same predicate as the architect replay:
  5xx / throttling / read timeout, `runtime_invoke.is_upstream_transient`), up to 3
  attempts, waiting `LAUNCHPAD_EVAL_TRANSIENT_RETRY_BACKOFF_SECONDS` (default `5,15`)
  between them. A record that still errors names every attempt: the replay failure
  reads `failed after 3/3 attempts: … (retried after: #1 …, waited 5 s; #2 …)` and a
  judge failure `judge <id> errored on every one of 3 attempts (transient upstream; waited
  5 s, 15 s): EventStreamError: …` (the record's `error_type` is the cause's class). The
  wait list reaches the worker on install / repair of the `launchpad-eval-worker`
  preset; the in-process executor and SCORE NOW read it live. A record that errored
  after ONE attempt on such an error means the worker predates cn/r12-f — repair it.
- **A console page is blank or a feature (Monaco editor, browser live view, video
  playback) stops working after a host update, and DevTools shows `Refused to load …
  Content Security Policy`.** nginx now sends an enforced CSP plus the other browser
  security headers on every response (`infra/ec2/nginx-site.sh`, security scan
  2026-10-17). A new third-party CDN, iframe or non-`wss:` WebSocket needs its source
  added to the directive table there (the test in `infra/tests/test_nginx_site.py` pins
  it), then `scripts/host-update.sh`. Directive-by-directive rationale and the rollback:
  `docs/cn/interfaces/deploy.md` §11.

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
- **TypeScript (Node.js) agents in cn-northwest-1 get traces through the platform's layer
  (N17-obs459 / N17-obs465).** ADOT Node (<= 0.13.0) cannot sign its trace export in the China
  regions, so for a Node container agent the deploy adds the platform's aws-cn trace exporter
  layer on top of your image (default on; the wizard's toggle / `byoc.cn_trace_exporter: false`
  turns it off). The agent detail shows your image, the deployed one and a **Traces** badge.
  The runtime log prints one line at start; its states:
  `attached` (the export is signed for the `.amazonaws.com.cn` endpoint), `native` (your ADOT
  release signs it itself — the layer stays out of the way), `skipped (no ADOT Node in the app)`
  (add `@aws/aws-distro-opentelemetry-node-autoinstrumentation` and start with
  `--require …/register`), `skipped (traces disabled by the app: OTEL_TRACES_EXPORTER=…)` /
  `skipped (… OTEL_SDK_DISABLED=true)` (your own setting wins — unset it for traces),
  `unavailable (no exporting span processor: …)` (ADOT installed nothing that exports spans),
  `unavailable (<reason>)` (a new ADOT release changed what the layer
  hooks — the agent still runs, without traces; report it). "Traces not arriving" means the
  newest session left no spans although the layer attached — check the execution role's X-Ray /
  Logs permissions. Traces only: the OTLP logs exporter is turned off by the layer. See
  evaluation-engine.md §33.7.

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
- **Studio is the native canvas.** The root lifecycle serves the native,
  bilingual Studio canvas at `/create/studio`; there is no separate
  standalone Studio application to run.

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
