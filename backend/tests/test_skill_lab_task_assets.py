from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.db import SessionLocal
from app.skill_lab import task_assets, tasksets

VALID_TASK = {"id": "asset_1", "question": "Inspect inputs", "rubric": "PASS"}


def _xlsx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return out.getvalue()


@pytest.fixture
def lab(tmp_path, monkeypatch, client):
    monkeypatch.setattr(task_assets, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(tasksets, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        tasksets, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )
    return client


def _upload(client, files):
    return client.post("/api/skill-lab/task-assets", files=files)


def test_multipart_stages_supported_files_exactly_and_commits_canonical_descriptors(lab):
    pdf = b"%PDF-1.7\nasset\x00bytes"
    xlsx = _xlsx()
    response = _upload(
        lab,
        [
            ("files", ("source.xlsx", xlsx, "application/octet-stream")),
            ("files", ("brief.pdf", pdf, "text/plain")),
        ],
    )
    assert response.status_code == 201, response.text
    staged = response.json()["assets"]
    assert [asset["name"] for asset in staged] == ["source.xlsx", "brief.pdf"]
    body = {
        "name": "assets",
        "mode": "single",
        "tasks_by_split": {
            "tasks": [
                {
                    **VALID_TASK,
                    "unknown": {"kept": True},
                    "files": {
                        "data/source.xlsx": staged[0],
                        "docs/brief.pdf": staged[1],
                        "notes/readme.txt": "legacy text",
                    },
                }
            ]
        },
    }
    created = lab.post("/api/skill-lab/tasksets", json=body)
    assert created.status_code == 201, created.text
    detail = lab.get(f"/api/skill-lab/tasksets/{created.json()['id']}?full=true").json()
    task = detail["tasks_by_split"]["tasks"][0]
    assert task["unknown"] == {"kept": True}
    assert task["files"]["notes/readme.txt"] == "legacy text"
    for destination, expected in (("data/source.xlsx", xlsx), ("docs/brief.pdf", pdf)):
        descriptor = task["files"][destination]
        assert set(descriptor) == {"asset", "name", "media_type", "size"}
        digest = descriptor["asset"].split(":", 1)[1]
        assert (
            tasksets.taskset_dir(created.json()["id"]) / "assets" / digest
        ).read_bytes() == expected


def test_security_workspace_isolation_and_stable_asset_ownership(lab, monkeypatch):
    staged = _upload(lab, [("files", ("a.pdf", b"%PDF-1.4\none", "application/pdf"))]).json()[
        "assets"
    ][0]
    monkeypatch.setattr(
        task_assets,
        "workspace_key",
        lambda workspace_id: hashlib.sha256(workspace_id.encode()).hexdigest()[:32],
    )
    with pytest.raises(Exception) as exc:
        task_assets.resolve_staged("another-workspace", staged["staged_asset"])
    assert getattr(exc.value, "code", "") == "skill_lab.asset_token_not_found"

    bad = dict(VALID_TASK, files={"../escape.pdf": staged})
    response = lab.post(
        "/api/skill-lab/tasksets",
        json={"name": "bad", "mode": "single", "tasks_by_split": {"tasks": [bad]}},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "skill_lab.asset_path_invalid"


def test_invalid_format_duplicate_name_missing_token_and_update_removes_omitted_asset(lab):
    mismatch = _upload(lab, [("files", ("fake.pdf", b"not pdf", "application/pdf"))])
    assert (
        mismatch.status_code == 422 and mismatch.json()["code"] == "skill_lab.asset_format_invalid"
    )
    duplicate = _upload(
        lab,
        [
            ("files", ("A.pdf", b"%PDF-a", "application/pdf")),
            ("files", ("a.PDF", b"%PDF-b", "application/pdf")),
        ],
    )
    assert (
        duplicate.status_code == 422
        and duplicate.json()["code"] == "skill_lab.asset_duplicate_name"
    )
    missing = dict(
        VALID_TASK,
        files={
            "data/missing.pdf": {
                "staged_asset": "ta_missing",
                "name": "x",
                "media_type": "application/pdf",
                "size": 1,
            }
        },
    )
    response = lab.post(
        "/api/skill-lab/tasksets",
        json={"name": "missing", "mode": "single", "tasks_by_split": {"tasks": [missing]}},
    )
    assert (
        response.status_code == 404 and response.json()["code"] == "skill_lab.asset_token_not_found"
    )

    staged = _upload(lab, [("files", ("one.pdf", b"%PDF-one", "application/pdf"))]).json()[
        "assets"
    ][0]
    created = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "keep",
            "mode": "single",
            "tasks_by_split": {"tasks": [dict(VALID_TASK, files={"data/one.pdf": staged})]},
        },
    ).json()
    detail = lab.get(f"/api/skill-lab/tasksets/{created['id']}?full=true").json()
    stable = detail["tasks_by_split"]["tasks"][0]["files"]["data/one.pdf"]
    kept = lab.put(
        f"/api/skill-lab/tasksets/{created['id']}",
        json={"tasks_by_split": {"tasks": [dict(VALID_TASK, files={"renamed/one.pdf": stable})]}},
    )
    assert kept.status_code == 200, kept.text
    removed = lab.put(
        f"/api/skill-lab/tasksets/{created['id']}", json={"tasks_by_split": {"tasks": [VALID_TASK]}}
    )
    assert removed.status_code == 200
    assert not (tasksets.taskset_dir(created["id"]) / "assets").exists()


