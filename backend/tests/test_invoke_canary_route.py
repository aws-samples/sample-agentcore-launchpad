"""invoke_agent_text canary routing (Phase 3): while a canary runs, a real
invocation is routed through the canary gateway; the non-canary path stays
byte-identical; a gateway failure degrades to the control-safe stable endpoint
(never DEFAULT)."""

import json

import app.services.invoke as invoke_mod
from app.models.ledger import Agent

ROUTE = {
    "gateway_url": "https://gw.example",
    "control_target": "canctrl",
    "stable_endpoint": "stablecan",
    "arn": "arn:agent",
}


def _agent() -> Agent:
    agent = Agent(
        name="subject",
        method="zip_runtime",
        status="active",
        arn="arn:agent",
        resource_id="subject-res",
        spec={"protocol": "http", "system_prompt": "p"},
    )
    agent.id = "a1"
    return agent


class _Resp:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def test_active_canary_routes_through_gateway(monkeypatch):
    monkeypatch.setattr(
        invoke_mod.canary_service, "active_canary_route", lambda agent_id: ROUTE
    )
    calls: dict = {}

    def fake_sigv4(url, body, *, session_id=None):
        calls.update(url=url, body=body, session_id=session_id)
        return _Resp(200, json.dumps({"result": "hello from candidate"}))

    monkeypatch.setattr(invoke_mod.gateway, "sigv4_post", fake_sigv4)
    monkeypatch.setattr(invoke_mod, "data_client", lambda: object())

    def _no_direct(*args, **kwargs):
        raise AssertionError("must route via gateway, not direct invoke")

    monkeypatch.setattr(invoke_mod.rt, "invoke_runtime_text", _no_direct)

    out = invoke_mod.invoke_agent_text(_agent(), "hi", session_id="x" * 40)

    assert calls["url"] == "https://gw.example/canctrl/invocations"
    assert calls["body"] == {"prompt": "hi", "sessionId": "x" * 40}
    assert calls["session_id"] == "x" * 40
    assert out == {"text": "hello from candidate", "session_id": "x" * 40}


def test_short_session_id_is_replaced_before_gateway_post(monkeypatch):
    monkeypatch.setattr(
        invoke_mod.canary_service, "active_canary_route", lambda agent_id: ROUTE
    )
    calls: dict = {}

    def fake_sigv4(url, body, *, session_id=None):
        calls.update(session_id=session_id, body=body)
        return _Resp(200, json.dumps({"result": "ok"}))

    monkeypatch.setattr(invoke_mod.gateway, "sigv4_post", fake_sigv4)
    monkeypatch.setattr(invoke_mod, "data_client", lambda: object())

    out = invoke_mod.invoke_agent_text(_agent(), "hi", session_id="short")

    # session ids must be >= 33 chars; a short one is regenerated
    assert len(calls["session_id"]) >= 33
    assert calls["body"]["sessionId"] == calls["session_id"]
    assert out["session_id"] == calls["session_id"]


def test_authenticated_canary_payload_keeps_user_policy_identity(monkeypatch):
    monkeypatch.setattr(
        invoke_mod.canary_service, "active_canary_route", lambda agent_id: ROUTE
    )
    calls: dict = {}

    def fake_sigv4(url, body, *, session_id=None):
        calls.update(body=body)
        return _Resp(200, json.dumps({"result": "ok"}))

    monkeypatch.setattr(invoke_mod.gateway, "sigv4_post", fake_sigv4)
    monkeypatch.setattr(invoke_mod, "data_client", lambda: object())
    invoke_mod.invoke_agent_text(
        _agent(),
        "hi",
        session_id="x" * 40,
        actor_id="agent__demo",
        runtime_user_id="demo",
        gateway_access_token="trusted-user-jwt",
    )
    assert calls["body"] == {
        "prompt": "hi",
        "sessionId": "x" * 40,
        "actor_id": "agent__demo",
        "gateway_access_token": "trusted-user-jwt",
    }


