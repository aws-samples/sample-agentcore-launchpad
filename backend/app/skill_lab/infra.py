"""Skill Lab infrastructure: exec-worker role/runtime + the dedicated venv.

Shared by both bootstrap paths (services.bootstrap.run_bootstrap for the hub,
workspace_bootstrap's skill_lab stage for registered workspaces). Every function
is idempotent; resource identifiers are returned for the caller to persist
(config/launchpad.yaml resources: / Workspace.resources). None of the keys are
in REQUIRED_RESOURCE_KEYS — a workspace without the worker simply shows Skill
Lab as unprovisioned.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import DATA_DIR, get_settings
from app.services import workspace_iam
from app.services.agent_iam import retry_iam_propagation
from app.services.agentcore import runtime as rt
from app.services.workspace import WorkspaceContext
from app.skill_lab import worker_build

WORKER_RUNTIME_NAME = "launchpad_skill_lab_worker"  # runtime names disallow hyphens
S3_PREFIX = "skill-lab"
EXEC_JOBS_PREFIX = f"{S3_PREFIX}/exec-jobs"
WORKER_LIFECYCLE = {"idleRuntimeSessionTimeout": 300, "maxLifetime": 28800}  # 5min/8h
SESSION_STORAGE_MOUNT = "/mnt/workspace"
VENV_DIR = DATA_DIR / "skill-lab-venv"
VENV_STAMP = VENV_DIR / ".requirements.sha256"
REQUIREMENTS_FILE = worker_build.VENDOR_ROOT / "requirements-launchpad.txt"


def worker_runtime_environment(region: str) -> dict[str, str]:
    """Env baked onto the runtime (upstream RUNTIME_ENV minus the codex vars)."""
    settings = get_settings()
    return {
        "AWS_REGION": region,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_MODEL": settings.skill_lab_target_model_id,
        "CLAUDE_CODE_EXEC_USE_SDK": "cli",
    }


def ensure_worker_role(
    workspace: WorkspaceContext,
    workspace_id: str,
    log: Callable[[str], None] = lambda _m: None,
) -> str:
    bucket = str(workspace.resources.get("artifacts_bucket") or "")
    repo_uri = str(workspace.resources.get("ecr_repo_uri") or "")
    repo = str(workspace.resources.get("ecr_repo") or "")
    if not (bucket and repo and repo_uri):
        raise RuntimeError(
            "artifacts_bucket/ecr_repo(_uri) missing from the resource map — "
            "run the base bootstrap first"
        )
    ecr_repo_arn = (
        f"arn:aws:ecr:{workspace.region}:{workspace.account_id}:repository/{repo}"
    )
    return workspace_iam.ensure_role(
        workspace.client("iam"),
        workspace_id=workspace_id,
        role_name=workspace_iam.regional_role_name(
            workspace_iam.SKILL_LAB_ROLE_BASE, workspace.region
        ),
        trust_policy=workspace_iam.service_trust_policy(
            "bedrock-agentcore.amazonaws.com", workspace.account_id
        ),
        inline_policies={
            "launchpad-skill-lab-worker": workspace_iam.skill_lab_worker_role_policy(
                bucket, ecr_repo_arn, workspace.account_id, s3_prefix=f"{S3_PREFIX}/"
            )
        },
        description="Assumed by the Launchpad Skill Lab exec-worker runtime",
        log=log,
    )


def _find_runtime(client: Any) -> dict[str, Any] | None:
    for summary in rt.list_runtimes(client):
        if summary.get("agentRuntimeName") == WORKER_RUNTIME_NAME:
            return summary
    return None


def ensure_worker_runtime(
    workspace: WorkspaceContext,
    *,
    image_uri: str,
    role_arn: str,
    log: Callable[[str], None] = lambda _m: None,
) -> dict[str, str]:
    """Create the worker runtime, or update it only when image/env drifted —
    an unconditional update would publish a new version (and reset managed
    session storage) on every bootstrap run."""
    from app.services.agentcore.client import control_client

    client = control_client(workspace)
    environment = worker_runtime_environment(workspace.region)
    filesystem = [{"sessionStorage": {"mountPath": SESSION_STORAGE_MOUNT}}]
    existing = _find_runtime(client)

    if existing is None:
        created = retry_iam_propagation(
            lambda: rt.create_container_runtime(
                client,
                runtime_name=WORKER_RUNTIME_NAME,
                container_uri=image_uri,
                role_arn=role_arn,
                environment=environment,
                filesystem_configurations=filesystem,
                lifecycle=WORKER_LIFECYCLE,
            ),
            log,
        )
        runtime_id = created["agentRuntimeId"]
        log(f"created runtime {WORKER_RUNTIME_NAME} · {runtime_id}")
    else:
        runtime_id = existing["agentRuntimeId"]
        detail = rt.get_runtime(client, runtime_id)
        current_uri = (
            (detail.get("agentRuntimeArtifact") or {})
            .get("containerConfiguration", {})
            .get("containerUri", "")
        )
        current_env = detail.get("environmentVariables") or {}
        in_sync = current_uri == image_uri and current_env == environment
        if in_sync and str(detail.get("status") or "") == "READY":
            log(f"runtime {WORKER_RUNTIME_NAME} up to date · {runtime_id}")
            arn = detail.get("agentRuntimeArn", existing.get("agentRuntimeArn", ""))
            return {"runtime_id": runtime_id, "runtime_arn": arn}
        if in_sync:
            # Image and env already match but the runtime is not READY: an
            # interrupted (or failed) create/update from a previous run. Waiting
            # below either sees it through or raises — returning the ARN here
            # would report a broken runtime as provisioned.
            log(f"runtime {WORKER_RUNTIME_NAME} is {detail.get('status')} · waiting")
        else:
            rt.update_container_runtime(
                client,
                runtime_id=runtime_id,
                container_uri=image_uri,
                role_arn=role_arn,
                environment=environment,
                filesystem_configurations=filesystem,
                lifecycle=WORKER_LIFECYCLE,
            )
            log(f"updated runtime {WORKER_RUNTIME_NAME} · {runtime_id} (new version)")

    detail = rt.wait_runtime_ready(
        client,
        runtime_id,
        on_status=lambda s: log(f"runtime status: {s}"),
    )
    return {
        "runtime_id": runtime_id,
        "runtime_arn": detail.get("agentRuntimeArn", ""),
    }


def ensure_skill_lab_venv(log: Callable[[str], None] = lambda _m: None) -> str:
    """Dedicated interpreter for the vendored CLIs (host-local, uv-managed).

    Idempotent via a stamp of the requirements file; a missing uv binary is a
    hard error on purpose — all repo Python runs through uv.
    """
    if not REQUIREMENTS_FILE.is_file():
        raise FileNotFoundError(f"missing {REQUIREMENTS_FILE} — vendor tree incomplete")
    settings = get_settings()
    python_path = Path(settings.skill_lab_python)
    wanted = hashlib.sha256(REQUIREMENTS_FILE.read_bytes()).hexdigest()
    if python_path.exists() and VENV_STAMP.exists() and VENV_STAMP.read_text().strip() == wanted:
        log(f"skill-lab venv up to date · {VENV_DIR}")
        return str(python_path)
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found on PATH — required to provision the skill-lab venv")
    VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    # --clear: a requirements change recreates the env from scratch (it is
    # derived state; uv refuses to reuse an existing dir without it).
    subprocess.run([uv, "venv", str(VENV_DIR), "--python", "3.12", "--clear"], check=True)
    subprocess.run(
        [uv, "pip", "install", "--python", str(python_path), "-r", str(REQUIREMENTS_FILE)],
        check=True,
    )
    _resolve_interpreter_symlinks(log)
    VENV_STAMP.write_text(wanted + "\n", encoding="utf-8")
    log(f"skill-lab venv provisioned · {VENV_DIR}")
    return str(python_path)


def _resolve_interpreter_symlinks(log: Callable[[str], None]) -> None:
    """Re-point the venv's absolute interpreter symlinks at their final target.

    A uv-MANAGED interpreter (host without a system python3.12) links the venv
    python through uv's version-less alias dir (cpython-3.12-… → the versioned
    cpython-3.12.13-… install). The agentic judge's bwrap sandbox binds the
    RESOLVED runtime dirs (sys.base_prefix) but knows nothing about the alias,
    so the venv symlink dangles inside the sandbox and the boundary probe dies
    with "prlimit: failed to execute …/bin/python: No such file or directory"
    (live on the us-east-1 prod box, 2026-08-20). Relative intra-venv links
    (python3 → python) are left alone.
    """
    for candidate in (VENV_DIR / "bin").iterdir():
        if not candidate.is_symlink():
            continue
        raw = os.readlink(candidate)
        resolved = candidate.resolve()
        if os.path.isabs(raw) and str(resolved) != raw:
            candidate.unlink()
            candidate.symlink_to(resolved)
            log(f"resolved venv symlink {candidate.name} → {resolved}")


def ensure_skill_lab_worker(
    workspace: WorkspaceContext,
    workspace_id: str,
    log: Callable[[str], None] = lambda _m: None,
) -> dict[str, str]:
    """Role → image → runtime; returns the resource keys to persist."""
    role_arn = ensure_worker_role(workspace, workspace_id, log)
    image = worker_build.ensure_worker_image(workspace, log)
    runtime = ensure_worker_runtime(
        workspace, image_uri=image["uri"], role_arn=role_arn, log=log
    )
    return {
        "skill_lab_worker_role_arn": role_arn,
        "skill_lab_worker_image_tag": image["tag"],
        "skill_lab_worker_image_digest": image["digest"],
        "skill_lab_worker_runtime_id": runtime["runtime_id"],
        "skill_lab_worker_runtime_arn": runtime["runtime_arn"],
    }
