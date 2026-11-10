"""Create a named CN environment and finish its policy-engine installation.

Run with the backend uv environment. Configuration contains SSM parameter NAMES,
never passwords. plan is read-only; apply creates only a new tagged stack or
resumes a stack recorded in this run's state file. It never updates another stack.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.deploy_cn_capacity import network_capacity, require_network_capacity  # noqa: E402


def read_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    for key in ("account_id", "region", "stack_name", "environment", "release_bucket"):
        if not cfg.get(key):
            raise ValueError(f"Missing {key}")
    if not re.fullmatch(r"\d{12}", cfg["account_id"]):
        raise ValueError("account_id must be the destination account's 12 digits")
    if cfg["region"] != "cn-northwest-1":
        raise ValueError("This release is validated for cn-northwest-1 only")
    if not re.fullmatch(r"[a-z0-9]{1,3}", cfg["environment"]):
        raise ValueError("Use an environment suffix of 1-3 lowercase letters/digits")
    if cfg["environment"] in ("kb",):
        raise ValueError("Reserved environment name")
    if cfg.get("console_entry", "direct") not in ("direct", "public_nlb"):
        raise ValueError("console_entry must be direct or public_nlb")
    params = cfg["parameters"]
    if params.get("AuthPasswordPolicy", "strict") not in ("strict", "relaxed"):
        raise ValueError("AuthPasswordPolicy must be strict or relaxed")
    minimum = str(params.get("AuthPasswordMinLength", 12))
    if not minimum.isdigit() or not 6 <= int(minimum) <= 128:
        raise ValueError("AuthPasswordMinLength must be an integer between 6 and 128")
    if params.get("MemoryDomainMode", "shared") not in ("shared", "dedicated"):
        raise ValueError("MemoryDomainMode must be shared or dedicated")
    if params.get("AllocateElasticIp", "true") not in ("true", "false"):
        raise ValueError("AllocateElasticIp must be true or false")
    network = ipaddress.ip_network(params["AllowedCidr"], strict=False)
    if network.version != 4 or network.prefixlen == 0:
        raise ValueError("AllowedCidr must be a specific IPv4 CIDR")
    if params.get("TlsMode", "selfsigned") != "selfsigned":
        raise ValueError("The distribution deployment requires HTTPS")
    port = str(params.get("ConsolePort", "8443"))
    if not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise ValueError("ConsolePort must be an integer between 1024 and 65535")
    if cfg.get("console_entry") == "public_nlb" and int(port) == 8080:
        raise ValueError("public_nlb requires a port other than 8080")
    if not isinstance(cfg.get("console_http_lab", False), bool):
        raise ValueError("console_http_lab must be an explicit boolean")
    if cfg.get("console_http_lab"):
        if cfg.get("console_entry") != "public_nlb":
            raise ValueError("console_http_lab requires public_nlb")
        http_port = str(cfg.get("http_port", 8088))
        if (not http_port.isdigit() or not 1024 <= int(http_port) <= 65535
                or int(http_port) in (8080, int(port))):
            raise ValueError("http_port must be 1024-65535, distinct from HTTPS and not 8080")
    elif "http_port" in cfg:
        raise ValueError("http_port requires console_http_lab=true")
    for key in ("GlobalBedrockApiKeyParameter", "ConsolePasswordParameter"):
        if not re.fullmatch(r"[\w.-]+", params.get(key, "")):
            raise ValueError(f"{key} must name a flat SSM parameter")
    cfg["engine_artifact"] = str((path.parent / cfg["engine_artifact"]).resolve())
    return cfg


def parameters(cfg: dict) -> list[dict]:
    values = {
        **cfg["parameters"],
        "EnvName": cfg["environment"],
        "PolicyInterceptorNetwork": "true",
        "PolicyPrincipalSigningKey": "true",
    }
    return [{"ParameterKey": k, "ParameterValue": str(v)} for k, v in values.items()]


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def resolve_commit(cfg: dict) -> str:
    p = cfg["parameters"]
    ref = p["GitBranch"]
    result = subprocess.run(
        [
            "git",
            "ls-remote",
            p["GitRepoUrl"],
            f"refs/heads/{ref}",
            f"refs/tags/{ref}",
            f"refs/tags/{ref}^{{}}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    rows = [line.split() for line in result.stdout.splitlines()]
    commits = {sha for sha, name in rows if not name.endswith("^{}")}
    peeled = [sha for sha, name in rows if name.endswith("^{}")]
    if len(commits) != 1:
        raise ValueError("Release ref is missing or ambiguous; publish an immutable tag first")
    return peeled[0] if peeled else next(iter(commits))


def stack_or_none(cfn, name: str):
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as exc:
        error = exc.response["Error"]
        if error["Code"] == "ValidationError" and "does not exist" in error["Message"]:
            return None
        raise


def wait_stack(cfn, name: str) -> dict:
    deadline = time.monotonic() + 5400
    previous = None
    while time.monotonic() < deadline:
        stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
        status = stack["StackStatus"]
        if status != previous:
            print(f"{name}: {status}", flush=True)
            previous = status
        if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            return stack
        if "FAILED" in status or "ROLLBACK" in status or status == "DELETE_COMPLETE":
            raise RuntimeError(f"{status}: inspect CloudFormation events; no automatic deletion")
        time.sleep(15)
    raise TimeoutError("Stack still pending; rerun apply to resume")


def nlb_stack_name(cfg: dict) -> str:
    return f"launchpad-cn-console-nlb-{cfg['environment']}"


def owned_host(cfn, cfg: dict, state: dict):
    """Only recover a lost create response with this run's persisted identity."""
    stack = stack_or_none(cfn, cfg["stack_name"])
    if stack:
        tags = {tag["Key"]: tag["Value"] for tag in stack.get("Tags", [])}
        owned = stack["StackId"] == state.get("stack_id")
        recovering = (
            not state.get("stack_id")
            and state.get("host_create_token")
            and tags.get("launchpad:deployment") == state["host_create_token"]
            and tags.get("launchpad:config") == state.get("config_sha256")
            and tags.get("launchpad:release") == state.get("commit")
        )
        if not (owned or recovering):
            raise RuntimeError("Host stack already exists and is not owned by this state file")
    elif state.get("stack_id"):
        raise RuntimeError("Recorded host stack is missing; inspect before creating a replacement")
    return stack


