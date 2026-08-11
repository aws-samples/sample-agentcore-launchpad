# Troubleshooting / 故障排查

Real, verified gotchas from building and running the platform on AWS. Every
entry below was observed during implementation — none is speculative.

中文版: [troubleshooting.zh-CN.md](troubleshooting.zh-CN.md)

## Account & environment

- **AgentCore previews must be enabled per account.** Runtime, Harness,
  Registry, Gateway, Policy and Evaluation are previews that have to be turned
  on for your account in `us-west-2` before bootstrap will succeed.
- **Default model is `global.anthropic.claude-sonnet-4-6`.** There is no
  `sonnet-5` inference profile in the target account (verified via
  `bedrock list-inference-profiles`). Agents default to this profile; override
  per agent with `model_id` in the AgentSpec.
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
- **Harness agents are excluded from batch evaluation.** Managed-harness agents
  don't expose a span service name for trace scoping, so batch eval targets
  runtime-backed agents (`zip_runtime` / `studio` / `container`). The UI states
  this limitation.

## Local dev

- **Vite auto-shifts the frontend port.** If `5173` is taken, the platform
  frontend lands on `5174` (or the next free port). Set `PLATFORM_UI_PORT` to
  pin it. The backend stays on `8000`. This applies to `make dev`;
  `start.py` uses strict ports and fails before starting if any configured
  port is occupied.
- **Standalone Studio is not root-started.** The root lifecycle serves the
  native bilingual canvas at `/create/studio`; the vendored `apps/studio/`
  application must be run separately when explicitly needed.

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
