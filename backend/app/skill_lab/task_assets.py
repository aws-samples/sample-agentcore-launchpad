"""Workspace-scoped staging and validation for Skill Lab binary task inputs."""

from __future__ import annotations

import codecs
import hashlib
import json
import secrets
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import Request, UploadFile
from fastapi.responses import JSONResponse

from app.core.config import DATA_DIR
from app.core.errors import AppError

STAGING_DIR = DATA_DIR / "skill-lab" / "task-asset-staging"
ASSETS_DIRNAME = "assets"
STAGING_TTL = timedelta(hours=24)
MAX_FILES_PER_UPLOAD = 32
MAX_FILES_PER_TASK = 32
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TASK_BYTES = 100 * 1024 * 1024
MAX_TASKSET_REFERENCES = 256
MAX_TASKSET_UNIQUE_BYTES = 200 * 1024 * 1024
# Content-Length includes multipart framing. One MiB leaves ample room for 32
# ordinary part headers while still rejecting an oversized request before the
# Starlette multipart parser allocates/spools every part.
MAX_UPLOAD_REQUEST_BYTES = MAX_TASK_BYTES + 1024 * 1024
_CHUNK = 1024 * 1024
_XLSX_MAX_MEMBERS = 10_000
_XLSX_MAX_UNCOMPRESSED = 200 * 1024 * 1024
_XLSX_MAX_RATIO = 100
_MEDIA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
}
_EXTENSIONS = {
    ".xlsx": "xlsx",
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".webp": "webp",
    ".md": "md",
    ".txt": "txt",
    ".csv": "csv",
}
# Text formats carry no signature to sniff, so they are verified by content class
# instead: decodable as UTF-8, free of NUL, and not a binary payload wearing a
# text extension.
_TEXT_KINDS = frozenset({"md", "txt", "csv"})
_BINARY_SIGNATURES = (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"PK\x03\x04")


def _error(code: str, message: str, status: int = 422) -> AppError:
    return AppError(f"skill_lab.{code}", message, status_code=status)


def workspace_key(workspace_id: str) -> str:
    return hashlib.sha256(workspace_id.encode()).hexdigest()[:32]


