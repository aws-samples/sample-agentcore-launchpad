"""Announcement publication, role boundaries, and persistence (no AWS calls)."""

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import Base, SessionLocal, init_db
from app.core.errors import AppError
from app.main import create_app
from app.models.announcement import Announcement
from app.services import announcements, users

CONTENT = {
    "title": "视频库上线",
    "body": "在 Launchpad 内观看架构助手介绍。",
    "link_url": "/videos",
    "link_label": "观看视频",
}


def create(client, **changes):
    response = client.post("/api/announcements", json={**CONTENT, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def action(client, row, verb):
    response = client.post(
        f"/api/announcements/{row['id']}/{verb}",
        json={"expected_revision": row["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_draft_publication_edit_withdraw_and_delete(client):
    row = create(client)
    assert row["status"] == "draft" and row["published_content"] is None
    assert client.get("/api/announcements").json() == {"announcements": [], "total": 0}
    assert client.get("/api/announcements/manage").json()["total"] == 1
    row = action(client, row, "publish")
    assert row["status"] == "published" and not row["has_unpublished_changes"]
    public = client.get("/api/announcements").json()["announcements"][0]
    assert public["title"] == CONTENT["title"]
    assert set(public) == {"id", "title", "body", "link_url", "link_label", "published_at"}

    response = client.put(
        f"/api/announcements/{row['id']}",
        json={**CONTENT, "title": "Unpublished edit", "expected_revision": row["revision"]},
    )
    assert response.status_code == 200
    row = response.json()
    assert row["has_unpublished_changes"]
    assert client.get("/api/announcements").json()["announcements"][0]["title"] == CONTENT["title"]
    row = action(client, row, "publish")
    public = client.get("/api/announcements").json()["announcements"][0]
    assert public["title"] == "Unpublished edit"
    row = action(client, row, "unpublish")
    assert client.get("/api/announcements").json()["total"] == 0
    assert row["content"]["title"] == "Unpublished edit"
    row = action(client, row, "publish")
    response = client.delete(
        f"/api/announcements/{row['id']}", params={"expected_revision": row["revision"]},
    )
    assert response.json() == {"deleted": True}
    assert client.get("/api/announcements").json()["total"] == 0
    assert client.get(f"/api/announcements/{row['id']}").status_code == 404


def test_stale_writes_never_replace_or_publish_newer_content(client):
    row = create(client)
    latest = action(client, row, "publish")
    base = f"/api/announcements/{row['id']}"
    responses = [
        client.put(base, json={**CONTENT, "title": "stale", "expected_revision": row["revision"]}),
        client.post(f"{base}/publish", json={"expected_revision": row["revision"]}),
        client.post(f"{base}/unpublish", json={"expected_revision": row["revision"]}),
        client.delete(base, params={"expected_revision": row["revision"]}),
    ]
    for response in responses:
        assert response.status_code == 409
        assert response.json()["code"] == "announcements.conflict"
    assert client.get(base).json() == latest


@pytest.mark.parametrize("operation", ["unpublish", "delete"])
def test_database_revision_guard_catches_racing_sessions(client, operation):
    row = create(client)
    with SessionLocal() as first, SessionLocal() as second:
        stale = second.get(Announcement, row["id"])
        announcements.change(first, row["id"], row["revision"], "first", action="publish")
        assert stale.revision == row["revision"]
        with pytest.raises(AppError) as caught:
            if operation == "delete":
                announcements.delete(second, row["id"], row["revision"])
            else:
                announcements.change(
                    second, row["id"], row["revision"], "second", action=operation,
                )
        assert caught.value.code == "announcements.conflict"
    assert client.get("/api/announcements").json()["total"] == 1


@pytest.mark.parametrize("link", [
    "javascript:alert(1)", "data:text/html,hello", "http://example.com",
    "//example.com", "/\\example.com", "https://user:password@example.com",
    "https://example.com/\nx", "https://example.com:invalid", "https:///missing-host",
    "/%2fexample.com", "/%2Fexample.com", "/%5cexample.com", "/%5Cexample.com",
    "/videos%00", "/videos%09", "/videos%0a", "/videos%0d", "/videos%7f",
    "https://example.com/%5cpath", "https://example.com/%0Apath",
    "/videos\x7f", "/videos%", "/videos%2", "/videos%xx", "/videos%ff",
    "https://exa%23mple.com", "https://exa^mple.com", "https://256.256.256.256",
])
def test_unsafe_links_rejected(client, link):
    response = client.post("/api/announcements", json={**CONTENT, "link_url": link})
    assert response.status_code == 422
    assert client.get("/api/announcements/manage").json()["total"] == 0


@pytest.mark.parametrize("link", [
    "/videos?video=architect-assistant",
    "https://launchpad.jugglehub.top",
    "https://catalog.us-east-1.prod.workshops.aws/workshops/example",
    "/videos?search=hello%20world",
    "/videos?search=%E8%A7%86%E9%A2%91",
    "HTTPS://example.com/path%20name",
    "https://例子.测试/path",
])
def test_safe_links_and_plain_text_preserved(client, link):
    row = create(client, title="<script>plain text</script>", link_url=link)
    assert row["content"]["link_url"] == link
    assert row["content"]["title"] == "<script>plain text</script>"


@pytest.mark.parametrize("patch", [
    {"title": "   "}, {"body": " "}, {"title": "x" * 161}, {"body": "x" * 6001},
    {"link_url": None}, {"link_label": None}, {"published_content": CONTENT},
    {"created_by": "somebody-else"},
])
def test_invalid_or_server_owned_content_rejected(client, patch):
    assert client.post("/api/announcements", json={**CONTENT, **patch}).status_code == 422


def test_published_pagination_excludes_drafts(client):
    one = action(client, create(client, title="one"), "publish")
    two = action(client, create(client, title="two"), "publish")
    create(client, title="draft")
    first = client.get("/api/announcements?limit=1").json()
    second = client.get("/api/announcements?limit=1&offset=1").json()
    assert first["total"] == second["total"] == 2
    assert first["announcements"][0]["id"] == two["id"]
    assert second["announcements"][0]["id"] == one["id"]
    assert client.get("/api/announcements?limit=0").status_code == 422
    assert client.get("/api/announcements/manage?offset=-1").status_code == 422


def test_upgrade_creates_table_without_seeding_and_preserves_saved_data(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(
        engine, tables=[table for name, table in Base.metadata.tables.items()
                        if name != "announcements"],
    )
    assert "announcements" not in sa.inspect(engine).get_table_names()
    init_db(engine)
    with sa.orm.Session(engine) as db:
        assert announcements.list_announcements(db, admin=True, limit=20, offset=0)["total"] == 0
        row = announcements.create(db, CONTENT, "operator")
        announcements.change(db, row["id"], row["revision"], "operator", action="publish")
    init_db(engine)
    with sa.orm.Session(engine) as db:
        result = announcements.list_announcements(db, admin=False, limit=20, offset=0)
        assert result["total"] == 1
        assert result["announcements"][0]["title"] == CONTENT["title"]
    engine.dispose()


def test_authenticated_roles_and_workspace_independence(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "admin-password")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as admin, TestClient(app) as member, TestClient(app) as anon:
            assert admin.post("/api/auth/login", json={
                "username": "operator", "password": "admin-password",
            }).status_code == 200
            assert member.post("/api/auth/register", json={
                "username": "notice-reader", "password": "member-password",
                "email": "notice-reader@acme-corp.com",
            }).status_code == 201
            with SessionLocal() as db:
                account = users.find_by_username(db, "notice-reader")
                account.status = "active"
                account.expires_at = datetime.now(UTC) + timedelta(days=1)
                db.commit()
            assert member.post("/api/auth/login", json={
                "username": "notice-reader", "password": "member-password",
            }).status_code == 200
            row = action(admin, create(admin), "publish")
            create(admin, title="secret draft")
            # No grants, and an invalid workspace header: notices are hub-global.
            public = member.get("/api/announcements", headers={"X-Workspace": "not-a-workspace"})
            assert public.status_code == 200
            assert public.json()["total"] == 1
            assert "secret draft" not in public.text
            assert anon.get("/api/announcements").status_code == 401
            base = f"/api/announcements/{row['id']}"
            for client, status in [(member, 403), (anon, 401)]:
                attempts = [
                    client.get("/api/announcements/manage"),
                    client.get(base),
                    client.post("/api/announcements", json=CONTENT),
                    client.put(base, json={**CONTENT, "expected_revision": row["revision"]}),
                    client.post(f"{base}/publish", json={"expected_revision": row["revision"]}),
                    client.post(f"{base}/unpublish", json={"expected_revision": row["revision"]}),
                    client.delete(base, params={"expected_revision": row["revision"]}),
                ]
                assert all(response.status_code == status for response in attempts)
            assert admin.get("/api/announcements/manage").json()["total"] == 2
    finally:
        get_settings.cache_clear()


@pytest.fixture
def run_publisher(client, monkeypatch, tmp_path):
    """Exercise the explicit publisher against the hermetic API, never a live socket."""
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    publisher = importlib.import_module("publish_announcements")
    document = tmp_path / "notices.json"
    opened_clients = []

    def authenticated_client(*args, **kwargs):
        http = TestClient(client.app)
        # The real helper has already used the client before returning it.
        assert http.get("/api/auth/status").status_code == 200
        opened_clients.append(http)
        return http

    monkeypatch.setattr(publisher, "e2e_client", authenticated_client)

    def run(contents, *, apply=False):
        document.write_text(json.dumps(contents))
        argv = ["publish_announcements.py", "--file", str(document)]
        if apply:
            argv.append("--apply")
        monkeypatch.setattr(sys, "argv", argv)
        try:
            publisher.main()
        finally:
            assert all(http.is_closed for http in opened_clients)

    return run


def test_publisher_preview_has_no_content_writes(client, run_publisher, capsys):
    draft = create(client)
    run_publisher([CONTENT, {**CONTENT, "title": "Another notice"}])
    assert capsys.readouterr().out.count('"would-publish"') == 2
    assert client.get("/api/announcements/manage").json()["announcements"] == [draft]
    assert client.get("/api/announcements").json()["total"] == 0


def test_publisher_apply_resumes_draft_and_preserves_later_edits(client, run_publisher, capsys):
    draft = create(client)
    contents = [CONTENT, {**CONTENT, "title": "Another notice"}]
    run_publisher(contents, apply=True)
    first = client.get("/api/announcements/manage").json()
    assert first["total"] == 2
    assert client.get(f"/api/announcements/{draft['id']}").json()["status"] == "published"
    row = client.get(f"/api/announcements/{draft['id']}").json()
    assert client.put(f"/api/announcements/{row['id']}", json={
        **CONTENT, "body": "Unpublished operator edit", "expected_revision": row["revision"],
    }).status_code == 200
    edited = client.get("/api/announcements/manage").json()
    capsys.readouterr()
    run_publisher(contents, apply=True)
    assert capsys.readouterr().out.count('"unchanged"') == 2
    assert client.get("/api/announcements/manage").json() == edited
    assert client.get("/api/announcements").json()["total"] == 2


@pytest.mark.parametrize("ambiguous", [False, True])
def test_publisher_refuses_edited_or_ambiguous_rows_before_any_write(
    client, run_publisher, ambiguous,
):
    create(client, body="Operator's own content")
    if ambiguous:
        create(client)
    before = client.get("/api/announcements/manage").json()
    message = "Ambiguous existing title" if ambiguous else "Existing notice was edited"
    with pytest.raises(SystemExit, match=message):
        run_publisher([{**CONTENT, "title": "Do not create this"}, CONTENT], apply=True)
    assert client.get("/api/announcements/manage").json() == before
    assert client.get("/api/announcements").json()["total"] == 0
