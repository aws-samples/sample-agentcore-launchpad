"""Thin wrappers over the AgentCore Runtime control/data APIs.

Explicit-client style (tests inject stubs). Shapes per bedrock-agentcore-control
1.43.x: runtime status enum is CREATING → READY (or CREATE_FAILED).
"""

import ast
import json
import logging
import re
import time
import urllib.parse
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from app.core.errors import AppError
from app.services.agentcore.filesystem_kinds import needs_vpc
from app.services.agentcore.harness import new_session_id

logger = logging.getLogger("launchpad.agentcore.runtime")

TERMINAL_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED"}
SSE_READ_CHUNK_BYTES = 32

# Bearer (JWT) invocation of the Runtime data plane. boto3 cannot send a
# bearer token, so JWT-mode runtimes are invoked over plain HTTPS (devguide
# "Authenticate and authorize with Inbound Auth and Outbound Auth", read
# 2026-09-19): POST https://bedrock-agentcore.{region}.amazonaws.com
# /runtimes/{urlencoded runtime ARN}/invocations?qualifier=... with
# Authorization: Bearer and the session-id header below.
BEARER_SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
BEARER_READ_TIMEOUT_S = 900.0


class RuntimeBearerAuthError(RuntimeError):
    """The Runtime's JWT authorizer rejected the request (401/403).

    Distinct from a generic runtime error so the invoke layer can answer with
    a token/client/claim hint instead of a bare 502."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RuntimeBearerHttpError(RuntimeError):
    """A bearer invoke answered a non-200 status other than 401/403.

    Typed so a retry policy can tell an upstream 5xx / 429 (transient) from a
    4xx (final) without parsing the message; the message keeps the historical
    ``bearer invoke returned HTTP <status>`` text."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _protocol_configuration(protocol: str | None) -> dict[str, Any] | None:
    """protocolConfiguration param, or None for the HTTP default.

    NB (probed live): UpdateAgentRuntime treats an omitted protocolConfiguration
    as a RESET to HTTP — every update path must echo the agent's protocol.
    """
    if not protocol or protocol == "http":
        return None
    return {"serverProtocol": protocol.upper()}


def create_code_runtime(
    client: Any,
    *,
    runtime_name: str,
    s3_bucket: str,
    s3_key: str,
    role_arn: str,
    environment: dict[str, str] | None = None,
    protocol: str | None = None,
    python_version: str | None = None,
    entrypoint: str | None = None,
    instrument: bool = True,
    authorizer_configuration: dict[str, Any] | None = None,
    filesystem_configurations: list[dict[str, Any]] | None = None,
    vpc: dict[str, Any] | None = None,
    tags: dict[str, str] | None = None,
    platform_version: str | None = None,
    resource_configurations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """CreateAgentRuntime from a zip on S3, instrumented via ADOT.

    ``python_version``/``entrypoint`` default to the platform artifact contract
    (PYTHON_3_13, main.py); BYOC passes the user's choices through and keeps
    the ADOT launcher only when its bundle ships one (user zips don't always).
    ``authorizer_configuration`` is the inbound JWT authorizer
    ({"customJWTAuthorizer": ...}); None means IAM/SigV4 (the field is omitted).
    ``vpc`` = the spec's resolved network profile ``{subnets, security_groups}``
    (`network_profiles.resolve_vpc`); None = PUBLIC. ``filesystem_configurations``
    (`deployer/filesystem.py`) is the session storage plus any S3 Files / EFS
    mounts; the latter need ``vpc`` (storage-mounts §3.2). ``tags`` are added to
    the `launchpad:*` project tags the environment's span attributes imply
    (`_runtime_tags`). ``platform_version`` and ``resource_configurations`` are
    optional passthroughs: callers without them (workers/probes) keep AWS defaults.
    An update preserves an omitted platform but resets omitted resources to
    consumption; agent deployers resolve preservation before calling here."""
    _check_mounts_network(filesystem_configurations, vpc)
    params: dict[str, Any] = {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": _code_artifact(
            s3_bucket, s3_key, python_version, entrypoint, instrument
        ),
        "networkConfiguration": _network_configuration(vpc),
        "roleArn": role_arn,
    }
    if environment:
        params["environmentVariables"] = dict(environment)
    proto = _protocol_configuration(protocol)
    if proto:
        params["protocolConfiguration"] = proto
    if authorizer_configuration:
        params["authorizerConfiguration"] = authorizer_configuration
    if platform_version is not None:
        params["platformVersion"] = platform_version
    if resource_configurations is not None:
        params["resourceConfigurations"] = resource_configurations
    if filesystem_configurations:
        params["filesystemConfigurations"] = filesystem_configurations
    if resource_tags := _runtime_tags(environment, tags):
        params["tags"] = resource_tags
    return client.create_agent_runtime(**params)


def _runtime_tags(
    environment: dict[str, str] | None, tags: dict[str, str] | None = None
) -> dict[str, str]:
    """CreateAgentRuntime / TagResource ``tags``: the unified cost-tag set of every
    runtime (cost-tags design §1.1, `cost_tags.runtime_tags`) — the project the
    environment's span attributes name (an isolated project's agent, projects design
    §10.8) or else the deploy's current project (`shared` when none), then the
    caller's explicit ``tags`` (the deployer's agent tags). The ``tags`` member is
    in the service model (botocore 1.43); unverified live."""
    from app.services.cost_tags import runtime_tags

    return runtime_tags(environment, tags)


def _tag_updated_runtime(
    client: Any, response: dict[str, Any], environment: dict[str, str] | None,
    tags: dict[str, str] | None = None,
) -> None:
    """UpdateAgentRuntime takes no tags, so a re-publish tags the runtime with
    TagResource — a runtime created before cost tags (or before its project was
    isolated, or transferred since) catches up on its next publish. Best-effort: a
    refusal is logged, never fails the deploy."""
    resource_tags = _runtime_tags(environment, tags)
    if not resource_tags:
        return
    arn = (response or {}).get("agentRuntimeArn")
    if not arn:
        return
    try:
        client.tag_resource(resourceArn=arn, tags=resource_tags)
    except Exception as exc:  # noqa: BLE001 — attribution must not fail a publish
        logger.warning("TagResource on %s failed: %s", arn, exc)


def _byo_entries(filesystem_configurations: list[dict[str, Any]] | None) -> bool:
    """Any entry that mounts over NFS (S3 Files / EFS). An ``s3BucketMount`` and the
    managed session storage work on PUBLIC."""
    return any(needs_vpc(fs) for fs in filesystem_configurations or [])


