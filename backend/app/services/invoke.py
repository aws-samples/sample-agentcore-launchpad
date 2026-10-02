"""Method-aware invoke + undeploy dispatch.

Chat playground (phase 8) and the public API share this single entry point,
so both consumption paths behave identically.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

from botocore.exceptions import ClientError

from app.assistant.sessions import refuse_assistant_session
from app.core.errors import AppError, aws_error_code
from app.harness_tool_access import (
    qualify_remote_selectors,
    remap_tool_patterns,
    selected_tool_patterns,
)
from app.models.ledger import Agent
from app.optimization import canary_harness, canary_service
from app.services import inbound_auth as inbound_auth_service
from app.services import oauth_sessions
from app.services.agentcore import gateway
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client, data_client
from app.services.attachments import PreparedAttachments
from app.services.runtime_discovery import (
    DISCOVERED_METHOD,
    is_discovered_harness,
    require_invoke_capability,
)
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.templates import gateway_support
from app.templates.identity_support import as_user_connections_stored

logger = logging.getLogger(__name__)
BUFFERED_CHUNK_CHARS = 60
# Runtime methods whose generated entrypoint emits the delta/tool/complete
# envelope over SSE (see `rt._runtime_payload_events`). Shared with chat so the
# advertised `mode` and the actual invoke path can't drift apart.
# byoc is included on the strength of the parser's documented fallback: user
# code that answers a JSON {"result": ...} body (BedrockAgentCoreApp's default)
# is folded into one delta, and code that streams SSE streams for real.
NATIVE_STREAM_METHODS = frozenset({"container", "zip_runtime", "byoc"})
# Methods whose agent ARN is an AgentCore *Runtime* — the only resource with a
# session-stop operation. A managed Harness (deployed or imported) has none:
# neither `bedrock-agentcore` nor `bedrock-agentcore-control` models an
# operation that names both Harness and Session.
RUNTIME_SESSION_METHODS = frozenset(
    {"zip_runtime", "studio", "container", "byoc", DISCOVERED_METHOD}
)


def _runtime_user_id(
    agent: Agent,
    actor_id: str,
    runtime_user_id: str | None = None,
) -> str | None:
    """See ``gateway_support.runtime_user_id`` — omitted unless the spec needs it."""
    return gateway_support.runtime_user_id(agent.spec, runtime_user_id or actor_id)


def _pending_force_reauth(agent: Agent, user_id: str | None) -> list[str]:
    """as_user Connections whose revocation is still in force for this user on
    this agent, limited to the spec's own as_user tools. Read per invoke — the
    spec gate keeps it free for every agent without an as_user tool."""
    if not user_id:
        return []
    connections = as_user_connections_stored(agent.spec)
    if not connections:
        return []
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        return oauth_sessions.pending_revocations(
            db,
            agent.workspace_id,
            user_id=user_id,
            agent_id=agent.id,
            providers=connections,
        )
    finally:
        db.close()


def _record_auth_sessions(
    agent: Agent,
    user_id: str | None,
    asks: list[dict[str, Any]],
    *,
    caller_kind: str = oauth_sessions.CALLER_IAM,
) -> list[dict[str, Any]]:
    """Persist each forwarded 3LO ask against the user it was asked of, and
    strip the session uri from what goes out to the caller.

    The binding leg (CompleteResourceTokenAuth) needs BOTH the session uri and
    the user id; the user id stays server-side (see services/oauth_sessions.py).
    The browser receives the session uri anyway — the IdP redirect carries it to
    the return page — so only the pairing is worth keeping out of the event.

    An ask raised over the workspace M2M token is refused outright (nothing is
    recorded and no consent URL goes out): its consent would bind to the one
    machine subject every M2M caller shares — see
    ``oauth_sessions.AS_USER_REQUIRES_USER_JWT``.
    """
    if not asks:
        return asks
    if caller_kind == oauth_sessions.CALLER_M2M:
        first = asks[0]
        raise oauth_sessions.as_user_requires_user_jwt(
            provider=str(first.get("provider") or ""),
            tool=str(first.get("tool") or ""),
            agent_id=agent.id,
        )
    from app.core.db import SessionLocal

    out: list[dict[str, Any]] = []
    db = SessionLocal()
    try:
        for ask in asks:
            session_uri = str(ask.get("session_uri") or "")
            if session_uri and user_id:
                oauth_sessions.record_pending(
                    db,
                    agent.workspace_id,
                    session_uri=session_uri,
                    provider=str(ask.get("provider") or ""),
                    user_id=user_id,
                    agent_id=agent.id,
                    tool=str(ask.get("tool") or ""),
                    scopes=ask.get("scopes") or (),
                    caller_kind=caller_kind,
                )
            public = {k: v for k, v in ask.items() if k != "session_uri"}
            out.append({**public, "agent_id": agent.id})
    finally:
        db.close()
    return out


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


def harness_user_overrides(
    agent: Agent, workspace: WorkspaceContext, access_token: str,
) -> dict[str, Any]:
    """Pair invocation ToolConfigs with selectors using their actual names."""
    spec = agent.spec or {}
    explicit = spec.get("allowed_tools")
    configured: list[dict[str, Any]] | None = None
    detail: dict[str, Any] = {}
    has_gateways = any(tool.get("type") == "gateway" for tool in spec.get("tools") or [])
    if has_gateways or (
        explicit is not None and any(p.startswith("@") or "/" in p for p in explicit)
    ):
        # The spec's Gateway label is not its resolved Harness alias. Read the
        # deployed aliases and preserve other Gateways' resolved auth configs.
        detail = hc.get_harness(control_client(workspace), agent.resource_id)
        configured = detail.get("tools") or []
        tools = hc.user_authenticated_tools(
            spec, workspace.resources, access_token, configured_tools=configured,
        )
    else:
        tools = hc.user_authenticated_tools(spec, workspace.resources, access_token)
    if len({tool["name"] for tool in tools}) != len(tools):
        raise ValueError("authenticated Gateway alias conflicts with another configured tool")
    names = {
        before["name"]: after["name"]
        for before, after in zip(configured or [], tools, strict=configured is not None)
    }
    deployed = detail.get("allowedTools")
    if explicit is None and isinstance(deployed, list) and deployed:
        # InvokeHarness.allowedTools REPLACES the configured list, so replay the one
        # the deploy derived — target-scoped per Registry record — under the request's
        # aliases. Re-deriving from the swapped tools would select the WHOLE user
        # Gateway (every target), and a stale alias would select nothing at all.
        allowed = remap_tool_patterns(deployed, names)
    elif explicit is None:
        allowed = selected_tool_patterns(
            tools, bool(spec.get("skills")), spec.get("native_tools") or [],
        )
    else:
        allowed = remap_tool_patterns(explicit, names)
        # Retain deployment's mounted-KB support even under an explicit override.
        if spec.get("knowledge_bases"):
            kb_arn = workspace.resources.get("kb_gateway_arn")
            for tool in tools:
                gateway = tool.get("config", {}).get("agentCoreGateway") or {}
                if kb_arn and gateway.get("gatewayArn") == kb_arn:
                    pattern = f"@{tool['name']}"
                    if pattern not in allowed:
                        allowed.append(pattern)
    # the swapped user Gateway is a remote_mcp server: its tool part is qualified
    swapped = {
        after["name"] for before, after in zip(configured or [], tools, strict=False)
        if before.get("type") == "agentcore_gateway" and after.get("type") == "remote_mcp"
    }
    allowed = qualify_remote_selectors(allowed, swapped)
    return {"tools": tools, "allowedTools": allowed}


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
        response = gateway.sigv4_post(
            url, body, workspace, session_id=sticky, user_id=runtime_user_id or actor_id,
        )
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


def _invoke_harness_via_canary(
    route: dict[str, Any],
    prompt: str,
    session_id: str | None,
    actor_id: str,
    workspace: WorkspaceContext,
    harness_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Route a real Harness invocation for an active Harness canary.

    Live gateway → POST the InvokeHarness body through the canary gateway's control
    target (``/<target>/``, no ``/invocations``), which assigns a variant by sticky
    session id; the reply is the raw Harness event stream. A gateway error — and the
    provisioning window before the A/B test exists — serves the control endpoint
    (the version under test's baseline), never DEFAULT (the treatment)."""
    sticky = session_id if (session_id and len(session_id) >= 33) else hc.new_session_id()

    def via_control() -> dict[str, Any]:
        return hc.invoke_harness_text(
            data_client(workspace), route["arn"], prompt,
            session_id=sticky, actor_id=actor_id, qualifier=route["stable_endpoint"],
            **harness_kwargs,
        )

    if not (route.get("gateway_url") and route.get("control_target")):
        return via_control()
    url = (f"{route['gateway_url'].rstrip('/')}/{route['control_target']}"
           f"{canary_harness.TARGET_PATH_SUFFIX}")
    body = canary_harness.invoke_body(
        prompt, actor_id=actor_id, tools=harness_kwargs.get("tools"),
        allowed_tools=harness_kwargs.get("allowed_tools"),
    )
    try:
        response = gateway.sigv4_post(
            url, body, workspace, session_id=sticky,
            user_id=harness_kwargs.get("runtime_user_id") or actor_id,
        )
        if response.status_code != 200:
            raise RuntimeError(f"gateway route returned HTTP {response.status_code}")
        return {"text": hc.event_stream_text(response.content), "session_id": sticky}
    except Exception as exc:
        logger.warning(
            "harness canary gateway route failed (%s); falling back to control endpoint %s",
            exc,
            route.get("stable_endpoint"),
        )
        return via_control()


