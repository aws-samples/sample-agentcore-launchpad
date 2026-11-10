# ruff: noqa: F811  — pytest fixtures are imported by name from test_assistant
"""Reusable upstream fault injection for the hermetic suite (quality pack Q4, N12-obs58).

The platform's stubbed clients already *answer*; these fixtures make them *fail* the way
the real upstreams do, so a recovery path is exercised against the exact exception shape
the production code classifies — never against a hand-rolled ``RuntimeError("boom")``.

One vocabulary, three surfaces (``Fault.surface``):

* ``frame`` — the AgentCore SDK's terminal error frame inside an otherwise healthy
  stream (``{"error", "error_type", "message"}``, see ``rt.SdkStreamError``): what a
  Bedrock 5xx / throttle / read timeout on the *Harness side* looks like to the assistant.
* ``client`` — a ``botocore.exceptions.ClientError`` raised by the data-plane call itself
  (the Runtime answered 5xx / 429 before any stream opened).
* ``transport`` — a ``botocore.exceptions.ReadTimeoutError`` on the call (the socket
  between the backend / worker and the data plane timed out).

Fixtures (all factories, so a test says *how many* and *where*):

* ``bedrock_5xx(n)``, ``throttle(n)``, ``read_timeout(n)`` → ``list[Fault]``; the
  keyword ``surface=`` picks the rendering (default ``frame``, ``read_timeout``'s
  non-frame rendering is ``transport``).
* ``sdk_error_frame(error_type, error=None)`` → one ``frame`` fault with that class name.
* ``harness`` — the architect preset's data plane (``FaultyArchitectRuntime``); it
  REPLACES ``tests.test_assistant.harness`` for this package, so every imported assistant
  helper (``ready``, ``_open``, ``_turn``) streams through it. ``harness.inject(*faults,
  before=...)`` arms the next calls; what follows is the scripted reply.
* ``worker_replay_5xx(n, *session_ids)`` → ``FlakyInvoke``, an ``env.invoke`` whose first
  ``n`` calls per session raise a 5xx ``ClientError`` (the eval worker's replay).
* ``worker_judge_5xx(n)`` / ``worker_judge_throttle(n)`` → ``FlakyJudge``, a scripted
  judge model whose first ``n`` model calls raise.
* ``spans_session`` — a real strands ``Session`` (the ``spans_session.json`` fixture
  through ``LaunchpadSpansProvider``) for judge-path tests.
* ``backoff`` — sets ``assistant_replay_backoff_seconds`` (the suite runs at 0).

``HAS_UPSTREAM_TRANSIENT`` is the import guard for the r12-f retry helper: a scenario
that only holds once that lands is ``xfail(strict=True)`` while the behaviour probe says
the code does not retry yet, and a real pass the moment it does.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from app.assistant import service
from app.core.config import get_settings
from app.services.agentcore import runtime as rt
from app.services.strands_eval.spans_provider import LaunchpadSpansProvider
from tests import test_assistant as ta
from tests.evaluation.fakes import FakeJudge, FakeLogs
from tests.test_assistant import (  # noqa: F401 — autouse fixtures + helpers by name
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

# r12-f import guard: the shared upstream-transient classifier (runtime.py). Absent until
# cnfork/cn/r12-f lands; the behaviour probes below are what the xfail conditions use.
HAS_UPSTREAM_TRANSIENT = hasattr(rt, "is_upstream_transient")

INVOKE_OP = "InvokeAgentRuntime"
MODEL_OP = "ConverseStream"
ENDPOINT = "https://bedrock-agentcore.cn-northwest-1.test"
STREAMING_MESSAGE = "An error occurred during streaming"  # the SDK frame's fixed text
# what the failing attempt had streamed before the fault (so a test can prove the replay
# discarded it): half a sentence, no tool step
HALF_REPLY = [{"contentBlockDelta": {"delta": {"text": "half an answer "}}}]


@dataclass(frozen=True)
class Fault:
    """One injected upstream failure. ``frame()`` renders it as the SDK's terminal error
    frame; ``exception(operation)`` as the botocore exception a client call raises."""

    name: str
    error_type: str  # the frame's exception class name (Harness side)
    error: str  # the frame's error text
    code: str | None = None  # botocore error code for the ``client`` surface
    http_status: int | None = None
    surface: str = "frame"  # frame | client | transport

    def frame(self) -> dict[str, Any]:
        return {"error": self.error, "error_type": self.error_type, "message": STREAMING_MESSAGE}

    def exception(self, operation: str = INVOKE_OP) -> BaseException:
        if self.surface == "transport":
            return ReadTimeoutError(endpoint_url=ENDPOINT)
        return ClientError(
            {"Error": {"Code": self.code or self.error_type, "Message": self.error},
             "ResponseMetadata": {"HTTPStatusCode": self.http_status or 500}},
            operation,
        )

    @property
    def marker(self) -> str:
        """The substring every surfaced form of this fault carries (retry reason, replay
        row, error message)."""
        return self.code or self.error_type


def _bedrock_5xx(surface: str) -> Fault:
    return Fault(
        name="bedrock_5xx", error_type="EventStreamError",
        error=("EventStreamError: An error occurred (internalServerException) when calling "
               "the ConverseStream operation: Try your request again."),
        code="InternalServerException", http_status=500, surface=surface,
    )


def _throttle(surface: str) -> Fault:
    return Fault(
        name="throttle", error_type="ModelThrottledException",
        error=("ModelThrottledException: An error occurred (ThrottlingException) when calling "
               "the ConverseStream operation: Too many requests, please wait before trying "
               "again."),
        code="ThrottlingException", http_status=429, surface=surface,
    )


def _read_timeout(surface: str) -> Fault:
    return Fault(
        name="read_timeout", error_type="ReadTimeoutError",
        error=f"Read timeout on endpoint URL: \"{ENDPOINT}\"",
        code="ReadTimeoutError", http_status=None,
        surface="transport" if surface != "frame" else "frame",
    )


# the client-surface shapes module-level behaviour probes key on
BEDROCK_5XX_CLIENT = _bedrock_5xx("client")
THROTTLE_CLIENT = _throttle("client")


def _factory(make: Callable[[str], Fault]) -> Callable[..., list[Fault]]:
    def fixture(n: int = 1, *, surface: str = "frame") -> list[Fault]:
        return [make(surface) for _ in range(int(n))]

    return fixture


@pytest.fixture
def bedrock_5xx() -> Callable[..., list[Fault]]:
    """``bedrock_5xx(n, surface="frame"|"client")`` — a Bedrock ``internalServerException``
    (the N-ARCH-obs18 live shape) as the SDK frame or as a 500 ``ClientError``."""
    return _factory(_bedrock_5xx)


@pytest.fixture
def throttle() -> Callable[..., list[Fault]]:
    """``throttle(n, surface=...)`` — ``ThrottlingException`` as the model's
    ``ModelThrottledException`` frame or as a 429 ``ClientError``."""
    return _factory(_throttle)


@pytest.fixture
def read_timeout() -> Callable[..., list[Fault]]:
    """``read_timeout(n, surface="frame"|"transport")`` — a model read timeout inside the
    Harness (frame) or botocore's ``ReadTimeoutError`` on the call itself (transport)."""
    return _factory(_read_timeout)


