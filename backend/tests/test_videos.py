"""Configured video directory (CN port): empty one-time import, publication, auth, V2 taxonomy.

The CN build owns no media stack: every media field is a permanent HTTPS CDN URL the
administrator enters, and the bundled `videos.initial.json` ships empty (the Global
recordings show the Global console). The import mechanism itself is kept and exercised
against a temporary manifest.
"""

import json
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

_MEDIA = "https://cdn.example.com/media/agents-tour/20261001-v2"
MANIFEST_ENTRY = {
    "id": "agents-tour",
    "sectionId": "agents",
    "title": {"en": "Agent management tour", "zh-CN": "Agent 管理导览"},
    "description": {"en": "Create, deploy and chat.", "zh-CN": "创建、部署并对话。"},
    "publishedAt": "2026-10-01",
    "durationSeconds": 120,
    "language": "zh-CN",
    "posterUrl": f"{_MEDIA}/poster.jpg",
    "sources": [
        {"url": f"{_MEDIA}/tour.mp4", "type": "video/mp4"},
        {"url": f"{_MEDIA}/tour.webm", "type": "video/webm"},
    ],
    "captions": [{"url": f"{_MEDIA}/zh.vtt", "language": "zh-CN", "label": "简体中文"}],
    "chapters": [{"startSeconds": 0, "title": {"en": "Intro", "zh-CN": "开场"}},
                 {"startSeconds": 60, "title": {"en": "Deploy", "zh-CN": "部署"}}],
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


def test_bundled_manifest_is_empty_and_the_import_marker_is_still_written(client):
    """CN: nothing is seeded (no Global recordings), but the one-time marker exists so an
    intentionally emptied directory stays empty on the next restart."""
    manifest = json.loads(videos._LEGACY_PATH.read_text(encoding="utf-8"))
    assert manifest == {"schemaVersion": 1, "categories": [], "collections": [], "videos": []}
    result = client.get("/api/videos")
    assert result.status_code == 200
    assert result.json() == {"schemaVersion": 3, "categories": [], "collections": [], "videos": []}
    assert client.get("/api/videos/manage").json()["videos"] == []
    with SessionLocal() as db:
        assert db.get(VideoCatalogSeed, 1) is not None


def test_manifest_import_mechanism_still_works_once(tmp_path):
    """The import path is kept for an operator who seeds a fresh ledger from a manifest:
    entries name their V2 section with `sectionId`; media and chapter metadata survive;
    a later `init_db` after the administrator removed everything imports nothing again."""
    manifest = tmp_path / "videos.initial.json"
    manifest.write_text(json.dumps({
        "schemaVersion": 1, "categories": [], "videos": [MANIFEST_ENTRY],
        "collections": [{"id": "agents", "videoIds": ["agents-tour"]}],
    }), encoding="utf-8")
    bind = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind)
    videos.seed_legacy_catalog(bind, manifest)
    with sa.orm.Session(bind) as db:
        row = db.get(Video, "agents-tour")
        assert row is not None and row.created_by == "catalog-import"
        content = row.published_content
        assert content == row.content
        assert content["section_id"] == "agents" and content["category_id"] == "build"
        assert content["cdn_url"] == f"{_MEDIA}/tour.mp4"
        assert content["webm_url"] == f"{_MEDIA}/tour.webm"
        assert content["poster_url"].endswith("/poster.jpg")
        assert content["caption_url"].endswith("/zh.vtt")
        assert content["console_version"] == "v2"  # the immutable `YYYYMMDD-v2` revision
        assert [c["startSeconds"] for c in content["chapters"]] == [0, 60]
        # SQLite hands the stored instant back naive
        assert row.published_at.replace(tzinfo=None) == datetime(2026, 10, 1)
        assert db.get(VideoCatalogSeed, 1) is not None
        db.query(Video).delete()
        db.commit()
    videos.seed_legacy_catalog(bind, manifest)
    with sa.orm.Session(bind) as db:
        assert db.query(Video).count() == 0
    bind.dispose()


def test_manifest_entry_without_a_section_is_refused(tmp_path):
    manifest = tmp_path / "videos.initial.json"
    entry = {key: value for key, value in MANIFEST_ENTRY.items() if key != "sectionId"}
    manifest.write_text(json.dumps({"schemaVersion": 1, "collections": [], "videos": [entry]}),
                        encoding="utf-8")
    bind = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind)
    with pytest.raises(RuntimeError, match="sectionId"):
        videos.seed_legacy_catalog(bind, manifest)
    with sa.orm.Session(bind) as db:
        assert db.query(Video).count() == 0 and db.get(VideoCatalogSeed, 1) is None
    bind.dispose()


