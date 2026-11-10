"""The three SSE test parsers (N17-obs52 + rev-r17 on cn/r17-i): frames are split on LF only
after the WHOLE text is normalised — CRLF-framed streams and bare-CR streams parse like LF
streams, and a `data:` JSON string carrying U+2028 / U+2029 / U+0085 stays one line."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.graph.test_graph_chat_session import _frames as graph_frames
from tests.test_assistant import _sse
from tests.test_assistant_async_turns import _sse_with_ids
from tests.test_chat_async_turns import _frames as chat_frames


def _sse_events(text: str) -> list[tuple[str, dict]]:
    """The former invoke-gate test's parser (that module left with the hub gate under Global
    parity): normalise the WHOLE text (CRLF / bare CR → LF) first, then split frames on LF only
    (N17-obs52, rev-r17: splitlines() cut a data: JSON string at U+2028-class characters)."""
    events = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for block in text.strip().split("\n\n"):
        kind, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if kind:
            events.append((kind, data))
    return events

PAYLOAD = {"text": "a\u2028b\u2029c\u0085d"}
LF = (f"event: meta\nid: 1\ndata: {json.dumps(PAYLOAD, ensure_ascii=False)}\n\n"
      "event: done\nid: 2\ndata: {}\n\n")


def _variants() -> list[tuple[str, str]]:
    return [
        ("lf", LF),
        ("crlf", LF.replace("\n", "\r\n")),
        ("bare-cr", LF.replace("\n", "\r")),
    ]


@pytest.mark.parametrize("label, text", _variants())
def test_chat_parser_sees_two_frames_whatever_the_line_ending(label, text):
    frames = chat_frames(text)
    assert [(k, s) for k, _d, s in frames] == [("meta", 1), ("done", 2)], (label, frames)
    assert frames[0][1] == PAYLOAD  # the U+2028-class characters never split the JSON


@pytest.mark.parametrize("label, text", _variants())
def test_graph_parser_sees_two_frames_whatever_the_line_ending(label, text):
    frames = graph_frames(text)
    assert [(k, s) for k, s, _d in frames] == [("meta", 1), ("done", 2)], (label, frames)
    assert frames[0][2] == PAYLOAD


@pytest.mark.parametrize("label, text", _variants())
def test_assistant_parser_sees_two_frames_whatever_the_line_ending(label, text):
    events = _sse(SimpleNamespace(text=text))
    assert [k for k, _d in events] == ["meta", "done"], (label, events)
    assert events[0][1] == PAYLOAD


@pytest.mark.parametrize("label, text", _variants())
def test_assistant_async_parser_sees_two_frames_whatever_the_line_ending(label, text):
    frames = _sse_with_ids(text)
    assert [(k, s) for k, _d, s in frames] == [("meta", 1), ("done", 2)], (label, frames)
    assert frames[0][1] == PAYLOAD


@pytest.mark.parametrize("label, text", _variants())
def test_policy_gate_parser_sees_two_frames_whatever_the_line_ending(label, text):
    events = _sse_events(text)
    assert [k for k, _d in events] == ["meta", "done"], (label, events)
    assert events[0][1] == PAYLOAD
