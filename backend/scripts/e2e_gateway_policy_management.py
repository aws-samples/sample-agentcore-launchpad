#!/usr/bin/env python3
"""Guarded real-AWS E2E for existing Gateway Policy management.

The target must be a disposable MCP Gateway. The script never deletes Policy,
Engine, Gateway, or Registry resources. It removes only the management tags it
added unless --keep-managed is set.

Example:
  cd backend
  uv run python scripts/e2e_gateway_policy_management.py \
    --gateway-id <DISPOSABLE_GATEWAY_ID> \
    --statement-file /tmp/e2e-policy.cedar \
    --confirm-real-aws

Add --allow-enforcement-changes, --candidate-statement-file, and
--override-reason to exercise ENFORCE, candidate cutover, and rollback.
"""

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

TERMINAL_OPERATION_STATES = {"succeeded", "failed", "partial", "interrupted"}


def request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(method, path, json=json)
    if not response.is_success:
        raise RuntimeError(
            f"{method} {path} failed ({response.status_code}): {response.text[:1000]}"
        )
    return response.json()


def wait_operation(
    client: httpx.Client,
    operation: dict[str, Any],
    *,
    timeout_s: int = 900,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    current = operation
    while current.get("status") not in TERMINAL_OPERATION_STATES:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"operation {current.get('id')} did not settle")
        # Real-AWS operation polling is intentionally paced and deadline-bounded.
        time.sleep(3)  # nosemgrep: arbitrary-sleep
        current = request(
            client,
            "GET",
            f"/api/governance/operations/{current['id']}",
        )["operation"]
    print(
        f"  operation {current['operation']} {current['id']}: "
        f"{current['status']}"
    )
    if current["status"] != "succeeded":
        raise RuntimeError(
            f"operation {current['id']} ended {current['status']}: "
            f"{current.get('error')}"
        )
    return current


def gateway_detail(client: httpx.Client, gateway_id: str) -> dict[str, Any]:
    return request(client, "GET", f"/api/governance/gateways/{gateway_id}")


def policies(client: httpx.Client, gateway_id: str) -> dict[str, Any]:
    return request(client, "GET", f"/api/governance/gateways/{gateway_id}/policies")


def policy_by_id(
    client: httpx.Client,
    gateway_id: str,
    policy_id: str,
) -> dict[str, Any]:
    return next(
        item
        for item in policies(client, gateway_id)["policies"]
        if item["id"] == policy_id
    )


def mutation_envelope(gateway: dict[str, Any]) -> dict[str, Any]:
    return {
        "expected_gateway_updated_at": gateway["updated_at"],
        "acknowledged_gateway_ids": [
            item["id"] for item in gateway["shared_gateways"]
        ],
    }


