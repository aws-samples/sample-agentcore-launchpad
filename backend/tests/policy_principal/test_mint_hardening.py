# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_mint_entrances
"""Stream A1/A2 review fixes (increment 1): the KMS signer is bounded on the turn's critical
path, a failed key resolution is remembered for 60 s, and minting never raises."""

from __future__ import annotations

import logging
import socket
import threading
import time

import botocore.session
import pytest

from app.services import aws_clients, principal_mint, principal_token
from app.services import invoke as invoke_service
from app.services.policy_entities import Principal
from tests.policy_principal.test_mint_entrances import (  # noqa: F401
    USER,
    _agent,
    _payload,
    _switches,
    data,
    kms,
    on,
)
from tests.policy_principal.test_principal_token import KEY_ID, StubKms


def test_the_signer_kms_client_has_bounded_timeouts_and_retries():
    cfg = aws_clients.PRINCIPAL_KMS_CONFIG
    assert (cfg.connect_timeout, cfg.read_timeout) == (1, 2)
    assert cfg.retries == {"total_max_attempts": 2, "mode": "standard"}


def test_hub_signer_builds_its_client_with_the_bounded_config(monkeypatch):
    from app.core.config import get_settings

    seen: list[dict] = []

    def fake_client(service, ctx, cache_token=None, **cfg):
        seen.append({"service": service, "cache_token": cache_token, **cfg})
        return object()

    monkeypatch.setattr(get_settings(), "policy_principal_kms_key_id", "alias/lp-principal")
    monkeypatch.setattr(aws_clients, "client", fake_client)
    principal_token.hub_signer()
    assert seen == [{"service": "kms", "cache_token": "principal-kms",
                     "config": aws_clients.PRINCIPAL_KMS_CONFIG}]


