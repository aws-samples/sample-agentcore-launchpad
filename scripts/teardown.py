#!/usr/bin/env python3
"""Launchpad teardown CLI — best-effort removal of everything bootstrap created.

Scope (reverse creation order, dependents before the shared substrate):
  1. Gateway targets of launchpad-gw / launchpad-kb-gw / launchpad-coa-gw (the
     COA context layer's managed gateway), then the gateways themselves (matched
     by gateway name; they reference the Cognito pool and the IAM roles that go
     with the stack)
  2. Cedar Policy Engines Launchpad created for those gateways (tagged
     managed-by agentcore-launchpad, and attached to one of them or described
     as "Policy Engine managed from Launchpad for <gateway>"), policies first.
     Policy names are unique across the account and Region, so a leftover
     engine's policies would block the next deployment's identically named ones
  3. Credential providers launchpad-office-facts-key (API key) and
     launchpad-gw-m2m (OAuth2), matched by exact name
  3b. Console workload identities launchpad-console-* (the COA context layer's
     token-vault identity, one per workspace; the Connection it used is a
     Connection like any other and is deleted from the console)
  4. Per-agent IAM roles: launchpad-agent-* carrying the launchpad:agent-id
     tag. Every per-agent role is tagged at creation, so this matches the roles
     of agents you have not deleted yet as well as the orphans a failed agent
     deletion leaves behind — delete demo agents first. The stack-owned
     launchpad-agent-execution-role is never matched, nor is a role whose
     policies name another Region (IAM is account-global; that role serves
     another deployment's agent)
  5. AgentCore memory  (launchpad_memory-*)
  6. AgentCore registry (launchpad-registry) — records must be gone first
  7. Skill Lab exec worker (launchpad_skill_lab_worker runtime + its IAM role);
     its image tags and the skill-lab/ S3 prefix live in stack-owned ECR/S3
  7b. Coding runtime (launchpad_coding_runtime runtime + its IAM role, runtime
     first); its image tags and the coding/ repo snapshots live in
     stack-owned ECR/S3
  8. CDK stack launchpad-base (S3 bucket auto-empties, ECR force-deletes). The
     agent-files bucket (launchpad-agent-files-<account>-<region>, the agents'
     session output folders) is RETAINED on purpose — customer files outlive the
     stack; empty and delete it by hand once nobody needs them.

Out of scope: X-Ray Transaction Search (account-wide, shared observability)
and deployed agent runtimes — delete demo agents from the
console/API first.

Usage:
    cd backend && uv run python ../scripts/teardown.py --dry-run
    cd backend && uv run python ../scripts/teardown.py --yes
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services import agent_iam  # noqa: E402
from app.services import bootstrap as bs  # noqa: E402
from app.services.agentcore import policy as policy_api  # noqa: E402
from app.services.context_layer import CONSOLE_WORKLOAD_PREFIX  # noqa: E402
from app.services.context_layer_gateway import GATEWAY_NAME as COA_GATEWAY_NAME  # noqa: E402
from app.services.gateway_bootstrap import (  # noqa: E402
    API_KEY_PROVIDER_NAME,
    GATEWAY_M2M_PROVIDER_NAME,
    GATEWAY_NAME,
)
from app.services.kb_gateway import KB_GATEWAY_NAME  # noqa: E402
from app.services.workspace_iam import EXECUTION_ROLE_BASE  # noqa: E402

# Gateways bootstrap creates, in deletion order. Matched by *name* — the ids in
# config/launchpad.yaml may be stale or absent on the box running teardown.
GATEWAY_NAMES = (GATEWAY_NAME, KB_GATEWAY_NAME, COA_GATEWAY_NAME)

# How long a gateway delete waits for its (already-deleted) targets to drop out
# of the listing before trying anyway, and how many ConflictException retries.
GATEWAY_TARGETS_GONE_TIMEOUT_S = 90
GATEWAY_DELETE_ATTEMPTS = 5

# The description governance.py gives every Policy Engine it creates, so an engine
# a previous deployment left behind (its gateway already gone) is still found.
ENGINE_DESCRIPTION_PREFIX = "Policy Engine managed from Launchpad for "
# How long an engine delete waits for its (already-deleted) policies to drop out.
ENGINE_POLICIES_GONE_TIMEOUT_S = 120
ENGINE_DELETE_ATTEMPTS = 5


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


def _policy_engine_targets(control) -> list[tuple[str, str, str]]:
    """Launchpad-created Policy Engines of the stack gateways — attached ones and
    the leftovers of an earlier deployment of the same gateways. An engine must
    carry Launchpad's managed tags; an engine someone else created is never
    matched, even when it is attached to launchpad-gw."""
    attached: set[str] = set()
    for gw in policy_api.list_gateways(control):
        if gw.get("name") not in GATEWAY_NAMES:
            continue
        detail = policy_api.get_gateway(control, gw["gatewayId"])
        arn = (detail.get("policyEngineConfiguration") or {}).get("arn")
        if arn:
            attached.add(arn)
    described = {f"{ENGINE_DESCRIPTION_PREFIX}{name}" for name in GATEWAY_NAMES}
    targets: list[tuple[str, str, str]] = []
    for engine in policy_api.list_policy_engines(control):
        arn = engine.get("policyEngineArn", "")
        if arn not in attached and engine.get("description") not in described:
            continue
        if not policy_api.is_managed(policy_api.list_tags(control, arn)):
            continue
        targets.append(
            ("policy-engine", engine["policyEngineId"], f"policy engine {engine.get('name', '?')}")
        )
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


def _console_workload_targets(control) -> list[tuple[str, str, str]]:
    """The COA context layer's console workload identities (launchpad-console-*).
    Runtime-owned workload identities are named after their runtime and never match."""
    targets: list[tuple[str, str, str]] = []
    kwargs: dict = {"maxResults": 20}
    while True:
        page = control.list_workload_identities(**kwargs)
        for identity in page.get("workloadIdentities", []):
            name = identity.get("name", "")
            if name.startswith(CONSOLE_WORKLOAD_PREFIX):
                targets.append(
                    ("console-workload", name, identity.get("workloadIdentityArn", ""))
                )
        token = page.get("nextToken")
        if not token:
            return targets
        kwargs = {"maxResults": 20, "nextToken": token}


def _role_region(iam, role_name: str) -> str | None:
    """The Region a per-agent role serves, read from its own inline policies: every
    capability policy carries the `Telemetry` statement on
    `arn:<partition>:logs:<region>:…:/aws/bedrock-agentcore/runtimes/*`. None when
    no policy names one (an orphan whose policies a half-failed delete removed)."""
    try:
        names = iam.list_role_policies(RoleName=role_name).get("PolicyNames", [])
    except Exception:  # noqa: BLE001 — unreadable: treated like an orphan
        return None
    for policy_name in names:
        try:
            document = iam.get_role_policy(
                RoleName=role_name, PolicyName=policy_name
            ).get("PolicyDocument", {})
        except Exception:  # noqa: BLE001
            continue
        if isinstance(document, str):  # boto3 decodes it; a raw client may not
            try:
                document = json.loads(urllib.parse.unquote(document))
            except ValueError:
                continue
        for statement in document.get("Statement", []) or []:
            resources = statement.get("Resource", [])
            for arn in [resources] if isinstance(resources, str) else resources:
                parts = str(arn).split(":")
                if (len(parts) > 6 and parts[2] == "logs"
                        and "/aws/bedrock-agentcore/runtimes/" in str(arn)):
                    return parts[3]
    return None


def _agent_role_targets(iam, region: str | None = None) -> list[tuple[str, str, str]]:
    """Per-agent execution roles: `launchpad-agent-*` **and** tagged
    `launchpad:agent-id`. Both are required — the tag alone is what separates
    them from the stack-owned shared execution role, which is skipped by name
    as well (its name is regional: `launchpad-agent-execution-role[-<region>]`).
    agent_iam tags every role it creates, so live agents' roles match too; the
    orphans of failed agent deletions are the ones nothing else will ever sweep.

    IAM is account-global, but a teardown removes ONE Region's deployment: a role
    whose policies name another Region belongs to another deployment's agent
    (live 2026-10-20: a us-west-2 teardown deleted the role of a us-east-1 agent,
    which then failed every invoke with "The execution role cannot be assumed"),
    so it is skipped. A role naming no Region is an orphan and is still swept.
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
            role_region = _role_region(iam, name) if region else None
            if role_region and role_region != region:
                print(f"  skip {name}: serves an agent in {role_region}, not {region}")
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
    targets.extend(_policy_engine_targets(control))
    targets.extend(_credential_provider_targets(control))
    targets.extend(_console_workload_targets(control))
    targets.extend(_agent_role_targets(iam, region))

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
    runtimes = rt.list_runtimes(control)
    for runtime in runtimes:
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

    # Coding runtime (runtime before its role: deleting the role under a live
    # runtime can wedge the runtime's own deletion).
    from app.coding.infra import RUNTIME_NAME as CODING_RUNTIME_NAME
    from app.services.workspace_iam import CODING_RUNTIME_ROLE_BASE

    for runtime in runtimes:
        if runtime.get("agentRuntimeName") == CODING_RUNTIME_NAME:
            targets.append(
                ("coding-runtime", runtime["agentRuntimeId"], runtime["agentRuntimeArn"])
            )
    coding_role = regional_role_name(CODING_RUNTIME_ROLE_BASE, region)
    try:
        role = iam.get_role(RoleName=coding_role)
        targets.append(("coding-role", coding_role, role["Role"]["Arn"]))
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


