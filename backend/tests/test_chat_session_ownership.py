# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_agent_visibility
"""Chat sessions are PERSONAL in BOTH visibility modes — the regression net for the whole
class of N16-obs70 / N16-obs71 (explore-1 channel B, t1 build aaacb0a6, 2026-10-19):

* a MEMBER could `POST …/sessions/{admin sid}/stop` → 200 and end the administrator's live
  session, read `GET …/memory?session_id=<admin sid>` → 200 with the admin's actor, and
  `POST /api/chat/{agent}` with the admin's idle `session_id` → 200 appending their turn
  INTO the admin's session, because `stop` and `memory` gated the session only under
  `isolated` while `history` gated always; the reattach answered 409 `turn_not_yours
  {author}` for a hidden session (existence + author leak).

Contract (users-and-ownership §4 / §7, E04 NC-E04-06): a member sees and may act only on
the sessions they opened, an administrator on every session, in BOTH modes; another
member's session id answers the SAME `404 chat.session_not_found` as an unknown one. One
choke point (`routers/chat._session_for` → `session_visibility.require_own`) serves every
by-id chat route, so the matrix below is asserted per route × role × mode.

N16-obs72: `session_id` is validated by character class as well as length (botocore
`[a-zA-Z0-9][a-zA-Z0-9-_]*`, 33–100) before the ledger and any AWS call; `AgentSpec.env`
is capped at the Runtime's 50 entries.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import app.routers.agents as agents_router
import app.routers.chat as chat_router
import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, ChatMessage, ChatSession, ChatTurnEvent
from app.schemas.agent import (
    AGENT_ENV_MAX_VARS,
    PLATFORM_ENV_RESERVED,
    RUNTIME_ENV_LIMIT,
    AgentSpec,
)
from app.services import memory_backend as mb
from app.services import users as users_service
from app.services import visibility
from app.services.agentcore import runtime as rt
from tests import test_agent_visibility as tav
from tests.test_visibility import _add, _isolate

# the auth-enabled console, its members (ada, bob), the admin-role user carol and the
# config admin — and a creator-unknown agent every one of them sees in BOTH modes, so a
# 404 below is the SESSION gate's, never the agent's
app, admin, ada, bob, world = tav.app, tav.admin, tav.ada, tav.bob, tav.world
no_real_deploy = tav.no_real_deploy

MODES = ("shared", "isolated")
ROUTES = ("history", "memory", "stop", "reattach", "post")
# owner = the member who opened the session; other = another approved member;
# admin = the config admin (no users row); admin_user = a users row with role admin
ROLES = ("owner", "other", "admin", "admin_user")
NOT_FOUND = (404, "chat.session_not_found")
# a 2xx for everyone who may act on the session; the one exception is the reattach of a
# turn the caller did not SEND (an administrator on the member's turn): 409, never 404 —
# the administrator may read the session, so nothing leaks
EXPECTED: dict[str, dict[str, tuple[int, str | None]]] = {
    route: {"owner": (200, None), "other": NOT_FOUND, "admin": (200, None),
            "admin_user": (200, None)}
    for route in ROUTES
}
EXPECTED["reattach"]["admin"] = (409, "chat.turn_not_yours")
EXPECTED["reattach"]["admin_user"] = (409, "chat.turn_not_yours")

_cell = {"n": 0}


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch):
    monkeypatch.setattr(users_service, "_ITERATIONS", 1_000)


def _seed_session(agent_id: str, created_by: str, actor: str = "ada") -> str:
    """ada's session with one replayable turn; the id satisfies the runtime bounds."""
    _cell["n"] += 1
    sid = f"sess-ada-own-{_cell['n']:04d}-" + "a" * 24
    assert len(sid) >= rt.SESSION_ID_MIN_LEN and rt.SESSION_ID_RE.fullmatch(sid)
    _add(ChatSession(agent_id=agent_id, session_id=sid, actor_id=actor, created_by=created_by,
                     created_by_name=actor, turns=1))
    _add(ChatMessage(agent_id=agent_id, session_id=sid, role="user", text="hi"))
    return sid


def _messages(sid: str) -> int:
    db = SessionLocal()
    try:
        return db.query(ChatMessage).filter(ChatMessage.session_id == sid).count()
    finally:
        db.close()


def _delta(*_a, **_k):
    return iter([{"event": "delta", "data": {"text": "ok"}}])


