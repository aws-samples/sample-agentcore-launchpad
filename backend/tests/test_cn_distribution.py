"""Distribution boundaries: wrong accounts/state must fail before AWS writes."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]


def module(path):
    spec = importlib.util.spec_from_file_location("distribution_under_test", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def config(tmp_path):
    cfg = json.loads((REPO / "config/deploy-cn.example.json").read_text())
    cfg.update(account_id="123456789012", release_bucket="private-test-release")
    cfg["parameters"]["AllowedCidr"] = "192.0.2.1/32"
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps(cfg))
    return path, cfg


@pytest.mark.parametrize("field,value", [
    ("account_id", "YOUR_ACCOUNT"), ("environment", ""), ("environment", "kb"),
    ("region", "us-west-2"), ("environment", "workshop"),
])
def test_deploy_rejects_ambiguous_target(tmp_path, field, value):
    path, cfg = config(tmp_path)
    cfg[field] = value
    path.write_text(json.dumps(cfg))
    deploy = module(REPO / "scripts/deploy_cn.py")
    with pytest.raises(ValueError):
        deploy.read_config(path)


def test_deploy_wrong_account_stops_before_other_clients(tmp_path, monkeypatch):
    path, _ = config(tmp_path)
    deploy = module(REPO / "scripts/deploy_cn.py")
    clients = []

    def client(name):
        clients.append(name)
        assert name == "sts"
        return SimpleNamespace(get_caller_identity=lambda: {"Account": "999999999999"})

    monkeypatch.setattr(deploy.boto3, "Session", lambda **_: SimpleNamespace(client=client))
    monkeypatch.setattr(sys, "argv", [
        "deploy", "apply", "--config", str(path), "--state", str(tmp_path / "state.json"),
    ])
    with pytest.raises(RuntimeError, match="account does not match"):
        deploy.main()
    assert clients == ["sts"]
    assert not (tmp_path / "state.json").exists()


def test_policy_install_command_has_no_secret_and_quotes_settings(tmp_path):
    import shlex

    deploy = module(REPO / "scripts/deploy_cn.py")
    _, cfg = config(tmp_path)
    outputs = {"ConsoleUrl": "https://192.0.2.10:8443",
               "PolicyPrincipalKeyArn": "arn:aws-cn:kms:cn-northwest-1:123456789012:key/x",
               "OpsRoleName": "launchpad-test", "PrivateIp": "10.0.0.1"}
    state = {"artifact_uri": "s3://private/key", "artifact_sha256": "a" * 64, "commit": "b" * 40}
    command = shlex.split(deploy.policy_script(cfg, outputs, state))
    assert len(command) == 7
    settings = json.loads(command[-1])
    assert settings["public_base_url"] == "https://192.0.2.10:8443"
    assert settings["policy_engine_endpoints"] == ["10.0.0.1:7443", "10.0.0.1:7444"]
    assert "password" not in deploy.policy_script(cfg, outputs, state)


def lab_module(tmp_path, monkeypatch):
    import boto3

    cfg = json.loads((REPO / "samples/itsm-cn/config.example.json").read_text())
    cfg.update(account_id="123456789012", tools_host="i-1234567890abcdef0",
               api_base="https://console.example.org", console="https://console.example.org",
               state_dir="run", profile=None)
    path = tmp_path / "lab.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("LAUNCHPAD_LAB_CONFIG", str(path))
    monkeypatch.setattr(boto3, "Session", lambda **_: SimpleNamespace(client=lambda _: object()))
    return module(REPO / "samples/itsm-cn/scripts/lab.py")


def test_lab_manifest_cannot_be_reused_for_different_config(tmp_path, monkeypatch):
    lab = lab_module(tmp_path, monkeypatch)
    lab.ROOT.mkdir()
    state = lab.load()
    lab.save(state)
    assert lab.load()["scenario"] == lab.SCENARIO
    state["config_sha256"] = "another-account-config"
    lab.save(state)
    with pytest.raises(RuntimeError, match="another config"):
        lab.load()


def test_lab_has_no_implicit_live_target(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_LAB_CONFIG", raising=False)
    with pytest.raises(RuntimeError, match="no default live target"):
        module(REPO / "samples/itsm-cn/scripts/lab.py")


def test_lab_refuses_plain_http_credentials(tmp_path, monkeypatch):
    lab = lab_module(tmp_path, monkeypatch)
    cfg = dict(lab.CONFIG, api_base="http://console.example.org")
    path = tmp_path / "http.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="HTTPS"):
        lab.read_config(path)


def test_lab_grants_every_publish_target_on_role_and_s3_endpoint(tmp_path, monkeypatch):
    """N17-obs525: a per-staff publish target needs the Lambda role's inline policy AND the S3
    Gateway VPC endpoint policy; the endpoint is modified only when its live grant differs."""
    lab = lab_module(tmp_path, monkeypatch)
    monkeypatch.setattr(lab, "STAFF", ["itdesk01", "desk-two"])
    monkeypatch.setattr(lab, "STAFF_PUBLISH_TARGETS",
                        {"desk-two": "s3://lab-kb/students/lab02/"})
    shared = "s3://lab-kb/shared/"
    targets = lab.publish_targets(shared)
    assert targets == ["s3://lab-kb/shared/", "s3://lab-kb/students/lab02/"]
    calls: list[tuple] = []
    live = {"policy": json.dumps(lab.publish_endpoint_policy_document([shared]))}

    class Iam:
        def put_role_policy(self, **kw):
            calls.append(("iam", kw["RoleName"], kw["PolicyName"],
                          json.loads(kw["PolicyDocument"])))

    class Ec2:
        def describe_vpc_endpoints(self, **kw):
            return {"VpcEndpoints": [{"VpcEndpointId": kw["VpcEndpointIds"][0],
                                      "PolicyDocument": live["policy"]}]}

        def modify_vpc_endpoint(self, **kw):
            calls.append(("ec2", kw["VpcEndpointId"], json.loads(kw["PolicyDocument"])))
            live["policy"] = kw["PolicyDocument"]

    clients = {"iam": Iam(), "ec2": Ec2()}
    monkeypatch.setattr(lab, "AWS", SimpleNamespace(client=lambda name: clients[name]))
    manifest = {"roles": {"lambda": "arn:aws-cn:iam::123456789012:role/launchpad-itsm-lab-lambda"},
                "lambda_s3_endpoint": "vpce-0123456789abcdef0"}
    grant = lab.grant_publish_targets(manifest, targets)
    assert grant == {"role": "launchpad-itsm-lab-lambda", "endpoint": "vpce-0123456789abcdef0",
                     "endpoint_changed": True, "targets": targets}
    (_, role, name, role_doc), (_, endpoint, endpoint_doc) = calls
    assert (role, name) == ("launchpad-itsm-lab-lambda", "PublishOnlyWorkshopKnowledge")
    [st] = role_doc["Statement"]
    assert (st["Effect"], st["Action"]) == ("Allow", "s3:PutObject")
    assert st["Resource"] == ["arn:aws-cn:s3:::lab-kb/shared/*",
                              "arn:aws-cn:s3:::lab-kb/students/lab02/*"]
    [est] = endpoint_doc["Statement"]
    assert est["Principal"] == "*" and est["Resource"] == st["Resource"]
    # the same set again: the role policy is re-put (idempotent), the endpoint left alone —
    # even when AWS hands the document back with a single-string Resource
    live["policy"] = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": "*", "Action": ["s3:PutObject"],
         "Resource": list(reversed(st["Resource"]))}]})
    calls.clear()
    again = lab.grant_publish_targets(manifest, targets)
    assert again["endpoint_changed"] is False and [c[0] for c in calls] == ["iam"]
    # no endpoint in the manifest (an environment without prepare_network.py): role only, the
    # role name derived from NAME when the manifest has no roles
    calls.clear()
    bare = lab.grant_publish_targets({}, targets)
    assert bare["endpoint"] is None and calls[0][1] == lab.NAME + "-lambda"


def test_lab_waits_for_gateway_schema_after_attachment(tmp_path, monkeypatch):
    lab = lab_module(tmp_path, monkeypatch)
    pending = {
        "code": "policy.compiler_unavailable",
        "detail": {"source": "tools_list", "reason": "HTTPStatusError"},
    }
    responses = iter([
        SimpleNamespace(ok=False, status_code=409, json=lambda: pending),
        SimpleNamespace(ok=True, status_code=200, json=lambda: {"ok": True}),
    ])
    sleeps = []
    monkeypatch.setattr(lab.time, "sleep", sleeps.append)
    client = SimpleNamespace(request=lambda *a, **kw: next(responses))
    assert lab.api(client, "POST", "/validate", {}, schema_read_attempts=3) == {"ok": True}
    assert len(sleeps) == 1


@pytest.mark.parametrize("code,source", [
    ("policy.invalid_statement", "tools_list"),
    ("policy.compiler_unavailable", "engine"),
])
def test_lab_does_not_retry_compiler_errors(tmp_path, monkeypatch, code, source):
    lab = lab_module(tmp_path, monkeypatch)
    calls = []

    def request(*a, **kw):
        calls.append(1)
        return SimpleNamespace(
            ok=False, status_code=409,
            json=lambda: {"code": code, "detail": {"source": source}},
        )

    with pytest.raises(RuntimeError):
        lab.api(SimpleNamespace(request=request), "POST", "/validate", {},
                schema_read_attempts=3)
    assert len(calls) == 1


def test_lab_schema_read_wait_is_bounded(tmp_path, monkeypatch):
    lab = lab_module(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lab.time, "sleep", lambda _: None)

    def request(*a, **kw):
        calls.append(1)
        return SimpleNamespace(ok=False, status_code=409, json=lambda: {
            "code": "policy.compiler_unavailable",
            "detail": {"source": "tools_list", "reason": "HTTPStatusError"},
        })

    with pytest.raises(RuntimeError, match="policy.compiler_unavailable"):
        lab.api(SimpleNamespace(request=request), "POST", "/validate", {},
                schema_read_attempts=3)
    assert len(calls) == 3


def test_synthetic_business_invariants_use_isolated_database():
    subprocess.run(
        [sys.executable, str(REPO / "samples/itsm-cn/scripts/test_business.py")],
        check=True, capture_output=True, text=True,
    )
