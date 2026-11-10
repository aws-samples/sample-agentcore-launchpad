"""Record edit endpoint (PUT /api/registry/records/{id}): description-only vs.
content edits (MCP url / skill SKILL.md overwrite / whole-bundle replace), the
validation + gating matrix, and staging consume-on-success semantics. S3 is a
fake; the registry is the real local backend on the test ledger; no live AWS
calls."""

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
from tests.conftest import set_default_resources, ws_ctx

WS = ws_ctx({"artifacts_bucket": "bkt"})

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

    def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        for obj in Delete.get("Objects", []):
            self.delete_object(Bucket=Bucket, Key=obj["Key"])
        return {"Deleted": [{"Key": o["Key"]} for o in Delete.get("Objects", [])]}

    def upload_file(self, src, bucket, key):
        self.ops.append(("upload", key))

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.ops.append(("put", Key))


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


def _seed_skill(backend, *, status="DRAFT", version="1.0.0-skill", name="meeting-summarizer",
                source=None, files=None) -> str:
    definition = {
        "name": name,
        "description": "old def desc",
        "version": "0.3.0",
        "path": f"s3://bkt/skills/{name}/",
        "files": files if files is not None else ["SKILL.md", "scripts/helper.py"],
        "source": source if source is not None
        else {"kind": "zip", "imported_at": "2020-01-01T00:00:00Z"},
    }
    record, _ = backend.upsert_record(
        WS, name=name, description="old record desc", descriptor_type="AGENT_SKILLS",
        descriptors=reg.build_skills_descriptors(skill_md="# old body", definition=definition),
        record_version=version,
    )
    rid = record["recordId"]
    _advance(backend, WS, rid, status)
    return rid


def _seed_mcp(backend, *, status="DRAFT", version="1.0.0-mcp", name="my-mcp") -> str:
    record, _ = backend.upsert_record(
        WS, name=name, description="old record desc", descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(
            target=name, description="old", gateway_url="https://old.example.com/mcp",
            tools=None,
        ),
        record_version=version,
    )
    rid = record["recordId"]
    _advance(backend, WS, rid, status)
    return rid


def _definition_from(record) -> dict:
    return json.loads(record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"])


# ---------- service: description-only ----------

def test_update_description_only_does_not_bump(monkeypatch):
    """R2: a description-only edit keeps both status and version — approve first."""
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend)
    backend.submit_record(WS, rid)
    backend.approve_record(WS, rid)
    before = backend.get_record(WS, rid)

    out = console_mod.update_record(rid, WS, description="a fresh description")

    assert out["status"] == "APPROVED"  # unchanged
    assert out["recordVersion"] == before["recordVersion"]  # metadata-only → no version bump
    assert out["description"] == "a fresh description"
    assert out["descriptors"] == before["descriptors"]  # resent unchanged
    assert fake.ops == []  # no S3 writes for a metadata-only edit


# ---------- service: MCP url ----------

