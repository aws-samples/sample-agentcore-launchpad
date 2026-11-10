# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_mint_entrances
"""Stream A2b (hub client) — the hub's own Gateway calls carry the principal token: a console
user's tools/list and tools/call (``ch: console``), the hub's reads ``System::"hub"`` (``ch:
system``); ``agt`` is ``hub`` (B's interceptor takes ws / agt from the token for the hub role).
Switch off: the request and the console's shared cache are exactly as before."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID
from app.policy.contracts import codec
from app.routers import tools as tools_router
from app.services import mcp_client
from app.services.workspace import WorkspaceContext
from tests.conftest import ws_ctx
from tests.policy_principal.test_mint_entrances import _switches, kms  # noqa: F401
from tests.policy_principal.test_principal_token import KEY_ID


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(WorkspaceContext, "credentials",
                        lambda self: SimpleNamespace(access_key="AK", secret_key="SK", token=None))

    def fake_post(url, *, headers=None, content=None, timeout=None):
        calls.append(json.loads(content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"tools": [{"name": "hr___get"}]}},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)
    return calls


def _ws():
    ws = ws_ctx({"gateway_url": "https://gw/mcp"}, id=DEFAULT_WORKSPACE_ID)
    return ws


def _claims(token: str, signer) -> dict:
    return codec.verify_token(token, {KEY_ID: signer.public_key}, now=time.time(),
                              workspace_id=DEFAULT_WORKSPACE_ID, env="", agent_id="hub").claims


def test_switch_off_sends_no_meta(sent, kms, monkeypatch):
    _switches(global_on=False, workspace_on=True, monkeypatch=monkeypatch)
    mcp_client.tools_list(_ws())
    assert "_meta" not in sent[0]["params"]
    assert not [n for n, _ in kms.calls if n == "sign"]


def test_the_hubs_own_read_is_system_hub(sent, kms, signer, monkeypatch):
    _switches(global_on=True, workspace_on=True, monkeypatch=monkeypatch)
    mcp_client.tools_list(_ws())
    claims = _claims(sent[0]["params"]["_meta"]["launchpad/principal"], signer)
    assert claims["p"] == {"kind": "system", "id": "hub", "label": "system:hub"}
    assert claims["ch"] == "system" and claims["agt"] == "hub"


def test_the_console_routes_carry_the_signed_in_user(client, sent, kms, signer, monkeypatch):
    _switches(global_on=True, workspace_on=True, monkeypatch=monkeypatch)
    monkeypatch.setattr(tools_router.mcp_client, "_gateway_url", lambda _ws: "https://gw/mcp")
    tools_router._cache.clear()
    assert client.get("/api/tools?refresh=true").status_code == 200
    client.post("/api/tools/call", json={"name": "hr___get", "arguments": {"id": 1}})
    listed, called = sent[0], sent[1]
    for message in (listed, called):
        claims = _claims(message["params"]["_meta"]["launchpad/principal"], signer)
        assert claims["ch"] == "console" and claims["p"]["kind"] == "user"
        assert claims["p"]["id"] == get_settings().auth_username  # the (auth-off) operator
    assert called["params"]["arguments"] == {"id": 1}


def test_the_console_cache_is_per_user_once_the_switch_may_be_on(monkeypatch):
    principal = SimpleNamespace(kind="user", id="hr-intern")
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", False)
    assert tools_router._cache_key("ws1", principal) == "ws1"
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    assert tools_router._cache_key("ws1", principal) == "ws1|user:hr-intern"