def queue_operation(
    client: httpx.Client,
    path: str,
    payload: dict[str, Any],
    *,
    method: str = "POST",
) -> dict[str, Any]:
    return wait_operation(
        client,
        request(client, method, path, json=payload)["operation"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--gateway-id", required=True)
    parser.add_argument("--statement-file", type=Path, required=True)
    parser.add_argument("--candidate-statement-file", type=Path)
    parser.add_argument("--policy-name")
    parser.add_argument("--override-reason")
    parser.add_argument("--allow-enforcement-changes", action="store_true")
    parser.add_argument("--keep-managed", action="store_true")
    parser.add_argument("--confirm-real-aws", action="store_true")
    args = parser.parse_args()
    if not args.confirm_real_aws:
        parser.error("--confirm-real-aws is required; use a disposable Gateway")
    if args.allow_enforcement_changes and not args.override_reason:
        parser.error("--override-reason is required with --allow-enforcement-changes")
    if args.allow_enforcement_changes and not args.candidate_statement_file:
        parser.error(
            "--candidate-statement-file is required with "
            "--allow-enforcement-changes"
        )
    return args


def main() -> int:
    args = parse_args()
    statement = args.statement_file.read_text(encoding="utf-8").strip()
    candidate_statement = (
        args.candidate_statement_file.read_text(encoding="utf-8").strip()
        if args.candidate_statement_file
        else None
    )
    if not statement:
        raise ValueError("statement file is empty")
    client = httpx.Client(base_url=args.base, timeout=300)
    gateway = gateway_detail(client, args.gateway_id)
    was_managed = gateway["managed"]
    print(f"-- target: {gateway['name']} ({gateway['id']})")
    print(f"   ARN: {gateway['arn']}")
    print("   Resources created by this E2E are intentionally retained.")

    try:
        if not was_managed:
            print("-- add Launchpad management tags")
            request(
                client,
                "POST",
                f"/api/governance/gateways/{args.gateway_id}/manage",
            )
        gateway = gateway_detail(client, args.gateway_id)

        if gateway["policy_engine"] is None:
            print("-- create and attach Policy Engine in LOG_ONLY")
            queue_operation(
                client,
                f"/api/governance/gateways/{args.gateway_id}/engine",
                {
                    **mutation_envelope(gateway),
                    "authorization_model": "custom",
                },
            )
            gateway = gateway_detail(client, args.gateway_id)
        else:
            print(
                f"-- adopt existing engine {gateway['policy_engine']['name']} "
                f"({gateway['policy_engine']['mode']})"
            )

        policy_name = args.policy_name or (
            "e2e_gateway_" + datetime.now(UTC).strftime("%m%d%H%M%S")
        )
        print(f"-- create LOG_ONLY policy {policy_name}")
        create_operation = queue_operation(
            client,
            f"/api/governance/gateways/{args.gateway_id}/policies",
            {
                **mutation_envelope(gateway),
                "name": policy_name,
                "statement": statement,
                "description": "Launchpad existing-Gateway guarded E2E",
                "authorization_model": "custom",
            },
        )
        policy_id = create_operation["after"]["policy"]["id"]
        print(f"   policy: {policy_id}")

        print("-- import Gateway-level Registry record (never auto-approve)")
        preview = request(
            client,
            "GET",
            f"/api/governance/gateways/{args.gateway_id}/registry-preview",
        )
        gateway = gateway_detail(client, args.gateway_id)
        imported = request(
            client,
            "POST",
            f"/api/governance/gateways/{args.gateway_id}/registry-import",
            json={
                **mutation_envelope(gateway),
                "record_name": preview["proposed"]["name"],
                "apply_update": preview["outcome"] == "changed",
            },
        )
        print(
            f"   Registry {imported['outcome']}: "
            f"{imported['record']['record_id']} ({imported['record']['status']})"
        )

        if args.allow_enforcement_changes:
            print("-- promote policy ACTIVE with explicit zero-evidence override")
            gateway = gateway_detail(client, args.gateway_id)
            current_policy = policy_by_id(client, args.gateway_id, policy_id)
            queue_operation(
                client,
                (
                    f"/api/governance/gateways/{args.gateway_id}/policies/"
                    f"{policy_id}/promote"
                ),
                {
                    **mutation_envelope(gateway),
                    "expected_policy_updated_at": current_policy["updated_at"],
                    "evidence_range": "24h",
                    "confirmation_name": gateway["name"],
                    "override_reason": args.override_reason,
                },
            )

            print("-- move Gateway to ENFORCE")
            gateway = gateway_detail(client, args.gateway_id)
            queue_operation(
                client,
                f"/api/governance/gateways/{args.gateway_id}/mode",
                {
                    **mutation_envelope(gateway),
                    "mode": "ENFORCE",
                    "evidence_range": "24h",
                    "confirmation_name": gateway["name"],
                    "override_reason": args.override_reason,
                },
            )

            print("-- create LOG_ONLY candidate from ACTIVE policy")
            gateway = gateway_detail(client, args.gateway_id)
            current_policy = policy_by_id(client, args.gateway_id, policy_id)
            candidate_operation = queue_operation(
                client,
                f"/api/governance/gateways/{args.gateway_id}/policies/{policy_id}",
                {
                    **mutation_envelope(gateway),
                    "expected_policy_updated_at": current_policy["updated_at"],
                    "statement": candidate_statement,
                    "description": "Launchpad guarded E2E candidate",
                },
                method="PUT",
            )
            candidate_id = candidate_operation["after"]["candidate"]["id"]

            print("-- cut over candidate, then roll it back")
            gateway = gateway_detail(client, args.gateway_id)
            candidate = policy_by_id(client, args.gateway_id, candidate_id)
            transition = {
                **mutation_envelope(gateway),
                "expected_policy_updated_at": candidate["updated_at"],
                "evidence_range": "24h",
                "confirmation_name": gateway["name"],
                "override_reason": args.override_reason,
                "audit_id": candidate_operation["id"],
            }
            queue_operation(
                client,
                (
                    f"/api/governance/gateways/{args.gateway_id}/policies/"
                    f"{candidate_id}/promote"
                ),
                transition,
            )
            gateway = gateway_detail(client, args.gateway_id)
            candidate = policy_by_id(client, args.gateway_id, candidate_id)
            transition.update(
                expected_gateway_updated_at=gateway["updated_at"],
                expected_policy_updated_at=candidate["updated_at"],
            )
            queue_operation(
                client,
                (
                    f"/api/governance/gateways/{args.gateway_id}/policies/"
                    f"{candidate_id}/rollback"
                ),
                transition,
            )

            print("-- return Gateway to LOG_ONLY")
            gateway = gateway_detail(client, args.gateway_id)
            queue_operation(
                client,
                f"/api/governance/gateways/{args.gateway_id}/mode",
                {
                    **mutation_envelope(gateway),
                    "mode": "LOG_ONLY",
                    "evidence_range": "24h",
                    "confirmation_name": gateway["name"],
                },
            )
    finally:
        if not was_managed and not args.keep_managed:
            print("-- remove only the Launchpad management tags")
            request(
                client,
                "DELETE",
                f"/api/governance/gateways/{args.gateway_id}/manage",
            )

    print("E2E GATEWAY POLICY MANAGEMENT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
