"""Claude Agent SDK container path (方式A).

    generate  → assemble the ARM64 build context (Dockerfile + rendered main.py
                + .claude scaffold) from the AgentSpec
    package   → zip context → S3 → CodeBuild (docker build+push, phases streamed
                into the job log) → ECR image
    provision → reuse the shared execution role
    deploy    → CreateAgentRuntime(containerConfiguration) + poll READY
    register  → create/refresh the A2A registry record (auto-submit)
"""

import shutil
import time
from pathlib import Path
from typing import Any

import boto3

from app.core.config import get_settings
from app.deployer.environment import runtime_environment
from app.deployer.pipeline import StageContext, StageResult, register_method
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import agent_iam, ecr
from app.services.agentcore import codebuild as cb
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.templates.claude_sdk_agent import assemble_build_context

from .zip_runtime import bundle_skill_paths_into, sanitize_runtime_name


def _image_ref(settings, agent: Agent) -> tuple[str, str, str]:
    registry = f"{settings.account_id}.dkr.ecr.{settings.region}.amazonaws.com"
    repo = settings.resources.get("ecr_repo", "launchpad-agents")
    tag = f"{agent.name}-v{agent.version or '1'}"
    return registry, repo, tag


def _build_context(spec: AgentSpec, agent: Agent, log) -> Path:
    """Template files + rendered main.py, then spec.skills S3 prefixes into
    .claude/skills/{name}/ so the claude CLI discovers them next to agents/."""
    context_dir = assemble_build_context(spec, Path(f"/tmp/launchpad_ctx_{agent.name}"))
    if spec.skills:
        bundle_skill_paths_into(spec.skills, context_dir / ".claude", log)
    return context_dir


def _stage_generate(ctx: StageContext, agent: Agent) -> StageResult:
    spec = AgentSpec(**agent.spec)
    context_dir = _build_context(spec, agent, ctx.log)
    files = sorted(str(p.relative_to(context_dir)) for p in context_dir.rglob("*") if p.is_file())
    ctx.scratch["context_dir"] = str(context_dir)
    ctx.log(f"build context assembled: {', '.join(files)}")
    return StageResult(detail=f"container context · {len(files)} files")


def _stage_package(ctx: StageContext, agent: Agent) -> StageResult:
    settings = get_settings()
    bucket = settings.resources.get("artifacts_bucket")
    project = settings.resources.get("codebuild_project")
    if not bucket or not project:
        raise RuntimeError("artifacts_bucket/codebuild_project missing — run scripts/bootstrap.py")

    spec = AgentSpec(**agent.spec)
    context_dir = Path(
        ctx.scratch.get("context_dir")
        or assemble_build_context(spec, Path(f"/tmp/launchpad_ctx_{agent.name}"))
    )
    archive = shutil.make_archive(str(context_dir) + "_src", "zip", context_dir)
    s3_key = f"builds/{agent.name}/source.zip"
    boto3.client("s3", region_name=settings.region).upload_file(archive, bucket, s3_key)
    ctx.log(f"source zip uploaded → s3://{bucket}/{s3_key}")

    registry, repo, tag = _image_ref(settings, agent)
    codebuild = boto3.client("codebuild", region_name=settings.region)
    t0 = time.monotonic()
    build_id = cb.start_image_build(
        codebuild,
        project=project,
        s3_bucket=bucket,
        s3_key=s3_key,
        region=settings.region,
        ecr_registry=registry,
        ecr_repo=repo,
        image_tag=tag,
    )
    ctx.log(f"codebuild started · {build_id}")
    cb.wait_build(codebuild, build_id, on_phase=lambda p: ctx.log(f"codebuild phase: {p}"))
    mins = (time.monotonic() - t0) / 60

    # Pin the deployment to the image that was just pushed, not to the tag: the tag
    # is mutable, so deploying by it means what the runtime executes can change
    # with no record of it.
    ecr_client = boto3.client("ecr", region_name=settings.region)
    digest = ecr.resolve_digest(ecr_client, repo, tag)
    _record_image_digest(ctx, digest)
    ctx.scratch["image_digest"] = digest
    ctx.scratch["image_uri"] = f"{registry}/{repo}@{digest}"
    ctx.log(f"image pushed · {registry}/{repo}:{tag} · {digest}")

    _run_scan_gate(ctx, ecr_client, repo, digest, settings)
    return StageResult(detail=f"codebuild · arm64 · {mins:.1f}m → :{tag} @ {digest[:19]}…")


def _record_image_digest(ctx: StageContext, digest: str) -> None:
    """Persist the digest on the Deployment row so the console can report exactly
    what is deployed and a resumed job re-uses the same image."""
    from app.models.ledger import Deployment

    db = ctx.session()
    try:
        row = db.get(Deployment, ctx.deployment_id) if ctx.deployment_id else None
        if row is not None:
            row.image_digest = digest
            db.commit()
    finally:
        db.close()


