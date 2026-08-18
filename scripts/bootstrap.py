#!/usr/bin/env python3
"""Launchpad bootstrap CLI.

Deploys the shared CDK stack when missing, then idempotently ensures the
AgentCore shared resources (registry, memory, Gateway, Transaction Search) and
writes config/launchpad.yaml. Policy resources remain operator-managed.

Run from the backend venv so the app package resolves:
    cd backend && uv run python ../scripts/bootstrap.py [--skip-cdk]
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from botocore.exceptions import ClientError  # noqa: E402

from app.services import bootstrap as bs  # noqa: E402

AGENTCORE_CLI_VERSION = "0.21.1"
AGENTCORE_CLI_PREFIX = REPO_ROOT / "data" / "agentcore-cli"
AGENTCORE_CLI = AGENTCORE_CLI_PREFIX / "node_modules" / ".bin" / "agentcore"


def _managed_agentcore_cli_version() -> str | None:
    try:
        # The repository-owned executable is invoked directly without a shell.
        proc = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            [str(AGENTCORE_CLI), "--version"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def ensure_agentcore_cli() -> str:
    """Install and verify the repository-owned AgentCore CLI."""
    version = _managed_agentcore_cli_version()
    if version == AGENTCORE_CLI_VERSION:
        return version

    print(f"── installing @aws/agentcore@{AGENTCORE_CLI_VERSION}…")
    subprocess.run(
        [
            "npm",
            "install",
            "--prefix",
            str(AGENTCORE_CLI_PREFIX),
            "--no-save",
            "--package-lock=false",
            f"@aws/agentcore@{AGENTCORE_CLI_VERSION}",
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    version = _managed_agentcore_cli_version()
    if version != AGENTCORE_CLI_VERSION:
        found = version or "unavailable"
        raise RuntimeError(
            "managed AgentCore CLI verification failed: "
            f"expected {AGENTCORE_CLI_VERSION}, got {found}"
        )
    return version


def stack_exists(region: str) -> bool:
    try:
        bs.get_stack_outputs(region)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ValidationError":
            return False
        raise


def deploy_cdk() -> None:
    print(f"── stack {bs.STACK_NAME} not found — running cdk deploy…")
    subprocess.run(
        ["uv", "run", "cdk", "deploy", "--require-approval", "never"],
        cwd=REPO_ROOT / "infra",
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap AgentCore Launchpad shared infra")
    parser.add_argument("--region", default=None, help="AWS region (default: settings)")
    parser.add_argument("--skip-cdk", action="store_true", help="never invoke cdk deploy")
    args = parser.parse_args()

    agentcore_cli_version = ensure_agentcore_cli()
    region = args.region or bs.get_settings().region
    if not stack_exists(region):
        if args.skip_cdk:
            print(f"stack {bs.STACK_NAME} missing and --skip-cdk given — aborting", flush=True)
            return 1
        deploy_cdk()

    summary = bs.run_bootstrap(region)

    registry = summary["registry"]
    rows = [
        ("account", summary["account_id"]),
        ("region", summary["region"]),
        ("agentcore CLI", agentcore_cli_version),
        (
            "registry",
            registry["arn"] if registry["available"] else "unavailable · skipped",
        ),
        (
            "registry state",
            (
                "created" if registry["created"] else "reused"
            )
            if registry["available"]
            else registry["reason"],
        ),
        ("memory", f"{summary['memory']['arn']}"),
        ("memory state", "created" if summary["memory"]["created"] else "reused"),
        ("artifacts bucket", summary["stack_outputs"]["ArtifactsBucketName"]),
        ("ecr repo", summary["stack_outputs"]["EcrRepoUri"]),
        ("codebuild", summary["stack_outputs"]["CodeBuildProjectName"]),
        ("user pool", summary["stack_outputs"]["UserPoolId"]),
        ("demo passwords", "set (see config/launchpad.yaml)"
         if summary["demo_passwords_set"] else "unchanged"),
    ]
    if summary.get("gateway"):
        gw = summary["gateway"]
        rows += [
            ("gateway", gw["gateway"]["url"]),
            ("gateway state", "created" if gw["gateway"]["created"] else "reused"),
            ("api-key provider", "created" if gw["api_key_provider"]["created"] else "reused"),
        ] + [
            (f"target {name}", ("created" if t["created"] else "reused") + f" · {t['id']}")
            for name, t in gw["targets"].items()
        ]
    if summary.get("observability"):
        obs = summary["observability"]
        rows.append(
            (
                "transaction search",
                ("enabled" if obs["enabled"] else str(obs.get("status") or "not ready")),
            )
        )
    if summary.get("skill_lab"):
        sl = summary["skill_lab"]
        rows += [
            ("skill-lab runtime", sl["skill_lab_worker_runtime_arn"]),
            ("skill-lab image", sl["skill_lab_worker_image_tag"]),
            ("skill-lab venv", sl["python"]),
        ]
    width = max(len(k) for k, _ in rows)
    print("\n══ bootstrap summary ══")
    for key, value in rows:
        print(f"  {key:<{width}}  {value}")
    print(f"\nconfig written → {bs.CONFIG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
