"""Live agent card read for A2A registry records — GetAgentCard through the
record → ledger agent → Agent.arn chain, with every refusal decided before AWS.

The data plane is a stub: the wrapper must send no runtimeSessionId, end the
session AWS opens to serve the card (fail-soft), and the card diff must key on
identity fields + skill ids only."""

import json

import pytest
from botocore.exceptions import ClientError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.services import registry_console as console
from app.services.agentcore import registry as reg
from app.services.agentcore import runtime as rt
from tests.conftest import set_default_resources

RECORD_ID = "r-a2a-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/aurora-1"
STORED_CARD = {
    "protocolVersion": "0.3.0",
    "name": "aurora-faq-a2a",
    "description": "Aurora FAQ",
    "url": "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/aurora-1/invocations",
    "preferredTransport": "JSONRPC",
    "version": "3",
    "capabilities": {"streaming": True},
    "skills": [{"id": "faq", "name": "FAQ"}, {"id": "refunds", "name": "Refunds"}],
    "metadata": {"launchpad.transport": "a2a-jsonrpc"},
}


def _record(card: dict | None = STORED_CARD) -> dict:
    descriptors = reg.build_a2a_descriptors(card) if card is not None else {}
    return {
        "recordId": RECORD_ID, "name": "aurora-faq-a2a", "descriptorType": "A2A",
        "status": "APPROVED", "recordVersion": "1.0.0-a2a", "descriptors": descriptors,
    }


def _mk_agent(**kw) -> str:
    db = SessionLocal()
    agent = Agent(**{
        "workspace_id": DEFAULT_WORKSPACE_ID, "name": "aurora-faq-a2a",
        "method": "zip_runtime", "status": "active", "arn": RUNTIME_ARN,
        "registry_record_id": RECORD_ID, "spec": {"protocol": "a2a"}, **kw,
    })
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


class DataPlane:
    """bedrock-agentcore data-plane stub: GetAgentCard + StopRuntimeSession."""

    def __init__(self, card: dict, *, session_id: str | None = None,
                 error_code: str | None = None, stop_error: str | None = None,
                 card_as_string: bool = False):
        self.card = card
        self.session_id = session_id
        self.error_code = error_code
        self.stop_error = stop_error
        self.card_as_string = card_as_string
        self.get_calls: list[dict] = []
        self.stop_calls: list[dict] = []

    def get_agent_card(self, **params):
        self.get_calls.append(params)
        if self.error_code:
            raise ClientError(
                {"Error": {"Code": self.error_code, "Message": "no such runtime"}},
                "GetAgentCard",
            )
        out = {"agentCard": json.dumps(self.card) if self.card_as_string else self.card,
               "statusCode": 200}
        if self.session_id:
            out["runtimeSessionId"] = self.session_id
        return out

    def stop_runtime_session(self, **params):
        self.stop_calls.append(params)
        if self.stop_error:
            raise ClientError(
                {"Error": {"Code": self.stop_error, "Message": "Session not found"}},
                "StopRuntimeSession",
            )
        return {"runtimeSessionId": params["runtimeSessionId"], "statusCode": 200}


@pytest.fixture
def plane(monkeypatch):
    """Wire a stub data plane + stub registry read; tests reconfigure `plane`."""
    set_default_resources({"registry_id": "reg-1"})
    stub = DataPlane(dict(STORED_CARD))
    monkeypatch.setattr(console, "data_client", lambda _ws: stub)
    monkeypatch.setattr(console, "console_get", lambda _ws, rid: _record())
    return stub


# ---- wrapper ----------------------------------------------------------------

def test_get_agent_card_wrapper_sends_no_session_and_parses_card():
    stub = DataPlane(dict(STORED_CARD), card_as_string=True)
    out = rt.get_agent_card(stub, runtime_arn=RUNTIME_ARN)
    assert stub.get_calls == [{"agentRuntimeArn": RUNTIME_ARN, "qualifier": "DEFAULT"}]
    assert "runtimeSessionId" not in stub.get_calls[0]
    assert out["card"] == STORED_CARD and out["status_code"] == 200
    assert out["session_id"] is None and stub.stop_calls == []


def test_get_agent_card_wrapper_ends_the_session_aws_opened():
    stub = DataPlane(dict(STORED_CARD), session_id="s" * 33)
    out = rt.get_agent_card(stub, runtime_arn=RUNTIME_ARN, qualifier="v2")
    assert out["session_id"] == "s" * 33
    assert stub.stop_calls == [
        {"agentRuntimeArn": RUNTIME_ARN, "runtimeSessionId": "s" * 33, "qualifier": "v2"}
    ]


