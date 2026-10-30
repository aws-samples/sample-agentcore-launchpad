"""The hub side of the runtime stream's policy frames and the denial signature.

Global parity (2026-10-27, contract amendment 9) retired the runtime guard, its pinned bundle
(``policy_bundle.build`` / ``write_bundle``) and the deploy-time narrowing — what is left here:
the hub's handling of the ``policy_denied`` / ``policy_decision`` frames a runtime streams (a
re-emitted GATEWAY denial keeps the Gateway's decision id; any other frame is one
``policy_decisions`` row with ``source="runtime"`` and the id back on the frame), the Global
denial signature parser (``policy_denials``, restored) and the bug4 ⑤ continuation cap env.
Hermetic: fakes only.
"""

from __future__ import annotations

import unittest.mock as mock

import pytest

from app.core import config
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import environment as env_mod
from app.deployer.environment import runtime_environment
from app.models.policy import PolicyDecision, PolicyEngineAttachment
from app.schemas.agent import AgentSpec
from app.services import (
    aws_clients,
    policy_backend,
    policy_denials,
    policy_enforcement,
    policy_engines,
    policy_store,
)
from app.services import invoke as invoke_service
from app.services.agentcore import runtime as rt
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import Principal
from tests.conftest import ws_ctx
from tests.policy_fakes import FakeS3
from tests.test_policy_deploy_narrowing import (
    CREATE_PAYOUT,
    FORBID_PAYOUT,
    GATEWAY_ID,
    GET_EMPLOYEE,
    PERMIT_HR,
    RESOURCES,
    _agent,
    _gateway_spec,
    _get,
    _publish,
    _set_mode,
)

WS = DEFAULT_WORKSPACE_ID
BUCKET = RESOURCES["artifacts_bucket"]
DENIAL = ("Tool Execution Denied: Tool call not allowed due to policy enforcement "
          "[Policy evaluation denied due to pol_abc]")
REASON = "Policy evaluation denied due to pol_abc"


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = CedarEngine()
    policy_backend.set_backend(eng)
    return eng


