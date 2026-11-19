"""Global-partition CloudFront console entry: validated before any AWS write, China unchanged."""
import json
from types import SimpleNamespace

import pytest

from tests.test_cn_distribution import REPO, config, module


@pytest.fixture
def deploy():
    return module(REPO / "scripts/deploy_cn.py")


@pytest.fixture
def verify():
    return module(REPO / "scripts/verify_deployment_host.py")


def global_config(tmp_path):
    cfg = json.loads((REPO / "config/deploy-global.example.json").read_text())
    cfg.update(account_id="123456789012", release_bucket="private-test-release")
    cfg["parameters"]["AllowedCidr"] = "192.0.2.1/32"
    cfg["engine_artifact"] = str(tmp_path / "engine.tar.gz")
    path = tmp_path / "deploy-global.json"
    path.write_text(json.dumps(cfg))
    return path, cfg


def test_global_example_is_a_valid_cloudfront_deployment(tmp_path, deploy):
    path, _ = global_config(tmp_path)
    read = deploy.read_config(path)
    assert read["region"] == "us-east-1" and read["console_entry"] == "cloudfront"
    assert deploy.partition(read) == "aws"


@pytest.mark.parametrize("change", [
    ("region", "cn-northwest-1"),                # CloudFront entry is Global-only
    ("console_entry", "public_nlb"),            # no open NLB in the Global partition
    ("console_entry", "direct"),
    ("region", "us-west-2"),                    # not a validated region
    ("TlsMode", "selfsigned"),                  # the VPC-origin hop is plain HTTP
    ("AllocateElasticIp", "true"),
    ("SampleRestApi", "true"),                  # API-key-only API counts as world-accessible
    ("ConsoleEntry", "cloudfront"),             # derived, never configured
    ("CloudFrontPrefixListId", "pl-12345678"),
])
def test_cloudfront_misconfiguration_fails_before_aws(tmp_path, deploy, change):
    path, cfg = global_config(tmp_path)
    key, value = change
    (cfg if key in ("region", "console_entry") else cfg["parameters"])[key] = value
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        deploy.read_config(path)


def test_china_configs_cannot_pick_the_cloudfront_entry(tmp_path, deploy):
    path, cfg = config(tmp_path)
    cfg["console_entry"] = "cloudfront"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="console_entry"):
        deploy.read_config(path)


def test_entry_parameters_only_for_cloudfront(tmp_path, deploy):
    _, china = config(tmp_path)
    china_params = {p["ParameterKey"] for p in deploy.parameters(china)}
    assert not {"ConsoleEntry", "CloudFrontPrefixListId"} & china_params
    _, cfg = global_config(tmp_path)
    cfg["cloudfront_prefix_list"] = "pl-3b927c52"
    params = {p["ParameterKey"]: p["ParameterValue"] for p in deploy.parameters(cfg)}
    assert params["ConsoleEntry"] == "cloudfront"
    assert params["CloudFrontPrefixListId"] == "pl-3b927c52"
    assert params["TlsMode"] == "none" and params["SampleRestApi"] == "false"


def test_prefix_list_lookup_is_exact(deploy):
    rows = {"PrefixLists": [{"PrefixListId": "pl-3b927c52"}]}
    ec2 = SimpleNamespace(describe_managed_prefix_lists=lambda **kw: (
        kw["Filters"][0]["Values"] == [deploy.CLOUDFRONT_PREFIX_LIST] and rows))
    assert deploy.cloudfront_prefix_list(ec2) == "pl-3b927c52"
    rows["PrefixLists"].append({"PrefixListId": "pl-other"})
    with pytest.raises(RuntimeError, match="exactly one"):
        deploy.cloudfront_prefix_list(ec2)


def test_hub_role_arn_follows_the_partition(tmp_path, deploy):
    outputs = {"ConsoleUrl": "https://d1.cloudfront.net", "PolicyPrincipalKeyArn": "k",
               "OpsRoleName": "launchpad-cn-ops-us-east-1-g1", "PrivateIp": "10.44.0.5"}
    _, cfg = global_config(tmp_path)
    assert deploy.policy_settings(cfg, outputs)["policy_hub_role_arn"].startswith("arn:aws:iam::")
    _, china = config(tmp_path)
    assert deploy.policy_settings(china, outputs)["policy_hub_role_arn"].startswith(
        "arn:aws-cn:iam::")


def test_cloudfront_policy_install_has_no_direct_address(tmp_path, deploy):
    import shlex

    _, cfg = global_config(tmp_path)
    outputs = {"ConsoleUrl": "https://d1abc.cloudfront.net", "PolicyPrincipalKeyArn": "k",
               "OpsRoleName": "r", "PrivateIp": "10.44.0.5",
               "CloudFrontDistributionId": "E1"}
    state = {"artifact_uri": "s3://private/key", "artifact_sha256": "a" * 64, "commit": "b" * 40}
    command = shlex.split(deploy.policy_script(cfg, outputs, state))
    assert len(command) == 7  # no sixth (direct-IPv4) argument for a CloudFront entry
    assert json.loads(command[-1])["public_base_url"] == "https://d1abc.cloudfront.net"


