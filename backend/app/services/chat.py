"""The single chat/invoke chain shared by the Chat playground and the public /v1 API.

Harness, Claude SDK container, and generated Strands zip-runtime agents stream
real deltas, including tool-use events. Other runtime methods keep the buffered
compatibility path.
"""

import json
import time
from collections.abc import Iterator
from contextlib import closing
from typing import Any

from app.assistant.sessions import refuse_assistant_session
from app.core.errors import AppError, envelope
from app.models.ledger import Agent
from app.services.agentcore import harness as hc
from app.services.agentcore.client import data_client
from app.services.agentcore.harness import new_session_id
from app.services.attachments import PreparedAttachments
from app.services.invoke import (
    NATIVE_STREAM_METHODS,
    harness_user_overrides,
    invoke_agent_events,
)
from app.services.payloads import payload_summary
from app.services.runtime_discovery import is_discovered_harness
from app.services.workspace import WorkspaceContext, context_for_workspace


def chat_stream(
    agent: Agent,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "river",
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    workspace: WorkspaceContext | None = None,
    attachments: PreparedAttachments | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield SSE-ready events: meta → (heartbeat|tool|delta)* → done.

    Never raises mid-stream; errors surface as an `error` event. ``workspace``
    defaults to the agent's own — see ``invoke._agent_workspace``.
    """
    session_id = session_id or new_session_id()
    workspace = workspace if workspace is not None else context_for_workspace(
        agent.workspace_id
    )
    # Imported harnesses stream through the same InvokeHarness path as 方式B.
    harness = agent.method == "harness" or is_discovered_harness(agent)
    mode = "stream" if harness or agent.method in NATIVE_STREAM_METHODS else "buffered"
    meta = {"session_id": session_id, "agent": agent.name, "mode": mode}
    if attachments:
        meta["attachments"] = attachments.metadata
    if extra_payload:
        # Summary only — the raw payload never enters the SSE meta or ledger.
        meta["payload"] = payload_summary(extra_payload)
    yield {
        "event": "meta",
        "data": meta,
    }
    started = time.monotonic()
    try:
        refuse_assistant_session(agent, session_id)
        if harness:
            if extra_payload:
                # Entrances refuse this before the stream opens; a direct caller
                # must not have its keys silently dropped either.
                raise AppError(
                    "invoke.payload_unsupported",
                    "The managed Harness accepts messages only; structured payload "
                    "fields cannot be forwarded.",
                    {"reason_code": "harness"}, status_code=422,
                )
            yield from _harness_events(
                agent,
                attachments.prompt(prompt) if attachments else prompt,
                session_id,
                actor_id,
                workspace,
                runtime_user_id=runtime_user_id,
                gateway_access_token=gateway_access_token,
            )
        else:
            invoke_kwargs: dict[str, Any] = {}
            if runtime_user_id:
                invoke_kwargs["runtime_user_id"] = runtime_user_id
            if gateway_access_token:
                invoke_kwargs["gateway_access_token"] = gateway_access_token
            if attachments:
                invoke_kwargs["attachments"] = attachments
            if extra_payload:
                invoke_kwargs["extra_payload"] = extra_payload
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
    except Exception as exc:
        yield {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}}
        return
    yield {
        "event": "done",
        "data": {"latency_ms": int((time.monotonic() - started) * 1000)},
    }


def _harness_events(
    agent: Agent,
    prompt: str,
    session_id: str,
    actor_id: str,
    workspace: WorkspaceContext,
    *,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
) -> Iterator[dict[str, Any]]:
    params: dict[str, Any] = {
        "harnessArn": agent.arn,
        "runtimeSessionId": session_id,
        "actorId": actor_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
    }
    if runtime_user_id:
        params["runtimeUserId"] = runtime_user_id
    if gateway_access_token:
        params.update(harness_user_overrides(agent, workspace, gateway_access_token))
    response = data_client(workspace).invoke_harness(
        **params,
    )
    with closing(hc.iter_harness_stream(response["stream"])) as events:
        for event in events:
            if "contentBlockStart" in event:
                tool_use = event["contentBlockStart"].get("start", {}).get("toolUse")
                if tool_use:
                    yield {
                        "event": "tool",
                        "data": {"name": tool_use.get("name", ""), "id": tool_use.get("toolUseId")},
                    }
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if delta.get("text"):
                    yield {"event": "delta", "data": {"text": delta["text"]}}


def sse_encode(event: dict[str, Any]) -> str:
    if event["event"] == "heartbeat":
        return ": keep-alive\n\n"
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
