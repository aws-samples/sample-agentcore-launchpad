# Interface: deploy (N5 → N9)

Frozen 2026-10-09, revised after a whole-branch review fix round (same date).
Step-by-step real deployment to cn-northwest-1. N9 runs this under main-agent
supervision — every AWS-touching command below is real (spec C8: no
sub-session may run these; this file is written by one, executed by the main
agent).

Every command assumes the shell's current directory is the repository root
(`cd` back there explicitly after any step that changes directory — called out
inline below) and that `cn/integration` (the N8 merge target) is checked out.

## 0. Prerequisites

- `aws sts get-caller-identity --profile cn-northwest-1` resolves to the target
  account (123456789012 per `docs/cn/CN-MIGRATION-ANALYSIS.md` §6.3 I1, or
  whichever account N9 is pointed at).
- `aws-cdk` on PATH: `npm i -g aws-cdk`. The `infra/` uv venv has no `cdk`
  binary of its own — `uv run cdk` resolves to whatever global `cdk` install
  exists on the host running Step 2.
- A Global AWS account's Bedrock API key for `global.zai.glm-5.3` /
  `global.moonshotai.kimi-k3` (spec C3/C4) — obtained out of band, never
  committed, never passed on a command line (shell history, `ps`, `/proc/<pid>/
  cmdline` would all expose it).
- A console admin password — pick one now. **This is not optional.** UserData
  sets `LAUNCHPAD_RUN_MODE=prod` unconditionally, and
  `backend/app/main.py::_assert_production_is_authenticated` refuses to even
  build the FastAPI app when `run_mode=="prod"` and no password is configured
  — without one, `uvicorn` crash-loops under systemd, the health-check loop in
  UserData times out, `signal_stack FAILURE` fires, and CloudFormation rolls
  the whole stack back. The template's `ConsolePasswordParameter` has no
  `Default` for exactly this reason — `aws cloudformation deploy` in Step 3
  will not proceed without one. (A second, independent reason the password
  matters even once the stack is up: nginx reverse-proxies to the backend's
  own loopback address, so `backend/app/routers/auth.py::_peer_is_loopback`
  — which decides whether to refuse an unauthenticated non-loopback caller —
  always sees `127.0.0.1`, nginx's own connection, and deliberately ignores
  `X-Forwarded-For`. The `auth.open_console_refused` safeguard `docs/
  setup.md`'s "Console login" section describes therefore never fires behind
  this template; the password is the *only* gate.)
- An operator-chosen `AllowedCidr` (your current egress IP as a `/32`, or an
  office/VPN CIDR) — **not** `0.0.0.0/0` and **not any `/0`**: the template's
  `AllowedPattern` restricts the prefix to `/1`–`/32`.
- `cdk bootstrap` + `make bootstrap` (`cdk deploy`) in Steps 2/3 below run
  under the EC2 ops role (`launchpad-cn-ops-<region>`, `infra/ec2/launchpad-
  cn-ec2.yaml`). Under CDK v2's modern bootstrap, `cdk deploy` first tries to
  **assume** the bootstrap's own `cdk-<qualifier>-deploy-role-…` (and the
  file-publishing/lookup roles), so that CloudFormation actually executes
  under `cdk-<qualifier>-cfn-exec-role-…`, not under the ops role's own
  credentials. The ops role's inline policy therefore grants it
  `sts:AssumeRole` on `role/cdk-*` (Sid `CdkAssumeBootstrapRoles`) — without
  that grant the AssumeRole is denied, the CLI silently falls back to the ops
  role's own credentials, and CloudFormation then needs every target-resource
  permission (OpenSearch, SQS, Lambda, API Gateway, …) directly on the ops
  role instead of on the cfn-exec role, which this least-privilege role does
  not have. Do not widen the ops role with those broad service actions as a
  workaround — deploys must run through the cfn-exec role.
