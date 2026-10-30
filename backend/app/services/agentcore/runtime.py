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
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from app.core.errors import AppError
from app.services import a2a_actor, aws_clients
from app.services.strands_eval import runtime_invoke as _upstream

logger = logging.getLogger("launchpad.agentcore.runtime")

TERMINAL_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED"}
SSE_READ_CHUNK_BYTES = 32

# Pinned on every CreateAgentRuntime/UpdateAgentRuntime call (spec C1): the CN
# partition only ships the V2 platform, and boto3>=1.43.109 is required to send
# this parameter at all.
RUNTIME_PLATFORM_VERSION = "V2"


# AgentCore Runtime ``runtimeSessionId`` bounds (InvokeAgentRuntime: min 33, max 100 chars).
# ``invoke.validate_session_id`` refuses a caller-supplied id outside them up front
# (N-SCN-obs16); ``new_session_id`` always generates 64.
SESSION_ID_MIN_LEN = 33
SESSION_ID_MAX_LEN = 100
# N16-obs72: the id travels as the ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`` header
# and as the Memory ``sessionId`` (botocore shape pattern ``[a-zA-Z0-9][a-zA-Z0-9-_]*``); a
# non-ASCII or blank-bearing id used to pass the length check and fail at AWS as a SigV4
# signature mismatch recorded as an error row. ``validate_session_id`` applies it first.
SESSION_ID_PATTERN = "[a-zA-Z0-9][a-zA-Z0-9-_]*"
SESSION_ID_RE = re.compile(rf"{SESSION_ID_PATTERN}\Z")


def new_session_id() -> str:
    """Runtime session ids must be ≥33 chars; two uuid4 hex strings = 64."""
    return uuid.uuid4().hex + uuid.uuid4().hex


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
    lifecycle: dict[str, int] | None = None,
) -> dict[str, Any]:
    """CreateAgentRuntime from a zip on S3, instrumented via ADOT.

    ``python_version``/``entrypoint`` default to the platform artifact contract
    (PYTHON_3_13, main.py); BYOC passes the user's choices through and disables
    the ADOT launcher (user zips don't necessarily vendor the distro).
    ``authorizer_configuration`` is the inbound JWT authorizer
    ({"customJWTAuthorizer": ...}); None means IAM/SigV4 (the field is omitted).
    ``lifecycle`` = {idleRuntimeSessionTimeout, maxLifetime} seconds (the evaluation
    worker keeps a session alive through an 8 h async job — N12 plan §B.0)."""
    params: dict[str, Any] = {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": _code_artifact(
            s3_bucket, s3_key, python_version, entrypoint, instrument
        ),
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "roleArn": role_arn,
        "platformVersion": RUNTIME_PLATFORM_VERSION,
    }
    if environment:
        params["environmentVariables"] = dict(environment)
    proto = _protocol_configuration(protocol)
    if proto:
        params["protocolConfiguration"] = proto
    if authorizer_configuration:
        params["authorizerConfiguration"] = authorizer_configuration
    if filesystem_configurations:
        params["filesystemConfigurations"] = filesystem_configurations
    if lifecycle:
        params["lifecycleConfiguration"] = dict(lifecycle)
    return client.create_agent_runtime(**params)


def _network_configuration(vpc: dict[str, Any] | None) -> dict[str, Any]:
    """PUBLIC by default; VPC mode when a networkModeConfig is supplied
    (required for BYO file systems — S3 Files / EFS access points)."""
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
) -> dict[str, Any]:
    """CreateAgentRuntime from an ECR image (Claude SDK container path).

    ``lifecycle`` = {idleRuntimeSessionTimeout, maxLifetime} seconds (the Skill
    Lab exec worker caps sessions at 8h and reaps idle ones at 5min)."""
    params: dict[str, Any] = {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": container_uri}
        },
        "networkConfiguration": _network_configuration(vpc),
        "roleArn": role_arn,
        "platformVersion": RUNTIME_PLATFORM_VERSION,
    }
    if environment:
        params["environmentVariables"] = dict(environment)
    if filesystem_configurations:
        params["filesystemConfigurations"] = filesystem_configurations
    if lifecycle:
        params["lifecycleConfiguration"] = dict(lifecycle)
    if authorizer_configuration:
        params["authorizerConfiguration"] = authorizer_configuration
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
    lifecycle: dict[str, int] | None = None,
) -> dict[str, Any]:
    """UpdateAgentRuntime with a new zip artifact — publishes a new version in
    place (same agentRuntimeId/ARN; the DEFAULT endpoint auto-rolls to it).

    ``lifecycle`` = {idleRuntimeSessionTimeout, maxLifetime} seconds — like the protocol
    and the mounts, an omitted lifecycleConfiguration is reset to the service defaults
    (900 s idle; observed on t1 + default after a preset REPAIR, N12-bug7), so a system
    preset echoes its lifecycle on every update.

    ``protocol`` must be passed for A2A agents on EVERY update — the service
    resets an omitted protocolConfiguration back to HTTP (probed live). The same
    holds for ``filesystem_configurations``: an omitted list detaches every mount
    (probed live 2026-10-04), so a re-publish must echo it each time. The
    authorizer rides the same call: every update passes the RESOLVED inbound
    auth — echoing the JWT config keeps (or sets) it, omitting it switches the
    runtime back to IAM/SigV4, which is also how JWT→IAM transitions are
    performed (CloudFormation marks authorizer updates "No interruption")."""
    params: dict[str, Any] = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": _code_artifact(
            s3_bucket, s3_key, python_version, entrypoint, instrument
        ),
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "roleArn": role_arn,
        "platformVersion": RUNTIME_PLATFORM_VERSION,
    }
    if environment is not None:
        params["environmentVariables"] = dict(environment)
    proto = _protocol_configuration(protocol)
    if proto:
        params["protocolConfiguration"] = proto
    if authorizer_configuration:
        params["authorizerConfiguration"] = authorizer_configuration
    if filesystem_configurations:
        params["filesystemConfigurations"] = filesystem_configurations
    if lifecycle:
        params["lifecycleConfiguration"] = dict(lifecycle)
    return client.update_agent_runtime(**params)


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
) -> dict[str, Any]:
    """UpdateAgentRuntime with a new container image — new version, same ARN.
    NB: a version bump resets managed session storage (documented UI note).
    Authorizer semantics as ``update_code_runtime``: echo to keep, omit to
    return the runtime to IAM/SigV4."""
    params: dict[str, Any] = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": container_uri}},
        "networkConfiguration": _network_configuration(vpc),
        "roleArn": role_arn,
        "platformVersion": RUNTIME_PLATFORM_VERSION,
    }
    if environment is not None:
        params["environmentVariables"] = dict(environment)
    if filesystem_configurations:
        params["filesystemConfigurations"] = filesystem_configurations
    if lifecycle:
        params["lifecycleConfiguration"] = dict(lifecycle)
    if authorizer_configuration:
        params["authorizerConfiguration"] = authorizer_configuration
    return client.update_agent_runtime(**params)