def validate_host_resume(cfg: dict, stack: dict) -> None:
    """Hardware repairs may replace the instance; deployment identity must not drift."""
    current = {p["ParameterKey"]: p["ParameterValue"] for p in stack.get("Parameters", [])}
    legacy_defaults = {
        "MemoryDomainMode": "shared", "AuthPasswordPolicy": "strict", "AuthPasswordMinLength": "12",
    }
    expected = {
        **legacy_defaults, "ConsolePort": "8443", "TlsMode": "selfsigned",
        **cfg["parameters"], "EnvName": cfg["environment"],
        "PolicyInterceptorNetwork": "true", "PolicyPrincipalSigningKey": "true",
    }
    for key in ("ConsolePort", "AuthPasswordMinLength"):
        expected[key] = str(int(expected[key]))
    protected = {
        "EnvName", "MemoryDomainMode", "ConsolePort", "TlsMode", "GitRepoUrl", "GitBranch",
        "ConsolePasswordParameter", "GlobalBedrockApiKeyParameter",
        "AuthPasswordPolicy", "AuthPasswordMinLength",
        "PolicyInterceptorNetwork", "PolicyPrincipalSigningKey",
    }
    mismatches = [
        key for key in sorted(protected)
        if current.get(key, legacy_defaults.get(key)) != str(expected[key])
    ]
    if mismatches:
        raise RuntimeError(
            "Owned host deployment parameters changed: " + ", ".join(mismatches)
            + "; restore the recorded deployment configuration before resuming"
        )


