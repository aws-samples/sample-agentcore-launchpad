"""Stream A2 (part 1) — the principal token is minted once per turn on every entrance of the
one invoke chain and carried to the runtime (contract I1 / I2; design D2 / D6, §14.2 A2).

Chat (``invoke_agent_events``), the console invoke and ``/v1`` (``invoke_agent_text``) and the
A2A branch. With either switch off nothing is minted and the request is byte-identical."""

from __future__ import annotations

import json
import logging

import pytest

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.policy.contracts import codec
from app.services import invoke as invoke_service
from app.services import principal_mint, principal_token
from app.services.agentcore import runtime as rt
from app.services.policy_entities import Principal
from tests.policy_principal.test_principal_token import KEY_ID, StubKms

ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/agent-abc"


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _DataPlane:
    def __init__(self, body: bytes = b'{"result": "ok"}') -> None:
        self.calls: list[dict] = []
        self.body = body

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": _Body(self.body), "contentType": "application/json"}


@pytest.fixture
def data(monkeypatch) -> _DataPlane:
    plane = _DataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    return plane


@pytest.fixture
def kms(signer, monkeypatch) -> StubKms:
    stub = StubKms(signer)
    principal_mint.reset_signer()
    monkeypatch.setattr(principal_token, "hub_signer",
                        lambda: principal_token.KmsSigner(stub, "alias/launchpad-principal"))
    yield stub
    principal_mint.reset_signer()


def _switches(*, global_on: bool, workspace_on: bool = True, monkeypatch) -> None:
    """Global model: only the platform switch is left; ``workspace_on`` is accepted for the
    older call sites — False means "switched off" (it was the second half of the switch)."""
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", global_on and workspace_on)


@pytest.fixture
def on(monkeypatch, kms):
    _switches(global_on=True, workspace_on=True, monkeypatch=monkeypatch)
    return kms


def _agent(method: str = "zip_runtime", protocol: str = "http", *, system_key: str | None = None,
           tools: list | None = None, name: str = "governed", arn: str = ARN,
           status: str = "active", workspace_id: str = DEFAULT_WORKSPACE_ID) -> Agent:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=workspace_id, name=name, method=method,
                      status=status, arn=arn, resource_id="agent-abc", system_key=system_key,
                      spec={"protocol": protocol, "system_prompt": "hi", "tools": tools or []})
        db.add(agent)
        db.commit()
        db.refresh(agent)
        return agent
    finally:
        db.close()


USER = type("I", (), {"username": "hr-intern", "role": "member", "user_id": "u_hrintern01"})()
KEY = type("K", (), {"id": "key_demo0001", "name": "demo-key", "created_by_name": None})()


def _payload(call: dict) -> dict:
    return json.loads(call["payload"])


def _verify(token: str, signer, agent_id: str) -> dict:
    return codec.verify_token(token, {KEY_ID: signer.public_key}, now=__import__("time").time(),
                              workspace_id=DEFAULT_WORKSPACE_ID, env="", agent_id=agent_id).claims


def _signs(kms: StubKms) -> int:
    return sum(1 for name, _ in kms.calls if name == "sign")


def test_switch_off_mints_nothing_and_the_request_is_byte_identical(data, kms, monkeypatch):
    agent = _agent()
    _switches(global_on=False, workspace_on=True, monkeypatch=monkeypatch)
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    _switches(global_on=True, workspace_on=False, monkeypatch=monkeypatch)
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    assert _signs(kms) == 0
    assert data.calls[0]["payload"] == data.calls[1]["payload"]
    assert "launchpad" not in _payload(data.calls[0])


@pytest.mark.parametrize("principal, channel, expected_ch, kind", [
    (Principal.from_identity(USER), None, "chat", "user"),
    (Principal.from_identity(USER), "console", "console", "user"),
    (Principal.from_api_key(KEY), None, "v1", "api_key"),
    (None, None, "system", "system"),
])
def test_the_sync_entrances_mint_once_and_carry_it_in_the_payload(
        data, on, signer, principal, channel, expected_ch, kind):
    agent = _agent()
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40, principal=principal,
                                     token_channel=channel)
    assert _signs(on) == 1
    call = data.calls[0]
    token = _payload(call)["launchpad"]["principal"]
    claims = _verify(token, signer, agent.id)
    assert claims["ch"] == expected_ch and claims["p"]["kind"] == kind
    assert claims["sid"] == call["runtimeSessionId"] and claims["agt"] == agent.id
    assert claims["exp"] - claims["iat"] == 900  # zip_runtime is a platform template (D4)