def test_update_mcp_url_rebuilds_descriptor_and_bumps(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_mcp(backend, version="1.0.0-mcp")

    out = console_mod.update_record(rid, WS, url="https://new.example.com/mcp")

    assert out["recordVersion"] == "1.1.0-mcp"
    assert out["descriptorType"] == "MCP"
    server = json.loads(out["descriptors"]["mcp"]["server"]["inlineContent"])
    assert server["remotes"][0]["url"] == "https://new.example.com/mcp"


# ---------- service: skill_md inline edit ----------

def test_update_skill_md_overwrites_only_skill_md(monkeypatch):
    fake = FakeS3(existing=[
        "skills/meeting-summarizer/SKILL.md",
        "skills/meeting-summarizer/scripts/helper.py",
    ])
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(
        backend,
        version="1.0.0-skill",
        files=["SKILL.md", "scripts/helper.py"],
        source={"kind": "git", "url": "https://github.com/o/r", "ref": "main",
                "subdir": "s", "imported_at": "2020-01-01T00:00:00Z"},
    )
    new_md = "---\nname: meeting-summarizer\nversion: 0.9.0\n---\n# new body\n"

    out = console_mod.update_record(rid, WS, skill_md=new_md)

    # ONLY SKILL.md is written — nothing deleted, support files untouched
    assert fake.ops == [("put", "skills/meeting-summarizer/SKILL.md")]
    assert out["recordVersion"] == "1.1.0-skill"  # minor bump
    definition = _definition_from(out)
    assert definition["files"] == ["SKILL.md", "scripts/helper.py"]  # preserved
    assert definition["version"] == "0.9.0"  # from new frontmatter
    assert definition["source"]["kind"] == "git"  # provenance preserved
    assert definition["source"]["ref"] == "main"
    assert definition["source"]["imported_at"].endswith("Z")
    assert definition["source"]["imported_at"] != "2020-01-01T00:00:00Z"  # refreshed
    # the descriptor carries the new SKILL.md content
    assert out["descriptors"]["agentSkills"]["skillMd"]["inlineContent"] == new_md


def test_update_skill_md_keeps_old_version_when_frontmatter_lacks_it(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend, version="1.0.0-skill")  # definition.version == "0.3.0"

    out = console_mod.update_record(rid, WS, skill_md="---\nname: meeting-summarizer\n---\n# x")

    assert _definition_from(out)["version"] == "0.3.0"  # fell back to old


def test_update_skill_md_legacy_definition_without_files_source(monkeypatch):
    # A record whose definition predates the files/source additions must not
    # crash and must not invent a wrong file list — it falls back to just
    # SKILL.md and inline provenance while keeping the SKILL.md-only write.
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend, version="1.0.0-skill")
    legacy_def = {"name": "meeting-summarizer", "description": "legacy",
                  "version": "0.3.0", "path": "s3://bkt/skills/meeting-summarizer/"}
    backend.update_record(
        WS, rid,
        descriptors=reg.build_skills_descriptors(skill_md="# old body", definition=legacy_def),
    )

    out = console_mod.update_record(
        rid, WS, skill_md="---\nname: meeting-summarizer\nversion: 2.0.0\n---\n# x"
    )

    assert fake.ops == [("put", "skills/meeting-summarizer/SKILL.md")]
    definition = _definition_from(out)
    assert definition["files"] == ["SKILL.md"]  # no invented files
    assert definition["source"]["kind"] == "inline"  # fallback provenance
    assert definition["source"]["imported_at"].endswith("Z")
    assert definition["version"] == "2.0.0"
    assert out["recordVersion"] == "1.1.0-skill"


def test_update_skill_md_unparseable_definition_rebuilds_from_scratch(monkeypatch):
    # A malformed skillDefinition inlineContent (legacy/corrupt) must not raise —
    # the branch rebuilds a minimal definition rather than crashing.
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend, version="1.0.0-skill")
    backend.update_record(
        WS, rid,
        descriptors={"agentSkills": {"skillMd": {"inlineContent": "# old"},
                                     "skillDefinition": {"inlineContent": "not json{"}}},
    )

    out = console_mod.update_record(
        rid, WS, skill_md="---\nname: meeting-summarizer\nversion: 2.0.0\n---\n# x"
    )

    assert fake.ops == [("put", "skills/meeting-summarizer/SKILL.md")]
    definition = _definition_from(out)
    assert definition["files"] == ["SKILL.md"]
    assert definition["source"]["kind"] == "inline"


def test_update_skill_md_and_description_together(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend, version="1.0.0-skill")

    out = console_mod.update_record(
        rid, WS, description="edited desc",
        skill_md="---\nname: meeting-summarizer\nversion: 1.2.0\n---\n# x",
    )

    assert out["recordVersion"] == "1.1.0-skill"
    assert out["description"] == "edited desc"
    assert _definition_from(out)["description"] == "edited desc"


def test_update_skill_md_non_semver_current_version_resets_bump(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend, version="weird")

    out = console_mod.update_record(rid, WS, skill_md="---\nname: meeting-summarizer\n---\n# x")

    assert out["recordVersion"] == "1.1.0"  # _bump_minor resets unparseable


