"""ECR/CodeBuild image helpers shared by the BYOC container paths (formerly
deployer/container.py)."""

import time
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.naming import default_ecr_repo
from app.deployer.pipeline import StageContext
from app.models.ledger import Agent
from app.services import ecr
from app.services.agentcore import codebuild as cb
from app.services.workspace import WorkspaceContext

BUILDSPEC_PATH = Path(__file__).parent / "buildspec.yml"


def platform_buildspec_path() -> Path:
    """The platform-owned CodeBuild recipe (docker build + push, ARM64).

    The launchpad-agent-builder project has no inline buildspec — it reads
    buildspec.yml from the source zip, so every build context this platform
    submits must ship this exact file. Callers that accept user-supplied build
    contexts (BYOC container_source) must copy it in OVERWRITING any
    buildspec.yml in the upload: the member controls the Dockerfile only,
    never the build recipe."""
    return BUILDSPEC_PATH


def _image_ref(workspace: WorkspaceContext, agent: Agent) -> tuple[str, str, str]:
    registry = f"{workspace.account_id}.dkr.ecr.{workspace.region}.{workspace.dns_suffix}"
    repo = workspace.resources.get("ecr_repo", default_ecr_repo())
    tag = f"{agent.name}-v{agent.version or '1'}"
    return registry, repo, tag


def build_and_push_image(ctx: StageContext, agent: Agent, archive: str,
                         extra_env: dict[str, str] | None = None) -> tuple[str, float]:
    """Upload one build-context archive → CodeBuild (ARM64) → ECR; pin + gate.

    The build/wait/digest/scan sequence shared by the container method and BYOC
    ``container_source``: uploads ``archive`` to ``builds/{agent.name}/source.zip``,
    runs the workspace's ``launchpad-agent-builder`` project, resolves the pushed
    tag to its immutable digest (recorded on the Deployment row so a resumed job
    re-uses the same image) and runs the scan gate. Returns
    ``(tag, minutes)`` and leaves ``image_digest``/``image_uri`` in scratch.
    """
    settings = get_settings()
    workspace = ctx.workspace
    bucket = workspace.resources.get("artifacts_bucket")
    project = workspace.resources.get("codebuild_project")
    if not bucket or not project:
        raise RuntimeError(
            "artifacts_bucket/codebuild_project missing from this workspace's "
            "resource map — run its bootstrap"
        )

    s3_key = f"builds/{agent.name}/source.zip"
    workspace.client("s3").upload_file(archive, bucket, s3_key)
    ctx.log(f"source zip uploaded → s3://{bucket}/{s3_key}")

    registry, repo, tag = _image_ref(workspace, agent)
    codebuild = workspace.client("codebuild")
    t0 = time.monotonic()
    build_id = cb.start_image_build(
        codebuild,
        project=project,
        s3_bucket=bucket,
        s3_key=s3_key,
        region=workspace.region,
        ecr_registry=registry,
        ecr_repo=repo,
        image_tag=tag,
        **({"extra_env": extra_env} if extra_env else {}),
    )
    ctx.log(f"codebuild started · {build_id}")
    cb.wait_build(codebuild, build_id, on_phase=lambda p: ctx.log(f"codebuild phase: {p}"))
    mins = (time.monotonic() - t0) / 60

    # Pin the deployment to the image that was just pushed, not to the tag: the tag
    # is mutable, so deploying by it means what the runtime executes can change
    # with no record of it.
    ecr_client = workspace.client("ecr")
    digest = ecr.resolve_digest(ecr_client, repo, tag)
    _record_image_digest(ctx, digest)
    ctx.scratch["image_digest"] = digest
    ctx.scratch["image_uri"] = f"{registry}/{repo}@{digest}"
    ctx.log(f"image pushed · {registry}/{repo}:{tag} · {digest}")

    _run_scan_gate(ctx, ecr_client, repo, digest, settings)
    return tag, mins


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
        packages = ecr.blocking_packages(ecr_client, repo, digest, severities)
        listed = f" — {'; '.join(packages)}" if packages else ""
        raise RuntimeError(
            f"image {repo}@{digest} has blocking vulnerabilities "
            f"({ecr.format_counts(blocking)}) at or above {', '.join(severities)}"
            f"{listed}. Rebuild on a patched base image, or set "
            "image_scan_block_severities / image_scan_enabled=false to deploy anyway."
        )


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
