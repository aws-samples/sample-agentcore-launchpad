"""Read-only host acceptance, using the running service's environment and identity.

Run as root from the backend uv environment. Secrets read from /proc stay in memory
and are passed only as the child process environment, never arguments or evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

# Only these nonsecret deployment settings may appear in an expectation or evidence.
SETTING_KEYS = (
    "account_id", "region", "env_name", "memory_domain_mode", "public_base_url", "oauth_return_url",
    "auth_password_policy", "auth_password_min_length", "policy_gateway_interceptor",
    "policy_principal_kms_key_id", "policy_hub_role_arn", "policy_engine_endpoints",
)


class VerificationError(RuntimeError):
    """A safe, deliberately nonsecret diagnostic."""


def service_pid() -> int:
    result = subprocess.run(
        ["systemctl", "show", "launchpad-backend", "-p", "MainPID", "-p", "ActiveState",
         "-p", "SubState"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    fields = dict(row.split("=", 1) for row in result.stdout.splitlines() if "=" in row)
    if fields.get("ActiveState") != "active" or fields.get("SubState") != "running":
        raise VerificationError("Backend systemd service is not running")
    pid = int(fields.get("MainPID", "0"))
    if pid <= 0:
        raise VerificationError("Backend systemd service has no live MainPID")
    return pid


def backend_process(main_pid: int, proc: Path = Path("/proc")) -> int:
    """uv may remain MainPID; select its actual single uvicorn descendant."""
    candidates = []

    def visit(pid: int, depth: int = 0) -> None:
        args = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")
        children = (proc / str(pid) / "task" / str(pid) / "children").read_text().split()
        for child in children:
            visit(int(child), depth + 1)
        if b"app.main:app" in args and any(b"uvicorn" in arg for arg in args):
            candidates.append((depth, pid))

    visit(main_pid)
    deepest = max((depth for depth, _ in candidates), default=-1)
    workers = [pid for depth, pid in candidates if depth == deepest]
    if len(workers) != 1:
        raise VerificationError("Expected one running uvicorn backend in the systemd process tree")
    return workers[0]


def process_environment(pid: int, proc: Path = Path("/proc")) -> dict[str, str]:
    # Do not log this mapping: it includes the configured admin/model credentials.
    rows = (proc / str(pid) / "environ").read_bytes().split(b"\0")
    return dict(os.fsdecode(row).split("=", 1) for row in rows if b"=" in row)


def selected_domain_outputs(settings, read_outputs) -> dict:
    suffix = f"-{settings.env_name}" if settings.env_name else ""
    name = "launchpad-memory-domain"
    if settings.memory_domain_mode == "dedicated":
        if not suffix:
            raise VerificationError("Dedicated OpenSearch requires a named environment")
        name += suffix
    try:
        return read_outputs(settings.region, name)
    except ClientError as exc:
        error = exc.response["Error"]
        missing = error["Code"] == "ValidationError" and "does not exist" in error["Message"]
        if settings.memory_domain_mode != "shared" or not missing:
            raise
    # Only a confirmed absent shared stack permits the pre-D17 memory-stack layout.
    return read_outputs(settings.region, f"launchpad-memory{suffix}")


def verify_settings(settings, expected: dict) -> dict:
    if getattr(settings, "console_http_lab", False) != expected.get("console_http_lab", False):
        raise VerificationError("Running HTTP lab opt-in differs from deployment")
    for key in SETTING_KEYS:
        if key in expected and getattr(settings, key) != expected[key]:
            raise VerificationError(f"Running backend setting {key} differs from deployment")
    if expected.get("console_entry") == "public_nlb":
        if os.environ.get("LAUNCHPAD_CONSOLE_ENTRY") != "public_nlb":
            raise VerificationError("Running backend is missing its public_nlb entry marker")
    return {
        **{key: getattr(settings, key) for key in SETTING_KEYS},
        "console_http_lab": getattr(settings, "console_http_lab", False),
    }


def verify_http(settings, client) -> dict:
    health = client.get("http://127.0.0.1:8000/api/health")
    if health.status_code != 200 or health.json().get("status") != "ok":
        raise VerificationError("Running backend health is not ok")
    response = client.get("http://127.0.0.1:8000/api/auth/status")
    if response.status_code != 200:
        raise VerificationError("Running backend auth status is unavailable")
    auth = response.json()
    policy = {
        "mode": settings.auth_password_policy, "min_length": settings.auth_password_min_length,
    }
    if auth.get("auth_required") is not True:
        raise VerificationError("Running backend authentication is disabled")
    if auth.get("password_policy") != policy:
        raise VerificationError("Running backend password policy differs from its process settings")
    return policy


def verify_resources(settings) -> dict:
    from app.policy.dogwood_compiler import engine_bin
    from app.services import aws_clients, bootstrap, memory_bootstrap
    from app.services import policy_engine_control as control
    from app.services.workspace import WorkspaceContext

    outputs = selected_domain_outputs(settings, bootstrap.get_stack_outputs)
    resources = settings.resources
    for key, output in (
        ("memory_os_endpoint", "MemoryDomainEndpoint"), ("memory_os_domain_arn", "MemoryDomainArn"),
    ):
        if resources.get(key) != outputs[output]:
            raise VerificationError(f"Configured {key} differs from the selected domain stack")
    if outputs.get("MemoryAdminRoleArn"):
        if resources.get("memory_admin_role_arn") != outputs["MemoryAdminRoleArn"]:
            raise VerificationError("Configured memory admin role differs from the selected stack")
    if resources.get("memory_unavailable_reason"):
        raise VerificationError("Memory bootstrap is degraded")
    ctx = WorkspaceContext(
        account_id=settings.account_id, region=settings.region,
        role_arn=resources.get("memory_admin_role_arn") or None,
    )
    healthy, _ = memory_bootstrap.probe_memory_substrate(
        aws_clients.opensearch_http(ctx, resources["memory_os_endpoint"]),
    )
    if not healthy:
        raise VerificationError("Selected OpenSearch substrate is not healthy")
    engine_bin()
    for shard in range(control.shard_count()):
        if control.call(shard, {"op": "status"}).get("ok") is not True:
            raise VerificationError(f"Policy shard {shard} refused its status check")
    return {"opensearch": "healthy", "policy_shards": control.shard_count()}


def verify_worker(expected: dict) -> dict:
    from app.core.config import get_settings

    settings = get_settings()
    actual = verify_settings(settings, expected)
    with httpx.Client(timeout=15, trust_env=False) as client:
        policy = verify_http(settings, client)
    return {
        "settings": actual, "password_policy": policy, **verify_resources(settings),
    }


def verify_host(expected: dict) -> None:
    if os.geteuid() != 0:
        raise VerificationError("Host verification must run as root to inspect the service")
    main_pid = service_pid()
    pid = backend_process(main_pid)
    if Path(f"/proc/{pid}/cwd").resolve() != REPO / "backend":
        raise VerificationError("Backend process is running from another checkout")
    commit = subprocess.run(
        ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    if expected.get("commit") and commit != expected["commit"]:
        raise VerificationError("Host checkout differs from the deployment release")
    environment = process_environment(pid)
    # runuser supplies the same supplementary groups as the systemd User=ubuntu
    # service. The OpenSearch and socket probes must not use root's privileges.
    result = subprocess.run(
        ["/usr/sbin/runuser", "-u", "ubuntu", "-m", "--", sys.executable, "-B", str(Path(__file__)),
         "--worker", "--expected", json.dumps(expected)],
        env=environment, cwd=REPO / "backend", capture_output=True, text=True, timeout=300,
    )
    if result.returncode:
        # The worker only emits safe diagnostics; unexpected exception text is suppressed.
        print(result.stderr.strip(), file=sys.stderr)
        raise VerificationError("Running backend acceptance failed")
    if service_pid() != main_pid or backend_process(main_pid) != pid:
        raise VerificationError("Backend restarted during verification; retry acceptance")
    for origin in (expected.get("public_base_url"), expected.get("direct_console_url")):
        if origin and urlsplit(origin).scheme == "https":
            address = urlsplit(origin).hostname
            result_tls = subprocess.run(
                ["openssl", "verify", "-CAfile", "/etc/nginx/launchpad-tls/cert.pem",
                 "-verify_ip", address, "/etc/nginx/launchpad-tls/cert.pem"],
                capture_output=True, timeout=15,
            )
            if result_tls.returncode:
                raise VerificationError("Console TLS certificate misses the planned address")
    print(json.dumps({"backend_pid": pid, "commit": commit, **json.loads(result.stdout)}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", default="{}", help="nonsecret deployment settings JSON")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        expected = json.loads(args.expected)
        if args.worker:
            print(json.dumps(verify_worker(expected)))
        else:
            verify_host(expected)
    except VerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Settings/AWS/HTTP exception strings can contain credentials or response data.
        code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
        print(f"Host verification failed ({code}); inspect the service privately", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
