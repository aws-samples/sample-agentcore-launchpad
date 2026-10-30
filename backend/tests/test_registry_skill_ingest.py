"""Multi-source skill ingestion: register_skill_bundle S3 funnel + cleanup, and
the inspect→import router flow with staging (mocked AWS, no live calls)."""

import io
import json
import zipfile

import app.routers.registry as registry_router
import app.services.registry_console as console_mod
from app.core.errors import AppError
from app.services import aws_clients
from app.services import registry_backend as reg
from app.services import skill_ingest as si
from app.services.registry_local import LocalRegistryBackend
from tests.conftest import ws_ctx

WS = ws_ctx({"artifacts_bucket": "bkt"})

SKILL_MD = (
    "---\nname: meeting-summarizer\ndescription: Summarize meetings\n"
    "version: 0.3.0\n---\n# md\n"
)


class FakeS3:
    def __init__(self):
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    def upload_file(self, src, bucket, key):
        self.uploaded.append(key)

    def delete_object(self, Bucket, Key):  # noqa: N803 (boto3 kwarg names)
        self.deleted.append(Key)

    def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        for obj in Delete.get("Objects", []):
            self.delete_object(Bucket=Bucket, Key=obj["Key"])
        return {"Deleted": [{"Key": o["Key"]} for o in Delete.get("Objects", [])]}

    def get_paginator(self, op):
        """N17-obs269: the post-upload prune lists the prefix — what is uploaded and not
        deleted."""
        assert op == "list_objects_v2"
        live = [k for k in self.uploaded if k not in self.deleted]

        class _Pages:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                yield {"Contents": [{"Key": k} for k in live if k.startswith(Prefix)]}

        return _Pages()


def _multifile_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        zf.writestr("scripts/helper.py", "print(1)\n")
        zf.writestr("references/doc.md", "# doc\n")
    return buf.getvalue()


def _patch_aws(monkeypatch, fake_s3) -> LocalRegistryBackend:
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake_s3)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    return backend


def test_register_skill_bundle_uploads_all_files_and_stamps_source(monkeypatch):
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    bundle = si.bundle_from_zip(_multifile_zip())
    try:
        record = console_mod.register_skill_bundle(bundle, WS)
    finally:
        bundle.close()
    assert record["status"] == "DRAFT"
    assert sorted(fake.uploaded) == [
        "skills/meeting-summarizer/SKILL.md",
        "skills/meeting-summarizer/references/doc.md",
        "skills/meeting-summarizer/scripts/helper.py",
    ]
    definition = json.loads(
        record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
    )
    assert definition["files"] == ["SKILL.md", "references/doc.md", "scripts/helper.py"]
    assert definition["path"] == "s3://bkt/skills/meeting-summarizer/"
    assert definition["source"]["kind"] == "zip"
    assert definition["source"]["imported_at"].endswith("Z")


