"""Method-aware invoke + undeploy dispatch.

Chat playground (phase 8) and the public API share this single entry point,
so both consumption paths behave identically.
"""

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

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
from app.services import oauth_sessions, outbound_attachments, user_gateways
from app.services.agentcore import gateway
from app.services.agentcore import harness as hc
from app.services.agentcore import policy as gateway_api
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client, data_client
from app.services.attachments import PreparedAttachments
from app.services.runtime_discovery import (
    DISCOVERED_METHOD,
    is_discovered_harness,
    require_invoke_capability,
)
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.templates import context_layer_support, gateway_support
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
    layered = context_layer_support.attached(agent.spec)
    connections = as_user_connections_stored(agent.spec)
    if not connections and not layered:
        return []
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        if layered:
            # the COA tool's Connection is the workspace layer's, not named in the spec
            from app.services import context_layer

            connections = as_user_connections_stored(
                agent.spec, context_layer.connection_for(db, agent.workspace_id)
            )
        if not connections:
            return []
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
    console_user: bool = True,
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

    ``console_user=False`` (the public /v1 API) refuses every ask the same way:
    the consent is completed by the console's return page for the console user
    the ask was recorded against, and a /v1 caller is an API key whose
    ``actor_id`` names nobody who can sign in — so its session could never
    complete (``oauth_sessions.AS_USER_REQUIRES_CONSOLE``). The vault user a
    /v1 turn presents is its scoped actor (``<agent>__<actor_id>``), never a
    console username, so a consent given in Chat does not carry over either:
    as_user tools are a Chat-console feature, and /v1 says so by name instead
    of handing out a consent URL that dead-ends.
    """
    if not asks:
        return asks
    if not console_user:
        first = asks[0]
        raise oauth_sessions.as_user_requires_console(
            provider=str(first.get("provider") or ""),
            tool=str(first.get("tool") or ""),
            agent_id=agent.id,
        )
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


def _usage_kw(on_usage: Any) -> dict[str, Any]:
    """Forward a meter's sink only when there is one, so a caller that passes none
    keeps the exact wrapper call it made before metering existed."""
    return {"on_usage": on_usage} if on_usage is not None else {}


def _parse_gateway_text(
    raw_text: str, session_id: str, on_usage: Any = None,
) -> dict[str, Any]:
    """Parse a gateway HTTP response body the SAME way ``rt.invoke_runtime_text``
    parses ``invoke_agent_runtime``: JSON ``{"result": ...}`` or an SSE stream via
    ``rt.flatten_sse_text``."""
    try:
        body = json.loads(raw_text)
    except (ValueError, TypeError):
        body = {"result": rt.flatten_sse_text(raw_text, **_usage_kw(on_usage)) or raw_text}
    else:
        if isinstance(body, dict):
            rt.report_usage(on_usage, body.get("usage"), "turn")
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


def _user_trusting_gateways(
    configured: list[dict[str, Any]], workspace: WorkspaceContext,
) -> dict[str, str]:
    """Gateway ARN -> MCP URL for configured Gateways (besides launchpad-gw) that may
    carry the signed-in user's token this turn.

    A candidate is a deployed ``agentcore_gateway`` tool authenticated with the
    workspace M2M provider — the only outbound auth the platform maps for a
    CUSTOM_JWT Gateway. Each one is re-read now, not trusted from deploy time: a
    Gateway whose authorizer or interceptors changed since deploy stops receiving the
    user token and keeps its configured M2M identity. A read failure does the same.
    """
    resources = workspace.resources
    provider = resources.get("oauth_provider_arn")
    result: dict[str, str] = {}
    for tool in configured:
        if tool.get("type") != "agentcore_gateway":
            continue
        cfg = (tool.get("config") or {}).get("agentCoreGateway") or {}
        arn = str(cfg.get("gatewayArn") or "")
        oauth = (cfg.get("outboundAuth") or {}).get("oauth") or {}
        if not arn or arn == resources.get("gateway_arn") or not provider:
            continue
        if oauth.get("providerArn") != provider or arn == resources.get("kb_gateway_arn"):
            continue
        parts = arn.split(":")
        region, gateway_id = parts[3], arn.rsplit("/", 1)[-1]
        ctx = workspace if region == workspace.region else replace(workspace, region=region)
        try:
            gateway = gateway_api.get_gateway(control_client(ctx), gateway_id)
        except (ClientError, BotoCoreError) as exc:
            logger.warning("user gateway %s unreadable, keeping M2M identity: %s", arn, exc)
            continue
        if gateway.get("gatewayArn") == arn and gateway.get("gatewayUrl") and \
                user_gateways.trusts_workspace_users(gateway, resources):
            result[arn] = str(gateway["gatewayUrl"])
    return result


def harness_user_overrides(
    agent: Agent, workspace: WorkspaceContext, access_token: str | None,
    *, coa_tool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pair invocation ToolConfigs with selectors using their actual names.

    ``access_token`` (the signed-in user's workspace-pool JWT) swaps the user-trusting
    Gateways; ``coa_tool`` (``context_layer_support.harness_tool``) adds the COA MCP
    server carrying the member's COA token, and ``@coa`` to the selectors. With a COA
    tool the deployed configuration is always read back, so the override REPLACES
    nothing it should keep (KB gateway, registry MCP servers, inline functions). So it
    is for a context layer in gateway mode, whose managed COA gateway is a deployed
    attachment the spec does not name (and its selector is kept under an explicit
    list)."""
    spec = agent.spec or {}
    explicit = spec.get("allowed_tools")
    configured: list[dict[str, Any]] | None = None
    detail: dict[str, Any] = {}
    has_gateways = any(tool.get("type") == "gateway" for tool in spec.get("tools") or [])
    coa_gateway = context_layer_support.uses_gateway(spec)
    if coa_tool is not None or coa_gateway or has_gateways or (
        explicit is not None and any(p.startswith("@") or "/" in p for p in explicit)
    ):
        # The spec's Gateway label is not its resolved Harness alias. Read the
        # deployed aliases and preserve other Gateways' resolved auth configs.
        detail = hc.get_harness(control_client(workspace), agent.resource_id)
        configured = detail.get("tools") or []
        if access_token:
            tools = hc.user_authenticated_tools(
                spec, workspace.resources, access_token, configured_tools=configured,
                user_gateways=_user_trusting_gateways(configured, workspace),
            )
        else:  # the COA tool alone: every deployed tool exactly as configured
            tools = [dict(tool) for tool in configured]
    else:
        tools = hc.user_authenticated_tools(spec, workspace.resources, access_token or "")
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
    selector = context_layer_support.GATEWAY_SELECTOR
    if coa_gateway and selector not in allowed and any(
        tool["name"] == context_layer_support.GATEWAY_ALIAS for tool in tools
    ):
        allowed = [*allowed, selector]  # as at deploy: an explicit list keeps the layer
    if coa_tool is not None:
        if any(tool["name"] == coa_tool["name"] for tool in tools):
            raise ValueError(
                f"a deployed tool is already named {coa_tool['name']!r}, the COA context "
                "layer's tool name — rename it and re-publish the agent"
            )
        tools = [*tools, coa_tool]
        # one selector exposes all six COA tools (the model sees coa_<tool>);
        # appended after any remap, so an explicit list keeps its own patterns
        if context_layer_support.HARNESS_SELECTOR not in allowed:
            allowed = [*allowed, context_layer_support.HARNESS_SELECTOR]
    return {"tools": tools, "allowedTools": allowed}


