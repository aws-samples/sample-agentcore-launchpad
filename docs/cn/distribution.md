# Deploying the China distribution

This is the entry point for deploying the CN fork into another account. Use the same
release tag for the operator checkout and the host checkout. Do not use the upstream
Global repository or copy an existing installation's database, YAML or credentials.

The example configuration creates a named environment with a dedicated OpenSearch domain
and a public NLB. The orchestrator waits for the host bootstrap, uploads
the policy binary to the new environment's private artifact bucket, installs both engine
shards and checks OpenSearch, the backend, control sockets and the NLB target/auth. A console login page alone is not
full deployment acceptance.

## Prerequisites

* An AWS China account with credentials for infrastructure provisioning in `cn-northwest-1`.
* An operator machine with Git, AWS CLI, uv, Rust and the ARM64 cross compiler
  (`gcc-aarch64-linux-gnu`). Python and Node dependencies on the EC2 are installed by
  the template. Rust compilation occurs on the operator machine, not the CN host.
* Model access: the default workshop uses a Global Bedrock API key; the platform's
  KB and memory bootstrap also use model/embedding credentials. Store the key and the
  chosen console admin password as **flat-named SSM SecureString parameters in the
  destination China region**. The key must support the configured chat and embedding
  models. Never reuse the sample environment's credentials.
* A private S3 release bucket in the destination account/region. Enable Block Public
  Access; do not grant anonymous access. The operator uploads the CloudFormation
  template here. The engine artifact is uploaded to the environment-owned bucket
  after bootstrap, so the host needs no permission on the release bucket.
* Network access from the new host to GitHub, package mirrors and the selected model
  endpoint. Configure `AllowedCidr` for the operator's actual IPv4 egress.
* Access to the CN Git repository. A private fork requires an SSH clone URL and a
  read-only deploy key in SSM (`GitDeployKeyParameter`). HTTPS cloning is anonymous:
  access in the operator's browser does not grant the new EC2 access.

Build the engine and install operator dependencies:

```bash
git checkout cn-v2026.10.28.2
cd backend && uv sync && cd ..
make policy-engine-artifact
```

The artifact and adjacent SHA256 file are written under `policy-engine/dist/`.
Do not use an unrelated precompiled binary with this release.

## Plan, deploy and resume

Copy `config/deploy-cn.example.json` outside the source tree. Set all placeholders,
including the destination account, bucket, CIDR, SSM parameter names and an **absolute**
`engine_artifact` path. Use a new, nonempty environment suffix and stack name.
Config files contain parameter names, never secret values.

The example explicitly selects `console_entry: "public_nlb"` and
`parameters.MemoryDomainMode: "dedicated"`:

* `public_nlb` creates `launchpad-cn-console-nlb-<env>` using the existing
  `infra/ec2/launchpad-console-nlb.yaml` template. Only the console listener (8443 by
  default) is open to all IPv4 sources. The host admits that listener from the NLB
  security group; `AllowedCidr` still limits the separate direct EC2 entry.
* `direct` skips the NLB and publishes the restricted EC2 address.
* `dedicated` creates `launchpad-memory-domain-<env>`, its own domain, admin role and
  SSM parameters. Bootstrap, the extractor Lambda, KB and Memory use that domain.
* `shared` explicitly reuses the account/region domain. Configs predating these options
  retain their legacy behavior (direct entry, shared domain); resume fingerprints do
  not change. Do not edit an active run's config to convert it into a new deployment.

Registration rules are also deployment configuration. The workshop example selects
`AuthPasswordPolicy: "relaxed"` and `AuthPasswordMinLength: 8`, matching the workshop's
chosen policy. These become `LAUNCHPAD_AUTH_PASSWORD_POLICY` and
`LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH` on the host. The template defaults remain strict/12
for existing callers that omit the parameters. They affect registration/member password
changes; the configured administrator's secret still comes from its SSM parameter.
Verify the effective rules through `/api/auth/status` after deployment.

```bash
cd backend
uv run python ../scripts/deploy_cn.py plan \
  --config /absolute/path/deploy-cn.json --state /absolute/path/run/deployment.json
uv run python ../scripts/deploy_cn.py apply \
  --config /absolute/path/deploy-cn.json --state /absolute/path/run/deployment.json
```

