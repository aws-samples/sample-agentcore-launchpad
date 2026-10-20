"""Read-only CODE view for one agent (``GET /api/agents/{id}/source``).

Nothing is re-rendered: the view shows what AWS holds for the agent, so a template
change after the last deploy cannot make it disagree with what runs.

* ``zip_runtime`` / ``studio`` (the Strands template, the canvas, converted
  ``-rt`` twins): GetAgentRuntime names the zip the Runtime runs. Most of that zip
  is pip-installed dependencies, so ranged reads fetch only its central directory
  and the platform's own files — the entrypoint, ``requirements.txt`` /
  ``requirements.lock``, a conversion's bundle files and the bundled Skills.
* ``container`` (方式A): the build context CodeBuild last built the image from
  (``builds/{name}/source.zip``).
* ``byoc`` ``code_zip`` / ``container_source``: the member's staged upload, the
  exact archive the package stage built from — their files only, not the
  dependencies the platform installed next to them. ``container_image`` has no
  source: the view names the image and its digest.
* ``harness``: a managed agent loop has no code. GetHarness's configuration is
  projected through an allow-list (no execution role, environment, artifact
  location or authorizer — the ``agent_versions`` rule), with header and OAuth
  custom-parameter values and URL query strings masked.

Contents are capped per file and per response; a file left out for the response
budget is fetched on its own with ``?path=``.
"""

import io
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile, ZipInfo

from botocore.exceptions import ClientError

from app.core.errors import AppError, NotFoundError, aws_error_code, is_not_found
from app.models.ledger import Agent
from app.services import byoc_uploads
from app.services.agentcore import harness as harness_api
from app.services.agentcore import runtime as runtime_api
from app.services.workspace import WorkspaceContext

GENERATED_METHODS = {"zip_runtime", "studio"}
CONTAINER_CONTEXT_KEY = "builds/{name}/source.zip"  # deployer/container.py

MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_LISTED_FILES = 2000
# One ranged GET fills this much; a small file and its local header land in one read.
_READ_BUFFER = 256 * 1024
_BINARY_SNIFF = 8192
_BINARY_EXT = {
    "png", "jpg", "jpeg", "gif", "webp", "ico", "bmp", "pdf", "zip", "gz", "tgz", "bz2",
    "xz", "7z", "whl", "egg", "so", "dylib", "dll", "exe", "bin", "pyc", "pyo", "o", "a",
    "class", "jar", "woff", "woff2", "ttf", "otf", "mp3", "mp4", "wav", "parquet", "pkl",
    "db", "sqlite", "npy", "onnx", "pt",
}
_SKIP_PARTS = {"__pycache__", ".git"}
_MASK = "******"

# GetHarness fields that leave the backend. Everything else — executionRoleArn,
# environment / environmentArtifact / environmentVariables, authorizerConfiguration,
# arn — stays behind.
HARNESS_FIELDS = (
    "harnessName",
    "harnessId",
    "harnessVersion",
    "status",
    "createdAt",
    "updatedAt",
    "model",
    "systemPrompt",
    "tools",
    "allowedTools",
    "skills",
    "memory",
    "truncation",
    "hooks",
    "maxIterations",
    "maxTokens",
    "timeoutSeconds",
    "failureReason",
)


def _unavailable(reason: str, message: str, **detail: Any) -> AppError:
    """409 with ``detail.reason`` — the console's copy is keyed on it: ``method`` /
    ``no_resource`` / ``deleted`` / ``gone`` (the AWS resource or the S3 package) /
    ``not_code`` / ``unreadable`` / ``no_bucket``."""
    return AppError(
        "agent.source_unavailable", message, {"reason": reason, **detail}, status_code=409
    )


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _artifacts_bucket(workspace: WorkspaceContext) -> str:
    bucket = workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise _unavailable(
            "no_bucket", "This workspace has no artifacts bucket — run its bootstrap."
        )
    return bucket


# ── ranged zip reads ─────────────────────────────────────────────────────────


