# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_assistant
"""N16-obs86 (t1 journal, 09:21:55Z on aaacb0a6): `chat_turns.turn_events` raised
`OverflowError: Python int too large to convert to SQLite INTEGER` — an unbounded integer
parameter of the reattach route (`?from=<seq>`, the `turn` path segment) reached the driver
as a Python int above SQLite's range: the class r12-h bounded with `core/db.SQLITE_MAX_INT`
on `offset` / `expected_revision` / `before`. Both reattach routes (chat and assistant) now
bound `turn` (path, `ge=0, le=SQLITE_MAX_INT` → 422 `validation.invalid_request`) and `from`
(`live_turns.parse_from`: `0..SQLITE_MAX_INT` → 422 `chat.invalid_seq` /
`assistant.invalid_seq`); `from` is a declared query parameter, so the OpenAPI document — and
the Schemathesis contract run — now carry it. One test per parameter with 2**63 (refused) and
2**63 - 1 (accepted: never a 500).
"""

from __future__ import annotations

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SQLITE_MAX_INT, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.services import live_turns
from tests.test_assistant import (  # noqa: F401 — shared hermetic fixtures + helpers
    BASE,
    VALID_PROPOSAL,
    _block,
    _open,
    harness,
    no_aws_clients,
    no_network,
    ready,
)

SID = "bounds-" + "b" * 40
TOO_BIG = 2**63
MAX_OK = 2**63 - 1


def _seed_session() -> str:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="bounds-agent",
                      method="zip_runtime", status="active",
                      arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/b",
                      spec={"name": "b"})
        db.add(agent)
        db.flush()
        db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, session_id=SID,
                           actor_id="river", turns=1))
        db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, session_id=SID,
                           role="user", text="hi"))
        db.commit()
        return agent.id
    finally:
        db.close()


def test_sqlite_max_int_is_the_bound():
    assert SQLITE_MAX_INT == MAX_OK


def test_chat_reattach_from_is_bounded(client):
    agent_id = _seed_session()
    base = f"/api/chat/{agent_id}/sessions/{SID}/turns/1/stream"
    res = client.get(base, params={"from": TOO_BIG})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "chat.invalid_seq"
    assert res.json()["detail"] == {"from": str(TOO_BIG), "min": 0, "max": MAX_OK}
    res = client.get(base, params={"from": -1})
    assert res.status_code == 422 and res.json()["code"] == "chat.invalid_seq"
    # the largest representable seq is past every event: a bare `done`, never a 500
    res = client.get(base, params={"from": MAX_OK})
    assert res.status_code == 200, res.text
    assert "event: done" in res.text
    # the pre-existing shapes stay: not an integer → 422 chat.invalid_seq; default 0
    assert client.get(base, params={"from": "x"}).json()["code"] == "chat.invalid_seq"
    assert client.get(base).status_code == 200


def test_chat_reattach_turn_is_bounded(client):
    agent_id = _seed_session()
    base = f"/api/chat/{agent_id}/sessions/{SID}/turns"
    res = client.get(f"{base}/{TOO_BIG}/stream")
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "validation.invalid_request"
    assert any(e["loc"][-1] == "turn" for e in res.json()["detail"])
    # a turn the session never reached, however large within range, is the route's 404
    res = client.get(f"{base}/{MAX_OK}/stream")
    assert res.status_code == 404 and res.json()["code"] == "chat.turn_not_found"
    res = client.get(f"{base}/-1/stream")
    assert res.status_code == 422 and res.json()["code"] == "validation.invalid_request"
    assert client.get(f"{base}/0/stream").json()["code"] == "chat.turn_not_found"


def test_assistant_reattach_from_and_turn_are_bounded(client, ready, harness):
    harness.reply("done deal " + _block(VALID_PROPOSAL))
    cid = _open(client)
    assert client.post(f"{BASE}/conversations/{cid}/turns", json={"prompt": "go"}
                       ).status_code == 200
    base = f"{BASE}/conversations/{cid}/turns"
    res = client.get(f"{base}/1/stream", params={"from": TOO_BIG})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "assistant.invalid_seq"
    res = client.get(f"{base}/1/stream", params={"from": MAX_OK})
    assert res.status_code == 200 and "event: done" in res.text
    res = client.get(f"{base}/{TOO_BIG}/stream")
    assert res.status_code == 422 and res.json()["code"] == "validation.invalid_request"
    res = client.get(f"{base}/{MAX_OK}/stream")
    assert res.status_code == 404 and res.json()["code"] == "assistant.turn_not_found"


@pytest.mark.parametrize(("raw", "value"), [(None, 0), ("", 0), ("0", 0), ("7", 7),
                                            (str(MAX_OK), MAX_OK)])
def test_parse_from_accepts_the_range(raw, value):
    assert live_turns.parse_from(raw, "x.invalid_seq") == value


@pytest.mark.parametrize("raw", [str(TOO_BIG), "-1", "x", "1.5", str(10**30)])
def test_parse_from_refuses_outside_the_range(raw):
    with pytest.raises(AppError) as exc:
        live_turns.parse_from(raw, "x.invalid_seq")
    assert exc.value.code == "x.invalid_seq" and exc.value.status_code == 422


def test_the_reattach_from_parameter_is_in_the_openapi_document(client):
    """Declared (not read raw from the query string) so the Schemathesis contract run
    generates it; the `turn` path parameter carries the bound."""
    spec = client.get("/api/openapi.json").json()
    for path in ("/api/chat/{agent_id}/sessions/{session_id}/turns/{turn}/stream",
                 "/api/assistant/architect/conversations/{conversation_id}/turns/{turn}/stream"):
        params = {p["name"]: p for p in spec["paths"][path]["get"]["parameters"]}
        assert params["from"]["in"] == "query", path
        turn = params["turn"]["schema"]
        assert (turn["minimum"], turn["maximum"]) == (0, MAX_OK), path
