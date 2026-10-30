"""The ``policy-interceptor`` hub bootstrap stage (stream B3; design §5.2, §13.6).

Creates — never attaches — the gateway interceptor Lambda ``launchpad-policy-interceptor[-env]``
from ``app/policy/interceptor/`` (+ ``cryptography`` for linux/arm64, hash-locked), its role,
the engine HMAC key object and the gateway role's ``lambda:InvokeFunction`` grant. Attaching it
to ``launchpad-gw[-env]`` is an ``UpdateGateway`` on a live resource: that is
``scripts/attach_policy_interceptor.py`` (dry run by default) with the user's explicit go
(§15.5), never this stage.

Inert unless ``Settings.policy_gateway_interceptor`` is on and the EC2 stack was updated with
``PolicyInterceptorNetwork=true`` (its private subnets + security group outputs, B4).

Placement (§13.6): two private subnets of the hub VPC, no NAT, no interface endpoints — the
bundle and key come from S3 through the gateway endpoint, the public keys ride in the bundle,
Lambda delivers its logs outside the VPC. Nothing is world-accessible: no Function URL, no
resource policy; only the gateway role may invoke it.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from app.core.naming import resource_name
from app.policy.interceptor.bundle import HMAC_KEY_BYTES, engine_key_key, gateway_prefix

FUNCTION_BASE = "launchpad-policy-interceptor"
ROLE_BASE = "launchpad-policy-interceptor"
GATEWAY_INVOKE_POLICY = "launchpad-policy-interceptor-invoke"
ROLE_POLICY = "launchpad-policy-interceptor"
RUNTIME = "python3.12"
PYTHON_VERSION = "3.12"
HANDLER = "main.handler"
TIMEOUT_S = 5  # P0 d: never let the interceptor reach the gateway's own 500
MEMORY_MB = 512
DIGEST_ENV = "LAUNCHPAD_INTERCEPTOR_DIGEST"
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "policy" / "interceptor"
ENTRY = "from interceptor.handler import handler  # noqa: F401\n"
_ROLE_RETRIES = 6
_NOT_FOUND = {"ResourceNotFoundException", "NoSuchEntity", "NoSuchKey", "404"}


def function_name() -> str:
    return resource_name(FUNCTION_BASE)


def role_name() -> str:
    return resource_name(ROLE_BASE)




def source_digest(requirements: list[str]) -> str:
    """sha256 over the package sources + the requirement pins: the function is updated only
    when this changes (zips are not byte-reproducible, CodeSha256 cannot tell)."""
    h = hashlib.sha256()
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    h.update("\n".join(requirements).encode())
    h.update(f"{RUNTIME}|{HANDLER}".encode())
    return h.hexdigest()


def requirements() -> list[str]:
    """The hub's own ``cryptography``, so the Lambda verifies with the exact code the hub
    signs and tests with."""
    return [f"cryptography=={importlib.metadata.version('cryptography')}"]


def build_package(build_dir: Path, *, runner: Callable[..., Any] = subprocess.run) -> bytes:
    """Resolve → hash-locked install for linux/arm64 CPython 3.12 → zip
    (``main.py`` + ``interceptor/`` + the wheels). Uses the zip deploy path's mirror settings
    and resolver; no fallback to an unlocked install."""
    from app.core.runtime_target import pip_platform_args
    from app.deployer import zip_runtime

    if build_dir.exists():
        shutil.rmtree(build_dir)
    pkg = build_dir / "pkg"
    (pkg / "interceptor").mkdir(parents=True)
    reqs = requirements()
    lock = zip_runtime._compile_lock(reqs, build_dir, runner, python_version=PYTHON_VERSION)
    proc = runner(
        [sys.executable, "-m", "pip", "install", "--require-hashes", "-r", str(lock),
         "-t", str(pkg), *pip_platform_args(), "--only-binary=:all:",
         "--python-version", PYTHON_VERSION, "--quiet"],
        capture_output=True, text=True, env=zip_runtime._build_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError("pip install of the interceptor requirements failed: "
                           + " ".join((proc.stderr or "").split())[-600:])
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        shutil.copyfile(path, pkg / "interceptor" / path.name)
    (pkg / "main.py").write_text(ENTRY, encoding="utf-8")
    (pkg / "requirements.lock").write_text(lock.read_text(encoding="utf-8"), encoding="utf-8")
    out = build_dir / "interceptor.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(pkg.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                zf.write(path, path.relative_to(pkg))
    return out.read_bytes()


_ENI_ACTIONS = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeSubnets", "ec2:DeleteNetworkInterface",
                "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]


def role_policy(ctx: Any, *, bucket: str, gateway_id: str, function: str) -> dict[str, Any]:
    """Least privilege: read the gateway bundle prefixes (bundle, pointer, key — every gateway
    of the bucket since the Global model lets one function serve several gateways;
    ``gateway_id`` is kept for the stage's logging only), write its own log group, and the
    ENI actions a VPC-attached Lambda needs (no resource-level permission exists for them)."""
    prefix = gateway_prefix(gateway_id).rsplit(gateway_id, 1)[0]  # `policy/gateway/`
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadGatewayBundle", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:{ctx.partition}:s3:::{bucket}/{prefix}*"},
        {"Sid": "OwnLogs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": [ctx.arn("logs", f"log-group:/aws/lambda/{function}"),
                      ctx.arn("logs", f"log-group:/aws/lambda/{function}:*")]},
        # The Lambda VPC guide requires these on "*" (the service creates the Hyperplane
        # ENIs before any id exists); the Deny below keeps the FUNCTION'S OWN CODE from using
        # them — calls it makes carry lambda:SourceFunctionArn, the service's do not (nit 6)
        {"Sid": "VpcNetworkInterfaces", "Effect": "Allow", "Action": _ENI_ACTIONS,
         "Resource": "*"},
        {"Sid": "VpcNetworkInterfacesNotFromCode", "Effect": "Deny", "Action": _ENI_ACTIONS,
         "Resource": "*",
         "Condition": {"ArnEquals": {
             "lambda:SourceFunctionArn": ctx.arn("lambda", f"function:{function}")}}},
    ]}


def _code(exc: Exception) -> str:
    return str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code") or "")


def ensure_role(iam: Any, ctx: Any, policy: dict[str, Any]) -> str:
    name = role_name()
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": ctx.service_principal("lambda")},
        "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.create_role(
            RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
            Description="Launchpad gateway policy interceptor (Lambda)",
            Tags=[{"Key": "launchpad:managed", "Value": "true"}],
        )["Role"]["Arn"]
    except ClientError as exc:
        if _code(exc) != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=name, PolicyName=ROLE_POLICY, PolicyDocument=json.dumps(policy))
    return arn


def ensure_engine_key(s3: Any, bucket: str, gateway_id: str) -> bool:
    """Create the engine HMAC key once (exactly 32 random raw bytes, never rewritten —
    ``IfNoneMatch``; contract amendment 3); True when this run created it. The engine host
    installs the same object byte-for-byte as ``/etc/launchpad-policy-engine/engine-key``."""
    key = engine_key_key(gateway_id)
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=secrets.token_bytes(HMAC_KEY_BYTES),
                      ContentType="application/octet-stream", IfNoneMatch="*",
                      ServerSideEncryption="AES256")
        return True
    except ClientError as exc:
        if _code(exc) in {"PreconditionFailed", "412"}:
            return False
        raise


def ensure_log_group(logs: Any, function: str, retention_days: int) -> str:
    """The function's own ``/aws/lambda/<fn>`` group, created up front with the platform's
    ``log_group_retention_days`` (0 = unbounded, as everywhere else) — Lambda would otherwise
    create it on first write with no retention (nit 5)."""
    name = f"/aws/lambda/{function}"
    try:
        logs.create_log_group(logGroupName=name, tags={"launchpad:managed": "true"})
    except ClientError as exc:
        if _code(exc) != "ResourceAlreadyExistsException":
            raise
    if retention_days > 0:
        logs.put_retention_policy(logGroupName=name, retentionInDays=int(retention_days))
    return name


def _wait_ready(lam: Any, name: str, sleep: Callable[[float], None], timeout_s: int = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cfg = lam.get_function_configuration(FunctionName=name)
        if cfg.get("State") == "Failed" or cfg.get("LastUpdateStatus") == "Failed":
            reason = cfg.get("StateReason") or cfg.get("LastUpdateStatusReason")
            raise RuntimeError(f"Lambda {name} failed: {reason}")
        if cfg.get("State") == "Active" and cfg.get("LastUpdateStatus") in (None, "Successful"):
            return
        sleep(5)
    raise TimeoutError(f"Lambda {name} not Active after {timeout_s}s")


def config_drift(current: dict[str, Any], wanted: dict[str, Any]) -> list[str]:
    """The configuration fields GetFunctionConfiguration reports differently from ``wanted``
    (``VpcConfig`` compared as sets, without the service-added ``VpcId``)."""
    drift = [k for k in ("Role", "Handler", "Runtime", "Timeout", "MemorySize")
             if current.get(k) != wanted[k]]
    if ((current.get("Environment") or {}).get("Variables") or {}) != \
            wanted["Environment"]["Variables"]:
        drift.append("Environment")
    vpc = current.get("VpcConfig") or {}
    if set(vpc.get("SubnetIds") or []) != set(wanted["VpcConfig"]["SubnetIds"]) or \
            set(vpc.get("SecurityGroupIds") or []) != set(wanted["VpcConfig"]["SecurityGroupIds"]):
        drift.append("VpcConfig")
    return drift


def ledger_attached(gateway_id: str) -> bool | None:
    """The gateway's persisted attach state (``policy_gateway_state``), or None when the ledger
    cannot be read: the stage only reports it, so a read failure never fails the stage."""
    try:
        from app.core.db import SessionLocal
        from app.services import policy_gateway_state

        with SessionLocal() as db:
            return policy_gateway_state.gateway_interceptor_attached(db, gateway_id)
    except Exception:  # noqa: BLE001 — the log line degrades to "unknown"
        return None


def attach_note(attached: bool | None, gateway_id: str) -> str:
    """The stage log's attach clause (N17-obs486: an update of an attached function used to
    say "NOT attached")."""
    if attached:
        return f"attached to {gateway_id} (the gateway calls this code from now on)"
    if attached is None:
        return (f"attach state of {gateway_id} unknown (ledger unreadable); "
                "scripts/attach_policy_interceptor.py attaches or detaches")
    return ("NOT attached (attach with scripts/attach_policy_interceptor.py after the "
            "user's go)")


def ensure_interceptor(
    *, ctx: Any, bucket: str, gateway_id: str, gateway_role_name: str,
    subnet_ids: list[str], security_group_id: str, build_dir: Path,
    package: Callable[[Path], bytes] = build_package, log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep, retention_days: int | None = None,
    attached: Callable[[str], bool | None] | None = None,
) -> dict[str, Any]:
    """Idempotent: role + inline policy, the engine key, the function (created, or its code
    and configuration updated when the source digest changed), the gateway role's invoke
    grant on exactly that function. Returns the resource-map entries."""
    if not subnet_ids or not security_group_id:
        raise RuntimeError("the EC2 stack has no policy interceptor network — update it with "
                           "PolicyInterceptorNetwork=true first")
    lam, iam, s3 = ctx.client("lambda"), ctx.client("iam"), ctx.client("s3")
    name = function_name()
    role_arn = ensure_role(iam, ctx, role_policy(ctx, bucket=bucket, gateway_id=gateway_id,
                                                 function=name))
    key_created = ensure_engine_key(s3, bucket, gateway_id)
    if retention_days is None:
        from app.core.config import get_settings

        retention_days = int(get_settings().log_group_retention_days or 0)
    log_group = ensure_log_group(ctx.client("logs"), name, retention_days)
    reqs = requirements()
    digest = source_digest(reqs)
    config = {
        "Role": role_arn, "Handler": HANDLER, "Runtime": RUNTIME, "Timeout": TIMEOUT_S,
        "MemorySize": MEMORY_MB,
        "Environment": {"Variables": {
            "LAUNCHPAD_POLICY_BUCKET": bucket, "LAUNCHPAD_POLICY_GATEWAY_ID": gateway_id,
            DIGEST_ENV: digest}},
        "VpcConfig": {"SubnetIds": list(subnet_ids), "SecurityGroupIds": [security_group_id]},
    }
    try:
        current = lam.get_function_configuration(FunctionName=name)
    except ClientError as exc:
        if _code(exc) not in _NOT_FOUND:
            raise
        current = None
    created = updated = False
    if current is None:
        code = package(build_dir)
        for attempt in range(_ROLE_RETRIES):
            try:
                lam.create_function(
                    FunctionName=name, Code={"ZipFile": code}, Architectures=["arm64"],
                    Description="Launchpad gateway policy interceptor (REQUEST + RESPONSE)",
                    Publish=False, Tags={"launchpad:managed": "true"}, **config)
                break
            except ClientError as exc:
                if _code(exc) == "InvalidParameterValueException" and attempt + 1 < _ROLE_RETRIES:
                    sleep(5)  # IAM propagation of a brand-new role
                    continue
                raise
        created = True
    else:
        if ((current.get("Environment") or {}).get("Variables") or {}).get(DIGEST_ENV) != digest:
            lam.update_function_code(FunctionName=name, ZipFile=package(build_dir),
                                     Architectures=["arm64"])
            _wait_ready(lam, name, sleep)
            updated = True
        if updated or config_drift(current, config):
            # a changed source digest, or a re-pointed gateway / bucket / network / role
            lam.update_function_configuration(FunctionName=name, **config)
            updated = True
    _wait_ready(lam, name, sleep)
    function_arn = lam.get_function_configuration(FunctionName=name)["FunctionArn"]
    iam.put_role_policy(
        RoleName=gateway_role_name, PolicyName=GATEWAY_INVOKE_POLICY,
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Sid": "InvokePolicyInterceptor", "Effect": "Allow",
            "Action": "lambda:InvokeFunction", "Resource": function_arn}]}))
    state = "created" if created else "updated" if updated else "up to date"
    key_note = ", engine key created" if key_created else ""
    log(f"policy-interceptor: {name} {state}{key_note} — "
        f"{attach_note((attached or ledger_attached)(gateway_id), gateway_id)}")
    return {"policy_interceptor_function_arn": function_arn,
            "policy_interceptor_role_arn": role_arn,
            "policy_interceptor_digest": digest, "policy_interceptor_log_group": log_group,
            "created": created, "updated": updated,
            "engine_key_created": key_created}


SUBNET_TAGS = ("launchpad-cn-policy-a", "launchpad-cn-policy-b")
SECURITY_GROUP_TAG = "launchpad-cn-policy-interceptor"


def discover_network(ec2: Any) -> tuple[list[str], str]:
    """The B4 subnets and security group of THIS environment, by their Name tags (the EC2
    stack's ``PolicyInterceptorNetwork=true`` resources); ``([], "")`` when absent."""
    names = [resource_name(t) for t in SUBNET_TAGS]
    subnets = ec2.describe_subnets(Filters=[{"Name": "tag:Name", "Values": names}]).get(
        "Subnets") or []
    groups = ec2.describe_security_groups(Filters=[{
        "Name": "tag:Name", "Values": [resource_name(SECURITY_GROUP_TAG)]}]).get(
        "SecurityGroups") or []
    by_name = {next((t["Value"] for t in s.get("Tags") or [] if t["Key"] == "Name"), ""):
               s["SubnetId"] for s in subnets}
    if len(groups) != 1 or any(n not in by_name for n in names):
        return [], ""
    return [by_name[n] for n in names], groups[0]["GroupId"]


PROBE_GATEWAY = "launchpad-probe-gw"


def probe_gateway(control: Any) -> tuple[str, str]:
    """``(gateway id, role ARN)`` of the kept P0 probe gateway, found by its exact name."""
    found, token = [], None
    while True:
        page = control.list_gateways(maxResults=100, **({"nextToken": token} if token else {}))
        found += [g for g in page.get("items") or [] if g.get("name") == PROBE_GATEWAY]
        token = page.get("nextToken")
        if not token:
            break
    if len(found) != 1:
        raise RuntimeError(f"expected exactly one gateway named {PROBE_GATEWAY}, "
                           f"found {len(found)}")
    detail = control.get_gateway(gatewayIdentifier=found[0]["gatewayId"])
    return detail["gatewayId"], detail["roleArn"]


def run_stage(ctx: Any, resources: dict[str, Any], *, gateway_role_arn: str, build_dir: Path,
              log: Callable[[str], None] = print, target: str | None = None,
              control: Any = None) -> dict[str, Any]:
    """The hub bootstrap's ``policy-interceptor`` stage (``Settings.policy_gateway_interceptor``
    only): the network by tag, then ``ensure_interceptor`` for this environment's gateway — or,
    with ``Settings.policy_interceptor_target = probe`` (M1), for the kept probe gateway and
    its own role. Re-running with the other target re-points the same function."""
    if target is None:
        from app.core.config import get_settings

        target = get_settings().policy_interceptor_target
    bucket = str(resources.get("artifacts_bucket") or "")
    gateway_id = str(resources.get("gateway_id") or "")
    if target == "probe":
        from app.services.agentcore.client import control_client

        gateway_id, gateway_role_arn = probe_gateway(control or control_client(ctx))
    if not bucket or not gateway_id or not gateway_role_arn:
        raise RuntimeError("the policy-interceptor stage needs artifacts_bucket, gateway_id and "
                           "the gateway role (run the storage and gateway stages first)")
    subnets, group = discover_network(ctx.client("ec2"))
    return ensure_interceptor(
        ctx=ctx, bucket=bucket, gateway_id=gateway_id,
        gateway_role_name=gateway_role_arn.rsplit("/", 1)[-1], subnet_ids=subnets,
        security_group_id=group, build_dir=build_dir, log=log)
