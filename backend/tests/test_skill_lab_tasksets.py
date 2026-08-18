"""Skill Lab task sets: CRUD, real-validator parity, atomic replace."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from app.skill_lab import tasksets as svc

VALID_TASK = {"id": "task_001", "question": "Do the thing.", "rubric": "PASS if done."}


@pytest.fixture
def lab(tmp_path, monkeypatch, client):
    """Isolated artifact dir + a real validator running on this interpreter
    (load_tasks' import chain is dependency-free — true CLI parity, no venv)."""
    monkeypatch.setattr(svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        svc, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )
    return client


def _create(client, **overrides):
    body = {
        "name": "demo",
        "mode": "single",
        "tasks_by_split": {"tasks": [VALID_TASK]},
    }
    body.update(overrides)
    return client.post("/api/skill-lab/tasksets", json=body)


# ── create ─────────────────────────────────────────────────────────────────


def test_create_single_and_list(lab):
    created = _create(lab)
    assert created.status_code == 201, created.text
    info = created.json()
    assert info["mode"] == "single" and info["counts"] == {"tasks": 1}
    listed = lab.get("/api/skill-lab/tasksets").json()
    assert [row["id"] for row in listed] == [info["id"]]
    # the stored artifact is the skillopt-native file, byte-preserving
    stored = json.loads((svc.taskset_dir(info["id"]) / "tasks.json").read_text())
    assert stored == [VALID_TASK]


def test_create_split_requires_train_and_val(lab):
    response = _create(
        lab, mode="split", tasks_by_split={"train": [VALID_TASK]}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "skill_lab.taskset_invalid"

    ok = _create(
        lab,
        mode="split",
        tasks_by_split={
            "train": [VALID_TASK],
            "val": [dict(VALID_TASK, id="task_002")],
        },
    )
    assert ok.status_code == 201
    assert ok.json()["counts"] == {"train": 1, "val": 1}


def test_row_level_validation_errors_surface_the_cli_message(lab):
    bad = [VALID_TASK, {"id": "task_002", "question": "q"}]  # missing rubric
    response = _create(lab, tasks_by_split={"tasks": bad})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["split"] == "tasks"
    # load_tasks locators are 0-indexed: the second item reports as "item #1"
    assert "item #1 (id='task_002')" in detail[0]["message"]
    assert "rubric" in detail[0]["message"]
    # nothing landed on disk and nothing landed in the ledger
    assert lab.get("/api/skill-lab/tasksets").json() == []
    assert not list(svc.TASKSETS_DIR.glob("ts_*"))


def test_duplicate_and_unsafe_ids_rejected(lab):
    dup = [VALID_TASK, dict(VALID_TASK)]
    assert _create(lab, tasks_by_split={"tasks": dup}).status_code == 422
    unsafe = [dict(VALID_TASK, id="../escape")]
    response = _create(lab, tasks_by_split={"tasks": unsafe})
    assert response.status_code == 422


def test_shape_rejects_leave_no_staging_debris(lab):
    """A reject on the *second* split must not leave the first one's staging dir."""
    response = _create(
        lab, mode="split", tasks_by_split={"train": [VALID_TASK], "val": []}
    )
    assert response.status_code == 422
    assert not list(svc.TASKSETS_DIR.glob(".staging-*"))
    assert not list(svc.TASKSETS_DIR.glob("ts_*"))


def test_name_longer_than_the_column_is_refused(lab):
    too_long = _create(lab, name="n" * 97)
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "validation.invalid_request"
    info = _create(lab).json()
    renamed = lab.put(
        f"/api/skill-lab/tasksets/{info['id']}",
        json={"name": "n" * 97, "tasks_by_split": {"tasks": [VALID_TASK]}},
    )
    assert renamed.status_code == 422


def test_blank_rename_is_refused_not_silently_ignored(lab):
    info = _create(lab).json()
    response = lab.put(
        f"/api/skill-lab/tasksets/{info['id']}",
        json={"name": "   ", "tasks_by_split": {"tasks": [VALID_TASK]}},
    )
    assert response.status_code == 422
    assert lab.get("/api/skill-lab/tasksets").json()[0]["name"] == "demo"


def test_validator_crash_is_a_500_and_cleans_up(lab, monkeypatch):
    monkeypatch.setattr(svc, "VALIDATOR_SCRIPT", svc.TASKSETS_DIR / "no_such_validator.py")
    response = _create(lab)
    assert response.status_code == 500
    assert response.json()["code"] == "skill_lab.validator_error"
    assert not list(svc.TASKSETS_DIR.glob(".staging-*"))


# ── read ───────────────────────────────────────────────────────────────────


def test_preview_caps_and_full_returns_everything(lab):
    tasks = [dict(VALID_TASK, id=f"task_{i:03d}") for i in range(1, 26)]
    info = _create(lab, tasks_by_split={"tasks": tasks}).json()
    preview = lab.get(f"/api/skill-lab/tasksets/{info['id']}").json()
    assert len(preview["tasks_by_split"]["tasks"]) == svc.PREVIEW_CAP
    assert preview["truncated"] is True
    full = lab.get(f"/api/skill-lab/tasksets/{info['id']}?full=true").json()
    assert len(full["tasks_by_split"]["tasks"]) == 25
    assert full["truncated"] is False


def test_preview_cap_boundary_is_not_truncated(lab):
    tasks = [dict(VALID_TASK, id=f"task_{i:03d}") for i in range(svc.PREVIEW_CAP)]
    info = _create(lab, tasks_by_split={"tasks": tasks}).json()
    preview = lab.get(f"/api/skill-lab/tasksets/{info['id']}").json()
    assert len(preview["tasks_by_split"]["tasks"]) == svc.PREVIEW_CAP
    assert preview["truncated"] is False


def test_get_unknown_404(lab):
    assert lab.get("/api/skill-lab/tasksets/ts_nope").status_code == 404
    assert lab.get("/api/skill-lab/tasksets/..").status_code == 404


# ── update ─────────────────────────────────────────────────────────────────


def test_update_round_trip_preserves_unknown_fields(lab):
    rich = dict(
        VALID_TASK,
        files={"input/data.txt": "hello"},
        task_type="report",
        custom_meta={"origin": "user", "priority": 3},
    )
    info = _create(lab, tasks_by_split={"tasks": [rich]}).json()
    full = lab.get(f"/api/skill-lab/tasksets/{info['id']}?full=true").json()
    tasks = full["tasks_by_split"]["tasks"]
    assert tasks == [rich]  # extras survived the store/read round trip

    tasks.append(dict(VALID_TASK, id="task_002"))
    updated = lab.put(
        f"/api/skill-lab/tasksets/{info['id']}",
        json={"name": "renamed", "tasks_by_split": {"tasks": tasks}},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "renamed"
    assert updated.json()["counts"] == {"tasks": 2}
    again = lab.get(f"/api/skill-lab/tasksets/{info['id']}?full=true").json()
    assert again["tasks_by_split"]["tasks"][0] == rich  # untouched fields identical


def test_split_update_can_drop_the_optional_test_split(lab):
    """PUT is a full replace: a split left out of the body stops existing."""
    three = {
        "train": [VALID_TASK],
        "val": [dict(VALID_TASK, id="task_002")],
        "test": [dict(VALID_TASK, id="task_003")],
    }
    info = _create(lab, mode="split", tasks_by_split=three).json()
    assert info["counts"] == {"train": 1, "val": 1, "test": 1}
    assert (svc.taskset_dir(info["id"]) / "test.json").exists()

    kept = {"train": three["train"], "val": three["val"]}
    updated = lab.put(f"/api/skill-lab/tasksets/{info['id']}", json={"tasks_by_split": kept}).json()
    assert updated["counts"] == {"train": 1, "val": 1}
    assert not (svc.taskset_dir(info["id"]) / "test.json").exists()
    reread = lab.get(f"/api/skill-lab/tasksets/{info['id']}?full=true").json()
    assert set(reread["tasks_by_split"]) == {"train", "val"}


def test_update_mode_is_immutable(lab):
    info = _create(lab).json()  # single
    response = lab.put(
        f"/api/skill-lab/tasksets/{info['id']}",
        json={"tasks_by_split": {"train": [VALID_TASK], "val": [VALID_TASK]}},
    )
    assert response.status_code == 422


def test_failed_update_leaves_old_content(lab):
    info = _create(lab).json()
    bad = lab.put(
        f"/api/skill-lab/tasksets/{info['id']}",
        json={"tasks_by_split": {"tasks": [{"id": "x", "question": "q"}]}},
    )
    assert bad.status_code == 422
    unchanged = lab.get(f"/api/skill-lab/tasksets/{info['id']}?full=true").json()
    assert unchanged["tasks_by_split"]["tasks"] == [VALID_TASK]
    assert not list(svc.TASKSETS_DIR.glob(".staging-*"))  # staging cleaned up


# ── delete / status / provisioning ─────────────────────────────────────────


def test_delete_removes_row_and_artifacts(lab):
    info = _create(lab).json()
    directory = svc.taskset_dir(info["id"])
    assert directory.exists()
    assert lab.delete(f"/api/skill-lab/tasksets/{info['id']}").json() == {"ok": True}
    assert not directory.exists()
    assert lab.get(f"/api/skill-lab/tasksets/{info['id']}").status_code == 404


def test_missing_interpreter_returns_503(lab, monkeypatch):
    monkeypatch.setattr(
        svc, "get_settings", lambda: SimpleNamespace(skill_lab_python="/nonexistent/python")
    )
    response = _create(lab)
    assert response.status_code == 503
    assert response.json()["code"] == "skill_lab.not_provisioned"


def test_status_reflects_worker_resource_keys(lab):
    from tests.conftest import set_default_resources

    # the dev box's real launchpad.yaml may carry live worker keys via the
    # settings→default-row mirror, so pin the row explicitly both ways
    set_default_resources({})
    status = lab.get("/api/skill-lab/status").json()
    assert status["provisioned"] is False
    assert "skill_lab_worker_runtime_arn" in status["missing"]

    set_default_resources(
        {
            "skill_lab_worker_runtime_arn": "arn:rt",
            "skill_lab_worker_role_arn": "arn:role",
            "skill_lab_worker_image_digest": "sha256:x",
        }
    )
    status = lab.get("/api/skill-lab/status").json()
    assert status["provisioned"] is True and status["missing"] == []


def test_stored_artifact_is_directly_consumable_by_load_tasks(lab):
    """Gate G2: the file we store IS what `evaluate_skill.py --tasks` reads."""
    info = _create(lab).json()
    artifact = svc.taskset_dir(info["id"]) / "tasks.json"
    import subprocess

    from app.skill_lab.worker_build import VENDOR_ROOT

    proc = subprocess.run(
        [sys.executable, str(VENDOR_ROOT / "scripts" / "validate_tasks.py"), str(artifact)],
        capture_output=True,
        text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
    )
    result = json.loads(proc.stdout)["results"][0]
    assert result["ok"] is True and result["count"] == 1


def test_workspace_scoping_hides_other_workspaces_rows(lab):
    from app.core.db import SessionLocal
    from app.skill_lab.models import SkillLabTaskset

    info = _create(lab).json()
    db = SessionLocal()
    try:
        row = db.get(SkillLabTaskset, info["id"])
        row.workspace_id = "other-ws"
        db.commit()
    finally:
        db.close()
    assert lab.get("/api/skill-lab/tasksets").json() == []
    assert lab.get(f"/api/skill-lab/tasksets/{info['id']}").status_code == 404
