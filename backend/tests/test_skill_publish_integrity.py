"""Skill record write integrity (rev-r23 review of cn/r17-ao, 2026-10-22).

* **N17-obs263** — Skill Lab publish rewrote ANY visible skill record: the route checked
  only the JOB's creator-or-admin rule, ``publish_job`` only the system-preset refusal, then
  ``update_record`` pushed bytes into ``skills/<name>/`` and settled an APPROVED record into
  DRAFT. Publish now applies the SOURCE record's creator-or-admin rule (N15-obs7,
  ``registry.forbidden`` with ``action: "publish"``) before any split or write.
* **N17-obs264** — ``upload_skill_bundle`` (the platform sample) wrote over its prefix
  without removing keys the bundle no longer ships, and every consumer downloads the whole
  prefix — a stale file survived a refresh and shipped. It now prunes the prefix to the
  bundle's file list after the upload.
* **N17-obs265** — ``register_skill_bundle`` uploaded before ``upsert_record`` (create-if-
  absent, ``created=False`` on a same-name row): a record created between the name check
  and the upsert got its prefix overwritten and the caller got that record back. The record
  is now claimed first (``created=False`` → 409 ``registry.name_exists``, nothing uploaded)
  and the platform sample's name is reserved (409 ``registry.sample_name_reserved``).

Real LocalRegistryBackend on the hermetic ledger, an in-memory S3 — no AWS.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import aws_clients, registry_console
from app.services import skill_ingest as si
from tests import test_visibility as tv
from tests.conftest import set_default_resources, ws_ctx

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

SEED = "---\nname: {name}\n---\n# seed\n"
BEST = "---\nname: {name}\n---\n# seed\n\nIMPROVED by training\n"


class MemS3:
    """The S3 calls the registry console makes, over an in-memory key → bytes map."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.refuse: set[str] = set()

    def upload_file(self, src, bucket, key):
        with open(src, "rb") as fh:
            self.objects[key] = fh.read()

    def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803 — boto kwargs
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        store = self

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                keys = sorted(k for k in store.objects if k.startswith(Prefix))
                yield {"Contents": [{"Key": k} for k in keys]}

        return _P()

    def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803
        errors = []
        for obj in Delete.get("Objects", []):
            if obj["Key"] in self.refuse:
                errors.append({"Key": obj["Key"], "Code": "AccessDenied"})
            else:
                self.objects.pop(obj["Key"], None)
        return {"Errors": errors}

    def under(self, prefix: str) -> dict[str, bytes]:
        return {k: v for k, v in self.objects.items() if k.startswith(prefix)}


@pytest.fixture
def s3(monkeypatch) -> MemS3:
    fake = MemS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    set_default_resources({"artifacts_bucket": "bkt"})
    return fake


# ─── N17-obs263: publish is the SOURCE record's creator-or-admin ─────────────────────


def _record(name: str, created_by: str | None, status: str, s3: MemS3) -> str:
    from app.models.ledger import RegistryRecord
    from app.services.registry_backend import build_skills_descriptors

    md = SEED.format(name=name)
    s3.objects[f"skills/{name}/SKILL.md"] = md.encode()
    s3.objects[f"skills/{name}/references/note.md"] = b"support"
    return tv._add(RegistryRecord(
        record_type="AGENT_SKILLS", name=name, description="d", record_version="1.0.0-skill",
        status=status, created_by=created_by, created_by_name="someone" if created_by else None,
        descriptors=build_skills_descriptors(skill_md=md, definition={
            "name": name, "version": "1.0.0", "path": f"s3://bkt/skills/{name}/",
            "files": ["SKILL.md", "references/note.md"], "source": {"kind": "inline"}}),
    ))


def _trained_job(created_by: str | None, record_id: str, name: str) -> str:
    """A succeeded training job whose best_skill.md differs from its seed."""
    from app.skill_lab import artifacts
    from app.skill_lab.models import SkillLabJob

    job_id = tv._add(SkillLabJob(
        type="train", status="succeeded", taskset_id="ts-x", created_by=created_by,
        skill_source={"kind": "registry", "record_id": record_id, "name": name}, params={},
    ))
    out = artifacts.out_root(job_id)
    (out / "skills").mkdir(parents=True)
    (out / "skills" / "skill_v0000.md").write_text(SEED.format(name=name))
    (out / "best_skill.md").write_text(BEST.format(name=name))
    return job_id


