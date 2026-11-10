"""`AttachmentBodyCap`'s path regex (N6 Task 3 fix round 1): the knowledge-base
upload entrance is a member-writable, request-body-sized endpoint too and must sit
behind the same ASGI-level cap as chat/invoke, not just `MAX_FILE_BYTES` validated
deep inside the route handler."""

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.attachment_body import _PATH, AttachmentBodyCap


def test_path_regex_matches_the_knowledge_base_upload_entrance():
    assert _PATH.fullmatch("/api/knowledge-bases/KB1A2B3C4D5E/files")
    assert _PATH.fullmatch("/api/knowledge-bases/KB1A2B3C4D5E/files/")


def test_path_regex_still_matches_the_pre_existing_entrances():
    assert _PATH.fullmatch("/api/chat/abc123")
    assert _PATH.fullmatch("/api/agents/abc123/invoke")
    assert _PATH.fullmatch("/api/agents/abc123/invoke-stream")
    assert _PATH.fullmatch("/v1/agents/abc123/invoke")


def test_path_regex_does_not_match_other_knowledge_base_routes():
    # Only the upload entrance carries member-controlled file bytes — every other
    # KB route (list, detail, query, …) must stay outside the cap.
    assert not _PATH.fullmatch("/api/knowledge-bases")
    assert not _PATH.fullmatch("/api/knowledge-bases/KB1A2B3C4D5E")
    assert not _PATH.fullmatch("/api/knowledge-bases/KB1A2B3C4D5E/query")


def test_streamed_body_cap_applies_to_the_knowledge_base_upload_route():
    """Mirrors `test_chat_attachments.py`'s
    `test_streamed_body_cap_does_not_depend_on_content_length`: the cap trips on
    bytes actually received, not on a (possibly absent/lied-about) Content-Length
    header, and it must apply to the KB upload path exactly like it does to chat."""
    app = create_app()
    app.add_middleware(AttachmentBodyCap, max_bytes=50)
    response = TestClient(app).post(
        "/api/knowledge-bases/KB1A2B3C4D5E/files",
        content=iter([b"--x\r\n", b"y" * 80, b"\r\n--x--\r\n"]),
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "chat.attachment_too_large"
