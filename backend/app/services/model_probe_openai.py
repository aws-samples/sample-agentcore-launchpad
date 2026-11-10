"""OpenAI-compatible endpoint transport for the model probes (D21 plan §7; M1-b).

One `httpx.Client` per probe run with the egress discipline of plan §9.3: strict
timeouts (connect 5 s / read ≤ 20 s, both capped by the run's remaining budget), **no
redirects**, **no proxy environment** (`trust_env=False` — no proxy credentials travel), bounded
bodies (1 MiB of JSON, 64 KiB of an SSE stream) and the SSRF guard re-run **before every
request** (`guard` — `model_probe.guard_base_url`; DNS may change between two requests).
Vendor error text leaves this module only through `sanitize()`: the key, `Bearer …`,
`sk-…`-looking tokens and query strings are redacted and the text is cut at 300 chars.

The transport is injectable (`httpx.MockTransport` in tests); nothing here reads settings.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 20.0
MAX_BODY_BYTES = 1 << 20
MAX_STREAM_BYTES = 64 << 10
MAX_ERROR_CHARS = 300
PROBE_PROMPT = "Reply with the single word OK."
PROBE_MAX_TOKENS = 16
USER_AGENT = "launchpad-model-probe/1"
PING_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {"name": "ping", "description": "ping",
                 "parameters": {"type": "object", "properties": {}}},
}

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"),
    re.compile(r"(?i)\b(api[_-]?key|authorization|x-api-key|token)\b\s*[:=]\s*\"?"
               r"[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(r"\?[^\s\"'<>]*"),  # query strings — some gateways carry the key there
)
_WS = re.compile(r"\s+")


def sanitize(text: Any, secrets: Iterable[str] = (), limit: int = MAX_ERROR_CHARS) -> str:
    """Vendor text safe to store / return: known secrets and secret-looking substrings
    redacted, whitespace collapsed, ≤ ``limit`` chars (plan §7.4)."""
    out = str(text or "")
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    out = _WS.sub(" ", out).strip()
    return out[:limit]


class ProbeError(RuntimeError):
    """A probe step that could not be completed; ``str()`` is sanitised and storable."""


class ProbeBlocked(ProbeError):
    """The SSRF / allowlist guard refused the endpoint — every further request would too."""


class ProbeBudgetExhausted(ProbeError):
    """The run's time budget is spent — the remaining fields stay unsettled."""


class ProbeTransportError(ProbeError):
    """Timeout, connection failure, refused redirect or an unexpected status."""


@dataclass
class ProbeResponse:
    status: int
    body: Any  # parsed JSON (dict / list) or None
    text: str  # bounded raw text (NOT sanitised — call `OpenAICompatibleProbe.sanitize`)
    first_chunk: str | None = None  # SSE: the first `data:` line, when streaming

    def message(self) -> str:
        """The vendor's `error.message` when the body has one, else the raw text."""
        if isinstance(self.body, dict):
            error = self.body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if isinstance(error, str):
                return error
            if self.body.get("message"):
                return str(self.body["message"])
        return self.text

    def names(self, *needles: str) -> bool:
        """Whether a 4xx body names one of ``needles`` (a rejected field / feature)."""
        if not 400 <= self.status < 500:
            return False
        haystack = f"{self.message()} {self.text}".lower()
        return any(needle.lower() in haystack for needle in needles)


class OpenAICompatibleProbe:
    """The requests of plan §7.1–7.3 against one endpoint, within one time budget."""

    def __init__(self, base_url: str, api_key: str, *, guard: Callable[[str], str],
                 deadline: float, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self._secrets = (api_key,)
        self._guard = guard
        self._deadline = deadline
        self._clock = clock
        self._client = httpx.Client(
            transport=transport, follow_redirects=False, trust_env=False,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT,
                     "Accept": "application/json"},
        )

    # ── lifecycle ──
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAICompatibleProbe:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def sanitize(self, text: Any) -> str:
        return sanitize(text, self._secrets)

    def remaining(self) -> float:
        return self._deadline - self._clock()

    # ── one bounded request ──
    def request(self, method: str, path: str, payload: dict[str, Any] | None = None, *,
                stream: bool = False) -> ProbeResponse:
        remaining = self.remaining()
        if remaining <= 0.05:
            raise ProbeBudgetExhausted("probe time budget exhausted before the request")
        try:
            root = self._guard(self.base_url)
        except Exception as exc:  # noqa: BLE001 — AppError / HostBlockedError / resolver OSError
            raise ProbeBlocked(self.sanitize(getattr(exc, "message", None) or str(exc))) from exc
        timeout = httpx.Timeout(
            connect=min(CONNECT_TIMEOUT_S, remaining), read=min(READ_TIMEOUT_S, remaining),
            write=min(CONNECT_TIMEOUT_S, remaining), pool=min(CONNECT_TIMEOUT_S, remaining),
        )
        request = self._client.build_request(method, f"{root}{path}", json=payload,
                                             timeout=timeout)
        try:
            response = self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise ProbeTransportError(
                f"timeout ({type(exc).__name__}) after {min(READ_TIMEOUT_S, remaining):.0f}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProbeTransportError(self.sanitize(f"{type(exc).__name__}: {exc}")) from exc
        try:
            if 300 <= response.status_code < 400:
                raise ProbeTransportError(
                    f"HTTP {response.status_code} redirect refused (probes follow no redirects)")
            if stream and response.status_code == 200:
                first: str | None = None
                size = 0
                for line in response.iter_lines():
                    size += len(line) + 1
                    if line.startswith("data:"):
                        first = line[:1024]
                        break
                    if size > MAX_STREAM_BYTES:
                        break
                return ProbeResponse(200, None, "", first)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_BODY_BYTES:
                    break
            text = b"".join(chunks)[:MAX_BODY_BYTES].decode("utf-8", errors="replace")
            body: Any = None
            if text.strip():
                try:
                    body = json.loads(text)
                except ValueError:
                    body = None
            return ProbeResponse(response.status_code, body, text)
        except httpx.TimeoutException as exc:
            raise ProbeTransportError(f"timeout while reading ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise ProbeTransportError(self.sanitize(f"{type(exc).__name__}: {exc}")) from exc
        finally:
            response.close()

    # ── the §7 requests ──
    def list_models(self) -> ProbeResponse:
        return self.request("GET", "/models")

    def chat(self, model: str, **extra: Any) -> ProbeResponse:
        """`POST /chat/completions` with the minimal body (``max_tokens`` 16, one user
        turn, ``stream`` false) plus ``extra`` (tools / stream / temperature / top_p)."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": PROBE_MAX_TOKENS,
            "stream": False,
        }
        payload.update(extra)
        return self.request("POST", "/chat/completions", payload, stream=bool(payload["stream"]))

    def embed(self, model: str) -> ProbeResponse:
        return self.request("POST", "/embeddings", {"model": model, "input": "ping"})
