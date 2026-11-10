"""Skill Lab exec-worker image: build-context assembly + CodeBuild build.

The worker image is the vendored SkillOpt agentcore_worker with the OpenCode CLI
pinned inside the Dockerfile (D16), built for linux/arm64 by the shared
launchpad-agent-builder CodeBuild project, pushed as a content-addressed tag into
the shared launchpad-agents ECR repository (a separate repo would need a CodeBuild
role policy widening; a tag namespace does not). The buildspec is rendered here from
Settings so every China mirror (base image, apt, the CodeBuild pip index + its
fallback, npm registry) reaches the Dockerfile as a build arg — and is part of the
content hash, so a mirror change rolls the image. The pip index is the BUILD mirror
(`codebuild_pip_index_url`), not the host mirror: tuna 4xx-blocks CodeBuild's egress
(N11-r3-bug7).
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import REPO_ROOT, get_settings
from app.services import ecr
from app.services.agentcore import codebuild as cb
from app.services.workspace import WorkspaceContext

VENDOR_ROOT = REPO_ROOT / "vendor" / "skillopt"
WORKER_DOCKERFILE = VENDOR_ROOT / "deploy" / "agentcore" / "Dockerfile"
WORKER_PACKAGE_DIR = VENDOR_ROOT / "skillopt"
SOURCE_ZIP_KEY = "builds/skill-lab-worker/source.zip"
IMAGE_TAG_PREFIX = "skill-lab-worker-"
# Keep in step with scripts/mirror_base_images.sh (the one image the mirror holds).
WORKER_BASE_IMAGE = "python:3.13-slim"
# Public mirror of that image for a build without the ECR mirror (spike §6 verified
# the arm64 manifest from cn-northwest-1-reachable hosts) — the Dockerfile default.
WORKER_BASE_IMAGE_PUBLIC_MIRROR = "docker.m.daocloud.io/library/" + WORKER_BASE_IMAGE

# Same contract as backend/app/deployer/buildspec.yml (ECR login → docker build
# arm64 → push), with the worker's extra build args rendered from Settings.
_BUILDSPEC_TEMPLATE = """version: 0.2

# Rendered by backend/app/skill_lab/worker_build.py — the mirrors come from Settings
# (codebuild_pip_index_url + codebuild_pip_fallback_index_url, npm_registry, apt_mirror,
# base_image_mirror_*) and are part of the image's content hash. Env via start_build
# environmentVariablesOverride: AWS_REGION, ECR_REGISTRY, ECR_REPO, IMAGE_TAG.
phases:
  pre_build:
    commands:
      - echo "logging in to $ECR_REGISTRY"
      - >-
        aws ecr get-login-password --region $AWS_REGION
        | docker login --username AWS --password-stdin $ECR_REGISTRY
      - |
        # N11-r3-bug7: both live worker builds died inside `docker build` with pip's
        # opaque "No matching distribution found" 0.2 s after "Looking in indexes" — an
        # immediate 4xx from the mirror to the CodeBuild egress. Probe the primary and
        # the fallback index first; the HTTP codes land in the log, and the build fails
        # here, before `docker build`, only when NEITHER answers (the Dockerfile falls
        # back to the second index on its own).
        reachable=0
        for idx in "{pip_index_url}" "{pip_fallback_index_url}"; do
          url="${{idx%/}}/uv/"
          code="$(curl -sS -o /dev/null -w '%{{http_code}}' --max-time 30 "$url")" || true
          echo "pypi pre-flight: $url -> HTTP $code"
          case "$code" in 2*|3*) reachable=1 ;; esac
        done
        if [ "$reachable" != 1 ]; then
          echo "pypi pre-flight FAILED: neither index answers the CodeBuild egress (codes above)"
          exit 1
        fi
  build:
    commands:
      - echo "building $ECR_REPO:$IMAGE_TAG (linux/arm64, skill-lab worker)"
      - |
        docker build --platform linux/arm64 \\
          --build-arg BASE_IMAGE="{base_image}" \\
          --build-arg APT_MIRROR="{apt_mirror}" \\
          --build-arg PIP_INDEX_URL="{pip_index_url}" \\
          --build-arg PIP_FALLBACK_INDEX_URL="{pip_fallback_index_url}" \\
          --build-arg PIP_EXTRA_INDEX_URL="{pip_extra_index_url}" \\
          --build-arg NPM_CONFIG_REGISTRY="{npm_registry}" \\
          --build-arg OPENCODE_VERSION="{opencode_version}" \\
          -t $ECR_REGISTRY/$ECR_REPO:$IMAGE_TAG .
  post_build:
    commands:
      - docker push $ECR_REGISTRY/$ECR_REPO:$IMAGE_TAG
      - echo "pushed $ECR_REGISTRY/$ECR_REPO:$IMAGE_TAG"
