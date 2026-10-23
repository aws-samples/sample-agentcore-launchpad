"""Images an agent answers with: runtime contract, validation, storage, read-back auth."""

import base64
import hashlib
import io
import json
import time
from types import SimpleNamespace

import pytest
from botocore.response import StreamingBody
from fastapi.testclient import TestClient
from PIL import Image

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.assistant import AssistantConversation, AssistantMessage
from app.models.ledger import Agent, ApiKey, ChatMessage, ChatOutputAttachment, Workspace
from app.routers.apikeys import hash_key
from app.schemas.attachments import MAX_FILE_BYTES, MAX_FILES
from app.services import outbound_attachments as oa
from app.services.agentcore import runtime as rt
from tests.conftest import client_error, ws_ctx

SID = "sess-" + "a" * 40


def image(fmt="PNG", size=(40, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format=fmt)
    return buffer.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def frame(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


class FakeS3:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.fail_put = False

    def put_object(self, **kwargs):
        if self.fail_put:
            raise client_error("AccessDenied", operation="PutObject")
        self.objects[kwargs["Key"]] = kwargs

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3's parameter names
        if Key not in self.objects:
            raise client_error("NoSuchKey", operation="GetObject")
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(oa, "_s3", lambda _ws: fake)
    monkeypatch.setattr(oa, "_bucket", lambda _ws: "launchpad-artifacts-test")
    return fake


def agent_row(method="zip_runtime", workspace_id=DEFAULT_WORKSPACE_ID) -> Agent:
    with SessionLocal() as db:
        row = Agent(
            name=f"shots-{method}", method=method, status="active", version="1",
            workspace_id=workspace_id, arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
            spec={"protocol": "http", "model_id": "global.anthropic.claude-sonnet-4-6"},
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def api_key(workspace_id=DEFAULT_WORKSPACE_ID, raw="reply-attachment-key") -> ApiKey:
    with SessionLocal() as db:
        key = ApiKey(workspace_id=workspace_id, name=raw, prefix="test", key_hash=hash_key(raw))
        db.add(key)
        db.commit()
        db.refresh(key)
        return key


def stub_runtime(monkeypatch, body: bytes, content_type="text/event-stream"):
    from app.services import invoke

    def runtime_invoke(**kwargs):
        return {"contentType": content_type, "response": io.BytesIO(body)}

    monkeypatch.setattr(
        invoke, "data_client", lambda _: SimpleNamespace(invoke_agent_runtime=runtime_invoke),
    )


def sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


# ---- runtime contract (rt parser) ---------------------------------------------------


def test_sse_attachment_frame_is_a_raw_attachment_event():
    data = b64(image())
    body = frame({"event": "delta", "text": "here"}) + frame(
        {"event": "attachment", "name": "shot.png", "media_type": "image/png", "data": data},
    )
    events = list(rt._runtime_body_events(io.BytesIO(body), "text/event-stream", False))
    assert events == [
        {"event": "delta", "data": {"text": "here"}},
        {"event": "attachment",
         "data": {"name": "shot.png", "media_type": "image/png", "data": data}},
    ]


@pytest.mark.parametrize("reply", [
    {"result": "see image"},
    {"answer": "see image"},
])
def test_json_reply_attachments_follow_the_text_and_never_enter_it(reply):
    data = b64(image())
    body = json.dumps({**reply, "attachments": [
        {"name": "a.png", "media_type": "image/png", "data": data},
    ]}).encode()
    events = list(rt._runtime_body_events(io.BytesIO(body), "application/json", False))
    assert events[0] == {"event": "delta", "data": {"text": "see image"}}
    assert events[1]["event"] == "attachment"
    assert events[1]["data"]["data"] == data
    assert all(data not in json.dumps(e) for e in events[:1])


def test_a_json_reply_with_only_attachments_dumps_no_base64():
    data = b64(image())
    body = json.dumps({"attachments": [{"name": "a.png", "media_type": "image/png",
                                        "data": data}], "note": "x"}).encode()
    events = list(rt._runtime_body_events(io.BytesIO(body), "application/json", False))
    assert [e["event"] for e in events] == ["delta", "attachment"]
    assert data not in events[0]["data"]["text"]


def test_an_unrelated_attachments_field_is_left_alone():
    body = json.dumps({"result": "ok", "attachments": ["report.pdf"]}).encode()
    events = list(rt._runtime_body_events(io.BytesIO(body), "application/json", False))
    assert events == [{"event": "delta", "data": {"text": "ok"}}]


def test_invoke_runtime_text_collects_raw_reply_attachments():
    data = b64(image())
    client = SimpleNamespace(invoke_agent_runtime=lambda **_: {
        "contentType": "text/event-stream",
        "response": io.BytesIO(frame({"event": "delta", "text": "t"}) + frame(
            {"event": "attachment", "name": "a.png", "media_type": "image/png", "data": data})),
    })
    result = rt.invoke_runtime_text(client, "arn", "hi", session_id=SID)
    assert result["text"] == "t"
    assert result["reply_attachments"][0]["data"] == data


def test_a_multi_megabyte_attachment_line_is_split_in_linear_time():
    """botocore's iter_lines is quadratic on one long line; the parser must not be."""
    data = b64(b"\x00" * (MAX_FILE_BYTES - 1024))
    body = frame({"event": "attachment", "name": "big.png", "media_type": "image/png",
                  "data": data}) + frame({"event": "delta", "text": "after"})
    started = time.monotonic()
    events = list(rt._runtime_body_events(
        StreamingBody(io.BytesIO(body), len(body)), "text/event-stream", False,
    ))
    assert time.monotonic() - started < 5
    assert events[0]["data"]["data"] == data
    assert events[1] == {"event": "delta", "data": {"text": "after"}}


def test_an_oversized_line_is_discarded_while_streaming_and_reported():
    huge = b"data: " + b"A" * (rt.MAX_SSE_LINE_BYTES + 10) + b"\n\n"
    body = frame({"event": "delta", "text": "before"}) + huge + frame(
        {"event": "delta", "text": "after"})
    events = list(rt._runtime_body_events(
        StreamingBody(io.BytesIO(body), len(body)), "text/event-stream", False,
    ))
    assert events == [
        {"event": "delta", "data": {"text": "before"}},
        {"event": "attachment", "data": {"oversized": True}},
        {"event": "delta", "data": {"text": "after"}},
    ]


def test_the_httpx_adapter_uses_the_same_bounded_splitter():
    data = b64(image())
    body = frame({"event": "delta", "text": "hi"}) + frame(
        {"event": "attachment", "name": "a.png", "media_type": "image/png", "data": data})
    response = SimpleNamespace(iter_bytes=lambda: iter([body[:7], body[7:5000], body[5000:]]),
                               iter_lines=lambda: pytest.fail("iter_lines used"))
    events = list(rt._runtime_body_events(rt._HttpxBody(response), "text/event-stream", False))
    assert [e["event"] for e in events] == ["delta", "attachment"]
    assert events[1]["data"]["data"] == data


# ---- validation + storage -----------------------------------------------------------


def sink(owner="console", session_id=SID) -> oa.ReplyAttachments:
    return oa.ReplyAttachments(agent_row(), ws_ctx(), session_id, owner)


def raw(data: bytes, media_type="image/png", name="shot.png") -> dict:
    return {"name": name, "media_type": media_type, "data": b64(data)}


def test_a_valid_png_is_stored_under_the_workspace_prefix_with_metadata_only(s3):
    reply = sink()
    data = image()
    meta = reply.accept(raw(data))
    digest = hashlib.sha256(data).hexdigest()
    assert meta == {
        "id": meta["id"], "name": "shot.png", "media_type": "image/png",
        "size_bytes": len(data), "sha256": digest, "status": "stored",
        "url": f"/api/chat/{reply.agent.id}/attachments/{meta['id']}",
    }
    key = f"chat-attachments/{DEFAULT_WORKSPACE_ID}/{reply.agent.id}/{SID}/{meta['id']}.png"
    put = s3.objects[key]
    assert put["Body"] == data and put["ContentType"] == "image/png"
    assert put["ServerSideEncryption"] == "AES256" and put["Bucket"] == "launchpad-artifacts-test"
    with SessionLocal() as db:
        row = db.get(ChatOutputAttachment, meta["id"])
        assert (row.storage_key, row.sha256, row.owner, row.session_id) == (
            key, digest, "console", SID)


@pytest.mark.parametrize(("item", "reason"), [
    (raw(image("JPEG"), "image/png"), "invalid"),  # forged type: JPEG bytes labelled PNG
    (raw(image(), "image/jpeg", "x.jpg"), "invalid"),
    (raw(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", "x.svg"),
     "unsupported_type"),
    (raw(b"not an image"), "invalid"),
    (raw(image()[:40]), "invalid"),  # truncated
    (raw(image(size=(8001, 2))), "invalid"),  # over the dimension limit
    ({"name": "x.png", "media_type": "image/png", "data": "!!not base64!!"}, "invalid"),
    ({"name": "x.png", "media_type": "image/png", "data": ""}, "invalid"),
    ({"name": "x.png", "media_type": "image/png", "data": "A" * (4 * MAX_FILE_BYTES)},
     "too_large"),
    ({"oversized": True}, "too_large"),
])
def test_refused_images_are_dropped_with_a_reason_never_stored(s3, item, reason):
    meta = sink().accept(item)
    assert meta["status"] == "dropped" and meta["reason_code"] == reason
    assert "data" not in meta and "id" not in meta
    assert s3.objects == {}


def test_per_turn_count_limit(s3):
    reply = sink()
    metas = [reply.accept(raw(image())) for _ in range(MAX_FILES + 1)]
    assert [m["status"] for m in metas] == ["stored"] * MAX_FILES + ["dropped"]
    assert metas[-1]["reason_code"] == "too_many"
    assert len(s3.objects) == MAX_FILES


def test_storage_failure_drops_the_image_but_not_the_turn(s3):
    s3.fail_put = True
    assert sink().accept(raw(image())) == {
        "name": "shot.png", "status": "dropped", "reason_code": "storage_unavailable",
    }


def test_an_entrance_without_an_owner_stores_nothing(s3):
    assert sink(owner=None).accept(raw(image())) is None
    assert s3.objects == {}


def test_names_are_reduced_to_a_plain_filename(s3):
    reply = sink()
    assert reply.accept(raw(image(), name="../../etc/x.png"))["name"] == "x.png"
    assert reply.accept(raw(image(), name="\x00"))["name"] == "image-2.png"


# ---- console chat: SSE → ledger → history → read-back --------------------------------


def chat_turn(client, monkeypatch, agent, data: bytes, session_id=SID):
    stub_runtime(monkeypatch, frame({"event": "delta", "text": "Here is the page"}) + frame(
        {"event": "attachment", "name": "page.png", "media_type": "image/png", "data": b64(data)},
    ))
    response = client.post(f"/api/chat/{agent.id}",
                           json={"prompt": "screenshot it", "session_id": session_id})
    assert response.status_code == 200
    return response


def test_console_chat_streams_metadata_persists_no_bytes_and_serves_the_image(s3, monkeypatch):
    agent = agent_row("byoc")
    client = TestClient(create_app())
    data = image()
    response = chat_turn(client, monkeypatch, agent, data)
    assert b64(data) not in response.text
    events = sse_events(response.text)
    kinds = [kind for kind, _ in events]
    assert kinds.index("attachment") > kinds.index("delta")
    meta = next(d for k, d in events if k == "attachment")
    assert meta["status"] == "stored" and meta["size_bytes"] == len(data)
    assert kinds.index("saved") < kinds.index("done")
    with SessionLocal() as db:
        answer = db.query(ChatMessage).filter_by(agent_id=agent.id, role="agent").one()
        assert answer.text == "Here is the page"
        assert answer.attachments == [{k: v for k, v in meta.items() if k != "url"}]
        assert b64(data) not in json.dumps(answer.attachments)
        assert "storage_key" not in answer.attachments[0]
    history = client.get(f"/api/chat/{agent.id}/history", params={"session_id": SID}).json()
    assert history["messages"][1]["attachments"][0]["id"] == meta["id"]
    got = client.get(meta["url"])
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-security-policy"] == "default-src 'none'"
    assert got.headers["content-disposition"].startswith('inline; filename="page.png"')


def test_an_attachment_only_answer_is_still_one_persisted_bubble(s3, monkeypatch):
    agent = agent_row()
    stub_runtime(monkeypatch, frame(
        {"event": "attachment", "name": "a.png", "media_type": "image/png",
         "data": b64(image())}))
    client = TestClient(create_app())
    response = client.post(f"/api/chat/{agent.id}", json={"prompt": "x", "session_id": SID})
    assert "saved" in [kind for kind, _ in sse_events(response.text)]
    with SessionLocal() as db:
        answer = db.query(ChatMessage).filter_by(agent_id=agent.id, role="agent").one()
        assert answer.text == "" and answer.attachments[0]["status"] == "stored"


def test_buffered_methods_relay_json_reply_attachments(s3, monkeypatch):
    agent = agent_row("studio")
    stub_runtime(monkeypatch, json.dumps({"result": "done", "attachments": [
        {"name": "c.webp", "media_type": "image/webp", "data": b64(image("WEBP"))},
        {"name": "fake.png", "media_type": "image/png", "data": b64(b"GIF89a nope")},
    ]}).encode(), content_type="application/json")
    response = TestClient(create_app()).post(f"/api/chat/{agent.id}",
                                             json={"prompt": "x", "session_id": SID})
    metas = [d for k, d in sse_events(response.text) if k == "attachment"]
    assert [m["status"] for m in metas] == ["stored", "dropped"]
    assert metas[0]["media_type"] == "image/webp" and metas[1]["reason_code"] == "invalid"


def test_forged_and_other_agents_ids_are_404(s3, monkeypatch):
    agent, other = agent_row(), agent_row("byoc")
    client = TestClient(create_app())
    response = chat_turn(client, monkeypatch, agent, image())
    meta = next(d for k, d in sse_events(response.text) if k == "attachment")
    assert client.get(f"/api/chat/{agent.id}/attachments/{'f' * 32}").status_code == 404
    wrong_agent = client.get(f"/api/chat/{other.id}/attachments/{meta['id']}")
    assert wrong_agent.status_code == 404
    assert wrong_agent.json()["code"] == "chat.attachment_not_found"


def test_another_principals_private_session_is_404(s3, monkeypatch):
    agent = agent_row()
    client = TestClient(create_app())
    meta = next(d for k, d in sse_events(chat_turn(client, monkeypatch, agent, image()).text)
                if k == "attachment")
    assert client.get(meta["url"]).status_code == 200
    with SessionLocal() as db:
        conv = AssistantConversation(workspace_id=DEFAULT_WORKSPACE_ID, owner="alice",
                                     owner_principal="user:alice")
        db.add(conv)
        db.flush()
        db.add(AssistantMessage(workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=conv.id,
                                turn=1, role="agent", text="", runtime_session_id=SID))
        db.commit()
    hidden = client.get(meta["url"])
    assert hidden.status_code == 404 and hidden.json()["code"] == "chat.attachment_not_found"


def test_another_workspace_cannot_read_it(s3, monkeypatch):
    agent = agent_row()
    client = TestClient(create_app())
    meta = next(d for k, d in sse_events(chat_turn(client, monkeypatch, agent, image()).text)
                if k == "attachment")
    with SessionLocal() as db:
        db.add(Workspace(id="otherws", name="Other", account_id="999988887777",
                         region="us-west-2"))
        db.commit()
    response = client.get(meta["url"], headers={"X-Workspace": "otherws"})
    assert response.status_code == 404
    # and a row of the other workspace is not reachable through the default one
    foreign = agent_row(workspace_id="otherws")
    with SessionLocal() as db:
        db.add(ChatOutputAttachment(
            id="e" * 32, workspace_id="otherws", agent_id=foreign.id, session_id=SID,
            owner="console", name="x.png", media_type="image/png", size_bytes=1,
            sha256="0" * 64, storage_key="chat-attachments/otherws/x.png"))
        db.commit()
    assert client.get(f"/api/chat/{foreign.id}/attachments/{'e' * 32}").status_code == 404


def test_an_expired_object_is_404(s3, monkeypatch):
    agent = agent_row()
    client = TestClient(create_app())
    meta = next(d for k, d in sse_events(chat_turn(client, monkeypatch, agent, image()).text)
                if k == "attachment")
    s3.objects.clear()  # the lifecycle rule expired it
    assert client.get(meta["url"]).status_code == 404


# ---- /v1 ----------------------------------------------------------------------------


def test_v1_sync_reply_carries_metadata_and_a_key_scoped_download_path(s3, monkeypatch):
    agent = agent_row()
    key = api_key()
    other = api_key(raw="another-key")
    data = image("JPEG")
    stub_runtime(monkeypatch, frame({"event": "delta", "text": "ok"}) + frame(
        {"event": "attachment", "name": "p.jpg", "media_type": "image/jpeg", "data": b64(data)}))
    client = TestClient(create_app())
    body = client.post(f"/v1/agents/{agent.id}/invoke", json={"prompt": "x"},
                       headers={"X-Api-Key": "reply-attachment-key"}).json()
    assert body["text"] == "ok"
    [meta] = body["attachments"]
    assert b64(data) not in json.dumps(body)
    assert meta["url"] == f"/v1/agents/{agent.id}/attachments/{meta['id']}"
    with SessionLocal() as db:
        assert db.get(ChatOutputAttachment, meta["id"]).owner == f"apikey:{key.id}"
    got = client.get(meta["url"], headers={"X-Api-Key": "reply-attachment-key"})
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"] == "image/jpeg"
    assert client.get(meta["url"], headers={"X-Api-Key": "another-key"}).status_code == 404
    assert other.id != key.id
    # a /v1 image is not readable through the console route, and vice versa
    assert client.get(f"/api/chat/{agent.id}/attachments/{meta['id']}").status_code == 404
    assert client.get(meta["url"]).status_code == 401


def test_v1_stream_emits_attachment_events_and_console_images_stay_console_only(
        s3, monkeypatch):
    agent = agent_row()
    api_key()
    client = TestClient(create_app())
    console_meta = next(d for k, d in sse_events(
        chat_turn(client, monkeypatch, agent, image()).text) if k == "attachment")
    response = client.post(f"/v1/agents/{agent.id}/invoke-stream", json={"prompt": "x"},
                           headers={"X-Api-Key": "reply-attachment-key"})
    meta = next(d for k, d in sse_events(response.text) if k == "attachment")
    assert meta["url"].startswith("/v1/") and meta["status"] == "stored"
    forged = client.get(f"/v1/agents/{agent.id}/attachments/{console_meta['id']}",
                        headers={"X-Api-Key": "reply-attachment-key"})
    assert forged.status_code == 404


def test_console_sync_invoke_relays_no_attachments(s3, monkeypatch):
    agent = agent_row()
    stub_runtime(monkeypatch, frame({"event": "delta", "text": "ok"}) + frame(
        {"event": "attachment", "name": "a.png", "media_type": "image/png",
         "data": b64(image())}))
    response = TestClient(create_app()).post(f"/api/agents/{agent.id}/invoke",
                                             json={"prompt": "x"})
    assert response.status_code == 200
    assert "reply_attachments" not in response.text and "attachments" not in response.json()
    assert s3.objects == {}
