"""Thin wrappers over the Harness control/data APIs.

Every function takes an explicit client so tests inject stubs that capture
kwargs. Payload shapes follow bedrock-agentcore-control 1.43.x.
"""

import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import closing
from typing import Any

from app.core.errors import AppError

TERMINAL_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
BUILTIN_TOOL_TYPES = {
    "code-interpreter": "agentcore_code_interpreter",
    "browser": "agentcore_browser",
}


def create_harness(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """CreateHarness; returns the harness detail dict (harnessId, arn, status…)."""
    return client.create_harness(**params)["harness"]


def update_harness(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """UpdateHarness — publishes a new harness version in place. Same harnessId
    and ARN; ``params`` carries ``harnessId`` plus the edited config (model,
    systemPrompt, tools, memory…), i.e. ``wrap_params_for_update`` output."""
    return client.update_harness(**params)["harness"]


def wrap_params_for_update(params: dict[str, Any]) -> dict[str, Any]:
    """Create-style params → UpdateHarness kwargs (same pattern as the registry's
    ``wrap_descriptors_for_update``). Update reuses the create shapes except
    ``memory``, whose value must sit in {"optionalValue": …}. Omitting memory
    means "keep the old config", so a spec without memory sends the explicit
    ``disabled`` variant to detach it. ``tools``/``skills`` share that omit=keep
    semantic — send explicit empty lists so deselecting the last tool (e.g. the
    only mounted KB) actually detaches it. Drops the immutable ``harnessName``."""
    update = {k: v for k, v in params.items() if k != "harnessName"}
    update["memory"] = {"optionalValue": update.get("memory") or {"disabled": {}}}
    update.setdefault("tools", [])
    update.setdefault("skills", [])
    return update


# HarnessAgentCoreRuntimeEnvironmentRequest members — GetHarness also echoes the
# read-only backing-runtime identifiers (agentRuntimeArn/Name/Id), which Update refuses.
_ENVIRONMENT_REQUEST_FIELDS = ("lifecycleConfiguration", "networkConfiguration")


def environment_for_update(
    live: Mapping[str, Any], session_storage: list[dict[str, Any]]
) -> dict[str, Any]:
    """UpdateHarness ``environment`` that sets the platform-owned session storage and
    keeps everything else the live harness carries.

    UpdateHarness replaces ``filesystemConfigurations`` wholesale (``[]`` detaches
    every mount; an omitted ``environment`` keeps the old one — probed live), so the
    live list is read back and only its ``sessionStorage`` entry is swapped: EFS /
    S3 Files mounts and network/lifecycle settings made outside the platform (an
    imported harness) survive a re-publish."""
    env = (live.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
    request = {k: env[k] for k in _ENVIRONMENT_REQUEST_FIELDS if env.get(k)}
    kept = [fs for fs in env.get("filesystemConfigurations") or [] if "sessionStorage" not in fs]
    request["filesystemConfigurations"] = [*session_storage, *kept]
    return {"agentCoreRuntimeEnvironment": request}


def get_harness(
    client: Any, harness_id: str, version: str | None = None
) -> dict[str, Any]:
    """GetHarness; ``version`` reads one immutable version's full config (the
    canary's rollback re-publishes an earlier version from it)."""
    kwargs: dict[str, Any] = {"harnessId": harness_id}
    if version:
        kwargs["harnessVersion"] = str(version)
    return client.get_harness(**kwargs)["harness"]


def list_harnesses(client: Any) -> list[dict[str, Any]]:
    """Return every Harness summary across all ListHarnesses pages."""
    harnesses: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"maxResults": 100}
    while True:
        page = client.list_harnesses(**kwargs)
        harnesses.extend(page.get("harnesses", []))
        token = page.get("nextToken")
        if not token:
            return harnesses
        kwargs["nextToken"] = token


def list_harness_versions(client: Any, harness_id: str) -> list[dict[str, Any]]:
    """Every version of one harness across all ListHarnessVersions pages.
    ``HarnessVersionSummary`` has no ``description`` and uses ``updatedAt`` (not
    ``lastUpdatedAt``): {harnessVersion, status, createdAt, updatedAt,
    failureReason, …}. The caller projects."""
    versions: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"harnessId": harness_id, "maxResults": 100}
    while True:
        page = client.list_harness_versions(**kwargs)
        versions.extend(page.get("harnessVersions", []))
        token = page.get("nextToken")
        if not token:
            return versions
        kwargs["nextToken"] = token


def list_harness_endpoints(client: Any, harness_id: str) -> list[dict[str, Any]]:
    """Every endpoint of one harness across all ListHarnessEndpoints pages:
    {endpointName, liveVersion, targetVersion, status, description, createdAt,
    updatedAt, failureReason, …}."""
    endpoints: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"harnessId": harness_id, "maxResults": 100}
    while True:
        page = client.list_harness_endpoints(**kwargs)
        endpoints.extend(page.get("endpoints", []))
        token = page.get("nextToken")
        if not token:
            return endpoints
        kwargs["nextToken"] = token