def test_chat_streaming_mints_once_with_the_resolved_session(on, signer, monkeypatch):
    seen: list[dict] = []

    def fake_stream(_client, _arn, _prompt, **kwargs):
        seen.append(kwargs)
        yield {"event": "complete", "data": {}}

    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: object())
    monkeypatch.setattr(invoke_service.rt, "stream_runtime_events", fake_stream)
    agent = _agent()
    list(invoke_service.invoke_agent_events(agent, "hi", principal=Principal.from_identity(USER)))
    assert _signs(on) == 1
    claims = _verify(seen[0]["launchpad_meta"]["principal"], signer, agent.id)
    assert claims["sid"] == seen[0]["session_id"] and claims["ch"] == "chat"
    assert claims["p"]["id"] == "hr-intern"  # D6: the sender


def test_the_canary_pointer_and_the_token_ride_together(data, on, signer, monkeypatch):
    monkeypatch.setattr(invoke_service, "_routing_kwargs", lambda _a, _s: {
        "launchpad_meta": {"bundle": "bndl_x:2", "variant": "T1"}})
    agent = _agent()
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    meta = _payload(data.calls[0])["launchpad"]
    assert meta["bundle"] == "bndl_x:2" and meta["variant"] == "T1"
    _verify(meta["principal"], signer, agent.id)


def test_token_carries_declared_gateway_epochs_but_not_unrelated_engines(data, on, signer):
    from app.models.policy import PolicyEngine, PolicyEngineAttachment

    agent = _agent(tools=[{"type": "gateway", "name": "second",
                          "config": {"gateway_id": "gateway-second"}}])
    with SessionLocal() as db:
        for i in range(40):
            eid = f"pe_{i:012x}"
            db.add(PolicyEngine(id=eid, workspace_id=DEFAULT_WORKSPACE_ID, name=f"engine{i}",
                                temporal_set_version=i))
            db.add(PolicyEngineAttachment(
                gateway_id="gateway-second" if i == 7 else f"gateway-unrelated-{i}",
                engine_id=eid, mode="ENFORCE"))
        db.commit()
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    claims = _verify(_payload(data.calls[-1])["launchpad"]["principal"], signer, agent.id)
    assert claims["engine_versions"] == {"pe_000000000007": 7}


def test_an_exempt_system_preset_mints_nothing(data, on):
    agent = _agent(system_key="architect", name="launchpad-architect")
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    assert _signs(on) == 0 and "launchpad" not in _payload(data.calls[0])


def test_a_kms_failure_never_fails_the_turn_and_never_logs_a_token(data, on, caplog):
    def boom(**_):
        raise RuntimeError("ThrottlingException")
    on.sign = boom
    caplog.set_level(logging.DEBUG)
    agent = _agent()
    result = invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                              principal=Principal.from_identity(USER))
    assert result["text"] == "ok"
    assert "launchpad" not in _payload(data.calls[0])
    assert "policy.principal_unavailable" in caplog.text


def test_byoc_tokens_live_five_minutes(data, on, signer):
    agent = _agent(method="byoc")
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    claims = _verify(_payload(data.calls[0])["launchpad"]["principal"], signer, agent.id)
    assert claims["exp"] - claims["iat"] == 300  # D4


PEER_ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/peer-hr"


def _a2a_ref(name: str = "hr-approvals", arn: str = PEER_ARN, **config) -> dict:
    return {"type": "a2a", "name": name,
            "config": {"record_id": "rec-1", "runtime_arn": arn, **config}}


def _aud(data, signer, agent) -> list[str]:
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    return _verify(_payload(data.calls[-1])["launchpad"]["principal"], signer, agent.id)["aud"]


def test_declared_a2a_peers_are_in_the_audience_by_runtime_arn(data, on, signer):
    peer = _agent(name="hr-approvals", arn=PEER_ARN)
    agent = _agent(tools=[_a2a_ref()])
    assert _aud(data, signer, agent) == [agent.id, peer.id]


def test_a_renamed_peer_stays_in_the_audience(data, on, signer):
    peer = _agent(name="approvals-v2", arn=PEER_ARN)  # the ref still says hr-approvals
    agent = _agent(tools=[_a2a_ref()])
    assert _aud(data, signer, agent) == [agent.id, peer.id]


def test_a_peer_named_by_agent_id_is_in_the_audience(data, on, signer):
    other = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/other-1"
    peer = _agent(name="approvals", arn=other)
    agent = _agent(tools=[_a2a_ref(agent_id=peer.id)])
    assert _aud(data, signer, agent) == [agent.id, peer.id]


