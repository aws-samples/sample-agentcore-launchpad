#!/usr/bin/env python3
"""Launchpad teardown CLI — best-effort removal of everything bootstrap created.

Scope (reverse creation order):
  1. AgentCore memory  (launchpad_memory-*)
  2. AgentCore registry (launchpad-registry) — records must be gone first
  3. Skill Lab exec worker (launchpad_skill_lab_worker runtime + its IAM role);
     its image tags and the skill-lab/ S3 prefix live in stack-owned ECR/S3
  4. CDK stack launchpad-base (S3 bucket auto-empties, ECR force-deletes)

Later phases extend this list (gateway, policy engine, runtimes) — teardown
always deletes dependents before the shared substrate.

Usage:
    cd backend && uv run python ../scripts/teardown.py --dry-run
    cd backend && uv run python ../scripts/teardown.py --yes
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services import bootstrap as bs  # noqa: E402


def collect_targets(region: str) -> list[tuple[str, str, str]]:
    """(kind, identifier, description) for every resource we would delete."""
    control = bs._client("bedrock-agentcore-control", region)
    registry_control = bs._client("agent-registry-control", region)
    targets: list[tuple[str, str, str]] = []

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
    iam = bs._client("iam", region)
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


def delete_target(kind: str, identifier: str, region: str) -> None:
    control = bs._client("bedrock-agentcore-control", region)
    if kind == "memory":
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
        iam = bs._client("iam", region)
        for policy in iam.list_role_policies(RoleName=identifier)["PolicyNames"]:
            iam.delete_role_policy(RoleName=identifier, PolicyName=policy)
        iam.delete_role(RoleName=identifier)
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
