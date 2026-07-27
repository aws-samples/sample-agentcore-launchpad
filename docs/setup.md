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
Authentication is disabled until a password is configured:

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
visitor supplies a username, a **company email**, and a password, and gets a
`member` account that is valid for **7 days** and can sign in immediately. The
built-in admin above is never stored in the database, so it cannot be locked out.

Public / disposable mail domains (Gmail, QQ, 163, Outlook, mailinator, …) are
rejected. Tune the policy with:

```bash
export LAUNCHPAD_AUTH_REGISTRATION_ENABLED=true   # false closes registration
export LAUNCHPAD_AUTH_REGISTRATION_VALID_DAYS=7   # default account validity
# allow list wins when non-empty; otherwise the built-in block list applies
export LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS='["your-company.com"]'
export LAUNCHPAD_AUTH_BLOCKED_EMAIL_DOMAINS='["gmail.com","qq.com"]'
```

The admin sees a **User Management** module (`/users`) with registration
statistics and per-account actions: extend validity (+7 / +30 / custom days or an
absolute date), disable / enable, change role, reset the password (shown once),
and delete. Expiry and disabling are enforced on every request, so an account
loses console access immediately — it does not have to wait for the session
cookie to lapse.

## Teardown / 资源清理

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # list what would be removed
uv run python ../scripts/teardown.py --yes       # delete (memory → registry → CDK stack)
```

Deletion is best-effort and ordered dependents-first; the S3 bucket auto-empties
and the ECR repo force-deletes via the stack.
