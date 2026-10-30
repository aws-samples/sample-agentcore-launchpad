"""N16-obs85: bounded tool-call summaries — runtime templates → hub normalizer → chat tool
row (SSE `tool_input` / `tool_result`, history `tool_input` / `tool_result_summary`).

The console used to persist only `{name, id}` per tool event, so nothing recorded which url
the browser builtin opened or what came back. Both generated templates now turn the Strands
`message` events (assistant `toolUse` blocks = the complete arguments; `toolResult` blocks)
into additive `tool_input` / `tool_result` frames cut at 512 / 256 characters; the hub cuts
again, the transcript recorder completes the tool row and history replays the summary.
Everything is hermetic: fake agents, no AWS.
"""

from __future__ import annotations

import json

import app.services.chat as chat_service
from app.core.db import DEFAULT_WORKSPACE_ID
from app.services import chat_turns
from app.services.agentcore import runtime as rt
from app.services.chat_ledger import save_message, track_session, update_tool_message

from .test_chat_api import delta_events, make_active_agent
from .test_max_tokens_recovery import (
    TOOL_START,
    _delta,
    _drain,
    _HarnessFakeAgent,
    _import_real_harness,
    _StrandsFakeAgent,
    _wire,
)
from .test_max_tokens_recovery import strands_module as strands_module  # noqa: F811, PLC0414

PAGE_TEXT = "Welcome to the HR portal. " * 800  # 20 800 chars — never summarised verbatim
BROWSER_RESULT = f"title: HR portal\nurl: https://hr.example.test/home\n\n{PAGE_TEXT}"


def _assistant_tool_use(name="browser", tool_id="t1", **arguments) -> dict:
    return {"message": {"role": "assistant", "content": [
        {"text": "Let me look."},
        {"toolUse": {"toolUseId": tool_id, "name": name, "input": arguments}},
    ]}}


def _tool_result(text: str, tool_id="t1", status="success") -> dict:
    return {"message": {"role": "user", "content": [
        {"toolResult": {"toolUseId": tool_id, "status": status, "content": [{"text": text}]}},
    ]}}


# --- the templates -----------------------------------------------------------------------


def test_strands_template_frames_browser_call_bounded(strands_module):
    frames = strands_module._launchpad_tool_frames(
        _assistant_tool_use(url="https://hr.example.test/")
    )
    assert frames == [{
        "event": "tool_input", "id": "t1", "name": "browser",
        "input": '{"url": "https://hr.example.test/"}',
    }]
    (result,) = strands_module._launchpad_tool_frames(_tool_result(BROWSER_RESULT))
    assert result == {
        "event": "tool_result", "id": "t1", "name": "browser", "status": "success",
        "summary": (
            f"title: HR portal | url: https://hr.example.test/home | text: {len(PAGE_TEXT)} chars"
        ),
    }
    assert "Welcome to the HR portal" not in json.dumps(frames + [result])


def test_strands_template_bounds_generic_input_and_result(strands_module):
    big = {"query": "q" * 5000, "token": "s" * 100}
    (frame,) = strands_module._launchpad_tool_frames(_assistant_tool_use("search", **big))
    assert len(frame["input"]) <= 512 + 30
    assert frame["input"].startswith('{"query": "qqqq')
    assert frame["input"].endswith(" chars]")  # explicit truncation marker
    (result,) = strands_module._launchpad_tool_frames(
        _tool_result("line one\n" + "r" * 2000, status="error")
    )
    assert result["status"] == "error"
    assert len(result["summary"]) <= 256 + 30 and result["summary"].startswith("line one rrr")
    # non-text result content is named, never dumped
    (image,) = strands_module._launchpad_tool_frames({"message": {"role": "user", "content": [
        {"toolResult": {"toolUseId": "t2", "status": "success",
                        "content": [{"image": {"format": "png", "source": {"bytes": b"x"}}}]}},
    ]}})
    assert image["summary"] == "[image]"
    # anything that is not a message carries no frame
    assert strands_module._launchpad_tool_frames({"data": "x"}) == []
    assert strands_module._launchpad_tool_frames("not a dict") == []