def validate_destination(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith(("/", "\\", "~")) or "\\" in path:
        raise _error("asset_path_invalid", f"unsafe task asset destination {path!r}")
    parts = PurePosixPath(path).parts
    if not parts or any(part in ("", ".", "..") for part in path.split("/")):
        raise _error("asset_path_invalid", f"unsafe task asset destination {path!r}")
    if parts[0].casefold() in {".agents", ".claude", ".codex", ".git", "task.md"}:
        raise _error("asset_path_invalid", f"task asset destination {path!r} is reserved")
    return path


async def task_asset_body_limit_middleware(request: Request, call_next: Any) -> Any:
    """Reject known-oversize multipart bodies before Starlette parses them.

    This is deliberately an exact route policy rather than a global body cap:
    JSON APIs have their own schema limits. Chunked requests have no
    Content-Length and therefore continue to the streamed per-file enforcement
    in :func:`stage_uploads`. Deployments should also enforce an ingress/proxy
    body limit when they need a hard whole-request cap for chunked transfer.
    """
    if request.method == "POST" and request.url.path == "/api/skill-lab/task-assets":
        raw_length = request.headers.get("content-length")
        try:
            length = int(raw_length) if raw_length is not None else None
        except ValueError:
            length = None
        if length is not None and length > MAX_UPLOAD_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={
                    "code": "skill_lab.asset_request_too_large",
                    "message": (
                        "task asset upload request exceeds the 101 MiB multipart limit"
                    ),
                    "detail": None,
                },
            )
    return await call_next(request)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def sweep_expired(now: datetime | None = None) -> None:
    now = now or _now()
    if not STAGING_DIR.exists():
        return
    for metadata_path in STAGING_DIR.glob("*/*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(str(metadata["expires_at"]))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            expires = datetime.min.replace(tzinfo=UTC)
        if expires <= now:
            shutil.rmtree(metadata_path.parent, ignore_errors=True)


def _verify_text(path: Path, name: str) -> None:
    """Content-class check for the text formats, which have no signature.

    Streams the blob: a text asset may be 25 MiB and there is no reason to hold
    it in memory. WebP's signature needs 12 bytes, so a 16-byte head covers
    every entry in `_BINARY_SIGNATURES`.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
            if any(head.startswith(signature) for signature in _BINARY_SIGNATURES):
                raise _error(
                    "asset_format_invalid",
                    f"task asset {name!r} does not match its extension",
                )
            chunk = head
            while chunk:
                if b"\x00" in chunk:
                    raise _error(
                        "asset_format_invalid",
                        f"text task asset {name!r} contains NUL bytes",
                    )
                decoder.decode(chunk)
                chunk = handle.read(_CHUNK)
        # Catches a multibyte sequence truncated at EOF, which the incremental
        # decoder holds back rather than rejecting mid-stream.
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise _error(
            "asset_format_invalid", f"text task asset {name!r} is not valid UTF-8: {exc}"
        ) from exc


def _detect(path: Path, name: str) -> tuple[str, str]:
    expected = _EXTENSIONS.get(Path(name).suffix.casefold())
    if expected is None:
        raise _error("asset_format_invalid", f"unsupported task asset extension for {name!r}")
    if expected in _TEXT_KINDS:
        _verify_text(path, name)
        return expected, _MEDIA[expected]
    with path.open("rb") as handle:
        head = handle.read(16)
    detected = None
    if head.startswith(b"%PDF-"):
        detected = "pdf"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "png"
    elif head.startswith(b"\xff\xd8\xff"):
        detected = "jpg"
    elif len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        detected = "webp"
    elif head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                names = {info.filename for info in infos}
                if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                    raise ValueError("required workbook members are missing")
                if len(infos) > _XLSX_MAX_MEMBERS:
                    raise ValueError("too many workbook members")
                total = sum(info.file_size for info in infos)
                if total > _XLSX_MAX_UNCOMPRESSED:
                    raise ValueError("workbook expands beyond the safety limit")
                if any(
                    info.file_size > max(1, info.compress_size) * _XLSX_MAX_RATIO for info in infos
                ):
                    raise ValueError("unsafe workbook compression ratio")
                if any(
                    info.filename.casefold().endswith(("vbaproject.bin", ".xlsm")) for info in infos
                ):
                    raise ValueError("macro-enabled workbooks are unsupported")
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            raise _error("asset_format_invalid", f"invalid XLSX {name!r}: {exc}") from exc
        detected = "xlsx"
    if detected != expected:
        raise _error("asset_format_invalid", f"task asset {name!r} does not match its extension")
    return detected, _MEDIA[detected]


async def stage_uploads(workspace_id: str, uploads: list[UploadFile]) -> list[dict[str, Any]]:
    sweep_expired()
    if not uploads:
        raise _error("asset_upload_empty", "at least one task asset is required")
    if len(uploads) > MAX_FILES_PER_UPLOAD:
        raise _error(
            "asset_limit_exceeded", f"at most {MAX_FILES_PER_UPLOAD} files may be uploaded"
        )
    names = [Path(upload.filename or "").name for upload in uploads]
    if any(
        not name or name != (upload.filename or "")
        for name, upload in zip(names, uploads, strict=True)
    ):
        raise _error("asset_name_invalid", "task asset names must be plain filenames")
    folded = [name.casefold() for name in names]
    if len(set(folded)) != len(folded):
        raise _error("asset_duplicate_name", "duplicate task asset filenames are not allowed")

    stage_id = secrets.token_urlsafe(18)
    stage_dir = STAGING_DIR / workspace_key(workspace_id) / stage_id
    blobs = stage_dir / "blobs"
    blobs.mkdir(parents=True)
    created = _now()
    records: list[dict[str, Any]] = []
    total_size = 0
    try:
        for upload, name in zip(uploads, names, strict=True):
            token = "ta_" + secrets.token_urlsafe(24)
            blob = blobs / token
            digest = hashlib.sha256()
            size = 0
            with blob.open("wb") as target:
                while chunk := await upload.read(_CHUNK):
                    size += len(chunk)
                    total_size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise _error("asset_too_large", f"task asset {name!r} exceeds 25 MiB", 413)
                    if total_size > MAX_TASK_BYTES:
                        raise _error(
                            "asset_limit_exceeded",
                            "task asset upload exceeds the 100 MiB aggregate limit",
                            413,
                        )
                    digest.update(chunk)
                    target.write(chunk)
            kind, media_type = _detect(blob, name)
            records.append(
                {
                    "staged_asset": token,
                    "name": name,
                    "media_type": media_type,
                    "size": size,
                    "sha256": digest.hexdigest(),
                    "format": kind,
                }
            )
        metadata = {
            "created_at": _iso(created),
            "expires_at": _iso(created + STAGING_TTL),
            "assets": records,
        }
        (stage_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    return [
        {k: record[k] for k in ("staged_asset", "name", "media_type", "size")} for record in records
    ]


def resolve_staged(workspace_id: str, token: str) -> tuple[dict[str, Any], Path, Path]:
    if not isinstance(token, str) or not token.startswith("ta_") or "/" in token or "\\" in token:
        raise _error("asset_token_invalid", "invalid staged task asset token")
    root = STAGING_DIR / workspace_key(workspace_id)
    for metadata_path in root.glob("*/metadata.json") if root.exists() else ():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(str(metadata["expires_at"]))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        for record in metadata.get("assets", []):
            if record.get("staged_asset") == token:
                if expires <= _now():
                    raise _error("asset_token_expired", "staged task asset has expired")
                blob = metadata_path.parent / "blobs" / token
                _verify_blob(blob, record)
                return record, blob, metadata_path.parent
    raise _error("asset_token_not_found", "staged task asset was not found", 404)


def _verify_blob(path: Path, record: dict[str, Any]) -> None:
    try:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(_CHUNK):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise _error("asset_missing", "task asset bytes are missing") from exc
    if size != int(record.get("size", -1)) or digest.hexdigest() != record.get("sha256"):
        raise _error("asset_digest_mismatch", "task asset bytes failed integrity verification")


def stable_descriptor(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset": f"sha256:{record['sha256']}",
        "name": str(record["name"]),
        "media_type": str(record["media_type"]),
        "size": int(record["size"]),
    }


def digest_from_descriptor(value: dict[str, Any]) -> str:
    asset = value.get("asset")
    if not isinstance(asset, str) or not asset.startswith("sha256:"):
        raise _error("asset_descriptor_invalid", "invalid stable task asset descriptor")
    digest = asset.removeprefix("sha256:")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise _error("asset_descriptor_invalid", "invalid stable task asset digest")
    return digest