@pytest.fixture
def lab(monkeypatch, tmp_path, s3):
    from app.skill_lab import artifacts

    monkeypatch.setattr(artifacts, "JOBS_DIR", tmp_path / "jobs")
    return s3


def _snapshot(admin_client, rid: str, s3: MemS3, name: str) -> tuple:
    rec = admin_client.get(f"/api/registry/records/{rid}").json()
    return rec["status"], rec["version"], rec["descriptors"], s3.under(f"skills/{name}/")


@pytest.mark.parametrize("owner", ["platform", "admin-import", "bob"])
def test_member_publish_onto_a_record_they_did_not_create_is_refused(
    lab, admin, ada, bob, owner
):
    """The platform sample (NULL creator, APPROVED), an administrator-imported skill (NULL
    creator) and another member's skill: ada trains on each (reads are shared) — publishing
    is 403 ``registry.forbidden`` and the record's bytes, version and status are unchanged."""
    ada_id, ada_client = ada
    bob_id, _ = bob
    name = {"platform": registry_console.SKILL_NAME, "admin-import": "imported-skill",
            "bob": "bob-skill"}[owner]
    rid = _record(name, bob_id if owner == "bob" else None, "APPROVED", lab)
    job = _trained_job(ada_id, rid, name)
    before = _snapshot(admin, rid, lab, name)
    res = ada_client.post(f"/api/skill-lab/jobs/{job}/publish", json={"reapprove": True})
    assert res.status_code == 403, res.text
    body = res.json()
    assert body["code"] == "registry.forbidden", body
    assert body["detail"]["record_id"] == rid and body["detail"]["action"] == "publish"
    assert "publish" in body["message"]  # N17-obs269: the copy names the refused action
    assert body["detail"]["created_by"] == (bob_id if owner == "bob" else None)
    assert _snapshot(admin, rid, lab, name) == before
    assert before[0] == "APPROVED"


def test_member_publish_onto_their_own_record_works(lab, admin, ada):
    ada_id, ada_client = ada
    rid = _record("ada-skill", ada_id, "DRAFT", lab)
    job = _trained_job(ada_id, rid, "ada-skill")
    res = ada_client.post(f"/api/skill-lab/jobs/{job}/publish", json={})
    assert res.status_code == 200, res.text
    assert res.json()["new_version"] == "1.1.0-skill"
    assert lab.objects["skills/ada-skill/SKILL.md"].decode().endswith("IMPROVED by training\n")


@pytest.mark.parametrize("owner", ["platform", "bob"])
def test_admin_publish_works_on_any_record(lab, admin, bob, owner):
    bob_id, _ = bob
    name = "platform-skill" if owner == "platform" else "bob-skill"
    rid = _record(name, bob_id if owner == "bob" else None, "APPROVED", lab)
    job = _trained_job(None, rid, name)
    res = admin.post(f"/api/skill-lab/jobs/{job}/publish", json={"reapprove": True})
    assert res.status_code == 200, res.text
    assert res.json()["status_after"] == "APPROVED"
    assert lab.objects[f"skills/{name}/SKILL.md"].decode().endswith("IMPROVED by training\n")


def test_the_record_refusal_runs_before_the_multi_file_split(lab, admin, ada, monkeypatch):
    """Bundle mode (``trainable_files``) rebuilds publish_skill/ and replaces the whole
    prefix — a forbidden caller never reaches the split, let alone the S3 sweep."""
    from app.skill_lab import jobs as jobs_svc
    from app.skill_lab.models import SkillLabJob

    ada_id, ada_client = ada
    rid = _record("admin-skill", None, "APPROVED", lab)
    job = _trained_job(ada_id, rid, "admin-skill")
    db = tv.SessionLocal()
    try:
        row = db.get(SkillLabJob, job)
        row.params = {"trainable_files": ["references/note.md"]}
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(jobs_svc, "_split_best_bundle",
                        lambda *_a: pytest.fail("the split ran for a forbidden caller"))
    before = lab.under("skills/admin-skill/")
    res = ada_client.post(f"/api/skill-lab/jobs/{job}/publish", json={})
    assert res.status_code == 403 and res.json()["code"] == "registry.forbidden", res.text
    assert lab.under("skills/admin-skill/") == before