def _run_scan_gate(
    ctx: StageContext, ecr_client: Any, repo: str, digest: str, settings: Any
) -> None:
    """Refuse to deploy an image whose push scan reports blocking findings.

    Runs here rather than in the deploy stage so a blocked image never reaches
    CreateAgentRuntime. Naturally idempotent on resume: reading a scan result does
    not change anything.

    A scan that could not be read (not enabled, API error) or did not finish is
    reported as exactly that — never folded into "clean", which would let an
    absent gate read as a passed one.
    """
    if not settings.image_scan_enabled:
        ctx.log("image scan gate disabled (image_scan_enabled=false) — image NOT scanned")
        return
    severities = settings.image_scan_block_severities
    try:
        counts = ecr.wait_for_scan(
            ecr_client,
            repo,
            digest,
            timeout_s=settings.image_scan_timeout_s,
            on_status=lambda s: ctx.log(f"image scan: {s}"),
        )
    except (ecr.ScanTimeout, ecr.ScanUnavailable) as exc:
        # Deliberately not fatal: an unreadable scan is an infrastructure gap, not
        # evidence of a vulnerable image, and failing every deploy on it would make
        # the platform unusable the moment scanning misbehaves. Loud instead.
        ctx.log(f"image scan gate did NOT complete — {exc}. Deploying unscanned.")
        return

    ctx.log(f"image scan findings · {ecr.format_counts(counts)}")
    blocking = ecr.blocking_findings(counts, severities)
    if blocking:
        raise RuntimeError(
            f"image {repo}@{digest} has blocking vulnerabilities "
            f"({ecr.format_counts(blocking)}) at or above {', '.join(severities)}. "
            "Rebuild on a patched base image, or set image_scan_block_severities / "
            "image_scan_enabled=false to deploy anyway."
        )


def _filesystem_configurations(spec: AgentSpec) -> list[dict]:
    """spec.filesystem → the filesystemConfigurations union list (AWS shapes)."""
    fs = spec.filesystem
    out: list[dict] = []
    if fs.session_storage:
        out.append({"sessionStorage": {"mountPath": fs.session_storage.mount_path}})
    for mount in fs.s3_files:
        out.append({"s3FilesAccessPoint": {
            "accessPointArn": mount.access_point_arn, "mountPath": mount.mount_path,
        }})
    for mount in fs.efs:
        out.append({"efsAccessPoint": {
            "accessPointArn": mount.access_point_arn, "mountPath": mount.mount_path,
        }})
    return out


def _vpc(spec: AgentSpec) -> dict | None:
    """networkModeConfig input — only when BYO mounts force VPC mode."""
    if not (spec.filesystem.byo and spec.network):
        return None
    return {"subnets": spec.network.subnets, "security_groups": spec.network.security_groups}


def _stage_provision(ctx: StageContext, agent: Agent, iam_client: Any = None) -> StageResult:
    spec = AgentSpec(**agent.spec)
    role_arn, detail = agent_iam.provision_execution_role(
        agent, spec, get_settings(), ctx.log, iam=iam_client
    )
    ctx.scratch["execution_role_arn"] = role_arn
    return StageResult(detail=detail)


def _recorded_digest_uri(ctx: StageContext, registry: str, repo: str) -> str | None:
    """`registry/repo@sha256:…` from the Deployment row, for a resumed job whose
    package stage already ran and left nothing in scratch."""
    from app.models.ledger import Deployment

    db = ctx.session()
    try:
        row = db.get(Deployment, ctx.deployment_id) if ctx.deployment_id else None
        digest = row.image_digest if row is not None else None
    finally:
        db.close()
    return f"{registry}/{repo}@{digest}" if digest else None


def _stage_deploy(ctx: StageContext, agent: Agent) -> StageResult:
    settings = get_settings()
    client = control_client()
    mode = ctx.scratch.get("mode", "create")
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)

        def _kwargs() -> dict:
            registry, repo, tag = _image_ref(settings, row)
            spec = AgentSpec(**row.spec)
            return {
                "container_uri": ctx.scratch.get("image_uri")
                or _recorded_digest_uri(ctx, registry, repo)
                # Only reached for a deployment created before digests were
                # recorded; the tag is mutable, so this is a fallback, not a path
                # to rely on.
                or f"{registry}/{repo}:{tag}",
                "role_arn": ctx.scratch.get("execution_role_arn")
                or settings.resources.get("execution_role_arn", ""),
                "environment": runtime_environment(spec, settings.resources),
                "filesystem_configurations": _filesystem_configurations(spec) or None,
                "vpc": _vpc(spec),
            }

        if mode == "update" and row.resource_id:  # re-publish → UpdateAgentRuntime (new version)
            runtime_id = row.resource_id
            updated = agent_iam.retry_iam_propagation(
                lambda: rt.update_container_runtime(client, runtime_id=runtime_id, **_kwargs()),
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
                lambda: rt.create_container_runtime(
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

register_method("container", STAGES)


def delete_agent_resources(agent: Agent, iam_client: Any = None) -> None:
    if not agent.resource_id:
        return
    client = control_client()
    try:
        rt.delete_runtime(client, agent.resource_id)
    except client.exceptions.ResourceNotFoundException:
        pass
    # With per-agent roles the whole role goes away (routers/agents.py), which takes
    # its inline policies with it. This cleanup is only still needed for the
    # shared-role fallback, where a stale launchpad-fs-<agent> policy would otherwise
    # accumulate on a principal every agent assumes — the exact problem T3 is about.
    settings = get_settings()
    if settings.per_agent_execution_roles:
        return
    role_arn = agent_iam.shared_role_arn(settings)
    if role_arn:
        try:
            iam = iam_client or boto3.client("iam", region_name=settings.region)
            iam.delete_role_policy(
                RoleName=role_arn.rsplit("/", 1)[-1],
                PolicyName=agent_iam.fs_policy_name(agent.name),
            )
        except Exception:  # noqa: BLE001 — absent on most agents
            pass
