#!/usr/bin/env python3
"""Launchpad teardown CLI — best-effort removal of everything bootstrap created.

Scope (reverse creation order, dependents before the shared substrate):
  1. Gateway targets of launchpad-gw / launchpad-kb-gw, then the two gateways
     themselves (matched by gateway name; they reference the gateway/KB IAM
     roles that go with the stack)
  2. Credential providers launchpad-office-facts-key (API key) and
     launchpad-gw-m2m (OAuth2), matched by exact name
  2.5. The evaluation worker Runtime (N12): the system preset launchpad-eval-worker
     deployed by the `evaluation` bootstrap stage, matched by the exact Runtime name
     shape `<runtime_name_base(env-suffixed preset name)>_<6 hex>` — never by prefix,
     since the default environment's base is a strict prefix of a suffixed one's
     (N12-bug12); its per-agent role is swept in step 3, its S3 artefacts
     (evaluations/, eval-datasets/) with the bucket
  2.6. The runtime log groups of every runtime deleted here (steps 2.5 and 3.5):
     /aws/bedrock-agentcore/runtimes/<runtime_id>-* — once the runtime is gone, then
     the same settle re-check the agent delete runs (N-SCN-obs8: re-list at +10/30/60 s,
     delete what the trailing container flush re-created, stop after two empty checks)
  3. Per-agent IAM roles: launchpad-agent-* carrying the launchpad:agent-id
     tag. Every per-agent role is tagged at creation, so this matches the roles
     of agents you have not deleted yet as well as the orphans a failed agent
     deletion leaves behind — delete demo agents first. The stack-owned
     launchpad-agent-execution-role is never matched
  3.5. Skill Lab exec worker (launchpad_skill_lab_worker runtime + its IAM role);
     its image tags and the skill-lab/ S3 prefix live in stack-owned ECR/S3; its
     runtime log groups as in 2.6
  4. The CN control-plane EC2 CloudFormation stack
     (infra/ec2/launchpad-cn-ec2.yaml), if the LAUNCHPAD_EC2_STACK_NAME
     environment variable names it and it exists — optional, since this script
     has no other record of that stack's name
  4.5. CDK stack launchpad-memory[-env] (this environment's SQS queue + DLQ
     and memory-extractor Lambda) — deleted and waited on before
     launchpad-base, since it imports the base stack's artifacts-bucket
     exports and CloudFormation refuses to delete an export still in use
  5. CDK stack launchpad-base (S3 bucket auto-empties, ECR force-deletes)
  5.5. Account-level residue the stacks leave behind (N11-obs43), ONLY when no other
     launchpad-base[-env] / launchpad-memory[-env] stack remains in the region: the API
     Gateway CloudWatch role(s) `launchpad-base[-env]-OfficeFactsApiCloudWatchRole…`
     (CDK retains them; the account-wide AWS::ApiGateway::Account setting of every
     environment's REST API points at one) and the service-created
     `/aws/lambda/launchpad-{hr-database,office-facts,memory-extractor-<region>}[-env]`
     log groups of stacks deployed before N11-obs56 (the CDK-named groups go with the
     stacks). While another environment exists they are listed as
     "shared — kept while <stacks> exist" and never deleted
  6. CDK stack launchpad-memory-domain — the OpenSearch domain every EnvName
     environment of this account+region shares (D17). KEPT by default: it is
     deleted only with --delete-shared-domain, and even then only when no
     other launchpad-memory[-env] environment stack still exists (the
     indices of every environment live in this one domain)

Registry has no AWS footprint in the CN build; Memory/KB ride the memory
stacks torn down at steps 4.5 and 6.

Out of scope: X-Ray Transaction Search (account-wide, shared observability),
the Policy engine, and deployed agent runtimes — delete demo agents from the
console/API first.

Usage:
    cd backend && uv run python ../scripts/teardown.py --dry-run
    cd backend && uv run python ../scripts/teardown.py --yes
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core import naming  # noqa: E402
from app.core.naming import ENV_TAG_KEY, resource_name  # noqa: E402
from app.services import agent_iam  # noqa: E402
from app.services import bootstrap as bs  # noqa: E402
from app.services import memory_bootstrap as mb  # noqa: E402
from app.services.agentcore import gateway as gateway_api  # noqa: E402
from app.services.agentcore import runtime as runtime_api  # noqa: E402
from app.services.gateway_bootstrap import (  # noqa: E402
    API_KEY_PROVIDER_NAME,
    GATEWAY_M2M_PROVIDER_NAME,
    GATEWAY_NAME,
    api_key_provider_name,
    gateway_m2m_provider_name,
)
from app.services.workspace_iam import EXECUTION_ROLE_BASE  # noqa: E402
from app.system_agents.presets import EVAL_WORKER, EVAL_WORKER_BASE_NAME  # noqa: E402

# N11: every name below is a *base* name; `resource_name()` applies `Settings.env_name`
# (LAUNCHPAD_ENV_NAME) at use, so a teardown run with LAUNCHPAD_ENV_NAME=t1 discovers
# only `launchpad-*-t1` resources / stacks and leaves the default environment alone
# (and vice versa). Per-agent roles are told apart by their `launchpad:env` tag.

# The AgentCore KB connector gateway a pre-CN install created lazily; nothing in the
# CN build creates one (the kb_gateway service is gone), but teardown still removes
# a leftover by name.
KB_GATEWAY_NAME = "launchpad-kb-gw"

# The CN control-plane EC2 CloudFormation stack (infra/ec2/launchpad-cn-ec2.yaml)
# is independent of the CDK launchpad-base stack but is the thing *using* its
# resources, so it is torn down first — mirrors creation order in reverse
# (gateway targets → gateways → credential providers → agent roles → EC2 stack →
# memory stack → base stack, spec N5 + N6/R21).
EC2_STACK_NAME_ENV = "LAUNCHPAD_EC2_STACK_NAME"

# The N6 self-hosted Memory/KB substrate stack (per environment). It imports
# launchpad-base's artifacts bucket and exec-role ARNs (infra/stacks/memory_stack.py),
# so CloudFormation will refuse to delete launchpad-base's exports while this
# stack still imports them — it must be fully deleted (not just delete_stack
# issued) before launchpad-base's own deletion is attempted (R21).
MEMORY_STACK_NAME = mb.MEMORY_STACK_NAME
MEMORY_STACK_DELETE_WAITER_CONFIG = {"Delay": 30, "MaxAttempts": 60}
# D17: the shared OpenSearch domain stack — unsuffixed, one per account+region. Nothing
# imports it (the environment stacks resolve its endpoint from SSM), so CloudFormation
# would happily delete it under a still-running sibling environment; this script is the
# guard: last in order, opt-in, and only when no other environment stack remains.
MEMORY_DOMAIN_STACK_NAME = mb.MEMORY_DOMAIN_STACK_NAME


def _env_stack_pattern(base: str) -> re.Pattern[str]:
    """`<base>` or `<base>-<env>` (naming.ENV_NAME_PATTERN) and nothing else — never
    `launchpad-memory-domain` for the memory base, never `launchpad-memory-extract-…`."""
    return re.compile(rf"^{re.escape(base)}(-[a-z0-9]{{1,{naming.ENV_NAME_MAX_LEN}}})?$")


# `launchpad-memory` or `launchpad-memory-<env>`; `launchpad-base` or `launchpad-base-<env>`.
_ENV_MEMORY_STACK_RE = _env_stack_pattern(MEMORY_STACK_NAME)
_ENV_BASE_STACK_RE = _env_stack_pattern(bs.STACK_NAME)
_LIVE_STACK_STATUSES = (
    "CREATE_IN_PROGRESS", "CREATE_FAILED", "CREATE_COMPLETE", "ROLLBACK_IN_PROGRESS",
    "ROLLBACK_FAILED", "ROLLBACK_COMPLETE", "DELETE_IN_PROGRESS", "DELETE_FAILED",
    "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", "UPDATE_COMPLETE",
    "UPDATE_FAILED", "UPDATE_ROLLBACK_IN_PROGRESS", "UPDATE_ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS", "UPDATE_ROLLBACK_COMPLETE",
    "REVIEW_IN_PROGRESS", "IMPORT_IN_PROGRESS", "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_IN_PROGRESS", "IMPORT_ROLLBACK_FAILED", "IMPORT_ROLLBACK_COMPLETE",
)

# Gateways bootstrap creates, in deletion order. Matched by *name* — the ids in
# config/launchpad.yaml may be stale or absent on the box running teardown.
GATEWAY_NAMES = (GATEWAY_NAME, KB_GATEWAY_NAME)
# Base names of the two credential providers bootstrap creates (resolved per env below).
PROVIDER_NAMES = (API_KEY_PROVIDER_NAME, GATEWAY_M2M_PROVIDER_NAME)

# N11-obs43: account-level residue that survives the stacks (user ruling 2026-10-14 #3).
# (a) The API Gateway CloudWatch role. CDK's `LambdaRestApi` (infra/stacks/base_stack.py,
# construct `OfficeFactsApi`) creates `<stack>-OfficeFactsApiCloudWatchRole<hash>-<random>`
# with AmazonAPIGatewayPushToCloudWatchLogs attached and points the ACCOUNT-WIDE
# `AWS::ApiGateway::Account` setting at it; CDK retains the role on stack deletion, so
# every deleted or rolled-back launchpad-base[-env] leaves one behind (deploy-ops 6b found
# three). The account setting is shared by every environment's REST API, so the role may
# only go once no other launchpad-base[-env] / launchpad-memory[-env] stack remains —
# deleting the role a sibling's API Gateway account setting points at breaks its logging.
# Nothing in backend/app creates it: the name is CDK's, hence matched by shape here.
# (b) The service-created `/aws/lambda/<function>` groups of the stack Lambdas: stacks
# deployed before N11-obs56 logged there with no retention; since then the Lambdas log into
# CDK-named groups that go with the stack, and the old groups are orphans nothing deletes.
# Same gate, so a one-environment teardown never reaches a sibling's groups by prefix.
APIGW_CLOUDWATCH_ROLE_CONSTRUCT = "OfficeFactsApiCloudWatchRole"
_APIGW_CLOUDWATCH_ROLE_RE = re.compile(
    rf"^{re.escape(bs.STACK_NAME)}(-[a-z0-9]{{1,{naming.ENV_NAME_MAX_LEN}}})?-"
    rf"{APIGW_CLOUDWATCH_ROLE_CONSTRUCT}[0-9A-Fa-f]*-[A-Za-z0-9]+$"
)
# Base function names of the Lambdas the CDK stacks create — `function_name=name(...)` in
# infra/stacks/base_stack.py (the two samples) and memory_stack.py (the extractor, whose
# name carries the region before the env suffix). Test-pinned against the stack sources.
SAMPLE_LAMBDA_BASE_NAMES = ("launchpad-hr-database", "launchpad-office-facts")
MEMORY_EXTRACTOR_LAMBDA_BASE = "launchpad-memory-extractor"
LAMBDA_LOG_GROUP_PREFIX = "/aws/lambda/launchpad-"
APIGW_CLOUDWATCH_ROLE_KIND = "apigw-cloudwatch-role"
LAMBDA_LOG_GROUP_KIND = "lambda-log-group"


def lambda_log_group_pattern(region: str) -> re.Pattern[str]:
    """`/aws/lambda/<base>[-env]` for exactly the stack Lambdas of `region`."""
    names = "|".join(
        re.escape(n)
        for n in (*SAMPLE_LAMBDA_BASE_NAMES, f"{MEMORY_EXTRACTOR_LAMBDA_BASE}-{region}")
    )
    return re.compile(
        rf"^/aws/lambda/({names})(-[a-z0-9]{{1,{naming.ENV_NAME_MAX_LEN}}})?$"
    )


def gateway_names() -> tuple[str, ...]:
    return tuple(resource_name(name) for name in GATEWAY_NAMES)

# How long a gateway delete waits for its (already-deleted) targets to drop out
# of the listing before trying anyway, and how many ConflictException retries.
GATEWAY_TARGETS_GONE_TIMEOUT_S = 90
GATEWAY_DELETE_ATTEMPTS = 5


def _list_credential_providers(control, list_op: str) -> list[dict]:
    """All credential providers across pages for `list_op`
    (list_api_key_credential_providers / list_oauth2_credential_providers)."""
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = getattr(control, list_op)(**kwargs)
        items.extend(page.get("credentialProviders", []))
        token = page.get("nextToken")
        if not token:
            return items
        kwargs = {"nextToken": token}


def _gateway_targets(control) -> list[tuple[str, str, str]]:
    """Gateway targets first (dependents), then the gateways they belong to."""
    by_name = {gw.get("name"): gw for gw in gateway_api.list_gateways(control)}
    gateways = [by_name[name] for name in gateway_names() if name in by_name]
    targets: list[tuple[str, str, str]] = []
    for gw in gateways:
        for target in gateway_api.list_gateway_targets(control, gw["gatewayId"]):
            targets.append(
                (
                    "gateway-target",
                    f"{gw['gatewayId']}/{target['targetId']}",
                    f"{gw['name']}/{target.get('name', '?')}",
                )
            )
    for gw in gateways:
        targets.append(("gateway", gw["gatewayId"], f"gateway {gw['name']}"))
    return targets


def _credential_provider_targets(control) -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    api_key_provider, m2m_provider = api_key_provider_name(), gateway_m2m_provider_name()
    for provider in _list_credential_providers(control, "list_api_key_credential_providers"):
        if provider.get("name") == api_key_provider:
            targets.append(
                (
                    "api-key-provider",
                    api_key_provider,
                    provider.get("credentialProviderArn", ""),
                )
            )
    for provider in _list_credential_providers(control, "list_oauth2_credential_providers"):
        if provider.get("name") == m2m_provider:
            targets.append(
                (
                    "oauth2-provider",
                    m2m_provider,
                    provider.get("credentialProviderArn", ""),
                )
            )
    return targets


def eval_worker_runtime_pattern() -> re.Pattern[str]:
    """Exact shape of this environment's evaluation worker Runtime name:
    ``^<base>_[0-9a-f]{6}$`` where the base is derived from the preset's EnvName-suffixed
    agent name exactly as ``zip_runtime.sanitize_runtime_name`` does
    (``agent_iam.runtime_name_pattern``). The env name is resolved at call time
    (``resource_name``), like every other selector here.

    N12-bug12: a ``startswith(base + "_")`` test is not enough — the default environment's
    base ``launchpad_eval_worker`` is a strict prefix of the ``t1`` environment's
    ``launchpad_eval_worker_t1``, so a default-env teardown would have selected
    ``launchpad_eval_worker_t1_bd4aba`` (seen in a live ``--dry-run``). The shape pins the
    name to base + one 6-hex segment and nothing else; the suffixed direction was already
    safe (``launchpad_eval_worker_t1_`` never prefixes a default-env name)."""
    return agent_iam.runtime_name_pattern(resource_name(EVAL_WORKER_BASE_NAME))


# N-SCN-obs8: how long to wait for a deleted runtime to be gone before its log groups go,
# and the settle re-check offsets (seconds after the first delete) — the agent delete's.
RUNTIME_GONE_TIMEOUT_S = 120
LOG_GROUP_SETTLE_S: tuple[float, ...] = (10.0, 30.0, 60.0)
LOG_GROUPS_KIND = "runtime-log-groups"
_RUNTIME_KINDS = ("agent-runtime", "skill-lab-runtime")


def log_group_target(runtime_id: str) -> tuple[str, str, str]:
    """The log-group follow-up of one runtime target: deleted right after the runtime,
    with the settle re-check (step 2.6 of the module docstring)."""
    from app.deployer.teardown import format_schedule, runtime_log_group_prefix

    return (
        LOG_GROUPS_KIND, runtime_id,
        f"runtime log groups {runtime_log_group_prefix(runtime_id)}* "
        f"(settle re-check at {format_schedule(LOG_GROUP_SETTLE_S)})",
    )


def with_log_groups(targets: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Interleave a ``runtime-log-groups`` target after every runtime target."""
    out: list[tuple[str, str, str]] = []
    for target in targets:
        out.append(target)
        if target[0] in _RUNTIME_KINDS:
            out.append(log_group_target(target[1]))
    return out


