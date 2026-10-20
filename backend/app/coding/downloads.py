"""Browser downloads of a coding project: one file, or the whole project as a zip.

Bytes never pass through ``InvokeAgentRuntime`` (its payloads are short JSON, and
the backend is no file proxy). The backend mints a presigned PUT under the
project's ``downloads/`` prefix; the runtime uploads the file (``upload_file``, any
regular file up to 200 MB, text or binary) or the project zip (``export`` — the
deploy handoff's file set: tracked + untracked-not-ignored, no ``.git``, no
dependency dirs) to it; the backend answers a presigned GET that lives
``DOWNLOAD_URL_TTL_S`` and whose ``Content-Disposition: attachment`` makes the
browser save the object under its file name (RFC 6266 / 5987: an ASCII
``filename`` plus the UTF-8 ``filename*``).

Owner-only: the router resolves the project through ``projects.require_project``
without ``admin_ok`` (an administrator can delete a member's project but never read
its files). Allowed while a turn runs — it only reads. Download objects older than
``s3urls.DOWNLOAD_KEEP_S`` are pruned at each request; deleting the project deletes
its whole prefix.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

from . import files, runtime_client, s3urls
from .deploy import agent_name_from
from .models import CodingProject
from .runtime_client import CodingRuntimeError

logger = logging.getLogger("launchpad.coding")

# The runtime's caps (image/src/workspace.mjs MAX_UPLOAD_BYTES / MAX_EXPORT_BYTES).
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
ARCHIVE_TYPE = "application/zip"
FILE_TYPE = "application/octet-stream"
_FALLBACK_NAME = "download"
# what a header value or a quoted-string must not carry (CR/LF would split the header)
_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\/]')
_NOT_ASCII_SAFE = re.compile(r"[^A-Za-z0-9._()+ -]")
_NOT_SLUG = re.compile(r"[^\w-]+")


def clean_filename(name: str) -> str:
    """A file name safe in a header value: no control characters, quotes,
    backslashes or slashes; never empty."""
    cleaned = _UNSAFE.sub("", name or "").strip()
    return cleaned if cleaned.strip(".") else _FALLBACK_NAME


def ascii_filename(name: str) -> str:
    """The ASCII stand-in for ``filename=`` (and the S3 key): every character
    outside a conservative set becomes ``_``."""
    return _NOT_ASCII_SAFE.sub("_", clean_filename(name))


def archive_names(project_name: str) -> tuple[str, str]:
    """``(display, ascii)`` names of the project zip: a slug of the project name
    that keeps its own letters (``天气 助手`` → ``天气-助手.zip``); a non-ASCII
    slug's stand-in is the project's Agent-name slug (the deploy handoff's)."""
    slug = _NOT_SLUG.sub("-", (project_name or "").lower()).strip("-_")[:80].strip("-_")
    display = f"{slug or agent_name_from(project_name)}.zip"
    return display, display if display.isascii() else f"{agent_name_from(project_name)}.zip"


def content_disposition(display: str, ascii_name: str | None = None) -> str:
    """``attachment; filename="…"; filename*=UTF-8''…`` (RFC 6266 + RFC 5987).
    Percent-encoding leaves only attr-chars, so the value is plain ASCII."""
    name = clean_filename(display)
    fallback = ascii_filename(ascii_name or name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _too_large(exc: CodingRuntimeError, max_bytes: int) -> AppError:
    detail: dict[str, Any] = {"max_bytes": max_bytes}
    size = exc.detail.get("size")
    if isinstance(size, int) and not isinstance(size, bool):
        detail["size"] = size
    return AppError("coding.download_too_large",
                    f"too large to download (at most {max_bytes // (1024 * 1024)} MB)",
                    detail, status_code=413)


def download(ctx: WorkspaceContext, workspace_id: str, project: CodingProject,
             path: str | None) -> dict[str, Any]:
    """Stage one file (``path``) or the project zip (``path`` None) for the
    browser; ``{url, filename, size, expires_in}``."""
    rel = files.clean_path(path) if path is not None else None
    runtime_client.require_runtime_arn(ctx)
    try:
        s3urls.prune_downloads(ctx, workspace_id, project.id)
    except Exception as exc:  # noqa: BLE001 — a leftover download costs a few bytes
        logger.warning("coding project %s: download prune failed: %s", project.id, exc)

    if rel is None:
        display, ascii_name = archive_names(project.name)
        action, payload, content_type, cap = "export", {}, ARCHIVE_TYPE, MAX_ARCHIVE_BYTES
    else:
        display = clean_filename(rel.rsplit("/", 1)[-1])
        ascii_name = ascii_filename(display)
        action, payload, content_type, cap = ("upload_file", {"path": rel}, FILE_TYPE,
                                              MAX_FILE_BYTES)
    key = s3urls.download_key(workspace_id, project.id, ascii_filename(ascii_name))
    put_url = s3urls.presign_put(ctx, key)
    try:
        result = runtime_client.project_call(ctx, project, action, put_url=put_url,
                                             **payload)
    except CodingRuntimeError as exc:
        if exc.code == "not_found" and rel is not None:
            raise AppError("coding.file_not_found", "the file does not exist in the project",
                           {"path": rel}, status_code=404) from exc
        if exc.code == "file_too_large":
            raise _too_large(exc, cap) from exc
        if exc.code == "bad_request" and exc.message.startswith("unknown action"):
            # a runtime installed before `upload_file` existed: a repair rolls it out
            raise AppError("coding.runtime_outdated",
                           "the coding runtime predates file downloads — an administrator "
                           "repairs it from the coding console", status_code=409) from exc
        raise runtime_client.api_error(exc) from exc
    url = s3urls.presign_download(
        ctx, key, disposition=content_disposition(display, ascii_name),
        content_type=content_type,
    )
    size = result.get("bytes") if isinstance(result, dict) else None
    logger.info("coding project %s: %s download staged (%s bytes)", project.id,
                "archive" if rel is None else "file", size)
    return {
        "url": url,
        "filename": display,
        "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
        "expires_in": s3urls.DOWNLOAD_URL_TTL_S,
    }