def test_draft_publish_edit_withdraw_and_delete(client):
    original_count = len(client.get("/api/videos").json()["videos"])
    row = create(client)
    assert row["status"] == "draft" and row["published_content"] is None
    assert row["content"]["console_version"] == "classic"
    assert row["content"]["title"] == {"en": "新视频", "zh-CN": "新视频"}
    assert len(client.get("/api/videos").json()["videos"]) == original_count
    row = action(client, row, "publish")
    catalog = client.get("/api/videos").json()
    public = next(v for v in catalog["videos"] if v["id"] == row["id"])
    assert public["sources"] == [{"url": CONTENT["cdn_url"], "type": "video/mp4"}]
    assert public["title"]["zh-CN"] == "新视频"
    assert public["consoleVersion"] == "classic"
    # the console's per-module demo button joins on the V2 sidebar path
    collection = next(c for c in catalog["collections"] if c["id"] == "assistant")
    assert collection["categoryId"] == "build" and collection["path"] == "/v2/assistant"
    assert collection["videoIds"] == [row["id"]]
    assert [c["id"] for c in catalog["categories"]] == ["build"]

    response = client.put(
        f"/api/videos/manage/{row['id']}",
        json={
            **row["content"], "title": {"zh-CN": "修改后"},
            "console_version": "v2", "expected_revision": row["revision"],
        },
    )
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["has_unpublished_changes"]
    public = next(v for v in client.get("/api/videos").json()["videos"] if v["id"] == row["id"])
    assert public["title"]["zh-CN"] == "新视频"
    assert public["consoleVersion"] == "classic"
    row = action(client, row, "publish")
    public = next(v for v in client.get("/api/videos").json()["videos"] if v["id"] == row["id"])
    assert public["title"]["zh-CN"] == "修改后"
    assert public["consoleVersion"] == "v2"
    row = action(client, row, "unpublish")
    assert row["status"] == "draft" and row["content"]["title"]["zh-CN"] == "修改后"
    assert all(v["id"] != row["id"] for v in client.get("/api/videos").json()["videos"])
    response = client.delete(
        f"/api/videos/manage/{row['id']}", params={"expected_revision": row["revision"]},
    )
    assert response.json() == {"deleted": True}
    assert client.get(f"/api/videos/manage/{row['id']}").status_code == 404


def test_published_catalog_orders_by_area_module_and_sort_order(client):
    later = create(client, section_id="agents", sort_order=20, title={"zh-CN": "后"})
    earlier = create(client, section_id="agents", sort_order=10, title={"zh-CN": "前"})
    evaluation = create(client, category_id="eval", section_id="eval-tasks")
    for row in (later, earlier, evaluation):
        action(client, row, "publish")
    catalog = client.get("/api/videos").json()
    assert [c["id"] for c in catalog["categories"]] == ["build", "eval"]
    assert [c["id"] for c in catalog["collections"]] == ["agents", "eval-tasks"]
    agents = next(c for c in catalog["collections"] if c["id"] == "agents")
    assert agents["videoIds"] == [earlier["id"], later["id"]]
    assert agents["description"]["zh-CN"] == CONTENT["description"]["zh-CN"]


def test_old_ledger_snapshots_are_classified_on_read_without_rewriting(client):
    row = create(client, cdn_url="https://cdn.example.com/media/demo/20260926-v2/demo.mp4")
    row = action(client, row, "publish")
    with SessionLocal() as db:
        stored = db.get(Video, row["id"])
        stored.content = {key: value for key, value in stored.content.items()
                          if key != "console_version"}
        stored.published_content = {key: value for key, value in stored.published_content.items()
                                    if key != "console_version"}
        db.commit()
        revision = stored.revision
    managed = client.get(f"/api/videos/manage/{row['id']}").json()
    public = next(v for v in client.get("/api/videos").json()["videos"] if v["id"] == row["id"])
    assert managed["content"]["console_version"] == "v2"
    assert managed["published_content"]["console_version"] == "v2"
    assert not managed["has_unpublished_changes"]
    assert public["consoleVersion"] == "v2"
    with SessionLocal() as db:
        stored = db.get(Video, row["id"])
        assert stored.revision == revision
        assert "console_version" not in stored.content
        assert "console_version" not in stored.published_content