def harness_turn_overrides(
    agent: Agent,
    workspace: WorkspaceContext,
    *,
    gateway_access_token: str | None = None,
    runtime_user_id: str | None = None,
    console_user: bool = True,
    session_id: str | None = None,
    user_role: str | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    """The per-turn ``InvokeHarness`` overrides: ``{tools, allowedTools}``, ``{}``
    when the deployed configuration applies unchanged, or ``{"auth_required":
    notice}`` when the member must consent to COA before the turn can run.

    An agent without a context layer gets exactly what it always got. One with a
    layer fetches the member's COA token (``context_layer_tokens``) for
    ``runtime_user_id`` — the signed-in console username — and fails closed with
    ``identity.as_user_requires_console`` when there is none (``/v1``, evaluation).
    One in gateway mode goes through ``_gateway_turn_overrides`` instead
    (``user_role`` / ``user_email``: the signed-in member's, for their pool JWT).
    """
    if not context_layer_support.attached(agent.spec):
        if gateway_access_token:
            return harness_user_overrides(agent, workspace, gateway_access_token)
        return {}
    if context_layer_support.uses_gateway(agent.spec):
        return _gateway_turn_overrides(
            agent, workspace, gateway_access_token=gateway_access_token,
            username=runtime_user_id, role=user_role, email=user_email,
            console_user=console_user, session_id=session_id or "",
        )
    from app.services import context_layer_tokens

    grant = context_layer_tokens.turn_token(
        workspace, runtime_user_id, console_user=console_user,
        agent_id=agent.id, session_id=session_id or "",
    )
    if isinstance(grant, context_layer_tokens.AuthNeeded):
        return {"auth_required": context_layer_tokens.auth_notice(grant, agent.id)}
    return harness_user_overrides(
        agent, workspace, gateway_access_token,
        coa_tool=context_layer_support.harness_tool(grant.layer.mcp_url, grant.value),
    )


def _gateway_turn_overrides(
    agent: Agent,
    workspace: WorkspaceContext,
    *,
    gateway_access_token: str | None,
    username: str | None,
    role: str | None,
    email: str | None,
    console_user: bool,
    session_id: str,
) -> dict[str, Any]:
    """A gateway-mode Harness turn (``context_layer.via = "gateway"``).

    1. The consent gate (``context_layer_gateway.gateway_turn_auth``, a blocking
       preflight through the managed gateway — every caller already runs off the event
       loop): ``{"auth_required": notice}`` ends the turn before InvokeHarness. No
       console user (``/v1``, evaluation; also a caller that did not say which role the
       member signed in with) ⇒ ``identity.as_user_requires_console``.
    2. The swap: the deployed ``launchpad_coa_gw`` attachment becomes a ``remote_mcp``
       server carrying the member's pool JWT (``harness_user_overrides``; the gateway
       is re-read and must still trust the workspace pool). The router's token, or the
       one the gate just minted (``policy_identity``'s per-process cache).
    3. Fail closed when the swap did not happen: the deployed configuration lacks the
       attachment (re-publish), or the gateway could not be verified. The turn never
       reaches COA's gateway with the workspace M2M identity.

    No COA token is fetched by the backend in this mode.
    """
    from app.services import context_layer_gateway, policy_identity

    gate = context_layer_gateway.gateway_turn_auth(
        agent, workspace, username=username, role=role or "", email=email,
        session_id=session_id, console_user=console_user and bool(role),
    )
    if gate is not None:
        return gate
    token = gateway_access_token or policy_identity.gateway_user_token(
        workspace, str(username), str(role), email,
        min_valid_s=context_layer_gateway.CONSENT_JWT_MIN_VALID_S,
    )
    overrides = harness_user_overrides(agent, workspace, token)
    alias = context_layer_support.GATEWAY_ALIAS
    coa = next((tool for tool in overrides["tools"] if tool.get("name") == alias), None)
    if coa is None:
        raise context_layer_gateway.gateway_unavailable(
            "this agent's deployed configuration does not attach the managed COA gateway: "
            "re-publish it to reach the context layer through the gateway"
        )
    remote = (coa.get("config") or {}).get("remoteMcp") or {}
    if (
        coa.get("type") != "remote_mcp"
        or not token
        or (remote.get("headers") or {}).get("Authorization") != f"Bearer {token}"
    ):
        # swapped means: a remote_mcp server carrying exactly this member's JWT — not an
        # unswapped M2M attachment, nor some other server that happens to bear the alias
        raise context_layer_gateway.gateway_unavailable(
            "the managed COA gateway could not be verified for this turn (it is missing, "
            "or no longer trusts the workspace's users): try again, or ask an "
            "administrator to repair it on the Context Layer page"
        )
    return overrides


def _runtime_gateway_mode(agent: Agent) -> bool:
    """A Strands (``zip_runtime``) agent reaching its context layer through the managed
    COA gateway (``context_layer.via = "gateway"``): its turns go through
    ``_runtime_gateway_gate`` before the runtime is invoked."""
    return agent.method == "zip_runtime" and context_layer_support.uses_gateway(agent.spec)


def _runtime_gateway_gate(
    agent: Agent,
    workspace: WorkspaceContext,
    *,
    gateway_access_token: str | None,
    username: str | None,
    role: str | None,
    email: str | None,
    console_user: bool,
    session_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """A gateway-mode Strands turn: ``(notice, "")`` ends it at the auth card before
    the runtime is invoked; ``(None, jwt)`` runs it with the member's pool JWT as the
    payload's ``gateway_access_token``, the bearer the generated runtime presents to the
    managed gateway (``CONTEXT_GATEWAY``).

    The same gate as a Harness turn (``_gateway_turn_overrides``): a blocking preflight
    (every caller runs off the event loop), ``identity.as_user_requires_console`` for
    ``/v1``, evaluation and a caller that did not say the member's role. The JWT is the
    router's, or the one the gate just minted (``policy_identity``'s per-process cache,
    with the consent margin), so the preflight and the turn carry one token. The runtime
    has no machine-identity fallback toward COA's gateway, and neither does this: no
    token, no turn. No COA token is fetched by the backend in this mode.
    """
    from app.services import context_layer_gateway, policy_identity

    gate = context_layer_gateway.gateway_turn_auth(
        agent, workspace, username=username, role=role or "", email=email,
        session_id=session_id, console_user=console_user and bool(role),
    )
    if gate is not None:
        return gate["auth_required"], ""
    token = gateway_access_token or policy_identity.gateway_user_token(
        workspace, str(username), str(role), email,
        min_valid_s=context_layer_gateway.CONSENT_JWT_MIN_VALID_S,
    )
    if not token:
        raise context_layer_gateway.gateway_unavailable(
            "the managed COA gateway could not be reached as you this turn: try again"
        )
    return None, token


def _invoke_via_stable_endpoint(
    route: dict[str, Any],
    prompt: str,
    session_id: str | None,
    actor_id: str,
    workspace: WorkspaceContext,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    on_usage: Any = None,
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
    kwargs.update(_usage_kw(on_usage))
    return rt.invoke_runtime_text(data_client(workspace), route["arn"], prompt, **kwargs)


def _invoke_via_canary(
    route: dict[str, Any],
    prompt: str,
    session_id: str | None,
    actor_id: str,
    workspace: WorkspaceContext,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    on_usage: Any = None,
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
            **_usage_kw(on_usage),
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
        return _parse_gateway_text(response.text, sticky, on_usage)
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
            **_usage_kw(on_usage),
        )


def _invoke_harness_via_canary(
    route: dict[str, Any],
    prompt: str,
    session_id: str | None,
    actor_id: str,
    workspace: WorkspaceContext,
    harness_kwargs: dict[str, Any],
    on_usage: Any = None,
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
            **harness_kwargs, **_usage_kw(on_usage),
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
        return {"text": hc.event_stream_text(response.content, **_usage_kw(on_usage)),
                "session_id": sticky}
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
    console_user: bool = True,
    on_usage: Any = None,
    user_role: str | None = None,
    user_email: str | None = None,
    attachment_owner: str | None = None,
) -> dict[str, Any]:
    """One synchronous turn. ``console_user=False`` marks a caller with no
    signed-in console user (public /v1): an as_user consent ask is then refused
    by name instead of recorded (see ``_record_auth_sessions``).

    ``on_usage`` is a ``UsageMeter.on_usage`` sink (services/usage_meter.py): the
    token usage each path reports in-band reaches it; the caller settles the turn.
    ``user_role`` / ``user_email``: the signed-in member's, for a context layer in
    gateway mode (``harness_turn_overrides``, ``_runtime_gateway_gate``).

    ``attachment_owner`` (``outbound_attachments``: ``console`` or an API key's
    owner) opts the entrance into reply attachments: the images the runtime
    answered with are stored and ``result["attachments"]`` lists their metadata.
    Without it they are dropped unstored; raw bytes never leave this function."""
    result = _invoke_agent_text(
        agent, prompt, session_id, actor_id, runtime_user_id, gateway_access_token,
        workspace, attachments, bearer_token, console_user, on_usage, user_role, user_email,
    )
    raw = result.pop("reply_attachments", None) or []
    if attachment_owner is not None:
        sink = outbound_attachments.ReplyAttachments(
            agent, workspace, result.get("session_id"), attachment_owner,
        )
        result["attachments"] = [
            meta for meta in (sink.accept(item) for item in raw) if meta is not None
        ]
    elif raw:
        logger.info("dropped %d reply attachment(s): the entrance relays none", len(raw))
    return result


def _invoke_agent_text(
    agent: Agent,
    prompt: str,
    session_id: str | None,
    actor_id: str,
    runtime_user_id: str | None,
    gateway_access_token: str | None,
    workspace: WorkspaceContext | None,
    attachments: PreparedAttachments | None,
    bearer_token: str | None,
    console_user: bool,
    on_usage: Any,
    user_role: str | None,
    user_email: str | None,
) -> dict[str, Any]:
    require_invoke_capability(agent)
    refuse_assistant_session(agent, session_id)
    workspace = _agent_workspace(agent, workspace)
    if _runtime_gateway_mode(agent):
        notice, gateway_access_token = _runtime_gateway_gate(
            agent, workspace, gateway_access_token=gateway_access_token,
            username=runtime_user_id, role=user_role, email=user_email,
            console_user=console_user, session_id=session_id or "",
        )
        if notice is not None:
            # the consent the member owes COA: the runtime is not invoked this turn
            return {"text": "", "session_id": session_id or hc.new_session_id(),
                    "auth_required": [notice]}
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
        kwargs.update(_usage_kw(on_usage))
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
                console_user=console_user,
            )
        return result
    # An imported harness carries the harness ARN, so it invokes exactly like a
    # launchpad-deployed one — InvokeHarness, never InvokeAgentRuntime.
    if agent.method == "harness" or is_discovered_harness(agent):
        harness_kwargs: dict[str, Any] = {}
        if runtime_user_id:
            harness_kwargs["runtime_user_id"] = runtime_user_id
        overrides = harness_turn_overrides(
            agent, workspace, gateway_access_token=gateway_access_token,
            runtime_user_id=runtime_user_id, console_user=console_user, session_id=session_id,
            user_role=user_role, user_email=user_email,
        )
        if "auth_required" in overrides:
            # the consent the member owes COA: nothing is invoked this turn
            asks = [overrides["auth_required"]]
            return {"text": "", "session_id": session_id or hc.new_session_id(),
                    "auth_required": asks}
        if overrides:
            harness_kwargs["tools"] = overrides["tools"]
            harness_kwargs["allowed_tools"] = overrides["allowedTools"]
        # a COA token only ever travels to COA: never through a canary gateway
        layered = context_layer_support.attached(agent.spec)
        route = None if layered else canary_service.active_canary_route(agent.id)
        if route is not None and route.get("kind") == "harness":
            return _invoke_harness_via_canary(
                route, prompt, session_id, actor_id, workspace, harness_kwargs,
                **_usage_kw(on_usage),
            )
        return hc.invoke_harness_text(
            data_client(workspace),
            agent.arn,
            prompt,
            session_id=session_id,
            actor_id=actor_id,
            **harness_kwargs,
            **_usage_kw(on_usage),
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
                **_usage_kw(on_usage),
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
        kwargs.update(_usage_kw(on_usage))
        result = rt.invoke_runtime_text(
            data_client(workspace), agent.arn, prompt, **kwargs
        )
        if result.get("auth_required"):
            result["auth_required"] = _record_auth_sessions(
                agent, resolved_user, result["auth_required"], console_user=console_user,
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
    console_user: bool = True,
    on_usage: Any = None,
    user_role: str | None = None,
    user_email: str | None = None,
    attachment_owner: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield native runtime events, with a buffered compatibility fallback.

    Claude SDK containers and generated Strands zip runtimes both emit the
    delta/tool/complete envelope over the Runtime SSE response, so they are
    parsed incrementally. A zip runtime deployed before its template streamed
    still answers a JSON ``{"result": ...}`` body, which the same parser turns
    into one delta — so the switch is safe for existing agents.
    ``console_user``, ``on_usage``, ``user_role`` and ``user_email`` as on
    ``invoke_agent_text``: a gateway-mode Strands turn without the member's consent
    is one ``auth_required`` event, and the runtime is not invoked.
    ``attachment_owner`` as on ``invoke_agent_text``: each reply image is one
    ``attachment`` event carrying its stored (or dropped) metadata.
    """
    require_invoke_capability(agent)
    refuse_assistant_session(agent, session_id)
    workspace = _agent_workspace(agent, workspace)
    if _runtime_gateway_mode(agent):
        notice, gateway_access_token = _runtime_gateway_gate(
            agent, workspace, gateway_access_token=gateway_access_token,
            username=runtime_user_id, role=user_role, email=user_email,
            console_user=console_user, session_id=session_id or "",
        )
        if notice is not None:
            yield {"event": "auth_required", "data": notice}
            return
    if attachment_owner is not None and not session_id:
        session_id = rt.new_session_id()  # the stored images are keyed on it
    reply = outbound_attachments.ReplyAttachments(
        agent, workspace, session_id, attachment_owner,
    )
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
        kwargs.update(_usage_kw(on_usage))
        try:
            for event in outbound_attachments.relay(rt.stream_runtime_events_bearer(
                workspace.region,
                agent.arn,
                token,
                prompt,
                session_id=session_id,
                actor_id=actor_id,
                **kwargs,
            ), reply):
                if event.get("event") == "auth_required":
                    event = {
                        "event": "auth_required",
                        "data": _record_auth_sessions(
                            agent, ledger_user, [event["data"]], caller_kind=caller_kind,
                            console_user=console_user,
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
        kwargs.update(_usage_kw(on_usage))
        for event in outbound_attachments.relay(rt.stream_runtime_events(
            data_client(workspace), agent.arn, prompt, **kwargs
        ), reply):
            if event.get("event") == "auth_required":
                event = {
                    "event": "auth_required",
                    "data": _record_auth_sessions(
                        agent, resolved_user, [event["data"]], console_user=console_user,
                    )[0],
                }
            yield event
        return

    extra: dict[str, Any] = {"attachments": attachments} if attachments else {}
    if not console_user:
        extra["console_user"] = False
    if user_role:
        # a gateway-mode agent's text path gates again: a cache hit after the gate above
        extra["user_role"] = user_role
        extra["user_email"] = user_email
    extra.update(_usage_kw(on_usage))
    if attachment_owner is not None:
        extra["attachment_owner"] = attachment_owner
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
    for meta in result.get("attachments") or []:
        yield {"event": "attachment", "data": meta}