def _eval_worker_targets(control) -> list[tuple[str, str, str]]:
    pattern = eval_worker_runtime_pattern()
    targets: list[tuple[str, str, str]] = []
    for runtime in runtime_api.list_runtimes(control):
        name = str(runtime.get("agentRuntimeName") or "")
        if pattern.match(name):
            targets.append((
                "agent-runtime", runtime["agentRuntimeId"],
                f"evaluation worker runtime {name} (system preset {EVAL_WORKER.key})",
            ))
    return with_log_groups(targets)


POLICY_LOG_GROUP_KIND = "policy-log-group"


def _policy_log_group_targets(logs) -> list[tuple[str, str, str]]:
    """D20 P1-d: this environment's CloudWatch decisions log group
    (`/launchpad/policy/decisions[-env]`), when it exists. The `policy/` S3 prefix goes
    with the artifacts bucket (base stack)."""
    from app.services import policy_audit

    name = policy_audit.log_group_name()
    if policy_audit.probe_log_group(logs, name) != "ready":
        return []
    return [(POLICY_LOG_GROUP_KIND, name, "policy decisions log group (D20 audit sink)")]


def _agent_role_targets(iam) -> list[tuple[str, str, str]]:
    """Per-agent execution roles: `launchpad-agent-*` **and** tagged
    `launchpad:agent-id`. Both are required — the tag alone is what separates
    them from the stack-owned shared execution role, which is skipped by name
    as well (its name is regional: `launchpad-agent-execution-role[-<region>]`).
    agent_iam tags every role it creates, so live agents' roles match too; the
    orphans of failed agent deletions are the ones nothing else will ever sweep.
    N11: a role's `launchpad:env` tag (absent = the default environment) must equal
    this run's env name — another environment's agents are never this run's orphans.
    """
    targets: list[tuple[str, str, str]] = []
    env_name = naming.current_env_name()
    # `PathPrefix` does not filter by name, so walk every page and filter here.
    for page in iam.get_paginator("list_roles").paginate():
        for role in page.get("Roles", []):
            name = role["RoleName"]
            if not name.startswith(agent_iam._ROLE_PREFIX):
                continue
            if name.startswith(EXECUTION_ROLE_BASE):
                continue  # stack-owned shared role — goes with the CDK stack
            tags = iam.list_role_tags(RoleName=name).get("Tags", [])
            agent_id = next(
                (t.get("Value", "") for t in tags if t.get("Key") == agent_iam.MANAGED_TAG_KEY),
                None,
            )
            if agent_id is None:
                continue
            role_env = next(
                (t.get("Value", "") for t in tags if t.get("Key") == ENV_TAG_KEY), ""
            )
            if role_env != env_name:
                continue  # belongs to another environment in this account
            desc = f"per-agent execution role ({agent_iam.MANAGED_TAG_KEY}={agent_id})"
            targets.append(("agent-role", name, desc))
    return targets


