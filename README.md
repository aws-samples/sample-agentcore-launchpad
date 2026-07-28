# AgentCore Launchpad

A production-grade **enterprise agent platform** built on Amazon Bedrock
AgentCore. It is a customer-deliverable sample asset — not a throwaway demo —
that wires the core AgentCore components to real APIs and real resources in
your own AWS account, and gives users a single place to **create an agent,
deploy it to AgentCore Runtime, and consume it** over chat or HTTP.

- 中文版: [README.zh-CN.md](README.zh-CN.md)

## What it is

Launchpad is one console (React) over one FastAPI backend, plus shared AWS
infrastructure (CDK) and a vendored Strands Studio sub-app. It delivers:

- **Three creation methods, one deploy pipeline.** Users create agents via
  **方式A — Claude Agent SDK** (packaged into an ARM64 container image),
  **方式B — Managed Harness** (declarative `CreateHarness` — model, prompt,
  tools, skills, memory; no code, no build), or **方式C — Strands Studio**
  (visual drag-and-drop canvas that generates Strands code). All three
  converge into the same five-stage pipeline and land on AgentCore Runtime
  (方式A/C) or the managed Harness service (方式B).
- **Registry console.** A visual front end over AgentCore Registry for
  cataloguing and discovering the three asset types — agents (A2A), MCP tools,
  and skills — with submit → approve lifecycle actions.
- **Chat playground + public `/v1` API.** Pick any active agent and talk to it
  with streaming responses, multi-turn history, and session-scoped memory. The
  same invoke chain is exposed as an `X-Api-Key`-authenticated `/v1` surface
  for system integration, so both entrances behave identically.
- **Governance.** Cedar policies enforced at the AgentCore Gateway (Allow/Deny
  with the deciding policy id), a decision log, and end-to-end traces read from
  CloudWatch Logs across the legacy `aws/spans` destination and unified
  per-agent runtime log groups.
- **Evaluation & optimization.** Real batch and online evaluation with 13
  built-in evaluators plus custom LLM-as-a-judge, failure-analysis insights,
  and an optimization loop that produces control/treatment **configuration
  bundles**, runs A/B and canary traffic through the gateway, and promotes the
  winner.

For how these map onto AgentCore services, see [docs/architecture.md](docs/architecture.md).

## Quickstart (~10 minutes)

### Prerequisites

- AWS account with Bedrock AgentCore previews enabled (Runtime, Harness,
  Registry, Gateway, Policy, Evaluation) in `us-west-2`
- Credentials with administrator-level access (`aws sts get-caller-identity` works)
- `uv` ≥ 0.8, Node.js ≥ 20 (`npm`), AWS CDK CLI v2 (`npm i -g aws-cdk`),
  Docker (ARM64-capable — only needed for the 方式A container path)
- One-time CDK bootstrap per account/region: `cdk bootstrap aws://<account>/us-west-2`

### 1. Install dependencies

```bash
cd backend  && uv sync && cd ..
cd frontend && npm install && cd ..
cd infra    && uv sync && cd ..
```

### 2. Bootstrap shared infra + AgentCore singletons

```bash
make bootstrap          # = cd backend && uv run python ../scripts/bootstrap.py
```

This deploys the CDK stack `launchpad-base` (only when missing), ensures the
AgentCore registry, memory, gateway and policy engine once, and writes
`config/launchpad.yaml`. It is **idempotent** — a second run prints `reused`.

### 3. Run locally

```bash
./start.py          # background development servers with auto-reload
./start.py --prod   # build the platform frontend, then run the local production preview
./stop.sh           # stop only processes owned by start.py
```

Open the console at `http://localhost:5173`; API docs are proxied at
`http://localhost:5173/api/docs`. Use `make dev` when you want the same stack
attached to the current terminal.

### 4. Create your first agent

The fastest path is a **Managed Harness** agent (方式B) — it deploys in about
30 seconds with no build step. Create it from the console's **Create Agent**
page, or with curl:

```bash
curl -s -X POST localhost:8000/api/agents -H 'Content-Type: application/json' -d '{
  "name": "hr-assistant",
  "method": "harness",
  "system_prompt": "You are a concise HR assistant. Use the hr-database tool for employee questions.",
  "tools": [{"type": "gateway", "name": "hr-database"}],
  "memory": {"short_term": true, "long_term": true}
}'
# → 202 {"agent": {...}, "job_id": "…", "deployment_id": "…"}
```

Poll the deploy job or the agent until it is `active`:

```bash
curl -s localhost:8000/api/agents/<AGENT_ID>          # status: deploying → active
curl -s localhost:8000/api/jobs/<JOB_ID>              # per-stage event feed
```

### 5. Chat with it

From the console **Chat** page, or over the public API — first mint a key:

```bash
curl -s -X POST localhost:8000/api/apikeys -H 'Content-Type: application/json' \
  -d '{"name": "quickstart"}'
# → {"id": "…", "prefix": "lp_live_…", "key": "lp_live_<shown-once>"}

curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: lp_live_<full-key>" -H 'Content-Type: application/json' \
  -d '{"prompt": "How many vacation days does Maya Chen have left?"}'
# → {"agent":"hr-assistant","text":"…","session_id":"…","latency_ms":…}
```

Full API reference (sync + SSE streaming, Python): [docs/api.md](docs/api.md).

## Start and stop

The root lifecycle scripts manage the platform backend and frontend as one
local stack. The standalone vendored Studio is not part of this lifecycle; the
platform's native Studio experience is available at `/create/studio`.

### Background development mode

```bash
./start.py
```

This starts the stack in the background with backend auto-reload. Development
servers bind to `127.0.0.1` by default.

### Local production mode

Production mode builds the platform frontend, serves its optimized bundle, and
runs the backend without auto-reload. Both the UI and API servers bind to
`0.0.0.0`, and **the login gate stays off until you configure a password** — so
enable it in the same step, or the stack is open to everyone who can reach the
host (the console shows an `AUTH OFF` badge whenever that is the case):

```bash
export LAUNCHPAD_AUTH_USERNAME=admin                # built-in admin (config-only, never in the DB)
export LAUNCHPAD_AUTH_PASSWORD='replace-with-a-strong-password'
export LAUNCHPAD_AUTH_COOKIE_SECURE=true            # only behind HTTPS (e.g. CloudFront/ALB)
./start.py --prod
```

`start.py` never enables the gate itself; it only passes the environment
through, so the same variables work with `make dev`, systemd units, or any other
supervisor.

| Service | Default URL | Port override |
|---|---|---|
| Platform console | `http://localhost:5173` | `PLATFORM_UI_PORT` |
| Platform API | `http://localhost:8000` | `PLATFORM_API_PORT` |

Override UI and API bindings with `LAUNCHPAD_HOST` and
`LAUNCHPAD_API_HOST`. The launcher fails before starting if a configured port
is already occupied.

With the gate on, the login page also offers **registration** (username +
company email + password). A new account lands in `pending` and **cannot sign in
until an admin approves it**; the 7-day validity window starts at approval. The
admin gets a **User Management** module (`/users`) with the approval queue,
statistics, extend/disable/role/reset-password/delete actions.

```bash
export LAUNCHPAD_AUTH_REGISTRATION_ENABLED=true           # false closes registration entirely
export LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL=true  # false = usable at registration
export LAUNCHPAD_AUTH_REGISTRATION_VALID_DAYS=7           # validity granted on approval
export LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS='["your-company.com"]'   # allow list wins when set
```

Public and disposable mail domains are rejected by default. Two caveats:
`LAUNCHPAD_AUTH_COOKIE_SECURE=true` over plain HTTP makes the browser drop the
session cookie, and rotating `LAUNCHPAD_AUTH_PASSWORD` invalidates **all**
sessions (the cookie signing key derives from it). The public `/v1` surface keeps
its own `X-Api-Key` auth and is never guarded by the console cookie.