def _bearer_token_for(
    agent: Agent,
    workspace: WorkspaceContext,
    caller_bearer_token: str | None,
) -> tuple[str, str]:
    """The bearer a JWT-mode agent's invoke sends, and which caller it names.

    Console Chat with "invoke as me" passes the signed-in user's Cognito JWT
    (``policy_identity.gateway_user_token``) → ``user_jwt``; every other caller
    (Chat with the toggle off, public /v1, evaluation runs) sends the workspace
    M2M client_credentials token → ``m2m``. ``m2m_bearer_token`` raises a named
    error when that client is not configured — deliberately no SigV4 fallback,
    which the JWT-mode runtime would just 403. The kind is what the as_user
    consent completion needs: under a JWT authorizer the token vault keys the
    user on the inbound JWT, so leg 4 must present a JWT of the same subject —
    which is why an as_user ask on an ``m2m`` call is refused, never recorded.
    """
    caller = oauth_sessions.CALLER_USER_JWT if caller_bearer_token else oauth_sessions.CALLER_M2M
    # both tokens come from the workspace pool: an agent on another IdP is
    # refused by name here rather than by a bare authorizer 403
    inbound_auth_service.require_platform_reachable(agent, workspace, caller)
    if caller_bearer_token:
        return caller_bearer_token, caller
    return inbound_auth_service.m2m_bearer_token(workspace), caller


