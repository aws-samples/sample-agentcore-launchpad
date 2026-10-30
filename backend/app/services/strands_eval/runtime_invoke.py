"""The worker's own ``InvokeAgentRuntime`` text call (N12 E1-3b).

The backend replays through ``services/agentcore/runtime.py``; the worker zip cannot import
``app.*``, so this is the compact twin it ships: the same ``{prompt, actor_id[, launchpad]}``
body every Launchpad runtime accepts, the pre-assigned ``runtimeSessionId`` / ``qualifier``
/ ``baggage`` (plan §B.0: routing is decided in the backend, the worker only executes),
and a tolerant reader of the platform's SSE envelope (``delta`` / ``complete`` / ``error``)
or a plain JSON body. Pure: the data-plane client is injected.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

ACTOR_ID = "launchpad-eval"
# ``runtimeClientError`` is the mid-stream event; ``RuntimeClientError`` the same failure
# raised by the invoke call itself, with a 4xx status (upstream #227, live 2026-10-08:
# "Runtime health check failed or timed out" failed a whole 19-scenario run at one scenario
# because only the event spelling was known and the status was not a 5xx).
_TRANSIENT_CODES = frozenset({
    "runtimeClientError", "RuntimeClientError", "internalServerException",
    "InternalServerException",
    "throttlingException", "ThrottlingException", "serviceUnavailableException",
    "ServiceUnavailableException",
})
# N-SCN-bug1: a socket-level timeout / connection failure between the worker and the data
# plane is a hiccup of the SAME kind as a 5xx — the runtime may well have answered after we
# stopped listening, and the next attempt (same pre-assigned session id) usually succeeds.
_TRANSIENT_BOTOCORE: tuple[type[BaseException], ...] = (
    ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError,
)

# Markers of a transient UPSTREAM failure in an error's text — a Runtime-level event, the
# AgentCore SDK's terminal error frame (``runtime returned error: EventStreamError … An error
# occurred (internalServerException) when calling the ConverseStream operation … Try your
# request again``), a botocore error code, the Harness's ``runtimeClientError`` event. ONE
# list for the architect assistant's replay (N-ARCH-obs18, cn/r11-j; its ``ClientError``
# branch is this predicate since upstream #227's health-check fix), the evaluation
# worker's scenario replay (N12-obs55) and its judge calls (N12-obs53) — this module is
# shipped into the worker zip, which cannot import ``app.*``, so the list lives here and
# ``services/agentcore/runtime.py`` re-exports it for the hub. Bare status codes only in
# anchored forms — never a substring of a longer number.
UPSTREAM_TRANSIENT_MARKERS: tuple[str, ...] = (
    "internalserverexception", "internalservererror", "internal server error",
    "runtimeclienterror", "runtime client error", "runtime health check",
    "throttl", "serviceunavailable", "service unavailable", "too many requests",
    "readtimeout", "read timed out", "timed out",
    "modelerror", "model_error",
    "http 500", "status 500", "status code 500", "status_code 500", " 500 ",
    "http 503", "status 503", "status code 503", "status_code 503", " 503 ",
)
# How long the worker waits before the 2nd / 3rd attempt on a transient upstream error
# (hub ``Settings.eval_transient_retry_backoff_seconds``, default "5,15"; the last value
# repeats when there are more retries than entries). N12-obs55 (live 2026-10-16 13:29Z): a
# retry fired straight into the same Bedrock ``internalServerException``.
DEFAULT_TRANSIENT_BACKOFF_S: tuple[float, ...] = (5.0, 15.0)
# N13-obs8: a backoff wait is sliced into sleeps of at most this many seconds, polling the
# job's stop predicate (the S3 cancel marker) between slices — a cancel during a 15 s wait is
# honoured within about a second instead of after the whole wait.
BACKOFF_SLICE_S = 1.0


def upstream_error_text(exc_or_text: BaseException | str | None) -> str:
    """The lower-cased text :func:`is_upstream_transient` scans: a string as is; an
    exception as its class name, its SDK-frame ``error_type`` (``RuntimeStreamError`` /
    ``SdkStreamError``), the botocore error code and its message."""
    if exc_or_text is None:
        return ""
    if isinstance(exc_or_text, str):
        return exc_or_text.lower()
    parts = [type(exc_or_text).__name__, str(getattr(exc_or_text, "error_type", "") or "")]
    if isinstance(exc_or_text, ClientError):
        error = exc_or_text.response.get("Error") or {}
        parts.append(str(error.get("Code") or ""))
    parts.append(str(exc_or_text))
    return " ".join(p for p in parts if p).lower()


def is_upstream_transient(exc_or_text: BaseException | str | None) -> bool:
    """A retry-worthy upstream hiccup, from an exception or its bare text: throttling / a
    5xx (botocore code or HTTP status), a mid-stream ``runtimeClientError`` /
    ``internalServerException`` event, the SDK's terminal error frame naming one, a
    botocore read / connect timeout or an unreachable endpoint. A caller error
    (``ValidationException``, a 4xx, ``ValueError``), a ``max_tokens`` stop, a telemetry
    readiness timeout and the Harness's own non-upstream refusals are NOT transient."""
    if isinstance(exc_or_text, _TRANSIENT_BOTOCORE):
        return True
    if isinstance(exc_or_text, ClientError):
        error = exc_or_text.response.get("Error") or {}
        status = (exc_or_text.response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0
        if error.get("Code") in _TRANSIENT_CODES or int(status) >= 500:
            return True
    text = upstream_error_text(exc_or_text)
    if not text:
        return False
    padded = f" {text} "  # so an anchored " 500 " also matches at either end
    return any(marker in padded for marker in UPSTREAM_TRANSIENT_MARKERS)


def is_transient_invoke_error(exc: BaseException) -> bool:
    """A replay-worthy upstream hiccup on the scenario replay — :func:`is_upstream_transient`
    (kept under the N-SCN-bug1 name the replay loop, its tests and the docs use)."""
    return is_upstream_transient(exc)


def backoff_for(delays: tuple[float, ...] | list[float] | None, retry_index: int) -> float:
    """The wait before retry number ``retry_index`` (0-based): ``delays[retry_index]``, the
    last entry when there are more retries than entries, 0 when ``delays`` is empty."""
    if not delays:
        return 0.0
    index = min(max(int(retry_index), 0), len(delays) - 1)
    return max(0.0, float(delays[index]))


def sliced_wait(seconds: float, sleep: Callable[[float], None],
                stop: Callable[[], bool] | None = None, *,
                slice_s: float = BACKOFF_SLICE_S) -> bool:
    """Wait ``seconds`` in slices of at most ``slice_s`` (N13-obs8), polling ``stop()``
    before each slice and once after the last — the cancel-aware pattern of the telemetry
    poll loop (``jobs_batch._wait_for_spans.cancel_aware_sleep``), applied to the transient
    retry backoff of the scenario replay and the judge calls. Returns ``True`` when the
    full wait elapsed with no stop, ``False`` as soon as ``stop()`` is true (nothing more is
    slept). Without ``stop`` it is one sliced sleep. ``seconds <= 0`` never sleeps."""
    remaining = float(seconds)
    while remaining > 0:
        if stop is not None and stop():
            return False
        step = min(float(slice_s), remaining)
        sleep(step)
        remaining -= step
    return not (stop is not None and stop())


def retry_reason(exc: BaseException, limit: int = 200) -> str:
    """``<Type>: <message>`` bounded to ``limit`` chars — what a retry records as its cause
    (never the prompt or the reply)."""
    text = " ".join(str(exc).split())
    reason = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return reason if len(reason) <= limit else reason[:limit] + "…"


def _sse_payloads(text: str) -> list[Any]:
    out: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            out.append(raw)
    return out


def _converse_frame(event: dict[str, Any]) -> tuple[str, bool]:
    """``(delta text, starts a tool call)`` of one Converse stream event (N17-obs419)."""
    start = event.get("contentBlockStart")
    start = start.get("start") if isinstance(start, dict) else None
    tool = isinstance(start, dict) and isinstance(start.get("toolUse"), dict)
    block = event.get("contentBlockDelta")
    delta = block.get("delta") if isinstance(block, dict) else None
    text = delta.get("text") if isinstance(delta, dict) else None
    return (str(text) if text else "", tool)


def _collect(payloads: list[Any]) -> str:
    """The reply's final answer: the text after the last tool call — the bubble the chat
    shows last (a ``tool`` event closes the open bubble, ``chat_turns``) — else the
    ``complete`` result. A pre-tool preamble is not the answer (N17-obs315). The self-built
    Harness runtime streams raw Strands / Converse frames — ``{"event": {"contentBlockDelta":
    …}}``, a ``toolUse`` ``contentBlockStart`` — read the same way (N17-obs419: they were
    ignored and every harness turn collected "")."""
    deltas: list[str] = []
    final: str | None = None
    for payload in payloads:
        if not isinstance(payload, dict):
            if isinstance(payload, str) and payload:
                final = payload
            continue
        if payload.get("error"):
            raise RuntimeError(f"runtime returned error: {payload['error']}")
        kind = payload.get("event")
        if isinstance(kind, dict):
            text, tool = _converse_frame(kind)
            if tool:
                deltas = []
            if text:
                deltas.append(text)
            continue
        if kind == "delta" and payload.get("text"):
            deltas.append(str(payload["text"]))
        elif kind == "tool":
            deltas = []
        elif kind == "complete":
            final = str(payload.get("result", ""))
        elif kind == "error":
            raise RuntimeError(str(payload.get("message", "runtime stream failed")))
        elif kind is None:
            if "runtimeClientError" in payload or "internalServerException" in payload:
                detail = payload.get("runtimeClientError") or payload.get("internalServerException")
                raise RuntimeError(f"runtime client error: {detail}")
            if "result" in payload:
                final = str(payload.get("result", ""))
    return "".join(deltas) if deltas else (final or "")


def invoke_text(
    client: Any,
    runtime_arn: str,
    prompt: str,
    *,
    session_id: str,
    actor_id: str = ACTOR_ID,
    qualifier: str | None = None,
    runtime_user_id: str | None = None,
    baggage: str | None = None,
    launchpad_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronous invoke; returns ``{"text", "session_id"}``."""
    body: dict[str, Any] = {"prompt": prompt, "actor_id": actor_id}
    if launchpad_meta:
        body["launchpad"] = dict(launchpad_meta)
    params: dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "runtimeSessionId": session_id,
        "payload": json.dumps(body).encode("utf-8"),
    }
    if qualifier:
        params["qualifier"] = qualifier
    if runtime_user_id:
        params["runtimeUserId"] = runtime_user_id[:1024]
    if baggage:
        params["baggage"] = baggage
    response = client.invoke_agent_runtime(**params)
    stream = response["response"]
    raw = stream.read()
    if hasattr(stream, "close"):
        stream.close()
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    content_type = str(response.get("contentType", "")).lower()
    if "text/event-stream" in content_type or text.lstrip().startswith("data:"):
        payloads = _sse_payloads(text)
    else:
        try:
            payloads = [json.loads(text)] if text else []
        except ValueError:
            payloads = [text]
    return {"text": _collect(payloads), "session_id": session_id}
