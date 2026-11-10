"""An owned host replacement retargets its NLB and never reuses another instance's SSM job."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_cn_distribution import REPO, config, module


@pytest.fixture
def deploy():
    return module(REPO / "scripts/deploy_cn.py")


@pytest.mark.parametrize("entry", ["direct", "public_nlb"])
@pytest.mark.parametrize("port,valid", [(22, False), (1023, False), (1024, True),
                                       (65535, True), (65536, False), (8443.5, False)])
def test_console_port_matches_both_templates(tmp_path, deploy, entry, port, valid):
    path, cfg = config(tmp_path)
    cfg["console_entry"] = entry
    cfg["parameters"]["ConsolePort"] = port
    path.write_text(json.dumps(cfg))
    if valid:
        assert deploy.read_config(path)["parameters"]["ConsolePort"] == port
    else:
        with pytest.raises(ValueError, match="ConsolePort"):
            deploy.read_config(path)


@pytest.mark.parametrize("entry,valid", [("direct", True), ("public_nlb", False)])
def test_8080_keeps_entry_specific_contract(tmp_path, deploy, entry, valid):
    path, cfg = config(tmp_path)
    cfg["console_entry"] = entry
    cfg["parameters"]["ConsolePort"] = 8080
    path.write_text(json.dumps(cfg))
    if valid:
        deploy.read_config(path)
    else:
        with pytest.raises(ValueError, match="8080"):
            deploy.read_config(path)


@pytest.fixture
def nlb(tmp_path, deploy):
    _, cfg = config(tmp_path)
    state = {"stack_id": "owned-host", "nlb_stack_id": "owned-nlb", "commit": "release"}
    desired = {"VpcId": "vpc-owned", "PublicSubnetId": "subnet-owned",
               "InstanceId": "i-new", "InstanceSecurityGroupId": "sg-new"}
    current = {
        **desired, "InstanceId": "i-old", "InstanceSecurityGroupId": "sg-old",
        "InstancePort": "8443", "ListenerPort": "8443",
        "AccessMode": "OpenToInternet", "NlbSecurityGroupMode": "enabled",
        "AllowedCidrs": "192.0.2.1/32",
    }
    outputs = {"ConsoleUrl": "https://192.0.2.43:8443", "NlbDnsName": "owned-nlb",
               "StaticIp": "192.0.2.43", "TargetGroupArn": "owned-target"}
    stack = {
        "StackId": "owned-nlb", "StackStatus": "CREATE_COMPLETE",
        "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
    }
    calls = []

    def describe(**_):
        stack["Parameters"] = [{"ParameterKey": k, "ParameterValue": v}
                               for k, v in current.items()]
        return {"Stacks": [stack]}

    def resource(StackName, LogicalResourceId):
        assert StackName == "owned-host"
        key = {"Vpc": "VpcId", "PublicSubnet": "PublicSubnetId", "Instance": "InstanceId",
               "ConsoleSecurityGroup": "InstanceSecurityGroupId"}[LogicalResourceId]
        return {"StackResourceDetail": {"PhysicalResourceId": desired[key]}}

    def update(**kwargs):
        assert kwargs["StackName"] == "owned-nlb"
        assert kwargs["UsePreviousTemplate"] is True
        calls.append(kwargs)
        for param in kwargs["Parameters"]:
            if "ParameterValue" in param:
                current[param["ParameterKey"]] = param["ParameterValue"]
        stack["StackStatus"] = "UPDATE_COMPLETE"
        return {"StackId": "owned-nlb"}

    cfn = SimpleNamespace(describe_stacks=describe, describe_stack_resource=resource,
                          update_stack=update)
    return SimpleNamespace(
        cfg=cfg, state=state, desired=desired, current=current, calls=calls, stack=stack, cfn=cfn,
        host={"InstanceId": "i-new", "ConsoleUrl": "https://192.0.2.42:8443"},
        path=tmp_path / "state.json",
    )


def test_owned_nlb_retargets_only_instance_and_host_sg(nlb, deploy):
    output = deploy.ensure_console_entry(nlb.cfn, nlb.cfg, nlb.host, nlb.state, nlb.path)
    assert output["ConsoleUrl"] == "https://192.0.2.43:8443"
    assert len(nlb.calls) == 1
    updated = {p["ParameterKey"]: p["ParameterValue"] for p in nlb.calls[0]["Parameters"]
               if "ParameterValue" in p}
    assert updated == {"InstanceId": "i-new", "InstanceSecurityGroupId": "sg-new"}
    assert all(p.get("UsePreviousValue") is True for p in nlb.calls[0]["Parameters"]
               if p["ParameterKey"] not in updated)
    assert nlb.state["nlb_update_attempts"][0]["previous_parameters"]["InstanceId"] == "i-old"
    deploy.ensure_console_entry(nlb.cfn, nlb.cfg, nlb.host, nlb.state, nlb.path)
    assert len(nlb.calls) == 1


def test_nlb_update_lost_response_resumes_same_stack(nlb, deploy):
    update = nlb.cfn.update_stack

    def lost(**kwargs):
        update(**kwargs)
        raise ConnectionError("response lost")

    nlb.cfn.update_stack = lost
    with pytest.raises(ConnectionError):
        deploy.ensure_console_entry(nlb.cfn, nlb.cfg, nlb.host, nlb.state, nlb.path)
    assert len(nlb.state["nlb_update_attempts"]) == 1
    deploy.ensure_console_entry(nlb.cfn, nlb.cfg, nlb.host, nlb.state, nlb.path)
    assert len(nlb.calls) == 1
    assert nlb.state["nlb_update_attempts"][0]["status"] == "UPDATE_COMPLETE"


@pytest.mark.parametrize("key,value", [
    ("VpcId", "another-vpc"), ("PublicSubnetId", "another-subnet"), ("InstancePort", "9443"),
    ("ListenerPort", "9443"), ("AccessMode", "AllowedCidrs"), ("NlbSecurityGroupMode", "disabled"),
])
def test_nlb_does_not_reconcile_network_or_access_changes(nlb, deploy, key, value):
    nlb.current[key] = value
    with pytest.raises(RuntimeError, match="network/access"):
        deploy.ensure_console_entry(nlb.cfn, nlb.cfg, nlb.host, nlb.state, nlb.path)
    assert not nlb.calls


def test_foreign_nlb_is_never_updated(nlb, deploy):
    nlb.stack["StackId"] = "foreign-nlb"
    with pytest.raises(RuntimeError, match="not owned"):
        deploy.ensure_console_entry(nlb.cfn, nlb.cfg, nlb.host, nlb.state, nlb.path)
    assert not nlb.calls


def test_nlb_rollback_is_not_success(nlb, deploy):
    update = nlb.cfn.update_stack

    def rollback(**kwargs):
        update(**kwargs)
        nlb.stack["StackStatus"] = "UPDATE_ROLLBACK_COMPLETE"

    nlb.cfn.update_stack = rollback
    with pytest.raises(RuntimeError, match="UPDATE_ROLLBACK_COMPLETE"):
        deploy.ensure_console_entry(nlb.cfn, nlb.cfg, nlb.host, nlb.state, nlb.path)
    assert "nlb_outputs" not in nlb.state


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_replacement_settles_original_target_then_preserves_evidence(
    tmp_path, deploy, monkeypatch, fresh, legacy,
):
    key = "verification_command" if fresh else "policy_command"
    context = {"command_id": "old-command", "instance_id": "i-old",
               "script_sha256": hashlib.sha256(b"install").hexdigest()}
    state = {key: "old-command", "outputs": {"InstanceId": "i-new"}}
    if not legacy:
        state[f"{key}_context"] = context
    statuses = iter(["InProgress", "Cancelling", "Success"])
    events = []

    def invocation(CommandId, InstanceId):
        events.append(("poll", CommandId, InstanceId))
        if CommandId == "old-command":
            assert InstanceId == "i-old"
            return {"CommandId": CommandId, "Status": next(statuses),
                    "StandardOutputContent": "old evidence"}
        assert InstanceId == "i-new"
        return {
            "CommandId": CommandId, "Status": "Success", "StandardOutputContent": "new evidence",
        }

    def send(**kwargs):
        assert kwargs["InstanceIds"] == ["i-new"]
        assert len(events) == 3
        events.append(("send", "new-command", "i-new"))
        return {"Command": {"CommandId": "new-command"}}

    ssm = SimpleNamespace(
        send_command=send, get_command_invocation=invocation,
        list_commands=lambda **_: {"Commands": [{
            "CommandId": "old-command", "DocumentName": "AWS-RunShellScript",
            "InstanceIds": ["i-old"], "Parameters": {"commands": ["install"]},
        }]},
    )
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    result = deploy.run_ssm(ssm, "i-new", "install", state, tmp_path / "state.json", fresh=fresh)
    assert result["CommandId"] == "new-command"
    assert state[f"{key}_context"]["instance_id"] == "i-new"
    assert [row["instance_id"] for row in state["command_history"]] == ["i-old", "i-new"]
    evidence = [json.loads(Path(row["evidence"]).read_text()) for row in state["command_history"]]
    assert [row["StandardOutputContent"] for row in evidence] == ["old evidence", "new evidence"]


@pytest.mark.parametrize("commands", [
    [], [{"CommandId": "old"}],
    [{"CommandId": "old", "DocumentName": "AWS-RunShellScript",
      "InstanceIds": ["i-old", "i-other"], "Parameters": {"commands": ["check"]}}],
])
def test_legacy_command_provenance_cannot_be_guessed(tmp_path, deploy, commands):
    ssm = SimpleNamespace(
        list_commands=lambda **_: {"Commands": commands},
        send_command=lambda **_: pytest.fail("ambiguous old command must not trigger an install"),
    )
    with pytest.raises(RuntimeError, match="target"):
        deploy.run_ssm(ssm, "i-new", "check", {"policy_command": "old"}, tmp_path / "state.json")


def test_same_instance_changed_script_runs_new_install_after_old_finishes(tmp_path, deploy):
    state = {
        "policy_command": "old",
        "policy_command_context": {"command_id": "old", "instance_id": "i-current",
                                   "script_sha256": hashlib.sha256(b"old script").hexdigest()},
    }
    events = []
    ssm = SimpleNamespace(
        get_command_invocation=lambda **kw: events.append(("poll", kw["CommandId"])) or {
            "CommandId": kw["CommandId"], "Status": "Success",
        },
        send_command=lambda **_: events.append(("send", "new")) or {
            "Command": {"CommandId": "new"},
        },
    )
    deploy.run_ssm(ssm, "i-current", "new script", state, tmp_path / "state.json")
    assert events == [("poll", "old"), ("send", "new"), ("poll", "new")]


@pytest.mark.parametrize("fresh", [False, True])
def test_pending_old_instance_command_blocks_replacement_attempt(
    tmp_path, deploy, monkeypatch, fresh,
):
    key = "verification_command" if fresh else "policy_command"
    state = {
        key: "old",
        f"{key}_context": {"command_id": "old", "instance_id": "i-old",
                          "script_sha256": hashlib.sha256(b"check").hexdigest()},
    }
    seen = []

    def pending(ssm, instance, cid):
        seen.append((instance, cid))
        raise TimeoutError("still pending")

    monkeypatch.setattr(deploy, "wait_ssm", pending)
    ssm = SimpleNamespace(send_command=lambda **_: pytest.fail("must settle original first"))
    with pytest.raises(TimeoutError):
        deploy.run_ssm(ssm, "i-new", "check", state, tmp_path / "state.json", fresh=fresh)
    assert seen == [("i-old", "old")]
    assert state[key] == "old"


@pytest.mark.parametrize("key,value,rejected", [
    ("EnvName", "old", True), ("MemoryDomainMode", "shared", True),
    ("GitBranch", "different-release", True), ("ConsolePort", "9443", True),
    ("InstanceType", "m7g.2xlarge", False), ("AmiId", "ami-replacement", False),
])
def test_host_repair_cannot_retarget_deployment_identity(tmp_path, deploy, key, value, rejected):
    _, cfg = config(tmp_path)
    params = {p["ParameterKey"]: p["ParameterValue"] for p in deploy.parameters(cfg)}
    params[key] = value
    stack = {"Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()]}
    if rejected:
        with pytest.raises(RuntimeError, match=key):
            deploy.validate_host_resume(cfg, stack)
    else:
        deploy.validate_host_resume(cfg, stack)
