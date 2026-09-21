"""Attachment bytes, explicit fallbacks and old-runtime safety at API boundaries."""

import base64
import io
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import Agent, ApiKey, ChatMessage, ChatSession
from app.routers.apikeys import hash_key
from app.schemas.attachments import MAX_FILE_BYTES, MAX_FILES, AttachmentInput
from app.services import chat as chat_service
from app.services.agentcore import runtime
from app.services.attachment_body import AttachmentBodyCap
from app.services.attachments import attachment_capability, prepare_attachments
from tests.conftest import ws_ctx


def agent_row(method="zip_runtime", *, native=True, protocol="http"):
    with SessionLocal() as db:
        row = Agent(
            name="attachments", method=method, status="active", version="2",
            workspace_id=DEFAULT_WORKSPACE_ID,
            arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/test",
            attachment_version="2" if native else None,
            spec={"protocol": protocol, "model_id": "global.anthropic.claude-sonnet-4-6"},
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def attachment(name: str, data: bytes, mime: str = "") -> AttachmentInput:
    return AttachmentInput(name=name, media_type=mime, data=base64.b64encode(data).decode())


def png(width=30) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, 20), "navy").save(buffer, format="PNG")
    return buffer.getvalue()


def pdf(text="file-code-732") -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=240)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 24 Tf 35 160 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def scanned_pdf() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (30, 20), "navy").save(buffer, format="PDF")
    return buffer.getvalue()


def test_native_file_bytes_and_filename_mapping():
    data = png()
    result = prepare_attachments(
        agent_row(), [attachment("照片.png", data, "image/png")], prompt="Read this",
    )
    assert base64.b64decode(result.native[0]["data"]) == data
    assert result.native[0]["name"] == "attachment-1.png"
    assert "照片.png" in result.prompt("Read this")
    assert result.metadata == [{
        "name": "照片.png", "media_type": "image/png", "size": len(data), "delivery": "native",
    }]
    assert "data" not in result.metadata[0]


def test_harness_pdf_extracts_text_and_explains_loss():
    result = prepare_attachments(agent_row("harness"), [attachment("report.pdf", pdf())])
    assert not result.native
    assert "file-code-732" in result.prompt("")
    assert "PDF text only" in result.prompt("")
    assert result.metadata[0]["delivery"] == "pdf_text"


@pytest.mark.parametrize(
    "method", ["harness", "studio", "zip_runtime", "discovered_runtime", "byoc"],
)
def test_old_or_custom_agents_get_text_files_without_republish(method):
    result = prepare_attachments(
        agent_row(method, native=False), [attachment("note.md", "中文 file-code-732".encode())],
    )
    assert not result.native
    assert "中文 file-code-732" in result.prompt("")


def test_scanned_pdf_has_no_text_fallback_but_native_keeps_original_bytes():
    data = scanned_pdf()
    with pytest.raises(AppError, match="readable text") as exc:
        prepare_attachments(agent_row("harness"), [attachment("scan.pdf", data)])
    assert exc.value.code == "chat.attachment_pdf_text_unavailable"
    native = prepare_attachments(agent_row(), [attachment("scan.pdf", data)])
    assert base64.b64decode(native.native[0]["data"]) == data


@pytest.mark.parametrize(
    ("item", "code"),
    [
        (AttachmentInput(name="x.txt", data="!!!"), "invalid"),
        (attachment("../x.txt", b"hello"), "invalid"),
        (attachment("x.txt", b"\x89PNG\x00fake"), "invalid"),
        (attachment("x.txt", b"\xff\xff"), "invalid"),
        (attachment("x.txt", b""), "invalid"),
        (attachment("x.png", b"not an image"), "invalid"),
        (attachment("x.jpg", png()), "invalid"),
        (attachment("x.png", png(), "application/pdf"), "invalid"),
        (attachment("x.png", png(8001)), "invalid"),
        (attachment("x.pdf", b"not PDF"), "pdf_unreadable"),
        (attachment("x.pdf", b"%PDF-1.7 broken"), "pdf_unreadable"),
        (attachment("x.zip", b"PK\x03\x04"), "unsupported"),
        (attachment("x.txt", b"a" * (MAX_FILE_BYTES + 1)), "too_large"),
        (attachment("x.txt", b"a" * 100001), "text_too_large"),
    ],
)
def test_invalid_files_fail_before_invoke(item, code):
    with pytest.raises(AppError) as exc:
        prepare_attachments(agent_row(), [item])
    assert exc.value.code == f"chat.attachment_{code}"


