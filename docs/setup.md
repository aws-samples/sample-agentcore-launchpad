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

Demo user passwords are generated and stored in `config/launchpad.yaml`
(**gitignored** — treat as local secrets; a sanitized `config/launchpad.example.yaml` is committed).

## Run locally / 本地运行

```bash
./start.py          # detached development mode
./start.py --prod   # build and run the local production preview
./stop.sh
```

Use `make dev` for the foreground, terminal-attached development stack.

### Optional console login

The console can use local accounts without Cognito or any other AWS dependency.
Authentication is disabled until a password is configured — and the console shows
an `AUTH OFF` badge in its top bar while that is the case. Note that
`./start.py --prod` binds both servers to `0.0.0.0`, so anything reachable must
have the gate on:

```bash
export LAUNCHPAD_AUTH_USERNAME=admin
export LAUNCHPAD_AUTH_PASSWORD='replace-with-a-strong-password'
./start.py
```

Sessions use a 12-hour HttpOnly cookie. For an HTTPS deployment, also set:

```bash
export LAUNCHPAD_AUTH_COOKIE_SECURE=true
```

The same values may be placed in `config/launchpad.yaml` as `auth_username`,
`auth_password`, and `auth_cookie_secure`, following the normal configuration
precedence. Prefer the process environment for the password. Changing the
credentials and restarting the backend invalidates existing sessions.

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