# ─── N17-obs264: a complete-bundle platform write leaves no stale key ────────────────


def test_sample_bundle_upload_prunes_keys_the_bundle_does_not_ship(s3):
    ws = ws_ctx({"artifacts_bucket": "bkt"})
    prefix = f"skills/{registry_console.SKILL_NAME}/"
    s3.objects[f"{prefix}scripts/old_release_helper.py"] = b"stale"
    s3.objects[f"{prefix}SKILL.md"] = b"old"
    s3.objects["skills/other-skill/SKILL.md"] = b"untouched"  # a sibling prefix
    out = registry_console.upload_skill_bundle(ws)
    shipped = {f"{prefix}{rel}" for rel in out["definition"]["files"]}
    assert set(s3.under(prefix)) == shipped
    assert f"{prefix}scripts/old_release_helper.py" not in s3.objects
    assert s3.objects["skills/other-skill/SKILL.md"] == b"untouched"
    assert s3.objects[f"{prefix}SKILL.md"] != b"old"


def test_sample_bundle_upload_raises_on_a_refused_stale_key(s3):
    """A stale key S3 refuses to delete would still ship — the refresh fails loudly
    (S3SweepResidue: counts and codes only) instead of reporting success."""
    from app.services.s3_sweep import S3SweepResidue

    ws = ws_ctx({"artifacts_bucket": "bkt"})
    stale = f"skills/{registry_console.SKILL_NAME}/stale.md"
    s3.objects[stale] = b"stale"
    s3.refuse.add(stale)
    with pytest.raises(S3SweepResidue) as exc:
        registry_console.upload_skill_bundle(ws)
    assert "AccessDenied" in str(exc.value) and stale not in str(exc.value)


# ─── N17-obs265: register claims the name before any upload ──────────────────────────


@pytest.fixture
def local_registry():
    from app.services import registry_backend
    from app.services.registry_local import LocalRegistryBackend

    registry_backend.set_backend(LocalRegistryBackend())
    return registry_backend.get_backend()


def _bundle(name: str) -> si.SkillBundle:
    return si.bundle_from_inline(f"---\nname: {name}\ndescription: d\n---\n# member bytes\n")


def test_a_record_created_between_the_name_check_and_the_claim_is_a_409(
    s3, monkeypatch, local_registry
):
    """Race: ``_require_new_name`` passes, then a platform row with the same name appears
    (a defaults sync) before ``upsert_record`` — the member gets 409 ``registry.name_exists``,
    the other record's prefix is untouched and its row is not handed back."""
    from app.services import registry_backend

    ws = ws_ctx({"artifacts_bucket": "bkt"})
    backend = local_registry
    platform_md = b"---\nname: shared-name\n---\n# platform bytes\n"
    s3.objects["skills/shared-name/SKILL.md"] = platform_md
    real_check = registry_console._require_new_name
    holder: dict[str, Any] = {}

    def check_then_race(be, w, name, kind):
        real_check(be, w, name, kind)
        holder["rec"], _ = backend.upsert_record(  # the concurrent platform write
            w, name=name, description="platform", descriptor_type="AGENT_SKILLS",
            descriptors=registry_backend.build_skills_descriptors(
                skill_md=platform_md.decode(), definition={"name": name}),
        )

    monkeypatch.setattr(registry_console, "_require_new_name", check_then_race)
    bundle = _bundle("shared-name")
    try:
        with pytest.raises(registry_console.AppError) as exc:
            registry_console.register_skill_bundle(bundle, ws)
    finally:
        bundle.close()
    assert (exc.value.status_code, exc.value.code) == (409, "registry.name_exists")
    assert s3.under("skills/shared-name/") == {"skills/shared-name/SKILL.md": platform_md}
    kept = backend.get_record(ws, holder["rec"]["recordId"])
    assert kept["description"] == "platform"