def ensure_host_stack(cfn, cfg: dict, state: dict, path: Path, template_url: str) -> str:
    stack = owned_host(cfn, cfg, state)
    if stack:
        state["stack_id"] = stack["StackId"]
    else:
        state.setdefault("host_create_token", str(uuid.uuid4()))
        save(path, state)
        response = cfn.create_stack(
            StackName=cfg["stack_name"],
            TemplateURL=template_url,
            Parameters=parameters(cfg),
            Capabilities=["CAPABILITY_NAMED_IAM"],
            ClientRequestToken=state["host_create_token"],
            Tags=[
                {"Key": "launchpad:deployment", "Value": state["host_create_token"]},
                {"Key": "launchpad:config", "Value": state["config_sha256"]},
                {"Key": "launchpad:release", "Value": state["commit"]},
            ],
            OnFailure="DO_NOTHING",
        )
        state["stack_id"] = response["StackId"]
    save(path, state)
    return state["stack_id"]


def owned_nlb(cfn, cfg: dict, state: dict):
    """Read-only preflight; recover a lost create response using a persisted run token."""
    stack = stack_or_none(cfn, nlb_stack_name(cfg))
    if stack:
        tags = {tag["Key"]: tag["Value"] for tag in stack.get("Tags", [])}
        owned = stack["StackId"] == state.get("nlb_stack_id")
        recovering = (
            not state.get("nlb_stack_id")
            and state.get("nlb_create_token")
            and tags.get("launchpad:deployment") == state["nlb_create_token"]
            and tags.get("launchpad:host-stack") == state.get("stack_id")
        )
        if not (owned or recovering):
            raise RuntimeError("NLB stack already exists and is not owned by this state file")
    elif state.get("nlb_stack_id"):
        raise RuntimeError("Recorded NLB stack is missing; inspect before creating a replacement")
    return stack


def ensure_console_entry(cfn, cfg: dict, outputs: dict, state: dict, path: Path) -> dict:
    """Provision the existing NLB template and return the advertised console origin."""
    if cfg.get("console_entry", "direct") == "direct":
        return outputs
    stack = owned_nlb(cfn, cfg, state)
    if stack:
        state["nlb_stack_id"] = stack["StackId"]
        save(path, state)
        stack = wait_stack(cfn, stack["StackId"])
    values = {}
    for param, logical in (
        ("VpcId", "Vpc"), ("PublicSubnetId", "PublicSubnet"),
        ("InstanceId", "Instance"), ("InstanceSecurityGroupId", "ConsoleSecurityGroup"),
    ):
        values[param] = cfn.describe_stack_resource(
            StackName=state["stack_id"], LogicalResourceId=logical,
        )["StackResourceDetail"]["PhysicalResourceId"]
    if values["InstanceId"] != outputs["InstanceId"]:
        raise RuntimeError("NLB target differs from the recorded host")
    port = str(cfg["parameters"].get("ConsolePort", "8443"))
    values.update(
        InstancePort=port, ListenerPort=port,
        AccessMode="OpenToInternet", NlbSecurityGroupMode="enabled",
    )
    if cfg.get("console_http_lab"):
        values.update(ConsoleHttpLab="true", HttpPort=str(int(cfg.get("http_port", 8088))))
    if not stack:
        state.setdefault("nlb_create_token", str(uuid.uuid4()))
        save(path, state)
        response = cfn.create_stack(
            StackName=nlb_stack_name(cfg),
            TemplateBody=(REPO / "infra/ec2/launchpad-console-nlb.yaml").read_text(),
            Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()],
            ClientRequestToken=state["nlb_create_token"],
            Tags=[
                {"Key": "launchpad:deployment", "Value": state["nlb_create_token"]},
                {"Key": "launchpad:host-stack", "Value": state["stack_id"]},
                {"Key": "launchpad:release", "Value": state["commit"]},
            ],
            OnFailure="DO_NOTHING",
        )
        state["nlb_stack_id"] = response["StackId"]
    else:
        current = {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}
        if values.keys() - current.keys():
            raise RuntimeError("Owned NLB is missing required parameters; inspect its template")
        replacement_keys = {"InstanceId", "InstanceSecurityGroupId"}
        incompatible = [
            key for key, value in values.items()
            if key not in replacement_keys and current.get(key) != value
        ]
        if incompatible:
            raise RuntimeError(
                "Owned NLB network/access parameters changed: " + ", ".join(incompatible)
                + "; reconcile explicitly before resuming (the public address is retained)"
            )
        if any(current.get(key) != values[key] for key in replacement_keys):
            attempt = {
                "token": str(uuid.uuid4()), "stack_id": state["nlb_stack_id"],
                "instance_id": outputs["InstanceId"],
                "previous_parameters": {key: current[key] for key in replacement_keys},
                "parameters": {key: values[key] for key in replacement_keys},
            }
            state.setdefault("nlb_update_attempts", []).append(attempt)
            save(path, state)
            cfn.update_stack(
                StackName=state["nlb_stack_id"], UsePreviousTemplate=True,
                Parameters=[
                    {"ParameterKey": key, "ParameterValue": values[key]}
                    if key in replacement_keys else {"ParameterKey": key, "UsePreviousValue": True}
                    for key in current
                ],
                ClientRequestToken=attempt["token"],
            )
            stack = wait_stack(cfn, state["nlb_stack_id"])
            attempt["status"] = stack["StackStatus"]
    save(path, state)
    stack = wait_stack(cfn, state["nlb_stack_id"])
    current = {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}
    if any(current.get(key) != value for key, value in values.items()):
        raise RuntimeError("Owned NLB parameters differ from the current host after reconciliation")
    for attempt in state.get("nlb_update_attempts", []):
        if not attempt.get("status") and attempt["stack_id"] == state["nlb_stack_id"]:
            if all(current[key] == value for key, value in attempt["parameters"].items()):
                attempt["status"] = stack["StackStatus"]
    nlb = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    state["nlb_outputs"] = nlb
    save(path, state)
    return {
        **outputs,
        "DirectConsoleUrl": outputs["ConsoleUrl"],
        "ConsoleUrl": nlb["ConsoleUrl"],
        "NlbDnsName": nlb["NlbDnsName"],
        "NlbStaticIp": nlb["StaticIp"],
    }


