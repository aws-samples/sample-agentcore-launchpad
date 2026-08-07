#!/usr/bin/env python3
"""Launchpad teardown CLI — best-effort removal of everything bootstrap created.

Scope (reverse creation order):
  1. AgentCore memory  (launchpad_memory-*)
  2. AgentCore registry (launchpad-registry) — records must be gone first
  3. CDK stack launchpad-base (S3 bucket auto-empties, ECR force-deletes)

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