def test_register_skill_bundle_uploads_nothing_on_record_failure(monkeypatch):
    """N17-obs265: the record is claimed BEFORE the upload — a failed record write leaves
    no S3 object at all (formerly: upload first, then delete the orphans)."""
    fake = FakeS3()
    backend = _patch_aws(monkeypatch, fake)

    def boom(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(backend, "upsert_record", boom)
    bundle = si.bundle_from_zip(_multifile_zip())
    try:
        try:
            console_mod.register_skill_bundle(bundle, WS)
            raise AssertionError("expected failure")
        except RuntimeError:
            pass
    finally:
        bundle.close()
    assert fake.uploaded == [] and fake.deleted == []


def test_register_skill_bundle_cleans_up_on_upload_failure(monkeypatch):
    """A mid-batch upload failure after the claim: every uploaded object is deleted and
    the claimed record is dropped (no orphan prefix, no record pointing at missing files)."""
    fake = FakeS3()
    backend = _patch_aws(monkeypatch, fake)
    real_upload = fake.upload_file

    def flaky(src, bucket, key):
        if len(fake.uploaded) == 2:
            raise RuntimeError("S3 down")
        real_upload(src, bucket, key)

    fake.upload_file = flaky
    bundle = si.bundle_from_zip(_multifile_zip())
    try:
        try:
            console_mod.register_skill_bundle(bundle, WS)
            raise AssertionError("expected failure")
        except RuntimeError:
            pass
    finally:
        bundle.close()
    assert len(fake.uploaded) == 2
    assert sorted(fake.deleted) == sorted(fake.uploaded)
    assert backend.find_record(WS, "meeting-summarizer", "AGENT_SKILLS") is None


def test_register_skill_bundle_rejects_bad_name(monkeypatch):
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    bundle = si.bundle_from_inline("---\nname: Bad Name\n---\n# x")
    try:
        try:
            console_mod.register_skill_bundle(bundle, WS)
            raise AssertionError("expected validation error")
        except AppError as exc:
            assert exc.status_code == 422
    finally:
        bundle.close()
    assert fake.uploaded == []  # nothing uploaded when the name is invalid


def test_register_skill_bundle_rejects_oversized_descriptor(monkeypatch):
    """A file list whose JSON descriptor exceeds the AWS 102,400-byte cap is
    refused at the Launchpad layer before any S3 object is created (AC4/AC5)."""
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    bundle = si.bundle_from_inline(SKILL_MD)
    # forge a file list that serializes well past the cap without touching disk
    bundle.files = [f"references/{'d' * 500}/doc-{i}.md" for i in range(200)]
    try:
        try:
            console_mod.register_skill_bundle(bundle, WS)
            raise AssertionError("expected validation error")
        except AppError as exc:
            assert exc.status_code == 422
            assert "descriptor" in exc.message
    finally:
        bundle.close()
    assert fake.uploaded == []  # nothing uploaded when the descriptor is too large


def test_inline_register_skill_stamps_inline_source(monkeypatch):
    """Legacy paste path still works and now records source.kind == inline."""
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    record = console_mod.register_skill(WS, "meeting-summarizer", "", SKILL_MD)
    definition = json.loads(
        record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
    )
    assert definition["source"]["kind"] == "inline"
    assert definition["files"] == ["SKILL.md"]


# ---------- router: inspect → import ----------

def test_inspect_then_import_happy_path(client, monkeypatch):
    seen = {}

    def fake_register(bundle, _ws, *, name_override=None, description_override=None):
        seen["files"] = bundle.files
        seen["name_override"] = name_override
        return {"recordId": "r9", "name": name_override or bundle.name,
                "descriptorType": "AGENT_SKILLS", "status": "DRAFT", "recordVersion": "1.0.0"}

    monkeypatch.setattr(registry_router.console, "register_skill_bundle", fake_register)

    res = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _multifile_zip(), "application/zip")},
    )
    assert res.status_code == 200
    body = res.json()
    sid = body["staging_id"]
    assert body["skills"][0]["name"] == "meeting-summarizer"
    assert body["skills"][0]["valid"] is True
    assert body["skills"][0]["files"] == ["SKILL.md", "references/doc.md", "scripts/helper.py"]

    imp = client.post("/api/registry/skills/import", json={
        "staging_id": sid,
        "selections": [{"index": 0, "name_override": "my-skill"}],
    })
    assert imp.status_code == 200
    records = imp.json()["records"]
    assert records[0]["ok"] is True
    assert records[0]["record"]["record_id"] == "r9"
    assert seen["name_override"] == "my-skill"
    assert seen["files"] == ["SKILL.md", "references/doc.md", "scripts/helper.py"]

    # staging is consumed after import → second import is 404 (unknown id)
    again = client.post("/api/registry/skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert again.status_code == 404
    assert again.json()["code"] == "registry.staging_not_found"


def test_inspect_rejects_non_zip(client):
    res = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("skill.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 400
    assert res.json()["code"] == "registry.invalid_upload"


def test_inspect_rejects_traversal_zip(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        zf.writestr("../evil.md", "pwn")
    res = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bad.zip", buf.getvalue(), "application/zip")},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "registry.skill_invalid"


def test_inspect_rejects_zip_without_skill_md(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("scripts/helper.py", "x\n")
    res = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("no-md.zip", buf.getvalue(), "application/zip")},
    )
    assert res.status_code == 422


def test_import_unknown_staging_id_404(client):
    res = client.post("/api/registry/skills/import", json={
        "staging_id": "does-not-exist", "selections": [{"index": 0}],
    })
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"


def test_import_name_conflict_reported_per_item(client, monkeypatch):
    def conflict(bundle, _ws, *, name_override=None, description_override=None):
        raise AppError("registry.name_exists", "already exists", status_code=409)

    monkeypatch.setattr(registry_router.console, "register_skill_bundle", conflict)
    inspect = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _multifile_zip(), "application/zip")},
    )
    sid = inspect.json()["staging_id"]
    imp = client.post("/api/registry/skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert imp.status_code == 200  # batch endpoint succeeds; the item fails
    record = imp.json()["records"][0]
    assert record["ok"] is False
    assert record["error"] == "already exists"  # plain string for inline display
    assert record["error_code"] == "registry.name_exists"
    # staging survives a failed import so the user can rename and retry
    assert sid in registry_router._staging


def test_import_expired_staging_swept(client, monkeypatch):
    inspect = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _multifile_zip(), "application/zip")},
    )
    sid = inspect.json()["staging_id"]
    registry_router._staging[sid]["expires"] = 0.0  # force-expire
    imp = client.post("/api/registry/skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert imp.status_code == 404
    assert imp.json()["code"] == "registry.staging_not_found"


# ---------- fix round 1: staging ownership (workspace + principal) ----------

def test_import_cross_workspace_staging_is_404_and_not_consumed(client, monkeypatch):
    def must_not_register(bundle, _ws, *, name_override=None, description_override=None):
        raise AssertionError("must not reach the registration pipeline")

    monkeypatch.setattr(registry_router.console, "register_skill_bundle", must_not_register)
    inspect = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _multifile_zip(), "application/zip")},
    )
    sid = inspect.json()["staging_id"]
    # simulates "staged in workspace A, imported from workspace B": the entry
    # inspect_skill stamped is tampered to another workspace's id
    registry_router._staging[sid]["workspace_id"] = "another-workspace"
    res = client.post("/api/registry/skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"
    assert sid in registry_router._staging  # not consumed/dropped


def test_import_cross_principal_staging_is_404_and_not_consumed(client, monkeypatch):
    def must_not_register(bundle, _ws, *, name_override=None, description_override=None):
        raise AssertionError("must not reach the registration pipeline")

    monkeypatch.setattr(registry_router.console, "register_skill_bundle", must_not_register)
    inspect = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _multifile_zip(), "application/zip")},
    )
    sid = inspect.json()["staging_id"]
    registry_router._staging[sid]["owner_principal"] = "someone-else"
    res = client.post("/api/registry/skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"
    assert sid in registry_router._staging  # not consumed/dropped
