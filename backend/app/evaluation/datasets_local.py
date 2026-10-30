"""Dataset "cloud" copies as immutable S3 snapshots (N12 E1-2; contract amendment CA-6).

The Global console synced a local dataset to an AgentCore *Dataset* (draft edited in
place, published numbered versions). That service does not exist in aws-cn, so the cloud
copy is a snapshot store in the workspace artifacts bucket::

    eval-datasets/<cloud_id>/meta.json   {datasetId, datasetName, workspaceId, schemaType,
                                          description, status, draftStatus, exampleCount,
                                          failureReason, createdAt, updatedAt, versions[]}
    eval-datasets/<cloud_id>/draft.json  the current examples (normalized scenarios)
    eval-datasets/<cloud_id>/v<n>.json   one immutable published snapshot per version

The row's ``cloud`` blob keeps the baseline keys (``dataset_id, arn, status, synced_at,
failure_reason, draft_status, example_count, versions``) and the UI copy stays verbatim.
The S3 client is the funnel's (``ws.client("s3")``), constructed in exactly one function
here so tests inject a fake.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.core.errors import AppError, NotFoundError
from app.services.workspace import WorkspaceContext

PREFIX = "eval-datasets"
META = "meta.json"
DRAFT = "draft.json"
# The schema identifiers the UI already knows (the baseline's AWS schema type names).
DATASET_SCHEMA_TYPES = {
    "legacy": "AGENTCORE_EVALUATION_PREDEFINED_V1",
    "predefined": "AGENTCORE_EVALUATION_PREDEFINED_V1",
    "simulated": "AGENTCORE_EVALUATION_SIMULATED_V1",
}
RUNNABLE_SCHEMAS = frozenset(DATASET_SCHEMA_TYPES.values())
_VERSION_RE = re.compile(r"^v(\d+)\.json$")
_MISSING = ("NoSuchKey", "404", "NotFound")


def s3_client(ws: WorkspaceContext) -> Any:
    """The one construction point (funnel); tests monkeypatch this."""
    return ws.client("s3")


def sanitize_dataset_name(name: str) -> str:
    """Coerce any display name into ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$``."""
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in name)
    cleaned = cleaned.strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"ds_{cleaned}" if cleaned else f"ds_{uuid.uuid4().hex[:8]}"
    return cleaned[:48]


def new_cloud_id() -> str:
    return f"cds_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_missing(exc: BaseException) -> bool:
    if isinstance(exc, KeyError):
        return True  # in-memory doubles
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "")) in _MISSING
    return False


class SnapshotStore:
    """One workspace's dataset snapshots in its artifacts bucket."""

    def __init__(self, s3: Any, bucket: str, workspace: WorkspaceContext) -> None:
        self._s3 = s3
        self.bucket = bucket
        self.workspace = workspace

    # ── keys ──
    @staticmethod
    def prefix(cloud_id: str) -> str:
        return f"{PREFIX}/{cloud_id}/"

    def meta_key(self, cloud_id: str) -> str:
        return self.prefix(cloud_id) + META

    def draft_key(self, cloud_id: str) -> str:
        return self.prefix(cloud_id) + DRAFT

    def version_key(self, cloud_id: str, version: str | int) -> str:
        return self.prefix(cloud_id) + f"v{int(version)}.json"

    def arn(self, cloud_id: str) -> str:
        """The snapshot store's S3 ARN (spec C2: partition-aware, via the context)."""
        return self.workspace.arn(
            "s3", f"{self.bucket}/{self.prefix(cloud_id).rstrip('/')}", region="", account=""
        )

    # ── raw objects ──
    def _get_json(self, key: str) -> Any | None:
        try:
            body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 — code-dispatched
            if _is_missing(exc):
                return None
            raise
        return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)

    def _put_json(self, key: str, payload: Any) -> None:
        self._s3.put_object(
            Bucket=self.bucket, Key=key, ContentType="application/json",
            Body=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        )

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            page = self._s3.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in page.get("Contents", []) or [])
            token = page.get("NextContinuationToken")
            if not token:
                return sorted(keys)
            kwargs["ContinuationToken"] = token

    # ── the dataset lifecycle ──
    def meta(self, cloud_id: str) -> dict[str, Any] | None:
        """The live meta of ``cloud_id`` in THIS workspace, else None."""
        meta = self._get_json(self.meta_key(cloud_id))
        if not isinstance(meta, dict) or meta.get("workspaceId") != self.workspace.id:
            return None
        return meta

    def require(self, cloud_id: str) -> dict[str, Any]:
        meta = self.meta(cloud_id)
        if meta is None:
            raise NotFoundError("dataset.not_found", f"cloud dataset {cloud_id} not found")
        return meta

    def list_meta(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key in self._list_keys(f"{PREFIX}/"):
            if key.endswith("/" + META):
                meta = self._get_json(key)
                if isinstance(meta, dict) and meta.get("workspaceId") == self.workspace.id:
                    out.append(meta)
        out.sort(key=lambda m: str(m.get("updatedAt") or ""), reverse=True)
        return out

    def create(self, *, name: str, schema_type: str, examples: list[dict[str, Any]],
               description: str = "") -> dict[str, Any]:
        cloud_id = new_cloud_id()
        now = _now()
        meta = {
            "datasetId": cloud_id, "datasetName": name, "workspaceId": self.workspace.id,
            "schemaType": schema_type, "description": description, "status": "ACTIVE",
            "draftStatus": "MODIFIED", "exampleCount": len(examples), "failureReason": None,
            "createdAt": now, "updatedAt": now, "versions": [], "highestVersion": 0,
        }
        self._put_json(self.draft_key(cloud_id), {"examples": list(examples)})
        self._put_json(self.meta_key(cloud_id), meta)
        return meta

    def replace_draft(self, cloud_id: str, examples: list[dict[str, Any]]) -> dict[str, Any]:
        meta = self.require(cloud_id)
        self._put_json(self.draft_key(cloud_id), {"examples": list(examples)})
        meta.update({"draftStatus": "MODIFIED", "exampleCount": len(examples),
                     "status": "ACTIVE", "failureReason": None, "updatedAt": _now()})
        self._put_json(self.meta_key(cloud_id), meta)
        return meta

    def draft_examples(self, cloud_id: str) -> list[dict[str, Any]]:
        blob = self._get_json(self.draft_key(cloud_id)) or {}
        return list(blob.get("examples") or [])

    def version_examples(self, cloud_id: str, version: str) -> list[dict[str, Any]]:
        blob = self._get_json(self.version_key(cloud_id, version))
        if blob is None:
            raise NotFoundError("dataset.version_not_found",
                                f"cloud dataset {cloud_id} has no version {version}")
        return list(blob.get("examples") or [])

    def publish_version(self, cloud_id: str) -> dict[str, Any]:
        """Snapshot the draft as the next immutable numbered version (never overwrites:
        the number is one above the highest ever published, even after deletions)."""
        meta = self.require(cloud_id)
        existing = [int(m.group(1)) for key in self._list_keys(self.prefix(cloud_id))
                    if (m := _VERSION_RE.match(key.rsplit("/", 1)[-1]))]
        known = [int(v["datasetVersion"]) for v in meta.get("versions") or []]
        # the high-water mark survives deletions: a deleted version's number is never reused
        number = max(existing + known + [int(meta.get("highestVersion") or 0)]) + 1
        examples = self.draft_examples(cloud_id)
        now = _now()
        self._put_json(self.version_key(cloud_id, number),
                       {"examples": examples, "createdAt": now, "exampleCount": len(examples)})
        versions = list(meta.get("versions") or [])
        versions.append({"datasetVersion": str(number), "exampleCount": len(examples),
                         "createdAt": now})
        meta.update({"versions": versions, "highestVersion": number, "draftStatus": "UNMODIFIED",
                     "status": "ACTIVE", "failureReason": None, "updatedAt": now})
        self._put_json(self.meta_key(cloud_id), meta)
        return meta

    def delete_version(self, cloud_id: str, version: str) -> dict[str, Any]:
        meta = self.require(cloud_id)
        kept = [v for v in meta.get("versions") or [] if str(v.get("datasetVersion")) != version]
        if len(kept) == len(meta.get("versions") or []):
            raise NotFoundError("dataset.version_not_found",
                                f"cloud dataset {cloud_id} has no version {version}")
        self._s3.delete_object(Bucket=self.bucket, Key=self.version_key(cloud_id, version))
        meta.update({"versions": kept, "updatedAt": _now()})
        self._put_json(self.meta_key(cloud_id), meta)
        return meta

    def delete(self, cloud_id: str) -> None:
        self.require(cloud_id)
        for key in self._list_keys(self.prefix(cloud_id)):
            self._s3.delete_object(Bucket=self.bucket, Key=key)


def store_for(ws: WorkspaceContext) -> SnapshotStore:
    bucket = (ws.resources or {}).get("artifacts_bucket") or ""
    if not bucket:
        raise AppError(
            "dataset.store_unavailable",
            "this workspace has no artifacts bucket yet (run its bootstrap) — dataset "
            "snapshots live there",
            status_code=503,
        )
    return SnapshotStore(s3_client(ws), bucket, ws)


# ── projections (the baseline's shapes) ────────────────────────────────────────
def versions_out(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Published versions, newest first (the baseline's ``ListDatasetVersions`` view)."""
    versions = [
        {"version": str(v.get("datasetVersion")), "example_count": v.get("exampleCount"),
         "created_at": v.get("createdAt")}
        for v in meta.get("versions") or []
    ]
    versions.sort(key=lambda v: (v["created_at"] or "", v["version"]), reverse=True)
    return versions


def cloud_blob(
    store: SnapshotStore, meta: dict[str, Any], *, synced_at: str,
    failure_reason: str | None = None, status: str | None = None,
) -> dict[str, Any]:
    """The row's ``cloud`` member (baseline keys)."""
    return {
        "dataset_id": meta.get("datasetId"),
        "arn": store.arn(str(meta.get("datasetId"))),
        "status": status or meta.get("status"),
        "synced_at": synced_at,
        "failure_reason": failure_reason,
        "draft_status": meta.get("draftStatus"),
        "example_count": meta.get("exampleCount"),
        "versions": versions_out(meta),
    }


def list_entry(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "datasetId": meta.get("datasetId"),
        "name": meta.get("datasetName"),
        "status": meta.get("status"),
        "schemaType": meta.get("schemaType"),
        "exampleCount": meta.get("exampleCount"),
        "draftStatus": meta.get("draftStatus"),
        "updatedAt": str(meta["updatedAt"]) if meta.get("updatedAt") else None,
    }


def strip_example_ids(examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in example.items() if k != "exampleId"} for example in examples]
