"""Network capacity must fail before a new deployment writes anything."""
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from tests.test_cn_distribution import REPO, config, module


@pytest.fixture
def capacity():
    return module(REPO / "scripts/deploy_cn_capacity.py")


@pytest.mark.parametrize("operation", ["plan", "apply", "plan_then_apply"])
def test_full_vpc_stops_before_any_aws_write(tmp_path, monkeypatch, operation):
    deploy = module(REPO / "scripts/deploy_cn.py")
    path, cfg = config(tmp_path)
    artifact = tmp_path / "engine.tar.gz"
    artifact.write_bytes(b"engine")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix(".gz.sha256").write_text(digest)
    cfg["engine_artifact"] = str(artifact)
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(deploy, "resolve_commit", lambda _: "release")
    monkeypatch.setattr(deploy.subprocess, "run",
                        lambda *_, **__: SimpleNamespace(stdout="release"))
    writes = []
    effective_quota = [5]

    def missing(**_):
        raise ClientError({"Error": {"Code": "ValidationError", "Message": "does not exist"}},
                          "DescribeStacks")

    def write(*_, **__):
        writes.append("write")
        raise AssertionError("AWS write attempted despite full VPC quota")

    inventory = {
        "describe_vpcs": {"Vpcs": [{"VpcId": f"vpc-{i}"} for i in range(5)]},
        "describe_internet_gateways": {"InternetGateways": []},
    }
    clients = {
        "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": cfg["account_id"]}),
        "cloudformation": SimpleNamespace(describe_stacks=missing, create_stack=write),
        "s3": SimpleNamespace(head_bucket=lambda **_: None, upload_file=write),
        "ssm": SimpleNamespace(
            get_parameter=lambda **_: {"Parameter": {"Type": "SecureString"}},
        ),
        "ec2": SimpleNamespace(
            get_paginator=lambda name: SimpleNamespace(paginate=lambda: [inventory[name]]),
            describe_addresses=lambda: {"Addresses": []},
        ),
        "service-quotas": SimpleNamespace(get_service_quota=lambda **kw: {"Quota": {
            "Value": effective_quota[0], "GlobalQuota": False, "QuotaCode": kw["QuotaCode"],
        }}),
    }
    monkeypatch.setattr(deploy.boto3, "Session",
                        lambda **_: SimpleNamespace(client=clients.__getitem__))
    state = tmp_path / "state.json"
    if operation == "plan_then_apply":
        effective_quota[0] = 10
        monkeypatch.setattr(sys, "argv", [
            "deploy", "plan", "--config", str(path), "--state", str(state),
        ])
        deploy.main()
        assert not state.exists()
        assert not writes
        # Capacity changed after the read-only plan. Apply must not reuse its result.
        effective_quota[0] = 5
        operation = "apply"
    monkeypatch.setattr(sys, "argv", [
        "deploy", operation, "--config", str(path), "--state", str(state),
    ])
    with pytest.raises(RuntimeError, match="vpc"):
        deploy.main()
    assert writes == []
    assert not state.exists()


@pytest.fixture
def world(tmp_path, capacity):
    _, cfg = config(tmp_path)
    data = {
        "vpcs": [{"VpcId": f"vpc-{i}"} for i in range(5)],
        "igws": [{"InternetGatewayId": f"igw-{i}"} for i in range(5)],
        "eips": [{"AllocationId": f"eipalloc-{i}", "PublicIp": f"192.0.2.{i}",
                  "PublicIpv4Pool": "amazon"} for i in range(6)],
        "resources": {},
        "quota": {"L-F678F1CE": 5, "L-A4707A72": 5, "L-0263D0A3": 10},
    }
    calls = []

    def paginate_inventory(name):
        key, output = {
            "describe_vpcs": ("vpcs", "Vpcs"),
            "describe_internet_gateways": ("igws", "InternetGateways"),
        }[name]
        calls.append(name)
        # Two pages force complete inventory accounting.
        return [{output: data[key][:2]}, {output: data[key][2:]}]

    def quota(ServiceCode, QuotaCode):
        calls.append(QuotaCode)
        value = data["quota"][QuotaCode]
        if isinstance(value, Exception):
            raise value
        return {"Quota": {"Value": value, "GlobalQuota": False, "QuotaCode": QuotaCode}}

    def resources(StackName):
        calls.append(("resources", StackName))
        rows = data["resources"].get(StackName, [])
        return [{"StackResourceSummaries": rows[:1]}, {"StackResourceSummaries": rows[1:]}]

    ec2 = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=lambda: paginate_inventory(name)),
        describe_addresses=lambda: {"Addresses": data["eips"]},
    )
    cfn = SimpleNamespace(get_paginator=lambda _: SimpleNamespace(paginate=resources))
    clients = {"ec2": ec2, "service-quotas": SimpleNamespace(get_service_quota=quota)}
    session = SimpleNamespace(client=clients.__getitem__)
    return SimpleNamespace(cfg=cfg, data=data, calls=calls, ec2=ec2, cfn=cfn, session=session)