def test_strands_agent_events_stream_tool_then_input_then_result(strands_module):
    agent = _StrandsFakeAgent([
        {"current_tool_use": {"toolUseId": "t1", "name": "browser", "input": {}}},
        _assistant_tool_use(url="https://hr.example.test/"),
        _tool_result(BROWSER_RESULT),
        {"data": "The portal says hi."},
        {"result": "The portal says hi."},
    ])
    events = _drain(strands_module.agent_events(agent, "open the portal"))
    assert [e["event"] for e in events] == [
        "tool", "tool_input", "tool_result", "delta", "complete",
    ]
    assert events[1]["input"] == '{"url": "https://hr.example.test/"}'
    assert events[2]["summary"].startswith("title: HR portal | url: https://hr.example.test/home")


def test_harness_invoke_streams_the_frames_beside_the_raw_converse_events(
    tmp_path, monkeypatch, request
):
    module = _import_real_harness(tmp_path, monkeypatch, request)
    agent = _HarnessFakeAgent([
        TOOL_START,
        _assistant_tool_use("code_interpreter", code="print(13 * 8750)"),
        _tool_result("113750\n", status="success"),
        _delta("The total is 113750."),
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
    ])
    ctx = _wire(module, monkeypatch, agent)
    out = _drain(module.invoke({"prompt": "compute"}, ctx))
    assert out == [
        TOOL_START,
        {"event": "tool_input", "id": "t1", "name": "code_interpreter",
         "input": '{"code": "print(13 * 8750)"}'},
        {"event": "tool_result", "id": "t1", "name": "code_interpreter", "status": "success",
         "summary": "113750"},
        _delta("The total is 113750."),
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
    ]
    # the message events themselves are still not forwarded (no "event" key)
    assert not any("message" in e for e in out)


# --- the hub normalizer --------------------------------------------------------------------


def test_hub_normalizer_bounds_both_frames_again():
    (frame,) = rt._runtime_payload_events({
        "event": "tool_input", "id": "t1", "name": "browser", "input": "x" * 3000,
    })
    assert frame["event"] == "tool_input"
    assert frame["data"]["id"] == "t1" and frame["data"]["name"] == "browser"
    assert frame["data"]["input"].startswith("x" * 512 + "… [+2488 chars]")
    (result,) = rt._runtime_payload_events({
        "event": "tool_result", "id": 42, "status": "error", "summary": "r" * 1000,
    })
    assert result == {"event": "tool_result", "data": {
        "id": "42", "name": "", "status": "error", "summary": "r" * 256 + "… [+744 chars]",
    }}
    # a frame without an id (a BYOC runtime) stays usable: the recorder falls back to name
    (anon,) = rt._runtime_payload_events({"event": "tool_input", "input": {"a": 1}})
    assert anon["data"] == {"id": None, "name": "", "input": "{'a': 1}"}


# --- the transcript recorder ------------------------------------------------------------------


def test_recorder_stamps_the_tool_row_and_completes_it():
    saved: list[tuple] = []
    updated: list[tuple] = []
    recorder = chat_turns.TranscriptRecorder(
        "ws", "agent", prompt="open it", actor_id="river", attachments=None,
        runtime_version=None,
        save=lambda *a, **k: saved.append((a, k)), track=lambda *a, **k: None,
        update_tool=lambda *a, **k: updated.append((a, k)),
    )
    for event in (
        {"event": "meta", "data": {"session_id": "s" * 40}},
        {"event": "delta", "data": {"text": "Looking… "}},
        {"event": "tool", "data": {"name": "browser", "id": "t1"}},
        {"event": "tool_input", "data": {"id": "t1", "name": "browser", "input": '{"url": "u"}'}},
        {"event": "tool_result", "data": {"id": "t1", "name": "browser", "status": "success",
                                          "summary": "title: X | url: u | text: 9 chars"}},
        {"event": "delta", "data": {"text": "done"}},
        {"event": "done", "data": {}},
    ):
        recorder.feed(event)
    roles = [(a[3], a[4], k.get("name"), k.get("tool_call")) for a, k in saved]
    assert roles == [
        ("user", "open it", None, None),
        ("agent", "Looking… ", None, None),
        ("tool", "", "browser", {"id": "t1"}),
        ("agent", "done", None, None),
    ]
    assert updated == [
        (("ws", "agent", "s" * 40, "t1", "browser"), {"input": '{"url": "u"}'}),
        (("ws", "agent", "s" * 40, "t1", "browser"),
         {"result_summary": "title: X | url: u | text: 9 chars", "status": "success"}),
    ]