def get_runtime(client: Any, runtime_id: str) -> dict[str, Any]:
    return client.get_agent_runtime(agentRuntimeId=runtime_id)


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


# Outcomes of ``delete_runtime_if_present`` (N16-obs87).
RUNTIME_DELETED = "deleted"
RUNTIME_ABSENT = "absent"


def _aws_marker(exc: BaseException) -> str:
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    return f"{type(exc).__name__}{code}"


def is_not_found_error(exc: BaseException) -> bool:
    return "ResourceNotFound" in _aws_marker(exc)


def is_access_denied_error(exc: BaseException) -> bool:
    return "AccessDenied" in _aws_marker(exc)


def delete_runtime_if_present(
    client: Any, runtime_id: str, log: Callable[[str], None] = lambda _m: None
) -> str:
    """``DeleteAgentRuntime`` guarded by a readback (N16-obs87, live t1 c1438eaf).

    The control plane answers ``AccessDeniedException`` — not ``ResourceNotFound`` — to
    a delete of a runtime id that no longer exists, although ``GetAgentRuntime`` on the
    same id says ResourceNotFound and the role holds ``bedrock-agentcore:*``. Surfaced
    as a 403 that aborted the agent delete before the ledger row, so the agent could
    never be deleted from the console. Here: read the runtime back first — gone →
    ``RUNTIME_ABSENT`` without a delete; a delete refused with AccessDenied is re-read —
    gone → the runtime went (``RUNTIME_DELETED``), still there → the refusal propagates
    (a real permission gap stays loud). Any other Get failure falls through to the
    delete, which is what the caller asked for."""
    try:
        get_runtime(client, runtime_id)
    except Exception as exc:  # noqa: BLE001 — only NotFound is an answer here
        if is_not_found_error(exc):
            log(f"runtime {runtime_id} is already gone — nothing to delete")
            return RUNTIME_ABSENT
        log(f"runtime {runtime_id} readback failed ({_aws_marker(exc)}) — deleting anyway")
    try:
        delete_runtime(client, runtime_id)
        return RUNTIME_DELETED
    except Exception as exc:  # noqa: BLE001
        if is_not_found_error(exc):
            return RUNTIME_ABSENT
        if is_access_denied_error(exc):
            try:
                get_runtime(client, runtime_id)
            except Exception as again:  # noqa: BLE001
                if is_not_found_error(again):
                    log(f"runtime {runtime_id}: delete answered AccessDenied but the runtime "
                        "reads back gone — treated as deleted")
                    return RUNTIME_DELETED
        raise