def _check_mounts_network(
    filesystem_configurations: list[dict[str, Any]] | None, vpc: dict[str, Any] | None
) -> None:
    """S3 Files / EFS mounts are reached from the runtime's ENIs, so they need VPC
    mode (design storage-mounts §3.2). `AgentSpec` already refuses the combination;
    this keeps any other caller from sending a request the service rejects late."""
    if _byo_entries(filesystem_configurations) and not vpc:
        raise ValueError(
            "S3 Files / EFS mounts need the runtime in a VPC — give the agent a "
            "network profile"
        )


def _network_configuration(vpc: dict[str, Any] | None) -> dict[str, Any]:
    """The one place a runtime's networkConfiguration is built (design §12.6,
    §12.13): PUBLIC when the spec names no network; VPC mode with the resolved
    profile's subnets / security groups otherwise (also what BYO file systems —
    S3 Files / EFS access points — require)."""
    if not vpc:
        return {"networkMode": "PUBLIC"}
    return {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": list(vpc["subnets"]),
            "securityGroups": list(vpc["security_groups"]),
        },
    }


def create_container_runtime(
    client: Any,
    *,
    runtime_name: str,
    container_uri: str,
    role_arn: str,
    environment: dict[str, str] | None = None,
    filesystem_configurations: list[dict[str, Any]] | None = None,
    vpc: dict[str, Any] | None = None,
    lifecycle: dict[str, int] | None = None,
    authorizer_configuration: dict[str, Any] | None = None,
    protocol: str | None = None,
    tags: dict[str, str] | None = None,
    platform_version: str | None = None,
    resource_configurations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """CreateAgentRuntime from an ECR image (Claude SDK container path).

    ``lifecycle`` = {idleRuntimeSessionTimeout, maxLifetime} seconds (the Skill
    Lab exec worker caps sessions at 8h and reaps idle ones at 5min).
    ``protocol`` and ``tags`` as ``create_code_runtime`` (None / "http" = the HTTP
    default)."""
    _check_mounts_network(filesystem_configurations, vpc)
    params: dict[str, Any] = {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": container_uri}
        },
        "networkConfiguration": _network_configuration(vpc),
        "roleArn": role_arn,
    }
    if environment:
        params["environmentVariables"] = dict(environment)
    if filesystem_configurations:
        params["filesystemConfigurations"] = filesystem_configurations
    if lifecycle:
        params["lifecycleConfiguration"] = dict(lifecycle)
    proto = _protocol_configuration(protocol)
    if proto:
        params["protocolConfiguration"] = proto
    if authorizer_configuration:
        params["authorizerConfiguration"] = authorizer_configuration
    if platform_version is not None:
        params["platformVersion"] = platform_version
    if resource_configurations is not None:
        params["resourceConfigurations"] = resource_configurations
    if resource_tags := _runtime_tags(environment, tags):
        params["tags"] = resource_tags
    return client.create_agent_runtime(**params)


def _code_artifact(
    s3_bucket: str,
    s3_key: str,
    python_version: str | None = None,
    entrypoint: str | None = None,
    instrument: bool = True,
) -> dict[str, Any]:
    """``instrument=False`` drops the opentelemetry-instrument launcher — BYOC
    zips only carry it when the member's own requirements install the distro,
    and an absent launcher fails the runtime at start."""
    entry = [entrypoint or "main.py"]
    if instrument:
        entry.insert(0, "opentelemetry-instrument")
    return {
        "codeConfiguration": {
            "code": {"s3": {"bucket": s3_bucket, "prefix": s3_key}},
            "runtime": python_version or "PYTHON_3_13",
            "entryPoint": entry,
        }
    }


def update_code_runtime(
    client: Any,
    *,
    runtime_id: str,
    s3_bucket: str,
    s3_key: str,
    role_arn: str,
    environment: dict[str, str] | None = None,
    protocol: str | None = None,
    python_version: str | None = None,
    entrypoint: str | None = None,
    instrument: bool = True,
    authorizer_configuration: dict[str, Any] | None = None,
    filesystem_configurations: list[dict[str, Any]] | None = None,
    vpc: dict[str, Any] | None = None,
    tags: dict[str, str] | None = None,
    platform_version: str | None = None,
    resource_configurations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """UpdateAgentRuntime with a new zip artifact — publishes a new version in
    place (same agentRuntimeId/ARN; the DEFAULT endpoint auto-rolls to it).

    ``protocol`` must be passed for A2A agents on EVERY update — the service
    resets an omitted protocolConfiguration back to HTTP (probed live). The same
    holds for ``filesystem_configurations``: an omitted list detaches every mount
    (probed live 2026-10-04), so a re-publish must echo it each time. The
    authorizer rides the same call: every update passes the RESOLVED inbound
    auth — echoing the JWT config keeps (or sets) it, omitting it switches the
    runtime back to IAM/SigV4, which is also how JWT→IAM transitions are
    performed (CloudFormation marks authorizer updates "No interruption").
    The network rides it too (design §12.6): every update sends the spec's
    resolved ``vpc`` (None = PUBLIC), so a changed network profile republishes
    in place. Whether the service accepts PUBLIC→VPC (or a subnet change) on
    UpdateAgentRuntime is unverified live; a refusal surfaces as the update's
    error."""
    _check_mounts_network(filesystem_configurations, vpc)
    params: dict[str, Any] = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": _code_artifact(
            s3_bucket, s3_key, python_version, entrypoint, instrument
        ),
        "networkConfiguration": _network_configuration(vpc),
        "roleArn": role_arn,
    }
    if environment is not None:
        params["environmentVariables"] = dict(environment)
    proto = _protocol_configuration(protocol)
    if proto:
        params["protocolConfiguration"] = proto
    if authorizer_configuration:
        params["authorizerConfiguration"] = authorizer_configuration
    if platform_version is not None:
        params["platformVersion"] = platform_version
    if resource_configurations is not None:
        params["resourceConfigurations"] = resource_configurations
    if filesystem_configurations:
        params["filesystemConfigurations"] = filesystem_configurations
    response = client.update_agent_runtime(**params)
    _tag_updated_runtime(client, response, environment, tags)
    return response


