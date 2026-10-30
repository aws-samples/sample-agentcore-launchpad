"""N16-obs88 — the public ``/v1`` entrance refuses an ENDED chat session exactly as the
console route does (409 ``chat.session_ended {session_id, ended_at}``, before any runtime
call, the ledger untouched). The gate lives in the shared invoke chain
(``services.invoke.require_session_open`` → ``chat_ledger.require_session_open``), so the
sync route inherits it, the stream route answers a plain 409 before the response opens,
and ``track_session`` no longer clears ``ended_at`` for anyone."""

import pytest

import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.db import SessionLocal
from app.core.display_time import display_iso, to_display_payload
from app.core.errors import AppError
from app.models.ledger import Agent
from app.services import chat_ledger
from app.services.agentcore import runtime as rt
from tests.graph.executor import ledger_digest
from tests.test_chat_session_stop import StopDataPlane, _row, make_agent, start_session


def _ended_session(client, monkeypatch, name: str) -> tuple[str, str, str, str]:
    """An agent, a console session ended through ``…/stop``, and a minted ``/v1`` key."""
    agent_id = make_agent(name=name)
    sid = start_session(client, monkeypatch, agent_id)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: StopDataPlane())
    ended_at = client.post(f"/api/chat/{agent_id}/sessions/{sid}/stop").json()["ended_at"]
    assert _row(agent_id, sid).ended_at is not None
    key = client.post("/api/apikeys", json={"name": f"{name}-key"}).json()["key"]
    return agent_id, sid, ended_at, key


def _runtime_recorder(monkeypatch) -> list[str]:
    """The data-plane seam: records every session id the chain would invoke."""
    invoked: list[str] = []

    def fake_invoke(_client, _arn, _prompt, *, session_id, **_kw):
        invoked.append(session_id)
        return {"text": "ok", "session_id": session_id}

    monkeypatch.setattr(rt, "invoke_runtime_text", fake_invoke)
    return invoked


def test_v1_sync_invoke_on_an_ended_session_is_409_and_writes_nothing(client, monkeypatch):
    agent_id, sid, ended_at, key = _ended_session(client, monkeypatch, "v1-ended-sync")
    invoked = _runtime_recorder(monkeypatch)
    turns_before = _row(agent_id, sid).turns
    before = ledger_digest()

    res = client.post(f"/v1/agents/{agent_id}/invoke",
                      json={"prompt": "again via v1", "session_id": sid},
                      headers={"X-Api-Key": key})

    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "chat.session_ended"
    assert body["detail"] == {"session_id": sid, "ended_at": ended_at}
    assert invoked == []  # refused before any runtime call
    assert ledger_digest() == before  # the refusal writes nothing — no row, no stamp
    row = _row(agent_id, sid)
    assert row.ended_at is not None and row.turns == turns_before
    # the console agrees — the two entrances answer one and the same envelope
    console = client.post(f"/api/chat/{agent_id}", json={"prompt": "again", "session_id": sid})
    assert console.status_code == 409 and console.json()["detail"] == body["detail"]


def test_v1_stream_invoke_on_an_ended_session_is_a_plain_409(client, monkeypatch):
    agent_id, sid, ended_at, key = _ended_session(client, monkeypatch, "v1-ended-stream")
    invoked: list[str] = []
    monkeypatch.setattr(
        chat_service, "invoke_agent_events",
        lambda a, p, session_id=None, **_kw: invoked.append(session_id) or iter([]),
    )
    before = ledger_digest()

    res = client.post(f"/v1/agents/{agent_id}/invoke-stream",
                      json={"prompt": "again via v1", "session_id": sid},
                      headers={"X-Api-Key": key})

    assert res.status_code == 409, res.text
    assert res.headers["content-type"].startswith("application/json")  # not an SSE error
    assert res.json()["code"] == "chat.session_ended"
    assert res.json()["detail"] == {"session_id": sid, "ended_at": ended_at}
    assert invoked == []
    assert ledger_digest() == before  # record_stream never upserted the row


def test_v1_new_session_id_still_answers_200_after_a_refusal(client, monkeypatch):
    agent_id, sid, _ended_at, key = _ended_session(client, monkeypatch, "v1-ended-fresh")
    invoked = _runtime_recorder(monkeypatch)
    fresh = "fresh-" + "f" * 40

    refused = client.post(f"/v1/agents/{agent_id}/invoke",
                          json={"prompt": "old", "session_id": sid}, headers={"X-Api-Key": key})
    assert refused.status_code == 409
    res = client.post(f"/v1/agents/{agent_id}/invoke",
                      json={"prompt": "new", "session_id": fresh}, headers={"X-Api-Key": key})

    assert res.status_code == 200, res.text
    assert res.json()["session_id"] == fresh and invoked == [fresh]
    sessions = {s["session_id"]: s for s in
                client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]}
    assert sessions[fresh]["source"] == "public_api" and sessions[fresh]["ended_at"] is None
    assert sessions[sid]["ended_at"] is not None  # the ended row stayed ended


def test_shared_chain_refuses_before_the_data_plane_is_built(client, monkeypatch):
    """The gate is ``invoke_agent_text`` / ``invoke_agent_events`` themselves (ONE_CHAIN),
    not a router: a direct caller with the ended id is refused before ``data_client``."""
    agent_id, sid, ended_at, _key = _ended_session(client, monkeypatch, "v1-ended-chain")
    built: list[object] = []
    monkeypatch.setattr(invoke_service, "data_client",
                        lambda ws=None: built.append(ws) or StopDataPlane())
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
    finally:
        db.close()

    with pytest.raises(AppError) as sync_exc:
        invoke_service.invoke_agent_text(agent, "hi", session_id=sid)
    with pytest.raises(AppError) as stream_exc:
        next(invoke_service.invoke_agent_events(agent, "hi", session_id=sid))

    for exc in (sync_exc.value, stream_exc.value):
        assert exc.status_code == 409 and exc.code == "chat.session_ended"
        # in process the detail is the ledger value; the wire carries it at +08:00 (N17-obs463)
        assert to_display_payload(exc.detail) == {"session_id": sid, "ended_at": ended_at}
    assert built == []
    # an unknown id (a new session) and a missing id pass the gate
    invoke_service.require_session_open(agent, "n" * 40)
    invoke_service.require_session_open(agent, None)


def test_track_session_keeps_ended_at_for_every_entrance(client, monkeypatch):
    """The ledger side of the ruling: the upsert no longer re-opens an ended row, so even a
    writer that bypassed the gate could not make the rail stop calling it ended."""
    agent_id, sid, ended_at, _key = _ended_session(client, monkeypatch, "v1-ended-track")
    row = _row(agent_id, sid)
    chat_ledger.track_session(row.workspace_id, agent_id, sid, "api",
                              source=chat_ledger.SOURCE_PUBLIC_API)
    after = _row(agent_id, sid)
    assert after.ended_at is not None and display_iso(after.ended_at) == ended_at
    assert after.turns == row.turns + 1
