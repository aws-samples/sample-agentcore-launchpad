"""Presigned S3 URLs and the per-project prefix in the workspace artifacts bucket.

The coding runtime's role has no S3 permission (the microVM shell can read its
credentials): snapshots, restores and exports move through presigned URLs this
backend mints with the workspace's own credentials, one per transfer and never
ahead of time — a presigned URL lives no longer than the credentials that signed
it (an assumed cross-account role lasts ≤ 1 h).

Layout: ``coding/<workspace_id>/<project_id>/{snapshots,exports}/<utc-ts>.<ext>``
and ``…/downloads/<utc-ts>-<rand>/<filename>`` (a browser download: the runtime PUTs
the file or the project zip, the browser GETs it through a short-lived URL). The
newest ``KEEP_SNAPSHOTS`` snapshots are kept; downloads older than
``DOWNLOAD_KEEP_S`` are pruned at the next download; deleting a project deletes its
whole prefix.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.config import Config

from app.services.workspace import WorkspaceContext

PREFIX = "coding"
KEEP_SNAPSHOTS = 3
# Long enough for a 50 MB transfer from the microVM; short enough to be useless later.
URL_TTL_S = 900
# A browser download URL is used at once (the console clicks it as it arrives).
DOWNLOAD_URL_TTL_S = 300
# Older download objects are deleted at the next download of the project (well past
# the URL's TTL, so a URL being used is never pruned).
DOWNLOAD_KEEP_S = 15 * 60
_SIGV4 = "coding-s3v4"
_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


def _s3(ctx: WorkspaceContext) -> Any:
    # SigV4 (regional host) — a global-endpoint presign would redirect, and the
    # runtime's plain HTTP PUT does not follow redirects.
    return ctx.client("s3", cache_token=_SIGV4, config=Config(signature_version="s3v4"))


def bucket_of(ctx: WorkspaceContext) -> str:
    bucket = str((ctx.resources or {}).get("artifacts_bucket") or "")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    return bucket


def project_prefix(workspace_id: str, project_id: str) -> str:
    return f"{PREFIX}/{workspace_id}/{project_id}/"


def _stamp(now: datetime | None = None) -> str:
    # microseconds: two snapshots in one second must not share a key
    return (now or datetime.now(UTC)).strftime(_STAMP_FORMAT)


def snapshot_key(workspace_id: str, project_id: str, now: datetime | None = None) -> str:
    return f"{project_prefix(workspace_id, project_id)}snapshots/{_stamp(now)}.tgz"


def export_key(workspace_id: str, project_id: str, now: datetime | None = None) -> str:
    return f"{project_prefix(workspace_id, project_id)}exports/{_stamp(now)}.zip"


def downloads_prefix(workspace_id: str, project_id: str) -> str:
    return f"{project_prefix(workspace_id, project_id)}downloads/"


def download_key(workspace_id: str, project_id: str, filename: str,
                 now: datetime | None = None) -> str:
    """``…/downloads/<utc-ts>-<rand>/<filename>``: the stamp dates the object for
    the prune; the random part keeps two downloads in one instant apart."""
    return (f"{downloads_prefix(workspace_id, project_id)}"
            f"{_stamp(now)}-{secrets.token_hex(4)}/{filename}")


def presign_put(ctx: WorkspaceContext, key: str, *, ttl: int = URL_TTL_S,
                client: Any = None) -> str:
    return (client or _s3(ctx)).generate_presigned_url(
        "put_object", Params={"Bucket": bucket_of(ctx), "Key": key}, ExpiresIn=ttl
    )


def presign_get(ctx: WorkspaceContext, key: str, *, ttl: int = URL_TTL_S,
                client: Any = None) -> str:
    return (client or _s3(ctx)).generate_presigned_url(
        "get_object", Params={"Bucket": bucket_of(ctx), "Key": key}, ExpiresIn=ttl
    )


def presign_download(ctx: WorkspaceContext, key: str, *, disposition: str,
                     content_type: str, ttl: int = DOWNLOAD_URL_TTL_S,
                     client: Any = None) -> str:
    """A presigned GET whose answer carries ``Content-Disposition`` and
    ``Content-Type`` overrides (S3 ``response-*`` query parameters): the browser
    saves the object under ``disposition``'s file name instead of rendering it."""
    return (client or _s3(ctx)).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket_of(ctx),
            "Key": key,
            "ResponseContentDisposition": disposition,
            "ResponseContentType": content_type,
        },
        ExpiresIn=ttl,
    )


