"""Images an agent answers with: validate, store in the workspace bucket, describe.

The runtime contract (docs/architecture.md, "Agent reply attachments"): an SSE
``{"event": "attachment", "name", "media_type", "data"}`` frame, or a JSON reply's
top-level ``attachments`` list. ``rt._runtime_payload_events`` turns both into raw
``attachment`` events; this module is the only place that sees the bytes after
that. An accepted image goes to the workspace's ``artifacts_bucket`` under the
platform's own workspace credentials and one ``ChatOutputAttachment`` row; every
caller downstream receives metadata only. A refused image is a metadata item with
``status: "dropped"`` — it never fails the turn.
"""

import base64
import binascii
import hashlib
import logging
import re
import uuid
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi.responses import StreamingResponse

from app.core.db import SessionLocal
from app.core.errors import aws_error_code
from app.models.ledger import Agent, ChatOutputAttachment
from app.schemas.attachments import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from app.services.attachments import image_is_valid
from app.services.workspace import WorkspaceContext, context_for_workspace

logger = logging.getLogger("launchpad.outbound_attachments")

PREFIX = "chat-attachments"
CONSOLE_OWNER = "console"
EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
}
_MAX_ENCODED = 4 * ((MAX_FILE_BYTES + 2) // 3)
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9._()+ -]")


def _s3(workspace: WorkspaceContext) -> Any:
    return workspace.client("s3")


def _bucket(workspace: WorkspaceContext) -> str:
    return str((workspace.resources or {}).get("artifacts_bucket") or "")


def api_key_owner(key_id: str) -> str:
    return f"apikey:{key_id}"


def download_path(owner: str, agent_id: str, attachment_id: str) -> str:
    """Where the entrance that produced the turn reads the image back."""
    if owner == CONSOLE_OWNER:
        return f"/api/chat/{agent_id}/attachments/{attachment_id}"
    return f"/v1/agents/{agent_id}/attachments/{attachment_id}"


def storage_key(workspace_id: str, agent_id: str, session_id: str, attachment_id: str,
                media_type: str) -> str:
    session = _KEY_UNSAFE.sub("_", session_id)[:256] or "_"
    return (f"{PREFIX}/{workspace_id}/{agent_id}/{session}/"
            f"{attachment_id}{EXTENSIONS[media_type]}")


def _display_name(name: str, index: int, media_type: str) -> str:
    """A plain filename for the bubble: the agent's when it is one, else neutral."""
    base = PurePosixPath(name.replace("\\", "/")).name.strip()
    if not base or base in {".", ".."} or any(ord(c) < 32 or c == "\x7f" for c in base):
        return f"image-{index}{EXTENSIONS.get(media_type, '')}"
    return base[:255]


def _dropped(name: str, reason_code: str) -> dict[str, Any]:
    return {"name": name, "status": "dropped", "reason_code": reason_code}


