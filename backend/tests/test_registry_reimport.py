"""Reimport pipeline: re-acquire a git/url-sourced skill, replace its S3 prefix
(delete-before-upload), and update the record with a bumped recordVersion and a
refreshed imported_at. S3 is a fake; the registry is the real local backend on
the test ledger; no live AWS calls."""

import io
import json
import zipfile

import pytest

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


def _multifile_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        zf.writestr("scripts/helper.py", "print(1)\n")
        zf.writestr("references/doc.md", "# doc\n")
    return buf.getvalue()


class FakePaginator:
    def __init__(self, keys):
        self._keys = keys

    def paginate(self, Bucket, Prefix):  # noqa: N803 (boto3 kwarg names)
        yield {"Contents": [{"Key": k} for k in self._keys if k.startswith(Prefix)]}


class FakeS3:
    """Records deletes and uploads in one ordered log so a test can assert the
    old prefix is cleared before any new object lands."""

    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.ops: list[tuple[str, str]] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self.existing)

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.ops.append(("delete", Key))

    def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        for obj in Delete.get("Objects", []):
            self.delete_object(Bucket=Bucket, Key=obj["Key"])
        return {"Deleted": [{"Key": o["Key"]} for o in Delete.get("Objects", [])]}

    def upload_file(self, src, bucket, key):
        self.ops.append(("upload", key))


def _advance(backend, ws, record_id, status):
    if status == "DRAFT":
        return
    backend.submit_record(ws, record_id)
    if status == "PENDING_APPROVAL":
        return
    backend.approve_record(ws, record_id)
    if status == "APPROVED":
        return
    if status == "DEPRECATED":
        backend.disable_record(ws, record_id)
        return
    raise ValueError(status)


def _seed(backend, source, *, status="DRAFT", version="1.0.0-skill",
         name="meeting-summarizer") -> str:
    definition = {
        "name": name,
        "description": "old",
        "version": "0.1.0",
        "path": f"s3://bkt/skills/{name}/",
        "files": ["SKILL.md"],
        "source": source,
    }
    record, _ = backend.upsert_record(
        WS, name=name, description="old desc", descriptor_type="AGENT_SKILLS",
        descriptors=reg.build_skills_descriptors(skill_md="# old", definition=definition),
        record_version=version,
    )
    rid = record["recordId"]
    _advance(backend, WS, rid, status)
    return rid


def _definition_from(record) -> dict:
    return json.loads(record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"])


def _patch_backend(monkeypatch, fake_s3) -> LocalRegistryBackend:
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake_s3)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    return backend


# ---------- happy paths ----------

def test_reimport_git_source_replaces_prefix_and_bumps(monkeypatch):
    src = {"kind": "git", "url": "https://github.com/org/skills", "ref": "main",
           "subdir": "skills/meeting-summarizer"}
    fake = FakeS3(existing=[
        "skills/meeting-summarizer/OLD.md",      # removed at source — must be deleted
        "skills/meeting-summarizer/SKILL.md",
    ])
    backend = _patch_backend(monkeypatch, fake)
    rid = _seed(backend, src, status="DRAFT", version="1.0.0-skill")

    def fake_bundles_from_git(url, ref=None, subdir=None, token=None):
        assert (url, ref, subdir) == (src["url"], "main", src["subdir"])
        assert token is None  # no token was persisted on the record
        b = si.bundle_from_zip(_multifile_zip())
        b.source = si.SkillSource(kind="git", url=url, ref=ref, subdir=subdir)
        return [b]

    monkeypatch.setattr(si, "bundles_from_git", fake_bundles_from_git)

    out = console_mod.reimport_skill(WS, rid)
    assert out["status"] == "DRAFT"

    delete_idx = [i for i, (op, _) in enumerate(fake.ops) if op == "delete"]
    upload_idx = [i for i, (op, _) in enumerate(fake.ops) if op == "upload"]
    assert delete_idx and upload_idx
    assert max(delete_idx) < min(upload_idx)  # cleared BEFORE new files land
    assert ("delete", "skills/meeting-summarizer/OLD.md") in fake.ops
    assert ("upload", "skills/meeting-summarizer/SKILL.md") in fake.ops

    assert out["recordVersion"] == "1.1.0-skill"  # minor bump
    definition = _definition_from(out)
    assert definition["name"] == "meeting-summarizer"  # record name kept
    assert definition["source"]["kind"] == "git"
    assert definition["source"]["imported_at"].endswith("Z")  # refreshed
    assert definition["files"] == ["SKILL.md", "references/doc.md", "scripts/helper.py"]


