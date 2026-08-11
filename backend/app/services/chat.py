"""The single chat/invoke chain shared by the Chat playground and the public /v1 API.

Harness and Claude SDK container agents stream real deltas, including tool-use
events. Other runtime methods keep the buffered compatibility path.
"""

import json
import time
from collections.abc import Iterator
from typing import Any

from app.core.config import get_settings
from app.models.ledger import Agent
from app.services.agentcore import harness as hc
from app.services.agentcore.client import data_client
from app.services.agentcore.harness import new_session_id
from app.services.invoke import invoke_agent_events
from app.services.runtime_discovery import is_discovered_harness


def chat_stream(
    agent: Agent,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "river",
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield SSE-ready events: meta → (heartbeat|tool|delta)* → done.

    Never raises mid-stream; errors surface as an `error` event.
    """
    session_id = session_id or new_session_id()
    # Imported harnesses stream through the same InvokeHarness path as 方式B.
    harness = agent.method == "harness" or is_discovered_harness(agent)
    mode = "stream" if harness or agent.method == "container" else "buffered"
    yield {
        "event": "meta",
        "data": {"session_id": session_id, "agent": agent.name, "mode": mode},
    }
    started = time.monotonic()
    try:
        if harness:
            yield from _harness_events(
                agent,
                prompt,
                session_id,
                actor_id,
                runtime_user_id=runtime_user_id,
                gateway_access_token=gateway_access_token,
            )
        else:
            invoke_kwargs: dict[str, Any] = {}
            if runtime_user_id:
                invoke_kwargs["runtime_user_id"] = runtime_user_id
            if gateway_access_token:
                invoke_kwargs["gateway_access_token"] = gateway_access_token
            yield from invoke_agent_events(
                agent,
                prompt,
                session_id=session_id,
                actor_id=actor_id,
                **invoke_kwargs,
            )
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
        params["tools"] = hc.user_authenticated_tools(
            agent.spec or {},
            get_settings().resources,
            gateway_access_token,
        )
    response = data_client().invoke_harness(
        **params,
    )
    for event in response["stream"]:
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
        elif "runtimeClientError" in event or "internalServerException" in event:
            detail = event.get("runtimeClientError") or event.get("internalServerException")
            raise RuntimeError(str(detail))


def sse_encode(event: dict[str, Any]) -> str:
    if event["event"] == "heartbeat":
        return ": keep-alive\n\n"
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
