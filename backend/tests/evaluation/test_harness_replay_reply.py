"""N17-obs419 — a dataset replay of a harness agent recorded an empty reply.

The self-built Harness runtime (``templates/harness_runtime/main.py.tmpl``) streams raw
Strands / Converse events — ``{"event": {"contentBlockDelta": {"delta": {"text": …}}}}``,
``{"event": {"contentBlockStart": {"start": {"toolUse": …}}}}`` — next to the platform's
``{"event": "tool", …}`` frames. The worker's ``runtime_invoke._collect`` only read the
platform envelope (``event`` a string), so every harness turn collected ``""`` and the
extraction reconciliation's ``replay`` reference reported a false 7/7 mismatch (live run
d50284f90734). Now the Converse frames are read too, and a session whose replay still
collected an empty answer gets NO replay reference — the engine falls back to the session's
own ``runtime_logs`` (N17-obs316), never to "".
"""

from __future__ import annotations

import json

from app.services.strands_eval import extraction_check, jobs_batch, runtime_invoke
from tests.evaluation.test_jobs_replay_failures import (
    S1,
    S2,
    S3,
    Sink,
    _spec,
    harness,  # noqa: F401 — fixture
)

PREAMBLE = "我先查一下差旅标准"
FINAL = "成都住宿上限 450 元/晚。"


def _delta(text: str) -> dict:
    return {"event": {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}}}


def _harness_stream() -> str:
    """The harness turn as the runtime sends it over SSE: a preamble, the tool call (the
    Converse ``toolUse`` start and the platform ``tool`` frame), the final answer, the stop."""
    frames = [
        {"event": {"messageStart": {"role": "assistant"}}},
        _delta(PREAMBLE),
        {"event": {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": "tu-1", "name": "kb_search"}}, "contentBlockIndex": 1}}},
        {"event": "tool", "name": "kb_search", "id": "tu-1"},
        {"event": {"contentBlockStop": {"contentBlockIndex": 1}}},
        _delta(FINAL[:6]),
        _delta(FINAL[6:]),
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
        {"event": {"metadata": {"usage": {"inputTokens": 9, "outputTokens": 4,
                                          "totalTokens": 13}}}},
    ]
    return "".join(f"data: {json.dumps(f, ensure_ascii=False)}\n\n" for f in frames)


class _Stream:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body


class _Client:
    def __init__(self, body: str):
        self.body = body

    def invoke_agent_runtime(self, **_params):
        return {"response": _Stream(self.body), "contentType": "text/event-stream"}


def test_the_harness_reply_is_the_text_after_the_last_tool_call():
    reply = runtime_invoke.invoke_text(_Client(_harness_stream()), "rt", "hi", session_id=S1)
    assert reply["text"] == FINAL


def test_a_harness_reply_without_tools_is_all_of_its_deltas():
    body = "".join(f"data: {json.dumps(_delta(t))}\n\n" for t in ("Hello", ", ", "world"))
    assert runtime_invoke.invoke_text(_Client(body), "rt", "hi", session_id=S1)["text"] \
        == "Hello, world"


def test_the_platform_envelope_is_unchanged():
    frames = [{"event": "delta", "text": PREAMBLE}, {"event": "tool", "name": "kb"},
              {"event": "delta", "text": FINAL}, {"event": "complete", "result": FINAL}]
    body = "".join(f"data: {json.dumps(f, ensure_ascii=False)}\n\n" for f in frames)
    assert runtime_invoke.invoke_text(_Client(body), "rt", "hi", session_id=S1)["text"] == FINAL


def test_an_empty_replay_answer_leaves_the_session_to_its_runtime_logs(harness):  # noqa: F811
    h = harness({S1: [FINAL], S2: [""], S3: ["   "]})
    spec = _spec()
    spec["sessions"][0]["prompts"] = ["q1", "q2"]
    references: dict[str, dict] = {}
    failed = jobs_batch._replay_dataset(h.env, Sink(), spec, 0, references)
    assert failed == {}
    # S1 answered every turn → its replay reference; S2 / S3 collected nothing → none, so
    # the engine's runtime_logs fallback (``provider.origin_reference``) checks them
    assert set(references) == {S1}
    assert references[S1]["source"] == extraction_check.SOURCE_REPLAY
    assert references[S1] == extraction_check.reference(extraction_check.SOURCE_REPLAY,
                                                        [FINAL, FINAL])


def test_a_partly_empty_replay_is_not_a_reference_either(harness):  # noqa: F811
    h = harness({S1: [FINAL]})
    h.runtime.plan[S1] = [FINAL, "", FINAL]
    spec = _spec()
    spec["sessions"] = spec["sessions"][:1]
    spec["sessions"][0]["prompts"] = ["q1", "q2"]
    references: dict[str, dict] = {}
    jobs_batch._replay_dataset(h.env, Sink(), spec, 0, references)
    assert references == {}



def test_the_worker_reads_the_harness_stream_as_the_chat_does():
    """Parity with the hub's normaliser (``_runtime_payload_events``, Chat's harness path):
    the chat's last bubble — the deltas after the last ``tool`` — is the worker's reply."""
    from app.services.agentcore.runtime import _runtime_payload_events

    bubble: list[str] = []
    for frame in runtime_invoke._sse_payloads(_harness_stream()):
        for event in _runtime_payload_events(frame):
            if event["event"] == "tool":
                bubble = []
            elif event["event"] == "delta":
                bubble.append(event["data"]["text"])
    assert "".join(bubble) == FINAL
    assert runtime_invoke.invoke_text(_Client(_harness_stream()), "rt", "hi",
                                      session_id=S1)["text"] == FINAL