"""


def worker_base_image(workspace: WorkspaceContext) -> str:
    """The Dockerfile's base: the ECR mirror copy when the CN mirror is on
    (scripts/mirror_base_images.sh populated it), else the public China mirror."""
    settings = get_settings()
    if settings.base_image_mirror_enabled:
        return (
            f"{workspace.account_id}.dkr.ecr.{workspace.region}.{workspace.dns_suffix}/"
            f"{settings.base_image_mirror_prefix}/{WORKER_BASE_IMAGE}"
        )
    return WORKER_BASE_IMAGE_PUBLIC_MIRROR


def render_buildspec(workspace: WorkspaceContext) -> str:
    settings = get_settings()
    return _BUILDSPEC_TEMPLATE.format(
        base_image=worker_base_image(workspace),
        apt_mirror=settings.apt_mirror,
        # N11-r3-bug7: the BUILD mirrors, never the host mirror (tuna 4xx-blocks CodeBuild)
        pip_index_url=settings.codebuild_pip_index_url,
        pip_fallback_index_url=settings.codebuild_pip_fallback_index_url,
        pip_extra_index_url=settings.pip_extra_index_url,
        npm_registry=settings.npm_registry,
        opencode_version=settings.skill_lab_worker_opencode_version,
    )

_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def _iter_context_files() -> list[tuple[str, Path]]:
    """(archive-relative name, source path) pairs, sorted for a stable hash."""
    if not WORKER_PACKAGE_DIR.is_dir():
        raise FileNotFoundError(
            f"vendored skillopt package missing: {WORKER_PACKAGE_DIR} — "
            "the vendor/skillopt tree must be present to build the worker image"
        )
    pairs: list[tuple[str, Path]] = [("Dockerfile", WORKER_DOCKERFILE)]
    for path in sorted(WORKER_PACKAGE_DIR.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(WORKER_PACKAGE_DIR).as_posix()
        pairs.append((f"skillopt/{rel}", path))
    return pairs


def _extra_context_blobs(workspace: WorkspaceContext) -> list[tuple[str, bytes]]:
    """In-memory build-context files (sorted, hashed alongside the file pairs):
    the buildspec rendered from Settings for this workspace."""
    return [("buildspec.yml", render_buildspec(workspace).encode("utf-8"))]


def context_content_hash(workspace: WorkspaceContext) -> str:
    """sha256 over every file that reaches the build context (incl. the rendered
    buildspec, so a mirror/pin change rolls the image). The image tag is derived
    from this, so 'rebuild when sources change' is a tag lookup."""
    digest = hashlib.sha256()
    for name, payload in [
        *((name, path.read_bytes()) for name, path in _iter_context_files()),
        *_extra_context_blobs(workspace),
    ]:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def image_tag(content_hash: str) -> str:
    return IMAGE_TAG_PREFIX + content_hash[:12]


def assemble_worker_context(target_dir: Path, workspace: WorkspaceContext) -> Path:
    """Stage Dockerfile + the rendered buildspec + the vendored skillopt package."""
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)
    shutil.copy2(WORKER_DOCKERFILE, target_dir / "Dockerfile")
    shutil.copytree(WORKER_PACKAGE_DIR, target_dir / "skillopt", ignore=_COPY_IGNORE)
    for name, payload in _extra_context_blobs(workspace):
        dest = target_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
    return target_dir


def _tag_exists(ecr_client: Any, repo: str, tag: str) -> bool:
    try:
        images = ecr_client.describe_images(
            repositoryName=repo, imageIds=[{"imageTag": tag}]
        ).get("imageDetails", [])
        return bool(images)
    except Exception as exc:  # noqa: BLE001 — typed error name is client-specific
        if exc.__class__.__name__ == "ImageNotFoundException":
            return False
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code == "ImageNotFoundException":
            return False
        raise


def ensure_worker_image(
    workspace: WorkspaceContext, log: Callable[[str], None] = lambda _m: None
) -> dict[str, str]:
    """Build (if needed) and pin the worker image; returns tag/digest/uri.

    Content-addressed: an existing tag for the current context hash means the
    image is already the wanted one and the build is skipped entirely.
    """
    bucket = str(workspace.resources.get("artifacts_bucket") or "")
    project = str(workspace.resources.get("codebuild_project") or "")
    repo = str(workspace.resources.get("ecr_repo") or "")
    repo_uri = str(workspace.resources.get("ecr_repo_uri") or "")
    if not (bucket and project and repo and repo_uri):
        raise RuntimeError(
            "artifacts_bucket/codebuild_project/ecr_repo(_uri) missing from the "
            "resource map — run the base bootstrap first"
        )
    registry = repo_uri.split("/", 1)[0]
    tag = image_tag(context_content_hash(workspace))
    ecr_client = workspace.client("ecr")

    if _tag_exists(ecr_client, repo, tag):
        digest = ecr.resolve_digest(ecr_client, repo, tag)
        log(f"worker image up to date · :{tag} @ {digest[:19]}…")
        return {"tag": tag, "digest": digest, "uri": f"{repo_uri}@{digest}"}

    with tempfile.TemporaryDirectory(prefix="skill_lab_worker_ctx_") as tmp:
        context_dir = assemble_worker_context(Path(tmp) / "ctx", workspace)
        archive = shutil.make_archive(str(context_dir) + "_src", "zip", context_dir)
        workspace.client("s3").upload_file(archive, bucket, SOURCE_ZIP_KEY)
    log(f"worker build context → s3://{bucket}/{SOURCE_ZIP_KEY}")

    codebuild = workspace.client("codebuild")
    t0 = time.monotonic()
    build_id = cb.start_image_build(
        codebuild,
        project=project,
        s3_bucket=bucket,
        s3_key=SOURCE_ZIP_KEY,
        region=workspace.region,
        ecr_registry=registry,
        ecr_repo=repo,
        image_tag=tag,
    )
    log(f"codebuild started · {build_id}")
    cb.wait_build(codebuild, build_id, on_phase=lambda p: log(f"codebuild phase: {p}"))
    digest = ecr.resolve_digest(ecr_client, repo, tag)
    mins = (time.monotonic() - t0) / 60
    log(f"worker image built · arm64 · {mins:.1f}m → :{tag} @ {digest[:19]}…")
    return {"tag": tag, "digest": digest, "uri": f"{repo_uri}@{digest}"}
