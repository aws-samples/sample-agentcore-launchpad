"""Per-user policy (~21:40Z ruling): the readers that look a conversation up by its caller
session id follow it into the derived per-principal runtime sessions (``lpp-…``) — the
observability session list (folded) / trace list (mapped) / session detail, the chat memory
rail, the memory session list and passive evaluation runs over chat session ids. Without any
derived session everything is byte-identical to before."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.models.policy import PolicyRuntimeSession
from app.services import observability, runtime_sessions
from app.services.visibility import Viewer
from tests.conftest import ws_ctx

CALLER = "c" * 40
LPP_ADA = "lpp-" + "1" * 60
LPP_ADMIN = "lpp-" + "2" * 60


@pytest.fixture
def conversation():
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="governed", method="zip_runtime",
                      status="active", spec={})
        db.add(agent)
        db.commit()
        db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
                           session_id=CALLER, actor_id="ada", turns=3))
        base = datetime.now(UTC)
        for i, derived in enumerate((LPP_ADA, LPP_ADMIN)):
            db.add(PolicyRuntimeSession(
                agent_id=agent.id, session_id=CALLER, principal=f"user:p{i}",
                runtime_session_id=derived, created_at=base + timedelta(seconds=i)))
        db.commit()
        return agent.id
    finally:
        db.close()


def _db():
    return SessionLocal()


# ── the helper ──────────────────────────────────────────────────────────────


def test_runtime_sessions_for_and_conversation_of(conversation):
    db = _db()
    try:
        assert runtime_sessions.runtime_sessions_for(db, DEFAULT_WORKSPACE_ID, CALLER) == [
            LPP_ADA, LPP_ADMIN]
        assert runtime_sessions.runtime_sessions_for(db, "other-ws", CALLER) == []
        assert runtime_sessions.runtime_sessions_for(db, DEFAULT_WORKSPACE_ID, LPP_ADA) == []
        assert runtime_sessions.conversation_of(
            db, DEFAULT_WORKSPACE_ID, [LPP_ADA, LPP_ADMIN, "x" * 40]) == {
            LPP_ADA: CALLER, LPP_ADMIN: CALLER}
    finally:
        db.close()


def test_fold_session_rows_shows_derived_rows_under_their_conversation():
    rows = [
        {"session_id": LPP_ADA, "service": "svc", "traces": 2, "llm_calls": 3, "errors": 0,
         "tokens": {"input": 10, "output": 5}, "first": "2026-10-26T10:00:00",
         "last": "2026-10-26T10:05:00", "platform": False},
        {"session_id": "z" * 40, "service": "svc", "traces": 1, "llm_calls": 1, "errors": 1,
         "tokens": {"input": 1, "output": 1}, "first": "2026-10-26T09:00:00",
         "last": "2026-10-26T09:00:00", "platform": False},
        {"session_id": LPP_ADMIN, "service": "svc", "traces": 1, "llm_calls": 2, "errors": 1,
         "tokens": {"input": 4, "output": 2}, "first": "2026-10-26T09:30:00",
         "last": "2026-10-26T11:00:00", "platform": False},
    ]
    folded = runtime_sessions.fold_session_rows(rows, {LPP_ADA: CALLER, LPP_ADMIN: CALLER})
    assert [r["session_id"] for r in folded] == [CALLER, "z" * 40]
    conv = folded[0]
    assert (conv["traces"], conv["llm_calls"], conv["errors"]) == (3, 5, 1)
    assert conv["tokens"] == {"input": 14, "output": 7}
    assert (conv["first"], conv["last"]) == ("2026-10-26T09:30:00", "2026-10-26T11:00:00")
    assert conv["platform"] is True and conv["runtime_session_ids"] == [LPP_ADA, LPP_ADMIN]
    assert runtime_sessions.fold_session_rows(rows, {}) is rows  # nothing derived: unchanged


# ── observability ──────────────────────────────────────────────────────────


def _queries(monkeypatch, by_query):
    asked: list[str] = []

    def fake(queries, hours, logs=None, log_groups=None, workspace=None):
        asked.extend(queries.values())
        return {key: by_query(text) for key, text in queries.items()}

    observability.reset_cache()
    monkeypatch.setattr(observability, "run_insights_queries", fake)
    return asked


def test_the_session_list_folds_and_the_trace_list_maps(conversation, monkeypatch):
    def rows(text):
        if "count_distinct(traceId) as traces" in text:  # the session aggregate
            return [{"session_id": LPP_ADA, "service": "svc", "traces": "2", "llm_calls": "1"},
                    {"session_id": LPP_ADMIN, "service": "svc", "traces": "1", "llm_calls": "1"}]
        return [{"traceId": "t1", "session_id": LPP_ADA, "service": "svc"}]

    _queries(monkeypatch, rows)
    db = _db()
    try:
        sessions = observability.list_sessions("24h", db, ws_ctx(id=DEFAULT_WORKSPACE_ID),
                                               force=True)["sessions"]
        traces = observability.list_traces("24h", db, ws_ctx(id=DEFAULT_WORKSPACE_ID),
                                           force=True)["traces"]
    finally:
        db.close()
    assert [(s["session_id"], s["traces"]) for s in sessions] == [(CALLER, 3)]
    assert sessions[0]["runtime_session_ids"] == [LPP_ADA, LPP_ADMIN]
    assert traces and traces[0]["session_id"] == CALLER
    assert traces[0]["runtime_session_id"] == LPP_ADA


def test_the_session_detail_reads_the_derived_sessions_too(conversation, monkeypatch):
    asked = _queries(monkeypatch, lambda text: [])
    monkeypatch.setattr(observability, "session_transcript", lambda *a, **k: {})
    from app.evaluation import online as online_eval

    monkeypatch.setattr(online_eval, "session_online_scores", lambda *a, **k: {})
    db = _db()
    try:
        observability.get_session(CALLER, "24h", db, ws_ctx(id=DEFAULT_WORKSPACE_ID),
                                  force=True)
    finally:
        db.close()
    joined = "\n".join(asked)
    for sid in (CALLER, LPP_ADA, LPP_ADMIN):
        assert f'attributes.session.id = "{sid}"' in joined


def test_without_derived_sessions_the_readers_are_unchanged(monkeypatch):
    asked = _queries(monkeypatch, lambda text: [])
    monkeypatch.setattr(observability, "session_transcript", lambda *a, **k: {})
    from app.evaluation import online as online_eval

    monkeypatch.setattr(online_eval, "session_online_scores", lambda *a, **k: {})
    db = _db()
    try:
        observability.get_session(CALLER, "24h", db, ws_ctx(id=DEFAULT_WORKSPACE_ID),
                                  force=True)
        assert runtime_sessions.expand_session_ids(db, DEFAULT_WORKSPACE_ID, [CALLER]) == [
            CALLER]
    finally:
        db.close()
    assert sum('attributes.session.id = "' in q for q in asked) == 1


def test_an_isolated_viewer_gets_no_derived_ids_of_a_conversation_it_cannot_see(
        conversation, monkeypatch):
    from app.services import visibility

    db = _db()
    try:
        row = db.query(ChatSession).filter_by(session_id=CALLER).first()
        row.created_by = "u_someone_else"
        db.commit()
        viewer = Viewer(user_id="u_bob", filters=True)
        monkeypatch.setattr(visibility, "sees", lambda v, d, created_by: created_by == "u_bob")
        assert runtime_sessions.runtime_sessions_for(db, DEFAULT_WORKSPACE_ID, CALLER,
                                                     viewer) == []
    finally:
        db.close()


# ── passive evaluation ─────────────────────────────────────────────────────


def test_a_passive_run_reads_the_derived_sessions_instead_of_the_empty_caller(conversation):
    db = _db()
    try:
        out = runtime_sessions.expand_session_ids(db, DEFAULT_WORKSPACE_ID,
                                                  [CALLER, "other" + "o" * 35])
    finally:
        db.close()
    assert out == [LPP_ADA, LPP_ADMIN, "other" + "o" * 35]


def test_a_conversation_with_turns_before_the_carrier_keeps_its_caller_id(conversation):
    db = _db()
    try:
        db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=conversation,
                           session_id=CALLER, role="user", text="before the switch",
                           created_at=datetime.now(UTC) - timedelta(hours=2)))
        db.commit()
        out = runtime_sessions.expand_session_ids(db, DEFAULT_WORKSPACE_ID, [CALLER])
    finally:
        db.close()
    assert out == [CALLER, LPP_ADA, LPP_ADMIN]


def test_plan_run_uses_the_expanded_sessions(conversation, monkeypatch):
    from app.evaluation import service as svc
    from tests.evaluation.test_resume import make_run

    run_id = make_run(status="queued", mode="evaluators", agent_id=conversation)
    monkeypatch.setattr(svc, "snapshot_definitions", lambda _run: [])
    plan = svc.plan_run(
        run_id, workspace=ws_ctx(id=DEFAULT_WORKSPACE_ID), agent_arn="arn", agent_id=conversation,
        protocol="http", service_name="svc", log_group="/lg", log_groups=["/lg"], items=[],
        evaluators=[], mode="evaluators", wait_seconds=0, existing_session_ids=[CALLER],
        time_range=None, insights=None, actor_model_id=None, runtime_user_id=None,
        judge_model_id="judge", failure_detection_model_id=None)
    assert plan.session_ids == [LPP_ADA, LPP_ADMIN]


# ── memory ─────────────────────────────────────────────────────────────────


def test_the_chat_memory_rail_merges_the_conversations_runtime_sessions():
    from app.routers.chat import _merge_memory_summaries

    merged = _merge_memory_summaries(
        {"event_count": 2, "records": [{"namespace": "/facts/a", "text": "likes tea"}]},
        [{"event_count": 3, "records": [{"namespace": "/facts/a", "text": "likes tea"},
                                        {"namespace": "/summaries/a/x", "text": "asked pay"}]}])
    assert merged["event_count"] == 5
    assert merged["records"] == [{"namespace": "/facts/a", "text": "likes tea"},
                                 {"namespace": "/summaries/a/x", "text": "asked pay"}]


def test_the_memory_session_list_names_the_conversation(conversation, client, monkeypatch):
    from types import SimpleNamespace

    from app.services import memory_backend

    page = {"items": [{"session_id": LPP_ADA}, {"session_id": "q" * 40}], "next_token": None}
    monkeypatch.setattr(memory_backend, "get_backend", lambda: SimpleNamespace(
        list_sessions=lambda *a, **k: {**page, "items": [dict(i) for i in page["items"]]}))
    res = client.get("/api/memory/sessions", params={"actor_id": f"{conversation}__ada"})
    assert res.status_code == 200, res.text
    items = {i["session_id"]: i for i in res.json()["items"]}
    assert items[LPP_ADA]["conversation_session_id"] == CALLER
    assert items[LPP_ADA]["ledger"]["agent_id"] == conversation
    assert items[LPP_ADA]["ledger"]["turns"] == 3
    assert "conversation_session_id" not in items["q" * 40]
    assert items["q" * 40]["ledger"] is None