def other_environment_stacks(cloudformation, base: str, own: str) -> list[str]:
    """Names of every live `<base>[-env]` stack in the region other than `own` (this
    environment's — it is being deleted in the same run). The D17 check generalised
    (N11-obs43): `base` is `launchpad-memory` or `launchpad-base`."""
    pattern = _env_stack_pattern(base)
    names: list[str] = []
    for page in cloudformation.get_paginator("list_stacks").paginate(
        StackStatusFilter=list(_LIVE_STACK_STATUSES)
    ):
        for summary in page.get("StackSummaries", []):
            name = summary["StackName"]
            if name != own and pattern.match(name):
                names.append(name)
    return sorted(set(names))


def other_environment_memory_stacks(cloudformation, own: str | None = None) -> list[str]:
    """Every `launchpad-memory[-env]` stack other than `own`. Non-empty means another
    environment still has indices on the shared domain (D17)."""
    return other_environment_stacks(
        cloudformation, MEMORY_STACK_NAME, own or mb.memory_stack_name()
    )


def other_environments(cloudformation) -> list[str]:
    """Every other environment's base or memory stack still live in the region — the gate
    for the account-level residue (N11-obs43): non-empty means the API Gateway CloudWatch
    role and the orphan Lambda log groups stay."""
    names = other_environment_stacks(cloudformation, bs.STACK_NAME, bs.base_stack_name())
    names += other_environment_memory_stacks(cloudformation)
    return sorted(set(names))


