"""Public entry provisioning and fail-closed acceptance without real AWS calls."""
import hashlib
import json
import shlex
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from tests.test_cn_distribution import REPO, config, module


@pytest.fixture
def deploy():
    return module(REPO / "scripts/deploy_cn.py")


def test_legacy_config_keeps_direct_entry_and_fingerprint(tmp_path, deploy):
    path, cfg = config(tmp_path)
    cfg.pop("console_entry")
    path.write_text(json.dumps(cfg))
    read = deploy.read_config(path)
    assert "console_entry" not in read
    outputs = {"ConsoleUrl": "https://192.0.2.10:8443"}
    assert deploy.ensure_console_entry(None, read, outputs, {}, tmp_path / "s") == outputs


@pytest.mark.parametrize("key,value", [
    ("console_entry", "typo"), ("MemoryDomainMode", "typo"), ("ConsolePort", "443"),
    ("AuthPasswordPolicy", "typo"), ("AuthPasswordMinLength", 5),
    ("AuthPasswordMinLength", 8.5),
])
def test_invalid_deployment_choice_fails_early(tmp_path, deploy, key, value):
    path, cfg = config(tmp_path)
    (cfg if key == "console_entry" else cfg["parameters"])[key] = value
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        deploy.read_config(path)


@pytest.mark.parametrize("http_lab", [False, True])
def test_create_resume_and_recover_lost_nlb_response(tmp_path, deploy, monkeypatch, http_lab):
    _, cfg = config(tmp_path)
    if http_lab:
        cfg.update(console_http_lab=True, http_port=8088)
    state = {"stack_id": "host-stack-id", "commit": "a" * 40}
    path = tmp_path / "state.json"
    host = {"InstanceId": "i-test", "ConsoleUrl": "https://192.0.2.10:8443"}
    outputs = {
        "ConsoleUrl": "https://192.0.2.11:8443", "StaticIp": "192.0.2.11",
        "NlbDnsName": "nlb.example", "TargetGroupArn": "target-group",
    }
    if http_lab:
        outputs["ConsoleUrl"] = "http://192.0.2.11:8088"
    created = []
    stack = None

    def create(**kwargs):
        nonlocal stack
        assert json.loads(path.read_text())["nlb_create_token"] == kwargs["ClientRequestToken"]
        created.append(kwargs)
        stack = {
            "StackId": "nlb-stack-id", "Tags": kwargs["Tags"], "StackStatus": "CREATE_COMPLETE",
            "Parameters": kwargs["Parameters"],
            "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
        }
        return {"StackId": stack["StackId"]}

    def resource(**kwargs):
        assert kwargs["StackName"] == "host-stack-id"
        return {"StackResourceDetail": {"PhysicalResourceId": {
            "Vpc": "vpc-test", "PublicSubnet": "subnet-test", "Instance": "i-test",
            "ConsoleSecurityGroup": "sg-host",
        }[kwargs["LogicalResourceId"]]}}

    cfn = SimpleNamespace(create_stack=create, describe_stack_resource=resource)
    monkeypatch.setattr(deploy, "stack_or_none", lambda *a: stack)
    monkeypatch.setattr(deploy, "wait_stack", lambda *a: stack)
    public = deploy.ensure_console_entry(cfn, cfg, host, state, path)
    params = {p["ParameterKey"]: p["ParameterValue"] for p in created[0]["Parameters"]}
    assert params["InstanceId"] == "i-test"
    assert params["AccessMode"] == "OpenToInternet"
    assert params["NlbSecurityGroupMode"] == "enabled"
    if http_lab:
        assert params["ConsoleHttpLab"] == "true"
        assert params["HttpPort"] == "8088"
    assert public["ConsoleUrl"] == outputs["ConsoleUrl"]
    assert public["DirectConsoleUrl"] == host["ConsoleUrl"]
    assert deploy.ensure_console_entry(cfn, cfg, host, state, path) == public
    assert len(created) == 1
    # If a process dies after AWS accepted creation but before StackId was saved,
    # only this persisted token + host ownership can recover the created stack.
    state.pop("nlb_stack_id")
    assert deploy.ensure_console_entry(cfn, cfg, host, state, path) == public
    assert len(created) == 1
    with pytest.raises(RuntimeError, match="not owned"):
        deploy.owned_nlb(cfn, cfg, {})
    with pytest.raises(RuntimeError, match="not owned"):
        deploy.owned_nlb(cfn, cfg, dict(state, nlb_stack_id="another-stack"))
    stack = None
    with pytest.raises(RuntimeError, match="missing"):
        deploy.owned_nlb(cfn, cfg, state)