def test_a_deleted_then_recreated_name_is_not_the_peer(data, on, signer):
    _agent(name="hr-approvals", arn=PEER_ARN, status="deleted")
    _agent(name="hr-approvals",  # same name, another runtime
           arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/impostor")
    agent = _agent(tools=[_a2a_ref()])
    assert _aud(data, signer, agent) == [agent.id]


@pytest.mark.parametrize("status", ["draft", "failed", "deploying", "deleted"])
def test_a_peer_that_is_not_active_is_not_in_the_audience(data, on, signer, status):
    _agent(name="hr-approvals", arn=PEER_ARN, status=status)
    agent = _agent(tools=[_a2a_ref()])
    assert _aud(data, signer, agent) == [agent.id]


def test_a_peer_in_another_workspace_is_not_in_the_audience(data, on, signer):
    from app.models.ledger import Workspace

    db = SessionLocal()
    try:
        if db.get(Workspace, "ws-other") is None:
            db.add(Workspace(id="ws-other", name="other", account_id="444455556666",
                             region="cn-northwest-1"))
            db.commit()
    finally:
        db.close()
    _agent(name="hr-approvals", arn=PEER_ARN, workspace_id="ws-other")
    agent = _agent(tools=[_a2a_ref()])
    assert _aud(data, signer, agent) == [agent.id]


def test_at_most_31_a2a_peers_fit_the_audience():
    from app.schemas.agent import A2A_MAX_PEERS, AgentSpec

    def spec(n: int) -> dict:
        return {"name": "many-peers", "method": "zip_runtime", "system_prompt": "hi",
                "tools": [_a2a_ref(name=f"peer-{i}", arn=f"{PEER_ARN}{i}") for i in range(n)]}

    assert A2A_MAX_PEERS == 31
    AgentSpec.model_validate(spec(31))
    with pytest.raises(ValueError, match="at most 31"):
        AgentSpec.model_validate(spec(32))


def test_the_a2a_branch_carries_the_token_in_the_message_metadata(on, signer, monkeypatch):
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "ok"}]}}
    plane = _DataPlane(json.dumps(reply).encode())
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    agent = _agent(protocol="a2a")
    invoke_service.invoke_agent_text(agent, "hi", principal=Principal.from_identity(USER))
    message = _payload(plane.calls[0])["params"]["message"]
    claims = _verify(message["metadata"][rt.PRINCIPAL_METADATA_KEY], signer, agent.id)
    assert claims["sid"] == message["contextId"] == plane.calls[0]["runtimeSessionId"]


def test_the_a2a_branch_without_a_token_is_unchanged(monkeypatch, kms):
    _switches(global_on=False, workspace_on=False, monkeypatch=monkeypatch)
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "ok"}]}}
    plane = _DataPlane(json.dumps(reply).encode())
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    agent = _agent(protocol="a2a")
    invoke_service.invoke_agent_text(agent, "hi", principal=Principal.from_identity(USER))
    message = _payload(plane.calls[0])["params"]["message"]
    assert "metadata" not in message or rt.PRINCIPAL_METADATA_KEY not in message["metadata"]


def test_the_runtime_arn_wins_when_it_and_agent_id_disagree(data, on, signer):
    by_arn = _agent(name="approvals", arn=PEER_ARN)
    other = _agent(name="other", arn=PEER_ARN.replace("peer-hr", "peer-other"))
    agent = _agent(tools=[_a2a_ref(agent_id=other.id)])
    assert _aud(data, signer, agent) == [agent.id, by_arn.id]


def test_the_audience_is_capped_at_mint_time_with_one_warn(data, on, signer, caplog):
    """Re-review nit a: an agent row with more peers than the token holds (an older spec, a
    bypassed validator) still gets its token — the first 31 peers, one WARN per agent."""
    peers = [_agent(name=f"peer-{i}", arn=f"{PEER_ARN}{i}") for i in range(33)]
    refs = [_a2a_ref(name=f"peer-{i}", arn=f"{PEER_ARN}{i}") for i in range(33)]
    refs.append(_a2a_ref(name="dup", arn=f"{PEER_ARN}0"))  # a duplicate is deduped
    agent = _agent(tools=refs)
    caplog.set_level(logging.WARNING, logger="launchpad.policy")
    aud = _aud(data, signer, agent)
    assert aud == [agent.id] + [p.id for p in peers[:31]]
    _aud(data, signer, agent)
    assert caplog.text.count("declares 33 a2a peers") == 1
