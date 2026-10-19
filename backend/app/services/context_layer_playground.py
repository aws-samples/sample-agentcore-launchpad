"""COA playground: one streamed question to COA's Serve runtime, as the member.

``POST /api/context-layer/playground`` proxies COA's Serve runtime
(``POST <serve invocations URL>`` with ``stream: true``) with the member's own COA
access token, so COA's grants decide what the answer may use. COA frames
(``data: {type: step|token|done|error, requestId, timestamp, payload, sessionId?}``,
COA ``coa_serve/sse_emitter.py``) are normalized to Launchpad's SSE events:

=========  =====================================================================
event      data
=========  =====================================================================
step       ``{name, status, duration_ms, tool, detail}`` (detail ≤ 300 chars)
token      ``{text}`` — answer text as COA streams it (Tier 3)
done       ``{answer, tier, confidence, rationale, columns, rows (≤ 200),
           row_count, rows_truncated, partial, guardrail_blocked,
           data_sources, citations, total_ms, session_id}``
error      ``{code, message, status, coa_error}`` — ``context_layer.*`` codes
=========  =====================================================================

plus ``auth_required`` (the as_user ask shape, ``tool: "context_layer"``) when the
member has not consented, and a ``: keep-alive`` comment whenever COA is silent for
``HEARTBEAT_S`` (a reasoning step can run for a minute; proxies drop idle streams).

**Custody.** The token rides only the request to COA (``Authorization``). No event,
log line or error carries it; errors name COA's status and at most 200 characters
of COA's own message. **Schema.** The request never sets ``options.includeDebugInfo``
and ``done`` never carries ``queryUsed`` / ``sparqlGenerated``: COA withholds them by
default because they expose internal schema (tables, columns, joins), and a member
granted a namespace's answers is not thereby trusted with its schema.
**Lifetime.** The upstream is an ``httpx.AsyncClient`` stream inside this generator:
a client disconnect (checked per frame and per heartbeat) or the generator's close
exits the ``async with`` and closes COA's connection. COA's own budget is 170 s; the
read timeout here is 180 s.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from app.schemas.context_layer import integer, strings, text
from app.services import context_layer_tokens as tokens
from app.services.agentcore.runtime import BEARER_SESSION_HEADER
from app.services.context_layer import ContextLayer
from app.services.context_layer_api import COA_MESSAGE_MAX

logger = logging.getLogger("launchpad.context_layer.playground")

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 180.0
# a little over COA's 170 s server-side budget: COA ends the stream before this does
TOTAL_BUDGET_S = 185.0
HEARTBEAT_S = 15.0
# what one ``done`` event may carry
MAX_ROWS = 200
MAX_COLUMNS = 50
CELL_MAX = 1000
STEP_DETAIL_MAX = 300
ANSWER_MAX = 20000
MAX_CITATIONS = 10
# a non-SSE body COA might answer with instead (one JSON object)
BODY_MAX = 2 * 1024 * 1024

MODES = ("standard", "deep-reasoning")
# COA's conversation id as the console may send it back (``PlaygroundRequest``): an
# id ``done`` could not round-trip is not echoed, so the next turn starts afresh
# instead of being refused
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# COA ``QueryStrategy`` (``options.strategy``); omitted = COA's default
STRATEGIES = ("best", "ontop", "nl_to_sql", "ontop_first", "nl_to_sql_first", "deep-reasoning")

_DEFAULT_MESSAGES = {
    "context_layer.reauthorize": "COA no longer accepts your authorization — authorize again",
    "context_layer.forbidden": "COA refused: you have no grant for this namespace in COA",
    "context_layer.coa_not_found": "COA has no such namespace, or does not show it to you",
    "context_layer.no_result": "COA found no answer to this question",
    "context_layer.coa_rejected": "COA rejected the question",
    "context_layer.upstream": "COA did not answer correctly",
}

_HEARTBEAT = object()


def _transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests (an ``httpx.MockTransport``); None = the real network."""
    return None


def runtime_session_id(workspace_id: str, username: str, namespace_id: str) -> str:
    """The AgentCore runtime session (``≥ 33`` chars): stable per member + namespace,
    so a member's turns stick to one COA microVM. COA's own conversation id
    (``sessionId`` in the body) is separate and comes back on ``done``."""
    digest = hashlib.sha256(f"{workspace_id}:{username}:{namespace_id}".encode()).hexdigest()
    return f"lp-{digest[:40]}"


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


KEEP_ALIVE = ": keep-alive\n\n"


# ── normalization (pure) ─────────────────────────────────────────────────────


def error_code(status: int | None, coa_error: str | None) -> str:
    """The ``context_layer.*`` code for a COA refusal (a status and COA's error type)."""
    if status == 401:
        return "context_layer.reauthorize"
    if status == 403:
        return "context_layer.forbidden"
    if coa_error in ("NoResult", "NoResultError"):
        return "context_layer.no_result"
    if status == 404:
        return "context_layer.coa_not_found"
    if status is not None and 400 <= status < 500 and status != 429:
        return "context_layer.coa_rejected"
    return "context_layer.upstream"