`plan` reads AWS metadata but does not create resources. It checks account identity,
the release ref, SSM parameter existence, private-bucket ownership, NLB name conflicts
and the engine checksum. It prints the selected entry and OpenSearch modes.
It also reports a **regional network quota preflight** for VPCs, internet gateways
and Amazon-provided Elastic IPs: destination account/region, current usage, effective
quota and additional allocations needed. A fresh public-NLB deployment normally needs
one VPC, one internet gateway and two EIPs; `AllocateElasticIp: "false"` removes the
host's EIP requirement. BYOIP/customer-owned addresses do not consume the checked
Amazon EIP quota.

`apply` re-reads these values before writing resources; a prior successful plan is
not cached approval of capacity. A pending increase request is not an effective
quota. Insufficient capacity stops with the service/quota code and required total;
unknown quota or inventory data stops allocations with the failed API/error code.
The operator needs `servicequotas:GetServiceQuota`, `ec2:DescribeVpcs`,
`ec2:DescribeInternetGateways`, `ec2:DescribeAddresses` and, for partial owned stacks,
`cloudformation:ListStackResources`. Provision the required read permissions rather
than assuming a default quota when the APIs are unavailable.

Partial/failed stacks receive credit only for their recorded physical resources
still found in the regional inventory, so their existing IGW/EIP is not counted
again as new demand. Completed owned host/NLB stacks need no new network allocations;
their acceptance-only resume reports `not_required` without requiring quota access.
This check does not cover EC2 vCPU or instance availability, OpenSearch, AgentCore,
or every other AWS quota. Capacity can change after this snapshot; CloudFormation
and final health checks remain authoritative. The orchestrator never deletes
existing resources to make space.

`apply` creates real billable AWS resources. Expect substantially longer than ten
minutes on a cold account: CDK bootstrap, OpenSearch provisioning and package downloads
can dominate. Progress is recorded in the state file and CloudFormation/SSM.

Run the same `apply` again after an interrupted terminal. It resumes only the stack
recorded in that state file; it refuses an unrelated existing stack, changed config
or moved release ref. A failed stack is kept for diagnosis, never silently deleted.
Correct a failed CloudFormation bootstrap using its events before retrying. `apply`
does not repair a `CREATE_FAILED` stack by guessing which infrastructure to replace.

A repaired owned host stack may replace its EC2 instance. Resume binds each installation
and verification command to its original instance and script; it waits for the previous
attempt to finish, archives its result under the state's `commands/` directory, and starts
the replacement attempt on the new instance. Legacy CommandId-only state is recovered
from SSM command metadata; unavailable or ambiguous metadata stops with a diagnostic
instead of guessing. Keep these evidence files alongside the state file.

For an owned NLB, resume can update the target instance and host security group within
the same VPC/subnet, preserving the existing template and public address. Changes to
VPC/subnet, listener port or access mode require explicit reconciliation. Host changes to
environment, domain mode, release, console/auth configuration or policy-network settings
also stop resume; a hardware/AMI repair does not authorize a different deployment.

```bash
uv run python ../scripts/deploy_cn.py status \
  --config /absolute/path/deploy-cn.json --state /absolute/path/run/deployment.json
```

The new console username is `admin`; its password is the value **you** placed in
`ConsolePasswordParameter`. The public URL and instance ID are in `outputs`. In NLB mode, `outputs.ConsoleUrl`
is the NLB static IP, `outputs.DirectConsoleUrl` is the restricted EC2 origin, and
`nlb_outputs` records the allocation ID, DNS name and target group. Both IPv4 addresses
are included in the host certificate. The published origin is also written to backend
configuration for OAuth callbacks. Self-signed TLS requires trusting the certificate.

### Isolation boundary

Each named environment has its own EC2, VPC, console ledger, Gateway, runtime resources,
policy shards and, with the example's `dedicated` mode, OpenSearch domain. Index prefixes
remain environment-specific in either mode. The domain stack is
`launchpad-memory-domain-<env>`, the domain is `lp-memory-cn-northwest-1-<env>`, and
discovery parameters live under `/launchpad/memory-domain/cn-northwest-1/<env>/`.
No read of the legacy domain is needed to create a dedicated environment.

AWS account/region services (for example CDK bootstrap and AgentCore Identity), the
operator-supplied model credential and external model inference are still prerequisites;
an independent domain is not a separate AWS account. Release `.1` was tested with a
shared domain. Do not describe that test as proof of a new domain or blank-account install.