def test_get_agent_card_wrapper_stop_failure_is_fail_soft(caplog):
    stub = DataPlane(dict(STORED_CARD), session_id="s" * 33,
                     stop_error="ResourceNotFoundException")
    out = rt.get_agent_card(stub, runtime_arn=RUNTIME_ARN)
    assert out["card"]["name"] == "aurora-faq-a2a"  # the card still comes back
    assert len(stub.stop_calls) == 1
    assert "not ended" in caplog.text


# ---- diff -------------------------------------------------------------------

def test_agent_card_diff_identical_when_cards_match():
    diff = reg.agent_card_diff(STORED_CARD, json.loads(json.dumps(STORED_CARD)))
    assert diff == {"identical": True, "fields": [],
                    "skills_only_in_live": [], "skills_only_in_record": []}


def test_agent_card_diff_lists_fields_and_skill_ids_on_both_sides():
    live = {**STORED_CARD, "version": "4", "description": "edited text is not drift",
            "skills": [{"id": "faq"}, {"id": "escalate"}]}
    diff = reg.agent_card_diff(STORED_CARD, live)
    assert diff["identical"] is False
    assert diff["fields"] == [{"field": "version", "record": "3", "live": "4"}]
    assert diff["skills_only_in_live"] == ["escalate"]
    assert diff["skills_only_in_record"] == ["refunds"]


def test_stored_a2a_card_tolerates_missing_or_broken_descriptor():
    assert reg.stored_a2a_card(_record(card=None)) == {}
    broken = _record()
    broken["descriptors"]["a2a"]["agentCard"]["inlineContent"] = "{not json"
    assert reg.stored_a2a_card(broken) == {}
    assert reg.stored_a2a_card(_record())["name"] == "aurora-faq-a2a"


# ---- route ------------------------------------------------------------------

def test_live_agent_card_reads_the_ledger_arn_and_diffs(client, plane):
    agent_id = _mk_agent()
    plane.card = {**STORED_CARD, "skills": [{"id": "faq"}, {"id": "escalate"}]}
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["agent_id"] == agent_id and body["runtime_arn"] == RUNTIME_ARN
    assert body["status_code"] == 200
    assert body["card"]["skills"][1]["id"] == "escalate"
    assert body["diff"]["identical"] is False
    assert body["diff"]["skills_only_in_live"] == ["escalate"]
    assert body["diff"]["skills_only_in_record"] == ["refunds"]
    # exactly one GetAgentCard, on the ledger's ARN, with no session id
    assert plane.get_calls == [{"agentRuntimeArn": RUNTIME_ARN, "qualifier": "DEFAULT"}]


def test_live_agent_card_identical_and_session_cleanup(client, plane):
    _mk_agent()
    plane.session_id = "s" * 33
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 200, res.text
    assert res.json()["diff"] == {"identical": True, "fields": [],
                                  "skills_only_in_live": [], "skills_only_in_record": []}
    assert plane.stop_calls[0]["runtimeSessionId"] == "s" * 33


def test_live_agent_card_stop_failure_does_not_fail_the_request(client, plane):
    _mk_agent()
    plane.session_id = "s" * 33
    plane.stop_error = "ResourceNotFoundException"
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 200, res.text
    assert res.json()["card"]["name"] == "aurora-faq-a2a"


def test_live_agent_card_refuses_record_without_ledger_agent(client, plane):
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 404
    assert res.json()["code"] == "registry.record_not_deployed"
    assert plane.get_calls == []


def test_live_agent_card_refuses_http_agent(client, plane):
    _mk_agent(spec={"protocol": "http"})
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 409
    assert res.json()["code"] == "registry.record_not_a2a"
    assert plane.get_calls == []


@pytest.mark.parametrize("status", ["deploying", "failed", "draft"])
def test_live_agent_card_refuses_agent_not_active(client, plane, status):
    _mk_agent(status=status)
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 409
    assert res.json()["code"] == "registry.agent_not_ready"
    assert plane.get_calls == []


def test_live_agent_card_ignores_deleted_and_foreign_agents(client, plane):
    _mk_agent(status="deleted")
    _mk_agent(workspace_id="acct-usw1")
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 404
    assert res.json()["code"] == "registry.record_not_deployed"
    assert plane.get_calls == []


def test_live_agent_card_maps_data_plane_client_error_to_envelope(client, plane):
    _mk_agent()
    plane.error_code = "ResourceNotFoundException"
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 404
    assert res.json()["code"] == "aws.not_found"
    plane.error_code = "AccessDeniedException"
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 403 and res.json()["code"] == "aws.access_denied"


def test_live_agent_card_unmapped_runtime_error_is_502_not_500(client, plane):
    _mk_agent()
    plane.error_code = "RuntimeClientError"  # in the GetAgentCard error list, not the map
    res = client.get(f"/api/registry/records/{RECORD_ID}/live-agent-card")
    assert res.status_code == 502
    body = res.json()
    assert body["code"] == "registry.live_card_failed"
    assert body["detail"]["aws_error_code"] == "RuntimeClientError"