def update_container_runtime(
    client: Any,
    *,
    runtime_id: str,
    container_uri: str,
    role_arn: str,
    environment: dict[str, str] | None = None,
    filesystem_configurations: list[dict[str, Any]] | None = None,
    vpc: dict[str, Any] | None = None,
    lifecycle: dict[str, int] | None = None,
    authorizer_configuration: dict[str, Any] | None = None,
    protocol: str | None = None,
    tags: dict[str, str] | None = None,
    platform_version: str | None = None,
    resource_configurations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """UpdateAgentRuntime with a new container image — new version, same ARN.
    NB: a version bump resets managed session storage (documented UI note).
    Authorizer semantics as ``update_code_runtime``: echo to keep, omit to
    return the runtime to IAM/SigV4. ``protocol`` must be echoed too: an omitted
    protocolConfiguration resets the runtime to HTTP."""
    _check_mounts_network(filesystem_configurations, vpc)
    params: dict[str, Any] = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": container_uri}},
        "networkConfiguration": _network_configuration(vpc),
        "roleArn": role_arn,
    }
    if environment is not None:
        params["environmentVariables"] = dict(environment)
    if filesystem_configurations:
        params["filesystemConfigurations"] = filesystem_configurations
    if lifecycle:
        params["lifecycleConfiguration"] = dict(lifecycle)
    proto = _protocol_configuration(protocol)
    if proto:
        params["protocolConfiguration"] = proto
    if authorizer_configuration:
        params["authorizerConfiguration"] = authorizer_configuration
    if platform_version is not None:
        params["platformVersion"] = platform_version
    if resource_configurations is not None:
        params["resourceConfigurations"] = resource_configurations
    response = client.update_agent_runtime(**params)
    _tag_updated_runtime(client, response, environment, tags)
    return response


def get_runtime(client: Any, runtime_id: str, *, version: str | None = None) -> dict[str, Any]:
    """Read current configuration, or a nominated immutable Runtime version."""
    params = {"agentRuntimeId": runtime_id}
    if version is not None:
        params["agentRuntimeVersion"] = version
    return client.get_agent_runtime(**params)


def list_runtimes(client: Any) -> list[dict[str, Any]]:
    """Return every Runtime summary across all ListAgentRuntimes pages."""
    runtimes: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"maxResults": 100}
    while True:
        page = client.list_agent_runtimes(**kwargs)
        runtimes.extend(page.get("agentRuntimes", []))
        token = page.get("nextToken")
        if not token:
            return runtimes
        kwargs["nextToken"] = token


def delete_runtime(client: Any, runtime_id: str) -> None:
    client.delete_agent_runtime(agentRuntimeId=runtime_id)


def wait_runtime_ready(
    client: Any,
    runtime_id: str,
    timeout_s: int = 1200,
    interval_s: int = 10,
    sleeper: Any = time.sleep,
    on_status: Any = None,
) -> dict[str, Any]:
    """Poll GetAgentRuntime until READY; runtimes can take 5–15 minutes."""
    deadline = time.monotonic() + timeout_s
    last_status = None
    while True:
        detail = get_runtime(client, runtime_id)
        status = detail["status"]
        if status != last_status and on_status:
            on_status(status)
        last_status = status
        if status == "READY":
            return detail
        if status in TERMINAL_FAILURES:
            reason = detail.get("failureReason", "no failureReason provided")
            raise RuntimeError(f"runtime {runtime_id} entered {status}: {reason}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"runtime {runtime_id} still {status} after {timeout_s}s")
        sleeper(interval_s)


# ─── named endpoints ─────────────────────────────────────────────────────────
# A named endpoint pins a runtime to a specific version (unlike DEFAULT, which
# auto-follows the latest version). Used by the target-based canary to hold a
# stable=v_current / treatment=v_candidate pair on one runtime. Shapes per the
# preview bedrock-agentcore-control API (create uses ``name``; update/get/delete
# use ``endpointName``); keep the defensive .get() reads since detail may drift.


def create_runtime_endpoint(
    client: Any, *, runtime_id: str, endpoint_name: str, version: int | str,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """CreateAgentRuntimeEndpoint — a named endpoint pinned to ``version``, carrying
    the cost tags of the current project (`_runtime_tags`) plus ``tags``."""
    return client.create_agent_runtime_endpoint(
        agentRuntimeId=runtime_id,
        name=endpoint_name,
        agentRuntimeVersion=str(version),
        tags=_runtime_tags(None, tags),
    )


def update_runtime_endpoint(
    client: Any, *, runtime_id: str, endpoint_name: str, version: int | str
) -> dict[str, Any]:
    """UpdateAgentRuntimeEndpoint — re-point a named endpoint at a new version
    (the promote cutover: stable endpoint → the candidate version)."""
    return client.update_agent_runtime_endpoint(
        agentRuntimeId=runtime_id,
        endpointName=endpoint_name,
        agentRuntimeVersion=str(version),
    )


def get_runtime_endpoint(
    client: Any, *, runtime_id: str, endpoint_name: str
) -> dict[str, Any]:
    return client.get_agent_runtime_endpoint(
        agentRuntimeId=runtime_id, endpointName=endpoint_name
    )


def delete_runtime_endpoint(client: Any, *, runtime_id: str, endpoint_name: str) -> None:
    client.delete_agent_runtime_endpoint(
        agentRuntimeId=runtime_id, endpointName=endpoint_name
    )


def list_runtime_versions(client: Any, runtime_id: str) -> list[dict[str, Any]]:
    """Every immutable version of one runtime, across all ListAgentRuntimeVersions
    pages. Summaries carry {agentRuntimeVersion, status, description,
    lastUpdatedAt, …}; the caller projects — nothing here filters."""
    versions: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"agentRuntimeId": runtime_id, "maxResults": 100}
    while True:
        page = client.list_agent_runtime_versions(**kwargs)
        versions.extend(page.get("agentRuntimes", []))
        token = page.get("nextToken")
        if not token:
            return versions
        kwargs["nextToken"] = token


def list_runtime_endpoints(client: Any, runtime_id: str) -> list[dict[str, Any]]:
    """Every endpoint of one runtime (DEFAULT + named), across all
    ListAgentRuntimeEndpoints pages. Summaries carry {name, liveVersion,
    targetVersion, status, description, createdAt, lastUpdatedAt, …}."""
    endpoints: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"agentRuntimeId": runtime_id, "maxResults": 100}
    while True:
        page = client.list_agent_runtime_endpoints(**kwargs)
        endpoints.extend(page.get("runtimeEndpoints", []))
        token = page.get("nextToken")
        if not token:
            return endpoints
        kwargs["nextToken"] = token


def wait_endpoint_ready(
    client: Any,
    *,
    runtime_id: str,
    endpoint_name: str,
    timeout_s: int = 600,
    interval_s: int = 5,
    sleeper: Any = time.sleep,
    on_status: Any = None,
) -> dict[str, Any]:
    """Poll GetAgentRuntimeEndpoint until READY (CREATING/UPDATING → READY).

    Mirrors ``wait_runtime_ready``; raises RuntimeError on CREATE_FAILED/
    UPDATE_FAILED and TimeoutError past the deadline."""
    deadline = time.monotonic() + timeout_s
    last_status = None
    while True:
        detail = get_runtime_endpoint(
            client, runtime_id=runtime_id, endpoint_name=endpoint_name
        )
        status = detail.get("status")
        if status != last_status and on_status:
            on_status(status)
        last_status = status
        if status == "READY":
            return detail
        if status in TERMINAL_FAILURES:
            reason = detail.get("failureReason", "no failureReason provided")
            raise RuntimeError(
                f"endpoint {endpoint_name} on {runtime_id} entered {status}: {reason}"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"endpoint {endpoint_name} on {runtime_id} still {status} "
                f"after {timeout_s}s"
            )
        sleeper(interval_s)


