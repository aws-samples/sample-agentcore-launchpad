"""Bring Your Own Code (byoc) — deploy user-written agent code to Runtime.

    generate  → load the staged upload's manifest (or describe the ECR image)
                and stamp server-verified provenance onto the spec
    package   → code_zip: download → safe-extract → verify entrypoint →
                resolve requirements.txt for linux/aarch64 → zip → S3
                container_source: download → verify Dockerfile → CodeBuild → ECR
                container_image: verify the image exists in this account+region
    provision → per-agent least-privilege IAM execution role
    deploy    → CreateAgentRuntime (codeConfiguration or containerConfiguration)
                + poll READY; re-publish → UpdateAgentRuntime (new version)
    register  → the shared A2A registry record stage

The platform NEVER executes the uploaded code on the Launchpad host: package
work is archive extraction, file checks and a pip *download/install into the
bundle directory* (wheels are unpacked, never imported or run).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.runtime_target import pip_platform_args
from app.deployer.environment import runtime_environment
from app.deployer.pipeline import StageContext, StageResult, register_method
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec, ByocConfig, parse_ecr_image_uri
from app.services import agent_iam, byoc_uploads
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.requirements_txt import (
    RESOLVE_FIX_HINTS,
    RequirementsFileError,
    parse_requirements_txt,
    pip_python_version,
    summarize_resolver_failure,
)
from app.services.workspace import WorkspaceContext

from .container import (
    _image_ref,
    _recorded_digest_uri,
    build_and_push_image,
    platform_buildspec_path,
)
from .zip_runtime import _compile_lock, sanitize_runtime_name

PACKAGE_KEY_TMPL = "agents/{name}/byoc_package.zip"


def _config(spec: AgentSpec) -> ByocConfig:
    if spec.byoc is None:  # schema guarantees this; belt for hand-built rows
        raise RuntimeError("byoc spec has no byoc settings block")
    return spec.byoc


# PYTHON_3_13 → 3.13 (the shape pip/uv take); shared with the upload pre-resolve
_pip_python_version = pip_python_version


def _requirements_lines(path: Path) -> list[str]:
    """The zip's requirements entries, per the pip file format (continuations,
    comments, markers) and the platform's supply-chain boundary (no includes,
    no URLs/VCS/paths, no index options — see `services/requirements_txt`).
    `--hash` options are dropped: the platform re-locks against its own deploy
    target and generates fresh hashes."""
    try:
        return parse_requirements_txt(path.read_text(encoding="utf-8"))
    except RequirementsFileError as exc:
        raise RuntimeError(f"the zip's requirements.txt was refused — {exc}") from exc


def _stamp_provenance(ctx: StageContext, agent: Agent, provenance: dict[str, Any]) -> None:
    """Persist server-verified provenance into the row's spec (idempotent).

    Overwrites whatever the client sent — provenance is server-owned; the spec
    field only exists so the console can render it back."""
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        spec = dict(row.spec)
        spec["byoc"] = {**(spec.get("byoc") or {}), "provenance": provenance}
        row.spec = spec
        db.commit()
        agent.spec = spec
    finally:
        db.close()


def _stage_generate(ctx: StageContext, agent: Agent) -> StageResult:
    """No code to generate — verify the artifact reference and stamp provenance."""
    spec = AgentSpec(**agent.spec)
    cfg = _config(spec)

    if cfg.artifact_kind == "container_image":
        digest, pushed_at = describe_image(ctx.workspace, cfg.image_uri or "")
        provenance = {
            "sha256": digest,
            "size_bytes": 0,
            "original_filename": cfg.image_uri or "",
            "uploaded_by": "",
            "uploaded_at": pushed_at,
        }
        _stamp_provenance(ctx, agent, provenance)
        ctx.log(f"ECR image verified · {cfg.image_uri} · {digest}")
        return StageResult(detail=f"container_image · {digest[:19]}…")

    manifest = byoc_uploads.get_manifest(ctx.workspace, cfg.upload_id or "")
    provenance = {
        "sha256": manifest.get("sha256", ""),
        "size_bytes": manifest.get("size_bytes", 0),
        "original_filename": manifest.get("original_filename", ""),
        "uploaded_by": manifest.get("uploaded_by", ""),
        "uploaded_at": manifest.get("uploaded_at", ""),
    }
    _stamp_provenance(ctx, agent, provenance)
    detected = manifest.get("detected") or {}
    if cfg.artifact_kind == "code_zip" and not detected.get("agentcore_sdk_detected"):
        ctx.log(
            "note: no BedrockAgentCoreApp/@app.entrypoint marker found — the "
            "entrypoint must serve POST /invocations + GET /ping on :8080 itself"
        )
    ctx.log(
        f"{cfg.artifact_kind} · {provenance['original_filename']} · "
        f"{provenance['size_bytes'] / 1e6:.1f}MB · sha256 {provenance['sha256'][:12]} · "
        f"uploaded by {provenance['uploaded_by'] or 'unknown'}"
    )
    return StageResult(
        detail=f"{cfg.artifact_kind} · sha256 {provenance['sha256'][:12]}…"
    )


def describe_image(
    workspace: WorkspaceContext, image_uri: str, ecr_client: Any = None
) -> tuple[str, str]:
    """(imageDigest, imagePushedAt ISO) of a private ECR image in THIS
    account+region — refuses other accounts/regions and missing images."""
    parsed = parse_ecr_image_uri(image_uri)
    if parsed is None:
        raise RuntimeError(f"'{image_uri}' is not a private ECR image URI")
    account_id, region = parsed
    if account_id != workspace.account_id or region != workspace.region:
        raise RuntimeError(
            f"image {image_uri} lives in {account_id}/{region}; this workspace "
            f"deploys from {workspace.account_id}/{workspace.region} only"
        )
    rest = image_uri.split(".amazonaws.com/", 1)[1]
    if "@sha256:" in rest:
        repo, _, ref = rest.partition("@")
        image_id = {"imageDigest": ref}
    else:
        repo, _, ref = rest.rpartition(":")
        image_id = {"imageTag": ref}
    ecr = ecr_client or workspace.client("ecr")
    detail = ecr.describe_images(repositoryName=repo, imageIds=[image_id])
    images = detail.get("imageDetails", [])
    if not images:
        raise RuntimeError(f"image {image_uri} not found in ECR")
    pushed = images[0].get("imagePushedAt")
    pushed_at = pushed.isoformat() if hasattr(pushed, "isoformat") else str(pushed or "")
    return images[0].get("imageDigest", ""), pushed_at


def resolve_requirements_into(
    src_root: Path,
    build_dir: Path,
    python_version: str,
    log: Any,
    pip_runner: Any = subprocess.run,
    compile_runner: Any = None,
) -> int:
    """Hash-locked install of the zip's requirements.txt into the bundle root
    for the Runtime target (linux/aarch64). Returns the locked package count;
    0 when there is nothing to resolve. Wheels are unpacked, never executed."""
    req_file = src_root / "requirements.txt"
    if not req_file.exists():
        return 0
    requirements = _requirements_lines(req_file)
    if not requirements:
        return 0
    pip_version = _pip_python_version(python_version)
    lock = _compile_lock(
        requirements, build_dir, compile_runner or pip_runner, python_version=pip_version
    )
    locked = [
        line for line in lock.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.lstrip().startswith("#")
    ]
    log(f"requirements locked · {len(locked)} packages pinned with hashes")
    proc = pip_runner(
        [
            sys.executable, "-m", "pip", "install",
            "--require-hashes", "-r", str(lock),
            "-t", str(src_root),
            *pip_platform_args(),
            "--only-binary=:all:",
            "--python-version", pip_version,
            "--quiet",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "pip install of the zip's locked requirements failed: "
            + summarize_resolver_failure(
                proc.stderr or "", python_version=pip_version, hints=RESOLVE_FIX_HINTS
            )
        )
    # the lock ships inside the artifact — the record of what was installed
    shutil.copy2(lock, src_root / "requirements.lock")
    return len(locked)


def _zip_tree(src_root: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src_root):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith(".pyc"):
                    continue
                full = Path(root) / name
                zf.write(full, full.relative_to(src_root))


def _package_code_zip(
    ctx: StageContext, agent: Agent, cfg: ByocConfig, bucket: str
) -> StageResult:
    # Agent names are only unique within a workspace. Each attempt owns its
    # directory, including while another job is resolving dependencies.
    with tempfile.TemporaryDirectory(prefix="launchpad_byoc_") as tmp:
        return _build_code_zip(ctx, agent, cfg, bucket, Path(tmp))


def _build_code_zip(
    ctx: StageContext, agent: Agent, cfg: ByocConfig, bucket: str, build_dir: Path
) -> StageResult:
    zip_path = build_dir / "upload.zip"
    byoc_uploads.download_upload(ctx.workspace, agent.workspace_id, cfg.upload_id or "",
                                 zip_path)
    src_root = byoc_uploads.extract_zip(zip_path, build_dir / "src")

    entry = src_root / cfg.entrypoint
    if not entry.is_file():
        raise RuntimeError(
            f"entrypoint '{cfg.entrypoint}' not found in the uploaded zip — "
            "pick one of the detected candidates or re-upload"
        )

    t0 = time.monotonic()
    installed = 0
    if cfg.install_requirements:
        installed = resolve_requirements_into(
            src_root, build_dir, cfg.python_version, ctx.log
        )
        if installed == 0:
            ctx.log("no requirements.txt in the zip — bundle shipped as uploaded")
    else:
        ctx.log("install_requirements=false — bundle shipped as uploaded")

    final_zip = build_dir / "deployment_package.zip"
    _zip_tree(src_root, final_zip)
    size_mb = final_zip.stat().st_size / 1e6
    secs = time.monotonic() - t0

    s3_key = PACKAGE_KEY_TMPL.format(name=agent.name)
    ctx.workspace.client("s3").upload_file(str(final_zip), bucket, s3_key)
    ctx.scratch["s3_bucket"], ctx.scratch["s3_key"] = bucket, s3_key
    ctx.log(f"package {secs:.1f}s · {size_mb:.1f}MB → s3://{bucket}/{s3_key}")
    detail = f"code_zip · {size_mb:.1f}MB · s3 ✓"
    if installed:
        detail += f" · {installed} deps resolved (aarch64)"
    return StageResult(detail=detail)


def _package_container_source(
    ctx: StageContext, agent: Agent, cfg: ByocConfig
) -> StageResult:
    with tempfile.TemporaryDirectory(prefix="launchpad_byoc_") as tmp:
        return _build_container_source(ctx, agent, cfg, Path(tmp))


def _build_container_source(
    ctx: StageContext, agent: Agent, cfg: ByocConfig, build_dir: Path
) -> StageResult:
    zip_path = build_dir / "upload.zip"
    byoc_uploads.download_upload(ctx.workspace, agent.workspace_id, cfg.upload_id or "",
                                 zip_path)
    # extract_zip normalizes a single top-level dir, so a `zip -r ctx.zip myagent/`
    # upload still presents its Dockerfile at the build-context root
    src_root = byoc_uploads.extract_zip(zip_path, build_dir / "src")
    if not (src_root / "Dockerfile").is_file():
        raise RuntimeError(
            "no Dockerfile at the zip root — a container_source upload must "
            "carry the docker build context (Dockerfile + code)"
        )
    # CodeBuild reads buildspec.yml from the source zip; the platform owns the
    # build recipe, so this overwrites any buildspec the member uploaded —
    # their Dockerfile is the only build input they control.
    if (src_root / "buildspec.yml").exists():
        ctx.log("upload carries its own buildspec.yml — replaced by the platform's")
    shutil.copy2(platform_buildspec_path(), src_root / "buildspec.yml")
    archive = shutil.make_archive(str(build_dir / "context_src"), "zip", src_root)
    tag, mins = build_and_push_image(ctx, agent, archive)
    digest = ctx.scratch["image_digest"]
    return StageResult(detail=f"codebuild · arm64 · {mins:.1f}m → :{tag} @ {digest[:19]}…")


def _stage_package(ctx: StageContext, agent: Agent) -> StageResult:
    spec = AgentSpec(**agent.spec)
    cfg = _config(spec)
    if cfg.artifact_kind == "container_image":
        # nothing to build — deploy pins the URI the generate stage verified
        ctx.scratch["image_uri"] = cfg.image_uri
        return StageResult(skipped=True, detail="existing image — no build")
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    if cfg.artifact_kind == "code_zip":
        return _package_code_zip(ctx, agent, cfg, bucket)
    return _package_container_source(ctx, agent, cfg)


def _stage_provision(ctx: StageContext, agent: Agent, iam_client: Any = None) -> StageResult:
    spec = AgentSpec(**agent.spec)
    role_arn, detail = agent_iam.provision_execution_role(
        agent, spec, get_settings(), ctx.workspace, ctx.log, iam=iam_client
    )
    ctx.scratch["execution_role_arn"] = role_arn
    return StageResult(detail=detail)


def _container_uri(ctx: StageContext, agent: Agent, cfg: ByocConfig) -> str:
    if cfg.artifact_kind == "container_image":
        return cfg.image_uri or ""
    registry, repo, tag = _image_ref(ctx.workspace, agent)
    return (
        ctx.scratch.get("image_uri")
        or _recorded_digest_uri(ctx, registry, repo)
        # only reached when the digest record is gone; the tag is mutable, so
        # this is a fallback, not a path to rely on
        or f"{registry}/{repo}:{tag}"
    )


def _stage_deploy(ctx: StageContext, agent: Agent) -> StageResult:
    # A resumed job skips its successful provision stage, but scratch is
    # process-local. Reconcile the role idempotently instead of silently
    # switching the workload to the workspace's broader shared role.
    if not ctx.scratch.get("execution_role_arn"):
        _stage_provision(ctx, agent)
    client = control_client(ctx.workspace)
    mode = ctx.scratch.get("mode", "create")
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        spec = AgentSpec(**row.spec)
        cfg = _config(spec)
        role_arn = ctx.scratch["execution_role_arn"]
        environment = runtime_environment(spec, ctx.workspace.resources)
        # The per-agent execution role scopes bedrock:InvokeModel to exactly
        # spec.allowed_model_ids (agent_iam.allowed_model_resources) — hand the
        # permitted ids to the user code: MODEL_ID is the primary (= model_id),
        # ALLOWED_MODEL_IDS the full comma-separated list. Explicit spec.env
        # values win for both.
        environment.setdefault("MODEL_ID", spec.model_id)
        environment.setdefault("ALLOWED_MODEL_IDS", ",".join(spec.allowed_model_ids))

        def _kwargs() -> dict:
            if cfg.artifact_kind == "code_zip":
                return {
                    "s3_bucket": ctx.scratch.get("s3_bucket")
                    or ctx.workspace.resources.get("artifacts_bucket", ""),
                    "s3_key": ctx.scratch.get("s3_key")
                    or PACKAGE_KEY_TMPL.format(name=row.name),
                    "role_arn": role_arn,
                    "environment": environment,
                    "python_version": cfg.python_version,
                    "entrypoint": cfg.entrypoint,
                    # user zips don't necessarily vendor the ADOT distro; an
                    # absent opentelemetry-instrument launcher fails at start
                    "instrument": False,
                }
            return {
                "container_uri": _container_uri(ctx, row, cfg),
                "role_arn": role_arn,
                "environment": environment,
            }

        create_fn = (
            rt.create_code_runtime if cfg.artifact_kind == "code_zip"
            else rt.create_container_runtime
        )
        update_fn = (
            rt.update_code_runtime if cfg.artifact_kind == "code_zip"
            else rt.update_container_runtime
        )

        if mode == "update" and row.resource_id:  # re-publish → new version, same ARN
            runtime_id = row.resource_id
            updated = agent_iam.retry_iam_propagation(
                lambda: update_fn(client, runtime_id=runtime_id, **_kwargs()),
                ctx.log,
            )
            row.version = str(updated.get("agentRuntimeVersion", row.version or "1"))
            db.commit()
            ctx.log(
                f"UpdateAgentRuntime accepted · runtimeId {runtime_id} · "
                f"new version {row.version}"
            )
        elif row.resource_id:
            runtime_id = row.resource_id
            ctx.log(f"resuming — runtime {runtime_id} already created, polling status")
        else:
            created = agent_iam.retry_iam_propagation(
                lambda: create_fn(
                    client, runtime_name=sanitize_runtime_name(row.name), **_kwargs()
                ),
                ctx.log,
            )
            runtime_id = created["agentRuntimeId"]
            row.resource_id = runtime_id
            row.arn = created["agentRuntimeArn"]
            row.version = str(created.get("agentRuntimeVersion", "1"))
            db.commit()
            ctx.log(f"CreateAgentRuntime accepted · runtimeId {runtime_id}")

        ready = rt.wait_runtime_ready(
            client, runtime_id, on_status=lambda s: ctx.log(f"runtime status: {s}")
        )
        row.arn = ready["agentRuntimeArn"]
        row.version = str(ready.get("agentRuntimeVersion", row.version or "1"))
        db.commit()
        return StageResult(detail=f"READY · {ready['agentRuntimeArn']}")
    finally:
        db.close()


def _stage_register(ctx: StageContext, agent: Agent) -> StageResult:
    from app.deployer.registration import register_stage

    return register_stage(ctx, agent)


STAGES = {
    "generate": _stage_generate,
    "package": _stage_package,
    "provision": _stage_provision,
    "deploy": _stage_deploy,
    "register": _stage_register,
}

register_method("byoc", STAGES)


def delete_agent_resources(
    agent: Agent, workspace: WorkspaceContext, ecr_client: Any = None
) -> None:
    """Runtime + staged upload objects + (container_source) the built image tags.

    Everything after the runtime delete is best-effort: an orphan S3 object or
    ECR tag must never block deleting the agent."""
    if agent.resource_id:
        client = control_client(workspace)
        try:
            rt.delete_runtime(client, agent.resource_id)
        except client.exceptions.ResourceNotFoundException:
            pass
    cfg = (agent.spec or {}).get("byoc") or {}
    upload_id = cfg.get("upload_id")
    if upload_id:
        byoc_uploads.delete_upload_objects(workspace, agent.workspace_id, upload_id)
    if cfg.get("artifact_kind") == "container_source":
        # the builds this agent pushed are {name}-v{version} tags on the shared repo
        repo = (workspace.resources or {}).get("ecr_repo", "launchpad-agents")
        try:
            ecr = ecr_client or workspace.client("ecr")
            versions = range(1, int(agent.version or "1") + 1)
            ecr.batch_delete_image(
                repositoryName=repo,
                imageIds=[{"imageTag": f"{agent.name}-v{v}"} for v in versions],
            )
        except Exception:  # noqa: BLE001 — cleanup must never block a delete
            pass
