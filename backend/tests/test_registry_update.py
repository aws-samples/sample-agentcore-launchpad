"""Record edit endpoint (PUT /api/registry/records/{id}): description-only vs.
content edits (MCP url / skill SKILL.md overwrite / whole-bundle replace), the
validation + gating matrix, and staging consume-on-success semantics. AWS + S3
are mocked; no live calls."""

import io
import json
import types
import zipfile

import pytest

import app.routers.registry as registry_router
import app.services.registry_console as console_mod
from app.core.errors import AppError
from app.services import aws_clients
from app.services import skill_ingest as si

SKILL_MD = (
    "---\nname: meeting-summarizer\ndescription: Summarize meetings\n"
    "version: 0.3.0\n---\n# md\n"
)


def test_bump_minor_preserves_ga_type_suffix():
    assert console_mod._bump_minor("1.0.0-skill") == "1.1.0-skill"
    assert console_mod._bump_minor("1.0.0-mcp") == "1.1.0-mcp"
    assert console_mod._bump_minor("1.0.0") == "1.1.0"


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
    """Ordered op log of every S3 mutation so a test can assert exactly which
    objects were put/uploaded/deleted and in what order."""

    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.ops: list[tuple[str, str]] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self.existing)

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.ops.append(("delete", Key))

    def upload_file(self, src, bucket, key):
        self.ops.append(("upload", key))

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.ops.append(("put", Key))


def _skill_record(*, status="DRAFT", version="1.0.0", name="meeting-summarizer",
                  source=None, files=None):
    definition = {
        "name": name,
        "description": "old def desc",
        "version": "0.3.0",
        "path": f"s3://bkt/skills/{name}/",
        "files": files if files is not None else ["SKILL.md", "scripts/helper.py"],
        "source": source if source is not None
        else {"kind": "zip", "imported_at": "2020-01-01T00:00:00Z"},
    }
    return {
        "recordId": "r1",
        "name": name,
        "status": status,
        "recordVersion": version,
        "descriptorType": "AGENT_SKILLS",
        "description": "old record desc",
        "descriptors": {
            "agentSkills": {
                "skillMd": {"inlineContent": "# old body"},
                "skillDefinition": {"inlineContent": json.dumps(definition)},
            }
        },
    }


def _mcp_record(*, status="DRAFT", version="1.0.0", name="my-mcp"):
    server = {
        "name": f"io.launchpad/{name}",
        "description": "old",
        "version": "1.0.0",
        "remotes": [{"type": "streamable-http", "url": "https://old.example.com/mcp"}],
    }
    return {
        "recordId": "m1",
        "name": name,
        "status": status,
        "recordVersion": version,
        "descriptorType": "MCP",
        "description": "old record desc",
        "descriptors": {"mcp": {"server": {"inlineContent": json.dumps(server)}}},
    }


def _patch_update_aws(monkeypatch, fake_s3, record) -> dict:
    monkeypatch.setattr(console_mod, "control_client", lambda: object())
    monkeypatch.setattr(console_mod, "_registry_id", lambda: "reg")
    monkeypatch.setattr(
        console_mod, "get_settings",
        lambda: types.SimpleNamespace(resources={"artifacts_bucket": "bkt"}, region="us-west-2"),
    )
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake_s3)
    monkeypatch.setattr(console_mod.reg, "get_record", lambda c, r, rid: record)
    captured: dict = {}

    def fake_upsert(client, rid, *, name, description, descriptor_type, descriptors,
                    record_version=None):
        captured.update(
            record_version=record_version, descriptors=descriptors, name=name,
            description=description, descriptor_type=descriptor_type,
        )
        return {"recordId": "r1", "name": name}, False

    monkeypatch.setattr(console_mod.reg, "upsert_record", fake_upsert)
    monkeypatch.setattr(
        console_mod.reg, "wait_record_settled",
        lambda c, r, rid: {"recordId": rid, "name": record.get("name"),
                           "status": "DRAFT", "descriptorType": record.get("descriptorType")},
    )
    return captured


