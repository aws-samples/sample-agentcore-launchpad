"""Attach-without-registering: staged skill bundle → /api/agent-skills/import.

Uploads land under agent-skills/{uid8}/{name}/ and NO registry record is
created (mocked AWS, no live calls). Bundles are parked in the registry router's
staging dict directly (bypassing ``POST /api/registry/skills/inspect`` for
speed); ``_inspect`` below stamps the same ``owner_principal``/``workspace_id``
the real route stamps, so ``take_staging``'s ownership check sees a normal
same-tenant entry."""

import io
import zipfile

from app.assistant.principal import LOCAL_OPERATOR
from app.core.db import DEFAULT_WORKSPACE_ID
from app.services import aws_clients
from tests.conftest import set_default_resources

SKILL_MD = (
    "---\nname: meeting-summarizer\ndescription: Summarize meetings\n"
    "version: 0.3.0\n---\n# md\n"
)


class FakeS3:
    def __init__(self, fail_keys: set[str] | None = None):
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.fail_keys = fail_keys or set()

    def upload_file(self, src, bucket, key):
        if any(fk in key for fk in self.fail_keys):
            raise RuntimeError("s3 down")
        self.uploaded.append(key)

    def delete_object(self, Bucket, Key):  # noqa: N803 (boto3 kwarg names)
        self.deleted.append(Key)

    def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        for obj in Delete.get("Objects", []):
            self.delete_object(Bucket=Bucket, Key=obj["Key"])
        return {"Deleted": [{"Key": o["Key"]} for o in Delete.get("Objects", [])]}


def _zip(name: str = "meeting-summarizer") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD.replace("meeting-summarizer", name))
        zf.writestr("scripts/helper.py", "print(1)\n")
    return buf.getvalue()


def _patch_aws(monkeypatch, fake_s3):
    # The route reads the bucket off the request's workspace row now.
    set_default_resources({"artifacts_bucket": "bkt"})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake_s3)


def _inspect(client, name: str = "meeting-summarizer") -> str:
    import app.routers.registry as registry_router
    from app.services.skill_ingest import bundle_from_zip

    sid = registry_router._stage([bundle_from_zip(_zip(name))])
    # Mirrors inspect_skill's own stamp (hermetic client → open-console principal,
    # default workspace) so take_staging's ownership check sees a normal entry.
    registry_router._staging[sid].update(
        owner_principal=LOCAL_OPERATOR, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    return sid


def test_attach_uploads_without_registry_record(client, monkeypatch):
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    registered: list[str] = []

    class _Registry:  # a backend that records any write — none may happen here
        def upsert_record(self, *a, **k):
            registered.append("called")

    from app.services import registry_backend

    registry_backend.set_backend(_Registry())

    sid = _inspect(client)
    res = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert res.status_code == 200
    (skill,) = res.json()["skills"]
    assert skill["ok"] is True
    assert skill["name"] == "meeting-summarizer"
    assert skill["path"].startswith("s3://bkt/agent-skills/")
    assert skill["path"].endswith("/meeting-summarizer/")
    assert skill["description"] == "Summarize meetings"
    prefix = skill["path"].removeprefix("s3://bkt/")
    assert sorted(fake.uploaded) == [
        f"{prefix}SKILL.md", f"{prefix}scripts/helper.py",
    ]
    assert registered == []  # the registry funnel was never touched

    # staging consumed after full success → replay is 404 (unknown id)
    again = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert again.status_code == 404
    assert again.json()["code"] == "registry.staging_not_found"


def test_attach_name_override_and_selection_by_name(client, monkeypatch):
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    sid = _inspect(client)
    res = client.post("/api/agent-skills/import", json={
        "staging_id": sid,
        "selections": [{"name": "meeting-summarizer", "name_override": "my-notes"}],
    })
    (skill,) = res.json()["skills"]
    assert skill["ok"] is True and skill["name"] == "my-notes"
    assert skill["path"].endswith("/my-notes/")


def test_attach_unknown_staging_404(client, monkeypatch):
    _patch_aws(monkeypatch, FakeS3())
    res = client.post("/api/agent-skills/import", json={
        "staging_id": "nope", "selections": [{"index": 0}],
    })
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"


def test_attach_cross_workspace_staging_is_404_and_not_consumed(client, monkeypatch):
    import app.routers.registry as registry_router

    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    sid = _inspect(client)
    registry_router._staging[sid]["workspace_id"] = "another-workspace"
    res = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"
    assert sid in registry_router._staging  # not consumed/dropped
    assert fake.uploaded == []  # nothing attached


def test_attach_cross_principal_staging_is_404_and_not_consumed(client, monkeypatch):
    import app.routers.registry as registry_router

    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    sid = _inspect(client)
    registry_router._staging[sid]["owner_principal"] = "someone-else"
    res = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"
    assert sid in registry_router._staging  # not consumed/dropped
    assert fake.uploaded == []  # nothing attached


def test_attach_bad_selection_and_staging_survives(client, monkeypatch):
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    sid = _inspect(client)
    res = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 7}],
    })
    (skill,) = res.json()["skills"]
    assert skill["ok"] is False
    assert skill["error_code"] == "registry.skill_not_staged"

    # staging kept on failure — a corrected retry succeeds without re-inspect
    retry = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert retry.json()["skills"][0]["ok"] is True


def test_attach_bad_name_override_rejected_per_item(client, monkeypatch):
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    sid = _inspect(client)
    res = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 0, "name_override": "Bad Name!"}],
    })
    (skill,) = res.json()["skills"]
    assert skill["ok"] is False
    assert skill["error_code"] == "registry.skill_invalid"
    assert fake.uploaded == []


def test_attach_upload_failure_cleans_partial_prefix(client, monkeypatch):
    fake = FakeS3(fail_keys={"helper.py"})
    _patch_aws(monkeypatch, fake)
    sid = _inspect(client)
    res = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    (skill,) = res.json()["skills"]
    assert skill["ok"] is False
    assert skill["error_code"] == "agents.skill_attach_failed"
    # the SKILL.md that landed before the failure was deleted again
    assert fake.deleted == fake.uploaded