def ensure_runtime_endpoint(
    client: Any,
    *,
    runtime_id: str,
    endpoint_name: str,
    version: int | str,
    timeout_s: int = 600,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    """Create — or re-point — a named endpoint at ``version`` and wait until it serves it.

    Idempotent: a ConflictException (the endpoint exists) becomes an update when it
    serves another version. Used by Agent-DLC's `live` / `candidate` endpoints.
    """
    try:
        create_runtime_endpoint(
            client, runtime_id=runtime_id, endpoint_name=endpoint_name, version=version
        )
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
        current = get_runtime_endpoint(client, runtime_id=runtime_id, endpoint_name=endpoint_name)
        if str(current.get("liveVersion") or "") != str(version):
            update_runtime_endpoint(
                client, runtime_id=runtime_id, endpoint_name=endpoint_name, version=version
            )
    deadline = time.monotonic() + timeout_s
    while True:
        detail = wait_endpoint_ready(
            client, runtime_id=runtime_id, endpoint_name=endpoint_name,
            timeout_s=timeout_s, sleeper=sleeper,
        )
        if str(detail.get("liveVersion") or "") == str(version):
            return detail
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"endpoint {endpoint_name} on {runtime_id} did not reach version {version}"
            )
        sleeper(5)


def flatten_sse_text(raw: str) -> str | None:
    """Join the text deltas of an SSE event stream, or None if raw isn't SSE.

    Supports both converted-Harness event envelopes and Launchpad's native
    runtime events.
    """
    if not raw.lstrip().startswith("data:"):
        return None
    parts = [
        event["data"]["text"]
        for event in _normalized_runtime_events(_sse_payloads(raw.splitlines()))
        if event["event"] == "delta"
    ]
    return "".join(parts) or None


def _sse_payloads(lines: Iterable[bytes | str]) -> Iterator[Any]:
    """Decode SSE data fields without buffering beyond one event."""
    data_lines: list[str] = []
    for raw_line in lines:
        line = (
            raw_line.decode("utf-8", errors="replace")
            if isinstance(raw_line, bytes)
            else str(raw_line)
        ).rstrip("\r\n")
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines.clear()
                try:
                    yield json.loads(data)
                except ValueError:
                    yield data
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if data_lines:
        data = "\n".join(data_lines)
        try:
            yield json.loads(data)
        except ValueError:
            yield data


# The AgentCore SDK serializes each yielded event with ``json.dumps`` and, when
# that fails (a reasoning block's ``redactedContent`` is bytes), falls back to
# ``json.dumps(str(event))``: the SSE line then carries the Python repr of a
# Converse stream event as a JSON *string*. Measured 2026-09-27 on a converted
# Runtime twin (Strands + GPT-6): every reply began with that repr as text.
_REPR_EVENT = re.compile(
    r"^\{(?:'event': \{)?'(?:messageStart|messageStop|contentBlockStart|contentBlockDelta"
    r"|contentBlockStop|metadata)': \{"
)
_REPR_EVENT_LIMIT = 1_000_000


def _repr_event(text: str) -> dict[str, Any] | None:
    """The event dict behind a Python-repr fallback string, else None.

    ``ast.literal_eval`` only builds literals (bytes included), never code."""
    if len(text) > _REPR_EVENT_LIMIT or not _REPR_EVENT.match(text):
        return None
    try:
        value = ast.literal_eval(text)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _safe_auth_url(value: Any) -> str | None:
    """An auth_required ``url`` fit to render as a link, else None.

    The runtime's stream is agent-authored (BYOC code, a prompt-injected tool),
    so a ``javascript:`` / ``data:`` / plain-http URL here would reach the
    console's consent card. Only an absolute https URL with a host survives;
    anything else is dropped (the card then has no link) and logged by scheme
    only — the URL itself carries the consent session's state.
    """
    url = str(value or "").strip()
    if not url:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        parts = None
    if parts is not None and parts.scheme.lower() == "https" and parts.hostname:
        return url
    scheme = (parts.scheme if parts is not None else "") or "<none>"
    logger.warning("dropped a non-https auth_required url (scheme %r)", scheme[:16])
    return None


# strands-builder SB-2 approval notices the generated runtime streams
APPROVAL_EVENTS = ("approval_required", "approval_refused")


