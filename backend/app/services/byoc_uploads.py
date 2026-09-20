"""BYOC artifact staging: zip validation, detection and S3 storage.

The upload endpoint (`POST /api/agents/uploads`) streams the member's zip to a
temp file, validates the archive WITHOUT executing anything in it, stores it to
the workspace artifacts bucket under ``byoc/{workspace_id}/{upload_id}/source.zip``
next to a ``manifest.json`` (detection summary + provenance), and returns the
summary. The deploy pipeline later downloads the object by ``upload_id`` and
re-validates on extraction — the S3 object is admin-writable in principle, so
package-time checks are defense in depth, not duplication.

Limits follow the AgentCore direct-code artifact caps (250 MiB zip / 750 MiB
uncompressed); entry-shape rules mirror ``skill_ingest``'s safe extractor
(no absolute paths, no ``..`` traversal, no symlinks, bounded entry count).
"""

from __future__ import annotations

import json
import re
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import AppError, NotFoundError
from app.core.runtime_target import TARGET_PYTHON
from app.services import requirements_txt
from app.services.workspace import WorkspaceContext

# AgentCore direct-code artifact limits (also enforced for container_source zips
# — CodeBuild contexts have no service cap this small, but one build contract is
# simpler to explain than two).
MAX_ZIP_BYTES = 250 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 750 * 1024 * 1024
MAX_ENTRIES = 20_000
# Content-Length includes multipart framing; 1 MiB of headroom mirrors the
# skill-lab guard.
UPLOAD_REQUEST_MAX_BYTES = MAX_ZIP_BYTES + 1024 * 1024

UPLOAD_PATH = "/api/agents/uploads"
UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_CHUNK = 1024 * 1024
# requirements.txt larger than this is not a requirements file
_REQUIREMENTS_MAX_BYTES = 256 * 1024
# Only .py members this size or smaller are content-scanned for the SDK markers;
# bigger ones are almost certainly vendored artifacts, not the user's entrypoint.
_SDK_SCAN_MAX_BYTES = 1024 * 1024
_SDK_SCAN_MAX_FILES = 400
_SDK_MARKERS = (b"BedrockAgentCoreApp", b"@app.entrypoint")


def _error(code: str, message: str, status: int = 422) -> AppError:
    return AppError(f"byoc.{code}", message, status_code=status)


def new_upload_id() -> str:
    return uuid.uuid4().hex


def source_key(workspace_id: str, upload_id: str) -> str:
    return f"byoc/{workspace_id}/{upload_id}/source.zip"


def manifest_key(workspace_id: str, upload_id: str) -> str:
    return f"byoc/{workspace_id}/{upload_id}/manifest.json"


async def upload_body_limit_middleware(request: Request, call_next: Any) -> Any:
    """Reject a known-oversize BYOC upload before Starlette parses the multipart
    body — the same exact-route pattern as the skill-lab asset guard. Chunked
    requests (no Content-Length) fall through to the streamed per-file cap in
    :func:`stage_upload`."""
    if request.method == "POST" and request.url.path == UPLOAD_PATH:
        raw_length = request.headers.get("content-length")
        try:
            length = int(raw_length) if raw_length is not None else None
        except ValueError:
            length = None
        if length is not None and length > UPLOAD_REQUEST_MAX_BYTES:
            return JSONResponse(
                status_code=413,
                content={
                    "code": "byoc.upload_request_too_large",
                    "message": "BYOC upload exceeds the 250 MiB zip limit",
                    "detail": None,
                },
            )
    return await call_next(request)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _reject_unsafe_name(name: str) -> None:
    norm = name.replace("\\", "/")
    if norm.startswith("/"):
        raise _error("zip_entry_unsafe", f"zip entry '{name}' uses an absolute path")
    if len(norm) >= 2 and norm[1] == ":":  # Windows drive letter (C:/...)
        raise _error("zip_entry_unsafe", f"zip entry '{name}' uses an absolute path")
    if ".." in PurePosixPath(norm).parts:
        raise _error("zip_entry_unsafe", f"zip entry '{name}' escapes the archive root")


