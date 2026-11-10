"""Read-only, regional network quota preflight for the two CN deployment templates.

This deliberately covers only VPCs, internet gateways and Amazon-provided EIPs.
It is not a general AWS capacity checker or an authorization to delete resources.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

from botocore.exceptions import ClientError

QUOTAS = {
    "vpc": ("vpc", "L-F678F1CE"),
    "internet_gateway": ("vpc", "L-A4707A72"),
    "amazon_eip": ("ec2", "L-0263D0A3"),
}
COMPLETE = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}


def _physical_resources(cfn, stack: dict | None) -> dict[str, str]:
    if stack is None or stack["StackStatus"] in COMPLETE:
        return {}
    resources = {}
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack["StackId"]):
        for row in page["StackResourceSummaries"]:
            if row.get("PhysicalResourceId") and row["ResourceStatus"] != "DELETE_COMPLETE":
                resources[row["LogicalResourceId"]] = row["PhysicalResourceId"]
    return resources


def _inventory(ec2, resource: str, account: str) -> tuple[int, set[str]]:
    if resource == "amazon_eip":
        rows = ec2.describe_addresses()["Addresses"]
        ids = {row[key] for row in rows for key in ("AllocationId", "PublicIp") if row.get(key)}
        used = sum(
            row.get("PublicIpv4Pool") in (None, "amazon")
            and not row.get("CustomerOwnedIp")
            and not row.get("CustomerOwnedIpv4Pool")
            for row in rows
        )
        return used, ids
    api, result, identifier = {
        "vpc": ("describe_vpcs", "Vpcs", "VpcId"),
        "internet_gateway": (
            "describe_internet_gateways", "InternetGateways", "InternetGatewayId",
        ),
    }[resource]
    ids = {
        row[identifier]
        for page in ec2.get_paginator(api).paginate()
        for row in page[result]
        if row.get("OwnerId", account) == account
    }
    return len(ids), ids


def _error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response["Error"]["Code"])
    return type(exc).__name__


def network_capacity(session, cfn, cfg: dict, host: dict | None, nlb: dict | None) -> dict:
    """Inspect fresh capacity only for allocations this apply may still need.

    Callers must establish stack ownership first. A completed stack is never
    recreated by apply, so its allocations are not new demand and need no quota
    permissions. Partial/failed stacks receive credit only for physical IDs still
    present in this account/region's inventory.
    """
    account, region = cfg["account_id"], cfg["region"]
    report = {
        "scope": "regional_network_quotas", "account_id": account, "region": region,
        "checked_at": datetime.now(UTC).isoformat(), "checks": [],
        "not_checked": ["EC2 vCPU/capacity", "OpenSearch", "AgentCore", "other AWS quotas"],
    }
    # Every list element represents one possible allocation; None means not yet created.
    needs: dict[str, list[str | None]] = {name: [] for name in QUOTAS}
    discovery_errors = {}
    for stack, requirements in (
        (host, {
            "vpc": "Vpc", "internet_gateway": "InternetGateway",
            **({"amazon_eip": "ElasticIp"}
               if cfg["parameters"].get("AllocateElasticIp", "true") == "true" else {}),
        }),
        (nlb, {"amazon_eip": "StaticIp"} if cfg.get("console_entry") == "public_nlb" else {}),
    ):
        if not requirements or (stack and stack["StackStatus"] in COMPLETE):
            continue
        try:
            physical = _physical_resources(cfn, stack)
        except Exception as exc:
            for resource in requirements:
                discovery_errors[resource] = (
                    f"cloudformation:ListStackResources failed ({_error(exc)})"
                )
            physical = {}
        for resource, logical in requirements.items():
            needs[resource].append(physical.get(logical))

    for resource, (service, code) in QUOTAS.items():
        check = {
            "resource": resource, "service_code": service, "quota_code": code,
            "used": None, "quota": None, "needed": 0, "status": "not_required",
        }
        report["checks"].append(check)
        if not needs[resource]:
            continue
        check["needed"] = None
        if resource in discovery_errors:
            check.update(
                status="unknown", error=discovery_errors[resource],
                action="Restore CloudFormation read permission; do not guess existing allocations.",
            )
            continue
        try:
            used, existing = _inventory(session.client("ec2"), resource, account)
            needed = sum(identifier not in existing for identifier in needs[resource])
            check.update(used=used, needed=needed)
        except Exception as exc:
            check.update(
                status="unknown", error=f"EC2 inventory failed ({_error(exc)})",
                action="Restore ec2:DescribeVpcs/DescribeInternetGateways/DescribeAddresses "
                       "and regional endpoint access before creating resources.",
            )
            continue
        if needed == 0:
            continue
        try:
            quota = session.client("service-quotas").get_service_quota(
                ServiceCode=service, QuotaCode=code,
            )["Quota"]
            if isinstance(quota["Value"], bool):
                raise ValueError("Invalid quota value")
            value = float(quota["Value"])
            if not math.isfinite(value) or value < 0 or quota.get("GlobalQuota"):
                raise ValueError("Invalid regional quota response")
            check["quota"] = value
        except Exception as exc:
            check.update(
                status="unknown", error=f"servicequotas:GetServiceQuota failed ({_error(exc)})",
                action="Restore Service Quotas read permission/endpoint access; no default "
                       "quota or pending increase request is treated as effective capacity.",
            )
            continue
        check["status"] = "sufficient" if used + needed <= value else "insufficient"
        if check["status"] == "insufficient":
            check["action"] = (
                f"Request {service}/{code} increase to at least {used + needed} in {region} "
                "and wait for the effective quota; or explicitly reclaim only known unused "
                "resources. This deployment does not delete existing resources."
            )
    report["status"] = (
        "blocked" if any(row["status"] in ("unknown", "insufficient") for row in report["checks"])
        else "not_required" if all(row["status"] == "not_required" for row in report["checks"])
        else "sufficient"
    )
    return report


def require_network_capacity(report: dict) -> None:
    blocked = [row for row in report["checks"] if row["status"] in ("unknown", "insufficient")]
    if blocked:
        detail = "; ".join(
            f"{row['resource']}: used={row['used']} quota={row['quota']} needed={row['needed']} "
            f"({row.get('error', row['status'])}). {row['action']}" for row in blocked
        )
        raise RuntimeError(
            f"Network quota preflight blocked {report['account_id']}/{report['region']}: {detail}"
        )
