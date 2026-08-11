"""Studio local-debug conversation endpoints (multi-turn chat against
un-deployed generated code).

Namespaced apart from the platform Chat page: `/api/chat/*` invokes DEPLOYED
AgentCore runtimes; `/api/conversations/*` here runs local, un-deployed studio
flows. Ported from strands_studio_ui backend/main.py (origin/main).
"""

import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.errors import AppError, NotFoundError
from app.models.conversation import (
    ChatRequest,
    ChatResponse,
    ConversationHistoryResponse,
    ConversationListResponse,
    ConversationSession,
    CreateConversationRequest,
    MessageListResponse,
    UpdateSessionCodeRequest,
)
from app.services import local_exec
from app.services.conversation_service import conversation_service

logger = logging.getLogger("launchpad.conversations")

router = APIRouter(prefix="/api", tags=["studio-local-debug"])


def _require_interpreter() -> None:
    """Same gate as `/api/execute` — this surface runs the same caller-supplied
    code on the same backend, so disabling one entrance and not the other would
    only move the door. Shared logic lives in ``local_exec.preflight_error``."""
    problem = local_exec.preflight_error()
    if problem:
        code, message, status = problem
        raise AppError(code, message, status_code=status)


def _not_found(exc: ValueError) -> NotFoundError:
    return NotFoundError("studio.session.not_found", str(exc))


@router.post("/conversations", response_model=ConversationSession)
async def create_conversation_session(request: CreateConversationRequest) -> ConversationSession:
    _require_interpreter()
    try:
        return await conversation_service.create_session(request)
    except Exception as exc:  # noqa: BLE001
        logger.error("error creating conversation session: %s", exc)
        raise AppError("studio.session.create_failed", str(exc), status_code=500) from exc


@router.get("/conversations", response_model=ConversationListResponse)
async def get_conversation_sessions() -> ConversationListResponse:
    return await conversation_service.get_sessions()


@router.get("/conversations/{session_id}", response_model=ConversationHistoryResponse)
async def get_conversation_history(session_id: str) -> ConversationHistoryResponse:
    try:
        return await conversation_service.get_session_history(session_id)
    except ValueError as exc:
        raise _not_found(exc) from exc


@router.delete("/conversations/{session_id}")
async def delete_conversation_session(session_id: str) -> dict:
    try:
        return await conversation_service.delete_session(session_id)
    except ValueError as exc:
        raise _not_found(exc) from exc


@router.post("/conversations/{session_id}/messages", response_model=ChatResponse)
async def send_chat_message(session_id: str, request: ChatRequest) -> ChatResponse:
    try:
        return await conversation_service.send_message(session_id, request.message)
    except ValueError as exc:
        raise _not_found(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("error sending chat message: %s", exc)
        raise AppError("studio.chat.send_failed", str(exc), status_code=500) from exc


@router.post("/conversations/{session_id}/messages/stream")
async def send_chat_message_stream(session_id: str, request: ChatRequest) -> StreamingResponse:
    async def generate_response():
        try:
            async for chunk in conversation_service.stream_message(session_id, request.message):
                # chunk_to_sse preserves newlines inside multiline chunks (empty
                # `data: ` line = newline); the [CHAT_COMPLETE:id] /
                # [CHAT_ERROR:json] sentinels are single-line and pass through.
                sse_event = local_exec.chunk_to_sse(chunk)
                if sse_event:
                    yield sse_event
        except ValueError as exc:
            yield f"data: [CHAT_ERROR:{json.dumps(str(exc))}]\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.error("error in streaming chat message: %s", exc)
            yield f"data: [CHAT_ERROR:{json.dumps(str(exc))}]\n\n"

    return StreamingResponse(
        generate_response(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no",
        },
    )


@router.put("/conversations/{session_id}/code", response_model=ConversationSession)
async def update_conversation_code(
    session_id: str, request: UpdateSessionCodeRequest
) -> ConversationSession:
    """Rewrite the session's agent code in place (messages kept) — backs the
    apply-fix path so subsequent turns run the fixed code."""
    try:
        return await conversation_service.update_session_code(session_id, request.generated_code)
    except ValueError as exc:
        raise _not_found(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("error updating conversation code: %s", exc)
        raise AppError("studio.chat.code_update_failed", str(exc), status_code=500) from exc


@router.get("/conversations/{session_id}/messages", response_model=MessageListResponse)
async def get_conversation_messages(session_id: str) -> MessageListResponse:
    try:
        return await conversation_service.get_session_messages(session_id)
    except ValueError as exc:
        raise _not_found(exc) from exc
