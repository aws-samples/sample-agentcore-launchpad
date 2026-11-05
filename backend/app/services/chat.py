"""The single chat/invoke chain shared by the Chat playground and the public /v1 API.

Harness, Claude SDK container, and generated Strands zip-runtime agents stream
real deltas, including tool-use events. Other runtime methods keep the buffered
compatibility path.
"""

import json
import time
from collections.abc import Iterator
from contextlib import closing
from typing import Any

from app.assistant.sessions import refuse_assistant_session
from app.core.errors import AppError, envelope
from app.models.ledger import Agent
from app.services import answer_rules, guardrail, policy_denials
from app.services import inbound_auth as inbound_auth_service
from app.services.agentcore import harness as hc
from app.services.agentcore.client import data_client
from app.services.agentcore.harness import new_session_id
from app.services.attachments import PreparedAttachments
from app.services.invoke import (
    NATIVE_STREAM_METHODS,
    harness_turn_overrides,
    invoke_agent_events,
)
from app.services.model_access import model_access_denied
from app.services.runtime_discovery import is_discovered_harness
from app.services.workspace import WorkspaceContext, context_for_workspace


def _with_transient(data: dict[str, Any], exc: Exception, console: bool) -> dict[str, Any]:
    """Console errors say whether a retry may help (chat-streaming S3); /v1 is unchanged."""
    if console:
        from app.evaluation.service import transient_invoke_error

        data = {**data, "transient": bool(transient_invoke_error(exc))}
    return data


