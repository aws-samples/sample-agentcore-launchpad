# Setup / 环境搭建

## Prerequisites / 前置条件

- AWS account with Bedrock AgentCore previews enabled (Runtime, Harness, Registry, Gateway, Policy, Evaluation) in `us-west-2`
- Credentials with administrator-level access (`aws sts get-caller-identity` works)
- `uv` ≥ 0.8, Node.js ≥ 20 (`npm`), AWS CDK CLI v2 (`npm i -g aws-cdk`), Docker (ARM64-capable, phase 5)
- One-time CDK bootstrap per account/region: `cdk bootstrap aws://<account>/us-west-2`

## Bootstrap / 引导

```bash
# 1. install dependencies
cd backend  && uv sync && cd ..
cd frontend && npm install && cd ..
cd infra    && uv sync && cd ..

# 2. deploy shared infra + AgentCore singletons, write config/launchpad.yaml
make bootstrap          # = cd backend && uv run python ../scripts/bootstrap.py
```

The bootstrap is **idempotent**: the CDK stack (`launchpad-base`) is deployed only
when missing, and the AgentCore registry (`launchpad-registry`) / memory
(`launchpad_memory`) are created **once** and reused on every later run.
再次运行只会打印 `reused`,不会产生重复资源。

Bootstrap also owns the CLI used to convert managed Harness agents into Runtime
agents. It installs exactly `@aws/agentcore@0.21.1` at
`data/agentcore-cli/node_modules/.bin/agentcore` without a global npm install,
verifies the version, and reuses it on later runs. Conversion never uses an
`agentcore` executable from `PATH`; if the managed installation is deleted or
unusable, rerun `make bootstrap`. This CLI version supports both Harness exports
without Skills and Skill-bearing exports whose generated code calls
`get_or_create_agent(session_id, user_id, _skill_plugins)`.

What it creates / 创建内容:

| Resource | Name |
|---|---|
| S3 artifacts bucket | `launchpad-artifacts-<acct>-<region>` |
| ECR repo | `launchpad-agents` |
| CodeBuild (ARM64) | `launchpad-agent-builder` |
| Cognito user pool | `launchpad-users` (+ groups `platform-admin`, `hr-analyst`, demo users `river`/`demo`) |
| IAM execution role | `launchpad-agent-execution-role` |
| AgentCore Registry | `launchpad-registry` |
| AgentCore Memory | `launchpad_memory` (short-term events + semantic & user-preference long-term strategies) |
| Managed AgentCore CLI | `data/agentcore-cli/` (`@aws/agentcore@0.21.1`) |

Demo user passwords are generated and stored in `config/launchpad.yaml`
(**gitignored** — treat as local secrets; a sanitized `config/launchpad.example.yaml` is committed).

### Policy span channel / 策略 span 通道

Bootstrap also opens the AgentCore **Policy decision span** channel for the
Gateway. AgentCore emits those spans only once *trace delivery* is enabled on the
attached Gateway, which is a CloudWatch vended-log delivery rather than a Gateway
setting — so **no Gateway resource is modified**:

| Delivery resource | Name |
|---|---|
| Delivery source (`logType=TRACES`) | `<gateway-id>-traces-source` |
| Delivery destination (`XRAY`) | `<gateway-id>-traces-destination` |

Spans then land in the shared `aws/spans` log group. This step requires
CloudWatch Transaction Search, which bootstrap enables first; if it is somehow
disabled the step is skipped and the summary reports
`gateway_traces: skipped · transaction_search_disabled`.

The step is idempotent (`present` on re-run) and **never fails the bootstrap** —
a telemetry delivery is not worth aborting over. Check the `gateway_traces` entry
in the summary: a `failed` status carries the AWS error code, which is usually a
missing IAM action. The operator credentials need:

```
logs:GetDeliverySource      logs:PutDeliverySource
logs:GetDeliveryDestination logs:PutDeliveryDestination
logs:DescribeDeliveries     logs:CreateDelivery
```