- The self-hosted Memory/KB OpenSearch domain is **shared by every EnvName
  environment of the account+region** (D17, 2026-10-10): CDK stack
  `launchpad-memory-domain` (never suffixed) owns the domain
  `lp-memory-<region>` and its FGAC master, the stack-owned role
  `launchpad-memory-admin-<region>`; each environment's
  `launchpad-memory[-env]` stack only adds its SQS queue, extractor Lambda
  and IAM, and re-exports the domain endpoint/ARN and `MemoryAdminRoleArn`.
  The hub (running as the EC2 ops role) **assumes** that admin role for every
  OpenSearch call — bootstrap of its own indices + FGAC role, console
  reads/writes — so the ops role's inline policy grants `sts:AssumeRole` on
  `role/launchpad-memory-admin-*` (Sid `MemoryAdminAssume`). Nothing is
  needed from the operator: UserData writes only
  `LAUNCHPAD_CDK_CONTEXT=env_name=<EnvName>` to `/etc/launchpad/env`. The
  pre-D17 `memory_master_arn=<ops role ARN>` context is gone (a master bound
  to one environment's ops role would have left the shared domain without an
  administrable master the day that environment was torn down); the CDK app
  ignores it if an old env file still carries it.
  **Off-host runs** (a laptop, CI runner): the identity running `make
  bootstrap` needs `sts:AssumeRole` on
  `arn:<partition>:iam::<account>:role/launchpad-memory-admin-<region>` — the
  role trusts the account root, so any identity policy that allows it works.
  Environment isolation inside the one domain is per index prefix
  (`launchpad-memory` … for the default environment, `launchpad-<env>-…`
  otherwise) plus a per-environment FGAC role limited to that prefix; the
  EnvName `kb` is therefore reserved (its prefix would collide with the
  default environment's `launchpad-kb-*` indices) and refused by all three
  layers.

## 1. Store the secrets in SSM (cn-northwest-1)

Three `SecureString` parameters: the Global Bedrock API key, the console
password decided in §0, and (N9: deploying from the private
`<your-fork>/launchpad-cn` repo, branch `cn/integration`) a read-only Git SSH
deploy key. **Read each from an environment variable or an interactive
`read -s` prompt — never type the value as a literal in a command you'll
keep, and never pass it as a `--parameter-overrides` value** (those are
visible in plain text via `describe-stacks`/the console; a `NoEcho`
CloudFormation parameter does not help either — `Fn::Sub` still bakes a
`NoEcho` value into the UserData text, which `describe-instance-attribute
--attribute userData` can read back regardless of the console's own masking).

Use a **flat** (non-hierarchical) name for each — no leading `/`, and the
template now enforces this with an `AllowedPattern` on all three parameters
(the node fix round, F2; N9 extends the same `AllowedPattern` to
`GitDeployKeyParameter`, with `""` also accepted since it is optional). The
reason: the `ReadLaunchpadSecretParameters` IAM statement builds each resource
ARN as `arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/
${name}` (literal `parameter/` immediately before the substitution). Per
AWS's own SAM policy-template pair (`SSMParameterReadPolicy` vs.
`SSMParameterWithSlashPrefixReadPolicy`, docs.aws.amazon.com/serverless-
application-model/.../serverless-policy-template-list.html), that
`parameter/${name}` form is correct **only** when `name` has no leading
slash — a hierarchical name such as `/launchpad/global-bedrock-api-key` would
make the resource ARN `…:parameter//launchpad/global-bedrock-api-key` (double
slash), which does **not** match the parameter's real ARN, and UserData's
`aws ssm get-parameter --with-decryption` call would fail `AccessDenied`
partway through the 60-minute boot window.

**Model-provider keys (D21, M1-b).** Every model provider the registry knows — the
built-in SiliconFlow row and any OpenAI-compatible provider an admin registers — reads
its key from an SSM `SecureString` named **`launchpad-<provider>-api-key`** (flat, same
rules as above). The console host reads it itself (`aws_clients.ssm_client` →
`GetParameter`), hands it once to AgentCore Identity and records only the parameter
*version*; the ops role's `ReadLaunchpadSecretParameters` statement therefore carries
the pattern `parameter/launchpad-*-api-key` next to the explicit parameters. The
parameter may be created **after** the hosts are up: Settings → Model Providers →
*Register credential* (or the next `make bootstrap`, stage `model-providers`) picks it
up — no reboot, no re-bootstrap. `SiliconFlowApiKeyParameter` / the
`LAUNCHPAD_SILICONFLOW_API_KEY` line in `/etc/launchpad/env` remain as the legacy boot
path and may stay empty (§8.6; `docs/cn/interfaces/model-registry.md` §8–9).

**Provider endpoints — no allowlist precondition (N17-obs203, 2026-10-21).** An
OpenAI-compatible provider's `base_url` needs no operator action any more: any public https
host is accepted and the console host resolves it before every request, refusing loopback /
private / link-local / IMDS addresses and internal names. Two optional `config/launchpad.yaml`
keys remain for operators who want a boundary: `model_provider_allowed_hosts` (non-empty =
**lock mode**, only the listed hosts, exact or `*.suffix`; default `[]`) and
`model_provider_denied_hosts` (always refused). Both are read at start (`LAUNCHPAD_`-prefixed
env works too) and never editable from the console.

```bash
read -rs BEDROCK_KEY; echo
aws ssm put-parameter \
  --name launchpad-global-bedrock-api-key \
  --type SecureString \
  --value "$BEDROCK_KEY" \
  --region cn-northwest-1 \
  --profile cn-northwest-1
unset BEDROCK_KEY

read -rs CONSOLE_PASSWORD; echo
aws ssm put-parameter \
  --name launchpad-console-password \
  --type SecureString \
  --value "$CONSOLE_PASSWORD" \
  --region cn-northwest-1 \
  --profile cn-northwest-1
unset CONSOLE_PASSWORD
```

**Optional — the SiliconFlow API key (N-R6,
[model-providers.md](model-providers.md)).** A fourth SecureString,
`launchpad-siliconflow-api-key`, enables the catalog's SiliconFlow (OpenAI-compatible)
models. Pass its name as the template parameter `SiliconFlowApiKeyParameter`
(default `""` = not offered). Same rules: flat name, value only through `read -s`,
never on a command line or as a stack parameter value. UserData writes it to
`/etc/launchpad/env` as `LAUNCHPAD_SILICONFLOW_API_KEY`; `make bootstrap` then creates
the Identity provider `launchpad-siliconflow` — without the parameter it logs
`model-credential launchpad-siliconflow not configured …` and everything else works.
Adding the key later = `put-parameter` + stack `deploy` with the parameter name (or
export the variable in `/etc/launchpad/env` by hand) + `make bootstrap` + restart.

```bash
read -rs SILICONFLOW_KEY; echo
aws ssm put-parameter \
  --name launchpad-siliconflow-api-key \
  --type SecureString \
  --value "$SILICONFLOW_KEY" \
  --region cn-northwest-1 \
  --profile cn-northwest-1
unset SILICONFLOW_KEY
```

**The Git deploy key** (N9) is generated once, out of band, as its own
dedicated ed25519 keypair — never an operator's personal SSH key, and never
a key with write access: it is added to the `<your-fork>/launchpad-cn` GitHub
repo under Settings → Deploy keys with **"Allow write access" left
unchecked**, so a compromised instance can at most read the repo, not push
to it.

```bash
ssh-keygen -t ed25519 -C "launchpad-cn-ec2-deploy-key" -N "" -f /tmp/launchpad-deploy-key
# Paste the contents of /tmp/launchpad-deploy-key.pub into GitHub → repo
# Settings → Deploy keys → Add deploy key (read-only, i.e. do NOT check
# "Allow write access").
aws ssm put-parameter \
  --name launchpad-git-deploy-key \
  --type SecureString \
  --value "file:///tmp/launchpad-deploy-key" \
  --region cn-northwest-1 \
  --profile cn-northwest-1
shred -u /tmp/launchpad-deploy-key /tmp/launchpad-deploy-key.pub
```

Verify all three are `SecureString` and not readable without decryption, and
sanity-check the ARN the IAM policy will end up matching against:

```bash
for name in launchpad-global-bedrock-api-key launchpad-console-password launchpad-git-deploy-key; do
  aws ssm get-parameter --name "$name" \
    --region cn-northwest-1 --profile cn-northwest-1 \
    --query '[Parameter.Type,Parameter.ARN]' --output text
done
# expect, for each: SecureString   arn:aws-cn:ssm:cn-northwest-1:<account>:parameter/<name>
# (single slash after "parameter" — if you see anything else, the matching
# *Parameter value in step 3 won't match this ARN and UserData will 403)
```

## 2. CDK bootstrap + shared stack

Pre-provisioning `launchpad-base` from here (rather than letting the EC2
host's own UserData create it cold) means the host's `make bootstrap` call in
Step 3 finds everything already `reused` and spends its 60-minute
`CREATE_COMPLETE` signal window on the frontend build instead of a cold `cdk
deploy`.

```bash
cd infra
uv sync
ACCOUNT_ID="$(aws sts get-caller-identity --profile cn-northwest-1 --query Account --output text)"
AWS_PROFILE=cn-northwest-1 uv run cdk bootstrap "aws://${ACCOUNT_ID}/cn-northwest-1"
AWS_PROFILE=cn-northwest-1 uv run cdk deploy launchpad-base --require-approval never
# optional, saves the host the ~16-minute domain creation too (D17: this stack is
# shared by every later environment and `cdk deploy --all` finds it unchanged)
AWS_PROFILE=cn-northwest-1 uv run cdk deploy launchpad-memory-domain --require-approval never
cd ..
```

Pre-provisioning is an optimisation, no longer a requirement: since N11-bug3 the
EC2 ops role carries the two scoped grants a **cold** `cdk bootstrap` needs —
Sid `EcrForCdkBootstrapRepo` (repository lifecycle actions on
`repository/cdk-*`, the CDKToolkit's own container-assets repo) and Sid
`SsmForCdkBootstrapVersion` (`PutParameter`/`GetParameter(s)`/`DeleteParameter`/
`AddTagsToResource` on `parameter/cdk-bootstrap/*`, the `/cdk-bootstrap/<qualifier>/version`
marker — `DeleteParameter` is what lets a failed CDKToolkit roll back instead of
sticking in `ROLLBACK_FAILED`). A host booted into an account/region with no
CDKToolkit stack therefore bootstraps it itself. The grants are template-tested
(`infra/tests/test_ec2_template.py`), not yet exercised live: every cold run so
far found CDKToolkit already present.

(Pass `-c base_image_mirror_enabled=true` only after Step 6 below has mirrored
the base image — it defaults off, R4, so every Dockerfile's own `ARG
BASE_IMAGE` public default applies unchanged until then.)

For a **parallel attempt** (§3.1) add `-c env_name=<EnvName>` to every `cdk`
command — `cdk deploy launchpad-base-t1 -c env_name=t1` — so the app
synthesizes the suffixed stacks (`launchpad-base-t1`, `launchpad-memory-t1`)
instead of the default ones; `launchpad-memory-domain` keeps its name under
any `env_name` and is the same stack for every environment (D17). Without the context flag the stack ids are the
default names and `cdk deploy launchpad-base-t1` reports "no stack named".

## 3. EC2 stack parameters

Run from the repository root (the `cd ..` at the end of Step 2 above matters —
`--template-file infra/ec2/launchpad-cn-ec2.yaml` is relative to it). Pass
`--disable-rollback` on N9's **first** attempt against a given account/region:
if UserData fails partway through, the instance and `/var/log/launchpad-
bootstrap.log` survive for diagnosis instead of CloudFormation tearing
everything down and erasing the evidence; delete the stack by hand afterward
if that happens (Step 7 covers normal teardown of a stack that *did* come up).

```bash
aws cloudformation deploy \
  --template-file infra/ec2/launchpad-cn-ec2.yaml \
  --stack-name launchpad-cn-ec2 \
  --capabilities CAPABILITY_NAMED_IAM \
  --disable-rollback \
  --region cn-northwest-1 --profile cn-northwest-1 \
  --parameter-overrides \
    AllowedCidr=<your-cidr>/32 \
    GitRepoUrl=git@github.com:<your-fork>/launchpad-cn.git \
    GitBranch=cn/integration \
    GitDeployKeyParameter=launchpad-git-deploy-key \
    GlobalBedrockApiKeyParameter=launchpad-global-bedrock-api-key \
    ConsolePasswordParameter=launchpad-console-password \
    TlsMode=selfsigned \
    ConsolePort=8443
```

`ConsolePort` (default 8443) is the only inbound port the security group opens
and the port nginx listens on. **Do not use 80 or 443**: in the China partition
inbound 80/443 to a public IP is blocked unless the site has an ICP filing
(备案) — the first live deployment was unreachable on 443 and reachable on 8443
within seconds (N11-bug12); the template's `Rules` block rejects 80/443 outright.

Optional parameters, omitted above because their defaults fit the common case:
`EnvName=<1-3 lowercase letters/digits>` (empty by default — see §3.1; every
other parameter is unchanged by it), `HubAdminAccess=true|false` (default `false`;
`true` attaches the AWS-managed AdministratorAccess policy to the instance role the
way the upstream Global sample runs its hub, so the host can provision same-account
workspaces in any region of the partition — the scoped `launchpad-bootstrap` policy
is pinned to the stack's region and a workspace elsewhere fails at `validate-access`;
N17-obs112, operator decision 2026-10-20; keep passing the same value on every
update; the hermetic twin of that decision is
`backend/tests/consistency/test_workspace_permissions.py`, which renders this template's
role for a hub region and checks every bootstrap stage's IAM needs for a workspace region —
testing.md §6.10), `AllocateElasticIp=true|false` (default
`true` — an Elastic IP is allocated and associated so the console address
survives a stop/start and the in-place update path; see "Elastic IP" in §8.2
for enabling it on a stack created before N11-obs47) and `GitCloneTimeout=<300..3000>` (default
1800 — the total seconds the first boot may spend fetching the repository, see
"Two-stage first boot" below; raise it only for a link slower than the ~36 KB/s
measured from cn-northwest-1 to GitHub, and remember the bootstrap that follows
must still fit inside the stack's `PT60M` signal timeout). `AmiId=<running AMI>`
is the exception to "omit it": empty on the create command above, **mandatory on
every later update** — see "AMI pin" next.

**AMI pin (N11-obs48b).** Leave `AmiId` empty (the default) on the create command:
the template then looks the latest Canonical Ubuntu 24.04 arm64 AMI up once,
through the public SSM parameter
`/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id`.
That lookup is a CloudFormation *dynamic reference*, and dynamic references are
re-resolved on **every** stack update: with `AmiId` empty, every change set
against the stack marks the `Instance` `Replacement: Conditional`, and the day
Canonical publishes a newer 24.04 arm64 AMI any unrelated update — the Elastic
IP add of §8.2, an IAM statement — **replaces the instance**: a new host, the
local SQLite ledger (`data/launchpad.db`) and `/etc/launchpad` gone (found live
by deploy-ops on the t1 change set, 2026-10-14; that day the lookup still
returned the running AMI, so it was a no-op — by luck). Therefore, **right after
`CREATE_COMPLETE`, read the AMI the instance actually booted from, record it next
to the stack's other parameter values, and pass it as `AmiId=<running AMI>` with
every update of that stack** (deploy-ops runs this; nothing on the host needs it):

```bash
INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name launchpad-cn-ec2 \
  --region cn-northwest-1 --profile cn-northwest-1 \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --region cn-northwest-1 --profile cn-northwest-1 \
  --query 'Reservations[0].Instances[0].ImageId' --output text        # -> ami-…
```

With the running AMI pinned, `ImageId` no longer differs between the deployed
template and the new one, so the `Instance` simply does not appear in the change
set. **One-time exception on the first update that introduces this parameter** (the
migration of a stack created before `AmiId` existed, observed on default 2026-10-14):
the `ImageId` *expression* changes from the literal dynamic reference to the `!If`,
so CloudFormation lists `ImageId` as a target with `RequiresRecreation: Always` even
though the resolved value equals the running AMI — it cannot prove equality through
a condition. That first change set may be executed only after the operator has
verified `resolve:ssm` latest == running AMI == the `AmiId` passed (as on t1/default);
nothing is replaced. From the second update on, `ImageId` must be absent from the
`Instance` row. The template's `AllowedPattern` accepts only `ami-` + 8–17 hex digits
or the empty string. **A change set that shows a `Replacement` on `Instance` must
never be executed** — §8.2 spells out the preview gate; the usual cause is an
update run without `AmiId` (or with a different one). Moving a live environment
to a newer AMI is a *maintenance* (back the ledger up, or build a second
`EnvName` environment and tear the old one down, §3.1/§7) — never a side effect
of another change.

**Public origin (N11-obs14).** UserData writes
`LAUNCHPAD_PUBLIC_BASE_URL=<scheme>://<public-ipv4>:<ConsolePort>` into
`/etc/launchpad/env` — scheme `https` for `TlsMode=selfsigned`, `http` for
`none`; the public IPv4 read over IMDSv2 — i.e. exactly the `ConsoleUrl`
output. The backend derives the Identity 3LO (as-user OAuth) return URL from it
(`{public_base_url}/auth/return`); before this it stayed at
`http://localhost:5173` and `run_mode=prod` warned at start-up. **A custom
hostname must override it:** if the console is fronted by a DNS name, a reverse
proxy or an ICP-filed domain, the template cannot know that origin — edit the
`LAUNCHPAD_PUBLIC_BASE_URL=` line in `/etc/launchpad/env` (root, mode 0600) to
`https://<hostname>[:<port>]`, `sudo systemctl restart launchpad-backend`, and
register the same `/auth/return` URL with every OAuth2 provider used by as-user
tools. **If the public IP changes** — the one-time switch to the Elastic IP when
`AllocateElasticIp=true` is applied to an existing stack (§8.2 "Elastic IP"), or a
stop/start of a stack created with `AllocateElasticIp=false` — the file keeps the
boot-time address until `scripts/host-update.sh` runs: its step 12b (§8.1) re-reads
IMDSv2 `public-ipv4` (which reports the associated Elastic IP) and rewrites the
line when its host part is an IPv4 literal that differs; a hostname set by hand is
never touched. The `PublicIp` / `ConsoleUrl` stack outputs show the Elastic IP
whenever one is allocated.

(`GitRepoUrl=git@github.com:<your-fork>/launchpad-cn.git` overrides the
template's public `aws-samples` https default with the private fork's SSH
form — paired with `GitDeployKeyParameter=launchpad-git-deploy-key`, the
name of the read-only deploy-key `SecureString` from §1 (an empty
`GitDeployKeyParameter`, the template's default, would leave UserData with no
credential to authenticate an SSH clone and the boot would fail at the clone
step). `GitBranch=cn/integration` overrides the template's `cn-region`
default — N9 deploys the merged integration branch, not the baseline fork
point. `TlsMode=selfsigned` is the recommended default: the session cookie is
*always* `Secure` in production — `routers/auth.py::cookie_secure()` is
`auth_cookie_secure OR run_mode=="prod"`, and this template always sets
`run_mode=prod` — so a `Secure` cookie is simply never sent back over plain
HTTP, meaning sign-in **cannot work** against a direct `http://<public-ip>`
visit regardless of `TlsMode`. With `selfsigned`, the console is
`https://<public-ip>:<ConsolePort>`; the browser must be told to trust the self-signed
certificate once. If you must use `TlsMode=none`, the only way to actually
sign in is through an SSM port-forward to `localhost` — browsers treat
`http://localhost` as a secure context and still send `Secure` cookies there:
`aws ssm start-session --target <instance-id> --region cn-northwest-1
--profile cn-northwest-1 --document-name AWS-StartPortForwardingSession
--parameters portNumber=<ConsolePort>,localPortNumber=8080` (default ConsolePort is 8443), then open
`http://localhost:8080`. **Never** visit the plain `http://<public-ip>`
`ConsoleUrl` directly under `TlsMode=none` — login will silently fail there.)

Watch it come up:

```bash
aws cloudformation describe-stack-events --stack-name launchpad-cn-ec2 \
  --region cn-northwest-1 --profile cn-northwest-1 --max-items 10
```

Expect `CREATE_COMPLETE` within the template's `PT60M` signal timeout (a cold environment — nothing pre-provisioned — takes ~35–40 min, dominated by the OpenSearch domain; the pre-provisioned path ~15 min) — the
instance's first boot runs `cdk bootstrap` + `make bootstrap` + builds the
frontend before signaling success, so the stack genuinely isn't done until the
console answers `/api/health`. If it signals `FAILURE` instead, the bootstrap
already dumped `journalctl -u launchpad-backend --no-pager -n 60` to
`/var/log/launchpad-bootstrap.log` before failing — fetch it over SSM (Step 4
shows how) rather than guessing; with `--disable-rollback` the instance is
still there to inspect.

**Two-stage first boot (N11-r5-bug1).** EC2 caps instance user data at
16,384 bytes and the inline bootstrap had outgrown it (`aws cloudformation
deploy` failed at the Instance with `User data is limited to 16384 bytes`
before anything ran), so the first boot is split:

| stage | where it lives | what it does |
|---|---|---|
| **stub** | the template's `Instance.Properties.UserData` (`Fn::Sub`, ≈5.3 KB rendered with the defaults, 7.3 KB worst case) | `apt` the minimum (`git`, `curl`, `python3-pip`) + `awscli` from `PipIndexUrl`; export the region; arm an `EXIT` trap that signals `FAILURE` to the stack on *any* failure; write `/etc/launchpad/bootstrap.env`; fetch the deploy key from SSM; the staged sparse clone of `GitRepoUrl`@`GitBranch` into `/home/ubuntu/launchpad` as `ubuntu` under the `GitCloneTimeout` budget (below); `exec bash /home/ubuntu/launchpad/infra/ec2/bootstrap-host.sh` |
| **body** | `infra/ec2/bootstrap-host.sh` **in the repo**, run from the fresh checkout | everything the old UserData did after the clone: the rest of the toolchain (nginx, node, uv, cdk), the secrets → `/etc/launchpad/env` (0600), `uv sync`/`npm install`/`cdk bootstrap`/`make bootstrap`/`npm run build` as `ubuntu` via `runuser -m`, the systemd unit, nginx, the `/api/health` wait, `signal-resource SUCCESS`/`FAILURE` |

`/etc/launchpad/bootstrap.env` (root, 0600) is the contract between the two: one
`LAUNCHPAD_BOOT_<NAME>=<value>` line per template parameter / pseudo parameter the
body needs — `REGION`, `STACK_NAME`, `SIGNAL_RESOURCE` (`Instance`), `ENV_NAME`,
`TLS` (`true|false`), `CONSOLE_PORT`, `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`,
`CODEBUILD_PIP_INDEX_URL`, `CODEBUILD_PIP_FALLBACK_INDEX_URL`, `NPM_REGISTRY`,
`BEDROCK_KEY_PARAMETER`, `CONSOLE_PASSWORD_PARAMETER`. It carries **no secret
values**: the last two are the SSM parameter *names*; the body reads the values
with the instance role exactly as before (spec C4). The body declares the keys it
requires at its top (`: "${LAUNCHPAD_BOOT_…:?}"`) and
`infra/tests/test_ec2_template.py` asserts the stub writes exactly that set, that
the worst-case rendered stub stays under the limit with margin, and that the body
is shellcheck-clean with no `${Param}` token left in it.

**What the host checks out, and how (N11-r5-bug2).** The clone is
`--depth 1 --filter=blob:none --sparse`, and the sparse set is `/*` minus
`docs/` (840 blobs, 194 MB — evidence PNGs and the workshop), `tests/` (the
Playwright e2e suite, never run on the host), `design/`, `.superpowers/` and
`.worktrees/` — i.e. **backend, frontend, infra, scripts, samples, config,
vendor and the root files**: 1,177 blobs / 14.4 MB instead of ~1,740 blobs.
Nothing on the host reads `docs/` or `tests/` at runtime or during
`make bootstrap` / `host-update.sh --verify` (`backend/tests/test_docs_tracked_links.py`
skips when `docs/` is absent; `n13_restore_check.sh` diffs vendored files against a
baseline commit). The first live run of the stub showed why this matters: GitHub
answered from cn-northwest-1 at ≈36 KB/s, the old fixed `timeout 600` killed the
blob fetch of the previous sparse set at 10.3 MB after 282 s, and the retry wiped
the directory and started again from zero. Now:

* `GitCloneTimeout` (default 1800 s) is the **total budget** of the fetch phase —
  initial clone, every sparse-checkout stage and every retry share it; each git
  network command runs under `timeout <remaining budget>`, so the phase can never
  exceed the parameter however many attempts it takes (no more "3 × timeout vs
  `PT60M`" arithmetic). Budget + the bootstrap after it (~15 min pre-provisioned,
  ~35–40 min cold) must fit `PT60M`.
* The sparse set is materialised in **stages** — root files + `infra` + `scripts`
  + `samples` + `config` first, then `sparse-checkout add` of `vendor`, `frontend`
  and `backend` (largest last). git fetches all missing blobs of one checkout as a
  single pack and discards it when killed, so smaller stages are what makes a
  retry resumable: a killed stage costs only that stage.
* The checkout directory is removed and re-created **only** when `git rev-parse
  HEAD` (as `ubuntu`) fails — no clone or a half-made one. A clone that succeeded
  keeps its packs; a failed stage is simply re-run ("`sparse-checkout attempt N
  failed after <s>s; resuming in 20s`" in the log), up to three attempts within
  the budget.
* `scripts/host-update.sh` is unaffected: it fast-forwards the branch with a plain
  `git fetch` + `merge --ff-only` and never re-applies the sparse set, so the
  host's checkout keeps the shape the stub gave it.

Where to look when a cold start fails:

* **Before the clone (stub)** — the stack goes `CREATE_FAILED` quickly with a
  `FAILURE` signal (the trap covers the clone-failed `exit 1` too; the old inline
  script would have waited out `PT60M` there). `/var/log/launchpad-bootstrap.log`
  ends inside the stub: `apt`/pip output, `git clone attempt N failed`,
  `sparse-checkout attempt N failed after <s>s` (a slow link — see
  `GitCloneTimeout` above; the log's byte counts tell you the rate), or
  `/home/ubuntu/launchpad/infra/ec2/bootstrap-host.sh missing from branch <GitBranch>`
  — the last one means `GitBranch` predates this split (the body is not in that
  branch): deploy a branch that carries `infra/ec2/bootstrap-host.sh`. If the log
  is empty or the stack timed out instead, the stub died before `awscli` existed
  (no network to the apt/PyPI mirrors) — the EC2 console output shows the
  cloud-init error.
* **After the clone (body)** — same log, now past `exec bash …/bootstrap-host.sh`;
  the body's sections are the ones documented above (toolchain, secrets,
  `make bootstrap`, nginx, health). The checkout is on disk, so after fixing the
  cause you can re-run the body by hand for diagnosis —
  `sudo bash /home/ubuntu/launchpad/infra/ec2/bootstrap-host.sh` appends to the
  same log and re-reads `/etc/launchpad/bootstrap.env`; note it ends with a
  `signal-resource` call, which CloudFormation rejects once the stack has left
  `CREATE_IN_PROGRESS` (harmless, logged).

### 3.1 Parallel attempts (`EnvName`) — N11

Every CloudFormation stack and most physical resource names in a Launchpad
environment are fixed (`launchpad-base`, `launchpad-memory`,
`launchpad-agent-execution-role-<region>`, `launchpad-gw`, the ECR repo, the SQS
queue, this template's `launchpad-cn-ops-<region>` role, ...), so by default
only one environment fits in an account+region and a failed attempt has to be
fully deleted before the next one can be created. `EnvName` lifts that: with
`EnvName=t1` **every** fixed name gets `-t1` appended at its END, in all three
layers, so a second complete environment can be created while the first one
is still being inspected or torn down.

| Layer | How the suffix is set | Examples with `t1` |
|---|---|---|
| EC2 template (this stack) | parameter `EnvName=t1` | ops role `launchpad-cn-ops-cn-northwest-1-t1`; VPC/subnet/instance `Name` tags |
| CDK app (what the host deploys) | `-c env_name=t1`, written by UserData into `LAUNCHPAD_CDK_CONTEXT=env_name=t1` | stacks `launchpad-base-t1`, `launchpad-memory-t1` (+ the shared, unsuffixed `launchpad-memory-domain`, created only if absent); `launchpad-agent-execution-role-cn-northwest-1-t1`, `launchpad-agents-t1`, `launchpad-memory-extract-cn-northwest-1-t1`; OpenSearch indices `launchpad-t1-memory` / `-jobs` / `-kb-docs` / `-kb-<id>` on the shared domain `lp-memory-cn-northwest-1` (D17) |
| Backend + scripts | `LAUNCHPAD_ENV_NAME=t1` (UserData writes it to `/etc/launchpad/env`; `Settings.env_name`) | `launchpad-gw-t1`, `launchpad-office-facts-key-t1`, `launchpad-agent-builder-t1`, per-agent roles `launchpad-agent-<name>-<id8>-t1` |

Rules:

* **Default is unchanged.** An empty `EnvName` (the default) produces exactly
  today's names, byte for byte — `infra/tests/test_naming.py` pins that against
  a snapshot taken before the knob existed. Existing deployments need nothing.
* **Allowed values: `^[a-z0-9]{0,3}$`** (empty, or 1–3 lowercase letters/digits),
  validated identically by the template's `AllowedPattern`, the CDK context and
  `Settings.env_name`, so a bad value is refused before anything is created.
  The three-character cap dates from when every environment owned a domain
  (`lp-memory-cn-northwest-1-` is 25 of the 28 allowed); the shared domain (D17)
  removed that constraint, but the cap is kept until relaxing it is decided
  separately. **`kb` is reserved** (D17): an environment's indices are
  `launchpad-<EnvName>-…`, and `launchpad-kb-…` is the default environment's
  per-KB index pattern — refused by the template rule `EnvNameIsNotReserved`,
  the CDK context and `Settings.env_name` alike.
* **One OpenSearch domain for all environments (D17).** The ~16-minute domain
  creation is paid once per account+region: the first environment's
  `cdk deploy --all` creates `launchpad-memory-domain`, every later one finds
  it and only creates its own `launchpad-base-<env>` + `launchpad-memory-<env>`,
  so a second environment cold-starts in ≈ 12 min instead of ≈ 27. Inside the
  domain each environment owns the indices `launchpad-<env>-memory` /
  `-jobs` / `-kb-docs` / `-kb-<id>` (the default environment keeps
  `launchpad-memory` / `-jobs` / `-kb-docs` / `-kb-<id>`) and a FGAC role
  `launchpad_memory_rw_<env>` whose index permissions cover exactly that
  prefix — `backend/tests/test_index_naming.py` proves no environment's role
  matches another's indices. Generated agents and the extractor Lambda learn
  the prefix from `LAUNCHPAD_ENV_NAME`.
* **The IAM wildcards still match.** The suffix is appended after the existing
  `launchpad-...` name, so the ops role's `role/launchpad-*`,
  `repository/launchpad-*`, `project/launchpad-*` and
  `launchpad-memory-extract-*` resource patterns cover the suffixed names with
  no template change (asserted in `infra/tests/test_ec2_template.py`).
* **One `EnvName` per environment, everywhere.** `scripts/bootstrap.py`
  appends `--context env_name=<Settings.env_name>` to `cdk deploy` itself and
  refuses a `LAUNCHPAD_CDK_CONTEXT` that names a different `env_name`, so the
  backend can never bootstrap against another environment's stacks. When you
  run the scripts by hand (not via this template), export
  `LAUNCHPAD_ENV_NAME=t1` first — that single variable drives both.
* **SSM secrets are independent of `EnvName`.** `GlobalBedrockApiKeyParameter`,
  `ConsolePasswordParameter` and `GitDeployKeyParameter` are *names you pass
  in* (§1), so two environments may point at the same three parameters (one
  key, one password) or at their own copies — your choice; nothing derives
  them from `EnvName`. Pick distinct `--stack-name`s for the two EC2 stacks.
* **Shared on purpose:** the AgentCore Identity API-key provider
  `launchpad-global-bedrock` (the same Global Bedrock key for every
  environment; `ensure_model_credential` is create-or-rotate idempotent and
  teardown never deletes it), the `CDKToolkit` bootstrap stack,
  Transaction Search (account/region-wide observability), and — D17 — the
  OpenSearch domain stack `launchpad-memory-domain` with its FGAC master role
  `launchpad-memory-admin-<region>` and the SSM parameters under
  `/launchpad/memory-domain/<region>/`.
* **Teardown is per environment** (§7): run `teardown.py` with the same
  `LAUNCHPAD_ENV_NAME`; it discovers only `launchpad-*-<EnvName>` stacks,
  gateways and providers, and only per-agent roles tagged `launchpad:env=<EnvName>`
  (untagged roles belong to the default environment and are left alone).

Second environment, in one command (everything else as above):

```bash
aws cloudformation deploy \
  --template-file infra/ec2/launchpad-cn-ec2.yaml \
  --stack-name launchpad-cn-ec2-t1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --disable-rollback \
  --region cn-northwest-1 --profile cn-northwest-1 \
  --parameter-overrides \
    EnvName=t1 \
    AllowedCidr=<your-cidr>/32 \
    GitRepoUrl=git@github.com:<your-fork>/launchpad-cn.git \
    GitBranch=cn/integration \
    GitDeployKeyParameter=launchpad-git-deploy-key \
    GlobalBedrockApiKeyParameter=launchpad-global-bedrock-api-key \
    ConsolePasswordParameter=launchpad-console-password \
    TlsMode=selfsigned
```

## 4. First login

```bash
INSTANCE_ID="$(aws cloudformation describe-stacks --stack-name launchpad-cn-ec2 \
  --region cn-northwest-1 --profile cn-northwest-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' --output text)"
aws ssm start-session --target "$INSTANCE_ID" --region cn-northwest-1 --profile cn-northwest-1
```

Inside that session (shell access with no inbound port at all —
`SessionManagerCommand` output repeats this same command):

```bash
sudo tail -n 100 /var/log/launchpad-bootstrap.log
systemctl status launchpad-backend nginx   # no separate frontend unit — nginx
                                            # serves frontend/dist statically
curl -s localhost:8000/api/health
curl -s localhost:8000/api/capabilities    # expect the ten kept features
```

The username/password are already live — UserData wrote `LAUNCHPAD_AUTH_
USERNAME=admin` and `LAUNCHPAD_AUTH_PASSWORD=<the SSM value from Step 1>`
into `/etc/launchpad/env` before `launchpad-backend` ever started (§0/§1
above), so sign-in works on first boot without any post-boot step. From a
machine inside `AllowedCidr`, open the `ConsoleUrl` stack output (`https://
<public-ip>` under the recommended `TlsMode=selfsigned`) and sign in with
`admin` / the password you stored in `launchpad-console-password`.

**Rotating the password later** (not initial setup — that already happened
above) is the one thing that *is* a post-boot, over-SSM operation, since there
is no CloudFormation update path for a value that only ever lives in `/etc/
launchpad/env`:

```bash
read -rs NEW_PW; echo
sudo env NEW_PW="$NEW_PW" bash -c '
  umask 077
  sed -i "s|^LAUNCHPAD_AUTH_PASSWORD=.*|LAUNCHPAD_AUTH_PASSWORD=$NEW_PW|" /etc/launchpad/env
  chmod 0600 /etc/launchpad/env
  chown root:root /etc/launchpad/env
'
unset NEW_PW
sudo systemctl restart launchpad-backend
```

## 5. Verification commands

```bash
# idempotence: a second bootstrap run must report everything already reused —
# same command `make bootstrap` runs, executed the same way inside the SSM
# session as root (via sudo) from /home/ubuntu/launchpad:
sudo -u ubuntu bash -lc 'cd /home/ubuntu/launchpad/backend && uv run python ../scripts/bootstrap.py'
# -> the "══ bootstrap summary ══" table's "gateway state" / "api-key
#    provider" / "target <name>" rows must all say "reused", never "created"
#    (scripts/bootstrap.py:main — the words it actually prints; "transaction
#    search" instead reports "enabled" or a status string, not reused/created)

# private-access checks (spec C6)
aws s3api get-public-access-block --bucket "launchpad-artifacts-${ACCOUNT_ID}-cn-northwest-1" \
  --region cn-northwest-1 --profile cn-northwest-1
# expect all four BlockPublic* fields true

# OpenSearch access policy (once N6 lands the Memory/KB domain via N8's merge
# — this command is a placeholder for N9 to fill in with the real domain name,
# which N6's own interface doc / `aws opensearch list-domain-names` will show)
aws opensearch describe-domain --domain-name <memory-domain-name> \
  --region cn-northwest-1 --profile cn-northwest-1 --query 'DomainStatus.AccessPolicies'
# expect scoped to the execution/control-plane roles only, no "*" principal
```

## 6. (Optional) Mirror the base image into ECR

Run from the repository root, on any machine with `docker` and the
`cn-northwest-1` profile (not necessarily the EC2 host itself):

```bash
AWS_REGION=cn-northwest-1 AWS_PROFILE=cn-northwest-1 bash scripts/mirror_base_images.sh
# then re-deploy launchpad-base (Step 2) with -c base_image_mirror_enabled=true
```

### 6.1 PyPI mirrors — the host index and the optional secondary one (N11-obs25)

Every Python install that runs **on the host** reads the same primary index: the host's
`uv sync`/`pip` (`/etc/launchpad/env`: `PIP_INDEX_URL`, `UV_INDEX_URL`,
`UV_DEFAULT_INDEX`) and the zip-runtime resolve (`uv pip compile` →
`Settings.pip_index_url`). The default is **`https://pypi.tuna.tsinghua.edu.cn/simple`**
in both places that set it (`Settings.pip_index_url`, EC2 parameter `PipIndexUrl`).
Installs that run **inside CodeBuild** — the agent image builder and the Skill Lab worker
image — read a *different* index: the build mirror of §6.2.

Probe facts from the 2026-10-10 outage (host `t1`, cn-northwest-1; the CodeBuild column
is from the two worker-image builds of the same day — N11-r3-bug7):

| mirror | plain `curl` from the host | `uv` (PEP 658 metadata) | from CodeBuild's NAT egress | verdict |
|---|---|---|---|---|
| `https://pypi.tuna.tsinghua.edu.cn/simple` | certificate **expired** until the renewal at 09:35Z, then healthy | complete | immediate **4xx** — before *and* after the certificate rotation | host default; **never** for CodeBuild |
| `https://mirrors.aliyun.com/pypi/simple` | healthy | advertises `.metadata` files it does **not** serve (404) → `uv sync` / `uv pip` hard-fail | — | **never** use with uv (pip tolerates it) |
| `https://mirrors.ustc.edu.cn/pypi/simple` | healthy | redirects files to tuna | — | same single point of failure as tuna |
| `https://mirror.nju.edu.cn/pypi/web/simple` | healthy | complete (its metadata hops to files.pythonhosted.org) | not probed (first build with it is the probe) | **CodeBuild default**; recommended host secondary |
| `https://mirrors.cloud.tencent.com/pypi/simple` | healthy | pip + uv clean (not probed deeper) | — | **CodeBuild fallback** |

The optional **secondary index** is empty by default and is set in one place per layer:

| layer | knob | effect |
|---|---|---|
| backend / host | `LAUNCHPAD_PIP_EXTRA_INDEX_URL` (→ `Settings.pip_extra_index_url`) | the zip resolve and the workspace CodeBuild project get `UV_INDEX` + `PIP_EXTRA_INDEX_URL`; the Skill Lab worker buildspec passes `--build-arg PIP_EXTRA_INDEX_URL` |
| CDK (`launchpad-base`) | `-c pip_extra_index_url=https://mirror.nju.edu.cn/pypi/web/simple` (or in `LAUNCHPAD_CDK_CONTEXT`) | the shared CodeBuild project env |
| EC2 template | parameter `PipExtraIndexUrl` | written to `/etc/launchpad/env` as `PIP_EXTRA_INDEX_URL`, `UV_INDEX` and `LAUNCHPAD_PIP_EXTRA_INDEX_URL` (only when non-empty) |

It is an *additional* index: uv/pip still resolve from the primary first, so a healthy
primary is unaffected and an outage degrades to the secondary instead of failing the
deploy. Pick only a uv-complete mirror for it (NJU; not Aliyun). When a resolve does
fail, the `package` stage now appends the resolver's own words to its summary
(`… (resolver output: … certificate has expired …)`), so a TLS or index fault is
readable in the job log instead of hiding behind "no matching distribution".

### 6.2 Host mirror vs build mirror — CodeBuild resolves from NJU (N11-r3-bug7)

Both Skill Lab worker-image builds of 2026-10-10 (`cf379555…` 10:19, `15a01adb…` 11:49 —
one before and one after tuna's certificate rotation) failed at the very first
`pip install … --index-url https://pypi.tuna.tsinghua.edu.cn/simple`, 0.2 s after
"Looking in indexes", with `No matching distribution found` and **no retry or TLS line**:
tuna answers the CodeBuild (NAT) egress class with an immediate 4xx while the EC2 hosts
get 200 from the same URL. Nothing had ever pip-installed inside CodeBuild in this account
(zip runtimes resolve on the host), so BYOC container builds — whose uv-based Dockerfiles
take `--build-arg PIP_INDEX_URL` from the CodeBuild project env — are affected the same
way. The build mirror is therefore its **own knob**, and the host keeps tuna:

| layer | host mirror (host-side `uv`/`pip`, zip resolve) | build mirror (CodeBuild projects, worker image) |
|---|---|---|
| backend | `Settings.pip_index_url` (tuna) | `Settings.codebuild_pip_index_url` (NJU) + `codebuild_pip_fallback_index_url` (Tencent) — `LAUNCHPAD_CODEBUILD_PIP_INDEX_URL` / `LAUNCHPAD_CODEBUILD_PIP_FALLBACK_INDEX_URL` |
| CDK (`launchpad-base`) | — (the stack has no host-side install) | `-c codebuild_pip_index_url=… -c codebuild_pip_fallback_index_url=…`; the project env gets `PIP_INDEX_URL` / `UV_INDEX_URL` / `UV_DEFAULT_INDEX` from the first and `PIP_FALLBACK_INDEX_URL` from the second |
| workspace bootstrap (spoke project) | — | the same five variables from the two settings |
| EC2 template | `PipIndexUrl` → `PIP_INDEX_URL` / `UV_INDEX_URL` / `UV_DEFAULT_INDEX` in `/etc/launchpad/env` | `CodeBuildPipIndexUrl` / `CodeBuildPipFallbackIndexUrl` → the two `LAUNCHPAD_CODEBUILD_*` lines **and** `codebuild_pip_index_url=…,codebuild_pip_fallback_index_url=…` appended to `LAUNCHPAD_CDK_CONTEXT`, so the hub project the host deploys with `cdk deploy` gets the same pair (`scripts/bootstrap.py` and `scripts/host-update.sh` split that variable on commas) |
| Skill Lab worker buildspec | — | `--build-arg PIP_INDEX_URL` / `PIP_FALLBACK_INDEX_URL` rendered from the two settings (both are in the image's content hash, so a mirror change rolls the image) |

What the worker build does with the pair:

1. **Pre-flight (buildspec `pre_build`, before `docker build`)** — `curl -sS -o /dev/null
   -w '%{http_code}' <index>/uv/` for the primary *and* the fallback; each HTTP code is
   printed (`pypi pre-flight: https://…/uv/ -> HTTP 403`), and the build fails right there
   only when *neither* answers 2xx/3xx. A blocked egress is now one readable line instead
   of pip's "No matching distribution found" three minutes into the image build.
2. **`uv` itself** — `pip install --index-url "$PIP_INDEX_URL" uv==0.9.10 || pip install
   --index-url "$PIP_FALLBACK_INDEX_URL" uv==0.9.10`.
3. **The Python deps** — `uv pip install --index "$PIP_INDEX_URL" --default-index
   "$PIP_FALLBACK_INDEX_URL" …`, then a retry on the fallback alone. uv ranks indexes:
   `--index` entries beat the `--default-index`, and `--extra-index-url` entries *out-rank*
   the `--index-url` — so the fallback is deliberately the default (lowest-priority) index
   and the primary the preferred one; a `--extra-index-url` fallback would have made
   Tencent the mirror every package is resolved from first. The opt-in secondary of §6.1
   (`PIP_EXTRA_INDEX_URL`) is still added as `--extra-index-url` when set.

Picking a build mirror: it must be uv-complete (NJU is; Aliyun is not — §6.1), and the
first CodeBuild run against it *is* the egress probe — read the pre-flight line in the
build log. To move the build mirror on a running environment, set the two
`LAUNCHPAD_CODEBUILD_*` lines in `/etc/launchpad/env` (spoke projects + worker buildspec on
the next bootstrap/build) and re-deploy `launchpad-base` with the two `-c` context values
(hub project); the host's own `PIP_INDEX_URL` stays untouched.

## 7. Teardown (reverse order)

`scripts/teardown.py` has no `--profile` flag — it builds boto3 clients via
the default session, so export `AWS_PROFILE` instead:

```bash
export AWS_PROFILE=cn-northwest-1
export LAUNCHPAD_EC2_STACK_NAME=launchpad-cn-ec2
# N11: for an environment created with EnvName=t1, also export the same value —
# teardown then targets launchpad-base-t1 / launchpad-memory-t1 / launchpad-gw-t1
# and only the per-agent roles tagged launchpad:env=t1 (and the EC2 stack name
# above becomes whatever --stack-name you gave that attempt):
#   export LAUNCHPAD_ENV_NAME=t1
cd backend && uv run python ../scripts/teardown.py --dry-run --region cn-northwest-1
# review the ec2-stack target appears BEFORE cdk-stack, then:
uv run python ../scripts/teardown.py --yes --region cn-northwest-1
# D17: the shared OpenSearch domain stack launchpad-memory-domain is KEPT by
# default (every environment's indices live in it). Only when this was the last
# environment, delete it too — the flag is refused (the stack is kept, with the
# names of the environments still using it) while any other
# launchpad-memory[-env] stack exists:
#   uv run python ../scripts/teardown.py --yes --delete-shared-domain --region cn-northwest-1
cd ..
```

Order (reverse of creation): gateway targets → gateways → credential providers →
per-agent roles → EC2 stack → `launchpad-memory[-env]` (waited, R21) →
`launchpad-base[-env]` → account-level residue (below; only when this was the last
environment) → `launchpad-memory-domain` (last, opt-in, waited — domain deletion
takes ~10 min).

**Account-level residue (N11-obs43).** Two kinds of leftover survive a stack deletion
and used to need a manual residue pass: the API Gateway CloudWatch role
(`launchpad-base[-env]-OfficeFactsApiCloudWatchRole<hash>-<random>` — CDK creates it for
the sample REST API, attaches `AmazonAPIGatewayPushToCloudWatchLogs`, points the
**account-wide** `AWS::ApiGateway::Account` setting at it and *retains* it on delete, so
every deleted or rolled-back `launchpad-base[-env]` leaves one; deploy-ops 6b found three)
and the service-created `/aws/lambda/launchpad-{hr-database,office-facts,memory-extractor-
<region>}[-env]` log groups of stacks deployed before N11-obs56 (since then the Lambdas log
into CDK-named groups that go with the stack — §8.4). `teardown.py` now deletes both,
after `launchpad-base[-env]`, **only when no other `launchpad-base[-env]` /
`launchpad-memory[-env]` stack is still live in the region** (user ruling 2026-10-14 #3):
the API Gateway account setting is shared by every environment's REST API, so removing
the role while a sibling exists would break its access logging. While another environment
remains, the dry run and the real run both print

```
  note: shared — kept while launchpad-base-t1, launchpad-memory-t1 exist:
    [apigw-cloudwatch-role] launchpad-base-OfficeFactsApiCloudWatchRole1AA11C75-… — …
    [lambda-log-group] /aws/lambda/launchpad-hr-database — …
```

and deletes neither; the last environment's teardown lists them as ordinary
`[apigw-cloudwatch-role]` / `[lambda-log-group]` targets (every environment's — by then each
is an orphan). The rule is automatic (no flag): unlike the shared domain it holds no data,
and the kept-while gate is the same `launchpad-*[-env]` stack discovery as D17's.

`teardown.py` does not delete the three SSM parameters from Step 1 (it has no
record of their names) or the `CDKToolkit` bootstrap stack from Step 2's `cdk
bootstrap` (shared toolkit infrastructure, not launchpad-specific — reused by
any future `cdk bootstrap` in this account/region). Clean those up explicitly
if this is a final teardown, not just a redeploy:

```bash
aws ssm delete-parameter --name launchpad-global-bedrock-api-key \
  --region cn-northwest-1 --profile cn-northwest-1
aws ssm delete-parameter --name launchpad-console-password \
  --region cn-northwest-1 --profile cn-northwest-1
aws ssm delete-parameter --name launchpad-git-deploy-key \
  --region cn-northwest-1 --profile cn-northwest-1
# Also remove the deploy key from the GitHub repo's Settings → Deploy keys
# page — deleting the SSM parameter does not revoke the key on GitHub's side.
# CDKToolkit is intentionally left in place — do not delete it here unless
# you are certain no other stack in this account/region still depends on it.
```

## 8. Updating an existing environment (N11 r3-B)

A running environment takes changes by two different paths. **Code** (`backend/`,
`frontend/`, `scripts/`, the CDK app under `infra/stacks/` + `infra/app.py`) is
pulled and applied on the host in place — §8.1; the CDK-owned part of it (the app and
the Lambda asset directories it packages) is applied only when that run is given
`--with-cdk`, and the script now says so when it is not (N11-obs55, §8.1 "CDK-owned
paths and --with-cdk"). **The EC2 template**
(`infra/ec2/launchpad-cn-ec2.yaml`: the ops role's IAM, UserData, security-group
rules) can only be applied with a CloudFormation stack update from outside the host,
and some of those updates replace the instance — §8.2. Named environments that
predate the shared OpenSearch domain (D17) have one extra ordering rule — §8.3.

### 8.1 Code-only path — `scripts/host-update.sh`

Run it on the host, inside the SSM session, as root — root is used only for `systemctl` /
`journalctl` and to read `/etc/launchpad/env`; every step that writes under the checkout
(`uv sync`, `npm`, the bootstrap, `cdk`, `make verify`) runs as the checkout owner through
`as_owner_env` (`runuser -m`, the sourced environment travels), exactly as the stack's own
UserData ran the first `make bootstrap`; git runs as the owner with the deploy key UserData
installed (N12-bug10):

```bash
cd /home/ubuntu/launchpad
sudo scripts/host-update.sh                 # code + deps + build + bootstrap --skip-cdk + nginx site + restart + health
sudo scripts/host-update.sh --with-cdk      # ...and apply pulled CDK-owned changes (CDK app + Lambda assets) with an explicit `cdk deploy --all`
sudo scripts/host-update.sh --verify        # run `make verify` before the restart
sudo scripts/host-update.sh --dry-run       # print the plan (no root needed)
sudo scripts/host-update.sh --fix-ownership # once, on a host the pre-N12-bug10 script updated as root
```

The sequence, and the N11 findings each step answers:

| # | step | note |
|---|---|---|
| 2 | `data/launchpad.db` → `data/launchpad.db.bak-<UTC stamp>` | always first — the ledger is the only state the host owns. It grows: since r11-b `chat_turn_events` keeps every frame of every console chat turn (the `delta` rows duplicate the `ChatMessage` text, roughly 2× the transcript) and nothing prunes it yet (v3) — watch the `.bak-*` sizes and the root volume |
| 3 | `git checkout -- backend/uv.lock infra/uv.lock` | **obs21.** A host-side `uv sync` rewrote both committed locks on every run (a different `uv` writes a different lock revision); the committed locks are the only locks a host may run. Any *other* local change aborts the update — it is never discarded. **N17-obs485.** The drift seen on the hosts since then came from every `uv run` *without* `--frozen` (steps 9 / 10, the unit's `ExecStart=uv run uvicorn`, an operator's `uv run`): it re-locks first and records the index it resolved against — the env file's CN mirror (`UV_DEFAULT_INDEX`) instead of the committed `pypi.org` — so 125 / 28 `source = { registry = … }` lines changed while names, versions and hashes did not. Steps 9 / 10 and step 12c now run everything frozen; this step still discards what an older host left behind. |
| 4 | `git fetch --prune origin <branch>` under `timeout 1800`, 3 attempts | **obs20.** A 17k-object / 33 MB push arrives at ~115 KB/s from GitHub to cn-northwest-1 (~6 min); the earlier 300 s timeout restarted from zero and silently rebuilt the *old* commit. Keep pushes small, or pre-fetch. |
| 5 | `git merge --ff-only origin/<branch>`, then `git diff --name-only <old> <new> -- <CDK_PATHS>` | never a merge commit on a host. **N11-obs55.** The diff restricted to the CDK-owned paths decides what step 9 and the summary say; without `--with-cdk` a non-empty result prints the multi-line `WARNING: CDK-owned files changed in <old>..<new> but --with-cdk was not given — these are NOT applied: …` on stdout **and** stderr — never an abort. A `--dry-run` plans against the fetched `origin/<branch>` tip (when it is a fast-forward of HEAD) so it reports the same set. |
| 6 | `scripts/uv_lock_fresh.py <project>`, then `uv sync --frozen` in `backend/` and `infra/`; `npm ci` when `package-lock.json` changed | `--frozen` installs exactly the committed lock and never rewrites it. The locks are universal (`uv lock --check` is clean on x86_64 with uv 0.9.10; the resolution markers are platform-independent), so the arm64 host needs no lock of its own. **N17-obs485.** `--frozen` also never *checks* the lock: a `pyproject.toml` whose dependencies changed without `uv lock` would install the old set silently. `uv lock --check` / `--locked` cannot stand in on a host — they resolve against the env's mirror index and report every committed lock as stale — so the step first compares the project's own requirements recorded in the lock (`[package.metadata] requires-dist` / `requires-dev`, `requires-python`) with `pyproject.toml` (stdlib only, no index, no network) and aborts on a difference: run `uv lock` on a dev machine, commit, update again (never `uv lock` on a host). |
| 7 | `npm run build` whenever `frontend/dist/.launchpad-build` is missing, names an unknown commit, or `frontend/` differs between that commit and HEAD (`--full` forces 6+7) | **N11-r3-bug8.** The step used to decide from its own fast-forward range, so a `dist/` built from an *older* pin (b990afc8, restored by hand) survived a pull to newer code and the restart would have served a stale UI. Every build now writes the commit it was built from into `dist/.launchpad-build`; the decision compares that commit's `frontend/` tree with HEAD (a backend-only range keeps `dist/`, an older frontend rebuilds it) and is printed either way (`rebuilding frontend/dist: …` / `frontend/dist built from <sha> … — keeping dist/`), also under `--dry-run`. nginx serves `frontend/dist` in place. |
| 7b | write `data/build-info.json` `{sha, built_at, branch, dist_built_from}` (as the owner, every run) | **N13-obs10 (E01-obs1).** `GET /api/health` serves it as `build{sha, short, built_at, source: "file"}` (`backend/app/services/build_info.py`; falls back to `LAUNCHPAD_BUILD_SHA` in the env, then to the dist stamp's sha + mtime, else nulls) and the V2 sidebar shows `Build <short>` — a demo's `recorded_version` is verified off the console, no ssh. `sha` is HEAD after the fast-forward (what the restarted backend runs); `dist_built_from` is the commit `dist/` was built from (= `sha` after a rebuild, the stamp's commit when `dist/` was kept). **N17-obs466:** health also serves `build.dist_sha` (the dist stamp, else `dist_built_from`); `vite build` compiles the checkout HEAD (or `LAUNCHPAD_BUILD_SHA`) into the bundle (`frontend/vite.config.ts` `define`, `src/v2/buildSha.ts`), and an open V2 tab whose bundle sha differs from `dist_sha` shows the non-blocking reload banner (`src/v2/NewVersionBanner.tsx`) — so the stamp must keep naming the commit `dist/` was built from |
| 8 | `make verify` (`--verify`) | with `AWS_REGION` / `AWS_DEFAULT_REGION` / `LAUNCHPAD_REGION` unset, as the prod runbook requires |
| 9 | `uv run --frozen cdk deploy --all --require-approval never <context>` (`--with-cdk` only; the precondition below is `uv run --frozen` too, N17-obs485) | **obs18.** `make bootstrap` deploys CDK only when a stack is *missing*; a pulled change to `infra/stacks/*` — or to a Lambda asset directory (obs55) — is otherwise never applied. With the flag the step first prints `cdk: <n> CDK-owned files changed — deploying` or `cdk: no CDK-owned changes — deploy is a no-op` (the deploy still runs: a D17 migration or a drifted/missing stack is applied); without it, `skipped — <n> CDK-owned file(s) changed … NOT applied …; pass --with-cdk`. The context is rebuilt the way `scripts/bootstrap.py` builds it: every `k=v` of `LAUNCHPAD_CDK_CONTEXT` plus `env_name=<LAUNCHPAD_ENV_NAME>`; a conflicting `env_name` is refused. **N11-obs44b.** Before the deploy the step runs `scripts/bootstrap.py --check-memory-admin` (as the owner): `memory-admin precondition: ok` / `denied` / `unknown`. `denied` — this host's ops role cannot assume `launchpad-memory-admin-<region>` (pre-D17 EC2 template without `MemoryAdminAssume`) — aborts the run with the §8.3 reason before anything is deployed; `unknown` (the caller is not a role, or its policies cannot be read) warns and continues. |
| 10 | `uv run --frozen python ../scripts/bootstrap.py --skip-cdk` | idempotent — the summary must read `reused`; it aborts if a stack is missing (then you wanted `--with-cdk`) and tolerates the pre-D17 memory layout (N11 r3 bug3). Stages, in order: `model-credential` → registry id → `memory` → `evaluation` → `gateway` → `observability` → **`policy`** (D20 P1-d: Cedar engine + the CloudWatch decisions log group `/launchpad/policy/decisions[-env]`, 90-day retention; `policy decisions log  … · reused` on a re-run) → `registry` defaults → `skill-lab` |
| 11 | ownership check: list anything under `data/`, `config/`, `backend/.venv`, `infra/.venv`, `frontend/dist`, `frontend/node_modules`, `backend/app`, the owner's `~/.cache/uv`, `~/.cache/npm`, `~/.npm` (depth 3; N11-r5-bug4) or the legacy `/tmp/launchpad_build_*` not owned by the checkout owner | **N12-bug10.** The pre-r4-G script ran every step as root and left `data/skill-lab-venv` (7k files), `backend/.venv`, `frontend/dist`, `backend/app/**/__pycache__`, the seeded tasksets and the agent build dir `/tmp/launchpad_build_<name>/deployment_package.zip` root-owned; the backend service (`User=ubuntu`) then failed a repair with `PermissionError`. Steps 2/6/7/8/9/10 now run as the owner (`as_owner_env`), so nothing new can be root-owned; `restore_ownership` (a `chown -R` of a path only when it holds a stray) runs as a safety net after steps 7 and 10, and this step only *asserts* — a leftover is a `WARNING`, never an abort. A host updated by the old script still carries root-owned files, which would make the owner-run steps fail: step 1 refuses with the hint and `--fix-ownership` repairs them once (`chown -R <owner>` of each affected top-level path). Agent packages are now built under the checkout's `data/builds/<agent>/` (git-ignored), not `/tmp`. **N11-r5-bug5.** The owner's caches are written by *other* processes while the scan runs (uv/npm stage downloads as `.tmpXXXX` entries that live for seconds), so a `.tmp*` entry younger than 60 s is not a stray and every candidate is `stat`ed once more before it counts — one that vanished is never reported; a real root-owned file (any name, or an old `.tmp*`) still is. `LAUNCHPAD_OWNER_HOME=<dir>` overrides the owner's passwd home for this scan (and `--fix-ownership`) — a test/ops knob: the hermetic dry-run tests point it at a scratch directory so they never read the live home; an operator can point it at the real home of a service user whose passwd entry says `/nonexistent`. |
| 12 | nginx site refresh — `refresh_nginx_site` from `infra/ec2/nginx-site.sh`: render `/etc/nginx/sites-available/launchpad` (TLS and port read from the live site's `listen` line, dist root = this checkout's `frontend/dist`, backend port 8000), and **only when the rendered file differs**: `nginx -t` on the *current* config, `install` the previous file as `launchpad.prev`, `install` the rendering, `nginx -t`, `systemctl reload nginx`. **This step never aborts the update** (r7-e): if the current config already fails `nginx -t` it prints `WARNING: nginx config already failing before our change — site refresh skipped` and continues; if the rendering fails `nginx -t` it restores `launchpad.prev`, prints `WARNING: the rendered nginx site failed nginx -t — restored <site> from <site>.prev (nginx not reloaded; the service restart still runs)` and continues; if an `install` itself fails (read-only `/etc`, ENOSPC) it prints `WARNING: could not write <site> — nginx not reloaded`, or — when the *restore* after a rejected rendering fails — `WARNING: restore of <site> from <site>.prev FAILED — <site> may be invalid; fix before the next nginx reload` (the rejected file is still on disk), and continues; if the reload itself fails (a stopped nginx passes `nginx -t`) it prints `WARNING: nginx reload failed (is nginx running?) — <site> is installed and valid; reload or start nginx by hand` and continues — step 13 restarts the backend in every case | **WB-003.** The site rule set lives in one file that `bootstrap-host.sh` renders on first boot and this step re-renders on every update, so a host bootstrapped before a rule change picks it up without a rebuild. An unchanged host prints `unchanged — nginx not reloaded` and never touches nginx (not even `nginx -t`); a host whose site predates the `/assets/` rule prints the unified diff it is about to install, also under `--dry-run` (where every root-run command is only printed). The first run on a pre-WB-003 host therefore prints the diff, two `nginx -t` results (before and after the install) and one `systemctl reload nginx`. **Why never abort (r7-e review of r7-B):** steps 5–10 have already fast-forwarded the checkout, rebuilt `dist/` and run the bootstrap by the time step 12 runs; an abort here skipped step 13 and left the *old* backend serving the *new* dist — exactly the mismatch the update exists to avoid. Skipped with a note when the checkout has no `infra/ec2/nginx-site.sh` (pre-WB-003 branch) or its copy predates `refresh_nginx_site` (pre-r7-e), or the host has no site file (not an nginx host; `LAUNCHPAD_NGINX_SITE` overrides the path for tests). The function is unit-tested with fake `nginx` / `systemctl` / `install` binaries (`infra/tests/test_nginx_site.py`) — unchanged, install + reload, rollback, pre-existing failure, dry run. See "Asset caching" below. |
| 12b | public origin — `refresh_public_base_url`: read the `LAUNCHPAD_PUBLIC_BASE_URL=` line of `/etc/launchpad/env`; when its host part is an IPv4 literal, probe IMDSv2 `public-ipv4` (`--max-time 2`, never fatal) and, if the address differs, rewrite that one line with the same scheme and port (`sed -i`, mode preserved) and print the new `/auth/return` URL to re-register with the as-user OAuth2 providers | **N11-obs47.** The first boot caches `<scheme>://<public-ipv4>:<port>` (§3 "Public origin"); t1's in-place stack update re-assigned the instance's public IPv4 and the file kept the old one. The EC2 template now allocates an Elastic IP (`AllocateElasticIp=true`, §8.2 "Elastic IP") — the address changes **once** when that is applied to an existing stack, and this step is what repoints the host afterwards; IMDS `public-ipv4` returns the Elastic IP once it is associated, so no separate lookup is needed. Prints `unchanged — … matches IMDS public-ipv4`, `kept — …: host is not an IPv4 literal` (an operator-set DNS name / reverse-proxy origin is never rewritten), or `skipped — …` (no env file, no line — origin set through a systemd drop-in — or no public IPv4 from IMDS). Runs before the restart so the backend comes up with the right 3LO return URL. `LAUNCHPAD_PUBLIC_IP` / `LAUNCHPAD_IMDS_ENDPOINT` override / redirect the probe (test/ops knobs; the hermetic tests never reach `169.254.169.254`). Numbered 12b to keep the `1..14` step labels stable. |
| 12c | uv frozen — `ensure_uv_frozen_env` + `ensure_uv_frozen_unit`: append `UV_FROZEN=1` to `/etc/launchpad/env` once (an existing `UV_FROZEN=<other>` is corrected in place; mode preserved), and rewrite every `ExecStart=… uv run uvicorn` in `/etc/systemd/system/launchpad-backend.service` and its `.service.d/*.conf` drop-ins to `uv run --frozen uvicorn`, then `systemctl daemon-reload` | **N17-obs485.** Without it the service (on every restart) and any operator `uv run` re-lock against the mirror and leave `uv.lock` rewritten in the checkout, which hides a real local edit in `git status`. Prints `unchanged — …` on a host already frozen, `skipped — …` without an env file or without a `uv run uvicorn` ExecStart; runs before the restart so the restarted service is already frozen. `LAUNCHPAD_SYSTEMD_DIR` (default `/etc/systemd/system`) is a test/ops knob. The first-boot path (`infra/ec2/bootstrap-host.sh`, run by the EC2 UserData) is deliberately unchanged — a UserData change on the instance can force a stop/start; it gets the same two lines at the next AMI / instance refresh, and the first host update after boot applies them meanwhile. |
| 13 | `systemctl restart launchpad-backend` (+ `launchpad-frontend` when that unit exists) | `resume_pending_jobs()` re-enters interrupted deploy stages; since r3-B a resumed `deploy` adopts the runtime the dead process created and writes nothing to IAM when the role is already right (N10v2-bug1). Workspace bootstrap jobs the restart cut short are swept first (r17-ws, rev-r17 nit): a full job still `running` is resumed (idempotent stages), a validate-only probe is marked `failed` ("interrupted by a backend restart …") and its workspace handed back the status it had, and a row stuck `bootstrapping` with no live job is restored from its newest job — so a restart never leaves a workspace that answers `409 workspace.bootstrap_conflict` forever (the smoke of "After each host update" would otherwise fail on it) |
| 14 | poll `/api/health`, count `error|traceback` journal lines | want 0 |

**CDK-owned paths and --with-cdk (N11-obs55).** A routine update to a tip that changed
`backend/lambdas/memory_extractor/handler.py` left the deployed extractor Lambda on the old
code: the handler is a CDK asset of `launchpad-memory[-env]`, step 9 runs only with
`--with-cdk`, and nothing else redeploys Lambda code — the operator only noticed when asked.
The script therefore owns one list of **CDK-owned paths**, `CDK_PATHS` in
`scripts/host-update.sh` (the single source of truth; this paragraph mirrors it and
`backend/tests/test_host_update_script.py` fails on drift): `infra/app.py`, `infra/cdk.json`,
`infra/pyproject.toml`, `infra/uv.lock`, `infra/stacks/` (the CDK app) and the directories
it packages as Lambda assets — `backend/lambdas/` (the memory extractor, `memory_stack.py`),
`samples/hr_database_lambda/` and `samples/rest_api/` (the two sample Lambdas,
`base_stack.py`). Deliberately *not* CDK-owned: `infra/ec2/` (the EC2 template, applied
with CloudFormation from outside the host — §8.2 — and the UserData/nginx scripts the host
runs directly), `infra/spoke/` (the spoke-account CloudFormation template) and
`infra/tests/`. After the fast-forward (step 5) the script diffs `<old>..<new>` against that
list:

- **changed, no `--with-cdk`** → the WARNING above on stdout and stderr (every file
  listed, then `re-run with --with-cdk`), the step-9 `skipped — … NOT applied` line, and
  the run ends with `cdk: pending (<n> files — re-run with --with-cdk)` in the summary.
  The exit code stays **0** — code, deps, dist, bootstrap and the restart are valid; only
  the CDK half is outstanding. Re-run with `--with-cdk` (idempotent; the second run finds
  everything else `reused`);
- **changed, `--with-cdk`** → `cdk: <n> CDK-owned files changed — deploying` before the
  deploy, `cdk: deployed (<n> files)` in the summary;
- **unchanged** → `cdk: no CDK-owned changes` in step 5; the summary reads `cdk: no
  changes` (or `cdk: deployed (no CDK-owned changes)` when the flag was given anyway, e.g.
  for the §8.3 migration).

The summary line is therefore always `══ host-update done · <old> -> <new> · branch
<name> · cdk: <no changes | pending (…) | deployed (…)> ══` — grep for `cdk: pending`
in an update log to find a host whose stacks lag its code. `--dry-run` prints the same
detection: it plans against the `origin/<branch>` tip the last fetch left (only when that
tip is a fast-forward of HEAD, mirroring `--ff-only`; otherwise HEAD → HEAD). Take the
pre-deploy `cdk diff` gate on the NEW tree (a temporary worktree of the fetched tip), not on
the pre-fast-forward checkout — the checkout still carries the old assets and the diff is
vacuous (operator finding 2026-10-12).

**A skipped CDK change stays pending until `--with-cdk` (N17-obs472).** The `<old>..<new>`
diff alone forgets: on t1 (2026-10-26) the obs465 builder grant in `infra/stacks/base_stack.py`
was skipped by a plain update (`cdk: pending`), and the next plain update — whose range no
longer held the file — reported `cdk: no changes` while the base stack still drifted. A plain
update now appends every CDK-owned file it skipped to `data/cdk-pending` (one
`<old>..<new> <path>` line, written as the checkout owner), and EVERY later update reads it:
without `--with-cdk` it prints `WARNING: CDK-owned changes skipped by an earlier plain update
are still NOT applied (data/cdk-pending):` with the lines, on stdout and stderr; step 9 says
`skipped — <n> carried CDK-owned files still NOT applied`; the summary reads
`cdk: pending (<n> carried files — re-run with --with-cdk)` (or `<m> files + <n> carried
files`). With `--with-cdk`, step 5 says `step 9 applies <n> carried CDK-owned files … too`, the
marker is removed only AFTER `cdk deploy --all` succeeded, and the summary reads
`cdk: deployed (…; <n> carried files applied)`. A failed deploy keeps the marker. To reset a
host whose stacks were brought up to date out of band, delete `data/cdk-pending` after a
`cdk diff` showed no difference.

**Asset caching (WB-003).** Vite writes content-hashed chunks under `frontend/dist/assets/`;
a rebuild (step 7) replaces them, and a tab that was open across the update still holds
the old `index.html` with the old chunk names. The nginx site therefore has two rules the
original inline config lacked — `location /assets/ { try_files $uri =404; add_header
Cache-Control "public, max-age=31536000, immutable"; }` (a hashed chunk is cached for a
year, and one nginx no longer has answers **404**, never `index.html` as `text/html`, which the
browser cannot parse as a module script) and `Cache-Control: no-cache` on `location /`
(the entry `index.html` and the few non-hashed files are revalidated on every load). The
console's route boundary (`frontend/src/layout/RouteChunk.tsx`) reloads the tab **once** on a
stale chunk (a `sessionStorage` mark per path, cleared when the page renders) and only then
shows the manual "reload" state, so the user no longer sees
`加载失败：本页面的代码 …` after a host update. The `/api` and `/v1` proxy block is unchanged.
Since r13-e the site has a fourth rule, `location /media/` — the self-hosted demo-video
media, aliased from `/home/ubuntu/launchpad-media/` outside the checkout with the same
immutable caching (§8.8); step 12 rolls it onto a live host like any other rule change.

**Running a NEW version of the script — copy-then-run (N11-obs36).** The fast-forward
in step 5 must never rewrite the file that is executing (bash reads a script
incrementally; a rewritten file mid-run executes garbage). So whenever the update you are
pulling changes `scripts/host-update.sh` itself — or whenever you are not sure — run the
*incoming* copy from outside the checkout and point it at the checkout with `--root`
(`LAUNCHPAD_ROOT` is the env equivalent; the flag wins; with neither, the script still
defaults to the checkout it lives in, so the in-checkout invocations above are unchanged):

```bash
cd /home/ubuntu/launchpad
sudo -u ubuntu timeout 1800 git fetch --prune origin cn/integration        # the new code, as the owner
git show FETCH_HEAD:scripts/host-update.sh > /root/host-update-$(git rev-parse --short FETCH_HEAD).sh
chmod +x /root/host-update-*.sh
sudo /root/host-update-<sha>.sh --root /home/ubuntu/launchpad --branch cn/integration [--fix-ownership]
sudo /root/host-update-<sha>.sh --root /home/ubuntu/launchpad --dry-run              # plan only
```

Preflight prints which source named the checkout (`1/14 preflight (/home/ubuntu/launchpad ·
from --root)`); a copy run from `/root` without `--root` / `LAUNCHPAD_ROOT` stops in step 1
with `… is not a git checkout — pass --root <checkout> …` instead of touching anything. The
copy is throw-away: the fast-forward installs the same version under `scripts/` for the next
in-checkout run.

**Reading the backend log afterwards.** The unit's stderr goes to journald, and every
record carries its level and logger name (`app/core/logging.py::configure_logging`,
format `LEVEL logger: message`; uvicorn's access lines keep uvicorn's format). Useful
greps on the host:

```bash
sudo journalctl -u launchpad-backend --since "-30 min" --no-pager | grep -E "^(WARNING|ERROR)"
sudo journalctl -u launchpad-backend --since "-30 min" --no-pager | grep opensearch_retry   # OpenSearch retries (N-SCN-obs6/obs11)
sudo journalctl -u launchpad-backend --since "-30 min" --no-pager | grep launchpad.knowledge # KB detail jobs_degraded warnings
```

A retry line reads `WARNING app.services.opensearch_retry: opensearch POST
https://…/launchpad-jobs/_search: ReadTimeoutError — retry 1/1 in 1s`; two of them
bracketing a gap in the KB detail polls is the N-SCN-obs11 signature (now answered from
the detail's job cache instead of stalling). Hosts updated before this change printed the
bare message without level or logger name, so these greps found nothing there.

Bootstrapping the script itself: a host that predates r3-B does not have it yet —
pull once by hand with the same care (`timeout 1800 git fetch --prune origin
<branch>` as `ubuntu`, then `git merge --ff-only`), after which the script owns
every later update. `docs/agent-runbook-prod.md`'s "Update recipe" is the Global
(pre-CN) sequence; CN hosts use this script.

**After each host update — the second-region release smoke (r17-ws, N17-obs117).** Once step
14 reads healthy, prove that the hub can still provision a workspace in the OTHER CN region
(N17-obs112 was found by a user, not by a test: the EC2 role's scoped policy is pinned to the
stack's region). The smoke registers a throwaway workspace `smoke-<region>-<stamp>`, runs the
bootstrap in validate-only mode (`POST /api/workspaces/{id}/bootstrap {"validate_only": true}`
— the `validate-access` stage alone, nothing provisioned, api.md "Console Workspaces API —
bootstrap"), waits for the job and purges the workspace again whatever happened. What the stage
probes, by name (N17-obs130, cn/r17-v): `bedrock-agentcore-control ListGateways` and
`s3 ListBuckets` (granted on `*`), `ecr DescribeRepositories` on the `launchpad-agents[-env]`
repository, `codebuild BatchGetProjects` on the `launchpad-agent-builder[-env]` project and
`iam GetRole` on the `launchpad-agent-execution-role-<region>[-env]` role — the exact resources
the storage / codebuild / iam stages create, so a `RepositoryNotFoundException` / `NoSuchEntity`
/ an empty `projects` list reads as "usable" and the scoped `launchpad-bootstrap` policy grants
every probe in the stack's own region (the other region still trips the `${AWS::Region}`-pinned
ECR / CodeBuild statements — N17-obs112, exit `2` on t1 by design):

```bash
cd /home/ubuntu/launchpad/backend
LAUNCHPAD_E2E_USERNAME=<console admin> LAUNCHPAD_E2E_PASSWORD=<password> \
  uv run python scripts/e2e_workspace_region_smoke.py --region cn-north-1   # on a cn-northwest-1 hub
echo RC=$?
```

Read the exit code, not the log: `0` = the job SUCCEEDED (the host can provision there);
`2` = the job FAILED with an access denial — **by design on t1** (`HubAdminAccess=false`, the
least-privilege role; the message says so and prints the stage detail), a **regression on the
default host** (`HubAdminAccess=true` since 2026-10-20: check the stack parameter and the
instance role's managed policies); `1` = anything else (a non-denial failure, a timeout, a
refused registration, or the purge failing — then the printed workspace id must be purged by
hand: `POST /api/workspaces/<id>/purge`). The script needs the console admin credentials
(`backend/scripts/_e2e_client.py`) and runs against `http://127.0.0.1:8000` by default; it is
real AWS and never part of `make verify` (C8). The hermetic twin that runs in the gate is
`backend/tests/consistency/test_workspace_permissions.py` (testing.md §6.10).

**Reading a `validate-access` failure — the four verdicts.** The stage detail (also the job's
`error`, and what the smoke prints) is one of four sentences; each names a different operator
action. The cause in parentheses is `core.errors.redacted_error` — the exception type / code and
the operation, never botocore's message with the endpoint URL (N17-obs132, N17-obs200):

| the detail says | what happened | retried? | do |
|---|---|---|---|
| `<service> <Operation> failed in <region>: <Code> — the hub's credentials must be able to provision this workspace` | the service answered with a refusal (`AccessDenied…`, any other 4xx code) | no (the stage-level AccessDenied propagation retry aside) | fix the hub role's policy for that action / resource (`services/workspace_permissions.py` lists every need; §8.2 updates the template) |
| `<service> is not usable in <region> (<Operation>: <Code>) — region not supported; register the workspace in a region where …` | endpoint RESOLUTION failed (`UnknownServiceError`, `NoRegionError`) or the service's own "not here" code (`UnrecognizedClientException`, `OptInRequired`, …) | no — a retry cannot make a region appear | register the workspace in a region where AgentCore and its dependencies exist |
| `<service> <Operation> failed in <region> after 2 attempts (<cause>) — transient — retry the bootstrap; neither the region nor the hub's permissions are in question` | a transport timeout (`ReadTimeoutError`, `ConnectTimeoutError`), a throttle or a server-side 5xx, twice | once (2 s) | re-run the bootstrap (`POST /api/workspaces/{id}/bootstrap`); nothing to change |
| `<service> endpoint (<service> · <region>) unreachable from this host after 2 attempts (<Operation>: EndpointConnectionError) — network/DNS, not the region: check the hub host's egress and DNS resolution …` | the endpoint resolved but the connect failed twice — the HUB's network or DNS (a supported region during an outage raises exactly this) | once (2 s) | on the hub host: `getent hosts <service>.<region>.amazonaws.com.cn`, the NAT / security-group egress, the VPC endpoints if any; then re-run — never re-register elsewhere (N17-obs200) |

A fifth text, `cannot assume this workspace's role (…)`, is STS's and means the spoke role / external
id of the workspace registration (architecture.md "Workspaces", `infra/spoke/launchpad-workspace-role.yaml`), not the probed service.

### 8.2 Template path — `infra/ec2/launchpad-cn-ec2.yaml`

Template changes are applied by re-running the §3 command against the **same**
stack name with the **same** parameter values **plus `AmiId=<running AMI>`**
(`aws cloudformation deploy --template-file infra/ec2/launchpad-cn-ec2.yaml
--stack-name <name> --capabilities CAPABILITY_NAMED_IAM --parameter-overrides
<exactly as created> AmiId=<running AMI>` — §3 "AMI pin" has the
`describe-instances` command that reads it; the "AMI pin" bullet below says why
it is not optional). Always preview first:

```bash
aws cloudformation deploy ... --no-execute-changeset          # prints the change-set ARN
aws cloudformation describe-change-set --change-set-name <arn> \
  --query 'Changes[].ResourceChange.[LogicalResourceId,Action,Replacement]' --output table
```

**Preview gate.** Read the `Instance` row before anything else. No `Instance` row,
or `Replacement: False` — fine. **`Replacement: True` or `Conditional` on
`Instance` is a STOP: the change set must never be executed.** It means `ImageId`
is about to change — `AmiId` was omitted or differs from the running AMI (N11-obs48b
below) — or UserData was edited; find out which with
`aws cloudformation describe-change-set --change-set-name <arn> --query
'Changes[?ResourceChange.LogicalResourceId==`Instance`].ResourceChange.Details[].Target.Name'`
(`ImageId` in that list = the AMI, fix the parameter and re-create the change set —
**except** on the very first update that introduces `AmiId` to a stack, where the
expression change itself lists `ImageId`; that one is executed only after the
resolve/running/pinned AMI triple has been verified equal, see §3 "AMI pin";
`UserData` only = the in-place path of N11-obs47, see the UserData bullet). Delete a
refused change set with `aws cloudformation delete-change-set --change-set-name <arn>`;
`deploy --no-execute-changeset` leaves it pending otherwise.

* **IAM-only changes update in place.** The ops role's inline policy
  (`IamForLaunchpadRoles`, the r2-B `iam:ListRolePolicies` /
  `iam:ListAttachedRolePolicies`, the r3-B `IamAdoptAgentRoles` statement) shows
  `Replacement: False`; the instance is untouched and the new permissions are
  effective within seconds. Until a template update is applied, the backend paths
  that needed those permissions degrade instead of failing (r3-B: `ensure_role` only
  rewrites a trust policy that differs; `delete_role` falls back to the known inline
  policy names) — the statement is still the real fix.
  **N17-obs201 (cn/r17-w):** the `iam:UpdateAssumeRolePolicy` grant is now two
  statements, each with a resource-tag Condition so IAM enforces what the code's tag
  check enforces (`workspace_iam.ensure_role` refuses an untagged role): `IamAdoptAgentRoles`
  keeps `role/launchpad-agent-*` under
  `Condition: {StringLike: {"iam:ResourceTag/launchpad:agent-id": "*"}}` (the tag
  `agent_iam.ensure_role` sets in its `CreateRole` call), and the new `IamAdoptWorkspaceRoles`
  names `role/launchpad-agent-execution-role-*`, `role/launchpad-skill-lab-worker-*`,
  `role/launchpad-gateway-role-*`, `role/launchpad-codebuild-role-*` under
  `Condition: {StringLike: {"iam:ResourceTag/launchpad:workspace": "*"}}` (the tag
  `workspace_iam.ensure_role` sets in its `CreateRole` call — before any trust write — and
  re-stamps with `TagRole` when it adopts a retired workspace's role, also before the write).
  Effect on a host: nothing for the bootstrap's own roles (all tagged at creation); the hub's
  CDK-made `launchpad-gateway-role-<region>` / `launchpad-agent-execution-role-<region>`
  (tagged `launchpad:managed` only) are now denied the trust rewrite by IAM itself. The one
  path that could previously slip through — `ensure_role` adopting a role whose tags it could
  not read (`ListRoleTags` denied AND `GetRole` without `Tags`, N11-r3-bug5) — now fails that
  adopt with `AccessDenied` on `UpdateAssumeRolePolicy` when the role really is untagged, which
  is the correct outcome. Apply with the usual §8.2 `deploy` (IAM-only, in place); verify with
  `aws iam get-role-policy --role-name <ops role> --policy-name <inline name> | grep -A3 IamAdoptWorkspaceRoles`.
  The spoke template `infra/spoke/launchpad-workspace-role.yaml` (`IamOnLaunchpadRoles`) still
  grants the action on `role/launchpad-*` unconditionally — the cross-account spoke owns every
  role in its account, so no twin exists there; left as is.
* **AMI pin (N11-obs48b) — pass `AmiId=<running AMI>` on every update.** The template's
  default `ImageId` is the dynamic reference `{{resolve:ssm:…/ubuntu/server/24.04/stable/
  current/arm64/hvm/ebs-gp3/ami-id}}`, and CloudFormation re-resolves dynamic references
  on every update: the two stacks created before `AmiId` existed showed `Instance`
  `Replacement: Conditional` on **every** change set, and the first newer Canonical
  24.04 arm64 AMI would have turned any unrelated update (this Elastic IP add
  included) into an instance replacement — new host, `data/launchpad.db` and
  `/etc/launchpad` gone. `AmiId` (`String`, default empty, `AllowedPattern` empty or
  `ami-` + 8–17 hex digits) drives the `UseLatestAmi` condition: empty → the lookup
  (create time only); set → `ImageId: !Ref AmiId`, a literal, no dynamic reference
  left anywhere in the template (`infra/tests/test_ec2_template.py` asserts the
  `UseLatestAmi` branch is the only one). Deploy-ops procedure, **every** update:
  1. Read the running AMI once per stack and keep it with the stack's parameter
     values (§3 "AMI pin"):
     `aws ec2 describe-instances --instance-ids <id> --query
     'Reservations[0].Instances[0].ImageId' --output text`.
  2. Add `AmiId=<running AMI>` to `--parameter-overrides`, preview with
     `--no-execute-changeset`, and apply the preview gate above: an `Instance` row
     with `Replacement: True`/`Conditional` is a STOP.
  3. **First-time migration** (stacks created before this parameter — `launchpad-cn-ec2`
     and `launchpad-cn-ec2-t1`): passing the running AMI makes the deployed and the
     new `ImageId` equal, so the update is a **no-op for `ImageId`** — the `Instance`
     row disappears from the change set instead of showing `Conditional`. That is
     the only acceptable outcome; if it still shows, the value is wrong — stop.
  Moving to a newer AMI is a deliberate maintenance (ledger backup, or a second
  `EnvName` environment and teardown of the old one — §3.1, §7), never a side effect.
* **UserData changes replace the instance** (`Replacement: True` on
  `AWS::EC2::Instance`): a new public IP, a fresh clone of `GitBranch`, a fresh
  `make bootstrap`, and **`data/launchpad.db` is gone** with the old root volume.
  (N12-bug10: the first boot runs that bootstrap — and `uv sync`, `npm install`, `cdk bootstrap`,
  `npm run build` — as `ubuntu` through `runuser -m`, so nothing under the checkout is ever
  root-owned; `host-update.sh` keeps the same rule for every later update.)
  Since N11-r5-bug1 the UserData is only the **stub** (§3 "Two-stage first boot")
  and rarely changes — a new parameter the body needs, a change to the clone or to
  how the deploy key is fetched. **The body, `infra/ec2/bootstrap-host.sh`, is a
  CODE change**: it is cloned and run by the *next cold start / instance
  replacement* of whatever `GitBranch` the stack points at, so editing it neither
  produces a change set nor touches a live host — and `scripts/host-update.sh` does
  **not** re-run it (hosts that booted from the pre-split inline UserData have no
  `/etc/launchpad/bootstrap.env` and keep updating in place exactly as before).
  Changing both at once (a new `LAUNCHPAD_BOOT_*` key) means: body first on the
  branch, then the stack update, so the branch the replacement instance clones
  already declares the key the new stub writes — the template test enforces the
  parity on the branch, not across branches.
  Treat it as a maintenance: back the ledger up off-host first (`scp`/SSM) and restore
  it before the first login, or — cleaner — create the new state as a second
  `EnvName` environment (§3.1) and tear the old one down (§7) once it is verified.
  The pending r2 template changes (`LAUNCHPAD_PUBLIC_BASE_URL` in UserData, the
  `ConsolePort` rule) are of this kind, which is why they were deferred; in the
  meantime the public base URL is set through the systemd drop-in from
  `docs/agent-runbook-prod.md`.
* **Elastic IP (N11-obs47) — an in-place update with one address change.** Stacks
  created before this change have no Elastic IP: a stop/start, and the in-place
  UserData update above, re-assign the instance's public IPv4 (t1: 203.0.113.50 →
  203.0.113.40; that address was retired 2026-10-14 when the Elastic IP below was
  applied — t1 is now `https://203.0.113.20:8443`, same instance) and
  `/etc/launchpad/env` keeps the old `LAUNCHPAD_PUBLIC_BASE_URL`.
  The template now has `AllocateElasticIp` (default `true`): an `AWS::EC2::EIP`
  (`Domain: vpc`) plus a separate `AWS::EC2::EIPAssociation` behind the `UseElasticIp`
  condition, and the `PublicIp` / `ConsoleUrl` outputs follow the EIP. Because the
  association is its own resource, applying it to an existing stack **does not touch the
  instance**: the change set shows `Add` on `ElasticIp` and `ElasticIpAssociation` and
  `Replacement: False` (or no entry) for `Instance` — verify that in the preview before
  executing. Deploy-ops procedure, once per environment:

  1. Re-run the §3 `aws cloudformation deploy` against the same stack with the **same**
     parameter values plus `AllocateElasticIp=true` (new stacks need nothing: it is the
     default) **and `AmiId=<running AMI>`** (the "AMI pin" bullet — without it the
     dynamic `ImageId` lookup puts an `Instance` `Replacement: Conditional` row into this
     very change set), first with `--no-execute-changeset`; confirm the change set is
     **exactly the two `Add`s** (`ElasticIp`, `ElasticIpAssociation`; an EIP from a
     template without the parameter also shows the modified outputs). **An `Instance`
     row is a STOP** — do not execute — unless you deliberately changed UserData in the
     same update *and* its `Replacement` is `False`/`Conditional` with `ImageId` **not**
     among the `Details[].Target.Name` (i.e. `ImageId` equal: `AmiId` matches the running
     AMI). Then execute it.
  2. **Expect the console address to change once**: the association supersedes the
     auto-assigned public IPv4, and the new address is the stack's `PublicIp` /
     `ConsoleUrl` output (`aws cloudformation describe-stacks --stack-name <name>
     --query 'Stacks[0].Outputs'`). Reach the console at the new address; it stays fixed
     from now on (stop/start, in-place UserData updates).
  3. On the host, run `scripts/host-update.sh` (§8.1; any variant — it is step 12b that
     matters): it rewrites `LAUNCHPAD_PUBLIC_BASE_URL` to the Elastic IP from IMDSv2 and
     restarts the backend. Re-register `<ConsoleUrl>/auth/return` with every OAuth2
     provider used by as-user tools (the URL embeds the address).
  4. Record the new address wherever the old one was written down (runbooks, the bugs
     register, the operator's bookmarks).

  Security posture is unchanged: the console is still private — inbound is only
  `ConsolePort` from `AllowedCidr` (the security group is untouched and `AllowedCidr` is
  passed through as before), so an Elastic IP creates no world-accessible entry point for
  Palisade/Epoxy to flag; what changes is only that the address is now stable. Do **not**
  flip an EIP stack back to `AllocateElasticIp=false` on a live host: releasing the EIP
  leaves the instance without a public IPv4 until its next stop/start (auto-assigned
  addresses are not re-attached on disassociation). The EIP is released with the stack
  (§7). Default quota: five Elastic IPs per region per account.
* A stack left in `CREATE_FAILED` with rollback disabled (the default environment's
  first attempt) cannot be updated — delete it and recreate (§7 order: the EC2 stack
  before the CDK stacks it bootstrapped).

### 8.3 Named environments and the shared-domain migration (D17, obs24)

An `EnvName≠""` environment created **before** D17 owns its own OpenSearch domain
through `launchpad-memory-<env>`; the index names it uses are the unprefixed
`launchpad-memory` / `-jobs` / `-kb-docs` / `-kb-<id>`. D17 code names them
`launchpad-<env>-…` on the shared domain `launchpad-memory-domain`. Restarting the
backend on D17 code **before** the stacks and indices have moved leaves a
half-migration: the backend writes to the new names while every record still lives
under the old ones.

Required order — exactly what `sudo scripts/host-update.sh --with-cdk` does (the flag is
also what applies any CDK-owned file change the pull carries — §8.1 "CDK-owned paths and
--with-cdk"; a run without it reports them as `cdk: pending`):

1. fetch + fast-forward the code (**no restart yet**; the running backend keeps
   serving the old layout);
2. `cdk deploy --all -c env_name=<env>` — creates `launchpad-memory-domain` if the
   account+region does not have it yet (~16 min) and re-points
   `launchpad-memory-<env>` at it;
3. `scripts/bootstrap.py --skip-cdk` — its memory stage creates the
   `launchpad-<env>-*` indices and the per-environment FGAC role on the shared domain
   (`created`, not `reused`, on this one run). Long-term records and KB chunks under
   the old names are **not** copied: either accept re-creation (memory is rebuilt by
   the extractor from the S3 event store; KBs are re-ingested from the console) or
   reindex them by hand before step 4;
4. `systemctl restart launchpad-backend`.

The default environment (`EnvName=""`) is unaffected: its names do not change, and
`bootstrap.py --skip-cdk` keeps working on the pre-D17 layout until the migration is
scheduled (N11 r3 bug3).

**`MemoryAdminAssume` on the ops role (N11-obs44).** Step 3's memory stage reaches the
shared domain by assuming the stack-owned admin role `launchpad-memory-admin-<region>`;
the grant is the `MemoryAdminAssume` statement of the EC2 template
(`infra/ec2/launchpad-cn-ec2.yaml`, `sts:AssumeRole` on `role/launchpad-memory-admin-*`).
A host whose EC2 stack was created from a pre-D17 template does not carry it. When the
assumption is refused the memory stage no longer labels the denial "AccessDenied right
after an IAM change (propagation)" and retries: it stops at the first denial, skips the
substrate probe (every OpenSearch call would fail the same way) and records

```
memory substrate unavailable — cannot assume the memory admin role
arn:aws-cn:iam::<account>:role/launchpad-memory-admin-<region> (AccessDenied on
sts:AssumeRole) — the caller <ops role ARN> lacks the `MemoryAdminAssume` statement
(sts:AssumeRole on role/launchpad-memory-admin-*, infra/ec2/launchpad-cn-ec2.yaml); a host
created from a pre-D17 template needs its EC2 stack updated to the current template before
`--with-cdk` (deploy.md §8.3)
```

as `memory_unavailable_reason` (the console degrades to `memory.not_provisioned`; the other
stages still run — the fix is §8.2, the EC2 stack update, then `make bootstrap` again).

**Precondition (N11-obs44b).** The same gap is caught *before* the deploy, so a migration
does not spend ~16 min to end in a degraded memory stage: `scripts/bootstrap.py` runs the
check before `cdk deploy` (and `scripts/host-update.sh --with-cdk` runs
`scripts/bootstrap.py --check-memory-admin` in step 9 — exit 1 aborts the update before
step 10/13). When `launchpad-memory-domain` already exists the admin role is assumed once
for real; when it does not exist yet (the first migration in the account+region creates
the role) the caller role's inline policies are read for the `MemoryAdminAssume` statement
(`iam:ListRolePolicies` / `iam:GetRolePolicy` on `role/launchpad-*`, which the ops role
has). The verdict is one line, `memory-admin precondition: ok|denied|unknown — <detail>`;
`denied` prints the reason above and aborts; `unknown` (an IAM user as caller, no
permission to read the policies, an STS error that is not a denial) warns and continues.
A pre-D17 host therefore needs §8.2 (the EC2 stack update that adds the statement to its
ops role) **before** its first `--with-cdk`.

**A degraded memory stage never aborts the update (N11-obs44c).** When the precondition
was undecided (or something else fails the stage — a FGAC call, an unreachable endpoint,
the refused AssumeRole above), the memory stage degrades on *any* error: the probe
decides between "healthy, step skipped" (WARN) and "unavailable" (reason recorded), and
the evaluation / gateway / observability / policy / registry / Skill Lab stages run as usual.
`scripts/bootstrap.py` exits 0 and prints a `memory stage DEGRADED  <reason> — the stages
after memory ran; fix the cause and re-run \`make bootstrap\`` row, so step 10 of
`host-update.sh` completes and the restart (step 13) happens; the console answers
`memory.not_provisioned` until the cause is removed and `make bootstrap` is re-run.

### 8.4 Log-group retention (N11-obs56)

Nothing bounded CloudWatch Logs retention before r8-nits: the Runtime creates
`/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT` itself with no retention, and the
stacks' Lambdas (`launchpad-hr-database`, `launchpad-office-facts`,
`launchpad-memory-extractor-<region>`) logged into service-created `/aws/lambda/<fn>`
groups with none either. Two mechanisms now apply the same 90 days:

- **Per-runtime groups — the deploy stage.** `Settings.log_group_retention_days`
  (default `90`; `LAUNCHPAD_LOG_GROUP_RETENTION_DAYS`; must be a value
  `PutRetentionPolicy` accepts, `0` disables the step) is applied by the last step of the
  zip / studio / Harness / BYOC deploy stage: `CreateLogGroup` (idempotent —
  `ResourceAlreadyExistsException` is the redeploy answer) then `PutRetentionPolicy`,
  through the workspace's funnel `logs` client. Fail-soft: a denied call is one
  `log-group retention WARN: … keeps the account default (never expires)` line in the
  deploy log, never a failed deploy. The EC2 ops role already carries `logs:*`; the spoke
  workspace role gained `logs:CreateLogGroup` + `logs:PutRetentionPolicy`
  (`infra/spoke/launchpad-workspace-role.yaml`) — re-deploy that template in spoke
  accounts, or the WARN line names the two grants. Runtimes deployed before this change
  pick the retention up on their next redeploy; the teardown sweeps the group as before.
- **Policy decisions group — the `policy` bootstrap stage (D20 P1-d).** The self-hosted
  Policy module mirrors every decision row to `/launchpad/policy/decisions[-env]`
  (`services/policy_audit.py`); `make bootstrap` creates that group with the same
  `Settings.log_group_retention_days` (hub: `ensure_policy_decisions`; a console-registered
  spoke workspace: its `policy` bootstrap stage, under the workspace role — which gained Sid
  `PolicyDecisionsLog` for `logs:CreateLogStream` + `logs:PutLogEvents`). Fail-soft: a denied
  create is a `policy decisions log WARN` summary row, decisions stay in the ledger and
  `GET /api/policies/audit.runtime_channel` reports `missing`. `scripts/teardown.py` deletes
  the group (`policy-log-group` target); the `policy/` S3 prefix goes with the bucket.
- **Stack Lambdas — CDK.** Each `lambda_.Function` is pointed at an explicit
  `logs.LogGroup` (`RetentionInDays: 90`, deleted with the stack —
  `base_stack.lambda_log_group`). The group is CDK-named on purpose: on a stack deployed
  before this change the service-created `/aws/lambda/<fn>` group already exists and
  CloudFormation would refuse to create it. That old group keeps its lines and stays
  unbounded until deleted by hand (`aws logs delete-log-group --log-group-name
  /aws/lambda/<fn>` once the new group receives traffic). **This is a CDK-owned change**
  (§8.1): hosts need one `host-update.sh --with-cdk`.

### 8.5 Enabling the Policy runtime guard (D20 P2-a)

The Phase-2 guard (`docs/cn/interfaces/policy-backend.md` §"Runtime guard") is off by
default and needs **two** switches: the host's `LAUNCHPAD_POLICY_RUNTIME_GUARD=1`
(`Settings.policy_runtime_guard`, the global kill switch — set it in the backend's env file
and restart the service per the prod runbook) and the workspace's
`PUT /api/policies/settings {"runtime_guard": true}` (console: Governance → Policies →
settings). Optional: `LAUNCHPAD_POLICY_RUNTIME_GUARD_FAIL_MODE=closed` (default `open`).

What then happens, in order:

1. Every agent with Gateway tools shows the `policy_stale` badge; `POST /api/policies/apply`
   (or the console's APPLY) re-deploys them — one update job each: the deployer compiles the
   agent's decision table to `s3://<artifacts>/policy/bundles/<agent>/v<n>.json`, adds the
   `PolicyBundleRead` Sid to the agent's role and pins `LAUNCHPAD_POLICY_BUNDLE` /
   `LAUNCHPAD_POLICY_FAIL_MODE` on the new Runtime version. Agents without Gateway tools,
   BYOC, studio and A2A runtimes are not guarded (coverage block in `GET /api/policies/settings`).
2. **One eval-worker repair.** The P2-a template change (guard placeholders + the bug4 ⑤
   continuation-cap helpers in `strands_agent/main.py.tmpl`) moves the evaluation worker's
   release digest — `templates/eval_worker.release()` hashes that file — so the next
   `make bootstrap` (or the console's preset *repair*) redeploys the evaluation worker and
   the other `zip_runtime` presets (the Skill Lab worker) **once** on each host, flag on or
   off. The architect preset does **not** re-publish (its release hashes its skill bundle, not
   the template); ordinary Harness agents pick the changed template up on their next deploy.
   Expected; no action beyond letting the repair run. Presets are exempt from policy
   (unconditionally — user ruling (12), `role_map[*].exempt`).
3. Turning the workspace switch off (or the host flag) makes the guarded agents stale again;
   the next apply renders them without the hook, env and Sid (byte-identical to before).

Rollback = step 3. The continuation cap is independent: `LAUNCHPAD_CONTINUATION_MAX_TOKENS=<n>`
on the host (`Settings.continuation_max_tokens`, default 0 = off) is injected into every
re-deployed generated agent; a value of 0 removes it on the next deploy.

### 8.6 D21 M1-b — model-provider credentials from SSM (IAM-only template update)

After the `cn/m1-b` merge the console registers model-provider keys from SSM parameters named
`launchpad-<provider>-api-key` (§1 "Model-provider keys"; `docs/cn/interfaces/model-registry.md`
§8–9). The only infrastructure change is **one more resource in the ops role's
`ReadLaunchpadSecretParameters` statement** (`infra/ec2/launchpad-cn-ec2.yaml`). Per environment
(`launchpad-cn-ec2`, `launchpad-cn-ec2-t1`), in this order:

1. **Parity capture** — `curl -sk https://<console>/api/capabilities > /tmp/capabilities-before.json`
   (the registry seed must leave every pre-D21 key byte-identical; diff after step 5).
2. **Code** — `scripts/host-update.sh` (§8.1) on the host: git pull, `uv sync`, frontend rebuild.
   The backend now registers the probe / credential hooks at start; nothing calls SSM until an
   admin clicks *Register credential* or bootstrap runs.
3. **Template (IAM only)** — re-run the §3 deploy command against the same stack with the
   **same** parameter values **and `AmiId=<running AMI>`** (§3 "AMI pin" — mandatory, otherwise
   the dynamic `ImageId` lookup puts an `Instance` row into this change set), first with
   `--no-execute-changeset`. The change set must contain **exactly one row**:
   `OpsRole`/the `AWS::IAM::Role` logical id, `Action: Modify`, `Replacement: False` (policy
   change) — **no `Instance` row, no `ElasticIp*` rows, no parameter change**. An `Instance`
   row is a STOP (§8.2 preview gate; delete the change set). Then execute; the grant is
   effective within seconds, no restart needed for IAM.
4. **Bootstrap** — `make bootstrap` (as root via the SSM session, §5). New stage line
   `bootstrap stage model-providers`: with `launchpad-siliconflow-api-key` present →
   `api-key credential provider launchpad-siliconflow created` (second run: `reused · key
   rotated from SSM parameter launchpad-siliconflow-api-key (version n)`); without it →
   `model-credential launchpad-siliconflow not configured — SSM parameter … does not exist …`
   and the stage still succeeds. The first start also logs
   `model-registry: seeded 2 provider(s) · 6 chat · 1 embedding (catalog v1)`. **No key value
   appears in the output** — if one does, that is a bug (file it as `D21-obsN`).
5. **Restart + verify** — restart the backend (prod runbook); diff
   `/api/capabilities` against step 1: only additive keys (`catalog_version`, `embedding`,
   `models[].{kind,source,enabled}`, `model_providers.*.{id,enabled,probe_state}`, and
   `model_providers.siliconflow.configured: true` once step 6 ran). Admin → Settings → Model
   Providers → **Probe** on *Global Bedrock* (bearer reachability: `ok`, `models_seen` = the two
   Bedrock rows) and on *SiliconFlow* (`failed · credential_unavailable` until step 6 — expected).
6. **SiliconFlow key (when the user has created the parameter, ruling ①)** — *Register
   credential* on the SiliconFlow row → `configured · key version n`; **Probe** → `ok` +
   `models_seen`; add / re-probe a model → `tools` / `temperature` findings (Kimi-style
   `temperature ✗` is a finding, not an error). The console host needs **no**
   `LAUNCHPAD_SILICONFLOW_API_KEY` in `/etc/launchpad/env` any more; a rotation is: update the
   parameter value → *Register credential* again (`rotated: true`, version increments; the hub's
   cached copy is invalidated at once, runtimes pick it up on their next `GetResourceApiKey`).
7. **Evidence** — the change-set table of step 3, the bootstrap stage lines of step 4 (value-free),
   the capabilities diff of step 5, the credential / probe results of step 6 →
   `docs/cn/evidence/round-6/`.

**Rollback** = previous sha + restart (the tables are additive and ignored by the old code);
the IAM widening is harmless to the old code and may stay. Nothing to delete: the Identity
providers are `launchpad-*` and already in the teardown's sweep.

### 8.9 N17-obs203 — provider keys typed in the console (IAM-only template update, r17-mp)

Since cn/r17-z a member or administrator may type a model provider's API key on Settings →
Model Providers; the **console host** writes it to the row's SSM SecureString itself
(`launchpad-<name>-api-key`, or `launchpad-user-<uid>-<name>-api-key` for a personal
provider) and deletes it with the provider (`model-registry.md` §8.1). The ops role therefore
needs the write twin of `ReadLaunchpadSecretParameters` — exactly this statement, added to
`infra/ec2/launchpad-cn-ec2.yaml`:

```yaml
- Sid: SsmWriteLaunchpadSecretParameters
  Effect: Allow
  Action:
    - ssm:PutParameter
    - ssm:DeleteParameter
    - ssm:AddTagsToResource
  Resource:
    - !Sub "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/launchpad-*-api-key"
```

No KMS statement: SecureStrings under the default `alias/aws/ssm` key need none (the read grant
already relies on that). Per environment, in this order:

1. **Template (IAM only)** — the §8.6 step 3 procedure: same parameters, `AmiId=<running AMI>`,
   `--no-execute-changeset` first; the change set must be exactly one `AWS::IAM::Role`
   `Modify` / `Replacement: False` row — an `Instance` row is a STOP. Execute; effective within
   seconds, no restart.
2. **Code** — `scripts/host-update.sh` (§8.1) + restart (the ledger gains
   `model_providers.scope / created_by_name / key_last4 / key_entered_at`, additive; the legacy
   `created_by` usernames are re-pointed once — `users-and-ownership.md` §10).
3. **Verify** — as an administrator add an OpenAI-compatible provider WITH a key: the card
   reads `configured · ****abcd`; `aws ssm get-parameter --name launchpad-<name>-api-key` shows
   a SecureString tagged `launchpad:managed=true` (do NOT print the value); the journal
   (`?view=changes`) shows `provider.create`, `provider.credential`, `provider.key` with the
   last four characters only. A `502 model_provider.ssm_write_failed` naming
   `SsmWriteLaunchpadSecretParameters` means step 1 has not landed. Delete the provider: the
   parameter is gone; its `provider.delete` journal note says `SSM parameter … deleted`.
4. **Evidence** — the change-set row, the redacted `get-parameter` (name, type, version,
   tags), the journal rows → `docs/cn/evidence/round-17/`.

**Operator note (N17-obs228, rev-r20):** changing the `base_url` of a shared provider whose key is
registered — operator-created parameter or console-typed — without re-entering the key deletes
the provider's Identity API-key provider and stops **every deployed agent of that provider at
once** (`GetResourceApiKey` fails until a key is entered again); the change is administrators
only and the drawer warns before the save, so re-enter the key in the same save when the agents
must keep running (`model-registry.md` §8.1).

**Rollback** = previous sha + restart; the statement may stay (the old code never writes).

### 8.7 D21 M1-d — model catalog propagation + embeddings (live steps; plan §11.3)

After merging `cn/m1-d` (needs M1-a + M1-b on the host first):

1. `sudo scripts/host-update.sh --with-cdk` — **the Lambda changed** (`backend/lambdas/
   memory_extractor/handler.py` reads the message's `embedding` descriptor; `infra/stacks/
   memory_stack.py` adds the `launchpad-*` Identity provider family to the two extractor
   Sids — IAM-only, additive, no `*`). Without `--with-cdk` the run ends with
   `cdk: pending`; the old Lambda keeps working on the new messages (it ignores the block)
   but an OpenAI-compatible embedding row would fail at the Lambda until it is deployed.
   The EC2 template is untouched (no stack update of `launchpad-cn-ec2[-env]`).
2. Restart the backend; `_migrate` adds `deployments.catalog_version` /
   `deployments.model_descriptor` (nullable; old rows stay `null` → no hint for them).
3. **Presets: repair once** (eval worker: `main.py.tmpl` gained the descriptor path and
   `jobs.py` the `models` field → the release digest moved; architect: the SKILL snapshot
   re-renders). ≈ 5 min each; `GET /api/system-agents` then shows
   `catalog_version.installed == current` and the Settings page has no
   `preset_snapshot_behind` warning.
4. Verify one memory extraction and one KB ingest land (`dimension` 1024 unchanged for the
   seed; the SQS messages now carry `embedding: {...}`); the extractor log shows the usual
   `outcome=ok` rows.
5. Agent detail: a redeploy of any agent pins `deployed with catalog vN`; editing a
   capability of **that** agent's model under Settings → Model Providers raises the
   *Model changed* tag / alert on the next list read; adding an unrelated model does not.
6. Embedding guard (G5): with memory records present, setting another embedding row as
   default answers **409 `embedding.reindex_required`** naming `launchpad[-env]-memory`
   (and any `-kb-<id>`) with their counts.

Rollback: previous sha + restart (the two columns are additive and ignored by the old
code); the IAM widening is harmless to the old Lambda.

### 8.8 Self-hosted demo-video media (N13-obs11)

User decision 2026-10-17: the video library's recordings are **self-hosted on the console
host — no CDN**. nginx serves `/home/ubuntu/launchpad-media/` as `/media/` on the console
port (`infra/ec2/nginx-site.sh`): static files only, `autoindex off`, explicit MIME types
(`video/mp4`, `video/webm`, `text/vtt`, `image/jpeg` — Ubuntu's `mime.types` has no `text/vtt`
and `<track>` refuses captions served as anything else), byte-range requests (nginx's
static default, `Accept-Ranges: bytes`), `Cache-Control: public, max-age=31536000, immutable`
and **no CORS headers** — the player fetches from the console's own origin. The directory is
**outside the git checkout**: `bootstrap-host.sh` creates it on first boot
(`install -d -m 0755 -o ubuntu -g ubuntu /home/ubuntu/launchpad-media`), and
`scripts/host-update.sh` never reads or writes it (every path it touches is under the
checkout; its ownership check lists `data/`, `config/`, the venvs, `frontend/dist`,
`frontend/node_modules`, `backend/app` and the owner's caches only). Path convention and the
origin rule: `docs/video-library.md` "Media hosting (CN build)".

**Rolling it onto a live host.** The `/media/` block reaches a running host through step 12 of
the next `host-update.sh` (the rendering differs → the diff is printed, `nginx -t`, one
`systemctl reload nginx`); a host bootstrapped before r13-e has no media directory yet —
create it once by hand, exactly as the bootstrap does:

```bash
sudo install -d -m 0755 -o ubuntu -g ubuntu /home/ubuntu/launchpad-media
```

**IAM — no stack update needed.** The host copies files from the evidence bucket with its
instance role; that role's inline policy already grants `s3:*` on `Resource: "*"` (Sid
`CdkAndStackLifecycle`, the CDK-bootstrap grant), which covers `s3:GetObject` /
`s3:ListBucket` on `launchpad-cn-evidence-<account>`. No scoped statement was added — it
would be a no-op inside the wider grant and would force an (AmiId-pinned, §8.2) update of
every environment's stack for nothing. `infra/tests/test_ec2_template.py` pins that the
role keeps a read grant covering the bucket **without a partition literal**; if
`CdkAndStackLifecycle` is ever narrowed, add
`Sid: EvidenceBucketRead` (`s3:GetObject`, `s3:ListBucket`) on
`arn:${AWS::Partition}:s3:::launchpad-cn-evidence-${AWS::AccountId}` and `…/*` at that time.

**Staging one recording** (producer machine → evidence bucket → host). One directory per
revision; never overwrite a published revision — add a new one (the year-long immutable
cache would keep serving the old bytes to every browser that saw them):

```bash
# 1. producer machine (profile cn-northwest-1; the bucket is in the CN account)
REV=agents/20261017-v2-cn-hr                          # <section>/<YYYYMMDD-v2-cn-hr>
aws s3 cp ./first-agent.zh-CN.mp4 "s3://launchpad-cn-evidence-123456789012/videos/$REV/" --profile cn-northwest-1
aws s3 cp ./first-agent.zh-CN.webm "s3://launchpad-cn-evidence-123456789012/videos/$REV/" --profile cn-northwest-1   # optional
aws s3 cp ./first-agent.jpg        "s3://launchpad-cn-evidence-123456789012/videos/$REV/" --profile cn-northwest-1   # poster
aws s3 cp ./first-agent.zh-CN.vtt  "s3://launchpad-cn-evidence-123456789012/videos/$REV/" --profile cn-northwest-1   # captions

# 2. on the host (SSM session), as ubuntu — the pip-installed CLI v1 needs --region;
#    files land 0644 under ubuntu's umask 022, readable by www-data through the 0711 home
sudo -u ubuntu install -d -m 0755 "/home/ubuntu/launchpad-media/$REV"
sudo -u ubuntu aws s3 cp "s3://launchpad-cn-evidence-123456789012/videos/$REV/" \
  "/home/ubuntu/launchpad-media/$REV/" --recursive --region cn-northwest-1
sudo -u ubuntu chmod 0644 /home/ubuntu/launchpad-media/$REV/*

# 3. verify from anywhere the console is reachable: 206 + video/mp4 + Accept-Ranges + immutable
curl -ksI -H 'Range: bytes=0-99' "https://<console-host>:8443/media/$REV/first-agent.zh-CN.mp4"
curl -ksI "https://<console-host>:8443/media/$REV/first-agent.zh-CN.vtt"     # Content-Type: text/vtt
curl -ksI "https://<console-host>:8443/media/$REV/"                          # 403 — no listing
```

Then, as an administrator, enter `https://<console-host>:8443/media/<REV>/first-agent.zh-CN.mp4`
(and the `.webm` / `.jpg` / `.vtt` siblings) on the **Manage** tab of **Learn → Videos** (`/v2/videos?view=manage`).
`<console-host>:8443` must be the host and port viewers use for the console (the NLB
address when they come through §9's NLB): the player requests media with
`crossOrigin="anonymous"`, so a URL on a different host is a cross-origin fetch that
`/media/` does not authorise. The backend's URL validator accepts a public IPv4 host with an
explicit port (verified; `backend/tests/test_videos.py`) and still refuses private /
loopback hosts, `http://`, query strings and non-media extensions. No backend restart and
no code change are involved in publishing a recording.

### 8.9 Unattended-upgrades reboot window (N13-obs13)

Both console hosts rebooted at 02:04Z on 2026-10-18 (10:04 Beijing, inside storyline /
recording hours): Ubuntu's `unattended-upgrades` had installed a kernel update and
`Automatic-Reboot` fired at its default time. The security upgrades stay on; the reboot is
pinned to the maintenance window instead. `bootstrap-host.sh` writes
`/etc/apt/apt.conf.d/51-launchpad-upgrades` (sorted after `50unattended-upgrades`, so it wins)
with `Unattended-Upgrade::Automatic-Reboot "true";`, `Automatic-Reboot-Time "20:00"` (UTC =
04:00 Beijing) and `Automatic-Reboot-WithUsers "false"` (never while an SSM / SSH session is
open) — a plain overwrite of fixed content, idempotent, asserted by
`infra/tests/test_ec2_template.py`. It is a cold-start change (the body is not re-run by
`host-update.sh`), so **deploy-ops applies the same file by hand on every existing host**:

```bash
sudo tee /etc/apt/apt.conf.d/51-launchpad-upgrades >/dev/null <<'APT'
// Launchpad-CN (N13-obs13): reboot for unattended upgrades only in the maintenance window.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "20:00";
Unattended-Upgrade::Automatic-Reboot-WithUsers "false";
APT
sudo chmod 0644 /etc/apt/apt.conf.d/51-launchpad-upgrades
sudo unattended-upgrade --dry-run --debug 2>&1 | grep -i reboot   # shows the effective settings
```

A pending reboot (`/var/run/reboot-required` present) then happens at the next 20:00 UTC with
no session open; the backend unit is `WantedBy=multi-user.target` and comes back on its own
(§8.1 step 14's health probe is the check).

### 8.10 Local build-directory retention (N17-obs151)

The package stage assembles every zip-based agent under the checkout's git-ignored
`data/builds/<agent name>/` (N12-bug10; one directory per agent name, rebuilt on every build)
and nothing ever removed it: t1 carried **15 GB in 66 directories**, most of them for agents
deleted long ago. Two mechanisms now bound it (`backend/app/deployer/build_dirs.py`), both
local-disk only and best-effort:

* **Agent delete** removes the agent's directory as the `build_dir` residue step of the delete
  report (between `registry_record` and `log_groups`; `deleted` with the byte count, `skipped`
  when there is none, `failed` with the reason — never blocking the delete).
* **Startup sweep** (before interrupted jobs are resumed, so no build is writing): directories of
  agents without a live ledger row go (a `deleted` row keeps its name reusable, not its scratch);
  each live agent keeps its newest `builds_keep_per_agent` directories; then the oldest go first
  until the root is under `builds_max_gb` GiB. One `launchpad.builds` INFO line (WARNING when a
  delete was refused) summarises counts and bytes — never a path:
  `build dirs: 66 scanned, 9 kept (1234567 bytes), 57 deleted (15000000000 bytes freed: 55 orphan, 0 over the per-agent keep, 2 over the total cap)`.

Operator keys of `config/launchpad.yaml` (or `LAUNCHPAD_BUILDS_*` in the environment; both
survive `make bootstrap`, which only rewrites the keys it owns):

| key | default | meaning |
|---|---|---|
| `builds_keep_per_agent` | `2` | newest directories kept per live agent (0–50). Today one directory exists per agent, so any value ≥ 1 keeps it; `0` purges the scratch of live agents too (the next deploy recreates it). |
| `builds_max_gb` | `10` | total cap for `data/builds` in GiB after the two rules above; the oldest directories go first. `0` = no cap. |

The sweep runs on every backend start, so applying a new value is `sudo systemctl restart
launchpad-backend` (§8.1 step 12 shape); read the one summary line with
`journalctl -u launchpad-backend --since -5m | grep "build dirs:"`. Dropping the directory of
a live agent is safe: the stage recreates it on the next deploy, and the deployed package is in
S3 (`agents/<name>/deployment_package.zip`), not here. The orphan check is by agent NAME across
all workspaces (the directory carries no workspace id).

## 9. Console NLB (fixed public IP) (N12-obs52)

The distribution orchestrator now includes this stack when `console_entry=public_nlb`
(the example default); it records ownership, publishes its IP as the console origin,
reconciles TLS SANs and waits for target health and the unauthenticated API gate. See
[the distribution guide](../distribution.md). The separate helper below remains available
for existing installations; do not run both creators concurrently for the same stack.

User decision 2026-10-16 (reaffirmed after the monitoring/ICP warning): the console gets a
**fixed public IP** through a Network Load Balancer with a static Elastic IP in front of the
**default host**, TCP passthrough on **8443 only** (no 80/443), and the port is **open to any
source IP** — the console sign-in is the only gate. `infra/ec2/launchpad-console-nlb.yaml` is
one optional stack, idempotent (`aws cloudformation deploy`), separate from `launchpad-cn-ec2`,
deletable without touching the host. The host's own Elastic IP and `AllowedCidr` rule stay
exactly as they are — the NLB is an additional entry. `scripts/ec2-console-nlb.sh` wraps the
commands below (`discover`, `create|update`, `status`, `delete`); each is also written out.

```text
browser (any IP)  → NLB static EIP :8443 (TCP passthrough) → default host nginx :8443 (HTTPS, self-signed)
browser (198.51.100.10/32) → host Elastic IP :8443                                   (unchanged direct path)
```

**What the stack owns:** a new Elastic IP (`StaticIp` — **retained** on stack deletion and
replacement, `DeletionPolicy`/`UpdateReplacePolicy: Retain`, because the user advertises this
address), the internet-facing NLB
mapped onto the host's single public subnet with that EIP, the NLB's security group, one TCP
target group (`InstancePort` 8443, instance target, health check `HTTPS GET /api/health` → 200
every 30 s, 2/2 thresholds, `preserve_client_ip.enabled=false`, deregistration 30 s), one TCP
listener on `ListenerPort` (8443), and **one ingress rule on the host's security group**
(`InstancePort` from the NLB security group). No certificate is involved: the NLB forwards TCP
and the host's self-signed `CN=launchpad.internal` certificate (valid to 2029) terminates TLS as
today — the browser trust step is the same as on the direct path. Deletion protection off,
access logs off, cross-zone off (one subnet). No IAM.

**Access mode (the caveat the user accepted).** `AccessMode=OpenToInternet` (default) opens
`ListenerPort` to `0.0.0.0/0` on the NLB security group — the only world-reachable rule in the
whole deployment. Consequences to keep in view: (1) the console login is the only gate, so keep
a strong administrator password (`launchpad-console-password` in SSM) and the existing
registration approval / `LAUNCHPAD_AUTH_ALLOWED_EMAIL_DOMAINS` pin (setup.md); (2) the account's
security monitoring may flag an open port and open a ticket — the user accepted this; (3) the
`/v1` public API with `X-Api-Key` becomes reachable from anywhere through the same port.
`AccessMode=AllowedCidrs` (`AllowedCidrs=<1-4 comma-separated CIDRs>`, default
`198.51.100.10/32`) keeps an allowlist available — the parameter's `AllowedPattern` refuses a
fifth entry and any `/0` (CloudFormation `Rules` cannot count list members, so the cap lives in
the pattern), so opening the port is always the explicit `OpenToInternet` choice. Switching modes is an in-place update of
security-group rules; the NLB and its EIP are never replaced by it.

**Client IP.** `preserve_client_ip.enabled=false` makes the host see the NLB's private address
instead of the client's, which is what lets the host's security group trust the NLB security
group alone. Trade-off: backend/nginx logs through the NLB show `10.44.0.x`, not the visitor;
the console records usernames, and the direct Elastic-IP path still logs real client IPs.

**ICP note (N11-bug12).** 80/443 (and 8080) on a China-partition public endpoint may require an
ICP filing; 8443 works today, so `ListenerPort` defaults to 8443 and the `Rules` block refuses
80/443/8080. **Idle timeout:** an NLB TCP flow idles out after 350 s; the console's turn streams
send a `heartbeat` event whenever nothing arrived for `HEARTBEAT_S = 1.0` s
(`backend/app/services/live_turns.py`), so SSE streams of any length stay alive.

**Why one stack per host (t1 later).** Every `launchpad-cn-ec2` stack creates its **own**
`10.44.0.0/16` VPC; a load balancer can only target instances in its own VPC and two VPCs with
the same CIDR cannot even be peered. The default host (`vpc-0123456789abcdef0`) and the t1 host
(`vpc-0fedcba9876543210`) therefore need one NLB stack each — t1 is **not** fronted now (user
decision); when wanted, run the same `create` with `--ec2-stack launchpad-cn-ec2-t1 --stack-name
launchpad-cn-console-nlb-t1`.

### 9.1 Create / update (default host)

Parameters come from the host stack (`discover` prints them; `create` resolves them itself):
`VpcId` = its `Vpc`, `PublicSubnetId` = its `PublicSubnet`, `InstanceId` = its `Instance`,
`InstanceSecurityGroupId` = its `ConsoleSecurityGroup`, `InstancePort` = its `ConsolePort`.
Live values for the default host (deploy-ops-2 discovery, 2026-10-16):

```bash
scripts/ec2-console-nlb.sh discover --ec2-stack launchpad-cn-ec2
scripts/ec2-console-nlb.sh create --ec2-stack launchpad-cn-ec2 --stack-name launchpad-cn-console-nlb   # add --no-execute to review the change set first
# the exact command the helper runs for the default host:
aws cloudformation deploy \
  --stack-name launchpad-cn-console-nlb \
  --template-file infra/ec2/launchpad-console-nlb.yaml \
  --no-fail-on-empty-changeset \
  --region cn-northwest-1 --profile cn-northwest-1 \
  --parameter-overrides \
    VpcId=vpc-0123456789abcdef0 \
    PublicSubnetId=subnet-0123456789abcdef0 \
    InstanceId=i-0123456789abcdef0 \
    InstanceSecurityGroupId=sg-0123456789abcdef0 \
    InstancePort=8443 ListenerPort=8443 \
    AccessMode=OpenToInternet
# allowlist variant instead of the open port:
#   ... AccessMode=AllowedCidrs AllowedCidrs=198.51.100.10/32      (helper: --cidr 198.51.100.10/32)
```

No `--capabilities` (no IAM), no host-stack update (the NLB-admitting rule is a separate
`AWS::EC2::SecurityGroupIngress` in this stack), and the AMI-pin rule of §3 does not apply
(no instance here). Re-running the same command is a no-op.

**Escape hatch — if the region rejects a security group on an NLB.** Security groups on
Network Load Balancers have been supported since 2023 and cfn-lint accepts the template, but it
has not been exercised in cn-northwest-1 yet. If `CREATE_FAILED` on `LoadBalancer` names
`SecurityGroups`, re-run with `NlbSecurityGroupMode=disabled` (helper `--no-sg`): the NLB then
has no security group and the host admits `PublicSubnetCidr` (10.44.0.0/24 — the NLB's private
addresses, since client IPs are not preserved) instead of the NLB security group. In that mode
only `OpenToInternet` is possible (the `Rules` block refuses `AllowedCidrs`, nothing could
enforce it), and the host rule is a subnet-wide one: **any future resource placed in
10.44.0.0/24 would also be admitted on 8443**, not just the NLB — say so in the register row if
it happens.

### 9.2 Verify

```bash
scripts/ec2-console-nlb.sh status --stack-name launchpad-cn-console-nlb   # outputs + target health
CONSOLE_URL=$(aws cloudformation describe-stacks --stack-name launchpad-cn-console-nlb \
  --region cn-northwest-1 --profile cn-northwest-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ConsoleUrl'].OutputValue" --output text)
curl -sk "$CONSOLE_URL/api/health"            # 200 - from any network in OpenToInternet mode
```

Expected: target `healthy` within ~1 min of `CREATE_COMPLETE`; `ConsoleUrl` =
`https://<StaticIp>:8443`, the browser shows the host's self-signed certificate (same trust
prompt as on the direct path), sign-in works (`Secure` cookie, HTTPS origin), SSE turns stream.
`unhealthy` with `Target.FailedHealthChecks` means the NLB cannot reach 8443 on the host —
check the rule this stack added on `ConsoleSecurityGroup` and that the host's `TlsMode` is
`selfsigned` (the health check is HTTPS). Record `StaticIp` **and** `EipAllocationId` next to
the stack's other values — the address is retained on deletion (§9.3), so the allocation id is
what releases it later.

### 9.3 Delete (and the teardown order)

```bash
scripts/ec2-console-nlb.sh delete --stack-name launchpad-cn-console-nlb
# equivalent:
aws cloudformation delete-stack --stack-name launchpad-cn-console-nlb --region cn-northwest-1 --profile cn-northwest-1
aws cloudformation wait stack-delete-complete --stack-name launchpad-cn-console-nlb --region cn-northwest-1 --profile cn-northwest-1
```

**Order:** the NLB stack **first**, then (if at all) the host stack — this stack owns the rule on
the host's security group and an ENI in the host's subnet, so deleting the host stack first
fails on `ConsoleSecurityGroup` / `PublicSubnet` still being in use. **The fixed IP survives
stack deletion** (`DeletionPolicy: Retain` — the user advertises it); the helper prints the
release command, and the address keeps costing while unattached. Release it manually only when
truly done:

```bash
aws ec2 release-address --allocation-id <EipAllocationId> --region cn-northwest-1 --profile cn-northwest-1
```

(`EipAllocationId` is a stack output — read it **before** deleting the stack, or find the
address under `aws ec2 describe-addresses` by its `Name` tag `launchpad-cn-console-nlb-console`.)
`scripts/teardown.py` does **not** know this stack or the retained address: §7's teardown of the
default environment must be preceded by this delete and, when final, the release. Deleting the
NLB stack leaves the host exactly as before (its own Elastic IP, `AllowedCidr` rule and
`ConsoleUrl` unchanged).

## 10. CI gate (N12-obs59)

`.github/workflows/cn-verify.yml` runs **exactly `make verify`** (= `scripts/verify.sh`) on
GitHub Actions for every push to `cn/integration`, `cn-region` and `main` and for every pull
request targeting them (plus manual `workflow_dispatch`). CI and the local gate are therefore
the same command on the same lockfiles: a red run reproduces with `make verify` on a
developer host, and the hosts' update path (`scripts/host-update.sh`, §8.1) ends in the same
command.

- **No AWS (C8).** The runner has no credentials, profile or region; the suite is hermetic
  (`backend/tests/conftest.py` redirects SQLite and stubs every client — the workflow sets no
  `LAUNCHPAD_*` or `AWS_*` variable). Only `AWS_EC2_METADATA_DISABLED=true` is exported so
  botocore never waits on an instance-metadata probe. Establishing this surfaced that the gate
  was NOT hermetic before: with an empty `HOME` and every `AWS_*` variable unset, 63 backend
  tests failed — 61 on the Global Bedrock bearer guard (`aws_clients.require_global_bedrock_bearer`,
  satisfied on the hosts by the real key in the environment) and 2 that built a real client
  (`get_dashboard` → `cloudwatch`, `fetch_catalog` → `bedrock-agentcore-control`) and passed on
  the hosts only because real credentials existed. `conftest.py` now `setdefault`s a placeholder
  bearer (a real value is left alone; the guard's own tests `delenv` it) and the two tests stub
  the client they leaked.
- **Hosts fail loudly too (follow-up, `cn/q-ci-2`).** `conftest.py` now strips every credential
  source botocore would resolve from the host before any client import — `AWS_PROFILE` /
  `AWS_DEFAULT_PROFILE`, static keys and session token, the container and web-identity
  provider variables — disables the instance-metadata probe and points
  `AWS_SHARED_CREDENTIALS_FILE` / `AWS_CONFIG_FILE` at paths that do not exist; the Global
  Bedrock bearer is *replaced* by the placeholder (a leaked bearer call can never spend the
  host's real key). A test that reaches AWS therefore raises `NoCredentialsError` at its first
  request on a developer host, an EC2 host and the CI runner alike. Obviously-fake static keys
  were deliberately not substituted: verified that with them botocore resolves credentials
  (`env`) and signs and sends the request to the real endpoint, which is the quiet failure the
  guard exists to remove. The full backend suite under this posture on the 8-core host passed
  with no further leak beyond the two fixed above.
- **What cannot run there:** the real-AWS e2e scripts (`backend/scripts/e2e_*.py`) and the
  release smoke test (`docs/cn/release-smoke-test.md`) — those remain deploy-ops steps against
  the `cn-northwest-1` hosts.
- **Toolchain:** `ubuntu-latest`, Python 3.12 (`actions/setup-python`), uv via
  `astral-sh/setup-uv` with the uv cache keyed on `backend/uv.lock` + `infra/uv.lock` and
  `uv sync --locked` (a stale lockfile fails the run), Node 20 with the npm cache keyed on
  `frontend/package-lock.json` and `npm ci`. `uvx cfn-lint --version` is warmed first so
  verify.sh's cfn-lint section lints instead of reporting SKIPPED.
- **Checkout is `fetch-depth: 0`:** `scripts/n13_restore_check.sh` reads the Skill Lab
  baseline commit `4e27b1f4` with `git ls-tree` / `git show`, and the forbidden-API step is a
  `git grep`; a shallow clone has neither.
- **On failure** the run uploads `verify.log` (the full gate output), the pytest junit files
  (`PYTEST_ADDOPTS=--junitxml=pytest-junit.xml`, written by the backend, infra and
  `samples/byoc` runs next to their cwd) and `frontend/vitest-junit.xml` (the junit reporter
  is added in `frontend/vite.config.ts` only when `GITHUB_ACTIONS` is set). Retention 14 days.
- **Concurrency** is one run per ref (`cn-verify-<ref>`, newer push cancels the older run);
  job timeout 40 min. Expected wall time ≈ 10–15 min on the 4-core runner (the backend suite
  alone is ~1 min on 8 cores locally; `uv sync`, `npm ci` and `vite build` make up the rest).
- **Runs:** `https://github.com/<your-fork>/launchpad-cn/actions/workflows/cn-verify.yml`
  (per run: `https://github.com/<your-fork>/launchpad-cn/actions/runs/<run_id>`). The README
  badge points at the `cn/integration` branch.
## 11. Security headers & TLS (N12-obs60)

The security scan of 2026-10-17 (`docs/cn/reports/security-scan-2026-10-17.md`, rows T-1..T-5)
found the console answering with no browser security headers, HSTS only on `/api` and `/v1`
(the backend's middleware), the nginx version banner, the distro's default TLS 1.2 cipher list
(static-RSA and CBC suites still offered) and a self-signed certificate without a SAN. All of it is
fixed in the two files every host renders from, so nothing here is hand-edited on a host.

### 11.1 What nginx now sends (`infra/ec2/nginx-site.sh`)

Every response of every location (`/api/`, `/v1/`, `/assets/`, the SPA) carries, on every status
code (`always`, so a 401 or 404 is covered too):

| header | value | why |
|---|---|---|
| `Content-Security-Policy` | enforced, explicit allowlist — table below | XSS / injection containment |
| `X-Frame-Options` | `DENY` | legacy twin of `frame-ancestors 'none'` |
| `X-Content-Type-Options` | `nosniff` | no MIME sniffing of chunks or member uploads |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | CloudWatch / docs deep links learn the origin, never the path |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=()` | the console uses none; clipboard is deliberately **not** restricted (copy buttons) |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (TLS site only, no `preload`) | pins HTTPS from the first navigation; the backend's own `hsts` middleware stays for the dev proxy and nginx hides that upstream copy so a response carries exactly one |

Also: `server_tokens off` (no `nginx/x.y.z` banner), and on the TLS site `ssl_protocols TLSv1.2
TLSv1.3` with `ssl_ciphers` = the six ECDHE + AEAD suites (`ECDHE-{ECDSA,RSA}-AES{256,128}-GCM`,
`ECDHE-{ECDSA,RSA}-CHACHA20-POLY1305`), `ssl_prefer_server_ciphers on` — forward secrecy only, no
CBC, no static RSA, no ARIA / Camellia. TLS 1.3 suites are nginx's fixed AEAD set.

**CSP, directive by directive** — each source is a need read from the bundle, not a guess:

| directive | sources | needed by |
|---|---|---|
| `default-src` | `'self'` | fallback for everything not listed |
| `base-uri` / `form-action` | `'self'` | no `<base>` hijack; forms submit to the console only |
| `object-src` / `frame-src` / `frame-ancestors` | `'none'` | no plugins; the console frames nothing and may not be framed |
| `script-src` | `'self' 'wasm-unsafe-eval' https://cdn.jsdelivr.net` | Vite's hashed chunks; the DCV live view (Governance → Tools → browser) decodes video with WebAssembly — there is no `eval` / `new Function` anywhere in that SDK; `@monaco-editor/react` loads Monaco from jsdelivr (`loader.js`, AMD modules, blob workers that `importScripts` the CDN) |
| `style-src` | `'self' 'unsafe-inline' https://cdn.jsdelivr.net` | Monaco, xyflow, Cloudscape and the DCV SDK inject `<style>` elements and `style=""` attributes — the one concession; Monaco's CSS is on the CDN |
| `font-src` | `'self' data: https://cdn.jsdelivr.net` | bundled `@fontsource` files; Monaco's codicon font |
| `img-src` | `'self' data: blob: https:` | inline SVG data URIs, screenshots / canvases, video posters on the library's external CDN |
| `media-src` | `'self' blob: https:` | the video library plays `<video>` / `<track>` from an external CDN |
| `connect-src` | `'self' https: wss:` | same-origin `fetch` + SSE (chat, assistant, Studio debug); the DCV live view opens a presigned `wss://` stream on the AgentCore endpoint. Scheme-wide because the partition host may not be written into `infra/ec2` (`scripts/cn_lint.sh`); tighten to the partition suffix once the live-view endpoint becomes a render parameter |
| `worker-src` | `'self' blob:` | DCV decoder workers (same origin under `/nice-dcv-web-client-sdk/`), Monaco's blob workers |
| `manifest-src` | `'self'` | — |

Hermetic tests: `infra/tests/test_nginx_site.py` asserts the block in every location, the CSP
directives above, HSTS on the TLS site only (and hidden from the upstream), `server_tokens off`,
the cipher list, and — where a local `nginx` exists, as on the gate hosts — that the real binary
accepts both renderings (`nginx -t`).

Validated once against the real bundle (2026-10-17, not part of the gate): the built SPA
behind the rendered site in a local nginx with a local backend, driven by Playwright over 28
console routes plus the classic Studio with Monaco loaded from jsdelivr and the xyflow canvas —
zero CSP violations and zero page errors. The DCV live view and CDN video playback need AWS /
library data; their sources were read, not exercised.

### 11.2 Applying it to a running host

The site reaches a host through `scripts/host-update.sh` step 12 (§8.1): the next update renders
the new site, prints the diff, runs `nginx -t` and reloads nginx; nothing else to do. Verify:

```bash
curl -sk -D - -o /dev/null https://<console-ip>:8443/ | grep -iE 'content-security|x-frame|nosniff|referrer|permissions|strict-transport|^server:'
curl -sk -D - -o /dev/null https://<console-ip>:8443/api/capabilities | grep -ic strict-transport   # 1, not 2
openssl s_client -connect <console-ip>:8443 -tls1_2 -cipher 'AES256-SHA' </dev/null 2>&1 | grep -c 'handshake failure'   # 1: static-RSA CBC refused
```

Rollback is the step-12 mechanism itself: `/etc/nginx/sites-available/launchpad.prev` holds the
previous site; `install -m 0644 launchpad.prev launchpad && nginx -t && systemctl reload nginx`.

### 11.3 The self-signed certificate — `infra/ec2/tls-cert.sh`

`bootstrap-host.sh` no longer runs an inline `openssl req`; it calls
`infra/ec2/tls-cert.sh --out-dir /etc/nginx/launchpad-tls --san IP:<this host's IPv4>` so the
certificate carries `subjectAltName = DNS:launchpad.internal, IP:<address>` (modern clients ignore
the CN). The Elastic IP is associated by a separate CloudFormation resource and the console NLB's
address (§9) is never known at first boot, so the boot-time SAN may be the pre-EIP address.
**Hosts that already run** keep their SAN-less cert until an operator regenerates it — the cert is
read from disk by nginx only, so this is a reload, not a redeploy:

```bash
# on the host, inside the SSM session
cd /home/ubuntu/launchpad
sudo infra/ec2/tls-cert.sh --force --san IP:203.0.113.10,IP:203.0.113.30     # EIP, NLB IP — deploy-time values
sudo nginx -t && sudo systemctl reload nginx
openssl s_client -connect 127.0.0.1:8443 </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName
```

`--san` takes `DNS:name` / `IP:addr` entries (bare host names and addresses are prefixed
automatically; comma-separated lists and repeats are fine), `--print` shows the extension without
writing anything, and without `--force` the script refuses to replace an existing key pair (exit 3).
Browsers that imported the previous certificate must import the new one. Nothing is committed:
the key pair exists only on the host (`infra/tests/test_tls_cert.py` also asserts no `.pem` is
tracked). An ICP-registered domain with an imported or ACM certificate remains the real fix
(N11-bug12 keeps 80/443 closed).

### 11.4 Troubleshooting

- **A console page is blank, a feature stopped working, and DevTools shows `Refused to load … because it violates the following Content Security Policy directive`** — a resource the enforced CSP does not list. Read the directive and the blocked URL from the message, add the source to the directive table in `infra/ec2/nginx-site.sh` (one place; the test in `infra/tests/test_nginx_site.py` pins the lists, so update both), run `host-update.sh`. The usual suspects are a new third-party CDN (a `script-src` / `style-src` host), a new iframe (`frame-src` is `'none'` on purpose) or a new WebSocket endpoint on a non-`wss:` scheme. The browser live view and Monaco were verified against the directives above; both also need outbound internet from the client to jsdelivr (Monaco) and the AgentCore endpoint (DCV).
- **After the host update the console is unreachable and `nginx -t` complains** — step 12 restores `launchpad.prev` itself and warns (§8.1); if nginx was reloaded onto a bad site anyway, restore it by hand as in §11.2.
- **Two `Strict-Transport-Security` headers on `/api/*`** — the upstream copy is no longer hidden: the proxy location lost its `proxy_hide_header`; re-render.
