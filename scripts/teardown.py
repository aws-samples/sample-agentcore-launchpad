#!/usr/bin/env python3
"""Launchpad teardown CLI — best-effort removal of everything bootstrap created.

Scope (reverse creation order, dependents before the shared substrate):
  1. Gateway targets of launchpad-gw / launchpad-kb-gw, then the two gateways
     themselves (matched by gateway name; they reference the Cognito pool and
     the IAM roles that go with the stack)
  2. Credential providers launchpad-office-facts-key (API key) and
     launchpad-gw-m2m (OAuth2), matched by exact name
  3. Per-agent IAM roles: launchpad-agent-* carrying the launchpad:agent-id
     tag. Every per-agent role is tagged at creation, so this matches the roles
     of agents you have not deleted yet as well as the orphans a failed agent
     deletion leaves behind — delete demo agents first. The stack-owned
     launchpad-agent-execution-role is never matched
  4. AgentCore memory  (launchpad_memory-*)
  5. AgentCore registry (launchpad-registry) — records must be gone first
  6. Skill Lab exec worker (launchpad_skill_lab_worker runtime + its IAM role);
     its image tags and the skill-lab/ S3 prefix live in stack-owned ECR/S3
  7. CDK stack launchpad-base (S3 bucket auto-empties, ECR force-deletes)

Out of scope: X-Ray Transaction Search (account-wide, shared observability),
the Policy engine, and deployed agent runtimes — delete demo agents from the
console/API first.

Usage:
    cd backend && uv run python ../scripts/teardown.py --dry-run
    cd backend && uv run python ../scripts/teardown.py --yes
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services import agent_iam  # noqa: E402
from app.services import bootstrap as bs  # noqa: E402
from app.services.agentcore import policy as policy_api  # noqa: E402
from app.services.gateway_bootstrap import (  # noqa: E402
    API_KEY_PROVIDER_NAME,
    GATEWAY_M2M_PROVIDER_NAME,
    GATEWAY_NAME,
)
from app.services.kb_gateway import KB_GATEWAY_NAME  # noqa: E402
from app.services.workspace_iam import EXECUTION_ROLE_BASE  # noqa: E402

# Gateways bootstrap creates, in deletion order. Matched by *name* — the ids in
# config/launchpad.yaml may be stale or absent on the box running teardown.
GATEWAY_NAMES = (GATEWAY_NAME, KB_GATEWAY_NAME)

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
    by_name = {gw.get("name"): gw for gw in policy_api.list_gateways(control)}
    gateways = [by_name[name] for name in GATEWAY_NAMES if name in by_name]
    targets: list[tuple[str, str, str]] = []
    for gw in gateways:
        for target in policy_api.list_gateway_targets(control, gw["gatewayId"]):
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
    for provider in _list_credential_providers(control, "list_api_key_credential_providers"):
        if provider.get("name") == API_KEY_PROVIDER_NAME:
            targets.append(
                (
                    "api-key-provider",
                    API_KEY_PROVIDER_NAME,
                    provider.get("credentialProviderArn", ""),
                )
            )
    for provider in _list_credential_providers(control, "list_oauth2_credential_providers"):
        if provider.get("name") == GATEWAY_M2M_PROVIDER_NAME:
            targets.append(
                (
                    "oauth2-provider",
                    GATEWAY_M2M_PROVIDER_NAME,
                    provider.get("credentialProviderArn", ""),
                )
            )
    return targets


def _agent_role_targets(iam) -> list[tuple[str, str, str]]:
    """Per-agent execution roles: `launchpad-agent-*` **and** tagged
    `launchpad:agent-id`. Both are required — the tag alone is what separates
    them from the stack-owned shared execution role, which is skipped by name
    as well (its name is regional: `launchpad-agent-execution-role[-<region>]`).
    agent_iam tags every role it creates, so live agents' roles match too; the
    orphans of failed agent deletions are the ones nothing else will ever sweep.
    """
    targets: list[tuple[str, str, str]] = []
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
            desc = f"per-agent execution role ({agent_iam.MANAGED_TAG_KEY}={agent_id})"
            targets.append(("agent-role", name, desc))
    return targets


def collect_targets(region: str) -> list[tuple[str, str, str]]:
    """(kind, identifier, description) for every resource we would delete."""
    control = bs._client("bedrock-agentcore-control", region)
    registry_control = bs._client("agent-registry-control", region)
    iam = bs._client("iam", region)
    targets: list[tuple[str, str, str]] = []

    # Gateway layer first: targets, then the gateways (they reference the Cognito
    # pool and gateway/KB roles that belong to the stack), then the credential
    # providers the gateway targets and harnesses used, then per-agent roles.
    targets.extend(_gateway_targets(control))
    targets.extend(_credential_provider_targets(control))
    targets.extend(_agent_role_targets(iam))

    memories = control.list_memories(maxResults=100).get("memories", [])
    for mem in memories:
        if mem["id"].startswith(f"{bs.MEMORY_NAME}-"):
            targets.append(("memory", mem["id"], mem["arn"]))

    registries = registry_control.list_registries(maxResults=100).get("registries", [])
    for reg in registries:
        if reg["name"] == bs.REGISTRY_NAME:
            targets.append(("registry", reg["registryId"], reg["registryArn"]))

    # Skill Lab exec worker (runtime + IAM role). Image tags and the skill-lab/
    # S3 prefix live in stack-owned ECR/S3 and go with the stack.
    from app.services.agentcore import runtime as rt
    from app.services.workspace_iam import SKILL_LAB_ROLE_BASE, regional_role_name
    from app.skill_lab.infra import WORKER_RUNTIME_NAME

    # Paginated: an account past one page of runtimes would otherwise keep the
    # worker (and its role) behind after the stack is gone.
    for runtime in rt.list_runtimes(control):
        if runtime.get("agentRuntimeName") == WORKER_RUNTIME_NAME:
            targets.append(
                ("skill-lab-runtime", runtime["agentRuntimeId"], runtime["agentRuntimeArn"])
            )
    role_name = regional_role_name(SKILL_LAB_ROLE_BASE, region)
    try:
        role = iam.get_role(RoleName=role_name)
        targets.append(("skill-lab-role", role_name, role["Role"]["Arn"]))
    except Exception:
        pass

    try:
        bs.get_stack_outputs(region)
        targets.append(("cdk-stack", bs.STACK_NAME, "cloudformation stack + all resources"))
    except Exception:
        pass
    return targets


def _delete_role(iam, role_name: str) -> None:
    """Inline policies first — IAM refuses to delete a role that still has any."""
    for policy in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=role_name, PolicyName=policy)
    iam.delete_role(RoleName=role_name)


def _wait_targets_gone(control, gateway_id: str, timeout_s: int) -> None:
    """Targets were deleted just before the gateway; they linger as DELETING for
    a while and the gateway delete conflicts until they are gone. Bounded wait."""
    deadline = time.time() + timeout_s
    while policy_api.list_gateway_targets(control, gateway_id):
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
    elif kind == "agent-role":
        _delete_role(bs._client("iam", region), identifier)
    elif kind == "memory":
        control.delete_memory(memoryId=identifier)
    elif kind == "registry":
        registry_control = bs._client("agent-registry-control", region)
        records = registry_control.list_registry_records(
            registryId=identifier, maxResults=100
        ).get("registryRecords", [])
        for rec in records:
            registry_control.delete_registry_record(
                registryId=identifier, recordId=rec["recordId"]
            )
        registry_control.delete_registry(registryId=identifier)
    elif kind == "skill-lab-runtime":
        from app.services.agentcore import runtime as rt

        rt.delete_runtime(control, identifier)
    elif kind == "skill-lab-role":
        _delete_role(bs._client("iam", region), identifier)
    elif kind == "cdk-stack":
        subprocess.run(
            ["uv", "run", "cdk", "destroy", "--force"],
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
    args = parser.parse_args()

    region = args.region or bs.get_settings().region
    targets = collect_targets(region)
    if not targets:
        print("nothing to tear down")
        return 0

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