def _definition_from(captured) -> dict:
    return json.loads(
        captured["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
    )


# ---------- service: description-only ----------

def test_update_description_only_does_not_bump(monkeypatch):
    fake = FakeS3()
    record = _skill_record()
    captured = _patch_update_aws(monkeypatch, fake, record)

    out = console_mod.update_record("r1", description="a fresh description")

    assert out["status"] == "DRAFT"
    assert captured["record_version"] is None  # metadata-only → no version bump
    assert captured["description"] == "a fresh description"
    assert captured["descriptors"] == record["descriptors"]  # resent unchanged
    assert fake.ops == []  # no S3 writes for a metadata-only edit


# ---------- service: MCP url ----------

def test_update_mcp_url_rebuilds_descriptor_and_bumps(monkeypatch):
    fake = FakeS3()
    record = _mcp_record(version="1.0.0")
    captured = _patch_update_aws(monkeypatch, fake, record)

    console_mod.update_record("m1", url="https://new.example.com/mcp")

    assert captured["record_version"] == "1.1.0"
    assert captured["descriptor_type"] == "MCP"
    server = json.loads(captured["descriptors"]["mcp"]["server"]["inlineContent"])
    assert server["remotes"][0]["url"] == "https://new.example.com/mcp"


# ---------- service: skill_md inline edit ----------

def test_update_skill_md_overwrites_only_skill_md(monkeypatch):
    fake = FakeS3(existing=[
        "skills/meeting-summarizer/SKILL.md",
        "skills/meeting-summarizer/scripts/helper.py",
    ])
    record = _skill_record(
        version="1.0.0",
        files=["SKILL.md", "scripts/helper.py"],
        source={"kind": "git", "url": "https://github.com/o/r", "ref": "main",
                "subdir": "s", "imported_at": "2020-01-01T00:00:00Z"},
    )
    captured = _patch_update_aws(monkeypatch, fake, record)
    new_md = "---\nname: meeting-summarizer\nversion: 0.9.0\n---\n# new body\n"

    console_mod.update_record("r1", skill_md=new_md)

    # ONLY SKILL.md is written — nothing deleted, support files untouched
    assert fake.ops == [("put", "skills/meeting-summarizer/SKILL.md")]
    assert captured["record_version"] == "1.1.0"  # minor bump
    definition = _definition_from(captured)
    assert definition["files"] == ["SKILL.md", "scripts/helper.py"]  # preserved
    assert definition["version"] == "0.9.0"  # from new frontmatter
    assert definition["source"]["kind"] == "git"  # provenance preserved
    assert definition["source"]["ref"] == "main"
    assert definition["source"]["imported_at"].endswith("Z")
    assert definition["source"]["imported_at"] != "2020-01-01T00:00:00Z"  # refreshed
    # the descriptor carries the new SKILL.md content
    assert captured["descriptors"]["agentSkills"]["skillMd"]["inlineContent"] == new_md


def test_update_skill_md_keeps_old_version_when_frontmatter_lacks_it(monkeypatch):
    fake = FakeS3()
    record = _skill_record(version="1.0.0")  # definition.version == "0.3.0"
    captured = _patch_update_aws(monkeypatch, fake, record)

    console_mod.update_record("r1", skill_md="---\nname: meeting-summarizer\n---\n# x")

    assert _definition_from(captured)["version"] == "0.3.0"  # fell back to old


def test_update_skill_md_legacy_definition_without_files_source(monkeypatch):
    # A record whose definition predates the files/source additions must not
    # crash and must not invent a wrong file list — it falls back to just
    # SKILL.md and inline provenance while keeping the SKILL.md-only write.
    fake = FakeS3()
    record = _skill_record()
    legacy_def = {"name": "meeting-summarizer", "description": "legacy",
                  "version": "0.3.0", "path": "s3://bkt/skills/meeting-summarizer/"}
    record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"] = json.dumps(
        legacy_def
    )
    captured = _patch_update_aws(monkeypatch, fake, record)

    console_mod.update_record(
        "r1", skill_md="---\nname: meeting-summarizer\nversion: 2.0.0\n---\n# x"
    )

    assert fake.ops == [("put", "skills/meeting-summarizer/SKILL.md")]
    definition = _definition_from(captured)
    assert definition["files"] == ["SKILL.md"]  # no invented files
    assert definition["source"]["kind"] == "inline"  # fallback provenance
    assert definition["source"]["imported_at"].endswith("Z")
    assert definition["version"] == "2.0.0"
    assert captured["record_version"] == "1.1.0"