Note that policy decision **counts** (the Governance → Decisions evidence view and
the cutover gate) come from CloudWatch metrics and need none of this — they work
without any enablement. The span channel only adds per-decision detail.

`scripts/teardown.py` deliberately leaves the delivery in place, as it also leaves
the Gateway and Policy engine. To remove it manually:

```bash
aws logs describe-deliveries --region us-west-2   # find the id
aws logs delete-delivery --region us-west-2 --id <delivery-id>
aws logs delete-delivery-source --region us-west-2 --name <gateway-id>-traces-source
aws logs delete-delivery-destination --region us-west-2 --name <gateway-id>-traces-destination
```

## Run locally / 本地运行

```bash
./start.py          # detached development mode
./start.py --prod   # build and run the local production preview
./stop.sh
```

Use `make dev` for the foreground, terminal-attached development stack.

### Console login / 控制台登录

The console can use local accounts without Cognito or any other AWS dependency.
Authentication is disabled until a password is configured, and the console shows
an `AUTH OFF` badge in its top bar while that is the case.

**An unauthenticated console only answers loopback callers.** `./start.py --prod`
binds both servers to `0.0.0.0`, so a reachable deployment must configure a
password; without one, requests to `/api` from any non-loopback address are
refused with `auth.open_console_refused`, and `./start.py` fails its pre-flight
rather than starting. `/api/health` and the sign-in endpoints stay reachable so a
locked-out operator can still see the gate.

```bash
export LAUNCHPAD_AUTH_USERNAME=admin
export LAUNCHPAD_AUTH_PASSWORD='replace-with-a-strong-password'
./start.py
```

Sessions use a 12-hour HttpOnly cookie. `Secure` is set automatically in
production mode (`run_mode: prod`, which `./start.py --prod` sets), together with
an HSTS response header; `LAUNCHPAD_AUTH_COOKIE_SECURE=true` forces it on in
development too. **Both require HTTPS end to end** — a `Secure` cookie is never
sent back over plain HTTP, so sign-in silently fails if TLS terminates somewhere
that then forwards over HTTP without the console knowing.

The same values may be placed in `config/launchpad.yaml` as `auth_username`,
`auth_password`, and `auth_cookie_secure`, following the normal configuration
precedence. Prefer the process environment for the password. Changing the
credentials and restarting the backend invalidates existing sessions.

### Roles: what a member can do / 成员权限

There are two roles. `admin` has the whole console. `member` is **effectively
read-only**: browse agents, registry records and knowledge bases, chat with and
invoke agents, run the retrieval playground, and read observability, memory,
evaluation and governance.

Everything that executes code, changes deployed or cloud state, mints
credentials, or changes governance posture is administrator-only — creating and
deploying agents, the Studio canvas, registry register/edit/import, knowledge-base
mutations, API keys, Cedar policy writes, and the browser / code-interpreter
demos. The authoritative list is the table in
`backend/app/core/route_policy.py`; a route missing from it is refused rather
than served.

This is deliberately restrictive: the console has no per-user data partitioning
yet, so a member who could deploy could also see and mutate every other member's
resources.

### Escape hatches / 应急开关

| Variable | Effect |
|---|---|
| `LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` | Serve an unauthenticated console on a reachable interface. Restores the pre-hardening behavior; use only on a trusted network. |
| `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` | Re-enable local code execution in production (see below). |
| `LAUNCHPAD_AUTH_COOKIE_SECURE=false` | Drop `Secure` when TLS is not actually terminated in front of the console. |

There is no switch that disables role authorization: a flag that turns
authorization off is the vulnerability. Correcting a misclassified route means
editing `route_policy.py`.

### Dependency and image supply chain / 依赖与镜像供应链

**Requirements must be pinned.** `spec.requirements` entries have to name one
immutable artifact — `name==version`, a direct URL with `#sha256=`, or
`pkg @ git+https://…@<40-char commit>`. A range is refused at validation with the
required form in the message. The platform's own requirement lists keep ranges
deliberately; reproducibility comes from the lockfile below, not from hand-pinning
them.

Existing agents may predate this. Nothing breaks until their next deploy — check
with:

```bash
cd backend && uv run python scripts/migrate_pin_requirements.py
cd backend && uv run python scripts/migrate_pin_requirements.py --apply
```

The same script lists git skill records with no recorded commit; those need a
**re-import** from the Registry, because a commit SHA needs a fetch.

**Every zip build is locked.** The package stage resolves the declared
requirements with `uv pip compile --generate-hashes` for the deploy target
(aarch64, Python 3.13) and installs with `--require-hashes`, so a substituted or
re-uploaded distribution fails the build instead of shipping. The lock travels
inside the deployment zip as `requirements.lock`. **The backend therefore needs the
`uv` CLI on PATH and access to the package index at deploy time**; if the resolve
fails, the deploy fails — there is no fall back to an unverified install.

**Container images are scanned and deployed by digest.** ECR scans on push, and
the package stage refuses to continue when the image carries findings at or above
`image_scan_block_severities`:

| Setting | Default | Effect |
|---|---|---|
| `image_scan_enabled` | `true` | Set false to skip the gate (the job log then says the image was not scanned). |
| `image_scan_block_severities` | `["CRITICAL"]` | Severities that block a deploy. |
| `image_scan_timeout_s` | `300` | How long to wait for the scan; a timeout is logged, not treated as clean. |

Deployment references the image by immutable digest, not by its `{agent}-v{version}`
tag, and the digest is recorded on the deployment. Image tags stay **mutable** on
purpose: packaging runs before the version is bumped, so a re-publish pushes the
same tag twice and an immutable-tag policy would fail that push.

> **Applies to both stacks.** Scan-on-push is a CDK change, so `make bootstrap`
> has to be run in `us-west-2` **and** on the `us-east-1` host. Until it is, the
> gate on that host will report that it could not read a scan.

Not implemented: SBOM generation, build provenance/attestation, image signing,
approved-mirror enforcement, and skill *content* review. Pinning makes a source
immutable, not trustworthy.

### Local code execution / 本地代码执行

The Studio local-debug endpoints (`/api/execute`, `/api/execute/stream`, and the
`/api/conversations` multi-turn surface) run **caller-supplied Python on the
server**. They are therefore **disabled in production mode**, and Studio local
debug plus AI Fix stop working there. Set
`LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` to accept the risk.