def _apigw_cloudwatch_role_targets(iam) -> list[tuple[str, str, str]]:
    """Every `launchpad-base[-env]-OfficeFactsApiCloudWatchRole…` role of the account — any
    environment's: once the gate says no other environment remains, each is an orphan."""
    targets: list[tuple[str, str, str]] = []
    for page in iam.get_paginator("list_roles").paginate():
        for role in page.get("Roles", []):
            name = role["RoleName"]
            if _APIGW_CLOUDWATCH_ROLE_RE.match(name):
                targets.append((
                    APIGW_CLOUDWATCH_ROLE_KIND, name,
                    "API Gateway CloudWatch role (retained by its launchpad-base stack; "
                    "the account-wide ApiGateway::Account setting)",
                ))
    return targets


def _lambda_log_group_targets(logs, region: str) -> list[tuple[str, str, str]]:
    """The service-created `/aws/lambda/<stack function>[-env]` groups (pre-N11-obs56
    stacks) — any environment's, same reasoning as the roles. Paginated by prefix."""
    pattern = lambda_log_group_pattern(region)
    targets: list[tuple[str, str, str]] = []
    kwargs: dict = {"logGroupNamePrefix": LAMBDA_LOG_GROUP_PREFIX}
    while True:
        page = logs.describe_log_groups(**kwargs)
        for group in page.get("logGroups", []):
            name = group["logGroupName"]
            if pattern.match(name):
                targets.append((
                    LAMBDA_LOG_GROUP_KIND, name,
                    "service-created Lambda log group (pre-N11-obs56; no stack owns it)",
                ))
        token = page.get("nextToken")
        if not token:
            return targets
        kwargs = {"logGroupNamePrefix": LAMBDA_LOG_GROUP_PREFIX, "nextToken": token}