def test_limits_are_centralized_and_enforced(lab, monkeypatch):
    monkeypatch.setattr(task_assets, "MAX_FILE_BYTES", 4)
    response = _upload(lab, [("files", ("large.pdf", b"%PDF-too-large", "application/pdf"))])
    assert response.status_code == 413
    assert response.json()["code"] == "skill_lab.asset_too_large"
    assert task_assets.MAX_FILES_PER_UPLOAD == task_assets.MAX_FILES_PER_TASK == 32
    assert task_assets.MAX_TASKSET_REFERENCES == 256


def test_chunked_style_upload_still_enforces_the_aggregate_stream_limit(lab, monkeypatch):
    monkeypatch.setattr(task_assets, "MAX_TASK_BYTES", 12)
    response = _upload(
        lab,
        [
            ("files", ("a.pdf", b"%PDF-one", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-two", "application/pdf")),
        ],
    )
    assert response.status_code == 413
    assert response.json()["code"] == "skill_lab.asset_limit_exceeded"
    assert not list(task_assets.STAGING_DIR.glob("*/*/metadata.json"))


def test_job_snapshot_is_immutable_and_verified(lab, tmp_path):
    staged = _upload(lab, [("files", ("one.pdf", b"%PDF-immutable", "application/pdf"))]).json()[
        "assets"
    ][0]
    created = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "snapshot",
            "mode": "single",
            "tasks_by_split": {"tasks": [dict(VALID_TASK, files={"data/one.pdf": staged})]},
        },
    ).json()
    snapshot = tasksets.snapshot_taskset(created["id"], tmp_path / "job" / "inputs")
    before = (snapshot / "tasks.json").read_bytes()
    update = lab.put(
        f"/api/skill-lab/tasksets/{created['id']}",
        json={"tasks_by_split": {"tasks": [dict(VALID_TASK, question="changed")]}},
    )
    assert update.status_code == 200
    assert (snapshot / "tasks.json").read_bytes() == before
    assert (snapshot / "assets").is_dir()


def test_all_supported_signatures_and_upload_metadata(lab):
    fixtures = (
        ("book.xlsx", _xlsx(), task_assets._MEDIA["xlsx"]),
        ("doc.pdf", b"%PDF-1.7\nbytes", task_assets._MEDIA["pdf"]),
        ("image.png", b"\x89PNG\r\n\x1a\nbytes", task_assets._MEDIA["png"]),
        ("photo.jpg", b"\xff\xd8\xffbytes", task_assets._MEDIA["jpg"]),
        ("photo.jpeg", b"\xff\xd8\xffmore", task_assets._MEDIA["jpg"]),
        ("image.webp", b"RIFF\x04\x00\x00\x00WEBP", task_assets._MEDIA["webp"]),
    )
    response = _upload(
        lab,
        [("files", (name, data, "application/octet-stream")) for name, data, _ in fixtures],
    )
    assert response.status_code == 201, response.text
    assert [(row["name"], row["media_type"], row["size"]) for row in response.json()["assets"]] == [
        (name, media, len(data)) for name, data, media in fixtures
    ]