def test_recorder_replay_without_persistence_updates_nothing():
    updated: list = []
    recorder = chat_turns.TranscriptRecorder(
        "ws", "agent", prompt="p", actor_id="river", attachments=None, runtime_version=None,
        save=lambda *a, **k: None, track=lambda *a, **k: None,
        update_tool=lambda *a, **k: updated.append(a), persist=False,
    )
    recorder.feed({"event": "meta", "data": {"session_id": "s" * 40}})
    recorder.feed({"event": "tool_input", "data": {"id": "t1", "name": "x", "input": "{}"}})
    assert updated == []


# --- the ledger + history -------------------------------------------------------------------


def test_update_tool_message_matches_by_id_then_name_then_newest(client):
    agent_id = make_active_agent(name="tool-ledger-agent")
    sid = "l" * 40
    track_session(DEFAULT_WORKSPACE_ID, agent_id, sid, "river")
    save_message(DEFAULT_WORKSPACE_ID, agent_id, sid, "tool", "", name="browser",
                 tool_call={"id": "t1"})
    save_message(DEFAULT_WORKSPACE_ID, agent_id, sid, "tool", "", name="search")
    save_message(DEFAULT_WORKSPACE_ID, agent_id, sid, "tool", "", name="browser",
                 tool_call={"id": "t3"})
    assert update_tool_message(DEFAULT_WORKSPACE_ID, agent_id, sid, "t1", "browser",
                               input='{"url": "first"}')
    assert update_tool_message(DEFAULT_WORKSPACE_ID, agent_id, sid, None, "search",
                               input='{"q": "x"}')
    assert update_tool_message(DEFAULT_WORKSPACE_ID, agent_id, sid, None, None,
                               result_summary="newest", status="success")
    assert not update_tool_message(DEFAULT_WORKSPACE_ID, agent_id, sid, "t-missing", "nope",
                                   input="dropped")
    history = client.get(
        f"/api/chat/{agent_id}/history", params={"session_id": sid}
    ).json()["messages"]
    assert [(m["name"], m.get("tool_input"), m.get("tool_result_summary"),
             m.get("tool_result_status")) for m in history] == [
        ("browser", '{"url": "first"}', None, None),
        ("search", '{"q": "x"}', None, None),
        ("browser", None, "newest", "success"),
    ]
    assert "tool_call" not in history[0]  # the column is internal; the fields are the contract


def test_chat_history_replays_the_browser_call_summary_end_to_end(client, monkeypatch):
    """The whole chain: runtime frames → `tool` row stamped at the call, completed by the
    input and result frames, replayed by /history — the url opened and what came back,
    never the page text; the SSE carries the same two additive events."""
    agent_id = make_active_agent(name="chat-hist-tool-summary")
    summary = f"title: HR portal | url: https://hr.example.test/home | text: {len(PAGE_TEXT)} chars"

    def events(*_a, **_k):
        yield {"event": "delta", "data": {"text": "Opening the portal. "}}
        yield {"event": "tool", "data": {"name": "browser", "id": "t1"}}
        yield {"event": "tool_input", "data": {"id": "t1", "name": "browser",
                                               "input": '{"url": "https://hr.example.test/"}'}}
        yield {"event": "tool_result", "data": {"id": "t1", "status": "success",
                                                "summary": summary}}
        yield from delta_events("The portal welcomes you.")

    monkeypatch.setattr(chat_service, "invoke_agent_events", events)
    response = client.post(
        f"/api/chat/{agent_id}", json={"prompt": "open the portal", "session_id": "h" * 40}
    )
    assert "event: tool_input" in response.text and "event: tool_result" in response.text
    assert "Welcome to the HR portal" not in response.text
    history = client.get(
        f"/api/chat/{agent_id}/history", params={"session_id": "h" * 40}
    ).json()["messages"]
    assert [(m["role"], m["text"], m["name"]) for m in history] == [
        ("user", "open the portal", None),
        ("agent", "Opening the portal. ", None),
        ("tool", "", "browser"),
        ("agent", "The portal welcomes you.", None),
    ]
    tool_row = history[2]
    assert tool_row["tool_input"] == '{"url": "https://hr.example.test/"}'
    assert tool_row["tool_result_summary"] == summary
    assert tool_row["tool_result_status"] == "success"
    # rows without a summary carry none of the additive keys
    assert not {"tool_input", "tool_result_summary", "tool_result_status"} & set(history[0])


