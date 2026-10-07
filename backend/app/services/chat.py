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
from app.services import inbound_auth as inbound_auth_service
from app.services import policy_denials
from app.services.agentcore import harness as hc
from app.services.agentcore.client import data_client
from app.services.agentcore.harness import new_session_id
from app.services.attachments import PreparedAttachments
from app.services.invoke import (
    NATIVE_STREAM_METHODS,
    harness_user_overrides,
    invoke_agent_events,
)
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
    bearer_token: str | None = None,
    console_user: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield SSE-ready events: meta → (heartbeat|tool|delta|policy_denied)* → done.

    ``policy_denied`` (Harness only) marks a Gateway tool call a Cedar policy denied.

    Never raises mid-stream; errors surface as an `error` event. ``workspace``
    defaults to the agent's own — see ``invoke._agent_workspace``.
    ``bearer_token`` is the caller's JWT for a JWT-inbound agent (console Chat
    passes the signed-in user's token; absent, the invoke layer falls back to
    the workspace M2M token or fails with a named error).
    ``console_user=False`` (public /v1) refuses as_user consent asks by name —
    see ``invoke._record_auth_sessions``.
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
    if inbound_auth_service.is_jwt_mode(agent):
        # Which bearer this turn presents to the JWT authorizer, so the
        # console can say who the runtime saw (the "invoke as me" toggle).
        meta["inbound"] = {
            "mode": "jwt",
            "caller": "user_jwt" if bearer_token else "m2m",
        }
    yield {
        "event": "meta",
        "data": meta,
    }
    started = time.monotonic()
    try:
        refuse_assistant_session(agent, session_id)
        if harness:
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
            if bearer_token:
                invoke_kwargs["bearer_token"] = bearer_token
            if not console_user:
                invoke_kwargs["console_user"] = False
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
    # toolUseId → tool name, and the toolResult blocks being streamed (by block
    # index): a Cedar denial only shows up in a result's text (policy_denials)
    tool_names: dict[Any, str] = {}
    results: dict[Any, dict[str, Any]] = {}
    with closing(hc.iter_harness_stream(response["stream"])) as events:
        for event in events:
            if "contentBlockStart" in event:
                block = event["contentBlockStart"]
                start = block.get("start", {})
                tool_use = start.get("toolUse")
                tool_result = start.get("toolResult")
                if tool_use:
                    tool_names[tool_use.get("toolUseId")] = tool_use.get("name", "")
                    yield {
                        "event": "tool",
                        "data": {"name": tool_use.get("name", ""), "id": tool_use.get("toolUseId")},
                    }
                elif isinstance(tool_result, dict):
                    results[block.get("contentBlockIndex")] = {
                        "id": tool_result.get("toolUseId"),
                        "status": tool_result.get("status"),
                        "chunks": [],
                    }
            elif "contentBlockDelta" in event:
                block = event["contentBlockDelta"]
                delta = block.get("delta", {})
                if delta.get("text"):
                    yield {"event": "delta", "data": {"text": delta["text"]}}
                pending = results.get(block.get("contentBlockIndex"))
                if pending is not None and isinstance(delta.get("toolResult"), list):
                    pending["chunks"].extend(
                        str(part["text"]) for part in delta["toolResult"]
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
            elif "contentBlockStop" in event:
                done = results.pop(event["contentBlockStop"].get("contentBlockIndex"), None)
                denied = _policy_denied(done, tool_names, workspace)
                if denied is not None:
                    yield {"event": "policy_denied", "data": denied}


def _policy_denied(
    result: dict[str, Any] | None, tool_names: dict[Any, str], workspace: WorkspaceContext,
) -> dict[str, Any] | None:
    """The ``policy_denied`` payload for a finished toolResult the Gateway denied.

    Only an error result carrying the Gateway's denial signature qualifies. A tool
    reached through the signed-in user's Gateway alias is named as the catalog
    names it (``<target>___<tool>``) and linked to the workspace gateway; any other
    alias is resolved only by the deployed Harness, so it keeps its name unlinked.
    """
    if result is None or result.get("status") != "error":
        return None
    denial = policy_denials.parse_tool_denial("".join(result["chunks"]))
    if denial is None:
        return None
    tool = tool_names.get(result["id"])
    tool = tool if isinstance(tool, str) else ""  # a toolResult with no toolUse seen
    gateway_id = None
    prefix = f"{hc.USER_GATEWAY_ALIAS}_"
    if tool.startswith(prefix):
        tool = tool[len(prefix):]
        gateway_id = workspace.resources.get("gateway_id") or None
    return {"tool": tool, "tool_use_id": result["id"], **denial, "gateway_id": gateway_id}


def sse_encode(event: dict[str, Any]) -> str:
    if event["event"] == "heartbeat":
        return ": keep-alive\n\n"
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
