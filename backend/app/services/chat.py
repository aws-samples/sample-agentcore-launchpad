"""The single chat/invoke chain shared by the Chat playground and the public /v1 API.

Claude SDK container and generated Strands zip-runtime agents stream real deltas,
including tool-use events. Other runtime methods keep the buffered compatibility
path.
"""

import json
import time
from collections.abc import Iterator
from typing import Any

from app.assistant.sessions import refuse_assistant_session
from app.core.display_time import stream_event_payload
from app.core.errors import AppError, envelope
from app.core.features import feature_unavailable
from app.models.ledger import Agent
from app.services.agentcore.runtime import (
    SdkStreamError,
    bounded_error_text,
    new_session_id,
    redact_secret_pairs,
)
from app.services.attachments import PreparedAttachments
from app.services.invoke import NATIVE_STREAM_METHODS, invoke_agent_events
from app.services.policy_entities import Principal
from app.services.runtime_discovery import is_discovered_harness
from app.services.workspace import WorkspaceContext, context_for_workspace


def chat_stream(
    agent: Agent,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "river",
    runtime_user_id: str | None = None,
    workspace: WorkspaceContext | None = None,
    attachments: PreparedAttachments | None = None,
    console_user: bool = True,
    principal: Principal | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield SSE-ready events: meta → (heartbeat|tool|delta)* → done.

    Never raises mid-stream; errors surface as an `error` event. ``workspace``
    defaults to the agent's own — see ``invoke._agent_workspace``.
    ``console_user=False`` (public /v1) refuses as_user consent asks by name —
    see ``invoke._record_auth_sessions``. ``principal`` (D20) is forwarded to the
    invoke gate; a policy refusal arrives as the first ``error`` event after ``meta``
    (code ``policy.denied`` / ``policy.undecidable`` with the envelope's ``detail``).
    """
    session_id = session_id or new_session_id()
    workspace = workspace if workspace is not None else context_for_workspace(
        agent.workspace_id
    )
    # A2A-protocol runtimes answer one JSON-RPC Task — buffered, whatever the method
    # (invoke.invoke_agent_events makes the same choice).
    mode = (
        "stream"
        if agent.method in NATIVE_STREAM_METHODS
        and (agent.spec or {}).get("protocol", "http") != "a2a"
        else "buffered"
    )
    meta = {"session_id": session_id, "agent": agent.name, "mode": mode}
    if attachments:
        meta["attachments"] = attachments.metadata
    yield {
        "event": "meta",
        "data": meta,
    }
    started = time.monotonic()
    try:
        refuse_assistant_session(agent, session_id)
        if is_discovered_harness(agent):
            raise feature_unavailable("harness")
        invoke_kwargs: dict[str, Any] = {}
        if runtime_user_id:
            invoke_kwargs["runtime_user_id"] = runtime_user_id
        if attachments:
            invoke_kwargs["attachments"] = attachments
        if not console_user:
            invoke_kwargs["console_user"] = False
        if principal is not None:
            invoke_kwargs["principal"] = principal
        yield from invoke_agent_events(
            agent,
            prompt,
            session_id=session_id,
            actor_id=actor_id,
            workspace=workspace,
            **invoke_kwargs,
        )
    except AppError as exc:
        yield {"event": "error", "data": envelope(exc.code, exc.message, exc.detail)}
        return
    except SdkStreamError as exc:
        # N17-obs98 (assistant-turns.md §4 / §8.2 parity): the Runtime stream ended on the
        # AgentCore SDK's terminal error frame (or a malformed frame / a REJECTED body) — the
        # member and the turn event log get the real cause, structured: `error_type` is the
        # runtime-side exception class, as the architect turn carries it
        # (rev-r17 on cn/r17-o: same redaction as the sync twin in invoke.py — one frame, one
        # policy; the text is the agent's own error, bounded and credential-pairs redacted)
        payload: dict[str, Any] = {
            "message": f"{type(exc).__name__}: "
                       f"{redact_secret_pairs(bounded_error_text(str(exc)))}",
        }
        if exc.detail:
            payload["detail"] = {
                key: redact_secret_pairs(value) if isinstance(value, str) else value
                for key, value in exc.detail.items()
            }
        yield {"event": "error", "data": payload}
        return
    except Exception as exc:
        yield {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}}
        return
    yield {
        "event": "done",
        "data": {"latency_ms": int((time.monotonic() - started) * 1000)},
    }


def sse_encode(event: dict[str, Any]) -> str:
    """One SSE frame. An event carrying ``seq`` (an assistant turn's event-log position,
    N-ARCH-obs15) is framed with the standard ``id:`` field so a client can resume from
    it; heartbeats are comment frames."""
    if event["event"] == "heartbeat":
        return ": keep-alive\n\n"
    head = f"id: {event['seq']}\n" if event.get("seq") is not None else ""
    data = stream_event_payload(event["event"], event["data"])  # N17-obs463: the envelope only
    return (f"{head}event: {event['event']}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n")
