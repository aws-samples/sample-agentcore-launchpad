"""Structured payload passthrough: bounds, capability, wire shape and ledger."""

import io
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import ApiKey, ChatMessage
from app.routers.apikeys import hash_key
from app.schemas.attachments import MAX_PAYLOAD_BYTES, AttachmentRequest
from app.services import chat as chat_service
from app.services.agentcore import runtime
from app.services.payloads import payload_capability, payload_summary
from tests.test_chat_attachments import agent_row

PAYLOAD = {"customer_id": "C-42", "options": {"temperature": 0.1}}


def api_key(workspace_id: str, raw: str = "payload-key") -> str:
    with SessionLocal() as db:
        db.add(ApiKey(
            workspace_id=workspace_id, name="payload", prefix="test", key_hash=hash_key(raw),
        ))
        db.commit()
    return raw


@pytest.mark.parametrize("key", [
    "prompt", "attachments", "actor_id", "session_id",
    "gateway_access_token", "force_reauth_providers",
])
def test_reserved_envelope_keys_are_rejected_not_merged(key):
    with pytest.raises(AppError) as exc:
        AttachmentRequest(prompt="hi", payload={key: "injected", "safe": 1})
    assert exc.value.code == "invoke.payload_reserved_key"
    assert exc.value.detail == {"keys": [key]}
    assert exc.value.status_code == 422


def test_oversized_payload_is_rejected_with_named_code():
    with pytest.raises(AppError) as exc:
        AttachmentRequest(prompt="hi", payload={"blob": "x" * MAX_PAYLOAD_BYTES})
    assert exc.value.code == "invoke.payload_too_large"
    assert exc.value.detail["max_bytes"] == MAX_PAYLOAD_BYTES
    assert exc.value.status_code == 422