def test_a_retired_section_is_remapped_on_read_without_rewriting(client):
    """R13 (N13-obs5): `my-connections` left the taxonomy when the page became the
    `?view=mine` tab of Connections. A snapshot filed under it is listed under
    `connections` (and its area) on read; the stored row is not touched."""
    row = create(client, cdn_url="https://cdn.example.com/media/demo/20260926-v2/demo.mp4")
    row = action(client, row, "publish")
    with SessionLocal() as db:
        stored = db.get(Video, row["id"])
        stored.content = {**stored.content, "section_id": "my-connections", "category_id": "run"}
        stored.published_content = {**stored.published_content, "section_id": "my-connections",
                                    "category_id": "run"}
        db.commit()
        revision = stored.revision
    catalog = client.get("/api/videos").json()
    collection = next(c for c in catalog["collections"] if row["id"] in c["videoIds"])
    assert collection["id"] == "connections" and collection["categoryId"] == "admin"
    assert collection["path"] == "/v2/connections"
    assert not any(c["id"] == "my-connections" for c in catalog["collections"])
    managed = client.get(f"/api/videos/manage/{row['id']}").json()
    assert managed["published_content"]["section_id"] == "connections"
    assert managed["published_content"]["category_id"] == "admin"
    with SessionLocal() as db:
        stored = db.get(Video, row["id"])
        assert stored.revision == revision
        assert stored.published_content["section_id"] == "my-connections"


def test_skill_lab_is_an_evaluation_module_and_old_area_snapshots_follow_it(client):
    experiment = action(client, create(client, category_id="eval", section_id="eval-experiments"),
                        "publish")
    row = create(client, category_id="eval", section_id="skill-lab")
    row = action(client, row, "publish")
    skill_lab = next(c for c in client.get("/api/videos").json()["collections"]
                     if c["id"] == "skill-lab")
    assert skill_lab["categoryId"] == "eval"
    with SessionLocal() as db:
        stored = db.get(Video, row["id"])
        # Saved while Skill Lab still sat under Agent development.
        stored.content = {**stored.content, "category_id": "build"}
        stored.published_content = {**stored.published_content, "category_id": "build"}
        db.commit()
    managed = client.get(f"/api/videos/manage/{row['id']}").json()
    assert managed["content"]["category_id"] == "eval"
    assert managed["published_content"]["category_id"] == "eval"
    order = [video["id"] for video in client.get("/api/videos/manage").json()["videos"]]
    assert order.index(row["id"]) > order.index(experiment["id"])
    row = action(client, managed, "publish")
    assert row["content"]["category_id"] == "eval"
    with SessionLocal() as db:
        assert db.get(Video, row["id"]).published_content["category_id"] == "eval"
    rejected = client.post("/api/videos/manage", json={**CONTENT, "section_id": "skill-lab"})
    assert rejected.status_code == 422


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
    {"cdn_url": "https://10.0.0.8/a.mp4"},
    {"console_version": "v3"},
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
    """`video_sections.json` is the directory source: every sidebar module is a section, and
    every section is a routed V2 page (some, like announcements, are reached from the
    overview rather than the sidebar)."""
    root = Path(__file__).resolve().parents[2]
    nav = (root / "frontend" / "src" / "v2" / "nav.ts").read_text(encoding="utf-8")
    nav_paths = set(re.findall(r'to: "(/v2[^"]*)"', nav))
    app = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    v2_block = app.split('path="v2"', 1)[1].split("<Route element={<ConsoleShell />}>", 1)[0]
    routed = {"/v2"} | {f"/v2/{p}" for p in re.findall(r'<Route path="([^"*]+)"', v2_block)}
    sections = videos.taxonomy()["sections"]
    section_paths = {section["path"] for section in sections}
    assert nav_paths <= section_paths, sorted(nav_paths - section_paths)
    assert section_paths <= routed, sorted(section_paths - routed)
    # one sidebar entry since N15-obs14: management is the library's `?view=manage` tab and
    # `/v2/video-management` stays routed only as a redirect (the section keeps that path)
    assert "/v2/videos" in nav_paths
    assert "/v2/video-management" not in nav_paths


