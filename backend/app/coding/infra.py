"""Coding runtime infrastructure: its IAM role, its image and its AgentCore Runtime.

One platform-owned runtime per workspace (account × region), installed only on an
administrator's explicit request (``install.py``) — never on startup or on a read.
Mirrors ``skill_lab.infra``: every function is idempotent and returns the resource
identifiers for the caller to persist (``merge_workspace_resources``); none of the
keys are in ``REQUIRED_RESOURCE_KEYS``, so a workspace without the runtime just
shows the coding agent as not installed.

Two deliberate differences from the Skill Lab worker:

* **Drift covers everything the runtime is configured with** — image, env, role,
  session-storage mount, lifecycle and network mode. An update publishes a new
  version, and a new version wipes every project's session storage once its live
  session ends, so an update must happen exactly when something changed (the
  snapshot restore brings the files back, but it is not free).
* **Updates always echo ``filesystemConfigurations``** — UpdateAgentRuntime treats
  an omitted list as "detach every mount" (probed live 2026-10-04).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.core.errors import is_not_found
from app.services import workspace_iam
from app.services.agent_iam import retry_iam_propagation
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.workspace import WorkspaceContext

from . import image_build

RUNTIME_NAME = "launchpad_coding_runtime"  # runtime names disallow hyphens
ROLE_POLICY_NAME = "launchpad-coding-runtime"
SESSION_STORAGE_MOUNT = "/mnt/workspace"
# 15-minute idle reaper (a busy turn keeps /ping HealthyBusy) and the 8-hour
# microVM lifetime that bounds one turn.
LIFECYCLE = {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}
FILESYSTEM = [{"sessionStorage": {"mountPath": SESSION_STORAGE_MOUNT}}]
NETWORK_MODE = "PUBLIC"

# Workspace resource keys (``workspace.resources``). The backend reads the runtime
# ARN from KEY_RUNTIME_ARN; import these rather than spelling the strings.
KEY_ROLE_ARN = "coding_runtime_role_arn"
KEY_IMAGE_TAG = "coding_runtime_image_tag"
KEY_IMAGE_DIGEST = "coding_runtime_image_digest"
KEY_RUNTIME_ID = "coding_runtime_id"
KEY_RUNTIME_ARN = "coding_runtime_arn"
KEY_MODELS = "coding_runtime_models"  # the models the role + container allow
RESOURCE_KEYS = (
    KEY_ROLE_ARN, KEY_IMAGE_TAG, KEY_IMAGE_DIGEST, KEY_RUNTIME_ID, KEY_RUNTIME_ARN, KEY_MODELS,
)

RUNTIME_GONE_TIMEOUT_S = 600
POLL_S = 5
_sleep = time.sleep  # injectable in tests

Log = Callable[[str], None]


def _nolog(_msg: str) -> None:
    return None


def role_name(region: str) -> str:
    return workspace_iam.regional_role_name(workspace_iam.CODING_RUNTIME_ROLE_BASE, region)


def coding_models() -> list[str]:
    """The configured coding models, de-duplicated, order kept."""
    seen: list[str] = []
    for model in get_settings().coding_models:
        model = str(model).strip()
        if model and model not in seen:
            seen.append(model)
    if not seen:
        raise RuntimeError("settings.coding_models is empty — configure at least one model")
    return seen


def runtime_environment(image_tag: str, models: list[str]) -> dict[str, str]:
    """Env baked onto the runtime; the container validates turns against it."""
    return {
        "CODING_MODELS": ",".join(models),
        "CODING_EFFORTS": ",".join(get_settings().coding_efforts),
        "CODING_RUNTIME_VERSION": image_tag,
    }


def _ecr_repo_arn(workspace: WorkspaceContext) -> str:
    repo = str(workspace.resources.get("ecr_repo") or "")
    if not repo:
        raise RuntimeError("ecr_repo missing from the resource map — run the base bootstrap first")
    return f"arn:aws:ecr:{workspace.region}:{workspace.account_id}:repository/{repo}"


def ensure_role(
    workspace: WorkspaceContext, workspace_id: str, models: list[str], log: Log = _nolog
) -> str:
    """Create or reconcile the runtime's execution role (inline policy rewritten
    every time, so a changed model list lands on a repair)."""
    return workspace_iam.ensure_role(
        workspace.client("iam"),
        workspace_id=workspace_id,
        role_name=role_name(workspace.region),
        trust_policy=workspace_iam.service_trust_policy(
            "bedrock-agentcore.amazonaws.com", workspace.account_id
        ),
        inline_policies={
            ROLE_POLICY_NAME: workspace_iam.coding_runtime_role_policy(
                workspace.account_id, workspace.region, _ecr_repo_arn(workspace), models
            )
        },
        description="Assumed by the Launchpad coding runtime (model invocation only)",
        log=log,
    )


def find_runtime(client: Any) -> dict[str, Any] | None:
    for summary in rt.list_runtimes(client):
        if summary.get("agentRuntimeName") == RUNTIME_NAME:
            return summary
    return None


def _mount_paths(filesystems: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in filesystems or []:
        for kind, cfg in (entry or {}).items():
            out.append((kind, str((cfg or {}).get("mountPath") or "").rstrip("/")))
    return sorted(out)


def drift(
    detail: dict[str, Any], *, image_uri: str, role_arn: str, environment: dict[str, str]
) -> list[str]:
    """What differs between a GetAgentRuntime answer and the wanted runtime
    (empty = in sync). Pure, so the decision is unit-tested on its own."""
    reasons: list[str] = []
    artifact = (detail.get("agentRuntimeArtifact") or {}).get("containerConfiguration") or {}
    if artifact.get("containerUri", "") != image_uri:
        reasons.append("image")
    if (detail.get("environmentVariables") or {}) != environment:
        reasons.append("environment")
    if str(detail.get("roleArn") or "") != role_arn:
        reasons.append("role")
    if _mount_paths(detail.get("filesystemConfigurations")) != _mount_paths(FILESYSTEM):
        reasons.append("filesystem")
    lifecycle = detail.get("lifecycleConfiguration") or {}
    if any(lifecycle.get(k) != v for k, v in LIFECYCLE.items()):
        reasons.append("lifecycle")
    network = (detail.get("networkConfiguration") or {}).get("networkMode") or NETWORK_MODE
    if network != NETWORK_MODE:
        reasons.append("network")
    return reasons


def ensure_runtime(
    workspace: WorkspaceContext,
    *,
    image_uri: str,
    role_arn: str,
    environment: dict[str, str],
    log: Log = _nolog,
    wait_log: Log | None = None,
) -> dict[str, str]:
    """Create the runtime, or update it only when its configuration drifted."""
    client = control_client(workspace)
    existing = find_runtime(client)
    if existing is None:
        created = retry_iam_propagation(
            lambda: rt.create_container_runtime(
                client,
                runtime_name=RUNTIME_NAME,
                container_uri=image_uri,
                role_arn=role_arn,
                environment=environment,
                filesystem_configurations=FILESYSTEM,
                lifecycle=LIFECYCLE,
            ),
            log,
        )
        runtime_id = created["agentRuntimeId"]
        log(f"created runtime {RUNTIME_NAME} · {runtime_id}")
    else:
        runtime_id = existing["agentRuntimeId"]
        detail = rt.get_runtime(client, runtime_id)
        status = str(detail.get("status") or "")
        reasons = drift(detail, image_uri=image_uri, role_arn=role_arn, environment=environment)
        if not reasons and status == "READY":
            log(f"runtime {RUNTIME_NAME} up to date · {runtime_id}")
            arn = detail.get("agentRuntimeArn") or existing.get("agentRuntimeArn", "")
            return {"runtime_id": runtime_id, "runtime_arn": arn}
        if not reasons:
            # in sync but not READY: an interrupted (or failed) create/update of a
            # previous run — the wait below sees it through or raises
            log(f"runtime {RUNTIME_NAME} is {status} · waiting")
        else:
            retry_iam_propagation(
                lambda: rt.update_container_runtime(
                    client,
                    runtime_id=runtime_id,
                    container_uri=image_uri,
                    role_arn=role_arn,
                    environment=environment,
                    filesystem_configurations=FILESYSTEM,  # omitted = mounts detached
                    lifecycle=LIFECYCLE,
                ),
                log,
            )
            log(f"updated runtime {RUNTIME_NAME} · {runtime_id} ({', '.join(reasons)} changed; "
                "new version — project files come back from their snapshots)")
    ready = rt.wait_runtime_ready(
        client, runtime_id, on_status=lambda s: (wait_log or log)(f"runtime status: {s}")
    )
    return {"runtime_id": runtime_id, "runtime_arn": ready.get("agentRuntimeArn", "")}


def ensure_coding_runtime(
    workspace: WorkspaceContext,
    workspace_id: str,
    log: Callable[[str, str], None] = lambda _stage, _msg: None,
) -> dict[str, Any]:
    """Role → image → runtime; returns the resource keys to persist.
    ``log(stage, message)`` with stage in role | image | runtime | wait."""
    models = coding_models()
    role_arn = ensure_role(workspace, workspace_id, models, lambda m: log("role", m))
    image = image_build.ensure_image(workspace, lambda m: log("image", m))
    runtime = ensure_runtime(
        workspace,
        image_uri=image["uri"],
        role_arn=role_arn,
        environment=runtime_environment(image["tag"], models),
        log=lambda m: log("runtime", m),
        wait_log=lambda m: log("wait", m),
    )
    return {
        KEY_ROLE_ARN: role_arn,
        KEY_IMAGE_TAG: image["tag"],
        KEY_IMAGE_DIGEST: image["digest"],
        KEY_RUNTIME_ID: runtime["runtime_id"],
        KEY_RUNTIME_ARN: runtime["runtime_arn"],
        KEY_MODELS: models,
    }


# ─── uninstall ───────────────────────────────────────────────────────────────


def _wait_runtime_gone(client: Any, runtime_id: str, log: Log) -> None:
    deadline = time.monotonic() + RUNTIME_GONE_TIMEOUT_S
    last = None
    while True:
        try:
            status = str(rt.get_runtime(client, runtime_id).get("status") or "")
        except Exception as exc:  # noqa: BLE001 — typed not-found is client-specific
            if is_not_found(exc):
                log(f"runtime {runtime_id} deleted")
                return
            raise
        if status != last:
            log(f"runtime status: {status}")
            last = status
        if status == "DELETE_FAILED":
            raise RuntimeError(f"runtime {runtime_id} entered DELETE_FAILED")
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"runtime {runtime_id} still {status} after {RUNTIME_GONE_TIMEOUT_S}s — run "
                "uninstall again"
            )
        _sleep(POLL_S)


def delete_role(iam: Any, name: str, workspace_id: str, log: Log = _nolog) -> bool:
    """Delete the runtime role if it is this workspace's; False when it is absent
    or not ours (an untagged role of the same name belongs to someone else)."""
    try:
        tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=name).get("Tags", [])}
    except Exception as exc:  # noqa: BLE001
        if is_not_found(exc):
            log(f"role {name} already gone")
            return False
        raise
    owner = tags.get(workspace_iam.WORKSPACE_TAG_KEY)
    if owner != workspace_id:
        log(f"role {name} is not this workspace's ({workspace_iam.WORKSPACE_TAG_KEY}={owner!r}) "
            "— left in place")
        return False
    for policy in iam.list_role_policies(RoleName=name).get("PolicyNames", []):
        iam.delete_role_policy(RoleName=name, PolicyName=policy)
    for attached in iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies", []):
        iam.detach_role_policy(RoleName=name, PolicyArn=attached["PolicyArn"])
    iam.delete_role(RoleName=name)
    log(f"deleted role {name}")
    return True


def uninstall_coding_runtime(
    workspace: WorkspaceContext,
    workspace_id: str,
    log: Callable[[str, str], None] = lambda _stage, _msg: None,
) -> dict[str, None]:
    """Runtime first (and wait until it is gone — deleting the role under a live
    runtime can wedge the runtime's own deletion), then the role. Project
    snapshots under ``coding/`` in the artifacts bucket are kept: a re-install
    restores every project. Returns the resource keys to clear."""
    client = control_client(workspace)
    existing = find_runtime(client)
    if existing is None:
        log("runtime", f"no runtime named {RUNTIME_NAME}")
    else:
        runtime_id = existing["agentRuntimeId"]
        try:
            rt.delete_runtime(client, runtime_id)
            log("runtime", f"deleting runtime {RUNTIME_NAME} · {runtime_id}")
        except Exception as exc:  # noqa: BLE001
            if not is_not_found(exc):
                raise
        _wait_runtime_gone(client, runtime_id, lambda m: log("wait", m))
    delete_role(workspace.client("iam"), role_name(workspace.region), workspace_id,
                lambda m: log("role", m))
    return {key: None for key in RESOURCE_KEYS}