def verify_console_entry(elb, cfg: dict, outputs: dict, state: dict, *, timeout=600) -> dict:
    """A created load balancer is not sufficient: check target, public route and auth."""
    target = {
        "Id": outputs["InstanceId"],
        "Port": int(cfg.get("http_port", 8088) if cfg.get("console_http_lab")
                    else cfg["parameters"].get("ConsolePort", "8443")),
    }
    deadline = time.monotonic() + timeout
    last = "waiting for target"
    # This distribution uses self-signed TLS; hostname SANs are checked on the host.
    with httpx.Client(verify=False, timeout=15, trust_env=False) as client:
        while time.monotonic() < deadline:
            rows = elb.describe_target_health(
                TargetGroupArn=state["nlb_outputs"]["TargetGroupArn"], Targets=[target],
            )["TargetHealthDescriptions"]
            healthy = (
                len(rows) == 1 and rows[0]["Target"]["Id"] == target["Id"]
                and rows[0]["Target"]["Port"] == target["Port"]
                and rows[0]["TargetHealth"]["State"] == "healthy"
            )
            last = json.dumps(rows)
            if healthy:
                try:
                    health = client.get(outputs["ConsoleUrl"] + "/api/health")
                    protected = client.get(outputs["ConsoleUrl"] + "/api/agents")
                    if protected.status_code != 401:
                        raise RuntimeError("Public console auth check did not return 401")
                    if health.status_code == 200 and health.json().get("status") == "ok":
                        return {"target": target, "health": "ok", "unauthenticated_status": 401}
                    last = f"Public health returned {health.status_code}"
                except (httpx.TransportError, ValueError) as exc:
                    last = str(exc)
            time.sleep(15)
    raise TimeoutError(f"NLB is not ready; resume apply after diagnosis: {last}")


def wait_ssm(ssm, instance: str, cid: str) -> dict:
    deadline = time.monotonic() + 1900
    while time.monotonic() < deadline:
        try:
            result = ssm.get_command_invocation(CommandId=cid, InstanceId=instance)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(5)
            continue
        if result["Status"] not in ("Pending", "InProgress", "Delayed", "Cancelling"):
            return {
                k: result.get(k)
                for k in ("CommandId", "Status", "StandardOutputContent", "StandardErrorContent")
            }
        time.sleep(5)
    raise TimeoutError("SSM still pending; rerun apply to resume")