For a real deployment (systemd units, nginx origin-key gate, CloudFront, and the
update procedure) see
[docs/setup.md](docs/setup.md#production-deployment--生产部署) and
`.trellis/spec/launchpad/remote-production-deployment.md`.

### Stop the stack

```bash
./stop.sh
```

`start.py` records process ownership and per-service logs under `.run/`.
`stop.sh` gracefully terminates only those recorded process groups, so it does
not kill unrelated services that happen to use similar commands. Re-running
`start.py` while its stack is healthy is idempotent and prints the active URLs.

For terminal-attached development, use `make dev` and stop it with `Ctrl+C`.

## Repo layout

| Path | What lives here |
|---|---|
| `backend/` | FastAPI backend — deploy pipeline, invoke chain, evaluation & optimization, SQLite ledger |
| `backend/app/routers/` | Console `/api` + public `/v1` endpoints |
| `backend/app/deployer/` | Unified pipeline + per-method stages (harness, zip_runtime, container, studio) |
| `frontend/` | React console (Vite) — Overview, Create Agent, Registry, Chat, Observability, Evaluation, Governance |
| `infra/` | AWS CDK app — the `launchpad-base` shared stack |
| `apps/studio/` | Vendored Strands Studio sub-app (方式C), rewired to the platform pipeline |
| `start.py`, `stop.sh` | Background local-stack lifecycle, health checks, PID ownership and logs |
| `scripts/` | `bootstrap.py`, `teardown.py`, `dev.sh`, `verify.sh`, `i18n_check.py` |
| `config/` | `launchpad.example.yaml` (committed); `launchpad.yaml` (generated, gitignored) |
| `docs/` | Setup, API, architecture, troubleshooting, teardown, Studio integration |

## Docs

| Doc | |
|---|---|
| [docs/lab/README.md](docs/lab/README.md) | **Hands-on lab** — the full deploy → test → observe → evaluate → optimize → A/B → govern walkthrough against real AWS (Chinese) |
| [docs/setup.md](docs/setup.md) | Environment setup, bootstrap, teardown ([中文](docs/setup.zh-CN.md)) |
| [docs/architecture.md](docs/architecture.md) | Platform ↔ AgentCore mapping, pipeline, invoke chain ([中文](docs/architecture.zh-CN.md)) |
| [docs/api.md](docs/api.md) | Public `/v1` API reference ([中文](docs/api.zh-CN.md)) |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Verified gotchas and timings ([中文](docs/troubleshooting.zh-CN.md)) |
| [docs/teardown.md](docs/teardown.md) | Demo resources vs shared infra cleanup ([中文](docs/teardown.zh-CN.md)) |
| [docs/studio-integration.md](docs/studio-integration.md) | Strands Studio (方式C) integration |

## Cost notes

Running the demo incurs ordinary AWS usage charges — there is no separate
Launchpad cost. Costs are qualitative and small at demo scale, but scale with
how much you exercise each layer:

- **Runtime / Harness invocations** — every invoke bills model tokens (default
  `global.anthropic.claude-sonnet-4-6`) plus managed runtime/session compute.
- **Container builds (方式A)** — CodeBuild ARM64 build minutes, roughly 2
  minutes per agent build; 方式B (harness) has no build, and 方式C rides the
  faster zip path.
- **Batch evaluation** — LLM-as-a-judge calls (model tokens) scale with
  evaluators × dataset items; insights runs are heavier and longer.
- **CloudWatch Transaction Search** — trace/span ingestion and storage while
  observability is enabled.
- **Storage** — S3 artifact zips and ECR container images accumulate per agent
  build; AgentCore Memory stores session events and extracted preferences.

**Delete demo agents after use** (console, or `DELETE /api/agents/{id}`), then
run `scripts/teardown.py` to remove the shared infra. See
[docs/teardown.md](docs/teardown.md).
