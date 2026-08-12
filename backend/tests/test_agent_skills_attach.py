"""Attach-without-registering: /api/registry/skills/inspect → /api/agent-skills/import.

Uploads land under agent-skills/{uid8}/{name}/ and NO registry record is
created (mocked AWS, no live calls)."""

import io
import types
import zipfile

import app.routers.agent_skills as attach_router
from app.services import aws_clients

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


def _zip(name: str = "meeting-summarizer") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD.replace("meeting-summarizer", name))
        zf.writestr("scripts/helper.py", "print(1)\n")
    return buf.getvalue()


def _patch_aws(monkeypatch, fake_s3):
    monkeypatch.setattr(
        attach_router, "get_settings",
        lambda: types.SimpleNamespace(resources={"artifacts_bucket": "bkt"}, region="us-west-2"),
    )
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake_s3)


def _inspect(client, name: str = "meeting-summarizer") -> str:
    res = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _zip(name), "application/zip")},
    )
    assert res.status_code == 200
    return res.json()["staging_id"]


def test_attach_uploads_without_registry_record(client, monkeypatch):
    fake = FakeS3()
    _patch_aws(monkeypatch, fake)
    registered: list[str] = []
    import app.routers.registry as registry_router

    monkeypatch.setattr(
        registry_router.console, "register_skill_bundle",
        lambda *a, **k: registered.append("called"),
    )

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

    # staging consumed after full success → replay is 410
    again = client.post("/api/agent-skills/import", json={
        "staging_id": sid, "selections": [{"index": 0}],
    })
    assert again.status_code == 410
    assert again.json()["code"] == "registry.staging_expired"


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


def test_attach_unknown_staging_410(client, monkeypatch):
    _patch_aws(monkeypatch, FakeS3())
    res = client.post("/api/agent-skills/import", json={
        "staging_id": "nope", "selections": [{"index": 0}],
    })
    assert res.status_code == 410


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