def error_event(
    code: str, message: str | None = None, *, status: int | None = None,
    coa_error: str | None = None,
) -> dict[str, Any]:
    return {
        "event": "error",
        "data": {
            "code": code,
            "message": message or _DEFAULT_MESSAGES.get(code, "COA did not answer correctly"),
            "status": status,
            "coa_error": coa_error,
        },
    }


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= CELL_MAX else value[:CELL_MAX] + "…"
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    return rendered if len(rendered) <= CELL_MAX else rendered[:CELL_MAX] + "…"


def _step(payload: dict[str, Any]) -> dict[str, Any]:
    detail = payload.get("detail")
    if isinstance(detail, dict | list):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    return {
        "name": text(payload.get("stepName") or payload.get("step"), 128) or "",
        "status": text(payload.get("status"), 64) or "",
        "duration_ms": integer(payload.get("durationMs", payload.get("duration_ms"))),
        "tool": text(payload.get("toolUsed"), 128),
        "detail": text(detail, STEP_DETAIL_MAX),
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _citations(value: Any) -> list[dict[str, Any]]:
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        name = text(
            item.get("sourceDocumentName") or item.get("sourceDoc") or item.get("label")
            or item.get("sourceDocumentId"),
        )
        snippet = text(item.get("text"), STEP_DETAIL_MAX)
        if name or snippet:
            score = _number(item.get("relevanceScore"))
            out.append({"name": name, "text": snippet, "score": score})
        if len(out) >= MAX_CITATIONS:
            break
    return out


def done_data(result: dict[str, Any], session_id: str | None) -> dict[str, Any]:
    """A COA ``QueryResult`` → the ``done`` event (rows capped, cells bounded)."""
    raw = result.get("resultRows")
    rows = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    shown = rows[:MAX_ROWS]
    columns: list[str] = []
    for row in shown:
        for key in row:
            name = str(key)
            if name not in columns and len(columns) < MAX_COLUMNS:
                columns.append(name)
    confidence = result.get("confidence")
    confidence = confidence if isinstance(confidence, dict) else {}
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "answer": text(result.get("synthesizedAnswer"), ANSWER_MAX),
        "tier": _number(result.get("tier")),
        "confidence": _number(confidence.get("score")),
        "rationale": text(confidence.get("rationale"), 1000),
        "columns": columns,
        "rows": [{c: _cell(row.get(c)) for c in columns if c in row} for row in shown],
        "row_count": len(rows),
        "rows_truncated": len(rows) > MAX_ROWS,
        "partial": bool(result.get("partial")),
        "guardrail_blocked": bool(result.get("guardrailBlocked")),
        "data_sources": strings(result.get("dataSources")),
        "citations": _citations(result.get("supportingContent")),
        "total_ms": integer(metadata.get("totalMs")),
        "session_id": session_id,
    }


def _error_from(payload: dict[str, Any]) -> dict[str, Any]:
    status = integer(payload.get("statusCode"))
    coa_error = text(payload.get("error"), 64)
    return error_event(
        error_code(status, coa_error),
        text(payload.get("message"), COA_MESSAGE_MAX),
        status=status,
        coa_error=coa_error,
    )


def normalize(frame: Any) -> list[dict[str, Any]]:
    """One COA frame → zero or more Launchpad events (``{event, data}``).

    Besides the typed envelopes, COA answers two bare shapes: a refusal before the
    stream starts (``{error, message, statusCode}``, e.g. a namespace grant denied)
    and a non-streamed ``{result, sessionId}``. Anything else is dropped."""
    if not isinstance(frame, dict):
        return []
    kind = frame.get("type")
    payload = frame.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    raw_session = frame.get("sessionId")
    session_id = (
        raw_session if isinstance(raw_session, str) and SESSION_ID_RE.fullmatch(raw_session)
        else None
    )
    if kind == "step":
        return [{"event": "step", "data": _step(payload)}]
    if kind == "token":
        chunk = payload.get("text")
        if not isinstance(chunk, str) or not chunk:
            return []
        return [{"event": "token", "data": {"text": chunk}}]
    if kind == "done":
        result = payload.get("result")
        return [{"event": "done", "data": done_data(result if isinstance(result, dict) else {},
                                                     session_id)}]
    if kind == "error":
        return [_error_from(payload)]
    if kind is None and frame.get("error"):
        return [_error_from(frame)]
    if kind is None and isinstance(frame.get("result"), dict):
        return [{"event": "done", "data": done_data(frame["result"], session_id)}]
    return []


# ── streaming ────────────────────────────────────────────────────────────────


async def _body(response: httpx.Response) -> bytes:
    """At most ``BODY_MAX`` bytes of a body: COA's answer is never read whole into memory."""
    limit = BODY_MAX
    out = bytearray()
    async for chunk in response.aiter_bytes():
        out.extend(chunk)
        if len(out) >= limit:
            break
    return bytes(out[:limit])


