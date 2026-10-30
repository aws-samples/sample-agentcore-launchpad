"""Knowledge-base mutations are the creator's or an administrator's (N17-obs530).

Workshop dry run 2026-10-29: a service-desk member opened the detail page of a KB an
administrator had created (visible to the whole desk under `isolated`) and saw — and could
use — 删除 / 新增数据源 / 全量重建 / 移除. `routers/knowledge._require_owner` now applies the
`services/ownership.py` rule (users-and-ownership.md §8) to every KB mutation, after the
visibility 404 and before the force / attached checks. The incremental sync and the retrieval
test stay every viewer's (the desk refreshes the trainer's personal KB and runs 检索测试).
"""

import pytest

from tests import test_visibility as tv
from tests.test_visibility import _isolate, _kb, _member, _refused, kb_world

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob
_ = kb_world  # the fixture is used by name below


@pytest.fixture
def carol(app, admin):
    """An administrator with a users row — their KB is visible to every member under
    `isolated` (admins publish) but may not be changed by one."""
    return _member(app, "carol", role="admin")


def _mutations(base: str) -> list[tuple]:
    return [
        ("PATCH", base, {"description": "x"}),
        ("DELETE", base),
        ("DELETE", f"{base}?force=true"),
        ("POST", f"{base}/data-sources", {"mode": "upload"}),
        ("DELETE", f"{base}/data-sources/DS1"),
        ("POST", f"{base}/data-sources/DS1/sync", {"full": True}),
    ]


def _reads_and_sync(client, base: str) -> None:
    for method, url, *body in (
        ("GET", base),
        ("POST", f"{base}/data-sources/DS1/sync"),
        ("POST", f"{base}/data-sources/DS1/sync", {"full": False}),
        ("GET", f"{base}/data-sources/DS1/ingestion-jobs"),
        ("GET", f"{base}/data-sources/DS1/documents"),
        ("POST", f"{base}/query", {"text": "hello"}),
    ):
        res = client.request(method, url, json=body[0] if body else None)
        assert res.status_code == 200, (method, url, res.text)


def test_a_visible_but_foreign_kb_is_read_only_for_a_member(kb_world, bob, carol):
    _, bob_client = bob
    carol_id, _ = carol
    _kb("KBCAROL", carol_id)
    _isolate()
    base = "/api/knowledge-bases/KBCAROL"
    # the administrator's KB is listed and readable …
    assert "KBCAROL" in {i["kb_id"] for i in bob_client.get("/api/knowledge-bases").json()["items"]}
    _reads_and_sync(bob_client, base)
    # … but no mutation reaches the backend
    kb_world.calls.clear()
    _refused(bob_client, "auth.forbidden", *_mutations(base), status=403)
    upload = bob_client.post(f"{base}/files", files={"files": ("a.txt", b"hi")})
    assert upload.status_code == 403 and upload.json()["code"] == "auth.forbidden", upload.text
    assert kb_world.calls == []
    refused = bob_client.delete(base).json()
    assert refused["detail"] == {
        "created_by": carol_id, "created_by_name": "someone", "kb_id": "KBCAROL",
        "action": "delete",
    }
    assert "creator or an administrator" in refused["message"]


def test_a_null_creator_kb_is_an_administrators(kb_world, admin, bob):
    _, bob_client = bob
    base = "/api/knowledge-bases/KBOUT"  # preset / legacy row: no creator recorded
    _refused(bob_client, "auth.forbidden", *_mutations(base), status=403)
    _reads_and_sync(bob_client, base)
    kb_world.calls.clear()
    assert admin.patch(base, json={"description": "x"}).status_code == 200
    assert admin.post(f"{base}/data-sources", json={"mode": "upload"}).status_code == 201
    assert admin.post(f"{base}/data-sources/DS1/sync", json={"full": True}).status_code == 200
    assert admin.delete(base).status_code == 200
    assert kb_world.calls == ["update_description", "add_data_source", "start_sync", "delete_kb"]


def test_the_creator_and_administrators_may_mutate(kb_world, admin, ada, bob, carol):
    _, bob_client = bob
    _, carol_client = carol
    base = "/api/knowledge-bases/KBBOB"
    kb_world.calls.clear()
    assert bob_client.patch(base, json={"description": "x"}).status_code == 200
    assert bob_client.post(f"{base}/data-sources", json={"mode": "upload"}).status_code == 201
    assert bob_client.delete(f"{base}/data-sources/DS1").status_code == 200
    assert bob_client.post(f"{base}/data-sources/DS1/sync", json={"full": True}).status_code == 200
    upload = bob_client.post(f"{base}/files", files={"files": ("a.txt", b"hi")})
    assert upload.status_code == 200, upload.text
    # a member's own `force` is still the administrator override (N17-obs4): ownership first,
    # capability second (users-and-ownership.md §8 order)
    forced = bob_client.delete(f"{base}?force=true")
    assert forced.status_code == 403 and forced.json()["detail"] == {"kb_id": "KBBOB"}
    assert bob_client.delete(base).status_code == 200
    assert kb_world.calls == [
        "update_description", "add_data_source", "delete_data_source", "start_sync",
        "upload_files", "delete_kb",
    ]
    # administrators (config admin and a users-row admin) act on anyone's KB
    assert admin.patch("/api/knowledge-bases/KBADA", json={"description": "x"}).status_code == 200
    assert carol_client.delete("/api/knowledge-bases/KBADA").status_code == 200


def test_ownership_holds_in_the_shared_workspace_too(kb_world, ada, bob):
    """`shared` widens what a member SEES, not what they may change: ada's KB is readable by
    bob (200) and refused on every mutation (403, never 404)."""
    _, bob_client = bob
    base = "/api/knowledge-bases/KBADA"
    _reads_and_sync(bob_client, base)
    _refused(bob_client, "auth.forbidden", *_mutations(base), status=403)


def test_hidden_stays_404_before_the_ownership_403(kb_world, ada, bob):
    _, bob_client = bob
    _isolate()
    _refused(bob_client, "kb.not_found", *_mutations("/api/knowledge-bases/KBADA"))