def _wait_runtime_gone(control, runtime_id: str, timeout_s: int) -> None:
    """Runtime deletion is asynchronous; its role must outlive it. Bounded wait."""
    deadline = time.time() + timeout_s
    while True:
        try:
            control.get_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                return
            raise
        if time.time() >= deadline:
            # no identifier in the message: teardown targets share one list with
            # credential-provider names, so CodeQL treats every identifier as sensitive
            print("  warning: the coding runtime is still deleting; continuing")
            return
        # Deliberate polling interval; the deadline above bounds the loop.
        time.sleep(5)  # nosemgrep: arbitrary-sleep


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


def _delete_policy_engine(control, engine_id: str) -> None:
    """Policies first (the engine refuses to go while it holds any), then the
    engine, retrying while the gateway that used it is still detaching."""
    for policy in policy_api.list_policies(control, engine_id):
        policy_api.delete_policy(control, engine_id=engine_id, policy_id=policy["policyId"])
    deadline = time.time() + ENGINE_POLICIES_GONE_TIMEOUT_S
    while policy_api.list_policies(control, engine_id):
        if time.time() >= deadline:
            print(f"  warning: engine {engine_id} still lists policies; deleting anyway")
            break
        time.sleep(3)  # nosemgrep: arbitrary-sleep
    for attempt in range(1, ENGINE_DELETE_ATTEMPTS + 1):
        try:
            control.delete_policy_engine(policyEngineId=engine_id)
            return
        except Exception as exc:
            conflict = "ConflictException" in f"{type(exc).__name__}{exc}"
            if not conflict or attempt == ENGINE_DELETE_ATTEMPTS:
                raise
            print(f"  engine {engine_id} busy ({attempt}/{ENGINE_DELETE_ATTEMPTS}), retrying…")
            time.sleep(5)  # nosemgrep: arbitrary-sleep


def delete_target(kind: str, identifier: str, region: str) -> None:
    control = bs._client("bedrock-agentcore-control", region)
    if kind == "gateway-target":
        gateway_id, target_id = identifier.split("/", 1)
        control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    elif kind == "gateway":
        _delete_gateway(control, identifier)
    elif kind == "policy-engine":
        _delete_policy_engine(control, identifier)
    elif kind == "api-key-provider":
        control.delete_api_key_credential_provider(name=identifier)
    elif kind == "oauth2-provider":
        control.delete_oauth2_credential_provider(name=identifier)
    elif kind == "console-workload":
        control.delete_workload_identity(name=identifier)
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
    elif kind == "coding-runtime":
        from app.services.agentcore import runtime as rt

        rt.delete_runtime(control, identifier)
        _wait_runtime_gone(control, identifier, timeout_s=600)
    elif kind == "coding-role":
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