def wait_runtime_gone(
    client: Any,
    runtime_id: str,
    timeout_s: float = 120,
    interval_s: float = 3,
    sleeper: Any = time.sleep,
) -> bool:
    """Poll GetAgentRuntime until the delete has completed — the control plane answers
    ``ResourceNotFoundException``. ``DeleteAgentRuntime`` returns while the runtime is
    still ``DELETING`` and its container may flush telemetry for a second or two more
    (N10v4-obs9). Returns ``False`` when the runtime is still there at the deadline;
    ``timeout_s=0`` is a single probe. Other errors propagate."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            get_runtime(client, runtime_id)
        except Exception as exc:  # noqa: BLE001 — only NotFound is the answer we want
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if "ResourceNotFound" in f"{type(exc).__name__}{code}":
                return True
            raise
        if time.monotonic() >= deadline:
            return False
        sleeper(interval_s)


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
    client: Any, *, runtime_id: str, endpoint_name: str, version: int | str
) -> dict[str, Any]:
    """CreateAgentRuntimeEndpoint — a named endpoint pinned to ``version``."""
    return client.create_agent_runtime_endpoint(
        agentRuntimeId=runtime_id,
        name=endpoint_name,
        agentRuntimeVersion=str(version),
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


class MalformedFrame(str):
    """An SSE ``data:`` payload that is not JSON (N17-obs96). The AgentCore SDK serialises
    every frame with ``json.dumps`` (a bare ``yield "text"`` arrives as the JSON string
    ``"text"``), so a line that does not parse is what a connection dropped mid-frame leaves
    — the recorded live shape is the agent template's own error frame cut at 300 chars — or a
    runtime writing something other than the protocol. A ``str`` subclass, so the architect's
    ``iter_sse_payloads`` consumers keep seeing the text they always did; the chat parser
    turns it into a protocol error instead of the member's reply."""


def _sse_payloads(lines: Iterable[bytes | str]) -> Iterator[Any]:
    """Decode SSE data fields without buffering beyond one event. A payload that is not JSON
    is yielded as a ``MalformedFrame`` (a ``str``)."""
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
                    yield MalformedFrame(data)
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if data_lines:
        data = "\n".join(data_lines)
        try:
            yield json.loads(data)
        except ValueError:
            yield MalformedFrame(data)


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


# What a recorded stream error keeps of the SDK frame's ``error`` text: the ledger row and
# the turn event log must stay bounded (a ReadTimeout repr is short; a ValidationException
# from a 100 k-token prompt can echo the prompt).
STREAM_ERROR_TEXT_MAX = 600