def test_no_canary_uses_direct_invoke_no_qualifier(monkeypatch):
    monkeypatch.setattr(
        invoke_mod.canary_service, "active_canary_route", lambda agent_id: None
    )
    captured: dict = {}

    def fake_invoke(
        client, arn, prompt, *, session_id=None, actor_id="default",
        qualifier=None, runtime_user_id=None,
    ):
        captured.update(
            arn=arn, prompt=prompt, session_id=session_id,
            actor_id=actor_id, qualifier=qualifier,
        )
        return {"text": "direct reply", "session_id": session_id or "s"}

    monkeypatch.setattr(invoke_mod.rt, "invoke_runtime_text", fake_invoke)
    monkeypatch.setattr(invoke_mod, "data_client", lambda: object())

    def _no_gateway(*args, **kwargs):
        raise AssertionError("non-canary path must not touch the gateway")

    monkeypatch.setattr(invoke_mod.gateway, "sigv4_post", _no_gateway)

    out = invoke_mod.invoke_agent_text(_agent(), "hi", session_id="sess", actor_id="bob")

    # byte-identical to the pre-canary path: direct ARN, DEFAULT (no qualifier)
    assert captured["arn"] == "arn:agent"
    assert captured["qualifier"] is None
    assert captured["actor_id"] == "bob"
    assert captured["session_id"] == "sess"
    assert out["text"] == "direct reply"


def test_gateway_error_falls_back_to_stable_endpoint(monkeypatch):
    monkeypatch.setattr(
        invoke_mod.canary_service, "active_canary_route", lambda agent_id: ROUTE
    )

    def boom_sigv4(url, body, *, session_id=None):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(invoke_mod.gateway, "sigv4_post", boom_sigv4)
    captured: dict = {}

    def fake_invoke(
        client, arn, prompt, *, session_id=None, actor_id="default",
        qualifier=None, runtime_user_id=None,
    ):
        captured.update(arn=arn, qualifier=qualifier, session_id=session_id)
        return {"text": "control reply", "session_id": session_id or "s"}

    monkeypatch.setattr(invoke_mod.rt, "invoke_runtime_text", fake_invoke)
    monkeypatch.setattr(invoke_mod, "data_client", lambda: object())

    out = invoke_mod.invoke_agent_text(_agent(), "hi", session_id=None)

    # fail-safe is control-safe: stable endpoint (pinned to v_current), NOT DEFAULT
    assert captured["arn"] == "arn:agent"
    assert captured["qualifier"] == "stablecan"
    assert out["text"] == "control reply"


def test_provisioning_route_invokes_stable_endpoint_directly(monkeypatch):
    # A canary still setting up exposes the "provisioning" route: stable endpoint
    # present, but NO live gateway (gateway_url / control_target). invoke must
    # direct-invoke the stable endpoint (v_current), never DEFAULT or the gateway.
    provisioning = {
        "runtime_id": "subject-res",
        "arn": "arn:agent",
        "stable_endpoint": "stablecan",
        "v_current": "3",
    }
    monkeypatch.setattr(
        invoke_mod.canary_service, "active_canary_route", lambda agent_id: provisioning
    )
    captured: dict = {}

    def fake_invoke(
        client, arn, prompt, *, session_id=None, actor_id="default",
        qualifier=None, runtime_user_id=None,
    ):
        captured.update(
            arn=arn, qualifier=qualifier, session_id=session_id, actor_id=actor_id
        )
        return {"text": "control reply", "session_id": session_id or "s"}

    monkeypatch.setattr(invoke_mod.rt, "invoke_runtime_text", fake_invoke)
    monkeypatch.setattr(invoke_mod, "data_client", lambda: object())

    def _no_gateway(*args, **kwargs):
        raise AssertionError("provisioning route must not touch the gateway")

    monkeypatch.setattr(invoke_mod.gateway, "sigv4_post", _no_gateway)

    out = invoke_mod.invoke_agent_text(_agent(), "hi", session_id="sess", actor_id="bob")

    assert captured["arn"] == "arn:agent"
    assert captured["qualifier"] == "stablecan"
    assert captured["session_id"] == "sess"
    assert captured["actor_id"] == "bob"
    assert out["text"] == "control reply"


def test_gateway_non_200_falls_back_to_stable_endpoint(monkeypatch):
    monkeypatch.setattr(
        invoke_mod.canary_service, "active_canary_route", lambda agent_id: ROUTE
    )
    monkeypatch.setattr(
        invoke_mod.gateway,
        "sigv4_post",
        lambda url, body, *, session_id=None: _Resp(503, "unavailable"),
    )
    captured: dict = {}

    def fake_invoke(
        client, arn, prompt, *, session_id=None, actor_id="default",
        qualifier=None, runtime_user_id=None,
    ):
        captured.update(qualifier=qualifier)
        return {"text": "control reply", "session_id": "s"}

    monkeypatch.setattr(invoke_mod.rt, "invoke_runtime_text", fake_invoke)
    monkeypatch.setattr(invoke_mod, "data_client", lambda: object())

    out = invoke_mod.invoke_agent_text(_agent(), "hi", session_id="y" * 40)

    assert captured["qualifier"] == "stablecan"
    assert out["text"] == "control reply"
