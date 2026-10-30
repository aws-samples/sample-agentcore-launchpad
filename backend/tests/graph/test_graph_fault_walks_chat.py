# ruff: noqa: F811  — pytest fixtures are imported by name from the world modules
"""Fault-injected walks over the CHAT SESSION graph (round 6, r17-l; testing.md §6.9): the gated
producer, the `/v1` sync wrapper and StopRuntimeSession serve the recorded shapes mid-walk; the
SSE-level shapes (a partial frame, the worker's REJECTED JSON body) run through the real chain."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.graph import faults
from tests.graph._fault_axis import axis_params, run_axis_cell
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import ChatFaults, make_client_dict
from tests.graph.test_graph_chat_session import seams  # noqa: F401 — fixture by name
from tests.test_visibility import PEER, _active_agent, ada, admin, app, bob  # noqa: F401


@pytest.fixture
def make_world(app, admin, ada, bob, seams):
    clients, ada_id = make_client_dict(admin, ada, bob)
    producer, plane = seams

    def make() -> ChatFaults:
        tab = TestClient(app, client=PEER)
        tab.cookies = clients["ada"].cookies
        tag = uuid.uuid4().hex[:6]
        key = clients["ada"].post("/api/apikeys", json={"name": f"fault-v1-{tag}"}).json()["key"]
        agent_id = _active_agent(f"g-fchat-{tag}", created_by=ada_id)
        return ChatFaults(clients, agent_id, producer, plane, tab, key)

    return make


@pytest.mark.parametrize(("family", "seed"), faults.params())
def test_chat_session_fault_walk(make_world, family, seed):
    faults.run_fault_walk(make_world, ChatFaults, GRAPHS["chat_session"], family, seed)


@pytest.mark.parametrize(("edge_id", "shape"), axis_params(ChatFaults, "chat_session"))
def test_chat_session_refusal_seam_axis(make_world, edge_id, shape):
    run_axis_cell(make_world, "chat_session", edge_id, shape)


# ---------------------------------------------------------------------------
# the round-6 pins (strict, narrow: the registered symptom type only — a fix flips them)
# ---------------------------------------------------------------------------


def _open_session(world: ChatFaults) -> None:
    graph = GRAPHS["chat_session"]
    world.step(graph.edge("first_turn"))
    world.step(graph.edge("turn_completes"))


def test_N17_obs95_v1_invoke_on_the_sdk_error_frame_is_a_bare_500(make_world):
    """N17-obs95 (fixed, cn/r17-o): a /v1 sync invoke whose Runtime stream ends on the SDK
    terminal error frame used to escape as SdkStreamError (a bare 500); it is the 502 envelope
    `agent.runtime_error` with the frame's `detail {error_type, error, message}` now, nothing
    ledgered — the same frame Chat renders as its terminal `error` event."""
    world = make_world()
    try:
        _open_session(world)
        world.fault_step(GRAPHS["chat_session"].edge("v1_turn"),
                         faults.SHAPES["rt_frame_no_workload_token"], expected_dst="open")
    finally:
        world.close()


def test_N17_obs94_transport_timeout_on_v1_invoke_is_a_bare_500(make_world):
    """N17-obs94 (fixed, cn/r17-o): a botocore ReadTimeoutError on InvokeAgentRuntime used to
    escape every route as a bare 500; `/v1` answers 504 `aws.timeout {error_type}` with the
    static message, nothing ledgered (api.md "Transport failures")."""
    world = make_world()
    try:
        _open_session(world)
        world.fault_step(GRAPHS["chat_session"].edge("v1_turn"), faults.SHAPES["rt_read_timeout"],
                         expected_dst="open")
    finally:
        world.close()


def test_N17_obs96_partial_frame_alone_is_rendered_as_the_reply(make_world):
    """N17-obs96 (fixed, cn/r17-o): a truncated SSE data line that is the WHOLE response (the
    recorded 300-char cut of the agent template's error frame) used to be rendered verbatim as
    the assistant's reply with a terminal `done`; it ends the turn on `error` (MalformedFrame)
    and the cut text never reads as the answer."""
    world = make_world()
    try:
        out = world.fault_step(GRAPHS["chat_session"].edge("first_turn"),
                               faults.SHAPES["rt_partial_frame_only"], expected_dst="open")
        frames = world.post_frames[world.turn]
        texts = "".join(d.get("text", "") for k, _s, d in frames if k == "delta")
        assert frames[-1][0] == "error" and not texts.startswith('{"error"'), (
            "a malformed frame must never read as the reply", out.observation)
    finally:
        world.close()


def test_N17_obs97_rejected_json_body_is_rendered_as_the_reply(make_world):
    """N17-obs97 (fixed, cn/r17-o): a structured refusal body (`state: REJECTED`, `code`,
    `message` — the eval worker's ack shape, application/json) used to be rendered as the
    assistant's reply with `done` (the free-form `message` key won); it ends the turn on `error`
    (RuntimeRejected, `detail.code`)."""
    world = make_world()
    try:
        out = world.fault_step(GRAPHS["chat_session"].edge("first_turn"),
                               faults.SHAPES["rt_rejected_json_body"], expected_dst="open")
        frames = world.post_frames[world.turn]
        texts = "".join(d.get("text", "") for k, _s, d in frames if k == "delta")
        assert frames[-1][0] == "error" and "unknown job_type" not in texts, (
            "a REJECTED body must surface as an error, never as the answer", out.observation)
    finally:
        world.close()


def test_N17_obs98_chat_error_event_lacks_the_frames_error_type(make_world):
    """N17-obs98 (fixed, cn/r17-o — DOC decided WITH the code): the chat `error` event for the
    SDK terminal error frame carries `detail {error_type, error, message}` exactly as §4 gives
    the architect turn; §8.2 "Terminal `error` data — §4 parity" says so."""
    world = make_world()
    try:
        world.fault_step(GRAPHS["chat_session"].edge("first_turn"),
                         faults.SHAPES["runtime_bedrock_5xx_frame"], expected_dst="open")
        frames = world.post_frames[world.turn]
        assert frames[-1][0] == "error" and frames[-1][2].get("detail", {}).get("error_type"), (
            frames[-1][2])
    finally:
        world.close()