def test_harness_image_rejection_and_limits():
    with pytest.raises(AppError) as exc:
        prepare_attachments(agent_row("harness"), [attachment("x.png", png())])
    assert exc.value.code == "chat.attachment_image_unsupported"
    with pytest.raises(AppError) as exc:
        prepare_attachments(agent_row(), [attachment("x.txt", b"test")] * (MAX_FILES + 1))
    assert exc.value.code == "chat.attachment_too_many"


def test_total_decoded_limit_and_combined_text_budget():
    data = png()
    padded = data + b"\x00" * (MAX_FILE_BYTES - len(data))
    with pytest.raises(AppError) as exc:
        prepare_attachments(agent_row(), [attachment(f"x{i}.png", padded) for i in range(4)])
    assert exc.value.code == "chat.attachment_too_large"
    with pytest.raises(AppError) as exc:
        prepare_attachments(
            agent_row("harness"), [attachment("x.txt", b"x" * 100)],
            prompt="p" * 99950,
        )
    assert exc.value.code == "chat.attachment_text_too_large"


def test_contract_is_version_bound_and_model_aware():
    row = agent_row()
    assert attachment_capability(row)["images"]
    row.version = "3"
    assert attachment_capability(row)["reason_code"] == "republish"
    row.attachment_version = "3"
    row.spec = {"model_id": "unknown-text-model"}
    assert not attachment_capability(row)["images"]
    row = agent_row(native=False, protocol="a2a")
    assert attachment_capability(row)["images"]


def test_byoc_with_model_selection_still_requires_a_custom_input_contract():
    row = agent_row("byoc", native=False)
    capability = attachment_capability(row)
    assert capability["images"] is False
    assert capability["pdf"] == "text"
    assert capability["reason_code"] == "custom"


def test_custom_json_event_reply_is_preserved_with_attachment_acknowledgement():
    payload = {
        "event": "business_reply", "answer": "file-code-732",
        "attachment_contract": "v1", "metadata": {"source": "custom"},
    }
    assert list(runtime._normalized_runtime_events(
        [payload], require_attachments=True,
    )) == [{"event": "delta", "data": {"text": "file-code-732"}}]


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_business_attachment_event_is_not_mistaken_for_protocol_ack(content_type):
    payload = {"event": "attachments", "answer": "Your report is ready", "metadata": {}}
    encoded = json.dumps(payload).encode()
    if content_type == "text/event-stream":
        encoded = b"data: " + encoded + b"\n\n"
    client = SimpleNamespace(invoke_agent_runtime=lambda **_: {
        "contentType": content_type, "response": io.BytesIO(encoded),
    })
    assert runtime.invoke_runtime_text(client, "arn", "read")["text"] == "Your report is ready"
    with pytest.raises(AppError) as exc:
        runtime.invoke_runtime_text(
            client, "arn", "read", attachments=[attachment("x.png", png()).model_dump()],
        )
    assert exc.value.code == "chat.attachment_new_session_required"


def test_old_console_session_cannot_receive_native_attachments():
    row = agent_row()
    with SessionLocal() as db:
        db.add(ChatSession(
            workspace_id=row.workspace_id, agent_id=row.id, session_id="old-session",
            actor_id="river", runtime_version="1",
        ))
        db.commit()
    with pytest.raises(AppError) as exc:
        prepare_attachments(row, [attachment("x.png", png())], session_id="old-session")
    assert exc.value.code == "chat.attachment_new_session_required"


def test_native_canary_is_refused_but_text_fallback_is_safe(monkeypatch):
    from app.optimization import canary_service
    monkeypatch.setattr(canary_service, "active_canary_route", lambda _: {"arn": "test"})
    with pytest.raises(AppError) as exc:
        prepare_attachments(agent_row(), [attachment("x.png", png())])
    assert exc.value.code == "chat.attachment_canary_unsupported"
    assert prepare_attachments(agent_row(), [attachment("x.txt", b"safe text")]).native == []