@pytest.fixture
def s3(monkeypatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    return fake


@pytest.fixture
def workspace():
    return ws_ctx(RESOURCES, id=WS)


@pytest.fixture
def knob(monkeypatch):
    """Override one Settings knob through its env var (cache rebuilt)."""

    def _set(name: str, value: str) -> None:
        monkeypatch.setenv(f"LAUNCHPAD_{name.upper()}", value)
        config.get_settings.cache_clear()

    yield _set
    config.get_settings.cache_clear()


def _hr_agent(**over) -> str:
    return _agent(_gateway_spec(tools=[GET_EMPLOYEE, CREATE_PAYOUT], **over))


def _rows() -> list[PolicyDecision]:
    db = SessionLocal()
    try:
        return db.query(PolicyDecision).order_by(PolicyDecision.created_at).all()
    finally:
        db.close()


def _frame(kind="policy_denied", **over) -> dict:
    """What the guard yields into the runtime stream (both templates)."""
    data = {"event": kind, "tool": CREATE_PAYOUT, "reason": REASON, "policy_id": "pol_abc",
            "tool_use_id": "t-1", "mode": "enforce",
            "decision": "deny" if kind == "policy_denied" else "would_deny",
            "bundle_version": 4}
    data.update(over)
    return data


def _principal() -> Principal:
    return Principal(kind="user", id="river", label="river")


# ── continuation cap env (bug4 ⑤) ─────────────────────────────────────────


def test_continuation_cap_env_is_written_only_when_set(knob):
    plain = AgentSpec(name="plain", method="zip_runtime", system_prompt="p",
                      memory={"short_term": False, "long_term": False})
    assert env_mod.ENV_CONTINUATION_MAX_TOKENS == "LAUNCHPAD_CONTINUATION_MAX_TOKENS"
    assert env_mod.ENV_CONTINUATION_MAX_TOKENS not in runtime_environment(plain, {})
    knob("continuation_max_tokens", "512")
    assert runtime_environment(plain, {})[env_mod.ENV_CONTINUATION_MAX_TOKENS] == "512"
    pinned = plain.model_copy(update={"env": {env_mod.ENV_CONTINUATION_MAX_TOKENS: "64"}})
    assert runtime_environment(pinned, {})[env_mod.ENV_CONTINUATION_MAX_TOKENS] == "64"


def test_new_settings_defaults_and_bounds():
    assert config.Settings(continuation_max_tokens=0).continuation_max_tokens == 0
    with pytest.raises(ValueError):
        config.Settings(continuation_max_tokens=-1)
    with pytest.raises(ValueError):
        config.Settings(continuation_max_tokens=70000)
    # Global parity: the runtime guard and its fail mode are gone
    assert not hasattr(config.Settings(), "policy_runtime_guard")
    assert not hasattr(config.Settings(), "policy_runtime_guard_fail_mode")


# ── policy_denials (restored Global signature) ────────────────────────────


def test_denial_signature_is_recognised():
    assert policy_denials.parse_tool_denial(DENIAL) == {"reason": REASON, "policy_id": "pol_abc"}
    default = ("Tool execution failed: Tool Execution Denied: Tool call not allowed due to "
               "policy enforcement [No policy applies to the request (denied by default).]")
    assert policy_denials.parse_tool_denial(default) == {
        "reason": "No policy applies to the request (denied by default).", "policy_id": None,
    }
    assert policy_denials.determining_policy_id(
        "Policy evaluation denied due to pol_x:v3"
    ) == "pol_x"


@pytest.mark.parametrize("text", [
    '{"payout_id": "PAY-1"}', "Tool execution failed: Lambda timed out",
    "the policy says vacation requests need approval", "", None, 42,
])
def test_anything_else_is_not_a_denial(text):
    assert policy_denials.parse_tool_denial(text) is None


# ── hub: the guard's frames ────────────────────────────────────────────────


def test_runtime_payload_parser_forwards_the_two_guard_frames():
    [event] = list(rt._runtime_payload_events(_frame()))
    assert event == {"event": "policy_denied", "data": {
        "tool": CREATE_PAYOUT, "reason": REASON, "policy_id": "pol_abc", "tool_use_id": "t-1",
        "mode": "enforce", "decision": "deny", "bundle_version": 4, "decision_id": None,
        "source": "runtime", "stage": None,
    }}
    [observed] = list(rt._runtime_payload_events(
        _frame("policy_decision", policy_id=None, bundle_version="x")
    ))
    assert observed["event"] == "policy_decision"
    assert observed["data"]["decision"] == "would_deny"
    assert observed["data"]["policy_id"] is None and observed["data"]["bundle_version"] is None
    # A6: a re-emitted Gateway denial keeps its source, the Gateway's decision id and stage
    [gateway] = list(rt._runtime_payload_events(
        _frame(source="gateway", decision_id="gw-dec-1", stage="bt")))
    assert (gateway["data"]["source"], gateway["data"]["decision_id"],
            gateway["data"]["stage"]) == ("gateway", "gw-dec-1", "bt")
    [odd] = list(rt._runtime_payload_events(_frame(source="other", stage="z",
                                                   decision_id="x")))
    assert (odd["data"]["source"], odd["data"]["decision_id"],
            odd["data"]["stage"]) == ("runtime", None, None)


def test_text_invoke_collects_the_guard_frames(monkeypatch):
    def fake_stream(client, arn, prompt, **kwargs):
        yield {"event": "tool", "data": {"name": CREATE_PAYOUT, "id": "t-1"}}
        yield from rt._runtime_payload_events(_frame())
        yield {"event": "delta", "data": {"text": "I could not do that."}}
        yield {"event": "complete", "data": {"text": "I could not do that."}}

    monkeypatch.setattr(rt, "stream_runtime_events", fake_stream)
    result = rt.invoke_runtime_text(object(), "arn:aws-cn:x", "pay", session_id="s-1")
    assert result["text"] == "I could not do that."
    [decision] = result["policy_decisions"]
    assert decision["tool"] == CREATE_PAYOUT and decision["decision"] == "deny"


def test_record_runtime_decision_writes_the_runtime_row(engine, s3, workspace):
    agent_id = _hr_agent()
    row = _get(agent_id)
    decision_id = policy_enforcement.record_runtime_decision(
        row, _principal(), _frame(), session_id="s-9",
    )
    [stored] = _rows()
    assert stored.id == decision_id
    assert (stored.source, stored.channel, stored.action) == ("runtime", "chat", "call_tool")
    assert (stored.principal_kind, stored.principal_id) == ("user", "river")
    assert (stored.resource_kind, stored.resource_id) == ("tool", CREATE_PAYOUT)
    assert stored.agent_id == agent_id and stored.session_id == "s-9"
    assert stored.decision == "deny" and stored.mode == "ENFORCE"
    assert stored.policy_ids == ["pol_abc"] and stored.bundle_version == 4
    assert stored.request_id == "t-1" and stored.reason == REASON
    # a log_only observation, API-key channel, policy id recovered from the signature text
    key = Principal(kind="api_key", id="k1", label="ci")
    policy_enforcement.record_runtime_decision(
        row, key, _frame("policy_decision", policy_id=None, mode="log_only"), session_id="s-9",
    )
    observed = _rows()[-1]
    assert (observed.decision, observed.channel, observed.mode) == ("would_deny", "v1", "LOG_ONLY")
    assert observed.policy_ids == ["pol_abc"]
    # no principal → System::"hub"
    policy_enforcement.record_runtime_decision(row, None, _frame(), session_id=None)
    assert (_rows()[-1].principal_kind, _rows()[-1].channel) == ("system", "system")


def test_a_gateway_frame_writes_no_row_and_keeps_the_gateways_id(engine, s3, workspace):
    """`runtime_policy_denied_frame`: the gateway audit line is the system of record."""
    row = _get(_hr_agent())
    frame = _frame(source="gateway", decision_id="gw-dec-7")
    assert policy_enforcement.record_runtime_decision(row, _principal(), frame) == "gw-dec-7"
    assert policy_enforcement.record_runtime_decision(
        row, _principal(), _frame(source="gateway", decision_id=None)) is None
    assert _rows() == []


def test_describe_denial_names_the_policy_and_finds_the_gateway_stage(engine, s3, workspace):
    """A6 deny card: the frame names only the policy id — the hub adds its NAME and, for a
    Gateway denial whose audit line is already ingested, the engine stage of that row."""
    agent_id = _hr_agent()
    row = _get(agent_id)
    pid = _publish(workspace, "forbid_payout", FORBID_PAYOUT)
    db = SessionLocal()
    try:
        gateway = policy_store.record_decision(
            db, WS, source="gateway", channel="chat", principal_kind="user",
            principal_id="river", action="call_tool", resource_kind="tool",
            resource_id=CREATE_PAYOUT, decision="deny", mode="enforce", agent_id=agent_id,
            policy_ids=[pid], commit=False)
        gateway.stage = "bt"
        db.commit()
        gateway_id = gateway.id
    finally:
        db.close()
    facts = policy_enforcement.describe_denial(
        row, _frame(policy_id=f"{pid}:v1", source="gateway", decision_id=gateway_id))
    assert facts == {"policy_name": "forbid_payout", "stage": "bt"}
    # another workspace's gateway row is never read (review nit)
    db = SessionLocal()
    try:
        foreign = policy_store.record_decision(
            db, "ws-other", source="gateway", channel="chat", principal_kind="user",
            principal_id="river", action="call_tool", resource_kind="tool",
            resource_id=CREATE_PAYOUT, decision="deny", mode="enforce", commit=False)
        foreign.stage = "a"
        db.commit()
        foreign_id = foreign.id
    finally:
        db.close()
    assert policy_enforcement.describe_denial(
        row, _frame(policy_id=None, source="gateway", decision_id=foreign_id)) == {
        "policy_name": None, "stage": None}
    # the frame's own stage wins; an unknown policy / decision id → no facts, no error
    assert policy_enforcement.describe_denial(row, _frame(policy_id=pid, stage="a")) == {
        "policy_name": "forbid_payout", "stage": "a"}
    assert policy_enforcement.describe_denial(
        row, _frame(policy_id="pol_nope", decision_id="dec_nope")) == {
        "policy_name": None, "stage": None}
    assert policy_enforcement.describe_denial(row, _frame(policy_id=None)) == {
        "policy_name": None, "stage": None}


def test_record_runtime_decision_is_a_no_op_without_an_engine(s3):
    agent_id = _hr_agent()
    assert policy_enforcement.record_runtime_decision(_get(agent_id), None, _frame()) is None
    assert _rows() == []


def test_stream_invoke_audits_and_stamps_the_decision_id(engine, s3, workspace, monkeypatch):
    agent_id = _hr_agent()
    _publish(workspace, "permit_hr", PERMIT_HR)

    def fake_stream(client, arn, prompt, **kwargs):
        yield {"event": "tool", "data": {"name": CREATE_PAYOUT, "id": "t-1"}}
        yield from rt._runtime_payload_events(_frame())
        yield from rt._runtime_payload_events(_frame("policy_decision", tool_use_id="t-2"))
        yield {"event": "complete", "data": {"text": "done"}}

    monkeypatch.setattr(rt, "stream_runtime_events", fake_stream)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: object())
    events = list(invoke_service.invoke_agent_events(
        _get(agent_id), "pay", session_id="s-1", workspace=workspace, principal=_principal(),
    ))
    assert [e["event"] for e in events] == ["tool", "policy_denied", "policy_decision", "complete"]
    denied = events[1]["data"]
    runtime_rows = [r for r in _rows() if r.source == "runtime"]
    assert len(runtime_rows) == 2
    assert denied["decision_id"] == runtime_rows[0].id
    assert runtime_rows[0].decision == "deny" and runtime_rows[1].decision == "would_deny"
    assert runtime_rows[0].channel == "chat" and runtime_rows[0].session_id == "s-1"
    assert all(r.source == "runtime" for r in _rows())  # no hub row: invoking is not gated


