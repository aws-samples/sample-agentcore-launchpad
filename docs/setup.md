# Setup / 环境搭建

## Prerequisites / 前置条件

- AWS account with Bedrock AgentCore previews enabled (Runtime, Harness, Gateway, Policy, Evaluation) in `us-west-2`; Agent Registry is GA (`agent-registry` namespace since 2026-08-06, see [registry-ga-migration.md](registry-ga-migration.md)) and needs no preview enablement
- Credentials with administrator-level access (`aws sts get-caller-identity` works)
- `uv` ≥ 0.8, Node.js ≥ 20 (`npm`), AWS CDK CLI v2 (`npm i -g aws-cdk`), Docker (ARM64-capable, phase 5; [finch](https://github.com/runfinch/finch) works too, see below)
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
| Cognito user pool | `launchpad-users` (+ groups `platform-admin`, `hr-analyst`, demo users `admin`/`demo`) |
| IAM execution role | `launchpad-agent-execution-role` |
| AgentCore Registry | `launchpad-registry` |
| AgentCore Memory | `launchpad_memory` (short-term events + semantic, user-preference, session-summary and episodic long-term strategies; re-running bootstrap adds missing strategies to an existing memory) |
| AgentCore Gateway | `launchpad-gw-<suffix>` |
| Managed AgentCore CLI | `data/agentcore-cli/` (`@aws/agentcore@0.21.1`) |

Demo user passwords are generated and stored in `config/launchpad.yaml`
(**gitignored** — treat as local secrets; a sanitized `config/launchpad.example.yaml` is committed).

### Policy is operator-managed / Policy 由用户显式管理

Bootstrap creates the shared Gateway but deliberately does **not** create a
Policy Engine, create Cedar policies, attach an Engine, or select a Gateway
enforcement mode. Existing Policy resources and attachments are also left
untouched when bootstrap is rerun.

Use Governance to opt in: manage the selected Gateway, create or select an
Engine, and choose its initial Gateway attachment mode (`ENFORCE` by default or
`LOG_ONLY`). `ENFORCE` is default-deny, so create and review permitting policies
before relying on Gateway traffic.

Bootstrap still enables CloudWatch Transaction Search for general
observability. It does not create the per-Gateway CloudWatch Logs delivery used
for detailed Policy decision spans. Policy decision counts come from
CloudWatch metrics and remain available after Policy is configured; detailed
decision rows require separately managed trace delivery.

## Run locally / 本地运行

```bash
./start.py          # detached development mode
./start.py --prod   # build and run the local production preview
./stop.sh
```

Use `make dev` for the foreground, terminal-attached development stack.

Every setting named in this document is a key in `config/launchpad.yaml`, or the
same name upper-cased with a `LAUNCHPAD_` prefix in the process environment
(`database_url` → `LAUNCHPAD_DATABASE_URL`). Precedence is defaults <
`config/launchpad.yaml` < environment < init kwargs, so an env var always wins
over the file bootstrap generated. Three keys decide how the backend itself
starts:

| Setting | Default | Effect |
|---|---|---|
| `database_url` | `sqlite:///<repo>/data/launchpad.db` | SQLAlchemy URL of the ledger. The default is a single SQLite file under `data/`; it holds identifiers and derived progress only — AWS stays the source of truth — so a lost ledger costs console history, not resources. Point it elsewhere to relocate the file. |
| `cors_origins` | `["http://localhost:5173", "http://127.0.0.1:5173"]` | Browser origins allowed to call `/api`. The defaults cover the dev frontend under either loopback spelling; add your own origin when the console is served from a different host or port. `./start.py --prod` serves console and API from one origin and needs no entry. |
| `agentcore_read_timeout_s` | `1000` | boto read timeout for the AgentCore data-plane client (`backend/app/services/agentcore/client.py:30`). A synchronous AgentCore invoke may run for **up to 15 minutes**, so this has to stay above that service limit — botocore's 60 s default abandons a slow buffered agent before it can return its final response. |

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

#### Single sign-on (OIDC) / 单点登录

With the login gate on, an administrator can add OpenID Connect identity providers
(Keycloak, Authentik, Okta, Entra ID, Google or any OIDC IdP) under **Configure →
Single sign-on**; each enabled provider gets a button on the login page. The local
form stays available, and the built-in admin always signs in locally as the way back
in. Two deployment settings matter:

- **`public_base_url`** (`LAUNCHPAD_PUBLIC_BASE_URL`) must be the console's URL as the
  browser sees it (for the reference deployment, the CloudFront URL), because the
  redirect URI registered at the IdP is built from it:
  `<public_base_url>/api/auth/oidc/<provider-id>/callback`. The provider page shows
  the exact value to copy. A wrong value makes the IdP refuse the redirect.
- **The client-secret key.** Client secrets are stored Fernet-encrypted. In production
  set `LAUNCHPAD_SSO_SECRET_KEY` to a Fernet key
  (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
  in the backend's environment (the systemd unit below). Without it, the backend creates
  `data/sso-secret.key` (mode 0600) next to the SQLite ledger on first use. Back that file
  up with the ledger: if it is lost, the stored secrets cannot be read, and every provider's
  secret has to be entered again. A secret given as a Secrets Manager ARN is read with the
  hub's credentials and is not encrypted locally.

An SSO account has no local password: its password cannot be reset or changed in the
console. Its global role and project memberships follow the provider's group mappings
at every sign-in; changes an administrator makes by hand take precedence.

Step-by-step provider guides: [IAM Identity Center (SAML 2.0)](sso-identity-center.md)
and [Keycloak (OIDC or SAML 2.0)](sso-keycloak.md).

### Roles: what a member can do / 成员权限

There are two roles, and since 2026-08-11 they differ in exactly one place:
**user management** (`/users`) is administrator-only; **everything else in the
console is open to members** — registry register/edit/import, knowledge-base
mutations, evaluation datasets/evaluators/runs, AB experiments and canaries,
Cedar policy writes, API keys, the Studio canvas, and the browser /
code-interpreter demos. The authoritative list is the table in
`backend/app/core/route_policy.py`; a route missing from it is refused rather
than served. (Local code execution additionally stays disabled in prod run mode
for every role unless `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED` opts in.)

A small set of capabilities remains **revocable per user** in the User
Management console: the agent lifecycle (deploy, import, delete, convert) and
starting evaluation/insights runs. Members hold them by default; revoking them
shuts off deploys and billable eval jobs for that account.

Note the console has no per-user data partitioning by default: in a `shared`
workspace every member sees and can mutate the same shared resources, so hand
member accounts only to people you would let operate the environment. An admin
can switch a workspace to `isolated` (Workspaces → detail → **Member visibility** card), after
which each member sees only the agents (with their observability traces, memory
actors and A2A registry records), knowledge bases, managed memories, evaluation
assets, experiments, canaries and Skill Lab jobs/task sets they created, plus
admin-created and creator-unknown ones (discovered runtimes and system presets are
creator-unknown, so they stay shared). A member also lists and toggles only their own
`/v1` API keys, and a member-minted key reaches only the agents that member can see.
MCP / skill registry records and gateways stay shared in both modes.

### System-managed presets / 系统托管预置

The Create page's **SYSTEM PRESETS** panel lists the platform-owned presets
(currently `aws-agent-solution-architect`, a managed Harness). Nothing is installed
by bootstrap, startup or any page read: an **administrator** installs a preset per
workspace with the panel's INSTALL button (or `POST
/api/system-agents/<key>/install`), which is a billable deploy through the normal
pipeline (~30 s). The workspace must be `ready` (bootstrap done, `artifacts_bucket`
and `execution_role_arn` present, per-agent execution roles enabled). A first install
uses the preset's defaults — for the architect: `global.openai.gpt-6-astra` on native
Bedrock (GPT-6 Astra on the global cross-region inference profile),
`max_tokens: 65536` per model call, `reasoning_effort: high` and `max_iterations: 100`. Afterwards an
administrator changes the stored settings with the panel's **CONFIGURE** button —
which opens the same configure page an existing agent's EDIT uses (the table's EDIT
on the preset's row does the same for an administrator) — or the same install API
body: model source/id, max output tokens per model call (the
Harness `bedrockModelConfig.maxTokens` — one response's ceiling, not a session or
spend cap), reasoning effort (OpenAI GPT-5.x on native Bedrock only), system prompt,
max iterations, timeout and existing knowledge bases to mount (verified in the
workspace during provision; none is created for you). SAVE & RE-PUBLISH posts only the changed
fields to the maintenance route (never the ordinary redeploy) and runs the normal
update job; members see the same page read-only (VIEW SETTINGS). Re-publishes and
bundle updates keep the stored settings — a preset installed by an
earlier build keeps its model and prompt until you save a change or USE PRESET
DEFAULTS. With nothing changed the same page offers RE-PUBLISH, which re-publishes
in place with the stored settings — use it to retry a failed deploy or after an
out-of-band change (there is no separate repair button); UNINSTALL queues a teardown
job — the preset shows UNINSTALLING and keeps its identity until the Harness and
role are gone, a failed teardown shows its reason with RETRY UNINSTALL, and install
or repair are refused meanwhile. The preset runs with persistent memory disabled
(AgentCore memory only — chat transcripts stay in the ledger and CloudWatch logs are
kept) and on its own restricted execution role (never the shared role). Members can chat with the preset
like any agent but see a SYSTEM chip with the edit/re-publish/convert/delete actions
disabled, and the backend refuses those calls regardless of permissions; a knowledge
base mounted on the preset cannot be force-deleted until an administrator detaches
it through the preset. If an ordinary agent already holds the reserved name, the
install is refused — delete or rename that agent first; the preset never adopts it.
The preset's versioned Skill is also listed in the Registry as its own record:
every install or re-publish made with this build registers it in the deploy's
register stage (an explicit `POST /api/system-agents/<key>/skill-registration` does
the same from the API). That points an `AGENT_SKILLS` record at the release already
published under `system-skills/` (nothing is uploaded and the Harness is not
re-published), submits it for review, and it becomes mountable by other agents only
after you **approve it in the Registry** — the Registry is where you manage the
record; the System presets card shows no Skill-record controls. After a bundle update
the record rolls forward, which needs approval again. The record shows a SYSTEM chip: nobody edits, re-imports or
deletes it from the console, members can view and mount it, administrators operate its
approval; uninstalling the preset keeps the record.
**Live smoke is still pending** (see architecture.md → *System-managed presets*).

### Architect assistant / 架构助手

> **Upgrade note:** this release changes the console session cookie to version 2
> (bound to the account id). Every signed-in user — members and the administrator —
> is signed out once and must log in again; nothing else changes for them.

Once the `aws-agent-solution-architect` preset is ACTIVE, every member of the workspace
can open the **architect assistant** (`/create/assistant`, linked from the Managed
Harness entrance card and from the SYSTEM PRESETS panel). Paste your Workshop output,
let the assistant confirm the baseline and ask only the impactful missing questions,
and review its proposal for **one new managed Harness business agent**. The proposal
only references tools, S3 skills and knowledge bases that already exist in the
workspace (APPROVED registry records, ACTIVE managed KBs) and shows the **exact
bindings** it will deploy (gateway ARN and auth identity, skill S3 path and content
digest of every file in the skill directory, the workspace's shared memory or none);
approved skills deploy from an immutable copy of exactly those bytes in the workspace's
artifacts bucket. It is inert until you click
**APPROVE & DEPLOY** — a separate, billable action that requires the `agents.deploy`
permission (re-checked, together with your account and workspace grant, at the moment
of execution) and creates the agent through the normal deploy job in the workspace's
account and Region; if any bound resource changed since you reviewed it, the approval
is refused and the job itself fails closed before touching AWS. Editing a proposal
creates a new revision that needs its own approval; cancelling makes it
non-executable. Memory is `disabled` or the workspace's existing shared memory (with
all its strategies) — nothing in between. Mounting a knowledge base requires the
workspace's existing KB gateway; where there is none, it is manual work. The assistant
never edits or deletes existing agents and creates no knowledge base, gateway or
evaluator — those, and the golden tests it recommends, are guidance for manual
implementation. Conversations are private to the account that opened them (by
immutable account id, not by username), per workspace, and are hidden from other
members in Observability too. **The live smoke of this flow
(a real conversation, proposal and approval) is still pending.**

**Evaluation assets from a proposal (SE-047).** Below the proposal panel, EVALUATION
ASSETS lets the conversation owner prepare a typed plan from any shape-valid proposal
revision (including an already-approved one), review the mapping table (golden tests →
Dataset scenarios with their steps and references; recommendations → existing
evaluator / new LLM judge / new derived / new code rules / runner check / human review /
metric baseline / external control), confirm or block the review-required scenarios
(legacy golden tests are drafted as single turns and are never parsed into
procedures — multi-session tests need typed steps), edit as JSON (every save is a new
plan revision) and — as an **administrator who owns the conversation** — CREATE ASSETS
after acknowledging the disclosure. Creation
registers a local Dataset, AgentCore evaluators and, for code rules, one Lambda with its
own role; it does not deploy, run, sync to AWS Datasets, enable online evaluation or
call a model, and "created" does not mean passed. Members can prepare and edit; the
create button tells them why it is disabled. Retry resumes the same resources; DELETE
OWNED CLOUD ASSETS removes exactly what the operation created (the local Dataset stays —
remove it in Evaluation → Datasets). Owned artifacts are named `launchpad-evalfn-<op>`
(Lambda, its role, `/aws/lambda/launchpad-evalfn-<op>`) and `launchpad-evalop-<op>` (the
additive policy on the workspace execution role); `scripts/teardown.py` does not know
them — clean them through the operation before tearing the stack down. **Live creation
against a real account is still pending; see architecture → Evaluation-assets plan for
the unverified AWS assumptions.**

### Escape hatches / 应急开关

| Variable | Effect |
|---|---|
| `LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` | Serve an unauthenticated console on a reachable interface. Restores the pre-hardening behavior; use only on a trusted network. |
| `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` | Re-enable local code execution in prod run mode (see below). |
| `LAUNCHPAD_STUDIO_EXEC_BACKEND=docker` | Run local-debug code in one-shot Docker containers instead of host subprocesses — also serves the endpoints in prod run mode (see below). |
| `LAUNCHPAD_AUTH_COOKIE_SECURE=false` | Drop `Secure` when TLS is not actually terminated in front of the console. |

There is no switch that disables role authorization: a flag that turns
authorization off is the vulnerability. Correcting a misclassified route means
editing `route_policy.py`.

### Per-agent execution roles / 按 Agent 的执行角色

Each deployed agent gets its own IAM execution role, derived from its spec, instead of
every agent assuming one shared `launchpad-agent-execution-role`. The point is
isolation: on the shared role, any agent could mount any other agent's file systems,
read every agent's skill bundles, retrieve from every knowledge base, and rewrite
gateway routing.

Roles are named `launchpad-agent-{name}-{agent-id-prefix}` and tagged
`launchpad:agent-id`. They are created in the `provision` stage, reconciled on
re-publish (a dropped capability shrinks the policy), and deleted with the agent.

| Setting | Default | Effect |
|---|---|---|
| `per_agent_execution_roles` | `true` | Set false to fall back to the shared role. |
| `agent_role_count_warn_threshold` | `800` | Warn once this many roles exist. |

**The IAM default quota is 1000 roles per account**, and this consumes one per agent.
At demo scale that is not a concern, but hitting it would surface as an
otherwise-mysterious deploy failure.

**Existing agents keep working on the shared role.** Migration is a **re-publish** —
not a hand-rolled `UpdateAgentRuntime`, which resets omitted fields and would silently
clear file-system mounts, protocol config or the environment. Check what is
outstanding with:

```bash
cd backend && uv run python scripts/migrate_agent_roles.py
```

Run `scripts/migrate_pin_requirements.py --apply` **first**: a re-publish re-validates
the spec, so an unpinned requirement is now refused.

> **Why the shared role still exists and still carries broad grants.** It backs
> agents that have not been re-published yet. Reducing it before every agent has moved
> would strip grants from agents still using it. That reduction is deliberately not
> done yet, and neither is the trust-policy `aws:SourceArn` condition (off pending a
> probe of whether AgentCore sends that key). Note also what per-agent roles do **not**
> give you: memory stays a single shared instance partitioned by actor id, not by IAM,
> and the account's 1000-role quota is now consumed one role per agent.

**A green deploy does not prove a policy is correct.** These policies are scoped per
spec, and an over-tight statement fails at **invoke** time, not deploy time. After
migrating, invoke each agent and check CloudTrail for `AccessDenied`.

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

> **Expect the default to block on day one.** A scan of a current demo image found
> **4 CRITICAL** findings, all unpatched OS packages in the Debian base image
> (`glibc`, `perl`) rather than anything this project installs — so with the
> `["CRITICAL"]` default, enabling scanning stops container deploys until the base
> image ships fixes. The block message names the CVEs and packages so you can tell
> whose problem it is. Decide deliberately between rebuilding on a newer base,
> relaxing `image_scan_block_severities` to `[]` (report-only: findings are logged
> on every deploy, nothing is blocked), and accepting the block.

Not implemented: SBOM generation, build provenance/attestation, image signing,
approved-mirror enforcement, and skill *content* review. Pinning makes a source
immutable, not trustworthy.

### Local code execution / 本地代码执行

The Studio local-debug endpoints (`/api/execute`, `/api/execute/stream`, and the
`/api/conversations` multi-turn surface) run **caller-supplied Python on the
server**. They are therefore **disabled in prod run mode**, and Studio local
debug plus AI Fix stop working there. Set
`LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` to accept the risk.

In development the subprocess gets a scrubbed environment (an allowlist, so the
ledger URL, `LAUNCHPAD_*` settings and your shell's secrets do not reach it) plus
memory/CPU/process/file-size ceilings.

It still runs as the backend user by default, and **still reaches your AWS
credentials**. Setting `studio_exec_forward_aws_credentials: false` keeps them
out of the child's environment and sets `AWS_EC2_METADATA_DISABLED=true`, which
is enough to stop the AWS SDKs and CLI from picking up the instance role — but
that variable is an SDK convention, not a boundary. On EC2 credentials arrive
over the network, so code that talks to `169.254.169.254` itself still gets them.
Measured on an EC2 host: with the environment scrubbed, ~20 lines of `urllib`
still returned live instance-role keys. To close that:

```bash
sudo scripts/setup_exec_env.sh --hardened   # Linux only
```

That creates a dedicated unprivileged account and a firewall rule denying **that
uid** egress to the metadata endpoint, then prints the two settings to add. The
same probe under it times out.

**Precondition:** switching the subprocess to another account needs privilege, so
`studio_exec_user` only works when **the backend itself runs as root**. `make dev`
and `start.py` run it as your own account, where the drop would fail — the
execution endpoints therefore refuse with `studio.exec.user_unavailable` (503)
rather than failing mid-run, and you must either run the backend as root or leave
`studio_exec_user` empty (tier 1: limits and environment scrubbing only).

Note the trade-off the script describes: the Bedrock Mantle path mints
its bearer token from the ambient credentials, so a credential-less subprocess
requires an explicit `bedrock_api_key` / `openai_api_key` with each local-debug
request.

#### Docker sandbox backend

Alternatively, run the generated code in a **one-shot Docker container** instead
of a host subprocess:

```bash
scripts/setup_exec_docker.sh          # builds launchpad-studio-exec:latest
export LAUNCHPAD_STUDIO_EXEC_BACKEND=docker   # or studio_exec_backend: docker in launchpad.yaml
```

Each run gets a fresh container with `--cap-drop ALL`, `no-new-privileges`, a
read-only rootfs (tmpfs `/tmp`), the same environment allowlist, and the
memory/CPU/pids/file-size ceilings mapped onto docker flags. Timeouts kill the
container itself, and a startup janitor sweeps any `strands-exec-*` containers a
backend crash left behind. Per-run overhead is ~0.3 s.

Those ceilings, and the image the container comes from, are settings:

| Setting | Default | Docker flag |
|---|---|---|
| `studio_exec_memory_mb` | `2048` | `--memory` and `--memory-swap`, pinned to the same value so the container cannot page around the limit |
| `studio_exec_cpu_seconds` | `300` | `--ulimit cpu=<n>:<n>` — CPU **seconds** burned, not wall clock; wall clock is `execute_timeout_s` |
| `studio_exec_max_processes` | `64` | `--pids-limit` |
| `studio_exec_max_file_mb` | `256` | `--ulimit fsize=<bytes>` — the largest file the generated code may write |
| `studio_exec_docker_image` | `launchpad-studio-exec:latest` | the image `docker run` starts; change it only if you build the sandbox image under another name |

The same four ceilings apply to the **subprocess** backend, where they become
`resource.RLIMIT_*` values lowered in the forked child instead of docker flags
(`backend/app/services/local_exec.py`: `_docker_run_argv` assembles the flags
from line 380, the rlimit list is built around line 557). One asymmetry:
`studio_exec_max_processes` maps to `RLIMIT_NPROC`, which counts processes and
threads **per uid**, so the subprocess backend applies it only when
`studio_exec_user` is set — against the backend's own uid it would count your
whole login session and every thread the child tried to start would fail.

Because the code no longer runs on the control-plane host, **selecting the
docker backend is itself the production opt-in**: `run_mode=prod` +
`studio_exec_backend=docker` serves the local-debug endpoints without
`LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` (an explicit `false` still disables
them). Requirements: a docker daemon the backend user may talk to (`docker`
group), and the image built by the script above.

No Docker? Every container call (this backend, `setup_exec_docker.sh`,
`local_container_smoke.sh`, `e2e_docker_exec.py`) uses `docker` if it is on
PATH, else `finch`; set `LAUNCHPAD_CONTAINER_CLI` (or `container_cli:` in
launchpad.yaml) to pin one. finch's VM shares only `$HOME`, so keep the checkout
under it (or add its `data/` to `additional_directories` in `~/.finch/finch.yaml`);
`--harden-net` stays docker-only.

One boundary does **not** come free: on EC2 with an IMDS hop limit ≥ 2 (the
default on these boxes), a container on the default bridge can still reach the
instance metadata service — which is exactly what keeps the ambient-credential Bedrock paths
working with no API keys. For a genuinely credential-less sandbox:

```bash
sudo scripts/setup_exec_docker.sh --harden-net   # Linux only
```

creates a dedicated `launchpad-exec` bridge network plus a `DOCKER-USER`
iptables rule denying that subnet egress to `169.254.169.254`, then prints the
settings to add (`studio_exec_docker_network`, and
`studio_exec_forward_aws_credentials: false` with the same Mantle trade-off as
above). The backend refuses the hardened-credentials configuration without the
network set, rather than pretending.

A deeper sandbox (AgentCore Code Interpreter re-host) remains unimplemented;
the docker backend is the recommended middle tier.

#### Code generation (AI Fix)

Studio's **AI Fix** hands the failing flow's generated code, its traceback and
the validation errors to a coding agent, which rewrites `generated_agent.py` in
a scratch workspace and hands it back over SSE. Four settings control that:

| Setting | Default | Effect |
|---|---|---|
| `codegen_backend` | `claude` | Which coding agent performs the fix. `claude` (the Claude Agent SDK) is the only registered backend today; an unregistered name fails the request with the list of registered names rather than silently falling back. |
| `codegen_model` | `global.anthropic.claude-sonnet-5` | Model that coding agent runs on. A stronger model repairs more per attempt and costs more per attempt. |
| `codegen_timeout_s` | `180.0` | End-to-end budget for one AI-fix request, repair rounds included. Raise it if fixes are being cut off before the agent finishes writing the file. |
| `codegen_max_repair_rounds` | `2` | How many times a rewrite may be re-validated and re-attempted before the request gives up. Each extra round is another model call on a flow whose first fix did not import cleanly. |

### Skill Lab

Skill Lab evaluates and trains Registry skill records: the vendored SkillOpt
CLIs run as **subprocesses** on the backend host, each task's agent rollout runs
in its own AgentCore Runtime microVM session on `launchpad_skill_lab_worker`,
and the LLM judge calls Bedrock directly. `make bootstrap` provisions both
halves — the dedicated interpreter and the worker image — so the settings below
tune a provisioned Skill Lab rather than switch it on.

| Setting | Default | Effect |
|---|---|---|
| `skill_lab_python` | `<repo>/data/skill-lab-venv/bin/python` | Interpreter the vendored `evaluate_skill.py` / `train.py` / task-set validators run in. Bootstrap builds it from `vendor/skillopt/requirements-launchpad.txt` and rebuilds it when that file changes; the backend process never imports the vendored tree itself. `GET /api/skill-lab/status` reports `venv_ready: false` while this path is missing — that is what an un-provisioned workspace looks like. |
| `skill_lab_max_concurrent_jobs` | `1` (1–4) | How many evaluation/training jobs run at once. Each job is a CLI subprocess plus its own worker sessions, so this trades wall clock against host CPU/memory and pressure on the worker runtime; excess jobs queue instead of failing. |
| `skill_lab_judge_model_id` | `us.openai.gpt-6-sol` | Model that scores rollouts. It must be a Bedrock **Converse inference-profile id** — the `bedrock_chat` judge rejects bare model ids. It also selects the agentic judge's host CLI by family: an `openai.*` id routes to the host `codex` binary with the same profile id (codex >= 0.155, whose model catalog needs an entry for that id), anything else to the host `claude` binary (`runner.judge_exec_route`). Changing this therefore changes which CLI the host must have installed. |
| `skill_lab_target_model_id` | `global.anthropic.claude-opus-5-5` | Default model the skill under test runs on for the `claude_code_exec` target backend — again a Converse inference-profile id. Per-job parameters override it; a blank value is never sent, because an empty `--model` would let the vendored CLI substitute its own non-Bedrock default. |
| `skill_lab_codex_target_model_id` | `global.openai.gpt-6-sol` | The same default for the `codex_exec` target backend. codex resolves models itself: its baked `amazon-bedrock-runtime` provider invokes this **inference-profile id** (a bare `openai.*` id is rejected for on-demand throughput) and reads its metadata from the host model catalog (`skill_lab_codex_catalog_path`), which therefore needs an entry for the exact id. Kept as a separate key so each backend gets an id its CLI accepts. |
| `skill_lab_codex_catalog_path` | `~/.codex/model-catalogs/bedrock-models.json` | Bedrock model catalog read from the backend host and staged into the worker image's codex-home at build time. The file embeds proprietary model instructions and is never committed, so when it is absent the build stages an empty `{}` catalog and logs that it did — codex targets on that image then have no catalog to resolve against. |
| `skill_lab_judge_sandbox` | `bwrap` | Sandbox launcher argv (shlex-split) for the agentic judge's artifact parsers, which run on the backend **host** — the worker microVM cannot run bubblewrap. On hosts where unprivileged `bwrap` is blocked by AppArmor, set `sudo -n bwrap`. `GET /api/skill-lab/status` probes this argv's first word for `agentic_judge_ready`, and the vendored fail-closed boundary check still applies on top. |
| `skill_lab_worker_cli_version` | `2.1.283` | Reports the `claude` CLI version baked into the worker image. |
| `skill_lab_worker_codex_version` | `0.155.1` | Reports the `codex` CLI version baked into the worker image. |

The two `*_version` keys are **mirrors, not inputs**. The build uses the
`ARG CLAUDE_CLI_VERSION` / `ARG CODEX_CLI_VERSION` defaults in
`vendor/skillopt/deploy/agentcore/Dockerfile` — that Dockerfile is what the
image's content hash covers — while these settings only feed the console's
display. **Bump both together:**
`backend/tests/test_skill_lab_foundation.py` asserts the parity, so a one-sided
change fails `make verify`. A version bump also changes the build-context hash,
which is how the next bootstrap knows to rebuild and re-push the worker image.

### Prompt optimization

The experiment RECOMMEND stage asks a 3rd-party provider
(`backend/app/optimization/providers`) to rewrite an agent's system prompt and
tool descriptions from a pinned evaluation run's worst- and best-scoring
sessions. Model ids here are Bedrock Converse inference-profile ids, invoked
through the same workspace client funnel as everything else.

| Setting | Default | Effect |
|---|---|---|
| `prompt_opt_models` | `["global.anthropic.claude-opus-5", "global.anthropic.claude-sonnet-5", "global.anthropic.claude-sonnet-4-6", "us.openai.gpt-5.6-sol"]` | The reflection models the console offers. Add an id here to make it selectable. |
| `prompt_opt_default_model_id` | `global.anthropic.claude-opus-5` | Which of them leads that list and is used when a request names none. |
| `prompt_opt_max_sessions` | `30` (3–100) | How many sessions of the pinned run a provider reads (worst-first plus a best-scoring contrast set). Fewer is cheaper and faster; more gives the reflection more evidence to generalise from. |
| `prompt_opt_max_tokens` | `8192` (512–16000) | Output budget of the reflection call. A two-component reflection (prompt plus several tool descriptions) ran past 4096, and the provider doubles this once on its own when a response comes back truncated. |
| `prompt_opt_read_timeout_s` | `900` | Read timeout of the reflection call. The call streams, so this bounds the gap between chunks rather than the total. botocore's 60 s default is far too low: in practice a large model over 30 sessions ran past it and botocore silently re-sent the whole request five times before failing. |

### Choosing models

Every model the platform itself calls or pre-selects is a **model role**: a
backend setting (`backend/app/services/model_settings.py` lists them with their
constraints). Agents, evaluators and presets that already exist keep the model id
they were created with — a role only decides what *new* ones get.

| Role (setting) | Default | Used for |
|---|---|---|
| `agent_default_model_id` | `global.openai.gpt-6-sol` | The model a new agent on native Bedrock pre-selects in the wizard, and what an agent created through the API without a `model_id` gets. |
| `claude_sdk_model_id` | `global.anthropic.claude-sonnet-5` | The same for the Claude Agent SDK (`container`) method, which drives Claude only: a container spec without a `model_id` gets this, never the agent default. Claude only. |
| `judge_model_id` | `global.openai.gpt-6-sol` | Default judge of a custom / derived LLM evaluator, and of the evaluators the architect assistant drafts. |
| `judge_fallback_model_id` | `global.anthropic.claude-sonnet-5-5` | Retried when AgentCore Evaluation refuses the judge model. Empty = no fallback; the refusal surfaces. |
| `assistant_model_id` | `global.openai.gpt-6-sol` | The model the architect assistant proposes for a new agent. |
| `architect_model_id` | `global.openai.gpt-6-astra` | The system architect preset's model when an admin installs or repairs it. |
| `actor_model_id` | `global.openai.gpt-6-luna` | The simulated user of a persona evaluation run. Must accept Converse forced tool choice. |
| `studio_fallback_model_id` | `global.anthropic.claude-sonnet-5` | What a Studio canvas node without a model generates. The backend scopes that agent's execution role to the same value and the canvas reads it from the server, so the two always agree. Deliberately separate from the agent default: moving it moves every existing flow's model-less nodes. |
| `codegen_model` | `global.anthropic.claude-sonnet-5` | The coding agent behind Studio AI Fix. Claude only. |
| `prompt_opt_default_model_id` | `global.anthropic.claude-opus-5` | See [Prompt optimization](#prompt-optimization). |
| `skill_lab_judge_model_id`, `skill_lab_target_model_id`, `skill_lab_codex_target_model_id` | see [Skill Lab](#skill-lab) | The judge is OpenAI or Claude; the Claude-CLI target Claude only; the codex target OpenAI only. |

A role that runs tools refuses a model measured not to support Converse tool use
(DeepSeek R1). Every id must be a Bedrock id (`[A-Za-z0-9._:/-]`, 3–121 characters).

**Precedence, highest first:** console override > `LAUNCHPAD_<ROLE>` environment
variable (for example `LAUNCHPAD_JUDGE_MODEL_ID`) > the same key in
`config/launchpad.yaml` > the built-in default above.

**The Models page** (`/v2/models`, admin only, under *Configuration*) lists each
role with its effective model, its default (what env / yaml / the built-in
resolve to), an *Overridden* badge, and who changed it and when. Per role an
admin can:

- pick a model or type a custom id and **Save** it — a console override, applied
  at once with no restart and stored in the ledger table `platform_settings`;
- **Reset to default**, which deletes the override;
- **Probe** the selected id: one small real Converse call (plus a tool-use call
  for roles that run tools) in the hub's default workspace, reporting OK with the
  latency, or the AWS error. The probe creates nothing in the account.

Console pickers (the V2 agent wizard, the evaluator editor, the evaluation task
wizard's actor, the assistant proposal editor, the Studio codegen fallback) take
their pre-selection from `GET /api/model-defaults`, so a change needs no frontend
rebuild; if that read fails they fall back to the built-in defaults.

Things to know before changing a role:

- **An override is global to the deployment.** It applies to every workspace and
  member at once. Each deployment has its own ledger, so two deployments sharing
  an AWS account (for example prod and a clone) keep separate overrides.
- **Changing the judge makes scores incomparable** with earlier evaluation runs:
  a different judge grades differently. Keep the old judge while you compare.
- A probe runs in the hub's default workspace; a model available there may still
  be missing in a spoke workspace's account or Region.

### Self-service accounts and User Management

While the gate is enabled, the login page also offers **registration**: a
visitor supplies a username, a **company email**, and a password. By default the
new account lands in **`pending`** and cannot sign in until an admin approves it;
the **7-day** validity window starts at approval. An admin can also create an
account directly in **Users → Add user** (active at once; any role, including
administrator). The built-in admin above has no `users` row; if its password is
changed in the console (**Change password**, the key icon in the top bar) the
configured `auth_password` stops working until the override is cleared with
`cd backend && uv run python scripts/reset_admin_password.py` (break-glass).

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

## Agent-DLC: turning the gate on / 打开放行门

[docs/agent-dlc-design.md](agent-dlc-design.md) is the design; this is the order of
operations. Nothing below happens by itself — a workspace keeps its existing
behaviour (`release_mode: direct`, a deploy goes straight to production) until an
administrator switches it on.

以下步骤不会自动发生：在管理员打开之前，工作区保持原有行为
（`release_mode: direct`，部署直接进生产）。

1. **Switch the workspace to gated.** Standards → Release gate → *Release policy*
   (administrator), or `PUT /api/release-policies/{workspace_id}` with
   `{"release_mode": "gated"}`. The same card sets how long a judge calibration holds
   (`calibration.period_days`, `kappa_floor`) and the evaluation spend guard
   (`eval_cost_confirm_usd`, `eval_cost_max_usd`) — the guard **refuses** a run over
   the ceiling, it does not merely warn.
2. **Move each agent onto named endpoints.** Standards → Release gate → *Move to
   live/candidate* (needs `release.sign`). Until then the agent has one endpoint that
   AWS auto-rolls on every update, so there is nothing a gate could hold back. A2A
   runtimes and system presets cannot be gated and say so.
3. **Grant the three standard-owner permissions to people, not roles.** Users →
   a member → `criteria.sign` (signs the standard), `golden.admit` (admits samples),
   `judge.calibrate` (declares a judge aligned). They are deliberately absent from
   every role default: these three decide what "good" means. `criteria.manage`,
   `waiver.approve` and `release.sign` follow the ordinary member/operator split.
4. **Write and sign the criteria table.** Standards → Criteria. At least one red
   line, every dimension covered or marked `n/a:<dimension>`, cost/performance as
   metrics, and no red line on an LLM judge — the editor refuses the rest. Publish
   freezes the version; someone *other than the author* signs it. An unsigned
   standard makes every gate report INVALID, which is the point.
5. **Curate the golden set once.** Standards → Golden set → *Curate the splits*
   (`golden.admit`). This is the only path that writes the holdout, and it seals it
   afterwards. Items are stratified by case tier so all three splits see the same
   mix of good / bad / ambiguous / adversarial cases.
6. **Invoke each endpoint once before the first gate run.** Each AgentCore endpoint
   logs to its own group, so a brand-new `candidate` has no telemetry until something
   runs on it, and the gate would time out waiting for records that do not exist yet.
   A single chat turn on the candidate is enough.
7. **Then the loop runs itself.** A deploy lands on `candidate` and opens a release;
   Standards → Release gate runs the regression *and* holdout splits against it,
   reports a verdict with every rate's confidence interval, and a second person signs
   it off — only then does `live` move. Rollback re-points `live`; nothing is deleted.

**Judges only block once calibrated.** A judge criterion is shown and treated as
`observe` until a labelling task records `aligned`. Standards → Calibration creates
the task from a completed run (the judge's verdicts are hidden from annotators), and
in a dev/staging workspace you can hand an outside expert an account-free link
(`/r/annotate/<token>`). A **prod** workspace refuses those links — labelling there
touches real customer transcripts, so invite the person as a member instead.

**End-to-end check (real AWS, cleans up after itself):**

```bash
cd backend
# the whole loop with three identities: deploy → migrate → criteria signed by
# someone else → golden set sealed → gate → sign → rollback → calibration
LAUNCHPAD_E2E_USERNAME=<admin> LAUNCHPAD_E2E_PASSWORD=<pw> \
  uv run python scripts/e2e_agent_dlc.py --base http://localhost:8000
# the nine console views in both languages, failing on console errors or 5xx
uv run python scripts/e2e_agent_dlc_browser.py --ui http://localhost:5173 --lang zh-CN
```

The first script refuses to run against a `prod`-tier workspace and restores the
release policy it changed. Both are excluded from `make verify` (they need real AWS
credentials and a bootstrapped workspace).

## Choosing models / 切换模型

Besides the models of the agents you build, the platform calls models of its own:
it names intents, writes suggested questions, judges evaluations, plays the
simulated user, proposes agents, fixes Studio code. The built-in defaults assume
the account can invoke OpenAI and Anthropic models on Bedrock. Some accounts
cannot; every one of these models is a setting you can switch, on the console or
on the host.

| Env var (`launchpad.yaml` key = the lower-case name without `LAUNCHPAD_`) | Default | Where it is used |
|---|---|---|
| `LAUNCHPAD_AGENT_DEFAULT_MODEL_ID` | `global.zai.glm-5.3` | Default model of a new agent: the Create Agent wizard pre-selects it, and a spec submitted without `model_id` gets it. |
| `LAUNCHPAD_PLATFORM_MODEL_ID` | `global.anthropic.claude-sonnet-5` | Intent clustering and naming (Observe → intent view) and the try-chat suggested questions. When the call fails, the intent view falls back to mechanical grouping and the panel to generic questions. |
| `LAUNCHPAD_JUDGE_MODEL_ID` | `global.openai.gpt-6-sol` | Default LLM-as-judge model of custom and derived AgentCore evaluators (the judge-model field of a new evaluator). |
| `LAUNCHPAD_JUDGE_FALLBACK_MODEL_ID` | `global.anthropic.claude-sonnet-5-5` | Judge retried when AgentCore's evaluator validation refuses the chosen one. `""` = no fallback; the refusal surfaces. |
| `LAUNCHPAD_ASSISTANT_MODEL_ID` | `global.openai.gpt-6-sol` | Default model of the agents the architect assistant proposes (managed Harness). |
| `LAUNCHPAD_ARCHITECT_MODEL_ID` | `global.openai.gpt-6-astra` | Model of the system architect agent when it is installed. `reasoning_effort` is preset only for an OpenAI model, the only family that takes it. |
| `LAUNCHPAD_ACTOR_MODEL_ID` | `global.openai.gpt-6-luna` | Simulated-user (actor) model the console pre-selects for persona scenarios. It must accept Converse forced tool choice. |
| `LAUNCHPAD_STUDIO_FALLBACK_MODEL_ID` | `global.anthropic.claude-sonnet-5` | Model of a Strands Studio canvas node that names none. |
| `LAUNCHPAD_PROMPT_OPT_DEFAULT_MODEL_ID` | `global.anthropic.claude-opus-5` | Reflection model of the experiment RECOMMEND stage when a request names none (see [Prompt optimization](#prompt-optimization)). |
| `LAUNCHPAD_PROMPT_OPT_MODELS` | the four ids in [Prompt optimization](#prompt-optimization) | The reflection models the console offers, as a JSON list (`'["global.zai.glm-5.3","deepseek.v3.2"]'`). Env / `launchpad.yaml` only; the Models page does not edit the list. |
| `LAUNCHPAD_CLAUDE_SDK_MODEL_ID` | `global.anthropic.claude-sonnet-5` | Default of the Claude Agent SDK (container) method. **Claude only.** |
| `LAUNCHPAD_CODEGEN_MODEL` | `global.anthropic.claude-sonnet-5` | Studio AI Fix, which runs on the Claude Agent SDK (see [Code generation](#code-generation-ai-fix)). **Claude only.** |
| `LAUNCHPAD_SKILL_LAB_JUDGE_MODEL_ID` | `us.openai.gpt-6-sol` | Skill Lab judge. An OpenAI id runs on the host `codex` CLI, a Claude id on the host `claude` CLI. **OpenAI or Claude only.** |
| `LAUNCHPAD_SKILL_LAB_TARGET_MODEL_ID` | `global.anthropic.claude-opus-5-5` | Skill Lab `claude_code_exec` target. **Claude only.** |
| `LAUNCHPAD_SKILL_LAB_CODEX_TARGET_MODEL_ID` | `global.openai.gpt-6-sol` | Skill Lab `codex_exec` target. **OpenAI only.** |

A setting is a default for what is created next. Existing agents keep their
`model_id`, an installed architect keeps its model, and an existing evaluator
keeps the judge it was created with (the script below can move those).

### The Models page

An administrator switches them on the console: **admin → Configuration → Models**
(V3; 配置 → 模型设置 in Chinese). One row per setting shows the current model and
where it comes from (`default`, `config` = `launchpad.yaml`, `env`, `console`).

1. Pick a model from the select, which lists the text models the account's
   Region offers (inference profiles and on-demand foundation models). Offered
   is not the same as entitled, so:
2. **Test** makes a real, tiny Converse call with that model. For the two judge
   rows it also runs the AgentCore Evaluations acceptance probe: it creates a
   throwaway `zz_probe_…` evaluator with that judge and deletes it again. For the
   actor row it checks forced tool choice; for the other rows whose agents call
   tools (agent default, assistant, architect, Studio fallback) it adds a
   `tool_use` check.
3. **Save** stores the choice in the ledger (table `platform_settings`) and
   applies it to the running backend at once; no restart.
4. **Reset** removes the console choice; the row falls back to env,
   `launchpad.yaml` or the default.

Claude-only and OpenAI-only rows refuse a model of another family
(`models.wrong_family`). The APIs behind the page: `GET /api/models/defaults`
(any member: the effective values the console pre-selects),
`GET /api/models/settings`, `PUT` / `DELETE /api/models/settings/{field}`
(administrator), `GET /api/model-catalog` (member), `POST /api/model-probe`
(administrator).

### The script: scripts/switch_models.py

For a host the console cannot reach yet, or to set everything in one go,
`scripts/switch_models.py` probes the account (the same real Converse calls; an
AgentCore `CreateEvaluator` test for judge models; forced tool choice for the
actor), takes the first working candidate per setting (the current value, then
the default, then `--prefer` ids, then curated alternatives) and writes the
`LAUNCHPAD_*` lines to `/etc/launchpad-models.env` (mode 0600). Settings that
are Claude-only or OpenAI-only get only candidates of that family; when none
works they are reported `UNAVAILABLE` and left out of the file.

```bash
cd <repo>/backend
# probe and print; changes nothing
uv run python ../scripts/switch_models.py --region us-east-1 --dry-run
# write /etc/launchpad-models.env and restart the backend (needs root for /etc)
sudo "$(command -v uv)" run --no-sync python ../scripts/switch_models.py \
  --region us-east-1 --prefer global.zai.glm-5.3 --restart
```

- `--prefer a,b` tries those ids right after each setting's default (when they
  fit its family). `--roles judge,actor` limits the run; `--json` adds a
  machine-readable result.
- `--restart` runs `systemctl restart launchpad-backend` after writing. Without
  it, restart the backend yourself: an env file is read only at start.
- The script lists the evaluators the console created whose judge model no
  longer works. `--update-evaluators` moves them to the chosen judge.
- Settings saved on the Models page win over the file; the script reports them
  and never changes them. Reset them on the page for the file value to apply.
- `--env-file <path>` writes elsewhere, for example when you run it from a
  laptop with credentials for the target account.
- `--no-sync` keeps a root run from re-syncing the backend venv the service
  user owns. Exit code 0 = every switchable setting resolved, 2 = some setting
  (or evaluator update) found no working model, 1 = usage, credential or write
  error.

Measured in us-east-1 on 2026-10-12: AgentCore Evaluations accepts
`global.zai.glm-5.3` as a judge model (and GPT-6 Sol, Claude Sonnet 5.5, Kimi
K3, DeepSeek R1). With OpenAI and Anthropic simulated unavailable, the script
chose `global.zai.glm-5.3` for the platform, judge, assistant, architect,
actor, Studio fallback and prompt-optimization defaults,
`deepseek.v3.2` as judge fallback, the reflection list
`global.zai.glm-5.3`, `deepseek.v3.2`, `global.moonshotai.kimi-k3`,
`moonshotai.kimi-k2.5`, and reported Claude Agent SDK, codegen and the three Skill Lab
settings `UNAVAILABLE`.

### Tested alternatives: DeepSeek and Kimi

Besides GLM-5.3, the DeepSeek and Kimi models on Bedrock are candidates for the
platform's settings. Live probe matrix, account 784675006790, 2026-10-12 (all
probe evaluators deleted afterwards):

| Model | us-east-1 Converse (latency) | us-west-2 Converse (latency) | Tool use (auto / forced any / stream) | Reasoning blocks | AgentCore judge | Eligible settings |
|---|---|---|---|---|---|---|
| `us.deepseek.r1-v1:0` | ok 2.1 s | ok 1.7 s | refused ("This model doesn't support tool use") | yes | accepted | platform, judge, judge fallback, prompt optimization |
| `deepseek.v3.2` | ok 0.6 s | ok 0.7 s | ok / ok / ok | no | accepted | all not limited to one family |
| `deepseek.v3-v1:0` (DeepSeek V3.1) | not offered | ok 0.6 s | ok / ok / ok | no | accepted (us-west-2) | all (us-west-2 only) |
| `global.moonshotai.kimi-k3` | ok 2.0 s | ok 2.3 s | ok / ok / ok | yes | accepted | all |
| `us.moonshotai.kimi-k3` | ok 2.8 s | ok 2.1 s | ok / ok / ok | yes | accepted | all |
| `moonshotai.kimi-k2.5` | ok 0.6 s | ok 0.8 s | ok / ok / ok | no | accepted | all |
| `moonshot.kimi-k2-thinking` | ok 1.2 s | ok 0.9 s | ok / ok / ok | yes | accepted | all |
| `global.zai.glm-5.3` | ok 0.8 s | ok 0.8 s | ok / ok / ok | yes | accepted | all |

"All" means every setting not limited to Claude or OpenAI. Reasoning blocks do no
harm: the platform reads only the text blocks of a response, so reasoning
content is ignored.

**A model without tool use is refused for tool settings.** The agent default,
assistant, architect, actor and Studio fallback settings drive agents that call
tools, so the backend refuses a model that does not support tool use (such as
DeepSeek R1) for them with `models.no_tool_use`. On the Models page, **Test** on
those rows adds a `tool_use` check (the actor row checks forced tool choice
instead).

**Pricing** (per 1M input / output tokens, Standard on-demand, US East (N.
Virginia), 2026-10-12, now in the backend price table `model_prices`): from the
[Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/), cross-checked
with the AWS Price List API: DeepSeek V3.2 $0.62 / $1.85, Kimi K2.5 $0.60 /
$3.00, Kimi K2 Thinking $0.60 / $2.50. Not on the page, so from the Price List
API: DeepSeek V3.1 $0.58 / $1.68 (us-west-2), DeepSeek R1 $1.35 / $5.40, Kimi K3
global $3.00 / $15.00 (cache read $0.30, cache write $3.75), Kimi K3 US profile
$3.30 / $16.50 ($0.33 / $4.125). GPT-6 Sol is in neither source; it is priced
from its [model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-sol.html)
(short context ≤ 272K): global profile $2.00 / $10.00 (cache read $0.20, cache
write $2.50), US profile and Mantle in-Region $2.20 / $11.00 ($0.22 / $2.75), so
the default judge is no longer reported as unpriced.

### Setting them by hand

Put the lines in `/etc/launchpad-models.env` (root-owned, mode 0600) and make
sure the backend unit loads that file. The leading `-` lets the unit start when
the file is absent:

```bash
sudo install -m 0600 /dev/null /etc/launchpad-models.env   # only if it does not exist yet
sudo tee -a /etc/launchpad-models.env >/dev/null <<'ENV'
LAUNCHPAD_JUDGE_MODEL_ID=global.zai.glm-5.3
LAUNCHPAD_PLATFORM_MODEL_ID=global.zai.glm-5.3
ENV
systemctl cat launchpad-backend | grep -q launchpad-models.env || {
  sudo mkdir -p /etc/systemd/system/launchpad-backend.service.d
  printf '[Service]\nEnvironmentFile=-/etc/launchpad-models.env\n' \
    | sudo tee /etc/systemd/system/launchpad-backend.service.d/models.conf
  sudo systemctl daemon-reload
}
sudo systemctl restart launchpad-backend
```

Or set the same keys, lower-case, in `config/launchpad.yaml`
(`judge_model_id: global.zai.glm-5.3`, `prompt_opt_models:` as a YAML list) and
restart. `make bootstrap` merges into that file, so they survive a re-bootstrap.

**Precedence:** Models page (console) > environment (including the env file) >
`config/launchpad.yaml` > built-in default.

### What cannot be switched to another family

Some features are bound to one model family by the SDK or CLI they run on. In
an account without Anthropic access they stay unavailable whatever you set: the
**Claude Agent SDK** deploy method, **Studio AI Fix** (codegen), and the Skill
Lab `claude_code_exec` target and claude-CLI judge path. Without OpenAI access
the Skill Lab `codex_exec` target and codex judge path are unavailable. The
Skill Lab judge needs one of the two families.

### Changing the judge changes the scores

An LLM-as-judge score is that model's opinion. After you switch the judge,
scores, pass rates and gate verdicts on judged criteria are not comparable with
runs scored by the previous judge, and a calibration (judge–human κ) recorded
for the old judge does not carry over: compare runs only within one judge, and
run a new labelling task before relying on the judge to block. Metric and code
criteria (token counts, latency, assertions) are unaffected.

## Hosted deployment / 托管部署

> This section describes how to keep the sample running on a long-lived host for a
> workshop or a shared evaluation environment. It is not a production deployment
> guide: the sample has not been hardened for production use, and anything you run
> on it is your responsibility to secure, test and operate. Work with your security
> and legal teams before exposing it to real users or real data.

`./start.py --prod` is a local preview: it builds the frontend, serves the built
bundle, drops backend auto-reload, and binds to `0.0.0.0`. For a host that stays
up, supervise the two processes instead and keep the console behind an edge that
terminates TLS. The reference deployment (workshop EC2 + CloudFront) — its unit
files and the verified update sequence — is written up in
[agent-runbook-prod.md](agent-runbook-prod.md#3-shape-b--systemd-reference-the-us-east-1-box); its shape is:

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
# only with single sign-on: the browser URL and the client-secret key
Environment=LAUNCHPAD_PUBLIC_BASE_URL=https://<console-host>
Environment=LAUNCHPAD_SSO_SECRET_KEY=<fernet-key>
# optional model settings, see "Choosing models"; "-" = may be absent
EnvironmentFile=-/etc/launchpad-models.env
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

**3. Update an existing host.** The short form is below; to upgrade from an older
version (backup, migration rehearsal, rollback, troubleshooting), follow
[upgrade.md](upgrade.md).

```bash
(cd backend && uv run python scripts/ledger_backup.py)   # online + verified; never cp a live ledger
git fetch origin && git merge --ff-only origin/main
cd backend && uv sync --frozen && cd ..
cd frontend && npm ci && npm run build && cd ..          # required: preview serves dist/
sudo systemctl restart launchpad-backend launchpad-frontend
curl -s localhost:8000/api/auth/status          # expect auth_required: true
```

New ledger tables (such as `users`) are created on startup, so no migration step
is needed. Registration is open as soon as the gate is on — set
`LAUNCHPAD_AUTH_REGISTRATION_ENABLED=false` or pin
`LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS` if the deployment should not accept
requests from anyone who has the URL.

**4. Online update (optional).** An administrator can update the console from
**Configure → System update** instead of running step 3 by hand. Install the
updater once, as root:

```bash
sudo backend/scripts/install_updater.sh      # --user / --services / --health-url to override
systemctl status launchpad-updater.path      # expect active (waiting)
```

It copies `backend/scripts/apply_update.sh` to `/usr/local/lib/launchpad/` (an
update never replaces the script that applies it), writes
`/etc/launchpad-updater.env` and two units: `launchpad-updater.path` watches
`data/updates/apply.request`, and `launchpad-updater.service` (`Type=oneshot`, root)
applies it.

The flow on the page:

1. **Check** compares the running commit with the latest commit of the configured
   source. Only a fast-forward is accepted. The source is deployment configuration,
   never a request parameter: `LAUNCHPAD_UPDATE_REPO` (default
   `https://github.com/aws-samples/sample-agentcore-launchpad`) and
   `LAUNCHPAD_UPDATE_BRANCH` (default `main`).
2. **Prepare** checks the commit out into `data/updates/stage-<sha>` and runs only
   fixed commands there at `nice 10`: `uv sync --frozen`, `npm ci`,
   `npm run build`, then an import check and a start check on a temporary port with
   its own throwaway ledger. The running console is not affected. A failure (for
   example no access to the npm registry) leaves the update `prepare_failed`. A
   backend restart resumes a prepare from its last finished step. The newest two
   stages are kept.
3. **Schedule restart** (now = in one minute, or a time within 7 days) publishes an
   announcement. If the restart would interrupt tasks in progress, the page lists
   them and asks you to acknowledge first. At the scheduled time the console writes
   `data/updates/apply.request`. The updater then backs up the ledger
   (`ledger_backup.py`), fast-forwards the checkout, swaps the built
   `frontend/dist` in by rename, runs `uv sync --frozen` and restarts the units. It
   waits up to 120 s for `/api/auth/status` to report `version.sha` equal to the new
   commit. If that check fails, it resets to the previous commit, restores the old
   `dist`, restarts again and reports `rolled_back`. The console reads the result
   (`data/updates/apply.result`) back and ends the announcement.
4. **Roll back** prepares the commit the last update replaced. You schedule it like
   an update.

Without the updater, the page still checks and prepares, and then shows the
commands to apply the prepared stage by hand. `LAUNCHPAD_UPDATE_ENABLED=false` turns
the whole feature off. By default, scheduling is available only where the updater
is installed. Every step is journaled as `system.update.*` in the hub's audit log.
Also tunable: `LAUNCHPAD_UPDATE_MIN_FREE_GB` (default 3, free disk needed to
prepare) and `LAUNCHPAD_UPDATE_STAGE_MAX_GB` (default 6, the stage size cap).

## Teardown / 资源清理

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # list what would be removed
uv run python ../scripts/teardown.py --yes       # delete (memory → registry → CDK stack)
```

Deletion is best-effort and ordered dependents-first; the S3 bucket auto-empties
and the ECR repo force-deletes via the stack.