@pytest.fixture
def sdk_error_frame() -> Callable[..., list[Fault]]:
    """``sdk_error_frame("ValidationException", error=...)`` — any terminal SDK frame."""

    def make(error_type: str, error: str | None = None) -> list[Fault]:
        return [Fault(name="sdk_error_frame", error_type=error_type,
                      error=error or f"{error_type}: injected by tests/faults")]

    return make


def _flatten(faults: tuple[Any, ...]) -> list[Fault]:
    out: list[Fault] = []
    for item in faults:
        out.extend(item if isinstance(item, list | tuple) else [item])
    return out


class FaultyArchitectRuntime(ta.FakeArchitectRuntime):
    """``FakeArchitectRuntime`` that serves the armed faults first — one per
    ``InvokeAgentRuntime`` call — then the scripted reply. ``frame`` faults stream
    ``before`` (default: half a reply) and end on the frame; ``client`` / ``transport``
    faults raise from the call. ``stamps`` records the monotonic time of every call."""

    def __init__(self) -> None:
        super().__init__()
        self.faults: list[Fault] = []
        self.before: list[dict[str, Any]] = list(HALF_REPLY)
        self.stamps: list[float] = []
        self.served: list[Fault] = []

    def inject(self, *faults: Any, before: list[dict[str, Any]] | None = None) -> None:
        self.faults.extend(_flatten(faults))
        if before is not None:
            self.before = list(before)

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        import time

        self.stamps.append(time.monotonic())
        if not self.faults:
            return super().invoke_agent_runtime(**kwargs)
        fault = self.faults.pop(0)
        self.served.append(fault)
        self.calls.append(kwargs)
        if self.on_invoke is not None:
            self.on_invoke(kwargs)
        if fault.surface != "frame":
            raise fault.exception(INVOKE_OP)
        body = ta.FakeBody(list(self.before) + [fault.frame()])
        self.streams.append(body)
        return {"response": body, "contentType": "text/event-stream"}


