"""Studio local-debug AI-fix API routes.

Ported from strands_studio_ui ``backend/app/routers/codegen.py`` (origin/main),
keeping only the AI-fix half:

- POST /api/fix-code/stream       : SSE AI-fix stream for a failed local run
- GET  /api/generate-code/status  : coding-agent backend availability (gates
                                     the frontend Fix button)

The generate-from-prompt endpoints (``/generate-code/stream``,
``/generate-code/cache``) were dropped with the generate pipeline. Namespace is
separate from the platform Chat (``/api/chat/*`` = deployed AgentCore runtimes).
"""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.codegen import config as codegen_config
from app.codegen.service import fix_code_events, get_status
from app.core.billing_policy import SOURCE_CODEGEN
from app.services import budgets
from app.services.usage_meter import UsageMeter
from app.utils.sse_formatter import SSEFormatter

logger = logging.getLogger("launchpad.codegen")

# Single router hosting the AI-fix stream + backend-availability status.
router = APIRouter(prefix="/api", tags=["studio-local-debug"])


class FlowDataModel(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class FixCodeRequest(BaseModel):
    code: str
    error: str
    flow_data: FlowDataModel
    graph_mode: bool = False
    input_data: str | None = None


def _sse_response(event_iterator) -> StreamingResponse:
    """Wrap a service event generator into an SSE StreamingResponse.

    Events are framed as ``event: <type>`` + ``data: <json>``; a terminal
    ``event: end`` always closes the stream. Errors raised mid-stream are
    surfaced as an ``error`` event (never a broken HTTP response)."""

    async def event_stream():
        try:
            async for event in event_iterator:
                yield SSEFormatter.format_json_data(
                    event["data"], event_type=event["event"]
                )
        except Exception as e:  # noqa: BLE001 — reported to the client as an error event
            logger.error(f"Codegen stream error: {e}", exc_info=True)
            yield SSEFormatter.format_json_data({"message": str(e)}, event_type="error")
        finally:
            yield SSEFormatter.format_end_event()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


@router.post("/fix-code/stream")
async def fix_code_stream(request: FixCodeRequest, http_request: Request) -> StreamingResponse:
    """Diagnose and fix a failed local execution via the coding agent (SSE)."""
    logger.info(
        "AI fix request: %d nodes, %d edges, graph_mode=%s, error_len=%d",
        len(request.flow_data.nodes),
        len(request.flow_data.edges),
        request.graph_mode,
        len(request.error),
    )
    # The coding agent's model calls are charged to the caller (the route-level
    # billing gate checked their budget); every agent round reports its usage.
    scope = getattr(http_request.state, "workspace", None)
    meter = UsageMeter.for_payer(
        budgets.payer_of_request(http_request), SOURCE_CODEGEN,
        origin_workspace_id=getattr(scope, "id", None), model_id=codegen_config.get_model(),
    )

    async def metered():
        try:
            async for event in fix_code_events(
                code=request.code,
                error=request.error,
                flow_data=request.flow_data.model_dump(),
                graph_mode=request.graph_mode,
                input_data=request.input_data,
                on_usage=meter.on_usage,
            ):
                yield event
        finally:
            meter.settle()

    return _sse_response(metered())


@router.get("/generate-code/status")
async def codegen_status() -> dict[str, Any]:
    """Report coding-agent backend availability (drives Fix-button enable/disable)."""
    return await get_status()
