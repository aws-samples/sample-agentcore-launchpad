"""The bounded attachment wire contract shared by every invoke entrance."""

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


class AttachmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="", max_length=128)
    # Decoded limits and format checks belong to services.attachments. The body
    # middleware bounds even invalid JSON before Pydantic allocates this string.
    data: str = Field(repr=False)


class AttachmentRequest(BaseModel):
    prompt: str = Field(default="", max_length=MAX_TEXT_CHARS)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> "AttachmentRequest":
        if len(self.attachments) > MAX_FILES:
            raise AppError(
                "chat.attachment_too_many", f"At most {MAX_FILES} attachments are allowed.",
                {"max_files": MAX_FILES}, status_code=422,
            )
        if not self.prompt.strip() and not self.attachments:
            raise ValueError("Provide a message or at least one attachment.")
        return self
