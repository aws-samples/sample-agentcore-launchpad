"""Skill Lab exec-worker image: build-context assembly + CodeBuild build.

The worker image is the vendored SkillOpt agentcore_worker (claude CLI pinned
inside the Dockerfile) built for linux/arm64 by the shared launchpad-agent-builder
CodeBuild project, pushed as a content-addressed tag into the shared
launchpad-agents ECR repository (a separate repo would need a CodeBuild role
policy widening; a tag namespace does not).
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import REPO_ROOT
from app.services import ecr
from app.services.agentcore import codebuild as cb
from app.services.workspace import WorkspaceContext

VENDOR_ROOT = REPO_ROOT / "vendor" / "skillopt"
WORKER_DOCKERFILE = VENDOR_ROOT / "deploy" / "agentcore" / "Dockerfile"
WORKER_PACKAGE_DIR = VENDOR_ROOT / "skillopt"
# The buildspec contract (ECR login → docker build arm64 → push) is shared with
# the 方式A container path; reuse its file verbatim.
BUILDSPEC_TEMPLATE = (
    REPO_ROOT / "backend" / "app" / "templates" / "claude_sdk_agent" / "buildspec.yml"
)
SOURCE_ZIP_KEY = "builds/skill-lab-worker/source.zip"
IMAGE_TAG_PREFIX = "skill-lab-worker-"

_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def _iter_context_files() -> list[tuple[str, Path]]:
    """(archive-relative name, source path) pairs, sorted for a stable hash."""
    if not WORKER_PACKAGE_DIR.is_dir():
        raise FileNotFoundError(
            f"vendored skillopt package missing: {WORKER_PACKAGE_DIR} — "
            "the vendor/skillopt tree must be present to build the worker image"
        )
    pairs: list[tuple[str, Path]] = [
        ("Dockerfile", WORKER_DOCKERFILE),
        ("buildspec.yml", BUILDSPEC_TEMPLATE),
    ]
    for path in sorted(WORKER_PACKAGE_DIR.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(WORKER_PACKAGE_DIR).as_posix()
        pairs.append((f"skillopt/{rel}", path))
    return pairs


def context_content_hash() -> str:
    """sha256 over every file that reaches the build context. The image tag is
    derived from this, so 'rebuild when sources change' is a tag lookup."""
    digest = hashlib.sha256()
    for name, path in _iter_context_files():
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def image_tag(content_hash: str | None = None) -> str:
    return IMAGE_TAG_PREFIX + (content_hash or context_content_hash())[:12]


def assemble_worker_context(target_dir: Path) -> Path:
    """Stage Dockerfile + buildspec + the vendored skillopt package."""
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)
    shutil.copy2(WORKER_DOCKERFILE, target_dir / "Dockerfile")
    shutil.copy2(BUILDSPEC_TEMPLATE, target_dir / "buildspec.yml")
    shutil.copytree(WORKER_PACKAGE_DIR, target_dir / "skillopt", ignore=_COPY_IGNORE)
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
    tag = image_tag()
    ecr_client = workspace.client("ecr")

    if _tag_exists(ecr_client, repo, tag):
        digest = ecr.resolve_digest(ecr_client, repo, tag)
        log(f"worker image up to date · :{tag} @ {digest[:19]}…")
        return {"tag": tag, "digest": digest, "uri": f"{repo_uri}@{digest}"}

    with tempfile.TemporaryDirectory(prefix="skill_lab_worker_ctx_") as tmp:
        context_dir = assemble_worker_context(Path(tmp) / "ctx")
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