@pytest.fixture
def silent_endpoint():
    """A TCP endpoint that accepts and never answers — a hung KMS."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                held.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}", held
    stop.set()
    thread.join(1)
    for conn in held:
        conn.close()
    server.close()


def test_a_hung_kms_costs_the_turn_at_most_the_client_budget(data, on, monkeypatch,
                                                             silent_endpoint):
    """Real botocore with the signer's config against a KMS that never answers: the turn
    goes on without a token inside 2 attempts x 2 s read + one backoff, not botocore's 60 s
    x 5 — and the hung endpoint saw exactly two connections."""
    session = botocore.session.get_session()
    endpoint, held = silent_endpoint
    hung = session.create_client(
        "kms", region_name="cn-northwest-1", endpoint_url=endpoint,
        aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret",
        config=aws_clients.PRINCIPAL_KMS_CONFIG)
    monkeypatch.setattr(principal_token, "hub_signer",
                        lambda: principal_token.KmsSigner(hung, "alias/launchpad-principal"))
    principal_mint.reset_signer()
    agent = _agent()
    started = time.monotonic()
    result = invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                              principal=Principal.from_identity(USER))
    elapsed = time.monotonic() - started
    assert result["text"] == "ok"
    assert "launchpad" not in _payload(data.calls[0])
    assert elapsed < 8.0, elapsed  # ~4.8 s measured; slack for a loaded gate host
    assert len(held) == 2


def test_a_failed_key_resolution_is_remembered_for_a_minute(data, on, monkeypatch, signer):
    calls: list[str] = []

    def unreadable(**_):
        calls.append("describe_key")
        raise RuntimeError("AccessDeniedException")

    stub = StubKms(signer)
    stub.describe_key = unreadable
    monkeypatch.setattr(principal_token, "hub_signer",
                        lambda: principal_token.KmsSigner(stub, "alias/launchpad-principal"))
    principal_mint.reset_signer()
    clock = [1000.0]
    monkeypatch.setattr(principal_mint.time, "monotonic", lambda: clock[0])
    agent = _agent()
    for _ in range(3):  # the turns inside the window make no KMS call
        invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                         principal=Principal.from_identity(USER))
    assert calls == ["describe_key"]
    clock[0] += principal_mint.KEY_FAILURE_TTL_S + 1  # the window ends: one more try
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    assert calls == ["describe_key", "describe_key"]
    assert all("launchpad" not in _payload(c) for c in data.calls)


def test_a_missing_key_is_remembered_too(data, monkeypatch, kms):
    from app.core.config import get_settings

    _switches(global_on=True, workspace_on=True, monkeypatch=monkeypatch)
    built: list[int] = []

    def no_key():
        built.append(1)
        raise principal_token.PrincipalTokenUnavailable(
            "policy.principal_unavailable", "no principal-token KMS key is configured")

    monkeypatch.setattr(get_settings(), "policy_principal_kms_key_id", "")
    monkeypatch.setattr(principal_token, "hub_signer", no_key)
    principal_mint.reset_signer()
    agent = _agent()
    for _ in range(2):
        assert principal_mint.mint_for_turn(agent, None, session_id="s" * 40,
                                            channel="chat") is None
    assert built == [1]


@pytest.mark.parametrize("where", ["claims", "peers", "tsv"])
def test_any_exception_while_minting_is_a_token_less_turn_with_one_warn(
        data, on, monkeypatch, caplog, where):
    def boom(*_a, **_k):
        raise KeyError("unexpected")

    target = {"claims": (principal_token, "build_claims"),
              "peers": (principal_mint, "_peer_ids"),
              "tsv": (principal_mint, "temporal_set_version")}[where]
    monkeypatch.setattr(*target, boom)
    caplog.set_level(logging.WARNING, logger="launchpad.policy")
    agent = _agent()
    result = invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                              principal=Principal.from_identity(USER))
    assert result["text"] == "ok"
    assert "launchpad" not in _payload(data.calls[0])
    warns = [r for r in caplog.records if r.name == "launchpad.policy"
             and r.levelno == logging.WARNING]
    assert len(warns) == 1 and "KeyError" in warns[0].getMessage()
    assert "unexpected" not in warns[0].getMessage()  # the code only, never the detail


def test_a_hub_call_mint_never_raises_either(on, monkeypatch, caplog):
    monkeypatch.setattr(principal_token, "build_claims",
                        lambda **_: (_ for _ in ()).throw(ValueError("x")))
    caplog.set_level(logging.WARNING, logger="launchpad.policy")
    assert principal_mint.mint_for_hub_call("default", None) is None
    assert "ValueError" in caplog.text


def test_a_sign_failure_trips_the_circuit_too(data, on, monkeypatch):
    """Re-review defect 1: the key resolves, KMS Sign fails — the next turn in the window
    makes no KMS call at all (the circuit is checked before mint)."""
    calls: list[str] = []
    real_describe = on.describe_key

    def describe(**kw):
        calls.append("describe_key")
        return real_describe(**kw)

    def sign(**_):
        calls.append("sign")
        raise RuntimeError("KMSInternalException")

    on.describe_key, on.sign = describe, sign
    principal_mint.reset_signer()
    clock = [5000.0]
    monkeypatch.setattr(principal_mint.time, "monotonic", lambda: clock[0])
    agent = _agent()
    for _ in range(3):
        invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                         principal=Principal.from_identity(USER))
    assert calls == ["describe_key", "sign"]
    clock[0] += principal_mint.KEY_FAILURE_TTL_S + 1
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    assert calls == ["describe_key", "sign", "sign"]  # the signer stays cached; one retry
    assert all("launchpad" not in _payload(c) for c in data.calls)


def test_sign_failure_circuits_are_per_workspace_and_per_path(data, on, monkeypatch):
    """N17-obs493 (re-review F4): a Sign failure in one workspace's turns opens that
    workspace's TURN circuit only — another workspace's turn, and the eval path of the same
    workspace, still reach KMS."""
    calls: list[str] = []
    real_sign = on.sign

    def sign(**kw):
        calls.append("sign")
        if len(calls) == 1:
            raise RuntimeError("KMSInternalException")
        return real_sign(**kw)

    on.sign = sign
    principal_mint.reset_signer()
    agent = _agent()
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    assert "launchpad" not in _payload(data.calls[0])  # token-less: the failed Sign
    assert principal_mint.circuit_open(principal_mint.PATH_TURN, agent.workspace_id)
    assert not principal_mint.circuit_open(principal_mint.PATH_TURN, "ws-other")
    assert not principal_mint.circuit_open(principal_mint.PATH_EVAL, agent.workspace_id)
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    assert calls == ["sign"]  # the window: no KMS call for this workspace's turns
    # the eval path of the same workspace mints (its own circuit is closed)
    from app.policy.contracts import codec as _codec

    claims = principal_token.build_claims(
        workspace_id=agent.workspace_id, agent_id=agent.id, session_id="e" * 40, tsv=0,
        channel="eval", principal=principal_token.TokenPrincipal.from_policy_principal(
            Principal.from_identity(USER)), method=agent.method)
    token = principal_mint._sign(claims, path=principal_mint.PATH_EVAL)
    assert token and calls == ["sign", "sign"]
    assert _codec.verify_token(token, {KEY_ID: on.signer.public_key}, now=time.time(),
                               workspace_id=agent.workspace_id, env="",
                               agent_id=agent.id).claims["ch"] == "eval"


def test_an_eval_trip_leaves_a_concurrent_turn_its_token(data, on):
    """The F4 scenario itself: a member's failing eval replay opens the EVAL circuit; the chat
    turn running next to it still carries its token, so the gateway's allow / deny outcome is
    the policy's — never `principal_missing` because of someone else's run."""
    principal_mint._trip("policy.principal_unavailable", path=principal_mint.PATH_EVAL,
                         workspace_id=_agent().workspace_id)
    agent = _agent(name="governed-turn")
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    assert "principal" in _payload(data.calls[-1])["launchpad"]
    assert principal_mint.circuit_open(principal_mint.PATH_EVAL, agent.workspace_id)
    assert not principal_mint.circuit_open(principal_mint.PATH_TURN, agent.workspace_id)


def test_a_claim_error_does_not_trip_the_circuit(data, on, monkeypatch):
    """Only the signer's failures open it: a bad claim set for one turn is that turn's."""
    from app.services import principal_token as pt

    real = pt.build_claims
    state = {"fail": True}

    def build(**kw):
        if state.pop("fail", False):
            raise pt.PrincipalTokenUnavailable("policy.principal_invalid", "x")
        return real(**kw)

    monkeypatch.setattr(pt, "build_claims", build)
    agent = _agent()
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    invoke_service.invoke_agent_text(agent, "hi", session_id="s" * 40,
                                     principal=Principal.from_identity(USER))
    assert "launchpad" not in _payload(data.calls[0])
    assert "principal" in _payload(data.calls[1])["launchpad"]
