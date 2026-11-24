# Teardown / 资源清理

Cleaning up has two distinct scopes: the **demo resources** you create while
using the platform (agents, datasets, experiments), and the **shared infra**
bootstrap provisioned once. Retire demo resources first, then the shared infra.

中文版: [teardown.zh-CN.md](teardown.zh-CN.md)

> **Safety rule.** Never delete resources you did not create. Teardown only
> touches resources named `launchpad*` / `launchpad_*`; it will not remove
> anything else in your account. Review the dry-run output before you confirm.

## 1. Demo resources (per-agent, reversible from the platform)

These are created through the console or API and removed the same way — no
shared infra is affected.

| Resource | How to remove |
|---|---|
| **Agents** | Console agent list, or `DELETE /api/agents/{id}`. This tears down the agent's runtime/harness resources and soft-deletes the ledger row; the auto-created A2A registry record is disabled separately (see below). |
| **Eval datasets** | Console Evaluation page, or `DELETE /api/eval/datasets/{id}`. |
| **Eval runs / experiments** | Runs and experiments are ledger rows over AWS-side artifacts; use the Experiments **cleanup** action (`POST /api/experiments/{id}/action` with `{"action":"cleanup"}`) to remove the optimization loop's gateway config bundles and A/B setup. |
| **Registry records** | Disable a record from the Registry console, or `POST /api/registry/records/{id}/action` with `{"action":"disable"}` — this moves it to `DEPRECATED` (terminal). |
| **API keys** | Disable via `POST /api/apikeys/{id}/disable`. |

Delete demo agents when you are done with them so you stop paying for their
runtime and stored artifacts. The golden-path E2E
(`backend/scripts/e2e_golden_path.py`) does exactly this in its cleanup step:
delete the dataset, disable the registry record, delete the agent.

## 2. Shared infra (bootstrap-provisioned, one command)

Everything `make bootstrap` (and the lazy Knowledge-Base gateway bootstrap)
created is removed by `scripts/teardown.py`, in reverse creation order so
dependents go before the shared substrate. It also sweeps the per-agent IAM
roles, including the orphans a failed agent deletion can leave behind:

```
Gateway targets of launchpad-gw / -kb-gw / -coa-gw   # dependents of the gateways
   → Gateways launchpad-gw, -kb-gw, -coa-gw             # matched by name; waits for targets to drop out
     → Cedar Policy Engines Launchpad created for them  # policies first
      → Credential providers                            # launchpad-office-facts-key (API key), launchpad-gw-m2m (OAuth2)
        → Console workload identities                   # launchpad-console-* (the COA context layer)
         → Per-agent IAM roles                          # launchpad-agent-* AND tagged launchpad:agent-id
            → AgentCore memory  (launchpad_memory-*)
               → AgentCore registry (launchpad-registry)   # records deleted first
                  → Skill Lab worker runtime + its IAM role
                   → Coding runtime launchpad_coding_runtime + its IAM role   # runtime first
                     → CDK stack launchpad-base             # S3 auto-empties, ECR force-deletes
```

**Always dry-run first** to see exactly what would be removed:

```bash
cd backend
uv run python ../scripts/teardown.py --dry-run   # list targets, delete nothing
uv run python ../scripts/teardown.py --yes        # delete (gateway layer → providers → roles → memory → registry → CDK stack)
```

Flags: `--region <region>` (defaults to the configured region), `--dry-run`
(list only), `--yes` (required to actually delete — without it, teardown behaves
as a dry-run).

Notes:

- Deletion is **best-effort and ordered dependents-first**: gateway targets are
  deleted before their gateway (the gateway delete waits, bounded, for the
  targets to disappear and retries once the API stops reporting a conflict),
  registry records before the registry, and everything before the CDK stack.
  A failed target prints a warning and the loop continues.
- The CDK stack deletion runs `cdk destroy --force`; the S3 artifacts bucket
  auto-empties and the ECR repo force-deletes as part of the stack. The agent-files bucket
  (`launchpad-agent-files-<account>-<region>`, the agents' session output folders) is
  **retained** on purpose — customer files outlive the stack; empty and delete it by hand
  when nobody needs them (an agent delete already removes that agent's folder, and objects
  expire 30 days after creation).
- Teardown targets are discovered by exact name or name prefix — gateways
  `launchpad-gw` / `launchpad-kb-gw` / `launchpad-coa-gw` (the COA context layer's
  managed gateway) (by gateway **name**, never by the ids in
  `config/launchpad.yaml`, which may be stale), credential providers
  `launchpad-office-facts-key` / `launchpad-gw-m2m`, `launchpad_memory-*`,
  `launchpad-registry`, the Skill Lab worker, stack `launchpad-base` —
  resources outside those names are never touched.
- Per-agent IAM roles are swept only when **both** hold: the name starts with
  `launchpad-agent-` **and** the role carries the `launchpad:agent-id` tag. The
  shared `launchpad-agent-execution-role` belongs to the CDK stack and is never
  matched. Every per-agent role is tagged at creation, so the sweep also lists
  the roles of agents you have not deleted yet — not only the orphans a failed
  agent deletion leaves behind. Delete demo agents first (section 1) and review
  the dry-run before confirming.
- Cedar Policy Engines that Launchpad created for `launchpad-gw` /
  `launchpad-kb-gw` are deleted after the gateways, their policies first. An
  engine must carry Launchpad's managed tags, so one someone else created is
  never matched; a leftover engine from an earlier deployment is (it is found by
  its "Policy Engine managed from Launchpad for …" description). Policy names are
  unique across the account and Region, so a leftover engine would otherwise
  block the next deployment's identically named policies.
- Console workload identities `launchpad-console-<workspace>` are the token-vault
  identities the COA context layer creates when an admin connects a COA
  deployment (`/v2/context-layer`, see [identity.md](identity.md) §7.7). Every
  identity with that name prefix is swept; a runtime's own workload identity is
  named after the runtime and never matches. The layer's OAuth2 Connection
  (default `coa-<prefix>`) is an ordinary Connection: disconnect the layer, then
  delete it under Outbound credentials. COA itself is a separate product and is
  never touched.
- Deployed agent runtimes are demo-scoped (section 1) and are not touched by
  the script; deleting your demo agents first keeps the shared-infra teardown
  clean. X-Ray Transaction Search is account-wide shared
  observability and stays out of scope.

## Delete the agents before the VPC

Agents (and hosted MCP servers) on a VPC network profile hold ENIs in its subnets
until AgentCore releases them, some time after the runtime is deleted. Delete those
agents first, wait for their ENI-release jobs, archive the profile, and only then
delete the subnets, security groups and the VPC. See
[private-networking.md §4](private-networking.md#4-teardown-delete-the-agents-before-the-vpc).

## Order of operations (recommended)

1. Delete demo **agents** (console or `DELETE /api/agents/{id}`).
2. Disable their **registry records** and delete **eval datasets** / run
   **experiment cleanup**.
3. Run `scripts/teardown.py --dry-run`, review, then `--yes`.
