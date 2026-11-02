"""Thin wrappers over the Harness control/data APIs.

Every function takes an explicit client so tests inject stubs that capture
kwargs. Payload shapes follow bedrock-agentcore-control 1.43.x.
"""

import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Any

from app.core.errors import AppError, is_not_found

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


# a sentinel for "the platform says nothing about the network": keep the live one
KEEP_NETWORK: Any = object()


def network_configuration(vpc: Mapping[str, Any] | None) -> dict[str, Any]:
    """``agentCoreRuntimeEnvironment.networkConfiguration`` for a resolved network
    profile (design §12.6), None = PUBLIC.

    UNVERIFIED: the harness's backing-runtime environment is assumed to take the
    runtime's ``NetworkConfiguration`` shape (``networkMode`` +
    ``networkModeConfig{subnets, securityGroups}``) — GetHarness echoes the member
    under that name, but no VPC harness has been created live yet."""
    from app.services.agentcore.runtime import _network_configuration

    return _network_configuration(dict(vpc) if vpc else None)


def environment_for_create(
    session_storage: list[dict[str, Any]], vpc: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """CreateHarness ``environment``: the session storage plus, for a VPC agent, its
    network. A PUBLIC agent with no storage omits the member (the service default)."""
    request: dict[str, Any] = {}
    if session_storage:
        request["filesystemConfigurations"] = list(session_storage)
    if vpc:
        request["networkConfiguration"] = network_configuration(vpc)
    return {"agentCoreRuntimeEnvironment": request} if request else None


def _mount_path(entry: Mapping[str, Any]) -> str:
    """The ``mountPath`` of one filesystemConfigurations union member ('' if none)."""
    for member in entry.values():
        if isinstance(member, Mapping) and member.get("mountPath"):
            return str(member["mountPath"]).rstrip("/")
    return ""


def environment_for_update(
    live: Mapping[str, Any], platform_entries: list[dict[str, Any]],
    network: Any = KEEP_NETWORK, *, managed_paths: Iterable[str] = (),
    on_conflict: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """UpdateHarness ``environment`` that sets the platform-owned mounts and keeps
    everything else the live harness carries.

    UpdateHarness replaces ``filesystemConfigurations`` wholesale (``[]`` detaches
    every mount; an omitted ``environment`` keeps the old one — probed live), so the
    live list is read back and merged by ownership (design storage-mounts §3.2):
    ``platform_entries`` (the spec's session storage + S3 Files / EFS mounts,
    `deployer/filesystem.py`) are sent as they are; a live entry is the platform's —
    and dropped — when it is session storage, sits at one of the spec's mount paths,
    or at one of ``managed_paths`` (mount paths an earlier publish of this agent
    set, so a mount removed from the spec is detached). Any other live entry — an
    EFS / S3 Files mount made outside the platform (an imported harness) — survives
    a re-publish, as do the network/lifecycle settings. A live entry at a spec path
    that the platform never set but that differs from the spec's is a conflict: the
    spec wins and ``on_conflict`` gets one line for the deploy log.

    ``network`` is the resolved VPC (`deployer/network.spec_vpc`) when the platform
    owns it: a dict sets VPC mode, None sets PUBLIC (a re-publish that moved the agent
    off its profile); ``KEEP_NETWORK`` (the default) keeps the live value."""
    env = (live.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
    request = {k: env[k] for k in _ENVIRONMENT_REQUEST_FIELDS if env.get(k)}
    spec_paths = {_mount_path(fs): fs for fs in platform_entries}
    managed = {str(p).rstrip("/") for p in managed_paths}
    kept = []
    for fs in env.get("filesystemConfigurations") or []:
        if "sessionStorage" in fs:
            continue
        path = _mount_path(fs)
        if path in spec_paths:
            if path not in managed and fs != spec_paths[path] and on_conflict is not None:
                on_conflict(f"mount {path}: the live harness's entry (set outside the "
                            "platform) is replaced by the spec's")
            continue
        if path in managed:
            continue
        kept.append(fs)
    request["filesystemConfigurations"] = [*platform_entries, *kept]
    if network is not KEEP_NETWORK:
        request["networkConfiguration"] = network_configuration(network)
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


def skill_s3_uris(harness: Mapping[str, Any]) -> list[str]:
    """The ``{"s3": {"uri": …}}`` sources of a GetHarness ``skills`` list (path, git
    and AWS-skills members are not S3 objects)."""
    out: list[str] = []
    for skill in harness.get("skills") or []:
        uri = ((skill or {}).get("s3") or {}).get("uri") if isinstance(skill, Mapping) else None
        if uri:
            out.append(str(uri))
    return out


def _within(uri: str, prefix: str) -> bool:
    """``uri`` names the prefix itself or something inside it (a legacy spec may
    carry the ``…/SKILL.md`` file path)."""
    base = prefix.rstrip("/") + "/"
    return uri.rstrip("/") + "/" == base or uri.startswith(base)


# The account-wide reference scan before a Skill bundle is deleted, in the style of
# agent_artifacts._referenced_by_other_runtimes (bounded fan-out, fail closed, skip what
# is being deleted). The Harness read APIs have low per-account rates
# (ListHarnessEndpoints throttled a 19-Harness scan on dev, 2026-11-02), so the pool is
# smaller and a throttled call backs off briefly beyond the SDK's own retries.
SKILL_SCAN_WORKERS = 4
SCAN_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
_THROTTLED = frozenset({"ThrottlingException", "TooManyRequestsException"})


def _error_code(exc: BaseException) -> str:
    code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")
    return str(code or type(exc).__name__)


def _patient(call: Callable[[], Any]) -> Any:
    for delay in (*SCAN_RETRY_DELAYS_S, None):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if _error_code(exc) not in _THROTTLED or delay is None:
                raise
            time.sleep(delay)  # nosemgrep: arbitrary-sleep
    raise AssertionError("unreachable")  # pragma: no cover


def _harness_mounts(client: Any, summary: Mapping[str, Any], prefix: str) -> bool:
    """Whether one Harness reads ``prefix``: its latest config, or an older version a
    named endpoint (a gated agent's ``live``, a canary) still serves — looked up only
    when an older version exists. A Harness that vanished since the list does not
    count; any other failure propagates."""
    harness_id = str(summary.get("harnessId") or "")
    try:
        latest = _patient(lambda: get_harness(client, harness_id))
        if latest.get("status") == "DELETING":
            return False  # being deleted since the listing: it never starts a session again
        if any(_within(uri, prefix) for uri in skill_s3_uris(latest)):
            return True
        latest_version = str(latest.get("harnessVersion") or "")
        if latest_version == "1":
            return False  # nothing older for an endpoint to serve
        served = {
            str(v) for e in _patient(lambda: list_harness_endpoints(client, harness_id))
            for v in (e.get("liveVersion"), e.get("targetVersion")) if v
        } - {latest_version}
        for version in sorted(served):
            older = _patient(lambda v=version: get_harness(client, harness_id, v))
            if any(_within(uri, prefix) for uri in skill_s3_uris(older)):
                return True
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        if is_not_found(exc):
            return False  # deleted since the listing
        raise
    return False


def harnesses_mounting_prefix(
    client: Any, prefix: str, *, workers: int = SKILL_SCAN_WORKERS,
) -> list[str]:
    """Names of every Harness in the client's account and region whose skills read
    the S3 ``prefix`` — not only this hub's: hubs sharing an account and region (prod
    and its clone) share the artifacts bucket, and a Harness loads its Skills from S3
    at session start. A Harness being deleted is skipped. GetHarness runs on a
    bounded pool (``workers``); any failure (AccessDenied, throttling past the SDK's
    retries) raises, so the caller keeps the files rather than guess."""
    summaries = [
        h for h in _patient(lambda: list_harnesses(client))
        if h.get("harnessId") and h.get("status") != "DELETING"
    ]
    if not summaries:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(summaries)))) as pool:
        mounts = list(pool.map(lambda h: _harness_mounts(client, h, prefix), summaries))
    return sorted(
        str(h.get("harnessName") or h.get("harnessId"))
        for h, hit in zip(summaries, mounts, strict=True) if hit
    )