The template's scoped host role is the default; this workflow does not enable
`HubAdminAccess`. Gateway policies remain unattached until configured in the console
or by the explicitly invoked workshop preparation.

## Verify and prepare the workshop

1. Confirm `platform_ready` and `policy-install.json` success, and compare the checked-out
   commit to the resolved release commit.
2. Log in. Confirm the destination region, available model, knowledge-base and memory
   capabilities. Create an Agent and verify an actual model answer.
3. Verify a single mixed `||` tool policy can validate, publish and simulate. Include both
   Agent and user permit baselines; test an allowed admin and a denied member/tool.
4. Prepare the [IT service desk lab](../../samples/itsm-cn/README.md) using its own config.
   Complete the generated eight-step manual and retain this environment's evidence.

Neither copied screenshots nor successful unit tests substitute for steps 2–4.

### Demo contents and provisioning boundary

| Package | Contents | Provisioning |
| --- | --- | --- |
| Platform samples | HR database Lambda, office-facts REST API and default Gateway targets | Platform CDK/bootstrap |
| IT service desk tools | Synthetic ticket database and ten ticket/knowledge-article tools, Lambda bridge, dedicated workshop Gateway and network rules | `samples/itsm-cn/scripts/workshop.py all` after platform deployment |
| IT knowledge and skills | Five Chinese documents, five skills and one workshop KB | Workshop console stage; indexed on the environment's selected OpenSearch domain |
| Accounts | Ten learners (`lab01`–`lab10`) and service desk user (`itdesk01`); generated passwords in destination SSM | Workshop console stage |
| Reference Agents | Service desk Agent; optional `lab01` learner reference | Workshop staff stage; learner reference is a separate command |
| Authorization demo | Agent/user baselines, requester identity rule, staff-only tools, simulator and audit exercises | Workshop policy stage |
| Evaluation and manual | Eight teaching questions, a separate held-out dataset, offline eight-step Chinese handout | Workshop console/manual stages; full exercise acceptance remains an operator task |
| Additional examples | HR approval Lambda source, BYOC HTTP/container examples, expense-report skill and sample KB documents/datasets | Repository assets; not all automatically registered or deployed |

The workshop service simulates ServiceNow workflows with synthetic data; it is not a
connection to a real company's ServiceNow, CRM or document system. Existing installations'
additional tools, third-party credentials, conversations and uploaded documents are not
copied by deployment. Record the actual initialized resources in the workshop manifest
and distinguish live evidence from labelled reference screenshots.

## Updating and cleanup

The explicit isolated-demo HTTP mode (`console_http_lab: true`, primary port
8088) and an existing-host migration/rollback procedure are documented in
[HTTP lab operations](http-lab.md). In that mode, distribute only the primary
`outputs.ConsoleUrl` to learners and use it for both workshop URLs. Default
HTTPS deployments and historical run manifests stay unchanged.

This entry point is for **new installations**. Existing environments use
`scripts/host-update.sh` and the production runbook; the policy binary is independently
installed with `scripts/install_policy_host.sh` after building and verifying the new artifact.
Back up the existing ledger first. Do not point a new-install state file at the live console.

There is no automatic teardown after failure. Retain the deployment state and workshop
manifest; use the existing teardown documentation to remove only the named environment's
resources. Never delete `launchpad-memory-domain` while another environment uses it.
KMS keys may be retained by the template and require separate lifecycle management.

Delete the NLB stack **before** its host stack; its retained EIP requires a separate
release when no longer needed (see deployment interface section 9).
Run teardown with the same `LAUNCHPAD_ENV_NAME` and `LAUNCHPAD_MEMORY_DOMAIN_MODE`.
Dedicated mode selects only its dedicated domain for deletion; shared mode retains the
existing last-environment guard and `--delete-shared-domain` opt-in.

### Converting an existing environment

Changing `MemoryDomainMode` is not a data migration. For an existing environment:
back up the ledger/config and its index data; create the dedicated domain first; quiesce
Agent calls and the extractor queue; copy only that environment's indices and verify
document IDs/content; update its memory CDK stack, backend configuration and workspace
resource maps; then re-publish Agents that embed KB/Memory endpoints so their runtime
environment and IAM policy target the new domain. Verify both real retrieval and memory
read/write before resuming. Keep the source indices until acceptance and rollback review.
Never run `bootstrap-host.sh` again on a populated host to perform this migration.