def test_existing_ledger_upgrade_and_intentional_empty_catalog(tmp_path):
    """A ledger created before the video tables existed gains them on `init_db`; with the
    CN manifest nothing is imported, and the marker makes the empty directory sticky."""
    bind = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(
        bind, tables=[table for name, table in Base.metadata.tables.items()
                      if name not in {"videos", "video_catalog_seed"}],
    )
    init_db(bind)
    with sa.orm.Session(bind) as db:
        assert db.query(Video).count() == 0
        assert db.get(VideoCatalogSeed, 1) is not None
        db.add(Video(content={}, created_by="t", updated_by="t"))
        db.commit()
    init_db(bind)
    with sa.orm.Session(bind) as db:
        assert db.query(Video).count() == 1  # a restart never touches administrator rows
        db.query(Video).delete()
        db.commit()
    init_db(bind)
    with sa.orm.Session(bind) as db:
        assert db.query(Video).count() == 0
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


def test_videos_feature_flag_toggles_the_capability(monkeypatch):
    """`videos` is a capability both navigations read; a deployment can switch it off."""
    monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(["agents", "chat"]))
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            assert "videos" not in client.get("/api/capabilities").json()["features"]
    finally:
        monkeypatch.delenv("LAUNCHPAD_FEATURES", raising=False)
        get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert "videos" in client.get("/api/capabilities").json()["features"]


# N13-obs11 (r13-e): recordings are self-hosted on the console host — nginx serves
# /home/ubuntu/launchpad-media as /media/ on the console port, so the stored URL is the
# console's own origin: a public IPv4 host with an explicit port and the revision path
# `/media/<section>/<YYYYMMDD-v2-cn-hr>/<slug>.zh-CN.<ext>`. The validator must accept that
# form for every media field (and classify the revision as a V2 recording) while it keeps
# refusing private / loopback hosts.
_HOST_MEDIA = "https://1.1.1.1:8443/media/assistant/20261017-v2-cn-hr"


def test_self_hosted_media_urls_with_a_public_ip_and_explicit_port_are_accepted(client):
    row = create(
        client,
        cdn_url=f"{_HOST_MEDIA}/first-agent.zh-CN.mp4",
        webm_url=f"{_HOST_MEDIA}/first-agent.zh-CN.webm",
        poster_url=f"{_HOST_MEDIA}/first-agent.jpg",
        caption_url=f"{_HOST_MEDIA}/first-agent.zh-CN.vtt",
    )
    content = row["content"]
    assert content["cdn_url"] == f"{_HOST_MEDIA}/first-agent.zh-CN.mp4"
    assert content["webm_url"] == f"{_HOST_MEDIA}/first-agent.zh-CN.webm"
    assert content["poster_url"] == f"{_HOST_MEDIA}/first-agent.jpg"
    assert content["caption_url"] == f"{_HOST_MEDIA}/first-agent.zh-CN.vtt"
    assert content["console_version"] == "v2"            # `20261017-v2-cn-hr` is a V2 revision
    published = action(client, row, "publish")
    [public] = [v for v in client.get("/api/videos").json()["videos"] if v["id"] == row["id"]]
    assert published["published_content"]["cdn_url"] == content["cdn_url"]
    assert [s["type"] for s in public["sources"]] == ["video/mp4", "video/webm"]
    assert public["posterUrl"] == f"{_HOST_MEDIA}/first-agent.jpg"
    assert public["captions"][0]["url"] == f"{_HOST_MEDIA}/first-agent.zh-CN.vtt"


@pytest.mark.parametrize("host", [
    "1.1.1.1:8443",      # public IPv4 + explicit console port (the user's exact form)
    "1.1.1.1",           # public IPv4, default port
    "console.example.cn:8443",  # a hostname with an explicit port
])
def test_url_validator_accepts_public_hosts_with_explicit_ports(host):
    url = f"https://{host}/media/agents/20261017-v2-cn-hr/tour.zh-CN.mp4"
    assert videos._url(url, ".mp4") == url
    assert videos._console_version({"cdn_url": url}) == "v2"


@pytest.mark.parametrize("host", ["10.0.0.1:8443", "127.0.0.1:8443", "localhost:8443",
                                  "169.254.169.254:8443", "[::1]:8443"])
def test_url_validator_still_refuses_private_hosts_with_explicit_ports(host):
    with pytest.raises(ValueError, match="public host"):
        videos._url(f"https://{host}/media/agents/20261017-v2-cn-hr/tour.zh-CN.mp4", ".mp4")
