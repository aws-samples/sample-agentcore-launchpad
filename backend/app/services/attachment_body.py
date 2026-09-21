"""Streaming request-size enforcement for the shared attachment entrances."""

import re

from starlette.responses import JSONResponse

from app.core.errors import envelope
from app.schemas.attachments import MAX_REQUEST_BYTES

_PATH = re.compile(r"^/(?:api/chat/[^/]+|(?:api|v1)/agents/[^/]+/invoke(?:-stream)?)/?$")


class AttachmentBodyCap:
    def __init__(self, app, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http" or scope.get("method") != "POST"
            or not _PATH.fullmatch(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return
        # Even a malformed root value can contain file bytes. Tell the error
        # handler to redact body inputs before a request model exists.
        scope["launchpad.attachment_request"] = True
        received, tripped, started = 0, False, False

        async def capped_receive():
            nonlocal received, tripped
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    tripped = True
                    return {"type": "http.disconnect"}
            return message

        async def capped_send(message):
            nonlocal started
            if not tripped:
                started = True
                await send(message)

        try:
            await self.app(scope, capped_receive, capped_send)
        except Exception:
            if not tripped:
                raise
        if tripped and not started:
            response = JSONResponse(
                envelope(
                    "chat.attachment_too_large", "Attachment request exceeds the size limit.",
                    {"max_bytes": self.max_bytes},
                ),
                status_code=413,
            )
            await response(scope, receive, send)