def _root_prefix(names: list[str]) -> str:
    """'' when files live at the archive root, else the single top-level
    directory ('myagent/') every entry sits under — the normalization that lets
    `zip -r agent.zip myagent/` and zipping the directory contents both work."""
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) != 1:
        return ""
    top = next(iter(tops))
    # a single file at the root ("main.py") is the root itself, not a dir
    if all("/" not in name for name in names):
        return ""
    return f"{top}/" if all(name.startswith(f"{top}/") for name in names) else ""


def validate_and_detect(path: Path) -> dict[str, Any]:
    """Validate the archive shape and report what's inside — never extracts.

    Returns ``{entries_count, uncompressed_bytes, root_prefix, detected:{...}}``.
    Raises AppError (422) on any safety violation.
    """
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise _error("zip_invalid", "uploaded file is not a valid zip archive") from exc

    with archive as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if not infos:
            raise _error("zip_empty", "the zip contains no files")
        if len(infos) > MAX_ENTRIES:
            raise _error(
                "zip_too_many_entries",
                f"the zip has more than {MAX_ENTRIES} entries",
            )
        total = 0
        for info in infos:
            if _is_symlink(info):
                raise _error(
                    "zip_entry_unsafe", f"zip entry '{info.filename}' is a symlink — refused"
                )
            _reject_unsafe_name(info.filename)
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise _error(
                    "zip_uncompressed_too_large",
                    "the zip expands beyond the 750 MiB uncompressed limit",
                )

        names = [info.filename for info in infos]
        root = _root_prefix(names)
        rel = [n[len(root):] for n in names]

        candidates = sorted(
            (n for n in rel if n.endswith(".py") and "/" not in n),
            # main.py / app.py first — the overwhelmingly common entrypoints
            key=lambda n: (n not in ("main.py", "app.py"), n),
        )
        detected = {
            "entrypoint_candidates": candidates[:50],
            "has_requirements": "requirements.txt" in rel,
            "has_dockerfile": "Dockerfile" in rel,
            "agentcore_sdk_detected": _scan_for_sdk(zf, root, rel),
        }
        return {
            "entries_count": len(infos),
            "uncompressed_bytes": total,
            "root_prefix": root,
            "detected": detected,
        }


def _requirements_text(path: Path, root: str) -> str | None:
    """The zip's root requirements.txt content, or None (absent / oversized)."""
    with zipfile.ZipFile(path) as zf:
        try:
            info = zf.getinfo(f"{root}requirements.txt")
        except KeyError:
            return None
        if info.file_size > _REQUIREMENTS_MAX_BYTES:
            return None
        return zf.read(info).decode("utf-8", errors="replace")


def check_requirements(
    path: Path, root: str, python_version: str = "PYTHON_3_13"
) -> dict[str, Any]:
    """Dry-resolve the zip's requirements.txt against the deploy target, so the
    wizard surfaces an unresolvable file before a deploy is even attempted:
    ``{status: ok|failed|skipped, package_count, error}``. Nothing from the zip
    is executed — the resolver only reads index metadata."""
    text = _requirements_text(path, root)
    if text is None:
        return {"status": "skipped", "package_count": None,
                "error": "no requirements.txt in the zip"}
    return requirements_txt.preresolve(
        text,
        python_version=requirements_txt.pip_python_version(python_version or TARGET_PYTHON),
        hints=requirements_txt.RESOLVE_FIX_HINTS,
    )


def _scan_for_sdk(zf: zipfile.ZipFile, root: str, rel_names: list[str]) -> bool:
    """True when any small root-adjacent .py member mentions the AgentCore SDK
    entrypoint contract. A *reading* scan only — nothing is imported or run."""
    scanned = 0
    for rel in rel_names:
        if not rel.endswith(".py") or rel.count("/") > 1:
            continue
        info = zf.getinfo(root + rel)
        if info.file_size > _SDK_SCAN_MAX_BYTES:
            continue
        data = zf.read(info)
        if any(marker in data for marker in _SDK_MARKERS):
            return True
        scanned += 1
        if scanned >= _SDK_SCAN_MAX_FILES:
            break
    return False