def test_sync_invoke_audits_the_collected_frames(engine, s3, workspace, monkeypatch):
    agent_id = _hr_agent()
    _publish(workspace, "permit_hr", PERMIT_HR)

    def fake_text(client, arn, prompt, **kwargs):
        return {"text": "no", "session_id": kwargs.get("session_id") or "s-2",
                "policy_decisions": [_frame()]}

    monkeypatch.setattr(rt, "invoke_runtime_text", fake_text)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: object())
    result = invoke_service.invoke_agent_text(
        _get(agent_id), "pay", session_id="s-2", workspace=workspace, principal=_principal(),
    )
    [decision] = result["policy_decisions"]
    [denied] = result["policy_denied"]
    runtime_rows = [r for r in _rows() if r.source == "runtime"]
    assert decision["decision_id"] == denied["decision_id"] == runtime_rows[0].id
    assert runtime_rows[0].channel == "chat"


# ── N17-obs507: the Gateway denial arrives as the tool's result text ────────────────────────

GATEWAY_TEXT = ("Tool execution failed: Tool Execution Denied: Tool call not allowed due to policy "
                "enforcement [Policy evaluation denied due to {a}, {b}]")


def test_gateway_denial_text_is_parsed_conservatively():
    from app.services import policy_denials

    frame = policy_denials.gateway_denial_from_tool_result(
        CREATE_PAYOUT, "error", GATEWAY_TEXT.format(a="pol_a", b="pol_b:v2"), tool_use_id="t-1")
    assert frame is not None
    assert (frame["tool"], frame["policy_id"], frame["policy_ids"]) == (
        CREATE_PAYOUT, "pol_a", ["pol_a", "pol_b"])
    assert frame["source"] == "gateway" and frame["stage"] is None
    assert frame["decision_id"] is None and frame["decision"] == "deny"
    assert frame["reason"].startswith("Tool execution failed: Tool Execution Denied")
    # reason codes (no policy decided) still make a card; the hub's 256-char cut tolerated
    code = policy_denials.gateway_denial_from_tool_result(
        CREATE_PAYOUT, "error", "Tool execution failed: Tool Execution Denied: Tool call not "
        "allowed due to policy enforcement [unknown_caller]")
    assert code is not None and code["policy_ids"] == [] and code["policy_id"] is None
    cut = policy_denials.gateway_denial_from_tool_result(
        CREATE_PAYOUT, "error", GATEWAY_TEXT.format(a="pol_a", b="pol_b")[:-1])
    assert cut is not None and cut["policy_ids"] == ["pol_a", "pol_b"]
    # anything without the signature, or a successful call, is not a denial
    assert policy_denials.gateway_denial_from_tool_result(CREATE_PAYOUT, "error", "boom") is None
    assert policy_denials.gateway_denial_from_tool_result(
        CREATE_PAYOUT, "success", GATEWAY_TEXT.format(a="pol_a", b="pol_b")) is None
    assert policy_denials.gateway_denial_from_tool_result(CREATE_PAYOUT, "error", None) is None


