"""Configured video directory: legacy import, publication, auth, and V2 taxonomy."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import Base, SessionLocal, init_db
from app.core.errors import AppError
from app.main import create_app
from app.models.video import Video, VideoCatalogSeed
from app.services import users, videos

CONTENT = {
    "category_id": "build",
    "section_id": "assistant",
    "title": {"zh-CN": "新视频"},
    "description": {"zh-CN": "演示架构助手的功能。"},
    "cdn_url": "https://cdn.example.com/media/intro.mp4",
}


def create(client, **changes):
    response = client.post("/api/videos/manage", json={**CONTENT, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def action(client, row, verb):
    response = client.post(
        f"/api/videos/manage/{row['id']}/{verb}",
        json={"expected_revision": row["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_existing_directory_imports_once_with_all_media_metadata(client):
    result = client.get("/api/videos")
    assert result.status_code == 200
    catalog = result.json()
    assert len(catalog["videos"]) == 16
    assert all(catalog["collections"])
    first = next(video for video in catalog["videos"] if video["id"] == "architect-assistant")
    assert len(first["chapters"]) == 11
    assert [source["type"] for source in first["sources"]] == ["video/mp4", "video/webm"]
    assert first["posterUrl"].endswith("/poster.jpg")
    collection = next(c for c in catalog["collections"] if c["id"] == "assistant")
    assert collection["categoryId"] == "build"
    assert collection["videoIds"] == ["architect-assistant", "architect-assistant-runtime-ab"]
    skill_lab = next(c for c in catalog["collections"] if c["id"] == "skill-lab")
    assert skill_lab["videoIds"] == ["skill-tasksets", "skill-evaluation", "skill-optimization"]
    with SessionLocal() as db:
        assert db.get(VideoCatalogSeed, 1) is not None


def test_draft_publish_edit_withdraw_and_delete(client):
    original_count = len(client.get("/api/videos").json()["videos"])
    row = create(client)
    assert row["status"] == "draft" and row["published_content"] is None
    assert row["content"]["title"] == {"en": "新视频", "zh-CN": "新视频"}
    assert len(client.get("/api/videos").json()["videos"]) == original_count
    row = action(client, row, "publish")
    public = next(v for v in client.get("/api/videos").json()["videos"] if v["id"] == row["id"])
    assert public["sources"] == [{"url": CONTENT["cdn_url"], "type": "video/mp4"}]
    assert public["title"]["zh-CN"] == "新视频"

    response = client.put(
        f"/api/videos/manage/{row['id']}",
        json={**row["content"], "title": {"zh-CN": "修改后"}, "expected_revision": row["revision"]},
    )
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["has_unpublished_changes"]
    public = next(v for v in client.get("/api/videos").json()["videos"] if v["id"] == row["id"])
    assert public["title"]["zh-CN"] == "新视频"
    row = action(client, row, "publish")
    public = next(v for v in client.get("/api/videos").json()["videos"] if v["id"] == row["id"])
    assert public["title"]["zh-CN"] == "修改后"
    row = action(client, row, "unpublish")
    assert row["status"] == "draft" and row["content"]["title"]["zh-CN"] == "修改后"
    assert all(v["id"] != row["id"] for v in client.get("/api/videos").json()["videos"])
    response = client.delete(
        f"/api/videos/manage/{row['id']}", params={"expected_revision": row["revision"]},
    )
    assert response.json() == {"deleted": True}
    assert client.get(f"/api/videos/manage/{row['id']}").status_code == 404


def test_stale_revisions_cannot_change_a_published_snapshot(client):
    row = create(client)
    latest = action(client, row, "publish")
    base = f"/api/videos/manage/{row['id']}"
    responses = [
        client.put(base, json={**latest["content"], "expected_revision": row["revision"]}),
        client.post(f"{base}/publish", json={"expected_revision": row["revision"]}),
        client.post(f"{base}/unpublish", json={"expected_revision": row["revision"]}),
        client.delete(base, params={"expected_revision": row["revision"]}),
    ]
    assert all(response.status_code == 409 for response in responses)
    assert all(response.json()["code"] == "videos.conflict" for response in responses)
    assert client.get(base).json() == latest


def test_competing_db_sessions_use_a_revision_predicate(client):
    row = create(client)
    with SessionLocal() as first, SessionLocal() as second:
        stale = second.get(Video, row["id"])
        videos.change(first, row["id"], row["revision"], "first", action="publish")
        assert stale.revision == row["revision"]
        with pytest.raises(AppError) as caught:
            videos.change(second, row["id"], row["revision"], "second", action="unpublish")
        assert caught.value.code == "videos.conflict"


@pytest.mark.parametrize("patch", [
    {"category_id": ""}, {"section_id": ""}, {"section_id": "memory"},
    {"title": {"zh-CN": ""}}, {"description": {"zh-CN": ""}},
    {"cdn_url": ""}, {"cdn_url": "http://cdn.example.com/a.mp4"},
    {"cdn_url": "https://cdn.example.com/a.mp4?token=temporary"},
    {"cdn_url": "https://cdn.example.com/a.txt"},
    {"cdn_url": "https://localhost/a.mp4"},
    {"webm_url": "https://cdn.example.com/other.mp4"},
    {"duration_seconds": 0, "chapters": [{"startSeconds": 0, "title": {"zh-CN": "开始"}}]},
    {"published_content": CONTENT},
])
def test_invalid_fields_do_not_create_a_draft(client, patch):
    before = len(client.get("/api/videos/manage").json()["videos"])
    response = client.post("/api/videos/manage", json={**CONTENT, **patch})
    assert response.status_code == 422
    assert len(client.get("/api/videos/manage").json()["videos"]) == before


def test_taxonomy_paths_match_v2_navigation():
    root = Path(__file__).resolve().parents[2]
    nav = (root / "frontend" / "src" / "v2" / "nav.ts").read_text(encoding="utf-8")
    nav_paths = set(re.findall(r'to: "(/v2[^"]*)"', nav))
    sections = videos.taxonomy()["sections"]
    assert {section["path"] for section in sections} == nav_paths


def test_existing_ledger_upgrade_and_intentional_empty_catalog(tmp_path):
    bind = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(
        bind, tables=[table for name, table in Base.metadata.tables.items()
                      if name not in {"videos", "video_catalog_seed"}],
    )
    init_db(bind)
    with sa.orm.Session(bind) as db:
        assert db.query(Video).count() == 16
        db.query(Video).delete()
        db.commit()
    init_db(bind)
    with sa.orm.Session(bind) as db:
        assert db.query(Video).count() == 0
        assert db.get(VideoCatalogSeed, 1) is not None
    bind.dispose()


def test_admin_management_and_member_public_read_are_hub_global(monkeypatch):
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
                "username": "video-reader", "password": "member-password",
                "email": "video-reader@acme-corp.com",
            }).status_code == 201
            with SessionLocal() as db:
                account = users.find_by_username(db, "video-reader")
                account.status = "active"
                account.expires_at = datetime.now(UTC) + timedelta(days=1)
                db.commit()
            assert member.post("/api/auth/login", json={
                "username": "video-reader", "password": "member-password",
            }).status_code == 200
            row = create(admin, title={"zh-CN": "未发布的秘密"})
            public = member.get("/api/videos", headers={"X-Workspace": "not-a-workspace"})
            assert public.status_code == 200
            assert all(video["id"] != row["id"] for video in public.json()["videos"])
            assert anon.get("/api/videos").status_code == 401
            base = f"/api/videos/manage/{row['id']}"
            for blocked, status in ((member, 403), (anon, 401)):
                attempts = [
                    blocked.get("/api/videos/manage"),
                    blocked.get(base),
                    blocked.post("/api/videos/manage", json=CONTENT),
                    blocked.put(
                        base, json={**row["content"], "expected_revision": row["revision"]},
                    ),
                    blocked.post(f"{base}/publish", json={"expected_revision": row["revision"]}),
                    blocked.post(f"{base}/unpublish", json={"expected_revision": row["revision"]}),
                    blocked.delete(base, params={"expected_revision": row["revision"]}),
                ]
                assert all(response.status_code == status for response in attempts)
    finally:
        monkeypatch.delenv("LAUNCHPAD_AUTH_PASSWORD", raising=False)
        monkeypatch.delenv("LAUNCHPAD_AUTH_USERNAME", raising=False)
        get_settings.cache_clear()