def test_update_skill_md_unparseable_definition_rebuilds_from_scratch(monkeypatch):
    # A malformed skillDefinition inlineContent (legacy/corrupt) must not raise —
    # the branch rebuilds a minimal definition rather than crashing.
    fake = FakeS3()
    record = _skill_record()
    record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"] = "not json{"
    captured = _patch_update_aws(monkeypatch, fake, record)

    console_mod.update_record(
        "r1", skill_md="---\nname: meeting-summarizer\nversion: 2.0.0\n---\n# x"
    )

    assert fake.ops == [("put", "skills/meeting-summarizer/SKILL.md")]
    definition = _definition_from(captured)
    assert definition["files"] == ["SKILL.md"]
    assert definition["source"]["kind"] == "inline"


def test_update_skill_md_and_description_together(monkeypatch):
    fake = FakeS3()
    record = _skill_record()
    captured = _patch_update_aws(monkeypatch, fake, record)

    console_mod.update_record(
        "r1", description="edited desc",
        skill_md="---\nname: meeting-summarizer\nversion: 1.2.0\n---\n# x",
    )

    assert captured["record_version"] == "1.1.0"
    assert captured["description"] == "edited desc"
    assert _definition_from(captured)["description"] == "edited desc"


def test_update_skill_md_non_semver_current_version_resets_bump(monkeypatch):
    fake = FakeS3()
    record = _skill_record(version="weird")
    captured = _patch_update_aws(monkeypatch, fake, record)

    console_mod.update_record("r1", skill_md="---\nname: meeting-summarizer\n---\n# x")

    assert captured["record_version"] == "1.1.0"  # _bump_minor resets unparseable


def test_update_skill_md_oversize_422(monkeypatch):
    fake = FakeS3()
    record = _skill_record()
    _patch_update_aws(monkeypatch, fake, record)

    with pytest.raises(AppError) as exc:
        console_mod.update_record("r1", skill_md="x" * (si.SKILL_MD_MAX_BYTES + 1))
    assert exc.value.status_code == 422
    assert exc.value.code == "registry.skill_invalid"
    assert fake.ops == []  # nothing written when the content is oversized


# ---------- service: whole-bundle replace ----------

def test_update_bundle_replace_swaps_prefix_and_files(monkeypatch):
    fake = FakeS3(existing=[
        "skills/meeting-summarizer/OLD.md",     # dropped in the new bundle → deleted
        "skills/meeting-summarizer/SKILL.md",
    ])
    record = _skill_record(version="2.0.0", files=["SKILL.md", "OLD.md"])
    captured = _patch_update_aws(monkeypatch, fake, record)

    bundle = si.bundle_from_zip(_multifile_zip())
    try:
        console_mod.update_record("r1", bundle=bundle)
    finally:
        bundle.close()

    delete_idx = [i for i, (op, _) in enumerate(fake.ops) if op == "delete"]
    upload_idx = [i for i, (op, _) in enumerate(fake.ops) if op == "upload"]
    assert delete_idx and upload_idx
    assert max(delete_idx) < min(upload_idx)  # old prefix cleared BEFORE new files land
    assert ("delete", "skills/meeting-summarizer/OLD.md") in fake.ops
    assert ("upload", "skills/meeting-summarizer/references/doc.md") in fake.ops
    assert captured["record_version"] == "2.1.0"
    definition = _definition_from(captured)
    assert definition["files"] == ["SKILL.md", "references/doc.md", "scripts/helper.py"]
    assert definition["source"]["imported_at"].endswith("Z")


# ---------- service: gating ----------

def test_update_deprecated_record_400(monkeypatch):
    record = _skill_record(status="DEPRECATED")
    _patch_update_aws(monkeypatch, FakeS3(), record)
    with pytest.raises(AppError) as exc:
        console_mod.update_record("r1", description="x")
    assert exc.value.status_code == 400
    assert exc.value.code == "registry.not_editable"


def test_update_a2a_record_400(monkeypatch):
    record = {"recordId": "a1", "name": "agent", "status": "DRAFT",
              "descriptorType": "A2A", "recordVersion": "1.0.0", "descriptors": {}}
    _patch_update_aws(monkeypatch, FakeS3(), record)
    with pytest.raises(AppError) as exc:
        console_mod.update_record("a1", description="x")
    assert exc.value.status_code == 400
    assert exc.value.code == "registry.not_editable"


# ---------- router: validation matrix ----------

def test_put_empty_body_400(client):
    res = client.put("/api/registry/records/r1", json={})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.nothing_to_update"


def test_put_skill_md_and_staging_conflict_400(client):
    res = client.put("/api/registry/records/r1",
                     json={"skill_md": "x", "staging_id": "s"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.field_conflict"


def test_put_url_on_skill_record_type_mismatch_400(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_get",
                        lambda rid: {"descriptorType": "AGENT_SKILLS"})
    res = client.put("/api/registry/records/r1", json={"url": "https://x.example.com"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.field_type_mismatch"


def test_put_skill_md_on_mcp_record_type_mismatch_400(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_get",
                        lambda rid: {"descriptorType": "MCP"})
    res = client.put("/api/registry/records/m1", json={"skill_md": "x"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.field_type_mismatch"


def test_put_bad_url_scheme_400(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_get",
                        lambda rid: {"descriptorType": "MCP"})
    res = client.put("/api/registry/records/m1", json={"url": "ftp://x"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.invalid_url"


def test_put_unknown_staging_410(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_get",
                        lambda rid: {"descriptorType": "AGENT_SKILLS"})
    res = client.put("/api/registry/records/r1",
                     json={"staging_id": "does-not-exist"})
    assert res.status_code == 410
    assert res.json()["code"] == "registry.staging_expired"


# ---------- router: staging consume-on-success semantics ----------

def _stage_zip(client):
    inspect = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _multifile_zip(), "application/zip")},
    )
    return inspect.json()["staging_id"]


def test_put_staging_index_out_of_range_400(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_get",
                        lambda rid: {"descriptorType": "AGENT_SKILLS"})
    sid = _stage_zip(client)  # single-skill zip → only index 0 exists
    res = client.put("/api/registry/records/r1",
                     json={"staging_id": sid, "index": 5})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.skill_not_staged"
    assert sid in registry_router._staging  # kept so the user can retry with a valid index


def test_put_staging_bundle_consumed_on_success(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_get",
                        lambda rid: {"descriptorType": "AGENT_SKILLS"})
    seen = {}

    def fake_update(rid, *, description=None, url=None, skill_md=None, bundle=None):
        seen["files"] = bundle.files if bundle else None
        return {"recordId": rid, "name": "s", "descriptorType": "AGENT_SKILLS",
                "status": "DRAFT", "recordVersion": "1.1.0"}

    monkeypatch.setattr(registry_router.console, "update_record", fake_update)
    sid = _stage_zip(client)
    res = client.put("/api/registry/records/r1", json={"staging_id": sid, "index": 0})
    assert res.status_code == 200
    assert res.json()["version"] == "1.1.0"
    assert seen["files"] == ["SKILL.md", "references/doc.md", "scripts/helper.py"]
    assert sid not in registry_router._staging  # consumed on success


def test_put_staging_bundle_kept_on_failure(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_get",
                        lambda rid: {"descriptorType": "AGENT_SKILLS"})

    def boom(rid, **kwargs):
        raise AppError("registry.skill_invalid", "bad bundle", status_code=422)

    monkeypatch.setattr(registry_router.console, "update_record", boom)
    sid = _stage_zip(client)
    res = client.put("/api/registry/records/r1", json={"staging_id": sid})
    assert res.status_code == 422
    assert sid in registry_router._staging  # kept so the user can retry


def test_put_returns_record_out_shape(client, monkeypatch):
    monkeypatch.setattr(registry_router.console, "console_get",
                        lambda rid: {"descriptorType": "AGENT_SKILLS"})
    monkeypatch.setattr(
        registry_router.console, "update_record",
        lambda rid, **k: {"recordId": rid, "name": "meeting-summarizer",
                          "descriptorType": "AGENT_SKILLS", "status": "DRAFT",
                          "recordVersion": "1.1.0", "description": "d"},
    )
    res = client.put("/api/registry/records/r1", json={"description": "d"})
    assert res.status_code == 200
    body = res.json()
    assert body["record_id"] == "r1"
    assert body["type"] == "AGENT_SKILLS"
    assert body["version"] == "1.1.0"
