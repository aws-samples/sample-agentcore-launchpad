"""Thumbs feedback on console chat answers: the `saved` SSE event, history id +
verdict, rate / re-rate / withdraw, one verdict per person, the 404 matrix,
workspace scope, member visibility isolation, the range-filtered list and the
console-v3 column-superset ledger."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import app.routers.chat as chat_router
from app.core import db as db_module
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ChatFeedback, ChatMessage, Workspace
from app.services import feedback as feedback_service
from tests import test_visibility as tv
from tests.test_visibility import _isolate

# the auth-enabled console + two members of the workspace visibility suite
app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob


def make_agent(name="fb-agent", created_by=None, workspace_id=DEFAULT_WORKSPACE_ID) -> str:
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=workspace_id, name=name, method="zip_runtime", status="active",
            arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
            spec={"name": name}, created_by=created_by,
            created_by_name="someone" if created_by else None,
        )
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def sse(res) -> list[tuple[str, dict]]:
    out = []
    for frame in res.text.split("\n\n"):
        if not frame.strip() or frame.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in frame.split("\n") if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


SCRIPT: dict[str, list[dict]] = {}


@pytest.fixture(autouse=True)
def scripted(monkeypatch):
    """Replace the invoke chain with a scripted stream (default: one answer)."""
    SCRIPT.clear()

    def fake_stream(agent, prompt, session_id=None, actor_id="river", **kwargs):
        sid = session_id or ("s" * 40)
        yield {"event": "meta", "data": {"session_id": sid, "agent": agent.name}}
        yield from SCRIPT.get("events") or [
            {"event": "delta", "data": {"text": f"answer to {prompt}"}},
        ]
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(chat_router, "chat_stream", fake_stream)


def turn(client, agent_id, prompt, session_id=None):
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": prompt, "session_id": session_id})
    assert res.status_code == 200, res.text
    events = sse(res)
    sid = events[0][1]["session_id"]
    message_id = next(d["message_id"] for e, d in events if e == "saved")
    return sid, message_id


def rate(client, message_id, verdict):
    return client.post("/api/feedback", json={"message_id": message_id, "verdict": verdict})


# ─── the message id ──────────────────────────────────────────────────────────


def test_saved_event_carries_the_persisted_id_before_the_closing_event(client):
    agent_id = make_agent()
    SCRIPT["events"] = [
        {"event": "delta", "data": {"text": "let me look"}},
        {"event": "tool", "data": {"name": "lookup"}},
        {"event": "delta", "data": {"text": "found it"}},
    ]
    events = sse(client.post(f"/api/chat/{agent_id}", json={"prompt": "q"}))
    kinds = [e for e, _ in events]
    assert kinds == ["meta", "delta", "saved", "tool", "delta", "saved", "done"]
    saved = [d["message_id"] for e, d in events if e == "saved"]

    db = SessionLocal()
    try:
        answers = db.query(ChatMessage).filter_by(role="agent").order_by(ChatMessage.id).all()
        assert [(m.id, m.text) for m in answers] == list(
            zip(saved, ["let me look", "found it"], strict=True))
    finally:
        db.close()


def test_no_saved_event_without_an_answer_and_on_error_before_it(client):
    agent_id = make_agent()
    SCRIPT["events"] = [{"event": "tool", "data": {"name": "lookup"}}]
    assert "saved" not in [e for e, _ in sse(client.post(f"/api/chat/{agent_id}",
                                                         json={"prompt": "q"}))]
    SCRIPT["events"] = [
        {"event": "delta", "data": {"text": "partial"}},
        {"event": "error", "data": {"message": "boom"}},
    ]
    kinds = [e for e, _ in sse(client.post(f"/api/chat/{agent_id}", json={"prompt": "q"}))]
    assert kinds[kinds.index("saved") + 1] == "error"


def test_history_has_ids_and_the_callers_verdict(client):
    agent_id = make_agent()
    sid, message_id = turn(client, agent_id, "hi")
    history = client.get(f"/api/chat/{agent_id}/history", params={"session_id": sid}).json()
    rows = history["messages"]
    assert all(isinstance(m["id"], int) for m in rows)
    assert [m["id"] for m in rows] == sorted(m["id"] for m in rows)
    assert [(m["role"], m["verdict"]) for m in rows] == [("user", None), ("agent", None)]
    assert rows[1]["id"] == message_id

    assert rate(client, message_id, "up").status_code == 200
    rows = client.get(f"/api/chat/{agent_id}/history", params={"session_id": sid}).json()
    assert [m["verdict"] for m in rows["messages"]] == [None, "up"]


# ─── rate / re-rate / withdraw ───────────────────────────────────────────────


def test_thumbs_down_is_stored_and_listed_as_a_bad_case(client):
    agent_id = make_agent()
    sid, message_id = turn(client, agent_id, "what is 2+2")
    res = rate(client, message_id, "down")
    assert res.status_code == 200 and res.json() == {"message_id": message_id, "verdict": "down"}

    db = SessionLocal()
    try:
        row = db.query(ChatFeedback).one()
        assert (row.workspace_id, row.agent_id, row.session_id, row.message_id) == (
            DEFAULT_WORKSPACE_ID, agent_id, sid, message_id)
        assert (row.verdict, row.source, row.actor) == ("down", "console", "river")
    finally:
        db.close()

    body = client.get("/api/feedback", params={"verdict": "down"}).json()
    assert body["totals"] == {"up": 0, "down": 1}
    assert body["down_session_ids"] == [sid]  # what from-sessions takes as-is
    item = body["items"][0]
    assert item["question"] == "what is 2+2" and item["answer"] == "answer to what is 2+2"
    assert item["agent_name"] == "fb-agent" and item["actor"] == "river"
    assert item["created_at"].endswith("+00:00") and item["answered_at"].endswith("+00:00")


def test_revoting_updates_and_null_withdraws(client):
    agent_id = make_agent()
    _, message_id = turn(client, agent_id, "q")
    rate(client, message_id, "down")
    rate(client, message_id, "up")
    body = client.get("/api/feedback").json()
    assert body["totals"] == {"up": 1, "down": 0} and body["down_session_ids"] == []
    assert len(body["items"]) == 1  # one verdict per person and message
    assert rate(client, message_id, None).json() == {"message_id": message_id, "verdict": None}
    assert client.get("/api/feedback").json()["totals"] == {"up": 0, "down": 0}
    assert rate(client, message_id, None).status_code == 200  # withdrawing nothing is fine


def test_the_question_is_the_closest_preceding_user_turn(client):
    agent_id = make_agent()
    sid, _ = turn(client, agent_id, "first")
    _, second = turn(client, agent_id, "second", session_id=sid)
    rate(client, second, "down")
    item = client.get("/api/feedback").json()["items"][0]
    assert (item["question"], item["answer"]) == ("second", "answer to second")


def test_previews_are_capped(client):
    agent_id = make_agent()
    long = "x" * 2000
    SCRIPT["events"] = [{"event": "delta", "data": {"text": long}}]
    _, message_id = turn(client, agent_id, long)
    rate(client, message_id, "down")
    item = client.get("/api/feedback").json()["items"][0]
    assert len(item["question"]) == len(item["answer"]) == feedback_service.PREVIEW_CHARS


def test_only_an_answer_in_this_workspace_can_be_rated(client):
    agent_id = make_agent()
    _, message_id = turn(client, agent_id, "q")
    db = SessionLocal()
    user_msg = db.query(ChatMessage).filter_by(role="user").one().id
    db.close()

    for target in (user_msg, 99999):  # a question, not an answer / unknown
        res = rate(client, target, "down")
        assert res.status_code == 404 and res.json()["code"] == "feedback.message_not_found"
    for body in ({"message_id": message_id, "verdict": "meh"},
                 {"message_id": message_id},  # verdict is required (null withdraws)
                 {"message_id": 0, "verdict": "up"}):
        assert client.post("/api/feedback", json=body).status_code == 422


def test_feedback_is_workspace_scoped(client):
    agent_id = make_agent()
    _, message_id = turn(client, agent_id, "q")
    rate(client, message_id, "down")
    db = SessionLocal()
    db.add(Workspace(id="lab", name="lab", account_id="222233334444", region="us-east-2",
                     bootstrap_status="ready"))
    db.commit()
    db.close()
    other = client.get("/api/feedback", headers={"X-Workspace": "lab"}).json()
    assert other["totals"] == {"up": 0, "down": 0} and other["items"] == []
    # nor can it be rated through another workspace's console
    res = client.post("/api/feedback", headers={"X-Workspace": "lab"},
                      json={"message_id": message_id, "verdict": "down"})
    assert res.status_code == 404


# ─── the range-filtered list ─────────────────────────────────────────────────


def _age(message_id: int, hours: float) -> None:
    db = SessionLocal()
    try:
        then = datetime.now(UTC) - timedelta(hours=hours)
        db.query(ChatFeedback).filter_by(message_id=message_id).update(
            {"updated_at": then, "created_at": then}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_range_filters_items_totals_and_down_sessions(client):
    agent_id = make_agent()
    fresh_sid, fresh = turn(client, agent_id, "fresh", session_id="fresh".ljust(40, "x"))
    old_sid, old = turn(client, agent_id, "old", session_id="old".ljust(40, "x"))
    _, older = turn(client, agent_id, "older", session_id="older".ljust(40, "x"))
    rate(client, fresh, "down")
    rate(client, old, "down")
    rate(client, older, "up")
    _age(old, 30)
    _age(older, 3)

    def listed(range_key):
        return client.get("/api/feedback", params={"range": range_key}).json()

    one = listed("1h")
    assert one["totals"] == {"up": 0, "down": 1}
    assert [i["message_id"] for i in one["items"]] == [fresh]
    assert one["down_session_ids"] == [fresh_sid]
    assert listed("6h")["totals"] == {"up": 1, "down": 1}
    week = listed("7d")
    assert week["totals"] == {"up": 1, "down": 2}
    assert [i["message_id"] for i in week["items"]] == [fresh, older, old]  # newest first
    assert week["down_session_ids"] == [fresh_sid, old_sid]
    assert listed("24h")["totals"] == {"up": 1, "down": 1}  # the default range too
    assert client.get("/api/feedback").json()["totals"] == {"up": 1, "down": 1}
    assert client.get("/api/feedback", params={"range": "30d"}).status_code == 422


def test_down_sessions_are_distinct_newest_first_and_capped(client, monkeypatch):
    monkeypatch.setattr(feedback_service, "MAX_SESSION_IDS", 2)
    agent_id = make_agent()
    made = []
    for n in range(3):
        sid, mid = turn(client, agent_id, f"q{n}", session_id=f"session-{n}".ljust(40, "x"))
        _, again = turn(client, agent_id, f"again{n}", session_id=sid)
        made.append((sid, mid, again))
    for n, (_sid, mid, again) in enumerate(made):
        rate(client, mid, "down")
        rate(client, again, "down")
        _age(mid, 3 - n)
        _age(again, 3 - n)
    ids = client.get("/api/feedback", params={"range": "6h"}).json()["down_session_ids"]
    assert ids == [made[2][0], made[1][0]]


# ─── people and visibility ───────────────────────────────────────────────────


def test_one_verdict_per_person_and_each_sees_their_own(app, admin, ada, bob):
    _, ada_client = ada
    _, bob_client = bob
    agent_id = make_agent()
    sid, message_id = turn(ada_client, agent_id, "q")
    assert rate(ada_client, message_id, "down").status_code == 200
    assert rate(bob_client, message_id, "up").status_code == 200
    assert rate(bob_client, message_id, "down").status_code == 200  # re-rate, same row

    db = SessionLocal()
    try:
        rows = db.query(ChatFeedback.actor, ChatFeedback.verdict).order_by(ChatFeedback.actor)
        assert rows.all() == [("ada", "down"), ("bob", "down")]
    finally:
        db.close()
    rate(ada_client, message_id, None)

    def verdict(client):
        rows = client.get(f"/api/chat/{agent_id}/history", params={"session_id": sid}).json()
        return [m["verdict"] for m in rows["messages"] if m["role"] == "agent"]

    assert verdict(ada_client) == [None] and verdict(bob_client) == ["down"]
    assert admin.get("/api/feedback").json()["totals"] == {"up": 0, "down": 1}


def test_isolated_member_cannot_rate_or_list_a_hidden_agents_answers(app, admin, ada, bob):
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    ada_agent = make_agent("ada-agent", created_by=ada_id)
    bob_agent = make_agent("bob-agent", created_by=bob_id)
    ada_sid, ada_msg = turn(ada_client, ada_agent, "ada asks", session_id="a".ljust(40, "a"))
    bob_sid, bob_msg = turn(bob_client, bob_agent, "bob asks", session_id="b".ljust(40, "b"))
    rate(ada_client, ada_msg, "down")
    rate(bob_client, bob_msg, "down")
    # shared (default): everyone sees, and may rate, every agent's answers
    assert bob_client.get("/api/feedback").json()["totals"] == {"up": 0, "down": 2}
    assert rate(bob_client, ada_msg, "up").status_code == 200
    rate(bob_client, ada_msg, None)

    _isolate()
    res = rate(bob_client, ada_msg, "down")
    assert res.status_code == 404 and res.json()["code"] == "feedback.message_not_found"
    bob_view = bob_client.get("/api/feedback").json()
    assert bob_view["totals"] == {"up": 0, "down": 1}
    assert [i["agent_id"] for i in bob_view["items"]] == [bob_agent]
    assert bob_view["down_session_ids"] == [bob_sid]
    # asking for the hidden agent by id reads as empty, not as its feedback
    hidden = bob_client.get("/api/feedback", params={"agent_id": ada_agent}).json()
    assert hidden["items"] == [] and hidden["totals"] == {"up": 0, "down": 0}

    ada_view = ada_client.get("/api/feedback").json()
    assert ada_view["down_session_ids"] == [ada_sid]
    everything = admin.get("/api/feedback").json()
    assert everything["totals"] == {"up": 0, "down": 2}
    assert rate(admin, ada_msg, "up").status_code == 200  # admins see every agent


# ─── a ledger that ran console-v3 ────────────────────────────────────────────

# console-v3's `chat_feedback`, column for column (incl. `correction`)
V3_CHAT_FEEDBACK = """
CREATE TABLE chat_feedback (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(32),
    agent_id VARCHAR(32) NOT NULL,
    session_id VARCHAR(80) NOT NULL,
    message_id INTEGER NOT NULL,
    verdict VARCHAR(8) NOT NULL,
    comment TEXT,
    correction TEXT,
    actor VARCHAR(96) NOT NULL,
    source VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_chat_feedback_actor UNIQUE (message_id, actor)
)
"""


def test_a_console_v3_chat_feedback_table_is_no_drift(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'v3.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text(V3_CHAT_FEEDBACK))
        conn.execute(sa.text(
            "INSERT INTO chat_feedback VALUES ('f1', 'default', 'a1', 's1', 7, 'down',"
            " 'meh', 'fixed', 'share:abc', 'share', '2026-10-12 10:00:00',"
            " '2026-10-12 10:00:00')"
        ))
    assert "chat_feedback" not in db_module.schema_drift(engine)

    db_module.Base.metadata.create_all(bind=engine)  # skips the existing table
    db_module._migrate(engine)
    db_module.assert_no_schema_drift(engine)
    indexes = {ix["name"] for ix in sa.inspect(engine).get_indexes("chat_feedback")}
    assert "ix_chat_feedback_workspace_id" in indexes
    # the v3 row reads back through main's model, unknown source and all
    with Session(engine) as db:
        row = db.get(ChatFeedback, "f1")
        assert (row.verdict, row.source, row.actor) == ("down", "share", "share:abc")
    engine.dispose()
