"""Per-user policy re-review (~21:50Z, item 1): the per-principal runtime sessions the carrier
derives (``lpp-…``, ``principal_mint.runtime_session_id``) carry a conversation's spans; under
workspace isolation they are hidden exactly when their conversation (or its agent) is — a
member never sees another member's ``lpp-`` sessions in the trace / session lists or details."""

from __future__ import annotations

from tests import test_agent_visibility as tav
from tests.test_agent_visibility import _add, _isolate, _refused

app, admin, ada, bob, world = tav.app, tav.admin, tav.ada, tav.bob, tav.world
no_real_deploy = tav.no_real_deploy

ADA_LPP = "lpp-" + "a" * 60
ADMIN_LPP = "lpp-" + "b" * 60
HIDDEN_AGENT_LPP = "lpp-" + "c" * 60


def _derived(agent_id: str, session_id: str, principal: str, runtime_session_id: str) -> None:
    from app.core.db import SessionLocal
    from app.models.policy import PolicyRuntimeSession

    db = SessionLocal()
    try:
        db.add(PolicyRuntimeSession(agent_id=agent_id, session_id=session_id,
                                    principal=principal, runtime_session_id=runtime_session_id))
        db.commit()
    finally:
        db.close()


def _fake_observability(monkeypatch, session_ids):
    import app.routers.observability as obs_router
    from app.services import observability as obs

    rows = [{"trace_id": f"{i:032x}", "service": "legacy_agent.DEFAULT",
             "agent": "legacy-agent", "session_id": s, "status": "ok"}
            for i, s in enumerate(session_ids)]
    monkeypatch.setattr(obs, "list_traces", lambda *a, **k: {"traces": list(rows)})
    monkeypatch.setattr(obs, "list_sessions", lambda *a, **k: {"sessions": list(rows)})
    monkeypatch.setattr(obs, "get_session", lambda s, *a, **k: {"session_id": s, "traces": []})
    monkeypatch.setattr(obs, "run_insights_queries", lambda *a, **k: {
        "ownership": [{"service": "legacy_agent.DEFAULT", "record_count": "1",
                       "attributed_count": "1", "service_count": "1"}],
    })
    monkeypatch.setattr(obs_router.trace_service, "session_trace",
                        lambda s, ws, **k: {"session_id": s, "spans": [], "span_count": 0})


def test_another_members_derived_sessions_stay_hidden_under_isolation(monkeypatch, world):
    from app.models.ledger import ChatSession
    sid = "sess-ada-conversation-0001"
    _add(ChatSession(agent_id=world.legacy_agent, session_id=sid, actor_id="ada",
                     created_by=world.ada_id, created_by_name="ada", turns=2))
    for principal, derived in (("user:ada", ADA_LPP), ("user:admin", ADMIN_LPP)):
        _derived(world.legacy_agent, sid, principal, derived)
    # a conversation on ada's own agent (hidden from bob under isolation)
    _derived(world.ada_agent, "sess-on-ada-agent-01", "user:ada", HIDDEN_AGENT_LPP)
    other = "sess-nobody-0001"
    _fake_observability(monkeypatch, [sid, ADA_LPP, ADMIN_LPP, HIDDEN_AGENT_LPP, other])

    _isolate()
    for path, key in (("sessions", "sessions"), ("traces", "traces")):
        ids = {r["session_id"] for r in world.bob.get(f"/api/observability/{path}").json()[key]}
        assert ids == {other}, path  # none of ada's conversation, none of its lpp- sessions
    for derived in (ADA_LPP, ADMIN_LPP, HIDDEN_AGENT_LPP):
        _refused(world.bob, "observability.session_not_found",
                 ("GET", f"/api/observability/sessions/{derived}"))
    _refused(world.bob, "chat.session_not_found", ("GET", f"/api/traces/{sid}"))
    # ada and the administrator still see them
    for client in (world.ada, world.admin):
        ids = {r["session_id"] for r in client.get("/api/observability/sessions").json()[
            "sessions"]}
        assert {ADA_LPP, ADMIN_LPP} <= ids
        assert client.get(f"/api/observability/sessions/{ADA_LPP}").status_code == 200


def test_shared_mode_is_unchanged(monkeypatch, world):
    from app.models.ledger import ChatSession
    sid = "sess-ada-conversation-0002"
    _add(ChatSession(agent_id=world.legacy_agent, session_id=sid, actor_id="ada",
                     created_by=world.ada_id, created_by_name="ada", turns=1))
    _derived(world.legacy_agent, sid, "user:ada", ADA_LPP)
    _fake_observability(monkeypatch, [sid, ADA_LPP])
    ids = {r["session_id"] for r in world.bob.get("/api/observability/sessions").json()[
        "sessions"]}
    assert ids == {sid, ADA_LPP}  # shared: workspace-wide, as before