def command_context(ssm, key: str, state: dict, path: Path) -> dict:
    """Upgrade legacy CommandId-only state from authoritative SSM targeting metadata."""
    cid = state[key]
    context_key = f"{key}_context"
    context = state.get(context_key)
    if context:
        if context.get("command_id") != cid or not context.get("instance_id"):
            raise RuntimeError(f"Invalid saved {key} target; inspect deployment state")
        return context
    commands = ssm.list_commands(CommandId=cid)["Commands"]
    if len(commands) != 1 or commands[0].get("CommandId") != cid:
        raise RuntimeError(f"Cannot recover {key} target from SSM; inspect the saved command")
    command = commands[0]
    targets = command.get("InstanceIds", [])
    scripts = command.get("Parameters", {}).get("commands", [])
    if (command.get("DocumentName") != "AWS-RunShellScript" or len(targets) != 1
            or len(scripts) != 1 or command.get("Targets")):
        raise RuntimeError(f"Ambiguous saved {key} target/script; refusing to guess")
    context = {
        "command_id": cid, "instance_id": targets[0],
        "script_sha256": hashlib.sha256(scripts[0].encode()).hexdigest(),
    }
    state[context_key] = context
    save(path, state)
    return context


def record_command(key: str, context: dict, evidence: dict, state: dict, path: Path) -> None:
    """Keep each attempt's result, even after a replacement overwrites the latest evidence."""
    digest = hashlib.sha256(context["command_id"].encode()).hexdigest()[:20]
    evidence_path = path.parent / "commands" / f"{key}-{digest}.json"
    save(evidence_path, {**context, **evidence})
    history = state.setdefault("command_history", [])
    if not any(row["command_id"] == context["command_id"] for row in history):
        history.append({
            **context, "kind": key, "status": evidence["Status"],
            "evidence": str(evidence_path),
        })
    save(path, state)


def run_ssm(
    ssm, instance: str, script: str, state: dict, path: Path, *, fresh: bool = False,
) -> dict:
    """Resume installation; serialize, then replace any older readiness command."""
    key = "verification_command" if fresh else "policy_command"
    evidence_name = "deployment-verification.json" if fresh else "policy-install.json"
    context_key = f"{key}_context"
    script_sha256 = hashlib.sha256(script.encode()).hexdigest()
    cid = state.get(key)
    if cid:
        context = command_context(ssm, key, state, path)
        replace = (
            fresh or context["instance_id"] != instance
            or context.get("script_sha256") != script_sha256
        )
        if replace:
            # Always settle on the original target. A replaced instance must never
            # be asked for another instance's invocation, nor overlap its installer.
            previous = wait_ssm(ssm, context["instance_id"], cid)
            record_command(key, context, previous, state, path)
            state.pop(key)
            state.pop(context_key)
            save(path, state)
            cid = None
    if not cid:
        cid = ssm.send_command(
            InstanceIds=[instance],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script], "executionTimeout": ["1800"]},
            Comment="Launchpad CN release: readiness" if fresh else
                    "Launchpad CN release: install and verify policy engine",
        )["Command"]["CommandId"]
        state[key] = cid
        context = {
            "command_id": cid, "instance_id": instance, "script_sha256": script_sha256,
        }
        state[context_key] = context
        save(path, state)
    evidence = wait_ssm(ssm, instance, cid)
    record_command(key, context, evidence, state, path)
    save(path.with_name(evidence_name), evidence)
    if evidence["Status"] != "Success":
        state.pop(key, None)
        state.pop(context_key, None)
        save(path, state)
        raise RuntimeError(f"Host command failed; inspect {evidence_name}, then resume")
    return evidence


def verification_script(cfg: dict, outputs: dict, state: dict) -> str:
    expected = {
        **policy_settings(cfg, outputs),
        "commit": state["commit"],
        "account_id": cfg["account_id"],
        "region": cfg["region"],
        "env_name": cfg["environment"],
        "console_entry": cfg.get("console_entry", "direct"),
        "auth_password_policy": cfg["parameters"].get("AuthPasswordPolicy", "strict"),
        "auth_password_min_length": int(cfg["parameters"].get("AuthPasswordMinLength", 12)),
        "direct_console_url": outputs.get("DirectConsoleUrl", ""),
    }
    return (
        "cd /home/ubuntu/launchpad/backend && "
        "/home/ubuntu/launchpad/backend/.venv/bin/python -B "
        "../scripts/verify_deployment_host.py --expected "
        + shlex.quote(json.dumps(expected))
    )