def check_map(report):
    return {row["resource"]: row for row in report["checks"]}


def test_failed_owned_igw_is_counted_once_and_pending_quota_is_not_capacity(world, capacity):
    world.data["resources"]["owned-host"] = [
        {"LogicalResourceId": "InternetGateway", "PhysicalResourceId": "igw-4",
         "ResourceStatus": "CREATE_FAILED"},
    ]
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, {
        "StackId": "owned-host", "StackStatus": "CREATE_FAILED",
    }, None)
    checks = check_map(report)
    assert checks["vpc"]["used"] == 5
    assert checks["vpc"]["quota"] == 5
    assert checks["vpc"]["needed"] == 1
    assert checks["vpc"]["status"] == "insufficient"
    assert checks["internet_gateway"]["used"] == 5
    assert checks["internet_gateway"]["needed"] == 0
    assert checks["internet_gateway"]["status"] == "not_required"
    assert "L-A4707A72" not in world.calls
    assert checks["amazon_eip"]["needed"] == 2
    with pytest.raises(RuntimeError, match="vpc: used=5 quota=5.0 needed=1"):
        capacity.require_network_capacity(report)
    # An effective quota increase, rather than a requested increase, changes the verdict.
    world.data["quota"]["L-F678F1CE"] = 10
    after = capacity.network_capacity(world.session, world.cfn, world.cfg, {
        "StackId": "owned-host", "StackStatus": "CREATE_FAILED",
    }, None)
    capacity.require_network_capacity(after)
    assert after["status"] == "sufficient"
    assert after["account_id"] == world.cfg["account_id"]
    assert after["region"] == world.cfg["region"]


def test_stale_physical_ids_do_not_hide_new_demand(world, capacity):
    world.data["resources"]["owned-host"] = [
        {"LogicalResourceId": "Vpc", "PhysicalResourceId": "vpc-no-longer-exists",
         "ResourceStatus": "CREATE_FAILED"},
        {"LogicalResourceId": "InternetGateway", "PhysicalResourceId": "igw-no-longer-exists",
         "ResourceStatus": "CREATE_FAILED"},
    ]
    checks = check_map(capacity.network_capacity(world.session, world.cfn, world.cfg, {
        "StackId": "owned-host", "StackStatus": "CREATE_FAILED",
    }, None))
    assert checks["vpc"]["needed"] == 1
    assert checks["internet_gateway"]["needed"] == 1


@pytest.mark.parametrize("entry,allocate,eips", [
    ("direct", "true", 1), ("direct", "false", 0),
    ("public_nlb", "true", 2), ("public_nlb", "false", 1),
])
def test_new_eip_demand_follows_template_conditions(world, capacity, entry, allocate, eips):
    world.cfg["console_entry"] = entry
    world.cfg["parameters"]["AllocateElasticIp"] = allocate
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, None, None)
    assert check_map(report)["amazon_eip"]["needed"] == eips