def _json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _frames(response: httpx.Response) -> AsyncIterator[Any]:
    """The decoded ``data:`` payloads of an SSE body, one event at a time (the
    async twin of ``agentcore.runtime._sse_payloads``). A body that is not SSE is
    read whole (bounded) as one JSON object."""
    content_type = str(response.headers.get("content-type") or "").lower()
    if "text/event-stream" not in content_type:
        raw = (await _body(response)).decode("utf-8", errors="replace")
        if not raw.lstrip().startswith("data:"):
            frame = _json(raw)
            if frame is not None:
                yield frame
            return
        lines: AsyncIterator[str] = _lines_of(raw)
    else:
        lines = response.aiter_lines()
    data: list[str] = []
    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if data:
                frame = _json("\n".join(data))
                data.clear()
                if frame is not None:
                    yield frame
            continue
        if line.startswith("data:"):
            data.append(line[len("data:"):].lstrip())
    if data:
        frame = _json("\n".join(data))
        if frame is not None:
            yield frame


async def _lines_of(raw: str) -> AsyncIterator[str]:
    for line in raw.splitlines():
        yield line


async def _with_heartbeat(source: AsyncIterator[Any], interval: float) -> AsyncIterator[Any]:
    """``source``'s items, plus ``_HEARTBEAT`` each time it stays silent for
    ``interval``. The pending read is never cancelled by a heartbeat (that would
    break the stream) — only when the consumer stops."""
    iterator = source.__aiter__()
    pending: asyncio.Future[Any] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield _HEARTBEAT
                continue
            finished, pending = pending, None
            try:
                item = finished.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        closer = getattr(iterator, "aclose", None)
        if closer is not None:
            with contextlib.suppress(BaseException):
                await closer()


async def _refusal(response: httpx.Response) -> dict[str, Any]:
    """The error event for a non-200 answer from the Serve runtime itself (its JWT
    authorizer, a throttle, a 5xx) — COA's message at most, never the body."""
    status = response.status_code
    message = None
    try:
        body = json.loads(await _body(response))
        if isinstance(body, dict):
            message = text(body.get("message") or body.get("error") or body.get("Message"),
                           COA_MESSAGE_MAX)
    except (ValueError, httpx.HTTPError):
        pass
    return error_event(error_code(status, None), message, status=status)


async def stream(
    *,
    layer: ContextLayer,
    token: str,
    workspace_id: str,
    username: str,
    namespace_id: str,
    query: str,
    session_id: str | None = None,
    mode: str | None = None,
    strategy: str | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    heartbeat_s: float = HEARTBEAT_S,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """Ask COA one question and yield the normalized SSE text (module docstring).

    Ends after the first ``done`` or ``error``. A COA 401 drops the member's cached
    token (``context_layer_tokens.invalidate``), so the next fetch forces consent."""
    body: dict[str, Any] = {"query": query, "namespace": namespace_id, "stream": True}
    if session_id:
        body["sessionId"] = session_id
    options = {key: value for key, value in (("mode", mode), ("strategy", strategy)) if value}
    if options:
        body["options"] = options
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        BEARER_SESSION_HEADER: runtime_session_id(workspace_id, username, namespace_id),
    }
    timeout = httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S)
    started = clock()

    def emit(event: dict[str, Any]) -> str:
        if event["event"] == "error" and event["data"]["code"] == "context_layer.reauthorize":
            tokens.invalidate(workspace_id, username)
        return sse(event["event"], event["data"])

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=_transport()
        ) as client:
            async with client.stream(
                "POST", layer.serve_url, headers=headers, json=body
            ) as response:
                if response.status_code != 200:
                    logger.warning("COA Serve answered HTTP %s", response.status_code)
                    yield emit(await _refusal(response))
                    return
                frames = _with_heartbeat(_frames(response), heartbeat_s)
                async with contextlib.aclosing(frames):
                    async for frame in frames:
                        if is_disconnected is not None and await is_disconnected():
                            logger.info("playground client left; closing the COA stream")
                            return
                        if clock() - started > TOTAL_BUDGET_S:
                            yield emit(error_event(
                                "context_layer.upstream", "COA did not finish in time",
                            ))
                            return
                        if frame is _HEARTBEAT:
                            yield KEEP_ALIVE
                            continue
                        for event in normalize(frame):
                            yield emit(event)
                            if event["event"] in ("done", "error"):
                                return
        yield emit(error_event(
            "context_layer.upstream", "COA ended the stream without an answer",
        ))
    except httpx.TimeoutException:
        yield emit(error_event("context_layer.upstream", "COA did not answer in time"))
    except httpx.HTTPError as exc:
        logger.warning("COA Serve stream failed (%s)", type(exc).__name__)
        yield emit(error_event(
            "context_layer.upstream", f"COA is unreachable ({type(exc).__name__})",
        ))


async def auth_required(grant: tokens.AuthNeeded) -> AsyncIterator[str]:
    """The member owes consent first: one ``auth_required`` event, the same ask
    Chat shows (``agent_id: ""`` = the console grant), then the stream ends."""
    yield sse("auth_required", tokens.auth_notice(grant, ""))