def test_update_skill_md_oversize_422(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend)

    with pytest.raises(AppError) as exc:
        console_mod.update_record(rid, WS, skill_md="x" * (si.SKILL_MD_MAX_BYTES + 1))
    assert exc.value.status_code == 422
    assert exc.value.code == "registry.skill_invalid"
    assert fake.ops == []  # nothing written when the content is oversized


# ---------- service: whole-bundle replace ----------

def test_update_bundle_replace_swaps_prefix_and_files(monkeypatch):
    fake = FakeS3(existing=[
        "skills/meeting-summarizer/OLD.md",     # dropped in the new bundle → deleted
        "skills/meeting-summarizer/SKILL.md",
    ])
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend, version="2.0.0-skill", files=["SKILL.md", "OLD.md"])

    bundle = si.bundle_from_zip(_multifile_zip())
    try:
        out = console_mod.update_record(rid, WS, bundle=bundle)
    finally:
        bundle.close()

    delete_idx = [i for i, (op, _) in enumerate(fake.ops) if op == "delete"]
    upload_idx = [i for i, (op, _) in enumerate(fake.ops) if op == "upload"]
    assert delete_idx and upload_idx
    assert max(delete_idx) < min(upload_idx)  # old prefix cleared BEFORE new files land
    assert ("delete", "skills/meeting-summarizer/OLD.md") in fake.ops
    assert ("upload", "skills/meeting-summarizer/references/doc.md") in fake.ops
    assert out["recordVersion"] == "2.1.0-skill"
    definition = _definition_from(out)
    assert definition["files"] == ["SKILL.md", "references/doc.md", "scripts/helper.py"]
    assert definition["source"]["imported_at"].endswith("Z")


# ---------- service: gating ----------