def _runtime_payload_events(payload: Any) -> Iterator[dict[str, Any]]:
    """Normalize one runtime payload to Chat's tool/delta/complete contract
    (lenient ``str()`` coercion of text fields, shared by Chat, the public API
    and evaluation replays)."""
    if isinstance(payload, str) and _REPR_EVENT.match(payload):
        recovered = _repr_event(payload)
        if recovered is None:
            # A stream event is never reply text, even when it cannot be recovered.
            logger.warning("dropped an unparseable runtime stream event repr (%d chars)",
                           len(payload))
            return
        payload = recovered
    if not isinstance(payload, dict):
        text = str(payload)
        if text:
            yield {"event": "complete", "data": {"text": text}}
        return

    if payload.get("error"):
        raise RuntimeError(f"runtime returned error: {payload['error']}")

    if payload.get("attachment_contract") == "v1":
        yield {"event": "attachments", "data": {"contract": "v1"}}
    kind = payload.get("event")
    if isinstance(kind, str) and (
        kind in {"delta", "heartbeat", "tool", "auth_required", "complete", "error"}
        or kind in APPROVAL_EVENTS
        or (kind == "attachments" and payload.get("contract") == "v1")
    ):
        if kind == "attachments":
            yield {"event": "attachments", "data": {"contract": payload.get("contract")}}
        elif kind == "delta":
            text = payload.get("text")
            if text:
                yield {"event": "delta", "data": {"text": str(text)}}
        elif kind == "heartbeat":
            yield {"event": "heartbeat", "data": {}}
        elif kind == "tool":
            yield {
                "event": "tool",
                "data": {"name": str(payload.get("name", "")), "id": payload.get("id")},
            }
        elif kind == "auth_required":
            # Outbound as_user (USER_FEDERATION 3LO): the tool's Connection holds
            # no token for this user yet. `url` is the IdP authorization URL the
            # user must open; `session_uri` is the consent session the platform
            # completes (CompleteResourceTokenAuth) once the user returns — the
            # invoke layer records it and strips it before the caller sees the
            # event (services/oauth_sessions.py). Never logged. The url comes
            # out of agent code, so only an https URL is passed through (see
            # ``_safe_auth_url``) — the console renders it as a link.
            yield {
                "event": "auth_required",
                "data": {
                    "provider": str(payload.get("provider", "")),
                    "tool": str(payload.get("tool", "")),
                    "url": _safe_auth_url(payload.get("url")),
                    "scopes": [str(s) for s in payload.get("scopes") or []],
                    "session_uri": str(payload.get("session_uri", "")),
                },
            }
        elif kind in APPROVAL_EVENTS:
            # strands-builder SB-2 tool hooks: a tool on the approval list was
            # paused for the console user (approval_required) or refused for a
            # caller that cannot approve (approval_refused). The runtime already
            # masked the input.
            yield {
                "event": kind,
                "data": {
                    "tool": str(payload.get("tool", "")),
                    "tool_use_id": str(payload.get("tool_use_id", "")),
                    "input": payload.get("input") if isinstance(payload.get("input"), dict)
                    else {},
                },
            }
        elif kind == "complete":
            data: dict[str, Any] = {"text": str(payload.get("result", ""))}
            # strands-builder SB-1: an output_schema agent's parsed answer
            if isinstance(payload.get("structured"), (dict, list)):
                data["structured"] = payload["structured"]
            yield {"event": "complete", "data": data}
        elif kind == "error":
            raise RuntimeError(str(payload.get("message", "runtime stream failed")))
        return

    strands_text = _strands_loop_event(payload)
    if strands_text is not None:
        if strands_text:
            yield {"event": "complete", "data": {"text": strands_text}}
        return

    inner = kind if isinstance(kind, dict) else payload
    if "runtimeClientError" in inner or "internalServerException" in inner:
        detail = inner.get("runtimeClientError") or inner.get("internalServerException")
        raise RuntimeError(f"runtime returned error: {detail}")
    converse_event = _is_converse_stream_event(inner)
    if converse_event:
        tool_use = inner.get("contentBlockStart", {}).get("start", {}).get("toolUse")
        if isinstance(tool_use, dict):
            yield {
                "event": "tool",
                "data": {"name": tool_use.get("name", ""), "id": tool_use.get("toolUseId")},
            }
        delta = inner.get("contentBlockDelta", {}).get("delta", {})
        text = delta.get("text")
        if text:
            yield {"event": "delta", "data": {"text": str(text)}}
        usage = _converse_usage(inner.get("metadata"))
        if usage is not None:
            # one model call's tokens; `_normalized_runtime_events` sums them
            yield {"event": "usage", "data": usage}
    if "result" in payload:
        yield {"event": "complete", "data": {"text": str(payload.get("result", ""))}}
    elif not converse_event:
        text = _free_form_payload_text(payload)
        if text:
            yield {"event": "complete", "data": {"text": text}}


# Strands' agent loop also yields its own lifecycle events next to the Converse ones
# when an entrypoint forwards every `stream_async` event (measured 2026-10-14 on an
# AgentCore CLI Strands template that does): bookkeeping flags, and the finished
# `message` / `result` the deltas already carried.
_STRANDS_LOOP_FLAGS = frozenset(
    {"init_event_loop", "start", "start_event_loop", "force_stop", "force_stop_reason"}
)


def _message_text(message: Any) -> str:
    """The text blocks of an assistant Converse message (``{role, content[]}``)."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return ""
    return "".join(
        str(block["text"]) for block in blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _strands_loop_event(payload: dict[str, Any]) -> str | None:
    """``None`` when ``payload`` is not a Strands loop event; otherwise the final
    text it carries ("" for pure bookkeeping). The text only matters when no
    delta was streamed — `_normalized_runtime_events` drops it after deltas."""
    keys = set(payload)
    if keys and keys <= _STRANDS_LOOP_FLAGS:
        return ""
    message = payload.get("message")
    if keys == {"message"} and isinstance(message, dict) and "role" in message:
        return _message_text(message)
    if keys == {"result"} and isinstance(payload["result"], dict):
        # a serialized AgentResult: {stop_reason, message, metrics, state}
        return _message_text(payload["result"].get("message"))
    return None


def _is_converse_stream_event(payload: dict[str, Any]) -> bool:
    """Converse's event union has one member with a structured value.

    A free-form reply may use the same keys for auxiliary fields, especially
    ``metadata``. Only suppress bookkeeping when the whole body is an event.
    """
    if len(payload) != 1:
        return False
    name, detail = next(iter(payload.items()))
    if not isinstance(detail, dict):
        return False
    if name == "metadata":
        return any(isinstance(detail.get(key), dict) for key in ("usage", "metrics", "trace"))
    fields = {
        "contentBlockStart": ("start", dict),
        "contentBlockDelta": ("delta", dict),
        "contentBlockStop": ("contentBlockIndex", int),
        "messageStart": ("role", str),
        "messageStop": ("stopReason", str),
    }
    if name not in fields:
        return False
    field, field_type = fields[name]
    return isinstance(detail.get(field), field_type)


# Converse `metadata.usage` counters → the normalized `usage` event's fields.
# Strands forwards Bedrock's camelCase as-is; snake_case is accepted for code
# that re-serializes it.
_USAGE_FIELDS = (
    ("input_tokens", ("inputTokens", "input_tokens")),
    ("output_tokens", ("outputTokens", "output_tokens")),
    ("total_tokens", ("totalTokens", "total_tokens")),
    ("cache_read_input_tokens", ("cacheReadInputTokens", "cache_read_input_tokens")),
    ("cache_write_input_tokens", ("cacheWriteInputTokens", "cache_write_input_tokens")),
)


def _converse_usage(metadata: Any) -> dict[str, int] | None:
    """The token counts of one Converse ``metadata`` event, or None when it
    carries none. Only non-negative integers count; a missing total is
    input + output."""
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return None
    counts: dict[str, int] = {}
    for field, keys in _USAGE_FIELDS:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts[field] = value
                break
    if not counts:
        return None
    if "total_tokens" not in counts:
        counts["total_tokens"] = counts.get("input_tokens", 0) + counts.get("output_tokens", 0)
    return counts


def _add_usage(total: dict[str, int] | None, usage: dict[str, int]) -> dict[str, int]:
    """Sum per-model-call usage into the turn's totals (``model_calls`` counts them)."""
    calls = (total or {}).get("model_calls", 0) + 1
    summed = {k: v for k, v in (total or {}).items() if k != "model_calls"}
    for field, value in usage.items():
        summed[field] = summed.get(field, 0) + value
    return {**summed, "model_calls": calls}


# Conventional text keys, in preference order: the devguide example, then the
# names BYOC code in the wild actually uses (measured 2026-09-18: a CrewAI
# agent answering {"answer", "session_id", "turns", "latency_ms"} rendered as
# an empty reply with no error).
_FREE_FORM_TEXT_KEYS = (
    "response",
    "answer",
    "output",
    "output_text",
    "text",
    "message",
    "content",
    "completion",
    "reply",
)
_FREE_FORM_DUMP_LIMIT = 4000