def test_stream_derives_the_policy_denied_frame_from_the_gateway_tool_result(
        engine, s3, workspace, monkeypatch):
    """N17-obs507: under Global parity the only producer of `policy_denied` was the removed
    runtime guard — the Gateway interceptor's denial reaches the hub as the agent's tool result
    text. The stream yields the frame right after that `tool_result`, with every determining
    policy named from the store; no hub audit row (the gateway line is the system of record)."""
    agent_id = _hr_agent()
    pid_a = _publish(workspace, "permit_hr", PERMIT_HR)
    pid_b = _publish(workspace, "forbid_payout", FORBID_PAYOUT)
    text = GATEWAY_TEXT.format(a=pid_a, b=f"{pid_b}:v1")

    def fake_stream(client, arn, prompt, **kwargs):
        yield {"event": "tool", "data": {"name": CREATE_PAYOUT, "id": "t-1"}}
        yield from rt._runtime_payload_events(
            {"event": "tool_result", "id": "t-1", "name": CREATE_PAYOUT, "status": "error",
             "summary": text})
        yield {"event": "delta", "data": {"text": "I could not pay that."}}
        yield {"event": "complete", "data": {"text": "done"}}

    monkeypatch.setattr(rt, "stream_runtime_events", fake_stream)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: object())
    events = list(invoke_service.invoke_agent_events(
        _get(agent_id), "pay", session_id="s-7", workspace=workspace, principal=_principal(),
    ))
    assert [e["event"] for e in events] == ["tool", "tool_result", "policy_denied", "delta",
                                            "complete"]
    denied = events[2]["data"]
    assert denied["tool"] == CREATE_PAYOUT and denied["tool_use_id"] == "t-1"
    assert denied["reason"] == text and denied["source"] == "gateway"
    assert denied["policy_ids"] == [pid_a, pid_b] and denied["policy_id"] == pid_a
    assert denied["policy_names"] == {pid_a: "permit_hr", pid_b: "forbid_payout"}
    assert denied["policy_name"] == "permit_hr" and denied["stage"] is None
    assert denied["decision_id"] is None and denied["decision"] == "deny"
    assert _rows() == []  # the gateway audit line is the system of record
    # an unknown id names itself; a runtime-emitted frame for the same tool is not doubled
    events = list(invoke_service.invoke_agent_events(
        _get(agent_id), "pay", session_id="s-8", workspace=workspace, principal=_principal(),
    ))
    assert events[2]["data"]["policy_names"][pid_a] == "permit_hr"

    def fake_stream_with_guard_frame(client, arn, prompt, **kwargs):
        yield {"event": "tool", "data": {"name": CREATE_PAYOUT, "id": "t-1"}}
        yield from rt._runtime_payload_events(_frame(source="gateway", decision_id="gw-1"))
        yield from rt._runtime_payload_events(
            {"event": "tool_result", "id": "t-1", "name": CREATE_PAYOUT, "status": "error",
             "summary": GATEWAY_TEXT.format(a="pol_zz", b="pol_yy")})
        yield {"event": "complete", "data": {"text": "done"}}

    monkeypatch.setattr(rt, "stream_runtime_events", fake_stream_with_guard_frame)
    events = list(invoke_service.invoke_agent_events(
        _get(agent_id), "pay", session_id="s-9", workspace=workspace, principal=_principal(),
    ))
    assert [e["event"] for e in events] == ["tool", "policy_denied", "tool_result", "complete"]


