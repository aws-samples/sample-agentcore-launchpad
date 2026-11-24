"""Session output files: the per-workspace agent-files bucket and the chat Files tab.

An agent with ``filesystem.outputs`` mounts ``projects/<project|shared>/agents/<agent
id>/sessions/{runtimeSessionId}/`` of the workspace's ``launchpad-agent-files-<account>-
<region>`` bucket at ``/mnt/outputs`` (an ``s3BucketMount``, read-write, probes P1/P4).
This module owns

* the bucket's posture (``bucket_policy`` / ``lifecycle_configuration``), built once and
  used by the workspace bootstrap (the hub's CDK stack carries the same shape — the
  twin assertions are in ``tests/test_agent_files.py`` / ``infra/tests/test_stack.py``);
* the console's read side — list a session's files, presign a download — behind the
  prefix the SERVER computes from the agent row, never from the client;
* the best-effort prefix delete an agent delete runs.

The bucket policy is the probe-verified shape (probe P4b, 2026-11-23): a request made
from inside a runtime session (it carries ``aws:AgentCoreRuntimeSourceSid``) reaches only
``.../sessions/<its own session id>/``; a request without the key — the backend — is
unaffected. Objects expire 30 days after creation (the bucket is not versioned, so the
expiry really deletes).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from botocore.config import Config

from app.core.errors import AppError, NotFoundError, cleanup_error
from app.services.agentcore.filesystem_kinds import (
    OUTPUTS_RETENTION_DAYS,
    SOURCE_SID_VAR,
    outputs_agent_prefix,
    outputs_prefix,
)

logger = logging.getLogger(__name__)

URL_TTL_S = 300
MAX_LISTED = 1000
MAX_PATH_CHARS = 1024
# `list_objects_v2` returns 1000 keys a page; a delete batch is capped at the same
DELETE_BATCH = 1000

# the session-id alphabet AgentCore accepts under a `{runtimeSessionId}` prefix — also
# what keeps a stored id from widening the prefix the console reads
# SigV4 on the *regional* virtual-hosted host. The default presign is SigV2 on the global
# endpoint (S3 answers 307 to the regional one). `signature_version="s3v4"` alone still
# signs `<bucket>.s3.amazonaws.com`: fine for an established bucket, but for the first
# hours after the bucket is created (a spoke bootstrap that is used straight away) the
# global name still resolves to us-east-1 and S3 answers 400 "the region 'us-west-2' is
# wrong; expecting 'us-east-1'" (live 2026-11-23, bucket 20 min old; the same bucket
# served 200 a day later). `addressing_style="virtual"` puts the Region in the host, so
# there is no such window.
_SIGV4 = "agent-files-s3v4"
_PRESIGN_CONFIG = Config(signature_version="s3v4", s3={"addressing_style": "virtual"})

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
# an agent id is the ledger's 32-hex id; nothing else may form a delete prefix
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
# a project segment of a prefix: a project id or "shared" — never a slash or a wildcard
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# the object actions a runtime session may use under its own prefix
_OBJECT_ACTIONS = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                   "s3:AbortMultipartUpload"]
_SESSIONS_WILDCARD = f"projects/*/agents/*/sessions/{SOURCE_SID_VAR}"


# ── the bucket's posture ──────────────────────────────────────────────────────


def bucket_policy(bucket: str, partition: str = "aws") -> dict[str, Any]:
    """The agent-files bucket policy: TLS only, and a runtime session is confined to its
    own session folder (probe P4b). ``Null aws:AgentCoreRuntimeSourceSid=false`` selects
    requests that carry the key, i.e. those made by a runtime session's role; the
    backend's requests do not, so they are never denied here."""
    arn = f"arn:{partition}:s3:::{bucket}"
    in_session = {"Null": {"aws:AgentCoreRuntimeSourceSid": "false"}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [arn, f"{arn}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
            {
                "Sid": "DenyOtherSessionsObjects",
                "Effect": "Deny",
                "Principal": "*",
                "Action": list(_OBJECT_ACTIONS),
                "NotResource": f"{arn}/{_SESSIONS_WILDCARD}/*",
                "Condition": dict(in_session),
            },
            {
                "Sid": "DenyListOutsideOwnSession",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:ListBucket",
                "Resource": arn,
                "Condition": {**in_session, "StringNotLike": {"s3:prefix": [
                    f"{_SESSIONS_WILDCARD}/*", _SESSIONS_WILDCARD]}},
            },
        ],
    }