# --- C4 (rev-r16): credential-shaped text never reaches the row -------------------------------

# obviously fake placeholder values — the shape is what the redaction keys on
FAKE_VALUE = "FAKE_PLACEHOLDER_VALUE_0000"
ENV_LINE = f"AWS_BEARER_TOKEN_BEDROCK={FAKE_VALUE}"
SHELL_RESULT = f"$ env | grep BEDROCK\n{ENV_LINE}\nAWS_REGION=cn-northwest-1\n"


def test_template_emits_no_summary_for_shell_and_file_tools(strands_module):
    """The reviewer's probe: a native `shell` / `file_operations` result is arbitrary host text
    (an `env` listing, a config file) — it gets `status` only, whatever it printed."""
    for name in ("shell", "file_operations"):
        strands_module._launchpad_tool_frames(
            _assistant_tool_use(name, tool_id="s1", command="env")
        )
        (result,) = strands_module._launchpad_tool_frames(
            _tool_result(SHELL_RESULT, tool_id="s1")
        )
        assert result == {"event": "tool_result", "id": "s1", "name": name,
                          "status": "success", "summary": ""}
    assert FAKE_VALUE not in json.dumps(result)


def test_template_redacts_credential_pairs_in_input_and_generic_results(strands_module):
    (frame,) = strands_module._launchpad_tool_frames(_assistant_tool_use(
        "http_get", tool_id="h1", url="https://api.example.test/",
        api_key=FAKE_VALUE, headers={"Authorization": f"Bearer {FAKE_VALUE}"},
    ))
    assert FAKE_VALUE not in frame["input"]
    assert "api_key=[redacted]" in frame["input"]
    assert "url" in frame["input"] and "api.example.test" in frame["input"]  # the rest stays
    (result,) = strands_module._launchpad_tool_frames(_tool_result(
        f"fetched 3 rows; password: {FAKE_VALUE} {ENV_LINE} client_secret = {FAKE_VALUE}",
        tool_id="h1",
    ))
    assert FAKE_VALUE not in result["summary"]
    assert result["summary"].startswith("fetched 3 rows; password=[redacted]")
    assert "AWS_BEARER_TOKEN_BEDROCK=[redacted]" in result["summary"]
    assert "client_secret=[redacted]" in result["summary"]


def test_hub_normalizer_redacts_again_and_drops_native_tool_summaries():
    """Defence in depth: a BYOC runtime streams whatever it likes — the hub applies the same
    rules before anything is persisted."""
    (shell,) = rt._runtime_payload_events({
        "event": "tool_result", "id": "s1", "name": "shell", "status": "success",
        "summary": SHELL_RESULT,
    })
    assert shell["data"] == {"id": "s1", "name": "shell", "status": "success", "summary": ""}
    (generic,) = rt._runtime_payload_events({
        "event": "tool_result", "id": "g1", "name": "http_get", "status": "success",
        "summary": f"ok {ENV_LINE} token: {FAKE_VALUE}",
    })
    assert generic["data"]["summary"] == "ok AWS_BEARER_TOKEN_BEDROCK=[redacted] token=[redacted]"
    (arguments,) = rt._runtime_payload_events({
        "event": "tool_input", "id": "g1", "name": "http_get",
        "input": f'{{"api_key": "{FAKE_VALUE}", "q": "x"}}',
    })
    assert FAKE_VALUE not in arguments["data"]["input"]
    assert rt.redact_secret_pairs("no pairs here key=value-ok") == "no pairs here key=[redacted]"
    assert rt.redact_secret_pairs("plain text, nothing to hide") == "plain text, nothing to hide"