def object_size(ctx: WorkspaceContext, key: str, *, client: Any = None) -> int:
    """The object's size in bytes (``HeadObject``)."""
    head = (client or _s3(ctx)).head_object(Bucket=bucket_of(ctx), Key=key)
    return int(head.get("ContentLength") or 0)


def download(ctx: WorkspaceContext, key: str, path: str, *, client: Any = None) -> None:
    (client or _s3(ctx)).download_file(bucket_of(ctx), key, path)


def delete_key(ctx: WorkspaceContext, key: str, *, client: Any = None) -> None:
    (client or _s3(ctx)).delete_object(Bucket=bucket_of(ctx), Key=key)


def _list_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            params["ContinuationToken"] = token
        page = s3.list_objects_v2(**params)
        keys.extend(str(item["Key"]) for item in page.get("Contents") or [])
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def _delete_keys(s3: Any, bucket: str, keys: list[str]) -> int:
    deleted = 0
    for start in range(0, len(keys), 1000):
        chunk = keys[start:start + 1000]
        answer = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
        )
        errors = answer.get("Errors") or []
        if errors:
            first = errors[0]
            raise RuntimeError(
                f"S3 refused to delete {len(errors)} object(s), e.g. {first.get('Key')}: "
                f"{first.get('Code')} {first.get('Message')}"
            )
        deleted += len(chunk)
    return deleted


def prune_snapshots(ctx: WorkspaceContext, workspace_id: str, project_id: str, *,
                    keep_key: str | None = None, keep: int = KEEP_SNAPSHOTS,
                    client: Any = None) -> list[str]:
    """Delete all but the newest ``keep`` snapshots (keys sort by time); the
    project's current ``keep_key`` is never deleted. Returns the deleted keys."""
    s3 = client or _s3(ctx)
    bucket = bucket_of(ctx)
    keys = sorted(_list_keys(s3, bucket, f"{project_prefix(workspace_id, project_id)}snapshots/"))
    doomed = [k for k in keys[:-keep] if k != keep_key] if keep > 0 else []
    if doomed:
        _delete_keys(s3, bucket, doomed)
    return doomed


def _download_stamp(key: str, prefix: str) -> datetime | None:
    """When a download object was minted, from its ``<utc-ts>-<rand>/`` segment."""
    segment = key[len(prefix):].split("/", 1)[0]
    try:
        return datetime.strptime(segment.split("-", 1)[0], _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def prune_downloads(ctx: WorkspaceContext, workspace_id: str, project_id: str, *,
                    keep_s: int = DOWNLOAD_KEEP_S, now: datetime | None = None,
                    client: Any = None) -> list[str]:
    """Delete the project's download objects minted more than ``keep_s`` seconds
    ago (an object whose key carries no stamp is not one this module wrote: it
    goes too). Returns the deleted keys."""
    s3 = client or _s3(ctx)
    bucket = bucket_of(ctx)
    prefix = downloads_prefix(workspace_id, project_id)
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=keep_s)
    doomed = []
    for key in _list_keys(s3, bucket, prefix):
        minted = _download_stamp(key, prefix)
        if minted is None or minted < cutoff:
            doomed.append(key)
    if doomed:
        _delete_keys(s3, bucket, doomed)
    return doomed


def delete_project_prefix(ctx: WorkspaceContext, workspace_id: str, project_id: str, *,
                          client: Any = None) -> int:
    """Delete every object under the project's prefix (snapshots, exports and
    downloads)."""
    s3 = client or _s3(ctx)
    bucket = bucket_of(ctx)
    keys = _list_keys(s3, bucket, project_prefix(workspace_id, project_id))
    return _delete_keys(s3, bucket, keys) if keys else 0