def test_sync_invoke_derives_policy_denied_from_the_gateway_tool_results(
        engine, s3, workspace, monkeypatch):
    """The sync entrance (`/v1`, buffered methods): the runtime's `tool_results` are read the
    same way and popped from the result; `policy_denied` carries the derived frame."""
    agent_id = _hr_agent()
    pid = _publish(workspace, "forbid_payout", FORBID_PAYOUT)
    text = GATEWAY_TEXT.format(a=pid, b="pol_unknown01")

    def fake_text(client, arn, prompt, **kwargs):
        return {"text": "no", "session_id": kwargs.get("session_id") or "s-2",
                "tool_results": [{"id": "t-1", "name": CREATE_PAYOUT, "status": "error",
                                  "summary": text},
                                 {"id": "t-2", "name": "hr-database___get_employee",
                                  "status": "success", "summary": "{...}"}]}

    monkeypatch.setattr(rt, "invoke_runtime_text", fake_text)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: object())
    result = invoke_service.invoke_agent_text(
        _get(agent_id), "pay", session_id="s-2", workspace=workspace, principal=_principal(),
    )
    assert "tool_results" not in result and "policy_decisions" not in result
    [denied] = result["policy_denied"]
    assert denied["tool"] == CREATE_PAYOUT and denied["policy_ids"] == [pid, "pol_unknown01"]
    assert denied["policy_names"] == {pid: "forbid_payout", "pol_unknown01": "pol_unknown01"}
    assert denied["source"] == "gateway" and denied["decision_id"] is None
    assert _rows() == []