def _bearer_kwargs(
    agent: Agent,
    actor_id: str,
    runtime_user_id: str | None,
    gateway_access_token: str | None,
    attachments: PreparedAttachments | None,
) -> tuple[dict[str, Any], str | None]:
    """The optional payload fields of a bearer invoke, plus the ledger user the
    as_user bookkeeping keys on (the Launchpad user, as on the SigV4 path —
    only the vault's view of the user moves to the JWT subject)."""
    ledger_user = _runtime_user_id(agent, actor_id, runtime_user_id)
    kwargs: dict[str, Any] = {}
    if gateway_access_token:
        kwargs["gateway_access_token"] = gateway_access_token
    if attachments and attachments.native:
        kwargs["attachments"] = attachments.native
    force_reauth = _pending_force_reauth(agent, ledger_user)
    if force_reauth:
        kwargs["force_reauth_providers"] = force_reauth
    return kwargs, ledger_user


def _bearer_auth_error(exc: rt.RuntimeBearerAuthError, agent: Agent) -> AppError:
    """Translate an authorizer rejection into a hint the console can act on."""
    config = (agent.inbound_auth_config or {}).get("jwt") or {}
    return AppError(
        "agent.inbound_auth_rejected",
        f"the runtime's JWT authorizer rejected the call (HTTP {exc.status_code}) "
        "— the token's client_id/aud/scope/claims must satisfy the deployed "
        "authorizer",
        {
            "status_code": exc.status_code,
            "allowed_clients": config.get("allowed_clients") or [],
            "allowed_audience": config.get("allowed_audience") or [],
            "allowed_scopes": config.get("allowed_scopes") or [],
            "custom_claims": [
                claim.get("name") for claim in config.get("custom_claims") or []
            ],
            "detail": exc.detail,
        },
        status_code=403,
    )