def _free_form_payload_text(payload: dict[str, Any]) -> str:
    """Text for a JSON body that follows none of the known shapes.

    Takes the first conventional key holding a non-empty string; a nested
    ``{"text": ...}`` block (Converse content-block style) under such a key
    also counts. Otherwise the whole body is shown as compact JSON so the
    operator sees exactly what the agent answered instead of a blank turn.
    """
    for key in _FREE_FORM_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("text")
        if isinstance(value, str) and value.strip():
            return value
    if not payload:
        return ""
    logger.warning(
        "runtime answered JSON with no conventional text key; showing raw body (keys=%s)",
        sorted(payload.keys()),
    )
    dumped = json.dumps(payload, ensure_ascii=False)
    if len(dumped) > _FREE_FORM_DUMP_LIMIT:
        dumped = dumped[:_FREE_FORM_DUMP_LIMIT] + "…"
    return dumped


_UNRECOGNIZED_SAMPLE_CHARS = 300


def _normalized_runtime_events(
    payloads: Iterable[Any], *, require_attachments: bool = False, require_text: bool = False,
) -> Iterator[dict[str, Any]]:
    """Suppress a final full result when real deltas were already emitted.

    ``require_text`` (the invoke stream that Chat, ``/v1`` and evaluation share): a
    stream that ends with no text, tool call or consent ask to show raises a named
    error carrying the start of what the runtime sent, instead of a blank turn.

    Token usage the runtime reports (Converse ``metadata.usage``, one per model
    call — a tool turn has several) is summed and yielded once, as a ``usage``
    event, when the stream ends: ``{input_tokens, output_tokens, total_tokens,
    model_calls[, cache_*_input_tokens]}``. No usage reported → no event."""
    usage: dict[str, int] | None = None
    saw_delta = False
    saw_other = False
    acknowledged = False
    payload_count = 0
    sample = ""
    for payload in payloads:
        payload_count += 1
        if not sample:
            sample = (payload if isinstance(payload, str) else json.dumps(payload, default=str))[
                :_UNRECOGNIZED_SAMPLE_CHARS
            ]
        for event in _runtime_payload_events(payload):
            if event["event"] == "attachments":
                acknowledged = event["data"].get("contract") == "v1"
                continue
            if event["event"] == "usage":
                usage = _add_usage(usage, event["data"])
                continue
            if require_attachments and not acknowledged and event["event"] != "heartbeat":
                raise _attachment_ack_error()
            if event["event"] == "delta":
                saw_delta = True
                yield event
            elif event["event"] == "complete":
                if not saw_delta and event["data"]["text"]:
                    saw_delta = True
                    yield {"event": "delta", "data": {"text": event["data"]["text"]}}
                if "structured" in event["data"]:
                    # the text answer above stays the reply; this rides alongside
                    yield {"event": "structured", "data": {"value": event["data"]["structured"]}}
            else:
                if event["event"] in ("tool", "auth_required", *APPROVAL_EVENTS):
                    saw_other = True
                yield event
    if require_attachments and not acknowledged:
        raise _attachment_ack_error()
    if usage is not None:
        # before the unreadable-reply error below: those tokens were still spent
        yield {"event": "usage", "data": usage}
    if require_text and not saw_delta and not saw_other:
        if payload_count == 0:
            raise AppError(
                "runtime.empty_reply",
                "the agent's runtime answered with an empty response",
                status_code=502,
            )
        raise AppError(
            "runtime.unrecognized_reply",
            "the agent answered in a format the console cannot read — see docs: "
            "accepted runtime response formats",
            {"sample": sample},
            status_code=502,
        )


def _attachment_ack_error() -> AppError:
    return AppError(
        "chat.attachment_new_session_required",
        "The runtime did not acknowledge attachments. Start a new session; "
        "republish the agent if its entrypoint has not been upgraded.",
        status_code=409,
    )


def _runtime_invoke_params(
    runtime_arn: str,
    prompt: str,
    session_id: str,
    actor_id: str,
    qualifier: str | None,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    context: dict[str, str] | None = None,
    tool_approvals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "runtimeSessionId": session_id,
        "payload": _invoke_payload(
            prompt, actor_id, gateway_access_token, attachments, force_reauth_providers,
            context, tool_approvals,
        ),
    }
    if qualifier:
        params["qualifier"] = qualifier
    if runtime_user_id:
        # What makes the Runtime inject a WorkloadAccessToken into the container
        # request — without it, an agent that wants an outbound M2M token has no
        # workload identity token to exchange (measured: see the zip-gateway task's
        # research/r1-m2m-token-path.md). Passed ONLY for agents whose spec needs
        # it, so every other agent's invoke call is unchanged.
        params["runtimeUserId"] = runtime_user_id[:1024]
    return params


def _invoke_payload(
    prompt: str,
    actor_id: str,
    gateway_access_token: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    context: dict[str, str] | None = None,
    tool_approvals: dict[str, Any] | None = None,
) -> bytes:
    """The request body both transports (SigV4 and bearer) send."""
    payload: dict[str, Any] = {"prompt": prompt, "actor_id": actor_id}
    if tool_approvals is not None:
        # console chat only (strands-builder SB-2 tool hooks): the caller can
        # approve, and which tools the user approved for this turn. /v1 and
        # evaluations never send it, so the runtime refuses approval-list tools.
        payload["tool_approvals"] = {
            "interactive": bool(tool_approvals.get("interactive")),
            "approved": [str(name) for name in tool_approvals.get("approved") or [] if name],
        }
    if attachments:
        payload["attachments"] = attachments
    if context:
        # this turn's {{variable}} values (strands-builder SB-1): the generated
        # Strands runtime fills declared prompt variables from it, others ignore it
        payload["context"] = dict(context)
    if force_reauth_providers:
        # as_user revocation: the generated identity block sends
        # forceAuthentication=true for exactly these Connections this turn.
        payload["force_reauth_providers"] = list(force_reauth_providers)
    if gateway_access_token:
        # The InvokeAgentRuntime payload is marked sensitive in the service
        # model. Generated Launchpad runtimes consume this value in memory only
        # and never log or persist it.
        payload["gateway_access_token"] = gateway_access_token
    return json.dumps(payload).encode("utf-8")