def _call(client, route: str, base: str, sid: str):
    if route == "history":
        return client.get(f"{base}/history", params={"session_id": sid})
    if route == "memory":
        return client.get(f"{base}/memory", params={"session_id": sid})
    if route == "stop":
        return client.post(f"{base}/sessions/{sid}/stop")
    if route == "reattach":
        return client.get(f"{base}/sessions/{sid}/turns/1/stream")
    return client.post(base, json={"prompt": "again", "session_id": sid})


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("route", ROUTES)
def test_every_by_id_chat_route_is_own_or_admin_in_both_modes(monkeypatch, world, route, role,
                                                              mode):
    _isolate(value=visibility.SHARED if mode == "shared" else visibility.ISOLATED)
    agent_id = world.legacy_agent
    sid = _seed_session(agent_id, world.ada_id)
    before = _messages(sid)
    client = {"owner": world.ada, "other": world.bob, "admin": world.admin,
              "admin_user": world.carol}[role]
    expected_status, expected_code = EXPECTED[route][role]

    # every seam that would reach AWS: recorded for an allowed caller, fatal for a refused
    # one — a refusal must land BEFORE the data plane is touched
    stopped: list[str] = []
    summarised: list[tuple] = []
    if expected_status == 404:
        monkeypatch.setattr(chat_router, "stop_agent_session",
                            lambda *a, **k: pytest.fail("stop reached AWS for a hidden session"))
        monkeypatch.setattr(chat_router, "chat_stream",
                            lambda *a, **k: pytest.fail("a turn started on a hidden session"))
        monkeypatch.setattr(mb, "get_backend", lambda: SimpleNamespace(
            session_memory_summary=lambda *a, **k: pytest.fail("memory read for a hidden session")))
    else:
        monkeypatch.setattr(chat_router, "stop_agent_session",
                            lambda agent, s, workspace: stopped.append(s) or
                            {"ended": True, "already_ended": False})
        monkeypatch.setattr(chat_service, "invoke_agent_events", _delta)
        monkeypatch.setattr(mb, "get_backend", lambda: SimpleNamespace(
            session_memory_summary=lambda ws, actor, s, **k: summarised.append((actor, s)) or
            {"short_term": [], "long_term": []}))

    res = _call(client, route, f"/api/chat/{agent_id}", sid)

    assert res.status_code == expected_status, (route, role, mode, res.text)
    if expected_code is not None:
        assert res.json()["code"] == expected_code, (route, role, mode, res.text)
    if expected_status == 404:
        # no `detail` naming an author or an owner — and, where an unknown id is a 404 too
        # (stop, reattach), the very same envelope
        assert res.json()["detail"] is None
        if route in ("stop", "reattach"):
            unknown = _call(client, route, f"/api/chat/{agent_id}", "sess-unknown-" + "z" * 30)
            assert (unknown.status_code, unknown.json()["code"]) == NOT_FOUND
            assert unknown.json()["detail"] is None
        assert _messages(sid) == before  # nothing was appended into the hidden session
        return
    if route == "memory":
        # the rail echoes the SESSION owner's actor — ada's, whoever reads it
        assert res.json()["actor_id"] == f"{agent_id}__ada"
        assert summarised == [(f"{agent_id}__ada", sid)]
    elif route == "stop":
        assert stopped == [sid] and res.json()["ended"] is True
    elif route == "history":
        assert [m["text"] for m in res.json()["messages"]] == ["hi"]
    elif route == "reattach" and expected_status == 409:
        assert res.json()["detail"] == {"author": "ada"}
    elif route == "post":
        # the continued session keeps its first owner (the Memory actor) and gains the turn
        assert "event: done" in res.text and _messages(sid) == before + 2


def test_reattach_409_is_only_for_a_readable_sessions_turn_by_another_account(monkeypatch,
                                                                              world):
    """The precise meaning of `409 chat.turn_not_yours {author}` after N16-obs71: it is
    reachable only on a session the caller may read — their own, or any as an
    administrator — when THAT turn was sent by a different account (here: an
    administrator's turn in ada's session); another member's session is 404 before any
    turn is looked at, in both modes, so neither its existence nor its author leaks."""
    agent_id = world.legacy_agent
    sid = _seed_session(agent_id, world.ada_id)
    db = SessionLocal()
    try:  # turn 2 of ada's session was sent by the administrator (logged ``meta.author``)
        row = db.query(ChatSession).filter(ChatSession.session_id == sid).one()
        row.turns = 2
        db.add(ChatTurnEvent(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                             session_id=sid, turn=2, seq=1, event="meta",
                             data={"turn": 2, "author": "operator"}))
        db.commit()
    finally:
        db.close()
    base = f"/api/chat/{agent_id}/sessions/{sid}/turns"
    for mode in MODES:
        _isolate(value=visibility.SHARED if mode == "shared" else visibility.ISOLATED)
        # ada: her own turn streams, the administrator's turn is 409 naming its sender
        assert world.ada.get(f"{base}/1/stream").status_code == 200
        res = world.ada.get(f"{base}/2/stream")
        assert res.status_code == 409 and res.json()["code"] == "chat.turn_not_yours", mode
        assert res.json()["detail"] == {"author": "operator"}
        # the administrator: the reverse
        assert world.admin.get(f"{base}/2/stream").status_code == 200
        assert world.admin.get(f"{base}/1/stream").json()["detail"] == {"author": "ada"}
        # bob: 404 on both turns and on a turn the session never reached — one answer
        for turn in (1, 2, 7):
            res = world.bob.get(f"{base}/{turn}/stream")
            assert (res.status_code, res.json()["code"]) == NOT_FOUND, (mode, turn)
            assert res.json()["detail"] is None