def shared_residue_targets(cloudformation, iam, logs, region: str) -> list[tuple[str, str, str]]:
    """N11-obs43: the API Gateway CloudWatch role(s) and the orphan Lambda log groups as
    targets — only when no other environment's stack remains; otherwise printed as
    "shared — kept while <stacks> exist" and left alone (the dry run shows the same)."""
    candidates = _apigw_cloudwatch_role_targets(iam) + _lambda_log_group_targets(logs, region)
    if not candidates:
        return []
    others = other_environments(cloudformation)
    if others:
        print(f"  note: shared — kept while {', '.join(others)} exist:")
        for kind, identifier, desc in candidates:
            print(f"    [{kind}] {identifier} — {desc}")
        return []
    return candidates


def shared_domain_target(
    region: str, *, delete_shared_domain: bool
) -> tuple[str, str, str] | None:
    """The domain stack as a teardown target, or None (with a printed reason) when it must
    stay: not requested, not deployed, or still used by another environment (D17)."""
    dedicated = bool(naming.memory_domain_env())
    if not dedicated and not delete_shared_domain:
        return None
    try:
        bs.get_stack_outputs(region, mb.memory_domain_stack_name())
    except Exception:
        return None
    if dedicated:
        return ("cdk-stack", mb.memory_domain_stack_name(), "dedicated OpenSearch domain — last")
    others = other_environment_memory_stacks(bs._client("cloudformation", region))
    if others:
        print(
            f"  note: keeping shared domain stack {mb.memory_domain_stack_name()} — still "
            f"used by {', '.join(others)}"
        )
        return None
    return (
        "cdk-stack",
        mb.memory_domain_stack_name(),
        "shared OpenSearch domain stack (every environment's indices) — last",
    )


