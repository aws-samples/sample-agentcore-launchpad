# Cross-Account Workspaces

A workspace is one `(account, region)` environment the console manages. By
default it lives in the hub's own account, reached with the backend's ambient
credentials. This page covers the other case: a workspace in **another AWS
account**, which the hub reaches by assuming a role there.

Written for the AWS administrator who owns the target ("spoke") account. The
console-side model is in
[architecture.md](architecture.md#workspaces--multi-accountmulti-region-environments).

## How it works

```
hub backend (EC2 instance role / ECS task role)
  └─ sts:AssumeRole  ──ExternalId──▶  arn:aws:iam::<spoke>:role/LaunchpadWorkspaceRole
        └─ every AWS call for that workspace: bootstrap, deploy, invoke, observe
```

The hub holds no long-lived credentials for the spoke. It assumes the role on
first use, with a one-hour session that refreshes itself, and every client it
builds for that workspace is keyed on `(account, region, role)` — so a spoke's
work can never accidentally sign with the hub's own credentials.

## Setup

Three steps, in this order. Registering first is deliberate: the console tells
you the two values the stack needs.

### 1. Register the workspace in the console

**Workspaces → NEW WORKSPACE**, turn on *external account*, and fill in:

| Field | Value |
| --- | --- |
| AWS account | the spoke's 12-digit account id |
| Region | the region to provision in that account |
| Role ARN | `arn:aws:iam::<spoke>:role/LaunchpadWorkspaceRole` (the default name; the account in the ARN must match the account field) |
| External ID | any 2–128 characters of `A-Za-z0-9+=,.@:/_-`. Use the *suggest* button, or your own convention. **Keep it** — step 2 needs the same value. |

The form also shows the **hub role ARN** (read live from
`sts:GetCallerIdentity` and normalized to the `iam:` role form). That is the
only principal the spoke will trust. Copy it.

Registration records the environment and validates the shapes; it makes no AWS
call into the spoke. Nothing is provisioned yet.

### 2. Deploy the role in the spoke account

Template: [`infra/spoke/launchpad-workspace-role.yaml`](../infra/spoke/launchpad-workspace-role.yaml).
Plain CloudFormation, one IAM role, no CDK and no bootstrapped environment
needed.

```bash
aws cloudformation deploy \
  --template-file infra/spoke/launchpad-workspace-role.yaml \
  --stack-name launchpad-workspace-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      HubRoleArn=arn:aws:iam::<hub>:role/<hub-role> \
      ExternalId=<the same ExternalId you registered> \
  --profile <spoke-admin> --region <any region>
```

IAM is global, so the region of this deployment does not matter — the role
covers every region you register for that account.

The stack outputs `RoleArn`; it must equal what you registered in step 1.

### 3. Run the bootstrap

Back in the console: **Workspaces → the new workspace → RUN BOOTSTRAP**. The
first stage, `validate-access`, assumes the role, checks that the identity it
gets really lives in the declared account, and probes each service the later
stages need — so a permission or trust problem surfaces there rather than
halfway through provisioning. The remaining stages are idempotent and the run
is resumable; a failed run can be resumed after fixing the stack.

When the job reports READY the workspace behaves like any other: create,
deploy, invoke, observe, evaluate. CodeBuild builds run in the spoke account,
per-agent execution roles are created there, and observability queries read the
spoke's CloudWatch in the spoke's region.

## Rolling out with StackSets

For many accounts, deploy the same template as a **CloudFormation StackSet**
targeting an OU, with `HubRoleArn` fixed and `ExternalId` either shared or
per-account (as a parameter override). Service-managed permissions and a
delegated administrator are the usual setup; nothing in the template depends on
StackSets specifically.

> Documented, not exercised: this repo's development Organization contains a
> single account, so the StackSets path has never been run end to end here. The
> single-account `aws cloudformation deploy` above has. STS does not check
> Organization membership — the trust policy names one role ARN and one
> ExternalId — so a spoke does not have to be in the same Organization for the
> mechanism to work; Organizations only make the *rollout* manageable.

## The trust boundary, stated plainly

`LaunchpadWorkspaceRole` is powerful. It creates IAM roles, because Launchpad
provisions a per-agent execution role for every agent it deploys, and it can
pass those roles to AgentCore, CodeBuild and Bedrock. A role that can create and
pass roles can, in principle, escalate within the spoke account.

So the boundary is **not** the permission set. It is:

- **the trust policy** — exactly one principal, the hub's role ARN, and nothing
  else;
- **the ExternalId** — a second factor the hub must present, so a confused
  deputy cannot use the trust on someone else's behalf;
- **name scoping** — every role, bucket, ECR repository, CodeBuild project and
  inline policy the hub touches is `launchpad-*`, and the statements that can be
  ARN-scoped are (IAM, PassRole, S3, ECR, CodeBuild);
- **CloudTrail in the spoke** — every call arrives as
  `launchpad-<account>-<region>`, the role session name the hub uses.

What genuinely cannot be scoped is written into the template statement by
statement, with the reason: the AgentCore and Agent Registry preview APIs (whose
`List*`/`Create*` calls are account-scoped), Cognito (a user pool has no ARN
before it exists), CloudWatch Logs Insights and metrics, and the account-level
X-Ray span destination.

Two statements — `CreateServiceLinkedRoles` and `KmsForAgentCore` — are not
derivable from the hub's code; they come from a least-privilege policy validated
live against the same console. If AgentCore adds a service-linked role, that is
the statement to extend.

### Hub side

If the hub's own role is `AdministratorAccess` (the default for this sample's
deployments), **nothing to do** — it may already assume the spoke role. A
least-privilege hub needs one statement:

```json
{
  "Effect": "Allow",
  "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::<spoke>:role/LaunchpadWorkspaceRole"
}
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `workspace.assume_role_failed` (502), or `validate-access` fails naming trust/ExternalId | AccessDenied on AssumeRole. STS reports all three causes identically: the trust policy does not name this hub's role, the ExternalId does not match the stack parameter, or **a role was deleted and recreated** — the trust policy stores the hub role's *unique id*, so a same-named replacement is a different principal. Update the stack after any hub-role replacement. |
| `workspace.role_account_mismatch` (400) at registration | The account inside the role ARN is not the account you typed. One of the two is a typo; nothing was recorded. |
| `validate-access` fails with the account it actually reached | The role exists but lives in a different account than the workspace declares. Re-register with the right pair. |
| `validate-access` refuses the region | That region already hosts another Launchpad deployment (its gateway/memory/registry/user pool/CodeBuild project are not in this workspace's resource map). Pick another region or detach the other install. |
| A later stage fails with a named action | A permission gap in the spoke stack. Add the action, update the stack, resume the bootstrap. |
| A long job dies with an AssumeRole error | The session's refresh failed with under ten minutes of validity left, which propagates rather than being swallowed. Fix the cause and resume; the sessions themselves recover automatically. |
| `workspace.cross_account_tool_unavailable` (409) | The **browser** tool demo. Its SDK client cannot be given a session, so it can only run under the hub's own credentials. The code-interpreter demo has no such limit. |

Known, deliberate limits:

- Sessions are capped at one hour because the hub is itself an assumed role (role
  chaining), which is why they refresh instead of being issued longer.
- Model prices are refreshed from the hub's region only, so a model first seen in
  a spoke region is priced on its next appearance in the hub.
- A few LLM calls the hub makes *for itself* — the simulated-actor persona in an
  evaluation scenario, code generation, AI Fix — run on the hub's own credentials
  in the hub's region, not in the spoke. That is why the spoke role carries no
  `bedrock:InvokeModel`: the agent under test invokes models under its own
  execution role in the spoke, while the harness driving it stays hub-side.