class _S3Range(io.RawIOBase):
    """A seekable, read-only view of one S3 object; every read is one ranged GET."""

    def __init__(self, s3: Any, bucket: str, key: str, size: int, version_id: str | None):
        super().__init__()
        self._s3, self._bucket, self._key = s3, bucket, key
        self._size, self._version = size, version_id
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        if base + offset < 0:
            raise ValueError("negative seek position")
        self._pos = base + offset
        return self._pos

    def _get(self, end: int) -> bytes:
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": self._key,
            "Range": f"bytes={self._pos}-{end}",
        }
        if self._version:
            kwargs["VersionId"] = self._version
        data = self._s3.get_object(**kwargs)["Body"].read()
        self._pos += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        if self._pos >= self._size or not len(buffer):
            return 0
        data = self._get(min(self._pos + len(buffer), self._size) - 1)
        buffer[: len(data)] = data
        return len(data)

    def readall(self) -> bytes:
        # one GET for "the rest" (zipfile reads the end record this way)
        return b"" if self._pos >= self._size else self._get(self._size - 1)


def _open_zip(s3: Any, bucket: str, key: str, version_id: str | None = None) -> tuple[ZipFile, Any]:
    """(archive, last-modified) for an S3 zip, read lazily over ranged GETs."""
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    try:
        head = s3.head_object(**kwargs)
    except ClientError as exc:
        if is_not_found(exc) or aws_error_code(exc) == "404":
            raise _unavailable("gone", "The package this view reads is no longer in S3.") from exc
        raise
    raw = _S3Range(s3, bucket, key, int(head["ContentLength"]), version_id)
    try:
        archive = ZipFile(io.BufferedReader(raw, buffer_size=_READ_BUFFER))
    except BadZipFile as exc:
        raise _unavailable(
            "unreadable", "The stored package is not a readable zip archive."
        ) from exc
    return archive, head.get("LastModified")