@pytest.mark.parametrize("payload", [["a"], "scalar", 42, True])
def test_non_object_payload_is_a_validation_error(payload):
    row = agent_row()
    response = TestClient(create_app()).post(
        f"/api/chat/{row.id}", json={"prompt": "hi", "payload": payload},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation.invalid_request"


def test_payload_only_request_is_accepted():
    req = AttachmentRequest(payload=PAYLOAD)
    assert req.prompt == ""
    with pytest.raises(ValueError):
        AttachmentRequest()


def test_http_runtime_receives_caller_keys_flat_next_to_envelope():
    captured = {}

    def invoke(**kwargs):
        captured.update(json.loads(kwargs["payload"]))
        return {"contentType": "application/json",
                "response": io.BytesIO(b'{"result":"ok"}')}

    client = SimpleNamespace(invoke_agent_runtime=invoke)
    runtime.invoke_runtime_text(client, "arn", "Read", extra_payload=PAYLOAD)
    assert captured["customer_id"] == "C-42"
    assert captured["options"] == {"temperature": 0.1}
    assert captured["prompt"] == "Read"
    assert captured["actor_id"] == "default"


def test_envelope_fields_win_over_extra_payload_defense_in_depth():
    # The schema refuses reserved keys; the wire builder must still not let a
    # bypassing caller override the envelope.
    params = runtime._runtime_invoke_params(
        "arn", "Read", "s" * 36, "actor", None,
        extra_payload={"prompt": "evil", "actor_id": "evil"},
    )
    body = json.loads(params["payload"])
    assert body["prompt"] == "Read"
    assert body["actor_id"] == "actor"


def test_a2a_runtime_receives_payload_as_data_part():
    captured = {}

    def invoke(**kwargs):
        captured.update(json.loads(kwargs["payload"]))
        return {"response": io.BytesIO(json.dumps({
            "result": {"kind": "message", "parts": [{"kind": "text", "text": "ok"}]},
        }).encode())}

    runtime.invoke_a2a_text(
        SimpleNamespace(invoke_agent_runtime=invoke), "arn", "Read", extra_payload=PAYLOAD,
    )
    parts = captured["params"]["message"]["parts"]
    assert parts[0] == {"kind": "text", "text": "Read"}
    assert parts[-1] == {"kind": "data", "data": PAYLOAD}


def test_harness_agent_answers_422_before_any_aws_call():
    row = agent_row("harness")
    response = TestClient(create_app()).post(
        f"/api/chat/{row.id}", json={"prompt": "hi", "payload": PAYLOAD},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invoke.payload_unsupported"
    assert response.json()["detail"] == {"reason_code": "harness"}


def test_active_canary_refuses_payload(monkeypatch):
    from app.optimization import canary_service
    monkeypatch.setattr(canary_service, "active_canary_route", lambda _: {"arn": "test"})
    row = agent_row()
    response = TestClient(create_app()).post(
        f"/api/chat/{row.id}", json={"prompt": "hi", "payload": PAYLOAD},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "invoke.payload_canary_unsupported"


@pytest.mark.parametrize("protocol", ["http", "a2a"])
@pytest.mark.parametrize("route", [
    "/api/chat/{agent_id}",
    "/api/agents/{agent_id}/invoke",
    "/v1/agents/{agent_id}/invoke",
    "/v1/agents/{agent_id}/invoke-stream",
])
def test_payload_reaches_runtime_through_every_entrance(monkeypatch, protocol, route):
    from app.services import invoke

    row = agent_row(protocol=protocol)
    key = api_key(row.workspace_id)
    captured = []

    def runtime_invoke(**kwargs):
        captured.append(json.loads(kwargs["payload"]))
        if protocol == "a2a":
            return {"response": io.BytesIO(json.dumps({
                "result": {"kind": "message", "parts": [{"kind": "text", "text": "ok"}]},
            }).encode())}
        return {"contentType": "application/json",
                "response": io.BytesIO(b'{"result":"ok"}')}

    monkeypatch.setattr(
        invoke, "data_client", lambda _: SimpleNamespace(invoke_agent_runtime=runtime_invoke),
    )
    response = TestClient(create_app()).post(
        route.format(agent_id=row.id),
        json={"prompt": "hi", "payload": PAYLOAD},
        headers={"X-Api-Key": key},
    )
    assert response.status_code == 200
    assert len(captured) == 1
    if protocol == "a2a":
        assert captured[0]["params"]["message"]["parts"][-1] == {"kind": "data", "data": PAYLOAD}
    else:
        assert captured[0]["customer_id"] == "C-42"
        assert captured[0]["prompt"] == "hi"


def test_payload_only_chat_persists_summary_not_raw_payload(monkeypatch):
    from app.services import invoke

    row = agent_row()
    secret = {"customer_id": "C-42", "note": "RAW_PAYLOAD_MARKER" * 400}

    def runtime_invoke(**kwargs):
        return {"contentType": "application/json",
                "response": io.BytesIO(b'{"result":"handled"}')}

    monkeypatch.setattr(
        invoke, "data_client", lambda _: SimpleNamespace(invoke_agent_runtime=runtime_invoke),
    )
    client = TestClient(create_app())
    response = client.post(f"/api/chat/{row.id}", json={"payload": secret})
    assert response.status_code == 200
    with SessionLocal() as db:
        message = db.query(ChatMessage).filter_by(agent_id=row.id, role="user").one()
        assert message.text == ""
        assert message.payload["keys"] == ["customer_id", "note"]
        assert message.payload["truncated"] is True
        assert len(message.payload["json"]) <= 2049
        sid = message.session_id
    history = client.get(f"/api/chat/{row.id}/history", params={"session_id": sid}).json()
    assert history["messages"][0]["payload"]["keys"] == ["customer_id", "note"]


def test_payload_summary_is_compact_and_sorted():
    assert payload_summary(None) is None
    assert payload_summary({}) is None
    summary = payload_summary({"b": 1, "a": 2})
    assert summary == {"keys": ["a", "b"], "json": '{"a": 2, "b": 1}', "truncated": False}


def test_capability_is_exposed_next_to_attachments_on_both_lists():
    row = agent_row()
    harness = agent_row("harness")
    key = api_key(row.workspace_id, "capability-key")
    client = TestClient(create_app())
    console = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
    assert console[row.id]["payload_capability"]["supported"] is True
    assert console[harness.id]["payload_capability"] == {
        "supported": False, "reason_code": "harness",
        "max_bytes": MAX_PAYLOAD_BYTES,
        "reserved_keys": payload_capability(harness)["reserved_keys"],
    }
    public = {a["id"]: a for a in client.get(
        "/v1/agents", headers={"X-Api-Key": key},
    ).json()["agents"]}
    assert public[row.id]["payload_capability"]["supported"] is True


def test_chat_meta_carries_payload_summary_not_raw(monkeypatch):
    from app.services import invoke as invoke_module

    row = agent_row()
    client = SimpleNamespace(invoke_agent_runtime=lambda **_: {
        "contentType": "application/json", "response": io.BytesIO(b'{"result":"ok"}'),
    })
    monkeypatch.setattr(invoke_module, "data_client", lambda _: client)
    from tests.conftest import ws_ctx
    result = list(chat_service.chat_stream(
        row, "hi", extra_payload={"a": 1}, workspace=ws_ctx(),
    ))
    assert result[0]["data"]["payload"] == {
        "keys": ["a"], "json": '{"a": 1}', "truncated": False,
    }
    assert result[-1]["event"] == "done"


def test_harness_chat_stream_refuses_payload_as_error_event():
    row = agent_row("harness")
    from tests.conftest import ws_ctx
    result = list(chat_service.chat_stream(
        row, "hi", extra_payload=PAYLOAD, workspace=ws_ctx(),
    ))
    assert result[-1]["event"] == "error"
    assert result[-1]["data"]["code"] == "invoke.payload_unsupported"
