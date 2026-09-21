"""Validate files and select verified input adapters before starting an invoke."""

import base64
import binascii
import json
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from app.core.errors import AppError
from app.models.ledger import Agent
from app.schemas.attachments import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MAX_PDF_PAGES,
    MAX_TEXT_CHARS,
    MAX_TOTAL_BYTES,
    AttachmentInput,
)
from app.services.runtime_discovery import is_discovered_harness

IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".xml", ".yaml",
    ".yml", ".toml", ".ini", ".sql", ".sh", ".go", ".rs", ".java", ".c", ".cpp",
})
_SIGNATURES = (b"%PDF-", b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"PK\x03\x04")
_MULTIMODAL_MODELS = re.compile(
    r"^(?:(?:global|us|eu|apac)\.)?"
    r"(?:anthropic\.claude-(?:sonnet|opus|haiku)|amazon\.nova-2-lite|openai\.gpt-5)"
)


def attachment_capability(agent: Agent) -> dict[str, Any]:
    spec = agent.spec or {}
    harness = agent.method == "harness" or is_discovered_harness(agent)
    generated_a2a = (
        agent.method == "zip_runtime" and spec.get("protocol") == "a2a"
        and not spec.get("code") and not spec.get("code_bundle")
    )
    deployed = bool(
        agent.version and getattr(agent, "attachment_version", None) == agent.version
    )
    model = bool(_MULTIMODAL_MODELS.match(str(spec.get("model_id", ""))))
    active = agent.status == "active" and bool(agent.arn)
    native = active and not harness and model and (deployed or generated_a2a)
    reason = None
    if not active:
        reason = "not_active"
    elif harness:
        reason = "harness"
    elif not model:
        reason = "model" if spec.get("model_id") else "custom"
    elif not (deployed or generated_a2a):
        reason = (
            "custom" if agent.method in {"discovered_runtime", "byoc"} or spec.get("code")
            or spec.get("code_bundle") else "republish"
        )
    return {
        "images": native, "text": active,
        "pdf": ("native" if native else "text") if active else "unsupported",
        "reason_code": reason,
        "accept": sorted(TEXT_EXTENSIONS | {".pdf"} | (set(IMAGE_TYPES) if native else set())),
        "max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
    }


@dataclass
class PreparedAttachments:
    """Internal preparation: no raw bytes enter transcript persistence."""

    text: list[str] = field(default_factory=list)
    native: list[dict[str, str]] = field(default_factory=list, repr=False)
    metadata: list[dict[str, Any]] = field(default_factory=list)

    def prompt(self, original: str) -> str:
        parts = [original.strip()] if original.strip() else []
        parts.extend(self.text)
        return "\n\n".join(parts) or "Please analyze the attached files."


def _error(kind: str, message: str, *, name: str = "", status: int = 422) -> AppError:
    return AppError(
        f"chat.attachment_{kind}", message, {"name": name} if name else None,
        status_code=status,
    )


def _decode(item: AttachmentInput) -> bytes:
    if len(item.data) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise _error("too_large", "Attachment exceeds the file size limit.", name=item.name)
    try:
        data = base64.b64decode(item.data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _error("invalid", "Attachment is not valid base64.", name=item.name) from exc
    if not data:
        raise _error("invalid", "Empty attachments are not supported.", name=item.name)
    if len(data) > MAX_FILE_BYTES:
        raise _error("too_large", "Attachment exceeds the file size limit.", name=item.name)
    return data


def _inspect_pdf(data: bytes, *, extract: bool, name: str) -> dict:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.services.attachment_pdf",
             *(["--extract"] if extract else [])],
            input=data, capture_output=True, timeout=8,
            cwd=Path(__file__).resolve().parents[2],
        )
        info = json.loads(result.stdout) if result.returncode == 0 else {}
    except (subprocess.TimeoutExpired, ValueError, OSError):
        info = {}
    kind = info.get("error")
    if kind or not info:
        kind = kind if kind in {
            "pdf_text_unavailable", "pdf_too_long", "text_too_large",
        } else "pdf_unreadable"
        messages = {
            "pdf_text_unavailable":
                "This PDF has pages without readable text. Use an agent with native PDF input.",
            "pdf_too_long": f"PDF attachments must have at most {MAX_PDF_PAGES} pages.",
            "text_too_large": "Extracted attachment text exceeds the input limit.",
            "pdf_unreadable":
                "The PDF is encrypted, invalid, or could not be read within the limits.",
        }
        raise _error(kind, messages[kind], name=name)
    return info