def collect_targets(
    region: str, *, delete_shared_domain: bool = False
) -> list[tuple[str, str, str]]:
    """(kind, identifier, description) for every resource we would delete, in order."""
    control = bs._client("bedrock-agentcore-control", region)
    iam = bs._client("iam", region)
    targets: list[tuple[str, str, str]] = []

    # Gateway layer first: targets, then the gateways (they reference the
    # gateway/KB roles that belong to the stack), then the credential
    # providers the gateway targets and harnesses used, then per-agent roles.
    targets.extend(_gateway_targets(control))
    targets.extend(_credential_provider_targets(control))
    # the evaluation worker Runtime before the per-agent roles (its own is one of them)
    targets.extend(_eval_worker_targets(control))
    targets.extend(_agent_role_targets(iam))
    # D20 P1-d: the policy decisions log group — created by the `policy` bootstrap stage,
    # owned by no stack. Discovery is best-effort like the stacks below.
    try:
        targets.extend(_policy_log_group_targets(bs._client("logs", region)))
    except Exception:
        pass

    # Skill Lab exec worker (runtime + IAM role). Image tags and the skill-lab/
    # S3 prefix live in stack-owned ECR/S3 and go with the stack.
    from app.services.agentcore import runtime as rt
    from app.services.workspace_iam import SKILL_LAB_ROLE_BASE, regional_role_name
    from app.skill_lab.infra import worker_runtime_name

    # Paginated: an account past one page of runtimes would otherwise keep the
    # worker (and its role) behind after the stack is gone.
    wanted_runtime = worker_runtime_name()
    for runtime in rt.list_runtimes(control):
        if runtime.get("agentRuntimeName") == wanted_runtime:
            targets.extend(with_log_groups([
                ("skill-lab-runtime", runtime["agentRuntimeId"], runtime["agentRuntimeArn"])
            ]))
    role_name = regional_role_name(SKILL_LAB_ROLE_BASE, region)
    try:
        role = iam.get_role(RoleName=role_name)
        targets.append(("skill-lab-role", role_name, role["Role"]["Arn"]))
    except Exception:
        pass

    ec2_stack_name = os.environ.get(EC2_STACK_NAME_ENV)
    if ec2_stack_name:
        cfn = bs._client("cloudformation", region)
        try:
            cfn.describe_stacks(StackName=ec2_stack_name)
            targets.append(("ec2-stack", ec2_stack_name, "control-plane EC2 CloudFormation stack"))
        except Exception:
            pass

    # The memory stack imports launchpad-base's exports, so it must be deleted
    # (and waited on — see delete_target) before the base stack is even
    # attempted (R21): discovered first, deleted first.
    try:
        bs.get_stack_outputs(region, mb.memory_stack_name())
        targets.append(
            (
                "cdk-stack",
                mb.memory_stack_name(),
                "memory substrate stack (OpenSearch domain, queue, Lambda)",
            )
        )
    except Exception:
        pass

    try:
        bs.get_stack_outputs(region)
        targets.append(
            ("cdk-stack", bs.base_stack_name(), "cloudformation stack + all resources")
        )
    except Exception:
        pass

    # N11-obs43: the account-level residue goes AFTER the base stack (CDK retains the role
    # when the stack is deleted; a Lambda's final flush can re-create its group) and only
    # when this was the last environment. Discovery is best-effort like the stacks above —
    # an undiscoverable account keeps everything.
    try:
        targets.extend(shared_residue_targets(
            bs._client("cloudformation", region), iam, bs._client("logs", region), region
        ))
    except Exception:
        pass

    # D17: the shared domain goes last, and only on explicit request with no other
    # environment left on it. Default: kept, so `teardown.py` of one environment never
    # takes a sibling's memory/KB data with it.
    domain = shared_domain_target(region, delete_shared_domain=delete_shared_domain)
    if domain:
        targets.append(domain)
    return targets


