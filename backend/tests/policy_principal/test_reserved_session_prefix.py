"""Per-user policy re-review (~21:50Z, item 2): ``lpp-`` names the per-principal runtime
sessions the hub derives — a caller may not pick one (it could land in another principal's
runtime process). 422 ``chat.session_id_reserved`` on EVERY entrance, before the gate and any
AWS call, with the switch on OR off."""

from __future__ import annotations

import pytest

import app.routers.agents as agents_router
import app.routers.chat as chat_router
import app.routers.public_api as public_api
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import ChatSession
from app.services import invoke as invoke_service
from app.services.agentcore import runtime as rt
from tests.test_chat_api import _mint_key, make_active_agent

RESERVED = "lpp-" + "0" * 60
UPPER = "LPP-" + "0" * 60


@pytest.fixture(params=[False, True], ids=["switch-off", "switch-on"])
def switch(request, monkeypatch):
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", request.param)
    return request.param


def _reserved(res) -> None:
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "chat.session_id_reserved" and body["detail"] == {"prefix": "lpp-"}


@pytest.mark.parametrize("sid", [RESERVED, UPPER])
def test_chat_refuses_a_reserved_session_id(client, monkeypatch, switch, sid):
    agent_id = make_active_agent(name="lpp-chat")
    monkeypatch.setattr(chat_router, "chat_stream",
                        lambda *a, **k: pytest.fail("chat_stream was reached"))
    _reserved(client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "session_id": sid}))
    db = SessionLocal()
    try:
        assert db.query(ChatSession).filter(ChatSession.session_id == sid).count() == 0
    finally:
        db.close()


def test_the_console_invoke_refuses_a_reserved_session_id(client, monkeypatch, switch):
    agent_id = make_active_agent(name="lpp-console")
    monkeypatch.setattr(agents_router, "invoke_agent_text",
                        lambda *a, **k: pytest.fail("invoke was reached"))
    _reserved(client.post(f"/api/agents/{agent_id}/invoke",
                          json={"prompt": "hi", "session_id": RESERVED}))


def test_v1_refuses_a_reserved_session_id(client, monkeypatch, switch):
    agent_id = make_active_agent(name="lpp-v1")
    monkeypatch.setattr(public_api, "invoke_agent_text",
                        lambda *a, **k: pytest.fail("invoke was reached"))
    monkeypatch.setattr(public_api, "chat_stream",
                        lambda *a, **k: pytest.fail("the stream was opened"))
    headers = {"X-Api-Key": _mint_key(client)}
    for path in ("invoke", "invoke-stream"):
        _reserved(client.post(f"/v1/agents/{agent_id}/{path}",
                              json={"prompt": "hi", "session_id": RESERVED}, headers=headers))


@pytest.mark.parametrize("protocol", ["http", "a2a"])
def test_the_invoke_chain_refuses_it_too(monkeypatch, switch, protocol):
    """Every caller of the one invoke chain (the A2A branch included), not only the routes."""
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        agent = db.get(Agent, make_active_agent(name=f"lpp-chain-{protocol}"))
        agent.spec = {**(agent.spec or {}), "protocol": protocol}
        db.commit()
        db.refresh(agent)
    finally:
        db.close()
    monkeypatch.setattr(invoke_service, "data_client",
                        lambda _ws=None: pytest.fail("AWS was reached"))
    monkeypatch.setattr(rt, "stream_runtime_events",
                        lambda *a, **k: pytest.fail("AWS was reached"))
    for call in (invoke_service.invoke_agent_text,
                 lambda *a, **k: list(invoke_service.invoke_agent_events(*a, **k))):
        with pytest.raises(AppError) as err:
            call(agent, "hi", session_id=RESERVED)
        assert err.value.code == "chat.session_id_reserved" and err.value.status_code == 422


def test_ordinary_and_generated_ids_pass():
    invoke_service.refuse_reserved_session_id(rt.new_session_id())
    invoke_service.refuse_reserved_session_id("lp-" + "x" * 40)
    invoke_service.refuse_reserved_session_id(None)
    for _ in range(50):
        assert not rt.new_session_id().lower().startswith("lpp-")  # hex never does