def test_runtime_acknowledgement_and_a2a_file_parts():
    data = attachment("x.png", png()).model_dump()
    captured = {}

    def invoke(**kwargs):
        captured.update(json.loads(kwargs["payload"]))
        return {"contentType": "text/event-stream", "response": io.BytesIO(
            b'data: {"event":"attachments","contract":"v1"}\n\n'
            b'data: {"event":"delta","text":"file-code-732"}\n\n'
        )}

    assert runtime.invoke_runtime_text(
        SimpleNamespace(invoke_agent_runtime=invoke), "arn", "Read", attachments=[data],
    )["text"] == "file-code-732"
    assert captured["attachments"] == [data]

    def a2a_invoke(**kwargs):
        captured.update(json.loads(kwargs["payload"]))
        return {"response": io.BytesIO(json.dumps({
            "result": {"parts": [{"kind": "text", "text": "read"}]},
        }).encode())}

    runtime.invoke_a2a_text(
        SimpleNamespace(invoke_agent_runtime=a2a_invoke), "arn", "Read", attachments=[data],
    )
    file = captured["params"]["message"]["parts"][1]["file"]
    assert base64.b64decode(file["bytes"]) == png()
    assert file["mimeType"] == ""


@pytest.mark.parametrize("payload", [
    b'data: {"event":"delta","text":"NO_ATTACHMENT"}\n\n',
    b'{"result":"NO_ATTACHMENT"}',
    b"",
])
def test_old_runtime_never_reports_success_when_it_ignored_a_file(payload):
    body = io.BytesIO(payload)
    client = SimpleNamespace(invoke_agent_runtime=lambda **_: {
        "response": body, "contentType": "application/json",
    })
    with pytest.raises(AppError) as exc:
        runtime.invoke_runtime_text(
            client, "arn", "Read", attachments=[attachment("x.png", png()).model_dump()],
        )
    assert exc.value.code == "chat.attachment_new_session_required"
    assert body.closed


def test_attachment_only_chat_persists_only_metadata_and_history(monkeypatch):
    row = agent_row("harness")
    captured = []

    def harness_events(_agent, prompt, *args, **kwargs):
        captured.append(prompt)
        yield {"event": "delta", "data": {"text": "read file-code-732"}}

    monkeypatch.setattr(chat_service, "_harness_events", harness_events)
    client = TestClient(create_app())
    item = attachment("note.txt", b"file-code-732").model_dump()
    response = client.post(f"/api/chat/{row.id}", json={"attachments": [item]})
    assert response.status_code == 200
    assert "file-code-732" in captured[0]
    assert '"attachments":' in response.text
    with SessionLocal() as db:
        message = db.query(ChatMessage).filter_by(agent_id=row.id, role="user").one()
        session = db.query(ChatSession).filter_by(agent_id=row.id).one()
        assert session.runtime_version == row.version
        assert message.text == ""
        assert "data" not in message.attachments[0]
        assert item["data"] not in json.dumps(message.attachments)
        sid = session.session_id
    history = client.get(f"/api/chat/{row.id}/history", params={"session_id": sid}).json()
    assert history["messages"][0]["attachments"][0]["name"] == "note.txt"


def test_public_sync_and_stream_use_same_text_adapter(monkeypatch):
    from app.routers import public_api

    row = agent_row("harness")
    with SessionLocal() as db:
        db.add(ApiKey(
            workspace_id=row.workspace_id, name="files", prefix="test",
            key_hash=hash_key("attachment-key"),
        ))
        db.commit()
    prompts = []

    def sync(_agent, prompt, **kwargs):
        prompts.append(kwargs["attachments"].prompt(prompt))
        return {"text": "ok", "session_id": "s" * 36}

    def stream(_agent, prompt, *args, **kwargs):
        prompts.append(prompt)
        yield {"event": "delta", "data": {"text": "ok"}}

    monkeypatch.setattr(public_api, "invoke_agent_text", sync)
    monkeypatch.setattr(chat_service, "_harness_events", stream)
    client = TestClient(create_app())
    body = {"attachments": [attachment("x.txt", b"public-file-token").model_dump()]}
    for suffix in ("invoke", "invoke-stream"):
        response = client.post(
            f"/v1/agents/{row.id}/{suffix}", json=body,
            headers={"X-Api-Key": "attachment-key"},
        )
        assert response.status_code == 200
    assert prompts[0] == prompts[1]
    assert "public-file-token" in prompts[0]