def _delete_role(iam, role_name: str) -> None:
    """Inline policies and managed-policy attachments first — IAM refuses to delete a role
    that still has either (the API Gateway CloudWatch role carries the managed
    AmazonAPIGatewayPushToCloudWatchLogs; the per-agent roles are inline-only)."""
    for policy in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=role_name, PolicyName=policy)
    attached = iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
    for policy in attached:
        iam.detach_role_policy(RoleName=role_name, PolicyArn=policy["PolicyArn"])
    iam.delete_role(RoleName=role_name)


def _wait_targets_gone(control, gateway_id: str, timeout_s: int) -> None:
    """Targets were deleted just before the gateway; they linger as DELETING for
    a while and the gateway delete conflicts until they are gone. Bounded wait."""
    deadline = time.time() + timeout_s
    while gateway_api.list_gateway_targets(control, gateway_id):
        if time.time() >= deadline:
            print(f"  warning: gateway {gateway_id} still lists targets; deleting anyway")
            return
        # Deliberate polling interval; the deadline above bounds the loop.
        time.sleep(3)  # nosemgrep: arbitrary-sleep


def _delete_gateway(control, gateway_id: str) -> None:
    _wait_targets_gone(control, gateway_id, GATEWAY_TARGETS_GONE_TIMEOUT_S)
    for attempt in range(1, GATEWAY_DELETE_ATTEMPTS + 1):
        try:
            control.delete_gateway(gatewayIdentifier=gateway_id)
            return
        except Exception as exc:
            conflict = "ConflictException" in f"{type(exc).__name__}{exc}"
            if not conflict or attempt == GATEWAY_DELETE_ATTEMPTS:
                raise
            print(f"  gateway {gateway_id} busy ({attempt}/{GATEWAY_DELETE_ATTEMPTS}), retrying…")
            time.sleep(5)  # nosemgrep: arbitrary-sleep


def delete_runtime_log_groups(control, logs, runtime_id: str) -> None:
    """Wait for the runtime to be gone, delete its log groups, then settle (N-SCN-obs8) —
    synchronously: this is a CLI, and the settle is bounded by ``LOG_GROUP_SETTLE_S``."""
    from app.deployer import teardown as agent_teardown

    def say(msg: str) -> None:
        print(f"  {msg}", flush=True)

    gone = runtime_api.wait_runtime_gone(
        control, runtime_id, timeout_s=RUNTIME_GONE_TIMEOUT_S, interval_s=3, sleeper=time.sleep
    )
    if not gone:
        say(f"runtime {runtime_id} still present after {RUNTIME_GONE_TIMEOUT_S}s — deleting "
            "its log groups anyway (the settle re-check catches a trailing flush)")
    deleted, errors = agent_teardown.delete_runtime_log_groups(logs, runtime_id, say)
    outcome = agent_teardown.settle_log_groups(
        logs, agent_teardown.runtime_log_group_prefix(runtime_id),
        schedule=LOG_GROUP_SETTLE_S, log=say, sleeper=time.sleep,
    )
    say(f"{len(deleted)} log group(s) deleted · settle: {outcome.summary()}")
    for error in errors:
        say(f"warning: {error}")