def _is_binary_name(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return "." in name and name.rsplit(".", 1)[-1] in _BINARY_EXT


def _read_member(archive: ZipFile, info: ZipInfo) -> tuple[str | None, bool, str | None]:
    """(content, truncated, omitted) for one member, capped at MAX_FILE_BYTES."""
    if _is_binary_name(info.filename):
        return None, False, "binary"
    with archive.open(info) as fh:
        data = fh.read(MAX_FILE_BYTES)
    if b"\x00" in data[:_BINARY_SNIFF]:
        return None, False, "binary"
    return data.decode("utf-8", errors="replace"), info.file_size > MAX_FILE_BYTES, None


def _files(
    archive: ZipFile,
    members: list[tuple[str, ZipInfo]],
    only: str | None,
) -> list[dict[str, Any]]:
    """The listing, with contents inline until the response budget runs out.

    ``only`` (the ``?path=`` read) returns that one file, read regardless of the
    budget; an unknown path is a 404."""
    if only is not None:
        members = [(path, info) for path, info in members if path == only]
        if not members:
            raise NotFoundError("agent.source_file_not_found", "file not found in the source")
    budget = MAX_TOTAL_BYTES
    out: list[dict[str, Any]] = []
    for path, info in members:
        entry: dict[str, Any] = {
            "path": path,
            "size": info.file_size,
            "content": None,
            "truncated": False,
            "omitted": None,
        }
        if min(info.file_size, MAX_FILE_BYTES) > budget and only is None:
            entry["omitted"] = "budget"
        else:
            content, truncated, omitted = _read_member(archive, info)
            entry.update(content=content, truncated=truncated, omitted=omitted)
            if content is not None:
                budget -= len(content.encode("utf-8"))
        out.append(entry)
    return out


def _listable(path: str) -> bool:
    parts = path.split("/")
    return not (_SKIP_PARTS & set(parts)) and not path.endswith((".pyc", ".pyo"))


def _ordered(members: list[tuple[str, ZipInfo]], first: list[str]) -> list[tuple[str, ZipInfo]]:
    """``first`` (in that order) ahead of the rest; the rest by path, root files first."""
    rank = {path: i for i, path in enumerate(first)}
    return sorted(
        members,
        key=lambda m: (rank.get(m[0], len(rank)), "/" in m[0], m[0].lower()),
    )


def _code_view(
    archive: ZipFile,
    members: list[tuple[str, ZipInfo]],
    *,
    origin: str,
    first: list[str],
    only: str | None,
    **meta: Any,
) -> dict[str, Any]:
    members = _ordered(members, first)
    listed = members[:MAX_LISTED_FILES]
    return {
        "kind": "code",
        "origin": origin,
        "files": _files(archive, listed, only),
        "files_total": len(members),
        **meta,
    }


# ── per-method sources ───────────────────────────────────────────────────────


def _generated_package(
    control: Any, s3: Any, agent: Agent, only: str | None
) -> dict[str, Any]:
    if not agent.resource_id:
        raise _unavailable(
            "no_resource",
            "The agent has no Runtime yet — its code appears once a deploy has created it.",
            status=agent.status,
        )
    try:
        runtime = runtime_api.get_runtime(control, agent.resource_id)
    except ClientError as exc:
        if is_not_found(exc):
            raise _unavailable("gone", "The agent's Runtime no longer exists on AWS.") from exc
        raise
    code = (runtime.get("agentRuntimeArtifact") or {}).get("codeConfiguration") or {}
    location = (code.get("code") or {}).get("s3") or {}
    if not location.get("bucket") or not location.get("prefix"):
        raise _unavailable("not_code", "The agent's Runtime is not deployed from a code package.")
    entry = (code.get("entryPoint") or ["main.py"])[-1]
    bundle = sorted(k for k in ((agent.spec or {}).get("code_bundle") or {}) if k != entry)
    platform = {entry, "requirements.txt", "requirements.lock", *bundle}

    archive, modified = _open_zip(
        s3, location["bucket"], location["prefix"], location.get("versionId")
    )
    with archive:
        names = [i for i in archive.infolist() if not i.is_dir() and _listable(i.filename)]
        members = [
            (i.filename, i)
            for i in names
            if i.filename in platform or i.filename.startswith("skills/")
        ]
        return _code_view(
            archive,
            members,
            origin="deployed_package",
            first=[entry, *bundle, "requirements.txt", "requirements.lock"],
            only=only,
            entrypoint=entry,
            runtime_version=_iso(runtime.get("agentRuntimeVersion")),
            package_updated_at=_iso(modified),
        )


def _container_context(
    workspace: WorkspaceContext, s3: Any, agent: Agent, only: str | None
) -> dict[str, Any]:
    key = CONTAINER_CONTEXT_KEY.format(name=agent.name)
    archive, modified = _open_zip(s3, _artifacts_bucket(workspace), key)
    with archive:
        members = [
            (i.filename, i)
            for i in archive.infolist()
            if not i.is_dir() and _listable(i.filename)
        ]
        return _code_view(
            archive,
            members,
            origin="build_context",
            first=["main.py", "Dockerfile", "requirements.txt"],
            only=only,
            entrypoint="main.py",
            package_updated_at=_iso(modified),
        )


def _byoc_source(
    workspace: WorkspaceContext, s3: Any, agent: Agent, only: str | None
) -> dict[str, Any]:
    byoc = (agent.spec or {}).get("byoc") or {}
    provenance = byoc.get("provenance") or {}
    kind = byoc.get("artifact_kind")
    if kind == "container_image":
        return {
            "kind": "image",
            "origin": "image",
            "image_uri": byoc.get("image_uri"),
            "image_digest": provenance.get("sha256") or None,
            "files": [],
            "files_total": 0,
        }
    upload_id = byoc.get("upload_id") or ""
    bucket = _artifacts_bucket(workspace)
    try:
        manifest = byoc_uploads.get_manifest(workspace, upload_id, s3_client=s3)
    except NotFoundError as exc:
        raise _unavailable("gone", "The uploaded package is no longer in S3.") from exc
    root = manifest.get("root_prefix") or ""
    archive, _ = _open_zip(s3, bucket, byoc_uploads.source_key(agent.workspace_id, upload_id))
    with archive:
        members = [
            (i.filename[len(root):], i)
            for i in archive.infolist()
            if not i.is_dir() and i.filename.startswith(root)
            and _listable(i.filename[len(root):])
        ]
        entry = byoc.get("entrypoint") or "main.py"
        first = ["Dockerfile", entry] if kind == "container_source" else [entry, "Dockerfile"]
        return _code_view(
            archive,
            members,
            origin="upload",
            first=[*first, "requirements.txt"],
            only=only,
            entrypoint=entry if kind == "code_zip" else None,
            artifact_kind=kind,
            original_filename=provenance.get("original_filename") or None,
            uploaded_by=provenance.get("uploaded_by") or None,
            uploaded_at=provenance.get("uploaded_at") or None,
            sha256=provenance.get("sha256") or None,
        )


# ── harness configuration ────────────────────────────────────────────────────


def _mask_url(url: Any) -> Any:
    if not isinstance(url, str) or "?" not in url:
        return url
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query=_MASK)) if parts.query else url


