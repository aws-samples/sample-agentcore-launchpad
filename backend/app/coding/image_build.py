"""Coding runtime image: build-context assembly + CodeBuild build.

Mirrors ``skill_lab.worker_build``: the context (``image/`` minus its tests, plus
the shared buildspec) is hashed into a content-addressed tag
``coding-runtime-<hash12>``; an existing tag in the shared ``launchpad-agents``
ECR repository means the image is already built, so a repair with unchanged
sources skips the build entirely. The build runs on the shared
``launchpad-agent-builder`` CodeBuild project (linux/arm64), whose role is
already scoped to that repository — a separate repo would need a role widening,
a tag namespace does not.
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

IMAGE_DIR = Path(__file__).resolve().parent / "image"
# The buildspec contract (ECR login → docker build arm64 → push) is shared with
# the 方式A container path and the Skill Lab worker; reuse its file verbatim.
BUILDSPEC_TEMPLATE = (
    REPO_ROOT / "backend" / "app" / "templates" / "claude_sdk_agent" / "buildspec.yml"
)
SOURCE_ZIP_KEY = "builds/coding-runtime/source.zip"
IMAGE_TAG_PREFIX = "coding-runtime-"
# What the Dockerfile COPYs, and nothing else: tests, editor files and caches
# must not change the tag (or a test edit would roll every project's microVM).
CONTEXT_FILES = ("Dockerfile", "codex-config.toml")
CONTEXT_DIRS = ("src", "templates")
_SKIP_NAMES = {".DS_Store", "__pycache__", "node_modules"}


def _iter_context_files() -> list[tuple[str, Path]]:
    """(archive-relative name, source path) pairs, sorted for a stable hash."""
    if not IMAGE_DIR.is_dir():
        raise FileNotFoundError(f"coding runtime image source missing: {IMAGE_DIR}")
    pairs: list[tuple[str, Path]] = [("buildspec.yml", BUILDSPEC_TEMPLATE)]
    for name in CONTEXT_FILES:
        pairs.append((name, IMAGE_DIR / name))
    for dirname in CONTEXT_DIRS:
        for path in sorted((IMAGE_DIR / dirname).rglob("*")):
            if not path.is_file() or _SKIP_NAMES & set(path.parts):
                continue
            pairs.append((path.relative_to(IMAGE_DIR).as_posix(), path))
    missing = [str(path) for _, path in pairs if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"coding runtime build context incomplete: {missing}")
    return sorted(pairs)


def context_content_hash() -> str:
    """sha256 over every file that reaches the build context (names + bytes)."""
    digest = hashlib.sha256()
    for name, path in _iter_context_files():
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def image_tag(content_hash: str | None = None) -> str:
    return IMAGE_TAG_PREFIX + (content_hash or context_content_hash())[:12]


def assemble_context(target_dir: Path) -> Path:
    """Stage exactly the hashed files into ``target_dir`` (recreated)."""
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)
    for name, path in _iter_context_files():
        dest = target_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
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


def ensure_image(
    workspace: WorkspaceContext, log: Callable[[str], None] = lambda _m: None
) -> dict[str, str]:
    """Build (if needed) and pin the coding runtime image; returns tag/digest/uri."""
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
        log(f"coding runtime image up to date · :{tag} @ {digest[:19]}…")
        return {"tag": tag, "digest": digest, "uri": f"{repo_uri}@{digest}"}

    with tempfile.TemporaryDirectory(prefix="coding_runtime_ctx_") as tmp:
        context_dir = assemble_context(Path(tmp) / "ctx")
        archive = shutil.make_archive(str(context_dir) + "_src", "zip", context_dir)
        workspace.client("s3").upload_file(archive, bucket, SOURCE_ZIP_KEY)
    log(f"build context → s3://{bucket}/{SOURCE_ZIP_KEY}")

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
    log(f"coding runtime image built · arm64 · {mins:.1f}m → :{tag} @ {digest[:19]}…")
    return {"tag": tag, "digest": digest, "uri": f"{repo_uri}@{digest}"}