def stage_upload(
    workspace: WorkspaceContext,
    *,
    filename: str,
    tmp_zip: Path,
    sha256: str,
    size_bytes: int,
    uploaded_by: str,
    uploaded_at: str,
    python_version: str = "PYTHON_3_13",
    s3_client: Any = None,
) -> dict[str, Any]:
    """Validate the staged temp zip, store object + manifest to S3, return the
    manifest. The caller has already streamed the request body to ``tmp_zip``
    (enforcing the 250 MiB cap) and computed its digest."""
    bucket = workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    report = validate_and_detect(tmp_zip)
    if report["detected"]["has_requirements"]:
        report["detected"]["requirements"] = check_requirements(
            tmp_zip, report["root_prefix"], python_version
        )
    else:
        report["detected"]["requirements"] = {
            "status": "skipped", "package_count": None,
            "error": "no requirements.txt in the zip",
        }
    upload_id = new_upload_id()
    manifest = {
        "upload_id": upload_id,
        "workspace_id": workspace.id,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "original_filename": filename,
        "uploaded_by": uploaded_by,
        "uploaded_at": uploaded_at,
        **report,
    }
    s3 = s3_client or workspace.client("s3")
    s3.upload_file(str(tmp_zip), bucket, source_key(workspace.id, upload_id))
    s3.put_object(
        Bucket=bucket,
        Key=manifest_key(workspace.id, upload_id),
        Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    return manifest


def get_manifest(
    workspace: WorkspaceContext, upload_id: str, s3_client: Any = None
) -> dict[str, Any]:
    """The stored manifest, or 404. The key embeds the caller's workspace id, so
    another workspace's upload_id is indistinguishable from a missing one."""
    if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
        raise NotFoundError("byoc.upload_not_found", "upload not found")
    bucket = workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    s3 = s3_client or workspace.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=manifest_key(workspace.id, upload_id))
    except Exception as exc:  # NoSuchKey and friends → uniform 404
        if type(exc).__name__ in ("NoSuchKey", "ClientError", "ResourceNotFoundException"):
            raise NotFoundError("byoc.upload_not_found", "upload not found") from exc
        raise
    return json.loads(body["Body"].read())


def download_upload(
    workspace: WorkspaceContext,
    workspace_id: str,
    upload_id: str,
    dest: Path,
    s3_client: Any = None,
) -> None:
    bucket = workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    s3 = s3_client or workspace.client("s3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, source_key(workspace_id, upload_id), str(dest))


def extract_zip(zip_path: Path, dest: Path) -> Path:
    """Safely extract a validated BYOC zip; returns the effective source root
    (``dest`` or the single top-level directory inside it).

    Runs the same shape checks as upload-time validation — the pipeline may be
    resuming from an object that was re-written after validation."""
    report = validate_and_detect(zip_path)
    dest_root = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            if not (target == dest_root or dest_root in target.parents):
                raise _error(
                    "zip_entry_unsafe", f"zip entry '{info.filename}' escapes the extract root"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(_CHUNK):
                    out.write(chunk)
    root = report["root_prefix"]
    return dest / root.rstrip("/") if root else dest


def delete_upload_objects(
    workspace: WorkspaceContext, workspace_id: str, upload_id: str, s3_client: Any = None
) -> None:
    """Best-effort removal of the staged zip + manifest (agent delete path)."""
    if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
        return
    bucket = workspace.resources.get("artifacts_bucket")
    if not bucket:
        return
    s3 = s3_client or workspace.client("s3")
    for key in (source_key(workspace_id, upload_id), manifest_key(workspace_id, upload_id)):
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:  # noqa: BLE001 — cleanup must never block a delete
            pass