In development the subprocess gets a scrubbed environment (an allowlist, so the
ledger URL, `LAUNCHPAD_*` settings and your shell's secrets do not reach it) plus
memory/CPU/process/file-size ceilings.

It still runs as the backend user by default, and **still reaches your AWS
credentials** — on EC2 those arrive from the instance metadata service over the
network, so scrubbing the environment does not remove them. To close that:

```bash
sudo scripts/setup_exec_env.sh --hardened   # Linux only
```

That creates a dedicated unprivileged account and a firewall rule denying it
egress to the metadata endpoint, then prints the two settings to add. Note the
trade-off it describes: the default Bedrock Mantle path mints its bearer token
from the ambient credentials, so a credential-less subprocess requires an
explicit `bedrock_api_key` / `openai_api_key` with each local-debug request.

A full sandbox (non-root container, seccomp, constrained egress) is **not**
implemented; production-disabled is the mitigation there.

### Self-service accounts and User Management

While the gate is enabled, the login page also offers **registration**: a
visitor supplies a username, a **company email**, and a password. By default the
new account lands in **`pending`** and cannot sign in until an admin approves it;
the **7-day** validity window starts at approval. The built-in admin above is
never stored in the database, so it cannot be locked out.

Public / disposable mail domains (Gmail, QQ, 163, Outlook, mailinator, …) are
rejected. Tune the policy with:

```bash
export LAUNCHPAD_AUTH_REGISTRATION_ENABLED=true          # false closes registration
export LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL=true # false = active on registration
export LAUNCHPAD_AUTH_REGISTRATION_VALID_DAYS=7          # validity granted at approval
# allow list wins when non-empty; otherwise the built-in block list applies
export LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS='["your-company.com"]'
export LAUNCHPAD_AUTH_BLOCKED_EMAIL_DOMAINS='["gmail.com","qq.com"]'
```

The admin sees a **User Management** module (`/users`) with an approval queue
(`AWAITING APPROVAL` tile + `PENDING` filter, **APPROVE** / **REJECT** per row),
registration statistics, and per-account actions: extend validity (+7 / +30 /
custom days or an absolute date), disable / enable, change role, reset the
password (shown once), and delete. Expiry and disabling are enforced on every request, so an account
loses console access immediately — it does not have to wait for the session
cookie to lapse.

## Production deployment / 生产部署

`./start.py --prod` is a local preview: it builds the frontend, serves the built
bundle, drops backend auto-reload, and binds to `0.0.0.0`. For a host that stays
up, supervise the two processes instead and keep the console behind an edge that
terminates TLS. The reference deployment (workshop EC2 + CloudFront) is specified
in `.trellis/spec/launchpad/remote-production-deployment.md`; its shape is:

```text
browser → CloudFront (TLS, no caching, all methods, injects a secret origin header)
            └─ nginx :80 on the instance — rejects any request without that header
                 ├─ /api/, /v1/ → 127.0.0.1:8000   (backend, proxy_buffering off for SSE)
                 └─ /,  /assets/ → 127.0.0.1:5173  (vite preview serving frontend/dist)
```

**1. Supervise the two processes.** The backend unit carries the auth
configuration; nothing else enables the gate for you:

```ini
# /etc/systemd/system/launchpad-backend.service   (excerpt)
[Service]
WorkingDirectory=/home/ubuntu/workspace/agentcore_launchpad/backend
Environment=LAUNCHPAD_RUN_MODE=prod
Environment=LAUNCHPAD_AUTH_USERNAME=admin
Environment=LAUNCHPAD_AUTH_PASSWORD=<strong-password>
Environment=LAUNCHPAD_AUTH_COOKIE_SECURE=true
ExecStart=/home/ubuntu/.local/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
```

```ini
# /etc/systemd/system/launchpad-frontend.service  (excerpt)
[Service]
WorkingDirectory=/home/ubuntu/workspace/agentcore_launchpad/frontend
Requires=launchpad-backend.service
ExecStart=/usr/bin/npm run preview -- --host 127.0.0.1 --port 5173 --strictPort
Restart=on-failure
```

`vite preview` serves `frontend/dist`, so **every frontend change needs
`npm run build` before the restart**. Both processes bind to `127.0.0.1`: only
the reverse proxy is exposed.

**2. Close the origin.** CloudFront adds a custom header (e.g.
`X-Launchpad-Origin-Key`) and nginx refuses anything without it, so the public
instance IP cannot bypass the CDN:

```nginx
if ($http_x_launchpad_origin_key != "<shared-secret>") { return 403; }
proxy_set_header X-Forwarded-Proto https;   # TLS terminates at CloudFront
```

Because TLS terminates at the edge, keep `LAUNCHPAD_AUTH_COOKIE_SECURE=true`;
over plain HTTP the browser would drop the session cookie.

**3. Update an existing host.**

```bash
cp data/launchpad.db data/launchpad.db.bak-$(date +%Y%m%d-%H%M)
git merge --ff-only origin/main
cd backend && uv sync && cd ..
cd frontend && npm run build && cd ..          # required: preview serves dist/
sudo systemctl restart launchpad-backend launchpad-frontend
curl -s localhost:8000/api/auth/status          # expect auth_required: true
```

New ledger tables (such as `users`) are created on startup, so no migration step
is needed. Registration is open as soon as the gate is on — set
`LAUNCHPAD_AUTH_REGISTRATION_ENABLED=false` or pin
`LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS` if the deployment should not accept
requests from anyone who has the URL.

## Teardown / 资源清理

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # list what would be removed
uv run python ../scripts/teardown.py --yes       # delete (memory → registry → CDK stack)
```

Deletion is best-effort and ordered dependents-first; the S3 bucket auto-empties
and the ECR repo force-deletes via the stack.