@pytest.fixture
def harness(monkeypatch) -> FaultyArchitectRuntime:
    """The architect preset's data plane, fault-injectable (replaces test_assistant's)."""
    fake = FaultyArchitectRuntime()
    monkeypatch.setattr(service, "data_client", lambda workspace: fake)
    monkeypatch.setattr(service, "fetch_catalog", lambda workspace: ta._catalog())
    return fake


@pytest.fixture
def backoff(monkeypatch) -> Callable[[float], None]:
    """Set the replay back-off (the suite pins it to 0 in the root conftest)."""

    def set_backoff(seconds: float) -> None:
        monkeypatch.setattr(get_settings(), "assistant_replay_backoff_seconds", seconds)

    return set_backoff


# ─── the eval worker ─────────────────────────────────────────────────────────


class FlakyInvoke:
    """``WorkerEnv.invoke`` stand-in: per session, the armed faults are raised first (one
    per call), then ``reply`` is answered. Records every call."""

    def __init__(self, faults_by_session: dict[str, list[Fault]], reply: str = "ok") -> None:
        self.faults = {sid: list(fs) for sid, fs in faults_by_session.items()}
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def __call__(self, prompt: str, session_id: str, session_spec: dict[str, Any]
                 ) -> dict[str, Any]:
        self.calls.append((session_id, prompt))
        queue = self.faults.get(session_id)
        if queue:
            raise queue.pop(0).exception(INVOKE_OP)
        return {"text": self.reply, "session_id": session_id}


@pytest.fixture
def worker_replay_5xx() -> Callable[..., FlakyInvoke]:
    """``worker_replay_5xx(n, *session_ids)`` — the worker's ``InvokeAgentRuntime`` answers
    ``n`` × 500 ``InternalServerException`` per named session, then ``ok``."""

    def make(n: int, *session_ids: str, reply: str = "ok") -> FlakyInvoke:
        return FlakyInvoke({sid: [_bedrock_5xx("client")] * int(n) for sid in session_ids},
                           reply=reply)

    return make


class FlakyJudge(FakeJudge):
    """A scripted judge whose first model calls raise the armed faults (as the model
    client's ``ClientError``), then answer a valid structured output. ``attempts`` counts
    every model call."""

    def __init__(self, faults: list[Fault], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.faults = list(faults)
        self.attempts = 0

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.attempts += 1
        if self.faults:
            raise self.faults.pop(0).exception(MODEL_OP)
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


@pytest.fixture
def worker_judge_5xx() -> Callable[..., FlakyJudge]:
    """``worker_judge_5xx(n)`` — the judge model's first ``n`` calls are a 500."""

    def make(n: int, **kwargs: Any) -> FlakyJudge:
        return FlakyJudge([_bedrock_5xx("client")] * int(n), **kwargs)

    return make


@pytest.fixture
def worker_judge_throttle() -> Callable[..., FlakyJudge]:
    """``worker_judge_throttle(n)`` — the judge model's first ``n`` calls are throttled."""

    def make(n: int, **kwargs: Any) -> FlakyJudge:
        return FlakyJudge([_throttle("client")] * int(n), **kwargs)

    return make


_SPANS_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "spans_session.json"


@pytest.fixture
def spans_session() -> tuple[LaunchpadSpansProvider, str]:
    """``(provider, session_id)``: a real strands ``Session`` behind
    ``provider.get_evaluation_data(session_id)``, from the recorded spans fixture."""
    fixture = json.loads(_SPANS_FIXTURE.read_text(encoding="utf-8"))
    logs = FakeLogs({"sort": [{"@message": json.dumps(r)} for r in fixture["records"]]})
    provider = LaunchpadSpansProvider(logs, [fixture["log_group"]], spans_source="runtime_logs")
    return provider, fixture["session_id"]


# ─── behaviour probes (what the xfail conditions key on, never a symbol name) ──


def assistant_replays(exc: BaseException) -> bool:
    """Whether the architect turn classifies ``exc`` as a replayable transient."""
    return bool(service._replayable_turn_error(exc))


def judge_retries(fault: Fault) -> bool:
    """Whether ``engine_core.evaluate_with_retry`` retries a judge call that raised
    ``fault`` once (pure: a stub evaluator, no model, no sleep)."""
    from app.services.strands_eval import engine_core

    class Once:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, case):
            self.calls += 1
            if self.calls == 1:
                raise fault.exception(MODEL_OP)
            return []

    try:
        engine_core.evaluate_with_retry(Once(), None, sleep=lambda s: None)
    except Exception:  # noqa: BLE001 — the probe's answer is "not retried"
        return False
    return True