def lifecycle_configuration() -> dict[str, Any]:
    """Expire every object 30 days after creation and drop abandoned multipart uploads
    after a day (the bucket is unversioned, so expiry deletes for good)."""
    return {"Rules": [{
        "ID": "expire-agent-files",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "Expiration": {"Days": OUTPUTS_RETENTION_DAYS},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }]}


# ── the console's read side ───────────────────────────────────────────────────


def outputs_of(agent: Any) -> dict[str, Any] | None:
    """The agent's stored, stamped ``filesystem.outputs`` (``None`` when off or not yet
    stamped with its bucket)."""
    spec = getattr(agent, "spec", None)
    outputs = ((spec or {}).get("filesystem") or {}).get("outputs") if isinstance(
        spec, dict) else None
    if not isinstance(outputs, dict) or not outputs.get("bucket"):
        return None
    return outputs


def session_prefix(agent: Any, session_id: str) -> str | None:
    """``projects/<project>/agents/<agent>/sessions/<session>/`` — the folder the
    agent's runtime writes for this session — or ``None`` when the agent has no output
    folder (or the id cannot be a mounted session id). The one formula the mount, the
    role policy and this reader share (`filesystem_kinds.outputs_prefix`)."""
    outputs = outputs_of(agent)
    if outputs is None or not _SESSION_ID_RE.match(session_id or ""):
        return None
    key = outputs.get("project_key")
    if key is not None and not _PROJECT_KEY_RE.match(str(key)):
        return None
    return outputs_prefix(key, agent.id, session_id)


def clean_relative_path(path: str) -> str:
    """A download path relative to the session folder: non-empty, at most 1024
    characters, no leading ``/``, no ``..`` / ``.`` segment, no backslash or control
    character. ``422 chat.invalid_file_path`` otherwise."""
    bad = (not path or len(path) > MAX_PATH_CHARS or path.startswith("/")
           or "\\" in path or any(ord(c) < 32 for c in path)
           or any(seg in ("", ".", "..") for seg in path.split("/")))
    if bad:
        raise AppError("chat.invalid_file_path",
                       "the file path must be relative to the session folder",
                       status_code=422)
    return path


def list_files(agent: Any, session_id: str, workspace: Any, s3: Any = None) -> dict[str, Any]:
    """The files of one session, newest first (at most 1000). ``enabled: false`` for an
    agent without an output folder."""
    outputs, prefix = outputs_of(agent), session_prefix(agent, session_id)
    if outputs is None or prefix is None:
        return {"enabled": False, "files": [], "truncated": False,
                "retention_days": OUTPUTS_RETENTION_DAYS}
    client = s3 or workspace.client("s3")
    found: list[dict[str, Any]] = []
    truncated = False
    kwargs: dict[str, Any] = {"Bucket": outputs["bucket"], "Prefix": prefix}
    while True:
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            key = str(obj.get("Key") or "")
            rel = key[len(prefix):] if key.startswith(prefix) else ""
            if not rel or rel.endswith("/"):  # a folder placeholder is not a file
                continue
            if len(found) >= MAX_LISTED:
                truncated = True
                break
            modified = obj.get("LastModified")
            found.append({"key_rel": rel, "size": int(obj.get("Size") or 0),
                          "last_modified": modified.isoformat() if modified else None})
        token = page.get("NextContinuationToken")
        if truncated or not page.get("IsTruncated") or not token:
            break
        kwargs["ContinuationToken"] = token
    found.sort(key=lambda f: f["last_modified"] or "", reverse=True)
    return {"enabled": True, "files": found, "truncated": truncated,
            "retention_days": OUTPUTS_RETENTION_DAYS,
            "mount_path": outputs.get("mount_path")}