def _mask_values(mapping: Any) -> Any:
    return {k: _MASK for k in mapping} if isinstance(mapping, dict) else mapping


def _mask_tool(tool: dict[str, Any]) -> dict[str, Any]:
    config = dict(tool.get("config") or {})
    if isinstance(config.get("remoteMcp"), dict):
        mcp = dict(config["remoteMcp"])
        mcp["url"] = _mask_url(mcp.get("url"))
        if "headers" in mcp:
            mcp["headers"] = _mask_values(mcp["headers"])
        config["remoteMcp"] = mcp
    gateway = config.get("agentCoreGateway")
    outbound = gateway.get("outboundAuth") if isinstance(gateway, dict) else None
    oauth = outbound.get("oauth") if isinstance(outbound, dict) else None
    if isinstance(gateway, dict) and isinstance(outbound, dict) and isinstance(oauth, dict):
        if "customParameters" in oauth:
            oauth = {**oauth, "customParameters": _mask_values(oauth["customParameters"])}
            config["agentCoreGateway"] = {**gateway, "outboundAuth": {**outbound, "oauth": oauth}}
    return {**tool, "config": config} if "config" in tool else tool


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return _iso(value) if isinstance(value, datetime) else value


def project_harness(raw: dict[str, Any]) -> dict[str, Any]:
    """GetHarness's ``harness`` object, allow-listed and masked."""
    out = {k: raw[k] for k in HARNESS_FIELDS if k in raw}
    if isinstance(out.get("tools"), list):
        out["tools"] = [_mask_tool(t) if isinstance(t, dict) else t for t in out["tools"]]
    return _json_safe(out)


def _harness_config(control: Any, agent: Agent) -> dict[str, Any]:
    if not agent.resource_id:
        raise _unavailable(
            "no_resource",
            "The agent has no Harness yet — its configuration appears once a deploy "
            "has created it.",
            status=agent.status,
        )
    try:
        raw = harness_api.get_harness(control, agent.resource_id)
    except ClientError as exc:
        if is_not_found(exc):
            raise _unavailable("gone", "The agent's Harness no longer exists on AWS.") from exc
        raise
    return {
        "kind": "harness_config",
        "origin": "harness",
        "harness_version": _text(raw.get("harnessVersion")),
        "config": project_harness(raw),
        "files": [],
        "files_total": 0,
    }


# ── entry point ──────────────────────────────────────────────────────────────


def agent_source(
    control: Any,
    s3: Any,
    workspace: WorkspaceContext,
    agent: Agent,
    *,
    path: str | None = None,
    deployment_status: str | None = None,
) -> dict[str, Any]:
    """The CODE view for ``agent``; 409 ``agent.source_unavailable`` when it has none.

    ``deployment_status`` is the latest deployment's — anything but ``succeeded``
    means the live version may still run the code from before."""
    if agent.status == "deleted":
        raise _unavailable("deleted", "The agent has been deleted.")
    if agent.method == "harness":
        view = _harness_config(control, agent)
    elif agent.method in GENERATED_METHODS:
        view = _generated_package(control, s3, agent, path)
    elif agent.method == "container":
        view = _container_context(workspace, s3, agent, path)
    elif agent.method == "byoc":
        view = _byoc_source(workspace, s3, agent, path)
    else:
        raise _unavailable(
            "method",
            f"The platform holds no code for this agent's method ({agent.method}).",
            method=agent.method,
        )
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "method": agent.method,
        "ledger_version": _text(agent.version),
        "deployment_status": deployment_status,
        **view,
    }