def chat_stream(
    agent: Agent,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "river",
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    workspace: WorkspaceContext | None = None,
    attachments: PreparedAttachments | None = None,
    bearer_token: str | None = None,
    console_user: bool = True,
    status_events: bool = False,
    stop: "StopSignal | None" = None,
    context: dict[str, str] | None = None,
    user_role: str | None = None,
    user_email: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield SSE-ready events: meta → (heartbeat|tool|delta|policy_denied)* → done.

    ``heartbeat`` (``{"elapsed_ms": n}``) fills every quiet stretch of the turn
    (``with_keepalive``); ``sse_encode`` decides how it is written on the wire.

    ``policy_denied`` (Harness only) marks a Gateway tool call a Cedar policy denied.

    Never raises mid-stream; errors surface as an `error` event. ``workspace``
    defaults to the agent's own — see ``invoke._agent_workspace``.
    ``bearer_token`` is the caller's JWT for a JWT-inbound agent (console Chat
    passes the signed-in user's token; absent, the invoke layer falls back to
    the workspace M2M token or fails with a named error).
    ``console_user=False`` (public /v1) refuses as_user consent asks by name —
    see ``invoke._record_auth_sessions``.

    ``status_events`` (the console chat, chat-streaming §3.1) adds the working-status
    stream: ``meta.started_at`` and ``tool.started_at`` (epoch ms), ``phase``
    (``starting`` → ``waiting_model`` → ``streaming``), ``thinking`` (Harness
    reasoning, status only — never the reasoning text) and ``tool_end``
    (``{id, status: success|error|denied, duration_ms}``). Off, the stream is
    exactly what /v1 and the share page document.

    ``stop`` (a console turn running in the background, chat-streaming §5.2): once
    set, the stream raises ``TurnStopped`` within ``STOP_POLL_SECONDS`` even while
    the upstream is silent, and the upstream is closed.

    ``context`` (strands-builder SB-1) is this turn's ``{{variable}}`` values, passed
    in the runtime payload. An ``output_schema`` agent's parsed answer arrives as a
    ``structured`` event (``{value}``) after the text; a guardrail turn drops it, as
    only the text is screened.

    ``user_role`` / ``user_email`` are the signed-in member's, for an agent whose
    context layer runs in gateway mode (a Harness: ``invoke.harness_turn_overrides``; a
    Strands runtime: ``invoke._runtime_gateway_gate``).
    """
    session_id = session_id or new_session_id()
    workspace = workspace if workspace is not None else context_for_workspace(
        agent.workspace_id
    )
    # Imported harnesses stream through the same InvokeHarness path as 方式B.
    harness = agent.method == "harness" or is_discovered_harness(agent)
    # T12: PII protection screens whole texts, and an entity can straddle two deltas,
    # so a guardrail-enabled agent deliberately gives up token-by-token streaming:
    # deltas are collected, the answer is screened once, and the masked text is
    # emitted as one delta. Slower first paint is the price of not leaking PII.
    screen = guardrail.guardrail_config(agent.spec)
    mode = (
        "buffered"
        if screen
        else ("stream" if harness or agent.method in NATIVE_STREAM_METHODS else "buffered")
    )
    meta = {"session_id": session_id, "agent": agent.name, "mode": mode}
    if attachments:
        meta["attachments"] = attachments.metadata
    if inbound_auth_service.is_jwt_mode(agent):
        # Which bearer this turn presents to the JWT authorizer, so the
        # console can say who the runtime saw (the "invoke as me" toggle).
        meta["inbound"] = {
            "mode": "jwt",
            "caller": "user_jwt" if bearer_token else "m2m",
        }
    if status_events:
        meta["started_at"] = _epoch_ms()
    yield {
        "event": "meta",
        "data": meta,
    }
    started = time.monotonic()
    buffered: list[str] = []
    try:
        refuse_assistant_session(agent, session_id)
        # the project's monthly budget (projects §11.3): a used-up budget ends the
        # stream with an `error` event (project.budget_exhausted), before the model
        from app.services.project_quotas import assert_agent_budget

        assert_agent_budget(agent)
        had_attachments = bool(attachments)
        if screen:
            prompt = guardrail.screen(
                attachments.prompt(prompt) if attachments else prompt,
                source="INPUT",
                mode=screen["mode"],
                workspace=workspace,
            ).text
            attachments = None
        # T35: a curated answer short-circuits the model call (see
        # `services/answer_rules` for the matching and PII-screen ordering). The owner's
        # text is emitted as-is, preceded by a `rule` event so every consumer can show
        # that a rule, not the model, answered.
        hit = None if had_attachments else answer_rules.match_for_agent(agent, prompt)
        if hit is not None:
            yield hit.event()
            yield {"event": "delta", "data": {"text": hit.answer}}
            yield {
                "event": "done",
                "data": {"latency_ms": int((time.monotonic() - started) * 1000),
                         "answered_by": "rule"},
            }
            return
        if harness:
            source = _harness_events(
                agent,
                attachments.prompt(prompt) if attachments else prompt,
                session_id,
                actor_id,
                workspace,
                runtime_user_id=runtime_user_id,
                gateway_access_token=gateway_access_token,
                thinking=status_events,
                console_user=console_user,
                user_role=user_role,
                user_email=user_email,
            )
        else:
            invoke_kwargs: dict[str, Any] = {}
            if runtime_user_id:
                invoke_kwargs["runtime_user_id"] = runtime_user_id
            if gateway_access_token:
                invoke_kwargs["gateway_access_token"] = gateway_access_token
            if attachments:
                invoke_kwargs["attachments"] = attachments
            if bearer_token:
                invoke_kwargs["bearer_token"] = bearer_token
            if not console_user:
                invoke_kwargs["console_user"] = False
            if context:
                invoke_kwargs["context"] = context
            if user_role:
                # a gateway-mode Strands agent's consent gate mints for this member
                invoke_kwargs["user_role"] = user_role
                invoke_kwargs["user_email"] = user_email
            source = invoke_agent_events(
                agent,
                prompt,
                session_id=session_id,
                actor_id=actor_id,
                workspace=workspace,
                **invoke_kwargs,
            )
        if screen:
            source = _withhold_deltas(source, buffered)
        if status_events:
            source = _status_events(source, reports_tool_end=harness)
        # withheld deltas never reach the keepalive, so a guardrail turn that streams
        # text upstream but sends nothing down still gets its heartbeats
        yield from with_keepalive(source, started=started, stop=stop)
    except AppError as exc:
        yield {"event": "error", "data": _with_transient(
            envelope(exc.code, exc.message, exc.detail), exc, status_events)}
        return
    except Exception as exc:
        # a refused model call is final and named (agent.model_access_denied): the
        # console must not retry it by itself or tell the person to send it again
        denied = model_access_denied(exc)
        if denied is not None:
            yield {"event": "error", "data": _with_transient(
                envelope(denied.code, denied.message, denied.detail), denied, status_events)}
            return
        yield {"event": "error", "data": _with_transient(
            {"message": f"{type(exc).__name__}: {exc}"}, exc, status_events)}
        return
    if screen:
        try:
            masked = guardrail.screen(
                "".join(buffered), source="OUTPUT", mode=screen["mode"], workspace=workspace
            ).text
        except AppError as exc:
            yield {"event": "error", "data": envelope(exc.code, exc.message, exc.detail)}
            return
        if masked:
            yield {"event": "delta", "data": {"text": masked}}
    yield {
        "event": "done",
        "data": {"latency_ms": int((time.monotonic() - started) * 1000)},
    }


def _withhold_deltas(
    events: Iterator[dict[str, Any]], buffered: list[str],
) -> Iterator[dict[str, Any]]:
    """While screening, text is withheld until the whole answer can be screened;
    tool and heartbeat events still flow so the UI stays alive. A `structured` object
    is not screened, so it is dropped."""
    try:
        for event in events:
            if event.get("event") == "delta":
                buffered.append(str(event["data"].get("text", "")))
                continue
            if event.get("event") == "structured":
                continue
            yield event
    finally:
        # closing this wrapper (client gone) closes the upstream stream too
        close = getattr(events, "close", None)
        if close is not None:
            close()


def _harness_events(
    agent: Agent,
    prompt: str,
    session_id: str,
    actor_id: str,
    workspace: WorkspaceContext,
    *,
    runtime_user_id: str | None = None,
    gateway_access_token: str | None = None,
    thinking: bool = False,
    console_user: bool = True,
    user_role: str | None = None,
    user_email: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Map one InvokeHarness stream onto chat events.

    ``thinking`` (status events on): a ``reasoningContent`` delta yields a bare
    ``thinking`` event — at most one per ``THINKING_THROTTLE_SECONDS`` and never
    the reasoning text (chat-streaming D-2) — and each finished toolResult yields
    ``tool_end`` with its status (``denied`` when the Gateway's policy refused it;
    ``_status_events`` adds the duration)."""
    params: dict[str, Any] = {
        "harnessArn": agent.arn,
        "runtimeSessionId": session_id,
        "actorId": actor_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
    }
    if runtime_user_id:
        params["runtimeUserId"] = runtime_user_id
    overrides = harness_turn_overrides(
        agent, workspace, gateway_access_token=gateway_access_token,
        runtime_user_id=runtime_user_id, console_user=console_user, session_id=session_id,
        user_role=user_role, user_email=user_email,
    )
    if "auth_required" in overrides:
        # COA consent first (direct, or the managed gateway's preflight): the card is
        # the whole turn, InvokeHarness never runs
        yield {"event": "auth_required", "data": overrides["auth_required"]}
        return
    params.update(overrides)
    # a gated agent serves production through its `live` endpoint (the release gate)
    from app.services.invoke import production_endpoint

    params.update(production_endpoint(agent))
    response = data_client(workspace).invoke_harness(
        **params,
    )
    # toolUseId → tool name, and the toolResult blocks being streamed (by block
    # index): a Cedar denial only shows up in a result's text (policy_denials)
    tool_names: dict[Any, str] = {}
    results: dict[Any, dict[str, Any]] = {}
    last_thinking: float | None = None
    with closing(hc.iter_harness_stream(response["stream"])) as events:
        for event in events:
            if "contentBlockStart" in event:
                block = event["contentBlockStart"]
                start = block.get("start", {})
                tool_use = start.get("toolUse")
                tool_result = start.get("toolResult")
                if tool_use:
                    tool_names[tool_use.get("toolUseId")] = tool_use.get("name", "")
                    yield {
                        "event": "tool",
                        "data": {"name": tool_use.get("name", ""), "id": tool_use.get("toolUseId")},
                    }
                elif isinstance(tool_result, dict):
                    results[block.get("contentBlockIndex")] = {
                        "id": tool_result.get("toolUseId"),
                        "status": tool_result.get("status"),
                        "chunks": [],
                    }
            elif "contentBlockDelta" in event:
                block = event["contentBlockDelta"]
                delta = block.get("delta", {})
                if delta.get("text"):
                    yield {"event": "delta", "data": {"text": delta["text"]}}
                elif thinking and delta.get("reasoningContent"):
                    now = time.monotonic()
                    if last_thinking is None or now - last_thinking >= THINKING_THROTTLE_SECONDS:
                        last_thinking = now
                        yield {"event": "thinking", "data": {}}
                pending = results.get(block.get("contentBlockIndex"))
                if pending is not None and isinstance(delta.get("toolResult"), list):
                    pending["chunks"].extend(
                        str(part["text"]) for part in delta["toolResult"]
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
            elif "contentBlockStop" in event:
                done = results.pop(event["contentBlockStop"].get("contentBlockIndex"), None)
                denied = _policy_denied(done, tool_names, workspace)
                if denied is not None:
                    yield {"event": "policy_denied", "data": denied}
                if thinking and done is not None:
                    status = (
                        "denied" if denied is not None
                        else "error" if done.get("status") == "error" else "success"
                    )
                    yield {"event": "tool_end", "data": {"id": done["id"], "status": status}}


# chat-streaming §3.1: reasoning deltas arrive token by token; the status line only
# needs to know reasoning is under way, so ``thinking`` is sent at most this often.
THINKING_THROTTLE_SECONDS = 0.2

# Events a code runtime's tool call cannot have ended on: its own keep-alive, and
# usage the stream reports at its very end (after which open tools close anyway).
_NOT_A_TOOL_END = frozenset({"heartbeat", "thinking", "usage", "tool", "tool_end"})


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _status_events(
    events: Iterator[dict[str, Any]], *, reports_tool_end: bool = False,
) -> Iterator[dict[str, Any]]:
    """Add the working-status events (chat-streaming §3.1) to a turn's upstream.

    ``phase``: ``starting`` before the upstream is invoked, ``waiting_model`` on its
    first event (any, a runtime heartbeat included), ``streaming`` on the first text.
    ``tool`` gains ``started_at`` (and an id when the runtime sent none, so its end
    can be matched). A Harness reports each ``tool_end`` itself
    (``reports_tool_end``); a code runtime does not, so a tool still open is ended —
    ``success``, timed up to here — by the next event that is not a tool event or
    keep-alive. Every tool still open when the stream ends is ended the same way."""
    yield {"event": "phase", "data": {"phase": "starting"}}
    phase = "starting"
    started: dict[Any, float] = {}  # open tool id → monotonic start
    count = 0

    def tool_end(tool_id: Any, status: str = "success") -> dict[str, Any]:
        begun = started.pop(tool_id, None)
        duration = None if begun is None else int((time.monotonic() - begun) * 1000)
        return {"event": "tool_end",
                "data": {"id": tool_id, "status": status, "duration_ms": duration}}

    try:
        for event in events:
            kind = event.get("event")
            if phase == "starting":
                phase = "waiting_model"
                yield {"event": "phase", "data": {"phase": phase}}
            if kind == "tool":
                count += 1
                data = dict(event["data"])
                data["id"] = data.get("id") or f"tool-{count}"
                data["started_at"] = _epoch_ms()
                started[data["id"]] = time.monotonic()
                yield {"event": "tool", "data": data}
                continue
            if kind == "tool_end":
                yield tool_end(event["data"].get("id"), event["data"].get("status", "success"))
                continue
            if not reports_tool_end and kind not in _NOT_A_TOOL_END:
                for tool_id in list(started):
                    yield tool_end(tool_id)
            if kind == "delta" and phase != "streaming":
                phase = "streaming"
                yield {"event": "phase", "data": {"phase": phase}}
            yield event
        for tool_id in list(started):
            yield tool_end(tool_id)
    finally:
        # closing this wrapper (client gone) closes the upstream stream too
        close = getattr(events, "close", None)
        if close is not None:
            close()


def _policy_denied(
    result: dict[str, Any] | None, tool_names: dict[Any, str], workspace: WorkspaceContext,
) -> dict[str, Any] | None:
    """The ``policy_denied`` payload for a finished toolResult the Gateway denied.

    Only an error result carrying the Gateway's denial signature qualifies. A tool
    reached through the signed-in user's Gateway alias is named as the catalog
    names it (``<target>___<tool>``) and linked to the workspace gateway; any other
    alias is resolved only by the deployed Harness, so it keeps its name unlinked.
    """
    if result is None or result.get("status") != "error":
        return None
    denial = policy_denials.parse_tool_denial("".join(result["chunks"]))
    if denial is None:
        return None
    tool = tool_names.get(result["id"])
    tool = tool if isinstance(tool, str) else ""  # a toolResult with no toolUse seen
    gateway_id = None
    prefix = f"{hc.USER_GATEWAY_ALIAS}_"
    if tool.startswith(prefix):
        tool = tool[len(prefix):]
        gateway_id = workspace.resources.get("gateway_id") or None
    return {"tool": tool, "tool_use_id": result["id"], **denial, "gateway_id": gateway_id}


def sse_encode(event: dict[str, Any], *, named_heartbeat: bool = False) -> str:
    """One SSE frame. A ``heartbeat`` is an SSE comment unless ``named_heartbeat``:
    the console chat sends it as a named ``event: heartbeat`` (``{"elapsed_ms": n}``)
    so the page can time the turn and tell a live connection from a dead one; the
    public /v1 stream keeps the ``: keep-alive`` comment its contract documents."""
    if event["event"] == "heartbeat" and not named_heartbeat:
        return ": keep-alive\n\n"
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

# CloudFront drops a response that sends no byte for 60 s. Only platform-template
# runtimes heartbeat on their own; a Harness reasoning for a long time, a slow tool
# call, or a guardrail holding the answer back for one final screen are all silent,
# so the stream heartbeats for the whole turn whenever the upstream goes quiet.
KEEPALIVE_SECONDS = 15.0


# How often a stoppable stream (``with_keepalive(stop=...)``) looks at its stop signal
# while the upstream is silent: the console's stop button and a turn's time limit act
# within this, not at the next heartbeat.
STOP_POLL_SECONDS = 0.25


class TurnStopped(BaseException):  # noqa: N818 — a control-flow signal, like GeneratorExit
    """A background chat turn was stopped (the stop button, its time limit).

    A ``BaseException`` on purpose: it must pass through ``chat_stream``'s
    ``except Exception`` (which would turn it into an ``error`` event) up to the
    turn's worker. ``interruption`` is what the ledger's ``interrupted: …`` row says."""

    def __init__(self, interruption: str, kind: str = "stopped") -> None:
        super().__init__(interruption)
        self.interruption = interruption
        self.kind = kind


class StopSignal:
    """A thread-safe, set-once stop request with its reason."""

    def __init__(self) -> None:
        import threading

        self._event = threading.Event()
        self.reason = ""
        self.kind = ""

    def set(self, reason: str, kind: str = "stopped") -> bool:
        """Request the stop; False when it was already requested (the first reason wins)."""
        if self._event.is_set():
            return False
        self.reason, self.kind = reason, kind
        self._event.set()
        return True

    def is_set(self) -> bool:
        return self._event.is_set()

    def raise_if_set(self) -> None:
        if self._event.is_set():
            raise TurnStopped(self.reason, self.kind)


def _heartbeat(started: float) -> dict[str, Any]:
    return {"event": "heartbeat",
            "data": {"elapsed_ms": int((time.monotonic() - started) * 1000)}}


def with_keepalive(
    events: Iterator[dict[str, Any]],
    interval: float | None = None,
    started: float | None = None,
    stop: StopSignal | None = None,
) -> Iterator[dict[str, Any]]:
    """Re-yield ``events``, adding a ``heartbeat`` whenever ``interval`` seconds pass
    with nothing to send — before the first event and after it alike. A heartbeat the
    runtime sends itself is passed on (it resets the silence, so the two never stack).
    Every heartbeat carries ``elapsed_ms`` since ``started`` (default: now).

    Each step of the source runs on one worker thread while this generator waits for
    it with a timeout. Closing this generator still closes the upstream stream: at
    once when no step is in flight, else as soon as the in-flight step returns (a
    generator cannot be closed while another thread is running it).

    ``stop``: checked between steps and every ``STOP_POLL_SECONDS`` while one is in
    flight; once set, ``TurnStopped`` is raised and the upstream closed as above."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    interval = KEEPALIVE_SECONDS if interval is None else interval
    started = time.monotonic() if started is None else started
    source = iter(events)
    _missing = object()
    # the source reads request-scoped context (workspace, identity): every step runs
    # in one copy of it, so what one step sets the next one sees
    ctx = contextvars.copy_context()

    def close_source(_future: Any = None) -> None:
        close = getattr(source, "close", None)
        if close is not None:
            ctx.run(close)

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-keepalive")
    future = None
    wait = interval if stop is None else min(interval, STOP_POLL_SECONDS)
    try:
        while True:
            if stop is not None:
                stop.raise_if_set()
            future = pool.submit(ctx.run, next, source, _missing)
            beat_at = time.monotonic() + interval
            while True:
                try:
                    event = future.result(timeout=wait)
                    break
                except FutureTimeout:
                    if stop is not None:
                        stop.raise_if_set()
                    if stop is None or time.monotonic() >= beat_at:
                        beat_at = time.monotonic() + interval
                        yield _heartbeat(started)
            future = None
            if event is _missing:
                return
            yield _heartbeat(started) if event.get("event") == "heartbeat" else event
    finally:
        if future is not None and not future.done():
            future.add_done_callback(close_source)
        else:
            close_source()
        pool.shutdown(wait=False)
