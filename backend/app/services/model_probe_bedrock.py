"""Global Bedrock transport for the model probes (D21 plan §7; M1-b).

The `bedrock-runtime` client is the bearer-authenticated Global one from
`aws_clients.global_bedrock_runtime_client(model_region)` — the single cross-partition call
the CN build allows (`region-boundary: global-bedrock`); it is handed in by
`model_probe.ModelProbeHooks` (injectable in tests). Timeouts are the shared client's; the
run's time budget is checked between calls so a slow model leaves the later fields
unsettled rather than holding the request thread.

Error texts leave this module only as ``(code, sanitised message)`` pairs.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

from app.services.model_probe_openai import ProbeBudgetExhausted, sanitize

PROBE_PROMPT = "Reply with the single word OK."
# GLM reasons; below ~200 output tokens the text block comes back empty (plan §7.1, C3 note).
PROBE_MAX_TOKENS = 256
PING_TOOL_SPEC: dict[str, Any] = {
    "toolSpec": {"name": "ping", "description": "ping",
                 "inputSchema": {"json": {"type": "object", "properties": {}}}},
}
_MODEL_MISSING_RE = re.compile(
    r"model (id|identifier)|provided model|(not found|isn't supported|is not supported|"
    r"invalid|unsupported|does not exist|doesn't exist|no access to).*model|"
    r"model.*(not found|isn't supported|is not supported|invalid|unsupported|does not "
    r"exist|doesn't exist|no access)",
    re.I,
)


def error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or "ClientError")


def error_message(exc: ClientError) -> str:
    return sanitize(f"{error_code(exc)}: {exc.response.get('Error', {}).get('Message') or ''}")


def is_model_missing(exc: ClientError) -> bool:
    """`ResourceNotFoundException`, or the `ValidationException` texts Bedrock answers for
    an unknown / unsupported model id (plan §7.2)."""
    code = error_code(exc)
    if code == "ResourceNotFoundException":
        return True
    message = str(exc.response.get("Error", {}).get("Message") or "")
    return code == "ValidationException" and bool(_MODEL_MISSING_RE.search(message))


def rejects_field(exc: ClientError, *names: str) -> bool:
    """The N12-bug11 signature: `ValidationException … doesn't support the temperature
    field` (Kimi K3) — a *rejection* of the named inference field, not a model problem."""
    if error_code(exc) != "ValidationException":
        return False
    message = str(exc.response.get("Error", {}).get("Message") or "").lower()
    return any(name.lower() in message for name in names)


class BedrockProbe:
    """`Converse` / `ConverseStream` / `InvokeModel` smoke calls within one time budget."""

    def __init__(self, runtime: Any, *, deadline: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._runtime = runtime
        self._deadline = deadline
        self._clock = clock

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def _check_budget(self) -> None:
        if self.remaining() <= 0.05:
            raise ProbeBudgetExhausted("probe time budget exhausted before the request")

    def converse(self, model_id: str, **extra: Any) -> dict[str, Any]:
        """One `Converse` with the minimal prompt; ``extra`` adds `toolConfig` or more
        `inferenceConfig` fields (merged over `maxTokens`)."""
        self._check_budget()
        inference = {"maxTokens": PROBE_MAX_TOKENS, **extra.pop("inferenceConfig", {})}
        return self._runtime.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": PROBE_PROMPT}]}],
            inferenceConfig=inference,
            **extra,
        )

    def converse_stream_first_event(self, model_id: str) -> dict[str, Any] | None:
        """`ConverseStream`; returns the first event (or None when the stream is empty)."""
        self._check_budget()
        response = self._runtime.converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": PROBE_PROMPT}]}],
            inferenceConfig={"maxTokens": PROBE_MAX_TOKENS},
        )
        stream = response.get("stream") if isinstance(response, dict) else None
        if stream is None:
            return None
        for event in stream:
            return event if isinstance(event, dict) else {"event": event}
        return None

    def embedding_dimension(self, model_id: str) -> int | None:
        """`InvokeModel` with the Titan family body; the vector length, or None when the
        response carries no `embedding` list (another Bedrock embedder → `manual`)."""
        self._check_budget()
        response = self._runtime.invoke_model(
            modelId=model_id, body=json.dumps({"inputText": "ping"}),
            contentType="application/json", accept="application/json",
        )
        raw = response.get("body") if isinstance(response, dict) else None
        payload = raw.read() if hasattr(raw, "read") else raw
        if isinstance(payload, bytes | bytearray):
            payload = payload.decode("utf-8", errors="replace")
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            return None
        vector = data.get("embedding") if isinstance(data, dict) else None
        return len(vector) if isinstance(vector, list) and vector else None