# ── N17-obs526: the deny card names the DECIDING gateway — the tool's own, not the default ──

ITSM_GW = "launchpad-itsm-lab-wu0medafqt"
SERVICENOW_REF = {"name": "servicenow", "type": "gateway",
                  "config": {"gateway_id": ITSM_GW, "tools": ["servicenow___get_ticket"]}}
GET_TICKET = "launchpad_gw_servicenow___get_ticket"  # the runtime's alias of the Gateway tool


def _attach_new_engine(gateway_id: str, name: str, mode: str = "ENFORCE") -> str:
    """A second engine attached to a gateway that is NOT the workspace's own."""
    db = SessionLocal()
    try:
        eng = policy_engines.create_engine(db, WS, name=name, operator="river")
        db.add(PolicyEngineAttachment(gateway_id=gateway_id, engine_id=eng.id, mode=mode))
        db.commit()
        return eng.id
    finally:
        db.close()


def _default_engine_id() -> str:
    db = SessionLocal()
    try:
        return policy_store.settings_row(db, WS).id
    finally:
        db.close()


def _result(name: str, text: str, tool_use_id: str = "t-1") -> dict:
    return {"name": name, "status": "error", "summary": text, "id": tool_use_id}


def test_derived_frame_names_the_tools_own_gateway_and_engine(engine, s3, workspace):
    """N17-obs526: live, a denial issued by the ITSM gateway was framed with the WORKSPACE
    gateway + its engine, so the card's 「打开策略」 opened the wrong engine. The deciding
    gateway is the agent's Gateway ToolRef for the tool's target; its attachment names the
    engine. The workspace gateway keeps naming the hr tools' denials."""
    _set_mode("log_only")  # the workspace gateway (hr-database) carries the default engine
    itsm_engine = _attach_new_engine(ITSM_GW, "itsm")
    agent = _get(_agent(_gateway_spec(tools=[GET_EMPLOYEE, CREATE_PAYOUT],
                                      extra_tools=[SERVICENOW_REF])))
    text = GATEWAY_TEXT.format(a="pol_bc1ef8a3689a", b="pol_other01")

    frame = policy_enforcement.gateway_denial_from_tool_result(agent, _result(GET_TICKET, text))
    assert frame is not None and frame["tool"] == GET_TICKET
    assert (frame["gateway_id"], frame["engine_id"]) == (ITSM_GW, itsm_engine)
    hr = policy_enforcement.gateway_denial_from_tool_result(agent, _result(CREATE_PAYOUT, text))
    assert (hr["gateway_id"], hr["engine_id"]) == (GATEWAY_ID, _default_engine_id())

    db = SessionLocal()
    try:
        resolve = lambda tool: policy_enforcement.deciding_gateway_id(db, agent, tool)  # noqa: E731
        # the Gateway's own spelling, the runtime alias, and a declared-tool match all resolve
        assert resolve("servicenow___get_ticket") == ITSM_GW
        assert resolve("launchpad_gw_" + CREATE_PAYOUT) == GATEWAY_ID
        assert resolve(GET_EMPLOYEE) == GATEWAY_ID
        # the audit row's mode follows the deciding gateway's attachment too
        assert policy_enforcement.gateway_mode(db, WS, gateway_id=ITSM_GW) == "enforce"
        assert policy_enforcement.gateway_mode(db, WS) == "log_only"
    finally:
        db.close()
    decision_id = policy_enforcement.record_runtime_decision(
        agent, _principal(), {"event": "policy_denied", "tool": GET_TICKET, "reason": REASON,
                              "decision": "deny", "tool_use_id": "t-1"}, session_id="s-1")
    [row] = _rows()
    assert row.id == decision_id and row.mode == "ENFORCE"


