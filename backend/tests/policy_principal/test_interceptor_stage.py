"""Stream B3: the ``policy-interceptor`` hub bootstrap stage creates the interceptor Lambda,
its role, the engine key and the gateway role's invoke grant — idempotently, least
privilege, never attached (design §5.2, §13.6)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.services import policy_interceptor as pi
from app.services.workspace import WorkspaceContext

GW = "launchpad-gw-exampleid0"
BUCKET = "launchpad-artifacts-123456789012-cn-northwest-1"


def _err(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class FakeLambda:
    def __init__(self, propagation_failures: int = 0) -> None:
        self.functions: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.propagation_failures = propagation_failures

    def get_function_configuration(self, FunctionName):
        if FunctionName not in self.functions:
            raise _err("ResourceNotFoundException")
        return {**self.functions[FunctionName], "State": "Active",
                "LastUpdateStatus": "Successful"}

    def create_function(self, **kw):
        self.calls.append("create")
        if self.propagation_failures:
            self.propagation_failures -= 1
            raise _err("InvalidParameterValueException")
        assert "FunctionUrlConfig" not in kw
        self.functions[kw["FunctionName"]] = {
            **{k: v for k, v in kw.items() if k != "Code"},
            "FunctionArn": f"arn:aws-cn:lambda:cn-northwest-1:123456789012:function:"
                           f"{kw['FunctionName']}"}
        self.code = kw["Code"]["ZipFile"]

    def update_function_code(self, FunctionName, ZipFile, Architectures):
        self.calls.append("update_code")

    def update_function_configuration(self, FunctionName, **kw):
        self.calls.append("update_config")
        self.functions[FunctionName].update(kw)


class FakeIam:
    def __init__(self) -> None:
        self.roles: dict[str, str] = {}
        self.policies: dict[tuple[str, str], dict[str, Any]] = {}
        self.trust: dict[str, Any] = {}

    def create_role(self, RoleName, AssumeRolePolicyDocument, Description, Tags):
        if RoleName in self.roles:
            raise _err("EntityAlreadyExists")
        self.trust = json.loads(AssumeRolePolicyDocument)
        self.roles[RoleName] = f"arn:aws-cn:iam::123456789012:role/{RoleName}"
        return {"Role": {"Arn": self.roles[RoleName]}}

    def get_role(self, RoleName):
        return {"Role": {"Arn": self.roles[RoleName]}}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.policies[(RoleName, PolicyName)] = json.loads(PolicyDocument)


class FakeLogs:
    def __init__(self) -> None:
        self.groups: dict[str, int | None] = {}

    def create_log_group(self, logGroupName, tags):
        if logGroupName in self.groups:
            raise _err("ResourceAlreadyExistsException")
        self.groups[logGroupName] = None

    def put_retention_policy(self, logGroupName, retentionInDays):
        self.groups[logGroupName] = retentionInDays


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None,
                   ServerSideEncryption=None):
        if IfNoneMatch == "*" and Key in self.objects:
            raise _err("PreconditionFailed")
        self.objects[Key] = Body




@pytest.fixture
def world(tmp_path):
    lam, iam, s3, logs = FakeLambda(), FakeIam(), FakeS3(), FakeLogs()
    ctx = WorkspaceContext(account_id="123456789012", region="cn-northwest-1")
    clients = {"lambda": lam, "iam": iam, "s3": s3, "logs": logs}
    ctx_ns = SimpleNamespace(
        client=lambda name: clients[name], partition=ctx.partition, arn=ctx.arn,
        service_principal=ctx.service_principal)
    built: list[Path] = []

    def package(build_dir: Path) -> bytes:
        built.append(build_dir)
        return b"PK-fake"

    def run(**overrides):
        kwargs = dict(ctx=ctx_ns, bucket=BUCKET, gateway_id=GW,
                      gateway_role_name="launchpad-gateway-role-cn-northwest-1",
                      subnet_ids=["subnet-a", "subnet-b"], security_group_id="sg-1",
                      build_dir=tmp_path / "build", package=package, log=lambda _m: None,
                      sleep=lambda _s: None, retention_days=30)
        kwargs.update(overrides)
        return pi.ensure_interceptor(**kwargs)

    return SimpleNamespace(lam=lam, iam=iam, s3=s3, logs=logs, run=run, built=built)


def test_first_run_creates_role_key_function_and_the_invoke_grant(world):
    out = world.run()
    assert out["created"] and out["engine_key_created"]
    fn = world.lam.functions[pi.function_name()]
    assert fn["Runtime"] == "python3.12" and fn["Architectures"] == ["arm64"]
    assert fn["Timeout"] == 5 and fn["Handler"] == "main.handler"
    assert fn["VpcConfig"] == {"SubnetIds": ["subnet-a", "subnet-b"], "SecurityGroupIds": ["sg-1"]}
    assert fn["Environment"]["Variables"]["LAUNCHPAD_POLICY_GATEWAY_ID"] == GW
    assert world.iam.trust["Statement"][0]["Principal"] == {"Service": "lambda.amazonaws.com"}
    grant = world.iam.policies[("launchpad-gateway-role-cn-northwest-1", pi.GATEWAY_INVOKE_POLICY)]
    assert grant["Statement"] == [{"Sid": "InvokePolicyInterceptor", "Effect": "Allow",
                                   "Action": "lambda:InvokeFunction",
                                   "Resource": out["policy_interceptor_function_arn"]}]
    assert len(world.s3.objects[f"policy/gateway/{GW}/engine-key"]) == 32


def test_role_reads_only_the_gateway_bundle_prefixes(world):
    """Global model: one function may serve several gateways, so the read covers every
    gateway prefix of the bucket — and nothing else in it."""
    world.run()
    policy = world.iam.policies[(pi.role_name(), pi.ROLE_POLICY)]
    read = next(s for s in policy["Statement"] if s["Sid"] == "ReadGatewayBundle")
    assert read == {"Sid": "ReadGatewayBundle", "Effect": "Allow", "Action": "s3:GetObject",
                    "Resource": f"arn:aws-cn:s3:::{BUCKET}/policy/gateway/*"}
    actions = {a for s in policy["Statement"] for a in
               ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
    assert not any(a.startswith(("kms:", "ssm:", "iam:", "lambda:", "s3:Put")) for a in actions)


def test_second_run_is_a_no_op_and_never_rotates_the_key(world):
    world.run()
    key = world.s3.objects[f"policy/gateway/{GW}/engine-key"]
    out = world.run()
    assert not out["created"] and not out["updated"] and not out["engine_key_created"]
    assert world.lam.calls == ["create"] and len(world.built) == 1
    assert world.s3.objects[f"policy/gateway/{GW}/engine-key"] == key


def test_a_changed_source_digest_updates_code_then_configuration(world, monkeypatch):
    world.run()
    monkeypatch.setattr(pi, "source_digest", lambda _r: "changed")
    out = world.run()
    assert out["updated"] and world.lam.calls == ["create", "update_code", "update_config"]


def test_iam_propagation_is_retried(tmp_path):
    lam = FakeLambda(propagation_failures=2)
    iam, s3 = FakeIam(), FakeS3()
    ctx = WorkspaceContext(account_id="123456789012", region="cn-northwest-1")
    ns = SimpleNamespace(client={"lambda": lam, "iam": iam, "s3": s3,
                                 "logs": FakeLogs()}.__getitem__,
                         partition=ctx.partition, arn=ctx.arn,
                         service_principal=ctx.service_principal)
    pi.ensure_interceptor(ctx=ns, bucket=BUCKET, gateway_id=GW, gateway_role_name="g",
                          subnet_ids=["s"], security_group_id="sg", build_dir=tmp_path,
                          package=lambda _d: b"z", log=lambda _m: None, sleep=lambda _s: None,
                          retention_days=0)
    assert lam.calls == ["create", "create", "create"]


def test_no_network_outputs_refuses_before_any_call(world):
    with pytest.raises(RuntimeError, match="PolicyInterceptorNetwork"):
        world.run(subnet_ids=[])
    assert world.lam.calls == [] and world.iam.roles == {}


def test_package_holds_the_entry_the_package_and_the_lock(tmp_path):
    def runner(cmd, **_kw):
        if cmd[0] == "uv":
            Path(cmd[cmd.index("-o") + 1]).write_text(
                "cryptography==1.0 --hash=sha256:00\n", encoding="utf-8")
        else:
            target = Path(cmd[cmd.index("-t") + 1])
            (target / "cryptography").mkdir(parents=True, exist_ok=True)
            (target / "cryptography" / "__init__.py").write_text("", encoding="utf-8")
            assert cmd[cmd.index("--python-version") + 1] == "3.12"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    data = pi.build_package(tmp_path / "b", runner=runner)
    zf = zipfile.ZipFile(__import__("io").BytesIO(data))
    names = set(zf.namelist())
    assert {"main.py", "interceptor/handler.py", "interceptor/core.py", "interceptor/codec.py",
            "interceptor/bundle.py", "interceptor/engine_client.py", "requirements.lock",
            "cryptography/__init__.py"} <= names
    assert not any("__pycache__" in n for n in names)
    assert zf.read("main.py").decode().startswith("from interceptor.handler import handler")


def test_the_zipped_entry_resolves_the_handler(tmp_path, monkeypatch):
    """The packaged layout imports: ``main.handler`` → ``interceptor.handler.handler``."""
    pkg = tmp_path / "pkg"
    (pkg / "interceptor").mkdir(parents=True)
    for path in pi.PACKAGE_DIR.glob("*.py"):
        (pkg / "interceptor" / path.name).write_bytes(path.read_bytes())
    (pkg / "main.py").write_text(pi.ENTRY, encoding="utf-8")
    monkeypatch.syspath_prepend(str(pkg))
    import importlib
    import sys

    try:
        main = importlib.import_module("main")
        assert callable(main.handler)
        assert main.handler.__module__ == "interceptor.handler"
    finally:
        for name in [n for n in sys.modules if n == "main" or n.split(".")[0] == "interceptor"]:
            sys.modules.pop(name)


class FakeEc2:
    def __init__(self, subnets, groups) -> None:
        self.subnets, self.groups = subnets, groups

    def describe_subnets(self, Filters):
        wanted = set(Filters[0]["Values"])
        return {"Subnets": [s for s in self.subnets
                            if {t["Value"] for t in s["Tags"]} & wanted]}

    def describe_security_groups(self, Filters):
        wanted = set(Filters[0]["Values"])
        return {"SecurityGroups": [g for g in self.groups
                                   if {t["Value"] for t in g["Tags"]} & wanted]}


def _subnet(name, sid):
    return {"SubnetId": sid, "Tags": [{"Key": "Name", "Value": name}]}


def test_network_is_found_by_this_environments_name_tags(monkeypatch):
    from app.core import naming

    ec2 = FakeEc2([_subnet("launchpad-cn-policy-b", "sb"), _subnet("launchpad-cn-policy-a", "sa"),
                   _subnet("launchpad-cn-policy-a-t1", "other-env")],
                  [{"GroupId": "sg-1", "Tags": [{"Key": "Name",
                                                 "Value": "launchpad-cn-policy-interceptor"}]}])
    assert pi.discover_network(ec2) == (["sa", "sb"], "sg-1")
    monkeypatch.setattr(naming, "current_env_name", lambda: "t1")
    assert pi.discover_network(ec2) == ([], "")  # t1 has one subnet and no group


def test_stage_without_the_network_degrades_with_a_reason(monkeypatch, tmp_path):
    from app.services import bootstrap

    ec2 = FakeEc2([], [])
    ctx = SimpleNamespace(client=lambda name: ec2)
    with pytest.raises(RuntimeError, match="PolicyInterceptorNetwork"):
        pi.run_stage(ctx, {"artifacts_bucket": BUCKET, "gateway_id": GW},
                     gateway_role_arn="arn:aws-cn:iam::1:role/g", build_dir=tmp_path)
    monkeypatch.setattr(pi, "run_stage", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("no network")))
    out = bootstrap._policy_interceptor_stage(lambda _n, fn: fn(), {"resources": {}},
                                              "cn-northwest-1", "123456789012", "arn:r/g")
    assert out == {"policy_interceptor_unavailable_reason": "RuntimeError: no network"}


def test_bootstrap_runs_the_stage_only_with_the_kill_switch_on():
    import inspect

    from app.services import bootstrap

    source = inspect.getsource(bootstrap)
    assert "if get_settings().policy_gateway_interceptor and outputs.get(\"GatewayRoleArn\")" \
        in source
    from app.core.config import Settings

    assert Settings.model_fields["policy_gateway_interceptor"].default is False


def test_the_log_group_is_created_with_the_platform_retention(world):
    """Nit 5: Lambda would otherwise create /aws/lambda/<fn> on first write, unbounded."""
    out = world.run()
    name = f"/aws/lambda/{pi.function_name()}"
    assert out["policy_interceptor_log_group"] == name
    assert world.logs.groups == {name: 30}
    world.run()  # exists: kept, retention re-applied
    assert world.logs.groups == {name: 30}


def test_eni_actions_are_denied_to_the_functions_own_code(world):
    """Nit 6: the ENI actions must be on "*" (Lambda's VPC guide); a Deny keyed on
    lambda:SourceFunctionArn keeps the function's code from calling them."""
    world.run()
    statements = {s["Sid"]: s for s in world.iam.policies[(pi.role_name(), pi.ROLE_POLICY)][
        "Statement"]}
    allow, deny = statements["VpcNetworkInterfaces"], statements["VpcNetworkInterfacesNotFromCode"]
    assert allow["Action"] == deny["Action"] and deny["Effect"] == "Deny"
    assert deny["Condition"] == {"ArnEquals": {"lambda:SourceFunctionArn": (
        f"arn:aws-cn:lambda:cn-northwest-1:123456789012:function:{pi.function_name()}")}}


def test_a_re_pointed_gateway_updates_the_configuration_only(world):
    """Same sources, new gateway (M1 probe → M2 launchpad-gw-t1): the digest is equal, so the
    code stays, but the configuration drift is applied."""
    world.run()
    out = world.run(gateway_id="launchpad-gw-t1-other")
    assert out["updated"] and world.lam.calls == ["create", "update_config"]
    fn = world.lam.functions[pi.function_name()]
    assert fn["Environment"]["Variables"]["LAUNCHPAD_POLICY_GATEWAY_ID"] == "launchpad-gw-t1-other"
    assert not world.run(gateway_id="launchpad-gw-t1-other")["updated"]


def test_config_drift_ignores_the_service_added_vpc_id():
    wanted = {"Role": "r", "Handler": "h", "Runtime": "p", "Timeout": 5, "MemorySize": 512,
              "Environment": {"Variables": {"A": "1"}},
              "VpcConfig": {"SubnetIds": ["a", "b"], "SecurityGroupIds": ["g"]}}
    current = {**wanted, "VpcConfig": {"SubnetIds": ["b", "a"], "SecurityGroupIds": ["g"],
                                       "VpcId": "vpc-1"}}
    assert pi.config_drift(current, wanted) == []
    assert pi.config_drift({**current, "Timeout": 30}, wanted) == ["Timeout"]


class FakeControl:
    def __init__(self, names):
        self.names = names

    def list_gateways(self, maxResults, **_):
        return {"items": [{"name": n, "gatewayId": f"{n}-id"} for n in self.names]}

    def get_gateway(self, gatewayIdentifier):
        return {"gatewayId": gatewayIdentifier,
                "roleArn": "arn:aws-cn:iam::123456789012:role/launchpad-probe-gateway-role"}


def test_the_probe_target_provisions_for_the_probe_gateway_and_its_role(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(pi, "discover_network", lambda _ec2: (["s1", "s2"], "sg"))
    monkeypatch.setattr(pi, "ensure_interceptor", lambda **kw: seen.update(kw) or {})
    ctx = SimpleNamespace(client=lambda _n: None)
    pi.run_stage(ctx, {"artifacts_bucket": BUCKET, "gateway_id": "launchpad-gw-t1-x"},
                 gateway_role_arn="arn:aws-cn:iam::1:role/launchpad-gateway-role-t1",
                 build_dir=tmp_path, target="probe",
                 control=FakeControl(["launchpad-gw-t1", "launchpad-probe-gw"]))
    assert seen["gateway_id"] == "launchpad-probe-gw-id"
    assert seen["gateway_role_name"] == "launchpad-probe-gateway-role"
    with pytest.raises(RuntimeError, match="exactly one"):
        pi.run_stage(ctx, {"artifacts_bucket": BUCKET, "gateway_id": "g"},
                     gateway_role_arn="arn:r/g", build_dir=tmp_path, target="probe",
                     control=FakeControl(["launchpad-gw-t1"]))
    pi.run_stage(ctx, {"artifacts_bucket": BUCKET, "gateway_id": "launchpad-gw-t1-x"},
                 gateway_role_arn="arn:aws-cn:iam::1:role/launchpad-gateway-role-t1",
                 build_dir=tmp_path, target="platform")
    assert seen["gateway_id"] == "launchpad-gw-t1-x"
    assert seen["gateway_role_name"] == "launchpad-gateway-role-t1"


@pytest.mark.parametrize(("state", "want", "never"), [
    (True, f"attached to {GW}", "NOT attached"),
    (False, "NOT attached (attach with", "attached to"),
    (None, f"attach state of {GW} unknown", "NOT attached"),
])
def test_the_stage_log_reports_the_gateways_recorded_attach_state(world, state, want, never):
    """N17-obs486: an update of an attached function said "NOT attached"."""
    lines: list[str] = []
    seen: list[str] = []
    world.run(log=lines.append, attached=lambda gw: seen.append(gw) or state)
    assert seen == [GW]
    assert len(lines) == 1 and want in lines[0] and never not in lines[0]


def test_the_default_attach_reader_follows_the_ledger_and_degrades_to_unknown(monkeypatch):
    from app.core.db import SessionLocal
    from app.services import policy_gateway_state

    gw = "launchpad-gw-obs486"
    assert pi.ledger_attached(gw) is False  # no row: not attached
    with SessionLocal() as db:
        policy_gateway_state.record_attach(db, gw, "arn:aws-cn:lambda:cn-northwest-1:1:function:f")
    assert pi.ledger_attached(gw) is True
    with SessionLocal() as db:
        policy_gateway_state.record_detach(db, gw)
    assert pi.ledger_attached(gw) is False

    def broken(*_a, **_k):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(policy_gateway_state, "gateway_interceptor_attached", broken)
    assert pi.ledger_attached(gw) is None
