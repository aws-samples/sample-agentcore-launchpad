"""Thin wrappers over the AgentCore Runtime control/data APIs.

Explicit-client style (tests inject stubs). Shapes per bedrock-agentcore-control
1.43.x: runtime status enum is CREATING → READY (or CREATE_FAILED).
"""

import json
import time
import uuid
from collections.abc import Iterable, Iterator
from typing import Any

from app.services.agentcore.harness import new_session_id

TERMINAL_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED"}
SSE_READ_CHUNK_BYTES = 32


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
) -> dict[str, Any]:
    """CreateAgentRuntime from a zip on S3, instrumented via ADOT."""
    params: dict[str, Any] = {
        "agentRuntimeName": runtime_name,
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {"s3": {"bucket": s3_bucket, "prefix": s3_key}},
                "runtime": "PYTHON_3_13",
                "entryPoint": ["opentelemetry-instrument", "main.py"],
            }
        },
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "roleArn": role_arn,
    }
    if environment:
        params["environmentVariables"] = dict(environment)
    proto = _protocol_configuration(protocol)
    if proto:
        params["protocolConfiguration"] = proto
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
) -> dict[str, Any]:
    """CreateAgentRuntime from an ECR image (Claude SDK container path)."""
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
    return client.create_agent_runtime(**params)


def _code_artifact(s3_bucket: str, s3_key: str) -> dict[str, Any]:
    return {
        "codeConfiguration": {
            "code": {"s3": {"bucket": s3_bucket, "prefix": s3_key}},
            "runtime": "PYTHON_3_13",
            "entryPoint": ["opentelemetry-instrument", "main.py"],
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
) -> dict[str, Any]:
    """UpdateAgentRuntime with a new zip artifact — publishes a new version in
    place (same agentRuntimeId/ARN; the DEFAULT endpoint auto-rolls to it).

    ``protocol`` must be passed for A2A agents on EVERY update — the service
    resets an omitted protocolConfiguration back to HTTP (probed live)."""
    params: dict[str, Any] = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": _code_artifact(s3_bucket, s3_key),
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "roleArn": role_arn,
    }
    if environment is not None:
        params["environmentVariables"] = dict(environment)
    proto = _protocol_configuration(protocol)
    if proto:
        params["protocolConfiguration"] = proto
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
) -> dict[str, Any]:
    """UpdateAgentRuntime with a new container image — new version, same ARN.
    NB: a version bump resets managed session storage (documented UI note)."""
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


def _runtime_payload_events(payload: Any) -> Iterator[dict[str, Any]]:
    """Normalize one runtime payload to Chat's tool/delta/complete contract."""
    if not isinstance(payload, dict):
        text = str(payload)
        if text:
            yield {"event": "complete", "data": {"text": text}}
        return

    if payload.get("error"):
        raise RuntimeError(f"runtime returned error: {payload['error']}")

    kind = payload.get("event")
    if isinstance(kind, str):
        if kind == "delta" and payload.get("text"):
            yield {"event": "delta", "data": {"text": str(payload["text"])}}
        elif kind == "heartbeat":
            yield {"event": "heartbeat", "data": {}}
        elif kind == "tool":
            yield {
                "event": "tool",
                "data": {"name": str(payload.get("name", "")), "id": payload.get("id")},
            }
        elif kind == "complete":
            yield {"event": "complete", "data": {"text": str(payload.get("result", ""))}}
        elif kind == "error":
            raise RuntimeError(str(payload.get("message", "runtime stream failed")))
        return

    inner = kind if isinstance(kind, dict) else payload
    if "runtimeClientError" in inner or "internalServerException" in inner:
        detail = inner.get("runtimeClientError") or inner.get("internalServerException")
        raise RuntimeError(f"runtime returned error: {detail}")
    tool_use = inner.get("contentBlockStart", {}).get("start", {}).get("toolUse")
    if isinstance(tool_use, dict):
        yield {
            "event": "tool",
            "data": {"name": tool_use.get("name", ""), "id": tool_use.get("toolUseId")},
        }
    delta = inner.get("contentBlockDelta", {}).get("delta", {})
    if isinstance(delta, dict) and delta.get("text"):
        yield {"event": "delta", "data": {"text": str(delta["text"])}}
    if "result" in payload:
        yield {"event": "complete", "data": {"text": str(payload.get("result", ""))}}


def _normalized_runtime_events(payloads: Iterable[Any]) -> Iterator[dict[str, Any]]:
    """Suppress a final full result when real deltas were already emitted."""
    saw_delta = False
    for payload in payloads:
        for event in _runtime_payload_events(payload):
            if event["event"] == "delta":
                saw_delta = True
                yield event
            elif event["event"] == "complete":
                if not saw_delta and event["data"]["text"]:
                    saw_delta = True
                    yield {"event": "delta", "data": event["data"]}
            else:
                yield event


def _runtime_invoke_params(
    runtime_arn: str,
    prompt: str,
    session_id: str,
    actor_id: str,
    qualifier: str | None,
    runtime_user_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "runtimeSessionId": session_id,
        "payload": json.dumps({"prompt": prompt, "actor_id": actor_id}).encode("utf-8"),
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


def stream_runtime_events(
    client: Any,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
    runtime_user_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Invoke a runtime and yield normalized tool/text events as bytes arrive."""
    session_id = session_id or new_session_id()
    response = client.invoke_agent_runtime(
        **_runtime_invoke_params(
            runtime_arn, prompt, session_id, actor_id, qualifier, runtime_user_id
        )
    )
    body = response["response"]
    content_type = str(response.get("contentType", "")).lower()
    if "text/event-stream" in content_type:
        lines = (
            body.iter_lines(chunk_size=SSE_READ_CHUNK_BYTES)
            if hasattr(body, "iter_lines")
            else body.read().splitlines()
        )
        yield from _normalized_runtime_events(_sse_payloads(lines))
        return

    raw = body.read()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        decoded = raw.decode("utf-8", errors="replace") if raw else ""
        if decoded.lstrip().startswith("data:"):
            yield from _normalized_runtime_events(_sse_payloads(decoded.splitlines()))
        elif decoded:
            yield from _normalized_runtime_events([decoded])
    else:
        yield from _normalized_runtime_events([payload])


def invoke_runtime_text(
    client: Any,
    runtime_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
    qualifier: str | None = None,
    runtime_user_id: str | None = None,
) -> dict[str, Any]:
    """Synchronous InvokeAgentRuntime, joining native streaming responses."""
    session_id = session_id or new_session_id()
    parts = [
        event["data"]["text"]
        for event in stream_runtime_events(
            client,
            runtime_arn,
            prompt,
            session_id=session_id,
            actor_id=actor_id,
            qualifier=qualifier,
            runtime_user_id=runtime_user_id,
        )
        if event["event"] == "delta"
    ]
    return {"text": "".join(parts), "session_id": session_id}


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
) -> dict[str, Any]:
    """JSON-RPC message/send against an A2A-protocol runtime.

    InvokeAgentRuntime passes the JSON-RPC envelope through unmodified for
    serverProtocol=A2A runtimes; the A2A server owns conversation state, so
    there is no actor_id/memory envelope here.
    """
    session_id = session_id or new_session_id()
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": uuid.uuid4().hex,
                "contextId": session_id,
                "parts": [{"kind": "text", "text": prompt}],
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