def test_derived_frame_falls_back_to_the_single_ref_then_the_workspace_gateway(
        engine, s3, workspace):
    """No ToolRef matches the tool's target: exactly one Gateway ToolRef ⇒ its gateway; several
    (or none) ⇒ the workspace gateway, as before obs526. An unattached gateway names no engine."""
    _set_mode("enforce")
    default_engine = _default_engine_id()
    text = GATEWAY_TEXT.format(a="pol_a", b="pol_b")
    # one ref (hr-database on the workspace gateway), a tool of an undeclared target
    one = _get(_hr_agent())
    frame = policy_enforcement.gateway_denial_from_tool_result(
        one, _result("launchpad_gw_other___thing", text))
    assert (frame["gateway_id"], frame["engine_id"]) == (GATEWAY_ID, default_engine)
    # the single ref names an UNATTACHED foreign gateway ⇒ that gateway, engine None
    foreign_only = AgentSpec.model_validate({
        "name": "sn-agent", "method": "zip_runtime", "system_prompt": "p",
        "memory": {"short_term": False, "long_term": False}, "tools": [SERVICENOW_REF],
    }).model_dump()
    sn = _get(_agent(foreign_only, agent_id="ag-sn", name="sn-agent"))
    frame = policy_enforcement.gateway_denial_from_tool_result(
        sn, _result("launchpad_gw_other___thing", text))
    assert (frame["gateway_id"], frame["engine_id"]) == (ITSM_GW, None)
    # two refs, neither matching ⇒ the workspace gateway
    two = _get(_agent(_gateway_spec(extra_tools=[SERVICENOW_REF]), agent_id="ag-two"))
    frame = policy_enforcement.gateway_denial_from_tool_result(
        two, _result("launchpad_gw_other___thing", text))
    assert (frame["gateway_id"], frame["engine_id"]) == (GATEWAY_ID, default_engine)
    # no Gateway ref at all ⇒ the workspace gateway
    bare = AgentSpec.model_validate({
        "name": "bare", "method": "zip_runtime", "system_prompt": "p",
        "memory": {"short_term": False, "long_term": False}, "tools": [],
    }).model_dump()
    none = _get(_agent(bare, agent_id="ag-bare", name="bare"))
    frame = policy_enforcement.gateway_denial_from_tool_result(none, _result(CREATE_PAYOUT, text))
    assert (frame["gateway_id"], frame["engine_id"]) == (GATEWAY_ID, default_engine)
    # a card fact never breaks the stream: a broken spec still yields the frame
    none.spec = {"tools": "not-a-list"}
    frame = policy_enforcement.gateway_denial_from_tool_result(none, _result(CREATE_PAYOUT, text))
    assert frame is not None and frame["tool"] == CREATE_PAYOUT


def test_runtime_text_collects_tool_results_for_the_invoke_layer(monkeypatch):
    def fake_events(*_a, **_k):
        yield {"event": "tool", "data": {"name": CREATE_PAYOUT, "id": "t-1"}}
        yield {"event": "tool_result", "data": {"id": "t-1", "name": CREATE_PAYOUT,
                                                "status": "error", "summary": "x"}}
        yield {"event": "delta", "data": {"text": "no"}}

    monkeypatch.setattr(rt, "stream_runtime_events", fake_events)
    result = rt.invoke_runtime_text(object(), "arn", "pay", session_id="s-1")
    assert result["tool_results"] == [{"id": "t-1", "name": CREATE_PAYOUT, "status": "error",
                                       "summary": "x"}]
    assert result["text"] == "no"


def test_chat_ledger_persists_the_policy_row_from_the_forwarded_frame():
    from app.services import chat_ledger

    saved: list[tuple] = []

    def fake_save(workspace_id, agent_id, session_id, role, text, **kw):
        saved.append((role, text, kw.get("name")))

    events = [
        {"event": "meta", "data": {"session_id": "s-3"}},
        {"event": "tool", "data": {"name": CREATE_PAYOUT, "id": "t-1"}},
        {"event": "policy_denied", "data": {**_frame(), "decision_id": "dec1"}},
        {"event": "delta", "data": {"text": "refused"}},
        {"event": "done", "data": {}},
    ]
    with mock.patch.object(chat_ledger, "save_message", fake_save), \
            mock.patch.object(chat_ledger, "track_session", lambda *a, **k: None):
        out = list(chat_ledger.record_stream(
            iter(events), workspace_id=WS, agent_id="ag1", actor_id="river", prompt="pay",
        ))
    assert [e["event"] for e in out] == [e["event"] for e in events]
    assert ("policy", REASON, CREATE_PAYOUT) in saved