class SdkStreamError(RuntimeError):
    """The Runtime stream ended on the AgentCore SDK's terminal error frame
    (``bedrock_agentcore/runtime/app.py::_stream_with_error_handling``:
    ``{"error": str(exc), "error_type": type(exc).__name__, "message": "An error
    occurred during streaming"}`` — what ``BedrockAgentCoreApp`` emits when the agent
    loop re-raises: a model ``ReadTimeout``, ``ModelThrottledException``,
    ``ContextWindowOverflowException``, ``ValidationException``, an expired bearer, …),
    or on the one-JSON-body ``{"error": …}`` refusal. ``error_type`` is the frame's
    exception class name (None when absent); ``detail`` is the structured form the
    assistant's terminal ``error`` event and turn event log carry (N-ARCH-obs18)."""

    def __init__(self, message: str, *, error_type: str | None = None,
                 detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.detail: dict[str, Any] = dict(detail or {})
        if error_type and "error_type" not in self.detail:
            self.detail["error_type"] = error_type


def bounded_error_text(value: Any) -> str:
    """One line of at most ``STREAM_ERROR_TEXT_MAX`` chars, cut with a visible marker."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= STREAM_ERROR_TEXT_MAX:
        return text
    return text[:STREAM_ERROR_TEXT_MAX] + f"… [+{len(text) - STREAM_ERROR_TEXT_MAX} chars]"


def is_sdk_error_frame(frame: dict[str, Any]) -> bool:
    """``error`` truthy or ``error_type`` present (an empty ``error`` with the class
    name set still names a failure). Converse events never carry these keys at the
    top level; the Harness's own ``{"event": "error", "message"}`` has neither."""
    return bool(frame.get("error")) or "error_type" in frame


def sdk_error_parts(frame: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    """``(text, error_type, detail)`` for an SDK error frame: ``runtime returned
    error: <error_type>: <error> (<message>)`` with every piece bounded."""
    error_type = str(frame.get("error_type") or "").strip() or None
    error = bounded_error_text(frame.get("error"))
    message = bounded_error_text(frame.get("message"))
    head = f"{error_type}: {error}" if error_type and error else (error_type or error
                                                                  or "unknown error")
    text = f"runtime returned error: {head}"
    if message and message != error:
        text += f" ({message})"
    detail: dict[str, Any] = {"error": error, "message": message}
    if error_type:
        detail["error_type"] = error_type
    return text, error_type, detail


def sdk_stream_error(frame: dict[str, Any]) -> SdkStreamError:
    text, error_type, detail = sdk_error_parts(frame)
    return SdkStreamError(text, error_type=error_type, detail=detail)


# N17-obs96 / N17-obs97: two more ways a Runtime answer is an error and never reply text. Both
# are raised as ``SdkStreamError`` so every consumer treats them exactly like the SDK's own
# terminal frame: Chat's terminal ``error`` event (with ``detail``), the 502
# ``agent.runtime_error`` envelope on the sync entrances, the architect's replay predicate
# (neither is upstream-transient).
MALFORMED_FRAME_TYPE = "MalformedFrame"
REJECTED_BODY_TYPE = "RuntimeRejected"
MALFORMED_FRAME_MESSAGE = "malformed runtime frame"


def malformed_frame_error(text: str) -> SdkStreamError:
    """A ``data:`` line that is not JSON — the stream was cut mid-frame (the recorded shape) or
    the runtime wrote plain text where the SDK protocol expects a JSON frame. The cut text is
    kept, bounded, in ``detail.error`` so the operator can see what arrived."""
    cut = bounded_error_text(text)
    return SdkStreamError(
        f"runtime returned error: {MALFORMED_FRAME_TYPE}: {MALFORMED_FRAME_MESSAGE} "
        f"(a data line that is not JSON: {cut})",
        error_type=MALFORMED_FRAME_TYPE,
        detail={"error": cut, "message": MALFORMED_FRAME_MESSAGE},
    )


#: The ``state`` values that make a ``code``-carrying body a refusal (rev-r17 on cn/r17-o: a
#: free-form ``{"code": "OK", "state": "done", …}`` body must not read as a rejection).
REFUSAL_STATES = frozenset({"REJECTED", "FAILED", "ERROR", "DENIED", "REFUSED"})


def is_rejection_body(payload: dict[str, Any]) -> bool:
    """A JSON body carrying a ``code`` plus a refusal ``state`` or an ``error`` key is a structured
    refusal (the evaluation worker's ``{"state": "REJECTED", "code": …, "message": …}`` ack
    is the recorded live shape — a Runtime invoked with a payload it refuses), not a reply:
    the free-form ``message`` key must not win. The devguide's ``{"answer": …}`` / BYOC
    ``{"result": …}`` bodies carry neither key."""
    code = payload.get("code")
    state = payload.get("state")
    return (
        isinstance(code, str)
        and bool(code.strip())
        and (
            (isinstance(state, str) and state.strip().upper() in REFUSAL_STATES)
            or "error" in payload
        )
    )


def rejection_error(payload: dict[str, Any]) -> SdkStreamError:
    code = str(payload.get("code") or "").strip()
    state = payload.get("state")
    message = bounded_error_text(payload.get("message") or payload.get("error") or "")
    head = f"{code}: {message}" if message else code
    detail: dict[str, Any] = {"error": message, "message": f"runtime refused the request ({code})",
                              "code": code}
    if isinstance(state, str):
        detail["state"] = state
    return SdkStreamError(
        f"runtime returned error: {REJECTED_BODY_TYPE}: {head}",
        error_type=REJECTED_BODY_TYPE,
        detail=detail,
    )


# The ONE transient-upstream predicate (N-ARCH-obs18 / N12-obs53 / N12-obs55): the architect
# assistant's replay backoff, the evaluation worker's scenario replay and its judge calls
# all ask it. It is defined in ``services/strands_eval/runtime_invoke.py`` — a module the
# worker zip ships byte-for-byte and which may not import ``app.*`` — and re-exported here
# so hub code keeps importing it from the Runtime wrapper.
UPSTREAM_TRANSIENT_MARKERS: tuple[str, ...] = _upstream.UPSTREAM_TRANSIENT_MARKERS


def is_upstream_transient(exc_or_text: BaseException | str | None) -> bool:
    """A retry-worthy upstream hiccup (5xx / throttle / read timeout / a mid-stream
    ``internalServerException`` / the SDK's terminal error frame naming one), from an
    exception or its text — see ``runtime_invoke.is_upstream_transient``."""
    return _upstream.is_upstream_transient(exc_or_text)


# N16-obs85: the bounds of the tool-call summaries a runtime streams (`tool_input.input`,
# `tool_result.summary`) — the generated templates cut at the same sizes; the hub cuts again.
TOOL_INPUT_MAX_CHARS = 512
TOOL_RESULT_SUMMARY_MAX_CHARS = 256
# C4 (rev-r16): defence in depth — the templates redact the same shape before they cut; a
# BYOC runtime does not, so the hub redacts again. Native shell / file tools get no result
# summary at all (arbitrary host text): status only.
_SECRET_PAIR_RE = re.compile(
    r"\b(\w*(?:token|secret|password|bearer|credential|authorization)\w*|\w*key)\b"
    r"[\"']?\s*[=:]\s*(?:[\"'][^\"']*[\"']|\S+)"
    r"|\b(bearer)\s+\S+",
    re.IGNORECASE,
)
UNSUMMARISED_TOOLS = frozenset({"shell", "file_operations"})


def redact_secret_pairs(text: str) -> str:
    """``<name>=<value>`` / ``<name>: <value>`` pairs whose name smells like a credential
    become ``<name>=[redacted]`` (value gone to the next whitespace)."""
    return _SECRET_PAIR_RE.sub(lambda m: f"{m.group(1) or m.group(2)}=[redacted]", text)


def bounded_summary(value: Any, limit: int) -> str:
    """One whitespace-collapsed, secret-redacted line of at most ``limit`` characters plus
    an explicit truncation marker (the architect's recorded-arguments shape)."""
    text = redact_secret_pairs(" ".join(str(value if value is not None else "").split()))
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit} chars]"


def _tool_use_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:128] if text else None