def presign(agent: Any, session_id: str, rel_path: str, workspace: Any,
            s3: Any = None) -> dict[str, Any]:
    """A five-minute presigned GET for one file of the session folder, served as an
    attachment. The key is the server's prefix plus the validated relative path."""
    outputs, prefix = outputs_of(agent), session_prefix(agent, session_id)
    if outputs is None or prefix is None:
        raise NotFoundError("chat.files_unavailable",
                            "this agent has no session output folder")
    rel = clean_relative_path(rel_path)
    name = rel.rsplit("/", 1)[-1]
    client = s3 or workspace.client("s3", cache_token=_SIGV4, config=_PRESIGN_CONFIG)
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": outputs["bucket"], "Key": f"{prefix}{rel}",
                "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(name)}"},
        ExpiresIn=URL_TTL_S,
    )
    return {"url": url, "name": name, "expires_in": URL_TTL_S}


# ── delete ────────────────────────────────────────────────────────────────────


def delete_prefix(s3: Any, bucket: str, prefix: str,
                  log: Any = lambda _m: None) -> int:
    """Delete every object under ``prefix`` (paginated list, batches of 1000); returns
    the count deleted. Never raises: a failure is logged and the lifecycle rule is the
    backstop."""
    deleted = 0
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    try:
        while True:
            page = s3.list_objects_v2(**kwargs)
            keys = [{"Key": o["Key"]} for o in page.get("Contents") or [] if o.get("Key")]
            for start in range(0, len(keys), DELETE_BATCH):
                batch = keys[start:start + DELETE_BATCH]
                result = s3.delete_objects(Bucket=bucket,
                                           Delete={"Objects": batch, "Quiet": True})
                deleted += len(batch) - len(result.get("Errors") or [])
                for err in result.get("Errors") or []:
                    log(f"could not delete s3://{bucket}/{err.get('Key')}: "
                        f"{err.get('Code')}")
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                break
            kwargs["ContinuationToken"] = token
    except Exception as exc:  # noqa: BLE001 — cleanup must never block a delete
        log(f"could not clear s3://{bucket}/{prefix}: {cleanup_error(exc)}")
    return deleted


def delete_agent_files(agent: Any, workspace: Any, s3_client: Any = None) -> int:
    """Remove the agent's output folders (``projects/<project|shared>/agents/<id>/``)
    from the workspace's agent-files bucket; best effort, never raises. The prefix names
    the agent's own id (and the project it was filed under — today's, and the one its
    stored spec was stamped with), so it can never reach another agent's objects."""
    try:
        bucket = (workspace.resources or {}).get("agent_files_bucket")
        agent_id = str(getattr(agent, "id", "") or "")
        if not bucket or not _AGENT_ID_RE.match(agent_id):
            return 0
        keys = {getattr(agent, "project_id", None)}
        outputs = ((getattr(agent, "spec", None) or {}).get("filesystem") or {}).get("outputs")
        if isinstance(outputs, dict):
            keys.add(outputs.get("project_key"))
        # a key that cannot be a project segment (a slash, a wildcard) forms no prefix
        prefixes = sorted({outputs_agent_prefix(k, agent_id) for k in keys
                           if k is None or _PROJECT_KEY_RE.match(str(k))})
        s3 = s3_client or workspace.client("s3")
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent %s: session output cleanup skipped: %s",
                       getattr(agent, "id", "?"), cleanup_error(exc))
        return 0
    total = 0
    for prefix in prefixes:
        total += delete_prefix(
            s3, bucket, prefix, lambda msg: logger.warning("agent %s: %s", agent.id, msg))
    if total:
        logger.info("agent %s: deleted %d session output file(s)", agent.id, total)
    return total