def test_nlb_certificate_keeps_direct_ip(tmp_path, deploy):
    _, cfg = config(tmp_path)
    outputs = {
        "ConsoleUrl": "https://192.0.2.11:8443",
        "DirectConsoleUrl": "https://192.0.2.10:8443",
        "PolicyPrincipalKeyArn": "key", "OpsRoleName": "role", "PrivateIp": "10.0.0.1",
    }
    command = shlex.split(deploy.policy_script(cfg, outputs, {
        "artifact_uri": "s3://bucket/key", "artifact_sha256": "a" * 64, "commit": "b" * 40,
    }))
    assert command[-1] == "192.0.2.10"
    assert json.loads(command[-2])["public_base_url"] == outputs["ConsoleUrl"]


@pytest.mark.parametrize("target_health,protected_status,expected", [
    ("healthy", 401, None),
    ("unhealthy", 401, TimeoutError),
    ("healthy", 200, RuntimeError),
])
def test_public_acceptance_checks_target_and_auth(
    tmp_path, deploy, monkeypatch, target_health, protected_status, expected,
):
    _, cfg = config(tmp_path)
    target = {"Id": "i-test", "Port": 8443}
    elb = SimpleNamespace(describe_target_health=lambda **kw: {
        "TargetHealthDescriptions": [{
            "Target": target, "TargetHealth": {"State": target_health},
        }],
    })
    now = [0]
    monkeypatch.setattr(deploy.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(deploy.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            return SimpleNamespace(
                status_code=200 if url.endswith("/health") else protected_status,
                json=lambda: {"status": "ok"},
            )

    monkeypatch.setattr(deploy.httpx, "Client", lambda **kw: Client())
    args = (elb, cfg, {"InstanceId": "i-test", "ConsoleUrl": "https://192.0.2.11:8443"},
            {"nlb_outputs": {"TargetGroupArn": "group"}})
    if expected:
        with pytest.raises(expected):
            deploy.verify_console_entry(*args, timeout=20)
    else:
        assert deploy.verify_console_entry(*args, timeout=20)["unauthenticated_status"] == 401


def test_host_recovers_only_its_persisted_create_token(tmp_path, deploy, monkeypatch):
    _, cfg = config(tmp_path)
    path = tmp_path / "state.json"
    state = {"config_sha256": "config-fingerprint", "commit": "a" * 40}
    stack = None
    calls = []

    def create(**kwargs):
        nonlocal stack
        persisted = json.loads(path.read_text())
        assert persisted["host_create_token"] == kwargs["ClientRequestToken"]
        calls.append(kwargs)
        stack = {"StackId": "host-stack", "Tags": kwargs["Tags"]}
        raise ConnectionError("create response lost")

    cfn = SimpleNamespace(create_stack=create)
    monkeypatch.setattr(deploy, "stack_or_none", lambda *_: stack)
    with pytest.raises(ConnectionError):
        deploy.ensure_host_stack(cfn, cfg, state, path, "https://template")
    assert "stack_id" not in state
    saved = json.loads(path.read_text())
    for wrong in ({}, dict(saved, config_sha256="other"), dict(saved, commit="other"),
                  dict(saved, host_create_token="other"), dict(saved, stack_id="another")):
        with pytest.raises(RuntimeError, match="not owned"):
            deploy.owned_host(cfn, cfg, wrong)
    assert deploy.ensure_host_stack(cfn, cfg, saved, path, "https://template") == "host-stack"
    assert len(calls) == 1
    # Older state files with a saved StackId remain valid without the new tags/token.
    assert deploy.owned_host(cfn, cfg, {"stack_id": "host-stack"}) == stack
    stack = None
    with pytest.raises(RuntimeError, match="missing"):
        deploy.owned_host(cfn, cfg, saved)


@pytest.mark.parametrize("status,success", [
    ("CREATE_COMPLETE", True), ("UPDATE_COMPLETE", True), ("CREATE_FAILED", False),
    ("UPDATE_ROLLBACK_COMPLETE", False), ("UPDATE_ROLLBACK_FAILED", False),
    ("ROLLBACK_COMPLETE", False), ("DELETE_COMPLETE", False),
])
def test_wait_stack_handles_repaired_and_failed_states(deploy, status, success):
    stack = {"StackStatus": status}
    cfn = SimpleNamespace(describe_stacks=lambda **_: {"Stacks": [stack]})
    if success:
        assert deploy.wait_stack(cfn, "owned-stack") == stack
    else:
        with pytest.raises(RuntimeError, match=status):
            deploy.wait_stack(cfn, "owned-stack")


def test_fresh_verifier_settles_pending_command_and_never_reuses_success(
    tmp_path, deploy, monkeypatch,
):
    path = tmp_path / "state.json"
    state = {
        "verification_command": "old", "policy_command": "install",
        "verification_command_context": {
            "command_id": "old", "instance_id": "i-new",
            "script_sha256": hashlib.sha256(b"check").hexdigest(),
        },
    }
    old_results = iter(["InProgress", "Cancelling", "Success"])
    events = []
    sent = []

    def invocation(CommandId, **_):
        events.append(CommandId)
        status = next(old_results) if CommandId == "old" else "Success"
        return {"CommandId": CommandId, "Status": status}

    def send(**kwargs):
        sent.append(kwargs)
        return {"Command": {"CommandId": f"fresh-{len(sent)}"}}

    ssm = SimpleNamespace(send_command=send, get_command_invocation=invocation)
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    result = deploy.run_ssm(ssm, "i-new", "check", state, path, fresh=True)
    assert result["CommandId"] == "fresh-1"
    assert events == ["old", "old", "old", "fresh-1"]
    assert state["policy_command"] == "install"
    result = deploy.run_ssm(ssm, "i-new", "check", state, path, fresh=True)
    assert result["CommandId"] == "fresh-2"
    assert events[-2:] == ["fresh-1", "fresh-2"]
    assert len(sent) == 2
    evidence = json.loads((tmp_path / "deployment-verification.json").read_text())
    assert evidence["Status"] == "Success"


def test_pending_verifier_timeout_does_not_launch_another(tmp_path, deploy, monkeypatch):
    state = {
        "verification_command": "old",
        "verification_command_context": {
            "command_id": "old", "instance_id": "i-new",
            "script_sha256": hashlib.sha256(b"check").hexdigest(),
        },
    }
    monkeypatch.setattr(deploy, "wait_ssm", lambda *_: (_ for _ in ()).throw(TimeoutError()))
    ssm = SimpleNamespace(send_command=lambda **_: pytest.fail("overlapping verification"))
    with pytest.raises(TimeoutError):
        deploy.run_ssm(ssm, "i-new", "check", state, tmp_path / "state.json", fresh=True)
    assert state["verification_command"] == "old"


@pytest.mark.parametrize("mode,error_code,missing,fallback", [
    ("shared", "ValidationError", True, True),
    ("dedicated", "ValidationError", True, False),
    ("shared", "AccessDenied", True, False),
    ("shared", "ValidationError", False, False),
])
def test_operator_domain_fallback_is_only_for_absent_legacy_stack(
    tmp_path, deploy, mode, error_code, missing, fallback,
):
    _, cfg = config(tmp_path)
    cfg["parameters"]["MemoryDomainMode"] = mode
    calls = []

    def describe(StackName):
        calls.append(StackName)
        if StackName == f"launchpad-memory-{cfg['environment']}":
            return {"Stacks": [{"Outputs": [{"OutputKey": "MemoryDomainEndpoint",
                                             "OutputValue": "legacy-domain"}]}]}
        raise ClientError({"Error": {
            "Code": error_code, "Message": "does not exist" if missing else "invalid request",
        }}, "DescribeStacks")

    cfn = SimpleNamespace(describe_stacks=describe)
    if fallback:
        assert deploy.selected_domain_outputs(cfn, cfg)["MemoryDomainEndpoint"] == "legacy-domain"
        assert len(calls) == 2
    else:
        with pytest.raises((ClientError, RuntimeError)):
            deploy.selected_domain_outputs(cfn, cfg)
        assert len(calls) == 1


def test_new_deployment_and_resume_use_new_environment_outputs(tmp_path, deploy, monkeypatch):
    """Run the orchestration boundary twice, with every AWS operation stubbed."""
    path, cfg = config(tmp_path)
    cfg["environment"] = "n42"
    cfg["stack_name"] = "launchpad-cn-ec2-n42"
    artifact = tmp_path / "engine.tar.gz"
    artifact.write_bytes(b"test engine")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix(".gz.sha256").write_text(digest)
    cfg["engine_artifact"] = str(artifact)
    path.write_text(json.dumps(cfg))
    commit = "a" * 40
    monkeypatch.setattr(deploy, "resolve_commit", lambda _: commit)
    monkeypatch.setattr(deploy.subprocess, "run", lambda *_, **__: SimpleNamespace(stdout=commit))
    stacks = {}
    created = []
    commands = []
    command_targets = {}
    updates = []
    uploads = []
    host_outputs = {
        "InstanceId": "i-new", "ConsoleUrl": "https://192.0.2.42:8443",
        "PolicyPrincipalKeyArn": "new-key", "OpsRoleName": "new-role", "PrivateIp": "10.44.0.42",
    }
    nlb_outputs = {
        "ConsoleUrl": "https://192.0.2.43:8443", "StaticIp": "192.0.2.43",
        "NlbDnsName": "new-nlb", "TargetGroupArn": "new-target",
    }

    def stack(outputs, **extra):
        return {"StackStatus": "CREATE_COMPLETE", "Outputs": [
            {"OutputKey": k, "OutputValue": v} for k, v in outputs.items()
        ], **extra}

    def describe(StackName):
        if StackName not in stacks:
            raise ClientError({"Error": {"Code": "ValidationError", "Message": "does not exist"}},
                              "DescribeStacks")
        return {"Stacks": [stacks[StackName]]}

    def create(StackName, **kwargs):
        created.append(StackName)
        is_host = StackName == cfg["stack_name"]
        outputs = host_outputs if is_host else nlb_outputs
        stacks[StackName] = stack(
            outputs, StackId=StackName, Tags=kwargs["Tags"], Parameters=kwargs["Parameters"],
        )
        if is_host:
            stacks["launchpad-base-n42"] = stack({"ArtifactsBucketName": "new-artifacts"})
            stacks["launchpad-memory-domain-n42"] = stack({
                "MemoryDomainEndpoint": "new-domain", "MemoryDomainArn": "new-domain-arn",
            })
        return {"StackId": StackName}

    def resource(**kwargs):
        return {"StackResourceDetail": {"PhysicalResourceId": {
            "Vpc": "new-vpc", "PublicSubnet": "new-subnet", "Instance": host_outputs["InstanceId"],
            "ConsoleSecurityGroup": "new-sg",
        }[kwargs["LogicalResourceId"]]}}

    def send(**kwargs):
        assert kwargs["InstanceIds"] == [host_outputs["InstanceId"]]
        commands.append(kwargs["Parameters"]["commands"][0])
        command_targets[str(len(commands))] = kwargs["InstanceIds"][0]
        return {"Command": {"CommandId": str(len(commands))}}

    def invocation(CommandId, InstanceId):
        assert InstanceId == command_targets[CommandId]
        return {"CommandId": CommandId, "Status": "Success"}

    def update(StackName, Parameters, **kwargs):
        assert StackName == "launchpad-cn-console-nlb-n42"
        assert kwargs["UsePreviousTemplate"] is True
        updates.append(Parameters)
        target_stack = stacks[StackName]
        old = {p["ParameterKey"]: p["ParameterValue"] for p in target_stack["Parameters"]}
        old.update({p["ParameterKey"]: p["ParameterValue"] for p in Parameters
                    if "ParameterValue" in p})
        target_stack["Parameters"] = [{"ParameterKey": k, "ParameterValue": v}
                                      for k, v in old.items()]
        target_stack["StackStatus"] = "UPDATE_COMPLETE"
        return {"StackId": StackName}

    clients = {
        "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": cfg["account_id"]}),
        "cloudformation": SimpleNamespace(
            describe_stacks=describe, create_stack=create, describe_stack_resource=resource,
            update_stack=update,
        ),
        "s3": SimpleNamespace(
            head_bucket=lambda **_: None, upload_file=lambda *a, **kw: uploads.append(a),
        ),
        "ssm": SimpleNamespace(
            get_parameter=lambda **_: {"Parameter": {"Type": "SecureString"}},
            send_command=send, get_command_invocation=invocation,
        ),
        "elbv2": object(),
        "ec2": SimpleNamespace(
            get_paginator=lambda name: SimpleNamespace(paginate=lambda: [{
                "Vpcs" if name == "describe_vpcs" else "InternetGateways": [],
            }]),
            describe_addresses=lambda: {"Addresses": []},
        ),
        "service-quotas": SimpleNamespace(get_service_quota=lambda **_: {
            "Quota": {"Value": 100, "GlobalQuota": False},
        }),
    }
    monkeypatch.setattr(deploy.boto3, "Session",
                        lambda **_: SimpleNamespace(client=clients.__getitem__))
    monkeypatch.setattr(deploy, "verify_console_entry", lambda _, cfg, outputs, state: (
        {"health": "ok"} if outputs["ConsoleUrl"] == nlb_outputs["ConsoleUrl"]
        else pytest.fail("wrong public URL")
    ))
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(sys, "argv", [
        "deploy", "apply", "--config", str(path), "--state", str(state_path),
    ])
    deploy.main()
    deploy.main()
    assert created == [cfg["stack_name"], "launchpad-cn-console-nlb-n42"]
    assert len(commands) == 3  # One installation, fresh verification on both applies.
    install_args = shlex.split(commands[0])
    assert install_args[-1] == "192.0.2.42"
    assert json.loads(install_args[-2])["public_base_url"] == nlb_outputs["ConsoleUrl"]
    for command in commands[1:]:
        expected = json.loads(shlex.split(command)[-1])
        assert expected["env_name"] == "n42"
        assert expected["memory_domain_mode"] == "dedicated"
        assert expected["auth_password_policy"] == "relaxed"
        assert expected["auth_password_min_length"] == 8
        assert expected["public_base_url"] == nlb_outputs["ConsoleUrl"]
        assert expected["direct_console_url"] == host_outputs["ConsoleUrl"]
        assert expected["account_id"] == cfg["account_id"]
        assert expected["policy_gateway_interceptor"] is True
        assert expected["policy_principal_kms_key_id"] == "new-key"
        assert expected["policy_hub_role_arn"].endswith(":role/new-role")
        assert expected["policy_engine_endpoints"] == ["10.44.0.42:7443", "10.44.0.42:7444"]
        assert "uv run" not in command
    state = json.loads(state_path.read_text())
    assert state["status"] == "platform_ready"
    assert state["outputs"]["MemoryDomainEndpoint"] == "new-domain"
    assert state["artifact_uri"].startswith("s3://new-artifacts/")
    # A legitimate host repair replaces EC2, without changing deployment identity.
    host_outputs.update(InstanceId="i-replacement", PrivateIp="10.44.0.99")
    stacks[cfg["stack_name"]]["Outputs"] = [
        {"OutputKey": k, "OutputValue": v} for k, v in host_outputs.items()
    ]
    stacks[cfg["stack_name"]]["StackStatus"] = "UPDATE_COMPLETE"
    deploy.main()
    state = json.loads(state_path.read_text())
    assert state["status"] == "platform_ready"
    assert state["policy_command_context"]["instance_id"] == "i-replacement"
    assert state["verification_command_context"]["instance_id"] == "i-replacement"
    assert len(commands) == 5  # Exactly one replacement install and one fresh check.
    assert len(updates) == 1
    assert len(created) == 2
    assert [row["instance_id"] for row in state["command_history"]] == [
        "i-new", "i-new", "i-new", "i-replacement", "i-replacement",
    ]
    replacement = json.loads(shlex.split(commands[-1])[-1])
    assert replacement["policy_engine_endpoints"] == ["10.44.0.99:7443", "10.44.0.99:7444"]
