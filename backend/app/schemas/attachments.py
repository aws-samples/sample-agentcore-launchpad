"""The bounded attachment + payload wire contract shared by every invoke entrance."""

import json
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.errors import AppError

MAX_FILES = 5
MAX_FILE_BYTES = 3 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_PDF_PAGES = 50
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_DIMENSION = 8000
MAX_PAYLOAD_BYTES = 1024 * 1024

# Envelope fields the platform owns on the InvokeAgentRuntime body. A caller
# payload key colliding with one of these is rejected, never merged —
# gateway_access_token in particular would otherwise be a sensitive injection.
RESERVED_PAYLOAD_KEYS = frozenset({
    "prompt", "attachments", "actor_id", "session_id",
    "gateway_access_token", "force_reauth_providers",
})


class AttachmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="", max_length=128)
    # Decoded limits and format checks belong to services.attachments. The body
    # middleware bounds even invalid JSON before Pydantic allocates this string.
    data: str = Field(repr=False)


class AttachmentRequest(BaseModel):
    prompt: str = Field(default="", max_length=MAX_TEXT_CHARS)
    attachments: list[AttachmentInput] = Field(default_factory=list)
    # Structured passthrough: a JSON object merged flat into the
    # InvokeAgentRuntime body next to `prompt`, so a Launchpad-managed runtime
    # keeps the native contract. Must be an object — the envelope it merges
    # into is one — and reserved keys are refused, not overridden.
    payload: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "AttachmentRequest":
        if len(self.attachments) > MAX_FILES:
            raise AppError(
                "chat.attachment_too_many", f"At most {MAX_FILES} attachments are allowed.",
                {"max_files": MAX_FILES}, status_code=422,
            )
        if self.payload is not None:
            reserved = sorted(RESERVED_PAYLOAD_KEYS.intersection(self.payload))
            if reserved:
                raise AppError(
                    "invoke.payload_reserved_key",
                    "Payload keys collide with the platform invoke envelope.",
                    {"keys": reserved}, status_code=422,
                )
            size = len(json.dumps(self.payload, ensure_ascii=False).encode("utf-8"))
            if size > MAX_PAYLOAD_BYTES:
                raise AppError(
                    "invoke.payload_too_large",
                    "Payload exceeds the serialized size limit.",
                    {"max_bytes": MAX_PAYLOAD_BYTES, "size_bytes": size}, status_code=422,
                )
        if not self.prompt.strip() and not self.attachments and self.payload is None:
            raise ValueError("Provide a message, an attachment, or a payload.")
        return self