def test_a_session_with_no_row_yet_stays_open_in_both_modes(monkeypatch, world):
    """The first turn's row lands at `meta`; until then the id has no owner: history is an
    empty thread and a POST under it starts the session for whoever holds the id."""
    monkeypatch.setattr(chat_service, "invoke_agent_events", _delta)
    for mode in MODES:
        _isolate(value=visibility.SHARED if mode == "shared" else visibility.ISOLATED)
        sid = f"sess-fresh-{mode}-" + "f" * 30
        res = world.bob.get(f"/api/chat/{world.legacy_agent}/history",
                            params={"session_id": sid})
        assert res.status_code == 200 and res.json()["messages"] == []
        res = world.bob.post(f"/api/chat/{world.legacy_agent}",
                             json={"prompt": "hi", "session_id": sid})
        assert res.status_code == 200 and "event: done" in res.text
        # … and from then on it is bob's: ada is refused, the administrator is not
        res = world.ada.get(f"/api/chat/{world.legacy_agent}/history",
                            params={"session_id": sid})
        assert (res.status_code, res.json()["code"]) == NOT_FOUND
        assert world.admin.get(f"/api/chat/{world.legacy_agent}/history",
                               params={"session_id": sid}).status_code == 200


def test_no_mode_conditional_gate_is_left_in_the_chat_router():
    """The N16-obs70 shape — `if viewer.filters: _require_listed(...)` — must not come
    back: the router resolves every session through the one helper."""
    import inspect

    source = inspect.getsource(chat_router)
    assert "viewer.filters" not in source
    assert "_require_listed" not in source
    assert source.count("_session_for(") == 6  # the definition + the five by-id routes


# ─── N16-obs72: session_id character class ────────────────────────────────────


CJK_40 = "会话" * 20
SPACED_40 = "sess " + "a" * 35
BAD_IDS = [
    pytest.param(CJK_40, id="cjk-40"),
    pytest.param(SPACED_40, id="space-40"),
    pytest.param("-" + "a" * 39, id="leading-dash"),
    pytest.param("a" * 39 + "\n", id="trailing-newline"),
    pytest.param("sess." + "a" * 35, id="dot"),
]


@pytest.mark.parametrize("sid", BAD_IDS)
def test_post_refuses_an_id_outside_the_character_class_before_the_ledger_and_aws(
    client, monkeypatch, sid
):
    agent_id = _add(Agent(name="syntax-agent", method="zip_runtime", status="active",
                          arn=tav.ARN.format(rid="syntax_abcdefghij"), spec={"name": "s"}))
    monkeypatch.setattr(chat_router, "chat_stream",
                        lambda *a, **k: pytest.fail("chat_stream was reached"))
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "session_id": sid})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "chat.session_id_invalid"
    assert body["detail"]["pattern"] == rt.SESSION_ID_PATTERN == "[a-zA-Z0-9][a-zA-Z0-9-_]*"
    assert body["detail"]["length"] == len(sid)
    assert "omit it" in body["message"]
    db = SessionLocal()
    try:
        assert db.query(ChatSession).filter(ChatSession.session_id == sid).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize("route", ("history", "memory", "stop", "reattach"))
