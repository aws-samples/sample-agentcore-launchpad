"""Method-aware invoke + undeploy dispatch.

Chat playground (phase 8) and the public API share this single entry point,
so both consumption paths behave identically.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

from botocore.exceptions import ClientError

from app.core.errors import AppError, aws_error_code
from app.models.ledger import Agent
from app.optimization import canary_service
from app.services.agentcore import gateway
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import data_client
from app.services.runtime_discovery import (
    DISCOVERED_METHOD,
    is_discovered_harness,
    require_invoke_capability,
)
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.templates import gateway_support

logger = logging.getLogger(__name__)
BUFFERED_CHUNK_CHARS = 60
# Runtime methods whose generated entrypoint emits the delta/tool/complete
# envelope over SSE (see `rt._runtime_payload_events`). Shared with chat so the
# advertised `mode` and the actual invoke path can't drift apart.
NATIVE_STREAM_METHODS = frozenset({"container", "zip_runtime"})
# Methods whose agent ARN is an AgentCore *Runtime* — the only resource with a
# session-stop operation. A managed Harness (deployed or imported) has none:
# neither `bedrock-agentcore` nor `bedrock-agentcore-control` models an
# operation that names both Harness and Session.
RUNTIME_SESSION_METHODS = frozenset({"zip_runtime", "studio", "container", DISCOVERED_METHOD})


def _runtime_user_id(
    agent: Agent,
    actor_id: str,
    runtime_user_id: str | None = None,
) -> str | None:
    """See ``gateway_support.runtime_user_id`` — omitted unless the spec needs it."""
    return gateway_support.runtime_user_id(agent.spec, runtime_user_id or actor_id)


def _parse_gateway_text(raw_text: str, session_id: str) -> dict[str, Any]:
    """Parse a gateway HTTP response body the SAME way ``rt.invoke_runtime_text``
    parses ``invoke_agent_runtime``: JSON ``{"result": ...}`` or an SSE stream via
    ``rt.flatten_sse_text``."""
    try:
        body = json.loads(raw_text)
    except (ValueError, TypeError):
        body = {"result": rt.flatten_sse_text(raw_text) or raw_text}
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"runtime returned error: {body['error']}")
    text = body.get("result", "") if isinstance(body, dict) else str(body)
    return {"text": str(text), "session_id": session_id}


def _agent_workspace(
    agent: Agent, workspace: WorkspaceContext | None
) -> WorkspaceContext:
    """Where this agent's runtime lives.

    The agent row is authoritative, not the caller: both entrances (console chat
    and `/v1`) must reach the same account/region for the same agent. Callers that
    already resolved the workspace pass it, which also keeps the hot path free of
    an extra ledger read per turn.
    """
    return workspace if workspace is not None else context_for_workspace(agent.workspace_id)


def _invoke_via_stable_endpoint(
    route: dict[str, Any],
    prompt: str,
    session_id: str | None,
    actor_id: str,
    workspace: WorkspaceContext,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
) -> dict[str, Any]:
    """Direct-invoke the runtime pinned to the stable endpoint (= v_current).

    Used both as the fail-safe for a gateway error and as the primary path while a
    canary is still provisioning (stable endpoint stood up, gateway A/B not live
    yet) — either way production serves the tested control version, never DEFAULT
    (which the candidate mint already rolled to the untested candidate)."""
    kwargs: dict[str, Any] = {
        "session_id": session_id,
        "actor_id": actor_id,
        "qualifier": route["stable_endpoint"],
        "runtime_user_id": runtime_user_id,
    }
    if gateway_access_token:
        kwargs["gateway_access_token"] = gateway_access_token
    return rt.invoke_runtime_text(data_client(workspace), route["arn"], prompt, **kwargs)


def _invoke_via_canary(
    route: dict[str, Any],
    prompt: str,
    session_id: str | None,
    actor_id: str,
    workspace: WorkspaceContext,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
) -> dict[str, Any]:
    """Route a real invocation for an active canary.

    Two forms (see ``canary_service.active_canary_route``):

    - **live gateway** (``gateway_url`` + ``control_target`` present) — POST
      through the canary gateway, which assigns a variant by sticky session id and
      splits by weight. A gateway error is control-safe: fall back to the stable
      endpoint (v_current), NOT DEFAULT (the untested candidate).
    - **provisioning** (stable endpoint only, no live gateway yet) — direct-invoke
      the stable endpoint so production stays on v_current during setup.
    """
    if not (route.get("gateway_url") and route.get("control_target")):
        return _invoke_via_stable_endpoint(
            route,
            prompt,
            session_id,
            actor_id,
            workspace,
            runtime_user_id,
            gateway_access_token,
        )
    # Runtime session ids must be ≥33 chars (spike); mint one when absent/short.
    sticky = session_id if (session_id and len(session_id) >= 33) else rt.new_session_id()
    url = f"{route['gateway_url'].rstrip('/')}/{route['control_target']}/invocations"
    try:
        # Same body shape service.send_gateway_traffic posts to the gateway.
        body = {"prompt": prompt, "sessionId": sticky}
        if gateway_access_token:
            body["actor_id"] = actor_id
            body["gateway_access_token"] = gateway_access_token
        response = gateway.sigv4_post(url, body, workspace, session_id=sticky)
        if response.status_code != 200:
            raise RuntimeError(f"gateway route returned HTTP {response.status_code}")
        return _parse_gateway_text(response.text, sticky)
    except Exception as exc:
        logger.warning(
            "canary gateway route failed (%s); falling back to stable endpoint %s",
            exc,
            route.get("stable_endpoint"),
        )
        return _invoke_via_stable_endpoint(
            route,
            prompt,
            session_id,
            actor_id,
            workspace,
            runtime_user_id,
            gateway_access_token,
        )


def invoke_agent_text(
    agent: Agent,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    workspace: WorkspaceContext | None = None,
) -> dict[str, Any]:
    require_invoke_capability(agent)
    workspace = _agent_workspace(agent, workspace)
    # An imported harness carries the harness ARN, so it invokes exactly like a
    # launchpad-deployed one — InvokeHarness, never InvokeAgentRuntime.
    if agent.method == "harness" or is_discovered_harness(agent):
        return hc.invoke_harness_text(
            data_client(workspace),
            agent.arn,
            prompt,
            session_id=session_id,
            actor_id=actor_id,
        )
    if agent.method in ("zip_runtime", "studio", "container", DISCOVERED_METHOD):
        # A2A-protocol runtimes speak JSON-RPC; the A2A server owns
        # conversation state (no actor_id/memory envelope) and can't be canaried
        if (agent.spec or {}).get("protocol") == "a2a":
            return rt.invoke_a2a_text(
                data_client(workspace), agent.arn, prompt, session_id=session_id
            )
        # During an active canary, real production traffic for this agent flows
        # through the canary's gateway; otherwise the path below is unchanged.
        route = canary_service.active_canary_route(agent.id)
        if route is not None:
            return _invoke_via_canary(
                route,
                prompt,
                session_id,
                actor_id,
                workspace,
                runtime_user_id,
                gateway_access_token,
            )
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "actor_id": actor_id,
            "runtime_user_id": _runtime_user_id(agent, actor_id, runtime_user_id),
        }
        if gateway_access_token:
            kwargs["gateway_access_token"] = gateway_access_token
        return rt.invoke_runtime_text(
            data_client(workspace), agent.arn, prompt, **kwargs
        )
    raise AppError(
        "agent.method_not_available",
        f"no invoke path for method '{agent.method}'",
        status_code=400,
    )


def stop_agent_session(
    agent: Agent,
    session_id: str,
    workspace: WorkspaceContext | None = None,
) -> dict[str, Any]:
    """End one live AgentCore Runtime session behind a conversation.

    Same method dispatch and data-plane client as the invoke path, so Chat ends the
    very session it talked to. AWS answering `ResourceNotFoundException` means the
    session already ended (explicitly, or by idle expiry) — reported as success
    with ``already_ended`` rather than as an error, because the caller's goal is
    reached either way. Any other `ClientError` propagates to the shared envelope
    mapping (a `RetryableConflictException` that outlived botocore's retries → 409).
    """
    if agent.method == "harness" or is_discovered_harness(agent):
        raise AppError(
            "chat.session_stop_unsupported",
            "the managed Harness service has no session-stop operation; "
            "start a new session instead",
            {"reason_code": "harness", "method": agent.method},
            status_code=409,
        )
    if agent.method not in RUNTIME_SESSION_METHODS or not agent.arn:
        raise AppError(
            "chat.session_stop_unsupported",
            f"no AgentCore Runtime session to stop for method '{agent.method}'",
            {"reason_code": "no-runtime", "method": agent.method},
            status_code=409,
        )
    workspace = _agent_workspace(agent, workspace)
    try:
        rt.stop_runtime_session(
            data_client(workspace), runtime_arn=agent.arn, session_id=session_id
        )
    except ClientError as exc:
        if aws_error_code(exc) != "ResourceNotFoundException":
            raise
        return {"ended": True, "already_ended": True}
    return {"ended": True, "already_ended": False}


def invoke_agent_events(
    agent: Agent,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    workspace: WorkspaceContext | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield native runtime events, with a buffered compatibility fallback.

    Claude SDK containers and generated Strands zip runtimes both emit the
    delta/tool/complete envelope over the Runtime SSE response, so they are
    parsed incrementally. A zip runtime deployed before its template streamed
    still answers a JSON ``{"result": ...}`` body, which the same parser turns
    into one delta — so the switch is safe for existing agents.
    """
    require_invoke_capability(agent)
    workspace = _agent_workspace(agent, workspace)
    streams_natively = (
        agent.method in NATIVE_STREAM_METHODS
        and (agent.spec or {}).get("protocol", "http") != "a2a"
    )
    if streams_natively and canary_service.active_canary_route(agent.id) is None:
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "actor_id": actor_id,
            "runtime_user_id": _runtime_user_id(agent, actor_id, runtime_user_id),
        }
        if gateway_access_token:
            kwargs["gateway_access_token"] = gateway_access_token
        yield from rt.stream_runtime_events(
            data_client(workspace), agent.arn, prompt, **kwargs
        )
        return

    result = invoke_agent_text(
        agent,
        prompt,
        session_id=session_id,
        actor_id=actor_id,
        runtime_user_id=runtime_user_id,
        gateway_access_token=gateway_access_token,
        workspace=workspace,
    )
    text = result["text"]
    for index in range(0, len(text), BUFFERED_CHUNK_CHARS):
        yield {
            "event": "delta",
            "data": {"text": text[index : index + BUFFERED_CHUNK_CHARS]},
        }