def _runtime_payload_events(payload: Any) -> Iterator[dict[str, Any]]:
    """Normalize one runtime payload to Chat's tool/delta/complete contract
    (lenient ``str()`` coercion of text fields, shared by Chat, the public API
    and evaluation replays). A ``MalformedFrame`` (N17-obs96) and a structured refusal
    body (N17-obs97) raise ``SdkStreamError`` — never reply text."""
    if isinstance(payload, MalformedFrame):
        raise malformed_frame_error(payload)
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

    if is_sdk_error_frame(payload):
        # the SDK's terminal frame / one-body refusal: bounded, error_type structured
        raise sdk_stream_error(payload)

    if payload.get("attachment_contract") == "v1":
        yield {"event": "attachments", "data": {"contract": "v1"}}
    kind = payload.get("event")
    if isinstance(kind, str) and (
        kind in {"delta", "heartbeat", "tool", "tool_input", "tool_result", "auth_required",
                 "complete", "error", "truncated", "policy_denied", "policy_decision"}
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
        elif kind == "tool_input":
            # N16-obs85: the call's arguments, bounded by the runtime and again here (a BYOC
            # runtime may send anything) — recorded on the chat tool row, never parsed
            yield {
                "event": "tool_input",
                "data": {
                    "id": _tool_use_id(payload.get("id")),
                    "name": str(payload.get("name", "")),
                    "input": bounded_summary(payload.get("input"), TOOL_INPUT_MAX_CHARS),
                },
            }
        elif kind == "tool_result":
            # N16-obs85: what came back, as the runtime summarised it (browser: title, final
            # url, text length; otherwise the first characters) — never the full output
            name = str(payload.get("name") or "")
            yield {
                "event": "tool_result",
                "data": {
                    "id": _tool_use_id(payload.get("id")),
                    "name": name,
                    "status": str(payload.get("status") or ""),
                    "summary": (
                        "" if name in UNSUMMARISED_TOOLS
                        else bounded_summary(
                            payload.get("summary"), TOOL_RESULT_SUMMARY_MAX_CHARS
                        )
                    ),
                },
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
        elif kind == "complete":
            data: dict[str, Any] = {"text": str(payload.get("result", ""))}
            if payload.get("truncated"):
                data["truncated"] = True  # N-SCN-bug4: additive, see `truncated` below
            yield {"event": "complete", "data": data}
        elif kind == "truncated":
            # N-SCN-bug4: the runtime recovered a max_tokens stop. `truncated: false`
            # separates the partial text from the continuation that follows;
            # `truncated: true` closes a turn that still could not finish (the notice
            # text arrived as a delta just before). Forwarded as its own SSE event; the
            # console ignores event kinds it does not know, so it keeps working as-is.
            yield {
                "event": "truncated",
                "data": {
                    "truncated": bool(payload.get("truncated")),
                    "stop_reason": str(payload.get("stop_reason") or "max_tokens"),
                    "continuations": int(payload.get("continuations") or 0),
                },
            }
        elif kind in ("policy_denied", "policy_decision"):
            # D20 P2-a: the runtime guard refused a Gateway tool call (`policy_denied`,
            # mode enforce — Global's PolicyDenyCard contract) or observed one it would
            # have refused (`policy_decision`, mode log_only — the tool ran). The invoke
            # layer writes the audit row and adds `decision_id`; `tool`, `reason`,
            # `policy_id`, `tool_use_id` come from the guard. Text fields are coerced.
            # A6 (additive): `source: "gateway"` marks a re-emitted I8 Gateway denial
            # (`runtime_policy_denied_frame`; the hub writes no second row), `stage` the
            # engine stage when the frame names one; both default for the runtime guard.
            policy_id = payload.get("policy_id")
            bundle_version = payload.get("bundle_version")
            source = payload.get("source")
            stage = payload.get("stage")
            gateway_id = payload.get("decision_id") if source == "gateway" else None
            yield {
                "event": kind,
                "data": {
                    "tool": str(payload.get("tool", "")),
                    "reason": str(payload.get("reason", "")),
                    "policy_id": str(policy_id) if policy_id else None,
                    "tool_use_id": str(payload.get("tool_use_id", "")),
                    "mode": str(payload.get("mode", "")),
                    "decision": "deny" if kind == "policy_denied" else "would_deny",
                    "bundle_version": (
                        int(bundle_version) if isinstance(bundle_version, int) else None
                    ),
                    "decision_id": str(gateway_id) if gateway_id else None,
                    "source": "gateway" if source == "gateway" else "runtime",
                    "stage": stage if stage in ("a", "bt", "final") else None,
                },
            }
        elif kind == "error":
            raise RuntimeError(str(payload.get("message", "runtime stream failed")))
        return

    inner = kind if isinstance(kind, dict) else payload
    if "runtimeClientError" in inner or "internalServerException" in inner:
        detail = inner.get("runtimeClientError") or inner.get("internalServerException")
        raise RuntimeError(f"runtime returned error: {detail}")
    if inner is not payload and is_sdk_error_frame(inner):
        raise sdk_stream_error(inner)
    if is_rejection_body(payload):
        raise rejection_error(payload)  # N17-obs97: a refusal, not the free-form reply
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
    if "result" in payload:
        yield {"event": "complete", "data": {"text": str(payload.get("result", ""))}}
    elif not converse_event:
        text = _free_form_payload_text(payload)
        if text:
            yield {"event": "complete", "data": {"text": text}}


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


# N17-obs114: the events that count as an answer reaching the member. `heartbeat`, the
# `attachments` ack and a `truncated {truncated: false}` continuation marker are bookkeeping;
# everything else (text, a tool card, a consent ask, a policy card, the closing `truncated:
# true` notice) renders a row, so a turn that streamed one of them was not silent.
_BOOKKEEPING_EVENTS = frozenset({"heartbeat", "attachments"})


def _is_answer_event(event: dict[str, Any]) -> bool:
    kind = event["event"]
    if kind in _BOOKKEEPING_EVENTS:
        return False
    if kind == "truncated":
        return bool(event["data"].get("truncated"))
    return True


def empty_answer_error() -> AppError:
    """N17-obs114: the runtime completed without producing anything to show — an empty
    `complete` / `{"result": ""}` body (the recorded shape: the BYOC sample's model spent its
    whole token budget on reasoning and returned no text block), or a stream that ended with no
    frame at all. Rendered as a `complete` with empty text it yielded NO event, so the hub
    streamed `meta → done` and the console showed neither an agent bubble nor an error row.
    An explicit 502 — the same class as `agent.runtime_error`: the runtime ran and gave no
    answer, which the caller must be able to tell apart from an answer that is empty on
    purpose (there is no such thing on this protocol: a runtime with nothing to say streams
    an `error` or a notice)."""
    return AppError(
        "agent.empty_answer",
        "the runtime completed without an answer (no text, tool call or notice was produced)",
        {"reason": "empty_answer"},
        status_code=502,
    )


def _normalized_runtime_events(
    payloads: Iterable[Any], *, require_attachments: bool = False, require_answer: bool = False,
) -> Iterator[dict[str, Any]]:
    """Suppress a final full result when real deltas were already emitted. With
    ``require_answer`` (the live invoke path, N17-obs114) a stream that ends without one
    answer-bearing event raises ``agent.empty_answer`` instead of ending silently."""
    saw_delta = False
    acknowledged = False
    answered = False
    for payload in payloads:
        for event in _runtime_payload_events(payload):
            if event["event"] == "attachments":
                acknowledged = event["data"].get("contract") == "v1"
                continue
            if require_attachments and not acknowledged and event["event"] != "heartbeat":
                raise _attachment_ack_error()
            if event["event"] == "delta":
                saw_delta = answered = True
                yield event
            elif event["event"] == "complete":
                if not saw_delta and event["data"]["text"]:
                    saw_delta = answered = True
                    yield {"event": "delta", "data": event["data"]}
            else:
                answered = answered or _is_answer_event(event)
                yield event
    if require_attachments and not acknowledged:
        raise _attachment_ack_error()
    if require_answer and not answered:
        raise empty_answer_error()


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
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    baggage: str | None = None,
    launchpad_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "runtimeSessionId": session_id,
        "payload": _invoke_payload(
            prompt, actor_id, attachments, force_reauth_providers, launchpad_meta,
        ),
    }
    if qualifier:
        params["qualifier"] = qualifier
    if baggage:
        # N12 A/B + canary routing (CA-2): the W3C ``baggage`` header is a first-class
        # InvokeAgentRuntime parameter. It carries the config-bundle pointer and the
        # SDK's experiment keys, which BaggageSpanProcessor stamps on every span.
        # Whether the cn data plane passes it through is an N9 probe (R-1); the payload
        # alias below is the guaranteed channel.
        params["baggage"] = baggage
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
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    launchpad_meta: dict[str, Any] | None = None,
) -> bytes:
    """The request body both transports (SigV4 and bearer) send."""
    payload: dict[str, Any] = {"prompt": prompt, "actor_id": actor_id}
    if attachments:
        payload["attachments"] = attachments
    if force_reauth_providers:
        # as_user revocation: the generated identity block sends
        # forceAuthentication=true for exactly these Connections this turn.
        payload["force_reauth_providers"] = list(force_reauth_providers)
    if launchpad_meta:
        # ``{"bundle": "<id>:<v>", "experiment": <owner id>, "variant": "C"|"T1"}`` —
        # the runtime's ``_config_bundle()`` reads the pointer here first (CA-2).
        payload["launchpad"] = dict(launchpad_meta)
    return json.dumps(payload).encode("utf-8")