def selected_domain_outputs(cfn, cfg: dict) -> dict:
    name = "launchpad-memory-domain"
    dedicated = cfg["parameters"].get("MemoryDomainMode", "shared") == "dedicated"
    if dedicated:
        name += f"-{cfg['environment']}"
    stack = stack_or_none(cfn, name)
    if stack is None and not dedicated:
        # Pre-D17 shared deployments export their domain from the memory stack.
        stack = stack_or_none(cfn, f"launchpad-memory-{cfg['environment']}")
    if stack is None:
        raise RuntimeError(f"Selected memory domain stack {name} is missing")
    return {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}


def policy_settings(cfg: dict, outputs: dict) -> dict:
    return {
        **({"console_http_lab": True, "oauth_return_url": ""} if cfg.get("console_http_lab")
           else {}),
        "public_base_url": outputs["ConsoleUrl"],
        "memory_domain_mode": cfg["parameters"].get("MemoryDomainMode", "shared"),
        "policy_gateway_interceptor": True,
        "policy_principal_kms_key_id": outputs["PolicyPrincipalKeyArn"],
        "policy_hub_role_arn": (
            f"arn:aws-cn:iam::{cfg['account_id']}:role/{outputs['OpsRoleName']}"
        ),
        "policy_engine_endpoints": [f"{outputs['PrivateIp']}:{p}" for p in (7443, 7444)],
    }


