"""Skill Lab training jobs: config generation, lifecycle, resume, publish.

Stub train CLI protocol (control rides the SEED SKILL content, materialized by
the fixture): "FAILTRAIN" in the seed → exit 2; otherwise the stub emulates the
vendored trainer's on-disk contract — skills/skill_v0000.md, history.json (one
record per configured epoch), best_skill.md (= seed + IMPROVED), summary.json,
runtime_state.json. On a RESUME (runtime_state.json already present) it appends
one more accepted step and marks the best skill RESUMED.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.core.errors import AppError
from app.skill_lab import artifacts, runner
from app.skill_lab import tasksets as taskset_svc
from tests.conftest import set_default_resources
from tests.test_skill_lab_jobs import WORKER_RESOURCES

STUB_TRAIN = '''
import argparse, json, pathlib, sys
import yaml
parser = argparse.ArgumentParser()
parser.add_argument("--config"); parser.add_argument("--out_root")
args = parser.parse_args()
config = yaml.safe_load(pathlib.Path(args.config).read_text())
seed = pathlib.Path(config["env"]["skill_init"]).read_text()
out = pathlib.Path(args.out_root); (out / "skills").mkdir(parents=True, exist_ok=True)
if "FAILTRAIN" in seed:
    print("trainer exploding", flush=True); sys.exit(2)
(out / "skills" / "skill_v0000.md").write_text(seed)
state_file = out / "runtime_state.json"
if state_file.is_file():  # resume: one more accepted step
    history = json.loads((out / "history.json").read_text())
    step = len(history) + 1
    print("STEP %d" % step, flush=True)
    history.append({"step": step, "epoch": 2, "action": "accept",
                    "selection_hard": 0.9, "selection_soft": 0.9,
                    "best_score": 0.9, "best_step": step, "skill_len": len(seed),
                    "wall_time_s": 1.0})
    (out / "history.json").write_text(json.dumps(history))
    (out / "best_skill.md").write_text(seed + "\\n\\nRESUMED")
else:
    epochs = int(config["train"]["num_epochs"])
    history = []
    for step in range(1, epochs + 1):
        print("STEP %d" % step, flush=True)
        history.append({"step": step, "epoch": step,
                        "action": "accept" if step % 2 else "reject",
                        "selection_hard": 0.5 + step / 10.0, "selection_soft": 0.6,
                        "best_score": 0.5 + step / 10.0, "best_step": step,
                        "skill_len": len(seed), "wall_time_s": 2.0})
    (out / "history.json").write_text(json.dumps(history))
    (out / "best_skill.md").write_text(seed + "\\n\\nIMPROVED")
state_file.write_text(json.dumps({"last_completed_step": len(history)}))
(out / "summary.json").write_text(json.dumps({
    "total_steps": len(history),
    "total_accepts": sum(1 for h in history if h["action"] == "accept"),
    "total_rejects": sum(1 for h in history if h["action"] == "reject"),
    "total_skips": 0,
    "total_wall_time_s": 2.0 * len(history),
    "baseline_selection_hard": 0.5,
    "best_selection_hard": history[-1]["best_score"],
    "best_step": history[-1]["best_step"],
    "baseline_test_hard": 0.4, "test_hard": 0.9, "final_test_hard": 0.8,
}))
print("[STEP %d done]" % len(history), flush=True)
'''


@pytest.fixture
def lab(tmp_path, monkeypatch, client):
    stub = tmp_path / "stub_train.py"
    stub.write_text(STUB_TRAIN)
    from app.core.config import get_settings

    # A real Settings copy, not a namespace: spawn() derives the child's rlimit
    # profile from it (model_copy) — same rationale as the jobs fixture.
    fake_settings = get_settings().model_copy(
        update={
            "skill_lab_python": sys.executable,
            "skill_lab_target_model_id": "us.anthropic.claude-sonnet-5",
            "skill_lab_judge_model_id": "us.anthropic.claude-sonnet-5",
        }
    )
    monkeypatch.setattr(runner, "TRAIN_SCRIPT", stub)
    monkeypatch.setattr(runner, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(artifacts, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        taskset_svc, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )
    monkeypatch.setattr(runner, "sweep_exec_jobs_prefix", lambda ws, log: None)

    seed_holder = {"text": "# demo skill\n\nDo the thing."}

    def fake_materialize(workspace, record_id, dest_parent, log):
        skill_dir = dest_parent / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(seed_holder["text"])
        (skill_dir / "references" / "note.md").parent.mkdir()
        (skill_dir / "references" / "note.md").write_text("support file")
        return skill_dir, {"kind": "registry", "record_id": record_id, "name": "demo-skill"}

    monkeypatch.setattr(runner, "materialize_registry_skill", fake_materialize)
    set_default_resources(WORKER_RESOURCES)
    client.seed_holder = seed_holder
    return client


def _split_taskset(client, with_test=False, name="train-ts") -> str:
    def tasks(prefix, n=2):
        return [
            {"id": f"{prefix}_{i}", "question": f"q {prefix} {i}", "rubric": "PASS always"}
            for i in range(n)
        ]

    tasks_by_split = {"train": tasks("tr"), "val": tasks("va")}
    if with_test:
        tasks_by_split["test"] = tasks("te")
    response = client.post(
        "/api/skill-lab/tasksets",
        json={"name": name, "mode": "split", "tasks_by_split": tasks_by_split},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _submit_train(client, taskset_id, **overrides):
    body = {
        "type": "train",
        "skill_source": {"kind": "registry", "record_id": "rec-1"},
        "taskset_id": taskset_id,
        "params": {"epochs": 2},
    }
    body.update(overrides)
    return client.post("/api/skill-lab/jobs", json=body)


def _wait(client, job_id, statuses=("succeeded", "failed", "cancelled"), timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/skill-lab/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    pytest.fail(f"job {job_id} stuck (last: {job})")


# ── config generation ──────────────────────────────────────────────────────


def test_train_config_contract_split_mode(lab):
    ts = _split_taskset(lab)  # no test split
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    assert job["status"] == "succeeded", job
    directory = artifacts.job_dir(job["id"])
    config = yaml.safe_load((directory / "config.yaml").read_text())
    # `_base_` must be ABSOLUTE: train.py joins it onto the config's own dir.
    assert config["_base_"].endswith("configs/skilleval/default.yaml")
    assert Path(config["_base_"]).is_absolute()
    assert config["model"]["optimizer_backend"] == "bedrock_chat"
    assert config["model"]["target_backend"] == "claude_code_exec"
    assert config["train"]["num_epochs"] == 2
    # Section.key placement is the contract with the vendored flatten map
    # (skillopt/config.py _FLATTEN_MAP): a param written under the wrong section
    # is silently dropped and the run quietly uses the vendored default.
    assert config["optimizer"]["learning_rate"] == 4  # → edit_budget
    assert config["evaluation"]["gate_metric"] == "soft"  # platform default
    assert config["env"]["judge_mode"] == "auto"
    assert config["env"]["skill_dir"].endswith("/skills/demo-skill")
    assert config["env"]["skill_init"].endswith("/skills/demo-skill/SKILL.md")
    assert config["env"]["workers"] == 2 and config["env"]["timeout"] == 600
    assert config["env"]["split_mode"] == "split_dir"
    # missing test split → backfilled from val, and the duplicate isn't scored
    assert config["evaluation"]["eval_test"] is False
    val = json.loads((directory / "splits" / "val" / "items.json").read_text())
    test = json.loads((directory / "splits" / "test" / "items.json").read_text())
    assert val == test
    assert (directory / "splits" / "train" / "items.json").is_file()


def test_train_config_single_mode_uses_ratio(lab):
    tasks = [
        {"id": f"t_{i}", "question": f"q{i}", "rubric": "PASS"} for i in range(10)
    ]
    ts = lab.post(
        "/api/skill-lab/tasksets",
        json={"name": "single", "mode": "single", "tasks_by_split": {"tasks": tasks}},
    ).json()["id"]
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    config = yaml.safe_load(
        (artifacts.job_dir(job["id"]) / "config.yaml").read_text()
    )
    assert config["env"]["split_mode"] == "ratio"
    assert config["env"]["split_ratio"] == "4:3:3"
    assert config["evaluation"]["eval_test"] is True


def _asset_descriptor(monkeypatch, tmp_path, data: bytes):
    from app.skill_lab import task_assets

    digest = hashlib.sha256(data).hexdigest()
    stage = tmp_path / f"stage-{digest}"
    blob = stage / "blob"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(data)
    record = {
        "staged_asset": f"ta_{digest}",
        "name": "input.pdf",
        "media_type": "application/pdf",
        "size": len(data),
        "sha256": digest,
    }
    monkeypatch.setattr(
        task_assets,
        "resolve_staged",
        lambda _workspace, _token: (record, blob, stage),
    )
    return digest, {
        "staged_asset": record["staged_asset"],
        "name": record["name"],
        "media_type": record["media_type"],
        "size": record["size"],
    }


@pytest.mark.parametrize("mode", ["single", "split"])
def test_train_submission_uses_binary_snapshot_for_both_modes(lab, monkeypatch, tmp_path, mode):
    data = f"%PDF-train-{mode}".encode()
    digest, descriptor = _asset_descriptor(monkeypatch, tmp_path, data)
    task = {
        "id": "asset",
        "question": "quick",
        "rubric": "PASS",
        "files": {"data/input.pdf": descriptor},
    }
    tasks_by_split = (
        {"tasks": [task]}
        if mode == "single"
        else {
            "train": [task],
            "val": [dict(task, id="asset-val")],
        }
    )
    created = lab.post(
        "/api/skill-lab/tasksets",
        json={"name": f"train-{mode}", "mode": mode, "tasks_by_split": tasks_by_split},
    )
    assert created.status_code == 201, created.text
    job = _wait(lab, _submit_train(lab, created.json()["id"]).json()["id"])
    directory = artifacts.job_dir(job["id"])
    inputs = directory / "inputs"
    config = yaml.safe_load((directory / "config.yaml").read_text())
    assert config["env"]["assets_dir"] == str(inputs / "assets")
    assert (inputs / "assets" / digest).read_bytes() == data
    if mode == "single":
        assert config["env"]["data_path"] == str(inputs / "tasks.json")
    else:
        assert json.loads((directory / "splits/train/items.json").read_text())[0]["id"] == "asset"

    live = taskset_svc.taskset_dir(created.json()["id"])
    (live / "assets" / digest).write_bytes(b"changed")
    assert (inputs / "assets" / digest).read_bytes() == data


def test_train_param_bounds_and_split_refusal(lab):
    ts = _split_taskset(lab)
    assert _submit_train(lab, ts, params={"epochs": 99}).status_code == 422
    assert (
        _submit_train(lab, ts, params={"epochs": 1, "gate_metric": "vibes"}).status_code
        == 422
    )
    refused = _submit_train(lab, ts, split="val")
    assert refused.status_code == 422
    assert "whole task set" in refused.json()["message"]


# ── lifecycle + reads ──────────────────────────────────────────────────────


def test_train_lifecycle_summary_and_diff(lab):
    ts = _split_taskset(lab, with_test=True)
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    assert job["status"] == "succeeded"

    summary = lab.get(f"/api/skill-lab/jobs/{job['id']}/train-summary").json()
    assert summary["finished"] is True
    assert summary["totals"] == {
        "steps": 2, "accepts": 1, "rejects": 1, "skips": 0, "wall_time_s": 4.0,
    }
    assert summary["baseline_selection_hard"] == 0.5
    # `final` grades best_skill.md (the trainer's test_hard = what publish ships),
    # not final_test_hard, which is the last accepted skill.
    assert summary["test_scores"] == {"baseline": 0.4, "final": 0.9}
    assert [s["step"] for s in summary["steps"]] == [1, 2]

    diff = lab.get(f"/api/skill-lab/jobs/{job['id']}/diff").json()
    assert diff["changed"] is True
    assert diff["best"].endswith("IMPROVED")
    assert "+IMPROVED" in diff["diff"] or "IMPROVED" in diff["diff"]


def test_train_summary_mid_run_derives_totals(lab):
    job_id = "job_midrun01"
    out = artifacts.out_root(job_id)
    out.mkdir(parents=True)
    (out / "history.json").write_text(
        json.dumps(
            [
                {"step": 1, "action": "accept", "selection_hard": 0.6,
                 "best_score": 0.6, "best_step": 1, "wall_time_s": 3.0},
                {"step": 2, "action": "skip_no_patches", "selection_hard": None,
                 "best_score": 0.6, "best_step": 1, "wall_time_s": 1.0},
            ]
        )
    )
    summary = artifacts.train_summary(job_id)
    assert summary["finished"] is False
    assert summary["totals"] == {
        "steps": 2, "accepts": 1, "rejects": 0, "skips": 1, "wall_time_s": 4.0,
    }
    assert summary["best_score"] == 0.6


def test_train_failure(lab):
    lab.seed_holder["text"] = "# demo\nFAILTRAIN"
    ts = _split_taskset(lab)
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    assert job["status"] == "failed"
    assert "process exited 2" in job["error"]


# ── resume ─────────────────────────────────────────────────────────────────


def test_resume_continues_from_disk_state(lab):
    from app.core.db import SessionLocal
    from app.skill_lab.models import SkillLabJob

    ts = _split_taskset(lab)
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    assert job["status"] == "succeeded"
    db = SessionLocal()
    try:  # simulate a backend restart that interrupted this run
        row = db.get(SkillLabJob, job["id"])
        row.status = "interrupted"
        db.commit()
    finally:
        db.close()

    resumed = lab.post(f"/api/skill-lab/jobs/{job['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    final = _wait(lab, job["id"])
    assert final["status"] == "succeeded"
    summary = lab.get(f"/api/skill-lab/jobs/{job['id']}/train-summary").json()
    assert summary["totals"]["steps"] == 3  # 2 original + 1 resumed
    diff = lab.get(f"/api/skill-lab/jobs/{job['id']}/diff").json()
    assert diff["best"].endswith("RESUMED")


def test_interrupted_train_is_told_to_resume(lab):
    """The restart sweep's copy is type-aware: an eval is re-submitted, a train
    run is resumed (its trainer checkpointed every completed step)."""
    from app.core.db import SessionLocal
    from app.skill_lab import jobs as jobs_svc
    from app.skill_lab.models import SkillLabJob

    db = SessionLocal()
    try:
        db.add(
            SkillLabJob(
                workspace_id="default", type="train", status="running",
                taskset_id="ts_x", skill_source={}, params={},
            )
        )
        db.commit()
    finally:
        db.close()
    jobs_svc.sweep_interrupted_jobs()
    row = lab.get("/api/skill-lab/jobs?type=train").json()[0]
    assert row["status"] == "interrupted"
    assert "resume it" in row["error"]


def test_resume_refusals(lab):
    ts_single = lab.post(
        "/api/skill-lab/tasksets",
        json={"name": "s", "mode": "single",
              "tasks_by_split": {"tasks": [{"id": "a", "question": "q", "rubric": "r"}]}},
    ).json()["id"]
    eval_job = lab.post(
        "/api/skill-lab/jobs",
        json={"type": "eval", "skill_source": {"kind": "registry", "record_id": "r"},
              "taskset_id": ts_single},
    ).json()
    _wait(lab, eval_job["id"], statuses=("succeeded", "failed"))
    refused = lab.post(f"/api/skill-lab/jobs/{eval_job['id']}/resume")
    assert refused.status_code == 400
    assert refused.json()["code"] == "skill_lab.job_not_resumable"


# ── publish ────────────────────────────────────────────────────────────────


@pytest.fixture
def registry_seam(monkeypatch):
    from app.services import registry_console

    calls = SimpleNamespace(update=None, approve=0, status_before="DRAFT")

    def console_get(ws, record_id):
        return {"recordId": record_id, "status": calls.status_before,
                "recordVersion": "1.0.0-skill"}

    def update_record(record_id, ws, **kwargs):
        calls.update = (record_id, kwargs)
        return {"recordId": record_id, "status": "DRAFT", "recordVersion": "1.1.0-skill"}

    def console_action(ws, record_id, action):
        assert action == "approve"
        calls.approve += 1
        return {"recordId": record_id, "status": "APPROVED",
                "recordVersion": "1.1.0-skill"}

    monkeypatch.setattr(registry_console, "console_get", console_get)
    monkeypatch.setattr(registry_console, "update_record", update_record)
    monkeypatch.setattr(registry_console, "console_action", console_action)
    return calls


def test_publish_happy_path(lab, registry_seam):
    ts = _split_taskset(lab)
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    result = lab.post(f"/api/skill-lab/jobs/{job['id']}/publish", json={}).json()
    assert result["new_version"] == "1.1.0-skill"
    assert result["status_before"] == "DRAFT" and result["status_after"] == "DRAFT"
    assert result["reapproved"] is False
    record_id, kwargs = registry_seam.update
    assert record_id == "rec-1"
    assert kwargs["skill_md"].endswith("IMPROVED")
    log = lab.get(f"/api/skill-lab/jobs/{job['id']}/log").json()["content"]
    assert "[publish]" in log


def test_trainable_files_bundle_flow(lab, registry_seam):
    """Multi-doc training end to end with the REAL vendored bundle codec:
    submit builds seed_bundle.md (FILE headers, SKILL.md last) and points
    skill_init at it; publish splits best_skill.md back onto the original dir
    and pushes a whole-bundle update instead of skill_md."""
    ts = _split_taskset(lab)
    job = _submit_train(
        lab, ts, params={"epochs": 2, "trainable_files": ["references/note.md"]}
    ).json()
    assert job["params"]["trainable_files"] == ["references/note.md"]

    config = yaml.safe_load(
        (artifacts.job_dir(job["id"]) / "config.yaml").read_text()
    )
    seed_path = Path(config["env"]["skill_init"])
    assert seed_path.name == "seed_bundle.md"
    assert config["env"]["trainable_files"] == ["references/note.md"]
    seed = seed_path.read_text()
    assert seed.index("<!-- FILE: references/note.md -->") < seed.index(
        "<!-- FILE: SKILL.md -->"
    )  # SKILL.md last, so tail-appends grow SKILL.md

    job = _wait(lab, job["id"])
    assert job["status"] == "succeeded"
    result = lab.post(f"/api/skill-lab/jobs/{job['id']}/publish", json={}).json()
    assert result["new_version"] == "1.1.0-skill"
    record_id, kwargs = registry_seam.update
    assert record_id == "rec-1" and "skill_md" not in kwargs
    assert set(kwargs["bundle"].files) == {"SKILL.md", "references/note.md"}
    # The stub trainer appends IMPROVED at the bundle tail = the SKILL.md section.
    assert "IMPROVED" in (kwargs["bundle"].root / "SKILL.md").read_text()
    assert (kwargs["bundle"].root / "references" / "note.md").read_text().strip() == (
        "support file"
    )


def test_trainable_files_validation(lab):
    ts = _split_taskset(lab, name="val-ts")
    for bad, code in (
        (["../escape.md"], 422),
        (["SKILL.md"], 422),
        ("references/note.md", 422),  # string, not a list
        (["references/missing.md"], 422),  # bundle build: file not found
    ):
        refused = _submit_train(
            lab, ts, params={"epochs": 1, "trainable_files": bad}
        )
        assert refused.status_code == code, refused.text
        assert refused.json()["code"] == "skill_lab.bad_params"


def test_publish_reapproves_only_previously_approved(lab, registry_seam):
    registry_seam.status_before = "APPROVED"
    ts = _split_taskset(lab)
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    result = lab.post(
        f"/api/skill-lab/jobs/{job['id']}/publish", json={"reapprove": True}
    ).json()
    assert result["reapproved"] is True
    assert result["status_after"] == "APPROVED"
    assert registry_seam.approve == 1


def test_publish_refusals(lab, registry_seam, monkeypatch):
    ts = _split_taskset(lab)
    job = _wait(lab, _submit_train(lab, ts).json()["id"])

    # not a train/succeeded job
    ts2 = lab.post(
        "/api/skill-lab/tasksets",
        json={"name": "s2", "mode": "single",
              "tasks_by_split": {"tasks": [{"id": "a", "question": "q", "rubric": "r"}]}},
    ).json()["id"]
    eval_job = lab.post(
        "/api/skill-lab/jobs",
        json={"type": "eval", "skill_source": {"kind": "registry", "record_id": "r"},
              "taskset_id": ts2},
    ).json()
    _wait(lab, eval_job["id"], statuses=("succeeded", "failed"))
    assert (
        lab.post(f"/api/skill-lab/jobs/{eval_job['id']}/publish", json={}).status_code
        == 400
    )

    # upload-sourced job
    from app.core.db import SessionLocal
    from app.skill_lab.models import SkillLabJob

    db = SessionLocal()
    try:
        row = db.get(SkillLabJob, job["id"])
        row.skill_source = {"kind": "upload", "name": "demo-skill"}
        db.commit()
    finally:
        db.close()
    refused = lab.post(f"/api/skill-lab/jobs/{job['id']}/publish", json={})
    assert refused.status_code == 400
    assert refused.json()["code"] == "skill_lab.publish_unsupported"


def test_publish_no_change_refused(lab, registry_seam):
    ts = _split_taskset(lab)
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    out = artifacts.out_root(job["id"])
    seed = (out / "skills" / "skill_v0000.md").read_text()
    (out / "best_skill.md").write_text(seed)  # no accepted edits
    refused = lab.post(f"/api/skill-lab/jobs/{job['id']}/publish", json={})
    assert refused.status_code == 400
    assert refused.json()["code"] == "skill_lab.publish_no_change"


def test_publish_records_nothing_on_registry_failure(lab, registry_seam, monkeypatch):
    from app.services import registry_console

    def boom(record_id, ws, **kwargs):
        raise AppError("registry.not_editable", "record is DEPRECATED", status_code=400)

    monkeypatch.setattr(registry_console, "update_record", boom)
    ts = _split_taskset(lab)
    job = _wait(lab, _submit_train(lab, ts).json()["id"])
    refused = lab.post(f"/api/skill-lab/jobs/{job['id']}/publish", json={})
    assert refused.status_code == 400
    log = lab.get(f"/api/skill-lab/jobs/{job['id']}/log").json()["content"]
    assert "[publish]" not in log