def stream_runtime_events(
    client: Any,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
    runtime_user_id: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    baggage: str | None = None,
    launchpad_meta: dict[str, Any] | None = None,
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
            attachments,
            force_reauth_providers,
            baggage=baggage,
            launchpad_meta=launchpad_meta,
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


def iter_sse_payloads(body: Any, content_type: str) -> Iterator[Any]:
    """Decoded ``data:`` payloads of a Runtime response (SSE or one JSON body).

    The raw, un-normalized counterpart of ``_runtime_body_events`` for callers that
    map the Converse events themselves (the architect assistant, plan R12). A JSON
    payload that fails to parse is yielded as the string ``_sse_payloads`` produced,
    so ``repr_event`` can still recover the SDK's repr fallback.
    """
    if "text/event-stream" in content_type.lower():
        lines = (
            body.iter_lines(chunk_size=SSE_READ_CHUNK_BYTES)
            if hasattr(body, "iter_lines")
            else body.read().splitlines()
        )
        yield from _sse_payloads(lines)
        return
    raw = body.read()
    try:
        yield json.loads(raw)
    except (ValueError, TypeError):
        decoded = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        yield from _sse_payloads(decoded.splitlines())


repr_event = _repr_event


def _runtime_body_events(
    body: Any, content_type: str, require_attachments: bool,
) -> Iterator[dict[str, Any]]:
    """The live invoke path: every shape of Runtime response normalised, and a response that
    carries no answer at all is ``agent.empty_answer`` (N17-obs114), never a silent end."""
    opts = {"require_attachments": require_attachments, "require_answer": True}
    if "text/event-stream" in content_type:
        lines = (
            body.iter_lines(chunk_size=SSE_READ_CHUNK_BYTES)
            if hasattr(body, "iter_lines")
            else body.read().splitlines()
        )
        yield from _normalized_runtime_events(_sse_payloads(lines), **opts)
        return

    raw = body.read()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        decoded = raw.decode("utf-8", errors="replace") if raw else ""
        if decoded.lstrip().startswith("data:"):
            yield from _normalized_runtime_events(_sse_payloads(decoded.splitlines()), **opts)
        else:
            yield from _normalized_runtime_events([decoded] if decoded else [], **opts)
    else:
        yield from _normalized_runtime_events([payload], **opts)


def invoke_runtime_text(
    client: Any,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
    runtime_user_id: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    force_reauth_providers: list[str] | None = None,
    baggage: str | None = None,
    launchpad_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronous InvokeAgentRuntime, joining native streaming responses.

    as_user consent asks (``auth_required`` events) are collected onto
    ``result["auth_required"]`` — a non-streaming caller still needs the
    authorization URL to relay to the user.
    """
    session_id = session_id or new_session_id()
    extra: dict[str, Any] = {"attachments": attachments} if attachments else {}
    if force_reauth_providers:
        extra["force_reauth_providers"] = force_reauth_providers
    if baggage:
        extra["baggage"] = baggage
    if launchpad_meta:
        extra["launchpad_meta"] = launchpad_meta
    parts: list[str] = []
    auth_required: list[dict[str, Any]] = []
    policy_decisions: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    truncated = False
    for event in stream_runtime_events(
        client,
        runtime_arn,
        prompt,
        session_id=session_id,
        actor_id=actor_id,
        qualifier=qualifier,
        runtime_user_id=runtime_user_id,
        **extra,
    ):
        if event["event"] == "delta":
            parts.append(event["data"]["text"])
        elif event["event"] == "auth_required":
            auth_required.append(event["data"])
        elif event["event"] == "truncated" and event["data"].get("truncated"):
            truncated = True
        elif event["event"] in ("policy_denied", "policy_decision"):
            policy_decisions.append(event["data"])  # D20 P2-a: audited by the invoke layer
        elif event["event"] == "tool_result":
            # N17-obs507: the invoke layer reads Gateway denials out of the result text
            tool_results.append(event["data"])
    result: dict[str, Any] = {"text": "".join(parts), "session_id": session_id}
    if auth_required:
        result["auth_required"] = auth_required
    if truncated:
        result["truncated"] = True  # N-SCN-bug4: the text ends with the runtime's notice
    if policy_decisions:
        result["policy_decisions"] = policy_decisions
    if tool_results:
        result["tool_results"] = tool_results  # consumed (popped) by services.invoke
    return result


def bearer_invoke_url(region: str, runtime_arn: str, qualifier: str = "DEFAULT") -> str:
    """The Runtime data-plane HTTPS invocation URL for one runtime ARN.

    Not used for invoking (this build's inbound auth is AWS_IAM-only, so every
    invoke goes through boto3's SigV4 ``invoke_agent_runtime``) — kept for the
    identity view's display-only caller example (``agent_identity._invoke_url``).
    """
    escaped = urllib.parse.quote(runtime_arn, safe="")
    suffix = aws_clients.dns_suffix_for_partition(aws_clients.partition_for_region(region))
    return (
        f"https://bedrock-agentcore.{region}.{suffix}"
        f"/runtimes/{escaped}/invocations?qualifier={qualifier}"
    )


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


# Task states in which status.message IS the agent's reply to the user (the agent is
# asking for input / authorization), not a failure.
A2A_REPLY_STATES = frozenset({"input-required", "auth-required"})
# N16-obs79 / N16-obs90: the A2A ``message.metadata`` keys carrying the Launchpad memory
# actor and the hub's signature over it; the A2A template reads the same literals.
A2A_ACTOR_METADATA_KEY = a2a_actor.METADATA_ACTOR
A2A_ACTOR_SIG_METADATA_KEY = a2a_actor.METADATA_SIG
# stream A2: the principal token on an A2A message (the I2 `_meta` key)
PRINCIPAL_METADATA_KEY = "launchpad/principal"
_A2A_LIVE_STATES = frozenset({"completed", "submitted", "working"}) | A2A_REPLY_STATES


def _a2a_status_text(result: dict[str, Any]) -> tuple[str, str]:
    """``(state, status.message text)`` of a Task result; empty strings when absent."""
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    state = str(status.get("state") or "").strip()
    message = status.get("message") if isinstance(status.get("message"), dict) else {}
    text = "".join(
        p.get("text", "") for p in (message.get("parts") or []) if isinstance(p, dict)
    ).strip()
    return state, text


def a2a_failure_detail(result: dict[str, Any]) -> str:
    """Why a message/send result carries no reply text: ``A2A task <state>: <status
    message>`` for a Task that failed/rejected/was canceled, else a plain "no text"."""
    state, reason = _a2a_status_text(result)
    if state and state not in _A2A_LIVE_STATES:
        return f"A2A task {state}: {reason or 'the agent reported no reason'}"
    return "the A2A agent returned no text" + (f" ({reason})" if reason else "")


def a2a_result_text(result: dict[str, Any]) -> str:
    """Reply text from a message/send result (Task or Message shape).

    Task replies carry the final text in artifacts[].parts[]; Task.history is
    streaming fragments (probed live: agent messages arrive split mid-word)
    and must never be joined. Message replies carry parts directly. A Task in
    ``input-required`` / ``auth-required`` carries the agent's question in
    status.message — that IS the reply.
    """
    parts: list[Any] = []
    # a Message (`kind: message`, or a bare parts[] without artifacts — tolerated)
    if result.get("kind") == "message" or ("parts" in result and "artifacts" not in result):
        parts = result.get("parts") or []
    else:  # task shape
        for artifact in result.get("artifacts") or []:
            parts.extend(artifact.get("parts") or [])
        if not parts:
            state, status_text = _a2a_status_text(result)
            if state in A2A_REPLY_STATES and status_text:
                return status_text
    return "".join(
        p.get("text", "") for p in parts if isinstance(p, dict)
    ).strip()


def invoke_a2a_text(
    client: Any,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    attachments: list[dict[str, str]] | None = None,
    runtime_user_id: str | None = None,
    actor_id: str | None = None,
    actor_key: str | None = None,
    principal_token: str | None = None,
) -> dict[str, Any]:
    """JSON-RPC message/send against an A2A-protocol runtime.

    InvokeAgentRuntime passes the JSON-RPC envelope through unmodified for
    serverProtocol=A2A runtimes; the A2A server owns conversation state, so
    there is no Launchpad request envelope here. ``runtime_user_id`` is still sent
    (N10v3-bug5 live correction): it is what makes the Runtime inject the workload
    access token the A2A server exchanges for its Global Bedrock key — a
    runtime-linked workload identity cannot self-mint one.

    ``actor_id`` (N16-obs79) travels as ``message.metadata.launchpad_actor_id`` — the
    A2A spec's free-form message metadata, the only field of the envelope the Runtime
    forwards to the container. The A2A template binds it to the context and keys its
    memory (S3 snapshot + turn events + recall) on it, so an A2A agent records memory
    under the same ``<agent>__<human>`` actor the HTTP template receives as
    ``actor_id`` and the console reads back. Omitted (a peer-to-peer call), the
    template keeps its per-context actor. N16-obs90: the actor is sent ONLY together with
    ``launchpad_actor_sig`` = ``a2a_actor.sign_actor(actor_key, contextId, actor_id)`` —
    the template refuses an unsigned or mis-signed actor — so a caller without the agent's
    key (``services/a2a_actor.actor_key``) sends no actor metadata at all.
    """
    session_id = session_id or new_session_id()
    parts = [{"kind": "text", "text": prompt}]
    for item in attachments or []:
        parts.append({
            "kind": "file",
            "file": {"name": item["name"], "mimeType": item["media_type"], "bytes": item["data"]},
        })
    message: dict[str, Any] = {
        "role": "user",
        "messageId": uuid.uuid4().hex,
        "contextId": session_id,
        "parts": parts,
    }
    if actor_id and actor_key:
        message["metadata"] = {
            A2A_ACTOR_METADATA_KEY: actor_id,
            A2A_ACTOR_SIG_METADATA_KEY: a2a_actor.sign_actor(actor_key, session_id, actor_id),
        }
    if principal_token:
        # stream A2 (contract I2): the A2A server forwards it in `_meta` on its tool calls
        message.setdefault("metadata", {})[PRINCIPAL_METADATA_KEY] = principal_token
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {"message": message},
    }
    params: dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "runtimeSessionId": session_id,
        "payload": json.dumps(payload).encode("utf-8"),
    }
    if runtime_user_id:
        params["runtimeUserId"] = runtime_user_id[:1024]
    response = client.invoke_agent_runtime(**params)
    body = json.loads(response["response"].read())
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise RuntimeError(
            f"A2A error {err.get('code', '?')}: {err.get('message', '')}"
        )
    result = body.get("result") if isinstance(body, dict) else None
    result = result if isinstance(result, dict) else {}
    text = a2a_result_text(result)
    if not text:
        # N10v3-bug5: a failed Task has no artifacts — its reason sits in status.message.
        # An AppError (502) is what turns "meta … done, no text" into a readable chat
        # error event and a /v1 invoke into a 502 with the reason, not a bare 500.
        raise AppError("agent.invoke_failed", a2a_failure_detail(result), status_code=502)
    return {"text": text, "session_id": session_id}