def test_history_payload_never_carries_the_probe_value(client, monkeypatch):
    agent_id = make_active_agent(name="chat-hist-c4-probe")

    def events(*_a, **_k):
        yield {"event": "tool", "data": {"name": "shell", "id": "s1"}}
        yield from rt._runtime_payload_events({
            "event": "tool_input", "id": "s1", "name": "shell",
            "input": '{"command": "env | grep BEDROCK"}',
        })
        yield from rt._runtime_payload_events({
            "event": "tool_result", "id": "s1", "name": "shell", "status": "success",
            "summary": SHELL_RESULT,
        })
        yield {"event": "tool", "data": {"name": "http_get", "id": "g1"}}
        yield from rt._runtime_payload_events({
            "event": "tool_result", "id": "g1", "name": "http_get", "status": "success",
            "summary": f"ok {ENV_LINE}",
        })
        yield from delta_events("done")

    monkeypatch.setattr(chat_service, "invoke_agent_events", events)
    response = client.post(
        f"/api/chat/{agent_id}", json={"prompt": "inspect", "session_id": "c" * 40}
    )
    assert FAKE_VALUE not in response.text
    history = client.get(
        f"/api/chat/{agent_id}/history", params={"session_id": "c" * 40}
    )
    assert FAKE_VALUE not in history.text
    rows = history.json()["messages"]
    shell_row, http_row = rows[1], rows[2]
    assert shell_row["name"] == "shell" and shell_row["tool_result_summary"] == ""
    assert shell_row["tool_input"] == '{"command": "env | grep BEDROCK"}'
    assert http_row["tool_result_summary"] == "ok AWS_BEARER_TOKEN_BEDROCK=[redacted]"
    from app.core.db import SessionLocal
    from app.models.ledger import ChatMessage
    db = SessionLocal()
    try:
        stored = [r.tool_call for r in db.query(ChatMessage).filter(
            ChatMessage.session_id == "c" * 40, ChatMessage.role == "tool").all()]
    finally:
        db.close()
    assert FAKE_VALUE not in json.dumps(stored)


def test_the_tool_summaries_block_is_byte_identical_in_both_templates():
    """The vendored block is the same text in the Harness and the shared Strands template, so
    the two runtimes cannot drift (the browser_stream parity test's role for this block)."""
    from pathlib import Path

    from app.deployer.harness_local import TEMPLATES

    start, end = "# <launchpad-tool-summaries:v1>", "# </launchpad-tool-summaries:v1>\n"
    blocks = []
    for rel in ("harness_runtime/main.py.tmpl", "strands_agent/main.py.tmpl"):
        text = Path(TEMPLATES / rel).read_text(encoding="utf-8")
        assert text.count(start) == 1 and text.count(end) == 1, rel
        blocks.append(text[text.index(start): text.index(end) + len(end)])
    assert blocks[0] == blocks[1]
    assert "LAUNCHPAD_UNSUMMARISED_TOOLS" in blocks[0] and "_launchpad_redact" in blocks[0]


def test_the_architect_runtime_path_is_untouched():
    """The architect's own `tool_input` (`{id, input}` rebuilt from the Converse deltas in
    assistant/runtime.py, 20 000-char bound) is a separate reducer: the chat kinds added here
    live in the hub normalizer and the templates only."""
    import inspect

    from app.assistant import runtime as assistant_runtime

    assert assistant_runtime.TOOL_INPUT_MAX_CHARS == 20_000
    source = inspect.getsource(assistant_runtime)
    assert "tool_result" not in source and "bounded_summary" not in source