def stream_runtime_events(
    client: Any,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    context: dict[str, str] | None = None,
    tool_approvals: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Invoke a runtime and yield normalized tool/text events as bytes arrive."""
    session_id = session_id or new_session_id()
    response = client.invoke_agent_runtime(
        **_runtime_invoke_params(
            runtime_arn,
            prompt,
            session_id,
            actor_id,
            qualifier,
            runtime_user_id,
            gateway_access_token,
            attachments,
            force_reauth_providers,
            context,
            tool_approvals,
        )
    )
    body = response["response"]
    try:
        yield from _runtime_body_events(
            body, str(response.get("contentType", "")).lower(), bool(attachments),
        )
    finally:
        if hasattr(body, "close"):
            body.close()


def _runtime_body_events(
    body: Any, content_type: str, require_attachments: bool,
) -> Iterator[dict[str, Any]]:
    if "text/event-stream" in content_type:
        lines = (
            body.iter_lines(chunk_size=SSE_READ_CHUNK_BYTES)
            if hasattr(body, "iter_lines")
            else body.read().splitlines()
        )
        yield from _normalized_runtime_events(
            _sse_payloads(lines), require_attachments=require_attachments, require_text=True,
        )
        return

    raw = body.read()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        decoded = raw.decode("utf-8", errors="replace") if raw else ""
        if decoded.lstrip().startswith("data:"):
            yield from _normalized_runtime_events(
                _sse_payloads(decoded.splitlines()),
                require_attachments=require_attachments, require_text=True,
            )
        else:
            yield from _normalized_runtime_events(
                [decoded] if decoded else [],
                require_attachments=require_attachments, require_text=True,
            )
    else:
        yield from _normalized_runtime_events(
            [payload], require_attachments=require_attachments, require_text=True,
        )


def invoke_runtime_text(
    client: Any,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    context: dict[str, str] | None = None,
    tool_approvals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronous InvokeAgentRuntime, joining native streaming responses.

    as_user consent asks (``auth_required`` events) are collected onto
    ``result["auth_required"]`` — a non-streaming caller still needs the
    authorization URL to relay to the user. Reported token usage (the stream's
    ``usage`` event) lands on ``result["usage"]``, and an ``output_schema``
    agent's parsed answer (the ``structured`` event) on ``result["structured"]``.
    SB-2 approval notices land on ``result["approval_required"]`` /
    ``result["approval_refused"]``.
    """
    session_id = session_id or new_session_id()
    extra: dict[str, Any] = {"attachments": attachments} if attachments else {}
    if force_reauth_providers:
        extra["force_reauth_providers"] = force_reauth_providers
    if context:
        extra["context"] = context
    if tool_approvals is not None:
        extra["tool_approvals"] = tool_approvals
    parts: list[str] = []
    auth_required: list[dict[str, Any]] = []
    usage: dict[str, int] | None = None
    structured: Any = None
    approvals: dict[str, list[dict[str, Any]]] = {}
    for event in stream_runtime_events(
        client,
        runtime_arn,
        prompt,
        session_id=session_id,
        actor_id=actor_id,
        qualifier=qualifier,
        runtime_user_id=runtime_user_id,
        gateway_access_token=gateway_access_token,
        **extra,
    ):
        if event["event"] == "delta":
            parts.append(event["data"]["text"])
        elif event["event"] == "auth_required":
            auth_required.append(event["data"])
        elif event["event"] == "usage":
            usage = event["data"]
        elif event["event"] == "structured":
            structured = event["data"]["value"]
        elif event["event"] in APPROVAL_EVENTS:
            approvals.setdefault(event["event"], []).append(event["data"])
    result: dict[str, Any] = {"text": "".join(parts), "session_id": session_id}
    if auth_required:
        result["auth_required"] = auth_required
    if usage is not None:
        result["usage"] = usage
    if structured is not None:
        result["structured"] = structured
    result.update(approvals)
    return result


def bearer_invoke_url(region: str, runtime_arn: str, qualifier: str = "DEFAULT") -> str:
    """The Runtime data-plane HTTPS invocation URL for one runtime ARN."""
    escaped = urllib.parse.quote(runtime_arn, safe="")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{escaped}/invocations?qualifier={qualifier}"
    )


@contextmanager
def _default_bearer_response(url: str, headers: dict[str, str], body: bytes):
    with httpx.Client(timeout=httpx.Timeout(10.0, read=BEARER_READ_TIMEOUT_S)) as client:
        with client.stream("POST", url, headers=headers, content=body) as response:
            yield response


class _HttpxBody:
    """Adapts a streamed httpx response to the botocore body ``_runtime_body_events``
    reads (``iter_lines(chunk_size=)`` / ``read()``), so both transports share
    one parser."""

    def __init__(self, response: Any):
        self._response = response

    def iter_lines(self, chunk_size: int | None = None) -> Iterator[str]:
        return self._response.iter_lines()

    def read(self) -> bytes:
        return self._response.read()