class ReplyAttachments:
    """One turn's reply attachments: limits are per turn, so one instance per turn.

    ``owner`` None (an entrance that does not relay attachments: evaluation, the
    console's sync invoke) accepts nothing and writes nothing."""

    def __init__(
        self, agent: Agent, workspace: WorkspaceContext | None, session_id: str | None,
        owner: str | None,
    ):
        self.agent = agent
        self.workspace = workspace
        self.session_id = session_id
        self.owner = owner
        self.count = 0
        self.total = 0

    def accept(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Metadata for one raw ``attachment`` event (stored or dropped), or None
        when this entrance relays no attachments."""
        if self.owner is None or not self.session_id:
            logger.info("reply attachment ignored: entrance relays none (agent %s)",
                        self.agent.id)
            return None
        self.count += 1
        media_type = str(raw.get("media_type") or "").lower()
        name = _display_name(str(raw.get("name") or ""), self.count, media_type)
        reason, data = self._check(raw, media_type)
        if reason is None:
            self.total += len(data)
            if self.total > MAX_TOTAL_BYTES:
                reason = "too_large"
        if reason is None:
            try:
                return self._store(name, media_type, data)
            except (ClientError, BotoCoreError, RuntimeError) as exc:
                logger.warning("reply attachment %r of agent %s not stored: %s",
                               name, self.agent.id, exc)
                reason = "storage_unavailable"
        logger.warning("reply attachment %r of agent %s dropped: %s",
                       name, self.agent.id, reason)
        return _dropped(name, reason)

    def _check(self, raw: dict[str, Any], media_type: str) -> tuple[str | None, bytes]:
        if self.count > MAX_FILES:
            return "too_many", b""
        if raw.get("oversized"):
            return "too_large", b""
        if media_type not in EXTENSIONS:
            return "unsupported_type", b""
        encoded = raw.get("data") or ""
        if len(encoded) > _MAX_ENCODED:
            return "too_large", b""
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return "invalid", b""
        if not data:
            return "invalid", b""
        if len(data) > MAX_FILE_BYTES:
            return "too_large", b""
        if not image_is_valid(data, media_type):
            return "invalid", b""
        return None, data

    def _store(self, name: str, media_type: str, data: bytes) -> dict[str, Any]:
        workspace = self.workspace or context_for_workspace(self.agent.workspace_id)
        bucket = _bucket(workspace)
        if not bucket:
            raise RuntimeError("artifacts_bucket missing from this workspace's resource map")
        attachment_id = uuid.uuid4().hex
        digest = hashlib.sha256(data).hexdigest()
        key = storage_key(str(self.agent.workspace_id), self.agent.id,
                          str(self.session_id), attachment_id, media_type)
        _s3(workspace).put_object(
            Bucket=bucket, Key=key, Body=data, ContentType=media_type,
            ServerSideEncryption="AES256", Metadata={"sha256": digest},
        )
        db = SessionLocal()
        try:
            db.add(ChatOutputAttachment(
                id=attachment_id, workspace_id=self.agent.workspace_id,
                agent_id=self.agent.id, session_id=str(self.session_id), owner=str(self.owner),
                name=name, media_type=media_type, size_bytes=len(data), sha256=digest,
                storage_key=key,
            ))
            db.commit()
        finally:
            db.close()
        return {
            "id": attachment_id, "name": name, "media_type": media_type,
            "size_bytes": len(data), "sha256": digest, "status": "stored",
            "url": download_path(str(self.owner), self.agent.id, attachment_id),
        }


def relay(
    events: Iterator[dict[str, Any]], attachments: ReplyAttachments,
) -> Iterator[dict[str, Any]]:
    """Replace each raw ``attachment`` event with its metadata (or drop it)."""
    for event in events:
        if event.get("event") != "attachment":
            yield event
            continue
        meta = attachments.accept(event.get("data") or {})
        if meta is not None:
            yield {"event": "attachment", "data": meta}


def find(db: Any, workspace_id: str, agent_id: str, attachment_id: str,
         owner: str) -> ChatOutputAttachment | None:
    return (
        db.query(ChatOutputAttachment)
        .filter(
            ChatOutputAttachment.id == attachment_id,
            ChatOutputAttachment.workspace_id == workspace_id,
            ChatOutputAttachment.agent_id == agent_id,
            ChatOutputAttachment.owner == owner,
        )
        .first()
    )


def open_object(workspace: WorkspaceContext, row: ChatOutputAttachment) -> Any | None:
    """The stored object's streaming body, or None once it is gone (expired by the
    bucket lifecycle rule, or deleted)."""
    bucket = _bucket(workspace)
    if not bucket:
        return None
    try:
        return _s3(workspace).get_object(Bucket=bucket, Key=row.storage_key)["Body"]
    except ClientError as exc:
        if aws_error_code(exc) in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise


def iter_body(body: Any, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    try:
        while chunk := body.read(chunk_size):
            yield chunk
    finally:
        if hasattr(body, "close"):
            body.close()


def _inline_disposition(name: str) -> str:
    """``inline`` with an ASCII ``filename`` and the UTF-8 ``filename*`` (RFC 6266 /
    5987), so "open in a new tab" and "save as" keep the agent's file name."""
    fallback = _ASCII_UNSAFE.sub("_", name) or "image"
    return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def image_response(row: ChatOutputAttachment, body: Any) -> StreamingResponse:
    """The stored image, as one response both read entrances share: the ledger's
    validated media type, never sniffed, never scriptable, private to the browser."""
    return StreamingResponse(
        iter_body(body),
        media_type=row.media_type,
        headers={
            "Content-Length": str(row.size_bytes),
            "Content-Disposition": _inline_disposition(row.name),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "Cache-Control": "private, max-age=300",
        },
    )
