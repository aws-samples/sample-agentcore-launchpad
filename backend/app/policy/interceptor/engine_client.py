"""I4 engine data-plane client of the interceptor (stream B2; contract
docs/cn/interfaces/policy-principal-contracts.md §I4, design §13.5).

Routing (§13.5 ownership rule): a call whose action is referenced by an ACTIVE temporal
policy goes to the session's OWNER shard with ``record: true`` and the 1500 ms budget, and
never fails over — its history lives there. Every other call is stateless: the owner shard
first (100 ms), then ONE retry on the next shard (50 ms), ``record: false``.

The transport is injected (``Transport``), so tests drive the stage-0 ``FakeEngine`` and the
Lambda uses ``http_transport`` (stdlib ``http.client``; the engine listens on the hub host's
private IP, plain HTTP inside the VPC, authenticated by the HMAC headers).

SELF-CONTAINED (stdlib + the vendored ``codec``): shipped in the interceptor Lambda zip.
"""

from __future__ import annotations

import http.client
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import codec

STATELESS_TIMEOUT_S = 0.100
STATELESS_RETRY_TIMEOUT_S = 0.050
OWNER_TIMEOUT_S = 1.500
LIST_FILTER_TIMEOUT_S = 0.250

# (endpoint "ip:port", method, path, body bytes, headers, timeout s) -> (status, json body)
Transport = Callable[[str, str, str, bytes, dict[str, str], float], tuple[int, dict[str, Any]]]


class EngineUnavailable(Exception):
    """No usable answer: connection refused / reset, timeout, 5xx, or a body that is not
    the I4 shape. ``code`` is what the audit row and the deny reason carry."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class EngineRejected(Exception):
    """A 4xx I4 error body (``session_invalidated`` | ``history_lost`` | ``set_mismatch``
    | ``bad_request`` | ``unauthorized``) — the engine answered, the answer is "no"."""

    def __init__(self, status: int, code: str, body: dict[str, Any]) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.body = body


def http_transport(endpoint: str, method: str, path: str, body: bytes,
                   headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any]]:
    host, _, port = endpoint.rpartition(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request(method, path, body=body or None,
                     headers={**headers, "Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(1 << 20)
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"code": "unavailable", "message": "non-JSON engine answer"}
        return response.status, payload if isinstance(payload, dict) else {}
    finally:
        conn.close()


@dataclass
class EngineClient:
    endpoints: list[str]
    shards: int
    key: bytes
    transport: Transport = http_transport
    clock: Callable[[], float] = time.time
    calls: list[dict[str, Any]] = field(default_factory=list)  # (endpoint, path, ms) trail

    def _endpoint(self, shard: int) -> str:
        return self.endpoints[shard % len(self.endpoints)]

    def _post(self, shard: int, path: str, payload: dict[str, Any],
              timeout: float) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        ts = str(int(self.clock()))
        nonce = uuid.uuid4().hex
        headers = {
            codec_header("ts"): ts,
            codec_header("nonce"): nonce,
            codec_header("sig"): codec.engine_signature(self.key, "POST", path, ts, nonce, body),
        }
        endpoint = self._endpoint(shard)
        started = time.perf_counter()
        try:
            status, answer = self.transport(endpoint, "POST", path, body, headers, timeout)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise EngineUnavailable("unavailable", type(exc).__name__) from None
        finally:
            self.calls.append({"endpoint": endpoint, "path": path,
                               "ms": round((time.perf_counter() - started) * 1000, 3)})
        if status >= 500:
            raise EngineUnavailable("unavailable", str(answer.get("code") or status))
        if status >= 400:
            raise EngineRejected(status, str(answer.get("code") or "bad_request"), answer)
        if status != 200 or not isinstance(answer, dict):
            raise EngineUnavailable("unavailable", f"status {status}")
        return answer

    def decide(self, request: dict[str, Any], *, owner_only: bool) -> dict[str, Any]:
        """``/v1/decide``. ``owner_only`` = the action is temporal-referenced: the owner
        shard, recorded, no failover. Otherwise stateless with one retry elsewhere."""
        owner = codec.shard_for(request["session"], self.shards)
        if owner_only:
            answer = self._post(owner, "/v1/decide", {**request, "record": True},
                                OWNER_TIMEOUT_S)
        else:
            stateless = {**request, "record": False}
            try:
                answer = self._post(owner, "/v1/decide", stateless, STATELESS_TIMEOUT_S)
            except EngineUnavailable:
                if self.shards < 2 or len(self.endpoints) < 2:
                    raise
                answer = self._post(owner + 1, "/v1/decide", stateless,
                                    STATELESS_RETRY_TIMEOUT_S)
        if answer.get("decision") not in ("allow", "deny"):
            raise EngineUnavailable("unavailable", "decide answer without a decision")
        return answer

    def respond(self, request: dict[str, Any]) -> dict[str, Any]:
        """``/v1/respond`` — the response / error event, always on the owner shard."""
        owner = codec.shard_for(request["session"], self.shards)
        return self._post(owner, "/v1/respond", request, OWNER_TIMEOUT_S)

    def list_filter(self, request: dict[str, Any], session: str) -> list[str]:
        """``/v1/list-filter`` (read-only): any shard, the session's owner first."""
        owner = codec.shard_for(session, self.shards)
        try:
            answer = self._post(owner, "/v1/list-filter", request, LIST_FILTER_TIMEOUT_S)
        except EngineUnavailable:
            if self.shards < 2 or len(self.endpoints) < 2:
                raise
            answer = self._post(owner + 1, "/v1/list-filter", request, LIST_FILTER_TIMEOUT_S)
        visible = answer.get("visible")
        if not isinstance(visible, list):
            raise EngineUnavailable("unavailable", "list-filter answer without visible[]")
        return [str(v) for v in visible]


def codec_header(name: str) -> str:
    return {"ts": "X-LP-Ts", "nonce": "X-LP-Nonce", "sig": "X-LP-Sig"}[name]