def test_by_id_routes_refuse_an_id_outside_the_character_class_before_the_ledger(
    client, monkeypatch, route
):
    agent_id = _add(Agent(name=f"syntax-{route}", method="zip_runtime", status="active",
                          arn=tav.ARN.format(rid=f"syntax_{route}_abcdefghij"),
                          spec={"name": "s"}))
    read: list[str] = []
    monkeypatch.setattr(chat_router, "stop_agent_session",
                        lambda *a, **k: pytest.fail("stop reached AWS"))
    monkeypatch.setattr(mb, "get_backend", lambda: SimpleNamespace(
        session_memory_summary=lambda ws, actor, s, **k: read.append(s) or {}))
    res = _call(client, route, f"/api/chat/{agent_id}", CJK_40)
    assert res.status_code == 422, (route, res.text)
    assert res.json()["code"] == "chat.session_id_invalid"
    assert res.json()["detail"]["pattern"] == rt.SESSION_ID_PATTERN
    assert read == []  # the memory backend was not consulted for the malformed id
    # a well-formed id of ANY length is judged by the ledger alone (the by-id routes answer
    # for rows, whose ids predate the length bounds): no row → the route's 404 (stop,
    # reattach) or its open answer (history: an empty thread; memory: the caller's own
    # partition, as before)
    res = _call(client, route, f"/api/chat/{agent_id}", "short-but-legal")
    if route in ("stop", "reattach"):
        assert (res.status_code, res.json()["code"]) == NOT_FOUND, res.text
    elif route == "history":
        assert res.status_code == 200 and res.json()["messages"] == []
    else:
        assert res.status_code == 200 and read == ["short-but-legal"]


@pytest.mark.parametrize(
    ("sid", "ok"),
    [
        pytest.param("a" * 32, False, id="32"),
        pytest.param("a" * 33, True, id="33"),
        pytest.param("a" * 100, True, id="100"),
        pytest.param("a" * 101, False, id="101"),
        pytest.param("A1-" + "_" * 30, True, id="class-mix"),
        pytest.param(CJK_40, False, id="cjk"),
        pytest.param(SPACED_40, False, id="space"),
        pytest.param("", True, id="empty-means-new"),
        pytest.param(None, True, id="none-means-new"),
    ],
)
def test_validate_session_id_pattern_and_length(sid, ok):
    if ok:
        invoke_service.validate_session_id(sid)
        return
    with pytest.raises(AppError) as exc:
        invoke_service.validate_session_id(sid)
    assert exc.value.code == "chat.session_id_invalid" and exc.value.status_code == 422
    assert exc.value.detail["min_length"] == 33 and exc.value.detail["max_length"] == 100
    # the length rule keeps its exact detail (pinned by test_chat_api); the character-class
    # rule adds the pattern
    assert ("pattern" in exc.value.detail) is not bool(rt.SESSION_ID_RE.fullmatch(sid))


# ─── N16-obs72: AgentSpec.env cap ─────────────────────────────────────────────


def _env(n: int) -> dict[str, str]:
    return {f"VAR_{i:03d}": "v" for i in range(n)}


def _spec(**kw: Any) -> dict[str, Any]:
    return {"name": "env-cap", "method": "zip_runtime", "system_prompt": "p", **kw}


def test_agent_spec_env_is_capped_at_the_runtime_limit_minus_the_platforms_keys():
    """Lead ruling 2026-10-19: the cap is what is left of the Runtime's 50 once the
    platform's own keys are reserved (`deployer/environment.PLATFORM_ENV_KEYS`, pinned equal
    in `test_runtime_environment`), and the message names that budget."""
    # N16-obs90 added LAUNCHPAD_A2A_ACTOR_KEY: 27 → 28 reserved, 23 → 22 for the member
    assert (RUNTIME_ENV_LIMIT, PLATFORM_ENV_RESERVED, AGENT_ENV_MAX_VARS) == (50, 28, 22)
    assert len(AgentSpec(**_spec(env=_env(AGENT_ENV_MAX_VARS))).env) == AGENT_ENV_MAX_VARS
    with pytest.raises(ValidationError) as exc:
        AgentSpec(**_spec(env=_env(AGENT_ENV_MAX_VARS + 1)))
    (error,) = exc.value.errors()
    assert error["loc"] == ("env",)
    assert "the platform reserves 28" in error["msg"] and "up to 22" in error["msg"]


def test_create_agent_with_one_env_var_too_many_is_422_before_the_pipeline(client, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    res = client.post("/api/agents", json=_spec(env=_env(AGENT_ENV_MAX_VARS + 1)))
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "validation.invalid_request"
    (error,) = [e for e in res.json()["detail"] if e["loc"][-1] == "env"]
    assert "reserves 28" in error["msg"] and "up to 22" in error["msg"]
    assert launched == []  # no job, no 130 s pipeline, no AWS ValidationException
    res = client.post("/api/agents", json=_spec(env=_env(AGENT_ENV_MAX_VARS)))
    assert res.status_code == 202, res.text
    assert len(launched) == 1