def test_reimport_git_keeps_name_when_frontmatter_renamed(monkeypatch):
    src = {"kind": "git", "url": "https://github.com/org/skills", "subdir": "skills/x"}
    fake = FakeS3(existing=["skills/registered-name/SKILL.md"])
    backend = _patch_backend(monkeypatch, fake)
    rid = _seed(backend, src, name="registered-name")

    def fake_bundles_from_git(url, ref=None, subdir=None, token=None):
        # frontmatter now says a different name — the record's name must win
        b = si.bundle_from_inline(SKILL_MD)  # name: meeting-summarizer
        b.source = si.SkillSource(kind="git", url=url, subdir=subdir)
        return [b]

    monkeypatch.setattr(si, "bundles_from_git", fake_bundles_from_git)
    out = console_mod.reimport_skill(WS, rid)
    assert out["name"] == "registered-name"
    assert ("upload", "skills/registered-name/SKILL.md") in fake.ops
    assert _definition_from(out)["name"] == "registered-name"


def test_reimport_url_source_happy_path(monkeypatch):
    fake = FakeS3(existing=["skills/meeting-summarizer/SKILL.md"])
    backend = _patch_backend(monkeypatch, fake)
    rid = _seed(backend, {"kind": "url", "url": "https://example.com/pack.zip"},
               version="2.4.0-skill")

    def fake_bundle_from_url(url):
        b = si.bundle_from_zip(_multifile_zip())
        b.source = si.SkillSource(kind="url", url=url)
        return b

    monkeypatch.setattr(si, "bundle_from_url", fake_bundle_from_url)
    out = console_mod.reimport_skill(WS, rid)
    assert out["status"] == "DRAFT"
    assert out["recordVersion"] == "2.5.0-skill"  # minor bump off 2.4.0-skill
    assert _definition_from(out)["source"]["kind"] == "url"


# ---------- rejections ----------

@pytest.mark.parametrize("kind", ["inline", "zip"])
def test_reimport_non_retrievable_source_400(monkeypatch, kind):
    fake = FakeS3()
    backend = _patch_backend(monkeypatch, fake)
    rid = _seed(backend, {"kind": kind})
    with pytest.raises(AppError) as exc:
        console_mod.reimport_skill(WS, rid)
    assert exc.value.status_code == 400
    assert exc.value.code == "registry.not_reimportable"
    assert fake.ops == []  # nothing touched in S3


def test_reimport_deprecated_record_400(monkeypatch):
    fake = FakeS3()
    backend = _patch_backend(monkeypatch, fake)
    rid = _seed(backend, {"kind": "git", "url": "https://github.com/o/r"},
               status="DEPRECATED")
    with pytest.raises(AppError) as exc:
        console_mod.reimport_skill(WS, rid)
    assert exc.value.status_code == 400
    assert exc.value.code == "registry.not_reimportable"
    assert fake.ops == []


def test_reimport_zero_bundle_git_4xx(monkeypatch):
    fake = FakeS3()
    backend = _patch_backend(monkeypatch, fake)
    rid = _seed(backend, {"kind": "git", "url": "https://github.com/o/r", "subdir": "skills/x"})
    monkeypatch.setattr(si, "bundles_from_git", lambda url, ref=None, subdir=None, token=None: [])
    with pytest.raises(AppError) as exc:
        console_mod.reimport_skill(WS, rid)
    assert 400 <= exc.value.status_code < 500
    assert fake.ops == []  # a failed re-acquire never touches S3


# ---------- router wiring ----------

def test_reimport_endpoint_returns_record(client, monkeypatch):
    # the creator-or-admin gate (N15-obs7) reads the record first; this test pins the
    # response projection only, so it is bypassed here (tests/test_mutation_ownership.py)
    monkeypatch.setattr(registry_router, "_require_record_mutable", lambda *a, **k: {})
    monkeypatch.setattr(
        registry_router.console, "reimport_skill",
        lambda _ws, rid: {"recordId": rid, "name": "s", "descriptorType": "AGENT_SKILLS",
                     "status": "DRAFT", "recordVersion": "1.1.0"},
    )
    res = client.post("/api/registry/records/r1/reimport")
    assert res.status_code == 200
    body = res.json()
    assert body["record_id"] == "r1"
    assert body["version"] == "1.1.0"
    assert body["status"] == "DRAFT"


def test_reimport_endpoint_surfaces_not_reimportable(client, monkeypatch):
    def boom(_ws, rid):
        raise AppError("registry.not_reimportable", "inline record", status_code=400)

    monkeypatch.setattr(registry_router, "_require_record_mutable", lambda *a, **k: {})
    monkeypatch.setattr(registry_router.console, "reimport_skill", boom)
    res = client.post("/api/registry/records/r1/reimport")
    assert res.status_code == 400
    assert res.json()["code"] == "registry.not_reimportable"