def delete_harness(client: Any, harness_id: str) -> None:
    client.delete_harness(harnessId=harness_id)


def get_harness_endpoint(client: Any, harness_id: str, endpoint_name: str) -> dict[str, Any]:
    """GetHarnessEndpoint → the ``endpoint`` object ({status, liveVersion, arn, …})."""
    return client.get_harness_endpoint(harnessId=harness_id, endpointName=endpoint_name)[
        "endpoint"
    ]


def ensure_harness_endpoint(
    client: Any,
    harness_id: str,
    endpoint_name: str,
    version: str,
    *,
    description: str = "",
    timeout_s: int = 300,
    interval_s: int = 5,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    """Create (or re-point) a named endpoint pinned to ``version`` and wait READY.

    Idempotent: a ConflictException means a previous attempt created it, in which
    case it is re-pointed when it serves another version."""
    try:
        client.create_harness_endpoint(
            harnessId=harness_id,
            endpointName=endpoint_name,
            targetVersion=str(version),
            description=description,
            clientToken=str(uuid.uuid4()),
        )
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
        current = get_harness_endpoint(client, harness_id, endpoint_name)
        if str(current.get("liveVersion") or "") != str(version):
            client.update_harness_endpoint(
                harnessId=harness_id, endpointName=endpoint_name,
                targetVersion=str(version), clientToken=str(uuid.uuid4()),
            )
    deadline = time.monotonic() + timeout_s
    while True:
        endpoint = get_harness_endpoint(client, harness_id, endpoint_name)
        status = endpoint.get("status")
        if status == "READY" and str(endpoint.get("liveVersion") or "") == str(version):
            return endpoint
        if status in TERMINAL_FAILURES:
            reason = endpoint.get("failureReason", "no failureReason provided")
            raise RuntimeError(f"harness endpoint {endpoint_name} entered {status}: {reason}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"harness endpoint {endpoint_name} still {status}")
        sleeper(interval_s)


def delete_harness_endpoint(client: Any, harness_id: str, endpoint_name: str) -> bool:
    """Delete a named endpoint; ``False`` when it is already gone."""
    try:
        client.delete_harness_endpoint(harnessId=harness_id, endpointName=endpoint_name)
    except Exception as exc:
        if type(exc).__name__ in {"ResourceNotFoundException", "NotFoundException"}:
            return False
        raise
    return True


def decode_event_stream(raw: bytes) -> list[dict[str, Any]]:
    """Raw ``application/vnd.amazon.eventstream`` bytes → boto3-shaped events.

    A canary gateway passthrough target relays InvokeHarness verbatim, so its HTTP
    body is the event stream boto3 would otherwise decode: each message becomes
    ``{<:event-type>: payload}`` (an ``exception`` message ``{<:exception-type>: …}``),
    which is exactly what :func:`iter_harness_stream` consumes."""
    import json

    from botocore.eventstream import EventStreamBuffer

    buffer = EventStreamBuffer()
    buffer.add_data(raw)
    events: list[dict[str, Any]] = []
    for message in buffer:
        headers = message.headers
        kind = headers.get(":event-type") or headers.get(":exception-type") or "unknown"
        try:
            payload = json.loads(message.payload.decode("utf-8")) if message.payload else {}
        except ValueError:
            payload = {"message": message.payload.decode("utf-8", "replace")}
        events.append({str(kind): payload})
    return events


def event_stream_text(raw: bytes) -> str:
    """Concatenated assistant text of a raw Harness event stream (raises on a
    stream error event, like the boto3 path)."""
    parts: list[str] = []
    with closing(iter_harness_stream(decode_event_stream(raw))) as events:
        for event in events:
            delta = event.get("contentBlockDelta", {}).get("delta", {})
            if delta.get("text"):
                parts.append(delta["text"])
    return "".join(parts)


def wait_harness_ready(
    client: Any,
    harness_id: str,
    timeout_s: int = 300,
    interval_s: int = 5,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    """Poll GetHarness until READY; raise on terminal failure or timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        harness = get_harness(client, harness_id)
        status = harness["status"]
        if status == "READY":
            return harness
        if status in TERMINAL_FAILURES:
            reason = harness.get("failureReason", "no failureReason provided")
            raise RuntimeError(f"harness {harness_id} entered {status}: {reason}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"harness {harness_id} still {status} after {timeout_s}s")
        sleeper(interval_s)


def new_session_id() -> str:
    # Runtime session ids must be long (≥33 chars); two uuid4 hex = 64.
    return uuid.uuid4().hex + uuid.uuid4().hex


# The remote_mcp server the logged-in chat path mounts in place of launchpad-gw. A
# Harness names its tools ``<alias>_<tool>`` — the evaluator Lambda strips exactly
# this prefix (app/assistant/lambda_runtime/handler.py USER_GATEWAY_TOOL_PREFIX).
USER_GATEWAY_ALIAS = "launchpad_gw_user"


def user_authenticated_tools(
    spec: Mapping[str, Any],
    resources: Mapping[str, Any],
    access_token: str,
    *,
    configured_tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Harness invocation tools with launchpad-gw authenticated as one user.

    ``InvokeHarness.tools`` replaces the configured tool list for that request,
    so every non-user tool declared by the stored spec is reconstructed too.
    The user token is accepted only for the bootstrapped shared Gateway whose
    Cognito authorizer issued it.
    """
    if configured_tools is not None:
        # Use the authoritative configuration when replacing the authenticated
        # Gateway, preserving all other resolved Gateway aliases/auth settings.
        result = []
        for tool in configured_tools:
            gateway_arn = (tool.get("config", {}).get("agentCoreGateway") or {}).get(
                "gatewayArn"
            )
            if (
                tool.get("type") == "agentcore_gateway"
                and gateway_arn
                and gateway_arn == resources.get("gateway_arn")
            ):
                if not resources.get("gateway_url"):
                    raise ValueError("authenticated user Gateway URL is missing")
                result.append({
                    "type": "remote_mcp",
                    "name": USER_GATEWAY_ALIAS,
                    "config": {"remoteMcp": {
                        "url": str(resources["gateway_url"]),
                        "headers": {"Authorization": f"Bearer {access_token}"},
                    }},
                })
            else:
                result.append(tool)
        return result

    result: list[dict[str, Any]] = []
    attached_user_gateway = False
    configured_gateway_id = str(resources.get("gateway_id") or "")
    for raw in spec.get("tools") or []:
        if not isinstance(raw, Mapping):
            continue
        tool_type = str(raw.get("type") or "")
        name = str(raw.get("name") or "")
        config = raw.get("config") if isinstance(raw.get("config"), Mapping) else {}
        if tool_type == "builtin" and name in BUILTIN_TOOL_TYPES:
            result.append({"type": BUILTIN_TOOL_TYPES[name], "name": name})
        elif tool_type == "mcp" and config.get("url"):
            result.append(
                {
                    "type": "remote_mcp",
                    "name": name,
                    "config": {"remoteMcp": {"url": str(config["url"])}},
                }
            )
        elif tool_type == "gateway":
            requested_id = str(config.get("gateway_id") or configured_gateway_id)
            if requested_id != configured_gateway_id or not resources.get("gateway_url"):
                raise ValueError(
                    "authenticated user policy identity is configured only for launchpad-gw"
                )
            if not attached_user_gateway:
                result.append(
                    {
                        "type": "remote_mcp",
                        "name": USER_GATEWAY_ALIAS,
                        "config": {
                            "remoteMcp": {
                                "url": str(resources["gateway_url"]),
                                "headers": {"Authorization": f"Bearer {access_token}"},
                            }
                        },
                    }
                )
                attached_user_gateway = True

    if spec.get("knowledge_bases") and resources.get("kb_gateway_arn"):
        result.append(
            {
                "type": "agentcore_gateway",
                "name": "launchpad_kb_gw",
                "config": {
                    "agentCoreGateway": {
                        "gatewayArn": str(resources["kb_gateway_arn"]),
                        "outboundAuth": {
                            "oauth": {
                                "providerArn": str(resources["oauth_provider_arn"]),
                                "grantType": "CLIENT_CREDENTIALS",
                                "scopes": ["launchpad-gw/invoke"],
                            }
                        },
                    }
                },
            }
        )
    return result


TOOL_INPUT_MAX_CHARS = 600


def bounded_tool_input(raw: str) -> str:
    """The recorded form of a tool call's arguments: the joined input JSON, whitespace
    collapsed, cut at ``TOOL_INPUT_MAX_CHARS`` with an explicit marker. Bounded because
    it lands in the ledger and on the wire; never parsed, so a partial JSON tail is
    harmless."""
    text = " ".join(raw.split())
    if len(text) <= TOOL_INPUT_MAX_CHARS:
        return text
    return text[:TOOL_INPUT_MAX_CHARS] + f"… [+{len(text) - TOOL_INPUT_MAX_CHARS} chars]"


def _stop_error(reason: Any) -> AppError | None:
    """Decode service stops, including the exported ExecutionLimitsHook messages."""
    normalized = reason.strip().lower() if isinstance(reason, str) else ""
    if normalized in {"end_turn", "stop_sequence", "tool_use", "tool_result"}:
        return None
    code, message, status = (
        "harness.incomplete_response",
        "Harness execution stopped without a complete response",
        502,
    )
    if normalized == "timeout_exceeded" or normalized.startswith("timeout exceeded:"):
        code, message, status = (
            "harness.execution_timeout", "Harness execution timed out", 504,
        )
    elif normalized in {"cancelled", "canceled", "interrupted"}:
        code, message = "harness.execution_cancelled", "Harness execution was cancelled"
    elif normalized in {
        "max_tokens", "max_iterations", "execution_limit_exceeded",
        "max_iterations_exceeded", "max_output_tokens_exceeded", "model_context_window_exceeded",
        "limit_turns", "limit_output_tokens", "limit_total_tokens",
    } or normalized.startswith(("max iterations exceeded:", "max output tokens exceeded:")):
        code, message = "harness.execution_limit", "Harness execution reached its limit"
    return AppError(code, message, {"stop_reason": reason}, status_code=status)


def iter_harness_stream(
    stream: Any, *, on_stream: Any = None, allow_handoff: bool = False,
) -> Iterator[dict[str, Any]]:
    """Drain and validate raw Harness events, always releasing the transport.

    A model's end_turn can precede a watchdog timeout. Cancellation can likewise
    precede the outer timeout_exceeded stop. Keep reading to preserve that final
    diagnosis; once a failure is seen, later text cannot make the call successful.
    ``allow_handoff``: a final ``tool_use`` stop is the inline-function handoff (the
    caller owes a ``toolResult``), not an unfinished response.
    """
    failure: AppError | None = None
    last_stop: Any = None
    has_text = False
    try:
        if on_stream is not None:
            on_stream(stream)
        for event in stream:
            if "messageStop" in event:
                last_stop = event["messageStop"].get("stopReason")
                error = _stop_error(last_stop)
                if error is not None and (
                    failure is None or error.code == "harness.execution_timeout"
                ):
                    failure = error
            elif "runtimeClientError" in event:
                raise RuntimeError(f"runtime client error: {event['runtimeClientError']}")
            elif "internalServerException" in event:
                raise RuntimeError(f"internal server error: {event['internalServerException']}")
            text = event.get("contentBlockDelta", {}).get("delta", {}).get("text")
            if isinstance(text, str) and text.strip():
                has_text = True
            if failure is None:
                yield event
        if failure is not None:
            raise failure
        # Preserve the existing text-only stream form when there is no stop event;
        # an empty stream or an explicitly unfinished tool cycle is never success.
        if allow_handoff and last_stop == "tool_use":
            return
        if not has_text or last_stop in {"tool_use", "tool_result"}:
            raise AppError(
                "harness.incomplete_response",
                "Harness execution ended without a complete text response",
                {"stop_reason": last_stop},
                status_code=502,
            )
    except Exception as exc:
        if failure is not None and exc is not failure:
            raise failure from exc
        raise
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - best effort on teardown
                pass


def invoke_harness_events(
    client: Any,
    harness_arn: str,
    messages: list[dict[str, Any]],
    *,
    session_id: str,
    actor_id: str,
    on_stream: Any = None,
    runtime_user_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    allowed_tools: list[str] | None = None,
    inline_tools: frozenset[str] = frozenset(),
    qualifier: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream one ``InvokeHarness`` call whose ``messages`` carries a bounded
    replayed conversation (``[{role: user|assistant, content: [{text}]}]``, the
    2023-06-05 model's ``ConversationMessage`` list). Yields the same ``tool`` /
    ``delta`` events the chat chain uses; runtime errors raise.

    ``inline_tools`` names client-executed ``inline_function`` tools: when the stream
    stops at ``tool_use`` on one of them, the last event is ``handoff`` with every
    pending call's complete input. The caller answers each with a ``toolResult`` in a
    follow-up call on the SAME session and the same tool overrides (harness-tools
    devguide, "Inline function calls")."""
    overrides: dict[str, Any] = {}
    if runtime_user_id:
        overrides["runtimeUserId"] = runtime_user_id
    if tools is not None:
        overrides["tools"] = tools
    if allowed_tools is not None:
        overrides["allowedTools"] = allowed_tools
    if qualifier:
        overrides["qualifier"] = qualifier
    response = client.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        actorId=actor_id,
        messages=messages,
        **overrides,
    )
    stream = iter_harness_stream(response["stream"], on_stream=on_stream,
                                 allow_handoff=bool(inline_tools))
    with closing(stream) as events:
        # toolUse input arrives as partial-JSON deltas per content block; the joined,
        # bounded text is emitted once at the block's stop so the ledger can record
        # *what* a tool was asked (which file was read, what was searched)
        pending: dict[int, dict[str, Any]] = {}
        handoff: list[dict[str, Any]] = []
        last_stop: Any = None
        for event in events:
            if "messageStop" in event:
                last_stop = event["messageStop"].get("stopReason")
            if "contentBlockStart" in event:
                tool_use = event["contentBlockStart"].get("start", {}).get("toolUse")
                if tool_use:
                    index = event["contentBlockStart"].get("contentBlockIndex")
                    if index is not None:
                        pending[index] = {"name": tool_use.get("name", ""),
                                          "id": tool_use.get("toolUseId"), "chunks": []}
                    yield {
                        "event": "tool",
                        "data": {"name": tool_use.get("name", ""),
                                 "id": tool_use.get("toolUseId")},
                    }
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if delta.get("text"):
                    yield {"event": "delta", "data": {"text": delta["text"]}}
                index = event["contentBlockDelta"].get("contentBlockIndex")
                partial = (delta.get("toolUse") or {}).get("input")
                if partial and index in pending:
                    pending[index]["chunks"].append(str(partial))
            elif "contentBlockStop" in event:
                done = pending.pop(event["contentBlockStop"].get("contentBlockIndex"), None)
                if done is not None:
                    full = "".join(done["chunks"])
                    if done["name"] in inline_tools:
                        handoff.append({"id": done["id"], "name": done["name"], "input": full})
                    yield {
                        "event": "tool_input",
                        "data": {"name": done["name"], "id": done["id"],
                                 "input": bounded_tool_input(full)},
                    }
        if inline_tools and last_stop == "tool_use":
            if not handoff:
                raise AppError(
                    "harness.incomplete_response",
                    "Harness execution ended without a complete text response",
                    {"stop_reason": last_stop}, status_code=502,
                )
            yield {"event": "handoff", "data": {"calls": handoff}}


def invoke_harness_text(
    client: Any,
    harness_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    *,
    runtime_user_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    allowed_tools: list[str] | None = None,
    qualifier: str | None = None,
) -> dict[str, Any]:
    """Synchronous invoke: send one user message, drain the event stream,
    return the concatenated assistant text plus session id. ``qualifier`` pins a
    named endpoint (a canary's control endpoint); omitted ⇒ DEFAULT."""
    session_id = session_id or new_session_id()
    text_parts: list[str] = []
    overrides: dict[str, Any] = {}
    if qualifier:
        overrides["qualifier"] = qualifier
    if runtime_user_id:
        overrides["runtime_user_id"] = runtime_user_id
    if tools is not None:
        overrides["tools"] = tools
    if allowed_tools is not None:
        overrides["allowed_tools"] = allowed_tools
    with closing(invoke_harness_events(
        client, harness_arn, [{"role": "user", "content": [{"text": prompt}]}],
        session_id=session_id, actor_id=actor_id,
        **overrides,
    )) as events:
        for event in events:
            if event["event"] == "delta":
                text_parts.append(event["data"]["text"])
    return {"text": "".join(text_parts), "session_id": session_id}