def policy_script(cfg: dict, outputs: dict, state: dict) -> str:
    args = [
        state["artifact_uri"],
        state["artifact_sha256"],
        state["commit"],
        cfg["region"],
        json.dumps(policy_settings(cfg, outputs)),
    ]
    if outputs.get("DirectConsoleUrl"):
        args.append(str(ipaddress.IPv4Address(urlsplit(outputs["DirectConsoleUrl"]).hostname)))
    return "bash /home/ubuntu/launchpad/scripts/install_policy_host.sh " + shlex.join(args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "apply", "status"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    cfg = read_config(args.config.resolve())
    session = boto3.Session(profile_name=cfg.get("profile"), region_name=cfg["region"])
    actual = session.client("sts").get_caller_identity()["Account"]
    if actual != cfg["account_id"]:
        raise RuntimeError("AWS credential account does not match account_id")
    cfn, s3, ssm = (session.client(n) for n in ("cloudformation", "s3", "ssm"))
    state = json.loads(args.state.read_text()) if args.state.exists() else {}
    signature = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    if state and state.get("config_sha256") != signature:
        raise RuntimeError("State belongs to a different config; do not reuse it")
    stack = stack_or_none(cfn, cfg["stack_name"])
    if args.operation == "status":
        print(
            json.dumps({"state": state, "stack_status": stack and stack["StackStatus"]}, indent=2)
        )
        return
    commit = resolve_commit(cfg)
    local_commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if local_commit != commit:
        raise RuntimeError("Operator checkout must match the configured release ref")
    subprocess.run(["git", "-C", str(REPO), "diff", "--quiet", "HEAD", "--"], check=True)
    if state and state.get("commit") != commit:
        raise RuntimeError("Release ref moved since the first run; use an immutable tag")
    artifact = Path(cfg["engine_artifact"])
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    # Independent checksum is mandatory, generated by make policy-engine-artifact.
    expected = Path(str(artifact) + ".sha256").read_text().split()[0]
    if digest != expected:
        raise RuntimeError("Engine artifact checksum mismatch")
    stack = owned_host(cfn, cfg, state)
    if stack:
        validate_host_resume(cfg, stack)
    if not stack:
        names = [f"launchpad-base-{cfg['environment']}", f"launchpad-memory-{cfg['environment']}"]
        if cfg["parameters"].get("MemoryDomainMode") == "dedicated":
            names.append(f"launchpad-memory-domain-{cfg['environment']}")
        for name in names:
            if stack_or_none(cfn, name):
                raise RuntimeError(
                    f"Environment resource stack {name} already exists; use a new name"
                )
    nlb = owned_nlb(cfn, cfg, state) if cfg.get("console_entry") == "public_nlb" else None
    capacity = network_capacity(session, cfn, cfg, stack, nlb)
    print(json.dumps({"network_capacity": capacity}, indent=2), flush=True)
    require_network_capacity(capacity)
    s3.head_bucket(Bucket=cfg["release_bucket"], ExpectedBucketOwner=actual)
    for key in (
        "GlobalBedrockApiKeyParameter",
        "ConsolePasswordParameter",
        "GitDeployKeyParameter",
    ):
        name = cfg["parameters"].get(key)
        if name:
            parameter = ssm.get_parameter(Name=name, WithDecryption=False)["Parameter"]
            if parameter["Type"] != "SecureString":
                raise ValueError(f"{key} must reference an SSM SecureString")
    print(
        json.dumps(
            {
                "account": actual,
                "region": cfg["region"],
                "stack": cfg["stack_name"],
                "environment": cfg["environment"],
                "commit": commit,
                "artifact_sha256": digest,
                "existing_default_environment": "preserved",
                "opensearch": cfg["parameters"].get("MemoryDomainMode", "shared"),
                "registration_password_policy": {
                    "mode": cfg["parameters"].get("AuthPasswordPolicy", "strict"),
                    "min_length": int(cfg["parameters"].get("AuthPasswordMinLength", 12)),
                },
                "console_entry": cfg.get("console_entry", "direct"),
                "nlb_stack": nlb_stack_name(cfg) if cfg.get("console_entry") == "public_nlb"
                else None,
                "console_access": "all IPv4 sources through NLB"
                if cfg.get("console_entry") == "public_nlb"
                else cfg["parameters"]["AllowedCidr"],
            },
            indent=2,
        ),
        flush=True,
    )
    if args.operation == "plan":
        return
    if not state:
        state = {"config_sha256": signature, "commit": commit}
        save(args.state, state)
    state["status"] = "deploying"
    state["network_capacity"] = capacity
    save(args.state, state)
    prefix = f"releases/{commit}/{cfg['environment']}"
    template_key = f"{prefix}/launchpad-cn-ec2.yaml"
    s3.upload_file(
        str(REPO / "infra/ec2/launchpad-cn-ec2.yaml"),
        cfg["release_bucket"],
        template_key,
        ExtraArgs={"ServerSideEncryption": "AES256"},
    )
    # Regional S3 URL; CloudFormation uses the caller's access to the private object.
    url = f"https://{cfg['release_bucket']}.s3.{cfg['region']}.amazonaws.com.cn/{template_key}"
    host_id = ensure_host_stack(cfn, cfg, state, args.state, url)
    stack = wait_stack(cfn, host_id)
    validate_host_resume(cfg, stack)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    outputs = ensure_console_entry(cfn, cfg, outputs, state, args.state)
    outputs.update(selected_domain_outputs(cfn, cfg))
    base = cfn.describe_stacks(StackName=f"launchpad-base-{cfg['environment']}")["Stacks"][0]
    bucket = next(
        o["OutputValue"] for o in base["Outputs"] if o["OutputKey"] == "ArtifactsBucketName"
    )
    key = f"policy-engine/{commit}/{artifact.name}"
    s3.upload_file(str(artifact), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    state.update(artifact_uri=f"s3://{bucket}/{key}", artifact_sha256=digest)
    state["outputs"] = outputs
    save(args.state, state)
    result = run_ssm(
        ssm, outputs["InstanceId"], policy_script(cfg, outputs, state), state, args.state
    )
    state["policy_install"] = result["Status"]
    readiness = run_ssm(
        ssm, outputs["InstanceId"], verification_script(cfg, outputs, state),
        state, args.state, fresh=True,
    )
    state["host_check"] = readiness["Status"]
    if cfg.get("console_entry") == "public_nlb":
        state["console_check"] = verify_console_entry(
            session.client("elbv2"), cfg, outputs, state,
        )
    state["status"] = "platform_ready"
    save(args.state, state)
    print(f"Platform ready: {outputs['ConsoleUrl']}; lab initialization is a separate step")


if __name__ == "__main__":
    main()