def invoke_agent_text(
    agent: Agent,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    workspace: WorkspaceContext | None = None,
    attachments: PreparedAttachments | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    require_invoke_capability(agent)
    refuse_assistant_session(agent, session_id)
    workspace = _agent_workspace(agent, workspace)
    if attachments:
        prompt = attachments.prompt(prompt)
    # A JWT-mode runtime accepts Bearer only — SigV4 InvokeAgentRuntime 403s.
    # The bearer path bypasses canary gateway routing (the canary gateway signs
    # SigV4 toward the runtime), so canaries and JWT inbound are mutually
    # exclusive.
    if inbound_auth_service.is_jwt_mode(agent):
        token, caller_kind = _bearer_token_for(agent, workspace, bearer_token)
        kwargs, ledger_user = _bearer_kwargs(
            agent, actor_id, runtime_user_id, gateway_access_token, attachments,
        )
        try:
            result = rt.invoke_runtime_text_bearer(
                workspace.region,
                agent.arn,
                token,
                prompt,
                session_id=session_id,
                actor_id=actor_id,
                **kwargs,
            )
        except rt.RuntimeBearerAuthError as exc:
            raise _bearer_auth_error(exc, agent) from exc
        if result.get("auth_required"):
            result["auth_required"] = _record_auth_sessions(
                agent, ledger_user, result["auth_required"], caller_kind=caller_kind,
            )
        return result
    # An imported harness carries the harness ARN, so it invokes exactly like a
    # launchpad-deployed one — InvokeHarness, never InvokeAgentRuntime.
    if agent.method == "harness" or is_discovered_harness(agent):
        harness_kwargs: dict[str, Any] = {}
        if runtime_user_id:
            harness_kwargs["runtime_user_id"] = runtime_user_id
        if gateway_access_token:
            overrides = harness_user_overrides(agent, workspace, gateway_access_token)
            harness_kwargs["tools"] = overrides["tools"]
            harness_kwargs["allowed_tools"] = overrides["allowedTools"]
        route = canary_service.active_canary_route(agent.id)
        if route is not None and route.get("kind") == "harness":
            return _invoke_harness_via_canary(
                route, prompt, session_id, actor_id, workspace, harness_kwargs,
            )
        return hc.invoke_harness_text(
            data_client(workspace),
            agent.arn,
            prompt,
            session_id=session_id,
            actor_id=actor_id,
            **harness_kwargs,
        )
    if agent.method in ("zip_runtime", "studio", "container", "byoc", DISCOVERED_METHOD):
        # A2A-protocol runtimes speak JSON-RPC; the A2A server owns
        # conversation state (no actor_id/memory envelope) and can't be canaried
        if (agent.spec or {}).get("protocol") == "a2a":
            files = (
                {"attachments": attachments.native} if attachments and attachments.native else {}
            )
            return rt.invoke_a2a_text(
                data_client(workspace), agent.arn, prompt, session_id=session_id, **files,
            )
        # During an active canary, real production traffic for this agent flows
        # through the canary's gateway; otherwise the path below is unchanged.
        route = canary_service.active_canary_route(agent.id)
        if route is not None:
            if attachments and attachments.native:
                raise AppError(
                    "chat.attachment_canary_unsupported",
                    "Native attachments are unavailable during a canary.", status_code=409,
                )
            return _invoke_via_canary(
                route,
                prompt,
                session_id,
                actor_id,
                workspace,
                runtime_user_id,
                gateway_access_token,
            )
        resolved_user = _runtime_user_id(agent, actor_id, runtime_user_id)
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "actor_id": actor_id,
            "runtime_user_id": resolved_user,
        }
        if gateway_access_token:
            kwargs["gateway_access_token"] = gateway_access_token
        if attachments and attachments.native:
            kwargs["attachments"] = attachments.native
        force_reauth = _pending_force_reauth(agent, resolved_user)
        if force_reauth:
            kwargs["force_reauth_providers"] = force_reauth
        result = rt.invoke_runtime_text(
            data_client(workspace), agent.arn, prompt, **kwargs
        )
        if result.get("auth_required"):
            result["auth_required"] = _record_auth_sessions(
                agent, resolved_user, result["auth_required"]
            )
        return result
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
    attachments: PreparedAttachments | None = None,
    bearer_token: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield native runtime events, with a buffered compatibility fallback.

    Claude SDK containers and generated Strands zip runtimes both emit the
    delta/tool/complete envelope over the Runtime SSE response, so they are
    parsed incrementally. A zip runtime deployed before its template streamed
    still answers a JSON ``{"result": ...}`` body, which the same parser turns
    into one delta — so the switch is safe for existing agents.
    """
    require_invoke_capability(agent)
    refuse_assistant_session(agent, session_id)
    workspace = _agent_workspace(agent, workspace)
    if inbound_auth_service.is_jwt_mode(agent):
        # Streaming parity with SigV4: the HTTPS response feeds the same body
        # parser, so tool/delta/auth_required arrive exactly as they do over
        # invoke_agent_runtime.
        token, caller_kind = _bearer_token_for(agent, workspace, bearer_token)
        if attachments:
            prompt = attachments.prompt(prompt)
        kwargs, ledger_user = _bearer_kwargs(
            agent, actor_id, runtime_user_id, gateway_access_token, attachments,
        )
        try:
            for event in rt.stream_runtime_events_bearer(
                workspace.region,
                agent.arn,
                token,
                prompt,
                session_id=session_id,
                actor_id=actor_id,
                **kwargs,
            ):
                if event.get("event") == "auth_required":
                    event = {
                        "event": "auth_required",
                        "data": _record_auth_sessions(
                            agent, ledger_user, [event["data"]], caller_kind=caller_kind,
                        )[0],
                    }
                yield event
        except rt.RuntimeBearerAuthError as exc:
            raise _bearer_auth_error(exc, agent) from exc
        return
    streams_natively = (
        agent.method in NATIVE_STREAM_METHODS
        and (agent.spec or {}).get("protocol", "http") != "a2a"
    )
    if streams_natively and canary_service.active_canary_route(agent.id) is None:
        resolved_user = _runtime_user_id(agent, actor_id, runtime_user_id)
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "actor_id": actor_id,
            "runtime_user_id": resolved_user,
        }
        if gateway_access_token:
            kwargs["gateway_access_token"] = gateway_access_token
        if attachments:
            prompt = attachments.prompt(prompt)
            if attachments.native:
                kwargs["attachments"] = attachments.native
        force_reauth = _pending_force_reauth(agent, resolved_user)
        if force_reauth:
            kwargs["force_reauth_providers"] = force_reauth
        for event in rt.stream_runtime_events(
            data_client(workspace), agent.arn, prompt, **kwargs
        ):
            if event.get("event") == "auth_required":
                event = {
                    "event": "auth_required",
                    "data": _record_auth_sessions(agent, resolved_user, [event["data"]])[0],
                }
            yield event
        return

    extra = {"attachments": attachments} if attachments else {}
    result = invoke_agent_text(
        agent,
        prompt,
        session_id=session_id,
        actor_id=actor_id,
        runtime_user_id=runtime_user_id,
        gateway_access_token=gateway_access_token,
        workspace=workspace,
        **extra,
    )
    for ask in result.get("auth_required") or []:
        yield {"event": "auth_required", "data": ask}
    text = result["text"]
    for index in range(0, len(text), BUFFERED_CHUNK_CHARS):
        yield {
            "event": "delta",
            "data": {"text": text[index : index + BUFFERED_CHUNK_CHARS]},
        }