def delete_harness(client: Any, harness_id: str) -> None:
    client.delete_harness(harnessId=harness_id)


# DeleteHarness is asynchronous: the harness stays DELETING (and keeps its name) for a
# minute or two after the console row is gone and its name released
NAME_RELEASE_TIMEOUT_S = 300
NAME_RELEASE_POLL_S = 10


def _is_name_conflict(exc: Exception) -> bool:
    return type(exc).__name__ == "ConflictException" and "already exists" in str(exc)


def create_after_name_release(
    client: Any,
    create: Callable[[], dict[str, Any]],
    harness_name: str,
    log: Callable[[str], None],
    timeout_s: float = NAME_RELEASE_TIMEOUT_S,
    poll_s: float = NAME_RELEASE_POLL_S,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run ``create`` (CreateHarness), waiting out a same-name harness that is still
    being deleted.

    Deleting an agent releases its console name at once while AWS takes a while to let
    go of the harness, so re-creating the name right away is refused with "already
    exists". Only that window is waited out: a conflict whose holder is a live
    (not DELETING) harness is a real name collision and is raised immediately."""
    deadline = clock() + timeout_s
    while True:
        try:
            return create()
        except Exception as exc:
            if not _is_name_conflict(exc):
                raise
            holder = next(
                (h for h in list_harnesses(client) if h.get("harnessName") == harness_name),
                None,
            )
            # absent from the list but still refused: the delete is just finishing
            if holder is not None and holder.get("status") != "DELETING":
                raise
            if clock() + poll_s > deadline:
                raise
            log(
                f"harness name {harness_name} is still held by a harness being deleted "
                f"— retrying CreateHarness in {poll_s:g}s"
            )
            sleeper(poll_s)


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
    workspace_id: str = "",
    user_gateways: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Harness invocation tools with launchpad-gw authenticated as one user.

    ``workspace_id`` (when given) lets launchpad-kb-gw take the current project's
    provider and scopes (`project_gateway.effective_resources`, projects §10.6).

    ``InvokeHarness.tools`` replaces the configured tool list for that request,
    so every non-user tool declared by the stored spec is reconstructed too.
    The user token is accepted only for the bootstrapped shared Gateway whose
    Cognito authorizer issued it, and for ``user_gateways`` (Gateway ARN -> MCP
    URL): other configured Gateways the caller has verified trust the same pool
    and clients (app.services.user_gateways). Those keep their configured alias,
    so allowedTools selectors written against it still match.
    """
    extra = dict(user_gateways or {})
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
            elif tool.get("type") == "agentcore_gateway" and gateway_arn in extra:
                result.append({
                    "type": "remote_mcp",
                    "name": tool["name"],
                    "config": {"remoteMcp": {
                        "url": str(extra[gateway_arn]),
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
        from app.core.project_scope import current_project
        from app.services import project_gateway

        kb_auth = project_gateway.effective_resources(
            dict(resources), current_project(), workspace_id or None)
        result.append(
            {
                "type": "agentcore_gateway",
                "name": "launchpad_kb_gw",
                "config": {
                    "agentCoreGateway": {
                        "gatewayArn": str(resources["kb_gateway_arn"]),
                        "outboundAuth": {
                            "oauth": {
                                "providerArn": str(kb_auth["oauth_provider_arn"]),
                                "grantType": "CLIENT_CREDENTIALS",
                                "scopes": project_gateway.gateway_scopes(kb_auth),
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


CONTEXT_WINDOW_STOPS = frozenset({"model_context_window_exceeded", "context_window_exceeded"})


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
    elif normalized in CONTEXT_WINDOW_STOPS:
        # the turn's own tool results overflowed the model's context window: distinct
        # from a budget limit, a replay with less history can succeed
        code, message = (
            "harness.context_window_exceeded",
            "Harness execution exceeded the model's context window",
        )
    elif normalized in {
        "max_tokens", "max_iterations", "execution_limit_exceeded",
        "max_iterations_exceeded", "max_output_tokens_exceeded",
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