def delete_target(kind: str, identifier: str, region: str) -> None:
    control = bs._client("bedrock-agentcore-control", region)
    if kind == "gateway-target":
        gateway_id, target_id = identifier.split("/", 1)
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    elif kind == "gateway":
        _delete_gateway(control, identifier)
    elif kind == "api-key-provider":
        control.delete_api_key_credential_provider(name=identifier)
    elif kind == "oauth2-provider":
        control.delete_oauth2_credential_provider(name=identifier)
    elif kind == "agent-runtime":
        try:
            runtime_api.delete_runtime(control, identifier)
        except control.exceptions.ResourceNotFoundException:
            pass
    elif kind == LOG_GROUPS_KIND:
        delete_runtime_log_groups(control, bs._client("logs", region), identifier)
    elif kind == POLICY_LOG_GROUP_KIND:
        logs = bs._client("logs", region)
        try:
            logs.delete_log_group(logGroupName=identifier)
        except logs.exceptions.ResourceNotFoundException:
            pass
    elif kind in ("agent-role", APIGW_CLOUDWATCH_ROLE_KIND):
        _delete_role(bs._client("iam", region), identifier)
    elif kind == LAMBDA_LOG_GROUP_KIND:
        logs = bs._client("logs", region)
        try:
            logs.delete_log_group(logGroupName=identifier)
        except logs.exceptions.ResourceNotFoundException:
            pass
    elif kind == "skill-lab-runtime":
        from app.services.agentcore import runtime as rt

        rt.delete_runtime(control, identifier)
    elif kind == "skill-lab-role":
        _delete_role(bs._client("iam", region), identifier)
    elif kind == "ec2-stack":
        bs._client("cloudformation", region).delete_stack(StackName=identifier)
    elif kind == "cdk-stack" and identifier in (
        mb.memory_stack_name(), mb.memory_domain_stack_name()
    ):
        # Deleted (and waited on) via the raw CloudFormation API, not `cdk
        # destroy` — the base stack's deletion a moment later must not race a
        # memory-stack delete that CloudFormation itself still considers
        # in-flight (R21: launchpad-base's exports are imported by this stack).
        # The shared domain stack (D17) takes the same path: unsuffixed, so
        # `cdk destroy` would need no context, but a waited raw delete reports
        # the ~10-minute domain deletion's outcome instead of returning early.
        cloudformation = bs._client("cloudformation", region)
        cloudformation.delete_stack(StackName=identifier)
        cloudformation.get_waiter("stack_delete_complete").wait(
            StackName=identifier, WaiterConfig=MEMORY_STACK_DELETE_WAITER_CONFIG
        )
    elif kind == "cdk-stack":
        # N11: the same `--context env_name=<x>` as deploy, or the CDK app would
        # synthesize the *default* stack ids and not find `identifier` to destroy.
        subprocess.run(
            ["uv", "run", "cdk", "destroy", identifier, "--force", *bs.cdk_context_args()],
            cwd=REPO_ROOT / "infra",
            check=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", default=None, help="AWS region (default: settings)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="list the resources that would be removed, delete nothing",
    )
    parser.add_argument(
        "--yes", action="store_true", help="confirm deletion (required to delete)"
    )
    parser.add_argument(
        "--delete-shared-domain", action="store_true",
        help=(
            "also delete the shared OpenSearch domain stack (launchpad-memory-domain, D17) "
            "— refused while any other launchpad-memory[-env] environment stack exists; "
            "kept by default"
        ),
    )
    args = parser.parse_args()

    region = args.region or bs.get_settings().region
    targets = collect_targets(region, delete_shared_domain=args.delete_shared_domain)
    if not targets:
        print("nothing to tear down")
        return 0
    if not args.delete_shared_domain and not naming.memory_domain_env():
        print(
            f"(shared domain stack {mb.memory_domain_stack_name()} is kept — pass "
            "--delete-shared-domain to remove it once no environment uses it)"
        )

    print("══ teardown targets (reverse creation order) ══")
    for kind, identifier, desc in targets:
        print(f"  [{kind}] {identifier} — {desc}")

    if args.dry_run or not args.yes:
        print("\ndry-run — nothing deleted (pass --yes to delete)")
        return 0

    for kind, identifier, _ in targets:
        print(f"deleting [{kind}] {identifier}…", flush=True)
        try:
            delete_target(kind, identifier, region)
        except Exception as exc:  # best-effort: keep going
            print(f"  warning: {exc}")
    print("teardown complete (best-effort)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
