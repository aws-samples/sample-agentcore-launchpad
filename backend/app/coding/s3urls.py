"""Presigned S3 URLs and the per-project prefix in the workspace artifacts bucket.

The coding runtime's role has no S3 permission (the microVM shell can read its
credentials): snapshots, restores and exports move through presigned URLs this
backend mints with the workspace's own credentials, one per transfer and never
ahead of time — a presigned URL lives no longer than the credentials that signed
it (an assumed cross-account role lasts ≤ 1 h).

Layout: ``coding/<workspace_id>/<project_id>/{snapshots,exports}/<utc-ts>.<ext>``.
The newest ``KEEP_SNAPSHOTS`` snapshots are kept; deleting a project deletes its
whole prefix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from botocore.config import Config

from app.services.workspace import WorkspaceContext

PREFIX = "coding"
KEEP_SNAPSHOTS = 3
# Long enough for a 50 MB transfer from the microVM; short enough to be useless later.
URL_TTL_S = 900
_SIGV4 = "coding-s3v4"


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
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")


def snapshot_key(workspace_id: str, project_id: str, now: datetime | None = None) -> str:
    return f"{project_prefix(workspace_id, project_id)}snapshots/{_stamp(now)}.tgz"


def export_key(workspace_id: str, project_id: str, now: datetime | None = None) -> str:
    return f"{project_prefix(workspace_id, project_id)}exports/{_stamp(now)}.zip"


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


def delete_project_prefix(ctx: WorkspaceContext, workspace_id: str, project_id: str, *,
                          client: Any = None) -> int:
    """Delete every object under the project's prefix (snapshots + exports)."""
    s3 = client or _s3(ctx)
    bucket = bucket_of(ctx)
    keys = _list_keys(s3, bucket, project_prefix(workspace_id, project_id))
    return _delete_keys(s3, bucket, keys) if keys else 0