def test_expired_token_has_stable_error_and_sweep_removes_it(lab):
    staged = _upload(lab, [("files", ("old.pdf", b"%PDF-old", "application/pdf"))]).json()[
        "assets"
    ][0]
    metadata_path = next(task_assets.STAGING_DIR.glob("*/*/metadata.json"))
    metadata = json.loads(metadata_path.read_text())
    metadata["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(Exception) as exc:
        task_assets.resolve_staged("default", staged["staged_asset"])
    assert getattr(exc.value, "code", "") == "skill_lab.asset_token_expired"
    stage_dir = metadata_path.parent
    task_assets.sweep_expired()
    assert not stage_dir.exists()


def test_limits_for_upload_task_and_taskset_are_enforced(lab, monkeypatch):
    monkeypatch.setattr(task_assets, "MAX_FILES_PER_UPLOAD", 1)
    response = _upload(
        lab,
        [
            ("files", ("a.pdf", b"%PDF-a", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-b", "application/pdf")),
        ],
    )
    assert response.status_code == 422
    assert response.json()["code"] == "skill_lab.asset_limit_exceeded"

    monkeypatch.setattr(task_assets, "MAX_FILES_PER_UPLOAD", 32)
    monkeypatch.setattr(task_assets, "MAX_FILES_PER_TASK", 1)
    staged = _upload(
        lab,
        [
            ("files", ("a.pdf", b"%PDF-a", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-b", "application/pdf")),
        ],
    ).json()["assets"]
    response = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "too-many",
            "mode": "single",
            "tasks_by_split": {
                "tasks": [
                    dict(VALID_TASK, files={"data/a.pdf": staged[0], "data/b.pdf": staged[1]})
                ]
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "skill_lab.taskset_invalid"

    monkeypatch.setattr(task_assets, "MAX_FILES_PER_TASK", 32)
    monkeypatch.setattr(task_assets, "MAX_TASKSET_REFERENCES", 1)
    response = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "too-many-refs",
            "mode": "single",
            "tasks_by_split": {
                "tasks": [
                    dict(VALID_TASK, files={"data/a.pdf": staged[0], "data/b.pdf": staged[1]})
                ]
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "skill_lab.asset_limit_exceeded"


def test_task_bytes_and_unique_taskset_bytes_are_enforced(lab, monkeypatch):
    staged = _upload(
        lab,
        [
            ("files", ("a.pdf", b"%PDF-a", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-bb", "application/pdf")),
        ],
    ).json()["assets"]
    monkeypatch.setattr(task_assets, "MAX_TASK_BYTES", 5)
    response = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "task-bytes",
            "mode": "single",
            "tasks_by_split": {"tasks": [dict(VALID_TASK, files={"data/a.pdf": staged[0]})]},
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "skill_lab.asset_limit_exceeded"

    monkeypatch.setattr(task_assets, "MAX_TASK_BYTES", 100)
    monkeypatch.setattr(task_assets, "MAX_TASKSET_UNIQUE_BYTES", len(b"%PDF-a"))
    response = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "set-bytes",
            "mode": "single",
            "tasks_by_split": {
                "tasks": [
                    dict(VALID_TASK, files={"data/a.pdf": staged[0], "data/b.pdf": staged[1]})
                ]
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "skill_lab.asset_limit_exceeded"


def test_update_rejects_forged_descriptor_metadata(lab):
    staged = _upload(lab, [("files", ("one.pdf", b"%PDF-owned", "application/pdf"))]).json()[
        "assets"
    ][0]
    created = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "owned",
            "mode": "single",
            "tasks_by_split": {"tasks": [dict(VALID_TASK, files={"data/one.pdf": staged})]},
        },
    ).json()
    stable = lab.get(f"/api/skill-lab/tasksets/{created['id']}?full=true").json()["tasks_by_split"][
        "tasks"
    ][0]["files"]["data/one.pdf"]
    forged = dict(stable, name="forged.pdf")
    response = lab.put(
        f"/api/skill-lab/tasksets/{created['id']}",
        json={"tasks_by_split": {"tasks": [dict(VALID_TASK, files={"data/one.pdf": forged})]}},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "skill_lab.asset_not_owned"


def test_update_commit_failure_restores_json_assets_and_preserves_staged_token(lab, monkeypatch):
    first = _upload(lab, [("files", ("old.pdf", b"%PDF-old", "application/pdf"))]).json()["assets"][
        0
    ]
    created = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "before",
            "mode": "single",
            "tasks_by_split": {"tasks": [dict(VALID_TASK, files={"data/old.pdf": first})]},
        },
    ).json()
    live = tasksets.taskset_dir(created["id"])
    before = {
        path.relative_to(live): path.read_bytes() for path in live.rglob("*") if path.is_file()
    }
    second = _upload(lab, [("files", ("new.pdf", b"%PDF-new", "application/pdf"))]).json()[
        "assets"
    ][0]

    db = SessionLocal()
    real_commit = db.commit
    calls = 0

    def fail_first_commit():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database commit refused")
        return real_commit()

    with patch.object(db, "commit", side_effect=fail_first_commit):
        with pytest.raises(RuntimeError, match="commit refused"):
            tasksets.update_taskset(
                db,
                "default",
                created["id"],
                name="after",
                tasks_by_split={"tasks": [dict(VALID_TASK, files={"data/new.pdf": second})]},
                validator_python=sys.executable,
            )
    db.rollback()
    db.close()

    after = {
        path.relative_to(live): path.read_bytes() for path in live.rglob("*") if path.is_file()
    }
    assert after == before
    metadata_path = next(task_assets.STAGING_DIR.glob("*/*/metadata.json"))
    assert second["staged_asset"] in metadata_path.read_text()
    assert not live.with_name(live.name + ".old").exists()
    detail = lab.get(f"/api/skill-lab/tasksets/{created['id']}?full=true").json()
    assert detail["info"]["name"] == "before"
    assert "data/old.pdf" in detail["tasks_by_split"]["tasks"][0]["files"]


def test_reserved_runtime_roots_are_rejected_case_insensitively(lab):
    for root in (".claude", ".codex", ".git", ".CLAUDE", ".CoDeX", ".GIT"):
        staged = _upload(
            lab, [("files", ("one.pdf", b"%PDF-reserved", "application/pdf"))]
        ).json()["assets"][0]
        response = lab.post(
            "/api/skill-lab/tasksets",
            json={
                "name": f"reserved-{root}",
                "mode": "single",
                "tasks_by_split": {
                    "tasks": [dict(VALID_TASK, files={f"{root}/input.pdf": staged})]
                },
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "skill_lab.asset_path_invalid"


def test_legacy_inline_files_do_not_inherit_binary_count_or_casefold_limits(lab):
    inline = {f"notes/{index:02d}.txt": str(index) for index in range(33)}
    inline.update({"Case/Note.txt": "upper", "case/note.txt": "lower"})
    created = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "legacy-inline",
            "mode": "single",
            "tasks_by_split": {"tasks": [dict(VALID_TASK, files=inline)]},
        },
    )
    assert created.status_code == 201, created.text
    task = lab.get(
        f"/api/skill-lab/tasksets/{created.json()['id']}?full=true"
    ).json()["tasks_by_split"]["tasks"][0]
    assert task["files"] == inline


def test_content_length_limit_rejects_before_multipart_parsing(lab, monkeypatch):
    monkeypatch.setattr(task_assets, "MAX_UPLOAD_REQUEST_BYTES", 1)
    response = _upload(lab, [("files", ("one.pdf", b"%PDF-body", "application/pdf"))])
    assert response.status_code == 413
    assert response.json()["code"] == "skill_lab.asset_request_too_large"
    assert not task_assets.STAGING_DIR.exists()


def test_update_and_snapshot_are_serialized_per_taskset(lab, tmp_path, monkeypatch):
    import threading
    import time

    created = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "serialized",
            "mode": "single",
            "tasks_by_split": {"tasks": [VALID_TASK]},
        },
    ).json()
    taskset_id = created["id"]
    entered_swap = threading.Event()
    release_swap = threading.Event()
    snapshot_done = threading.Event()
    real_swap = tasksets._swap_in

    def paused_swap(staging, live):
        entered_swap.set()
        assert release_swap.wait(5)
        return real_swap(staging, live)

    monkeypatch.setattr(tasksets, "_swap_in", paused_swap)
    update_result = {}

    def update():
        db = SessionLocal()
        try:
            update_result["value"] = tasksets.update_taskset(
                db,
                "default",
                taskset_id,
                tasks_by_split={"tasks": [dict(VALID_TASK, question="after")]},
                validator_python=sys.executable,
            )
        finally:
            db.close()

    def snapshot():
        tasksets.snapshot_taskset(taskset_id, tmp_path / "snapshot")
        snapshot_done.set()

    update_thread = threading.Thread(target=update)
    update_thread.start()
    assert entered_swap.wait(5)
    snapshot_thread = threading.Thread(target=snapshot)
    snapshot_thread.start()
    time.sleep(0.05)
    assert not snapshot_done.is_set()
    release_swap.set()
    update_thread.join(5)
    snapshot_thread.join(5)
    assert not update_thread.is_alive() and not snapshot_thread.is_alive()
    assert update_result["value"]["name"] == "serialized"
    snap_task = json.loads((tmp_path / "snapshot" / "tasks.json").read_text())[0]
    assert snap_task["question"] == "after"
    assert not list(tasksets.TASKSETS_DIR.glob("*.old"))


def test_delete_cleanup_failure_restores_row_and_assets(lab, monkeypatch):
    created = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "cleanup-retry",
            "mode": "single",
            "tasks_by_split": {"tasks": [VALID_TASK]},
        },
    ).json()
    directory = tasksets.taskset_dir(created["id"])
    real_rmtree = tasksets.shutil.rmtree

    def fail_tombstone(path, *args, **kwargs):
        if path.name.startswith(".deleting-"):
            raise OSError("disk cleanup refused")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(tasksets.shutil, "rmtree", fail_tombstone)
    response = lab.delete(f"/api/skill-lab/tasksets/{created['id']}")
    assert response.status_code == 500
    assert response.json()["code"] == "skill_lab.taskset_cleanup_failed"
    assert directory.is_dir()
    detail = lab.get(f"/api/skill-lab/tasksets/{created['id']}?full=true")
    assert detail.status_code == 200
    assert detail.json()["tasks_by_split"]["tasks"] == [VALID_TASK]