def test_register_refuses_the_platform_sample_name(s3, admin, ada):
    """The sample skill's name is the defaults sync's: an ordinary register (paste or
    import, member or administrator) is refused before any lookup or upload."""
    _ada_id, ada_client = ada
    name = registry_console.SKILL_NAME
    for client in (ada_client, admin):
        res = client.post("/api/registry/records", json={
            "type": "AGENT_SKILLS", "name": name, "description": "d",
            "skill_md": f"---\nname: {name}\n---\n# mine\n",
        })
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "registry.sample_name_reserved"
    assert s3.under(f"skills/{name}/") == {}
    assert registry_console.sample_skill_records(
        registry_console._backend(), ws_ctx({"artifacts_bucket": "bkt"})) == []


def test_a_failed_upload_after_the_claim_drops_the_record_and_its_objects(
    s3, monkeypatch, local_registry
):
    ws = ws_ctx({"artifacts_bucket": "bkt"})

    def half_upload(bundle, bucket, prefix, s3_client, *, uploaded):
        s3.objects[f"{prefix}SKILL.md"] = b"partial"
        uploaded.append(f"{prefix}SKILL.md")
        raise RuntimeError("S3 down mid-upload")

    monkeypatch.setattr(registry_console, "upload_bundle_files", half_upload)
    bundle = _bundle("half-skill")
    try:
        with pytest.raises(RuntimeError):
            registry_console.register_skill_bundle(bundle, ws)
    finally:
        bundle.close()
    assert s3.under("skills/half-skill/") == {}
    assert local_registry.find_record(ws, "half-skill", "AGENT_SKILLS") is None


# ─── N17-obs269: a new record never inherits a deleted same-name record's residue ─────


def test_register_prunes_residue_a_deleted_same_name_record_left(s3, local_registry):
    """rev-r23 L2: ``register_skill_bundle`` wrote over ``skills/<name>/`` without removing
    keys outside the bundle — a refused delete of an earlier same-name record left
    ``scripts/old.py`` behind, and every deploy downloading the prefix shipped it with the
    new record. Now the prefix is pruned to the bundle's files after the upload."""
    ws = ws_ctx({"artifacts_bucket": "bkt"})
    s3.objects["skills/reused/scripts/old.py"] = b"residue of a deleted record"
    s3.objects["skills/reused/SKILL.md"] = b"old bytes"
    s3.objects["skills/reused-two/SKILL.md"] = b"a sibling prefix"
    bundle = _bundle("reused")
    try:
        record = registry_console.register_skill_bundle(bundle, ws)
    finally:
        bundle.close()
    assert set(s3.under("skills/reused/")) == {"skills/reused/SKILL.md"}
    assert s3.objects["skills/reused/SKILL.md"] != b"old bytes"
    assert s3.objects["skills/reused-two/SKILL.md"] == b"a sibling prefix"
    assert record["name"] == "reused"


def test_register_rolls_back_when_the_residue_cannot_be_deleted(s3, local_registry):
    """A residue key S3 refuses would ship with the new record: the registration fails
    (``S3SweepResidue`` — counts and codes, never a key) and is rolled back like a failed
    upload: the uploaded files and the claimed record are gone, only the refused key stays."""
    from app.services.s3_sweep import S3SweepResidue

    ws = ws_ctx({"artifacts_bucket": "bkt"})
    stale = "skills/stuck/scripts/old.py"
    s3.objects[stale] = b"residue"
    s3.refuse.add(stale)
    bundle = _bundle("stuck")
    try:
        with pytest.raises(S3SweepResidue) as exc:
            registry_console.register_skill_bundle(bundle, ws)
    finally:
        bundle.close()
    assert "AccessDenied" in str(exc.value) and stale not in str(exc.value)
    assert set(s3.under("skills/stuck/")) == {stale}
    assert local_registry.find_record(ws, "stuck", "AGENT_SKILLS") is None