@pytest.mark.parametrize("protocol", ["http", "a2a"])
@pytest.mark.parametrize("route", [
    "/api/chat/{agent_id}",
    "/api/agents/{agent_id}/invoke",
    "/v1/agents/{agent_id}/invoke",
    "/v1/agents/{agent_id}/invoke-stream",
])
def test_native_bytes_reach_runtime_through_every_entrance(monkeypatch, protocol, route):
    from app.services import invoke

    row = agent_row(protocol=protocol)
    captured = []
    with SessionLocal() as db:
        db.add(ApiKey(
            workspace_id=row.workspace_id, name="native-files", prefix="test",
            key_hash=hash_key("native-attachment-key"),
        ))
        db.commit()

    def runtime_invoke(**kwargs):
        captured.append(json.loads(kwargs["payload"]))
        if protocol == "a2a":
            return {"response": io.BytesIO(json.dumps({
                "result": {"kind": "message", "parts": [{"kind": "text", "text": "native-ok"}]},
            }).encode())}
        return {"contentType": "text/event-stream", "response": io.BytesIO(
            b'data: {"event":"attachments","contract":"v1"}\n\n'
            b'data: {"event":"delta","text":"native-ok"}\n\n'
        )}

    monkeypatch.setattr(
        invoke, "data_client", lambda _: SimpleNamespace(invoke_agent_runtime=runtime_invoke),
    )
    data = png()
    response = TestClient(create_app()).post(
        route.format(agent_id=row.id),
        json={"attachments": [attachment("photo.png", data, "image/png").model_dump()]},
        headers={"X-Api-Key": "native-attachment-key"},
    )
    assert response.status_code == 200
    assert "native-ok" in response.text
    assert len(captured) == 1
    if protocol == "a2a":
        file = captured[0]["params"]["message"]["parts"][1]["file"]
        assert file["mimeType"] == "image/png"
        encoded = file["bytes"]
    else:
        file = captured[0]["attachments"][0]
        assert file["media_type"] == "image/png"
        encoded = file["data"]
    assert file["name"] == "attachment-1.png"
    assert base64.b64decode(encoded) == data


def test_invalid_attachment_errors_do_not_echo_file_data():
    row = agent_row()
    response = TestClient(create_app()).post(f"/api/chat/{row.id}", json={
        "attachments": [{"name": None, "data": "DO_NOT_ECHO_THIS_FILE"}],
    })
    assert response.status_code == 422
    assert "DO_NOT_ECHO_THIS_FILE" not in response.text


@pytest.mark.parametrize("wrap", [
    lambda body: [body],
    lambda body: json.dumps(body),
])
def test_invalid_attachment_root_does_not_echo_file_data(wrap):
    row = agent_row()
    body = {"attachments": [{"name": "x.txt", "data": "DO_NOT_ECHO_THIS_FILE"}]}
    response = TestClient(create_app()).post(f"/api/chat/{row.id}", json=wrap(body))
    assert response.status_code == 422
    assert "DO_NOT_ECHO_THIS_FILE" not in response.text
    assert all("input" not in error for error in response.json()["detail"])


def test_truncated_jpeg_is_rejected_before_invoke():
    buffer = io.BytesIO()
    Image.new("RGB", (30, 20), "navy").save(buffer, format="JPEG")
    data = buffer.getvalue()[:-10]
    # JPEG headers still pass Pillow.verify(); the pixel stream cannot decode.
    with pytest.raises(AppError) as exc:
        prepare_attachments(agent_row(), [attachment("truncated.jpg", data, "image/jpeg")])
    assert exc.value.code == "chat.attachment_invalid"


def test_corrupt_png_returns_validation_error_before_sse():
    row = agent_row()
    data = bytearray(png())
    data[data.index(b"IDAT") + 5] ^= 1
    response = TestClient(create_app()).post(f"/api/chat/{row.id}", json={
        "attachments": [attachment("corrupt.png", bytes(data), "image/png").model_dump()],
    })
    assert response.status_code == 422
    assert response.json()["code"] == "chat.attachment_invalid"
    with SessionLocal() as db:
        assert db.query(ChatMessage).filter_by(agent_id=row.id).count() == 0


def test_streamed_body_cap_does_not_depend_on_content_length():
    app = create_app()
    app.add_middleware(AttachmentBodyCap, max_bytes=50)
    row = agent_row()
    response = TestClient(app).post(
        f"/api/chat/{row.id}",
        content=iter([b'{"prompt":"', b"x" * 80, b'"}']),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "chat.attachment_too_large"


def test_harness_chat_stream_receives_prepared_text(monkeypatch):
    row = agent_row("harness")
    received = []
    def events(_agent, prompt, *args, **kwargs):
        received.append(prompt)
        yield {"event": "delta", "data": {"text": "ok"}}
    monkeypatch.setattr(chat_service, "_harness_events", events)
    files = prepare_attachments(row, [attachment("x.txt", b"test-token")])
    result = list(chat_service.chat_stream(row, "read", attachments=files, workspace=ws_ctx()))
    assert "test-token" in received[0]
    assert result[0]["data"]["attachments"][0]["delivery"] == "text"