def test_create_resume_and_recover_lost_cloudfront_response(tmp_path, deploy, monkeypatch):
    _, cfg = global_config(tmp_path)
    state = {"stack_id": "host-stack-id", "commit": "a" * 40}
    path = tmp_path / "state.json"
    host = {"InstanceId": "i-test", "ConsoleUrl": "http://198.51.100.7:8080"}
    entry = {"ConsoleUrl": "https://d1abc.cloudfront.net", "DistributionId": "E1ABC",
             "DomainName": "d1abc.cloudfront.net", "VpcOriginId": "vo_x", "InstanceId": "i-test"}
    created, stack = [], None

    def create(**kwargs):
        nonlocal stack
        token = json.loads(path.read_text())["cloudfront_create_token"]
        assert token == kwargs["ClientRequestToken"]
        assert kwargs["StackName"] == "launchpad-console-cloudfront-g1"
        assert "AWS::CloudFront::VpcOrigin" in kwargs["TemplateBody"]
        created.append(kwargs)
        stack = {"StackId": "cf-stack-id", "Tags": kwargs["Tags"], "StackStatus": "CREATE_COMPLETE",
                 "Parameters": kwargs["Parameters"],
                 "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in entry.items()]}
        return {"StackId": stack["StackId"]}

    ec2 = SimpleNamespace(describe_instances=lambda **kw: {"Reservations": [{"Instances": [
        {"InstanceId": kw["InstanceIds"][0], "PrivateDnsName": "ip-10-44-0-5.ec2.internal"}]}]})
    cfn = SimpleNamespace(create_stack=create)
    monkeypatch.setattr(deploy, "stack_or_none", lambda *a: stack)
    monkeypatch.setattr(deploy, "wait_stack", lambda *a: stack)
    public = deploy.ensure_console_entry(cfn, cfg, host, state, path, ec2=ec2)
    params = {p["ParameterKey"]: p["ParameterValue"] for p in created[0]["Parameters"]}
    assert params == {"InstanceId": "i-test", "InstancePrivateDnsName": "ip-10-44-0-5.ec2.internal",
                      "ConsolePort": "8080", "EnvName": "g1"}
    assert public["ConsoleUrl"] == "https://d1abc.cloudfront.net"
    assert public["CloudFrontDistributionId"] == "E1ABC"
    assert "DirectConsoleUrl" not in public  # nothing but CloudFront is advertised
    assert deploy.ensure_console_entry(cfn, cfg, host, state, path, ec2=ec2) == public
    state.pop("cloudfront_stack_id")  # lost create response: recover by persisted token only
    assert deploy.ensure_console_entry(cfn, cfg, host, state, path, ec2=ec2) == public
    assert len(created) == 1
    with pytest.raises(RuntimeError, match="not owned"):
        deploy.owned_cloudfront(cfn, cfg, {})
    with pytest.raises(RuntimeError, match="differs from the current host"):
        deploy.ensure_console_entry(cfn, cfg, dict(host, InstanceId="i-replaced"), state, path,
                                    ec2=ec2)
    stack = None
    with pytest.raises(RuntimeError, match="missing"):
        deploy.owned_cloudfront(cfn, cfg, state)


class _Response:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


@pytest.mark.parametrize("agents_status,passes", [(401, True), (200, False)])
def test_cloudfront_acceptance_needs_trusted_tls_and_auth(deploy, monkeypatch, agents_status,
                                                          passes):
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            if url.endswith("/api/health"):
                return _Response(200, {"status": "ok"})
            return _Response(agents_status)

    monkeypatch.setattr(deploy.httpx, "Client", Client)
    cloudfront = SimpleNamespace(
        get_distribution=lambda Id: {"Distribution": {"Status": "Deployed"}})
    outputs = {"ConsoleUrl": "https://d1abc.cloudfront.net", "CloudFrontDistributionId": "E1"}
    if passes:
        result = deploy.verify_cloudfront_entry(cloudfront, outputs, timeout=5)
        assert result["unauthenticated_status"] == 401 and result["tls"] == "trusted"
    else:
        with pytest.raises(RuntimeError, match="401"):
            deploy.verify_cloudfront_entry(cloudfront, outputs, timeout=5)
    assert seen["verify"] is True  # CloudFront's certificate must validate


def test_cloudfront_marker_required_and_no_host_certificate_check(verify, monkeypatch):
    from tests.test_deployment_host_verification import settings

    monkeypatch.delenv("LAUNCHPAD_CONSOLE_ENTRY", raising=False)
    with pytest.raises(verify.VerificationError, match="cloudfront entry marker"):
        verify.verify_settings(settings(verify), {"console_entry": "cloudfront"})
    monkeypatch.setenv("LAUNCHPAD_CONSOLE_ENTRY", "cloudfront")
    assert verify.verify_settings(settings(verify), {"console_entry": "cloudfront"})
    source = (REPO / "scripts/verify_deployment_host.py").read_text()
    assert "host_tls = \"cloudfront\" not in (" in source
    assert 'os.environ.get("LAUNCHPAD_CONSOLE_ENTRY")' in source.split("host_tls =", 1)[1][:200]


def test_policy_installer_accepts_only_a_cloudfront_origin_without_direct_ip():
    text = (REPO / "scripts/install_policy_host.sh").read_text()
    assert '"$PUBLIC_ORIGIN" =~ ^https://[a-z0-9]+\\.cloudfront\\.net$' in text
    assert "printf 'LAUNCHPAD_CONSOLE_ENTRY=cloudfront\\n'" in text
    assert "A CloudFront entry has no direct console address" in text