@pytest.mark.parametrize("physical_id", ["eipalloc-0", "192.0.2.0"])
def test_partial_host_eip_can_be_identified_by_allocation_or_address(world, capacity, physical_id):
    world.data["resources"]["owned-host"] = [
        {"LogicalResourceId": "ElasticIp", "PhysicalResourceId": physical_id,
         "ResourceStatus": "CREATE_COMPLETE"},
    ]
    checks = check_map(capacity.network_capacity(world.session, world.cfn, world.cfg, {
        "StackId": "owned-host", "StackStatus": "CREATE_FAILED",
    }, None))
    assert checks["amazon_eip"]["used"] == 6
    assert checks["amazon_eip"]["needed"] == 1


def test_amazon_eip_quota_excludes_byoip_and_customer_owned_pools(world, capacity):
    world.data["eips"] += [
        {"AllocationId": "byoip", "PublicIpv4Pool": "ipv4pool-ec2-byoip"},
        {"AllocationId": "coip", "CustomerOwnedIp": "192.0.2.80"},
        {"AllocationId": "coip-pool", "CustomerOwnedIpv4Pool": "ipv4pool-coip-test"},
    ]
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, None, None)
    assert check_map(report)["amazon_eip"]["used"] == 6


def test_shared_vpc_does_not_consume_this_accounts_quota(world, capacity):
    world.data["vpcs"] += [{"VpcId": "shared-vpc", "OwnerId": "999999999999"}]
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, None, None)
    assert check_map(report)["vpc"]["used"] == 5


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1, True])
def test_invalid_quota_is_unknown_without_default_fallback(world, capacity, value):
    world.data["quota"]["L-F678F1CE"] = value
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, None, None)
    assert check_map(report)["vpc"]["status"] == "unknown"
    with pytest.raises(RuntimeError, match="GetServiceQuota"):
        capacity.require_network_capacity(report)


def test_missing_permission_blocks_only_needed_capacity(world, capacity):
    world.data["quota"]["L-F678F1CE"] = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not logged"}},
        "GetServiceQuota",
    )
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, None, None)
    row = check_map(report)["vpc"]
    assert row["status"] == "unknown"
    assert "AccessDeniedException" in row["error"]
    assert "not logged" not in row["error"]
    with pytest.raises(RuntimeError, match="AccessDeniedException"):
        capacity.require_network_capacity(report)


def test_inventory_failure_does_not_claim_unused_quota(world, capacity):
    world.ec2.get_paginator = lambda _: (_ for _ in ()).throw(ConnectionError())
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, None, None)
    assert check_map(report)["vpc"]["needed"] is None
    assert check_map(report)["vpc"]["status"] == "unknown"
    assert "L-F678F1CE" not in world.calls


def test_owned_resource_discovery_failure_does_not_guess_remaining_demand(world, capacity):
    world.cfn.get_paginator = lambda _: (_ for _ in ()).throw(PermissionError())
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, {
        "StackId": "owned-host", "StackStatus": "CREATE_FAILED",
    }, None)
    assert check_map(report)["internet_gateway"]["needed"] is None
    assert check_map(report)["internet_gateway"]["status"] == "unknown"


def test_ready_resume_has_no_new_network_capacity_or_quota_dependency(world, capacity):
    def forbidden(*_, **__):
        pytest.fail("ready resume must not require quota or inventory permissions")

    world.session.client = forbidden
    world.cfn.get_paginator = forbidden
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, {
        "StackId": "owned-host", "StackStatus": "UPDATE_COMPLETE",
    }, {"StackId": "owned-nlb", "StackStatus": "CREATE_COMPLETE"})
    assert report["status"] == "not_required"
    assert all(row["needed"] == 0 for row in report["checks"])
    capacity.require_network_capacity(report)


def test_complete_host_needs_only_the_missing_nlb_eip(world, capacity):
    report = capacity.network_capacity(world.session, world.cfn, world.cfg, {
        "StackId": "owned-host", "StackStatus": "CREATE_COMPLETE",
    }, None)
    checks = check_map(report)
    assert checks["vpc"]["needed"] == checks["internet_gateway"]["needed"] == 0
    assert checks["amazon_eip"]["needed"] == 1
    assert world.calls == ["L-0263D0A3"]