def _validate_image(data: bytes, extension: str, name: str) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                media_type = Image.MIME.get(image.format or "")
                if media_type != IMAGE_TYPES[extension]:
                    raise ValueError("image does not match its extension")
                if (
                    max(image.size) > MAX_IMAGE_DIMENSION
                    or image.width * image.height > MAX_IMAGE_PIXELS
                ):
                    raise ValueError("image dimensions exceed limits")
                image.verify()
            # verify() checks container structure only for some formats (JPEG
            # is a no-op). Decode too so broken pixels fail before invocation.
            with Image.open(BytesIO(data)) as image:
                image.load()
    except (ValueError, OSError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise _error("invalid", "Invalid image format or dimensions.", name=name) from exc


def prepare_attachments(
    agent: Agent, inputs: list[AttachmentInput], *, prompt: str = "",
    session_id: str | None = None,
) -> PreparedAttachments | None:
    if not inputs:
        return None
    if len(inputs) > MAX_FILES:
        raise _error("too_many", f"At most {MAX_FILES} attachments are allowed.")
    capability = attachment_capability(agent)
    prepared = PreparedAttachments()
    total = 0
    for index, item in enumerate(inputs, 1):
        name = item.name
        if (
            name in {".", ".."} or name != name.strip()
            or any(ord(c) < 32 or c in "/\\" for c in name)
        ):
            raise _error("invalid", "Attachment name must be a plain filename.", name=name)
        extension = Path(name).suffix.lower()
        data = _decode(item)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise _error("too_large", "The combined attachments exceed the size limit.")
        delivery = "text"
        media_type = "text/plain"
        text = ""
        if extension in IMAGE_TYPES:
            if not capability["images"]:
                raise _error(
                    "image_unsupported",
                    "This agent cannot accept images. Select a compatible agent or republish it.",
                    name=name, status=409,
                )
            _validate_image(data, extension, name)
            media_type, delivery = IMAGE_TYPES[extension], "native"
        elif extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise _error("pdf_unreadable", "Invalid PDF file.", name=name)
            native = capability["pdf"] == "native"
            info = _inspect_pdf(data, extract=not native, name=name)
            text = info.get("text", "")
            media_type, delivery = "application/pdf", "native" if native else "pdf_text"
        elif extension in TEXT_EXTENSIONS:
            try:
                if data.startswith(_SIGNATURES) or b"\x00" in data:
                    raise ValueError("binary input")
                text = data.decode("utf-8-sig")
                if any(ord(c) < 32 and c not in "\t\n\r\f" for c in text):
                    raise ValueError("binary control characters")
                if not text.strip():
                    raise ValueError("empty text")
            except (UnicodeError, ValueError) as exc:
                raise _error("invalid", "Text attachments must contain UTF-8 text.", name=name) \
                    from exc
        else:
            raise _error("unsupported", "This file format is not supported.", name=name)
        # Browser MIME types are inconsistent for text/code. Binary types must
        # agree when supplied, apart from the generic octet-stream upload type.
        if (
            delivery in {"native", "pdf_text"} and item.media_type
            and item.media_type not in {media_type, "application/octet-stream"}
        ):
            raise _error("invalid", "Attachment MIME type does not match its content.", name=name)
        label = json.dumps(name, ensure_ascii=False)
        if delivery == "native":
            neutral_name = f"attachment-{index}{extension}"
            prepared.native.append({
                "name": neutral_name, "media_type": media_type, "data": item.data,
            })
            prepared.text.append(f"Attached file {neutral_name} has original filename {label}.")
        else:
            note = (
                " (PDF text only; images and layout are excluded)" if delivery == "pdf_text" else ""
            )
            prepared.text.append(
                f"--- Begin attached file {label}{note} ---\n{text}\n--- End attached file ---"
            )
        prepared.metadata.append({
            "name": name, "media_type": media_type, "size": len(data), "delivery": delivery,
        })
        if len(prepared.prompt(prompt)) > MAX_TEXT_CHARS:
            raise _error("text_too_large", "Message and attachment text exceed the input limit.")
    if prepared.native and (agent.spec or {}).get("protocol") != "a2a":
        from app.core.db import SessionLocal
        from app.models.ledger import ChatSession
        from app.optimization import canary_service

        if canary_service.active_canary_route(agent.id):
            raise _error(
                "canary_unsupported",
                "Native attachments are unavailable while this agent has an active canary.",
                status=409,
            )
        if session_id:
            with SessionLocal() as db:
                row = db.query(ChatSession).filter(
                    ChatSession.workspace_id == agent.workspace_id,
                    ChatSession.agent_id == agent.id,
                    ChatSession.session_id == session_id,
                ).first()
                if row and not row.ended_at and row.runtime_version != agent.version:
                    raise _error(
                        "new_session_required",
                        "Start a new session to use this agent's attachment support.", status=409,
                    )
    return prepared
