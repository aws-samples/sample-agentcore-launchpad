"""``InvokeAgentRuntime`` request shape for routed sessions (N12 E3-2, plan §B.1
``_runtime_invoke_params`` row): ``baggage`` is a first-class request parameter and the
bundle pointer also travels as ``payload["launchpad"]`` — the guaranteed channel (CA-2)."""

import json

from app.services.agentcore import runtime as rt

ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/agent-abc"
BAGGAGE = (
    "launchpad.bundle=bndl_t%3A2,"
    "aws.agentcore.gateway.routing_experiment_arn=exp1,"
    "aws.agentcore.gateway.routing_experiment_variant_name=T1"
)
META = {"bundle": "bndl_t:2", "experiment": "exp1", "variant": "T1"}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": _Body(b'{"result": "ok"}'), "contentType": "application/json"}


def test_invoke_params_carry_baggage_and_the_payload_alias():
    params = rt._runtime_invoke_params(
        ARN, "hi", "s" * 40, "river", None, baggage=BAGGAGE, launchpad_meta=META,
    )
    assert params["baggage"] == BAGGAGE
    assert "qualifier" not in params
    payload = json.loads(params["payload"])
    assert payload["launchpad"] == META
    assert payload["prompt"] == "hi" and payload["actor_id"] == "river"


def test_invoke_params_omit_both_when_the_session_is_not_routed():
    params = rt._runtime_invoke_params(ARN, "hi", "s" * 40, "river", None)
    assert "baggage" not in params
    assert "launchpad" not in json.loads(params["payload"])


def test_endpoint_arm_sets_the_qualifier_without_a_bundle_pointer():
    baggage = (
        "aws.agentcore.gateway.routing_experiment_arn=can1,"
        "aws.agentcore.gateway.routing_experiment_variant_name=C"
    )
    params = rt._runtime_invoke_params(
        ARN, "hi", "s" * 40, "river", "stableabc123", baggage=baggage,
        launchpad_meta={"experiment": "can1", "variant": "C"},
    )
    assert params["qualifier"] == "stableabc123"
    assert params["baggage"] == baggage
    assert json.loads(params["payload"])["launchpad"] == {"experiment": "can1", "variant": "C"}


def test_invoke_runtime_text_and_stream_pass_the_routing_kwargs_through():
    client = _Client()
    result = rt.invoke_runtime_text(
        client, ARN, "hi", session_id="s" * 40, baggage=BAGGAGE, launchpad_meta=META,
    )
    assert result["text"] == "ok"
    (call,) = client.calls
    assert call["baggage"] == BAGGAGE
    assert json.loads(call["payload"])["launchpad"] == META

    client = _Client()
    events = list(rt.stream_runtime_events(
        client, ARN, "hi", session_id="s" * 40, qualifier="treatabc123",
        baggage=BAGGAGE, launchpad_meta=META,
    ))
    assert [e["event"] for e in events] == ["delta"]
    (call,) = client.calls
    assert call["qualifier"] == "treatabc123"
    assert call["baggage"] == BAGGAGE
    assert json.loads(call["payload"])["launchpad"] == META
