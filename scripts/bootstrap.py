#!/usr/bin/env python3
"""Launchpad bootstrap CLI.

Deploys the CDK app (`launchpad-base[-env]` + the shared, unsuffixed
`launchpad-memory-domain` + `launchpad-memory[-env]`) when any of the three
stacks is missing, then idempotently ensures the AgentCore shared resources
(Gateway, Transaction Search) and writes config/launchpad.yaml. Registry has
no AWS footprint; Memory/KB ride the memory stacks (D17: ONE OpenSearch domain
per account+region in `launchpad-memory-domain`, created by the first
environment and found unchanged by every later one; SQS + Lambda per
environment). The self-hosted Policy module (D20) adds one idempotent `policy`
stage: the CloudWatch decisions log group `/launchpad/policy/decisions[-env]`.

Run from the backend venv so the app package resolves:
    cd backend && uv run python ../scripts/bootstrap.py [--skip-cdk | --check-memory-admin]
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from botocore.exceptions import ClientError  # noqa: E402

from app.services import bootstrap as bs  # noqa: E402
from app.services import memory_bootstrap as mb  # noqa: E402
from app.services.workspace import WorkspaceContext  # noqa: E402

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


def stack_exists(region: str, name: str | None = None) -> bool:
    """`name=None` means this environment's base stack (`bs.base_stack_name()`)."""
    try:
        bs.get_stack_outputs(region, name or bs.base_stack_name())
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ValidationError":
            return False
        raise


def cdk_context_args() -> list[str]:
    """Repeated `cdk --context k=v` args from `LAUNCHPAD_CDK_CONTEXT`, plus the
    `env_name` the backend runs with (N11) — see `app.services.bootstrap.cdk_context_args`,
    which `scripts/teardown.py` shares so deploy and destroy address the same stacks."""
    return bs.cdk_context_args()


def expected_stacks() -> tuple[str, ...]:
    """The CDK stacks this environment needs, in `cdk deploy --all` order: base, the
    shared domain (D17 — unsuffixed, one per account+region, idempotent across
    environments), this environment's memory stack."""
    return (bs.base_stack_name(), mb.memory_domain_stack_name(), mb.memory_stack_name())


def missing_stacks(region: str) -> list[str]:
    return [name for name in expected_stacks() if not stack_exists(region, name)]


def precheck_memory_admin(region: str) -> int:
    """N11-obs44b: before any `cdk deploy`, can this caller assume the memory admin role
    (`launchpad-memory-admin-<region>`)? Prints one `memory-admin precondition:` line.
    Returns 1 on `denied` (the caller fails fast with the same reason the memory stage
    would record after the deploy), 0 on `ok` and on `unknown` (a WARNING, never a
    guess that blocks). `scripts/host-update.sh --with-cdk` runs it through
    `--check-memory-admin`; `main()` runs it itself before `deploy_cdk`."""
    sts = bs._client("sts", region)
    iam = bs._client("iam", region)
    identity = sts.get_caller_identity()
    account_id = str(identity["Account"])
    caller_arn = str(identity.get("Arn") or "")
    hub = WorkspaceContext(account_id=account_id, region=region)
    admin_role_arn = hub.arn("iam", f"role/{mb.memory_admin_role_name(region)}", region="")
    status, detail = mb.check_memory_admin_assume(
        sts, iam,
        admin_role_arn=admin_role_arn,
        caller_arn=caller_arn,
        domain_exists=stack_exists(region, mb.memory_domain_stack_name()),
    )
    print(f"── memory-admin precondition: {status} — {detail}", flush=True)
    if status == mb.PRECHECK_DENIED:
        if mb.caller_role_name(caller_arn) is None:
            # an off-host operator (IAM user, root): no EC2 stack owns its policy
            print(
                "aborting before cdk deploy: grant sts:AssumeRole on "
                f"role/{mb.memory_admin_role_name(region)} to {caller_arn or '<caller>'} "
                "(deploy.md §8.3), then re-run (N11-obs44b)",
                flush=True,
            )
        else:
            print(
                "aborting before cdk deploy: update the EC2 stack to the current template "
                "(deploy.md §8.2) so the ops role carries `MemoryAdminAssume`, then re-run "
                "(N11-obs44b)",
                flush=True,
            )
        return 1
    if status == mb.PRECHECK_UNKNOWN:
        print(
            "WARNING: memory-admin precondition undecided — continuing; if the memory "
            "stage then reports `cannot assume the memory admin role`, see deploy.md §8.3",
            flush=True,
        )
    return 0