def test_update_deprecated_record_400(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    rid = _seed_skill(backend, status="DEPRECATED")
    with pytest.raises(AppError) as exc:
        console_mod.update_record(rid, WS, description="x")
    assert exc.value.status_code == 400
    assert exc.value.code == "registry.not_editable"


def test_update_a2a_record_400(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    card = reg.build_a2a_card(name="agent", description="d", arn="arn:x", method="zip_runtime")
    record, _ = backend.upsert_record(
        WS, name="agent", description="d", descriptor_type="A2A",
        descriptors=reg.build_a2a_descriptors(card),
    )
    rid = record["recordId"]
    with pytest.raises(AppError) as exc:
        console_mod.update_record(rid, WS, description="x")
    assert exc.value.status_code == 400
    assert exc.value.code == "registry.not_editable"


# ---------- router: validation matrix ----------

def _seed_route_record(descriptor_type, *, name="rec", version=None, status="DRAFT"):
    backend = reg.get_backend()
    ws = ws_ctx({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    if descriptor_type == "AGENT_SKILLS":
        definition = {"name": name, "description": "d", "version": "0.1.0",
                      "path": f"s3://bkt/skills/{name}/", "files": ["SKILL.md"],
                      "source": {"kind": "zip", "imported_at": "2020-01-01T00:00:00Z"}}
        descriptors = reg.build_skills_descriptors(skill_md="# x", definition=definition)
    elif descriptor_type == "MCP":
        descriptors = reg.build_mcp_descriptors(
            target=name, description="d", gateway_url="https://old.example.com/mcp", tools=None,
        )
    else:
        raise ValueError(descriptor_type)
    record, _ = backend.upsert_record(
        ws, name=name, description="d", descriptor_type=descriptor_type,
        descriptors=descriptors, record_version=version,
    )
    rid = record["recordId"]
    _advance(backend, ws, rid, status)
    return rid


def test_put_empty_body_400(client):
    res = client.put("/api/registry/records/r1", json={})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.nothing_to_update"


def test_put_skill_md_and_staging_conflict_400(client):
    res = client.put("/api/registry/records/r1",
                     json={"skill_md": "x", "staging_id": "s"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.field_conflict"


def test_put_url_on_skill_record_type_mismatch_400(client):
    rid = _seed_route_record("AGENT_SKILLS", name="skill-rec")
    res = client.put(f"/api/registry/records/{rid}", json={"url": "https://x.example.com"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.field_type_mismatch"


def test_put_skill_md_on_mcp_record_type_mismatch_400(client):
    rid = _seed_route_record("MCP", name="mcp-rec")
    res = client.put(f"/api/registry/records/{rid}", json={"skill_md": "x"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.field_type_mismatch"


def test_put_bad_url_scheme_400(client):
    rid = _seed_route_record("MCP", name="mcp-rec2")
    res = client.put(f"/api/registry/records/{rid}", json={"url": "ftp://x"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.invalid_url"


def test_put_unknown_staging_404(client):
    rid = _seed_route_record("AGENT_SKILLS", name="skill-rec2")
    res = client.put(f"/api/registry/records/{rid}",
                     json={"staging_id": "does-not-exist"})
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"


# ---------- router: staging consume-on-success semantics ----------

def _stage_zip(client):
    inspect = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _multifile_zip(), "application/zip")},
    )
    return inspect.json()["staging_id"]


def test_put_staging_index_out_of_range_400(client, monkeypatch):
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    rid = _seed_route_record("AGENT_SKILLS", name="meeting-summarizer")
    sid = _stage_zip(client)  # single-skill zip → only index 0 exists
    res = client.put(f"/api/registry/records/{rid}", json={"staging_id": sid, "index": 5})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.skill_not_staged"
    assert sid in registry_router._staging  # kept so the user can retry with a valid index


# ---------- fix round 1: staging ownership (workspace + principal) ----------

def test_put_staging_cross_workspace_is_404_and_not_consumed(client, monkeypatch):
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    rid = _seed_route_record("AGENT_SKILLS", name="meeting-summarizer")
    sid = _stage_zip(client)
    registry_router._staging[sid]["workspace_id"] = "another-workspace"
    res = client.put(f"/api/registry/records/{rid}", json={"staging_id": sid})
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"
    assert sid in registry_router._staging  # not consumed/dropped


def test_put_staging_cross_principal_is_404_and_not_consumed(client, monkeypatch):
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    rid = _seed_route_record("AGENT_SKILLS", name="meeting-summarizer")
    sid = _stage_zip(client)
    registry_router._staging[sid]["owner_principal"] = "someone-else"
    res = client.put(f"/api/registry/records/{rid}", json={"staging_id": sid})
    assert res.status_code == 404
    assert res.json()["code"] == "registry.staging_not_found"
    assert sid in registry_router._staging  # not consumed/dropped


def test_put_staging_bundle_consumed_on_success(client, monkeypatch):
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    rid = _seed_route_record("AGENT_SKILLS", name="meeting-summarizer", version="1.0.0-skill")
    sid = _stage_zip(client)
    res = client.put(f"/api/registry/records/{rid}", json={"staging_id": sid, "index": 0})
    assert res.status_code == 200
    assert res.json()["version"] == "1.1.0-skill"
    assert sid not in registry_router._staging  # consumed on success


def test_put_staging_bundle_kept_on_failure(client, monkeypatch):
    # The old mock-based "service boom" trigger no longer exists against the real
    # pipeline; a DEPRECATED record (terminal, refuses every edit) is the natural
    # real failure that still proves the staged bundle survives for retry.
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    rid = _seed_route_record("AGENT_SKILLS", name="meeting-summarizer", status="DEPRECATED")
    sid = _stage_zip(client)
    res = client.put(f"/api/registry/records/{rid}", json={"staging_id": sid})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.not_editable"
    assert sid in registry_router._staging  # kept so the user can retry


def test_put_returns_record_out_shape(client, monkeypatch):
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    rid = _seed_route_record("AGENT_SKILLS", name="meeting-summarizer", version="1.0.0-skill",
                             status="APPROVED")
    res = client.put(
        f"/api/registry/records/{rid}",
        json={"description": "d", "skill_md": "---\nname: meeting-summarizer\n---\n# x"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["record_id"] == rid
    assert body["type"] == "AGENT_SKILLS"
    assert body["version"] == "1.1.0-skill"
    assert body["status"] == "DRAFT"