def stream_runtime_events_bearer(
    region: str,
    runtime_arn: str,
    bearer_token: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
    gateway_access_token: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    http_response: Any = None,
    context: dict[str, str] | None = None,
    tool_approvals: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Bearer-token InvokeAgentRuntime over the data-plane HTTPS endpoint.

    Same body the SigV4 path sends and the same normalized events out
    (``_runtime_body_events``), so everything downstream of the transport is
    shared. No runtimeUserId: under a JWT authorizer the Runtime derives the
    user identity from the validated token itself (iss + sub, devguide
    "Get workload access token"). ``http_response`` injects a stub response
    context manager for tests.
    """
    session_id = session_id or new_session_id()
    url = bearer_invoke_url(region, runtime_arn, qualifier or "DEFAULT")
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        BEARER_SESSION_HEADER: session_id,
    }
    body = _invoke_payload(
        prompt, actor_id, gateway_access_token, attachments, force_reauth_providers,
        context, tool_approvals,
    )
    opener = http_response or _default_bearer_response
    with opener(url, headers, body) as response:
        status = int(getattr(response, "status_code", 0))
        if status in (401, 403):
            detail = _read_error_body(response)
            raise RuntimeBearerAuthError(
                status,
                f"the Runtime's JWT authorizer rejected the request (HTTP {status})"
                + (f": {detail}" if detail else ""),
            )
        if status != 200:
            detail = _read_error_body(response)
            raise RuntimeBearerHttpError(
                status,
                f"bearer invoke returned HTTP {status}"
                + (f": {detail}" if detail else ""),
            )
        content_type = str(
            (getattr(response, "headers", None) or {}).get("content-type") or ""
        ).lower()
        yield from _runtime_body_events(_HttpxBody(response), content_type, bool(attachments))


def _read_error_body(response: Any, limit: int = 500) -> str:
    try:
        raw = response.read()
    except Exception:  # noqa: BLE001 — the status code is the real signal
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    return text.strip()[:limit]


def invoke_runtime_text_bearer(
    region: str,
    runtime_arn: str,
    bearer_token: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
    http_response: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """Synchronous bearer invoke (SigV4 twin: ``invoke_runtime_text``); ``extra``
    carries the same optional payload fields (attachments, force-reauth,
    gateway token, context, tool approvals)."""
    session_id = session_id or new_session_id()
    parts: list[str] = []
    auth_required: list[dict[str, Any]] = []
    usage: dict[str, int] | None = None
    structured: Any = None
    approvals: dict[str, list[dict[str, Any]]] = {}
    for event in stream_runtime_events_bearer(
        region,
        runtime_arn,
        bearer_token,
        prompt,
        session_id=session_id,
        actor_id=actor_id,
        qualifier=qualifier,
        http_response=http_response,
        **extra,
    ):
        if event["event"] == "delta":
            parts.append(event["data"]["text"])
        elif event["event"] == "auth_required":
            auth_required.append(event["data"])
        elif event["event"] == "usage":
            usage = event["data"]
        elif event["event"] == "structured":
            structured = event["data"]["value"]
        elif event["event"] in APPROVAL_EVENTS:
            approvals.setdefault(event["event"], []).append(event["data"])
    result: dict[str, Any] = {"text": "".join(parts), "session_id": session_id}
    if auth_required:
        result["auth_required"] = auth_required
    if usage is not None:
        result["usage"] = usage
    if structured is not None:
        result["structured"] = structured
    result.update(approvals)
    return result


def stop_runtime_session(
    client: Any,
    *,
    runtime_arn: str,
    session_id: str,
    qualifier: str | None = None,
) -> dict[str, Any]:
    """Data-plane ``StopRuntimeSession``: end one live runtime session now.

    Terminates the session's microVM and any streaming response still in flight,
    so the next turn under a fresh id starts on the runtime's *current* version
    (an existing session stays pinned to the version that first served it).
    ``ResourceNotFoundException`` — the session already ended or idle-expired —
    is the caller's to interpret; here it propagates unchanged.
    """
    params: dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "runtimeSessionId": session_id,
    }
    if qualifier:
        params["qualifier"] = qualifier
    response = client.stop_runtime_session(**params)
    return {
        "session_id": response.get("runtimeSessionId", session_id),
        "status_code": response.get("statusCode"),
    }


def get_agent_card(
    client: Any,
    *,
    runtime_arn: str,
    qualifier: str = "DEFAULT",
) -> dict[str, Any]:
    """Data-plane ``GetAgentCard``: the A2A card the runtime serves right now.

    The card is what an A2A client would read at ``/.well-known/agent-card.json``
    (a JSON document — botocore hands it back parsed; a string body is parsed
    here). No ``runtimeSessionId`` is sent: this is a one-off read, not a
    conversation. AgentCore still opens a session to serve it and echoes its id;
    that session is ended at once with ``StopRuntimeSession`` so a card read
    never leaves a warm microVM behind. Ending it is fail-soft — the card is the
    answer, a stop failure is logged and ignored.
    """
    response = client.get_agent_card(agentRuntimeArn=runtime_arn, qualifier=qualifier)
    card = response.get("agentCard")
    if isinstance(card, (str, bytes)):
        try:
            card = json.loads(card)
        except ValueError:
            card = {"raw": card.decode() if isinstance(card, bytes) else card}
    session_id = response.get("runtimeSessionId")
    if session_id:
        try:
            stop_runtime_session(
                client, runtime_arn=runtime_arn, session_id=session_id, qualifier=qualifier
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft by design
            logger.warning(
                "GetAgentCard session %s on %s not ended: %s", session_id, runtime_arn, exc
            )
    return {
        "card": card if isinstance(card, dict) else {},
        "status_code": response.get("statusCode"),
        "session_id": session_id,
    }


def a2a_result_text(result: dict[str, Any]) -> str:
    """Reply text from a message/send result (Task or Message shape).

    Task replies carry the final text in artifacts[].parts[]; Task.history is
    streaming fragments (probed live: agent messages arrive split mid-word)
    and must never be joined. Message replies carry parts directly.
    """
    parts: list[Any] = []
    if result.get("kind") == "message":
        parts = result.get("parts") or []
    else:  # task shape
        for artifact in result.get("artifacts") or []:
            parts.extend(artifact.get("parts") or [])
    return "".join(
        p.get("text", "") for p in parts if isinstance(p, dict)
    ).strip()


def invoke_a2a_text(
    client: Any,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """JSON-RPC message/send against an A2A-protocol runtime.

    InvokeAgentRuntime passes the JSON-RPC envelope through unmodified for
    serverProtocol=A2A runtimes; the A2A server owns conversation state, so
    there is no actor_id/memory envelope here.
    """
    session_id = session_id or new_session_id()
    parts = [{"kind": "text", "text": prompt}]
    for item in attachments or []:
        parts.append({
            "kind": "file",
            "file": {"name": item["name"], "mimeType": item["media_type"], "bytes": item["data"]},
        })
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": uuid.uuid4().hex,
                "contextId": session_id,
                "parts": parts,
            }
        },
    }
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode("utf-8"),
    )
    body = json.loads(response["response"].read())
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise RuntimeError(
            f"A2A error {err.get('code', '?')}: {err.get('message', '')}"
        )
    result = body.get("result") if isinstance(body, dict) else None
    return {
        "text": a2a_result_text(result if isinstance(result, dict) else {}),
        "session_id": session_id,
    }


class RuntimeJsonError(RuntimeError):
    """A JSON invoke got an answer that is not a JSON document."""


def invoke_runtime_json(
    ctx: Any,
    runtime_arn: str,
    session_id: str,
    payload: dict[str, Any],
    *,
    read_timeout: float | None = None,
    client: Any = None,
) -> Any:
    """InvokeAgentRuntime with a caller-defined JSON body; returns the decoded JSON.

    For platform-owned runtimes that speak their own action protocol rather than
    the agent ``{prompt, actor_id}`` contract (the coding runtime: ``{action, …}``
    in, ``{ok, result | error}`` out). Unlike ``invoke_runtime_text`` nothing is
    interpreted here — the protocol belongs to the caller. ``ctx`` is the
    workspace the runtime lives in; ``read_timeout`` overrides the data client's
    default (``agentcore_read_timeout_s``) for a call known to be short or long;
    ``client`` is injectable for tests. AWS errors (throttling, a runtime that is
    not READY, ``RuntimeClientError``) propagate unchanged.
    """
    if client is None:
        from app.services.agentcore.client import data_client

        client = data_client(ctx, read_timeout=read_timeout)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        # UTF-8, not \u-escaped: a 2 MB file save must not double on the wire
        payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    raw = response["response"].read()
    try:
        return json.loads(raw)
    except ValueError as exc:
        preview = raw[:200].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)[:200]
        raise RuntimeJsonError(f"runtime answered a non-JSON body: {preview!r}") from exc