def deploy_cdk(missing: list[str] | None = None) -> None:
    which = ", ".join(missing) if missing else " and/or ".join(expected_stacks())
    print(f"── stack(s) {which} not found — running cdk deploy --all…")
    subprocess.run(
        ["uv", "run", "cdk", "deploy", "--all", "--require-approval", "never", *cdk_context_args()],
        cwd=REPO_ROOT / "infra",
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap AgentCore Launchpad shared infra")
    parser.add_argument("--region", default=None, help="AWS region (default: settings)")
    parser.add_argument("--skip-cdk", action="store_true", help="never invoke cdk deploy")
    parser.add_argument(
        "--check-memory-admin", action="store_true",
        help="only run the --with-cdk precondition (N11-obs44b): exit 1 when this caller "
             "cannot assume launchpad-memory-admin-<region>; nothing is deployed or written",
    )
    args = parser.parse_args()

    region = args.region or bs.get_settings().region
    if args.check_memory_admin:
        return precheck_memory_admin(region)

    agentcore_cli_version = ensure_agentcore_cli()
    missing = missing_stacks(region)
    if missing:
        legacy_memory_layout = (
            bs.get_settings().memory_domain_mode == "shared"
            and missing == [mb.memory_domain_stack_name()]
            and stack_exists(region, mb.memory_stack_name())
        )
        if args.skip_cdk and legacy_memory_layout:
            # Pre-D17 environment: the per-env memory stack still owns (and exports) its
            # own OpenSearch domain. Code-only updates must keep working until the
            # shared-domain migration (deploy.md) is run deliberately with cdk.
            print(
                f"── {mb.memory_domain_stack_name()} missing but {mb.memory_stack_name()} "
                "exists: legacy memory layout (pre-D17) — continuing without cdk; run the "
                "shared-domain migration from docs/cn/interfaces/deploy.md when scheduled",
                flush=True,
            )
        elif args.skip_cdk:
            print(
                f"stack(s) {', '.join(missing)} missing and --skip-cdk given — aborting",
                flush=True,
            )
            return 1
        else:
            # N11-obs44b: a D17 migration / first deploy fails fast here, not 16 min later
            # in the memory stage, when the caller cannot assume the memory admin role.
            if precheck_memory_admin(region) != 0:
                return 1
            deploy_cdk(missing)

    summary = bs.run_bootstrap(region)

    rows = [
        ("account", summary["account_id"]),
        ("region", summary["region"]),
        ("agentcore CLI", agentcore_cli_version),
        ("artifacts bucket", summary["stack_outputs"]["ArtifactsBucketName"]),
        ("ecr repo", summary["stack_outputs"]["EcrRepoUri"]),
        ("codebuild", summary["stack_outputs"]["CodeBuildProjectName"]),
    ]
    if summary.get("memory"):
        mem = summary["memory"]
        rows += [
            ("memory domain", mem["endpoint"] or mem["unavailable_reason"]),
            ("memory stage", f"{len(mem['created'])} created · {len(mem['reused'])} reused"),
        ]
        if mem.get("warning"):  # N-SCN-obs6: healthy substrate, one provisioning step skipped
            rows.append(("memory stage WARN", mem["warning"]))
        if mem.get("unavailable_reason"):
            # N11-obs44c: a degraded stage is a WARN row, not an exit code — the stages
            # after memory ran; the console answers memory.not_provisioned until the cause
            # is removed and `make bootstrap` is re-run (deploy.md §8.3)
            rows.append((
                "memory stage DEGRADED",
                f"{mem['unavailable_reason']} — the stages after memory ran; fix the cause "
                "and re-run `make bootstrap` (N11-obs44c)",
            ))
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
    if summary.get("policy"):
        # D20 P1-d: self-hosted Cedar Policy — engine / default mode / ACTIVE count, and
        # the CloudWatch decisions log group the audit sink mirrors into
        pol = summary["policy"]
        if pol.get("skipped"):
            rows.append(("policy", "skipped · feature off"))
        else:
            rows.append((
                "policy",
                f"engine {pol.get('engine') or 'unknown'} · mode {pol.get('mode')} · "
                f"{pol.get('active_policies', 0)} active",
            ))
            if pol.get("unavailable_reason"):
                rows.append((
                    "policy decisions log WARN",
                    f"{pol.get('log_group')} not ensured ({pol['unavailable_reason']}) — "
                    "decisions stay in the ledger until the grant is fixed and "
                    "`make bootstrap` re-runs",
                ))
            else:
                rows.append((
                    "policy decisions log",
                    f"{pol.get('log_group')} · "
                    f"{'created' if pol.get('created') else 'reused'} · "
                    f"retention {pol.get('retention_days')} d",
                ))
    if summary.get("skill_lab"):
        sl = summary["skill_lab"]
        rows += [
            ("skill-lab runtime", sl["skill_lab_worker_runtime_arn"]),
            ("skill-lab image", sl["skill_lab_worker_image_tag"]),
            ("skill-lab venv", sl["python"]),
        ]
    if summary.get("evaluation"):
        ev = summary["evaluation"]
        if ev.get("installed"):
            rows += [
                ("eval worker", ev.get("runtime_arn") or ev.get("agent_id") or "installed"),
                ("eval worker release", ev.get("release") or "unknown"),
                ("eval worker state", "installed" if ev.get("changed") else "reused"),
            ]
        else:
            reason = ev.get("unavailable_reason") or "not installed"
            rows.append(("eval worker", f"unavailable · {reason}"))
    width = max(len(k) for k, _ in rows)
    print("\n══ bootstrap summary ══")
    for key, value in rows:
        print(f"  {key:<{width}}  {value}")
    print(f"\nconfig written → {bs.CONFIG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
